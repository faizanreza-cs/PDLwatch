"""Tables and side-by-side lists, read from word positions.

A header row ("Drug Name | Status | PA | QL", or "PREFERRED AGENTS | NON-PREFERRED AGENTS") sets
the columns: each header phrase starts a column, which runs to the start of the next one. Words
below are assigned to columns by their left edge. This works the same on a digital page and on
OCR word boxes, with or without ruling lines.

* **record mode** (drug, status, tier, PA, QL, ST, requirements): one drug per row. A row starts
  when the status or tier cell has text; a line without one continues the row above (wrapped
  cells).
* **list mode** (preferred / non-preferred / not covered): every line is one drug per list
  column; the column says the status.

Class headings come from a class column, from a bold or class-named line, or, failing both,
from the drug dictionary later.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from rapidfuzz import fuzz, process

from pdlwatch.drugs import match_class, match_product
from pdlwatch.pdf import Line, Page, Phrase, Word

LIST_ROLES = ("preferred", "non_preferred", "not_covered")
_ROLE_PATTERNS: list[tuple[str, str]] = [
    ("not_covered", r"(not covered|excluded( drugs| products)?|non-covered|not on formulary)"),
    ("non_preferred", r"(non-? ?preferred|not preferred|non-? ?pdl)"
                      r"( agents| drugs| products| medications)?"),
    ("preferred", r"preferred( agents| drugs| products| medications)"),
    ("criteria", r"(pa criteria|clinical criteria|criteria|comments)"),
    ("class", r"(therapeutic class|drug class|class|category|therapeutic category)"),
    ("drug", r"(drug|drug name|medication|product|product name|agent|brand name|generic name)"),
    ("status", r"(status|pdl status|preferred status|formulary status|preferred\?"
               r"|preferred y/n|preferred|pdl)"),
    ("tier", r"(tier|drug tier|formulary tier)"),
    ("pa", r"(pa|prior auth|prior authorization|pa required|prior authorization required)"),
    ("ql", r"(ql|quantity limit|quantity limits|qty limit)"),
    ("st", r"(st|step therapy|step)"),
    ("req", r"(requirements/limits|requirements|coverage requirements and limits"
            r"|requirements and limits|restrictions|notes|limits)"),
]  # fmt: skip
_FOOTER = re.compile(r"^(page \d+( of \d+)?|\d+)$", re.I)
_TOP, _BOTTOM = 0.085, 0.945  # page decor (title, banner, page number) lives outside this band


# long header phrases, for headers OCR split or misspelt ("Cc overage Requirements and Limits")
_LONG_HEADERS: dict[str, str] = {
    phrase: r
    for r, phrases in {
        "not_covered": ["not covered", "excluded drugs", "excluded products", "not on formulary"],
        "non_preferred": ["non-preferred agents", "non-preferred drugs", "non-preferred products",
                          "non-preferred medications", "not preferred"],
        "preferred": ["preferred agents", "preferred drugs", "preferred products",
                      "preferred medications"],
        "criteria": ["pa criteria", "clinical criteria"],
        "class": ["therapeutic class", "therapeutic category", "drug class"],
        "drug": ["drug name", "medication", "product name", "brand name", "generic name"],
        "status": ["pdl status", "preferred status", "formulary status"],
        "tier": ["drug tier", "formulary tier"],
        "pa": ["prior authorization", "prior authorization required", "pa required"],
        "ql": ["quantity limit", "quantity limits"],
        "st": ["step therapy"],
        "req": ["requirements", "requirements/limits", "requirements and limits",
                "coverage requirements and limits", "restrictions"],
    }.items()
    for phrase in phrases
}  # fmt: skip


def role(text: str) -> str | None:
    t = " ".join(re.sub(r"[^a-z0-9/?\- ]", " ", text.lower()).split())
    for r, pattern in _ROLE_PATTERNS:
        if re.fullmatch(pattern, t):
            return r
    if len(t) >= 10 and len(t.split()) <= 6:
        hit = process.extractOne(t, list(_LONG_HEADERS), scorer=fuzz.ratio, score_cutoff=90)
        if hit is not None:
            return _LONG_HEADERS[hit[0]]
    return None


@dataclass
class Column:
    role: str
    hx0: float  # where its header phrase sits
    hx1: float
    x0: float = 0.0  # the column's span on the page
    x1: float = 0.0


def _bounds(cols: list[Column], width: float) -> list[Column]:
    """Column spans from header positions. A column starts a quarter of the gap (3 to 20 pt)
    before its header, so text that drifts left (scans, centred headers) stays in its column."""
    cols = sorted(cols, key=lambda c: c.hx0)
    cols[0].x0 = 0.0
    for prev, c in zip(cols, cols[1:], strict=False):
        c.x0 = prev.x1 = c.hx0 - min(20.0, max(3.0, 0.25 * (c.hx0 - prev.hx1)))
    cols[-1].x1 = width + 1
    return cols


@dataclass
class Layout:
    mode: str  # record | list
    columns: list[Column]
    width: float

    def roles(self) -> set[str]:
        return {c.role for c in self.columns}

    def column(self, x: float) -> Column:
        for c in self.columns:
            if c.x0 <= x < c.x1:
                return c
        return self.columns[0] if x < self.columns[0].x0 else self.columns[-1]


def _join_split_phrases(phrases: list[Phrase]) -> list[Phrase]:
    """'NON-PREFERRED' + 'AGENTS' that a wide gap (or OCR) split into two phrases."""
    out: list[Phrase] = []
    for p in phrases:
        if (
            out
            and role(out[-1].text)
            and role(p.text) is None
            and role(out[-1].text + " " + p.text)
        ):
            out[-1] = Phrase(out[-1].words + p.words)
        else:
            out.append(p)
    return out


def header(line: Line, width: float) -> Layout | None:
    """The column layout a header line defines, or None if the line isn't a header."""
    tagged = [(role(p.text), p) for p in _join_split_phrases(line.phrases)]
    found = [(r, p) for r, p in tagged if r]
    unknown = [p for r, p in tagged if r is None and len(p.text) > 2]
    # real headers carry columns this parser doesn't use ("Brand/Generic", "Class Criteria");
    # allow them when two or more columns are recognised, the line starts with one of them and
    # none of the others is a drug name. A data row starts with its drug, known or not
    # ("metformin | Preferred | QL"), so it never passes for a header
    first = next((r for r, p in tagged if r is not None or len(p.text) > 2), None)
    if not found or (unknown and (len(found) < 2 or first is None or any(
            match_product(p.text).score == 100 for p in unknown))):  # fmt: skip
        return None
    names = {r for r, _ in found}
    if "drug" in names and len(found) >= 2:
        mode = "record"
    elif names & {"non_preferred", "not_covered"} or (
        "preferred" in {role(p.text + " agents") for _, p in found} and len(found) >= 2
    ):
        mode = "list"
    else:
        return None
    cols = []
    for r, p in tagged:
        if r is None:  # a column this parser doesn't read: kept so its text stays out of others
            cols.append(Column("other", p.x0, p.x1))
            continue
        if mode == "list" and r == "status":  # "PDL" or "PREFERRED" beside "NON-PDL"
            r = "preferred"
        cols.append(Column(r, p.x0, p.x1))
    return Layout(mode, _bounds(cols, width), width)


