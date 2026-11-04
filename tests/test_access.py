"""The public demo's sign-in and spending limits. No network: models are never called."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from backend import access, config, db, live, main, meta_client

CODE = "USTAD-TEST-CODE"


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "a.db")
    monkeypatch.setattr(config, "MEDIA_DIR", tmp_path)
    monkeypatch.setattr(config, "DEMO_ACCESS_CODE", CODE)
    monkeypatch.setattr(config, "supervisor_token", lambda: "tok")
    monkeypatch.setattr(main, "_login_gates", {})
    monkeypatch.setattr(main, "_login_all", live._RateGate(1000, 600.0))
    monkeypatch.setattr(main, "_socket_gates", {})
    db.init()
    access.reset_spend_cache()
    (tmp_path / "photo.jpg").write_bytes(b"\xff\xd8photo")
    yield TestClient(main.app, base_url="https://testserver", follow_redirects=False)
    access.reset_spend_cache()


def _sign_in(c):
    assert c.post("/api/login", json={"code": CODE}).status_code == 200


def _ws(c, path):
    # The test client's sockets are ws://, so a Secure cookie must be passed by hand.
    return c.websocket_connect(path, headers={"cookie": f"{access.COOKIE}={c.cookies.get(access.COOKIE)}"})


def test_landing_is_public(client):
    assert client.get("/").status_code == 200
    assert client.get("/api/health").status_code == 200
    assert client.get("/api/me").json() == {"required": True, "signed_in": False}


@pytest.mark.parametrize("path", ["/api/procedures", "/api/sessions", "/api/metrics", "/api/usage", "/media/photo.jpg"])
def test_data_and_photos_need_sign_in(client, path):
    assert client.get(path).status_code == 401
    _sign_in(client)
    assert client.get(path).status_code == 200


def test_pages_send_you_to_sign_in_and_keep_where_you_were_going(client):
    r = client.get("/evidence?s=abc")
    assert r.status_code == 303 and r.headers["location"] == "/?signin=%2Fevidence%3Fs%3Dabc"
    _sign_in(client)
    assert client.get("/evidence?s=abc").status_code == 200


def test_wrong_code_and_forged_cookie_are_refused(client):
    assert client.post("/api/login", json={"code": "nope"}).status_code == 401
    assert client.post("/api/login", json={"code": 5}).status_code == 401
    client.cookies.set(access.COOKIE, "9999999999.forged")
    assert client.get("/api/sessions").status_code == 401


def test_expired_sign_in_is_refused(client, monkeypatch):
    _sign_in(client)
    token = client.cookies.get(access.COOKIE)
    assert access.signed_in(token)
    monkeypatch.setattr(access.time, "time", lambda: 10 ** 11)
    assert not access.signed_in(token)


def test_cookie_is_protected(client):
    cookie = client.post("/api/login", json={"code": CODE}).headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=lax" in cookie and "secure" in cookie


def test_guessing_is_limited(client):
    codes = [client.post("/api/login", json={"code": "x"}).status_code for _ in range(10)]
    assert codes[:8] == [401] * 8 and codes[8:] == [429, 429]


@pytest.mark.parametrize("path", ["/ws/guide", "/ws/capture"])
def test_sockets_refuse_without_sign_in(client, path):
    with client.websocket_connect(path) as ws:
        msg = ws.receive_json()
        assert msg["type"] == "error" and "sign in" in msg["message"].lower()
        with pytest.raises(WebSocketDisconnect):
            ws.receive_json()


def test_sockets_open_after_sign_in(client, monkeypatch):
    _sign_in(client)
    with _ws(client, "/ws/capture") as ws:
        ws.send_json({"type": "ping"})
        assert ws.receive_json()["type"] == "pong"


def test_budget_stops_new_sessions_and_paid_calls(client, monkeypatch):
    _sign_in(client)
    db.log_usage("sam", "image", 1, 30.0, None)
    monkeypatch.setattr(config, "BUDGET_USD", 25.0)
    access.reset_spend_cache()
    with _ws(client, "/ws/guide") as ws:
        assert "spending limit" in ws.receive_json()["message"]
    monkeypatch.setattr(meta_client, "client", lambda: pytest.fail("must not reach the paid API"))
    with pytest.raises(meta_client.SpendLimit):
        meta_client.reason("sys", [], force_hosted=True)


def test_hourly_cap(client, monkeypatch):
    db.log_usage("sam", "image", 1, 5.0, None)
    monkeypatch.setattr(config, "BUDGET_USD", 50.0)
    monkeypatch.setattr(config, "HOURLY_SPEND_CAP_USD", 4.0)
    access.reset_spend_cache()
    assert "hourly" in access.spend_stop()


def test_too_many_sockets_from_one_device(client, monkeypatch):
    _sign_in(client)
    monkeypatch.setattr(config, "SOCKETS_PER_CLIENT_PER_HOUR", 1)
    with _ws(client, "/ws/capture"):
        pass
    with _ws(client, "/ws/capture") as ws:
        assert "Too many" in ws.receive_json()["message"]


def test_demo_full(client, monkeypatch):
    _sign_in(client)
    monkeypatch.setattr(config, "MAX_LIVE_SOCKETS", 1)
    monkeypatch.setattr(main, "_live_sockets", 1)
    with _ws(client, "/ws/guide") as ws:
        assert "full" in ws.receive_json()["message"]


def test_no_code_means_open_for_local_work(client, monkeypatch):
    monkeypatch.setattr(config, "DEMO_ACCESS_CODE", "")
    assert client.get("/api/sessions").status_code == 200
    assert client.get("/api/me").json() == {"required": False, "signed_in": True}
