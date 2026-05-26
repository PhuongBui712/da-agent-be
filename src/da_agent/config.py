"""Configuration and path resolution.

Everything the agent needs to locate itself on disk lives here. Kept dependency-free
so it can be imported by both the CLI and (later) the web backend.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


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

    # Start each session in plan mode so complex requests produce a plan + approval
    # (demonstrates the approval UX). Flip to False for straight-to-execution.
    plan_first: bool = field(
        default_factory=lambda: _bool_env("DA_AGENT_PLAN_FIRST", False)
    )

    # Show the model's extended-thinking blocks in the TUI.
    show_thinking: bool = field(
        default_factory=lambda: _bool_env("DA_AGENT_SHOW_THINKING", True)
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
        """Where the agent writes generated artifacts (new .xlsx, charts, etc.)."""
        return self.data_root / "workspace"

    @property
    def sessions_dir(self) -> Path:
        """CLAUDE_CONFIG_DIR target: keeps SDK session JSONL with the tool's data."""
        return self.data_root / "sessions"

    @property
    def skills_dir(self) -> Path:
        return self.project_root / ".claude" / "skills"

    def ensure_dirs(self) -> None:
        for d in (self.data_root, self.kb_dir, self.workspace_dir, self.sessions_dir):
            d.mkdir(parents=True, exist_ok=True)


def _bool_env(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _int_env(name: str) -> int | None:
    raw = os.getenv(name)
    return int(raw) if raw and raw.strip().isdigit() else None
