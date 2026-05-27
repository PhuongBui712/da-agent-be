"""Unit tests for token-level streaming output (spec §8.6).

Tests drive `AgentRunner._handle_stream_event` and `_render` directly — no
network / API key required. A `RecordingUI` captures all Protocol calls.
"""

from __future__ import annotations

import re

from claude_agent_sdk import (
    AssistantMessage,
    StreamEvent,
    TextBlock,
    ThinkingBlock,
)

from da_agent.agent.core import AgentRunner
from da_agent.agent.events import (
    PlanDecision,
    QuestionRequest,
    QuestionResponse,
)
from da_agent.config import Settings


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

_BLOCK_ID_TEXT_RE = re.compile(r"^txt_[0-9a-f]{12}$")
_BLOCK_ID_THINK_RE = re.compile(r"^thk_[0-9a-f]{12}$")


class RecordingUI:
    """Minimal AgentUI that records every call as a tuple in `self.calls`."""

    def __init__(self):
        self.calls: list[tuple] = []

    def _rec(self, *a):
        self.calls.append(a)

    def on_user_prompt(self, t):
        self._rec("prompt", t)

    def on_thinking(self, t):
        self._rec("thinking", t)

    def on_text(self, t):
        self._rec("text", t)

    def on_text_delta(self, block_id, delta):
        self._rec("text_delta", block_id, delta)

    def on_text_end(self, block_id):
        self._rec("text_end", block_id)

    def on_thinking_delta(self, block_id, delta):
        self._rec("thinking_delta", block_id, delta)

    def on_thinking_end(self, block_id):
        self._rec("thinking_end", block_id)

    def on_tool_use(self, n, i, *, depth=0):
        self._rec("tool_use", n, depth)

    def on_tool_result(self, s, *, is_error=False, depth=0):
        self._rec("tool_result", is_error, depth)

    def on_system(self, st, d):
        self._rec("system", st)

    def on_result(self, *, turns, cost_usd, duration_s):
        self._rec("result", turns)

    def on_error(self, m):
        self._rec("error", m)

    def on_todos(self, snapshot):
        pass

    def begin_wait(self, label="Working"):
        pass

    def end_wait(self):
        pass

    async def ask_question(self, request: QuestionRequest) -> QuestionResponse:
        return QuestionResponse(answers=[])

    async def approve_plan(self, plan: str) -> PlanDecision:
        return PlanDecision(verdict="approve")  # type: ignore[arg-type]


def _make_runner(
    show_thinking: bool = True, stream_responses: bool = True
) -> tuple[AgentRunner, RecordingUI]:
    ui = RecordingUI()
    s = Settings()
    s.show_thinking = show_thinking
    s.stream_responses = stream_responses
    runner = AgentRunner(ui, s)
    return runner, ui


def _stream_event(event: dict, parent_tool_use_id: str | None = None) -> StreamEvent:
    return StreamEvent(
        uuid="u1",
        session_id="s1",
        event=event,
        parent_tool_use_id=parent_tool_use_id,
    )


def _block_start(index: int, kind: str, **kwargs) -> StreamEvent:
    return _stream_event(
        {
            "type": "content_block_start",
            "index": index,
            "content_block": {"type": kind, **kwargs},
        }
    )


def _block_delta_text(index: int, text: str) -> StreamEvent:
    return _stream_event(
        {
            "type": "content_block_delta",
            "index": index,
            "delta": {"type": "text_delta", "text": text},
        }
    )


def _block_delta_thinking(index: int, text: str) -> StreamEvent:
    return _stream_event(
        {
            "type": "content_block_delta",
            "index": index,
            "delta": {"type": "thinking_delta", "thinking": text},
        }
    )


def _block_delta_input_json(index: int, partial_json: str) -> StreamEvent:
    return _stream_event(
        {
            "type": "content_block_delta",
            "index": index,
            "delta": {"type": "input_json_delta", "partial_json": partial_json},
        }
    )


def _block_delta_signature(index: int, signature: str) -> StreamEvent:
    return _stream_event(
        {
            "type": "content_block_delta",
            "index": index,
            "delta": {"type": "signature_delta", "signature": signature},
        }
    )


def _block_stop(index: int) -> StreamEvent:
    return _stream_event({"type": "content_block_stop", "index": index})


