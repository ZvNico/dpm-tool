# dpm-tool documentation

`dpm-tool` is a terminal UI toolkit for working with the EIOPA Solvency II
**Data Point Model (DPM)**. It has three jobs:

1. **Ingest** an official EIOPA DPM release (a SQLite database) into a compact,
   versioned local DuckDB.
2. **Diff** two ingested versions into a reviewable, queryable delta — rendered
   as an Excel workbook and browsable in the TUI.
3. **Apply** that delta to an existing XBRL instance document, migrating it to
   the new taxonomy version (deleting, renaming and re-pointing facts) without
   touching the original file.

Everything runs inside a [Textual](https://textual.textualize.io/) TUI; there
are no subcommands to memorise. The [README](../README.md) is the quick-start;
these documents are the exhaustive reference.

## Contents

| Document | What it covers |
| --- | --- |
| [Reporting data model](reporting_data_model.md) | The business concepts — fact, metric, dimension, member, domain, context, cell — and how they map to the DuckDB tables. **Read this first** if the domain is new to you. |
| [Architecture](architecture.md) | The module map, the end-to-end data flow, and how the pieces fit together. |
| [Ingestion](ingestion.md) | How an EIOPA DPM SQLite database is downloaded, resolved and projected into the pivot schema. |
| [Delta](delta.md) | How two versions are compared, what the delta contains, and the cached delta-database artifact. |
| [Applying a delta to XBRL](apply_xbrl.md) | The exact, per-cell Data Point Signature matching that rewrites an instance document. |
| [Configuration & storage](configuration.md) | On-disk layout, tracked versions, output paths, and environment variables. |

## The pipeline at a glance

```
 EIOPA DPM SQLite (.db)                        XBRL instance (.xbrl)
         │                                              │
         │  ingest (dpm.dpm_source → dpm.db)            │
         ▼                                              │
 db/versions/<version>.duckdb  ── pivot schema, per version
         │                                              │
         │  pick two versions                           │
         ▼                                              │
 compare (dpm.delta) ─► DeltaResult                     │
         │                                              │
         │  cache (dpm.delta_db)                        │
         ▼                                              │
 db/delta/<old>_to_<new>.duckdb                         │
    ├── structure/metric/member_changes ─► Excel workbook (dpm.excel)
    │                                    └► Explore-Delta screen
    └── datapoint_changes (DPS pivot) ──────► apply (dpm.xbrl) ──► new .xbrl
                                                                    ▲
                                                                    └── original never modified
```
