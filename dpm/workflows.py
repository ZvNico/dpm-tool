from __future__ import annotations

import logging
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

import duckdb
import polars as pl

from dpm._constants import DELTA_DIR
from dpm._types import ApplyStats, DpmDataset
from dpm.db import (
    insert_dimension_members,
    insert_dimensions,
    insert_fact_dimensions,
    insert_facts,
    insert_metrics,
    insert_model_version,
    insert_perimeter_template,
    insert_perimeters,
    insert_subtemplates,
    insert_templates,
    load_db_stats,
    load_db_tree,
    load_default_members,
    load_dimension_members_df,
    load_open_dimensions,
    load_dimensions_with_members,
    load_entries,
    filter_entries_by_perimeter,
    load_fact_context,
    load_fact_dimensions_df,
    load_metric_usage,
    load_metrics,
    load_metrics_df,
    load_perimeter_facts,
    load_subtemplate_facts,
    load_template_facts,
    open_db,
)
from dpm.delta import compare_versions
from dpm.dpm_source import parse_dpm_database
from dpm.delta_db import (
    delta_db_path,
    load_delta_result,
    save_delta_db,
)
from dpm.excel import generate_delta_workbook
from dpm.xbrl import apply_delta

LOG = logging.getLogger(__name__)

_VERSION_RE = re.compile(r"_(\d+\.\d+(?:\.\d+)*)_")


def detect_version(path: Path) -> str | None:
    m = _VERSION_RE.search(path.name)
    return m.group(1) if m else None


def version_key(version: str) -> tuple[int, ...]:
    """Sort key for dotted numeric DPM versions, e.g. '2.10.0' > '2.8.2'."""
    parts: list[int] = []
    for chunk in version.split("."):
        parts.append(int(chunk) if chunk.isdigit() else 0)
    return tuple(parts)


def available_db_versions(db_dir: Path) -> list[str]:
    """Return DPM versions with an ingested DuckDB in ``db_dir``, oldest-first."""
    if not db_dir.is_dir():
        return []
    return sorted((p.stem for p in db_dir.glob("*.duckdb")), key=version_key)


def _load_dataset(db_path: Path, selected_perimeters: set[str]) -> DpmDataset:
    """Load a filtered dataset from an ingested DuckDB."""
    LOG.info("Loading dataset from DuckDB: %s", db_path.name)
    conn = duckdb.connect(str(db_path), read_only=True)
    try:
        entries_all = load_entries(conn)
        entries = filter_entries_by_perimeter(entries_all, selected_perimeters)
        fact_dims = load_fact_dimensions_df(conn)
        metrics = (
            load_metrics_df(conn)
            .filter(pl.col("perimeter").str.to_lowercase().is_in(selected_perimeters))
            .join(
                fact_dims,
                on=["subtemplate_code", "row_code", "column_code"],
                how="left",
            )
            .with_columns(pl.col("dimensions").fill_null(""))
        )
        # Catalogues are version-level (not perimeter-scoped), compared in full.
        metric_catalog = conn.execute(
            "SELECT metric_code, metric_label FROM metrics"
        ).pl()
        dimension_members = load_dimension_members_df(conn)
        return DpmDataset(
            metrics=metrics,
            entries=entries,
            metric_catalog=metric_catalog,
            dimension_members=dimension_members,
        )
    finally:
        conn.close()


def _build_datapoint_changes(structure: pl.DataFrame) -> pl.DataFrame:
    """Resolve the cell-keyed structure delta into the per-perimeter DPS pivot.

    Runs the same resolution apply used to do at load time (:func:`build_dps_delta`),
    once per perimeter, so the delta DB persists the apply-ready index directly.
    """
    from dpm.delta_schema import DATAPOINT_CHANGE_COLS
    from dpm.xbrl import build_dps_delta, dps_delta_to_rows

    schema = {c: pl.String for c in DATAPOINT_CHANGE_COLS}
    if structure.is_empty() or "perimeter" not in structure.columns:
        return pl.DataFrame(schema=schema)
    cells = structure.filter(pl.col("type").is_in(["Row", "Column", "Matrix"]))
    rows: list[dict[str, str]] = []
    for perim in sorted(cells["perimeter"].unique().to_list()):
        sub = cells.filter(pl.col("perimeter") == perim)
        rows.extend(dps_delta_to_rows(perim, build_dps_delta(sub)))
    return pl.DataFrame(rows, schema=schema) if rows else pl.DataFrame(schema=schema)


def _load_apply_context(old_db: Path, new_db: Path) -> tuple[list[str], list[str]]:
    """Union of open dimensions and default members across two ingested versions."""
    open_dims: set[str] = set()
    default_members: set[str] = set()
    for db_path in (old_db, new_db):
        conn = duckdb.connect(str(db_path), read_only=True)
        try:
            open_dims.update(load_open_dimensions(conn))
            default_members.update(load_default_members(conn))
        finally:
            conn.close()
    return sorted(open_dims), sorted(default_members)


