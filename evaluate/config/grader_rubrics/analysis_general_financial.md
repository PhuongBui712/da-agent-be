# Rubric: General Financial Analysis

You are evaluating a Data Analyst Agent's output on a financial analysis task.

## Context

The agent was given a real financial review Excel file and asked to analyze specific aspects of the company's performance. The task description and ground truth key insights will be provided alongside this rubric.

## Scoring Dimensions

Score each dimension on a 1–5 scale.

### 1. root_cause_identification

| Score | Criteria |
|-------|----------|
| 1 | Did not identify any meaningful drivers. Output is purely descriptive. |
| 2 | Identified 1 key driver but missed major ones from the ground truth. |
| 3 | Identified 2+ key drivers, at least 1 matching the ground truth closely. |
| 4 | Identified most key drivers from the ground truth with supporting evidence from the data. |
| 5 | Identified all ground truth drivers plus additional valid insights from the data. Distinguished between one-time and structural factors. |

### 2. insight_quality

| Score | Criteria |
|-------|----------|
| 1 | Insights are trivial restatements ("revenue went up/down"). |
| 2 | Some segmentation but insights don't go beyond what's directly visible in the table. |
| 3 | Non-obvious patterns identified. Cross-referenced multiple sheets (P&L + BS, or P&L + Segments). |
| 4 | Multi-dimensional analysis: time trends × segments × ratios. Connected financial statement items meaningfully. |
| 5 | Exceptional: identified structural shifts, connected balance sheet health to P&L performance, spotted anomalies, or surfaced risks not apparent from surface-level reading. |

### 3. recommendation_specificity

| Score | Criteria |
|-------|----------|
| 1 | No recommendations or only generic platitudes. |
| 2 | Recommendations exist but don't tie to specific findings. |
| 3 | 2+ recommendations that reference specific findings. Actions are named. |
| 4 | Recommendations are prioritized, tied to findings, and include expected impact or monitoring metrics. |
| 5 | Strategic-quality recommendations: short-term + long-term, specific KPIs to track, and acknowledgment of trade-offs. |

### 4. quantitative_rigor

| Score | Criteria |
|-------|----------|
| 1 | No numbers. All qualitative. |
| 2 | Numbers cited but with errors or without context (no baselines, no comparisons). |
| 3 | Key metrics correctly stated with YoY or period comparisons. At least one ratio or derived metric. |
| 4 | Comprehensive: multiple metrics, contribution analysis, margin analysis, trend characterization with correct growth rates. |
| 5 | Forensic-level: decomposition of changes, normalization for one-offs, cross-validation of metrics across sheets, and appropriate caveats on data quality. |

### 5. executive_summary

| Score | Criteria |
|-------|----------|
| 1 | Absent or longer than the analysis body. |
| 2 | Present but unfocused — doesn't lead with the key finding. |
| 3 | Concise (<200 words), leads with the main finding, includes a recommendation. |
| 4 | Well-structured: finding → quantified impact → action. Non-technical language. |
| 5 | Could be sent directly to a board or investment committee with zero editing. |

## Output Format

Return ONLY valid JSON, no markdown formatting:

{"root_cause_identification": <1-5>, "insight_quality": <1-5>, "recommendation_specificity": <1-5>, "quantitative_rigor": <1-5>, "executive_summary": <1-5>, "reasoning": "<brief explanation of each score>"}