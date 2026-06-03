"""Memory-driven KB ingestion pipeline.

Public surface:

- `IngestionMeta` / `IngestionStatus`  — registry row dataclass + status enum.
- `IngestionRegistry`                  — atomic-rename JSON registry that reuses
                                         the on-disk file (`kb/registry.json`).
- `KbProfiler` / `ProfileResult`       — opus-driven subagent invocation.
- `build_kb_profiler_definition`       — factory for the AgentDefinition;
                                         exposed so tests can assert shape.
- `run_pipeline`                       — async orchestrator, fire-and-forget
                                         from the upload handler.
"""

from .profiler import KbProfiler, ProfileResult, build_kb_profiler_definition
from .registry import IngestionMeta, IngestionRegistry, IngestionStatus
from .runner import run_pipeline

__all__ = [
    "IngestionMeta",
    "IngestionRegistry",
    "IngestionStatus",
    "KbProfiler",
    "ProfileResult",
    "build_kb_profiler_definition",
    "run_pipeline",
]
