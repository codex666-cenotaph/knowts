"""SQLite access and schema migrations.

A tiny ordered-migration runner keeps the schema versioned and repeatable
without pulling in a heavier ORM/migration framework. Migrations are plain SQL
strings applied once each, tracked in ``schema_migrations``.

The full data model from PLAN.md §7 is created up front so later phases add
features, not tables. Phase 1 only reads/writes ``users`` and ``sessions``.
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

# Ordered list of (version, SQL). Never edit an applied migration in place —
# append a new one. Each script may contain multiple statements.
MIGRATIONS: list[tuple[int, str]] = [
    (
        1,
        """
        CREATE TABLE users (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            username      TEXT NOT NULL UNIQUE,
            password_hash TEXT NOT NULL,
            role          TEXT NOT NULL DEFAULT 'member'
                              CHECK (role IN ('admin', 'member')),
            active        INTEGER NOT NULL DEFAULT 1,
            created_at    TEXT NOT NULL DEFAULT (datetime('now'))
        );

        CREATE TABLE sessions (
            id            TEXT PRIMARY KEY,          -- sha256 of the cookie token
            user_id       INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            csrf_token    TEXT NOT NULL,
            created_at    TEXT NOT NULL DEFAULT (datetime('now')),
            expires_at    TEXT NOT NULL              -- idle expiry, refreshed on use
        );
        CREATE INDEX idx_sessions_user ON sessions(user_id);
        """,
    ),
    (
        2,
        """
        CREATE TABLE meetings (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id       INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            title         TEXT NOT NULL,
            meeting_date  TEXT,
            filename      TEXT,
            duration_s    REAL,
            status        TEXT NOT NULL DEFAULT 'processing',
            created_at    TEXT NOT NULL DEFAULT (datetime('now'))
        );
        CREATE INDEX idx_meetings_user ON meetings(user_id);

        CREATE TABLE jobs (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            meeting_id    INTEGER NOT NULL REFERENCES meetings(id) ON DELETE CASCADE,
            kind          TEXT NOT NULL CHECK (kind IN ('transcribe', 'notes')),
            status        TEXT NOT NULL DEFAULT 'queued'
                              CHECK (status IN ('queued', 'running', 'done', 'error')),
            step          TEXT,
            error         TEXT,
            started_at    TEXT,
            finished_at   TEXT
        );
        CREATE INDEX idx_jobs_meeting ON jobs(meeting_id);

        CREATE TABLE transcripts (
            meeting_id    INTEGER PRIMARY KEY REFERENCES meetings(id) ON DELETE CASCADE,
            text          TEXT NOT NULL,
            segments_json TEXT,
            language      TEXT,
            created_at    TEXT NOT NULL DEFAULT (datetime('now'))
        );

        CREATE TABLE prompts (
            id                     INTEGER PRIMARY KEY AUTOINCREMENT,
            name                   TEXT NOT NULL,
            description            TEXT,
            owner_id               INTEGER REFERENCES users(id) ON DELETE SET NULL,
            read_only              INTEGER NOT NULL DEFAULT 0,
            cloned_from_version_id INTEGER REFERENCES prompt_versions(id),
            archived               INTEGER NOT NULL DEFAULT 0,
            created_by             INTEGER REFERENCES users(id) ON DELETE SET NULL,
            created_at             TEXT NOT NULL DEFAULT (datetime('now'))
        );

        CREATE TABLE prompt_versions (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            prompt_id       INTEGER NOT NULL REFERENCES prompts(id) ON DELETE CASCADE,
            version         INTEGER NOT NULL,
            system          TEXT,
            template        TEXT NOT NULL,
            reduce_template TEXT,
            model           TEXT,
            temperature     REAL,
            max_tokens      INTEGER,
            created_at      TEXT NOT NULL DEFAULT (datetime('now')),
            UNIQUE (prompt_id, version)
        );

        CREATE TABLE notes (
            id                INTEGER PRIMARY KEY AUTOINCREMENT,
            meeting_id        INTEGER NOT NULL REFERENCES meetings(id) ON DELETE CASCADE,
            prompt_version_id INTEGER REFERENCES prompt_versions(id),
            model_used        TEXT,
            markdown          TEXT NOT NULL,
            created_at        TEXT NOT NULL DEFAULT (datetime('now'))
        );
        CREATE INDEX idx_notes_meeting ON notes(meeting_id);
        """,
    ),
    (
        3,
        """
        ALTER TABLE users ADD COLUMN language TEXT NOT NULL DEFAULT 'en';
        """,
    ),
    (
        4,
        """
        ALTER TABLE meetings ADD COLUMN language TEXT NOT NULL DEFAULT 'auto';
        """,
    ),
    (
        5,
        """
        ALTER TABLE meetings ADD COLUMN diarization_num_speakers INTEGER NOT NULL DEFAULT 0;
        """,
    ),
]


def connect(db_path: Path) -> sqlite3.Connection:
    """Open a connection with sane defaults (WAL, foreign keys, row access)."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL;")
    conn.execute("PRAGMA foreign_keys = ON;")
    conn.execute("PRAGMA busy_timeout = 5000;")
    return conn


def _current_version(conn: sqlite3.Connection) -> int:
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_migrations ("
        " version INTEGER PRIMARY KEY,"
        " applied_at TEXT NOT NULL DEFAULT (datetime('now')))"
    )
    row = conn.execute("SELECT MAX(version) AS v FROM schema_migrations").fetchone()
    return row["v"] or 0


def run_migrations(conn: sqlite3.Connection) -> int:
    """Apply any pending migrations in order. Returns the resulting version."""
    current = _current_version(conn)
    for version, script in sorted(MIGRATIONS):
        if version <= current:
            continue
        with conn:  # transaction per migration
            conn.executescript(script)
            conn.execute(
                "INSERT INTO schema_migrations (version) VALUES (?)", (version,)
            )
        current = version
    return current


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """Context manager that commits on success and rolls back on error."""
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
