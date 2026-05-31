from pathlib import Path
import pytest
from da_agent.server.routes.messages import _unique_filename


def test_unique_filename_returns_base_when_no_collision(tmp_path: Path):
    assert _unique_filename(tmp_path, "report", ".xlsx") == "report.xlsx"


def test_unique_filename_bumps_to_v2_on_collision(tmp_path: Path):
    (tmp_path / "report.xlsx").write_bytes(b"existing")
    assert _unique_filename(tmp_path, "report", ".xlsx") == "report_v2.xlsx"


def test_unique_filename_bumps_to_v3_when_v2_exists(tmp_path: Path):
    (tmp_path / "report.xlsx").write_bytes(b"existing")
    (tmp_path / "report_v2.xlsx").write_bytes(b"existing v2")
    assert _unique_filename(tmp_path, "report", ".xlsx") == "report_v3.xlsx"
