"""Unstructured documents: prior authorisation criteria written as prose.

Each class section states facts (which agents are preferred, which need PA, quantity limits,
step therapy, exclusions) as sentences. The text and the truth come from the same facts, so they
can't disagree. About 30% of sentences use phrasings without the usual cue words ("subject to
clinical review", "limited to"), and some sentences name a drug without stating its status
("members stable on X may continue"), which is what makes prose harder than tables.
"""

from __future__ import annotations

from typing import Any

import numpy as np

from pdlwatch.drugs import BY_NAME
from pdlwatch.synth.truth import V1_DATE, Document, Policy, TruthRow

HARD = 0.3

_PREFERRED = {
    "easy": ["Preferred agents: {list}.",
             "The preferred agents in this class are {list}.",
             "{list} {are} preferred and available without prior authorization."],
    "hard": ["{list} {are} on the list of drugs pharmacies may fill without a prior approval request.",
             "Prescribers should choose {list} first; {these} {are} paid at the point of sale."],
}  # fmt: skip
_NON_PREFERRED = {
    "easy": ["Non-preferred agents ({list}) require prior authorization.",
             "{list} {are} non-preferred and require prior authorization.",
             "Prior authorization is required for {list}."],
    "hard": ["Requests for {or_list} are reviewed for medical necessity.",
             "{list} {are} subject to clinical review before dispensing."],
}  # fmt: skip
_PREFERRED_PA = {
    "easy": ["{x} is preferred but requires prior authorization; clinical criteria apply."],
    "hard": ["{x} is preferred, and prescribers must submit the clinical criteria form."],
}
_QL = {
    "easy": ["Quantity limits apply to {x}: {detail}."],
    "hard": ["{x} is limited to {detail}."],
}
_ST = {
    "easy": ["Step therapy applies to {x}: a trial of a preferred agent is required first."],
    "hard": ["Before {x} is approved, the member must have tried {pref} or another preferred agent."],
}  # fmt: skip
_EXCLUDED = {
    "easy": ["{list} {are} not covered.", "{list} {are} excluded from the pharmacy benefit."],
    "hard": ["The program does not pay for {list}."],
}
_FILLER = [
    "Approval duration is 12 months.",
    "Initial approvals are granted for 6 months; renewals require documentation of response.",
    "Requests must include the diagnosis and the prescriber's NPI.",
    "Members new to Medicaid who are stable on {x} may continue therapy for 90 days.",
]
_HEADINGS = ["{cls}", "{upper}", "{cls}: Prior Authorization Criteria"]


def _name(product: str, rng: np.random.Generator) -> str:
    p = BY_NAME[product]
    if p.kind == "brand" and rng.random() < 0.4:
        return f"{p.name} ({p.ingredient})"
    return p.name


def _join(names: list[str], word: str = "and") -> str:
    if len(names) == 1:
        return names[0]
    return ", ".join(names[:-1]) + f" {word} " + names[-1]


def _fill(templates: dict[str, list[str]], rng: np.random.Generator, **kw: Any) -> str:
    kind = "hard" if rng.random() < HARD else "easy"
    options = templates[kind]
    t = options[int(rng.integers(len(options)))]
    return t.format(**kw)


def _plural(n: int) -> dict[str, str]:
    return {"are": "are" if n > 1 else "is", "these": "these" if n > 1 else "it"}


def section(cls: str, policy: list[Policy], rng: np.random.Generator
            ) -> tuple[dict[str, Any], list[TruthRow]]:  # fmt: skip
    """One class: heading, sentences, and the truth rows they state."""
    heading = _HEADINGS[int(rng.integers(len(_HEADINGS)))].format(cls=cls, upper=cls.upper())
    rows: dict[str, TruthRow] = {}
    sentences: list[str] = []
    if all(p.status == "not_covered" for p in policy):
        names = [p.product for p in policy]
        sentences.append(_fill(_EXCLUDED, rng, list=_join([_name(n, rng) for n in names]),
                               **_plural(len(names))))  # fmt: skip
        for n in names:
            rows[n] = TruthRow(n, cls, "not_covered", pa=False)
        sentences.append(_FILLER[int(rng.integers(3))])
        return {"heading": heading, "sentences": sentences}, list(rows.values())

    pref = [p for p in policy if p.status == "preferred"][:6]
    nonp = [p for p in policy if p.status == "non_preferred"][:6]
    lead = _fill(_PREFERRED, rng, list=_join([_name(p.product, rng) for p in pref]),
                 **_plural(len(pref)))  # fmt: skip
    for p in pref:
        rows[p.product] = TruthRow(p.product, cls, "preferred", pa=False)
    rest: list[str] = []
    if nonp:
        names = [_name(p.product, rng) for p in nonp]
        rest.append(_fill(_NON_PREFERRED, rng, list=_join(names), or_list=_join(names, "or"),
                          **_plural(len(nonp))))  # fmt: skip
        for p in nonp:
            rows[p.product] = TruthRow(p.product, cls, "non_preferred", pa=True)
    for p in pref:
        if p.pa:  # always said, so this document never contradicts the state's PDL
            rest.append(_fill(_PREFERRED_PA, rng, x=_name(p.product, rng)))
            rows[p.product].pa = True
    stated = pref + nonp
    for p in [p for p in stated if p.ql][:2]:
        rest.append(_fill(_QL, rng, x=_name(p.product, rng), detail=p.ql_detail))
        rows[p.product].ql = True
    for p in [p for p in nonp if p.st][:1]:
        rest.append(_fill(_ST, rng, x=_name(p.product, rng), pref=_name(pref[0].product, rng)))
        rows[p.product].st = True
    if rng.random() < 0.5:
        x = stated[int(rng.integers(len(stated)))].product
        rest.append(_FILLER[int(rng.integers(len(_FILLER)))].format(x=_name(x, rng)))
    order = rng.permutation(len(rest))
    sentences = [lead] + [rest[int(i)] for i in order]
    return {"heading": heading, "sentences": sentences}, list(rows.values())


def pa_criteria_document(code: str, policy: list[Policy], split: str,
                         rng: np.random.Generator) -> Document:  # fmt: skip
    classes = list(dict.fromkeys(p.drug_class for p in policy))
    chosen = sorted(rng.choice(len(classes), min(len(classes), int(rng.integers(5, 9))),
                               replace=False))  # fmt: skip
    text, rows = [], []
    for i in chosen:
        cls = classes[int(i)]
        sec, truth = section(cls, [p for p in policy if p.drug_class == cls], rng)
        text.append(sec)
        rows += truth
    return Document(f"{code}-PAC-2026-07", code, "pa_criteria", "narrative", 1, V1_DATE, False,
                    split, {"font": "Times-Roman" if rng.random() < 0.5 else "Helvetica",
                            "size": 10.0}, policy, rows, text)  # fmt: skip
