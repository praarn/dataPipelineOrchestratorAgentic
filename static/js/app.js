// ============================================================================
// Pipeline Orchestrator — frontend application
//
// No build step, no framework: a small hand-rolled state machine over five
// panels (ingest, clean, analyze, visualize, report), backed by the REST
// API in backend/app.py. Session id persists in localStorage so a page
// refresh doesn't lose progress.
// ============================================================================

const API = {
  base: "/api",
  async _handle(res) {
    let body;
    try { body = await res.json(); } catch { body = null; }
    if (!res.ok) {
      const msg = (body && body.error) ? body.error : `Request failed (${res.status})`;
      throw new Error(msg);
    }
    return body;
  },
  createSession() {
    return fetch(`${this.base}/session`, { method: "POST" }).then(r => this._handle(r));
  },
  getState(sid) {
    return fetch(`${this.base}/session/${sid}/state`).then(r => this._handle(r));
  },
  ingest(sid, file) {
    const fd = new FormData();
    fd.append("file", file);
    return fetch(`${this.base}/session/${sid}/ingest`, { method: "POST", body: fd }).then(r => this._handle(r));
  },
  ingestSample(sid) {
    return fetch(`${this.base}/session/${sid}/ingest-sample`, { method: "POST" }).then(r => this._handle(r));
  },
  cleanPreview(sid) {
    return fetch(`${this.base}/session/${sid}/clean/preview`, { method: "POST" }).then(r => this._handle(r));
  },
  cleanApply(sid, approvedIds) {
    return fetch(`${this.base}/session/${sid}/clean/apply`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ approved_action_ids: approvedIds }),
    }).then(r => this._handle(r));
  },
  analyze(sid, goal) {
    return fetch(`${this.base}/session/${sid}/analyze`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ goal: goal || null }),
    }).then(r => this._handle(r));
  },
  analyzeFlag(sid, column) {
    return fetch(`${this.base}/session/${sid}/analyze/flag`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ column }),
    }).then(r => this._handle(r));
  },
  visualize(sid) {
    return fetch(`${this.base}/session/${sid}/visualize`, { method: "POST" }).then(r => this._handle(r));
  },
  report(sid, audience) {
    return fetch(`${this.base}/session/${sid}/report`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ audience }),
    }).then(r => this._handle(r));
  },
};

// ----------------------------------------------------------------------------
// App state
// ----------------------------------------------------------------------------

const STAGES = [
  { key: "ingest", label: "Ingest" },
  { key: "clean", label: "Clean" },
  { key: "analyze", label: "Analyze" },
  { key: "visualize", label: "Visualize" },
  { key: "report", label: "Report" },
];

const app = {
  sessionId: null,
  session: null,        // last full /state payload
  currentPanel: "ingest",
  audience: "executive",
  busy: {},              // panelKey -> bool, for loading indicators
};

function toast(message, type = "error") {
  const host = document.getElementById("toastHost");
  const el = document.createElement("div");
  el.className = `toast ${type === "success" ? "toast-success" : ""}`;
  el.textContent = message;
  host.appendChild(el);
  setTimeout(() => el.remove(), 5200);
}

function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, c => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));
}

function fmtPct(x) { return `${(x * 100).toFixed(1)}%`; }

// ----------------------------------------------------------------------------
// Panel status derivation
// ----------------------------------------------------------------------------

function panelStatus() {
  const s = app.session || {};
  const done = {
    ingest: !!s.ingestion_report,
    clean: !!s.cleaning_log,
    analyze: !!s.analysis_result,
    visualize: !!s.visualization_result,
    report: !!s.report_result,
  };
  const order = ["ingest", "clean", "analyze", "visualize", "report"];
  const status = {};
  let activeAssigned = false;
  for (const key of order) {
    if (done[key]) { status[key] = "done"; continue; }
    if (!activeAssigned) { status[key] = "active"; activeAssigned = true; continue; }
    status[key] = "locked";
  }
  return status;
}

// ----------------------------------------------------------------------------
// Conduit (top nav) rendering
// ----------------------------------------------------------------------------

