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

CREATE TABLE IF NOT EXISTS usage (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    at      REAL NOT NULL,
    model   TEXT NOT NULL,
    kind    TEXT NOT NULL,
    units   REAL NOT NULL,
    cost    REAL NOT NULL,
    note    TEXT
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

CREATE TABLE IF NOT EXISTS procedure_history (
    procedure_id TEXT NOT NULL,
    version      INTEGER NOT NULL,
    steps_json   TEXT NOT NULL,
    note         TEXT,
    created_at   REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS proposals (
    id           TEXT PRIMARY KEY,
    procedure_id TEXT NOT NULL,
    version      INTEGER NOT NULL,
    items_json   TEXT NOT NULL,
    status       TEXT NOT NULL DEFAULT 'open',
    created_at   REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS second_opinions (
    session_id    TEXT NOT NULL,
    event_id      TEXT NOT NULL,
    step_index    INTEGER NOT NULL,
    model         TEXT NOT NULL,
    first_verdict TEXT,
    verdict       TEXT,
    agrees        INTEGER,
    confidence    REAL,
    reason        TEXT,
    latency_ms    INTEGER,
    error         TEXT,
    created_at    REAL NOT NULL,
    PRIMARY KEY (session_id, event_id, model)
);

CREATE TABLE IF NOT EXISTS translations (
    procedure_id TEXT NOT NULL,
    version      INTEGER NOT NULL,
    lang         TEXT NOT NULL,
    data_json    TEXT NOT NULL,
    created_at   REAL NOT NULL,
    PRIMARY KEY (procedure_id, version, lang)
);
"""

# Columns added after the first schema. Applied on every init so an existing
# database upgrades in place instead of needing a wipe mid-hackathon.
MIGRATIONS = [
    ("step_events", "backend", "TEXT"),
    ("step_events", "detections", "TEXT"),          # SAM boxes on the evidence photo
    ("procedures", "version", "INTEGER DEFAULT 1"),
    ("sessions", "procedure_version", "INTEGER"),
]

# Verdicts that are a judged check (as opposed to a skip, a question, a notice).
CHECK_VERDICTS = ("pass", "fail", "retry")


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


def save_second_opinion(session_id: str, row: dict[str, Any]) -> None:
    agrees = row.get("agrees")
    with connect() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO second_opinions (session_id, event_id, step_index, model, first_verdict,"
            " verdict, agrees, confidence, reason, latency_ms, error, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (session_id, row["event_id"], row["step_index"], row["model"], row.get("first_verdict"), row.get("verdict"),
             None if agrees is None else int(agrees), row.get("confidence"), row.get("reason"),
             row.get("latency_ms"), row.get("error"), time.time()))


def second_opinions(session_id: str) -> list[dict[str, Any]]:
    with connect() as conn:
        rows = conn.execute("SELECT * FROM second_opinions WHERE session_id = ? ORDER BY step_index",
                            (session_id,)).fetchall()
    return [dict(r) for r in rows]


def get_translation(pid: str, version: int, lang: str) -> dict[str, Any] | None:
    with connect() as conn:
        row = conn.execute("SELECT data_json FROM translations WHERE procedure_id = ? AND version = ? AND lang = ?",
                           (pid, version, lang)).fetchone()
    return json.loads(row["data_json"]) if row else None


def save_translation(pid: str, version: int, lang: str, data: dict[str, Any]) -> None:
    with connect() as conn:
        conn.execute("INSERT OR REPLACE INTO translations (procedure_id, version, lang, data_json, created_at)"
                     " VALUES (?,?,?,?,?)", (pid, version, lang, json.dumps(data, ensure_ascii=False), time.time()))


def update_procedure_steps(pid: str, steps: list[dict[str, Any]], note: str) -> int:
    """Replace a procedure's steps, keeping the old ones in procedure_history.
    Returns the new version number."""
    with connect() as conn:
        row = conn.execute("SELECT steps_json, version FROM procedures WHERE id = ?", (pid,)).fetchone()
        if not row:
            raise KeyError(pid)
        version = int(row["version"] or 1)
        conn.execute(
            "INSERT INTO procedure_history (procedure_id, version, steps_json, note, created_at)"
            " VALUES (?,?,?,?,?)", (pid, version, row["steps_json"], note, time.time()))
        conn.execute("UPDATE procedures SET steps_json = ?, version = ? WHERE id = ?",
                     (json.dumps(steps), version + 1, pid))
    return version + 1


def procedure_history(pid: str) -> list[dict[str, Any]]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT version, note, created_at FROM procedure_history WHERE procedure_id = ?"
            " ORDER BY version DESC", (pid,)).fetchall()
    return [dict(r) for r in rows]


def procedure_history_steps(pid: str, version: int) -> list[list[dict[str, Any]]]:
    """The steps a procedure had at `version` (a one-item list, or empty)."""
    with connect() as conn:
        row = conn.execute("SELECT steps_json FROM procedure_history WHERE procedure_id = ? AND version = ?",
                           (pid, version)).fetchone()
    return [json.loads(row["steps_json"])] if row else []


def save_proposal(pid: str, version: int, items: list[dict[str, Any]]) -> str:
    prid = new_id()
    with connect() as conn:
        conn.execute(
            "INSERT INTO proposals (id, procedure_id, version, items_json, status, created_at)"
            " VALUES (?,?,?,?,?,?)", (prid, pid, version, json.dumps(items), "open", time.time()))
    return prid


def get_proposal(prid: str) -> dict[str, Any] | None:
    with connect() as conn:
        row = conn.execute("SELECT * FROM proposals WHERE id = ?", (prid,)).fetchone()
    if not row:
        return None
    out = dict(row)
    out["items"] = json.loads(out.pop("items_json"))
    return out


def latest_proposal(pid: str) -> dict[str, Any] | None:
    with connect() as conn:
        row = conn.execute("SELECT id FROM proposals WHERE procedure_id = ? ORDER BY created_at DESC LIMIT 1",
                           (pid,)).fetchone()
    return get_proposal(row["id"]) if row else None


def set_proposal_status(prid: str, status: str) -> None:
    with connect() as conn:
        conn.execute("UPDATE proposals SET status = ? WHERE id = ?", (status, prid))


def list_procedures() -> list[dict[str, Any]]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT id, title, machine, language, steps_json, created_at, version"
            " FROM procedures ORDER BY created_at DESC"
        ).fetchall()
    out = []
    for row in rows:
        d = dict(row)
        d["step_count"] = len(json.loads(d.pop("steps_json")))
        out.append(d)
    return out


# ------------------------------------------------------------------ sessions

def start_session(procedure_id: str, worker_name: str | None, mode: str = "guide",
                  version: int | None = None) -> str:
    sid = new_id()
    with connect() as conn:
        conn.execute(
            "INSERT INTO sessions (id, procedure_id, worker_name, mode, started_at, procedure_version)"
            " VALUES (?,?,?,?,?,?)",
            (sid, procedure_id, worker_name, mode, time.time(), version),
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
    detections: list[dict[str, Any]] | None = None,
) -> str:
    eid = new_id()
    with connect() as conn:
        conn.execute(
            "INSERT INTO step_events (id, session_id, step_index, verdict, confidence,"
            " reason, grounding, evidence_path, attempt, latency_ms, backend, detections, created_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                eid, session_id, step_index, verdict, confidence, reason,
                grounding, evidence_path, attempt, latency_ms, backend,
                json.dumps(detections) if detections else None, time.time(),
            ),
        )
    return eid


def session_log(session_id: str) -> dict[str, Any]:
    with connect() as conn:
        session = conn.execute(
            "SELECT s.*, p.title AS procedure_title FROM sessions s"
            " LEFT JOIN procedures p ON p.id = s.procedure_id WHERE s.id = ?", (session_id,)
        ).fetchone()
        events = conn.execute(
            "SELECT * FROM step_events WHERE session_id = ? ORDER BY created_at",
            (session_id,),
        ).fetchall()
    out = []
    for e in events:
        d = dict(e)
        try:
            d["detections"] = json.loads(d["detections"]) if d.get("detections") else []
        except (TypeError, ValueError):
            d["detections"] = []
        out.append(d)
    return {"session": dict(session) if session else None, "events": out}


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


# ------------------------------------------------------------------ insights

DONE_VERDICTS = ("pass", "inferred", "confirmed", "skipped")


def step_insights(procedure_id: str) -> dict[str, Any]:
    """Where operators struggle on one procedure, across all its guided runs.

    Per step: how many runs reached it, how often the first check passed,
    errors caught, median time spent on it, and the most recent reasons it
    failed. Time on a step runs from the previous step's completion (or the
    session start) to this step's completion.
    """
    import statistics

    with connect() as conn:
        sessions = conn.execute(
            "SELECT id, started_at FROM sessions WHERE procedure_id = ? AND mode = 'guide'",
            (procedure_id,),
        ).fetchall()
        events = conn.execute(
            "SELECT e.* FROM step_events e JOIN sessions s ON s.id = e.session_id"
            " WHERE s.procedure_id = ? ORDER BY e.created_at",
            (procedure_id,),
        ).fetchall()

    by_session: dict[str, list[dict[str, Any]]] = {}
    for e in events:
        by_session.setdefault(e["session_id"], []).append(dict(e))
    started = {s["id"]: s["started_at"] for s in sessions}

    steps: dict[int, dict[str, Any]] = {}

    def slot(i: int) -> dict[str, Any]:
        return steps.setdefault(i, {"step": i + 1, "runs": 0, "first_try": 0, "caught": 0,
                                    "inferred": 0, "skipped": 0, "asked": 0, "times": [],
                                    "reasons": [], "questions": []})

    for sid, evs in by_session.items():
        last_done = started.get(sid, evs[0]["created_at"])
        seen: set[int] = set()
        for e in evs:
            st = slot(e["step_index"])
            if e["verdict"] in ("asked", "notice", "help"):
                if e["verdict"] in ("asked", "help"):
                    st["asked"] += 1
                    if e["reason"]:
                        st["questions"].append(e["reason"])
                continue                         # not an attempt at the step
            if e["step_index"] not in seen:
                seen.add(e["step_index"])
                st["runs"] += 1
                if e["verdict"] in ("pass", "confirmed"):
                    st["first_try"] += 1
            if e["verdict"] == "fail":
                st["caught"] += 1
                if e["reason"]:
                    st["reasons"].append(e["reason"])
            elif e["verdict"] == "inferred":
                st["inferred"] += 1
            elif e["verdict"] == "skipped":
                st["skipped"] += 1
            if e["verdict"] in DONE_VERDICTS:
                st["times"].append(e["created_at"] - last_done)
                last_done = e["created_at"]

    out = []
    for i in sorted(steps):
        st = steps.pop(i)
        times = st.pop("times")
        st["median_s"] = round(statistics.median(times), 1) if times else None
        st["first_try_rate"] = round(st["first_try"] / st["runs"], 2) if st["runs"] else None
        st["reasons"] = list(dict.fromkeys(reversed(st["reasons"])))[:3]
        st["questions"] = list(dict.fromkeys(reversed(st["questions"])))[:5]
        out.append(st)
    hardest = max(out, key=lambda s: (s["caught"], -(s["first_try_rate"] or 1)), default=None)
    return {"runs": len(by_session), "steps": out,
            "hardest_step": hardest["step"] if hardest and hardest["caught"] else None}


# --------------------------------------------------------------------- usage

def log_usage(model: str, kind: str, units: float, cost: float, note: str | None = None) -> None:
    """One billed API call (or a batch of audio seconds) and what it cost in USD."""
    with connect() as conn:
        conn.execute(
            "INSERT INTO usage (at, model, kind, units, cost, note) VALUES (?,?,?,?,?,?)",
            (time.time(), model, kind, units, cost, note),
        )


def usage_total() -> float:
    with connect() as conn:
        row = conn.execute("SELECT COALESCE(SUM(cost), 0) FROM usage").fetchone()
    return float(row[0] or 0)


def usage_summary() -> dict[str, Any]:
    """Spend so far, by model, plus the last hour, for the dashboard meter."""
    with connect() as conn:
        rows = conn.execute(
            "SELECT model, kind, COUNT(*) AS calls, SUM(units) AS units, SUM(cost) AS cost"
            " FROM usage GROUP BY model, kind ORDER BY cost DESC"
        ).fetchall()
        hour = conn.execute(
            "SELECT COALESCE(SUM(cost),0) FROM usage WHERE at > ?", (time.time() - 3600,)
        ).fetchone()[0]
    by = [dict(r) for r in rows]
    for r in by:
        r["cost"] = round(r["cost"] or 0, 4)
        r["units"] = round(r["units"] or 0, 2)
    total = round(sum(r["cost"] for r in by), 4)
    return {
        "total_usd": total,
        "last_hour_usd": round(float(hour or 0), 4),
        "budget_usd": config.BUDGET_USD,
        "remaining_usd": round(config.BUDGET_USD - total, 4),
        "by_model": by,
    }


def metrics() -> dict[str, Any]:
    """Headline numbers for the dashboard and the demo."""
    with connect() as conn:
        row = conn.execute(
            "SELECT"
            "  COUNT(*) AS checks,"
            "  SUM(CASE WHEN verdict='pass' THEN 1 ELSE 0 END) AS passes,"
            "  SUM(CASE WHEN verdict='fail' THEN 1 ELSE 0 END) AS fails,"
            "  AVG(latency_ms) AS avg_latency_ms"
            " FROM step_events WHERE verdict IN ('pass','fail','retry')"
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
            " FROM step_events WHERE verdict IN ('pass','fail','retry') GROUP BY 1"
        ).fetchall()
    d["checks_by_backend"] = {r["backend"]: r["n"] for r in by_backend}

    checks = d.get("checks") or 0
    d["pass_rate"] = round((d.get("passes") or 0) / checks, 3) if checks else None
    if d.get("avg_latency_ms") is not None:
        d["avg_latency_ms"] = round(d["avg_latency_ms"])
    if d.get("avg_duration_s") is not None:
        d["avg_duration_s"] = round(d["avg_duration_s"], 1)
    return d
