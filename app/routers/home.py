"""Home / dashboard. Empty in Phase 1 — proves the login-protected UI serves."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request

from ..auth import require_user
from ..templating import render
from ..users import User

router = APIRouter()


@router.get("/")
def home(request: Request, user: User = Depends(require_user)):
    return render(request, "home.html")
