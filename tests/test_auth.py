"""End-to-end auth tests via the ASGI app (real SQLite in a temp dir)."""

from __future__ import annotations

from tests.conftest import csrf_from, login


def test_healthz(client):
    r = client.get("/healthz")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_home_requires_login(client):
    r = client.get("/", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"].startswith("/login")


def test_login_and_access_home(client):
    r = login(client, "admin", "adminpass123")
    assert r.status_code == 303
    assert r.headers["location"] == "/"

    home = client.get("/")
    assert home.status_code == 200
    # Home is the upload page from Phase 2; the topbar greets the logged-in user.
    assert "New meeting" in home.text
    assert "admin" in home.text


def test_login_wrong_password(client):
    r = login(client, "admin", "nope")
    assert r.status_code == 401
    assert "Invalid username or password" in r.text


def test_logout_clears_session(client):
    login(client, "admin", "adminpass123")
    csrf = csrf_from(client, "/")
    r = client.post("/logout", data={"csrf_token": csrf}, follow_redirects=False)
    assert r.status_code == 303
    # Now protected pages redirect again.
    assert client.get("/", follow_redirects=False).status_code == 303


def test_logout_requires_csrf(client):
    login(client, "admin", "adminpass123")
    r = client.post("/logout", data={"csrf_token": "bogus"}, follow_redirects=False)
    assert r.status_code == 403


def test_rate_limit_locks_out(client):
    # Threshold is 3 in the test env.
    for _ in range(3):
        assert login(client, "admin", "wrong").status_code == 401
    r = login(client, "admin", "wrong")
    assert r.status_code == 429
    # Correct password is also blocked while locked out.
    assert login(client, "admin", "adminpass123").status_code == 429


def test_admin_can_create_and_member_cannot_reach_admin(client):
    login(client, "admin", "adminpass123")
    csrf = csrf_from(client, "/admin/users")
    r = client.post(
        "/admin/users/create",
        data={"username": "bob", "password": "bobpass123", "role": "member", "csrf_token": csrf},
        follow_redirects=False,
    )
    assert r.status_code == 303

    # New member cannot see the admin page.
    member = client.__class__(client.app)
    with member:
        assert login_as(member, "bob", "bobpass123")
        assert member.get("/admin/users", follow_redirects=False).status_code == 403


def login_as(c, username, password) -> bool:
    r = c.post(
        "/login",
        data={"username": username, "password": password},
        follow_redirects=False,
    )
    return r.status_code == 303


def test_deactivated_user_cannot_login(client):
    # Create then deactivate a user.
    login(client, "admin", "adminpass123")
    csrf = csrf_from(client, "/admin/users")
    client.post(
        "/admin/users/create",
        data={"username": "carol", "password": "carolpass123", "role": "member", "csrf_token": csrf},
        follow_redirects=False,
    )
    # Find carol's id via the users listing page action URL.
    html = client.get("/admin/users").text
    import re

    m = re.search(r"/admin/users/(\d+)/active", html)
    assert m
    # Deactivate the first non-admin id found by trying each.
    for uid in re.findall(r"/admin/users/(\d+)/active", html):
        client.post(
            f"/admin/users/{uid}/active",
            data={"active": "0", "csrf_token": csrf},
            follow_redirects=False,
        )

    fresh = client.__class__(client.app)
    with fresh:
        assert not login_as(fresh, "carol", "carolpass123")


def test_change_own_password(client):
    login(client, "admin", "adminpass123")
    csrf = csrf_from(client, "/profile")
    r = client.post(
        "/profile/password",
        data={
            "current_password": "adminpass123",
            "new_password": "newpass456",
            "confirm_password": "newpass456",
            "csrf_token": csrf,
        },
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert "msg=Password+updated" in r.headers["location"]

    # Old password no longer works; new one does.
    fresh = client.__class__(client.app)
    with fresh:
        assert not login_as(fresh, "admin", "adminpass123")
    fresh2 = client.__class__(client.app)
    with fresh2:
        assert login_as(fresh2, "admin", "newpass456")
