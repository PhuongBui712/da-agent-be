#!/usr/bin/env python3
"""Live smoke test for the session-scoped outputs pipeline (Phase C 2026-05-31).

Asks the agent to create a tiny .xlsx via the running BE, then verifies:
  * an `output.created` SSE event arrives with a session-scoped download_url
  * the file is on disk under `~/.da-agent/outputs/<sid>/<out_id>/...`
  * assistant text contains NO absolute path leak (path-scrubbing works)
  * `DELETE /sessions/<sid>` wipes the per-session outputs subtree

Loads credentials from (1) shell env vars, then (2) repo-root `.env` file if missing.

Opt-in. CI does NOT run this. Run manually after starting the BE locally:

    # Terminal 1: boot the backend
    uv run uvicorn da_agent.server.app:create_app --factory --port 8765

    # Terminal 2: run the smoke
    DA_AGENT_SMOKE_URL=http://127.0.0.1:8765 \\
    ANTHROPIC_API_KEY=sk-... \\
    uv run python scripts/smoke_outputs.py

Exits 0 on PASS, 0 with skip message if no API key, 1 on FAIL.
"""

from __future__ import annotations

import json
import os
import re
import sys
import threading
import time
from pathlib import Path

import httpx

# --------------------------------------------------------------------------- #
# Config + helpers
# --------------------------------------------------------------------------- #
URL = os.environ.get("DA_AGENT_SMOKE_URL", "http://127.0.0.1:8765").rstrip("/")
DATA_HOME = Path(os.environ.get("DA_AGENT_HOME", str(Path.home() / ".da-agent")))
OUTPUTS_ROOT = DATA_HOME / "outputs"

DOWNLOAD_URL_RE = re.compile(r"^/outputs/out_[0-9a-f]{16}$")
# Any absolute path-looking segment ending in a known output extension.
LEAK_RE = re.compile(r"/(?:data|home|tmp|var)/[\w/.-]+\.(?:xlsx|pptx|docx|csv)")

PROMPT = (
    "Create a tiny .xlsx with one row of data: column A 'Hello', column B 'World'. "
    "Then mention only the filename in your reply — never paste an absolute path."
)


class CheckResult:
    """Tiny PASS/FAIL recorder; final summary reads `failures`."""

    def __init__(self) -> None:
        self.failures: list[str] = []

    def check(self, label: str, ok: bool, detail: str = "") -> None:
        suffix = f" — {detail}" if detail else ""
        if ok:
            print(f"  PASS  {label}{suffix}")
        else:
            print(f"  FAIL  {label}{suffix}")
            self.failures.append(label)


def _maybe_load_dotenv() -> None:
    """Best-effort .env loader at repo root. Sets only vars not already in os.environ."""
    env_path = Path(__file__).resolve().parent.parent / ".env"
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
    keys = ("ANTHROPIC_API_KEY", "DATABRICKS_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN")
    if any(os.environ.get(k) for k in keys):
        return True
    _maybe_load_dotenv()
    return any(os.environ.get(k) for k in keys)


def _parse_sse_chunk(buf: str) -> tuple[str, dict] | None:
    """Parse one SSE block (event/data lines, terminated by blank line).

    Returns (event_type, payload) or None if no JSON parse is possible.
    """
    event_type = "message"
    data_lines: list[str] = []
    for line in buf.splitlines():
        if line.startswith("event:"):
            event_type = line[len("event:") :].strip()
        elif line.startswith("data:"):
            data_lines.append(line[len("data:") :].lstrip())
    if not data_lines:
        return None
    try:
        return event_type, json.loads("\n".join(data_lines))
    except json.JSONDecodeError:
        return None


