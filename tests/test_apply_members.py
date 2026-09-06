"""Unit tests for exact, per-cell DPS matching in apply-delta.

Facts are matched to model cells by their canonical Data Point Signature — the metric
qname plus the *fixed* (closed, non-default) members — so a metric deleted at one cell
but surviving at another is no longer wiped wholesale (the ``mi363`` over-deletion bug).
"""

from __future__ import annotations

import polars as pl

from dpm.delta_db import save_delta_db
from dpm.delta_schema import DELTA_STRUCTURE_COLS
from dpm._types import DeltaResult
from dpm.xbrl import (
    _decide_fact,
    _match_fixed,
    apply_delta,
    build_dps_delta,
    parse_contexts,
)


def _delta(rows: list[dict[str, str]]) -> pl.DataFrame:
    cols = ["qname_old", "qname_new", "status", "dimensions_old", "dimensions_new"]
    return pl.DataFrame(
        [{c: r.get(c, "") for c in cols} for r in rows],
        schema={c: pl.String for c in cols},
    )


def _s(spec: str) -> frozenset[tuple[str, str]]:
    return frozenset(
        (d.split("=")[0], d.split("=")[1]) for d in spec.split(";") if "=" in d
    )


# ── _match_fixed ─────────────────────────────────────────────────────────────


def test_match_fixed_most_specific_wins():
    # Two nested candidates both subset the fact; the larger (more specific) wins.
    generic = _s("s2c_dim:VG=s2c_AM:x80")
    specific = _s("s2c_dim:VG=s2c_AM:x80;s2c_dim:VI=s2c_VM:x64")
    members = _s("s2c_dim:VG=s2c_AM:x80;s2c_dim:VI=s2c_VM:x64;s2c_dim:OC=s2c_CU:USD")
    open_dims = frozenset({"s2c_dim:OC"})
    assert _match_fixed([generic, specific], members, open_dims) == specific


def test_match_fixed_extra_on_open_axis_allowed():
    fixed = _s("s2c_dim:TB=s2c_LB:x28")
    members = _s("s2c_dim:TB=s2c_LB:x28;s2c_dim:OC=s2c_CU:AFN")
    assert _match_fixed([fixed], members, frozenset({"s2c_dim:OC"})) == fixed


def test_match_fixed_extra_on_closed_axis_rejected():
    # An extra member on a non-open dimension means this is a different cell — no match.
    fixed = _s("s2c_dim:TB=s2c_LB:x28")
    members = _s("s2c_dim:TB=s2c_LB:x28;s2c_dim:BL=s2c_LB:x91")
    assert _match_fixed([fixed], members, frozenset()) is None


# ── build_dps_delta ──────────────────────────────────────────────────────────


def test_survivor_spans_all_new_side_rows():
    # A cell that moved row/column shows as Deleted(old) + Added(new); its signature
    # must still count as a survivor so the fact is kept, not deleted.
    delta = _delta(
        [
            {"qname_old": "s2md_met:mi1", "status": "Deleted",
             "dimensions_old": "s2c_dim:VG=s2c_AM:x80"},
            {"qname_new": "s2md_met:mi1", "status": "Added",
             "dimensions_new": "s2c_dim:VG=s2c_AM:x80"},
        ]
    )
    dps = build_dps_delta(delta)
    assert _s("s2c_dim:VG=s2c_AM:x80") in dps.survivor["s2md_met:mi1"]
    # not left in the deleted set — it survives elsewhere
    assert dps.deleted.get("s2md_met:mi1", []) == []


def test_modified_conflict_dropped():
    delta = _delta(
        [
            {"qname_old": "s2md_met:mi1", "qname_new": "s2md_met:mi1", "status": "Modified",
             "dimensions_old": "s2c_dim:VG=s2c_AM:x80", "dimensions_new": "s2c_dim:VG=s2c_AM:x81"},
            {"qname_old": "s2md_met:mi1", "qname_new": "s2md_met:mi1", "status": "Modified",
             "dimensions_old": "s2c_dim:VG=s2c_AM:x80", "dimensions_new": "s2c_dim:VG=s2c_AM:x82"},
        ]
    )
    dps = build_dps_delta(delta)
    assert dps.modified.get("s2md_met:mi1", []) == []  # ambiguous link dropped


