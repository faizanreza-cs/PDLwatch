"""Real state PDLs: download the public PDFs listed in ``sources/medicaid_pdls.yaml``, then run
the same extraction on them.

Polite by design: one request per second per host, a User-Agent that names the project,
robots.txt honoured, only ``application/pdf`` bodies kept, and a size cap. Every source's outcome
(downloaded, not found, blocked by robots.txt, not a PDF) goes into ``manifest.json`` and the
results, so a stale link shows up as a line to update rather than a silent gap.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from dataclasses import asdict, dataclass
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
    status: str  # ok | not_found | blocked | not_pdf | too_large | error
    file: str | None = None
    bytes: int = 0
    sha256: str | None = None
    detail: str = ""


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


def _download(p: Politeness, state: str, title: str, source: str, url: str,
              folder: Path) -> Fetched:  # fmt: skip
    f = Fetched(state, title, source, url, "error")
    if not p.allowed(url):
        f.status = "blocked"
        f.detail = "disallowed by robots.txt"
        return f
    try:
        r = p.get(url)
    except httpx.HTTPError as e:
        f.detail = type(e).__name__
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
    name = f"{state}-{_slug(Path(unquote(urlparse(url).path)).stem)}.pdf"
    (folder / name).write_bytes(body)
    f.status, f.file, f.bytes = "ok", name, len(body)
    f.sha256 = hashlib.sha256(body).hexdigest()
    return f


def fetch(sources: Path, out: Path, states: list[str] | None = None,
          client: httpx.Client | None = None, delay: float = 1.0) -> list[Fetched]:  # fmt: skip
    """Download every source (or those of ``states``) into ``out/pdfs``; write a manifest."""
    entries = yaml.safe_load(sources.read_text())
    if states:
        entries = [e for e in entries if e["state"] in states]
    folder = out / "pdfs"
    folder.mkdir(parents=True, exist_ok=True)
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
                f = _download(p, state, title, e.get("url") or e["page"], url, folder)
                results.append(f)
                print(f"[fetch] {state} {f.status:9} {url} {f.detail}", flush=True)
    finally:
        if own:
            client.close()
    (out / "manifest.json").write_text(json.dumps([asdict(f) for f in results], indent=1))
    return results


def fetch_and_build(sources: Path, out: Path, states: list[str] | None = None, llm: Any = None,
                    settings: Settings | None = None) -> dict[str, Any]:  # fmt: skip
    """Fetch the PDFs, extract them into the warehouse, and record what was fetched."""
    from pdlwatch.pipeline import run_folder

    s = settings or get_settings()
    got = fetch(sources, out, states)
    ok = [f for f in got if f.status == "ok"]
    by_file = {Path(f.file).stem: f.state for f in ok if f.file}
    res = run_folder(out / "pdfs", s, by_file, llm=llm, source="state Medicaid PDLs (public PDFs)")
    statuses: dict[str, int] = {}
    for f in got:
        statuses[f.status] = statuses.get(f.status, 0) + 1
    res["fetch"] = {
        "sources": len(yaml.safe_load(sources.read_text())),
        "pdfs_downloaded": len(ok),
        "states": sorted({f.state for f in ok}),
        "megabytes": round(sum(f.bytes for f in ok) / 1e6, 1),
        "outcomes": statuses,
        "failed": [asdict(f) for f in got if f.status != "ok"],
    }
    results_path(s.db_path).write_text(json.dumps(res, indent=1, default=str))
    return res
