# Rubric: Revenue Drop Root Cause Analysis

You are evaluating a Data Analyst Agent's output on a revenue analysis task.

## Context

The agent was given a synthetic sales dataset (Orders + Customers sheets) where revenue dropped significantly in Q3 2024 compared to Q2 2024.

**Known ground truth (planted in the data):**
1. The South region experienced a ~69% revenue decline in Q3 vs Q2 (revenue multiplied by 0.45 for South+Q3 orders)
2. The Paid channel experienced a ~30% decline in Q3 vs Q2 (revenue multiplied by 0.70 for Paid+Q3 orders)
3. Overall Q3 vs Q2 decline was approximately 18.9%

## Scoring Dimensions

Score each dimension on a 1–5 scale using the anchors below.

### 1. root_cause_identification

| Score | Criteria |
|-------|----------|
| 1 | Did not identify any root cause. Just restated that revenue dropped. |
| 2 | Identified one of the two root causes (South region OR Paid channel) but missed the other entirely. |
| 3 | Identified one root cause clearly and hinted at the second, OR identified both but without quantification. |
| 4 | Identified both root causes (South region AND Paid channel) with approximate quantification. |
| 5 | Identified both root causes with precise quantification and correctly characterized the interaction (South+Paid compounding effect). |

### 2. insight_quality

| Score | Criteria |
|-------|----------|
| 1 | Insights are trivial restatements of the data ("revenue went down in Q3"). |
| 2 | Some segmentation was done but insights are surface-level. |
| 3 | Non-obvious patterns identified. Analysis goes beyond top-level aggregation (e.g., region × time breakdown). |
| 4 | Multi-dimensional analysis with cross-cutting insights (region × channel × time). Evidence-backed. |
| 5 | Exceptional: discovered the interaction effect, explored customer-level patterns, or identified additional nuances not in the ground truth. |

### 3. recommendation_specificity

| Score | Criteria |
|-------|----------|
| 1 | No recommendations, or only generic advice ("improve sales"). |
| 2 | Recommendations exist but are vague ("focus on the South region"). |
| 3 | At least 2 recommendations that tie to specific findings and name concrete actions. |
| 4 | 3+ recommendations, each tied to a finding, with expected impact estimates or prioritization. |
| 5 | Recommendations include short-term fixes AND long-term strategy, with clear prioritization and measurable success criteria. |

### 4. quantitative_rigor

| Score | Criteria |
|-------|----------|
| 1 | No numbers cited. All claims are qualitative. |
| 2 | Some numbers but they are wrong or inconsistent with the data. |
| 3 | Key numbers are correct: overall drop %, and at least one segment breakdown. Compared against a baseline (Q2 or prior quarters). |
| 4 | Comprehensive quantification: overall drop, segment-level drops, contribution analysis (how much each segment contributed to the total drop). |
| 5 | Statistical rigor: tested whether differences are significant, provided confidence intervals or variance context, decomposed the drop mathematically. |

### 5. executive_summary

| Score | Criteria |
|-------|----------|
| 1 | No summary, or summary is longer than the analysis. |
| 2 | Summary exists but buries the lead or is overly technical. |
| 3 | Concise summary (<200 words) that leads with the key finding and includes a recommendation. Understandable by a non-technical reader. |
| 4 | Excellent summary: structured as finding → impact → recommendation. Uses concrete numbers. |
| 5 | Boardroom-ready: could be sent directly to a C-level stakeholder with zero editing. Clear, quantified, actionable. |

## Output Format

Return ONLY valid JSON, no markdown formatting:

{"root_cause_identification": <1-5>, "insight_quality": <1-5>, "recommendation_specificity": <1-5>, "quantitative_rigor": <1-5>, "executive_summary": <1-5>, "reasoning": "<brief explanation of each score>"}