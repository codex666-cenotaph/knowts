"""HTTP-level tests for the Phase 3 prompt manager (PLAN.md §4, §10 step 13).

Covers CRUD, clone, archive, server-side permission enforcement, placeholder
validation, and search. The live LLM (model dropdown, test-run) is not
exercised here — the dropdown degrades to empty when llama-swap is unreachable,
which is exactly the test environment.
"""

from __future__ import annotations

from tests.conftest import csrf_from, login


def _pid(loc: str) -> str:
    """Extract the prompt id from a redirect Location (which carries ?msg=...)."""
    return loc.split("?", 1)[0].rstrip("/").rsplit("/", 1)[-1]


def _member(client, username="mel", password="melpass12345"):
    """Create a member via the admin API and return a logged-in client for them."""
    login(client, "admin", "adminpass123")
    admin_csrf = csrf_from(client, "/admin/users")
    client.post(
        "/admin/users/create",
        data={"username": username, "password": password, "role": "member", "csrf_token": admin_csrf},
        follow_redirects=False,
    )
    c = client.__class__(client.app)
    c.post("/login", data={"username": username, "password": password}, follow_redirects=False)
    return c


def _create_prompt(c, csrf, *, name="my prompt", template="Do {transcript}", official=""):
    data = {
        "name": name,
        "description": "desc",
        "system": "sys",
        "template": template,
        "reduce_template": "",
        "model": "",
        "temperature": "",
        "max_tokens": "",
        "csrf_token": csrf,
    }
    if official:
        data["official"] = "1"
    return c.post("/prompts", data=data, follow_redirects=False)


def test_prompts_requires_login(client):
    assert client.get("/prompts", follow_redirects=False).status_code == 303


def test_manager_lists_starter_prompts(client):
    login(client, "admin", "adminpass123")
    html = client.get("/prompts").text
    assert "summary" in html and "action-items" in html
    assert "official" in html  # badged


def test_create_personal_prompt(client):
    login(client, "admin", "adminpass123")
    csrf = csrf_from(client, "/prompts/new")
    r = _create_prompt(client, csrf, name="standup notes")
    assert r.status_code == 303
    loc = r.headers["location"]
    assert loc.startswith("/prompts/")
    assert client.get("/prompts").text.count("standup notes") >= 1


def test_create_rejects_missing_placeholder(client):
    login(client, "admin", "adminpass123")
    csrf = csrf_from(client, "/prompts/new")
    r = _create_prompt(client, csrf, template="no placeholder here")
    assert r.status_code == 303
    assert "err=" in r.headers["location"]
    assert "transcript" in r.headers["location"]


def test_edit_adds_a_new_version(client):
    login(client, "admin", "adminpass123")
    csrf = csrf_from(client, "/prompts/new")
    loc = _create_prompt(client, csrf, name="editable").headers["location"]

    edit_csrf = csrf_from(client, loc)
    r = client.post(
        loc,
        data={
            "name": "editable",
            "description": "desc",
            "system": "sys",
            "template": "Do something else with {transcript}",
            "reduce_template": "",
            "model": "",
            "temperature": "",
            "max_tokens": "",
            "csrf_token": edit_csrf,
        },
        follow_redirects=False,
    )
    assert r.status_code == 303
    page = client.get(loc).text
    assert "v2" in page  # version history shows a second version


def test_member_cannot_edit_official_prompt(client):
    member = _member(client)
    # Find an official prompt id from the manager list link.
    import re

    html = member.get("/prompts").text
    m = re.search(r'/prompts/(\d+)"', html)
    assert m
    pid = m.group(1)
    edit_csrf = csrf_from(member, f"/prompts/{pid}")
    r = member.post(
        f"/prompts/{pid}",
        data={
            "name": "hijacked",
            "description": "",
            "system": "",
            "template": "evil {transcript}",
            "reduce_template": "",
            "model": "",
            "temperature": "",
            "max_tokens": "",
            "csrf_token": edit_csrf,
        },
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert "err=" in r.headers["location"]


def test_clone_makes_personal_copy_owned_by_cloner(client):
    member = _member(client)
    import re

    html = member.get("/prompts").text
    pid = re.search(r'/prompts/(\d+)"', html).group(1)
    clone_csrf = csrf_from(member, "/prompts")
    r = member.post(
        f"/prompts/{pid}/clone",
        data={"csrf_token": clone_csrf},
        follow_redirects=False,
    )
    assert r.status_code == 303
    new_loc = r.headers["location"]
    # The clone is editable by the member (personal, owned by them): saving works.
    edit_csrf = csrf_from(member, new_loc)
    r2 = member.post(
        new_loc,
        data={
            "name": "my clone",
            "description": "",
            "system": "",
            "template": "cloned {transcript}",
            "reduce_template": "",
            "model": "",
            "temperature": "",
            "max_tokens": "",
            "csrf_token": edit_csrf,
        },
        follow_redirects=False,
    )
    assert r2.status_code == 303
    assert "err=" not in r2.headers["location"]


def test_archive_hides_from_picker_but_keeps_in_manager(client):
    login(client, "admin", "adminpass123")
    csrf = csrf_from(client, "/prompts/new")
    loc = _create_prompt(client, csrf, name="temp prompt").headers["location"]
    pid = _pid(loc)

    arch_csrf = csrf_from(client, "/prompts")
    r = client.post(
        f"/prompts/{pid}/archive",
        data={"archived": "1", "csrf_token": arch_csrf},
        follow_redirects=False,
    )
    assert r.status_code == 303
    # Gone from the default (active) manager list and the upload picker...
    assert "temp prompt" not in client.get("/prompts").text
    assert "temp prompt" not in client.get("/upload").text
    # ...but visible with ?show=all.
    assert "temp prompt" in client.get("/prompts?show=all").text


def test_test_run_validates_placeholder(client):
    # /prompts/test must not be shadowed by /prompts/{prompt_id} (int) and must
    # return the preview fragment, not a 400/redirect.
    login(client, "admin", "adminpass123")
    csrf = csrf_from(client, "/prompts/new")
    r = client.post(
        "/prompts/test",
        data={"template": "no placeholder", "transcript": "hello", "csrf_token": csrf},
        follow_redirects=False,
    )
    assert r.status_code == 200
    assert "must contain the {transcript} placeholder" in r.text


def test_search_filters_by_name(client):
    login(client, "admin", "adminpass123")
    html = client.get("/prompts?q=summary").text
    assert "summary" in html
    assert "action-items" not in html
