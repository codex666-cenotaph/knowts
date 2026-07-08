# knowts — Implementation Plan

Convert meeting MP3 files into structured meeting notes using self-hosted services:

- **whisper STT** — deployed on link's AMD GPU **as part of this project** (the
  old faster-whisper/Wyoming container is being removed — see §5)
- **llama-swap** (already running on the link machine) — OpenAI-compatible API with on-demand model swapping
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
│    ├── ffmpeg: MP3 ─► 16 kHz mono WAV                                    │
│    ├── STT client (/v1/audio/transcriptions) ──┐                         │
│    ├── Notes generator (/v1/chat/completions) ─┴► llama-swap (link:8080) │
│    │      (llama-swap swaps whisper.cpp and LLM models on the AMD GPU)   │
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
3. Worker converts audio with ffmpeg to 16 kHz mono WAV (whisper.cpp's expected input).
4. Worker POSTs it to `/v1/audio/transcriptions` (`response_format=verbose_json`) and stores the transcript with timestamped segments.
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

## 5. Speech-to-text deployment + integration contracts (link machine)

The old `lscr.io/linuxserver/faster-whisper` (Wyoming) container is being
removed. **Deploying its replacement on link's AMD GPU is in scope for this
project.** Current relevant state of link: llama-swap
(`ghcr.io/mostlygeek/llama-swap:vulkan`) published on `0.0.0.0:8080`.

### Recommended: whisper.cpp managed by llama-swap (one endpoint for everything)

llama-swap supports routing `/v1/audio/transcriptions` by model name, and the
project publishes a **unified image** ("llama-server, ik-llama-server,
stable-diffusion.cpp, whisper.cpp and llama-swap built from source", available
for CUDA **and Vulkan** — exact tag to confirm at deploy time, e.g.
`ghcr.io/mostlygeek/llama-swap:unified-vulkan`). whisper.cpp's Vulkan backend
runs on the same AMD GPU llama-swap already uses.

Deployment = swap the llama-swap container to the unified image (existing config
and models carry over), add a whisper model entry to `llama-swap.yaml` that
launches `whisper-server`, and drop a ggml whisper model (e.g.
`large-v3-turbo`) into the models volume. Benefits:

- **One upstream for knowts**: `http://link:8080/v1` serves both
  `/v1/audio/transcriptions` (model = the whisper entry) and
  `/v1/chat/completions` (notes prompts) — one client, one base URL.
- **GPU arbitration for free**: llama-swap loads/unloads whisper and the LLM on
  demand, so they never fight over VRAM; `groups`/`ttl` config tunes this.
- **Timestamps**: `response_format=verbose_json` returns per-segment timestamps,
  so the transcript panel gets timestamped segments and `.srt` export.

### Alternatives (kept in the plan in case the unified image disappoints)

- **A — standalone whisper.cpp Vulkan container**: build/run `whisper-server`
  in its own container publishing e.g. `:8081`; knowts points `STT_BASE_URL` at
  it. Same API, but GPU sharing with llama-swap must be managed manually.
- **B — CPU-only faster-whisper (e.g. `speaches`)**: no GPU dependency at all;
  OpenAI-compatible; fine for occasional meetings but roughly real-time-or-slower
  on long recordings.

### Contracts knowts codes against

- **STT**: `POST {STT_BASE_URL}/audio/transcriptions` (multipart file +
  `model` + `response_format=verbose_json`) → text + segments. Implemented as
  `OpenAiCompatTranscriber` behind a small transcriber interface (so a future
  backend swap is a new adapter, not a refactor).
- **LLM**: `POST {LLM_BASE_URL}/chat/completions`; models listed from
  `/v1/models`. Generous timeouts everywhere — model swap + cold load takes a
  while, and transcribing a one-hour meeting is minutes, not seconds.
- If the STT service is down when a meeting is uploaded, the job queues and the
  UI says transcription is waiting on the service.

## 6. Long-transcript handling

- Estimate tokens (chars/4 heuristic); if the transcript fits within
  `LLM_CONTEXT_TOKENS` minus prompt/response headroom → single call.
- Otherwise split into overlapping chunks (on segment boundaries when the backend
  provides them, else on paragraph/sentence boundaries), run the prompt's
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
   - **transcript** panel: collapsible, timestamped segments, copy/download as
     `.txt`/`.srt`;
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
| `STT_BASE_URL` | `http://link:8080/v1` | transcription endpoint (defaults to `LLM_BASE_URL`) |
| `STT_MODEL` | `whisper-large-v3-turbo` | model name of the whisper entry in llama-swap |
| `LLM_BASE_URL` | `http://link:8080/v1` | llama-swap OpenAI-compatible base |
| `LLM_DEFAULT_MODEL` | `qwen2.5-32b` | model when a prompt doesn't pin one |
| `LLM_CONTEXT_TOKENS` | `32768` | chunking threshold |
| `SECRET_KEY` | random 32+ bytes | session cookie signing |
| `ADMIN_USER` / `ADMIN_PASSWORD` | `marco` / … | first-run admin bootstrap |
| `MAX_UPLOAD_MB` | `500` | upload guard |
| `DATA_DIR` | `/data` | storage root |

`docker-compose.yml` mounts `./data:/data` and publishes the UI port (default
`8000`). No GPU needed in this container — the heavy lifting stays on the
link machine behind llama-swap.

## 10. Execution steps

**Phase 0 — Deploy whisper STT on link (in scope, blocker for the pipeline)**
1. Remove the old faster-whisper (Wyoming) container.
2. Confirm the unified llama-swap image tag with whisper.cpp + Vulkan support; switch the llama-swap container to it (existing config and model entries carry over).
3. Download a ggml whisper model (start with `large-v3-turbo`; drop to `medium`/`small` if VRAM or speed disappoints) into the models volume; add a `whisper-server` model entry to `llama-swap.yaml`, in an appropriate `group`/`ttl` so it swaps cleanly against the LLMs.
4. Verify with `curl`: `/v1/models` lists the whisper entry and the LLMs; `/v1/audio/transcriptions` with a short test clip returns `verbose_json` with segments; an existing LLM still answers `/v1/chat/completions`. Fall back to alternative A (standalone whisper.cpp container) or B (CPU faster-whisper) if the unified image doesn't pan out.

**Phase 1 — Skeleton + auth**
3. Scaffold FastAPI app, Jinja2/HTMX layout, settings module (pydantic-settings), SQLite schema/migrations.
4. Auth: login/logout, session middleware, CSRF, argon2 hashing, admin bootstrap, login rate limiting, user-management pages, profile page.
5. Dockerfile (python-slim + ffmpeg) and docker-compose.yml with volumes; confirm the container builds and the login-protected empty UI serves.

**Phase 2 — Pipeline core**
6. Upload endpoint + file storage; meeting + job models with status transitions; ownership enforcement on every meeting route.
7. ffmpeg conversion helper (MP3 → 16 kHz mono WAV) with duration probe.
8. `OpenAiCompatTranscriber` behind the transcriber interface; integration-test against the whisper entry deployed in Phase 0.
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

- **Unified image tag** — confirmed as `ghcr.io/mostlygeek/llama-swap:unified-vulkan`
  (the unified build bundles whisper.cpp; documented upstream for CUDA and
  Vulkan). One thing still to confirm on `link` at deploy time: the
  `whisper-server` binary path inside that image (assumed on `PATH`). Phase 0
  runbook and artifacts are in [`deploy/`](./deploy/); alternatives A/B remain
  the fallback. llama-swap itself is confirmed at `http://link:8080/v1`.
- **Whisper model size** — starting with `large-v3-turbo` (ggml); Phase 0
  measures speed/VRAM on the AMD GPU and steps down if needed.
- **Swapping the llama-swap image** briefly interrupts anything else using it
  (open-webui, hermes) — worth a quiet moment on link.
- **Personal prompt visibility** — assumed personal prompts are also visible/usable
  workspace-wide (only *editing* is restricted to the owner). Flip to
  private-to-owner if that's the intent of "personal".
- **Where knowts runs** — assumed on link alongside the other containers; nothing
  depends on it as long as link:8080 and the whisper endpoint are reachable.
- **Meeting visibility** — private per user (admins see all); a share toggle is a stretch goal.
- **Diarization** — not assumed; notes prompts work without speaker labels but benefit from them if present.
- **One MP3 = one meeting** — no multi-file merge in v1.
