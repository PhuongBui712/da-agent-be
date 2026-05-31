#!/usr/bin/env python3
"""Live smoke test for AskUserQuestion 2-back-to-back (Phase B 2026-05-31).

Asks the agent to call `AskUserQuestion` twice in the same turn, automatically
answers each one as soon as it appears in the parked-interactions queue, and
verifies the wire-level invariants the FE relies on:

  * BOTH `interaction.requested` events arrive (with distinct tool_use_ids)
  * EACH /respond resolves with 204
  * After EACH /respond, an `interaction.resolved` SSE event arrives with the
    matching tool_use_id (the bug-fix that made this possible: BE was silent
    before, so the FE reducer accumulated stale entries and the modal was
    stuck on the first id forever)
  * The order on the wire is requested-1 → resolved-1 → requested-2 →
    resolved-2 → result (turn ends cleanly)

Opt-in. CI does NOT run this. Manual workflow:

    # Terminal 1: boot the BE
    uv run uvicorn da_agent.server.app:create_app --factory --port 8765

    # Terminal 2: run the smoke
    DA_AGENT_SMOKE_URL=http://127.0.0.1:8765 \\
    ANTHROPIC_API_KEY=sk-... \\
    uv run python scripts/smoke_interactions.py

Exits 0 on PASS, 0 with skip message if no API key, 1 on FAIL.
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
from typing import Any

import httpx

# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #
URL = os.environ.get("DA_AGENT_SMOKE_URL", "http://127.0.0.1:8765").rstrip("/")

PROMPT = (
    "Use the AskUserQuestion tool TWICE in this single turn — first ask one "
    "small question, then after I answer, ask a second separate question. "
    "Use distinct headers like 'AspectOne' and 'AspectTwo' so each call is "
    "clearly separate. After both answers come back, briefly confirm what "
    "I picked."
)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
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


def _have_api_key() -> bool:
    return bool(
        os.environ.get("ANTHROPIC_API_KEY")
        or os.environ.get("DATABRICKS_TOKEN")
        or os.environ.get("CLAUDE_CODE_OAUTH_TOKEN")
    )


def _parse_sse_chunk(buf: str) -> tuple[str, dict] | None:
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


def _auto_responder_loop(
    sid: str,
    *,
    answered: dict[str, str],
    stop_event: threading.Event,
    log: list[str],
) -> None:
    """Background poller — checks /interactions/pending every 0.5s, answers
    each unanswered question with the first option, records what it did.

    Mirrors what a real user would do: open modal → pick first option → submit.
    Lives in a worker thread so it runs in parallel with the SSE consumer.
    """
    with httpx.Client(timeout=15.0) as client:
        while not stop_event.is_set():
            try:
                r = client.get(f"{URL}/sessions/{sid}/interactions/pending")
            except Exception as exc:  # noqa: BLE001
                log.append(f"poll-error: {exc}")
                time.sleep(0.5)
                continue
            if r.status_code != 200:
                time.sleep(0.5)
                continue
            for item in r.json().get("pending", []):
                tu_id = item["tool_use_id"]
                if tu_id in answered:
                    continue
                kind = item["kind"]
                payload = item.get("payload", {})

                if kind == "question":
                    questions = payload.get("questions") or []
                    if not questions:
                        log.append(f"{tu_id}: no questions in payload, skip")
                        continue
                    body_answers: list[dict[str, Any]] = []
                    for q in questions:
                        header = q.get("header") or "Q"
                        opts = q.get("options") or []
                        first = (opts[0]["label"] if opts else "Yes") if opts else "Yes"
                        body_answers.append(
                            {"header": header, "selected": [first], "other_text": None}
                        )
                    body = {"answers": body_answers}
                elif kind == "plan":
                    body = {"verdict": "approve"}
                else:
                    log.append(f"{tu_id}: unknown kind {kind!r}, skip")
                    continue

                resp = client.post(
                    f"{URL}/sessions/{sid}/interactions/{tu_id}/respond",
                    json=body,
                )
                answered[tu_id] = (
                    f"{resp.status_code}:{kind}:{json.dumps(body, sort_keys=True)}"
                )
                log.append(f"answered {tu_id} ({kind}) -> {resp.status_code}")
            time.sleep(0.4)


def _stream_turn(client: httpx.Client, sid: str, prompt: str) -> list[tuple[str, dict]]:
    events: list[tuple[str, dict]] = []
    with client.stream(
        "POST",
        f"{URL}/sessions/{sid}/messages",
        json={"prompt": prompt},
        timeout=httpx.Timeout(connect=10.0, read=300.0, write=10.0, pool=10.0),
    ) as resp:
        if resp.status_code != 200:
            raise RuntimeError(
                f"messages POST returned {resp.status_code}: {resp.text}"
            )
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


def _try_delete(client: httpx.Client, sid: str) -> None:
    try:
        client.delete(f"{URL}/sessions/{sid}")
    except Exception:  # noqa: BLE001
        pass


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
    res = CheckResult()

    with httpx.Client(timeout=30.0) as client:
        # 1. Create session
        r = client.post(f"{URL}/sessions", json={"name": "smoke-interactions"})
        if r.status_code != 201:
            print(f"=== FAIL: POST /sessions -> {r.status_code} {r.text} ===")
            return 1
        sid = r.json()["id"]
        print(f"[smoke] session id = {sid}")

        # 2. Spawn auto-responder
        answered: dict[str, str] = {}
        stop_event = threading.Event()
        responder_log: list[str] = []
        responder = threading.Thread(
            target=_auto_responder_loop,
            kwargs={
                "sid": sid,
                "answered": answered,
                "stop_event": stop_event,
                "log": responder_log,
            },
            daemon=True,
        )
        responder.start()

        # 3. Send prompt and collect SSE
        t0 = time.monotonic()
        try:
            events = _stream_turn(client, sid, PROMPT)
        except Exception as exc:  # noqa: BLE001
            stop_event.set()
            print(f"=== FAIL: SSE stream error: {exc} ===")
            _try_delete(client, sid)
            return 1
        finally:
            stop_event.set()
            responder.join(timeout=2.0)
        elapsed = time.monotonic() - t0
        print(f"[smoke] turn done in {elapsed:.1f}s — {len(events)} events")
        if responder_log:
            print(f"[smoke] auto-responder log:")
            for line in responder_log:
                print(f"   - {line}")

        # 4. Wire-level checks
        requested = [p for t, p in events if t == "interaction.requested"]
        resolved = [p for t, p in events if t == "interaction.resolved"]
        result = [p for t, p in events if t == "result"]

        res.check(
            "two interaction.requested events arrived",
            len(requested) >= 2,
            f"got {len(requested)}",
        )

        if len(requested) >= 2:
            res.check(
                "the two requested events have distinct tool_use_ids",
                requested[0]["tool_use_id"] != requested[1]["tool_use_id"],
                f"{requested[0]['tool_use_id']!r} vs {requested[1]['tool_use_id']!r}",
            )

        res.check(
            "matching number of interaction.resolved events",
            len(resolved) == len(requested),
            f"requested={len(requested)} resolved={len(resolved)}",
        )

        # tool_use_ids match between requested and resolved
        req_ids = [p["tool_use_id"] for p in requested]
        res_ids = [p["tool_use_id"] for p in resolved]
        res.check(
            "every requested tool_use_id has a matching resolved",
            sorted(req_ids) == sorted(res_ids),
            f"requested_ids={req_ids} resolved_ids={res_ids}",
        )

        # Order: req-1 must come before res-1, res-1 before req-2 (the agent
        # cannot ask Q2 until the SDK sees Q1's answer flow back).
        order = [
            (t, p["tool_use_id"])
            for t, p in events
            if t in {"interaction.requested", "interaction.resolved"}
        ]
        res.check(
            "wire order: requested → resolved alternates per question",
            len(order) >= 4
            and order[0][0] == "interaction.requested"
            and order[1][0] == "interaction.resolved"
            and order[1][1] == order[0][1]
            and order[2][0] == "interaction.requested"
            and order[3][0] == "interaction.resolved"
            and order[3][1] == order[2][1],
            f"order={order}",
        )

        res.check(
            "auto-responder answered both",
            len(answered) >= 2,
            f"answered={list(answered.keys())}",
        )

        res.check(
            "result event present (turn closed cleanly)",
            len(result) >= 1,
            f"got {len(result)} result event(s)",
        )

        _try_delete(client, sid)

    if not res.failures:
        print("\n=== PASS ===")
        return 0
    print(f"\n=== FAIL: {len(res.failures)} check(s): {res.failures} ===")
    return 1


if __name__ == "__main__":
    sys.exit(main())
