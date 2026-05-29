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
    manifest_path: Path
    manifest_size: int


@dataclass(slots=True)
class ScopeAttachmentEntry:
    attachment_id: str
    filename: str
    file_path: Path


@dataclass(slots=True)
class ScopeBlock:
    kb_entries: list[ScopeKbEntry] = field(default_factory=list)
    attachment_entries: list[ScopeAttachmentEntry] = field(default_factory=list)
    total_manifest_bytes: int = 0


def _bad_request(message: str) -> HTTPException:
    """Spec §8.5 validation table — body == {"error": "<message>"}.

    FastAPI wraps `HTTPException.detail` under a top-level `detail` key, so
    clients see `{"detail": {"error": "..."}}` and can read `detail.error`
    for the verbatim spec string.
    """
    return HTTPException(status_code=400, detail={"error": message})


async def build_scope(*, state: AppState, sid: str, body: MessageRequest) -> ScopeBlock:
    """Run the spec §8.5 validation table; raises 400 on first failure.

    Validation order (spec §8.5 lines 656-662):
        1. kb_scope == []   -> 400 "kb_scope cannot be empty; omit the field for default-all"
        2. unknown kb_id    -> 400 "unknown kb_id: <id>"
        3. non-READY id     -> 400 "kb <id> is in status <X>; only READY files can be scoped"
        4. duplicate att_id -> 400 "duplicate attachment_id"
        5. unknown att_id   -> 400 "unknown attachment_id: <id>"

    On success, soft-warn (log only) when total manifest bytes >
    settings.scope_warn_bytes (spec §8.5 line 724).
    """
    block = ScopeBlock()

    # --- KB scope ---
    if body.kb_scope is None:
        # Default-all: every READY KB. Spec §8.5 lines 689-691.
        for meta in await state.kb.list():
            if meta.status == "READY":
                entry = _kb_entry(state, meta)
                block.kb_entries.append(entry)
                block.total_manifest_bytes += entry.manifest_size
    else:
        if len(body.kb_scope) == 0:
            raise _bad_request(
                "kb_scope cannot be empty; omit the field for default-all"
            )
        for kb_id in body.kb_scope:
            meta = await state.kb.get(kb_id)
            if meta is None:
                raise _bad_request(f"unknown kb_id: {kb_id}")
            if meta.status != "READY":
                raise _bad_request(
                    f"kb {kb_id} is in status {meta.status}; only READY files can be scoped"
                )
            entry = _kb_entry(state, meta)
            block.kb_entries.append(entry)
            block.total_manifest_bytes += entry.manifest_size

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

    # --- Soft-warn over scope_warn_bytes (spec §8.5 line 724) ---
    if block.total_manifest_bytes > state.settings.scope_warn_bytes:
        _LOG.warning(
            "scope block manifest bytes %d exceed scope_warn_bytes %d",
            block.total_manifest_bytes,
            state.settings.scope_warn_bytes,
        )

    return block


def _kb_entry(state: AppState, meta) -> ScopeKbEntry:
    manifest_path = state.settings.kb_dir / meta.id / "manifest.json"
    size = manifest_path.stat().st_size if manifest_path.exists() else 0
    return ScopeKbEntry(
        kb_id=meta.id,
        filename=meta.filename,
        manifest_path=manifest_path,
        manifest_size=size,
    )


def render_scope(block: ScopeBlock, user_prompt: str) -> str:
    """Compose the <scope>…</scope> block per spec §8.5 lines 674-687.

    Form:
        <scope>
        For this turn, only these KB files are in scope:
        - kb_<id> (<filename>) — manifest at <path>

        Short-term attachments (no manifest, read directly with xlsx skill):
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
            lines.append(f"- {e.kb_id} ({e.filename}) — manifest at {e.manifest_path}")
    else:
        lines.append("For this turn, no KB files are in scope.")

    if block.attachment_entries:
        lines.append("")
        lines.append(
            "Short-term attachments (no manifest, read directly with xlsx skill):"
        )
        for a in block.attachment_entries:
            lines.append(f"- {a.file_path}")

    lines.append("</scope>")
    lines.append("")
    lines.append("<user_prompt>")
    lines.append(user_prompt)
    lines.append("</user_prompt>")
    return "\n".join(lines)