# --------------------------------------------------------------------------- #
# 1. Text block emits delta and end
# --------------------------------------------------------------------------- #
def test_stream_event_text_emits_delta_and_end():
    runner, ui = _make_runner()

    runner._handle_stream_event(_block_start(0, "text"))
    runner._handle_stream_event(_block_delta_text(0, "Hello"))
    runner._handle_stream_event(_block_delta_text(0, " world"))
    runner._handle_stream_event(_block_stop(0))

    delta_calls = [c for c in ui.calls if c[0] == "text_delta"]
    end_calls = [c for c in ui.calls if c[0] == "text_end"]

    assert len(delta_calls) == 2
    assert len(end_calls) == 1

    # All calls share the same block_id.
    block_id = delta_calls[0][1]
    assert _BLOCK_ID_TEXT_RE.match(block_id), (
        f"block_id {block_id!r} does not match txt_<12hex>"
    )
    assert delta_calls[1][1] == block_id
    assert end_calls[0][1] == block_id

    # Delta texts are correct.
    assert delta_calls[0][2] == "Hello"
    assert delta_calls[1][2] == " world"


# --------------------------------------------------------------------------- #
# 2. Thinking block emits delta and end
# --------------------------------------------------------------------------- #
def test_stream_event_thinking_emits_delta_and_end():
    runner, ui = _make_runner()

    runner._handle_stream_event(_block_start(0, "thinking"))
    runner._handle_stream_event(_block_delta_thinking(0, "hmm"))
    runner._handle_stream_event(_block_delta_thinking(0, " deeper"))
    runner._handle_stream_event(_block_stop(0))

    delta_calls = [c for c in ui.calls if c[0] == "thinking_delta"]
    end_calls = [c for c in ui.calls if c[0] == "thinking_end"]

    assert len(delta_calls) == 2
    assert len(end_calls) == 1

    block_id = delta_calls[0][1]
    assert _BLOCK_ID_THINK_RE.match(block_id), (
        f"block_id {block_id!r} does not match thk_<12hex>"
    )
    assert delta_calls[1][1] == block_id
    assert end_calls[0][1] == block_id

    assert delta_calls[0][2] == "hmm"
    assert delta_calls[1][2] == " deeper"


# --------------------------------------------------------------------------- #
# 3. Two text blocks at different indices get distinct block_ids
# --------------------------------------------------------------------------- #
def test_stream_event_text_blocks_have_distinct_ids():
    runner, ui = _make_runner()

    runner._handle_stream_event(_block_start(0, "text"))
    runner._handle_stream_event(_block_delta_text(0, "first"))
    runner._handle_stream_event(_block_stop(0))

    runner._handle_stream_event(_block_start(1, "text"))
    runner._handle_stream_event(_block_delta_text(1, "second"))
    runner._handle_stream_event(_block_stop(1))

    delta_calls = [c for c in ui.calls if c[0] == "text_delta"]
    assert len(delta_calls) == 2
    id0 = delta_calls[0][1]
    id1 = delta_calls[1][1]
    assert id0 != id1, "Different text blocks must have distinct block_ids"


# --------------------------------------------------------------------------- #
# 4. Subagent StreamEvent is dropped
# --------------------------------------------------------------------------- #
def test_stream_event_subagent_dropped():
    runner, ui = _make_runner()

    # Build events as if from a subagent (parent_tool_use_id set).
    subagent_start = StreamEvent(
        uuid="u1",
        session_id="s1",
        event={
            "type": "content_block_start",
            "index": 0,
            "content_block": {"type": "text"},
        },
        parent_tool_use_id="t_sub",
    )
    subagent_delta = StreamEvent(
        uuid="u2",
        session_id="s1",
        event={
            "type": "content_block_delta",
            "index": 0,
            "delta": {"type": "text_delta", "text": "sub"},
        },
        parent_tool_use_id="t_sub",
    )
    subagent_stop = StreamEvent(
        uuid="u3",
        session_id="s1",
        event={"type": "content_block_stop", "index": 0},
        parent_tool_use_id="t_sub",
    )

    # `_render` is responsible for the subagent guard.
    runner._render(subagent_start, 0.0)
    runner._render(subagent_delta, 0.0)
    runner._render(subagent_stop, 0.0)

    assert not ui.calls, f"Expected no UI calls for subagent events, got {ui.calls}"


# --------------------------------------------------------------------------- #
# 5. input_json_delta is ignored (no UI call, no error)
# --------------------------------------------------------------------------- #
def test_stream_event_input_json_delta_ignored():
    runner, ui = _make_runner()

    # tool_use block start
    runner._handle_stream_event(
        _stream_event(
            {
                "type": "content_block_start",
                "index": 0,
                "content_block": {"type": "tool_use", "id": "t1", "name": "Bash"},
            }
        )
    )
    runner._handle_stream_event(_block_delta_input_json(0, '{"cmd"'))

    assert not any(
        c[0] in {"text_delta", "text_end", "thinking_delta", "thinking_end"}
        for c in ui.calls
    )


