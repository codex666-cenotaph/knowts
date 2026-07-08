# Phase 0 — Deploy whisper STT on the `link` machine

This is the runbook for Phase 0 of the [plan](../PLAN.md): stand up an
OpenAI-compatible speech-to-text endpoint on `link` that knowts can call at
`http://link:8080/v1/audio/transcriptions`, sharing the AMD GPU with the LLMs
via llama-swap.

**These steps run on `link`, not inside the knowts container.** They swap the
llama-swap image for its unified build (which bundles `whisper.cpp`), add one
whisper model, and add one config entry. Nothing here changes how the LLMs are
served — existing model entries carry over untouched.

> Run these commands on `link`. Confirmed values for the current setup (from
> `docker inspect llama-swap`):
>
> | | value |
> |---|---|
> | `MODELS_DIR_HOST` | `/home/link/llm-host/models` (→ `/models`) |
> | `CONFIG_HOST` | `/home/link/llm-host/llama-swap.yaml` |
> | GPU device | `/dev/dri` (Vulkan, AMD) |
> | Published port | `8080:8080` |
>
> ⚠️ **Config path changes with the image.** The legacy `:vulkan` image reads
> config at `/app/config.yaml`; the **unified** image reads it at
> `/etc/llama-swap/config/config.yaml`. When you swap the image (step 4) you
> **must** change the config mount's destination to the new path, or llama-swap
> starts with no config and `/v1/models` comes back empty.

---

## What changes

- llama-swap image: `ghcr.io/mostlygeek/llama-swap:vulkan` → `:unified-vulkan`
  (the unified build adds `whisper-server`, `stable-diffusion.cpp`, etc.; the
  plain `:vulkan` image is llama-server only).
- One new file on the models volume: a ggml whisper model.
- One new entry in `llama-swap.yaml`: the `whisper-large-v3-turbo` model.

Everything else — the AMD Vulkan device passthrough, port `8080`, your existing
LLM entries — stays exactly as it is.

---

## Step 1 — Record your current setup

You need three values from the running container. Get them, then reuse them
verbatim in step 4 so the GPU passthrough and volumes stay identical.

```sh
# The exact command/compose that launched the current container:
docker inspect llama-swap --format '{{json .Config.Image}}{{"\n"}}{{json .Mounts}}{{"\n"}}{{json .HostConfig.Devices}}'
```

Note down:
- `MODELS_DIR_HOST` — the host path mounted to `/models` (from `.Mounts`).
- `CONFIG_HOST` — the host path mounted to the config file
  (`/etc/llama-swap/config/config.yaml` or wherever `-config` points).
- Whether it was started by `docker run`, a shell script, or `docker compose`
  (check for a `docker-compose.yml` next to the project, e.g.
  `~/llm-host/docker-compose.yml`).

If it's compose-managed, you'll edit the `image:` line there in step 4 rather
than re-running `docker run`.

---

## Step 2 — Download a whisper model onto the models volume

`large-v3-turbo` is the recommended starting point (fast, high quality, ~1.5 GB
VRAM). Drop to `medium` or `small` in step 3 if VRAM or speed disappoints.

```sh
mkdir -p /home/link/llm-host/models/whisper
curl -L --fail -o /home/link/llm-host/models/whisper/ggml-large-v3-turbo.bin \
  https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-large-v3-turbo.bin
```

(Alternative sizes, same URL pattern: `ggml-medium.bin`, `ggml-small.bin`,
`ggml-base.bin`.)

Inside the container this file is visible at
`/models/whisper/ggml-large-v3-turbo.bin`.

---

## Step 3 — Add the whisper entry to `llama-swap.yaml`

Merge the model entry from [`llama-swap.whisper.snippet.yaml`](./llama-swap.whisper.snippet.yaml)
into your existing config's `models:` map. Read that file's comments — they
explain the `whisper-server` flags, the `/v1/audio/transcriptions` path, and the
one thing to verify (the binary path inside the image).

Do **not** replace your config — add the one entry (and, optionally, adjust
`groups`/`ttl` as the snippet describes so whisper swaps cleanly against the
LLMs on the shared GPU).

---

## Step 4 — Swap the image to the unified build

The current container is `docker run`-managed (config bind-mounted to
`/app/config.yaml`, models to `/models`, `/dev/dri` passed through). Pull the
unified image, remove the old container, and re-run — changing **two** things:
the image tag, and the config mount's destination path.

```sh
docker pull ghcr.io/mostlygeek/llama-swap:unified-vulkan
docker rm -f llama-swap

docker run -d --name llama-swap \
  --restart unless-stopped \
  -p 8080:8080 \
  --device /dev/dri \
  -v /home/link/llm-host/llama-swap.yaml:/etc/llama-swap/config/config.yaml \
  -v /home/link/llm-host/models:/models \
  ghcr.io/mostlygeek/llama-swap:unified-vulkan
```

Changed vs. the old container:
- image tag `:vulkan` → `:unified-vulkan`
- config destination `/app/config.yaml` → `/etc/llama-swap/config/config.yaml`

Everything else (port, `/dev/dri`, models mount) is identical. If your original
container used a different `--restart` policy or extra flags/env, match them —
`docker inspect llama-swap` on the *old* container would have shown them; the
essentials above are what's confirmed.

> If this is actually compose-managed (check for
> `/home/link/llm-host/docker-compose.yml`), edit the service's `image:` tag and
> the config volume's destination there instead, then
> `docker compose up -d llama-swap`.

> Heads-up: this briefly restarts the endpoint that open-webui and hermes talk
> to. Do it during a quiet moment.

---

## Step 5 — Verify

Run the verification script (edit the two variables at the top first):

```sh
./verify-stt.sh
```

It checks, against `http://localhost:8080`:
1. `/v1/models` lists both the whisper entry and your existing LLMs.
2. `/v1/audio/transcriptions` transcribes a generated test clip and returns text.
3. An existing LLM still answers `/v1/chat/completions` (proves the swap works).

Green on all three = Phase 0 done; knowts can point `STT_BASE_URL` and
`LLM_BASE_URL` at `http://link:8080/v1`.

---

## If the unified image doesn't work out

Fallbacks from the plan (§5), in order of preference:

- **A — standalone whisper.cpp Vulkan container** on its own port (e.g. `:8081`);
  point knowts's `STT_BASE_URL` there instead of at llama-swap.
- **B — CPU-only faster-whisper** (e.g. the `speaches` image); no GPU
  dependency, fine for occasional meetings, ~real-time or slower on long audio.

Both expose the same `POST /v1/audio/transcriptions` contract, so knowts's code
is unchanged — only `STT_BASE_URL` / `STT_MODEL` differ.

---

## Things this runbook can't verify from outside `link`

- The exact **image tag** — `unified-vulkan` is documented upstream for the
  Vulkan build; if `docker pull` 404s, run `docker pull ghcr.io/mostlygeek/llama-swap:unified-cuda`
  only if link is NVIDIA (it isn't) or check the registry's tag list for the
  current unified-vulkan tag name.
- The **`whisper-server` binary path** inside the unified image — the snippet
  assumes it's on `PATH`. Confirm with
  `docker run --rm ghcr.io/mostlygeek/llama-swap:unified-vulkan which whisper-server`
  and adjust the `cmd` if it's an absolute path.
- Whether `large-v3-turbo` returns per-segment **timestamps** with
  `response_format=verbose_json` — knowts degrades gracefully to plain text if
  not (see plan §5, §8).
