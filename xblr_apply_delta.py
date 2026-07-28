#!/usr/bin/env python3
from __future__ import annotations

import argparse
import logging
from pathlib import Path

from dpm.xbrl import apply_delta

LOG = logging.getLogger("xblr_apply_delta")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Fast byte/regex XBRL updater: delete and rename s2md_met facts using Delta_DPM.xlsx."
    )
    parser.add_argument("delta_workbook", type=Path, help="Delta workbook produced by dpm_delta.py")
    parser.add_argument("input_xbrl", type=Path, help="Input XBRL instance")
    parser.add_argument(
        "output_xbrl", type=Path, help="Output XBRL instance. Must differ from input."
    )
    parser.add_argument(
        "--perimeter", help="Override perimeter detected from schemaRef, e.g. qrs"
    )
    parser.add_argument("--dry-run", action="store_true", help="Do not write output")
    parser.add_argument(
        "--facts-parquet",
        type=Path,
        help="Optional parquet dump of flattened metric facts after delta flags",
    )
    parser.add_argument(
        "--log-level", default="INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR"]
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(levelname)s %(name)s - %(message)s",
    )
    try:
        if not args.delta_workbook.exists():
            raise FileNotFoundError(args.delta_workbook)
        if not args.input_xbrl.exists():
            raise FileNotFoundError(args.input_xbrl)

        stats = apply_delta(
            delta_workbook=args.delta_workbook,
            input_xbrl=args.input_xbrl,
            output_xbrl=args.output_xbrl,
            perimeter_override=args.perimeter,
            dry_run=args.dry_run,
            facts_parquet=args.facts_parquet,
        )
        LOG.info(
            "Result: perimeter=%s facts_before=%d facts_after=%d "
            "deleted_facts=%d renamed_facts=%d deleted_qnames=%d modified_qnames=%d",
            stats.perimeter,
            stats.facts_before,
            stats.facts_after,
            stats.deleted_facts,
            stats.renamed_facts,
            stats.deleted_qnames,
            stats.modified_qnames,
        )
        return 0
    except Exception:
        LOG.exception("XBRL delta application failed")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
