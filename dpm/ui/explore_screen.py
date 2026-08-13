from __future__ import annotations

from pathlib import Path

from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.screen import Screen
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

_FACT_COLUMNS = ("Row", "Col", "Row label", "Col label", "Metric", "Metric label")
_CONTEXT_COLUMNS = ("Dimension", "Dim label", "Member", "Member label")
_METRIC_COLUMNS = ("Metric", "Label")
_USAGE_COLUMNS = ("Subtemplate", "Row", "Col", "Row label", "Col label")
_DIM_COLUMNS = ("Dimension", "Label", "Members")
_MEMBER_COLUMNS = ("Member", "Label")


def _cells(row: tuple) -> tuple[str, ...]:
    return tuple(str(c) if c is not None else "" for c in row)


class ExploreScreen(Screen):
    CSS_PATH = "explore_screen.tcss"

    _versions: list[str] = []
    # Full metric catalogue kept in memory so the search box can filter locally.
    _metrics: list[tuple[str, str]] = []
    _dimensions: list[tuple[str, str, list]] = []
    # (subtemplate_code, row_code, column_code) of the fact selected in the grid.
    _current_subtemplate: str | None = None

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
        self.query_one("#facts-table", DataTable).add_columns(*_FACT_COLUMNS)
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
            p_node = widget.root.add(f"[b]{perim}[/b]", expand=False)
            for t_code, t_label, subs in templates:
                label = f"{t_code}  [dim]{t_label}[/dim]" if t_label else t_code
                t_node = p_node.add(label, expand=False)
                for s_code, s_label, _s_type in subs:
                    leaf = f"{s_code}  [dim]{s_label}[/dim]" if s_label else s_code
                    t_node.add_leaf(leaf, data=s_code)

    def on_tree_node_selected(self, event: Tree.NodeSelected) -> None:
        node: TreeNode = event.node
        subtemplate_code = node.data
        if not isinstance(subtemplate_code, str):
            return
        db_path = self._db_path()
        if db_path is None:
            return
        self._current_subtemplate = subtemplate_code
        self.query_one("#fact-context", DataTable).clear()

        def worker() -> None:
            try:
                facts = load_facts(db_path, subtemplate_code)
                if self.is_mounted:
                    self.app.call_from_thread(self._populate_facts, facts)
            except Exception as exc:
                if self.is_mounted:
                    self.app.call_from_thread(self._on_error, exc)

        self.run_worker(worker, thread=True, name="explore-facts")

    def _populate_facts(self, facts: list[tuple]) -> None:
        if not self.is_mounted:
            return
        table = self.query_one("#facts-table", DataTable)
        table.clear()
        for row in facts:
            # row = (row_code, column_code, row_label, column_label, metric, label)
            table.add_row(*_cells(row), key=f"{row[0]}|{row[1]}")

    def _load_fact_context(self, row_code: str, column_code: str) -> None:
        db_path = self._db_path()
        if db_path is None or self._current_subtemplate is None:
            return
        subtemplate = self._current_subtemplate

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
            row_code, _, column_code = key.partition("|")
            self._load_fact_context(row_code, column_code)
        elif table_id == "metrics-table":
            self._load_metric_usage(key)
        elif table_id == "dims-table":
            self._populate_members(key)
