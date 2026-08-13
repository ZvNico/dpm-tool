from __future__ import annotations

import logging
import re
from collections.abc import Callable
from dataclasses import dataclass


from dpm._constants import CODE_RE, COL_RE, ROW_RE
from dpm._text import (
    extract_dimension_token,
    extract_member_token,
    extract_metric_label,
    extract_paren_label,
    extract_qname,
    first_match,
    is_qname,
    nearest_text,
    norm,
)
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
    WorkbookCache,
)

LOG = logging.getLogger(__name__)

_TOC_NAMES = frozenset({"table of contents", "toc"})
_SUBTEMPLATE_HEADER_RE = re.compile(
    r"^[A-Z]{1,3}\.(?:[0-9]{2}\.){2,7}[0-9]{2}\s+-\s+", re.I
)


@dataclass
class ParsedWorkbook:
    source_file: str
    templates: list[TemplateRow]
    subtemplates: list[SubtemplateRow]
    perimeters: list[PerimeterRow]
    perimeter_template: list[PerimeterTemplateRow]
    metrics: list[MetricRow]
    facts: list[FactRow]
    dimensions: list[DimensionRow]
    dimension_members: list[DimensionMemberRow]
    fact_dimensions: list[FactDimensionRow]


def _is_subtemplate_header(row: list[str]) -> bool:
    if not row or not row[0]:
        return False
    return bool(_SUBTEMPLATE_HEADER_RE.match(row[0]))


def _subtemplate_code_from_header(cell: str) -> str:
    match = CODE_RE.search(cell)
    return match.group(0).upper() if match else ""


def _pad(matrix: list[list[str]], width: int) -> list[list[str]]:
    return [
        row + [""] * (width - len(row)) if len(row) < width else row for row in matrix
    ]


def _parse_subtemplate_window(
    window: list[list[str]],
    template_code: str,
    sub_code: str,
    sub_label: str,
    crossed: frozenset[tuple[int, int]],
) -> tuple[
    str,
    list[MetricRow],
    list[FactRow],
    list[DimensionRow],
    list[DimensionMemberRow],
    list[FactDimensionRow],
]:
    """Return (subtemplate_type, metrics, facts, dimensions, members, fact_dimensions)."""
    if not window:
        return "metrics_row", [], [], [], [], []

    width = max(len(r) for r in window)
    w = _pad(window, width)

    metrics_anchors: list[tuple[int, int]] = []
    for r, row in enumerate(w):
        for c, cell in enumerate(row):
            if cell.lower() == "metrics":
                metrics_anchors.append((r, c))

    if not metrics_anchors:
        LOG.debug("No 'Metrics' anchor in %s; skipping", sub_code)
        return "metrics_row", [], [], [], [], []

    metrics_out: dict[str, MetricRow] = {}
    facts_out: dict[tuple[str, str, str], FactRow] = {}
    # (window_row, window_col) of each emitted fact -> (row_code, column_code),
    # used to attach positionally-resolved dimension members.
    fact_pos: dict[tuple[int, int], tuple[str, str]] = {}
    sub_type = "metrics_row"

    for m_row, m_col in metrics_anchors:
        below_left = w[m_row + 1][m_col - 1] if m_row + 1 < len(w) and m_col > 0 else ""
        if first_match(COL_RE, below_left):
            sub_type = "metrics_col"
            _extract_metrics_col(
                w, m_row, m_col, sub_code, metrics_out, facts_out, crossed, fact_pos
            )
        else:
            sub_type = "metrics_row"
            _extract_metrics_row(
                w, m_row, m_col, sub_code, metrics_out, facts_out, crossed, fact_pos
            )

    dims_out: dict[str, DimensionRow] = {}
    members_out: dict[str, DimensionMemberRow] = {}
    fact_dims_out: dict[tuple[str, str, str, str], FactDimensionRow] = {}
    _extract_window_dimensions(
        w, sub_code, fact_pos, crossed, dims_out, members_out, fact_dims_out
    )

    return (
        sub_type,
        list(metrics_out.values()),
        list(facts_out.values()),
        list(dims_out.values()),
        list(members_out.values()),
        list(fact_dims_out.values()),
    )


