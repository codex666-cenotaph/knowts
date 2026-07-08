"""Job records + the in-process pipeline worker (PLAN.md §1, §10 step 6/9).

Jobs are rows in the ``jobs`` table (kind = ``transcribe`` | ``notes``). A
single background asyncio worker drains a queue of meeting ids and runs their
queued jobs one at a time. Serial processing means transcription and note
generation never contend for the shared GPU behind llama-swap, which matches
the sequential pipeline the plan describes.

The worker uses its **own** SQLite connection (WAL allows one writer alongside
the request connections' readers) and runs blocking work (ffmpeg, HTTP to
STT/LLM) in a thread so the event loop stays responsive.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
from dataclasses import dataclass

from . import db as db_mod
from .config import Settings

log = logging.getLogger("knowts.jobs")

KIND_TRANSCRIBE = "transcribe"
KIND_NOTES = "notes"

STATUS_QUEUED = "queued"
STATUS_RUNNING = "running"
STATUS_DONE = "done"
STATUS_ERROR = "error"


@dataclass(frozen=True)
class Job:
    id: int
    meeting_id: int
    kind: str
    status: str
    step: str | None
    error: str | None
    started_at: str | None
    finished_at: str | None
    prompt_id: int | None = None


# --- Job records ---------------------------------------------------------


def create_transcribe_job(conn: sqlite3.Connection, meeting_id: int) -> int:
    cur = conn.execute(
        "INSERT INTO jobs (meeting_id, kind, status) VALUES (?, ?, ?)",
        (meeting_id, KIND_TRANSCRIBE, STATUS_QUEUED),
    )
    conn.commit()
    return cur.lastrowid


def create_notes_job(conn: sqlite3.Connection, meeting_id: int, prompt_id: int) -> int:
    """A notes job records its target prompt id in ``step`` as ``prompt:<id>``
    (the schema has no dedicated column; step doubles as the parameter here)."""
    cur = conn.execute(
        "INSERT INTO jobs (meeting_id, kind, status, step) VALUES (?, ?, ?, ?)",
        (meeting_id, KIND_NOTES, STATUS_QUEUED, f"prompt:{prompt_id}"),
    )
    conn.commit()
    return cur.lastrowid


def _prompt_id_from_step(step: str | None) -> int | None:
    if step and step.startswith("prompt:"):
        try:
            return int(step.split(":", 1)[1])
        except ValueError:
            return None
    return None


def _row_to_job(row: sqlite3.Row) -> Job:
    return Job(
        id=row["id"],
        meeting_id=row["meeting_id"],
        kind=row["kind"],
        status=row["status"],
        step=row["step"],
        error=row["error"],
        started_at=row["started_at"],
        finished_at=row["finished_at"],
        prompt_id=_prompt_id_from_step(row["step"]) if row["kind"] == KIND_NOTES else None,
    )


def list_for_meeting(conn: sqlite3.Connection, meeting_id: int) -> list[Job]:
    rows = conn.execute(
        "SELECT id, meeting_id, kind, status, step, error, started_at, finished_at "
        "FROM jobs WHERE meeting_id = ? ORDER BY id",
        (meeting_id,),
    ).fetchall()
    return [_row_to_job(r) for r in rows]


def queued_for_meeting(conn: sqlite3.Connection, meeting_id: int, kind: str) -> list[Job]:
    rows = conn.execute(
        "SELECT id, meeting_id, kind, status, step, error, started_at, finished_at "
        "FROM jobs WHERE meeting_id = ? AND kind = ? AND status = ? ORDER BY id",
        (meeting_id, kind, STATUS_QUEUED),
    ).fetchall()
    return [_row_to_job(r) for r in rows]


def mark_running(conn: sqlite3.Connection, job_id: int, step: str | None = None) -> None:
    conn.execute(
        "UPDATE jobs SET status = ?, started_at = datetime('now'), "
        "step = COALESCE(?, step) WHERE id = ?",
        (STATUS_RUNNING, step, job_id),
    )
    conn.commit()


def set_step(conn: sqlite3.Connection, job_id: int, step: str) -> None:
    conn.execute("UPDATE jobs SET step = ? WHERE id = ?", (step, job_id))
    conn.commit()


def mark_done(conn: sqlite3.Connection, job_id: int) -> None:
    conn.execute(
        "UPDATE jobs SET status = ?, error = NULL, finished_at = datetime('now') "
        "WHERE id = ?",
        (STATUS_DONE, job_id),
    )
    conn.commit()


def mark_error(conn: sqlite3.Connection, job_id: int, message: str) -> None:
    conn.execute(
        "UPDATE jobs SET status = ?, error = ?, finished_at = datetime('now') "
        "WHERE id = ?",
        (STATUS_ERROR, message[:2000], job_id),
    )
    conn.commit()


def requeue_interrupted(conn: sqlite3.Connection) -> int:
    """On startup, any job left ``running`` belongs to a crashed worker — put it
    back on the queue. Returns the count so callers can re-enqueue the meetings.
    """
    cur = conn.execute(
        "UPDATE jobs SET status = ?, started_at = NULL WHERE status = ?",
        (STATUS_QUEUED, STATUS_RUNNING),
    )
    conn.commit()
    return cur.rowcount


def meetings_with_queued_jobs(conn: sqlite3.Connection) -> list[int]:
    rows = conn.execute(
        "SELECT DISTINCT meeting_id FROM jobs WHERE status = ? ORDER BY meeting_id",
        (STATUS_QUEUED,),
    ).fetchall()
    return [r["meeting_id"] for r in rows]


# --- Worker --------------------------------------------------------------


class PipelineWorker:
    """Drains a queue of meeting ids and runs their queued jobs serially."""

    def __init__(self, settings: Settings):
        self._settings = settings
        self._queue: asyncio.Queue[int] = asyncio.Queue()
        self._task: asyncio.Task | None = None
        self._conn: sqlite3.Connection | None = None

    def start(self) -> None:
        self._conn = db_mod.connect(self._settings.db_path)
        self._task = asyncio.create_task(self._run(), name="knowts-pipeline-worker")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        if self._conn is not None:
            self._conn.close()

    def enqueue(self, meeting_id: int) -> None:
        self._queue.put_nowait(meeting_id)

    async def recover_and_resume(self) -> None:
        """Requeue interrupted jobs and re-enqueue their meetings (crash safety)."""
        assert self._conn is not None
        requeued = requeue_interrupted(self._conn)
        if requeued:
            log.info("Requeued %d interrupted job(s) after restart.", requeued)
        for meeting_id in meetings_with_queued_jobs(self._conn):
            self.enqueue(meeting_id)

    async def _run(self) -> None:
        assert self._conn is not None
        # Import here to avoid a circular import at module load.
        from . import pipeline

        while True:
            meeting_id = await self._queue.get()
            try:
                await asyncio.to_thread(
                    pipeline.process_meeting, self._conn, self._settings, meeting_id
                )
            except Exception:  # noqa: BLE001 — worker must never die
                log.exception("pipeline failed for meeting %d", meeting_id)
            finally:
                self._queue.task_done()
