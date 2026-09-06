# Applying a delta to an XBRL instance

Apply migrates an existing XBRL instance document from the old taxonomy version
to the new one, using the cached delta as the map. It is implemented in
[`dpm/xbrl.py`](../dpm/xbrl.py) and orchestrated by
[`workflows.run_apply_delta`](../dpm/workflows.py). The input file is **never
modified** — a new instance is written (and output must differ from input).

## The core idea: exact per-cell matching

A naive migration keyed on the metric qname alone is wrong: the same metric
appears in many cells, so "metric X was deleted at one cell" must not wipe every
fact of X. Apply is **exact** because it matches on a cell's **fixed Data Point
Signature** — the closed, non-default `(dimension, member)` members that pin the
cell down — not on the metric qname alone. A metric deleted at one cell but
surviving at another keeps its surviving facts.

A dimension can be an open axis in one table yet a fixed key member in another
(e.g. `TB` is free-form in most tables but fixed to `s2c_LB:x28` in
`S.27.03.01.01`), so matching is **per cell**: a single global "strip the open
dimensions" pass cannot canonicalise a fact.

## The matching index (`DpsDelta`)

The delta is indexed by metric qname into three maps (`build_dps_delta`, stored
as `datapoint_changes` and reloaded via `rows_to_dps_delta`):

- **`survivor`** — for each metric, the fixed signatures of every cell present in
  the *new* version (from `Kept`/`Added`/`Modified` rows). A fact matching one
  still exists and is left untouched, **even if its cell moved** row/column
  (which the coordinate-keyed delta reports as a Deleted old cell + an Added new
  one).
- **`modified`** — links an old cell's `(qname, fixed set)` to its successor
  `(new qname, new fixed set)`. Drives renames and dimensional re-points.
- **`deleted`** — old fixed sets with no successor.

Resolution order when building the index guarantees **survival wins**: a
conflicting modified link is dropped; any signature that also survives is removed
from `modified` and `deleted`; any modified key is removed from `deleted`.

## Reducing an instance fact to its key

For each metric fact in the instance:

1. Its `contextRef` is resolved to the context's explicit `(dimension, member)`
   set (`parse_contexts` / `EXPLICIT_MEMBER_RE`). `typedMember` dimensions carry
   a typed value, not a member token, and are ignored.
2. **Default members are stripped** (using `default_members` from the delta) —
   instances omit defaults, and the DPS lists only non-default members, so this
   makes the two comparable.
3. The remaining non-default members are matched against the metric's candidate
   fixed signatures with `_match_fixed`: a candidate matches when **all** its
   fixed members are present **and** every *extra* member the fact carries sits
   on an **open axis** (`open_dimensions`). The **most specific** (largest)
   matching candidate wins, so a fact lands on its true cell rather than a less
   specific one sharing a prefix.

## The decision per fact (`_decide_fact`)

Checked in this order:

1. **Survives** → keep untouched (return `None`, leave its bytes in place).
2. **Modified** → the best modified link gives a target `(new qname, new fixed
   set)`. The fact's target members are `(members − old_fixed) | new_fixed`
   (open-axis members preserved). If the qname changed the fact is **renamed**;
   if the members changed it is **re-pointed** to a context carrying the new
   member set. (If neither actually changed, it is kept.)
3. **Deleted** → the fact is removed.
4. **Unmatched** → kept. This is the exactness guarantee: nothing is deleted
   unless its cell truly disappeared.

## Rewriting the bytes

Apply works on the raw bytes with regexes (not a DOM parse) and splices in place:

- **Rename** — `_rename_fact_bytes` rewrites the fact's open/close tag qname.
- **Delete** — the fact's byte span is dropped (`_expand_deleted_span` also
  swallows the surrounding blank line/newline so no empty lines are left).
- **Re-point** — the fact's `contextRef` is pointed at a context carrying the new
  members. Contexts are **cloned, not edited in place** (a context is shared by
  many facts): `_clone_context` copies the block with a fresh id
  (`cdpm<N>`) and the explicit members replaced, preserving entity, period and
  any `typedMember` elements. A signature index (`sig_to_id`, seeded with
  existing contexts) reuses a matching context instead of duplicating it. New
  context blocks are spliced in before the closing root element.

Both `<met:...>value</met:...>` and self-closing `<met:.../>` forms are matched
(`flatten_metric_facts`), and overlapping matches are guarded against.

## Pruning orphaned contexts

Deleting facts and re-pointing them to cloned contexts can leave `<xbrli:context>`
blocks that nothing references any more. As a **final pass over the fully patched
document**, `_prune_orphan_contexts` removes them: it scans the output for **every**
`contextRef` — metric facts, filing indicators (`find:filingIndicator`), footnotes,
any consumer — and drops each context whose id appears in none of them
(`_expand_deleted_span` again swallows the surrounding blank line).

Running on the final bytes is what makes this correct and simple:

- Newly cloned/reused contexts **survive**, because the facts we just re-pointed
  reference them.
- A context still used by a **non-metric** element (e.g. a filing indicator) is
  **kept** — scanning only metric facts would wrongly delete it.
- Contexts already orphaned in the input are cleaned up too.

The count of removed contexts is reported as `ApplyStats.removed_contexts`.

## Perimeter detection

The perimeter is auto-detected from the instance's `schemaRef` href
(`detect_perimeter_from_xml_bytes`) — e.g. `.../mod/qrs.xsd` → `qrs` — and can be
overridden with `perimeter_override`. Multiple candidates log a warning and use
the first.

## Outputs and options

`apply_delta` returns an `ApplyStats` (perimeter, facts before/after, and counts
of deleted / renamed / re-pointed facts, new contexts, **removed (orphaned)
contexts**, and deleted/modified qnames). Options:

- **`dry_run`** — compute and report everything but do not write the output file.
- **`debug_xlsx`** — write a debug workbook (`generate_apply_debug_workbook`)
  itemising every deleted, renamed and re-pointed fact and every new context,
  for review; its Summary also reports the removed-context count.

## Summary of what happens to each fact

| Situation | Action |
| --- | --- |
| Cell's fixed signature still exists in the new version | Kept, untouched (even if the cell moved). |
| Metric qname changed at the cell | Fact tag renamed. |
| Cell's fixed dimensional members changed | Fact re-pointed to a (cloned/reused) context with the new members. |
| Cell disappeared entirely | Fact deleted. |
| No match in the delta | Kept — exactness prevents collateral deletion. |
| Context left with no referencer after the above | Pruned in the final orphan-cleanup pass. |