def _extract_metrics_row(
    w: list[list[str]],
    m_row: int,
    m_col: int,
    sub_code: str,
    metrics_out: dict[str, MetricRow],
    facts_out: dict[tuple, FactRow],
    crossed: frozenset[tuple[int, int]],
    fact_pos: dict[tuple[int, int], tuple[str, str]],
) -> None:
    """Metrics anchor heads a row; qnames to the right at C-code column positions.
    R codes (if any) occupy the same column as 'Metrics', in rows above m_row.
    """
    c_codes_map: dict[str, tuple[int, int, str]] = {}
    for r in range(m_row):
        for c, cell in enumerate(w[r]):
            code = first_match(COL_RE, cell)
            if code:
                if code not in c_codes_map or r > c_codes_map[code][0]:
                    c_codes_map[code] = (r, c, code)
    if not c_codes_map:
        return

    qname_by_col: dict[str, tuple[str, str, str]] = {}
    for col_code, (c_row, c_col, _) in c_codes_map.items():
        if c_col >= len(w[m_row]):
            continue
        qname_cell = w[m_row][c_col]
        if not is_qname(qname_cell):
            continue
        qname = extract_qname(qname_cell) or qname_cell
        metric_label = extract_metric_label(qname_cell)
        col_label = (
            w[c_row - 1][c_col] if c_row > 0 and c_col < len(w[c_row - 1]) else ""
        )
        qname_by_col[col_code] = (qname, metric_label, col_label)
        if qname and qname not in metrics_out:
            metrics_out[qname] = MetricRow(metric_code=qname, metric_label=metric_label)

    if not qname_by_col:
        return

    r_codes: list[tuple[int, str]] = []
    for r in range(m_row):
        if m_col < len(w[r]):
            code = first_match(ROW_RE, w[r][m_col])
            if code:
                r_codes.append((r, code))

    if r_codes:
        for r_row, row_code in r_codes:
            row_label = nearest_text(reversed(w[r_row][:m_col]))
            for col_code, (qname, _, col_label) in qname_by_col.items():
                c_col = c_codes_map[col_code][1]
                if (r_row, c_col) in crossed:
                    continue
                key = (sub_code, row_code, col_code)
                fact_pos[(r_row, c_col)] = (row_code, col_code)
                if key not in facts_out:
                    facts_out[key] = FactRow(
                        subtemplate_code=sub_code,
                        row_code=row_code,
                        column_code=col_code,
                        row_label=row_label,
                        column_label=col_label,
                        metric_code=qname,
                    )
    else:
        for col_code, (qname, _, col_label) in qname_by_col.items():
            c_col = c_codes_map[col_code][1]
            if (m_row, c_col) in crossed:
                continue
            key = (sub_code, "", col_code)
            fact_pos[(m_row, c_col)] = ("", col_code)
            if key not in facts_out:
                facts_out[key] = FactRow(
                    subtemplate_code=sub_code,
                    row_code="",
                    column_code=col_code,
                    row_label="",
                    column_label=col_label,
                    metric_code=qname,
                )


def _extract_metrics_col(
    w: list[list[str]],
    m_row: int,
    m_col: int,
    sub_code: str,
    metrics_out: dict[str, MetricRow],
    facts_out: dict[tuple, FactRow],
    crossed: frozenset[tuple[int, int]],
    fact_pos: dict[tuple[int, int], tuple[str, str]],
) -> None:
    """Metrics anchor heads a column of qnames; C codes are in the row below the anchor."""
    if m_row + 1 >= len(w):
        return

    c_codes: list[tuple[int, str]] = []
    for c, cell in enumerate(w[m_row + 1]):
        code = first_match(COL_RE, cell)
        if code:
            c_codes.append((c, code))
    if not c_codes:
        return

    r_codes: list[tuple[int, int, str]] = []
    for r in range(m_row + 2, len(w)):
        for c, cell in enumerate(w[r]):
            code = first_match(ROW_RE, cell)
            if code:
                r_codes.append((r, c, code))
                break

    for r_row, r_col, row_code in r_codes:
        if m_col >= len(w[r_row]):
            continue
        qname_cell = w[r_row][m_col]
        if not is_qname(qname_cell):
            continue
        qname = extract_qname(qname_cell) or qname_cell
        metric_label = extract_metric_label(qname_cell)
        row_label = nearest_text(reversed(w[r_row][:r_col]))

        if qname and qname not in metrics_out:
            metrics_out[qname] = MetricRow(metric_code=qname, metric_label=metric_label)

        for c_col, col_code in c_codes:
            if (r_row, c_col) in crossed:
                continue
            col_label = w[m_row][c_col] if c_col < len(w[m_row]) else ""
            key = (sub_code, row_code, col_code)
            fact_pos[(r_row, c_col)] = (row_code, col_code)
            if key not in facts_out:
                facts_out[key] = FactRow(
                    subtemplate_code=sub_code,
                    row_code=row_code,
                    column_code=col_code,
                    row_label=row_label,
                    column_label=col_label,
                    metric_code=qname,
                )


