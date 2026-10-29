"""Tables and side-by-side lists, read from word positions.

A header row ("Drug Name | Status | PA | QL", or "PREFERRED AGENTS | NON-PREFERRED AGENTS") sets
the columns: each header phrase starts a column, which runs to the start of the next one. Words
below are assigned to columns by their left edge. This works the same on a digital page and on
OCR word boxes, with or without ruling lines.

* **record mode** (drug, status, tier, PA, QL, ST, requirements): one drug per row. A row starts
  when the status or tier cell has text; a line without one continues the row above (wrapped
  cells).
* **list mode** (preferred / non-preferred / not covered): every line is one drug per list
  column; the column says the status. A list ends at the next header, or at the title of the
  criteria printed under it ("PA Criteria").

Class headings come from a class column, from a bold or class-named line, or, failing both,
from the drug dictionary later.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field, replace

from rapidfuzz import fuzz, process

from pdlwatch.drugs import match_class, match_product
from pdlwatch.pdf import Line, Page, Phrase, Word

# list columns, each saying the status of the drugs listed in it; "preferred_pa" is a column of
# preferred drugs that still need prior authorization ("PREFERRED WITH PA")
LIST_ROLES = ("preferred", "preferred_pa", "non_preferred", "not_covered")
_ROLE_PATTERNS: list[tuple[str, str]] = [
    ("not_covered", r"(not covered|excluded( drugs| products)?|non-covered|not on formulary)"),
    ("non_preferred", r"(non-? ?preferred|not preferred|non-? ?pdl)"
                      r"( agents| drugs| products| medications)?"
                      r"( ?/ ?requires? (prior authorization|pa)"
                      r"| (prior authorization|pa) required)?"),
    ("preferred_pa", r"preferred( agents| drugs| products| medications)?"
                     r"( with (pa|prior authorization|clinical criteria)"
                     r"| ?[-/] ?(pa|prior authorization) required)"),
    ("preferred", r"preferred( agents| drugs| products| medications)"),
    ("criteria", r"(pa criteria|clinical criteria|criteria|comments)"),
    ("class", r"(therapeutic class|(.+ )?drug (class|category)|class|category|therapeutic category"
              r"|hic3 desc(ription)?|system|body system)"),
    ("code", r"(hic3|hicl|gcn|gcn seq no|gsn|ndc)"),  # a class or product code column
    ("drug", r"(drug|drug name|drug ?/ ?product( name)?|medication|product|product name|agent"
             r"|brand name|label name)"),
    ("generic", r"(generic name|generic)"),
    ("status", r"(status|pdl status|preferred status|formulary status|preferred\?"
               r"|preferred y/n|preferred|pdl)"),
    ("tier", r"(tier|drug tier|formulary tier)"),
    ("pa", r"(pa|pa status|prior auth|prior authorization|pa required"
           r"|prior authorization required)"),
    ("ql", r"(ql|quantity limit|quantity limits|qty limit)"),
    ("st", r"(st|step therapy|step)"),
    ("req", r"(requirements/limits|requirements|coverage requirements and limits"
            r"|requirements and limits|restrictions|notes|limits)"),
]  # fmt: skip
_FOOTER = re.compile(r"^(page \d+( of \d+)?|\d+)$", re.I)
_TOP, _BOTTOM = 0.085, 0.945  # page decor (title, banner, page number) lives outside this band
# a column titled for the status ("Status"), unlike a bare "PREFERRED" over a list
_STATUS_TITLE = re.compile(r"(pdl |preferred |formulary )?status")
# the title of a column of drugs ("Generic Name Drugs", "Brand Name Drugs", "Drugs")
_DRUGS_TITLE = re.compile(r"(.+ )?(drugs|medications|products)( name)?")


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
    item_x: float | None = None  # where its items start on this page, when that is clear


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
    if not any(r == "drug" for r, _ in tagged):  # "Generic Name" is the drug when it is alone
        tagged = [("drug" if r == "generic" else r, p) for r, p in tagged]
    # one column per role: "Drug Name ... Product" and "Preferred Status ... Number of Preferred"
    # name the drug and the status in their first column; "System | Class" names the class in its
    # last, more specific one
    for one in ("drug", "status", "class"):
        at = [k for k, (rk, _) in enumerate(tagged) if rk == one]
        for k in at[:-1] if one == "class" else at[1:]:
            tagged[k] = ("other", tagged[k][1])
    # "Preferred Drugs | Status | Type" (Utah): a list's title over the drug column of a table
    # whose Status column says each drug's status
    listed = [k for k, (rk, _) in enumerate(tagged) if rk in LIST_ROLES]
    status = [
        k
        for k, (rk, p) in enumerate(tagged)
        if rk == "status" and _STATUS_TITLE.fullmatch(" ".join(p.text.lower().split()))
    ]
    if len(listed) == 1 and status and listed[0] < status[0] and all(
            rk != "drug" for rk, _ in tagged):  # fmt: skip
        tagged[listed[0]] = ("drug", tagged[listed[0]][1])
    found = [(r, p) for r, p in tagged if r and r != "other"]
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
    elif names & {"non_preferred", "not_covered", "preferred_pa"} or (
        "preferred" in {role(p.text + " agents") for _, p in found} and len(found) >= 2
    ):
        mode = "list"
    else:
        return None
    cols = []
    for r, p in tagged:
        if r in {None, "other", "code"}:  # a column this parser doesn't read: kept so its text
            cols.append(Column("other", p.x0, p.x1))  # stays out of the others
            continue
        if mode == "list" and r == "status":  # "PDL" or "PREFERRED" beside "NON-PDL"
            r = "preferred"
        cols.append(Column(r, p.x0, p.x1))
    return Layout(mode, _bounds(cols, width), width)


def _starts(lines: list[Line]) -> list[float]:
    """Where list items start: phrases that name something, bullets and markers left out."""
    out = []
    for line in lines:
        for p in line.phrases:
            words = [w for w in p.words if w.text.lstrip(_BULLETS)]
            text = " ".join(w.text for w in words).lstrip(_BULLETS)
            if len(text) >= 3 and not _MARKER_ONLY.match(text):
                out.append(words[0].x0)
    return out


def _after_bullets(lines: list[Line]) -> list[float]:
    """Where the text after each bullet starts: a bulleted list's item position."""
    out = []
    for line in lines:
        words = [w for p in line.phrases for w in p.words]
        for a, b in zip(words, words[1:], strict=False):
            if not a.text.strip(_BULLETS) and b.text.lstrip(_BULLETS):
                out.append(b.x0)
    return out


