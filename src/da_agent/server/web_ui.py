"""WebAgentUI — `AgentUI` Protocol implemented by pushing events onto a `TurnStream`.

The streaming render methods are synchronous fire-and-forget (just a `put_nowait`).
The interaction methods (`ask_question`, `approve_plan`) park a `PendingInteraction`
in the `InteractionStore` and await its `Future`; the corresponding REST endpoint
resolves the future when the frontend submits.
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Any, Callable

from ..agent.events import (
    Answer,
    PlanDecision,
    PlanVerdict,
    QuestionRequest,
    QuestionResponse,
    TodoSnapshot,
)
from .state import AppState, PendingInteraction, TurnStream


def _new_tool_use_id() -> str:
    return f"int_{uuid.uuid4().hex[:12]}"


class WebAgentUI:
    """One instance per session. The active turn's stream is set via `attach`."""

    def __init__(
        self,
        *,
        session_id: str,
        app_state: AppState,
        on_sdk_session_id: Callable[[str], None] | None = None,
    ) -> None:
        self.session_id = session_id
        self._app_state = app_state
        self._stream: TurnStream | None = None
        # Per-turn streaming bookkeeping (spec §8.6). `_text_delta_seen`
        # latches True on the first text delta of a turn so we can clear
        # the thinking wait label exactly once.
        self._text_delta_seen: bool = False
        # Invoked once per turn from `on_system("init", data)` with the SDK's
        # minted UUID. Lets the route layer persist it to SessionMeta so we
        # can replay the JSONL transcript on reopen and resume the SDK on
        # subsequent runner connects. None for non-server usages.
        self._on_sdk_session_id = on_sdk_session_id

    # --- stream binding (per turn) ------------------------------------- #
    def attach(self, stream: TurnStream) -> None:
        self._stream = stream
        self._text_delta_seen = False

    def detach(self) -> None:
        self._stream = None
        self._text_delta_seen = False

    def _emit(self, type_: str, **data: Any) -> None:
        stream = self._stream
        if stream is None:
            return
        stream.emit({"type": type_, "session_id": self.session_id, **data})

    # --- streaming render --------------------------------------------- #
    def on_user_prompt(self, text: str) -> None:
        self._emit("user.prompt", text=text)

    def on_thinking(self, text: str) -> None:
        self._emit("assistant.thinking", text=text)

    def on_text(self, text: str) -> None:
        # Atomic fallback path (streaming off OR a full TextBlock that never
        # received a delta). Streaming-on path goes through on_text_delta.
        self._emit("assistant.text", text=text)

    # --- token-level streaming (spec §8.6) ---------------------------- #
    def on_text_delta(self, block_id: str, delta: str) -> None:
        if not self._text_delta_seen:
            # First text delta of the turn -> clear the "Thinking" wait
            # label so the FE caret takes over (spec §8.6).
            self._text_delta_seen = True
            self._emit("wait.end")
        self._emit("assistant.text.delta", block_id=block_id, text=delta)

    def on_text_end(self, block_id: str) -> None:
        self._emit("assistant.text.end", block_id=block_id)

    def on_thinking_delta(self, block_id: str, delta: str) -> None:
        self._emit("assistant.thinking.delta", block_id=block_id, text=delta)

    def on_thinking_end(self, block_id: str) -> None:
        self._emit("assistant.thinking.end", block_id=block_id)

    def on_tool_use(
        self,
        name: str,
        tool_input: dict[str, Any],
        *,
        depth: int = 0,
        tool_use_id: str | None = None,
    ) -> None:
        self._emit(
            "tool.use",
            name=name,
            input=tool_input,
            depth=depth,
            tool_use_id=tool_use_id,
        )

    def on_tool_result(
        self,
        summary: str,
        *,
        is_error: bool = False,
        depth: int = 0,
        tool_use_id: str | None = None,
    ) -> None:
        self._emit(
            "tool.result",
            summary=summary,
            is_error=is_error,
            depth=depth,
            tool_use_id=tool_use_id,
        )

    def on_system(self, subtype: str, data: dict[str, Any]) -> None:
        if subtype == "init" and self._on_sdk_session_id is not None:
            sdk_uuid = data.get("session_id")
            if isinstance(sdk_uuid, str) and sdk_uuid:
                self._on_sdk_session_id(sdk_uuid)
        self._emit("system", subtype=subtype, data=data)

    def on_result(
        self, *, turns: int, cost_usd: float | None, duration_s: float
    ) -> None:
        self._emit("result", turns=turns, cost_usd=cost_usd, duration_s=duration_s)

    def on_error(self, message: str) -> None:
        self._emit("error", message=message)

    def on_todos(self, snapshot: TodoSnapshot) -> None:
        self._emit("todos.snapshot", items=[item.to_dict() for item in snapshot.items])

    def on_output(self, payload: dict[str, Any]) -> None:
        # Spec §8.2 / §11 — `output.created`. Payload keys vary by kind:
        # standalone -> {output_id, kind, title, download_url}
        # kb_version -> {kind, kb_id, version, title, download_url}
        self._emit("output.created", **payload)

    # --- waiting indicator -------------------------------------------- #
    def begin_wait(self, label: str = "Working") -> None:
        self._emit("wait.begin", label=label)

    def end_wait(self) -> None:
        self._emit("wait.end")

    # --- interaction (block on the human) ----------------------------- #
    async def ask_question(self, request: QuestionRequest) -> QuestionResponse:
        loop = asyncio.get_running_loop()
        future: asyncio.Future = loop.create_future()
        tool_use_id = _new_tool_use_id()
        payload = {
            "questions": [
                {
                    "question": q.question,
                    "header": q.header,
                    "multiSelect": q.multi_select,
                    "options": [
                        {"label": o.label, "description": o.description}
                        for o in q.options
                    ],
                }
                for q in request.questions
            ],
        }
        await self._app_state.interactions.park(
            self.session_id,
            PendingInteraction(
                tool_use_id=tool_use_id, kind="question", payload=payload, future=future
            ),
        )
        self._emit(
            "interaction.requested", tool_use_id=tool_use_id, kind="question", **payload
        )
        try:
            answers_raw = await future
        except asyncio.CancelledError:
            return QuestionResponse(answers=[])
        return QuestionResponse(
            answers=[
                Answer(
                    header=a.get("header", ""),
                    selected=list(a.get("selected") or []),
                    other_text=a.get("other_text"),
                )
                for a in (answers_raw or [])
            ]
        )

    async def approve_plan(self, plan: str) -> PlanDecision:
        loop = asyncio.get_running_loop()
        future: asyncio.Future = loop.create_future()
        tool_use_id = _new_tool_use_id()
        payload = {"plan": plan}
        await self._app_state.interactions.park(
            self.session_id,
            PendingInteraction(
                tool_use_id=tool_use_id, kind="plan", payload=payload, future=future
            ),
        )
        self._emit(
            "interaction.requested", tool_use_id=tool_use_id, kind="plan", **payload
        )
        try:
            verdict_raw = await future
        except asyncio.CancelledError:
            return PlanDecision(verdict=PlanVerdict.REJECT, feedback="cancelled")
        verdict_str = (verdict_raw or {}).get("verdict", "reject")
        feedback = (verdict_raw or {}).get("feedback")
        verdict = (
            PlanVerdict.APPROVE if verdict_str == "approve" else PlanVerdict.REJECT
        )
        return PlanDecision(verdict=verdict, feedback=feedback)
