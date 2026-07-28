# Pipeline Orchestrator

A production-grade data pipeline that ingests messy real-world files, cleans
them with full transparency, runs real statistical analysis, generates
charts only where the data actually supports them, and writes a narrative
report — all through a five-stage multi-agent pipeline with a human-in-the-
loop checkpoint and a feedback loop from analysis back to cleaning.

No API keys, no external services, no build step.

![stages](https://img.shields.io/badge/stages-5-ffb454) ![deps](https://img.shields.io/badge/dependencies-8-52d9c9) ![build step](https://img.shields.io/badge/build%20step-none-9d8cff)

---

## Quick Start

Commands are grouped by OS. Run them **in the order shown** from the project
root (the folder containing `run.py`).

### Windows (PowerShell)

```powershell
# 1. Unzip (skip if already extracted)
Expand-Archive -Path data-pipeline-orchestrator.zip -DestinationPath .
cd data-pipeline-orchestrator

# 2. Create a virtual environment (use py -3.12 if you have multiple Python versions)
py -3.12 -m venv .venv

# 3. Allow the activation script to run (one-time, this session only)
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass

# 4. Activate the virtual environment
.venv\Scripts\Activate.ps1

# 5. Confirm the interpreter version
python --version

# 6. Install dependencies
python -m pip install --upgrade pip
python -m pip install -r requirements.txt

# 7. Run the server
python run.py
```

Leave that window open — it's running the server. Open **http://127.0.0.1:5050**
in a browser and click **"Use the sample dataset"**.

To run tests, open a **second** PowerShell window (leave the server running
in the first):

```powershell
cd data-pipeline-orchestrator
.venv\Scripts\Activate.ps1
$env:PYTHONPATH = "."
python tests\test_agents_smoke.py
python tests\test_api.py
```

### macOS / Linux (bash / zsh)

```bash
# 1. Unzip (skip if already extracted)
unzip data-pipeline-orchestrator.zip
cd data-pipeline-orchestrator

# 2. Create a virtual environment
python3 -m venv .venv

# 3. Activate the virtual environment
source .venv/bin/activate

# 4. Confirm the interpreter version
python --version

# 5. Install dependencies
python -m pip install --upgrade pip
python -m pip install -r requirements.txt

# 6. Run the server
python run.py
```

Leave that terminal open — it's running the server. Open **http://127.0.0.1:5050**
in a browser and click **"Use the sample dataset"**.

To run tests, open a **second** terminal (leave the server running in the first):

```bash
cd data-pipeline-orchestrator
source .venv/bin/activate
PYTHONPATH=. python tests/test_agents_smoke.py
PYTHONPATH=. python tests/test_api.py
```

### Custom port (any OS)

```bash
PORT=8080 python run.py        # macOS/Linux
```
```powershell
$env:PORT = "8080"; python run.py   # Windows PowerShell
```

### Driving the API directly instead of the browser

**Windows (PowerShell), second window, server already running:**

```powershell
$sid = (Invoke-RestMethod -Uri "http://127.0.0.1:5050/api/session" -Method Post).session_id
Invoke-RestMethod -Uri "http://127.0.0.1:5050/api/session/$sid/ingest-sample" -Method Post | Out-Null
$plan = Invoke-RestMethod -Uri "http://127.0.0.1:5050/api/session/$sid/clean/preview" -Method Post
$approvedIds = $plan.cleaning_plan.proposed_actions | ForEach-Object { $_.action_id }
$applyBody = @{ approved_action_ids = $approvedIds } | ConvertTo-Json
Invoke-RestMethod -Uri "http://127.0.0.1:5050/api/session/$sid/clean/apply" -Method Post -Body $applyBody -ContentType "application/json" | Out-Null
Invoke-RestMethod -Uri "http://127.0.0.1:5050/api/session/$sid/analyze" -Method Post | Out-Null
Invoke-RestMethod -Uri "http://127.0.0.1:5050/api/session/$sid/visualize" -Method Post | Out-Null
$reportBody = @{ audience = "executive" } | ConvertTo-Json
$report = Invoke-RestMethod -Uri "http://127.0.0.1:5050/api/session/$sid/report" -Method Post -Body $reportBody -ContentType "application/json"
$report.report_result.markdown
Invoke-WebRequest -Uri "http://127.0.0.1:5050/api/session/$sid/report/download?format=pdf" -OutFile report.pdf
```

**macOS/Linux (bash), second terminal, server already running:**

```bash
SID=$(curl -s -X POST http://127.0.0.1:5050/api/session | python3 -c "import sys,json;print(json.load(sys.stdin)['session_id'])")

curl -s -X POST http://127.0.0.1:5050/api/session/$SID/ingest-sample > /dev/null

curl -s -X POST http://127.0.0.1:5050/api/session/$SID/clean/preview > /tmp/plan.json
python3 -c "
import json
d = json.load(open('/tmp/plan.json'))
ids = [a['action_id'] for a in d['cleaning_plan']['proposed_actions']]
json.dump({'approved_action_ids': ids}, open('/tmp/apply_body.json','w'))
"

curl -s -X POST http://127.0.0.1:5050/api/session/$SID/clean/apply -H "Content-Type: application/json" -d @/tmp/apply_body.json > /dev/null
curl -s -X POST http://127.0.0.1:5050/api/session/$SID/analyze > /dev/null
curl -s -X POST http://127.0.0.1:5050/api/session/$SID/visualize > /dev/null

curl -s -X POST http://127.0.0.1:5050/api/session/$SID/report -H "Content-Type: application/json" -d '{"audience":"executive"}'
curl -s -o report.pdf "http://127.0.0.1:5050/api/session/$SID/report/download?format=pdf"
```

### Stopping the server

`Ctrl+C` in the terminal/window running `python run.py`.

### Troubleshooting

- **`ModuleNotFoundError: No module named 'backend'`** when running tests
  directly — set `PYTHONPATH` first (shown above), or run as a module
  instead: `python -m tests.test_agents_smoke`.
- **`unzip`/`Expand-Archive` not found** — on Windows use
  `Expand-Archive`, not `unzip` (PowerShell has no `unzip` command).
- **Activation script blocked on Windows** — run
  `Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass` once per
  session before `Activate.ps1`.
- **`pip install` seems to hang or gets interrupted** — `scipy` and
  `matplotlib` are large downloads; let the single install command run to
  completion rather than splitting/cancelling it.
- **Port already in use / stale server** — find and stop any leftover
  Python process (`Get-Process python | Stop-Process -Force` on Windows,
  `pkill -f run.py` on macOS/Linux) before starting a fresh one.

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

---

## Architecture

run.py entry point — python run.py
backend/
app.py Flask routes (HTTP concerns only — no data logic)
schemas.py dataclass data contracts between agents/stages
store.py in-memory session store (dataframe handles, not raw data)
dtype_utils.py pandas 2.x/3.x string-dtype compatibility shim
report_export.py markdown -> PDF / DOCX renderers
agents/
ingestion_agent.py
cleaning_agent.py
analysis_agent.py
visualization_agent.py
report_agent.py
static/
index.html, css/style.css, js/app.js zero-build-step vanilla frontend
sample_data/
messy_sales_data.csv synthetic dataset with dupes, nulls, outliers,
inconsistent state names, numbers-as-text
tests/
test_agents_smoke.py exercises all 5 agents directly
test_api.py exercises the full HTTP API incl. the feedback loop


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
