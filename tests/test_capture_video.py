"""scripts/capture_video.py: where a capture's narration comes from. Model calls
are stubbed and a temp database keeps the real one untouched. No network."""
from __future__ import annotations

import shutil
import subprocess

import pytest

from backend import config, db, meta_client, procedures
from scripts import capture_video
from scripts.replay import ReplayError

needs_ffmpeg = pytest.mark.skipif(not (shutil.which("ffmpeg") and shutil.which("ffprobe")), reason="ffmpeg not on PATH")


@pytest.fixture
def clip(tmp_path):
    path = tmp_path / "clip.mp4"
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i",
                    "testsrc=size=48x64:rate=10:duration=5", "-pix_fmt", "yuv420p", str(path)], check=True)
    return path


@pytest.fixture
def seen(tmp_path, monkeypatch):
    """Stubs the writer and ASR, and records what each was given."""
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "c.db")
    monkeypatch.setattr(config, "MEDIA_DIR", tmp_path)
    monkeypatch.setattr(meta_client, "active_backend", lambda: "hosted")
    calls = {"asr": [], "build": []}

    def transcribe(audio, *, filename="audio.m4a", **_k):
        calls["asr"].append(filename)
        return "words from ASR"

    def build(transcript, frames, *, hint=None, frame_times=None, validate=None):
        calls["build"].append({"transcript": transcript, "frames": len(frames), "times": frame_times, "hint": hint})
        return {"title": "Job", "machine": None, "_frames": list(frames), "steps": [
            {"index": 0, "title": "One", "target_phrase": "part", "keyframe_index": 0}]}

    monkeypatch.setattr(meta_client, "transcribe", transcribe)
    monkeypatch.setattr(procedures, "build_procedure", build)
    return calls


@needs_ffmpeg
def test_a_txt_narration_is_used_as_written_with_no_asr(clip, seen, tmp_path):
    note = tmp_path / "n.txt"
    note.write_text("[0:01] Load the part.\n", encoding="utf-8")
    out = capture_video.capture(clip, end=4, narration_file=note)
    assert seen["asr"] == []
    assert seen["build"][0]["transcript"] == "[0:01] Load the part."
    assert seen["build"][0]["times"] == [0, 2, 4]
    saved = db.get_procedure(out["procedure_id"])
    assert saved["source_transcript"] == "[0:01] Load the part."
    assert saved["steps"][0]["ref_image"].startswith("media/ref_file_")   # a real reference photo


@needs_ffmpeg
def test_an_audio_narration_is_transcribed_instead_of_the_video(clip, seen, tmp_path):
    voice = tmp_path / "voice.m4a"
    voice.write_bytes(b"not read by the stub")
    capture_video.capture(clip, end=2, narration_file=voice)
    assert seen["asr"] == ["voice.m4a"]
    assert seen["build"][0]["transcript"] == "words from ASR"


@needs_ffmpeg
def test_without_the_option_the_videos_own_audio_is_transcribed(clip, seen):
    capture_video.capture(clip, end=2)
    assert seen["asr"] == ["clip.mp4"]


@needs_ffmpeg
def test_frame_times_are_relative_to_the_start_of_the_take(clip, seen, tmp_path):
    note = tmp_path / "n.txt"
    note.write_text("words", encoding="utf-8")
    capture_video.capture(clip, start=1, end=3, narration_file=note)
    assert seen["build"][0]["times"] == [0, 2]


def test_a_bad_narration_file_is_refused(tmp_path, seen):
    with pytest.raises(ReplayError):
        capture_video.read_narration(tmp_path / "missing.txt")
    empty = tmp_path / "e.txt"
    empty.write_text("  \n", encoding="utf-8")
    with pytest.raises(ReplayError):
        capture_video.read_narration(empty)
    wrong = tmp_path / "n.pdf"
    wrong.write_bytes(b"x")
    with pytest.raises(ReplayError):
        capture_video.read_narration(wrong)
    assert seen["asr"] == []


def test_sampling_includes_the_start_and_stays_inside_the_range():
    assert capture_video.sample_times(0, 5, 2) == [0, 2, 4]
    with pytest.raises(ReplayError):
        capture_video.sample_times(5, 5, 2)


@needs_ffmpeg
def test_refuses_an_open_weights_backend(clip, seen, monkeypatch):
    monkeypatch.setattr(meta_client, "active_backend", lambda: "open")
    with pytest.raises(ReplayError):
        capture_video.capture(clip)
    assert seen["build"] == []
