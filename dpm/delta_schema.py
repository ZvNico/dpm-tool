"""Canonical schema for the delta between two DPM versions — the source of truth.

The delta DuckDB (``dpm.delta_db``) stores three flat *change* tables in this
snake_case schema, consistent with the ingested databases (``dpm.db``). The
Excel workbook (``dpm.excel``) is a human-facing render of these tables and maps
each canonical column to a PascalCase header via the ``*_XLSX`` maps below;
nothing else depends on those headers.
"""

from __future__ import annotations

# ── Canonical (DuckDB) column lists — snake_case, source of truth ─────────────

DELTA_STRUCTURE_COLS = [
    "perimeter",
    "template_code",
    "subtemplate_code",
    "row_code",
    "column_code",
    "qname_old",
    "qname_new",
    "metric_label_old",
    "metric_label_new",
    "row_label_old",
    "row_label_new",
    "column_label_old",
    "column_label_new",
    "dimensions_old",
    "dimensions_new",
    "status",
    "change_type",
]

DELTA_METRIC_COLS = [
    "metric_code",
    "metric_label_old",
    "metric_label_new",
    "status",
]

DELTA_DIMENSION_COLS = [
    "dimension_code",
    "dimension_label",
    "member_code",
    "member_label_old",
    "member_label_new",
    "status",
]

# ── Excel presentation maps — canonical → PascalCase header (order-stable) ─────
# Consumed only by dpm.excel when rendering the human-facing workbook.

STRUCTURE_XLSX = {
    "perimeter": "Perimeter",
    "template_code": "TemplateCode",
    "subtemplate_code": "SubTemplateCode",
    "row_code": "RowCode",
    "column_code": "ColumnCode",
    "qname_old": "QNameOld",
    "qname_new": "QNameNew",
    "metric_label_old": "MetricLabelOld",
    "metric_label_new": "MetricLabelNew",
    "row_label_old": "RowLabelOld",
    "row_label_new": "RowLabelNew",
    "column_label_old": "ColumnLabelOld",
    "column_label_new": "ColumnLabelNew",
    "dimensions_old": "DimensionsOld",
    "dimensions_new": "DimensionsNew",
    "status": "Status",
    "change_type": "ChangeType",
}

METRIC_XLSX = {
    "metric_code": "MetricCode",
    "metric_label_old": "MetricLabelOld",
    "metric_label_new": "MetricLabelNew",
    "status": "Status",
}

DIMENSION_XLSX = {
    "dimension_code": "DimensionCode",
    "dimension_label": "DimensionLabel",
    "member_code": "MemberCode",
    "member_label_old": "MemberLabelOld",
    "member_label_new": "MemberLabelNew",
    "status": "Status",
}
