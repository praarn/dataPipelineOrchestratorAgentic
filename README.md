# Pipeline Orchestrator

A production-grade data pipeline that ingests messy real-world files, cleans
them with full transparency, runs real statistical analysis, generates
charts only where the data actually supports them, and writes a narrative
report — all through a five-stage multi-agent pipeline with a human-in-the-
loop checkpoint and a feedback loop from analysis back to cleaning.

No API keys, no external services, no build step. `pip install`, `python
run.py`, done.

![stages](https://img.shields.io/badge/stages-5-ffb454) ![deps](https://img.shields.io/badge/dependencies-8-52d9c9) ![build step](https://img.shields.io/badge/build%20step-none-9d8cff)

---

## Quick start

```bash
python -m venv .venv && source .venv/bin/activate   # optional but recommended
pip install -r requirements.txt
python run.py
```

Open **http://127.0.0.1:5050** and click **"Use the sample dataset"** to see
the whole pipeline run against a synthetic messy sales CSV — or drop in your
own `.csv` / `.tsv` / `.json` / `.xlsx` / `.xls` file.

Change the port with `PORT=8080 python run.py`.

---

## What it actually does

Five agents, each a pure Python module with no shared mutable state, wired
together by a thin Flask API:

| # | Agent | Role |
|---|-------|------|
| 1 | **Ingestion** | Loads the file, infers a schema, profiles nulls/uniques/ranges, flags structural issues (bad rows, empty columns, encoding fallbacks). Makes **no quality judgments** — just establishes facts. |
| 2 | **Cleaning** | Proposes fixes (dedup, impute, coerce types, standardize categories, flag outliers) as a dry-run plan. **Nothing is applied until you approve it.** Safe actions are pre-checked; anything that could lose information (outlier removal, sparse-column handling) requires an explicit opt-in. |
| 3 | **Analysis** | Runs real `scipy` tests — linear regression for trends, Pearson correlation, t-test/ANOVA for group differences — and ranks findings by statistical confidence, not by how interesting they sound. |
| 4 | **Visualization** | Generates a chart only when the finding supports one. A "no significant correlation" finding gets stated in plain text, not forced into a scatter plot. Bar-chart axes always start at zero. |
| 5 | **Report** | Stitches everything into a narrative document with a full audit trail: every cleaning action, every statistical test, every caveat. Available as Markdown, PDF, or Word. |

### Two things that make this a pipeline and not a script

**Human-in-the-loop checkpoint.** `POST /clean/preview` never touches your
data — it only proposes actions. You (via the UI) approve or reject each
one, then `POST /clean/apply` executes exactly what was approved. Every
approved and rejected action is logged either way.

**Feedback loop.** If a finding in Analysis looks like it might trace back
to a data quality issue, you can flag the column right from the finding
card. That reopens the Cleaning checkpoint pre-focused on that column,
without losing your ingestion report — you fix it and flow back downstream.

### What "nothing changes silently" means in practice

- Duplicate rows dropped → logged with a count.
- A column imputed → logged with the method (median/mode) and why.
- `"1,234.56"` coerced to a number → logged, and see below for why order
  matters here.
- `"NY"` / `"ny"` / `"New York"` collapsed into `"NY"` → logged with the
  specific mapping, using a real US-state synonym table (this is more than
  case/whitespace folding — it's genuine domain knowledge, and it's the one
  place the cleaning agent makes a semantic judgment rather than a
  structural one).
- An outlier *not* dropped → still logged, as a flag for your review. The
  agent never assumes an extreme value is wrong.

One bug worth mentioning because it's a good example of why the "nothing
silently" principle matters: early in development, imputation and type
coercion could be proposed in a display order that didn't match a safe
*execution* order — a comma-formatted number like `"1,486.13"` would fail
plain numeric parsing, get treated as missing, and get overwritten by the
column median before the coercion step ran. `apply()` now executes actions
in a fixed dependency-safe order (coerce → impute → standardize → flag)
regardless of the order they're displayed in, and a shared comma-safe
numeric parser is used everywhere a value might need it. Caught by testing
against a synthetic messy dataset before shipping — see `tests/`.

---

## Architecture

```
run.py                      entry point — python run.py
backend/
  app.py                    Flask routes (HTTP concerns only — no data logic)
  schemas.py                dataclass data contracts between agents/stages
  store.py                  in-memory session store (dataframe handles, not raw data)
  dtype_utils.py            pandas 2.x/3.x string-dtype compatibility shim
  report_export.py          markdown -> PDF / DOCX renderers
  agents/
    ingestion_agent.py
    cleaning_agent.py
    analysis_agent.py
    visualization_agent.py
    report_agent.py
static/
  index.html, css/style.css, js/app.js     zero-build-step vanilla frontend
sample_data/
  messy_sales_data.csv      synthetic dataset with dupes, nulls, outliers,
                             inconsistent state names, numbers-as-text
tests/
  test_agents_smoke.py      exercises all 5 agents directly
  test_api.py                exercises the full HTTP API incl. the feedback loop
```

**Data never re-enters "agent" reasoning as a giant blob.** Dataframes live
in `SessionStore`, keyed by session id; agents and API routes pass around
lightweight structured summaries (`IngestionReport`, `CleaningPlan`,
`AnalysisResult`, ...) defined in `schemas.py`. This is what keeps the
system usable on non-trivial datasets and keeps every decision auditable
and JSON-serializable for the UI.

### Why Flask + dataclasses instead of FastAPI + Pydantic

Functionally interchangeable for this project — the data contracts are the
same shapes either way. This build uses Flask and stdlib `dataclasses` so
the whole thing runs with zero framework dependencies beyond the
data-science stack (`pandas`/`numpy`/`scipy`/`matplotlib`) plus
`openpyxl`/`python-docx`/`reportlab` for file formats. If you'd rather run
this behind FastAPI with strict Pydantic validation and async support, the
route handlers in `backend/app.py` are a thin enough layer that porting
them is mechanical — the actual pipeline logic in `backend/agents/` doesn't
change at all.

### Storage

Sessions (including in-memory dataframes) live in a process-local dict with
a 4-hour TTL — fine for a single-instance deployment or local use. For
multi-instance/production deployment, swap `SessionStore` in `store.py` for
a Redis- or DuckDB-backed implementation; nothing above that layer needs to
know the difference, since everything talks to it through `get()` /
`create()`.

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

## Testing

No pytest dependency required — both test files are directly runnable and
use plain asserts:

```bash
PYTHONPATH=. python tests/test_agents_smoke.py   # agents in isolation
PYTHONPATH=. python tests/test_api.py             # full HTTP flow incl. feedback loop
```

If you do have pytest installed, `pytest tests/` will collect and run
neither of these automatically since they use `main()`/module-level scripts
rather than `test_*` functions — that's intentional, so the suite has zero
required dependencies beyond what's already in `requirements.txt`. Feel
free to wrap them in `test_*` functions if you're integrating this into a
CI pipeline that already has pytest.

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
- **LLM-narrated commentary**: the Report Agent currently writes
  deterministic, template-driven prose from structured data (which is why
  it needs no API key and is fully reproducible). If you want an LLM to
  narrate the findings instead of/alongside the template, that's an
  additional step you'd bolt onto `report_agent.build_report()` — feed it
  the same structured `AnalysisResult`/`CleaningLog` dicts rather than raw
  data, for the same auditability reasons the rest of the pipeline follows.

---

## Design notes on the UI

The interface treats the pipeline as a schematic — five stage "chips"
connected by a circuit trace across the top, each one an inspectable
module rather than a black box. Safe/review/danger states use a
consistent amber/cyan/red vocabulary throughout (amber = pending or
needs your input, cyan = safe/complete, violet = statistical/analysis
content, red = something needs rejecting or removing). No dependency on
a JS framework or build tool — it's vanilla JS against the REST API, so
there's nothing to compile and nothing to break between Node versions.
