"""Tests for `TodoStore` (Task* + legacy TodoWrite ingestion)."""

from __future__ import annotations

import json

import pytest
from claude_agent_sdk import ToolResultBlock, ToolUseBlock

from da_agent.agent.core import AgentRunner
from da_agent.agent.events import TodoSnapshot, TodoStatus
from da_agent.agent.todos import TODO_TOOL_NAMES, TodoStore
from da_agent.config import Settings


# --------------------------------------------------------------------------- #
# Pure store unit tests
# --------------------------------------------------------------------------- #
def test_tool_names_cover_both_modalities():
    assert {
        "TodoWrite",
        "TaskCreate",
        "TaskUpdate",
        "TaskList",
        "TaskGet",
    } <= TODO_TOOL_NAMES


def test_taskcreate_pending_until_result_lands():
    store = TodoStore()
    changed = store.observe_tool_use(
        "tu1",
        "TaskCreate",
        {"subject": "Profile sheets", "activeForm": "Profiling sheets"},
    )
    # Snapshot does not change until the matching tool_result arrives with the assigned id
    assert changed is False
    assert len(store.snapshot()) == 0
    assert store.is_pending("tu1") is True

    changed = store.observe_tool_result(
        "tu1", json.dumps({"task": {"id": "T-001", "subject": "Profile sheets"}})
    )
    assert changed is True
    snap = store.snapshot()
    assert len(snap) == 1
    item = snap.items[0]
    assert item.task_id == "T-001"
    assert item.subject == "Profile sheets"
    assert item.active_form == "Profiling sheets"
    assert item.status is TodoStatus.PENDING


def test_taskupdate_changes_status_and_subject():
    store = TodoStore()
    store.observe_tool_use(
        "tu1", "TaskCreate", {"subject": "Aggregate", "activeForm": "Aggregating"}
    )
    store.observe_tool_result("tu1", '{"task": {"id": "T-1"}}')

    assert (
        store.observe_tool_use(
            "tu2", "TaskUpdate", {"taskId": "T-1", "status": "in_progress"}
        )
        is True
    )
    assert store.snapshot().items[0].status is TodoStatus.IN_PROGRESS

    # Subject can be patched on update; idempotent updates report no change.
    assert (
        store.observe_tool_use(
            "tu3", "TaskUpdate", {"taskId": "T-1", "subject": "Aggregate v2"}
        )
        is True
    )
    assert store.snapshot().items[0].subject == "Aggregate v2"
    assert (
        store.observe_tool_use(
            "tu4", "TaskUpdate", {"taskId": "T-1", "subject": "Aggregate v2"}
        )
        is False
    )


def test_taskupdate_status_deleted_removes_item():
    store = TodoStore()
    store.observe_tool_use("tu1", "TaskCreate", {"subject": "A"})
    store.observe_tool_result("tu1", '{"task": {"id": "T-A"}}')
    store.observe_tool_use("tu2", "TaskCreate", {"subject": "B"})
    store.observe_tool_result("tu2", '{"task": {"id": "T-B"}}')

    assert [i.task_id for i in store.snapshot()] == ["T-A", "T-B"]
    assert (
        store.observe_tool_use(
            "tu3", "TaskUpdate", {"taskId": "T-A", "status": "deleted"}
        )
        is True
    )
    assert [i.task_id for i in store.snapshot()] == ["T-B"]


def test_taskupdate_before_create_synthesises_entry():
    """The runner should never drop a TaskUpdate that arrives before its TaskCreate."""
    store = TodoStore()
    changed = store.observe_tool_use(
        "tu1",
        "TaskUpdate",
        {"taskId": "T-X", "subject": "Mystery", "status": "in_progress"},
    )
    assert changed is True
    snap = store.snapshot()
    assert len(snap) == 1
    assert snap.items[0].subject == "Mystery"
    assert snap.items[0].status is TodoStatus.IN_PROGRESS


def test_unknown_status_is_ignored_not_crashed():
    store = TodoStore()
    store.observe_tool_use("tu1", "TaskCreate", {"subject": "A"})
    store.observe_tool_result("tu1", '{"task": {"id": "T-A"}}')
    # Bogus status string is dropped, not raised.
    store.observe_tool_use("tu2", "TaskUpdate", {"taskId": "T-A", "status": "bogus"})
    assert store.snapshot().items[0].status is TodoStatus.PENDING


