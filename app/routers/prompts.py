"""Prompt manager (PLAN.md §4, §10 step 13).

Full CRUD over database-backed prompts: list/search with official-vs-personal
badges, create, edit (versioned), clone, archive, a live model dropdown from
llama-swap's ``/v1/models``, and a test-run preview that renders a prompt
against a transcript snippet before saving.

Server-side permission rules mirror the plan:
  * official prompts (no owner, read-only) are editable by admins only;
  * personal prompts by their owner or an admin;
  * clone is available to everyone on every prompt.
"""

from __future__ import annotations

import logging
import sqlite3

from fastapi import APIRouter, Depends, Form, Request, status
from fastapi.responses import RedirectResponse

from .. import auth as auth_mod
from .. import prompts as prompts_mod
from ..config import Settings, get_settings
from ..llm import LLMClient, LLMError
from ..notes import system_with_language_override
from ..templating import render
from ..users import User

log = logging.getLogger("knowts.routes.prompts")

router = APIRouter(prefix="/prompts")

# Keep test-runs snappy: only feed the model the start of a long transcript.
_TEST_SNIPPET_CHARS = 6000


def _db(request: Request) -> sqlite3.Connection:
    return request.app.state.db


def _redirect(path: str, query: str = "") -> RedirectResponse:
    suffix = f"?{query}" if query else ""
    return RedirectResponse(f"{path}{suffix}", status_code=status.HTTP_303_SEE_OTHER)


def _available_models(settings: Settings) -> list[str]:
    """Model ids from llama-swap for the dropdown; empty if it's unreachable
    (the form falls back to a free-text field)."""
    try:
        return LLMClient(settings.llm_base_url, timeout=10.0, max_retries=0).list_models()
    except Exception as exc:  # never let a dead upstream 500 the page
        log.warning("could not list models for prompt manager: %s", exc)
        return []


def _parse_float(value: str) -> float | None:
    value = (value or "").strip()
    return float(value) if value else None


def _parse_int(value: str) -> int | None:
    value = (value or "").strip()
    return int(value) if value else None


# --- List / search -------------------------------------------------------


@router.get("")
def list_prompts(
    request: Request,
    q: str = "",
    show: str = "",
    user: User = Depends(auth_mod.require_user),
):
    conn = _db(request)
    include_archived = show == "all"
    items = prompts_mod.list_for_manager(
        conn, query=q.strip() or None, include_archived=include_archived
    )
    return render(
        request,
        "prompts_list.html",
        items=items,
        q=q,
        include_archived=include_archived,
    )


# --- Create --------------------------------------------------------------


@router.get("/new")
def new_prompt(
    request: Request,
    user: User = Depends(auth_mod.require_user),
    settings: Settings = Depends(get_settings),
):
    return render(
        request,
        "prompt_form.html",
        mode="new",
        prompt=None,
        version=None,
        versions=[],
        models=_available_models(settings),
    )


@router.post("")
def create_prompt(
    request: Request,
    name: str = Form(...),
    description: str = Form(""),
    system: str = Form(""),
    template: str = Form(...),
    reduce_template: str = Form(""),
    model: str = Form(""),
    temperature: str = Form(""),
    max_tokens: str = Form(""),
    official: str = Form(""),
    csrf_token: str = Form(...),
    ctx: auth_mod.AuthContext = Depends(auth_mod.resolve_auth),
):
    user = auth_mod.require_user(ctx)
    auth_mod.verify_csrf(request, csrf_token, ctx)
    conn = _db(request)

    name = name.strip()
    if not name:
        return _redirect("/prompts/new", "err=Name+is+required.")
    # Only admins may create an official (shared, read-only) prompt; everyone
    # else creates a personal prompt they own.
    make_official = bool(official) and user.is_admin
    try:
        prompt = prompts_mod.create_prompt(
            conn,
            name=name,
            description=description.strip() or None,
            system=system.strip() or None,
            template=template,
            reduce_template=reduce_template.strip() or None,
            model=model.strip() or None,
            temperature=_parse_float(temperature),
            max_tokens=_parse_int(max_tokens),
            owner_id=None if make_official else user.id,
            read_only=make_official,
            created_by=user.id,
        )
    except ValueError:
        return _redirect(
            "/prompts/new", "err=The+template+must+contain+the+%7Btranscript%7D+placeholder."
        )
    return _redirect(f"/prompts/{prompt.id}", "msg=Prompt+created.")


# --- Test-run preview ----------------------------------------------------
# Registered before the ``/{prompt_id}`` routes: an ``int`` annotation doesn't
# restrict the URL regex, so ``/prompts/test`` would otherwise be captured by
# ``/prompts/{prompt_id}`` and fail int conversion.


