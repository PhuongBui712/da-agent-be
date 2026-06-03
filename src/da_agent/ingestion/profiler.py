"""KbProfiler — drives the kb_profiler subagent for one upload.

A thin wrapper over `ClaudeSDKClient` that:

  * Builds a single-purpose `ClaudeAgentOptions` containing only the
    `kb_profiler` subagent (configured opus model and the xlsx skill). Reuses
    the project's security layer so the profiler runs under the same sandbox +
    deny rules + Bash hook as the main agent.

  * Sends one query (`@kb_profiler Profile <raw_path>`), drains the response
    stream until a `ResultMessage` arrives, and returns the resolved memory
    path (or `None` on failure).

A module-level `asyncio.Semaphore(1)` serialises concurrent uploads: multiple
uploads queue here because the opus quota is global per process.

Failures (SDK error, file not produced) surface to the runner, which sets
status=READY_PARTIAL — the KB stays scope-able, just without semantic memory.
"""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass
from pathlib import Path

from claude_agent_sdk import (
    AgentDefinition,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    ResultMessage,
)

from ..agent.security import (
    build_permission_settings_json,
    build_sandbox_settings,
    build_security_hooks,
)
from ..config import Settings
from .prompts import (
    KB_PROFILER_DESCRIPTION,
    KB_PROFILER_PROMPT,
    build_invocation_prompt,
)

_LOG = logging.getLogger(__name__)

# Global single-flight: opus is gated to 1 concurrent invocation per CLAUDE.md.
_PROFILE_LOCK = asyncio.Semaphore(1)


@dataclass(slots=True)
class ProfileResult:
    ok: bool
    memory_path: Path | None
    error: str | None
    duration_ms: int


def build_kb_profiler_definition(settings: Settings) -> AgentDefinition:
    """Construct the kb_profiler AgentDefinition.

    NOTE on memory: we do NOT set `memory="local"` here. That SDK feature
    hard-codes the persistent memory directory to the repo checkout, which is
    wrong for a packaged tool — the user's data should live under their
    `~/.da-agent` data root. We pass the absolute target path through the
    invocation prompt instead. As a result we must list Read/Write/Edit
    explicitly in `tools` (the SDK's auto-enable for those only fires when
    `memory` is set).

    `maxTurns` is intentionally unset — the profiler runs to completion for
    whatever shape the workbook turns out to be.
    """
    return AgentDefinition(
        description=KB_PROFILER_DESCRIPTION,
        prompt=KB_PROFILER_PROMPT,
        tools=["Read", "Write", "Edit", "Bash", "Glob", "Grep"],
        skills=["xlsx"],
        model=settings.kb_profiler_model,
    )


def _build_options(settings: Settings) -> ClaudeAgentOptions:
    """Single-shot options for the profiler turn.

    Reuses the project's sandbox + permission rules + security hook so the
    profiler runs under the exact same isolation as the main agent. The
    `kb_profiler` AgentDefinition is the only registered subagent.
    """
    env = dict(os.environ)
    # Keep SDK session JSONL co-located with the rest of the tool's data.
    env["CLAUDE_CONFIG_DIR"] = str(settings.sessions_dir)

    return ClaudeAgentOptions(
        cwd=str(settings.project_root),
        # Discover .claude/skills (xlsx). We do NOT include "user" because
        # we don't want host-level Claude config bleeding into ingestion.
        setting_sources=["project"],
        skills=["xlsx"],
        agents={"kb_profiler": build_kb_profiler_definition(settings)},
        # The main loop must be allowed to dispatch the subagent (Task) and
        # the subagent's own tools propagate from its AgentDefinition.
        allowed_tools=["Task", "Read", "Bash", "Glob", "Grep", "Write", "Edit"],
        disallowed_tools=["WebFetch", "WebSearch"],
        sandbox=build_sandbox_settings(),
        settings=build_permission_settings_json(settings),
        hooks=build_security_hooks(),
        # No max_turns — ingest runs to completion regardless of file shape.
        env=env,
    )


class KbProfiler:
    """One-shot driver. Construct, call `run`, throw away."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    async def run(self, *, kb_id: str, raw_path: Path, filename: str) -> ProfileResult:
        """Invoke the kb_profiler subagent and resolve the memory path.

        The result `ok=True` requires (a) the SDK ResultMessage came back
        without `is_error`, AND (b) the expected file `<kb_id>.md` exists
        on disk under the configured memory directory. Either condition
        failing yields `ok=False` with a short reason — the runner writes
        that into the registry as `error` on READY_PARTIAL.
        """
        memory_dir = self.settings.kb_profiler_memory_dir
        # The SDK creates this on first write but the registry-write path
        # below also touches it via Read/etc; create eagerly so the first
        # invocation does not hit a missing-parent error.
        memory_dir.mkdir(parents=True, exist_ok=True)
        target_file = memory_dir / f"{kb_id}.md"

        prompt = build_invocation_prompt(
            kb_id=kb_id,
            raw_path=str(raw_path),
            filename=filename,
            memory_dir=str(memory_dir),
        )
        options = _build_options(self.settings)

        async with _PROFILE_LOCK:
            try:
                started = asyncio.get_event_loop().time()
                async with ClaudeSDKClient(options=options) as client:
                    await client.query(prompt)
                    result_msg: ResultMessage | None = None
                    async for msg in client.receive_response():
                        if isinstance(msg, ResultMessage):
                            result_msg = msg
                            break
                    elapsed_ms = int((asyncio.get_event_loop().time() - started) * 1000)
            except Exception as exc:  # noqa: BLE001 — surface as READY_PARTIAL
                _LOG.exception("kb_profiler crashed for %s", kb_id)
                return ProfileResult(
                    ok=False,
                    memory_path=None,
                    error=f"profiler crashed: {type(exc).__name__}: {exc}",
                    duration_ms=0,
                )

        if result_msg is None:
            return ProfileResult(
                ok=False,
                memory_path=None,
                error="profiler produced no ResultMessage",
                duration_ms=0,
            )
        if result_msg.is_error:
            return ProfileResult(
                ok=False,
                memory_path=None,
                error=f"profiler returned is_error=True after {result_msg.num_turns} turns",
                duration_ms=elapsed_ms,
            )
        if not target_file.exists():
            return ProfileResult(
                ok=False,
                memory_path=None,
                error=f"profiler finished but {target_file.name} not found on disk",
                duration_ms=elapsed_ms,
            )

        return ProfileResult(
            ok=True,
            memory_path=target_file,
            error=None,
            duration_ms=elapsed_ms,
        )
