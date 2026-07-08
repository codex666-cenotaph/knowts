"""Pipeline orchestration (PLAN.md §1 pipeline steps 3-6).

``process_meeting`` runs the queued jobs for one meeting to completion, updating
job and meeting status as it goes:

  1. transcribe: ffmpeg MP3 -> 16 kHz mono WAV -> STT -> store transcript
  2. notes:      render prompt against the transcript (single-shot or
                 map-reduce) -> store notes

Called from the background worker thread (jobs.PipelineWorker), so everything
here is synchronous and uses the worker's own SQLite connection.
"""

from __future__ import annotations

import logging
import sqlite3
from pathlib import Path

from . import audio, jobs, meetings, notes, prompts
from .audio import AudioError
from .config import Settings
from .llm import LLMClient, LLMError
from .transcriber import OpenAiCompatTranscriber, TranscriptionError

log = logging.getLogger("knowts.pipeline")


def _audio_path(settings: Settings, filename: str) -> Path:
    return settings.audio_dir / filename


def process_meeting(conn: sqlite3.Connection, settings: Settings, meeting_id: int) -> None:
    meeting = meetings.get(conn, meeting_id)
    if meeting is None:
        log.warning("process_meeting: meeting %d gone", meeting_id)
        return

    # 1. Transcription (only if a transcribe job is queued).
    transcribe_jobs = jobs.queued_for_meeting(conn, meeting_id, jobs.KIND_TRANSCRIBE)
    for job in transcribe_jobs:
        ok = _run_transcribe(conn, settings, meeting_id, job.id)
        if not ok:
            meetings.set_status(conn, meeting_id, meetings.STATUS_ERROR)
            return  # can't generate notes without a transcript

    # 2. Notes (any queued notes jobs — from upload or "generate more notes").
    notes_jobs = jobs.queued_for_meeting(conn, meeting_id, jobs.KIND_NOTES)
    if notes_jobs:
        transcript = meetings.get_transcript(conn, meeting_id)
        if transcript is None:
            for job in notes_jobs:
                jobs.mark_error(conn, job.id, "no transcript available")
            meetings.set_status(conn, meeting_id, meetings.STATUS_ERROR)
            return
        meetings.set_status(conn, meeting_id, meetings.STATUS_GENERATING)
        client = LLMClient(settings.llm_base_url)
        any_error = False
        for job in notes_jobs:
            if not _run_notes(conn, settings, client, transcript, job):
                any_error = True
        _finalize_status(conn, meeting_id, had_error=any_error)
    else:
        _finalize_status(conn, meeting_id, had_error=False)


def _run_transcribe(
    conn: sqlite3.Connection, settings: Settings, meeting_id: int, job_id: int
) -> bool:
    meeting = meetings.get(conn, meeting_id)
    if meeting is None or not meeting.filename:
        jobs.mark_error(conn, job_id, "meeting has no audio file")
        return False

    jobs.mark_running(conn, job_id, step="converting")
    meetings.set_status(conn, meeting_id, meetings.STATUS_TRANSCRIBING)
    src = _audio_path(settings, meeting.filename)
    wav = src.with_suffix(".wav")
    try:
        duration = audio.probe_duration_s(src)
        if duration is not None:
            meetings.set_duration(conn, meeting_id, duration)
        audio.to_wav_16k_mono(src, wav)
    except AudioError as exc:
        jobs.mark_error(conn, job_id, str(exc))
        log.error("transcode failed for meeting %d: %s", meeting_id, exc)
        return False

    jobs.set_step(conn, job_id, "transcribing")
    transcriber = OpenAiCompatTranscriber(
        settings.effective_stt_base_url, settings.stt_model
    )
    try:
        result = transcriber.transcribe(wav, model=settings.stt_model)
    except TranscriptionError as exc:
        jobs.mark_error(conn, job_id, str(exc))
        log.error("transcription failed for meeting %d: %s", meeting_id, exc)
        return False
    finally:
        wav.unlink(missing_ok=True)  # keep only the original upload

    meetings.save_transcript(
        conn,
        meeting_id,
        text=result.text,
        segments=result.segments,
        language=result.language,
    )
    jobs.mark_done(conn, job_id)
    log.info("transcribed meeting %d (%d chars)", meeting_id, len(result.text))
    return True


def _run_notes(
    conn: sqlite3.Connection,
    settings: Settings,
    client: LLMClient,
    transcript: meetings.Transcript,
    job: jobs.Job,
) -> bool:
    if job.prompt_id is None:
        jobs.mark_error(conn, job.id, "notes job has no prompt")
        return False
    version = prompts.latest_version(conn, job.prompt_id)
    prompt = prompts.get(conn, job.prompt_id)
    if version is None or prompt is None:
        jobs.mark_error(conn, job.id, "prompt not found")
        return False

    jobs.mark_running(conn, job.id, step=f"notes:{prompt.name}")
    try:
        markdown, model_used = notes.generate(
            client,
            version,
            transcript.text,
            transcript.segments,
            default_model=settings.llm_default_model,
            context_tokens=settings.llm_context_tokens,
        )
    except LLMError as exc:
        jobs.mark_error(conn, job.id, str(exc))
        log.error("notes gen failed for meeting %d prompt %d: %s", job.meeting_id, job.prompt_id, exc)
        return False

    meetings.save_note(
        conn,
        job.meeting_id,
        prompt_version_id=version.id,
        model_used=model_used,
        markdown=markdown,
    )
    jobs.mark_done(conn, job.id)
    log.info("generated '%s' notes for meeting %d", prompt.name, job.meeting_id)
    return True


def _finalize_status(conn: sqlite3.Connection, meeting_id: int, *, had_error: bool) -> None:
    """Set the meeting's status from the state of all its jobs."""
    all_jobs = jobs.list_for_meeting(conn, meeting_id)
    statuses = {j.status for j in all_jobs}
    if jobs.STATUS_QUEUED in statuses or jobs.STATUS_RUNNING in statuses:
        return  # more work pending; leave as-is
    if had_error or jobs.STATUS_ERROR in statuses:
        # Errored, but a transcript/some notes may still exist → partial.
        has_transcript = meetings.get_transcript(conn, meeting_id) is not None
        meetings.set_status(
            conn,
            meeting_id,
            meetings.STATUS_ERROR if not has_transcript else meetings.STATUS_DONE,
        )
    else:
        meetings.set_status(conn, meeting_id, meetings.STATUS_DONE)
