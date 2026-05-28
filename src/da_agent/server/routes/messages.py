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
import time

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse

from ...agent.core import AgentRunner
from ...outputs import OutputDetection
from ..schemas import MessageRequest
from ..scope import build_scope, render_scope
from ..sse import format_event
from ..state import AppState, SessionRuntime, TurnStream
from ..web_ui import WebAgentUI

router = APIRouter(prefix="/sessions", tags=["messages"])


def get_state(request: Request) -> AppState:
    return request.app.state.app_state


async def _ensure_runner(runtime: SessionRuntime, state: AppState) -> None:
    if runtime.runner is not None:
        return
    ui = WebAgentUI(session_id=runtime.meta.id, app_state=state)

    def on_output_detection(det: OutputDetection) -> None:
        # Bridge sync observer -> async registry + UI emission. Fire-and-forget;
        # not tracked alongside KB ingestion tasks because lifecycle differs
        # (per-turn — best-effort, OK to be cancelled on shutdown).
        asyncio.create_task(
            _handle_output_detection(det, runtime=runtime, state=state, ui=ui)
        )

    runner = AgentRunner(
        ui, state.settings, on_output_detection=on_output_detection
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

    Standalone path: adopt the existing `outputs/<output_id>/<filename>` so the
    sidecar `meta.json` and the registry row land. KB-version path: write the
    sidecar `kb/<kb_id>/versions/v<N>.meta.json` best-effort and emit. The
    observer is conservative; we trust its classification here.
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
        # parent_version / operation / sheet_affected aren't known at observe
        # time; the runner that wrote the file already knows them but the
        # backend can only infer parent_version from the version number.
        version_n = int(det.version[1:])
        sidecar_path = (
            state.settings.kb_dir
            / det.kb_id
            / "versions"
            / f"{det.version}.meta.json"
        )
        try:
            size = (
                det.file_path.stat().st_size if det.file_path.exists() else 0
            )
            sidecar_path.parent.mkdir(parents=True, exist_ok=True)
            sidecar_path.write_text(
                json.dumps(
                    {
                        "version": det.version,
                        "parent_version": "raw"
                        if version_n == 1
                        else f"v{version_n - 1}",
                        "kind": "kb_version",
                        "operation": None,
                        "sheet_affected": None,
                        "source_session_id": sid,
                        "created_at": time.time(),
                        "size_bytes": size,
                    },
                    indent=2,
                ),
                encoding="utf-8",
            )
        except OSError:
            # Sidecar is best-effort — the version file itself is the
            # source of truth (spec §7).
            pass
        ui.on_output(
            {
                "kind": "kb_version",
                "kb_id": det.kb_id,
                "version": det.version,
                "title": f"{det.kb_id} {det.version}",
                "download_url": f"/kb/files/{det.kb_id}/versions/{det.version}/download",
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
