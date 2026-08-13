from __future__ import annotations

import logging
import re
from collections import Counter
from collections.abc import Sequence

from dpm._constants import CODE_RE, COL_RE, GENERIC_TOC_CODES, ROW_RE
from dpm._text import first_match, norm
from dpm._types import TocEntry, WorkbookCache

LOG = logging.getLogger(__name__)

_TOC_HEADER_SCAN_LIMIT = 80


def discover_toc_sheet(workbook: WorkbookCache) -> str:
    match = next(
        (name for name in workbook.sheet_names if name.lower() == "table of contents"),
        None,
    )
    if match:
        LOG.info("TOC sheet: %s", match)
        return match
    raise ValueError('Required TOC sheet "Table of Contents" not found')


def looks_like_perimeter(value: str) -> bool:
    value = norm(value).lower()
    if (
        not value
        or CODE_RE.search(value)
        or ROW_RE.search(value)
        or COL_RE.search(value)
    ):
        return False
    forbidden = {
        "perimeter",
        "scope",
        "template",
        "templates",
        "subtemplate",
        "sub-template",
        "sheet",
        "code",
        "name",
        "table",
        "contents",
        "toc",
        "description",
    }
    return bool(
        value not in forbidden
        and len(value) <= 12
        and re.fullmatch(r"[a-z][a-z0-9_-]{1,11}", value)
    )


def _find_header_row(rows: list[list[str]]) -> int:
    best_i, best = 0, -1
    for i, row in enumerate(rows[:_TOC_HEADER_SCAN_LIMIT]):
        low = [c.lower() for c in row]
        score = 100 if any(c == "template code" for c in low) else 0
        score += 10 if any(c == "description" for c in low) else 0
        score += sum(5 for c in low if looks_like_perimeter(c))
        if score > best:
            best_i, best = i, score
    return best_i


def _parent_template_code(subtemplate_code: str, current_template: str = "") -> str:
    sub = norm(subtemplate_code).upper()
    cur = norm(current_template).upper()
    if cur and sub.startswith(cur + "."):
        return cur
    parts = sub.split(".")
    return ".".join(parts[:-1]) if len(parts) > 3 else sub


def _parse_toc_header(
    workbook: WorkbookCache,
) -> tuple[list[list[str]], int, int, list[tuple[int, str]]]:
    """Return (rows, header_i, code_col, perimeter_cols) parsed from the TOC sheet."""
    toc_sheet = discover_toc_sheet(workbook)
    raw = workbook.matrix(toc_sheet)
    if not raw:
        raise ValueError("TOC sheet is empty")

    width = max(len(row) for row in raw)
    rows = [row + [""] * (width - len(row)) for row in raw]
    header_i = _find_header_row(rows)
    headers = rows[header_i]

    code_col = next(
        (
            i
            for i, h in enumerate(headers)
            if h.lower() in {"template code", "template", "code"}
        ),
        -1,
    )
    desc_col = next(
        (i for i, h in enumerate(headers) if h.lower() == "description"), -1
    )
    perimeter_cols = [
        (i, h.lower())
        for i, h in enumerate(headers)
        if i not in {code_col, desc_col} and looks_like_perimeter(h)
    ]
    if code_col < 0 or not perimeter_cols:
        raise ValueError(
            "Unable to parse 'Table of Contents': "
            "expected Template code, Description, then perimeter columns"
        )
    return rows, header_i, code_col, perimeter_cols


def extract_toc(workbook: WorkbookCache) -> list[TocEntry]:
    """Legacy: return flat TocEntry list from TOC. Used by the Excel fallback path."""
    rows, header_i, code_col, perimeter_cols = _parse_toc_header(workbook)

    entries: dict[tuple[str, str, str], TocEntry] = {}
    current_template = ""
    for row in rows[header_i + 1 :]:
        row_code = first_match(CODE_RE, row[code_col]) if code_col < len(row) else ""
        if not row_code or row_code in GENERIC_TOC_CODES or row_code.startswith("T.99"):
            continue
        populated: list[tuple[str, str]] = []
        for col, perimeter in perimeter_cols:
            cell_code = first_match(CODE_RE, row[col]) if col < len(row) else ""
            if (
                cell_code
                and cell_code not in GENERIC_TOC_CODES
                and not cell_code.startswith("T.99")
            ):
                populated.append((perimeter, cell_code))
        if not populated:
            current_template = row_code
            continue
        for perimeter, subtemplate in populated:
            template = _parent_template_code(subtemplate, current_template)
            entries[(perimeter, template, subtemplate)] = TocEntry(
                perimeter, template, subtemplate
            )

    if not entries:
        raise ValueError(
            "TOC extraction returned no perimeter/template/subtemplate entries"
        )
    distribution = Counter(e.perimeter for e in entries.values())
    LOG.info(
        "TOC entries=%d perimeters=%s", len(entries), dict(sorted(distribution.items()))
    )
    return sorted(
        entries.values(),
        key=lambda e: (e.perimeter, e.template_code, e.subtemplate_code),
    )


def extract_toc_perimeters(workbook: WorkbookCache) -> dict[str, set[str]]:
    """Return {perimeter_code: set[template_code]} from the TOC sheet."""
    rows, header_i, code_col, perimeter_cols = _parse_toc_header(workbook)

    result: dict[str, set[str]] = {p: set() for _, p in perimeter_cols}
    for row in rows[header_i + 1 :]:
        row_code = first_match(CODE_RE, row[code_col]) if code_col < len(row) else ""
        if not row_code or row_code in GENERIC_TOC_CODES or row_code.startswith("T.99"):
            continue
        for col, perimeter in perimeter_cols:
            cell_code = first_match(CODE_RE, row[col]) if col < len(row) else ""
            if (
                cell_code
                and cell_code not in GENERIC_TOC_CODES
                and not cell_code.startswith("T.99")
            ):
                parts = cell_code.upper().split(".")
                template_code = (
                    ".".join(parts[:-1]) if len(parts) > 3 else cell_code.upper()
                )
                result[perimeter].add(template_code)

    LOG.info(
        "TOC perimeters: %s",
        {p: len(tc) for p, tc in result.items()},
    )
    return result


def extract_perimeters(entries: Sequence[TocEntry]) -> set[str]:
    return {e.perimeter for e in entries}


def parse_text_selection(raw: str, perimeters: Sequence[str]) -> set[str]:
    raw = raw.strip().lower()
    if not raw or raw == "all":
        return {p.lower() for p in perimeters}
    selected: set[str] = set()
    by_name = {p.lower(): p.lower() for p in perimeters}
    for token in re.split(r"[,;\s]+", raw):
        if not token:
            continue
        if "-" in token and all(part.isdigit() for part in token.split("-", 1)):
            start, end = (int(p) for p in token.split("-", 1))
            for idx in range(start, end + 1):
                if 1 <= idx <= len(perimeters):
                    selected.add(perimeters[idx - 1].lower())
        elif token.isdigit():
            idx = int(token)
            if 1 <= idx <= len(perimeters):
                selected.add(perimeters[idx - 1].lower())
        elif token in by_name:
            selected.add(by_name[token])
        else:
            raise ValueError(f"Unknown perimeter selection token: {token!r}")
    if not selected:
        raise ValueError("At least one perimeter must be selected")
    return selected


def filter_entries_by_perimeter(
    entries: Sequence[TocEntry], selected: set[str]
) -> list[TocEntry]:
    return [e for e in entries if e.perimeter.lower() in selected]
