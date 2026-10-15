import json

from openpyxl import Workbook

from pdlwatch.extract.document import extract_document
from pdlwatch.pdf import load
from pdlwatch.sheet import ROWS_PER_PAGE, check_profile, columns

HEADER = [
    "APPLE HEALTH DRUG CLASS",
    "GENERIC NAME",
    "DRUG NAME",
    "DOSE FORM",
    "PREFERRED STATUS",
    "NUMBER OF PREFERRED AGENTS TO TRY",
    "PA STATUS",
    "EFFECTIVE DATE",
]
ROWS = [
    ["GLP-1 RECEPTOR AGONISTS", "SEMAGLUTIDE", "OZEMPIC", "PEN", "NON-PREFERRED", 2,
     "PA REQUIRED", "2026-01-01"],
    [None, "DULAGLUTIDE", "TRULICITY", "PEN", "PREFERRED", None, None, "2025-07-01"],
    [None, "LIRAGLUTIDE", "VICTOZA", "PEN", "PREFERRED", None, "PA REQUIRED", "2025-07-01"],
]  # fmt: skip


def _workbook(path, rows=ROWS):
    wb = Workbook()
    ws = wb.active
    ws.append(["Apple Health (Medicaid) Preferred Drug List"])
    ws.append(["FERT = Fertility product, OTCS = Over the counter product"])
    ws.append([])
    ws.append(HEADER)
    for r in rows:
        ws.append(r)
    wb.save(path)
    return path


def test_a_pdl_published_as_a_workbook_reads_like_a_table(tmp_path):
    doc = extract_document(_workbook(tmp_path / "WA-apple-health-pdl-20261001.xlsx"), state="WA")
    got = {r.product: (r.status, r.pa, r.drug_class) for r in doc.rows}
    assert got["Ozempic"] == ("non_preferred", True, "GLP-1 Receptor Agonists")
    assert got["Trulicity"][:2] == ("preferred", False)
    assert got["Victoza"][:2] == ("preferred", True)  # preferred, with PA in its own column
    assert all(r.accepted for r in doc.rows)
    # a date column inside the table is not the list's date (that comes from the file name)
    assert doc.effective is None and doc.doc_type == "pdl"
    assert len({r.row_key for r in doc.rows}) == 3


def test_a_row_keeps_its_key_when_other_rows_change(tmp_path):
    a = extract_document(_workbook(tmp_path / "a.xlsx"), state="WA")
    changed = [ROWS[0], ["GLP-1 RECEPTOR AGONISTS", "EXENATIDE", "BYETTA", "PEN", "PREFERRED",
                         None, None, "2026-10-01"], *ROWS[1:]]  # fmt: skip
    b = extract_document(_workbook(tmp_path / "b.xlsx", changed), state="WA")
    key = {r.product: r.row_key for r in a.rows}
    assert {r.product: r.row_key for r in b.rows if r.product in key} == key