function renderConduit() {
  const status = panelStatus();
  const nodesEl = document.getElementById("conduitNodes");
  const s = app.session || {};

  const readouts = {
    ingest: s.ingestion_report ? `${s.ingestion_report.row_count}×${s.ingestion_report.column_count}` : "",
    clean: s.cleaning_log ? `q=${s.cleaning_log.quality_score_after.toFixed(2)}` : "",
    analyze: s.analysis_result ? `${s.analysis_result.findings.length} found` : "",
    visualize: s.visualization_result ? `${s.visualization_result.charts.length} charts` : "",
    report: s.report_result ? "ready" : "",
  };

  nodesEl.innerHTML = STAGES.map((st, i) => {
    const state = status[st.key];
    const clickable = state !== "locked";
    return `
      <div class="conduit-node state-${state} ${clickable ? "is-clickable" : ""}" data-panel="${st.key}" ${clickable ? 'role="button" tabindex="0"' : ""}>
        <div class="node-chip">${state === "done" ? "✓" : String(i + 1).padStart(2, "0")}</div>
        <div class="node-label">${st.label}</div>
        <div class="node-readout">${readouts[st.key] || ""}</div>
      </div>`;
  }).join("");

  nodesEl.querySelectorAll(".is-clickable").forEach(node => {
    node.addEventListener("click", () => switchPanel(node.dataset.panel));
    node.addEventListener("keydown", e => {
      if (e.key === "Enter" || e.key === " ") { e.preventDefault(); switchPanel(node.dataset.panel); }
    });
  });

  const doneCount = Object.values(status).filter(v => v === "done").length;
  const progressPct = Math.min(1, doneCount / STAGES.length);
  const trace = document.getElementById("traceProgress");
  const totalLen = 840; // approx path length between x=80 and x=920
  trace.setAttribute("stroke-dasharray", `${totalLen * progressPct} ${1000}`);
}

function switchPanel(key) {
  const status = panelStatus();
  if (status[key] === "locked") return;
  app.currentPanel = key;
  renderAll();
}

// ----------------------------------------------------------------------------
// Small reusable widgets
// ----------------------------------------------------------------------------

function qualityGauge(value, label) {
  const pct = Math.max(0, Math.min(1, value));
  const r = 26, c = 2 * Math.PI * r;
  const color = pct >= 0.85 ? "var(--cyan)" : pct >= 0.6 ? "var(--amber)" : "var(--red)";
  return `
    <div class="gauge">
      <svg width="68" height="68" viewBox="0 0 68 68">
        <circle cx="34" cy="34" r="${r}" fill="none" stroke="var(--border)" stroke-width="6"/>
        <circle cx="34" cy="34" r="${r}" fill="none" stroke="${color}" stroke-width="6"
          stroke-dasharray="${c}" stroke-dashoffset="${c * (1 - pct)}"
          stroke-linecap="round" transform="rotate(-90 34 34)"/>
      </svg>
      <div class="gauge-value">${pct.toFixed(2)}</div>
      <div class="gauge-label">${label}</div>
    </div>`;
}

function confidenceBadge(conf) {
  const map = { high: "badge-cyan", medium: "badge-amber", low: "badge-mute" };
  return `<span class="badge ${map[conf] || "badge-mute"}">${conf} confidence</span>`;
}

function loadingRow(text) {
  return `<div class="loading-row"><span class="spinner"></span>${esc(text)}</div>`;
}

// ----------------------------------------------------------------------------
// Panel: Ingest
// ----------------------------------------------------------------------------

