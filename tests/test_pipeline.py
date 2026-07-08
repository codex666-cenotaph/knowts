"""End-to-end pipeline test with faked ffmpeg/STT/LLM (PLAN.md §1, §10).

Exercises ``pipeline.process_meeting`` directly (no HTTP, no background worker)
so the transcribe -> notes orchestration and status transitions are
deterministic.
"""

from __future__ import annotations

import pytest

from app import audio as audio_mod
from app import db as db_mod
from app import jobs, meetings, pipeline, prompts
from app.config import Settings
from app.transcriber import TranscriptResult


class _FakeTranscriber:
    def __init__(self, *args, **kwargs):
        pass

    def transcribe(self, wav_path, *, model=None):
        return TranscriptResult(
            text="hello world this is the meeting",
            segments=[
                {"start": 0.0, "end": 1.5, "text": "hello world"},
                {"start": 1.5, "end": 3.0, "text": "this is the meeting"},
            ],
            language="en",
        )


class _FakeLLM:
    def __init__(self, *args, **kwargs):
        pass

    def complete(self, *, model, system, user, temperature=None, max_tokens=None):
        return f"# Notes\n\nGenerated with {model}."


@pytest.fixture
def env(tmp_path, monkeypatch):
    settings = Settings(DATA_DIR=str(tmp_path), SECRET_KEY="x" * 40)
    settings.ensure_dirs()
    conn = db_mod.connect(settings.db_path)
    db_mod.run_migrations(conn)
    prompts.seed_starter_prompts(conn)

    # Fake the heavy dependencies.
    monkeypatch.setattr(audio_mod, "probe_duration_s", lambda src: 42.0)
    monkeypatch.setattr(
        audio_mod, "to_wav_16k_mono", lambda src, dst: dst.write_bytes(b"RIFFfake") or dst
    )
    monkeypatch.setattr(pipeline, "OpenAiCompatTranscriber", _FakeTranscriber)
    monkeypatch.setattr(pipeline, "LLMClient", _FakeLLM)

    yield settings, conn
    conn.close()


def _make_user(conn):
    from app import users

    return users.create(conn, "alice", "alicepass123", "member")


def test_full_pipeline_transcribe_and_notes(env):
    settings, conn = env
    user = _make_user(conn)
    meeting = meetings.create(
        conn, user_id=user.id, title="Sync", meeting_date=None, filename="1.mp3"
    )
    # A fake original upload on disk.
    (settings.audio_dir / meeting.filename).write_bytes(b"fake-mp3")

    summary = next(p for p in prompts.list_active(conn) if p.name == "summary")
    jobs.create_transcribe_job(conn, meeting.id)
    jobs.create_notes_job(conn, meeting.id, summary.id)

    pipeline.process_meeting(conn, settings, meeting.id)

    refreshed = meetings.get(conn, meeting.id)
    assert refreshed.status == meetings.STATUS_DONE
    assert refreshed.duration_s == 42.0

    transcript = meetings.get_transcript(conn, meeting.id)
    assert transcript is not None
    assert "meeting" in transcript.text
    assert transcript.segments and len(transcript.segments) == 2

    notes_list = meetings.list_notes(conn, meeting.id)
    assert len(notes_list) == 1
    assert notes_list[0].prompt_name == "summary"
    assert "Notes" in notes_list[0].markdown

    all_jobs = jobs.list_for_meeting(conn, meeting.id)
    assert all(j.status == jobs.STATUS_DONE for j in all_jobs)

    # The intermediate WAV is cleaned up; only the original remains.
    assert (settings.audio_dir / "1.mp3").exists()
    assert not (settings.audio_dir / "1.wav").exists()


def test_transcription_failure_marks_meeting_error(env, monkeypatch):
    settings, conn = env
    from app.transcriber import TranscriptionError

    class _Boom:
        def __init__(self, *a, **k):
            pass

        def transcribe(self, wav_path, *, model=None):
            raise TranscriptionError("STT down")

    monkeypatch.setattr(pipeline, "OpenAiCompatTranscriber", _Boom)

    user = _make_user(conn)
    meeting = meetings.create(
        conn, user_id=user.id, title="Sync", meeting_date=None, filename="1.mp3"
    )
    (settings.audio_dir / "1.mp3").write_bytes(b"fake")
    jobs.create_transcribe_job(conn, meeting.id)
    summary = next(p for p in prompts.list_active(conn) if p.name == "summary")
    jobs.create_notes_job(conn, meeting.id, summary.id)

    pipeline.process_meeting(conn, settings, meeting.id)

    assert meetings.get(conn, meeting.id).status == meetings.STATUS_ERROR
    tjob = [j for j in jobs.list_for_meeting(conn, meeting.id) if j.kind == "transcribe"][0]
    assert tjob.status == jobs.STATUS_ERROR and "STT down" in tjob.error
    # Notes were never attempted (no transcript).
    assert meetings.list_notes(conn, meeting.id) == []


def test_notes_use_dutch_override_for_dutch_owner(env, monkeypatch):
    settings, conn = env
    from app import users

    captured_systems = []

    class _CapturingLLM:
        def __init__(self, *a, **k):
            pass

        def complete(self, *, model, system, user, temperature=None, max_tokens=None):
            captured_systems.append(system)
            return "# Notes\n\ncontent"

    monkeypatch.setattr(pipeline, "LLMClient", _CapturingLLM)

    owner = users.create(conn, "marieke", "marspass123", "member")
    users.set_language(conn, owner.id, "nl")

    meeting = meetings.create(
        conn, user_id=owner.id, title="Sync", meeting_date=None, filename="1.mp3"
    )
    (settings.audio_dir / "1.mp3").write_bytes(b"fake-mp3")
    summary = next(p for p in prompts.list_active(conn) if p.name == "summary")
    jobs.create_transcribe_job(conn, meeting.id)
    jobs.create_notes_job(conn, meeting.id, summary.id)

    pipeline.process_meeting(conn, settings, meeting.id)

    assert meetings.get(conn, meeting.id).status == meetings.STATUS_DONE
    assert len(captured_systems) == 1
    assert "Dutch" in captured_systems[0]


def test_generate_more_notes_reuses_transcript(env):
    settings, conn = env
    user = _make_user(conn)
    meeting = meetings.create(
        conn, user_id=user.id, title="Sync", meeting_date=None, filename="1.mp3"
    )
    (settings.audio_dir / "1.mp3").write_bytes(b"fake")
    jobs.create_transcribe_job(conn, meeting.id)
    pipeline.process_meeting(conn, settings, meeting.id)
    assert meetings.get_transcript(conn, meeting.id) is not None

    # Now queue a notes job only — no re-transcription.
    decisions = next(p for p in prompts.list_active(conn) if p.name == "decisions")
    jobs.create_notes_job(conn, meeting.id, decisions.id)
    pipeline.process_meeting(conn, settings, meeting.id)

    notes_list = meetings.list_notes(conn, meeting.id)
    assert [n.prompt_name for n in notes_list] == ["decisions"]
    assert meetings.get(conn, meeting.id).status == meetings.STATUS_DONE
