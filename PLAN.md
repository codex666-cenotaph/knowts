# knowts — Implementation Plan

Convert meeting MP3 files into structured meeting notes using self-hosted services:

- **whisperflow** (already running on the link machine) — speech-to-text
- **llama-swap** (already running on the link machine) — OpenAI-compatible LLM API with on-demand model swapping
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
│    ├── Transcription client  ───────────────► whisperflow (link machine) │
│    ├── Notes generator (DB-backed prompts) ─► llama-swap  (link machine) │
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

**Prompt manager UI:**
- List with search; create / edit / duplicate / archive (no hard delete once used —
  existing notes must keep a valid reference).
- Edit form with placeholder validation (`{transcript}` must be present) and a model
  dropdown populated live from llama-swap's `/v1/models`.
- **Test run**: paste (or pick from an existing meeting) a transcript snippet and
  preview the prompt's output before saving.
- Version history: each edit stores a new prompt version; notes record which version
  produced them, so old notes stay reproducible/attributable.
- Starter set seeded on first run: `summary`, `action-items`, `decisions`,
  `minutes`, `qa-highlights` (editable like any other prompt).

## 5. Integration contracts (to verify against the live instances)

### llama-swap
Standard OpenAI-compatible endpoints: `/v1/chat/completions`, `/v1/models`. The
`model` field selects which model llama-swap loads/routes to. knowts will:
- read the base URL from `LLM_BASE_URL` (e.g. `http://link:8080/v1`)
- populate model dropdowns from `/v1/models`
- use generous HTTP timeouts (model swap + cold load can take a while)

### whisperflow
The open-source whisper-flow server is **streaming-first**: WebSocket at `/ws`
(default port 8181), accepting 16 kHz mono int16 PCM chunks and returning
incremental transcript segments; `/health` for liveness.

knowts will use a small **transcriber adapter interface** with two implementations:
1. `WhisperFlowWsTranscriber` — decode the MP3 with ffmpeg to raw PCM, stream it
   in chunks over the WebSocket, collect segments until EOF. (Default.)
2. `OpenAiCompatTranscriber` — POST the file to `/v1/audio/transcriptions`, for
   the case where the running container actually exposes a batch endpoint
   (several whisper server images do).

Selected via `TRANSCRIBER_KIND=whisperflow-ws | openai-compat`. **First execution
step is to probe the real container and confirm which contract applies.**

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
- `prompts(id, name, description, archived, created_by, created_at)`
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
| `WHISPER_URL` | `ws://link:8181/ws` or `http://link:8181` | whisperflow endpoint |
| `TRANSCRIBER_KIND` | `whisperflow-ws` | adapter selection |
| `LLM_BASE_URL` | `http://link:8080/v1` | llama-swap OpenAI-compatible base |
| `LLM_DEFAULT_MODEL` | `qwen2.5-32b` | model when a prompt doesn't pin one |
| `LLM_CONTEXT_TOKENS` | `32768` | chunking threshold |
| `SECRET_KEY` | random 32+ bytes | session cookie signing |
| `ADMIN_USER` / `ADMIN_PASSWORD` | `marco` / … | first-run admin bootstrap |
| `MAX_UPLOAD_MB` | `500` | upload guard |
| `DATA_DIR` | `/data` | storage root |

`docker-compose.yml` mounts `./data:/data` and publishes the UI port (default
`8000`). No GPU needed in this container — the heavy lifting stays on the link
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
16. README: setup, env vars, first-run admin bootstrap, docker-compose example wired to the link machine.
17. Optional stretch goals: per-meeting sharing between users, SSE live transcript preview during transcription, speaker diarization (if the whisper backend supports it), Obsidian/Notion-friendly export, tags on meetings.

## 11. Open questions / assumptions

- **whisperflow API shape** — assumed the dimastatz whisper-flow WS contract; Phase 0 verifies against the actual container and picks the right adapter.
- **Hostnames/ports** — placeholders (`link:8181`, `link:8080`) until confirmed.
- **Meeting visibility** — assumed private per user (admins see all); a share toggle is a stretch goal.
- **Prompt scope** — assumed prompts are shared by all users and editable by any member; flip to admin-only editing if preferred.
- **Diarization** — not assumed; notes prompts work without speaker labels but benefit from them if present.
- **One MP3 = one meeting** — no multi-file merge in v1.
