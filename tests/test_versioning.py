"""Versioning rotation tests for KB and attachment 2-slot caps (spec §8.2).

We test the BE pre-write rotation helper directly (it lives in
`server.routes.messages`). The helper:

  1. Drops any existing `v_prev.*` (regardless of extension).
  2. Renames any `v_curr.*` to `v_prev.<same ext>`.

Then the model writes its NEW file as `v_curr.<ext>`. Calling the helper again
keeps the cap at 2: the just-written `v_curr` becomes `v_prev` and the older
`v_prev` is dropped.
"""

from __future__ import annotations

from da_agent.server.routes.messages import _rotate_versions_dir


def _touch(path, content: bytes = b"x"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)


def test_first_rotation_creates_versions_dir(tmp_path):
    versions_dir = tmp_path / "kb_xyz" / "versions"
    assert not versions_dir.exists()

    _rotate_versions_dir(versions_dir, ".xlsx")

    assert versions_dir.exists()
    assert list(versions_dir.iterdir()) == []


def test_first_write_no_prior_curr_no_rotation(tmp_path):
    versions_dir = tmp_path / "v"
    versions_dir.mkdir()

    _rotate_versions_dir(versions_dir, ".xlsx")

    # The model will then write v_curr.xlsx; this helper does not touch it.
    assert not (versions_dir / "v_curr.xlsx").exists()
    assert not (versions_dir / "v_prev.xlsx").exists()


def test_rotation_moves_curr_to_prev(tmp_path):
    versions_dir = tmp_path / "v"
    versions_dir.mkdir()
    _touch(versions_dir / "v_curr.xlsx", b"first-revision")

    _rotate_versions_dir(versions_dir, ".xlsx")

    # v_curr is gone; v_prev now holds the prior bytes.
    assert not (versions_dir / "v_curr.xlsx").exists()
    assert (versions_dir / "v_prev.xlsx").read_bytes() == b"first-revision"


def test_rotation_drops_existing_prev(tmp_path):
    """Cap at 2 — the older v_prev is discarded on rotation."""
    versions_dir = tmp_path / "v"
    versions_dir.mkdir()
    _touch(versions_dir / "v_prev.xlsx", b"older")
    _touch(versions_dir / "v_curr.xlsx", b"middle")

    _rotate_versions_dir(versions_dir, ".xlsx")

    # The "older" v_prev is gone; the previously-current bytes are now v_prev.
    assert (versions_dir / "v_prev.xlsx").read_bytes() == b"middle"
    # v_curr is gone; the model will write the new one shortly.
    assert not (versions_dir / "v_curr.xlsx").exists()


def test_third_write_keeps_only_two_slots(tmp_path):
    """Repeat the rotate-then-write cycle three times; only 2 slots survive."""
    versions_dir = tmp_path / "v"
    versions_dir.mkdir()

    # Cycle 1: write v1.
    _rotate_versions_dir(versions_dir, ".xlsx")
    _touch(versions_dir / "v_curr.xlsx", b"v1")
    assert sorted(p.name for p in versions_dir.iterdir()) == ["v_curr.xlsx"]

    # Cycle 2: rotate, write v2.
    _rotate_versions_dir(versions_dir, ".xlsx")
    _touch(versions_dir / "v_curr.xlsx", b"v2")
    assert sorted(p.name for p in versions_dir.iterdir()) == [
        "v_curr.xlsx",
        "v_prev.xlsx",
    ]
    assert (versions_dir / "v_curr.xlsx").read_bytes() == b"v2"
    assert (versions_dir / "v_prev.xlsx").read_bytes() == b"v1"

    # Cycle 3: rotate, write v3 — v1 is dropped.
    _rotate_versions_dir(versions_dir, ".xlsx")
    _touch(versions_dir / "v_curr.xlsx", b"v3")
    assert sorted(p.name for p in versions_dir.iterdir()) == [
        "v_curr.xlsx",
        "v_prev.xlsx",
    ]
    assert (versions_dir / "v_curr.xlsx").read_bytes() == b"v3"
    assert (versions_dir / "v_prev.xlsx").read_bytes() == b"v2"


def test_rotation_drops_foreign_extension_prev(tmp_path):
    """v_prev with a different extension is also dropped (cap by slot, not ext)."""
    versions_dir = tmp_path / "v"
    versions_dir.mkdir()
    _touch(versions_dir / "v_prev.csv", b"older-csv")
    _touch(versions_dir / "v_curr.xlsx", b"middle")

    _rotate_versions_dir(versions_dir, ".xlsx")

    # No v_prev.csv survives — slot is cleared.
    assert not (versions_dir / "v_prev.csv").exists()
    assert (versions_dir / "v_prev.xlsx").read_bytes() == b"middle"