def _stream_turn(client: httpx.Client, sid: str, prompt: str) -> list[tuple[str, dict]]:
    """POST /sessions/<sid>/messages and collect every parsed SSE event.

    Times out generously (4 minutes) — model writes can take a while. The
    server closes the stream when the turn ends, which is our exit signal.
    """
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


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
def main() -> int:
    if not _have_api_key():
        print(
            "[skip] no API key in env (ANTHROPIC_API_KEY / DATABRICKS_TOKEN / "
            "CLAUDE_CODE_OAUTH_TOKEN). Smoke is opt-in; exiting 0."
        )
        return 0

    print(f"[smoke] BE URL = {URL}")
    print(f"[smoke] data home = {DATA_HOME}")
    res = CheckResult()

    with httpx.Client(timeout=30.0) as client:
        # --- 1. Create session ----------------------------------------------
        r = client.post(f"{URL}/sessions", json={"name": "smoke-outputs"})
        if r.status_code != 201:
            print(f"=== FAIL: POST /sessions -> {r.status_code} {r.text} ===")
            return 1
        sid = r.json()["id"]
        print(f"[smoke] session id = {sid}")

        # --- 2. Send prompt and collect SSE ---------------------------------
        t0 = time.monotonic()
        try:
            events = _stream_turn(client, sid, PROMPT)
        except Exception as exc:  # noqa: BLE001 — surface any error verbatim
            print(f"=== FAIL: SSE stream error: {exc} ===")
            _try_delete(client, sid)
            return 1
        elapsed = time.monotonic() - t0
        print(f"[smoke] turn done in {elapsed:.1f}s — {len(events)} events")

        # --- 3. Aggregate --------------------------------------------------
        output_events = [(t, p) for t, p in events if t == "output.created"]
        text_chunks = [
            p.get("text", "")
            for (t, p) in events
            if t in {"assistant.text", "assistant.text.delta"}
        ]
        full_text = "".join(text_chunks)

        # 3a. output.created arrived
        res.check(
            "output.created event arrived",
            len(output_events) >= 1,
            f"got {len(output_events)} event(s)",
        )
        if not output_events:
            print(f"=== FAIL: no output.created event (sid={sid}) ===")
            _try_delete(client, sid)
            return 1

        _, payload = output_events[0]
        output_id = payload.get("output_id", "")
        download_url = payload.get("download_url", "")
        res.check(
            "download_url shape",
            bool(DOWNLOAD_URL_RE.match(download_url)),
            f"got '{download_url}'",
        )

        # 3b. HEAD download_url returns 200
        head = client.head(f"{URL}{download_url}")
        res.check(
            "HEAD download_url returns 200",
            head.status_code == 200,
            f"status={head.status_code}",
        )

        # 3c. On-disk file exists under outputs/<sid>/ (Phase C flat layout)
        sid_dir = OUTPUTS_ROOT / sid
        files = (
            sorted(p for p in sid_dir.glob("*.xlsx") if not p.name.startswith("."))
            if sid_dir.exists()
            else []
        )
        res.check(
            "on-disk file exists under outputs/<sid>/",
            len(files) >= 1,
            f"dir={sid_dir} files={[f.name for f in files]}",
        )

        # 3d. No absolute-path leak in assistant.text
        leaks = LEAK_RE.findall(full_text)
        res.check(
            "no absolute path leak in assistant.text",
            not leaks,
            f"leaks={leaks[:3]}" if leaks else "",
        )

        # --- 4. DELETE session and verify cleanup ---------------------------
        d = client.delete(f"{URL}/sessions/{sid}")
        res.check(
            "DELETE /sessions/<sid> returns 204",
            d.status_code == 204,
            f"status={d.status_code}",
        )

        # Give the registry a tick (delete_session_outputs is awaited
        # in the route, so by the time the response returns it's done — but
        # rmtree is filesystem-level and can race in odd environments).
        time.sleep(0.2)

        res.check(
            "outputs/<sid>/ removed after delete",
            not sid_dir.exists(),
            f"dir={sid_dir}",
        )

        # --- 7. Dummy retail excel (end-to-end user scenario) ---------------
        print("\n[smoke] phase 7: dummy retail excel")
        r2 = client.post(f"{URL}/sessions", json={"name": "smoke-retail"})
        if r2.status_code != 201:
            print(f"=== FAIL: POST /sessions (phase 7) -> {r2.status_code} {r2.text} ===")
            return 1
        sid_2 = r2.json()["id"]
        print(f"[smoke] phase-7 session id = {sid_2}")

        retail_prompt = (
            "Tạo 1 file excel dummy về chủ đề retail (3-5 cột, 5-10 hàng dữ liệu giả)."
        )
        try:
            events_2 = _stream_turn_with_autoanswer(client, sid_2, retail_prompt)
        except Exception as exc:  # noqa: BLE001
            print(f"=== FAIL: SSE stream error (phase 7): {exc} ===")
            _try_delete(client, sid_2)
            return 1
        print(f"[smoke] phase-7 turn done — {len(events_2)} events")

        output_events_2 = [(t, p) for t, p in events_2 if t == "output.created"]

        # 7a. at least 1 output.created event
        res.check(
            "phase7: output.created event arrived",
            len(output_events_2) >= 1,
            f"got {len(output_events_2)} event(s)",
        )

        # 7b. exactly 1 .xlsx on disk under outputs/<sid_2>/
        sid_2_dir = OUTPUTS_ROOT / sid_2
        xlsx_files = (
            [p for p in sid_2_dir.iterdir() if p.suffix == ".xlsx"]
            if sid_2_dir.exists()
            else []
        )
        res.check(
            "phase7: exactly 1 .xlsx under outputs/<sid_2>/",
            len(xlsx_files) == 1,
            f"dir={sid_2_dir} xlsx_files={[f.name for f in xlsx_files]}",
        )

        # 7c. GET /outputs?session_id=<sid_2> returns >= 1 entry with .xlsx filename
        list_r = client.get(f"{URL}/outputs", params={"session_id": sid_2})
        entries = list_r.json() if list_r.status_code == 200 else []
        xlsx_entries = [e for e in entries if str(e.get("filename", "")).endswith(".xlsx")]
        res.check(
            "phase7: GET /outputs?session_id returns >= 1 .xlsx entry",
            len(xlsx_entries) >= 1,
            f"status={list_r.status_code} entries={len(entries)} xlsx={len(xlsx_entries)}",
        )

        _try_delete(client, sid_2)

    # --- Summary -----------------------------------------------------------
    if not res.failures:
        print("\n=== PASS ===")
        return 0
    print(f"\n=== FAIL: {len(res.failures)} check(s): {res.failures} ===")
    return 1


