"""What changed between two versions of a state's PDL: the question a market access team asks
every quarter ("did our brand lose preferred status anywhere?").

Rows are compared by product between consecutive PDL documents of the same state (ordered by
effective date): status changes, products added or removed, and quantity limits added, removed
or changed. A limit is compared by the amounts it names, so one typed another way, or with other
words beside it, is the same limit. A change that rests on a row still awaiting review is marked
as such.
"""

from __future__ import annotations

import re
from functools import partial
from typing import Any

import pandas as pd

from pdlwatch.extract.rows import ACCEPT, amounts
from pdlwatch.history import pdl_versions

SCANNED_CAP = 0.6  # changes against a scanned version always get a look

COLUMNS = ["state", "product", "drug_class", "change", "before", "after", "from_doc", "to_doc",
           "effective", "confidence", "needs_review"]  # fmt: skip


def _missing(v: Any) -> bool:
    return v is None or (isinstance(v, float) and pd.isna(v))


def _ql(r: pd.Series) -> str | None:
    if _missing(r["ql"]):
        return None
    if not r["ql"]:
        return "none"
    return "yes" if _missing(r["ql_detail"]) else str(r["ql_detail"])


def _said(ql: str) -> str | tuple[str, ...]:
    """What a quantity limit says, to compare two versions by: the amounts it names, whatever words
    the line picked up beside them ("QTY Limit: 1 tab/day" and "QTY LIMIT : 1 tablet per day
    treatment failure to..." say the same), or else its words without the label, case or
    spacing."""
    if ql in {"none", "yes"}:
        return ql
    found = amounts(ql)
    if found:
        return tuple(sorted(found))
    words = re.sub(r"^\s*(qty|quantity)\s*limits?\s*:?\s*", "", ql.lower())
    words = re.sub(r"\s*([:,;=/])\s*", r"\1", words)
    return " ".join(words.split()).strip(" .;,")


def _record(out: list[dict[str, Any]], pair: dict[str, Any], cap: float, product: str,
            change: str, before: Any, after: Any, conf: float, cls: str) -> None:  # fmt: skip
    conf = min(conf, cap)
    out.append({**pair, "product": product, "drug_class": cls, "change": change,
                "before": before, "after": after, "confidence": round(conf, 2),
                "needs_review": conf < ACCEPT})  # fmt: skip


def detect(rows: pd.DataFrame, docs: pd.DataFrame) -> pd.DataFrame:
    out: list[dict[str, Any]] = []
    pdl = pdl_versions(docs)  # the same versions the access table and history use
    scanned = set(docs.loc[docs["ocr_pages"] > 0, "doc_id"])
    for state, g in pdl.groupby("state"):
        ids = g["doc_id"].tolist()
        for a, b in zip(ids, ids[1:], strict=False):
            ra = rows[(rows["doc_id"] == a) & rows["product"].notna()].set_index("product")
            rb = rows[(rows["doc_id"] == b) & rows["product"].notna()].set_index("product")
            eff = g.loc[g["doc_id"] == b, "effective"].iloc[0]
            # a drug OCR missed in one version looks exactly like an addition or a removal
            cap = SCANNED_CAP if {a, b} & scanned else 1.0
            pair = {"state": state, "from_doc": a, "to_doc": b, "effective": eff}
            add = partial(_record, out, pair, cap)
            for p in sorted(set(ra.index) | set(rb.index)):
                if p not in rb.index:
                    x = ra.loc[p]
                    add(p, "removed", x["status"], None, x["confidence"], x["drug_class"])
                elif p not in ra.index:
                    y = rb.loc[p]
                    add(p, "added", None, y["status"], y["confidence"], y["drug_class"])
                else:
                    x, y = ra.loc[p], rb.loc[p]
                    conf = min(x["confidence"], y["confidence"])
                    readable = not _missing(x["status"]) and not _missing(y["status"])
                    if readable and x["status"] != y["status"]:
                        add(p, "status", x["status"], y["status"], conf, y["drug_class"])
                    qa, qb = _ql(x), _ql(y)
                    if qa is not None and qb is not None and _said(qa) != _said(qb):
                        add(p, "ql", None if qa == "none" else qa, None if qb == "none" else qb,
                            conf, y["drug_class"])  # fmt: skip
    return pd.DataFrame(out, columns=COLUMNS)


def evaluate(found: pd.DataFrame, truth: dict[str, Any]) -> dict[str, Any]:
    """Precision and recall of detected changes against the corpus's true changes.

    A quantity-limit change only counts when both versions of the document can show quantity
    limits; status changes, additions and removals always count.
    """
    docs = {d["doc_id"]: d for d in truth["documents"]}
    expresses_ql = {
        d["state"]: all(r["ql"] is not None for r in d["rows"] if r["status"] != "not_covered")
        for d in docs.values()
        if d["doc_type"] == "pdl"
    }
    true = {(c["state"], c["product"], c["change"]) for c in truth["changes"]
            if c["change"] != "ql" or expresses_ql.get(c["state"], False)}  # fmt: skip
    got = {(r.state, r.product, r.change) for r in found.itertuples()}
    hit = true & got
    sure = {(r.state, r.product, r.change) for r in found.itertuples() if not r.needs_review}
    by_type = {}
    for kind in ("status", "added", "removed", "ql"):
        t = {x for x in true if x[2] == kind}
        g = {x for x in got if x[2] == kind}
        by_type[kind] = {"true": len(t), "found": len(g), "correct": len(t & g)}
    return {
        "true_changes": len(true),
        "detected": len(got),
        "precision": round(len(hit) / len(got), 4) if got else None,
        "recall": round(len(hit) / len(true), 4) if true else None,
        "confirmed": len(sure),
        "confirmed_precision": round(len(sure & true) / len(sure), 4) if sure else None,
        "confirmed_recall": round(len(sure & true) / len(true), 4) if true else None,
        "by_type": by_type,
    }
