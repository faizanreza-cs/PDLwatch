"""Draw each synthetic document as a PDF, in the layouts state PDLs actually use.

* **grid** (structured): a ruled table, one drug per row, status / PA / QL / ST columns.
* **columns** (semi-structured): per class, PREFERRED and NON-PREFERRED lists side by side, no
  ruling, footnote markers explained in a legend.
* **tiered** (semi-structured): a managed-care style formulary: drug, tier, requirements.
* **narrative** (unstructured): prior authorisation criteria written as prose.
* **scanned**: any of the above, printed and scanned: an image with no text layer.

Every page carries a banner saying the document is synthetic. No agency names or logos.
"""

from __future__ import annotations

import io
from pathlib import Path
from typing import Any
from xml.sax.saxutils import escape

import numpy as np
from reportlab.lib import colors
from reportlab.lib.pagesizes import landscape, letter
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import inch
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from pdlwatch.drugs import BY_NAME
from pdlwatch.synth.states import STATES
from pdlwatch.synth.truth import Document, Policy

BANNER = ("SYNTHETIC TEST DOCUMENT: fictional statuses generated for software testing. "
          "Not issued by any state agency.")  # fmt: skip
_DATES = {"2026-07-01": ["July 1, 2026", "07/01/2026", "2026-07-01"],
          "2026-10-01": ["October 1, 2026", "10/01/2026", "2026-10-01"]}  # fmt: skip
GRID_STATUS = {
    "word": {"preferred": "Preferred", "non_preferred": "Non-Preferred", "not_covered": "Not Covered"},
    "code": {"preferred": "P", "non_preferred": "NP", "not_covered": "NC"},
    "yesno": {"preferred": "Yes", "non_preferred": "No", "not_covered": "Not covered"},
    "pdl": {"preferred": "PDL", "non_preferred": "Non-PDL", "not_covered": "Excluded"},
}  # fmt: skip


def _bold(font: str) -> str:
    return "Times-Bold" if font.startswith("Times") else "Helvetica-Bold"


def _title(doc: Document) -> str:
    name = STATES[doc.state]
    return {
        "grid": f"{name} Medicaid Preferred Drug List",
        "columns": f"{name} Medicaid Preferred Drug List",
        "tiered": f"{name} Medicaid Managed Care Common Formulary",
        "narrative": f"{name} Medicaid Prior Authorization Criteria",
    }[doc.family]


def _date(doc: Document) -> str:
    options = _DATES[doc.effective]
    return options[sum(map(ord, doc.state)) % len(options)]  # a state keeps one format


def _drug(product: str, style: dict[str, Any]) -> str:
    p = BY_NAME[product]
    if p.kind == "generic":
        return p.name
    if style.get("name_style") == "upper":
        return p.name.upper()
    if style.get("name_style") == "with_ingredient":
        return f"{p.name} ({p.ingredient})"
    return p.name


def _page_decor(doc: Document, pagesize: tuple[float, float]) -> Any:
    title, date = _title(doc), _date(doc)
    font = doc.style.get("font", "Helvetica")

    def draw(canvas: Any, d: Any) -> None:
        w, h = pagesize
        canvas.saveState()
        canvas.setFont(_bold(font), 11)
        canvas.drawString(0.6 * inch, h - 0.55 * inch, title)
        canvas.setFont(font, 8.5)
        canvas.drawRightString(w - 0.6 * inch, h - 0.55 * inch, f"Effective {date}")
        canvas.setFont(font, 7)
        canvas.setFillColor(colors.HexColor("#9a2b2b"))
        canvas.drawString(0.6 * inch, h - 0.72 * inch, BANNER)
        canvas.setFillColor(colors.black)
        canvas.drawRightString(w - 0.6 * inch, 0.45 * inch, f"Page {d.page}")
        canvas.restoreState()

    return draw


def _para(text: str, font: str, size: float, bold: bool = False, **kw: Any) -> Paragraph:
    st = ParagraphStyle("p", fontName=_bold(font) if bold else font, fontSize=size,
                        leading=size * 1.22, **kw)  # fmt: skip
    return Paragraph(escape(text), st)


# --------------------------------------------------------------------------- grid


def _flag(on: bool, vocab: tuple[str, str]) -> str:
    return vocab[0] if on else vocab[1]