def merge_lines(first: Line, second: Line) -> Line:
    """A header whose cells wrap onto a second line ("PA" / "Required", "Step T" / "herapy").

    Each phrase of the second line joins the first-line phrase whose column it falls in; a
    continuation that starts in lower case was split mid-word, so it joins without a space.
    """
    starts = [p.x0 for p in first.phrases]
    joined = [list(p.words) for p in first.phrases]
    for ph in second.phrases:
        i = max((k for k, x in enumerate(starts) if x - 4 <= ph.x0), default=None)
        if i is None:
            return first
        words = list(ph.words)
        if words[0].text[:1].islower() and joined[i][-1].text[-1:].isalpha():
            last = joined[i][-1]
            joined[i][-1] = Word(last.text + words[0].text, last.x0, words[0].x1,
                                 last.top, words[0].bottom, last.bold,
                                 min(last.conf, words[0].conf))  # fmt: skip
            words = words[1:]
        joined[i] += words
    return Line([Phrase(ws) for ws in joined])


def _known(layout: Layout) -> int:
    return sum(c.role != "other" for c in layout.columns)


def _merge(new: Layout, last: Layout) -> Layout:
    """A list header that lost a phrase (OCR, a tilted scan) borrows it from the last one."""
    if new.mode != "list" or last.mode != "list" or not new.roles() < last.roles():
        return new
    missing = [c for c in last.columns if c.role not in new.roles()]
    cols = [Column(c.role, c.hx0, c.hx1) for c in new.columns + missing]
    return Layout("list", _bounds(cols, new.width), new.width)


@dataclass
class Cell:
    text: str
    conf: float


