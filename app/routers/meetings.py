"""Meeting routes: upload, archive, detail, generate-more-notes, downloads,
delete (PLAN.md §8). Ownership is enforced on every route via
``meetings.get_owned``.

The UI here is deliberately minimal — enough to drive and observe the Phase 2
pipeline end to end. The polished archive/detail/upload experience (drag-drop,
notes tabs, etc.) is Phase 3.
"""

from __future__ import annotations

import logging
import sqlite3
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile, status
from fastapi.responses import FileResponse, RedirectResponse, Response

from .. import auth as auth_mod
from .. import jobs as jobs_mod
from .. import meetings as meetings_mod
from .. import prompts as prompts_mod
from ..config import Settings, get_settings
from ..i18n import SUPPORTED_LANGUAGES
from ..templating import render
from ..users import User

log = logging.getLogger("knowts.routes.meetings")

router = APIRouter(prefix="/meetings")

_ALLOWED_EXT = {".mp3", ".m4a", ".wav", ".mp4", ".ogg", ".flac", ".webm", ".aac"}
_CHUNK = 1024 * 1024
_MAX_SPEAKERS = 20


def _db(request: Request) -> sqlite3.Connection:
    return request.app.state.db


def _worker(request: Request):
    return request.app.state.worker


def _audio_file(settings: Settings, filename: str) -> Path:
    return settings.audio_dir / filename


# --- Archive -------------------------------------------------------------


@router.get("")
def archive(
    request: Request,
    q: str = "",
    date_from: str = "",
    date_to: str = "",
    user: User = Depends(auth_mod.require_user),
):
    conn = _db(request)
    rows = meetings_mod.search_for_user(
        conn,
        user,
        query=q.strip() or None,
        date_from=date_from.strip() or None,
        date_to=date_to.strip() or None,
    )
    items = [
        {"meeting": m, "notes": meetings_mod.note_count(conn, m.id)} for m in rows
    ]
    return render(
        request,
        "meetings_list.html",
        items=items,
        q=q,
        date_from=date_from,
        date_to=date_to,
        filtered=bool(q or date_from or date_to),
    )


# --- Upload --------------------------------------------------------------


@router.post("")
async def upload(
    request: Request,
    title: str = Form(...),
    meeting_date: str = Form(""),
    language: str = Form(""),
    num_speakers: str = Form(""),
    prompt_ids: list[int] = Form(default=[]),
    csrf_token: str = Form(...),
    file: UploadFile = File(...),
    settings: Settings = Depends(get_settings),
    ctx: auth_mod.AuthContext = Depends(auth_mod.resolve_auth),
):
    user = auth_mod.require_user(ctx)
    auth_mod.verify_csrf(request, csrf_token, ctx)
    conn = _db(request)

    def fail(message: str) -> RedirectResponse:
        return RedirectResponse(f"/?err={message}", status_code=status.HTTP_303_SEE_OTHER)

    title = title.strip()
    if not title:
        return fail("Title+is+required.")
    if not file or not file.filename:
        return fail("An+audio+file+is+required.")
    ext = Path(file.filename).suffix.lower()
    if ext not in _ALLOWED_EXT:
        return fail("Unsupported+audio+format.")

    # Meeting language: the picked value, else fall back to the user's own
    # interface language, else autodetect. "auto" is always valid.
    language = language.strip().lower()
    if language != "auto" and language not in SUPPORTED_LANGUAGES:
        language = user.language if user.language in SUPPORTED_LANGUAGES else "auto"

    # Expected speaker count for diarization: 0/blank = auto-detect. Clamp to a
    # sane range; ignore garbage.
    try:
        speakers = int(num_speakers)
    except (TypeError, ValueError):
        speakers = 0
    speakers = max(0, min(speakers, _MAX_SPEAKERS))

    # Create the meeting first so the stored file can be named by its id
    # (PLAN.md §7: /data/audio/<meeting_id>.<ext>).
    meeting = meetings_mod.create(
        conn,
        user_id=user.id,
        title=title,
        meeting_date=meeting_date.strip() or None,
        filename=None,
        language=language,
        diarization_num_speakers=speakers,
    )
    filename = f"{meeting.id}{ext}"
    dest = _audio_file(settings, filename)
    max_bytes = settings.max_upload_mb * 1024 * 1024

    written = 0
    try:
        settings.audio_dir.mkdir(parents=True, exist_ok=True)
        with open(dest, "wb") as out:
            while chunk := await file.read(_CHUNK):
                written += len(chunk)
                if written > max_bytes:
                    raise ValueError("too large")
                out.write(chunk)
    except ValueError:
        dest.unlink(missing_ok=True)
        meetings_mod.delete(conn, meeting.id)
        return fail(f"File+exceeds+the+{settings.max_upload_mb}+MB+limit.")
    finally:
        await file.close()

    if written == 0:
        dest.unlink(missing_ok=True)
        meetings_mod.delete(conn, meeting.id)
        return fail("Uploaded+file+was+empty.")

    conn.execute(
        "UPDATE meetings SET filename = ? WHERE id = ?", (filename, meeting.id)
    )
    conn.commit()

    # Queue transcription, then one notes job per valid, active prompt.
    jobs_mod.create_transcribe_job(conn, meeting.id)
    active_ids = {p.id for p in prompts_mod.list_active(conn)}
    for pid in prompt_ids:
        if pid in active_ids:
            jobs_mod.create_notes_job(conn, meeting.id, pid)

    _worker(request).enqueue(meeting.id)
    return RedirectResponse(
        f"/meetings/{meeting.id}", status_code=status.HTTP_303_SEE_OTHER
    )


