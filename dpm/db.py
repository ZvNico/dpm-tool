from __future__ import annotations

from pathlib import Path

import duckdb
import polars as pl

from dpm._constants import METRIC_COLS
from dpm._types import (
    DimensionMemberRow,
    DimensionRow,
    FactDimensionRow,
    FactRow,
    MetricRow,
    PerimeterRow,
    PerimeterTemplateRow,
    SubtemplateRow,
    TemplateRow,
    TocEntry,
)

_DDL = """
CREATE TABLE IF NOT EXISTS templates (
    template_code VARCHAR PRIMARY KEY,
    template_label VARCHAR
);

CREATE TABLE IF NOT EXISTS subtemplates (
    subtemplate_code  VARCHAR PRIMARY KEY,
    template_code     VARCHAR REFERENCES templates(template_code),
    subtemplate_label VARCHAR,
    subtemplate_type  VARCHAR
);

CREATE TABLE IF NOT EXISTS perimeters (
    perimeter_code VARCHAR PRIMARY KEY
);

CREATE TABLE IF NOT EXISTS perimeter_template (
    perimeter_code VARCHAR REFERENCES perimeters(perimeter_code),
    template_code  VARCHAR REFERENCES templates(template_code),
    PRIMARY KEY (perimeter_code, template_code)
);

CREATE TABLE IF NOT EXISTS metrics (
    metric_code  VARCHAR PRIMARY KEY,
    metric_label VARCHAR
);

CREATE TABLE IF NOT EXISTS facts (
    subtemplate_code VARCHAR REFERENCES subtemplates(subtemplate_code),
    row_code         VARCHAR,
    column_code      VARCHAR,
    row_label        VARCHAR,
    column_label     VARCHAR,
    metric_code      VARCHAR REFERENCES metrics(metric_code),
    PRIMARY KEY (subtemplate_code, row_code, column_code)
);

CREATE TABLE IF NOT EXISTS dimensions (
    dimension_code  VARCHAR PRIMARY KEY,
    dimension_label VARCHAR
);

CREATE TABLE IF NOT EXISTS dimension_members (
    member_code    VARCHAR PRIMARY KEY,
    dimension_code VARCHAR REFERENCES dimensions(dimension_code),
    member_label   VARCHAR
);

CREATE TABLE IF NOT EXISTS fact_dimensions (
    subtemplate_code VARCHAR,
    row_code         VARCHAR,
    column_code      VARCHAR,
    dimension_code   VARCHAR REFERENCES dimensions(dimension_code),
    member_code      VARCHAR REFERENCES dimension_members(member_code),
    PRIMARY KEY (subtemplate_code, row_code, column_code, dimension_code)
);
"""

_ALLOWED_TABLES = frozenset(
    {
        "templates",
        "subtemplates",
        "perimeters",
        "perimeter_template",
        "metrics",
        "facts",
        "dimensions",
        "dimension_members",
        "fact_dimensions",
    }
)


def open_db(path: Path) -> duckdb.DuckDBPyConnection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = duckdb.connect(str(path))
    conn.execute(_DDL)
    return conn


def _bulk_upsert(conn: duckdb.DuckDBPyConnection, table: str, rows: list) -> None:
    """Insert rows via Arrow/Polars bulk transfer — orders of magnitude faster than executemany."""
    if not rows:
        return
    if table not in _ALLOWED_TABLES:
        raise ValueError(f"Unknown table: {table!r}")
    df = pl.DataFrame(rows)
    conn.execute(f"INSERT OR REPLACE INTO {table} SELECT * FROM df")


def insert_templates(conn: duckdb.DuckDBPyConnection, rows: list[TemplateRow]) -> None:
    _bulk_upsert(conn, "templates", rows)


def insert_subtemplates(
    conn: duckdb.DuckDBPyConnection, rows: list[SubtemplateRow]
) -> None:
    _bulk_upsert(conn, "subtemplates", rows)


def insert_perimeters(
    conn: duckdb.DuckDBPyConnection, rows: list[PerimeterRow]
) -> None:
    _bulk_upsert(conn, "perimeters", rows)


def insert_perimeter_template(
    conn: duckdb.DuckDBPyConnection, rows: list[PerimeterTemplateRow]
) -> None:
    _bulk_upsert(conn, "perimeter_template", rows)


