"""Configuration and path resolution.

Everything the agent needs to locate itself on disk lives here. Kept dependency-free
so it can be imported by both the CLI and (later) the web backend.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(".env")


def find_project_root(start: Path | None = None) -> Path:
    """Walk upward from this file (or `start`) until a dir containing `.claude` is found.

    The `.claude/skills/` tree must sit under the agent SDK `cwd` for skill discovery,
    so the project root is defined as "the directory that owns `.claude`".
    """
    here = (start or Path(__file__)).resolve()
    for candidate in [here, *here.parents]:
        if (candidate / ".claude").is_dir():
            return candidate
    # Fallback: repo layout is <root>/src/da_agent/config.py
    return Path(__file__).resolve().parents[2]


@dataclass(slots=True)
class Settings:
    """Runtime settings. Override via env vars or constructor; safe defaults otherwise."""

    # --- model / agent loop ---
    model: str = field(
        default_factory=lambda: os.getenv(
            "DA_AGENT_MODEL", "databricks-claude-sonnet-4-6"
        )
    )
    max_turns: int | None = field(
        default_factory=lambda: _int_env("DA_AGENT_MAX_TURNS")
    )

    # Ingestion profiler (kb_profiler subagent) — separate from in-session
    # analysis (which inherits the main `model` above). Defaults to the SDK's
    # `opus` alias so the env-configured ANTHROPIC_DEFAULT_OPUS_MODEL applies;
    # set DA_AGENT_KB_PROFILER_MODEL to a full id to pin a specific opus build.
    kb_profiler_model: str = field(
        default_factory=lambda: os.getenv("DA_AGENT_KB_PROFILER_MODEL", "databricks-claude-opus-4-6")
    )

    # Start each session in plan mode so complex requests produce a plan + approval
    # (demonstrates the approval UX). Flip to False for straight-to-execution.
    plan_first: bool = field(
        default_factory=lambda: _bool_env("DA_AGENT_PLAN_FIRST", False)
    )

    # Show the model's extended-thinking blocks in the TUI.
    show_thinking: bool = field(
        default_factory=lambda: _bool_env("DA_AGENT_SHOW_THINKING", True)
    )

    # Token-level streaming via SDK `include_partial_messages` (spec §8.6).
    # When False, the runner falls back to atomic assistant.text / assistant.thinking
    # SSE events emitted from the trailing AssistantMessage.
    stream_responses: bool = field(
        default_factory=lambda: _bool_env("DA_AGENT_STREAM", True)
    )

    # Spec §5.3 — short-term attachment hard cap. Server returns 413 above this.
    attachment_max_bytes: int = field(
        default_factory=lambda: _int_env_default(
            "DA_AGENT_ATTACHMENT_MAX_BYTES", 100 * 1024 * 1024
        )
    )

    # Spec §8.5 — soft warn threshold for assembled `<scope>` block size.
    scope_warn_bytes: int = field(
        default_factory=lambda: _int_env_default(
            "DA_AGENT_SCOPE_WARN_BYTES", 256 * 1024
        )
    )

    # --- filesystem ---
    project_root: Path = field(default_factory=find_project_root)
    data_root: Path = field(
        default_factory=lambda: Path(
            os.getenv("DA_AGENT_HOME", "~/.da-agent")
        ).expanduser()
    )

    @property
    def kb_dir(self) -> Path:
        return self.data_root / "kb"

    @property
    def workspace_dir(self) -> Path:
        """DEPRECATED — never expose to the agent.

        Retained as an importable property for backward compatibility with any
        external consumer. The directory is no longer created by `ensure_dirs`,
        is no longer in `ClaudeAgentOptions.add_dirs`, and is no longer
        referenced by the system prompt. Spec §8.2 routes all writes through
        `outputs_dir` (standalone) or `kb_dir/<id>/versions/` (KB-bound) or
        `attachments_dir/<sid>/<att_id>/versions/` (attachment-bound).
        """
        return self.data_root / "workspace"

    @property
    def sessions_dir(self) -> Path:
        """CLAUDE_CONFIG_DIR target: keeps SDK session JSONL with the tool's data."""
        return self.data_root / "sessions"

    @property
    def outputs_dir(self) -> Path:
        """Spec §4 / §8.2 — registered standalone outputs."""
        return self.data_root / "outputs"

    def outputs_session_dir(self, session_id: str) -> Path:
        """Per-session outputs root: `<outputs_dir>/<session_id>/`.

        Created lazily on first write — `ensure_dirs()` does NOT mkdir per-session.
        Layout: `outputs/<session_id>/<output_id>/<filename>` (Phase C 2026-05-31:
        all outputs — standalone, KB-bound, attachment-bound — land here so
        `DELETE /sessions/<sid>` can wipe with a single rmtree).
        """
        return self.outputs_dir / session_id

    @property
    def attachments_dir(self) -> Path:
        """Spec §4 / §5.3 — short-term per-session attachments."""
        return self.data_root / "attachments"

    @property
    def skills_dir(self) -> Path:
        return self.project_root / ".claude" / "skills"

    @property
    def agent_memory_dir(self) -> Path:
        """Persistent memory for ingestion subagents.

        Lives under the app data root (`~/.da-agent/agent-memory/`) so it
        survives across dev/prod environments and is independent of the
        repo checkout. Per-agent subdirs are created on first write.

        We intentionally do NOT use the SDK's `memory="local"` field on the
        kb_profiler subagent: that scope hard-codes the path to
        `<project_root>/.claude/agent-memory-local/`, which leaks profile
        artefacts into a developer's checkout. We pass the absolute path
        explicitly in the invocation prompt instead.
        """
        return self.data_root / "agent-memory"

    @property
    def kb_profiler_memory_dir(self) -> Path:
        """`<data_root>/agent-memory/kb_profiler/` — where per-KB notes land."""
        return self.agent_memory_dir / "kb_profiler"

    @property
    def sessions_data_dir(self) -> Path:
        """Per-session symlink farm root: `<data_root>/sessions-data/`.

        Each session gets `<sessions-data>/<sid>/{kb, workspace, outputs}/`.
        `kb/` and `outputs/` are populated with symlinks pointing at the
        canonical `kb_dir` / `outputs_dir/<sid>/` so the SDK only sees
        per-turn-scoped paths through `add_dirs` (spec §8.5 enforcement).
        """
        return self.data_root / "sessions-data"

    def session_data_dir(self, session_id: str) -> Path:
        """`<sessions-data>/<sid>/` — root of one session's farm."""
        return self.sessions_data_dir / session_id

    def session_kb_dir(self, session_id: str) -> Path:
        """`<sessions-data>/<sid>/kb/` — symlink farm for in-scope KBs."""
        return self.session_data_dir(session_id) / "kb"

    def session_workspace_dir(self, session_id: str) -> Path:
        """`<sessions-data>/<sid>/workspace/` — per-session scratch root."""
        return self.session_data_dir(session_id) / "workspace"

    def session_outputs_view_dir(self, session_id: str) -> Path:
        """`<sessions-data>/<sid>/outputs/` — symlink to canonical `outputs/<sid>/`."""
        return self.session_data_dir(session_id) / "outputs"

    def ensure_dirs(self) -> None:
        for d in (
            self.data_root,
            self.kb_dir,
            self.sessions_dir,
            self.outputs_dir,
            self.attachments_dir,
            self.kb_profiler_memory_dir,
            self.sessions_data_dir,
        ):
            d.mkdir(parents=True, exist_ok=True)


def _bool_env(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _int_env(name: str) -> int | None:
    raw = os.getenv(name)
    return int(raw) if raw and raw.strip().isdigit() else None


def _int_env_default(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw and raw.strip().lstrip("-").isdigit():
        return int(raw)
    return default
