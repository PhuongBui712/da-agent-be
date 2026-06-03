"""Permission handling.

The `can_use_tool` callback is the SDK's gate before a tool runs. We use it for the
two built-in tools that need a human:

- **AskUserQuestion**: the model surfaces a multiple-choice question; we drive our own
  UI, then return the user's selections to the model via `updated_input.answers`.
- **ExitPlanMode**: when the agent finishes planning we surface the plan for approval
  and, on accept, relax the permission mode.

When the question's first sub-question carries `header == "Target"`, the web path
additionally validates the (Target, Source) pair and returns the resolved absolute
write path inside `updated_input` so the model writes to it verbatim.
CLI usage passes `resolve_target=None` and skips that hop.

Everything else is allowed (this is a trusted, single-user local tool; all steps
are still shown in the TUI for transparency).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from claude_agent_sdk import (
    PermissionResultAllow,
    PermissionResultDeny,
    ToolPermissionContext,
)

from .events import PlanDecision, QuestionRequest, QuestionResponse

# Called when a plan is approved, so the runner can relax the permission mode.
OnPlanApproved = Callable[[], Awaitable[None]]
AskPlan = Callable[[str], Awaitable[PlanDecision]]
AskQuestion = Callable[[QuestionRequest], Awaitable[QuestionResponse]]


@dataclass(slots=True)
class TargetResolution:
    """Result of (Target, Source) validation + path resolution."""

    resolved_target_path: str
    resolved_target_kind: str  # "standalone" | "kb_version" | "attachment_version"


@dataclass(slots=True)
class TargetValidationError(Exception):
    """Raised by a `resolve_target` callback when the (Target, Source) pair
    fails validation. The message is surfaced verbatim to the model in
    `PermissionResultDeny`.
    """

    message: str

    def __str__(self) -> str:
        return self.message


# A resolver receives the raw question payload from the SDK plus the user's
# selected labels and returns the resolved absolute path + kind. Raise
# `TargetValidationError` to surface a deny back to the model.
ResolveTarget = Callable[
    [list[dict[str, Any]], list[dict[str, Any]]],
    Awaitable[TargetResolution | None],
]


def make_can_use_tool(
    ask_plan: AskPlan,
    on_approved: OnPlanApproved,
    ask_question: AskQuestion,
    *,
    resolve_target: ResolveTarget | None = None,
):
    async def can_use_tool(
        tool_name: str,
        tool_input: dict[str, Any],
        context: ToolPermissionContext,
    ):
        if tool_name == "AskUserQuestion":
            return await _handle_ask_user_question(
                tool_input, ask_question, resolve_target
            )
        if tool_name == "ExitPlanMode":
            return await _handle_exit_plan_mode(tool_input, ask_plan, on_approved)
        return PermissionResultAllow()

    return can_use_tool


async def _handle_ask_user_question(
    tool_input: dict[str, Any],
    ask_question: AskQuestion,
    resolve_target: ResolveTarget | None,
):
    """Drive the UI for the model's questions and return answers via `updated_input`.

    The built-in AskUserQuestion tool reads `updated_input.answers` (a mapping of
    question text -> selected label string) as the user's response.

    If the first question carries `header == "Target"` and a `resolve_target` callback
    was supplied, validate the (Target, Source) pair and inject `resolved_target_path`
    + `resolved_target_kind` into `updated_input` so the model writes to the correct
    disk location.
    """
    raw_questions = list(tool_input.get("questions") or [])
    request = QuestionRequest.from_tool_input(tool_input)
    response = await ask_question(request)

    answers: dict[str, str] = {}
    raw_answers: list[dict[str, Any]] = []
    for i, q_dict in enumerate(raw_questions):
        key = q_dict.get("question") or q_dict.get("header") or f"Q{i + 1}"
        if i < len(response.answers):
            ans = response.answers[i]
            answers[key] = ", ".join(ans.values())
            raw_answers.append(
                {
                    "header": q_dict.get("header", ""),
                    "selected": list(ans.values()),
                }
            )
        else:
            answers[key] = ""
            raw_answers.append({"header": q_dict.get("header", ""), "selected": []})

    updated_input: dict[str, Any] = {
        "questions": raw_questions,
        "answers": answers,
    }

    if resolve_target is not None and _is_output_target_question(raw_questions):
        try:
            resolution = await resolve_target(raw_questions, raw_answers)
        except TargetValidationError as exc:
            return PermissionResultDeny(message=str(exc), interrupt=False)
        if resolution is not None:
            updated_input["resolved_target_path"] = resolution.resolved_target_path
            updated_input["resolved_target_kind"] = resolution.resolved_target_kind
            # SDK does not forward extra updated_input keys to the tool result;
            # the agent only sees `{questions, answers}`. Encode the resolved
            # path into the Target answer string itself so the model reads it
            # back from tool_result without guessing.
            target_q_text = _find_target_question_text(raw_questions)
            if target_q_text and target_q_text in answers:
                answers[target_q_text] = (
                    f"{answers[target_q_text]} → write final file to "
                    f"{resolution.resolved_target_path}"
                )

    return PermissionResultAllow(updated_input=updated_input)


def _find_target_question_text(raw_questions: list[dict[str, Any]]) -> str | None:
    """Return the `question` text of the first question whose header is 'Target'.

    Falls back to the first question's text if no Target header found.
    """
    for q in raw_questions:
        if (q.get("header") or "").strip().lower() == "target":
            return q.get("question")
    return raw_questions[0].get("question") if raw_questions else None


def _is_output_target_question(raw_questions: list[dict[str, Any]]) -> bool:
    """Detect the output-target question chain by header convention.

    First question MUST carry `header == "Target"`. We deliberately fence on
    the literal English string (set by the prompt) so non-output questions —
    arbitrary clarifications the model raises — are not mis-routed.
    """
    if not raw_questions:
        return False
    return raw_questions[0].get("header") == "Target"


async def _handle_exit_plan_mode(
    tool_input: dict[str, Any],
    ask_plan: AskPlan,
    on_approved: OnPlanApproved,
):
    plan = tool_input.get("plan") or tool_input.get("plan_text") or ""
    decision = await ask_plan(plan)
    if decision.approved:
        await on_approved()
        return PermissionResultAllow()
    feedback = decision.feedback or "The user rejected the plan."
    return PermissionResultDeny(
        message=(
            f"User did not approve the plan. Revise it based on this feedback "
            f"and present an updated plan: {feedback}"
        ),
        interrupt=False,
    )
