from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import NamedTuple

import polars as pl

from dpm._types import ApplyStats
from dpm.delta_db import (
    load_apply_changes,
    load_apply_context,
    load_datapoint_changes,
)

LOG = logging.getLogger(__name__)

FACT_SCHEMA: dict[str, type[pl.DataType]] = {
    "fact_index": pl.Int64,
    "start": pl.Int64,
    "end": pl.Int64,
    "qname": pl.String,
    "attributes": pl.String,
    "value": pl.String,
}

METRIC_FACT_RE = re.compile(
    rb"<(?P<qname>s2md_met:[A-Za-z_][A-Za-z0-9_.-]*)\b(?P<attrs>[^>]*)>"
    rb"(?P<value>.*?)"
    rb"</(?P=qname)>",
    re.DOTALL,
)
SELF_CLOSING_METRIC_FACT_RE = re.compile(
    rb"<(?P<qname>s2md_met:[A-Za-z_][A-Za-z0-9_.-]*)\b(?P<attrs>[^>]*)/>",
    re.DOTALL,
)
SCHEMA_REF_RE = re.compile(
    rb"<(?P<tag>(?:[A-Za-z_][A-Za-z0-9_.-]*:)?schemaRef)\b[^>]*\b"
    rb"(?:[A-Za-z_][A-Za-z0-9_.-]*:)?href\s*=\s*(['\"])(?P<href>.*?)\2[^>]*/?>",
    re.DOTALL,
)
# An explicit dimension member inside a context, e.g.
# ``<xbrldi:explicitMember dimension="s2c_dim:VG">s2c_AM:x80</xbrldi:explicitMember>``.
# The captured ``dim``/``member`` tokens match the delta DB's stored form exactly.
# ``typedMember`` dimensions carry a typed value (not a member token) and are ignored.
EXPLICIT_MEMBER_RE = re.compile(
    rb"<(?:[A-Za-z_][A-Za-z0-9_.-]*:)?explicitMember\b[^>]*\bdimension\s*=\s*"
    rb"(['\"])(?P<dim>.*?)\1[^>]*>(?P<member>.*?)</(?:[A-Za-z_][A-Za-z0-9_.-]*:)?explicitMember>",
    re.DOTALL,
)
# A full ``<xbrli:context id="cN">…</xbrli:context>`` block.
CONTEXT_RE = re.compile(
    rb"<(?:[A-Za-z_][A-Za-z0-9_.-]*:)?context\b[^>]*\bid\s*=\s*(['\"])(?P<id>.*?)\1"
    rb"[^>]*>(?P<body>.*?)</(?:[A-Za-z_][A-Za-z0-9_.-]*:)?context\s*>",
    re.DOTALL,
)
# ``contextRef="cN"`` on a fact's opening tag.
CONTEXT_REF_RE = re.compile(r"\bcontextRef\s*=\s*(['\"])(?P<ref>.*?)\1")
# Same, over raw bytes — used to scan the whole patched document for every context
# consumer (metric facts, filing indicators, footnotes, …) when pruning orphans.
CONTEXT_REF_BYTES_RE = re.compile(rb"\bcontextRef\s*=\s*(['\"])(?P<ref>.*?)\1")
# The closing root element (``</xbrli:xbrl>``); new contexts are spliced in before it.
ROOT_CLOSE_RE = re.compile(rb"</(?:[A-Za-z_][A-Za-z0-9_.-]*:)?xbrl\s*>")
# The closing scenario element, where cloned explicit members are injected.
SCENARIO_CLOSE_RE = re.compile(rb"</(?:[A-Za-z_][A-Za-z0-9_.-]*:)?scenario\s*>")
# The ``id="…"`` attribute of a context, neutralized when computing a frame signature
# and rewritten when cloning.
_ID_ATTR_RE = re.compile(rb"\bid\s*=\s*(['\"]).*?\1")


