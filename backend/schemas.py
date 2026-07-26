"""
Data contracts exchanged between agents.

Design note (see project spec, section 4): these objects are the ONLY things
that flow through "LLM/agent reasoning" context in this system. The actual
dataframes never get serialized wholesale — they live in the SessionStore
(backend/store.py) and agents receive/return handles plus these lightweight,
structured summaries. This keeps the system usable on large datasets and
keeps every agent decision auditable and serializable to JSON for the UI.

Implemented with stdlib dataclasses rather than Pydantic so the reference
build runs with zero third-party dependencies beyond the data-science stack
(pandas/numpy/scipy/matplotlib) — see README for notes on swapping in
Pydantic + FastAPI for a stricter-typed deployment; the shapes are identical.
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Any, Optional
import time
import uuid


def _to_dict(obj: Any) -> Any:
    if hasattr(obj, "to_dict"):
        return obj.to_dict()
    if isinstance(obj, list):
        return [_to_dict(o) for o in obj]
    if isinstance(obj, dict):
        return {k: _to_dict(v) for k, v in obj.items()}
    return obj


class Serializable:
    def to_dict(self) -> dict:
        out = {}
        for k, v in asdict(self).items():
            out[k] = _to_dict(v)
        return out


# ---------------------------------------------------------------------------
# Ingestion
# ---------------------------------------------------------------------------

@dataclass
class ColumnProfile(Serializable):
    column: str
    inferred_type: str            # "integer" | "float" | "date" | "boolean" | "categorical" | "text" | "identifier"
    null_count: int
    null_pct: float
    unique_count: int
    sample_values: list = field(default_factory=list)
    min_value: Optional[str] = None
    max_value: Optional[str] = None


@dataclass
class IngestionReport(Serializable):
    report_id: str
    source_type: str              # csv | json | excel
    file_name: str
    row_count: int
    column_count: int
    schema: list[ColumnProfile]
    structural_issues: list[str]
    sample_rows: list[dict]
    dataframe_ref: str
    created_at: float = field(default_factory=time.time)

    @staticmethod
    def new_id() -> str:
        return f"ing_{uuid.uuid4().hex[:10]}"


# ---------------------------------------------------------------------------
# Cleaning
# ---------------------------------------------------------------------------

@dataclass
class CleaningAction(Serializable):
    action_id: str
    action: str                   # dropped_rows | imputed | standardized | coerced_type | flagged_not_dropped
    column: Optional[str]
    reason: str
    count: int
    method: Optional[str] = None
    note: Optional[str] = None
    severity: str = "safe"        # safe | review
    status: str = "proposed"      # proposed | approved | rejected | auto_applied


@dataclass
class CleaningPlan(Serializable):
    plan_id: str
    proposed_actions: list[CleaningAction]
    quality_score_before: float
    rows_before: int
    columns_before: int


@dataclass
class CleaningLog(Serializable):
    log_id: str
    applied_actions: list[CleaningAction]
    rejected_actions: list[CleaningAction]
    quality_score_before: float
    quality_score_after: float
    rows_before: int
    rows_after: int
    dataframe_ref: str
    created_at: float = field(default_factory=time.time)


# ---------------------------------------------------------------------------
# Analysis
# ---------------------------------------------------------------------------

@dataclass
class Finding(Serializable):
    finding_id: str
    summary: str
    statistical_support: str
    recommended_chart: str        # line | grouped_bar | scatter_with_trendline | box | bar | none
    confidence: str                # high | medium | low
    related_columns: list[str]
    effect_size: Optional[float] = None
    p_value: Optional[float] = None
    caveat: Optional[str] = None   # upstream cleaning caveat, if relevant


@dataclass
class AnalysisResult(Serializable):
    analysis_id: str
    goal: Optional[str]
    summary_stats: dict
    findings: list[Finding]
    created_at: float = field(default_factory=time.time)


# ---------------------------------------------------------------------------
# Visualization
# ---------------------------------------------------------------------------

@dataclass
class ChartSpec(Serializable):
    finding_id: str
    chart_type: str
    image_base64: str
    caption: str
    n: Optional[int] = None


@dataclass
class SkippedFinding(Serializable):
    finding_id: str
    reason: str


@dataclass
class VisualizationResult(Serializable):
    viz_id: str
    charts: list[ChartSpec]
    findings_without_charts: list[SkippedFinding]


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------

@dataclass
class ReportResult(Serializable):
    report_id: str
    audience: str
    markdown: str
    created_at: float = field(default_factory=time.time)