# ── _decide_fact ─────────────────────────────────────────────────────────────


def _decide(qname, members, delta, *, open_dims="", defaults=""):
    return _decide_fact(
        qname,
        _s(members),
        build_dps_delta(delta),
        frozenset(open_dims.split(",")) if open_dims else frozenset(),
        frozenset(defaults.split(",")) if defaults else frozenset(),
    )


def test_kept_signature_is_untouched():
    delta = _delta([{"qname_old": "s2md_met:mi1", "qname_new": "s2md_met:mi1",
                     "status": "Kept", "dimensions_old": "s2c_dim:VG=s2c_AM:x80",
                     "dimensions_new": "s2c_dim:VG=s2c_AM:x80"}])
    assert _decide("s2md_met:mi1", "s2c_dim:VG=s2c_AM:x80", delta) is None


def test_gone_signature_deleted():
    delta = _delta([{"qname_old": "s2md_met:mi1", "status": "Deleted",
                     "dimensions_old": "s2c_dim:VG=s2c_AM:x80"}])
    dec = _decide("s2md_met:mi1", "s2c_dim:VG=s2c_AM:x80", delta)
    assert dec is not None and dec.delete


def test_default_member_stripped_before_match():
    # The instance carries a default member (x0) the model signature omits; stripping it
    # lets the deleted signature match.
    delta = _delta([{"qname_old": "s2md_met:mi1", "status": "Deleted",
                     "dimensions_old": "s2c_dim:VG=s2c_AM:x80"}])
    dec = _decide(
        "s2md_met:mi1",
        "s2c_dim:VG=s2c_AM:x80;s2c_dim:RT=s2c_RT:x0",
        delta,
        defaults="s2c_RT:x0",
    )
    assert dec is not None and dec.delete


def test_modified_repoints_keeping_other_members():
    # VI x64→x116 while VG=x80 (a fixed member the delta doesn't vary) is preserved.
    delta = _delta([{"qname_old": "s2md_met:mi363", "qname_new": "s2md_met:mi363",
                     "status": "Modified",
                     "dimensions_old": "s2c_dim:VG=s2c_AM:x80;s2c_dim:VI=s2c_VM:x64",
                     "dimensions_new": "s2c_dim:VG=s2c_AM:x80;s2c_dim:VI=s2c_VM:x116"}])
    dec = _decide("s2md_met:mi363", "s2c_dim:VG=s2c_AM:x80;s2c_dim:VI=s2c_VM:x64", delta)
    assert dec is not None and not dec.delete
    assert dec.new_members == _s("s2c_dim:VG=s2c_AM:x80;s2c_dim:VI=s2c_VM:x116")


def test_rename_only_changes_qname():
    delta = _delta([{"qname_old": "s2md_met:mi1", "qname_new": "s2md_met:mi9",
                     "status": "Modified", "dimensions_old": "s2c_dim:VG=s2c_AM:x80",
                     "dimensions_new": "s2c_dim:VG=s2c_AM:x80"}])
    dec = _decide("s2md_met:mi1", "s2c_dim:VG=s2c_AM:x80", delta)
    assert dec is not None and not dec.delete
    assert dec.new_qname == "s2md_met:mi9" and dec.new_members is None


def test_unknown_signature_kept():
    delta = _delta([{"qname_old": "s2md_met:mi1", "status": "Deleted",
                     "dimensions_old": "s2c_dim:VG=s2c_AM:x80"}])
    # different member set — not the deleted cell, so keep
    assert _decide("s2md_met:mi1", "s2c_dim:VG=s2c_AM:x81", delta) is None


# ── mi363 golden ─────────────────────────────────────────────────────────────


