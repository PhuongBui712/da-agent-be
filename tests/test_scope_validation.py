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
# 1. Default — no kb_scope, no attachments → 200, turn opens.
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
# 2. kb_scope == [] → 400 with the verbatim spec message.
# --------------------------------------------------------------------------- #
async def test_kb_scope_empty_array_returns_400(client, sid):
    r = await client.post(
        f"/sessions/{sid}/messages",
        json={"prompt": "x", "kb_scope": []},
    )
    assert r.status_code == 400
    detail = r.json()["detail"]
    assert detail["error"] == (
        "kb_scope cannot be empty; omit the field for default-all"
    )


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
        f"kb {meta.id} is in status PENDING; only READY files can be scoped"
    )


# --------------------------------------------------------------------------- #
# 5. Explicit READY kb → 200 and prompt carries the kb id + manifest path.
# --------------------------------------------------------------------------- #
async def test_kb_scope_ready_kb_starts_turn(app, client, sid):
    state = app.state.app_state
    meta = await state.kb.create(filename="sales.xlsx", size_bytes=10)
    await state.kb.update_status(meta.id, "READY")

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
    assert "manifest at" in composed


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
                manifest_path=Path("/tmp/kb/kb_xxx/manifest.json"),
                manifest_size=42,
            )
        ]
    )
    out = render_scope(block, "go")
    assert "For this turn, only these KB files are in scope:" in out
    assert "- kb_xxx (file.xlsx) — manifest at /tmp/kb/kb_xxx/manifest.json" in out


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
# 10. Default-all with mixed READY / PROCESSING — only READY ends up in scope.
# --------------------------------------------------------------------------- #
async def test_default_all_kbs_only_ready_in_block(app, client, sid):
    state = app.state.app_state
    ready = await state.kb.create(filename="ready.xlsx", size_bytes=10)
    await state.kb.update_status(ready.id, "READY")
    processing = await state.kb.create(filename="proc.xlsx", size_bytes=10)
    await state.kb.update_status(processing.id, "PROCESSING")

    original = _install_script([_result_message()])
    try:
        async with client.stream(
            "POST", f"/sessions/{sid}/messages", json={"prompt": "p"}
        ) as resp:
            assert resp.status_code == 200
            await _drain(resp)
    finally:
        _restore_init(original)

    fc = FakeClient.instances[-1]
    composed = fc.queries[0]
    assert ready.id in composed
    assert processing.id not in composed
