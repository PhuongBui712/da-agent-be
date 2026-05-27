"""Tests for the FastAPI backend.

The Claude Agent SDK is fully mocked: `ClaudeSDKClient` is replaced by `FakeClient`
so no model is contacted. `Settings.data_root` is pinned to `tmp_path` so tests
never touch `~/.da-agent/`.

Patterns:
- HTTP is driven via `httpx.AsyncClient(transport=ASGITransport(app=app))`.
- Lifespan is started manually with `app.router.lifespan_context(app)` since
  `ASGITransport` (httpx 0.28) does not run it.
- `pyproject.toml` sets `asyncio_mode = "auto"`, so async tests need no marker.
"""

from __future__ import annotations

import asyncio
import json
import re
from typing import Any

import httpx
import pytest
import pytest_asyncio
from claude_agent_sdk import (
    AssistantMessage,
    ResultMessage,
    StreamEvent,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
)

from da_agent.config import Settings
from da_agent.server.app import create_app
from da_agent.server.state import PendingInteraction


# --------------------------------------------------------------------------- #
# Fake SDK client
# --------------------------------------------------------------------------- #
class FakeClient:
    """Async-context-manager stand-in for `ClaudeSDKClient`.

    Tests can mutate `script` (a list of SDK messages or callables) before the
    runner calls `receive_response()`. Callables are awaited and given the
    `FakeClient` so they can drive the `can_use_tool` callback or the UI directly.
    """

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


# --------------------------------------------------------------------------- #
# SSE parsing helpers
# --------------------------------------------------------------------------- #
async def _collect_sse(response: httpx.Response) -> list[tuple[str, dict]]:
    events: list[tuple[str, dict]] = []
    event_type: str | None = None
    data_lines: list[str] = []
    async for raw in response.aiter_lines():
        line = raw.rstrip("\r")
        if line == "":
            if data_lines:
                payload = "\n".join(data_lines)
                try:
                    parsed = json.loads(payload)
                except json.JSONDecodeError:
                    parsed = {"raw": payload}
                events.append((event_type or "message", parsed))
            event_type, data_lines = None, []
        elif line.startswith("event:"):
            event_type = line[len("event:") :].strip()
        elif line.startswith("data:"):
            data_lines.append(line[len("data:") :].lstrip())
    return events


async def _post_message_events(
    client: httpx.AsyncClient, sid: str, prompt: str
) -> list[tuple[str, dict]]:
    async with client.stream(
        "POST", f"/sessions/{sid}/messages", json={"prompt": prompt}
    ) as resp:
        assert resp.status_code == 200
        return await _collect_sse(resp)


# --------------------------------------------------------------------------- #
# A. Session CRUD
# --------------------------------------------------------------------------- #
async def test_list_sessions_initially_empty(client):
    r = await client.get("/sessions")
    assert r.status_code == 200
    assert r.json() == {"sessions": []}


async def test_create_session_returns_201(client):
    r = await client.post("/sessions", json={"name": "alpha"})
    assert r.status_code == 201
    body = r.json()
    assert body["name"] == "alpha"
    assert body["id"].startswith("sess_")
    assert body["parent_id"] is None


async def test_list_after_create_includes_session(client):
    create = await client.post("/sessions", json={"name": "beta"})
    sid = create.json()["id"]
    r = await client.get("/sessions")
    assert r.status_code == 200
    ids = [s["id"] for s in r.json()["sessions"]]
    assert sid in ids


async def test_get_session_404_for_unknown_id(client):
    create = await client.post("/sessions", json={"name": "gamma"})
    sid = create.json()["id"]
    ok = await client.get(f"/sessions/{sid}")
    assert ok.status_code == 200
    assert ok.json()["id"] == sid

    missing = await client.get("/sessions/sess_doesnotexist")
    assert missing.status_code == 404


async def test_patch_rename_and_404(client):
    create = await client.post("/sessions", json={"name": "old"})
    sid = create.json()["id"]
    r = await client.patch(f"/sessions/{sid}", json={"name": "renamed"})
    assert r.status_code == 200
    assert r.json()["name"] == "renamed"

    missing = await client.patch("/sessions/sess_nope", json={"name": "x"})
    assert missing.status_code == 404


