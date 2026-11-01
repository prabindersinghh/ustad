"""Tests for the live guide session, voice intents and capture helpers.

The models are mocked: guide.ground and guide.judge_step are replaced so the
watcher, the background judge and auto-advance can be driven deterministically.
"""
from __future__ import annotations

import base64
import struct

import pytest
from fastapi.testclient import TestClient

from backend import live, procedures, sam_parse
from backend.realtime import intent_of

FRAME = "data:image/jpeg;base64," + base64.b64encode(b"\xff\xd8fakejpeg").decode()


# ---------------------------------------------------------------- intents

@pytest.mark.parametrize("text,intent", [
    ("Done.", "done"), ("ok done", "done"), ("I'm done", "done"), ("Next", "done"),
    ("finished", "done"), ("repeat", "repeat"), ("say that again", "repeat"),
    ("skip", "skip"), ("what's wrong?", "why"), ("why", "why"),
    ("where is it", "where"), ("show me", "where"),
])
def test_intents(text, intent):
    assert intent_of(text) == intent


@pytest.mark.parametrize("text", [
    "I'm not done yet", "not done",
    "now take the red valve handle and turn it until it is done properly",  # narration, too long
    "the bottle is on the table",
])
def test_non_commands_are_ignored(text):
    assert intent_of(text) is None


# ---------------------------------------------------------------- capture helpers

def test_select_keyframes_spans_whole_recording():
    keep = procedures.select_keyframes(60, 12)
    assert len(keep) == 12
    assert keep[0] == 0 and keep[-1] == 59          # the end of the job is kept
    assert keep == sorted(set(keep))
    assert procedures.select_keyframes(5, 12) == [0, 1, 2, 3, 4]


def test_pcm_to_wav_header():
    pcm = b"\x00\x01" * 16000                     # 1 s of 16 kHz s16
    wav = live.pcm_to_wav(pcm)
    assert wav[:4] == b"RIFF" and wav[8:12] == b"WAVE"
    channels, rate = struct.unpack("<HI", wav[22:28])
    assert (channels, rate) == (1, 16000)
    assert wav[44:] == pcm


# ---------------------------------------------------------------- live guide

def _det():
    return sam_parse.Detection(frame=0, ordinal=0, x1=10, y1=10, x2=60, y2=60,
                               img_w=100, img_h=100, confidence=0.9)


def _action(tool, conf=0.95):
    detail = {"confidence": conf, "reason": "looks right", "say": "Good."} if tool == "verify_step" \
        else {"severity": "minor", "what_is_wrong": "valve open", "how_to_fix": "close it", "say": "Close it."}
    return {"tool": tool, "say": detail["say"], "detail": detail,
            "verdict": "pass" if tool == "verify_step" else "fail",
            "latency_ms": 5, "grounding": "Grounding: ok", "detections": [], "backend": "hosted"}


@pytest.fixture
def ctx(tmp_path, monkeypatch):
    from backend import config, db, guide
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "t.db")
    monkeypatch.setattr(config, "MEDIA_DIR", tmp_path)
    monkeypatch.setattr(config, "VOICE", False)
    monkeypatch.setattr(config, "WATCH_GROUND_INTERVAL_S", 0.0)
    monkeypatch.setattr(config, "WATCH_JUDGE_INTERVAL_S", 0.0)
    monkeypatch.setattr(config, "WATCH_JUDGE_AFTER_S", 0.0)
    db.init()
    pid = db.save_procedure("P", machine=None, steps=[
        {"index": i, "title": f"S{i}", "instruction": f"do {i}", "target_phrase": "red valve",
         "success_criteria": "closed", "keyframe_index": 0, "common_mistake": None, "hazard": None}
        for i in range(2)])
    calls = {"ground": 0, "judge": []}

    def fake_ground(step, frame):
        calls["ground"] += 1
        return [_det()]

    plan = {"tool": "verify_step"}

    def fake_judge(step, frame, *, worker_said=None, attempt=1, detections=None, **_):
        calls["judge"].append({"said": worker_said, "pre_grounded": detections is not None})
        return dict(_action(plan["tool"]))

    monkeypatch.setattr(guide, "ground", fake_ground)
    monkeypatch.setattr(guide, "judge_step", fake_judge)
    from backend.main import app
    return TestClient(app), pid, calls, plan, db