def insert_metrics(conn: duckdb.DuckDBPyConnection, rows: list[MetricRow]) -> None:
    _bulk_upsert(conn, "metrics", rows)


def insert_facts(conn: duckdb.DuckDBPyConnection, rows: list[FactRow]) -> None:
    _bulk_upsert(conn, "facts", rows)


def insert_dimensions(
    conn: duckdb.DuckDBPyConnection, rows: list[DimensionRow]
) -> None:
    _bulk_upsert(conn, "dimensions", rows)


def insert_dimension_members(
    conn: duckdb.DuckDBPyConnection, rows: list[DimensionMemberRow]
) -> None:
    _bulk_upsert(conn, "dimension_members", rows)


def insert_fact_dimensions(
    conn: duckdb.DuckDBPyConnection, rows: list[FactDimensionRow]
) -> None:
    _bulk_upsert(conn, "fact_dimensions", rows)


_METRICS_DF_SQL = """
SELECT
    pt.perimeter_code  AS perimeter,
    st.template_code   AS template_code,
    f.subtemplate_code AS subtemplate_code,
    f.row_code         AS row_code,
    f.column_code      AS column_code,
    m.metric_code      AS qname,
    m.metric_label     AS metric_label,
    f.row_label        AS row_label,
    f.column_label     AS column_label
FROM facts f
JOIN subtemplates st      ON f.subtemplate_code = st.subtemplate_code
JOIN perimeter_template pt ON st.template_code  = pt.template_code
JOIN metrics m            ON f.metric_code      = m.metric_code
"""


def load_metrics_df(conn: duckdb.DuckDBPyConnection) -> pl.DataFrame:
    return conn.execute(_METRICS_DF_SQL).pl()


_STATS_TABLES = (
    "perimeters",
    "templates",
    "subtemplates",
    "metrics",
    "facts",
    "dimensions",
    "dimension_members",
)


def load_db_stats(conn: duckdb.DuckDBPyConnection) -> dict[str, int]:
    """Row counts per table, keyed by table name."""
    return {
        table: conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
        for table in _STATS_TABLES
    }


def load_db_tree(
    conn: duckdb.DuckDBPyConnection,
) -> list[tuple[str, list[tuple[str, str, list[tuple[str, str, str]]]]]]:
    """Nested structure for the explorer tree.

    Returns ``[(perimeter, [(template_code, template_label,
    [(subtemplate_code, subtemplate_label, subtemplate_type), ...])])]``,
    ordered by perimeter, template and subtemplate code.
    """
    rows = conn.execute("""
        SELECT
            pt.perimeter_code,
            t.template_code,
            t.template_label,
            s.subtemplate_code,
            s.subtemplate_label,
            s.subtemplate_type
        FROM perimeter_template pt
        JOIN templates t   ON t.template_code = pt.template_code
        JOIN subtemplates s ON s.template_code = pt.template_code
        ORDER BY pt.perimeter_code, t.template_code, s.subtemplate_code
    """).fetchall()

    tree: list[tuple[str, list]] = []
    perim_idx: dict[str, list] = {}
    tmpl_idx: dict[tuple[str, str], list] = {}
    for perim, t_code, t_label, s_code, s_label, s_type in rows:
        templates = perim_idx.get(perim)
        if templates is None:
            templates = []
            perim_idx[perim] = templates
            tree.append((perim, templates))
        subs = tmpl_idx.get((perim, t_code))
        if subs is None:
            subs = []
            tmpl_idx[(perim, t_code)] = subs
            templates.append((t_code, t_label, subs))
        subs.append((s_code, s_label, s_type))
    return tree


def load_subtemplate_facts(
    conn: duckdb.DuckDBPyConnection, subtemplate_code: str
) -> list[tuple[str, str, str, str, str, str]]:
    """Facts for a single subtemplate, joined to their metric label."""
    return conn.execute(
        """
        SELECT
            f.row_code,
            f.column_code,
            f.row_label,
            f.column_label,
            f.metric_code,
            m.metric_label
        FROM facts f
        JOIN metrics m ON f.metric_code = m.metric_code
        WHERE f.subtemplate_code = ?
        ORDER BY f.row_code, f.column_code
        """,
        [subtemplate_code],
    ).fetchall()


