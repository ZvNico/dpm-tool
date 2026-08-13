"""Unit tests for the cached delta DuckDB artifact (dpm.delta_db)."""

from __future__ import annotations

from pathlib import Path

import polars as pl

from dpm._types import DeltaResult
from dpm.delta_db import (
    delta_db_path,
    delta_perimeters,
    load_apply_changes,
    load_delta_counts,
    load_delta_meta,
    load_delta_result,
    load_member_changes,
    load_metric_changes,
    load_structure_for_perimeter,
    save_delta_db,
)
from dpm.delta_schema import (
    DELTA_DIMENSION_COLS,
    DELTA_METRIC_COLS,
    DELTA_STRUCTURE_COLS,
)


def _row(cols: list[str], **kw: str) -> dict[str, str]:
    return {c: kw.get(c, "") for c in cols}


def _result() -> DeltaResult:
    structure = pl.DataFrame(
        [
            # aeb row is a matrix cell (row + non-C0010 column) → FactMatrix,
            # which must still pass the apply-delta filter.
            _row(DELTA_STRUCTURE_COLS, perimeter="aeb", template_code="T1",
                 subtemplate_code="S1", row_code="R010", column_code="C020",
                 qname_old="a", qname_new="b", status="Modified",
                 change_type="FactMatrix"),
            _row(DELTA_STRUCTURE_COLS, perimeter="aes", template_code="T2",
                 subtemplate_code="S2", row_code="", column_code="C020",
                 qname_old="x", status="Deleted", change_type="FactColumn"),
        ],
        schema={c: pl.String for c in DELTA_STRUCTURE_COLS},
    )
    metrics = pl.DataFrame(
        [
            _row(DELTA_METRIC_COLS, metric_code="a", metric_label_old="A",
                 metric_label_new="A", status="Kept"),
            _row(DELTA_METRIC_COLS, metric_code="b", metric_label_new="B",
                 status="Added"),
        ],
        schema={c: pl.String for c in DELTA_METRIC_COLS},
    )
    dimensions = pl.DataFrame(schema={c: pl.String for c in DELTA_DIMENSION_COLS})
    return DeltaResult(structure=structure, metrics=metrics, dimensions=dimensions)


def _saved(tmp_path: Path) -> Path:
    path = delta_db_path(tmp_path, "2.8.2", "2.10.0")
    save_delta_db(_result(), path, "2.8.2", "2.10.0")
    return path


def test_meta_and_counts(tmp_path: Path):
    path = _saved(tmp_path)
    assert path.name == "2.8.2_to_2.10.0.duckdb"
    meta = load_delta_meta(path)
    assert meta["old_version"] == "2.8.2" and meta["new_version"] == "2.10.0"
    counts = load_delta_counts(path)
    assert counts["structure"] == {"Modified": 1, "Deleted": 1}
    assert counts["metrics"] == {"Kept": 1, "Added": 1}
    assert counts["dimensions"] == {}


def test_perimeters_and_structure(tmp_path: Path):
    path = _saved(tmp_path)
    assert delta_perimeters(path) == ["aeb", "aes"]
    aeb = load_structure_for_perimeter(path, "aeb")
    assert aeb.height == 1
    assert aeb.to_dicts()[0]["qname_new"] == "b"


def test_catalogue_and_roundtrip(tmp_path: Path):
    path = _saved(tmp_path)
    # load_metric_changes drops Kept (the fixture has 1 Kept + 1 Added) → 1 row.
    metrics = load_metric_changes(path)
    assert metrics.height == 1
    assert metrics.to_dicts()[0]["status"] == "Added"
    assert load_member_changes(path).height == 0
    res = load_delta_result(path)
    assert (res.structure.height, res.metrics.height, res.dimensions.height) == (2, 2, 0)


def test_load_apply_changes(tmp_path: Path):
    path = _saved(tmp_path)
    # Case-insensitive perimeter match; fact-cell changes returned, including
    # FactMatrix cells.
    aeb = load_apply_changes(path, "AEB")
    assert aeb.height == 1
    row = aeb.to_dicts()[0]
    assert row["change_type"] == "FactMatrix"
    assert (row["status"], row["qname_old"], row["qname_new"]) == ("Modified", "a", "b")
    aes = load_apply_changes(path, "aes")
    assert aes.to_dicts()[0]["status"] == "Deleted"


def test_save_overwrites(tmp_path: Path):
    path = _saved(tmp_path)
    save_delta_db(_result(), path, "2.8.2", "2.10.0")  # must not raise on existing file
    assert load_delta_counts(path)["structure"]["Modified"] == 1
