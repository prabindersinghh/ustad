"""Self-improving procedures and the evidence pack.

Spark is mocked: it proposes edits from the run log; SAM checks proposed
phrases on the expert's photo; nothing changes until approved, and an
approval makes a new version with the old steps kept.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from backend import improve, meta_client, sam_parse


@pytest.fixture
def env(tmp_path, monkeypatch):
    from backend import config, db
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "i.db")
    monkeypatch.setattr(config, "MEDIA_DIR", tmp_path)
    db.init()
    (tmp_path / "ref_p_s00.jpg").write_bytes(b"\xff\xd8ref")
    pid = db.save_procedure("Blade change", steps=[
        {"index": 0, "title": "Loosen bolt", "instruction": "Loosen the bolt", "target_phrase": "bolt",
         "success_criteria": "bolt out", "hazard": "Blade is sharp.", "ref_image": "media/ref_p_s00.jpg"},
        {"index": 1, "title": "Fit blade", "instruction": "Fit the blade", "target_phrase": "blade",
         "success_criteria": "seated"},
    ])
    sid = db.start_session(pid, "Ravi", version=1)
    db.log_step_event(sid, 0, "fail", reason="loosened the wrong bolt", attempt=1)
    db.log_step_event(sid, 0, "asked", reason="Q: which bolt? | A: The centre one.")
    db.log_step_event(sid, 0, "pass", confidence=0.9, reason="bolt out", evidence_path="media/e.jpg", attempt=2,
                      detections=[{"x1": 1, "y1": 2, "x2": 3, "y2": 4, "img_w": 10, "img_h": 10}])
    db.finish_session(sid, "completed")
    from backend.main import app
    return TestClient(app), pid, sid, db


PROPOSALS = {"proposals": [
    {"step": 1, "field": "instruction", "value": "Loosen the centre bolt on the blade hub",
     "why": "workers pick the wrong bolt", "evidence": "1 error: loosened the wrong bolt; asked which bolt"},
    {"step": 1, "field": "target_phrase", "value": "centre hub bolt", "why": "more specific", "evidence": "x"},
    {"step": 1, "field": "tip", "value": "It is the bolt in the middle of the hub.", "why": "asked", "evidence": "x"},
    {"step": 1, "field": "hazard", "value": "Wear cut gloves.", "why": "sharp", "evidence": "x"},
    {"step": 1, "field": "reading", "value": "lower the range", "why": "not allowed", "evidence": "x"},
    {"step": 9, "field": "tip", "value": "out of range step", "why": "", "evidence": ""},
    {"step": 2, "field": "watch_for", "value": "blade edge", "role": "nonsense", "why": "", "evidence": ""},
]}


def test_suggestions_are_checked_and_applied_only_on_approval(env, monkeypatch):
    client, pid, sid, db = env
    seen = {}
    monkeypatch.setattr(meta_client, "reason_json",
                        lambda system, content, **k: seen.setdefault("text", content[0]["text"]) and PROPOSALS)
    box = sam_parse.Detection(frame=0, ordinal=0, x1=5, y1=5, x2=9, y2=9, img_w=20, img_h=20, confidence=0.8)
    monkeypatch.setattr(meta_client, "segment", lambda phrase, img: [box] if phrase == "centre hub bolt" else [])

    prop = client.post(f"/api/procedures/{pid}/improve").json()
    assert "loosened the wrong bolt" in seen["text"] and "which bolt?" in seen["text"]   # the run evidence
    fields = [i["field"] for i in prop["items"]]
    assert fields == ["instruction", "target_phrase", "tip", "hazard"]                 # invalid ones dropped
    target = prop["items"][1]
    assert target["sam_check"] == "found" and target["before"] == "bolt"
    assert db.get_procedure(pid)["steps"][0]["instruction"] == "Loosen the bolt"        # nothing changed yet

    out = client.post(f"/api/procedures/{pid}/proposals/{prop['id']}/apply", json={"accept": [0, 1, 2, 3]}).json()
    assert out == {"version": 2, "applied": 4}
    step = db.get_procedure(pid)["steps"][0]
    assert step["instruction"] == "Loosen the centre bolt on the blade hub"
    assert step["target_phrase"] == "centre hub bolt" and step["ref_box"]["x1"] == 5
    assert step["tips"] == ["It is the bolt in the middle of the hub."]
    assert step["hazard"] == "Blade is sharp. Wear cut gloves."                         # added to, never replaced
    assert db.procedure_history(pid)[0]["version"] == 1                                 # old steps kept

    again = client.post(f"/api/procedures/{pid}/proposals/{prop['id']}/apply", json={"accept": [0]})
    assert again.status_code == 409                                                     # already handled


def test_no_runs_no_suggestions(env):
    client, pid, sid, db = env
    other = db.save_procedure("Fresh", steps=[{"index": 0, "title": "A", "instruction": "do"}])
    r = client.post(f"/api/procedures/{other}/improve")
    assert r.status_code == 409 and "Run it" in r.json()["detail"]


def test_stale_suggestions_are_refused(env, monkeypatch):
    client, pid, sid, db = env
    monkeypatch.setattr(meta_client, "reason_json", lambda *a, **k: PROPOSALS)
    monkeypatch.setattr(meta_client, "segment", lambda *a: [])
    first = client.post(f"/api/procedures/{pid}/improve").json()
    second = client.post(f"/api/procedures/{pid}/improve").json()
    client.post(f"/api/procedures/{pid}/proposals/{second['id']}/apply", json={"accept": [0]})
    r = client.post(f"/api/procedures/{pid}/proposals/{first['id']}/apply", json={"accept": [0]})
    assert r.status_code == 409 and "changed" in r.json()["detail"]


def test_unfindable_watch_item_is_added_as_not_trackable(env, monkeypatch):
    client, pid, sid, db = env
    monkeypatch.setattr(meta_client, "reason_json", lambda *a, **k: {"proposals": [
        {"step": 1, "field": "watch_for", "value": "blade edge", "role": "hazard", "why": "w", "evidence": "e"}]})
    monkeypatch.setattr(meta_client, "segment", lambda *a: [])
    prop = client.post(f"/api/procedures/{pid}/improve").json()
    assert prop["items"][0]["sam_check"] == "not_found"
    client.post(f"/api/procedures/{pid}/proposals/{prop['id']}/apply", json={"accept": [0]})
    w = db.get_procedure(pid)["steps"][0]["watch_for"]
    assert w == [{"phrase": "blade edge", "role": "hazard", "trackable": False}]


def test_evidence_pack_has_the_job_and_its_boxes(env):
    client, pid, sid, db = env
    pack = client.get(f"/api/sessions/{sid}/pack").json()
    assert pack["procedure"]["title"] == "Blade change" and len(pack["procedure"]["steps"]) == 2
    assert pack["session"]["worker_name"] == "Ravi"
    passed = [e for e in pack["events"] if e["verdict"] == "pass"][0]
    assert passed["detections"][0]["x1"] == 1
    assert client.get("/api/sessions/nope/pack").status_code == 404
    assert client.get(f"/evidence?s={sid}").status_code == 200


def test_pack_shows_the_steps_as_they_were_when_the_job_ran(env):
    client, pid, sid, db = env
    db.update_procedure_steps(pid, [{"index": 0, "title": "NEW", "instruction": "new"}], "test")
    pack = client.get(f"/api/sessions/{sid}/pack").json()
    assert pack["procedure"]["version"] == 1 and pack["procedure"]["steps"][0]["title"] == "Loosen bolt"


def test_metrics_count_checks_not_questions(env):
    client, pid, sid, db = env
    m = db.metrics()
    assert m["checks"] == 2 and m["passes"] == 1 and m["fails"] == 1      # the question isn't a check
    ins = db.step_insights(pid)["steps"][0]
    assert ins["asked"] == 1 and ins["questions"] == ["Q: which bolt? | A: The centre one."]
    assert ins["runs"] == 1 and ins["first_try"] == 0 and ins["caught"] == 1
