"""Which document is a state's PDL on a given date, and each drug's status over time.

One rule decides it everywhere (the access table, change detection, history): PDLs with a
readable effective date, one per state and date. When two files share a date, the one fetched
last is the PDL (a new version posted under an old file name inherits that name's date), then the
longer one (a list and an advisory posted the same day).

``drug_access_history`` gives every product's status in every version, valid from that
version's effective date until the next version's (exclusive), so "what was Ozempic's status in
Ohio on 15 August?" is a lookup. A row still in review is kept with ``confirmed = false``; it is
never filled in from the version before, which could hide a real change.
"""

from __future__ import annotations

import re

import pandas as pd

COLUMNS = ["state", "product", "drug_class", "status", "tier", "pa", "st", "ql", "ql_detail",
           "valid_from", "valid_to", "confirmed", "doc_id", "page"]  # fmt: skip
_ISO = re.compile(r"\d{4}-\d{2}-\d{2}")


def pdl_versions(docs: pd.DataFrame) -> pd.DataFrame:
    """Each state's PDL versions in date order: one per state and effective date."""
    pdl = docs[(docs["doc_type"] == "pdl") & docs["state"].notna() & docs["effective"].notna()]
    # on a shared date the file fetched last wins (a new version posted under the old name keeps
    # the old name's date), then the longer one (a list beats an advisory posted the same day)
    keys = ["state", "effective", "first_seen", "rows", "doc_id"]
    pdl = pdl.sort_values([k for k in keys if k in pdl.columns], na_position="first")
    return pdl.drop_duplicates(["state", "effective"], keep="last")


def latest_pdls(docs: pd.DataFrame) -> pd.Series:
    """doc_id of each state's latest PDL."""
    return pdl_versions(docs).groupby("state")["doc_id"].last()


def build(rows: pd.DataFrame, docs: pd.DataFrame) -> pd.DataFrame:
    versions = pdl_versions(docs)
    if versions.empty or rows.empty:
        return pd.DataFrame(columns=COLUMNS)
    nxt = versions.groupby("state")["effective"].shift(-1)  # the next version's date, if any
    span = versions.assign(valid_from=versions["effective"], valid_to=nxt)[
        ["doc_id", "valid_from", "valid_to"]
    ]
    rows = rows[rows["product"].notna()].drop(columns=["effective"], errors="ignore")
    hist = rows.merge(span, on="doc_id")
    hist["confirmed"] = hist["accepted"].astype(bool)
    hist = hist.sort_values(["state", "product", "valid_from"])
    return hist[COLUMNS].reset_index(drop=True)


def check_date(value: str) -> str:
    """An ISO date (YYYY-MM-DD), or a clear error; it goes into SQL, so nothing else passes."""
    v = str(value).strip()
    if not _ISO.fullmatch(v):
        raise ValueError(f"as_of must be a date like 2026-08-15, not {value!r}")
    pd.Timestamp(v)  # a real calendar date (no 2026-02-30)
    return v
