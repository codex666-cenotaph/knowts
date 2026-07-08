# knowts
convert meeting mp3 files to meeting notes on your own setup.

See [PLAN.md](./PLAN.md) for the full design. Phase 0 (self-hosted whisper STT
on the `link` machine) lives in [`deploy/`](./deploy/). This repo now also
contains **Phase 1** (skeleton + auth), **Phase 2** (the pipeline core), and
**Phase 3** (the full web UI).

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
  profile page (`app/i18n.py`) switches the whole interface. Selecting Dutch
  also makes note generation (and the prompt manager's test-run preview)
  append a "respond only in Dutch" instruction to the system message,
  overriding the starter prompts' default "same language as the transcript"
  behavior — regardless of what language the meeting was actually held in.

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
