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
    last_language = None

    def __init__(self, *args, **kwargs):
        pass

    def transcribe(self, wav_path, *, model=None, language=None):
        _FakeTranscriber.last_language = language
        return TranscriptResult(
            text="hello world this is the meeting",
            segments=[
                {"start": 0.0, "end": 1.5, "text": "hello world"},
                {"start": 1.5, "end": 3.0, "text": "this is the meeting"},
            ],
            language=language or "en",
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

        def transcribe(self, wav_path, *, model=None, language=None):
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


def _transcribe_only(conn, settings, user, *, language="auto"):
    meeting = meetings.create(
        conn, user_id=user.id, title="Sync", meeting_date=None, filename="1.mp3",
        language=language,
    )
    (settings.audio_dir / "1.mp3").write_bytes(b"fake-mp3")
    jobs.create_transcribe_job(conn, meeting.id)
    pipeline.process_meeting(conn, settings, meeting.id)
    return meeting


def test_stt_language_autodetects_when_meeting_is_auto(env):
    settings, conn = env
    _FakeTranscriber.last_language = None
    user = _make_user(conn)
    _transcribe_only(conn, settings, user, language="auto")
    # No hint sent -> whisper autodetects.
    assert _FakeTranscriber.last_language is None


def test_stt_language_pinned_from_meeting_choice(env):
    settings, conn = env
    _FakeTranscriber.last_language = None
    user = _make_user(conn)
    meeting = _transcribe_only(conn, settings, user, language="nl")
    # The meeting's chosen language pins transcription...
    assert _FakeTranscriber.last_language == "nl"
    # ...and the stored transcript reflects it instead of "english".
    assert meetings.get_transcript(conn, meeting.id).language == "nl"


def test_stt_language_explicit_setting_overrides_meeting_choice(env):
    settings, conn = env
    settings.stt_language = "de"  # deployment-wide pin wins over the meeting choice
    _FakeTranscriber.last_language = None
    user = _make_user(conn)
    _transcribe_only(conn, settings, user, language="nl")
    assert _FakeTranscriber.last_language == "de"


def test_stt_language_auto_setting_ignores_meeting_choice(env):
    settings, conn = env
    settings.stt_language = "auto"
    _FakeTranscriber.last_language = None
    user = _make_user(conn)
    _transcribe_only(conn, settings, user, language="nl")
    assert _FakeTranscriber.last_language is None


def test_notes_use_dutch_override_for_dutch_meeting(env, monkeypatch):
    settings, conn = env

    captured_systems = []

    class _CapturingLLM:
        def __init__(self, *a, **k):
            pass

        def complete(self, *, model, system, user, temperature=None, max_tokens=None):
            captured_systems.append(system)
            return "# Notes\n\ncontent"

    monkeypatch.setattr(pipeline, "LLMClient", _CapturingLLM)

    user = _make_user(conn)
    meeting = meetings.create(
        conn, user_id=user.id, title="Sync", meeting_date=None, filename="1.mp3",
        language="nl",
    )
    (settings.audio_dir / "1.mp3").write_bytes(b"fake-mp3")
    summary = next(p for p in prompts.list_active(conn) if p.name == "summary")
    jobs.create_transcribe_job(conn, meeting.id)
    jobs.create_notes_job(conn, meeting.id, summary.id)

    pipeline.process_meeting(conn, settings, meeting.id)

    assert meetings.get(conn, meeting.id).status == meetings.STATUS_DONE
    assert len(captured_systems) == 1
    assert "Dutch" in captured_systems[0]


def test_notes_receive_speaker_attributed_transcript(env, monkeypatch):
    settings, conn = env

    captured_users = []

    class _CapturingLLM:
        def __init__(self, *a, **k):
            pass

        def complete(self, *, model, system, user, temperature=None, max_tokens=None):
            captured_users.append(user)
            return "# Notes\n\ncontent"

    class _DiarizingTranscriber:
        def __init__(self, *a, **k):
            pass

        def transcribe(self, wav_path, *, model=None, language=None):
            from app.transcriber import TranscriptResult

            return TranscriptResult(
                text="hi there good thanks",
                segments=[
                    {"start": 0.0, "end": 1.0, "text": "hi there", "speaker": "Speaker A"},
                    {"start": 1.0, "end": 2.0, "text": "good thanks", "speaker": "Speaker B"},
                ],
                language="en",
            )

    monkeypatch.setattr(pipeline, "OpenAiCompatTranscriber", _DiarizingTranscriber)
    monkeypatch.setattr(pipeline, "LLMClient", _CapturingLLM)

    user = _make_user(conn)
    meeting = meetings.create(
        conn, user_id=user.id, title="Sync", meeting_date=None, filename="1.mp3"
    )
    (settings.audio_dir / "1.mp3").write_bytes(b"fake-mp3")
    summary = next(p for p in prompts.list_active(conn) if p.name == "summary")
    jobs.create_transcribe_job(conn, meeting.id)
    jobs.create_notes_job(conn, meeting.id, summary.id)

    pipeline.process_meeting(conn, settings, meeting.id)

    assert meetings.get(conn, meeting.id).status == meetings.STATUS_DONE
    assert len(captured_users) == 1
    # The prompt the LLM saw is speaker-attributed, not the plain transcript.
    assert "Speaker A: hi there" in captured_users[0]
    assert "Speaker B: good thanks" in captured_users[0]


def test_diarization_labels_segments_when_enabled(env, monkeypatch):
    settings, conn = env
    from app import diarize
    from app.diarize import SpeakerTurn

    # Enable diarization and inject a fake diarizer (no sherpa-onnx / models).
    settings.diarization_enabled = True

    class _FakeDiarizer:
        def diarize(self, wav_path):
            return [
                SpeakerTurn(0.0, 1.5, "Speaker A"),
                SpeakerTurn(1.5, 3.0, "Speaker B"),
            ]

    monkeypatch.setattr(
        diarize, "build_diarizer", lambda s, num_speakers=None: _FakeDiarizer()
    )

    user = _make_user(conn)
    meeting = meetings.create(
        conn, user_id=user.id, title="Sync", meeting_date=None, filename="1.mp3"
    )
    (settings.audio_dir / "1.mp3").write_bytes(b"fake-mp3")
    jobs.create_transcribe_job(conn, meeting.id)
    pipeline.process_meeting(conn, settings, meeting.id)

    # The two whisper segments (0-1.5s, 1.5-3s from _FakeTranscriber) are now
    # labelled by max overlap with the fake turns.
    transcript = meetings.get_transcript(conn, meeting.id)
    speakers = [s.get("speaker") for s in transcript.segments]
    assert speakers == ["Speaker A", "Speaker B"]


def test_meeting_speaker_count_reaches_diarizer(env, monkeypatch):
    settings, conn = env
    from app import diarize
    from app.diarize import SpeakerTurn

    settings.diarization_enabled = True
    captured = {}

    class _FakeDiarizer:
        def diarize(self, wav_path):
            return [SpeakerTurn(0.0, 3.0, "Speaker A")]

    def _fake_build(s, num_speakers=None):
        captured["num_speakers"] = num_speakers
        return _FakeDiarizer()

    monkeypatch.setattr(diarize, "build_diarizer", _fake_build)

    user = _make_user(conn)
    meeting = meetings.create(
        conn, user_id=user.id, title="Sync", meeting_date=None, filename="1.mp3",
        diarization_num_speakers=3,
    )
    (settings.audio_dir / "1.mp3").write_bytes(b"fake-mp3")
    jobs.create_transcribe_job(conn, meeting.id)
    pipeline.process_meeting(conn, settings, meeting.id)

    assert captured["num_speakers"] == 3


def test_diarization_off_leaves_segments_unlabelled(env):
    settings, conn = env
    # settings.diarization_enabled defaults False.
    user = _make_user(conn)
    meeting = meetings.create(
        conn, user_id=user.id, title="Sync", meeting_date=None, filename="1.mp3"
    )
    (settings.audio_dir / "1.mp3").write_bytes(b"fake-mp3")
    jobs.create_transcribe_job(conn, meeting.id)
    pipeline.process_meeting(conn, settings, meeting.id)

    transcript = meetings.get_transcript(conn, meeting.id)
    assert all("speaker" not in s for s in transcript.segments)


def test_retry_requeues_failed_notes_job_and_reruns(env, monkeypatch):
    settings, conn = env
    from app.llm import LLMError

    boom = {"fail": True}

    class _FlakyLLM:
        def __init__(self, *a, **k):
            pass

        def complete(self, *, model, system, user, temperature=None, max_tokens=None):
            if boom["fail"]:
                raise LLMError("LLM cold-start timeout")
            return "# Notes\n\nrecovered"

    monkeypatch.setattr(pipeline, "LLMClient", _FlakyLLM)

    user = _make_user(conn)
    meeting = meetings.create(
        conn, user_id=user.id, title="Sync", meeting_date=None, filename="1.mp3"
    )
    (settings.audio_dir / "1.mp3").write_bytes(b"fake-mp3")
    summary = next(p for p in prompts.list_active(conn) if p.name == "summary")
    jobs.create_transcribe_job(conn, meeting.id)
    jobs.create_notes_job(conn, meeting.id, summary.id)

    # First pass: transcript succeeds, notes generation fails.
    pipeline.process_meeting(conn, settings, meeting.id)
    notes_job = [j for j in jobs.list_for_meeting(conn, meeting.id) if j.kind == "notes"][0]
    assert notes_job.status == jobs.STATUS_ERROR
    assert meetings.get_transcript(conn, meeting.id) is not None  # transcript kept
    assert meetings.list_notes(conn, meeting.id) == []

    # Retry requeues only the failed job (transcribe stays done).
    assert jobs.has_failed_jobs(conn, meeting.id) is True
    assert jobs.retry_failed_jobs(conn, meeting.id) == 1
    requeued = [j for j in jobs.list_for_meeting(conn, meeting.id) if j.kind == "notes"][0]
    assert requeued.status == jobs.STATUS_QUEUED
    # The prompt parameter survived the requeue (step still carries prompt:<id>).
    assert requeued.prompt_id == summary.id
    tjob = [j for j in jobs.list_for_meeting(conn, meeting.id) if j.kind == "transcribe"][0]
    assert tjob.status == jobs.STATUS_DONE  # not re-run

    # Second pass with a healthy LLM: the note generates, no re-transcription.
    boom["fail"] = False
    pipeline.process_meeting(conn, settings, meeting.id)
    assert meetings.get(conn, meeting.id).status == meetings.STATUS_DONE
    notes_list = meetings.list_notes(conn, meeting.id)
    assert [n.prompt_name for n in notes_list] == ["summary"]
    assert "recovered" in notes_list[0].markdown


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
