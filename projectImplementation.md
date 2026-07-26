```markdown
# Implementation Documentation — Data Pipeline Orchestrator

## 1. Overview

The Data Pipeline Orchestrator is a five-stage, agent-based system that ingests
messy real-world data files, cleans them with full human-in-the-loop
transparency, runs genuine statistical analysis, generates charts only where
the data supports them, and produces a narrative report — all through a REST
API backed by a zero-build-step vanilla JS frontend.

The five stages map to five independent, stateless agent modules:

1. **Ingestion Agent** — loads raw files, infers schema, profiles data quality
2. **Cleaning Agent** — proposes and applies data-quality fixes with full audit logging
3. **Analysis Agent** — runs real statistical tests and ranks findings by significance
4. **Visualization Agent** — generates charts matched to what each finding actually supports
5. **Report Agent** — stitches everything into a narrative document (Markdown/PDF/DOCX)

Design goals, in priority order:

- **No transformation happens silently.** Every cleaning action is logged,
  whether applied, rejected, or merely flagged for review.
- **Human-in-the-loop.** Cleaning proposes a dry-run plan; nothing is applied
  until explicitly approved.
- **Feedback loop.** Analysis can flag a column as suspicious and reopen the
  Cleaning checkpoint on it, without discarding upstream progress.
- **Statistical honesty.** Every finding is backed by a real `scipy` test
  (t-test, ANOVA, Pearson correlation, linear regression) — never a heuristic
  guess at significance.
- **Zero-build frontend.** No npm, no bundler, no framework — just HTML/CSS/JS
  served directly by Flask.
- **Minimal dependency footprint.** No FastAPI/Pydantic — stdlib
  `dataclasses` provide the same "typed data contract" guarantees with zero
  extra installs beyond the data-science stack.

---

## 2. Architecture

```
run.py                          Entry point — python run.py
backend/
  app.py                        Flask routes (HTTP concerns only)
  schemas.py                    Dataclass-based data contracts between stages
  store.py                      In-memory session store (dataframe handles)
  dtype_utils.py                pandas 2.x/3.x string-dtype compatibility shim
  report_export.py              Markdown -> PDF / DOCX renderers
  agents/
    ingestion_agent.py
    cleaning_agent.py
    analysis_agent.py
    visualization_agent.py
    report_agent.py
static/
  index.html                    SPA shell + <template> blocks per stage
  css/style.css                 Design system (schematic/instrumentation theme)
  js/app.js                     State machine + API client + rendering
sample_data/
  messy_sales_data.csv          Synthetic dataset with dupes, nulls, outliers,
                                 inconsistent state names, numbers-as-text
tests/
  test_agents_smoke.py          Exercises all 5 agents directly
  test_api.py                   Exercises the full HTTP API incl. feedback loop
