"""Prompt storage, versioning, and starter-set seeding (PLAN.md §4, §7).

Phase 2 needs enough of the prompt model to run notes generation: the tables
(created in migration 2), the seeded official starter set, and read access for
the picker plus the version a note was generated from. The full management UI
(create/edit/clone/archive, test-run, version history) lands in Phase 3.
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass

from .users import User

log = logging.getLogger("knowts.prompts")

TRANSCRIPT_PLACEHOLDER = "{transcript}"


@dataclass(frozen=True)
class PromptVersion:
    id: int
    prompt_id: int
    version: int
    system: str | None
    template: str
    reduce_template: str | None
    model: str | None
    temperature: float | None
    max_tokens: int | None


@dataclass(frozen=True)
class Prompt:
    id: int
    name: str
    description: str | None
    owner_id: int | None
    read_only: bool
    archived: bool

    @property
    def is_official(self) -> bool:
        return self.owner_id is None


@dataclass(frozen=True)
class PromptSummary:
    """A prompt plus the joined data the manager list needs (owner name,
    version count, latest version number)."""

    prompt: Prompt
    owner_username: str | None
    version_count: int
    latest_version: int


def _row_to_prompt(row: sqlite3.Row) -> Prompt:
    return Prompt(
        id=row["id"],
        name=row["name"],
        description=row["description"],
        owner_id=row["owner_id"],
        read_only=bool(row["read_only"]),
        archived=bool(row["archived"]),
    )


def _row_to_version(row: sqlite3.Row) -> PromptVersion:
    return PromptVersion(
        id=row["id"],
        prompt_id=row["prompt_id"],
        version=row["version"],
        system=row["system"],
        template=row["template"],
        reduce_template=row["reduce_template"],
        model=row["model"],
        temperature=row["temperature"],
        max_tokens=row["max_tokens"],
    )


# --- Reads ---------------------------------------------------------------


def get(conn: sqlite3.Connection, prompt_id: int) -> Prompt | None:
    row = conn.execute(
        "SELECT id, name, description, owner_id, read_only, archived "
        "FROM prompts WHERE id = ?",
        (prompt_id,),
    ).fetchone()
    return _row_to_prompt(row) if row else None


def list_active(conn: sqlite3.Connection) -> list[Prompt]:
    """Non-archived prompts for the picker, official first then by name."""
    rows = conn.execute(
        "SELECT id, name, description, owner_id, read_only, archived "
        "FROM prompts WHERE archived = 0 "
        "ORDER BY (owner_id IS NOT NULL), name COLLATE NOCASE"
    ).fetchall()
    return [_row_to_prompt(r) for r in rows]


def latest_version(conn: sqlite3.Connection, prompt_id: int) -> PromptVersion | None:
    row = conn.execute(
        "SELECT id, prompt_id, version, system, template, reduce_template, "
        "       model, temperature, max_tokens "
        "FROM prompt_versions WHERE prompt_id = ? "
        "ORDER BY version DESC LIMIT 1",
        (prompt_id,),
    ).fetchone()
    return _row_to_version(row) if row else None


def get_version(conn: sqlite3.Connection, version_id: int) -> PromptVersion | None:
    row = conn.execute(
        "SELECT id, prompt_id, version, system, template, reduce_template, "
        "       model, temperature, max_tokens "
        "FROM prompt_versions WHERE id = ?",
        (version_id,),
    ).fetchone()
    return _row_to_version(row) if row else None


def list_for_manager(
    conn: sqlite3.Connection,
    *,
    query: str | None = None,
    include_archived: bool = False,
) -> list[PromptSummary]:
    """Prompts for the management UI, with owner name and version stats.

    Officials (no owner) sort first, then by name. ``query`` matches name or
    description (case-insensitive substring). Archived prompts are excluded
    unless ``include_archived`` is set.
    """
    sql = (
        "SELECT p.id, p.name, p.description, p.owner_id, p.read_only, p.archived, "
        "       u.username AS owner_username, "
        "       COUNT(pv.id) AS version_count, "
        "       COALESCE(MAX(pv.version), 0) AS latest_version "
        "FROM prompts p "
        "LEFT JOIN users u ON u.id = p.owner_id "
        "LEFT JOIN prompt_versions pv ON pv.prompt_id = p.id "
    )
    where: list[str] = []
    params: list[object] = []
    if not include_archived:
        where.append("p.archived = 0")
    if query:
        where.append("(p.name LIKE ? OR IFNULL(p.description, '') LIKE ?)")
        like = f"%{query}%"
        params.extend([like, like])
    if where:
        sql += "WHERE " + " AND ".join(where) + " "
    sql += (
        "GROUP BY p.id "
        "ORDER BY (p.owner_id IS NOT NULL), p.name COLLATE NOCASE"
    )
    rows = conn.execute(sql, params).fetchall()
    return [
        PromptSummary(
            prompt=_row_to_prompt(r),
            owner_username=r["owner_username"],
            version_count=r["version_count"],
            latest_version=r["latest_version"],
        )
        for r in rows
    ]


def list_versions(conn: sqlite3.Connection, prompt_id: int) -> list[PromptVersion]:
    """All versions of a prompt, newest first (for the version-history panel)."""
    rows = conn.execute(
        "SELECT id, prompt_id, version, system, template, reduce_template, "
        "       model, temperature, max_tokens "
        "FROM prompt_versions WHERE prompt_id = ? ORDER BY version DESC",
        (prompt_id,),
    ).fetchall()
    return [_row_to_version(r) for r in rows]


def can_edit(prompt: Prompt, user: User) -> bool:
    """Who may edit a prompt (PLAN.md §4): official prompts by admins only;
    personal prompts by their owner or an admin."""
    if prompt.is_official:
        return user.is_admin
    return user.is_admin or prompt.owner_id == user.id


# --- Writes --------------------------------------------------------------


def create_prompt(
    conn: sqlite3.Connection,
    *,
    name: str,
    description: str | None,
    system: str | None,
    template: str,
    reduce_template: str | None = None,
    model: str | None = None,
    temperature: float | None = None,
    max_tokens: int | None = None,
    owner_id: int | None = None,
    read_only: bool = False,
    created_by: int | None = None,
) -> Prompt:
    """Create a prompt and its first version (v1). ``owner_id=None`` +
    ``read_only=True`` makes an official prompt."""
    if TRANSCRIPT_PLACEHOLDER not in template:
        raise ValueError(f"template must contain {TRANSCRIPT_PLACEHOLDER}")
    cur = conn.execute(
        "INSERT INTO prompts (name, description, owner_id, read_only, created_by) "
        "VALUES (?, ?, ?, ?, ?)",
        (name, description, owner_id, 1 if read_only else 0, created_by),
    )
    prompt_id = cur.lastrowid
    conn.execute(
        "INSERT INTO prompt_versions "
        "(prompt_id, version, system, template, reduce_template, model, "
        " temperature, max_tokens) VALUES (?, 1, ?, ?, ?, ?, ?, ?)",
        (prompt_id, system, template, reduce_template, model, temperature, max_tokens),
    )
    conn.commit()
    prompt = get(conn, prompt_id)
    assert prompt is not None
    return prompt


def _next_version_number(conn: sqlite3.Connection, prompt_id: int) -> int:
    row = conn.execute(
        "SELECT MAX(version) AS v FROM prompt_versions WHERE prompt_id = ?",
        (prompt_id,),
    ).fetchone()
    return (row["v"] or 0) + 1


def add_version(
    conn: sqlite3.Connection,
    prompt_id: int,
    *,
    system: str | None,
    template: str,
    reduce_template: str | None = None,
    model: str | None = None,
    temperature: float | None = None,
    max_tokens: int | None = None,
) -> int:
    """Append a new version to an existing prompt. Older versions are kept so
    notes generated from them stay attributable/reproducible (PLAN.md §4)."""
    if TRANSCRIPT_PLACEHOLDER not in template:
        raise ValueError(f"template must contain {TRANSCRIPT_PLACEHOLDER}")
    version = _next_version_number(conn, prompt_id)
    cur = conn.execute(
        "INSERT INTO prompt_versions "
        "(prompt_id, version, system, template, reduce_template, model, "
        " temperature, max_tokens) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (prompt_id, version, system, template, reduce_template, model, temperature, max_tokens),
    )
    conn.commit()
    return cur.lastrowid


def update_meta(
    conn: sqlite3.Connection,
    prompt_id: int,
    *,
    name: str,
    description: str | None,
) -> None:
    """Update the non-versioned fields (name, description) on a prompt."""
    conn.execute(
        "UPDATE prompts SET name = ?, description = ? WHERE id = ?",
        (name, description, prompt_id),
    )
    conn.commit()


def edit_prompt(
    conn: sqlite3.Connection,
    prompt_id: int,
    *,
    name: str,
    description: str | None,
    system: str | None,
    template: str,
    reduce_template: str | None = None,
    model: str | None = None,
    temperature: float | None = None,
    max_tokens: int | None = None,
) -> bool:
    """Save an edit: always refresh name/description; append a new version only
    when a versioned field actually changed (so trivial metadata edits don't
    spawn versions). Returns True iff a new version was created."""
    if TRANSCRIPT_PLACEHOLDER not in template:
        raise ValueError(f"template must contain {TRANSCRIPT_PLACEHOLDER}")
    update_meta(conn, prompt_id, name=name, description=description)

    latest = latest_version(conn, prompt_id)
    desired = (system, template, reduce_template, model, temperature, max_tokens)
    current = (
        (
            latest.system,
            latest.template,
            latest.reduce_template,
            latest.model,
            latest.temperature,
            latest.max_tokens,
        )
        if latest
        else None
    )
    if current == desired:
        return False
    add_version(
        conn,
        prompt_id,
        system=system,
        template=template,
        reduce_template=reduce_template,
        model=model,
        temperature=temperature,
        max_tokens=max_tokens,
    )
    return True


def clone_prompt(
    conn: sqlite3.Connection,
    source_prompt_id: int,
    *,
    owner_id: int,
    created_by: int,
) -> Prompt:
    """Clone any prompt into a personal, editable copy owned by ``owner_id``.

    The copy records which version it came from (``cloned_from_version_id``)
    so official-prompt lineage stays attributable (PLAN.md §4)."""
    source = get(conn, source_prompt_id)
    version = latest_version(conn, source_prompt_id)
    if source is None or version is None:
        raise ValueError("prompt to clone not found")
    cur = conn.execute(
        "INSERT INTO prompts "
        "(name, description, owner_id, read_only, cloned_from_version_id, created_by) "
        "VALUES (?, ?, ?, 0, ?, ?)",
        (f"{source.name} (copy)", source.description, owner_id, version.id, created_by),
    )
    prompt_id = cur.lastrowid
    conn.execute(
        "INSERT INTO prompt_versions "
        "(prompt_id, version, system, template, reduce_template, model, "
        " temperature, max_tokens) VALUES (?, 1, ?, ?, ?, ?, ?, ?)",
        (
            prompt_id,
            version.system,
            version.template,
            version.reduce_template,
            version.model,
            version.temperature,
            version.max_tokens,
        ),
    )
    conn.commit()
    clone = get(conn, prompt_id)
    assert clone is not None
    return clone


def set_archived(conn: sqlite3.Connection, prompt_id: int, archived: bool) -> None:
    conn.execute(
        "UPDATE prompts SET archived = ? WHERE id = ?",
        (1 if archived else 0, prompt_id),
    )
    conn.commit()


# --- Starter set ---------------------------------------------------------

_GENERIC_SYSTEM = (
    "You are an expert meeting assistant. You are given the transcript of a "
    "meeting and must produce clear, accurate, well-structured Markdown. "
    "Always write your response in the same language as the transcript "
    "(e.g. a Dutch transcript gets Dutch notes). "
    "Never invent facts that are not supported by the transcript."
)

_STARTER_PROMPTS: list[dict] = [
    {
        "name": "summary",
        "description": "Concise narrative summary of the meeting.",
        "system": _GENERIC_SYSTEM,
        "template": (
            "Summarize the following meeting transcript in a few short "
            "paragraphs. Capture the purpose, the main topics discussed, and "
            "the outcome. Use Markdown.\n\n"
            "Transcript:\n{transcript}"
        ),
        "reduce_template": (
            "The following are summaries of consecutive parts of one meeting. "
            "Merge them into a single coherent summary without repetition, "
            "preserving chronology. Use Markdown.\n\n{transcript}"
        ),
    },
    {
        "name": "action-items",
        "description": "Actionable tasks with owners and due dates where stated.",
        "system": _GENERIC_SYSTEM,
        "template": (
            "Extract all action items from the meeting transcript below. Return "
            "a Markdown checklist; for each item note the owner and due date if "
            "they were mentioned, otherwise leave them out. If there are none, "
            "say so.\n\nTranscript:\n{transcript}"
        ),
        "reduce_template": (
            "Combine these action-item lists from consecutive parts of one "
            "meeting into a single de-duplicated Markdown checklist.\n\n{transcript}"
        ),
    },
    {
        "name": "decisions",
        "description": "Decisions that were made during the meeting.",
        "system": _GENERIC_SYSTEM,
        "template": (
            "List the decisions made in the meeting transcript below as a "
            "Markdown bullet list. For each, state the decision and, if given, "
            "the rationale. If no decisions were made, say so.\n\n"
            "Transcript:\n{transcript}"
        ),
        "reduce_template": (
            "Merge these decision lists from consecutive parts of one meeting "
            "into one de-duplicated Markdown list.\n\n{transcript}"
        ),
    },
    {
        "name": "minutes",
        "description": "Formal meeting minutes.",
        "system": _GENERIC_SYSTEM,
        "template": (
            "Write formal meeting minutes for the transcript below in Markdown, "
            "with sections for Attendees (if identifiable), Agenda/Topics, "
            "Discussion, Decisions, and Action Items. Only include what the "
            "transcript supports.\n\nTranscript:\n{transcript}"
        ),
        "reduce_template": (
            "Merge these partial minutes from consecutive parts of one meeting "
            "into a single set of minutes with the same sections, without "
            "repetition.\n\n{transcript}"
        ),
    },
    {
        "name": "qa-highlights",
        "description": "Key questions raised and the answers given.",
        "system": _GENERIC_SYSTEM,
        "template": (
            "From the meeting transcript below, extract the notable questions "
            "that were asked and the answers given, as a Markdown list of "
            "**Q:** / **A:** pairs. If a question went unanswered, note that.\n\n"
            "Transcript:\n{transcript}"
        ),
        "reduce_template": (
            "Merge these Q&A lists from consecutive parts of one meeting into a "
            "single de-duplicated Markdown list of Q/A pairs.\n\n{transcript}"
        ),
    },
]


def seed_starter_prompts(conn: sqlite3.Connection) -> int:
    """Seed/refresh the official starter set. Idempotent when the specs are
    unchanged. Returns the number of prompts newly created.

    On an existing deployment, when a starter's text has changed since it was
    seeded (e.g. a new language instruction), a new prompt *version* is appended
    rather than editing history in place — old notes keep pointing at the
    version that produced them. Official starters are read-only and have no edit
    path, so their latest version is always a previous seed and safe to bump.
    """
    created = 0
    updated = 0
    for spec in _STARTER_PROMPTS:
        row = conn.execute(
            "SELECT id FROM prompts WHERE name = ? AND owner_id IS NULL LIMIT 1",
            (spec["name"],),
        ).fetchone()
        if row is None:
            create_prompt(
                conn,
                name=spec["name"],
                description=spec["description"],
                system=spec["system"],
                template=spec["template"],
                reduce_template=spec["reduce_template"],
                owner_id=None,
                read_only=True,
            )
            created += 1
            continue

        latest = latest_version(conn, row["id"])
        current = (latest.system, latest.template, latest.reduce_template) if latest else None
        desired = (spec["system"], spec["template"], spec["reduce_template"])
        if current != desired:
            add_version(
                conn,
                row["id"],
                system=spec["system"],
                template=spec["template"],
                reduce_template=spec["reduce_template"],
            )
            updated += 1

    if created:
        log.info("Seeded %d official starter prompt(s).", created)
    if updated:
        log.info("Upgraded %d starter prompt(s) to a new version.", updated)
    return created
