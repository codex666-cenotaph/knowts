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


def _upload(client, csrf, *, title="Team sync", prompt_ids=("1",), filename="m.mp3",
            language=None, num_speakers=None):
    # httpx encodes a dict value that is a list as repeated form fields; a
    # list-of-tuples `data=` with `files=` does NOT round-trip through multipart.
    data = {
        "title": title,
        "meeting_date": "2026-01-02",
        "csrf_token": csrf,
        "prompt_ids": list(prompt_ids),
    }
    if language is not None:
        data["language"] = language
    if num_speakers is not None:
        data["num_speakers"] = num_speakers
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


def test_upload_form_has_language_select_defaulting_to_user_language(client, monkeypatch):
    _stub_pipeline(monkeypatch)
    login(client, "admin", "adminpass123")
    # Admin defaults to English.
    html = client.get("/").text
    assert 'name="language"' in html
    assert '<option value="en" selected>' in html
    assert 'value="auto"' in html


def test_upload_stores_chosen_language(client, monkeypatch):
    _stub_pipeline(monkeypatch)
    from app import meetings as meetings_mod

    login(client, "admin", "adminpass123")
    csrf = csrf_from(client, "/")
    loc = _upload(client, csrf, language="nl").headers["location"]
    meeting_id = int(loc.rstrip("/").rsplit("/", 1)[-1])
    conn = client.app.state.db
    assert meetings_mod.get(conn, meeting_id).language == "nl"


def test_upload_defaults_language_to_user_preference(client, monkeypatch):
    _stub_pipeline(monkeypatch)
    from app import meetings as meetings_mod

    login(client, "admin", "adminpass123")
    csrf = csrf_from(client, "/")
    # No language field submitted -> falls back to the user's language (en).
    loc = _upload(client, csrf).headers["location"]
    meeting_id = int(loc.rstrip("/").rsplit("/", 1)[-1])
    conn = client.app.state.db
    assert meetings_mod.get(conn, meeting_id).language == "en"


def test_upload_stores_num_speakers(client, monkeypatch):
    _stub_pipeline(monkeypatch)
    from app import meetings as meetings_mod

    login(client, "admin", "adminpass123")
    csrf = csrf_from(client, "/")
    loc = _upload(client, csrf, num_speakers="3").headers["location"]
    mid = int(loc.rstrip("/").rsplit("/", 1)[-1])
    conn = client.app.state.db
    assert meetings_mod.get(conn, mid).diarization_num_speakers == 3


def test_upload_num_speakers_defaults_and_clamps(client, monkeypatch):
    _stub_pipeline(monkeypatch)
    from app import meetings as meetings_mod

    login(client, "admin", "adminpass123")
    conn = client.app.state.db
    # Blank/garbage -> 0 (auto).
    csrf = csrf_from(client, "/")
    mid = int(_upload(client, csrf, num_speakers="not-a-number").headers["location"].rstrip("/").rsplit("/", 1)[-1])
    assert meetings_mod.get(conn, mid).diarization_num_speakers == 0
    # Over-large -> clamped to the max.
    csrf = csrf_from(client, "/")
    mid = int(_upload(client, csrf, num_speakers="999").headers["location"].rstrip("/").rsplit("/", 1)[-1])
    assert meetings_mod.get(conn, mid).diarization_num_speakers == 20


def test_upload_form_hides_speakers_field_when_diarization_off(client, monkeypatch):
    _stub_pipeline(monkeypatch)
    login(client, "admin", "adminpass123")
    # Diarization defaults off in the test env -> no speakers field.
    assert 'name="num_speakers"' not in client.get("/").text


def test_upload_form_shows_speakers_field_when_diarization_on(client, monkeypatch):
    _stub_pipeline(monkeypatch)
    login(client, "admin", "adminpass123")
    from app.config import Settings
    from app.routers import home as home_router

    monkeypatch.setattr(
        home_router, "get_settings",
        lambda: Settings(SECRET_KEY="x" * 40, DIARIZATION_ENABLED=True),
    )
    assert 'name="num_speakers"' in client.get("/").text


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


def test_archive_search_and_date_filter(client, monkeypatch):
    _stub_pipeline(monkeypatch)
    login(client, "admin", "adminpass123")
    csrf = csrf_from(client, "/")
    # Two meetings with distinct titles/dates.
    client.post(
        "/meetings",
        data={"title": "Budget review", "meeting_date": "2026-03-10", "csrf_token": csrf, "prompt_ids": ["1"]},
        files={"file": ("a.mp3", b"ID3x", "audio/mpeg")},
        follow_redirects=False,
    )
    client.post(
        "/meetings",
        data={"title": "Hiring sync", "meeting_date": "2026-05-20", "csrf_token": csrf, "prompt_ids": ["1"]},
        files={"file": ("b.mp3", b"ID3y", "audio/mpeg")},
        follow_redirects=False,
    )

    # Title search.
    hits = client.get("/meetings?q=Budget").text
    assert "Budget review" in hits and "Hiring sync" not in hits

    # Date range excludes the March meeting.
    ranged = client.get("/meetings?date_from=2026-05-01").text
    assert "Hiring sync" in ranged and "Budget review" not in ranged

    # A filter that matches nothing shows the empty-filter message.
    none = client.get("/meetings?q=zzz-nomatch").text
    assert "No meetings match" in none