def _until(ws, kind, limit=60):
    for _ in range(limit):
        msg = ws.receive_json()
        if msg["type"] == kind:
            return msg
    raise AssertionError(f"no {kind}")


def test_watch_frame_produces_live_track(ctx):
    client, pid, calls, plan, db = ctx
    plan["tool"] = "request_better_view"          # keep the background judge quiet
    with client.websocket_connect("/ws/guide") as ws:
        ws.send_json({"type": "start", "procedure_id": pid})
        _until(ws, "speak")
        ws.send_json({"type": "watch", "data": FRAME})
        track = _until(ws, "track")
        assert track["target"] == "red valve"
        assert track["detections"][0]["x1"] == 10
        ws.send_json({"type": "abort"})


def test_auto_advance_needs_two_confident_passes(ctx):
    client, pid, calls, plan, db = ctx
    with client.websocket_connect("/ws/guide") as ws:
        ws.send_json({"type": "start", "procedure_id": pid})
        _until(ws, "speak")
        ws.send_json({"type": "watch", "data": FRAME})
        assert _until(ws, "auto")["state"] == "looks_done"      # first pass: provisional
        ws.send_json({"type": "watch", "data": FRAME})
        verdict = _until(ws, "verdict")                        # second pass: advances
        assert verdict["source"] == "auto" and verdict["advance"] is True
        nxt = _until(ws, "speak")
        nxt = nxt if nxt.get("step") == 2 else _until(ws, "speak")
        assert nxt["step"] == 2
        ws.send_json({"type": "abort"})
    events = db.session_log(db.list_sessions()[0]["id"])["events"]
    assert events[0]["verdict"] == "pass" and events[0]["reason"].startswith("[auto]")


def test_done_reuses_live_grounding(ctx):
    client, pid, calls, plan, db = ctx
    monkey_auto = plan
    monkey_auto["tool"] = "flag_error"            # background stays silent on minor errors
    with client.websocket_connect("/ws/guide") as ws:
        ws.send_json({"type": "start", "procedure_id": pid})
        _until(ws, "speak")
        ws.send_json({"type": "watch", "data": FRAME})
        _until(ws, "track")
        ws.send_json({"type": "check", "said": "done"})
        verdict = _until(ws, "verdict")
        assert verdict["source"] == "tap" and verdict["verdict"] == "fail"
        ws.send_json({"type": "abort"})
    explicit = [c for c in calls["judge"] if c["said"] == "done"]
    assert explicit and explicit[0]["pre_grounded"] is True   # no SAM wait on the hot path


# ---------------------------------------------------------------- spend meter

def test_token_cost_counts_reasoning_and_cache(tmp_path, monkeypatch):
    from types import SimpleNamespace as NS
    from backend import config, db, meta_client
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "u.db")
    db.init()
    usage = NS(input_tokens=1_000_000, output_tokens=1_000_000,
               input_tokens_details=NS(cached_tokens=200_000))
    meta_client._meter_tokens("muse-spark-1.3", usage)
    # 800k fresh in, 200k cached in, 1M out (incl. hidden reasoning)
    expected = 0.8 * 1.25 + 0.2 * 0.15 + 1.0 * 4.25
    assert abs(db.usage_total() - expected) < 1e-9


def test_usage_summary_against_budget(tmp_path, monkeypatch):
    from backend import config, db
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "u.db")
    monkeypatch.setattr(config, "BUDGET_USD", 50.0)
    db.init()
    for _ in range(400):
        db.log_usage("sam-3.1", "image", 1, config.PRICE_SAM_IMAGE)   # 400 frames = $1.00
    s = db.usage_summary()
    assert s["total_usd"] == 1.0 and s["remaining_usd"] == 49.0
    assert s["by_model"][0]["calls"] == 400


