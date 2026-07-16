"""User CRUD — used by the admin pages, the profile page, and admin bootstrap."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from . import security
from .i18n import DEFAULT_LANGUAGE, SUPPORTED_LANGUAGES

_USER_COLUMNS = "id, username, role, active, created_at, language, email"

# Sentinel password hash for SSO-provisioned accounts. It is not a valid
# argon2 encoding, so ``security.verify_password`` always returns False —
# such accounts can never be signed into with a local password.
SSO_NO_PASSWORD = "!"


class UsernameTaken(Exception):
    """Raised when creating/renaming to a username that already exists."""


@dataclass(frozen=True)
class User:
    id: int
    username: str
    role: str
    active: bool
    created_at: str
    language: str = DEFAULT_LANGUAGE
    email: str | None = None

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"


def _row_to_user(row: sqlite3.Row) -> User:
    return User(
        id=row["id"],
        username=row["username"],
        role=row["role"],
        active=bool(row["active"]),
        created_at=row["created_at"],
        language=row["language"],
        email=row["email"] if "email" in row.keys() else None,
    )


def get_by_id(conn: sqlite3.Connection, user_id: int) -> User | None:
    row = conn.execute(
        f"SELECT {_USER_COLUMNS} FROM users WHERE id = ?",
        (user_id,),
    ).fetchone()
    return _row_to_user(row) if row else None


def get_by_username(conn: sqlite3.Connection, username: str) -> User | None:
    row = conn.execute(
        f"SELECT {_USER_COLUMNS} FROM users WHERE username = ?",
        (username,),
    ).fetchone()
    return _row_to_user(row) if row else None


def get_by_email(conn: sqlite3.Connection, email: str) -> User | None:
    row = conn.execute(
        f"SELECT {_USER_COLUMNS} FROM users WHERE email = ?",
        (email.strip().lower(),),
    ).fetchone()
    return _row_to_user(row) if row else None


def get_by_oidc_subject(conn: sqlite3.Connection, subject: str) -> User | None:
    row = conn.execute(
        f"SELECT {_USER_COLUMNS} FROM users WHERE oidc_subject = ?",
        (subject,),
    ).fetchone()
    return _row_to_user(row) if row else None


def get_password_hash(conn: sqlite3.Connection, user_id: int) -> str | None:
    row = conn.execute(
        "SELECT password_hash FROM users WHERE id = ?", (user_id,)
    ).fetchone()
    return row["password_hash"] if row else None


def list_all(conn: sqlite3.Connection) -> list[User]:
    rows = conn.execute(
        f"SELECT {_USER_COLUMNS} FROM users ORDER BY username COLLATE NOCASE"
    ).fetchall()
    return [_row_to_user(r) for r in rows]


def count(conn: sqlite3.Connection) -> int:
    return conn.execute("SELECT COUNT(*) AS c FROM users").fetchone()["c"]


def create(
    conn: sqlite3.Connection,
    username: str,
    password: str,
    role: str = "member",
) -> User:
    username = username.strip()
    if not username:
        raise ValueError("username is required")
    if role not in ("admin", "member"):
        raise ValueError(f"invalid role: {role}")
    try:
        cur = conn.execute(
            "INSERT INTO users (username, password_hash, role) VALUES (?, ?, ?)",
            (username, security.hash_password(password), role),
        )
        conn.commit()
    except sqlite3.IntegrityError as exc:
        conn.rollback()
        raise UsernameTaken(username) from exc
    user = get_by_id(conn, cur.lastrowid)
    assert user is not None
    return user


def set_password(conn: sqlite3.Connection, user_id: int, password: str) -> None:
    conn.execute(
        "UPDATE users SET password_hash = ? WHERE id = ?",
        (security.hash_password(password), user_id),
    )
    conn.commit()


def set_active(conn: sqlite3.Connection, user_id: int, active: bool) -> None:
    conn.execute(
        "UPDATE users SET active = ? WHERE id = ?", (1 if active else 0, user_id)
    )
    conn.commit()


def set_language(conn: sqlite3.Connection, user_id: int, language: str) -> None:
    if language not in SUPPORTED_LANGUAGES:
        raise ValueError(f"unsupported language: {language}")
    conn.execute("UPDATE users SET language = ? WHERE id = ?", (language, user_id))
    conn.commit()


# --- SSO (Entra ID / OIDC) provisioning -------------------------------------


def provision_sso(
    conn: sqlite3.Connection,
    *,
    subject: str,
    email: str,
    role: str = "member",
) -> User:
    """Create a knowts account for an Entra identity on first SSO login.

    The username is the email; the password hash is a sentinel that can never
    verify, so the account is SSO-only unless an admin sets a local password.
    """
    email = email.strip().lower()
    if role not in ("admin", "member"):
        raise ValueError(f"invalid role: {role}")
    try:
        cur = conn.execute(
            "INSERT INTO users (username, password_hash, role, email, oidc_subject) "
            "VALUES (?, ?, ?, ?, ?)",
            (email, SSO_NO_PASSWORD, role, email, subject),
        )
        conn.commit()
    except sqlite3.IntegrityError as exc:
        conn.rollback()
        raise UsernameTaken(email) from exc
    user = get_by_id(conn, cur.lastrowid)
    assert user is not None
    return user


def link_oidc_subject(
    conn: sqlite3.Connection,
    user_id: int,
    subject: str,
    email: str | None = None,
) -> None:
    """Attach an Entra ``oid`` (and optionally email) to a pre-existing account
    so a local user can subsequently sign in via SSO as the same person."""
    if email is not None:
        conn.execute(
            "UPDATE users SET oidc_subject = ?, email = ? WHERE id = ?",
            (subject, email.strip().lower(), user_id),
        )
    else:
        conn.execute(
            "UPDATE users SET oidc_subject = ? WHERE id = ?", (subject, user_id)
        )
    conn.commit()


def sync_sso_profile(
    conn: sqlite3.Connection,
    user_id: int,
    *,
    email: str,
    promote_admin: bool,
) -> None:
    """Refresh the stored email on each SSO login and, when the identity is in
    the admin allowlist, promote to admin. Never auto-demotes — an admin
    removed from the allowlist is demoted deliberately via the admin UI."""
    conn.execute(
        "UPDATE users SET email = ? WHERE id = ?", (email.strip().lower(), user_id)
    )
    if promote_admin:
        conn.execute(
            "UPDATE users SET role = 'admin' WHERE id = ? AND role <> 'admin'",
            (user_id,),
        )
    conn.commit()


def verify_credentials(
    conn: sqlite3.Connection, username: str, password: str
) -> User | None:
    """Return the user iff the username exists, is active, and the password
    matches. Always runs a hash verification (even for unknown users) to avoid
    leaking account existence through response timing."""
    row = conn.execute(
        f"SELECT {_USER_COLUMNS}, password_hash FROM users WHERE username = ?",
        (username,),
    ).fetchone()

    if row is None:
        # Spend comparable time so missing vs. wrong-password are indistinguishable.
        security.verify_password(
            "$argon2id$v=19$m=65536,t=3,p=4$"
            "c29tZXNhbHRzb21lc2FsdA$c29tZWhhc2hzb21laGFzaHNvbWVoYXNo",
            password,
        )
        return None

    if not security.verify_password(row["password_hash"], password):
        return None
    if not row["active"]:
        return None

    if security.needs_rehash(row["password_hash"]):
        conn.execute(
            "UPDATE users SET password_hash = ? WHERE id = ?",
            (security.hash_password(password), row["id"]),
        )
        conn.commit()

    return _row_to_user(row)
