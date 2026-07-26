"""
Visualization Agent

Role: turn analysis findings into charts that accurately represent what the
data supports — never force a chart where a plain statement fits better.

Design principles enforced here (spec section 3.4 / 8.4):
  - bar/grouped-bar axes always start at 0 (no truncated-axis distortion)
  - no dual y-axes, no 3D charts
  - sample size (n=) annotated on every chart
  - findings with recommended_chart == "none" get NO chart — they're
    surfaced as a plain-text conclusion instead
"""
from __future__ import annotations

import base64
import io
import uuid

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

from ..schemas import ChartSpec, Finding, SkippedFinding, VisualizationResult

plt.rcParams.update({
    "figure.facecolor": "#12161c",
    "axes.facecolor": "#12161c",
    "axes.edgecolor": "#3a4250",
    "axes.labelcolor": "#e6e9ef",
    "text.color": "#e6e9ef",
    "xtick.color": "#a9b2c3",
    "ytick.color": "#a9b2c3",
    "grid.color": "#252b36",
    "font.family": "DejaVu Sans",
    "font.size": 10.5,
    "savefig.facecolor": "#12161c",
})

ACCENT = "#5b8cff"
ACCENT_2 = "#ff8a5b"
PALETTE = ["#5b8cff", "#ff8a5b", "#5bd9a8", "#ffd15b", "#c17bff", "#ff5b8a"]


def _fig_to_base64(fig) -> str:
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=150, bbox_inches="tight")
    plt.close(fig)
    return base64.b64encode(buf.getvalue()).decode("ascii")


def _line_chart(df: pd.DataFrame, finding: Finding) -> tuple[str, int] | None:
    date_col, num_col = finding.related_columns[0], finding.related_columns[1]
    sub = df[[date_col, num_col]].dropna()
    dates = pd.to_datetime(sub[date_col], errors="coerce", format="mixed")
    sub = sub.assign(_d=dates).dropna(subset=["_d"]).sort_values("_d")
    if sub.empty:
        return None

    fig, ax = plt.subplots(figsize=(7.2, 4.2))
    ax.plot(sub["_d"], sub[num_col], color=ACCENT, linewidth=2.2, marker="o", markersize=3.5)
    ax.set_xlabel(date_col)
    ax.set_ylabel(num_col)
    ax.set_title(f"{num_col} over time", fontsize=12, fontweight="bold", loc="left")
    ax.grid(True, alpha=0.35, linewidth=0.6)
    for spine in ("top", "right"):
        ax.spines[spine].set_visible(False)
    fig.autofmt_xdate()
    return _fig_to_base64(fig), int(len(sub))


def _scatter_chart(df: pd.DataFrame, finding: Finding) -> tuple[str, int] | None:
    col_a, col_b = finding.related_columns[0], finding.related_columns[1]
    sub = df[[col_a, col_b]].dropna()
    if sub.empty:
        return None
    x, y = sub[col_a].astype(float), sub[col_b].astype(float)

    fig, ax = plt.subplots(figsize=(7.2, 4.2))
    ax.scatter(x, y, color=ACCENT, alpha=0.55, s=22, edgecolors="none")
    if x.std() > 0:
        z = np.polyfit(x, y, 1)
        xs = np.linspace(x.min(), x.max(), 50)
        ax.plot(xs, np.polyval(z, xs), color=ACCENT_2, linewidth=2, linestyle="--")
    ax.set_xlabel(col_a)
    ax.set_ylabel(col_b)
    ax.set_title(f"{col_a} vs {col_b}", fontsize=12, fontweight="bold", loc="left")
    ax.grid(True, alpha=0.35, linewidth=0.6)
    for spine in ("top", "right"):
        ax.spines[spine].set_visible(False)
    return _fig_to_base64(fig), int(len(sub))


def _grouped_bar_chart(df: pd.DataFrame, finding: Finding) -> tuple[str, int] | None:
    cat_col, num_col = finding.related_columns[0], finding.related_columns[1]
    sub = df[[cat_col, num_col]].dropna()
    if sub.empty:
        return None
    means = sub.groupby(cat_col)[num_col].mean().sort_values(ascending=False)
    counts = sub.groupby(cat_col)[num_col].count()

    fig, ax = plt.subplots(figsize=(7.2, 4.2))
    colors = [PALETTE[i % len(PALETTE)] for i in range(len(means))]
    bars = ax.bar(means.index.astype(str), means.values, color=colors, width=0.6)
    ax.set_ylim(bottom=0)  # never truncate a bar-chart axis
    ax.set_ylabel(f"mean {num_col}")
    ax.set_title(f"{num_col} by {cat_col}", fontsize=12, fontweight="bold", loc="left")
    ax.grid(True, axis="y", alpha=0.35, linewidth=0.6)
    for spine in ("top", "right"):
        ax.spines[spine].set_visible(False)
    plt.setp(ax.get_xticklabels(), rotation=20, ha="right")
    for bar, cat in zip(bars, means.index):
        ax.annotate(f"n={counts[cat]}", (bar.get_x() + bar.get_width() / 2, bar.get_height()),
                    textcoords="offset points", xytext=(0, 4), ha="center", fontsize=8, color="#a9b2c3")
    return _fig_to_base64(fig), int(len(sub))


CHART_BUILDERS = {
    "line": _line_chart,
    "scatter_with_trendline": _scatter_chart,
    "grouped_bar": _grouped_bar_chart,
}


def visualize(df: pd.DataFrame, findings: list[Finding]) -> VisualizationResult:
    charts: list[ChartSpec] = []
    skipped: list[SkippedFinding] = []

    for finding in findings:
        chart_type = finding.recommended_chart
        builder = CHART_BUILDERS.get(chart_type)
        if not builder:
            skipped.append(SkippedFinding(
                finding_id=finding.finding_id,
                reason="No correlation/effect found — best represented as a stated "
                       "conclusion rather than a chart with no visible pattern."
                       if chart_type == "none" else
                       f"No chart builder configured for type '{chart_type}'.",
            ))
            continue
        try:
            result = builder(df, finding)
        except Exception as exc:  # pragma: no cover - defensive
            result = None
        if result is None:
            skipped.append(SkippedFinding(
                finding_id=finding.finding_id,
                reason="Insufficient non-null overlapping data to render a reliable chart.",
            ))
            continue

        image_b64, n = result
        caption = finding.summary
        if finding.caveat:
            caption += f" (Note: {finding.caveat}.)"
        charts.append(ChartSpec(
            finding_id=finding.finding_id,
            chart_type=chart_type,
            image_base64=image_b64,
            caption=caption,
            n=n,
        ))

    return VisualizationResult(
        viz_id=f"viz_{uuid.uuid4().hex[:8]}",
        charts=charts,
        findings_without_charts=skipped,
    )
