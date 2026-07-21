# knowts — single-container web app (PLAN.md §2).
# ffmpeg is baked in now so Phase 2's MP3 → WAV conversion needs no rebuild.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    DATA_DIR=/data

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt requirements-diarization.txt ./
# Base deps + the optional CPU speaker-diarization deps (sherpa-onnx, numpy).
# They're inert unless DIARIZATION_ENABLED=true and the ONNX models are mounted
# under /data (see README "Enabling diarization").
RUN pip install --no-cache-dir -r requirements.txt -r requirements-diarization.txt

COPY app ./app

# Non-root runtime; /data is a mounted volume owned by this user.
RUN useradd --create-home --uid 10001 knowts \
    && mkdir -p /data \
    && chown -R knowts:knowts /data /app
USER knowts

VOLUME ["/data"]
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/healthz').status==200 else 1)"

# --proxy-headers: when a TLS-terminating reverse proxy (the bundled Caddy, or
# your own) sits in front, honour X-Forwarded-Proto so redirect URLs and the
# Secure cookie flag reflect the real https scheme. FORWARDED_ALLOW_IPS controls
# which upstream peers are trusted for those headers (default "*" = any; set it
# to your proxy's source IP to stop LAN clients spoofing the scheme). Runs via
# `sh -c ... exec` so the env var is interpolated while uvicorn stays PID 1.
CMD ["sh", "-c", "exec uvicorn app.main:app --host 0.0.0.0 --port 8000 --proxy-headers --forwarded-allow-ips \"${FORWARDED_ALLOW_IPS:-*}\""]
