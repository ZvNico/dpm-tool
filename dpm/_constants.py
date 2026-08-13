from __future__ import annotations

import os
import re
from pathlib import Path

import platformdirs

# ── On-disk layout ──────────────────────────────────────────────────────────
# Everything lives under per-user platform directories so the installed tool
# works from any working directory. Override the root with the DPM_TOOL_HOME
# environment variable (points both data and config at that path).
_ENV_HOME = os.environ.get("DPM_TOOL_HOME")
DATA_ROOT = Path(_ENV_HOME) if _ENV_HOME else Path(platformdirs.user_data_dir("dpm-tool"))
_CONFIG_ROOT = Path(_ENV_HOME) if _ENV_HOME else Path(platformdirs.user_config_dir("dpm-tool"))

# Ingested version databases in ``db/versions`` and computed delta databases in
# ``db/delta``.
DB_ROOT = DATA_ROOT / "db"
VERSIONS_DIR = DB_ROOT / "versions"
DELTA_DIR = DB_ROOT / "delta"

# Downloaded source workbooks are cached here; app config lives at CONFIG_PATH.
DOWNLOADS_DIR = DATA_ROOT / "downloads"
CONFIG_PATH = _CONFIG_ROOT / "config.json"

ROW_RE = re.compile(r"\b[A-Z]{0,3}R\d{3,6}\b", re.I)
COL_RE = re.compile(r"\b[A-Z]{0,3}C\d{3,6}\b", re.I)
CODE_RE = re.compile(r"\b[A-Z]{1,3}\.(?:[0-9]{2}\.){1,6}[0-9]{2}\b", re.I)
QNAME_RE = re.compile(
    r"(^\{[^}]+\}.+)|(^[A-Za-z_][\w.-]*:[A-Za-z_][\w.-]*$)"
    r"|(^[A-Za-z_][\w.-]*\.[A-Za-z_][\w.-]*(?:\.[A-Za-z_][\w.-]*)+$)"
)
QNAME_TOKEN_RE = re.compile(r"\b(?:s2md_met|[A-Za-z_][\w.-]*):[A-Za-z_][\w.-]*\b")
METRIC_LABEL_RE = re.compile(r"Metric:\s*(.*?)(?:\)\s*(?:\[|$)|$)")
# Dimension declaration, e.g. "s2c_dim:BL (Line of business [general])"
DIM_DECL_RE = re.compile(r"\bs2c_dim:([A-Za-z0-9]+)\b", re.I)
# Dimension member, e.g. "s2c_LB:x91 (Neither unit-linked ...)". Excludes the
# `dim` domain so declarations are not mistaken for members.
MEMBER_RE = re.compile(r"\bs2c_(?!dim:)([A-Za-z0-9]+):(x\d+)\b", re.I)
# Trailing parenthetical label, e.g. "... (Line of business [general])"
PAREN_LABEL_RE = re.compile(r"\((.*)\)\s*$")
GENERIC_TOC_CODES = frozenset({"T99", "T.99", "T.99.99", "TOC", "TABLE OF CONTENTS"})

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
IDENTITY_COLS = ["template_code", "subtemplate_code", "row_code", "column_code"]
