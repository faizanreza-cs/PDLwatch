import pytest
from conftest import HAS_OCR

from pdlwatch.warehouse import TABLES, get_warehouse


def test_results_report_every_group(built):
    res = built["results"]
    assert res["extractor"] == "rules" and res["data"]["source"] == "synthetic"
    groups = res["extraction"]["all"]
    assert {"overall", "digital", "family:grid", "family:columns", "family:tiered",
            "family:narrative"} <= set(groups)  # fmt: skip
    assert ("scanned" in groups) == HAS_OCR
    assert res["data"]["documents"] == len(built["truth"]["documents"])


def test_each_layout_is_read_correctly(built):
    g = built["results"]["extraction"]["all"]
    for family in ("grid", "columns", "tiered"):  # structured and semi-structured
        m = g[f"family:{family}"]
        assert m["row_recall"] >= 0.98 and m["row_precision"] >= 0.98, family
        assert m["rows_fully_correct"] >= 0.9, family
    prose = g["family:narrative"]  # unstructured: found, but every row waits for review
    assert prose["row_recall"] >= 0.8 and prose["accepted_share"] == 0
    assert g["overall"]["accepted_error_rate"] <= 0.02


def test_changes_between_versions_are_found(built):
    ch = built["results"]["changes"]
    assert ch["true_changes"] > 0
    assert ch["recall"] >= 0.85 and ch["confirmed_precision"] >= 0.95


def test_warehouse_tables(built):
    wh = get_warehouse()
    for t in TABLES:
        assert len(wh.query(f"SELECT * FROM {t} LIMIT 1")) == 1, t
    access = wh.query("SELECT * FROM drug_access")
    assert access["product"].notna().all()
    latest = wh.query("""SELECT state, MAX(effective) AS effective FROM documents
                         WHERE doc_type = 'pdl' GROUP BY state""")  # fmt: skip
    docs = wh.query("SELECT doc_id, state, effective FROM documents")
    used = docs[docs["doc_id"].isin(access["doc_id"].unique())]
    merged = used.merge(latest, on="state", suffixes=("", "_latest"))
    assert (merged["effective"] == merged["effective_latest"]).all()  # only the latest PDL
    review = wh.query("SELECT * FROM review_queue")
    assert (review["review_reasons"].str.len() > 0).all()  # nothing waits without a reason
    np_rows = wh.query("SELECT pa FROM drug_access WHERE status = 'non_preferred'")
    assert np_rows["pa"].astype(bool).all()  # non-preferred means prior authorization


def test_document_types(built):
    got = dict(get_warehouse().query("SELECT doc_id, doc_type FROM documents").values.tolist())
    assert got == {d["doc_id"]: d["doc_type"] for d in built["truth"]["documents"]}


def test_rows_cite_their_source(built):
    rows = get_warehouse().query("SELECT doc_id, page, evidence FROM formulary_rows")
    assert rows["page"].ge(1).all() and rows["evidence"].str.len().gt(0).all()
    pages = get_warehouse().query("SELECT doc_id, pages FROM documents")
    m = rows.merge(pages, on="doc_id")
    assert (m["page"] <= m["pages"]).all()


def test_review_reasons_are_counted(built):
    reasons = built["results"]["review_reasons"]
    assert "read from prose by keyword rules" in reasons
    assert all(isinstance(n, int) and n > 0 for n in reasons.values())


def test_scanned_pdl_is_ocrd(built):
    if not HAS_OCR:
        return
    docs = get_warehouse().query("SELECT doc_id, ocr_pages, rows FROM documents")
    scanned = [d["doc_id"] for d in built["truth"]["documents"] if d["scanned"]]
    got = docs[docs["doc_id"].isin(scanned)]
    assert len(got) == 1 and int(got["ocr_pages"].iloc[0]) > 0
    m = built["results"]["extraction"]["all"]["scanned"]
    assert m["row_recall"] >= 0.9 and m["accepted_error_rate"] <= 0.03


def test_an_empty_folder_is_a_clear_error(tmp_path):
    from pdlwatch.pipeline import run_folder

    with pytest.raises(FileNotFoundError, match="no PDFs"):
        run_folder(tmp_path)


def test_an_unreadable_pdf_is_reported_and_the_rest_extracted(built, tmp_path):
    from pdlwatch.config import Settings
    from pdlwatch.pipeline import run_folder

    good = next(d for d in built["truth"]["documents"] if d["family"] == "grid")
    (tmp_path / "pdfs").mkdir()
    (tmp_path / "pdfs" / "good.pdf").write_bytes(
        (built["root"] / "corpus" / good["file"]).read_bytes()
    )
    (tmp_path / "pdfs" / "broken.pdf").write_bytes(b"%PDF-1.7 truncated")
    s = Settings(db_path=tmp_path / "real.duckdb")
    res = run_folder(tmp_path / "pdfs", s, workers=1)
    assert [f["doc_id"] for f in res["failed_documents"]] == ["broken"]
    assert res["data"]["rows"] >= len(good["rows"])
