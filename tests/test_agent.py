"""Tests for the agent core seams. No API key / network required."""

from __future__ import annotations

import pytest
from claude_agent_sdk import (
    PermissionResultAllow,
    PermissionResultDeny,
    TextBlock,
    ThinkingBlock,
    ToolResultBlock,
    ToolUseBlock,
)

from da_agent.agent.core import AgentRunner
from da_agent.agent.events import (
    Answer,
    PlanDecision,
    PlanVerdict,
    Question,
    QuestionRequest,
    QuestionResponse,
    TodoSnapshot,
)
from da_agent.agent.permissions import make_can_use_tool
from da_agent.config import Settings


class FakeUI:
    """Records render calls; returns canned answers for interaction."""

    def __init__(self, question_response=None, plan_decision=None):
        self.calls: list[tuple] = []
        self._qr = question_response
        self._pd = plan_decision
        self.todo_snapshots: list[TodoSnapshot] = []

    def _rec(self, *a):
        self.calls.append(a)

    def on_user_prompt(self, t):
        self._rec("prompt", t)

    def on_thinking(self, t):
        self._rec("thinking", t)

    def on_text(self, t):
        self._rec("text", t)

    def on_tool_use(self, n, i, *, depth=0):
        self._rec("tool_use", n, depth)

    def on_tool_result(self, s, *, is_error=False, depth=0):
        self._rec("tool_result", is_error, depth)

    def on_system(self, st, d):
        self._rec("system", st)

    def on_result(self, *, turns, cost_usd, duration_s):
        self._rec("result", turns)

    def on_error(self, m):
        self._rec("error", m)

    def on_todos(self, snapshot):
        self.todo_snapshots.append(snapshot)

    def begin_wait(self, label="Working"):
        pass

    def end_wait(self):
        pass

    async def ask_question(self, request):
        self._rec("ask_question", len(request.questions))
        return self._qr or QuestionResponse(answers=[])

    async def approve_plan(self, plan):
        return self._pd


def _runner(ui=None):
    s = Settings()
    s.show_thinking = True
    return AgentRunner(ui or FakeUI(), s)


# --------------------------------------------------------------------------- #
def test_options_assembly():
    opts = _runner()._build_options()
    assert opts.skills == ["xlsx"]
    assert "project" in opts.setting_sources
    assert opts.permission_mode == "plan"
    # AskUserQuestion is the built-in tool we route through can_use_tool now —
    # there is no longer a custom MCP server registered for it.
    assert "AskUserQuestion" in opts.allowed_tools
    assert not opts.mcp_servers
    assert set(opts.agents) == {"profiler", "analyst", "visualizer"}
    assert callable(opts.can_use_tool)
    assert opts.env["CLAUDE_CONFIG_DIR"].endswith("sessions")


def test_render_blocks_and_filtering():
    ui = FakeUI()
    r = _runner(ui)
    r._render_block(ThinkingBlock(thinking="hmm", signature="s"), 0)
    r._render_block(TextBlock(text="hello"), 0)
    r._render_block(ToolUseBlock(id="t1", name="Bash", input={"command": "ls"}), 0)
    # Built-in interactive tools must be filtered out of the normal step stream.
    r._render_block(ToolUseBlock(id="t2", name="ExitPlanMode", input={"plan": "x"}), 0)
    r._render_block(
        ToolUseBlock(id="t3", name="AskUserQuestion", input={"questions": []}), 0
    )
    # Todo tools also bypass the normal renderer; they update the overlay instead.
    r._render_block(
        ToolUseBlock(
            id="t4", name="TaskUpdate", input={"taskId": "42", "status": "completed"}
        ),
        0,
    )
    kinds = [c[0] for c in ui.calls]
    assert kinds.count("thinking") == 1
    assert kinds.count("text") == 1
    assert kinds.count("tool_use") == 1  # only Bash, none of the interactive/todo tools


def test_render_tool_result_depth_and_error():
    ui = FakeUI()
    r = _runner(ui)
    r._render_tool_result(
        ToolResultBlock(tool_use_id="t", content="oops", is_error=True), depth=1
    )
    assert ui.calls[-1] == ("tool_result", True, 1)


def test_tool_result_list_content():
    ui = FakeUI()
    r = _runner(ui)
    block = ToolResultBlock(
        tool_use_id="t",
        content=[{"type": "text", "text": "a"}, {"type": "text", "text": "b"}],
        is_error=False,
    )
    r._render_tool_result(block, depth=0)
    assert ui.calls[-1] == ("tool_result", False, 0)


