"""Session CRUD: list / create / get / rename / delete / fork."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, status

from ..schemas import (
    CreateSessionRequest,
    ForkSessionRequest,
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
    await state.discard_runtime(sid)


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
