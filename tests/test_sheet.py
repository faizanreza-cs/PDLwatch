from openpyxl import Workbook

from pdlwatch.extract.document import extract_document
from pdlwatch.pdf import load
from pdlwatch.sheet import ROWS_PER_PAGE

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
