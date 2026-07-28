#!/usr/bin/env python3
from __future__ import annotations

import argparse
import logging
import warnings
from pathlib import Path

warnings.filterwarnings(
    "ignore",
    message="Data Validation extension is not supported and will be removed",
    category=UserWarning,
    module="openpyxl.worksheet._reader",
)

from dpm._types import WorkbookCache
from dpm.delta import compare_versions
from dpm.excel import generate_delta_workbook
from dpm.metrics import build_metric_dataset
from dpm.toc import (
    extract_perimeters,
    extract_toc,
    filter_entries_by_perimeter,
    select_perimeters_interactively,
)

LOG = logging.getLogger("dpm_delta")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Compare two EIOPA DPM Excel workbooks and create a delta workbook"
    )
    parser.add_argument("old_version", type=Path)
    parser.add_argument("new_version", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument(
        "--no-interactive-perimeters",
        action="store_true",
        help="Do not show perimeter selection; include all perimeters",
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
        if not args.old_version.exists():
            raise FileNotFoundError(args.old_version)
        if not args.new_version.exists():
            raise FileNotFoundError(args.new_version)

        old_book = WorkbookCache.open(args.old_version)
        new_book = WorkbookCache.open(args.new_version)
        try:
            old_entries_all = extract_toc(old_book)
            new_entries_all = extract_toc(new_book)

            if args.no_interactive_perimeters:
                selected_perimeters = {
                    p.lower()
                    for p in sorted(
                        extract_perimeters(old_entries_all) | extract_perimeters(new_entries_all)
                    )
                }
                LOG.info("Selected perimeters: all")
            else:
                selected_perimeters = select_perimeters_interactively(
                    old_entries_all, new_entries_all
                )
                LOG.info("Selected perimeters: %s", sorted(selected_perimeters))

            old_entries = filter_entries_by_perimeter(old_entries_all, selected_perimeters)
            new_entries = filter_entries_by_perimeter(new_entries_all, selected_perimeters)

            old_dataset = build_metric_dataset(old_book, old_entries, label="Extracting OLD metrics")
            new_dataset = build_metric_dataset(new_book, new_entries, label="Extracting NEW metrics")

            delta = compare_versions(old_dataset, new_dataset)
            generate_delta_workbook(delta, args.output)
            LOG.info("Delta workbook generated: %s", args.output)
            return 0
        finally:
            old_book.close()
            new_book.close()
    except KeyboardInterrupt:
        LOG.error("Cancelled by user")
        return 130
    except Exception:
        LOG.exception("DPM delta generation failed")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