function renderIngestPanel() {
  const tpl = document.getElementById("tpl-ingest").content.cloneNode(true);
  const resultEl = tpl.getElementById("ingestResult");
  const s = app.session || {};
  const report = s.ingestion_report;

  if (report) {
    resultEl.innerHTML = ingestResultHTML(report);
  }

  document.getElementById("workspace").innerHTML = "";
  document.getElementById("workspace").appendChild(tpl);

  const dropzone = document.getElementById("dropzone");
  const fileInput = document.getElementById("fileInput");
  dropzone.addEventListener("click", () => fileInput.click());
  dropzone.addEventListener("keydown", e => { if (e.key === "Enter" || e.key === " ") fileInput.click(); });
  ["dragenter", "dragover"].forEach(evt => dropzone.addEventListener(evt, e => {
    e.preventDefault(); dropzone.classList.add("is-dragover");
  }));
  ["dragleave", "drop"].forEach(evt => dropzone.addEventListener(evt, e => {
    e.preventDefault(); dropzone.classList.remove("is-dragover");
  }));
  dropzone.addEventListener("drop", e => {
    const file = e.dataTransfer.files?.[0];
    if (file) handleIngest(file);
  });
  fileInput.addEventListener("change", () => {
    if (fileInput.files?.[0]) handleIngest(fileInput.files[0]);
  });

  document.getElementById("useSampleBtn").addEventListener("click", async () => {
    try {
      setBusy("ingest", true);
      document.getElementById("ingestResult").innerHTML = loadingRow("Loading sample dataset\u2026");
      const res = await API.ingestSample(app.sessionId);
      app.session = { ...app.session, ...res, stage: res.stage };
      await refreshState();
      app.currentPanel = "ingest";
      renderAll();
      toast("Sample dataset loaded.", "success");
    } catch (err) {
      toast(err.message);
      renderAll();
    } finally {
      setBusy("ingest", false);
    }
  });
}

async function handleIngest(file) {
  try {
    setBusy("ingest", true);
    document.getElementById("ingestResult").innerHTML = loadingRow(`Reading ${file.name}\u2026`);
    await API.ingest(app.sessionId, file);
    await refreshState();
    app.currentPanel = "ingest";
    renderAll();
    toast("File ingested.", "success");
  } catch (err) {
    toast(err.message);
    renderAll();
  } finally {
    setBusy("ingest", false);
  }
}

function ingestResultHTML(report) {
  const issues = report.structural_issues || [];
  const schema = report.schema || [];
  const sampleRows = report.sample_rows || [];
  const cols = schema.map(c => c.column);

  return `
    <div class="subpanel">
      <h3>Dataset overview <span class="type-pill">${esc(report.source_type)}</span></h3>
      <div class="stat-grid">
        <div class="stat-card"><div class="stat-num">${report.row_count.toLocaleString()}</div><div class="stat-label">rows</div></div>
        <div class="stat-card"><div class="stat-num">${report.column_count}</div><div class="stat-label">columns</div></div>
        <div class="stat-card"><div class="stat-num">${issues.length}</div><div class="stat-label">structural issues</div></div>
        <div class="stat-card"><div class="stat-num">${esc(report.file_name)}</div><div class="stat-label">source file</div></div>
      </div>
      ${issues.length ? `<ul class="issue-list">${issues.map(i => `<li>${esc(i)}</li>`).join("")}</ul>` : ""}
    </div>

    <div class="subpanel">
      <h3>Schema &amp; profile</h3>
      <div class="table-scroll">
        <table class="data-table">
          <thead><tr><th>Column</th><th>Type</th><th>Null %</th><th>Unique</th><th>Sample values</th></tr></thead>
          <tbody>
            ${schema.map(c => `
              <tr>
                <td class="col-name">${esc(c.column)}</td>
                <td><span class="type-pill">${esc(c.inferred_type)}</span></td>
                <td class="mono">${fmtPct(c.null_pct)}</td>
                <td class="mono">${c.unique_count.toLocaleString()}</td>
                <td class="mono">${esc((c.sample_values || []).slice(0, 3).join(", "))}</td>
              </tr>`).join("")}
          </tbody>
        </table>
      </div>
    </div>

    <div class="subpanel">
      <h3>Sample rows</h3>
      <div class="table-scroll">
        <table class="data-table">
          <thead><tr>${cols.map(c => `<th>${esc(c)}</th>`).join("")}</tr></thead>
          <tbody>
            ${sampleRows.map(row => `<tr>${cols.map(c => `<td class="mono">${esc(row[c])}</td>`).join("")}</tr>`).join("")}
          </tbody>
        </table>
      </div>
    </div>

    <div class="panel-actions">
      <button class="btn btn-primary" id="toCleanBtn" type="button">Continue to Cleaning →</button>
    </div>
  `;
}

