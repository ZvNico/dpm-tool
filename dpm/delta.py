from __future__ import annotations

import logging

import polars as pl

from dpm._constants import DELTA_COLS, KEY_COLS, METRIC_COLS
from dpm._types import DpmDataset

LOG = logging.getLogger(__name__)

_METRIC_STATUSES = ("Added", "Deleted", "Modified", "Kept")
_STRUCT_TYPES = (("Template", "Template"), ("SubTemplate", "Subtemplate"))


def empty_metric_df() -> pl.DataFrame:
    return pl.DataFrame(schema={col: pl.String for col in METRIC_COLS})


def empty_delta_df() -> pl.DataFrame:
    return pl.DataFrame(schema={col: pl.String for col in DELTA_COLS})


def to_delta_columns(df: pl.DataFrame) -> pl.DataFrame:
    return (
        df.select([pl.col(col).cast(pl.String).fill_null("") for col in DELTA_COLS])
        if df.height
        else empty_delta_df()
    )


def metric_change_type_expr(row_col: str = "row_code") -> pl.Expr:
    return (
        pl.when(pl.col(row_col) != "")
        .then(pl.lit("MetricRow"))
        .otherwise(pl.lit("MetricColumn"))
    )


def metric_status_df(
    old: pl.DataFrame, new: pl.DataFrame
) -> tuple[pl.DataFrame, pl.DataFrame, pl.DataFrame]:
    old_pref = old.rename({
        "qname": "QNameOld", "metric_label": "MetricLabelOld",
        "row_label": "RowLabelOld", "column_label": "ColumnLabelOld",
    })
    new_pref = new.rename({
        "qname": "QNameNew", "metric_label": "MetricLabelNew",
        "row_label": "RowLabelNew", "column_label": "ColumnLabelNew",
    })
    joined = old_pref.join(new_pref, on=KEY_COLS, how="inner")
    kept_modified = joined.with_columns([
        pl.col("perimeter").alias("Perimeter"),
        pl.col("template_code").alias("TemplateCode"),
        pl.col("subtemplate_code").alias("SubTemplateCode"),
        pl.col("row_code").alias("RowCode"),
        pl.col("column_code").alias("ColumnCode"),
        pl.col("RowLabelOld"),
        pl.col("RowLabelNew"),
        pl.col("ColumnLabelOld"),
        pl.col("ColumnLabelNew"),
        pl.when(pl.col("QNameOld") != pl.col("QNameNew"))
        .then(pl.lit("Modified"))
        .otherwise(pl.lit("Kept"))
        .alias("Status"),
        metric_change_type_expr().alias("ChangeType"),
        pl.when(pl.col("QNameOld") != pl.col("QNameNew"))
        .then(pl.lit("QName changed for same template/subtemplate/row/column coordinates"))
        .otherwise(pl.lit(""))
        .alias("Comments"),
    ])
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

    modified = changed.with_columns([
        pl.col("perimeter").alias("Perimeter"),
        pl.col("template_code_new").alias("TemplateCode"),
        pl.col("subtemplate_code_new").alias("SubTemplateCode"),
        pl.col("row_code_new").alias("RowCode"),
        pl.col("column_code_new").alias("ColumnCode"),
        pl.col("qname").alias("QNameOld"),
        pl.col("qname").alias("QNameNew"),
        pl.col("metric_label").alias("MetricLabelOld"),
        pl.col("metric_label_new").alias("MetricLabelNew"),
        pl.col("row_label").alias("RowLabelOld"),
        pl.col("row_label_new").alias("RowLabelNew"),
        pl.col("column_label").alias("ColumnLabelOld"),
        pl.col("column_label_new").alias("ColumnLabelNew"),
        pl.lit("Modified").alias("Status"),
        metric_change_type_expr("row_code_new").alias("ChangeType"),
        pl.lit("Same QName with changed template/subtemplate/row/column coordinates").alias("Comments"),
    ])
    old_remove = changed.select(KEY_COLS)
    new_remove = changed.select([
        pl.col("perimeter"),
        pl.col("template_code_new").alias("template_code"),
        pl.col("subtemplate_code_new").alias("subtemplate_code"),
        pl.col("row_code_new").alias("row_code"),
        pl.col("column_code_new").alias("column_code"),
    ])
    return (
        to_delta_columns(modified),
        old_only.join(old_remove, on=KEY_COLS, how="anti"),
        new_only.join(new_remove, on=KEY_COLS, how="anti"),
    )


