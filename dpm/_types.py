from __future__ import annotations

from dataclasses import dataclass, field
from typing import TypedDict

import polars as pl


@dataclass(frozen=True)
class StructureEntry:
    """One perimeter → template → subtemplate row of the model's reporting structure."""

    perimeter: str
    template_code: str
    subtemplate_code: str


@dataclass(frozen=True)
class DpmDataset:
    metrics: pl.DataFrame
    entries: list[StructureEntry]
    # Full metric catalogue (metric_code, metric_label) for the metrics delta.
    metric_catalog: pl.DataFrame = field(default_factory=pl.DataFrame)
    # Flat dimensions × members frame for the dimensions delta.
    dimension_members: pl.DataFrame = field(default_factory=pl.DataFrame)

    @property
    def templates(self) -> set[tuple[str, str]]:
        return {(e.perimeter, e.template_code) for e in self.entries}

    @property
    def subtemplates(self) -> set[tuple[str, str, str]]:
        return {
            (e.perimeter, e.template_code, e.subtemplate_code) for e in self.entries
        }


@dataclass(frozen=True)
class DeltaResult:
    """The three-section delta between two DPM versions.

    Each frame follows the canonical snake_case schema in ``dpm.delta_schema``:
    ``structure`` the per-perimeter fact/structural delta (``DELTA_STRUCTURE_COLS``),
    ``metrics`` the metric-catalogue delta (``DELTA_METRIC_COLS``), and
    ``dimensions`` the dimensions/members delta (``DELTA_DIMENSION_COLS``).
    """

    structure: pl.DataFrame
    metrics: pl.DataFrame
    dimensions: pl.DataFrame


class TemplateRow(TypedDict):
    template_code: str
    template_label: str


class SubtemplateRow(TypedDict):
    subtemplate_code: str
    template_code: str
    subtemplate_label: str
    subtemplate_type: str


class PerimeterRow(TypedDict):
    perimeter_code: str


class PerimeterTemplateRow(TypedDict):
    perimeter_code: str
    template_code: str


class MetricRow(TypedDict, total=False):
    metric_code: str
    metric_label: str
    # Enrichment from the DPM database.
    data_type: str | None
    period_type: str | None
    balance: str | None
    referenced_domain: str | None


class FactRow(TypedDict, total=False):
    subtemplate_code: str
    row_code: str
    column_code: str
    row_label: str
    column_label: str
    metric_code: str
    # Canonical Data Point Signature (metric + non-default members).
    data_point_signature: str | None


class DimensionRow(TypedDict, total=False):
    dimension_code: str
    dimension_label: str
    # XBRL code of the dimension's default member.
    default_member_code: str | None


class DimensionMemberRow(TypedDict, total=False):
    member_code: str
    dimension_code: str
    member_label: str
    # True for a domain's default member (omitted from instance contexts).
    is_default: bool


class ModelVersionRow(TypedDict):
    version: str
    from_date: str | None
    to_date: str | None


@dataclass
class ParsedModel:
    """A full DPM model projected into the pivot-schema row lists, ready to insert.

    Produced by the DPM-database projection (:mod:`dpm.dpm_source`), the sole
    ingestion source.
    """

    templates: list[TemplateRow]
    subtemplates: list[SubtemplateRow]
    perimeters: list[PerimeterRow]
    perimeter_template: list[PerimeterTemplateRow]
    metrics: list[MetricRow]
    facts: list[FactRow]
    dimensions: list[DimensionRow]
    dimension_members: list[DimensionMemberRow]
    fact_dimensions: list[FactDimensionRow]
    model_version: list[ModelVersionRow] = field(default_factory=list)


class FactDimensionRow(TypedDict):
    subtemplate_code: str
    row_code: str
    column_code: str
    dimension_code: str
    member_code: str


@dataclass(frozen=True)
class ApplyStats:
    perimeter: str
    facts_before: int
    facts_after: int
    deleted_facts: int
    renamed_facts: int
    deleted_qnames: int
    modified_qnames: int
    repointed_facts: int = 0
    new_contexts: int = 0
    removed_contexts: int = 0
