from __future__ import annotations

from pathlib import Path

from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.widgets import (
    Button,
    DataTable,
    Footer,
    Input,
    Label,
    Select,
    Static,
    TabbedContent,
    TabPane,
    Tree,
)
from textual.widgets.tree import TreeNode

from dpm._constants import VERSIONS_DIR
from dpm.ui._utils import CopyScreen
from dpm.ui.delta_screen import SourceSelect
from dpm.workflows import (
    DbContents,
    available_db_versions,
    load_db_contents,
    load_facts,
    load_facts_for_metric,
    load_fact_dimensions,
)

_STAT_LABELS = (
    ("perimeters", "perimeters"),
    ("templates", "templates"),
    ("subtemplates", "subtemplates"),
    ("metrics", "metrics"),
    ("facts", "facts"),
    ("dimensions", "dimensions"),
    ("dimension_members", "members"),
)

_FACT_COLUMNS = (
    "Subtemplate", "Row", "Col", "Row label", "Col label", "Metric", "Metric label"
)
_FACT_COL_COLUMNS = ("Subtemplate", "Col", "Col label", "Cells")
_FACT_ROW_COLUMNS = ("Subtemplate", "Row", "Row label", "Cells")
_CONTEXT_COLUMNS = ("Dimension", "Dim label", "Member", "Member label")
_METRIC_COLUMNS = ("Metric", "Label")
_USAGE_COLUMNS = ("Subtemplate", "Row", "Col", "Row label", "Col label")
_DIM_COLUMNS = ("Dimension", "Label", "Members")
_MEMBER_COLUMNS = ("Member", "Label")


def _cells(row: tuple) -> tuple[str, ...]:
    return tuple(str(c) if c is not None else "" for c in row)


