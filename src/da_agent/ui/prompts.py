"""Interactive prompts (prompt_toolkit).

Renders the tabbed multiple-choice picker (like Claude Code's AskUserQuestion) and the
plan approval prompt. Inline (non-fullscreen) so the conversation scrollback stays
visible. Falls back to a plain stdin selector when there is no TTY (pipes, the demo).
"""

from __future__ import annotations

import sys
from typing import Optional

from prompt_toolkit import PromptSession
from prompt_toolkit.application import Application
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import Layout, Window
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.styles import Style

from ..agent.events import (
    Answer,
    Option,
    PlanDecision,
    PlanVerdict,
    Question,
    QuestionRequest,
    QuestionResponse,
)

_OTHER_LABEL = "Type something…"

_STYLE = Style.from_dict(
    {
        "tab": "#8b949e",
        "tab.active": "#a371f7 bold reverse",
        "tab.submit": "#3fb950 bold",
        "q": "bold",
        "cursor": "#a371f7 bold",
        "opt": "",
        "opt.sel": "#3fb950 bold",
        "desc": "#6e7681",
        "footer": "#6e7681",
        "num": "#8b949e",
    }
)


# --------------------------------------------------------------------------- #
# public API
# --------------------------------------------------------------------------- #
async def run_question_selector(request: QuestionRequest) -> QuestionResponse:
    if not request.questions:
        return QuestionResponse(answers=[])
    answers = await _select(request.questions)
    return QuestionResponse(answers=answers)


async def confirm_plan() -> PlanDecision:
    q = Question(
        question="Proceed with this plan?",
        header="Approve",
        options=[
            Option("Yes, execute", "Run the plan as described"),
            Option("No, revise", "Send feedback and get an updated plan"),
        ],
        multi_select=False,
        allow_other=False,
    )
    answers = await _select([q])
    chosen = answers[0].values() if answers else []
    if chosen and chosen[0].startswith("Yes"):
        return PlanDecision(verdict=PlanVerdict.APPROVE)
    feedback = await _ask_text("  What should change? ")
    return PlanDecision(
        verdict=PlanVerdict.REJECT, feedback=feedback or "Please revise the plan."
    )


# --------------------------------------------------------------------------- #
# selection engine
# --------------------------------------------------------------------------- #
async def _select(questions: list[Question]) -> list[Answer]:
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        return _fallback_select(questions)
    app, state = build_selector_app(questions)
    result = await app.run_async()
    if result is None:  # cancelled
        return []
    return await _gather_answers(questions, state)


