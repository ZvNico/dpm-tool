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

import polars as pl
from rich.text import Text

from dpm._constants import DELTA_DIR, VERSIONS_DIR
from dpm.delta_db import (
    load_cell_changes,
    load_delta_counts,
    load_delta_tree,
    load_member_changes,
    load_metric_changes,
)
from dpm.ui.delta_screen import SourceSelect
from dpm.workflows import available_db_versions, ensure_delta_db, version_key

# Merged display headers (Old/New collapsed into one column; Status → row color).
_CELL_COLS = ("Row", "Col", "QName", "Type")
_CELLDIM_COLS = ("Dimension", "Member")
_METRIC_COLS = ("MetricCode", "MetricLabel")
# Dimensions tab is master-detail (like the DB explorer): a dimensions list on the
# left, member changes for the selected dimension on the right.
_DIM_COLS = ("Dimension", "Label", "Changes")
_MEMBER_COLS = ("Member", "MemberLabel")

# Status → row text color.
_STATUS_STYLE = {"Added": "green", "Modified": "dark_orange", "Deleted": "red"}

# Status → single-char badge used on tree labels, e.g. "[+2 ~3 -1]".
_BADGE = {"Added": "+", "Modified": "~", "Deleted": "-"}


def _merge(old, new) -> str:
    """Collapse an old/new value pair into one cell (``old → new`` when both differ)."""
    old, new = str(old or ""), str(new or "")
    if old and new and old != new:
        return f"{old} → {new}"
    return new or old


def _member_status(old, new) -> str:
    """Infer a status from which side of a value pair is populated."""
    if old and new:
        return "Modified"
    if new:
        return "Added"
    if old:
        return "Deleted"
    return ""


def _styled_row(row: dict, specs, status: str) -> list[Text]:
    """Build one colored table row; each spec is a column name or an (old, new) merge."""
    style = _STATUS_STYLE.get(status, "")
    out = []
    for spec in specs:
        val = (
            _merge(row[spec[0]], row[spec[1]])
            if isinstance(spec, tuple)
            else str(row.get(spec) or "")
        )
        out.append(Text(val, style=style))
    return out


def _counts_str(counts: dict[str, int]) -> str:
    order = ["Added", "Deleted", "Modified", "Kept"]
    parts = [f"{s} {counts[s]}" for s in order if counts.get(s)]
    return "  ".join(parts) if parts else "no changes"


def _badge(counts: dict[str, int]) -> str:
    parts = [f"{sym}{counts[s]}" for s, sym in _BADGE.items() if counts.get(s)]
    return f"[dim]\\[{' '.join(parts)}][/dim]" if parts else ""


def _colored_badge(counts: dict[str, int]) -> Text:
    """Per-status Changes cell: e.g. green '+2', orange '~1', red '-1'."""
    out = Text()
    for status, sym in _BADGE.items():
        n = counts.get(status, 0)
        if not n:
            continue
        if out:
            out.append(" ")
        out.append(f"{sym}{n}", style=_STATUS_STYLE[status])
    return out


def _parse_dims(serialized: str) -> dict[str, str]:
    """Turn ``d1=m1;d2=m2`` into ``{d1: m1, d2: m2}``."""
    out: dict[str, str] = {}
    for token in (serialized or "").split(";"):
        if "=" in token:
            dim, _, member = token.partition("=")
            out[dim] = member
    return out