def test_mi363_golden_keep_repoint_delete():
    """The headline case: BL/VG kept, VI x64→x116 re-pointed, IZ/VL deleted."""
    delta = _delta(
        [
            # a surviving BL/VG cell
            {"qname_old": "s2md_met:mi363", "qname_new": "s2md_met:mi363", "status": "Kept",
             "dimensions_old": "s2c_dim:BL=s2c_LB:x70;s2c_dim:VG=s2c_AM:x80",
             "dimensions_new": "s2c_dim:BL=s2c_LB:x70;s2c_dim:VG=s2c_AM:x80"},
            # a re-pointed VI cell
            {"qname_old": "s2md_met:mi363", "qname_new": "s2md_met:mi363", "status": "Modified",
             "dimensions_old": "s2c_dim:VG=s2c_AM:x80;s2c_dim:VI=s2c_VM:x64",
             "dimensions_new": "s2c_dim:VG=s2c_AM:x80;s2c_dim:VI=s2c_VM:x116"},
            # a deleted IZ/VL cell
            {"qname_old": "s2md_met:mi363", "status": "Deleted",
             "dimensions_old": "s2c_dim:IZ=s2c_RT:x1;s2c_dim:VG=s2c_AM:x80;s2c_dim:VL=s2c_VM:x5"},
        ]
    )
    keep = _decide("s2md_met:mi363", "s2c_dim:BL=s2c_LB:x70;s2c_dim:VG=s2c_AM:x80", delta)
    repoint = _decide("s2md_met:mi363", "s2c_dim:VG=s2c_AM:x80;s2c_dim:VI=s2c_VM:x64", delta)
    delete = _decide(
        "s2md_met:mi363",
        "s2c_dim:IZ=s2c_RT:x1;s2c_dim:VG=s2c_AM:x80;s2c_dim:VL=s2c_VM:x5",
        delta,
    )
    assert keep is None  # BL/VG survives → untouched
    assert repoint is not None and not repoint.delete
    assert repoint.new_members == _s("s2c_dim:VG=s2c_AM:x80;s2c_dim:VI=s2c_VM:x116")
    assert delete is not None and delete.delete  # IZ/VL truly removed


# ── apply_delta end-to-end ───────────────────────────────────────────────────


def _ctx(cid: str, members: str, typed: str = "") -> bytes:
    explicit = "".join(
        f'<xbrldi:explicitMember dimension="{d}">{m}</xbrldi:explicitMember>'
        for d, m in (p.split("=") for p in members.split(";") if p)
    )
    return (
        f'<xbrli:context id="{cid}">'
        f"<xbrli:entity><xbrli:identifier>LEI</xbrli:identifier></xbrli:entity>"
        f"<xbrli:period><xbrli:instant>2025-12-31</xbrli:instant></xbrli:period>"
        f"<xbrli:scenario>{typed}{explicit}</xbrli:scenario>"
        f"</xbrli:context>"
    ).encode()


def _fact(qname: str, ref: str, value: str = "1") -> bytes:
    return (
        f'<s2md_met:{qname} contextRef="{ref}" unitRef="u" decimals="0">{value}'
        f"</s2md_met:{qname}>"
    ).encode()


def _doc(*bodies: bytes) -> bytes:
    return (
        b'<xbrli:xbrl xmlns:xbrli="x" xmlns:xbrldi="d" xmlns:s2md_met="m" xmlns:find="f">'
        b'<link:schemaRef xlink:href="http://x/mod/ars.xsd"/>'
        + b"".join(bodies)
        + b"</xbrli:xbrl>"
    )


def _filing_indicator(ref: str, code: str = "S.01.01") -> bytes:
    return f'<find:filingIndicator contextRef="{ref}">{code}</find:filingIndicator>'.encode()


def _structure(rows: list[dict[str, str]]) -> pl.DataFrame:
    return pl.DataFrame(
        [{c: r.get(c, "") for c in DELTA_STRUCTURE_COLS} | {"type": "Row"} for r in rows],
        schema={c: pl.String for c in DELTA_STRUCTURE_COLS},
    )


