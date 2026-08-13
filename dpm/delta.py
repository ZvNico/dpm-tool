from __future__ import annotations

import logging
from collections.abc import Callable

import polars as pl

from dpm._constants import DEFAULT_COLUMN_CODE, KEY_COLS, METRIC_COLS
from dpm._types import DeltaResult, DpmDataset
from dpm.delta_schema import (
    DELTA_DIMENSION_COLS,
    DELTA_METRIC_COLS,
    DELTA_STRUCTURE_COLS,
)

LOG = logging.getLogger(__name__)

_METRIC_STATUSES = ("Added", "Deleted", "Modified", "Kept")
_STRUCT_TYPES = (("Template", "Template"), ("SubTemplate", "Subtemplate"))
_SUBTEMPLATE_KEYS = ["perimeter", "template_code", "subtemplate_code"]


def empty_metric_df() -> pl.DataFrame:
    return pl.DataFrame(schema={col: pl.String for col in METRIC_COLS})


def empty_delta_df() -> pl.DataFrame:
    return pl.DataFrame(schema={col: pl.String for col in DELTA_STRUCTURE_COLS})


def to_delta_columns(df: pl.DataFrame) -> pl.DataFrame:
    return (
        df.select(
            [pl.col(col).cast(pl.String).fill_null("") for col in DELTA_STRUCTURE_COLS]
        )
        if df.height
        else empty_delta_df()
    )


def subtemplate_matrix_lookup(
    old_metrics: pl.DataFrame, new_metrics: pl.DataFrame
) -> pl.DataFrame:
    """Per-subtemplate ``is_matrix`` flag from the full metric catalogues.

    A subtemplate is a matrix when any of its row cells uses a column other than
    the default value column (``C0010``). Only cells with a non-empty ``row_code``
    count — column-only metrics live on a different axis and never make the row
    cells a matrix. The union of both versions covers old-only (deleted) and
    new-only subtemplates; matrix-ness does not differ per version.
    """
    combined = pl.concat(
        [df.select(_SUBTEMPLATE_KEYS + ["row_code", "column_code"]) for df in (old_metrics, new_metrics)],
        how="vertical",
    ).filter((pl.col("row_code") != "") & (pl.col("column_code") != ""))
    if combined.is_empty():
        return pl.DataFrame(
            schema={k: pl.String for k in _SUBTEMPLATE_KEYS} | {"is_matrix": pl.Boolean}
        )
    return combined.group_by(_SUBTEMPLATE_KEYS).agg(
        (pl.col("column_code") != DEFAULT_COLUMN_CODE).any().alias("is_matrix")
    )


def classify_change_type(
    metric_delta: pl.DataFrame, matrix_lookup: pl.DataFrame
) -> pl.DataFrame:
    """Stamp the three-way fact-cell ``change_type`` onto delta rows.

    ``FactColumn`` when the cell has no row, ``FactMatrix`` when its subtemplate
    is a matrix, else ``FactRow``.
    """
    if metric_delta.is_empty():
        return metric_delta
    joined = metric_delta.join(matrix_lookup, on=_SUBTEMPLATE_KEYS, how="left")
    return joined.with_columns(
        pl.when(pl.col("row_code") == "")
        .then(pl.lit("FactColumn"))
        .when(pl.col("is_matrix").fill_null(False))
        .then(pl.lit("FactMatrix"))
        .otherwise(pl.lit("FactRow"))
        .alias("change_type")
    ).drop("is_matrix")


