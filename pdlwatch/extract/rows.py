"""From raw cells to canonical formulary rows, with a confidence score and a reason for review.

Canonical fields:

* ``status``: preferred | non_preferred | not_covered (Medicaid's non-preferred means PA)
* ``tier``: the document's tier number, when it uses tiers
* ``pa``, ``st``, ``ql``, ``bvg``: True / False, or None when the document has no column, code
  or marker for that field (then it is unknown, not false)

Confidence starts at 1 and drops for anything that makes a row less certain: a drug name not in
the dictionary, a fuzzy (OCR) match, an unreadable status, a conflict with another row for the
same drug, prose rather than a table. Rows below the acceptance threshold go to review.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from typing import Any

from pdlwatch.drugs import match_class, match_product, normalise, rxcui
from pdlwatch.extract.layout import LIST_ROLES, RawRow
from pdlwatch.extract.legend import DEFAULT_STATUS, Legend

ACCEPT = 0.75  # rows at or above this confidence are accepted without review

_YES = {"y", "yes", "x", "✓", "✔", "r", "required", "true", "pa", "st", "ql"}
_NO = {"", "n", "no", "-", "–", "none", "false", "n/a"}
_QUANTITY = re.compile(
    r"\d+(\.\d+)?\s*(mg\s+)?per\s+(\d+\s+)?(days|day|weeks|week|months|month)", re.I
)


@dataclass
class Row:
    doc_id: str
    state: str | None
    page: int
    method: str  # table | list | prose | llm
    drug_text: str
    drug_class: str | None
    product: str | None
    ingredient: str | None
    kind: str | None
    match_score: float
    status: str | None
    tier: int | None = None
    pa: bool | None = None
    st: bool | None = None
    ql: bool | None = None
    ql_detail: str | None = None
    bvg: bool | None = None
    ocr: bool = False
    ocr_conf: float = 100.0
    evidence: str = ""
    confidence: float = 1.0
    review_reasons: list[str] = field(default_factory=list)
    accepted: bool = True
    y: float | None = None  # top of the source line on the page (tables and lists)
    rxcui: str | None = None  # RxNorm concept id of the product, when the lexicon is loaded

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["review_reasons"] = "; ".join(self.review_reasons)
        return d


def _flag(text: str | None) -> bool | None:
    """A Yes / No cell. None when it can't be read: a scan's "Ne", "bes" or "[" is a guess
    either way, so it goes to review instead."""
    if text is None:
        return None
    t = text.strip().lower()
    if t in _YES:
        return True
    if t in _NO:
        return False
    core = re.sub(r"[^a-z0-9/ ]", "", t).strip()  # "_No", "Yes*"
    if core in _YES - {""} or core in _NO - {""}:
        return core in _YES
    if re.search(r"\d|\b(pa|st|ql|prior auth|step therapy|quantity limit)", t):
        return True  # a limit or a code written in the cell ("4 per 28 days")
    return None


def _requirements(text: str) -> dict[str, Any]:
    """PA / ST / QL codes and a quantity-limit detail from a requirements cell."""
    out: dict[str, Any] = {}
    if re.search(r"\bPA\b|prior auth", text, re.I):
        out["pa"] = True
    if re.search(r"\bST\b|step therapy", text, re.I):
        out["st"] = True
    m = re.search(r"\bQL\b\s*(?:\(([^)]*)\))?", text)
    q = _QUANTITY.search(text)
    if m or q or re.search(r"quantity limit", text, re.I):
        out["ql"] = True
        detail = (m.group(1) if m and m.group(1) else None) or (q.group(0) if q else None)
        if detail:
            out["ql_detail"] = detail.strip()
    return out


_SYMBOLS = "*†‡§#^"


# codes common enough to read without a legend
_DEFAULT_MARKERS = {"bvg": "bvg", "cc": "pa", "pa": "pa", "ql": "ql", "st": "st"}


def _symbols(run: str, markers: dict[str, str]) -> set[str]:
    """What a run of footnote symbols means: "**" as a whole when the legend defines it, else
    each different symbol on its own ("*†"). An undefined "**" is not read as "*"."""
    if run in markers:
        return {markers[run]}
    if len(set(run)) == 1:
        return set()
    return {markers[c] for c in run if c in markers}


_SYMBOL_RUN = re.compile(rf"(?<!\S)[{re.escape(_SYMBOLS)}]+(?!\S)")


def undefined_symbols(text: str, legend: Legend) -> set[str]:
    """Footnote symbols in a name that the document's legend doesn't define: what they mean
    (a PA, a limit, a carve-out) is unknown, so the row's flags can't be trusted."""
    markers = {**legend.notes, **legend.markers()}
    out: set[str] = set()
    for run in re.findall(rf"[{re.escape(_SYMBOLS)}]+", text):
        if run in markers:
            continue
        if len(set(run)) == 1:
            out.add(run)  # "**" when only "*" is defined: not read as "*"
        else:
            out |= {c for c in run if c not in markers}  # "*†": each on its own
    return out


def strip_markers(text: str, legend: Legend) -> tuple[str, set[str]]:
    """'Ozempic CC QL' -> ('Ozempic', {'pa', 'ql'}) using the document's legend; footnote symbols
    are read wherever they stand ("codeine sulfate * TABLET", "sumatriptan**")."""
    flags: set[str] = set()
    t = re.sub(r"(?<=\w),\s*(?=[a-z/]{1,5}(,|\s*$))", " ", text.strip(), flags=re.I)  # "cc,ql"
    markers = dict(sorted({**_DEFAULT_MARKERS, **legend.markers()}.items(),
                          key=lambda kv: -len(kv[0])))  # fmt: skip
    for run in _SYMBOL_RUN.findall(t):  # a symbol set apart inside the name, before its form
        flags |= _symbols(run, markers)
    t = " ".join(_SYMBOL_RUN.sub(" ", t).split())
    changed = True
    while changed and t:
        changed = False
        for code, flag in markers.items():
            if code[0] in _SYMBOLS:
                continue
            if t.lower().endswith(" " + code) or t.lower() == code:
                t = t[: len(t) - len(code)].strip()
                flags.add(flag)
                changed = True
        run = re.search(rf"[{re.escape(_SYMBOLS)}]+$", t)  # stuck to the name: "Ozempic*"
        if run:
            flags |= _symbols(run.group(0), markers)
            t = t[: run.start()].strip()
            changed = True
    return t, flags


_DAGGERS = "tTfY+y"  # how Tesseract tends to read † and ‡ stuck to a name


def _ocr_marker(text: str, legend: Legend, cls: str | None) -> tuple[str, set[str]]:
    """'Zituviot' -> ('Zituvio', {'ql'}): a scan turned a dagger into letters.

    One dagger-like letter after a complete drug name is a single dagger, two are a double
    dagger. The role comes from the legend; when OCR lost the legend's symbol too, from the
    marker the legend lost.
    """
    flags: set[str] = set()
    if match_product(text, cls).score == 100:
        return text, flags
    for n in (2, 1):
        if len(text) <= n + 3 or not all(c in _DAGGERS for c in text[-n:]):
            continue
        bare = text[:-n].rstrip()
        if match_product(bare, cls).score < 96:  # exact, or an OCR-folded exact match
            continue
        symbol = "†" if n == 1 else "‡"
        role = legend.codes.get(symbol)
        if n == 1:
            role = role or legend.codes.get("+") or legend.codes.get("t")
        if role is None and len(legend.lost) == 1:
            role = next(iter(legend.lost))
        if role in {"pa", "ql", "st", "bvg"}:
            flags.add(role)
        return bare, flags
    return text, flags


def from_raw(r: RawRow, legend: Legend, doc_id: str, state: str | None,
             default_status: str | None = None) -> Row:  # fmt: skip
    """A canonical row. ``default_status``: the status of every row in a table without a status
    column, when the document says it (a preferred drug list that lists only preferred drugs)."""
    cells = r.cells
    text, markers = strip_markers(cells.get("drug", ""), legend)
    cls = match_class(r.drug_class) if r.drug_class else None
    ocr_symbols = False
    if r.ocr:
        before = text
        text, more = _ocr_marker(text, legend, cls)
        markers |= more
        ocr_symbols = text != before or any(s in cells.get("drug", "") for s in "*†‡+")
    m = match_product(text, cls)
    if m.product is not None:  # a missed class heading must not mislabel the rows below it
        cls = m.product.drug_class
    row = Row(doc_id, state, r.page, r.method, text, cls or r.drug_class,
              m.product.name if m.product else None, m.product.ingredient if m.product else None,
              m.product.kind if m.product else None, m.score, None, ocr=r.ocr, ocr_conf=r.conf,
              evidence=r.evidence, y=round(r.y, 1),
              rxcui=(m.product.rxcui or rxcui(m.product.name)) if m.product else None)  # fmt: skip
    roles = r.roles
    req = _requirements(cells.get("req", ""))
    status_pa = False  # "PREFERRED_WITH_PA", "Preferred with PA" written in the status cell
    if r.list_role:
        row.status = "preferred" if r.list_role == "preferred_pa" else r.list_role
        status_pa = r.list_role == "preferred_pa"
    elif "tier" in cells:
        row.tier, row.status = legend.tier(cells["tier"])
    elif "status" in cells:
        row.status = legend.status(cells["status"])
        status_pa = bool(re.search(r"\bwith (pa|prior auth)", cells["status"].replace("_", " "),
                                   re.I))  # fmt: skip
        code = cells["status"].strip().lower()
        defined = {c for c, role in legend.codes.items() if role in DEFAULT_STATUS}
        if r.ocr and defined and len(code) <= 4 and code not in defined:
            row.review_reasons.append("status code not in the legend")  # "NF" read for "NP"
    elif default_status and not roles & {"status", "tier", *LIST_ROLES}:
        row.status = default_status
        row.evidence += " / status: the list names only preferred drugs"
    if r.reread:
        row.review_reasons.append("status cell re-read from the scan")
    # a field is known when the table has a column for it, a requirements column, or a marker
    marks = set(legend.markers().values())
    unreadable = []
    for f in ("pa", "st", "ql", "bvg"):
        known = f in roles or ("req" in roles and f != "bvg") or f in marks
        value: bool | None = None
        if f in cells:
            value = _flag(cells[f])
            if value is None:
                unreadable.append(f)
        if req.get(f) or f in markers or (f == "pa" and status_pa):
            value = True
        if value is None and known and f not in unreadable:
            value = False
        setattr(row, f, value)
    if "ql" in cells and row.ql and re.search(r"\d", cells["ql"]):
        row.ql_detail = cells["ql"].strip()
    row.ql_detail = row.ql_detail or req.get("ql_detail")
    loose = _requirements(cells.get("other", ""))
    if any(loose.get(f) for f in ("pa", "st", "ql")):
        # PA / ST / QL under a header that wasn't recognised: the flags are real, but whether
        # the column holds every flag for this drug is a guess
        for f in ("pa", "st", "ql"):
            if loose.get(f):
                setattr(row, f, True)
        row.ql_detail = row.ql_detail or loose.get("ql_detail")
        row.review_reasons.append("flags in a column without a known header")
    if not r.ocr and undefined_symbols(cells.get("drug", ""), legend):
        row.review_reasons.append("footnote symbol not in the legend")
    if ocr_symbols and legend.markers():
        # OCR turns daggers into letters ("Zepatiertt") and can't tell † from ‡ reliably
        row.review_reasons.append("footnote symbol read by OCR")
    if row.tier is not None and legend.tiers and row.tier not in legend.tiers:
        row.review_reasons.append(f"tier {row.tier} is not in the legend")
    for f in legend.lost if r.ocr else ():  # the scan lost this marker's symbol
        if not getattr(row, f):
            setattr(row, f, None)
            row.review_reasons.append(f"{f.upper()} marker unreadable in the scan")
    if m.product is not None and m.score < 100:
        # the name found inside a longer cell: what's left may be another product ("Humalog
        # Mix 75/25" is not Humalog) or, on a scan, a marker OCR garbled ("Nicotrol ... aL")
        bare = normalise(re.sub(r"\([^)]*\)", " ", text))
        name = normalise(m.product.name)
        if name and name in bare and bare.replace(name, " ", 1).strip():
            row.review_reasons.append(
                "text beside the name unreadable in the scan"
                if r.ocr
                else "words beside the name the dictionary doesn't know"
            )
    if row.status == "non_preferred":
        row.pa = True  # Medicaid: non-preferred drugs need prior authorisation
    if row.status == "not_covered":
        row.pa = row.st = False
    for f in unreadable:
        if getattr(row, f) is None:
            row.review_reasons.append(f"{f.upper()} cell unreadable")
    return row


def score(rows: list[Row], accept: float = ACCEPT) -> list[Row]:
    """Confidence and review reasons; conflicting rows for one product in one document."""
    by_product: dict[str, list[Row]] = {}
    for r in rows:
        if r.product:
            by_product.setdefault(r.product, []).append(r)
    for r in rows:
        c = 1.0
        if r.product is None:
            c -= 0.6
            r.review_reasons.append("drug name not in the dictionary")
        elif r.match_score < 100:
            c -= 0.1 if r.match_score >= 95 else 0.2
            r.review_reasons.append("approximate name match")
        if r.status is None:
            c -= 0.7
            r.review_reasons.append("status not readable")
        if r.ocr and r.ocr_conf < 80:
            c -= 0.15
            r.review_reasons.append("low OCR confidence")
        if r.method == "prose":  # keyword rules on prose: never accepted without review
            c -= 0.3
            r.review_reasons.append("read from prose by keyword rules")
        elif r.method == "llm":
            c -= 0.15  # one model's reading, not confirmed by the rules
        if r.method == "llm+rules" and "LLM and rules disagree" in r.review_reasons:
            c -= 0.5
        if any("unreadable" in x or "symbol read by OCR" in x or "not in the legend" in x
               or "without a known header" in x or "re-read" in x or "beside the name" in x
               for x in r.review_reasons):  # fmt: skip
            c -= 0.3
        same = by_product.get(r.product or "", [])
        if len({x.status for x in same}) > 1:
            c -= 0.5
            r.review_reasons.append("conflicting statuses in this document")
        r.confidence = round(max(c, 0.0), 2)
        r.accepted = r.confidence >= accept
    return rows


def dedupe(rows: list[Row]) -> list[Row]:
    """One row per product and document: the most confident (agreeing duplicates add nothing)."""
    best: dict[str, Row] = {}
    out = []
    for r in rows:
        if r.product is None:
            out.append(r)
            continue
        if r.product not in best or r.confidence > best[r.product].confidence:
            best[r.product] = r
    return out + list(best.values())
