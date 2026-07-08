# knowts
convert meeting mp3 files to meeting notes on your own setup.

See [PLAN.md](./PLAN.md) for the full design. Phase 0 (self-hosted whisper STT
on the `link` machine) lives in [`deploy/`](./deploy/). This repo now also
contains **Phase 1**: the application skeleton and authentication.

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

Upload, transcription, and note generation arrive in Phases 2–3.

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
