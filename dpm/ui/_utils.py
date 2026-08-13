from __future__ import annotations

from pathlib import Path

from textual import events, on
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen, Screen
from textual.widgets import Button, Input, Static
from textual_fspicker import FileOpen, Filters
from textual_fspicker.parts import DirectoryNavigation

from dpm.workflows import detect_version  # re-exported for screens

__all__ = [
    "detect_version",
    "ConfirmCancelModal",
    "OverrideDbModal",
    "file_filters",
    "prompt_open_file",
    "prompt_open_xlsx",
]

def file_filters(label: str, suffix: str) -> Filters:
    """A two-entry filter: the given suffix (e.g. '.xlsx') plus an all-files fallback."""
    return Filters(
        (label, lambda p: p.suffix.lower() == suffix),
        ("All files (*.*)", lambda _p: True),
    )


_XLSX_FILTERS = file_filters("Excel workbooks (*.xlsx)", ".xlsx")
_XBRL_FILTERS = file_filters("XBRL (*.xbrl)", ".xbrl")


class _FilePicker(FileOpen):
    """FileOpen tailored for a click/double-click pick flow.

    - Hides the filter dropdown (the filter still applies) and constrains the input
      bar to the dialog width with fixed-width buttons, so both Open and Cancel stay
      visible (upstream lets the bar overflow, which clips Cancel when narrow).
    - Double-clicking a file opens it immediately (upstream only double-click-opens
      directories; a double-clicked file otherwise just loads its name in the input).
    """

    DEFAULT_CSS = """
    _FilePicker FileFilter { display: none; }
    _FilePicker InputBar { width: 100%; height: auto; }
    """

    _highlighted: Path | None = None

    def on_mount(self) -> None:
        super().on_mount()
        # Give the actions button colours so they clearly read as buttons.
        self.query_one("#select", Button).variant = "primary"
        self.query_one("#cancel", Button).variant = "error"

    @on(DirectoryNavigation.Highlighted)
    def _remember_highlight(self, event: DirectoryNavigation.Highlighted) -> None:
        self._highlighted = event.path

    def on_click(self, event: events.Click) -> None:
        if getattr(event, "chain", 1) != 2:
            return
        path = self._highlighted
        if path is None or not path.is_file():
            return
        nav = self.query_one(DirectoryNavigation)
        if not nav.region.contains(event.screen_x, event.screen_y):
            return
        self.query_one(Input).value = path.name
        self._confirm_file(event)


async def prompt_open_file(
    screen: Screen,
    *,
    title: str = "Select file",
    filters: Filters | None = None,
    must_exist: bool = True,
    start: str | None = None,
) -> Path | None:
    """Open a file-select dialog and return the chosen path (or None).

    Must be awaited from within a worker (``push_screen_wait`` requirement).
    """
    return await screen.app.push_screen_wait(
        _FilePicker(
            start or ".",
            title=title,
            filters=filters,
            must_exist=must_exist,
            open_button="Open",
            cancel_button="Cancel",
        )
    )


async def prompt_open_xlsx(screen: Screen, start: str | None = None) -> Path | None:
    """Open a file-select dialog filtered to .xlsx (backwards-compatible helper)."""
    return await prompt_open_file(
        screen, title="Select workbook", filters=_XLSX_FILTERS, start=start
    )


class ConfirmCancelModal(ModalScreen[bool]):
    """Confirm leaving a screen while a task is running. Dismisses True to cancel & go back."""

    CSS_PATH = "_confirm_cancel_modal.tcss"

    def compose(self) -> ComposeResult:
        yield Vertical(
            Static("A task is still running.", id="msg"),
            Static("Cancel it and go back?", id="sub"),
            Horizontal(
                Button("Keep running", id="btn-keep"),
                Button("Cancel & go back", id="btn-cancel", variant="error"),
                id="btn-row",
            ),
            id="dialog",
        )

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "btn-cancel")


class OverrideDbModal(ModalScreen[bool]):
    """Ask whether to override an already-ingested DB. Dismisses True to override."""

    CSS_PATH = "_override_db_modal.tcss"

    def __init__(self, version: str) -> None:
        super().__init__()
        self._version = version

    def compose(self) -> ComposeResult:
        yield Vertical(
            Static(f"A database for version {self._version} already exists.", id="msg"),
            Static(
                "Override it? This deletes the existing DB and re-ingests.", id="sub"
            ),
            Horizontal(
                Button("Cancel", id="btn-keep"),
                Button("Override", id="btn-override", variant="error"),
                id="btn-row",
            ),
            id="dialog",
        )

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "btn-override")
