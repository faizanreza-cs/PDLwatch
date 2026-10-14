"""RxNorm names for the drugs the hand-built dictionary doesn't know.

Real PDLs list thousands of products; the dictionary has 193. The rest are read against a lexicon
built from NLM's RxNorm Current Prescribable Content, which is free and needs no license: every
ingredient (IN, PIN, MIN) and brand name (BN), each brand with its ingredients, and each with its
RxCUI. ``pdlwatch rxnorm`` downloads the monthly release and writes the lexicon; extraction uses
it when ``PDLWATCH_RXNORM`` points at the file (the real-data workflow sets it). Nothing from
RxNorm is committed to the repository.

A whole-cell match counts like a dictionary match. A name found inside a longer cell goes to
review with the words the lexicon doesn't know, as it does for the dictionary.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import zipfile
from datetime import date, timedelta
from functools import lru_cache
from pathlib import Path
from typing import Any

URL = "https://download.nlm.nih.gov/rxnorm/RxNorm_full_prescribe_{d:%m%d%Y}.zip"
# term types kept, best first when two share a name: an ingredient reading beats a brand
TTYS = {"IN": "generic", "PIN": "generic", "MIN": "generic", "BN": "brand"}
_MAX_WORDS = 6  # longest name looked for inside a cell


def release_dates(today: date | None = None, months: int = 3) -> list[date]:
    """Releases come out on the first Monday of the month: this month's and those before."""
    today = today or date.today()
    out = []
    y, m = today.year, today.month
    for _ in range(months):
        first = date(y, m, 1)
        monday = first + timedelta(days=(7 - first.weekday()) % 7)
        if monday <= today:
            out.append(monday)
        y, m = (y, m - 1) if m > 1 else (y - 1, 12)
    return out


def download(folder: Path, client: Any = None, today: date | None = None) -> Path:
    """The newest release zip in ``folder``, downloading it when it isn't there yet."""
    import httpx

    folder.mkdir(parents=True, exist_ok=True)
    own = client is None
    client = client or httpx.Client(
        timeout=600, follow_redirects=True,
        headers={"User-Agent": "pdlwatch/0.1 (+https://github.com/faizanreza-cs/pdlwatch)"},
    )  # fmt: skip
    try:
        for d in release_dates(today):
            url = URL.format(d=d)
            dest = folder / url.rsplit("/", 1)[-1]
            if dest.exists():
                return dest
            tmp = dest.with_suffix(".part")
            with client.stream("GET", url) as r:
                if r.status_code != 200:
                    continue
                with tmp.open("wb") as f:
                    for chunk in r.iter_bytes():
                        f.write(chunk)
            if zipfile.is_zipfile(tmp):
                tmp.replace(dest)
                return dest
            tmp.unlink(missing_ok=True)
    finally:
        if own:
            client.close()
    raise RuntimeError("no RxNorm prescribable release found for the last three months")


def build(zip_path: Path) -> dict[str, Any]:
    """The lexicon: {normalised name: {name, kind, ingredient, rxcui, tty}} from RXNCONSO and
    RXNREL (brands to their ingredients through tradename relations)."""
    from pdlwatch.drugs import normalise

    names: dict[str, tuple[str, str]] = {}  # rxcui -> (tty, name)
    ingredients: dict[str, set[str]] = {}  # brand rxcui -> ingredient names
    with zipfile.ZipFile(zip_path) as z:
        files = z.namelist()
        conso = next(n for n in files if n.endswith("RXNCONSO.RRF"))
        rel = next(n for n in files if n.endswith("RXNREL.RRF"))
        with z.open(conso) as f:
            for line in io.TextIOWrapper(f, encoding="utf-8"):
                p = line.split("|")
                # RXCUI|LAT|TS|LUI|STT|SUI|ISPREF|RXAUI|SAUI|SCUI|SDUI|SAB|TTY|CODE|STR|SRL|SUPPRESS
                if len(p) > 16 and p[11] == "RXNORM" and p[12] in TTYS and p[16] == "N":
                    names.setdefault(p[0], (p[12], p[14]))
        with z.open(rel) as f:
            for line in io.TextIOWrapper(f, encoding="utf-8"):
                p = line.split("|")
                # RXCUI1|RXAUI1|STYPE1|REL|RXCUI2|RXAUI2|STYPE2|RELA|...
                if len(p) < 8 or p[7] not in {"tradename_of", "has_tradename"}:
                    continue
                for a, b in ((p[0], p[4]), (p[4], p[0])):
                    tb = names.get(b, ("",))[0]
                    if names.get(a, ("",))[0] == "BN" and tb in TTYS and tb != "BN":
                        ingredients.setdefault(a, set()).add(names[b][1])
    order = list(TTYS)
    lex: dict[str, dict[str, str]] = {}
    for cui, (tty, name) in sorted(names.items(), key=lambda kv: order.index(kv[1][0])):
        key = normalise(name)
        if not key or key in lex:
            continue
        ing = name if tty != "BN" else " / ".join(sorted(ingredients.get(cui, set())))
        lex[key] = {"name": name, "kind": TTYS[tty], "ingredient": ing.lower(), "rxcui": cui,
                    "tty": tty}  # fmt: skip
    return {"source": zip_path.name, "names": lex}


def save(lexicon: dict[str, Any], path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(lexicon))
    tmp.replace(path)
    return path


@lru_cache(maxsize=2)
def _load(path: str, mtime: float) -> dict[str, dict[str, str]]:
    names: dict[str, dict[str, str]] = json.loads(Path(path).read_text())["names"]
    return names


def _path() -> Path | None:
    p = os.environ.get("PDLWATCH_RXNORM")
    return Path(p) if p and Path(p).is_file() else None


def lexicon() -> dict[str, dict[str, str]]:
    """The lexicon ``PDLWATCH_RXNORM`` points at, or nothing."""
    p = _path()
    return _load(str(p), p.stat().st_mtime) if p else {}


def fingerprint() -> str:
    """What extraction results depend on: the lexicon's content (empty without one)."""
    p = _path()
    return hashlib.sha256(p.read_bytes()).hexdigest()[:16] if p else ""


def exact(keys: list[str]) -> dict[str, str] | None:
    lex = lexicon()
    return next((lex[k] for k in keys if k and k in lex), None)


def contained(n: str) -> dict[str, str] | None:
    """The longest RxNorm name inside ``n`` (whole words), if any; one-word names must be
    longer than four letters, so "gel" or "acid" never match on their own."""
    lex = lexicon()
    if not lex:
        return None
    words = n.split()
    for size in range(min(_MAX_WORDS, len(words)), 0, -1):
        for i in range(len(words) - size + 1):
            key = " ".join(words[i : i + size])
            if key in lex and (size > 1 or len(key) > 4):
                return lex[key]
    return None
