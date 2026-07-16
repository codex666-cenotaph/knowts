"""Entra ID / OIDC SSO: pure-logic unit tests plus login-page behaviour.

The full authorization-code round trip needs a live Entra tenant, so these
tests cover everything up to (and around) that boundary: claim parsing, the
admin/domain policy, just-in-time provisioning against a real SQLite DB, and
that the login page + SSO routes react correctly to configuration — all
offline, no network.
"""

from __future__ import annotations

import importlib
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import db as db_mod
from app import oidc as oidc_mod
from app import users as users_mod
from app.config import Settings


# --- claim parsing ----------------------------------------------------------


def test_identity_from_claims_prefers_oid_and_email():
    ident = oidc_mod.identity_from_claims(
        {"oid": "obj-123", "email": "Alice@Example.com", "name": "Alice"}
    )
    assert ident is not None
    assert ident.subject == "obj-123"
    assert ident.email == "alice@example.com"  # normalised
    assert ident.display_name == "Alice"
    assert ident.email_domain == "example.com"


def test_identity_falls_back_to_preferred_username():
    ident = oidc_mod.identity_from_claims(
        {"sub": "s-1", "preferred_username": "bob@example.com"}
    )
    assert ident is not None
    assert ident.subject == "s-1"
    assert ident.email == "bob@example.com"


@pytest.mark.parametrize(
    "claims",
    [
        {"email": "no-subject@example.com"},  # missing subject
        {"oid": "x"},  # missing email
        {"oid": "x", "email": "not-an-email"},  # no @
    ],
)
def test_identity_rejects_incomplete_claims(claims):
    assert oidc_mod.identity_from_claims(claims) is None


# --- policy helpers ---------------------------------------------------------


def _settings(**over) -> Settings:
    base = dict(SECRET_KEY="k", OIDC_ENABLED="true")
    base.update(over)
    return Settings(**{k: v for k, v in base.items()})


def test_admin_allowlist_is_case_insensitive():
    s = _settings(OIDC_ADMIN_EMAILS="Boss@Example.com, cfo@example.com")
    ident = oidc_mod.identity_from_claims({"oid": "1", "email": "boss@example.com"})
    other = oidc_mod.identity_from_claims({"oid": "2", "email": "temp@example.com"})
    assert oidc_mod.should_be_admin(s, ident) is True
    assert oidc_mod.should_be_admin(s, other) is False


def test_domain_restriction():
    s = _settings(OIDC_ALLOWED_EMAIL_DOMAIN="example.com")
    ok = oidc_mod.identity_from_claims({"oid": "1", "email": "a@example.com"})
    bad = oidc_mod.identity_from_claims({"oid": "2", "email": "a@evil.com"})
    assert oidc_mod.is_email_allowed(s, ok) is True
    assert oidc_mod.is_email_allowed(s, bad) is False
    # No restriction configured -> everything allowed.
    assert oidc_mod.is_email_allowed(_settings(), bad) is True


# --- just-in-time provisioning against a real DB ----------------------------


@pytest.fixture
def conn(tmp_path: Path):
    c = db_mod.connect(tmp_path / "t.db")
    db_mod.run_migrations(c)
    yield c
    c.close()


def test_first_login_provisions_member(conn):
    s = _settings()
    ident = oidc_mod.identity_from_claims({"oid": "o1", "email": "new@example.com"})
    user = oidc_mod.resolve_user(conn, s, ident)
    assert user.role == "member"
    assert user.username == "new@example.com"
    assert user.email == "new@example.com"
    # SSO-only: the sentinel password can never verify.
    assert users_mod.verify_credentials(conn, "new@example.com", "anything") is None


def test_admin_allowlist_provisions_admin_and_is_stable(conn):
    s = _settings(OIDC_ADMIN_EMAILS="boss@example.com")
    ident = oidc_mod.identity_from_claims({"oid": "o2", "email": "boss@example.com"})
    first = oidc_mod.resolve_user(conn, s, ident)
    assert first.is_admin
    # Second login matches by oid, does not duplicate the account.
    second = oidc_mod.resolve_user(conn, s, ident)
    assert second.id == first.id
    assert users_mod.count(conn) == 1


def test_existing_local_admin_is_linked_by_username(conn):
    # An admin seeded before SSO (username == their work email) keeps their id
    # and admin role, and gets the oid linked on first SSO login.
    seeded = users_mod.create(conn, "marco@example.com", "localpass123", role="admin")
    ident = oidc_mod.identity_from_claims({"oid": "o3", "email": "marco@example.com"})
    linked = oidc_mod.resolve_user(conn, _settings(), ident)
    assert linked.id == seeded.id
    assert linked.is_admin  # not demoted
    assert users_mod.get_by_oidc_subject(conn, "o3").id == seeded.id


def test_sync_never_demotes(conn):
    # Provision as admin, then a login with an empty allowlist must not demote.
    admin_ident = oidc_mod.identity_from_claims({"oid": "o4", "email": "a@example.com"})
    oidc_mod.resolve_user(conn, _settings(OIDC_ADMIN_EMAILS="a@example.com"), admin_ident)
    again = oidc_mod.resolve_user(conn, _settings(), admin_ident)
    assert again.is_admin


