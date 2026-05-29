"""Tests for session history: sdk_session_id persistence, WebAgentUI.on_system,
SessionRegistry.set_sdk_session_id, replay_to_events, and GET /sessions/{sid}/messages.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any
from unittest.mock import MagicMock

import httpx
import pytest
import pytest_asyncio
from claude_agent_sdk import ResultMessage, SessionMessage

from da_agent.config import Settings
from da_agent.server.app import create_app
from da_agent.server.replay import replay_to_events
from da_agent.server.state import SessionRegistry
from da_agent.server.web_ui import WebAgentUI


# --------------------------------------------------------------------------- #
# Re-use FakeClient from test_server (copy pattern to avoid import coupling)
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


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #
@pytest.fixture(autouse=True)
def _reset_fake_clients():
    FakeClient.instances.clear()
    yield
    FakeClient.instances.clear()


@pytest.fixture
def settings(tmp_path, monkeypatch):
    monkeypatch.setenv("DA_AGENT_HOME", str(tmp_path))
    s = Settings()
    s.data_root = tmp_path
    s.ensure_dirs()
    return s


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


def _session_message(
    msg_type: str,
    content: Any,
    uuid: str = "u1",
    sdk_session_id: str = "sdk-sess",
) -> SessionMessage:
    """Build a SessionMessage with the given role and content."""
    if msg_type == "user":
        message = {"role": "user", "content": content}
    else:
        message = {"role": "assistant", "content": content}
    return SessionMessage(
        uuid=uuid,
        session_id=sdk_session_id,
        message=message,
        type=msg_type,
    )


# --------------------------------------------------------------------------- #
# 1. Fresh session returns empty history
# --------------------------------------------------------------------------- #
async def test_history_empty_for_fresh_session(client):
    r = await client.post("/sessions", json={})
    assert r.status_code == 201
    body = r.json()
    assert body["name"] == "Untitled"
    sid = body["id"]

    r2 = await client.get(f"/sessions/{sid}/messages")
    assert r2.status_code == 200
    assert r2.json() == {"events": []}


# --------------------------------------------------------------------------- #
# 2. 404 for unknown sid
# --------------------------------------------------------------------------- #
async def test_history_404_for_unknown_sid(client):
    r = await client.get("/sessions/sess_doesnotexist/messages")
    assert r.status_code == 404


# --------------------------------------------------------------------------- #
# 3. WebAgentUI.on_system calls callback on init with session_id
# --------------------------------------------------------------------------- #
def test_sdk_session_id_persisted_after_init():
    app_state_mock = MagicMock()
    calls: list[str] = []

    ui = WebAgentUI(
        session_id="sess_test",
        app_state=app_state_mock,
        on_sdk_session_id=calls.append,
    )

    # Happy path: subtype="init" with valid session_id
    ui.on_system("init", {"session_id": "uuid-test-123"})
    assert calls == ["uuid-test-123"]

    # subtype != "init" → no call
    calls.clear()
    ui.on_system("other", {"session_id": "uuid-ignored"})
    assert calls == []

    # Missing session_id key → no call
    calls.clear()
    ui.on_system("init", {})
    assert calls == []

    # Empty string → no call
    calls.clear()
    ui.on_system("init", {"session_id": ""})
    assert calls == []


# --------------------------------------------------------------------------- #
# 4. SessionRegistry.set_sdk_session_id idempotency
# --------------------------------------------------------------------------- #
async def test_set_sdk_session_id_idempotent(tmp_path):
    reg = SessionRegistry(tmp_path / "registry.json")
    meta = await reg.create(name="s")
    sid = meta.id

    # First set → True
    result = await reg.set_sdk_session_id(sid, "uuid-1")
    assert result is True

    # Same value again → False (idempotent)
    result2 = await reg.set_sdk_session_id(sid, "uuid-1")
    assert result2 is False

    # Value is stored correctly
    m = await reg.get(sid)
    assert m is not None
    assert m.sdk_session_id == "uuid-1"

    # Different UUID → True, value updated
    result3 = await reg.set_sdk_session_id(sid, "uuid-2")
    assert result3 is True
    m2 = await reg.get(sid)
    assert m2 is not None
    assert m2.sdk_session_id == "uuid-2"

    # Non-existent sid → False
    result4 = await reg.set_sdk_session_id("sess_nope", "uuid-x")
    assert result4 is False


# --------------------------------------------------------------------------- #
# 5. resume_sdk_session_id passed to FakeClient options
# --------------------------------------------------------------------------- #
async def test_resume_passes_sdk_session_id_to_options(app, client):
    r = await client.post("/sessions", json={"name": "resume-test"})
    sid = r.json()["id"]

    # Persist a known SDK session ID
    state = app.state.app_state
    await state.registry.set_sdk_session_id(sid, "uuid-X")

    # Discard any cached runtime so it will be rebuilt with the new meta
    await state.discard_runtime(sid)

    # Pre-configure FakeClient script via __init__ hook
    original_init = FakeClient.__init__

    def _init_with_script(self, options=None):
        original_init(self, options)
        self.script = [_result_message()]

    FakeClient.__init__ = _init_with_script  # type: ignore[assignment]
    try:
        async with client.stream(
            "POST", f"/sessions/{sid}/messages", json={"prompt": "hi"}
        ) as resp:
            assert resp.status_code == 200
            # Drain the response
            async for _ in resp.aiter_lines():
                pass
    finally:
        FakeClient.__init__ = original_init  # type: ignore[assignment]

    assert FakeClient.instances, "Expected FakeClient to be instantiated"
    fc = FakeClient.instances[-1]
    assert fc.options is not None
    assert fc.options.resume == "uuid-X"


# --------------------------------------------------------------------------- #
# 6. replay: user text → user.prompt + final result
# --------------------------------------------------------------------------- #
def test_replay_translates_user_text_to_user_prompt():
    msgs = [_session_message("user", "hello", uuid="u1")]
    events = replay_to_events(msgs, "sess_x")

    assert len(events) == 2
    assert events[0] == {"type": "user.prompt", "session_id": "sess_x", "text": "hello"}
    assert events[1]["type"] == "result"
    assert events[1]["session_id"] == "sess_x"
    assert events[1]["turns"] == 1


# --------------------------------------------------------------------------- #
# 7. replay: assistant blocks (thinking, text, tool_use)
# --------------------------------------------------------------------------- #
def test_replay_translates_assistant_blocks():
    msgs = [
        _session_message("user", "q1", uuid="u1"),
        _session_message(
            "assistant",
            [
                {"type": "thinking", "thinking": "reasoning"},
                {"type": "text", "text": "answer"},
                {
                    "type": "tool_use",
                    "id": "t1",
                    "name": "Read",
                    "input": {"file_path": "/foo"},
                },
            ],
            uuid="u2",
        ),
    ]
    events = replay_to_events(msgs, "sess_x")

    types = [e["type"] for e in events]
    assert types == [
        "user.prompt",
        "assistant.thinking",
        "assistant.text",
        "tool.use",
        "result",
    ]
    assert events[1]["text"] == "reasoning"
    assert events[2]["text"] == "answer"
    assert events[3]["name"] == "Read"
    assert events[3]["tool_use_id"] == "t1"


# --------------------------------------------------------------------------- #
# 8. replay: tool_result from user role
# --------------------------------------------------------------------------- #
def test_replay_translates_tool_result_from_user_role():
    msgs = [
        _session_message("user", "q", uuid="u1"),
        _session_message(
            "assistant",
            [{"type": "tool_use", "id": "t1", "name": "Read", "input": {}}],
            uuid="u2",
        ),
        _session_message(
            "user",
            [
                {
                    "type": "tool_result",
                    "tool_use_id": "t1",
                    "content": "output",
                    "is_error": False,
                }
            ],
            uuid="u3",
        ),
        _session_message("assistant", [{"type": "text", "text": "done"}], uuid="u4"),
    ]
    events = replay_to_events(msgs, "sess_x")

    tool_results = [e for e in events if e["type"] == "tool.result"]
    assert len(tool_results) == 1
    tr = tool_results[0]
    assert tr["summary"] == "output"
    assert tr["is_error"] is False
    assert tr["tool_use_id"] == "t1"
    assert tr["depth"] == 0


# --------------------------------------------------------------------------- #
# 9. replay: result event inserted between turns, turns counter increments
# --------------------------------------------------------------------------- #
def test_replay_inserts_result_between_turns():
    msgs = [
        _session_message("user", "q1", uuid="u1"),
        _session_message("assistant", [{"type": "text", "text": "a1"}], uuid="u2"),
        _session_message("user", "q2", uuid="u3"),
        _session_message("assistant", [{"type": "text", "text": "a2"}], uuid="u4"),
    ]
    events = replay_to_events(msgs, "sess_x")

    types = [e["type"] for e in events]
    # Expected: user.prompt, assistant.text, result(turns=1), user.prompt, assistant.text, result(turns=2)
    assert types == [
        "user.prompt",
        "assistant.text",
        "result",
        "user.prompt",
        "assistant.text",
        "result",
    ]

    result_events = [e for e in events if e["type"] == "result"]
    assert len(result_events) == 2
    assert result_events[0]["turns"] == 1
    assert result_events[1]["turns"] == 2

    # The inter-turn result appears BEFORE the second user.prompt
    result_idx = types.index("result")
    second_prompt_idx = [i for i, t in enumerate(types) if t == "user.prompt"][1]
    assert result_idx < second_prompt_idx


# --------------------------------------------------------------------------- #
# 10. Registry round-trip preserves sdk_session_id
# --------------------------------------------------------------------------- #
async def test_registry_round_trip_preserves_sdk_session_id(tmp_path):
    reg_path = tmp_path / "registry.json"
    reg1 = SessionRegistry(reg_path)
    meta = await reg1.create(name="persist-test")
    sid = meta.id
    await reg1.set_sdk_session_id(sid, "uuid-persist")

    # Load from fresh registry instance
    reg2 = SessionRegistry(reg_path)
    loaded = await reg2.get(sid)
    assert loaded is not None
    assert loaded.sdk_session_id == "uuid-persist"


# --------------------------------------------------------------------------- #
# 11. Legacy JSON without sdk_session_id → meta.sdk_session_id is None
# --------------------------------------------------------------------------- #
async def test_registry_loads_legacy_json_without_sdk_session_id(tmp_path):
    reg_path = tmp_path / "registry.json"
    # Write legacy format without sdk_session_id field
    legacy = {
        "sessions": [
            {
                "id": "sess_legacy001",
                "name": "Legacy",
                "created_at": 1000000.0,
                "updated_at": 1000001.0,
                "parent_id": None,
            }
        ]
    }
    reg_path.write_text(json.dumps(legacy), encoding="utf-8")

    reg = SessionRegistry(reg_path)
    meta = await reg.get("sess_legacy001")
    assert meta is not None
    assert meta.sdk_session_id is None


# --------------------------------------------------------------------------- #
# 12. replay: interactive tools filtered out (TodoWrite, TaskCreate, etc.)
# --------------------------------------------------------------------------- #
def test_replay_filters_interactive_tools():
    msgs = [
        _session_message("user", "q", uuid="u1"),
        _session_message(
            "assistant",
            [
                {"type": "tool_use", "id": "t1", "name": "TodoWrite", "input": {}},
                {
                    "type": "tool_use",
                    "id": "t2",
                    "name": "Read",
                    "input": {"file_path": "/x"},
                },
            ],
            uuid="u2",
        ),
    ]
    events = replay_to_events(msgs, "sess_x")

    tool_uses = [e for e in events if e["type"] == "tool.use"]
    assert len(tool_uses) == 1
    assert tool_uses[0]["name"] == "Read"


# --------------------------------------------------------------------------- #
# 13. replay: whitespace-only text and thinking blocks are skipped
# --------------------------------------------------------------------------- #
def test_replay_skips_whitespace_only_text():
    msgs = [
        _session_message("user", "q", uuid="u1"),
        _session_message(
            "assistant",
            [
                {"type": "text", "text": "   \n  "},
                {"type": "thinking", "thinking": ""},
                {"type": "text", "text": "real answer"},
            ],
            uuid="u2",
        ),
    ]
    events = replay_to_events(msgs, "sess_x")

    text_events = [e for e in events if e["type"] == "assistant.text"]
    thinking_events = [e for e in events if e["type"] == "assistant.thinking"]

    assert len(text_events) == 1
    assert text_events[0]["text"] == "real answer"
    assert len(thinking_events) == 0


# --------------------------------------------------------------------------- #
# 14. replay: empty messages list → empty output
# --------------------------------------------------------------------------- #
def test_replay_handles_empty_messages_list():
    events = replay_to_events([], "sess_x")
    assert events == []
