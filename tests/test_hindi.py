"""Hindi: what the worker hears changes; what gets checked does not.

Models are mocked. The safety properties matter most: a translation can't change
a number or lose a warning, SAM keeps its English phrases, and English sessions
send nothing new to the model code.
"""
from __future__ import annotations

import base64

import pytest
from fastapi.testclient import TestClient

from backend import guide, i18n, meta_client, translate
from backend.realtime import intent_of, is_howto, is_question

FRAME = "data:image/jpeg;base64," + base64.b64encode(b"\xff\xd8fakejpeg").decode()

STEPS = [
    {"index": 0, "title": "Isolate power", "instruction": "Turn the isolator off and lock it",
     "target_phrase": "red isolator switch", "success_criteria": "switch off and locked",
     "hazard": "Live electrics", "expert_said": "Lock it before you touch anything.",
     "watch_for": [{"role": "hazard", "phrase": "blade guard"}]},
    {"index": 1, "title": "Check pressure", "instruction": "Set pressure between 6 and 8 bar",
     "target_phrase": "pressure gauge", "success_criteria": "needle in range", "check": "reading",
     "reading": {"what": "discharge pressure", "min": 6, "max": 8, "unit": "bar"}},
]

GOOD = {
    "steps": [
        {"i": 0, "title": "बिजली बंद करें", "instruction": "आइसोलेटर बंद करके लॉक करें", "hazard": "चालू बिजली है",
         "expert_said": "कुछ छूने से पहले लॉक करें।"},
        {"i": 1, "title": "प्रेशर जाँचें", "instruction": "प्रेशर 6 से 8 बार के बीच रखें", "reading_what": "डिस्चार्ज प्रेशर"},
    ],
    "names": {"red isolator switch": "लाल आइसोलेटर स्विच", "pressure gauge": "प्रेशर गेज", "blade guard": "ब्लेड गार्ड"},
}


def _until(ws, kind, limit=60):
    for _ in range(limit):
        msg = ws.receive_json()
        if msg["type"] == kind:
            return msg
    raise AssertionError(f"no {kind}")


@pytest.fixture
def env(tmp_path, monkeypatch):
    from backend import config, db
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "h.db")
    monkeypatch.setattr(config, "MEDIA_DIR", tmp_path)
    monkeypatch.setattr(config, "VOICE", False)
    monkeypatch.setattr(config, "AUTO_ADVANCE", False)
    db.init()
    pid = db.save_procedure("Isolate and check", steps=[dict(s) for s in STEPS])
    monkeypatch.setattr(guide, "ground_scene", lambda step, frame: {"target": [], "objects": [], "hands": []})
    monkeypatch.setattr(guide, "left_behind", lambda proc, frame: None)
    from backend.main import app
    return TestClient(app), pid, db


# ------------------------------------------------------------- voice commands

@pytest.mark.parametrize("text,intent", [
    ("हो गया", "done"), ("काम हो गया।", "done"), ("ho gaya", "done"), ("अगला", "done"),
    ("फिर से", "repeat"), ("दोबारा बोलो", "repeat"), ("यह कहाँ है", "where"), ("दिखाओ", "where"),
    ("छोड़ो", "skip"), ("क्यों", "why"), ("kya galat hai", "why"),
])
def test_hindi_commands(text, intent):
    assert intent_of(text) == intent


@pytest.mark.parametrize("text", ["नहीं हुआ, हो गया नहीं", "अभी नहीं हो गया", "क्या हो गया", "not done"])
def test_not_done_and_questions_never_trigger_a_check(text):
    assert intent_of(text) != "done"


def test_both_spellings_of_a_hindi_letter_match():
    assert intent_of("छोड़ो") == "skip"            # ड + nukta, then the single letter
    assert intent_of("छोड़ो") == "skip"


@pytest.mark.parametrize("text", ["यह कैसे खुलेगा", "वाल्व कहाँ है भाई", "मदद चाहिए मुझे", "kya yeh sahi hai"])
def test_hindi_questions_are_recognised(text):
    assert is_question(text)


@pytest.mark.parametrize("text", ["अब क्या करूँ", "क्या करना है", "मदद करो", "समझ नहीं आया", "ab kya karu"])
def test_hindi_howto_gets_the_instant_answer(text):
    assert is_howto(text)