class ExploreDeltaScreen(Screen):
    CSS_PATH = "explore_delta_screen.tcss"

    _versions: list[str] = []
    _metrics: pl.DataFrame = pl.DataFrame()
    _members: pl.DataFrame = pl.DataFrame()
    # Grouped (code, label, per-status counts) rows for the dimensions list.
    _dimensions_list: list[tuple[str, str, dict[str, int]]] = []
    _cells: pl.DataFrame = pl.DataFrame()
    _delta_path: Path | None = None

    def compose(self) -> ComposeResult:
        yield Horizontal(
            SourceSelect([], id="sel-old", prompt="Old version…"),
            SourceSelect([], id="sel-new", prompt="New version…"),
            Button("Open", id="btn-open", variant="primary"),
            id="topbar",
        )
        yield Static("Pick two versions and press Open.", id="overview")
        with TabbedContent(id="tabs"):
            with TabPane("Structure", id="tab-structure"):
                yield Horizontal(
                    Tree("Changes", id="delta-tree"),
                    Vertical(
                        Label("Changed cells"),
                        DataTable(id="cells-table"),
                        Label("Selected cell — dimensions (old → new)"),
                        DataTable(id="cell-dims"),
                        id="cells-detail",
                    ),
                    id="structure-row",
                )
            with TabPane("Metrics", id="tab-metrics"):
                yield Vertical(
                    Input(id="inp-metric-search", placeholder="Filter metrics…"),
                    DataTable(id="metrics-table"),
                    id="metrics-col",
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
                        Label("Member changes"),
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
        self.query_one("#cells-table", DataTable).add_columns(*_CELL_COLS)
        self.query_one("#cells-table", DataTable).cursor_type = "row"
        self.query_one("#cell-dims", DataTable).add_columns(*_CELLDIM_COLS)
        self.query_one("#metrics-table", DataTable).add_columns(*_METRIC_COLS)
        self.query_one("#dims-table", DataTable).add_columns(*_DIM_COLS)
        self.query_one("#dims-table", DataTable).cursor_type = "row"
        self.query_one("#members-table", DataTable).add_columns(*_MEMBER_COLS)
        self._refresh_versions()

    # ── version dropdowns ───────────────────────────────────────────────────

    def _db_dir(self) -> Path:
        return VERSIONS_DIR

    def _delta_dir(self) -> Path:
        return DELTA_DIR

    def _select(self, side: str) -> SourceSelect:
        return self.query_one(f"#sel-{side}", SourceSelect)

    def _selected_version(self, side: str) -> str | None:
        value = self._select(side).value
        return value if value in self._versions else None

    def _options_for(self, side: str) -> list[tuple[str, str]]:
        versions = self._versions
        if side == "new":
            old_v = self._selected_version("old")
            if old_v is not None:
                versions = [v for v in versions if version_key(v) > version_key(old_v)]
        return [("", SourceSelect.NONE)] + [(f"DB {v}", v) for v in versions]

    def _rebuild(self, side: str) -> None:
        sel = self._select(side)
        options = self._options_for(side)
        valid = {value for _, value in options}
        keep = sel.value if sel.value in valid else SourceSelect.NONE
        sel.set_options(options)
        sel.value = keep

    def _refresh_versions(self) -> None:
        self._versions = available_db_versions(self._db_dir())
        self._rebuild("old")
        self._rebuild("new")

    def _db_path(self, side: str) -> Path | None:
        version = self._selected_version(side)
        return self._db_dir() / f"{version}.duckdb" if version else None

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id == "inp-metric-search":
            self._filter_metrics(event.value.strip().lower())
        elif event.input.id == "inp-dimension-search":
            self._filter_dimensions(event.value.strip().lower())

    # ── open / ensure delta DB ──────────────────────────────────────────────

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "btn-back":
            self.app.action_go_back()
        elif event.button.id == "btn-open":
            self._start_open()

    def on_select_changed(self, event: Select.Changed) -> None:
        if event.select.id == "sel-old":
            self._rebuild("new")

    def _start_open(self) -> None:
        old_db = self._db_path("old")
        new_db = self._db_path("new")
        if old_db is None or new_db is None:
            self.query_one("#overview", Static).update(
                "[red]Pick an ingested DB version for both old and new.[/red]"
            )
            return
        delta_dir = self._delta_dir()

        self.query_one("#btn-open", Button).disabled = True
        self.query_one("#overview", Static).update("Preparing delta…")

        cft = self.app.call_from_thread

        def worker() -> None:
            try:
                delta_path = ensure_delta_db(
                    old_db,
                    new_db,
                    delta_dir,
                    on_step=lambda label: cft(
                        self.query_one("#overview", Static).update, label
                    ),
                )
                counts = load_delta_counts(delta_path)
                tree = load_delta_tree(delta_path)
                metrics = load_metric_changes(delta_path)
                members = load_member_changes(delta_path)
                if self.is_mounted:
                    cft(
                        self._on_open_success,
                        delta_path,
                        counts,
                        tree,
                        metrics,
                        members,
                    )
            except Exception as exc:
                if self.is_mounted:
                    cft(self._on_error, exc)

        self.run_worker(worker, thread=True, name="ensure-delta")

    def _on_open_success(
        self,
        delta_path: Path,
        counts: dict[str, dict[str, int]],
        tree: list,
        metrics: pl.DataFrame,
        members: pl.DataFrame,
    ) -> None:
        if not self.is_mounted:
            return
        self._delta_path = delta_path
        self.query_one("#btn-open", Button).disabled = False
        self.query_one("#overview", Static).update(
            f"[b]Structure[/b] {_counts_str(counts['structure'])}    "
            f"[b]Metrics[/b] {_counts_str(counts['metrics'])}    "
            f"[b]Dimensions[/b] {_counts_str(counts['dimensions'])}"
        )

        self._build_tree(tree)
        self._metrics = metrics
        self._filter_metrics("")
        self._members = members
        self._dimensions_list = self._dim_rows()
        self._populate_dimensions(self._dimensions_list)
        self.query_one("#members-table", DataTable).clear()
        self.query_one("#cells-table", DataTable).clear()
        self.query_one("#cell-dims", DataTable).clear()

    def _on_error(self, exc: Exception) -> None:
        if not self.is_mounted:
            return
        self.query_one("#btn-open", Button).disabled = False
        self.query_one("#overview", Static).update(f"[red]Error: {exc}[/red]")
        self.notify(str(exc), severity="error", title="Delta failed")

    # ── Structure tree ──────────────────────────────────────────────────────

    def _build_tree(self, tree: list) -> None:
        widget = self.query_one("#delta-tree", Tree)
        widget.clear()
        widget.root.expand()
        for perim, templates in tree:
            p_node = widget.root.add(f"[b]{perim}[/b]", expand=False)
            for t_code, subs in templates:
                t_node = p_node.add(t_code, expand=False)
                for s_code, counts in subs:
                    badge = _badge(counts)
                    label = f"{s_code}  {badge}" if badge else s_code
                    # Leaf carries (perimeter, subtemplate_code) for the detail query.
                    t_node.add_leaf(label, data=(perim, s_code))

    def on_tree_node_selected(self, event: Tree.NodeSelected) -> None:
        node: TreeNode = event.node
        data = node.data
        if not isinstance(data, tuple) or self._delta_path is None:
            return
        perimeter, subtemplate_code = data
        path = self._delta_path
        self.query_one("#cell-dims", DataTable).clear()

        def worker() -> None:
            try:
                df = load_cell_changes(path, perimeter, subtemplate_code)
                if self.is_mounted:
                    self.app.call_from_thread(self._populate_cells, df)
            except Exception as exc:
                if self.is_mounted:
                    self.app.call_from_thread(self._on_error, exc)

        self.run_worker(worker, thread=True, name="load-cells")

    def _populate_cells(self, df: pl.DataFrame) -> None:
        if not self.is_mounted:
            return
        self._cells = df
        table = self.query_one("#cells-table", DataTable)
        table.clear()
        if df.height:
            for row in df.iter_rows(named=True):
                cells = _styled_row(
                    row,
                    ["row_code", "column_code", ("qname_old", "qname_new"), "type"],
                    row.get("status", ""),
                )
                table.add_row(*cells, key=f"{row['row_code']}|{row['column_code']}")

    def _show_cell_dims(self, row_code: str, column_code: str) -> None:
        table = self.query_one("#cell-dims", DataTable)
        table.clear()
        if not self._cells.height:
            return
        match = self._cells.filter(
            (pl.col("row_code") == row_code) & (pl.col("column_code") == column_code)
        )
        if not match.height:
            return
        row = match.to_dicts()[0]
        old = _parse_dims(row.get("dimensions_old", ""))
        new = _parse_dims(row.get("dimensions_new", ""))
        for dim in sorted(set(old) | set(new)):
            o, n = old.get(dim, ""), new.get(dim, "")
            style = _STATUS_STYLE.get(_member_status(o, n), "")
            table.add_row(Text(dim, style=style), Text(_merge(o, n), style=style))

    # ── Metrics / Dimensions tabs ───────────────────────────────────────────

    def _fill_table(self, table_id: str, df: pl.DataFrame, specs, status_col: str) -> None:
        table = self.query_one(table_id, DataTable)
        table.clear()
        if df.height:
            for row in df.iter_rows(named=True):
                table.add_row(*_styled_row(row, specs, row.get(status_col, "")))

    def _filter_metrics(self, term: str) -> None:
        df = self._metrics
        if term and df.height:
            df = df.filter(
                pl.col("metric_code").str.to_lowercase().str.contains(term, literal=True)
                | pl.col("metric_label_new")
                .str.to_lowercase()
                .str.contains(term, literal=True)
                | pl.col("metric_label_old")
                .str.to_lowercase()
                .str.contains(term, literal=True)
            )
        self._fill_table(
            "#metrics-table",
            df,
            ["metric_code", ("metric_label_old", "metric_label_new")],
            "status",
        )

    def _dim_rows(self) -> list[tuple[str, str, dict[str, int]]]:
        """Group member changes into one row per dimension (code, label, counts).

        ``counts`` maps each status to its member count within the dimension, so the
        Changes cell can render a per-status colored breakdown (e.g. ``+2 ~1 -1``).
        """
        df = self._members
        if not df.height:
            return []
        grouped = (
            df.group_by("dimension_code", "status")
            .agg(
                pl.col("dimension_label").first().alias("label"),
                pl.len().alias("n"),
            )
            .sort("dimension_code")
        )
        rows: dict[str, tuple[str, dict[str, int]]] = {}
        for r in grouped.iter_rows(named=True):
            code = r["dimension_code"]
            label, counts = rows.setdefault(code, (r["label"] or "", {}))
            counts[r["status"]] = r["n"]
        return [(code, label, counts) for code, (label, counts) in rows.items()]

    def _populate_dimensions(self, dims: list[tuple[str, str, dict[str, int]]]) -> None:
        table = self.query_one("#dims-table", DataTable)
        table.clear()
        for code, label, counts in dims:
            table.add_row(
                Text(code),
                Text(label),
                _colored_badge(counts),
                key=code,
            )

    def _filter_dimensions(self, term: str) -> None:
        filtered = (
            self._dimensions_list
            if not term
            else [
                d
                for d in self._dimensions_list
                if term in d[0].lower() or term in str(d[1]).lower()
            ]
        )
        self._populate_dimensions(filtered)

    def _populate_members(self, dimension_code: str) -> None:
        df = self._members.filter(pl.col("dimension_code") == dimension_code)
        table = self.query_one("#members-table", DataTable)
        table.clear()
        for row in df.iter_rows(named=True):
            table.add_row(
                *_styled_row(
                    row,
                    ["member_code", ("member_label_old", "member_label_new")],
                    row.get("status", ""),
                )
            )

    # ── shared events ───────────────────────────────────────────────────────

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        key = event.row_key.value
        if not isinstance(key, str):
            return
        if event.data_table.id == "cells-table":
            row_code, _, column_code = key.partition("|")
            self._show_cell_dims(row_code, column_code)
        elif event.data_table.id == "dims-table":
            self._populate_members(key)
