"""POST a turn → SSE event stream.

Per-message lifecycle:
1. Acquire session lock (serialize messages within a session).
2. Lazy-init the session's `AgentRunner` + `WebAgentUI` on first message.
3. Spawn a background task running `runner.send(prompt)`; events flow into a
   per-turn `TurnStream`.
4. The SSE response body iterates the stream until the runner closes it (sentinel).
5. On client disconnect, cancel the runner task and release the lock.
"""

from __future__ import annotations

import asyncio
import json
import mimetypes
import secrets
import time
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse

from ...agent.core import AgentRunner
from ...agent.permissions import TargetResolution, TargetValidationError
from ...outputs import OutputDetection
from ..schemas import MessageRequest
from ..scope import build_scope, render_scope
from ..sse import format_event
from ..state import AppState, SessionRuntime, TurnStream
from ..web_ui import WebAgentUI

router = APIRouter(prefix="/sessions", tags=["messages"])


def get_state(request: Request) -> AppState:
    return request.app.state.app_state


def _new_output_id() -> str:
    """Mint `out_<16hex>` matching `OutputsRegistry._new_id` shape."""
    return f"out_{secrets.token_hex(8)}"


def _suffix_for(filename: str) -> str:
    """Return `.<ext>` (lowercased) or `.xlsx` if there's no extension."""
    ext = Path(filename).suffix.lower()
    return ext or ".xlsx"


def _rotate_versions_dir(versions_dir: Path, new_ext: str) -> None:
    """Spec §8.2 — pre-write rotation: `v_curr.* → v_prev.*` (cap at 2).

    Run BEFORE the model writes its new `v_curr.<ext>`. Drops any existing
    `v_prev.*` (regardless of extension) to keep the cap, then renames the
    current `v_curr.*` to `v_prev.<that ext>`. Sidecars (`<slot>.meta.json`)
    follow the same rotation so listings stay coherent.
    """
    if not versions_dir.exists():
        versions_dir.mkdir(parents=True, exist_ok=True)
        return
    # Drop ANY v_prev (even foreign extensions) — the new rotation will land
    # the current v_curr's extension.
    for entry in list(versions_dir.iterdir()):
        if entry.name.startswith("v_prev."):
            entry.unlink(missing_ok=True)
    # Rotate any v_curr.* -> v_prev.<same ext>; same for the meta sidecar.
    for entry in list(versions_dir.iterdir()):
        if entry.name.startswith("v_curr.") and entry.is_file():
            ext = entry.name.split(".", 1)[1]
            entry.rename(versions_dir / f"v_prev.{ext}")


def _intersection_kb_options(
    kb_metas: list[Any], kb_scope: list[str] | None
) -> list[str]:
    """READY KB ∩ kb_scope (None == all). Returns kb_id list."""
    ids = [m.id for m in kb_metas if m.status == "READY"]
    if kb_scope is None:
        return ids
    keep = set(kb_scope)
    return [kid for kid in ids if kid in keep]


async def _resolve_output_target(
    *,
    raw_questions: list[dict[str, Any]],
    raw_answers: list[dict[str, Any]],
    state: AppState,
    sid: str,
) -> TargetResolution:
    """Spec §8.2 — validate (Target, Source) and resolve absolute write path.

    Performs pre-write rotation (`v_curr → v_prev`) for KB/attachment writes
    so the next observer-detected write lands in a freshly-cleared `v_curr`
    slot. Raises `TargetValidationError` on any validation failure (the
    permission gate maps it to `PermissionResultDeny`).
    """
    if len(raw_answers) < 2:
        raise TargetValidationError(
            f"expected two answers (Target, Source); got {len(raw_answers)}"
        )
    target = (raw_answers[0].get("selected") or [""])[0].strip()
    source = (raw_answers[1].get("selected") or [""])[0].strip()
    if not target:
        raise TargetValidationError("Target answer is empty")

    # Resolve KB and attachment lookups once.
    kb_metas = await state.kb.list()
    att_metas = await state.attachments.list(sid)

    if target == "New .xlsx":
        # Source is N/A (or any answer). Mint a new output_id and a default
        # filename. The model picks the actual filename when it writes; we
        # simply give it a sandboxed directory.
        output_id = _new_output_id()
        out_dir = state.settings.outputs_dir / output_id
        out_dir.mkdir(parents=True, exist_ok=True)
        # Default filename mirrors the spec example.
        filename = "output.xlsx"
        return TargetResolution(
            resolved_target_path=str(out_dir / filename),
            resolved_target_kind="standalone",
        )

    if target in {"New sheet", "Pick sheet"}:
        # Source must point at a KB or attachment. We accept both
        # `kb_<id>` / `kb_<id>::<sheet>` and `att_<id>` / `att_<id>::<sheet>`.
        if not source or source == "N/A":
            raise TargetValidationError(f"Source is required when Target = '{target}'")
        head = source.split("::", 1)[0]

        if head.startswith("kb_"):
            allowed = set(_intersection_kb_options(kb_metas, None))
            if head not in allowed:
                raise TargetValidationError(f"unknown or non-READY kb_id: {head}")
            meta = next(m for m in kb_metas if m.id == head)
            ext = _suffix_for(meta.filename)
            versions_dir = state.settings.kb_dir / head / "versions"
            _rotate_versions_dir(versions_dir, ext)
            return TargetResolution(
                resolved_target_path=str(versions_dir / f"v_curr{ext}"),
                resolved_target_kind="kb_version",
            )
        if head.startswith("att_"):
            att = next((a for a in att_metas if a.id == head), None)
            if att is None:
                raise TargetValidationError(f"unknown attachment_id: {head}")
            ext = _suffix_for(att.filename)
            versions_dir = state.settings.attachments_dir / sid / head / "versions"
            _rotate_versions_dir(versions_dir, ext)
            return TargetResolution(
                resolved_target_path=str(versions_dir / f"v_curr{ext}"),
                resolved_target_kind="attachment_version",
            )
        raise TargetValidationError(
            f"Source must start with 'kb_' or 'att_'; got '{source}'"
        )

    raise TargetValidationError(
        f"Target must be one of 'New .xlsx', 'New sheet', 'Pick sheet'; got '{target}'"
    )


