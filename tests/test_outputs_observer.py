"""Pure-unit tests for OutputsObserver (spec §8.2).

Phase A 2026-06-01: the observer only emits `standalone` detections under the
per-session layout

    outputs/<session_id>/<filename>

Direct children of `outputs/<session_id>/` only — anything 2+ levels deep,
or sidecar `.<output_id>.meta.json` files, are rejected.

The `kb_version` and `attachment_version` branches are DEPRECATED — they
remain in code for type stability but `_classify` returns None for any path
under `kb_dir` or `attachments_dir`.
"""

from __future__ import annotations

import pytest

from da_agent.outputs import OutputDetection, OutputsObserver

# Fixed sid used by every test that constructs an observer. We keep it
# constant so assertions can hard-code the expected session-scoped path.
SID = "sess_test_abc"


@pytest.fixture
def dirs(tmp_path):
    outputs_dir = tmp_path / "outputs"
    kb_dir = tmp_path / "kb"
    attachments_dir = tmp_path / "attachments"
    outputs_dir.mkdir()
    (outputs_dir / SID).mkdir()
    kb_dir.mkdir()
    attachments_dir.mkdir()
    return outputs_dir, kb_dir, attachments_dir


@pytest.fixture
def make_observer(dirs):
    outputs_dir, kb_dir, attachments_dir = dirs

    def _make():
        events: list[OutputDetection] = []
        obs = OutputsObserver(
            outputs_dir, SID, kb_dir, attachments_dir, on_detect=events.append
        )
        return obs, events

    return _make


def test_write_direct_child_of_session_dir_fires(dirs, make_observer):
    """Phase A: direct child of outputs/<sid>/ is standalone."""
    outputs_dir, _, _ = dirs
    obs, events = make_observer()

    file_path = outputs_dir / SID / "report.xlsx"
    obs.observe_tool_use("u1", "Write", {"file_path": str(file_path)})
    obs.observe_tool_result("u1", "ok", False)

    assert len(events) == 1
    det = events[0]
    assert det.kind == "standalone"
    assert det.filename == "report.xlsx"
    assert det.session_id == SID


def test_tool_result_with_is_error_does_not_fire(dirs, make_observer):
    outputs_dir, _, _ = dirs
    obs, events = make_observer()

    obs.observe_tool_use(
        "u1", "Write", {"file_path": str(outputs_dir / SID / "x.xlsx")}
    )
    obs.observe_tool_result("u1", "permission denied", True)

    assert events == []


def test_bash_redirect_outside_known_roots_does_not_fire(dirs, make_observer):
    obs, events = make_observer()

    obs.observe_tool_use("u1", "Bash", {"command": "echo hi > /tmp/x.txt"})
    obs.observe_tool_result("u1", "", False)

    assert events == []


def test_bash_redirect_under_session_outputs_fires_standalone(dirs, make_observer):
    """A `>` redirect into outputs/<sid>/<filename> classifies as standalone."""
    outputs_dir, _, _ = dirs
    obs, events = make_observer()

    target = outputs_dir / SID / "report.xlsx"
    obs.observe_tool_use(
        "u1",
        "Bash",
        {"command": f"python script.py --output {target}"},
    )
    obs.observe_tool_result("u1", "", False)

    assert len(events) == 1
    det = events[0]
    assert det.kind == "standalone"
    assert det.filename == "report.xlsx"


def test_kb_version_now_returns_none_deprecated(dirs, make_observer):
    """DEPRECATED 2026-05-31: KB-bound writes no longer emit detections.

    Phase C routes them through the standalone layout instead, so the
    observer must return None for any path under `kb_dir`.
    """
    _, kb_dir, _ = dirs
    obs, events = make_observer()

    target = kb_dir / "kb_xyz" / "versions" / "v_curr.xlsx"
    obs.observe_tool_use(
        "u1",
        "Bash",
        {"command": f"python script.py --output {target}"},
    )
    obs.observe_tool_result("u1", "", False)

    assert events == []


def test_write_two_levels_deep_does_not_fire(dirs, make_observer):
    """2-level deep path (outputs/<sid>/subdir/file.xlsx) is rejected."""
    outputs_dir, _, _ = dirs
    obs, events = make_observer()

    file_path = outputs_dir / SID / "some_random_dir" / "file.xlsx"
    obs.observe_tool_use("u1", "Write", {"file_path": str(file_path)})
    obs.observe_tool_result("u1", "", False)

    assert events == []


