#!/usr/bin/env python3
"""Live smoke for the Vietnamese pptx delegation contract (Bug #2 + #3 + #4).

Sends a Vietnamese deliverable prompt, auto-answers AskUserQuestion with
Target=`New .pptx` / Source=`N/A`, and verifies four invariants in one
turn:

  1. The main agent dispatches the `reporter` subagent (does NOT build
     the .pptx itself — Bug #3, skill cut to xlsx + data-analysis).
  2. The dispatch prompt names `working_dir=` and `output_path=` per
     `<delegation_rules>` (Bug #2).
  3. The dispatch prompt forwards the user's verbatim Vietnamese text
     (Bug #4 — main agent must not transliterate before delegating).
  4. The .pptx slide XML preserves at least one Vietnamese diacritic
     token (`chó`) — Bug #4 reporter-side preservation.

Run after starting the BE locally:

    .venv/bin/python -m uvicorn da_agent.server.app:create_app \\
        --factory --port 8765
    DA_AGENT_SMOKE_URL=http://127.0.0.1:8765 \\
    .venv/bin/python scripts/smoke_pptx_delegation.py

Exits 0 on PASS, 1 on FAIL, 0 with skip if no API key.
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
from pathlib import Path

import httpx

URL = os.environ.get("DA_AGENT_SMOKE_URL", "http://127.0.0.1:8765").rstrip("/")
DATA_HOME = Path(os.environ.get("DA_AGENT_HOME", str(Path.home() / ".da-agent")))
OUTPUTS_ROOT = DATA_HOME / "outputs"
PROMPT = "Tạo 3-slide presentation mô tả con chó."


def _have_api_key() -> bool:
    keys = (
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_AUTH_TOKEN",
        "DATABRICKS_TOKEN",
        "CLAUDE_CODE_OAUTH_TOKEN",
    )
    if any(os.environ.get(k) for k in keys):
        return True
    settings_path = Path(__file__).resolve().parent.parent / ".claude" / "settings.local.json"
    if settings_path.exists():
        try:
            data = json.loads(settings_path.read_text(encoding="utf-8"))
            return bool((data.get("env") or {}).get("ANTHROPIC_AUTH_TOKEN"))
        except Exception:
            return False
    return False


def _parse_sse(buf: str) -> tuple[str, dict] | None:
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
        payload = json.loads("\n".join(data_lines))
    except json.JSONDecodeError:
        return None
    return event_type, payload


def _stream(client: httpx.Client, sid: str) -> list[tuple[str, dict]]:
    events: list[tuple[str, dict]] = []
    answer_threads: list[threading.Thread] = []

    def _post_answer(tool_use_id: str, answers: list[dict]) -> None:
        with httpx.Client(timeout=15.0) as bg:
            try:
                bg.post(
                    f"{URL}/sessions/{sid}/interactions/{tool_use_id}/respond",
                    json={"answers": answers},
                )
            except Exception as exc:
                events.append(("interaction.respond_failed", {"err": repr(exc)}))

    with client.stream(
        "POST",
        f"{URL}/sessions/{sid}/messages",
        json={"prompt": PROMPT},
        # Vietnamese pptx generation can run 5+ min end-to-end (planning +
        # subagent slide composition). Generous read timeout.
        timeout=httpx.Timeout(connect=10.0, read=600.0, write=10.0, pool=10.0),
    ) as resp:
        if resp.status_code != 200:
            raise RuntimeError(f"messages POST {resp.status_code}: {resp.text}")
        chunk: list[str] = []
        for raw in resp.iter_lines():
            line = raw.rstrip("\r")
            if line == "":
                if chunk:
                    parsed = _parse_sse("\n".join(chunk))
                    if parsed is not None:
                        events.append(parsed)
                        et, ed = parsed
                        if et == "interaction.requested":
                            tool_use_id = ed.get("tool_use_id", "")
                            qs = ed.get("questions", [])
                            # `AnswerSubmission` shape: header + selected[].
                            # The label-only shape is silently ignored by the
                            # Pydantic validator (extra=ignore) and produces
                            # `selected=[]`, which the resolver rejects with
                            # "Target answer is empty". Send the canonical
                            # shape the FE submits.
                            answers = []
                            for q in qs:
                                header = (q.get("header") or "")
                                if header.lower() == "source":
                                    answers.append(
                                        {"header": header, "selected": ["N/A"]}
                                    )
                                else:
                                    answers.append(
                                        {"header": header, "selected": ["New .pptx"]}
                                    )
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
            parsed = _parse_sse("\n".join(chunk))
            if parsed is not None:
                events.append(parsed)
    for t in answer_threads:
        t.join(timeout=5.0)
    return events


class _Result:
    def __init__(self) -> None:
        self.failures: list[str] = []

    def check(self, label: str, ok: bool, detail: str = "") -> None:
        suffix = f" — {detail}" if detail else ""
        if ok:
            print(f"  PASS  {label}{suffix}")
        else:
            print(f"  FAIL  {label}{suffix}")
            self.failures.append(label)


def main() -> int:
    if not _have_api_key():
        print("[skip] no API key in env or settings.local.json. Smoke is opt-in; exit 0.")
        return 0

    print(f"[smoke] BE URL = {URL}")
    print(f"[smoke] data home = {DATA_HOME}")
    res = _Result()

    with httpx.Client(timeout=30.0) as client:
        r = client.post(f"{URL}/sessions", json={"name": "smoke-vn-pptx"})
        if r.status_code != 201:
            print(f"=== FAIL: POST /sessions -> {r.status_code} {r.text} ===")
            return 1
        sid = r.json()["id"]
        print(f"[smoke] session id = {sid}")

        t0 = time.monotonic()
        try:
            events = _stream(client, sid)
        except Exception as exc:  # noqa: BLE001
            print(f"=== FAIL: SSE stream error: {exc} ===")
            try:
                client.delete(f"{URL}/sessions/{sid}")
            except Exception:
                pass
            return 1
        elapsed = time.monotonic() - t0
        print(f"[smoke] turn done in {elapsed:.1f}s — {len(events)} events")

        # 1. Reporter dispatched.
        reporter_dispatches = [
            (t, p) for (t, p) in events
            if t == "tool.use"
            and p.get("name") == "Agent"
            and (p.get("input") or {}).get("subagent_type") == "reporter"
        ]
        res.check(
            "main agent dispatches reporter (no self-build of .pptx)",
            len(reporter_dispatches) >= 1,
            f"got {len(reporter_dispatches)} reporter dispatch(es)",
        )

        # 2 & 3. Dispatch prompt contract.
        if reporter_dispatches:
            first = (reporter_dispatches[0][1].get("input") or {})
            dispatch = str(first.get("prompt", ""))
            res.check(
                "dispatch prompt names working_dir=",
                "working_dir=" in dispatch,
                f"prompt[:200]={dispatch[:200]!r}",
            )
            res.check(
                "dispatch prompt names output_path=",
                "output_path=" in dispatch,
                f"prompt[:200]={dispatch[:200]!r}",
            )
            res.check(
                "dispatch prompt preserves Vietnamese diacritics (`chó`)",
                "chó" in dispatch,
                "main agent stripped diacritics before delegating",
            )

        # 4. Output is .pptx with diacritic-preserving slide XML.
        outputs = [(t, p) for (t, p) in events if t == "output.created"]
        res.check(
            "output.created arrived",
            len(outputs) >= 1,
            f"got {len(outputs)} event(s)",
        )
        if outputs:
            filename = outputs[0][1].get("filename", "")
            res.check(
                "output filename ends in .pptx",
                filename.lower().endswith(".pptx"),
                f"filename={filename!r}",
            )
            on_disk = OUTPUTS_ROOT / sid / filename
            res.check(
                ".pptx exists on disk under outputs/<sid>/",
                on_disk.exists(),
                f"path={on_disk}",
            )
            if on_disk.exists():
                import zipfile

                slide_xml = ""
                try:
                    with zipfile.ZipFile(on_disk) as z:
                        for name in z.namelist():
                            if name.startswith("ppt/slides/slide") and name.endswith(".xml"):
                                slide_xml += z.read(name).decode("utf-8", errors="replace")
                except Exception as exc:  # noqa: BLE001
                    res.check(".pptx unzippable", False, f"{type(exc).__name__}: {exc}")
                res.check(
                    "slide XML preserves Vietnamese (`chó` token)",
                    "chó" in slide_xml,
                    "diacritic-loss bug — only `cho`/no Vietnamese token in slides",
                )

        try:
            client.delete(f"{URL}/sessions/{sid}")
        except Exception:
            pass

    if not res.failures:
        print("\n=== PASS ===")
        return 0
    print(f"\n=== FAIL: {len(res.failures)} check(s): {res.failures} ===")
    return 1


if __name__ == "__main__":
    sys.exit(main())
