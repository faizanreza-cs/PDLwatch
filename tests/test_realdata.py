import json

import httpx
import yaml

from pdlwatch import realdata
from pdlwatch.synth.states import STATES

PDF = b"%PDF-1.4 fake body"
LANDING = """<html><body>
<a href="/files/PDL%20July%202026.pdf">Preferred Drug List (July 2026)</a>
<a href="/files/minutes.pdf">P&amp;T committee minutes</a>
<a href="https://other.example.org/archive/pdl-april-2026.pdf"><span>PDL</span> archive</a>
<a href="/files/PDL%20July%202026.pdf">same file again</a>
<a href="/about.html">About the PDL</a>
</body></html>"""


def _handler(request: httpx.Request) -> httpx.Response:
    host, path = request.url.host, request.url.path
    if path == "/robots.txt":
        if host == "blocked.example.org":
            return httpx.Response(200, text="User-agent: *\nDisallow: /")
        return httpx.Response(404)
    if path == "/pdl":
        return httpx.Response(200, text=LANDING, headers={"content-type": "text/html"})
    if path == "/missing.pdf":
        return httpx.Response(404)
    if path == "/page.pdf":
        return httpx.Response(200, text="<html>moved</html>", headers={"content-type": "text/html"})
    return httpx.Response(200, content=PDF, headers={"content-type": "application/pdf"})


def _sources(tmp_path, entries):
    path = tmp_path / "sources.yaml"
    path.write_text(yaml.safe_dump(entries))
    return path


def test_fetch_outcomes_and_manifest(tmp_path):
    src = _sources(tmp_path, [
        {"state": "OH", "title": "direct", "url": "https://ok.example.org/files/ohio%20pdl.pdf"},
        {"state": "TX", "title": "landing page", "page": "https://ok.example.org/pdl",
         "match": "(?i)pdl", "max": 3},
        {"state": "IA", "title": "gone", "url": "https://ok.example.org/missing.pdf"},
        {"state": "VT", "title": "html", "url": "https://ok.example.org/page.pdf"},
        {"state": "UT", "title": "robots", "url": "https://blocked.example.org/x.pdf"},
    ])  # fmt: skip
    client = httpx.Client(transport=httpx.MockTransport(_handler))
    got = realdata.fetch(src, tmp_path / "out", client=client, delay=0)
    status = {(f.state, f.url): f.status for f in got}
    assert status[("OH", "https://ok.example.org/files/ohio%20pdl.pdf")] == "ok"
    tx = [f for f in got if f.state == "TX"]
    assert [f.url for f in tx] == [
        "https://ok.example.org/files/PDL%20July%202026.pdf",
        "https://other.example.org/archive/pdl-april-2026.pdf",
    ]
    assert all(f.status == "ok" for f in tx)  # minutes and the html page don't match
    assert status[("IA", "https://ok.example.org/missing.pdf")] == "not_found"
    assert status[("VT", "https://ok.example.org/page.pdf")] == "not_pdf"
    assert status[("UT", "https://blocked.example.org/x.pdf")] == "blocked"
    files = sorted(p.name for p in (tmp_path / "out" / "pdfs").iterdir())
    assert files == ["OH-ohio-pdl.pdf", "TX-PDL-July-2026.pdf", "TX-pdl-april-2026.pdf"]
    manifest = json.loads((tmp_path / "out" / "manifest.json").read_text())
    assert len(manifest) == len(got) and all(m["sha256"] for m in manifest if m["status"] == "ok")


def test_requests_identify_the_project_and_are_spaced(tmp_path):
    seen = []

    def handler(request):
        seen.append(request.headers.get("user-agent"))
        return _handler(request)

    client = httpx.Client(transport=httpx.MockTransport(handler),
                          headers={"User-Agent": realdata.USER_AGENT})  # fmt: skip
    p = realdata.Politeness(client, delay=0.2)
    import time

    t0 = time.monotonic()
    p.get("https://ok.example.org/a.pdf")
    p.get("https://ok.example.org/b.pdf")
    assert time.monotonic() - t0 >= 0.2  # one request per delay per host
    assert all("pdlwatch" in ua for ua in seen)


def test_sources_file_is_well_formed():
    from pathlib import Path

    entries = yaml.safe_load(Path("sources/medicaid_pdls.yaml").read_text())
    assert len(entries) >= 30
    for e in entries:
        assert e["state"] in STATES and e["title"]
        assert ("url" in e) != ("page" in e)
        assert (e.get("url") or e["page"]).startswith("https://")
    assert len({e["state"] for e in entries}) >= 15
