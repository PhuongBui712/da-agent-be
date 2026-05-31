---
name: data-analysis
description: >
  Professional data analysis skill for the Data Analyst Agent (DAA).
  Trigger this skill ONLY when the user explicitly asks to "analyze", "investigate",
  "find the cause of", "deep dive into", or asks a complex, open-ended analytical
  question that requires multi-step reasoning across data (e.g. "Why did revenue drop?",
  "What's driving churn in Q2?", "Which campaigns actually moved the needle?").
  DO NOT trigger for simple questions ("what's the total revenue?"), data extraction
  ("pull all rows where status = active"), basic aggregation ("average order value by month"),
  or straightforward data manipulation ("add a column", "pivot this table", "merge these sheets").
  If the user's request can be answered with a single query or formula, skip this skill entirely.
  The boundary: if the answer requires hypothesis formation, segmentation, and interpretation — use this skill.
  If it just requires reading or reshaping data — don't.
---

# Data Analysis Skill

## Overview

This skill defines a strict, phased process for professional data analysis. It transforms
a vague analytical question into actionable insights and recommendations delivered as a
formal document. The agent's job is not to produce charts — it is to produce decisions.

**Output format:** confirmed via `AskUserQuestion` in Phase 1 — one of `New .xlsx`,
`New .pptx`, or `New .docx`. The xlsx target is valid when the deliverable is a
structured workbook (model summary, multi-sheet pivot, scenario grid). pptx/docx
remain the default for narrative reports.

---

## When NOT to Use This Skill

Recognize these patterns and handle them directly WITHOUT invoking the full DA process:

| User request pattern             | What to do instead                          |
|----------------------------------|---------------------------------------------|
| "What's the total of column X?"  | Read the file, compute, answer.             |
| "Filter rows where Y > 100"      | Read the file, filter, return result.       |
| "Merge Sheet1 and Sheet2 on ID"  | Read the file, merge, return result.        |
| "Create a pivot table of Z"      | Read the file, pivot, return result.        |
| "Convert this to CSV"            | Read the file, convert, return result.      |
| "Show me the first 10 rows"      | Read the file, display, done.               |

These are data extraction or manipulation tasks — not analysis. Answer them directly and
do NOT enter the phased process, do NOT call `AskUserQuestion`, do NOT call `TodoWrite`.

---

## The Process

The full DA process has 6 phases. Every phase is mandatory and must be executed in order.
Never skip ahead. Never start Phase 3 without completing Phase 2.

```
Phase 1: Business Understanding  ←  YOU ARE HERE FIRST. ALWAYS.
Phase 2: Data Understanding
Phase 3: Data Cleaning & Preparation
Phase 4: Analysis & Hypothesis Testing
Phase 5: Synthesis & Recommendations
Phase 6: Delivery (xlsx / pptx / docx)
```

After completing Phase 1, ALWAYS call `TodoWrite` to materialize Phases 2–6 as a
user-visible plan before proceeding.

---

## Phase 1 — Business Understanding

**Objective:** Fully define the problem and the deliverable before touching the data seriously.

### Step 1.1 — Quick Data Exploration

Before asking the user anything, do a fast scan of the uploaded Excel file(s):

- List all sheet names.
- For each sheet: row count, column names, dtypes, first 5 rows.
- Note obvious data characteristics (date ranges, categorical fields, numeric fields).

This gives you context to ask smarter, data-grounded questions in the next step.
Keep this exploration lightweight — it is for orientation, not analysis.

### Step 1.2 — Ensure the Problem Is Fully Defined (+ Confirm Output)

You MUST have clear answers for these **three** items before moving forward:

1. **Business Question** — What specific question are we answering?
2. **KPIs / Metrics** — Which metrics matter for this question?
3. **Dimensions / Segments** — Along which axes should we slice the data?

You must ALSO have a confirmed **output format**:

