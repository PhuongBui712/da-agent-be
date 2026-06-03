"""Manifest schema + atomic IO.

`manifest.json` is the agent's primary view of a KB file. All fields are
JSON-serializable; dataclasses are `asdict`-friendly so reading/writing is
mechanical.

The `from_` / `to` rename in `Relationship` mirrors the `"from"` JSON key
(which collides with the Python keyword), see `to_dict` below.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any


@dataclass(slots=True)
class Column:
    name: str
    dtype: str  # "int" | "float" | "str" | "date" | "datetime" | "bool" | "mixed"
    cardinality: int
    null_pct: float
    role: str | None = None  # "pk?" | "fk?->Sheet.col" | None
    min: float | int | str | None = None
    max: float | int | str | None = None
    sample_values: list[Any] = field(default_factory=list)
    cardinality_capped: bool = False


@dataclass(slots=True)
class Region:
    region_id: str  # "<sheet>!<topleft>", e.g. "Sales!A1"
    range: str  # e.g. "A1:L48211"
    header_row: int  # 1-based row index in the SHEET, not the region
    columns: list[Column] = field(default_factory=list)
    sample_rows: list[list[Any]] = field(default_factory=list)
    low_confidence: bool = False


@dataclass(slots=True)
class SheetSummary:
    name: str
    dims: dict[str, int]  # {"rows": int, "cols": int}
    regions: list[Region] = field(default_factory=list)


@dataclass(slots=True)
class Relationship:
    from_: str  # "Sales.customer_id" -- serialized as JSON key "from"
    to: str  # "Customers.id"
    confidence: float


@dataclass(slots=True)
class Manifest:
    kb_id: str
    source_filename: str
    generated_at: float
    sheets: list[SheetSummary] = field(default_factory=list)
    relationships: list[Relationship] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        # Rename `from_` -> `from` to match the spec wire format.
        for rel in d.get("relationships", []):
            rel["from"] = rel.pop("from_")
        return d


def write_manifest_atomic(path: Path, manifest: Manifest) -> None:
    """Atomic write via tmp + os.replace. Crash-safe: no partial file at `path`."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(
        json.dumps(manifest.to_dict(), indent=2, default=str, ensure_ascii=False),
        encoding="utf-8",
    )
    os.replace(tmp, path)


def read_manifest(path: Path) -> dict[str, Any]:
    """Read `manifest.json` as a plain dict for the manifest API endpoint."""
    return json.loads(path.read_text(encoding="utf-8"))