def metric_status_df(
    old: pl.DataFrame, new: pl.DataFrame
) -> tuple[pl.DataFrame, pl.DataFrame, pl.DataFrame]:
    old_pref = old.rename(
        {
            "qname": "qname_old",
            "metric_label": "metric_label_old",
            "row_label": "row_label_old",
            "column_label": "column_label_old",
            "dimensions": "dimensions_old",
        }
    )
    new_pref = new.rename(
        {
            "qname": "qname_new",
            "metric_label": "metric_label_new",
            "row_label": "row_label_new",
            "column_label": "column_label_new",
            "dimensions": "dimensions_new",
        }
    )
    joined = old_pref.join(new_pref, on=KEY_COLS, how="inner")
    qname_changed = pl.col("qname_old") != pl.col("qname_new")
    dims_changed = pl.col("dimensions_old") != pl.col("dimensions_new")
    kept_modified = joined.with_columns(
        [
            pl.when(qname_changed | dims_changed)
            .then(pl.lit("Modified"))
            .otherwise(pl.lit("Kept"))
            .alias("status"),
            pl.lit("").alias("change_type"),
        ]
    )
    old_only = old.join(new.select(KEY_COLS), on=KEY_COLS, how="anti")
    new_only = new.join(old.select(KEY_COLS), on=KEY_COLS, how="anti")
    return to_delta_columns(kept_modified), old_only, new_only


def qname_modified_df(
    old_only: pl.DataFrame, new_only: pl.DataFrame
) -> tuple[pl.DataFrame, pl.DataFrame, pl.DataFrame]:
    old_q = old_only.filter(pl.col("qname") != "")
    new_q = new_only.filter(pl.col("qname") != "")
    if old_q.is_empty() or new_q.is_empty():
        return empty_delta_df(), old_only, new_only

    def _unique_by_perimeter_qname(df: pl.DataFrame, count_col: str) -> pl.DataFrame:
        return (
            df.with_columns(pl.len().over(["perimeter", "qname"]).alias(count_col))
            .filter(pl.col(count_col) == 1)
            .drop(count_col)
        )

    joined = _unique_by_perimeter_qname(old_q, "_old_count").join(
        _unique_by_perimeter_qname(new_q, "_new_count"),
        on=["perimeter", "qname"],
        how="inner",
        suffix="_new",
    )
    if joined.is_empty():
        return empty_delta_df(), old_only, new_only

    changed = joined.filter(
        (pl.col("template_code") != pl.col("template_code_new"))
        | (pl.col("subtemplate_code") != pl.col("subtemplate_code_new"))
        | (pl.col("row_code") != pl.col("row_code_new"))
        | (pl.col("column_code") != pl.col("column_code_new"))
    )
    if changed.is_empty():
        return empty_delta_df(), old_only, new_only

    modified = changed.with_columns(
        [
            pl.col("template_code_new").alias("template_code"),
            pl.col("subtemplate_code_new").alias("subtemplate_code"),
            pl.col("row_code_new").alias("row_code"),
            pl.col("column_code_new").alias("column_code"),
            pl.col("qname").alias("qname_old"),
            pl.col("qname").alias("qname_new"),
            pl.col("metric_label").alias("metric_label_old"),
            pl.col("metric_label_new").alias("metric_label_new"),
            pl.col("row_label").alias("row_label_old"),
            pl.col("row_label_new").alias("row_label_new"),
            pl.col("column_label").alias("column_label_old"),
            pl.col("column_label_new").alias("column_label_new"),
            pl.col("dimensions").alias("dimensions_old"),
            pl.col("dimensions_new").alias("dimensions_new"),
            pl.lit("Modified").alias("status"),
            pl.lit("").alias("change_type"),
        ]
    )
    old_remove = changed.select(KEY_COLS)
    new_remove = changed.select(
        [
            pl.col("perimeter"),
            pl.col("template_code_new").alias("template_code"),
            pl.col("subtemplate_code_new").alias("subtemplate_code"),
            pl.col("row_code_new").alias("row_code"),
            pl.col("column_code_new").alias("column_code"),
        ]
    )
    return (
        to_delta_columns(modified),
        old_only.join(old_remove, on=KEY_COLS, how="anti"),
        new_only.join(new_remove, on=KEY_COLS, how="anti"),
    )