```

### 2.1 Core design principle: handles, not blobs

Dataframes never leave the backend process as raw data. They live in
`SessionStore` (`backend/store.py`), keyed by a session id, and every
stage/agent communicates via small structured summaries defined in
`schemas.py` (`IngestionReport`, `CleaningPlan`, `CleaningLog`,
`AnalysisResult`, `VisualizationResult`, `ReportResult`). This keeps the
system usable on non-trivial datasets and keeps every agent decision
auditable and JSON-serializable for the UI.

### 2.2 Why Flask + dataclasses instead of FastAPI + Pydantic

Functionally interchangeable — the data contracts are identical shapes
either way. This build uses Flask and stdlib `dataclasses` so the project
runs with zero framework dependencies beyond
`pandas`/`numpy`/`scipy`/`matplotlib` plus `openpyxl`/`python-docx`/
`reportlab` for file format support. Porting to FastAPI/Pydantic would only
require rewriting `backend/app.py`'s route handlers — the agent logic in
`backend/agents/` is framework-agnostic and would not change.

### 2.3 Session storage

Sessions (including in-memory dataframes) live in a process-local dict with
a 4-hour TTL. Suitable for a single-instance deployment or local use. For
multi-instance/production deployment, `SessionStore` in `store.py` is
designed to be swapped for a Redis- or DuckDB-backed implementation without
requiring changes anywhere above that layer — everything talks to it
through `create()` / `get()`.

---

## 3. Data Contracts (`backend/schemas.py`)

All contracts are stdlib `dataclasses` with a `to_dict()` helper for JSON
serialization (`Serializable` mixin). No third-party validation library is
used; shapes are enforced by convention and by the agents that construct
them.

| Contract | Produced by | Fields (key ones) |
|---|---|---|
| `ColumnProfile` | Ingestion | column, inferred_type, null_count, null_pct, unique_count, sample_values, min/max |
| `IngestionReport` | Ingestion | source_type, row_count, column_count, schema (list of ColumnProfile), structural_issues, sample_rows |
| `CleaningAction` | Cleaning | action_id, action, column, reason, count, method, severity (safe/review), status (proposed/approved/rejected) |
| `CleaningPlan` | Cleaning (preview) | proposed_actions, quality_score_before, rows_before, columns_before |
| `CleaningLog` | Cleaning (apply) | applied_actions, rejected_actions, quality_score_before/after, rows_before/after |
| `Finding` | Analysis | summary, statistical_support, recommended_chart, confidence, related_columns, effect_size, p_value, caveat |
| `AnalysisResult` | Analysis | goal, summary_stats, findings (list of Finding) |
| `ChartSpec` | Visualization | finding_id, chart_type, image_base64, caption, n |
| `SkippedFinding` | Visualization | finding_id, reason |
| `VisualizationResult` | Visualization | charts, findings_without_charts |
| `ReportResult` | Report | audience, markdown |

---

## 4. Agent Implementations

### 4.1 Ingestion Agent (`ingestion_agent.py`)

**Responsibility:** load raw bytes into a DataFrame and establish ground
truth about shape and quality. Makes **no judgment calls** about what to fix
— that is the Cleaning Agent's job. Separation of concerns: ingestion states
facts, cleaning decides actions.

**Supported formats:** CSV, TSV, JSON (flat array or `{"data": [...]}` /
`{"records": [...]}` wrapped), Excel (`.xlsx`/`.xls`, first sheet used with
a structural-issue note if multiple sheets exist).

**Key behaviors:**
- Encoding fallback: tries `utf-8` → `utf-8-sig` → `latin-1`, logs which was
  used if not the default.
- Bad-row quarantine: CSV rows with mismatched column counts are dropped and
  counted, not silently discarded.
- Type inference (`_infer_type`): distinguishes integer / float / date /
  boolean / categorical / identifier / text / constant, using a mix of
  pandas dtype checks and heuristic coercion (numeric-string detection,
  date-string detection via `pd.to_datetime(..., format="mixed")`,
  uniqueness ratio for identifier vs. free-text vs. categorical).
- Column profiling: null count/percentage, unique count, min/max (type-aware),
  up to 4 sample values per column.
- Structural issue detection: empty columns, unnamed columns (stray
  delimiters), zero-row files.

**Output:** `(pd.DataFrame, IngestionReport)`.

### 4.2 Cleaning Agent (`cleaning_agent.py`)

**Responsibility:** detect data-quality issues and apply fixes transparently.
Split into two phases:

- `preview(df) -> CleaningPlan` — **never mutates** the dataframe. Only
  inspects and proposes.
- `apply(df, plan_actions, approved_ids) -> (cleaned_df, CleaningLog)` —
  executes only the approved subset, in a dependency-safe order (see §4.2.4).

**4.2.1 Severity model**

Every proposed action carries a `severity`:
- `"safe"` — pre-checked by default in the UI; auto-applies unless the user
  explicitly unchecks it (e.g. dropping exact duplicate rows, imputing a
  column with <15% missing values, coercing an obviously-numeric text
  column).
- `"review"` — unchecked by default; requires explicit opt-in (e.g. outlier
  removal via IQR, imputing a column with >50% missing values).

This ensures nothing that could plausibly destroy real information is ever
applied without explicit human consent.

**4.2.2 Detectors implemented**

- **Exact duplicate rows** — `df.duplicated()`, safe to auto-drop.
- **Missing values** — per column, branches on missing percentage:
  - ≥50% missing → flagged for review only (`flagged_not_dropped`), never
    auto-imputed; sparse columns are too risky to fabricate values for.
  - Numeric columns → median imputation (chosen over mean for outlier
    robustness).
  - Categorical/text columns → mode (most frequent value) imputation.
  - Safe vs. review severity is threshold-gated at 15% missing.
- **Type coercion** — text columns that parse as >90% numeric (after
  stripping thousands-separator commas) are flagged for coercion to
  numeric dtype.
- **Categorical standardization** — two independent detectors:
  - *Generic case/whitespace consolidation*: groups values by
    `strip().lower()` and merges variants that differ only in case or
    surrounding whitespace (e.g. `"NY"` / `" ny "` → canonical form chosen
    by frequency).
  - *US state canonicalization*: a curated 50-state name↔abbreviation
    lookup table. Only fires when ≥80% of a column's values match a known
    state name or abbreviation, to avoid false positives on unrelated text
    columns. Takes priority over the generic detector when both would fire
    on the same column — this is a genuine semantic/domain judgment
    (`"NY"` = `"New York"` = `"ny"`), not just superficial normalization.
- **Outlier detection** — IQR method (Q1 − 1.5×IQR, Q3 + 1.5×IQR) per
  numeric column. Always `severity="review"` — outliers are flagged, never
  auto-removed, since extreme values are sometimes the most important data
  points.

**4.2.3 Quality score**

A 0–1 heuristic blending three components:
- **Completeness** (50% weight) — 1 minus overall null ratio.
- **Uniqueness** (30% weight) — 1 minus duplicate-row ratio.
- **Consistency** (20% weight) — average, across object columns, of
  `normalized_unique_count / raw_unique_count` (penalizes case/whitespace
  variance as a proxy for messiness).

Computed before and after cleaning and surfaced in both the UI and the
final report.

**4.2.4 Execution ordering (bug fix — see §6.1)**

`apply()` executes approved actions in a **fixed dependency-safe order**
(`_EXECUTION_PRIORITY`), independent of the order they were proposed/
displayed in:

```
dropped_rows (dedupe)  →  coerced_type  →  imputed  →  standardized  →  flagged_not_dropped
```

This guarantees, for example, that a column of `"1,234.56"`-style strings
is correctly parsed to numeric *before* any imputation or outlier statistic
is computed from it — imputing first would treat comma-formatted values as
unparseable/missing and silently overwrite them with the column median.

A shared helper, `_to_numeric_safe()`, strips thousands-separator commas
before every numeric coercion (imputation, outlier bounds, type coercion)
so this class of bug cannot resurface if execution order changes again.

**4.2.5 Logging guarantee**

`CleaningLog` always contains both `applied_actions` and `rejected_actions`
— including actions that were approved but *failed* to apply (wrapped in a
defensive `try/except` per action, with the failure reason appended to the
action's `note`). The log is built in original proposal order for
UI readability, even though execution ran in dependency order.

### 4.3 Analysis Agent (`analysis_agent.py`)

**Responsibility:** run exploratory + statistical analysis on the **cleaned**
dataframe and surface only the findings that clear a significance/effect-size
bar — not a dump of every computable statistic.

**Detectors, each backed by a real `scipy.stats` test:**

- **Trend over time** (`_trend_finding`) — requires a detected date column
  (via `_find_date_column`, checking `is_datetime64_any_dtype` or >90%
  successful `pd.to_datetime` parse rate on text columns) and a numeric
  column. Uses `scipy.stats.linregress` on days-since-start vs. value.
  Surfaced only if `p < 0.05` and `|r| >= 0.2`. Reports percent change over
  the observed period.
- **Correlation** (`_correlation_finding`) — `scipy.stats.pearsonr` between
  every pair of numeric columns (capped at 15 pairs to bound cost).
  Classifies strength as moderate (`|r| >= 0.3`) or strong (`|r| >= 0.6`);
  also explicitly surfaces **null results** ("no significant correlation")
  when `p >= 0.2`, so the report doesn't only show positive hits.
- **Group differences** (`_group_difference_finding`) — categorical (≤12
  levels) × numeric pairs. Uses `scipy.stats.ttest_ind` (Welch's, unequal
  variance) for exactly 2 groups, `scipy.stats.f_oneway` (one-way ANOVA)
  for 3+. Requires each group to have ≥3 samples. Surfaced only if
  `p < 0.05`.

**Ranking:** all findings sorted by `(confidence_tier, p_value)` ascending;
top 6 kept. Confidence tiers: high (`p < 0.01`), medium, low.

**Cleaning-aware caveats:** every finding checks whether its `related_columns`
intersect with columns the Cleaning Agent modified, or whether duplicate
rows were dropped. If so, an explicit caveat is attached (e.g. *"analysis
excludes 15 row(s) removed as duplicates during cleaning; column(s)
['revenue'] were modified during cleaning — worth confirming that didn't
bias this result"*). This directly implements the "nothing happens silently"
principle at the analysis layer.

### 4.4 Visualization Agent (`visualization_agent.py`)

**Responsibility:** render a chart only when the finding's data actually
supports one; otherwise route the finding to a plain-text conclusion instead
of forcing a misleading chart.

**Chart types, matched to `Finding.recommended_chart`:**
- `"line"` — time series with markers, for trend findings.
- `"scatter_with_trendline"` — scatter plot + `np.polyfit` degree-1
  regression line, for correlation findings.
- `"grouped_bar"` — mean-per-category bars with sample-size (`n=`)
  annotations per bar, for group-difference findings.
- `"none"` — no chart type is registered; the finding is routed to
  `findings_without_charts` with an explanation.

**Design constraints enforced in code, not just convention:**
- Bar chart y-axis always starts at 0 (`ax.set_ylim(bottom=0)`) — never a
  truncated/distorted axis.
- No dual y-axes, no 3D charts (not offered as options at all).
- Every chart is annotated with its sample size.
- A consistent dark theme (`#12161c` background, muted axis text) matches
  the frontend's design system, since PNGs are embedded directly into the
  UI and reports.
