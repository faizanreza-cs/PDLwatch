from collections import Counter

import pdfplumber

from pdlwatch.drugs import BY_NAME
from pdlwatch.synth.render import BANNER
from pdlwatch.synth.states import STATES
from pdlwatch.synth.truth import plan


def test_corpus_plan():
    docs, changes = plan()
    assert len(docs) >= 100 and {d.state for d in docs} == set(STATES)
    families = Counter(d.family for d in docs)
    assert set(families) == {"grid", "columns", "tiered", "narrative"}
    assert 0.1 <= sum(d.scanned for d in docs) / len(docs) <= 0.2
    # dev and test are split by state: no state in both
    split = {}
    for d in docs:
        assert split.setdefault(d.state, d.split) == d.split
    assert set(split.values()) == {"dev", "test"}
    assert {c.change for c in changes} == {"status", "ql", "added", "removed"}


def test_plan_is_deterministic():
    a, b = plan(7), plan(7)
    assert [d.to_dict() for d in a[0]] == [d.to_dict() for d in b[0]]
    assert plan(8)[0][0].to_dict() != a[0][0].to_dict()


def test_truth_follows_medicaid_rules():
    docs, _ = plan()
    for d in docs:
        for r in d.rows:
            assert r.product in BY_NAME
            if r.status == "non_preferred" and r.pa is not None:
                assert r.pa  # non-preferred means prior authorization
            if r.tier is not None:
                assert r.tier in (1, 2, 3)


def test_versions_carry_their_changes():
    docs, changes = plan()
    by_id = {d.doc_id: d for d in docs}
    for c in changes:
        v1 = {r.product: r for r in by_id[f"{c.state}-PDL-2026-07"].rows}
        v2 = {r.product: r for r in by_id[f"{c.state}-PDL-2026-10"].rows}
        if c.change == "added":
            assert c.product in v2 and c.product not in v1
        elif c.change == "removed":
            assert c.product in v1 and c.product not in v2
        elif c.change == "status":
            assert (v1[c.product].status, v2[c.product].status) == (c.before, c.after)


def test_every_page_says_synthetic(built):
    for d in built["truth"]["documents"]:
        if d["scanned"]:
            continue  # an image: checked by OCR elsewhere
        with pdfplumber.open(built["root"] / "corpus" / d["file"]) as pdf:
            for page in pdf.pages:
                assert BANNER.split(":")[0] in (page.extract_text() or ""), d["doc_id"]
