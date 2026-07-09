"""Unit tests for OpenAiCompatTranscriber's request shaping.

Verifies the multipart form knowts sends to the STT endpoint — in particular
that a language hint is forwarded (so whisper transcribes in the intended
language instead of autodetecting and sometimes returning English)."""

from __future__ import annotations

import httpx
import pytest

from app.transcriber import OpenAiCompatTranscriber, TranscriptionError


class _FakeResponse:
    def __init__(self, payload):
        self._payload = payload
        self.status_code = 200
        self.headers = {"content-type": "application/json"}

    def json(self):
        return self._payload


@pytest.fixture
def wav(tmp_path):
    p = tmp_path / "a.wav"
    p.write_bytes(b"RIFFfake")
    return p


def _capture_post(monkeypatch):
    captured = {}

    def fake_post(url, *, data, files, timeout):
        captured["url"] = url
        captured["data"] = data
        return _FakeResponse({"text": "hallo wereld", "segments": [], "language": "nl"})

    monkeypatch.setattr(httpx, "post", fake_post)
    return captured


def test_language_hint_is_sent_when_provided(monkeypatch, wav):
    captured = _capture_post(monkeypatch)
    t = OpenAiCompatTranscriber("http://link:8080/v1", "whisper-x")
    result = t.transcribe(wav, language="nl")
    assert captured["data"]["language"] == "nl"
    assert captured["data"]["model"] == "whisper-x"
    assert captured["data"]["response_format"] == "verbose_json"
    assert result.language == "nl"


def test_no_language_field_when_omitted(monkeypatch, wav):
    captured = _capture_post(monkeypatch)
    t = OpenAiCompatTranscriber("http://link:8080/v1", "whisper-x")
    t.transcribe(wav)
    assert "language" not in captured["data"]
