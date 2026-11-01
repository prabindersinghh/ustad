"""WebSocket protocol tests for guide mode's start/resume handshake.

These exercise the reconnect-resume path (the one new piece of server logic)
without the model: start, resume, skip and announce never call Muse Spark, only
`check` does. A temp DB keeps the real one untouched.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def ctx(tmp_path, monkeypatch):
    from backend import config, db
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "test.db")
    db.init()
    pid = db.save_procedure(
        "Test proc", machine=None,
        steps=[
            {"index": 0, "title": "One", "instruction": "do one", "target_phrase": "a",
             "success_criteria": "", "keyframe_index": 0, "common_mistake": None, "hazard": None},
            {"index": 1, "title": "Two", "instruction": "do two", "target_phrase": "b",
             "success_criteria": "", "keyframe_index": 0, "common_mistake": None, "hazard": None},
            {"index": 2, "title": "Three", "instruction": "do three", "target_phrase": "c",
             "success_criteria": "", "keyframe_index": 0, "common_mistake": None, "hazard": None},
        ],
    )
    from backend.main import app
    return TestClient(app), db, pid


def test_start_announces_first_step(ctx):
    client, db, pid = ctx
    with client.websocket_connect("/ws/guide") as ws:
        ws.send_json({"type": "start", "procedure_id": pid})
        session = ws.receive_json()
        assert session["type"] == "session"
        assert session["steps"] == 3
        speak = ws.receive_json()
        assert speak["type"] == "speak"
        assert speak["step"] == 1


def test_resume_picks_up_after_last_passed_step(ctx):
    client, db, pid = ctx
    # Start to create a session id.
    with client.websocket_connect("/ws/guide") as ws:
        ws.send_json({"type": "start", "procedure_id": pid})
        sid = ws.receive_json()["session_id"]
        ws.receive_json()  # step 1 announce

    # Two steps passed before the connection dropped.
    db.log_step_event(sid, 0, "pass")
    db.log_step_event(sid, 1, "pass")

    # Reconnect and resume: server must put the worker on step 3, not step 1.
    with client.websocket_connect("/ws/guide") as ws:
        ws.send_json({"type": "resume", "session_id": sid})
        session = ws.receive_json()
        assert session["type"] == "session"
        assert session["resumed_at"] == 3
        speak = ws.receive_json()
        assert speak["type"] == "speak"
        assert speak["step"] == 3


def test_resume_completes_when_all_steps_passed(ctx):
    client, db, pid = ctx
    with client.websocket_connect("/ws/guide") as ws:
        ws.send_json({"type": "start", "procedure_id": pid})
        sid = ws.receive_json()["session_id"]
        ws.receive_json()

    for i in range(3):
        db.log_step_event(sid, i, "pass")

    with client.websocket_connect("/ws/guide") as ws:
        ws.send_json({"type": "resume", "session_id": sid})
        msg = ws.receive_json()
        assert msg["type"] == "complete"
        assert msg["session_id"] == sid


def test_resume_unknown_session_errors(ctx):
    client, db, pid = ctx
    with client.websocket_connect("/ws/guide") as ws:
        ws.send_json({"type": "resume", "session_id": "deadbeef"})
        msg = ws.receive_json()
        assert msg["type"] == "error"
