"""Unit tests for the cached delta DuckDB artifact (dpm.delta_db)."""

from __future__ import annotations

from pathlib import Path

import polars as pl

from dpm._types import DeltaResult
from dpm.delta_db import (
    delta_db_path,
    delta_perimeters,
    load_apply_changes,
    load_cell_changes,
    load_delta_counts,
    load_delta_meta,
    load_delta_result,
    load_delta_tree,
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
            # aeb row is a matrix cell (row + non-C0010 column) → Matrix,
            # which must still pass the apply-delta filter.
            _row(DELTA_STRUCTURE_COLS, perimeter="aeb", template_code="T1",
                 subtemplate_code="S1", row_code="R010", column_code="C020",
                 qname_old="a", qname_new="b", status="Modified",
                 type="Matrix"),
            _row(DELTA_STRUCTURE_COLS, perimeter="aes", template_code="T2",
                 subtemplate_code="S2", row_code="", column_code="C020",
                 qname_old="x", status="Deleted", type="Column"),
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
    # Matrix cells.
    aeb = load_apply_changes(path, "AEB")
    assert aeb.height == 1
    row = aeb.to_dicts()[0]
    assert row["type"] == "Matrix"
    assert (row["status"], row["qname_old"], row["qname_new"]) == ("Modified", "a", "b")
    aes = load_apply_changes(path, "aes")
    assert aes.to_dicts()[0]["status"] == "Deleted"


def _label_result() -> DeltaResult:
    """One perimeter/subtemplate with a label-only move, a real dims change, an Added
    and a Deleted cell — to exercise the include_labels filter."""
    structure = pl.DataFrame(
        [
            # label-only: qname + dimensions identical, only the row label differs.
            _row(DELTA_STRUCTURE_COLS, perimeter="qrs", template_code="T1",
                 subtemplate_code="S1", row_code="R010", column_code="C010",
                 qname_old="q", qname_new="q", dimensions_old="d=m",
                 dimensions_new="d=m", row_label_old="Old", row_label_new="New",
                 status="Modified", type="Row"),
            # genuine change: dimensions differ.
            _row(DELTA_STRUCTURE_COLS, perimeter="qrs", template_code="T1",
                 subtemplate_code="S1", row_code="R020", column_code="C010",
                 qname_old="q2", qname_new="q2", dimensions_old="d=m1",
                 dimensions_new="d=m2", status="Modified", type="Row"),
            _row(DELTA_STRUCTURE_COLS, perimeter="qrs", template_code="T1",
                 subtemplate_code="S1", row_code="R030", column_code="C010",
                 qname_new="q3", status="Added", type="Row"),
            _row(DELTA_STRUCTURE_COLS, perimeter="qrs", template_code="T1",
                 subtemplate_code="S1", row_code="R040", column_code="C010",
                 qname_old="q4", status="Deleted", type="Row"),
        ],
        schema={c: pl.String for c in DELTA_STRUCTURE_COLS},
    )
    empty_m = pl.DataFrame(schema={c: pl.String for c in DELTA_METRIC_COLS})
    empty_d = pl.DataFrame(schema={c: pl.String for c in DELTA_DIMENSION_COLS})
    return DeltaResult(structure=structure, metrics=empty_m, dimensions=empty_d)


def _tree_leaf_counts(tree: list) -> dict[str, int]:
    for _perim, templates in tree:
        for _t_code, _t_struct, subs in templates:
            for _s_code, _s_struct, counts in subs:
                return counts
    return {}


def test_include_labels_filter(tmp_path: Path):
    path = delta_db_path(tmp_path, "old", "new")
    save_delta_db(_label_result(), path, "old", "new")

    # Default: everything included → both Modified cells present.
    full = load_cell_changes(path, "qrs", "S1")
    assert full.height == 4
    assert set(full["status"]) == {"Modified", "Added", "Deleted"}

    # Excluded: the label-only Modified row drops; the dims change + Added/Deleted stay.
    filtered = load_cell_changes(path, "qrs", "S1", include_labels=False)
    assert filtered.height == 3
    rows = {(r["row_code"], r["status"]) for r in filtered.to_dicts()}
    assert ("R010", "Modified") not in rows  # label-only gone
    assert ("R020", "Modified") in rows  # dims change kept

    # Counts and tree reflect the same drop (one fewer Modified).
    assert load_delta_counts(path)["structure"]["Modified"] == 2
    assert load_delta_counts(path, include_labels=False)["structure"]["Modified"] == 1
    assert _tree_leaf_counts(load_delta_tree(path))["Modified"] == 2
    assert _tree_leaf_counts(load_delta_tree(path, include_labels=False))["Modified"] == 1


def test_load_cell_changes_scoping(tmp_path: Path):
    # Two subtemplates under one perimeter; verify perimeter / template / subtemplate
    # scoping narrows the changed cells returned.
    path = delta_db_path(tmp_path, "old", "new")
    save_delta_db(
        _struct_result(
            [
                _cell("p", "T1", "S1", "R010", "Modified", qname_old="a", qname_new="b"),
                _cell("p", "T1", "S2", "R010", "Added", qname_new="c"),
                _cell("p", "T2", "S3", "R010", "Deleted", qname_old="d"),
            ]
        ),
        path,
        "old",
        "new",
    )
    perim = load_cell_changes(path, "p")
    assert perim.height == 3
    assert "subtemplate_code" in perim.columns

    tmpl = load_cell_changes(path, "p", template_code="T1")
    assert set(tmpl["subtemplate_code"]) == {"S1", "S2"}

    sub = load_cell_changes(path, "p", "S1")
    assert sub.height == 1
    assert set(sub["subtemplate_code"]) == {"S1"}


def test_save_overwrites(tmp_path: Path):
    path = _saved(tmp_path)
    save_delta_db(_result(), path, "2.8.2", "2.10.0")  # must not raise on existing file
    assert load_delta_counts(path)["structure"]["Modified"] == 1


def _cell(perimeter, template, subtemplate, row, status, **kw) -> dict[str, str]:
    return _row(
        DELTA_STRUCTURE_COLS,
        perimeter=perimeter,
        template_code=template,
        subtemplate_code=subtemplate,
        row_code=row,
        column_code="C010",
        status=status,
        type="Row",
        **kw,
    )


def _struct_result(rows: list[dict[str, str]]) -> DeltaResult:
    structure = pl.DataFrame(rows, schema={c: pl.String for c in DELTA_STRUCTURE_COLS})
    empty_m = pl.DataFrame(schema={c: pl.String for c in DELTA_METRIC_COLS})
    empty_d = pl.DataFrame(schema={c: pl.String for c in DELTA_DIMENSION_COLS})
    return DeltaResult(structure=structure, metrics=empty_m, dimensions=empty_d)


def _tree_index(tree: list) -> dict[tuple[str, str], tuple[str, dict]]:
    """Map (template, subtemplate) → (s_struct, counts) and (template, "") → t_struct."""
    out: dict[tuple[str, str], tuple[str, dict]] = {}
    for _perim, templates in tree:
        for t_code, t_struct, subs in templates:
            out[(t_code, "")] = (t_struct, {})
            for s_code, s_struct, counts in subs:
                out[(t_code, s_code)] = (s_struct, counts)
    return out


def test_subtemplate_red_only_when_fully_deleted(tmp_path: Path):
    # S1: all cells deleted → red. S2: deletes + a surviving Kept cell → not red.
    path = delta_db_path(tmp_path, "old", "new")
    save_delta_db(
        _struct_result(
            [
                _cell("p", "T1", "S1", "R010", "Deleted", qname_old="a"),
                _cell("p", "T1", "S1", "R020", "Deleted", qname_old="b"),
                _cell("p", "T2", "S2", "R010", "Deleted", qname_old="c"),
                _cell("p", "T2", "S2", "R020", "Kept", qname_old="d", qname_new="d"),
            ]
        ),
        path,
        "old",
        "new",
    )
    idx = _tree_index(load_delta_tree(path))
    assert idx[("T1", "S1")][0] == "Deleted"  # every cell deleted → red
    assert idx[("T2", "S2")][0] == ""  # a Kept survivor → not red
    # Kept cell is not shown in the badge counts.
    assert idx[("T2", "S2")][1] == {"Deleted": 1}


def test_template_red_only_when_all_subtemplates_deleted(tmp_path: Path):
    # T1: both subtemplates fully deleted → red. T2: one deleted, one partial → not red.
    path = delta_db_path(tmp_path, "old", "new")
    save_delta_db(
        _struct_result(
            [
                _cell("p", "T1", "S1", "R010", "Deleted", qname_old="a"),
                _cell("p", "T1", "S2", "R010", "Deleted", qname_old="b"),
                _cell("p", "T2", "S3", "R010", "Deleted", qname_old="c"),
                _cell("p", "T2", "S4", "R010", "Deleted", qname_old="d"),
                _cell("p", "T2", "S4", "R020", "Kept", qname_old="e", qname_new="e"),
            ]
        ),
        path,
        "old",
        "new",
    )
    idx = _tree_index(load_delta_tree(path))
    assert idx[("T1", "")][0] == "Deleted"  # all subtemplates red → red
    assert idx[("T2", "")][0] == ""  # S4 has a survivor → not all red → not red


def test_added_rollup(tmp_path: Path):
    # Wholly-new subtemplate/template → green (Added) via the same content rule.
    path = delta_db_path(tmp_path, "old", "new")
    save_delta_db(
        _struct_result(
            [
                _cell("p", "T1", "S1", "R010", "Added", qname_new="a"),
                _cell("p", "T1", "S1", "R020", "Added", qname_new="b"),
            ]
        ),
        path,
        "old",
        "new",
    )
    idx = _tree_index(load_delta_tree(path))
    assert idx[("T1", "S1")][0] == "Added"
    assert idx[("T1", "")][0] == "Added"


def test_label_only_survivor_keeps_node_out_of_red(tmp_path: Path):
    # With include_labels=False the label-only Modified cell is hidden from counts,
    # but it is still a survivor → the subtemplate must not read as red.
    path = delta_db_path(tmp_path, "old", "new")
    save_delta_db(
        _struct_result(
            [
                _cell("p", "T1", "S1", "R010", "Deleted", qname_old="a"),
                _cell(
                    "p", "T1", "S1", "R020", "Modified",
                    qname_old="q", qname_new="q",
                    dimensions_old="d=m", dimensions_new="d=m",
                    row_label_old="Old", row_label_new="New",
                ),
            ]
        ),
        path,
        "old",
        "new",
    )
    idx = _tree_index(load_delta_tree(path, include_labels=False))
    # Label-only Modified is dropped from the badge, only the Delete shows…
    assert idx[("T1", "S1")][1] == {"Deleted": 1}
    # …but the surviving cell keeps the node out of the red set.
    assert idx[("T1", "S1")][0] == ""
