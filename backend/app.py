"""
Flask application — the orchestration layer.

This wires the five agents together behind a small REST API. The pipeline
is sequential with two deliberate non-linear features called out in the
spec (section 5):

  1. A human-in-the-loop checkpoint after Cleaning: `clean/preview` never
     mutates data, only proposes actions; `clean/apply` requires an
     explicit set of approved action ids from the caller.
  2. A feedback loop from Analysis back to Cleaning: `analyze/flag` lets
     the caller send a suspicious column back for a second cleaning pass
     without losing the rest of the pipeline's state.

Every route is a thin wrapper around a pure function in backend/agents/*;
the routes themselves only handle HTTP concerns (uploads, session lookup,
JSON (de)serialization) and never contain data-transformation logic.
"""
from __future__ import annotations

import io
import os
import traceback

from flask import Flask, jsonify, request, send_file, send_from_directory, abort

from .agents import (
    analysis_agent,
    cleaning_agent,
    ingestion_agent,
    report_agent,
    visualization_agent,
)
from .schemas import CleaningAction
from .store import store

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATIC_DIR = os.path.join(BASE_DIR, "static")
SAMPLE_DATA_DIR = os.path.join(BASE_DIR, "sample_data")

ALLOWED_EXTENSIONS = {"csv", "tsv", "json", "xlsx", "xls"}
MAX_CONTENT_LENGTH = 60 * 1024 * 1024  # 60 MB

app = Flask(__name__, static_folder=STATIC_DIR, static_url_path="/static")
app.config["MAX_CONTENT_LENGTH"] = MAX_CONTENT_LENGTH


class ApiError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.message = message
        self.status = status


@app.errorhandler(ApiError)
def handle_api_error(err: ApiError):
    return jsonify({"error": err.message}), err.status


@app.errorhandler(413)
def handle_too_large(err):
    return jsonify({"error": "File too large (60 MB limit)."}), 413


@app.errorhandler(Exception)
def handle_unexpected(err):
    from werkzeug.exceptions import HTTPException
    if isinstance(err, HTTPException):
        # let Flask's own 404/405/etc responses through unchanged
        return err
    app.logger.error("Unhandled error: %s\n%s", err, traceback.format_exc())
    return jsonify({"error": f"Internal error: {err}"}), 500


def _get_session(session_id: str):
    state = store.get(session_id)
    if state is None:
        raise ApiError("Session not found or expired. Start a new session.", 404)
    return state


def _require_stage(state, required_attr: str, human_name: str):
    if getattr(state, required_attr) is None:
        raise ApiError(f"{human_name} has not run yet for this session.", 409)


# ---------------------------------------------------------------------------
# Static frontend
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    return send_from_directory(STATIC_DIR, "index.html")


@app.route("/<path:path>")
def static_passthrough(path):
    full = os.path.join(STATIC_DIR, path)
    if os.path.isfile(full):
        return send_from_directory(STATIC_DIR, path)
    abort(404)


# ---------------------------------------------------------------------------
# Session lifecycle
# ---------------------------------------------------------------------------

@app.route("/api/session", methods=["POST"])
def create_session():
    state = store.create()
    return jsonify({"session_id": state.session_id, "stage": state.stage()})


@app.route("/api/session/<session_id>/state", methods=["GET"])
def get_state(session_id):
    state = _get_session(session_id)
    return jsonify({
        "session_id": state.session_id,
        "stage": state.stage(),
        "file_name": state.file_name,
        "ingestion_report": state.ingestion_report,
        "cleaning_plan": state.cleaning_plan,
        "cleaning_log": state.cleaning_log,
        "analysis_result": state.analysis_result,
        "visualization_result": state.visualization_result,
        "report_result": state.report_result,
        "reopened_for_column": state.reopened_for_column,
    })


# ---------------------------------------------------------------------------
# Stage 1: Ingestion
# ---------------------------------------------------------------------------

