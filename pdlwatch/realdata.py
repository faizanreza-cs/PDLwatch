"""Real state PDLs: download the public PDFs listed in ``sources/medicaid_pdls.yaml``, then run
the same extraction on them.

Polite by design: one request per second per host, a User-Agent that names the project,
robots.txt honoured, only PDF and spreadsheet bodies kept (Washington posts its weekly list as
Excel), and a size cap. Every source's outcome (downloaded, unchanged, not found, blocked by
robots.txt, not a document) goes into ``manifest.json`` and the results, so a stale link shows
up as a line to update rather than a silent gap.

Built to run every week: files are stored by content (``<state>-<name>-<sha8>.pdf``), so a new
version posted under the same address never overwrites the old one, and every outcome is
appended to ``ledger.jsonl``. The next run asks each server whether the file changed
(``If-None-Match`` / ``If-Modified-Since``) and downloads only what did.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urljoin, urlparse
from urllib.robotparser import RobotFileParser

import httpx
import yaml

from pdlwatch.config import Settings, get_settings, results_path

USER_AGENT = "pdlwatch/0.1 (+https://github.com/faizanreza-cs/pdlwatch)"
MAX_BYTES = 40 * 1024 * 1024
MATCH = "(?i)pdl"  # a page entry's default pattern
_LINK = re.compile(r"<a\b[^>]*?href\s*=\s*[\"']([^\"']+)[\"'][^>]*>(.*?)</a>", re.I | re.S)
_TAGS = re.compile(r"<[^>]+>")


@dataclass
class Fetched:
    state: str
    title: str
    source: str  # the url or page from the sources file
    url: str | None  # the PDF actually requested
    status: str  # ok | not_modified | not_found | blocked | not_pdf | too_large | error
    file: str | None = None
    bytes: int = 0
    sha256: str | None = None
    detail: str = ""
    etag: str | None = None
    last_modified: str | None = None
    fetched_at: str = ""
    new: bool = False  # content this archive hadn't seen: a new document or a new version
    label: str = ""  # the link's text on the source page ("Preferred Drug List, Q2 2026")


class Politeness:
    """robots.txt and one request per second per host."""

    def __init__(self, client: httpx.Client, delay: float = 1.0) -> None:
        self.client, self.delay = client, delay
        self._robots: dict[str, RobotFileParser | None] = {}
        self._last: dict[str, float] = {}

    def allowed(self, url: str) -> bool:
        host = urlparse(url).scheme + "://" + urlparse(url).netloc
        if host not in self._robots:
            rp: RobotFileParser | None = RobotFileParser()
            try:
                r = self.get(host + "/robots.txt")
                if r.status_code == 200:
                    assert rp is not None
                    rp.parse(r.text.splitlines())
                else:
                    rp = None  # no robots.txt: nothing is disallowed
            except httpx.HTTPError:
                rp = None
            self._robots[host] = rp
        rp = self._robots[host]
        return rp is None or rp.can_fetch(USER_AGENT, url)

    def get(self, url: str, **kw: Any) -> httpx.Response:
        host = urlparse(url).netloc
        wait = self._last.get(host, 0) + self.delay - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        try:
            return self.client.get(url, **kw)
        finally:
            self._last[host] = time.monotonic()


def _document_link(url: str) -> bool:
    """A link to a PDL file: a ".pdf", ".xlsx" or ".csv" address, or a content system's file
    link that names no type ("/media/41358/download"); what comes back is checked anyway."""
    path = urlparse(url).path.lower()
    return ".pdf" in url.lower() or path.endswith(("/download", ".xlsx", ".xlsm", ".csv"))


def _kind(body: bytes, url: str, content_type: str) -> str | None:
    """The file type of a downloaded body: ".pdf", ".xlsx" or ".csv", or None for anything
    else (an HTML page served in place of a missing file, an image)."""
    if body.startswith(b"%PDF"):
        return ".pdf"
    path = urlparse(url).path.lower()
    if body.startswith(b"PK\x03\x04") and (path.endswith((".xlsx", ".xlsm"))
                                            or "spreadsheet" in content_type):  # fmt: skip
        return ".xlsx"
    if path.endswith(".csv") and "html" not in content_type and b"<html" not in body[:512].lower():
        return ".csv"
    return None


def files_on_page(html: str, base: str, limit: int = 10,
                  pattern: str | None = None) -> list[tuple[str, str]]:  # fmt: skip
    """(address, link text) of the document links on a page (PDF, Excel, CSV or a content
    system's download link): what to look at when none matched a source's pattern. A page with
    no document links at all gives its links that match ``pattern`` instead: the pages one
    step further that may hold the list."""
    docs: dict[str, str] = {}
    pages: dict[str, str] = {}
    for href, text in _LINK.findall(html):
        url = urljoin(base, href.strip())
        label = " ".join(_TAGS.sub(" ", text).split())
        if re.search(r"\.(pdf|xlsx?|csv)\b|/download\b", urlparse(url).path, re.I):
            docs.setdefault(url, label)
        # a page's path and text, not its site's name ("papdl.com/about" is not a PDL)
        elif (
            pattern
            and url.startswith("http")
            and url.split("#")[0] != base
            and re.search(pattern, f"{urlparse(url).path} {label}")
        ):
            pages.setdefault(url, label)
    return list((docs or pages).items())[:limit]


def _newness(url: str, label: str) -> tuple[str, int]:
    """How recent a link looks: a date in its text or address, then the largest number in its
    address (content systems number uploads in order: "/media/41358/download")."""
    from pdlwatch.extract.document import date_hint

    numbers = [int(n) for n in re.findall(r"\d+", urlparse(url).path)]
    return date_hint(label) or date_hint(unquote(url)) or "", max(numbers, default=0)


def discover(html: str, base: str, pattern: str, limit: int,
             newest_first: bool = False) -> list[tuple[str, str]]:  # fmt: skip
    """(address, link text) of the PDF links on a page whose address or text match
    ``pattern``: the first ``limit`` in page order, or the newest-looking with
    ``newest_first`` (for pages that list old versions first)."""
    out: dict[str, str] = {}
    for href, text in _LINK.findall(html):
        url = urljoin(base, href.strip())
        label = " ".join(_TAGS.sub(" ", text).split())
        if _document_link(url) and re.search(pattern, url + " " + label) and url not in out:
            out[url] = label
    links = list(out.items())
    if newest_first:
        links.sort(key=lambda link: _newness(*link), reverse=True)
    return links[:limit]


def _slug(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "-", text).strip("-")[:70]


LEDGER = "ledger.jsonl"


def read_ledger(out: Path) -> list[dict[str, Any]]:
    """Every fetch outcome so far, oldest first."""
    path = out / LEDGER
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def selecting(entries: list[dict[str, Any]],
              ledger: list[dict[str, Any]]) -> dict[str, dict[str, Any] | None]:  # fmt: skip
    """For each archived file (by stem), the listed source that selects it: an entry with its
    address as ``url``, or with the ``page`` it was found on and a ``match`` its address or link
    text still matches. None when no listed source selects it any more."""

    def selects(s: dict[str, Any], e: dict[str, Any]) -> bool:
        if s["state"] != e.get("state"):
            return False
        if "url" in s:
            return bool(s["url"] == e.get("url"))
        text = f"{e.get('url') or ''} {e.get('label') or ''}"
        return s.get("page") == e.get("source") and bool(re.search(s.get("match", MATCH), text))

    out: dict[str, dict[str, Any] | None] = {}
    for e in ledger:
        if e.get("file"):
            stem = Path(e["file"]).stem
            if out.get(stem) is None:
                out[stem] = next((s for s in entries if selects(s, e)), None)
    return out


def retired(entries: list[dict[str, Any]], ledger: list[dict[str, Any]]) -> set[str]:
    """Archived files no listed source selects any more: the entry that fetched them was
    removed from the sources file, or its page's ``match`` was narrowed to leave them out (a
    stale copy, a FAQ). They stay in the archive but out of the tables, so the sources file
    decides what is delivered. Older versions a listed page still matches stay in: history."""
    return {stem for stem, s in selecting(entries, ledger).items() if s is None}


def _download(p: Politeness, state: str, title: str, source: str, url: str, folder: Path,
              previous: dict[str, Any] | None, by_sha: dict[str, str],
              label: str = "") -> Fetched:  # fmt: skip
    now = datetime.now(UTC).isoformat(timespec="seconds")
    f = Fetched(state, title, source, url, "error", fetched_at=now)
    if not p.allowed(url):
        f.status = "blocked"
        f.detail = "disallowed by robots.txt"
        return f
    headers = {}
    if previous:  # we hold this file: ask the server whether it changed
        if previous.get("etag"):
            headers["If-None-Match"] = previous["etag"]
        if previous.get("last_modified"):
            headers["If-Modified-Since"] = previous["last_modified"]
    try:
        r = p.get(url, headers=headers)
    except httpx.HTTPError as e:
        f.detail = type(e).__name__
        return f
    if r.status_code == 304 and previous:
        f.status, f.file, f.sha256 = "not_modified", previous["file"], previous["sha256"]
        f.bytes, f.etag = previous.get("bytes", 0), previous.get("etag")
        f.last_modified = previous.get("last_modified")
        return f
    if r.status_code != 200:
        f.status, f.detail = "not_found", f"HTTP {r.status_code}"
        return f
    body = r.content
    if len(body) > MAX_BYTES:
        f.status, f.detail = "too_large", f"{len(body):,} bytes"
        return f
    kind = _kind(body, url, r.headers.get("content-type", ""))
    if kind is None:
        f.status, f.detail = "not_pdf", r.headers.get("content-type", "")
        return f
    sha = hashlib.sha256(body).hexdigest()
    name = by_sha.get(f"{state}:{sha}")  # a state's same bytes under any address: stored once
    if name is None:
        stem = Path(unquote(urlparse(url).path)).stem
        if stem.lower() in {"download", "file", "view", "index"} or stem.isdigit():
            stem = label or title  # "/media/41358/download" says nothing: its link text does
        name = f"{state}-{_slug(stem)}-{sha[:8]}{kind}"
        (folder / name).write_bytes(body)
        by_sha[f"{state}:{sha}"] = name
        f.new = True
    f.status, f.file, f.bytes, f.sha256 = "ok", name, len(body), sha
    f.etag, f.last_modified = r.headers.get("etag"), r.headers.get("last-modified")
    return f


def fetch(sources: Path, out: Path, states: list[str] | None = None,
          client: httpx.Client | None = None, delay: float = 1.0) -> list[Fetched]:  # fmt: skip
    """Download every source (or those of ``states``) into ``out/pdfs``: new files only, kept
    by content. Writes this run's ``manifest.json`` and appends to ``ledger.jsonl``."""
    entries = yaml.safe_load(sources.read_text())
    if states:
        entries = [e for e in entries if e["state"] in states]
    folder = out / "pdfs"
    folder.mkdir(parents=True, exist_ok=True)
    previous: dict[str, dict[str, Any]] = {}  # url -> its latest outcome with a file we hold
    by_sha: dict[str, str] = {}
    for e in read_ledger(out):
        if e.get("file") and (folder / e["file"]).exists():
            previous[e["url"]] = e
            by_sha[f"{e['state']}:{e['sha256']}"] = e["file"]
    own = client is None
    client = client or httpx.Client(headers={"User-Agent": USER_AGENT}, timeout=60,
                                    follow_redirects=True)  # fmt: skip
    p = Politeness(client, delay)
    results: list[Fetched] = []
    seen: set[str] = set()

    def report(f: Fetched) -> None:  # every outcome goes in the manifest and the log
        results.append(f)
        shown = f" ({f.label})" if f.label else ""
        print(f"[sync] {f.state} {f.status:12} {f.url or f.source}{shown} {f.detail}", flush=True)

    try:
        for e in entries:
            state, title = e["state"], e["title"]
            if "url" in e:
                links = [(e["url"], "")]
            else:
                if not p.allowed(e["page"]):
                    report(Fetched(state, title, e["page"], None, "blocked",
                                   detail="disallowed by robots.txt"))  # fmt: skip
                    continue
                try:
                    r = p.get(e["page"])
                    r.raise_for_status()
                    links = discover(r.text, str(r.url), e.get("match", MATCH),
                                     e.get("max", 3), bool(e.get("newest_first")))  # fmt: skip
                except httpx.HTTPError as err:
                    report(Fetched(state, title, e["page"], None, "error",
                                   detail=type(err).__name__))  # fmt: skip
                    continue
                if not links:
                    report(Fetched(state, title, e["page"], None, "not_found",
                                   detail="no matching document links on the page"))  # fmt: skip
                    for url, label in files_on_page(r.text, str(r.url),
                                                    pattern=e.get("match", MATCH)):  # fmt: skip
                        print(f"[sync] {state} on the page:  {url} ({label})", flush=True)
            for url, label in links:
                if url in seen:
                    continue
                seen.add(url)
                f = _download(p, state, title, e.get("url") or e["page"], url, folder,
                              previous.get(url), by_sha, label)  # fmt: skip
                f.label = label
                report(f)
    finally:
        if own:
            client.close()
    (out / "manifest.json").write_text(json.dumps([asdict(f) for f in results], indent=1))
    with (out / LEDGER).open("a") as fh:
        for f in results:
            fh.write(json.dumps(asdict(f)) + "\n")
    return results


