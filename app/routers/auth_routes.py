"""Login and logout."""

from __future__ import annotations

import sqlite3

from fastapi import APIRouter, Depends, Form, Request, status
from fastapi.responses import RedirectResponse

from .. import auth as auth_mod
from .. import users as users_mod
from ..config import Settings, get_settings
from ..templating import render

router = APIRouter()


def _db(request: Request) -> sqlite3.Connection:
    return request.app.state.db


def _client_ip(request: Request) -> str:
    # Honour a single proxy hop if present; fall back to the socket peer.
    fwd = request.headers.get("x-forwarded-for")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def _safe_next(raw: str | None) -> str:
    """Only allow same-site relative redirect targets."""
    if raw and raw.startswith("/") and not raw.startswith("//"):
        return raw
    return "/"


@router.get("/login")
def login_form(
    request: Request,
    next: str = "/",
    auth: auth_mod.AuthContext = Depends(auth_mod.resolve_auth),
):
    if auth.user is not None:
        return RedirectResponse(_safe_next(next), status_code=status.HTTP_303_SEE_OTHER)
    return render(request, "login.html", next=_safe_next(next))


@router.post("/login")
def login_submit(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    next: str = Form("/"),
    settings: Settings = Depends(get_settings),
    _auth: auth_mod.AuthContext = Depends(auth_mod.resolve_auth),
):
    conn = _db(request)
    limiter = request.app.state.login_limiter
    username = username.strip()
    ip_key = f"ip:{_client_ip(request)}"
    user_key = f"user:{username.lower()}"

    locked = limiter.check(ip_key, user_key)
    if locked:
        mins = (locked + 59) // 60
        return render(
            request,
            "login.html",
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            next=_safe_next(next),
            error=f"Too many attempts. Try again in about {mins} minute(s).",
            username=username,
        )

    user = users_mod.verify_credentials(conn, username, password)
    if user is None:
        limiter.record_failure(ip_key, user_key)
        return render(
            request,
            "login.html",
            status_code=status.HTTP_401_UNAUTHORIZED,
            next=_safe_next(next),
            error="Invalid username or password.",
            username=username,
        )

    limiter.reset(ip_key, user_key)
    cookie_value = auth_mod.create_session(conn, settings, user.id)
    response = RedirectResponse(_safe_next(next), status_code=status.HTTP_303_SEE_OTHER)
    auth_mod.set_session_cookie(response, settings, cookie_value)
    return response


@router.post("/logout")
def logout(
    request: Request,
    csrf_token: str = Form(...),
    settings: Settings = Depends(get_settings),
    ctx: auth_mod.AuthContext = Depends(auth_mod.resolve_auth),
):
    if ctx.user is not None:
        auth_mod.verify_csrf(request, csrf_token, ctx)
        cookie = request.cookies.get(settings.session_cookie_name)
        if cookie:
            auth_mod.logout(_db(request), settings, cookie)
    response = RedirectResponse("/login?msg=Signed+out.", status_code=status.HTTP_303_SEE_OTHER)
    auth_mod.clear_session_cookie(response, settings)
    return response