def detect_perimeter_from_xml_bytes(xml: bytes) -> str:
    candidates: list[str] = []
    for match in SCHEMA_REF_RE.finditer(xml):
        href = match.group("href").decode("utf-8", errors="replace")
        mod_match = re.search(r"/mod/([a-z0-9_-]+)\.xsd(?:$|[?#])", href, re.I)
        if mod_match:
            candidates.append(mod_match.group(1).lower())
            continue
        filename = href.rstrip("/").split("/")[-1]
        if filename.lower().endswith(".xsd"):
            candidates.append(filename[:-4].lower())
    if not candidates:
        raise ValueError(
            "Unable to detect perimeter from schemaRef href. Use --perimeter."
        )
    unique = list(dict.fromkeys(candidates))
    if len(unique) > 1:
        LOG.warning(
            "Multiple perimeter candidates found in schemaRef hrefs: %s. Using %s",
            unique,
            unique[0],
        )
    return unique[0]


MemberSet = frozenset[tuple[str, str]]  # a set of (dimension, member) tokens

# A cell's *fixed* signature: the closed, non-default members that pin it down. A
# dimension can be an open axis in one table yet a fixed key member in another (e.g.
# ``TB`` is free-form in most tables but fixed to ``s2c_LB:x28`` in S.27.03.01.01), so a
# single global "open dimension" strip cannot canonicalise a fact — matching is per cell.
FixedSet = frozenset[tuple[str, str]]


class DpsDelta(NamedTuple):
    """The delta indexed by metric for exact, per-cell fixed-signature matching.

    Each map is ``metric qname → candidate cells``. ``survivor`` lists the fixed-member
    sets of every cell present in the *new* version (from Kept/Added/Modified rows) — a
    fact matching one still exists and is left untouched, even if its cell moved row/column.
    ``modified`` links an old cell's fixed set to its successor ``(new qname, new fixed
    set)``. ``deleted`` lists old fixed sets with no successor. A fact is matched to the
    *most specific* (largest) candidate whose fixed members it contains, with any extra
    members lying only on open axes — see :func:`_match_fixed`.
    """

    survivor: dict[str, list[FixedSet]]
    modified: dict[str, list[tuple[FixedSet, str, FixedSet]]]
    deleted: dict[str, list[FixedSet]]


def _match_fixed(
    candidates: list[FixedSet],
    members: frozenset[tuple[str, str]],
    open_dimensions: frozenset[str],
) -> FixedSet | None:
    """The most specific candidate fixed set a fact's members satisfy, or ``None``.

    A candidate matches when all its fixed members are present and every *extra* member
    the fact carries sits on an open axis (the free-form values, e.g. currencies, that
    the model signature wildcards). The largest matching candidate wins so a fact lands
    on its true cell rather than a less specific one that merely shares a prefix.
    """
    best: FixedSet | None = None
    for fixed in candidates:
        if fixed <= members and all(
            dim in open_dimensions for dim, _ in (members - fixed)
        ):
            if best is None or len(fixed) > len(best):
                best = fixed
    return best


