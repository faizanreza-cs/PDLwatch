"""Guardrails for model-written SQL (Snowflake dialect).

The agent may run ad-hoc SQL, but only:

* one read-only SELECT (CTEs and UNION allowed),
* against the allow-listed tables,
* with no table functions, LATERAL, or file / network / session functions,
* with a row cap.

Formulary data has no personal information, so there is no row-level privacy check here; the
point is that a model can read the tables and nothing else. The engine is the second line of
defence: DuckDB runs read-only with external access disabled, and on Snowflake the agent should
use the read-only role in ``snowflake/setup.sql``.
"""

from __future__ import annotations

import re

import sqlglot
from sqlglot import exp

from pdlwatch.warehouse import TABLES

ALLOWED_TABLES = frozenset(TABLES)
BLOCKED_FUNCTIONS = frozenset(
    {
        "read_csv", "read_csv_auto", "read_parquet", "parquet_scan", "read_json",
        "read_json_auto", "read_json_objects", "read_ndjson", "read_text", "read_blob", "glob",
        "sniff_csv", "query", "query_table", "getenv", "json_execute_serialized_sql",
        "result_scan", "infer_schema", "get_ddl", "system$whitelist", "flatten",
    }
)  # fmt: skip


class UnsafeSQLError(ValueError):
    pass


def validate(sql: str, row_limit: int = 200) -> str:
    """Return a safe, row-capped version of ``sql`` (Snowflake dialect) or raise."""
    try:
        statements = [s for s in sqlglot.parse(sql, read="snowflake") if s is not None]
    except sqlglot.errors.ParseError as e:
        raise UnsafeSQLError(f"could not parse SQL: {e}") from e
    if len(statements) != 1:
        raise UnsafeSQLError("exactly one statement is allowed")
    tree = statements[0]
    if not isinstance(tree, exp.Select | exp.Union | exp.Intersect | exp.Except):
        raise UnsafeSQLError(f"only SELECT queries are allowed (got {tree.key.upper()})")
    for node in tree.walk():
        if isinstance(node, exp.Insert | exp.Update | exp.Delete | exp.Drop | exp.Create
                      | exp.Alter | exp.Command | exp.Copy | exp.Merge):  # fmt: skip
            raise UnsafeSQLError("write and DDL statements are not allowed")
        if isinstance(node, exp.Lateral):
            raise UnsafeSQLError("LATERAL is not allowed")

    cte_names = {c.alias_or_name.lower() for c in tree.find_all(exp.CTE)}
    for table in tree.find_all(exp.Table):
        name = table.name.lower()
        if isinstance(table.this, exp.Func) or not name:
            raise UnsafeSQLError("table functions are not allowed")
        if table.args.get("db") or table.args.get("catalog"):
            raise UnsafeSQLError("schema-qualified table names are not allowed")
        if name not in ALLOWED_TABLES and name not in cte_names:
            raise UnsafeSQLError(f"table {table.name!r} is not allowed")

    for func in tree.find_all(exp.Func):
        fname = (func.name if isinstance(func, exp.Anonymous) else func.sql_name()).lower()
        if fname in BLOCKED_FUNCTIONS:
            raise UnsafeSQLError(f"function {fname!r} is not allowed")

    limit = tree.args.get("limit")
    options = limit.args.get("limit_options") if limit is not None else None
    plain = isinstance(tree, exp.Select) and (
        limit is None
        or (
            isinstance(limit, exp.Limit)
            and isinstance(limit.expression, exp.Literal)
            and not limit.expression.is_string
            and not (options and options.args.get("percent"))
        )
    )
    if plain:
        n = int(limit.expression.name) if limit is not None else row_limit
        return tree.limit(min(n, row_limit)).sql(dialect="snowflake")
    return exp.select("*").from_(tree.subquery("q")).limit(row_limit).sql(dialect="snowflake")


_QUOTED = re.compile(r"'[^']*'")


def scrub_error(message: str) -> str:
    """Mask quoted values in an engine error before the model sees it."""
    return _QUOTED.sub("'...'", message)
