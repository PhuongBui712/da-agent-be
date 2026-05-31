"""Tests for KbProfiler and build_kb_profiler_definition."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
from claude_agent_sdk import ResultMessage

from da_agent.config import Settings
from da_agent.ingestion.profiler import KbProfiler, build_kb_profiler_definition


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _ok_result_message() -> ResultMessage:
    return ResultMessage(
        subtype="success",
        duration_ms=1,
        duration_api_ms=1,
        is_error=False,
        num_turns=1,
        session_id="fake",
        total_cost_usd=0.0,
    )


def _error_result_message() -> ResultMessage:
    return ResultMessage(
        subtype="error",
        duration_ms=1,
        duration_api_ms=1,
        is_error=True,
        num_turns=1,
        session_id="fake",
        total_cost_usd=0.0,
    )


class FakeSDKClient:
    """Minimal fake for ClaudeSDKClient context-manager + query/receive_response protocol."""

    def __init__(self, *, script: list[Any], sleep: float = 0.0):
        self._script = script
        self._sleep = sleep
        self.queries: list[str] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        pass

    async def query(self, prompt: str) -> None:
        self.queries.append(prompt)

    async def receive_response(self):
        if self._sleep:
            await asyncio.sleep(self._sleep)
        for item in self._script:
            yield item


def _make_fake_client_class(script: list[Any], sleep: float = 0.0):
    """Return a class (not instance) whose constructor ignores options and yields script."""

    class _Cls(FakeSDKClient):
        def __init__(self, options=None):
            super().__init__(script=script, sleep=sleep)

    return _Cls


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


# --------------------------------------------------------------------------- #
# AgentDefinition shape tests
# --------------------------------------------------------------------------- #


def test_agent_definition_model_from_settings(settings: Settings):
    defn = build_kb_profiler_definition(settings)
    assert defn.model == settings.kb_profiler_model


def test_agent_definition_memory_is_unset(settings: Settings):
    # We deliberately do NOT use the SDK's `memory="local"` scope: it would
    # pin the on-disk path to the dev checkout. The BE passes the absolute
    # memory directory through the invocation prompt instead.
    defn = build_kb_profiler_definition(settings)
    assert defn.memory is None


def test_agent_definition_skills_include_xlsx(settings: Settings):
    defn = build_kb_profiler_definition(settings)
    assert "xlsx" in defn.skills


def test_agent_definition_no_max_turns(settings: Settings):
    defn = build_kb_profiler_definition(settings)
    # maxTurns must not be set (None or absent attribute); SDK default applies.
    max_turns = getattr(defn, "max_turns", None) or getattr(defn, "maxTurns", None)
    assert max_turns is None


def test_agent_definition_tools_include_read_write_edit(settings: Settings):
    # Without `memory="local"`, the SDK no longer auto-adds Read/Write/Edit;
    # we must list them explicitly so the subagent can author the memory
    # note files.
    defn = build_kb_profiler_definition(settings)
    tools = defn.tools or []
    for required in ("Read", "Write", "Edit", "Bash"):
        assert required in tools, f"{required} missing from explicit tools list"


def test_env_override_changes_definition_model(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("DA_AGENT_KB_PROFILER_MODEL", "claude-opus-custom-42")
    monkeypatch.setenv("DA_AGENT_HOME", str(tmp_path))
    s = Settings()
    defn = build_kb_profiler_definition(s)
    assert defn.model == "claude-opus-custom-42"


def test_build_kb_profiler_definition_is_stable(settings: Settings):
    defn1 = build_kb_profiler_definition(settings)
    defn2 = build_kb_profiler_definition(settings)
    assert defn1.model == defn2.model
    assert defn1.memory == defn2.memory
    assert defn1.skills == defn2.skills


# --------------------------------------------------------------------------- #
# Memory directory location (Bug #1: must live under data_root, not the dev
# checkout's .claude/agent-memory-local/).
# --------------------------------------------------------------------------- #


def test_memory_dir_lives_under_data_root(settings: Settings):
    mem_dir = settings.kb_profiler_memory_dir
    assert mem_dir.is_relative_to(settings.data_root), (
        f"memory dir {mem_dir} must be under data_root {settings.data_root}"
    )
    # Specifically: <data_root>/agent-memory/kb_profiler/
    assert mem_dir.name == "kb_profiler"
    assert mem_dir.parent.name == "agent-memory"
    # Must NOT be under any `.claude/agent-memory-local` directory — that
    # would mean we slipped back into the SDK's `memory="local"` scope.
    assert ".claude" not in mem_dir.parts


def test_invocation_prompt_carries_absolute_memory_dir(settings: Settings):
    from da_agent.ingestion.prompts import build_invocation_prompt

    mem_dir = str(settings.kb_profiler_memory_dir)
    prompt = build_invocation_prompt(
        kb_id="kb_xyz",
        raw_path="/tmp/raw.xlsx",
        filename="raw.xlsx",
        memory_dir=mem_dir,
    )
    assert mem_dir in prompt
    assert f"{mem_dir}/kb_xyz.md" in prompt
    assert f"{mem_dir}/MEMORY.md" in prompt


# --------------------------------------------------------------------------- #
# KbProfiler.run — happy path
# --------------------------------------------------------------------------- #


async def test_run_ok_when_memory_file_exists(
    settings: Settings, monkeypatch, tmp_path: Path
):
    kb_id = "kb_testok"
    # Pre-create the file the profiler is expected to have written.
    mem_dir = settings.kb_profiler_memory_dir
    mem_dir.mkdir(parents=True, exist_ok=True)
    (mem_dir / f"{kb_id}.md").write_text("# profile", encoding="utf-8")

    raw_path = tmp_path / kb_id / "raw.xlsx"
    raw_path.parent.mkdir(parents=True, exist_ok=True)
    raw_path.write_bytes(b"fake")

    FakeClass = _make_fake_client_class([_ok_result_message()])
    monkeypatch.setattr("da_agent.ingestion.profiler.ClaudeSDKClient", FakeClass)

    profiler = KbProfiler(settings)
    result = await profiler.run(kb_id=kb_id, raw_path=raw_path, filename="sales.xlsx")

    assert result.ok is True
    assert result.error is None
    assert result.memory_path is not None
    assert result.memory_path == mem_dir / f"{kb_id}.md"


# --------------------------------------------------------------------------- #
# KbProfiler.run — error ResultMessage
# --------------------------------------------------------------------------- #


async def test_run_error_result_message_returns_ok_false(
    settings: Settings, monkeypatch, tmp_path: Path
):
    kb_id = "kb_testerr"
    raw_path = tmp_path / kb_id / "raw.xlsx"
    raw_path.parent.mkdir(parents=True, exist_ok=True)
    raw_path.write_bytes(b"fake")

    FakeClass = _make_fake_client_class([_error_result_message()])
    monkeypatch.setattr("da_agent.ingestion.profiler.ClaudeSDKClient", FakeClass)

    profiler = KbProfiler(settings)
    result = await profiler.run(kb_id=kb_id, raw_path=raw_path, filename="err.xlsx")

    assert result.ok is False
    assert result.error is not None
    assert "is_error" in result.error


# --------------------------------------------------------------------------- #
# KbProfiler.run — memory file not written
# --------------------------------------------------------------------------- #


async def test_run_ok_result_but_no_memory_file_returns_ok_false(
    settings: Settings, monkeypatch, tmp_path: Path
):
    kb_id = "kb_testnofile"
    raw_path = tmp_path / kb_id / "raw.xlsx"
    raw_path.parent.mkdir(parents=True, exist_ok=True)
    raw_path.write_bytes(b"fake")

    # Do NOT create the expected .md file — simulates profiler not writing it.
    FakeClass = _make_fake_client_class([_ok_result_message()])
    monkeypatch.setattr("da_agent.ingestion.profiler.ClaudeSDKClient", FakeClass)

    profiler = KbProfiler(settings)
    result = await profiler.run(kb_id=kb_id, raw_path=raw_path, filename="nofile.xlsx")

    assert result.ok is False
    assert result.error is not None
    assert "not found on disk" in result.error


# --------------------------------------------------------------------------- #
# KbProfiler.run — SDK raises exception
# --------------------------------------------------------------------------- #


async def test_run_sdk_exception_returns_ok_false(
    settings: Settings, monkeypatch, tmp_path: Path
):
    kb_id = "kb_testexc"
    raw_path = tmp_path / kb_id / "raw.xlsx"
    raw_path.parent.mkdir(parents=True, exist_ok=True)
    raw_path.write_bytes(b"fake")

    class ExplodingClient:
        def __init__(self, options=None):
            pass

        async def __aenter__(self):
            raise RuntimeError("sdk exploded")

        async def __aexit__(self, *_):
            pass

    monkeypatch.setattr("da_agent.ingestion.profiler.ClaudeSDKClient", ExplodingClient)

    profiler = KbProfiler(settings)
    result = await profiler.run(kb_id=kb_id, raw_path=raw_path, filename="exc.xlsx")

    assert result.ok is False
    assert result.error is not None
    assert "profiler crashed" in result.error


# --------------------------------------------------------------------------- #
# Semaphore: at most 1 concurrent SDK invocation
# --------------------------------------------------------------------------- #


async def test_semaphore_limits_concurrent_sdk_invocations(
    settings: Settings, monkeypatch, tmp_path: Path
):
    """Three concurrent run() calls must serialise — never more than 1 inside
    the SDK __aenter__ simultaneously."""

    inside_count = 0
    max_inside = 0
    lock = asyncio.Lock()

    class CountingClient:
        def __init__(self, options=None):
            pass

        async def __aenter__(self):
            nonlocal inside_count, max_inside
            async with lock:
                inside_count += 1
                if inside_count > max_inside:
                    max_inside = inside_count
            return self

        async def __aexit__(self, *_):
            nonlocal inside_count
            async with lock:
                inside_count -= 1

        async def query(self, prompt: str) -> None:
            pass

        async def receive_response(self):
            await asyncio.sleep(0.05)
            yield _ok_result_message()

    monkeypatch.setattr("da_agent.ingestion.profiler.ClaudeSDKClient", CountingClient)

    # Pre-create memory files so each run can return ok=True.
    mem_dir = settings.kb_profiler_memory_dir
    mem_dir.mkdir(parents=True, exist_ok=True)
    kb_ids = ["kb_sem1", "kb_sem2", "kb_sem3"]
    for kid in kb_ids:
        (mem_dir / f"{kid}.md").write_text("# x", encoding="utf-8")
        raw = tmp_path / kid / "raw.xlsx"
        raw.parent.mkdir(parents=True, exist_ok=True)
        raw.write_bytes(b"fake")

    # Reset the module-level semaphore so prior tests don't bleed state.
    import da_agent.ingestion.profiler as profiler_mod

    profiler_mod._PROFILE_LOCK = asyncio.Semaphore(1)

    profiler = KbProfiler(settings)
    await asyncio.gather(
        *[
            profiler.run(
                kb_id=kid, raw_path=tmp_path / kid / "raw.xlsx", filename=f"{kid}.xlsx"
            )
            for kid in kb_ids
        ]
    )

    assert max_inside == 1, f"Expected max 1 concurrent SDK client, got {max_inside}"
