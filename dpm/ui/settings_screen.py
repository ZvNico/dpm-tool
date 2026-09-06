from __future__ import annotations

from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import Screen
from textual.widget import Widget
from textual.widgets import Button, Footer, Input, Label, ProgressBar, Static

from dpm._constants import OUTPUT_DIR
from dpm.config import VersionEntry, add_version, load_versions, remove_version
from dpm.eiopa import download_version_files


class VersionRow(Widget):
    """One tracked version: label, source, a download button and a delete button."""

    def __init__(self, entry: VersionEntry) -> None:
        super().__init__()
        self.version = entry.version
        self._source = entry.url or "[dim]auto-download[/dim]"

    def compose(self) -> ComposeResult:
        yield Label(self.version, classes="ver-name")
        yield Label(self._source, classes="ver-source")
        yield Button("⬇ Download", classes="row-dl", variant="primary")
        yield Button("✕", classes="row-del")


class SettingsScreen(Screen):
    CSS_PATH = "settings_screen.tcss"

    BINDINGS = [
        ("delete", "remove", "Remove"),
    ]

    # Version of the focused row (target of the delete shortcut).
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
            yield VerticalScroll(id="versions-list")
            with Vertical(id="progress"):
                yield Static("", id="step-label")
                yield ProgressBar(id="progress-bar", show_eta=False)
        yield Footer()

    def on_mount(self) -> None:
        self.query_one("#progress").display = False
        self._reload()
        self.query_one("#inp-version", Input).focus()

    # ── data ────────────────────────────────────────────────────────────────

    def _reload(self, entries: list[VersionEntry] | None = None) -> None:
        entries = entries if entries is not None else load_versions()
        listing = self.query_one("#versions-list", VerticalScroll)
        listing.remove_children()
        if entries:
            listing.mount_all([VersionRow(e) for e in entries])
        else:
            listing.mount(Label("No versions tracked yet.", classes="empty"))
        self._highlighted = entries[0].version if entries else None

    # ── events ──────────────────────────────────────────────────────────────

    def on_button_pressed(self, event: Button.Pressed) -> None:
        button = event.button
        if button.id == "btn-add":
            self._add()
            return
        row = button.ancestors_with_self
        parent = next((w for w in row if isinstance(w, VersionRow)), None)
        if parent is None:
            return
        self._highlighted = parent.version
        if "row-dl" in button.classes:
            self._download(parent)
        elif "row-del" in button.classes:
            self.action_remove()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        # Enter in either field adds the version.
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

    # ── download ────────────────────────────────────────────────────────────

    def _download(self, row: VersionRow) -> None:
        version = row.version
        btn = row.query_one(".row-dl", Button)
        btn.disabled = True
        self.query_one("#progress").display = True
        self._set_step(f"Preparing download for {version}…")
        self.query_one("#progress-bar", ProgressBar).update(total=None, progress=0)
        self._run_download(version, btn)

    def _set_step(self, label: str) -> None:
        self.query_one("#step-label", Static).update(label)

    def _on_progress(self, done: int, total: int | None, label: str) -> None:
        bar = self.query_one("#progress-bar", ProgressBar)
        bar.update(total=total, progress=done)
        pct = f"  {done * 100 // total}%" if total else ""
        self._set_step(f"{label}{pct}")

    def _run_download(self, version: str, btn: Button) -> None:
        cft = self.app.call_from_thread

        def on_progress(done: int, total: int | None, label: str) -> None:
            cft(self._on_progress, done, total, label)

        def worker() -> None:
            try:
                saved = download_version_files(version, OUTPUT_DIR, on_progress=on_progress)
                if self.is_mounted:
                    cft(self._on_success, version, saved, btn)
            except Exception as exc:  # noqa: BLE001 — surfaced to the user
                if self.is_mounted:
                    cft(self._on_error, exc, btn)

        self.run_worker(worker, thread=True, name=f"download-{version}")

    def _on_success(self, version: str, saved: list, btn: Button) -> None:
        if not self.is_mounted:
            return
        btn.disabled = False
        bar = self.query_one("#progress-bar", ProgressBar)
        if bar.total is not None:
            bar.update(progress=bar.total)
        self._set_step(f"Done — {len(saved)} files in {OUTPUT_DIR / version}")
        self.notify(
            f"Downloaded {len(saved)} files to {OUTPUT_DIR / version}",
            severity="information",
        )

    def _on_error(self, exc: Exception, btn: Button) -> None:
        if not self.is_mounted:
            return
        btn.disabled = False
        self._set_step("Failed")
        self.notify(str(exc), severity="error", title="Download failed")
