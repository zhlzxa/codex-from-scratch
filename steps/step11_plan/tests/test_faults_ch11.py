"""Chapter 11: a plan the model keeps and the loop can read.

Nothing here touches the network. The agents are real `Agent`s driven by a
scripted model; the plan is the real `TaskPlan`; the files are real files.

Test names carry the fault IDs from FAULTS.md.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest

from minicodex.agent import Agent
from minicodex.agent_types import ToolSet
from minicodex.approval import Session
from minicodex.compaction import Sizer, SummaryRequest
from minicodex.compaction import compact as run_compaction
from minicodex.composition import watching
from minicodex.history import History
from minicodex.model import Completed, TextDelta, ToolCallDelta
from minicodex.plan import (
    MAX_STEPS,
    PLAN_INSTRUCTIONS,
    PLAN_SCHEMA,
    PlanStep,
    TaskPlan,
    plan_toolset,
    unfinished_note,
    update_plan,
)
from minicodex.shell import ENV_ALLOWLIST, ShellSession

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


class ScriptedModel:
    """Replays a fixed list of turns and remembers what it was sent."""

    def __init__(self, turns: Sequence[Any]) -> None:
        self.turns = list(turns)
        self.sent: list[list[dict[str, Any]]] = []

    async def stream(self, messages: Sequence[dict[str, Any]]) -> Any:
        self.sent.append([dict(m) for m in messages])
        turn = self.turns[min(len(self.sent) - 1, len(self.turns) - 1)]
        if isinstance(turn, str):
            yield TextDelta(turn)
        else:
            for index, (call_id, name, arguments) in enumerate(turn):
                yield ToolCallDelta(
                    call_id=call_id, index=index, name=name, arguments=json.dumps(arguments)
                )
        yield Completed("stop")


def steps(*pairs: tuple[str, str]) -> list[dict[str, str]]:
    return [{"step": text, "status": status} for text, status in pairs]


async def call(plan: TaskPlan, *pairs: tuple[str, str]) -> str:
    return await update_plan(plan, {"plan": steps(*pairs)})


# ---------------------------------------------------------------------------
# F11-01  a plan says what finished means, before the model decides
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_F11_01_the_plan_answers_a_question_the_loop_could_not_ask() -> None:
    """Chapter 0's stop condition is "no tool calls this turn", which knows
    nothing about the task. `outstanding()` is the first thing in this program
    that does."""
    plan = TaskPlan()
    assert plan.outstanding() == ()  # no plan: chapter 0 behaviour exactly

    await call(plan, ("add subtract", "in_progress"), ("update README", "pending"))
    assert [s.text for s in plan.outstanding()] == ["add subtract", "update README"]

    plan.record_work("apply_patch")
    await call(plan, ("add subtract", "completed"), ("update README", "pending"))
    assert [s.text for s in plan.outstanding()] == ["update README"]


@pytest.mark.asyncio
async def test_F11_01_the_tool_and_its_schema_travel_together() -> None:
    """`ToolSet.__post_init__` is what makes that a fact rather than a habit."""
    tools = plan_toolset(TaskPlan())
    assert set(tools.handlers) == {"update_plan"}
    assert [s["function"]["name"] for s in tools.schemas] == ["update_plan"]


def test_F11_01_a_plan_call_is_stateful_so_it_never_races() -> None:
    """Two `update_plan` calls in one turn would both read the pre-update list.
    The default footprint is `STATEFUL`, which is the right answer here without
    anyone having to choose it -- chapter 8's conservative default paying off
    for the third time."""
    from minicodex.agent_types import ToolCall

    footprint = plan_toolset(TaskPlan()).footprint_of(
        ToolCall("call_1", "update_plan", {"plan": []}, "{}")
    )
    assert footprint.stateful


# ---------------------------------------------------------------------------
# F11-02  too fine
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_F11_02_a_plan_longer_than_the_limit_is_refused() -> None:
    plan = TaskPlan()
    answer = await update_plan(
        plan, {"plan": steps(*[(f"step {i}", "pending") for i in range(MAX_STEPS + 1)])}
    )
    assert answer.startswith("Error")
    assert str(MAX_STEPS) in answer
    assert plan.steps == ()  # refused means unchanged, not partially applied


@pytest.mark.asyncio
async def test_F11_02_the_limit_is_a_limit_and_not_a_suggestion() -> None:
    plan = TaskPlan()
    answer = await update_plan(
        plan, {"plan": steps(*[(f"step {i}", "pending") for i in range(MAX_STEPS)])}
    )
    assert not answer.startswith("Error")
    assert len(plan.steps) == MAX_STEPS


# ---------------------------------------------------------------------------
# F11-03  too coarse -- what the code can and cannot say
# ---------------------------------------------------------------------------


def test_F11_03_the_description_states_the_grain_because_the_code_cannot() -> None:
    """Granularity is the one thing in this module that has to live in prose:
    "one piece of work you could show is done" is not checkable here. The test
    pins that the sentence exists, which is all a test can do -- F03-10's
    snapshot is what stops it being edited by accident."""
    description = PLAN_SCHEMA["function"]["parameters"]["properties"]["plan"]["items"][
        "properties"
    ]["step"]["description"]
    assert "shown to be done" in description


# ---------------------------------------------------------------------------
# F11-04  the plan moves when the task does
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_F11_04_every_revision_is_kept() -> None:
    """A plan that silently changes shape is invisible from outside: the model
    sends the whole list every time, so the previous one is gone unless
    somebody keeps it."""
    plan = TaskPlan()
    await call(plan, ("write divide.py", "in_progress"))
    plan.record_work("run_shell")
    await call(plan, ("add divide() to calc.py", "in_progress"))

    assert len(plan.revisions) == 2
    assert "write divide.py" in plan.revisions[0]
    assert "add divide() to calc.py" in plan.revisions[1]
    assert plan.updates == 2


@pytest.mark.asyncio
async def test_F11_04_a_renamed_step_counts_as_a_new_one() -> None:
    """Steps are matched on their text, because there are no ids to match on.
    A rename therefore reads as a new step and has to justify its completion
    like any other -- the safe direction of an unavoidable ambiguity."""
    plan = TaskPlan()
    await call(plan, ("add divide", "completed"))
    answer = await update_plan(plan, {"plan": steps(("add divide()", "completed"))})
    assert answer.startswith("Error")


# ---------------------------------------------------------------------------
# F11-05  updating the plan is not doing the work
# ---------------------------------------------------------------------------


def test_F11_05_a_plan_update_does_not_count_as_work() -> None:
    plan = TaskPlan()
    plan.record_work("update_plan")
    assert plan.work_since_update == 0
    plan.record_work("read_file")
    assert plan.work_since_update == 1


@pytest.mark.asyncio
async def test_F11_05_two_updates_in_a_row_cannot_both_close_a_step() -> None:
    """The cheapest form of plan theatre: call the tool twice and move the
    markers. The second call has nothing behind it and is refused."""
    plan = TaskPlan()
    await call(plan, ("a", "in_progress"), ("b", "pending"))
    answer = await call(plan, ("a", "completed"), ("b", "in_progress"))
    assert answer.startswith("Error")
    assert plan.steps[0].status == "in_progress"


@pytest.mark.asyncio
async def test_F11_05_the_counter_resets_on_every_accepted_update() -> None:
    plan = TaskPlan()
    await call(plan, ("a", "in_progress"))
    plan.record_work("apply_patch")
    assert plan.work_since_update == 1
    await call(plan, ("a", "completed"))
    assert plan.work_since_update == 0


# ---------------------------------------------------------------------------
# F11-08  a step marked done that was never done
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_F11_08_closing_a_step_with_nothing_behind_it_is_refused() -> None:
    plan = TaskPlan()
    await call(plan, ("update the README", "in_progress"))
    answer = await call(plan, ("update the README", "completed"))

    assert answer.startswith("Error")
    assert "nothing has run" in answer
    assert "run the test or read the file back" in answer
    assert plan.outstanding()


@pytest.mark.asyncio
async def test_F11_08_the_first_plan_may_contain_completed_steps() -> None:
    """A model that did the work and then wrote the plan down is not lying.
    The check is on the transition, and there is no transition on the first
    call -- refusing that would punish the honest order."""
    plan = TaskPlan()
    answer = await call(plan, ("read the tests", "completed"), ("add divide", "in_progress"))
    assert not answer.startswith("Error")


@pytest.mark.asyncio
async def test_F11_08_work_of_any_kind_is_enough_evidence_and_says_so() -> None:
    """The check is deliberately weak: it knows that *something* ran, not that
    the right thing ran. Pinning the weak version stops anyone reading it as
    the strong one."""
    plan = TaskPlan()
    await call(plan, ("update the README", "in_progress"))
    plan.record_work("read_file")  # not the README, and nothing here can tell
    answer = await call(plan, ("update the README", "completed"))
    assert not answer.startswith("Error")


@pytest.mark.asyncio
async def test_F11_08_the_loop_asks_once_before_it_stops() -> None:
    model = ScriptedModel(["all done"])
    plan = TaskPlan()
    await call(plan, ("a", "completed"), ("b", "pending"))

    agent = Agent(model, {}, max_turns=6, on_stop=unfinished_note(plan))
    result = await agent.run("do a and b")

    assert result.stop_reason == "completed"
    assert result.turns_used == 2  # stopped, was nudged, stopped again
    notes = [m for m in model.sent[-1] if m["role"] == "system"]
    assert any("not marked completed" in str(m["content"]) for m in notes)
    assert any("[ ] b" in str(m["content"]) for m in notes)


@pytest.mark.asyncio
async def test_F11_08_the_nudge_happens_at_most_once() -> None:
    """A nudge that can renew itself is a turn budget spent arguing. The bound
    is in the loop, not in the callback, because the callback is supplied by
    the caller and the loop is not."""
    model = ScriptedModel(["still not done"])
    plan = TaskPlan()
    await call(plan, ("a", "pending"))

    agent = Agent(model, {}, max_turns=6, on_stop=unfinished_note(plan))
    result = await agent.run("do a")

    assert result.turns_used == 2
    assert result.stop_reason == "completed"


@pytest.mark.asyncio
async def test_F11_08_a_finished_plan_does_not_nudge() -> None:
    model = ScriptedModel(["done"])
    plan = TaskPlan()
    await call(plan, ("a", "completed"))

    agent = Agent(model, {}, max_turns=6, on_stop=unfinished_note(plan))
    result = await agent.run("do a")

    assert result.turns_used == 1


@pytest.mark.asyncio
async def test_F11_08_no_plan_means_chapter_zero_behaviour() -> None:
    """The stop check is wired in unconditionally by `__main__`, so the run
    with no plan has to be indistinguishable from the one before this chapter."""
    model = ScriptedModel(["done"])
    agent = Agent(model, {}, max_turns=6, on_stop=unfinished_note(TaskPlan()))
    result = await agent.run("hello")
    assert result.turns_used == 1


# ---------------------------------------------------------------------------
# F11-07  the budget runs out and nothing is delivered
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_F11_07_the_last_turn_does_not_run_tools() -> None:
    """Measured before this existed: a task that does not fit ended with a
    final answer of **zero characters**, four runs out of five. The model
    spends its last turn on a tool call, the loop runs it, appends a result
    nobody will ever read, and falls out of the loop with nothing to say.

    A better-worded warning cannot fix that, because while the model still has
    a tool it can call, calling one is a reasonable thing to do."""
    ran: list[str] = []

    async def tool(_args: dict[str, Any]) -> str:
        ran.append("x")
        return "done"

    model = ScriptedModel([[("call_1", "t", {})]])
    agent = Agent(model, {"t": tool}, max_turns=3)
    result = await agent.run("go")

    assert result.stop_reason == "turn_limit"
    assert result.turns_used == 3
    assert len(ran) == 2, "the first two turns run tools; the last one does not"


@pytest.mark.asyncio
async def test_F11_07_an_unrun_call_still_gets_an_output() -> None:
    """F07-04's rule has no exception for a call the loop chose not to make.
    A history with an unanswered call is one `to_wire()` refuses to render, so
    the run would be unresumable."""

    async def tool(_args: dict[str, Any]) -> str:
        return "done"

    model = ScriptedModel([[("call_1", "t", {}), ("call_2", "t", {})]])
    agent = Agent(model, {"t": tool}, max_turns=2)
    result = await agent.run("go")

    assert result.history.unanswered() == ()
    results = [i for i in result.history.items if type(i).__name__ == "ToolResult"]
    assert results[-1].content.startswith("Error: the turn budget ended")
    result.history.to_wire("chat_completions")  # would raise if it were illegal


@pytest.mark.asyncio
async def test_F11_07_the_last_two_turns_are_told_different_things() -> None:
    """The second-to-last turn still has tools; the last one does not. One
    message cannot mean both."""

    async def tool(_args: dict[str, Any]) -> str:
        return "done"

    model = ScriptedModel([[("call_1", "t", {})]])
    await Agent(model, {"t": tool}, max_turns=3).run("go")

    def notes(request: list[dict[str, Any]]) -> str:
        return " ".join(str(m["content"]) for m in request if m["role"] == "system")

    assert "2 tool-calling turn(s) left" in notes(model.sent[1])
    assert "Do not start new work" in notes(model.sent[1])
    assert "last turn" in notes(model.sent[2])
    assert "will not be run" in notes(model.sent[2])


@pytest.mark.asyncio
async def test_F11_07_the_stop_check_does_not_spend_the_last_turn() -> None:
    """A nudge needs a turn to be answered in. Firing it when there is none
    left turns a delivered answer into a turn_limit with nothing after it."""
    plan = TaskPlan()
    await call(plan, ("a", "pending"))
    model = ScriptedModel(["here is what I did"])

    result = await Agent(model, {}, max_turns=1, on_stop=unfinished_note(plan)).run("go")

    assert result.stop_reason == "completed"
    assert result.final_text == "here is what I did"


# ---------------------------------------------------------------------------
# validation: the rules the description states and the code enforces
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_two_steps_in_progress_at_once_is_refused() -> None:
    """codex states this rule in the tool description and in two system
    prompts, and does not check it anywhere. One line of code makes it true."""
    plan = TaskPlan()
    answer = await call(plan, ("a", "in_progress"), ("b", "in_progress"))
    assert answer.startswith("Error")
    assert "in_progress" in answer
    assert plan.steps == ()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("argument", "expected", "advice"),
    [
        ({}, "non-empty list", "Example:"),
        ({"plan": []}, "non-empty list", "Example:"),
        ({"plan": "add subtract"}, "non-empty list", "Example:"),
        ({"plan": ["add subtract"]}, "not an object", "Each step is"),
        ({"plan": [{"status": "pending"}]}, 'no "step" text', "Each step is"),
        ({"plan": [{"step": "  ", "status": "pending"}]}, 'no "step" text', "Each step is"),
        ({"plan": [{"step": "a", "status": "doing"}]}, "not one of the three", "Use one of:"),
    ],
)
async def test_a_malformed_plan_says_what_to_send_instead(
    argument: dict[str, Any], expected: str, advice: str
) -> None:
    """Every rejection carries the third part. `tool_error` makes `do_this`
    required, so the only way to ship a message without one is not to use it --
    which is what `test_F03_07_no_module_builds_an_error_string_by_hand` now
    checks for `plan.py` too."""
    answer = await update_plan(TaskPlan(), argument)
    assert answer.startswith("Error")
    assert expected in answer
    assert advice in answer


@pytest.mark.asyncio
async def test_the_answer_carries_the_plan_back() -> None:
    """codex answers `Plan updated` and shows the list in its own UI. This
    program has no panel, so the two places a plan can be seen are the terminal
    and the history, and this string is the only thing that reaches the
    second."""
    plan = TaskPlan()
    answer = await call(plan, ("a", "in_progress"), ("b", "pending"))
    assert "[>] a" in answer
    assert "[ ] b" in answer
    assert "2 step(s) to go" in answer


# ---------------------------------------------------------------------------
# the plan and the rest of the program
# ---------------------------------------------------------------------------


def test_every_tool_reports_work_to_the_plan_including_a_remote_one() -> None:
    """`watching()` wraps the merged table, so an MCP tool counts as work in
    exactly the way a local one does. Wrapping earlier -- inside `local_tools`,
    say -- would leave remote calls invisible to the evidence check, which is
    the shape of bug chapter 9's namespacing exists to prevent."""

    async def handler(_args: dict[str, Any]) -> str:
        return "ok"

    schema = {
        "type": "function",
        "function": {"name": "mcp__notes__search", "description": "x", "parameters": {}},
    }
    plan = TaskPlan()
    tools = watching(ToolSet(handlers={"mcp__notes__search": handler}, schemas=[schema]), plan)

    asyncio.run(tools.handlers["mcp__notes__search"]({}))
    assert plan.work_since_update == 1