// ----------------------------------------------------------------------------
// Panel: Clean
// ----------------------------------------------------------------------------

async function renderCleanPanel() {
  const tpl = document.getElementById("tpl-clean").content.cloneNode(true);
  document.getElementById("workspace").innerHTML = "";
  document.getElementById("workspace").appendChild(tpl);
  const body = document.getElementById("cleanBody");

  const s = app.session || {};

  if (!s.cleaning_plan && !s.cleaning_log) {
    body.innerHTML = loadingRow("Scanning for data quality issues\u2026");
    try {
      const res = await API.cleanPreview(app.sessionId);
      await refreshState();
      renderAll();
    } catch (err) {
      toast(err.message);
      body.innerHTML = `<div class="empty-note">Could not generate a cleaning plan: ${esc(err.message)}</div>`;
    }
    return;
  }

  if (s.cleaning_log) {
    body.innerHTML = cleaningLogHTML(s.cleaning_log, s.reopened_for_column);
    document.getElementById("toAnalyzeBtn")?.addEventListener("click", () => switchPanel("analyze"));
    document.getElementById("rerunPreviewBtn")?.addEventListener("click", async () => {
      try {
        setBusy("clean", true);
        await API.cleanPreview(app.sessionId);
        await refreshState();
        renderAll();
      } catch (err) { toast(err.message); }
      finally { setBusy("clean", false); }
    });
    return;
  }

  // cleaning_plan present, not yet applied
  body.innerHTML = cleaningPlanHTML(s.cleaning_plan, s.reopened_for_column);
  wireCleaningPlanEvents(s.cleaning_plan);
}

function actionVerb(action) {
  const map = {
    dropped_rows: "Drop rows",
    imputed: "Impute",
    standardized: "Standardize",
    coerced_type: "Coerce type",
    flagged_not_dropped: "Remove flagged rows",
  };
  return map[action] || action;
}

function cleaningPlanHTML(plan, reopenedFor) {
  const safe = plan.proposed_actions.filter(a => a.severity === "safe");
  const review = plan.proposed_actions.filter(a => a.severity === "review");

  const rowHTML = (a) => {
    const forceChecked = a.severity === "safe" || (reopenedFor && a.column === reopenedFor);
    const highlight = reopenedFor && a.column === reopenedFor ? "is-reopened" : "";
    return `
      <div class="action-row ${highlight}">
        <input type="checkbox" data-action-id="${a.action_id}" ${forceChecked ? "checked" : ""}>
        <div class="action-row-body">
          <div class="action-row-top">
            <span class="action-verb">${esc(actionVerb(a.action))}</span>
            ${a.column ? `<span class="action-col">${esc(a.column)}</span>` : `<span class="badge badge-mute">all columns</span>`}
            <span class="action-count">${a.count} row${a.count === 1 ? "" : "s"}</span>
          </div>
          <div class="action-reason">${esc(a.reason)}${a.method ? ` &middot; method: <span class="mono">${esc(a.method)}</span>` : ""}</div>
          ${a.note ? `<div class="action-note">${esc(a.note)}</div>` : ""}
        </div>
      </div>`;
  };

  return `
    ${reopenedFor ? `<div class="reopen-banner">↺ Re-opened for review at the request of the Analysis stage — column <strong>${esc(reopenedFor)}</strong> looked suspicious in a finding.</div>` : ""}

    <div class="subpanel">
      <h3>Quality before cleaning</h3>
      <div class="gauge-row">
        ${qualityGauge(plan.quality_score_before, "current")}
        <div style="color:var(--text-dim); font-size:13px; max-width:420px;">
          Score blends completeness (missing values), uniqueness (duplicates), and consistency
          (case/whitespace variants) into a single 0–1 read on data health.
        </div>
      </div>
    </div>

    <div class="subpanel">
      <div class="action-group-title"><span class="badge badge-cyan">Safe</span> pre-approved — uncheck to skip</div>
      ${safe.length ? safe.map(rowHTML).join("") : '<div class="empty-note">No safe auto-fixes were needed.</div>'}

      <div class="action-group-title"><span class="badge badge-amber">Needs review</span> unchecked by default</div>
      ${review.length ? review.map(rowHTML).join("") : '<div class="empty-note">Nothing flagged for manual review.</div>'}
    </div>

    <div class="panel-actions">
      <button class="btn btn-primary" id="applyCleaningBtn" type="button">Apply approved actions →</button>
    </div>
  `;
}

