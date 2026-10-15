"""End to end: a folder of PDFs -> canonical rows -> warehouse tables + ``results.json``.

``build`` runs on the synthetic corpus (generated on first use) and scores every step against
its truth. ``run_folder`` runs the same extraction on any folder of PDFs, such as the real state
PDLs the manual workflow downloads, without a truth to score against.
"""

from __future__ import annotations

import json
import multiprocessing as mp
import time
from collections import Counter
from concurrent.futures import Executor, ProcessPoolExecutor, ThreadPoolExecutor
from datetime import UTC, date, datetime
from functools import partial
from pathlib import Path
from typing import Any

import duckdb
import pandas as pd

from pdlwatch import changes as changes_mod
from pdlwatch import history
from pdlwatch import review as review_mod
from pdlwatch.cache import DocCache, extractor_key, file_sha256, profile_key
from pdlwatch.config import Settings, cache_path, corpus_path, get_settings, results_path
from pdlwatch.drugs import PRODUCTS, rxcui
from pdlwatch.evaluate import evaluate
from pdlwatch.extract.document import DocResult, date_hint, extract_document
from pdlwatch.pdf import modified
from pdlwatch.sheet import is_sheet
from pdlwatch.synth import corpus as synth_corpus
from pdlwatch.synth.states import STATES
from pdlwatch.warehouse import SCHEMA, reset_cache

Job = tuple[str, str | None, str | None]  # path, doc_id, state


Profiles = dict[str, dict[str, Any]]


def _extract(job: Job, profile: dict[str, Any] | None = None, llm: Any = None) -> DocResult:
    path, doc_id, state = job
    try:
        return extract_document(path, doc_id, state, llm=llm, profile=profile)
    except Exception as e:  # one unreadable PDF must not stop a run of hundreds
        detail = f"{type(e).__name__}: {e}"[:200]
        return DocResult(doc_id or Path(path).stem, path, state, None, detail, "error", 0, 0, [])


def extract_all(jobs: list[Job], llm: Any = None, workers: int = 4,
                cache: DocCache | None = None,
                profiles: Profiles | None = None) -> list[DocResult]:  # fmt: skip
    """Extract documents in parallel: processes for parsing and OCR, threads when an LLM is used
    (provider clients don't cross process boundaries, and the calls are network-bound).

    With a ``cache``, documents extracted before (same bytes, same extractor) are loaded instead,
    so a run's cost follows the number of new PDFs, not the size of the archive. ``profiles``
    maps a file's path to its source profile (spreadsheets, ``sheet.py``).
    """
    shas = [file_sha256(path) for path, _, _ in jobs]
    profs = [(profiles or {}).get(path) for path, _, _ in jobs]
    keys = [f"{sha}-{profile_key(p)}" if p else sha for sha, p in zip(shas, profs, strict=True)]
    results: list[DocResult | None] = [None] * len(jobs)
    if cache is not None:
        for i, (path, doc_id, state) in enumerate(jobs):
            results[i] = cache.get(keys[i], path, doc_id or Path(path).stem, state)
    todo = [i for i, r in enumerate(results) if r is None]
    if todo:
        spawn = mp.get_context("spawn")
        pool: Executor = (ThreadPoolExecutor(workers) if llm else
                          ProcessPoolExecutor(workers, mp_context=spawn))  # fmt: skip
        with pool as ex:
            done = ex.map(partial(_extract, llm=llm), [jobs[i] for i in todo],
                          [profs[i] for i in todo])  # fmt: skip
            for i, res in zip(todo, done, strict=True):
                res.sha256 = shas[i]
                results[i] = res
                if cache is not None:
                    cache.put(keys[i], res)
    out = [r for r in results if r is not None]
    for r, sha in zip(out, shas, strict=True):
        r.sha256 = sha
    return out


def _products(rows: pd.DataFrame) -> pd.DataFrame:
    """The dictionary's products, and every product found through RxNorm in these documents
    (its class is the one the documents file it under most often)."""
    known = pd.DataFrame([{"product": p.name, "ingredient": p.ingredient, "kind": p.kind,
                           "drug_class": p.drug_class, "rxcui": rxcui(p.name)}
                          for p in PRODUCTS])  # fmt: skip
    found = rows[rows["product"].notna() & ~rows["product"].isin(known["product"])]
    if found.empty:
        return known
    found = found.assign(drug_class=found["drug_class"].fillna(""))
    cls = found.groupby("product")["drug_class"].agg(lambda s: s.mode().iloc[0])
    first = found.drop_duplicates("product").set_index("product")
    extra = first[["ingredient", "kind", "rxcui"]].assign(drug_class=cls).reset_index()
    return pd.concat([known, extra[list(known.columns)]], ignore_index=True)


