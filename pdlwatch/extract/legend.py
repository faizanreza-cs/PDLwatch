"""What a document's codes mean: "NP = Non-Preferred", "* = clinical criteria", "Tier 3 = ...".

Each PDL defines its own shorthand in a legend, usually at the top of the first page. The
parser reads definitions of the form ``CODE = meaning`` (or ``CODE: meaning``) and maps each
meaning to one of a few roles. When a document has no legend, common defaults apply.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

ROLES = ("bvg", "not_covered", "non_preferred", "preferred", "pa", "ql", "st")

_DEF = re.compile(
    r"(?<![a-z0-9])(?P<code>\(brand preferred\)|tier\s*\d|t\d|\*{1,3}|[†‡§#^+]|[a-z]{1,4}|\d)"
    r"\s*(?:=|:|\s[–—]\s)\s*(?P<meaning>[^.;=]+)",
    re.I,
)


def meaning_role(text: str) -> str | None:
    t = text.lower()
    if "brand" in t and "generic" in t:
        return "bvg"
    if re.search(r"not covered|excluded|not on (the )?formulary|non-?formulary", t):
        return "not_covered"
    if re.search(r"non-?\s?preferred|not preferred|non-pdl", t):
        return "non_preferred"
    if "preferred" in t:
        return "preferred"
    if re.search(r"prior auth|clinical criteria|\bpa\b", t):
        return "pa"
    if "quantity" in t:
        return "ql"
    if "step" in t:
        return "st"
    return None


DEFAULT_STATUS = {
    "preferred": {"p", "preferred", "pdl", "pref", "yes", "y"},
    "non_preferred": {"np", "non-preferred", "nonpreferred", "non preferred", "non-pdl", "no", "n",
                      "not preferred", "npdl"},
    "not_covered": {"nc", "not covered", "excluded", "nf", "non-formulary", "not on formulary"},
}  # fmt: skip


@dataclass
class Legend:
    codes: dict[str, str] = field(default_factory=dict)  # code (lower case) -> role
    tiers: dict[int, str] = field(default_factory=dict)  # tier -> status role
    text: str = ""
    lost: set[str] = field(default_factory=set)  # defined, but OCR lost the code ("= Step ...")

    def status(self, raw: str) -> str | None:
        """A status cell ("NP", "Non-PDL", "Yes") as preferred / non_preferred / not_covered."""
        t = " ".join(re.sub(r"[^a-z\- ]", " ", raw.lower()).split())
        if not t:
            return None
        role = self.codes.get(t)
        if role in {"preferred", "non_preferred", "not_covered"}:
            return role
        for status, words in DEFAULT_STATUS.items():
            if t in words:
                return status
        return meaning_role(t) if meaning_role(t) in DEFAULT_STATUS else None

    def tier(self, raw: str) -> tuple[int | None, str | None]:
        """A tier cell ("Tier 2", "T3", "1", "NF") as (tier, status)."""
        t = raw.strip().lower()
        m = re.fullmatch(r"(?:tier\s*|t)?(\d)", t)
        if m:
            n = int(m.group(1))
            return n, self.tiers.get(n, "preferred" if n <= 2 else "non_preferred")
        st = self.status(raw)
        return (None, st) if st == "not_covered" else (None, None)

    def markers(self) -> dict[str, str]:
        """Codes that can follow a drug name, longest first: {"(brand preferred)": "bvg", ...}."""
        out = {c: r for c, r in self.codes.items() if r in {"pa", "ql", "st", "bvg"}}
        return dict(sorted(out.items(), key=lambda kv: -len(kv[0])))


_LOST = re.compile(r"(?:^|[.;]\s*|\|\|\s*)=\s*(?P<meaning>[^.;=]+)")


def parse(text: str) -> Legend:
    leg = Legend(text=text)
    ambiguous: set[str] = set()
    for m in _LOST.finditer(text):
        role = meaning_role(m.group("meaning"))
        if role in {"pa", "ql", "st", "bvg"}:
            leg.lost.add(role)
    for m in _DEF.finditer(text):
        code = " ".join(m.group("code").lower().split())
        role = meaning_role(m.group("meaning"))
        if role is None:
            continue
        t = re.fullmatch(r"(?:tier\s*|t)(\d)", code)
        if t and role in {"preferred", "non_preferred", "not_covered"}:
            leg.tiers[int(t.group(1))] = role
        elif code.isdigit():
            continue  # "1 = ..." outside a tier context is too ambiguous
        elif code in leg.codes and leg.codes[code] != role and role in _FLAGS:
            # one symbol for two meanings: a scan read † and ‡ both as "+". Neither can be
            # trusted, so both count as lost
            leg.lost |= {role, leg.codes[code]} & _FLAGS
            ambiguous.add(code)
        else:
            leg.codes.setdefault(code, role)
    for code in ambiguous:
        leg.codes.pop(code, None)
    return leg


_FLAGS = {"pa", "ql", "st", "bvg"}
