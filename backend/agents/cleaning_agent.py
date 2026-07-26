"""
Cleaning Agent

Role: detect data quality issues and apply transformations transparently,
with every decision logged. Nothing is silently dropped or changed.

Design (per spec section 3.2 / 8.2): actions are split into two severities:
  - "safe"   -> proposed pre-checked in dry-run, auto-applies on approval
               (e.g. exact duplicate rows)
  - "review" -> proposed UNCHECKED by default (e.g. outlier removal,
               imputation of a heavily-null column) — a human must opt in

`preview()` NEVER mutates the input dataframe — it only inspects it and
returns a CleaningPlan of proposed actions. `apply()` takes that plan plus
the set of action_ids the user approved and performs the actual pandas
operations. All data manipulation happens in real pandas code; the "agent
reasoning" only decides *which* strategy to use.
"""
from __future__ import annotations

import re
import uuid
from typing import Optional

import numpy as np
import pandas as pd

from ..dtype_utils import is_textual_dtype, textual_columns
from ..schemas import CleaningAction, CleaningLog, CleaningPlan

MISSING_DROP_THRESHOLD = 0.5      # column >50% null -> flag for review, don't auto-impute
MISSING_SAFE_IMPUTE_THRESHOLD = 0.15  # column <15% null -> safe to auto-impute
OUTLIER_IQR_MULTIPLIER = 1.5


def _to_numeric_safe(series: pd.Series) -> pd.Series:
    """pd.to_numeric, but comma-thousands-separators don't turn valid numbers
    into NaN. Used everywhere a column might contain '1,234.56'-style text
    so imputation/outlier stats never silently misread real values as missing.
    """
    if is_textual_dtype(series):
        cleaned = series.astype(str).str.replace(",", "", regex=False)
        return pd.to_numeric(cleaned, errors="coerce")
    return pd.to_numeric(series, errors="coerce")


def _action_id() -> str:
    return f"act_{uuid.uuid4().hex[:8]}"


def _quality_score(df: pd.DataFrame) -> float:
    """Heuristic 0-1 quality score: completeness + uniqueness + consistency."""
    if df.empty:
        return 0.0
    completeness = 1 - (df.isna().sum().sum() / (df.shape[0] * max(df.shape[1], 1)))
    dup_ratio = df.duplicated().sum() / max(len(df), 1)
    uniqueness = 1 - dup_ratio

    # consistency: for object columns, penalize high "near-duplicate" variance
    # (case/whitespace variants) as a rough proxy
    consistency_scores = []
    for col in textual_columns(df):
        series = df[col].dropna().astype(str)
        if series.empty:
            continue
        normalized = series.str.strip().str.lower()
        raw_unique = series.nunique()
        norm_unique = normalized.nunique()
        if raw_unique == 0:
            continue
        consistency_scores.append(norm_unique / raw_unique)
    consistency = float(np.mean(consistency_scores)) if consistency_scores else 1.0

    score = 0.5 * completeness + 0.3 * uniqueness + 0.2 * consistency
    return round(max(0.0, min(1.0, score)), 4)


def _looks_numeric_string(series: pd.Series) -> bool:
    non_null = series.dropna()
    if non_null.empty:
        return False
    coerced = _to_numeric_safe(non_null)
    return coerced.notna().mean() > 0.9


US_STATE_TO_ABBREV = {
    "alabama": "AL", "alaska": "AK", "arizona": "AZ", "arkansas": "AR", "california": "CA",
    "colorado": "CO", "connecticut": "CT", "delaware": "DE", "florida": "FL", "georgia": "GA",
    "hawaii": "HI", "idaho": "ID", "illinois": "IL", "indiana": "IN", "iowa": "IA",
    "kansas": "KS", "kentucky": "KY", "louisiana": "LA", "maine": "ME", "maryland": "MD",
    "massachusetts": "MA", "michigan": "MI", "minnesota": "MN", "mississippi": "MS",
    "missouri": "MO", "montana": "MT", "nebraska": "NE", "nevada": "NV",
    "new hampshire": "NH", "new jersey": "NJ", "new mexico": "NM", "new york": "NY",
    "north carolina": "NC", "north dakota": "ND", "ohio": "OH", "oklahoma": "OK",
    "oregon": "OR", "pennsylvania": "PA", "rhode island": "RI", "south carolina": "SC",
    "south dakota": "SD", "tennessee": "TN", "texas": "TX", "utah": "UT", "vermont": "VT",
    "virginia": "VA", "washington": "WA", "west virginia": "WV", "wisconsin": "WI",
    "wyoming": "WY",
}
US_STATE_ABBREVS = set(US_STATE_TO_ABBREV.values())