def test_still_view_keeps_grounding_fresh_without_sam(ctx):
    client, pid, calls, plan, db = ctx
    plan["tool"] = "request_better_view"
    with client.websocket_connect("/ws/guide") as ws:
        ws.send_json({"type": "start", "procedure_id": pid})
        _until(ws, "speak")
        ws.send_json({"type": "watch", "data": FRAME})
        _until(ws, "track")
        grounded = calls["ground"]
        for _ in range(5):
            ws.send_json({"type": "still"})
        ws.send_json({"type": "check", "said": "done"})
        _until(ws, "verdict")
        ws.send_json({"type": "abort"})
    assert calls["ground"] == grounded            # no extra SAM calls for a still view


# ---------------------------------------------------------------- smarter guide

def _verify(**extra):
    a = _action("verify_step")
    a["detail"] = {**a["detail"], **extra}
    return a


def test_jump_ahead_credits_steps_in_between(ctx, monkeypatch):
    client, pid, calls, plan, db = ctx
    from backend import guide
    monkeypatch.setattr(guide, "judge_step", lambda *a, **k: _verify(completed_through=2))
    with client.websocket_connect("/ws/guide") as ws:
        ws.send_json({"type": "start", "procedure_id": pid})
        _until(ws, "speak")
        ws.send_json({"type": "check", "data": FRAME, "said": "done"})
        _until(ws, "complete")
    sid = db.list_sessions()[0]["id"]
    ev = {e["step_index"]: e["verdict"] for e in db.session_log(sid)["events"]}
    assert ev == {0: "pass", 1: "inferred"}
    assert db.session_log(sid)["session"]["outcome"] == "completed"


def test_skipped_step_satisfied_in_final_state_counts(ctx, monkeypatch):
    client, pid, calls, plan, db = ctx
    from backend import guide
    monkeypatch.setattr(guide, "judge_step", lambda *a, **k: _verify())
    monkeypatch.setattr(guide, "audit_skipped",
                        lambda steps, frame: {0: {"status": "satisfied", "why": "valve visibly closed"}})
    with client.websocket_connect("/ws/guide") as ws:
        ws.send_json({"type": "start", "procedure_id": pid})
        _until(ws, "speak")
        ws.send_json({"type": "watch", "data": FRAME})
        ws.send_json({"type": "skip"})                       # skip step 1
        ws.send_json({"type": "check", "said": "done"})       # pass step 2 (the last)
        _until(ws, "complete")
    sid = db.list_sessions()[0]["id"]
    log = db.session_log(sid)
    assert log["session"]["outcome"] == "completed"
    assert any(e["step_index"] == 0 and e["verdict"] == "inferred" and "final audit" in e["reason"]
               for e in log["events"])


def test_skipped_step_not_visible_stays_flagged(ctx, monkeypatch):
    client, pid, calls, plan, db = ctx
    from backend import guide
    monkeypatch.setattr(guide, "judge_step", lambda *a, **k: _verify())
    monkeypatch.setattr(guide, "audit_skipped",
                        lambda steps, frame: {0: {"status": "not_visible", "why": "lockout not visible"}})
    with client.websocket_connect("/ws/guide") as ws:
        ws.send_json({"type": "start", "procedure_id": pid})
        _until(ws, "speak")
        ws.send_json({"type": "watch", "data": FRAME})
        ws.send_json({"type": "skip"})
        ws.send_json({"type": "check", "said": "done"})
        _until(ws, "complete")
    assert db.session_log(db.list_sessions()[0]["id"])["session"]["outcome"] == "completed_with_skips"


def test_out_of_range_reading_becomes_a_fail(monkeypatch):
    import json
    from types import SimpleNamespace as NS
    from backend import guide, meta_client
    step = {"index": 0, "title": "Check pressure", "instruction": "Read the gauge", "target_phrase": "pressure gauge",
            "success_criteria": "needle in green band",
            "reading": {"what": "pressure", "unit": "bar", "min": 6, "max": 8}}
    resp = NS(output=[meta_client._FunctionCall("verify_step", json.dumps(
        {"confidence": 0.9, "reason": "needle visible", "say": "Good.", "reading": "9.5 bar"}))],
        output_text="", backend="hosted")
    monkeypatch.setattr(meta_client, "reason", lambda *a, **k: resp)
    out = guide.judge_step(step, b"x", detections=[])
    assert out["verdict"] == "fail" and "9.5" in out["say"]
    assert guide.reading_spec(step) == "pressure between 6 and 8 bar"