4. **Output format** — `New .xlsx`, `New .pptx`, or `New .docx`. Confirm via the
   `AskUserQuestion(Target)` sub-question of the standard output mechanism.

**If the user's request does not clearly define all three problem items, OR has not
clearly declared the output format, call `AskUserQuestion`.** Resolve everything in a
single `AskUserQuestion` call where possible — do not ask one item at a time.

Frame your questions using what you learned in Step 1.1. Reference actual columns,
sheets, and data ranges you found — never ask generic questions.

Example `AskUserQuestion` usage:

```
I scanned the file — it has one sheet with columns: revenue, cost, region, channel, date
(spanning Jan–Jun 2024). Before I analyze, I need to confirm a few things:

1. Focus metric — revenue, profit (revenue − cost), or both?
2. Breakdown — by region, channel, or both?
3. Time window — full range (Jan–Jun 2024) or a specific period?
4. Deliverable target — New .xlsx, New .pptx, or New .docx?
```

Do NOT ask about things you can infer from the data. If a `date` column clearly spans
Jan–Dec 2024 and the user gave no hint of a sub-window, don't ask "what time range?".
But output format is NEVER inferable — always confirm it if the user didn't state it.

### Step 1.3 — Formulate Hypotheses (Agent-Generated)

Once the problem is defined, YOU (the agent) generate the hypotheses — do not ask the
user for them. Generate **at most 3** — the most important and impactful for the problem.
Fewer is fine if only one or two are genuinely worth testing.

Selection criteria:
- **Impact** — Would confirming this explain a large portion of the observed problem?
- **Testability** — Can you actually test it with the available data?
- **Actionability** — If true, does it lead to a concrete recommendation?

Format:

```
H1: [Highest-impact / most likely hypothesis]
H2: [Second hypothesis]
H3: [Alternative / contrarian hypothesis]  (optional — only if genuinely valuable)
```

Example:

```
Business question: "Why did revenue drop 15% in May?"

H1: The drop is concentrated in the Southeast region due to a pricing change on May 3rd.
H2: Paid ad spend decreased in May, reducing traffic across all regions.
H3: A stockout in the top-selling SKU created the revenue gap.
```

### Step 1.4 — Materialize the Plan via `TodoWrite`

After completing Steps 1.1–1.3, call `TodoWrite` to enumerate one entry per remaining
phase (Phase 2 through Phase 6). Each entry should reference the specific data scope,
KPI, hypothesis, and deliverable target from Step 1.2 / 1.3. The frontend renders this
todo list as the user-visible plan; updates flow as you complete each phase. If
`permission_mode=plan` is active in this session, additionally call `ExitPlanMode`
after presenting the plan; otherwise the `TodoWrite` list IS the plan.

Plan template (one todo entry per Phase 2–6 bullet):

```
## Analysis Plan

### Context
- Business Question: [from Step 1.2]
- KPIs: [from Step 1.2]
- Dimensions: [from Step 1.2]
- Hypotheses: [from Step 1.3]
- Deliverable target: [New .xlsx / New .pptx / New .docx — from Step 1.2]

### Phase 2 — Data Understanding
- Profile each sheet: nulls, duplicates, outliers, distributions
- Identify grain of each sheet (1 row = 1 what?)
- Validate key metrics against any known benchmarks
- Map relationships between sheets (join keys)

### Phase 3 — Data Cleaning & Preparation
- Handle missing values in [specific columns]
- Remove duplicates using [specific logic]
- Standardize [dates/currencies/categories]
- Build analysis-ready dataset by joining [Sheet X] with [Sheet Y] on [key]

### Phase 4 — Analysis & Hypothesis Testing
- H1: [specific analysis steps]
- H2: [specific analysis steps]
- H3: [specific analysis steps, if applicable]
- Additional exploratory analysis if hypotheses are inconclusive

### Phase 5 — Synthesis & Recommendations
- Summarize confirmed/rejected hypotheses and why
- Quantify impact of each confirmed root cause
- Formulate 2–4 specific, actionable recommendations

### Phase 6 — Delivery
- Generate the confirmed deliverable (xlsx / pptx / docx) following Phase 6
```

