"""Unit tests for the refactored, dimension-aware delta comparison."""

from __future__ import annotations

import polars as pl

from dpm._constants import METRIC_COLS
from dpm._types import DpmDataset, TocEntry
from dpm.delta import compare_dimensions, compare_metrics, compare_versions


def _metric_row(qname: str, dimensions: str = "", **over: str) -> dict[str, str]:
    row = {
        "perimeter": "solo",
        "template_code": "T1",
        "subtemplate_code": "S1",
        "row_code": "R010",
        "column_code": "C010",
        "qname": qname,
        "metric_label": "L",
        "row_label": "r",
        "column_label": "c",
        "dimensions": dimensions,
    }
    row.update(over)
    return row


def _facts(rows: list[dict[str, str]]) -> pl.DataFrame:
    return pl.DataFrame(rows, schema={**{c: pl.String for c in METRIC_COLS}, "dimensions": pl.String})


def _dataset(rows: list[dict[str, str]]) -> DpmDataset:
    return DpmDataset(
        metrics=_facts(rows),
        entries=[TocEntry(perimeter="solo", template_code="T1", subtemplate_code="S1")],
    )


# ── metric catalogue delta ──────────────────────────────────────────────────


def test_compare_metrics_added_deleted_modified():
    old = pl.DataFrame({"metric_code": ["a", "b", "c"], "metric_label": ["A", "B", "C"]})
    new = pl.DataFrame({"metric_code": ["b", "c", "d"], "metric_label": ["B", "C2", "D"]})
    out = {r["metric_code"]: r["status"] for r in compare_metrics(old, new).to_dicts()}
    # Every metric is listed with its status, including the unchanged 'b' (Kept).
    assert out == {"a": "Deleted", "b": "Kept", "c": "Modified", "d": "Added"}


# ── dimensions delta ────────────────────────────────────────────────────────


def test_compare_dimensions_added_deleted_modified():
    old = pl.DataFrame(
        {
            "dimension_code": ["d1", "d1"],
            "dimension_label": ["L", "L"],
            "member_code": ["x1", "x2"],
            "member_label": ["M1", "M2"],
        }
    )
    new = pl.DataFrame(
        {
            "dimension_code": ["d1", "d1"],
            "dimension_label": ["L", "L"],
            "member_code": ["x2", "x3"],
            "member_label": ["M2b", "M3"],
        }
    )
    out = {r["member_code"]: r["status"] for r in compare_dimensions(old, new).to_dicts()}
    assert out == {"x1": "Deleted", "x2": "Modified", "x3": "Added"}


# ── type: row list vs matrix vs column ──────────────────────────────────────


def test_type_row_matrix_column():
    # S1 uses only C0010 → a plain row list; S2 also has C0020 → a matrix.
    # A row-empty cell is always a Column regardless of subtemplate.
    old = _dataset(
        [
            _metric_row("s2md_met:r1", subtemplate_code="S1", column_code="C0010"),
            _metric_row("s2md_met:m1", subtemplate_code="S2", column_code="C0010"),
            _metric_row("s2md_met:m2", subtemplate_code="S2", column_code="C0020"),
            _metric_row("s2md_met:c1", subtemplate_code="S1", row_code="", column_code="C0030"),
        ]
    )
    # New version modifies a label on each so they surface (Kept rows still stamp).
    new = _dataset(
        [
            _metric_row("s2md_met:r1", subtemplate_code="S1", column_code="C0010", metric_label="X"),
            _metric_row("s2md_met:m1", subtemplate_code="S2", column_code="C0010", metric_label="X"),
            _metric_row("s2md_met:m2", subtemplate_code="S2", column_code="C0020", metric_label="X"),
            _metric_row("s2md_met:c1", subtemplate_code="S1", row_code="", column_code="C0030", metric_label="X"),
        ]
    )
    res = compare_versions(old, new)
    by_qname = {r["qname_new"]: r["type"] for r in res.structure.to_dicts()}
    assert by_qname["s2md_met:r1"] == "Row"
    assert by_qname["s2md_met:m1"] == "Matrix"
    assert by_qname["s2md_met:m2"] == "Matrix"
    assert by_qname["s2md_met:c1"] == "Column"


# ── dimension-only fact change surfaces as Modified in the structure delta ───


def test_structure_modified_when_only_dimensions_change():
    old = _dataset([_metric_row("s2md_met:mi1", dimensions="d:BL=x1")])
    new = _dataset([_metric_row("s2md_met:mi1", dimensions="d:BL=x2")])
    res = compare_versions(old, new)
    rows = res.structure.filter(
        pl.col("type").is_in(["Row", "Column", "Matrix"])
    )
    assert rows.height == 1
    row = rows.to_dicts()[0]
    assert row["status"] == "Modified"
    assert row["qname_old"] == row["qname_new"] == "s2md_met:mi1"
    assert row["dimensions_old"] == "d:BL=x1"
    assert row["dimensions_new"] == "d:BL=x2"


def test_structure_kept_when_nothing_changes():
    old = _dataset([_metric_row("s2md_met:mi1", dimensions="d:BL=x1")])
    new = _dataset([_metric_row("s2md_met:mi1", dimensions="d:BL=x1")])
    res = compare_versions(old, new)
    row = res.structure.to_dicts()[0]
    assert row["status"] == "Kept"
