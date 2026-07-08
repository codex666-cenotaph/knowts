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
need curl; need jq

echo "1. /v1/models lists whisper + LLMs"
MODELS_JSON="$(curl -fsS "$BASE/v1/models")"
echo "$MODELS_JSON" | jq -e --arg m "$WHISPER_MODEL" '.data[] | select(.id==$m)' >/dev/null \
  && pass "whisper entry '$WHISPER_MODEL' present" \
  || fail "whisper entry '$WHISPER_MODEL' NOT in /v1/models"
echo "$MODELS_JSON" | jq -r '.data[].id' | sed 's/^/       - /'

echo "2. /v1/audio/transcriptions transcribes a sample clip"
TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT
# Use whisper.cpp's bundled jfk.wav (16 kHz mono real speech) — a correct
# transcript proves the model actually decodes audio, not just that the
# endpoint returns 200. Override with SAMPLE_WAV=/path/to/a.wav (e.g. offline).
SAMPLE_WAV="${SAMPLE_WAV:-}"
if [[ -z "$SAMPLE_WAV" ]]; then
  curl -fsSL -o "$TMP/jfk.wav" \
    https://raw.githubusercontent.com/ggml-org/whisper.cpp/master/samples/jfk.wav \
    || fail "couldn't fetch sample clip; set SAMPLE_WAV=/path/to/a.wav and re-run"
  SAMPLE_WAV="$TMP/jfk.wav"
fi
STT_JSON="$(curl -fsS "$BASE/v1/audio/transcriptions" \
  -F "model=$WHISPER_MODEL" \
  -F "file=@$SAMPLE_WAV" \
  -F "response_format=verbose_json")"
echo "$STT_JSON" | jq -e '(.text // "") | gsub("^\\s+|\\s+$";"") | length > 0' >/dev/null \
  && pass "transcribed: $(echo "$STT_JSON" | jq -r '.text' | tr -s '[:space:]' ' ' | sed 's/^ //' | head -c 90)" \
  || fail "no/empty text in transcription response: $STT_JSON"
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
