"""
Analysis Agent

Role: run exploratory + statistical analysis on the CLEANED data and surface
the actually-interesting, ranked findings — not a dump of every statistic.

Every statistical claim here is backed by a real scipy computation (t-test,
ANOVA, Pearson correlation, linear trend test). The "agent" logic decides
*which* tests to run and how to rank/phrase results — it never estimates
significance from vibes.

Findings that touch columns the Cleaning Agent modified get an explicit
caveat, per spec section 3.3 ("this trend excludes the 47 rows dropped as
duplicates...").
"""
from __future__ import annotations

import uuid
from itertools import combinations

import numpy as np
import pandas as pd
from scipy import stats

from ..dtype_utils import textual_columns
from ..schemas import AnalysisResult, CleaningLog, Finding

MAX_FINDINGS = 6
CATEGORICAL_MAX_LEVELS = 12


def _find_date_column(df: pd.DataFrame) -> str | None:
    for col in df.columns:
        if pd.api.types.is_datetime64_any_dtype(df[col]):
            return col
    for col in textual_columns(df):
        parsed = pd.to_datetime(df[col], errors="coerce", format="mixed")
        if parsed.notna().mean() > 0.9:
            return col
    return None


def _numeric_columns(df: pd.DataFrame) -> list[str]:
    return [c for c in df.select_dtypes(include=np.number).columns]


def _categorical_columns(df: pd.DataFrame) -> list[str]:
    out = []
    for c in textual_columns(df):
        n = df[c].nunique(dropna=True)
        if 1 < n <= CATEGORICAL_MAX_LEVELS:
            out.append(c)
    return out


def _affected_by_cleaning(columns: list[str], cleaning_log: dict | None) -> str | None:
    if not cleaning_log:
        return None
    touched_cols = set()
    dropped_dupes = 0
    for a in cleaning_log.get("applied_actions", []):
        if a.get("column"):
            touched_cols.add(a["column"])
        if a.get("action") == "dropped_rows":
            dropped_dupes += a.get("count", 0)

    relevant = touched_cols.intersection(columns)
    parts = []
    if dropped_dupes:
        parts.append(f"analysis excludes {dropped_dupes} row(s) removed as duplicates during cleaning")
    if relevant:
        parts.append(f"column(s) {sorted(relevant)} were modified during cleaning (imputed/standardized) — "
                      f"worth confirming that didn't bias this result")
    return "; ".join(parts) if parts else None


def _trend_finding(df: pd.DataFrame, date_col: str, numeric_col: str, cleaning_log) -> Finding | None:
    sub = df[[date_col, numeric_col]].dropna()
    if len(sub) < 6:
        return None
    dates = pd.to_datetime(sub[date_col], errors="coerce", format="mixed")
    sub = sub.assign(_d=dates).dropna(subset=["_d"]).sort_values("_d")
    if len(sub) < 6:
        return None
    x = (sub["_d"] - sub["_d"].min()).dt.days.astype(float).values
    y = sub[numeric_col].astype(float).values
    if np.std(x) == 0:
        return None
    slope, intercept, r, p, se = stats.linregress(x, y)
    if p >= 0.05 or abs(r) < 0.2:
        return None

    direction = "increased" if slope > 0 else "decreased"
    pct_change = None
    first_val, last_val = y[0], y[-1]
    if first_val not in (0, None) and not np.isnan(first_val) and first_val != 0:
        pct_change = round((last_val - first_val) / abs(first_val) * 100, 1)

    change_txt = f" ({pct_change:+.1f}% over the observed period)" if pct_change is not None else ""
    summary = f"{numeric_col} has {direction} over time{change_txt}, tracked by {date_col}."
    caveat = _affected_by_cleaning([numeric_col, date_col], cleaning_log)

    return Finding(
        finding_id=f"f_{uuid.uuid4().hex[:8]}",
        summary=summary,
        statistical_support=f"linear trend: r={r:.2f}, p={p:.4f}, n={len(sub)}",
        recommended_chart="line",
        confidence="high" if p < 0.01 else "medium",
        related_columns=[date_col, numeric_col],
        effect_size=round(float(r), 4),
        p_value=round(float(p), 6),
        caveat=caveat,
    )


