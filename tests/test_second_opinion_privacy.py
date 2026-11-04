"""What leaves the system on a second opinion, and what a seed leaves behind."""
from __future__ import annotations

import importlib.util
import sqlite3
import sys
import types
from pathlib import Path

from backend import config, guide, meta_client

PASS = '{"confidence":0.9,"reason":"ok","say":"ok"}'
STEP = {"index": 0, "title": "x", "instruction": "x", "target_phrase": "lid", "ref_image": "media/ref.jpg"}


def _images_sent(monkeypatch, **kw) -> list[dict]:
    seen = {}

    def fake_reason(system, content, **_):
        seen["content"] = content
        call = type("Call", (), {"type": "function_call", "name": "verify_step", "arguments": PASS})()
        return type("R", (), {"output": [call], "output_text": "", "backend": "open"})()

    monkeypatch.setattr(config, "JUDGE_REFERENCE", True)
    monkeypatch.setattr(guide, "reference_photo", lambda step: b"\xff\xd8expert")
    monkeypatch.setattr(meta_client, "reason", fake_reason)
    guide.judge_step(STEP, b"\xff\xd8job", detections=[], **kw)
    return [p for p in seen["content"] if p.get("type") != "input_text"]


def test_second_opinion_sends_only_the_job_photo(monkeypatch):
    assert len(_images_sent(monkeypatch, open_weights=True)) == 1


def test_live_judging_still_uses_the_reference_photo(monkeypatch):
    assert len(_images_sent(monkeypatch)) == 2


def _modal_setup():
    # The real script imports modal, which only the deploy machine needs.
    sys.modules.setdefault("modal", types.ModuleType("modal"))
    path = Path(__file__).resolve().parent.parent / "deploy" / "modal_setup.py"
    spec = importlib.util.spec_from_file_location("modal_setup_for_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_seed_finds_every_table_keyed_by_session():
    conn = sqlite3.connect(":memory:")
    conn.executescript(
        "CREATE TABLE sessions (id TEXT);"
        "CREATE TABLE step_events (session_id TEXT);"
        "CREATE TABLE second_opinions (session_id TEXT);"
        "CREATE TABLE replay_checkpoints (session_id TEXT);"
        "CREATE TABLE procedures (id TEXT);"
    )
    assert set(_modal_setup().db_session_tables(conn)) == {"step_events", "second_opinions", "replay_checkpoints"}
