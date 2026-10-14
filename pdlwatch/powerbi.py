"""Power BI kit: export a star schema as CSV files for Power BI Desktop.

``pdlwatch export-powerbi`` writes ``powerbi/data/*.csv``. Load them with Get data > Text/CSV,
create the relationships listed in ``powerbi/README.md``, paste the measures from
``powerbi/measures.dax`` and apply ``powerbi/theme.json``.
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd

from pdlwatch import pipeline
from pdlwatch.warehouse import get_warehouse


def tables() -> dict[str, pd.DataFrame]:
    wh = get_warehouse()
    res = pipeline.load_results()
    out = {
        "dim_state": wh.query("SELECT state, state_name FROM states ORDER BY state"),
        "dim_product": wh.query("SELECT product, ingredient, kind, drug_class FROM products"),
        "dim_document": wh.query(
            "SELECT doc_id, state, doc_type, effective, pages, ocr_pages, rows, accepted, review "
            "FROM documents"
        ),
        "fact_access": wh.query(
            "SELECT state, product, status, tier, pa, st, ql, ql_detail, bvg, effective, doc_id, "
            "page FROM drug_access"
        ),
        "fact_changes": wh.query(
            "SELECT state, product, change, before, after, effective, from_doc, to_doc, "
            "needs_review FROM changes"
        ),
        "fact_review": wh.query(
            "SELECT doc_id, state, page, drug_text, product, status, confidence, review_reasons "
            "FROM review_queue"
        ),
    }
    if "extraction" in res:  # the synthetic corpus: accuracy by document type
        rows = [{"group": g, **{k: v for k, v in m.items() if not isinstance(v, dict)}}
                for g, m in res["extraction"]["test"].items()]  # fmt: skip
        out["fact_extraction_quality"] = pd.DataFrame(rows)
    return out


def export(out: str | Path = "powerbi/data") -> list[Path]:
    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    paths = []
    for name, df in tables().items():
        path = out / f"{name}.csv"
        df.to_csv(path, index=False)
        paths.append(path)
    return paths
