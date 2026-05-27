"""Tests for manifest IO: dataclass serialization, atomic write, and read-back."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from da_agent.kb.manifest import (
    Column,
    Manifest,
    Region,
    Relationship,
    SheetSummary,
    read_manifest,
    write_manifest_atomic,
)


def _minimal_manifest(kb_id: str = "kb_test") -> Manifest:
    col = Column(
        name="id",
        dtype="int",
        cardinality=3,
        null_pct=0.0,
        sample_values=[1, 2, 3],
    )
    region = Region(
        region_id="Sheet1!A1",
        range="A1:A4",
        header_row=1,
        columns=[col],
        sample_rows=[[1], [2], [3]],
    )
    sheet = SheetSummary(name="Sheet1", dims={"rows": 4, "cols": 1}, regions=[region])
    return Manifest(
        kb_id=kb_id,
        source_filename="test.xlsx",
        generated_at=1_700_000_000.0,
        sheets=[sheet],
        relationships=[],
    )


def test_manifest_to_dict_renames_from_key():
    rel = Relationship(from_="Sales.cid", to="Customers.id", confidence=0.95)
    manifest = Manifest(
        kb_id="kb_x",
        source_filename="s.xlsx",
        generated_at=1.0,
        sheets=[],
        relationships=[rel],
    )
    d = manifest.to_dict()
    rel_dict = d["relationships"][0]
    assert "from" in rel_dict, "key 'from' must appear in serialized relationship"
    assert "from_" not in rel_dict, (
        "key 'from_' must NOT appear in serialized relationship"
    )
    assert rel_dict["from"] == "Sales.cid"


def test_write_manifest_atomic_round_trip(tmp_path: Path):
    path = tmp_path / "manifest.json"
    m = _minimal_manifest()
    write_manifest_atomic(path, m)
    result = read_manifest(path)

    assert result["kb_id"] == "kb_test"
    assert len(result["sheets"]) == 1
    assert len(result["sheets"][0]["regions"]) == 1
    assert result["sheets"][0]["regions"][0]["columns"][0]["name"] == "id"


def test_write_manifest_atomic_no_partial_file(tmp_path: Path):
    path = tmp_path / "manifest.json"
    m = _minimal_manifest()
    write_manifest_atomic(path, m)
    tmp_file = path.with_suffix(path.suffix + ".tmp")
    assert not tmp_file.exists(), "no .tmp file should remain after successful write"


def test_write_manifest_serializes_datetime(tmp_path: Path):
    dt_value = datetime(2024, 6, 15, 12, 0, 0)
    col = Column(
        name="ts",
        dtype="datetime",
        cardinality=1,
        null_pct=0.0,
        sample_values=[dt_value],
    )
    region = Region(
        region_id="Sheet1!A1",
        range="A1:A2",
        header_row=1,
        columns=[col],
    )
    sheet = SheetSummary(name="Sheet1", dims={"rows": 2, "cols": 1}, regions=[region])
    m = Manifest(
        kb_id="kb_dt",
        source_filename="dt.xlsx",
        generated_at=1.0,
        sheets=[sheet],
        relationships=[],
    )
    path = tmp_path / "manifest.json"
    write_manifest_atomic(path, m)

    result = read_manifest(path)
    sv = result["sheets"][0]["regions"][0]["columns"][0]["sample_values"]
    assert len(sv) == 1
    assert isinstance(sv[0], str), (
        "datetime sample value must be serialized to a string"
    )
