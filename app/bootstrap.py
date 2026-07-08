"""First-run admin bootstrap.

If ``ADMIN_USER`` / ``ADMIN_PASSWORD`` are set and no such user exists yet, an
admin account is created on startup. If they are unset and there are no users
at all, the app logs a warning and the login page shows a one-time setup prompt
(PLAN.md §3).
"""

from __future__ import annotations

import logging
import sqlite3

from . import users as users_mod
from .config import Settings

log = logging.getLogger("knowts.bootstrap")


def bootstrap_admin(conn: sqlite3.Connection, settings: Settings) -> None:
    if not settings.admin_user or not settings.admin_password:
        if users_mod.count(conn) == 0:
            log.warning(
                "No users exist and ADMIN_USER/ADMIN_PASSWORD are unset. "
                "Set them and restart to create the first admin."
            )
        return

    existing = users_mod.get_by_username(conn, settings.admin_user)
    if existing is not None:
        # Make sure the bootstrap account stays an active admin.
        if not existing.is_admin or not existing.active:
            conn.execute(
                "UPDATE users SET role = 'admin', active = 1 WHERE id = ?",
                (existing.id,),
            )
            conn.commit()
            log.info("Ensured bootstrap user '%s' is an active admin.", settings.admin_user)
        return

    users_mod.create(conn, settings.admin_user, settings.admin_password, role="admin")
    log.info("Created bootstrap admin user '%s'.", settings.admin_user)


def needs_setup(conn: sqlite3.Connection) -> bool:
    """True when there are no users at all (drives the login setup hint)."""
    return users_mod.count(conn) == 0
