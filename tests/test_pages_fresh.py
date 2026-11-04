"""A page must never be served stale: a cached page with no sign-in looked like lost data."""
from __future__ import annotations

from fastapi.testclient import TestClient

from backend import config, main


def test_pages_are_never_cached_but_big_assets_are(monkeypatch):
    monkeypatch.setattr(config, "DEMO_ACCESS_CODE", "")
    c = TestClient(main.app)
    for path in ("/", "/dashboard", "/evidence"):
        assert c.get(path).headers["cache-control"] == "no-store", path
    assert "max-age" in c.get("/assets/logo-blue.png").headers["cache-control"]