# --------------------------------------------------------------------------- #
# can_use_tool routing — AskUserQuestion (built-in) + ExitPlanMode + everything else
# --------------------------------------------------------------------------- #
def _make_callbacks(qr: QuestionResponse | None = None, pd: PlanDecision | None = None):
    state = {"approved": False, "questions_asked": []}

    async def on_approved():
        state["approved"] = True

    async def ask_plan(plan):
        return pd or PlanDecision(verdict=PlanVerdict.APPROVE)

    async def ask_question(request: QuestionRequest):
        state["questions_asked"].append(request)
        return qr or QuestionResponse(answers=[])

    return state, ask_plan, on_approved, ask_question


@pytest.mark.asyncio
async def test_ask_user_question_routes_via_can_use_tool():
    qr = QuestionResponse(
        answers=[
            Answer(header="Output", selected=["New .xlsx in workspace"]),
            Answer(header="Format", selected=["CSV"], other_text="parquet"),
        ]
    )
    state, ask_plan, on_approved, ask_question = _make_callbacks(qr=qr)
    can_use = make_can_use_tool(ask_plan, on_approved, ask_question)

    tool_input = {
        "questions": [
            {
                "question": "Where should output go?",
                "header": "Output",
                "options": [
                    {"label": "New .xlsx in workspace"},
                    {"label": "Edit in place"},
                ],
            },
            {
                "question": "Output format?",
                "header": "Format",
                "options": [{"label": "CSV"}],
            },
        ]
    }
    result = await can_use("AskUserQuestion", tool_input, None)
    assert isinstance(result, PermissionResultAllow)
    # Each question is round-tripped, with the user's answer indexed by its question text.
    assert result.updated_input["questions"] == tool_input["questions"]
    answers = result.updated_input["answers"]
    assert answers["Where should output go?"] == "New .xlsx in workspace"
    assert answers["Output format?"] == "CSV, parquet"
    assert len(state["questions_asked"]) == 1


@pytest.mark.asyncio
async def test_ask_user_question_handles_empty_response():
    state, ask_plan, on_approved, ask_question = _make_callbacks(
        qr=QuestionResponse(answers=[])
    )
    can_use = make_can_use_tool(ask_plan, on_approved, ask_question)

    tool_input = {
        "questions": [{"question": "Q?", "header": "H", "options": [{"label": "x"}]}]
    }
    result = await can_use("AskUserQuestion", tool_input, None)
    assert isinstance(result, PermissionResultAllow)
    assert result.updated_input["answers"] == {"Q?": ""}


@pytest.mark.asyncio
async def test_plan_approval_allows_and_relaxes():
    state, ask_plan, on_approved, ask_question = _make_callbacks()
    can_use = make_can_use_tool(ask_plan, on_approved, ask_question)
    result = await can_use("ExitPlanMode", {"plan": "do things"}, None)
    assert isinstance(result, PermissionResultAllow)
    assert state["approved"] is True


@pytest.mark.asyncio
async def test_plan_rejection_denies_with_feedback():
    state, ask_plan, on_approved, ask_question = _make_callbacks(
        pd=PlanDecision(verdict=PlanVerdict.REJECT, feedback="too broad")
    )
    can_use = make_can_use_tool(ask_plan, on_approved, ask_question)
    result = await can_use("ExitPlanMode", {"plan": "x"}, None)
    assert isinstance(result, PermissionResultDeny)
    assert "too broad" in result.message


@pytest.mark.asyncio
async def test_non_plan_tool_is_allowed():
    state, ask_plan, on_approved, ask_question = _make_callbacks()
    can_use = make_can_use_tool(ask_plan, on_approved, ask_question)
    assert isinstance(
        await can_use("Bash", {"command": "ls"}, None), PermissionResultAllow
    )


# --------------------------------------------------------------------------- #
def test_events_serialization():
    q = Question.from_dict(
        {
            "question": "Where?",
            "header": "Output",
            "options": [{"label": "A", "description": "d"}],
            "multiSelect": True,
            "allowOther": False,
        }
    )
    assert q.multi_select and not q.allow_other and q.options[0].label == "A"
    resp = QuestionResponse(answers=[Answer("Output", ["A", "B"], other_text="C")])
    assert resp.to_model_text() == "Output: A, B, C"


def test_question_request_from_tool_input():
    req = QuestionRequest.from_tool_input(
        {"questions": [{"question": "Q?", "header": "H", "options": [{"label": "x"}]}]}
    )
    assert len(req.questions) == 1 and req.questions[0].header == "H"
