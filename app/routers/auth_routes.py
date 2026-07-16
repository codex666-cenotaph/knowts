"""Login and logout."""

from __future__ import annotations

import sqlite3

from fastapi import APIRouter, Depends, Form, HTTPException, Request, status
from fastapi.responses import RedirectResponse

from .. import auth as auth_mod
from .. import oidc as oidc_mod
from .. import users as users_mod
from ..config import Settings, get_settings
from ..templating import render

router = APIRouter()


def _db(request: Request) -> sqlite3.Connection:
    return request.app.state.db


def _login_context(request: Request, settings: Settings) -> dict:
    """Shared login-page context: which auth methods to surface.

    The local password form is shown when local login is enabled, when SSO is
    not configured (never lock everyone out), or when explicitly requested via
    ``?local=1`` (break-glass for the bootstrap admin)."""
    force_local = request.query_params.get("local") == "1"
    show_local = settings.local_login_enabled or not settings.oidc_configured or force_local
    return {
        "oidc_enabled": settings.oidc_configured,
        "oidc_button_label": settings.oidc_button_label,
        "show_local": show_local,
    }


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
    settings: Settings = Depends(get_settings),
    auth: auth_mod.AuthContext = Depends(auth_mod.resolve_auth),
):
    if auth.user is not None:
        return RedirectResponse(_safe_next(next), status_code=status.HTTP_303_SEE_OTHER)
    return render(
        request, "login.html", next=_safe_next(next), **_login_context(request, settings)
    )


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
            **_login_context(request, settings),
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
            **_login_context(request, settings),
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


# --- Microsoft Entra ID (OIDC) SSO ------------------------------------------


def _sso_error(request: Request, settings: Settings, message: str):
    return render(
        request,
        "login.html",
        status_code=status.HTTP_401_UNAUTHORIZED,
        next="/",
        error=message,
        **_login_context(request, settings),
    )


@router.get("/auth/sso/login")
async def sso_login(
    request: Request,
    next: str = "/",
    settings: Settings = Depends(get_settings),
):
    if not settings.oidc_configured:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    oauth = request.app.state.oauth
    # Remember where to land after the round trip (same-site only).
    request.session["sso_next"] = _safe_next(next)
    redirect_uri = settings.oidc_redirect_url or str(request.url_for("sso_callback"))
    client = oauth.create_client(oidc_mod.CLIENT_NAME)
    return await client.authorize_redirect(request, redirect_uri)


@router.get("/auth/sso/callback", name="sso_callback")
async def sso_callback(
    request: Request,
    settings: Settings = Depends(get_settings),
    _auth: auth_mod.AuthContext = Depends(auth_mod.resolve_auth),
):
    if not settings.oidc_configured:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    oauth = request.app.state.oauth
    client = oauth.create_client(oidc_mod.CLIENT_NAME)
    try:
        token = await client.authorize_access_token(request)
    except Exception:
        # Covers user cancellation, state/nonce mismatch, and token errors.
        return _sso_error(
            request, settings, "Microsoft sign-in failed or was cancelled. Please try again."
        )

    identity = oidc_mod.identity_from_claims(token.get("userinfo") or {})
    if identity is None:
        return _sso_error(
            request, settings, "Microsoft did not return an email for your account."
        )
    if not oidc_mod.is_email_allowed(settings, identity):
        return _sso_error(
            request, settings, "Your account is not permitted to use this application."
        )

    conn = _db(request)
    try:
        user = oidc_mod.resolve_user(conn, settings, identity)
    except users_mod.UsernameTaken:
        return _sso_error(
            request,
            settings,
            "An account with your email already exists under a different login. "
            "Ask an administrator to reconcile it.",
        )
    if not user.active:
        return _sso_error(
            request, settings, "Your account has been deactivated. Contact an administrator."
        )

    next_url = _safe_next(request.session.pop("sso_next", "/"))
    cookie_value = auth_mod.create_session(conn, settings, user.id)
    response = RedirectResponse(next_url, status_code=status.HTTP_303_SEE_OTHER)
    auth_mod.set_session_cookie(response, settings, cookie_value)
    return response
