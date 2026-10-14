"""Read a PDF page into positioned words, lines and phrases, from the text layer or by OCR.

Every extractor works on the same structure, so a scanned page (OCR word boxes) goes through
the same layout parsing as a digital one:

* **word**: text with its box (PDF points, origin top left), bold flag and OCR confidence;
* **line**: words that share a baseline, left to right;
* **phrase**: words on a line separated by ordinary spaces. A wider gap starts a new phrase,
  which is how table cells and side-by-side columns separate.
"""

from __future__ import annotations

import os
import statistics
import warnings
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pdfplumber

MIN_CHARS = 25  # fewer characters than this on a page: treat it as a scan
OCR_DPI = 300
MIN_OCR_CONF = 30


@dataclass
class Word:
    text: str
    x0: float
    x1: float
    top: float
    bottom: float
    bold: bool = False
    conf: float = 100.0


@dataclass
class Phrase:
    words: list[Word]

    @property
    def text(self) -> str:
        return " ".join(w.text for w in self.words)

    @property
    def x0(self) -> float:
        return self.words[0].x0

    @property
    def x1(self) -> float:
        return self.words[-1].x1

    @property
    def bold(self) -> bool:
        return all(w.bold for w in self.words)

    @property
    def conf(self) -> float:
        return min(w.conf for w in self.words)


@dataclass
class Line:
    phrases: list[Phrase]

    @property
    def text(self) -> str:
        return " ".join(p.text for p in self.phrases)

    @property
    def top(self) -> float:
        return min(w.top for p in self.phrases for w in p.words)

    @property
    def bottom(self) -> float:
        return max(w.bottom for p in self.phrases for w in p.words)

    @property
    def x0(self) -> float:
        return self.phrases[0].x0

    @property
    def bold(self) -> bool:
        return all(p.bold for p in self.phrases)


@dataclass
class Page:
    number: int  # 1-based
    width: float
    height: float
    lines: list[Line]
    ocr: bool
    image: Any = None  # the cleaned page image, kept on OCR'd pages for re-reading cells
    ocr_failed: bool = False  # a scanned page skipped because OCR wasn't available

    def ocr_region(self, x0: float, top: float, x1: float, bottom: float) -> str:
        """Re-OCR one cell as a single line of text (empty on digital pages)."""
        if self.image is None:
            return ""
        import pytesseract

        s = self.image.width / self.width
        box = (int(x0 * s), int(top * s), int(x1 * s), int(bottom * s))
        crop = self.image.crop(box)
        return " ".join(pytesseract.image_to_string(crop, config="--psm 7").split())

    @property
    def text(self) -> str:
        return "\n".join(line.text for line in self.lines)


def _lines(words: list[Word]) -> list[Line]:
    """Group words into lines by vertical overlap, then into phrases by horizontal gaps."""
    if not words:
        return []
    height = statistics.median(w.bottom - w.top for w in words)
    rows: list[list[Word]] = []
    for w in sorted(words, key=lambda w: ((w.top + w.bottom) / 2, w.x0)):
        mid = (w.top + w.bottom) / 2
        if rows:
            last = rows[-1]
            lmid = statistics.fmean((x.top + x.bottom) / 2 for x in last)
            if abs(mid - lmid) <= 0.45 * height:
                last.append(w)
                continue
        rows.append([w])
    out = []
    for row in rows:
        row.sort(key=lambda w: w.x0)
        h = statistics.median(w.bottom - w.top for w in row)
        gap = max(3.0, 0.6 * h)  # a space is ~0.3 em; a column gutter is wider
        phrases = [[row[0]]]
        for w in row[1:]:
            if w.x0 - phrases[-1][-1].x1 > gap:
                phrases.append([w])
            else:
                phrases[-1].append(w)
        out.append(Line([Phrase(p) for p in phrases]))
    return out


def deskew(image: Any) -> Any:
    """Straighten a scanned page: the angle whose horizontal ink profile is sharpest.

    Text lines are horizontal when the row sums of dark pixels vary the most. Searched on a
    quarter-size copy from -2 to +2 degrees in 0.1 degree steps.
    """
    import numpy as np
    from PIL import Image

    small = image.resize((max(1, image.width // 4), max(1, image.height // 4)))
    best, best_angle = -1.0, 0.0
    for angle in np.arange(-2.0, 2.01, 0.1):
        rotated = small.rotate(float(angle), resample=Image.Resampling.BILINEAR, fillcolor=255)
        rows = (np.asarray(rotated) < 128).sum(axis=1).astype(float)
        if rows.var() > best:
            best, best_angle = float(rows.var()), float(angle)
    if abs(best_angle) < 0.05:
        return image
    return image.rotate(best_angle, resample=Image.Resampling.BICUBIC, fillcolor=255)


def ocr_words(image: Any, scale: float) -> list[Word]:
    """Tesseract word boxes, scaled from image pixels to PDF points."""
    import pytesseract

    # one thread per Tesseract: pages are OCR'd in parallel processes, and nested threading
    # oversubscribes the CPU several times over
    os.environ.setdefault("OMP_THREAD_LIMIT", "1")
    data = pytesseract.image_to_data(image, config="--psm 11", output_type=pytesseract.Output.DICT)
    out = []
    for i, text in enumerate(data["text"]):
        text = text.strip()
        conf = float(data["conf"][i])
        # rule lines and speckles come back as "_", "|" or low-confidence fragments
        if not text or conf < MIN_OCR_CONF or all(c in "_|-—–~.,'\"`" for c in text):
            continue
        x, y, w, h = (data[k][i] for k in ("left", "top", "width", "height"))
        out.append(Word(text, x * scale, (x + w) * scale, y * scale, (y + h) * scale, False, conf))
    return out


def load(path: str | Path, ocr: bool = True) -> list[Page]:
    """All pages of a PDF. Pages without a text layer are OCR'd (when ``ocr`` is on)."""
    pages = []
    with pdfplumber.open(str(path)) as pdf:
        for n, pg in enumerate(pdf.pages, 1):
            if len(pg.chars) >= MIN_CHARS:
                raw = pg.extract_words(x_tolerance=1.5, y_tolerance=2.5, extra_attrs=["fontname"])
                words = [Word(w["text"], w["x0"], w["x1"], w["top"], w["bottom"],
                              "bold" in w["fontname"].lower()) for w in raw]  # fmt: skip
                pages.append(Page(n, pg.width, pg.height, _lines(words), False))
            elif ocr:
                # deskewed but otherwise untouched: binarising (Otsu) or median-filtering first
                # broke thin strokes on the dev scans and cost more accuracy than it gained
                img = deskew(pg.to_image(resolution=OCR_DPI).original.convert("L"))
                try:
                    words = ocr_words(img, 72 / OCR_DPI)
                except (ImportError, OSError) as e:  # no pytesseract, or no tesseract binary
                    warnings.warn(f"{Path(path).name} p.{n}: scanned page skipped, OCR "
                                  f"unavailable ({type(e).__name__})", stacklevel=2)  # fmt: skip
                    words = []
                    pages.append(Page(n, pg.width, pg.height, [], True, img, ocr_failed=True))
                    continue
                pages.append(Page(n, pg.width, pg.height, _lines(words), True, img))
            else:
                pages.append(Page(n, pg.width, pg.height, [], True))
    return pages
