"""Tests for the profile language preference (English/Dutch interface toggle
and the "respond only in Dutch" note-generation override it triggers)."""

from __future__ import annotations

from tests.conftest import csrf_from, login


def test_profile_shows_language_selector_defaulting_to_english(client):
    login(client, "admin", "adminpass123")
    html = client.get("/profile").text
    assert 'value="en" selected' in html
    assert 'value="nl"' in html


def test_switching_to_dutch_changes_the_interface(client):
    login(client, "admin", "adminpass123")
    csrf = csrf_from(client, "/profile")
    r = client.post(
        "/profile/language", data={"language": "nl", "csrf_token": csrf},
        follow_redirects=False,
    )
    assert r.status_code == 303

    home = client.get("/")
    assert "Uploaden" in home.text
    assert "Vergaderingen" in home.text
    assert '<html lang="nl">' in home.text

    # Profile itself re-renders in Dutch and remembers the selection.
    profile = client.get("/profile").text
    assert "Wachtwoord wijzigen" in profile
    assert 'value="nl" selected' in profile


def test_switching_back_to_english_restores_the_interface(client):
    login(client, "admin", "adminpass123")
    csrf = csrf_from(client, "/profile")
    client.post("/profile/language", data={"language": "nl", "csrf_token": csrf}, follow_redirects=False)
    csrf2 = csrf_from(client, "/profile")
    client.post("/profile/language", data={"language": "en", "csrf_token": csrf2}, follow_redirects=False)
    assert "Upload" in client.get("/").text
    assert "Uploaden" not in client.get("/").text


def test_rejects_unsupported_language(client):
    login(client, "admin", "adminpass123")
    csrf = csrf_from(client, "/profile")
    r = client.post(
        "/profile/language", data={"language": "xx", "csrf_token": csrf},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert "err=" in r.headers["location"]
    # Language is unchanged.
    assert 'value="en" selected' in client.get("/profile").text


def test_language_change_requires_csrf(client):
    login(client, "admin", "adminpass123")
    r = client.post(
        "/profile/language", data={"language": "nl", "csrf_token": "bogus"},
        follow_redirects=False,
    )
    assert r.status_code == 403


def test_prompt_manager_test_run_forces_dutch_for_dutch_user(client, monkeypatch):
    """The test-run preview should apply the same language override real note
    generation would use, based on the current user's profile language."""
    import app.routers.prompts as prompts_router

    captured = {}

    class _FakeClient:
        def __init__(self, *a, **k):
            pass

        def complete(self, *, model, system, user, temperature=None, max_tokens=None):
            captured["system"] = system
            return "**notes**"

    monkeypatch.setattr(prompts_router, "LLMClient", _FakeClient)

    login(client, "admin", "adminpass123")
    csrf = csrf_from(client, "/profile")
    client.post("/profile/language", data={"language": "nl", "csrf_token": csrf}, follow_redirects=False)

    csrf2 = csrf_from(client, "/prompts/new")
    r = client.post(
        "/prompts/test",
        data={
            "system": "base system",
            "template": "Do {transcript}",
            "transcript": "hello world",
            "csrf_token": csrf2,
        },
        follow_redirects=False,
    )
    assert r.status_code == 200
    assert "Dutch" in captured["system"]
    assert captured["system"].startswith("base system\n\n")
