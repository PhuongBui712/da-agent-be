"""Cross-session isolation tests for OutputsObserver (Phase C 2026-05-31).

The observer is bound to ONE session_id at construction and only fires for
paths under `outputs/<that_session_id>/<out_*>/<filename>`. Writes that
target a different session's outputs subtree, or the legacy flat
`outputs/<out_*>/...` layout (no session prefix), must be silently ignored.

This guards against the observer cross-registering files into session A's
runtime when the runner sees a Write into session B's directory (e.g. a
buggy agent or a stale path leak).
"""

from __future__ import annotations

import pytest

from da_agent.outputs import OutputDetection, OutputsObserver


@pytest.fixture
def make_observer(tmp_path):
    outputs_dir = tmp_path / "outputs"
    kb_dir = tmp_path / "kb"
    attachments_dir = tmp_path / "attachments"
    outputs_dir.mkdir()
    kb_dir.mkdir()
    attachments_dir.mkdir()

    def _make(session_id: str):
        events: list[OutputDetection] = []
        obs = OutputsObserver(
            outputs_dir,
            session_id,
            kb_dir,
            attachments_dir,
            on_detect=events.append,
        )
        return obs, events, outputs_dir

    return _make


def test_write_under_other_session_does_not_fire(make_observer):
    """Observer for session A must ignore writes into session B's outputs dir."""
    obs, events, outputs_dir = make_observer("sess_a")

    # The other session's directory exists on disk (this can happen in real
    # life since /outputs/ is shared across sessions). Writing into it
    # bypasses the per-session observer.
    other = outputs_dir / "sess_b" / "out_xxxx" / "report.xlsx"
    other.parent.mkdir(parents=True, exist_ok=True)
    obs.observe_tool_use("u1", "Write", {"file_path": str(other)})
    obs.observe_tool_result("u1", "ok", False)

    assert events == []


def test_write_under_legacy_flat_layout_does_not_fire(make_observer):
    """Pre-Phase-C `outputs/<out_*>/<filename>` (no session prefix) is rejected.

    Even though the directory matches the old shape, the new observer only
    matches under `outputs/<session_id>/<out_*>/...`, so the path is
    classified as not-an-output and dropped.
    """
    obs, events, outputs_dir = make_observer("sess_a")

    legacy = outputs_dir / "out_xxxx" / "file.xlsx"
    legacy.parent.mkdir(parents=True, exist_ok=True)
    obs.observe_tool_use("u1", "Write", {"file_path": str(legacy)})
    obs.observe_tool_result("u1", "ok", False)

    assert events == []


def test_bash_redirect_under_other_session_does_not_fire(make_observer):
    """Same isolation rule for `>` / `--output` redirections."""
    obs, events, outputs_dir = make_observer("sess_a")

    other = outputs_dir / "sess_b" / "out_yyyy" / "out.csv"
    obs.observe_tool_use(
        "u1", "Bash", {"command": f"python build.py --output {other}"}
    )
    obs.observe_tool_result("u1", "", False)

    assert events == []
