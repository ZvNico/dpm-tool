# Graph Report - michael  (2026-09-06)

## Corpus Check
- Corpus is ~33,761 words - fits in a single context window. You may not need a graph.

## Summary
- 699 nodes · 1656 edges · 30 communities (24 shown, 4 thin omitted)
- Extraction: 95% EXTRACTED · 5% INFERRED · 0% AMBIGUOUS · INFERRED: 77 edges (avg confidence: 0.92)
- Token cost: 55,011 input · 0 output

## Community Hubs (Navigation)
- DPM Database & Ingestion
- XBRL Delta Apply
- App Config & EIOPA Fetch
- Delta Explorer Screen
- Delta DB Persistence
- Delta Computation Engine
- Data Explorer Screen
- Ingest Screen & Modals
- DPM Source Parsing & Types
- Excel Workbook Export
- UI Compose & Widgets
- Delta Screen
- Apply Screen
- Constants & Screen Registry
- Clipboard Copy Utils
- Apply/Delta Docs
- File Picker Widget
- Architecture Docs
- File Open Prompts
- Ingestion & Matching Docs
- Configuration & README Docs
- Exact Matching & Gap Analysis
- Dimension Data Model
- Fact & Metric Data Model
- Test Fixtures
- Worker Threads Doc
- Output Path Config
- Package Root

## God Nodes (most connected - your core abstractions)
1. `ExploreDeltaScreen` - 37 edges
2. `ExploreScreen` - 33 edges
3. `apply_delta()` - 25 edges
4. `DeltaScreen` - 22 edges
5. `SourceSelect` - 21 edges
6. `save_delta_db()` - 20 edges
7. `ApplyScreen` - 20 edges
8. `IngestScreen` - 20 edges
9. `_project()` - 19 edges
10. `run_ingest()` - 19 edges

## Surprising Connections (you probably didn't know these)
- `Solvency II reporting` --semantically_similar_to--> `Data Point Model (DPM)`  [INFERRED] [semantically similar]
  temp.md → README.md
- `_run_delta()` --uses--> `DeltaResult`  [INFERRED]
  tests/test_golden.py → dpm/_types.py
- `Default-member matching bug (mi363)` --references--> `Default members`  [INFERRED]
  temp.md → docs/ingestion.md
- `Solvency II reporting` --conceptually_related_to--> `Fact`  [INFERRED]
  temp.md → docs/reporting_data_model.md
- `DpmToolApp` --uses--> `MainScreen`  [INFERRED]
  app.py → dpm/ui/main_screen.py

## Import Cycles
- None detected.

## Hyperedges (group relationships)
- **Ingest to Delta to Apply pipeline** — docs_ingestion_ingestion, docs_delta_delta_result, docs_apply_xbrl_apply, docs_architecture_end_to_end_flow [EXTRACTED 1.00]
- **Core reporting data model vocabulary** — docs_reporting_data_model_fact, docs_reporting_data_model_metric, docs_reporting_data_model_dimension, docs_reporting_data_model_member, docs_reporting_data_model_domain, docs_reporting_data_model_context [EXTRACTED 1.00]
- **DPS-based exact matching backbone** — docs_ingestion_dps, docs_delta_datapoint_changes, docs_apply_xbrl_dps_delta, docs_ingestion_default_members [INFERRED 0.85]

## Communities (30 total, 4 thin omitted)

### Community 0 - "DPM Database & Ingestion"
Cohesion: 0.07
Nodes (78): _bulk_upsert(), insert_dimension_members(), insert_dimensions(), insert_fact_dimensions(), insert_facts(), insert_metrics(), insert_model_version(), insert_perimeter_template() (+70 more)

### Community 1 - "XBRL Delta Apply"
Cohesion: 0.06
Nodes (76): load_apply_context(), Return ``(open_dimensions, default_members)`` stored with the delta. Older…, generate_apply_debug_workbook(), Readable apply-delta debug: a Summary plus one metric-rooted sheet per action.…, ApplyStats, _build_datapoint_changes(), DataFrame, Resolve the cell-keyed structure delta into the per-perimeter DPS pivot. Runs… (+68 more)

### Community 2 - "App Config & EIOPA Fetch"
Cohesion: 0.05
Nodes (53): App, DpmToolApp, main(), work, Go back a screen, confirming first if the current screen has a running task.…, add_version(), _load_raw(), load_theme() (+45 more)

