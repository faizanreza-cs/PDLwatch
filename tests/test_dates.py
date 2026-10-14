from pdlwatch.extract.document import _effective, date_hint
from pdlwatch.pdf import Line, Page, Phrase, Word


def _page(*texts: str) -> Page:
    lines = [Line([Phrase([Word(w, 50 + 30 * k, 75 + 30 * k, 100 + 12 * i, 109 + 12 * i)
                           for k, w in enumerate(t.split())])]) for i, t in enumerate(texts)]  # fmt: skip
    return Page(1, 612, 792, lines, False)


def test_effective_dates_in_the_document():
    assert _effective([_page("Preferred Drug List", "Effective for dates of service on or after "
                             "07/01/2026")]) == ("2026-07-01", "document")  # fmt: skip
    assert _effective([_page("PDL", "Effective: Jan. 1, 2026")]) == ("2026-01-01", "document")
    assert _effective([_page("Vermont PDL", "October 2, 2026", "Preferred Agents")]) == (
        "2026-10-02", "title")  # fmt: skip
    assert _effective([_page("Preferred Drug List", "Drug Class Preferred")]) == (None, None)


def test_dates_from_titles_and_file_names():
    cases = {
        "20260701_UPDL_New_Version_APPROVED-FINAL": "2026-07-01",
        "kentucky-medicaid-pdl_05312024": "2024-05-31",
        "pdl10242025": "2025-10-24",
        "VERMONT PDL 4_30_24_V3_ FINAL": "2024-04-30",
        "PDL_2025_01_01_v1": "2025-01-01",
        "PDL Effective 07.01.2026": "2026-07-01",
        "MS-Medicaid-PDL_October-2026_v1_FINAL": "2026-10-01",
        "Health First Colorado PDL, effective April 1, 2026": "2026-04-01",
        "ga-med-m-2964344-georgia-medicaid-preferred-drug-list": None,
        "NYRx_PDP_PDL": None,
        "iowa-webpdl-10126": None,
    }
    assert {k: date_hint(k) for k in cases} == cases
