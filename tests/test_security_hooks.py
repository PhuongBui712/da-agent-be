"""Tests for the PreToolUse Bash inspection hook (Layer 2).

Covers each deny pattern in `da_agent.agent.security._DENY_PATTERNS` plus
the allow paths (legitimate pandas/openpyxl use) and the no-op cases
(non-Bash tools, non-PreToolUse events, empty command).
"""

from __future__ import annotations

import pytest

from da_agent.agent.security import inspect_bash_command


def _bash_event(command: str) -> dict:
    return {
        "hook_event_name": "PreToolUse",
        "tool_name": "Bash",
        "tool_input": {"command": command},
    }


# ---------------------------------------------------------------------------
# Deny — network egress from Python
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "command",
    [
        "python3 -c 'import urllib.request; urllib.request.urlopen(\"http://x\")'",
        "python -c \"from urllib import request; request.urlopen('x')\"",
        "python3 -c 'import requests; requests.get(\"http://x\")'",
        "python3 -c 'import httpx; httpx.get(\"x\")'",
        "python3 -c 'import aiohttp'",
        "python3 -c 'import socket; s = socket.socket()'",
        "python3 -c 'from socket import socket'",
        "python3 -c 'import ftplib'",
        "python3 -c 'import smtplib'",
    ],
)
async def test_blocks_network_imports(command):
    res = await inspect_bash_command(_bash_event(command), None, None)
    assert res["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert "blocked" in res["hookSpecificOutput"]["permissionDecisionReason"].lower()


# ---------------------------------------------------------------------------
# Deny — process / shell escape
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "command",
    [
        "python3 -c 'import subprocess; subprocess.run([\"ls\"])'",
        "python3 -c 'from subprocess import run'",
        "python3 -c 'import os; os.system(\"ls\")'",
        'python3 -c \'import os; os.execv("/bin/sh", ["sh"])\'',
        'python3 -c \'import os; os.execlp("sh", "sh")\'',
        "python3 -c 'import os; os.popen(\"ls\")'",
        "python3 -c 'import os; os.fork()'",
    ],
)
async def test_blocks_process_escape(command):
    res = await inspect_bash_command(_bash_event(command), None, None)
    assert res["hookSpecificOutput"]["permissionDecision"] == "deny"


# ---------------------------------------------------------------------------
# Deny — code injection / FFI
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "command",
    [
        "python3 -c 'import ctypes; ctypes.CDLL(\"libc.so.6\")'",
        "python3 -c 'from ctypes import CDLL'",
        'python3 -c \'__import__("os").system("ls")\'',
        "python3 -c '__import__(\"subprocess\")'",
        "python3 -c '__import__(\"socket\")'",
        "python3 -c '__import__(\"urllib\")'",
    ],
)
async def test_blocks_code_injection(command):
    res = await inspect_bash_command(_bash_event(command), None, None)
    assert res["hookSpecificOutput"]["permissionDecision"] == "deny"


# ---------------------------------------------------------------------------
# Deny — filesystem destruction beyond data_root
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "command",
    [
        "python3 -c 'import shutil; shutil.rmtree(\"/etc\")'",
        "python3 -c 'import shutil; shutil.rmtree(\"/usr/local\")'",
        "python3 -c 'import shutil; shutil.rmtree(\"~/projects\")'",
    ],
)
async def test_blocks_rmtree_on_system_paths(command):
    res = await inspect_bash_command(_bash_event(command), None, None)
    assert res["hookSpecificOutput"]["permissionDecision"] == "deny"


# ---------------------------------------------------------------------------
# Deny — path traversal
# ---------------------------------------------------------------------------


async def test_blocks_path_traversal():
    res = await inspect_bash_command(_bash_event("cat ../../../etc/shadow"), None, None)
    assert res["hookSpecificOutput"]["permissionDecision"] == "deny"


# ---------------------------------------------------------------------------
# Deny — Golden Rule 4 (raw.xlsx is immutable)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "command",
    [
        "python3 -c 'df.to_excel(\"raw.xlsx\")'",
        "python3 -c 'import pandas as pd; pd.DataFrame().to_excel(\"raw.xlsx\")'",
        "python3 -c 'wb.save(\"raw.xlsx\")'",
        "python3 -c 'workbook.save(\"raw.xlsx\")'",
        'python3 -c \'open("raw.xlsx", "wb").write(b"")\'',
    ],
)
async def test_blocks_writes_to_raw_xlsx(command):
    res = await inspect_bash_command(_bash_event(command), None, None)
    assert res["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert "raw.xlsx" in res["hookSpecificOutput"]["permissionDecisionReason"].lower()


# ---------------------------------------------------------------------------
# Deny — credential reads
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "command",
    [
        "cat /etc/shadow",
        "cat /etc/sudoers",
        "cat ~/.ssh/id_rsa",
        "cat ~/.aws/credentials",
        "cat ~/.claude.json",
        "cat ~/.netrc",
        "cat ~/.gnupg/secring.gpg",
        "cat ~/.config/gh/hosts.yml",
    ],
)
async def test_blocks_credential_reads(command):
    res = await inspect_bash_command(_bash_event(command), None, None)
    assert res["hookSpecificOutput"]["permissionDecision"] == "deny"


# ---------------------------------------------------------------------------
# Allow — legitimate pandas / openpyxl use
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "command",
    [
        "python3 -c 'import pandas as pd; print(pd.read_excel(\"data.xlsx\").head())'",
        "python3 -c 'import openpyxl; wb = openpyxl.load_workbook(\"a.xlsx\")'",
        "python3 -c 'from openpyxl import load_workbook'",
        'python3 -c \'import pandas as pd; df = pd.read_csv("x.csv"); df.to_excel("out.xlsx")\'',
        "python3 -c 'import json; print(json.load(open(\"manifest.json\")))'",
        "ls -la /home/user/.da-agent/kb/",
        "head -5 /tmp/data.csv",
        # `import os` itself is fine — only os.system / os.exec / os.popen / os.fork are denied.
        "python3 -c 'import os; print(os.path.exists(\"a.xlsx\"))'",
        # `requests-cache` is sometimes a transitive name; `requests_cache` is not the
        # same as `requests`. Make sure the regex is anchored to a real word boundary.
        "python3 -c 'print(\"requests for analysis\")'",
    ],
)
async def test_allows_legitimate_pandas_use(command):
    res = await inspect_bash_command(_bash_event(command), None, None)
    assert res == {}, f"Unexpected deny for: {command} -> {res}"


# ---------------------------------------------------------------------------
# No-op — non-Bash tool / wrong event / empty command
# ---------------------------------------------------------------------------


async def test_noop_for_non_bash_tool():
    res = await inspect_bash_command(
        {
            "hook_event_name": "PreToolUse",
            "tool_name": "Read",
            "tool_input": {"file_path": "~/.ssh/id_rsa"},
        },
        None,
        None,
    )
    assert res == {}


async def test_noop_for_wrong_event():
    res = await inspect_bash_command(
        {
            "hook_event_name": "PostToolUse",
            "tool_name": "Bash",
            "tool_input": {"command": "import urllib"},
        },
        None,
        None,
    )
    assert res == {}


async def test_noop_for_empty_command():
    res = await inspect_bash_command(_bash_event(""), None, None)
    assert res == {}


async def test_noop_for_non_string_command():
    res = await inspect_bash_command(
        {
            "hook_event_name": "PreToolUse",
            "tool_name": "Bash",
            "tool_input": {"command": 12345},
        },
        None,
        None,
    )
    assert res == {}