def build_dps_delta(delta: pl.DataFrame) -> DpsDelta:
    """Index per-cell structure changes by metric for exact apply.

    Each row carries a cell's old/new metric qname and its closed non-default member
    string (``dimensions_old``/``dimensions_new``) — that pair *is* the old/new fixed
    signature. Survivors are gathered from the whole *new* side (Kept/Added/Modified) so a
    fact is kept whenever its signature still exists in the new version, including when its
    cell merely moved row/column (which the coordinate-keyed delta reports as a Deleted old
    cell plus an Added new one). Keying on the fixed signature, not the metric qname alone,
    is what makes apply exact — a metric deleted at one cell but surviving at another no
    longer wipes every fact of it. A modified link with a conflicting target is dropped.
    """
    survivor: dict[str, set[FixedSet]] = {}
    modified: dict[str, dict[FixedSet, tuple[str, FixedSet]]] = {}
    conflicts: dict[str, set[FixedSet]] = {}
    deleted: dict[str, set[FixedSet]] = {}
    if delta.is_empty():
        return DpsDelta({}, {}, {})

    for row in delta.iter_rows(named=True):
        status = row.get("status")
        qname_old = row.get("qname_old") or ""
        qname_new = row.get("qname_new") or ""
        old_fixed = _dimensions_set(row.get("dimensions_old") or "")
        new_fixed = _dimensions_set(row.get("dimensions_new") or "")
        if qname_new and status in ("Kept", "Added", "Modified"):
            survivor.setdefault(qname_new, set()).add(new_fixed)
        if status == "Deleted" and qname_old:
            deleted.setdefault(qname_old, set()).add(old_fixed)
        elif (
            status == "Modified"
            and qname_old
            and qname_new
            and (qname_old, old_fixed) != (qname_new, new_fixed)
        ):
            target = (qname_new, new_fixed)
            existing = modified.setdefault(qname_old, {}).get(old_fixed)
            if existing is not None and existing != target:
                conflicts.setdefault(qname_old, set()).add(old_fixed)
            else:
                modified[qname_old][old_fixed] = target

    # Drop conflicting modified links, then let survival win over transform/removal.
    for qname, keys in conflicts.items():
        for key in keys:
            modified.get(qname, {}).pop(key, None)
    for qname, survivors in survivor.items():
        mod = modified.get(qname)
        if mod:
            for key in survivors:
                mod.pop(key, None)
        dele = deleted.get(qname)
        if dele:
            dele -= survivors
    for qname, mod in modified.items():
        dele = deleted.get(qname)
        if dele:
            dele -= mod.keys()

    return DpsDelta(
        {q: list(s) for q, s in survivor.items()},
        {q: [(k, *v) for k, v in m.items()] for q, m in modified.items() if m},
        {q: list(s) for q, s in deleted.items() if s},
    )


def dps_delta_to_rows(perimeter: str, dps: DpsDelta) -> list[dict[str, str]]:
    """Flatten a resolved :class:`DpsDelta` into ``datapoint_changes`` rows.

    Persisted at delta-build time (per perimeter) so apply loads the resolved index
    directly — see :func:`rows_to_dps_delta` for the inverse.
    """
    rows: list[dict[str, str]] = []
    for qname, fixeds in dps.survivor.items():
        rows += [
            {"perimeter": perimeter, "qname": qname, "role": "survivor",
             "fixed": _format_members(fixed), "target_qname": "", "target_fixed": ""}
            for fixed in fixeds
        ]
    for qname, entries in dps.modified.items():
        rows += [
            {"perimeter": perimeter, "qname": qname, "role": "modified",
             "fixed": _format_members(old_fixed), "target_qname": new_qname,
             "target_fixed": _format_members(new_fixed)}
            for old_fixed, new_qname, new_fixed in entries
        ]
    for qname, fixeds in dps.deleted.items():
        rows += [
            {"perimeter": perimeter, "qname": qname, "role": "deleted",
             "fixed": _format_members(fixed), "target_qname": "", "target_fixed": ""}
            for fixed in fixeds
        ]
    return rows


def rows_to_dps_delta(rows: pl.DataFrame) -> DpsDelta:
    """Reassemble a :class:`DpsDelta` from persisted ``datapoint_changes`` rows.

    The rows are already resolved (survival-wins, conflicts dropped) at build time, so
    this is a plain regroup — no re-derivation.
    """
    survivor: dict[str, list[FixedSet]] = {}
    modified: dict[str, list[tuple[FixedSet, str, FixedSet]]] = {}
    deleted: dict[str, list[FixedSet]] = {}
    for row in rows.iter_rows(named=True):
        qname = row["qname"]
        fixed = _dimensions_set(row.get("fixed") or "")
        role = row["role"]
        if role == "survivor":
            survivor.setdefault(qname, []).append(fixed)
        elif role == "modified":
            modified.setdefault(qname, []).append(
                (fixed, row.get("target_qname") or "", _dimensions_set(row.get("target_fixed") or ""))
            )
        elif role == "deleted":
            deleted.setdefault(qname, []).append(fixed)
    return DpsDelta(survivor, modified, deleted)


