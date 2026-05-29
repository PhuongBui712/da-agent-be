"""Pure-unit tests for OutputsObserver (spec §8.2).

Spec §8.2 — three detection kinds:
  * standalone         — outputs/<out_id>/<filename>
  * kb_version         — kb/<kb_id>/versions/v_(curr|prev).<ext>
  * attachment_version — attachments/<sid>/<att_id>/versions/v_(curr|prev).<ext>

Versions are capped at 2 slots (v_curr, v_prev) per spec §8.2; the legacy
`v<N>.xlsx` shape is intentionally rejected.
"""

from __future__ import annotations

import pytest

from da_agent.outputs import OutputDetection, OutputsObserver


@pytest.fixture
def dirs(tmp_path):
    outputs_dir = tmp_path / "outputs"
    kb_dir = tmp_path / "kb"
    attachments_dir = tmp_path / "attachments"
    outputs_dir.mkdir()
    kb_dir.mkdir()
    attachments_dir.mkdir()
    return outputs_dir, kb_dir, attachments_dir


@pytest.fixture
def make_observer(dirs):
    outputs_dir, kb_dir, attachments_dir = dirs

    def _make():
        events: list[OutputDetection] = []
        obs = OutputsObserver(
            outputs_dir, kb_dir, attachments_dir, on_detect=events.append
        )
        return obs, events

    return _make


def test_write_under_outputs_dir_with_out_prefix_fires(dirs, make_observer):
    outputs_dir, _, _ = dirs
    obs, events = make_observer()

    file_path = outputs_dir / "out_abc" / "report.xlsx"
    obs.observe_tool_use("u1", "Write", {"file_path": str(file_path)})
    obs.observe_tool_result("u1", "ok", False)

    assert len(events) == 1
    det = events[0]
    assert det.kind == "standalone"
    assert det.output_id == "out_abc"
    assert det.filename == "report.xlsx"


def test_tool_result_with_is_error_does_not_fire(dirs, make_observer):
    outputs_dir, _, _ = dirs
    obs, events = make_observer()

    obs.observe_tool_use(
        "u1", "Write", {"file_path": str(outputs_dir / "out_abc" / "x.xlsx")}
    )
    obs.observe_tool_result("u1", "permission denied", True)

    assert events == []


def test_bash_redirect_outside_known_roots_does_not_fire(dirs, make_observer):
    obs, events = make_observer()

    obs.observe_tool_use("u1", "Bash", {"command": "echo hi > /tmp/x.txt"})
    obs.observe_tool_result("u1", "", False)

    assert events == []


def test_bash_output_flag_under_kb_versions_fires_kb_version(dirs, make_observer):
    _, kb_dir, _ = dirs
    obs, events = make_observer()

    target = kb_dir / "kb_xyz" / "versions" / "v_curr.xlsx"
    obs.observe_tool_use(
        "u1",
        "Bash",
        {"command": f"python script.py --output {target}"},
    )
    obs.observe_tool_result("u1", "", False)

    assert len(events) == 1
    det = events[0]
    assert det.kind == "kb_version"
    assert det.kb_id == "kb_xyz"
    assert det.version == "v_curr"


def test_write_under_outputs_dir_without_out_prefix_does_not_fire(dirs, make_observer):
    outputs_dir, _, _ = dirs
    obs, events = make_observer()

    file_path = outputs_dir / "some_random_dir" / "file.xlsx"
    obs.observe_tool_use("u1", "Write", {"file_path": str(file_path)})
    obs.observe_tool_result("u1", "", False)

    assert events == []


def test_duplicate_tool_result_fires_only_once(dirs, make_observer):
    outputs_dir, _, _ = dirs
    obs, events = make_observer()

    file_path = outputs_dir / "out_abc" / "report.xlsx"
    obs.observe_tool_use("u1", "Write", {"file_path": str(file_path)})
    obs.observe_tool_result("u1", "", False)
    obs.observe_tool_result("u1", "", False)  # second time -> no-op

    assert len(events) == 1


def test_legacy_numbered_kb_version_does_not_fire(dirs, make_observer):
    """The old `v<N>.xlsx` layout is no longer accepted (spec §8.2 — 2-slot cap)."""
    _, kb_dir, _ = dirs
    obs, events = make_observer()

    for legacy in ("v3.xlsx", "v0.5.xlsx", "v_now.xlsx"):
        target = kb_dir / "kb_xyz" / "versions" / legacy
        obs.observe_tool_use(f"u-{legacy}", "Write", {"file_path": str(target)})
        obs.observe_tool_result(f"u-{legacy}", "", False)

    assert events == []


def test_attachment_version_curr_fires(dirs, make_observer):
    _, _, attachments_dir = dirs
    obs, events = make_observer()

    target = attachments_dir / "sess_001" / "att_001" / "versions" / "v_curr.xlsx"
    obs.observe_tool_use("u1", "Write", {"file_path": str(target)})
    obs.observe_tool_result("u1", "", False)

    assert len(events) == 1
    det = events[0]
    assert det.kind == "attachment_version"
    assert det.session_id == "sess_001"
    assert det.attachment_id == "att_001"
    assert det.version == "v_curr"


def test_attachment_version_prev_also_fires(dirs, make_observer):
    """v_prev writes are unusual but still classified — the bridge layer
    can decide what (if anything) to do with them."""
    _, _, attachments_dir = dirs
    obs, events = make_observer()

    target = attachments_dir / "sess_001" / "att_001" / "versions" / "v_prev.csv"
    obs.observe_tool_use("u1", "Write", {"file_path": str(target)})
    obs.observe_tool_result("u1", "", False)

    assert len(events) == 1
    assert events[0].kind == "attachment_version"
    assert events[0].version == "v_prev"


def test_attachment_version_unknown_extension_does_not_fire(dirs, make_observer):
    _, _, attachments_dir = dirs
    obs, events = make_observer()

    target = attachments_dir / "sess_001" / "att_001" / "versions" / "v_curr.json"
    obs.observe_tool_use("u1", "Write", {"file_path": str(target)})
    obs.observe_tool_result("u1", "", False)

    assert events == []


def test_attachment_version_missing_att_prefix_does_not_fire(dirs, make_observer):
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
        "u1", "Write", {"file_path": str(outputs_dir / "out_abc" / "x.xlsx")}
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
        {"file_path": str(outputs_dir / "out_abc" / "x.xlsx")},
    )
    obs.observe_tool_result("u1", "ok", False)

    assert events == []


def test_kb_version_xlsm_extension_fires(dirs, make_observer):
    _, kb_dir, _ = dirs
    obs, events = make_observer()

    target = kb_dir / "kb_macro" / "versions" / "v_curr.xlsm"
    obs.observe_tool_use("u1", "Write", {"file_path": str(target)})
    obs.observe_tool_result("u1", "", False)

    assert len(events) == 1
    assert events[0].kind == "kb_version"
    assert events[0].version == "v_curr"
