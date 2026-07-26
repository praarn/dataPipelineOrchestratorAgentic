import os

from backend.agents import ingestion_agent, cleaning_agent, analysis_agent, visualization_agent, report_agent

SAMPLE = os.path.join(os.path.dirname(__file__), "..", "sample_data", "messy_sales_data.csv")

with open(SAMPLE, "rb") as f:
    raw = f.read()

df, ing_report = ingestion_agent.ingest(raw, "messy_sales_data.csv")
print("=== INGESTION ===")
print("rows", ing_report.row_count, "cols", ing_report.column_count, "issues", ing_report.structural_issues)
for c in ing_report.schema:
    print(" ", c.column, c.inferred_type, c.null_pct, c.unique_count)

plan = cleaning_agent.preview(df)
print("\n=== CLEANING PLAN ===")
for a in plan.proposed_actions:
    print(" ", a.action, a.column, a.severity, a.count, "|", a.reason)

approved = {a.action_id for a in plan.proposed_actions}
cleaned_df, log = cleaning_agent.apply(df, plan.proposed_actions, approved)
print("\n=== CLEANING LOG ===")
print("quality", log.quality_score_before, "->", log.quality_score_after)
print("rows", log.rows_before, "->", log.rows_after)
print("state values:", sorted(cleaned_df["state"].unique()))
print("dtypes:\n", cleaned_df.dtypes)
print("nulls:\n", cleaned_df.isna().sum())

analysis = analysis_agent.analyze(cleaned_df, log.to_dict())
print("\n=== ANALYSIS ===")
for f in analysis.findings:
    print(" -", f.summary, "|", f.recommended_chart, "|", f.confidence, "|", f.statistical_support)

viz = visualization_agent.visualize(cleaned_df, analysis.findings)
print("\n=== VIZ ===")
print("charts:", len(viz.charts), "skipped:", len(viz.findings_without_charts))
for c in viz.charts:
    print(" ", c.chart_type, "n=", c.n, "imglen=", len(c.image_base64))
for s in viz.findings_without_charts:
    print(" skipped:", s.reason)

report = report_agent.build_report(ing_report.to_dict(), log.to_dict(), analysis.to_dict(), viz.to_dict(), audience="technical")
print("\n=== REPORT LENGTH ===", len(report.markdown))

exec_report = report_agent.build_report(ing_report.to_dict(), log.to_dict(), analysis.to_dict(), viz.to_dict(), audience="executive")
print("=== EXEC REPORT LENGTH ===", len(exec_report.markdown))

print("\nALL OK")
