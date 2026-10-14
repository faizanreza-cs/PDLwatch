"""Command line: ``pdlwatch corpus | build | extract | drug | state | changes | review | query |
sql | ask | charts | export-powerbi | load | sync | audit``."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from pdlwatch.config import get_settings


def _print_table(rows: list[dict]) -> None:
    import pandas as pd

    with pd.option_context("display.width", 180, "display.max_columns", 20,
                           "display.max_colwidth", 60):  # fmt: skip
        print(pd.DataFrame(rows).to_string(index=False, na_rep=""))


def _summary(res: dict[str, Any]) -> str:
    out: dict[str, Any] = {"data": res["data"], "speed": res["speed"],
                           "extractor": res.get("extractor")}  # fmt: skip
    if "extraction" in res:
        keep = ("documents", "row_recall", "row_precision", "rows_fully_correct",
                "accepted_share", "accepted_error_rate")  # fmt: skip
        out["test_split"] = {g: {k: m[k] for k in keep}
                             for g, m in res["extraction"]["test"].items()}  # fmt: skip
        out["changes"] = {k: v for k, v in res["changes"].items() if k != "by_type"}
    return json.dumps(out, indent=2)


def _llm(enabled: bool) -> Any:
    if not enabled:
        return None
    from pdlwatch.agent.provider import get_provider
    from pdlwatch.extract.llm import LLMExtractor

    return LLMExtractor(get_provider())


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="pdlwatch", description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("corpus", help="generate the synthetic PDLs and their truth")
    c.add_argument("--out", type=Path, default=Path("data/corpus"))
    c.add_argument("--seed", type=int, default=7)
    b = sub.add_parser("build", help="extract the synthetic corpus, score it, write the warehouse")
    b.add_argument("--llm", action="store_true", help="read prose with the LLM (needs a key)")
    b.add_argument("--workers", type=int, default=4)
    e = sub.add_parser("extract", help="extract one PDF (rows to CSV) or a folder (warehouse)")
    e.add_argument("path", type=Path)
    e.add_argument("--state", help="two-letter code, when the PDF doesn't name its state")
    e.add_argument("--out", type=Path, help="CSV for a single PDF's rows")
    e.add_argument("--llm", action="store_true")
    d = sub.add_parser("drug", help="a drug's status in every state")
    d.add_argument("name")
    d.add_argument("--as-of", help="the status in force on this date (YYYY-MM-DD)")
    st = sub.add_parser("state", help="a state's PDL, optionally one class")
    st.add_argument("state")
    st.add_argument("--class", dest="drug_class")
    ch = sub.add_parser("changes", help="changes between PDL versions")
    ch.add_argument("--state")
    ch.add_argument("--drug")
    rv = sub.add_parser("review", help="rows waiting for review (CSV with --out)")
    rv.add_argument("--state")
    rv.add_argument("--out", type=Path)
    q = sub.add_parser("query", help="run a saved query (no name: list them)")
    q.add_argument("name", nargs="?")
    s = sub.add_parser("sql", help="run a guarded read-only Snowflake SQL query")
    s.add_argument("sql")
    a = sub.add_parser("ask", help="ask the Access Copilot a question")
    a.add_argument("question")
    a.add_argument("--provider", default=None, help="anthropic | openai | azure | mock")
    ch2 = sub.add_parser("charts", help="render the README charts")
    ch2.add_argument("--out", default="docs/img")
    ep = sub.add_parser("export-powerbi", help="write the Power BI star schema as CSV files")
    ep.add_argument("--out", default="powerbi/data")
    sub.add_parser("load", help="copy the tables to Snowflake (SNOWFLAKE_* variables)")
    au = sub.add_parser("audit", help="print sampled rows beside their source line to check")
    au.add_argument("-n", type=int, default=40)
    au.add_argument("--seed", type=int, default=1)
    au.add_argument("--state")
    au.add_argument("--pdfs", help="folder of the PDFs (default: the synthetic corpus)")
    f = sub.add_parser("sync", help="download new state PDL versions and extract them")
    f.add_argument("--sources", type=Path, default=Path("sources/medicaid_pdls.yaml"))
    f.add_argument("--out", type=Path, default=Path("data/real"))
    f.add_argument("--states", help="comma-separated codes to fetch (default: all)")
    f.add_argument("--llm", action="store_true")
    args = p.parse_args(argv)

    if args.cmd == "corpus":
        from pdlwatch.synth.corpus import build as build_corpus

        truth = build_corpus(args.out, args.seed)
        n = len(truth["documents"])
        print(f"wrote {n} PDFs to {args.out / 'pdfs'} and their truth to {args.out / 'truth.json'}")
        return 0
    if args.cmd == "build":
        from pdlwatch.pipeline import build

        res = build(llm=_llm(args.llm), workers=args.workers)
        print(_summary(res))
        print(f"wrote {get_settings().db_path} and results.json")
        return 0
    if args.cmd == "extract":
        return _extract(args)
    if args.cmd == "sync":
        from pdlwatch.realdata import fetch_and_build

        states = [x.strip().upper() for x in args.states.split(",")] if args.states else None
        res = fetch_and_build(args.sources, args.out, states, llm=_llm(args.llm))
        print(_summary(res))
        return 0

    if args.cmd == "audit":
        from pdlwatch.audit import report

        for i, r in enumerate(report(args.n, args.seed, args.state, args.pdfs), 1):
            flags = ", ".join(f"{k}={int(r[k]) if k == 'tier' else r[k]}"
                              for k in ("tier", "pa", "ql") if _given(r[k]))  # fmt: skip
            print(f"[{i}] {r['doc']} p.{r['page']} {r['method']}: {r['drug']} -> {r['status']}"
                  f" ({flags})\n    header: {r['header']}\n    line:   {r['line']}")  # fmt: skip
        return 0

    from pdlwatch.agent import tools

    if args.cmd == "drug":
        out, err = tools.execute("drug_access", {"drug": args.name, "as_of": args.as_of})
    elif args.cmd == "state":
        out, err = tools.execute("state_formulary", {"state": args.state,
                                                     "drug_class": args.drug_class})  # fmt: skip
    elif args.cmd == "changes":
        out, err = tools.execute("pdl_changes", {"state": args.state, "drug": args.drug})
    elif args.cmd == "review":
        if args.out:
            return _review_csv(args.state, args.out)
        out, err = tools.execute("review_queue", {"state": args.state, "limit": 50})
    elif args.cmd == "query":
        out, err = tools.execute(
            "run_saved_query" if args.name else "list_saved_queries",
            {"name": args.name} if args.name else {},
        )
    elif args.cmd == "sql":
        out, err = tools.execute("run_sql", {"sql": args.sql})
    elif args.cmd == "ask":
        from pdlwatch.agent.core import Agent
        from pdlwatch.agent.provider import get_provider

        reply = Agent(get_provider(args.provider)).ask(args.question)
        print(reply.answer)
        print(f"\n[{reply.provider} | tools: {', '.join(reply.tools_used) or 'none'} | "
              f"{reply.latency_ms} ms]")  # fmt: skip
        return 0
    elif args.cmd == "charts":
        try:
            from pdlwatch.charts import render
        except ImportError:
            print("charts need matplotlib: pip install 'pdlwatch[charts]'")
            return 1
        for path in render(args.out):
            print(f"wrote {path}")
        return 0
    elif args.cmd == "export-powerbi":
        from pdlwatch.powerbi import export

        for path in export(args.out):
            print(f"wrote {path}")
        return 0
    else:  # load
        from pdlwatch.warehouse import TABLES, DuckDBWarehouse, SnowflakeWarehouse

        local, remote = DuckDBWarehouse(get_settings().db_path), SnowflakeWarehouse()
        for t in TABLES:
            n = remote.write(t, local.query(f"SELECT * FROM {t}"))
            print(f"{t}: {n:,} rows")
        return 0

    payload = json.loads(out)
    if err:
        print(payload["error"], file=sys.stderr)
        return 1
    _print_table(payload["rows"])
    if payload.get("note"):
        print(f"\n{payload['note']}")
    return 0


def _given(v: Any) -> bool:
    import pandas as pd

    return v is not None and not (isinstance(v, float) and pd.isna(v)) and v is not pd.NA


def _extract(args: argparse.Namespace) -> int:
    from pdlwatch.extract.document import extract_document

    if args.path.is_dir():
        from pdlwatch.pipeline import run_folder

        res = run_folder(args.path, llm=_llm(args.llm), source=str(args.path))
        print(_summary(res))
        return 0
    doc = extract_document(args.path, state=args.state, llm=_llm(args.llm))
    print(json.dumps(doc.summary(), indent=2))
    if args.out:
        import pandas as pd

        args.out.parent.mkdir(parents=True, exist_ok=True)
        pd.DataFrame([r.to_dict() for r in doc.rows]).to_csv(args.out, index=False)
        print(f"wrote {len(doc.rows)} rows to {args.out}")
    return 0


def _review_csv(state: str | None, out: Path) -> int:
    """The review queue as a CSV a reviewer can work through (source page and text included)."""
    from pdlwatch.agent.tools import resolve_state
    from pdlwatch.warehouse import get_warehouse

    where = f"WHERE state = '{resolve_state(state)}'" if state else ""
    df = get_warehouse().query(f"SELECT * FROM review_queue {where} ORDER BY doc_id, page")
    out.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(out, index=False)
    print(f"wrote {len(df)} rows to {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
