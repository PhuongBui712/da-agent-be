"""Outputs registry — `outputs/registry.json` mirrors `KbRegistry`.

Standalone outputs only (kind=standalone). KB-bound and attachment-bound
writes are also routed under this layout (Phase C 2026-05-31 — Golden Rule 4
broken per user approval); the legacy `kb/<kb_id>/versions/` and
`attachments/<sid>/<att_id>/versions/` chains are no longer written to.

Layout (Phase C 2026-05-31):

    outputs/
      registry.json                   # this file
      <session_id>/
        <output_id>/
          <filename>                  # the actual bytes
          meta.json                   # sidecar mirroring the registry row
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import shutil
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# `out_<16hex>` — matches `_new_id` and the route layer's `_new_output_id`.
_OUTPUT_ID_RE = re.compile(r"^out_[0-9a-f]{16}$")


def _now() -> float:
    return time.time()


def _new_id() -> str:
    return f"out_{uuid.uuid4().hex[:16]}"


@dataclass(slots=True)
class OutputMeta:
    """Spec §8.2 — `outputs/<session_id>/<id>/meta.json` shape.

    `session_id` is the layout key (which session's directory the file lives
    under). `source_session_id` is the spec §8.2 schema field — for files
    minted in this layout they are always equal, but we keep both so the
    schema stays stable for future fork/import scenarios.
    """

    id: str  # output_id "out_<16hex>"
    kind: str  # "standalone" only in this registry
    title: str
    filename: str  # name on disk under outputs/<session_id>/<id>/
    mime: str
    size_bytes: int
    session_id: str | None = None  # layout key
    source_session_id: str | None = None  # spec §8.2 provenance field
    source_kb_ids: list[str] = field(default_factory=list)
    created_at: float = field(default_factory=_now)

    def to_dict(self) -> dict[str, Any]:
        return {
            "output_id": self.id,
            "kind": self.kind,
            "title": self.title,
            "filename": self.filename,
            "mime": self.mime,
            "size_bytes": self.size_bytes,
            "session_id": self.session_id,
            "source_session_id": self.source_session_id,
            "source_kb_ids": list(self.source_kb_ids),
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "OutputMeta":
        # Legacy rows (pre-Phase-C) lack `session_id`; fall back to
        # `source_session_id` so the row remains addressable. If both are
        # missing the row is effectively orphaned — `load()` will warn.
        session_id = d.get("session_id") or d.get("source_session_id")
        return cls(
            id=d.get("output_id") or d["id"],
            kind=d.get("kind", "standalone"),
            title=d.get("title", ""),
            filename=d.get("filename", ""),
            mime=d.get("mime", "application/octet-stream"),
            size_bytes=int(d.get("size_bytes", 0)),
            session_id=session_id,
            source_session_id=d.get("source_session_id"),
            source_kb_ids=list(d.get("source_kb_ids") or []),
            created_at=float(d.get("created_at", _now())),
        )


class OutputsRegistry:
    """Single JSON file at `root/registry.json`.

    Files at `root/<session_id>/<output_id>/<filename>` (Phase C 2026-05-31).
    """

    def __init__(self, root: Path) -> None:
        self.root = root
        self.path = root / "registry.json"
        self._lock = asyncio.Lock()
        self._items: dict[str, OutputMeta] = {}
        self._loaded = False

    async def load(self) -> None:
        if self._loaded:
            return
        if self.path.exists():
            try:
                raw = json.loads(self.path.read_text("utf-8"))
                for item in raw.get("outputs", []):
                    meta = OutputMeta.from_dict(item)
                    if meta.session_id is None:
                        logger.warning(
                            "legacy outputs row %s has no session_id; keeping but unindexed by session",
                            meta.id,
                        )
                    self._items[meta.id] = meta
            except (json.JSONDecodeError, OSError):
                # Corrupt registry — start clean rather than refusing to boot.
                self._items.clear()
        # Detect legacy flat-layout dirs (pre-Phase-C) and warn. Do NOT
        # migrate or delete — that's an operator decision.
        if self.root.exists():
            try:
                for entry in self.root.iterdir():
                    if entry.is_dir() and _OUTPUT_ID_RE.match(entry.name):
                        logger.warning(
                            "legacy flat outputs dir %s; not indexed (Phase C migration required)",
                            entry,
                        )
            except OSError:
                pass
        self._loaded = True

    async def _flush_locked(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"outputs": [m.to_dict() for m in self._items.values()]}
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        tmp.replace(self.path)

    def path_for(self, meta: OutputMeta) -> Path:
        """`<root>/<session_id>/<output_id>/<filename>`.

        Falls back to `<root>/<output_id>/<filename>` for legacy rows with no
        `session_id` so existing references don't crash; new code should
        always set `session_id`.
        """
        if meta.session_id is None:
            return self.root / meta.id / meta.filename
        return self.root / meta.session_id / meta.id / meta.filename

    def output_dir(self, meta: OutputMeta) -> Path:
        """`<root>/<session_id>/<output_id>/`. Same legacy fallback as `path_for`."""
        if meta.session_id is None:
            return self.root / meta.id
        return self.root / meta.session_id / meta.id

    async def list(self, *, session_id: str | None = None) -> list[OutputMeta]:
        async with self._lock:
            await self.load()
            items = list(self._items.values())
        if session_id is not None:
            items = [m for m in items if m.source_session_id == session_id]
        items.sort(key=lambda m: m.created_at, reverse=True)
        return items

    async def get(self, output_id: str) -> OutputMeta | None:
        async with self._lock:
            await self.load()
            return self._items.get(output_id)

    async def register_standalone(
        self,
        *,
        session_id: str,
        src_path: Path,
        title: str,
        filename: str,
        mime: str,
        source_session_id: str | None = None,
        source_kb_ids: list[str] | None = None,
    ) -> OutputMeta:
        """Register an already-written file: move it under `outputs/<sid>/<id>/<filename>`.

        If `src_path` already lives at the canonical target path, no move
        happens and we just stamp the sidecar `meta.json`.

        `source_session_id` defaults to `session_id` when not provided — the
        common case (file was created in this session). Pass an explicit
        value only for fork/import scenarios.
        """
        async with self._lock:
            await self.load()
            output_id = _new_id()
            target_dir = self.root / session_id / output_id
            target_dir.mkdir(parents=True, exist_ok=True)
            target_path = target_dir / filename
            size = src_path.stat().st_size if src_path.exists() else 0
            if src_path.resolve() != target_path.resolve():
                shutil.move(str(src_path), str(target_path))
            meta = OutputMeta(
                id=output_id,
                kind="standalone",
                title=title,
                filename=filename,
                mime=mime,
                size_bytes=size,
                session_id=session_id,
                source_session_id=source_session_id or session_id,
                source_kb_ids=list(source_kb_ids or []),
            )
            (target_dir / "meta.json").write_text(
                json.dumps(meta.to_dict(), indent=2), encoding="utf-8"
            )
            self._items[output_id] = meta
            await self._flush_locked()
            return meta

    async def adopt_at(
        self,
        *,
        output_id: str,
        session_id: str,
        title: str,
        filename: str,
        mime: str,
        source_session_id: str | None = None,
        source_kb_ids: list[str] | None = None,
    ) -> OutputMeta | None:
        """Adopt an existing `outputs/<session_id>/<output_id>/<filename>` path.

        Used when the model wrote directly into the harness-resolved path
        using an id we minted upstream. Returns None if the file is missing;
        if the id is already registered, returns the existing row unchanged.
        """
        target_dir = self.root / session_id / output_id
        target_path = target_dir / filename
        if not target_path.exists():
            return None
        async with self._lock:
            await self.load()
            if output_id in self._items:
                return self._items[output_id]
            size = target_path.stat().st_size
            meta = OutputMeta(
                id=output_id,
                kind="standalone",
                title=title,
                filename=filename,
                mime=mime,
                size_bytes=size,
                session_id=session_id,
                source_session_id=source_session_id or session_id,
                source_kb_ids=list(source_kb_ids or []),
            )
            (target_dir / "meta.json").write_text(
                json.dumps(meta.to_dict(), indent=2), encoding="utf-8"
            )
            self._items[output_id] = meta
            await self._flush_locked()
            return meta

    async def delete(self, output_id: str) -> bool:
        async with self._lock:
            await self.load()
            meta = self._items.pop(output_id, None)
            if meta is None:
                return False
            await self._flush_locked()
        target_dir = self.output_dir(meta)
        if target_dir.exists():
            await asyncio.to_thread(shutil.rmtree, str(target_dir), True)
        return True

    async def delete_session_outputs(self, session_id: str) -> None:
        """Bulk-remove every output owned by `session_id` (Phase C cleanup hook).

        Wipes the on-disk session subtree (`<root>/<session_id>/`) and drops
        all matching registry rows. Best-effort — never raises; missing
        directories are not an error.
        """
        async with self._lock:
            await self.load()
            removed_ids = [
                oid
                for oid, meta in self._items.items()
                if meta.session_id == session_id
            ]
            for oid in removed_ids:
                self._items.pop(oid, None)
            if removed_ids:
                await self._flush_locked()
        session_dir = self.root / session_id
        if session_dir.exists():
            await asyncio.to_thread(shutil.rmtree, str(session_dir), True)