function wireCleaningPlanEvents(plan) {
  document.getElementById("applyCleaningBtn").addEventListener("click", async () => {
    const checked = Array.from(document.querySelectorAll('#cleanBody input[type="checkbox"]:checked'))
      .map(el => el.dataset.actionId);
    try {
      setBusy("clean", true);
      await API.cleanApply(app.sessionId, checked);
      await refreshState();
      renderAll();
      toast("Cleaning applied.", "success");
    } catch (err) {
      toast(err.message);
    } finally {
      setBusy("clean", false);
    }
  });
}

function cleaningLogHTML(log, reopenedFor) {
  const applied = log.applied_actions || [];
  const rejected = (log.rejected_actions || []).filter(a => a.status === "rejected");

  const rowHTML = (a, tone) => `
    <div class="action-row">
      <span class="badge ${tone === "applied" ? "badge-cyan" : "badge-mute"}" style="margin-top:2px;">${tone === "applied" ? "applied" : "skipped"}</span>
      <div class="action-row-body">
        <div class="action-row-top">
          <span class="action-verb">${esc(actionVerb(a.action))}</span>
          ${a.column ? `<span class="action-col">${esc(a.column)}</span>` : `<span class="badge badge-mute">all columns</span>`}
          <span class="action-count">${a.count} row${a.count === 1 ? "" : "s"}</span>
        </div>
        <div class="action-reason">${esc(a.reason)}</div>
        ${a.note ? `<div class="action-note">${esc(a.note)}</div>` : ""}
      </div>
    </div>`;

  return `
    <div class="subpanel">
      <h3>Result</h3>
      <div class="gauge-row">
        ${qualityGauge(log.quality_score_before, "before")}
        <span class="gauge-arrow">→</span>
        ${qualityGauge(log.quality_score_after, "after")}
        <div class="stat-grid" style="flex:1; margin-top:0;">
          <div class="stat-card"><div class="stat-num">${log.rows_before.toLocaleString()} → ${log.rows_after.toLocaleString()}</div><div class="stat-label">rows</div></div>
          <div class="stat-card"><div class="stat-num">${applied.length}</div><div class="stat-label">actions applied</div></div>
          <div class="stat-card"><div class="stat-num">${rejected.length}</div><div class="stat-label">left as-is</div></div>
        </div>
      </div>
    </div>

    <div class="subpanel">
      <h3>Applied</h3>
      ${applied.length ? applied.map(a => rowHTML(a, "applied")).join("") : '<div class="empty-note">Nothing was changed.</div>'}
    </div>

    ${rejected.length ? `
    <div class="subpanel">
      <h3>Reviewed, left as-is</h3>
      ${rejected.map(a => rowHTML(a, "skipped")).join("")}
    </div>` : ""}

    <div class="panel-actions">
      <button class="btn btn-ghost" id="rerunPreviewBtn" type="button">Re-run cleaning preview</button>
      <button class="btn btn-primary" id="toAnalyzeBtn" type="button">Continue to Analysis →</button>
    </div>
  `;
}

// ----------------------------------------------------------------------------
// Panel: Analyze
// ----------------------------------------------------------------------------

