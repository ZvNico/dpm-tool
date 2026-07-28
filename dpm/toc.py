from __future__ import annotations

import logging
import re
import sys
from collections import Counter
from collections.abc import Sequence

from dpm._constants import CODE_RE, COL_RE, GENERIC_TOC_CODES, ROW_RE
from dpm._text import first_match, norm
from dpm._types import TocEntry, WorkbookCache

LOG = logging.getLogger(__name__)


def discover_toc_sheet(workbook: WorkbookCache) -> str:
    names = workbook.sheet_names
    exact = next((name for name in names if name == "Table of Contents"), None)
    if exact:
        LOG.info("TOC sheet: %s", exact)
        return exact
    case_insensitive = next(
        (name for name in names if name.lower() == "table of contents"), None
    )
    if case_insensitive:
        LOG.info("TOC sheet: %s", case_insensitive)
        return case_insensitive
    raise ValueError('Required TOC sheet "Table of Contents" not found')


def looks_like_perimeter(value: str) -> bool:
    value = norm(value).lower()
    if not value or CODE_RE.search(value) or ROW_RE.search(value) or COL_RE.search(value):
        return False
    forbidden = {
        "perimeter", "scope", "template", "templates", "subtemplate",
        "sub-template", "sheet", "code", "name", "table", "contents",
        "toc", "description",
    }
    return bool(
        value not in forbidden
        and len(value) <= 12
        and re.fullmatch(r"[a-z][a-z0-9_-]{1,11}", value)
    )


def parent_template_code(subtemplate_code: str, current_template: str = "") -> str:
    sub = norm(subtemplate_code).upper()
    cur = norm(current_template).upper()
    if cur and sub.startswith(cur + "."):
        return cur
    parts = sub.split(".")
    return ".".join(parts[:-1]) if len(parts) > 3 else sub


def _find_header_row(rows: list[list[str]]) -> int:
    best_i, best = 0, -1
    for i, row in enumerate(rows[:80]):
        low = [c.lower() for c in row]
        score = 100 if any(c == "template code" for c in low) else 0
        score += 10 if any(c == "description" for c in low) else 0
        score += sum(5 for c in low if looks_like_perimeter(c))
        if score > best:
            best_i, best = i, score
    return best_i


def extract_toc(workbook: WorkbookCache) -> list[TocEntry]:
    toc_sheet = discover_toc_sheet(workbook)
    raw = workbook.matrix(toc_sheet)
    if not raw:
        raise ValueError("TOC sheet is empty")
    for i, row in enumerate(raw[:15], start=1):
        LOG.debug("TOC first lines %s: %s", i, row[:30])

    width = max(len(row) for row in raw)
    rows = [row + [""] * (width - len(row)) for row in raw]
    header_i = _find_header_row(rows)
    headers = rows[header_i]

    code_col = next(
        (i for i, h in enumerate(headers) if h.lower() in {"template code", "template", "code"}),
        -1,
    )
    desc_col = next((i for i, h in enumerate(headers) if h.lower() == "description"), -1)
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

    entries: dict[tuple[str, str, str], TocEntry] = {}
    current_template = ""
    for row in rows[header_i + 1:]:
        row_code = first_match(CODE_RE, row[code_col]) if code_col < len(row) else ""
        if not row_code or row_code in GENERIC_TOC_CODES or row_code.startswith("T.99"):
            continue
        populated: list[tuple[str, str]] = []
        for col, perimeter in perimeter_cols:
            cell_code = first_match(CODE_RE, row[col]) if col < len(row) else ""
            if cell_code and cell_code not in GENERIC_TOC_CODES and not cell_code.startswith("T.99"):
                populated.append((perimeter, cell_code))
        if not populated:
            current_template = row_code
            continue
        for perimeter, subtemplate in populated:
            template = parent_template_code(subtemplate, current_template)
            entries[(perimeter, template, subtemplate)] = TocEntry(perimeter, template, subtemplate)

    if not entries:
        raise ValueError("TOC extraction returned no perimeter/template/subtemplate entries")
    distribution = Counter(e.perimeter for e in entries.values())
    LOG.info("TOC entries=%d perimeters=%s", len(entries), dict(sorted(distribution.items())))
    return sorted(
        entries.values(),
        key=lambda e: (e.perimeter, e.template_code, e.subtemplate_code),
    )


def extract_perimeters(entries: Sequence[TocEntry]) -> set[str]:
    return {e.perimeter for e in entries}


def extract_templates(entries: Sequence[TocEntry]) -> set[tuple[str, str]]:
    return {(e.perimeter, e.template_code) for e in entries}


def extract_subtemplates(entries: Sequence[TocEntry]) -> set[tuple[str, str, str]]:
    return {(e.perimeter, e.template_code, e.subtemplate_code) for e in entries}


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


def select_perimeters_interactively(
    old_entries: Sequence[TocEntry], new_entries: Sequence[TocEntry]
) -> set[str]:
    perimeters = sorted(extract_perimeters(old_entries) | extract_perimeters(new_entries))
    if not perimeters:
        raise ValueError("No perimeters discovered in TOC")
    if sys.stdin.isatty():
        try:
            import questionary
            from questionary import Choice

            choices = [Choice(title=p, value=p, checked=True) for p in perimeters]
            selected = questionary.checkbox("Select reporting perimeters:", choices=choices).ask()
            if selected is None:
                raise KeyboardInterrupt("Perimeter selection cancelled")
            if not selected:
                raise ValueError("At least one perimeter must be selected")
            return {str(p).lower() for p in selected}
        except ImportError:
            pass
    print("\nDetected reporting perimeters:")
    for idx, perimeter in enumerate(perimeters, start=1):
        print(f"  [{idx:02d}] {perimeter}")
    if sys.stdin.isatty():
        print("\nInstall questionary for checkboxes: pip install questionary")
        print("Select by number/name/range, e.g. '1 3-5 ars qrs'. Press Enter for all.")
        return parse_text_selection(input("Perimeters> "), perimeters)
    LOG.warning(
        "Non-interactive terminal detected; selecting all perimeters. "
        "Use --no-interactive-perimeters to make this explicit."
    )
    return {p.lower() for p in perimeters}


def filter_entries_by_perimeter(
    entries: Sequence[TocEntry], selected: set[str]
) -> list[TocEntry]:
    return [e for e in entries if e.perimeter.lower() in selected]