Decisions = dict[str, dict[str, Any]]


def tables(results: list[DocResult], source: str,
           decisions: Decisions | None = None) -> dict[str, pd.DataFrame]:  # fmt: skip
    """The warehouse tables. ``decisions``: reviewers' decisions by row key (``review.load``);
    what they measured is kept in ``formulary_rows.attrs["review"]``."""
    docs = pd.DataFrame([{**r.summary(), "source": source} for r in results])
    docs["state_name"] = docs["state"].map(STATES)
    docs["file"] = docs["path"].map(lambda p: Path(p).name)
    rows = pd.DataFrame([{**row.to_dict(), "effective": r.effective}
                         for r in results for row in r.rows])  # fmt: skip
    if rows.empty:
        rows = pd.DataFrame(columns=[c for c in SCHEMA["formulary_rows"] if c != "reviewed"])
    rows, stats = review_mod.apply(rows, decisions or {})
    rows = rows[list(SCHEMA["formulary_rows"])]
    rows.attrs["review"] = stats
    # the latest PDL per state is the access table; rows awaiting review stay out of it, and
    # rows a reviewer rejected (not drug rows) stay out of everything but formulary_rows
    latest = history.latest_pdls(docs)
    access = rows[rows["doc_id"].isin(latest) & rows["accepted"] & rows["product"].notna()].copy()
    access["state_name"] = access["state"].map(STATES)
    kept = rows[rows["reviewed"] != "reject"]
    review = kept[~kept["accepted"]]
    out = {
        "documents": docs[list(SCHEMA["documents"])],
        "formulary_rows": rows,
        "drug_access": access[list(SCHEMA["drug_access"])],
        "review_queue": review[list(SCHEMA["review_queue"])],
        "changes": changes_mod.detect(kept, docs),
        "drug_access_history": history.build(kept, docs),
        "products": _products(kept),
        "states": pd.DataFrame({"state": list(STATES), "state_name": list(STATES.values())}),
    }  # fmt: skip
    return out


