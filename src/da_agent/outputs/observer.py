"""OutputsObserver — parallel to `TodoStore` (spec §8.2, §8.4).

Watches Write/Edit/Bash tool calls; on tool_result without error, classifies
the input.file_path or Bash command for paths under `outputs_dir/<id>/` or
`kb_dir/<kb_id>/versions/v<N>.xlsx`. Emits a detection through `on_detect`;
the runner bridges that into the async registry + UI.

Conservative by design: ambiguous matches are dropped silently. Better to
under-register than mis-register (Anti-Pattern §13).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Literal

_VERSION_FILE_RE = re.compile(r"^v(\d+)\.xlsx$")
# Bash: `> path` redirection or an `--output` flag. Conservative: only flags
# we know mean "output target".
_BASH_REDIR_RE = re.compile(r"(?:>\s*|--output[= ])(\S+)")
_WRITE_TOOLS = {"Write", "Edit", "NotebookEdit"}


@dataclass(slots=True)
class OutputDetection:
    """Either a standalone (`outputs/<id>/<filename>`) or a KB version
    (`kb/<kb_id>/versions/v<N>.xlsx`)."""

    kind: Literal["standalone", "kb_version"]
    file_path: Path
    output_id: str | None = None              # standalone — outputs dir name
    filename: str | None = None               # standalone — relative path under <id>/
    kb_id: str | None = None                  # kb_version
    version: str | None = None                # "v<N>"


class OutputsObserver:
    def __init__(
        self,
        outputs_dir: Path,
        kb_dir: Path,
        on_detect: Callable[[OutputDetection], None],
    ) -> None:
        self._outputs_dir = outputs_dir.resolve()
        self._kb_dir = kb_dir.resolve()
        self._on_detect = on_detect
        # Per-turn pending tool_use entries (input was Write/Edit/Bash).
        # Cleared on `reset()`.
        self._pending: dict[str, dict[str, Any]] = {}
        # tool_use_ids whose detection has fired — guards against duplicate
        # emissions if the SDK forwards a tool_result twice.
        self._fired: set[str] = set()

    def reset(self) -> None:
        self._pending.clear()
        self._fired.clear()

    def observe_tool_use(
        self, tool_use_id: str, name: str, tool_input: dict[str, Any]
    ) -> None:
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
                self._on_detect(det)
                return  # one detection per tool_use is enough

    def _classify(self, path: Path) -> OutputDetection | None:
        try:
            resolved = path if path.is_absolute() else (self._outputs_dir / path)
            resolved = resolved.resolve(strict=False)
        except OSError:
            return None
        if _is_under(resolved, self._outputs_dir):
            rel = resolved.relative_to(self._outputs_dir)
            parts = rel.parts
            if len(parts) >= 2 and parts[0].startswith("out_"):
                return OutputDetection(
                    kind="standalone",
                    file_path=resolved,
                    output_id=parts[0],
                    filename="/".join(parts[1:]),
                )
            return None
        if _is_under(resolved, self._kb_dir):
            rel = resolved.relative_to(self._kb_dir)
            parts = rel.parts
            if (
                len(parts) == 3
                and parts[0].startswith("kb_")
                and parts[1] == "versions"
                and _VERSION_FILE_RE.match(parts[2])
            ):
                return OutputDetection(
                    kind="kb_version",
                    file_path=resolved,
                    kb_id=parts[0],
                    version=parts[2].rsplit(".", 1)[0],
                )
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