- Charts that fail to render (insufficient non-null overlapping data) are
  caught defensively and routed to `findings_without_charts` rather than
  crashing the pipeline.

**Output format:** base64-encoded PNG (`matplotlib` `Agg` backend), embedded
directly as `data:image/png;base64,...` — no separate file storage needed.

### 4.5 Report Agent (`report_agent.py`)

**Responsibility:** stitch Ingestion, Cleaning, Analysis, and Visualization
output into one coherent Markdown document. Deterministic, template-driven
— no LLM call, no API key required, fully reproducible from the same input
data.

**Two audience modes:**
- `"technical"` — full report: Executive Summary → Data Overview (schema
  table) → Methodology & Data Quality (full cleaning log, applied and
  reviewed-but-rejected actions) → Findings (numbered, each with stats,
  caveat, chart) → Caveats & Limitations.
- `"executive"` — condensed: Executive Summary → Findings (with charts and
  caveats) → a pointer to regenerate with `audience=technical` for the full
  methodology appendix. No schema table, no cleaning-log detail section.

The frontend was later changed (see §7) to expose only the executive
variant via the UI, since both were originally being generated but the
technical variant's extra sections weren't adding UI value for this
project's target user — the executive report remains available via the API
with `audience=technical` if needed.

**Methodology transparency:** the technical report's Section 2 is built
directly from `CleaningLog` — every applied action is listed with its
reason, row count, and method; every reviewed-but-rejected action is listed
separately. This is the single place in the system where a reader can
verify, end to end, that no transformation happened silently.