def _dimensions_set(spec: str) -> frozenset[tuple[str, str]]:
    """Parse a ``s2c_dim:XX=s2c_YY:zN;…`` string into a set of ``(dim, member)`` pairs."""
    pairs: set[tuple[str, str]] = set()
    for part in spec.split(";"):
        dim, sep, member = part.strip().partition("=")
        if sep and dim.strip() and member.strip():
            pairs.add((dim.strip(), member.strip()))
    return frozenset(pairs)


def _format_members(members: frozenset[tuple[str, str]]) -> str:
    """Render a ``(dim, member)`` set as a sorted ``s2c_dim:XX=s2c_YY:zN; …`` string."""
    return "; ".join(f"{dim}={member}" for dim, member in sorted(members))


class _ContextInfo(NamedTuple):
    block: bytes  # the full ``<…:context …>…</…:context>`` element
    frame: bytes  # normalized signature with id neutralized and explicit members removed
    members: frozenset[tuple[str, str]]  # its explicit (dimension, member) set


def _context_frame(block: bytes) -> bytes:
    """A context's identity minus its id and explicit members (entity/period/typed)."""
    frame = EXPLICIT_MEMBER_RE.sub(b"", block)
    frame = _ID_ATTR_RE.sub(b'id="__ID__"', frame, count=1)
    return re.sub(rb"\s+", b" ", frame).strip()


def parse_contexts(xml: bytes) -> dict[str, _ContextInfo]:
    """Index every context by id → (raw block, frame signature, explicit member set)."""
    contexts: dict[str, _ContextInfo] = {}
    for match in CONTEXT_RE.finditer(xml):
        cid = match.group("id").decode("utf-8", errors="replace")
        block = match.group(0)
        members = frozenset(
            (
                m.group("dim").decode("utf-8", errors="replace"),
                m.group("member").strip().decode("utf-8", errors="replace"),
            )
            for m in EXPLICIT_MEMBER_RE.finditer(match.group("body"))
        )
        contexts[cid] = _ContextInfo(block, _context_frame(block), members)
    return contexts


def _clone_context(
    block: bytes, new_id: str, members: frozenset[tuple[str, str]]
) -> bytes:
    """Copy a context with a fresh id and its explicit members replaced by ``members``.

    Entity, period and any ``typedMember`` elements are preserved verbatim.
    """
    cloned = _ID_ATTR_RE.sub(b'id="' + new_id.encode() + b'"', block, count=1)
    cloned = EXPLICIT_MEMBER_RE.sub(b"", cloned)
    injected = b"".join(
        b'<xbrldi:explicitMember dimension="'
        + dim.encode()
        + b'">'
        + member.encode()
        + b"</xbrldi:explicitMember>"
        for dim, member in sorted(members)
    )
    if injected:
        cloned = SCENARIO_CLOSE_RE.sub(injected + rb"\g<0>", cloned, count=1)
    return cloned


_REPOINT_SCHEMA = {
    "qname": pl.String,
    "context_old": pl.String,
    "context_new": pl.String,
    "dimensions_old": pl.String,
    "dimensions_new": pl.String,
    "value": pl.String,
}
_NEW_CONTEXT_SCHEMA = {
    "context_id": pl.String,
    "dimensions": pl.String,
    "cloned_from": pl.String,
}
_DELETED_SCHEMA = {"qname": pl.String, "value": pl.String}
_RENAMED_SCHEMA = {"qname": pl.String, "qname_new": pl.String, "value": pl.String}


def flatten_metric_facts(xml: bytes) -> pl.DataFrame:
    rows: list[dict[str, object]] = []
    for fact_index, match in enumerate(METRIC_FACT_RE.finditer(xml)):
        rows.append(
            {
                "fact_index": fact_index,
                "start": match.start(),
                "end": match.end(),
                "qname": match.group("qname").decode("utf-8", errors="replace"),
                "attributes": match.group("attrs").decode("utf-8", errors="replace"),
                "value": match.group("value").decode("utf-8", errors="replace"),
            }
        )

    occupied = [(int(r["start"]), int(r["end"])) for r in rows]
    for fact_index, match in enumerate(
        SELF_CLOSING_METRIC_FACT_RE.finditer(xml), start=len(rows)
    ):
        start, end = match.start(), match.end()
        if any(start >= lo and end <= hi for lo, hi in occupied):
            continue
        rows.append(
            {
                "fact_index": fact_index,
                "start": start,
                "end": end,
                "qname": match.group("qname").decode("utf-8", errors="replace"),
                "attributes": match.group("attrs").decode("utf-8", errors="replace"),
                "value": "",
            }
        )

    return (
        pl.DataFrame(rows, schema=FACT_SCHEMA).sort("start")
        if rows
        else pl.DataFrame(schema=FACT_SCHEMA)
    )


