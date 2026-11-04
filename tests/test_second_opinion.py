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


# --- The edge-case review: only the doubtful steps. -------------------------

@pytest.fixture
def mixed(env, tmp_path):
    """Step 0 clean, 1 low confidence, 2 second attempt, 3 failed, 4 a retry then
    a pass, 5 only ever a retry (no photo), 6 a pass with no confidence recorded."""
    client, _ = env
    steps = [{"index": i, "title": f"Step {i}", "instruction": "x", "target_phrase": "lid",
              "success_criteria": "closed"} for i in range(7)]
    pid = db.save_procedure("Mixed job", steps=steps)
    sid = db.start_session(pid, None, "guide", version=1)
    for i in range(7):
        (tmp_path / f"m{i}.jpg").write_bytes(b"\xff\xd8photo")

    def photo(i):
        return f"media/m{i}.jpg"

    db.log_step_event(sid, 0, "pass", confidence=0.95, evidence_path=photo(0), attempt=1)
    db.log_step_event(sid, 1, "pass", confidence=second_opinion.DOUBT_CONFIDENCE - 0.01, evidence_path=photo(1), attempt=1)
    db.log_step_event(sid, 2, "pass", confidence=0.95, evidence_path=photo(2), attempt=2)
    db.log_step_event(sid, 3, "fail", evidence_path=photo(3), attempt=1)
    db.log_step_event(sid, 4, "retry", attempt=1)
    db.log_step_event(sid, 4, "pass", confidence=0.95, evidence_path=photo(4), attempt=1)
    db.log_step_event(sid, 5, "retry", attempt=1)
    db.log_step_event(sid, 6, "pass", evidence_path=photo(6), attempt=1)
    return client, sid


def _start(client, sid, query=""):
    return client.post(f"/api/sessions/{sid}/second-opinion{query}", headers={"X-Supervisor-Token": "tok"})


def test_doubtful_scope_checks_only_the_doubtful_steps(mixed, monkeypatch):
    client, sid = mixed
    monkeypatch.setattr(meta_client, "_reason_open", lambda *a, **k: _result(_call("verify_step", PASS)))
    r = _start(client, sid, "?scope=doubtful")
    assert r.status_code == 200 and r.json()["steps"] == 4 and r.json()["scope"] == "doubtful"
    _wait()
    rows = client.get(f"/api/sessions/{sid}/second-opinion").json()["rows"]
    assert [row["step_index"] for row in rows] == [1, 2, 3, 4]
    assert all(row["model"] == MODEL for row in rows)


def test_with_no_option_every_judged_step_is_still_checked(mixed, monkeypatch):
    client, sid = mixed
    monkeypatch.setattr(meta_client, "_reason_open", lambda *a, **k: _result(_call("verify_step", PASS)))
    r = _start(client, sid)
    assert r.status_code == 200 and r.json()["steps"] == 6 and r.json()["scope"] == "all"
    _wait()
    rows = client.get(f"/api/sessions/{sid}/second-opinion").json()["rows"]
    assert [row["step_index"] for row in rows] == [0, 1, 2, 3, 4, 6]


def test_status_counts_doubtful_steps_and_those_with_no_photo(mixed):
    client, sid = mixed
    data = client.get(f"/api/sessions/{sid}/second-opinion").json()
    assert data["doubtful"] == 4 and data["doubtful_no_photo"] == 1
    assert data["doubt_confidence"] == second_opinion.DOUBT_CONFIDENCE


def test_exactly_at_the_threshold_is_not_doubtful():
    events = [{"step_index": 0, "verdict": "pass", "attempt": 1, "confidence": second_opinion.DOUBT_CONFIDENCE}]
    assert second_opinion.doubtful_steps(events) == set()


def test_skipped_and_confirmed_steps_are_not_doubtful():
    events = [{"step_index": 0, "verdict": "skipped", "attempt": 2, "confidence": 0.1},
              {"step_index": 1, "verdict": "confirmed", "attempt": 3, "confidence": None}]
    assert second_opinion.doubtful_steps(events) == set()


def test_a_clean_job_has_nothing_doubtful_to_check(env, monkeypatch, tmp_path):
    client, _ = env
    monkeypatch.setattr(meta_client, "_reason_open", lambda *a, **k: pytest.fail("must not call the model"))
    pid = db.save_procedure("Clean", steps=[{"index": 0, "title": "x", "instruction": "x"}])
    sid = db.start_session(pid, None, "guide", version=1)
    (tmp_path / "c.jpg").write_bytes(b"\xff\xd8photo")
    db.log_step_event(sid, 0, "pass", confidence=0.99, evidence_path="media/c.jpg", attempt=1)
    r = _start(client, sid, "?scope=doubtful")
    assert r.status_code == 409 and "doubtful" in r.json()["detail"]


def test_an_unknown_scope_is_refused(mixed, monkeypatch):
    client, sid = mixed
    monkeypatch.setattr(meta_client, "_reason_open", lambda *a, **k: pytest.fail("must not call the model"))
    assert _start(client, sid, "?scope=everything").status_code == 400


def test_doubtful_scope_needs_the_token(mixed, monkeypatch):
    client, sid = mixed
    monkeypatch.setattr(meta_client, "_reason_open", lambda *a, **k: pytest.fail("must not call the model"))
    assert client.post(f"/api/sessions/{sid}/second-opinion?scope=doubtful").status_code == 401


def test_doubtful_review_never_falls_back_and_leaves_the_record_alone(mixed, monkeypatch):
    client, sid = mixed
    before = db.session_log(sid)

    def down(*a, **k):
        raise RuntimeError("down")

    monkeypatch.setattr(meta_client, "_reason_open", down)
    monkeypatch.setattr(config, "OPEN_FALLBACK", True)
    monkeypatch.setattr(meta_client, "client", lambda: pytest.fail("must not fall back to the hosted model"))
    _start(client, sid, "?scope=doubtful")
    _wait()
    rows = client.get(f"/api/sessions/{sid}/second-opinion").json()["rows"]
    assert rows and all(r["verdict"] is None and r["error"] == "RuntimeError" for r in rows)
    assert db.session_log(sid) == before


def test_only_one_runs_at_a_time_in_either_scope(mixed, monkeypatch):
    client, sid = mixed
    monkeypatch.setattr(second_opinion, "_running", "another-job")
    assert _start(client, sid, "?scope=doubtful").status_code == 409


def test_the_rate_gate_covers_the_doubtful_scope(mixed, monkeypatch):
    client, sid = mixed
    from backend import live, main
    monkeypatch.setattr(main, "_second_opinion_gate", live._RateGate(0, 600.0))
    monkeypatch.setattr(meta_client, "_reason_open", lambda *a, **k: pytest.fail("must not call the model"))
    assert _start(client, sid, "?scope=doubtful").status_code == 429