# --------------------------------------------------------------------------- #
# 6. signature_delta is ignored (no UI call, no error)
# --------------------------------------------------------------------------- #
def test_stream_event_signature_delta_ignored():
    runner, ui = _make_runner()

    runner._handle_stream_event(_block_start(0, "thinking"))
    runner._handle_stream_event(_block_delta_thinking(0, "reasoning"))
    runner._handle_stream_event(_block_delta_signature(0, "sig123"))
    runner._handle_stream_event(_block_stop(0))

    # Only thinking_delta for "reasoning" and thinking_end should appear.
    call_kinds = [c[0] for c in ui.calls]
    assert "thinking_delta" in call_kinds
    assert "thinking_end" in call_kinds
    # No error or unexpected calls.
    assert "error" not in call_kinds


# --------------------------------------------------------------------------- #
# 7. Empty text delta is dropped
# --------------------------------------------------------------------------- #
def test_stream_event_empty_text_delta_dropped():
    runner, ui = _make_runner()

    runner._handle_stream_event(_block_start(0, "text"))
    runner._handle_stream_event(_block_delta_text(0, ""))  # empty delta
    runner._handle_stream_event(_block_stop(0))

    assert not any(c[0] == "text_delta" for c in ui.calls), (
        "Empty deltas must be dropped"
    )


# --------------------------------------------------------------------------- #
# 8. Thinking suppressed when show_thinking=False
# --------------------------------------------------------------------------- #
def test_stream_event_thinking_suppressed_when_show_thinking_off():
    runner, ui = _make_runner(show_thinking=False)

    runner._handle_stream_event(_block_start(0, "thinking"))
    runner._handle_stream_event(_block_delta_thinking(0, "internal thought"))
    runner._handle_stream_event(_block_stop(0))

    assert not any(c[0] in {"thinking_delta", "thinking_end"} for c in ui.calls)


# --------------------------------------------------------------------------- #
# 9. Streamed text block suppresses atomic render
# --------------------------------------------------------------------------- #
def test_suppression_skips_streamed_text_block():
    runner, ui = _make_runner()

    # Stream a text block with deltas.
    runner._handle_stream_event(_block_start(0, "text"))
    runner._handle_stream_event(_block_delta_text(0, "streamed"))
    runner._handle_stream_event(_block_stop(0))

    # Feed the trailing AssistantMessage with the same content (full block).
    runner._render(
        AssistantMessage(content=[TextBlock(text="streamed")], model="fake"),
        0.0,
    )

    # Only streaming calls should appear; no atomic "text" call.
    assert any(c[0] == "text_delta" for c in ui.calls)
    assert any(c[0] == "text_end" for c in ui.calls)
    assert not any(c[0] == "text" for c in ui.calls), (
        "Streamed text block must not atomic-render"
    )


# --------------------------------------------------------------------------- #
# 10. Thinking still atomic-renders when a text block was streamed
# --------------------------------------------------------------------------- #
def test_suppression_does_not_skip_thinking_when_text_streamed():
    runner, ui = _make_runner()

    # The trailing AssistantMessage content order MUST match the SDK
    # content_block index order: thinking at 0, text at 1.
    runner._handle_stream_event(_block_start(1, "text"))
    runner._handle_stream_event(_block_delta_text(1, "hello"))
    runner._handle_stream_event(_block_stop(1))

    # AssistantMessage has ThinkingBlock at position 0, TextBlock at position 1.
    runner._render(
        AssistantMessage(
            content=[
                ThinkingBlock(thinking="thought", signature="sig"),
                TextBlock(text="hello"),
            ],
            model="fake",
        ),
        0.0,
    )

    # Atomic thinking MUST render (it never streamed).
    assert any(c[0] == "thinking" for c in ui.calls), (
        "Thinking should still atomic-render"
    )
    # Atomic text MUST be suppressed (position 1 was streamed).
    assert not any(c[0] == "text" for c in ui.calls), "Text should be suppressed"


# --------------------------------------------------------------------------- #
# 11. Block with no delta does not trigger suppression
# --------------------------------------------------------------------------- #
def test_block_with_no_delta_does_not_suppress():
    runner, ui = _make_runner()

    # Start and stop a text block but emit NO delta.
    runner._handle_stream_event(_block_start(0, "text"))
    runner._handle_stream_event(_block_stop(0))

    # The trailing AssistantMessage should still atomic-render.
    runner._render(
        AssistantMessage(content=[TextBlock(text="hello")], model="fake"),
        0.0,
    )

    assert any(c[0] == "text" for c in ui.calls), (
        "No delta means no suppression; atomic text must fire"
    )