def _write_version_sidecar(
    *,
    versions_dir: Path,
    version: str,
    kind: str,
    file_path: Path,
    source_session_id: str | None,
) -> None:
    """Best-effort sidecar `versions/<version>.meta.json` (spec §8.2)."""
    sidecar_path = versions_dir / f"{version}.meta.json"
    try:
        size = file_path.stat().st_size if file_path.exists() else 0
        sidecar_path.parent.mkdir(parents=True, exist_ok=True)
        sidecar_path.write_text(
            json.dumps(
                {
                    "version": version,
                    "parent_version": "v_prev" if version == "v_curr" else None,
                    "kind": kind,
                    "operation": None,
                    "sheet_affected": None,
                    "source_session_id": source_session_id,
                    "created_at": time.time(),
                    "size_bytes": size,
                },
                indent=2,
            ),
            encoding="utf-8",
        )
    except OSError:
        # Sidecar is best-effort — the version file itself is the source of
        # truth (spec §7).
        pass


async def _ensure_runner(runtime: SessionRuntime, state: AppState) -> None:
    if runtime.runner is not None:
        return
    sid = runtime.meta.id

    def on_sdk_session_id(sdk_uuid: str) -> None:
        # Fire-and-forget capture from `SystemMessage(subtype="init")` —
        # `WebAgentUI.on_system` runs in the runner task; we hop back to
        # the async registry to persist. Idempotent inside the registry.
        # add_done_callback retrieves any exception so it doesn't surface
        # as a "Task exception was never retrieved" warning on shutdown.
        task = asyncio.create_task(state.registry.set_sdk_session_id(sid, sdk_uuid))
        task.add_done_callback(lambda t: t.exception() if not t.cancelled() else None)

    ui = WebAgentUI(
        session_id=sid, app_state=state, on_sdk_session_id=on_sdk_session_id
    )

    def on_output_detection(det: OutputDetection) -> None:
        # Bridge sync observer -> async registry + UI emission. Fire-and-forget;
        # not tracked alongside KB ingestion tasks because lifecycle differs
        # (per-turn — best-effort, OK to be cancelled on shutdown).
        asyncio.create_task(
            _handle_output_detection(det, runtime=runtime, state=state, ui=ui)
        )

    async def resolve_target(
        raw_questions: list[dict[str, Any]],
        raw_answers: list[dict[str, Any]],
    ) -> TargetResolution:
        # Spec §8.2 — invoked from `permissions.py` after the FE answer
        # arrives. Validates the (Target, Source) pair, rotates v_curr→v_prev
        # for KB/attachment writes, and returns the resolved absolute path.
        return await _resolve_output_target(
            raw_questions=raw_questions,
            raw_answers=raw_answers,
            state=state,
            sid=sid,
        )

    runner = AgentRunner(
        ui,
        state.settings,
        on_output_detection=on_output_detection,
        resume_sdk_session_id=runtime.meta.sdk_session_id,
        resolve_target=resolve_target,
    )
    await runner.__aenter__()
    runtime.ui = ui
    runtime.runner = runner


