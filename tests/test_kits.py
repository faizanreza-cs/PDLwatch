import ast
import importlib
import json
import re
from pathlib import Path

import pandas as pd

from pdlwatch import charts, powerbi
from pdlwatch.api import app
from pdlwatch.queries import saved_queries
from pdlwatch.warehouse import SCHEMA, get_warehouse

ROOT = Path(__file__).resolve().parents[1]


def test_saved_queries_run(built):
    wh = get_warehouse()
    for name, q in saved_queries().items():
        assert q.title and q.description, name
        assert len(wh.query(q.sql)) > 0, name


def test_powerbi_export_and_measures_agree(built, tmp_path):
    paths = powerbi.export(tmp_path)
    cols = {p.stem: set(pd.read_csv(p, nrows=1).columns) for p in paths}
    assert {"dim_state", "dim_product", "fact_access", "fact_changes", "fact_review"} <= cols.keys()
    dax = (ROOT / "powerbi" / "measures.dax").read_text()
    refs = re.findall(r"\b(dim_\w+|fact_\w+)\[(\w+)\]", dax)
    assert refs
    for table, column in refs:  # every measure points at a real exported column
        assert column in cols[table], f"{table}[{column}]"


def test_powerbi_theme_is_valid():
    theme = json.loads((ROOT / "powerbi" / "theme.json").read_text())
    assert len(theme["dataColors"]) == 8
    assert all(re.fullmatch(r"#[0-9A-F]{6}", c) for c in theme["dataColors"])


def test_copilot_connector_matches_the_api():
    spec = json.loads((ROOT / "copilot" / "access-copilot.swagger.json").read_text())
    assert spec["swagger"] == "2.0"
    routes = {(r.path, m) for r in app.routes for m in getattr(r, "methods", set())}
    for path, ops in spec["paths"].items():
        for method in ops:
            assert (path, method.upper()) in routes, path
    assert "/extract" not in spec["paths"]  # uploads stay out of the connector
    api = set(app.openapi()["components"]["schemas"]["AskRequest"]["properties"])
    assert set(spec["definitions"]["AskRequest"]["properties"]) == api


def test_dataiku_recipes_import_real_functions():
    recipes = list((ROOT / "dataiku" / "recipes").glob("*.py"))
    assert recipes
    for recipe in recipes:
        tree = ast.parse(recipe.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and (node.module or "").startswith("pdlwatch"):
                mod = importlib.import_module(node.module)
                for alias in node.names:
                    assert hasattr(mod, alias.name), f"{recipe.name}: {node.module}.{alias.name}"
        loop = re.search(r"for name in \(([^)]*)\)", recipe.read_text())
        if loop:  # the datasets it writes are tables the pipeline produces
            assert set(re.findall(r'"(\w+)"', loop.group(1))) <= set(SCHEMA), recipe.name


def test_snowflake_setup_has_a_read_only_role():
    sql = (ROOT / "snowflake" / "setup.sql").read_text()
    analyst = [line for line in sql.splitlines() if "TO ROLE PDLWATCH_ANALYST" in line]
    assert analyst and not any(re.search(r"CREATE|INSERT|UPDATE|DELETE", g) for g in analyst)


def test_charts_render(built, tmp_path):
    paths = charts.render(tmp_path)
    assert len(paths) == 3 and all(p.stat().st_size > 10_000 for p in paths)
