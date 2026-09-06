# dpm-tool reviews: (1) current vs target DB architecture, (2) current data vs EIOPA-available data, (3) how Solvency II reporting works

## Review 1 — Current DB architecture vs the target our app needs

### 1a. What we have today (`dpm/db.py`, 9 tables — model *structure* only, no instance facts)

| Table | Columns | Role |
|---|---|---|
| `templates` | template_code, label | template catalogue |
| `subtemplates` | subtemplate_code, template_code, label, type | table catalogue |
| `perimeters` | perimeter_code | perimeter list |
| `perimeter_template` | perimeter_code, template_code | which templates a perimeter files |
| `metrics` | metric_code, label | metric catalogue (label only) |
| `facts` | subtemplate_code, row_code, column_code, row/col label, **metric_code** | one row per **template cell** (a coordinate), keyed by cell position |
| `dimensions` | dimension_code (`s2c_dim:XX`), label | dimension catalogue |
| `dimension_members` | member_code (`s2c_YY:zN`), dimension_code, label | member catalogue |
| `fact_dimensions` | subtemplate_code, row_code, column_code, dimension_code, member_code | dimensional context of each cell |

Built by parsing the **Annotated Templates Excel** (`dpm/parser.py`,
`_extract_window_dimensions`). Delta tables (`structure_changes`, `metric_changes`,
`member_changes` in `dpm/delta_schema.py`) are computed by diffing two such DBs.

### 1b. What the app must do → what the schema must support

Jobs: **ingest** a DPM version · **delta** two versions · **apply** the delta to an XBRL
instance (delete/rename/re-point facts) · **explore** in the TUI.

The apply job needs to map each instance fact `(metric, full dimensional context)` to exactly
one model data point. That is where today's schema fails.

### 1c. Gap analysis (what's missing vs needed)

| Gap | Consequence today | Needed for |
|---|---|---|
| **No default-member flag** (`is_default` / `dimension.default_member`) | implicit members (`VG=x80`) captured inconsistently → instance can't be matched | correct apply (the mi363 bug) |
| **No canonical Data Point Signature per cell** | facts keyed by template position, not by their dimensional identity → same coordinate reused/ambiguous | 1:1 instance↔model match |
| **Dimensions captured positionally/partially** | ~45% of cells carry `VG`, 55% don't | complete coordinates |
| **`metrics` has label only** — no data_type / period (flow-stock) / balance / referenced domain | can't tell a fact's unit/decimals or that an enum metric's *value* is a member | correct apply, validation, enum handling |
| **No typed-dimension modelling** | typedMembers only handled ad-hoc in apply | completeness |
| **No hierarchies / open-axis restrictions** | can't model open/semi-open tables or member relationships | validation, open tables |
| **No explicit version/taxonomy metadata** (version, from/to) | version lives only in the filename | multi-version correctness |
| **No module ↔ filing-indicator ↔ table map** | can't tell which tables a filing covers | targeted apply/validation |
| **No validation rules** | none | future validation feature |

### 1d. Target architecture (what "correct" needs)

The minimal target adds, to today's schema:
- `dimension_members.is_default` (+ `dimensions.default_member_code`) — the **default members**.
- a canonical **`data_point_signature`** per cell on `facts` (the metric + non-default
  dimension-member set, XBRL-coded) — the exact key an instance fact normalises to.
- richer `metrics`: `data_type`, `period_type`, `balance`, `referenced_domain` (+ hierarchy).
- version/taxonomy row (version, from/to dates) so a DB self-describes its model version.

The full target mirrors the relevant subset of the **official EIOPA DPM database** (see Review 2):
`mDimension`(+DefaultMemberID) · `mMember`(+IsDefaultMember) · `mMetric`(datatype/flow/balance/
referenced domain+hierarchy) · `mTableCell.DPS` · `mHierarchy(Node)` · `mOpenAxisValueRestriction`
· `mTaxonomy`/`mModule`/`mModuleBusinessTemplate`. Two ways to reach it:
- **Adopt** the official DPM DB as the ingestion source (authoritative, no parsing guesswork), or
- **Augment** today's parser-built DB with the default-member + DPS data pulled from it.

## Review 2 — Data we have vs what EIOPA resources can additionally provide

Current DB content = **structure + labels only**, from one Excel file. Each EIOPA artifact adds:

| EIOPA resource | Additional data beyond what we have now |
|---|---|
| **DPM Database (SQLite)** | **Default members** (`IsDefaultMember`, `DefaultMemberID`); **canonical DPS per cell** (with default-omission `?` marks); **metric** data_type / flow-stock / balance / referenced domain+hierarchy; **hierarchies** (member trees, aggregation operators); **open/semi-open axis restrictions**; **typed dimensions**; **versioning** (taxonomy from/to, version); **modules** = filing units + filing-indicator↔table map; **validation rules** referencing DPS. Authoritative, no parsing. |
| **DPM Dictionary (xlsx)** | Same dictionary content (domains/members/dimensions/metrics + labels & descriptions) in spreadsheet form — richer descriptions than we store. |
| **Annotated Templates (xlsx)** | What we parse today — visual layout; the DB above supersedes it for structure. |
| **Change Log 2.10.0 vs 2.8.2 (xlsx)** | The **official delta** between versions — could replace/validate our hand-computed `structure_changes`/`metric_changes`/`member_changes`. |
| **XBRL Taxonomy (zip)** | The authoritative source: **dimension-default arcs** (defaults), hypercubes (which dims apply per table), enumerations, **validation/formula linkbases**, presentation labels. Ground truth if we ever parse XBRL directly. |
| **XBRL Instance examples (zip)** | Real/golden instances — test fixtures for apply + round-trip validation. |
| **Validations list (xlsx) + syntax (pdf)** | Cross-template/intra-cell checks — enables a validation feature and post-apply verification. |
| **Filing Rules (pdf)** | Rules for building valid instances (default-member omission, contexts, units, decimals) — directly informs correct apply output. |
| **DB / DPM / Taxonomy Documentation (pdf)** | Schema + methodology references (used for this review). |
| **Technical instructions (S.30.x, PEPP)** | Template-specific reporting semantics. |

### Bottom line
Today's DB is a **partial, label-level projection of the model structure parsed from one Excel
file**, missing the two things that make instance-patching correct — **default members** and a
**canonical data-point signature**. Both are provided authoritatively by the **official EIOPA DPM
database**, which also unlocks metric semantics, hierarchies, versioning, filing-unit mapping, and
validations. The target is to source those (adopt the DPM DB, or augment our DB with its default+
DPS data); everything else EIOPA ships (change log, taxonomy, instance examples, filing rules,
validations) supports delta/apply/validation/testing around it.

---

## Review 3 — How Solvency II reporting works (plain-language, with concrete examples)

### The 30-second version
An EU insurance company must periodically prove to its regulator that it holds enough capital
to survive bad years. To do that it files a big, strict "financial return" — hundreds of tables
of numbers — every quarter/year. Think of it as a **tax return for solvency**: standardised
forms, filled with numbers, submitted in a machine-readable file. That file is **XBRL**. The
rulebook that defines *every number that may appear* is the **DPM + taxonomy**. `dpm-tool` reads
that rulebook (into our DB), compares two versions of it, and edits a submitted file so it
matches a newer rulebook.

### One reported number, peeled open (the running example)
In the filing `ars_2024-10-15_instance.xbrl` there is this line:

```xml
<s2md_met:mi251 unitRef="uEUR" decimals="-3" contextRef="c6">4800000</s2md_met:mi251>
```
It means: **"Our premiums written = €4,800,000."** Everything below explains the pieces.

### The objects, bottom-up — each with a concrete example

- **Reporting framework** — the whole regime. *Example:* **Solvency II**. (Others exist, e.g.
  Pension Funds.)
- **Taxonomy / version** — one dated edition of the rulebook. *Example:* **Solvency II 2.10.0**
  (vs the older **2.8.2 Hotfix**). A version change is *why* `dpm-tool` exists.
- **Template (QRT)** — one report form. *Example:* **S.02.01 = Balance Sheet**; **S.06.02 =
  List of Assets**; **S.05.01 = Premiums, claims & expenses**. A filing includes a set of them.
- **Filing indicator** — a flag saying "this form is included in this submission." *Example:*
  `<find:filingIndicator>S.02.01</find:filingIndicator>`.
- **Table / subtemplate** — the actual grid of a template (a template can have several).
  *Example:* the balance-sheet grid with asset rows down the side, value columns across the top.
- **Row / Column** — coordinates on the grid. *Example:* **R0010** = "Goodwill", **C0010** =
  "Solvency II value". Their intersection is one box.
- **Cell / data point** — one box = one reportable number's *definition*. *Example:* "S.02.01,
  R0010, C0010" = *the* place you report the Solvency II value of goodwill.