@dataclass
class RawRow:
    page: int
    method: str  # table | list
    cells: dict[str, str]  # role -> text: drug, class, status, tier, pa, ql, st, req
    drug_class: str | None
    list_role: str | None  # list mode: the column the drug was in
    ocr: bool
    conf: float
    evidence: str
    roles: frozenset[str] = frozenset()  # the columns this row's table has
    lines: int = 1
    reread: bool = False  # the status or tier cell was re-OCR'd on its own
    y: float = 0.0  # top of the line it starts on, to find it again on the page


def _cells(line: Line, layout: Layout) -> dict[str, Cell]:
    words: dict[str, list[tuple[str, float]]] = {}
    for p in line.phrases:
        for w in p.words:
            words.setdefault(layout.column(w.x0).role, []).append((w.text, w.conf))
    return {r: Cell(" ".join(t for t, _ in ws), min(c for _, c in ws)) for r, ws in words.items()}


def _decor(line: Line, page: Page) -> bool:
    return (line.top < _TOP * page.height or line.bottom > _BOTTOM * page.height
            or bool(_FOOTER.match(line.text.strip())))  # fmt: skip


def _heading(line: Line, cells: dict[str, Cell], layout: Layout) -> str | None:
    """A class heading inside a table: one phrase, bold or named like a class."""
    if len(line.phrases) != 1:
        return None
    text = line.text.strip()
    if match_class(text):
        return text
    if match_product(text).product is not None:
        return None  # "COBENFY" above the next header: a drug, even if the class bar was lost
    if line.bold:
        return text
    if layout.mode == "list" and text.isupper() and len(text) > 6:
        return text
    return None


@dataclass
class _State:
    layout: Layout | None = None
    last_list: Layout | None = None
    drug_class: str | None = None
    rows: list[RawRow] = field(default_factory=list)
    pages: set[int] = field(default_factory=set)


Trace = dict[int, list[tuple[Line, "Layout | None", str]]]


def parse(pages: list[Page], trace: Trace | None = None) -> tuple[list[RawRow], set[int]]:
    """Every table or list row in the document, and the pages they came from.

    ``trace`` (for ``pdlwatch inspect``) collects, per page, each line with the layout in force
    and what the line was taken for: header, heading, row or skipped.
    """
    s = _State()

    def note(page: Page, line: Line, what: str) -> None:
        if trace is not None:
            trace.setdefault(page.number, []).append((line, s.layout, what))

    for page in pages:
        lines = [ln for ln in page.lines if not _decor(ln, page)]
        on_page = False
        skip_next = False
        for i, line in enumerate(lines):
            if skip_next:
                skip_next = False
                note(page, line, "header")
                continue
            lay = header(line, page.width)
            if i + 1 < len(lines) and line.bottom + 6 > lines[i + 1].top:
                # a header whose cells wrap: the two lines together name more columns
                merged = header(merge_lines(line, lines[i + 1]), page.width)
                if merged is not None and (lay is None or _known(merged) > _known(lay)):
                    lay, skip_next = merged, True
            if lay is not None:
                if s.last_list is not None:
                    lay = _merge(lay, s.last_list)
                if lay.mode == "list" and lay.roles() & {"preferred", "non_preferred"}:
                    s.last_list = lay  # a NOT COVERED block isn't a template for the next class
                s.layout, on_page = lay, True
                s.pages.add(page.number)
                note(page, line, "header")
                continue
            if s.layout is None:  # before the first table: title, legend, notes
                note(page, line, "before the first header")
                nxt = header(lines[i + 1], page.width) if i + 1 < len(lines) else None
                if (
                    nxt is not None
                    and len(line.phrases) == 1
                    and (line.bold or match_class(line.text.strip()))
                ):
                    s.drug_class = line.text.strip()  # the first class bar, above the header
                continue
            if not on_page:  # a page that doesn't repeat the header: keep the last layout
                on_page = True
                s.pages.add(page.number)
            cells = _cells(line, s.layout)
            nxt = header(lines[i + 1], page.width) if i + 1 < len(lines) else None
            heading = _heading(line, cells, s.layout)
            if heading and (nxt is not None or s.layout.mode == "record" or match_class(heading)):
                s.drug_class = heading
                note(page, line, "class heading")
                continue
            note(page, line, "row")
            if s.layout.mode == "record":
                _record_line(s, page, line, cells)
            else:
                _list_line(s, page, line, cells)
    return s.rows, s.pages


