"""Ground truth for the synthetic corpus.

Each state gets a PDL *policy*: for every product it lists, a status (preferred, non-preferred or
not covered) and its utilisation management (prior authorisation, step therapy, quantity limit,
brand preferred over generic). Documents then express that policy in different ways, and each
document's truth is exactly what that document states: a PDL without a step-therapy column
can't tell you about step therapy, so those fields are ``None`` and aren't scored.

The statuses are random draws with realistic shapes (generics usually preferred, specialty classes
under PA, anti-obesity drugs often excluded). They are fictional: not any state's real PDL.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field, replace
from typing import Any

import numpy as np

from pdlwatch.drugs import BY_NAME, CLASSES, PRODUCTS, Product
from pdlwatch.synth.states import STATES

SPECIALTY = {
    "Hepatitis C Agents", "Cytokine and CAM Antagonists", "Multiple Sclerosis Agents",
    "Immunomodulators, Asthma", "Antimigraine Agents, CGRP", "Anti-Obesity Agents",
}  # fmt: skip
QL_RATE = {
    "Antimigraine Agents, CGRP": 0.7, "GLP-1 Receptor Agonists": 0.5, "ADHD Stimulants": 0.4,
    "Opioid Use Disorder Agents": 0.6, "Proton Pump Inhibitors": 0.3, "Hepatitis C Agents": 0.4,
    "Anti-Obesity Agents": 0.5,
}  # fmt: skip
QL_DETAIL = {  # units-free, so a limit never names the wrong dosage form
    "Antimigraine Agents, CGRP": ["1 per 30 days", "8 per 30 days", "16 per 30 days"],
    "GLP-1 Receptor Agonists": ["4 per 28 days", "2 per 28 days", "30 per 30 days"],
    "ADHD Stimulants": ["1 per day", "2 per day", "30 per 30 days"],
    "Opioid Use Disorder Agents": ["3 per day", "24 mg per day", "1 per 28 days"],
    "Proton Pump Inhibitors": ["1 per day", "2 per day"],
    "Hepatitis C Agents": ["28 per 28 days", "84 per 84 days"],
    "Anti-Obesity Agents": ["4 per 28 days", "1 per day"],
}  # fmt: skip
DEFAULT_QL = ["30 per 30 days", "1 per day", "2 per day"]

V1_DATE, V2_DATE = "2026-07-01", "2026-10-01"
FAMILIES = ("grid", "columns", "tiered")


@dataclass
class Policy:
    """One product's place on one state's PDL."""

    product: str
    drug_class: str
    kind: str
    status: str  # preferred | non_preferred | not_covered
    pa: bool
    st: bool
    ql: bool
    ql_detail: str | None
    bvg: bool  # brand preferred over its generic (set on the brand)


@dataclass
class TruthRow:
    product: str
    drug_class: str
    status: str
    pa: bool | None = None
    st: bool | None = None
    ql: bool | None = None
    tier: int | None = None
    bvg: bool | None = None


@dataclass
class Change:
    state: str
    product: str
    change: str  # added | removed | status | ql
    before: str | None
    after: str | None


@dataclass
class Document:
    doc_id: str
    state: str
    doc_type: str  # pdl | pa_criteria
    family: str  # grid | columns | tiered | narrative
    version: int
    effective: str
    scanned: bool
    split: str  # dev | test (by state)
    style: dict[str, Any]
    policy: list[Policy]
    rows: list[TruthRow] = field(default_factory=list)
    text: list[dict[str, Any]] = field(default_factory=list)  # narrative sections

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d.pop("policy")
        return d


# --------------------------------------------------------------------------- policies


def _class_policy(cls: str, rng: np.random.Generator, excluded: bool) -> list[Policy]:
    members = [p for p in PRODUCTS if p.drug_class == cls]
    keep = rng.random(len(members)) < rng.uniform(0.85, 1.0)
    listed = [p for p, k in zip(members, keep, strict=True) if k] or members[:3]
    if excluded:
        return [Policy(p.name, cls, p.kind, "not_covered", False, False, False, None, False)
                for p in listed]  # fmt: skip
    special = cls in SPECIALTY
    out: list[Policy] = []
    for p in listed:
        base = 0.75 if p.kind == "generic" else (0.3 if special else 0.35)
        out.append(Policy(p.name, cls, p.kind, "preferred" if rng.random() < base else
                          "non_preferred", False, False, False, None, False))  # fmt: skip
    if not any(x.status == "preferred" for x in out):
        out[int(rng.integers(len(out)))].status = "preferred"
    # brand preferred over generic: a rebate makes the brand cheaper than its generic
    if rng.random() < 0.25:
        by_ing: dict[str, list[Policy]] = {}
        for x in out:
            by_ing.setdefault(BY_NAME[x.product].ingredient, []).append(x)
        pairs = [(b, g) for v in by_ing.values() for b in v for g in v
                 if b.kind == "brand" and g.kind == "generic"]  # fmt: skip
        if pairs:
            b, g = pairs[int(rng.integers(len(pairs)))]
            b.status, b.bvg, g.status = "preferred", True, "non_preferred"
    for x in out:
        if x.status == "non_preferred":
            x.pa = True
            x.st = special and rng.random() < 0.35
        else:
            x.pa = rng.random() < (0.55 if special else 0.1)
        if rng.random() < QL_RATE.get(cls, 0.12):
            x.ql = True
            choices = QL_DETAIL.get(cls, DEFAULT_QL)
            x.ql_detail = choices[int(rng.integers(len(choices)))]
    return out


def state_policy(rng: np.random.Generator) -> list[Policy]:
    n = int(rng.integers(12, 18))
    classes = sorted(rng.choice(CLASSES, n, replace=False), key=CLASSES.index)
    excluded_obesity = rng.random() < 0.55
    out: list[Policy] = []
    for cls in classes:
        out += _class_policy(cls, rng, excluded=cls == "Anti-Obesity Agents" and excluded_obesity)
    return out


def revise(policy: list[Policy], state: str, rng: np.random.Generator
           ) -> tuple[list[Policy], list[Change]]:  # fmt: skip
    """A quarterly update: a few status flips, quantity-limit edits, additions and removals."""
    new = [replace(p) for p in policy]
    changes: list[Change] = []
    movable = [p for p in new if p.status != "not_covered" and not p.bvg]
    flips = rng.choice(len(movable), max(2, round(0.05 * len(movable))), replace=False)
    for i in flips:
        p = movable[int(i)]
        before = p.status
        p.status = "non_preferred" if before == "preferred" else "preferred"
        # PA follows the status: non-preferred always needs it
        p.pa = True if p.status == "non_preferred" else (p.drug_class in SPECIALTY and p.pa)
        changes.append(Change(state, p.product, "status", before, p.status))
    for i in rng.choice(len(new), max(1, round(0.02 * len(new))), replace=False):
        p = new[int(i)]
        if p.status == "not_covered":
            continue
        before_ql = p.ql_detail if p.ql else None
        if p.ql:
            p.ql, p.ql_detail = False, None
        else:
            choices = QL_DETAIL.get(p.drug_class, DEFAULT_QL)
            p.ql, p.ql_detail = True, choices[int(rng.integers(len(choices)))]
        changes.append(Change(state, p.product, "ql", before_ql, p.ql_detail))
    # a product leaves the list, and an unlisted one in a listed class joins as non-preferred
    changed = {c.product for c in changes}  # a product that leaves has no other change
    removable = [p for p in new if p.status != "not_covered" and p.product not in changed]
    gone = removable[int(rng.integers(len(removable)))]
    new.remove(gone)
    changes.append(Change(state, gone.product, "removed", gone.status, None))
    listed = {p.product for p in new} | {gone.product}
    classes = {p.drug_class for p in new}
    extra = [p for p in PRODUCTS if p.drug_class in classes and p.name not in listed
             and p.drug_class != "Anti-Obesity Agents"]  # fmt: skip
    if extra:
        add: Product = extra[int(rng.integers(len(extra)))]
        new.append(Policy(add.name, add.drug_class, add.kind, "non_preferred", True, False,
                          False, None, False))  # fmt: skip
        new.sort(key=lambda p: CLASSES.index(p.drug_class))
        changes.append(Change(state, add.name, "added", None, "non_preferred"))
    return new, changes


# --------------------------------------------------------------------------- documents


def _pdl_style(family: str, rng: np.random.Generator) -> dict[str, Any]:
    def pick(options: list[Any]) -> Any:
        return options[int(rng.integers(len(options)))]

    style: dict[str, Any] = {"font": pick(["Helvetica", "Times-Roman"]),
                             "size": float(pick([8, 8.5, 9])),
                             "name_style": pick(["plain", "upper", "with_ingredient"])}  # fmt: skip
    if family == "grid":
        style.update(pick([
            {"columns": ["Therapeutic Class", "Drug Name", "PDL Status", "PA Required",
                         "Quantity Limit", "Step Therapy"], "status_vocab": "word"},
            {"columns": ["Drug Class", "Medication", "Status", "PA", "QL", "ST"],
             "status_vocab": "code"},
            {"columns": ["Class", "Product", "Preferred?", "Prior Auth", "Limits"],
             "status_vocab": "yesno"},
            {"columns": ["Category", "Drug", "Formulary Status", "Prior Authorization",
                         "Quantity Limits"], "status_vocab": "pdl"},
        ]))  # fmt: skip
        style["flag_vocab"] = pick([("Y", ""), ("Yes", "No"), ("X", "")])
        style["class_every_row"] = bool(rng.random() < 0.4)
        style["alphabetical"] = bool(rng.random() < 0.2)
        style["landscape"] = bool(rng.random() < 0.3)
    elif family == "columns":
        style["headers"] = pick([("PREFERRED AGENTS", "NON-PREFERRED AGENTS"),
                                 ("Preferred Drugs", "Non-Preferred Drugs"),
                                 ("PREFERRED", "NON-PREFERRED")])  # fmt: skip
        style["criteria_column"] = bool(rng.random() < 0.5)
        style["markers"] = pick([
            {"pa": "*", "ql": "†", "st": "‡", "bvg": "BvG"},
            {"pa": "CC", "ql": "QL", "st": "ST", "bvg": "BvG"},
            {"pa": "*", "ql": "QL", "st": None, "bvg": "(Brand preferred)"},
            {"pa": None, "ql": None, "st": None, "bvg": None},
        ])  # fmt: skip
    else:  # tiered
        style["columns"] = pick([("Drug Name", "Tier", "Requirements/Limits"),
                                 ("Medication", "Drug Tier", "Coverage Requirements and Limits"),
                                 ("Product", "Tier", "Notes")])  # fmt: skip
        style["tier_vocab"] = pick(["number", "T", "word"])
        style["sep"] = pick(["; ", ", "])
        style["excluded_label"] = pick(["NF", "Excluded", "Not covered"])
    return style


def _pdl_rows(policy: list[Policy], family: str, style: dict[str, Any]) -> list[TruthRow]:
    rows = []
    for p in policy:
        if family == "grid":
            cols = style["columns"]
            st = p.st if len(cols) == 6 else None
            rows.append(TruthRow(p.product, p.drug_class, p.status, p.pa, st, p.ql, None, None))
        elif family == "columns":
            m = style["markers"]
            # PA is implied for non-preferred agents; a marker is needed to show it on preferred
            pa = p.pa if (m["pa"] or p.status != "preferred") else None
            rows.append(TruthRow(p.product, p.drug_class, p.status, pa,
                                 p.st if m["st"] else None, p.ql if m["ql"] else None, None,
                                 p.bvg if m["bvg"] else None))  # fmt: skip
        else:
            tier = (
                None
                if p.status == "not_covered"
                else (3 if p.status == "non_preferred" else (1 if p.kind == "generic" else 2))
            )
            rows.append(TruthRow(p.product, p.drug_class, p.status, p.pa, p.st, p.ql, tier,
                                 None))  # fmt: skip
    return rows


def plan(seed: int = 7) -> tuple[list[Document], list[Change]]:
    """Every document in the corpus, with its truth, and the true changes between versions."""
    from pdlwatch.synth.prose import pa_criteria_document

    rng = np.random.default_rng(seed)
    codes = list(STATES)
    order = rng.permutation(len(codes))
    family_of = {codes[int(i)]: FAMILIES[k % 3] for k, i in enumerate(order)}
    dev = set(rng.choice(codes, 25, replace=False))
    revised = set(rng.choice(codes, 25, replace=False))
    with_criteria = set(rng.choice(codes, 30, replace=False))
    docs: list[Document] = []
    changes: list[Change] = []
    for code in codes:
        srng = np.random.default_rng([seed, codes.index(code)])
        policy = state_policy(srng)
        family = family_of[code]
        style = _pdl_style(family, srng)
        split = "dev" if code in dev else "test"
        docs.append(Document(f"{code}-PDL-2026-07", code, "pdl", family, 1, V1_DATE,
                             False, split, style, policy, _pdl_rows(policy, family, style)))  # fmt: skip
        if code in revised:
            new, ch = revise(policy, code, srng)
            changes += ch
            docs.append(Document(f"{code}-PDL-2026-10", code, "pdl", family, 2, V2_DATE, False,
                                 split, style, new, _pdl_rows(new, family, style)))  # fmt: skip
        if code in with_criteria:
            docs.append(pa_criteria_document(code, policy, split, srng))
    # about one document in seven arrives as a scan
    for i in rng.choice(len(docs), round(len(docs) / 7), replace=False):
        docs[int(i)].scanned = True
    return docs, changes


def product(name: str) -> Product:
    return BY_NAME[name]
