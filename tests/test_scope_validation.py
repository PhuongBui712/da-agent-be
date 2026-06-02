"""Tests for spec §8.5 — per-turn data scope validation + <scope> block composition.

Covers:
- MessageRequest accepts default-only payloads (back-compat).
- The five validation rules from §8.5 (lines 656-662) return 400 with the
  exact error string.
- A successful turn opens (200) and the prompt that reaches the FakeClient
  carries the composed <scope> + <user_prompt> block.
- `render_scope` composes the prescribed verbatim form (§8.5 lines 674-687).
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import httpx
import pytest
import pytest_asyncio
from claude_agent_sdk import (
    ResultMessage,
)

from da_agent.config import Settings
from da_agent.server.app import create_app
from da_agent.server.scope import (
    ScopeAttachmentEntry,
    ScopeBlock,
    ScopeKbEntry,
    render_scope,
)


# --------------------------------------------------------------------------- #
# Fake SDK client (mirrors test_server.py)
# --------------------------------------------------------------------------- #
class FakeClient:
    instances: list["FakeClient"] = []

    def __init__(self, options=None):
        self.options = options
        self.script: list[Any] = []
        self.queries: list[str] = []
        self.permission_modes: list[str] = []
        FakeClient.instances.append(self)

    async def connect(self) -> None:
        return None

    async def disconnect(self) -> None:
        return None

    async def query(self, prompt: str) -> None:
        self.queries.append(prompt)

    async def receive_response(self):
        for item in self.script:
            if callable(item):
                result = item(self)
                if asyncio.iscoroutine(result):
                    result = await result
                if result is not None:
                    yield result
            else:
                yield item

    async def set_permission_mode(self, mode: str) -> None:
        self.permission_modes.append(mode)


def _result_message() -> ResultMessage:
    return ResultMessage(
        subtype="success",
        duration_ms=1,
        duration_api_ms=1,
        is_error=False,
        num_turns=1,
        session_id="fake",
        total_cost_usd=0.0,
    )


def _install_script(script: list):
    original_init = FakeClient.__init__

    def _init_with_script(self, options=None):
        original_init(self, options)
        self.script = list(script)

    FakeClient.__init__ = _init_with_script  # type: ignore[assignment]
    return original_init


def _restore_init(original_init):
    FakeClient.__init__ = original_init  # type: ignore[assignment]


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #
@pytest.fixture
def settings(tmp_path, monkeypatch):
    monkeypatch.setenv("DA_AGENT_HOME", str(tmp_path))
    s = Settings()
    s.data_root = tmp_path
    s.ensure_dirs()
    return s


@pytest.fixture(autouse=True)
def _reset_fake_clients():
    FakeClient.instances.clear()
    yield
    FakeClient.instances.clear()


@pytest.fixture
def patch_sdk(monkeypatch):
    monkeypatch.setattr("da_agent.agent.core.ClaudeSDKClient", FakeClient)
    return FakeClient


@pytest_asyncio.fixture
async def app(settings, patch_sdk):
    a = create_app(settings)
    async with a.router.lifespan_context(a):
        yield a


@pytest_asyncio.fixture
async def client(app):
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest_asyncio.fixture
async def sid(client):
    r = await client.post("/sessions", json={"name": "scope"})
    assert r.status_code == 201
    return r.json()["id"]


async def _drain(resp: httpx.Response) -> None:
    """Pull the SSE body to completion so the runner closes cleanly."""
    async for _ in resp.aiter_lines():
        pass


# --------------------------------------------------------------------------- #
# 1. Default — no kb_scope, no attachments → 200, turn opens with empty scope.
# 2026-06-02: a missing `kb_scope` field is identical to `kb_scope: []` —
# both yield an empty <scope> block. The BE never silently auto-loads
# every READY KB; the FE must list explicit kb_ids when it wants context.
# --------------------------------------------------------------------------- #
async def test_default_no_scope_no_attachments_starts_turn(client, sid):
    original = _install_script([_result_message()])
    try:
        async with client.stream(
            "POST", f"/sessions/{sid}/messages", json={"prompt": "x"}
        ) as resp:
            assert resp.status_code == 200
            await _drain(resp)
    finally:
        _restore_init(original)

    assert FakeClient.instances, "expected a FakeClient to be created"
    fc = FakeClient.instances[-1]
    assert fc.queries, "runner should have called query() once"
    composed = fc.queries[0]
    # No KBs, no attachments — block contains the empty-KB notice.
    assert "<scope>" in composed
    assert "no KB files are in scope" in composed
    assert "<user_prompt>" in composed and "x" in composed


# --------------------------------------------------------------------------- #
# 1b. Even with READY KBs in registry, omitting `kb_scope` yields empty scope
#     (regression test for the implicit-default-all bug, 2026-06-02).
# --------------------------------------------------------------------------- #
async def test_missing_kb_scope_does_not_auto_load_ready_kbs(app, client, sid):
    state = app.state.app_state
    meta = await state.kb.create(filename="auto.xlsx", size_bytes=10)
    await state.kb.update_status(meta.id, "READY")

    original = _install_script([_result_message()])
    try:
        async with client.stream(
            "POST", f"/sessions/{sid}/messages", json={"prompt": "Hi"}
        ) as resp:
            assert resp.status_code == 200
            await _drain(resp)
    finally:
        _restore_init(original)

    fc = FakeClient.instances[-1]
    composed = fc.queries[0]
    # The READY KB MUST NOT appear in the composed prompt.
    assert meta.id not in composed
    assert "no KB files are in scope" in composed


# --------------------------------------------------------------------------- #
# 2. kb_scope == [] → 200 with empty scope (was 400 before 2026-06-02).
# --------------------------------------------------------------------------- #
async def test_kb_scope_empty_array_yields_empty_scope(client, sid):
    original = _install_script([_result_message()])
    try:
        async with client.stream(
            "POST",
            f"/sessions/{sid}/messages",
            json={"prompt": "x", "kb_scope": []},
        ) as resp:
            assert resp.status_code == 200
            await _drain(resp)
    finally:
        _restore_init(original)

    fc = FakeClient.instances[-1]
    composed = fc.queries[0]
    assert "no KB files are in scope" in composed


# --------------------------------------------------------------------------- #
# 3. Unknown kb_id → 400.
# --------------------------------------------------------------------------- #
async def test_kb_scope_unknown_id_returns_400(client, sid):
    r = await client.post(
        f"/sessions/{sid}/messages",
        json={"prompt": "x", "kb_scope": ["kb_doesnotexist"]},
    )
    assert r.status_code == 400
    assert r.json()["detail"]["error"] == "unknown kb_id: kb_doesnotexist"


# --------------------------------------------------------------------------- #
# 4. Non-READY (PENDING) kb → 400.
# --------------------------------------------------------------------------- #
async def test_kb_scope_non_ready_returns_400(app, client, sid):
    state = app.state.app_state
    meta = await state.kb.create(filename="pending.xlsx", size_bytes=10)
    # Status starts at PENDING by default.
    r = await client.post(
        f"/sessions/{sid}/messages",
        json={"prompt": "x", "kb_scope": [meta.id]},
    )
    assert r.status_code == 400
    assert r.json()["detail"]["error"] == (
        f"kb {meta.id} is in status PENDING; only READY/READY_PARTIAL files can be scoped"
    )


# --------------------------------------------------------------------------- #
# 5. Explicit READY kb → 200 and prompt carries the kb id + memory path.
# --------------------------------------------------------------------------- #
async def test_kb_scope_ready_kb_starts_turn(app, client, sid):
    state = app.state.app_state
    meta = await state.kb.create(filename="sales.xlsx", size_bytes=10)

    # Write a memory file so _kb_entry resolves a non-None memory_path.
    memory_dir = state.settings.kb_profiler_memory_dir
    memory_dir.mkdir(parents=True, exist_ok=True)
    memory_file = memory_dir / f"{meta.id}.md"
    memory_file.write_text("# sales notes", encoding="utf-8")

    await state.kb.update_status(meta.id, "READY", memory_path=str(memory_file))

    original = _install_script([_result_message()])
    try:
        async with client.stream(
            "POST",
            f"/sessions/{sid}/messages",
            json={"prompt": "summary", "kb_scope": [meta.id]},
        ) as resp:
            assert resp.status_code == 200
            await _drain(resp)
    finally:
        _restore_init(original)

    fc = FakeClient.instances[-1]
    composed = fc.queries[0]
    assert meta.id in composed
    assert "sales.xlsx" in composed
    assert "memory at" in composed


# --------------------------------------------------------------------------- #
# 6. Unknown attachment_id → 400.
# --------------------------------------------------------------------------- #
async def test_attachments_unknown_id_returns_400(client, sid):
    r = await client.post(
        f"/sessions/{sid}/messages",
        json={
            "prompt": "x",
            "attachments": [{"attachment_id": "att_unknown"}],
        },
    )
    assert r.status_code == 400
    assert r.json()["detail"]["error"] == "unknown attachment_id: att_unknown"


# --------------------------------------------------------------------------- #
# 7. Duplicate attachment_id → 400.
# --------------------------------------------------------------------------- #
async def test_attachments_duplicate_returns_400(app, client, sid):
    # Seed a real attachment so the second occurrence trips the duplicate
    # check rather than the unknown-id check (the first ref is resolved
    # before the second ref's `seen` membership test runs).
    state = app.state.app_state
    att = await state.attachments.create(
        sid, filename="d.bin", size_bytes=4, mime="application/octet-stream"
    )
    path = state.attachments.path_for(att)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"data")

    r = await client.post(
        f"/sessions/{sid}/messages",
        json={
            "prompt": "x",
            "attachments": [
                {"attachment_id": att.id},
                {"attachment_id": att.id},
            ],
        },
    )
    assert r.status_code == 400
    assert r.json()["detail"]["error"] == "duplicate attachment_id"


# --------------------------------------------------------------------------- #
# 8. Valid attachment → 200, prompt references the on-disk file path.
# --------------------------------------------------------------------------- #
async def test_attachment_valid_starts_turn(app, client, sid):
    state = app.state.app_state
    att = await state.attachments.create(
        sid, filename="x.xlsx", size_bytes=10, mime="application/octet-stream"
    )
    # Materialise the file at the registry's path_for() location so the
    # existence check in build_scope passes.
    path = state.attachments.path_for(att)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * 10)

    original = _install_script([_result_message()])
    try:
        async with client.stream(
            "POST",
            f"/sessions/{sid}/messages",
            json={
                "prompt": "look",
                "attachments": [{"attachment_id": att.id}],
            },
        ) as resp:
            assert resp.status_code == 200
            await _drain(resp)
    finally:
        _restore_init(original)

    fc = FakeClient.instances[-1]
    composed = fc.queries[0]
    assert "Short-term attachments" in composed
    assert str(path) in composed


# --------------------------------------------------------------------------- #
# 9. Unit test of render_scope — empty / single-kb / single-attachment forms.
# --------------------------------------------------------------------------- #
def test_render_scope_empty_block():
    out = render_scope(ScopeBlock(), "hello")
    assert "<scope>" in out
    assert "no KB files are in scope" in out
    assert "<user_prompt>" in out
    assert "hello" in out
    assert "</user_prompt>" in out


def test_render_scope_with_kb_entry():
    block = ScopeBlock(
        kb_entries=[
            ScopeKbEntry(
                kb_id="kb_xxx",
                filename="file.xlsx",
                memory_path=Path("/tmp/kb/kb_xxx/memory.md"),
                memory_size=128,
            )
        ]
    )
    out = render_scope(block, "go")
    assert "For this turn, only these KB files are in scope:" in out
    assert "- kb_xxx (file.xlsx) — memory at /tmp/kb/kb_xxx/memory.md" in out


def test_render_scope_with_attachment_entry():
    block = ScopeBlock(
        attachment_entries=[
            ScopeAttachmentEntry(
                attachment_id="att_a",
                filename="draft.xlsx",
                file_path=Path("/tmp/att/sess_x/att_a/draft.xlsx"),
            )
        ]
    )
    out = render_scope(block, "go")
    assert "Short-term attachments" in out
    assert "/tmp/att/sess_x/att_a/draft.xlsx" in out


# --------------------------------------------------------------------------- #
# Spec §8.5 — symlink farm enforces add_dirs scoping.
# --------------------------------------------------------------------------- #
async def test_kb_scope_rebuilds_symlinks(app, client, sid):
    """Per-turn rebuild swaps the symlink farm to match kb_scope exactly."""
    state = app.state.app_state
    settings = state.settings

    # Seed two READY KBs whose canonical kb_dir/<id>/ directories exist.
    a = await state.kb.create(filename="a.xlsx", size_bytes=10)
    await state.kb.update_status(a.id, "READY")
    (settings.kb_dir / a.id).mkdir(parents=True, exist_ok=True)

    b = await state.kb.create(filename="b.xlsx", size_bytes=10)
    await state.kb.update_status(b.id, "READY")
    (settings.kb_dir / b.id).mkdir(parents=True, exist_ok=True)

    farm = settings.session_kb_dir(sid)

    # Turn 1: kb_scope=[a]
    original = _install_script([_result_message()])
    try:
        async with client.stream(
            "POST",
            f"/sessions/{sid}/messages",
            json={"prompt": "p", "kb_scope": [a.id]},
        ) as resp:
            assert resp.status_code == 200
            await _drain(resp)
    finally:
        _restore_init(original)

    children = {p.name for p in farm.iterdir() if p.is_symlink()}
    assert children == {a.id}

    # Turn 2: kb_scope=[b] — stale symlink for `a` is removed, `b` is added.
    original = _install_script([_result_message()])
    try:
        async with client.stream(
            "POST",
            f"/sessions/{sid}/messages",
            json={"prompt": "p2", "kb_scope": [b.id]},
        ) as resp:
            assert resp.status_code == 200
            await _drain(resp)
    finally:
        _restore_init(original)

    children = {p.name for p in farm.iterdir() if p.is_symlink()}
    assert children == {b.id}


async def test_session_root_canonical_outputs_dir_exists(app, client, sid):
    """`prepare_session_root` (run by post_message) ensures the canonical
    `outputs/<sid>/` exists and is a real directory.

    2026-06-02 Bug-A: outputs is no longer aliased via a symlink under
    `sessions-data/<sid>/outputs/`. The canonical path is in `add_dirs`
    directly so the SDK sandbox can write through it.
    """
    state = app.state.app_state
    settings = state.settings

    # Trigger a turn so post_message → prepare_session_root fires.
    original = _install_script([_result_message()])
    try:
        async with client.stream(
            "POST", f"/sessions/{sid}/messages", json={"prompt": "p"}
        ) as resp:
            assert resp.status_code == 200
            await _drain(resp)
    finally:
        _restore_init(original)

    canonical = settings.outputs_dir / sid
    assert canonical.is_dir() and not canonical.is_symlink()
    # The legacy alias must NOT exist.
    view = settings.session_outputs_view_dir(sid)
    assert not view.exists() or not view.is_symlink()


def test_add_dirs_uses_session_farm_plus_canonical_outputs(settings):
    """AgentRunner with a session_id must list per-session farm paths for kb +
    workspace AND the CANONICAL outputs/<sid> path (Bug-A fix). The bare
    global `kb_dir`/`outputs_dir`/`attachments_dir` must NOT be in add_dirs."""
    from da_agent.agent.core import AgentRunner

    class _SilentUI:
        def begin_wait(self, *_a, **_k):
            pass

        def end_wait(self):
            pass

        def on_todos(self, *_a, **_k):
            pass

    sid = "sess_farm_assert"
    runner = AgentRunner(_SilentUI(), settings, session_id=sid)
    opts = runner._build_options()

    add_dirs = list(opts.add_dirs)
    assert str(settings.kb_dir) not in add_dirs
    assert str(settings.outputs_dir) not in add_dirs  # bare root forbidden
    assert str(settings.attachments_dir) not in add_dirs
    # Per-session farm for kb + workspace.
    assert str(settings.session_kb_dir(sid)) in add_dirs
    assert str(settings.session_workspace_dir(sid)) in add_dirs
    # Canonical outputs is in add_dirs (sandbox-allowed write target).
    assert str(settings.outputs_session_dir(sid)) in add_dirs
    # The deprecated symlink alias path is NOT in add_dirs.
    assert str(settings.session_outputs_view_dir(sid)) not in add_dirs


# --------------------------------------------------------------------------- #
# 10. Explicit scope with PROFILING kb in same registry — only the explicitly
#     listed READY one ends up in scope (2026-06-02: replaces the legacy
#     default-all test).
# --------------------------------------------------------------------------- #
async def test_explicit_kb_scope_filters_to_only_listed_ready(app, client, sid):
    state = app.state.app_state
    ready = await state.kb.create(filename="ready.xlsx", size_bytes=10)
    await state.kb.update_status(ready.id, "READY")
    profiling = await state.kb.create(filename="profiling.xlsx", size_bytes=10)
    await state.kb.update_status(profiling.id, "PROFILING")

    original = _install_script([_result_message()])
    try:
        async with client.stream(
            "POST",
            f"/sessions/{sid}/messages",
            json={"prompt": "p", "kb_scope": [ready.id]},
        ) as resp:
            assert resp.status_code == 200
            await _drain(resp)
    finally:
        _restore_init(original)

    fc = FakeClient.instances[-1]
    composed = fc.queries[0]
    assert ready.id in composed
    assert profiling.id not in composed


# --------------------------------------------------------------------------- #
# 11. READY_PARTIAL is scopable and renders with the legacy fallback line.
# --------------------------------------------------------------------------- #
async def test_kb_scope_ready_partial_is_scopable(app, client, sid):
    state = app.state.app_state
    meta = await state.kb.create(filename="partial.xlsx", size_bytes=10)
    await state.kb.update_status(meta.id, "READY_PARTIAL")

    original = _install_script([_result_message()])
    try:
        async with client.stream(
            "POST",
            f"/sessions/{sid}/messages",
            json={"prompt": "go", "kb_scope": [meta.id]},
        ) as resp:
            assert resp.status_code == 200
            await _drain(resp)
    finally:
        _restore_init(original)

    fc = FakeClient.instances[-1]
    composed = fc.queries[0]
    assert meta.id in composed
    assert "NO MEMORY" in composed


# --------------------------------------------------------------------------- #
# 12. READY kb with no memory file on disk → legacy fallback line in prompt.
# --------------------------------------------------------------------------- #
async def test_ready_kb_with_no_memory_file_renders_legacy_fallback(app, client, sid):
    state = app.state.app_state
    meta = await state.kb.create(filename="legacy.xlsx", size_bytes=10)
    # Mark READY but deliberately leave no memory file on disk.
    await state.kb.update_status(meta.id, "READY")

    original = _install_script([_result_message()])
    try:
        async with client.stream(
            "POST",
            f"/sessions/{sid}/messages",
            json={"prompt": "go", "kb_scope": [meta.id]},
        ) as resp:
            assert resp.status_code == 200
            await _drain(resp)
    finally:
        _restore_init(original)

    fc = FakeClient.instances[-1]
    composed = fc.queries[0]
    assert meta.id in composed
    assert "NO MEMORY (legacy; inspect raw.xlsx via xlsx skill)" in composed
