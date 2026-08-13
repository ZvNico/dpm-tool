from __future__ import annotations

import logging
import re
from collections.abc import Callable
from pathlib import Path

import polars as pl
import xlsxwriter
from xlsxwriter.utility import xl_col_to_name

from dpm._types import DeltaResult
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
                "change_type",
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
