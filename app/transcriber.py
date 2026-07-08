"""Speech-to-text client behind a small interface (PLAN.md §5, §10 step 8).

knowts codes against one contract:

    POST {STT_BASE_URL}/audio/transcriptions   (multipart: file, model,
    response_format=verbose_json) -> { text, segments?, language? }

The concrete ``OpenAiCompatTranscriber`` talks to llama-swap's whisper entry
(or any OpenAI-compatible STT). Keeping it behind ``Transcriber`` means a future
backend swap (see plan §5 alternatives A/B) is a new adapter, not a refactor.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import httpx

log = logging.getLogger("knowts.stt")


class TranscriptionError(Exception):
    """STT service unreachable or returned an unusable response."""


@dataclass(frozen=True)
class TranscriptResult:
    text: str
    segments: list[dict] | None
    language: str | None


class Transcriber(Protocol):
    def transcribe(
        self, wav_path: Path, *, model: str | None = None, language: str | None = None
    ) -> TranscriptResult:
        ...


class OpenAiCompatTranscriber:
    """Calls an OpenAI-compatible ``/audio/transcriptions`` endpoint.

    ``base_url`` includes the ``/v1`` suffix (e.g. ``http://link:8080/v1``).
    Timeouts are generous: a model swap + cold load plus transcribing a long
    meeting is minutes, not seconds (plan §5).
    """

    def __init__(
        self,
        base_url: str,
        default_model: str,
        *,
        connect_timeout: float = 15.0,
        read_timeout: float = 60.0 * 30,
    ):
        self._base_url = base_url.rstrip("/")
        self._default_model = default_model
        self._timeout = httpx.Timeout(read_timeout, connect=connect_timeout)

    @property
    def endpoint(self) -> str:
        return f"{self._base_url}/audio/transcriptions"

    def transcribe(
        self, wav_path: Path, *, model: str | None = None, language: str | None = None
    ) -> TranscriptResult:
        model_name = model or self._default_model
        try:
            with open(wav_path, "rb") as fh:
                files = {"file": (wav_path.name, fh, "audio/wav")}
                data = {"model": model_name, "response_format": "verbose_json"}
                # Pin the source language when known so whisper transcribes in
                # that language instead of autodetecting (and sometimes guessing
                # wrong, returning an English-translated transcript).
                if language:
                    data["language"] = language
                resp = httpx.post(
                    self.endpoint, data=data, files=files, timeout=self._timeout
                )
        except (httpx.HTTPError, OSError) as exc:
            raise TranscriptionError(
                f"could not reach STT service at {self.endpoint}: {exc}"
            ) from exc

        if resp.status_code >= 400:
            raise TranscriptionError(
                f"STT service returned {resp.status_code}: {resp.text[:500]}"
            )

        return _parse_response(resp)


def _parse_response(resp: httpx.Response) -> TranscriptResult:
    ctype = resp.headers.get("content-type", "")
    if "application/json" in ctype:
        try:
            payload = resp.json()
        except json.JSONDecodeError as exc:
            raise TranscriptionError(f"STT returned invalid JSON: {exc}") from exc
        return _from_json(payload)
    # Some backends honour only text/plain; degrade gracefully.
    text = resp.text.strip()
    if not text:
        raise TranscriptionError("STT returned an empty transcript")
    return TranscriptResult(text=text, segments=None, language=None)


def _from_json(payload: dict) -> TranscriptResult:
    if not isinstance(payload, dict):
        raise TranscriptionError("STT JSON was not an object")
    text = (payload.get("text") or "").strip()
    segments = payload.get("segments")
    if segments is not None and not isinstance(segments, list):
        segments = None
    # verbose_json may put text only in segments; reconstruct if the top-level
    # text is missing.
    if not text and segments:
        text = " ".join(str(s.get("text", "")).strip() for s in segments).strip()
    if not text:
        raise TranscriptionError("STT response contained no transcript text")
    return TranscriptResult(
        text=text,
        segments=segments,
        language=payload.get("language"),
    )
