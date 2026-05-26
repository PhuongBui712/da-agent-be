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
)
from da_agent.agent.permissions import make_can_use_tool
from da_agent.agent.tools import QUALIFIED_TOOL_NAME, build_ask_tool, build_interaction_server
from da_agent.config import Settings


class FakeUI:
    """Records render calls; returns canned answers for interaction."""

    def __init__(self, question_response=None, plan_decision=None):
        self.calls: list[tuple] = []
        self._qr = question_response
        self._pd = plan_decision

    def _rec(self, *a):
        self.calls.append(a)

    def on_user_prompt(self, t): self._rec("prompt", t)
    def on_thinking(self, t): self._rec("thinking", t)
    def on_text(self, t): self._rec("text", t)
    def on_tool_use(self, n, i, *, depth=0): self._rec("tool_use", n, depth)
    def on_tool_result(self, s, *, is_error=False, depth=0): self._rec("tool_result", is_error, depth)
    def on_system(self, st, d): self._rec("system", st)
    def on_result(self, *, turns, cost_usd, duration_s): self._rec("result", turns)
    def on_error(self, m): self._rec("error", m)
    def begin_wait(self, label="Working"): pass
    def end_wait(self): pass

    async def ask_question(self, request): return self._qr
    async def approve_plan(self, plan): return self._pd


def _runner(ui=None):
    s = Settings(); s.show_thinking = True
    return AgentRunner(ui or FakeUI(), s)


# --------------------------------------------------------------------------- #
def test_options_assembly():
    opts = _runner()._build_options()
    assert opts.skills == ["xlsx"]
    assert "project" in opts.setting_sources
    assert opts.permission_mode == "plan"
    assert "interaction" in opts.mcp_servers
    assert set(opts.agents) == {"profiler", "analyst", "visualizer"}
    assert QUALIFIED_TOOL_NAME in opts.allowed_tools
    assert callable(opts.can_use_tool)
    assert opts.env["CLAUDE_CONFIG_DIR"].endswith("sessions")


def test_render_blocks_and_filtering():
    ui = FakeUI()
    r = _runner(ui)
    r._render_block(ThinkingBlock(thinking="hmm", signature="s"), 0)
    r._render_block(TextBlock(text="hello"), 0)
    r._render_block(ToolUseBlock(id="t1", name="Bash", input={"command": "ls"}), 0)
    # interactive tools must be filtered out of the normal step stream
    r._render_block(ToolUseBlock(id="t2", name="ExitPlanMode", input={"plan": "x"}), 0)
    r._render_block(ToolUseBlock(id="t3", name=QUALIFIED_TOOL_NAME, input={}), 0)
    kinds = [c[0] for c in ui.calls]
    assert kinds.count("thinking") == 1
    assert kinds.count("text") == 1
    assert kinds.count("tool_use") == 1  # only Bash, not the two interactive tools


def test_render_tool_result_depth_and_error():
    ui = FakeUI()
    r = _runner(ui)
    r._render_tool_result(ToolResultBlock(tool_use_id="t", content="oops", is_error=True), depth=1)
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


@pytest.mark.asyncio
async def test_ask_user_question_tool_roundtrip():
    qr = QuestionResponse(answers=[Answer(header="Output", selected=["New .xlsx"])])
    ui = FakeUI(question_response=qr)
    ask_tool = build_ask_tool(lambda: ui)
    result = await ask_tool.handler(
        {"questions": [{"question": "Where?", "header": "Output", "options": [{"label": "New .xlsx"}]}]}
    )
    assert result["content"][0]["text"] == "Output: New .xlsx"


@pytest.mark.asyncio
async def test_plan_approval_allows_and_relaxes():
    approved = {"flag": False}

    async def on_approved():
        approved["flag"] = True

    async def ask_plan(plan):
        return PlanDecision(verdict=PlanVerdict.APPROVE)

    can_use = make_can_use_tool(ask_plan, on_approved)
    result = await can_use("ExitPlanMode", {"plan": "do things"}, None)
    assert isinstance(result, PermissionResultAllow)
    assert approved["flag"] is True


@pytest.mark.asyncio
async def test_plan_rejection_denies_with_feedback():
    async def on_approved(): ...
    async def ask_plan(plan):
        return PlanDecision(verdict=PlanVerdict.REJECT, feedback="too broad")

    can_use = make_can_use_tool(ask_plan, on_approved)
    result = await can_use("ExitPlanMode", {"plan": "x"}, None)
    assert isinstance(result, PermissionResultDeny)
    assert "too broad" in result.message


@pytest.mark.asyncio
async def test_non_plan_tool_is_allowed():
    async def on_approved(): ...
    async def ask_plan(plan): return PlanDecision(verdict=PlanVerdict.APPROVE)
    can_use = make_can_use_tool(ask_plan, on_approved)
    assert isinstance(await can_use("Bash", {"command": "ls"}, None), PermissionResultAllow)


# --------------------------------------------------------------------------- #
def test_events_serialization():
    q = Question.from_dict(
        {"question": "Where?", "header": "Output", "options": [{"label": "A", "description": "d"}],
         "multiSelect": True, "allowOther": False}
    )
    assert q.multi_select and not q.allow_other and q.options[0].label == "A"
    resp = QuestionResponse(answers=[Answer("Output", ["A", "B"], other_text="C")])
    assert resp.to_model_text() == "Output: A, B, C"


def test_question_request_from_tool_input():
    req = QuestionRequest.from_tool_input(
        {"questions": [{"question": "Q?", "header": "H", "options": [{"label": "x"}]}]}
    )
    assert len(req.questions) == 1 and req.questions[0].header == "H"
