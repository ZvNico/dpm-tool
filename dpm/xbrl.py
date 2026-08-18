from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import NamedTuple

import polars as pl

from dpm._types import ApplyStats
from dpm.delta_db import load_apply_changes

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


def build_delta_maps(delta: pl.DataFrame) -> tuple[pl.DataFrame, pl.DataFrame]:
    if delta.is_empty():
        return (
            pl.DataFrame(schema={"qname": pl.String}),
            pl.DataFrame(schema={"qname": pl.String, "qname_new": pl.String}),
        )

    deleted = (
        delta.filter(
            (pl.col("status") == "Deleted")
            & ((pl.col("qname_old") != "") | (pl.col("qname_new") != ""))
        )
        .with_columns(
            pl.when(pl.col("qname_old") != "")
            .then(pl.col("qname_old"))
            .otherwise(pl.col("qname_new"))
            .alias("qname")
        )
        .select("qname")
        .unique()
    )
    modified = (
        delta.filter(
            (pl.col("status") == "Modified")
            & (pl.col("qname_old") != "")
            & (pl.col("qname_new") != "")
            & (pl.col("qname_old") != pl.col("qname_new"))
        )
        .select(
            [pl.col("qname_old").alias("qname"), pl.col("qname_new").alias("qname_new")]
        )
        .unique(subset=["qname"], keep="first")
    )
    return deleted, modified


MemberSet = frozenset  # frozenset[tuple[str, str]] of (dimension, member) tokens


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


_FACT_DETAIL_SCHEMA = {
    "qname": pl.String,
    "qname_new": pl.String,
    "context_ref": pl.String,
    "dimensions": pl.String,
    "value": pl.String,
}


def _fact_detail_frame(
    subset: pl.DataFrame, ctx_members: dict[str, str]
) -> pl.DataFrame:
    """Per-fact debug rows: qname, its rename target, context, dimensions, value.

    ``subset`` is a slice of the flagged facts frame; ``ctx_members`` maps a context id
    to its rendered dimension string. Kept tiny — only changed facts flow through here.
    """
    rows: list[dict[str, str]] = []
    for fact in subset.iter_rows(named=True):
        ref_match = CONTEXT_REF_RE.search(str(fact["attributes"]))
        ref = ref_match.group("ref") if ref_match else ""
        rows.append(
            {
                "qname": str(fact["qname"]),
                "qname_new": str(fact.get("qname_out") or fact["qname"]),
                "context_ref": ref,
                "dimensions": ctx_members.get(ref, ""),
                "value": str(fact["value"]),
            }
        )
    return pl.DataFrame(rows, schema=_FACT_DETAIL_SCHEMA)


ContextRemap = dict[
    str, list[tuple[frozenset[tuple[str, str]], frozenset[tuple[str, str]]]]
]


def build_context_remap(delta: pl.DataFrame) -> ContextRemap:
    """Map each metric qname to its ``(old dims, new dims)`` cell changes.

    Each structure ``Modified`` row is a cell whose dimensional signature changed. The
    delta records only the dimensions the template *varies* — an instance context also
    carries implicit/default members (e.g. ``s2c_dim:VG=s2c_AM:x80``) that the delta omits
    — so matching is by **subset**: a change applies to a fact whose context is a superset
    of ``old_set`` (see :func:`apply_context_remap`). Returned as ``{qname: [(old_set,
    new_set), …]}`` sorted by descending ``old_set`` size, so the most specific matching
    cell wins. A ``(qname, old_set)`` yielding two distinct targets is dropped with a
    warning (that key is a proven function over the real delta).
    """
    if delta.is_empty() or "dimensions_old" not in delta.columns:
        return {}

    per_key: dict[tuple[str, frozenset[tuple[str, str]]], frozenset[tuple[str, str]]] = {}
    conflicts: set[tuple[str, frozenset[tuple[str, str]]]] = set()
    for row in delta.iter_rows(named=True):
        if row.get("status") != "Modified":
            continue
        old_spec = row.get("dimensions_old") or ""
        new_spec = row.get("dimensions_new") or ""
        qname = row.get("qname_old") or ""
        if not qname or not old_spec or not new_spec or old_spec == new_spec:
            continue
        old_set = _dimensions_set(old_spec)
        new_set = _dimensions_set(new_spec)
        if old_set == new_set:
            continue
        key = (qname, old_set)
        if key in per_key and per_key[key] != new_set:
            conflicts.add(key)
            LOG.warning("Conflicting context remap for qname=%s; dropping key", qname)
            continue
        per_key[key] = new_set

    for key in conflicts:
        per_key.pop(key, None)

    remap: ContextRemap = {}
    for (qname, old_set), new_set in per_key.items():
        remap.setdefault(qname, []).append((old_set, new_set))
    for changes in remap.values():
        changes.sort(key=lambda pair: len(pair[0]), reverse=True)
    return remap


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


class ContextRemapResult(NamedTuple):
    xml: bytes
    repointed_facts: int
    new_contexts: int
    # One row per re-pointed fact: qname, context_old, context_new, dimensions_old/new, value.
    repoint_details: pl.DataFrame
    # One row per created context: context_id, dimensions, cloned_from.
    new_context_details: pl.DataFrame


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


