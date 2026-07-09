"""Optional in-container speaker diarization (PLAN follow-up).

Runs the sherpa-onnx (k2-fsa) diarization pipeline — VAD + segmentation +
speaker embedding + clustering — on the same 16 kHz mono WAV whisper already
transcribes, entirely on **CPU via ONNX** (no GPU, no ROCm, no HuggingFace
token). The resulting speaker turns are merged onto whisper's timestamped
segments, attaching a ``speaker`` key that the transcript panel, `.txt`/`.srt`
exports, and the notes prompts already surface.

Everything here is best-effort and off by default (``DIARIZATION_ENABLED``):
``apply_diarization`` never raises — if the deps or models are missing, or the
run fails, it logs and returns the segments unchanged, so transcription is
never broken by diarization.

``sherpa_onnx`` and ``numpy`` are imported lazily (only when enabled) and are
NOT in the base ``requirements.txt`` — install ``requirements-diarization.txt``
to use this feature. The pure ``merge_speakers_into_segments`` helper has no
such deps and is unit-tested directly.
"""

from __future__ import annotations

import logging
import wave
from dataclasses import dataclass
from pathlib import Path

from .config import Settings

log = logging.getLogger("knowts.diarize")


class DiarizationError(Exception):
    """Diarization could not run (missing deps/models or bad audio)."""


@dataclass(frozen=True)
class SpeakerTurn:
    start: float
    end: float
    speaker: str


def speaker_label(index: int) -> str:
    """Map sherpa-onnx's integer speaker ids to ``Speaker A``, ``Speaker B``…"""
    if 0 <= index < 26:
        return f"Speaker {chr(ord('A') + index)}"
    return f"Speaker {index + 1}"


def merge_speakers_into_segments(
    segments: list[dict] | None, turns: list[SpeakerTurn]
) -> list[dict] | None:
    """Attach a ``speaker`` to each whisper segment by maximum time overlap.

    Pure and dependency-free. Returns new segment dicts (never mutates the
    input); a segment that overlaps no turn is left without a ``speaker`` key,
    so downstream consumers treat it as unlabelled.
    """
    if not segments or not turns:
        return segments
    out: list[dict] = []
    for seg in segments:
        start = float(seg.get("start", 0) or 0)
        end = float(seg.get("end", start) or start)
        best_speaker: str | None = None
        best_overlap = 0.0
        for turn in turns:
            overlap = min(end, turn.end) - max(start, turn.start)
            if overlap > best_overlap:
                best_overlap = overlap
                best_speaker = turn.speaker
        new = dict(seg)
        if best_speaker is not None and best_overlap > 0:
            new["speaker"] = best_speaker
        out.append(new)
    return out


class Diarizer:
    """Thin wrapper over sherpa-onnx's ``OfflineSpeakerDiarization``.

    Constructed via :func:`build_diarizer`, which imports the optional deps and
    loads the ONNX models once. ``diarize`` reads a 16 kHz mono WAV and returns
    the speaker turns.
    """

    def __init__(self, impl, sample_rate: int):
        self._impl = impl
        self._sample_rate = sample_rate

    def diarize(self, wav_path: Path) -> list[SpeakerTurn]:
        import numpy as np

        with wave.open(str(wav_path), "rb") as wf:
            channels = wf.getnchannels()
            sample_rate = wf.getframerate()
            frames = wf.readframes(wf.getnframes())
        if sample_rate != self._sample_rate:
            raise DiarizationError(
                f"WAV sample rate {sample_rate} != model rate {self._sample_rate}"
            )
        data = np.frombuffer(frames, dtype=np.int16)
        if channels > 1:  # our pipeline emits mono, but be defensive
            data = data.reshape(-1, channels).mean(axis=1)
        samples = (data.astype(np.float32) / 32768.0)

        result = self._impl.process(samples).sort_by_start_time()
        return [
            SpeakerTurn(start=s.start, end=s.end, speaker=speaker_label(s.speaker))
            for s in result
        ]


def build_diarizer(
    settings: Settings, *, num_speakers: int | None = None
) -> Diarizer | None:
    """Construct a :class:`Diarizer` from settings, or ``None`` when diarization
    is disabled/unconfigured. Raises :class:`DiarizationError` if enabled but the
    deps or model files are unusable (the caller downgrades that to a warning).

    ``num_speakers`` (per-meeting, chosen at upload) overrides the global
    ``DIARIZATION_NUM_SPEAKERS`` when > 0; otherwise the global applies, and 0
    on both means auto-detect by clustering threshold."""
    if not settings.diarization_configured:
        return None

    seg_path = Path(settings.diarization_segmentation_model or "")
    emb_path = Path(settings.diarization_embedding_model or "")
    for label, path in (("segmentation", seg_path), ("embedding", emb_path)):
        if not path.is_file():
            raise DiarizationError(f"{label} model not found at {path}")

    try:
        import sherpa_onnx
    except ImportError as exc:  # optional dependency not installed
        raise DiarizationError(
            "sherpa-onnx is not installed (pip install -r requirements-diarization.txt)"
        ) from exc

    threads = max(1, settings.diarization_num_threads)
    effective_speakers = (
        num_speakers
        if num_speakers and num_speakers > 0
        else settings.diarization_num_speakers
    )
    config = sherpa_onnx.OfflineSpeakerDiarizationConfig(
        segmentation=sherpa_onnx.OfflineSpeakerSegmentationModelConfig(
            pyannote=sherpa_onnx.OfflineSpeakerSegmentationPyannoteModelConfig(
                model=str(seg_path)
            ),
            num_threads=threads,
            provider="cpu",
        ),
        embedding=sherpa_onnx.SpeakerEmbeddingExtractorConfig(
            model=str(emb_path), num_threads=threads, provider="cpu"
        ),
        clustering=sherpa_onnx.FastClusteringConfig(
            num_clusters=effective_speakers if effective_speakers > 0 else -1,
            threshold=settings.diarization_cluster_threshold,
        ),
        min_duration_on=0.3,
        min_duration_off=0.5,
    )
    if not config.validate():
        raise DiarizationError("invalid sherpa-onnx diarization config")

    impl = sherpa_onnx.OfflineSpeakerDiarization(config)
    return Diarizer(impl, impl.sample_rate)


def apply_diarization(
    settings: Settings,
    wav_path: Path,
    segments: list[dict] | None,
    *,
    num_speakers: int | None = None,
) -> list[dict] | None:
    """Best-effort: label ``segments`` with speakers, or return them unchanged.

    ``num_speakers`` is the per-meeting expected speaker count (0/None = auto).
    Never raises — any failure (disabled, missing deps/models, runtime error)
    is logged and the original segments are returned, so a diarization problem
    can never fail a transcription.
    """
    if not settings.diarization_enabled or not segments:
        return segments
    try:
        diarizer = build_diarizer(settings, num_speakers=num_speakers)
        if diarizer is None:
            return segments
        turns = diarizer.diarize(Path(wav_path))
        merged = merge_speakers_into_segments(segments, turns)
        log.info("diarization labelled %d segment(s) across turns", len(segments))
        return merged
    except Exception as exc:  # noqa: BLE001 — diarization must never break STT
        log.warning("diarization failed, keeping unlabelled transcript: %s", exc)
        return segments
