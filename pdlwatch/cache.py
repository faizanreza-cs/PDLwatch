"""Extraction results cached by content, so a run only extracts the PDFs it hasn't seen.

A document's key is the sha256 of its bytes plus a hash of everything that shapes its rows: the
extraction code (``pdf.py``, ``sheet.py``, ``drugs.py``, ``extract/``), the Tesseract version and
whether an LLM read the prose (and which model); a spreadsheet read through a source profile adds
the profile's hash (``profile_key``). Change any of them and the old entries simply stop
matching, so a cache never serves rows from an older extractor.

Only complete results are cached: a scan whose pages couldn't be OCR'd is extracted again
next time, when OCR may be available.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import asdict, replace
from functools import lru_cache
from pathlib import Path
from typing import Any

from pdlwatch.extract.document import DocResult
from pdlwatch.extract.rows import Row

_PKG = Path(__file__).resolve().parent


def file_sha256(path: str | Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


@lru_cache(maxsize=1)
def _code_hash() -> str:
    h = hashlib.sha256()
    files = [
        _PKG / "pdf.py",
        _PKG / "sheet.py",
        _PKG / "drugs.py",
        *(_PKG / "extract").glob("*.py"),
    ]
    for f in sorted(files):
        h.update(f.name.encode() + f.read_bytes())
    try:
        import pytesseract

        h.update(str(pytesseract.get_tesseract_version()).encode())
    except Exception:  # no OCR here: scans won't be complete, so they won't be cached anyway
        h.update(b"no-ocr")
    return h.hexdigest()


def profile_key(profile: dict[str, Any]) -> str:
    """A short hash of a source profile as written (its column order sets the page layout): a
    file read through another profile is another entry."""
    return hashlib.sha256(json.dumps(profile).encode()).hexdigest()[:12]


def extractor_key(llm: Any = None) -> str:
    """What the cached rows depend on besides the file: code, OCR engine, LLM model and the
    RxNorm lexicon."""
    from pdlwatch import rxnorm

    prov = getattr(llm, "provider", None)
    model = f"{getattr(prov, 'name', '')}:{getattr(prov, 'model', '')}" if llm else ""
    key = f"{_code_hash()}|{model}|{rxnorm.fingerprint()}"
    return hashlib.sha256(key.encode()).hexdigest()[:16]


class DocCache:
    def __init__(self, folder: str | Path, key: str) -> None:
        self.folder = Path(folder)
        self.key = key
        self.hits = 0

    def _path(self, sha: str) -> Path:
        return self.folder / f"{sha}-{self.key}.json"

    def get(self, sha: str, path: str, doc_id: str, state: str | None) -> DocResult | None:
        p = self._path(sha)
        if not p.exists():
            return None
        d = json.loads(p.read_text())
        rows = [Row(**r) for r in d.pop("rows")]
        res = DocResult(**d, rows=rows)
        state = state or res.state
        for r in rows:  # the same content can arrive under another name
            r.doc_id, r.state = doc_id, state
        self.hits += 1
        return replace(res, doc_id=doc_id, path=path, state=state)

    def put(self, sha: str, res: DocResult) -> None:
        if not res.complete or res.doc_type == "error":
            return
        self.folder.mkdir(parents=True, exist_ok=True)
        tmp = self._path(sha).with_suffix(f".tmp{os.getpid()}")
        tmp.write_text(json.dumps(asdict(res)))
        tmp.replace(self._path(sha))  # readers never see half a file