def _rename_fact_bytes(segment: bytes, old_qname: str, new_qname: str) -> bytes:
    pattern = re.compile(rb"(<\/?)" + re.escape(old_qname.encode()) + rb"(\b)")
    return pattern.sub(
        lambda m: m.group(1) + new_qname.encode() + m.group(2), segment, count=2
    )


def _repoint_ref_bytes(segment: bytes, old_ref: str, new_ref: str) -> bytes:
    return re.sub(
        rb"(\bcontextRef\s*=\s*['\"])" + re.escape(old_ref.encode()) + rb"(['\"])",
        rb"\g<1>" + new_ref.encode() + rb"\g<2>",
        segment,
        count=1,
    )


def _expand_deleted_span(xml: bytes, start: int, end: int) -> tuple[int, int]:
    line_start = xml.rfind(b"\n", 0, start) + 1
    if line_start < 0:
        line_start = 0
    if xml[line_start:start].strip() == b"":
        start = line_start
    if end < len(xml) and xml[end : end + 2] == b"\r\n":
        end += 2
    elif end < len(xml) and xml[end : end + 1] in {b"\n", b"\r"}:
        end += 1
    return start, end


def _prune_orphan_contexts(patched: bytes) -> tuple[bytes, list[str]]:
    """Drop every ``<…:context>`` block no ``contextRef`` in the document points at.

    Run on the fully patched output so it accounts for *all* consumers — metric facts,
    filing indicators, footnotes, anything carrying a ``contextRef`` — not just the metric
    facts the apply loop rewrites. Newly cloned contexts survive because the facts we just
    re-pointed reference them. Whitespace around a removed block is swallowed via
    :func:`_expand_deleted_span`, matching deleted-fact removal, so no blank line is left.
    """
    referenced = {
        m.group("ref").decode("utf-8", errors="replace")
        for m in CONTEXT_REF_BYTES_RE.finditer(patched)
    }
    removed_ids: list[str] = []
    parts: list[bytes] = []
    cursor = 0
    for match in CONTEXT_RE.finditer(patched):
        cid = match.group("id").decode("utf-8", errors="replace")
        if cid in referenced:
            continue
        start, end = _expand_deleted_span(patched, match.start(), match.end())
        if start < cursor:
            continue
        parts.append(patched[cursor:start])
        cursor = end
        removed_ids.append(cid)
    if not removed_ids:
        return patched, []
    parts.append(patched[cursor:])
    return b"".join(parts), removed_ids


class _FactDecision(NamedTuple):
    """The resolved action for one instance fact against the DPS delta."""

    delete: bool
    new_qname: str  # unchanged qname when not renamed
    new_members: frozenset[tuple[str, str]] | None  # target member set when re-pointed


