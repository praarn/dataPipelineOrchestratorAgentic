"""
Ingestion Agent

Role: load raw data from any supported source and establish ground truth
about its shape and basic quality before anything else touches it.

This agent deliberately makes NO judgment calls about data quality — it
only establishes facts (schema, null rates, structural problems). Deciding
what to *do* about those facts is the Cleaning Agent's job (separation of
concerns called out in the project spec, section 1).
"""
from __future__ import annotations

import io
import json
from typing import Tuple

import numpy as np
import pandas as pd

from ..dtype_utils import is_textual_dtype
from ..schemas import ColumnProfile, IngestionReport

SUPPORTED_EXTENSIONS = {"csv", "json", "xlsx", "xls", "tsv"}


def _infer_type(series: pd.Series) -> str:
    non_null = series.dropna()
    if non_null.empty:
        return "unknown"

    if pd.api.types.is_bool_dtype(series):
        return "boolean"
    if pd.api.types.is_integer_dtype(series):
        return "integer"
    if pd.api.types.is_float_dtype(series):
        return "float"
    if pd.api.types.is_datetime64_any_dtype(series):
        return "date"

    # try numeric coercion on strings
    coerced = pd.to_numeric(non_null, errors="coerce")
    if coerced.notna().mean() > 0.9:
        return "float" if (coerced % 1 != 0).any() else "integer"

    # try date coercion
    try:
        parsed = pd.to_datetime(non_null, errors="coerce", format="mixed")
        if parsed.notna().mean() > 0.9:
            return "date"
    except Exception:
        pass

    unique_ratio = non_null.nunique() / max(len(non_null), 1)
    if non_null.nunique() <= 1:
        return "constant"
    if unique_ratio > 0.95 and is_textual_dtype(non_null):
        # near-unique strings: could be an identifier or free text
        avg_len = non_null.astype(str).str.len().mean()
        return "identifier" if avg_len <= 24 else "text"
    if unique_ratio < 0.5 or non_null.nunique() <= 50:
        return "categorical"
    return "text"


def _safe_sample(series: pd.Series, n: int = 4) -> list:
    vals = series.dropna().unique()[:n]
    out = []
    for v in vals:
        if isinstance(v, (np.integer,)):
            out.append(int(v))
        elif isinstance(v, (np.floating,)):
            out.append(float(v))
        else:
            out.append(str(v))
    return out


def _min_max(series: pd.Series, inferred_type: str):
    try:
        if inferred_type in ("integer", "float"):
            coerced = pd.to_numeric(series, errors="coerce")
            return (str(coerced.min()), str(coerced.max()))
        if inferred_type == "date":
            coerced = pd.to_datetime(series, errors="coerce", format="mixed")
            return (str(coerced.min()), str(coerced.max()))
    except Exception:
        pass
    return (None, None)


def _detect_source_type(filename: str) -> str:
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    if ext in ("xlsx", "xls"):
        return "excel"
    if ext == "json":
        return "json"
    if ext == "tsv":
        return "tsv"
    return "csv"


def _load_csv(raw_bytes: bytes, sep=",") -> tuple[pd.DataFrame, list[str]]:
    issues = []
    text = None
    for enc in ("utf-8", "utf-8-sig", "latin-1"):
        try:
            text = raw_bytes.decode(enc)
            if enc != "utf-8":
                issues.append(f"File was not valid UTF-8; decoded using '{enc}' fallback.")
            break
        except UnicodeDecodeError:
            continue
    if text is None:
        raise ValueError("Could not decode file with utf-8, utf-8-sig, or latin-1 encodings.")

    # First pass: strict parse to count bad lines
    bad_lines: list[int] = []

    def _collect_bad(bad_line):
        bad_lines.append(bad_line)
        return None

    try:
        df = pd.read_csv(
            io.StringIO(text), sep=sep, engine="python",
            on_bad_lines=_collect_bad,
        )
    except Exception:
        # last resort: let pandas do its best, skipping bad lines silently
        df = pd.read_csv(io.StringIO(text), sep=sep, engine="python", on_bad_lines="skip")

    if bad_lines:
        issues.append(
            f"{len(bad_lines)} row(s) had a mismatched column count and were "
            f"quarantined (excluded) during load."
        )

    # detect fully-blank columns named "Unnamed: N" (common artifact of stray commas)
    unnamed = [c for c in df.columns if str(c).startswith("Unnamed:")]
    if unnamed:
        issues.append(
            f"{len(unnamed)} unnamed column(s) detected (likely stray delimiters "
            f"in the source file): {unnamed}"
        )
    return df, issues