def _side_df(df: pl.DataFrame, status: str) -> pl.DataFrame:
    is_added = status == "Added"
    return to_delta_columns(
        df.with_columns(
            [
                (pl.lit("") if is_added else pl.col("qname")).alias("qname_old"),
                (pl.col("qname") if is_added else pl.lit("")).alias("qname_new"),
                (pl.lit("") if is_added else pl.col("metric_label")).alias(
                    "metric_label_old"
                ),
                (pl.col("metric_label") if is_added else pl.lit("")).alias(
                    "metric_label_new"
                ),
                (pl.lit("") if is_added else pl.col("row_label")).alias("row_label_old"),
                (pl.col("row_label") if is_added else pl.lit("")).alias("row_label_new"),
                (pl.lit("") if is_added else pl.col("column_label")).alias(
                    "column_label_old"
                ),
                (pl.col("column_label") if is_added else pl.lit("")).alias(
                    "column_label_new"
                ),
                (pl.lit("") if is_added else pl.col("dimensions")).alias(
                    "dimensions_old"
                ),
                (pl.col("dimensions") if is_added else pl.lit("")).alias(
                    "dimensions_new"
                ),
                pl.lit(status).alias("status"),
                pl.lit("").alias("change_type"),
            ]
        )
    )


def added_deleted_df(old_only: pl.DataFrame, new_only: pl.DataFrame) -> pl.DataFrame:
    frames = [
        _side_df(df, status)
        for df, status in ((new_only, "Added"), (old_only, "Deleted"))
        if df.height
    ]
    return pl.concat(frames, how="vertical") if frames else empty_delta_df()


def _struct_rows(
    pairs: set[tuple[str, ...]],
    change_type: str,
    status: str,
) -> list[dict[str, str]]:
    return [
        {
            "perimeter": pair[0],
            "template_code": pair[1],
            "subtemplate_code": pair[2] if len(pair) > 2 else "",
            "row_code": "",
            "column_code": "",
            "qname_old": "",
            "qname_new": "",
            "metric_label_old": "",
            "metric_label_new": "",
            "row_label_old": "",
            "row_label_new": "",
            "column_label_old": "",
            "column_label_new": "",
            "dimensions_old": "",
            "dimensions_new": "",
            "status": status,
            "change_type": change_type,
        }
        for pair in sorted(pairs)
    ]


def structural_delta_df(old: DpmDataset, new: DpmDataset) -> pl.DataFrame:
    rows = (
        _struct_rows(new.templates - old.templates, "Template", "Added")
        + _struct_rows(old.templates - new.templates, "Template", "Deleted")
        + _struct_rows(new.subtemplates - old.subtemplates, "SubTemplate", "Added")
        + _struct_rows(old.subtemplates - new.subtemplates, "SubTemplate", "Deleted")
    )
    return pl.DataFrame(rows, schema=DELTA_STRUCTURE_COLS) if rows else empty_delta_df()


def _empty_status_df(cols: list[str]) -> pl.DataFrame:
    return pl.DataFrame(schema={col: pl.String for col in cols})


def _with_presence(df: pl.DataFrame, keys: list[str], flag: str) -> pl.DataFrame:
    """Deduplicate on ``keys`` and tag every row with a boolean presence flag."""
    return df.unique(subset=keys, keep="first").with_columns(
        pl.lit(True).alias(flag)
    )


