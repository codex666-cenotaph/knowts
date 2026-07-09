"""Profile page — change your own password."""

from __future__ import annotations

import sqlite3

from fastapi import APIRouter, Depends, Form, Request, status
from fastapi.responses import RedirectResponse

from .. import auth as auth_mod
from .. import security
from .. import users as users_mod
from ..i18n import SUPPORTED_LANGUAGES
from ..templating import render
from ..users import User

router = APIRouter()

MIN_PASSWORD_LEN = 8


def _db(request: Request) -> sqlite3.Connection:
    return request.app.state.db


@router.get("/profile")
def profile(request: Request, user: User = Depends(auth_mod.require_user)):
    return render(request, "profile.html", languages=SUPPORTED_LANGUAGES)


@router.post("/profile/language")
def change_language(
    request: Request,
    language: str = Form(...),
    csrf_token: str = Form(...),
    ctx: auth_mod.AuthContext = Depends(auth_mod.resolve_auth),
):
    user = auth_mod.require_user(ctx)
    auth_mod.verify_csrf(request, csrf_token, ctx)
    conn = _db(request)

    if language not in SUPPORTED_LANGUAGES:
        return RedirectResponse(
            "/profile?err=Unsupported+language.", status_code=status.HTTP_303_SEE_OTHER
        )
    users_mod.set_language(conn, user.id, language)
    return RedirectResponse(
        "/profile?msg=Language+updated.", status_code=status.HTTP_303_SEE_OTHER
    )


@router.post("/profile/password")
def change_password(
    request: Request,
    current_password: str = Form(...),
    new_password: str = Form(...),
    confirm_password: str = Form(...),
    csrf_token: str = Form(...),
    ctx: auth_mod.AuthContext = Depends(auth_mod.resolve_auth),
):
    user = auth_mod.require_user(ctx)
    auth_mod.verify_csrf(request, csrf_token, ctx)
    conn = _db(request)

    def fail(message: str) -> RedirectResponse:
        return RedirectResponse(
            f"/profile?err={message}", status_code=status.HTTP_303_SEE_OTHER
        )

    stored = users_mod.get_password_hash(conn, user.id)
    if stored is None or not security.verify_password(stored, current_password):
        return fail("Current+password+is+incorrect.")
    if len(new_password) < MIN_PASSWORD_LEN:
        return fail(f"New+password+must+be+at+least+{MIN_PASSWORD_LEN}+characters.")
    if new_password != confirm_password:
        return fail("New+passwords+do+not+match.")

    users_mod.set_password(conn, user.id, new_password)
    return RedirectResponse(
        "/profile?msg=Password+updated.", status_code=status.HTTP_303_SEE_OTHER
    )