def build_selector_app(questions: list[Question]):
    """Construct the inline tabbed selector Application and its mutable state.

    Returned separately from running so it can be driven by prompt_toolkit's test
    harness (pipe input + DummyOutput) without a real terminal.
    """
    nq = len(questions)
    state = {
        "tab": 0,
        "cursor": [0] * nq,
        "selected": [set() for _ in range(nq)],  # selected option indices
        "other": [False] * nq,
    }

    def opt_count(qi: int) -> int:
        return len(questions[qi].options) + (1 if questions[qi].allow_other else 0)

    def is_other(qi: int, oi: int) -> bool:
        return questions[qi].allow_other and oi == len(questions[qi].options)

    def toggle(qi: int, oi: int) -> None:
        q = questions[qi]
        if is_other(qi, oi):
            state["other"][qi] = not state["other"][qi]
            if not q.multi_select:
                state["selected"][qi].clear()
            return
        if q.multi_select:
            sel = state["selected"][qi]
            sel.discard(oi) if oi in sel else sel.add(oi)
        else:
            state["selected"][qi] = {oi}
            state["other"][qi] = False

    def fragments():
        out = []
        # tab row
        for qi, q in enumerate(questions):
            style = "class:tab.active" if state["tab"] == qi else "class:tab"
            mark = "✓" if (state["selected"][qi] or state["other"][qi]) else "□"
            out.append((style, f" {mark} {q.header} "))
            out.append(("", " "))
        submit_style = "class:tab.active" if state["tab"] == nq else "class:tab.submit"
        out.append((submit_style, " ✔ Submit "))
        out.append(("", "\n\n"))

        tab = state["tab"]
        if tab == nq:
            out.append(("class:q", "Submit your answers?\n"))
            out.append(("class:footer", "Enter to confirm · ←/→ to review answers\n"))
        else:
            q = questions[tab]
            hint = "select all that apply" if q.multi_select else "choose one"
            out.append(("class:q", f"{q.question}  "))
            out.append(("class:desc", f"({hint})\n\n"))
            for oi in range(opt_count(tab)):
                cur = oi == state["cursor"][tab]
                if is_other(tab, oi):
                    label, desc, chosen = _OTHER_LABEL, "", state["other"][tab]
                else:
                    o = q.options[oi]
                    label, desc, chosen = (
                        o.label,
                        o.description,
                        oi in state["selected"][tab],
                    )
                box = (
                    ("[x]" if chosen else "[ ]")
                    if q.multi_select
                    else ("(•)" if chosen else "( )")
                )
                arrow = "❯" if cur else " "
                out.append(("class:cursor" if cur else "", f" {arrow} "))
                out.append(("class:num", f"{oi + 1}. "))
                out.append(
                    ("class:opt.sel" if chosen else "class:opt", f"{box} {label}\n")
                )
                if desc:
                    out.append(("class:desc", f"       {desc}\n"))
            out.append(("", "\n"))
            out.append(
                (
                    "class:footer",
                    "↑/↓ move · Enter/Space select · ←/→ next · Esc cancel\n",
                )
            )
        return out

    kb = KeyBindings()

    @kb.add("left")
    @kb.add("s-tab")
    def _(e):
        state["tab"] = (state["tab"] - 1) % (nq + 1)

    @kb.add("right")
    @kb.add("tab")
    def _(e):
        state["tab"] = (state["tab"] + 1) % (nq + 1)

    @kb.add("up")
    def _(e):
        if state["tab"] < nq:
            t = state["tab"]
            state["cursor"][t] = (state["cursor"][t] - 1) % opt_count(t)

    @kb.add("down")
    def _(e):
        if state["tab"] < nq:
            t = state["tab"]
            state["cursor"][t] = (state["cursor"][t] + 1) % opt_count(t)

    @kb.add("space")
    def _(e):
        if state["tab"] < nq:
            toggle(state["tab"], state["cursor"][state["tab"]])

    @kb.add("enter")
    def _(e):
        t = state["tab"]
        if t == nq:
            e.app.exit(result="ok")
            return
        if not questions[t].multi_select:
            toggle(t, state["cursor"][t])
        state["tab"] = t + 1  # advance toward Submit

    for d in range(1, 10):

        @kb.add(str(d))
        def _(e, d=d):
            t = state["tab"]
            if t < nq and d <= opt_count(t):
                state["cursor"][t] = d - 1
                toggle(t, d - 1)

    @kb.add("escape")
    @kb.add("c-c")
    def _(e):
        e.app.exit(result=None)

    app = Application(
        layout=Layout(
            Window(FormattedTextControl(fragments, focusable=True), wrap_lines=True)
        ),
        key_bindings=kb,
        style=_STYLE,
        full_screen=False,
        mouse_support=False,
    )
    return app, state


async def _gather_answers(questions: list[Question], state: dict) -> list[Answer]:
    answers: list[Answer] = []
    for qi, q in enumerate(questions):
        labels = [q.options[i].label for i in sorted(state["selected"][qi])]
        other_text: Optional[str] = None
        if state["other"][qi]:
            other_text = await _ask_text(f"  {q.header} — type your answer: ")
        answers.append(Answer(header=q.header, selected=labels, other_text=other_text))
    return answers


async def _ask_text(prompt: str) -> str:
    try:
        session: PromptSession = PromptSession()
        return (await session.prompt_async(prompt)).strip()
    except (EOFError, KeyboardInterrupt):
        return ""


# --------------------------------------------------------------------------- #
# non-TTY fallback (pipes / demo without a terminal)
# --------------------------------------------------------------------------- #
def _fallback_select(questions: list[Question]) -> list[Answer]:
    answers: list[Answer] = []
    for q in questions:
        print(f"\n{q.question} ({'multi' if q.multi_select else 'single'} select)")
        for i, o in enumerate(q.options, 1):
            desc = f" — {o.description}" if o.description else ""
            print(f"  {i}. {o.label}{desc}")
        if q.allow_other:
            print(f"  {len(q.options) + 1}. {_OTHER_LABEL}")
        try:
            raw = input("  > ").strip()
        except (EOFError, KeyboardInterrupt):
            raw = ""
        labels, other_text = [], None
        for tok in raw.replace(",", " ").split():
            if tok.isdigit():
                idx = int(tok) - 1
                if 0 <= idx < len(q.options):
                    labels.append(q.options[idx].label)
                elif q.allow_other and idx == len(q.options):
                    try:
                        other_text = input("    your answer: ").strip()
                    except (EOFError, KeyboardInterrupt):
                        other_text = None
        if not labels and not other_text and q.options:
            labels = [q.options[0].label]
        answers.append(Answer(header=q.header, selected=labels, other_text=other_text))
    return answers
