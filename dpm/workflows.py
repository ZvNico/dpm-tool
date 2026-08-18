from __future__ import annotations

import logging
import re
import warnings
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

import duckdb
import polars as pl

from dpm._constants import DELTA_DIR
from dpm._types import ApplyStats, DpmDataset, WorkbookCache
from dpm.db import (
    insert_dimension_members,
    insert_dimensions,
    insert_fact_dimensions,
    insert_facts,
    insert_metrics,
    insert_perimeter_template,
    insert_perimeters,
    insert_subtemplates,
    insert_templates,
    load_db_stats,
    load_db_tree,
    load_dimension_members_df,
    load_dimensions_with_members,
    load_entries,
    load_fact_context,
    load_fact_dimensions_df,
    load_metric_usage,
    load_metrics,
    load_metrics_df,
    load_subtemplate_facts,
    open_db,
)
from dpm.delta import compare_versions
from dpm.delta_db import (
    delta_db_path,
    load_delta_result,
    save_delta_db,
)
from dpm.excel import generate_delta_workbook
from dpm.parser import parse_workbook
from dpm.toc import extract_toc_perimeters, filter_entries_by_perimeter
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


def _open_xlsx(path: Path) -> WorkbookCache:
    warnings.filterwarnings(
        "ignore",
        message="Data Validation extension is not supported and will be removed",
        category=UserWarning,
        module="openpyxl.worksheet._reader",
    )
    return WorkbookCache.open(path)


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


def load_facts(db_path: Path, subtemplate_code: str) -> list[tuple]:
    """Open an ingested DuckDB read-only and return one subtemplate's facts."""
    conn = duckdb.connect(str(db_path), read_only=True)
    try:
        return load_subtemplate_facts(conn, subtemplate_code)
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
    save_delta_db(result, out_path, old_db.stem, new_db.stem)
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


def run_ingest(
    workbook_path: Path,
    version: str,
    db_dir: Path,
    on_sheet: Callable[[int, int, str], None] | None = None,
    on_step: Callable[[str], None] | None = None,
) -> dict:
    """Parse a DPM workbook and persist all data to DuckDB. Returns stats dict."""

    def _step(label: str) -> None:
        if on_step:
            on_step(label)

    workbook = _open_xlsx(workbook_path)
    try:
        toc_perimeters = extract_toc_perimeters(workbook)
        parsed = parse_workbook(workbook, toc_perimeters, on_sheet=on_sheet)
    finally:
        workbook.close()

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
