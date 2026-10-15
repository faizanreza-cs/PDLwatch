import json
import re

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
<a href="/media/41358/download">Preferred Drug List, Q2 2026</a>
<a href="/media/41359/download">Pharmacy newsletter</a>
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
    # each address serves its own bytes, as real files do
    body = PDF + request.url.path.encode()
    return httpx.Response(200, content=body, headers={"content-type": "application/pdf"})


def _sources(tmp_path, entries):
    path = tmp_path / "sources.yaml"
    path.write_text(yaml.safe_dump(entries))
    return path


def test_fetch_outcomes_and_manifest(tmp_path):
    src = _sources(tmp_path, [
        {"state": "OH", "title": "direct", "url": "https://ok.example.org/files/ohio%20pdl.pdf"},
        {"state": "TX", "title": "landing page", "page": "https://ok.example.org/pdl",
         "match": "(?i)pdl|preferred drug list", "max": 3},
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
        "https://ok.example.org/media/41358/download",  # a file link that names no type
    ]
    assert all(f.status == "ok" for f in tx)  # minutes and the html page don't match
    assert status[("IA", "https://ok.example.org/missing.pdf")] == "not_found"
    assert status[("VT", "https://ok.example.org/page.pdf")] == "not_pdf"
    assert status[("UT", "https://blocked.example.org/x.pdf")] == "blocked"
    files = sorted(p.name for p in (tmp_path / "out" / "pdfs").iterdir())
    assert [re.sub(r"-[0-9a-f]{8}\.pdf$", "", f) for f in files] == [
        "OH-ohio-pdl",
        "TX-PDL-July-2026",
        "TX-Preferred-Drug-List-Q2-2026",  # named after its link when the address names nothing
        "TX-pdl-april-2026",
    ]  # stored by content
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


def test_unchanged_files_are_not_downloaded_and_new_versions_kept(tmp_path):
    """A weekly run: 304 when nothing changed; a new version under the same address is kept
    beside the old one, so the changes between them can be found."""
    state = {"body": PDF + b" v1", "etag": '"v1"'}
    seen = []

    def handler(request):
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        seen.append(request.headers.get("if-none-match"))
        if request.headers.get("if-none-match") == state["etag"]:
            return httpx.Response(304)
        return httpx.Response(200, content=state["body"], headers={"etag": state["etag"]})

    src = _sources(
        tmp_path, [{"state": "OH", "title": "PDL", "url": "https://ok.example.org/pdl.pdf"}]
    )
    out = tmp_path / "out"

    def run():
        client = httpx.Client(transport=httpx.MockTransport(handler))
        return realdata.fetch(src, out, client=client, delay=0)[0]

    first = run()
    assert first.status == "ok" and first.new and seen == [None]
    again = run()
    assert again.status == "not_modified" and again.file == first.file and seen[-1] == '"v1"'
    state.update(body=PDF + b" v2", etag='"v2"')
    newer = run()
    assert newer.status == "ok" and newer.new and newer.file != first.file
    assert sorted(p.name for p in (out / "pdfs").iterdir()) == sorted([first.file, newer.file])
    ledger = realdata.read_ledger(out)
    assert [e["status"] for e in ledger] == ["ok", "not_modified", "ok"]


def test_pages_that_list_old_versions_first_can_be_read_newest_first():
    html = """
    <a href="/media/26936/download">Preferred Drug List, effective April 1, 2025</a>
    <a href="/media/27001/download">PDL change notice</a>
    <a href="/media/41358/download">Preferred Drug List, effective April 1, 2026</a>
    <a href="/media/40870/download">PDL archive</a>
    """
    base = "https://bms.example.gov/pdl"
    first = realdata.discover(html, base, "(?i)pdl|preferred", 2)
    assert [u.rsplit("/", 2)[1] for u, _ in first] == ["26936", "27001"]  # page order
    newest = realdata.discover(html, base, "(?i)pdl|preferred", 2, newest_first=True)
    assert newest[0] == ("https://bms.example.gov/media/41358/download",
                         "Preferred Drug List, effective April 1, 2026")  # fmt: skip
    assert newest[1][0].endswith("/26936/download")  # dated beats undated, then the number


def test_a_page_without_matching_links_shows_what_it_does_link():
    html = """<a href="/files/apple-health-pdl.xlsx">Apple Health PDL (Excel)</a>
    <a href="/about">About</a><a href="/files/notice.pdf">Provider notice</a>"""
    got = realdata.files_on_page(html, "https://hca.example.gov/pdl")
    site = "https://hca.example.gov/files"
    assert got == [(f"{site}/apple-health-pdl.xlsx", "Apple Health PDL (Excel)"),
                   (f"{site}/notice.pdf", "Provider notice")]  # fmt: skip
