"""ffmpeg audio helpers (PLAN.md §1 step 3, §10 step 7).

whisper.cpp expects 16 kHz mono PCM WAV. We convert every upload with ffmpeg
before sending it to the STT endpoint, and probe the original for its duration
so the meeting archive can show it. Both shell out to the ffmpeg/ffprobe
binaries baked into the image (see Dockerfile).
"""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
from pathlib import Path

log = logging.getLogger("knowts.audio")


class AudioError(Exception):
    """Raised when ffmpeg/ffprobe fail or are missing."""


def _binary(name: str) -> str:
    path = shutil.which(name)
    if path is None:
        raise AudioError(
            f"{name} not found on PATH — it must be installed in the image."
        )
    return path


def probe_duration_s(src: Path) -> float | None:
    """Best-effort duration in seconds via ffprobe. Returns None if unknown."""
    try:
        ffprobe = _binary("ffprobe")
    except AudioError:
        log.warning("ffprobe unavailable; skipping duration probe")
        return None
    try:
        out = subprocess.run(
            [
                ffprobe,
                "-v", "error",
                "-show_entries", "format=duration",
                "-of", "json",
                str(src),
            ],
            capture_output=True,
            text=True,
            timeout=60,
            check=True,
        )
        data = json.loads(out.stdout or "{}")
        value = data.get("format", {}).get("duration")
        return float(value) if value is not None else None
    except (subprocess.SubprocessError, ValueError, KeyError) as exc:
        log.warning("duration probe failed for %s: %s", src, exc)
        return None


def to_wav_16k_mono(src: Path, dst: Path) -> Path:
    """Convert any audio file to 16 kHz mono signed-16-bit PCM WAV.

    Raises AudioError on failure so the pipeline can mark the job errored with a
    useful message.
    """
    ffmpeg = _binary("ffmpeg")
    dst.parent.mkdir(parents=True, exist_ok=True)
    try:
        subprocess.run(
            [
                ffmpeg,
                "-nostdin",
                "-y",
                "-i", str(src),
                "-ar", "16000",
                "-ac", "1",
                "-c:a", "pcm_s16le",
                str(dst),
            ],
            capture_output=True,
            text=True,
            timeout=60 * 30,
            check=True,
        )
    except subprocess.CalledProcessError as exc:
        tail = (exc.stderr or "").strip().splitlines()[-3:]
        raise AudioError(
            "ffmpeg conversion failed: " + " ".join(tail) if tail else "ffmpeg failed"
        ) from exc
    except subprocess.SubprocessError as exc:
        raise AudioError(f"ffmpeg conversion failed: {exc}") from exc
    if not dst.exists() or dst.stat().st_size == 0:
        raise AudioError("ffmpeg produced no output")
    return dst
