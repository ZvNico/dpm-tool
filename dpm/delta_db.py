"""Persist a computed delta between two DPM versions as a cached DuckDB artifact.

A delta DB lives at ``{delta_dir}/{old}_to_{new}.duckdb``. It is the canonical,
queryable source of truth and holds:

* ``structure_changes`` / ``metric_changes`` / ``member_changes`` — the human-facing,
  cell-keyed delta sections the Excel workbook and the Explore-Delta screen render.
* ``datapoint_changes`` — the resolved, per-metric **DPS pivot** (survivor / modified /
  deleted fixed signatures) that XBRL apply reads directly, per perimeter.
* ``open_dimensions`` / ``default_members`` — the apply context (unions across both
  versions) used to reduce an instance fact to its canonical signature.
* a ``meta`` row (old/new version, created-at).
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import duckdb
import polars as pl

from dpm._types import DeltaResult
from dpm.delta_schema import (
    DATAPOINT_CHANGE_COLS,
    DELTA_DIMENSION_COLS,
    DELTA_METRIC_COLS,
    DELTA_STRUCTURE_COLS,
)

# table name → its column list (order-stable, all strings)
_STRUCTURE = "structure_changes"
_METRICS = "metric_changes"
_DIMENSIONS = "member_changes"
_DATAPOINT = "datapoint_changes"
_OPEN_DIMENSIONS = "open_dimensions"
_DEFAULT_MEMBERS = "default_members"
_TABLE_COLS = {
    _STRUCTURE: DELTA_STRUCTURE_COLS,
    _METRICS: DELTA_METRIC_COLS,
    _DIMENSIONS: DELTA_DIMENSION_COLS,
    _DATAPOINT: DATAPOINT_CHANGE_COLS,
}


# A "label-only" change is a Modified cell whose qname and dimensions are identical —
# only its metric/row/column label differs (this includes qname-move rows). Appending
# this predicate hides them so the explorer can suppress label-noise on demand.
_EXCLUDE_LABEL_ONLY = (
    "AND NOT (status = 'Modified' "
    "AND qname_old = qname_new AND dimensions_old = dimensions_new)"
)


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


def _save_code_table(
    conn: duckdb.DuckDBPyConnection, name: str, column: str, codes: list[str]
) -> None:
    conn.execute(f"CREATE TABLE {name} ({column} VARCHAR)")
    if codes:
        frame = pl.DataFrame({column: sorted(set(codes))})
        conn.register("_tmp_codes", frame.to_arrow())
        conn.execute(f"INSERT INTO {name} SELECT * FROM _tmp_codes")
        conn.unregister("_tmp_codes")


def save_delta_db(
    result: DeltaResult,
    out_path: Path,
    old_version: str,
    new_version: str,
    open_dimensions: list[str] | None = None,
    default_members: list[str] | None = None,
    datapoint_changes: pl.DataFrame | None = None,
) -> None:
    """Write a :class:`DeltaResult` to a fresh delta DuckDB (overwrites).

    ``open_dimensions`` and ``default_members`` (unions across both versions) are
    stored alongside the change tables so apply-delta can reduce an instance fact
    to its canonical Data Point Signature key without reopening the version DBs.
    ``datapoint_changes`` is the resolved, per-metric DPS pivot (built once here) that
    apply reads directly instead of re-deriving from ``structure_changes``.
    """
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
        _save_table(
            conn,
            _DATAPOINT,
            datapoint_changes
            if datapoint_changes is not None
            else pl.DataFrame(schema={c: pl.String for c in DATAPOINT_CHANGE_COLS}),
        )
        _save_code_table(conn, _OPEN_DIMENSIONS, "dimension_code", open_dimensions or [])
        _save_code_table(conn, _DEFAULT_MEMBERS, "member_code", default_members or [])
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


def _status_counts(
    conn: duckdb.DuckDBPyConnection, table: str, extra_where: str = ""
) -> dict[str, int]:
    rows = conn.execute(
        f"SELECT status, count(*) FROM {table} WHERE 1=1 {extra_where} GROUP BY status"
    ).fetchall()
    return {status: n for status, n in rows}


def load_delta_counts(path: Path, include_labels: bool = True) -> dict[str, dict[str, int]]:
    """Per-section Added/Deleted/Modified(/Kept) counts for the overview line.

    When ``include_labels`` is False, label-only cell changes are excluded from the
    ``structure`` count (the metric/dimension catalogues are unaffected).
    """
    struct_where = "" if include_labels else _EXCLUDE_LABEL_ONLY
    conn = duckdb.connect(str(path), read_only=True)
    try:
        return {
            "structure": _status_counts(conn, _STRUCTURE, struct_where),
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
    include_labels: bool = True,
) -> list[
    tuple[str, list[tuple[str, str, list[tuple[str, str, dict[str, int]]]]]]
]:
    """Nested change tree: perimeter → template → subtemplate with status counts.

    Only nodes carrying real changes (``Status <> 'Kept'``) are shown, so the tree
    shows the delta rather than the whole model. Each template and subtemplate
    carries a ``struct_status`` string derived from its *actual content*, not from
    the ``Template``/``SubTemplate`` marker rows:

    * A subtemplate is ``"Deleted"`` only when the deleted cells are the complete
      set of cells it has in the old DB — i.e. nothing survives (no ``Kept``,
      ``Modified`` or ``Added`` fact rows remain). It is ``"Added"`` when every
      cell is ``Added`` (wholly new), and ``""`` otherwise.
    * A template is ``"Deleted"`` only when *all* of its shown subtemplates are
      ``"Deleted"``, ``"Added"`` when all are ``"Added"``, and ``""`` otherwise.

    Completeness is decided over every fact row (incl. ``Kept`` and label-only
    ``Modified``, regardless of ``include_labels``) so a hidden survivor still
    keeps a node out of the red set. The displayed per-status ``counts`` exclude
    ``Kept`` (and label-only rows when ``include_labels`` is False), matching what
    ``load_cell_changes`` shows. Shape: ``[(perimeter, [(template_code,
    struct_status, [(subtemplate_code, struct_status, {status: n}), ...])])]``.
    """
    label_filter = "" if include_labels else _EXCLUDE_LABEL_ONLY
    conn = duckdb.connect(str(path), read_only=True)
    try:
        # Fact cells only; markers (Template/SubTemplate) no longer drive coloring.
        # n_all counts every row for the completeness/red decision; n_disp is the
        # display count (non-Kept, label-filtered) used for the badge.
        rows = conn.execute(
            f"""
            SELECT perimeter, template_code, subtemplate_code, status,
                   count(*) AS n_all,
                   count(*) FILTER (
                       WHERE status <> 'Kept' {label_filter}
                   ) AS n_disp
            FROM {_STRUCTURE}
            WHERE type NOT IN ('Template', 'SubTemplate')
            GROUP BY perimeter, template_code, subtemplate_code, status
            ORDER BY perimeter, template_code, subtemplate_code
            """
        ).fetchall()
    finally:
        conn.close()

    def _content_status(per_status_all: dict[str, int]) -> str:
        """Red/green only when a single status accounts for every cell."""
        total = sum(per_status_all.values())
        if total <= 0:
            return ""
        if per_status_all.get("Deleted", 0) == total:
            return "Deleted"
        if per_status_all.get("Added", 0) == total:
            return "Added"
        return ""

    # Accumulate per (perim, template, subtemplate): full status counts (for the
    # completeness decision) and display counts (for the badge).
    sub_all: dict[tuple[str, str, str], dict[str, int]] = {}
    sub_disp: dict[tuple[str, str, str], dict[str, int]] = {}
    order: list[tuple[str, str, str]] = []
    for perim, t_code, s_code, status, n_all, n_disp in rows:
        key = (perim, t_code, s_code)
        if key not in sub_all:
            sub_all[key] = {}
            sub_disp[key] = {}
            order.append(key)
        if n_all:
            sub_all[key][status] = sub_all[key].get(status, 0) + n_all
        if n_disp:
            sub_disp[key][status] = sub_disp[key].get(status, 0) + n_disp

    tree: list[tuple[str, list]] = []
    perim_idx: dict[str, list] = {}
    tmpl_idx: dict[tuple[str, str], list] = {}
    for perim, t_code, s_code in order:
        counts = sub_disp[(perim, t_code, s_code)]
        # Only subtemplates with a real (shown) change appear in the tree.
        if not counts:
            continue
        s_struct = _content_status(sub_all[(perim, t_code, s_code)])
        templates = perim_idx.get(perim)
        if templates is None:
            templates = []
            perim_idx[perim] = templates
            tree.append((perim, templates))
        tmpl = tmpl_idx.get((perim, t_code))
        if tmpl is None:
            tmpl = [t_code, "", []]
            tmpl_idx[(perim, t_code)] = tmpl
            templates.append(tmpl)
        tmpl[2].append((s_code, s_struct, counts))

    def _rollup(sub_statuses: list[str]) -> str:
        if sub_statuses and all(s == "Deleted" for s in sub_statuses):
            return "Deleted"
        if sub_statuses and all(s == "Added" for s in sub_statuses):
            return "Added"
        return ""

    return [
        (
            perim,
            [
                (t[0], _rollup([s[1] for s in t[2]]), t[2])
                for t in templates
            ],
        )
        for perim, templates in tree
    ]


def load_cell_changes(
    path: Path,
    perimeter: str,
    subtemplate_code: str | None = None,
    include_labels: bool = True,
    template_code: str | None = None,
) -> pl.DataFrame:
    """Changed cells (``status <> 'Kept'``) scoped to a tree node.

    Always filters by ``perimeter``; additionally narrows to one ``template_code``
    or one ``subtemplate_code`` when given, so the caller can view a whole
    perimeter, a template, or a single subtemplate. When ``include_labels`` is
    False, label-only changes are excluded.
    """
    label_filter = "" if include_labels else _EXCLUDE_LABEL_ONLY
    scope = "perimeter = ?"
    params: list[str] = [perimeter]
    if subtemplate_code is not None:
        scope += " AND subtemplate_code = ?"
        params.append(subtemplate_code)
    elif template_code is not None:
        scope += " AND template_code = ?"
        params.append(template_code)
    conn = duckdb.connect(str(path), read_only=True)
    try:
        return conn.execute(
            f"""
            SELECT subtemplate_code, row_code, column_code, qname_old, qname_new,
                   metric_label_old, metric_label_new,
                   row_label_old, row_label_new,
                   column_label_old, column_label_new,
                   dimensions_old, dimensions_new,
                   status, type
            FROM {_STRUCTURE}
            WHERE {scope} AND status <> 'Kept'
              AND type NOT IN ('Template', 'SubTemplate') {label_filter}
            ORDER BY subtemplate_code, type, row_code, column_code, status
            """,
            params,
        ).pl()
    finally:
        conn.close()


def load_apply_context(path: Path) -> tuple[frozenset[str], frozenset[str]]:
    """Return ``(open_dimensions, default_members)`` stored with the delta.

    Older delta DBs predate these tables; they yield empty sets (apply then keys on
    the full member context, which still matches when instances omit defaults).
    """
    conn = duckdb.connect(str(path), read_only=True)
    try:
        def _codes(table: str, column: str) -> frozenset[str]:
            try:
                return frozenset(
                    r[0] for r in conn.execute(f"SELECT {column} FROM {table}").fetchall()
                )
            except duckdb.CatalogException:
                return frozenset()

        return (
            _codes(_OPEN_DIMENSIONS, "dimension_code"),
            _codes(_DEFAULT_MEMBERS, "member_code"),
        )
    finally:
        conn.close()


def load_datapoint_changes(path: Path, perimeter: str) -> pl.DataFrame | None:
    """Resolved DPS pivot rows for one perimeter, or ``None`` on a pre-pivot delta DB.

    ``None`` signals apply to fall back to deriving the index from ``structure_changes``
    (:func:`dpm.xbrl.build_dps_delta`), so older cached deltas keep working.
    """
    conn = duckdb.connect(str(path), read_only=True)
    try:
        return conn.execute(
            f"SELECT * FROM {_DATAPOINT} WHERE lower(perimeter) = lower(?)",
            [perimeter],
        ).pl()
    except duckdb.CatalogException:
        return None
    finally:
        conn.close()


def load_apply_changes(path: Path, perimeter: str) -> pl.DataFrame:
    """Metric-cell structure changes for one perimeter, for XBRL apply-delta.

    Returns the columns apply-delta needs to patch the instance:
    ``perimeter, type, status, qname_old, qname_new`` to delete/rename metric
    facts, plus ``dimensions_old, dimensions_new`` (each a ``s2c_dim:XX=s2c_YY:zN;…``
    string) to rewrite a fact's dimensional members when they were swapped.
    """
    conn = duckdb.connect(str(path), read_only=True)
    try:
        return conn.execute(
            f"""
            SELECT perimeter, type, status, qname_old, qname_new,
                   dimensions_old, dimensions_new
            FROM {_STRUCTURE}
            WHERE lower(perimeter) = lower(?)
              AND type IN ('Row', 'Column', 'Matrix')
            """,
            [perimeter],
        ).pl()
    finally:
        conn.close()
