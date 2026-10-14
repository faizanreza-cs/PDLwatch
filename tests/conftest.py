import os
import shutil

import pytest

HAS_OCR = shutil.which("tesseract") is not None


def _pick() -> set[str]:
    """One digital document per layout, a state with two digital PDL versions, one scan."""
    from pdlwatch.synth.truth import plan

    docs, _ = plan()
    by_id = {d.doc_id: d for d in docs}
    out: set[str] = set()
    for d in docs:  # a v1/v2 pair, both digital, for change detection
        v2 = by_id.get(d.doc_id.replace("2026-07", "2026-10"))
        if d.version == 1 and d.doc_type == "pdl" and v2 and not d.scanned and not v2.scanned:
            out |= {d.doc_id, v2.doc_id}
            break
    for family in ("grid", "columns", "tiered", "narrative"):
        if not any(by_id[i].family == family for i in out):
            out.add(next(d.doc_id for d in docs if d.family == family and not d.scanned))
    if HAS_OCR:  # the shortest scanned PDL
        scans = [d for d in docs if d.scanned and d.doc_type == "pdl"]
        out.add(min(scans, key=lambda d: len(d.rows)).doc_id)
    return out


@pytest.fixture(scope="session")
def built(tmp_path_factory):
    """A small corpus, extracted into its own warehouse (PDLWATCH_DB points at it)."""
    root = tmp_path_factory.mktemp("atlas")
    os.environ["PDLWATCH_DB"] = str(root / "pdlwatch.duckdb")
    os.environ["PDLWATCH_PROVIDER"] = "mock"
    from pdlwatch.pipeline import build
    from pdlwatch.synth import corpus

    truth = corpus.build(root / "corpus", only=_pick())
    res = build(workers=2)
    return {"root": root, "results": res, "truth": truth}
