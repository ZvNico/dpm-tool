from __future__ import annotations

import logging
import re
from collections.abc import Sequence

import polars as pl
from tqdm import tqdm

from dpm._constants import COL_RE, KEY_COLS, METRIC_COLS, ROW_RE
from dpm._text import extract_metric_label, extract_qname, first_match, is_qname, nearest_text
from dpm._types import DpmDataset, TocEntry, WorkbookCache
from dpm.toc import extract_perimeters, extract_subtemplates, extract_templates

LOG = logging.getLogger(__name__)


def _empty_metric_df() -> pl.DataFrame:
    return pl.DataFrame(schema={col: pl.String for col in METRIC_COLS})


def candidate_sheets(entry: TocEntry, sheets: Sequence[str]) -> list[str]:
    by_lower = {sheet.lower(): sheet for sheet in sheets}
    exact = by_lower.get(entry.template_code.lower())
    if exact:
        return [exact]
    if entry.sheet_name and entry.sheet_name.lower() in by_lower:
        return [by_lower[entry.sheet_name.lower()]]
    raise KeyError(f"No worksheet named like template {entry.template_code!r}")


def _first_non_empty_cell(row: Sequence[str]) -> tuple[int, str]:
    for col, cell in enumerate(row):
        if cell:
            return col, cell
    return -1, ""


def _is_subtemplate_header_row(row: Sequence[str], subtemplate_code: str | None = None) -> bool:
    col, cell = _first_non_empty_cell(row)
    if col != 0 or not cell:
        return False
    if subtemplate_code:
        return bool(re.match(rf"^{re.escape(subtemplate_code)}\s+-\s+", cell, re.I))
    return bool(re.match(r"^[A-Z]{1,3}\.(?:[0-9]{2}\.){2,7}[0-9]{2}\s+-\s+", cell, re.I))


def subtemplate_window(matrix: list[list[str]], subtemplate_code: str) -> list[list[str]]:
    anchors = [i for i, row in enumerate(matrix) if _is_subtemplate_header_row(row, subtemplate_code)]
    all_anchors = [i for i, row in enumerate(matrix) if _is_subtemplate_header_row(row)]
    if not anchors:
        LOG.debug("Subtemplate header not found for %s; using full sheet window", subtemplate_code)
        return matrix
    start = anchors[0]
    end = min((i for i in all_anchors if i > start), default=len(matrix))
    return matrix[start:end]


def _find_col_info(
    matrix: list[list[str]], qrow: int, qcol: int, search_start: int, search_end: int
) -> tuple[str, int, int]:
    """Return (col_code, code_row, code_col); (-1, -1) position when not found by proximity."""
    candidates: list[tuple[int, int, str, int, int]] = []
    for r in range(max(0, search_start), min(len(matrix), search_end + 1)):
        for c in range(max(0, qcol - 2), min(len(matrix[r]), qcol + 1)):
            code = first_match(COL_RE, matrix[r][c])
            if code:
                candidates.append((abs(qrow - r) + abs(qcol - c), -c, code, r, c))
    if candidates:
        _, _, code, code_r, code_c = sorted(candidates)[0]
        return code, code_r, code_c
    all_codes_pos = [
        (r, c, code)
        for r in range(max(0, search_start), min(len(matrix), search_end + 1))
        for c, cell in enumerate(matrix[r])
        if (code := first_match(COL_RE, cell))
    ]
    if all_codes_pos and len({pos[2] for pos in all_codes_pos}) == 1:
        r, c, code = all_codes_pos[0]
        return code, r, c
    return "", -1, -1


def _find_col_code(
    matrix: list[list[str]], qrow: int, qcol: int, search_start: int, search_end: int
) -> str:
    return _find_col_info(matrix, qrow, qcol, search_start, search_end)[0]


def _col_label(matrix: list[list[str]], code_row: int, code_col: int) -> str:
    if code_row <= 0 or code_col < 0:
        return ""
    return nearest_text([matrix[code_row - 1][code_col]])


def _row_label(matrix: list[list[str]], row_idx: int, row_code_col: int) -> str:
    left = matrix[row_idx][:row_code_col]
    right = matrix[row_idx][row_code_col + 1:]
    return nearest_text(reversed(left)) or nearest_text(right)