# --------------------------------------------------------------------------- #
# 12. Suppression does not apply to subagent AssistantMessage
# --------------------------------------------------------------------------- #
def test_suppression_does_not_apply_to_subagent_assistant_message():
    runner, ui = _make_runner()

    # Stream a main-thread text block (increments suppression counter).
    runner._handle_stream_event(_block_start(0, "text"))
    runner._handle_stream_event(_block_delta_text(0, "main"))
    runner._handle_stream_event(_block_stop(0))

    # Subagent AssistantMessage (parent_tool_use_id set) must NOT be suppressed.
    runner._render(
        AssistantMessage(
            content=[TextBlock(text="subagent output")],
            model="fake",
            parent_tool_use_id="t_x",
        ),
        0.0,
    )

    assert any(c[0] == "text" for c in ui.calls), (
        "Subagent AssistantMessage must not be suppressed"
    )


# --------------------------------------------------------------------------- #
# 13. send() resets streaming state
# --------------------------------------------------------------------------- #
async def test_send_resets_streaming_state():
    runner, ui = _make_runner()

    # Manually dirty the per-turn streaming state.
    runner._stream_blocks[0] = ("text", "txt_aabbccddeeff")
    runner._streamed_block_ids.add("txt_aabbccddeeff")

    # Install a minimal stub client so `send` can run without a real SDK.
    class _StubClient:
        async def query(self, prompt):
            pass

        async def receive_response(self):
            return
            yield  # make it an async generator

    runner._client = _StubClient()  # type: ignore[assignment]
    await runner.send("hi", echo_prompt=False)

    assert runner._stream_blocks == {}
    assert runner._streamed_block_ids == set()


# --------------------------------------------------------------------------- #
# 13b. Gap block in the middle does not corrupt suppression positions.
# Regression for the positional-counter bug in the first impl: a streamed
# block at index 0, an empty start+stop at index 1, and a streamed block at
# index 2 must suppress positions 0 and 2 only -- the trailing TextBlock at
# position 1 must atomic-render.
# --------------------------------------------------------------------------- #
def test_suppression_gap_block_in_middle_does_not_shift_positions():
    runner, ui = _make_runner()

    # Position 0 -- streams.
    runner._handle_stream_event(_block_start(0, "text"))
    runner._handle_stream_event(_block_delta_text(0, "first"))
    runner._handle_stream_event(_block_stop(0))

    # Position 1 -- starts and stops without any delta.
    runner._handle_stream_event(_block_start(1, "text"))
    runner._handle_stream_event(_block_stop(1))

    # Position 2 -- streams.
    runner._handle_stream_event(_block_start(2, "text"))
    runner._handle_stream_event(_block_delta_text(2, "third"))
    runner._handle_stream_event(_block_stop(2))

    runner._render(
        AssistantMessage(
            content=[
                TextBlock(text="first"),
                TextBlock(text="middle-no-delta"),
                TextBlock(text="third"),
            ],
            model="fake",
        ),
        0.0,
    )

    atomic_texts = [c[1] for c in ui.calls if c[0] == "text"]
    assert atomic_texts == ["middle-no-delta"], (
        "Only the gap block at position 1 should atomic-render; positions 0 and "
        "2 are suppressed by streaming."
    )


# --------------------------------------------------------------------------- #
# 13c. Streamed thinking block suppresses the trailing atomic ThinkingBlock.
# --------------------------------------------------------------------------- #
def test_suppression_skips_streamed_thinking_block():
    runner, ui = _make_runner()

    runner._handle_stream_event(_block_start(0, "thinking"))
    runner._handle_stream_event(_block_delta_thinking(0, "deep thoughts"))
    runner._handle_stream_event(_block_stop(0))

    runner._render(
        AssistantMessage(
            content=[ThinkingBlock(thinking="deep thoughts", signature="sig")],
            model="fake",
        ),
        0.0,
    )

    assert any(c[0] == "thinking_delta" for c in ui.calls)
    assert any(c[0] == "thinking_end" for c in ui.calls)
    assert not any(c[0] == "thinking" for c in ui.calls), (
        "Streamed thinking block must not atomic-render"
    )


# --------------------------------------------------------------------------- #
# 14. _build_options honours stream_responses setting
# --------------------------------------------------------------------------- #
def test_options_includes_partial_messages_when_stream_on():
    runner_on, _ = _make_runner(stream_responses=True)
    opts_on = runner_on._build_options()
    assert opts_on.include_partial_messages is True

    runner_off, _ = _make_runner(stream_responses=False)
    opts_off = runner_off._build_options()
    assert opts_off.include_partial_messages is False


# --------------------------------------------------------------------------- #
# 15. message_start / message_delta / message_stop are no-ops
# --------------------------------------------------------------------------- #
def test_message_lifecycle_events_are_noop():
    runner, ui = _make_runner()

    for ev_type in ("message_start", "message_delta", "message_stop"):
        runner._handle_stream_event(_stream_event({"type": ev_type}))

    assert not ui.calls, f"Expected no UI calls for lifecycle events, got {ui.calls}"
