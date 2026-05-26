"""Console rendering with `rich`.

`ConsoleAgentUI` implements the full `AgentUI` protocol. Rendering uses rich; the
interactive bits (questions, plan approval) delegate to `ui.prompts` (prompt_toolkit).

A single `rich.live.Live` region owns the bottom of the terminal whenever a wait
indicator OR a todo list is active. Streaming text/tool prints scroll above it;
the overlay redraws as state changes.
"""

from __future__ import annotations

import json
from typing import Any

from rich.console import Console, Group
from rich.live import Live
from rich.spinner import Spinner
from rich.text import Text

from ..agent.events import (
    PlanDecision,
    QuestionRequest,
    QuestionResponse,
    TodoSnapshot,
    TodoStatus,
)
from . import prompts as interactive

_RESULT_MAX_LINES = 14
_THINKING_MAX_CHARS = 600
_GREEN = "#3fb950"
_DIM = "grey62"
_ACTIVE = "bold"
_DONE = "green"

_BOX_PENDING = "□"
_BOX_ACTIVE = "▪"
_BOX_DONE = "✔"


class ConsoleAgentUI:
    def __init__(self, console: Console | None = None) -> None:
        self.console = console or Console()
        self._live: Live | None = None
        self._wait_label: str = ""
        self._todos: TodoSnapshot = TodoSnapshot()

    # ------------------------------------------------------------------ #
    # waiting indicator
    # ------------------------------------------------------------------ #
    def begin_wait(self, label: str = "Working") -> None:
        self._wait_label = label
        self._refresh_overlay()

    def end_wait(self) -> None:
        self._wait_label = ""
        self._refresh_overlay()

    # ------------------------------------------------------------------ #
    # render
    # ------------------------------------------------------------------ #
    def on_user_prompt(self, text: str) -> None:
        self.end_wait()
        self.console.print()
        self.console.print(Text("› ", style="bold") + Text(text, style="bold white"))

    def on_thinking(self, text: str) -> None:
        self.end_wait()
        snippet = text.strip()
        if len(snippet) > _THINKING_MAX_CHARS:
            snippet = snippet[:_THINKING_MAX_CHARS].rstrip() + " …"
        self.console.print()
        self.console.print(Text("✻ Thinking", style=f"italic {_DIM}"))
        for line in snippet.splitlines():
            self.console.print(Text("  " + line, style=f"italic {_DIM}"))

    def on_text(self, text: str) -> None:
        self.end_wait()
        self.console.print()
        self.console.print(Text(text.strip()))

    def on_tool_use(
        self, name: str, tool_input: dict[str, Any], *, depth: int = 0
    ) -> None:
        self.end_wait()
        pad = "  " * depth
        head = Text(f"{pad}● ", style=_GREEN) + Text(name, style="bold")
        arg = _format_args(name, tool_input)
        if arg:
            head += Text(f"({arg})", style=_DIM)
        self.console.print(head)

    def on_tool_result(
        self, summary: str, *, is_error: bool = False, depth: int = 0
    ) -> None:
        self.end_wait()
        pad = "  " * depth
        lines = summary.rstrip().splitlines() or [""]
        shown, extra = lines[:_RESULT_MAX_LINES], len(lines) - _RESULT_MAX_LINES
        style = "red" if is_error else _DIM
        for i, line in enumerate(shown):
            prefix = f"{pad}  ⎿  " if i == 0 else f"{pad}     "
            self.console.print(Text(prefix, style=_DIM) + Text(line, style=style))
        if extra > 0:
            self.console.print(Text(f"{pad}     … +{extra} more lines", style=_DIM))

    def on_system(self, subtype: str, data: dict[str, Any]) -> None:
        if subtype == "init":
            model = data.get("model", "")
            note = f"session ready · {model}" if model else "session ready"
            self.console.print(Text(f"· {note}", style=_DIM))

    def on_result(
        self, *, turns: int, cost_usd: float | None, duration_s: float
    ) -> None:
        self.end_wait()
        bits = [
            f"done in {duration_s:.1f}s",
            f"{turns} turn{'s' if turns != 1 else ''}",
        ]
        if cost_usd:
            bits.append(f"${cost_usd:.4f}")
        self.console.print()
        self.console.print(Text("✶ " + " · ".join(bits), style=_DIM))

    def on_error(self, message: str) -> None:
        self.end_wait()
        self.console.print(Text(f"✗ {message}", style="bold red"))

    def on_todos(self, snapshot: TodoSnapshot) -> None:
        self._todos = snapshot
        self._refresh_overlay()

    # ------------------------------------------------------------------ #
    # interaction
    # ------------------------------------------------------------------ #
    async def ask_question(self, request: QuestionRequest) -> QuestionResponse:
        self.end_wait()
        return await interactive.run_question_selector(request)

    async def approve_plan(self, plan: str) -> PlanDecision:
        self.end_wait()
        self._render_plan(plan)
        return await interactive.confirm_plan()

    def _render_plan(self, plan: str) -> None:
        self.console.print()
        self.console.print(Text("▌ Plan", style="bold"))
        for line in plan.strip().splitlines():
            self.console.print(Text("  " + line))

    # ------------------------------------------------------------------ #
    # live overlay
    # ------------------------------------------------------------------ #
    def _refresh_overlay(self) -> None:
        renderable = self._build_overlay()
        if renderable is None:
            self._stop_live()
            return
        if self._live is None:
            self._live = Live(
                renderable,
                console=self.console,
                refresh_per_second=12,
                transient=True,
            )
            self._live.start()
        else:
            self._live.update(renderable)

    def _stop_live(self) -> None:
        if self._live is not None:
            self._live.stop()
            self._live = None

    def _build_overlay(self):
        has_label = bool(self._wait_label)
        has_todos = bool(self._todos)
        if not has_label and not has_todos:
            return None

        rows: list[Any] = []
        if has_label:
            label = self._wait_label
            active = self._todos.in_progress if has_todos else None
            if active is not None:
                label = active.display_text
            rows.append(Spinner("dots", text=Text(f" {label}…", style=_DIM)))
        if has_todos:
            rows.append(_render_todo_list(self._todos))
        return Group(*rows) if len(rows) > 1 else rows[0]


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _render_todo_list(snapshot: TodoSnapshot) -> Text:
    """Format the snapshot as a checkbox-style block (in_progress in bold)."""
    block = Text()
    for i, item in enumerate(snapshot.items):
        prefix = "  └ " if i == 0 else "    "
        if item.status is TodoStatus.IN_PROGRESS:
            box, style = _BOX_ACTIVE, _ACTIVE
        elif item.status is TodoStatus.COMPLETED:
            box, style = _BOX_DONE, _DONE
        else:
            box, style = _BOX_PENDING, _DIM
        line = Text(prefix, style=_DIM) + Text(
            f"{box} {item.display_text}", style=style
        )
        block.append_text(line)
        if i < len(snapshot.items) - 1:
            block.append("\n")
    return block


def _format_args(name: str, ti: dict[str, Any]) -> str:
    if not ti:
        return ""
    if name == "Bash":
        return _truncate(str(ti.get("command", "")), 90)
    if name in {"Read", "Write", "Edit", "NotebookEdit"}:
        return _truncate(str(ti.get("file_path", "")), 90)
    if name in {"Glob", "Grep"}:
        return _truncate(str(ti.get("pattern", "")), 90)
    if name == "Task":
        sub = ti.get("subagent_type", "")
        desc = ti.get("description", "")
        return _truncate(f"{sub}: {desc}".strip(": "), 90)
    if name == "Skill":
        return _truncate(str(ti.get("name", "")), 60)
    try:
        return _truncate(json.dumps(ti, ensure_ascii=False), 90)
    except (TypeError, ValueError):
        return _truncate(str(ti), 90)


def _truncate(s: str, n: int) -> str:
    s = " ".join(s.split())
    return s if len(s) <= n else s[: n - 1] + "…"
