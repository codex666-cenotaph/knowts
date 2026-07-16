# knowts
convert meeting mp3 files to meeting notes on your own setup.

See [PLAN.md](./PLAN.md) for the full design. Phase 0 (self-hosted whisper STT
on the `link` machine) lives in [`deploy/`](./deploy/). This repo now also
contains **Phase 1** (skeleton + auth), **Phase 2** (the pipeline core),
**Phase 3** (the full web UI), and **Phase 4** (hardening & docs).

## What's here (Phase 1)

- FastAPI app with server-rendered Jinja2/HTMX pages, no build step.
- SQLite with a small ordered-migration runner (`app/db.py`); the full data
  model from the plan is created up front.
- Auth: argon2id password hashing, server-side sessions in a signed HTTP-only
  cookie (idle + absolute expiry), per-session CSRF tokens on every form,
  and in-process login rate limiting (per-IP + per-account).
- First-run admin bootstrap from `ADMIN_USER` / `ADMIN_PASSWORD`.
- Pages: login/logout, home dashboard, profile (change own password), and
  admin → users (create, deactivate/reactivate, reset password).

## What's here (Phase 2 — pipeline core)

- **Upload → meeting**: authenticated MP3 (and other audio) upload with a size
  guard; each upload creates a *meeting* that permanently groups the original
  audio, its transcript, and every set of generated notes. Ownership is enforced
  on every meeting route (members see only their own; admins see all).
- **Jobs + worker**: a `transcribe` job and one `notes` job per selected prompt
  are queued and run serially by a single in-process background worker
  (`app/jobs.py`), so transcription and note generation never contend for the
  shared GPU behind llama-swap. Interrupted jobs are re-queued on restart.
- **Transcription**: ffmpeg converts audio to 16 kHz mono WAV (`app/audio.py`),
  then `OpenAiCompatTranscriber` (`app/transcriber.py`) POSTs it to
  `/v1/audio/transcriptions` (`verbose_json`) behind a small `Transcriber`
  interface, so swapping STT backends is a new adapter, not a refactor.
- **Notes**: the LLM client (`app/llm.py`) plus the notes generator
  (`app/notes.py`) render each prompt against the transcript — single-shot when
  it fits the context window, otherwise chunked map-reduce (segment-boundary
  chunking with overlap, then a reduce/merge pass).
- **Prompts**: the official starter set (`summary`, `action-items`, `decisions`,
  `minutes`, `qa-highlights`) is seeded on first run.
- **Meeting UI**: upload page, meetings archive, and a detail page with an audio
  player (HTTP range streaming), transcript panel with `.txt`/`.srt` download,
  rendered Markdown notes, "generate more notes" against the stored transcript
  (no re-transcription), live HTMX status polling, and delete.

## What's here (Phase 3 — full web UI)

- **Meetings archive** with title search and a meeting-date range filter
  (ownership-scoped: members see their own, admins see all).
- **Meeting detail polish**: generated note sets are shown as tabs (one per
  prompt run), each with a copy button and a `.md` download, alongside the
  existing audio player and `.txt`/`.srt` transcript exports.
- **Prompt manager** (`/prompts`): list/search with official-vs-personal badges,
  create, edit, clone, and archive. Edits are versioned (each save appends a new
  prompt version so past notes stay attributable) and enforced server-side —
  official prompts are editable by admins only, personal prompts by their owner
  or an admin, and clone is available to everyone on every prompt. The editor
  offers a live model dropdown from llama-swap's `/v1/models`, `{transcript}`
  placeholder validation, and a **test-run** that previews a prompt against a
  transcript snippet before saving.
- **Profile language preference**: a language select (English/Dutch) on the
  profile page (`app/i18n.py`) switches the whole interface, and becomes the
  default for the per-meeting language picker below.
