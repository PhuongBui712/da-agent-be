"""Ingestion registry — `kb/registry.json`, atomic-rename writes.

Status state machine:

    PENDING -> PROFILING -> READY
                       \\-> READY_PARTIAL    (profiler failed; raw.xlsx still usable)
                       \\-> FAILED           (catastrophic; KB cannot be scoped)

Crash recovery: any leftover PROFILING row is rewritten to FAILED on `load`
with `error="interrupted by restart"`. There is no auto-retry — the user
either re-uploads or hits POST /kb/files/{id}/reprofile manually.

`memory_path` is set when the kb_profiler subagent writes its per-KB note.
It stays None for legacy rows that were ingested via the old manifest pipeline.
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

IngestionStatus = Literal[
    "PENDING",
    "PROFILING",
    "READY",
    "READY_PARTIAL",
    "FAILED",
]


def _now() -> float:
    return time.time()


def _new_id() -> str:
    return f"kb_{uuid.uuid4().hex[:16]}"


@dataclass(slots=True)
class IngestionMeta:
    id: str
    filename: str
    size_bytes: int
    status: IngestionStatus = "PENDING"
    created_at: float = field(default_factory=_now)
    updated_at: float = field(default_factory=_now)
    error: str | None = None
    # Absolute path on disk to the per-KB memory note written by the
    # kb_profiler subagent. None if the file was ingested by the legacy
    # manifest pipeline OR if the profiler failed.
    memory_path: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "filename": self.filename,
            "size_bytes": self.size_bytes,
            "status": self.status,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "error": self.error,
            "memory_path": self.memory_path,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "IngestionMeta":
        # Forward-compatible: legacy rows use `PROCESSING` (manifest pipeline).
        # Map them to FAILED on load with a hint so the user knows to reprofile;
        # we do NOT try to keep the old pipeline alive in parallel.
        raw_status = d.get("status", "PENDING")
        if raw_status == "PROCESSING":
            raw_status = "FAILED"
            err_hint = d.get("error") or "legacy manifest row; reprofile to enable"
        else:
            err_hint = d.get("error")
        return cls(
            id=d["id"],
            filename=d["filename"],
            size_bytes=int(d.get("size_bytes", 0)),
            status=raw_status,
            created_at=float(d.get("created_at", _now())),
            updated_at=float(d.get("updated_at", _now())),
            error=err_hint,
            memory_path=d.get("memory_path"),
        )


class IngestionRegistry:
    """Persistent JSON registry (`kb/registry.json`) — atomic-rename writes."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = asyncio.Lock()
        self._items: dict[str, IngestionMeta] = {}
        self._loaded = False

    async def load(self) -> None:
        if self._loaded:
            return
        migrated = False
        if self.path.exists():
            try:
                raw = json.loads(self.path.read_text("utf-8"))
                for item in raw.get("files", []):
                    # `from_dict` rewrites legacy `PROCESSING` rows (manifest
                    # pipeline) to FAILED. Detect that here so we know the
                    # on-disk JSON drifted and must be re-flushed.
                    if item.get("status") == "PROCESSING":
                        migrated = True
                    meta = IngestionMeta.from_dict(item)
                    if meta.status == "PROFILING":
                        meta.status = "FAILED"
                        meta.error = "interrupted by restart"
                        meta.updated_at = _now()
                        migrated = True
                    self._items[meta.id] = meta
            except (json.JSONDecodeError, OSError):
                self._items.clear()
        self._loaded = True
        if migrated:
            await self._flush_locked()

    async def _flush_locked(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"files": [m.to_dict() for m in self._items.values()]}
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        tmp.replace(self.path)

    async def list(self) -> list[IngestionMeta]:
        async with self._lock:
            await self.load()
            return sorted(
                self._items.values(), key=lambda m: m.updated_at, reverse=True
            )

    async def get(self, kb_id: str) -> IngestionMeta | None:
        async with self._lock:
            await self.load()
            return self._items.get(kb_id)

    async def create(self, *, filename: str, size_bytes: int) -> IngestionMeta:
        async with self._lock:
            await self.load()
            meta = IngestionMeta(id=_new_id(), filename=filename, size_bytes=size_bytes)
            self._items[meta.id] = meta
            await self._flush_locked()
            return meta

    async def update_status(
        self,
        kb_id: str,
        status: IngestionStatus,
        *,
        error: str | None = None,
        memory_path: str | None = None,
    ) -> IngestionMeta | None:
        """Transition status. `error` is captured on FAILED/READY_PARTIAL only;
        cleared on success states. `memory_path` is set when given; pass
        explicit `None` to keep the prior value (no-op)."""
        async with self._lock:
            await self.load()
            meta = self._items.get(kb_id)
            if meta is None:
                return None
            meta.status = status
            if status in {"FAILED", "READY_PARTIAL"}:
                meta.error = error
            else:
                meta.error = None
            if memory_path is not None:
                meta.memory_path = memory_path
            meta.updated_at = _now()
            await self._flush_locked()
            return meta

    async def clear_memory_path(self, kb_id: str) -> IngestionMeta | None:
        """Used when /reprofile is triggered or when a legacy row is rebuilt.

        Keeps status untouched; just nulls `memory_path` and bumps updated_at.
        """
        async with self._lock:
            await self.load()
            meta = self._items.get(kb_id)
            if meta is None:
                return None
            meta.memory_path = None
            meta.updated_at = _now()
            await self._flush_locked()
            return meta

    async def delete(self, kb_id: str) -> bool:
        async with self._lock:
            await self.load()
            if kb_id not in self._items:
                return False
            del self._items[kb_id]
            await self._flush_locked()
            return True
