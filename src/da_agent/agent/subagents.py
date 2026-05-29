"""Subagents used by the end-to-end analyst workflow.

The main agent dispatches these via the `Task` tool during a complex investigation.
Defining them programmatically (AgentDefinition) keeps orchestration in code while the
prompts stay editable.
"""

from __future__ import annotations

from claude_agent_sdk import AgentDefinition

_READONLY = ["Read", "Bash", "Glob", "Grep"]
_READWRITE = ["Read", "Write", "Edit", "Bash", "Glob", "Grep"]


def build_subagents() -> dict[str, AgentDefinition]:
    return {
        "profiler": AgentDefinition(
            description="Profiles spreadsheets: schema, table regions, data quality, "
            "distributions, and candidate cross-sheet relationships. Use first.",
            prompt=(
                "You profile spreadsheet data. Detect distinct table regions per sheet, "
                "infer column types, report cardinality / null rates / ranges, and flag "
                "likely keys and cross-sheet relationships. Do all computation in pandas. "
                "Return a compact structured summary, not raw rows."
            ),
            tools=_READONLY,
            skills=["xlsx"],
        ),
        "analyst": AgentDefinition(
            description="Runs the quantitative analysis: aggregations, joins, statistics, "
            "and hypothesis testing against one or more sheets.",
            prompt=(
                "You perform rigorous data analysis. Given a profiled dataset and a "
                "question or hypothesis, compute the answer in pandas, validate "
                "intermediate results, and report findings with the numbers that support "
                "them. State assumptions and any data-quality caveats."
            ),
            tools=_READONLY,
            skills=["xlsx"],
        ),
        "visualizer": AgentDefinition(
            description="Produces charts, pivot tables, and new summary sheets from "
            "analysis results, registered as a downloadable .xlsx output.",
            prompt=(
                "You turn analysis results into clear deliverables: charts, pivot tables, "
                "and summary sheets. Use the xlsx skill. Keep spreadsheets formula-driven "
                "and free of formula errors. Write to the resolved output target path "
                "supplied by the AskUserQuestion tool_result and report that path."
            ),
            tools=_READWRITE,
            skills=["xlsx"],
        ),
    }
