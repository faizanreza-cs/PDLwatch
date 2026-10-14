"""Build the synthetic corpus: PDFs plus a truth file that says what each one contains."""

from __future__ import annotations

import json
import tempfile
from dataclasses import asdict
from pathlib import Path
from typing import Any

import numpy as np

from pdlwatch.synth.render import render, scan
from pdlwatch.synth.states import STATES
from pdlwatch.synth.truth import Change, Document, plan


def build(out: Path, seed: int = 7, only: set[str] | None = None) -> dict[str, Any]:
    """Write ``out/pdfs/*.pdf`` and ``out/truth.json``; return the truth.

    ``only``: document ids to render (a small corpus for tests); the truth covers just those,
    and the changes of states with both versions included.
    """
    docs, changes = plan(seed)
    if only is not None:
        docs = [d for d in docs if d.doc_id in only]
        pairs = {d.state for d in docs if d.version == 2} & {
            d.state for d in docs if d.version == 1
        }
        changes = [c for c in changes if c.state in pairs]
    pdfs = out / "pdfs"
    pdfs.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        for d in docs:
            path = pdfs / f"{d.doc_id}.pdf"
            if d.scanned:  # noise seeded per document: the same scan in any subset
                rng = np.random.default_rng([seed, *d.doc_id.encode()])
                scan(render(d, Path(tmp) / path.name), path, rng)
            else:
                render(d, path)
    truth = to_truth(docs, changes, seed)
    (out / "truth.json").write_text(json.dumps(truth, indent=1))
    return truth


def to_truth(docs: list[Document], changes: list[Change], seed: int) -> dict[str, Any]:
    return {
        "seed": seed,
        "documents": [
            {**d.to_dict(), "state_name": STATES[d.state], "file": f"pdfs/{d.doc_id}.pdf"}
            for d in docs
        ],
        "changes": [asdict(c) for c in changes],
    }


def load_truth(folder: Path) -> dict[str, Any]:
    return json.loads((folder / "truth.json").read_text())
