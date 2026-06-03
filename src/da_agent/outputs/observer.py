"""OutputsObserver — parallel to `TodoStore`.

Watches Write/Edit/Bash tool calls; on tool_result without error, classifies
the input.file_path or Bash command for paths under the session-scoped
outputs layout:

  outputs/<session_id>/<filename>          -> standalone

Direct children of `outputs/<session_id>/` only — anything 2+ levels deep,
or sidecar `.<output_id>.meta.json` files, are rejected.

The `kb_version` and `attachment_version` branches no longer fire — KB-bound
and attachment-bound writes are routed through the standalone layout via
`resolved_target_path`.

Emits a detection through `on_detect`; the runner bridges that into the
async registry + UI.

Conservative by design: ambiguous matches are dropped silently. Better to
under-register than mis-register.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Literal

# Bash: `> path` / `>> path` redirection or an `--output` flag. Conservative:
# the captured token MUST be an absolute path (`/...`) and must NOT include
# shell metacharacters that would split a token. Three classes of false
# positive must be rejected:
#   1. FD redirects: `2>&1`, `1>&2`, `&>file` — preceded by digit or `&`.
#   2. Token starts with `&` (e.g. `>&1`).
#   3. Bare numeric or relative tokens from non-shell contexts, e.g. Python
#      comparisons `[b for b in data if b > 127]` (matches `> 127`).
# We require the captured path to start with `/`. The post-result dir-scan
# branch is the safety net for writes that don't fit this strict shape.
_BASH_REDIR_RE = re.compile(
    r"(?:(?<![\d&])>{1,2}\s*|--output[= ])(/[^\s;|&<>]+)"
)
_WRITE_TOOLS = {"Write", "Edit", "NotebookEdit"}
_SIDECAR_RE = re.compile(r"^\.out_[0-9a-f]{16}\.meta\.json$")


@dataclass(slots=True)
class OutputDetection:
    """One detected output. Only `standalone` is emitted in practice; the other
    two literals are retained for type stability."""

    kind: Literal["standalone", "kb_version", "attachment_version"]
    file_path: Path
    filename: str
    session_id: str


class OutputsObserver:
    def __init__(
        self,
        outputs_dir: Path,
        session_id: str,
        kb_dir: Path,
        attachments_dir: Path,
        on_detect: Callable[[OutputDetection], None],
    ) -> None:
        self._outputs_dir = outputs_dir.resolve()
        self._session_id = session_id
        # Cache the resolved per-session root so `_classify` can match it
        # without rebuilding the path on every tool call.
        self._session_outputs_dir = (outputs_dir / session_id).resolve()
        self._kb_dir = kb_dir.resolve()
        self._attachments_dir = attachments_dir.resolve()
        self._on_detect = on_detect
        # Per-turn pending tool_use entries (input was Write/Edit/Bash).
        # Cleared on `reset()`.
        self._pending: dict[str, dict[str, Any]] = {}
        # tool_use_ids whose detection has fired — guards against duplicate
        # emissions if the SDK forwards a tool_result twice.
        self._fired: set[str] = set()
        # Paths already emitted via the dir-scan path — prevents re-firing the
        # same file on a subsequent tool_result if the snapshot wasn't refreshed.
        self._fired_paths: set[Path] = set()
        # Snapshot of non-sidecar files directly under the session outputs dir.
        # Refreshed at every observe_tool_use; diffed at every observe_tool_result.
        self._dir_snapshot: set[Path] = self._snapshot_dir()

    def reset(self) -> None:
        self._pending.clear()
        self._fired.clear()
        self._fired_paths.clear()

    def _snapshot_dir(self) -> set[Path]:
        """Return current set of non-sidecar files directly under the session
        outputs dir. Returns empty set if dir doesn't exist yet."""
        if not self._session_outputs_dir.exists():
            return set()
        return {
            p for p in self._session_outputs_dir.iterdir()
            if p.is_file() and not _SIDECAR_RE.match(p.name)
        }

    def observe_tool_use(
        self, tool_use_id: str, name: str, tool_input: dict[str, Any]
    ) -> None:
        if name in {"Bash", "Write", "Edit", "NotebookEdit"}:
            self._dir_snapshot = self._snapshot_dir()
        if name in _WRITE_TOOLS:
            fp = tool_input.get("file_path") or tool_input.get("path")
            if isinstance(fp, str):
                self._pending[tool_use_id] = {"kind": "write", "file_path": fp}
        elif name == "Bash":
            cmd = tool_input.get("command")
            if isinstance(cmd, str):
                self._pending[tool_use_id] = {"kind": "bash", "command": cmd}

    def observe_tool_result(
        self, tool_use_id: str, content: Any, is_error: bool
    ) -> None:
        del content
        if is_error or tool_use_id in self._fired:
            self._pending.pop(tool_use_id, None)
            return
        rec = self._pending.pop(tool_use_id, None)
        if rec is None:
            return
        candidates: list[Path] = []
        if rec["kind"] == "write":
            p = _safe_path(rec["file_path"])
            if p is not None:
                candidates.append(p)
        elif rec["kind"] == "bash":
            for m in _BASH_REDIR_RE.finditer(rec["command"]):
                p = _safe_path(m.group(1).strip("'\""))
                if p is not None:
                    candidates.append(p)
        for p in candidates:
            det = self._classify(p)
            if det is not None:
                self._fired.add(tool_use_id)
                self._fired_paths.add(det.file_path)
                self._on_detect(det)
                return  # one detection per tool_use is enough

        # Post-result directory scan — catches writes that didn't go through a
        # shell redirect (Python heredoc, shutil.copy, pd.to_excel, Write tool).
        current = self._snapshot_dir()
        new_paths = current - self._dir_snapshot
        for path in sorted(new_paths):
            if path in self._fired_paths:
                continue
            det = self._classify(path)
            if det is not None:
                self._fired_paths.add(path)
                self._on_detect(det)
        self._dir_snapshot = current

    def _classify(self, path: Path) -> OutputDetection | None:
        try:
            # Relative paths resolve against the per-session outputs dir so
            # the agent can pass either absolute or relative `file_path`.
            resolved = (
                path
                if path.is_absolute()
                else (self._session_outputs_dir / path)
            )
            resolved = resolved.resolve(strict=False)
        except OSError:
            return None
        # Require direct child of `outputs/<session_id>/`. Reject deeper paths and sidecar files.
        if resolved.parent == self._session_outputs_dir:
            if _SIDECAR_RE.match(resolved.name):
                return None
            return OutputDetection(
                kind="standalone",
                file_path=resolved,
                filename=resolved.name,
                session_id=self._session_id,
            )
        # KB writes now redirect to outputs/<sid>/; this branch never emits.
        if _is_under(resolved, self._kb_dir):
            return None
        # Attachment-bound writes also redirect; this branch never emits.
        if _is_under(resolved, self._attachments_dir):
            return None
        return None


def _safe_path(raw: str | None) -> Path | None:
    if not raw:
        return None
    try:
        return Path(raw)
    except (TypeError, ValueError):
        return None


def _is_under(p: Path, root: Path) -> bool:
    try:
        p.relative_to(root)
        return True
    except ValueError:
        return False
