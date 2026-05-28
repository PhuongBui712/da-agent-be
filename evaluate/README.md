# DAA Evaluation Suite

Complete evaluation resources for the **Data Analyst Agent (DAA)** — ready to run against any Claude Agent SDK implementation.

## Directory Structure

```
daa_eval/
├── evals.json                          # Master task definitions (25 tasks across 4 suites)
├── config/
│   ├── eval_config.yaml                # Global settings (model, trials, thresholds)
│   └── grader_rubrics/
│       ├── analysis_revenue_drop.md    # Rubric for synthetic revenue drop task
│       └── analysis_general_financial.md  # Rubric for real financial analysis tasks
├── test_data/
│   ├── real/                           # 4 real financial review Excel files
│   │   ├── 240826-UOB_Financial_Review.xlsx
│   │   ├── 250304-UBS_Review_4Q2024__SY_.xlsx
│   │   ├── Alibaba_4Q2025_Review_-_16May2025.xlsx
│   │   └── Straits_Trading_FY2023_Result_-20240322.xlsx
│   └── synthetic/                      # 3 generated test files with known ground truth
│       ├── syn_revenue_drop.xlsx       # Orders + Customers, Q3 drop planted
│       ├── syn_messy_data.xlsx         # Duplicates, nulls, inconsistent labels
│       └── syn_simple_summary.xlsx     # Pre-aggregated monthly + regional data
├── references/
│   └── reference_values.json           # 27 pre-computed ground truth values
├── graders/
│   ├── __init__.py
│   ├── code_graders.py                 # 6 deterministic grader implementations
│   └── model_graders.py               # 4 LLM-as-judge grader implementations
└── scripts/
    └── run_eval.py                     # Main evaluation runner
```

## Quick Start

### 1. Implement the Agent Interface

Open `scripts/run_eval.py` and replace the `run_agent()` placeholder with your actual DAA invocation:

```python
def run_agent(excel_file: str, user_prompt: str, config: dict) -> dict:
    from your_agent_sdk import DAAAgent
    agent = DAAAgent(model="claude-sonnet-4-20250514")
    result = agent.run(input_file=excel_file, prompt=user_prompt, ...)
    return {
        "response_text": result.final_response,
        "transcript": result.messages,        # Full message array
        "final_answer": result.extracted_answer,
        "files_produced": result.output_files,
        "metadata": {"total_tokens": ..., "n_turns": ...}
    }
```

### 2. Validate Config

```bash
python scripts/run_eval.py --dry-run
```

### 3. Run Evaluations

```bash
# All suites
python scripts/run_eval.py

# Specific suite
python scripts/run_eval.py --suite suite_a_operations

# Specific task
python scripts/run_eval.py --task analysis_revenue_drop_001

# Custom trial count
python scripts/run_eval.py --n-trials 5
```

### 4. Read Results

Results go to `results/<timestamp>/`:
- `raw_results.json` — complete grader outputs per trial
- `eval_report.md` — summary table with pass@1, pass@3, avg scores

---

## Suite Overview

### Suite A — Data Operations (10 tasks)

Tests extraction, aggregation, calculation, and manipulation. The agent should answer directly **without** triggering the full DA process (`EnterPlanMode` must NOT appear in transcript).

| Task ID | File | What It Tests |
|---------|------|--------------|
| ops_extract_001 | UOB | Extract single value (NII FY2023) |
| ops_extract_002 | UBS | Extract single value (NFCI FY2024) |
| ops_calc_003 | UBS | Calculate YoY growth rate |
| ops_calc_004 | Alibaba | Calculate YoY growth rate |
| ops_extract_005 | Straits Trading | Extract from specific P&L line |
| ops_agg_006 | Synthetic | Sum across all rows |
| ops_filter_007 | Synthetic | Filter and count |
| ops_multisheet_008 | UOB | List all sheet names |
| ops_compare_009 | UOB | Compare two values |
| ops_cleaning_010 | Synthetic (messy) | Count duplicates and nulls |

