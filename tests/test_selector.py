"""Drive the real prompt_toolkit selector with scripted keystrokes (no terminal)."""

from __future__ import annotations

import pytest
from prompt_toolkit.application import create_app_session
from prompt_toolkit.input.defaults import create_pipe_input
from prompt_toolkit.output import DummyOutput

from da_agent.agent.events import Option, Question
from da_agent.ui.prompts import build_selector_app

DOWN = "\x1b[B"
RIGHT = "\x1b[C"
ENTER = "\r"
SPACE = " "


async def _drive(questions, keys):
    with create_pipe_input() as pipe:
        with create_app_session(input=pipe, output=DummyOutput()):
            app, state = build_selector_app(questions)
            for k in keys:
                pipe.send_text(k)
            await app.run_async()
            return state


@pytest.mark.asyncio
async def test_single_select_arrow_and_enter():
    q = Question(
        "Where?",
        "Output",
        [Option("a"), Option("b"), Option("c")],
        multi_select=False,
        allow_other=False,
    )
    # down -> cursor=1, enter -> select+advance to Submit, enter -> submit
    state = await _drive([q], [DOWN, ENTER, ENTER])
    assert state["selected"][0] == {1}


@pytest.mark.asyncio
async def test_digit_select():
    q = Question(
        "Where?",
        "Output",
        [Option("a"), Option("b"), Option("c")],
        multi_select=False,
        allow_other=False,
    )
    # press '3' selects option 3 (single-select), then go to submit and enter
    state = await _drive([q], ["3", RIGHT, ENTER])
    assert state["selected"][0] == {2}


@pytest.mark.asyncio
async def test_multi_select_space_toggles():
    q = Question(
        "Pick",
        "Tags",
        [Option("x"), Option("y"), Option("z")],
        multi_select=True,
        allow_other=False,
    )
    # space toggles option 1; down; space toggles option 2; right to submit; enter
    state = await _drive([q], [SPACE, DOWN, SPACE, RIGHT, ENTER])
    assert state["selected"][0] == {0, 1}
