"""Tests for speaker-attribution (diarization-ready) plumbing.

No backend we run produces speaker labels today, but the Transcriber contract
lets a future backend attach a ``speaker`` key to each segment. These tests
lock in that every consumer surfaces speakers when present and is byte-for-byte
unchanged when they're absent.
"""

from __future__ import annotations

from app import db as db_mod
from app import diarize, llm, meetings
from app.diarize import SpeakerTurn
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


# --- merge_speakers_into_segments (pure, no deps/models) -----------------


def test_speaker_label_maps_indices():
    assert diarize.speaker_label(0) == "Speaker A"
    assert diarize.speaker_label(1) == "Speaker B"
    assert diarize.speaker_label(25) == "Speaker Z"
    assert diarize.speaker_label(26) == "Speaker 27"


def test_merge_assigns_max_overlap_speaker():
    segments = [_seg("one", start=0.0, end=2.0), _seg("two", start=2.0, end=4.0)]
    turns = [
        SpeakerTurn(0.0, 2.1, "Speaker A"),
        SpeakerTurn(2.1, 4.0, "Speaker B"),
    ]
    out = diarize.merge_speakers_into_segments(segments, turns)
    assert out[0]["speaker"] == "Speaker A"
    assert out[1]["speaker"] == "Speaker B"
    # Input is not mutated.
    assert "speaker" not in segments[0]


def test_merge_leaves_unlabelled_when_no_overlap():
    segments = [_seg("orphan", start=10.0, end=11.0)]
    turns = [SpeakerTurn(0.0, 2.0, "Speaker A")]
    out = diarize.merge_speakers_into_segments(segments, turns)
    assert "speaker" not in out[0]


def test_merge_noop_without_turns():
    segments = [_seg("x", start=0.0, end=1.0)]
    assert diarize.merge_speakers_into_segments(segments, []) == segments
    assert diarize.merge_speakers_into_segments(None, []) is None


# --- apply_diarization best-effort behavior ------------------------------


def test_apply_diarization_disabled_is_noop():
    from app.config import Settings

    settings = Settings(SECRET_KEY="x" * 40)  # DIARIZATION_ENABLED defaults False
    segs = [_seg("hi", start=0.0, end=1.0)]
    assert diarize.apply_diarization(settings, "/nonexistent.wav", segs) is segs


def test_apply_diarization_swallows_errors(monkeypatch):
    from app.config import Settings

    settings = Settings(
        SECRET_KEY="x" * 40,
        DIARIZATION_ENABLED=True,
        DIARIZATION_SEGMENTATION_MODEL="/nope/seg.onnx",
        DIARIZATION_EMBEDDING_MODEL="/nope/emb.onnx",
    )
    segs = [_seg("hi", start=0.0, end=1.0)]
    # Missing model files -> build_diarizer raises -> apply returns segs unchanged.
    out = diarize.apply_diarization(settings, "/nonexistent.wav", segs)
    assert out == segs and "speaker" not in out[0]


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
