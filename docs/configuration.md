# Configuration & storage

Paths are centralised in [`dpm/_constants.py`](../dpm/_constants.py); the app
config is handled by [`dpm/config.py`](../dpm/config.py).

## On-disk layout

Data and config live in **per-user platform directories** so the installed tool
works from any working directory. Defaults (via `platformdirs`) on Linux are
`~/.local/share/dpm-tool` (data) and `~/.config/dpm-tool` (config); macOS and
Windows use the OS-appropriate equivalents.

| Path | Constant | Contents |
| --- | --- | --- |
| `<data>/db/versions/<version>.duckdb` | `VERSIONS_DIR` | Ingested per-version DPM databases. |
| `<data>/db/delta/<old>_to_<new>.duckdb` | `DELTA_DIR` | Cached delta databases between two versions. |
| `<data>/downloads/<version>.db` | `DOWNLOADS_DIR` | DPM SQLite databases downloaded & extracted from EIOPA. |
| `<config>/config.json` | `CONFIG_PATH` | Tracked versions and the selected UI theme. |

### `DPM_TOOL_HOME`

Set the `DPM_TOOL_HOME` environment variable to override the root — it points
**both** the data and config roots (and the default output dir) at that single
path, keeping everything under one directory. Useful for tests, CI, or a
portable install.

## Output paths for exports

User-facing exports (the delta workbook, the applied XBRL, debug workbooks)
resolve their destination with `resolve_output_path(value, default_name)`:

- a **bare filename** (no directory component) lands in the output dir — the
  platform Downloads folder by default, or `DPM_TOOL_HOME` when set;
- an explicit **relative or absolute** path is honoured as typed;
- **blank** falls back to `default_name` in the output dir.

## The config file (`config.json`)

A small JSON document with two keys:

```json
{
  "versions": [
    { "version": "2.8.0" },
    { "version": "2.10.0", "url": "https://dev.eiopa.europa.eu/.../custom.zip" }
  ],
  "theme": "textual-dark"
}
```

- **`versions`** — the DPM versions you track. Each entry is a `version` plus an
  optional explicit download `url`. The URL overrides EIOPA URL auto-resolution
  (see [ingestion](ingestion.md#stage-1--fetch-the-release-dpmeiopa)) and is only
  needed for the odd hotfix build whose URL cannot be probed mechanically.
  Entries are always stored sorted by dotted-numeric version.
- **`theme`** — the selected Textual UI theme, persisted whenever it changes
  (e.g. via the command palette) and restored on launch (an unknown/removed
  theme name is ignored).

`config.py` exposes `load_versions` / `save_versions` / `add_version` /
`remove_version` and `load_theme` / `save_theme`; writes merge into the existing
file so unrelated keys survive. Managing versions is done in the **Settings**
screen; the theme is changed via the command palette.