def _stream_turn_with_autoanswer(
    client: httpx.Client, sid: str, prompt: str
) -> list[tuple[str, dict]]:
    """Stream a turn that may include AskUserQuestion interactions.

    When `interaction.requested` arrives mid-stream, the response is posted
    from a background thread (separate httpx.Client — sharing the foreground
    client across threads is unsafe) so the server's awaited future resolves
    and more SSE events flow on the same connection.
    """
    events: list[tuple[str, dict]] = []
    answer_threads: list[threading.Thread] = []

    def _post_answer(tool_use_id: str, answers: list[dict]) -> None:
        # Use a fresh client because httpx.Client is not thread-safe across
        # streams; the foreground client owns the open stream connection.
        with httpx.Client(timeout=15.0) as bg:
            try:
                bg.post(
                    f"{URL}/sessions/{sid}/interactions/{tool_use_id}/respond",
                    json={"answers": answers},
                )
            except Exception:
                # surface the failure into events so the caller sees it
                events.append(("interaction.respond_failed", {"tool_use_id": tool_use_id}))

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
                        ev_type, ev_data = parsed
                        if ev_type == "interaction.requested":
                            tool_use_id = ev_data.get("tool_use_id", "")
                            questions = ev_data.get("questions", [])
                            answers = []
                            for q in questions:
                                header = (q.get("header") or "").lower()
                                # Q1 is "Target"; Q2 is "Source" (or absent for standalone)
                                if "source" in header:
                                    answers.append({"label": "N/A"})
                                else:
                                    answers.append({"label": "New .xlsx"})
                            t = threading.Thread(
                                target=_post_answer,
                                args=(tool_use_id, answers),
                                daemon=True,
                            )
                            t.start()
                            answer_threads.append(t)
                chunk = []
            else:
                chunk.append(line)
        if chunk:
            parsed = _parse_sse_chunk("\n".join(chunk))
            if parsed is not None:
                events.append(parsed)

    for t in answer_threads:
        t.join(timeout=5.0)

    return events


def _try_delete(client: httpx.Client, sid: str) -> None:
    """Best-effort session cleanup on early failure paths."""
    try:
        client.delete(f"{URL}/sessions/{sid}")
    except Exception:
        pass


if __name__ == "__main__":
    sys.exit(main())