def test_long_sheets_become_pages_and_csv_works_too(tmp_path):
    rows = [["GLP-1 RECEPTOR AGONISTS", "SEMAGLUTIDE", f"OZEMPIC {i}", "PEN", "PREFERRED", None,
             None, None] for i in range(130)]  # fmt: skip
    pages = load(_workbook(tmp_path / "long.xlsx", rows))
    assert len(pages) == -(-(len(rows) + 4) // ROWS_PER_PAGE)  # 4 title and header rows
    csv_path = tmp_path / "pdl.csv"
    csv_path.write_text("Drug Name,Status,PA\nOzempic,Non-Preferred,Y\nTrulicity,Preferred,N\n")
    doc = extract_document(csv_path, state="OR")
    assert {r.product: r.status for r in doc.rows} == {"Ozempic": "non_preferred",
                                                       "Trulicity": "preferred"}  # fmt: skip


def test_a_weekly_workbook_becomes_the_states_latest_list(tmp_path):
    from pdlwatch.config import Settings
    from pdlwatch.pipeline import run_folder
    from pdlwatch.warehouse import DuckDBWarehouse

    folder = tmp_path / "pdfs"
    folder.mkdir()
    stem = "WA-apple-health-pdl-20261001-0a1b2c3d"
    _workbook(folder / f"{stem}.xlsx")
    s = Settings(db_path=tmp_path / "real.duckdb")
    hints = {stem: {"title": "Apple Health PDL 10/1/2026 – 10/9/2026 Washington weekly"}}
    res = run_folder(folder, s, {stem: "WA"}, workers=1, hints=hints)
    assert res["data"]["rows"] == 3
    got = DuckDBWarehouse(s.db_path).query(
        "SELECT product, status, effective FROM drug_access WHERE state = 'WA' ORDER BY product"
    )
    assert got.values.tolist() == [
        ["Ozempic", "non_preferred", "2026-10-01"],  # dated by its link text
        ["Trulicity", "preferred", "2026-10-01"],
        ["Victoza", "preferred", "2026-10-01"],
    ]


# Washington's real layout (run 10): one row per package (NDC), 23 coded columns
WA_HEADER = ["PRODUCT IDENTIFIER", "IDENTIFIER TYPE", "APPLE HEALTH DRUG CLASS", "LABEL NAME",
             "GENERIC NAME", "STRENGTH", "DOSE FORM", "BRAND OR GENERIC IDENTIFIER",
             "Rx IDENTIFIER", "FEDERAL REBATE STATUS", "AHPDL INCLUSION", "MCO CARVE OUT",
             "COVERAGE TYPE", "NON-COVERED REASON", "MEDICAL PREFERRED STATUS",
             "MEDICAL PA STATUS", "PHARMACY PREFERRED STATUS", "NUMBER OF PREFERRED",
             "CONTINUATION PERIOD", "PHARMACY PA STATUS", "SUBJECT TO SON LIMITS",
             "NON CLINICAL TYPE", "PA POLICY NUMBER"]  # fmt: skip
WA_PROFILE = {
    "columns": {"APPLE HEALTH DRUG CLASS": "Drug Class", "LABEL NAME": "Drug Name",
                "PHARMACY PREFERRED STATUS": "Preferred Status", "PHARMACY PA STATUS": "PA Status"},
    "rows": {"AHPDL INCLUSION": "Y"},
}  # fmt: skip
GLP1 = "ANTIDIABETICS : GLP-1 RECEPTOR AGONISTS"


def _package(ndc, cls, label, strength, on_pdl, status, pa, medical_pa=None):
    return [ndc, "NDC", cls, label, label, strength, "PEN INJECTOR", "B", "R", "Y", on_pdl, "N",
            "CP", None, "X", medical_pa, status, None, None, pa, "N", "NOTYPE", None]  # fmt: skip


def _wa_workbook(path):
    wb = Workbook()
    ws = wb.active
    for title in ("Apple Health (Medicaid) Preferred Drug List",
                  "Click here for column description and explanation",
                  "Effective October 1, 2026 - October 9, 2026"):  # fmt: skip
        ws.append([title])
    ws.append(WA_HEADER)
    for row in [
        _package("00169413212", GLP1, "OZEMPIC", "0.25-0.5 MG", "Y", "P", "N"),
        _package("00169413013", GLP1, "OZEMPIC", "1 MG/DOSE", "Y", "P", "N"),
        _package("00002143380", GLP1, "TRULICITY", "0.75 MG", "Y", "N", "Y"),
        _package("00169406013", GLP1, "VICTOZA", "18 MG/3 ML", "Y", "P", "N", medical_pa="Y"),
        _package("81968035101", "ADHD / ANTI-NARCOLEPSY : DNRIS", "SUNOSI", "150 MG", "Y",
                 "N", "Y"),
        _package("00093310905", "ANTIBIOTICS : PENICILLINS", "AMOXICILLIN", "500 MG", "N",
                 "X", "N"),
    ]:  # fmt: skip
        ws.append(row)
    wb.save(path)
    return path


def test_a_package_level_workbook_reads_through_its_source_profile(tmp_path):
    path = _wa_workbook(tmp_path / "WA-apple-health-pdl-20261001.xlsx")
    assert extract_document(path, state="WA").rows == []  # as filed, it has no header we know
    doc = extract_document(path, state="WA", profile=WA_PROFILE)
    # the pharmacy benefit's status and PA, not the medical benefit's (Victoza's medical PA);
    # the drug off the PDL is left out, and a product's packages make one row
    assert {r.drug_text: (r.status, r.pa) for r in doc.rows} == {
        "OZEMPIC": ("preferred", False),
        "TRULICITY": ("non_preferred", True),
        "VICTOZA": ("preferred", False),
        "SUNOSI": ("non_preferred", True),
    }
    assert all(r.accepted for r in doc.rows if r.product in {"Ozempic", "Trulicity", "Victoza"})
    assert doc.effective == "2026-10-01" and doc.profile == WA_PROFILE


def test_profiles_are_checked_and_columns_listed_to_write_one(tmp_path):
    assert check_profile(WA_PROFILE) == []
    assert check_profile(
        {"columns": {"LABEL": "Medicine"}, "rows": {"ON PDL": True}, "pages": 2}
    ) == [  # fmt: skip
        "unknown profile key 'pages'",
        "'Medicine' is not a header the parser reads",
        "profile rows must map names to text (quote Y and N)",
    ]
    out = columns(_wa_workbook(tmp_path / "wa.xlsx"))
    assert out[0] == "sheet Sheet: 6 rows under the header on row 4"
    assert "  AHPDL INCLUSION: 2 values; Y 5, N 1" in out
    assert "  PHARMACY PREFERRED STATUS: 3 values; P 3, N 2, X 1" in out


def test_the_audit_finds_a_profiled_row_beside_its_header(tmp_path):
    from pdlwatch import audit

    path = _wa_workbook(tmp_path / "wa.xlsx")
    doc = extract_document(path, state="WA", profile=WA_PROFILE)
    row = next(r for r in doc.rows if r.drug_text == "TRULICITY")
    ctx = audit.context(str(path), row.page, row.drug_text, row.y, json.dumps(WA_PROFILE))
    assert (
        ctx["header"] == "Drug Class @20 | Drug Name @140 | Preferred Status @260 | PA Status @380"
    )
    assert ctx["line"] == f"{GLP1} @20 | TRULICITY @140 | N @260 | Y @380"


def test_a_workbook_read_through_another_profile_is_extracted_again(tmp_path):
    from pdlwatch.config import Settings
    from pdlwatch.pipeline import run_folder
    from pdlwatch.warehouse import DuckDBWarehouse

    folder = tmp_path / "pdfs"
    folder.mkdir()
    stem = "WA-apple-health-pdl-20261001-0a1b2c3d"
    _wa_workbook(folder / f"{stem}.xlsx")
    s = Settings(db_path=tmp_path / "real.duckdb")

    def run(profile):
        return run_folder(folder, s, {stem: "WA"}, workers=1, profiles={stem: profile})

    first, again = run(WA_PROFILE), run(WA_PROFILE)
    assert first["data"]["rows"] == 4 and first["cache"] == {"from_cache": 0, "extracted": 1}
    assert again["cache"] == {"from_cache": 1, "extracted": 0}
    wider = {**WA_PROFILE, "rows": {}}  # the drugs off the PDL too
    third = run(wider)
    assert third["cache"] == {"from_cache": 0, "extracted": 1}
    assert third["data"]["rows"] > first["data"]["rows"]
    docs = DuckDBWarehouse(s.db_path).query("SELECT profile FROM documents")
    assert json.loads(docs["profile"][0]) == wider  # each document says how it was read
