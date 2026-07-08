"""Meeting, transcript, and note storage (PLAN.md §7, §10 step 6).

The meeting is the central object: it permanently groups the original upload,
its transcript, and every set of generated notes. Ownership is enforced here
via ``get_owned`` (members see only their own meetings; admins see all).
"""

from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass

from fastapi import HTTPException, status

from .users import User

log = logging.getLogger("knowts.meetings")

# Meeting-level status (free-form; the per-job table has the granular state).
STATUS_PROCESSING = "processing"
STATUS_TRANSCRIBING = "transcribing"
STATUS_GENERATING = "generating"
STATUS_DONE = "done"
STATUS_ERROR = "error"


@dataclass(frozen=True)
class Meeting:
    id: int
    user_id: int
    title: str
    meeting_date: str | None
    filename: str | None
    duration_s: float | None
    status: str
    created_at: str
    # Transcription/notes language chosen at upload: an ISO-639-1 code
    # (e.g. "en", "nl") or "auto" to let whisper autodetect.
    language: str = "auto"


@dataclass(frozen=True)
class Transcript:
    meeting_id: int
    text: str
    segments: list[dict] | None
    language: str | None
    created_at: str


@dataclass(frozen=True)
class Note:
    id: int
    meeting_id: int
    prompt_version_id: int | None
    model_used: str | None
    markdown: str
    created_at: str
    prompt_name: str | None = None


def _row_to_meeting(row: sqlite3.Row) -> Meeting:
    return Meeting(
        id=row["id"],
        user_id=row["user_id"],
        title=row["title"],
        meeting_date=row["meeting_date"],
        filename=row["filename"],
        duration_s=row["duration_s"],
        status=row["status"],
        created_at=row["created_at"],
        language=row["language"],
    )


_MEETING_COLUMNS = (
    "id, user_id, title, meeting_date, filename, duration_s, status, "
    "created_at, language"
)


# --- Meetings ------------------------------------------------------------


def create(
    conn: sqlite3.Connection,
    *,
    user_id: int,
    title: str,
    meeting_date: str | None,
    filename: str | None,
    language: str = "auto",
) -> Meeting:
    cur = conn.execute(
        "INSERT INTO meetings (user_id, title, meeting_date, filename, status, language) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (user_id, title, meeting_date, filename, STATUS_PROCESSING, language),
    )
    conn.commit()
    meeting = get(conn, cur.lastrowid)
    assert meeting is not None
    return meeting


def get(conn: sqlite3.Connection, meeting_id: int) -> Meeting | None:
    row = conn.execute(
        f"SELECT {_MEETING_COLUMNS} FROM meetings WHERE id = ?",
        (meeting_id,),
    ).fetchone()
    return _row_to_meeting(row) if row else None


def get_owned(conn: sqlite3.Connection, meeting_id: int, user: User) -> Meeting:
    """Fetch a meeting the user is allowed to see, or raise 404.

    404 (not 403) for someone else's meeting so ownership doesn't leak the
    existence of other users' meetings.
    """
    meeting = get(conn, meeting_id)
    if meeting is None or (not user.is_admin and meeting.user_id != user.id):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Meeting not found.")
    return meeting


def list_for_user(conn: sqlite3.Connection, user: User) -> list[Meeting]:
    if user.is_admin:
        rows = conn.execute(
            f"SELECT {_MEETING_COLUMNS} FROM meetings ORDER BY created_at DESC"
        ).fetchall()
    else:
        rows = conn.execute(
            f"SELECT {_MEETING_COLUMNS} FROM meetings WHERE user_id = ? "
            "ORDER BY created_at DESC",
            (user.id,),
        ).fetchall()
    return [_row_to_meeting(r) for r in rows]


def search_for_user(
    conn: sqlite3.Connection,
    user: User,
    *,
    query: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
) -> list[Meeting]:
    """Archive listing with optional title search and meeting-date range
    (PLAN.md §8.3). Ownership scoping matches ``list_for_user`` — members see
    only their own meetings, admins see all.

    The date range matches against ``meeting_date`` when set, otherwise the
    creation date, so meetings without an explicit date still filter sensibly.
    """
    sql = f"SELECT {_MEETING_COLUMNS} FROM meetings"
    where: list[str] = []
    params: list[object] = []
    if not user.is_admin:
        where.append("user_id = ?")
        params.append(user.id)
    if query:
        where.append("title LIKE ?")
        params.append(f"%{query}%")
    effective_date = "COALESCE(meeting_date, substr(created_at, 1, 10))"
    if date_from:
        where.append(f"{effective_date} >= ?")
        params.append(date_from)
    if date_to:
        where.append(f"{effective_date} <= ?")
        params.append(date_to)
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY created_at DESC"
    rows = conn.execute(sql, params).fetchall()
    return [_row_to_meeting(r) for r in rows]


