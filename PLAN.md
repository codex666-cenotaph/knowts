# knowts — Implementation Plan

Convert meeting MP3 files into structured meeting notes using self-hosted services:

- **whisperflow** (already running on the link machine) — speech-to-text
- **llama-swap** (already running on the link machine) — OpenAI-compatible LLM API with on-demand model swapping
- **knowts** (this project) — a web app in its own Docker container that orchestrates the pipeline and manages a library of predefined note-taking prompts

---

## 1. Architecture

```
┌──────────────────────────── knowts container ────────────────────────────┐
│                                                                          │
│  Browser ──► Web UI (upload MP3, pick prompt, watch progress, view notes)│
│                 │                                                        │
│                 ▼                                                        │
│  FastAPI backend                                                         │
│    ├── Job queue (async, in-process)                                     │
│    ├── ffmpeg: MP3 ─► 16 kHz mono PCM/WAV                                │
│    ├── Transcription client  ───────────────► whisperflow (link machine) │
│    ├── Notes generator (prompt library) ───► llama-swap  (link machine)  │
│    └── SQLite + file storage (volume-mounted)                            │
│                                                                          │
└──────────────────────────────────────────────────────────────────────────┘
```

**Pipeline per meeting:**

1. User uploads an MP3 and selects one or more prompts from the library (e.g. "Action items", "Executive summary", "Decision log").
2. Backend stores the file, creates a job, returns immediately.
3. Worker converts audio with ffmpeg to the format whisperflow expects (16 kHz, mono, 16-bit PCM).
4. Worker sends audio to whisperflow and collects the full transcript (with timestamps if available).
5. Worker runs each selected prompt against the transcript via llama-swap's `/v1/chat/completions`. Long transcripts are chunked (map-reduce: summarize chunks, then merge).
6. Results are stored as Markdown; UI shows live job status and renders the finished notes with download/copy options.

## 2. Tech stack

| Component | Choice | Rationale |
|---|---|---|
| Backend | Python 3.12 + FastAPI | async-friendly, easy HTTP/WS clients, small footprint |
| Frontend | Server-rendered Jinja2 + HTMX (no build step) | simple, fast to build, no node toolchain in the image |
| Job handling | asyncio background tasks + SQLite job table | one container, no Redis/Celery needed at this scale |
| Storage | SQLite + files on a mounted volume (`/data`) | survives container restarts, trivially backed up |
| Audio | ffmpeg (in the image) | MP3 → PCM/WAV conversion, duration probing |
| LLM client | `openai` Python SDK pointed at llama-swap base URL | llama-swap is OpenAI-compatible; model name selects the model |
| Container | Single Dockerfile + docker-compose.yml | "runs in its own docker" requirement |

## 3. Integration contracts (to verify against the live instances)

### llama-swap
Standard OpenAI-compatible endpoints: `/v1/chat/completions`, `/v1/models`. The
`model` field in the request selects which model llama-swap loads/routes to.
knowts will:
- read the base URL from `LLM_BASE_URL` (e.g. `http://link:8080/v1`)
- list available models from `/v1/models` so prompts can pin a model or fall back to `LLM_DEFAULT_MODEL`
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

## 4. Prompt library

Prompts live as YAML files in `prompts/`, volume-mounted so they can be added or
edited without rebuilding the image. Hot-reloaded on each request.

```yaml
# prompts/action-items.yaml
id: action-items
name: Action Items
description: Extract owners, tasks, and deadlines.
model: qwen2.5-32b        # optional; falls back to LLM_DEFAULT_MODEL
temperature: 0.2          # optional
system: |
  You are a meticulous meeting assistant...
template: |
  Extract all action items from this meeting transcript.
  For each item list: owner, task, deadline (if mentioned).
  Transcript:
  {transcript}
reduce_template: |        # optional; used to merge chunked partial results
  Merge these partial action-item lists into one deduplicated list:
  {partials}
```

Ship with a starter set: `summary`, `action-items`, `decisions`, `minutes`
(formal), `qa-highlights`.

## 5. Long-transcript handling

- Estimate tokens (chars/4 heuristic); if the transcript fits within
  `LLM_CONTEXT_TOKENS` minus prompt/response headroom → single call.
- Otherwise split on segment boundaries into overlapping chunks, run the
  prompt's `template` per chunk, then merge with `reduce_template` (or a
  generic merge prompt if the YAML doesn't define one).

## 6. Data model & storage layout

