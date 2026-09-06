# Ingestion

Ingestion turns an official EIOPA DPM release into a compact, self-contained
per-version DuckDB (`db/versions/<version>.duckdb`). It has two stages:
**fetch** the release ([`dpm/eiopa.py`](../dpm/eiopa.py)) and **project** it into
the pivot schema ([`dpm/dpm_source.py`](../dpm/dpm_source.py) →
[`dpm/db.py`](../dpm/db.py)). The orchestrator is
[`workflows.run_ingest`](../dpm/workflows.py).

> **Source of truth:** the official DPM **SQLite database** is the *sole*
> ingestion source. `run_ingest` rejects anything that is not a `.db` /
> `.sqlite` / `.sqlite3` file. (Earlier annotated-templates-workbook parsing has
> been removed.)

## Stage 1 — fetch the release (`dpm.eiopa`)

`fetch_dpm_database(version, dest_dir, url=None)` returns a local
`{version}.db`, downloading it only if absent (cache-first).

**Why resolution is non-trivial.** The download URL cannot be derived from a
version string alone:

- hotfix builds live in suffixed folders with inconsistent casing
  (`2.8.2_hotfix` vs `2.8.1_Hotfix`);
- the zip filename spells the product either `Solvency_II` or `SolvencyII`;
- some builds carry non-mechanical suffixes.

So `candidate_urls(version)` generates a **bounded, ordered** set of candidate
URLs (clean release first, then ascending hotfix numbers up to `_MAX_HOTFIX`,
across the observed casings and both filename spellings), and
`resolve_dpm_database_url` HEAD-probes them, keeping the **last** success — so a
plain version like `2.8.0` resolves to its latest hotfix. A caller can always
supply an explicit `url` (stored per version in the config) to bypass probing —
needed for the odd non-mechanical build.

**Download & extract.** `download_file` streams the zip to a `.part` temp file
(atomic rename on success) and fails fast if the payload lacks the `PK` zip
magic (catching HTML error pages). `_extract_database` then pulls the **largest**
member whose name ends in a database suffix — DPM zips ship one `.db` alongside
small readme/licence files — and writes it to `{version}.db`. Progress is
reported by real byte counts for the download and extraction; unmeasurable steps
(resolve, cache hit) report indeterminate progress.

## Stage 2 — project into the pivot schema (`dpm.dpm_source`)

`parse_dpm_database(db_path, version)` opens the SQLite read-only and projects
its ~30 XBRL-oriented tables into the subset the app needs: a `ParsedModel` of
nine row lists plus a model-version row. Two pieces of data that *only* the DPM
database carries are captured here:

- **default members** (`mMember.IsDefaultMember`) — the member a dimension
  assumes when an instance omits it; needed to canonicalise facts on apply.
- a **canonical Data Point Signature (DPS)** per cell (`mTableCell.DPS`).

### The Data Point Signature (DPS)

Each cell's `DPS` is the model's own canonical key for that cell, e.g.:

```
MET(s2md_met:mi343)|s2c_dim:BL(s2c_LB:x91)|s2c_dim:VG(s2c_AM:x80)
```

Properties the projection relies on (verified against the 2.10.0 database):

- The leading `MET(...)` is the metric qname. It is stored upper-cased
  (`s2md_MET:`) in `mMember` but DPS/XBRL use `s2md_met:`; `_norm_metric`
  normalises to the lower-case form.
- The DPS **already omits default members** and lists only the **non-default**
  members that pin the cell down. (`VG=x80` stays because VG's domain defaults to
  `x0`, so `x80` is non-default.)
- `(*)` marks a fully **open/typed** axis; `(*[dom;start;incl])` / `(*?[…])`
  mark an axis open over a restricted domain subtree. These carry no fixed
  member — the instance supplies the concrete value — and are preserved verbatim
  on `facts.data_point_signature` but excluded from the fixed member pairs.

`parse_dps_members` returns exactly the fixed `(dimension, member)` pairs;
`parse_dps_metric` returns the normalised metric qname. These are what make apply
exact — the fixed signature, not the metric qname alone, identifies a cell.

### What each pivot table is projected from

| Pivot table | DPM source | Notes |
| --- | --- | --- |
| `perimeters` | `mModule.ModuleCode` | The reporting perimeters (ars, qrs, …). |
| `templates` | `mTemplateOrTable` where type = `TableGroup` | Template code + label. |
| `subtemplates` | `mTable` | Keyed on the real data-table code; parent 4-part code is the template. `subtemplate_type` (`metrics_row`/`metrics_col`) is derived from the cell layout — a table is `metrics_col` when its cells span more than one column code. |
| `perimeter_template` | `mModuleBusinessTemplate` join | Which templates each perimeter reports. |
| `metrics` | `mMetric` ⋈ `mMember` ⋈ `mDomain` | Code + label, plus `data_type`, `period_type` (flow), `balance`, and `referenced_domain` (the value domain of an enumerated metric). Metrics referenced by a cell but absent from `mMetric` still get a bare row. |
| `dimensions` | `mDimension` | Code, label, and resolved `default_member_code` (explicit `DefaultMemberID`, else the domain's `IsDefaultMember`). |
| `dimension_members` | `mMember` ⋈ representative `mDimension` | Member code, label, `is_default`. See the caveat below. |
| `facts` (cells) | `mTableCell` where `DPS IS NOT NULL` | One row per cell: `(subtemplate, row_code, column_code)` from `mCellPosition` axis orientation (Y = row, X = column), the metric from the DPS head, and the full `data_point_signature`. |
| `fact_dimensions` | parsed DPS members | One row per (cell + fixed dimension + member) — the actual dimensional signature. |
| `model_version` | `mTaxonomy` | The version label plus `from_date`/`to_date`. |

### The `dimension_members` representative-dimension caveat

`dimension_members` has `member_code` as its primary key, so it stores exactly
**one** dimension per member — but a member really belongs to a **domain**, and a
domain can back several dimensions. Ingestion therefore maps each member to a
**representative** dimension of its domain (deterministic `MIN(DimensionID)`)
purely so the explorer can group members somewhere. It is *not* an authoritative
binding — `fact_dimensions` is the source of truth for actual bindings. This is
covered in depth in the [reporting data model](reporting_data_model.md#7-known-modeling-caveat-dimension_membersdimension_code).

## Stage 3 — load into DuckDB (`dpm.db`)

`run_ingest` opens `db/versions/{version}.duckdb` (`db.open_db` runs the DDL for
the nine tables) and bulk-loads each `ParsedModel` list inside one transaction.
`_bulk_upsert` transfers rows via Arrow/Polars (`INSERT OR REPLACE`) — orders of
magnitude faster than `executemany` — writing only the columns present in the
rows so nullable columns default to `NULL`. `run_ingest` returns a stats dict
(row counts per table) that the ingest screen displays.

The nine tables and their keys are defined by `_DDL` in
[`dpm/db.py`](../dpm/db.py); their business meaning is in the
[reporting data model](reporting_data_model.md#6-mapping-to-the-duckdb-schema).

## Progress accounting

`run_ingest` emits a fixed number of steps (`_INGEST_STEPS = 22`: one read + the
projection's read steps + the ten inserts + model-version + commit) so the
progress bar advances predictably regardless of database size.