async def test_delete_then_404_on_second_delete(client):
    create = await client.post("/sessions", json={"name": "to-delete"})
    sid = create.json()["id"]
    first = await client.delete(f"/sessions/{sid}")
    assert first.status_code == 204
    second = await client.delete(f"/sessions/{sid}")
    assert second.status_code == 404


async def test_fork_sets_parent_id(client):
    parent = await client.post("/sessions", json={"name": "parent"})
    parent_id = parent.json()["id"]
    r = await client.post(f"/sessions/{parent_id}/fork", json={"name": "child"})
    assert r.status_code == 201
    body = r.json()
    assert body["parent_id"] == parent_id
    assert body["name"] == "child"
    assert body["id"] != parent_id


# --------------------------------------------------------------------------- #
# B. Interaction respond endpoint
# --------------------------------------------------------------------------- #
async def test_respond_question_resolves_future(app, client):
    create = await client.post("/sessions", json={"name": "q"})
    sid = create.json()["id"]
    state = app.state.app_state
    loop = asyncio.get_running_loop()
    future: asyncio.Future = loop.create_future()
    pending = PendingInteraction(
        tool_use_id="tu_q1",
        kind="question",
        payload={"questions": [{"question": "Q?", "header": "H", "options": []}]},
        future=future,
    )
    await state.interactions.park(sid, pending)

    body = {
        "answers": [
            {"header": "H", "selected": ["A"], "other_text": None},
        ]
    }
    r = await client.post(f"/sessions/{sid}/interactions/tu_q1/respond", json=body)
    assert r.status_code == 204

    value = await asyncio.wait_for(future, timeout=1.0)
    assert isinstance(value, list) and len(value) == 1
    assert value[0]["header"] == "H"
    assert value[0]["selected"] == ["A"]


async def test_respond_plan_resolves_future(app, client):
    create = await client.post("/sessions", json={"name": "p"})
    sid = create.json()["id"]
    state = app.state.app_state
    loop = asyncio.get_running_loop()
    future: asyncio.Future = loop.create_future()
    pending = PendingInteraction(
        tool_use_id="tu_p1", kind="plan", payload={"plan": "do thing"}, future=future
    )
    await state.interactions.park(sid, pending)

    r = await client.post(
        f"/sessions/{sid}/interactions/tu_p1/respond",
        json={"verdict": "approve"},
    )
    assert r.status_code == 204

    value = await asyncio.wait_for(future, timeout=1.0)
    assert value["verdict"] == "approve"


async def test_respond_unknown_tool_use_id_returns_404(client):
    create = await client.post("/sessions", json={"name": "n"})
    sid = create.json()["id"]
    r = await client.post(
        f"/sessions/{sid}/interactions/tu_missing/respond",
        json={"verdict": "approve"},
    )
    assert r.status_code == 404


async def test_pending_lists_parked_interactions(app, client):
    create = await client.post("/sessions", json={"name": "pending"})
    sid = create.json()["id"]
    state = app.state.app_state
    loop = asyncio.get_running_loop()
    future: asyncio.Future = loop.create_future()
    pending = PendingInteraction(
        tool_use_id="tu_x", kind="question", payload={"foo": "bar"}, future=future
    )
    await state.interactions.park(sid, pending)

    r = await client.get(f"/sessions/{sid}/interactions/pending")
    assert r.status_code == 200
    items = r.json()["pending"]
    assert len(items) == 1
    assert items[0]["tool_use_id"] == "tu_x"
    assert items[0]["kind"] == "question"
    assert items[0]["payload"] == {"foo": "bar"}

    # Don't leave the future hanging.
    future.cancel()


# --------------------------------------------------------------------------- #
# C. Todos snapshot streaming
# --------------------------------------------------------------------------- #
def _assistant(*blocks) -> AssistantMessage:
    return AssistantMessage(content=list(blocks), model="fake-model")


def _user_tool_result(tool_use_id: str, content: str) -> UserMessage:
    return UserMessage(
        content=[
            ToolResultBlock(tool_use_id=tool_use_id, content=content, is_error=False)
        ]
    )


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


