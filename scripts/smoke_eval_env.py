#!/usr/bin/env python3
"""Smoke test cho eval runtime environment (da-agent-runenv/).

Verify:
  1. docker compose config parse OK (static)
  2. Container up + /healthz trả 200
  3. POST /sessions + /messages → ≥1 output.created event
  4. Output file ghi vào HOST runenv/data/da-agent-home/outputs/<sid>/ (data-root override)
  5. Session JSONL ghi vào HOST runenv/data/da-agent-home/sessions/ (CLAUDE_CONFIG_DIR)
  6. ~/.da-agent/outputs/ KHÔNG bị đụng (no leak vào dev data root)
  7. Cleanup container

Loads creds từ da-agent-runenv/.env.eval.

Opt-in. Run sau khi `docker compose -f da-agent-runenv/docker-compose.eval.yml up --build -d`:

    DA_AGENT_SMOKE_URL=http://127.0.0.1:8766 \\
    uv run python scripts/smoke_eval_env.py

Exits 0 on PASS, 0 với skip nếu thiếu API key, 1 on FAIL.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

import httpx

# --- Config -----------------------------------------------------------------
URL = os.environ.get("DA_AGENT_SMOKE_URL", "http://127.0.0.1:8766").rstrip("/")
REPO_ROOT = Path(__file__).resolve().parent.parent          # /…/da-agent-be
RUNENV_ROOT = REPO_ROOT.parent / "da-agent-runenv"          # sibling
EVAL_DATA_HOME = RUNENV_ROOT / "data" / "da-agent-home"
EVAL_OUTPUTS = EVAL_DATA_HOME / "outputs"
EVAL_SESSIONS = EVAL_DATA_HOME / "sessions"
DEV_DATA_HOME = Path.home() / ".da-agent"                   # phải KHÔNG bị đụng
DEV_OUTPUTS = DEV_DATA_HOME / "outputs"

COMPOSE_FILE = RUNENV_ROOT / "docker-compose.eval.yml"

PROMPT = (
    "Create a tiny .xlsx with one row of data: column A 'Hello', column B 'World'. "
    "Then mention only the filename in your reply — never paste an absolute path."
)


class CheckResult:
    def __init__(self) -> None:
        self.failures: list[str] = []

    def check(self, label: str, ok: bool, detail: str = "") -> None:
        suffix = f" — {detail}" if detail else ""
        if ok:
            print(f"  PASS  {label}{suffix}")
        else:
            print(f"  FAIL  {label}{suffix}")
            self.failures.append(label)


# --- Helpers reused từ smoke_outputs.py pattern ----------------------------
def _maybe_load_dotenv() -> None:
    """Load runenv/.env.eval (KHÁC smoke_outputs.py — đó load be/.env)."""
    env_path = RUNENV_ROOT / ".env.eval"
    if not env_path.exists():
        return
    for raw in env_path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and value:
            os.environ.setdefault(key, value)


def _have_api_key() -> bool:
    keys = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "DATABRICKS_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN")
    if any(os.environ.get(k) for k in keys):
        return True
    _maybe_load_dotenv()
    return any(os.environ.get(k) for k in keys)


def _parse_sse_chunk(buf: str) -> tuple[str, dict] | None:
    event_type = "message"
    data_lines: list[str] = []
    for line in buf.splitlines():
        if line.startswith("event:"):
            event_type = line[len("event:"):].strip()
        elif line.startswith("data:"):
            data_lines.append(line[len("data:"):].lstrip())
    if not data_lines:
        return None
    try:
        return event_type, json.loads("\n".join(data_lines))
    except json.JSONDecodeError:
        return None


def _stream_turn(client: httpx.Client, sid: str, prompt: str) -> list[tuple[str, dict]]:
    events: list[tuple[str, dict]] = []
    with client.stream(
        "POST",
        f"{URL}/sessions/{sid}/messages",
        json={"prompt": prompt},
        timeout=httpx.Timeout(connect=10.0, read=240.0, write=10.0, pool=10.0),
    ) as resp:
        if resp.status_code != 200:
            raise RuntimeError(f"messages POST returned {resp.status_code}: {resp.text}")
        chunk: list[str] = []
        for raw in resp.iter_lines():
            line = raw.rstrip("\r")
            if line == "":
                if chunk:
                    parsed = _parse_sse_chunk("\n".join(chunk))
                    if parsed is not None:
                        events.append(parsed)
                chunk = []
            else:
                chunk.append(line)
        if chunk:
            parsed = _parse_sse_chunk("\n".join(chunk))
            if parsed is not None:
                events.append(parsed)
    return events


def _wait_health(timeout: float = 30.0) -> bool:
    """Poll /healthz cho tới khi 200 hoặc timeout."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            r = httpx.get(f"{URL}/healthz", timeout=2.0)
            if r.status_code == 200:
                return True
        except Exception:
            pass
        time.sleep(1.0)
    return False