def test_taskcreate_without_id_in_result_uses_local_fallback():
    store = TodoStore()
    store.observe_tool_use("tu1", "TaskCreate", {"subject": "Fallback"})
    store.observe_tool_result("tu1", "no json here")
    snap = store.snapshot()
    assert len(snap) == 1
    assert snap.items[0].task_id.startswith("local-")


def test_taskcreate_parses_real_sdk_plain_string():
    """Real SDK content is `Task #<id> created successfully: <subject>`, not JSON.

    Regression for the bug where _extract_task_id only handled the JSON shape: every
    TaskCreate fell back to a `local-…` id, so the matching TaskUpdate (which carries
    the SDK id like "1") could not find its row and synthesised a "(unknown)" entry.
    """
    store = TodoStore()
    store.observe_tool_use(
        "tu1",
        "TaskCreate",
        {"subject": "Profile sheets", "activeForm": "Profiling sheets"},
    )
    changed = store.observe_tool_result(
        "tu1", "Task #1 created successfully: Profile sheets"
    )
    assert changed is True
    snap = store.snapshot()
    assert len(snap) == 1
    assert snap.items[0].task_id == "1"
    assert snap.items[0].subject == "Profile sheets"


def test_taskcreate_then_update_updates_same_row_no_unknown():
    """End-to-end of the user-reported bug: 5 creates + 5 updates must yield 5 rows."""
    store = TodoStore()
    creates = [
        ("tu_c1", "1", "Load and profile sales dataset"),
        ("tu_c2", "2", "Clean and deduplicate records"),
        ("tu_c3", "3", "Build monthly revenue pivot table"),
        ("tu_c4", "4", "Generate revenue trend chart"),
        ("tu_c5", "5", "Export final report to workspace"),
    ]
    for use_id, task_id, subject in creates:
        store.observe_tool_use(use_id, "TaskCreate", {"subject": subject})
        store.observe_tool_result(
            use_id, f"Task #{task_id} created successfully: {subject}"
        )

    # Now update three of them, exactly as the model did in the screenshot.
    store.observe_tool_use(
        "tu_u1", "TaskUpdate", {"taskId": "1", "status": "completed"}
    )
    store.observe_tool_use(
        "tu_u2", "TaskUpdate", {"taskId": "2", "status": "completed"}
    )
    store.observe_tool_use(
        "tu_u3", "TaskUpdate", {"taskId": "3", "status": "in_progress"}
    )

    snap = store.snapshot()
    # Five rows total, NOT five creates + three "(unknown)" synthesised rows.
    assert [i.task_id for i in snap] == ["1", "2", "3", "4", "5"]
    assert [i.status for i in snap] == [
        TodoStatus.COMPLETED,
        TodoStatus.COMPLETED,
        TodoStatus.IN_PROGRESS,
        TodoStatus.PENDING,
        TodoStatus.PENDING,
    ]
    assert all(i.subject != "(unknown)" for i in snap)


def test_taskcreate_legacy_json_format_still_parsed():
    """Belt-and-braces: callers that wrap the result in JSON keep working."""
    store = TodoStore()
    store.observe_tool_use("tu1", "TaskCreate", {"subject": "Legacy"})
    store.observe_tool_result("tu1", '{"task": {"id": "T-7", "subject": "Legacy"}}')
    assert store.snapshot().items[0].task_id == "T-7"


def test_extract_task_id_alphanumeric_id_supported():
    """Task ids may be longer than a digit (e.g. opaque slugs)."""
    from da_agent.agent.todos import _extract_task_id

    assert _extract_task_id("Task #abc-123 created successfully: Whatever") == "abc-123"
    assert _extract_task_id("Task #42 created successfully: x") == "42"
    assert _extract_task_id("nothing here") is None


