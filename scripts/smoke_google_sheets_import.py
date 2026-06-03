#!/usr/bin/env python3
"""Smoke for Google Sheets URL import — KB + Attachment.

Boots the ASGI app via httpx.ASGITransport and monkeypatches the network
downloader so no live Google fetch is performed (CI-hermetic). Exercises:

  Phase 1 — POST /kb/files/import-sheet (happy: name supplied)        → 202
  Phase 2 — POST /kb/files/import-sheet (happy: name omitted)         → 202 + auto-name
  Phase 3 — POST /kb/files/import-sheet (NotPublic)                   → 403
  Phase 4 — POST /kb/files/import-sheet (InvalidUrl)                  → 400
  Phase 5 — POST /sessions/<sid>/attachments/import-sheet (happy)     → 201
  Phase 6 — POST /sessions/<sid>/attachments/import-sheet (NotFound)  → 404

Run:
    .venv/bin/python scripts/smoke_google_sheets_import.py

Exits 0 on PASS, 1 on FAIL.
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from pathlib import Path
from typing import Any

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from da_agent.config import Settings
from da_agent.server import google_sheets as gs_mod
from da_agent.server.app import create_app
from da_agent.server.routes import attachments as attachments_routes
from da_agent.server.routes import kb as kb_routes


_FAKE_XLSX_BYTES = b"PK\x03\x04" + b"\x00" * 1024


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


def _install_fake_downloaders() -> dict[str, Any]:
    """Replace `download_sheet_as_xlsx` in both routes with controllable fakes."""

    state = {"mode": "happy", "captured": []}

    async def fake_download(sheet_id, dest, *, max_bytes, **kwargs):
        state["captured"].append((sheet_id, str(dest), max_bytes))
        mode = state["mode"]
        if mode == "happy":
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(_FAKE_XLSX_BYTES)
            return len(_FAKE_XLSX_BYTES)
        if mode == "not_public":
            raise gs_mod.NotPublicError(
                "sheet is not public; share with 'Anyone with the link can view'"
            )
        if mode == "not_found":
            raise gs_mod.NotFoundError(f"sheet not found: {sheet_id}")
        if mode == "network":
            raise gs_mod.NetworkError("connection refused")
        raise RuntimeError(f"unexpected mode: {mode}")

    kb_routes.download_sheet_as_xlsx = fake_download
    attachments_routes.download_sheet_as_xlsx = fake_download

    async def fake_run_pipeline(*, registry, settings, kb_root, kb_id, profiler=None):
        await registry.update_status(kb_id, "READY")

    kb_routes.run_pipeline = fake_run_pipeline
    return state


async def _run() -> int:
    res = _Result()

    tmp_root = Path(tempfile.mkdtemp(prefix="da-agent-smoke-sheets-"))
    print(f"→ data_root: {tmp_root}")
    os.environ["DA_AGENT_HOME"] = str(tmp_root)
    settings = Settings()
    settings.data_root = tmp_root
    settings.ensure_dirs()

    state = _install_fake_downloaders()

    app = create_app(settings)
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://smoke") as cx:
            # Phase 1 — KB import with name supplied.
            print("\n[smoke] phase 1: KB import with name → 202 + name preserved")
            state["mode"] = "happy"
            url = (
                "https://docs.google.com/spreadsheets/d/"
                "1XSOLsjlPL2F6jILErWtqHvLHJujOMVa6jRlmPq-vJ48/edit?usp=sharing"
            )
            r = await cx.post(
                "/kb/files/import-sheet",
                json={"url": url, "name": "monthly_sales"},
            )
            res.check("KB import returns 202", r.status_code == 202, f"got {r.status_code}: {r.text[:200]}")
            body = r.json() if r.status_code == 202 else {}
            res.check(
                "KB filename preserves user-supplied name",
                body.get("filename", "").startswith("monthly_sales"),
                f"filename={body.get('filename')!r}",
            )
            kb_id = body.get("id", "")
            res.check("KB id starts with kb_", kb_id.startswith("kb_"), f"id={kb_id!r}")
            raw = settings.kb_dir / kb_id / "raw.xlsx" if kb_id else None
            res.check(
                "raw.xlsx written under kb_dir/<id>/",
                bool(raw and raw.is_file() and raw.stat().st_size > 0),
                f"path={raw}",
            )

            # Phase 2 — KB import without name (auto-generated).
            print("\n[smoke] phase 2: KB import without name → auto filename")
            r = await cx.post("/kb/files/import-sheet", json={"url": url})
            res.check("KB auto-name returns 202", r.status_code == 202, f"got {r.status_code}")
            body = r.json() if r.status_code == 202 else {}
            fname = body.get("filename", "")
            res.check(
                "auto filename uses imported_sheet_<id8>.xlsx",
                fname.startswith("imported_sheet_") and fname.endswith(".xlsx"),
                f"filename={fname!r}",
            )

            # Phase 3 — KB import non-public sheet → 403.
            print("\n[smoke] phase 3: KB import non-public → 403")
            state["mode"] = "not_public"
            r = await cx.post("/kb/files/import-sheet", json={"url": url})
            res.check("non-public returns 403", r.status_code == 403, f"got {r.status_code}")

            # Phase 4 — invalid URL → 400 (no monkeypatch needed; extract_sheet_id
            # raises before download_sheet_as_xlsx is called).
            print("\n[smoke] phase 4: invalid URL → 400")
            r = await cx.post(
                "/kb/files/import-sheet",
                json={"url": "https://example.com/not-a-sheet"},
            )
            res.check("invalid URL returns 400", r.status_code == 400, f"got {r.status_code}")

            # Set up a session for attachment phases.
            sess_resp = await cx.post("/sessions", json={"name": "smoke-sheets"})
            sid = sess_resp.json()["id"]

            # Phase 5 — attachment import (happy).
            print("\n[smoke] phase 5: attachment import → 201")
            state["mode"] = "happy"
            r = await cx.post(
                f"/sessions/{sid}/attachments/import-sheet",
                json={"url": url, "name": "deck_data"},
            )
            res.check("attachment import returns 201", r.status_code == 201, f"got {r.status_code}: {r.text[:200]}")
            ab = r.json() if r.status_code == 201 else {}
            att_id = ab.get("attachment_id", "")
            res.check("attachment id starts with att_", att_id.startswith("att_"), f"id={att_id!r}")
            res.check(
                "attachment filename preserves name",
                ab.get("filename", "").startswith("deck_data"),
                f"filename={ab.get('filename')!r}",
            )
            on_disk = settings.attachments_dir / sid / att_id / ab.get("filename", "")
            res.check(
                "attachment file written under attachments/<sid>/<att_id>/",
                on_disk.is_file() and on_disk.stat().st_size > 0,
                f"path={on_disk}",
            )

            # Phase 6 — attachment import non-existent sheet → 404.
            print("\n[smoke] phase 6: attachment import not-found → 404")
            state["mode"] = "not_found"
            r = await cx.post(
                f"/sessions/{sid}/attachments/import-sheet",
                json={"url": url},
            )
            res.check("not-found returns 404", r.status_code == 404, f"got {r.status_code}")

    if not res.failures:
        print("\n=== PASS ===")
        return 0
    print(f"\n=== FAIL: {len(res.failures)} check(s): {res.failures} ===")
    return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(_run()))
