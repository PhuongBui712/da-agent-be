"""Knowledge Base ingestion subsystem.

The KB pipeline ingests `.xlsx` files into per-KB directories under
`~/.da-agent/kb/<kb_id>/` and produces a compact `manifest.json` so the agent
can reason about schema/data without loading raw rows. Spec: technical-spec
§5.1, §5.2, §7.

Public surface:

- `KbRegistry`     — atomic-rename JSON registry of `KbMeta` rows.
- `KbMeta`         — per-KB metadata (id, filename, status, error, timestamps).
- `KbStatus`       — Literal["PENDING", "PROCESSING", "READY", "FAILED"].
- `Manifest`       — schema for `manifest.json` (sheets / regions / columns / FKs).
- `build_manifest` — pure sync pipeline; runs in an executor.
- `run_pipeline`   — async orchestrator that flips status PENDING → READY/FAILED.
"""

from .manifest import (
    Column,
    Manifest,
    Region,
    Relationship,
    SheetSummary,
    read_manifest,
    write_manifest_atomic,
)
from .preprocess import build_manifest
from .registry import KbMeta, KbRegistry, KbStatus
from .runner import run_pipeline

__all__ = [
    "Column",
    "KbMeta",
    "KbRegistry",
    "KbStatus",
    "Manifest",
    "Region",
    "Relationship",
    "SheetSummary",
    "build_manifest",
    "read_manifest",
    "run_pipeline",
    "write_manifest_atomic",
]