@router.post("/test")
def test_run(
    request: Request,
    system: str = Form(""),
    template: str = Form(...),
    model: str = Form(""),
    temperature: str = Form(""),
    max_tokens: str = Form(""),
    transcript: str = Form(""),
    csrf_token: str = Form(...),
    settings: Settings = Depends(get_settings),
    ctx: auth_mod.AuthContext = Depends(auth_mod.resolve_auth),
):
    """Run the prompt (single-shot) against a transcript snippet and return an
    HTMX fragment with the rendered Markdown — a preview before saving.

    Applies the same "respond only in <language>" override the real pipeline
    would use for this user's meetings, so the preview matches reality."""
    user = auth_mod.require_user(ctx)
    auth_mod.verify_csrf(request, csrf_token, ctx)

    if prompts_mod.TRANSCRIPT_PLACEHOLDER not in template:
        return render(
            request,
            "_prompt_test_result.html",
            error="The template must contain the {transcript} placeholder.",
            rendered=None,
            model_used=None,
        )
    snippet = (transcript or "").strip()
    if not snippet:
        return render(
            request,
            "_prompt_test_result.html",
            error="Paste a transcript snippet to test against.",
            rendered=None,
            model_used=None,
        )
    snippet = snippet[:_TEST_SNIPPET_CHARS]
    model_used = model.strip() or settings.llm_default_model
    user_prompt = template.replace(prompts_mod.TRANSCRIPT_PLACEHOLDER, snippet)
    language_override = user.language if user.language != "en" else None
    effective_system = system_with_language_override(system.strip() or None, language_override)
    try:
        content = LLMClient(settings.llm_base_url).complete(
            model=model_used,
            system=effective_system,
            user=user_prompt,
            temperature=_parse_float(temperature),
            max_tokens=_parse_int(max_tokens),
        )
    except (LLMError, ValueError) as exc:
        return render(
            request,
            "_prompt_test_result.html",
            error=f"Test run failed: {exc}",
            rendered=None,
            model_used=None,
        )
    from markdown_it import MarkdownIt

    md = MarkdownIt("commonmark", {"html": False, "linkify": True, "typographer": True})
    return render(
        request,
        "_prompt_test_result.html",
        error=None,
        rendered=md.render(content),
        model_used=model_used,
    )


# --- Edit ----------------------------------------------------------------


@router.get("/{prompt_id}")
def edit_form(
    request: Request,
    prompt_id: int,
    user: User = Depends(auth_mod.require_user),
    settings: Settings = Depends(get_settings),
):
    conn = _db(request)
    prompt = prompts_mod.get(conn, prompt_id)
    if prompt is None:
        return _redirect("/prompts", "err=Prompt+not+found.")
    return render(
        request,
        "prompt_form.html",
        mode="edit",
        prompt=prompt,
        version=prompts_mod.latest_version(conn, prompt_id),
        versions=prompts_mod.list_versions(conn, prompt_id),
        can_edit=prompts_mod.can_edit(prompt, user),
        models=_available_models(settings),
    )


@router.post("/{prompt_id}")
def save_edit(
    request: Request,
    prompt_id: int,
    name: str = Form(...),
    description: str = Form(""),
    system: str = Form(""),
    template: str = Form(...),
    reduce_template: str = Form(""),
    model: str = Form(""),
    temperature: str = Form(""),
    max_tokens: str = Form(""),
    csrf_token: str = Form(...),
    ctx: auth_mod.AuthContext = Depends(auth_mod.resolve_auth),
):
    user = auth_mod.require_user(ctx)
    auth_mod.verify_csrf(request, csrf_token, ctx)
    conn = _db(request)

    prompt = prompts_mod.get(conn, prompt_id)
    if prompt is None:
        return _redirect("/prompts", "err=Prompt+not+found.")
    if not prompts_mod.can_edit(prompt, user):
        return _redirect(f"/prompts/{prompt_id}", "err=You+cannot+edit+this+prompt.")

    name = name.strip()
    if not name:
        return _redirect(f"/prompts/{prompt_id}", "err=Name+is+required.")
    try:
        bumped = prompts_mod.edit_prompt(
            conn,
            prompt_id,
            name=name,
            description=description.strip() or None,
            system=system.strip() or None,
            template=template,
            reduce_template=reduce_template.strip() or None,
            model=model.strip() or None,
            temperature=_parse_float(temperature),
            max_tokens=_parse_int(max_tokens),
        )
    except ValueError:
        return _redirect(
            f"/prompts/{prompt_id}",
            "err=The+template+must+contain+the+%7Btranscript%7D+placeholder.",
        )
    msg = "Saved+as+a+new+version." if bumped else "Saved."
    return _redirect(f"/prompts/{prompt_id}", f"msg={msg}")


# --- Clone / archive -----------------------------------------------------


@router.post("/{prompt_id}/clone")
def clone(
    request: Request,
    prompt_id: int,
    csrf_token: str = Form(...),
    ctx: auth_mod.AuthContext = Depends(auth_mod.resolve_auth),
):
    user = auth_mod.require_user(ctx)
    auth_mod.verify_csrf(request, csrf_token, ctx)
    conn = _db(request)
    if prompts_mod.get(conn, prompt_id) is None:
        return _redirect("/prompts", "err=Prompt+not+found.")
    clone = prompts_mod.clone_prompt(conn, prompt_id, owner_id=user.id, created_by=user.id)
    return _redirect(f"/prompts/{clone.id}", "msg=Cloned+into+a+personal+copy.")


@router.post("/{prompt_id}/archive")
def archive(
    request: Request,
    prompt_id: int,
    archived: str = Form("1"),
    csrf_token: str = Form(...),
    ctx: auth_mod.AuthContext = Depends(auth_mod.resolve_auth),
):
    user = auth_mod.require_user(ctx)
    auth_mod.verify_csrf(request, csrf_token, ctx)
    conn = _db(request)
    prompt = prompts_mod.get(conn, prompt_id)
    if prompt is None:
        return _redirect("/prompts", "err=Prompt+not+found.")
    if not prompts_mod.can_edit(prompt, user):
        return _redirect("/prompts", "err=You+cannot+archive+this+prompt.")
    make_archived = archived != "0"
    prompts_mod.set_archived(conn, prompt_id, make_archived)
    verb = "archived" if make_archived else "restored"
    return _redirect("/prompts", f"msg=Prompt+{verb}.")
