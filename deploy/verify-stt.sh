#!/usr/bin/env bash
# Phase 0 verification — run on `link` after swapping to the unified image and
# adding the whisper entry. Checks the three things the plan (§5 step 4) requires.
set -euo pipefail

# --- edit these two ---------------------------------------------------------
BASE="${BASE:-http://localhost:8080}"       # llama-swap base URL
LLM_MODEL="${LLM_MODEL:-}"                   # name of any existing LLM entry
WHISPER_MODEL="${WHISPER_MODEL:-whisper-large-v3-turbo}"
# ---------------------------------------------------------------------------

if [[ -z "$LLM_MODEL" ]]; then
  echo "Set LLM_MODEL to one of your existing model names (see /v1/models)." >&2
  echo "  e.g.  LLM_MODEL=qwen2.5-32b ./verify-stt.sh" >&2
fi

pass() { printf '  \033[32mPASS\033[0m %s\n' "$1"; }
fail() { printf '  \033[31mFAIL\033[0m %s\n' "$1"; exit 1; }
need() { command -v "$1" >/dev/null || { echo "missing dependency: $1" >&2; exit 1; }; }
need curl; need ffmpeg; need jq

echo "1. /v1/models lists whisper + LLMs"
MODELS_JSON="$(curl -fsS "$BASE/v1/models")"
echo "$MODELS_JSON" | jq -e --arg m "$WHISPER_MODEL" '.data[] | select(.id==$m)' >/dev/null \
  && pass "whisper entry '$WHISPER_MODEL' present" \
  || fail "whisper entry '$WHISPER_MODEL' NOT in /v1/models"
echo "$MODELS_JSON" | jq -r '.data[].id' | sed 's/^/       - /'

echo "2. /v1/audio/transcriptions transcribes a test clip"
TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT
# Generate ~2s of a spoken-frequency tone as a valid 16 kHz mono wav. Whisper
# won't produce meaningful words from a tone, but a 200 + JSON body with a
# "text" field proves the endpoint, model load, and audio decode all work.
ffmpeg -nostdin -loglevel error -f lavfi -i "sine=frequency=220:duration=2" \
  -ar 16000 -ac 1 "$TMP/test.wav"
STT_JSON="$(curl -fsS "$BASE/v1/audio/transcriptions" \
  -F "model=$WHISPER_MODEL" \
  -F "file=@$TMP/test.wav" \
  -F "response_format=verbose_json")"
echo "$STT_JSON" | jq -e 'has("text")' >/dev/null \
  && pass "transcription endpoint returned a text field" \
  || fail "no text field in transcription response: $STT_JSON"
if echo "$STT_JSON" | jq -e '.segments and (.segments|length>0)' >/dev/null 2>&1; then
  pass "verbose_json includes segments (timestamps available)"
else
  echo "       note: no segments returned — knowts uses plain-text transcript (fine)"
fi

echo "3. an LLM still answers /v1/chat/completions"
if [[ -n "$LLM_MODEL" ]]; then
  CHAT_JSON="$(curl -fsS "$BASE/v1/chat/completions" \
    -H 'Content-Type: application/json' \
    -d "{\"model\":\"$LLM_MODEL\",\"messages\":[{\"role\":\"user\",\"content\":\"reply with OK\"}],\"max_tokens\":8}")"
  echo "$CHAT_JSON" | jq -e '.choices[0].message.content' >/dev/null \
    && pass "LLM '$LLM_MODEL' responded (swap works both ways)" \
    || fail "LLM did not respond: $CHAT_JSON"
else
  echo "  SKIP  set LLM_MODEL to run this check"
fi

echo
echo "Phase 0 verification complete."
