"""Layer-1 + Layer-2 isolation for the Excel data-analyst agent.

Two complementary mechanisms protect the user's host:

* **SDK sandbox** — `SandboxSettings(enabled=True, ...)` runs Bash invocations
  inside the platform-native sandbox the SDK provides (filesystem isolation
  outside `cwd`/`add_dirs`, process tree confinement, optional network
  policy). Combined with the declarative deny rules below, the model loses
  filesystem access to credentials and system directories.

* **`PreToolUse` hook on Bash** — `inspect_bash_command` reads each Bash
  command BEFORE it runs and denies on Python escape-hatch patterns the
  filesystem layer can't catch (e.g. `python3 -c "import urllib.request;
  ..."`). Python-via-Bash is the agent's native mode (pandas/openpyxl), so
  text-level inspection is the right place to block network egress and
  destructive ops that the SDK pattern matcher in `excludedCommands` misses.

The hook intentionally errs on the side of allow: the codebase is for a
trusted single user. Each rule has a matching test under tests/test_security_*.

Spec ground rules enforced here:
- Golden Rule 4 (raw.xlsx immutable) — denied via path rule + bash regex.
- Golden Rule 3 (sessions JSONL is SDK SSOT) — denied via path rule.
- §8.2 output mechanism — writes outside outputs/, kb/<id>/versions/,
  attachments/<sid>/<id>/versions/ are denied.
"""

from __future__ import annotations

import json
import re
from typing import Any

from claude_agent_sdk import (
    HookMatcher,
    SandboxNetworkConfig,
    SandboxSettings,
)

from ..config import Settings


# ---------------------------------------------------------------------------
# Sandbox
# ---------------------------------------------------------------------------


def build_sandbox_settings() -> SandboxSettings:
    """SDK sandbox config — filesystem + network isolation for Bash.

    `excludedCommands` blocks the most common destructive / network shell
    binaries at the SDK pattern layer. The PreToolUse hook covers
    Python-via-Bash escape hatches (`python -c "import urllib"`).

    `autoAllowBashIfSandboxed=True` lets the model run pandas/openpyxl
    without prompting per-command — the sandbox is the safety boundary,
    not interactive approval.

    `allowUnsandboxedCommands=False` is critical: we never want a Bash call
    to ESCAPE the sandbox (the SDK supports per-command opt-out, which we
    refuse).

    Network policy: deny by default. The data-analyst workflow has zero
    legitimate need for network access — all data is already on disk.
    """
    return SandboxSettings(
        enabled=True,
        autoAllowBashIfSandboxed=True,
        allowUnsandboxedCommands=False,
        excludedCommands=[
            "curl",
            "wget",
            "ssh",
            "scp",
            "rsync",
            "nc",
            "ncat",
            "telnet",
            "ftp",
            "sudo",
            "su",
            "rm -rf /",
            "rm -rf ~",
            "rm -rf ${HOME}",
            "rm -rf $HOME",
            "shutdown",
            "reboot",
            "halt",
            "mkfs",
            "dd if=",
            "chmod 777",
            ":(){ :|:& };:",  # fork bomb
        ],
        network=SandboxNetworkConfig(
            # Container is host-isolation; allow pip/npm registries only.
            allowedDomains=[
                "pypi.org",
                "files.pythonhosted.org",
                "registry.npmjs.org",
                "registry.yarnpkg.com",
                "github.com",
                "objects.githubusercontent.com",
                "raw.githubusercontent.com",
            ],
            deniedDomains=[],
            allowManagedDomainsOnly=False,
            allowUnixSockets=[],
            allowAllUnixSockets=False,
            allowLocalBinding=False,
            allowMachLookup=[],
            httpProxyPort=0,
            socksProxyPort=0,
        ),
    )


# ---------------------------------------------------------------------------
# Declarative permission rules (settings.json schema)
# ---------------------------------------------------------------------------


