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
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse

from ...agent.core import AgentRunner
from ...agent.permissions import TargetResolution, TargetValidationError
from ...outputs import OutputDetection
from ..schemas import MessageRequest
from ..scope import ScopeBlock, build_scope, render_scope
from ..session_farm import prepare_session_root, rebuild_kb_symlinks
from ..sse import format_event
from ..state import AppState, SessionRuntime, TurnStream
from ..web_ui import WebAgentUI

router = APIRouter(prefix="/sessions", tags=["messages"])


def get_state(request: Request) -> AppState:
    return request.app.state.app_state


def _unique_filename(parent: Path, base: str, ext: str) -> str:
    """Return a filename like 'base.ext' or 'base_v2.ext' that doesn't exist in `parent` yet."""
    candidate = f"{base}{ext}"
    if not (parent / candidate).exists():
        return candidate
    n = 2
    while True:
        candidate = f"{base}_v{n}{ext}"
        if not (parent / candidate).exists():
            return candidate
        n += 1


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
    """Validate (Target, Source) answers and resolve the absolute write path.

    All targets land flat under `outputs/<session_id>/<filename>`. Filename
    collisions in the same session get a `_vN` suffix from `_unique_filename`.

    `resolved_target_path` is the canonical `<data_root>/outputs/<sid>/<filename>`;
    using the canonical root directly avoids EROFS/EXDEV errors that occur when
    the sandbox follows a symlink alias across device boundaries.

    Raises `TargetValidationError` on any validation failure (the
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

    canonical_root = state.settings.outputs_session_dir(sid)
    canonical_root.mkdir(parents=True, exist_ok=True)

    _STANDALONE_EXT = {
        "New .xlsx": ".xlsx",
        "New .pptx": ".pptx",
        "New .docx": ".docx",
    }
    if target in _STANDALONE_EXT:
        ext = _STANDALONE_EXT[target]
        filename = _unique_filename(canonical_root, "output", ext)
        return TargetResolution(
            resolved_target_path=str(canonical_root / filename),
            resolved_target_kind="standalone",
        )

    if target in {"New sheet", "Pick sheet"}:
        # Source must point at a KB or attachment; we still validate the
        # reference even though the write lands under outputs/<sid>/.
        if not source or source == "N/A":
            raise TargetValidationError(f"Source is required when Target = '{target}'")
        head = source.split("::", 1)[0]

        if head.startswith("kb_"):
            allowed = set(_intersection_kb_options(kb_metas, None))
            if head not in allowed:
                raise TargetValidationError(f"unknown or non-READY kb_id: {head}")
            meta = next(m for m in kb_metas if m.id == head)
            base = Path(meta.filename).stem or "output"
            ext = Path(meta.filename).suffix or ".xlsx"
            filename = _unique_filename(canonical_root, base, ext)
            return TargetResolution(
                resolved_target_path=str(canonical_root / filename),
                resolved_target_kind="kb_version",
            )
        if head.startswith("att_"):
            att = next((a for a in att_metas if a.id == head), None)
            if att is None:
                raise TargetValidationError(f"unknown attachment_id: {head}")
            base = Path(att.filename).stem or "output"
            ext = Path(att.filename).suffix or ".xlsx"
            filename = _unique_filename(canonical_root, base, ext)
            return TargetResolution(
                resolved_target_path=str(canonical_root / filename),
                resolved_target_kind="attachment_version",
            )
        raise TargetValidationError(
            f"Source must start with 'kb_' or 'att_'; got '{source}'"
        )

    raise TargetValidationError(
        f"Target must be one of 'New .xlsx', 'New .pptx', 'New .docx', 'New sheet', 'Pick sheet'; got '{target}'"
    )


async def _ensure_runner(runtime: SessionRuntime, state: AppState) -> None:
    if runtime.runner is not None:
        return
    sid = runtime.meta.id

    def on_sdk_session_id(sdk_uuid: str) -> None:
        # Fire-and-forget capture from `SystemMessage(subtype="init")`.
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
        # not tracked alongside KB ingestion tasks because it is per-turn and
        # best-effort (OK to be cancelled on shutdown).
        asyncio.create_task(
            _handle_output_detection(det, runtime=runtime, state=state, ui=ui)
        )

    async def resolve_target(
        raw_questions: list[dict[str, Any]],
        raw_answers: list[dict[str, Any]],
    ) -> TargetResolution:
        # Invoked from `permissions.py` after the FE answer arrives.
        # Routes all targets flat under `outputs/<sid>/<filename>`.
        return await _resolve_output_target(
            raw_questions=raw_questions,
            raw_answers=raw_answers,
            state=state,
            sid=sid,
        )

    runner = AgentRunner(
        ui,
        state.settings,
        session_id=sid,
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
    """Register a detected output file and emit `output.created`.

    Only `standalone` detections are handled; the registry mints `output_id`
    on adoption and writes the sidecar.
    """
    if det.kind != "standalone":
        return
    sid = runtime.meta.id
    meta = await state.outputs.register_standalone(
        session_id=sid,
        file_path=det.file_path,
        filename=det.filename,
        kind="standalone",
        source_id=None,
    )
    ui.on_output(
        {
            "output_id": meta.id,
            "kind": meta.kind,
            "title": meta.filename,
            "filename": meta.filename,
            "size_bytes": meta.size_bytes,
            "mime": meta.mime,
            "download_url": f"/outputs/{meta.id}",
            "created_at": meta.created_at,
        }
    )


@router.post("/{sid}/messages")
async def post_message(
    sid: str, body: MessageRequest, state: AppState = Depends(get_state)
) -> StreamingResponse:
    runtime = await state.get_or_create_runtime(sid)
    if runtime is None:
        raise HTTPException(status_code=404, detail="session not found")

    # Bootstrap the per-session symlink farm before validating scope. Idempotent.
    prepare_session_root(state.settings, sid)

    # Validate kb_scope/attachments and prepend the <scope> block to the user
    # prompt. Validation HTTPException(400) bubbles up; AgentRunner sees only
    # the composed string.
    block = await build_scope(state=state, sid=sid, body=body)
    composed_prompt = render_scope(block, body.prompt)

    return StreamingResponse(
        _stream_turn(
            runtime=runtime, prompt=composed_prompt, scope_block=block, state=state
        ),
        media_type="text/event-stream",
    )


async def _stream_turn(
    *,
    runtime: SessionRuntime,
    prompt: str,
    scope_block: ScopeBlock,
    state: AppState,
):
    await runtime.lock.acquire()
    try:
        await _ensure_runner(runtime, state)
        await state.registry.touch(runtime.meta.id)

        # Rebuild symlink farm for this turn's kb_scope. Runs under
        # `runtime.lock` (acquired above), so concurrent turns within a session
        # serialise.
        rebuild_kb_symlinks(state.settings, runtime.meta.id, scope_block)

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