def _write_delta_db(tmp_path, rows, *, open_dims, defaults):
    from dpm.workflows import _build_datapoint_changes

    path = tmp_path / "delta.duckdb"
    structure = _structure(rows)
    save_delta_db(
        DeltaResult(
            structure=structure,
            metrics=pl.DataFrame(),
            dimensions=pl.DataFrame(),
        ),
        path,
        "old",
        "new",
        open_dimensions=open_dims,
        default_members=defaults,
        datapoint_changes=_build_datapoint_changes(structure),
    )
    return path


def test_apply_delta_deletes_renames_repoints(tmp_path):
    xml = _doc(
        _ctx("c1", "s2c_dim:BL=s2c_LB:x70;s2c_dim:VG=s2c_AM:x80"),  # survivor → keep
        _ctx("c2", "s2c_dim:VG=s2c_AM:x80;s2c_dim:VI=s2c_VM:x64"),  # modified → repoint
        _ctx("c3", "s2c_dim:IZ=s2c_RT:x1;s2c_dim:VG=s2c_AM:x80"),   # deleted
        _fact("mi363", "c1", "10"),
        _fact("mi363", "c2", "20"),
        _fact("mi363", "c3", "30"),
        _fact("mi1", "c1", "40"),  # rename target
    )
    inp = tmp_path / "in.xbrl"
    inp.write_bytes(xml)
    delta = _write_delta_db(
        tmp_path,
        [
            {"perimeter": "ars", "qname_old": "s2md_met:mi363", "qname_new": "s2md_met:mi363",
             "status": "Kept", "dimensions_old": "s2c_dim:BL=s2c_LB:x70;s2c_dim:VG=s2c_AM:x80",
             "dimensions_new": "s2c_dim:BL=s2c_LB:x70;s2c_dim:VG=s2c_AM:x80"},
            {"perimeter": "ars", "qname_old": "s2md_met:mi363", "qname_new": "s2md_met:mi363",
             "status": "Modified",
             "dimensions_old": "s2c_dim:VG=s2c_AM:x80;s2c_dim:VI=s2c_VM:x64",
             "dimensions_new": "s2c_dim:VG=s2c_AM:x80;s2c_dim:VI=s2c_VM:x116"},
            {"perimeter": "ars", "qname_old": "s2md_met:mi363", "status": "Deleted",
             "dimensions_old": "s2c_dim:IZ=s2c_RT:x1;s2c_dim:VG=s2c_AM:x80"},
            {"perimeter": "ars", "qname_old": "s2md_met:mi1", "qname_new": "s2md_met:mi9",
             "status": "Modified", "dimensions_old": "s2c_dim:BL=s2c_LB:x70;s2c_dim:VG=s2c_AM:x80",
             "dimensions_new": "s2c_dim:BL=s2c_LB:x70;s2c_dim:VG=s2c_AM:x80"},
        ],
        open_dims=[],
        defaults=[],
    )
    out = tmp_path / "out.xbrl"
    stats = apply_delta(delta, inp, out, "ars", dry_run=False, debug_xlsx=None)

    assert stats.deleted_facts == 1  # only the IZ cell
    assert stats.renamed_facts == 1  # mi1 → mi9
    assert stats.repointed_facts == 1  # VI x64 cell

    result = out.read_bytes()
    assert b"contextRef=\"c3\"" not in result  # deleted fact's context ref gone (fact removed)
    assert b'<s2md_met:mi363 contextRef="c1"' in result  # survivor untouched
    assert b'<xbrli:context id="c1"' in result  # survivor's context kept
    assert b"s2md_met:mi9" in result  # rename applied
    # the re-pointed fact moved to a new context carrying VI=x116
    ctxs = parse_contexts(result)
    repointed_ref = result.split(b'>20</s2md_met:mi363>')[0].split(b'contextRef="')[-1].split(b'"')[0].decode()
    assert ctxs[repointed_ref].members == _s("s2c_dim:VG=s2c_AM:x80;s2c_dim:VI=s2c_VM:x116")


