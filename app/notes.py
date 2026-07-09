"""Notes generation: single-shot and chunked map-reduce (PLAN.md §6, §10 step 9).

Given a stored transcript and a prompt version, produce Markdown notes. Short
transcripts go through in one LLM call; long ones are split (on segment
boundaries when available, else paragraph/sentence), the prompt's ``template``
runs per chunk, and the partials are merged with ``reduce_template`` (or a
generic merge prompt when the prompt doesn't define one).
"""

from __future__ import annotations

import logging

from . import llm as llm_mod
from .i18n import LLM_LANGUAGE_NAMES
from .llm import LLMClient
from .prompts import TRANSCRIPT_PLACEHOLDER, PromptVersion

log = logging.getLogger("knowts.notes")

# Reserve context for the prompt boilerplate and the model's response.
_HEADROOM_TOKENS = 2048

_GENERIC_REDUCE = (
    "The following are notes generated from consecutive parts of a single "
    "meeting. Merge them into one coherent, de-duplicated result that preserves "
    "chronological order. Keep the same Markdown structure.\n\n{transcript}"
)


def _render(template: str, transcript: str) -> str:
    return template.replace(TRANSCRIPT_PLACEHOLDER, transcript)


def system_with_language_override(system: str | None, language_override: str | None) -> str | None:
    """Append a "respond only in <language>" instruction when the meeting
    owner has a non-English profile language set. Overrides the starter
    prompts' default "same language as the transcript" behavior, since the
    user explicitly asked for their chosen language regardless of the source.

    Public so the prompt manager's test-run preview (app/routers/prompts.py)
    can apply the same override the real pipeline would use.
    """
    name = LLM_LANGUAGE_NAMES.get(language_override or "")
    if not name:
        return system
    instruction = (
        f"IMPORTANT: Write your entire response only in {name}, regardless of "
        "the language of the transcript or any other instruction above."
    )
    return f"{system}\n\n{instruction}" if system else instruction


def generate(
    client: LLMClient,
    version: PromptVersion,
    transcript_text: str,
    segments: list[dict] | None,
    *,
    default_model: str,
    context_tokens: int,
    language_override: str | None = None,
) -> tuple[str, str]:
    """Run a prompt against a transcript. Returns ``(markdown, model_used)``.

    ``language_override`` (an ISO code like ``"nl"``) forces the response
    language regardless of the transcript's language — set from the meeting
    owner's profile preference (PLAN.md follow-up: language setting).
    """
    model = version.model or default_model
    system = system_with_language_override(version.system, language_override)

    def call(user_prompt: str) -> str:
        return client.complete(
            model=model,
            system=system,
            user=user_prompt,
            temperature=version.temperature,
            max_tokens=version.max_tokens,
        )

    if llm_mod.fits_single_call(transcript_text, context_tokens, _HEADROOM_TOKENS):
        return call(_render(version.template, transcript_text)), model

    # --- map-reduce over chunks ---
    if segments:
        chunks = llm_mod.chunk_segments(segments, context_tokens, _HEADROOM_TOKENS)
    else:
        chunks = llm_mod.chunk_text(transcript_text, context_tokens, _HEADROOM_TOKENS)
    if not chunks:
        chunks = [transcript_text]

    log.info("map-reduce over %d chunk(s) with model %s", len(chunks), model)
    partials = [call(_render(version.template, chunk)) for chunk in chunks]

    if len(partials) == 1:
        return partials[0], model

    reduce_template = version.reduce_template or _GENERIC_REDUCE
    combined = "\n\n---\n\n".join(
        f"## Part {i + 1}\n\n{p}" for i, p in enumerate(partials)
    )
    # If the merge itself would overflow, reduce pairwise until it fits.
    while not llm_mod.fits_single_call(combined, context_tokens, _HEADROOM_TOKENS) and len(partials) > 1:
        partials = _reduce_round(call, reduce_template, partials, context_tokens)
        combined = "\n\n---\n\n".join(
            f"## Part {i + 1}\n\n{p}" for i, p in enumerate(partials)
        )
        if len(partials) == 1:
            return partials[0], model

    return call(_render(reduce_template, combined)), model


def _reduce_round(call, reduce_template, partials, context_tokens):
    """Merge partials pairwise to shrink the set for a final reduce."""
    merged: list[str] = []
    i = 0
    while i < len(partials):
        pair = partials[i : i + 2]
        if len(pair) == 1:
            merged.append(pair[0])
        else:
            combined = f"## Part 1\n\n{pair[0]}\n\n---\n\n## Part 2\n\n{pair[1]}"
            merged.append(call(_render(reduce_template, combined)))
        i += 2
    return merged
