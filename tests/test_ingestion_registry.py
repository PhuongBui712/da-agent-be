"""Tests for IngestionRegistry: CRUD, status transitions, crash recovery, legacy compat."""

from __future__ import annotations

import json
from pathlib import Path

from da_agent.ingestion.registry import IngestionRegistry


async def test_create_then_get_returns_pending(tmp_path: Path):
    reg = IngestionRegistry(tmp_path / "registry.json")
    meta = await reg.create(filename="data.xlsx", size_bytes=1024)
    fetched = await reg.get(meta.id)
    assert fetched is not None
    assert fetched.status == "PENDING"
    assert fetched.filename == "data.xlsx"
    assert fetched.size_bytes == 1024


async def test_list_returns_descending_updated_at(tmp_path: Path):
    reg = IngestionRegistry(tmp_path / "registry.json")
    first = await reg.create(filename="a.xlsx", size_bytes=100)
    second = await reg.create(filename="b.xlsx", size_bytes=200)
    # Touch second so its updated_at is strictly larger.
    await reg.update_status(second.id, "PROFILING")

    items = await reg.list()
    assert len(items) == 2
    assert items[0].id == second.id
    assert items[1].id == first.id


async def test_transition_pending_profiling_ready(tmp_path: Path):
    reg = IngestionRegistry(tmp_path / "registry.json")
    meta = await reg.create(filename="x.xlsx", size_bytes=10)

    await reg.update_status(meta.id, "PROFILING")
    profiling = await reg.get(meta.id)
    assert profiling is not None
    assert profiling.status == "PROFILING"
    assert profiling.error is None

    mem = "/some/path/kb_abc.md"
    await reg.update_status(meta.id, "READY", memory_path=mem)
    ready = await reg.get(meta.id)
    assert ready is not None
    assert ready.status == "READY"
    assert ready.error is None
    assert ready.memory_path == mem


async def test_ready_partial_captures_error_keeps_memory_path_none(tmp_path: Path):
    reg = IngestionRegistry(tmp_path / "registry.json")
    meta = await reg.create(filename="y.xlsx", size_bytes=10)
    await reg.update_status(meta.id, "READY_PARTIAL", error="profiler blew up")
    fetched = await reg.get(meta.id)
    assert fetched is not None
    assert fetched.status == "READY_PARTIAL"
    assert fetched.error == "profiler blew up"
    assert fetched.memory_path is None


async def test_failed_captures_error(tmp_path: Path):
    reg = IngestionRegistry(tmp_path / "registry.json")
    meta = await reg.create(filename="z.xlsx", size_bytes=10)
    await reg.update_status(meta.id, "FAILED", error="disk error")
    fetched = await reg.get(meta.id)
    assert fetched is not None
    assert fetched.status == "FAILED"
    assert fetched.error == "disk error"


async def test_delete_removes_row(tmp_path: Path):
    reg = IngestionRegistry(tmp_path / "registry.json")
    meta = await reg.create(filename="del.xlsx", size_bytes=10)
    result = await reg.delete(meta.id)
    assert result is True
    assert await reg.get(meta.id) is None

    second_delete = await reg.delete(meta.id)
    assert second_delete is False


async def test_crash_sweep_profiling_becomes_failed(tmp_path: Path):
    registry_path = tmp_path / "registry.json"
    registry_path.write_text(
        json.dumps(
            {
                "files": [
                    {
                        "id": "kb_crash1",
                        "filename": "crash.xlsx",
                        "size_bytes": 1,
                        "status": "PROFILING",
                        "created_at": 1.0,
                        "updated_at": 1.0,
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    reg = IngestionRegistry(registry_path)
    await reg.load()

    fetched = await reg.get("kb_crash1")
    assert fetched is not None
    assert fetched.status == "FAILED"
    assert fetched.error == "interrupted by restart"

    on_disk = json.loads(registry_path.read_text("utf-8"))
    disk_row = on_disk["files"][0]
    assert disk_row["status"] == "FAILED"
    assert disk_row["error"] == "interrupted by restart"


async def test_legacy_processing_status_migrated_to_failed(tmp_path: Path):
    registry_path = tmp_path / "registry.json"
    registry_path.write_text(
        json.dumps(
            {
                "files": [
                    {
                        "id": "kb_legacy1",
                        "filename": "old.xlsx",
                        "size_bytes": 5,
                        "status": "PROCESSING",
                        "created_at": 1.0,
                        "updated_at": 1.0,
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    reg = IngestionRegistry(registry_path)
    await reg.load()

    fetched = await reg.get("kb_legacy1")
    assert fetched is not None
    assert fetched.status == "FAILED"
    assert fetched.error is not None
    assert "legacy manifest row" in fetched.error
    assert "reprofile" in fetched.error


async def test_clear_memory_path_nulls_it_preserves_status(tmp_path: Path):
    reg = IngestionRegistry(tmp_path / "registry.json")
    meta = await reg.create(filename="mem.xlsx", size_bytes=10)
    await reg.update_status(meta.id, "READY", memory_path="/some/kb_mem.md")

    ready = await reg.get(meta.id)
    assert ready is not None
    assert ready.memory_path == "/some/kb_mem.md"

    cleared = await reg.clear_memory_path(meta.id)
    assert cleared is not None
    assert cleared.memory_path is None
    assert cleared.status == "READY"


async def test_update_status_none_memory_path_does_not_clobber_existing(tmp_path: Path):
    reg = IngestionRegistry(tmp_path / "registry.json")
    meta = await reg.create(filename="keep.xlsx", size_bytes=10)
    await reg.update_status(meta.id, "READY", memory_path="/original/path.md")

    # Transition again without passing memory_path — must not wipe the existing one.
    await reg.update_status(meta.id, "READY")
    fetched = await reg.get(meta.id)
    assert fetched is not None
    assert fetched.memory_path == "/original/path.md"