def test_transcript_exports_include_speakers(client, monkeypatch):
    _stub_pipeline(monkeypatch)
    from app import meetings as meetings_mod

    login(client, "admin", "adminpass123")
    csrf = csrf_from(client, "/")
    loc = _upload(client, csrf).headers["location"]
    meeting_id = int(loc.rstrip("/").rsplit("/", 1)[-1])

    conn = client.app.state.db
    meetings_mod.save_transcript(
        conn,
        meeting_id,
        text="hi there good thanks",
        segments=[
            {"start": 0.0, "end": 1.0, "text": "hi there", "speaker": "Speaker A"},
            {"start": 1.0, "end": 2.0, "text": "good thanks", "speaker": "Speaker B"},
        ],
        language="en",
    )

    txt = client.get(f"{loc}/transcript.txt").text
    assert "Speaker A: hi there" in txt and "Speaker B: good thanks" in txt

    srt = client.get(f"{loc}/transcript.srt").text
    assert "Speaker A: hi there" in srt

    detail = client.get(loc).text
    assert 'class="speaker">Speaker A:' in detail


def _fail_all_jobs(conn, meeting_id):
    """Simulate an upstream failure: mark the meeting's jobs errored."""
    from app import jobs as jobs_mod
    from app import meetings as meetings_mod

    for j in jobs_mod.list_for_meeting(conn, meeting_id):
        jobs_mod.mark_error(conn, j.id, "STT service unreachable")
    meetings_mod.set_status(conn, meeting_id, meetings_mod.STATUS_ERROR)


def test_retry_button_shown_and_requeues_failed_jobs(client, monkeypatch):
    _stub_pipeline(monkeypatch)
    from app import jobs as jobs_mod

    login(client, "admin", "adminpass123")
    csrf = csrf_from(client, "/")
    loc = _upload(client, csrf).headers["location"]
    meeting_id = int(loc.rstrip("/").rsplit("/", 1)[-1])
    conn = client.app.state.db
    _fail_all_jobs(conn, meeting_id)

    # The failed state offers a retry control.
    detail = client.get(loc).text
    assert f"/meetings/{meeting_id}/retry" in detail

    retry_csrf = csrf_from(client, loc)
    r = client.post(
        f"{loc}/retry", data={"csrf_token": retry_csrf}, follow_redirects=False
    )
    assert r.status_code == 303
    assert "Retrying" in r.headers["location"]
    # Every job is back on the queue.
    assert all(j.status == jobs_mod.STATUS_QUEUED for j in jobs_mod.list_for_meeting(conn, meeting_id))
    assert jobs_mod.has_failed_jobs(conn, meeting_id) is False


def test_retry_with_nothing_failed_reports_it(client, monkeypatch):
    _stub_pipeline(monkeypatch)
    login(client, "admin", "adminpass123")
    csrf = csrf_from(client, "/")
    loc = _upload(client, csrf).headers["location"]
    retry_csrf = csrf_from(client, loc)
    r = client.post(
        f"{loc}/retry", data={"csrf_token": retry_csrf}, follow_redirects=False
    )
    assert r.status_code == 303
    assert "err=Nothing+to+retry" in r.headers["location"]


def test_retry_requires_csrf(client, monkeypatch):
    _stub_pipeline(monkeypatch)
    login(client, "admin", "adminpass123")
    csrf = csrf_from(client, "/")
    loc = _upload(client, csrf).headers["location"]
    r = client.post(f"{loc}/retry", data={"csrf_token": "bogus"}, follow_redirects=False)
    assert r.status_code == 403


def test_retry_ownership_isolation(client, monkeypatch):
    _stub_pipeline(monkeypatch)
    login(client, "admin", "adminpass123")
    csrf = csrf_from(client, "/")
    loc = _upload(client, csrf).headers["location"]

    admin_csrf = csrf_from(client, "/admin/users")
    client.post(
        "/admin/users/create",
        data={"username": "mallory", "password": "mallorypass1", "role": "member", "csrf_token": admin_csrf},
        follow_redirects=False,
    )
    member = client.__class__(client.app)
    member.post(
        "/login",
        data={"username": "mallory", "password": "mallorypass1"},
        follow_redirects=False,
    )
    # A member cannot retry someone else's meeting (404, no existence leak).
    m_csrf = csrf_from(member, "/")
    r = member.post(f"{loc}/retry", data={"csrf_token": m_csrf}, follow_redirects=False)
    assert r.status_code == 404


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
