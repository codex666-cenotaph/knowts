"""HTTP-level tests for the meeting routes (upload, ownership, downloads).

The background pipeline is stubbed out (``process_meeting`` -> no-op) so these
tests exercise routing/validation/ownership deterministically, without ffmpeg or
network calls.
"""

from __future__ import annotations

from tests.conftest import csrf_from, login


def _stub_pipeline(monkeypatch):
    from app import pipeline

    monkeypatch.setattr(pipeline, "process_meeting", lambda *a, **k: None)


def _upload(client, csrf, *, title="Team sync", prompt_ids=("1",), filename="m.mp3"):
    # httpx encodes a dict value that is a list as repeated form fields; a
    # list-of-tuples `data=` with `files=` does NOT round-trip through multipart.
    data = {
        "title": title,
        "meeting_date": "2026-01-02",
        "csrf_token": csrf,
        "prompt_ids": list(prompt_ids),
    }
    return client.post(
        "/meetings",
        data=data,
        files={"file": (filename, b"ID3fake-audio-bytes", "audio/mpeg")},
        follow_redirects=False,
    )


def test_meetings_requires_login(client):
    assert client.get("/meetings", follow_redirects=False).status_code == 303


def test_upload_creates_meeting_and_jobs(client, monkeypatch):
    _stub_pipeline(monkeypatch)
    login(client, "admin", "adminpass123")
    csrf = csrf_from(client, "/")
    r = _upload(client, csrf)
    assert r.status_code == 303
    loc = r.headers["location"]
    assert loc.startswith("/meetings/")

    detail = client.get(loc)
    assert detail.status_code == 200
    assert "Team sync" in detail.text
    # A transcribe job and at least one notes job were queued.
    assert "transcribe" in detail.text


def test_upload_rejects_bad_extension(client, monkeypatch):
    _stub_pipeline(monkeypatch)
    login(client, "admin", "adminpass123")
    csrf = csrf_from(client, "/")
    r = client.post(
        "/meetings",
        data={"title": "x", "csrf_token": csrf},
        files={"file": ("notes.txt", b"hello", "text/plain")},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert "err=" in r.headers["location"]


def test_upload_requires_csrf(client, monkeypatch):
    _stub_pipeline(monkeypatch)
    login(client, "admin", "adminpass123")
    r = _upload(client, "bogus-csrf")
    assert r.status_code == 403


def test_upload_requires_title(client, monkeypatch):
    _stub_pipeline(monkeypatch)
    login(client, "admin", "adminpass123")
    csrf = csrf_from(client, "/")
    r = _upload(client, csrf, title="   ")
    assert r.status_code == 303
    assert "Title" in r.headers["location"]


def test_ownership_isolation(client, monkeypatch):
    _stub_pipeline(monkeypatch)
    # admin uploads a meeting.
    login(client, "admin", "adminpass123")
    csrf = csrf_from(client, "/")
    admin_meeting = _upload(client, csrf).headers["location"]

    # Create a member and upload as them.
    admin_csrf = csrf_from(client, "/admin/users")
    client.post(
        "/admin/users/create",
        data={"username": "mallory", "password": "mallorypass1", "role": "member", "csrf_token": admin_csrf},
        follow_redirects=False,
    )

    # A second client WITHOUT `with` shares the already-running app (entering a
    # nested TestClient context would re-run the lifespan and close the shared DB).
    member = client.__class__(client.app)
    member.post(
        "/login",
        data={"username": "mallory", "password": "mallorypass1"},
        follow_redirects=False,
    )
    # Member cannot see the admin's meeting (404, not 403 — no existence leak).
    assert member.get(admin_meeting, follow_redirects=False).status_code == 404
    # Member's own upload is visible to them.
    m_csrf = csrf_from(member, "/")
    mine = _upload(member, m_csrf, title="Mine").headers["location"]
    assert member.get(mine).status_code == 200

    # Admin can see everyone's meetings.
    assert client.get(mine).status_code == 200


def test_transcript_download_404_before_ready(client, monkeypatch):
    _stub_pipeline(monkeypatch)
    login(client, "admin", "adminpass123")
    csrf = csrf_from(client, "/")
    loc = _upload(client, csrf).headers["location"]
    assert client.get(f"{loc}/transcript.txt").status_code == 404


def test_delete_meeting(client, monkeypatch):
    _stub_pipeline(monkeypatch)
    login(client, "admin", "adminpass123")
    csrf = csrf_from(client, "/")
    loc = _upload(client, csrf).headers["location"]
    detail_csrf = csrf_from(client, loc)
    r = client.post(
        f"{loc}/delete", data={"csrf_token": detail_csrf}, follow_redirects=False
    )
    assert r.status_code == 303
    assert client.get(loc, follow_redirects=False).status_code == 404