def test_workers_chatting_is_not_a_question():
    assert not is_question("चाय पी लेते हैं")


# --------------------------------------------------------------- translation

def test_translated_numbers_are_kept():
    source = translate._source({"steps": STEPS})
    assert translate._clean(source, GOOD, "hi")["steps"]["1"]["instruction"] == "प्रेशर 6 से 8 बार के बीच रखें"


def test_a_changed_number_throws_the_field_away():
    source = translate._source({"steps": STEPS})
    bad = {"steps": [{"i": 1, "title": "प्रेशर", "instruction": "प्रेशर 6 से 9 बार के बीच रखें"}], "names": {}}
    assert "instruction" not in translate._clean(source, bad, "hi")["steps"]["1"]


def test_devanagari_digits_count_as_the_same_number():
    assert translate.numbers("प्रेशर ६ से ८ बार") == translate.numbers("between 6 and 8 bar")


def test_a_lost_warning_falls_back_to_the_original(env, monkeypatch):
    client, pid, db = env
    no_hazard = {"steps": [{"i": 0, "title": "बिजली", "instruction": "बंद करें"},
                           {"i": 1, "title": "प्रेशर", "instruction": "प्रेशर 6 से 8 बार रखें"}], "names": {}}
    monkeypatch.setattr(meta_client, "reason_json", lambda *a, **k: no_hazard)
    with client.websocket_connect("/ws/guide") as ws:
        ws.send_json({"type": "start", "procedure_id": pid, "lang": "hi"})
        said = _until(ws, "speak")["text"]
        ws.send_json({"type": "abort"})
    assert "Live electrics" in said and "चेतावनी" in said


def test_translation_is_cached_per_version(env, monkeypatch):
    client, pid, db = env
    calls = []
    monkeypatch.setattr(meta_client, "reason_json", lambda *a, **k: calls.append(1) or GOOD)
    proc = db.get_procedure(pid)
    translate.for_session(proc, "hi")
    translate.for_session(proc, "hi")
    assert len(calls) == 1
    db.update_procedure_steps(pid, proc["steps"], "edited")
    translate.for_session(db.get_procedure(pid), "hi")
    assert len(calls) == 2


def test_a_procedure_captured_in_hindi_needs_no_translation_to_hindi():
    hindi = {"steps": [{"instruction": "वाल्व बंद करें"}]}
    assert not translate.needs_translation(hindi, "hi")
    assert translate.needs_translation(hindi, "en")
    assert translate.needs_translation({"steps": STEPS}, "hi")


# -------------------------------------------------------------- guide session

def test_hindi_job_speaks_hindi_but_sam_keeps_its_english_phrase(env, monkeypatch):
    client, pid, db = env
    monkeypatch.setattr(meta_client, "reason_json", lambda *a, **k: GOOD)
    with client.websocket_connect("/ws/guide") as ws:
        ws.send_json({"type": "start", "procedure_id": pid, "lang": "hi"})
        assert _until(ws, "preparing")["lang"] == "hi"
        msg = _until(ws, "speak")
        ws.send_json({"type": "abort"})
    assert msg["text"].startswith("चेतावनी। चालू बिजली है आइसोलेटर बंद करके लॉक करें")
    assert msg["title"] == "बिजली बंद करें"
    assert msg["target"] == "red isolator switch"


def test_reading_range_is_spoken_in_hindi_with_the_procedures_numbers(env, monkeypatch):
    client, pid, db = env
    monkeypatch.setattr(meta_client, "reason_json", lambda *a, **k: GOOD)
    seen = {}

    def fake_judge(step, frame, **kw):
        seen.update(kw)
        return {"tool": "verify_step", "say": "ठीक है", "detail": {"confidence": 0.95, "reason": "ok"},
                "verdict": "pass", "latency_ms": 1, "grounding": "", "detections": [], "backend": "hosted"}

    monkeypatch.setattr(guide, "judge_step", fake_judge)
    with client.websocket_connect("/ws/guide") as ws:
        ws.send_json({"type": "start", "procedure_id": pid, "lang": "hi"})
        _until(ws, "speak")
        ws.send_json({"type": "check", "data": FRAME})
        assert _until(ws, "speak")["text"] == "हो गया।"
        msg = _until(ws, "speak")
        ws.send_json({"type": "abort"})
    assert msg["reading"] == "डिस्चार्ज प्रेशर 6 से 8 bar के बीच"
    assert "प्रेशर 6 से 8 बार" in msg["text"]
    assert seen["lang"] == "hi"