def _clusters(xs: list[float], tol: float = 3.0) -> list[tuple[float, int]]:
    """Left edges shared by several phrases: (leftmost x, how many), left to right."""
    out: list[tuple[float, int]] = []
    for x in sorted(xs):
        if out and x - out[-1][0] <= tol:
            out[-1] = (out[-1][0], out[-1][1] + 1)
        else:
            out.append((x, 1))
    return out


def refit(lay: Layout, below: list[Line], ocr: bool = False) -> Layout:
    """Fit a list's or table's columns to where their cells start on this page.

    Centred headers put cells in the wrong column: "PREFERRED" sits over the middle of a column
    whose bullets start 80 pt further left, Illinois centres "Drug Name" 120 pt to the right of
    the names under it, and some PDLs move the columns from page to page under one header. A
    column's left edge moves to the leftmost place where several cells start, in the gap before
    its header, when more start there than at the header itself. The header layout is never
    changed: each page is fitted from it afresh. Tables on scans keep their header's columns:
    OCR word positions scatter too much to move a cell edge on.
    """
    if len(lay.columns) < 2 or (lay.mode == "record" and ocr):
        return lay
    clusters = _clusters(_starts(below))
    bullets = _clusters(_after_bullets(below))  # two bulleted items are evidence enough
    cols = [Column(c.role, c.hx0, c.hx1, c.x0, c.x1) for c in lay.columns]
    for prev, c in zip(cols, cols[1:], strict=False):
        at_header = sum(n for x, n in clusters if c.x0 <= x < c.hx0 + 30)
        gap = sorted(
            [(x, n) for x, n in clusters if prev.hx1 < x < c.x0 and n >= 3]
            + [(x, n) for x, n in bullets if prev.hx1 < x < c.x0 and n >= 2]
        )
        if gap and sum(n for _, n in gap) > at_header:
            c.x0 = prev.x1 = gap[0][0] - 3
    for c in cols:
        after = [(x, n) for x, n in bullets if c.x0 <= x < c.x1 and n >= 2]
        inside = [(x, n) for x, n in clusters if c.x0 <= x < c.x1 and n >= 3]
        c.item_x = max(after, key=lambda t: t[1])[0] if after else inside[0][0] if inside else None
    return Layout(lay.mode, cols, lay.width)


