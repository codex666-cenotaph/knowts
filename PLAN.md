# knowts — Implementation Plan

Convert meeting MP3 files into structured meeting notes using self-hosted services:

- **whisperflow** (to run on the lync machine; not in the current `docker ps` — see §5) — speech-to-text
- **llama-swap** (already running on the lync machine) — OpenAI-compatible LLM API with on-demand model swapping
- **knowts** (this project) — a full web application in its own Docker container with
  user login, prompt management, and a per-meeting archive that groups the original
  MP3, the transcription, and the LLM-generated notes together

---

## 1. Architecture

```
┌──────────────────────────── knowts container ────────────────────────────┐
│                                                                          │
│  Browser ──► Web UI (login, upload, meetings archive, prompt manager)    │
│                 │                                                        │
│                 ▼                                                        │
│  FastAPI backend                                                         │
│    ├── Auth (session cookies, password hashing, user management)         │
│    ├── Job queue (async, in-process)                                     │
│    ├── ffmpeg: MP3 ─► 16 kHz mono PCM/WAV                                │
│    ├── Transcription client  ───────────────► whisperflow (lync machine) │
│    ├── Notes generator (DB-backed prompts) ─► llama-swap  (lync machine) │
│    └── SQLite + file storage (volume-mounted /data)                      │
│                                                                          │
└──────────────────────────────────────────────────────────────────────────┘
```

**The meeting is the central object.** Every upload creates a *meeting* record that
permanently groups:
1. the original MP3 (playable in the browser, downloadable),
2. the transcription (full text + timestamped segments),
3. every set of LLM-generated notes produced for it (one per prompt run, each
   tagged with the prompt and model used).

Meetings are listed in an archive and each opens on its own detail page. Additional
prompts can be run against a stored transcript at any time without re-transcribing.

**Pipeline per meeting:**

1. Logged-in user uploads an MP3, gives it a title/date, selects one or more prompts.
2. Backend stores the file, creates the meeting + job, returns immediately.
3. Worker converts audio with ffmpeg to the format whisperflow expects (16 kHz, mono, 16-bit PCM).
4. Worker sends audio to whisperflow and stores the transcript (with timestamps if available).
5. Worker runs each selected prompt against the transcript via llama-swap's
   `/v1/chat/completions`. Long transcripts are chunked (map-reduce: per-chunk pass,
   then merge).
6. Notes are stored as Markdown on the meeting; the UI shows live job status and the
   finished meeting page.

## 2. Tech stack

| Component | Choice | Rationale |
|---|---|---|
| Backend | Python 3.12 + FastAPI | async-friendly, easy HTTP/WS clients, small footprint |
| Frontend | Server-rendered Jinja2 + HTMX (no build step) | full-featured UI without a node toolchain in the image |
| Auth | Session cookie (signed, HTTP-only) + argon2 password hashes | simple, robust, no external identity provider |
| Job handling | asyncio background tasks + SQLite job table | one container, no Redis/Celery needed at this scale |
| Storage | SQLite + files on a mounted volume (`/data`) | survives container restarts, trivially backed up |
| Audio | ffmpeg (in the image) | MP3 → PCM/WAV conversion, duration probing |
| LLM client | `openai` Python SDK pointed at llama-swap base URL | llama-swap is OpenAI-compatible; model name selects the model |
| Container | Single Dockerfile + docker-compose.yml | "runs in its own docker" requirement |

## 3. Authentication & users

- **Login page**; all other routes require a valid session. Signed, HTTP-only session
  cookies with idle + absolute expiry. CSRF protection on all mutating forms.
- **Password storage**: argon2id hashes. Login rate-limited (per-IP + per-account backoff).
- **Bootstrap**: on first start, an admin account is created from
  `ADMIN_USER` / `ADMIN_PASSWORD` env vars (or a one-time setup page if unset).
- **Roles**: `admin` and `member`.
  - *admin*: manage users (create, deactivate, reset password), plus everything members can do.
  - *member*: upload meetings, manage prompts, view/manage their own meetings.
- **Ownership**: meetings belong to the user who uploaded them; users see only their
  own meetings. Admins can see all. (A per-meeting "share with everyone" toggle is a
  cheap follow-up if wanted.) Prompts are shared workspace-wide.
- **Profile page**: change own password.

## 4. Prompt management (database-backed, full UI)

Prompts live in the database and are managed entirely through the web UI — no
container rebuilds, no volume edits.

**Sharing & ownership model:**
- All prompts are **visible and usable workspace-wide** — any user can select any
  prompt when generating notes.
- **Official prompts** (admin-created, read-only): only admins can create or edit
  them. They form the curated library everyone relies on. Members cannot modify
  them but can **clone** any official prompt into a personal copy.
- **Personal prompts**: created from scratch or by cloning; owned by a user and
  editable only by that owner (and admins). A clone records which official prompt
  (and version) it came from.
- The prompt picker and manager badge each prompt as `official` (lock icon) or
  `personal` (owner shown), with clone available on everything.

**Prompt fields:**

