"""LLM client + long-transcript chunking (PLAN.md §6, §10 step 9).

Thin wrapper over the ``openai`` SDK pointed at llama-swap's OpenAI-compatible
base URL. The pure helpers (``estimate_tokens``, ``chunk_segments``,
``chunk_text``) carry the map-reduce logic and are unit-tested without a live
model.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Sequence

from openai import OpenAI, OpenAIError

log = logging.getLogger("knowts.llm")

# Roughly 4 characters per token — good enough to decide single-shot vs. chunked
# without pulling a tokenizer into the image (plan §6).
_CHARS_PER_TOKEN = 4


class LLMError(Exception):
    """LLM service unreachable or returned an error."""


def estimate_tokens(text: str) -> int:
    return max(1, len(text) // _CHARS_PER_TOKEN)


@dataclass(frozen=True)
class Chunk:
    text: str
    index: int
    total: int


def _budget_chars(context_tokens: int, headroom_tokens: int) -> int:
    """Character budget for one chunk's transcript slice."""
    usable = max(context_tokens - headroom_tokens, 512)
    return usable * _CHARS_PER_TOKEN


def fits_single_call(text: str, context_tokens: int, headroom_tokens: int) -> bool:
    return len(text) <= _budget_chars(context_tokens, headroom_tokens)


def chunk_segments(
    segments: Sequence[dict],
    context_tokens: int,
    headroom_tokens: int,
    *,
    overlap_segments: int = 1,
) -> list[str]:
    """Group timestamped segments into transcript slices under the char budget.

    Splits on segment boundaries (never mid-sentence) and overlaps by
    ``overlap_segments`` so context isn't lost at the seams.
    """
    budget = _budget_chars(context_tokens, headroom_tokens)
    texts = [str(s.get("text", "")).strip() for s in segments]
    texts = [t for t in texts if t]
    if not texts:
        return []

    chunks: list[str] = []
    start = 0
    n = len(texts)
    while start < n:
        cur: list[str] = []
        size = 0
        i = start
        while i < n:
            piece = texts[i]
            add = len(piece) + 1
            if cur and size + add > budget:
                break
            cur.append(piece)
            size += add
            i += 1
        chunks.append(" ".join(cur))
        if i >= n:
            break
        start = max(i - overlap_segments, start + 1)
    return chunks


def chunk_text(
    text: str,
    context_tokens: int,
    headroom_tokens: int,
    *,
    overlap_chars: int = 400,
) -> list[str]:
    """Fallback chunker when there are no segments: split on paragraph/sentence
    boundaries under the char budget, with a small character overlap."""
    budget = _budget_chars(context_tokens, headroom_tokens)
    text = text.strip()
    if len(text) <= budget:
        return [text] if text else []

    # Prefer paragraph boundaries, then sentence-ish, then hard cut.
    units = _split_units(text)
    chunks: list[str] = []
    cur = ""
    for unit in units:
        if cur and len(cur) + len(unit) + 1 > budget:
            chunks.append(cur.strip())
            tail = cur[-overlap_chars:] if overlap_chars else ""
            cur = (tail + " " + unit).strip()
        else:
            cur = (cur + " " + unit).strip() if cur else unit
    if cur.strip():
        chunks.append(cur.strip())
    return chunks


def _split_units(text: str) -> list[str]:
    import re

    paras = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    units: list[str] = []
    for para in paras:
        if len(para) <= 2000:
            units.append(para)
            continue
        # Break oversized paragraphs on sentence boundaries.
        sentences = re.split(r"(?<=[.!?])\s+", para)
        units.extend(s.strip() for s in sentences if s.strip())
    return units


class LLMClient:
    """OpenAI-compatible chat client (llama-swap). Generous timeouts for cold
    model swaps."""

    def __init__(self, base_url: str, *, timeout: float = 60.0 * 20):
        # llama-swap ignores the key, but the SDK requires a non-empty string.
        self._client = OpenAI(base_url=base_url, api_key="not-needed", timeout=timeout)

    def list_models(self) -> list[str]:
        try:
            resp = self._client.models.list()
        except OpenAIError as exc:
            raise LLMError(f"could not list models: {exc}") from exc
        return [m.id for m in resp.data]

    def complete(
        self,
        *,
        model: str,
        system: str | None,
        user: str,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> str:
        messages: list[dict] = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": user})
        kwargs: dict = {"model": model, "messages": messages}
        if temperature is not None:
            kwargs["temperature"] = temperature
        if max_tokens is not None:
            kwargs["max_tokens"] = max_tokens
        try:
            resp = self._client.chat.completions.create(**kwargs)
        except OpenAIError as exc:
            raise LLMError(f"chat completion failed: {exc}") from exc
        if not resp.choices:
            raise LLMError("LLM returned no choices")
        content = resp.choices[0].message.content
        if not content or not content.strip():
            raise LLMError("LLM returned empty content")
        return content.strip()
