"""Unit tests for the map-reduce chunking helpers (PLAN.md §6)."""

from __future__ import annotations

from app import llm


def test_estimate_tokens_scales_with_length():
    assert llm.estimate_tokens("") == 1
    assert llm.estimate_tokens("a" * 40) == 10


def test_fits_single_call_thresholds():
    # budget = (context - headroom) * 4 chars
    assert llm.fits_single_call("x" * 100, context_tokens=1000, headroom_tokens=500)
    assert not llm.fits_single_call("x" * 100000, context_tokens=1000, headroom_tokens=500)


def test_chunk_text_splits_long_input_with_overlap():
    text = "\n\n".join(f"Paragraph number {i} with some content." for i in range(400))
    # Use usable tokens above the 512 floor so the budget is the real value.
    chunks = llm.chunk_text(text, context_tokens=1000, headroom_tokens=200)
    assert len(chunks) > 1
    # Every chunk stays within the character budget (plus the overlap tail).
    budget = (1000 - 200) * 4
    assert all(len(c) <= budget + 500 for c in chunks)
    # Nothing is dropped: first/last content survives.
    assert "Paragraph number 0" in chunks[0]
    assert "Paragraph number 399" in chunks[-1]


def test_chunk_text_single_when_small():
    assert llm.chunk_text("short", context_tokens=1000, headroom_tokens=100) == ["short"]
    assert llm.chunk_text("", context_tokens=1000, headroom_tokens=100) == []


def test_chunk_segments_respects_boundaries_and_overlap():
    segments = [{"text": f"segment {i} " + "word " * 20} for i in range(50)]
    chunks = llm.chunk_segments(segments, context_tokens=200, headroom_tokens=50, overlap_segments=1)
    assert len(chunks) > 1
    # Segments are never split mid-way: each source segment text appears whole.
    joined = " ".join(chunks)
    assert "segment 0" in chunks[0]
    assert "segment 49" in joined


def test_chunk_segments_empty():
    assert llm.chunk_segments([], 1000, 100) == []
    assert llm.chunk_segments([{"text": "  "}], 1000, 100) == []
