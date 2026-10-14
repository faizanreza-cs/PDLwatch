"""Tools the Access Copilot can call. Every status in an answer comes from one of these, with the
document and page it was read from.

Statuses come from the warehouse (``drug_access`` is each state's latest PDL, rows accepted
without review; ``review_queue`` holds the rest). Extraction quality comes from ``results.json``.
Only known values (dictionary products, state codes, class names) are ever put into SQL.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

import pandas as pd

from pdlwatch import history, pipeline
from pdlwatch.agent.sql_guard import UnsafeSQLError, scrub_error, validate
from pdlwatch.config import get_settings
from pdlwatch.drugs import CLASSES, PRODUCTS, match_class, match_product
from pdlwatch.queries import run_saved, saved_queries
from pdlwatch.synth.states import BY_NAME as STATE_CODES
from pdlwatch.synth.states import STATES
from pdlwatch.warehouse import get_warehouse

STATUS_WORDS = {"preferred": "preferred", "non_preferred": "non-preferred (PA)",
                "not_covered": "not covered"}  # fmt: skip


def _ok(rows: list[dict[str, Any]] | pd.DataFrame, note: str = "", **extra: Any) -> dict:
    if isinstance(rows, pd.DataFrame):
        rows = json.loads(rows.to_json(orient="records", date_format="iso", double_precision=4))
    return {"rows": rows, "row_count": len(rows), "note": note, **extra}


def _q(sql: str) -> pd.DataFrame:
    return get_warehouse().query(sql)


def _in(values: list[str]) -> str:
    """A SQL IN list of values that came from a fixed vocabulary (never free text)."""
    return ", ".join("'" + v.replace("'", "''") + "'" for v in values)


def _found_by_ingredient(key: str) -> list[str]:
    try:
        products = _q("SELECT product, ingredient FROM products")  # filtered here, not in SQL
    except FileNotFoundError:  # no warehouse yet
        return []
    hit = products[products["ingredient"].fillna("").str.lower() == key]
    return [str(p) for p in hit["product"]]


def resolve_drugs(name: str) -> list[str]:
    """Products a name refers to: a brand or generic product, or every product of an ingredient
    ("semaglutide" -> Ozempic, Rybelsus, Wegovy)."""
    key = " ".join(name.lower().split())
    by_ingredient = [p.name for p in PRODUCTS if p.ingredient.lower() == key]
    if by_ingredient:
        return by_ingredient
    found = _found_by_ingredient(key)  # products the documents named that only RxNorm knows
    if found:
        return found
    m = match_product(name)
    if m.product is None:
        raise ValueError(f"no drug in the dictionary matches {name!r}")
    return [m.product.name]


def resolve_state(name: str) -> str:
    key = name.strip()
    if key.upper() in STATES:
        return key.upper()
    code = STATE_CODES.get(key.lower())
    if code is None:
        raise ValueError(f"unknown state {name!r}; use a name or a two-letter code")
    return code


def resolve_class(name: str) -> str:
    cls = match_class(name)
    if cls is None:
        raise ValueError(f"unknown class {name!r}; classes: {', '.join(CLASSES)}")
    return cls


def _cite(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df["source"] = df["doc_id"] + " p." + df["page"].astype(str)
    return df.drop(columns=["doc_id", "page"])


# --------------------------------------------------------------------------- tools


def t_drug_access(drug: str, as_of: str | None = None) -> dict:
    products = resolve_drugs(drug)
    if as_of:
        return _as_of(products, history.check_date(as_of))
    df = _q(f"""SELECT state, state_name, product, status, tier, pa, st, ql, ql_detail, effective,
                       doc_id, page FROM drug_access WHERE product IN ({_in(products)})
                ORDER BY product, state""")  # fmt: skip
    pending = _q(f"SELECT COUNT(*) AS n FROM review_queue WHERE product IN ({_in(products)})")
    counts = df.groupby(["product", "status"]).size().unstack(fill_value=0)
    parts = []
    for product, row in counts.iterrows():
        bits = [f"{int(row[s])} {STATUS_WORDS[s]}" for s in STATUS_WORDS if s in row and row[s]]
        parts.append(f"{product}: listed by {int(row.sum())} states ({', '.join(bits)})")
    note = "; ".join(parts) + (
        f". {int(pending['n'].iloc[0])} more rows for these products are waiting for review "
        "and are not counted."
        if int(pending["n"].iloc[0])
        else "."
    )
    return _ok(_cite(df), note=note + " Source = document and page each status was read from.")


def _as_of(products: list[str], day: str) -> dict:
    df = _q(f"""SELECT state, product, status, tier, pa, st, ql, ql_detail, valid_from, valid_to,
                       confirmed, doc_id, page FROM drug_access_history
                WHERE product IN ({_in(products)}) AND valid_from <= '{day}'
                  AND (valid_to IS NULL OR '{day}' < valid_to)
                ORDER BY product, state""")  # fmt: skip
    df["state_name"] = df["state"].map(STATES)
    unsure = int((~df["confirmed"].astype(bool)).sum()) if len(df) else 0
    note = (f"Status in force on {day}, from the PDL version effective then (valid_from) until the "
            f"next version (valid_to, exclusive). {unsure} rows were still in review "
            "(confirmed = false).")  # fmt: skip
    return _ok(_cite(df), note=note)


def t_state_formulary(state: str, drug_class: str | None = None) -> dict:
    code = resolve_state(state)
    where = f"state = '{code}'"
    if drug_class:
        where += f" AND drug_class = '{resolve_class(drug_class)}'"
    df = _q(f"""SELECT drug_class, product, status, tier, pa, st, ql, ql_detail, effective, doc_id,
                       page FROM drug_access WHERE {where} ORDER BY drug_class, status, product""")  # fmt: skip
    if df.empty:
        raise ValueError(f"no accepted rows for {STATES[code]}" + (f" in {drug_class}" if
                         drug_class else ""))  # fmt: skip
    pref = df.loc[df["status"] == "preferred", "product"].tolist()
    note = (f"{STATES[code]} PDL effective {df['effective'].iloc[0]}: {len(df)} products, "
            f"{len(pref)} preferred.")  # fmt: skip
    return _ok(_cite(df), note=note)


def t_class_landscape(drug_class: str) -> dict:
    cls = resolve_class(drug_class)
    df = run_saved("class_landscape", get_warehouse())
    df = df[df["drug_class"] == cls].drop(columns=["drug_class"])
    note = (f"{cls}: each product's status across the states that list it (latest PDL per state, "
            "rows accepted without review). preferred_share = states preferring it / states "
            "listing it.")  # fmt: skip
    return _ok(df, note=note)


def t_compare_drugs(drugs: list[str]) -> dict:
    if not 2 <= len(drugs) <= 4:
        raise ValueError("compare two to four drugs")
    products = [p for d in drugs for p in resolve_drugs(d)]
    df = _q(f"""SELECT state, product, status FROM drug_access
                WHERE product IN ({_in(products)})""")  # fmt: skip
    wide = df.pivot_table(index="state", columns="product", values="status", aggfunc="first")
    wide = wide.reset_index().fillna("not listed")
    both = wide[[p for p in products if p in wide.columns]].eq("preferred").all(axis=1).sum()
    note = f"Status by state; {int(both)} states prefer all of {', '.join(products)}."
    return _ok(wide, note=note)


def t_pdl_changes(state: str | None = None, drug: str | None = None,
                  change: str | None = None) -> dict:  # fmt: skip
    where = ["1 = 1"]
    if state:
        where.append(f"state = '{resolve_state(state)}'")
    if drug:
        where.append(f"product IN ({_in(resolve_drugs(drug))})")
    if change:
        if change not in {"status", "added", "removed", "ql"}:
            raise ValueError("change must be one of status, added, removed, ql")
        where.append(f"change = '{change}'")
    df = _q(f"""SELECT state, product, drug_class, change, before, after, effective, from_doc,
                       to_doc, needs_review FROM changes WHERE {' AND '.join(where)}
                ORDER BY needs_review, state, change, product""")  # fmt: skip
    sure = int((~df["needs_review"].astype(bool)).sum()) if len(df) else 0
    note = (f"{len(df)} changes between consecutive PDL versions; {sure} confirmed automatically, "
            f"{len(df) - sure} waiting for review (they involve a scanned version, where a drug "
            "OCR missed looks like an addition or removal).")  # fmt: skip
    return _ok(df.head(100), note=note)


def t_review_queue(state: str | None = None, limit: int = 20) -> dict:
    where = f"WHERE state = '{resolve_state(state)}'" if state else ""
    limit = max(1, min(int(limit), 100))
    df = _q(f"""SELECT doc_id, page, drug_text, product, status, confidence, review_reasons,
                       evidence FROM review_queue {where}
                ORDER BY confidence, doc_id, page LIMIT {limit}""")  # fmt: skip
    total = _q(f"SELECT COUNT(*) AS n FROM review_queue {where}")["n"].iloc[0]
    reasons = _q(f"""SELECT review_reasons, COUNT(*) AS n FROM review_queue {where}
                     GROUP BY review_reasons ORDER BY n DESC LIMIT 5""")  # fmt: skip
    return _ok(df, note=f"{int(total)} rows waiting for review; lowest confidence first.",
               top_reasons=json.loads(reasons.to_json(orient="records")))  # fmt: skip


def t_extraction_quality() -> dict:
    res = pipeline.load_results()
    if "extraction" not in res:
        d = res["data"]
        return _ok([], note=(f"{d['source']}: {d['documents']} documents, {d['rows']:,} rows, "
                             f"{d['accepted']:,} accepted without review. No truth to score "
                             "against for this data."))  # fmt: skip
    groups = res["extraction"]["test"]
    keep = ["documents", "truth_rows", "row_recall", "row_precision", "rows_fully_correct",
            "status_accuracy", "accepted_share", "accepted_error_rate"]  # fmt: skip
    rows = [{"group": g, **{k: m[k] for k in keep}} for g, m in groups.items()
            if not g.startswith("split:")]  # fmt: skip
    ch = res["changes"]
    note = (
        "Scored against the synthetic corpus's truth on the held-out test states (thresholds "
        "were set on the dev states). accepted_error_rate = share of rows accepted without "
        f"review that are wrong. Change detection: recall {ch['recall']:.1%}; "
        f"{ch['confirmed']} changes confirmed automatically at precision "
        f"{ch['confirmed_precision']:.1%}."
    )
    return _ok(rows, note=note, extractor=res.get("extractor"))


def t_documents(state: str | None = None) -> dict:
    where = f"WHERE state = '{resolve_state(state)}'" if state else ""
    df = _q(f"""SELECT doc_id, state, doc_type, effective, pages, ocr_pages, rows, accepted,
                       review FROM documents {where} ORDER BY state, effective, doc_id""")  # fmt: skip
    return _ok(df, note=f"{len(df)} documents; ocr_pages > 0 means scanned pages were OCR'd.")


def t_list_saved_queries() -> dict:
    rows = [{"name": q.name, "title": q.title, "description": q.description}
            for q in saved_queries().values()]  # fmt: skip
    return _ok(
        rows, note="Saved queries (Snowflake SQL; also used by the Power BI and Dataiku kits)."
    )


def t_run_saved_query(name: str) -> dict:
    q = saved_queries().get(name)
    if q is None:
        raise ValueError(f"unknown query {name!r}; choose from {', '.join(saved_queries())}")
    df = run_saved(name, get_warehouse())
    return _ok(df.head(200), note=f"{q.title}: {q.description}", total_rows=len(df))


def t_run_sql(sql: str) -> dict:
    s = get_settings()
    safe = validate(sql, row_limit=s.sql_row_limit)
    try:
        df = get_warehouse().query(safe, timeout=s.sql_timeout)
    except TimeoutError as e:
        raise ValueError(f"{e}; simplify the query") from e
    except Exception as e:  # engine errors can quote values; mask them
        raise ValueError(scrub_error(str(e))) from e
    return _ok(df, note=f"Ran (row-capped): {safe}")


# --------------------------------------------------------------------------- registry

TOOLS: dict[str, tuple[Callable[..., dict], dict[str, Any]]] = {
    "drug_access": (t_drug_access, {
        "description": "Where one drug is covered: its status (preferred, non-preferred = prior authorization, not covered), PA, step therapy and quantity limit in every state that lists it, from each state's latest PDL, with the document and page as source. Accepts a brand (Ozempic), a generic product (liraglutide) or an ingredient (semaglutide -> every product with it). With as_of (YYYY-MM-DD), the status in force on that date instead.",
        "input_schema": {"type": "object", "properties": {"drug": {"type": "string"},
                                                          "as_of": {"type": "string"}},
                         "required": ["drug"]},
    }),
    "state_formulary": (t_state_formulary, {
        "description": "One state's PDL: every product with its status, PA, step therapy and quantity limit, optionally for one therapeutic class, with sources.",
        "input_schema": {"type": "object", "properties": {
            "state": {"type": "string", "description": "State name or two-letter code"},
            "drug_class": {"type": "string"}},
            "required": ["state"]},
    }),
    "class_landscape": (t_class_landscape, {
        "description": "A therapeutic class across all states: for each product, how many states prefer it, list it as non-preferred or exclude it, and how often PA or a quantity limit applies. Classes: " + ", ".join(CLASSES) + ".",
        "input_schema": {"type": "object", "properties": {"drug_class": {"type": "string"}},
                         "required": ["drug_class"]},
    }),
    "compare_drugs": (t_compare_drugs, {
        "description": "Side-by-side status of two to four drugs in every state (e.g. a brand against its competitors).",
        "input_schema": {"type": "object", "properties": {
            "drugs": {"type": "array", "items": {"type": "string"}, "minItems": 2, "maxItems": 4}},
            "required": ["drugs"]},
    }),
    "pdl_changes": (t_pdl_changes, {
        "description": "What changed between consecutive PDL versions: status changes, products added or removed, quantity-limit changes, optionally for one state, drug or change type. Says which changes are confirmed and which wait for review.",
        "input_schema": {"type": "object", "properties": {
            "state": {"type": "string"}, "drug": {"type": "string"},
            "change": {"type": "string", "enum": ["status", "added", "removed", "ql"]}}},
    }),
    "review_queue": (t_review_queue, {
        "description": "Rows the extraction wasn't sure about and sent to a person: drug, reason (unreadable status, OCR, prose read by rules, conflict), confidence and the text it came from.",
        "input_schema": {"type": "object", "properties": {
            "state": {"type": "string"},
            "limit": {"type": "integer", "minimum": 1, "maximum": 100}}},
    }),
    "extraction_quality": (t_extraction_quality, {
        "description": "How accurate the extraction is: row recall and precision, fields fully correct, status accuracy, share accepted without review and the error rate of accepted rows, by document type and for scanned vs digital documents; plus change-detection precision and recall.",
        "input_schema": {"type": "object", "properties": {}},
    }),
    "documents": (t_documents, {
        "description": "The documents loaded (one state or all): type (PDL or PA criteria), effective date, pages, OCR'd pages, rows read and rows accepted.",
        "input_schema": {"type": "object", "properties": {"state": {"type": "string"}}},
    }),
    "list_saved_queries": (t_list_saved_queries, {
        "description": "List the saved queries (name, title, description).",
        "input_schema": {"type": "object", "properties": {}},
    }),
    "run_saved_query": (t_run_saved_query, {
        "description": "Run a saved query: class_landscape (products across states), state_scorecard (per state: preferred, PA and QL shares, review backlog), brand_over_generic (brands preferred while their generic is not), quarterly_changes (changes by state and type), review_backlog (rows to review by state and reason), extraction_by_document (pages, OCR, rows accepted per document).",
        "input_schema": {"type": "object", "properties": {
            "name": {"type": "string", "enum": sorted(saved_queries())}},
            "required": ["name"]},
    }),
    "run_sql": (t_run_sql, {
        "description": "Run one read-only Snowflake SQL SELECT when no other tool fits. Tables: drug_access(state, state_name, product, ingredient, kind, drug_class, status, tier, pa, st, ql, ql_detail, bvg, effective, doc_id, page, confidence) = latest PDL per state, accepted rows; formulary_rows (every row read, with method, confidence, accepted, review_reasons, evidence); review_queue; changes(state, product, drug_class, change, before, after, from_doc, to_doc, effective, confidence, needs_review); documents(doc_id, state, doc_type, effective, pages, ocr_pages, rows, accepted, review); products(product, ingredient, kind, drug_class); states(state, state_name). status is preferred | non_preferred | not_covered. Results are capped at 200 rows.",
        "input_schema": {"type": "object", "properties": {"sql": {"type": "string"}},
                         "required": ["sql"]},
    }),
}  # fmt: skip


def tool_definitions() -> list[dict[str, Any]]:
    return [{"name": name, **spec} for name, (_, spec) in TOOLS.items()]


def execute(name: str, args: dict[str, Any]) -> tuple[str, bool]:
    """Run a tool; returns (json_string, is_error). Errors go back to the model, not up."""
    if name not in TOOLS:
        return json.dumps({"error": f"unknown tool {name!r}"}), True
    fn, _ = TOOLS[name]
    if "_invalid_arguments" in args:  # the model's arguments weren't valid JSON
        return json.dumps({"error": "the tool arguments were not valid JSON; call it again"}), True
    try:
        return json.dumps(fn(**args), default=str), False
    except (UnsafeSQLError, ValueError, TypeError, KeyError, FileNotFoundError) as e:
        return json.dumps({"error": f"{type(e).__name__}: {e}"}), True
    except Exception as e:  # warehouse errors and the like: tell the model, keep the run alive
        return json.dumps({"error": f"{type(e).__name__}: {e}"}), True