### Community 3 - "Delta Explorer Screen"
Cohesion: 0.07
Nodes (29): _colored_badge(), _counts_str(), ExploreDeltaScreen, _member_status(), _merge(), _node_color(), _parse_dims(), Changed (+21 more)

### Community 4 - "Delta DB Persistence"
Cohesion: 0.11
Nodes (54): delta_db_path(), delta_perimeters(), load_apply_changes(), load_cell_changes(), load_datapoint_changes(), load_delta_counts(), load_delta_meta(), load_delta_result() (+46 more)

### Community 5 - "Delta Computation Engine"
Cohesion: 0.08
Nodes (45): filter_entries_by_perimeter(), Keep only entries whose perimeter (case-insensitively) is in ``selected``., added_deleted_df(), classify_type(), compare_dimensions(), compare_metrics(), compare_versions(), empty_delta_df() (+37 more)

### Community 6 - "Data Explorer Screen"
Cohesion: 0.10
Nodes (11): _cells(), ExploreScreen, Changed, Exception, NodeSelected, Path, Pressed, RowSelected (+3 more)

### Community 7 - "Ingest Screen & Modals"
Cohesion: 0.10
Nodes (14): IngestScreen, Changed, Exception, Path, Pressed, work, Prompt to override an existing DB for this version, then start ingest.…, Drive the bar from a ``(done, total, label)`` event on the UI thread.… (+6 more)

### Community 8 - "DPM Source Parsing & Types"
Cohesion: 0.16
Nodes (28): Connection, _norm_metric(), parse_dpm_database(), parse_dps_members(), parse_dps_metric(), _project(), Path, Ingest the official EIOPA DPM SQLite database into the thin pivot schema. This… (+20 more)

### Community 9 - "Excel Workbook Export"
Cohesion: 0.12
Nodes (23): _col_width(), _count_by_qname(), generate_delta_workbook(), _group_by_metric(), DataFrame, Path, A metric-rooted sheet: ``display_rows`` is ``[(level, [values]), …]`` where…, Yield ``(key, count)`` per distinct ``cols`` tuple, in first-seen order.… (+15 more)

### Community 10 - "UI Compose & Widgets"
Cohesion: 0.12
Nodes (11): DataTable, ComposeResult, ComposeResult, Select of ingested DB versions with a greyed placeholder on the collapsed…, SourceSelect, ComposeResult, ComposeResult, ComposeResult (+3 more)

### Community 11 - "Delta Screen"
Cohesion: 0.18
Nodes (7): DeltaScreen, Changed, Exception, Path, Pressed, delta_output_name(), Filename for a delta workbook, encoding its perimeter coverage.…

### Community 12 - "Apply Screen"
Cohesion: 0.17
Nodes (8): ApplyScreen, Changed, Exception, Filters, Path, Pressed, Screen, work