async def test_todos_snapshot_emitted_on_task_create(app, client):
    create = await client.post("/sessions", json={"name": "todos1"})
    sid = create.json()["id"]

    def install_script(fc: FakeClient):
        fc.script = [
            _assistant(
                ToolUseBlock(
                    id="tu_create_1",
                    name="TaskCreate",
                    input={"subject": "X", "activeForm": "Doing X"},
                )
            ),
            _user_tool_result("tu_create_1", "Task #1 created successfully: X"),
            _result_message(),
        ]

    # The runtime is created lazily on first message — install a hook so the new
    # FakeClient is configured the moment AgentRunner instantiates it.
    original_init = FakeClient.__init__

    def _init_with_script(self, options=None):
        original_init(self, options)
        install_script(self)

    FakeClient.__init__ = _init_with_script  # type: ignore[assignment]
    try:
        events = await _post_message_events(client, sid, "go")
    finally:
        FakeClient.__init__ = original_init  # type: ignore[assignment]

    snapshots = [e for t, e in events if t == "todos.snapshot"]
    # At least: initial empty snapshot + one snapshot containing task "1".
    assert len(snapshots) >= 2
    last = snapshots[-1]
    items = last.get("items", [])
    assert any(it.get("task_id") == "1" and it.get("subject") == "X" for it in items)


async def test_todos_snapshot_status_update(app, client):
    create = await client.post("/sessions", json={"name": "todos2"})
    sid = create.json()["id"]

    def install_script(fc: FakeClient):
        fc.script = [
            _assistant(
                ToolUseBlock(
                    id="tu_create_1",
                    name="TaskCreate",
                    input={"subject": "X", "activeForm": "Doing X"},
                )
            ),
            _user_tool_result("tu_create_1", "Task #1 created successfully: X"),
            _assistant(
                ToolUseBlock(
                    id="tu_update_1",
                    name="TaskUpdate",
                    input={"taskId": "1", "status": "completed"},
                )
            ),
            _result_message(),
        ]

    original_init = FakeClient.__init__

    def _init_with_script(self, options=None):
        original_init(self, options)
        install_script(self)

    FakeClient.__init__ = _init_with_script  # type: ignore[assignment]
    try:
        events = await _post_message_events(client, sid, "go")
    finally:
        FakeClient.__init__ = original_init  # type: ignore[assignment]

    snapshots = [e for t, e in events if t == "todos.snapshot"]
    assert snapshots, "expected at least one todos.snapshot event"
    final_items = snapshots[-1].get("items", [])
    assert any(
        it.get("task_id") == "1" and it.get("status") == "completed"
        for it in final_items
    )


# --------------------------------------------------------------------------- #
# D. Interactive flow — interaction.requested + /respond resolves the future
# --------------------------------------------------------------------------- #
async def test_interaction_requested_and_respond_resolves(app, client):
    """Drive `ui.ask_question` directly inside the FakeClient and then resolve it
    via the public /respond endpoint. This verifies the full park -> SSE event ->
    REST resolve loop without exercising the real `can_use_tool` plumbing."""
    create = await client.post("/sessions", json={"name": "interactive"})
    sid = create.json()["id"]

    from da_agent.agent.events import Option, Question, QuestionRequest

    async def driver(fc: FakeClient):
        # Find the runtime the server just created and reach its UI.
        runtime = await app.state.app_state.get_or_create_runtime(sid)
        assert runtime is not None and runtime.ui is not None
        request = QuestionRequest(
            questions=[
                Question(
                    question="q1",
                    header="H",
                    options=[Option(label="A", description="")],
                )
            ]
        )
        # Spawn the UI ask in a background task so we can simulate the frontend
        # POSTing to /respond while the SDK turn is "in flight".
        ask_task = asyncio.create_task(runtime.ui.ask_question(request))

        # Wait until the interaction is parked.
        for _ in range(50):
            pending = await app.state.app_state.interactions.pending(sid)
            if pending:
                break
            await asyncio.sleep(0.01)
        assert pending, "interaction was never parked"
        tool_use_id = pending[0].tool_use_id

        # Use a separate AsyncClient — the outer `client` is busy reading the SSE.
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://test"
        ) as inner:
            r = await inner.post(
                f"/sessions/{sid}/interactions/{tool_use_id}/respond",
                json={
                    "answers": [{"header": "H", "selected": ["A"], "other_text": None}]
                },
            )
            assert r.status_code == 204

        response = await asyncio.wait_for(ask_task, timeout=1.0)
        assert response.answers and response.answers[0].selected == ["A"]
        return None  # nothing to yield from receive_response for this step

    def install_script(fc: FakeClient):
        fc.script = [driver, _result_message()]

    original_init = FakeClient.__init__

    def _init_with_script(self, options=None):
        original_init(self, options)
        install_script(self)

    FakeClient.__init__ = _init_with_script  # type: ignore[assignment]
    try:
        events = await _post_message_events(client, sid, "ask me")
    finally:
        FakeClient.__init__ = original_init  # type: ignore[assignment]

    kinds = [t for t, _ in events]
    assert "interaction.requested" in kinds
    requested = next(payload for t, payload in events if t == "interaction.requested")
    assert requested["kind"] == "question"
    assert requested["questions"][0]["header"] == "H"


