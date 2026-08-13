from __future__ import annotations

import logging
import re
from pathlib import Path

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
    facts_parquet: Path | None,
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
    LOG.info("Delta qnames: deleted=%d modified=%d", deleted.height, modified.height)

    facts = flatten_metric_facts(xml)
    LOG.info("Flattened metric facts: %d", facts.height)

    flagged = flag_facts_with_delta(facts, deleted, modified)
    deleted_facts = flagged.filter(pl.col("delete_fact")).height
    renamed_facts = flagged.filter(
        ~pl.col("delete_fact") & pl.col("rename_fact")
    ).height

    if facts_parquet:
        facts_parquet.parent.mkdir(parents=True, exist_ok=True)
        flagged.write_parquet(facts_parquet)
        LOG.info("Fact dataframe written to: %s", facts_parquet)

    if not dry_run:
        output_xbrl.parent.mkdir(parents=True, exist_ok=True)
        output_xbrl.write_bytes(rebuild_xml_bytes(xml, flagged))
        LOG.info("Updated XBRL written to: %s", output_xbrl)
    else:
        LOG.info("Dry run enabled; output file not written")

    return ApplyStats(
        perimeter=perimeter,
        facts_before=facts.height,
        facts_after=facts.height - deleted_facts,
        deleted_facts=deleted_facts,
        renamed_facts=renamed_facts,
        deleted_qnames=deleted.height,
        modified_qnames=modified.height,
    )