def load_perimeters(old_db: Path, new_db: Path) -> list[str]:
    """Return the sorted union of perimeters from two ingested DuckDBs."""
    def perimeters(db_path: Path) -> set[str]:
        conn = duckdb.connect(str(db_path), read_only=True)
        try:
            return {
                r[0]
                for r in conn.execute("SELECT perimeter_code FROM perimeters").fetchall()
            }
        finally:
            conn.close()

    return sorted(perimeters(old_db) | perimeters(new_db))


def delta_output_name(perimeters: Sequence[str], *, all_selected: bool) -> str:
    """Filename for a delta workbook, encoding its perimeter coverage.

    ``all_selected`` collapses to a single ``_all`` tag; otherwise each perimeter
    is appended in lowercase, underscore-separated (e.g. ``Delta_Dpm_qrs_ars.xlsx``).
    """
    if all_selected:
        return "Delta_Dpm_all.xlsx"
    suffix = "_".join(sorted(p.lower() for p in perimeters))
    return f"Delta_Dpm_{suffix}.xlsx"


@dataclass(frozen=True)
class DbContents:
    """Everything the explorer loads up front when a DB is opened."""

    stats: dict[str, int]
    tree: list
    metrics: list[tuple[str, str]]
    dimensions: list[tuple[str, str, list[tuple[str, str]]]]


def load_db_contents(db_path: Path) -> DbContents:
    """Open an ingested DuckDB read-only and load the explorer's browse data."""
    conn = duckdb.connect(str(db_path), read_only=True)
    try:
        return DbContents(
            stats=load_db_stats(conn),
            tree=load_db_tree(conn),
            metrics=load_metrics(conn),
            dimensions=load_dimensions_with_members(conn),
        )
    finally:
        conn.close()


def load_facts(db_path: Path, kind: str, code: str) -> list[tuple]:
    """Open an ingested DuckDB read-only and return facts for a tree node.

    ``kind`` is ``"subtemplate"``, ``"template"`` or ``"perimeter"``; ``code`` is
    the matching identifier. Rows are 7-tuples with subtemplate_code first.
    """
    loaders = {
        "subtemplate": load_subtemplate_facts,
        "template": load_template_facts,
        "perimeter": load_perimeter_facts,
    }
    loader = loaders[kind]
    conn = duckdb.connect(str(db_path), read_only=True)
    try:
        return loader(conn, code)
    finally:
        conn.close()


def load_fact_dimensions(
    db_path: Path, subtemplate_code: str, row_code: str, column_code: str
) -> list[tuple]:
    """Open an ingested DuckDB read-only and return one fact's dimensional context."""
    conn = duckdb.connect(str(db_path), read_only=True)
    try:
        return load_fact_context(conn, subtemplate_code, row_code, column_code)
    finally:
        conn.close()


def load_facts_for_metric(db_path: Path, metric_code: str) -> list[tuple]:
    """Open an ingested DuckDB read-only and return facts that use a metric."""
    conn = duckdb.connect(str(db_path), read_only=True)
    try:
        return load_metric_usage(conn, metric_code)
    finally:
        conn.close()


_COMPARE_STEPS = 7


def run_delta(
    old_db: Path,
    new_db: Path,
    output_path: Path,
    selected_perimeters: set[str],
    on_progress: Callable[[str, int, int, str], None] | None = None,
) -> None:
    """Compare two ingested DuckDBs and write a delta Excel workbook.

    ``on_progress(phase, done, total, label)`` is invoked throughout, where ``phase`` is
    one of ``load_old`` / ``load_new`` / ``compare`` / ``write`` and ``done``/``total``
    track inner steps within that phase (comparison steps, sheets written).
    """
    def emit(phase: str, done: int, total: int, label: str) -> None:
        if on_progress:
            on_progress(phase, done, total, label)

    def load(db_path: Path, phase: str, human: str) -> DpmDataset:
        emit(phase, 0, 1, f"Loading {human} version…")
        dataset = _load_dataset(db_path, selected_perimeters)
        emit(phase, 1, 1, f"Loaded {human} version")
        return dataset

    old_dataset = load(old_db, "load_old", "old")
    new_dataset = load(new_db, "load_new", "new")

    step = 0

    def compare_step(label: str) -> None:
        nonlocal step
        step += 1
        emit("compare", step, _COMPARE_STEPS, label)

    delta = compare_versions(old_dataset, new_dataset, on_step=compare_step)

    emit("write", 0, 1, "Writing workbook…")

    def write_sheet(idx: int, total: int, name: str) -> None:
        emit("write", idx, total, f"Writing sheet {idx}/{total}: {name}")

    generate_delta_workbook(delta, output_path, on_sheet=write_sheet)
    emit("write", 1, 1, "Done!")
    LOG.info("Delta workbook generated: %s", output_path)


