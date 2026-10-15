"""Reviewer decisions that carry over between runs.

Every row is keyed by what it was read from (``row_key``): the text of its page, the position of
its line and the name as printed. Extracting the same file again, or a re-saved copy
with the same pages, gives the same keys; a page that changed gives new ones, so its rows come
back for review instead of inheriting a decision made about different text.

``pdlwatch review --out review.csv`` writes the queue with empty decision columns:

* ``decision``: ``accept`` (the row is right as read), ``correct`` (fill in the corrected fields)
  or ``reject`` (not a drug row: a note, a header, a line of criteria);
* ``corrected_status`` (preferred, non_preferred, not_covered), ``corrected_pa``,
  ``corrected_st``, ``corrected_ql`` (yes / no) and ``corrected_product``: for ``correct`` only;
* ``note``: free text, kept with the decision.

``pdlwatch review --apply review.csv --reviewer NAME`` checks every filled-in line and appends the
valid ones to ``review_decisions.jsonl`` next to the warehouse, with the reviewer and the time.
Nothing is overwritten: the latest decision for a key wins. Every build or sync applies them, and
``results.json`` reports, per review reason, how often a reviewer had to correct or reject a row:
the measured error rate on real documents. Decisions can cover accepted rows too (an audit), which
measures the error rate of automatic acceptance.
"""

from __future__ import annotations

import csv
import hashlib
import json
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pandas as pd

DECISIONS = ("accept", "correct", "reject")
STATUSES = ("preferred", "non_preferred", "not_covered")
CSV_COLUMNS = ("decision", "corrected_status", "corrected_pa", "corrected_st", "corrected_ql",
               "corrected_product", "note")  # fmt: skip
FLAGS = ("pa", "st", "ql")
AUTO = "(accepted automatically)"  # the reason bucket for decisions on accepted rows
_YES, _NO = {"yes", "y", "true", "1"}, {"no", "n", "false", "0"}


def row_key(page_text: str, y: float | None, drug_text: str) -> str:
    """A row's identity: what it was read from (the page's text, the line's position, the name
    as printed), not which file or address it came from."""
    page = hashlib.sha1(page_text.encode()).hexdigest()[:12]
    name = " ".join(str(drug_text).lower().split())
    return hashlib.sha1(f"{page}|{round(y or 0.0)}|{name}".encode()).hexdigest()[:16]


def decisions_path(db_path: str | Path) -> Path:
    return Path(db_path).with_name("review_decisions.jsonl")


def to_csv(queue: pd.DataFrame, out: Path) -> int:
    """The review queue with empty decision columns, for a reviewer to fill in."""
    out.parent.mkdir(parents=True, exist_ok=True)
    df = queue.copy()
    for c in CSV_COLUMNS:
        df[c] = ""
    df.to_csv(out, index=False)
    return len(df)


def _flag(value: str) -> bool | None:
    v = value.strip().lower()
    if v in _YES:
        return True
    if v in _NO:
        return False
    raise ValueError(f"{value!r} is not yes or no")


def read_csv(path: Path) -> tuple[list[dict[str, Any]], list[str]]:
    """The decisions filled in on a review CSV, and what's wrong with the lines that can't be
    used (by line number). Lines without a decision are skipped."""
    out: list[dict[str, Any]] = []
    errors: list[str] = []
    with path.open(newline="") as f:
        reader = csv.DictReader(f)
        missing = {"row_key", "decision"} - set(reader.fieldnames or [])
        if missing:
            return [], [f"{path}: missing column(s) {', '.join(sorted(missing))}"]
        for n, line in enumerate(reader, start=2):
            decision = (line.get("decision") or "").strip().lower()
            if not decision:
                continue
            key = (line.get("row_key") or "").strip()
            if not key:
                errors.append(f"line {n}: no row_key")
                continue
            if decision not in DECISIONS:
                errors.append(f"line {n}: decision must be accept, correct or reject")
                continue
            d: dict[str, Any] = {"row_key": key, "decision": decision,
                                 "note": (line.get("note") or "").strip()}  # fmt: skip
            try:
                if decision == "correct":
                    d.update(_corrections(line))
            except ValueError as e:
                errors.append(f"line {n}: {e}")
                continue
            out.append(d)
    return out, errors