Do NOT proceed to Phase 2 until the todo list is published.

---

## Phase 2 — Data Understanding

**Objective:** Know the data deeply before analyzing it.

### Checklist

1. **Schema exploration** — For every sheet: column names, types, sample values, enums.
2. **Data profiling** — Per column: null rate, duplicate rate, unique count, min/max,
   distribution shape (numerics), top categories (categoricals).
3. **Grain identification** — Define what one row represents in each sheet. Critical.
   Wrong grain = wrong analysis. Document it explicitly.
4. **Cross-sheet relationships** — Identify join keys. Verify uniqueness where expected
   (e.g., confirm `order_id` is actually unique in the orders sheet).
5. **Metric validation** — If the user mentioned known numbers ("revenue was $2M last
   month"), check whether your data reproduces them. Flag discrepancies immediately.

---

## Phase 3 — Data Cleaning & Preparation

**Objective:** Produce an analysis-ready dataset.

### Checklist

1. **Handle missing values** — Decide per column: drop, impute, or flag. Document each
   decision and its reasoning.
2. **Remove duplicates** — Use appropriate dedup logic (which columns define uniqueness).
   Log how many rows were removed.
3. **Standardize** — Dates (format + timezone), currency (single unit), categories
   (merge synonyms like "NY" / "New York").
4. **Build the analytical dataset** — Join sheets as needed; create derived columns
   (profit = revenue − cost, cohort = signup month). Verify row counts after every join —
   unexpected inflation signals a grain mismatch.

---

## Phase 4 — Analysis & Hypothesis Testing

**Objective:** Test each hypothesis with evidence, not intuition.

### For each hypothesis:

1. **State it clearly** — what you expect to find.
2. **Define the test** — what analysis would confirm or reject it.
3. **Execute** — segment, compare, compute.
4. **State the result** — confirmed, rejected, or inconclusive, with evidence.

### Techniques (use as appropriate):

| Technique             | When to use                                      |
|-----------------------|--------------------------------------------------|
| Segment comparison    | Compare a metric across groups (region, cohort)  |
| Trend analysis        | Identify changes over time                       |
| Funnel analysis       | Find drop-off points in a process                |
| Cohort analysis       | Compare groups defined by time                   |
| Correlation analysis  | Test relationships between variables             |
| Contribution analysis | Decompose a metric change into its components    |
| Outlier detection     | Identify anomalies skewing results               |

### Rules:

- Always compare against a baseline (prior period, benchmark, control group).
- Distinguish a real pattern from noise; for experiments, mind sample size, peeking,
  and multiple comparisons.
- Correlation ≠ causation. When you can't randomize, be explicit about confounders.
- If all hypotheses are rejected, do not force a narrative. Say so and identify new
  directions from what you learned.

---

## Phase 5 — Synthesis & Recommendations

**Objective:** Turn findings into decisions.

1. **Root cause summary** — Which hypotheses were confirmed? What was the primary driver?
   Quantify impact ("Southeast accounts for 60% of the revenue decline").
2. **Recommendations** — 2–4 specific, actionable next steps. Each must tie to a finding,
   be concrete ("Pause Campaign X", not "Improve marketing"), and include expected impact
   where possible.
3. **Risks and caveats** — Assumptions made, data limitations, what could invalidate the
   conclusions.

---

## Phase 6 — Delivery

**Objective:** Communicate results clearly as a formal deliverable.

The deliverable is the confirmed target from Phase 1 — never a loose chart dump or a
chat-only answer. To generate the file, USE THE CORRESPONDING SKILL: `New .xlsx` →
**xlsx** skill; `New .pptx` → **pptx** skill; `New .docx` → **docx** skill. Write the
deliverable to the absolute `resolved_target_path` returned by the `AskUserQuestion`
tool result — do not invent a path.

Read that skill's `SKILL.md` before generating, and follow its conventions for structure,
styling, and file creation.

### Executive Summary (lead with this in any format):

```
[METRIC] changed by [AMOUNT] over [PERIOD] due to:
- [Root cause 1] — [quantified impact]
- [Root cause 2] — [quantified impact]

Recommendations:
1. [Action] — expected to [impact]
2. [Action] — expected to [impact]

Key caveat: [most important limitation]
```

### If delivering a `New .pptx` — recommended slide structure:

```
Slide 1  — Executive Summary
Slide 2  — Problem Statement & Scope
Slide 3  — Methodology & Data
Slides 4–8 — Findings (one message per slide, hypothesis-driven)
Slide 9  — Recommendations
Slide 10 — Next Steps & Caveats
```

### If delivering a `New .docx` — recommended section structure:

```
1. Executive Summary
2. Problem Statement & Objectives
3. Data & Methodology
4. Findings (per hypothesis, with evidence)
5. Recommendations
6. Risks, Assumptions & Caveats
7. Appendix (metric definitions, cleaning decisions)
```

### If delivering a `New .xlsx` — recommended workbook structure:

```
Sheet 1 — Executive Summary (KPIs, headline numbers, recommendations)
Sheet 2 — Methodology & Assumptions
Sheet 3+ — One sheet per hypothesis (evidence tables, pivots)
Final sheet — Appendix (cleaning log, metric definitions)
```

### Visualization principles (apply in any format):

- One chart = one message. Highlight the insight; don't make the reader decode it.
- Clarity over beauty. No decorative charts — every chart supports a finding.
- Label axes, units, and time ranges. Annotate the key takeaway directly on the chart.

---

## Reading Excel Files — Technical Notes

- Always check for multiple sheets — they often represent different entities or periods.
- Watch for merged cells, multi-row headers, or metadata rows at the top of sheets;
  clean these before processing.
- Beware Excel quirks: dates stored as numbers, trailing whitespace in categories,
  formulas vs. values, hidden sheets.
- If a sheet has no clear header row, infer it from the data or ask the user.

---

## Critical Reminders

1. **Gate the trigger.** Only run this process for explicit analysis or complex,
   open-ended questions. Simple lookups, extraction, and manipulation are answered
   directly — no phases, no `AskUserQuestion`, no `TodoWrite` plan.
2. **Never skip Phase 1.** A perfect analysis of the wrong question is worthless.
3. **Confirm the output in Phase 1.** Output target (`New .xlsx` / `New .pptx` /
   `New .docx`) is never inferable — confirm it via `AskUserQuestion` if the user
   didn't declare it.
4. **Always materialize the plan via `TodoWrite` after Phase 1**, with one entry per
   Phase 2–6. The plan must strictly follow Phases 2–6.
5. **Hypotheses are agent-generated, max 3.** Every Phase 4 step tests a specific one.
6. **Output decisions, not dashboards.** The deliverable is "here's what to do and why".
7. **Grain awareness.** Confirm the grain of each sheet before any join or aggregation.
8. **Document everything** — cleaning decisions, assumptions, caveats — for reproducibility.

---

## Output mechanism alignment (§8.2)

This skill operates inside the standard output mechanism. Every deliverable file
the user can download MUST be produced by:

1. Calling `AskUserQuestion` with TWO sub-questions (`Target`, `Source`) — except
   when explicit user wording already chose both.
2. Reading `resolved_target_path` and `resolved_target_kind` from the
   tool_result.
3. Writing the file to `resolved_target_path` verbatim.

For `.pptx` / `.docx` targets the resolved kind is always `standalone` and the
filename defaults to `output.pptx` / `output.docx` under
`outputs/<output_id>/`. The Source sub-question's answer is `N/A` for these
targets.

Never write to a "scratch" directory. Never invent a path. Never overwrite
`raw.xlsx` (Golden Rule 4).