def _match_context_change(
    changes: list[tuple[frozenset[tuple[str, str]], frozenset[tuple[str, str]]]],
    members: frozenset[tuple[str, str]],
) -> frozenset[tuple[str, str]] | None:
    """Resolve a fact's new dimensional context, or ``None`` if nothing applies.

    ``changes`` are ``(old_set, new_set)`` cell changes for the fact's qname, ordered by
    descending ``old_set`` size. The most specific cell whose ``old_set`` is a subset of
    the fact's ``members`` and actually changes it wins; only the dimensions it names are
    rewritten, the rest of the context is kept: ``new = (members - old_set) | new_set``.
    No-op candidates (whose target equals the current context — the fact already reflects
    them) are skipped so a real change on a less specific cell is not masked.
    """
    for old_set, new_set in changes:
        if old_set <= members:
            target = (members - old_set) | new_set
            if target != members:
                return target
    return None


def apply_context_remap(xml: bytes, remap: ContextRemap) -> ContextRemapResult:
    """Re-point facts whose ``(qname, dimensional context)`` changed between versions.

    Contexts are shared and the mapping is qname-dependent, so a fact is moved onto a
    context carrying the new member set — reusing an existing matching context or cloning
    one — rather than editing the shared context in place (which would corrupt the other
    metrics on it). Returns the patched bytes, the re-point / new-context counts, and
    per-fact / per-new-context detail frames.
    """
    if not remap:
        return ContextRemapResult(
            xml, 0, 0,
            pl.DataFrame(schema=_REPOINT_SCHEMA),
            pl.DataFrame(schema=_NEW_CONTEXT_SCHEMA),
        )

    contexts = parse_contexts(xml)
    # signature (frame, member set) -> context id, seeded with the existing contexts so
    # unchanged and target contexts are reused and never duplicated.
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

    new_blocks: list[bytes] = []
    parts: list[bytes] = []
    cursor = 0
    repoint_rows: list[dict[str, str]] = []
    new_context_rows: list[dict[str, str]] = []

    facts = flatten_metric_facts(xml).sort("start")
    for fact in facts.iter_rows(named=True):
        attrs = str(fact["attributes"])
        ref_match = CONTEXT_REF_RE.search(attrs)
        if ref_match is None:
            continue
        ctx_ref = ref_match.group("ref")
        info = contexts.get(ctx_ref)
        if info is None:
            continue
        changes = remap.get(str(fact["qname"]))
        if not changes:
            continue
        target = _match_context_change(changes, info.members)
        if target is None:
            continue

        sig = (info.frame, target)
        new_id = sig_to_id.get(sig)
        if new_id is None:
            new_id = _fresh_id()
            new_blocks.append(b"  " + _clone_context(info.block, new_id, target) + b"\n")
            sig_to_id[sig] = new_id
            new_context_rows.append(
                {
                    "context_id": new_id,
                    "dimensions": _format_members(target),
                    "cloned_from": ctx_ref,
                }
            )

        start, end = int(fact["start"]), int(fact["end"])
        segment = re.sub(
            rb"(\bcontextRef\s*=\s*['\"])" + re.escape(ctx_ref.encode()) + rb"(['\"])",
            rb"\g<1>" + new_id.encode() + rb"\g<2>",
            xml[start:end],
            count=1,
        )
        parts.append(xml[cursor:start])
        parts.append(segment)
        cursor = end
        repoint_rows.append(
            {
                "qname": str(fact["qname"]),
                "context_old": ctx_ref,
                "context_new": new_id,
                "dimensions_old": _format_members(info.members),
                "dimensions_new": _format_members(target),
                "value": str(fact["value"]),
            }
        )

    parts.append(xml[cursor:])
    out = b"".join(parts)

    if new_blocks:
        roots = list(ROOT_CLOSE_RE.finditer(out))
        insert_at = roots[-1].start() if roots else len(out)
        out = out[:insert_at] + b"".join(new_blocks) + out[insert_at:]

    return ContextRemapResult(
        out,
        len(repoint_rows),
        len(new_blocks),
        pl.DataFrame(repoint_rows, schema=_REPOINT_SCHEMA),
        pl.DataFrame(new_context_rows, schema=_NEW_CONTEXT_SCHEMA),
    )


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


def flag_facts_with_delta(
    facts: pl.DataFrame, deleted: pl.DataFrame, modified: pl.DataFrame
) -> pl.DataFrame:
    if facts.is_empty():
        return facts.with_columns(
            [
                pl.lit(False).alias("delete_fact"),
                pl.lit(False).alias("rename_fact"),
                pl.col("qname").alias("qname_out"),
            ]
        )

    result = facts
    if deleted.is_empty():
        result = result.with_columns(pl.lit(False).alias("delete_fact"))
    else:
        result = result.join(
            deleted.with_columns(pl.lit(True).alias("delete_fact")),
            on="qname",
            how="left",
        ).with_columns(pl.col("delete_fact").fill_null(False))

    if modified.is_empty():
        result = result.with_columns(pl.lit(None).cast(pl.String).alias("qname_new"))
    else:
        result = result.join(modified, on="qname", how="left")

    return result.with_columns(
        [
            pl.col("qname_new").is_not_null().alias("rename_fact"),
            pl.when(pl.col("qname_new").is_not_null())
            .then(pl.col("qname_new"))
            .otherwise(pl.col("qname"))
            .alias("qname_out"),
        ]
    )


