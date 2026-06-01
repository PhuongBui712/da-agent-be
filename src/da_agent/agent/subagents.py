"""Subagents used by the end-to-end analyst workflow.

The main agent dispatches these via the `Agent` tool during a complex investigation.
Defining them programmatically (AgentDefinition) keeps orchestration in code while the
prompts stay editable.

The `kb_profiler` subagent is intentionally NOT included here. It is invoked
only by the BE ingestion pipeline (see `da_agent.ingestion.profiler`) under
its own `ClaudeAgentOptions` so the in-session model never accidentally
delegates to opus during analysis turns. Per the project mandate: ingest
uses opus; in-session analysis stays on sonnet.
"""

from __future__ import annotations

from claude_agent_sdk import AgentDefinition

from ..config import Settings

_READONLY = ["Read", "Bash", "Glob", "Grep"]
_READWRITE = ["Read", "Write", "Edit", "Bash", "Glob", "Grep"]


def build_subagents(settings: Settings | None = None) -> dict[str, AgentDefinition]:
    # `settings` is accepted for forward-compat (the kb_profiler factory takes
    # one) but is unused by the in-session subagents below — they inherit the
    # main agent's model.
    del settings
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
                "You execute Phase 3 (data preparation) and Phase 4 (hypothesis testing) "
                "of the data-analysis workflow.\n"
                "\n"
                "Phase 3 — produce an analysis-ready dataset:\n"
                "  - Handle missing values per column (drop / impute / flag), document each "
                "    decision.\n"
                "  - Remove duplicates with explicit dedup keys; log how many rows were "
                "    removed.\n"
                "  - Standardize dates (format + timezone), currencies (single unit), and "
                "    categorical synonyms.\n"
                "  - Build the analytical dataset by joining sheets on the keys identified "
                "    in profiling. Verify row counts after every join — unexpected inflation "
                "    signals a grain mismatch and you must stop to investigate.\n"
                "\n"
                "Phase 4 — test each hypothesis:\n"
                "  - State the hypothesis, define the test (segment compare, trend, funnel, "
                "    cohort, correlation, contribution, or outlier), execute in pandas, and "
                "    state the verdict (confirmed / rejected / inconclusive) with the "
                "    numbers that support it.\n"
                "  - Always compare against a baseline (prior period, benchmark, control). "
                "    Distinguish real signal from noise.\n"
                "  - State assumptions and any data-quality caveats.\n"
                "\n"
                "Return a compact structured summary with derived dataset shape, cleaning "
                "decisions, and per-hypothesis verdicts. Do not return raw rows."
            ),
            tools=_READONLY,
            skills=["xlsx"],
        ),
        "reporter": AgentDefinition(
            description="Produces the Phase 6 deliverable: writes the final analytical "
            "artifact (.xlsx workbook, .pptx deck, or .docx report) to the resolved "
            "target path.",
            prompt=(
                "You produce the Phase 6 deliverable for the data-analysis workflow. "
                "Pick the right SDK skill based on `resolved_target_kind` and the file "
                "extension of `resolved_target_path` returned in the AskUserQuestion "
                "tool_result:\n"
                "  - `.xlsx` / `.xlsm` → use the `xlsx` skill (formula-driven, zero "
                "    formula errors, professional formatting per skill rules).\n"
                "  - `.pptx`           → use the `pptx` skill (executive-summary slide "
                "    first, one message per slide, follow the skill's slide structure).\n"
                "  - `.docx`           → use the `docx` skill (lead with executive "
                "    summary, follow the skill's section structure).\n"
                "Write to `resolved_target_path` verbatim — never invent a path or "
                "use a scratch directory. State the on-disk path in your final reply. "
                "Spreadsheets stay formula-driven and free of #REF/#DIV/0 errors. "
                "Slides and docs lead with the answer; supporting detail follows."
            ),
            tools=_READWRITE,
            skills=["xlsx", "pptx", "docx"],
        ),
    }
