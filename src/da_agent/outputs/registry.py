"""Outputs registry — `outputs/registry.json` mirrors `KbRegistry`.

Standalone outputs only (kind=standalone). KB-bound outputs are recorded as
version sidecars under `kb/<kb_id>/versions/` and surfaced via the KB version
endpoints (Wave 1 / spec §7); we never mint an `output_id` for those.

Layout (spec §8.2):

    outputs/
      registry.json                 # this file
      <output_id>/
        <filename>                  # the actual bytes
        meta.json                   # sidecar mirroring the registry row
"""

from __future__ import annotations

import asyncio
import json
import shutil
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


def _now() -> float:
    return time.time()


def _new_id() -> str:
    return f"out_{uuid.uuid4().hex[:16]}"


@dataclass(slots=True)
class OutputMeta:
    """Spec §8.2 — `outputs/<id>/meta.json` shape."""

    id: str  # output_id "out_<16hex>"
    kind: str  # "standalone" only in this registry
    title: str
    filename: str  # name on disk under outputs/<id>/
    mime: str
    size_bytes: int
    source_session_id: str | None = None
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
            "source_session_id": self.source_session_id,
            "source_kb_ids": list(self.source_kb_ids),
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "OutputMeta":
        return cls(
            id=d.get("output_id") or d["id"],
            kind=d.get("kind", "standalone"),
            title=d.get("title", ""),
            filename=d.get("filename", ""),
            mime=d.get("mime", "application/octet-stream"),
            size_bytes=int(d.get("size_bytes", 0)),
            source_session_id=d.get("source_session_id"),
            source_kb_ids=list(d.get("source_kb_ids") or []),
            created_at=float(d.get("created_at", _now())),
        )


class OutputsRegistry:
    """Single JSON file at `root/registry.json`. Files at `root/<id>/<filename>`."""

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
                    self._items[meta.id] = meta
            except (json.JSONDecodeError, OSError):
                # Corrupt registry — start clean rather than refusing to boot.
                self._items.clear()
        self._loaded = True

    async def _flush_locked(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"outputs": [m.to_dict() for m in self._items.values()]}
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        tmp.replace(self.path)

    def path_for(self, meta: OutputMeta) -> Path:
        return self.root / meta.id / meta.filename

    def output_dir(self, output_id: str) -> Path:
        return self.root / output_id

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
        src_path: Path,
        title: str,
        filename: str,
        mime: str,
        source_session_id: str | None,
        source_kb_ids: list[str] | None = None,
    ) -> OutputMeta:
        """Register an already-written file: move it under `outputs/<id>/<filename>`.

        If `src_path` already lives inside the canonical layout, no move happens
        and we just stamp the sidecar `meta.json`.
        """
        async with self._lock:
            await self.load()
            output_id = _new_id()
            target_dir = self.root / output_id
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
                source_session_id=source_session_id,
                source_kb_ids=list(source_kb_ids or []),
            )
            # Sidecar meta.json next to the file (spec §8.2 schema).
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
        title: str,
        filename: str,
        mime: str,
        source_session_id: str | None,
        source_kb_ids: list[str] | None = None,
    ) -> OutputMeta | None:
        """Adopt an existing `outputs/<output_id>/<filename>` path.

        Used when the model wrote directly into `outputs_dir` using an id we
        did not mint. Returns None if the directory or file is missing; if the
        id is already registered, returns the existing row unchanged.
        """
        target_dir = self.root / output_id
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
                source_session_id=source_session_id,
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
        target_dir = self.root / output_id
        if target_dir.exists():
            await asyncio.to_thread(shutil.rmtree, str(target_dir), True)
        return True
