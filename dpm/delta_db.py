"""Persist a computed delta between two DPM versions as a cached DuckDB artifact.

A delta DB lives at ``{delta_dir}/{old}_to_{new}.duckdb`` and holds the three
delta sections as tables plus a ``meta`` row. It is the canonical, queryable
source of truth: the Excel workbook is rendered from it, and the Explore-Delta
screen browses it.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import duckdb
import polars as pl

from dpm._types import DeltaResult
from dpm.delta_schema import (
    DELTA_DIMENSION_COLS,
    DELTA_METRIC_COLS,
    DELTA_STRUCTURE_COLS,
)

# table name → its column list (order-stable, all strings)
_STRUCTURE = "structure_changes"
_METRICS = "metric_changes"
_DIMENSIONS = "member_changes"
_TABLE_COLS = {
    _STRUCTURE: DELTA_STRUCTURE_COLS,
    _METRICS: DELTA_METRIC_COLS,
    _DIMENSIONS: DELTA_DIMENSION_COLS,
}


def delta_db_path(delta_dir: Path, old_version: str, new_version: str) -> Path:
    return delta_dir / f"{old_version}_to_{new_version}.duckdb"


def _save_table(conn: duckdb.DuckDBPyConnection, name: str, df: pl.DataFrame) -> None:
    cols = _TABLE_COLS[name]
    # Normalise to the canonical column set as strings so the schema is stable
    # even when a section is empty.
    if df.height:
        frame = df.select([pl.col(c).cast(pl.String).fill_null("") for c in cols])
    else:
        frame = pl.DataFrame(schema={c: pl.String for c in cols})
    conn.register("_tmp_save", frame.to_arrow())
    conn.execute(f"CREATE TABLE {name} AS SELECT * FROM _tmp_save")
    conn.unregister("_tmp_save")


def save_delta_db(
    result: DeltaResult,
    out_path: Path,
    old_version: str,
    new_version: str,
) -> None:
    """Write a :class:`DeltaResult` to a fresh delta DuckDB (overwrites)."""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    if out_path.exists():
        out_path.unlink()
    conn = duckdb.connect(str(out_path))
    try:
        conn.execute(
            "CREATE TABLE meta (old_version VARCHAR, new_version VARCHAR, created_at VARCHAR)"
        )
        conn.execute(
            "INSERT INTO meta VALUES (?, ?, ?)",
            [old_version, new_version, datetime.now(timezone.utc).isoformat()],
        )
        _save_table(conn, _STRUCTURE, result.structure)
        _save_table(conn, _METRICS, result.metrics)
        _save_table(conn, _DIMENSIONS, result.dimensions)
    finally:
        conn.close()


# ── read helpers ────────────────────────────────────────────────────────────


def load_delta_result(path: Path) -> DeltaResult:
    """Reconstruct a :class:`DeltaResult` from a delta DuckDB (for xlsx rendering)."""
    conn = duckdb.connect(str(path), read_only=True)
    try:
        return DeltaResult(
            structure=conn.execute(f"SELECT * FROM {_STRUCTURE}").pl(),
            metrics=conn.execute(f"SELECT * FROM {_METRICS}").pl(),
            dimensions=conn.execute(f"SELECT * FROM {_DIMENSIONS}").pl(),
        )
    finally:
        conn.close()


def load_delta_meta(path: Path) -> dict[str, str]:
    conn = duckdb.connect(str(path), read_only=True)
    try:
        row = conn.execute(
            "SELECT old_version, new_version, created_at FROM meta"
        ).fetchone()
    finally:
        conn.close()
    old, new, created = row if row else ("", "", "")
    return {"old_version": old, "new_version": new, "created_at": created}


def _status_counts(conn: duckdb.DuckDBPyConnection, table: str) -> dict[str, int]:
    rows = conn.execute(
        f"SELECT status, count(*) FROM {table} GROUP BY status"
    ).fetchall()
    return {status: n for status, n in rows}


def load_delta_counts(path: Path) -> dict[str, dict[str, int]]:
    """Per-section Added/Deleted/Modified(/Kept) counts for the overview line."""
    conn = duckdb.connect(str(path), read_only=True)
    try:
        return {
            "structure": _status_counts(conn, _STRUCTURE),
            "metrics": _status_counts(conn, _METRICS),
            "dimensions": _status_counts(conn, _DIMENSIONS),
        }
    finally:
        conn.close()


def load_metric_changes(path: Path) -> pl.DataFrame:
    """Changed metrics only (``Status <> 'Kept'``) for delta exploration.

    The persisted ``metric_changes`` table keeps every metric (incl. ``Kept``)
    for the full-catalogue xlsx sheet; the explorer wants just the changes.
    """
    conn = duckdb.connect(str(path), read_only=True)
    try:
        return conn.execute(
            f"SELECT * FROM {_METRICS} WHERE status <> 'Kept' ORDER BY status, metric_code"
        ).pl()
    finally:
        conn.close()


def load_member_changes(path: Path) -> pl.DataFrame:
    conn = duckdb.connect(str(path), read_only=True)
    try:
        return conn.execute(
            f"SELECT * FROM {_DIMENSIONS} ORDER BY status, dimension_code, member_code"
        ).pl()
    finally:
        conn.close()


def delta_perimeters(path: Path) -> list[str]:
    conn = duckdb.connect(str(path), read_only=True)
    try:
        rows = conn.execute(
            f"SELECT DISTINCT perimeter FROM {_STRUCTURE} "
            "WHERE perimeter <> '' ORDER BY perimeter"
        ).fetchall()
    finally:
        conn.close()
    return [r[0] for r in rows]


def load_structure_for_perimeter(path: Path, perimeter: str) -> pl.DataFrame:
    conn = duckdb.connect(str(path), read_only=True)
    try:
        return conn.execute(
            f"SELECT * FROM {_STRUCTURE} WHERE perimeter = ? "
            "ORDER BY type, template_code, subtemplate_code, "
            "row_code, column_code, status",
            [perimeter],
        ).pl()
    finally:
        conn.close()


def load_delta_tree(
    path: Path,
) -> list[tuple[str, list[tuple[str, list[tuple[str, dict[str, int]]]]]]]:
    """Nested change tree: perimeter → template → subtemplate with status counts.

    Only subtemplates carrying real changes (``Status <> 'Kept'``) are included,
    so the tree shows the delta rather than the whole model. Shape mirrors the
    DB explorer's ``load_db_tree``: ``[(perimeter, [(template_code,
    [(subtemplate_code, {status: n}), ...])])]``.
    """
    conn = duckdb.connect(str(path), read_only=True)
    try:
        rows = conn.execute(
            f"""
            SELECT perimeter, template_code, subtemplate_code, status, count(*) AS n
            FROM {_STRUCTURE}
            WHERE status <> 'Kept' AND subtemplate_code <> ''
            GROUP BY perimeter, template_code, subtemplate_code, status
            ORDER BY perimeter, template_code, subtemplate_code
            """
        ).fetchall()
    finally:
        conn.close()

    tree: list[tuple[str, list]] = []
    perim_idx: dict[str, list] = {}
    tmpl_idx: dict[tuple[str, str], list] = {}
    sub_idx: dict[tuple[str, str, str], dict[str, int]] = {}
    for perim, t_code, s_code, status, n in rows:
        templates = perim_idx.get(perim)
        if templates is None:
            templates = []
            perim_idx[perim] = templates
            tree.append((perim, templates))
        subs = tmpl_idx.get((perim, t_code))
        if subs is None:
            subs = []
            tmpl_idx[(perim, t_code)] = subs
            templates.append((t_code, subs))
        counts = sub_idx.get((perim, t_code, s_code))
        if counts is None:
            counts = {}
            sub_idx[(perim, t_code, s_code)] = counts
            subs.append((s_code, counts))
        counts[status] = n
    return tree


def load_cell_changes(path: Path, perimeter: str, subtemplate_code: str) -> pl.DataFrame:
    """Changed cells (``status <> 'Kept'``) for one perimeter+subtemplate."""
    conn = duckdb.connect(str(path), read_only=True)
    try:
        return conn.execute(
            f"""
            SELECT row_code, column_code, qname_old, qname_new,
                   metric_label_old, metric_label_new,
                   dimensions_old, dimensions_new,
                   status, type
            FROM {_STRUCTURE}
            WHERE perimeter = ? AND subtemplate_code = ? AND status <> 'Kept'
            ORDER BY type, row_code, column_code, status
            """,
            [perimeter, subtemplate_code],
        ).pl()
    finally:
        conn.close()


def load_apply_changes(path: Path, perimeter: str) -> pl.DataFrame:
    """Metric-cell structure changes for one perimeter, for XBRL apply-delta.

    Returns just the columns apply-delta needs to delete/rename metric facts:
    ``perimeter, type, status, qname_old, qname_new``.
    """
    conn = duckdb.connect(str(path), read_only=True)
    try:
        return conn.execute(
            f"""
            SELECT perimeter, type, status, qname_old, qname_new
            FROM {_STRUCTURE}
            WHERE lower(perimeter) = lower(?)
              AND type IN ('Row', 'Column', 'Matrix')
            """,
            [perimeter],
        ).pl()
    finally:
        conn.close()