| Field | Purpose |
|---|---|
| `name`, `description` | shown in the picker at upload time |
| `system` | system message for the LLM |
| `template` | user message; `{transcript}` placeholder is substituted |
| `reduce_template` | optional; merges partial results when the transcript is chunked |
| `model` | optional; pins a llama-swap model, else `LLM_DEFAULT_MODEL` |
| `temperature`, `max_tokens` | optional generation params |
| `archived` | archived prompts are hidden from the picker but past notes keep their reference |

Plus ownership fields: `owner_id` (`NULL` = official/admin-owned) and `read_only`
(true for official prompts), and `cloned_from_version_id` on clones.

**Prompt manager UI:**
- List with search, badged official vs. personal; create / edit / **clone** /
  archive (no hard delete once used — existing notes must keep a valid reference).
- Edit is enforced server-side: official prompts are editable by admins only;
  personal prompts by their owner or an admin. Clone is available to everyone on
  every prompt.
- Edit form with placeholder validation (`{transcript}` must be present) and a model
  dropdown populated live from llama-swap's `/v1/models`.
- **Test run**: paste (or pick from an existing meeting) a transcript snippet and
  preview the prompt's output before saving.
- Version history: each edit stores a new prompt version; notes record which version
  produced them, so old notes stay reproducible/attributable.
- Starter set seeded on first run: `summary`, `action-items`, `decisions`,
  `minutes`, `qa-highlights` (editable like any other prompt).

## 5. Integration contracts (lync machine)

Current state of lync per `docker ps` (2026-07-08): llama-swap
(`ghcr.io/mostlygeek/llama-swap:vulkan`) is up and published on `0.0.0.0:8080`;
also running: open-webui (:3001), nginx workspace-web (:8090), hermes-agent and
homey-mcp (loopback-only). **No whisper/STT container is currently running.**

### llama-swap — CONFIRMED
`http://lync:8080/v1`, OpenAI-compatible endpoints: `/v1/chat/completions`,
`/v1/models`. Published on `0.0.0.0`, so it's reachable from the knowts container
whether knowts runs on lync or another host. knowts will:
- read the base URL from `LLM_BASE_URL` (default `http://lync:8080/v1`)
- populate model dropdowns from `/v1/models`
- use generous HTTP timeouts (model swap + cold load can take a while)

### whisperflow — NOT RUNNING YET (prerequisite)
No whisperflow container appears in the `docker ps` output, so it must be
(re)started before Phase 0 can verify its contract — or it lives on a different
machine, in which case knowts just needs its URL. Depending on the image used,
the contract is one of:
1. **WS streaming** (dimastatz whisper-flow): WebSocket at `/ws` (default port
   8181), 16 kHz mono int16 PCM chunks in, incremental segments out; `/health`
   for liveness.
2. **OpenAI-compatible batch**: POST the file to `/v1/audio/transcriptions`
   (several whisper server images expose this).

knowts implements both behind a **transcriber adapter interface**
(`WhisperFlowWsTranscriber`, `OpenAiCompatTranscriber`), selected via
`TRANSCRIBER_KIND=whisperflow-ws | openai-compat`, so whichever container ends up
running is supported. If the whisperflow service is down when a meeting is
uploaded, the job queues and the UI says transcription is waiting on the service.

## 6. Long-transcript handling

- Estimate tokens (chars/4 heuristic); if the transcript fits within
  `LLM_CONTEXT_TOKENS` minus prompt/response headroom → single call.
- Otherwise split on segment boundaries into overlapping chunks, run the prompt's
  `template` per chunk, then merge with `reduce_template` (or a generic merge
  prompt if the prompt doesn't define one).

## 7. Data model & storage layout

SQLite tables:

- `users(id, username, password_hash, role, active, created_at)`
- `sessions(id, user_id, expires_at)` *(if server-side sessions are chosen over pure signed cookies)*
- `meetings(id, user_id, title, meeting_date, filename, duration_s, status, created_at)`
- `jobs(id, meeting_id, kind[transcribe|notes], status[queued|running|done|error], step, error, started_at, finished_at)`
- `transcripts(meeting_id, text, segments_json, language, created_at)`
- `prompts(id, name, description, owner_id NULL=official, read_only, cloned_from_version_id, archived, created_by, created_at)`
- `prompt_versions(id, prompt_id, version, system, template, reduce_template, model, temperature, max_tokens, created_at)`
- `notes(id, meeting_id, prompt_version_id, model_used, markdown, created_at)`

Files under the `/data` volume:
```
/data/
  knowts.db
  audio/<meeting_id>.mp3        # original upload, kept permanently
```

Deleting a meeting removes its audio, transcript, and notes together (with a
confirmation step); nothing else in v1 deletes data.

## 8. Web UI (pages)

1. **Login** — username/password; everything else is behind it.
2. **Home / Upload** — drag-and-drop MP3, title + meeting date, prompt multi-select,
   submit → meeting page in "processing" state.
3. **Meetings archive** — table of the user's meetings (title, date, duration,
   status, note count), search by title, filter by date; click through to detail.