def test_timed_step_waits_without_spending_a_model_call(tmp_path, monkeypatch):
    from backend import config, db, guide
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "t.db")
    monkeypatch.setattr(config, "MEDIA_DIR", tmp_path)
    monkeypatch.setattr(config, "VOICE", False)
    monkeypatch.setattr(config, "AUTO_ADVANCE", False)
    db.init()
    pid = db.save_procedure("Mix", machine=None, steps=[
        {"index": 0, "title": "Run mixer", "instruction": "Run the mixer", "target_phrase": "mixer",
         "success_criteria": "", "check": "timed", "duration_s": 120}])
    judged = []
    monkeypatch.setattr(guide, "judge_step", lambda *a, **k: judged.append(1) or _verify())
    from backend.main import app
    with TestClient(app).websocket_connect("/ws/guide") as ws:
        ws.send_json({"type": "start", "procedure_id": pid})
        _until(ws, "speak")
        ws.send_json({"type": "check", "data": FRAME, "said": "done"})
        assert _until(ws, "verdict")["verdict"] == "wait"    # 120 s timer not up
        ws.send_json({"type": "abort"})
    assert not judged


def test_confirm_step_advances_on_voice_word(tmp_path, monkeypatch):
    from backend import config, db, guide
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "t.db")
    monkeypatch.setattr(config, "MEDIA_DIR", tmp_path)
    monkeypatch.setattr(config, "VOICE", False)
    db.init()
    pid = db.save_procedure("Click", machine=None, steps=[
        {"index": 0, "title": "Listen", "instruction": "Listen for the click", "target_phrase": "",
         "success_criteria": "", "check": "confirm"}])

    def no_vision(*a, **k):
        raise AssertionError("a confirm step must not call the judge")
    monkeypatch.setattr(guide, "judge_step", no_vision)
    from backend.main import app
    with TestClient(app).websocket_connect("/ws/guide") as ws:
        ws.send_json({"type": "start", "procedure_id": pid})
        _until(ws, "speak")
        ws.send_json({"type": "check", "data": FRAME, "said": "heard the click"})
        assert _until(ws, "verdict")["verdict"] == "confirmed"
        _until(ws, "complete")


def test_normalise_keeps_reading_and_timer_fields():
    out = procedures._normalise({"title": "T", "steps": [
        {"instruction": "Check the gauge", "target_phrase": "gauge", "check": "reading",
         "reading": {"what": "pressure", "unit": "bar", "min": "8", "max": 6}},
        {"instruction": "Mix", "target_phrase": "mixer", "check": "timed", "duration_s": 180},
        {"instruction": "Weird", "target_phrase": "x", "check": "reading", "reading": None},
    ]}, n_frames=2)
    s = out["steps"]
    assert s[0]["check"] == "reading" and s[0]["reading"]["min"] == 6 and s[0]["reading"]["max"] == 8
    assert s[1]["check"] == "timed" and s[1]["duration_s"] == 180
    assert s[2]["check"] == "visual"                          # a reading without a range falls back
    assert [x["index"] for x in s] == [0, 1, 2]


def test_capture_pause_ignores_frames_and_marks_steps(tmp_path, monkeypatch):
    from backend import config, db
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "c.db")
    monkeypatch.setattr(config, "VOICE", False)
    db.init()
    from backend.main import app
    with TestClient(app).websocket_connect("/ws/capture") as ws:
        ws.send_json({"type": "start"})
        _until(ws, "status")
        ws.send_json({"type": "pause"})
        assert _until(ws, "paused")
        ws.send_json({"type": "frame", "data": FRAME})        # ignored while paused
        ws.send_json({"type": "resume"})
        assert _until(ws, "resumed")
        ws.send_json({"type": "frame", "data": FRAME})
        assert _until(ws, "frame_ack")["count"] == 1
        ws.send_json({"type": "mark"})
        assert _until(ws, "marker")["n"] == 2