@app.route("/api/session/<session_id>/ingest", methods=["POST"])
def ingest(session_id):
    state = _get_session(session_id)

    if "file" not in request.files:
        raise ApiError("No file uploaded. Attach a file under the 'file' field.")
    f = request.files["file"]
    if not f.filename:
        raise ApiError("Uploaded file has no name.")
    ext = f.filename.rsplit(".", 1)[-1].lower() if "." in f.filename else ""
    if ext not in ALLOWED_EXTENSIONS:
        raise ApiError(
            f"Unsupported file type '.{ext}'. Supported: {sorted(ALLOWED_EXTENSIONS)}"
        )

    raw_bytes = f.read()
    if not raw_bytes:
        raise ApiError("Uploaded file is empty.")

    try:
        df, report = ingestion_agent.ingest(raw_bytes, f.filename)
    except Exception as exc:
        raise ApiError(f"Could not parse file: {exc}")

    if df.empty:
        raise ApiError("The file parsed but contains zero data rows.")

    # reset any downstream state — a fresh ingest starts the pipeline over
    state.file_name = f.filename
    state.raw_df = df
    state.ingestion_report = report.to_dict()
    state.cleaned_df = None
    state.cleaning_plan = None
    state.cleaning_log = None
    state.analysis_result = None
    state.visualization_result = None
    state.report_result = None
    state.reopened_for_column = None

    return jsonify({"stage": state.stage(), "ingestion_report": state.ingestion_report})


@app.route("/api/session/<session_id>/ingest-sample", methods=["POST"])
def ingest_sample(session_id):
    """Load the bundled demo dataset instead of an upload — lets a first-time
    visitor exercise the whole pipeline with one click."""
    state = _get_session(session_id)
    sample_path = os.path.join(SAMPLE_DATA_DIR, "messy_sales_data.csv")
    if not os.path.isfile(sample_path):
        raise ApiError("Sample dataset is not available on this server.", 500)
    with open(sample_path, "rb") as fh:
        raw_bytes = fh.read()
    df, report = ingestion_agent.ingest(raw_bytes, "messy_sales_data.csv")

    state.file_name = "messy_sales_data.csv"
    state.raw_df = df
    state.ingestion_report = report.to_dict()
    state.cleaned_df = None
    state.cleaning_plan = None
    state.cleaning_log = None
    state.analysis_result = None
    state.visualization_result = None
    state.report_result = None
    state.reopened_for_column = None

    return jsonify({"stage": state.stage(), "ingestion_report": state.ingestion_report})


# ---------------------------------------------------------------------------
# Stage 2: Cleaning (dry-run preview -> human review -> apply)
# ---------------------------------------------------------------------------

@app.route("/api/session/<session_id>/clean/preview", methods=["POST"])
def clean_preview(session_id):
    state = _get_session(session_id)
    _require_stage(state, "raw_df", "Ingestion")

    plan = cleaning_agent.preview(state.raw_df)
    state.cleaning_plan = plan.to_dict()
    # a fresh preview invalidates any prior apply/downstream results
    state.cleaned_df = None
    state.cleaning_log = None
    state.analysis_result = None
    state.visualization_result = None
    state.report_result = None

    return jsonify({"stage": state.stage(), "cleaning_plan": state.cleaning_plan})


@app.route("/api/session/<session_id>/clean/apply", methods=["POST"])
def clean_apply(session_id):
    state = _get_session(session_id)
    _require_stage(state, "cleaning_plan", "Cleaning preview")

    body = request.get_json(silent=True) or {}
    approved_ids = set(body.get("approved_action_ids", []))

    actions = [CleaningAction(**a) for a in state.cleaning_plan["proposed_actions"]]
    cleaned_df, log = cleaning_agent.apply(state.raw_df, actions, approved_ids)

    state.cleaned_df = cleaned_df
    state.cleaning_log = log.to_dict()
    state.analysis_result = None
    state.visualization_result = None
    state.report_result = None
    state.reopened_for_column = None

    return jsonify({"stage": state.stage(), "cleaning_log": state.cleaning_log})


# ---------------------------------------------------------------------------
# Stage 3: Analysis (+ feedback loop back to Cleaning)
# ---------------------------------------------------------------------------

