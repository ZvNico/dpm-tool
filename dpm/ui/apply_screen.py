from __future__ import annotations

from pathlib import Path

from textual import work
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.screen import Screen
from textual.widgets import Button, Checkbox, Footer, Input, Label, RichLog, Select, Static
from textual_fspicker import Filters

from dpm._constants import VERSIONS_DIR
from dpm.ui._utils import _XBRL_FILTERS, prompt_open_file
from dpm.ui.delta_screen import SourceSelect
from dpm.ui.log_handler import RichLogHandler, attach, detach
from dpm.workflows import available_db_versions, run_apply_delta, version_key


class ApplyScreen(Screen):
    CSS_PATH = "apply_screen.tcss"

    _versions: list[str] = []

    def compose(self) -> ComposeResult:
        yield Vertical(
            Static("XBRL Apply Delta", id="screen-title"),
            Label("Old version (ingested DB)"),
            SourceSelect([], id="sel-old", prompt="Pick a DB version…"),
            Label("New version (ingested DB)"),
            SourceSelect([], id="sel-new", prompt="Pick a DB version…"),
            Label("Input XBRL"),
            Horizontal(
                Input(id="inp-input-xbrl", placeholder="path/to/input.xbrl"),
                Button("Browse", id="btn-browse-xbrl", classes="browse"),
                id="xbrl-row",
            ),
            Label("Output XBRL"),
            Input(id="inp-output-xbrl", placeholder="path/to/output.xbrl"),
            Label("Perimeter override (blank = auto-detect)"),
            Input(id="inp-perimeter", placeholder="e.g. qrs"),
            Checkbox("Dry run (no file written)", id="chk-dry-run"),
            Label("Facts parquet dump (optional)"),
            Input(id="inp-facts-parquet", placeholder="facts.parquet"),
            Horizontal(
                Button("← Back", id="btn-back"),
                Button("Run Apply Delta", id="btn-run", variant="primary"),
                id="btn-row",
            ),
            id="form-panel",
        )
        yield RichLog(id="log", highlight=True, markup=True)
        yield Footer()

    # ── DB version dropdowns ────────────────────────────────────────────────

    def on_mount(self) -> None:
        self._versions = available_db_versions(VERSIONS_DIR)
        self._rebuild("old")
        self._rebuild("new")

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
        return VERSIONS_DIR / f"{version}.duckdb" if version else None

    def on_select_changed(self, event: Select.Changed) -> None:
        if event.select.id == "sel-old":
            self._rebuild("new")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "btn-back":
            self.app.action_go_back()
        elif event.button.id == "btn-browse-xbrl":
            self._browse("#inp-input-xbrl", "Select input XBRL", _XBRL_FILTERS)
        elif event.button.id == "btn-run":
            self._start_apply()

    @work
    async def _browse(self, input_id: str, title: str, filters: Filters) -> None:
        current = self.query_one(input_id, Input).value.strip()
        start = str(Path(current).parent) if current else None
        path = await prompt_open_file(self, title=title, filters=filters, start=start)
        if path is not None:
            self.query_one(input_id, Input).value = str(path)

    def _start_apply(self) -> None:
        log = self.query_one("#log", RichLog)
        log.clear()

        old_db = self._db_path("old")
        new_db = self._db_path("new")
        input_xbrl_str = self.query_one("#inp-input-xbrl", Input).value.strip()
        output_xbrl_str = self.query_one("#inp-output-xbrl", Input).value.strip()
        perimeter_str = self.query_one("#inp-perimeter", Input).value.strip() or None
        dry_run = self.query_one("#chk-dry-run", Checkbox).value
        facts_parquet_str = self.query_one("#inp-facts-parquet", Input).value.strip()

        if old_db is None or new_db is None:
            log.write("[red]Error: pick an ingested DB version for both old and new[/red]")
            return
        if not input_xbrl_str:
            log.write("[red]Error: input XBRL path is required[/red]")
            return
        if not output_xbrl_str:
            log.write("[red]Error: output XBRL path is required[/red]")
            return

        input_xbrl = Path(input_xbrl_str)
        output_xbrl = Path(output_xbrl_str)
        facts_parquet = Path(facts_parquet_str) if facts_parquet_str else None

        if not input_xbrl.exists():
            log.write(f"[red]Error: input XBRL not found: {input_xbrl}[/red]")
            return

        self.query_one("#btn-run", Button).disabled = True

        handler = RichLogHandler(log)
        attach(handler)

        def worker() -> None:
            try:
                stats = run_apply_delta(
                    old_db,
                    new_db,
                    input_xbrl,
                    output_xbrl,
                    perimeter_override=perimeter_str,
                    dry_run=dry_run,
                    facts_parquet=facts_parquet,
                )
                if self.is_mounted:
                    self.app.call_from_thread(self._on_success, stats, dry_run)
            except Exception as exc:
                if self.is_mounted:
                    self.app.call_from_thread(self._on_error, exc)
            finally:
                detach(handler)

        self.run_worker(worker, thread=True, name="apply-delta")

    def _on_success(self, stats: object, dry_run: bool) -> None:
        if not self.is_mounted:
            return
        self.query_one("#btn-run", Button).disabled = False
        log = self.query_one("#log", RichLog)
        dry_tag = " [yellow](dry run)[/yellow]" if dry_run else ""
        log.write(
            f"[green bold]Done!{dry_tag}[/green bold]\n"
            f"  perimeter=[bold]{stats.perimeter}[/bold]\n"
            f"  facts before={stats.facts_before}  after={stats.facts_after}\n"
            f"  deleted={stats.deleted_facts}  renamed={stats.renamed_facts}\n"
            f"  deleted qnames={stats.deleted_qnames}  modified qnames={stats.modified_qnames}"
        )
        self.notify("Apply complete", severity="information")

    def _on_error(self, exc: Exception) -> None:
        if not self.is_mounted:
            return
        self.query_one("#btn-run", Button).disabled = False
        self.query_one("#log", RichLog).write(f"[red bold]Error:[/red bold] {exc}")
        self.notify(str(exc), severity="error", title="Apply failed")
