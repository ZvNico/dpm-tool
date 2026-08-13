from __future__ import annotations

from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.screen import Screen
from textual.widgets import Button, DataTable, Footer, Input, Label

from dpm.config import VersionEntry, add_version, load_versions, remove_version


class SettingsScreen(Screen):
    CSS_PATH = "settings_screen.tcss"

    BINDINGS = [
        ("delete", "remove", "Remove"),
    ]

    # Version under the table cursor (target of the delete shortcut).
    _highlighted: str | None = None

    def compose(self) -> ComposeResult:
        with Vertical(id="card"):
            with Vertical(id="hero"):
                yield Label("⚙  Settings", id="brand")
                yield Label("DPM versions you track", id="tagline")
            with Horizontal(id="add-row"):
                yield Input(id="inp-version", placeholder="version — e.g. 2.10.0 or 2.8.2_hotfix")
                yield Input(id="inp-url", placeholder="explicit URL (optional, for hotfixes)")
                yield Button("Add", id="btn-add", variant="primary")
            yield DataTable(id="versions-table")
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one("#versions-table", DataTable)
        table.add_columns("Version", "Source")
        table.cursor_type = "row"
        table.zebra_stripes = True
        self._reload()
        self.query_one("#inp-version", Input).focus()

    # ── data ────────────────────────────────────────────────────────────────

    def _reload(self, entries: list[VersionEntry] | None = None) -> None:
        entries = entries if entries is not None else load_versions()
        table = self.query_one("#versions-table", DataTable)
        table.clear()
        for e in entries:
            source = e.url or "[dim]auto-download[/dim]"
            table.add_row(e.version, source, key=e.version)
        self._highlighted = entries[0].version if entries else None

    # ── events ──────────────────────────────────────────────────────────────

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        self._highlighted = event.row_key.value

    def on_input_submitted(self, event: Input.Submitted) -> None:
        # Enter in either field adds the version.
        self._add()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "btn-add":
            self._add()

    def action_remove(self) -> None:
        if self._highlighted is None:
            self.notify("Nothing to remove.", severity="warning")
            return
        version = self._highlighted
        self._reload(remove_version(version))
        self.notify(f"Removed {version}", severity="information")

    def _add(self) -> None:
        version = self.query_one("#inp-version", Input).value.strip()
        url = self.query_one("#inp-url", Input).value.strip()
        if not version:
            self.notify("Enter a version.", severity="warning")
            self.query_one("#inp-version", Input).focus()
            return
        self._reload(add_version(version, url or None))
        self.query_one("#inp-version", Input).value = ""
        self.query_one("#inp-url", Input).value = ""
        self.query_one("#inp-version", Input).focus()
        self.notify(f"Added {version}", severity="information")