def compare_metrics(old: pl.DataFrame, new: pl.DataFrame) -> pl.DataFrame:
    """Delta of the metric catalogue: Added / Deleted / Modified metrics."""
    if "metric_code" not in old.columns or "metric_code" not in new.columns:
        return _empty_status_df(DELTA_METRIC_COLS)
    o = _with_presence(
        old.select("metric_code", pl.col("metric_label").alias("metric_label_old")),
        ["metric_code"],
        "_in_old",
    )
    n = _with_presence(
        new.select("metric_code", pl.col("metric_label").alias("metric_label_new")),
        ["metric_code"],
        "_in_new",
    )
    joined = o.join(n, on="metric_code", how="full", coalesce=True)
    if joined.is_empty():
        return _empty_status_df(DELTA_METRIC_COLS)

    in_old = pl.col("_in_old").fill_null(False)
    in_new = pl.col("_in_new").fill_null(False)
    label_old = pl.col("metric_label_old").fill_null("")
    label_new = pl.col("metric_label_new").fill_null("")
    out = joined.with_columns(
        [
            label_old.alias("metric_label_old"),
            label_new.alias("metric_label_new"),
            pl.when(~in_old)
            .then(pl.lit("Added"))
            .when(~in_new)
            .then(pl.lit("Deleted"))
            .when(label_old != label_new)
            .then(pl.lit("Modified"))
            .otherwise(pl.lit("Kept"))
            .alias("status"),
        ]
    )
    # Every metric is listed, tagged with its status (incl. Kept) — the sheet is a
    # full catalogue view, not just the changed subset.
    return out.select(
        [pl.col(c).cast(pl.String).fill_null("") for c in DELTA_METRIC_COLS]
    ).sort(["status", "metric_code"])


def compare_dimensions(old: pl.DataFrame, new: pl.DataFrame) -> pl.DataFrame:
    """Delta of dimensions and their members: Added / Deleted / Modified."""
    if "dimension_code" not in old.columns or "dimension_code" not in new.columns:
        return _empty_status_df(DELTA_DIMENSION_COLS)

    def prep(df: pl.DataFrame, label_alias: str, dim_alias: str, flag: str) -> pl.DataFrame:
        return _with_presence(
            df.select(
                "dimension_code",
                pl.col("dimension_label").fill_null("").alias(dim_alias),
                pl.col("member_code").fill_null("").alias("member_code"),
                pl.col("member_label").fill_null("").alias(label_alias),
            ),
            ["dimension_code", "member_code"],
            flag,
        )

    o = prep(old, "member_label_old", "dimension_label_old", "_in_old")
    n = prep(new, "member_label_new", "dimension_label_new", "_in_new")
    joined = o.join(n, on=["dimension_code", "member_code"], how="full", coalesce=True)
    if joined.is_empty():
        return _empty_status_df(DELTA_DIMENSION_COLS)

    in_old = pl.col("_in_old").fill_null(False)
    in_new = pl.col("_in_new").fill_null(False)
    dim_old = pl.col("dimension_label_old").fill_null("")
    dim_new = pl.col("dimension_label_new").fill_null("")
    mem_old = pl.col("member_label_old").fill_null("")
    mem_new = pl.col("member_label_new").fill_null("")
    out = joined.with_columns(
        [
            mem_old.alias("member_label_old"),
            mem_new.alias("member_label_new"),
            # Prefer the new dimension label, falling back to the old one.
            pl.when(dim_new != "")
            .then(dim_new)
            .otherwise(dim_old)
            .alias("dimension_label"),
            pl.when(~in_old)
            .then(pl.lit("Added"))
            .when(~in_new)
            .then(pl.lit("Deleted"))
            .when((mem_old != mem_new) | (dim_old != dim_new))
            .then(pl.lit("Modified"))
            .otherwise(pl.lit("Kept"))
            .alias("status"),
        ]
    )
    out = out.filter(pl.col("status") != "Kept")
    return out.select(
        [pl.col(c).cast(pl.String).fill_null("") for c in DELTA_DIMENSION_COLS]
    ).sort(["status", "dimension_code", "member_code"])


