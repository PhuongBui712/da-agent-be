"""Async orchestrator for the memory-driven KB ingestion pipeline.

Status flow:

    PENDING -> PROFILING -> READY              (memory file written)
                       \\-> READY_PARTIAL       (profiler failed; raw.xlsx still usable)
                       \\-> FAILED              (catastrophic; e.g. raw.xlsx missing)

A failed profiler does NOT block KB usability — the main agent can still
fall back to the xlsx skill against `raw.xlsx`. We only emit FAILED when the
upload itself is unrecoverable (file missing, registry crash, etc.).
"""

from __future__ import annotations

import logging
from pathlib import Path

from ..config import Settings
from .profiler import KbProfiler
from .registry import IngestionRegistry

_LOG = logging.getLogger(__name__)


async def run_pipeline(
    *,
    registry: IngestionRegistry,
    settings: Settings,
    kb_root: Path,
    kb_id: str,
    profiler: KbProfiler | None = None,
) -> None:
    """Drive ingestion for a single KB. Fire-and-forget from the route handler.

    `profiler` is injectable so tests can swap in a stub; production callers
    pass `None` and we construct one from `settings`.
    """
    raw_path = kb_root / kb_id / "raw.xlsx"
    if not raw_path.exists():
        await registry.update_status(kb_id, "FAILED", error="raw.xlsx missing on disk")
        return

    meta = await registry.get(kb_id)
    if meta is None:
        # Race with delete; nothing to do.
        return
    filename = meta.filename

    await registry.update_status(kb_id, "PROFILING")

    runner = profiler or KbProfiler(settings)
    try:
        result = await runner.run(kb_id=kb_id, raw_path=raw_path, filename=filename)
    except Exception as exc:  # noqa: BLE001 — final safety net
        _LOG.exception("ingestion runner crashed for %s", kb_id)
        await registry.update_status(
            kb_id,
            "READY_PARTIAL",
            error=f"runner crashed: {type(exc).__name__}: {exc}",
        )
        return

    if result.ok and result.memory_path is not None:
        await registry.update_status(
            kb_id, "READY", memory_path=str(result.memory_path)
        )
        return

    # Profiler completed but did not produce a usable note — degrade gracefully.
    await registry.update_status(
        kb_id,
        "READY_PARTIAL",
        error=result.error or "profiler did not produce a memory file",
    )
