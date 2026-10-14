"""Score extracted rows against the synthetic corpus's truth.

* **Row recall / precision**: a truth row is found when a row for the same product comes out of
  the same document; extracted rows with no truth row (or no product) are false positives.
* **Field accuracy**: on found rows, every field the document states (status, PA, step therapy,
  quantity limit, tier, brand over generic). Fields a document doesn't express aren't scored.
* **Straight-through processing**: the share of rows accepted without review, and how many of
  the accepted rows are completely right (the error rate a team would live with).
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from pdlwatch.extract.document import DocResult
from pdlwatch.extract.rows import Row

FIELDS = ("status", "pa", "st", "ql", "tier", "bvg")


def _correct(row: Row, truth: dict[str, Any]) -> bool:
    return all(truth[f] is None or getattr(row, f) == truth[f] for f in FIELDS)


def doc_counts(result: DocResult, truth_doc: dict[str, Any]) -> dict[str, Any]:
    truth = {t["product"]: t for t in truth_doc["rows"]}
    got = {r.product: r for r in result.rows if r.product}
    extra = len(result.rows) - len(got) + len(got.keys() - truth.keys())
    c: dict[str, Any] = defaultdict(int)
    c["truth"] = len(truth)
    c["extracted"] = len(result.rows)
    c["found"] = len(truth.keys() & got.keys())
    c["false_positive"] = extra
    for p in truth.keys() & got.keys():
        t, r = truth[p], got[p]
        for f in FIELDS:
            if t[f] is None:
                continue
            c[f"{f}_n"] += 1
            c[f"{f}_ok"] += getattr(r, f) == t[f]
        c["all_ok"] += _correct(r, t)
    for r in result.rows:
        if not r.accepted:
            continue
        c["accepted"] += 1
        t = truth.get(r.product or "")
        c["accepted_ok"] += bool(t and _correct(r, t))
    return dict(c)


def _ratios(counts: dict[str, float]) -> dict[str, Any]:
    c: dict[str, float] = defaultdict(float, counts)

    def div(a: float, b: float) -> float | None:
        return round(a / b, 4) if b else None

    out: dict[str, Any] = {
        "documents": int(c.get("documents", 0)),
        "truth_rows": int(c["truth"]),
        "row_recall": div(c["found"], c["truth"]),
        "row_precision": div(c["found"], c["found"] + c["false_positive"]),
        "rows_fully_correct": div(c["all_ok"], c["truth"]),
    }
    for f in FIELDS:
        out[f"{f}_accuracy"] = div(c.get(f"{f}_ok", 0), c.get(f"{f}_n", 0))
    out["accepted_share"] = div(c["accepted"], c["extracted"])
    out["accepted_error_rate"] = (
        round(1 - c["accepted_ok"] / c["accepted"], 4) if c["accepted"] else None
    )
    out["straight_through_recall"] = div(c["accepted_ok"], c["truth"])
    for k in ("extracted", "accepted", "accepted_ok", "found", "all_ok"):
        out[k] = int(c[k])
    return out


GROUPS = {
    "grid": "structured (ruled tables)",
    "columns": "semi-structured (preferred / non-preferred lists)",
    "tiered": "semi-structured (tiered formularies)",
    "narrative": "unstructured (prior authorization prose)",
}


def evaluate(results: list[DocResult], truth: dict[str, Any],
             split: str | None = None) -> dict[str, Any]:  # fmt: skip
    """Metrics overall, by layout (digital documents), for scans, and by dev / test split."""
    docs = {d["doc_id"]: d for d in truth["documents"]}
    buckets: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    per_doc = []
    for res in results:
        td = docs.get(res.doc_id)
        if td is None or (split and td["split"] != split):
            continue
        c = doc_counts(res, td)
        per_doc.append({"doc_id": res.doc_id, "family": td["family"], "scanned": td["scanned"],
                        "split": td["split"], **_ratios(c)})  # fmt: skip
        # layouts are scored on digital documents; scans are their own group, whatever the layout
        keys = ["overall", "scanned" if td["scanned"] else f"family:{td['family']}",
                f"split:{td['split']}"]  # fmt: skip
        if not td["scanned"]:
            keys.append("digital")
        for k in keys:
            buckets[k]["documents"] += 1
            for name, v in c.items():
                buckets[k][name] += v
    return {
        "groups": {k: _ratios(v) for k, v in sorted(buckets.items())},
        "documents": per_doc,
    }
