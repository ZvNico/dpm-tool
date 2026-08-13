from __future__ import annotations

import logging

from textual.widgets import RichLog


class RichLogHandler(logging.Handler):
    def __init__(self, rich_log: RichLog) -> None:
        super().__init__()
        self._rich_log = rich_log

    def emit(self, record: logging.LogRecord) -> None:
        try:
            msg = self.format(record)
            self._rich_log.app.call_from_thread(self._rich_log.write, msg)
        except Exception:
            self.handleError(record)


_FORMATTER = logging.Formatter("%(asctime)s %(levelname)-8s %(name)s — %(message)s")

DPM_LOGGERS = [
    "dpm.parser",
    "dpm.toc",
    "dpm.db",
    "dpm.workflows",
    "dpm.delta",
    "dpm.excel",
    "dpm.xbrl",
]


def attach(handler: RichLogHandler, level: int = logging.DEBUG) -> None:
    handler.setFormatter(_FORMATTER)
    for name in DPM_LOGGERS:
        lg = logging.getLogger(name)
        lg.addHandler(handler)
        lg.setLevel(level)
        lg.propagate = False  # prevent records reaching root's StreamHandler and leaking onto the terminal


def detach(handler: RichLogHandler) -> None:
    for name in DPM_LOGGERS:
        lg = logging.getLogger(name)
        lg.removeHandler(handler)
        lg.propagate = True
