"""README charts, rendered from ``results.json`` and the warehouse with matplotlib.

Palette: a validated categorical order; every chart carries a legend or direct labels, so no
meaning rests on colour alone.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
from matplotlib.colors import ListedColormap  # noqa: E402
from matplotlib.patches import Patch  # noqa: E402

from pdlwatch import pipeline  # noqa: E402
from pdlwatch.warehouse import get_warehouse  # noqa: E402

SLOTS = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#6250d6", "#e34948"]
SURFACE, INK, INK2, MUTED, GRID, AXIS = (
    "#fcfcfb", "#0b0b0b", "#52514e", "#898781", "#e1e0d9", "#c3c2b7"
)  # fmt: skip
NEUTRAL = "#e9e8e3"
GROUPS = [("family:grid", "Ruled tables"), ("family:columns", "Preferred / non-preferred lists"),
          ("family:tiered", "Tiered formularies"), ("family:narrative", "PA criteria prose"),
          ("scanned", "Scanned PDFs (any layout)"), ("overall", "All documents")]  # fmt: skip


def _axes(title: str, subtitle: str, size: tuple[float, float] = (8.0, 4.4)) -> tuple[Any, Any]:
    fig, ax = plt.subplots(figsize=size, dpi=200)
    fig.patch.set_facecolor(SURFACE)
    ax.set_facecolor(SURFACE)
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    for side in ("left", "bottom"):
        ax.spines[side].set_color(AXIS)
        ax.spines[side].set_linewidth(0.8)
    ax.tick_params(colors=MUTED, labelcolor=INK2, labelsize=8.5, length=0)
    ax.set_axisbelow(True)
    fig.text(0.012, 0.975, title, fontsize=12.5, fontweight="bold", color=INK, va="top")
    fig.text(0.012, 0.915, subtitle, fontsize=9, color=INK2, va="top")
    return fig, ax


def _save(fig: Any, path: Path) -> Path:
    fig.savefig(path, facecolor=SURFACE, bbox_inches="tight", pad_inches=0.15)
    plt.close(fig)
    return path


def rows_chart(res: dict[str, Any], path: Path) -> Path:
    """Where extracted rows end up, by document type: accepted and right, accepted and wrong,
    or sent to review."""
    g = res["extraction"]["test"]
    names, ok, wrong, review = [], [], [], []
    for key, label in GROUPS:
        m = g.get(key)
        if not m or not m["extracted"]:
            continue
        names.append(label)
        ok.append(m["accepted_ok"] / m["extracted"])
        wrong.append((m["accepted"] - m["accepted_ok"]) / m["extracted"])
        review.append((m["extracted"] - m["accepted"]) / m["extracted"])
    fig, ax = _axes(
        "Tables and lists go straight through; prose and damaged scans go to a person",
        "Held-out test states of the synthetic corpus: every extracted row, accepted and right, "
        "accepted but wrong, or sent to review",
        size=(8.6, 4.1),
    )
    y = np.arange(len(names))[::-1]
    left = np.zeros(len(names))
    parts = [(ok, SLOTS[0], "Accepted, correct"), (wrong, SLOTS[7], "Accepted, wrong"),
             (review, "#b9b7ae", "Sent to review")]  # fmt: skip
    for values, color, label in parts:
        ax.barh(y, values, left=left, color=color, height=0.6, edgecolor=SURFACE, linewidth=1.5,
                label=label)  # fmt: skip
        left += np.asarray(values)
    for yi, a, w in zip(y, ok, wrong, strict=True):
        ax.text(1.01, yi, f"{a:.0%} straight through, {w:.1%} wrong", va="center",
                fontsize=8, color=INK2)  # fmt: skip
    ax.set_yticks(y, names)
    ax.set_xlim(0, 1)
    ax.set_xticks([0, 0.25, 0.5, 0.75, 1], ["0%", "25%", "50%", "75%", "100%"])
    ax.grid(axis="x", color=GRID, linewidth=0.6)
    ax.legend(loc="upper center", bbox_to_anchor=(0.5, -0.1), ncol=3, frameon=False, fontsize=8.5)
    fig.subplots_adjust(top=0.8, right=0.72)
    return _save(fig, path)


def atlas_chart(path: Path, drug_class: str = "GLP-1 Receptor Agonists") -> Path:
    """One class across every state: the access map a market access team starts from."""
    wh = get_warehouse()
    q = drug_class.replace("'", "''")
    df = wh.query(f"SELECT state, product, status FROM drug_access WHERE drug_class = '{q}'")
    states = wh.query("SELECT state FROM states ORDER BY state")["state"].tolist()
    products = (df.groupby("product")["status"].apply(lambda s: (s == "preferred").mean())
                .sort_values(ascending=False).index.tolist())  # fmt: skip
    code = {"preferred": 0, "non_preferred": 1, "not_covered": 2}
    grid = np.full((len(products), len(states)), 3)
    for r in df.itertuples():
        grid[products.index(r.product), states.index(r.state)] = code[r.status]
    fig, ax = _axes(
        f"{drug_class}: preferred in some states, prior authorization in most",
        "Synthetic corpus (fictional statuses): each state's latest PDL, as extracted; blank = "
        "not listed or waiting for review",
        size=(11.5, 3.6),
    )
    cmap = ListedColormap([SLOTS[0], SLOTS[1], "#5c5b56", NEUTRAL])
    ax.imshow(grid, cmap=cmap, vmin=0, vmax=3, aspect="auto", interpolation="nearest")
    ax.set_xticks(range(len(states)), states, fontsize=6.5, rotation=90)
    ax.set_yticks(range(len(products)), products, fontsize=8.5)
    ax.set_xticks(np.arange(-0.5, len(states)), minor=True)
    ax.set_yticks(np.arange(-0.5, len(products)), minor=True)
    ax.grid(which="minor", color=SURFACE, linewidth=1.2)
    ax.tick_params(which="minor", length=0)
    for side in ("left", "bottom"):
        ax.spines[side].set_visible(False)
    legend = [Patch(color=SLOTS[0], label="Preferred"),
              Patch(color=SLOTS[1], label="Non-preferred (PA)"),
              Patch(color="#5c5b56", label="Not covered"),
              Patch(facecolor=NEUTRAL, edgecolor=AXIS, label="Not listed / in review")]  # fmt: skip
    legend = [h for i, h in enumerate(legend) if (grid == i).any()]
    ax.legend(handles=legend, loc="upper center", bbox_to_anchor=(0.5, -0.22), ncol=len(legend),
              frameon=False, fontsize=8.5)  # fmt: skip
    fig.subplots_adjust(top=0.78)
    return _save(fig, path)


def sample_pages(path: Path, corpus: Path) -> Path:
    """The first page of one document per layout, top half, side by side."""
    import pypdfium2 as pdfium

    from pdlwatch.synth.corpus import load_truth

    truth = load_truth(corpus)
    picks: dict[str, str] = {}
    # a held-out (test) document of each kind where there is one, else any
    for d in sorted(truth["documents"], key=lambda d: d["split"] != "test"):
        key = "scanned" if d["scanned"] else d["family"]
        if key not in picks and not d["style"].get("landscape"):
            picks[key] = d["file"]
    order = [(k, label) for k, label in [
        ("grid", "Structured: ruled table"), ("columns", "Semi-structured: lists"),
        ("tiered", "Semi-structured: tiers"), ("narrative", "Unstructured: prose"),
        ("scanned", "Scanned: no text layer")] if k in picks]  # fmt: skip
    fig, axes = plt.subplots(1, len(order), figsize=(2.8 * len(order), 4.6), dpi=170,
                             squeeze=False)  # fmt: skip
    axes = axes[0]
    fig.patch.set_facecolor(SURFACE)
    for ax, (key, label) in zip(axes, order, strict=True):
        pdf = pdfium.PdfDocument(str(corpus / picks[key]))
        img = pdf[0].render(scale=1.6).to_pil()
        pdf.close()
        img = img.crop((0, 0, img.width, int(img.height * 0.5)))
        ax.imshow(img)
        ax.set_title(label, fontsize=10, color=INK, pad=6)
        ax.axis("off")
    fig.subplots_adjust(wspace=0.04, left=0.01, right=0.99, top=0.9, bottom=0.01)
    return _save(fig, path)


def render(out: str | Path = "docs/img") -> list[Path]:
    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    res = pipeline.load_results()
    paths = []
    if "extraction" in res:
        paths.append(rows_chart(res, out / "where_rows_go.png"))
        from pdlwatch.config import corpus_path, get_settings

        paths.append(sample_pages(out / "sample_pages.png", corpus_path(get_settings().db_path)))
    paths.append(atlas_chart(out / "glp1_atlas.png"))
    return paths
