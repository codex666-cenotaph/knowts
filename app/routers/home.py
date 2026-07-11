"""Landing redirect + the upload page (PLAN.md §8.2).

The meetings dashboard (``/meetings``) is the app's landing page; ``/`` just
redirects there. The upload form lives on its own page at ``/upload``.
"""

from __future__ import annotations

import sqlite3

from fastapi import APIRouter, Depends, Request, status
from fastapi.responses import RedirectResponse

from .. import meetings as meetings_mod
from .. import prompts as prompts_mod
from ..auth import require_user
from ..config import get_settings
from ..i18n import SUPPORTED_LANGUAGES
from ..templating import render
from ..users import User

router = APIRouter()


@router.get("/")
def index(_user: User = Depends(require_user)) -> RedirectResponse:
    """Land on the meetings dashboard, not the upload form."""
    return RedirectResponse("/meetings", status_code=status.HTTP_303_SEE_OTHER)


@router.get("/upload")
def upload_page(request: Request, user: User = Depends(require_user)):
    conn: sqlite3.Connection = request.app.state.db
    recent = meetings_mod.list_for_user(conn, user)[:5]
    return render(
        request,
        "home.html",
        prompts=prompts_mod.list_active(conn),
        recent=recent,
        languages=SUPPORTED_LANGUAGES,
        diarization_enabled=get_settings().diarization_enabled,
    )
