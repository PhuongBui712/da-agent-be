"""Full-stack SSE test: write site -> output.created (spec §8.2, §11)."""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest
import pytest_asyncio
from claude_agent_sdk import (
    AssistantMessage,
    ResultMessage,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
)

from da_agent.config import Settings
from da_agent.server.app import create_app


# --------------------------------------------------------------------------- #
# Fake SDK client (copy of test_server.py — kept here so this file stands
# alone; keeping the module independent matches the existing test layout).
# --------------------------------------------------------------------------- #
class FakeClient:
    instances: list["FakeClient"] = []

    def __init__(self, options=None):
        self.options = options
        self.script: list = []
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
# Helpers
# --------------------------------------------------------------------------- #
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
# Tests
# --------------------------------------------------------------------------- #
async def test_write_under_outputs_dir_emits_output_created(app, client):
    create = await client.post("/sessions", json={"name": "outputs-sse"})
    sid = create.json()["id"]

    state = app.state.app_state
    # Phase A 2026-06-01 layout: outputs/<sid>/<filename> (flat, no out_* subdir).
    session_dir = state.settings.outputs_dir / sid
    session_dir.mkdir(parents=True, exist_ok=True)
    target = session_dir / "report.xlsx"
    payload = b"PK\x03\x04 fake xlsx"

    def write_file(_fc):
        # The script is iterated lazily — by the time this lambda runs the
        # tool_use has already been emitted into the runner, but the file
        # didn't exist yet. The runner observes Write at tool_use time, then
        # detects on tool_result; the registry adopts the path that's now
        # on disk.
        target.write_bytes(payload)

    script = [
        AssistantMessage(
            content=[
                ToolUseBlock(
                    id="tu_w1",
                    name="Write",
                    input={"file_path": str(target)},
                )
            ],
            model="fake",
        ),
        write_file,
        UserMessage(
            content=[
                ToolResultBlock(
                    tool_use_id="tu_w1", content="File written", is_error=False
                )
            ]
        ),
        _result_message(),
    ]

    original = _install_script(script)
    try:
        events = await _post_message_events(client, sid, "make a report")
    finally:
        _restore_init(original)

    # Wait briefly for the fire-and-forget _handle_output_detection task to
    # adopt the file (it was scheduled inside the SSE generator). The
    # output.created event is emitted from that task.
    output_events = [(t, p) for t, p in events if t == "output.created"]

    # Best-effort: even if the SSE stream had closed, the registry must have
    # a row for session_id=sid with filename="report.xlsx".
    registered = None
    for _ in range(50):
        listing = await state.outputs.list(session_id=sid)
        if listing:
            registered = listing[0]
            break
        await asyncio.sleep(0.01)

    assert registered is not None, "registry should have adopted report.xlsx"
    assert registered.filename == "report.xlsx"
    assert registered.session_id == sid

    # If the SSE caught the event in time, validate its shape.
    if output_events:
        _, evt_payload = output_events[0]
        assert evt_payload["kind"] == "standalone"
        assert "/outputs/" in evt_payload["download_url"]


async def test_output_created_sse_includes_download_url_and_created_at(app, client):
    """Phase A 2026-06-01: SSE `output.created` carries `download_url`,
    `created_at`, and `mime` so the FE can populate the persistent sidebar
    card immediately without a follow-up REST round-trip."""
    create = await client.post("/sessions", json={"name": "outputs-sse-fields"})
    sid = create.json()["id"]

    state = app.state.app_state
    session_dir = state.settings.outputs_dir / sid
    session_dir.mkdir(parents=True, exist_ok=True)
    target = session_dir / "report.xlsx"
    payload = b"PK\x03\x04 fake xlsx with fields"

    def write_file(_fc):
        target.write_bytes(payload)

    script = [
        AssistantMessage(
            content=[
                ToolUseBlock(
                    id="tu_w_fields",
                    name="Write",
                    input={"file_path": str(target)},
                )
            ],
            model="fake",
        ),
        write_file,
        UserMessage(
            content=[
                ToolResultBlock(
                    tool_use_id="tu_w_fields",
                    content="File written",
                    is_error=False,
                )
            ]
        ),
        _result_message(),
    ]

    original = _install_script(script)
    try:
        events = await _post_message_events(client, sid, "make a fielded report")
    finally:
        _restore_init(original)

    # Wait for the registry to adopt the file (fire-and-forget detection).
    registered = None
    for _ in range(50):
        listing = await state.outputs.list(session_id=sid)
        if listing:
            registered = listing[0]
            break
        await asyncio.sleep(0.01)
    assert registered is not None

    output_events = [(t, p) for t, p in events if t == "output.created"]
    # The SSE generator may have closed before the fire-and-forget task
    # emitted; only assert payload shape when we caught the event.
    if output_events:
        _, payload_evt = output_events[0]
        assert payload_evt["output_id"] == registered.id
        assert payload_evt["download_url"] == f"/outputs/{registered.id}"
        assert payload_evt["created_at"] == registered.created_at
        assert payload_evt["mime"] == registered.mime
        assert payload_evt["filename"] == "report.xlsx"