def test_legacy_todowrite_path():
    store = TodoStore()
    todos = [
        {
            "content": "Set up project structure",
            "status": "in_progress",
            "activeForm": "Setting up project structure",
        },
        {
            "content": "Implement core feature",
            "status": "pending",
            "activeForm": "Implementing core feature",
        },
        {
            "content": "Write unit tests",
            "status": "pending",
            "activeForm": "Writing unit tests",
        },
    ]
    changed = store.observe_tool_use("tu1", "TodoWrite", {"todos": todos})
    assert changed is True
    snap = store.snapshot()
    assert [i.subject for i in snap] == [t["content"] for t in todos]
    assert snap.items[0].status is TodoStatus.IN_PROGRESS
    assert snap.items[1].status is TodoStatus.PENDING

    # Re-applying the same array is a no-op.
    assert store.observe_tool_use("tu2", "TodoWrite", {"todos": todos}) is False


def test_snapshot_summary_helpers():
    store = TodoStore()
    store._apply_todowrite(
        {
            "todos": [
                {"content": "a", "status": "completed"},
                {"content": "b", "status": "in_progress"},
                {"content": "c", "status": "pending"},
                {"content": "d", "status": "pending"},
            ]
        }
    )
    snap = store.snapshot()
    assert bool(snap) and len(snap) == 4
    assert snap.in_progress.subject == "b"
    counts = snap.counts()
    assert counts == {"pending": 2, "in_progress": 1, "completed": 1}


def test_reset_clears_pending_and_state():
    store = TodoStore()
    store.observe_tool_use("tu1", "TaskCreate", {"subject": "leftover"})
    assert store.is_pending("tu1") is True
    store.reset()
    assert store.is_pending("tu1") is False
    assert len(store.snapshot()) == 0


def test_todoitem_display_text_uses_active_form_when_running():
    store = TodoStore()
    store.observe_tool_use(
        "tu1",
        "TaskCreate",
        {"subject": "Profile sheets", "activeForm": "Profiling sheets"},
    )
    store.observe_tool_result("tu1", '{"task": {"id": "T1"}}')
    item = store.snapshot().items[0]
    assert item.display_text == "Profile sheets"
    store.observe_tool_use(
        "tu2", "TaskUpdate", {"taskId": "T1", "status": "in_progress"}
    )
    assert store.snapshot().items[0].display_text == "Profiling sheets"


# --------------------------------------------------------------------------- #
# AgentRunner integration: tool_use / tool_result blocks drive the snapshot
# --------------------------------------------------------------------------- #
class _FakeUI:
    def __init__(self):
        self.snapshots: list[TodoSnapshot] = []

    def on_user_prompt(self, t):
        pass

    def on_thinking(self, t):
        pass

    def on_text(self, t):
        pass

    def on_tool_use(self, n, i, *, depth=0, tool_use_id=None):
        pass

    def on_tool_result(self, s, *, is_error=False, depth=0, tool_use_id=None):
        pass

    def on_system(self, st, d):
        pass

    def on_result(self, *, turns, cost_usd, duration_s):
        pass

    def on_error(self, m):
        pass

    def on_todos(self, snapshot):
        self.snapshots.append(snapshot)

    def begin_wait(self, label="Working"):
        pass

    def end_wait(self):
        pass

    async def ask_question(self, request): ...
    async def approve_plan(self, plan): ...


@pytest.fixture
def runner():
    ui = _FakeUI()
    return AgentRunner(ui, Settings()), ui


def test_runner_routes_taskcreate_through_store(runner):
    r, ui = runner
    r._absorb_todo_tool_use(
        ToolUseBlock(
            id="tu1", name="TaskCreate", input={"subject": "S1", "activeForm": "AF1"}
        )
    )
    # No snapshot pushed yet — TaskCreate state changes only when its result lands.
    assert ui.snapshots == []
    r._absorb_todo_tool_result(
        ToolResultBlock(
            tool_use_id="tu1", content='{"task": {"id": "T1"}}', is_error=False
        )
    )
    assert len(ui.snapshots) == 1
    assert ui.snapshots[-1].items[0].task_id == "T1"