**Graders:** code_value_match, code_contains_all, code_transcript (assert EnterPlanMode absent)

### Suite B — Data Analysis (5 tasks)

Tests the full 6-phase DA process. The agent **must** trigger `EnterPlanMode` and follow all phases.

| Task ID | File | What It Tests |
|---------|------|--------------|
| analysis_revenue_drop_001 | Synthetic | Root cause of Q3 drop (ground truth planted) |
| analysis_ubs_post_cs_002 | UBS | Post-acquisition profitability analysis |
| analysis_baba_segments_003 | Alibaba | Segment-level growth decomposition |
| analysis_st_loss_004 | Straits Trading | Profit-to-loss swing investigation |
| analysis_uob_asset_quality_005 | UOB | Multi-year NPL trend analysis |

**Graders:** code_phase_gate + model_rubric (5 dimensions scored 1–5) + model_assertion

### Suite C — Routing (10 tasks)

Tests whether the agent correctly chooses between operations mode and analysis mode. Balanced 50/50.

| Route | Count | What It Tests |
|-------|-------|--------------|
| Operations (should NOT analyze) | 5 | Simple extraction/calculation questions |
| Analysis (should analyze) | 5 | Open-ended, complex analytical questions |

**Graders:** code_transcript (assert_present or assert_absent for EnterPlanMode)

### Suite D — Regression (0 tasks initially)

Populated over time as tasks from Suites A–C achieve consistent high pass rates and "graduate" into regression protection.

---

## Grader Reference

### Code-Based Graders

| Grader | Input | Output |
|--------|-------|--------|
| `code_value_match` | Agent output + expected value + tolerance | Binary pass/fail + numeric diff |
| `code_contains_all` | Agent text + list of required strings | Fraction found + missing list |
| `code_transcript` | Full transcript + required/forbidden markers | Binary per marker |
| `code_phase_gate` | Transcript + output, checks 6 phases + EnterPlanMode + hypothesis count | Composite score |
| `code_dataframe_compare` | Two DataFrames + tolerance | Column match + row count + value accuracy |

### Model-Based Graders

| Grader | What It Does | Runs |
|--------|-------------|------|
| `model_rubric` | Scores 5 dimensions (1–5) against a structured rubric | 3x, median |
| `model_assertion` | Checks N natural-language claims against output | 3x, majority vote |
| `model_hallucination` | Detects fabricated data/columns/numbers | 1x |
| `model_pairwise` | Compares two outputs, picks winner | 1x (position-randomized) |

---

## Scoring

### Per-Task

```
task_score = Σ (weight_i × grader_i_score) / Σ weight_i
task_passed = task_score ≥ 0.6 AND all critical graders pass
```

Default weights for Suite B analysis tasks:

| Dimension | Weight |
|-----------|--------|
| Phase completion (code) | 0.15 |
| Root cause identification (model) | 0.25 |
| Insight quality (model) | 0.20 |
| Recommendation specificity (model) | 0.20 |
| Quantitative rigor (model) | 0.10 |
| Executive summary (model) | 0.10 |

### Per-Suite

- **pass@1**: % of tasks passing on first trial (primary metric)
- **pass@3**: % of tasks passing at least once in 3 trials (secondary)

---

## Extending the Suite

### Adding a New Task

1. Add a task object to the appropriate suite in `evals.json`
2. If using synthetic data, add the generation logic and reference values
3. Choose graders from the available set
4. Run `--dry-run` to validate
5. Run the task: `--task your_new_task_id`

### Adding a New Grader

1. Implement in `graders/code_graders.py` or `graders/model_graders.py`
2. Register in the `GRADER_REGISTRY` (for code graders)
3. Add dispatch logic in `scripts/run_eval.py::run_graders()`

### Graduating Tasks to Regression

When a task achieves >95% pass@3 across 5+ evaluation runs:
1. Copy the task definition to `suite_d_regression`
2. Tighten the pass threshold (e.g., 0.8 instead of 0.6)
3. Continue running it as a regression guard
