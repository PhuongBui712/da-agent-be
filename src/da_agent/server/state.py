"""In-process state for the FastAPI backend.

`SessionRegistry`     persistent metadata (`registry.json`) for session CRUD.
`InteractionStore`    in-memory pending plan / question requests keyed by tool_use_id.
`TurnStream`          per-message event channel; closed with a sentinel when the turn ends.
`SessionRuntime`      live `AgentRunner` + `WebAgentUI` per session, lazy-initialised.
`AppState`            aggregate held on `app.state.app_state`.
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..config import Settings


def _now() -> float:
    return time.time()


def _new_id() -> str:
    return f"sess_{uuid.uuid4().hex[:16]}"


# --------------------------------------------------------------------------- #
# Session registry
# --------------------------------------------------------------------------- #
@dataclass(slots=True)
class SessionMeta:
    id: str
    name: str
    created_at: float = field(default_factory=_now)
    updated_at: float = field(default_factory=_now)
    parent_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "parent_id": self.parent_id,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "SessionMeta":
        return cls(
            id=d["id"],
            name=d.get("name", "untitled"),
            created_at=float(d.get("created_at", _now())),
            updated_at=float(d.get("updated_at", _now())),
            parent_id=d.get("parent_id"),
        )


class SessionRegistry:
    """Single JSON file on disk. Single-user, low-concurrency — atomic-rename writes."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = asyncio.Lock()
        self._items: dict[str, SessionMeta] = {}
        self._loaded = False

    async def load(self) -> None:
        if self._loaded:
            return
        if self.path.exists():
            try:
                raw = json.loads(self.path.read_text("utf-8"))
                for item in raw.get("sessions", []):
                    meta = SessionMeta.from_dict(item)
                    self._items[meta.id] = meta
            except (json.JSONDecodeError, OSError):
                # Corrupt / unreadable registry — start clean rather than refusing to boot.
                self._items.clear()
        self._loaded = True

    async def _flush_locked(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"sessions": [m.to_dict() for m in self._items.values()]}
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        tmp.replace(self.path)

    async def list(self) -> list[SessionMeta]:
        async with self._lock:
            await self.load()
            return sorted(
                self._items.values(), key=lambda m: m.updated_at, reverse=True
            )

    async def get(self, sid: str) -> SessionMeta | None:
        async with self._lock:
            await self.load()
            return self._items.get(sid)

    async def create(
        self, name: str = "untitled", parent_id: str | None = None
    ) -> SessionMeta:
        async with self._lock:
            await self.load()
            meta = SessionMeta(
                id=_new_id(), name=name or "untitled", parent_id=parent_id
            )
            self._items[meta.id] = meta
            await self._flush_locked()
            return meta

    async def rename(self, sid: str, name: str) -> SessionMeta | None:
        async with self._lock:
            await self.load()
            meta = self._items.get(sid)
            if meta is None:
                return None
            meta.name = name or meta.name
            meta.updated_at = _now()
            await self._flush_locked()
            return meta

    async def touch(self, sid: str) -> None:
        async with self._lock:
            await self.load()
            meta = self._items.get(sid)
            if meta is None:
                return
            meta.updated_at = _now()
            await self._flush_locked()

    async def delete(self, sid: str) -> bool:
        async with self._lock:
            await self.load()
            if sid not in self._items:
                return False
            del self._items[sid]
            await self._flush_locked()
            return True


# --------------------------------------------------------------------------- #
# Interaction store
# --------------------------------------------------------------------------- #
@dataclass(slots=True)
class PendingInteraction:
    tool_use_id: str
    kind: str  # "question" | "plan"
    payload: dict[str, Any]
    future: asyncio.Future


class InteractionStore:
    """`{session_id: {tool_use_id: PendingInteraction}}`. In-memory; not persisted."""

    def __init__(self) -> None:
        self._items: dict[str, dict[str, PendingInteraction]] = {}
        self._lock = asyncio.Lock()

    async def park(self, session_id: str, pending: PendingInteraction) -> None:
        async with self._lock:
            self._items.setdefault(session_id, {})[pending.tool_use_id] = pending

    async def resolve(self, session_id: str, tool_use_id: str, value: Any) -> bool:
        async with self._lock:
            sess = self._items.get(session_id)
            if not sess or tool_use_id not in sess:
                return False
            pending = sess.pop(tool_use_id)
            if not sess:
                self._items.pop(session_id, None)
        if not pending.future.done():
            pending.future.set_result(value)
        return True

    async def pending(self, session_id: str) -> list[PendingInteraction]:
        async with self._lock:
            return list(self._items.get(session_id, {}).values())

    async def clear_session(self, session_id: str) -> None:
        async with self._lock:
            sess = self._items.pop(session_id, {})
        for pending in sess.values():
            if not pending.future.done():
                pending.future.cancel()


# --------------------------------------------------------------------------- #
# Per-turn event stream
# --------------------------------------------------------------------------- #
class TurnStream:
    """A single-turn async iterable of event dicts, terminated by a sentinel.

    The runner task pushes events via `emit`; the SSE response generator iterates
    until `close()` is called (after `runner.send` returns or errors).
    """

    SENTINEL: object = object()

    def __init__(self) -> None:
        self._q: asyncio.Queue[Any] = asyncio.Queue()
        self._closed = False

    def emit(self, item: dict[str, Any]) -> None:
        if self._closed:
            return
        self._q.put_nowait(item)

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        await self._q.put(self.SENTINEL)

    def __aiter__(self) -> "TurnStream":
        return self

    async def __anext__(self) -> dict[str, Any]:
        item = await self._q.get()
        if item is self.SENTINEL:
            raise StopAsyncIteration
        return item


# --------------------------------------------------------------------------- #
# Per-session runtime (forward import of AgentRunner / WebAgentUI to break cycles)
# --------------------------------------------------------------------------- #
@dataclass
class SessionRuntime:
    meta: SessionMeta
    settings: Settings
    runner: Any = None  # AgentRunner | None
    ui: Any = None  # WebAgentUI | None
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


# --------------------------------------------------------------------------- #
# Aggregate app state
# --------------------------------------------------------------------------- #
class AppState:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.registry = SessionRegistry(settings.data_root / "registry.json")
        self.interactions = InteractionStore()
        self._runtimes: dict[str, SessionRuntime] = {}
        self._runtimes_lock = asyncio.Lock()

    async def get_or_create_runtime(self, sid: str) -> SessionRuntime | None:
        async with self._runtimes_lock:
            existing = self._runtimes.get(sid)
            if existing is not None:
                return existing
        meta = await self.registry.get(sid)
        if meta is None:
            return None
        async with self._runtimes_lock:
            existing = self._runtimes.get(sid)
            if existing is not None:
                return existing
            runtime = SessionRuntime(meta=meta, settings=self.settings)
            self._runtimes[sid] = runtime
            return runtime

    def runtime_ids(self) -> list[str]:
        return list(self._runtimes.keys())

    async def discard_runtime(self, sid: str) -> None:
        async with self._runtimes_lock:
            runtime = self._runtimes.pop(sid, None)
        await self.interactions.clear_session(sid)
        if runtime and runtime.runner is not None:
            try:
                await runtime.runner.__aexit__(None, None, None)
            except Exception:
                pass

    async def shutdown(self) -> None:
        for sid in list(self._runtimes.keys()):
            await self.discard_runtime(sid)
