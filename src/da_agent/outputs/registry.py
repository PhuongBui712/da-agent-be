"""Outputs registry — `outputs/registry.json` mirrors `KbRegistry`.

Standalone outputs only (kind=standalone). KB-bound and attachment-bound
writes are routed through this same registry; the legacy
`kb/<kb_id>/versions/` and `attachments/<sid>/<att_id>/versions/` chains are
no longer written to.

Layout (Phase A 2026-06-01 — flat per-session):

    outputs/
      registry.json                   # this file
      <session_id>/
        <filename>                    # the actual bytes
        .<output_id>.meta.json        # sidecar mirroring the registry row
"""

from __future__ import annotations

import asyncio
import json
import logging
import mimetypes
import re
import secrets
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# `out_<16hex>` — matches the route layer's `_new_output_id`.
_OUTPUT_ID_RE = re.compile(r"^out_[0-9a-f]{16}$")
_SIDECAR_RE = re.compile(r"^\.out_[0-9a-f]{16}\.meta\.json$")


def _now() -> float:
    return time.time()


def _new_id() -> str:
    return f"out_{secrets.token_hex(8)}"


@dataclass(slots=True)
class OutputMeta:
    """One row in `outputs/registry.json`.

    On-disk file lives at `<root>/<session_id>/<filename>`; sidecar lives at
    `<root>/<session_id>/.<output_id>.meta.json`.
    """

    id: str  # output_id "out_<16hex>"
    kind: str  # "standalone" only in this registry
    filename: str  # name on disk under outputs/<session_id>/
    size_bytes: int
    session_id: str  # layout key
    source_id: str | None = None  # optional source kb_id / att_id
    created_at: float = field(default_factory=_now)
    # Derived/back-compat fields (kept so the HTTP routes don't need to change
    # in this phase). `title` defaults to `filename`; `mime` is guessed.
    title: str = ""
    mime: str = "application/octet-stream"
    source_session_id: str | None = None
    source_kb_ids: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "output_id": self.id,
            "kind": self.kind,
            "title": self.title or self.filename,
            "filename": self.filename,
            "mime": self.mime,
            "size_bytes": self.size_bytes,
            "session_id": self.session_id,
            "source_id": self.source_id,
            "source_session_id": self.source_session_id or self.session_id,
            "source_kb_ids": list(self.source_kb_ids),
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "OutputMeta":
        session_id = d.get("session_id") or d.get("source_session_id") or ""
        filename = d.get("filename", "")
        mime = d.get("mime") or mimetypes.guess_type(filename)[0] or "application/octet-stream"
        return cls(
            id=d.get("output_id") or d["id"],
            kind=d.get("kind", "standalone"),
            filename=filename,
            size_bytes=int(d.get("size_bytes", 0)),
            session_id=session_id,
            source_id=d.get("source_id"),
            created_at=float(d.get("created_at", _now())),
            title=d.get("title") or filename,
            mime=mime,
            source_session_id=d.get("source_session_id") or session_id or None,
            source_kb_ids=list(d.get("source_kb_ids") or []),
        )


class OutputsRegistry:
    """Single JSON file at `root/registry.json`.

    Files at `root/<session_id>/<filename>` (Phase A 2026-06-01). Sidecar at
    `root/<session_id>/.<output_id>.meta.json`.
    """

    def __init__(self, root: Path) -> None:
        self.root = root
        self._root = root
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
                    target = self._root / meta.session_id / meta.filename
                    if not target.exists():
                        # Legacy layout `<root>/<sid>/<output_id>/<filename>`
                        # is not migrated — user resets local data.
                        logger.warning(
                            "outputs row %s points at missing file %s; skipping",
                            meta.id,
                            target,
                        )
                        continue
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
        """`<root>/<session_id>/<filename>` (no `<output_id>` middle layer)."""
        return self._root / meta.session_id / meta.filename

    def output_dir(self, meta: OutputMeta) -> Path:
        """`<root>/<session_id>/`. The session-level dir, shared across outputs."""
        return self._root / meta.session_id

    async def list(self, *, session_id: str | None = None) -> list[OutputMeta]:
        async with self._lock:
            await self.load()
            items = list(self._items.values())
        if session_id is not None:
            items = [m for m in items if m.session_id == session_id]
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
        file_path: Path,
        filename: str,
        kind: str,
        source_id: str | None = None,
    ) -> OutputMeta:
        """Adopt a written file under `outputs/<session_id>/<filename>`.

        If `file_path` is already at the canonical target, no copy happens —
        we just stamp the sidecar. Otherwise the source bytes are copied
        (preserving timestamps) into place.
        """
        async with self._lock:
            await self.load()
            outputs_session_dir = self._root / session_id
            outputs_session_dir.mkdir(parents=True, exist_ok=True)
            target_path = outputs_session_dir / filename
            src = Path(file_path)
            if src.resolve() != target_path.resolve():
                shutil.copy2(str(src), str(target_path))
            size = target_path.stat().st_size if target_path.exists() else 0
            output_id = _new_id()
            mime = mimetypes.guess_type(filename)[0] or "application/octet-stream"
            meta = OutputMeta(
                id=output_id,
                kind=kind,
                filename=filename,
                size_bytes=size,
                session_id=session_id,
                source_id=source_id,
                title=filename,
                mime=mime,
                source_session_id=session_id,
            )
            sidecar = outputs_session_dir / f".{output_id}.meta.json"
            sidecar.write_text(
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
        outputs_session_dir = self._root / meta.session_id
        data_path = outputs_session_dir / meta.filename
        sidecar = outputs_session_dir / f".{output_id}.meta.json"
        for p in (data_path, sidecar):
            try:
                if p.exists():
                    await asyncio.to_thread(p.unlink)
            except OSError:
                pass
        return True

    async def delete_session_outputs(self, session_id: str) -> None:
        """Bulk-remove every output owned by `session_id`.

        Wipes `<root>/<session_id>/` and drops all matching registry rows.
        Best-effort — never raises; missing directories are not an error.
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
        session_dir = self._root / session_id
        if session_dir.exists():
            await asyncio.to_thread(shutil.rmtree, str(session_dir), True)