def ensure_delta_db(
    old_db: Path,
    new_db: Path,
    delta_dir: Path,
    on_step: Callable[[str], None] | None = None,
) -> Path:
    """Return the cached delta DuckDB for two versions, computing it if missing.

    The version identifier is the source DB's file stem (e.g. ``2.10.0``). The
    delta is computed across *all* perimeters so the cached artifact is complete;
    consumers (xlsx render, explorer) filter as needed.
    """
    def _step(label: str) -> None:
        if on_step:
            on_step(label)

    out_path = delta_db_path(delta_dir, old_db.stem, new_db.stem)
    if out_path.exists():
        _step("Using cached delta database")
        return out_path

    _step("Loading old version…")
    selected = {p.lower() for p in load_perimeters(old_db, new_db)}
    old_ds = _load_dataset(old_db, selected)
    _step("Loading new version…")
    new_ds = _load_dataset(new_db, selected)
    _step("Comparing versions…")
    result = compare_versions(old_ds, new_ds, on_step=on_step)
    _step("Saving delta database…")
    open_dims, default_members = _load_apply_context(old_db, new_db)
    save_delta_db(
        result,
        out_path,
        old_db.stem,
        new_db.stem,
        open_dimensions=open_dims,
        default_members=default_members,
        datapoint_changes=_build_datapoint_changes(result.structure),
    )
    LOG.info("Delta database written: %s", out_path)
    return out_path


def render_delta_xlsx(
    delta_path: Path,
    output_path: Path,
    on_sheet: Callable[[int, int, str], None] | None = None,
) -> None:
    """Render an Excel workbook from a cached delta DuckDB."""
    result = load_delta_result(delta_path)
    generate_delta_workbook(result, output_path, on_sheet=on_sheet)
    LOG.info("Delta workbook generated: %s", output_path)


_DPM_DB_SUFFIXES = frozenset({".db", ".sqlite", ".sqlite3"})

# Fixed number of steps run_ingest emits, driving its progress %:
# 1 "Reading DPM database…" + 9 parse steps + 10 inserts + model-version + commit.
_INGEST_STEPS = 22


def run_ingest(
    source_path: Path,
    version: str,
    db_dir: Path,
    on_progress: Callable[[int, int, str], None] | None = None,
) -> dict:
    """Ingest the official EIOPA DPM SQLite database into DuckDB and return a stats dict.

    The DPM database (:mod:`dpm.dpm_source`) is the sole ingestion source; the file
    must be a ``.db``/``.sqlite`` SQLite database. ``on_progress(done, total, label)``
    is invoked per step (including the parse's read steps) to drive a progress bar.
    """

    done = 0

    def _step(label: str) -> None:
        nonlocal done
        done += 1
        if on_progress:
            on_progress(min(done, _INGEST_STEPS), _INGEST_STEPS, label)

    if source_path.suffix.lower() not in _DPM_DB_SUFFIXES:
        raise ValueError(
            f"Ingest source must be a DPM SQLite database "
            f"({', '.join(sorted(_DPM_DB_SUFFIXES))}); got {source_path.name!r}"
        )
    _step("Reading DPM database…")
    parsed = parse_dpm_database(source_path, version, on_step=_step)

    db_path = db_dir / f"{version}.duckdb"
    conn = open_db(db_path)
    try:
        conn.begin()
        _step("Inserting templates…")
        insert_templates(conn, parsed.templates)
        _step("Inserting subtemplates…")
        insert_subtemplates(conn, parsed.subtemplates)
        _step("Inserting perimeters…")
        insert_perimeters(conn, parsed.perimeters)
        _step("Inserting perimeter–template map…")
        insert_perimeter_template(conn, parsed.perimeter_template)
        _step("Inserting metrics…")
        insert_metrics(conn, parsed.metrics)
        _step("Inserting facts…")
        insert_facts(conn, parsed.facts)
        _step("Inserting dimensions…")
        insert_dimensions(conn, parsed.dimensions)
        _step("Inserting dimension members…")
        insert_dimension_members(conn, parsed.dimension_members)
        _step("Inserting fact–dimension map…")
        insert_fact_dimensions(conn, parsed.fact_dimensions)
        if parsed.model_version:
            _step("Inserting model version…")
            insert_model_version(conn, parsed.model_version)
        _step("Committing…")
        conn.commit()
    finally:
        conn.close()

    return {
        "db_path": db_path,
        "templates": len(parsed.templates),
        "perimeters": len(parsed.perimeters),
        "metrics": len(parsed.metrics),
        "facts": len(parsed.facts),
        "dimensions": len(parsed.dimensions),
        "dimension_members": len(parsed.dimension_members),
        "fact_dimensions": len(parsed.fact_dimensions),
    }


def run_apply_delta(
    old_db: Path,
    new_db: Path,
    input_xbrl: Path,
    output_xbrl: Path,
    delta_dir: Path = DELTA_DIR,
    perimeter_override: str | None = None,
    dry_run: bool = False,
    debug_xlsx: Path | None = None,
    on_step: Callable[[str], None] | None = None,
) -> ApplyStats:
    """Apply the delta between two ingested DB versions to an XBRL file.

    Resolves (computing if needed) the cached delta DuckDB for ``old_db``/``new_db``
    — the source of truth — then rewrites the XBRL from it.
    """
    delta_path = ensure_delta_db(old_db, new_db, delta_dir, on_step=on_step)
    return apply_delta(
        delta_path=delta_path,
        input_xbrl=input_xbrl,
        output_xbrl=output_xbrl,
        perimeter_override=perimeter_override,
        dry_run=dry_run,
        debug_xlsx=debug_xlsx,
    )
