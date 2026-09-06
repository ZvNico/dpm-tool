# Architecture

This document maps the code to the pipeline: the modules, what each is
responsible for, and how data flows from an EIOPA release to a migrated XBRL
instance. For the business meaning of the terms used here (fact, metric,
dimension, member, cell), see the [reporting data model](reporting_data_model.md).

## Layers

The codebase splits cleanly into a **domain/engine** layer (`dpm/*.py`, pure
functions over Polars/DuckDB, no UI) and a **presentation** layer
(`dpm/ui/*.py`, Textual screens). `app.py` is the entry point that wires them
together.

```
app.py                     ── Textual App, theme persistence, global "Back"
└── dpm/ui/                 ── one screen per workflow (see below)
     └── dpm/workflows.py   ── the façade the UI calls; orchestrates the engine
          ├── dpm/eiopa.py        ── download & unpack the DPM release
          ├── dpm/dpm_source.py   ── project the DPM SQLite → pivot rows
          ├── dpm/db.py           ── ingested-version DuckDB (schema + loaders)
          ├── dpm/delta.py        ── compare two versions → DeltaResult
          ├── dpm/delta_db.py     ── persist/query the cached delta DuckDB
          ├── dpm/xbrl.py         ── apply a delta to an XBRL instance
          └── dpm/excel.py        ── render a delta / debug workbook
```

[dpm/workflows.py](../dpm/workflows.py) is the single seam between UI and
engine. Every screen calls a `run_*` / `load_*` function there and never reaches
into the lower modules directly. Those functions own connection lifecycle
(open DuckDB read-only, `try/finally` close) and emit `on_progress` / `on_step`
callbacks so screens can drive progress bars without knowing the internals.

## Module responsibilities

### Engine

| Module | Responsibility |
| --- | --- |
| [`_constants.py`](../dpm/_constants.py) | On-disk paths (data/config roots, versions/delta/downloads dirs), `resolve_output_path`, and the column-name constants shared across the delta code. |
| [`_types.py`](../dpm/_types.py) | Typed row dicts (`TemplateRow`, `FactRow`, …), the `ParsedModel` bundle produced by ingestion, and the dataclasses `DpmDataset`, `DeltaResult`, `ApplyStats`, `StructureEntry`. |
| [`config.py`](../dpm/config.py) | The JSON app config: the list of tracked `VersionEntry` (version + optional explicit URL) and the selected UI theme. |
| [`eiopa.py`](../dpm/eiopa.py) | Resolve a version to a download URL (HEAD-probing a bounded candidate set), stream the zip, extract the SQLite database, cache it. |
| [`dpm_source.py`](../dpm/dpm_source.py) | The **sole ingestion source**: project the ~30-table official DPM SQLite into the nine pivot rows. Parses the Data Point Signature (DPS) per cell. |
| [`db.py`](../dpm/db.py) | The ingested-version DuckDB: DDL for the nine pivot tables, bulk upsert inserters, and every read query the explorer and delta loader use. |
| [`delta.py`](../dpm/delta.py) | Pure comparison of two `DpmDataset`s → a `DeltaResult` (structure + metric + dimension deltas), plus the summary aggregation. |
| [`delta_schema.py`](../dpm/delta_schema.py) | Canonical (snake_case) column lists for the delta tables and the DPS pivot, plus the canonical→PascalCase maps the Excel renderer uses. |
| [`delta_db.py`](../dpm/delta_db.py) | Persist a `DeltaResult` (plus apply context and the resolved DPS pivot) to a cached delta DuckDB, and every query the Explore-Delta screen and apply step run against it. |
| [`xbrl.py`](../dpm/xbrl.py) | Apply a delta to an XBRL instance: flatten metric facts with regexes, reduce each to its canonical DPS key, match against the delta, and splice the rewritten bytes. |
| [`excel.py`](../dpm/excel.py) | Render the human-facing delta workbook and the apply debug workbook (xlsxwriter). |
| [`workflows.py`](../dpm/workflows.py) | Orchestration façade — see above. |

### Presentation (`dpm/ui/`)

| Screen | File | Workflow |
| --- | --- | --- |
| Main menu | `main_screen.py` | Entry hub; routes to the five workflows and Settings. |
| Settings | `settings_screen.py` | Add/remove tracked versions (with optional explicit URL); theme is set via the command palette and persisted. |
| DPM Ingest | `ingest_screen.py` | Download (if needed) and ingest a version into `db/versions/`. |
| DPM Delta | `delta_screen.py` | Pick old/new version + perimeters, compute the delta, export the Excel workbook. |
| Explore Database | `explore_screen.py` | Browse one ingested version — perimeter → template → subtemplate → cells, metrics, dimensions/members. |
| Explore Delta | `explore_delta_screen.py` | Browse the computed changes between two versions interactively. |
| XBRL Apply Delta | `apply_screen.py` | Pick two versions + an instance file, apply the delta, write a new instance (with dry-run and debug-workbook options). |

Supporting UI helpers: `_utils.py` (shared widgets, modals such as
`ConfirmCancelModal` and the DB-override modal), `log_handler.py` (routes
`logging` records into a TUI log widget), and one `.tcss` stylesheet per screen.

Long-running work (download, ingest, compare, apply) runs in Textual `@work`
threads so the UI stays responsive; `app.py`'s global **Back** handler confirms
before cancelling a screen's in-flight worker.

## End-to-end data flow

### 1. Ingest

`ingest_screen` → `workflows.run_ingest`:

1. `eiopa.fetch_dpm_database(version, url)` ensures a local `{version}.db`
   SQLite file (cache-first; resolves + downloads + extracts if absent).
2. `dpm_source.parse_dpm_database` projects it into a `ParsedModel` — nine row
   lists plus a model-version row.
3. `db.open_db` creates `db/versions/{version}.duckdb` and the `insert_*`
   helpers bulk-load each table inside one transaction.

Output: a self-contained per-version DuckDB. See [ingestion](ingestion.md).

### 2. Delta

`delta_screen` / `explore_delta_screen` → `workflows.run_delta` /
`ensure_delta_db`:

1. `workflows._load_dataset` reads each version's DuckDB into a `DpmDataset`
   (metric cells joined to their dimensional signature, metric catalogue,
   dimension members), filtered to the selected perimeters.
2. `delta.compare_versions` diffs the two datasets into a `DeltaResult` with
   three sections: `structure` (per-cell), `metrics` (catalogue), `dimensions`
   (members).
3. For the interactive/apply path, `delta_db.save_delta_db` caches the result at
   `db/delta/{old}_to_{new}.duckdb`, additionally persisting the **resolved DPS
   pivot** (`datapoint_changes`) and the apply context (`open_dimensions`,
   `default_members`).
4. `excel.generate_delta_workbook` renders the workbook (Summary + Metrics +
   Dimensions + one sheet per perimeter).

See [delta](delta.md).

### 3. Apply

`apply_screen` → `workflows.run_apply_delta`:

1. `ensure_delta_db` resolves (computing if missing) the cached delta DuckDB for
   the two versions.
2. `xbrl.apply_delta` reads the DPS pivot + apply context from it, detects the
   instance's perimeter, and rewrites the XBRL bytes: deleting facts whose cell
   vanished, renaming metrics, and re-pointing dimensional contexts where a cell
   moved — cloning contexts as needed. The original file is never modified.

See [applying a delta to XBRL](apply_xbrl.md).
