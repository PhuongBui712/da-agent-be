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
async def app(settings):
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
        if body["status"] in {"READY", "FAILED"}:
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


async def test_get_manifest_409_when_not_ready(client, app):
    state = app.state.app_state
    meta = await state.kb.create(filename="x.xlsx", size_bytes=1)
    # Status starts as PENDING — no manifest written.
    r = await client.get(f"/kb/files/{meta.id}/manifest")
    assert r.status_code == 409


async def test_get_manifest_200_after_ready(client):
    r = await client.post(
        "/kb/files",
        files={"file": ("data.xlsx", _SMALL_XLSX, "application/octet-stream")},
    )
    kb_id = r.json()["id"]
    await _poll_ready(client, kb_id)

    manifest_r = await client.get(f"/kb/files/{kb_id}/manifest")
    assert manifest_r.status_code == 200
    body = manifest_r.json()
    assert "kb_id" in body
    assert "sheets" in body
    assert "relationships" in body


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

    # Delete.
    del_r = await client.delete(f"/kb/files/{kb_id}")
    assert del_r.status_code == 204

    # Row is gone.
    get_r = await client.get(f"/kb/files/{kb_id}")
    assert get_r.status_code == 404

    # On-disk directory is gone.
    kb_dir = app.state.app_state.settings.kb_dir / kb_id
    assert not kb_dir.exists()


async def test_post_does_not_block_other_requests(client, app, monkeypatch):
    import time as _time
    from da_agent.kb import preprocess as _preprocess_mod

    original_build = _preprocess_mod.build_manifest

    def slow_build(raw_path, kb_id):
        _time.sleep(1.5)
        return original_build(raw_path, kb_id)

    monkeypatch.setattr("da_agent.kb.runner.build_manifest", slow_build)

    # POST the file (fire-and-forget pipeline starts with slow_build).
    await client.post(
        "/kb/files",
        files={"file": ("data.xlsx", _SMALL_XLSX, "application/octet-stream")},
    )

    # /health should respond immediately even though the pipeline is sleeping.
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
