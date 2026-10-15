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
from pdlwatch.review import row_key
from pdlwatch.sheet import is_sheet
from pdlwatch.synth.states import BY_NAME as STATE_CODES

_MONTHS = (
    "january|february|march|april|may|june|july|august|september|october|november|"
    "december|jan|feb|mar|apr|jun|jul|aug|sept|sep|oct|nov|dec"
)
_DATE_TEXT = (rf"(?:{_MONTHS})\.?\s+\d{{1,2}}(?:st|nd|rd|th)?,?\s+\d{{4}}"
              r"|\d{1,2}[/.-]\d{1,2}[/.-](?:\d{4}|\d{2})(?!\d)"
              r"|\d{4}-\d{2}-\d{2}"
              rf"|(?:{_MONTHS})\.?,?\s+\d{{4}}")  # fmt: skip
# "Effective July 1, 2026", "Effective for dates of service on or after 07/01/2026"
_DATE = re.compile(rf"\beffective\b[^.\n]{{0,60}}?({_DATE_TEXT})", re.I)
_ANY_DATE = re.compile(rf"(?<!\d)({_DATE_TEXT})", re.I)
_DATE_FORMATS = ("%B %d %Y", "%b %d %Y", "%m/%d/%Y", "%m/%d/%y", "%m.%d.%Y", "%m.%d.%y",
                 "%m-%d-%Y", "%m-%d-%y", "%Y-%m-%d", "%B %Y", "%b %Y")  # fmt: skip