# --- Detail + status -----------------------------------------------------


@router.get("/{meeting_id}")
def detail(
    request: Request, meeting_id: int, user: User = Depends(auth_mod.require_user)
):
    conn = _db(request)
    meeting = meetings_mod.get_owned(conn, meeting_id, user)
    return render(
        request,
        "meeting_detail.html",
        meeting=meeting,
        transcript=meetings_mod.get_transcript(conn, meeting_id),
        notes=meetings_mod.list_notes(conn, meeting_id),
        jobs=jobs_mod.list_for_meeting(conn, meeting_id),
        prompts=prompts_mod.list_active(conn),
        rendered_notes=_render_notes(meetings_mod.list_notes(conn, meeting_id)),
    )


@router.get("/{meeting_id}/status")
def status_fragment(
    request: Request, meeting_id: int, user: User = Depends(auth_mod.require_user)
):
    """HTMX polling fragment: job progress + meeting status."""
    conn = _db(request)
    meeting = meetings_mod.get_owned(conn, meeting_id, user)
    return render(
        request,
        "_meeting_status.html",
        meeting=meeting,
        jobs=jobs_mod.list_for_meeting(conn, meeting_id),
    )


@router.post("/{meeting_id}/notes")
def generate_more(
    request: Request,
    meeting_id: int,
    prompt_ids: list[int] = Form(default=[]),
    csrf_token: str = Form(...),
    ctx: auth_mod.AuthContext = Depends(auth_mod.resolve_auth),
):
    user = auth_mod.require_user(ctx)
    auth_mod.verify_csrf(request, csrf_token, ctx)
    conn = _db(request)
    meeting = meetings_mod.get_owned(conn, meeting_id, user)

    if meetings_mod.get_transcript(conn, meeting_id) is None:
        return RedirectResponse(
            f"/meetings/{meeting_id}?err=Transcript+not+ready+yet.",
            status_code=status.HTTP_303_SEE_OTHER,
        )
    active_ids = {p.id for p in prompts_mod.list_active(conn)}
    queued = 0
    for pid in prompt_ids:
        if pid in active_ids:
            jobs_mod.create_notes_job(conn, meeting_id, pid)
            queued += 1
    if queued:
        meetings_mod.set_status(conn, meeting_id, meetings_mod.STATUS_GENERATING)
        _worker(request).enqueue(meeting_id)
        msg = f"Queued+{queued}+more+note+set(s)."
    else:
        msg = "err=Select+at+least+one+prompt."
        return RedirectResponse(
            f"/meetings/{meeting_id}?{msg}", status_code=status.HTTP_303_SEE_OTHER
        )
    return RedirectResponse(
        f"/meetings/{meeting_id}?msg={msg}", status_code=status.HTTP_303_SEE_OTHER
    )


@router.post("/{meeting_id}/retry")
def retry_failed(
    request: Request,
    meeting_id: int,
    csrf_token: str = Form(...),
    ctx: auth_mod.AuthContext = Depends(auth_mod.resolve_auth),
):
    """Requeue a meeting's failed jobs after a transient upstream error
    (STT/LLM unreachable, cold-start timeout) — PLAN.md §10 step 14."""
    user = auth_mod.require_user(ctx)
    auth_mod.verify_csrf(request, csrf_token, ctx)
    conn = _db(request)
    meetings_mod.get_owned(conn, meeting_id, user)  # ownership check

    requeued = jobs_mod.retry_failed_jobs(conn, meeting_id)
    if not requeued:
        return RedirectResponse(
            f"/meetings/{meeting_id}?err=Nothing+to+retry.",
            status_code=status.HTTP_303_SEE_OTHER,
        )
    meetings_mod.set_status(conn, meeting_id, meetings_mod.STATUS_PROCESSING)
    _worker(request).enqueue(meeting_id)
    return RedirectResponse(
        f"/meetings/{meeting_id}?msg=Retrying+{requeued}+failed+job(s).",
        status_code=status.HTTP_303_SEE_OTHER,
    )


# --- Audio + downloads ---------------------------------------------------


