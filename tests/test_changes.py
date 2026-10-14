import numpy as np
import pandas as pd

from pdlwatch.changes import detect


def _rows(doc, **products):
    out = []
    for p, (status, ql, detail) in products.items():
        out.append(
            {
                "doc_id": doc,
                "product": p,
                "drug_class": "c",
                "status": status,
                "ql": ql,
                "ql_detail": detail,
                "confidence": 1.0,
            }
        )
    return out


DOCS = pd.DataFrame([
    {"doc_id": "X-1", "state": "XX", "doc_type": "pdl", "effective": "2026-07-01", "ocr_pages": 0,
     "rows": 2},
    {"doc_id": "X-2", "state": "XX", "doc_type": "pdl", "effective": "2026-10-01", "ocr_pages": 0,
     "rows": 2},
])  # fmt: skip


def test_status_additions_removals_and_limits():
    rows = pd.DataFrame(
        _rows("X-1", A=("preferred", True, np.nan), B=("preferred", False, None),
              C=("non_preferred", True, "4 per 28 days"))
        + _rows("X-2", A=("preferred", True, np.nan), B=("non_preferred", False, None),
                C=("non_preferred", True, "2 per 28 days"), D=("non_preferred", False, None))
    )  # fmt: skip
    got = {(r.product, r.change) for r in detect(rows, DOCS).itertuples()}
    # A: an unknown QL detail (NaN) on both sides is no change
    assert got == {("B", "status"), ("C", "ql"), ("D", "added")}


def test_an_unreadable_status_is_not_a_change():
    rows = pd.DataFrame(_rows("X-1", A=("preferred", False, None))
                        + _rows("X-2", A=(None, False, None)))  # fmt: skip
    assert detect(rows, DOCS).empty


def test_changes_against_a_scan_need_review():
    docs = DOCS.assign(ocr_pages=[3, 0])
    rows = pd.DataFrame(_rows("X-1", A=("preferred", False, None))
                        + _rows("X-2", A=("non_preferred", False, None)))  # fmt: skip
    assert detect(rows, docs)["needs_review"].tolist() == [True]


def test_two_files_with_one_date_are_not_compared():
    extra = {"doc_id": "X-1-notice", "state": "XX", "doc_type": "pdl", "effective": "2026-07-01",
             "ocr_pages": 0, "rows": 1}  # fmt: skip
    docs = pd.concat([DOCS, pd.DataFrame([extra])])
    rows = pd.DataFrame(_rows("X-1", A=("preferred", False, None))
                        + _rows("X-1-notice", A=("non_preferred", True, None))
                        + _rows("X-2", A=("preferred", False, None)))  # fmt: skip
    assert detect(rows, docs).empty  # the shorter same-day file is not a version
