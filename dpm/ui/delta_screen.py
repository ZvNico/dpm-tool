from __future__ import annotations

from pathlib import Path

from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.screen import Screen
from textual.widgets import (
    Button,
    Footer,
    Input,
    Label,
    ProgressBar,
    RichLog,
    Select,
    Static,
)
from textual.widgets._select import SelectCurrent

from dpm._constants import DELTA_DIR, VERSIONS_DIR
from dpm.ui.log_handler import RichLogHandler, attach, detach
from dpm.workflows import (
    available_db_versions,
    ensure_delta_db,
    render_delta_xlsx,
    version_key,
)


class SourceSelect(Select):
    """Select of ingested DB versions with a greyed placeholder on the collapsed
    control while nothing is chosen.

    The resting value is the ``NONE`` sentinel (a real, blank-labelled option, so
    Textual's option/index bookkeeping stays intact); the collapsed control shows
    the ``prompt`` placeholder instead of that blank row, and the placeholder text
    itself never appears in the dropdown.
    """

    NONE = "__none__"

    def __init__(self, versions, *, prompt: str, id: str | None = None) -> None:
        super().__init__(
            [("", self.NONE), *versions], prompt=prompt, allow_blank=False, id=id
        )

    def _show_placeholder(self) -> None:
        if self.value == self.NONE:
            self.query_one(SelectCurrent).has_value = False

    def _watch_value(self, value) -> None:
        super()._watch_value(value)
        self._show_placeholder()

    def _watch_expanded(self, expanded: bool) -> None:
        super()._watch_expanded(expanded)
        if not expanded:
            self._show_placeholder()


class DeltaScreen(Screen):
    CSS_PATH = "delta_screen.tcss"

    _versions: list[str] = []

    def compose(self) -> ComposeResult:
        yield Vertical(
            Static("DPM Delta", id="screen-title"),
            Label("Old version (ingested DB)"),
            SourceSelect([], id="sel-old", prompt="Pick a DB version…"),
            Label("New version (ingested DB)"),
            SourceSelect([], id="sel-new", prompt="Pick a DB version…"),
            Label("Output workbook"),
            Input(id="inp-output", placeholder="Delta_DPM.xlsx", value="Delta_DPM.xlsx"),
            Horizontal(
                Button("← Back", id="btn-back"),
                Button("Run Delta", id="btn-run", variant="primary"),
                id="btn-row",
            ),
            Vertical(
                Horizontal(
                    Static("", id="step-label"),
                    Static("", id="step-detail"),
                    id="step-row",
                ),
                ProgressBar(id="progress-bar", total=None, show_eta=False),
                id="progress",
            ),
            id="form-panel",
        )
        yield RichLog(id="log", highlight=True, markup=True)
        yield Footer()

    # ── DB version dropdowns ────────────────────────────────────────────────

    def on_mount(self) -> None:
        self._versions = available_db_versions(self._db_dir())
        self._rebuild("old")
        self._rebuild("new")

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

    def _db_path(self, side: str) -> Path | None:
        version = self._selected_version(side)
        return self._db_dir() / f"{version}.duckdb" if version else None

    def on_select_changed(self, event: Select.Changed) -> None:
        if event.select.id == "sel-old":
            self._rebuild("new")
        if self._selected_version("old") and self._selected_version("new"):
            self.query_one("#btn-run", Button).focus()

    # ── run ─────────────────────────────────────────────────────────────────

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "btn-back":
            self.app.action_go_back()
        elif event.button.id == "btn-run":
            self._start_run_delta()

    def _set_step(self, label: str, detail: str = "") -> None:
        self.query_one("#step-label", Static).update(label)
        self.query_one("#step-detail", Static).update(detail)

    def _start_run_delta(self) -> None:
        log = self.query_one("#log", RichLog)
        log.clear()

        old_db = self._db_path("old")
        new_db = self._db_path("new")
        if old_db is None or new_db is None:
            log.write("[red]Error: pick an ingested DB version for both old and new[/red]")
            return
        delta_dir = self._delta_dir()
        output_path = Path(
            self.query_one("#inp-output", Input).value.strip() or "Delta_DPM.xlsx"
        )

        self.query_one("#btn-run", Button).disabled = True
        handler = RichLogHandler(log)
        attach(handler)

        self._set_step("Starting…", "")
        self.query_one("#progress").display = True
        self.query_one("#progress-bar", ProgressBar).update(total=None)

        cft = self.app.call_from_thread

        def on_step(label: str) -> None:
            cft(self._set_step, label, "")

        def on_sheet(idx: int, total: int, name: str) -> None:
            cft(self._on_sheet, idx, total, name)

        def worker() -> None:
            try:
                delta_path = ensure_delta_db(old_db, new_db, delta_dir, on_step=on_step)
                render_delta_xlsx(delta_path, output_path, on_sheet=on_sheet)
                if self.is_mounted:
                    cft(self._on_success, delta_path, output_path)
            except Exception as exc:
                if self.is_mounted:
                    cft(self._on_error, exc)
            finally:
                detach(handler)

        self.run_worker(worker, thread=True, name="run-delta")

    def _on_sheet(self, idx: int, total: int, name: str) -> None:
        bar = self.query_one("#progress-bar", ProgressBar)
        bar.update(total=total, progress=idx)
        self._set_step(f"Writing sheet {idx}/{total}", name)

    def _on_success(self, delta_path: Path, output_path: Path) -> None:
        if not self.is_mounted:
            return
        self.query_one("#btn-run", Button).disabled = False
        self._set_step("Done!", "")
        self.query_one("#log", RichLog).write(
            f"[green bold]Done![/green bold] Delta DB [bold]{delta_path}[/bold]\n"
            f"Workbook written to [bold]{output_path}[/bold]"
        )
        self.notify(
            f"Saved {output_path}", severity="information", title="Delta complete"
        )

    def _on_error(self, exc: Exception) -> None:
        if not self.is_mounted:
            return
        self.query_one("#btn-run", Button).disabled = False
        self._set_step("Failed", "")
        self.query_one("#log", RichLog).write(f"[red bold]Error:[/red bold] {exc}")
        self.notify(str(exc), severity="error", title="Delta failed")
