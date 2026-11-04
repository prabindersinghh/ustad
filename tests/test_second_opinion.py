"""The Glimmer second opinion. Models are mocked. What matters most: it never
touches live guiding, needs the supervisor token, and a failure is recorded as a
failure rather than answered by another model under Glimmer's name."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from backend import config, db, guide, meta_client, second_opinion

MODEL = "meta/muse-glimmer-30b"


def _call(name, args):
    return type("Call", (), {"type": "function_call", "name": name, "arguments": args})()


def _result(*calls):
    return type("R", (), {"output": list(calls), "output_text": "", "backend": "open"})()


PASS = '{"confidence":0.95,"reason":"lid is closed","say":"ok"}'
FAIL = '{"severity":"major","what_is_wrong":"lid still open","how_to_fix":"close it","say":"close it"}'
RETRY = '{"what_to_show":"the lid","say":"show the lid"}'


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "s.db")
    monkeypatch.setattr(config, "MEDIA_DIR", tmp_path)
    monkeypatch.setattr(config, "OPEN_BASE_URL", "https://example.test/v1")
    monkeypatch.setattr(config, "OPEN_MODEL", MODEL)
    monkeypatch.setattr(config, "REASONING_BACKEND", "hosted")
    monkeypatch.setattr(config, "supervisor_token", lambda: "tok")
    monkeypatch.setattr(second_opinion, "_running", None)
    db.init()
    steps = [{"index": i, "title": f"Step {i}", "instruction": "x", "target_phrase": "lid",
              "success_criteria": "closed"} for i in range(3)]
    pid = db.save_procedure("Lid job", steps=steps)
    sid = db.start_session(pid, None, "guide", version=1)
    for i, verdict in enumerate(("pass", "fail")):
        (tmp_path / f"e{i}.jpg").write_bytes(b"\xff\xd8photo")
        db.log_step_event(sid, i, verdict, reason="first look", evidence_path=f"media/e{i}.jpg", attempt=1)
    db.log_step_event(sid, 2, "skipped", reason="skipped")
    from backend import live, main
    monkeypatch.setattr(main, "_second_opinion_gate", live._RateGate(100, 600.0))
    return TestClient(main.app), sid


def _wait():
    if second_opinion._thread:
        second_opinion._thread.join(timeout=10)


def test_needs_the_supervisor_token(env, monkeypatch):
    client, sid = env
    monkeypatch.setattr(meta_client, "_reason_open", lambda *a, **k: pytest.fail("must not call the model"))
    assert client.post(f"/api/sessions/{sid}/second-opinion").status_code == 401
    assert client.post(f"/api/sessions/{sid}/second-opinion",
                       headers={"X-Supervisor-Token": "wrong"}).status_code == 401


def test_records_what_glimmer_said_with_the_model_id(env, monkeypatch):
    client, sid = env
    answers = [_result(_call("verify_step", PASS)), _result(_call("flag_error", FAIL))]
    monkeypatch.setattr(meta_client, "_reason_open", lambda *a, **k: answers.pop(0))
    r = client.post(f"/api/sessions/{sid}/second-opinion", headers={"X-Supervisor-Token": "tok"})
    assert r.status_code == 200 and r.json()["steps"] == 2      # the skipped step has no photo
    _wait()
    data = client.get(f"/api/sessions/{sid}/second-opinion").json()
    assert data["available"] and not data["running"] and data["model"] == MODEL
    first, second = data["rows"]
    assert first["model"] == MODEL and first["verdict"] == "pass" and first["agrees"] == 1
    assert first["reason"] == "lid is closed"
    assert second["first_verdict"] == "fail" and second["verdict"] == "fail" and second["agrees"] == 1


def test_a_disagreement_is_shown_as_one(env, monkeypatch):
    client, sid = env
    answers = [_result(_call("request_better_view", RETRY)), _result(_call("verify_step", PASS))]
    monkeypatch.setattr(meta_client, "_reason_open", lambda *a, **k: answers.pop(0))
    client.post(f"/api/sessions/{sid}/second-opinion", headers={"X-Supervisor-Token": "tok"})
    _wait()
    rows = client.get(f"/api/sessions/{sid}/second-opinion").json()["rows"]
    assert [r["agrees"] for r in rows] == [0, 0]


def test_not_yet_in_different_words_still_agrees(env, monkeypatch):
    client, sid = env
    answers = [_result(_call("verify_step", PASS)), _result(_call("request_better_view", RETRY))]
    monkeypatch.setattr(meta_client, "_reason_open", lambda *a, **k: answers.pop(0))
    client.post(f"/api/sessions/{sid}/second-opinion", headers={"X-Supervisor-Token": "tok"})
    _wait()
    assert client.get(f"/api/sessions/{sid}/second-opinion").json()["rows"][1]["agrees"] == 1


def test_a_failed_call_is_an_error_never_another_models_answer(env, monkeypatch):
    client, sid = env

    def down(*a, **k):
        raise RuntimeError("secret detail that must not reach the page")

    monkeypatch.setattr(meta_client, "_reason_open", down)
    monkeypatch.setattr(config, "OPEN_FALLBACK", True)     # even with live fallback on
    monkeypatch.setattr(meta_client, "client", lambda: pytest.fail("must not fall back to the hosted model"))
    client.post(f"/api/sessions/{sid}/second-opinion", headers={"X-Supervisor-Token": "tok"})
    _wait()
    rows = client.get(f"/api/sessions/{sid}/second-opinion").json()["rows"]
    assert all(r["verdict"] is None and r["error"] == "RuntimeError" for r in rows)
    assert all("secret" not in str(r) for r in rows)


def test_second_opinion_changes_nothing_in_the_job_record(env, monkeypatch):
    client, sid = env
    before = db.session_log(sid)
    monkeypatch.setattr(meta_client, "_reason_open", lambda *a, **k: _result(_call("flag_error", FAIL)))
    client.post(f"/api/sessions/{sid}/second-opinion", headers={"X-Supervisor-Token": "tok"})
    _wait()
    assert db.session_log(sid) == before


def test_live_judging_stays_on_the_hosted_model(env, monkeypatch):
    """Having Glimmer configured must not move live checks onto it."""
    monkeypatch.setattr(meta_client, "_reason_open", lambda *a, **k: pytest.fail("live check went to Glimmer"))
    seen = {}

    def hosted(system, content, **kw):
        seen.update(kw)
        return _result(_call("verify_step", PASS))

    assert meta_client.active_backend() == "hosted"
    monkeypatch.setattr(meta_client, "reason", hosted)
    guide.judge_step({"index": 0, "title": "x", "instruction": "x", "target_phrase": "lid"}, b"\xff\xd8", detections=[])
    assert "force_open" not in seen


def test_force_open_uses_glimmer_while_the_product_is_on_hosted(env, monkeypatch):
    monkeypatch.setattr(meta_client, "_reason_open", lambda *a, **k: "from glimmer")
    assert meta_client.reason("sys", [], force_open=True) == "from glimmer"


def test_not_configured_is_a_clear_refusal(env, monkeypatch):
    client, sid = env
    monkeypatch.setattr(config, "OPEN_BASE_URL", "")
    r = client.post(f"/api/sessions/{sid}/second-opinion", headers={"X-Supervisor-Token": "tok"})
    assert r.status_code == 409
    assert client.get(f"/api/sessions/{sid}/second-opinion").json()["available"] is False


def test_only_one_runs_at_a_time(env, monkeypatch):
    client, sid = env
    monkeypatch.setattr(second_opinion, "_running", "another-job")
    r = client.post(f"/api/sessions/{sid}/second-opinion", headers={"X-Supervisor-Token": "tok"})
    assert r.status_code == 409


def test_unknown_job_and_job_without_photos(env):
    client, sid = env
    h = {"X-Supervisor-Token": "tok"}
    assert client.post("/api/sessions/nope/second-opinion", headers=h).status_code == 404
    pid = db.save_procedure("Empty", steps=[{"index": 0, "title": "x", "instruction": "x"}])
    empty = db.start_session(pid, None, "guide", version=1)
    assert client.post(f"/api/sessions/{empty}/second-opinion", headers=h).status_code == 409