def write_duckdb(tabs: dict[str, pd.DataFrame], path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp.duckdb")
    tmp.unlink(missing_ok=True)
    con = duckdb.connect(str(tmp))
    try:
        for name, df in tabs.items():
            con.register("_df", df)
            con.execute(f"CREATE TABLE {name} AS SELECT * FROM _df")
            con.unregister("_df")
    finally:
        con.close()
    tmp.replace(path)  # readers never see a half-written database
    reset_cache()
    return path


def summary(results: list[DocResult], tabs: dict[str, pd.DataFrame], seconds: float,
            source: str) -> dict[str, Any]:  # fmt: skip
    docs, rows = tabs["documents"], tabs["formulary_rows"]
    reasons: Counter[str] = Counter()
    for r in rows.loc[~rows["accepted"], "review_reasons"]:
        reasons.update(x for x in str(r).split("; ") if x)
    pages = int(docs["pages"].sum())
    return {
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "data": {
            "source": source,
            "documents": len(docs),
            "states": int(docs["state"].nunique()),
            "pages": pages,
            "ocr_pages": int(docs["ocr_pages"].sum()),
            "by_type": docs["doc_type"].value_counts().to_dict(),
            "rows": len(rows),
            "accepted": int(rows["accepted"].sum()),
            "review": int((~rows["accepted"]).sum()),
            "methods": rows["method"].value_counts().to_dict(),
        },
        "review_reasons": dict(reasons.most_common()),
        **({"review_decisions": rows.attrs["review"]} if rows.attrs.get("review") else {}),
        "failed_documents": [
            {"doc_id": r.doc_id, "error": r.title} for r in results if r.doc_type == "error"
        ],  # fmt: skip
        "changes_detected": len(tabs["changes"]),
        "speed": {
            "seconds": round(seconds, 1),
            "seconds_per_page": round(seconds / max(pages, 1), 3),
        },
        "llm_calls": sum(r.llm_calls for r in results),
    }


def build(settings: Settings | None = None, seed: int = 7, llm: Any = None,
          workers: int = 4) -> dict[str, Any]:  # fmt: skip
    """The synthetic corpus end to end, scored against its truth."""
    s = settings or get_settings()
    folder = corpus_path(s.db_path)
    if not (folder / "truth.json").exists():
        synth_corpus.build(folder, seed)
    truth = synth_corpus.load_truth(folder)
    jobs = [(str(folder / d["file"]), d["doc_id"], d["state"]) for d in truth["documents"]]
    t0 = time.perf_counter()
    results = extract_all(jobs, llm, workers)
    seconds = time.perf_counter() - t0
    tabs = tables(results, "synthetic", review_mod.load(review_mod.decisions_path(s.db_path)))
    out = summary(results, tabs, seconds, "synthetic")
    out["extraction"] = {"all": evaluate(results, truth)["groups"],
                         "test": evaluate(results, truth, "test")["groups"]}  # fmt: skip
    out["changes"] = changes_mod.evaluate(tabs["changes"], truth)
    out["extractor"] = "llm" if llm else "rules"
    write_duckdb(tabs, s.db_path)
    results_path(s.db_path).write_text(json.dumps(out, indent=1, default=str))
    return out


_LIVING = 730  # days: a file saved this long after the date it prints is a living list


def date_hints(results: list[DocResult], hints: dict[str, dict[str, str]]) -> None:
    """A date for documents that don't print one: from the source's title, else the file's
    original name ("20260701_UPDL...", "pdl10242025"). Each says where its date came from.

    A list updated in place under one title keeps the date it began ("Apple Health Preferred
    Drug List, Effective June 1, 2020", saved in 2026): when the file was saved more than two
    years after the date it prints, this version is dated by the file instead.
    """
    for r in results:
        h = hints.get(r.doc_id, {})
        r.first_seen = h.get("first_seen") or r.first_seen
        if r.effective and r.doc_type != "error":
            saved = modified(r.path)
            gap = (date.fromisoformat(saved) - date.fromisoformat(r.effective)).days if saved else 0
            if saved and gap > _LIVING:
                r.effective, r.effective_from = saved, "file date"
        if r.effective or r.doc_type == "error":
            continue
        for origin, text in (("source title", h.get("title", "")),
                             ("file name", h.get("name") or Path(r.path).stem)):  # fmt: skip
            if d := date_hint(text):
                r.effective, r.effective_from = d, origin  # rows take it in tables()
                break


def run_folder(folder: Path, settings: Settings | None = None,
               states: dict[str, str] | None = None, llm: Any = None, workers: int = 4,
               source: str = "pdf folder", cache: bool = True,
               hints: dict[str, dict[str, str]] | None = None,
               skip: set[str] | None = None,
               profiles: Profiles | None = None) -> dict[str, Any]:  # fmt: skip
    """Any folder of PDFs and spreadsheets (real state PDLs). ``states`` maps file stem ->
    state code; ``hints`` maps file stem -> {title, name, first_seen} from the fetch ledger;
    files whose stem is in ``skip`` are left out; ``profiles`` maps file stem -> the source
    profile a spreadsheet is read with.

    PDFs extracted by an earlier run (same bytes, same extractor) come from the cache next to
    the warehouse, so a weekly run only pays for what changed.
    """
    s = settings or get_settings()
    files = sorted(f for f in Path(folder).glob("**/*")
                   if f.suffix.lower() == ".pdf" or is_sheet(f))  # fmt: skip
    files = [f for f in files if f.stem not in (skip or set())]
    if not files:
        raise FileNotFoundError(f"no PDFs or spreadsheets in {folder}")
    jobs: list[Job] = [(str(f), f.stem, (states or {}).get(f.stem)) for f in files]
    store = DocCache(cache_path(s.db_path) / "docs", extractor_key(llm)) if cache else None
    t0 = time.perf_counter()
    by_path = {str(f): (profiles or {})[f.stem] for f in files if f.stem in (profiles or {})}
    results = extract_all(jobs, llm, workers, store, by_path)
    date_hints(results, hints or {})
    tabs = tables(results, source, review_mod.load(review_mod.decisions_path(s.db_path)))
    out = summary(results, tabs, time.perf_counter() - t0, source)
    out["extractor"] = "llm" if llm else "rules"
    if store is not None:
        out["cache"] = {"from_cache": store.hits, "extracted": len(jobs) - store.hits}
    write_duckdb(tabs, s.db_path)
    results_path(s.db_path).write_text(json.dumps(out, indent=1, default=str))
    return out


def load_results(settings: Settings | None = None) -> dict[str, Any]:
    s = settings or get_settings()
    path = results_path(s.db_path)
    if not path.exists():
        raise FileNotFoundError(f"No results at {path}. Run `pdlwatch build` first.")
    return json.loads(path.read_text())