---

## 5. API Layer (`backend/app.py`)

Flask routes are intentionally thin — they handle only HTTP concerns
(uploads, session lookup, JSON (de)serialization, error mapping) and never
contain data-transformation logic, which stays entirely in
`backend/agents/`.

### 5.1 Route table

| Method | Route | Purpose |
|---|---|---|
| POST | `/api/session` | Create a new session |
| GET | `/api/session/<id>/state` | Full current pipeline state |
| POST | `/api/session/<id>/ingest` | Upload file (`multipart/form-data`, field `file`) |
| POST | `/api/session/<id>/ingest-sample` | Load bundled demo dataset |
| POST | `/api/session/<id>/clean/preview` | Dry-run: propose cleaning actions |
| POST | `/api/session/<id>/clean/apply` | Apply approved actions — body `{"approved_action_ids": [...]}` |
| POST | `/api/session/<id>/analyze` | Run analysis — body `{"goal": "optional text"}` |
| POST | `/api/session/<id>/analyze/flag` | Feedback loop — body `{"column": "..."}`, reopens cleaning |
| POST | `/api/session/<id>/visualize` | Generate charts for current findings |
| POST | `/api/session/<id>/report` | Generate report — body `{"audience": "technical" \| "executive"}` |
| GET | `/api/session/<id>/report/download?format=md\|pdf\|docx` | Download report |
| GET | `/api/health` | Liveness + active session count |

