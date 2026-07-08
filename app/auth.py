"""Session lifecycle, request auth dependencies, and CSRF protection.

Sessions are server-side rows keyed by the SHA-256 of a random token. The raw
token travels in a signed, HTTP-only cookie (``itsdangerous`` signature guards
against tampering; the server-side row enables revocation and idle/absolute
expiry). Each session carries a per-session CSRF token that mutating forms must
echo back.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

from fastapi import Depends, HTTPException, Request, status
from fastapi.responses import Response
from itsdangerous import BadSignature, TimestampSigner

from . import security
from .config import Settings, get_settings
from .users import User, get_by_id

_SIGNER_SALT = "knowts.session.v1"


def _signer(settings: Settings) -> TimestampSigner:
    return TimestampSigner(settings.secret_key, salt=_SIGNER_SALT)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse(ts: str) -> datetime:
    dt = datetime.fromisoformat(ts)
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


# --- Session store -------------------------------------------------------


def create_session(conn: sqlite3.Connection, settings: Settings, user_id: int) -> str:
    """Create a session row and return the *signed* cookie value to set."""
    token = security.new_token()
    session_id = security.hash_token(token)
    csrf_token = security.new_token()
    expires_at = _now() + timedelta(seconds=settings.session_idle_seconds)
    conn.execute(
        "INSERT INTO sessions (id, user_id, csrf_token, expires_at) "
        "VALUES (?, ?, ?, ?)",
        (session_id, user_id, csrf_token, expires_at.isoformat()),
    )
    conn.commit()
    return _signer(settings).sign(token).decode("ascii")


def _load_session(
    conn: sqlite3.Connection, settings: Settings, signed_value: str
) -> sqlite3.Row | None:
    try:
        token = _signer(settings).unsign(signed_value).decode("ascii")
    except BadSignature:
        return None

    session_id = security.hash_token(token)
    row = conn.execute(
        "SELECT id, user_id, csrf_token, created_at, expires_at "
        "FROM sessions WHERE id = ?",
        (session_id,),
    ).fetchone()
    if row is None:
        return None

    now = _now()
    if now >= _parse(row["expires_at"]):
        delete_session_by_id(conn, session_id)
        return None
    if now >= _parse(row["created_at"]) + timedelta(
        seconds=settings.session_absolute_seconds
    ):
        delete_session_by_id(conn, session_id)
        return None

    # Refresh idle expiry (sliding window).
    new_expiry = now + timedelta(seconds=settings.session_idle_seconds)
    conn.execute(
        "UPDATE sessions SET expires_at = ? WHERE id = ?",
        (new_expiry.isoformat(), session_id),
    )
    conn.commit()
    return row


def delete_session_by_id(conn: sqlite3.Connection, session_id: str) -> None:
    conn.execute("DELETE FROM sessions WHERE id = ?", (session_id,))
    conn.commit()


def logout(conn: sqlite3.Connection, settings: Settings, signed_value: str) -> None:
    try:
        token = _signer(settings).unsign(signed_value).decode("ascii")
    except BadSignature:
        return
    delete_session_by_id(conn, security.hash_token(token))


def set_session_cookie(response: Response, settings: Settings, value: str) -> None:
    response.set_cookie(
        settings.session_cookie_name,
        value,
        max_age=settings.session_absolute_seconds,
        httponly=True,
        secure=settings.cookie_secure,
        samesite="lax",
        path="/",
    )


def clear_session_cookie(response: Response, settings: Settings) -> None:
    response.delete_cookie(settings.session_cookie_name, path="/")


# --- Request-scoped state ------------------------------------------------


class AuthContext:
    """Resolved auth info attached to a request by the dependencies below."""

    def __init__(self, user: User | None, csrf_token: str | None):
        self.user = user
        self.csrf_token = csrf_token


def _db(request: Request) -> sqlite3.Connection:
    return request.app.state.db


def resolve_auth(
    request: Request,
    settings: Settings = Depends(get_settings),
) -> AuthContext:
    """Populate ``request.state.auth`` from the session cookie (if any).

    Never raises — anonymous requests resolve to an empty context. Route guards
    (``require_user`` / ``require_admin``) enforce access.
    """
    cookie = request.cookies.get(settings.session_cookie_name)
    ctx = AuthContext(None, None)
    if cookie:
        conn = _db(request)
        row = _load_session(conn, settings, cookie)
        if row is not None:
            user = get_by_id(conn, row["user_id"])
            if user is not None and user.active:
                ctx = AuthContext(user, row["csrf_token"])
    request.state.auth = ctx
    return ctx


def require_user(ctx: AuthContext = Depends(resolve_auth)) -> User:
    if ctx.user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="login required",
            headers={"Location": "/login"},
        )
    return ctx.user


def require_admin(user: User = Depends(require_user)) -> User:
    if not user.is_admin:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="admin only")
    return user


def verify_csrf(
    request: Request,
    submitted: str | None,
    ctx: AuthContext,
) -> None:
    """Raise 403 unless ``submitted`` matches the session's CSRF token."""
    if ctx.csrf_token is None or not submitted:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="missing CSRF token")
    if not security.tokens_equal(ctx.csrf_token, submitted):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="invalid CSRF token")