# dates in file names: 20260701_UPDL, PDL_2025_01_01, pdl10242025, 4_30_24, October-2026
_FILE_DATES = (
    (re.compile(r"(?<!\d)(20\d\d)[-_.]?(0[1-9]|1[0-2])[-_.]?(0[1-9]|[12]\d|3[01])(?!\d)"), "ymd"),
    (re.compile(r"(?<!\d)(0[1-9]|1[0-2])[-_.]?(0[1-9]|[12]\d|3[01])[-_.]?(20\d\d)(?!\d)"), "mdy"),
    (re.compile(r"(?<!\d)(\d{1,2})[-_.](\d{1,2})[-_.](\d\d)(?!\d)"), "mdy"),
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
    complete: bool = True  # False when a scanned page couldn't be OCR'd
    sha256: str | None = None  # of the PDF's bytes
    effective_from: str | None = None  # document | title | source title | file name
    first_seen: str | None = None  # when the archive first fetched this file (real PDLs)

    def summary(self) -> dict[str, Any]:
        methods = Counter(r.method for r in self.rows)
        return {
            "doc_id": self.doc_id, "path": self.path, "state": self.state,
            "effective": self.effective, "title": self.title, "doc_type": self.doc_type,
            "pages": self.pages, "ocr_pages": self.ocr_pages, "rows": len(self.rows),
            "accepted": sum(r.accepted for r in self.rows),
            "review": sum(not r.accepted for r in self.rows),
            "methods": dict(methods), "seconds": round(self.seconds, 2),
            "llm_calls": self.llm_calls, "sha256": self.sha256,
            "effective_from": self.effective_from, "first_seen": self.first_seen,
        }  # fmt: skip


def _to_date(raw: str) -> str | None:
    t = re.sub(r"(\d)(st|nd|rd|th)\b", r"\1", raw.strip(), flags=re.I)
    t = re.sub(r"(?<=[a-z])\.", "", t, flags=re.I).replace(",", " ")  # "Jan. 1, 2026"
    t = " ".join(re.sub(r"\bsept\b", "sep", t, flags=re.I).split())
    for fmt in _DATE_FORMATS:
        try:
            d = datetime.strptime(t, fmt).date()
        except ValueError:
            continue
        if 2000 <= d.year <= 2040:
            return d.isoformat()
    return None


def effective_source(pages: list[Page]) -> tuple[str | None, str | None, str]:
    """The effective date, where it was read and the words around it: an "effective ..."
    phrase on the first two pages, else a date in the opening lines (the title)."""
    text = " ".join(ln.text for p in pages[:2] for ln in p.lines)
    for m in _DATE.finditer(text):
        if d := _to_date(m.group(1)):
            return d, "document", text[max(0, m.start() - 40) : m.end() + 40]
    head = " ".join(ln.text for ln in pages[0].lines[:8]) if pages else ""
    for m in _ANY_DATE.finditer(head):
        if d := _to_date(m.group(1)):
            return d, "title", head[max(0, m.start() - 40) : m.end() + 40]
    return None, None, ""


def _effective(pages: list[Page]) -> tuple[str | None, str | None]:
    d, origin, _ = effective_source(pages)
    return d, origin


def date_hint(text: str) -> str | None:
    """A date in a source's title or a file name, for a document that doesn't print one."""
    t = text or ""
    for pattern in (_DATE, _ANY_DATE):
        for m in pattern.finditer(t):
            if d := _to_date(m.group(1)):
                return d
    for pattern, order in _FILE_DATES:
        for m in pattern.finditer(t):
            parts = dict(zip(order, m.groups(), strict=True))
            y = int(parts["y"]) + (2000 if len(parts["y"]) == 2 else 0)
            try:
                return datetime(y, int(parts["m"]), int(parts["d"])).date().isoformat()
            except ValueError:
                continue
    month = re.search(rf"(?<![a-z])({_MONTHS})[-_ ]?(20\d\d)(?!\d)", t, re.I)
    return _to_date(f"{month.group(1)} {month.group(2)}") if month else None


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


# a footnote at the foot of a page: "* Drug coverage subject to meeting clinical prior
# authorization criteria", "** Drug coverage subject to quantity limits"
_FOOTNOTE = re.compile(r"^(\*{1,3}|[†‡§#^])\s+([A-Za-z].{5,})$")


def legend_text(pages: list[Page]) -> str:
    """Page 1 up to the first table header, plus any definition lines ("X = ...") later on and
    the footnotes pages define their symbols in (as "* = meaning.")."""
    out = []
    if pages:
        for line in pages[0].lines:
            if layout.header(line, pages[0].width):
                break
            out.append(line.text)
    notes: dict[str, str] = {}
    for page in pages:
        for line in page.lines:
            m = _FOOTNOTE.match(line.phrases[0].text.strip()) if line.phrases else None
            if m:  # the first phrase only: a note beside it in the next column is not its meaning
                notes.setdefault(m.group(1), m.group(2).rstrip(" ."))
    for page in pages[1:]:
        out += [ln.text for ln in page.lines if " = " in ln.text]
    out += [f"{code} = {meaning}." for code, meaning in notes.items()]
    return " ".join(out)


def _default_status(head: str, raw: list[layout.RawRow]) -> str | None:
    """A preferred drug list whose tables have no status column lists only preferred drugs
    (Florida's: HIC3, label name, generic name, ages, PA required). A document with a status
    column anywhere gets no default: a table without one is something else there."""
    if not raw or not re.search(r"preferred drug list", head, re.I):
        return None
    if re.search(r"non-?\s?preferred drug list", head, re.I):
        return None
    status = {"status", "tier", *layout.LIST_ROLES}
    return None if any(r.roles & status for r in raw) else "preferred"


def extract_document(path: str | Path, doc_id: str | None = None, state: str | None = None,
                     llm: Any = None) -> DocResult:  # fmt: skip
    """Extract every formulary row from one PDF or spreadsheet. ``llm``: an ``LLMExtractor`` for
    prose pages."""
    t0 = time.perf_counter()
    path = Path(path)
    doc_id = doc_id or path.stem
    pages = load(path)
    spreadsheet = is_sheet(path)
    head = " ".join(ln.text for ln in pages[0].lines[:6]) if pages else ""
    state = state or _state(head)
    leg = legend_mod.parse(legend_text(pages))
    raw, table_pages = layout.parse(pages)
    default = _default_status(head, raw)
    rows = [from_raw(r, leg, doc_id, state, default) for r in raw]
    # a workbook's content is its tables: a notes sheet is not prose to read drugs from
    prose_pages = [] if spreadsheet else [p for p in pages if p.number not in table_pages]
    calls = 0
    if llm is not None and prose_pages:
        llm_rows, calls = llm.extract(prose_pages, doc_id, state)
        rows += llm_rows
    elif not spreadsheet:
        rows += [prose.to_row(p, doc_id, state) for p in prose.extract(pages, table_pages)]
    rows = [r for r in rows if r.product or _worth_review(r.drug_text)]
    rows = dedupe(score(rows))
    texts = {p.number: p.text for p in pages}
    for r in rows:
        # a spreadsheet row is its own record: it keeps its decisions while other rows change
        r.row_key = (row_key(r.evidence, 0, r.drug_text) if spreadsheet
                     else row_key(texts.get(r.page, ""), r.y, r.drug_text))  # fmt: skip
    title = pages[0].lines[0].text if pages and pages[0].lines else path.stem
    doc_type = _doc_type(title, head, bool(table_pages))
    # a workbook states its date above the table, if anywhere: a date column inside the table
    # ("Effective date" of each listing) is not the list's
    effective, effective_from = _effective(_title_rows(pages) if spreadsheet else pages)
    return DocResult(doc_id, str(path), state, effective, title, doc_type, len(pages),
                     sum(p.ocr for p in pages), rows, leg.codes, time.perf_counter() - t0, calls,
                     complete=not any(p.ocr_failed for p in pages),
                     effective_from=effective_from)  # fmt: skip


def _title_rows(pages: list[Page]) -> list[Page]:
    """The first page's lines above its first table header (a workbook's title and notes)."""
    if not pages:
        return []
    above = []
    for line in pages[0].lines:
        if layout.header(line, pages[0].width):
            break
        above.append(line)
    return [Page(1, pages[0].width, pages[0].height, above, False)]


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