### 5.2 Sequential-stage enforcement

Most routes call `_require_stage()`, returning `409 Conflict` with a clear
message if a prerequisite stage hasn't run (e.g. calling `/analyze` before
`/clean/apply`). Prevents the pipeline from being driven out of order.

### 5.3 State invalidation cascade

Each write route resets downstream state to keep the session internally
consistent — e.g. re-running `/clean/preview` clears `cleaned_df`,
`cleaning_log`, `analysis_result`, `visualization_result`, and
`report_result`, since all of those were derived from a now-superseded
cleaning plan.

### 5.4 Feedback loop implementation

`/analyze/flag` re-runs `cleaning_agent.preview()` on the **original raw
dataframe** (not the previously-cleaned one), sets `reopened_for_column` on
the session state, and clears all downstream results. The frontend reads
`reopened_for_column` to highlight the relevant action row and pre-check it
in the UI, and displays a banner explaining why the checkpoint reopened.

### 5.5 Error handling

- Custom `ApiError` exception mapped to structured `{"error": "..."}` JSON
  with the appropriate status code (400/404/409/500).
- A generic `Exception` handler explicitly re-raises `HTTPException`
  instances (404, 405, etc.) unchanged rather than converting them to 500 —
  see bug fix in §6.3.
- File upload validation: extension allowlist, empty-file check,
  zero-data-row check after parsing, 60 MB size cap
  (`MAX_CONTENT_LENGTH`), all returning clear 400 messages rather than
  crashing.

### 5.6 Report export (`backend/report_export.py`)

A purpose-built parser (`_parse_blocks`), not a general CommonMark engine,
since `report_agent` only ever emits a known, fixed set of constructs
(headings, bullets, blockquotes, embedded base64 PNGs, simple pipe tables,
italic captions, horizontal rules). Two renderers consume the same parsed
block list:
- `markdown_to_pdf_bytes()` — `reportlab` Platypus (`SimpleDocTemplate`,
  `Paragraph`, `Table`, `Image`, `HRFlowable`).
- `markdown_to_docx_bytes()` — `python-docx` (`Document`, `add_heading`,
  `add_picture`, table styles).

---

## 6. Bugs Found and Fixed During Development

Each of the following was caught by writing and running real end-to-end
tests against a synthetic messy dataset — not by code review alone.

### 6.1 Comma-formatted number corruption (data-integrity bug)

**Symptom:** revenue values like `"1,486.13"` were silently replaced by the
column median after cleaning.

**Root cause:** cleaning actions were proposed in the order [dedupe →
impute missing → coerce type → standardize → flag outliers], and `apply()`
originally executed them in that same proposal order. Plain
`pd.to_numeric()` (without comma-stripping) run during imputation treated
every comma-formatted value as unparseable/`NaN`, so those real values got
overwritten by the median *before* the later type-coercion step — which
correctly strips commas — ever got a chance to run.

