"""Ingest the official EIOPA DPM SQLite database into the thin pivot schema.

This is the sole ingestion path. The DPM database is a normalised, XBRL-oriented model
of ~30 tables; we project the subset the app needs into the same nine pivot tables
(:mod:`dpm.db`), enriched with two pieces only the database carries —
**default members** (``mMember.IsDefaultMember``) and a **canonical Data Point
Signature** per cell (``mTableCell.DPS``) — plus metric semantics and a version row.

Key semantics verified against the 2.10.0 database:

* Perimeters are ``mModule.ModuleCode`` (ars, qrs, …).
* A cell's ``DPS`` already omits default members and lists non-default members
  explicitly, e.g. ``MET(s2md_met:mi343)|s2c_dim:BL(s2c_LB:x91)|s2c_dim:VG(s2c_AM:x80)``;
  ``(*)`` marks an open/typed dimension. ``VG=x80`` is a *non-default* member (VG's
  domain AM defaults to ``s2c_AM:x0``), so it correctly stays in the signature.
* Metric qnames are stored upper-cased (``s2md_MET:``) in ``mMember`` but the DPS
  and XBRL instances use ``s2md_met:`` — we normalise to the lower-case form.
"""

from __future__ import annotations

import logging
import re
import sqlite3
from collections import defaultdict
from collections.abc import Callable
from pathlib import Path

from dpm._types import (
    DimensionMemberRow,
    DimensionRow,
    FactDimensionRow,
    FactRow,
    MetricRow,
    ModelVersionRow,
    ParsedModel,
    PerimeterRow,
    PerimeterTemplateRow,
    SubtemplateRow,
    TemplateRow,
)

LOG = logging.getLogger(__name__)

# A DPS component: ``PREFIX(VALUE)`` — the first is ``MET(qname)``, the rest are
# ``s2c_dim:XX(s2c_YY:zN)`` members (or ``(*)``/``(*[..])`` for open/typed dims).
_DPS_COMPONENT_RE = re.compile(r"([^|(]+)\(([^)]*)\)")
# A concrete domain member value, e.g. ``s2c_AM:x80`` — excludes open/typed
# wildcards (``*``, ``*[..]``) whose value is a literal, not a member.
_MEMBER_VALUE_RE = re.compile(r"^s2c_[A-Za-z0-9]+:x\d+$")


def _norm_metric(code: str) -> str:
    """Normalise a metric qname to the lower-case ``s2md_met:`` form used by DPS/XBRL."""
    return code.replace("s2md_MET:", "s2md_met:")


def _template_code(subtemplate_code: str) -> str:
    """Parent template code of a table code, e.g. ``S.02.01.01.01`` → ``S.02.01.01``."""
    return subtemplate_code.rsplit(".", 1)[0]


def parse_dps_members(dps: str) -> list[tuple[str, str]]:
    """Return the *fixed* ``(dimension_code, member_code)`` pairs in a DPS.

    The leading ``MET(...)`` metric is skipped, as are open-axis dimensions whose
    value is a restriction descriptor rather than a single member — ``(*)`` (fully
    open) and ``(*[dom;start;incl])`` / ``(*?[…])`` (open over a restricted domain
    subtree, the ``?`` marking default-omission). Those carry no fixed member: the
    instance supplies the concrete one, and the descriptor is preserved verbatim on
    ``facts.data_point_signature``. What remains is exactly the non-default members
    that pin the cell down.
    """
    pairs: list[tuple[str, str]] = []
    for prefix, value in _DPS_COMPONENT_RE.findall(dps):
        prefix = prefix.strip()
        if prefix == "MET" or not _MEMBER_VALUE_RE.match(value):
            continue
        pairs.append((prefix, value))
    return pairs


def parse_dps_metric(dps: str) -> str | None:
    """Return the normalised metric qname from a DPS ``MET(...)`` head, if present."""
    m = _DPS_COMPONENT_RE.match(dps)
    if m and m.group(1).strip() == "MET":
        return _norm_metric(m.group(2).strip())
    return None


def _step(on_step: Callable[[str], None] | None, label: str) -> None:
    if on_step:
        on_step(label)


def parse_dpm_database(
    db_path: Path,
    version: str,
    on_step: Callable[[str], None] | None = None,
) -> ParsedModel:
    """Project the official DPM SQLite at ``db_path`` into the pivot schema."""
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        return _project(conn, version, on_step)
    finally:
        conn.close()


