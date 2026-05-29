"""Attachment CRUD: upload, list, delete per session.

Short-term attachments are scoped to a session and deleted with it (spec §5.3).
Files are streamed to a tmp path then renamed so partial uploads never appear
at the final path.

Spec §8.2 — attachments support symmetric 2-slot versioning identical to KB:
on disk under `attachments/<sid>/<att_id>/versions/v_curr.<ext>` and
`v_prev.<ext>`. The download endpoint at the bottom serves both slots.
"""

from __future__ import annotations

import asyncio
import re
import shutil
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request, UploadFile, status
from fastapi.responses import FileResponse

from ..schemas import AttachmentListResponse, AttachmentResponse
from ..state import AppState

router = APIRouter(prefix="/sessions", tags=["attachments"])

_VERSION_FILE_RE = re.compile(r"^v_(curr|prev)\.(xlsx|xlsm|xls|csv|tsv)$")
_VERSION_SLOT_RE = re.compile(r"^v_(curr|prev)$")

# Copy of the pattern from routes/kb.py — intentionally NOT imported from there
# to keep the two modules independent (spec §11).
_FILENAME_CLEAN = re.compile(r"[^A-Za-z0-9._-]+")


def get_state(request: Request) -> AppState:
    return request.app.state.app_state


def _sanitize_filename(raw: str | None) -> str:
    """Strip path components and collapse non-safe chars."""
    name = (raw or "upload.bin").rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
    cleaned = _FILENAME_CLEAN.sub("_", name).strip("._-") or "upload.bin"
    return cleaned[:200]


def _meta_to_response(meta) -> AttachmentResponse:
    return AttachmentResponse(
        attachment_id=meta.id,
        filename=meta.filename,
        size_bytes=meta.size_bytes,
        mime=meta.mime,
        uploaded_at=meta.uploaded_at,
    )


# --------------------------------------------------------------------------- #
# Endpoints (spec §5.3)
# --------------------------------------------------------------------------- #


@router.post(
    "/{sid}/attachments",
    response_model=AttachmentResponse,
    status_code=status.HTTP_201_CREATED,
)
async def upload_attachment(
    sid: str, file: UploadFile, state: AppState = Depends(get_state)
) -> AttachmentResponse:
    # 404 if the session doesn't exist in the registry (spec §5.3).
    if await state.registry.get(sid) is None:
        raise HTTPException(status_code=404, detail="session not found")

    filename = _sanitize_filename(file.filename)
    mime = file.content_type or "application/octet-stream"

    att_root = state.settings.attachments_dir
    tmp_dir = att_root / sid / "_tmp"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    # Unique tmp name to avoid collisions when parallel uploads arrive.
    tmp_path = tmp_dir / f"upload_{id(file):x}.bin"

    max_bytes = state.settings.attachment_max_bytes
    total = 0
    try:
        with tmp_path.open("wb") as out:
            while chunk := await file.read(1024 * 1024):
                total += len(chunk)
                # max_bytes <= 0 means unlimited (spec §5.3 defensive note).
                if max_bytes > 0 and total > max_bytes:
                    raise HTTPException(status_code=413, detail="file too large")
                out.write(chunk)
    except BaseException:
        tmp_path.unlink(missing_ok=True)
        raise

    if total == 0:
        tmp_path.unlink(missing_ok=True)
        raise HTTPException(status_code=400, detail="file is empty")

    # Register first to obtain an att_id, then move bytes into the final path.
    # On move failure, roll back the registry row so the user never sees a ghost
    # entry (mirrors the kb.py pattern).
    meta = await state.attachments.create(
        sid, filename=filename, size_bytes=total, mime=mime
    )
    dest = state.attachments.path_for(meta)
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        await asyncio.to_thread(shutil.move, str(tmp_path), str(dest))
    except BaseException:
        await state.attachments.delete(sid, meta.id)
        await asyncio.to_thread(shutil.rmtree, str(dest.parent), True)
        tmp_path.unlink(missing_ok=True)
        raise

    return _meta_to_response(meta)


@router.get("/{sid}/attachments", response_model=AttachmentListResponse)
async def list_attachments(
    sid: str, state: AppState = Depends(get_state)
) -> AttachmentListResponse:
    if await state.registry.get(sid) is None:
        raise HTTPException(status_code=404, detail="session not found")
    metas = await state.attachments.list(sid)
    return AttachmentListResponse(attachments=[_meta_to_response(m) for m in metas])


@router.delete("/{sid}/attachments/{att_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_attachment(
    sid: str, att_id: str, state: AppState = Depends(get_state)
) -> None:
    if await state.registry.get(sid) is None:
        raise HTTPException(status_code=404, detail="session not found")
    meta = await state.attachments.get(sid, att_id)
    if meta is None:
        raise HTTPException(status_code=404, detail="attachment not found")
    await state.attachments.delete(sid, att_id)
    att_dir = state.attachments.path_for(meta).parent
    if att_dir.exists():
        await asyncio.to_thread(shutil.rmtree, str(att_dir), True)


def _find_attachment_version(versions_dir: Path, slot: str) -> Path | None:
    if not versions_dir.exists():
        return None
    for entry in versions_dir.iterdir():
        m = _VERSION_FILE_RE.match(entry.name)
        if m is None:
            continue
        if f"v_{m.group(1)}" == slot:
            return entry
    return None


@router.get("/{sid}/attachments/{att_id}/versions/{version}/download")
async def download_attachment_version(
    sid: str,
    att_id: str,
    version: str,
    state: AppState = Depends(get_state),
) -> FileResponse:
    if await state.registry.get(sid) is None:
        raise HTTPException(status_code=404, detail="session not found")
    meta = await state.attachments.get(sid, att_id)
    if meta is None:
        raise HTTPException(status_code=404, detail="attachment not found")
    if not _VERSION_SLOT_RE.match(version):
        raise HTTPException(
            status_code=400,
            detail="invalid version slot; expected v_curr or v_prev",
        )
    versions_dir = state.attachments.path_for(meta).parent / "versions"
    file_path = _find_attachment_version(versions_dir, version)
    if file_path is None:
        raise HTTPException(status_code=404, detail="version not found on disk")
    return FileResponse(
        path=file_path,
        media_type=meta.mime,
        filename=f"{att_id}_{version}{file_path.suffix}",
    )