**Fix:** introduced `_EXECUTION_PRIORITY` to decouple proposal/display order
from execution order; `coerced_type` now always executes before `imputed`
regardless of proposal order. Also added a shared `_to_numeric_safe()`
helper used consistently across imputation, outlier-bound computation, and
type coercion, so the bug class cannot resurface even if ordering changes
again.

**Verification:** targeted test comparing a comma-formatted source value
against its cleaned counterpart via a stable natural key (order_date),
confirming exact numeric match.

### 6.2 pandas 3.0 string-dtype compatibility bug

**Symptom:** trend findings (e.g. revenue-over-time) were silently missing
from analysis output despite the underlying signal being present in the
data.

**Root cause:** pandas 3.0 introduced a new default `str`/`StringDtype`
storage for text columns, replacing the legacy `object` dtype. Code that
compared `series.dtype == object` (e.g. in `_find_date_column`) silently
stopped matching text columns, so date-column detection failed entirely
under pandas 3.x.

**Fix:** added `backend/dtype_utils.py` with `is_textual_dtype()` /
`textual_columns()` helpers using `pd.api.types.is_object_dtype()` OR
`pd.api.types.is_string_dtype()`, safe across both pandas 2.x and 3.x.
Replaced all direct `dtype == object` comparisons and
`select_dtypes(include="object")` calls throughout `ingestion_agent.py`,
`cleaning_agent.py`, and `analysis_agent.py`.

### 6.3 Flask error handler swallowing 404s

**Symptom:** requests to nonexistent routes or missing static files
returned `500 Internal Server Error` instead of the expected `404`.

**Root cause:** a catch-all `@app.errorhandler(Exception)` handler caught
Werkzeug's `NotFound`/`HTTPException` instances (which are themselves
`Exception` subclasses) and converted them into a generic 500 response.

**Fix:** the handler now checks `isinstance(err, HTTPException)` and
returns the exception unchanged in that case, only converting genuinely
unexpected exceptions to a 500.

---

## 7. Frontend (`static/`)

### 7.1 Design system

Vanilla HTML/CSS/JS, no build step, no framework. Visual theme is a
schematic/instrumentation aesthetic — the pipeline is rendered as a circuit
trace connecting five inspectable stage "chips," rather than a generic
dashboard.

**Tokens:**
- Colors: `--bg: #0c1017` (deep blue-charcoal), `--panel: #161d27`,
  `--amber: #ffb454` (pending/action), `--cyan: #52d9c9` (safe/complete),
  `--violet: #9d8cff` (analysis/stats), `--red: #ff6b6b` (review/danger).
- Type: Space Grotesk (headings), Inter (body/UI), IBM Plex Mono (data
  readouts, statistical output, action logs).

### 7.2 State management (`static/js/app.js`)

A hand-rolled state machine over five panels (`ingest`, `clean`, `analyze`,
`visualize`, `report`), backed entirely by the REST API — no client-side
business logic duplicates what the backend already computes. Session id
persists in `localStorage` so a page refresh doesn't lose progress
(re-fetches full state via `/api/session/<id>/state` on boot).

**Panel status derivation** (`panelStatus()`): purely a function of which
result fields are present in the session state (`ingestion_report`,
`cleaning_log`, etc.) — `done` / `active` / `locked`, with the conduit UI
only allowing navigation to `done` or `active` stages.

**Custom Markdown renderer** (`markdownToHTML()`): mirrors the same
block-parsing logic as `backend/report_export.py`, since the report's
markdown structure is a fixed, known set of constructs rather than
arbitrary CommonMark.

### 7.3 UI iteration: report audience toggle removed

The Report panel originally exposed a Technical/Executive audience toggle.
Because both variants embed the same chart images (which dominate the
total document byte size), the two outputs looked nearly identical at a
glance in the UI even though the underlying markdown genuinely differs
(the technical variant adds a schema table and full methodology section
further down the document). To avoid the confusing appearance of two
options producing "the same" output, the toggle was removed from the
frontend; the Report panel now always generates the executive-style report
via a single "Generate report" button. The backend endpoint still accepts
`audience: "technical"` for programmatic/API use.