def merge_lines(first: Line, second: Line, titled: bool = False) -> Line:
    """A header whose cells wrap onto a second line ("PA" / "Required", "Step T" / "herapy").

    Each phrase of the second line joins the first-line phrase whose column it falls in; a
    continuation that starts in lower case was split mid-word, so it joins without a space.
    When the first line is a header by itself (``titled``), a note in parentheses under a
    title ("(PA required)") isn't part of it, and a column title set past its end is a column
    of its own ("PREFERRED AGENTS | NON-PREFERRED AGENTS" over "(No PA required ...) | (PA
    required) | PA CRITERIA": Vermont). Below anything else, a "Non-PDL" is a cell, not a title.
    """
    starts = [p.x0 for p in first.phrases]
    joined = [list(p.words) for p in first.phrases]
    for ph in second.phrases:
        i = max((k for k, x in enumerate(starts) if x - 4 <= ph.x0), default=None)
        if i is None:
            return first
        text = ph.text.strip()
        if titled and text.startswith("(") and text.endswith(")"):
            continue
        if titled and i == len(starts) - 1 and ph.x0 > first.phrases[-1].x1 + 12 and role(text):
            joined.append(list(ph.words))
            continue
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


def _rank(layout: Layout) -> tuple[int, int]:
    """Which of two readings of a header is better: more columns known, then more different."""
    return _known(layout), len(layout.roles())


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
    bullet: bool = False  # the item starts with a bullet: a new item, never a wrapped line
    x: float = 0.0  # where its text starts
    sub: bool = False  # a second-level bullet ("o"): a note on the item above, or a sub-item


_BULLETS = "•●▪◦‣■□➢►"
_GLYPHS = "®™©"  # superscripts PDFs often set apart, sometimes on a line of their own


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
    open_note: str | None = None  # the cell a wrapped note line goes to ("ql", "st", "req")


def _cells(line: Line, layout: Layout) -> dict[str, Cell]:
    words: dict[str, list[Word]] = {}
    bulleted: set[str] = set()
    pending = False  # a bullet on its own: the next word starts an item
    for p in line.phrases:
        drop_number = False
        for w in p.words:
            text = w.text.lstrip(_BULLETS)
            if not text:
                pending = True
                continue
            if not text.strip(_GLYPHS):
                drop_number = True  # "Taltz ® 1": the ® and the footnote number after it
                continue
            if drop_number and re.fullmatch(r"\d{1,2}", text):
                continue
            drop_number = False
            r = layout.column(w.x0).role
            if pending or text != w.text:
                bulleted.add(r)
                pending = False
            words.setdefault(r, []).append(Word(text, w.x0, w.x1, w.top, w.bottom, w.bold, w.conf))
    out = {}
    for r, ws in words.items():
        sub = len(ws) > 1 and ws[0].text == "o"  # "o" as a second-level bullet
        if sub:
            ws = ws[1:]
        out[r] = Cell(" ".join(w.text for w in ws), min(w.conf for w in ws), r in bulleted,
                      ws[0].x0, sub)  # fmt: skip
    return out


def _band(line: Line, page: Page) -> str | None:
    """Where a line sits among the page's furniture: "number" (a page number), "top" (the title
    band), "bottom" (the footer band), or None (the body). A number alone on its line in the
    body is a page number only when it is this page's (a tier "1" set apart is a cell)."""
    text = line.text.strip()
    bottom, top = line.bottom > _BOTTOM * page.height, line.top < _TOP * page.height
    if _FOOTER.match(text) and (bottom or top or not text.isdigit()
                                or abs(int(text) - page.number) <= 2):  # fmt: skip
        return "number"
    if bottom:
        return "bottom"
    if top:
        return "top"
    return None


_STATUS_WORDS = re.compile(r"\b(preferred|non preferred|not covered|excluded|pdl|non pdl)\b")


def _status_like(text: str) -> bool:
    """A status or tier value ("NON_PREFERRED", "Preferred with PA", "T2", "NP", "X"), not a
    sentence that happens to cross the status column."""
    t = " ".join(re.sub(r"[^a-z0-9 ]", " ", text.lower().replace("_", " ")).split())
    if not t or len(t.split()) > 4:
        return False
    return bool(_STATUS_WORDS.search(t) or re.fullmatch(r"(t|tier ?)?\d|[a-z]{1,3}", t))


def _band_row(line: Line, layout: Layout | None) -> bool:
    """A table row printed in the title or footer band: a drug with a status value beside it,
    in a record table."""
    if layout is None or layout.mode != "record":
        return False
    cells = _cells(line, layout)
    keys = ("status", "tier", *LIST_ROLES)
    return "drug" in cells and any(_status_like(cells[r].text) for r in keys if r in cells)


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


def _status_value(line: Line, new: Layout, current: Layout) -> bool:
    """A lone status word ("Non-PDL", "Preferred") inside a record table."""
    return (current.mode == "record" and new.mode == "list" and len(line.phrases) == 1
            and len(line.text.split()) <= 2
            and role(line.text) in {"preferred", "non_preferred", "status"})  # fmt: skip


