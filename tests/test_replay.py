"""scripts/replay.py: the logic around the real pipeline, with the model calls
stubbed. A temp database keeps the real one untouched. No network."""
from __future__ import annotations

import io
import shutil
import subprocess
from types import SimpleNamespace

import pytest

from backend import config, db, guide, meta_client, sam_parse
from scripts import replay

STEPS = [
    {"index": 0, "title": "One", "instruction": "do one", "target_phrase": "red pad", "success_criteria": "seen"},
    {"index": 1, "title": "Two", "instruction": "do two", "target_phrase": "blue part", "success_criteria": "seen"},
]

needs_ffmpeg = pytest.mark.skipif(not (shutil.which("ffmpeg") and shutil.which("ffprobe")), reason="ffmpeg not on PATH")


@pytest.fixture
def clip(tmp_path):
    path = tmp_path / "clip.mp4"
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i",
                    "testsrc=size=48x64:rate=10:duration=2", "-pix_fmt", "yuv420p", str(path)], check=True)
    return path


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "r.db")
    monkeypatch.setattr(config, "MEDIA_DIR", tmp_path)
    monkeypatch.setattr(config, "REASONING_BACKEND", "hosted")
    db.init()
    return db.save_procedure("Proc", STEPS)


def _det(**kw):
    base = dict(frame=0, ordinal=0, x1=20, y1=40, x2=60, y2=80, img_w=96, img_h=128, confidence=0.9)
    return sam_parse.Detection(**{**base, **kw})


def _stub_models(monkeypatch, *tools, segment=None):
    calls = iter(tools)

    def reason(*_a, **_k):
        name = next(calls)
        args = {"verify_step": {"confidence": 0.8, "reason": "part in view", "say": "ok"},
                "request_better_view": {"what_to_show": "the part", "say": "closer"},
                "flag_error": {"severity": "major", "what_is_wrong": "hand on it", "how_to_fix": "move", "say": "stop"}}[name]
        import json
        return SimpleNamespace(backend="hosted", output_text="",
                               output=[SimpleNamespace(type="function_call", name=name, arguments=json.dumps(args))])

    monkeypatch.setattr(meta_client, "reason", reason)
    monkeypatch.setattr(meta_client, "segment", segment or (lambda *_a, **_k: [_det()]))


def test_checkpoints_must_increase():
    assert replay.parse_checkpoints("6, 14,22.5") == [6.0, 14.0, 22.5]
    for bad in ("", "a,b", "6,6", "14,6", "-1,2"):
        with pytest.raises(replay.ReplayError):
            replay.parse_checkpoints(bad)


def test_upscaled_box_maps_back_to_source_pixels():
    d = _det().to_dict()                   # measured on a 96x128 frame, source is 48x64
    assert replay.to_source_box(d, 48, 64) == (10, 20, 30, 40)
    same = _det(img_w=48, img_h=64).to_dict()
    assert replay.to_source_box(same, 48, 64) == (20, 40, 60, 80)


def test_table_stays_one_row_per_check_and_reports_sam_errors():
    base = {"checkpoint_s": 6.0, "step_index": 0, "title": "One", "verdict": "fail", "confidence": None,
            "phrase": "red pad", "latency_ms": 1500, "backend": "hosted", "video_w": 48, "video_h": 64,
            "reason": "a | b\nc", "detections": [_det().to_dict()]}
    text = replay.results_table([base, {**base, "detections": [], "sam_error": True},
                                 {**base, "detections": []}])
    rows = text.splitlines()
    assert len(rows) == 5
    assert "a \\| b c" in rows[2] and "1.5 s" in rows[2] and "(10,20)-(30,40)" in rows[2]
    assert "SAM ERROR" in rows[3]
    assert "not found" in rows[4]


@needs_ffmpeg
def test_decode_native_and_upscaled(clip):
    pil = pytest.importorskip("PIL.Image")
    assert replay.probe_size(clip) == (48, 64)
    assert pil.open(io.BytesIO(replay.decode_frame(clip, 1))).size == (48, 64)
    assert pil.open(io.BytesIO(replay.decode_frame(clip, 1, 2))).size == (96, 128)
    with pytest.raises(replay.ReplayError):
        replay.decode_frame(clip, 99)


@needs_ffmpeg
def test_replay_writes_a_real_session_with_boxes_even_on_retry(clip, store, monkeypatch):
    _stub_models(monkeypatch, "verify_step", "request_better_view")
    run = replay.run_replay(clip, store, [0.5, 1.5], upscale=2)

    log = db.session_log(run["session_id"])
    assert log["session"]["worker_name"] == "replay: clip.mp4 (2x)"
    assert log["session"]["mode"] == "guide"
    assert log["session"]["finished_at"] is not None
    assert log["session"]["outcome"] == "replay: 1 of 2 verified"
    assert [e["verdict"] for e in log["events"]] == ["pass", "retry"]
    assert log["events"][1]["detections"] == []       # step_events keeps no boxes on a retry...

    points = db.replay_checkpoints(run["session_id"])
    assert [p["checkpoint_s"] for p in points] == [0.5, 1.5]
    assert points[1]["detections"][0]["x1"] == 20     # ...the sidecar does
    assert points[1]["verdict"] == "retry" and points[1]["tool"] == "request_better_view"
    assert points[0]["reason"] == "part in view" and points[0]["backend"] == "hosted"
    assert (points[0]["video_w"], points[0]["frame_w"]) == (48, 96)
    assert db.metrics()["checks"] == 2                # counted like any other run


@needs_ffmpeg
def test_a_failed_sam_call_is_not_reported_as_a_miss(clip, store, monkeypatch):
    def boom(*_a, **_k):
        raise RuntimeError("network down")

    _stub_models(monkeypatch, "request_better_view", segment=boom)
    run = replay.run_replay(clip, store, [0.5])
    assert run["rows"][0]["sam_error"] is True
    assert "SAM ERROR" in replay.results_table(run["rows"])


@needs_ffmpeg
def test_refuses_to_send_footage_to_an_open_weights_endpoint(clip, store, monkeypatch):
    monkeypatch.setattr(meta_client, "active_backend", lambda: "open")
    with pytest.raises(replay.ReplayError):
        replay.run_replay(clip, store, [0.5])
    assert db.list_sessions() == []


@needs_ffmpeg
def test_a_bad_timestamp_leaves_no_session(clip, store, monkeypatch):
    _stub_models(monkeypatch, "verify_step", "verify_step")
    with pytest.raises(replay.ReplayError):
        replay.run_replay(clip, store, [0.5, 99])
    assert db.list_sessions() == []


@needs_ffmpeg
def test_a_model_error_still_finishes_the_session_honestly(clip, store, monkeypatch):
    _stub_models(monkeypatch, "verify_step")          # the second check runs out of answers
    with pytest.raises((StopIteration, RuntimeError)):
        replay.run_replay(clip, store, [0.5, 1.5])
    assert db.list_sessions()[0]["outcome"] == "replay: stopped by an error"


def test_spend_counts_only_this_process_and_still_logs(store):
    with replay.spend_of_this_process() as spent:
        db.log_usage("m", "image", 1, 0.25)
    assert spent == {"usd": 0.25, "calls": 1}
    assert db.usage_total() == 0.25
    assert guide.db.log_usage is db.log_usage
