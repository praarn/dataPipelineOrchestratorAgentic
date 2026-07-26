"""
SessionStore — the "shared execution environment" referenced in the spec
(section 4 / 5). Dataframes live here, in memory, keyed by a session id and
a stage name. Agents and API routes pass around `dataframe_ref` strings
(e.g. "session:cleaned") instead of raw data. Only summaries/profiles ever
get handed to an "agent" for reasoning.

Swap point for production: replace the dict-backed store with a DuckDB
table or a Redis/temp-file-backed store keyed the same way — nothing
above this layer needs to change, which is the whole point of the
handle-based design.
"""
from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Optional

import pandas as pd

SESSION_TTL_SECONDS = 60 * 60 * 4  # 4 hours


@dataclass
class SessionState:
    session_id: str
    created_at: float = field(default_factory=time.time)
    last_touched: float = field(default_factory=time.time)

    file_name: Optional[str] = None
    raw_df: Optional[pd.DataFrame] = None
    cleaned_df: Optional[pd.DataFrame] = None

    ingestion_report: Optional[dict] = None
    cleaning_plan: Optional[dict] = None
    cleaning_log: Optional[dict] = None
    analysis_result: Optional[dict] = None
    visualization_result: Optional[dict] = None
    report_result: Optional[dict] = None

    # bookkeeping for the analysis -> cleaning feedback loop
    reopened_for_column: Optional[str] = None

    def stage(self) -> str:
        if self.report_result:
            return "reported"
        if self.visualization_result:
            return "visualized"
        if self.analysis_result:
            return "analyzed"
        if self.cleaning_log:
            return "cleaned"
        if self.cleaning_plan:
            return "cleaning_previewed"
        if self.ingestion_report:
            return "ingested"
        return "empty"


class SessionStore:
    """Thread-safe in-memory session store with lazy TTL eviction."""

    def __init__(self):
        self._lock = threading.RLock()
        self._sessions: dict[str, SessionState] = {}

    def create(self) -> SessionState:
        with self._lock:
            self._evict_expired()
            sid = uuid.uuid4().hex
            state = SessionState(session_id=sid)
            self._sessions[sid] = state
            return state

    def get(self, session_id: str) -> Optional[SessionState]:
        with self._lock:
            state = self._sessions.get(session_id)
            if state:
                state.last_touched = time.time()
            return state

    def _evict_expired(self):
        now = time.time()
        expired = [
            sid for sid, s in self._sessions.items()
            if now - s.last_touched > SESSION_TTL_SECONDS
        ]
        for sid in expired:
            del self._sessions[sid]

    def stats(self) -> dict:
        with self._lock:
            return {"active_sessions": len(self._sessions)}


store = SessionStore()
