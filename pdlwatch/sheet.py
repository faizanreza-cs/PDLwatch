"""PDLs published as spreadsheets (Excel or CSV), read as pages of positioned rows.

Some states publish their list as a workbook as well as, or instead of, a PDF (Washington posts
its Apple Health PDL every week as Excel). Each sheet becomes pages of lines: one line per row,
one phrase per filled cell, each column at its own position. The table parser then reads it
exactly as it reads a ruled PDF table: the same header vocabulary, legends, matching, scoring
and review, and every row cites its sheet ("page") and row position.
"""

from __future__ import annotations

import csv
from datetime import date, datetime
from pathlib import Path
from typing import Any

from pdlwatch.pdf import Line, Page, Phrase, Word

SUFFIXES = {".xlsx", ".xlsm", ".csv"}
ROWS_PER_PAGE = 60
COLUMN_WIDTH = 120.0
ROW_HEIGHT = 20.0  # far enough apart that no two rows read as one wrapped header


def is_sheet(path: str | Path) -> bool:
    return Path(path).suffix.lower() in SUFFIXES


def _text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    return " ".join(str(value).split())


def _tables(path: Path) -> list[tuple[str, list[list[str]]]]:
    """(sheet name, rows of cell text) for every sheet with content."""
    if path.suffix.lower() == ".csv":
        with path.open(newline="", encoding="utf-8-sig", errors="replace") as f:
            return [(path.stem, [[_text(v) for v in row] for row in csv.reader(f)])]
    from openpyxl import load_workbook

    wb = load_workbook(path, read_only=True, data_only=True)
    try:
        out = []
        for ws in wb.worksheets:
            rows = [[_text(v) for v in row] for row in ws.iter_rows(values_only=True)]
            if any(any(row) for row in rows):
                out.append((ws.title, rows))
        return out
    finally:
        wb.close()


def _line(cells: list[str], top: float) -> Line | None:
    phrases = []
    for col, text in enumerate(cells):
        if not text:
            continue
        x = 20.0 + col * COLUMN_WIDTH
        # a cell's words stay inside its column however long the text: the parser assigns each
        # word to the column its left edge falls in
        words = [Word(w, x + i * 0.01, x + i * 0.01 + 0.005, top, top + 9.0)
                 for i, w in enumerate(text.split())]  # fmt: skip
        phrases.append(Phrase(words))
    return Line(phrases) if phrases else None


def load(path: str | Path) -> list[Page]:
    """Every sheet as pages of up to ROWS_PER_PAGE rows, numbered on from one sheet to the
    next. The rows sit inside the page body, clear of the title and footer bands."""
    pages: list[Page] = []
    for _name, rows in _tables(Path(path)):
        width = 40.0 + COLUMN_WIDTH * max((len(r) for r in rows), default=1)
        for start in range(0, len(rows), ROWS_PER_PAGE):
            chunk = rows[start : start + ROWS_PER_PAGE]
            height = len(chunk) * ROW_HEIGHT / 0.8
            lines = [ln for i, cells in enumerate(chunk)
                     if (ln := _line(cells, 0.1 * height + i * ROW_HEIGHT))]  # fmt: skip
            if lines:
                pages.append(Page(len(pages) + 1, width, height, lines, False))
    return pages