def _correlation_finding(df: pd.DataFrame, col_a: str, col_b: str, cleaning_log) -> Finding | None:
    sub = df[[col_a, col_b]].dropna()
    if len(sub) < 8:
        return None
    a, b = sub[col_a].astype(float), sub[col_b].astype(float)
    if a.std() == 0 or b.std() == 0:
        return None
    r, p = stats.pearsonr(a, b)

    if p < 0.05 and abs(r) >= 0.3:
        strength = "strong" if abs(r) >= 0.6 else "moderate"
        direction = "positive" if r > 0 else "negative"
        summary = f"{col_a} and {col_b} show a {strength} {direction} correlation."
        chart = "scatter_with_trendline"
        confidence = "high" if p < 0.01 and abs(r) >= 0.5 else "medium"
    else:
        summary = f"{col_a} shows no significant correlation with {col_b}."
        chart = "none"
        confidence = "high" if p >= 0.2 else "medium"

    caveat = _affected_by_cleaning([col_a, col_b], cleaning_log)
    return Finding(
        finding_id=f"f_{uuid.uuid4().hex[:8]}",
        summary=summary,
        statistical_support=f"pearson r={r:.2f}, p={p:.4f}, n={len(sub)}",
        recommended_chart=chart,
        confidence=confidence,
        related_columns=[col_a, col_b],
        effect_size=round(float(r), 4),
        p_value=round(float(p), 6),
        caveat=caveat,
    )


def _group_difference_finding(df: pd.DataFrame, cat_col: str, num_col: str, cleaning_log) -> Finding | None:
    sub = df[[cat_col, num_col]].dropna()
    groups = [g[num_col].astype(float).values for _, g in sub.groupby(cat_col) if len(g) >= 3]
    if len(groups) < 2:
        return None
    try:
        if len(groups) == 2:
            stat, p = stats.ttest_ind(groups[0], groups[1], equal_var=False)
            test_name = "t-test"
        else:
            stat, p = stats.f_oneway(*groups)
            test_name = "one-way ANOVA"
    except Exception:
        return None

    means = sub.groupby(cat_col)[num_col].mean().sort_values(ascending=False)
    if p >= 0.05:
        return None

    top_group, bottom_group = means.index[0], means.index[-1]
    summary = (f"{num_col} differs significantly across {cat_col} groups — "
               f"'{top_group}' averages highest, '{bottom_group}' lowest.")
    caveat = _affected_by_cleaning([cat_col, num_col], cleaning_log)
    return Finding(
        finding_id=f"f_{uuid.uuid4().hex[:8]}",
        summary=summary,
        statistical_support=f"{test_name}: stat={stat:.2f}, p={p:.4f}, groups={len(groups)}",
        recommended_chart="grouped_bar",
        confidence="high" if p < 0.01 else "medium",
        related_columns=[cat_col, num_col],
        p_value=round(float(p), 6),
        caveat=caveat,
    )


def _rank_key(f: Finding):
    conf_rank = {"high": 0, "medium": 1, "low": 2}[f.confidence]
    p = f.p_value if f.p_value is not None else 1.0
    return (conf_rank, p)


def analyze(df: pd.DataFrame, cleaning_log: dict | None = None, goal: str | None = None) -> AnalysisResult:
    findings: list[Finding] = []

    numeric_cols = _numeric_columns(df)
    categorical_cols = _categorical_columns(df)
    date_col = _find_date_column(df)

    # 1. Trends over time
    if date_col:
        for num_col in numeric_cols[:6]:
            f = _trend_finding(df, date_col, num_col, cleaning_log)
            if f:
                findings.append(f)

    # 2. Correlations between numeric pairs
    for col_a, col_b in list(combinations(numeric_cols, 2))[:15]:
        f = _correlation_finding(df, col_a, col_b, cleaning_log)
        if f:
            findings.append(f)

    # 3. Group differences (categorical x numeric)
    for cat_col in categorical_cols[:5]:
        for num_col in numeric_cols[:5]:
            f = _group_difference_finding(df, cat_col, num_col, cleaning_log)
            if f:
                findings.append(f)

    # rank: prefer high confidence, then lower p-value; keep a mix of
    # "significant" and clearly-stated "no effect" findings, but cap total
    findings.sort(key=_rank_key)
    top_findings = findings[:MAX_FINDINGS]

    summary_stats = {}
    for col in numeric_cols[:10]:
        desc = df[col].describe()
        summary_stats[col] = {k: (round(float(v), 4) if pd.notna(v) else None) for k, v in desc.items()}

    return AnalysisResult(
        analysis_id=f"an_{uuid.uuid4().hex[:8]}",
        goal=goal,
        summary_stats=summary_stats,
        findings=top_findings,
    )
