"""HTTP integration tests for /kb/files/{kb_id}/versions* endpoints."""

from __future__ import annotations

import json

import httpx
import pytest
import pytest_asyncio

from da_agent.config import Settings
from da_agent.server.app import create_app

# Minimal valid XLSX magic bytes (PK zip header). The route uses FileResponse
# which streams raw bytes — it never parses the file — so this is sufficient.
_FAKE_XLSX = b"PK\x03\x04" + b"\x00" * 28


# --------------------------------------------------------------------------- #
# Fixtures — mirror test_kb_routes.py verbatim
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
async def _register_kb(app) -> str:
    """Register a KbMeta directly in the registry and return its id."""
    state = app.state.app_state
    meta = await state.kb.create(filename="x.xlsx", size_bytes=10)
    return meta.id


def _versions_dir(app, kb_id: str):
    return app.state.app_state.settings.kb_dir / kb_id / "versions"


# --------------------------------------------------------------------------- #
# Tests
# --------------------------------------------------------------------------- #
async def test_list_versions_unknown_kb_returns_404(client):
    r = await client.get("/kb/files/kb_nope/versions")
    assert r.status_code == 404


async def test_list_versions_no_versions_dir_returns_empty(client, app):
    kb_id = await _register_kb(app)
    # versions/ directory intentionally absent
    r = await client.get(f"/kb/files/{kb_id}/versions")
    assert r.status_code == 200
    assert r.json() == {"versions": []}


async def test_list_versions_only_curr_no_sidecar(client, app):
    """2-slot cap: a fresh write lands in `v_curr` only."""
    kb_id = await _register_kb(app)
    vdir = _versions_dir(app, kb_id)
    vdir.mkdir(parents=True)
    (vdir / "v_curr.xlsx").write_bytes(_FAKE_XLSX)

    r = await client.get(f"/kb/files/{kb_id}/versions")
    assert r.status_code == 200
    versions = r.json()["versions"]
    assert len(versions) == 1
    entry = versions[0]
    assert entry["version"] == "v_curr"
    # No v_prev exists yet, so the default parent for v_curr stays the
    # sidecar-supplied "v_prev" string (the response default for v_curr).
    assert entry["operation"] is None
    assert entry["size_bytes"] > 0
    assert entry["created_at"] > 0


async def test_list_versions_curr_and_prev_with_sidecars(client, app):
    """After two writes the 2-slot cap holds: v_curr and v_prev side-by-side."""
    kb_id = await _register_kb(app)
    vdir = _versions_dir(app, kb_id)
    vdir.mkdir(parents=True)

    # v_prev — older revision; no sidecar
    (vdir / "v_prev.xlsx").write_bytes(_FAKE_XLSX)
    # v_curr — latest revision; full sidecar
    (vdir / "v_curr.xlsx").write_bytes(_FAKE_XLSX)
    sidecar = {
        "parent_version": "v_prev",
        "operation": "overwrite_sheet",
        "sheet_affected": "Sales",
        "source_session_id": "sess_x",
        "created_at": 1700000000.0,
        "size_bytes": 42,
    }
    (vdir / "v_curr.meta.json").write_text(json.dumps(sidecar), encoding="utf-8")

    r = await client.get(f"/kb/files/{kb_id}/versions")
    assert r.status_code == 200
    versions = r.json()["versions"]
    assert len(versions) == 2

    v_curr = next(v for v in versions if v["version"] == "v_curr")
    assert v_curr["parent_version"] == "v_prev"
    assert v_curr["operation"] == "overwrite_sheet"
    assert v_curr["sheet_affected"] == "Sales"
    assert v_curr["source_session_id"] == "sess_x"
    assert v_curr["created_at"] == 1700000000.0
    assert v_curr["size_bytes"] == 42


async def test_download_v_curr_returns_bytes(client, app):
    kb_id = await _register_kb(app)
    vdir = _versions_dir(app, kb_id)
    vdir.mkdir(parents=True)
    content = _FAKE_XLSX + b"extra_data"
    (vdir / "v_curr.xlsx").write_bytes(content)

    r = await client.get(f"/kb/files/{kb_id}/versions/v_curr/download")
    assert r.status_code == 200
    assert r.content == content
    assert "spreadsheetml" in r.headers["content-type"]


async def test_download_v_prev_returns_bytes(client, app):
    """`v_prev` is the rollback slot; download must work too."""
    kb_id = await _register_kb(app)
    vdir = _versions_dir(app, kb_id)
    vdir.mkdir(parents=True)
    content = _FAKE_XLSX + b"older_revision"
    (vdir / "v_prev.xlsx").write_bytes(content)

    r = await client.get(f"/kb/files/{kb_id}/versions/v_prev/download")
    assert r.status_code == 200
    assert r.content == content


async def test_download_version_missing_slot_returns_404(client, app):
    kb_id = await _register_kb(app)
    vdir = _versions_dir(app, kb_id)
    vdir.mkdir(parents=True)

    r = await client.get(f"/kb/files/{kb_id}/versions/v_curr/download")
    assert r.status_code == 404


async def test_download_version_unknown_kb_returns_404(client):
    r = await client.get("/kb/files/kb_nope/versions/v_curr/download")
    assert r.status_code == 404


async def test_download_version_bogus_format_returns_400(client, app):
    kb_id = await _register_kb(app)
    r = await client.get(f"/kb/files/{kb_id}/versions/v1/download")
    assert r.status_code == 400


async def test_import_sheet_missing_body_returns_422(client):
    r = await client.post("/kb/files/import-sheet")
    assert r.status_code == 422


async def test_import_sheet_invalid_url_returns_400(client):
    r = await client.post(
        "/kb/files/import-sheet",
        json={"url": "https://example.com/not-a-sheet"},
    )
    assert r.status_code == 400