def _decide_fact(
    qname: str,
    members: frozenset[tuple[str, str]],
    dps: DpsDelta,
    open_dimensions: frozenset[str],
    default_members: frozenset[str],
) -> _FactDecision | None:
    """Resolve a fact to keep (``None``) / delete / rename / re-point.

    The fact's non-default members are matched, per cell, against the metric's candidate
    fixed signatures (most-specific wins). Survival is checked first — a fact whose
    signature still exists in the new version is left untouched — then a modified link
    (rename/re-point), then deletion. An unmatched fact is kept: this exactness is what
    stops one deleted cell from wiping every fact of a metric that survives elsewhere.
    """
    nd_members = frozenset(
        (dim, member) for dim, member in members if member not in default_members
    )
    if _match_fixed(dps.survivor.get(qname, []), nd_members, open_dimensions) is not None:
        return None

    mod_candidates = dps.modified.get(qname, [])
    best: tuple[FixedSet, str, FixedSet] | None = None
    for old_fixed, new_qname, new_fixed in mod_candidates:
        if old_fixed <= nd_members and all(
            dim in open_dimensions for dim, _ in (nd_members - old_fixed)
        ):
            if best is None or len(old_fixed) > len(best[0]):
                best = (old_fixed, new_qname, new_fixed)
    if best is not None:
        old_fixed, new_qname, new_fixed = best
        target_members = (members - old_fixed) | new_fixed
        renamed = new_qname != qname
        repointed = target_members != members
        if not renamed and not repointed:
            return None
        return _FactDecision(
            delete=False,
            new_qname=new_qname,
            new_members=target_members if repointed else None,
        )

    if _match_fixed(dps.deleted.get(qname, []), nd_members, open_dimensions) is not None:
        return _FactDecision(delete=True, new_qname=qname, new_members=None)
    return None


