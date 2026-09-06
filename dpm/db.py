from __future__ import annotations

import re
from collections.abc import Sequence
from pathlib import Path

import duckdb
import polars as pl

from dpm._types import (
    DimensionMemberRow,
    DimensionRow,
    FactDimensionRow,
    FactRow,
    MetricRow,
    ModelVersionRow,
    PerimeterRow,
    PerimeterTemplateRow,
    StructureEntry,
    SubtemplateRow,
    TemplateRow,
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
    metric_code       VARCHAR PRIMARY KEY,
    metric_label      VARCHAR,
    data_type         VARCHAR,
    period_type       VARCHAR,
    balance           VARCHAR,
    referenced_domain VARCHAR
);

CREATE TABLE IF NOT EXISTS facts (
    subtemplate_code     VARCHAR REFERENCES subtemplates(subtemplate_code),
    row_code             VARCHAR,
    column_code          VARCHAR,
    row_label            VARCHAR,
    column_label         VARCHAR,
    metric_code          VARCHAR REFERENCES metrics(metric_code),
    data_point_signature VARCHAR,
    PRIMARY KEY (subtemplate_code, row_code, column_code)
);

CREATE TABLE IF NOT EXISTS dimensions (
    dimension_code      VARCHAR PRIMARY KEY,
    dimension_label     VARCHAR,
    default_member_code VARCHAR
);

CREATE TABLE IF NOT EXISTS dimension_members (
    member_code    VARCHAR PRIMARY KEY,
    dimension_code VARCHAR REFERENCES dimensions(dimension_code),
    member_label   VARCHAR,
    is_default     BOOLEAN
);

CREATE TABLE IF NOT EXISTS fact_dimensions (
    subtemplate_code VARCHAR,
    row_code         VARCHAR,
    column_code      VARCHAR,
    dimension_code   VARCHAR REFERENCES dimensions(dimension_code),
    member_code      VARCHAR REFERENCES dimension_members(member_code),
    PRIMARY KEY (subtemplate_code, row_code, column_code, dimension_code)
);

CREATE TABLE IF NOT EXISTS model_version (
    version   VARCHAR PRIMARY KEY,
    from_date VARCHAR,
    to_date   VARCHAR
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
        "model_version",
    }
)


def open_db(path: Path) -> duckdb.DuckDBPyConnection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = duckdb.connect(str(path))
    conn.execute(_DDL)
    return conn


def _bulk_upsert(conn: duckdb.DuckDBPyConnection, table: str, rows: list) -> None:
    """Insert rows via Arrow/Polars bulk transfer — orders of magnitude faster than executemany.

    Only the columns present in ``rows`` are written (named explicitly), so callers
    may omit nullable columns and let them default to NULL.
    """
    if not rows:
        return
    if table not in _ALLOWED_TABLES:
        raise ValueError(f"Unknown table: {table!r}")
    # ``infer_schema_length=None`` scans every row so a column that is null for the
    # first hundreds of rows but populated later (e.g. metric ``referenced_domain``)
    # is typed correctly rather than as Null.
    df = pl.DataFrame(rows, infer_schema_length=None)
    cols = ", ".join(df.columns)
    conn.execute(f"INSERT OR REPLACE INTO {table} ({cols}) SELECT {cols} FROM df")


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


def insert_model_version(
    conn: duckdb.DuckDBPyConnection, rows: list[ModelVersionRow]
) -> None:
    _bulk_upsert(conn, "model_version", rows)


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


# Shared fact projection; every loader prepends subtemplate_code so callers can
# attribute a row to its subtemplate (needed when facts are aggregated).
_FACT_SELECT = """
    SELECT
        f.subtemplate_code,
        f.row_code,
        f.column_code,
        f.row_label,
        f.column_label,
        f.metric_code,
        m.metric_label
    FROM facts f
    JOIN metrics m ON f.metric_code = m.metric_code
"""


def load_subtemplate_facts(
    conn: duckdb.DuckDBPyConnection, subtemplate_code: str
) -> list[tuple[str, str, str, str, str, str, str]]:
    """Facts for a single subtemplate, joined to their metric label."""
    return conn.execute(
        _FACT_SELECT
        + """
        WHERE f.subtemplate_code = ?
        ORDER BY f.row_code, f.column_code
        """,
        [subtemplate_code],
    ).fetchall()


def load_template_facts(
    conn: duckdb.DuckDBPyConnection, template_code: str
) -> list[tuple[str, str, str, str, str, str, str]]:
    """Facts across every subtemplate of one template."""
    return conn.execute(
        _FACT_SELECT
        + """
        JOIN subtemplates st ON f.subtemplate_code = st.subtemplate_code
        WHERE st.template_code = ?
        ORDER BY f.subtemplate_code, f.row_code, f.column_code
        """,
        [template_code],
    ).fetchall()


def load_perimeter_facts(
    conn: duckdb.DuckDBPyConnection, perimeter_code: str
) -> list[tuple[str, str, str, str, str, str, str]]:
    """Facts across every subtemplate reachable from one perimeter."""
    return conn.execute(
        _FACT_SELECT
        + """
        JOIN subtemplates st ON f.subtemplate_code = st.subtemplate_code
        JOIN perimeter_template pt ON st.template_code = pt.template_code
        WHERE pt.perimeter_code = ?
        ORDER BY f.subtemplate_code, f.row_code, f.column_code
        """,
        [perimeter_code],
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


_OPEN_DIM_RE = re.compile(r"([\w:]+)\(\*")


def load_open_dimensions(conn: duckdb.DuckDBPyConnection) -> list[str]:
    """Dimension codes that appear as an open/typed axis (``DIM(*…)``) in any DPS.

    An open axis carries no fixed member — the instance supplies a concrete value
    (e.g. a currency) that the model signature wildcards out — so apply-delta must
    drop these dimensions when reducing an instance fact to its canonical key.
    """
    open_dims: set[str] = set()
    for (dps,) in conn.execute(
        "SELECT DISTINCT data_point_signature FROM facts "
        "WHERE data_point_signature LIKE '%(*%'"
    ).fetchall():
        if dps:
            open_dims.update(m.strip() for m in _OPEN_DIM_RE.findall(dps))
    return sorted(open_dims)


def load_default_members(conn: duckdb.DuckDBPyConnection) -> list[str]:
    """Member codes flagged as their domain's default (omitted from instance contexts)."""
    return [
        r[0]
        for r in conn.execute(
            "SELECT member_code FROM dimension_members WHERE is_default"
        ).fetchall()
    ]


def load_entries(conn: duckdb.DuckDBPyConnection) -> list[StructureEntry]:
    rows = conn.execute("""
        SELECT pt.perimeter_code, pt.template_code, s.subtemplate_code
        FROM perimeter_template pt
        JOIN subtemplates s ON s.template_code = pt.template_code
        ORDER BY 1, 2, 3
    """).fetchall()
    return [
        StructureEntry(
            perimeter=perimeter_code,
            template_code=template_code,
            subtemplate_code=subtemplate_code,
        )
        for perimeter_code, template_code, subtemplate_code in rows
    ]


def filter_entries_by_perimeter(
    entries: Sequence[StructureEntry], selected: set[str]
) -> list[StructureEntry]:
    """Keep only entries whose perimeter (case-insensitively) is in ``selected``."""
    return [e for e in entries if e.perimeter.lower() in selected]
