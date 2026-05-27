"""KB CRUD: upload, list, get meta, get manifest, delete.

Upload is multipart -- the request streams the file to disk in an executor
thread, then schedules the preprocessing pipeline as a fire-and-forget
asyncio task and returns 202 immediately. Status transitions are surfaced
on subsequent GETs (FE polls; SSE for KB status is open question §14).
"""

from __future__ import annotations

import asyncio
import re
import shutil
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request, UploadFile, status
from fastapi.responses import JSONResponse

from ...kb import read_manifest, run_pipeline
from ..schemas import KbFileListResponse, KbFileResponse
from ..state import AppState

router = APIRouter(prefix="/kb", tags=["kb"])

# Defensive limits. Spec mentions `attachment_max_bytes` for short-term
# attachments (§5.3); KB uploads are persistent and can be larger, but
# rejecting absurd sizes early protects the executor pool.
_MAX_UPLOAD_BYTES = 500 * 1024 * 1024  # 500 MB
_FILENAME_CLEAN = re.compile(r"[^A-Za-z0-9._-]+")
_ALLOWED_EXTS = {".xlsx", ".xlsm"}


def get_state(request: Request) -> AppState:
    return request.app.state.app_state


def _sanitize_filename(raw: str | None) -> str:
    """Strip path components and collapse anything weird. Keeps `.xlsx`."""
    name = (raw or "uploaded.xlsx").rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
    cleaned = _FILENAME_CLEAN.sub("_", name).strip("._-") or "uploaded.xlsx"
    return cleaned[:200]  # keep filenames short for filesystem sanity


def _meta_to_response(meta) -> KbFileResponse:
    return KbFileResponse(
        id=meta.id,
        filename=meta.filename,
        size_bytes=meta.size_bytes,
        status=meta.status,
        created_at=meta.created_at,
        updated_at=meta.updated_at,
        error=meta.error,
    )


# --------------------------------------------------------------------------- #
# Endpoints
# --------------------------------------------------------------------------- #
@router.post(
    "/files",
    response_model=KbFileResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def upload_kb_file(
    file: UploadFile, state: AppState = Depends(get_state)
) -> KbFileResponse:
    filename = _sanitize_filename(file.filename)
    if Path(filename).suffix.lower() not in _ALLOWED_EXTS:
        raise HTTPException(
            status_code=400, detail="only .xlsx / .xlsm files are accepted"
        )

    # Stream to a tmp path while counting bytes; reject if it exceeds the cap.
    kb_root = state.settings.kb_dir
    kb_root.mkdir(parents=True, exist_ok=True)
    tmp_dir = kb_root / "_tmp"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    tmp_path = tmp_dir / f"upload_{id(file):x}.bin"

    total = 0
    try:
        with tmp_path.open("wb") as out:
            while chunk := await file.read(1024 * 1024):
                total += len(chunk)
                if total > _MAX_UPLOAD_BYTES:
                    raise HTTPException(status_code=413, detail="file too large")
                out.write(chunk)
    except BaseException:
        # Always clean up the tmp file -- HTTP errors, disk-full OSError,
        # client-disconnect CancelledError, anything.
        tmp_path.unlink(missing_ok=True)
        raise

    if total == 0:
        tmp_path.unlink(missing_ok=True)
        raise HTTPException(status_code=400, detail="file is empty")

    # Register first so we own a kb_id, then move the bytes into place. If
    # the move fails (disk full, permissions), roll back the registry row
    # so the user does not see a permanently-FAILED orphan.
    meta = await state.kb.create(filename=filename, size_bytes=total)
    kb_dir = kb_root / meta.id
    kb_dir.mkdir(parents=True, exist_ok=True)
    raw_path = kb_dir / "raw.xlsx"
    try:
        await asyncio.to_thread(shutil.move, str(tmp_path), str(raw_path))
    except BaseException:
        await state.kb.delete(meta.id)
        await asyncio.to_thread(shutil.rmtree, str(kb_dir), True)
        tmp_path.unlink(missing_ok=True)
        raise

    # Fire-and-forget pipeline. Tracked so shutdown can cancel cleanly.
    task = asyncio.create_task(
        run_pipeline(registry=state.kb, kb_root=kb_root, kb_id=meta.id)
    )
    state.track_kb_task(task)

    return _meta_to_response(meta)


@router.get("/files", response_model=KbFileListResponse)
async def list_kb_files(
    state: AppState = Depends(get_state),
) -> KbFileListResponse:
    metas = await state.kb.list()
    return KbFileListResponse(files=[_meta_to_response(m) for m in metas])


@router.get("/files/{kb_id}", response_model=KbFileResponse)
async def get_kb_file(
    kb_id: str, state: AppState = Depends(get_state)
) -> KbFileResponse:
    meta = await state.kb.get(kb_id)
    if meta is None:
        raise HTTPException(status_code=404, detail="kb file not found")
    return _meta_to_response(meta)


@router.get("/files/{kb_id}/manifest")
async def get_kb_manifest(
    kb_id: str, state: AppState = Depends(get_state)
) -> JSONResponse:
    meta = await state.kb.get(kb_id)
    if meta is None:
        raise HTTPException(status_code=404, detail="kb file not found")
    if meta.status != "READY":
        raise HTTPException(
            status_code=409,
            detail=f"manifest unavailable; status={meta.status}",
        )
    manifest_path = state.settings.kb_dir / kb_id / "manifest.json"
    if not manifest_path.exists():
        raise HTTPException(status_code=404, detail="manifest file missing on disk")
    payload = await asyncio.to_thread(read_manifest, manifest_path)
    return JSONResponse(content=payload)


@router.delete("/files/{kb_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_kb_file(kb_id: str, state: AppState = Depends(get_state)) -> None:
    meta = await state.kb.get(kb_id)
    if meta is None:
        raise HTTPException(status_code=404, detail="kb file not found")
    await state.kb.delete(kb_id)
    kb_dir = state.settings.kb_dir / kb_id
    if kb_dir.exists():
        await asyncio.to_thread(shutil.rmtree, str(kb_dir), True)
