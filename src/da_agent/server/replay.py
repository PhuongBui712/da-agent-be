"""Translate `claude_agent_sdk.SessionMessage` history into SSE event dicts.

Reopening a session in the FE fetches `GET /sessions/{sid}/messages`; this
module reads the JSONL transcript via the SDK and converts each message
into the same wire-format event dicts the live SSE stream emits. The FE
folds them through the same `streamReducer` so render output is identical.

Block-level mapping mirrors `agent/core.py`:
- assistant.text       <- TextBlock      ('assistant.text')
- assistant.thinking   <- ThinkingBlock  ('assistant.thinking')
- tool.use             <- ToolUseBlock   (filtered by _INTERACTIVE_TOOLS)
- tool.result          <- ToolResultBlock embedded in user role
- user.prompt          <- user role with str content
- result               <- synthetic, between turns and at the end
"""

from __future__ import annotations

from typing import Any

from claude_agent_sdk import SessionMessage

from ..agent.core import _stringify_tool_result
from ..agent.todos import TODO_TOOL_NAMES

# Mirrors `agent/core.py:_INTERACTIVE_TOOLS`. These render via dedicated UI
# surfaces during a live turn and are NOT replayed as ordinary tool steps.
_INTERACTIVE_TOOLS = {"AskUserQuestion", "ExitPlanMode"} | TODO_TOOL_NAMES


def replay_to_events(messages: list[SessionMessage], sid: str) -> list[dict[str, Any]]:
    """Build an SSE-compatible event list from a session JSONL transcript.

    Args:
        messages: Output of `claude_agent_sdk.get_session_messages(...)`.
        sid: Backend session id (`sess_<hex>`) to stamp on every event so
            FE wire-shape matches the live stream.

    Returns:
        List of event dicts ready to be JSON-serialized and replayed by the
        FE reducer. Empty when `messages` is empty.
    """
    events: list[dict[str, Any]] = []
    if not messages:
        return events

    turn_count = 0
    for msg in messages:
        message = getattr(msg, "message", None) or {}
        if msg.type == "user":
            content = message.get("content")
            if isinstance(content, str):
                # Close the prior turn before opening a new user prompt so the
                # FE reducer flips `inToolChain` and marks earlier thinking as
                # `done` -- mirrors the live `ResultMessage` boundary.
                if turn_count > 0:
                    events.append(_result_event(sid, turn_count))
                turn_count += 1
                events.append(
                    {"type": "user.prompt", "session_id": sid, "text": content}
                )
            elif isinstance(content, list):
                # User-role tool_result blocks (paired with assistant tool_use).
                for block in content:
                    if not isinstance(block, dict):
                        continue
                    if block.get("type") != "tool_result":
                        continue
                    summary = _stringify_tool_result(block.get("content"))
                    events.append(
                        {
                            "type": "tool.result",
                            "session_id": sid,
                            "summary": summary,
                            "is_error": bool(block.get("is_error")),
                            "depth": 0,
                            "tool_use_id": block.get("tool_use_id"),
                        }
                    )
        elif msg.type == "assistant":
            content = message.get("content") if isinstance(message, dict) else None
            if not isinstance(content, list):
                continue
            for block in content:
                if not isinstance(block, dict):
                    continue
                btype = block.get("type")
                if btype == "text":
                    text = block.get("text", "")
                    if text and text.strip():
                        events.append(
                            {
                                "type": "assistant.text",
                                "session_id": sid,
                                "text": text,
                            }
                        )
                elif btype == "thinking":
                    thinking = block.get("thinking", "")
                    if thinking and thinking.strip():
                        events.append(
                            {
                                "type": "assistant.thinking",
                                "session_id": sid,
                                "text": thinking,
                            }
                        )
                elif btype == "tool_use":
                    name = block.get("name", "")
                    if name in _INTERACTIVE_TOOLS:
                        continue
                    events.append(
                        {
                            "type": "tool.use",
                            "session_id": sid,
                            "name": name,
                            "input": block.get("input") or {},
                            "depth": 0,
                            "tool_use_id": block.get("id"),
                        }
                    )

    if turn_count > 0:
        events.append(_result_event(sid, turn_count))
    return events


def _result_event(sid: str, turns: int) -> dict[str, Any]:
    return {
        "type": "result",
        "session_id": sid,
        "turns": turns,
        "cost_usd": None,
        "duration_s": 0.0,
    }