def _extract_window_dimensions(
    w: list[list[str]],
    sub_code: str,
    fact_pos: dict[tuple[int, int], tuple[str, str]],
    crossed: frozenset[tuple[int, int]],
    dims_out: dict[str, DimensionRow],
    members_out: dict[str, DimensionMemberRow],
    fact_dims_out: dict[tuple[str, str, str, str], FactDimensionRow],
) -> None:
    """Resolve dimension members positionally and attach them to facts.

    A ``s2c_dim:XX`` declaration heads either a column (members read down that
    column, sharing each fact's row) or a row (members read across, sharing each
    fact's column). A member cell is linked to its dimension by whichever of its
    own row/column carries a declaration — the domain codes never match by name.
    """
    if not fact_pos:
        return

    # Column/row index -> dimension declaration token, and register dimensions.
    decl_by_col: dict[int, str] = {}
    decl_by_row: dict[int, str] = {}
    for r, row in enumerate(w):
        for c, cell in enumerate(row):
            dim_code = extract_dimension_token(cell)
            if not dim_code:
                continue
            if dim_code not in dims_out:
                dims_out[dim_code] = DimensionRow(
                    dimension_code=dim_code, dimension_label=extract_paren_label(cell)
                )
            decl_by_col.setdefault(c, dim_code)
            decl_by_row.setdefault(r, dim_code)

    if not decl_by_col and not decl_by_row:
        return

    # Members grouped by the axis they qualify.
    row_members: dict[int, list[tuple[str, str]]] = {}
    col_members: dict[int, list[tuple[str, str]]] = {}
    for mr, row in enumerate(w):
        for mc, cell in enumerate(row):
            member_code = extract_member_token(cell)
            if not member_code or (mr, mc) in crossed:
                continue
            if mc in decl_by_col:
                dim_code = decl_by_col[mc]
                row_members.setdefault(mr, []).append((dim_code, member_code))
            elif mr in decl_by_row:
                dim_code = decl_by_row[mr]
                col_members.setdefault(mc, []).append((dim_code, member_code))
            else:
                continue
            if member_code not in members_out:
                members_out[member_code] = DimensionMemberRow(
                    member_code=member_code,
                    dimension_code=dim_code,
                    member_label=extract_paren_label(cell),
                )

    for (r_row, c_col), (row_code, col_code) in fact_pos.items():
        for dim_code, member_code in row_members.get(r_row, []) + col_members.get(
            c_col, []
        ):
            key = (sub_code, row_code, col_code, dim_code)
            if key not in fact_dims_out:
                fact_dims_out[key] = FactDimensionRow(
                    subtemplate_code=sub_code,
                    row_code=row_code,
                    column_code=col_code,
                    dimension_code=dim_code,
                    member_code=member_code,
                )


def _parse_template_sheet(
    matrix: list[list[str]],
    template_code: str,
    sheet_crossed: frozenset[tuple[int, int]],
) -> tuple[
    str,
    list[SubtemplateRow],
    list[MetricRow],
    list[FactRow],
    list[DimensionRow],
    list[DimensionMemberRow],
    list[FactDimensionRow],
]:
    """Return (template_label, subtemplates, metrics, facts, dimensions, members,
    fact_dimensions)."""
    if not matrix:
        return "", [], [], [], [], [], []

    template_label = norm(matrix[0][0]) if matrix[0] else ""

    anchors: list[tuple[int, str, str]] = []
    for i, row in enumerate(matrix):
        if _is_subtemplate_header(row):
            sub_code = _subtemplate_code_from_header(row[0])
            sub_label = norm(row[0])
            if sub_code:
                anchors.append((i, sub_code, sub_label))

    if not anchors:
        LOG.debug("No subtemplate headers in sheet %s", template_code)
        return template_label, [], [], [], [], [], []

    all_subtemplates: list[SubtemplateRow] = []
    all_metrics: list[MetricRow] = []
    all_facts: list[FactRow] = []
    all_dimensions: list[DimensionRow] = []
    all_members: list[DimensionMemberRow] = []
    all_fact_dimensions: list[FactDimensionRow] = []
    seen_metrics: set[str] = set()
    seen_dimensions: set[str] = set()
    seen_members: set[str] = set()

    for idx, (start, sub_code, sub_label) in enumerate(anchors):
        end = anchors[idx + 1][0] if idx + 1 < len(anchors) else len(matrix)
        window = matrix[start:end]
        window_crossed = frozenset(
            (r - start, c) for r, c in sheet_crossed if start <= r < end
        )
        sub_type, m_rows, f_rows, d_rows, mem_rows, fd_rows = _parse_subtemplate_window(
            window, template_code, sub_code, sub_label, window_crossed
        )

        all_subtemplates.append(
            SubtemplateRow(
                subtemplate_code=sub_code,
                template_code=template_code,
                subtemplate_label=sub_label,
                subtemplate_type=sub_type,
            )
        )
        for m in m_rows:
            if m["metric_code"] not in seen_metrics:
                seen_metrics.add(m["metric_code"])
                all_metrics.append(m)
        all_facts.extend(f_rows)
        for d in d_rows:
            if d["dimension_code"] not in seen_dimensions:
                seen_dimensions.add(d["dimension_code"])
                all_dimensions.append(d)
        for mem in mem_rows:
            if mem["member_code"] not in seen_members:
                seen_members.add(mem["member_code"])
                all_members.append(mem)
        all_fact_dimensions.extend(fd_rows)

    return (
        template_label,
        all_subtemplates,
        all_metrics,
        all_facts,
        all_dimensions,
        all_members,
        all_fact_dimensions,
    )