### Community 13 - "Constants & Screen Registry"
Cohesion: 0.30
Nodes (10): Path, Resolve a user-entered output path for an export. A bare filename (no directory…, resolve_output_path(), attach(), detach(), RichLogHandler, available_db_versions(), Sort key for dotted numeric DPM versions, e.g. '2.10.0' > '2.8.2'. (+2 more)

### Community 14 - "Clipboard Copy Utils"
Cohesion: 0.22
Nodes (11): CopyScreen, _datatable_all_text(), _plain(), The screen's output log, so ``c``/``a`` copy it even when focus is on a…, Flatten a possibly-styled cell/label to plain text for the clipboard., Text to copy for the currently focused widget, or None if it has none.…, The whole table as tab-separated text: a header row plus every data row., Write ``text`` to the system clipboard via an external tool; True on success. (+3 more)

### Community 15 - "Apply/Delta Docs"
Cohesion: 0.18
Nodes (12): Apply (XBRL migration), ApplyStats, Regex byte splicing rewrite, Context cloning (_clone_context), DpsDelta matching index, Perimeter auto-detection, Survival-wins resolution order, Apply context (open_dimensions, default_members) (+4 more)

### Community 16 - "File Picker Widget"
Cohesion: 0.22
Nodes (6): Click, _FilePicker, FileOpen tailored for a click/double-click pick flow. - Hides the filter…, FileOpen, Highlighted, on

### Community 17 - "Architecture Docs"
Cohesion: 0.28
Nodes (9): End-to-end data flow (ingest/delta/apply), Engine/Presentation layer split, workflows.py orchestration facade, DeltaResult, DpmDataset, QName-move detection, Structure delta (_structure_delta), The pipeline (ingest/diff/apply) (+1 more)

### Community 18 - "File Open Prompts"
Cohesion: 0.28
Nodes (9): file_filters(), prompt_open_dpm_source(), prompt_open_file(), Filters, Path, Screen, A two-entry filter: the given suffix (e.g. '.xlsx') plus an all-files fallback., Open a file-select dialog and return the chosen path (or None). Must be awaited… (+1 more)

### Community 19 - "Ingestion & Matching Docs"
Cohesion: 0.25
Nodes (8): _decide_fact per-fact decision, _match_fixed candidate matching, Arrow/Polars bulk upsert, Default members, Data Point Signature (DPS), ParsedModel (nine pivot tables), Pivot schema projection (dpm_source), Cell (data point)

### Community 20 - "Configuration & README Docs"
Cohesion: 0.29
Nodes (7): config.json app config, Per-user platform directory layout, Fetch release (eiopa), Bounded URL candidate resolution, dpm-tool, DPM_TOOL_HOME override, Textual TUI

### Community 21 - "Exact Matching & Gap Analysis"
Cohesion: 0.40
Nodes (5): Exact per-cell matching, DPM SQLite as sole ingestion source, Default-member matching bug (mi363), Official EIOPA DPM Database (SQLite), DB architecture gap analysis

### Community 22 - "Dimension Data Model"
Cohesion: 0.50
Nodes (5): dimension_members representative-dimension caveat, Dimension, Domain, fact_dimensions as source of truth, Member

### Community 23 - "Fact & Metric Data Model"
Cohesion: 0.40
Nodes (5): Context, Fact, Metric, Data Point Model (DPM), Solvency II reporting

## Knowledge Gaps
- **9 isolated node(s):** `dpm-tool`, `Textual TUI`, `Perimeter auto-detection`, `ApplyStats`, `resolve_output_path` (+4 more)
  These have ≤1 connection - possible missing edges or undocumented components. (Counts symbols only; 206 node(s) total have ≤1 connection when file, concept and rationale nodes are included.)
- **4 thin communities (<3 nodes) omitted from report** — run `graphify query` to explore isolated nodes.

## Suggested Questions
_Questions this graph is uniquely positioned to answer:_

- **Why does `ExploreDeltaScreen` connect `Delta Explorer Screen` to `UI Compose & Widgets`, `App Config & EIOPA Fetch`, `Clipboard Copy Utils`?**
  _High betweenness centrality (0.093) - this node is a cross-community bridge._
- **Why does `ExploreScreen` connect `Data Explorer Screen` to `UI Compose & Widgets`, `App Config & EIOPA Fetch`, `Constants & Screen Registry`, `Clipboard Copy Utils`?**
  _High betweenness centrality (0.078) - this node is a cross-community bridge._
- **Why does `SourceSelect` connect `UI Compose & Widgets` to `Delta Explorer Screen`, `Data Explorer Screen`, `Delta Screen`, `Apply Screen`, `Constants & Screen Registry`?**
  _High betweenness centrality (0.049) - this node is a cross-community bridge._
- **Are the 2 inferred relationships involving `ExploreDeltaScreen` (e.g. with `SourceSelect` and `MainScreen`) actually correct?**
  _`ExploreDeltaScreen` has 2 INFERRED edges - model-reasoned connections that need verification._
- **Are the 3 inferred relationships involving `ExploreScreen` (e.g. with `SourceSelect` and `DbContents`) actually correct?**
  _`ExploreScreen` has 3 INFERRED edges - model-reasoned connections that need verification._
- **Are the 2 inferred relationships involving `DeltaScreen` (e.g. with `RichLogHandler` and `MainScreen`) actually correct?**
  _`DeltaScreen` has 2 INFERRED edges - model-reasoned connections that need verification._
- **What connects `dpm-tool`, `Textual TUI`, `Perimeter auto-detection` to the rest of the system?**
  _9 weakly-connected nodes found - possible documentation gaps or missing edges._