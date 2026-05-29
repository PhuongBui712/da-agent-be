"""Outputs HTTP endpoints (spec §11).

Read-only browse + download + delete of standalone outputs. KB-version files
are served via `/kb/files/{kb_id}/versions/{version}/download` (spec §7) —
this router does not attempt to surface them.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import FileResponse

from ..schemas import OutputListResponse, OutputResponse
from ..state import AppState

router = APIRouter(prefix="/outputs", tags=["outputs"])


def get_state(request: Request) -> AppState:
    return request.app.state.app_state


def _meta_to_response(meta) -> OutputResponse:
    return OutputResponse(
        output_id=meta.id,
        kind=meta.kind,
        title=meta.title,
        filename=meta.filename,
        mime=meta.mime,
        size_bytes=meta.size_bytes,
        source_session_id=meta.source_session_id,
        source_kb_ids=list(meta.source_kb_ids),
        created_at=meta.created_at,
    )


@router.get("", response_model=OutputListResponse)
async def list_outputs(
    session_id: str | None = None, state: AppState = Depends(get_state)
) -> OutputListResponse:
    metas = await state.outputs.list(session_id=session_id)
    return OutputListResponse(outputs=[_meta_to_response(m) for m in metas])


@router.get("/{output_id}/meta", response_model=OutputResponse)
async def get_output_meta(
    output_id: str, state: AppState = Depends(get_state)
) -> OutputResponse:
    meta = await state.outputs.get(output_id)
    if meta is None:
        raise HTTPException(status_code=404, detail="output not found")
    return _meta_to_response(meta)


@router.get("/{output_id}")
async def download_output(
    output_id: str, state: AppState = Depends(get_state)
) -> FileResponse:
    meta = await state.outputs.get(output_id)
    if meta is None:
        raise HTTPException(status_code=404, detail="output not found")
    path = state.outputs.path_for(meta)
    if not path.exists():
        raise HTTPException(status_code=404, detail="output file missing on disk")
    return FileResponse(path=path, media_type=meta.mime, filename=meta.filename)


@router.delete("/{output_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_output(output_id: str, state: AppState = Depends(get_state)) -> None:
    if not await state.outputs.delete(output_id):
        raise HTTPException(status_code=404, detail="output not found")
