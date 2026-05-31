"""HTTP integration tests for /kb/* endpoints."""

from __future__ import annotations

import asyncio
import io
import time

import httpx
import openpyxl
import pytest
import pytest_asyncio

from da_agent.config import Settings
from da_agent.server.app import create_app


# --------------------------------------------------------------------------- #
# Helper: build a minimal xlsx in memory
# --------------------------------------------------------------------------- #
def _make_xlsx_bytes(rows: list[list]) -> bytes:
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Sheet1"
    for r in rows:
        ws.append(r)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


_SMALL_XLSX = _make_xlsx_bytes(
    [
        ["id", "name"],
        [1, "Alice"],
        [2, "Bob"],
    ]
)


# --------------------------------------------------------------------------- #
# Pipeline stub — prevents real opus subagent calls in unit tests
# --------------------------------------------------------------------------- #
async def _fake_pipeline(*, registry, settings, kb_root, kb_id, profiler=None):
    await registry.update_status(kb_id, "READY")


# --------------------------------------------------------------------------- #
# Fixtures (narrower than test_server.py — no SDK fake needed)
# --------------------------------------------------------------------------- #
@pytest.fixture
def settings(tmp_path, monkeypatch):
    monkeypatch.setenv("DA_AGENT_HOME", str(tmp_path))
    s = Settings()
    s.data_root = tmp_path
    s.ensure_dirs()
    return s


@pytest_asyncio.fixture
async def app(settings, monkeypatch):
    monkeypatch.setattr("da_agent.server.routes.kb.run_pipeline", _fake_pipeline)
    a = create_app(settings)
    async with a.router.lifespan_context(a):
        yield a


@pytest_asyncio.fixture
async def client(app):
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


# --------------------------------------------------------------------------- #
# Poll helper
# --------------------------------------------------------------------------- #
async def _poll_ready(
    client: httpx.AsyncClient, kb_id: str, timeout: float = 10.0
) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        r = await client.get(f"/kb/files/{kb_id}")
        assert r.status_code == 200
        body = r.json()
        if body["status"] in {"READY", "READY_PARTIAL", "FAILED"}:
            return body
        await asyncio.sleep(0.1)
    raise TimeoutError(f"{kb_id} did not reach terminal status within {timeout}s")


# --------------------------------------------------------------------------- #
# Tests
# --------------------------------------------------------------------------- #
async def test_post_kb_file_returns_202_pending(client):
    r = await client.post(
        "/kb/files",
        files={"file": ("data.xlsx", _SMALL_XLSX, "application/octet-stream")},
    )
    assert r.status_code == 202
    body = r.json()
    assert body["status"] == "PENDING"
    assert body["id"].startswith("kb_")
    assert body["filename"].endswith(".xlsx")


async def test_post_kb_pipeline_eventually_marks_ready(client):
    r = await client.post(
        "/kb/files",
        files={"file": ("data.xlsx", _SMALL_XLSX, "application/octet-stream")},
    )
    assert r.status_code == 202
    kb_id = r.json()["id"]
    final = await _poll_ready(client, kb_id)
    assert final["status"] == "READY"


async def test_post_rejects_non_xlsx(client):
    r = await client.post(
        "/kb/files",
        files={"file": ("data.csv", b"a,b\n1,2", "text/csv")},
    )
    assert r.status_code == 400


async def test_post_rejects_empty_file(client):
    r = await client.post(
        "/kb/files",
        files={"file": ("data.xlsx", b"", "application/octet-stream")},
    )
    assert r.status_code == 400


async def test_get_manifest_404_when_kb_missing(client):
    r = await client.get("/kb/files/kb_nope/manifest")
    assert r.status_code == 404


async def test_get_manifest_returns_410_gone_for_existing_kb(client, app):
    state = app.state.app_state
    meta = await state.kb.create(filename="x.xlsx", size_bytes=1)
    r = await client.get(f"/kb/files/{meta.id}/manifest")
    assert r.status_code == 410
    body = r.json()
    assert body["detail"]["memory_endpoint"].endswith("/memory")