class ExploreScreen(CopyScreen):
    CSS_PATH = "explore_screen.tcss"

    _versions: list[str] = []
    # Full metric catalogue kept in memory so the search box can filter locally.
    _metrics: list[tuple[str, str]] = []
    _dimensions: list[tuple[str, str, list]] = []
    # (subtemplate_code, row_code, column_code) of the fact selected in the grid.
    _current_subtemplate: str | None = None
    # Facts for the current tree selection, kept so the view toggle can re-render.
    _facts: list[tuple] = []
    # Facts grouping: "cell" (per cell), "col" (per column), "row" (per row).
    _fact_view: str = "cell"

    def compose(self) -> ComposeResult:
        yield Horizontal(
            SourceSelect([], id="sel-db", prompt="Pick a DB version…"),
            Button("Open", id="btn-open", variant="primary"),
            Static("Pick a version and press Open.", id="overview"),
            id="topbar",
        )
        with TabbedContent(id="tabs"):
            with TabPane("Structure", id="tab-structure"):
                yield Horizontal(
                    Tree("Database", id="db-tree"),
                    Vertical(
                        Label("Facts"),
                        Select(
                            [("By cell", "cell"), ("By column", "col"), ("By row", "row")],
                            value="cell",
                            allow_blank=False,
                            id="fact-view",
                        ),
                        DataTable(id="facts-table"),
                        Label("Selected fact — dimensions"),
                        DataTable(id="fact-context"),
                        id="struct-detail",
                    ),
                    id="structure-row",
                )
            with TabPane("Metrics", id="tab-metrics"):
                yield Horizontal(
                    Vertical(
                        Input(id="inp-metric-search", placeholder="Filter metrics…"),
                        DataTable(id="metrics-table"),
                        id="metrics-left",
                    ),
                    Vertical(
                        Label("Used in facts"),
                        DataTable(id="metric-usage"),
                        id="metrics-right",
                    ),
                    id="metrics-row",
                )
            with TabPane("Dimensions", id="tab-dimensions"):
                yield Horizontal(
                    Vertical(
                        Input(
                            id="inp-dimension-search",
                            placeholder="Filter dimensions…",
                        ),
                        DataTable(id="dims-table"),
                        id="dims-left",
                    ),
                    Vertical(
                        Label("Members"),
                        DataTable(id="members-table"),
                        id="dims-right",
                    ),
                    id="dimensions-row",
                )
        yield Horizontal(
            Button("← Back", id="btn-back"),
            id="bottombar",
        )
        yield Footer()

    def on_mount(self) -> None:
        self._configure_fact_columns()
        self.query_one("#fact-context", DataTable).add_columns(*_CONTEXT_COLUMNS)
        self.query_one("#metrics-table", DataTable).add_columns(*_METRIC_COLUMNS)
        self.query_one("#metric-usage", DataTable).add_columns(*_USAGE_COLUMNS)
        self.query_one("#dims-table", DataTable).add_columns(*_DIM_COLUMNS)
        self.query_one("#members-table", DataTable).add_columns(*_MEMBER_COLUMNS)
        for tid in ("metrics-table", "dims-table"):
            self.query_one(f"#{tid}", DataTable).cursor_type = "row"
        self.query_one("#facts-table", DataTable).cursor_type = "row"
        self._refresh_versions()

    # ── DB version dropdown ─────────────────────────────────────────────────

    def _db_dir(self) -> Path:
        return VERSIONS_DIR

    def _select(self) -> SourceSelect:
        return self.query_one("#sel-db", SourceSelect)

    def _selected_version(self) -> str | None:
        value = self._select().value
        return value if value in self._versions else None

    def _refresh_versions(self) -> None:
        self._versions = available_db_versions(self._db_dir())
        sel = self._select()
        options = [("", SourceSelect.NONE)] + [(f"DB {v}", v) for v in self._versions]
        keep = sel.value if sel.value in self._versions else SourceSelect.NONE
        sel.set_options(options)
        sel.value = keep

    def _db_path(self) -> Path | None:
        version = self._selected_version()
        return self._db_dir() / f"{version}.duckdb" if version else None

    # ── open database ───────────────────────────────────────────────────────

    def on_select_changed(self, event: Select.Changed) -> None:
        # Picking a real version moves focus straight to Open for quick keyboard flow.
        if event.select.id == "sel-db" and self._selected_version() is not None:
            self.query_one("#btn-open", Button).focus()
        elif event.select.id == "fact-view":
            self._fact_view = str(event.value)
            self.query_one("#fact-context", DataTable).clear()
            self._render_facts()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "btn-back":
            self.app.action_go_back()
        elif event.button.id == "btn-open":
            self._start_open()

    def _start_open(self) -> None:
        db_path = self._db_path()
        if db_path is None:
            self.query_one("#overview", Static).update(
                "[red]Pick an ingested DB version first.[/red]"
            )
            return

        self.query_one("#btn-open", Button).disabled = True
        self.query_one("#overview", Static).update("Loading…")

        def worker() -> None:
            try:
                contents = load_db_contents(db_path)
                if self.is_mounted:
                    self.app.call_from_thread(self._on_open_success, contents)
            except Exception as exc:
                if self.is_mounted:
                    self.app.call_from_thread(self._on_error, exc)

        self.run_worker(worker, thread=True, name="explore-open")

    def _on_open_success(self, contents: DbContents) -> None:
        if not self.is_mounted:
            return
        self.query_one("#btn-open", Button).disabled = False
        summary = "   ".join(
            f"{contents.stats.get(key, 0)} {label}" for key, label in _STAT_LABELS
        )
        self.query_one("#overview", Static).update(summary)

        self._build_tree(contents.tree)
        self._metrics = contents.metrics
        self._populate_metrics(self._metrics)
        self._dimensions = contents.dimensions
        self._populate_dimensions(self._dimensions)

        self._current_subtemplate = None
        self._facts = []
        for tid in ("facts-table", "fact-context", "metric-usage", "members-table"):
            self.query_one(f"#{tid}", DataTable).clear()

    def _on_error(self, exc: Exception) -> None:
        if not self.is_mounted:
            return
        self.query_one("#btn-open", Button).disabled = False
        self.query_one("#overview", Static).update(f"[red]Error: {exc}[/red]")
        self.notify(str(exc), severity="error", title="Explore failed")

    # ── Structure tab ───────────────────────────────────────────────────────

    def _build_tree(self, tree: list) -> None:
        widget = self.query_one("#db-tree", Tree)
        widget.clear()
        widget.root.expand()
        for perim, templates in tree:
            p_node = widget.root.add(
                f"[b]{perim}[/b]", expand=False, data=("perimeter", perim)
            )
            for t_code, t_label, subs in templates:
                label = f"{t_code}  [dim]{t_label}[/dim]" if t_label else t_code
                t_node = p_node.add(label, expand=False, data=("template", t_code))
                for s_code, s_label, _s_type in subs:
                    leaf = f"{s_code}  [dim]{s_label}[/dim]" if s_label else s_code
                    t_node.add_leaf(leaf, data=("subtemplate", s_code))

    def on_tree_node_selected(self, event: Tree.NodeSelected) -> None:
        node: TreeNode = event.node
        data = node.data
        if not isinstance(data, tuple):
            return
        kind, code = data
        db_path = self._db_path()
        if db_path is None:
            return
        # Aggregated views carry each row's subtemplate in the key instead.
        self._current_subtemplate = code if kind == "subtemplate" else None
        self.query_one("#fact-context", DataTable).clear()

        def worker() -> None:
            try:
                facts = load_facts(db_path, kind, code)
                if self.is_mounted:
                    self.app.call_from_thread(self._populate_facts, facts)
            except Exception as exc:
                if self.is_mounted:
                    self.app.call_from_thread(self._on_error, exc)

        self.run_worker(worker, thread=True, name="explore-facts")

    def _configure_fact_columns(self) -> None:
        """(Re)build the facts table columns to match the current grouping view."""
        table = self.query_one("#facts-table", DataTable)
        table.clear(columns=True)
        cols = {
            "col": _FACT_COL_COLUMNS,
            "row": _FACT_ROW_COLUMNS,
        }.get(self._fact_view, _FACT_COLUMNS)
        table.add_columns(*cols)

    def _populate_facts(self, facts: list[tuple]) -> None:
        if not self.is_mounted:
            return
        self._facts = facts
        self._render_facts()

    def _render_facts(self) -> None:
        if not self.is_mounted:
            return
        self._configure_fact_columns()
        table = self.query_one("#facts-table", DataTable)
        table.clear()
        if self._fact_view == "cell":
            for row in self._facts:
                # row = (subtemplate, row_code, column_code, row_label, column_label,
                #        metric, label)
                table.add_row(*_cells(row), key=f"{row[0]}|{row[1]}|{row[2]}")
            return
        # Aggregate: one line per (subtemplate, row|column) with a cell count.
        code_idx, label_idx = (2, 4) if self._fact_view == "col" else (1, 3)
        groups: dict[tuple[str, str], list] = {}
        for row in self._facts:
            key = (row[0], row[code_idx])
            entry = groups.get(key)
            if entry is None:
                groups[key] = [row[label_idx], 1]
            else:
                entry[1] += 1
        for (sub, code), (label, count) in groups.items():
            table.add_row(
                str(sub),
                str(code) if code is not None else "",
                str(label) if label is not None else "",
                str(count),
            )

    def _load_fact_context(
        self, subtemplate: str, row_code: str, column_code: str
    ) -> None:
        db_path = self._db_path()
        if db_path is None or not subtemplate:
            return

        def worker() -> None:
            try:
                ctx = load_fact_dimensions(db_path, subtemplate, row_code, column_code)
                if self.is_mounted:
                    self.app.call_from_thread(self._populate_fact_context, ctx)
            except Exception as exc:
                if self.is_mounted:
                    self.app.call_from_thread(self._on_error, exc)

        self.run_worker(worker, thread=True, name="explore-fact-context")

    def _populate_fact_context(self, ctx: list[tuple]) -> None:
        if not self.is_mounted:
            return
        table = self.query_one("#fact-context", DataTable)
        table.clear()
        for row in ctx:
            table.add_row(*_cells(row))

    # ── Metrics tab ─────────────────────────────────────────────────────────

    def _populate_metrics(self, metrics: list[tuple[str, str]]) -> None:
        table = self.query_one("#metrics-table", DataTable)
        table.clear()
        for code, label in metrics:
            table.add_row(code, str(label) if label is not None else "", key=code)

    def _load_metric_usage(self, metric_code: str) -> None:
        db_path = self._db_path()
        if db_path is None:
            return

        def worker() -> None:
            try:
                usage = load_facts_for_metric(db_path, metric_code)
                if self.is_mounted:
                    self.app.call_from_thread(self._populate_metric_usage, usage)
            except Exception as exc:
                if self.is_mounted:
                    self.app.call_from_thread(self._on_error, exc)

        self.run_worker(worker, thread=True, name="explore-metric-usage")

    def _populate_metric_usage(self, usage: list[tuple]) -> None:
        if not self.is_mounted:
            return
        table = self.query_one("#metric-usage", DataTable)
        table.clear()
        for row in usage:
            table.add_row(*_cells(row))

    # ── Dimensions tab ──────────────────────────────────────────────────────

    def _populate_dimensions(self, dimensions: list[tuple[str, str, list]]) -> None:
        table = self.query_one("#dims-table", DataTable)
        table.clear()
        for code, label, members in dimensions:
            table.add_row(
                code,
                str(label) if label is not None else "",
                str(len(members)),
                key=code,
            )

    def _populate_members(self, dimension_code: str) -> None:
        members: list[tuple[str, str]] = []
        for code, _label, mems in self._dimensions:
            if code == dimension_code:
                members = mems
                break
        table = self.query_one("#members-table", DataTable)
        table.clear()
        for m_code, m_label in members:
            table.add_row(m_code, str(m_label) if m_label is not None else "")

    # ── shared events ───────────────────────────────────────────────────────

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id == "inp-metric-search":
            term = event.value.strip().lower()
            filtered = (
                self._metrics
                if not term
                else [
                    m
                    for m in self._metrics
                    if term in m[0].lower() or term in str(m[1]).lower()
                ]
            )
            self._populate_metrics(filtered)
        elif event.input.id == "inp-dimension-search":
            term = event.value.strip().lower()
            filtered = (
                self._dimensions
                if not term
                else [
                    d
                    for d in self._dimensions
                    if term in d[0].lower() or term in str(d[1]).lower()
                ]
            )
            self._populate_dimensions(filtered)

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        table_id = event.data_table.id
        key = event.row_key.value
        if key is None:
            return
        if table_id == "facts-table":
            if self._fact_view != "cell":
                self.query_one("#fact-context", DataTable).clear()
                return
            subtemplate, row_code, column_code = key.split("|")
            self._load_fact_context(subtemplate, row_code, column_code)
        elif table_id == "metrics-table":
            self._load_metric_usage(key)
        elif table_id == "dims-table":
            self._populate_members(key)