SQLite tables:
- `meetings(id, title, filename, duration_s, created_at)`
- `jobs(id, meeting_id, status[queued|transcribing|generating|done|error], step, error, started_at, finished_at)`
- `transcripts(meeting_id, text, segments_json)`
- `notes(id, meeting_id, prompt_id, model, markdown, created_at)`

Files under the `/data` volume:
```
/data/
  knowts.db
  audio/<meeting_id>.mp3
  transcripts/<meeting_id>.json
```

## 7. Web UI (pages)

1. **Home / Upload** — drag-and-drop MP3, title field, prompt multi-select
   (from library), submit → job page.
2. **Job status** — progress steps (uploaded → transcribing → generating →
   done) via HTMX polling; errors surfaced with retry button.
3. **Meeting detail** — rendered Markdown notes per prompt (tabs), collapsible
   full transcript, download `.md`, copy to clipboard; "run another prompt"
   button to generate additional notes from the stored transcript without
   re-transcribing.
4. **History** — list of past meetings with search by title/date.
5. **Prompt library** — read-only view of loaded prompts (editing happens on
   disk via the volume).

## 8. Configuration (environment variables)

| Variable | Example | Purpose |
|---|---|---|
| `WHISPER_URL` | `ws://link:8181/ws` or `http://link:8181` | whisperflow endpoint |
| `TRANSCRIBER_KIND` | `whisperflow-ws` | adapter selection |
| `LLM_BASE_URL` | `http://link:8080/v1` | llama-swap OpenAI-compatible base |
| `LLM_DEFAULT_MODEL` | `qwen2.5-32b` | model when a prompt doesn't pin one |
| `LLM_CONTEXT_TOKENS` | `32768` | chunking threshold |
| `MAX_UPLOAD_MB` | `500` | upload guard |
| `DATA_DIR` | `/data` | storage root |

`docker-compose.yml` mounts `./data:/data` and `./prompts:/app/prompts`, and
publishes the UI port (default `8000`). No GPU needed in this container — the
heavy lifting stays on the link machine.

## 9. Execution steps

**Phase 0 — Verify contracts (blocker for everything else)**
1. Probe the running whisperflow container (`/health`, try `/v1/audio/transcriptions`, try WS `/ws`) and record the actual API shape, port, and audio format expectations.
2. Hit llama-swap `/v1/models`; record base URL, available model names, and rough context sizes.

**Phase 1 — Skeleton**
3. Scaffold FastAPI app, Jinja2/HTMX layout, health endpoint, settings module (pydantic-settings reading the env vars above).
4. Dockerfile (python-slim + ffmpeg) and docker-compose.yml with volumes; confirm the container builds and serves the empty UI.

**Phase 2 — Pipeline core**
5. Upload endpoint + file storage + SQLite schema/migrations; job model with status transitions.
6. ffmpeg conversion helper (MP3 → 16 kHz mono PCM/WAV) with duration probe.
7. Transcriber adapters (WS streaming first, OpenAI-compat second) behind the common interface; integration-test against the real whisperflow.
8. Prompt library loader + validation; ship the starter prompt set.
9. Notes generator: single-shot path, then chunked map-reduce path; integration-test against llama-swap.

**Phase 3 — UI & polish**
10. Upload page with prompt multi-select; job status page with HTMX polling.
11. Meeting detail page (Markdown rendering, transcript, downloads, "run another prompt").
12. History page + search.
13. Error handling: unreachable services, transcription failures, LLM timeouts (llama-swap cold-start), oversized uploads — all surfaced in the UI with retry.

**Phase 4 — Hardening & docs**
14. Concurrency guard (limit to N concurrent transcriptions; queue the rest).
15. README: setup, env vars, how to add prompts, docker-compose example wired to the link machine.
16. Optional stretch goals: SSE live transcript preview during transcription, speaker diarization (if the whisper backend supports it), export to Obsidian/Notion-friendly Markdown, basic auth for non-LAN exposure.

## 10. Open questions / assumptions

- **whisperflow API shape** — assumed the dimastatz whisper-flow WS contract; Phase 0 verifies against the actual container and picks the right adapter.
- **Hostnames/ports** — placeholders (`link:8181`, `link:8080`) until confirmed.
- **Auth** — assumed LAN-only, no login. Basic auth is a stretch goal.
- **Diarization** — not assumed; notes prompts are written to work without speaker labels but benefit from them if present.
- **One MP3 = one meeting** — no multi-file merge in v1.