def parse_workbook(
    workbook: WorkbookCache,
    toc_perimeters: dict[str, set[str]],
    on_sheet: Callable[[int, int, str], None] | None = None,
) -> ParsedWorkbook:
    """Sheet-first parse: each non-TOC sheet is a template."""
    toc_names = {
        name.lower() for name in workbook.sheet_names if name.lower() in _TOC_NAMES
    }
    template_sheets = [
        name for name in workbook.sheet_names if name.lower() not in toc_names
    ]

    templates: list[TemplateRow] = []
    subtemplates: list[SubtemplateRow] = []
    metrics: list[MetricRow] = []
    facts: list[FactRow] = []
    dimensions: list[DimensionRow] = []
    dimension_members: list[DimensionMemberRow] = []
    fact_dimensions: list[FactDimensionRow] = []
    seen_metrics: set[str] = set()
    seen_dimensions: set[str] = set()
    seen_members: set[str] = set()
    template_codes_in_sheets: set[str] = set()

    for idx, sheet_name in enumerate(template_sheets, 1):
        LOG.debug("Parsing sheet %d/%d: %s", idx, len(template_sheets), sheet_name)
        if on_sheet is not None:
            on_sheet(idx, len(template_sheets), sheet_name)
        template_code = sheet_name.upper()
        matrix = workbook.matrix(sheet_name)
        sheet_crossed = workbook.crossed_cells(sheet_name)
        (
            template_label,
            subs,
            m_rows,
            f_rows,
            d_rows,
            mem_rows,
            fd_rows,
        ) = _parse_template_sheet(matrix, template_code, sheet_crossed)

        templates.append(
            TemplateRow(template_code=template_code, template_label=template_label)
        )
        template_codes_in_sheets.add(template_code)
        subtemplates.extend(subs)
        for m in m_rows:
            if m["metric_code"] not in seen_metrics:
                seen_metrics.add(m["metric_code"])
                metrics.append(m)
        facts.extend(f_rows)
        for d in d_rows:
            if d["dimension_code"] not in seen_dimensions:
                seen_dimensions.add(d["dimension_code"])
                dimensions.append(d)
        for mem in mem_rows:
            if mem["member_code"] not in seen_members:
                seen_members.add(mem["member_code"])
                dimension_members.append(mem)
        fact_dimensions.extend(fd_rows)

    perimeter_set: set[str] = set()
    pt_set: set[tuple[str, str]] = set()
    for perimeter_code, template_codes in toc_perimeters.items():
        for tc in template_codes:
            tc_upper = tc.upper()
            if tc_upper in template_codes_in_sheets:
                perimeter_set.add(perimeter_code)
                pt_set.add((perimeter_code, tc_upper))

    return ParsedWorkbook(
        source_file=str(workbook.path),
        templates=templates,
        subtemplates=subtemplates,
        perimeters=[PerimeterRow(perimeter_code=p) for p in sorted(perimeter_set)],
        perimeter_template=[
            PerimeterTemplateRow(perimeter_code=p, template_code=t)
            for p, t in sorted(pt_set)
        ],
        metrics=metrics,
        facts=facts,
        dimensions=dimensions,
        dimension_members=dimension_members,
        fact_dimensions=fact_dimensions,
    )