def test_duplicate_tool_result_fires_only_once(dirs, make_observer):
    outputs_dir, _, _ = dirs
    obs, events = make_observer()

    file_path = outputs_dir / SID / "report.xlsx"
    obs.observe_tool_use("u1", "Write", {"file_path": str(file_path)})
    obs.observe_tool_result("u1", "", False)
    obs.observe_tool_result("u1", "", False)  # second time -> no-op

    assert len(events) == 1


def test_legacy_numbered_kb_version_does_not_fire(dirs, make_observer):
    """The old `v<N>.xlsx` layout under kb_dir is no longer accepted (Phase C)."""
    _, kb_dir, _ = dirs
    obs, events = make_observer()

    for legacy in ("v3.xlsx", "v0.5.xlsx", "v_now.xlsx"):
        target = kb_dir / "kb_xyz" / "versions" / legacy
        obs.observe_tool_use(f"u-{legacy}", "Write", {"file_path": str(target)})
        obs.observe_tool_result(f"u-{legacy}", "", False)

    assert events == []


def test_attachment_version_now_returns_none_deprecated(dirs, make_observer):
    """DEPRECATED 2026-05-31: attachment-bound writes no longer emit detections.

    Phase C routes them through the standalone layout, so any path under
    `attachments_dir` must yield None from `_classify`.
    """
    _, _, attachments_dir = dirs
    obs, events = make_observer()

    target = attachments_dir / "sess_001" / "att_001" / "versions" / "v_curr.xlsx"
    obs.observe_tool_use("u1", "Write", {"file_path": str(target)})
    obs.observe_tool_result("u1", "", False)

    assert events == []


def test_attachment_version_prev_now_returns_none_deprecated(dirs, make_observer):
    """DEPRECATED 2026-05-31: same as v_curr — v_prev attachment writes no longer emit."""
    _, _, attachments_dir = dirs
    obs, events = make_observer()

    target = attachments_dir / "sess_001" / "att_001" / "versions" / "v_prev.csv"
    obs.observe_tool_use("u1", "Write", {"file_path": str(target)})
    obs.observe_tool_result("u1", "", False)

    assert events == []


def test_attachment_unknown_extension_does_not_fire(dirs, make_observer):
    _, _, attachments_dir = dirs
    obs, events = make_observer()

    target = attachments_dir / "sess_001" / "att_001" / "versions" / "v_curr.json"
    obs.observe_tool_use("u1", "Write", {"file_path": str(target)})
    obs.observe_tool_result("u1", "", False)

    assert events == []


def test_attachment_missing_att_prefix_does_not_fire(dirs, make_observer):
    _, _, attachments_dir = dirs
    obs, events = make_observer()

    # Notice "abc" instead of "att_..." in the attachment id slot.
    target = attachments_dir / "sess_001" / "abc" / "versions" / "v_curr.xlsx"
    obs.observe_tool_use("u1", "Write", {"file_path": str(target)})
    obs.observe_tool_result("u1", "", False)

    assert events == []


def test_reset_clears_pending_state(dirs, make_observer):
    outputs_dir, _, _ = dirs
    obs, events = make_observer()

    obs.observe_tool_use(
        "u1", "Write", {"file_path": str(outputs_dir / SID / "x.xlsx")}
    )
    obs.reset()
    obs.observe_tool_result("u1", "", False)

    assert events == []


def test_non_write_non_bash_tool_is_ignored(dirs, make_observer):
    outputs_dir, _, _ = dirs
    obs, events = make_observer()

    obs.observe_tool_use(
        "u1",
        "Read",
        {"file_path": str(outputs_dir / SID / "x.xlsx")},
    )
    obs.observe_tool_result("u1", "ok", False)

    assert events == []


def test_kb_version_xlsm_returns_none_deprecated(dirs, make_observer):
    """DEPRECATED 2026-05-31: KB writes no longer emit, regardless of extension."""
    _, kb_dir, _ = dirs
    obs, events = make_observer()

    target = kb_dir / "kb_macro" / "versions" / "v_curr.xlsm"
    obs.observe_tool_use("u1", "Write", {"file_path": str(target)})
    obs.observe_tool_result("u1", "", False)

    assert events == []


def test_sidecar_file_does_not_fire(dirs, make_observer):
    """Sidecar `.out_<hex>.meta.json` files under outputs/<sid>/ are rejected."""
    outputs_dir, _, _ = dirs
    obs, events = make_observer()

    sidecar = outputs_dir / SID / ".out_abcdef0123456789.meta.json"
    obs.observe_tool_use("u1", "Write", {"file_path": str(sidecar)})
    obs.observe_tool_result("u1", "", False)

    assert events == []


