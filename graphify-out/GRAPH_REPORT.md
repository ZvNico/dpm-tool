# Graph Report - .  (2026-08-13)

## Corpus Check
- 29 files · ~19,803 words
- Verdict: corpus is large enough that graph structure adds value.

## Summary
- 661 nodes · 1517 edges · 27 communities (18 shown, 9 thin omitted)
- Extraction: 97% EXTRACTED · 3% INFERRED · 0% AMBIGUOUS · INFERRED: 41 edges (avg confidence: 0.55)
- Token cost: 15,000 input · 3,674 output

## Community Hubs (Navigation)
- DuckDB Persistence Layer
- Workbook Template Parsing
- App Shell & Config
- TOC & Perimeter Extraction
- Explore Delta Screen
- Delta Computation
- Delta DuckDB Cache
- XBRL Apply Screen
- Explore Database Screen
- UI Modals & File Pickers
- Ingest Screen
- Delta Screen
- DPM Concepts & Workflows
- Parser Layout Tests
- XBRL Delta Application
- Logging Handler
- Test Fixtures
- Any Type
- DataFrame (dpm)
- DataFrame (delta)
- Filters
- DuckDB Connection
- Polars Expr
- MainScreen
- dpm-tool CLI

## God Nodes (most connected - your core abstractions)
1. `ExploreDeltaScreen` - 33 edges
2. `ExploreScreen` - 31 edges
3. `norm()` - 23 edges
4. `SourceSelect` - 21 edges
5. `DeltaScreen` - 21 edges
6. `TocEntry` - 20 edges
7. `ApplyScreen` - 19 edges
8. `IngestScreen` - 19 edges
9. `run_ingest()` - 18 edges
10. `WorkbookCache` - 17 edges

## Surprising Connections (you probably didn't know these)
- `TestNorm` --uses--> `TocEntry`  [INFERRED]
  tests/test_parsing.py → dpm/_types.py
- `TestIsQname` --uses--> `TocEntry`  [INFERRED]
  tests/test_parsing.py → dpm/_types.py
- `TestExtractQname` --uses--> `TocEntry`  [INFERRED]
  tests/test_parsing.py → dpm/_types.py
- `TestExtractMetricLabel` --uses--> `TocEntry`  [INFERRED]
  tests/test_parsing.py → dpm/_types.py
- `TestNearestText` --uses--> `TocEntry`  [INFERRED]
  tests/test_parsing.py → dpm/_types.py

## Import Cycles
- None detected.

## Hyperedges (group relationships)
- **Ingest to Delta to Apply Pipeline** — readme_dpm_ingest, readme_dpm_delta, readme_xbrl_apply_delta, readme_versioned_database [EXTRACTED 0.85]

## Communities (27 total, 9 thin omitted)

### Community 0 - "DuckDB Persistence Layer"
Cohesion: 0.05
Nodes (92): _bulk_upsert(), insert_dimension_members(), insert_dimensions(), insert_fact_dimensions(), insert_facts(), insert_metrics(), insert_perimeter_template(), insert_perimeters() (+84 more)

### Community 1 - "Workbook Template Parsing"
Cohesion: 0.06
Nodes (49): _extract_metrics_col(), _extract_metrics_row(), _extract_window_dimensions(), _is_subtemplate_header(), _pad(), _parse_subtemplate_window(), _parse_template_sheet(), parse_workbook() (+41 more)

### Community 2 - "App Shell & Config"
Cohesion: 0.06
Nodes (48): DpmToolApp, main(), work, Go back a screen, confirming first if the current screen has a running task.…, add_version(), _load_raw(), load_theme(), load_versions() (+40 more)

### Community 3 - "TOC & Perimeter Extraction"
Cohesion: 0.07
Nodes (25): first_match(), discover_toc_sheet(), extract_perimeters(), extract_toc(), extract_toc_perimeters(), filter_entries_by_perimeter(), _find_header_row(), looks_like_perimeter() (+17 more)

### Community 4 - "Explore Delta Screen"
Cohesion: 0.06
Nodes (28): _colored_badge(), _counts_str(), ExploreDeltaScreen, _member_status(), _merge(), _node_color(), _parse_dims(), Changed (+20 more)

### Community 5 - "Delta Computation"
Cohesion: 0.13
Nodes (36): added_deleted_df(), classify_type(), compare_dimensions(), compare_metrics(), compare_versions(), empty_delta_df(), empty_metric_df(), _empty_status_df() (+28 more)

### Community 6 - "Delta DuckDB Cache"
Cohesion: 0.13
Nodes (37): delta_db_path(), delta_perimeters(), load_apply_changes(), load_cell_changes(), load_delta_counts(), load_delta_meta(), load_delta_result(), load_delta_tree() (+29 more)

