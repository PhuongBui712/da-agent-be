"""Tests for KbRegistry: CRUD, status transitions, persistence, crash recovery."""

from __future__ import annotations

import json
from pathlib import Path


from da_agent.kb.registry import KbRegistry


async def test_create_then_get_returns_pending(tmp_path: Path):
    reg = KbRegistry(tmp_path / "registry.json")
    meta = await reg.create(filename="data.xlsx", size_bytes=1024)
    fetched = await reg.get(meta.id)
    assert fetched is not None
    assert fetched.status == "PENDING"
    assert fetched.filename == "data.xlsx"


async def test_list_returns_descending_updated_at(tmp_path: Path):
    reg = KbRegistry(tmp_path / "registry.json")
    await reg.create(filename="a.xlsx", size_bytes=100)
    second = await reg.create(filename="b.xlsx", size_bytes=200)
    await reg.update_status(second.id, "PROCESSING")

    items = await reg.list()
    assert len(items) == 2
    # Most recently updated (second) should come first.
    assert items[0].id == second.id


async def test_update_status_pending_to_processing_to_ready(tmp_path: Path):
    reg = KbRegistry(tmp_path / "registry.json")
    meta = await reg.create(filename="x.xlsx", size_bytes=10)

    await reg.update_status(meta.id, "PROCESSING")
    processing = await reg.get(meta.id)
    assert processing is not None
    assert processing.status == "PROCESSING"

    # Passing error= to a non-FAILED transition: the impl clears error.
    await reg.update_status(meta.id, "READY", error="irrelevant")
    ready = await reg.get(meta.id)
    assert ready is not None
    assert ready.status == "READY"
    assert ready.error is None


async def test_update_status_failed_captures_error(tmp_path: Path):
    reg = KbRegistry(tmp_path / "registry.json")
    meta = await reg.create(filename="y.xlsx", size_bytes=10)
    await reg.update_status(meta.id, "FAILED", error="boom")
    fetched = await reg.get(meta.id)
    assert fetched is not None
    assert fetched.status == "FAILED"
    assert fetched.error == "boom"


async def test_delete_removes_row(tmp_path: Path):
    reg = KbRegistry(tmp_path / "registry.json")
    meta = await reg.create(filename="z.xlsx", size_bytes=10)
    result = await reg.delete(meta.id)
    assert result is True
    assert await reg.get(meta.id) is None

    second_delete = await reg.delete(meta.id)
    assert second_delete is False


async def test_load_sweeps_processing_to_failed_on_boot(tmp_path: Path):
    registry_path = tmp_path / "registry.json"
    # Write a JSON file directly simulating a leftover PROCESSING row.
    registry_path.write_text(
        json.dumps(
            {
                "files": [
                    {
                        "id": "kb_x",
                        "filename": "a.xlsx",
                        "size_bytes": 1,
                        "status": "PROCESSING",
                        "created_at": 1,
                        "updated_at": 1,
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    reg = KbRegistry(registry_path)
    await reg.load()

    fetched = await reg.get("kb_x")
    assert fetched is not None
    assert fetched.status == "FAILED"
    assert fetched.error == "interrupted by restart"

    # The file on disk must also reflect the sweep.
    on_disk = json.loads(registry_path.read_text("utf-8"))
    disk_row = on_disk["files"][0]
    assert disk_row["status"] == "FAILED"
    assert disk_row["error"] == "interrupted by restart"


async def test_persistence_round_trip(tmp_path: Path):
    registry_path = tmp_path / "registry.json"
    reg_a = KbRegistry(registry_path)
    meta = await reg_a.create(filename="persist.xlsx", size_bytes=42)

    reg_b = KbRegistry(registry_path)
    await reg_b.load()
    items = await reg_b.list()
    assert len(items) == 1
    assert items[0].id == meta.id
    assert items[0].filename == "persist.xlsx"
