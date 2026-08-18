from __future__ import annotations

import logging
import re
from collections.abc import Callable
from pathlib import Path

import polars as pl
import xlsxwriter
from xlsxwriter.utility import xl_col_to_name

from dpm._types import ApplyStats, DeltaResult
from dpm.delta import generate_summary
from dpm.delta_schema import DIMENSION_XLSX, METRIC_XLSX, STRUCTURE_XLSX

LOG = logging.getLogger(__name__)

_CLR_HEADER = "#D9EAF7"
_CLR_BORDER = "#B7B7B7"
_CLR_ADDED = "#C6EFCE"
_CLR_DELETED = "#FFC7CE"
_CLR_MODIFIED = "#FCE4D6"


def _safe_sheet_name(name: str, used: set[str]) -> str:
    base = re.sub(r"[\\/*?:\[\]]", "_", name)[:31] or "perimeter"
    candidate = base
    i = 1
    while candidate in used:
        suffix = f"_{i}"
        candidate = (base[: 31 - len(suffix)] + suffix)[:31]
        i += 1
    used.add(candidate)
    return candidate


def _write_status_sheet(
    wb,
    name: str,
    df: pl.DataFrame,
    hmap: dict[str, str],
    hdr_fmt,
    cell_fmt,
    cf_fmts: dict[str, object],
) -> None:
    """Write one Added/Deleted/Modified sheet: header, widths, streamed rows, colours.

    ``hmap`` maps canonical (snake_case) delta columns → PascalCase display headers;
    it must contain a ``status`` column, whose ``"Status"`` header drives row colour.
    """
    src_cols = list(hmap.keys())
    headers = list(hmap.values())
    n_cols = len(headers)
    last_col = xl_col_to_name(n_cols - 1)
    status_letter = xl_col_to_name(headers.index("Status"))
    n_rows = df.height

    ws = wb.add_worksheet(name)
    ws.freeze_panes(1, 0)
    ws.autofilter(0, 0, max(n_rows, 1), n_cols - 1)

    # Column widths from Polars — set before writing rows (required for constant_memory mode)
    for col, (src, header) in enumerate(zip(src_cols, headers)):
        max_data = int(df[src].cast(pl.String).str.len_chars().max() or 0)
        ws.set_column(col, col, min(80, max(10, max(max_data, len(header)) + 2)))

    for col, header in enumerate(headers):
        ws.write(0, col, header, hdr_fmt)

    # Data rows — streamed directly to the zip, no in-memory DOM
    for row_idx, row in enumerate(df.select(src_cols).iter_rows(named=False), 1):
        ws.write_row(row_idx, 0, row, cell_fmt)

    # Status fills as conditional-format rules — O(1) XML, Excel evaluates at open time
    if n_rows:
        cf_range = f"A2:{last_col}{n_rows + 1}"
        for status, fmt in cf_fmts.items():
            ws.conditional_format(
                cf_range,
                {
                    "type": "formula",
                    "criteria": f'=${status_letter}2="{status}"',
                    "format": fmt,
                },
            )