---

## 8. Testing

No `pytest` dependency required — both test files are directly runnable,
plain-assert Python scripts:

```bash
PYTHONPATH=. python tests/test_agents_smoke.py   # agents in isolation
PYTHONPATH=. python tests/test_api.py             # full HTTP flow incl. feedback loop
```

**`test_agents_smoke.py`** — imports and calls all five agents directly
against `sample_data/messy_sales_data.csv`, printing ingestion schema,
proposed cleaning actions, before/after quality scores, ranked findings,
chart generation results, and report length. Used to eyeball correctness
during development.

**`test_api.py`** — uses Flask's `test_client()` to exercise the complete
HTTP surface: session creation, file ingestion, cleaning preview/apply,
analysis, the analysis→cleaning feedback loop (flag a column, verify
cleaning reopens, re-apply, re-analyze), visualization, report generation,
and all three download formats (md/pdf/docx) with byte-length and
content-type assertions. Also covers error paths: missing session (404),
missing file (400).

**Sample dataset** (`sample_data/messy_sales_data.csv`): synthetically
generated with `numpy`/`pandas`, 520 rows, deliberately containing:
- 20 exact duplicate rows
- ~25 missing `revenue` values, ~15 missing `customer_age` values
- 6 extreme `customer_age` outliers (150–200, clipped from a normal
  distribution otherwise bounded 18–90)
- 30 `revenue` values formatted with thousands-separator commas as strings
- `state` column with mixed case/whitespace/full-name/abbreviation variants
  per region (e.g. `"NY"`, `"ny"`, `"New York"`, `" New York"`)
- A genuine linear revenue trend over time and a real `region`/`state`
  group effect, so analysis has real signal to detect

---

## 9. Extension Points

- **New file formats** — add a `_load_*` function in `ingestion_agent.py`;
  register the extension in `SUPPORTED_EXTENSIONS` and `ALLOWED_EXTENSIONS`
  (`app.py`).
- **New cleaning strategies** — add a detector in
  `cleaning_agent.preview()` and a matching branch in `apply()`. Assign
  `severity="safe"` or `"review"` based on whether the action could
  plausibly discard real information; add to `_EXECUTION_PRIORITY` if it
  has an ordering dependency on other action types.
- **New statistical tests** — add a `_*_finding()` function in
  `analysis_agent.py` following the existing pattern: return `None` if the
  test doesn't clear a significance/effect-size bar, else return a
  populated `Finding`.
- **New chart types** — register a builder function in `CHART_BUILDERS`
  (`visualization_agent.py`), keyed by the `recommended_chart` string an
  analysis function emits.
- **Swap the session store** — implement `create()` / `get()` /
  `_evict_expired()` / `stats()` against Redis or DuckDB in `store.py`;
  nothing above that layer changes.
- **LLM-narrated reporting** — `report_agent.build_report()` is currently
  fully deterministic/template-driven (no API key needed, fully
  reproducible). An LLM narration layer could be bolted on afterward,
  fed the same structured `AnalysisResult`/`CleaningLog` dicts rather than
  raw data, preserving the system's auditability guarantees.

---

## 10. Known Limitations

- Session storage is in-memory and process-local — not suitable for
  multi-instance deployment without swapping `SessionStore`'s backend.
- Categorical standardization beyond US states (e.g. other country/region
  code systems) is not implemented — only case/whitespace folding and the
  US-state lookup table currently perform semantic consolidation.
- Statistical tests are limited to trend/correlation/group-difference
  patterns; no support for multi-way interactions, time-series
  decomposition, or non-parametric alternatives when normality assumptions
  are violated.
- Chart types are limited to line, scatter+trendline, and grouped bar;
  box plots and other chart types mentioned in early design discussion are
  not yet wired into `CHART_BUILDERS`.
- The 60 MB upload cap and 4-hour session TTL are hardcoded constants in
  `app.py` / `store.py` rather than configurable via environment variables.
```