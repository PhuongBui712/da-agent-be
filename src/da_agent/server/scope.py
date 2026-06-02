"""Per-turn data scope (spec §8.5).

Validates `kb_scope` + `attachments` from the message request and composes
the <scope> block prepended to the user prompt. The agent sees the result
as a single prompt string; the SDK transcript records the composed form,
which is intentional (debuggable, replayable, visible to subagents).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

from fastapi import HTTPException

from .schemas import MessageRequest
from .state import AppState

_LOG = logging.getLogger(__name__)


@dataclass(slots=True)
class ScopeKbEntry:
    kb_id: str
    filename: str
    # Absolute path to the per-KB memory note written by kb_profiler. None for
    # legacy rows that were ingested via the old manifest pipeline OR for
    # READY_PARTIAL rows where the profiler failed; in either case the agent
    # falls back to inspecting raw.xlsx via the xlsx skill.
    memory_path: Path | None
    memory_size: int


@dataclass(slots=True)
class ScopeAttachmentEntry:
    attachment_id: str
    filename: str
    file_path: Path


@dataclass(slots=True)
class ScopeBlock:
    kb_entries: list[ScopeKbEntry] = field(default_factory=list)
    attachment_entries: list[ScopeAttachmentEntry] = field(default_factory=list)
    total_memory_bytes: int = 0


def _bad_request(message: str) -> HTTPException:
    """Spec §8.5 validation table — body == {"error": "<message>"}.

    FastAPI wraps `HTTPException.detail` under a top-level `detail` key, so
    clients see `{"detail": {"error": "..."}}` and can read `detail.error`
    for the verbatim spec string.
    """
    return HTTPException(status_code=400, detail={"error": message})


_SCOPABLE_STATUSES = {"READY", "READY_PARTIAL"}


async def build_scope(*, state: AppState, sid: str, body: MessageRequest) -> ScopeBlock:
    """Run the spec §8.5 validation table; raises 400 on first failure.

    Default semantics (2026-06-02): a payload with no `kb_scope` field — or an
    explicit empty list — yields an empty <scope> block. The caller (FE) MUST
    list the kb_ids it wants in scope; the BE never silently auto-loads every
    READY KB. This mirrors the symlink-farm filesystem layer (the operative
    permission gate) — both are "explicit-only".

    Validation order:
        1. unknown kb_id    -> 400 "unknown kb_id: <id>"
        2. non-scopable id  -> 400 "kb <id> is in status <X>; only READY/READY_PARTIAL files can be scoped"
        3. duplicate att_id -> 400 "duplicate attachment_id"
        4. unknown att_id   -> 400 "unknown attachment_id: <id>"

    READY_PARTIAL is intentionally allowed: those KBs lack a memory note
    (profiler failed) but the raw.xlsx is intact and the agent can still
    inspect it via the xlsx skill. The scope renderer marks them with
    `— NO MEMORY (legacy)`.

    On success, soft-warn (log only) when total memory bytes exceed
    settings.scope_warn_bytes.
    """
    block = ScopeBlock()

    # --- KB scope ---
    # `kb_scope is None` (field omitted) and `kb_scope == []` are now identical:
    # both produce an empty scope. The agent only sees the KBs the FE listed.
    if body.kb_scope:
        for kb_id in body.kb_scope:
            meta = await state.kb.get(kb_id)
            if meta is None:
                raise _bad_request(f"unknown kb_id: {kb_id}")
            if meta.status not in _SCOPABLE_STATUSES:
                raise _bad_request(
                    f"kb {kb_id} is in status {meta.status}; "
                    f"only READY/READY_PARTIAL files can be scoped"
                )
            entry = _kb_entry(state, meta)
            block.kb_entries.append(entry)
            block.total_memory_bytes += entry.memory_size

    # --- Attachments ---
    seen: set[str] = set()
    for ref in body.attachments:
        if ref.attachment_id in seen:
            raise _bad_request("duplicate attachment_id")
        seen.add(ref.attachment_id)
        att_meta = await state.attachments.get(sid, ref.attachment_id)
        if att_meta is None:
            raise _bad_request(f"unknown attachment_id: {ref.attachment_id}")
        att_path = state.attachments.path_for(att_meta)
        if not att_path.exists():
            # Race with delete; treat as unknown per spec §8.5 edge-case table.
            raise _bad_request(f"unknown attachment_id: {ref.attachment_id}")
        block.attachment_entries.append(
            ScopeAttachmentEntry(
                attachment_id=att_meta.id,
                filename=att_meta.filename,
                file_path=att_path,
            )
        )

    # --- Soft-warn over scope_warn_bytes ---
    if block.total_memory_bytes > state.settings.scope_warn_bytes:
        _LOG.warning(
            "scope block memory bytes %d exceed scope_warn_bytes %d",
            block.total_memory_bytes,
            state.settings.scope_warn_bytes,
        )

    return block


def _kb_entry(state: AppState, meta) -> ScopeKbEntry:
    """Resolve the on-disk path of the per-KB memory note.

    `meta.memory_path` is the authoritative pointer (stamped by the
    ingestion runner on success); we only fall back to the conventional
    location for forward-compat. If the file does not exist, the entry is
    returned with `memory_path=None` so the renderer emits the legacy
    fallback line.
    """
    candidate: Path | None = None
    if getattr(meta, "memory_path", None):
        candidate = Path(meta.memory_path)
    else:
        # Conventional location — useful when a profiler succeeded but the
        # registry write was lost (e.g. crash between memory write and flush).
        candidate = state.settings.kb_profiler_memory_dir / f"{meta.id}.md"

    if candidate is not None and candidate.exists():
        size = candidate.stat().st_size
        memory_path: Path | None = candidate
    else:
        size = 0
        memory_path = None

    return ScopeKbEntry(
        kb_id=meta.id,
        filename=meta.filename,
        memory_path=memory_path,
        memory_size=size,
    )


def render_scope(block: ScopeBlock, user_prompt: str) -> str:
    """Compose the <scope>…</scope> block.

    Form:
        <scope>
        For this turn, only these KB files are in scope:
        - kb_<id> (<filename>) — memory at <path>
        - kb_<id> (<filename>) — NO MEMORY (legacy; inspect raw.xlsx via xlsx skill)

        Short-term attachments (no memory, read directly with xlsx skill):
        - <path>
        </scope>

        <user_prompt>
        <text>
        </user_prompt>
    """
    lines: list[str] = ["<scope>"]
    if block.kb_entries:
        lines.append("For this turn, only these KB files are in scope:")
        for e in block.kb_entries:
            if e.memory_path is not None:
                lines.append(f"- {e.kb_id} ({e.filename}) — memory at {e.memory_path}")
            else:
                lines.append(
                    f"- {e.kb_id} ({e.filename}) — NO MEMORY "
                    f"(legacy; inspect raw.xlsx via xlsx skill)"
                )
    else:
        lines.append("For this turn, no KB files are in scope.")

    if block.attachment_entries:
        lines.append("")
        lines.append(
            "Short-term attachments (no memory, read directly with xlsx skill):"
        )
        for a in block.attachment_entries:
            lines.append(f"- {a.file_path}")

    lines.append("</scope>")
    lines.append("")
    lines.append("<user_prompt>")
    lines.append(user_prompt)
    lines.append("</user_prompt>")
    return "\n".join(lines)