def _foreign_header(line: Line, layout: Layout | None = None) -> bool:
    """A header row of another kind of table: it starts with a drug or class column ("Drug
    Name", "Drug / Class Name") and the rest are column titles this parser has no role for
    ("Step Therapy (ST) Parameters", "Dose Optimization Limitations"). Inside a record table, also
    titles over columns of drugs, naming a column this parser knows but no status, tier or list
    ("Generic Name Drugs | Brand Name Drugs | Updated | Limits": Utah's lists after its PDL)."""
    if len(line.phrases) < 2:
        return False
    first, rest = line.phrases[0].text, line.phrases[1:]
    titles = all(len(p.text.split()) <= 5 and p.text[:1].isupper()
                 and match_product(p.text).product is None for p in rest)  # fmt: skip
    named = role(first) in {"drug", "class"} or bool(
        re.fullmatch(r"(drug|medication|product)( ?/ ?class)?( name)?", first.lower().strip())
    )
    if named:
        return titles and all(role(p.text) is None for p in rest)
    roles = [role(p.text) for p in line.phrases]
    return (
        layout is not None
        and layout.mode == "record"
        and len(rest) >= 2
        and bool(_DRUGS_TITLE.fullmatch(" ".join(first.lower().split())))
        and any(roles[1:])
        and not set(roles) & {"status", "tier", *LIST_ROLES}
        and titles
    )


# the title of the criteria a list's class prints under it ("PA Criteria", "Clinical Criteria:")
_CRITERIA_TITLE = re.compile(r"(pa|clinical) criteria\b", re.I)


def _criteria_title(line: Line, layout: Layout | None) -> bool:
    """A line of its own, left of a list's columns, titling the criteria printed under the list
    (Vermont): what follows is criteria, neither drugs nor their notes, until the next header."""
    return (
        layout is not None
        and layout.mode == "list"
        and len(line.phrases) == 1
        and bool(_CRITERIA_TITLE.match(line.text.strip()))
        and line.x0 < min(c.hx0 for c in layout.columns) - 2
    )


def _table_end(lines: list[Line], heads: list[Layout | None], start: int,
               layout: Layout | None) -> int:  # fmt: skip
    """Where a table's lines stop on a page: at the next header, or at the title of the criteria
    printed under a list, whose paragraphs would otherwise move where the list's items start."""
    return next((k for k in range(start, len(lines))
                 if heads[k] or _criteria_title(lines[k], layout)), len(lines))  # fmt: skip


def _spanning(line: Line, layout: Layout) -> str | None:
    """A title set across list columns ("Antipsychotics – Second Generation" centred over
    the page) is a class heading, not an item: no list item crosses into the next column."""
    if layout.mode != "list" or len(line.phrases) != 1:
        return None
    p = line.phrases[0]
    text = p.text.strip()
    if text[:1] == "(" or text[:1].islower():
        return None  # the rest of a wrapped name
    if any(abs(p.x0 - c.item_x) <= 4 for c in layout.columns if c.item_x is not None):
        return None  # starts where items start: a long name running over, not a title
    edges = [c.x0 for c in layout.columns[1:]]
    if any(p.x0 + 4 < e < p.x1 - 4 for e in edges) and match_product(text).score < 100:
        return text
    return None


@dataclass
class _State:
    layout: Layout | None = None
    base: Layout | None = None  # the layout the last header defined, before fitting to a page
    last_list: Layout | None = None
    bulleted: set[tuple[int, str]] = field(default_factory=set)  # (page, column) using bullets
    block: int = 0  # index of the first row under the latest header: nothing above continues
    drug_class: str | None = None
    rows: list[RawRow] = field(default_factory=list)
    pages: set[int] = field(default_factory=set)
    # record tables: lines waiting for the row that starts below them, and the last line read
    carry: list[tuple[Line, dict[str, Cell]]] = field(default_factory=list)
    prev_top: float | None = None
    prev_page: int | None = None


Trace = dict[int, list[tuple[Line, "Layout | None", str]]]


