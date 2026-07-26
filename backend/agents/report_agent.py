"""
Report Agent (optional stage)

Role: stitch ingestion, cleaning, analysis, and visualization output into
one coherent narrative document. The cleaning log is surfaced explicitly as
a "Methodology & Data Quality" section — per spec section 3.5, this is what
separates a trustworthy pipeline from a black box: a reader should be able
to see exactly what happened to the data before any headline number was
produced.
"""
from __future__ import annotations

import uuid

from ..schemas import ReportResult


def _fmt_pct(x: float) -> str:
    return f"{x * 100:.1f}%"


def _action_line(a: dict) -> str:
    col = f" `{a['column']}`" if a.get("column") else ""
    method = f" (method: {a['method']})" if a.get("method") else ""
    return f"- **{a['action']}**{col} — {a['reason']}, {a['count']} row(s) affected{method}."


def build_report(
    ingestion_report: dict,
    cleaning_log: dict,
    analysis_result: dict,
    visualization_result: dict,
    audience: str = "technical",
) -> ReportResult:
    findings = analysis_result.get("findings", [])
    charts = {c["finding_id"]: c for c in visualization_result.get("charts", [])}
    skipped = {s["finding_id"]: s for s in visualization_result.get("findings_without_charts", [])}

    lines: list[str] = []
    lines.append(f"# Data Pipeline Report — {ingestion_report.get('file_name', 'dataset')}")
    lines.append("")

    # --- Executive summary -------------------------------------------------
    lines.append("## Executive Summary")
    lines.append("")
    if findings:
        headline = findings[0]
        lines.append(f"**Headline finding:** {headline['summary']}")
        lines.append("")
        if len(findings) > 1:
            lines.append(f"{len(findings)} findings were surfaced and ranked by statistical "
                         f"confidence; the top result is highlighted above, with the full set below.")
    else:
        lines.append("No statistically significant findings were surfaced from this dataset.")
    lines.append("")
    lines.append(
        f"Quality score improved from **{cleaning_log.get('quality_score_before', 0):.2f}** to "
        f"**{cleaning_log.get('quality_score_after', 0):.2f}** (0–1 scale) during cleaning."
    )
    lines.append("")

    if audience == "executive":
        lines.append("---")
        lines.append("")
        lines.append("## Findings")
        lines.append("")
        for f in findings:
            lines.append(f"### {f['summary']}")
            lines.append("")
            if f.get("caveat"):
                lines.append(f"> ⚠️ {f['caveat']}.")
                lines.append("")
            chart = charts.get(f["finding_id"])
            if chart:
                lines.append(f"![chart](data:image/png;base64,{chart['image_base64']})")
                lines.append("")
        lines.append("---")
        lines.append("")
        lines.append("*A full technical methodology and data-quality appendix is available "
                     "on request — generate this report with `audience=technical` to include it.*")
        return ReportResult(
            report_id=f"rep_{uuid.uuid4().hex[:8]}",
            audience=audience,
            markdown="\n".join(lines),
        )

    # --- Technical / full report --------------------------------------------
    lines.append("---")
    lines.append("")
    lines.append("## 1. Data Overview")
    lines.append("")
    lines.append(f"- **Source:** {ingestion_report.get('source_type', 'unknown').upper()} "
                 f"— `{ingestion_report.get('file_name', 'unknown')}`")
    lines.append(f"- **Shape:** {ingestion_report.get('row_count')} rows × "
                 f"{ingestion_report.get('column_count')} columns")
    if ingestion_report.get("structural_issues"):
        lines.append("- **Structural issues detected at ingestion:**")
        for issue in ingestion_report["structural_issues"]:
            lines.append(f"  - {issue}")
    lines.append("")
    lines.append("| Column | Type | Null % | Unique |")
    lines.append("|---|---|---|---|")
    for col in ingestion_report.get("schema", []):
        lines.append(f"| `{col['column']}` | {col['inferred_type']} | "
                     f"{_fmt_pct(col['null_pct'])} | {col['unique_count']} |")
    lines.append("")

    # --- Methodology & Data Quality -----------------------------------------
    lines.append("---")
    lines.append("")
    lines.append("## 2. Methodology & Data Quality")
    lines.append("")
    lines.append("Every transformation applied to this dataset is logged below. "
                 "**No transformation happened silently.**")
    lines.append("")
    lines.append(f"- Quality score before cleaning: **{cleaning_log.get('quality_score_before', 0):.2f}**")
    lines.append(f"- Quality score after cleaning: **{cleaning_log.get('quality_score_after', 0):.2f}**")
    lines.append(f"- Rows before → after: {cleaning_log.get('rows_before')} → {cleaning_log.get('rows_after')}")
    lines.append("")
    applied = cleaning_log.get("applied_actions", [])
    if applied:
        lines.append("**Applied:**")
        for a in applied:
            lines.append(_action_line(a))
    else:
        lines.append("*No cleaning actions were applied — data passed all checks as-loaded.*")
    lines.append("")
    rejected = cleaning_log.get("rejected_actions", [])
    reviewed = [a for a in rejected if a.get("status") == "rejected"]
    if reviewed:
        lines.append("**Reviewed but not applied** (flagged, left to human judgment):")
        for a in reviewed:
            lines.append(_action_line(a))
        lines.append("")

    # --- Findings ------------------------------------------------------------
    lines.append("---")
    lines.append("")
    lines.append("## 3. Findings")
    lines.append("")
    if not findings:
        lines.append("No statistically significant findings were surfaced.")
    for i, f in enumerate(findings, 1):
        lines.append(f"### {i}. {f['summary']}")
        lines.append("")
        lines.append(f"- **Statistical support:** {f['statistical_support']}")
        lines.append(f"- **Confidence:** {f['confidence']}")
        if f.get("caveat"):
            lines.append(f"- ⚠️ **Caveat:** {f['caveat']}.")
        lines.append("")
        chart = charts.get(f["finding_id"])
        skip = skipped.get(f["finding_id"])
        if chart:
            lines.append(f"![chart](data:image/png;base64,{chart['image_base64']})")
            lines.append(f"*n = {chart.get('n')}*")
        elif skip:
            lines.append(f"*No chart rendered: {skip['reason']}*")
        lines.append("")

    # --- Caveats ---------------------------------------------------------------
    lines.append("---")
    lines.append("")
    lines.append("## 4. Caveats & Limitations")
    lines.append("")
    lines.append("- Findings reflect the cleaned dataset only; rows/values removed or flagged "
                 "during cleaning (see Section 2) are excluded from all statistics above.")
    lines.append("- Statistical significance (p < 0.05) does not imply practical significance — "
                 "review effect sizes alongside p-values before acting on any finding.")
    lines.append("- This report was generated by an automated pipeline. Findings involving small "
                 "sample sizes (n < 30) should be treated as directional, not conclusive.")
    lines.append("")

    return ReportResult(
        report_id=f"rep_{uuid.uuid4().hex[:8]}",
        audience=audience,
        markdown="\n".join(lines),
    )