async def test_list_kb_files_returns_uploaded(client):
    r = await client.post(
        "/kb/files",
        files={"file": ("data.xlsx", _SMALL_XLSX, "application/octet-stream")},
    )
    kb_id = r.json()["id"]
    list_r = await client.get("/kb/files")
    assert list_r.status_code == 200
    ids = [f["id"] for f in list_r.json()["files"]]
    assert kb_id in ids


async def test_delete_removes_row_and_files(client, app):
    r = await client.post(
        "/kb/files",
        files={"file": ("data.xlsx", _SMALL_XLSX, "application/octet-stream")},
    )
    kb_id = r.json()["id"]
    await _poll_ready(client, kb_id)

    del_r = await client.delete(f"/kb/files/{kb_id}")
    assert del_r.status_code == 204

    get_r = await client.get(f"/kb/files/{kb_id}")
    assert get_r.status_code == 404

    kb_dir = app.state.app_state.settings.kb_dir / kb_id
    assert not kb_dir.exists()


async def test_post_does_not_block_other_requests(client, app, monkeypatch):
    """The ingestion pipeline runs as a background task; /health must not block."""

    async def slow_pipeline(*, registry, settings, kb_root, kb_id, profiler=None):
        await asyncio.sleep(1.5)
        await registry.update_status(kb_id, "READY")

    monkeypatch.setattr("da_agent.server.routes.kb.run_pipeline", slow_pipeline)

    await client.post(
        "/kb/files",
        files={"file": ("data.xlsx", _SMALL_XLSX, "application/octet-stream")},
    )

    health_r = await asyncio.wait_for(client.get("/health"), timeout=0.5)
    assert health_r.status_code == 200


async def test_oversize_upload_does_not_leak_tmp_file(client, app, monkeypatch):
    """413 path: the chunked-write loop raises HTTPException; the cleanup
    must run for ANY exception, not only HTTPException."""
    import da_agent.server.routes.kb as kb_routes

    monkeypatch.setattr(kb_routes, "_MAX_UPLOAD_BYTES", 100)

    payload = b"x" * 4096  # exceeds the 100-byte cap
    r = await client.post(
        "/kb/files",
        files={"file": ("big.xlsx", payload, "application/octet-stream")},
    )
    assert r.status_code == 413

    tmp_dir = app.state.app_state.settings.kb_dir / "_tmp"
    leftovers = list(tmp_dir.glob("upload_*.bin")) if tmp_dir.exists() else []
    assert leftovers == [], f"expected no tmp files, got {leftovers}"


async def test_failed_move_rolls_back_registry_row(client, app, monkeypatch):
    """If `shutil.move` raises, the registry row created seconds earlier
    must be deleted so the user does not see a stuck PENDING entry."""
    import asyncio as _asyncio

    real_to_thread = _asyncio.to_thread

    async def boom_move(func, *args, **kwargs):
        # Only sabotage the shutil.move call; let other to_thread calls pass.
        if getattr(func, "__name__", "") == "move":
            raise OSError("simulated disk failure")
        return await real_to_thread(func, *args, **kwargs)

    monkeypatch.setattr("da_agent.server.routes.kb.asyncio.to_thread", boom_move)

    pre = await client.get("/kb/files")
    pre_ids = {f["id"] for f in pre.json()["files"]}

    # The route re-raises the OSError; httpx ASGITransport surfaces it.
    with pytest.raises(OSError, match="simulated disk failure"):
        await client.post(
            "/kb/files",
            files={"file": ("ok.xlsx", _SMALL_XLSX, "application/octet-stream")},
        )

    post = await client.get("/kb/files")
    post_ids = {f["id"] for f in post.json()["files"]}
    assert pre_ids == post_ids, "registry row must be rolled back on move failure"


# --------------------------------------------------------------------------- #
# GET /kb/files/{id}/memory
# --------------------------------------------------------------------------- #
async def test_get_memory_404_when_kb_missing(client):
    r = await client.get("/kb/files/kb_nope/memory")
    assert r.status_code == 404


async def test_get_memory_409_when_pending(client, app):
    state = app.state.app_state
    meta = await state.kb.create(filename="x.xlsx", size_bytes=1)
    # Default status is PENDING.
    r = await client.get(f"/kb/files/{meta.id}/memory")
    assert r.status_code == 409
    assert "status" in r.json()["detail"]