4. **Meeting detail** — the grouped view, one page per meeting:
   - header: title, date, duration, status;
   - **audio player** for the original MP3 (HTTP range requests for seeking) + download;
   - **transcript** panel: collapsible, timestamped segments, copy/download as `.txt`/`.srt`;
   - **notes** tabs: one tab per generated note set, labelled with prompt name +
     model; rendered Markdown with copy and `.md` download;
   - **"Generate more notes"**: pick further prompts and run them against the stored
     transcript (no re-transcription); re-run an existing prompt to regenerate;
   - live progress via HTMX polling while jobs run; errors shown inline with retry;
   - delete meeting (confirmation required).
5. **Prompt manager** — list/search, create/edit/duplicate/archive, test-run,
   version history (section 4).
6. **Admin → Users** — admin only: create user, deactivate, reset password.
7. **Profile** — change own password.

## 9. Configuration (environment variables)

| Variable | Example | Purpose |
|---|---|---|
| `WHISPER_URL` | `ws://lync:8181/ws` or `http://lync:8181` | whisperflow endpoint |
| `TRANSCRIBER_KIND` | `whisperflow-ws` | adapter selection |
| `LLM_BASE_URL` | `http://lync:8080/v1` | llama-swap OpenAI-compatible base |
| `LLM_DEFAULT_MODEL` | `qwen2.5-32b` | model when a prompt doesn't pin one |
| `LLM_CONTEXT_TOKENS` | `32768` | chunking threshold |
| `SECRET_KEY` | random 32+ bytes | session cookie signing |
| `ADMIN_USER` / `ADMIN_PASSWORD` | `marco` / … | first-run admin bootstrap |
| `MAX_UPLOAD_MB` | `500` | upload guard |
| `DATA_DIR` | `/data` | storage root |

`docker-compose.yml` mounts `./data:/data` and publishes the UI port (default
`8000`). No GPU needed in this container — the heavy lifting stays on the lync
machine.

## 10. Execution steps

**Phase 0 — Verify contracts (blocker for everything else)**
1. Probe the running whisperflow container (`/health`, try `/v1/audio/transcriptions`, try WS `/ws`) and record the actual API shape, port, and audio format expectations.
2. Hit llama-swap `/v1/models`; record base URL, available model names, and rough context sizes.

**Phase 1 — Skeleton + auth**
3. Scaffold FastAPI app, Jinja2/HTMX layout, settings module (pydantic-settings), SQLite schema/migrations.
4. Auth: login/logout, session middleware, CSRF, argon2 hashing, admin bootstrap, login rate limiting, user-management pages, profile page.
5. Dockerfile (python-slim + ffmpeg) and docker-compose.yml with volumes; confirm the container builds and the login-protected empty UI serves.

**Phase 2 — Pipeline core**
6. Upload endpoint + file storage; meeting + job models with status transitions; ownership enforcement on every meeting route.
7. ffmpeg conversion helper (MP3 → 16 kHz mono PCM/WAV) with duration probe.
8. Transcriber adapters (WS streaming first, OpenAI-compat second) behind the common interface; integration-test against the real whisperflow.
9. Prompt tables + seeding of the starter set; notes generator: single-shot path, then chunked map-reduce path; integration-test against llama-swap.

**Phase 3 — Full UI**
10. Upload page with prompt multi-select; meeting page in processing state with HTMX polling.
11. Meeting detail page: audio player with range-request streaming, transcript panel with `.txt`/`.srt` export, notes tabs with Markdown rendering and `.md` download, "generate more notes", delete with confirmation.
12. Meetings archive with search/filter.
13. Prompt manager: CRUD, duplicate, archive, placeholder validation, model dropdown from `/v1/models`, test-run preview, version history.

**Phase 4 — Hardening & docs**
14. Error handling everywhere it can fail: unreachable services, transcription failures, LLM timeouts (llama-swap cold-start), oversized uploads — surfaced in the UI with retry.
15. Concurrency guard (limit to N concurrent transcriptions; queue the rest).
16. README: setup, env vars, first-run admin bootstrap, docker-compose example wired to the lync machine.
17. Optional stretch goals: per-meeting sharing between users, SSE live transcript preview during transcription, speaker diarization (if the whisper backend supports it), Obsidian/Notion-friendly export, tags on meetings.

## 11. Open questions / assumptions

- **whisperflow is not running yet** (confirmed absent from lync's `docker ps`,
  2026-07-08) — it must be started (image + port TBD), or its URL provided if it
  lives elsewhere. Phase 0 verifies its API shape and picks the right adapter.
  llama-swap is confirmed at `http://lync:8080/v1`.
- **Personal prompt visibility** — assumed personal prompts are also visible/usable
  workspace-wide (only *editing* is restricted to the owner). Flip to
  private-to-owner if that's the intent of "personal".
- **Where knowts runs** — assumed on lync alongside the other containers; nothing
  depends on it as long as lync:8080 (and the whisper port) are reachable.
- **Meeting visibility** — private per user (admins see all); a share toggle is a stretch goal.
- **Diarization** — not assumed; notes prompts work without speaker labels but benefit from them if present.
- **One MP3 = one meeting** — no multi-file merge in v1.
