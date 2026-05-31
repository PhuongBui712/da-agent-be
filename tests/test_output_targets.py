"""Tests for the (Target, Source) → resolved_target_path resolver (spec §8.2).

Phase A 2026-06-01: ALL targets — including KB-bound and attachment-bound —
resolve flat under `outputs/<session_id>/<filename>`. No `<output_id>`
subdirectory is minted by the resolver. `resolved_target_kind` still reports
`kb_version` / `attachment_version` so the agent's mental model stays intact.

Covers:
- New .xlsx / .pptx / .docx → outputs/<sid>/output.<ext>.
- New sheet / Pick sheet on a READY KB → outputs/<sid>/<source_filename>,
  kind=kb_version.
- New sheet / Pick sheet on an attachment → outputs/<sid>/<source_filename>,
  kind=attachment_version.
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


async def test_new_xlsx_resolves_flat_under_session_dir(state):
    sid = "sess_001"
    res = await _resolve_output_target(
        raw_questions=_make_target_questions(),
        raw_answers=_ans("New .xlsx", "N/A"),
        state=state,
        sid=sid,
    )

    assert res.resolved_target_kind == "standalone"
    p = Path(res.resolved_target_path)
    # Phase A 2026-06-01 layout: outputs/<sid>/output.xlsx (no out_* subdir)
    assert p.parent == state.settings.outputs_session_dir(sid)
    assert p.name == "output.xlsx"


async def test_new_pptx_resolves_flat_under_session_dir(state):
    sid = "sess_001"
    res = await _resolve_output_target(
        raw_questions=_make_target_questions(),
        raw_answers=_ans("New .pptx", "N/A"),
        state=state,
        sid=sid,
    )

    assert res.resolved_target_kind == "standalone"
    p = Path(res.resolved_target_path)
    assert p.parent == state.settings.outputs_session_dir(sid)
    assert p.name == "output.pptx"


async def test_new_docx_resolves_flat_under_session_dir(state):
    sid = "sess_001"
    res = await _resolve_output_target(
        raw_questions=_make_target_questions(),
        raw_answers=_ans("New .docx", "N/A"),
        state=state,
        sid=sid,
    )

    assert res.resolved_target_kind == "standalone"
    p = Path(res.resolved_target_path)
    assert p.parent == state.settings.outputs_session_dir(sid)
    assert p.name == "output.docx"


async def test_new_pptx_ignores_source(state):
    res = await _resolve_output_target(
        raw_questions=_make_target_questions(),
        raw_answers=_ans("New .pptx", "kb_anything"),
        state=state,
        sid="sess_001",
    )

    assert res.resolved_target_kind == "standalone"
    assert Path(res.resolved_target_path).name == "output.pptx"


async def test_new_docx_ignores_source(state):
    res = await _resolve_output_target(
        raw_questions=_make_target_questions(),
        raw_answers=_ans("New .docx", "kb_anything"),
        state=state,
        sid="sess_001",
    )

    assert res.resolved_target_kind == "standalone"
    assert Path(res.resolved_target_path).name == "output.docx"


async def test_unknown_target_lists_all_five_valid(state):
    with pytest.raises(TargetValidationError) as excinfo:
        await _resolve_output_target(
            raw_questions=_make_target_questions(),
            raw_answers=_ans("New .pdf", "N/A"),
            state=state,
            sid="sess_001",
        )
    msg = str(excinfo.value)
    assert "New .xlsx" in msg
    assert "New .pptx" in msg
    assert "New .docx" in msg
    assert "New sheet" in msg
    assert "Pick sheet" in msg


async def test_new_sheet_on_ready_kb_resolves_to_session_dir(state):
    """Phase A 2026-06-01: KB-bound writes land flat under outputs/<sid>/.

    `resolved_target_kind` still reports `kb_version` so the agent / UI
    surface unchanged, but the path is a direct child of the session dir
    using the KB's source filename (so users see `Sales.xlsx`).
    """
    sid = "sess_001"
    kb = await state.kb.create(filename="Sales.xlsx", size_bytes=10)
    await state.kb.update_status(kb.id, "READY")

    res = await _resolve_output_target(
        raw_questions=_make_target_questions(),
        raw_answers=_ans("New sheet", kb.id),
        state=state,
        sid=sid,
    )

    assert res.resolved_target_kind == "kb_version"
    p = Path(res.resolved_target_path)
    assert p.parent == state.settings.outputs_session_dir(sid)
    assert p.name == "Sales.xlsx"  # source filename, not v_curr.xlsx


async def test_pick_sheet_with_sheet_qualifier_resolves(state):
    """Pick sheet on a KB resolves flat under outputs/<sid>/<source_filename>."""
    sid = "sess_001"
    kb = await state.kb.create(filename="Sales.xlsx", size_bytes=10)
    await state.kb.update_status(kb.id, "READY")

    res = await _resolve_output_target(
        raw_questions=_make_target_questions(),
        raw_answers=_ans("Pick sheet", f"{kb.id}::Q1"),
        state=state,
        sid=sid,
    )

    assert res.resolved_target_kind == "kb_version"
    p = Path(res.resolved_target_path)
    assert p.parent == state.settings.outputs_session_dir(sid)
    assert p.name == "Sales.xlsx"


async def test_attachment_target_resolves_flat_under_session_dir(state):
    """Phase A 2026-06-01: attachment-bound writes also land flat under outputs/<sid>/.

    `resolved_target_kind` is `attachment_version` for surface-area parity
    but the path is a direct child of the per-session dir.
    """
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
    p = Path(res.resolved_target_path)
    assert p.parent == state.settings.outputs_session_dir(sid)
    assert p.name == "upload.xlsx"  # source filename, not v_curr.xlsx


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


async def test_attachment_filename_preserved_in_outputs_path(state):
    """The source attachment's filename (e.g. data.csv) is reused as the on-disk
    name under outputs/<sid>/. This is what the user sees in the outputs view,
    so preserving the original name is part of the contract.
    """
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

    p = Path(res.resolved_target_path)
    assert p.name == "data.csv"
    assert p.parent == state.settings.outputs_session_dir(sid)