def apply_delta(
    delta_path: Path,
    input_xbrl: Path,
    output_xbrl: Path,
    perimeter_override: str | None,
    dry_run: bool,
    debug_xlsx: Path | None,
) -> ApplyStats:
    """Migrate an XBRL instance to the new taxonomy version via exact DPS matching.

    Every metric fact is resolved to its canonical Data Point Signature and matched 1:1
    against the delta — deleting only cells that truly disappeared, renaming metrics and
    re-pointing dimensional contexts where the cell moved, and leaving everything else
    untouched. Context re-points clone/reuse a context carrying the new member set rather
    than editing the shared one in place.
    """
    if output_xbrl.resolve() == input_xbrl.resolve():
        raise ValueError("Output path must be different from input XBRL path")

    xml = input_xbrl.read_bytes()
    perimeter = (
        perimeter_override.lower()
        if perimeter_override
        else detect_perimeter_from_xml_bytes(xml)
    )
    LOG.info("Detected perimeter: %s", perimeter)

    open_dimensions, default_members = load_apply_context(delta_path)
    # Prefer the persisted DPS pivot; fall back to deriving it for pre-pivot delta DBs.
    datapoint_changes = load_datapoint_changes(delta_path, perimeter)
    dps = (
        rows_to_dps_delta(datapoint_changes)
        if datapoint_changes is not None
        else build_dps_delta(load_apply_changes(delta_path, perimeter))
    )
    LOG.info(
        "DPS delta metrics: survivor=%d modified=%d deleted=%d (open dims=%d, default members=%d)",
        len(dps.survivor),
        len(dps.modified),
        len(dps.deleted),
        len(open_dimensions),
        len(default_members),
    )

    contexts = parse_contexts(xml)
    # signature (frame, member set) -> context id, seeded with existing contexts so a
    # re-point target that already exists is reused rather than duplicated.
    sig_to_id: dict[tuple[bytes, frozenset[tuple[str, str]]], str] = {}
    for cid, info in contexts.items():
        sig_to_id.setdefault((info.frame, info.members), cid)
    used_ids = set(contexts)
    counter = 0

    def _fresh_id() -> str:
        nonlocal counter
        while True:
            counter += 1
            candidate = f"cdpm{counter}"
            if candidate not in used_ids:
                used_ids.add(candidate)
                return candidate

    facts = flatten_metric_facts(xml)
    LOG.info("Flattened metric facts: %d", facts.height)

    new_blocks: list[bytes] = []
    parts: list[bytes] = []
    cursor = 0
    deleted_rows: list[dict[str, str]] = []
    renamed_rows: list[dict[str, str]] = []
    repoint_rows: list[dict[str, str]] = []
    new_context_rows: list[dict[str, str]] = []

    for fact in facts.iter_rows(named=True):
        qname = str(fact["qname"])
        attrs = str(fact["attributes"])
        ref_match = CONTEXT_REF_RE.search(attrs)
        ctx_ref = ref_match.group("ref") if ref_match else None
        info = contexts.get(ctx_ref) if ctx_ref else None
        members = info.members if info else frozenset()

        decision = _decide_fact(qname, members, dps, open_dimensions, default_members)
        if decision is None:
            continue  # keep untouched — leave its bytes in place

        start, end = int(fact["start"]), int(fact["end"])
        if decision.delete:
            d_start, d_end = _expand_deleted_span(xml, start, end)
            if d_end <= cursor:
                continue
            parts.append(xml[cursor : max(d_start, cursor)])
            cursor = d_end
            deleted_rows.append({"qname": qname, "value": str(fact["value"])})
            continue

        segment = xml[start:end]
        if decision.new_qname != qname:
            segment = _rename_fact_bytes(segment, qname, decision.new_qname)
            renamed_rows.append(
                {
                    "qname": qname,
                    "qname_new": decision.new_qname,
                    "value": str(fact["value"]),
                }
            )
        if decision.new_members is not None and info is not None and ctx_ref:
            sig = (info.frame, decision.new_members)
            new_id = sig_to_id.get(sig)
            if new_id is None:
                new_id = _fresh_id()
                new_blocks.append(
                    b"  " + _clone_context(info.block, new_id, decision.new_members) + b"\n"
                )
                sig_to_id[sig] = new_id
                new_context_rows.append(
                    {
                        "context_id": new_id,
                        "dimensions": _format_members(decision.new_members),
                        "cloned_from": ctx_ref,
                    }
                )
            segment = _repoint_ref_bytes(segment, ctx_ref, new_id)
            repoint_rows.append(
                {
                    "qname": decision.new_qname,
                    "context_old": ctx_ref,
                    "context_new": new_id,
                    "dimensions_old": _format_members(members),
                    "dimensions_new": _format_members(decision.new_members),
                    "value": str(fact["value"]),
                }
            )

        if start < cursor:
            raise ValueError("Overlapping metric fact matches detected")
        parts.append(xml[cursor:start])
        parts.append(segment)
        cursor = end

    parts.append(xml[cursor:])
    patched = b"".join(parts)

    if new_blocks:
        roots = list(ROOT_CLOSE_RE.finditer(patched))
        insert_at = roots[-1].start() if roots else len(patched)
        patched = patched[:insert_at] + b"".join(new_blocks) + patched[insert_at:]

    patched, removed_context_ids = _prune_orphan_contexts(patched)

    deleted_facts = len(deleted_rows)
    stats = ApplyStats(
        perimeter=perimeter,
        facts_before=facts.height,
        facts_after=facts.height - deleted_facts,
        deleted_facts=deleted_facts,
        renamed_facts=len(renamed_rows),
        deleted_qnames=len(dps.deleted),
        modified_qnames=len(dps.modified),
        repointed_facts=len(repoint_rows),
        new_contexts=len(new_blocks),
        removed_contexts=len(removed_context_ids),
    )
    LOG.info(
        "Applied: deleted=%d renamed=%d repointed=%d new_contexts=%d removed_contexts=%d",
        deleted_facts,
        len(renamed_rows),
        len(repoint_rows),
        len(new_blocks),
        len(removed_context_ids),
    )

    if debug_xlsx:
        from dpm.excel import generate_apply_debug_workbook

        debug_xlsx.parent.mkdir(parents=True, exist_ok=True)
        generate_apply_debug_workbook(
            debug_xlsx,
            stats=stats,
            deleted=pl.DataFrame(deleted_rows, schema=_DELETED_SCHEMA),
            renamed=pl.DataFrame(renamed_rows, schema=_RENAMED_SCHEMA),
            repointed=pl.DataFrame(repoint_rows, schema=_REPOINT_SCHEMA),
            new_contexts=pl.DataFrame(new_context_rows, schema=_NEW_CONTEXT_SCHEMA),
        )
        LOG.info("Debug workbook written to: %s", debug_xlsx)

    if not dry_run:
        output_xbrl.parent.mkdir(parents=True, exist_ok=True)
        output_xbrl.write_bytes(patched)
        LOG.info("Updated XBRL written to: %s", output_xbrl)
    else:
        LOG.info("Dry run enabled; output file not written")

    return stats