# --------------------------------------------------------------------------- #
# E. Streaming SSE (spec §8.6)
# --------------------------------------------------------------------------- #

_BLOCK_ID_TEXT_RE = re.compile(r"^txt_[0-9a-f]{12}$")
_BLOCK_ID_THINK_RE = re.compile(r"^thk_[0-9a-f]{12}$")


def _stream_event(event: dict, parent_tool_use_id: str | None = None) -> StreamEvent:
    return StreamEvent(
        uuid="u1",
        session_id="s1",
        event=event,
        parent_tool_use_id=parent_tool_use_id,
    )


def _text_block_stream(index: int, *deltas: str) -> list[StreamEvent]:
    """Return the start, N delta, stop events for a text block at `index`."""
    events = [
        _stream_event(
            {
                "type": "content_block_start",
                "index": index,
                "content_block": {"type": "text"},
            }
        ),
    ]
    for d in deltas:
        events.append(
            _stream_event(
                {
                    "type": "content_block_delta",
                    "index": index,
                    "delta": {"type": "text_delta", "text": d},
                }
            )
        )
    events.append(_stream_event({"type": "content_block_stop", "index": index}))
    return events


def _thinking_block_stream(index: int, *deltas: str) -> list[StreamEvent]:
    """Return the start, N delta, stop events for a thinking block at `index`."""
    events = [
        _stream_event(
            {
                "type": "content_block_start",
                "index": index,
                "content_block": {"type": "thinking"},
            }
        ),
    ]
    for d in deltas:
        events.append(
            _stream_event(
                {
                    "type": "content_block_delta",
                    "index": index,
                    "delta": {"type": "thinking_delta", "thinking": d},
                }
            )
        )
    events.append(_stream_event({"type": "content_block_stop", "index": index}))
    return events


def _install_script(script: list):
    """Context-manager helper that monkeypatches FakeClient.__init__ to install a script."""
    original_init = FakeClient.__init__

    def _init_with_script(self, options=None):
        original_init(self, options)
        self.script = list(script)

    FakeClient.__init__ = _init_with_script  # type: ignore[assignment]
    return original_init


def _restore_init(original_init):
    FakeClient.__init__ = original_init  # type: ignore[assignment]


# 16 -----------------------------------------------------------------------
async def test_streaming_emits_text_delta_and_end_sse_events(app, client):
    create = await client.post("/sessions", json={"name": "s16"})
    sid = create.json()["id"]

    script = [
        *_text_block_stream(0, "Hello", " world"),
        _result_message(),
    ]
    original = _install_script(script)
    try:
        events = await _post_message_events(client, sid, "go")
    finally:
        _restore_init(original)

    delta_events = [(t, p) for t, p in events if t == "assistant.text.delta"]
    end_events = [(t, p) for t, p in events if t == "assistant.text.end"]

    assert len(delta_events) == 2
    assert len(end_events) == 1

    block_id = delta_events[0][1]["block_id"]
    assert _BLOCK_ID_TEXT_RE.match(block_id), (
        f"block_id {block_id!r} does not match pattern"
    )
    assert delta_events[1][1]["block_id"] == block_id
    assert end_events[0][1]["block_id"] == block_id

    texts = [p["text"] for _, p in delta_events]
    assert texts == ["Hello", " world"]


