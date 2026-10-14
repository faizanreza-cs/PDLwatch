"""Evaluate the Access Copilot on golden questions.

Metrics
-------
* **tool_accuracy**   - the agent called at least one of the expected tools
* **answer_accuracy** - every ground-truth fact (number or label) appears in the answer
* **tool_error_rate** - questions where a tool returned an error
* latency and token usage per question

Run ``python -m pdlwatch.evals.run_evals`` (mock provider in CI) or
``--provider anthropic`` (or openai / azure) to measure a real model. Writes
``reports/eval_results_<provider>.json`` and ``reports/eval_summary_<provider>.md``.
"""

from __future__ import annotations

import argparse
import json
import re
import statistics
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pandas as pd
import yaml

from pdlwatch import pipeline
from pdlwatch.agent.core import Agent
from pdlwatch.agent.provider import get_provider
from pdlwatch.queries import run_saved, saved_queries
from pdlwatch.warehouse import get_warehouse

GOLDEN = Path(__file__).with_name("golden.yaml")
Fact = float | int | str


# --------------------------------------------------------------------------- ground truth


def _q(sql: str) -> pd.DataFrame:
    return get_warehouse().query(sql)


def _one(sql: str) -> Fact:
    v = _q(sql).iloc[0, 0]
    return v if isinstance(v, str) else float(v)


def _saved(name: str) -> pd.DataFrame:
    return run_saved(name, get_warehouse())


def _counts(product: str) -> list[Fact]:
    df = _q(f"SELECT status FROM drug_access WHERE product = '{product}'")
    return [len(df), int((df["status"] == "preferred").sum())]


def _state_class(state: str, cls: str) -> list[Fact]:
    df = _q(f"SELECT status FROM drug_access WHERE state = '{state}' AND drug_class = '{cls}'")
    return [len(df), int((df["status"] == "preferred").sum())]


def _top(cls: str) -> list[Fact]:
    df = _saved("class_landscape")
    row = df[df["drug_class"] == cls].iloc[0]
    return [str(row["product"]), float(row["preferred_share"])]


def _changes(where: str) -> list[Fact]:
    df = _q(f"SELECT needs_review FROM changes WHERE {where}")
    return [len(df), int((~df["needs_review"].astype(bool)).sum())]


def _quality(group: str) -> list[Fact]:
    g = pipeline.load_results()["extraction"]["test"][group]
    return [float(g["rows_fully_correct"]), float(g["accepted_error_rate"])]


def _both_preferred(a: str, b: str) -> list[Fact]:
    df = _q(f"SELECT state, product, status FROM drug_access WHERE product IN ('{a}', '{b}')")
    wide = df.pivot_table(index="state", columns="product", values="status", aggfunc="first")
    return [int(wide.eq("preferred").all(axis=1).sum())]


TRUTH: dict[str, Callable[[], Sequence[Fact]]] = {
    "saved_query_names": lambda: list(saved_queries())[:3],
    "mounjaro_counts": lambda: _counts("Mounjaro"),
    "wegovy_not_covered": lambda: ["Wegovy", _one("SELECT COUNT(*) FROM drug_access WHERE "
                                                  "product = 'Wegovy' AND status = 'not_covered'")],
    "liraglutide_counts": lambda: _counts("liraglutide"),
    "ohio_glp1": lambda: _state_class("OH", "GLP-1 Receptor Agonists"),
    "texas_anticoagulants": lambda: _state_class("TX", "Anticoagulants, Oral"),
    "sglt2_top": lambda: _top("SGLT2 Inhibitors"),
    "hepc_top": lambda: _top("Hepatitis C Agents"),
    "obesity_top": lambda: _top("Anti-Obesity Agents"),
    "ozempic_mounjaro_both": lambda: _both_preferred("Ozempic", "Mounjaro"),
    "ozempic_changes": lambda: _changes("product = 'Ozempic'"),
    "alabama_changes": lambda: _changes("state = 'AL'"),
    "maryland_review": lambda: [_one("SELECT COUNT(*) FROM review_queue WHERE state = 'MD'")],
    "first_review_reason": lambda: [str(_q(
        "SELECT review_reasons FROM review_queue ORDER BY confidence, doc_id, page LIMIT 1"
    ).iloc[0, 0]).split("; ")[0]],
    "overall_quality": lambda: _quality("overall"),
    "scanned_quality": lambda: _quality("scanned"),
    "first_brand_over_generic": lambda: [str(_saved("brand_over_generic").iloc[0]["state"]),
                                         str(_saved("brand_over_generic").iloc[0]["preferred_brand"])],
    "first_scorecard": lambda: [str(_saved("state_scorecard").iloc[0]["state"]),
                                float(_saved("state_scorecard").iloc[0]["preferred_share"])],
    "wv_documents": lambda: list(_q("SELECT doc_id FROM documents WHERE state = 'WV' "
                                    "ORDER BY doc_id")["doc_id"]),
    "sql_status": lambda: [_one("SELECT COUNT(*) AS n FROM drug_access GROUP BY status "
                                "ORDER BY n DESC LIMIT 1")],
    "sql_classes": lambda: [_one("SELECT COUNT(DISTINCT product) AS n FROM drug_access "
                                 "GROUP BY drug_class ORDER BY n DESC LIMIT 1")],
}  # fmt: skip