def test_runner_routes_taskupdate_immediately(runner):
    r, ui = runner
    r._absorb_todo_tool_use(
        ToolUseBlock(id="tu1", name="TaskCreate", input={"subject": "X"})
    )
    r._absorb_todo_tool_result(
        ToolResultBlock(
            tool_use_id="tu1", content='{"task": {"id": "TX"}}', is_error=False
        )
    )
    pre = len(ui.snapshots)
    r._absorb_todo_tool_use(
        ToolUseBlock(
            id="tu2", name="TaskUpdate", input={"taskId": "TX", "status": "in_progress"}
        )
    )
    assert len(ui.snapshots) == pre + 1
    assert ui.snapshots[-1].items[0].status is TodoStatus.IN_PROGRESS


def test_runner_filters_todo_tools_from_normal_renderer(runner):
    """`_render_block` for a Task* tool must NOT call `on_tool_use`."""
    r, ui = runner
    block = ToolUseBlock(
        id="tu1", name="TaskUpdate", input={"taskId": "T", "status": "completed"}
    )
    r._render_block(block, depth=0)
    # No TaskCreate was registered, so this update synthesises a new row.
    assert ui.snapshots and ui.snapshots[-1].items[0].task_id == "T"


def test_runner_handles_real_sdk_create_then_update_no_unknown(runner):
    """Reproduce the screenshot bug at runner level using real SDK content strings.

    Five TaskCreate blocks with plain-string results, followed by three TaskUpdate
    blocks. The final snapshot must contain exactly five rows -- no `(unknown)` rows
    spawned by the synthesise-on-update fallback.
    """
    r, ui = runner
    rows = [
        ("tu_c1", "1", "Load and profile sales dataset"),
        ("tu_c2", "2", "Clean and deduplicate records"),
        ("tu_c3", "3", "Build monthly revenue pivot table"),
        ("tu_c4", "4", "Generate revenue trend chart"),
        ("tu_c5", "5", "Export final report to workspace"),
    ]
    for use_id, task_id, subject in rows:
        r._absorb_todo_tool_use(
            ToolUseBlock(id=use_id, name="TaskCreate", input={"subject": subject})
        )
        r._absorb_todo_tool_result(
            ToolResultBlock(
                tool_use_id=use_id,
                content=f"Task #{task_id} created successfully: {subject}",
                is_error=False,
            )
        )
    r._absorb_todo_tool_use(
        ToolUseBlock(
            id="tu_u1", name="TaskUpdate", input={"taskId": "1", "status": "completed"}
        )
    )
    r._absorb_todo_tool_use(
        ToolUseBlock(
            id="tu_u2",
            name="TaskUpdate",
            input={"taskId": "3", "status": "in_progress"},
        )
    )

    final = ui.snapshots[-1]
    assert [i.task_id for i in final] == ["1", "2", "3", "4", "5"]
    assert all(i.subject != "(unknown)" for i in final)
    assert final.items[0].status is TodoStatus.COMPLETED
    assert final.items[2].status is TodoStatus.IN_PROGRESS
    assert final.items[4].status is TodoStatus.PENDING


def test_runner_send_orders_overlay_calls_to_avoid_flicker():
    """`send` must invoke begin_wait BEFORE pushing the empty todo snapshot.

    Otherwise the rich-Live overlay collapses (no label + no todos) and immediately
    re-mounts, producing a one-frame flicker between turns.
    """
    events: list[str] = []

    class _OrderUI(_FakeUI):
        def begin_wait(self, label="Working"):
            events.append("begin_wait")

        def end_wait(self):
            events.append("end_wait")

        def on_todos(self, snapshot):
            events.append("on_todos")
            super().on_todos(snapshot)

        def on_user_prompt(self, t):
            events.append("on_user_prompt")

    class _FakeClient:
        async def query(self, *_a, **_kw):
            pass

        async def receive_response(self):
            if False:
                yield None  # empty async iterator
            return

    import asyncio

    ui = _OrderUI()
    r = AgentRunner(ui, Settings())
    r._client = _FakeClient()  # bypass the connect() lifecycle for a focused unit test
    asyncio.run(r.send("hi", echo_prompt=False))

    # The first todo snapshot push must come strictly AFTER begin_wait so the live
    # overlay never observes (no label AND no todos) on the turn boundary.
    first_todos = events.index("on_todos")
    first_begin = events.index("begin_wait")
    assert first_begin < first_todos