def test_apply_delta_no_over_deletion_of_shared_metric(tmp_path):
    # The mi363 regression: a metric deleted at ONE cell must not delete a fact of the
    # SAME metric at a surviving cell.
    xml = _doc(
        _ctx("c1", "s2c_dim:BL=s2c_LB:x70;s2c_dim:VG=s2c_AM:x80"),  # survives
        _ctx("c2", "s2c_dim:IZ=s2c_RT:x1;s2c_dim:VG=s2c_AM:x80"),   # deleted
        _fact("mi363", "c1", "10"),
        _fact("mi363", "c2", "20"),
    )
    inp = tmp_path / "in.xbrl"
    inp.write_bytes(xml)
    delta = _write_delta_db(
        tmp_path,
        [
            {"perimeter": "ars", "qname_old": "s2md_met:mi363", "qname_new": "s2md_met:mi363",
             "status": "Kept", "dimensions_old": "s2c_dim:BL=s2c_LB:x70;s2c_dim:VG=s2c_AM:x80",
             "dimensions_new": "s2c_dim:BL=s2c_LB:x70;s2c_dim:VG=s2c_AM:x80"},
            {"perimeter": "ars", "qname_old": "s2md_met:mi363", "status": "Deleted",
             "dimensions_old": "s2c_dim:IZ=s2c_RT:x1;s2c_dim:VG=s2c_AM:x80"},
        ],
        open_dims=[],
        defaults=[],
    )
    out = tmp_path / "out.xbrl"
    stats = apply_delta(delta, inp, out, "ars", dry_run=False, debug_xlsx=None)
    assert stats.deleted_facts == 1  # ONLY the IZ fact, not both
    assert b'<s2md_met:mi363 contextRef="c1"' in out.read_bytes()  # survivor kept


def test_apply_delta_removes_context_orphaned_by_deletion(tmp_path):
    # A context referenced only by a deleted fact must be pruned from the output.
    xml = _doc(
        _ctx("c1", "s2c_dim:BL=s2c_LB:x70;s2c_dim:VG=s2c_AM:x80"),  # survives
        _ctx("c2", "s2c_dim:IZ=s2c_RT:x1;s2c_dim:VG=s2c_AM:x80"),   # deleted → orphaned
        _fact("mi363", "c1", "10"),
        _fact("mi363", "c2", "20"),
    )
    inp = tmp_path / "in.xbrl"
    inp.write_bytes(xml)
    delta = _write_delta_db(
        tmp_path,
        [
            {"perimeter": "ars", "qname_old": "s2md_met:mi363", "qname_new": "s2md_met:mi363",
             "status": "Kept", "dimensions_old": "s2c_dim:BL=s2c_LB:x70;s2c_dim:VG=s2c_AM:x80",
             "dimensions_new": "s2c_dim:BL=s2c_LB:x70;s2c_dim:VG=s2c_AM:x80"},
            {"perimeter": "ars", "qname_old": "s2md_met:mi363", "status": "Deleted",
             "dimensions_old": "s2c_dim:IZ=s2c_RT:x1;s2c_dim:VG=s2c_AM:x80"},
        ],
        open_dims=[],
        defaults=[],
    )
    out = tmp_path / "out.xbrl"
    stats = apply_delta(delta, inp, out, "ars", dry_run=False, debug_xlsx=None)
    result = out.read_bytes()
    assert stats.removed_contexts == 1
    assert b'<xbrli:context id="c2"' not in result  # orphaned context pruned
    assert b'<xbrli:context id="c1"' in result       # survivor's context kept