function renderAnalyzePanel() {
  const tpl = document.getElementById("tpl-analyze").content.cloneNode(true);
  document.getElementById("workspace").innerHTML = "";
  document.getElementById("workspace").appendChild(tpl);
  const body = document.getElementById("analyzeBody");
  const s = app.session || {};

  const goalRow = `
    <div class="goal-row">
      <input class="goal-input" id="goalInput" type="text" placeholder="Optional: what are you interested in? e.g. &quot;regional revenue differences&quot;" value="${esc(s.analysis_result?.goal || "")}">
      <button class="btn btn-primary" id="runAnalysisBtn" type="button">${s.analysis_result ? "Re-run analysis" : "Run analysis"}</button>
    </div>`;

  body.innerHTML = goalRow + (s.analysis_result ? analysisResultHTML(s.analysis_result) : "");

  document.getElementById("runAnalysisBtn").addEventListener("click", async () => {
    const goal = document.getElementById("goalInput").value.trim();
    try {
      setBusy("analyze", true);
      body.innerHTML = goalRow + loadingRow("Running statistical tests\u2026");
      await API.analyze(app.sessionId, goal);
      await refreshState();
      renderAll();
    } catch (err) {
      toast(err.message);
      renderAll();
    } finally {
      setBusy("analyze", false);
    }
  });

  if (s.analysis_result) {
    document.getElementById("toVisualizeBtn")?.addEventListener("click", () => switchPanel("visualize"));
    document.querySelectorAll(".flag-column-btn").forEach(btn => {
      btn.addEventListener("click", async () => {
        const column = btn.dataset.column;
        try {
          setBusy("analyze", true);
          await API.analyzeFlag(app.sessionId, column);
          await refreshState();
          app.currentPanel = "clean";
          renderAll();
          toast(`Sent "${column}" back to Cleaning for review.`, "success");
        } catch (err) {
          toast(err.message);
        } finally {
          setBusy("analyze", false);
        }
      });
    });
  }
}

function analysisResultHTML(result) {
  const findings = result.findings || [];
  if (!findings.length) {
    return `<div class="empty-note">No statistically significant findings were surfaced from this dataset.</div>
      <div class="panel-actions"><button class="btn btn-primary" id="toVisualizeBtn" type="button">Continue to Visualization →</button></div>`;
  }

  const cards = findings.map(f => `
    <div class="finding-card">
      <div class="finding-top">
        <div class="finding-summary">${esc(f.summary)}</div>
        ${confidenceBadge(f.confidence)}
      </div>
      <div class="finding-support">${esc(f.statistical_support)}</div>
      ${f.caveat ? `<div class="finding-caveat">⚠ ${esc(f.caveat)}.</div>` : ""}
      <div class="finding-actions">
        ${(f.related_columns || []).map(col => `
          <button class="btn btn-ghost btn-sm flag-column-btn" data-column="${esc(col)}" type="button">
            ↺ Flag "${esc(col)}" for re-cleaning
          </button>`).join("")}
      </div>
    </div>`).join("");

  return `
    <div class="subpanel">
      <h3>${findings.length} finding${findings.length === 1 ? "" : "s"}, ranked by confidence</h3>
      ${cards}
    </div>
    <div class="panel-actions">
      <button class="btn btn-primary" id="toVisualizeBtn" type="button">Continue to Visualization →</button>
    </div>`;
}

// ----------------------------------------------------------------------------
// Panel: Visualize
// ----------------------------------------------------------------------------

async function renderVisualizePanel() {
  const tpl = document.getElementById("tpl-visualize").content.cloneNode(true);
  document.getElementById("workspace").innerHTML = "";
  document.getElementById("workspace").appendChild(tpl);
  const body = document.getElementById("vizBody");
  const s = app.session || {};

  if (!s.visualization_result) {
    body.innerHTML = loadingRow("Rendering charts\u2026");
    try {
      await API.visualize(app.sessionId);
      await refreshState();
      renderAll();
    } catch (err) {
      toast(err.message);
      body.innerHTML = `<div class="empty-note">Could not generate charts: ${esc(err.message)}</div>`;
    }
    return;
  }

  body.innerHTML = vizResultHTML(s.visualization_result);
  document.getElementById("toReportBtn")?.addEventListener("click", () => switchPanel("report"));
}

