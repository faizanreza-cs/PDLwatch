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


def test_an_icon_before_a_name_is_where_the_name_starts(tmp_path):
    """Vermont marks a new drug with a small image before its name: the name then starts at
    the image, in line with the names under it. An image over a word moves nothing."""
    from reportlab.lib.utils import ImageReader
    from reportlab.pdfgen import canvas

    path = tmp_path / "icons.pdf"
    c = canvas.Canvas(str(path), pagesize=(612, 792))
    c.drawString(78, 692, "Mounjaro (tirzepatide)")
    c.drawImage(ImageReader(Image.new("RGB", (11, 13), "red")), 79, 662, 11, 13)
    c.drawString(91, 664, "Ozempic (semaglutide) auto-injector pen")
    c.drawString(78, 632, "Agents: Proton Pump Inhibitors")
    c.drawImage(ImageReader(Image.new("RGB", (8, 10), "red")), 112, 630, 8, 10)
    c.save()
    words = {w.text: w for line in pdf.load(path)[0].lines for p in line.phrases for w in p.words}
    assert round(words["Ozempic"].x0) == 79
    assert words["Proton"].x0 > words["Agents:"].x1  # the image sits on "Agents:"


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
