"""Live smoke test for token-level streaming (spec §8.6).

Drives a real `AgentRunner` against the configured model. Captures every UI
event (including stream deltas) into a counter and prints a summary so we can
verify thinking + text + tool_use coverage. Caps `max_turns` to keep cost
bounded.

Run:
    uv run python scripts/smoke_streaming.py
"""

from __future__ import annotations

import asyncio
import sys
import time
from collections import Counter
from pathlib import Path
from typing import Any

from da_agent.agent.core import AgentRunner
from da_agent.config import Settings


class CountingUI:
    """Records call counts and snippets so we can assert coverage end-of-run."""

    def __init__(self) -> None:
        self.counts: Counter[str] = Counter()
        self.text_blocks: dict[str, list[str]] = {}
        self.thinking_blocks: dict[str, list[str]] = {}
        self.atomic_text: list[str] = []
        self.atomic_thinking: list[str] = []
        self.tools: list[tuple[str, dict]] = []
        self.errors: list[str] = []

    # render
    def on_user_prompt(self, t: str) -> None:
        self.counts["user_prompt"] += 1

    def on_thinking(self, t: str) -> None:
        self.counts["atomic_thinking"] += 1
        self.atomic_thinking.append(t[:60])

    def on_text(self, t: str) -> None:
        self.counts["atomic_text"] += 1
        self.atomic_text.append(t[:60])

    def on_text_delta(self, block_id: str, delta: str) -> None:
        self.counts["text_delta"] += 1
        self.text_blocks.setdefault(block_id, []).append(delta)

    def on_text_end(self, block_id: str) -> None:
        self.counts["text_end"] += 1

    def on_thinking_delta(self, block_id: str, delta: str) -> None:
        self.counts["thinking_delta"] += 1
        self.thinking_blocks.setdefault(block_id, []).append(delta)

    def on_thinking_end(self, block_id: str) -> None:
        self.counts["thinking_end"] += 1

    def on_tool_use(self, name: str, ti: dict[str, Any], *, depth: int = 0) -> None:
        self.counts["tool_use"] += 1
        self.tools.append((name, ti))

    def on_tool_result(self, s: str, *, is_error: bool = False, depth: int = 0) -> None:
        self.counts["tool_result"] += 1

    def on_system(self, st: str, d: dict[str, Any]) -> None:
        self.counts["system"] += 1

    def on_result(
        self, *, turns: int, cost_usd: float | None, duration_s: float
    ) -> None:
        self.counts["result"] += 1
        self.last_result = (turns, cost_usd, duration_s)

    def on_error(self, m: str) -> None:
        self.counts["error"] += 1
        self.errors.append(m)

    def on_todos(self, snapshot: Any) -> None:
        self.counts["todos"] += 1

    def begin_wait(self, label: str = "Working") -> None:
        self.counts["wait_begin"] += 1

    def end_wait(self) -> None:
        self.counts["wait_end"] += 1

    async def ask_question(self, request):  # pragma: no cover -- not exercised
        from da_agent.agent.events import QuestionResponse

        return QuestionResponse(answers=[])

    async def approve_plan(self, plan):  # pragma: no cover -- not exercised
        from da_agent.agent.events import PlanDecision, PlanVerdict

        return PlanDecision(verdict=PlanVerdict.APPROVE)


async def main() -> int:
    target_file = Path(__file__).resolve().parent / "smoke_target.txt"
    target_file.write_text(
        "This is a tiny target file for the streaming smoke test.\n"
        "Line 2: cảm ơn bạn đã đọc file này.\n"
    )

    settings = Settings()
    settings.show_thinking = True
    settings.stream_responses = True
    settings.plan_first = False
    settings.max_turns = 4

    prompt = (
        "Hãy suy nghĩ 10 lần về con gà có trước hay quả trứng có trước, "
        f"sau đó hãy đọc file {target_file} và trả lời ngắn gọn nội dung."
    )

    ui = CountingUI()
    started = time.monotonic()
    print(
        "→ smoke: starting (model=",
        settings.model,
        ", stream=",
        settings.stream_responses,
        ")",
        sep="",
    )
    print("→ prompt:", prompt[:100], "…")

    async with AgentRunner(ui, settings) as runner:
        await runner.send(prompt, echo_prompt=False)

    duration = time.monotonic() - started
    print()
    print("=" * 60)
    print(f"Wall time: {duration:.1f}s")
    print(f"Counts: {dict(ui.counts)}")
    print(f"Text blocks streamed: {len(ui.text_blocks)}")
    for bid, parts in ui.text_blocks.items():
        full = "".join(parts)
        print(f"  - {bid}: {len(parts)} deltas, {len(full)} chars -- {full[:80]!r}")
    print(f"Thinking blocks streamed: {len(ui.thinking_blocks)}")
    for bid, parts in ui.thinking_blocks.items():
        full = "".join(parts)
        print(f"  - {bid}: {len(parts)} deltas, {len(full)} chars -- {full[:80]!r}")
    print(f"Tools used: {[name for name, _ in ui.tools]}")
    if ui.atomic_text:
        print(f"!! Atomic text fired ({len(ui.atomic_text)}): {ui.atomic_text}")
    if ui.atomic_thinking:
        print(
            f"!! Atomic thinking fired ({len(ui.atomic_thinking)}): {ui.atomic_thinking}"
        )
    if ui.errors:
        print(f"!! Errors: {ui.errors}")

    # Acceptance criteria for the smoke test:
    issues: list[str] = []
    if ui.counts["text_delta"] == 0:
        issues.append("no text deltas")
    if ui.counts["text_end"] != len(ui.text_blocks):
        issues.append(
            f"text_end count {ui.counts['text_end']} != block count {len(ui.text_blocks)}"
        )
    if ui.counts["thinking_delta"] == 0:
        issues.append("no thinking deltas (model did not emit thinking)")
    if ui.counts["tool_use"] == 0:
        issues.append("no tool_use observed (model did not call Read)")
    if ui.atomic_text:
        issues.append("atomic on_text fired despite streaming on (suppression broken)")

    if issues:
        print()
        print("FAIL:", "; ".join(issues))
        return 1
    print()
    print("OK: streaming covers thinking + text + tool_use; no atomic leaks.")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