def _us_state_map(series: pd.Series) -> Optional[dict]:
    """Domain-specific synonym resolution: 'NY', 'ny', 'New York' -> 'NY'.
    Only fires when the column looks predominantly like US state names/codes,
    since blindly canonicalizing arbitrary text this way would be wrong.
    """
    non_null = series.dropna().astype(str)
    if non_null.empty:
        return None
    normalized = non_null.str.strip()
    hits = normalized.apply(
        lambda v: v.upper() in US_STATE_ABBREVS or v.strip().lower() in US_STATE_TO_ABBREV
    )
    if hits.mean() < 0.8:
        return None

    mapping = {}
    for raw in non_null.unique():
        stripped = raw.strip()
        if stripped.lower() in US_STATE_TO_ABBREV:
            canonical = US_STATE_TO_ABBREV[stripped.lower()]
        elif stripped.upper() in US_STATE_ABBREVS:
            canonical = stripped.upper()
        else:
            continue
        if raw != canonical:
            mapping[raw] = canonical
    return mapping or None


def _standardization_map(series: pd.Series) -> Optional[dict]:
    """Detect case/whitespace variants of the same categorical value, e.g.
    'NY', 'ny', ' New York' -> propose a canonical mapping.
    Only fires for genuinely categorical-looking text columns.
    """
    non_null = series.dropna().astype(str)
    if non_null.empty or non_null.nunique() > 100:
        return None
    normalized = non_null.str.strip().str.lower()
    if normalized.nunique() == non_null.nunique():
        return None  # nothing to consolidate

    # build groups keyed by normalized value
    df_tmp = pd.DataFrame({"raw": non_null})
    df_tmp["norm"] = normalized.values
    mapping = {}
    for norm_val, group in df_tmp.groupby("norm"):
        variants = sorted(group["raw"].unique(), key=lambda v: (-len(v), v))
        if len(variants) <= 1:
            continue
        # canonical = most frequent variant, tie-broken by title case preference
        counts = group["raw"].value_counts()
        canonical = counts.idxmax()
        for variant in variants:
            if variant != canonical:
                mapping[variant] = canonical
    return mapping or None