def _record_line(s: _State, page: Page, line: Line, cells: dict[str, Cell]) -> None:
    assert s.layout is not None
    keys = s.layout.roles() & {"status", "tier"}
    starts = any(r in cells for r in keys) if keys else "drug" in cells
    reread = False
    if not starts and keys and "drug" in cells and _whole_drug(cells["drug"].text):
        # a drug on its own with an empty status: re-read the cell (OCR drops short tokens
        # like "T2"), else start the row anyway and let review fill the status in
        for k in keys:
            col = next(c for c in s.layout.columns if c.role == k)
            text = page.ocr_region(col.x0, line.top - 2, col.x1, line.bottom + 2)
            if text:
                cells[k] = Cell(text, 50.0)
                reread = True
        starts = True
    if starts and "drug" in cells:
        raw = {r: c.text for r, c in cells.items()}
        if raw.get("class"):
            s.drug_class = raw["class"]
        s.rows.append(RawRow(page.number, "table", raw, s.drug_class, None, page.ocr,
                             min(c.conf for c in cells.values()), line.text,
                             frozenset(s.layout.roles()), reread=reread, y=line.top))  # fmt: skip
        return
    if s.rows and s.rows[-1].page == page.number:  # a wrapped cell continues the last row
        last = s.rows[-1]
        for r, c in cells.items():
            last.cells[r] = (last.cells.get(r, "") + " " + c.text).strip()
            if r == "class":
                s.drug_class = last.drug_class = last.cells["class"]
        last.evidence += " / " + line.text
        last.lines += 1


def _continues(prev: str, text: str) -> bool:
    """Does ``text`` continue the list item above it rather than start a new one?

    Only on evidence of a wrap: a marker or a parenthesis on its own, an unclosed "(", a name
    broken after "/" or "-", or a dosage form left over ("inhaler", "pen injector"). A line that
    merely starts in lower case is usually a generic on its own line ("metformin ER").
    """
    if _MARKER_ONLY.match(text) or text.startswith("("):
        return True
    if prev.count("(") > prev.count(")"):
        return True  # "Genvoya (elvitegravir/cobicistat/..." waiting for its ")"
    if prev.rstrip().endswith(("/", "-", ",", "&")):
        return True  # "insulin glargine-" / "yfgn"
    return bool(text[:1].islower() and _FORM_ONLY.match(text))


_FORM_ONLY = re.compile(
    r"^((pen|auto-?)?injectors?|pens?|inhalers?|inhalation|patch(es)?|spray|kit|vials?|"
    r"tablets?|capsules?|solution|suspension|powder|film|gel|cream|syringes?|cartridges?|"
    r"chewable|sprinkle|oral|nasal|topical|er|xr|sr|dr|odt)( \w+)?$",
    re.I,
)


# one or more footnote markers on their own ("QL", "CC QL", "†")
_MARKER_ONLY = re.compile(r"^(([*†‡§#^]+|cc|ql|st|pa|bvg|\(brand preferred\))\s*)+$", re.I)


def _whole_drug(text: str) -> bool:
    """A cell that is a drug name by itself, not the second line of a wrapped one."""
    return not text.startswith("(") and match_product(text).score == 100


def _list_line(s: _State, page: Page, line: Line, cells: dict[str, Cell]) -> None:
    if any(role(c.text) in {"criteria", *LIST_ROLES} for c in cells.values()):
        return  # a header OCR mangled ("DONT _PA CRITERIA")
    for r in LIST_ROLES:
        c = cells.get(r)
        if c is None or not c.text.strip():
            continue
        text = c.text.strip()
        marker = bool(_MARKER_ONLY.match(text))  # "QL" alone is a flag, not a "QL" header
        if not marker and (role(text) is not None or len(text) < 3):
            continue  # a stray header word or an OCR speck
        if text.endswith(".") and len(text.split()) >= 3:
            continue  # criteria prose that strayed into a list column
        if re.search(r"criteria|\b(non-?)?preferred\b", text, re.I) and not re.search(
            r"\(brand preferred\)$", text, re.I
        ):
            continue  # header words OCR mangled ("DONT _PA CRITERIA")
        prev = next((x for x in reversed(s.rows) if x.list_role == r), None)
        if prev is not None and prev.page == page.number and _continues(prev.cells["drug"], text):
            # a wrapped name ("glargine-" / "yfgn" joins without a space), or a marker
            sep = "" if prev.cells["drug"].rstrip().endswith(("-", "/")) else " "
            prev.cells["drug"] = prev.cells["drug"].rstrip() + sep + text
            prev.evidence += " / " + text
            continue
        if marker:
            continue
        assert s.layout is not None
        s.rows.append(RawRow(page.number, "list", {"drug": c.text}, s.drug_class, r, page.ocr,
                             c.conf, line.text, frozenset(s.layout.roles()),
                             y=line.top))  # fmt: skip
