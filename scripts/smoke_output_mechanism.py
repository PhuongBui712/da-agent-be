"""Live smoke for output mechanism (spec §8.2) — workspace deprecation,
AskUserQuestion target chain, symmetric KB↔attachment versioning, trigger
fence (simple Q&A vs analysis vs explicit override).

Boots a real uvicorn subprocess against an isolated `DA_AGENT_HOME=$(mktemp -d)`,
copies a small xlsx from `data/output/` into the KB and into a session
attachment, and runs a 5-phase script:

  Phase A — simple Q&A on KB              -> expect NO output, NO question
  Phase B — explicit-save override on KB  -> expect output (no question)
  Phase C — analysis on KB                -> expect AskUserQuestion → kb_version
  Phase D — second analysis on KB         -> expect rotation v_curr → v_prev
  Phase E — analysis on attachment        -> expect attachment_version

Pre-req: `ANTHROPIC_API_KEY` (or whatever credential the configured model uses)
must be available. Configurable via `DA_AGENT_MODEL`.

Run:
    uv run python scripts/smoke_output_mechanism.py
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
import uuid
from pathlib import Path

# Small, deterministic file (~150 KB) with mostly-numeric data — fast for the
# model to inspect, big enough to be worth analysing.
SOURCE_XLSX = (
    Path(__file__).resolve().parents[1]
    / "data"
    / "output"
    / "Singapore Retail Sales Index.xlsx"
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
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read())


def _get_json(port: int, path: str) -> dict:
    with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=15) as r:
        return json.loads(r.read())


def _delete(port: int, path: str) -> int:
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", method="DELETE")
    with urllib.request.urlopen(req, timeout=15) as r:
        return r.status


def _multipart_upload(port: int, path: str, file_path: Path, mime: str) -> dict:
    """Tiny multipart/form-data emitter — stdlib-only."""
    boundary = f"----smoke{uuid.uuid4().hex}"
    body = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="file"; filename="{file_path.name}"\r\n'
        f"Content-Type: {mime}\r\n\r\n"
    ).encode()
    body += file_path.read_bytes()
    body += f"\r\n--{boundary}--\r\n".encode()
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}",
        data=body,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read())


def _stream_turn(
    port: int,
    sid: str,
    prompt: str,
    *,
    deadline_s: float = 1500.0,
    on_interaction: object | None = None,
    body_extra: dict | None = None,
) -> list[dict]:
    """POST a message; if an `interaction.requested` event arrives, invoke
    the `on_interaction(event)` callback to drive the response (callback is
    expected to POST to /interactions/<id>/respond synchronously).

    Returns the full event list (including any events emitted after the
    interaction is resolved).
    """
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/sessions/{sid}/messages",
        data=json.dumps({"prompt": prompt, **(body_extra or {})}).encode(),
        headers={"Content-Type": "application/json", "Accept": "text/event-stream"},
        method="POST",
    )
    events: list[dict] = []
    deadline = time.monotonic() + deadline_s
    with urllib.request.urlopen(req, timeout=deadline_s) as r:
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
                    if rec.get("type") == "interaction.requested" and on_interaction:
                        # Fire-and-forget — callback decides what to answer.
                        on_interaction(rec)
                buf.clear()
            else:
                buf.append(line)
            if events and events[-1].get("type") == "result":
                break
    return events


def _summarise(events: list[dict], label: str) -> None:
    from collections import Counter

    types = Counter(e.get("type") for e in events)
    print(f"  [{label}] {len(events)} events: {dict(types)}")


def _wait_kb_ready(port: int, kb_id: str, *, timeout: float = 60.0) -> str:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        meta = _get_json(port, f"/kb/files/{kb_id}")
        if meta["status"] == "READY":
            return "READY"
        if meta["status"] == "FAILED":
            raise RuntimeError(f"kb {kb_id} FAILED: {meta.get('error')}")
        time.sleep(0.5)
    raise TimeoutError(f"kb {kb_id} never reached READY")


def main() -> int:
    if not SOURCE_XLSX.exists():
        print(f"✗ source xlsx missing: {SOURCE_XLSX}")
        return 2

    repo_root = Path(__file__).resolve().parents[1]
    data_root = Path(tempfile.mkdtemp(prefix="da-agent-output-smoke-"))
    port = _free_port()
    print(f"→ smoke: data_root={data_root}  port={port}")

    env = dict(os.environ)
    env["DA_AGENT_HOME"] = str(data_root)
    env["PYTHONUNBUFFERED"] = "1"

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
        try:
            _wait_healthz(port)
            print("✓ server healthy")

            # 0. Sanity: workspace_dir was deprecated — directory must NOT exist.
            if (data_root / "workspace").exists():
                failures.append("workspace dir was created — Settings.ensure_dirs leak")

            # Upload KB.
            kb_meta = _multipart_upload(
                port,
                "/kb/files",
                SOURCE_XLSX,
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            )
            kb_id = kb_meta["id"]
            print(f"✓ kb uploaded: {kb_id}  status={kb_meta['status']}")
            _wait_kb_ready(port, kb_id)
            print(f"✓ kb {kb_id} READY")

            # Create session for attachment phases later.
            sess = _post_json(port, "/sessions", {"name": "smoke-output"})
            sid = sess["id"]
            print(f"✓ session: {sid}  name={sess['name']!r}")

            # Upload attachment to that session.
            att_meta = _multipart_upload(
                port,
                f"/sessions/{sid}/attachments",
                SOURCE_XLSX,
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            )
            att_id = att_meta["attachment_id"]
            print(f"✓ attachment uploaded: {att_id}")

            kb_versions_dir = data_root / "kb" / kb_id / "versions"
            att_versions_dir = data_root / "attachments" / sid / att_id / "versions"

            # ---------------------------------------------------------------
            # Phase A — simple Q&A on KB (no output expected)
            # ---------------------------------------------------------------
            print("\n=== Phase A — simple Q&A (no output expected) ===")
            events_a = _stream_turn(
                port,
                sid,
                "Có bao nhiêu sheet trong file Singapore Retail Sales Index? "
                "Trả lời ngắn gọn bằng tiếng Việt.",
            )
            _summarise(events_a, "A")
            if any(e.get("type") == "output.created" for e in events_a):
                failures.append("Phase A unexpectedly emitted output.created")
            if any(
                e.get("type") == "interaction.requested"
                and e.get("kind") == "question"
                and e.get("questions", [{}])[0].get("header") == "Target"
                for e in events_a
            ):
                failures.append(
                    "Phase A unexpectedly asked AskUserQuestion(Target,Source)"
                )

            # ---------------------------------------------------------------
            # Phase B — explicit-save override (output expected, no question)
            # ---------------------------------------------------------------
            print(
                "\n=== Phase B — explicit save (output expected, no Target question) ==="
            )
            events_b = _stream_turn(
                port,
                sid,
                "Liệt kê 3 dòng đầu tiên của sheet đầu tiên và XUẤT KẾT QUẢ ra "
                "MỘT FILE EXCEL MỚI (New .xlsx). Tên file: head3.xlsx.",
                body_extra={"kb_scope": [kb_id]},
            )
            _summarise(events_b, "B")
            out_b = [e for e in events_b if e.get("type") == "output.created"]
            if not out_b:
                failures.append("Phase B did not emit output.created")
            else:
                ev = out_b[0]
                if ev.get("kind") != "standalone":
                    failures.append(
                        f"Phase B output.kind = {ev.get('kind')!r}, expected 'standalone'"
                    )

            # ---------------------------------------------------------------
            # Phase C — analysis on KB (must ask AskUserQuestion)
            # We answer (Target=New sheet, Source=kb_<id>); expect kb_version.
            # ---------------------------------------------------------------
            print("\n=== Phase C — analysis on KB (expect Target question) ===")

            def _answer_kb_new_sheet(ev: dict) -> None:
                tu_id = ev["tool_use_id"]
                _post_json(
                    port,
                    f"/sessions/{sid}/interactions/{tu_id}/respond",
                    {
                        "answers": [
                            {"header": "Target", "selected": ["New sheet"]},
                            {"header": "Source", "selected": [kb_id]},
                        ]
                    },
                )

            events_c = _stream_turn(
                port,
                sid,
                "Phân tích xu hướng chỉ số bán lẻ qua các năm và đưa ra insight "
                "với một sheet tổng kết. Trả lời tiếng Việt.",
                on_interaction=_answer_kb_new_sheet,
                body_extra={"kb_scope": [kb_id]},
            )
            _summarise(events_c, "C")
            out_c = [e for e in events_c if e.get("type") == "output.created"]
            if not out_c:
                failures.append(
                    "Phase C did not emit output.created (kb_version expected)"
                )
            else:
                ev = out_c[0]
                if ev.get("kind") != "kb_version":
                    failures.append(
                        f"Phase C output.kind = {ev.get('kind')!r}, expected 'kb_version'"
                    )
                if ev.get("version") != "v_curr":
                    failures.append(
                        f"Phase C version = {ev.get('version')!r}, expected 'v_curr'"
                    )
            # Disk: only v_curr should exist.
            v_curr = (
                list(kb_versions_dir.glob("v_curr.*"))
                if kb_versions_dir.exists()
                else []
            )
            v_prev = (
                list(kb_versions_dir.glob("v_prev.*"))
                if kb_versions_dir.exists()
                else []
            )
            if not v_curr:
                failures.append(f"Phase C: no v_curr.* in {kb_versions_dir}")
            if v_prev:
                failures.append(
                    f"Phase C: unexpected v_prev.* (rotation should not have happened): {v_prev}"
                )

            # ---------------------------------------------------------------
            # Phase D — second write rotates v_curr → v_prev
            # ---------------------------------------------------------------
            print("\n=== Phase D — second analysis (expect rotation) ===")
            events_d = _stream_turn(
                port,
                sid,
                "Thêm một sheet nữa với tổng theo từng tháng. Trả lời tiếng Việt.",
                on_interaction=_answer_kb_new_sheet,
                body_extra={"kb_scope": [kb_id]},
            )
            _summarise(events_d, "D")
            out_d = [e for e in events_d if e.get("type") == "output.created"]
            if not out_d:
                failures.append("Phase D did not emit output.created")
            v_curr = list(kb_versions_dir.glob("v_curr.*"))
            v_prev = list(kb_versions_dir.glob("v_prev.*"))
            if not v_curr or not v_prev:
                failures.append(
                    f"Phase D: expected BOTH v_curr.* and v_prev.* "
                    f"(curr={v_curr}, prev={v_prev})"
                )

            # ---------------------------------------------------------------
            # Phase E — attachment analysis (Target=New sheet, Source=att_<id>)
            # ---------------------------------------------------------------
            print("\n=== Phase E — attachment analysis (expect attachment_version) ===")

            def _answer_att_new_sheet(ev: dict) -> None:
                tu_id = ev["tool_use_id"]
                _post_json(
                    port,
                    f"/sessions/{sid}/interactions/{tu_id}/respond",
                    {
                        "answers": [
                            {"header": "Target", "selected": ["New sheet"]},
                            {"header": "Source", "selected": [att_id]},
                        ]
                    },
                )

            events_e = _stream_turn(
                port,
                sid,
                "Thêm một sheet tóm tắt vào file ATTACHMENT vừa upload. "
                "Trả lời tiếng Việt.",
                on_interaction=_answer_att_new_sheet,
                body_extra={"attachments": [{"attachment_id": att_id}]},
            )
            _summarise(events_e, "E")
            out_e = [e for e in events_e if e.get("type") == "output.created"]
            if not out_e:
                failures.append("Phase E did not emit output.created")
            else:
                ev = out_e[0]
                if ev.get("kind") != "attachment_version":
                    failures.append(
                        f"Phase E kind = {ev.get('kind')!r}, expected 'attachment_version'"
                    )
            v_curr_att = (
                list(att_versions_dir.glob("v_curr.*"))
                if att_versions_dir.exists()
                else []
            )
            if not v_curr_att:
                failures.append(f"Phase E: no v_curr.* under {att_versions_dir}")

            # Cleanup.
            _delete(port, f"/sessions/{sid}")
        except Exception as exc:
            failures.append(f"smoke crashed mid-flow: {type(exc).__name__}: {exc}")

    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
        if not failures:
            shutil.rmtree(data_root, ignore_errors=True)
        else:
            print(f"\n⚠ data_root preserved for inspection: {data_root}")

    print()
    if failures:
        print("✗ SMOKE FAILED:")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("✓ SMOKE PASSED — output mechanism honored across all 5 phases.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