function vizResultHTML(viz) {
  const charts = viz.charts || [];
  const skipped = viz.findings_without_charts || [];

  return `
    ${charts.length ? `
    <div class="chart-grid">
      ${charts.map(c => `
        <div class="chart-card">
          <img src="data:image/png;base64,${c.image_base64}" alt="${esc(c.caption)}" loading="lazy">
          <div class="chart-caption">${esc(c.caption)}<div class="chart-n">n = ${c.n ?? "—"}</div></div>
        </div>`).join("")}
    </div>` : '<div class="empty-note">No charts were generated for the current findings.</div>'}

    ${skipped.length ? `
    <div class="subpanel">
      <h3>Findings shown as text only</h3>
      ${skipped.map(s => `<div class="skip-card"><strong>No chart:</strong> ${esc(s.reason)}</div>`).join("")}
    </div>` : ""}

    <div class="panel-actions">
      <button class="btn btn-primary" id="toReportBtn" type="button">Continue to Report →</button>
    </div>
  `;
}

// ----------------------------------------------------------------------------
// Panel: Report
// ----------------------------------------------------------------------------

function renderReportPanel() {
  const tpl = document.getElementById("tpl-report").content.cloneNode(true);
  document.getElementById("workspace").innerHTML = "";
  document.getElementById("workspace").appendChild(tpl);
  const body = document.getElementById("reportBody");
  const s = app.session || {};

  body.innerHTML = `
    <div>
      <button class="btn btn-primary" id="generateReportBtn" type="button">${s.report_result ? "Regenerate" : "Generate"} report</button>
    </div>
    <div id="reportDocHost">${s.report_result ? reportDocHTML(s.report_result) : ""}</div>
  `;

  document.getElementById("generateReportBtn").addEventListener("click", async () => {
    try {
      setBusy("report", true);
      document.getElementById("reportDocHost").innerHTML = loadingRow("Writing report\u2026");
      await API.report(app.sessionId, app.audience);
      await refreshState();
      renderAll();
    } catch (err) {
      toast(err.message);
      renderAll();
    } finally {
      setBusy("report", false);
    }
  });

  wireDownloadButtons();
}

function reportDocHTML(reportResult) {
  return `
    <div class="report-doc">${markdownToHTML(reportResult.markdown)}</div>
    <div class="download-row">
      <button class="btn btn-secondary" data-fmt="md" type="button">↓ Download .md</button>
      <button class="btn btn-secondary" data-fmt="pdf" type="button">↓ Download .pdf</button>
      <button class="btn btn-secondary" data-fmt="docx" type="button">↓ Download .docx</button>
    </div>
  `;
}

function wireDownloadButtons() {
  document.querySelectorAll("[data-fmt]").forEach(btn => {
    btn.addEventListener("click", () => {
      const fmt = btn.dataset.fmt;
      window.location.href = `${API.base}/session/${app.sessionId}/report/download?format=${fmt}`;
    });
  });
}

// ----------------------------------------------------------------------------
// Minimal markdown renderer, purpose-built for report_agent's output shape
// (headings, bullets, blockquotes, base64 images, pipe tables, bold/italic/code)
// ----------------------------------------------------------------------------