async def test_get_memory_returns_body_when_file_exists(client, app, settings):
    state = app.state.app_state
    meta = await state.kb.create(filename="sales.xlsx", size_bytes=10)

    # Write the memory file where the route will look for it.
    memory_dir = settings.kb_profiler_memory_dir
    memory_dir.mkdir(parents=True, exist_ok=True)
    memory_file = memory_dir / f"{meta.id}.md"
    memory_file.write_text("# Sales KB\nsome notes", encoding="utf-8")

    await state.kb.update_status(meta.id, "READY", memory_path=str(memory_file))

    r = await client.get(f"/kb/files/{meta.id}/memory")
    assert r.status_code == 200
    body = r.json()
    assert body["kb_id"] == meta.id
    assert body["content"] == "# Sales KB\nsome notes"
    assert body["path"] == str(memory_file)


async def test_get_memory_404_when_file_missing(client, app):
    state = app.state.app_state
    meta = await state.kb.create(filename="x.xlsx", size_bytes=1)
    # READY_PARTIAL but no memory file on disk.
    await state.kb.update_status(meta.id, "READY_PARTIAL")
    r = await client.get(f"/kb/files/{meta.id}/memory")
    assert r.status_code == 404


# --------------------------------------------------------------------------- #
# POST /kb/files/{id}/reprofile
# --------------------------------------------------------------------------- #
async def test_reprofile_404_when_kb_missing(client):
    r = await client.post("/kb/files/kb_nope/reprofile")
    assert r.status_code == 404


async def test_reprofile_409_when_already_profiling(client, app):
    state = app.state.app_state
    meta = await state.kb.create(filename="x.xlsx", size_bytes=1)
    await state.kb.update_status(meta.id, "PROFILING")
    r = await client.post(f"/kb/files/{meta.id}/reprofile")
    assert r.status_code == 409


async def test_reprofile_409_when_no_raw_xlsx(client, app):
    state = app.state.app_state
    meta = await state.kb.create(filename="x.xlsx", size_bytes=1)
    await state.kb.update_status(meta.id, "READY")
    # No raw.xlsx on disk.
    r = await client.post(f"/kb/files/{meta.id}/reprofile")
    assert r.status_code == 409


async def test_reprofile_schedules_pipeline_and_returns_202(client, app, monkeypatch):
    # Upload a file so raw.xlsx exists on disk and status reaches terminal.
    r = await client.post(
        "/kb/files",
        files={"file": ("data.xlsx", _SMALL_XLSX, "application/octet-stream")},
    )
    assert r.status_code == 202
    kb_id = r.json()["id"]
    await _poll_ready(client, kb_id)

    # Now install a stub that records invocations.
    calls: list[str] = []

    async def recording_pipeline(*, registry, settings, kb_root, kb_id, profiler=None):
        calls.append(kb_id)
        await registry.update_status(kb_id, "READY")

    monkeypatch.setattr("da_agent.server.routes.kb.run_pipeline", recording_pipeline)

    reprofile_r = await client.post(f"/kb/files/{kb_id}/reprofile")
    assert reprofile_r.status_code == 202

    # Give the background task a moment to run.
    await asyncio.sleep(0.05)
    assert kb_id in calls


# --------------------------------------------------------------------------- #
# DELETE — memory file cleanup
# --------------------------------------------------------------------------- #
async def test_delete_cleans_memory_file(client, app, settings):
    r = await client.post(
        "/kb/files",
        files={"file": ("data.xlsx", _SMALL_XLSX, "application/octet-stream")},
    )
    kb_id = r.json()["id"]
    await _poll_ready(client, kb_id)

    # Write a memory file as if the profiler had produced it.
    memory_dir = settings.kb_profiler_memory_dir
    memory_dir.mkdir(parents=True, exist_ok=True)
    memory_file = memory_dir / f"{kb_id}.md"
    memory_file.write_text("notes", encoding="utf-8")

    del_r = await client.delete(f"/kb/files/{kb_id}")
    assert del_r.status_code == 204
    assert not memory_file.exists(), "memory file should be removed on delete"
