"""CLI entrypoint.

`da-agent` (or `da-agent chat`)  -> interactive multi-turn REPL backed by the SDK.
`da-agent demo`                  -> render a scripted session through the same UI,
                                    no API key required (great for seeing the TUI).
"""
from __future__ import annotations

import argparse
import asyncio

from prompt_toolkit import PromptSession

from .agent.core import AgentRunner
from .agent.events import Option, Question, QuestionRequest
from .config import Settings
from .ui.console import ConsoleAgentUI

_BANNER = "DA-Agent · Excel data analyst  ·  /exit to quit, /plan to re-plan next turn"


# --------------------------------------------------------------------------- #
# interactive chat
# --------------------------------------------------------------------------- #
async def run_chat(settings: Settings) -> None:
    ui = ConsoleAgentUI()
    ui.console.print(_BANNER, style="grey62")
    ui.console.print(f"KB: {settings.kb_dir}   workspace: {settings.workspace_dir}", style="grey62")

    session: PromptSession = PromptSession()
    async with AgentRunner(ui, settings) as runner:
        while True:
            try:
                text = (await session.prompt_async("\n› ")).strip()
            except (EOFError, KeyboardInterrupt):
                break
            if not text:
                continue
            if text in {"/exit", "/quit"}:
                break
            if text == "/plan":
                await runner.set_plan_mode()
                ui.console.print("· plan mode on for the next turn", style="grey62")
                continue
            try:
                await runner.send(text, echo_prompt=False)
            except Exception as exc:  # noqa: BLE001 - surface any SDK/runtime error to the user
                ui.on_error(str(exc))
    ui.console.print("\nbye", style="grey62")


# --------------------------------------------------------------------------- #
# scripted demo (no API key)
# --------------------------------------------------------------------------- #
async def run_demo(settings: Settings) -> None:
    ui = ConsoleAgentUI()
    ui.console.print(_BANNER + "   [demo]", style="grey62")

    ui.on_user_prompt("Analyze sales.xlsx and surface the key trends")
    ui.on_thinking(
        "The user wants an open-ended analysis. I should inspect the file first, then "
        "propose a plan before doing the heavy work."
    )
    ui.on_tool_use("Bash", {"command": "extract-text /data/sales.xlsx | head -40"})
    ui.on_tool_result(
        "## Sheet: Orders\norder_id\tcustomer_id\tregion\tamount\tdate\n"
        "1\t8821\tNorth\t420.50\t2024-01-03\n2\t8822\tSouth\t180.00\t2024-01-03\n"
        "## Sheet: Customers\ncustomer_id\tname\tsegment\tsignup_date"
    )
    ui.on_text("Two sheets: Orders (48k rows) and Customers, joinable on customer_id.")

    decision = await ui.approve_plan(
        "1. Profile both sheets (types, ranges, data quality)\n"
        "2. Join Orders→Customers; aggregate revenue by segment and month\n"
        "3. Test whether the North region's Q2 dip is significant\n"
        "4. Produce a summary sheet + charts in the workspace"
    )
    if not decision.approved:
        ui.on_text(f"Revising based on feedback: {decision.feedback}")

    ui.on_tool_use("Task", {"subagent_type": "profiler", "description": "profile both sheets"})
    ui.on_tool_result("Orders: 48,211 rows, 0.2% null customer_id. Customers: 5,140 rows, PK clean.", depth=1)
    ui.on_tool_use("Task", {"subagent_type": "analyst", "description": "revenue by segment/month"})
    ui.on_tool_result("Enterprise segment = 61% of revenue; North Q2 down 18% (p=0.02).", depth=1)

    resp = await ui.ask_question(
        QuestionRequest(
            questions=[
                Question(
                    question="Where should I put the analysis output?",
                    header="Output",
                    options=[
                        Option("New .xlsx in workspace", "A standalone file you can download"),
                        Option("New sheet in sales.xlsx", "Append to the source workbook"),
                        Option("Edit in place", "Modify the existing sheet"),
                    ],
                    multi_select=False,
                    allow_other=True,
                )
            ]
        )
    )
    ui.on_text(f"Got it — {resp.to_model_text()}.")
    ui.on_tool_use("Skill", {"name": "xlsx"})
    ui.on_tool_use("Write", {"file_path": str(settings.workspace_dir / "sales_insights.xlsx")})
    ui.on_tool_result("Wrote sales_insights.xlsx (3 sheets, 2 charts, 0 formula errors).")
    ui.on_text(
        "Done. Key findings: Enterprise drives 61% of revenue; the North region's Q2 dip is "
        "statistically significant. Output: workspace/sales_insights.xlsx"
    )
    ui.on_result(turns=7, cost_usd=0.0421, duration_s=12.3)


# --------------------------------------------------------------------------- #
# arg parsing
# --------------------------------------------------------------------------- #
def _build_settings(args: argparse.Namespace) -> Settings:
    s = Settings()
    if args.no_plan:
        s.plan_first = False
    if args.no_thinking:
        s.show_thinking = False
    if args.model:
        s.model = args.model
    return s


def main() -> None:
    parser = argparse.ArgumentParser(prog="da-agent", description="Excel data-analyst agent (CLI)")
    parser.add_argument("mode", nargs="?", default="chat", choices=["chat", "demo"])
    parser.add_argument("--no-plan", action="store_true", help="skip plan-mode on session start")
    parser.add_argument("--no-thinking", action="store_true", help="hide extended-thinking blocks")
    parser.add_argument("--model", default=None, help="override the model id")
    args = parser.parse_args()

    settings = _build_settings(args)
    runner = run_demo if args.mode == "demo" else run_chat
    try:
        asyncio.run(runner(settings))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