def test_observer_detects_python_heredoc_write_via_dir_scan(tmp_path):
    """Bug #4 regression: python3 -c "wb.save(path)" has no shell redirect.
    Observer must still detect via post-result dir-snapshot diff."""
    sid = "sess_test_heredoc"
    outputs_dir = tmp_path / "outputs"
    session_dir = outputs_dir / sid
    session_dir.mkdir(parents=True)

    fired: list[OutputDetection] = []
    obs = OutputsObserver(
        outputs_dir=outputs_dir,
        session_id=sid,
        kb_dir=tmp_path / "kb",
        attachments_dir=tmp_path / "attachments",
        on_detect=fired.append,
    )

    # Pre-tool snapshot: empty dir
    obs.observe_tool_use(
        "tool_001",
        "Bash",
        {"command": 'python3 -c "from openpyxl import Workbook; wb=Workbook(); wb.save(\\"/tmp/x.xlsx\\")"'},
    )

    # Simulate the heredoc actually writing the file
    target = session_dir / "report.xlsx"
    target.write_bytes(b"PK\x03\x04" + b"\x00" * 200)  # tiny zip-magic-prefixed blob

    obs.observe_tool_result("tool_001", [{"type": "text", "text": "done"}], False)

    assert len(fired) == 1, f"expected 1 detection, got {len(fired)}: {fired}"
    assert fired[0].filename == "report.xlsx"
    assert fired[0].session_id == sid


def test_observer_detects_shutil_copy_to_outputs(tmp_path):
    """shutil.copy() has no bash redirect; dir scan catches it."""
    sid = "sess_test_shutil"
    outputs_dir = tmp_path / "outputs"
    session_dir = outputs_dir / sid
    session_dir.mkdir(parents=True)

    fired: list[OutputDetection] = []
    obs = OutputsObserver(
        outputs_dir=outputs_dir, session_id=sid,
        kb_dir=tmp_path / "kb", attachments_dir=tmp_path / "attachments",
        on_detect=fired.append,
    )

    obs.observe_tool_use(
        "tool_002", "Bash",
        {"command": 'python3 -c "import shutil; shutil.copy(\\"/tmp/a.xlsx\\", \\"/data/outputs/sess_test_shutil/copy.xlsx\\")"'},
    )
    target = session_dir / "copy.xlsx"
    target.write_bytes(b"PK\x03\x04" + b"\x00" * 100)
    obs.observe_tool_result("tool_002", [{"type": "text", "text": "done"}], False)

    assert len(fired) == 1
    assert fired[0].filename == "copy.xlsx"


def test_observer_ignores_sidecar_meta_files(tmp_path):
    """Sidecar `.<oid>.meta.json` written by registry must not register."""
    sid = "sess_test_sidecar"
    outputs_dir = tmp_path / "outputs"
    session_dir = outputs_dir / sid
    session_dir.mkdir(parents=True)

    fired: list[OutputDetection] = []
    obs = OutputsObserver(
        outputs_dir=outputs_dir, session_id=sid,
        kb_dir=tmp_path / "kb", attachments_dir=tmp_path / "attachments",
        on_detect=fired.append,
    )

    obs.observe_tool_use("tool_003", "Bash", {"command": "echo noop"})
    # Simulate registry writing a sidecar
    sidecar = session_dir / ".out_abc123def456789a.meta.json"
    sidecar.write_text('{"output_id": "out_abc123def456789a"}')
    obs.observe_tool_result("tool_003", [{"type": "text", "text": "ok"}], False)

    assert len(fired) == 0, f"sidecar must not fire: {fired}"


def test_observer_dedup_across_multiple_tool_results(tmp_path):
    """Two tool_results, second touches no new file → still 1 detection only."""
    sid = "sess_test_dedup"
    outputs_dir = tmp_path / "outputs"
    session_dir = outputs_dir / sid
    session_dir.mkdir(parents=True)

    fired: list[OutputDetection] = []
    obs = OutputsObserver(
        outputs_dir=outputs_dir, session_id=sid,
        kb_dir=tmp_path / "kb", attachments_dir=tmp_path / "attachments",
        on_detect=fired.append,
    )

    # First tool: writes file
    obs.observe_tool_use("tool_a", "Bash", {"command": "python3 -c '...'"})
    (session_dir / "first.xlsx").write_bytes(b"PK\x03\x04" + b"\x00" * 100)
    obs.observe_tool_result("tool_a", [{"type": "text", "text": "ok"}], False)
    assert len(fired) == 1

    # Second tool: reads only (no new file in dir)
    obs.observe_tool_use("tool_b", "Bash", {"command": "echo verify"})
    obs.observe_tool_result("tool_b", [{"type": "text", "text": "ok"}], False)
    assert len(fired) == 1, "no new file → no new detection"