def test_apply_delta_removes_context_orphaned_by_repoint(tmp_path):
    # A context whose only fact is re-pointed to a cloned context becomes orphaned.
    xml = _doc(
        _ctx("c2", "s2c_dim:VG=s2c_AM:x80;s2c_dim:VI=s2c_VM:x64"),  # modified → repoint
        _fact("mi363", "c2", "20"),
    )
    inp = tmp_path / "in.xbrl"
    inp.write_bytes(xml)
    delta = _write_delta_db(
        tmp_path,
        [
            {"perimeter": "ars", "qname_old": "s2md_met:mi363", "qname_new": "s2md_met:mi363",
             "status": "Modified",
             "dimensions_old": "s2c_dim:VG=s2c_AM:x80;s2c_dim:VI=s2c_VM:x64",
             "dimensions_new": "s2c_dim:VG=s2c_AM:x80;s2c_dim:VI=s2c_VM:x116"},
        ],
        open_dims=[],
        defaults=[],
    )
    out = tmp_path / "out.xbrl"
    stats = apply_delta(delta, inp, out, "ars", dry_run=False, debug_xlsx=None)
    result = out.read_bytes()
    assert stats.repointed_facts == 1
    assert stats.new_contexts == 1
    assert stats.removed_contexts == 1
    assert b'<xbrli:context id="c2"' not in result  # source context orphaned & pruned
    # the cloned context carrying the new member set survives (fact points at it)
    ctxs = parse_contexts(result)
    assert any(
        info.members == _s("s2c_dim:VG=s2c_AM:x80;s2c_dim:VI=s2c_VM:x116")
        for info in ctxs.values()
    )


def test_apply_delta_keeps_context_used_only_by_filing_indicator(tmp_path):
    # A context with no metric fact but referenced by a filing indicator must NOT be pruned.
    xml = _doc(
        _ctx("c1", "s2c_dim:BL=s2c_LB:x70;s2c_dim:VG=s2c_AM:x80"),
        _ctx("cfi", ""),  # no metric fact; only the filing indicator references it
        _filing_indicator("cfi"),
        _fact("mi363", "c1", "10"),
    )
    inp = tmp_path / "in.xbrl"
    inp.write_bytes(xml)
    delta = _write_delta_db(
        tmp_path,
        [
            {"perimeter": "ars", "qname_old": "s2md_met:mi363", "qname_new": "s2md_met:mi363",
             "status": "Kept", "dimensions_old": "s2c_dim:BL=s2c_LB:x70;s2c_dim:VG=s2c_AM:x80",
             "dimensions_new": "s2c_dim:BL=s2c_LB:x70;s2c_dim:VG=s2c_AM:x80"},
        ],
        open_dims=[],
        defaults=[],
    )
    out = tmp_path / "out.xbrl"
    stats = apply_delta(delta, inp, out, "ars", dry_run=False, debug_xlsx=None)
    result = out.read_bytes()
    assert stats.removed_contexts == 0
    assert b'<xbrli:context id="cfi"' in result  # kept: still used by filing indicator


# ── DPS pivot (datapoint_changes) ────────────────────────────────────────────


def test_dps_delta_row_trip():
    """Flatten→persist→reassemble reproduces the resolved DpsDelta exactly."""
    from dpm.xbrl import build_dps_delta, dps_delta_to_rows, rows_to_dps_delta
    from dpm.delta_schema import DATAPOINT_CHANGE_COLS

    delta = _delta(
        [
            {"qname_old": "s2md_met:mi1", "qname_new": "s2md_met:mi1", "status": "Modified",
             "dimensions_old": "s2c_dim:VI=s2c_VM:x64", "dimensions_new": "s2c_dim:VI=s2c_VM:x116"},
            {"qname_old": "s2md_met:mi2", "status": "Deleted", "dimensions_old": "s2c_dim:VG=s2c_AM:x80"},
            {"qname_new": "s2md_met:mi3", "status": "Added", "dimensions_new": "s2c_dim:BL=s2c_LB:x1"},
        ]
    )
    dps = build_dps_delta(delta)
    rows = pl.DataFrame(
        dps_delta_to_rows("ars", dps), schema={c: pl.String for c in DATAPOINT_CHANGE_COLS}
    )

    def _norm(d):
        return (
            {q: sorted(sorted(f) for f in v) for q, v in d.survivor.items()},
            {q: sorted((sorted(o), nq, sorted(nf)) for o, nq, nf in v) for q, v in d.modified.items()},
            {q: sorted(sorted(f) for f in v) for q, v in d.deleted.items()},
        )

    assert _norm(rows_to_dps_delta(rows)) == _norm(dps)


