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


def test_a_limit_is_compared_by_what_it_says():
    """Vermont typed "QTY LIMIT" one year and "QTY Limit" the next, and its notes pick up words
    from the criteria beside them: neither is a change. Another amount is."""
    before = {
        "A": "QTY LIMIT : 1 patch/day",
        "B": "QTY LIMIT: 2 tablets/day three preferred drugs AND dosing does not exc",
        "C": "60ml per 30 days",
        "D": "1 tab/day",
        "E": "1 inhaler/30 days",
        "F": "Maximum 1-month supply per fill",
        "G": "Maximum 1-month supply per fill",
    }
    after = {
        "A": "QTY Limit: 1 patch/day",
        "B": "QTY Limit: 2 tablets/day Maximum 30-day supply per fill",
        "C": "60ml/30 days",
        "D": "1 tablet per day",
        "E": "3 inhalers/30 days",
        "F": "maximum 1-month  supply per fill",
        "G": "Maximum 2-month supply per fill",
    }
    rows = pd.DataFrame(
        _rows("X-1", **{p: ("preferred", True, d) for p, d in before.items()})
        + _rows("X-2", **{p: ("preferred", True, d) for p, d in after.items()})
    )
    got = detect(rows, DOCS)
    assert got["product"].tolist() == ["E", "G"]
    assert got.iloc[0][["before", "after"]].tolist() == ["1 inhaler/30 days", "3 inhalers/30 days"]


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
