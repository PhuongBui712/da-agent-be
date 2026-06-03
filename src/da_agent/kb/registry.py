"""KB registry — `kb/registry.json`.

One JSON file on disk, atomic-rename writes, single asyncio.Lock. Holds
per-KB metadata (status + error). The actual `manifest.json` and `raw.xlsx`
live one level deeper inside `kb/<kb_id>/`.

Status state machine:

    PENDING -> PROCESSING -> READY
                        \\-> FAILED   (error: str captured)

Crash recovery: `load()` rewrites any leftover PROCESSING rows to FAILED with
`error="interrupted by restart"`. There is no retry path — the user re-uploads.
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

KbStatus = Literal["PENDING", "PROCESSING", "READY", "FAILED"]


def _now() -> float:
    return time.time()


def _new_id() -> str:
    return f"kb_{uuid.uuid4().hex[:16]}"


@dataclass(slots=True)
class KbMeta:
    id: str
    filename: str  # original filename, sanitized (no path components)
    size_bytes: int
    status: KbStatus = "PENDING"
    created_at: float = field(default_factory=_now)
    updated_at: float = field(default_factory=_now)
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "filename": self.filename,
            "size_bytes": self.size_bytes,
            "status": self.status,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "error": self.error,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "KbMeta":
        return cls(
            id=d["id"],
            filename=d["filename"],
            size_bytes=int(d.get("size_bytes", 0)),
            status=d.get("status", "PENDING"),
            created_at=float(d.get("created_at", _now())),
            updated_at=float(d.get("updated_at", _now())),
            error=d.get("error"),
        )


class KbRegistry:
    """Single JSON file on disk. Atomic-rename writes."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = asyncio.Lock()
        self._items: dict[str, KbMeta] = {}
        self._loaded = False

    async def load(self) -> None:
        if self._loaded:
            return
        if self.path.exists():
            try:
                raw = json.loads(self.path.read_text("utf-8"))
                for item in raw.get("files", []):
                    meta = KbMeta.from_dict(item)
                    # Sweep crash-interrupted rows on boot (see module docstring).
                    if meta.status == "PROCESSING":
                        meta.status = "FAILED"
                        meta.error = "interrupted by restart"
                        meta.updated_at = _now()
                    self._items[meta.id] = meta
            except (json.JSONDecodeError, OSError):
                self._items.clear()
        self._loaded = True
        # Persist any sweep transitions so a second boot is consistent.
        if any(m.error == "interrupted by restart" for m in self._items.values()):
            await self._flush_locked()

    async def _flush_locked(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"files": [m.to_dict() for m in self._items.values()]}
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        tmp.replace(self.path)

    async def list(self) -> list[KbMeta]:
        async with self._lock:
            await self.load()
            return sorted(
                self._items.values(), key=lambda m: m.updated_at, reverse=True
            )

    async def get(self, kb_id: str) -> KbMeta | None:
        async with self._lock:
            await self.load()
            return self._items.get(kb_id)

    async def create(self, *, filename: str, size_bytes: int) -> KbMeta:
        async with self._lock:
            await self.load()
            meta = KbMeta(id=_new_id(), filename=filename, size_bytes=size_bytes)
            self._items[meta.id] = meta
            await self._flush_locked()
            return meta

    async def update_status(
        self, kb_id: str, status: KbStatus, *, error: str | None = None
    ) -> KbMeta | None:
        async with self._lock:
            await self.load()
            meta = self._items.get(kb_id)
            if meta is None:
                return None
            meta.status = status
            meta.error = error if status == "FAILED" else None
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