- **Metric** — *what kind of number* the cell holds. *Example:* `s2md_met:mi251` = "premiums
  written (a monetary amount)". A metric carries a data type (monetary/percentage/date/…), so
  the file knows to attach `unitRef="uEUR"` and `decimals`.
- **Dimension** — an *extra qualifier axis* that splits a number by category. *Example:*
  `s2c_dim:VI` = "valuation/variant", `s2c_dim:BL` = "line of business", `s2c_dim:VG` = a
  value-nature axis.
- **Domain + Member** — a dimension's allowed values; a **member** is one value. *Example:*
  domain `LB` (lines of business) has members like `s2c_LB:x23` = "motor insurance". A
  dimension picks its values from a domain.
- **Default member** — the value assumed when a dimension is *not written* in the file.
  *Example:* `s2c_dim:VG`'s default is `s2c_AM:x80`; because it's the default, the file may omit
  it. **This omission is the root of dpm-tool's matching problem.**
- **Hierarchy** — members arranged in a parent/child tree (and how they sum). *Example:* "total
  assets" = sum of its child asset lines.
- **Context** — the bundle of qualifiers (entity + reporting date + the dimension→member picks)
  shared by facts. *Example:* context `c6` = "entity XYZ, 2025-12-31, VG=x80".
- **Fact** — one *actual reported value* = metric + context + number. *Example:* `mi251` on `c6`
  = `4800000`. This is what lives in the instance file and what `dpm-tool` edits.
- **Unit** — the measure a monetary/numeric fact is in. *Example:* `uEUR` = euros.
- **Data Point Signature (DPS)** — the model's *canonical name* for a data point: its metric +
  its non-default dimension-members. *Example (conceptual):* `mi363|VI=x64` (the implicit
  `VG=x80` is dropped because it's default). This is the clean key that would let us match a
  file's fact to the rulebook 1:1.

### How they relate (the shape of the data)
```
Framework (Solvency II)
  └─ Taxonomy version (2.10.0)
       ├─ Template / QRT (S.02.01 Balance Sheet)     ← turned on by a Filing Indicator
       │    └─ Table (grid)
       │         ├─ Rows (R0010 …) × Columns (C0010 …) → Cell (data point)
       │         │        └─ Metric (mi251) + Dimensions→Members (VI=x64, …) = DPS
       │         └─ Axes may be "open" (a variable list, e.g. one row per asset)
       └─ Dictionary: Dimensions → Domains → Members (+ default member) → Hierarchies

Instance file (what a company submits)
  ├─ Contexts (entity + date + member picks)   ← Units (uEUR)
  └─ Facts:  metric  +  contextRef  =  value    (mi251 @ c6 = 4,800,000)
```
Key relationships: a **fact** points to one **metric** and one **context**; a **context** holds
many **(dimension→member)** picks and is **shared by many facts**; a **metric** appears in many
**cells** across templates; a **cell** = metric + a fixed set of members = a **DPS**.

### Concrete usage — what actually happens, end to end
1. **Rulebook load:** EIOPA publishes Solvency II 2.10.0 (templates, dimensions, members,
   defaults, validations). `dpm-tool` ingests it into our DB.
2. **A company files:** insurer XYZ submits `ars_…instance.xbrl` built against the *old* 2.8.2
   rulebook — a bag of contexts + facts like `mi251@c6 = 4,800,000`.
3. **The rulebook changes:** 2.10.0 renames a metric, removes some cells, and moves a
   dimension member (e.g. `VI: x64 → x116`). EIOPA ships a **Change Log**; `dpm-tool` computes
   the same **delta**.
4. **Apply/migrate:** `dpm-tool` rewrites the old file to the new rulebook — rename the metric
   tag, delete facts whose cell no longer exists, and re-point facts whose dimensional context
   moved — so the file validates against 2.10.0. (This is the feature whose correctness hinges
   on knowing default members / DPS.)
5. **Validate & submit:** the migrated file is checked against the taxonomy's validation rules,
   then sent to the supervisor.

### Why this was hard (tie back to the bug)
Because the rulebook we parsed omits **default members** inconsistently, one submitted fact
(`mi363 {VI=x64, VG=x80}`) matches several rulebook cells at once (a Modified `{VI=x64}` and a
Kept `{VG=x80}`), so `dpm-tool` can't tell whether to keep, move, or delete it. The **DPS +
default-member data** from EIOPA's official DPM database is precisely what removes that ambiguity.