def load_fact_context(
    conn: duckdb.DuckDBPyConnection,
    subtemplate_code: str,
    row_code: str,
    column_code: str,
) -> list[tuple[str, str, str, str]]:
    """Dimensional context of a single fact: its dimensions and their members."""
    return conn.execute(
        """
        SELECT
            fd.dimension_code,
            d.dimension_label,
            fd.member_code,
            dm.member_label
        FROM fact_dimensions fd
        LEFT JOIN dimensions d        ON d.dimension_code = fd.dimension_code
        LEFT JOIN dimension_members dm ON dm.member_code  = fd.member_code
        WHERE fd.subtemplate_code = ? AND fd.row_code = ? AND fd.column_code = ?
        ORDER BY fd.dimension_code
        """,
        [subtemplate_code, row_code, column_code],
    ).fetchall()


def load_metrics(
    conn: duckdb.DuckDBPyConnection,
) -> list[tuple[str, str]]:
    """The full metric catalogue as ``(metric_code, metric_label)``."""
    return conn.execute(
        "SELECT metric_code, metric_label FROM metrics ORDER BY metric_code"
    ).fetchall()


def load_metric_usage(
    conn: duckdb.DuckDBPyConnection, metric_code: str
) -> list[tuple[str, str, str, str, str]]:
    """Facts that reference a given metric."""
    return conn.execute(
        """
        SELECT subtemplate_code, row_code, column_code, row_label, column_label
        FROM facts
        WHERE metric_code = ?
        ORDER BY subtemplate_code, row_code, column_code
        """,
        [metric_code],
    ).fetchall()


def load_dimensions_with_members(
    conn: duckdb.DuckDBPyConnection,
) -> list[tuple[str, str, list[tuple[str, str]]]]:
    """Dimensions each with their members.

    Returns ``[(dimension_code, dimension_label, [(member_code, member_label), ...])]``,
    ordered by dimension then member code.
    """
    rows = conn.execute("""
        SELECT
            d.dimension_code,
            d.dimension_label,
            dm.member_code,
            dm.member_label
        FROM dimensions d
        LEFT JOIN dimension_members dm ON dm.dimension_code = d.dimension_code
        ORDER BY d.dimension_code, dm.member_code
    """).fetchall()

    dims: list[tuple[str, str, list]] = []
    idx: dict[str, list] = {}
    for d_code, d_label, m_code, m_label in rows:
        members = idx.get(d_code)
        if members is None:
            members = []
            idx[d_code] = members
            dims.append((d_code, d_label, members))
        if m_code is not None:
            members.append((m_code, m_label))
    return dims


def load_fact_dimensions_df(conn: duckdb.DuckDBPyConnection) -> pl.DataFrame:
    """One row per fact with its dimensional context serialized as ``dim=member;…``.

    Columns: ``subtemplate_code, row_code, column_code, dimensions`` — the
    ``dimensions`` string is sorted by dimension code so it is order-stable and
    can be compared directly between versions.
    """
    return conn.execute("""
        SELECT
            subtemplate_code,
            row_code,
            column_code,
            string_agg(dimension_code || '=' || member_code, ';' ORDER BY dimension_code)
                AS dimensions
        FROM fact_dimensions
        GROUP BY subtemplate_code, row_code, column_code
    """).pl()


def load_dimension_members_df(conn: duckdb.DuckDBPyConnection) -> pl.DataFrame:
    """Flat dimensions × members frame for the dimensions delta.

    Columns: ``dimension_code, dimension_label, member_code, member_label``.
    Dimensions with no members still appear once with a null ``member_code``.
    """
    return conn.execute("""
        SELECT
            d.dimension_code,
            d.dimension_label,
            dm.member_code,
            dm.member_label
        FROM dimensions d
        LEFT JOIN dimension_members dm ON dm.dimension_code = d.dimension_code
        ORDER BY d.dimension_code, dm.member_code
    """).pl()


def load_entries(conn: duckdb.DuckDBPyConnection) -> list[TocEntry]:
    rows = conn.execute("""
        SELECT pt.perimeter_code, pt.template_code, s.subtemplate_code
        FROM perimeter_template pt
        JOIN subtemplates s ON s.template_code = pt.template_code
        ORDER BY 1, 2, 3
    """).fetchall()
    return [
        TocEntry(
            perimeter=perimeter_code,
            template_code=template_code,
            subtemplate_code=subtemplate_code,
        )
        for perimeter_code, template_code, subtemplate_code in rows
    ]
