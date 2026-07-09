"""Tests for speaker-attribution (diarization-ready) plumbing.

No backend we run produces speaker labels today, but the Transcriber contract
lets a future backend attach a ``speaker`` key to each segment. These tests
lock in that every consumer surfaces speakers when present and is byte-for-byte
unchanged when they're absent.
"""

from __future__ import annotations

from app import db as db_mod
from app import llm, meetings
from app.routers.meetings import _to_srt


def _seg(text, speaker=None, start=0.0, end=1.0):
    s = {"start": start, "end": end, "text": text}
    if speaker is not None:
        s["speaker"] = speaker
    return s


# --- speaker_attributed_text ---------------------------------------------


def test_attributed_text_falls_back_without_speakers():
    segs = [_seg("hello"), _seg("world")]
    assert meetings.speaker_attributed_text(segs, "PLAIN") == "PLAIN"
    assert meetings.speaker_attributed_text(None, "PLAIN") == "PLAIN"
    assert meetings.speaker_attributed_text([], "PLAIN") == "PLAIN"


def test_attributed_text_groups_consecutive_speakers():
    segs = [
        _seg("hi there", "Speaker A"),
        _seg("how are you", "Speaker A"),
        _seg("good thanks", "Speaker B"),
        _seg("back to me", "Speaker A"),
    ]
    out = meetings.speaker_attributed_text(segs, "PLAIN")
    assert out == (
        "Speaker A: hi there how are you\n"
        "Speaker B: good thanks\n"
        "Speaker A: back to me"
    )


def test_attributed_text_handles_partial_speakers():
    # Some segments labelled, some not -> still attributed (unlabelled lines
    # pass through without a prefix).
    segs = [_seg("intro"), _seg("a point", "Speaker A")]
    out = meetings.speaker_attributed_text(segs, "PLAIN")
    assert out == "intro\nSpeaker A: a point"


# --- segment_speaker -----------------------------------------------------


def test_segment_speaker_normalizes_blank_and_missing():
    assert meetings.segment_speaker({"text": "x"}) is None
    assert meetings.segment_speaker({"text": "x", "speaker": ""}) is None
    assert meetings.segment_speaker({"text": "x", "speaker": "  "}) is None
    assert meetings.segment_speaker({"text": "x", "speaker": "Speaker A"}) == "Speaker A"


# --- SRT export ----------------------------------------------------------


def test_srt_prefixes_speaker_when_present():
    srt = _to_srt([_seg("hallo", "Speaker A", 0.0, 1.5)])
    assert "Speaker A: hallo" in srt


def test_srt_unchanged_without_speaker():
    srt = _to_srt([_seg("hallo", None, 0.0, 1.5)])
    assert "hallo" in srt
    assert "Speaker" not in srt


# --- chunk_segments keeps attribution ------------------------------------


def test_chunk_segments_prefixes_speaker():
    segs = [_seg("first", "Speaker A"), _seg("second", "Speaker B")]
    chunks = llm.chunk_segments(segs, context_tokens=32768, headroom_tokens=2048)
    joined = "\n".join(chunks)
    assert "Speaker A: first" in joined
    assert "Speaker B: second" in joined


def test_chunk_segments_plain_without_speaker():
    segs = [_seg("first"), _seg("second")]
    chunks = llm.chunk_segments(segs, context_tokens=32768, headroom_tokens=2048)
    assert "Speaker" not in "\n".join(chunks)


# --- storage round-trips the speaker key ---------------------------------


def test_segments_roundtrip_speaker_key(tmp_path):
    conn = db_mod.connect(tmp_path / "t.db")
    db_mod.run_migrations(conn)
    conn.execute("INSERT INTO users (username, password_hash) VALUES ('u', 'x')")
    conn.execute(
        "INSERT INTO meetings (user_id, title, status) VALUES (1, 'M', 'done')"
    )
    conn.commit()
    meetings.save_transcript(
        conn, 1, text="hi", segments=[_seg("hi", "Speaker A")], language="en"
    )
    got = meetings.get_transcript(conn, 1)
    assert got.segments[0]["speaker"] == "Speaker A"
    conn.close()