def _dev_outputs_mtime() -> float:
    return DEV_OUTPUTS.stat().st_mtime if DEV_OUTPUTS.exists() else -1.0


# --- Main ------------------------------------------------------------------
def main() -> int:
    if not _have_api_key():
        print(
            "[skip] no API key in env (ANTHROPIC_AUTH_TOKEN / ANTHROPIC_API_KEY / "
            "DATABRICKS_TOKEN / CLAUDE_CODE_OAUTH_TOKEN). Smoke is opt-in; exiting 0."
        )
        return 0

    res = CheckResult()
    print(f"[smoke] BE URL = {URL}")
    print(f"[smoke] eval data home = {EVAL_DATA_HOME}")
    print(f"[smoke] dev data home  = {DEV_DATA_HOME} (must NOT be written to)")

    # Phase 1: docker compose config OK (static)
    print("\n[phase 1] docker compose config")
    proc = subprocess.run(
        ["docker", "compose", "-f", str(COMPOSE_FILE), "config"],
        capture_output=True,
        text=True,
        cwd=str(RUNENV_ROOT),
    )
    res.check(
        "docker compose config parse OK",
        proc.returncode == 0,
        f"rc={proc.returncode}",
    )
    if proc.returncode != 0:
        print(proc.stderr[:500])

    # Phase 2: /healthz
    print("\n[phase 2] container /healthz")
    res.check("/healthz returns 200 within 30s", _wait_health(30.0))

    # Snapshot dev mtime trước khi gọi BE
    dev_mtime_before = _dev_outputs_mtime()

    with httpx.Client(timeout=30.0) as client:
        # Phase 3: round-trip prompt
        print("\n[phase 3] round-trip prompt")
        r = client.post(f"{URL}/sessions", json={"name": "smoke-eval-env"})
        if r.status_code != 201:
            print(f"=== FAIL: POST /sessions -> {r.status_code} {r.text} ===")
            return 1
        sid = r.json()["id"]
        print(f"[smoke] session id = {sid}")

        try:
            events = _stream_turn(client, sid, PROMPT)
        except Exception as exc:
            print(f"=== FAIL: SSE stream error: {exc} ===")
            client.delete(f"{URL}/sessions/{sid}")
            return 1
        output_events = [(t, p) for t, p in events if t == "output.created"]
        res.check(
            "≥1 output.created event arrived",
            len(output_events) >= 1,
            f"got {len(output_events)} event(s)",
        )

        # Phase 4: filesystem verify (eval data root)
        print("\n[phase 4] filesystem — eval data root")
        sid_outputs_dir = EVAL_OUTPUTS / sid
        xlsx_files = (
            list(sid_outputs_dir.rglob("*.xlsx")) if sid_outputs_dir.exists() else []
        )
        res.check(
            f"outputs/<sid>/ chứa ≥1 .xlsx ở runenv/data/",
            len(xlsx_files) >= 1,
            f"dir={sid_outputs_dir} files={[f.name for f in xlsx_files]}",
        )

        # Phase 5: sessions JSONL
        print("\n[phase 5] sessions JSONL — CLAUDE_CONFIG_DIR")
        session_files = list(EVAL_SESSIONS.rglob("*.jsonl")) if EVAL_SESSIONS.exists() else []
        res.check(
            "sessions/ chứa ≥1 .jsonl ở runenv/data/",
            len(session_files) >= 1,
            f"files={len(session_files)}",
        )

        # Phase 7: cleanup
        print("\n[phase 7] cleanup")
        d = client.delete(f"{URL}/sessions/{sid}")
        res.check(
            "DELETE /sessions/<sid> returns 204",
            d.status_code == 204,
            f"status={d.status_code}",
        )

    # Phase 6: verify dev data root KHÔNG bị đụng
    # (đặt SAU when BE đã xử lý xong tất cả ghi đĩa)
    print("\n[phase 6] dev data root isolation")
    dev_mtime_after = _dev_outputs_mtime()
    if dev_mtime_before < 0:
        # Dev data root chưa từng tồn tại — eval container không thể tạo nó.
        res.check(
            "~/.da-agent/outputs/ KHÔNG được tạo bởi eval container",
            not DEV_OUTPUTS.exists(),
            f"dir={DEV_OUTPUTS}",
        )
    else:
        res.check(
            "~/.da-agent/outputs/ mtime KHÔNG đổi",
            abs(dev_mtime_after - dev_mtime_before) < 0.001,
            f"before={dev_mtime_before} after={dev_mtime_after}",
        )

    if not res.failures:
        print("\n=== PASS ===")
        return 0
    print(f"\n=== FAIL: {len(res.failures)} check(s): {res.failures} ===")
    return 1


if __name__ == "__main__":
    sys.exit(main())