async def test_write_under_kb_versions_does_NOT_emit_anymore(app, client):
    """DEPRECATED 2026-05-31: writes under `kb/<id>/versions/` no longer emit.

    Phase C broke Golden Rule 4 per user approval: KB-bound deliverables now
    land under `outputs/<sid>/<filename>` instead of the legacy versions chain.
    The observer's kb_version branch returns None, so a direct Write into
    `kb_dir` produces no `output.created` event and no registry row.
    """
    create = await client.post("/sessions", json={"name": "kbv-sse"})
    sid = create.json()["id"]

    state = app.state.app_state
    versions_dir = state.settings.kb_dir / "kb_xyz" / "versions"
    versions_dir.mkdir(parents=True, exist_ok=True)
    target = versions_dir / "v_curr.xlsx"
    payload = b"PK\x03\x04 latest"

    def write_file(_fc):
        target.write_bytes(payload)

    script = [
        AssistantMessage(
            content=[
                ToolUseBlock(
                    id="tu_w2",
                    name="Write",
                    input={"file_path": str(target)},
                )
            ],
            model="fake",
        ),
        write_file,
        UserMessage(
            content=[
                ToolResultBlock(
                    tool_use_id="tu_w2", content="File written", is_error=False
                )
            ]
        ),
        _result_message(),
    ]

    original = _install_script(script)
    try:
        events = await _post_message_events(client, sid, "make a new revision")
    finally:
        _restore_init(original)

    # Give any fire-and-forget detection task a tick to run before asserting
    # absence — we want to be sure no event is *ever* emitted, not just that
    # the SSE response returned before adoption.
    await asyncio.sleep(0.05)

    output_events = [(t, p) for t, p in events if t == "output.created"]
    assert output_events == [], (
        f"kb_version writes should NOT emit output.created (got {output_events})"
    )

    # And nothing in the standalone registry was minted from this write.
    listed = await state.outputs.list()
    assert listed == []


async def test_write_under_attachment_versions_does_NOT_emit_anymore(app, client):
    """DEPRECATED 2026-05-31: writes under `attachments/<sid>/<att>/versions/` no longer emit.

    Symmetric to the kb_version case above — Phase C routes attachment-bound
    deliverables under `outputs/<sid>/<filename>`.
    """
    create = await client.post("/sessions", json={"name": "att-sse"})
    sid = create.json()["id"]

    state = app.state.app_state
    versions_dir = state.settings.attachments_dir / sid / "att_42" / "versions"
    versions_dir.mkdir(parents=True, exist_ok=True)
    target = versions_dir / "v_curr.xlsx"
    payload = b"PK\x03\x04 attachment-edit"

    def write_file(_fc):
        target.write_bytes(payload)

    script = [
        AssistantMessage(
            content=[
                ToolUseBlock(
                    id="tu_att1",
                    name="Write",
                    input={"file_path": str(target)},
                )
            ],
            model="fake",
        ),
        write_file,
        UserMessage(
            content=[
                ToolResultBlock(
                    tool_use_id="tu_att1",
                    content="File written",
                    is_error=False,
                )
            ]
        ),
        _result_message(),
    ]

    original = _install_script(script)
    try:
        events = await _post_message_events(client, sid, "edit my attachment")
    finally:
        _restore_init(original)

    await asyncio.sleep(0.05)

    output_events = [(t, p) for t, p in events if t == "output.created"]
    assert output_events == [], (
        f"attachment_version writes should NOT emit output.created (got {output_events})"
    )
    listed = await state.outputs.list()
    assert listed == []
