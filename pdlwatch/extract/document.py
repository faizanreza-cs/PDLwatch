"""One PDF in, canonical rows out: metadata, legend, tables and lists, prose, scoring."""

from __future__ import annotations

import re
import time
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from pdlwatch.extract import layout, prose
from pdlwatch.extract import legend as legend_mod
from pdlwatch.extract.rows import Row, dedupe, from_raw, score
from pdlwatch.pdf import Page, load
from pdlwatch.synth.states import BY_NAME as STATE_CODES

_DATE_FORMATS = ("%B %d, %Y", "%m/%d/%Y", "%Y-%m-%d", "%b %d, %Y", "%B %Y")
_DATE = re.compile(
    r"effective(?: date)?:?\s+((?:january|february|march|april|may|june|july|august|september|"
    r"october|november|december)\s+\d{1,2},\s+\d{4}|\d{1,2}/\d{1,2}/\d{4}|\d{4}-\d{2}-\d{2})",
    re.I,
)


@dataclass
class DocResult:
    doc_id: str
    path: str
    state: str | None
    effective: str | None
    title: str
    doc_type: str  # pdl | pa_criteria
    pages: int
    ocr_pages: int
    rows: list[Row]
    legend: dict[str, str] = field(default_factory=dict)
    seconds: float = 0.0
    llm_calls: int = 0

    def summary(self) -> dict[str, Any]:
        methods = Counter(r.method for r in self.rows)
        return {
            "doc_id": self.doc_id, "path": self.path, "state": self.state,
            "effective": self.effective, "title": self.title, "doc_type": self.doc_type,
            "pages": self.pages, "ocr_pages": self.ocr_pages, "rows": len(self.rows),
            "accepted": sum(r.accepted for r in self.rows),
            "review": sum(not r.accepted for r in self.rows),
            "methods": dict(methods), "seconds": round(self.seconds, 2),
            "llm_calls": self.llm_calls,
        }  # fmt: skip


def _effective(text: str) -> str | None:
    m = _DATE.search(text)
    if not m:
        return None
    raw = " ".join(m.group(1).split())
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(raw, fmt).date().isoformat()
        except ValueError:
            continue
    return None


def _state(text: str) -> str | None:
    """The first state named in the document's opening lines."""
    low = text.lower()
    hits = [(low.find(name), code) for name, code in STATE_CODES.items() if name in low]
    hits = [h for h in hits if h[0] >= 0]
    if not hits:
        return None
    # "West Virginia" contains "Virginia": prefer the longest name at the earliest position
    first = min(h[0] for h in hits)
    return max((h for h in hits if h[0] == first), key=lambda h: len(h[1]))[1] if hits else None


def legend_text(pages: list[Page]) -> str:
    """Page 1 up to the first table header, plus any definition lines ("X = ...") later on."""
    out = []
    if pages:
        for line in pages[0].lines:
            if layout.header(line, pages[0].width):
                break
            out.append(line.text)
    for page in pages[1:]:
        out += [ln.text for ln in page.lines if " = " in ln.text]
    return " ".join(out)


def extract_document(path: str | Path, doc_id: str | None = None, state: str | None = None,
                     llm: Any = None) -> DocResult:  # fmt: skip
    """Extract every formulary row from one PDF. ``llm``: an ``LLMExtractor`` for prose pages."""
    t0 = time.perf_counter()
    path = Path(path)
    doc_id = doc_id or path.stem
    pages = load(path)
    head = " ".join(ln.text for ln in pages[0].lines[:6]) if pages else ""
    state = state or _state(head)
    leg = legend_mod.parse(legend_text(pages))
    raw, table_pages = layout.parse(pages)
    rows = [from_raw(r, leg, doc_id, state) for r in raw]
    prose_pages = [p for p in pages if p.number not in table_pages]
    calls = 0
    if llm is not None and prose_pages:
        llm_rows, calls = llm.extract(prose_pages, doc_id, state)
        rows += llm_rows
    else:
        rows += [prose.to_row(p, doc_id, state) for p in prose.extract(pages, table_pages)]
    rows = [r for r in rows if r.product or _worth_review(r.drug_text)]
    rows = dedupe(score(rows))
    title = pages[0].lines[0].text if pages and pages[0].lines else path.stem
    doc_type = _doc_type(title, head, bool(table_pages))
    return DocResult(doc_id, str(path), state, _effective(" ".join(
        ln.text for p in pages[:1] for ln in p.lines)), title, doc_type, len(pages),
        sum(p.ocr for p in pages), rows, leg.codes, time.perf_counter() - t0, calls)  # fmt: skip


def _doc_type(title: str, head: str, has_tables: bool) -> str:
    """pa_criteria for criteria documents, pdl for drug lists and formularies. The title decides
    first: a PDL's first lines often include a "PA Criteria" column header."""
    for text in (title, head):
        if re.search(r"preferred drug list|\bpdl\b|formulary|drug list", text, re.I):
            return "pdl"
        if re.search(r"criteria", text, re.I):
            return "pa_criteria"
    return "pdl" if has_tables else "pa_criteria"


def _worth_review(text: str) -> bool:
    """An unmatched cell is kept for review if it looks like a name, not an OCR speck."""
    return bool(re.search(r"[A-Za-z]{4,}", text)) and sum(c.isalpha() for c in text) >= 5