async def _handle_output_detection(
    det: OutputDetection,
    *,
    runtime: SessionRuntime,
    state: AppState,
    ui: WebAgentUI,
) -> None:
    """Spec §8.2 — register the detected file and emit `output.created`.

    - `standalone`: adopt `outputs/<output_id>/<filename>`; sidecar `meta.json`
      and registry row land.
    - `kb_version` / `attachment_version`: rotate the previous `v_curr` to
      `v_prev` (deleting any older `v_prev`), write a sidecar `v_curr.meta.json`,
      then emit. Only `v_curr` is the freshly-written file; the model never
      writes directly to `v_prev` — the observer rejects mismatched slots
      (`v_prev` won't reach this branch in practice, but we still emit a
      best-effort SSE so downstream listeners see it).
    """
    sid = runtime.meta.id
    if det.kind == "standalone" and det.output_id and det.filename:
        # Filename may be a nested path under <output_id>/ — registry stores
        # only the final segment as the on-disk name.
        filename = det.filename.rsplit("/", 1)[-1]
        mime, _ = mimetypes.guess_type(filename)
        meta = await state.outputs.adopt_at(
            output_id=det.output_id,
            title=filename,
            filename=filename,
            mime=mime or "application/octet-stream",
            source_session_id=sid,
        )
        if meta is None:
            return
        ui.on_output(
            {
                "output_id": meta.id,
                "kind": "standalone",
                "title": meta.title,
                "download_url": f"/outputs/{meta.id}",
            }
        )
        return

    if det.kind == "kb_version" and det.kb_id and det.version:
        versions_dir = state.settings.kb_dir / det.kb_id / "versions"
        _write_version_sidecar(
            versions_dir=versions_dir,
            version=det.version,
            kind="kb_version",
            file_path=det.file_path,
            source_session_id=sid,
        )
        ui.on_output(
            {
                "kind": "kb_version",
                "kb_id": det.kb_id,
                "version": det.version,
                "title": f"{det.kb_id} {det.version}",
                "download_url": (
                    f"/kb/files/{det.kb_id}/versions/{det.version}/download"
                ),
            }
        )
        return

    if (
        det.kind == "attachment_version"
        and det.session_id
        and det.attachment_id
        and det.version
    ):
        versions_dir = (
            state.settings.attachments_dir
            / det.session_id
            / det.attachment_id
            / "versions"
        )
        _write_version_sidecar(
            versions_dir=versions_dir,
            version=det.version,
            kind="attachment_version",
            file_path=det.file_path,
            source_session_id=sid,
        )
        ui.on_output(
            {
                "kind": "attachment_version",
                "session_id": det.session_id,
                "attachment_id": det.attachment_id,
                "version": det.version,
                "title": f"{det.attachment_id} {det.version}",
                "download_url": (
                    f"/sessions/{det.session_id}/attachments/{det.attachment_id}"
                    f"/versions/{det.version}/download"
                ),
            }
        )


@router.post("/{sid}/messages")
async def post_message(
    sid: str, body: MessageRequest, state: AppState = Depends(get_state)
) -> StreamingResponse:
    runtime = await state.get_or_create_runtime(sid)
    if runtime is None:
        raise HTTPException(status_code=404, detail="session not found")

    # Spec §8.5 — validate kb_scope/attachments and prepend the <scope> block to
    # the user prompt before the SDK is started. Validation HTTPException(400)
    # bubbles up; AgentRunner sees only the composed string.
    block = await build_scope(state=state, sid=sid, body=body)
    composed_prompt = render_scope(block, body.prompt)

    return StreamingResponse(
        _stream_turn(runtime=runtime, prompt=composed_prompt, state=state),
        media_type="text/event-stream",
    )


async def _stream_turn(
    *,
    runtime: SessionRuntime,
    prompt: str,
    state: AppState,
):
    await runtime.lock.acquire()
    try:
        await _ensure_runner(runtime, state)
        await state.registry.touch(runtime.meta.id)

        stream = TurnStream()
        runtime.ui.attach(stream)

        async def runner_task_fn() -> None:
            try:
                await runtime.runner.send(prompt, echo_prompt=True)
            except asyncio.CancelledError:
                stream.emit({"type": "error", "message": "turn cancelled"})
                raise
            except Exception as exc:  # noqa: BLE001 - surface any SDK error to the client
                stream.emit(
                    {"type": "error", "message": f"{type(exc).__name__}: {exc}"}
                )
            finally:
                await stream.close()

        task = asyncio.create_task(runner_task_fn())

        try:
            async for item in stream:
                yield format_event(item)
        except asyncio.CancelledError:
            task.cancel()
            raise
        finally:
            if not task.done():
                task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
            runtime.ui.detach()
    finally:
        runtime.lock.release()