def test_the_plan_is_not_in_the_history_so_compaction_cannot_delete_it() -> None:
    """Measured: a history whose only record of the plan is the `update_plan`
    call loses it entirely at the first compaction -- 20 items to 5, and the
    call is in the dropped region. The plan survives because it is an object
    the run holds, not a message."""
    plan = TaskPlan()
    plan.steps = (PlanStep("add divide", "in_progress"),)

    history = History()
    history.add_system_note("You are a coding agent.")
    history.add_user("bring the calculator up to scratch")
    for index in range(2, 8):
        from minicodex.agent_types import ToolCall

        read = ToolCall(f"call_{index}", "read_file", {"path": "calc.py"}, "{}")
        history.add_assistant("", (read,))
        history.add_tool_result(f"call_{index}", "x" * 4000)

    async def summarise(_request: SummaryRequest) -> str:
        return "## Done\n- work happened"

    result = asyncio.run(run_compaction(history, summarise=summarise, budget=1200, sizer=Sizer()))

    assert result.plan.drops > 0
    assert len(result.history.items) < len(history.items)
    assert plan.outstanding()  # untouched by any of that


# ---------------------------------------------------------------------------
# found while measuring this chapter, not on the list
# ---------------------------------------------------------------------------


def test_a_subprocess_can_import_asyncio() -> None:
    """The agent's own way of checking its work is `python -m pytest`, and on
    Windows that needs `SYSTEMROOT` to reach the subprocess: without it Winsock
    cannot initialise and `import asyncio` dies with WinError 10106, *after*
    the command has been accepted and run. The model read that traceback,
    decided it was an environment problem rather than its own, and reported the
    task finished with two tests red.

    Asserted on the allowlist rather than by running Python in a subprocess:
    the failure is Windows-only and a POSIX runner would go green either way,
    which is exactly how this survived nine chapters."""
    assert "SYSTEMROOT" in ENV_ALLOWLIST


