"""How fast extraction runs on this machine: ``pdlwatch benchmark``.

Reads the synthetic corpus (or ``--folder`` of PDFs) once with each worker count, then once more
from a warm cache, and reports documents, pages and OCR'd pages per minute. Scanned pages cost
the most (OCR), so the mix matters: the report says how many there were. The cached pass is what
a daily or weekly sync costs when no state has posted anything new.
"""

from __future__ import annotations

import os
import tempfile
import time
from pathlib import Path
from typing import Any

from pdlwatch.cache import DocCache, extractor_key
from pdlwatch.pipeline import Job, extract_all


def jobs_for(folder: Path) -> list[Job]:
    return [(str(f), f.stem, None) for f in sorted(folder.glob("**/*.pdf"))]


def run(jobs: list[Job], workers: tuple[int, ...] = (1, 4)) -> dict[str, Any]:
    """Throughput per worker count, and the time of a run whose documents are all cached."""
    if not jobs:
        raise ValueError("no PDFs to read")
    passes = []
    for w in workers:
        t0 = time.perf_counter()
        results = extract_all(jobs, workers=w)
        seconds = time.perf_counter() - t0
        pages = sum(r.pages for r in results)
        passes.append({
            "workers": w, "seconds": round(seconds, 1),
            "documents_per_minute": round(len(results) / seconds * 60, 1),
            "pages_per_minute": round(pages / seconds * 60, 1),
            "pages_per_minute_per_worker": round(pages / seconds * 60 / w, 1),
        })  # fmt: skip
    with tempfile.TemporaryDirectory() as tmp:
        store = DocCache(tmp, extractor_key())
        extract_all(jobs, workers=max(workers), cache=store)  # fill the cache
        t0 = time.perf_counter()
        extract_all(jobs, workers=max(workers), cache=store)
        cached = time.perf_counter() - t0
    return {
        "documents": len(results),
        "pages": sum(r.pages for r in results),
        "ocr_pages": sum(r.ocr_pages for r in results),
        "cpus": os.cpu_count(),
        "passes": passes,
        "cached_seconds": round(cached, 2),
    }


def report(res: dict[str, Any]) -> str:
    lines = [f"{res['documents']} PDFs, {res['pages']} pages ({res['ocr_pages']} OCR'd), "
             f"{res['cpus']} CPUs"]  # fmt: skip
    for p in res["passes"]:
        lines.append(f"  {p['workers']} worker(s): {p['seconds']} s, "
                     f"{p['pages_per_minute']:,.0f} pages/min "
                     f"({p['pages_per_minute_per_worker']:,.0f} per worker)")  # fmt: skip
    lines.append(f"  everything cached (nothing new to read): {res['cached_seconds']} s")
    return "\n".join(lines)
