"""Tests for the rich-Live overlay (spinner + todo checklist) in ConsoleAgentUI."""

from __future__ import annotations

import io

from rich.console import Console

from da_agent.agent.events import TodoItem, TodoSnapshot, TodoStatus
from da_agent.ui.console import ConsoleAgentUI, _render_todo_list


def _ui() -> ConsoleAgentUI:
    """Build a UI backed by a captured non-terminal console (no live animation)."""
    buf = io.StringIO()
    return ConsoleAgentUI(
        Console(file=buf, force_terminal=False, width=80, record=True)
    )


def _snapshot(*statuses: TodoStatus) -> TodoSnapshot:
    return TodoSnapshot(
        items=[
            TodoItem(
                task_id=f"T{i}",
                subject=f"task {i}",
                active_form=f"running {i}",
                status=s,
            )
            for i, s in enumerate(statuses)
        ]
    )


# --------------------------------------------------------------------------- #
def test_render_todo_list_uses_active_form_for_in_progress():
    snap = _snapshot(TodoStatus.COMPLETED, TodoStatus.IN_PROGRESS, TodoStatus.PENDING)
    block = _render_todo_list(snap)
    plain = block.plain
    # Completed shows subject; in_progress shows the active_form; pending shows subject.
    assert "task 0" in plain
    assert "running 1" in plain
    assert "task 2" in plain
    # Glyphs reflect status.
    assert "✔" in plain
    assert "▪" in plain
    assert "□" in plain
    # First row gets the corner branch, subsequent rows are flush-indented.
    lines = plain.splitlines()
    assert lines[0].startswith("  └ ")
    assert lines[1].startswith("    ")


def test_overlay_starts_when_wait_label_set_and_stops_on_end_wait():
    ui = _ui()
    assert ui._live is None
    ui.begin_wait("Thinking")
    assert ui._live is not None  # overlay started
    ui.end_wait()
    assert ui._live is None  # no todos and no label -> overlay stopped


def test_overlay_persists_when_only_todos_remain():
    """Clearing the wait label does NOT stop the overlay if todos are still active."""
    ui = _ui()
    ui.on_todos(_snapshot(TodoStatus.IN_PROGRESS, TodoStatus.PENDING))
    assert ui._live is not None
    ui.begin_wait("Working")
    assert ui._live is not None
    ui.end_wait()
    # Label cleared, but todos still present -> overlay alive.
    assert ui._live is not None
    ui.on_todos(TodoSnapshot())  # empty
    assert ui._live is None


def test_overlay_label_replaced_by_active_todo():
    """When a todo is in_progress, the spinner line shows its active_form."""
    ui = _ui()
    ui.on_todos(_snapshot(TodoStatus.IN_PROGRESS, TodoStatus.PENDING))
    ui.begin_wait("Thinking")
    overlay = ui._build_overlay()
    # Group of (Spinner, Text) — peek at the spinner's text element.
    assert overlay is not None
    spinner = overlay.renderables[0]  # type: ignore[attr-defined]
    label_text = spinner.text.plain  # type: ignore[attr-defined]
    assert "running 0" in label_text
    ui.end_wait()
    ui.on_todos(TodoSnapshot())


def test_on_todos_drops_overlay_when_snapshot_emptied():
    ui = _ui()
    ui.on_todos(_snapshot(TodoStatus.PENDING))
    assert ui._live is not None
    ui.on_todos(TodoSnapshot())
    assert ui._live is None