def _side_df(df: pl.DataFrame, status: str) -> pl.DataFrame:
    is_added = status == "Added"
    return to_delta_columns(df.with_columns([
        pl.col("perimeter").alias("Perimeter"),
        pl.col("template_code").alias("TemplateCode"),
        pl.col("subtemplate_code").alias("SubTemplateCode"),
        pl.col("row_code").alias("RowCode"),
        pl.col("column_code").alias("ColumnCode"),
        (pl.lit("") if is_added else pl.col("qname")).alias("QNameOld"),
        (pl.col("qname") if is_added else pl.lit("")).alias("QNameNew"),
        (pl.lit("") if is_added else pl.col("metric_label")).alias("MetricLabelOld"),
        (pl.col("metric_label") if is_added else pl.lit("")).alias("MetricLabelNew"),
        (pl.lit("") if is_added else pl.col("row_label")).alias("RowLabelOld"),
        (pl.col("row_label") if is_added else pl.lit("")).alias("RowLabelNew"),
        (pl.lit("") if is_added else pl.col("column_label")).alias("ColumnLabelOld"),
        (pl.col("column_label") if is_added else pl.lit("")).alias("ColumnLabelNew"),
        pl.lit(status).alias("Status"),
        metric_change_type_expr().alias("ChangeType"),
        pl.lit("").alias("Comments"),
    ]))


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
    comment: str,
) -> list[dict[str, str]]:
    return [
        {
            "Perimeter": pair[0],
            "TemplateCode": pair[1],
            "SubTemplateCode": pair[2] if len(pair) > 2 else "",
            "RowCode": "",
            "ColumnCode": "",
            "QNameOld": "",
            "QNameNew": "",
            "MetricLabelOld": "",
            "MetricLabelNew": "",
            "RowLabelOld": "",
            "RowLabelNew": "",
            "ColumnLabelOld": "",
            "ColumnLabelNew": "",
            "Status": status,
            "ChangeType": change_type,
            "Comments": comment,
        }
        for pair in sorted(pairs)
    ]


def structural_delta_df(old: DpmDataset, new: DpmDataset) -> pl.DataFrame:
    rows = (
        _struct_rows(new.templates - old.templates, "Template", "Added", "Template present only in new version")
        + _struct_rows(old.templates - new.templates, "Template", "Deleted", "Template present only in old version")
        + _struct_rows(new.subtemplates - old.subtemplates, "SubTemplate", "Added", "Subtemplate present only in new version")
        + _struct_rows(old.subtemplates - new.subtemplates, "SubTemplate", "Deleted", "Subtemplate present only in old version")
    )
    return pl.DataFrame(rows, schema=DELTA_COLS) if rows else empty_delta_df()


def compare_versions(old: DpmDataset, new: DpmDataset) -> pl.DataFrame:
    LOG.info("Delta step 1/5: coordinate comparison")
    kept_modified, old_only, new_only = metric_status_df(old.metrics, new.metrics)

    LOG.info("Delta step 2/5: QName move detection")
    qname_modified, old_remaining, new_remaining = qname_modified_df(old_only, new_only)

    LOG.info("Delta step 3/5: added/deleted metrics")
    added_deleted = added_deleted_df(old_remaining, new_remaining)

    LOG.info("Delta step 4/5: structural changes")
    structural = structural_delta_df(old, new)

    LOG.info("Delta step 5/5: final assembly")
    frames = [df for df in [kept_modified, qname_modified, added_deleted, structural] if df.height]
    return pl.concat(frames, how="vertical") if frames else empty_delta_df()


def _summary_keys() -> list[str]:
    keys: list[str] = []
    for status in _METRIC_STATUSES:
        keys += [f"Metric {status}", f"Metric {status} %"]
    for _, label in _STRUCT_TYPES:
        keys += [f"{label} Added", f"{label} Added %", f"{label} Deleted", f"{label} Deleted %"]
    return keys


def generate_summary(delta: pl.DataFrame) -> dict[str, float]:
    if delta.is_empty():
        return dict.fromkeys(_summary_keys(), 0.0)

    counts: dict[tuple[str, str], int] = {
        (r["ChangeType"], r["Status"]): r["len"]
        for r in delta.group_by(["ChangeType", "Status"]).len().to_dicts()
    }

    def metric_count(status: str) -> int:
        return counts.get(("MetricRow", status), 0) + counts.get(("MetricColumn", status), 0)

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
