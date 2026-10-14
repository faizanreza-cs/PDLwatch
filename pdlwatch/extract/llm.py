"""LLM extraction for prose pages: structured output, checked against the page.

Keyword rules read "Non-preferred agents require prior authorization" but not the many other
ways prior authorisation criteria say the same thing. Here an LLM reads each prose page and
returns rows in a fixed schema (structured outputs, so the reply always parses). Nothing it
returns is trusted blindly:

* **grounding**: a row is kept only if its drug is a known product and appears on the page;
  anything else is counted as a rejected (possibly invented) row;
* **agreement**: each row is compared with the keyword rules' reading of the same page. Where
  both agree the row is accepted; where they disagree it goes to review with both readings.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

from pdlwatch.drugs import find_products, match_product, normalise
from pdlwatch.extract import prose
from pdlwatch.extract.rows import Row
from pdlwatch.pdf import Page

SYSTEM = """\
You extract drug coverage facts from US state Medicaid documents (preferred drug lists, prior
authorization criteria). Return one row for every drug whose coverage the text states.

- status: "preferred" (covered without prior authorization), "non_preferred" (covered only with
  prior authorization), "not_covered" (excluded from the benefit), or "unclear" when the text
  mentions the drug without saying how it is covered.
- Statuses are often stated indirectly. A drug that needs an approval, a request or a review
  before it is dispensed is non_preferred. A drug pharmacies may dispense with no approval step
  is preferred. Judge by what happens to the claim, not by whether the word "preferred" appears.
- prior_authorization: true for every non_preferred drug, and for a preferred drug the text says
  still needs prior authorization or clinical criteria; false otherwise.
- step_therapy: true only for a drug that is approved after the member has tried other drugs
  first; null when the text doesn't say. A drug named only as something to try first is not
  under step therapy.
- quantity_limit: the limit as written ("4 per 28 days") when the text gives one, else null.
- drug: the name exactly as written in the text. Do not add drugs the text doesn't name, and
  do not split a brand from the generic name in parentheses after it.
- evidence: the shortest verbatim quote that supports the status.
"""


class LLMRow(BaseModel):
    drug: str = Field(description="Drug name exactly as written")
    drug_class: str | None = Field(default=None, description="Therapeutic class heading")
    status: Literal["preferred", "non_preferred", "not_covered", "unclear"]
    prior_authorization: bool | None = None
    step_therapy: bool | None = None
    quantity_limit: str | None = None
    evidence: str = Field(description="Shortest verbatim quote supporting the status")


class PageRows(BaseModel):
    rows: list[LLMRow]


def _grounded(product: str, page_text: str) -> bool:
    return any(p.name == product for p in find_products(page_text)) or normalise(
        product
    ) in normalise(page_text)


class LLMExtractor:
    """Reads prose pages with an LLM provider that has ``structured()``."""

    def __init__(self, provider: Any) -> None:
        if getattr(provider, "structured", None) is None:
            raise ValueError(f"provider {getattr(provider, 'name', provider)!r} has no structured "
                             "output; use anthropic, openai or azure")  # fmt: skip
        self.provider = provider
        self.rejected: list[dict[str, Any]] = []

    def page(self, page: Page, state: str | None) -> list[LLMRow]:
        user = f"State: {state or 'unknown'}. Page {page.number}:\n\n{page.text}"
        return list(self.provider.structured(SYSTEM, user, PageRows).rows)

    def extract(self, pages: list[Page], doc_id: str, state: str | None
                ) -> tuple[list[Row], int]:  # fmt: skip
        rules = {r.product.name: r for r in prose.extract(pages)}
        rows: dict[str, Row] = {}
        calls = 0
        for page in pages:
            if not find_products(page.text):
                continue  # no known drug on the page: nothing to pay for
            calls += 1
            for item in self.page(page, state):
                m = match_product(item.drug)
                if m.product is None or not _grounded(m.product.name, page.text):
                    self.rejected.append({"doc_id": doc_id, "page": page.number,
                                          "drug": item.drug,
                                          "evidence": item.evidence})  # fmt: skip
                    continue
                p = m.product
                status = None if item.status == "unclear" else item.status
                ql = item.quantity_limit
                row = Row(doc_id, state, page.number, "llm", item.drug, p.drug_class, p.name,
                          p.ingredient, p.kind, m.score, status, None, item.prior_authorization,
                          item.step_therapy, bool(ql) if ql is not None else None, ql, None,
                          page.ocr, 100.0, item.evidence)  # fmt: skip
                if status == "non_preferred":
                    row.pa = True
                rule = rules.get(p.name)
                if rule is not None and rule.status is not None:
                    row.method = "llm+rules"
                    if rule.status != status:
                        row.review_reasons.append("LLM and rules disagree")
                        row.evidence += f" | rules read: {rule.status} ({rule.evidence[:80]})"
                if p.name not in rows or rows[p.name].status is None:
                    rows[p.name] = row
        # drugs the rules found and the model didn't: kept, for review
        for name, rule in rules.items():
            if name not in rows:
                rows[name] = prose.to_row(rule, doc_id, state)
        return list(rows.values()), calls
