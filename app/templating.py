"""Jinja2 setup and a small ``render`` helper.

Every response gets ``user`` and ``csrf_token`` from the resolved auth context
so templates never have to thread them through by hand. Flash messages are
passed via ``?msg=`` / ``?err=`` query params on redirects (no session storage
needed for a single-container app).
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from fastapi import Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from . import i18n
from .auth import AuthContext

_TEMPLATE_DIR = Path(__file__).parent / "templates"
_STATIC_DIR = Path(__file__).parent / "static"
templates = Jinja2Templates(directory=str(_TEMPLATE_DIR))

_asset_versions: dict[str, tuple[int, str]] = {}


def static_url(path: str) -> str:
    """``/static/<path>`` with a short content-hash query string so browsers
    fetch the new file after a deploy instead of serving a stale cached copy.
    The hash is recomputed only when the file's mtime changes."""
    file = _STATIC_DIR / path
    try:
        mtime = file.stat().st_mtime_ns
    except OSError:
        return f"/static/{path}"
    cached = _asset_versions.get(path)
    if cached is None or cached[0] != mtime:
        digest = hashlib.md5(file.read_bytes()).hexdigest()[:8]
        cached = (mtime, digest)
        _asset_versions[path] = cached
    return f"/static/{path}?v={cached[1]}"


# Exposed to every template so asset links can be cache-busted.
templates.env.globals["static_url"] = static_url


def render(
    request: Request,
    name: str,
    *,
    status_code: int = 200,
    **context: object,
) -> HTMLResponse:
    auth: AuthContext | None = getattr(request.state, "auth", None)
    lang = auth.user.language if auth and auth.user else i18n.DEFAULT_LANGUAGE
    base = {
        "user": auth.user if auth else None,
        "csrf_token": auth.csrf_token if auth else None,
        "flash_msg": request.query_params.get("msg"),
        "flash_err": request.query_params.get("err"),
        "lang": lang,
        "t": lambda key: i18n.t(key, lang),
    }
    base.update(context)
    return templates.TemplateResponse(request, name, base, status_code=status_code)