def _extract_primary(
    matrix: list[list[str]],
    perimeter: str,
    template: str,
    subtemplate: str,
    metrics_row: int,
    metrics_col: int,
) -> dict[tuple[str, str], dict[str, str]]:
    out: dict[tuple[str, str], dict[str, str]] = {}

    row_items: list[tuple[int, int, str]] = []
    for r in range(metrics_row + 1, len(matrix)):
        for c, cell in enumerate(matrix[r]):
            if row_code := first_match(ROW_RE, cell):
                row_items.append((r, c, row_code))
                break

    if row_items:
        first_data_row = row_items[0][0]
        for r, row_code_col, row_code in row_items:
            label = _row_label(matrix, r, row_code_col)
            for qcol, cell in enumerate(matrix[r]):
                if (qcol < metrics_col and not is_qname(cell)) or not is_qname(cell):
                    continue
                col_code, cc_row, cc_col = _find_col_info(matrix, r, qcol, metrics_row, first_data_row)
                if not col_code:
                    continue
                out.setdefault(
                    (row_code, col_code),
                    {
                        "perimeter": perimeter,
                        "template_code": template,
                        "subtemplate_code": subtemplate,
                        "row_code": row_code,
                        "column_code": col_code,
                        "qname": extract_qname(cell) or cell,
                        "metric_label": extract_metric_label(cell) or label,
                        "row_label": label,
                        "column_label": _col_label(matrix, cc_row, cc_col),
                    },
                )

    for qcol in range(metrics_col + 1, len(matrix[metrics_row])):
        qname = matrix[metrics_row][qcol]
        if not is_qname(qname):
            continue
        col_code, cc_row, cc_col = _find_col_info(matrix, metrics_row, qcol, max(0, metrics_row - 8), metrics_row)
        if not col_code:
            continue
        label = nearest_text(matrix[metrics_row - 1][qcol: qcol + 1] if metrics_row else [])
        if not label:
            label = nearest_text(matrix[r][qcol] for r in range(max(0, metrics_row - 8), metrics_row))
        col_lbl = _col_label(matrix, cc_row, cc_col) or label
        out.setdefault(
            ("", col_code),
            {
                "perimeter": perimeter,
                "template_code": template,
                "subtemplate_code": subtemplate,
                "row_code": "",
                "column_code": col_code,
                "qname": extract_qname(qname) or qname,
                "metric_label": extract_metric_label(qname) or label,
                "row_label": "",
                "column_label": col_lbl,
            },
        )

    return out


def _extract_fallback(
    matrix: list[list[str]], perimeter: str, template: str, subtemplate: str
) -> list[dict[str, str]]:
    row_codes = sorted({
        code for row in matrix for cell in row if (code := first_match(ROW_RE, cell))
    })
    col_codes = sorted({
        code for row in matrix for cell in row if (code := first_match(COL_RE, cell))
    })
    base = {
        "perimeter": perimeter,
        "template_code": template,
        "subtemplate_code": subtemplate,
        "qname": "",
        "metric_label": "",
        "row_label": "",
        "column_label": "",
    }
    if row_codes:
        return [
            {**base, "row_code": rc, "column_code": cc}
            for rc in row_codes
            for cc in (col_codes or [""])
        ]
    return [{**base, "row_code": "", "column_code": cc} for cc in col_codes]


def extract_records_from_matrix(
    matrix: list[list[str]], perimeter: str, template: str, subtemplate: str
) -> pl.DataFrame:
    if not matrix:
        return _empty_metric_df()
    width = max(len(row) for row in matrix)
    matrix = [row + [""] * (width - len(row)) for row in matrix]

    rows_by_key: dict[tuple[str, str], dict[str, str]] = {}
    for r, row in enumerate(matrix):
        for c, cell in enumerate(row):
            if cell.lower() == "metrics":
                rows_by_key.update(
                    _extract_primary(matrix, perimeter, template, subtemplate, r, c)
                )

    if rows_by_key:
        return pl.DataFrame(list(rows_by_key.values()), schema=METRIC_COLS)

    fallback = _extract_fallback(matrix, perimeter, template, subtemplate)
    return pl.DataFrame(fallback, schema=METRIC_COLS) if fallback else _empty_metric_df()


def extract_metrics(
    workbook: WorkbookCache,
    entries: Sequence[TocEntry],
    label: str = "Metrics",
) -> pl.DataFrame:
    frames: list[pl.DataFrame] = []
    for entry in tqdm(entries, desc=label, unit="subtemplate", leave=True):
        found = _empty_metric_df()
        for sheet in candidate_sheets(entry, workbook.sheet_names):
            window = subtemplate_window(workbook.matrix(sheet), entry.subtemplate_code)
            found = extract_records_from_matrix(
                window, entry.perimeter, entry.template_code, entry.subtemplate_code
            )
            if found.height:
                break
        if found.height:
            frames.append(found)
        else:
            LOG.debug(
                "No metrics found for %s/%s/%s",
                entry.perimeter, entry.template_code, entry.subtemplate_code,
            )

    result = (
        pl.concat(frames, how="vertical").unique(subset=KEY_COLS, keep="first")
        if frames
        else _empty_metric_df()
    )
    LOG.info("Metrics extracted total: %d", result.height)
    return result


def build_metric_dataset(
    workbook: WorkbookCache, entries: Sequence[TocEntry], label: str
) -> DpmDataset:
    LOG.info(
        "Perimeters=%d Templates=%d Subtemplates=%d",
        len(extract_perimeters(entries)),
        len(extract_templates(entries)),
        len(extract_subtemplates(entries)),
    )
    return DpmDataset(
        metrics=extract_metrics(workbook, entries, label=label),
        entries=list(entries),
    )
