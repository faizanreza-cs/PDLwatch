import json

import pandas as pd
import pytest

from pdlwatch import history
from pdlwatch.agent.tools import execute
from pdlwatch.warehouse import get_warehouse

DOCS = pd.DataFrame(
    [("OH-1", "pdl", "2026-07-01", 90), ("OH-notice", "pdl", "2026-07-01", 3),
     ("OH-2", "pdl", "2026-10-01", 90), ("OH-pa", "pa_criteria", "2026-11-01", 9),
     ("OH-undated", "pdl", None, 99)],
    columns=["doc_id", "doc_type", "effective", "rows"],
).assign(state="OH")  # fmt: skip


def _row(doc, product, status, accepted=True):
    return {"doc_id": doc, "state": "OH", "product": product, "drug_class": "c", "status": status,
            "tier": None, "pa": status != "preferred", "st": False, "ql": False,
            "ql_detail": None, "page": 1, "accepted": accepted}  # fmt: skip


def test_one_version_per_date_and_the_latest():
    v = history.pdl_versions(DOCS)
    assert v["doc_id"].tolist() == ["OH-1", "OH-2"]  # the longer same-day file; dated PDLs only
    assert history.latest_pdls(DOCS).to_dict() == {"OH": "OH-2"}


def test_intervals_run_to_the_next_version():
    rows = pd.DataFrame([
        _row("OH-1", "Ozempic", "preferred"), _row("OH-2", "Ozempic", "non_preferred"),
        _row("OH-1", "Byetta", "preferred"),  # dropped in OH-2: no interval after it
        _row("OH-2", "Mounjaro", None, accepted=False),  # in review: kept, not confirmed
    ])  # fmt: skip
    h = history.build(rows, DOCS).set_index(["product", "valid_from"])
    assert h.loc[("Ozempic", "2026-07-01"), "valid_to"] == "2026-10-01"
    assert pd.isna(h.loc[("Ozempic", "2026-10-01"), "valid_to"])
    assert h.loc[("Byetta", "2026-07-01"), "valid_to"] == "2026-10-01"
    assert ("Byetta", "2026-10-01") not in h.index
    assert not h.loc[("Mounjaro", "2026-10-01"), "confirmed"]


@pytest.mark.parametrize(
    "bad", ["Aug 15", "2026-02-30", "2026-08-15'; DROP TABLE x", "", "15/08/2026"]
)
def test_as_of_must_be_a_real_iso_date(bad):
    with pytest.raises(ValueError):
        history.check_date(bad)


def test_history_agrees_with_access_and_changes(built):
    wh = get_warehouse()
    open_rows = wh.query("""SELECT state, product, status FROM drug_access_history
                            WHERE valid_to IS NULL AND confirmed""")  # fmt: skip
    access = wh.query("SELECT state, product, status FROM drug_access")
    key = ["state", "product", "status"]
    assert open_rows.sort_values(key).reset_index(drop=True).equals(
        access.sort_values(key).reset_index(drop=True))  # fmt: skip
    # a status change between versions, read back as of a day before and after it
    ch = wh.query("SELECT * FROM changes WHERE change = 'status' AND NOT needs_review LIMIT 1")
    c = ch.iloc[0]
    v1 = wh.query(f"SELECT effective FROM documents WHERE doc_id = '{c['from_doc']}'").iloc[0, 0]
    for day, want in ((v1, c["before"]), (c["effective"], c["after"])):
        out, err = execute("drug_access", {"drug": c["product"], "as_of": day})
        assert not err
        got = {r["state"]: r["status"] for r in json.loads(out)["rows"]}
        assert got[c["state"]] == want, day
