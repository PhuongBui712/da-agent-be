"""KB CRUD: upload, list, get meta, get memory, reprofile, delete.

Upload is multipart -- the request streams the file to disk in an executor
thread, then schedules the new memory-driven ingestion pipeline (kb_profiler
subagent) as a fire-and-forget asyncio task and returns 202 immediately.
Status transitions are surfaced on subsequent GETs.
"""

from __future__ import annotations

import asyncio
import json
import re
import shutil
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, UploadFile, status
from fastapi.responses import FileResponse, JSONResponse

from ...ingestion import run_pipeline
from ..schemas import (
    KbFileListResponse,
    KbFileResponse,
    KbMemoryResponse,
    KbVersionListResponse,
    KbVersionResponse,
)
from ..state import AppState

router = APIRouter(prefix="/kb", tags=["kb"])

# Defensive limits. KB uploads are persistent and can be larger than
# attachments, but rejecting absurd sizes early protects the executor pool.
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
        memory_path=getattr(meta, "memory_path", None),
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

    # Fire-and-forget memory-driven pipeline. Tracked so shutdown can cancel
    # cleanly. The runner serialises opus calls via a global semaphore so the
    # actual work may queue here, but the HTTP response returns immediately.
    task = asyncio.create_task(
        run_pipeline(
            registry=state.kb,
            settings=state.settings,
            kb_root=kb_root,
            kb_id=meta.id,
        )
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
    """Returns 410 Gone with a pointer to /memory; manifest pipeline replaced by memory-driven ingestion.

    The 404 path (kb not found) takes precedence so callers cannot use the
    410 to probe for kb_id existence.
    """
    meta = await state.kb.get(kb_id)
    if meta is None:
        raise HTTPException(status_code=404, detail="kb file not found")
    raise HTTPException(
        status_code=status.HTTP_410_GONE,
        detail={
            "error": "manifest endpoint deprecated; use GET /kb/files/{id}/memory",
            "memory_endpoint": f"/kb/files/{kb_id}/memory",
        },
    )


@router.get("/files/{kb_id}/memory", response_model=KbMemoryResponse)
async def get_kb_memory(
    kb_id: str, state: AppState = Depends(get_state)
) -> KbMemoryResponse:
    """Return the per-KB memory note written by the kb_profiler subagent.

    409 if the KB is still PENDING/PROFILING.
    404 if the KB is READY_PARTIAL/FAILED with no memory file on disk.
    """
    meta = await state.kb.get(kb_id)
    if meta is None:
        raise HTTPException(status_code=404, detail="kb file not found")
    if meta.status in {"PENDING", "PROFILING"}:
        raise HTTPException(
            status_code=409,
            detail=f"memory unavailable; status={meta.status}",
        )
    candidate: Path | None = (
        Path(meta.memory_path) if getattr(meta, "memory_path", None) else None
    )
    if candidate is None:
        candidate = state.settings.kb_profiler_memory_dir / f"{kb_id}.md"
    if not candidate.exists():
        raise HTTPException(
            status_code=404,
            detail=(
                f"memory file missing on disk for kb {kb_id} "
                f"(status={meta.status}); use POST /reprofile to rebuild"
            ),
        )
    content = await asyncio.to_thread(candidate.read_text, "utf-8")
    return KbMemoryResponse(
        kb_id=kb_id,
        path=str(candidate),
        content=content,
        size_bytes=len(content.encode("utf-8")),
    )


@router.post(
    "/files/{kb_id}/reprofile",
    response_model=KbFileResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def reprofile_kb_file(
    kb_id: str, state: AppState = Depends(get_state)
) -> KbFileResponse:
    """Manually re-run the ingestion pipeline against an existing KB.

    Used to backfill memory for legacy KBs, recover from READY_PARTIAL, or
    regenerate when the user wants a fresh note.

    409 if a profile is already in flight (status PROFILING).
    """
    meta = await state.kb.get(kb_id)
    if meta is None:
        raise HTTPException(status_code=404, detail="kb file not found")
    if meta.status == "PROFILING":
        raise HTTPException(
            status_code=409,
            detail=f"kb {kb_id} is already PROFILING; wait for it to settle",
        )
    raw_path = state.settings.kb_dir / kb_id / "raw.xlsx"
    if not raw_path.exists():
        raise HTTPException(
            status_code=409,
            detail=f"kb {kb_id} has no raw.xlsx on disk; cannot reprofile",
        )
    # Eagerly transition to PENDING so polling clients see the in-flight state
    # immediately. The runner will flip it to PROFILING → READY/READY_PARTIAL.
    await state.kb.clear_memory_path(kb_id)
    await state.kb.update_status(kb_id, "PENDING")
    # Delete the existing memory file BEFORE scheduling. The profiler
    # subagent's stopping condition is "the file exists"; leaving the old file
    # in place risks the LLM short-circuiting without rewriting on reprofile.
    # Removing it forces a real new write (or, on failure, a clean
    # READY_PARTIAL with no stale content).
    existing_memory = state.settings.kb_profiler_memory_dir / f"{kb_id}.md"
    if existing_memory.exists():
        await asyncio.to_thread(existing_memory.unlink, missing_ok=True)
    task = asyncio.create_task(
        run_pipeline(
            registry=state.kb,
            settings=state.settings,
            kb_root=state.settings.kb_dir,
            kb_id=kb_id,
        )
    )
    state.track_kb_task(task)
    refreshed = await state.kb.get(kb_id)
    return _meta_to_response(refreshed if refreshed is not None else meta)


@router.delete("/files/{kb_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_kb_file(kb_id: str, state: AppState = Depends(get_state)) -> None:
    meta = await state.kb.get(kb_id)
    if meta is None:
        raise HTTPException(status_code=404, detail="kb file not found")
    await state.kb.delete(kb_id)
    kb_dir = state.settings.kb_dir / kb_id
    if kb_dir.exists():
        await asyncio.to_thread(shutil.rmtree, str(kb_dir), True)
    # Clean the per-KB memory note if the profiler ever wrote one.
    # MEMORY.md is intentionally left alone — the kb_profiler subagent
    # curates its own index and a stale entry is harmless.
    memory_file = state.settings.kb_profiler_memory_dir / f"{kb_id}.md"
    if memory_file.exists():
        await asyncio.to_thread(memory_file.unlink, missing_ok=True)


# --------------------------------------------------------------------------- #
# KB version endpoints — READ-ONLY.
# Physical 2-version cap: only `v_curr` (latest) and optionally `v_prev`
# (one-step rollback) live on disk. Older revisions are deleted on rotation.
# The observer + permission resolver mint these files; we only scan and serve here.
# --------------------------------------------------------------------------- #
_VERSION_FILE_RE = re.compile(r"^v_(curr|prev)\.(xlsx|xlsm|xls|csv|tsv)$")
_VERSION_SLOT_RE = re.compile(r"^v_(curr|prev)$")
_XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def _scan_versions(versions_dir: Path) -> list[KbVersionResponse]:
    """Scan kb/<id>/versions/ for v_curr.<ext> / v_prev.<ext> files.

    For each, read companion <slot>.meta.json sidecar if present.
    Slots are ordered: v_curr first, v_prev second.
    """
    out: list[KbVersionResponse] = []
    if not versions_dir.exists():
        return out
    by_slot: dict[str, Path] = {}
    for entry in sorted(versions_dir.iterdir()):
        m = _VERSION_FILE_RE.match(entry.name)
        if m is None or not entry.is_file():
            continue
        by_slot[f"v_{m.group(1)}"] = entry
    for slot in ("v_curr", "v_prev"):
        entry = by_slot.get(slot)
        if entry is None:
            continue
        sidecar = versions_dir / f"{slot}.meta.json"
        meta: dict[str, Any] = {}
        if sidecar.exists():
            try:
                meta = json.loads(sidecar.read_text("utf-8"))
            except (json.JSONDecodeError, OSError):
                meta = {}
        parent_default = "v_prev" if slot == "v_curr" else "raw"
        out.append(
            KbVersionResponse(
                version=slot,
                parent_version=meta.get("parent_version", parent_default),
                operation=meta.get("operation"),
                sheet_affected=meta.get("sheet_affected"),
                source_session_id=meta.get("source_session_id"),
                created_at=float(meta.get("created_at", entry.stat().st_mtime)),
                size_bytes=int(meta.get("size_bytes", entry.stat().st_size)),
            )
        )
    return out


def _find_version_file(versions_dir: Path, slot: str) -> Path | None:
    """Locate `versions_dir/<slot>.<ext>` for any allowed extension."""
    if not versions_dir.exists():
        return None
    for entry in versions_dir.iterdir():
        m = _VERSION_FILE_RE.match(entry.name)
        if m is None:
            continue
        if f"v_{m.group(1)}" == slot:
            return entry
    return None


@router.get("/files/{kb_id}/versions", response_model=KbVersionListResponse)
async def list_kb_versions(
    kb_id: str, state: AppState = Depends(get_state)
) -> KbVersionListResponse:
    meta = await state.kb.get(kb_id)
    if meta is None:
        raise HTTPException(status_code=404, detail="kb file not found")
    versions_dir = state.settings.kb_dir / kb_id / "versions"
    versions = await asyncio.to_thread(_scan_versions, versions_dir)
    return KbVersionListResponse(versions=versions)


@router.get("/files/{kb_id}/versions/{version}/download")
async def download_kb_version(
    kb_id: str, version: str, state: AppState = Depends(get_state)
) -> FileResponse:
    meta = await state.kb.get(kb_id)
    if meta is None:
        raise HTTPException(status_code=404, detail="kb file not found")
    if not _VERSION_SLOT_RE.match(version):
        raise HTTPException(
            status_code=400,
            detail="invalid version slot; expected v_curr or v_prev",
        )
    versions_dir = state.settings.kb_dir / kb_id / "versions"
    file_path = _find_version_file(versions_dir, version)
    if file_path is None:
        raise HTTPException(status_code=404, detail="version not found on disk")
    return FileResponse(
        path=file_path,
        media_type=_XLSX_MIME,
        filename=f"{kb_id}_{version}{file_path.suffix}",
    )


# --- Google Sheets import stub ---------------------------------------- #
@router.post("/files/import-sheet", status_code=status.HTTP_501_NOT_IMPLEMENTED)
async def import_sheet_stub() -> JSONResponse:
    """Google Sheets import — OAuth flow not yet defined.

    Returning 501 lets the FE see a deliberate `not implemented` rather
    than a 404 for an endpoint that doesn't exist at all.
    """
    return JSONResponse(
        status_code=status.HTTP_501_NOT_IMPLEMENTED,
        content={
            "error": "Google Sheets import not implemented",
            "spec_reference": "technical-spec.md §14 open question (OAuth)",
        },
    )
