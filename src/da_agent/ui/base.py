"""The UI seam.

`AgentRunner` depends only on this protocol, never on rich/prompt_toolkit. The CLI
provides `ConsoleAgentUI`; a future web backend provides a websocket-backed
implementation of the same methods. That is what makes the agent core reusable.

Rendering methods are fire-and-forget (sync). Interaction methods are awaitable
because they block on a human.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from ..agent.events import PlanDecision, QuestionRequest, QuestionResponse, TodoSnapshot


@runtime_checkable
class AgentUI(Protocol):
    # ---- streaming render (one call per step the user should see) ----
    def on_user_prompt(self, text: str) -> None: ...
    def on_thinking(self, text: str) -> None: ...
    def on_text(self, text: str) -> None: ...

    # ---- token-level streaming. `block_id` is server-minted
    # (`txt_<12hex>` / `thk_<12hex>`); deltas accumulate per block_id and
    # close on `*_end`. Atomic `on_text` / `on_thinking` are NOT emitted
    # for a block that already saw at least one delta. ----
    def on_text_delta(self, block_id: str, delta: str) -> None: ...
    def on_text_end(self, block_id: str) -> None: ...
    def on_thinking_delta(self, block_id: str, delta: str) -> None: ...
    def on_thinking_end(self, block_id: str) -> None: ...
    def on_tool_use(
        self,
        name: str,
        tool_input: dict[str, Any],
        *,
        depth: int = 0,
        tool_use_id: str | None = None,
    ) -> None: ...
    def on_tool_result(
        self,
        summary: str,
        *,
        is_error: bool = False,
        depth: int = 0,
        tool_use_id: str | None = None,
    ) -> None: ...
    def on_system(self, subtype: str, data: dict[str, Any]) -> None: ...
    def on_result(
        self, *, turns: int, cost_usd: float | None, duration_s: float
    ) -> None: ...
    def on_error(self, message: str) -> None: ...

    # ---- output registration. Payload schema is the `output.created` SSE event. ----
    def on_output(self, payload: dict[str, Any]) -> None: ...

    # ---- todo list (snapshot pushed whenever the agent's task list changes) ----
    def on_todos(self, snapshot: TodoSnapshot) -> None: ...

    # ---- waiting indicator ----
    def begin_wait(self, label: str = "Working") -> None: ...
    def end_wait(self) -> None: ...

    # ---- interaction (blocks on the human) ----
    async def ask_question(self, request: QuestionRequest) -> QuestionResponse: ...
    async def approve_plan(self, plan: str) -> PlanDecision: ...