# 17 -----------------------------------------------------------------------
async def test_streaming_emits_thinking_delta_and_end_sse_events(app, client):
    create = await client.post("/sessions", json={"name": "s17"})
    sid = create.json()["id"]

    script = [
        *_thinking_block_stream(0, "hmm", " deeper"),
        _result_message(),
    ]
    original = _install_script(script)
    try:
        events = await _post_message_events(client, sid, "go")
    finally:
        _restore_init(original)

    delta_events = [(t, p) for t, p in events if t == "assistant.thinking.delta"]
    end_events = [(t, p) for t, p in events if t == "assistant.thinking.end"]

    assert len(delta_events) == 2
    assert len(end_events) == 1

    block_id = delta_events[0][1]["block_id"]
    assert _BLOCK_ID_THINK_RE.match(block_id), (
        f"block_id {block_id!r} does not match pattern"
    )
    assert delta_events[1][1]["block_id"] == block_id
    assert end_events[0][1]["block_id"] == block_id


# 18 -----------------------------------------------------------------------
async def test_streaming_first_text_delta_emits_wait_end(app, client):
    create = await client.post("/sessions", json={"name": "s18"})
    sid = create.json()["id"]

    script = [
        *_text_block_stream(0, "hi"),
        _result_message(),
    ]
    original = _install_script(script)
    try:
        events = await _post_message_events(client, sid, "go")
    finally:
        _restore_init(original)

    event_types = [t for t, _ in events]

    # At least one wait.end must appear before the first assistant.text.delta.
    first_delta_idx = event_types.index("assistant.text.delta")
    wait_end_before_delta = [
        i for i, t in enumerate(event_types) if t == "wait.end" and i < first_delta_idx
    ]
    assert wait_end_before_delta, (
        "A wait.end must appear before the first assistant.text.delta"
    )


# 19 -----------------------------------------------------------------------
async def test_streaming_subagent_event_does_not_reach_sse(app, client):
    create = await client.post("/sessions", json={"name": "s19"})
    sid = create.json()["id"]

    subagent_events = [
        StreamEvent(
            uuid="u1",
            session_id="s1",
            event={
                "type": "content_block_start",
                "index": 0,
                "content_block": {"type": "text"},
            },
            parent_tool_use_id="t_sub",
        ),
        StreamEvent(
            uuid="u2",
            session_id="s1",
            event={
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "text_delta", "text": "subagent-secret"},
            },
            parent_tool_use_id="t_sub",
        ),
        StreamEvent(
            uuid="u3",
            session_id="s1",
            event={"type": "content_block_stop", "index": 0},
            parent_tool_use_id="t_sub",
        ),
    ]
    script = [
        *subagent_events,
        *_text_block_stream(1, "main-content"),
        _result_message(),
    ]
    original = _install_script(script)
    try:
        events = await _post_message_events(client, sid, "go")
    finally:
        _restore_init(original)

    # No SSE event should contain the subagent text.
    all_texts = [p.get("text", "") for _, p in events]
    assert not any("subagent-secret" in t for t in all_texts)

    # Main-thread delta must still appear.
    delta_texts = [p.get("text", "") for t, p in events if t == "assistant.text.delta"]
    assert "main-content" in delta_texts


# 20 -----------------------------------------------------------------------
async def test_streaming_atomic_fallback_when_streaming_off(
    tmp_path, monkeypatch, patch_sdk
):
    monkeypatch.setenv("DA_AGENT_HOME", str(tmp_path))
    s = Settings()
    s.data_root = tmp_path
    s.stream_responses = False
    s.ensure_dirs()
    a = create_app(s)
    async with a.router.lifespan_context(a):
        transport = httpx.ASGITransport(app=a)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
            create = await c.post("/sessions", json={"name": "s20"})
            sid = create.json()["id"]

            script = [
                _assistant(TextBlock(text="hi")),
                _result_message(),
            ]
            original = _install_script(script)
            try:
                events = await _post_message_events(c, sid, "go")
            finally:
                _restore_init(original)

    event_types = [t for t, _ in events]
    assert "assistant.text" in event_types, (
        "Atomic assistant.text must appear when streaming is off"
    )
    assert "assistant.text.delta" not in event_types, (
        "No delta events when streaming is off"
    )