def preview(df: pd.DataFrame) -> CleaningPlan:
    """Inspect the dataframe and propose (but do not apply) cleaning actions."""
    actions: list[CleaningAction] = []

    # 1. Exact duplicate rows — safe to auto-drop
    dup_count = int(df.duplicated().sum())
    if dup_count > 0:
        actions.append(CleaningAction(
            action_id=_action_id(),
            action="dropped_rows",
            column=None,
            reason="exact duplicate records",
            count=dup_count,
            severity="safe",
            status="proposed",
            note="Rows fully identical across all columns.",
        ))

    # 2. Missing values, per column
    for col in df.columns:
        series = df[col]
        null_count = int(series.isna().sum())
        if null_count == 0:
            continue
        null_pct = null_count / max(len(series), 1)
        is_numeric = pd.api.types.is_numeric_dtype(series) or _looks_numeric_string(series)

        if null_pct >= MISSING_DROP_THRESHOLD:
            actions.append(CleaningAction(
                action_id=_action_id(),
                action="flagged_not_dropped",
                column=col,
                reason=f"{round(null_pct*100,1)}% missing — too sparse to safely impute",
                count=null_count,
                severity="review",
                status="proposed",
                note="Recommend manual review: consider dropping the column or "
                     "sourcing the data rather than imputing.",
            ))
        elif is_numeric:
            method = "median"
            severity = "safe" if null_pct <= MISSING_SAFE_IMPUTE_THRESHOLD else "review"
            actions.append(CleaningAction(
                action_id=_action_id(),
                action="imputed",
                column=col,
                reason=f"{round(null_pct*100,1)}% missing numeric values",
                count=null_count,
                method=method,
                severity=severity,
                status="proposed",
                note="Median chosen over mean to reduce outlier sensitivity.",
            ))
        else:
            method = "mode"
            severity = "safe" if null_pct <= MISSING_SAFE_IMPUTE_THRESHOLD else "review"
            actions.append(CleaningAction(
                action_id=_action_id(),
                action="imputed",
                column=col,
                reason=f"{round(null_pct*100,1)}% missing categorical/text values",
                count=null_count,
                method=method,
                severity=severity,
                status="proposed",
                note="Most frequent value used as fill.",
            ))

    # 3. Type coercion — numeric values stored as strings
    for col in textual_columns(df):
        if _looks_numeric_string(df[col]):
            non_null = df[col].dropna()
            # count only entries that are *actually* string instances (as opposed
            # to numbers already sitting in an object-dtype column) so the log
            # reflects real affected rows, not the whole column
            n_affected = int(non_null.apply(lambda v: isinstance(v, str)).sum())
            if n_affected == 0:
                continue
            actions.append(CleaningAction(
                action_id=_action_id(),
                action="coerced_type",
                column=col,
                reason="numeric values stored as text",
                count=n_affected,
                method="to_numeric",
                severity="safe",
                status="proposed",
                note="Thousands separators stripped before coercion.",
            ))

    # 4. Categorical standardization
    for col in textual_columns(df):
        state_mapping = _us_state_map(df[col])
        if state_mapping:
            actions.append(CleaningAction(
                action_id=_action_id(),
                action="standardized",
                column=col,
                reason="US state names/abbreviations recognized as variants of the same value",
                count=len(state_mapping),
                method="us_state_canonicalize",
                severity="safe",
                status="proposed",
                note=f"e.g. consolidating {list(state_mapping.items())[:3]} into 2-letter codes",
            ))
            continue  # don't also propose the generic case/whitespace pass for this column

        mapping = _standardization_map(df[col])
        if mapping:
            actions.append(CleaningAction(
                action_id=_action_id(),
                action="standardized",
                column=col,
                reason="case/whitespace variants of the same value",
                count=len(mapping),
                method="canonicalize",
                severity="safe",
                status="proposed",
                note=f"e.g. consolidating {list(mapping.items())[:3]}",
            ))

    # 5. Outlier detection (numeric columns) — always review, never auto-drop
    for col in df.select_dtypes(include=np.number).columns:
        series = df[col].dropna()
        if len(series) < 8:
            continue
        q1, q3 = series.quantile(0.25), series.quantile(0.75)
        iqr = q3 - q1
        if iqr == 0:
            continue
        lower = q1 - OUTLIER_IQR_MULTIPLIER * iqr
        upper = q3 + OUTLIER_IQR_MULTIPLIER * iqr
        outliers = series[(series < lower) | (series > upper)]
        if len(outliers) > 0:
            actions.append(CleaningAction(
                action_id=_action_id(),
                action="flagged_not_dropped",
                column=col,
                reason=f"{len(outliers)} value(s) outside IQR bounds "
                       f"[{round(lower,2)}, {round(upper,2)}]",
                count=int(len(outliers)),
                method="iqr",
                severity="review",
                status="proposed",
                note="Retained by default — extreme values are sometimes the "
                     "most important data points. Approve to remove instead.",
            ))

    return CleaningPlan(
        plan_id=f"plan_{uuid.uuid4().hex[:8]}",
        proposed_actions=actions,
        quality_score_before=_quality_score(df),
        rows_before=int(len(df)),
        columns_before=int(len(df.columns)),
    )


_EXECUTION_PRIORITY = {
    "dropped_rows": 0,       # dedupe first — independent of column values
    "coerced_type": 1,       # must run before anything that reads numeric values
    "imputed": 2,            # depends on clean numeric parsing
    "standardized": 3,
    "flagged_not_dropped": 4,
}