### Community 7 - "XBRL Apply Screen"
Cohesion: 0.07
Nodes (17): ApplyScreen, Changed, ComposeResult, Exception, Path, Pressed, Screen, work (+9 more)

### Community 8 - "Explore Database Screen"
Cohesion: 0.11
Nodes (9): _cells(), ExploreScreen, Changed, Exception, NodeSelected, Path, Pressed, RowSelected (+1 more)

### Community 9 - "UI Modals & File Pickers"
Cohesion: 0.08
Nodes (21): Click, ConfirmCancelModal, file_filters(), _FilePicker, OverrideDbModal, prompt_open_file(), prompt_open_xlsx(), ComposeResult (+13 more)

### Community 10 - "Ingest Screen"
Cohesion: 0.15
Nodes (9): IngestScreen, Changed, Exception, Path, Pressed, Screen, work, Auto-fill the detected version as the file path is typed. Leaves the field… (+1 more)

### Community 11 - "Delta Screen"
Cohesion: 0.20
Nodes (6): DeltaScreen, Changed, Exception, Path, Pressed, Screen

### Community 12 - "DPM Concepts & Workflows"
Cohesion: 0.14
Nodes (18): EIOPA Annotated-Templates Workbook, config.json (tracked versions and UI theme), EIOPA Solvency II Data Point Model (DPM), Reviewable Delta Workbook (Delta_DPM.xlsx), DPM Delta Workflow, DPM Ingest Workflow, dpm-tool, DPM_TOOL_HOME env override (+10 more)

### Community 13 - "Parser Layout Tests"
Cohesion: 0.24
Nodes (4): metric qnames run across the anchor row; a row-scoped dimension (declared as a…, metric qnames run down a column; dim declared in the anchor row to the right of…, TestMetricsColDimensions, TestMetricsRowBothScopes

### Community 14 - "XBRL Delta Application"
Cohesion: 0.36
Nodes (10): apply_delta(), build_delta_maps(), detect_perimeter_from_xml_bytes(), _expand_deleted_span(), flag_facts_with_delta(), flatten_metric_facts(), DataFrame, Path (+2 more)

### Community 15 - "Logging Handler"
Cohesion: 0.32
Nodes (5): attach(), detach(), RichLogHandler, LogRecord, RichLog

## Knowledge Gaps
- **8 isolated node(s):** `dpm-tool`, `EIOPA Solvency II Data Point Model (DPM)`, `Textual TUI`, `EIOPA Annotated-Templates Workbook`, `XBRL Instance Document` (+3 more)
  These have ≤1 connection - possible missing edges or undocumented components.
- **9 thin communities (<3 nodes) omitted from report** — run `graphify query` to explore isolated nodes.

## Suggested Questions
_Questions this graph is uniquely positioned to answer:_

- **Why does `ExploreDeltaScreen` connect `Explore Delta Screen` to `App Shell & Config`, `Delta DuckDB Cache`, `XBRL Apply Screen`?**
  _High betweenness centrality (0.090) - this node is a cross-community bridge._
- **Why does `ExploreScreen` connect `Explore Database Screen` to `DuckDB Persistence Layer`, `App Shell & Config`, `XBRL Apply Screen`?**
  _High betweenness centrality (0.082) - this node is a cross-community bridge._
- **Why does `SourceSelect` connect `XBRL Apply Screen` to `DuckDB Persistence Layer`, `Explore Delta Screen`, `Delta DuckDB Cache`, `Explore Database Screen`, `Delta Screen`?**
  _High betweenness centrality (0.057) - this node is a cross-community bridge._
- **Are the 2 inferred relationships involving `ExploreDeltaScreen` (e.g. with `SourceSelect` and `MainScreen`) actually correct?**
  _`ExploreDeltaScreen` has 2 INFERRED edges - model-reasoned connections that need verification._
- **Are the 3 inferred relationships involving `ExploreScreen` (e.g. with `SourceSelect` and `DbContents`) actually correct?**
  _`ExploreScreen` has 3 INFERRED edges - model-reasoned connections that need verification._
- **Are the 3 inferred relationships involving `SourceSelect` (e.g. with `ApplyScreen` and `ExploreDeltaScreen`) actually correct?**
  _`SourceSelect` has 3 INFERRED edges - model-reasoned connections that need verification._
- **What connects `dpm-tool`, `EIOPA Solvency II Data Point Model (DPM)`, `Textual TUI` to the rest of the system?**
  _8 weakly-connected nodes found - possible documentation gaps or missing edges._