def fetch_and_build(sources: Path, out: Path, states: list[str] | None = None, llm: Any = None,
                    settings: Settings | None = None) -> dict[str, Any]:  # fmt: skip
    """Fetch the PDFs, extract them into the warehouse, and record what was fetched."""
    from pdlwatch.pipeline import run_folder

    s = settings or get_settings()
    entries = yaml.safe_load(sources.read_text())
    got = fetch(sources, out, states)
    held = [f for f in got if f.status in {"ok", "not_modified"}]
    # every version ever fetched stays in the folder: states for all of them, from the ledger,
    # and for undated PDFs a date from the source's title or the file's original name
    ledger = read_ledger(out)
    by_file = {Path(e["file"]).stem: e["state"] for e in ledger if e.get("file")}
    hints: dict[str, dict[str, str]] = {}
    for e in ledger:  # oldest first: the first sighting is when the file was first seen
        if e.get("file"):
            name = Path(unquote(urlparse(e.get("url") or "").path)).stem
            title = f"{e.get('label', '')} {e.get('title', '')}".strip()  # the link's text first
            hints.setdefault(
                Path(e["file"]).stem,
                {"title": title, "name": name, "first_seen": e.get("fetched_at", "")},
            )
    chosen = selecting(entries, ledger)
    gone = {stem for stem, e in chosen.items() if e is None}
    for stem in sorted(gone):
        print(f"[sync] left out      {stem}: no listed source selects it any more", flush=True)
    # a spreadsheet is read through the profile of the source that selects it
    profiles = {stem: e["profile"] for stem, e in chosen.items() if e and e.get("profile")}
    res = run_folder(out / "pdfs", s, by_file, llm=llm, source="state Medicaid PDLs (public PDFs)",
                     hints=hints, skip=gone, profiles=profiles)  # fmt: skip
    statuses: dict[str, int] = {}
    for f in got:
        statuses[f.status] = statuses.get(f.status, 0) + 1
    res["fetch"] = {
        "sources": len(entries),
        "pdfs_current": len(held),
        "new_files": sum(f.new for f in got),
        "states": sorted({f.state for f in held}),
        "megabytes": round(sum(f.bytes for f in held) / 1e6, 1),
        "outcomes": statuses,
        "failed": [asdict(f) for f in got if f.status not in {"ok", "not_modified"}],
        "left_out": sorted(gone),
    }
    results_path(s.db_path).write_text(json.dumps(res, indent=1, default=str))
    return res
