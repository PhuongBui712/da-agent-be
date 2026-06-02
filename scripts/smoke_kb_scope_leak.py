#!/usr/bin/env python3
"""Smoke for the per-session symlink farm — verifies kb_scope actually
constrains what the SDK can see (spec §8.5 / Bug #1).

This smoke does NOT require a live model. It fabricates two READY KB
entries directly in the registry and checks the symlink farm shape after
each turn body is processed via `prepare_session_root` +
`rebuild_kb_symlinks`. The farm is the authoritative scope-enforcement
layer; the prose `<scope>` block is verified separately by unit tests.

Run:
    .venv/bin/python scripts/smoke_kb_scope_leak.py

Exits 0 on PASS, 1 on FAIL.
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from pathlib import Path

# Make the BE source layout importable when run directly from scripts/.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from da_agent.config import Settings
from da_agent.server.scope import ScopeBlock, ScopeKbEntry
from da_agent.server.session_farm import (
    prepare_session_root,
    rebuild_kb_symlinks,
)


_KB_A = "kb_aaaaaaaaaaaaaaaa"
_KB_B = "kb_bbbbbbbbbbbbbbbb"


class _Result:
    def __init__(self) -> None:
        self.failures: list[str] = []

    def check(self, label: str, ok: bool, detail: str = "") -> None:
        suffix = f" — {detail}" if detail else ""
        if ok:
            print(f"  PASS  {label}{suffix}")
        else:
            print(f"  FAIL  {label}{suffix}")
            self.failures.append(label)


def _seed_kb(settings: Settings, kb_id: str, filename: str) -> None:
    """Create a fake KB on disk — raw.xlsx + memory note + registry entry."""
    kb_root = settings.kb_dir / kb_id
    kb_root.mkdir(parents=True, exist_ok=True)
    (kb_root / "raw.xlsx").write_bytes(b"PK\x03\x04dummy-xlsx-bytes")
    memory_path = settings.kb_profiler_memory_dir / f"{kb_id}.md"
    memory_path.parent.mkdir(parents=True, exist_ok=True)
    memory_path.write_text(f"# {filename}\nstub memory note\n", encoding="utf-8")


def _scope_with(settings: Settings, kb_ids: list[str]) -> ScopeBlock:
    """Build a ScopeBlock the way `build_scope` would for the listed kb_ids."""
    block = ScopeBlock()
    for kb_id in kb_ids:
        memory_path = settings.kb_profiler_memory_dir / f"{kb_id}.md"
        size = memory_path.stat().st_size if memory_path.exists() else 0
        block.kb_entries.append(
            ScopeKbEntry(
                kb_id=kb_id,
                filename=f"{kb_id}.xlsx",
                memory_path=memory_path if memory_path.exists() else None,
                memory_size=size,
            )
        )
        block.total_memory_bytes += size
    return block


async def _run() -> int:
    res = _Result()

    tmp_root = Path(tempfile.mkdtemp(prefix="da-agent-smoke-scope-"))
    print(f"→ data_root: {tmp_root}")
    os.environ["DA_AGENT_HOME"] = str(tmp_root)
    settings = Settings()
    settings.data_root = tmp_root
    settings.ensure_dirs()

    _seed_kb(settings, _KB_A, "Sales.xlsx")
    _seed_kb(settings, _KB_B, "Inventory.xlsx")

    sid = "sess_smoke_scope_test"
    prepare_session_root(settings, sid)

    # Phase 1 — scope: only kb_A.
    print("\n[smoke] phase 1: kb_scope=[kb_A] → farm must contain only kb_A")
    block_a = _scope_with(settings, [_KB_A])
    rebuild_kb_symlinks(settings, sid, block_a)
    farm = settings.session_kb_dir(sid)
    entries = sorted(p.name for p in farm.iterdir())
    res.check(
        "farm contains exactly the scoped KB",
        entries == [_KB_A],
        f"expected [{_KB_A}], got {entries}",
    )
    res.check(
        "scoped entry is a symlink",
        (farm / _KB_A).is_symlink(),
        f"is_symlink={ (farm / _KB_A).is_symlink() }",
    )
    res.check(
        "symlink resolves to canonical kb_dir/<id>",
        (farm / _KB_A).resolve() == (settings.kb_dir / _KB_A).resolve(),
        f"resolved={(farm / _KB_A).resolve()}",
    )

    # Phase 2 — scope changes to kb_B only. Farm must replace, not union.
    print("\n[smoke] phase 2: kb_scope=[kb_B] → farm must replace kb_A with kb_B")
    block_b = _scope_with(settings, [_KB_B])
    rebuild_kb_symlinks(settings, sid, block_b)
    entries = sorted(p.name for p in farm.iterdir())
    res.check(
        "farm replaced kb_A with kb_B (no leak across turns)",
        entries == [_KB_B],
        f"expected [{_KB_B}], got {entries}",
    )

    # Phase 3 — canonical outputs/<sid>/ exists and is NOT a symlink alias
    # (2026-06-02 Bug-A: the alias was rejected by the SDK sandbox; we now
    # mount canonical directly via add_dirs).
    print("\n[smoke] phase 3: canonical outputs dir + no legacy symlink alias")
    view = settings.session_outputs_view_dir(sid)
    canonical = settings.outputs_dir / sid
    res.check(
        "canonical outputs/<sid>/ exists as a real dir",
        canonical.is_dir() and not canonical.is_symlink(),
        f"is_dir={canonical.is_dir()} is_symlink={canonical.is_symlink()}",
    )
    res.check(
        "legacy outputs alias is NOT created under sessions-data",
        not view.exists() or not view.is_symlink(),
        f"exists={view.exists()} is_symlink={view.is_symlink()}",
    )

    # Phase 4 — empty scope means empty farm (default-all is not exercised
    # here; the BE renders default-all into a populated ScopeBlock before
    # calling this function, so an empty block is the correct equivalent).
    print("\n[smoke] phase 4: scope=[] → farm becomes empty")
    rebuild_kb_symlinks(settings, sid, _scope_with(settings, []))
    entries = sorted(p.name for p in farm.iterdir())
    res.check(
        "empty scope → empty farm",
        entries == [],
        f"expected [], got {entries}",
    )

    if not res.failures:
        print("\n=== PASS ===")
        return 0
    print(f"\n=== FAIL: {len(res.failures)} check(s): {res.failures} ===")
    return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(_run()))
