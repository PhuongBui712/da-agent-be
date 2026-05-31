"""Tests for run_pipeline: status transitions and error handling."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest

from da_agent.config import Settings
from da_agent.ingestion.profiler import ProfileResult
from da_agent.ingestion.registry import IngestionRegistry
from da_agent.ingestion.runner import run_pipeline


# --------------------------------------------------------------------------- #
# Stub profiler
# --------------------------------------------------------------------------- #


@dataclass
class _StubProfiler:
    """Injectable profiler stub — returns a scripted ProfileResult."""

    result: ProfileResult
    called: bool = False

    async def run(self, *, kb_id: str, raw_path: Path, filename: str) -> ProfileResult:
        self.called = True
        return self.result


class _RaisingProfiler:
    """Profiler that raises unconditionally."""

    async def run(self, *, kb_id: str, raw_path: Path, filename: str) -> ProfileResult:
        raise RuntimeError("stub raised")


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #


@pytest.fixture
def settings(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("DA_AGENT_HOME", str(tmp_path))
    s = Settings()
    s.data_root = tmp_path
    s.project_root = tmp_path
    return s


@pytest.fixture
def kb_root(tmp_path: Path) -> Path:
    root = tmp_path / "kb"
    root.mkdir(parents=True, exist_ok=True)
    return root


async def _registry_with_entry(
    kb_root: Path, tmp_path: Path, filename: str = "data.xlsx"
):
    """Create a registry, seed one PENDING entry, and return (registry, kb_id)."""
    reg = IngestionRegistry(tmp_path / "registry.json")
    meta = await reg.create(filename=filename, size_bytes=100)
    return reg, meta.id


def _place_raw_xlsx(kb_root: Path, kb_id: str) -> Path:
    raw = kb_root / kb_id / "raw.xlsx"
    raw.parent.mkdir(parents=True, exist_ok=True)
    raw.write_bytes(b"fake xlsx content")
    return raw


# --------------------------------------------------------------------------- #
# Happy path
# --------------------------------------------------------------------------- #


async def test_happy_path_transitions_to_ready(
    settings: Settings, kb_root: Path, tmp_path: Path
):
    reg, kb_id = await _registry_with_entry(kb_root, tmp_path)
    _place_raw_xlsx(kb_root, kb_id)

    mem_path = settings.kb_profiler_memory_dir / f"{kb_id}.md"
    stub = _StubProfiler(
        result=ProfileResult(ok=True, memory_path=mem_path, error=None, duration_ms=10)
    )

    await run_pipeline(
        registry=reg,
        settings=settings,
        kb_root=kb_root,
        kb_id=kb_id,
        profiler=stub,
    )

    assert stub.called
    final = await reg.get(kb_id)
    assert final is not None
    assert final.status == "READY"
    assert final.memory_path == str(mem_path)
    assert final.error is None


# --------------------------------------------------------------------------- #
# Profiler returns ok=False → READY_PARTIAL
# --------------------------------------------------------------------------- #


async def test_profiler_fail_transitions_to_ready_partial(
    settings: Settings, kb_root: Path, tmp_path: Path
):
    reg, kb_id = await _registry_with_entry(kb_root, tmp_path)
    _place_raw_xlsx(kb_root, kb_id)

    stub = _StubProfiler(
        result=ProfileResult(
            ok=False, memory_path=None, error="model blew up", duration_ms=5
        )
    )

    await run_pipeline(
        registry=reg,
        settings=settings,
        kb_root=kb_root,
        kb_id=kb_id,
        profiler=stub,
    )

    assert stub.called
    final = await reg.get(kb_id)
    assert final is not None
    assert final.status == "READY_PARTIAL"
    assert final.error == "model blew up"
    assert final.memory_path is None


# --------------------------------------------------------------------------- #
# raw.xlsx missing → FAILED, profiler never invoked
# --------------------------------------------------------------------------- #


async def test_missing_raw_xlsx_transitions_to_failed(
    settings: Settings, kb_root: Path, tmp_path: Path
):
    reg, kb_id = await _registry_with_entry(kb_root, tmp_path)
    # Deliberately do NOT place raw.xlsx.

    stub = _StubProfiler(
        result=ProfileResult(ok=True, memory_path=None, error=None, duration_ms=0)
    )

    await run_pipeline(
        registry=reg,
        settings=settings,
        kb_root=kb_root,
        kb_id=kb_id,
        profiler=stub,
    )

    assert not stub.called, "profiler must not be invoked when raw.xlsx is missing"
    final = await reg.get(kb_id)
    assert final is not None
    assert final.status == "FAILED"
    assert final.error == "raw.xlsx missing on disk"


# --------------------------------------------------------------------------- #
# Profiler raises → READY_PARTIAL
# --------------------------------------------------------------------------- #


async def test_profiler_raises_transitions_to_ready_partial(
    settings: Settings, kb_root: Path, tmp_path: Path
):
    reg, kb_id = await _registry_with_entry(kb_root, tmp_path)
    _place_raw_xlsx(kb_root, kb_id)

    await run_pipeline(
        registry=reg,
        settings=settings,
        kb_root=kb_root,
        kb_id=kb_id,
        profiler=_RaisingProfiler(),
    )

    final = await reg.get(kb_id)
    assert final is not None
    assert final.status == "READY_PARTIAL"
    assert final.error is not None
    assert "runner crashed" in final.error


# --------------------------------------------------------------------------- #
# meta deleted mid-flight (registry returns None for get)
# --------------------------------------------------------------------------- #


async def test_meta_deleted_mid_flight_no_exception(
    settings: Settings, kb_root: Path, tmp_path: Path
):
    reg, kb_id = await _registry_with_entry(kb_root, tmp_path)
    _place_raw_xlsx(kb_root, kb_id)

    # Delete the entry to simulate a concurrent delete between raw-check and
    # registry.get(kb_id).
    await reg.delete(kb_id)

    # Should return cleanly without raising and without attempting a status write.
    await run_pipeline(
        registry=reg,
        settings=settings,
        kb_root=kb_root,
        kb_id=kb_id,
        profiler=_StubProfiler(
            result=ProfileResult(ok=True, memory_path=None, error=None, duration_ms=0)
        ),
    )
    # Nothing to assert on registry state — key is that no exception propagated.
