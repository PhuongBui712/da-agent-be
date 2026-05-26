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

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse

from ...agent.core import AgentRunner
from ..schemas import MessageRequest
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
    runner = AgentRunner(ui, state.settings)
    await runner.__aenter__()
    runtime.ui = ui
    runtime.runner = runner


@router.post("/{sid}/messages")
async def post_message(
    sid: str, body: MessageRequest, state: AppState = Depends(get_state)
) -> StreamingResponse:
    runtime = await state.get_or_create_runtime(sid)
    if runtime is None:
        raise HTTPException(status_code=404, detail="session not found")

    return StreamingResponse(
        _stream_turn(runtime=runtime, prompt=body.prompt, state=state),
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
