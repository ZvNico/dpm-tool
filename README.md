# DPM Delta and XBRL Update Tool

## What it does

Two scripts are provided:

- `dpm_delta.py`: compares two EIOPA DPM Excel workbooks and creates `Delta_DPM.xlsx`.
- `xbrl_apply_delta.py`: applies that delta to an XBRL file and writes a new updated XBRL file.

The input XBRL is never modified.

---

## Setup

```bash
uv sync
```

---

## 1. Create the DPM delta

```bash
uv run python dpm_delta.py OLD_DPM.xlsx NEW_DPM.xlsx Delta_DPM.xlsx
```

Example:

```bash
uv run python dpm_delta.py \
  EIOPA_DPM_2.8.2.xlsx \
  EIOPA_DPM_2.10.0.xlsx \
  Delta_DPM.xlsx
```

The script detects available perimeters from `Table of Contents` and asks you to select them.

To skip selection and compare all perimeters:

```bash
uv run python dpm_delta.py OLD_DPM.xlsx NEW_DPM.xlsx Delta_DPM.xlsx --no-interactive-perimeters
```

Useful option:

```bash
--log-level DEBUG
```

---

## 2. Apply the delta to an XBRL file

```bash
uv run python xbrl_apply_delta.py Delta_DPM.xlsx input.xbrl output.xbrl
```

Example:

```bash
uv run python xbrl_apply_delta.py \
  Delta_DPM.xlsx \
  report_before.xbrl \
  report_after.xbrl
```

The perimeter is detected from the XBRL `schemaRef`, for example:

```xml
<link:schemaRef xlink:href=".../mod/qrs.xsd" />
```

Force a perimeter if needed:

```bash
uv run python xbrl_apply_delta.py Delta_DPM.xlsx input.xbrl output.xbrl --perimeter qrs
```

Dry run:

```bash
uv run python xbrl_apply_delta.py Delta_DPM.xlsx input.xbrl output.xbrl --dry-run
```

Export flattened facts for debugging:

```bash
uv run python xbrl_apply_delta.py \
  Delta_DPM.xlsx input.xbrl output.xbrl \
  --facts-parquet facts_debug.parquet
```

---

## What happens to XBRL facts

- `Deleted` metric: matching facts are removed.
- `Modified` metric with changed QName: fact tag is renamed.
- `Added` metric: ignored because no value/context can be inferred.
- `Kept` metric: unchanged.

---

## Typical full run

```bash
uv run python dpm_delta.py OLD_DPM.xlsx NEW_DPM.xlsx Delta_DPM.xlsx

uv run python xbrl_apply_delta.py \
  Delta_DPM.xlsx \
  input.xbrl \
  output.xbrl
```
