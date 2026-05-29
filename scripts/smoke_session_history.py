"""Live smoke for session-history loading + Untitled default.

Boots a real uvicorn subprocess with an isolated `DA_AGENT_HOME=$(mktemp -d)`,
creates a session, sends one Vietnamese prompt that triggers thinking + a tool
call, then GETs `/sessions/{sid}/messages` and asserts the replay payload
mirrors the live SSE stream the FE just consumed.

Pre-req: `ANTHROPIC_API_KEY` (or whatever credential the configured model uses)
must be available in the environment. Configurable via `DA_AGENT_MODEL`.

Run:
    uv run python scripts/smoke_session_history.py
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

PROMPT = (
    "Hãy đọc file pyproject.toml ở thư mục hiện tại rồi liệt kê 5 dependencies chính. "
    "Trả lời ngắn gọn bằng tiếng Việt."
)


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_healthz(port: int, timeout: float = 30.0) -> None:
    url = f"http://127.0.0.1:{port}/health"
    deadline = time.monotonic() + timeout
    last_err: Exception | None = None
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=1.5) as r:
                if r.status == 200:
                    return
        except (urllib.error.URLError, ConnectionError, TimeoutError) as e:
            last_err = e
            time.sleep(0.3)
    raise RuntimeError(f"server never became healthy: {last_err}")


def _post_json(port: int, path: str, payload: dict) -> dict:
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read())


def _get_json(port: int, path: str) -> dict:
    with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=10) as r:
        return json.loads(r.read())


def _delete(port: int, path: str) -> int:
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", method="DELETE")
    with urllib.request.urlopen(req, timeout=10) as r:
        return r.status


def _stream_turn(
    port: int, sid: str, prompt: str, *, deadline_s: float = 240.0
) -> list[dict]:
    """POST a message, drain the SSE stream, return all parsed event dicts.

    Streams text/event-stream line-by-line; parses `event:` + `data:` pairs.
    """
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/sessions/{sid}/messages",
        data=json.dumps({"prompt": prompt}).encode(),
        headers={"Content-Type": "application/json", "Accept": "text/event-stream"},
        method="POST",
    )
    events: list[dict] = []
    deadline = time.monotonic() + deadline_s
    with urllib.request.urlopen(req, timeout=deadline_s) as r:
        # `urlopen` returns bytes line iterator; SSE lines end with '\n\n' between
        # records but each line is delimited by '\n'.
        buf: list[str] = []
        for raw in r:
            if time.monotonic() > deadline:
                raise TimeoutError("SSE drain timed out")
            line = raw.decode("utf-8").rstrip("\n")
            if line == "":
                rec = {}
                for entry in buf:
                    if entry.startswith("data:"):
                        try:
                            rec = json.loads(entry[5:].strip())
                        except json.JSONDecodeError:
                            pass
                if rec:
                    events.append(rec)
                buf.clear()
            else:
                buf.append(line)
            if events and events[-1].get("type") == "result":
                break
    return events


def _summarise_events(events: list[dict], label: str) -> None:
    from collections import Counter

    types = Counter(e.get("type") for e in events)
    print(f"[{label}] {len(events)} events, types: {dict(types)}")


def main() -> int:
    repo_root = Path(__file__).resolve().parents[1]
    data_root = Path(tempfile.mkdtemp(prefix="da-agent-smoke-"))
    port = _free_port()
    print(f"→ smoke: data_root={data_root}  port={port}")

    env = dict(os.environ)
    env["DA_AGENT_HOME"] = str(data_root)
    env["PYTHONUNBUFFERED"] = "1"

    # Boot uvicorn via the package's own CLI to keep the contract aligned with
    # how a user would actually run the server.
    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "da_agent.cli",
            "serve",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
        ],
        cwd=str(repo_root),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    failures: list[str] = []
    try:
        _wait_healthz(port)
        print("✓ server healthy")

        # 1. Default name capitalization regression.
        meta = _post_json(port, "/sessions", {})
        sid = meta["id"]
        if meta["name"] != "Untitled":
            failures.append(f"default name = {meta['name']!r}, expected 'Untitled'")
        print(f"✓ session created: id={sid}  name={meta['name']!r}")

        # 2. Drive a live turn.
        print("→ posting prompt:", PROMPT[:80], "…")
        live = _stream_turn(port, sid, PROMPT)
        _summarise_events(live, "live")
        live_types = {e.get("type") for e in live}
        # In stream mode (default), text comes via delta+end pairs; in atomic
        # fallback, via assistant.text. Accept either.
        if (
            "assistant.text" not in live_types
            and "assistant.text.end" not in live_types
        ):
            failures.append(
                "live stream produced no text (neither atomic nor streamed)"
            )
        for required in ("user.prompt", "result"):
            if required not in live_types:
                failures.append(f"live stream missing event type: {required}")

        # 3. Registry should now hold the SDK UUID.
        registry = json.loads((data_root / "registry.json").read_text())
        row = next((s for s in registry["sessions"] if s["id"] == sid), None)
        if row is None or not row.get("sdk_session_id"):
            failures.append(f"registry row missing sdk_session_id: {row}")
        else:
            print(f"✓ sdk_session_id captured: {row['sdk_session_id']}")

        # 4. Replay endpoint. SDK flushes JSONL asynchronously after the
        # turn ends; give it a moment so the trailing assistant.text block
        # lands on disk before we replay.
        time.sleep(1.0)
        history = _get_json(port, f"/sessions/{sid}/messages")
        events = history.get("events", [])
        _summarise_events(events, "history")
        if not events:
            failures.append("history endpoint returned empty events list")

        types = [e.get("type") for e in events]
        if not types or types[0] != "user.prompt":
            failures.append(
                f"first replay event = {types[0]!r}, expected 'user.prompt'"
            )
        if "assistant.text" not in types:
            failures.append("replay missing assistant.text")
        if types[-1] != "result":
            failures.append(f"last replay event = {types[-1]!r}, expected 'result'")

        # Tool-use parity: if the model called Read, the replay should mirror it
        # AND the paired tool.result must reference the same tool_use_id.
        tool_uses = [e for e in events if e.get("type") == "tool.use"]
        tool_results = [e for e in events if e.get("type") == "tool.result"]
        if tool_uses:
            ids_used = {t.get("tool_use_id") for t in tool_uses}
            ids_result = {t.get("tool_use_id") for t in tool_results}
            unmatched = ids_used - ids_result
            if unmatched:
                failures.append(f"tool.use without paired tool.result: {unmatched}")
            else:
                print(f"✓ tool.use ↔ tool.result paired ({len(tool_uses)} tool calls)")

        thinking = [e for e in events if e.get("type") == "assistant.thinking"]
        print(
            f"  user.prompt={types.count('user.prompt')}  "
            f"thinking={len(thinking)}  text={types.count('assistant.text')}  "
            f"tool.use={len(tool_uses)}  tool.result={len(tool_results)}"
        )

        # 5. Cleanup.
        status = _delete(port, f"/sessions/{sid}")
        if status != 204:
            failures.append(f"DELETE /sessions/{sid} returned {status}")

    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
        if not failures:
            shutil.rmtree(data_root, ignore_errors=True)
        else:
            print(f"⚠ data_root preserved for inspection: {data_root}")

    print()
    if failures:
        print("✗ SMOKE FAILED:")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("✓ SMOKE PASSED — manifest-first reopen contract honored.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