def _rename_fact_bytes(segment: bytes, old_qname: str, new_qname: str) -> bytes:
    pattern = re.compile(rb"(<\/?)" + re.escape(old_qname.encode()) + rb"(\b)")
    return pattern.sub(
        lambda m: m.group(1) + new_qname.encode() + m.group(2), segment, count=2
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


def rebuild_xml_bytes(xml: bytes, facts: pl.DataFrame) -> bytes:
    changed = (
        facts.filter(pl.col("delete_fact") | pl.col("rename_fact"))
        .select(["start", "end", "qname", "qname_out", "delete_fact", "rename_fact"])
        .sort("start")
        .to_dicts()
    )
    if not changed:
        return xml

    parts: list[bytes] = []
    cursor = 0
    for row in changed:
        start, end = int(row["start"]), int(row["end"])
        if row["delete_fact"]:
            start, end = _expand_deleted_span(xml, start, end)
            if end <= cursor:
                continue
            parts.append(xml[cursor : max(start, cursor)])
            cursor = end
        else:
            if start < cursor:
                raise ValueError("Overlapping metric fact matches detected")
            parts.append(xml[cursor:start])
            parts.append(
                _rename_fact_bytes(
                    xml[start:end], str(row["qname"]), str(row["qname_out"])
                )
                if row["rename_fact"]
                else xml[start:end]
            )
            cursor = end
    parts.append(xml[cursor:])
    return b"".join(parts)


def apply_delta(
    delta_path: Path,
    input_xbrl: Path,
    output_xbrl: Path,
    perimeter_override: str | None,
    dry_run: bool,
    debug_xlsx: Path | None,
) -> ApplyStats:
    if output_xbrl.resolve() == input_xbrl.resolve():
        raise ValueError("Output path must be different from input XBRL path")

    xml = input_xbrl.read_bytes()
    perimeter = (
        perimeter_override.lower()
        if perimeter_override
        else detect_perimeter_from_xml_bytes(xml)
    )
    LOG.info("Detected perimeter: %s", perimeter)

    delta = load_apply_changes(delta_path, perimeter)
    deleted, modified = build_delta_maps(delta)
    context_remap = build_context_remap(delta)
    LOG.info(
        "Delta qnames: deleted=%d modified=%d; context remaps=%d",
        deleted.height,
        modified.height,
        len(context_remap),
    )

    # Dimensional context re-pointing runs on the ORIGINAL xml — it matches facts by
    # their original qname and context — before the qname delete/rename pass.
    remap_result = apply_context_remap(xml, context_remap)
    remapped = remap_result.xml
    LOG.info(
        "Re-pointed facts: %d; new contexts created: %d",
        remap_result.repointed_facts,
        remap_result.new_contexts,
    )

    facts = flatten_metric_facts(remapped)
    LOG.info("Flattened metric facts: %d", facts.height)

    flagged = flag_facts_with_delta(facts, deleted, modified)
    deleted_facts = flagged.filter(pl.col("delete_fact")).height
    renamed_facts = flagged.filter(
        ~pl.col("delete_fact") & pl.col("rename_fact")
    ).height

    patched = rebuild_xml_bytes(remapped, flagged)

    stats = ApplyStats(
        perimeter=perimeter,
        facts_before=facts.height,
        facts_after=facts.height - deleted_facts,
        deleted_facts=deleted_facts,
        renamed_facts=renamed_facts,
        deleted_qnames=deleted.height,
        modified_qnames=modified.height,
        repointed_facts=remap_result.repointed_facts,
        new_contexts=remap_result.new_contexts,
    )

    if debug_xlsx:
        from dpm.excel import generate_apply_debug_workbook

        debug_xlsx.parent.mkdir(parents=True, exist_ok=True)
        ctx_members = {
            cid: _format_members(info.members)
            for cid, info in parse_contexts(remapped).items()
        }
        generate_apply_debug_workbook(
            debug_xlsx,
            stats=stats,
            deleted=_fact_detail_frame(flagged.filter(pl.col("delete_fact")), ctx_members),
            renamed=_fact_detail_frame(
                flagged.filter(~pl.col("delete_fact") & pl.col("rename_fact")),
                ctx_members,
            ),
            repointed=remap_result.repoint_details,
            new_contexts=remap_result.new_context_details,
        )
        LOG.info("Debug workbook written to: %s", debug_xlsx)

    if not dry_run:
        output_xbrl.parent.mkdir(parents=True, exist_ok=True)
        output_xbrl.write_bytes(patched)
        LOG.info("Updated XBRL written to: %s", output_xbrl)
    else:
        LOG.info("Dry run enabled; output file not written")

    return stats