@router.get("/{meeting_id}/audio")
def audio_stream(
    request: Request,
    meeting_id: int,
    user: User = Depends(auth_mod.require_user),
    settings: Settings = Depends(get_settings),
):
    conn = _db(request)
    meeting = meetings_mod.get_owned(conn, meeting_id, user)
    if not meeting.filename:
        return Response("No audio.", status_code=status.HTTP_404_NOT_FOUND)
    path = _audio_file(settings, meeting.filename)
    if not path.exists():
        return Response("Audio file missing.", status_code=status.HTTP_404_NOT_FOUND)
    # Starlette's FileResponse honours Range requests, enabling seek in the
    # browser audio player.
    return FileResponse(path, media_type="audio/mpeg", filename=meeting.filename)


@router.get("/{meeting_id}/transcript.txt")
def transcript_txt(
    request: Request, meeting_id: int, user: User = Depends(auth_mod.require_user)
):
    conn = _db(request)
    meeting = meetings_mod.get_owned(conn, meeting_id, user)
    transcript = meetings_mod.get_transcript(conn, meeting_id)
    if transcript is None:
        return Response("No transcript.", status_code=status.HTTP_404_NOT_FOUND)
    # Speaker-attributed when a diarizing backend labelled the segments; plain
    # transcript text otherwise.
    body = meetings_mod.speaker_attributed_text(transcript.segments, transcript.text)
    return Response(
        body,
        media_type="text/plain; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="meeting-{meeting_id}.txt"'
        },
    )


@router.get("/{meeting_id}/transcript.srt")
def transcript_srt(
    request: Request, meeting_id: int, user: User = Depends(auth_mod.require_user)
):
    conn = _db(request)
    meeting = meetings_mod.get_owned(conn, meeting_id, user)
    transcript = meetings_mod.get_transcript(conn, meeting_id)
    if transcript is None or not transcript.segments:
        return Response(
            "No timestamped segments for this transcript.",
            status_code=status.HTTP_404_NOT_FOUND,
        )
    return Response(
        _to_srt(transcript.segments),
        media_type="application/x-subrip; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="meeting-{meeting_id}.srt"'
        },
    )


@router.get("/{meeting_id}/notes/{note_id}.md")
def note_markdown(
    request: Request,
    meeting_id: int,
    note_id: int,
    user: User = Depends(auth_mod.require_user),
):
    conn = _db(request)
    meetings_mod.get_owned(conn, meeting_id, user)  # ownership check
    note = meetings_mod.get_note(conn, note_id)
    if note is None or note.meeting_id != meeting_id:
        return Response("Note not found.", status_code=status.HTTP_404_NOT_FOUND)
    slug = (note.prompt_name or "notes").replace(" ", "-")
    return Response(
        note.markdown,
        media_type="text/markdown; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="meeting-{meeting_id}-{slug}.md"'
        },
    )


@router.post("/{meeting_id}/delete")
def delete_meeting(
    request: Request,
    meeting_id: int,
    csrf_token: str = Form(...),
    settings: Settings = Depends(get_settings),
    ctx: auth_mod.AuthContext = Depends(auth_mod.resolve_auth),
):
    user = auth_mod.require_user(ctx)
    auth_mod.verify_csrf(request, csrf_token, ctx)
    conn = _db(request)
    meeting = meetings_mod.get_owned(conn, meeting_id, user)
    if meeting.filename:
        _audio_file(settings, meeting.filename).unlink(missing_ok=True)
    meetings_mod.delete(conn, meeting_id)
    return RedirectResponse(
        "/meetings?msg=Meeting+deleted.", status_code=status.HTTP_303_SEE_OTHER
    )


# --- Helpers -------------------------------------------------------------


def _render_notes(notes: list) -> dict[int, str]:
    """Render each note's Markdown to safe HTML (source HTML is escaped)."""
    from markdown_it import MarkdownIt

    md = MarkdownIt("commonmark", {"html": False, "linkify": True, "typographer": True})
    return {n.id: md.render(n.markdown) for n in notes}


def _fmt_ts(seconds: float) -> str:
    if seconds < 0:
        seconds = 0
    ms = int(round(seconds * 1000))
    h, ms = divmod(ms, 3_600_000)
    m, ms = divmod(ms, 60_000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def _to_srt(segments: list[dict]) -> str:
    lines: list[str] = []
    for i, seg in enumerate(segments, start=1):
        start = float(seg.get("start", 0) or 0)
        end = float(seg.get("end", start) or start)
        text = str(seg.get("text", "")).strip()
        speaker = meetings_mod.segment_speaker(seg)
        if speaker:
            text = f"{speaker}: {text}"
        lines.append(str(i))
        lines.append(f"{_fmt_ts(start)} --> {_fmt_ts(end)}")
        lines.append(text)
        lines.append("")
    return "\n".join(lines)
