"""HTTP integration tests for /outputs endpoints (spec §8.2, §11).

No SDK interaction needed — these tests exercise the standalone outputs
registry + routes only. Mirrors the fixture pattern from
test_attachments_routes.py / test_kb_versions_routes.py.
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


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
async def _register_output(
    app, *, session_id: str | None = None, content: bytes = b"PK fake xlsx"
):
    """Drop a file into outputs_dir/_tmp/, then register it via the registry.

    Phase A 2026-06-01: layout is `outputs/<session_id>/<filename>`. We
    default to `sess_default` when the caller doesn't care which session
    owns the row.
    """
    state = app.state.app_state
    tmp_dir = state.settings.outputs_dir / "_tmp"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    src = tmp_dir / f"src_{id(content):x}.xlsx"
    src.write_bytes(content)
    layout_sid = session_id or "sess_default"
    meta = await state.outputs.register_standalone(
        session_id=layout_sid,
        file_path=src,
        filename="report.xlsx",
        kind="standalone",
        source_id=None,
    )
    return meta


# --------------------------------------------------------------------------- #
# Tests
# --------------------------------------------------------------------------- #
async def test_list_outputs_empty(client):
    r = await client.get("/outputs")
    assert r.status_code == 200
    assert r.json() == {"outputs": []}


async def test_list_outputs_returns_registered_entry(client, app):
    meta = await _register_output(app)
    r = await client.get("/outputs")
    assert r.status_code == 200
    items = r.json()["outputs"]
    assert len(items) == 1
    assert items[0]["output_id"] == meta.id
    assert items[0]["kind"] == "standalone"
    assert items[0]["filename"] == "report.xlsx"


async def test_get_outputs_includes_download_url(client, app):
    """Phase A 2026-06-01: REST list/meta surfaces `download_url` so the FE
    can render persistent download cards from session replay alone."""
    a = await _register_output(app, session_id="sess_dl_a")
    b = await _register_output(app, session_id="sess_dl_b")

    r = await client.get("/outputs")
    assert r.status_code == 200
    items = r.json()["outputs"]
    assert len(items) >= 2
    by_id = {item["output_id"]: item for item in items}
    for oid in (a.id, b.id):
        assert "download_url" in by_id[oid]
        assert by_id[oid]["download_url"] == f"/outputs/{oid}"

    # /meta endpoint surfaces it too.
    meta_r = await client.get(f"/outputs/{a.id}/meta")
    assert meta_r.status_code == 200
    assert meta_r.json()["download_url"] == f"/outputs/{a.id}"


async def test_list_outputs_filters_by_session_id(client, app):
    a = await _register_output(app, session_id="sess_a")
    b = await _register_output(app, session_id="sess_b")

    r_all = await client.get("/outputs")
    assert {o["output_id"] for o in r_all.json()["outputs"]} == {a.id, b.id}

    r_a = await client.get("/outputs", params={"session_id": "sess_a"})
    ids_a = [o["output_id"] for o in r_a.json()["outputs"]]
    assert ids_a == [a.id]

    r_b = await client.get("/outputs", params={"session_id": "sess_b"})
    ids_b = [o["output_id"] for o in r_b.json()["outputs"]]
    assert ids_b == [b.id]


async def test_get_output_meta_returns_meta(client, app):
    meta = await _register_output(app, session_id="sess_x")
    r = await client.get(f"/outputs/{meta.id}/meta")
    assert r.status_code == 200
    body = r.json()
    assert body["output_id"] == meta.id
    assert body["source_session_id"] == "sess_x"
    assert body["mime"].endswith("spreadsheetml.sheet")


async def test_download_output_returns_file_bytes(client, app):
    payload = b"PK\x03\x04 fake xlsx body"
    meta = await _register_output(app, content=payload)

    r = await client.get(f"/outputs/{meta.id}")
    assert r.status_code == 200
    assert r.content == payload
    # FileResponse uses meta.mime for content-type.
    assert "spreadsheetml" in r.headers["content-type"]


async def test_delete_output_removes_entry_and_files(client, app):
    """Phase A 2026-06-01: delete removes data file and sidecar, not a directory."""
    meta = await _register_output(app)
    state = app.state.app_state
    settings = state.settings
    # data file and sidecar both live under outputs/<session_id>/
    data_file = settings.outputs_dir / "sess_default" / "report.xlsx"
    sidecar = settings.outputs_dir / "sess_default" / f".{meta.id}.meta.json"
    assert data_file.exists()
    assert sidecar.exists()

    r = await client.delete(f"/outputs/{meta.id}")
    assert r.status_code == 204

    # Subsequent fetches 404.
    miss = await client.get(f"/outputs/{meta.id}/meta")
    assert miss.status_code == 404

    # On-disk files are gone.
    assert not data_file.exists()
    assert not sidecar.exists()


async def test_meta_unknown_id_returns_404(client):
    r = await client.get("/outputs/out_doesnotexist/meta")
    assert r.status_code == 404


async def test_download_unknown_id_returns_404(client):
    r = await client.get("/outputs/out_doesnotexist")
    assert r.status_code == 404


async def test_delete_unknown_id_returns_404(client):
    r = await client.delete("/outputs/out_doesnotexist")
    assert r.status_code == 404
