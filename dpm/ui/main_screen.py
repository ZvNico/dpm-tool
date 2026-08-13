from __future__ import annotations

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import Screen
from textual.widgets import Button, Footer, Label

# Each action: button id, icon, title, one-line description.
_ACTIONS = (
    ("btn-ingest", "⇩", "DPM Ingest", "Parse an EIOPA workbook into a versioned database"),
    ("btn-delta", "Δ", "DPM Delta", "Compare two DPM versions and review the changes"),
    ("btn-apply", "⇄", "XBRL Apply Delta", "Roll a delta forward onto your XBRL instances"),
    ("btn-explore", "⌕", "Explore Database", "Browse an ingested DPM database"),
    ("btn-explore-delta", "≠", "Explore Delta", "Browse the changes between two versions"),
)


class MainScreen(Screen):
    CSS_PATH = "main_screen.tcss"

    # The custom arrow-nav surfaces in the footer (it's not the default Tab
    # focus movement); the no-op home Back stays hidden.
    BINDINGS = [
        Binding("up", "nav(-1)", "Up"),
        Binding("down", "nav(1)", "Down"),
        Binding("s", "settings", "Settings"),
        Binding("escape", "go_back", "Back", show=False),
    ]

    def action_nav(self, direction: int) -> None:
        if direction < 0:
            self.focus_previous()
        else:
            self.focus_next()

    def action_settings(self) -> None:
        from dpm.ui.settings_screen import SettingsScreen

        self.app.push_screen(SettingsScreen())

    def compose(self) -> ComposeResult:
        with Vertical(id="home"):
            with Vertical(id="hero"):
                yield Label("DPM TOOL", id="brand")
                yield Label("EIOPA DPM Toolkit", id="tagline")
            with Vertical(id="menu"):
                for btn_id, icon, title, desc in _ACTIONS:
                    yield Button(
                        f"[b]{icon}  {title}[/b]\n   [dim]{desc}[/dim]",
                        id=btn_id,
                        classes="action",
                    )
        yield Footer()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        from dpm.ui.ingest_screen import IngestScreen
        from dpm.ui.delta_screen import DeltaScreen
        from dpm.ui.apply_screen import ApplyScreen
        from dpm.ui.explore_screen import ExploreScreen
        from dpm.ui.explore_delta_screen import ExploreDeltaScreen

        if event.button.id == "btn-ingest":
            self.app.push_screen(IngestScreen())
        elif event.button.id == "btn-delta":
            self.app.push_screen(DeltaScreen())
        elif event.button.id == "btn-apply":
            self.app.push_screen(ApplyScreen())
        elif event.button.id == "btn-explore":
            self.app.push_screen(ExploreScreen())
        elif event.button.id == "btn-explore-delta":
            self.app.push_screen(ExploreDeltaScreen())
