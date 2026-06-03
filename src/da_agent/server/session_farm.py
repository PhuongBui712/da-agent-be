"""Per-session symlink farm for scoped filesystem visibility.

`kb_scope` constrains what the agent can `Glob` / `Read` by giving the SDK
`add_dirs` per-session paths under `<sessions-data>/<sid>/{kb,workspace,outputs}/`
instead of the global `kb_dir` / `outputs_dir` / `attachments_dir` roots.

`prepare_session_root` is idempotent — call on session bootstrap.
`rebuild_kb_symlinks` is per-turn — diff the existing symlink set against
the desired scope and emit only delta operations.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

from ..config import Settings
from .scope import ScopeBlock

_LOG = logging.getLogger(__name__)


def prepare_session_root(settings: Settings, session_id: str) -> None:
    """Idempotent bootstrap: mkdir kb/, workspace/, canonical outputs/<sid>/.

    Safe to call on every message; cheap when already populated.

    No longer creates a `<sessions-data>/<sid>/outputs/` symlink — the
    sandbox follows symlinks and rejects writes through the alias as
    cross-device. `add_dirs` lists the canonical `outputs_dir/<sid>/`
    directly. Existing alias symlinks are removed for cleanliness.
    """
    settings.session_data_dir(session_id).mkdir(parents=True, exist_ok=True)
    settings.session_kb_dir(session_id).mkdir(parents=True, exist_ok=True)
    settings.session_workspace_dir(session_id).mkdir(parents=True, exist_ok=True)

    # Canonical outputs root must exist for the SDK to mount it via add_dirs
    # and for OutputsObserver to snapshot it.
    (settings.outputs_dir / session_id).mkdir(parents=True, exist_ok=True)

    # Tear down any stale alias from older builds.
    view = settings.session_outputs_view_dir(session_id)
    if view.is_symlink():
        try:
            view.unlink()
        except OSError:
            _LOG.warning("failed to remove stale outputs alias %s", view)


def rebuild_kb_symlinks(
    settings: Settings, session_id: str, scope: ScopeBlock
) -> None:
    """Per-turn: align symlinks under `session_kb_dir` to `scope.kb_entries`.

    Each in-scope kb_id gets a symlink `<session_kb_dir>/<kb_id> → <kb_dir>/<kb_id>`.
    Stale entries (symlinks not in current scope) are unlinked. Real files /
    real dirs are NEVER touched — this only manages symlinks.
    """
    kb_view = settings.session_kb_dir(session_id)
    kb_view.mkdir(parents=True, exist_ok=True)

    desired: dict[str, Path] = {}
    for entry in scope.kb_entries:
        target = settings.kb_dir / entry.kb_id
        if target.exists():
            desired[entry.kb_id] = target

    existing: set[str] = set()
    for child in kb_view.iterdir():
        if child.is_symlink():
            existing.add(child.name)

    # Add missing
    for kb_id, target in desired.items():
        link = kb_view / kb_id
        if kb_id in existing:
            continue
        try:
            os.symlink(target, link, target_is_directory=True)
        except FileExistsError:
            pass  # race: someone else created it; fine

    # Remove stale (only symlinks)
    for name in existing - set(desired):
        link = kb_view / name
        try:
            link.unlink()
        except FileNotFoundError:
            pass
