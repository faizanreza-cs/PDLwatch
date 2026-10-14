"""Prior authorisation criteria and other prose: statuses from sentences, without an LLM.

Sentences are split into clauses; each clause's drugs come from the dictionary and its status
from cue phrases ("non-preferred", "requires prior authorization", "available without PA",
"not covered"). This is the offline baseline. Prose that says the same thing without the usual
words ("subject to clinical review", "paid at the point of sale") is where it fails and the LLM
extractor earns its cost; a drug mentioned with no readable status becomes a row with no status,
so it goes to review rather than disappearing.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from pdlwatch.drugs import Product, find_products, match_class
from pdlwatch.extract.rows import Row
from pdlwatch.pdf import Page

_NOT_COVERED = re.compile(r"\bnot covered\b|\bexcluded\b|does not (cover|pay)|not a covered", re.I)
_NOT_NEGATED = r"(?<!non-)(?<!non )(?<!not )(?<!no longer )"
_PREF_PA = re.compile(
    _NOT_NEGATED + r"\bpreferred\b[^.;]*\b(requires?|with)\b[^.;]*\b(prior auth\w*|pa|clinical "
    r"criteria)\b|" + _NOT_NEGATED + r"\bpreferred\b[^.;]*\bclinical criteria apply",
    re.I,
)
_NON_PREF = re.compile(
    r"non-?\s?preferred|not preferred|no longer preferred|"
    r"requires? (a )?prior auth|prior auth\w* (is|are) required|\bpa (is |are )?required|"
    r"requires? pa\b|subject to prior auth",
    re.I,
)
_PREF = re.compile(
    _NOT_NEGATED + r"\bpreferred\b|without (a )?prior auth\w*|without pa\b|"
    r"no (prior auth\w*|pa) (is )?required|do(es)? not require (prior auth\w*|pa)\b",
    re.I,
)
_QL = re.compile(r"quantity limit|\blimited to\b|\bQL\b", re.I)
_ST = re.compile(r"step therapy|trial (and|of|&)|must have tried|after (a )?trial", re.I)
_DETAIL = re.compile(
    r"\d+(\.\d+)?\s*(mg\s+)?per\s+(\d+\s+)?(days|day|weeks|week|months|month)", re.I
)
_SPLIT_SENTENCES = re.compile(r"(?<=[.!?])\s+")
_SPLIT_CLAUSES = re.compile(r";\s+|,\s+(?:while|whereas|but)\s+")


@dataclass
class ProseRow:
    page: int
    product: Product
    status: str | None
    pa: bool | None
    st: bool | None
    ql: bool | None
    ql_detail: str | None
    heading: str | None
    evidence: str
    conflict: bool = False


def _paragraphs(pages: list[Page], skip: set[int]) -> list[tuple[int, str | None, str]]:
    """(page, heading, text) blocks in reading order, from pages not read as tables."""
    out: list[tuple[int, str | None, str]] = []
    heading: str | None = None
    for page in pages:
        if page.number in skip:
            continue
        buf: list[str] = []
        for line in page.lines:
            if line.top < 0.085 * page.height or line.bottom > 0.945 * page.height:
                continue
            text = line.text.strip()
            title = re.sub(r":\s*prior authorization criteria$", "", text, flags=re.I)
            is_heading = (line.bold and len(text) < 80 and not text.endswith(".")) or (
                match_class(title) is not None and len(text) < 80
            )
            if is_heading:
                if buf:
                    out.append((page.number, heading, " ".join(buf)))
                    buf = []
                heading = title
                continue
            if buf and buf[-1].endswith("-"):
                buf[-1] = buf[-1] + text
            else:
                buf.append(text)
        if buf:
            out.append((page.number, heading, " ".join(buf)))
    return out


def extract(pages: list[Page], skip: set[int] | None = None) -> list[ProseRow]:
    found: dict[str, ProseRow] = {}
    st_candidates: set[str] = set()
    for page_no, heading, text in _paragraphs(pages, skip or set()):
        for sentence in _SPLIT_SENTENCES.split(text):
            for clause in _SPLIT_CLAUSES.split(sentence):
                products = find_products(clause)
                if not products:
                    continue
                step = bool(_ST.search(clause))
                status, pa = None, None
                if step:
                    pass  # "must have tried X or another preferred agent" says nothing about X
                elif _NOT_COVERED.search(clause):
                    status, pa = "not_covered", False
                elif _PREF_PA.search(clause):
                    status, pa = "preferred", True
                elif _NON_PREF.search(clause):
                    status, pa = "non_preferred", True
                elif _PREF.search(clause):
                    status, pa = "preferred", False
                ql = bool(_QL.search(clause))
                detail = _DETAIL.search(clause)
                for p in products:
                    row = found.get(p.name)
                    if row is None:
                        row = found[p.name] = ProseRow(page_no, p, None, None, None, None, None,
                                                       heading, sentence)  # fmt: skip
                    if status is not None:
                        if row.status is None:
                            row.status, row.evidence = status, sentence
                            row.pa = pa
                        elif row.status != status and not (status == "preferred" and pa):
                            row.conflict = True
                        if status == "preferred" and pa:
                            row.pa = True  # "X is preferred but requires PA"
                    if ql and len(products) == 1:
                        row.ql = True
                        row.ql_detail = detail.group(0) if detail else row.ql_detail
                    if step:
                        st_candidates.add(p.name)
    for name in st_candidates:
        row = found[name]
        if row.status == "non_preferred":
            row.st = True
    for row in found.values():
        if row.status == "non_preferred":
            row.pa = True
    return list(found.values())


def to_row(p: ProseRow, doc_id: str, state: str | None) -> Row:
    row = Row(doc_id, state, p.page, "prose", p.product.name, p.product.drug_class,
              p.product.name, p.product.ingredient, p.product.kind, 100.0, p.status, None,
              p.pa, p.st, p.ql, p.ql_detail, None, False, 100.0, p.evidence)  # fmt: skip
    if p.conflict:
        row.review_reasons.append("prose states more than one status")
    return row
