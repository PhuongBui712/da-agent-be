"""Per-session attachments registry.

Each session owns `attachments/<sid>/registry.json`. Reads are lazy (first
access per sid), writes are atomic-rename (mirror kb/registry.py). The whole
`attachments/<sid>/` tree is removed when the session is deleted (spec §5.3).
"""

from __future__ import annotations

import asyncio
import json
import shutil
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any


def _now() -> float:
    return time.time()


def _new_id() -> str:
    return f"att_{uuid.uuid4().hex[:16]}"


@dataclass(slots=True)
class AttachmentMeta:
    id: str
    session_id: str
    filename: str  # sanitized, no path separators
    size_bytes: int
    mime: str
    uploaded_at: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "session_id": self.session_id,
            "filename": self.filename,
            "size_bytes": self.size_bytes,
            "mime": self.mime,
            "uploaded_at": self.uploaded_at,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "AttachmentMeta":
        return cls(
            id=d["id"],
            session_id=d["session_id"],
            filename=d["filename"],
            size_bytes=int(d.get("size_bytes", 0)),
            mime=d.get("mime", "application/octet-stream"),
            uploaded_at=float(d.get("uploaded_at", _now())),
        )


class AttachmentsRegistry:
    """Per-session attachment metadata. registry.json at attachments/<sid>/registry.json."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self._lock = asyncio.Lock()
        self._sessions: dict[str, dict[str, AttachmentMeta]] = {}
        self._loaded: set[str] = set()

    def _registry_path(self, session_id: str) -> Path:
        return self.root / session_id / "registry.json"

    async def _ensure_loaded(self, session_id: str) -> None:
        """Lazy-load registry.json for a session. Must be called under _lock."""
        if session_id in self._loaded:
            return
        items: dict[str, AttachmentMeta] = {}
        path = self._registry_path(session_id)
        if path.exists():
            try:
                raw = json.loads(path.read_text("utf-8"))
                for item in raw.get("attachments", []):
                    meta = AttachmentMeta.from_dict(item)
                    items[meta.id] = meta
            except (json.JSONDecodeError, OSError):
                pass
        self._sessions[session_id] = items
        self._loaded.add(session_id)

    async def _flush_locked(self, session_id: str) -> None:
        """Atomic-rename flush. Must be called under _lock."""
        items = self._sessions.get(session_id, {})
        path = self._registry_path(session_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"attachments": [m.to_dict() for m in items.values()]}
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        tmp.replace(path)

    async def list(self, session_id: str) -> list[AttachmentMeta]:
        async with self._lock:
            await self._ensure_loaded(session_id)
            return sorted(
                self._sessions[session_id].values(),
                key=lambda m: m.uploaded_at,
                reverse=True,
            )

    async def get(self, session_id: str, att_id: str) -> AttachmentMeta | None:
        async with self._lock:
            await self._ensure_loaded(session_id)
            return self._sessions[session_id].get(att_id)

    async def create(
        self, session_id: str, *, filename: str, size_bytes: int, mime: str
    ) -> AttachmentMeta:
        async with self._lock:
            await self._ensure_loaded(session_id)
            meta = AttachmentMeta(
                id=_new_id(),
                session_id=session_id,
                filename=filename,
                size_bytes=size_bytes,
                mime=mime,
                uploaded_at=_now(),
            )
            self._sessions[session_id][meta.id] = meta
            await self._flush_locked(session_id)
            return meta

    async def delete(self, session_id: str, att_id: str) -> bool:
        async with self._lock:
            await self._ensure_loaded(session_id)
            if att_id not in self._sessions[session_id]:
                return False
            del self._sessions[session_id][att_id]
            await self._flush_locked(session_id)
            return True

    async def delete_session(self, session_id: str) -> None:
        """Remove the entire attachments/<sid>/ directory. Best-effort (spec §5.3)."""
        async with self._lock:
            self._sessions.pop(session_id, None)
            self._loaded.discard(session_id)
        sid_dir = self.root / session_id
        if sid_dir.exists():
            await asyncio.to_thread(shutil.rmtree, str(sid_dir), True)

    def path_for(self, meta: AttachmentMeta) -> Path:
        return self.root / meta.session_id / meta.id / meta.filename

    def session_dir(self, session_id: str) -> Path:
        return self.root / session_id
