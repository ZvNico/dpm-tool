from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from textual import events, on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen, Screen
from textual.widgets import Button, DataTable, Input, RichLog, Static, Tree
from textual_fspicker import FileOpen, Filters
from textual_fspicker.parts import DirectoryNavigation
from rich.text import Text

from dpm.workflows import detect_version  # re-exported for screens

__all__ = [
    "detect_version",
    "ConfirmCancelModal",
    "CopyScreen",
    "OverrideDbModal",
    "file_filters",
    "prompt_open_file",
    "prompt_open_dpm_source",
]


def _plain(value) -> str:
    """Flatten a possibly-styled cell/label to plain text for the clipboard."""
    return value.plain if isinstance(value, Text) else str(value)


def _widget_copy_text(widget) -> str | None:
    """Text to copy for the currently focused widget, or None if it has none.

    ``DataTable`` (row cursor → whole row tab-separated, cell cursor → that cell),
    ``Tree`` (focused node label) and ``RichLog`` (full contents) are supported —
    the interactive widgets that opt out of the screen's drag-to-select, so their
    keyboard cursor needs an explicit bridge to the clipboard.
    """
    if isinstance(widget, DataTable):
        if not widget.row_count:
            return None
        if widget.cursor_type == "row":
            return "\t".join(_plain(c) for c in widget.get_row_at(widget.cursor_row))
        if widget.cursor_type == "cell":
            return _plain(widget.get_cell_at(widget.cursor_coordinate))
        return None
    if isinstance(widget, Tree):
        node = widget.cursor_node
        return _plain(node.label) if node is not None else None
    if isinstance(widget, RichLog):
        return "\n".join(strip.text for strip in widget.lines).rstrip() or None
    return None


def _datatable_all_text(table: DataTable) -> str | None:
    """The whole table as tab-separated text: a header row plus every data row."""
    if not table.row_count:
        return None
    header = "\t".join(_plain(col.label) for col in table.columns.values())
    rows = (
        "\t".join(_plain(c) for c in table.get_row_at(i))
        for i in range(table.row_count)
    )
    return "\n".join([header, *rows])


# Local clipboard backends, tried in order; the first present one wins. Textual's
# own copy_to_clipboard only emits an OSC 52 escape, which many terminals (e.g. the
# VSCode integrated terminal) silently ignore — so we shell out to a real tool when
# one is available and keep OSC 52 as the last-resort fallback.
_CLIPBOARD_COMMANDS = (
    ("wl-copy", ["wl-copy"]),
    ("xclip", ["xclip", "-selection", "clipboard"]),
    ("xsel", ["xsel", "--clipboard", "--input"]),
)


def _system_clipboard_copy(text: str) -> bool:
    """Write ``text`` to the system clipboard via an external tool; True on success."""
    for name, cmd in _CLIPBOARD_COMMANDS:
        if shutil.which(name) is None:
            continue
        try:
            subprocess.run(cmd, input=text.encode(), check=True)
            return True
        except (OSError, subprocess.SubprocessError):
            continue
    return False


class CopyScreen(Screen):
    """Screen base adding a ``c`` shortcut to copy the focused widget's content.

    ``Tree``/``DataTable``/``RichLog`` don't take part in Textual's drag-to-select,
    so their keyboard cursor (highlighted row/cell/node, or the whole log) never
    reaches the clipboard on its own. Focus the widget and press ``c`` to copy it.
    """

    BINDINGS = [
        Binding("c", "copy_focused", "Copy", show=True),
        Binding("a", "copy_all", "Copy all", show=True),
    ]

    def _copy(self, text: str | None) -> None:
        if not text:
            self.notify("Nothing to copy here.", severity="warning")
            return
        # Emit OSC 52 (works in terminals that honour it, e.g. kitty, and over SSH)
        # and, when present, a local tool for terminals that drop OSC 52 (Konsole).
        self.app.copy_to_clipboard(text)
        _system_clipboard_copy(text)
        self.notify("Copied to clipboard.")

    def _fallback_log_text(self) -> str | None:
        """The screen's output log, so ``c``/``a`` copy it even when focus is on a
        button/select rather than the log itself (log screens have a single one)."""
        logs = self.query(RichLog)
        return _widget_copy_text(logs.last()) if logs else None

    def action_copy_focused(self) -> None:
        self._copy(_widget_copy_text(self.focused) or self._fallback_log_text())

    def action_copy_all(self) -> None:
        widget = self.focused
        if isinstance(widget, DataTable):
            self._copy(_datatable_all_text(widget))
        else:
            # Tree/RichLog already copy their whole content; nothing else to widen.
            self._copy(_widget_copy_text(widget) or self._fallback_log_text())

def file_filters(label: str, suffix: str) -> Filters:
    """A two-entry filter: the given suffix (e.g. '.xlsx') plus an all-files fallback."""
    return Filters(
        (label, lambda p: p.suffix.lower() == suffix),
        ("All files (*.*)", lambda _p: True),
    )


_XBRL_FILTERS = file_filters("XBRL (*.xbrl)", ".xbrl")
# The sole ingest source is the official EIOPA DPM SQLite database.
_DPM_SOURCE_FILTERS = Filters(
    ("DPM database (*.db, *.sqlite)", lambda p: p.suffix.lower() in {".db", ".sqlite", ".sqlite3"}),
    ("All files (*.*)", lambda _p: True),
)


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


async def prompt_open_dpm_source(screen: Screen, start: str | None = None) -> Path | None:
    """Open a file-select dialog for a DPM ingest source (the SQLite database)."""
    return await prompt_open_file(
        screen, title="Select DPM database", filters=_DPM_SOURCE_FILTERS, start=start
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
