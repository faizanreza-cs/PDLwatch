"""Saved queries (``pdlwatch/sql/*.sql``, Snowflake dialect).

Each file starts with comment lines: the first is the title, the rest describe the query. The
same files run on DuckDB (through sqlglot) and unchanged on Snowflake, Dataiku SQL recipes or a
Power BI DirectQuery source.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import pandas as pd

from pdlwatch.warehouse import Warehouse

SQL_DIR = Path(__file__).with_name("sql")


@dataclass(frozen=True)
class SavedQuery:
    name: str
    title: str
    description: str
    sql: str


def _parse(path: Path) -> SavedQuery:
    lines = path.read_text().splitlines()
    header = []
    for line in lines:
        if not line.startswith("--"):
            break
        header.append(line.lstrip("-").strip())
    if not header:
        raise ValueError(f"{path.name}: first line must be a '-- Title' comment")
    return SavedQuery(path.stem, header[0], " ".join(header[1:]), path.read_text())


@lru_cache(maxsize=1)
def saved_queries() -> dict[str, SavedQuery]:
    return {p.stem: _parse(p) for p in sorted(SQL_DIR.glob("*.sql"))}


def run_saved(name: str, wh: Warehouse) -> pd.DataFrame:
    queries = saved_queries()
    if name not in queries:
        raise KeyError(f"unknown query {name!r}; choose from {', '.join(queries)}")
    return wh.query(queries[name].sql)
