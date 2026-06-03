"""Session CRUD: list / create / get / rename / delete / fork."""

from __future__ import annotations

import asyncio

from claude_agent_sdk import get_session_messages
from fastapi import APIRouter, Depends, HTTPException, Request, status

from ..replay import replay_to_events
from ..schemas import (
    CreateSessionRequest,
    ForkSessionRequest,
    MessageHistoryResponse,
    RenameSessionRequest,
    SessionListResponse,
    SessionResponse,
)
from ..state import AppState

router = APIRouter(prefix="/sessions", tags=["sessions"])


def get_state(request: Request) -> AppState:
    return request.app.state.app_state


@router.get("", response_model=SessionListResponse)
async def list_sessions(state: AppState = Depends(get_state)) -> SessionListResponse:
    metas = await state.registry.list()
    return SessionListResponse(sessions=[SessionResponse(**m.to_dict()) for m in metas])


@router.post("", response_model=SessionResponse, status_code=status.HTTP_201_CREATED)
async def create_session(
    body: CreateSessionRequest, state: AppState = Depends(get_state)
) -> SessionResponse:
    meta = await state.registry.create(name=body.name)
    return SessionResponse(**meta.to_dict())


@router.get("/{sid}", response_model=SessionResponse)
async def get_session(
    sid: str, state: AppState = Depends(get_state)
) -> SessionResponse:
    meta = await state.registry.get(sid)
    if meta is None:
        raise HTTPException(status_code=404, detail="session not found")
    return SessionResponse(**meta.to_dict())


@router.patch("/{sid}", response_model=SessionResponse)
async def rename_session(
    sid: str,
    body: RenameSessionRequest,
    state: AppState = Depends(get_state),
) -> SessionResponse:
    meta = await state.registry.rename(sid, body.name)
    if meta is None:
        raise HTTPException(status_code=404, detail="session not found")
    return SessionResponse(**meta.to_dict())


@router.delete("/{sid}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_session(sid: str, state: AppState = Depends(get_state)) -> None:
    if not await state.registry.delete(sid):
        raise HTTPException(status_code=404, detail="session not found")
    # Wipe the session's outputs subtree and registry rows BEFORE discarding the
    # runtime (which already cleans up attachments). Best-effort: errors inside
    # `delete_session_outputs` don't block runtime cleanup.
    await state.outputs.delete_session_outputs(sid)
    await state.discard_runtime(sid)
    # Wipe the per-session symlink farm under `<sessions-data>/<sid>/`.
    # Best-effort; rmtree only follows symlinks to drop the link entries
    # themselves (canonical kb_dir / outputs_dir subtrees are not affected).
    import shutil

    shutil.rmtree(state.settings.session_data_dir(sid), ignore_errors=True)


@router.post(
    "/{sid}/fork",
    response_model=SessionResponse,
    status_code=status.HTTP_201_CREATED,
)
async def fork_session(
    sid: str,
    body: ForkSessionRequest,
    state: AppState = Depends(get_state),
) -> SessionResponse:
    parent = await state.registry.get(sid)
    if parent is None:
        raise HTTPException(status_code=404, detail="session not found")
    name = body.name or f"{parent.name} (fork)"
    meta = await state.registry.create(name=name, parent_id=parent.id)
    return SessionResponse(**meta.to_dict())


@router.get("/{sid}/messages", response_model=MessageHistoryResponse)
async def get_session_history(
    sid: str, state: AppState = Depends(get_state)
) -> MessageHistoryResponse:
    """Return the session's prior turns as SSE-shaped event dicts.

    Returns empty `events` for fresh sessions (no SDK runner has connected yet).
    Offloads the sync JSONL read to a thread.
    """
    meta = await state.registry.get(sid)
    if meta is None:
        raise HTTPException(status_code=404, detail="session not found")
    if not meta.sdk_session_id:
        return MessageHistoryResponse(events=[])
    # `directory=None` lets the SDK scan every project dir under
    # `CLAUDE_CONFIG_DIR/projects/`. Passing project_root would silently miss
    # the JSONL on path-normalization mismatches (NFC, symlinks, worktrees).
    # The single-user data root is small enough that the extra scan is free.
    msgs = await asyncio.to_thread(get_session_messages, meta.sdk_session_id)
    return MessageHistoryResponse(events=replay_to_events(msgs, sid))