def generate_delta_workbook(
    delta: DeltaResult,
    output_path: Path,
    on_sheet: Callable[[int, int, str], None] | None = None,
) -> None:
    wb = xlsxwriter.Workbook(str(output_path), {"constant_memory": True})

    hdr_fmt = wb.add_format(
        {
            "bold": True,
            "bg_color": _CLR_HEADER,
            "border": 1,
            "border_color": _CLR_BORDER,
        }
    )
    cell_fmt = wb.add_format({"border": 1, "border_color": _CLR_BORDER})
    pct_fmt = wb.add_format(
        {"num_format": "0.00%", "border": 1, "border_color": _CLR_BORDER}
    )
    cf_fmts = {
        "Added": wb.add_format(
            {"bg_color": _CLR_ADDED, "border": 1, "border_color": _CLR_BORDER}
        ),
        "Deleted": wb.add_format(
            {"bg_color": _CLR_DELETED, "border": 1, "border_color": _CLR_BORDER}
        ),
        "Modified": wb.add_format(
            {"bg_color": _CLR_MODIFIED, "border": 1, "border_color": _CLR_BORDER}
        ),
    }

    structure = delta.structure
    perimeters = (
        sorted(structure.get_column("perimeter").unique().to_list())
        if not structure.is_empty()
        else []
    )
    # Metrics + Dimensions + one sheet per perimeter (Summary is not counted).
    total_sheets = 2 + len(perimeters)
    written = 0

    def announce(name: str) -> None:
        nonlocal written
        written += 1
        LOG.debug("Writing sheet %d/%d: %s", written, total_sheets, name)
        if on_sheet is not None:
            on_sheet(written, total_sheets, name)

    # ── Summary sheet ──────────────────────────────────────────────────────
    summary = generate_summary(structure)
    keys, vals = list(summary.keys()), list(summary.values())
    ws = wb.add_worksheet("Summary")
    ws.freeze_panes(1, 0)
    ws.autofilter(0, 0, 1, len(keys) - 1)
    for col, k in enumerate(keys):
        ws.set_column(col, col, max(14, len(k) + 2))
        ws.write(0, col, k, hdr_fmt)
    for col, v in enumerate(vals):
        ws.write(1, col, v, pct_fmt if isinstance(v, float) else cell_fmt)

    used: set[str] = {"Summary"}

    # ── Metrics + Dimensions delta sheets ──────────────────────────────────
    announce("Metrics")
    _write_status_sheet(
        wb, "Metrics", delta.metrics, METRIC_XLSX, hdr_fmt, cell_fmt, cf_fmts
    )
    used.add("Metrics")

    announce("Dimensions")
    _write_status_sheet(
        wb,
        "Dimensions",
        delta.dimensions,
        DIMENSION_XLSX,
        hdr_fmt,
        cell_fmt,
        cf_fmts,
    )
    used.add("Dimensions")

    # ── Per-perimeter structure sheets ─────────────────────────────────────
    for perimeter in perimeters:
        announce(str(perimeter))
        subset = structure.filter(pl.col("perimeter") == perimeter).sort(
            [
                "type",
                "template_code",
                "subtemplate_code",
                "row_code",
                "column_code",
                "status",
            ]
        )
        _write_status_sheet(
            wb,
            _safe_sheet_name(str(perimeter), used),
            subset,
            STRUCTURE_XLSX,
            hdr_fmt,
            cell_fmt,
            cf_fmts,
        )

    wb.close()


# ── Apply-delta debug workbook ────────────────────────────────────────────────


def _col_width(header: str, values: list[str]) -> int:
    return min(80, max(10, max([len(header), *(len(v) for v in values)]) + 2))


def _write_flat_sheet(wb, name, headers, rows, header_fmt, cell_fmt) -> None:
    ws = wb.add_worksheet(name)
    ws.freeze_panes(1, 0)
    for col, header in enumerate(headers):
        ws.set_column(col, col, _col_width(header, [str(r[col]) for r in rows]))
        ws.write(0, col, header, header_fmt)
    ws.autofilter(0, 0, max(len(rows), 1), len(headers) - 1)
    for row_idx, values in enumerate(rows, 1):
        ws.write_row(row_idx, 0, values, cell_fmt)


def _write_grouped_sheet(
    wb, name, headers, display_rows, header_fmt, parent_fmt, child_fmt
) -> None:
    """A metric-rooted sheet: ``display_rows`` is ``[(level, [values]), …]`` where
    level 0 is a bold parent row and level 1 an indented, collapsible child row."""
    ws = wb.add_worksheet(name)
    ws.freeze_panes(1, 0)
    ws.outline_settings(True, False, True, False)
    for col, header in enumerate(headers):
        column = [str(vals[col]) for _, vals in display_rows]
        ws.set_column(col, col, _col_width(header, column))
        ws.write(0, col, header, header_fmt)
    ws.autofilter(0, 0, max(len(display_rows), 1), len(headers) - 1)
    for row_idx, (level, values) in enumerate(display_rows, 1):
        if level:
            ws.set_row(row_idx, None, None, {"level": 1, "hidden": False})
        ws.write_row(row_idx, 0, values, parent_fmt if level == 0 else child_fmt)


def _count_by_qname(df, cols):
    """Yield ``(key, count)`` per distinct ``cols`` tuple, in first-seen order. ``key`` is
    the scalar when ``cols`` has one entry, else the tuple."""
    counts: dict[tuple, int] = {}
    order: list[tuple] = []
    for row in df.iter_rows(named=True):
        key = tuple(row[c] for c in cols)
        if key not in counts:
            counts[key] = 0
            order.append(key)
        counts[key] += 1
    for key in order:
        yield (key[0] if len(cols) == 1 else key), counts[key]