def _corrections(line: dict[str, str]) -> dict[str, Any]:
    fix: dict[str, Any] = {}
    status = (line.get("corrected_status") or "").strip().lower()
    if status:
        status = status.replace("-", "_").replace(" ", "_")
        if status not in STATUSES:
            raise ValueError("corrected_status must be preferred, non_preferred or not_covered")
        fix["status"] = status
    for f in FLAGS:
        value = (line.get(f"corrected_{f}") or "").strip()
        if value:
            try:
                fix[f] = _flag(value)
            except ValueError as e:
                raise ValueError(f"corrected_{f}: {e}") from None
    product = (line.get("corrected_product") or "").strip()
    if product:
        fix["product"] = product
    if not fix:
        raise ValueError("correct needs at least one corrected_ value")
    return fix


def record(path: Path, decisions: list[dict[str, Any]], reviewer: str) -> int:
    """Append decisions to the log (never rewritten), each with its reviewer and time."""
    if not reviewer.strip():
        raise ValueError("name the reviewer")
    path.parent.mkdir(parents=True, exist_ok=True)
    now = datetime.now(UTC).isoformat(timespec="seconds")
    with path.open("a") as f:
        for d in decisions:
            f.write(json.dumps({**d, "reviewer": reviewer.strip(), "decided_at": now}) + "\n")
    return len(decisions)


def load(path: Path) -> dict[str, dict[str, Any]]:
    """The latest decision for each row key."""
    if not path.exists():
        return {}
    latest: dict[str, dict[str, Any]] = {}
    for line in path.read_text().splitlines():
        if line.strip():
            d = json.loads(line)
            latest[d["row_key"]] = d
    return latest


def apply(rows: pd.DataFrame, decisions: dict[str, dict[str, Any]]
          ) -> tuple[pd.DataFrame, dict[str, Any]]:  # fmt: skip
    """Rows with their reviewers' decisions applied, and what the decisions measured.

    ``accept`` and ``correct`` accept the row (with the corrections) at confidence 1; ``reject``
    takes it out of the review queue and out of every status table. ``reviewed`` says which.
    """
    rows = rows.copy()
    rows["reviewed"] = None
    if not decisions or rows.empty:
        return rows, {}
    from pdlwatch.drugs import match_product

    hit = rows["row_key"].isin(decisions)
    wrong: Counter[str] = Counter()
    seen: Counter[str] = Counter()
    for i in rows.index[hit]:
        d = decisions[rows.at[i, "row_key"]]
        reasons = [x for x in str(rows.at[i, "review_reasons"] or "").split("; ") if x]
        for reason in reasons if not rows.at[i, "accepted"] else [AUTO]:
            seen[reason] += 1
            wrong[reason] += d["decision"] != "accept"
        rows.at[i, "reviewed"] = d["decision"]
        if d["decision"] == "reject":
            rows.at[i, "accepted"] = False
            continue
        if d["decision"] == "correct":
            for f in ("status", *FLAGS):
                if f in d:
                    rows.at[i, f] = d[f]
            if "product" in d:
                m = match_product(d["product"])
                p = m.product if m.score == 100 else None
                rows.at[i, "product"] = p.name if p else d["product"]
                for f in ("ingredient", "kind", "rxcui"):
                    rows.at[i, f] = getattr(p, f, None) if p else None
        rows.at[i, "accepted"] = True
        rows.at[i, "confidence"] = 1.0
    applied = int(hit.sum())
    stats = {
        "decisions": len(decisions),
        "applied": applied,
        "orphaned": len(decisions) - int(rows.loc[hit, "row_key"].nunique()),
        "by_reason": {
            r: {"reviewed": n, "wrong": wrong[r], "error_rate": round(wrong[r] / n, 3)}
            for r, n in seen.most_common()
        },
    }
    return rows, stats
