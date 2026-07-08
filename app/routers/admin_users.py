"""Admin → Users: create, deactivate/reactivate, reset password."""

from __future__ import annotations

import sqlite3

from fastapi import APIRouter, Depends, Form, Request, status
from fastapi.responses import RedirectResponse

from .. import auth as auth_mod
from .. import users as users_mod
from ..templating import render
from ..users import User

router = APIRouter(prefix="/admin/users")

MIN_PASSWORD_LEN = 8


def _db(request: Request) -> sqlite3.Connection:
    return request.app.state.db


def _redirect(query: str = "") -> RedirectResponse:
    suffix = f"?{query}" if query else ""
    return RedirectResponse(
        f"/admin/users{suffix}", status_code=status.HTTP_303_SEE_OTHER
    )


@router.get("")
def list_users(request: Request, admin: User = Depends(auth_mod.require_admin)):
    all_users = users_mod.list_all(_db(request))
    return render(request, "admin_users.html", users=all_users)


@router.post("/create")
def create_user(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    role: str = Form("member"),
    csrf_token: str = Form(...),
    ctx: auth_mod.AuthContext = Depends(auth_mod.resolve_auth),
):
    auth_mod.require_admin(auth_mod.require_user(ctx))
    auth_mod.verify_csrf(request, csrf_token, ctx)
    conn = _db(request)

    username = username.strip()
    if not username:
        return _redirect("err=Username+is+required.")
    if len(password) < MIN_PASSWORD_LEN:
        return _redirect(f"err=Password+must+be+at+least+{MIN_PASSWORD_LEN}+characters.")
    if role not in ("admin", "member"):
        return _redirect("err=Invalid+role.")
    try:
        users_mod.create(conn, username, password, role)
    except users_mod.UsernameTaken:
        return _redirect(f"err=Username+'{username}'+is+already+taken.")
    return _redirect(f"msg=Created+user+'{username}'.")


@router.post("/{user_id}/active")
def set_active(
    request: Request,
    user_id: int,
    active: str = Form(...),
    csrf_token: str = Form(...),
    ctx: auth_mod.AuthContext = Depends(auth_mod.resolve_auth),
):
    admin = auth_mod.require_admin(auth_mod.require_user(ctx))
    auth_mod.verify_csrf(request, csrf_token, ctx)
    conn = _db(request)

    if user_id == admin.id:
        return _redirect("err=You+cannot+deactivate+your+own+account.")
    target = users_mod.get_by_id(conn, user_id)
    if target is None:
        return _redirect("err=User+not+found.")

    make_active = active == "1"
    users_mod.set_active(conn, user_id, make_active)
    verb = "reactivated" if make_active else "deactivated"
    return _redirect(f"msg=User+'{target.username}'+{verb}.")


@router.post("/{user_id}/password")
def reset_password(
    request: Request,
    user_id: int,
    password: str = Form(...),
    csrf_token: str = Form(...),
    ctx: auth_mod.AuthContext = Depends(auth_mod.resolve_auth),
):
    auth_mod.require_admin(auth_mod.require_user(ctx))
    auth_mod.verify_csrf(request, csrf_token, ctx)
    conn = _db(request)

    target = users_mod.get_by_id(conn, user_id)
    if target is None:
        return _redirect("err=User+not+found.")
    if len(password) < MIN_PASSWORD_LEN:
        return _redirect(f"err=Password+must+be+at+least+{MIN_PASSWORD_LEN}+characters.")

    users_mod.set_password(conn, user_id, password)
    return _redirect(f"msg=Password+reset+for+'{target.username}'.")
