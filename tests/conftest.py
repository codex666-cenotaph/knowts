"""Test fixtures: a fresh app instance backed by a temp data dir per test."""

from __future__ import annotations

import importlib
from pathlib import Path

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("SECRET_KEY", "test-secret-key-fixed-for-determinism")
    monkeypatch.setenv("ADMIN_USER", "admin")
    monkeypatch.setenv("ADMIN_PASSWORD", "adminpass123")
    # Low threshold so the rate-limit test is fast.
    monkeypatch.setenv("LOGIN_MAX_ATTEMPTS", "3")
    monkeypatch.setenv("LOGIN_LOCKOUT_SECONDS", "60")

    # Rebuild modules so the cached Settings pick up this test's env.
    from app import config

    config.get_settings.cache_clear()
    import app.main as main

    main = importlib.reload(main)

    with TestClient(main.app) as c:
        yield c


def login(client: TestClient, username: str, password: str) -> "object":
    return client.post(
        "/login",
        data={"username": username, "password": password},
        follow_redirects=False,
    )


def csrf_from(client: TestClient, path: str) -> str:
    """Pull the CSRF token out of a rendered form on the given page."""
    import re

    html = client.get(path).text
    m = re.search(r'name="csrf_token" value="([^"]+)"', html)
    assert m, f"no csrf token on {path}"
    return m.group(1)
