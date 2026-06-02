"""Tests for the system prompt builder.

Verifies that:
1. The builder returns the SDK SystemPromptPreset dict shape (preset = claude_code).
2. The append text contains the mandatory contract tokens (AskUserQuestion,
   the 3 Target labels, the resolved-path field name).
3. The append text does NOT mention `workspace` (deprecated per spec §8.2).
4. The builder interpolates the runtime paths.
"""

from __future__ import annotations

from pathlib import Path

from da_agent.agent.prompts import build_system_prompt
from da_agent.config import Settings


def _settings(tmp_path: Path) -> Settings:
    s = Settings()
    s.data_root = tmp_path
    return s


def test_returns_preset_dict_shape(tmp_path):
    sp = build_system_prompt(_settings(tmp_path))
    assert isinstance(sp, dict)
    assert sp["type"] == "preset"
    assert sp["preset"] == "claude_code"
    assert isinstance(sp.get("append"), str)
    assert sp["append"]


def test_append_contains_mandatory_contract_tokens(tmp_path):
    sp = build_system_prompt(_settings(tmp_path))
    a = sp["append"]
    # The model must know to call this tool before writing.
    assert "AskUserQuestion" in a
    # The 3 sanctioned target labels (spec §8.2).
    assert "New .xlsx" in a
    assert "New sheet" in a
    assert "Pick sheet" in a
    # The contract field name returned by the BE permission resolver.
    assert "resolved_target_path" in a
    assert "resolved_target_kind" in a


def test_append_never_mentions_legacy_top_level_workspace(tmp_path):
    sp = build_system_prompt(_settings(tmp_path))
    # The deprecated TOP-LEVEL workspace dir (`<data_root>/workspace/`)
    # must never appear — that path was global and broke per-turn scope.
    # The current per-session `<sessions-data>/<sid>/workspace/` is
    # legitimate and is verified by `test_append_mentions_per_session_workspace`.
    s = _settings(tmp_path)
    assert str(s.data_root / "workspace") not in sp["append"]


def test_append_mentions_per_session_workspace(tmp_path):
    """Per-session workspace path must appear so the model can pass it to subagents."""
    s = _settings(tmp_path)
    sp = build_system_prompt(s, session_id="sess_test")
    a = sp["append"]
    # Resolved per-session path must be present (used in the dispatch contract).
    assert str(s.session_workspace_dir("sess_test")) in a


def test_append_drops_legacy_versioning_slots(tmp_path):
    sp = build_system_prompt(_settings(tmp_path))
    a = sp["append"]
    # The v_curr / v_prev rollback chain was removed; outputs land flat under
    # outputs/<session_id>/<filename> and the harness bumps a `_vN` suffix on
    # collision. The prompt must not reintroduce the legacy slot names.
    assert "v_curr" not in a
    assert "v_prev" not in a


def test_append_interpolates_runtime_paths(tmp_path):
    s = _settings(tmp_path)
    sp = build_system_prompt(s)
    a = sp["append"]
    assert str(s.kb_dir) in a
    assert str(s.attachments_dir) in a
    assert str(s.outputs_dir) in a


def test_append_lists_trigger_rules_and_examples(tmp_path):
    sp = build_system_prompt(_settings(tmp_path))
    a = sp["append"]
    # Trigger discipline (per the user brief): explicit fence with examples.
    assert "<trigger_rules>" in a
    assert "</trigger_rules>" in a
    assert "<examples>" in a
    assert "</examples>" in a
    # Make sure the override clause for explicit-save user intent is present.
    assert "OVERRIDE" in a or "override" in a.lower()


def test_append_warns_about_immutable_sources(tmp_path):
    sp = build_system_prompt(_settings(tmp_path))
    a = sp["append"]
    # The model must never modify raw.xlsx or the original attachment file.
    assert "raw.xlsx" in a
    assert "IMMUTABLE" in a or "immutable" in a.lower()


def test_append_lists_expanded_5_target_labels(tmp_path):
    sp = build_system_prompt(_settings(tmp_path))
    a = sp["append"]
    # The two new standalone-deliverable targets (spec §8.2 expansion).
    assert "New .pptx" in a
    assert "New .docx" in a
    # The pre-existing three are still enumerated.
    assert "New .xlsx" in a
    assert "New sheet" in a
    assert "Pick sheet" in a