# --------------------------------------------------------------------------- grading

_NUM = re.compile(r"(-?\d[\d,]*\.?\d*)\s*([kKmM%]?)")


def extract_numbers(text: str) -> list[float]:
    out = []
    for raw, suffix in _NUM.findall(text):
        try:
            n = float(raw.replace(",", ""))
        except ValueError:
            continue
        if suffix in "kK" and suffix:
            n *= 1_000
        elif suffix in "mM" and suffix:
            n *= 1_000_000
        out.append(n)
    return out


def fact_present(fact: Fact, answer: str) -> bool:
    if isinstance(fact, str):
        return fact.lower() in answer.lower()
    nums = extract_numbers(answer)
    v = float(fact)
    if abs(v) <= 1.5 and not float(v).is_integer():  # a rate / share
        return any(abs(n - v) <= 0.0015 or abs(n - 100 * v) <= 0.15 for n in nums)
    tol = max(abs(v) * 0.005, 0.6)  # counts and volumes: 0.5% (allows "62.7K")
    return any(abs(n - v) <= tol for n in nums)


@dataclass
class EvalRow:
    id: str
    question: str
    expected_tools: list[str]
    tools_used: list[str]
    tool_ok: bool
    facts: list[Fact]
    facts_found: list[bool]
    answer_ok: bool
    tool_error: bool
    latency_ms: int
    input_tokens: int
    output_tokens: int
    answer: str


def run(provider_name: str = "mock", only: list[str] | None = None) -> list[EvalRow]:
    cases = yaml.safe_load(GOLDEN.read_text())
    if only:
        cases = [c for c in cases if c["id"] in only]
    agent = Agent(get_provider(provider_name))
    rows = []
    for case in cases:
        res = agent.ask(case["question"])
        facts = list(TRUTH[case["truth"]]())
        found = [fact_present(f, res.answer) for f in facts]
        rows.append(
            EvalRow(
                id=case["id"],
                question=case["question"],
                expected_tools=case["expected_tools"],
                tools_used=res.tools_used,
                tool_ok=bool(set(case["expected_tools"]) & set(res.tools_used)),
                facts=facts,
                facts_found=found,
                answer_ok=all(found),
                tool_error=any(s.is_error for s in res.steps),
                latency_ms=res.latency_ms,
                input_tokens=res.usage.get("input_tokens", 0),
                output_tokens=res.usage.get("output_tokens", 0),
                answer=res.answer,
            )
        )
        mark = "PASS" if rows[-1].tool_ok and rows[-1].answer_ok else "FAIL"
        print(f"[{mark}] {case['id']:<20} tools={res.tools_used} facts={found}", flush=True)
    return rows


def summarise(rows: list[EvalRow], provider: str) -> dict[str, Any]:
    n = len(rows)
    return {
        "provider": provider,
        "questions": n,
        "tool_accuracy": round(sum(r.tool_ok for r in rows) / n, 4),
        "answer_accuracy": round(sum(r.answer_ok for r in rows) / n, 4),
        "pass_rate": round(sum(r.tool_ok and r.answer_ok for r in rows) / n, 4),
        "tool_error_rate": round(sum(r.tool_error for r in rows) / n, 4),
        "median_latency_ms": int(statistics.median(r.latency_ms for r in rows)),
        "total_input_tokens": sum(r.input_tokens for r in rows),
        "total_output_tokens": sum(r.output_tokens for r in rows),
    }


def write_reports(rows: list[EvalRow], summary: dict[str, Any], out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    tag = summary["provider"]
    (out_dir / f"eval_results_{tag}.json").write_text(
        json.dumps({"summary": summary, "rows": [r.__dict__ for r in rows]}, indent=2, default=str)
    )
    lines = [
        f"# Eval summary ({tag})",
        "",
        "| metric | value |",
        "|---|---|",
        *[f"| {k} | {v} |" for k, v in summary.items()],
        "",
        "| id | tool ok | answer ok | tools used |",
        "|---|---|---|---|",
        *[
            f"| {r.id} | {'✅' if r.tool_ok else '❌'} | {'✅' if r.answer_ok else '❌'} "
            f"| {', '.join(r.tools_used)} |"
            for r in rows
        ],
    ]
    (out_dir / f"eval_summary_{tag}.md").write_text("\n".join(lines) + "\n")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--provider", default="mock", help="mock | anthropic | openai | azure")
    p.add_argument("--only", nargs="*", help="run only these case ids")
    p.add_argument("--out", default="reports")
    p.add_argument("--min-pass-rate", type=float, default=0.0, help="exit 1 below this")
    a = p.parse_args(argv)
    rows = run(a.provider, a.only)
    summary = summarise(rows, a.provider)
    write_reports(rows, summary, Path(a.out))
    print(json.dumps(summary, indent=2))
    return 0 if summary["pass_rate"] >= a.min_pass_rate else 1


if __name__ == "__main__":
    sys.exit(main())
