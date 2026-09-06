# dpm-tool

A terminal UI toolkit for working with the EIOPA Solvency II **Data Point Model
(DPM)**. It ingests the official DPM release databases into local databases,
compares two DPM versions to produce a reviewable delta, and rolls that delta
forward onto your XBRL instance documents.

Everything runs in a [Textual](https://textual.textualize.io/) TUI — no
subcommands to memorise.

> 📚 For the exhaustive reference — architecture, the ingestion/delta/apply
> pipeline, the data model, and the on-disk layout — see [docs/](docs/index.md).

---

## Install

```bash
uv tool install dpm-tool     # as a standalone tool
# or, inside a project
uv add dpm-tool
```

Requires Python ≥ 3.12.

## Run

```bash
dpm-tool
```

This launches the **EIOPA DPM Toolkit** home screen. Navigate with the arrow
keys, `Enter` to select, `Esc` to go back, `s` for settings, `q` to quit.

---

## What it does

The home screen exposes five workflows:

| Screen | What it does |
| --- | --- |
| **⇩ DPM Ingest** | Project an official EIOPA DPM release database (SQLite) into a versioned local database (`db/versions/<version>.duckdb`). Downloads it from EIOPA if needed. |
| **Δ DPM Delta** | Compare two ingested versions and export a reviewable delta workbook (`Delta_Dpm_<perimeters>.xlsx`). |
| **⇄ XBRL Apply Delta** | Roll the delta between two versions forward onto an XBRL instance, writing a new file (the input is never modified). |
| **⌕ Explore Database** | Browse an ingested DPM database — templates, metrics, dimensions and their members. |
| **≠ Explore Delta** | Browse the computed changes between two versions interactively. |

### Typical flow

1. **Add the versions you track** in **Settings** (`s`). The tool can download
   the official release databases straight from EIOPA, or you can supply an
   explicit URL for the odd hotfix build.
2. **Ingest** the old and new releases into versioned databases.
3. **Delta** the two versions to review what changed, or **Apply Delta** to
   update your XBRL instances.

---

## How the XBRL update works

Matching is **exact and per-cell**: each fact is reduced to its canonical Data
Point Signature (its fixed, non-default dimensional members) and matched against
the delta, so a metric deleted at one cell but surviving at another keeps its
surviving facts. For each fact:

- **Survives** (its cell still exists in the new version, even if it moved) —
  left untouched.
- **Renamed** — the metric's QName changed; the fact tag is renamed.
- **Re-pointed** — the cell's fixed dimensional members changed; the fact is
  pointed at a context carrying the new members (contexts are cloned, never
  edited in place).
- **Deleted** — the cell disappeared entirely; the fact is removed.
- **Added** cells are ignored (no value or context can be inferred).

The perimeter is auto-detected from the instance's `schemaRef`
(e.g. `.../mod/qrs.xsd` → `qrs`) and can be overridden. A dry-run mode reports
what *would* change without writing output, and an optional debug workbook
itemises every deleted, renamed and re-pointed fact.

See [docs/apply_xbrl.md](docs/apply_xbrl.md) for the full algorithm.

---

## Data & config layout

Data and config live in per-user platform directories so the tool works from any
working directory (`~/.local/share/dpm-tool` and `~/.config/dpm-tool` on Linux;
the OS-appropriate equivalents on macOS/Windows).

| Path | Contents |
| --- | --- |
| `<data>/db/versions/` | Ingested DPM version databases (DuckDB). |
| `<data>/db/delta/` | Cached delta databases computed between two versions. |
| `<data>/downloads/` | DPM release databases (SQLite) downloaded from EIOPA. |
| `<config>/config.json` | Tracked versions and the selected UI theme. |

Set `DPM_TOOL_HOME` to override the root and keep everything under one directory.
See [docs/configuration.md](docs/configuration.md) for the full layout.

---

## Development

```bash
uv sync          # install with dev dependencies
uv run dpm-tool  # run from source
uv run pytest    # run the test suite
```

## License

MIT — see [LICENSE](LICENSE).