def _group_by_metric(df, parent_fn, child_fn) -> list[tuple[int, list[str]]]:
    """Order rows by first-seen qname into parent (L0) + child (L1) display rows."""
    groups: dict[str, list[dict]] = {}
    order: list[str] = []
    for row in df.iter_rows(named=True):
        qname = row["qname"]
        if qname not in groups:
            groups[qname] = []
            order.append(qname)
        groups[qname].append(row)
    display: list[tuple[int, list[str]]] = []
    for qname in order:
        grp = groups[qname]
        display.append((0, parent_fn(qname, grp)))
        display.extend((1, child_fn(row)) for row in grp)
    return display


def generate_apply_debug_workbook(
    path: Path,
    *,
    stats: ApplyStats,
    deleted: pl.DataFrame,
    renamed: pl.DataFrame,
    repointed: pl.DataFrame,
    new_contexts: pl.DataFrame,
) -> None:
    """Readable apply-delta debug: a Summary plus one metric-rooted sheet per action.

    Each action sheet nests the affected facts one outline level below their metric, so
    a collapsed view shows the metric-level change and expanding reveals the per-fact
    detail — matching the real ``metric → facts`` relationship in the instance.
    """
    wb = xlsxwriter.Workbook(str(path), {"constant_memory": True})
    border = {"border": 1, "border_color": _CLR_BORDER}
    cell_fmt = wb.add_format(border)
    parent_fmt = wb.add_format({"bold": True, **border})
    child_fmt = wb.add_format({"indent": 1, **border})

    def hdr(color: str):
        return wb.add_format({"bold": True, "bg_color": color, **border})

    # ── Summary ────────────────────────────────────────────────────────────
    summary = {
        "Perimeter": stats.perimeter,
        "Facts before": stats.facts_before,
        "Facts after": stats.facts_after,
        "Deleted facts": stats.deleted_facts,
        "Renamed facts": stats.renamed_facts,
        "Re-pointed facts": stats.repointed_facts,
        "New contexts": stats.new_contexts,
    }
    ws = wb.add_worksheet("Summary")
    ws.freeze_panes(1, 0)
    ws.autofilter(0, 0, 1, len(summary) - 1)
    summary_hdr = hdr(_CLR_HEADER)
    # constant_memory flushes a row when the next is written — emit the whole header
    # row before any value cell, or later header cells are silently dropped.
    for col, key in enumerate(summary):
        ws.set_column(col, col, max(14, len(key) + 2))
        ws.write(0, col, key, summary_hdr)
    for col, value in enumerate(summary.values()):
        ws.write(1, col, value, cell_fmt)

    # ── Deleted metrics ────────────────────────────────────────────────────
    # A delete removes the whole fact; nothing about the fact changes beyond that, so
    # this is one row per metric with the number of facts it removed.
    _write_flat_sheet(
        wb,
        "Deleted metrics",
        ["Metric", "Facts"],
        [
            [qname, count]
            for qname, count in _count_by_qname(deleted, ["qname"])
        ],
        hdr(_CLR_DELETED),
        cell_fmt,
    )

    # ── Renamed metrics ────────────────────────────────────────────────────
    # A rename only rewrites the metric element name — context, dimensions and value are
    # untouched — so one row per metric with its new code and the number of facts hit.
    _write_flat_sheet(
        wb,
        "Renamed metrics",
        ["Metric (old → new)", "Facts"],
        [
            [f"{qname} → {qname_new}", count]
            for (qname, qname_new), count in _count_by_qname(renamed, ["qname", "qname_new"])
        ],
        hdr(_CLR_MODIFIED),
        cell_fmt,
    )

    # ── Re-pointed cells ───────────────────────────────────────────────────
    _write_grouped_sheet(
        wb,
        "Re-pointed cells",
        ["Metric", "Context (old → new)", "Dimensions (old → new)", "Value"],
        _group_by_metric(
            repointed,
            lambda q, g: [q, f"{len(g)} facts", "", ""],
            lambda r: [
                "",
                f"{r['context_old']} → {r['context_new']}",
                f"{r['dimensions_old']} → {r['dimensions_new']}",
                r["value"],
            ],
        ),
        hdr(_CLR_MODIFIED),
        parent_fmt,
        child_fmt,
    )

    # ── New contexts (flat) ────────────────────────────────────────────────
    _write_flat_sheet(
        wb,
        "New contexts",
        ["Context id", "Dimensions", "Cloned from"],
        [
            [r["context_id"], r["dimensions"], r["cloned_from"]]
            for r in new_contexts.iter_rows(named=True)
        ],
        hdr(_CLR_ADDED),
        cell_fmt,
    )

    wb.close()