# --- login page + route wiring (offline) ------------------------------------


def _client(tmp_path, monkeypatch, **env) -> TestClient:
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("SECRET_KEY", "test-secret-key-fixed-for-determinism")
    monkeypatch.setenv("ADMIN_USER", "admin")
    monkeypatch.setenv("ADMIN_PASSWORD", "adminpass123")
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    from app import config

    config.get_settings.cache_clear()
    import app.main as main

    main = importlib.reload(main)
    return TestClient(main.app)


def test_sso_disabled_by_default(tmp_path, monkeypatch):
    with _client(tmp_path, monkeypatch) as c:
        page = c.get("/login")
        assert "btn-sso" not in page.text
        # Local form is present, and the SSO routes 404.
        assert 'action="/login"' in page.text
        assert c.get("/auth/sso/login", follow_redirects=False).status_code == 404


def test_sso_button_shown_when_configured(tmp_path, monkeypatch):
    with _client(
        tmp_path,
        monkeypatch,
        OIDC_ENABLED="true",
        OIDC_TENANT_ID="00000000-0000-0000-0000-000000000000",
        OIDC_CLIENT_ID="client-abc",
        OIDC_CLIENT_SECRET="secret-xyz",
    ) as c:
        page = c.get("/login")
        assert "btn-sso" in page.text
        assert "/auth/sso/login" in page.text
        # Hybrid default: local form still offered.
        assert 'action="/login"' in page.text


def test_local_login_can_be_hidden_but_reachable(tmp_path, monkeypatch):
    with _client(
        tmp_path,
        monkeypatch,
        OIDC_ENABLED="true",
        OIDC_TENANT_ID="00000000-0000-0000-0000-000000000000",
        OIDC_CLIENT_ID="client-abc",
        OIDC_CLIENT_SECRET="secret-xyz",
        LOCAL_LOGIN_ENABLED="false",
    ) as c:
        hidden = c.get("/login")
        assert 'action="/login"' not in hidden.text  # form hidden
        assert "btn-sso" in hidden.text
        # Break-glass: ?local=1 brings the form back.
        assert 'action="/login"' in c.get("/login?local=1").text


class _FakeClient:
    """Stands in for the Authlib client: returns canned Entra claims instead of
    doing the real token exchange, so the callback's provisioning + session
    logic can be driven end-to-end offline."""

    def __init__(self, claims):
        self._claims = claims

    async def authorize_access_token(self, request):
        return {"userinfo": self._claims}


class _FakeOAuth:
    def __init__(self, claims):
        self._client = _FakeClient(claims)

    def create_client(self, name):
        return self._client


def _configured_client(tmp_path, monkeypatch, **env):
    return _client(
        tmp_path,
        monkeypatch,
        OIDC_ENABLED="true",
        OIDC_TENANT_ID="00000000-0000-0000-0000-000000000000",
        OIDC_CLIENT_ID="client-abc",
        OIDC_CLIENT_SECRET="secret-xyz",
        **env,
    )


def test_callback_signs_in_and_provisions_member(tmp_path, monkeypatch):
    with _configured_client(tmp_path, monkeypatch) as c:
        c.app.state.oauth = _FakeOAuth({"oid": "abc", "email": "jane@example.com"})
        r = c.get("/auth/sso/callback", follow_redirects=False)
        assert r.status_code == 303
        assert r.headers["location"] == "/"
        # The session cookie now grants access to a protected page as a member.
        home = c.get("/")
        assert home.status_code == 200
        assert "jane@example.com" in home.text
        assert "/admin/users" not in home.text  # member, not admin


def test_callback_grants_admin_from_allowlist(tmp_path, monkeypatch):
    with _configured_client(
        tmp_path, monkeypatch, OIDC_ADMIN_EMAILS="boss@example.com"
    ) as c:
        c.app.state.oauth = _FakeOAuth({"oid": "def", "email": "boss@example.com"})
        c.get("/auth/sso/callback", follow_redirects=False)
        assert "/admin/users" in c.get("/").text  # admin nav visible


def test_callback_rejects_wrong_domain(tmp_path, monkeypatch):
    with _configured_client(
        tmp_path, monkeypatch, OIDC_ALLOWED_EMAIL_DOMAIN="example.com"
    ) as c:
        c.app.state.oauth = _FakeOAuth({"oid": "ghi", "email": "intruder@evil.com"})
        r = c.get("/auth/sso/callback", follow_redirects=False)
        assert r.status_code == 401
        assert "not permitted" in r.text
        assert c.get("/", follow_redirects=False).status_code == 303  # not signed in


def test_incomplete_oidc_config_leaves_sso_off(tmp_path, monkeypatch):
    # Enabled but missing client secret -> treated as not configured.
    with _client(
        tmp_path,
        monkeypatch,
        OIDC_ENABLED="true",
        OIDC_TENANT_ID="tenant",
        OIDC_CLIENT_ID="client-abc",
    ) as c:
        assert "btn-sso" not in c.get("/login").text
        assert c.get("/auth/sso/login", follow_redirects=False).status_code == 404