def grid(doc: Document) -> list[Any]:
    s = doc.style
    font, size = s["font"], s["size"]
    cols = s["columns"]
    words = GRID_STATUS[s["status_vocab"]]
    legend = {
        "word": "Non-Preferred drugs require prior authorization (PA).",
        "code": "P = Preferred; NP = Non-Preferred (prior authorization required); NC = Not covered.",
        "yesno": "Preferred? Yes = preferred; No = non-preferred, prior authorization required.",
        "pdl": "PDL = preferred; Non-PDL = non-preferred, prior authorization required.",
    }[s["status_vocab"]]
    policy = (
        sorted(doc.policy, key=lambda p: p.product.lower()) if s["alphabetical"] else doc.policy
    )
    every = s["class_every_row"] or s["alphabetical"]
    rows: list[list[Any]] = [[_para(c, font, size, bold=True) for c in cols]]
    last = None
    for p in policy:
        cls = p.drug_class if (every or p.drug_class != last) else ""
        last = p.drug_class
        cells = [cls, _drug(p.product, s), words[p.status], _flag(p.pa, s["flag_vocab"]),
                 p.ql_detail if p.ql else s["flag_vocab"][1]]  # fmt: skip
        if len(cols) == 6:
            cells.append(_flag(p.st, s["flag_vocab"]))
        rows.append([_para(str(c), font, size) for c in cells])
    width = (10.0 if s["landscape"] else 7.3) * inch
    shares = [0.24, 0.27, 0.14, 0.11, 0.16, 0.08][: len(cols)]
    total = sum(shares)
    table = Table(rows, colWidths=[width * x / total for x in shares], repeatRows=1)
    table.setStyle(TableStyle([
        ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#808080")),
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#e3e6ea")),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("TOPPADDING", (0, 0), (-1, -1), 2), ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
    ]))  # fmt: skip
    return [_para(legend, font, size), Spacer(1, 6), table]


# --------------------------------------------------------------------------- columns


def _marked(p: Policy, s: dict[str, Any]) -> str:
    m = s["markers"]
    name = _drug(p.product, s)
    tags = []
    if m["pa"] and p.pa and p.status == "preferred":
        tags.append(m["pa"])
    if m["ql"] and p.ql:
        tags.append(m["ql"])
    if m["st"] and p.st:
        tags.append(m["st"])
    if m["bvg"] and p.bvg:
        tags.append(m["bvg"])
    return (
        name + ("".join(tags) if all(t in "*†‡" for t in tags) else " " + " ".join(tags))
        if tags
        else name
    )


def _legend_columns(s: dict[str, Any]) -> str:
    m = s["markers"]
    parts = []
    meaning = {"pa": "Prior authorization required (clinical criteria)", "ql": "Quantity limit applies",
               "st": "Step therapy applies", "bvg": "Brand preferred over generic"}  # fmt: skip
    for k in ("pa", "ql", "st", "bvg"):
        if m[k]:
            parts.append(f"{m[k]} = {meaning[k]}")
    parts.append("All non-preferred agents require prior authorization")
    return ". ".join(parts) + "."


CRITERIA = [
    "Non-preferred agents require trial and failure of two preferred agents.",
    "Approval requires a diagnosis consistent with the FDA-approved indication.",
    "Prescriber must document a contraindication to the preferred agents.",
]


def columns(doc: Document) -> list[Any]:
    s = doc.style
    font, size = s["font"], s["size"]
    head_pref, head_non = s["headers"]
    crit = s["criteria_column"]
    out: list[Any] = [_para(_legend_columns(s), font, size), Spacer(1, 8)]
    width = 7.3 * inch
    widths = [width * 0.36, width * 0.36, width * 0.28] if crit else [width / 2, width / 2]
    by_class: dict[str, list[Policy]] = {}
    for p in doc.policy:
        by_class.setdefault(p.drug_class, []).append(p)
    for k, (cls, ps) in enumerate(by_class.items()):
        excluded = all(p.status == "not_covered" for p in ps)
        bar = [_para(cls.upper(), font, size + 0.5, bold=True, textColor=colors.white)]
        bar += [""] * (len(widths) - 1)
        if excluded:
            header = [_para("NOT COVERED", font, size, bold=True)] + [""] * (len(widths) - 1)
            lists = [[_marked(p, s) for p in ps], []]
        else:
            header = [_para(head_pref, font, size, bold=True),
                      _para(head_non, font, size, bold=True)]  # fmt: skip
            if crit:
                header.append(_para("PA CRITERIA", font, size, bold=True))
            lists = [[_marked(p, s) for p in ps if p.status == "preferred"],
                     [_marked(p, s) for p in ps if p.status == "non_preferred"]]  # fmt: skip
        n = max(len(lists[0]), len(lists[1]))
        rows: list[list[Any]] = [bar, header]
        for i in range(n):
            row = [_para(lists[j][i], font, size) if i < len(lists[j]) else "" for j in (0, 1)]
            if crit:
                row.append(_para(CRITERIA[k % len(CRITERIA)], font, size - 0.5) if i == 0 and
                           not excluded else "")  # fmt: skip
            rows.append(row)
        table = Table(rows, colWidths=widths, repeatRows=2)
        table.setStyle(TableStyle([
            ("SPAN", (0, 0), (-1, 0)),
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#2f4a6d")),
            ("LINEBELOW", (0, 1), (-1, 1), 0.5, colors.HexColor("#808080")),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("TOPPADDING", (0, 0), (-1, -1), 1.5), ("BOTTOMPADDING", (0, 0), (-1, -1), 1.5),
        ]))  # fmt: skip
        out += [table, Spacer(1, 10)]
    return out


# --------------------------------------------------------------------------- tiered