def _structure_delta(
    old: DpmDataset,
    new: DpmDataset,
    on_step: Callable[[str], None] | None = None,
) -> pl.DataFrame:
    def _step(label: str) -> None:
        if on_step:
            on_step(label)

    def _ensure_dims(df: pl.DataFrame) -> pl.DataFrame:
        return df if "dimensions" in df.columns else df.with_columns(
            pl.lit("").alias("dimensions")
        )

    old_metrics = _ensure_dims(old.metrics)
    new_metrics = _ensure_dims(new.metrics)
    matrix_lookup = subtemplate_matrix_lookup(old_metrics, new_metrics)

    LOG.info("Delta step: coordinate comparison")
    _step("Comparing coordinates…")
    kept_modified, old_only, new_only = metric_status_df(old_metrics, new_metrics)

    LOG.info("Delta step: QName move detection")
    _step("Detecting QName moves…")
    qname_modified, old_remaining, new_remaining = qname_modified_df(old_only, new_only)

    LOG.info("Delta step: added/deleted metrics")
    _step("Added / deleted metrics…")
    added_deleted = added_deleted_df(old_remaining, new_remaining)

    # Classify fact cells (FactRow / FactColumn / FactMatrix) once, using
    # the full-catalogue subtemplate lookup. Structural rows keep their own
    # Template / SubTemplate change_type and are not reclassified.
    metric_frames = [
        df for df in [kept_modified, qname_modified, added_deleted] if df.height
    ]
    metric_delta = (
        classify_change_type(pl.concat(metric_frames, how="vertical"), matrix_lookup)
        if metric_frames
        else empty_delta_df()
    )

    LOG.info("Delta step: structural changes")
    _step("Structural changes…")
    structural = structural_delta_df(old, new)

    frames = [df for df in [metric_delta, structural] if df.height]
    return pl.concat(frames, how="vertical") if frames else empty_delta_df()


def compare_versions(
    old: DpmDataset,
    new: DpmDataset,
    on_step: Callable[[str], None] | None = None,
) -> DeltaResult:
    def _step(label: str) -> None:
        if on_step:
            on_step(label)

    structure = _structure_delta(old, new, on_step=on_step)

    LOG.info("Delta step: metric catalogue")
    _step("Comparing metrics…")
    metrics = compare_metrics(old.metric_catalog, new.metric_catalog)

    LOG.info("Delta step: dimensions")
    _step("Comparing dimensions…")
    dimensions = compare_dimensions(old.dimension_members, new.dimension_members)

    LOG.info("Delta step: assembly")
    _step("Assembling delta…")
    return DeltaResult(structure=structure, metrics=metrics, dimensions=dimensions)


def _summary_keys() -> list[str]:
    keys: list[str] = []
    for status in _METRIC_STATUSES:
        keys += [f"Metric {status}", f"Metric {status} %"]
    for _, label in _STRUCT_TYPES:
        keys += [
            f"{label} Added",
            f"{label} Added %",
            f"{label} Deleted",
            f"{label} Deleted %",
        ]
    return keys


def generate_summary(delta: pl.DataFrame) -> dict[str, float]:
    if delta.is_empty():
        return dict.fromkeys(_summary_keys(), 0.0)

    counts: dict[tuple[str, str], int] = {
        (r["change_type"], r["status"]): r["len"]
        for r in delta.group_by(["change_type", "status"]).len().to_dicts()
    }

    def metric_count(status: str) -> int:
        return (
            counts.get(("FactRow", status), 0)
            + counts.get(("FactColumn", status), 0)
            + counts.get(("FactMatrix", status), 0)
        )

    out: dict[str, float] = {}
    metric_totals = {s: metric_count(s) for s in _METRIC_STATUSES}
    metric_total = sum(metric_totals.values())
    for status, n in metric_totals.items():
        out[f"Metric {status}"] = n
        out[f"Metric {status} %"] = n / metric_total if metric_total else 0.0

    for ct, label in _STRUCT_TYPES:
        added = counts.get((ct, "Added"), 0)
        deleted = counts.get((ct, "Deleted"), 0)
        total = added + deleted
        out[f"{label} Added"] = added
        out[f"{label} Added %"] = added / total if total else 0.0
        out[f"{label} Deleted"] = deleted
        out[f"{label} Deleted %"] = deleted / total if total else 0.0

    return out
