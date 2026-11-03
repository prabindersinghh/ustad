"""Network drops: a recording or a guided job carries on across reconnects.

Capture keeps the take on the server by id; the phone rejoins it and syncs
what it recorded offline. Guide resumes at the exact step, skips and attempt
count intact. Models are mocked.
"""
from __future__ import annotations

import base64

import pytest
from fastapi.testclient import TestClient

from backend import live, meta_client, procedures

FRAME = "data:image/jpeg;base64," + base64.b64encode(b"\xff\xd8fakejpeg").decode()


def _until(ws, kind, limit=60):
    for _ in range(limit):
        msg = ws.receive_json()
        if msg["type"] == kind:
            return msg
    raise AssertionError(f"no {kind}")


@pytest.fixture
def cap(tmp_path, monkeypatch):
    from backend import config, db
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "r.db")
    monkeypatch.setattr(config, "MEDIA_DIR", tmp_path)
    monkeypatch.setattr(config, "VOICE", False)
    db.init()
    live._TAKES.clear()
    built = {}

    def fake_build(transcript, keyframes, *, hint=None, frame_times=None):
        built.update(transcript=transcript, frames=len(keyframes), times=list(frame_times or []))
        return {"title": "Job", "machine": None, "_frames": list(keyframes),
                "steps": [{"index": 0, "title": "A", "instruction": "Do A", "target_phrase": "valve",
                           "success_criteria": "x", "keyframe_index": 0}]}
    monkeypatch.setattr(procedures, "build_procedure", fake_build)
    from backend.main import app
    return TestClient(app), built, db


def test_capture_rejoins_and_syncs_offline_frames_and_narration(cap, monkeypatch):
    client, built, db = cap
    monkeypatch.setattr(meta_client, "transcribe", lambda audio, filename: "Close the valve fully.")
    with client.websocket_connect("/ws/capture") as ws:
        ws.send_json({"type": "start"})
        cid = _until(ws, "capture")["capture_id"]
        ws.send_json({"type": "frame", "data": FRAME})
        _until(ws, "frame_ack")
    # The socket dropped. The take is still on the server.
    assert cid in live._TAKES
    with client.websocket_connect("/ws/capture") as ws:
        ws.send_json({"type": "rejoin", "capture_id": cid, "at": 30.0, "paused": False})
        msg = _until(ws, "rejoined")
        assert msg["state"] == "recording" and msg["frames"] == 1
        ws.send_json({"type": "frame", "data": FRAME, "at": 12.0})          # taken while offline
        ws.send_json({"type": "mark", "at": 14.0, "quiet": True})
        pcm = base64.b64encode(b"\x01\x00" * 16000 * 2).decode()           # 2 s offline narration
        ws.send_json({"type": "audio_gap", "at": 13.0, "data": pcm})
        assert _until(ws, "frame_ack")["count"] == 2
        ws.send_json({"type": "stop"})
        proc = _until(ws, "procedure")
        assert proc["procedure"]["steps"][0]["ref_image"].startswith("media/ref_")
        assert "_frames" not in proc["procedure"]
    assert built["frames"] == 2 and built["times"][1] == 12.0
    assert "[0:13] Close the valve fully." in built["transcript"]
    assert built["transcript"].index("[0:13]") < built["transcript"].index("[0:14] [step marker]")


def test_a_drop_while_building_still_delivers_the_procedure(cap, monkeypatch):
    client, built, db = cap
    import time as _t

    def slow_build(*a, **k):
        _t.sleep(0.4)
        return {"title": "Job", "machine": None, "_frames": [], "steps": [
            {"index": 0, "title": "A", "instruction": "Do A", "target_phrase": "v", "keyframe_index": 0}]}
    monkeypatch.setattr(procedures, "build_procedure", slow_build)
    from backend.main import app
    with TestClient(app) as one_loop:             # one event loop for both sockets, like the real server
        with one_loop.websocket_connect("/ws/capture") as ws:
            ws.send_json({"type": "start"})
            cid = _until(ws, "capture")["capture_id"]
            ws.send_json({"type": "frame", "data": FRAME})
            _until(ws, "frame_ack")
            ws.send_json({"type": "stop"})
        # The phone lost signal right after tapping stop; the build goes on.
        with one_loop.websocket_connect("/ws/capture") as ws:
            ws.send_json({"type": "rejoin", "capture_id": cid})
            assert _until(ws, "rejoined")["state"] in ("building", "done")
            assert _until(ws, "procedure")["procedure"]["title"] == "Job"
            ws.send_json({"type": "close"})
    assert cid not in live._TAKES                 # delivered and closed: freed


def test_rejoin_to_an_unknown_take_says_so(cap):
    client, *_ = cap
    with client.websocket_connect("/ws/capture") as ws:
        ws.send_json({"type": "rejoin", "capture_id": "nope"})
        assert _until(ws, "rejoin_failed")


def test_ping_gets_a_pong_on_both_sockets(cap):
    client, *_ = cap
    for path in ("/ws/capture", "/ws/guide"):
        with client.websocket_connect(path) as ws:
            ws.send_json({"type": "ping"})
            assert _until(ws, "pong")


def test_guide_resumes_after_a_skip_with_the_attempt_count(cap, monkeypatch):
    client, built, db = cap
    from backend import config, guide
    monkeypatch.setattr(config, "AUTO_ADVANCE", False)
    monkeypatch.setattr(guide, "ground_scene", lambda step, frame: {"target": [], "objects": [], "hands": []})
    monkeypatch.setattr(guide, "left_behind", lambda proc, frame: None)
    monkeypatch.setattr(guide, "judge_step", lambda *a, **k: {
        "tool": "flag_error", "say": "No.", "verdict": "fail", "latency_ms": 1, "grounding": "", "detections": [],
        "backend": "hosted", "detail": {"severity": "minor", "what_is_wrong": "w", "how_to_fix": "f", "say": "No."}})
    pid = db.save_procedure("P", steps=[{"index": i, "title": f"S{i}", "instruction": f"do {i}",
                                         "target_phrase": "x", "success_criteria": "y"} for i in range(3)])
    with client.websocket_connect("/ws/guide") as ws:
        ws.send_json({"type": "start", "procedure_id": pid})
        sid = _until(ws, "session")["session_id"]
        _until(ws, "speak")
        ws.send_json({"type": "skip"})                       # skip step 1
        assert _until(ws, "speak")["step"] == 2
        ws.send_json({"type": "check", "data": FRAME})       # fail step 2 once
        _until(ws, "verdict")
    with client.websocket_connect("/ws/guide") as ws:
        ws.send_json({"type": "resume", "session_id": sid})
        assert _until(ws, "session")["resumed_at"] == 2      # not back to the skipped step 1
        ws.send_json({"type": "check", "data": FRAME})
        _until(ws, "verdict")
        ws.send_json({"type": "abort"})
    events = db.session_log(sid)["events"]
    assert [e["attempt"] for e in events if e["verdict"] == "fail"] == [1, 2]


def test_resuming_a_finished_job_just_reports_it(cap):
    client, built, db = cap
    pid = db.save_procedure("P", steps=[{"index": 0, "title": "S", "instruction": "do", "target_phrase": "x"}])
    sid = db.start_session(pid, None)
    db.finish_session(sid, "completed")
    with client.websocket_connect("/ws/guide") as ws:
        ws.send_json({"type": "resume", "session_id": sid})
        assert _until(ws, "complete")["session_id"] == sid
