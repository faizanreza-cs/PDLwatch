"""Alerts: the changes a customer watches, rated, written up and sent once.

A watchlist names brands, ingredients, classes and, optionally, states::

    name: GLP-1 launch team
    products: [Ozempic, Mounjaro]
    ingredients: [semaglutide]
    classes: [GLP-1 Receptor Agonists]
    states: [OH, TX]   # leave out for every state

``pdlwatch alerts --watchlist FILE`` matches the ``changes`` table against it and rates each
change:

* **high**: lost preferred status (to non-preferred or not covered), or removed from the list;
* **medium**: a quantity limit added or changed, or another status change;
* **info**: gained preferred status, or added to the list.

Only confirmed changes alert. A change that rests on a row still in review is listed under
"waiting for review", so nobody acts on a misread. Every alert names the two documents it
compares. A ledger next to the warehouse remembers what was sent, so a change alerts once.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pandas as pd
import yaml

LEVELS = ("high", "medium", "info")
STATUS_WORDS = {"preferred": "preferred", "non_preferred": "non-preferred",
                "not_covered": "not covered"}  # fmt: skip
COLUMNS = ["severity", "state", "product", "drug_class", "change", "before", "after", "effective",
           "from_doc", "to_doc", "confirmed", "key"]  # fmt: skip


@dataclass
class Watchlist:
    name: str
    products: set[str] = field(default_factory=set)  # all lower case
    ingredients: set[str] = field(default_factory=set)
    classes: set[str] = field(default_factory=set)
    states: set[str] = field(default_factory=set)  # empty: every state

    @classmethod
    def load(cls, path: str | Path) -> Watchlist:
        d = yaml.safe_load(Path(path).read_text()) or {}
        if not isinstance(d, dict):
            raise ValueError(f"{path}: a watchlist is a mapping (name, products, ...)")

        def words(key: str) -> set[str]:
            return {str(v).strip().lower() for v in d.get(key) or [] if str(v).strip()}

        wl = cls(str(d.get("name") or Path(path).stem), words("products"), words("ingredients"),
                 words("classes"), {s.upper() for s in words("states")})  # fmt: skip
        if not (wl.products or wl.ingredients or wl.classes):
            raise ValueError(f"{path}: name at least one product, ingredient or class to watch")
        return wl


def severity(change: str, before: Any, after: Any) -> str:
    if change == "removed" or (change == "status" and before == "preferred"
                               and after in {"non_preferred", "not_covered"}):  # fmt: skip
        return "high"
    if change == "ql" or (change == "status" and after != "preferred"):
        return "medium"
    return "info"


def _key(r: Any) -> str:
    return f"{r.state}|{r.product}|{r.change}|{r.to_doc}"


def match(changes: pd.DataFrame, products: pd.DataFrame, wl: Watchlist) -> pd.DataFrame:
    """The changes a watchlist covers, rated, most severe first."""
    if changes.empty:
        return pd.DataFrame(columns=COLUMNS)
    ing = dict(zip(products["product"], products["ingredient"].fillna(""), strict=True))
    c = changes.copy()
    hit = (c["product"].str.lower().isin(wl.products)
           | c["product"].map(lambda p: str(ing.get(p, "")).lower()).isin(wl.ingredients)
           | c["drug_class"].fillna("").str.lower().isin(wl.classes))  # fmt: skip
    if wl.states:
        hit &= c["state"].isin(wl.states)
    c = c[hit]
    if c.empty:
        return pd.DataFrame(columns=COLUMNS)
    c = c.assign(
        severity=[severity(r.change, r.before, r.after) for r in c.itertuples()],
        confirmed=~c["needs_review"].astype(bool),
        key=[_key(r) for r in c.itertuples()],
    )
    order = c["severity"].map(LEVELS.index)
    return (
        c.assign(_o=order).sort_values(["_o", "state", "product"])[COLUMNS].reset_index(drop=True)
    )


def ledger_path(db_path: str | Path) -> Path:
    return Path(db_path).with_name("alerts_sent.jsonl")


def sent_keys(path: Path, watchlist: str) -> set[str]:
    if not path.exists():
        return set()
    lines = (json.loads(x) for x in path.read_text().splitlines() if x.strip())
    return {e["key"] for e in lines if e.get("watchlist") == watchlist}


def record(path: Path, watchlist: str, alerts: pd.DataFrame) -> None:
    now = datetime.now(UTC).isoformat(timespec="seconds")
    with path.open("a") as f:
        for k in alerts["key"]:
            f.write(json.dumps({"watchlist": watchlist, "key": k, "sent_at": now}) + "\n")


def _word(v: Any) -> str:
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return "none"
    return STATUS_WORDS.get(str(v), str(v))


def _line(r: Any) -> str:
    if r.change == "added":
        what = f"added as {_word(r.after)}"
    elif r.change == "removed":
        what = f"removed (was {_word(r.before)})"
    elif r.change == "ql":
        what = f"quantity limit {_word(r.before)} → {_word(r.after)}"
    else:
        what = f"{_word(r.before)} → {_word(r.after)}"
    return (f"- **{r.state} · {r.product}**: {what}, effective {r.effective} "
            f"({r.to_doc}, compared with {r.from_doc})")  # fmt: skip


def digest(alerts: pd.DataFrame, wl: Watchlist) -> str:
    """The alerts as Markdown: confirmed changes by severity, then those waiting for review."""
    sure = alerts[alerts["confirmed"]]
    waiting = alerts[~alerts["confirmed"]]
    counts = ", ".join(f"{int((sure['severity'] == lv).sum())} {lv}" for lv in LEVELS)
    out = [f"# PDLwatch alerts: {wl.name}", "",
           f"{counts}; {len(waiting)} more waiting for review."]  # fmt: skip
    for lv in LEVELS:
        part = sure[sure["severity"] == lv]
        if len(part):
            out += ["", f"## {lv.capitalize()}", *(_line(r) for r in part.itertuples())]
    if len(waiting):
        out += ["", "## Waiting for review (not confirmed: check the source before acting)",
                *(_line(r) for r in waiting.itertuples())]  # fmt: skip
    if not len(alerts):
        out += ["", "No changes for this watchlist."]
    return "\n".join(out) + "\n"


def send(text: str, url: str | None = None, client: Any = None) -> bool:
    """POST the digest to an incoming webhook (Slack or Teams take {"text": ...}). Returns
    whether it was sent; nothing is sent when no webhook is configured."""
    url = url or os.environ.get("PDLWATCH_ALERT_WEBHOOK")
    if not url:
        return False
    import httpx

    own = client is None
    client = client or httpx.Client(timeout=20)
    try:
        client.post(url, json={"text": text}).raise_for_status()
    finally:
        if own:
            client.close()
    return True
