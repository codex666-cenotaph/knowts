"""Home / upload page (PLAN.md §8.2)."""

from __future__ import annotations

import sqlite3

from fastapi import APIRouter, Depends, Request

from .. import meetings as meetings_mod
from .. import prompts as prompts_mod
from ..auth import require_user
from ..i18n import SUPPORTED_LANGUAGES
from ..templating import render
from ..users import User

router = APIRouter()


@router.get("/")
def home(request: Request, user: User = Depends(require_user)):
    conn: sqlite3.Connection = request.app.state.db
    recent = meetings_mod.list_for_user(conn, user)[:5]
    return render(
        request,
        "home.html",
        prompts=prompts_mod.list_active(conn),
        recent=recent,
        languages=SUPPORTED_LANGUAGES,
    )
