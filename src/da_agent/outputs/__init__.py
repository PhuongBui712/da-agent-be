"""Standalone outputs registry + write-tool observer.

KB-bound outputs are persisted as version sidecars under
`kb/<kb_id>/versions/v<N>.meta.json` — no `output_id` is minted for them.
The observer detects both, but only standalone writes flow through
`OutputsRegistry`.
"""

from .observer import OutputDetection, OutputsObserver
from .registry import OutputMeta, OutputsRegistry

__all__ = ["OutputMeta", "OutputsRegistry", "OutputsObserver", "OutputDetection"]