def test_append_references_data_analysis_skill(tmp_path):
    sp = build_system_prompt(_settings(tmp_path))
    a = sp["append"]
    # The skill is loaded automatically for analytical questions; the prompt
    # must defer to it explicitly (case-sensitive on the canonical name).
    assert "data-analysis skill" in a


def test_append_includes_analytical_why_example(tmp_path):
    sp = build_system_prompt(_settings(tmp_path))
    a = sp["append"]
    # Example #7 demonstrates the skill flow on a Vietnamese "why" question.
    assert "Tại sao doanh thu Q2" in a


def test_append_clarifies_source_na_for_standalone_targets(tmp_path):
    sp = build_system_prompt(_settings(tmp_path))
    a = sp["append"]
    # The new sentence under the targets table.
    assert "Source is N/A" in a


def test_append_drops_stale_3_target_enumeration(tmp_path):
    sp = build_system_prompt(_settings(tmp_path))
    a = sp["append"]
    # The old AskUserQuestion options enumeration listed only the original
    # three targets; the expansion replaces it with the 5-label form.
    assert "New .xlsx, New sheet, Pick sheet" not in a


def test_append_contains_delegation_rules_block(tmp_path):
    sp = build_system_prompt(_settings(tmp_path))
    a = sp["append"]
    # The orchestrator role + routing table must be encoded as its own
    # XML block so the model treats it as a hard contract, not advisory.
    assert "<delegation_rules>" in a
    assert "</delegation_rules>" in a
    block = a.split("<delegation_rules>", 1)[1].split("</delegation_rules>", 1)[0]
    # All three subagent_type values must appear inside the block.
    assert "profiler" in block
    assert "analyst" in block
    assert "reporter" in block
    # The dispatch contract names the SDK tool + the kwarg the model passes.
    # The tool is named `Agent` in the SDK (formerly `Task` — renamed in
    # newer claude-agent-sdk builds; subagent_type kwarg is unchanged).
    assert "Agent" in block
    assert "subagent_type" in block
    # Guard against accidentally re-introducing the old `Task` tool name.
    assert "the `Task` tool" not in block
    # Routing table preamble must be present (catches accidental rewording).
    assert "Routing table" in block


def test_append_delegation_workflow_mandates_orchestrator_role(tmp_path):
    sp = build_system_prompt(_settings(tmp_path))
    a = sp["append"]
    # Step 6 of <workflow> was rewritten from advisory to mandate-form.
    # If someone reverts it, this token disappears.
    assert "never writes the deliverable itself" in a
    assert "orchestrator" in a.lower()


def test_append_delegation_examples_present(tmp_path):
    sp = build_system_prompt(_settings(tmp_path))
    a = sp["append"]
    # Example 9 — deliverable request → must dispatch reporter.
    assert "Tạo 1 file excel dummy về chủ đề retail" in a
    assert 'subagent_type="reporter"' in a
    # Example 10 — inline lookup → must NOT dispatch a subagent.
    assert "What's in cell A1" in a
    # Tolerate the soft line wrap inside <example index="10">.
    assert "Do NOT dispatch a" in a
    assert "subagent" in a


def test_delegation_rules_mandate_subagent_dispatch_contract(tmp_path):
    """The four-item dispatch contract must be encoded inside <delegation_rules>."""
    sp = build_system_prompt(_settings(tmp_path), session_id="sess_test")
    a = sp["append"]
    block = a.split("<delegation_rules>", 1)[1].split("</delegation_rules>", 1)[0]
    # Item 1 — working_dir, with the resolved per-session path.
    assert "working_dir=" in block
    # Item 2 — output_path naming.
    assert "output_path=" in block
    # Item 3 — verbatim forwarding + diacritic preservation negative prompt.
    assert "VERBATIM" in block or "verbatim" in block.lower()
    assert "transliterate" in block.lower()
    assert "diacritic" in block.lower()
    # Item 4 — output language rule.
    assert "language" in block.lower()


def test_example_11_demonstrates_pptx_delegation(tmp_path):
    """Example 11 is the worked Vietnamese pptx pattern: reporter dispatch + diacritics."""
    sp = build_system_prompt(_settings(tmp_path), session_id="sess_test")
    a = sp["append"]
    # The Vietnamese user prompt and the dispatch shape must both appear.
    assert "Tạo 3-slide presentation mô tả con chó" in a
    assert 'subagent_type="reporter"' in a
    # Negative-prompt phrasing inside the example body — model must internalise
    # the failure mode it is preventing.
    assert "`chó`" in a
    assert "never `cho`" in a
