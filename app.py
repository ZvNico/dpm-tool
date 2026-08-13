from __future__ import annotations

import logging

from textual import work
from textual.app import App

from dpm.config import load_theme, save_theme
from dpm.ui._utils import ConfirmCancelModal
from dpm.ui.main_screen import MainScreen


class DpmToolApp(App):
    TITLE = "EIOPA DPM Toolkit"
    BINDINGS = [
        ("q", "quit", "Quit"),
        ("escape", "go_back", "Back"),
    ]

    def on_mount(self) -> None:
        # Restore the persisted theme (ignore an unknown/removed theme name), then
        # persist any later change (e.g. via the command palette) as it happens.
        saved = load_theme()
        if saved and saved in self.available_themes:
            self.theme = saved
        self.watch(self, "theme", self._persist_theme, init=False)

        # Push rather than override get_default_screen: Textual only loads a
        # screen's CSS_PATH when the screen is pushed, not for the default screen.
        self.push_screen(MainScreen())

    def _persist_theme(self, theme: str) -> None:
        save_theme(theme)

    def action_go_back(self) -> None:
        self._go_back()

    @work(exclusive=True, name="go-back")
    async def _go_back(self) -> None:
        """Go back a screen, confirming first if the current screen has a running task.

        Shared by the Escape binding and every screen's '← Back' button. ``workers`` is
        app-global, so we match on ``w.node`` to count only the current screen's tasks
        (this back worker itself runs on the app).
        """
        if isinstance(self.screen, MainScreen):
            return  # main menu — nothing to go back to
        screen = self.screen
        running = [w for w in self.workers if w.is_running and w.node is screen]
        if running:
            if not await self.push_screen_wait(ConfirmCancelModal()):
                return
            for w in running:
                w.cancel()
        self.pop_screen()


def main() -> None:
    logging.basicConfig(level=logging.WARNING)
    DpmToolApp().run()


if __name__ == "__main__":
    main()
