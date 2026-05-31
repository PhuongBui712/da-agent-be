"""Phase A 2026-06-01 — DELETE /sessions/<sid> wipes per-session outputs subtree.

The route handler in `server/routes/sessions.py` calls
`state.outputs.delete_session_outputs(sid)` after the registry delete, which
should:
  * remove `outputs/<sid>/` and all files belonging to that session
  * drop matching rows from `outputs/registry.json`
  * leave outputs from other sessions untouched

These tests exercise the integration end-to-end through the HTTP route so we
catch any wiring bugs between the route, registry, and on-disk layout.
"""

from __future__ import annotations

import httpx
import pytest
import pytest_asyncio

from da_agent.config import Settings
from da_agent.server.app import create_app


# --------------------------------------------------------------------------- #
# Fixtures (mirror test_outputs_routes.py — same shape; intentional duplication
# to keep this module self-contained, matching the existing test layout).
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
async def _register_output_in(app, *, session_id: str, suffix: str = ""):
    """Register one standalone output under `outputs/<session_id>/`.

    Phase A 2026-06-01: flat layout — no `<output_id>` subdirectory.
    Returns the resulting `OutputMeta`.
    """
    state = app.state.app_state
    tmp_dir = state.settings.outputs_dir / "_tmp"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    src = tmp_dir / f"src_{session_id}{suffix}.xlsx"
    src.write_bytes(b"PK\x03\x04 fake")
    return await state.outputs.register_standalone(
        session_id=session_id,
        file_path=src,
        filename=f"report_{session_id}{suffix}.xlsx",
        kind="standalone",
        source_id=None,
    )


# --------------------------------------------------------------------------- #
# Tests
# --------------------------------------------------------------------------- #
async def test_delete_session_wipes_only_that_sessions_outputs(client, app):
    """Two sessions, one output each → DELETE A leaves B intact."""
    state = app.state.app_state

    a = await state.registry.create(name="A")
    b = await state.registry.create(name="B")
    out_a = await _register_output_in(app, session_id=a.id)
    out_b = await _register_output_in(app, session_id=b.id)

    outputs_root = state.settings.outputs_dir
    # Phase A layout: data file directly under outputs/<sid>/
    assert (outputs_root / a.id / out_a.filename).is_file()
    assert (outputs_root / b.id / out_b.filename).is_file()
    pre_listing = await state.outputs.list()
    assert {m.id for m in pre_listing} == {out_a.id, out_b.id}

    r = await client.delete(f"/sessions/{a.id}")
    assert r.status_code == 204

    # A's subtree is gone; B's is untouched.
    assert not (outputs_root / a.id).exists()
    assert (outputs_root / b.id / out_b.filename).is_file()

    # Registry only has B's row.
    post_listing = await state.outputs.list()
    assert {m.id for m in post_listing} == {out_b.id}


async def test_delete_session_with_no_outputs_is_a_noop(client, app):
    """A session that never produced any outputs deletes cleanly (no-op path)."""
    state = app.state.app_state
    sess = await state.registry.create(name="empty")

    # Sanity: outputs dir for this sid was never created.
    assert not (state.settings.outputs_dir / sess.id).exists()

    r = await client.delete(f"/sessions/{sess.id}")
    assert r.status_code == 204

    # Registry remains empty.
    listing = await state.outputs.list()
    assert listing == []


async def test_delete_session_also_removes_attachments_dir(client, app):
    """Cross-cutting check: attachments cleanup still runs alongside outputs cleanup.

    The route does both in `delete_session` — outputs first, then runtime
    discard (which clears attachments). We register an output AND drop a
    fake attachment file, then assert both subtrees are gone after delete.
    """
    state = app.state.app_state
    sess = await state.registry.create(name="mixed")
    sid = sess.id

    # Output under outputs/<sid>/.
    await _register_output_in(app, session_id=sid)

    # Attachment under attachments/<sid>/<att_id>/. We use the registry to
    # mint the att_id so the on-disk layout matches what the routes write.
    att = await state.attachments.create(
        sid, filename="upload.xlsx", size_bytes=4, mime="application/x-xlsx"
    )
    att_root = state.settings.attachments_dir / sid / att.id
    att_root.mkdir(parents=True, exist_ok=True)
    (att_root / "upload.xlsx").write_bytes(b"data")

    outputs_sid_dir = state.settings.outputs_dir / sid
    assert outputs_sid_dir.is_dir()
    assert att_root.is_dir()

    r = await client.delete(f"/sessions/{sid}")
    assert r.status_code == 204

    assert not outputs_sid_dir.exists()
    # The attachments/<sid>/ root itself is wiped by `discard_runtime` →
    # `attachments.delete_session(sid)`.
    assert not (state.settings.attachments_dir / sid).exists()
