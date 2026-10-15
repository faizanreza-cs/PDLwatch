"""Spot-check extracted rows against their PDF, for data without a truth file (real PDLs).

``pdlwatch audit`` samples accepted rows and prints, for each, the line it came from and the
nearest table header above it, with every phrase's horizontal position. A reader can then check
by eye that the drug sits under the column the row says (PREFERRED vs NON-PREFERRED, the Status
or Tier cell) and that the flags match the cells. That gives a measured precision on real
documents without anyone labelling them in advance.

``pdlwatch inspect`` shows how the parser reads chosen pages: each line's phrases with their
positions, the column each one falls in, and the rows that came out. It is how a new state's
layout is checked before its rows are trusted.
"""

from __future__ import annotations

import json
from functools import lru_cache
from typing import Any

import pandas as pd

from pdlwatch.extract import layout
from pdlwatch.extract.document import effective_source
from pdlwatch.pdf import Line, Page, file_dates, load
from pdlwatch.sheet import columns, is_sheet
from pdlwatch.warehouse import get_warehouse


@lru_cache(maxsize=16)
def _pages(path: str, profile: str | None = None) -> tuple[Page, ...]:
    """The pages as the document was read: a spreadsheet through its source profile (JSON)."""
    return tuple(load(path, profile=json.loads(profile) if profile else None))


def _show(line: Line) -> str:
    return " | ".join(f"{p.text} @{p.x0:.0f}" for p in line.phrases)


def context(path: str, page: int, drug_text: str, y: float | None = None,
            profile: str | None = None) -> dict[str, Any]:  # fmt: skip
    """The source line of a row and the nearest header line above it on the same page.

    The line is the one at the row's position ``y`` when known, else the first one that
    contains the drug's name. ``profile``: the document's source profile (JSON), if any.
    """
    pg = _pages(path, profile)[page - 1]
    key = drug_text.split("(")[0].strip().lower()[:24]
    found = None
    if y is not None and y == y and pg.lines:  # y == y: not NaN
        i, line = min(enumerate(pg.lines), key=lambda t: abs(t[1].top - y))
        if abs(line.top - y) < 1.0:
            found = i
    if found is None:
        found = next((i for i, ln in enumerate(pg.lines) if key and key in ln.text.lower()), None)
    if found is None:
        return {"header": "", "line": "(line not found)"}
    head = next((ln for ln in reversed(pg.lines[:found]) if layout.header(ln, pg.width)), None)
    return {"header": _show(head) if head else "(no header above on this page)",
            "line": _show(pg.lines[found])}  # fmt: skip


def inspect(path: str, pages: list[int], width: int = 48) -> list[str]:
    """How the parser reads ``pages`` of one PDF, as printable lines. For a spreadsheet, each
    column's values come first (to write a source profile from); its pages are shown as filed."""
    doc = list(_pages(path))
    wanted = sorted({p for p in pages if 1 <= p <= len(doc)})
    if not wanted:
        return [f"{path}: no such page (it has {len(doc)})"]
    trace: layout.Trace = {}
    rows, _ = layout.parse(doc[: wanted[-1]], trace)
    out = columns(path) if is_sheet(path) else []
    if 1 in wanted:
        d, origin, words = effective_source(doc)
        where = f" ({origin}) from: ...{words}..." if d else ""
        out.append(f"effective: {d or 'not found'}{where}")
        out.append(f"file dates: {file_dates(path)}")
    for n in wanted:
        pg = doc[n - 1]
        out.append(f"== {path.rsplit('/', 1)[-1]} p.{n} ({pg.width:.0f} x {pg.height:.0f} pt"
                   f"{', OCR' if pg.ocr else ''})")  # fmt: skip
        last = None
        for line, lay, what in trace.get(n, []):
            if lay is not None and lay is not last:
                cols = " | ".join(f"{c.role} {c.x0:.0f}-{c.x1:.0f}" for c in lay.columns)
                out.append(f"   [{lay.mode} columns] {cols}")
                last = lay
            cells = " | ".join(
                f"{p.text[:width]}@{p.x0:.0f}"
                + (f">{lay.column(p.x0).role}" if lay is not None and what == "row" else "")
                for p in line.phrases
            )
            out.append(f"   y={line.top:6.1f} {what[:7]:<7} {cells}")
        got = [r for r in rows if r.page == n]
        out.append(f"   rows read: {len(got)}")
        for r in got:
            out.append(f"     y={r.y:6.1f} {r.list_role or r.method}: {r.cells}")
    return out


def sample(n: int = 40, seed: int = 1, state: str | None = None) -> pd.DataFrame:
    wh = get_warehouse()
    where = "r.accepted"
    if state:
        where += f" AND r.state = '{state.upper()[:2]}'"
    rows = wh.query(f"""SELECT r.doc_id, r.state, r.page, r.y, r.method, r.drug_text, r.product,
                               r.status, r.tier, r.pa, r.ql, r.st, d.file, d.profile
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
        profile = r.profile if isinstance(r.profile, str) and r.profile else None
        ctx = context(str(base / r.file), int(r.page), str(r.drug_text), r.y, profile)
        out.append({"doc": r.doc_id, "page": r.page, "method": r.method, "drug": r.drug_text,
                    "status": r.status, "tier": r.tier, "pa": r.pa, "ql": r.ql, **ctx})  # fmt: skip
    return out