def _tier(p: Policy, s: dict[str, Any]) -> str:
    if p.status == "not_covered":
        return str(s["excluded_label"])
    n = 3 if p.status == "non_preferred" else (1 if BY_NAME[p.product].kind == "generic" else 2)
    return {"number": str(n), "T": f"T{n}", "word": f"Tier {n}"}[s["tier_vocab"]]


def tiered(doc: Document) -> list[Any]:
    s = doc.style
    font, size = s["font"], s["size"]
    legend = (
        "Tier 1 = Preferred generic drugs. Tier 2 = Preferred brand drugs. Tier 3 = Non-preferred "
        f"drugs (prior authorization required). {s['excluded_label']} = Not on formulary "
        "(not covered). PA = Prior authorization. ST = Step therapy. QL = Quantity limit."
    )
    rows: list[list[Any]] = [[_para(c, font, size, bold=True) for c in s["columns"]]]
    spans = []
    last = None
    for p in doc.policy:
        if p.drug_class != last:
            rows.append([_para(p.drug_class, font, size + 0.5, bold=True), "", ""])
            spans.append(len(rows) - 1)
            last = p.drug_class
        req = []
        if p.pa:
            req.append("PA")
        if p.st:
            req.append("ST")
        if p.ql:
            req.append(f"QL ({p.ql_detail})")
        rows.append([_para(_drug(p.product, s), font, size), _para(_tier(p, s), font, size),
                     _para(s["sep"].join(req), font, size)])  # fmt: skip
    width = 7.3 * inch
    table = Table(rows, colWidths=[width * 0.46, width * 0.14, width * 0.40], repeatRows=1)
    style = [
        ("LINEBELOW", (0, 0), (-1, 0), 0.6, colors.black),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("TOPPADDING", (0, 0), (-1, -1), 1.2), ("BOTTOMPADDING", (0, 0), (-1, -1), 1.2),
    ]  # fmt: skip
    for r in spans:
        style += [("SPAN", (0, r), (-1, r)), ("TOPPADDING", (0, r), (-1, r), 7)]
    table.setStyle(TableStyle(style))
    return [_para(legend, font, size), Spacer(1, 8), table]


# --------------------------------------------------------------------------- narrative


def narrative(doc: Document) -> list[Any]:
    font, size = doc.style["font"], doc.style["size"]
    name = STATES[doc.state]
    out: list[Any] = [_para(
        f"This document sets out the prior authorization criteria for selected classes on the "
        f"{name} Medicaid Preferred Drug List. Prescribers should submit requests through the "
        "pharmacy prior authorization line.", font, size), Spacer(1, 8)]  # fmt: skip
    for sec in doc.text:
        out.append(_para(sec["heading"], font, size + 1.5, bold=True, spaceBefore=6, spaceAfter=3))
        sentences = sec["sentences"]
        cut = max(1, len(sentences) // 2) if len(sentences) > 3 else len(sentences)
        for chunk in (sentences[:cut], sentences[cut:]):
            if chunk:
                out.append(_para(" ".join(chunk), font, size, spaceAfter=4))
    return out


# --------------------------------------------------------------------------- output

_BUILDERS = {"grid": grid, "columns": columns, "tiered": tiered, "narrative": narrative}


def render(doc: Document, path: Path) -> Path:
    pagesize = landscape(letter) if doc.style.get("landscape") else letter
    buf = SimpleDocTemplate(str(path), pagesize=pagesize, leftMargin=0.6 * inch,
                            rightMargin=0.6 * inch, topMargin=0.95 * inch,
                            bottomMargin=0.7 * inch, title=_title(doc),
                            author="PDLwatch synthetic corpus")  # fmt: skip
    decor = _page_decor(doc, pagesize)
    buf.build(_BUILDERS[doc.family](doc), onFirstPage=decor, onLaterPages=decor)
    return path


def scan(src: Path, dst: Path, rng: np.random.Generator, dpi: int = 200) -> Path:
    """Print-and-scan: render each page to an image, tilt it, add noise, keep no text layer."""
    import pypdfium2 as pdfium
    from PIL import Image, ImageFilter

    pdf = pdfium.PdfDocument(str(src))
    pages = []
    for page in pdf:
        img = page.render(scale=dpi / 72).to_pil().convert("L")
        img = img.rotate(float(rng.uniform(-0.8, 0.8)), resample=Image.Resampling.BICUBIC,
                         fillcolor=255)  # fmt: skip
        arr = np.asarray(img, dtype=np.float32)
        arr = arr + rng.normal(0, 9, arr.shape)
        img = Image.fromarray(np.clip(arr, 0, 255).astype(np.uint8))
        img = img.filter(ImageFilter.GaussianBlur(0.5))
        jpg = io.BytesIO()
        img.save(jpg, format="JPEG", quality=70)
        pages.append(Image.open(io.BytesIO(jpg.getvalue())))
    pdf.close()
    pages[0].save(dst, format="PDF", save_all=True, append_images=pages[1:], resolution=dpi)
    return dst