def _with_next(s: _State, page: Page, lines: list[Line], heads: list[Layout | None], i: int,
               cells: dict[str, Cell]) -> str | None:  # fmt: skip
    """A record table's line with no drug name that belongs with the line close below it:
    "header", the top line of a header that wraps upward ("Last" / "Required Prior Authorization"
    over "Non Preferred Drugs | Status"), when the header is nearer than the line above; "carry",
    the first line of a cell centred on the row below ("30 Units / 15 Days" half above
    "Journavx"), when that row is at most half as far as the line above. Not on scans, whose word
    positions scatter too much to tell."""
    line = lines[i]
    if (page.ocr or "drug" in cells or i + 1 >= len(lines) or s.layout is None
            or s.prev_top is None or s.prev_page != page.number):  # fmt: skip
        return None
    gap, above = lines[i + 1].top - line.top, line.top - s.prev_top
    if not 0 < gap <= 1.5 * (line.bottom - line.top):
        return None
    if heads[i + 1] is not None:
        return "header" if gap < above else None
    below = _cells(lines[i + 1], s.layout)
    if (2 * gap <= above and "drug" in below and _row_starts(below, s.layout)
            and not cells.keys() & {"status", "tier", *LIST_ROLES}):  # fmt: skip
        s.carry.append((line, cells))
        return "carry"
    return None


def _side_by_side(line: Line) -> float | None:
    """Where the second of two tables printed side by side under one header line starts
    ("Drug Name | Tier | Restrictions | Drug Name | Tier | Restrictions"), if this is one."""
    roles = [role(p.text) for p in line.phrases]
    half = len(roles) // 2
    if len(roles) < 4 or len(roles) % 2 or roles[:half] != roles[half:] or "drug" not in roles:
        return None
    return line.phrases[half].x0 - 2


def _sides(page: Page) -> list[Page]:
    """A page holding two tables side by side, as two pages read one after the other (left,
    then right); any other page as it is."""
    split = next((x for ln in page.lines if (x := _side_by_side(ln)) is not None), None)
    if split is None:
        return [page]
    halves = []
    for keep in (lambda x: x < split, lambda x: x >= split):
        lines = [Line(ph) for ln in page.lines
                 if (ph := [p for p in ln.phrases if keep(p.x0)])]  # fmt: skip
        halves.append(Page(page.number, page.width, page.height, lines, page.ocr, page.image,
                           page.ocr_failed))  # fmt: skip
    return halves


def parse(pages: list[Page], trace: Trace | None = None) -> tuple[list[RawRow], set[int]]:
    """Every table or list row in the document, and the pages they came from.

    ``trace`` (for ``pdlwatch inspect``) collects, per page, each line with the layout in force
    and what the line was taken for: header, heading, row or skipped.
    """
    s = _State()
    pages = [half for page in pages for half in _sides(page)]

    def note(page: Page, line: Line, what: str) -> None:
        if trace is not None:
            trace.setdefault(page.number, []).append((line, s.layout, what))

    def decor(page: Page, line: Line) -> None:
        if trace is not None:
            trace.setdefault(page.number, []).append((line, None, "decor"))

    for page in pages:
        # page furniture is skipped, except a table header printed in the title band on every
        # page (Indiana, Illinois) and the rows under it, and table rows that run into a band
        lines, in_band = [], []
        band_header = False
        for ln in page.lines:
            where = _band(ln, page)
            if where == "top" and not band_header:
                band_lay = header(ln, page.width)
                band_header = band_lay is not None and _known(band_lay) >= 2
            if where == "number":
                decor(page, ln)
                continue
            lines.append(ln)
            in_band.append(where == "bottom" or (where == "top" and not band_header))
        heads = [header(ln, page.width) for ln in lines]
        on_page = False
        skip_next = False
        if s.base is not None:  # the last header's layout, fitted to this page's own lines
            first = _table_end(lines, heads, 0, s.base)
            s.layout = refit(s.base, lines[:first], page.ocr)
        for i, line in enumerate(lines):
            if skip_next:
                skip_next = False
                note(page, line, "header")
                continue
            if in_band[i] and not _band_row(line, s.layout):
                decor(page, line)
                continue
            lay = heads[i]
            if i + 1 < len(lines) and line.bottom + 6 > lines[i + 1].top:
                # a header whose cells wrap: the two lines together name more columns, or tell
                # two apart ("PREFERRED" / "PREFERRED" + "WITH PA" below it)
                merged = header(merge_lines(line, lines[i + 1], lay is not None), page.width)
                if merged is not None and (lay is None or _rank(merged) > _rank(lay)):
                    lay, skip_next = merged, True
            if lay is not None and s.layout is not None and _status_value(line, lay, s.layout):
                lay, skip_next = None, False  # "Non-PDL" split off its table row: a cell
            if lay is not None:
                if s.last_list is not None:
                    lay = _merge(lay, s.last_list)
                if lay.mode == "list" and lay.roles() & {"preferred", "non_preferred"}:
                    s.last_list = lay  # a NOT COVERED block isn't a template for the next class
                start = i + (2 if skip_next else 1)
                stop = _table_end(lines, heads, start, lay)
                s.base, s.block = lay, len(s.rows)
                s.layout, on_page = refit(lay, lines[start:stop], page.ocr), True
                s.carry, s.prev_top, s.prev_page = [], line.top, page.number
                s.pages.add(page.number)
                note(page, line, "header")
                continue
            if s.layout is not None and _foreign_header(line, s.layout):
                # the header of a table this parser doesn't read ("Brand Name | Dose
                # Optimization Limitations"): its rows aren't the list above
                s.layout = s.base = None
                note(page, line, "other table")
                continue
            if _criteria_title(line, s.layout):  # the criteria under the list: the list ends
                s.layout = s.base = None
                note(page, line, "criteria")
                continue
            if s.layout is None:  # before the first table: title, legend, notes
                note(page, line, "before the first header")
                nxt = heads[i + 1] if i + 1 < len(lines) else None
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
            nxt = heads[i + 1] if i + 1 < len(lines) else None
            heading = _heading(line, cells, s.layout)
            spanning = _spanning(line, s.layout)
            if spanning or heading and (nxt is not None or s.layout.mode == "record"
                                        or match_class(heading)):  # fmt: skip
                heading = spanning or heading
                s.drug_class = heading
                note(page, line, "class heading")
                continue
            record = s.layout.mode == "record"
            with_next = _with_next(s, page, lines, heads, i, cells) if record else None
            if with_next == "header":
                note(page, line, "header")
                continue
            note(page, line, "row")
            if not record:
                _list_line(s, page, line, cells)
                continue
            if with_next is None:  # a carried line joins the row below it
                _record_line(s, page, line, cells)
            s.prev_top, s.prev_page = line.top, page.number
    for seen in (trace or {}).values():
        seen.sort(key=lambda t: t[0].top)  # decor lines in their place on the page
    return s.rows, s.pages


