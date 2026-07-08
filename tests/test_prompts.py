"""Tests for prompt storage, seeding, and the notes generator (PLAN.md §4, §6)."""

from __future__ import annotations

import sqlite3

import pytest

from app import db as db_mod
from app import notes, prompts
from app.prompts import PromptVersion


@pytest.fixture
def conn(tmp_path):
    c = db_mod.connect(tmp_path / "t.db")
    db_mod.run_migrations(c)
    yield c
    c.close()


def test_seed_is_idempotent(conn: sqlite3.Connection):
    first = prompts.seed_starter_prompts(conn)
    assert first == 5
    again = prompts.seed_starter_prompts(conn)
    assert again == 0
    active = prompts.list_active(conn)
    names = {p.name for p in active}
    assert {"summary", "action-items", "decisions", "minutes", "qa-highlights"} <= names
    assert all(p.is_official and p.read_only for p in active)


def test_starter_prompts_are_language_aware(conn: sqlite3.Connection):
    prompts.seed_starter_prompts(conn)
    summary = next(p for p in prompts.list_active(conn) if p.name == "summary")
    version = prompts.latest_version(conn, summary.id)
    assert "same language as the transcript" in (version.system or "")


def test_seed_upgrades_stale_official_prompt(conn: sqlite3.Connection):
    # Simulate a prior deployment that seeded an older 'summary' text.
    stale = prompts.create_prompt(
        conn, name="summary", description="old", system="OLD SYSTEM",
        template="Old template {transcript}", reduce_template=None,
        owner_id=None, read_only=True,
    )
    assert prompts.latest_version(conn, stale.id).version == 1

    prompts.seed_starter_prompts(conn)

    # The stale starter gained a new version matching the current spec...
    latest = prompts.latest_version(conn, stale.id)
    assert latest.version == 2
    assert "same language as the transcript" in latest.system
    # ...v1 is preserved for attribution of any old notes.
    v1 = conn.execute(
        "SELECT system FROM prompt_versions WHERE prompt_id = ? AND version = 1",
        (stale.id,),
    ).fetchone()
    assert v1["system"] == "OLD SYSTEM"
    # A second seed is a no-op now that specs match.
    before = conn.execute("SELECT COUNT(*) AS c FROM prompt_versions").fetchone()["c"]
    prompts.seed_starter_prompts(conn)
    after = conn.execute("SELECT COUNT(*) AS c FROM prompt_versions").fetchone()["c"]
    assert before == after


def test_create_requires_transcript_placeholder(conn: sqlite3.Connection):
    with pytest.raises(ValueError):
        prompts.create_prompt(
            conn, name="bad", description=None, system=None, template="no placeholder"
        )


def test_latest_version_returned(conn: sqlite3.Connection):
    p = prompts.create_prompt(
        conn, name="x", description=None, system="sys", template="do {transcript}"
    )
    v = prompts.latest_version(conn, p.id)
    assert v is not None and v.template == "do {transcript}" and v.version == 1


class _FakeLLM:
    """Records prompts sent and returns canned content."""

    def __init__(self):
        self.calls: list[str] = []
        self.systems: list[str | None] = []

    def complete(self, *, model, system, user, temperature=None, max_tokens=None):
        self.calls.append(user)
        self.systems.append(system)
        return f"NOTES({len(self.calls)})"


def _version(template="Summarize: {transcript}", reduce=None):
    return PromptVersion(
        id=1, prompt_id=1, version=1, system="sys", template=template,
        reduce_template=reduce, model=None, temperature=None, max_tokens=None,
    )


def test_notes_single_shot_for_short_transcript():
    fake = _FakeLLM()
    md, model = notes.generate(
        fake, _version(), "a short transcript", None,
        default_model="m", context_tokens=32768,
    )
    assert md == "NOTES(1)"
    assert model == "m"
    assert len(fake.calls) == 1
    assert "a short transcript" in fake.calls[0]


def test_notes_map_reduce_for_long_transcript():
    fake = _FakeLLM()
    long_text = "\n\n".join(f"Para {i} " + "word " * 50 for i in range(400))
    md, _ = notes.generate(
        fake, _version(reduce="Merge: {transcript}"), long_text, None,
        default_model="m", context_tokens=500,
    )
    # More than one call means it chunked, and the final call is the reduce.
    assert len(fake.calls) > 1
    assert "Merge:" in fake.calls[-1]
    assert md.startswith("NOTES(")


def test_notes_uses_pinned_model():
    fake = _FakeLLM()
    v = PromptVersion(
        id=1, prompt_id=1, version=1, system=None, template="{transcript}",
        reduce_template=None, model="pinned-model", temperature=0.2, max_tokens=100,
    )
    _, model = notes.generate(
        fake, v, "hi", None, default_model="default", context_tokens=32768
    )
    assert model == "pinned-model"


def test_notes_no_language_override_by_default():
    fake = _FakeLLM()
    notes.generate(
        fake, _version(), "hi", None, default_model="m", context_tokens=32768
    )
    assert fake.systems == ["sys"]


def test_notes_dutch_override_adds_instruction_to_system():
    fake = _FakeLLM()
    notes.generate(
        fake, _version(), "hi", None, default_model="m", context_tokens=32768,
        language_override="nl",
    )
    assert len(fake.systems) == 1
    system = fake.systems[0]
    assert system.startswith("sys\n\n")
    assert "Dutch" in system


def test_notes_unknown_language_override_is_a_no_op():
    fake = _FakeLLM()
    notes.generate(
        fake, _version(), "hi", None, default_model="m", context_tokens=32768,
        language_override="fr",
    )
    assert fake.systems == ["sys"]


def test_system_with_language_override_handles_missing_system():
    assert notes.system_with_language_override(None, "nl") is not None
    assert "Dutch" in notes.system_with_language_override(None, "nl")
    assert notes.system_with_language_override(None, None) is None
    assert notes.system_with_language_override("sys", "en") == "sys"
