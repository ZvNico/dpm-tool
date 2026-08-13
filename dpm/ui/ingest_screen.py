from __future__ import annotations

from pathlib import Path

from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.screen import Screen
from textual.widgets import Button, Footer, Input, Label, ProgressBar, RichLog, Select, Static
from textual import work

from collections.abc import Callable

from dpm._constants import DOWNLOADS_DIR, VERSIONS_DIR
from dpm.config import load_versions
from dpm.eiopa import fetch_annotated_templates
from dpm.ui._utils import OverrideDbModal, detect_version, prompt_open_xlsx
from dpm.ui.log_handler import RichLogHandler, attach, detach
from dpm.workflows import run_ingest

_DB_STEPS = (
    7  # templates, subtemplates, perimeters, perimeter_template, metrics, facts, commit
)


class IngestScreen(Screen):
    CSS_PATH = "ingest_screen.tcss"

    # Last version value we auto-filled; lets us update on new file input
    # without clobbering a version the user typed themselves.
    _auto_version: str = ""

    # version → optional explicit URL for the configured-version dropdown.
    _configured_urls: dict[str, str | None] = {}

    def compose(self) -> ComposeResult:
        yield Vertical(
            Static("DPM Ingest", id="screen-title"),
            Label("Configured version (auto-download)"),
            Select([], id="sel-configured", prompt="Pick a configured version…"),
            Label("Or a workbook (.xlsx)"),
            Horizontal(
                Input(id="inp-file", placeholder="path/to/workbook.xlsx"),
                Button("Browse", id="btn-browse-file", classes="browse"),
                id="file-row",
            ),
            Label("Version (blank = auto-detect)"),
            Input(id="inp-version", placeholder="e.g. 3.4.0"),
            Horizontal(
                Button("← Back", id="btn-back"),
                Button("Run Ingest", id="btn-run", variant="primary"),
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

    def on_mount(self) -> None:
        entries = load_versions()
        self._configured_urls = {e.version: e.url for e in entries}
        options = [
            (f"{e.version}  ({'url' if e.url else 'auto'})", e.version) for e in entries
        ]
        self.query_one("#sel-configured", Select).set_options(options)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "btn-back":
            self.app.action_go_back()
        elif event.button.id == "btn-browse-file":
            self._browse_file()
        elif event.button.id == "btn-run":
            self._start_ingest()

    def on_select_changed(self, event: Select.Changed) -> None:
        if event.select.id != "sel-configured":
            return
        version = event.value
        if isinstance(version, str):
            # A configured version drives the ingest; clear the manual file path.
            self.query_one("#inp-file", Input).value = ""
            self.query_one("#inp-version", Input).value = version
            self.query_one("#btn-run", Button).focus()

    @work
    async def _browse_file(self) -> None:
        current = self.query_one("#inp-file", Input).value.strip()
        start = str(Path(current).parent) if current else None
        path = await prompt_open_xlsx(self, start)
        if path is not None:
            # Setting the value fires on_input_changed → version auto-detect.
            self.query_one("#inp-file", Input).value = str(path)
            self.query_one("#btn-run", Button).focus()

    def on_input_changed(self, event: Input.Changed) -> None:
        """Auto-fill the detected version as the file path is typed.

        Leaves the field alone once the user has typed their own version.
        """
        if event.input.id != "inp-file":
            return
        version_input = self.query_one("#inp-version", Input)
        if version_input.value and version_input.value != self._auto_version:
            return  # user-provided version — don't overwrite
        file_str = event.value.strip()
        detected = detect_version(Path(file_str)) if file_str else None
        self._auto_version = detected or ""
        version_input.value = self._auto_version

    # ── validation + kick-off ──────────────────────────────────────────────

    def _start_ingest(self) -> None:
        file_str = self.query_one("#inp-file", Input).value.strip()
        version_str = self.query_one("#inp-version", Input).value.strip()
        configured = self.query_one("#sel-configured", Select).value

        log = self.query_one("#log", RichLog)
        log.clear()

        # A manual file path takes precedence; otherwise fall back to a
        # configured version that is downloaded (cache-first) on demand.
        if file_str:
            file = Path(file_str)
            if not file.exists():
                log.write(f"[red]Error: file not found: {file}[/red]")
                return
            version = version_str or detect_version(file)
            if not version:
                log.write(
                    "[red]Error: cannot auto-detect version from filename; provide it explicitly[/red]"
                )
                return
            self.query_one("#inp-version", Input).value = version
            self._confirm_and_run(version, lambda _step: file)
            return

        if isinstance(configured, str) and configured:
            url = self._configured_urls.get(configured)
            self._confirm_and_run(
                configured,
                lambda step, v=configured, u=url: fetch_annotated_templates(
                    v, DOWNLOADS_DIR, url=u, on_step=step
                ),
            )
            return

        log.write(
            "[red]Error: pick a configured version or provide a workbook path[/red]"
        )

    @work
    async def _confirm_and_run(
        self, version: str, resolve_file: Callable[[Callable[[str], None]], Path]
    ) -> None:
        """Prompt to override an existing DB for this version, then start ingest.

        ``resolve_file`` yields the workbook path inside the worker (it may
        download); it receives a step callback for progress messages.
        """
        log = self.query_one("#log", RichLog)
        db_path = VERSIONS_DIR / f"{version}.duckdb"
        if db_path.exists():
            override = await self.app.push_screen_wait(OverrideDbModal(version))
            if not override:
                log.write("[yellow]Ingest cancelled — existing DB kept.[/yellow]")
                return
            try:
                db_path.unlink()
            except OSError as exc:
                log.write(f"[red]Could not delete {db_path}: {exc}[/red]")
                return
            log.write(f"[yellow]Overriding existing DB [bold]{db_path.name}[/bold].[/yellow]")

        self.query_one("#btn-run", Button).disabled = True
        self._set_step("Preparing…", "")
        self.query_one("#progress").display = True
        self.query_one("#progress-bar", ProgressBar).update(total=None, progress=0)
        self._run_ingest(resolve_file, version, VERSIONS_DIR)

    # ── progress helpers (called on UI thread) ─────────────────────────────

    def _set_step(self, label: str, detail: str) -> None:
        self.query_one("#step-label", Static).update(label)
        self.query_one("#step-detail", Static).update(detail)

    def _init_bar(self, total: int) -> None:
        self.query_one("#progress-bar", ProgressBar).update(total=total, progress=0)

    def _advance(self, label: str, detail: str = "") -> None:
        self._set_step(label, detail)
        self.query_one("#progress-bar", ProgressBar).advance(1)

    # ── worker ─────────────────────────────────────────────────────────────

    def _run_ingest(
        self,
        resolve_file: Callable[[Callable[[str], None]], Path],
        version: str,
        db_dir: Path,
    ) -> None:
        log = self.query_one("#log", RichLog)
        handler = RichLogHandler(log)
        attach(handler)

        cft = self.app.call_from_thread
        bar_initialized = False

        def on_sheet(idx: int, total: int, name: str) -> None:
            nonlocal bar_initialized
            if not bar_initialized:
                cft(self._init_bar, 2 + total + _DB_STEPS)
                cft(self._advance, "Reading table of contents…")
                bar_initialized = True
            cft(self._advance, f"Parsing sheet {idx}/{total}", name)

        def on_step(label: str) -> None:
            nonlocal bar_initialized
            if not bar_initialized:
                # no template sheets — initialize bar now
                cft(self._init_bar, 2 + _DB_STEPS)
                cft(self._advance, "Reading table of contents…")
                bar_initialized = True
            cft(self._advance, label)

        def fetch_step(label: str) -> None:
            # Download/resolve messages arrive before the bar exists.
            cft(self._set_step, label, "")

        def worker() -> None:
            try:
                file = resolve_file(fetch_step)
                stats = run_ingest(
                    file, version, db_dir, on_sheet=on_sheet, on_step=on_step
                )
                if self.is_mounted:
                    cft(self._on_success, stats)
            except Exception as exc:
                if self.is_mounted:
                    cft(self._on_error, exc)
            finally:
                detach(handler)

        self.run_worker(worker, thread=True, name="ingest")

    # ── completion callbacks ───────────────────────────────────────────────

    def _on_success(self, stats: dict) -> None:
        if not self.is_mounted:
            return
        self.query_one("#btn-run", Button).disabled = False
        self._set_step("Done!", "")
        self.query_one("#log", RichLog).write(
            f"[green]Done![/green] Saved to [bold]{stats['db_path']}[/bold]\n"
            f"  templates={stats['templates']}  perimeters={stats['perimeters']}"
            f"  metrics={stats['metrics']}  facts={stats['facts']}"
        )
        self.notify("Ingest complete", severity="information")

    def _on_error(self, exc: Exception) -> None:
        if not self.is_mounted:
            return
        self.query_one("#btn-run", Button).disabled = False
        self._set_step("Failed", "")
        self.query_one("#log", RichLog).write(f"[red bold]Error:[/red bold] {exc}")
        self.notify(str(exc), severity="error", title="Ingest failed")
