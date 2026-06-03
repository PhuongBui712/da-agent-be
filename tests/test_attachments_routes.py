"""HTTP integration tests for /sessions/{sid}/attachments endpoints.

No SDK interaction needed — these tests only exercise the attachment CRUD
layer. Mirrors the fixture pattern from test_kb_routes.py.
"""

from __future__ import annotations

import httpx
import pytest
import pytest_asyncio

from da_agent.config import Settings
from da_agent.server.app import create_app


# --------------------------------------------------------------------------- #
# Fixtures
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


@pytest_asyncio.fixture
async def sid(client):
    """Create a session and return its id."""
    r = await client.post("/sessions", json={"name": "test"})
    assert r.status_code == 201
    return r.json()["id"]


_SMALL_FILE = b"hello attachments"


# --------------------------------------------------------------------------- #
# Tests
# --------------------------------------------------------------------------- #


async def test_upload_unknown_session_returns_404(client):
    r = await client.post(
        "/sessions/sess_unknown/attachments",
        files={"file": ("report.txt", _SMALL_FILE, "text/plain")},
    )
    assert r.status_code == 404


async def test_upload_returns_201_and_correct_response(client, app, sid):
    r = await client.post(
        f"/sessions/{sid}/attachments",
        files={"file": ("report.txt", _SMALL_FILE, "text/plain")},
    )
    assert r.status_code == 201
    body = r.json()
    assert body["attachment_id"].startswith("att_")
    assert body["filename"] == "report.txt"
    assert body["size_bytes"] == len(_SMALL_FILE)

    # File must be on disk at the expected path.
    att_id = body["attachment_id"]
    dest = app.state.app_state.settings.attachments_dir / sid / att_id / "report.txt"
    assert dest.exists(), f"expected file at {dest}"


async def test_upload_too_large_returns_413(client, sid, settings):
    # Temporarily lower the cap so we don't need a huge payload.
    settings.attachment_max_bytes = 10
    payload = b"x" * 100
    r = await client.post(
        f"/sessions/{sid}/attachments",
        files={"file": ("big.bin", payload, "application/octet-stream")},
    )
    assert r.status_code == 413


async def test_upload_empty_file_returns_400(client, sid):
    r = await client.post(
        f"/sessions/{sid}/attachments",
        files={"file": ("empty.txt", b"", "text/plain")},
    )
    assert r.status_code == 400


async def test_list_returns_uploaded_attachment(client, sid):
    r_up = await client.post(
        f"/sessions/{sid}/attachments",
        files={"file": ("data.csv", b"a,b\n1,2", "text/csv")},
    )
    assert r_up.status_code == 201
    att_id = r_up.json()["attachment_id"]

    r_list = await client.get(f"/sessions/{sid}/attachments")
    assert r_list.status_code == 200
    ids = [a["attachment_id"] for a in r_list.json()["attachments"]]
    assert att_id in ids


async def test_delete_removes_entry_and_disk_dir(client, app, sid):
    r_up = await client.post(
        f"/sessions/{sid}/attachments",
        files={"file": ("notes.txt", b"some content", "text/plain")},
    )
    assert r_up.status_code == 201
    att_id = r_up.json()["attachment_id"]

    # Delete.
    r_del = await client.delete(f"/sessions/{sid}/attachments/{att_id}")
    assert r_del.status_code == 204

    # No longer in the list.
    r_list = await client.get(f"/sessions/{sid}/attachments")
    ids = [a["attachment_id"] for a in r_list.json()["attachments"]]
    assert att_id not in ids

    # On-disk directory for the attachment is gone.
    att_dir = app.state.app_state.settings.attachments_dir / sid / att_id
    assert not att_dir.exists(), f"expected {att_dir} to be removed"


async def test_delete_session_removes_attachments_dir(client, app, sid):
    # Upload so there is something on disk.
    r_up = await client.post(
        f"/sessions/{sid}/attachments",
        files={"file": ("x.txt", b"data", "text/plain")},
    )
    assert r_up.status_code == 201

    att_root = app.state.app_state.settings.attachments_dir
    sid_dir = att_root / sid
    assert sid_dir.exists(), "attachment dir should exist before session delete"

    r_del = await client.delete(f"/sessions/{sid}")
    assert r_del.status_code == 204

    assert not sid_dir.exists(), "attachment dir should be removed with the session"


async def test_delete_nonexistent_attachment_returns_404(client, sid):
    r = await client.delete(f"/sessions/{sid}/attachments/att_doesnotexist")
    assert r.status_code == 404


async def test_list_empty_session_returns_empty_list(client, sid):
    r = await client.get(f"/sessions/{sid}/attachments")
    assert r.status_code == 200
    assert r.json()["attachments"] == []
