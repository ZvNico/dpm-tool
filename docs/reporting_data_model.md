# Solvency II / EIOPA Reporting — Business Logic

A plain-language reference for the data model behind the DPM tool: what a
**fact** is, how **metric**, **dimension**, **member**, **domain** and
**context** relate, and how those concepts map to the DuckDB tables in this
project.

---

## 1. The core idea

Solvency II reporting is a large pile of numbers sent to the regulator. A number
on its own is meaningless. To turn a raw number into a **fact**, you must pin
down three things:

| Question | Answered by |
|---|---|
| *What* is being measured? | the **metric** |
| *Under what circumstances?* | the **context** (dimension = member pairs + entity + period) |
| *What is the answer?* | the **value** (a number, or a **member** if the metric is enumerated) |

Combine those three and you have one fact.

---

## 2. The vocabulary

### Metric
*What* is being measured. It also defines the **data type** of the value:

- **Monetary** — an amount (codes like `mi2004`; e.g. €3,084,000).
- **Integer / decimal / percentage** — plain numbers.
- **Boolean / date / string** — true-false, a date, free text.
- **Enumeration** — the value must be **picked from a fixed list**, i.e. the
  value *is a member* (codes often `ei…`; e.g. value `s2c_GA:fr` = France).

The metric type decides whether a fact's value is a **number** or a **member**.

### Dimension
A *question you must answer* about the number — one axis of "circumstances."
Examples: *"Line of business?"*, *"Country?"*, *"Risk scenario?"*. A dimension
is not free text; its answer must come from a fixed list.

### Member
*One allowed answer* to a dimension (or one allowed value of an enumerated
metric). Example: `s2c_RT:x11` = "Before diversification effect".

### Domain
The **reusable list of allowed answers**. Crucially, the *same* list can be
reused by more than one question. That shared list is a **domain**.

**Dropdown analogy:**
- **Domain** = the dropdown's list of options (defined once — e.g. all countries).
- **Dimension** = an actual dropdown placed on a form that uses that list
  ("Country where risk is located", "Country of the insurer" — both use the
  country list).
- **Member** = one option in the list ("France").

So: one **domain** → reused by several **dimensions** → each offering the same
**members**.

### Context
The bundle of circumstances attached to a value in an instance document: the
reporting **entity**, the **period**, and the set of **dimension = member**
pairs. In XBRL this is the `<xbrli:context>` block.

### Fact
A single reported value, together with its metric and context. The fact is the
**assembly point** — the finished product.

---

## 3. How they relate ("input of" direction)

Everything points **toward** the fact. The fact is never an input to anything.

```
                    ┌─────────── metric        (what is measured)
                    │
      FACT  ◄───────┼─────────── value         (a number  OR  a member, if enumerated)
   (the number      │
    you report)     └─────────── context ◄──── (dimension = member) pairs
                                    ▲            + entity + period
                                    │
                              built from
```

Read as "X is the input/answer to Y":

- **member → dimension** — a member is the answer plugged into a dimension.
- **(dimension, member) pair → context** — the *pair* is the input, not the
  dimension alone; the context is assembled from several pairs (+ entity, period).
- **metric + context + value → fact** — the metric and context are inputs *to*
  the fact. (Common mistake: the fact is **not** an input to the metric.)
- **member → fact value** — when the metric is enumerated, a member fills the
  value slot.

### A member plays two roles
1. **Dimension answer** — the *circumstance* of a fact (context).
2. **Fact value** — when the metric is an enumeration, the member *is* the
   reported value.

Same kind of thing (a member from a domain); in one case it describes the
number's circumstances, in the other it *is* the answer.

### Where the domain sits
The fact never stores a domain directly. It is reached **indirectly**:

```
FACT ──► member    ──► domain
FACT ──► dimension ──► domain      ← both must resolve to the SAME domain
```

- A **member** belongs to a domain (`mMember.DomainID`).
- A **dimension** is built on a domain (`mDimension.DomainID`).
- For a valid fact, the member's domain **equals** the dimension's domain — you
  are answering a question with an option from the correct list.

For an **enumerated metric**, the metric itself points at a value domain, and
the reported member must belong to it.

---

## 4. Worked example (from a real XBRL instance)

```xml
<xbrli:context id="c27582">
  <xbrli:entity>
    <xbrli:identifier scheme="http://standards.iso.org/iso/17442">Q1W2E3R4T5Y6U7I8O9P0</xbrli:identifier>
  </xbrli:entity>
  <xbrli:period>
    <xbrli:instant>2025-12-31</xbrli:instant>
  </xbrli:period>
  <xbrli:scenario>
    <xbrldi:explicitMember dimension="s2c_dim:TX">s2c_EL:x28</xbrldi:explicitMember>
    <xbrldi:explicitMember dimension="s2c_dim:VG">s2c_AM:x80</xbrldi:explicitMember>
  </xbrli:scenario>
</xbrli:context>

<s2md_met:mi2004 unitRef="uEUR" decimals="-3" contextRef="c27582">3084000</s2md_met:mi2004>
```