def _project(
    conn: sqlite3.Connection,
    version: str,
    on_step: Callable[[str], None] | None,
) -> ParsedModel:
    def q(sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        return conn.execute(sql, params).fetchall()

    _step(on_step, "Reading perimeters…")
    perimeters: list[PerimeterRow] = [
        {"perimeter_code": r["ModuleCode"]}
        for r in q("SELECT ModuleCode FROM mModule ORDER BY ModuleCode")
    ]

    _step(on_step, "Reading templates…")
    templates: list[TemplateRow] = [
        {"template_code": r["TemplateOrTableCode"], "template_label": r["TemplateOrTableLabel"]}
        for r in q(
            "SELECT TemplateOrTableCode, TemplateOrTableLabel FROM mTemplateOrTable "
            "WHERE TemplateOrTableType = 'TableGroup'"
        )
    ]

    _step(on_step, "Reading subtemplates…")
    # Facts key on real data tables (mTable); its parent 4-part code is the template.
    tables = q("SELECT TableID, TableCode, TableLabel FROM mTable")
    table_code: dict[int, str] = {r["TableID"]: r["TableCode"] for r in tables}
    # subtemplate_type ('metrics_row'/'metrics_col') is filled after facts are built.
    subtemplate_label: dict[str, str] = {r["TableCode"]: r["TableLabel"] for r in tables}

    _step(on_step, "Reading perimeter–template map…")
    perimeter_template: list[PerimeterTemplateRow] = [
        {"perimeter_code": r["ModuleCode"], "template_code": r["TemplateOrTableCode"]}
        for r in q(
            "SELECT mo.ModuleCode AS ModuleCode, tt.TemplateOrTableCode AS TemplateOrTableCode "
            "FROM mModuleBusinessTemplate mbt "
            "JOIN mModule mo ON mo.ModuleID = mbt.ModuleID "
            "JOIN mTemplateOrTable tt ON tt.TemplateOrTableID = mbt.BusinessTemplateID "
            "WHERE tt.TemplateOrTableType = 'TableGroup'"
        )
    ]

    _step(on_step, "Reading metrics…")
    metrics: dict[str, MetricRow] = {}
    for r in q(
        "SELECT mb.MemberXBRLCode AS code, mb.MemberLabel AS label, "
        "mt.DataType AS data_type, mt.FlowType AS flow, mt.BalanceType AS balance, "
        "dom.DomainXBRLCode AS ref_domain "
        "FROM mMetric mt "
        "JOIN mMember mb ON mb.MemberID = mt.CorrespondingMemberID "
        "LEFT JOIN mDomain dom ON dom.DomainID = mt.ReferencedDomainID"
    ):
        code = _norm_metric(r["code"])
        metrics[code] = {
            "metric_code": code,
            "metric_label": r["label"],
            "data_type": r["data_type"],
            "period_type": r["flow"],
            "balance": r["balance"],
            "referenced_domain": r["ref_domain"],
        }

    _step(on_step, "Reading dimensions…")
    # A dimension's default member is its explicit DefaultMemberID, else the
    # IsDefaultMember of its domain.
    domain_default: dict[int, str] = {
        r["DomainID"]: r["MemberXBRLCode"]
        for r in q("SELECT DomainID, MemberXBRLCode FROM mMember WHERE IsDefaultMember = 1")
    }
    member_xbrl: dict[int, str] = {
        r["MemberID"]: r["MemberXBRLCode"]
        for r in q("SELECT MemberID, MemberXBRLCode FROM mMember")
    }
    dimensions: list[DimensionRow] = []
    for r in q(
        "SELECT DimensionXBRLCode, DimensionLabel, DomainID, DefaultMemberID FROM mDimension"
    ):
        default = None
        if r["DefaultMemberID"] is not None:
            default = member_xbrl.get(r["DefaultMemberID"])
        if default is None:
            default = domain_default.get(r["DomainID"])
        dimensions.append(
            {
                "dimension_code": r["DimensionXBRLCode"],
                "dimension_label": r["DimensionLabel"],
                "default_member_code": default,
            }
        )

    _step(on_step, "Reading dimension members…")
    # Members belong to domains; map each to a representative dimension of that
    # domain (deterministic min DimensionID) so the explorer can group them. The
    # is_default flag is domain-level and independent of that choice.
    dimension_members: list[DimensionMemberRow] = [
        {
            "member_code": r["MemberXBRLCode"],
            "dimension_code": r["DimensionXBRLCode"],
            "member_label": r["MemberLabel"],
            "is_default": bool(r["IsDefaultMember"]),
        }
        for r in q(
            "SELECT m.MemberXBRLCode, m.MemberLabel, m.IsDefaultMember, dim.DimensionXBRLCode "
            "FROM mMember m "
            "JOIN (SELECT DomainID, MIN(DimensionID) AS did FROM mDimension GROUP BY DomainID) dd "
            "  ON dd.DomainID = m.DomainID "
            "JOIN mDimension dim ON dim.DimensionID = dd.did"
        )
    ]

    _step(on_step, "Reading cells (facts + signatures)…")
    # Each cell's row/column codes come from its ordinates' axis orientation.
    cell_pos: dict[int, dict[str, tuple[str, str]]] = defaultdict(dict)
    for r in q(
        "SELECT cp.CellID AS cid, ax.AxisOrientation AS o, "
        "ao.OrdinateCode AS code, ao.OrdinateLabel AS label "
        "FROM mCellPosition cp "
        "JOIN mAxisOrdinate ao ON ao.OrdinateID = cp.OrdinateID "
        "JOIN mAxis ax ON ax.AxisID = ao.AxisID"
    ):
        cell_pos[r["cid"]][r["o"]] = (r["code"] or "", r["label"] or "")

    facts: dict[tuple[str, str, str], FactRow] = {}
    fact_dimensions: list[FactDimensionRow] = []
    for r in q("SELECT CellID, TableID, DPS FROM mTableCell WHERE DPS IS NOT NULL"):
        subtemplate = table_code.get(r["TableID"])
        if subtemplate is None:
            continue
        dps = r["DPS"]
        metric = parse_dps_metric(dps)
        if metric is None:
            continue
        pos = cell_pos.get(r["CellID"], {})
        row_code, row_label = pos.get("Y", ("", ""))
        column_code, column_label = pos.get("X", ("", ""))
        key = (subtemplate, row_code, column_code)
        facts[key] = {
            "subtemplate_code": subtemplate,
            "row_code": row_code,
            "column_code": column_code,
            "row_label": row_label,
            "column_label": column_label,
            "metric_code": metric,
            "data_point_signature": dps,
        }
        for dim_code, member_code in parse_dps_members(dps):
            fact_dimensions.append(
                {
                    "subtemplate_code": subtemplate,
                    "row_code": row_code,
                    "column_code": column_code,
                    "dimension_code": dim_code,
                    "member_code": member_code,
                }
            )
        # A metric referenced by a cell but absent from mMetric (rare) still needs a row.
        metrics.setdefault(
            metric,
            {
                "metric_code": metric,
                "metric_label": None,
                "data_type": None,
                "period_type": None,
                "balance": None,
                "referenced_domain": None,
            },
        )

    # Derive subtemplate_type from the cell layout: metrics vary across columns
    # (metrics_col) unless the table is a single-column row list (metrics_row).
    cols_per_sub: dict[str, set[str]] = defaultdict(set)
    for (sub, _row, col) in facts:
        cols_per_sub[sub].add(col)
    subtemplates: list[SubtemplateRow] = [
        {
            "subtemplate_code": code,
            "template_code": _template_code(code),
            "subtemplate_label": subtemplate_label.get(code, ""),
            "subtemplate_type": "metrics_col" if len(cols_per_sub.get(code, set())) > 1 else "metrics_row",
        }
        for code in table_code.values()
    ]

    _step(on_step, "Reading model version…")
    tax = q("SELECT Version, FromDate, ToDate FROM mTaxonomy LIMIT 1")
    from_date = tax[0]["FromDate"] if tax else None
    to_date = tax[0]["ToDate"] if tax else None
    model_version: list[ModelVersionRow] = [
        {"version": version, "from_date": from_date, "to_date": to_date}
    ]

    return ParsedModel(
        templates=templates,
        subtemplates=subtemplates,
        perimeters=perimeters,
        perimeter_template=perimeter_template,
        metrics=list(metrics.values()),
        facts=list(facts.values()),
        dimensions=dimensions,
        dimension_members=dimension_members,
        fact_dimensions=fact_dimensions,
        model_version=model_version,
    )