- **Per-meeting language**: the upload page has a language dropdown (English /
  Dutch / Auto-detect) defaulting to the user's interface language. The chosen
  language (a) pins the whisper transcription language so a Dutch meeting is
  transcribed in Dutch instead of whisper autodetecting and sometimes returning
  an English transcript, and (b) makes note generation (and the prompt
  manager's test-run preview) append a "respond only in `<language>`"
  instruction — overriding the starter prompts' default "same language as the
  transcript" behavior. Auto-detect leaves both to the model/transcript.
  A deployment can still force one STT language for everyone with `STT_LANGUAGE`
  (`auto` = always autodetect, or a fixed ISO-639-1 code), which overrides the
  per-meeting choice.
- **Speaker diarization (optional, CPU, off by default)**: `whisper-server` can't
  label speakers itself, so knowts can run diarization in-container via
  **sherpa-onnx** — the full VAD + segmentation + embedding + clustering pipeline as
  **ONNX on CPU** (no GPU, no ROCm, no HuggingFace token; non-gated models).
  When `DIARIZATION_ENABLED=true`, each transcript segment gets a `speaker`, which
  the transcript panel, `.txt`/`.srt` exports, and the notes prompts all surface and
  attribute automatically. When off (default), transcripts render exactly as before.
  Diarization is best-effort — if the deps/models are missing or a run fails, the
  transcript is saved unlabelled and transcription is never blocked. See
  `app/diarize.py` and the setup below.
  When enabled, the upload form shows a **Speakers** field (0 = auto-detect) — set
  the exact participant count per meeting if auto-detect splits one person across
  several speakers; it overrides the global `DIARIZATION_NUM_SPEAKERS` for that
  meeting. `DIARIZATION_CLUSTER_THRESHOLD` (higher = fewer speakers) is the global
  knob for the auto case.

## What's here (Phase 4 — hardening & docs)

- **Every failure is caught and surfaced.** Each pipeline step that can fail —
  ffmpeg conversion, an unreachable/slow STT service, an LLM cold-start timeout
  or bad response, oversized/empty uploads, unsupported formats — is turned into
  a job-level error message and shown on the meeting page instead of crashing the
  worker. Uploads are guarded by `MAX_UPLOAD_MB` and an allow-list of audio
  extensions; STT/LLM clients use generous timeouts (llama-swap model swaps take
  a while) and the worker never dies on an exception.
- **Retry from the UI.** When a step errors, the meeting page shows a **Retry
  failed jobs** button (`POST /meetings/{id}/retry`) that requeues only the failed
  steps against the same upload — a failed transcription reruns without re-uploading,
  and a failed note reruns without re-transcribing (the stored transcript is
  reused). A notes job's target prompt lives in its own `jobs.prompt_id` column so
  the parameter survives a requeue even after the live progress label overwrote the
  old `step`-encoded value.
- **Crash recovery.** A single in-process worker drains the job queue serially, so
  at most one transcription runs at a time — the concurrency guard the plan calls
  for (§10 step 15), keeping transcription and note generation from contending for
  the shared GPU behind llama-swap. On restart, any job left `running` by a crashed
  worker is requeued and its meeting re-enqueued, so interrupted work resumes
  instead of hanging.

### Enabling diarization

```sh
# 1. Extra deps (CPU-only; not in the base image):
pip install -r requirements-diarization.txt

# 2. Download the two non-gated ONNX models (no HF token needed) — e.g.:
mkdir -p ./data/models/diarization
#   segmentation (MIT pyannote-3.0 exported to ONNX, ~6.6 MB) and an embedding
#   model, from the k2-fsa sherpa-onnx model releases:
#   https://github.com/k2-fsa/sherpa-onnx/releases/tag/speaker-segmentation-models
#   https://github.com/k2-fsa/sherpa-onnx/releases/tag/speaker-recongition-models
# Place them and point the env vars at the files.

# 3. Enable it:
export DIARIZATION_ENABLED=true
export DIARIZATION_SEGMENTATION_MODEL=./data/models/diarization/segmentation.onnx
export DIARIZATION_EMBEDDING_MODEL=./data/models/diarization/embedding.onnx
# Optional: DIARIZATION_NUM_SPEAKERS (0=auto), DIARIZATION_CLUSTER_THRESHOLD, DIARIZATION_NUM_THREADS
```

Diarization runs on the same 16 kHz mono WAV whisper uses, on CPU, so the GPU stays
dedicated to whisper. (In the Docker image, `pip install -r requirements-diarization.txt`
in the Dockerfile and mount/copy the models into the data volume.)

## Run locally

```sh
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt

export SECRET_KEY=$(python -c "import secrets; print(secrets.token_urlsafe(48))")
export DATA_DIR=./data ADMIN_USER=marco ADMIN_PASSWORD=change-me
uvicorn app.main:app --reload --port 8000
```

Open http://localhost:8000 and sign in as the bootstrap admin.

## Run with Docker

```sh
cp .env.example .env      # set SECRET_KEY + ADMIN_USER/ADMIN_PASSWORD
docker compose up --build
```

Data (SQLite DB + uploaded audio) persists in `./data`.

## Publishing internally (Entra ID SSO + TLS)

To publish knowts to your organisation over the office network/VPN (not the
public internet) with Microsoft **Entra ID single sign-on** and **HTTPS**, see
[`deploy/INTERNAL-DEPLOYMENT.md`](./deploy/INTERNAL-DEPLOYMENT.md). In short:

- Set the `OIDC_*` variables (from an Entra app registration) in `.env`; the
  login page then offers **"Sign in with Microsoft"** alongside local login.
  First-time SSO users are provisioned as members just-in-time;
  `OIDC_ADMIN_EMAILS` grants admin.
- Bring up the bundled Caddy TLS reverse proxy with the `tls` profile:
  ```sh
  docker compose --profile tls up -d --build
  ```
  Caddy terminates HTTPS on 443 (self-signed internal CA by default, or drop in
  your own corporate cert) and forwards to knowts. Set `COOKIE_SECURE=true`.

## Tests

```sh
pip install -r requirements-dev.txt
pytest
```

## Configuration

Environment variables are documented in [PLAN.md §9](./PLAN.md) and
[`.env.example`](./.env.example). `SECRET_KEY` should be set explicitly in any
real deployment; `STT_BASE_URL` defaults to `LLM_BASE_URL` (one llama-swap
endpoint serves both).
