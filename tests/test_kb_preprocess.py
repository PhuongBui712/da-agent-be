"""Tests for the KB preprocessing pipeline (build_manifest)."""

from __future__ import annotations

from pathlib import Path

import openpyxl
import pytest

from da_agent.kb.preprocess import build_manifest


# --------------------------------------------------------------------------- #
# Helper
# --------------------------------------------------------------------------- #
def make_xlsx(tmp_path: Path, sheets: dict[str, list[list]]) -> Path:
    """sheets: {sheet_name: list_of_rows}. Returns path to the .xlsx."""
    wb = openpyxl.Workbook()
    wb.remove(wb.active)
    for name, rows in sheets.items():
        ws = wb.create_sheet(name)
        for row in rows:
            ws.append(row)
    path = tmp_path / "test.xlsx"
    wb.save(path)
    return path


# --------------------------------------------------------------------------- #
# Tests
# --------------------------------------------------------------------------- #
def test_single_region_single_sheet(tmp_path: Path):
    path = make_xlsx(
        tmp_path,
        {
            "Orders": [
                ["order_id", "product", "qty"],
                [1, "apple", 5],
                [2, "banana", 3],
                [3, "cherry", 8],
                [4, "date", 2],
                [5, "elderberry", 1],
            ]
        },
    )
    m = build_manifest(path, "kb_1")
    assert len(m.sheets) == 1
    assert len(m.sheets[0].regions) == 1
    col_names = [c.name for c in m.sheets[0].regions[0].columns]
    assert col_names == ["order_id", "product", "qty"]


def test_two_regions_separated_by_blank_row(tmp_path: Path):
    path = make_xlsx(
        tmp_path,
        {
            "Sheet1": [
                ["a", "b", "c"],
                [1, 2, 3],
                [4, 5, 6],
                [None, None, None],  # fully blank row
                ["x", "y", "z"],
                [7, 8, 9],
                [10, 11, 12],
            ]
        },
    )
    m = build_manifest(path, "kb_2")
    assert len(m.sheets[0].regions) == 2
    # Verify ranges differ.
    ranges = {r.range for r in m.sheets[0].regions}
    assert len(ranges) == 2


def test_two_regions_separated_by_blank_column(tmp_path: Path):
    # cols A-C data, col D blank, cols E-G data across 5 data rows
    path = make_xlsx(
        tmp_path,
        {
            "Sheet1": [
                ["a", "b", "c", None, "x", "y", "z"],
                [1, 2, 3, None, 10, 20, 30],
                [4, 5, 6, None, 40, 50, 60],
                [7, 8, 9, None, 70, 80, 90],
                [10, 11, 12, None, 100, 110, 120],
            ]
        },
    )
    m = build_manifest(path, "kb_3")
    assert len(m.sheets[0].regions) == 2


def test_dtype_inference_int_float_str_mixed_bool(tmp_path: Path):
    path = make_xlsx(
        tmp_path,
        {
            "Types": [
                ["int_col", "float_col", "str_col", "mixed_col", "bool_col"],
                [1, 1.1, "hello", 1, True],
                [2, 2.2, "world", "two", False],
                [3, 3.3, "foo", 3, True],
                [4, 4.4, "bar", "four", False],
                [5, 5.5, "baz", 5, True],
            ]
        },
    )
    m = build_manifest(path, "kb_4")
    region = m.sheets[0].regions[0]
    by_name = {c.name: c for c in region.columns}

    assert by_name["int_col"].dtype == "int"
    assert by_name["float_col"].dtype == "float"
    assert by_name["str_col"].dtype == "str"
    # int + str = mixed
    assert by_name["mixed_col"].dtype == "mixed"
    assert by_name["bool_col"].dtype == "bool"


def test_pk_role_assigned_to_unique_no_null_int_column(tmp_path: Path):
    path = make_xlsx(
        tmp_path,
        {
            "Customers": [
                ["id", "name"],
                [1, "Alice"],
                [2, "Bob"],
                [3, "Carol"],
                [4, "Dave"],
                [5, "Eve"],
            ]
        },
    )
    m = build_manifest(path, "kb_5")
    region = m.sheets[0].regions[0]
    id_col = next(c for c in region.columns if c.name == "id")
    assert id_col.role == "pk?"