@app.route("/api/session/<session_id>/analyze", methods=["POST"])
def analyze(session_id):
    state = _get_session(session_id)
    _require_stage(state, "cleaned_df", "Cleaning")

    body = request.get_json(silent=True) or {}
    goal = body.get("goal") or None

    result = analysis_agent.analyze(state.cleaned_df, state.cleaning_log, goal=goal)
    state.analysis_result = result.to_dict()
    state.visualization_result = None
    state.report_result = None

    return jsonify({"stage": state.stage(), "analysis_result": state.analysis_result})


@app.route("/api/session/<session_id>/analyze/flag", methods=["POST"])
def analyze_flag(session_id):
    """Feedback loop: the analysis stage noticed something suspicious in a
    specific column (e.g. a value that survived cleaning) and wants a second
    cleaning pass on it. This re-opens the cleaning checkpoint pre-focused
    on that column, without discarding the ingestion report."""
    state = _get_session(session_id)
    _require_stage(state, "raw_df", "Ingestion")

    body = request.get_json(silent=True) or {}
    column = body.get("column")
    if not column or column not in state.raw_df.columns:
        raise ApiError("A valid 'column' must be provided to re-open cleaning.")

    plan = cleaning_agent.preview(state.raw_df)
    state.cleaning_plan = plan.to_dict()
    state.reopened_for_column = column
    state.cleaned_df = None
    state.cleaning_log = None
    state.analysis_result = None
    state.visualization_result = None
    state.report_result = None

    return jsonify({
        "stage": state.stage(),
        "cleaning_plan": state.cleaning_plan,
        "reopened_for_column": column,
    })


# ---------------------------------------------------------------------------
# Stage 4: Visualization
# ---------------------------------------------------------------------------

@app.route("/api/session/<session_id>/visualize", methods=["POST"])
def visualize(session_id):
    state = _get_session(session_id)
    _require_stage(state, "analysis_result", "Analysis")

    from .schemas import Finding
    findings = [Finding(**f) for f in state.analysis_result["findings"]]
    result = visualization_agent.visualize(state.cleaned_df, findings)
    state.visualization_result = result.to_dict()
    state.report_result = None

    return jsonify({"stage": state.stage(), "visualization_result": state.visualization_result})


# ---------------------------------------------------------------------------
# Stage 5: Report (optional)
# ---------------------------------------------------------------------------

@app.route("/api/session/<session_id>/report", methods=["POST"])
def report(session_id):
    state = _get_session(session_id)
    _require_stage(state, "visualization_result", "Visualization")

    body = request.get_json(silent=True) or {}
    audience = body.get("audience", "technical")
    if audience not in ("technical", "executive"):
        raise ApiError("audience must be 'technical' or 'executive'.")

    result = report_agent.build_report(
        state.ingestion_report, state.cleaning_log, state.analysis_result,
        state.visualization_result, audience=audience,
    )
    state.report_result = result.to_dict()

    return jsonify({"stage": state.stage(), "report_result": state.report_result})


@app.route("/api/session/<session_id>/report/download", methods=["GET"])
def report_download(session_id):
    state = _get_session(session_id)
    _require_stage(state, "report_result", "Report")

    fmt = request.args.get("format", "md")
    base_name = (state.file_name or "report").rsplit(".", 1)[0]

    if fmt == "md":
        buf = io.BytesIO(state.report_result["markdown"].encode("utf-8"))
        return send_file(buf, mimetype="text/markdown", as_attachment=True,
                          download_name=f"{base_name}_report.md")

    if fmt == "pdf":
        from .report_export import markdown_to_pdf_bytes
        pdf_bytes = markdown_to_pdf_bytes(state.report_result["markdown"])
        buf = io.BytesIO(pdf_bytes)
        return send_file(buf, mimetype="application/pdf", as_attachment=True,
                          download_name=f"{base_name}_report.pdf")

    if fmt == "docx":
        from .report_export import markdown_to_docx_bytes
        docx_bytes = markdown_to_docx_bytes(state.report_result["markdown"])
        buf = io.BytesIO(docx_bytes)
        return send_file(
            buf,
            mimetype="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            as_attachment=True, download_name=f"{base_name}_report.docx",
        )

    raise ApiError("format must be one of: md, pdf, docx")


@app.route("/api/health", methods=["GET"])
def health():
    return jsonify({"status": "ok", **store.stats()})
