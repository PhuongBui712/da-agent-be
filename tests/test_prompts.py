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


def test_append_never_mentions_workspace(tmp_path):
    sp = build_system_prompt(_settings(tmp_path))
    # Case-insensitive sweep — "workspace" is the deprecated path; nothing in
    # the prompt must steer the model to a scratch dir.
    assert "workspace" not in sp["append"].lower()


def test_append_documents_versioning_layout(tmp_path):
    sp = build_system_prompt(_settings(tmp_path))
    a = sp["append"]
    # Both KB and attachments share the v_curr / v_prev slot names.
    assert "v_curr" in a
    assert "v_prev" in a


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