def test_persisted_pivot_keeps_row_column_move(tmp_path):
    """A data point that only moved row/column (Deleted old cell + Added new cell, same
    signature) survives — the case the cell-keyed format reports as Deleted+Added."""
    xml = _doc(
        _ctx("c1", "s2c_dim:VG=s2c_AM:x80"),
        _fact("mi1", "c1", "10"),
    )
    inp = tmp_path / "in.xbrl"
    inp.write_bytes(xml)
    delta = _write_delta_db(
        tmp_path,
        [
            {"perimeter": "ars", "qname_old": "s2md_met:mi1", "status": "Deleted",
             "dimensions_old": "s2c_dim:VG=s2c_AM:x80"},   # old cell (row R0010)
            {"perimeter": "ars", "qname_new": "s2md_met:mi1", "status": "Added",
             "dimensions_new": "s2c_dim:VG=s2c_AM:x80"},   # same signature, new cell (row R0020)
        ],
        open_dims=[],
        defaults=[],
    )
    out = tmp_path / "out.xbrl"
    stats = apply_delta(delta, inp, out, "ars", dry_run=False, debug_xlsx=None)
    assert stats.deleted_facts == 0  # survives — not deleted
    assert b'<s2md_met:mi1 contextRef="c1"' in out.read_bytes()


# ── debug workbook ──────────────────────────────────────────────────────────


def test_debug_workbook_sheets_and_nesting(tmp_path):
    import openpyxl

    from dpm._types import ApplyStats
    from dpm.excel import generate_apply_debug_workbook

    stats = ApplyStats(
        perimeter="ars", facts_before=10, facts_after=9, deleted_facts=1,
        renamed_facts=1, deleted_qnames=1, modified_qnames=1,
        repointed_facts=2, new_contexts=1,
    )
    deleted = pl.DataFrame({"qname": ["s2md_met:mi1"], "value": ["5"]})
    renamed = pl.DataFrame(
        {"qname": ["s2md_met:mi2"], "qname_new": ["s2md_met:mi9"], "value": ["7"]}
    )
    repointed = pl.DataFrame(
        {"qname": ["s2md_met:mi3", "s2md_met:mi3"], "context_old": ["c3", "c4"],
         "context_new": ["cdpm1", "cdpm1"],
         "dimensions_old": ["s2c_dim:RT=s2c_RT:x148"] * 2,
         "dimensions_new": ["s2c_dim:RT=s2c_RT:x355"] * 2, "value": ["1", "2"]}
    )
    new_contexts = pl.DataFrame(
        {"context_id": ["cdpm1"], "dimensions": ["s2c_dim:RT=s2c_RT:x355"], "cloned_from": ["c3"]}
    )

    out = tmp_path / "debug.xlsx"
    generate_apply_debug_workbook(
        out, stats=stats, deleted=deleted, renamed=renamed,
        repointed=repointed, new_contexts=new_contexts,
    )

    wb = openpyxl.load_workbook(out)
    assert wb.sheetnames == [
        "Summary", "Deleted metrics", "Renamed metrics", "Re-pointed cells", "New contexts",
    ]
    dl = wb["Deleted metrics"]
    assert [dl["A2"].value, dl["B2"].value] == ["s2md_met:mi1", 1] and dl.max_row == 2
    rn = wb["Renamed metrics"]
    assert [rn["A2"].value, rn["B2"].value] == ["s2md_met:mi2 → s2md_met:mi9", 1]
    assert rn.max_row == 2
    ws = wb["Re-pointed cells"]
    assert ws["A2"].value == "s2md_met:mi3" and ws["B2"].value == "2 facts"
    assert ws.row_dimensions[3].outline_level == 1
    assert ws.row_dimensions[4].outline_level == 1
    assert "cdpm1" in str(ws["B3"].value)
    assert wb["Summary"]["A2"].value == "ars"