def set_status(conn: sqlite3.Connection, meeting_id: int, status_value: str) -> None:
    conn.execute(
        "UPDATE meetings SET status = ? WHERE id = ?", (status_value, meeting_id)
    )
    conn.commit()


def set_duration(conn: sqlite3.Connection, meeting_id: int, duration_s: float | None) -> None:
    conn.execute(
        "UPDATE meetings SET duration_s = ? WHERE id = ?", (duration_s, meeting_id)
    )
    conn.commit()


def note_count(conn: sqlite3.Connection, meeting_id: int) -> int:
    return conn.execute(
        "SELECT COUNT(*) AS c FROM notes WHERE meeting_id = ?", (meeting_id,)
    ).fetchone()["c"]


def delete(conn: sqlite3.Connection, meeting_id: int) -> None:
    """Delete a meeting and everything hanging off it (ON DELETE CASCADE covers
    jobs, transcript, notes). Audio-file removal is the caller's job."""
    conn.execute("DELETE FROM meetings WHERE id = ?", (meeting_id,))
    conn.commit()


# --- Transcripts ---------------------------------------------------------


def save_transcript(
    conn: sqlite3.Connection,
    meeting_id: int,
    *,
    text: str,
    segments: list[dict] | None,
    language: str | None,
) -> None:
    conn.execute(
        "INSERT INTO transcripts (meeting_id, text, segments_json, language) "
        "VALUES (?, ?, ?, ?) "
        "ON CONFLICT(meeting_id) DO UPDATE SET "
        "text = excluded.text, segments_json = excluded.segments_json, "
        "language = excluded.language",
        (
            meeting_id,
            text,
            json.dumps(segments) if segments is not None else None,
            language,
        ),
    )
    conn.commit()


def get_transcript(conn: sqlite3.Connection, meeting_id: int) -> Transcript | None:
    row = conn.execute(
        "SELECT meeting_id, text, segments_json, language, created_at "
        "FROM transcripts WHERE meeting_id = ?",
        (meeting_id,),
    ).fetchone()
    if row is None:
        return None
    segments = None
    if row["segments_json"]:
        try:
            segments = json.loads(row["segments_json"])
        except json.JSONDecodeError:
            segments = None
    return Transcript(
        meeting_id=row["meeting_id"],
        text=row["text"],
        segments=segments,
        language=row["language"],
        created_at=row["created_at"],
    )


# --- Notes ---------------------------------------------------------------


def save_note(
    conn: sqlite3.Connection,
    meeting_id: int,
    *,
    prompt_version_id: int | None,
    model_used: str | None,
    markdown: str,
) -> int:
    cur = conn.execute(
        "INSERT INTO notes (meeting_id, prompt_version_id, model_used, markdown) "
        "VALUES (?, ?, ?, ?)",
        (meeting_id, prompt_version_id, model_used, markdown),
    )
    conn.commit()
    return cur.lastrowid


def _row_to_note(r: sqlite3.Row) -> Note:
    return Note(
        id=r["id"],
        meeting_id=r["meeting_id"],
        prompt_version_id=r["prompt_version_id"],
        model_used=r["model_used"],
        markdown=r["markdown"],
        created_at=r["created_at"],
        prompt_name=r["prompt_name"],
    )


_NOTE_SELECT = (
    "SELECT n.id, n.meeting_id, n.prompt_version_id, n.model_used, "
    "       n.markdown, n.created_at, p.name AS prompt_name "
    "FROM notes n "
    "LEFT JOIN prompt_versions pv ON pv.id = n.prompt_version_id "
    "LEFT JOIN prompts p ON p.id = pv.prompt_id "
)


def get_note(conn: sqlite3.Connection, note_id: int) -> Note | None:
    row = conn.execute(
        _NOTE_SELECT + "WHERE n.id = ?", (note_id,)
    ).fetchone()
    return _row_to_note(row) if row else None


def list_notes(conn: sqlite3.Connection, meeting_id: int) -> list[Note]:
    rows = conn.execute(
        _NOTE_SELECT + "WHERE n.meeting_id = ? ORDER BY n.created_at, n.id",
        (meeting_id,),
    ).fetchall()
    return [_row_to_note(r) for r in rows]
