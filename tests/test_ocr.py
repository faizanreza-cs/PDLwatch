import numpy as np
import pytest
from conftest import HAS_OCR
from PIL import Image, ImageDraw

from pdlwatch import pdf


def _text_like_page() -> Image.Image:
    img = Image.new("L", (1200, 1600), 255)
    d = ImageDraw.Draw(img)
    for y in range(100, 1500, 40):  # dark bands where text lines would be
        d.rectangle((100, y, 1100, y + 14), fill=0)
    return img


def _profile(img: Image.Image) -> float:
    return float((np.asarray(img) < 128).sum(axis=1).astype(float).var())


def test_deskew_straightens_a_tilted_scan():
    page = _text_like_page()
    tilted = page.rotate(1.2, resample=Image.Resampling.BICUBIC, fillcolor=255)
    fixed = pdf.deskew(tilted)
    assert _profile(fixed) > 2 * _profile(tilted)
    assert pdf.deskew(page) is page  # a straight page is left alone


def test_scanned_page_without_ocr_is_skipped_with_a_warning(tmp_path, monkeypatch):
    path = tmp_path / "scan.pdf"
    _text_like_page().convert("RGB").save(path)  # an image only PDF: no text layer

    def missing(*a, **kw):
        raise OSError("tesseract is not installed")

    monkeypatch.setattr(pdf, "ocr_words", missing)
    with pytest.warns(UserWarning, match="scanned page skipped"):
        pages = pdf.load(path)
    assert len(pages) == 1 and pages[0].ocr and pages[0].lines == []


@pytest.mark.skipif(not HAS_OCR, reason="needs the tesseract binary")
def test_ocr_reads_words_with_positions(tmp_path):
    img = Image.new("L", (1700, 400), 255)
    d = ImageDraw.Draw(img)
    from PIL import ImageFont

    font = ImageFont.load_default(size=60)
    d.text((100, 100), "Ozempic", fill=0, font=font)
    d.text((900, 100), "Preferred", fill=0, font=font)
    words = pdf.ocr_words(img, 72 / 300)
    texts = [w.text for w in words]
    assert "Ozempic" in texts and "Preferred" in texts
    oz = words[texts.index("Ozempic")]
    assert abs(oz.x0 - 100 * 72 / 300) < 5  # boxes come back in PDF points