function inlineMd(text) {
  let out = esc(text);
  out = out.replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>");
  out = out.replace(/`([^`]+)`/g, "<code>$1</code>");
  return out;
}

function markdownToHTML(md) {
  const lines = md.split("\n");
  let html = "";
  let i = 0;
  let inList = false;

  const closeList = () => { if (inList) { html += "</ul>"; inList = false; } };

  while (i < lines.length) {
    const line = lines[i];

    if (!line.trim()) { closeList(); i++; continue; }

    if (line.trim() === "---") { closeList(); html += "<hr>"; i++; continue; }

    const heading = line.match(/^(#{1,4})\s+(.*)/);
    if (heading) {
      closeList();
      const level = heading[1].length;
      html += `<h${level}>${inlineMd(heading[2])}</h${level}>`;
      i++; continue;
    }

    const img = line.match(/^!\[[^\]]*\]\((data:image\/png;base64,[A-Za-z0-9+/=]+)\)/);
    if (img) {
      closeList();
      html += `<img src="${img[1]}" alt="chart">`;
      i++; continue;
    }

    if (line.startsWith("> ")) {
      closeList();
      html += `<blockquote>${inlineMd(line.slice(2))}</blockquote>`;
      i++; continue;
    }

    if (line.startsWith("- ")) {
      if (!inList) { html += "<ul>"; inList = true; }
      html += `<li>${inlineMd(line.slice(2))}</li>`;
      i++; continue;
    }

    if (line.trim().startsWith("|")) {
      closeList();
      const tableLines = [];
      while (i < lines.length && lines[i].trim().startsWith("|")) { tableLines.push(lines[i].trim()); i++; }
      const rows = tableLines
        .map(l => l.replace(/^\||\|$/g, "").split("|").map(c => c.trim()))
        .filter(cells => !cells.every(c => /^:?-+:?$/.test(c)));
      if (rows.length) {
        html += "<table><thead><tr>" + rows[0].map(c => `<th>${inlineMd(c)}</th>`).join("") + "</tr></thead><tbody>";
        for (const row of rows.slice(1)) {
          html += "<tr>" + row.map(c => `<td>${inlineMd(c)}</td>`).join("") + "</tr>";
        }
        html += "</tbody></table>";
      }
      continue;
    }

    if (line.trim().startsWith("*") && line.trim().endsWith("*") && line.trim().length > 1) {
      closeList();
      html += `<p><em>${inlineMd(line.trim().slice(1, -1))}</em></p>`;
      i++; continue;
    }

    closeList();
    html += `<p>${inlineMd(line)}</p>`;
    i++;
  }
  closeList();
  return html;
}

// ----------------------------------------------------------------------------
// Orchestration: render dispatch, event delegation for "continue" buttons
// ----------------------------------------------------------------------------

function setBusy(panel, val) { app.busy[panel] = val; }

async function refreshState() {
  app.session = await API.getState(app.sessionId);
}

function renderAll() {
  renderConduit();
  const panel = app.currentPanel;
  if (panel === "ingest") renderIngestPanel();
  else if (panel === "clean") renderCleanPanel();
  else if (panel === "analyze") renderAnalyzePanel();
  else if (panel === "visualize") renderVisualizePanel();
  else if (panel === "report") renderReportPanel();

  // wire "continue" buttons that just switch panels (only present after data loaded)
  document.getElementById("toCleanBtn")?.addEventListener("click", () => switchPanel("clean"));
}

// ----------------------------------------------------------------------------
// Boot
// ----------------------------------------------------------------------------

async function boot() {
  const dot = document.getElementById("sessionDot");
  const label = document.getElementById("sessionLabel");

  const savedId = localStorage.getItem("pipeline_session_id");
  try {
    if (savedId) {
      app.sessionId = savedId;
      app.session = await API.getState(savedId);
      app.currentPanel = panelStatus_currentFromSession(app.session);
    } else {
      const created = await API.createSession();
      app.sessionId = created.session_id;
      localStorage.setItem("pipeline_session_id", app.sessionId);
      app.session = { session_id: app.sessionId, stage: "empty" };
    }
  } catch (err) {
    // saved session expired server-side — start fresh
    const created = await API.createSession();
    app.sessionId = created.session_id;
    localStorage.setItem("pipeline_session_id", app.sessionId);
    app.session = { session_id: app.sessionId, stage: "empty" };
  }

  dot.classList.add("live");
  label.textContent = app.sessionId.slice(0, 8);
  renderAll();
}

function panelStatus_currentFromSession(session) {
  app.session = session;
  const status = panelStatus();
  const firstActive = STAGES.find(st => status[st.key] === "active");
  return firstActive ? firstActive.key : "report";
}

document.getElementById("startOverBtn").addEventListener("click", async () => {
  if (!confirm("Start a new session? Current progress will be discarded.")) return;
  try {
    const created = await API.createSession();
    app.sessionId = created.session_id;
    localStorage.setItem("pipeline_session_id", app.sessionId);
    app.session = { session_id: app.sessionId, stage: "empty" };
    app.currentPanel = "ingest";
    document.getElementById("sessionLabel").textContent = app.sessionId.slice(0, 8);
    renderAll();
    toast("Started a new session.", "success");
  } catch (err) {
    toast(err.message);
  }
});

boot();