def apply(df: pd.DataFrame, plan_actions: list[CleaningAction], approved_ids: set[str]) -> tuple[pd.DataFrame, CleaningLog]:
    """Apply only the actions whose action_id is in approved_ids.
    Safe-severity actions are approved by default by the caller (API layer)
    unless explicitly rejected. Returns the cleaned df + a full log of what
    was actually done (never just what was proposed).

    Actions execute in a dependency-safe order (type coercion before
    imputation, etc.) regardless of the order they were proposed in, so a
    column full of "1,234.56"-style strings gets parsed correctly before any
    stat (median, IQR bounds) is computed from it — proposal order stays
    whatever reads best in the UI, execution order is what's actually safe.
    """
    working = df.copy()
    rows_before = len(working)
    outcomes: dict[str, tuple[str, Optional[str]]] = {}  # action_id -> (status, extra_note)

    approved_actions = [a for a in plan_actions if a.action_id in approved_ids]
    execution_order = sorted(approved_actions, key=lambda a: _EXECUTION_PRIORITY.get(a.action, 9))

    for action in execution_order:
        try:
            if action.action == "dropped_rows" and action.column is None:
                working = working.drop_duplicates()

            elif action.action == "imputed" and action.column in working.columns:
                col = action.column
                if action.method == "median":
                    numeric = _to_numeric_safe(working[col])
                    working[col] = numeric.fillna(numeric.median())
                else:  # mode
                    mode_vals = working[col].mode(dropna=True)
                    fill = mode_vals.iloc[0] if not mode_vals.empty else None
                    working[col] = working[col].fillna(fill)

            elif action.action == "coerced_type" and action.column in working.columns:
                col = action.column
                working[col] = _to_numeric_safe(working[col])

            elif action.action == "standardized" and action.column in working.columns:
                col = action.column
                if action.method == "us_state_canonicalize":
                    mapping = _us_state_map(df[col]) or {}
                else:
                    mapping = _standardization_map(df[col]) or {}
                working[col] = working[col].replace(mapping)

            elif action.action == "flagged_not_dropped":
                # Approving a "flagged" outlier/high-null action means the
                # user wants it actually removed, not just flagged.
                if action.method == "iqr" and action.column in working.columns:
                    col = action.column
                    series = _to_numeric_safe(working[col])
                    q1, q3 = series.quantile(0.25), series.quantile(0.75)
                    iqr = q3 - q1
                    lower, upper = q1 - OUTLIER_IQR_MULTIPLIER * iqr, q3 + OUTLIER_IQR_MULTIPLIER * iqr
                    mask = (series < lower) | (series > upper)
                    working = working[~mask.fillna(False)]
                elif action.column in working.columns:
                    # sparse column flagged for review, approved -> drop rows with nulls there
                    working = working[working[action.column].notna()]

            outcomes[action.action_id] = ("approved", None)
        except Exception as exc:  # pragma: no cover - defensive
            outcomes[action.action_id] = ("rejected", f"[FAILED TO APPLY: {exc}]")

    # build the log in the ORIGINAL proposal order (readable in the UI),
    # even though execution above ran in dependency order
    applied: list[CleaningAction] = []
    rejected: list[CleaningAction] = []
    for action in plan_actions:
        if action.action_id not in approved_ids:
            rejected.append(CleaningAction(**{**action.to_dict(), "status": "rejected"}))
            continue
        status, extra_note = outcomes.get(action.action_id, ("rejected", "[action not executed]"))
        note = f"{action.note or ''} {extra_note}".strip() if extra_note else action.note
        copy = CleaningAction(**{**action.to_dict(), "status": status, "note": note})
        (applied if status == "approved" else rejected).append(copy)

    working = working.reset_index(drop=True)

    log = CleaningLog(
        log_id=f"log_{uuid.uuid4().hex[:8]}",
        applied_actions=applied,
        rejected_actions=rejected,
        quality_score_before=_quality_score(df),
        quality_score_after=_quality_score(working),
        rows_before=rows_before,
        rows_after=int(len(working)),
        dataframe_ref="session:cleaned",
    )
    return working, log
