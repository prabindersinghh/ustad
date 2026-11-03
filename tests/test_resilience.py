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
    monkeypatch.setattr(config, "TAKES_DIR", tmp_path / "takes")
    monkeypatch.setattr(config, "VALIDATE_PROCEDURE", False)
    db.init()
    live._TAKES.clear()
    live._take_gates.clear()
    built = {}

    def fake_build(transcript, keyframes, *, hint=None, frame_times=None, **_):
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


def test_offline_audio_and_takes_are_bounded(cap, monkeypatch):
    client, built, db = cap
    monkeypatch.setattr(live, "MAX_GAP_BYTES", 64000)        # 2 s, to keep the test small
    with client.websocket_connect("/ws/capture") as ws:
        ws.send_json({"type": "start"})
        cid = _until(ws, "capture")["capture_id"]
        big = base64.b64encode(b"\x00" * 50000).decode()
        for at in (1.0, 2.0, 3.0):
            ws.send_json({"type": "audio_gap", "at": at, "data": big})
        ws.send_json({"type": "ping"})
        _until(ws, "pong")
        # The first fits; the rest exceed the room left and are refused
        # before being decoded.
        assert sum(len(p) for _, p in live._TAKES[cid].gaps) == 50000
    # The take above is detached but kept, waiting for its phone. With room
    # for one take, someone else is refused: never evicted for another client.
    # (Pretend the test client is the local tunnel, so the header counts.)
    monkeypatch.setattr(live, "MAX_TAKES", 1)
    monkeypatch.setattr(live, "_client_of", lambda ws: ws.headers.get("cf-connecting-ip") or "testclient")
    with client.websocket_connect("/ws/capture", headers={"cf-connecting-ip": "203.0.113.9"}) as ws:
        ws.send_json({"type": "start"})
        assert "busy" in _until(ws, "error")["message"]
    assert list(live._TAKES) == [cid]
    # The same client may replace its own abandoned take.
    with client.websocket_connect("/ws/capture") as ws:
        ws.send_json({"type": "start"})
        new = _until(ws, "capture")["capture_id"]
        assert list(live._TAKES) == [new]


def test_client_identity_trusts_the_tunnel_header_only_from_loopback():
    class WS:
        def __init__(self, host, header=None):
            self.client = type("C", (), {"host": host})()
            self.headers = {"cf-connecting-ip": header} if header else {}
    assert live._client_of(WS("127.0.0.1", "198.51.100.7")) == "198.51.100.7"   # via cloudflared
    assert live._client_of(WS("192.168.1.20", "1.2.3.4")) == "192.168.1.20"     # LAN: header ignored
    assert live._client_of(WS("::1")) == "::1"


def test_near_empty_abandoned_takes_free_their_slot_quickly(cap, monkeypatch):
    client, built, db = cap
    with client.websocket_connect("/ws/capture") as ws:
        ws.send_json({"type": "start"})
        cid = _until(ws, "capture")["capture_id"]
        ws.send_json({"type": "frame", "data": FRAME})        # one frame: kept on drop, but near-empty
        _until(ws, "frame_ack")
    live._TAKES[cid].detached_at -= live.TAKE_TTL_EMPTY_S + 1
    assert live._admit_take("someone-else") and cid not in live._TAKES


def test_take_creation_is_rate_limited_per_client(cap):
    assert all(live._admit_take("burst") for _ in range(6))
    assert not live._admit_take("burst")
    assert live._admit_take("another")


def test_a_long_take_is_written_in_parts_while_recording_then_merged(cap, monkeypatch):
    client, built, db = cap
    from backend import config
    monkeypatch.setattr(config, "SEGMENT_S", 0.0)             # every frame closes a part, for the test
    drafted, merged = [], {}

    def draft(transcript, frames, times, *, k, previous, hint=None):
        drafted.append((k, len(frames), list(previous)))
        return {"title": "Job", "machine": None, "steps": [
            {"index": 0, "title": f"Part {k}", "instruction": f"Do part {k}", "target_phrase": "valve",
             "keyframe_index": 0}]}

    def merge(drafts, n_frames, hint=None):
        merged["frames"] = [s["frame"] for d in drafts for s in d["steps"]]
        merged["n"] = n_frames
        return procedures._normalise({"title": "Job", "steps": [
            {"title": s["title"], "instruction": s["instruction"], "target_phrase": "valve",
             "keyframe_index": s["frame"]} for d in drafts for s in d["steps"]]}, n_frames=n_frames)

    monkeypatch.setattr(procedures, "draft_segment", draft)
    monkeypatch.setattr(procedures, "merge_drafts", merge)
    monkeypatch.setattr(procedures, "build_procedure",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("one big call is not used")))
    from backend.main import app
    with TestClient(app) as one_loop:
        with one_loop.websocket_connect("/ws/capture") as ws:
            ws.send_json({"type": "start"})
            cid = _until(ws, "capture")["capture_id"]
            for _ in range(3):
                ws.send_json({"type": "frame", "data": FRAME})
                _until(ws, "frame_ack")
            folder = config.TAKES_DIR / cid
            assert len(list(folder.glob("*.jpg"))) == 3            # frames on disk, not in memory
            ws.send_json({"type": "stop"})
            proc = _until(ws, "procedure")["procedure"]
    assert [d[0] for d in drafted] == [1, 2, 3]                     # one part per frame here
    assert drafted[2][2] == ["Part 1", "Part 2"]                    # later parts know the earlier steps
    assert merged == {"frames": [0, 1, 2], "n": 3}                  # local keyframes mapped to the take
    assert [s["title"] for s in proc["steps"]] == ["Part 1", "Part 2", "Part 3"]
    assert all(s["ref_image"].startswith("media/ref_") for s in proc["steps"])
    assert not folder.exists()                                      # frames freed once saved


def test_frame_store_reads_back_what_it_wrote(tmp_path):
    fs = live.FrameStore(tmp_path / "t")
    for b in (b"a", b"b", b"c"):
        fs.append(b)
    assert len(fs) == 3 and fs[0] == b"a" and fs[-1] == b"c" and list(fs) == [b"a", b"b", b"c"]
    with pytest.raises(IndexError):
        fs[3]
    fs.remove()
    assert not (tmp_path / "t").exists()


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