def build_permission_settings_json(settings: Settings) -> str:
    """Emit the SDK `settings` JSON with deny rules for sensitive paths.

    Per the official permissions docs, rules use string-form syntax
    (`"Tool(specifier)"`), not object form. `deny` is evaluated before any
    permission mode and BEFORE `bypassPermissions`, so these are hard
    floors regardless of mode.

    We intentionally do NOT add allow rules here — the existing `add_dirs`
    + `permission_mode="default"` + `can_use_tool` chain already governs
    legitimate writes, and a narrow allow list would break the §8.2
    AskUserQuestion flow (any path computed at resolve-time wouldn't match
    a static rule).
    """
    home = str(settings.data_root)

    deny_rules: list[str] = [
        # Golden Rule 4 — raw.xlsx is immutable. Block both Write and Edit.
        f"Write({home}/kb/*/raw.xlsx)",
        f"Edit({home}/kb/*/raw.xlsx)",
        # Manifest is BE-managed metadata; agent reads but never writes.
        f"Write({home}/kb/*/manifest.json)",
        f"Edit({home}/kb/*/manifest.json)",
        # Golden Rule 3 — SDK session JSONL is single source of truth.
        f"Write({home}/sessions/**)",
        f"Edit({home}/sessions/**)",
        # Original attachment file is immutable; only versions/ is writable.
        f"Write({home}/attachments/*/*/[!v]*)",
        f"Edit({home}/attachments/*/*/[!v]*)",
        # Credentials & host config — agent must never read these.
        "Read(~/.claude.json)",
        "Read(~/.claude/**)",
        "Read(~/.aws/**)",
        "Read(~/.ssh/**)",
        "Read(~/.config/gh/**)",
        "Read(~/.netrc)",
        "Read(~/.gnupg/**)",
        "Read(/etc/shadow)",
        "Read(/etc/sudoers)",
        "Read(/etc/sudoers.d/**)",
        # System directories — never write.
        "Write(/etc/**)",
        "Edit(/etc/**)",
        "Write(/usr/**)",
        "Edit(/usr/**)",
        "Write(/var/**)",
        "Edit(/var/**)",
        "Write(/bin/**)",
        "Edit(/bin/**)",
        # Backup defense for destructive shell ops the sandbox might miss.
        "Bash(rm -rf /*)",
        "Bash(rm -rf /)",
        "Bash(rm -rf ~)",
        "Bash(rm -rf ~/*)",
        "Bash(rm -rf $HOME*)",
        "Bash(sudo *)",
        "Bash(su *)",
        # Network egress at the Bash layer (sandbox network deny is primary).
        "Bash(curl *)",
        "Bash(wget *)",
        "Bash(ssh *)",
        "Bash(scp *)",
        "Bash(rsync *)",
        # WebFetch/WebSearch — agent has no legitimate web need.
        "WebFetch",
        "WebSearch",
    ]

    return json.dumps({"permissions": {"deny": deny_rules}})


# ---------------------------------------------------------------------------
# PreToolUse hook for Bash inspection
# ---------------------------------------------------------------------------