def test_the_allowlist_still_keeps_the_key_out() -> None:
    """The repair must not turn the allowlist into a passthrough."""
    session = ShellSession()
    assert "OPENAI_API_KEY" not in session.env


def test_the_plan_is_reported_even_when_the_model_does_not_mention_it(tmp_path: Path) -> None:
    del tmp_path
    plan = TaskPlan()
    plan.steps = (PlanStep("a", "completed"), PlanStep("b", "pending"))
    assert plan.describe() == "plan: 1/2 step(s) completed, 0 update(s)"
    assert plan.render() == "[x] a\n[ ] b"


def test_an_empty_plan_describes_itself_as_absent() -> None:
    assert TaskPlan().describe() == "plan: none"
    assert TaskPlan().render() == "(no plan)"


def test_the_prompt_paragraph_only_appears_when_the_tool_does() -> None:
    """Measured twice, and the second measurement is the one that made this a
    shipped paragraph rather than an experimental arm: a real CLI run with the
    tool wired in and nothing said about it printed `[plan: none]` after twelve
    turns.

    Conditional on the tool existing, for F05-10's reason -- chapter 5 wrote a
    prompt that named a tool the current policy did not provide, and the model
    called it 2/3."""
    from minicodex.__main__ import _instructions

    session = Session()
    with_tool = _instructions(session, plan_toolset(TaskPlan()))
    without = _instructions(session, ToolSet(handlers={}, schemas=[]))

    assert PLAN_INSTRUCTIONS in with_tool
    assert PLAN_INSTRUCTIONS not in without
    assert PLAN_INSTRUCTIONS not in _instructions(session)
    # Volatile last: the permission block is the part `request_permissions`
    # rewrites mid-session, so it stays at the end (F13-07).
    assert with_tool.index(PLAN_INSTRUCTIONS) < with_tool.index("sandbox_mode")