def test_translation_failure_keeps_the_job_going_in_english(env, monkeypatch):
    client, pid, db = env

    def boom(*a, **k):
        raise RuntimeError("model down")

    monkeypatch.setattr(meta_client, "reason_json", boom)
    with client.websocket_connect("/ws/guide") as ws:
        ws.send_json({"type": "start", "procedure_id": pid, "lang": "hi"})
        assert "अंग्रेज़ी" in _until(ws, "notice")["text"]
        msg = _until(ws, "speak")
        ws.send_json({"type": "abort"})
    assert "Turn the isolator off and lock it" in msg["text"]


def test_english_job_makes_no_translation_call_and_passes_no_language(env, monkeypatch):
    client, pid, db = env
    monkeypatch.setattr(meta_client, "reason_json",
                        lambda *a, **k: pytest.fail("English must not call the translator"))
    seen = {}

    def fake_judge(step, frame, **kw):
        seen.update(kw)
        return {"tool": "verify_step", "say": "Good", "detail": {"confidence": 0.95, "reason": "ok"},
                "verdict": "pass", "latency_ms": 1, "grounding": "", "detections": [], "backend": "hosted"}

    monkeypatch.setattr(guide, "judge_step", fake_judge)
    with client.websocket_connect("/ws/guide") as ws:
        ws.send_json({"type": "start", "procedure_id": pid})
        assert "Warning. Live electrics Turn the isolator off" in _until(ws, "speak")["text"]
        ws.send_json({"type": "check", "data": FRAME})
        assert _until(ws, "speak")["text"] == "Done."
        ws.send_json({"type": "abort"})
    assert "lang" not in seen


def test_unknown_language_is_english():
    assert i18n.norm("xx") == "en" and i18n.norm(None) == "en" and i18n.norm("hi") == "hi"


# ---------------------------------------------------------------- model prompts

def test_judge_tells_the_model_to_answer_in_hindi(monkeypatch):
    captured = {}

    def fake_reason(system, content, **kw):
        captured.update(system=system, key=kw.get("cache_key"))
        return type("R", (), {"output": [], "output_text": ""})()

    monkeypatch.setattr(meta_client, "reason", fake_reason)
    action = guide.judge_step(STEPS[0], b"\xff\xd8", detections=[], lang="hi")
    assert "Devanagari" in captured["system"] and captured["key"] == "ustad-judge-hi"
    assert action["say"] == i18n.t("hi", "better_view")


def test_ask_keeps_point_at_in_english_for_sam(monkeypatch):
    captured = {}

    def fake_reason(system, content, **kw):
        captured["system"] = system
        return type("R", (), {"output": [], "output_text": ""})()

    monkeypatch.setattr(meta_client, "reason", fake_reason)
    guide.answer_question({"title": "x", "steps": STEPS}, 0, "यह कहाँ है", None, lang="hi")
    assert "point_at" in captured["system"] and "only" in captured["system"]


def test_out_of_range_reading_in_hindi_says_the_real_numbers(monkeypatch):
    call = type("C", (), {"type": "function_call", "name": "verify_step",
                          "arguments": '{"confidence":0.9,"reason":"r","say":"ठीक","reading":"9.5 bar"}'})()
    monkeypatch.setattr(meta_client, "reason",
                        lambda *a, **k: type("R", (), {"output": [call], "output_text": ""})())
    action = guide.judge_step(STEPS[1], b"\xff\xd8", detections=[], lang="hi",
                              spoken_spec="डिस्चार्ज प्रेशर 6 से 8 bar के बीच")
    assert action["verdict"] == "fail"
    assert action["say"] == "रीडिंग 9.5 bar है। यह डिस्चार्ज प्रेशर 6 से 8 bar के बीच होनी चाहिए।"
