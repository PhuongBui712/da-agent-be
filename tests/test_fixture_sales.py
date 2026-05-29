"""Sanity test for tests/fixtures/sales.xlsx.

Ensures the fixture remains preprocessable. If it breaks (header changes,
sheet renames, openpyxl bumps), this fails before the live smoke does.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from da_agent.kb import run_pipeline
from da_agent.kb.registry import KbRegistry


FIXTURE = Path(__file__).parent / "fixtures" / "sales.xlsx"


@pytest.mark.asyncio
async def test_sales_fixture_preprocesses(tmp_path: Path) -> None:
    assert FIXTURE.exists(), "run tests/fixtures/_gen_sales_xlsx.py first"
    kb_root = tmp_path / "kb"
    kb_root.mkdir()
    registry = KbRegistry(kb_root / "registry.json")
    meta = await registry.create(
        filename="sales.xlsx", size_bytes=FIXTURE.stat().st_size
    )
    kb_dir = kb_root / meta.id
    kb_dir.mkdir()
    (kb_dir / "raw.xlsx").write_bytes(FIXTURE.read_bytes())
    await run_pipeline(registry=registry, kb_root=kb_root, kb_id=meta.id)
    updated = await registry.get(meta.id)
    assert updated is not None
    assert updated.status == "READY", updated.error
    manifest_path = kb_dir / "manifest.json"
    assert manifest_path.exists()
    payload = manifest_path.read_text("utf-8")
    # Sanity: each sheet name appears in the manifest.
    for sheet in ("Customers", "Products", "Sales"):
        assert sheet in payload