def test_fk_relationship_inferred_from_overlap(tmp_path: Path):
    path = make_xlsx(
        tmp_path,
        {
            "Customers": [
                ["id", "name"],
                [1, "Alice"],
                [2, "Bob"],
                [3, "Carol"],
            ],
            "Sales": [
                ["order_id", "customer_id", "amount"],
                [101, 1, 50.0],
                [102, 2, 75.0],
                [103, 3, 30.0],
            ],
        },
    )
    m = build_manifest(path, "kb_6")
    assert len(m.relationships) == 1
    rel = m.relationships[0]
    assert rel.from_ == "Sales.customer_id"
    assert rel.to == "Customers.id"
    assert rel.confidence >= 0.8

    # Also verify the FK role is set on the source column.
    sales_sheet = next(s for s in m.sheets if s.name == "Sales")
    region = sales_sheet.regions[0]
    cid_col = next(c for c in region.columns if c.name == "customer_id")
    assert cid_col.role == "fk?->Customers.id"


def test_no_fk_when_overlap_too_low(tmp_path: Path):
    path = make_xlsx(
        tmp_path,
        {
            "Customers": [
                ["id", "name"],
                [1, "Alice"],
                [2, "Bob"],
                [3, "Carol"],
            ],
            "Sales": [
                ["order_id", "customer_id", "amount"],
                [101, 99, 50.0],
                [102, 100, 75.0],
                [103, 101, 30.0],
            ],
        },
    )
    m = build_manifest(path, "kb_7")
    assert len(m.relationships) == 0

    sales_sheet = next(s for s in m.sheets if s.name == "Sales")
    region = sales_sheet.regions[0]
    cid_col = next(c for c in region.columns if c.name == "customer_id")
    assert cid_col.role is None


def test_null_pct_computed(tmp_path: Path):
    path = make_xlsx(
        tmp_path,
        {
            "Data": [
                ["val"],
                [1],
                [None],
                [3],
                [None],
                [5],
            ]
        },
    )
    m = build_manifest(path, "kb_8")
    region = m.sheets[0].regions[0]
    col = region.columns[0]
    assert col.null_pct == pytest.approx(0.4, abs=1e-4)


def test_low_confidence_when_first_row_has_numbers(tmp_path: Path):
    # First row is purely numeric — no clear header.
    path = make_xlsx(
        tmp_path,
        {
            "Sheet1": [
                [1, 2, 3, 4, 5],
                [6, 7, 8, 9, 10],
                [11, 12, 13, 14, 15],
            ]
        },
    )
    m = build_manifest(path, "kb_9")
    region = m.sheets[0].regions[0]
    assert region.low_confidence is True


def test_sample_rows_limited_to_5(tmp_path: Path):
    rows = [["id", "val"]] + [[i, i * 2] for i in range(1, 21)]
    path = make_xlsx(tmp_path, {"Sheet1": rows})
    m = build_manifest(path, "kb_10")
    region = m.sheets[0].regions[0]
    assert len(region.sample_rows) == 5


def test_empty_sheet(tmp_path: Path):
    path = make_xlsx(tmp_path, {"Empty": []})
    m = build_manifest(path, "kb_11")
    assert len(m.sheets) == 1
    assert len(m.sheets[0].regions) == 0


def test_pk_role_assigned_to_self_referencing_id_column(tmp_path: Path):
    """`order_id` in the `Sales` sheet (no `Orders` sheet) must be PK?.

    Spec §5.1 example marks `order_id` with `role: "pk?"`. Our heuristic
    must accept generic `<noun>_id` patterns when the noun is not another
    sheet's entity.
    """
    path = make_xlsx(
        tmp_path,
        {
            "Customers": [
                ["id", "name"],
                [1, "Alice"],
                [2, "Bob"],
            ],
            "Sales": [
                ["order_id", "customer_id", "amount"],
                [101, 1, 50.0],
                [102, 2, 75.0],
                [103, 1, 30.0],
            ],
        },
    )
    m = build_manifest(path, "kb_pk1")
    sales = next(s for s in m.sheets if s.name == "Sales")
    region = sales.regions[0]
    order_col = next(c for c in region.columns if c.name == "order_id")
    cust_col = next(c for c in region.columns if c.name == "customer_id")
    assert order_col.role == "pk?", "order_id should be PK on Sales"
    # customer_id references Customers -> not a PK on Sales (FK candidate).
    assert cust_col.role != "pk?", (
        f"customer_id should not be PK on Sales (got {cust_col.role!r})"
    )
