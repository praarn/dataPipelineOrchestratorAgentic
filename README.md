---

## API reference

All routes are under `/api`. The pipeline is sequential — most stages 409
if you try to skip ahead (e.g. `analyze` before `clean/apply`).

| Method | Route | Does |
|---|---|---|
| POST | `/session` | Start a new session |
| GET | `/session/<id>/state` | Full current pipeline state (for reload) |
| POST | `/session/<id>/ingest` | Upload a file (`multipart/form-data`, field `file`) |
| POST | `/session/<id>/ingest-sample` | Load the bundled demo dataset |
| POST | `/session/<id>/clean/preview` | Dry-run: propose cleaning actions |
| POST | `/session/<id>/clean/apply` | Apply approved actions — body `{"approved_action_ids": [...]}` |
| POST | `/session/<id>/analyze` | Run analysis — body `{"goal": "optional free text"}` |
| POST | `/session/<id>/analyze/flag` | Feedback loop — body `{"column": "..."}`, reopens cleaning |
| POST | `/session/<id>/visualize` | Generate charts for current findings |
| POST | `/session/<id>/report` | Generate report — body `{"audience": "technical" \| "executive"}` |
| GET | `/session/<id>/report/download?format=md\|pdf\|docx` | Download the report |
| GET | `/health` | Liveness + active session count |

---

## Extension points

- **New file formats**: add a `_load_*` function in `ingestion_agent.py`
  and register the extension in `SUPPORTED_EXTENSIONS` (backend) and
  `ALLOWED_EXTENSIONS` (`app.py`).
- **New cleaning strategies**: add a detector in `cleaning_agent.preview()`
  and a matching branch in `apply()`. Give it a `severity` of `"safe"` or
  `"review"` depending on whether it could plausibly lose real information.
- **New statistical tests**: add a `_*_finding()` function in
  `analysis_agent.py` following the existing pattern (return `None` if the
  test doesn't clear a significance/effect-size bar, else a `Finding`).
- **New chart types**: add a builder function to `CHART_BUILDERS` in
  `visualization_agent.py`, keyed by the `recommended_chart` string your
  analysis functions emit.
- **Swap the session store**: implement the same four methods
  (`create`, `get`, `_evict_expired`, `stats`) against Redis/DuckDB/a real
  database in `store.py`.

## Design notes on the UI

The interface treats the pipeline as a schematic — five stage "chips"
connected by a circuit trace across the top, each one an inspectable
module rather than a black box. Safe/review/danger states use a
consistent amber/cyan/red vocabulary throughout (amber = pending or
needs your input, cyan = safe/complete, violet = statistical/analysis
content, red = something needs rejecting or removing). No dependency on
a JS framework or build tool — it's vanilla JS against the REST API, so
there's nothing to compile and nothing to break between Node versions.
