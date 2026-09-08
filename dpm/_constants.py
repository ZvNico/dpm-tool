from __future__ import annotations

import os
from pathlib import Path

import platformdirs

# ── On-disk layout ──────────────────────────────────────────────────────────
# Everything lives under per-user platform directories so the installed tool
# works from any working directory. Override the root with the DPM_TOOL_HOME
# environment variable (points both data and config at that path).
_ENV_HOME = os.environ.get("DPM_TOOL_HOME")
DATA_ROOT = (
    Path(_ENV_HOME)
    if _ENV_HOME
    else Path(platformdirs.user_data_dir("dpm-tool", appauthor=False))
)
_CONFIG_ROOT = (
    Path(_ENV_HOME)
    if _ENV_HOME
    else Path(platformdirs.user_config_dir("dpm-tool", appauthor=False))
)

# Ingested version databases in ``db/versions`` and computed delta databases in
# ``db/delta``.
DB_ROOT = DATA_ROOT / "db"
VERSIONS_DIR = DB_ROOT / "versions"
DELTA_DIR = DB_ROOT / "delta"

# Downloaded source workbooks are cached here; app config lives at CONFIG_PATH.
DOWNLOADS_DIR = DATA_ROOT / "downloads"
CONFIG_PATH = _CONFIG_ROOT / "config.json"

# User-facing exports (delta workbook, applied XBRL) default to the platform's
# Downloads folder when the user gives a bare filename rather than a full path.
OUTPUT_DIR = Path(_ENV_HOME) if _ENV_HOME else Path(platformdirs.user_downloads_dir())


def resolve_output_path(value: str, default_name: str) -> Path:
    """Resolve a user-entered output path for an export.

    A bare filename (no directory component) lands in :data:`OUTPUT_DIR`; an
    explicit relative or absolute path is honoured as typed. Blank falls back to
    ``default_name`` in :data:`OUTPUT_DIR`.
    """
    text = value.strip()
    if not text:
        return OUTPUT_DIR / default_name
    path = Path(text).expanduser()
    if path.parent == Path("."):
        return OUTPUT_DIR / path
    return path


METRIC_COLS = [
    "perimeter",
    "template_code",
    "subtemplate_code",
    "row_code",
    "column_code",
    "qname",
    "metric_label",
    "row_label",
    "column_label",
]
# Default single value column: a subtemplate whose only column is this is a plain
# row list; any other column code makes it a row×column matrix (see type).
DEFAULT_COLUMN_CODE = "C0010"
KEY_COLS = ["perimeter", "template_code", "subtemplate_code", "row_code", "column_code"]
