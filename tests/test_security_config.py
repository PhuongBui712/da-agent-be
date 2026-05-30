"""Config-assembly tests for Layer-1 sandbox + declarative deny rules.

These do not exercise the SDK; they assert the static shape of the values
we hand to `ClaudeAgentOptions` so a regression on this surface fails
loudly in CI rather than silently disabling a security control.
"""

from __future__ import annotations

import json

from claude_agent_sdk import HookMatcher

from da_agent.agent.security import (
    build_permission_settings_json,
    build_sandbox_settings,
    build_security_hooks,
    inspect_bash_command,
)
from da_agent.config import Settings


# ---------------------------------------------------------------------------
# build_sandbox_settings()
# ---------------------------------------------------------------------------


def test_sandbox_settings_shape_and_invariants():
    sb = build_sandbox_settings()
    # SandboxSettings is a TypedDict (not a runtime class); validate by
    # required keys instead of isinstance.
    assert isinstance(sb, dict)
    assert sb["enabled"] is True
    # MUST stay True so the hook is the secondary guard, not the primary
    # interactive gate.
    assert sb["autoAllowBashIfSandboxed"] is True
    # MUST stay False; per-command sandbox bypass is exactly the escape
    # hatch we are trying to prevent.
    assert sb["allowUnsandboxedCommands"] is False

    excluded = sb["excludedCommands"]
    for must_block in ("curl", "wget", "ssh", "sudo", "shutdown"):
        assert any(must_block in cmd for cmd in excluded), (
            f"sandbox.excludedCommands missing {must_block!r}: {excluded}"
        )


def test_sandbox_network_denies_all():
    sb = build_sandbox_settings()
    net = sb["network"]
    assert net["deniedDomains"] == ["*"]
    assert net["allowedDomains"] == []
    assert net["allowAllUnixSockets"] is False
    assert net["allowLocalBinding"] is False


# ---------------------------------------------------------------------------
# build_permission_settings_json()
# ---------------------------------------------------------------------------


def test_permission_json_is_valid_and_includes_critical_denies(tmp_path, monkeypatch):
    monkeypatch.setenv("DA_AGENT_HOME", str(tmp_path))
    s = Settings()
    s.data_root = tmp_path

    payload = build_permission_settings_json(s)
    parsed = json.loads(payload)
    deny = parsed["permissions"]["deny"]

    # raw.xlsx (Golden Rule 4) — both Write and Edit must be denied.
    assert any("raw.xlsx" in r and r.startswith("Write(") for r in deny)
    assert any("raw.xlsx" in r and r.startswith("Edit(") for r in deny)

    # manifest.json — BE-managed metadata.
    assert any("manifest.json" in r and r.startswith("Write(") for r in deny)

    # sessions/ — Golden Rule 3 (SDK SSOT).
    assert any("sessions/**" in r for r in deny)

    # Credential paths beyond ~/.claude.json.
    for cred in ("~/.aws/**", "~/.ssh/**", "~/.config/gh/**", "~/.netrc"):
        assert any(cred in r for r in deny), f"missing credential deny for {cred}"

    # Web tools blocked outright.
    assert "WebFetch" in deny
    assert "WebSearch" in deny


def test_permission_json_does_not_include_allow_rules(tmp_path, monkeypatch):
    """Allow rules must NOT be set here — they would clash with the
    AskUserQuestion-resolved per-turn output paths."""
    monkeypatch.setenv("DA_AGENT_HOME", str(tmp_path))
    s = Settings()
    s.data_root = tmp_path
    parsed = json.loads(build_permission_settings_json(s))
    assert "allow" not in parsed["permissions"]


def test_permission_json_uses_string_form(tmp_path, monkeypatch):
    """Per the SDK schema, rules are STRINGS like `Tool(spec)` — not dicts."""
    monkeypatch.setenv("DA_AGENT_HOME", str(tmp_path))
    s = Settings()
    s.data_root = tmp_path
    parsed = json.loads(build_permission_settings_json(s))
    for r in parsed["permissions"]["deny"]:
        assert isinstance(r, str), f"non-string deny rule: {r!r}"


# ---------------------------------------------------------------------------
# build_security_hooks()
# ---------------------------------------------------------------------------


def test_security_hooks_register_pretooluse_for_bash():
    hooks = build_security_hooks()
    assert "PreToolUse" in hooks
    matchers = hooks["PreToolUse"]
    assert len(matchers) == 1
    matcher = matchers[0]
    assert isinstance(matcher, HookMatcher)
    assert matcher.matcher == "Bash"
    assert inspect_bash_command in matcher.hooks
