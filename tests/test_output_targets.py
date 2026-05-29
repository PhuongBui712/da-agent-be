"""Tests for the (Target, Source) → resolved_target_path resolver (spec §8.2).

Covers:
- New .xlsx → outputs/<output_id>/output.xlsx (mints output_id, creates dir)
- New sheet / Pick sheet on a READY KB → kb/<id>/versions/v_curr.<ext>
  with pre-write rotation (existing v_curr → v_prev).
- New sheet / Pick sheet on an attachment → attachments/<sid>/<att_id>/versions/v_curr.<ext>.
- Validation deny: too few answers, unknown ids, non-READY kb, missing source.
- Header-fence: arbitrary clarifications (header != "Target") get no resolved path.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from da_agent.agent.permissions import (
    TargetValidationError,
    _is_output_target_question,
)
from da_agent.config import Settings
from da_agent.server.routes.messages import _resolve_output_target
from da_agent.server.state import AppState


@pytest.fixture
def state(tmp_path, monkeypatch):
    monkeypatch.setenv("DA_AGENT_HOME", str(tmp_path))
    s = Settings()
    s.data_root = tmp_path
    s.ensure_dirs()
    return AppState(s)


def _make_target_questions() -> list[dict]:
    return [
        {"header": "Target", "question": "Where?", "options": []},
        {"header": "Source", "question": "Which?", "options": []},
    ]


def _ans(target: str, source: str) -> list[dict]:
    return [
        {"header": "Target", "selected": [target]},
        {"header": "Source", "selected": [source]},
    ]


async def test_new_xlsx_mints_output_id_and_creates_dir(state):
    res = await _resolve_output_target(
        raw_questions=_make_target_questions(),
        raw_answers=_ans("New .xlsx", "N/A"),
        state=state,
        sid="sess_001",
    )

    assert res.resolved_target_kind == "standalone"
    p = Path(res.resolved_target_path)
    # outputs/<out_*>/output.xlsx
    assert p.parent.parent == state.settings.outputs_dir
    assert p.parent.name.startswith("out_")
    assert p.name == "output.xlsx"
    assert p.parent.is_dir()  # directory was created


async def test_new_sheet_on_ready_kb_resolves_to_v_curr(state):
    kb = await state.kb.create(filename="Sales.xlsx", size_bytes=10)
    await state.kb.update_status(kb.id, "READY")

    res = await _resolve_output_target(
        raw_questions=_make_target_questions(),
        raw_answers=_ans("New sheet", kb.id),
        state=state,
        sid="sess_001",
    )

    assert res.resolved_target_kind == "kb_version"
    expected = state.settings.kb_dir / kb.id / "versions" / "v_curr.xlsx"
    assert Path(res.resolved_target_path) == expected
    assert expected.parent.is_dir()


async def test_pick_sheet_with_sheet_qualifier_resolves(state):
    kb = await state.kb.create(filename="Sales.xlsx", size_bytes=10)
    await state.kb.update_status(kb.id, "READY")

    res = await _resolve_output_target(
        raw_questions=_make_target_questions(),
        raw_answers=_ans("Pick sheet", f"{kb.id}::Q1"),
        state=state,
        sid="sess_001",
    )

    assert res.resolved_target_kind == "kb_version"
    assert Path(res.resolved_target_path).name == "v_curr.xlsx"


async def test_resolution_rotates_existing_v_curr_to_v_prev(state):
    kb = await state.kb.create(filename="Sales.xlsx", size_bytes=10)
    await state.kb.update_status(kb.id, "READY")
    versions_dir = state.settings.kb_dir / kb.id / "versions"
    versions_dir.mkdir(parents=True, exist_ok=True)
    (versions_dir / "v_curr.xlsx").write_bytes(b"first-revision")

    await _resolve_output_target(
        raw_questions=_make_target_questions(),
        raw_answers=_ans("New sheet", kb.id),
        state=state,
        sid="sess_001",
    )

    # The previously-current bytes are now v_prev; v_curr is cleared so the
    # model can drop its new bytes there.
    assert not (versions_dir / "v_curr.xlsx").exists()
    assert (versions_dir / "v_prev.xlsx").read_bytes() == b"first-revision"


async def test_attachment_target_resolves_under_session(state):
    sid = "sess_42"
    att = await state.attachments.create(
        sid, filename="upload.xlsx", size_bytes=10, mime="application/x-xlsx"
    )

    res = await _resolve_output_target(
        raw_questions=_make_target_questions(),
        raw_answers=_ans("New sheet", att.id),
        state=state,
        sid=sid,
    )

    assert res.resolved_target_kind == "attachment_version"
    expected = (
        state.settings.attachments_dir / sid / att.id / "versions" / "v_curr.xlsx"
    )
    assert Path(res.resolved_target_path) == expected


async def test_unknown_kb_id_raises_validation_error(state):
    with pytest.raises(TargetValidationError):
        await _resolve_output_target(
            raw_questions=_make_target_questions(),
            raw_answers=_ans("New sheet", "kb_does_not_exist"),
            state=state,
            sid="sess_001",
        )


async def test_non_ready_kb_raises_validation_error(state):
    kb = await state.kb.create(filename="Sales.xlsx", size_bytes=10)
    # left in PENDING

    with pytest.raises(TargetValidationError):
        await _resolve_output_target(
            raw_questions=_make_target_questions(),
            raw_answers=_ans("New sheet", kb.id),
            state=state,
            sid="sess_001",
        )


async def test_unknown_attachment_id_raises(state):
    with pytest.raises(TargetValidationError):
        await _resolve_output_target(
            raw_questions=_make_target_questions(),
            raw_answers=_ans("New sheet", "att_unknown"),
            state=state,
            sid="sess_001",
        )


async def test_new_sheet_without_source_raises(state):
    with pytest.raises(TargetValidationError):
        await _resolve_output_target(
            raw_questions=_make_target_questions(),
            raw_answers=_ans("New sheet", "N/A"),
            state=state,
            sid="sess_001",
        )


async def test_too_few_answers_raises(state):
    with pytest.raises(TargetValidationError):
        await _resolve_output_target(
            raw_questions=_make_target_questions(),
            raw_answers=[{"header": "Target", "selected": ["New .xlsx"]}],
            state=state,
            sid="sess_001",
        )


async def test_unknown_target_label_raises(state):
    with pytest.raises(TargetValidationError):
        await _resolve_output_target(
            raw_questions=_make_target_questions(),
            raw_answers=_ans("Edit in place", "N/A"),
            state=state,
            sid="sess_001",
        )


def test_header_fence_skips_non_target_questions():
    """`_is_output_target_question` only fires when first header == 'Target'."""
    assert _is_output_target_question([{"header": "Target", "question": "Where?"}])
    assert not _is_output_target_question(
        [{"header": "Plan_strictness", "question": "Strict?"}]
    )
    assert not _is_output_target_question([])
    # First-question dependence: a 'Target' in position 2 doesn't trigger.
    assert not _is_output_target_question(
        [
            {"header": "Sanity", "question": "?"},
            {"header": "Target", "question": "?"},
        ]
    )


async def test_attachment_extension_inferred_from_filename(state):
    sid = "sess_42"
    att = await state.attachments.create(
        sid, filename="data.csv", size_bytes=10, mime="text/csv"
    )

    res = await _resolve_output_target(
        raw_questions=_make_target_questions(),
        raw_answers=_ans("Pick sheet", att.id),
        state=state,
        sid=sid,
    )

    assert Path(res.resolved_target_path).name == "v_curr.csv"