_LISTED = {"preferred": "preferred", "preferred_pa": "preferred", "non_preferred": "non-preferred",
           "not_covered": "not covered"}  # fmt: skip


def _row_starts(cells: dict[str, Cell], layout: Layout) -> bool:
    """Does this line of a record table start a row? Text in its status or tier cell does, or in
    a column per status ("... | PREFERRED | NON-PREFERRED", a mark or the status written out); in
    a table with neither, a drug name does."""
    keys = layout.roles() & {"status", "tier"}
    listed = [r for r in LIST_ROLES if r in cells]
    return any(r in cells for r in keys) or bool(listed) if keys or listed else "drug" in cells


def _record_line(s: _State, page: Page, line: Line, cells: dict[str, Cell]) -> None:
    assert s.layout is not None
    carried, s.carry = s.carry, []
    for _, above in carried:  # a cell centred on this row: its first lines sit above it
        for r, c in above.items():
            old = cells.get(r)
            cells[r] = (
                replace(old, text=f"{c.text} {old.text}", conf=min(c.conf, old.conf)) if old else c
            )
    evidence = " / ".join([line.text, *(ln.text for ln, _ in carried)])
    keys = s.layout.roles() & {"status", "tier"}
    listed = [r for r in LIST_ROLES if r in cells]
    starts = _row_starts(cells, s.layout)
    reread = False
    # the row above on this page and under this header: nothing continues across either
    last = s.rows[-1] if len(s.rows) > s.block and s.rows[-1].page == page.number else None
    first = len(s.rows) == s.block and not page.ocr  # the first name under a header on a page
    if not starts and keys and "drug" in cells and (_whole_drug(cells["drug"].text) or first):
        # a drug on its own with an empty status: re-read the cell (OCR drops short tokens
        # like "T2"), else start the row anyway and let review fill the status in; the first
        # name under a header can't continue anything, so it starts a row too (its tier may sit
        # beside its next line)
        for k in keys:
            col = next(c for c in s.layout.columns if c.role == k)
            text = page.ocr_region(col.x0, line.top - 2, col.x1, line.bottom + 2)
            if text:
                cells[k] = Cell(text, 50.0)
                reread = True
        starts = True
    if (
        starts
        and "drug" in cells
        and last is not None
        and keys
        and not page.ocr
        and not any(last.cells.get(k) for k in keys)
        and _wrapped_part(cells["drug"].text)
    ):
        # the name wrapped and its tier or status sits beside the second line, as cells
        # centred vertically put it ("ibuprofen oral tablet" / "400 mg, 600 mg, 800 | 1")
        for r, c in cells.items():
            last.cells[r] = (last.cells.get(r, "") + " " + c.text).strip()
        last.evidence += " / " + evidence
        last.lines += 1
        return
    if starts and "drug" in cells:
        raw = {r: c.text for r, c in cells.items()}
        if listed and "status" not in raw and "tier" not in raw:
            raw["status"] = _LISTED[listed[0]]
            if listed[0] == "preferred_pa":
                raw.setdefault("pa", "yes")
        if raw.get("class"):
            s.drug_class = raw["class"]
        s.rows.append(RawRow(page.number, "table", raw, s.drug_class, None, page.ocr,
                             min(c.conf for c in cells.values()), evidence,
                             frozenset(s.layout.roles()), reread=reread, y=line.top))  # fmt: skip
        return
    if last is not None:  # a wrapped cell continues the last row
        for r, c in cells.items():
            last.cells[r] = (last.cells.get(r, "") + " " + c.text).strip()
            if r == "class":
                s.drug_class = last.drug_class = last.cells["class"]
        last.evidence += " / " + evidence
        last.lines += 1


