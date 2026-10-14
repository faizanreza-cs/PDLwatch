"""Real state PDLs: download the public PDFs listed in ``sources/medicaid_pdls.yaml``, then run
the same extraction on them.

Polite by design: one request per second per host, a User-Agent that names the project,
robots.txt honoured, only ``application/pdf`` bodies kept, and a size cap. Every source's outcome
(downloaded, unchanged, not found, blocked by robots.txt, not a PDF) goes into ``manifest.json``
and the results, so a stale link shows up as a line to update rather than a silent gap.

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


def discover(html: str, base: str, pattern: str, limit: int) -> list[str]:
    """PDF links on a page whose address or link text match ``pattern``, in page order."""
    out: list[str] = []
    for href, text in _LINK.findall(html):
        url = urljoin(base, href.strip())
        label = " ".join(_TAGS.sub(" ", text).split())
        if ".pdf" not in url.lower() or not re.search(pattern, url + " " + label):
            continue
        if url not in out:
            out.append(url)
        if len(out) >= limit:
            break
    return out


def _slug(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "-", text).strip("-")[:70]


LEDGER = "ledger.jsonl"


def read_ledger(out: Path) -> list[dict[str, Any]]:
    """Every fetch outcome so far, oldest first."""
    path = out / LEDGER
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def _download(p: Politeness, state: str, title: str, source: str, url: str, folder: Path,
              previous: dict[str, Any] | None, by_sha: dict[str, str]) -> Fetched:  # fmt: skip
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
    if not body.startswith(b"%PDF"):
        f.status, f.detail = "not_pdf", r.headers.get("content-type", "")
        return f
    sha = hashlib.sha256(body).hexdigest()
    name = by_sha.get(f"{state}:{sha}")  # a state's same bytes under any address: stored once
    if name is None:
        name = f"{state}-{_slug(Path(unquote(urlparse(url).path)).stem)}-{sha[:8]}.pdf"
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
    try:
        for e in entries:
            state, title = e["state"], e["title"]
            if "url" in e:
                urls = [e["url"]]
            else:
                if not p.allowed(e["page"]):
                    results.append(Fetched(state, title, e["page"], None, "blocked",
                                           detail="disallowed by robots.txt"))  # fmt: skip
                    continue
                try:
                    r = p.get(e["page"])
                    r.raise_for_status()
                    urls = discover(r.text, str(r.url), e.get("match", "(?i)pdl"), e.get("max", 3))
                except httpx.HTTPError as err:
                    results.append(Fetched(state, title, e["page"], None, "error",
                                           detail=type(err).__name__))  # fmt: skip
                    continue
                if not urls:
                    results.append(Fetched(state, title, e["page"], None, "not_found",
                                           detail="no matching PDF links on the page"))  # fmt: skip
            for url in urls:
                if url in seen:
                    continue
                seen.add(url)
                f = _download(p, state, title, e.get("url") or e["page"], url, folder,
                              previous.get(url), by_sha)  # fmt: skip
                results.append(f)
                print(f"[sync] {state} {f.status:12} {url} {f.detail}", flush=True)
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
    got = fetch(sources, out, states)
    held = [f for f in got if f.status in {"ok", "not_modified"}]
    # every version ever fetched stays in the folder: states for all of them, from the ledger
    by_file = {Path(e["file"]).stem: e["state"] for e in read_ledger(out) if e.get("file")}
    res = run_folder(out / "pdfs", s, by_file, llm=llm, source="state Medicaid PDLs (public PDFs)")
    statuses: dict[str, int] = {}
    for f in got:
        statuses[f.status] = statuses.get(f.status, 0) + 1
    res["fetch"] = {
        "sources": len(yaml.safe_load(sources.read_text())),
        "pdfs_current": len(held),
        "new_files": sum(f.new for f in got),
        "states": sorted({f.state for f in held}),
        "megabytes": round(sum(f.bytes for f in held) / 1e6, 1),
        "outcomes": statuses,
        "failed": [asdict(f) for f in got if f.status not in {"ok", "not_modified"}],
    }
    results_path(s.db_path).write_text(json.dumps(res, indent=1, default=str))
    return res
