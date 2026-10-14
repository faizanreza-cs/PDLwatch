"""Spot-check extracted rows against their PDF, for data without a truth file (real PDLs).

``pdlwatch audit`` samples accepted rows and prints, for each, the line it came from and the
nearest table header above it, with every phrase's horizontal position. A reader can then check
by eye that the drug sits under the column the row says (PREFERRED vs NON-PREFERRED, the Status
or Tier cell) and that the flags match the cells. That gives a measured precision on real
documents without anyone labelling them in advance.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Any

import pandas as pd

from pdlwatch.extract import layout
from pdlwatch.pdf import Line, Page, load
from pdlwatch.warehouse import get_warehouse


@lru_cache(maxsize=16)
def _pages(path: str) -> tuple[Page, ...]:
    return tuple(load(path))


def _show(line: Line) -> str:
    return " | ".join(f"{p.text} @{p.x0:.0f}" for p in line.phrases)


def context(path: str, page: int, drug_text: str) -> dict[str, Any]:
    """The source line of a row and the nearest header line above it on the same page."""
    pg = _pages(path)[page - 1]
    key = drug_text.split("(")[0].strip().lower()[:24]
    for i, line in enumerate(pg.lines):
        if key and key in line.text.lower():
            head = next((ln for ln in reversed(pg.lines[:i]) if layout.header(ln, pg.width)), None)
            return {"header": _show(head) if head else "(no header above on this page)",
                    "line": _show(line)}  # fmt: skip
    return {"header": "", "line": "(line not found)"}


def sample(n: int = 40, seed: int = 1, state: str | None = None) -> pd.DataFrame:
    wh = get_warehouse()
    where = "r.accepted"
    if state:
        where += f" AND r.state = '{state.upper()[:2]}'"
    rows = wh.query(f"""SELECT r.doc_id, r.state, r.page, r.method, r.drug_text, r.product,
                               r.status, r.tier, r.pa, r.ql, r.st, d.file
                        FROM formulary_rows r JOIN documents d ON d.doc_id = r.doc_id
                        WHERE {where}""")  # fmt: skip
    picked = rows.sample(min(n, len(rows)), random_state=seed) if len(rows) else rows
    return picked.sort_values(["state", "doc_id", "page"]).reset_index(drop=True)


def report(n: int = 40, seed: int = 1, state: str | None = None,
           folder: str | None = None) -> list[dict[str, Any]]:  # fmt: skip
    """Rows to check, each with its source line and header. ``folder``: where the PDFs are."""
    from pathlib import Path

    from pdlwatch.config import corpus_path, get_settings

    base = Path(folder) if folder else corpus_path(get_settings().db_path) / "pdfs"
    out = []
    for r in sample(n, seed, state).itertuples():
        ctx = context(str(base / r.file), int(r.page), str(r.drug_text))
        out.append({"doc": r.doc_id, "page": r.page, "method": r.method, "drug": r.drug_text,
                    "status": r.status, "tier": r.tier, "pa": r.pa, "ql": r.ql, **ctx})  # fmt: skip
    return out