def _wrapped_part(text: str) -> bool:
    """The rest of a name that wrapped: a strength, a dose form or a lower-case word, never
    the start of another product."""
    t = text.strip()
    return bool(t) and (
        t[0].isdigit()
        or t[0] == "("
        or bool(_FORM_ONLY.match(t))
        or (t[0].islower() and not _whole_drug(t))
    )


def _continues(prev: str, text: str) -> bool:
    """Does ``text`` continue the list item above it rather than start a new one?

    Only on evidence of a wrap: a marker or a parenthesis on its own, an unclosed "(", a name
    broken after "/", "-" or "biosimilar to", or a dosage form left over ("inhaler", "pen
    injector"). A line that merely starts in lower case is usually a generic on its own line
    ("metformin ER").
    """
    if _MARKER_ONLY.match(text) or text.startswith("("):
        return True
    if prev.rstrip().endswith(("/", "-", ",", "&")) or _DANGLING.search(prev):
        return True  # "insulin glargine-" / "yfgn", "... biosimilar to" / "Humira"
    if prev.count("(") > prev.count(")"):
        # "Genvoya (elvitegravir/cobicistat/..." waiting for its ")": the rest of the name is in
        # lower case or brings the ")"; "Nicotine Gum" on the next line is the next drug
        return ")" in text or text[:1].islower()
    return bool(text[:1].islower() and _FORM_ONLY.match(text))


_DANGLING = re.compile(r"\b(biosimilar|compare|generic|gen|equivalent)( (to|of|for))?\s*$", re.I)
# a second-level note: "o ST – must have tried ...", "o QL – 2 tabs/day", "o AGE – 18 and older"
_NOTE = re.compile(r"^(?:[A-Z][A-Za-z]+ )?(ST|QL|PA|AGE|AL)\s*[–—-]", re.I)
_NOTE_CELL = {"st": "st", "ql": "ql", "pa": "pa", "age": "req", "al": "req"}
_REQUIRE = re.compile(r"\b(require[sd]?|not required)\b", re.I)

_FORM_ONLY = re.compile(
    r"^((pen|auto-?)?injectors?|pens?|inhalers?|inhalation|patch(es)?|spray|kit|vials?|"
    r"tablets?|capsules?|solution|suspension|powder|film|gel|cream|syringes?|cartridges?|"
    r"chewable|sprinkle|oral|nasal|topical|er|xr|sr|dr|odt)( \w+)?$",
    re.I,
)


# one or more footnote markers on their own ("QL", "CC QL", "†")
_MARKER_ONLY = re.compile(r"^(([*†‡§#^]+|cc|ql|st|pa|bvg|\(brand preferred\))\s*)+$", re.I)


# words drug names don't contain but criteria and notes do
_PROSE = """
a an and are as at be been by can cannot could does during each either for from has have if in
into is it its may more must no not of on once only or other otherwise per prior provide should
than that the their then there these this those through to unless until use used using was were
when where which while who will with within without after before approval approved required
requires requirement trial failure failed patients patient members member years older younger
diagnosis documented documentation noted except least
"""
_PROSE_WORDS = frozenset(_PROSE.split())
_QL_NOTE = re.compile(r"^(qty|quantity)\s*limits?\b|^limits?\s*:|^ql\s*[:=]|^max(imum)?\b", re.I)
_MAX_ITEM_WORDS = 16  # a wrapped drug name is never longer than this


