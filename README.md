# dpm-tool

A terminal UI toolkit for working with the EIOPA Solvency II **Data Point Model
(DPM)**. It ingests the official annotated-templates workbooks into local
databases, compares two DPM versions to produce a reviewable delta, and rolls
that delta forward onto your XBRL instance documents.

Everything runs in a [Textual](https://textual.textualize.io/) TUI — no
subcommands to memorise.

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
| **⇩ DPM Ingest** | Parse an EIOPA annotated-templates workbook into a versioned local database (`db/versions/<version>.duckdb`). |
| **Δ DPM Delta** | Compare two ingested versions and export a reviewable delta workbook (`Delta_DPM.xlsx`). |
| **⇄ XBRL Apply Delta** | Roll the delta between two versions forward onto an XBRL instance, writing a new file (the input is never modified). |
| **⌕ Explore Database** | Browse an ingested DPM database — templates, metrics, dimensions and their members. |
| **≠ Explore Delta** | Browse the computed changes between two versions interactively. |

### Typical flow

1. **Add the versions you track** in **Settings** (`s`). The tool can download
   the official workbooks straight from EIOPA, or you can supply an explicit URL
   for the odd hotfix build.
2. **Ingest** the old and new workbooks into versioned databases.
3. **Delta** the two versions to review what changed, or **Apply Delta** to
   update your XBRL instances.

---

## How the XBRL update works

When applying a delta to an XBRL instance:

- **Deleted** metric — matching facts are removed.
- **Modified** metric with a changed QName — the fact tag is renamed.
- **Added** metric — ignored (no value or context can be inferred).
- **Kept** metric — left unchanged.

The perimeter is auto-detected from the instance's `schemaRef`
(e.g. `.../mod/qrs.xsd` → `qrs`) and can be overridden. A dry-run mode reports
what *would* change without writing output.

---

## Data & config layout

| Path | Contents |
| --- | --- |
| `db/versions/` | Ingested DPM version databases (DuckDB). |
| `db/delta/` | Cached delta databases computed between two versions. |
| `data/downloads/` | Source workbooks downloaded from EIOPA. |
| `dpm-tool.config.json` | Tracked versions and the selected UI theme. |

---

## Development

```bash
uv sync          # install with dev dependencies
uv run dpm-tool  # run from source
uv run pytest    # run the test suite
```

## License

MIT — see [LICENSE](LICENSE).