# Patterns we refuse inside any Bash command body. Each entry is `(regex,
# reason)`. The regex MUST be tight enough to avoid false positives on
# pandas/openpyxl idioms.
_DENY_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    # --- Network egress from Python --------------------------------------
    (
        re.compile(r"\b(?:import|from)\s+urllib\b"),
        "urllib import is blocked (network egress)",
    ),
    (
        re.compile(r"\b(?:import|from)\s+(?:requests|httpx|aiohttp|websockets)\b"),
        "HTTP client library is blocked (network egress)",
    ),
    (
        re.compile(r"\b(?:import|from)\s+socket\b"),
        "socket import is blocked (raw network access)",
    ),
    (
        re.compile(r"\b(?:import|from)\s+(?:ftplib|smtplib|poplib|imaplib)\b"),
        "Network protocol library is blocked",
    ),
    # --- Process / shell escape ------------------------------------------
    (
        re.compile(r"\b(?:import|from)\s+subprocess\b"),
        "subprocess import is blocked (process spawn)",
    ),
    (
        re.compile(r"\bos\.system\s*\("),
        "os.system is blocked (shell escape)",
    ),
    (
        re.compile(r"\bos\.exec[lv]p?e?\s*\("),
        "os.exec* is blocked (process replacement)",
    ),
    (
        re.compile(r"\bos\.popen\s*\("),
        "os.popen is blocked (subprocess via shell)",
    ),
    (
        re.compile(r"\bos\.fork\s*\("),
        "os.fork is blocked",
    ),
    # --- Code injection ---------------------------------------------------
    (
        re.compile(r"\b(?:import|from)\s+ctypes\b"),
        "ctypes import is blocked (low-level memory / FFI)",
    ),
    (
        re.compile(
            r"\b__import__\s*\(\s*['\"](?:os|sys|subprocess|socket|urllib|ctypes)"
        ),
        "Dynamic __import__ of sensitive module is blocked",
    ),
    # --- Filesystem destruction beyond data_root --------------------------
    (
        re.compile(r"\bshutil\.rmtree\s*\(\s*['\"]?(?:/|~/[^.]|/etc|/usr|/var|/bin)"),
        "shutil.rmtree on system path is blocked",
    ),
    # --- Path traversal ---------------------------------------------------
    (
        re.compile(r"\.\./\.\./\.\."),
        "Excessive path traversal (../../..) is blocked",
    ),
    # --- Direct read of system credential paths ---------------------------
    (
        re.compile(r"/etc/(shadow|sudoers|passwd-)\b"),
        "Reading system credential files is blocked",
    ),
    (
        re.compile(r"~/\.(ssh|aws|gnupg|netrc|claude\.json|config/gh)"),
        "Reading host credential paths is blocked",
    ),
)


# Write-verb tokens that, when co-located with `raw.xlsx`, indicate a
# Golden-Rule-4 violation. Any one match in a command containing
# `raw.xlsx` triggers a deny.
_RAW_XLSX_WRITE_VERBS = re.compile(
    r"\b(?:to_excel|ExcelWriter|\.save\s*\(|"
    r"open\s*\([^)]*['\"][raw\\][^)]*[ab+w])"
)
_RAW_XLSX_REF = re.compile(r"\braw\.xlsx\b")


def _match_deny_pattern(command: str) -> str | None:
    for pattern, reason in _DENY_PATTERNS:
        if pattern.search(command):
            return reason
    # Golden Rule 4 — `raw.xlsx` write attempt. Two-clause check: the
    # filename is referenced AND a write verb is present in the same
    # command. Read-only references (`pd.read_excel("raw.xlsx")`) are
    # untouched.
    if _RAW_XLSX_REF.search(command) and _RAW_XLSX_WRITE_VERBS.search(command):
        return (
            "Writing to raw.xlsx is blocked (Golden Rule 4 — KB original is "
            "immutable). Use kb/<id>/versions/v_curr.xlsx instead."
        )
    return None


async def inspect_bash_command(
    input_data: dict[str, Any],
    tool_use_id: str | None,
    context: Any,
) -> dict[str, Any]:
    """PreToolUse hook for Bash — denies escape-hatch patterns.

    Returns `{}` to allow (the SDK falls through to the rest of the
    permission chain). Returns a deny payload to block, with a reason the
    model sees so it doesn't retry the same pattern.
    """
    if input_data.get("hook_event_name") != "PreToolUse":
        return {}
    if input_data.get("tool_name") != "Bash":
        return {}

    tool_input = input_data.get("tool_input") or {}
    command = tool_input.get("command", "")
    if not isinstance(command, str) or not command:
        return {}

    reason = _match_deny_pattern(command)
    if reason is None:
        return {}

    return {
        "systemMessage": f"Security: blocked Bash command — {reason}",
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": (
                f"Blocked by security policy: {reason}. "
                "Use only the standard pandas/openpyxl APIs against files "
                "under the kb/, outputs/, or attachments/ trees."
            ),
        },
    }


def build_security_hooks() -> dict[str, list[HookMatcher]]:
    """Wrap `inspect_bash_command` in the SDK's hook-config schema."""
    return {
        "PreToolUse": [
            HookMatcher(matcher="Bash", hooks=[inspect_bash_command]),
        ],
    }