# 21 -----------------------------------------------------------------------
async def test_streaming_suppresses_atomic_text_when_streamed(app, client):
    create = await client.post("/sessions", json={"name": "s21"})
    sid = create.json()["id"]

    script = [
        *_text_block_stream(0, "streamed-content"),
        _assistant(TextBlock(text="should-not-emit")),
        _result_message(),
    ]
    original = _install_script(script)
    try:
        events = await _post_message_events(client, sid, "go")
    finally:
        _restore_init(original)

    event_types = [t for t, _ in events]
    delta_events = [p for t, p in events if t == "assistant.text.delta"]
    end_events = [p for t, p in events if t == "assistant.text.end"]

    assert len(delta_events) == 1
    assert len(end_events) == 1
    assert "assistant.text" not in event_types, (
        "Atomic assistant.text must be suppressed when streamed"
    )

    # Verify the delta carried the streamed text.
    assert delta_events[0]["text"] == "streamed-content"


# 22 -----------------------------------------------------------------------
async def test_streaming_block_id_format(app, client):
    create = await client.post("/sessions", json={"name": "s22"})
    sid = create.json()["id"]

    script = [
        *_text_block_stream(0, "data"),
        _result_message(),
    ]
    original = _install_script(script)
    try:
        events = await _post_message_events(client, sid, "go")
    finally:
        _restore_init(original)

    delta_events = [p for t, p in events if t == "assistant.text.delta"]
    assert delta_events, "Expected at least one delta event"
    block_id = delta_events[0]["block_id"]
    assert _BLOCK_ID_TEXT_RE.match(block_id), (
        f"block_id {block_id!r} must match ^txt_[0-9a-f]{{12}}$"
    )


# 23 -----------------------------------------------------------------------
async def test_streaming_per_turn_state_resets_between_turns(app, client):
    create = await client.post("/sessions", json={"name": "s23"})
    sid = create.json()["id"]

    def _wait_end_before_first_delta(events):
        types = [t for t, _ in events]
        try:
            first_delta_idx = types.index("assistant.text.delta")
        except ValueError:
            return False
        return any(t == "wait.end" for t in types[:first_delta_idx])

    # Turn 1 — install via __init__ hook; FakeClient is created on first send.
    script_turn1 = [
        *_text_block_stream(0, "turn1"),
        _result_message(),
    ]
    original = _install_script(script_turn1)
    try:
        events1 = await _post_message_events(client, sid, "first")
    finally:
        _restore_init(original)

    # Turn 2 — FakeClient already exists; update its script directly.
    assert FakeClient.instances, (
        "Expected at least one FakeClient instance after turn 1"
    )
    fc = FakeClient.instances[-1]
    fc.script = [
        *_text_block_stream(0, "turn2"),
        _result_message(),
    ]
    events2 = await _post_message_events(client, sid, "second")

    assert _wait_end_before_first_delta(events1), (
        "Turn 1 must emit wait.end before first delta"
    )
    assert _wait_end_before_first_delta(events2), (
        "Turn 2 must emit wait.end before first delta (state reset)"
    )

    deltas1 = [p["text"] for t, p in events1 if t == "assistant.text.delta"]
    deltas2 = [p["text"] for t, p in events2 if t == "assistant.text.delta"]
    assert "turn1" in deltas1
    assert "turn2" in deltas2


# --------------------------------------------------------------------------- #
# F. KB registry is wired into AppState
# --------------------------------------------------------------------------- #
async def test_app_state_has_kb_registry(app):
    state = app.state.app_state
    assert state.kb is not None
    files = await state.kb.list()
    assert files == []  # fresh registry