def _prose(text: str) -> bool:
    """Criteria or a note in a list column rather than a drug: judged on the words outside
    parentheses ("Varenicline (Limited to 18 years and older)" is a drug), or inside them for a
    line that is all parenthesis ("(No PA required unless otherwise noted")."""
    outside = re.sub(r"\([^()]*(\)|$)", " ", text)
    if not outside.strip():
        outside = text.strip("() ")
    words = re.findall(r"[a-z]+", outside.lower())
    hits = sum(w in _PROSE_WORDS for w in words)
    return hits >= 3 or (hits >= 2 and len(words) >= 5) or len(words) > 14


def _whole_drug(text: str) -> bool:
    """A cell that is a drug name by itself, not the second line of a wrapped one."""
    return not text.startswith("(") and match_product(text).score == 100


def _note_open(row: RawRow) -> bool:
    """The row's last line was a note (a quantity limit, an ST or age rule), which may wrap."""
    return row.open_note is not None


def _list_line(s: _State, page: Page, line: Line, cells: dict[str, Cell]) -> None:
    if any(role(cells[r].text) in {"criteria", *LIST_ROLES} for r in LIST_ROLES if r in cells):
        return  # a header OCR mangled ("DONT _PA CRITERIA")
    assert s.layout is not None
    for r in LIST_ROLES:
        c = cells.get(r)
        if c is None or not c.text.strip():
            continue
        text = c.text.strip()
        marker = bool(_MARKER_ONLY.match(text))  # "QL" alone is a flag, not a "QL" header
        if not marker and (role(text) is not None or len(text) < 3):
            continue  # a stray header word or an OCR speck
        prev = next((x for x in reversed(s.rows[s.block :]) if x.list_role == r), None)
        here = prev is not None and prev.page == page.number
        if c.bullet:
            s.bulleted.add((page.number, r))
        col = next(k for k in s.layout.columns if k.role == r)
        # indented under the item above: a wrap. Not on scans, where OCR specks land anywhere
        indented = (not page.ocr and col.item_x is not None and c.x >= col.item_x + 6
                    and not c.bullet)  # fmt: skip
        if c.sub and _NOTE.match(text) or _QL_NOTE.match(text):
            if here and prev is not None:  # "o ST – must have tried metformin", "QTY LIMIT: 2/day"
                m = _NOTE.match(text)
                cell = _NOTE_CELL[m.group(1).lower()] if m else "ql"
                _add_note(prev, cell, text)
            continue
        if (
            here
            and prev is not None
            and prev.open_note
            and not c.bullet
            and not c.sub
            and (indented or text[:1].islower() or text[:1].isdigit() or text[:1] == "(")
        ):
            _add_note(prev, prev.open_note, text)  # a note that wraps onto the next line
            continue
        if text.endswith((".", ":")) and (len(text.split()) >= 3 or text[:1].islower()
                                          or text.endswith(":")):  # fmt: skip
            continue  # criteria prose or a sub-heading in a list column ("program.", "Note:")
        outside = re.sub(r"\([^()]*(\)|$)", " ", text)
        if _prose(text) or _REQUIRE.search(outside if outside.strip() else text):
            continue  # "Xarelto or Eliquis cannot be used...", "(No PA required unless noted"
        if re.search(r"criteria|\b(non-?)?preferred\b", text, re.I) and not re.search(
            r"\(brand preferred\)$", text, re.I
        ):
            continue  # header words OCR mangled ("DONT _PA CRITERIA")
        in_bullets = (page.number, r) in s.bulleted and not c.bullet and not c.sub
        if (
            here
            and prev is not None
            and in_bullets
            and col.item_x is not None
            and c.x < col.item_x - 8
        ):
            continue  # a label at the bullets' position ("DPP4-I & metformin combination")
        if (
            here
            and prev is not None
            and not c.bullet
            and not c.sub
            and (in_bullets or indented or _continues(prev.cells["drug"], text))
            and len((prev.cells["drug"] + " " + text).split()) <= _MAX_ITEM_WORDS
        ):
            # a wrapped name ("glargine-" / "yfgn" joins without a space), or a marker
            sep = "" if prev.cells["drug"].rstrip().endswith(("-", "/")) else " "
            prev.cells["drug"] = prev.cells["drug"].rstrip() + sep + text
            prev.evidence += " / " + text
            continue
        if marker or in_bullets:
            continue
        s.rows.append(RawRow(page.number, "list", {"drug": text}, s.drug_class, r, page.ocr,
                             c.conf, line.text, frozenset(s.layout.roles()),
                             y=line.top))  # fmt: skip


def _add_note(row: RawRow, cell: str, text: str) -> None:
    row.cells[cell] = (row.cells.get(cell, "") + " " + text).strip()
    row.evidence += " / " + text
    row.open_note = cell
