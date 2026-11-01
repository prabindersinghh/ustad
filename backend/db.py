"""SQLite persistence. Deliberately small -- swap for Postgres behind these
functions if the deployment needs it."""
from __future__ import annotations

import json
import sqlite3
import time
import uuid
from typing import Any

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS procedures (
    id          TEXT PRIMARY KEY,
    title       TEXT NOT NULL,
    machine     TEXT,
    language    TEXT DEFAULT 'en',
    steps_json  TEXT NOT NULL,
    source_transcript TEXT,
    created_at  REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS sessions (
    id            TEXT PRIMARY KEY,
    procedure_id  TEXT NOT NULL,
    worker_name   TEXT,
    mode          TEXT NOT NULL,
    started_at    REAL NOT NULL,
    finished_at   REAL,
    outcome       TEXT
);

CREATE TABLE IF NOT EXISTS step_events (
    id           TEXT PRIMARY KEY,
    session_id   TEXT NOT NULL,
    step_index   INTEGER NOT NULL,
    verdict      TEXT NOT NULL,
    confidence   REAL,
    reason       TEXT,
    grounding    TEXT,
    evidence_path TEXT,
    attempt      INTEGER DEFAULT 1,
    latency_ms   INTEGER,
    backend      TEXT,
    created_at   REAL NOT NULL
);
"""

# Columns added after the first schema. Applied on every init so an existing
# database upgrades in place instead of needing a wipe mid-hackathon.
MIGRATIONS = [("step_events", "backend", "TEXT")]


def connect() -> sqlite3.Connection:
    config.DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(config.DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def init() -> None:
    with connect() as conn:
        conn.executescript(SCHEMA)
        for table, column, coltype in MIGRATIONS:
            existing = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
            if column not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {coltype}")


def new_id() -> str:
    return uuid.uuid4().hex[:12]


# ---------------------------------------------------------------- procedures

def save_procedure(
    title: str,
    steps: list[dict[str, Any]],
    *,
    machine: str | None = None,
    language: str = "en",
    transcript: str | None = None,
) -> str:
    pid = new_id()
    with connect() as conn:
        conn.execute(
            "INSERT INTO procedures (id, title, machine, language, steps_json,"
            " source_transcript, created_at) VALUES (?,?,?,?,?,?,?)",
            (pid, title, machine, language, json.dumps(steps), transcript, time.time()),
        )
    return pid


def get_procedure(pid: str) -> dict[str, Any] | None:
    with connect() as conn:
        row = conn.execute("SELECT * FROM procedures WHERE id = ?", (pid,)).fetchone()
    if not row:
        return None
    out = dict(row)
    out["steps"] = json.loads(out.pop("steps_json"))
    return out


def list_procedures() -> list[dict[str, Any]]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT id, title, machine, language, steps_json, created_at"
            " FROM procedures ORDER BY created_at DESC"
        ).fetchall()
    out = []
    for row in rows:
        d = dict(row)
        d["step_count"] = len(json.loads(d.pop("steps_json")))
        out.append(d)
    return out


# ------------------------------------------------------------------ sessions

def start_session(procedure_id: str, worker_name: str | None, mode: str = "guide") -> str:
    sid = new_id()
    with connect() as conn:
        conn.execute(
            "INSERT INTO sessions (id, procedure_id, worker_name, mode, started_at)"
            " VALUES (?,?,?,?,?)",
            (sid, procedure_id, worker_name, mode, time.time()),
        )
    return sid


def finish_session(session_id: str, outcome: str) -> None:
    with connect() as conn:
        conn.execute(
            "UPDATE sessions SET finished_at = ?, outcome = ? WHERE id = ?",
            (time.time(), outcome, session_id),
        )


def log_step_event(
    session_id: str,
    step_index: int,
    verdict: str,
    *,
    confidence: float | None = None,
    reason: str | None = None,
    grounding: str | None = None,
    evidence_path: str | None = None,
    attempt: int = 1,
    latency_ms: int | None = None,
    backend: str | None = None,
) -> str:
    eid = new_id()
    with connect() as conn:
        conn.execute(
            "INSERT INTO step_events (id, session_id, step_index, verdict, confidence,"
            " reason, grounding, evidence_path, attempt, latency_ms, backend, created_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                eid, session_id, step_index, verdict, confidence, reason,
                grounding, evidence_path, attempt, latency_ms, backend, time.time(),
            ),
        )
    return eid


def session_log(session_id: str) -> dict[str, Any]:
    with connect() as conn:
        session = conn.execute(
            "SELECT * FROM sessions WHERE id = ?", (session_id,)
        ).fetchone()
        events = conn.execute(
            "SELECT * FROM step_events WHERE session_id = ? ORDER BY created_at",
            (session_id,),
        ).fetchall()
    return {
        "session": dict(session) if session else None,
        "events": [dict(e) for e in events],
    }


def list_sessions(limit: int = 50) -> list[dict[str, Any]]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT s.*, p.title AS procedure_title,"
            " (SELECT COUNT(*) FROM step_events e WHERE e.session_id = s.id"
            "   AND e.verdict = 'fail') AS failures"
            " FROM sessions s LEFT JOIN procedures p ON p.id = s.procedure_id"
            " ORDER BY s.started_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return [dict(r) for r in rows]


def metrics() -> dict[str, Any]:
    """Headline numbers for the dashboard and the demo."""
    with connect() as conn:
        row = conn.execute(
            "SELECT"
            "  COUNT(*) AS checks,"
            "  SUM(CASE WHEN verdict='pass' THEN 1 ELSE 0 END) AS passes,"
            "  SUM(CASE WHEN verdict='fail' THEN 1 ELSE 0 END) AS fails,"
            "  AVG(latency_ms) AS avg_latency_ms"
            " FROM step_events"
        ).fetchone()
        sessions = conn.execute(
            "SELECT COUNT(*) AS n,"
            " AVG(CASE WHEN finished_at IS NOT NULL"
            "     THEN finished_at - started_at END) AS avg_duration_s"
            " FROM sessions"
        ).fetchone()
    d = dict(row or {})
    d.update(dict(sessions or {}))
    with connect() as conn:
        by_backend = conn.execute(
            "SELECT COALESCE(backend,'unknown') AS backend, COUNT(*) AS n"
            " FROM step_events GROUP BY 1"
        ).fetchall()
    d["checks_by_backend"] = {r["backend"]: r["n"] for r in by_backend}

    checks = d.get("checks") or 0
    d["pass_rate"] = round((d.get("passes") or 0) / checks, 3) if checks else None
    if d.get("avg_latency_ms") is not None:
        d["avg_latency_ms"] = round(d["avg_latency_ms"])
    if d.get("avg_duration_s") is not None:
        d["avg_duration_s"] = round(d["avg_duration_s"], 1)
    return d
