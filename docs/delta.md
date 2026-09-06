# Delta — comparing two versions

The delta is the difference between two ingested DPM versions. It is computed by
[`dpm/delta.py`](../dpm/delta.py) into a `DeltaResult`, cached as a queryable
DuckDB by [`dpm/delta_db.py`](../dpm/delta_db.py), rendered to Excel by
[`dpm/excel.py`](../dpm/excel.py), and browsed in the Explore-Delta screen. The
canonical column schema lives in [`dpm/delta_schema.py`](../dpm/delta_schema.py).

## Inputs

`workflows._load_dataset` reads each version's DuckDB into a `DpmDataset`:

- **`metrics`** — every metric *cell*: `perimeter, template_code,
  subtemplate_code, row_code, column_code, qname, metric_label, row_label,
  column_label`, left-joined to its serialized dimensional signature
  (`dimensions`, a stable `dim=member;…` string sorted by dimension code).
  Filtered to the selected perimeters.
- **`metric_catalog`** — the full `(metric_code, metric_label)` catalogue
  (version-level, not perimeter-scoped, so compared in full).
- **`dimension_members`** — flat `dimension × member` frame for the dimensions
  delta.
- **`entries`** — the perimeter → template → subtemplate structure, from which
  `DpmDataset.templates` and `.subtemplates` sets are derived.

## The three sections (`DeltaResult`)

`compare_versions(old, new)` produces three Polars frames.

### 1. Structure (`DELTA_STRUCTURE_COLS`)

The per-cell delta plus structural (template/subtemplate) additions and
deletions. This is the heart of the delta. It is built in stages by
`_structure_delta`:

1. **Coordinate comparison** (`metric_status_df`) — inner-join old and new cells
   on the coordinate key (`KEY_COLS`: perimeter, template, subtemplate, row,
   column). Matched cells are `Modified` if their qname *or* dimensions changed,
   else `Kept`. Unmatched cells fall through as old-only / new-only.
2. **QName-move detection** (`qname_modified_df`) — a cell whose *coordinate*
   moved but whose qname is unique within its perimeter on both sides is matched
   old→new and reported as a single `Modified` row (rather than a Deleted + Added
   pair). Ambiguous (non-unique) qnames are left for the next stage.
3. **Added / deleted** (`added_deleted_df`) — remaining new-only cells become
   `Added`, remaining old-only become `Deleted`.
4. **Cell-type classification** (`classify_type`) — every metric row is stamped
   with a `type`: `Column` (no row code), `Matrix` (its subtemplate uses a
   non-default column, per `subtemplate_matrix_lookup`), or `Row`.
5. **Structural rows** (`structural_delta_df`) — set-difference of the
   `templates` and `subtemplates` sets yields `Template`/`SubTemplate` marker
   rows with status `Added`/`Deleted`.

Each row carries old/new values side-by-side (`qname_old`/`qname_new`,
`*_label_old`/`*_label_new`, `dimensions_old`/`dimensions_new`) plus `status`
and `type`.

### 2. Metrics (`DELTA_METRIC_COLS`)

`compare_metrics` full-joins the two metric catalogues on `metric_code`:
`Added` (new only), `Deleted` (old only), `Modified` (label changed), else
`Kept`. **Every** metric is listed with its status — the sheet is a full
catalogue view, not just the changed subset (the Explore-Delta screen filters
`Kept` out via `load_metric_changes`).

### 3. Dimensions / members (`DELTA_DIMENSION_COLS`)

`compare_dimensions` full-joins on `(dimension_code, member_code)`:
`Added`/`Deleted`/`Modified` (member or dimension label changed). Unlike
metrics, `Kept` rows are dropped here.

## Summary

`generate_summary` aggregates the structure delta into counts and percentages
per status for metric cells (`Row`+`Column`+`Matrix`) and per structural type
(Template/SubTemplate Added/Deleted). It backs the workbook's Summary sheet.

## The cached delta database (`dpm.delta_db`)

For the interactive and apply paths, the result is persisted at
`db/delta/{old}_to_{new}.duckdb` by `save_delta_db`. `ensure_delta_db` is
cache-first: it returns an existing file untouched, and otherwise computes the
delta across **all** perimeters (so the artifact is complete) before saving.

The delta DB is the **canonical, queryable source of truth** and holds:

| Table | Contents |
| --- | --- |
| `structure_changes` | The cell-keyed structure delta (`DELTA_STRUCTURE_COLS`). |
| `metric_changes` | The full metric-catalogue delta (incl. `Kept`). |
| `member_changes` | The dimensions/members delta. |
| `datapoint_changes` | The **resolved DPS pivot** apply reads directly — see below. |
| `open_dimensions` | Dimension codes that appear as an open axis in any DPS (union of both versions). |
| `default_members` | Member codes flagged as their domain's default (union of both versions). |
| `meta` | Old/new version labels + created-at timestamp. |

`open_dimensions` and `default_members` are the **apply context**: the data
needed to reduce an instance fact to its canonical DPS key without reopening the
version DBs.

### `datapoint_changes` — the DPS pivot

Rather than making apply re-derive its matching index from `structure_changes`
each run, the resolved index is computed once at delta-build time
(`workflows._build_datapoint_changes` → `xbrl.build_dps_delta` →
`dps_delta_to_rows`, per perimeter) and persisted. Each row is
`perimeter, qname, role, fixed, target_qname, target_fixed` where `role` is
`survivor` / `modified` / `deleted` and `fixed` is a cell's fixed-member
signature. Apply reassembles it with `rows_to_dps_delta`. The full semantics are
described in [applying a delta to XBRL](apply_xbrl.md). Older cached deltas that
predate this table are handled gracefully — apply falls back to deriving the
index from `structure_changes`.

## The Excel workbook (`dpm.excel`)

`generate_delta_workbook` writes, in order:

1. **Summary** — the aggregated counts/percentages.
2. **Metrics** — the metric-catalogue delta, status-coloured.
3. **Dimensions** — the members delta, status-coloured.
4. **One sheet per perimeter** — that perimeter's structure delta.

Rows are conditionally coloured by status (Added / Deleted / Modified), and
canonical snake_case columns are mapped to PascalCase headers via the `*_XLSX`
maps in `delta_schema`. Default output name encodes perimeter coverage
(`delta_output_name` → `Delta_Dpm_all.xlsx` or `Delta_Dpm_<p1>_<p2>.xlsx`).

## Explore-Delta

The Explore-Delta screen queries the cached delta DB directly:
`load_delta_tree` builds a perimeter → template → subtemplate tree showing only
nodes with real changes, colouring a node red/green only when a *single* status
accounts for **all** of its cells; `load_cell_changes` lists the changed cells
for a selected node; `load_metric_changes` / `load_member_changes` back the
catalogue views. A toggle can hide "label-only" changes (a `Modified` cell whose
qname and dimensions are identical and only a label differs).
