from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import polars as pl
from openpyxl import load_workbook

from dpm._text import norm


@dataclass(frozen=True)
class TocEntry:
    perimeter: str
    template_code: str
    subtemplate_code: str
    sheet_name: str | None = None


@dataclass(frozen=True)
class DpmDataset:
    metrics: pl.DataFrame
    entries: list[TocEntry]

    @property
    def templates(self) -> set[tuple[str, str]]:
        return {(e.perimeter, e.template_code) for e in self.entries}

    @property
    def subtemplates(self) -> set[tuple[str, str, str]]:
        return {(e.perimeter, e.template_code, e.subtemplate_code) for e in self.entries}


@dataclass
class WorkbookCache:
    path: Path
    wb: Any
    sheet_names: list[str]
    _matrix_cache: dict[str, list[list[str]]] = field(default_factory=dict, repr=False, init=False)

    @classmethod
    def open(cls, path: Path) -> WorkbookCache:
        wb = load_workbook(path, read_only=True, data_only=True)
        return cls(path=path, wb=wb, sheet_names=list(wb.sheetnames))

    def close(self) -> None:
        self.wb.close()

    def matrix(self, sheet_name: str) -> list[list[str]]:
        if sheet_name not in self._matrix_cache:
            ws = self.wb[sheet_name]
            self._matrix_cache[sheet_name] = [
                [norm(v) for v in row] for row in ws.iter_rows(values_only=True)
            ]
        return self._matrix_cache[sheet_name]


@dataclass(frozen=True)
class ApplyStats:
    perimeter: str
    facts_before: int
    facts_after: int
    deleted_facts: int
    renamed_facts: int
    deleted_qnames: int
    modified_qnames: int
