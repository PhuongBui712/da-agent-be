"""Async orchestrator that runs the blocking pipeline in an executor and
flips registry status. Imported by the route handler -- not by `manifest.py`
or `preprocess.py`, so those stay framework-free.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from .manifest import write_manifest_atomic
from .preprocess import build_manifest
from .registry import KbRegistry

log = logging.getLogger(__name__)


async def run_pipeline(*, registry: KbRegistry, kb_root: Path, kb_id: str) -> None:
    """Drive the pipeline for a single KB.

    Transitions: PENDING -> PROCESSING -> READY (or FAILED with error).
    Always run as a fire-and-forget `asyncio.create_task` so the POST handler
    returns immediately. Exceptions are captured into the registry; never
    re-raised (the task lifecycle is owned by the caller's task tracker).
    """
    await registry.update_status(kb_id, "PROCESSING")
    raw_path = kb_root / kb_id / "raw.xlsx"
    manifest_path = kb_root / kb_id / "manifest.json"
    try:
        manifest = await asyncio.to_thread(build_manifest, raw_path, kb_id)
        await asyncio.to_thread(write_manifest_atomic, manifest_path, manifest)
    except Exception as exc:  # noqa: BLE001 - surface as FAILED
        log.exception("KB pipeline failed for %s", kb_id)
        await registry.update_status(
            kb_id, "FAILED", error=f"{type(exc).__name__}: {exc}"
        )
        return
    await registry.update_status(kb_id, "READY")