Reading it:

- **Metric** = `mi2004` (what is measured).
- **Value** = `3084000` → with `unitRef="uEUR"` and `decimals="-3"` = **€3,084,000**.
  (The value is *not* the metric — it's the fact's amount.)
- **Dimensions** = `s2c_dim:TX`, `s2c_dim:VG` (the questions).
- **Members** = `s2c_EL:x28` (answers TX), `s2c_AM:x80` (answers VG).
- **Entity** = `Q1W2E3R4T5Y6U7I8O9P0`; **period** = `2025-12-31`.

Note the pairing lives in the attribute + text: `dimension="…"` is the
dimension, the element text is the member.

> **Important:** the instance file records only *dimension = member*. The
> **domain** is *not* in the instance — it lives in the taxonomy / DPM. To
> resolve a domain you must join to the DPM tables.

---

## 5. Cell vs. Fact

A **cell** is a *slot*; a **fact** is a *filled-in slot* in an actual report.

- A **cell** is defined by the **template** (DPM taxonomy): a position
  (subtemplate × row × column) with a fixed meaning (its metric + dimensional
  signature). Cells exist whether or not anyone reports anything.
- A **fact** appears in an **instance document**: an actual value reported for a
  cell, for a given entity and period.

Relationship: **one cell → zero, one, or many facts.**

- **Zero** — nobody reported it this period.
- **One** — the usual case.
- **Many** — the same cell across different **contexts** (different entities,
  periods, or a repeating open/typed dimension — e.g. once per country).

The **context** is what makes each occurrence a distinct fact.

---

## 6. Mapping to the DuckDB schema

| Concept | Table(s) | Notes |
|---|---|---|
| Cell definitions (slots) | `facts` | Keyed by `(subtemplate_code, row_code, column_code)`. Despite the name, this holds *cells* (template definitions), not reported instance values. |
| A cell's dimensional signature | `fact_dimensions` | One row per (cell + dimension + member) — the **actual** dimension/member pairing. |
| Metric catalogue | `metrics` | Currently code + label only (does not yet capture data type or a metric's value domain). |
| Dimension catalogue | `dimensions` | |
| Member catalogue | `dimension_members` | See caveat below. |

Reported **values** (like `3084000`, `s2c_GA:fr`) live in the **XBRL instance**,
not in this DB. The DuckDB describes the *shape* of what can be reported (cells);
the instance carries the *values*.

---

## 7. Known modeling caveat: `dimension_members.dimension_code`

`dimension_members` has `member_code` as its **primary key**, so it records
**exactly one dimension per member**. But a member truly belongs to a
**domain**, and a domain can back **several dimensions** — so the same member
legitimately appears under different dimensions in `fact_dimensions`.

Concretely, `dimension_members.dimension_code` is a **representative** value:
ingestion picks the `MIN(DimensionID)` of the member's domain purely so the
explorer can group members somewhere (see
[`dpm/dpm_source.py`](../dpm/dpm_source.py) — the members query joining
`mMember` → domain → representative `mDimension`). It is **not** an
authoritative binding.

Consequence: for members whose domain backs multiple dimensions, the two tables
disagree — e.g. `s2c_RT:x11`:

| Source | Dimension | Label |
|---|---|---|
| `dimension_members` (catalogue, representative) | `s2c_dim:AB` | Risk scenario |
| `fact_dimensions` (actual usage) | `s2c_dim:DV` | Diversification of risk |

Both are "correct"; the column name oversells what it holds. **`fact_dimensions`
is the source of truth for actual bindings.**

### Options to address it
- **A — Model domains truthfully (most correct).** Add a `domains` table; give
  members and dimensions a `domain_code`. Then "what dimensions can this member
  appear in?" is an honest Member → Domain → Dimensions join. Requires
  ingestion + schema + minor UI changes.
- **B — Rename to stop the lie (cheap).** Rename
  `dimension_members.dimension_code` → `representative_dimension_code` (or store
  `domain_code`) and document it.
- **C — Read actual bindings in the UI.** Where the explorer answers "what
  dimension is this member in," source it from `fact_dimensions` (real usage)
  rather than `dimension_members`.

---

## 8. One-line summary

> A **domain** is a reusable list of **members**; a **dimension** is one question
> that uses such a list; a **member** is one answer. A **fact** combines a
> **metric** (what), a **context** (dimension=member circumstances + entity +
> period), and a **value** (a number, or a member if the metric is enumerated).
> A **cell** is the template slot; a **fact** is that slot filled in a report.