def _load_excel(raw_bytes: bytes) -> tuple[pd.DataFrame, list[str]]:
    issues = []
    xls = pd.ExcelFile(io.BytesIO(raw_bytes))
    if len(xls.sheet_names) > 1:
        issues.append(
            f"Workbook has {len(xls.sheet_names)} sheets; loaded the first "
            f"sheet ('{xls.sheet_names[0]}') only."
        )
    df = xls.parse(xls.sheet_names[0])
    return df, issues


def _load_json(raw_bytes: bytes) -> tuple[pd.DataFrame, list[str]]:
    issues = []
    data = json.loads(raw_bytes.decode("utf-8"))
    if isinstance(data, dict):
        # common shape: {"records": [...]} or {"data": [...]}
        for key in ("records", "data", "rows", "results", "items"):
            if key in data and isinstance(data[key], list):
                data = data[key]
                issues.append(f"Top-level JSON was an object; used the '{key}' list field.")
                break
        else:
            data = [data]
            issues.append("Top-level JSON was a single object; wrapped as a one-row table.")
    df = pd.json_normalize(data)
    return df, issues


def ingest(raw_bytes: bytes, filename: str) -> Tuple[pd.DataFrame, IngestionReport]:
    """Load raw bytes from an uploaded file into a DataFrame + IngestionReport.

    This is the single place raw file bytes touch the system — everything
    downstream operates on the DataFrame handle plus this report.
    """
    source_type = _detect_source_type(filename)
    structural_issues: list[str] = []

    if source_type == "excel":
        df, issues = _load_excel(raw_bytes)
    elif source_type == "json":
        df, issues = _load_json(raw_bytes)
    elif source_type == "tsv":
        df, issues = _load_csv(raw_bytes, sep="\t")
    else:
        df, issues = _load_csv(raw_bytes, sep=",")
    structural_issues.extend(issues)

    # normalize column names: strip whitespace (structural, not a quality judgment)
    original_cols = list(df.columns)
    df.columns = [str(c).strip() for c in df.columns]
    if list(df.columns) != original_cols:
        structural_issues.append("Column headers had leading/trailing whitespace trimmed.")

    if df.empty:
        structural_issues.append("Loaded dataframe has zero rows.")

    schema: list[ColumnProfile] = []
    for col in df.columns:
        series = df[col]
        inferred = _infer_type(series)
        null_count = int(series.isna().sum())
        row_count = max(len(series), 1)
        mn, mx = _min_max(series, inferred)
        schema.append(ColumnProfile(
            column=str(col),
            inferred_type=inferred,
            null_count=null_count,
            null_pct=round(null_count / row_count, 4),
            unique_count=int(series.nunique(dropna=True)),
            sample_values=_safe_sample(series),
            min_value=mn,
            max_value=mx,
        ))

    # fully-empty columns are a structural red flag worth surfacing immediately
    empty_cols = [c.column for c in schema if c.null_pct >= 0.999]
    if empty_cols:
        structural_issues.append(f"Column(s) entirely empty: {empty_cols}")

    sample_rows = json.loads(df.head(5).to_json(orient="records", date_format="iso"))

    report = IngestionReport(
        report_id=IngestionReport.new_id(),
        source_type=source_type,
        file_name=filename,
        row_count=int(len(df)),
        column_count=int(len(df.columns)),
        schema=schema,
        structural_issues=structural_issues,
        sample_rows=sample_rows,
        dataframe_ref="session:raw",
    )
    return df, report
