"""Tests for the subagent registry returned by `build_subagents`."""

from __future__ import annotations

from da_agent.agent.subagents import build_subagents

_READONLY = ["Read", "Bash", "Glob", "Grep"]


def test_returns_three_subagents() -> None:
    agents = build_subagents()
    assert set(agents.keys()) == {"profiler", "analyst", "reporter"}
    assert "visualizer" not in agents


def test_profiler_is_readonly() -> None:
    profiler = build_subagents()["profiler"]
    assert profiler.tools == ["Read", "Bash", "Glob", "Grep"]
    assert profiler.skills == ["xlsx"]


def test_analyst_covers_phase3_and_phase4() -> None:
    analyst = build_subagents()["analyst"]
    assert "Phase 3" in analyst.prompt
    assert "Phase 4" in analyst.prompt
    assert analyst.tools == _READONLY
    assert analyst.skills == ["xlsx"]


def test_reporter_has_all_three_delivery_skills() -> None:
    reporter = build_subagents()["reporter"]
    assert reporter.skills == ["xlsx", "pptx", "docx"]
    assert "Write" in reporter.tools


def test_reporter_prompt_mentions_resolved_target_path() -> None:
    reporter = build_subagents()["reporter"]
    assert "resolved_target_path" in reporter.prompt


def test_reporter_prompt_routes_by_extension() -> None:
    reporter = build_subagents()["reporter"]
    assert ".xlsx" in reporter.prompt
    assert ".pptx" in reporter.prompt
    assert ".docx" in reporter.prompt


def test_build_subagents_accepts_optional_settings() -> None:
    from da_agent.config import Settings

    without = set(build_subagents().keys())
    with_settings = set(build_subagents(Settings()).keys())
    assert without == with_settings == {"profiler", "analyst", "reporter"}


def test_kb_profiler_NOT_in_main_subagents() -> None:
    from da_agent.config import Settings

    agents = build_subagents(Settings())
    assert "kb_profiler" not in agents


def test_reporter_prompt_preserves_vietnamese_rule():
    """Reporter prompt must contain explicit Vietnamese diacritic preservation rules."""
    from da_agent.agent.subagents import build_subagents

    reporter = build_subagents()["reporter"]
    text = reporter.prompt
    # Positive: must mention Vietnamese + diacritics
    assert "Vietnamese" in text
    assert "diacritic" in text.lower()
    # Negative-prompt phrasing: explicitly forbid the failure modes
    assert "transliterate" in text.lower()
    # Concrete worked examples (one or both):
    assert "Ngân hàng" in text or "chó" in text


def test_reporter_prompt_mandates_working_dir_discipline():
    """Reporter prompt must mandate working_dir vs output_path discipline."""
    from da_agent.agent.subagents import build_subagents

    reporter = build_subagents()["reporter"]
    text = reporter.prompt
    assert "working_dir" in text
    assert "output_path" in text
    # Negative: forbid ad-hoc paths
    assert "/tmp" in text  # the rule explicitly mentions /tmp as forbidden
