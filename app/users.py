"""User CRUD — used by the admin pages, the profile page, and admin bootstrap."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from . import security
from .i18n import DEFAULT_LANGUAGE, SUPPORTED_LANGUAGES

_USER_COLUMNS = "id, username, role, active, created_at, language"


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
