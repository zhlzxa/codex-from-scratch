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
    the run holds, not a message.

    The first version of this test built a history with no `update_plan` call
    in it and a plan object nothing ever touched, then asserted the object was
    still there -- true of any object. This one puts the call in the history
    the way a real run does, checks that compaction really removes it, and
    checks that the stop question can still be asked afterwards.
    """
    from minicodex.agent_types import ToolCall

    plan = TaskPlan()
    planned = steps(("add divide", "in_progress"), ("update README", "pending"))

    history = History()
    history.add_system_note("You are a coding agent.")
    history.add_user("bring the calculator up to scratch")
    plan_call = ToolCall("call_1", "update_plan", {"plan": planned}, json.dumps({"plan": planned}))
    history.add_assistant("Here is the plan.", (plan_call,))
    history.add_tool_result("call_1", asyncio.run(update_plan(plan, {"plan": planned})))
    for index in range(2, 8):
        read = ToolCall(f"call_{index}", "read_file", {"path": "calc.py"}, "{}")
        history.add_assistant("", (read,))
        history.add_tool_result(f"call_{index}", "x" * 4000)
    assert "update README" in json.dumps(history.to_wire("chat_completions"))

    async def summarise(_request: SummaryRequest) -> str:
        return "## Done\n- work happened"

    result = asyncio.run(run_compaction(history, summarise=summarise, budget=1200, sizer=Sizer()))

    assert result.plan.drops > 0
    # The history has forgotten the plan entirely...
    assert "update README" not in json.dumps(result.history.to_wire("chat_completions"))
    # ...and the loop can still ask what is left.
    note = unfinished_note(plan)()
    assert note is not None and "[ ] update README" in note


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


def test_a_plan_describes_and_renders_itself() -> None:
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


def test_watching_keeps_the_exception_for_deferred_tools() -> None:
    """`watching()` rebuilds the `ToolSet`, so everything the old one carried
    has to be carried across by hand. Dropping `callable_without_schema` left
    every test green -- and with sixty MCP tools configured it is a program
    that refuses to start, because a deferred tool is a handler with no schema
    (interlude B). Found by mutation."""

    async def handler(_args: dict[str, Any]) -> str:
        return "ok"

    schema = {"type": "function", "function": {"name": "shown", "parameters": {}}}
    tools = ToolSet(
        handlers={"shown": handler, "hidden": handler},
        schemas=[schema],
        callable_without_schema=frozenset({"hidden"}),
    )

    watched = watching(tools, TaskPlan())

    assert watched.callable_without_schema == {"hidden"}
    assert watched.schemas is tools.schemas  # the registry's live list, not a copy


@pytest.mark.asyncio
async def test_F11_08_a_nudge_is_written_to_the_transcript(tmp_path: Path) -> None:
    """The nudge is the one message in a run that neither the user nor the
    model wrote. Without a record of it, a transcript shows a model that
    stopped and then, for no visible reason, carried on."""
    from minicodex.recorder import Recorder

    plan = TaskPlan()
    await call(plan, ("a", "pending"))
    recorder = Recorder(tmp_path / "rec.jsonl")
    agent = Agent(
        ScriptedModel(["stopping"]),
        {},
        max_turns=6,
        recorder=recorder,
        on_stop=unfinished_note(plan),
    )
    await agent.run("do a")

    assert '"nudge"' in recorder.path.read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_F11_07_a_run_that_was_cut_off_says_so_in_its_session_file(tmp_path: Path) -> None:
    """A session that ended because the budget ended has to be tellable apart,
    afterwards, from one that ended because the work did."""
    from minicodex.rollout import RolloutWriter, SessionMeta, read_rollout

    async def tool(_args: dict[str, Any]) -> str:
        return "done"

    writer = RolloutWriter(tmp_path / "s.jsonl", SessionMeta(session_id="s", created=0.0))
    try:
        agent = Agent(ScriptedModel([[("c1", "t", {})]]), {"t": tool}, max_turns=2, rollout=writer)
        await agent.run("go")
    finally:
        writer.release()

    marks = [mark["mark"] for mark in read_rollout(tmp_path / "s.jsonl").marks]
    assert "budget_exhausted" in marks


# ---------------------------------------------------------------------------
# found by the resume test below: session ids were not unique within a process
# ---------------------------------------------------------------------------


def test_two_sessions_started_in_the_same_second_get_different_ids() -> None:
    from minicodex.rollout import new_session_id

    issued = [new_session_id() for _ in range(5)]  # far faster than one a second
    assert len(set(issued)) == 5
    # Sessions are listed by sorting *file names*, so that is what has to
    # come out in creation order -- `-2.jsonl` would sort before `.jsonl`.
    names = [f"{session_id}.jsonl" for session_id in issued]
    assert names == sorted(names), "listing a directory should still list a history"


@pytest.mark.asyncio
async def test_two_sub_agents_in_the_same_second_get_a_file_each(tmp_path: Path) -> None:
    """Second-plus-pid was unique while one process meant one session. Chapter
    10 ended that, and this function was not looked at again: two children that
    finished inside one second wrote two sessions into one file, and a child
    spawned in the second its parent started hit the parent's own lock."""
    from minicodex.agent import Wiring
    from minicodex.approval import AllowAll
    from minicodex.composition import sub_context
    from minicodex.rollout import RolloutWriter, SessionMeta, new_session_id, rollout_path
    from minicodex.subagent import TaskSpec, run_task

    sessions = tmp_path / "s"
    parent_id = new_session_id()
    parent = RolloutWriter(rollout_path(parent_id, sessions), SessionMeta(parent_id, created=0.0))
    ctx = sub_context(
        build_model=lambda _schemas: ScriptedModel(["an answer"]),
        root=tmp_path,
        session=Session(mode="read-only", approver=AllowAll()),
        parent_shell=ShellSession(),
        wiring=Wiring(),
        sessions_dir=sessions,
        parent_session_id=parent_id,
    )
    try:
        first = await run_task(TaskSpec("one", expected_output="x"), ctx)
        second = await run_task(TaskSpec("two", expected_output="x"), ctx)
    finally:
        parent.release()

    assert len({parent_id, first.session_id, second.session_id}) == 3
    assert len(list(sessions.glob("*.jsonl"))) == 3


# ---------------------------------------------------------------------------
# the command line: where the plan is handed to the two places that need it
# ---------------------------------------------------------------------------


def _scripted_cli(
    monkeypatch: pytest.MonkeyPatch, turns: Sequence[Any]
) -> list[list[dict[str, Any]]]:
    """Make every model client the CLI builds replay `turns` instead of
    calling a server. Everything else in `main()` -- the loop, the tools, the
    session file -- runs for real. Returns the list the requests are logged to."""
    from minicodex.model import ChatCompletionsModel

    requests: list[list[dict[str, Any]]] = []

    async def stream(self: Any, messages: Sequence[dict[str, Any]]) -> Any:
        requests.append([dict(m) for m in messages])
        turn = turns[min(len(requests) - 1, len(turns) - 1)]
        if isinstance(turn, str):
            yield TextDelta(turn)
        else:
            for index, (call_id, name, arguments) in enumerate(turn):
                yield ToolCallDelta(
                    call_id=call_id, index=index, name=name, arguments=json.dumps(arguments)
                )
        yield Completed("stop")

    monkeypatch.setattr(ChatCompletionsModel, "stream", stream)
    return requests


HALF_DONE = steps(("add subtract", "completed"), ("update README", "pending"))


def test_the_cli_prints_the_plan_and_asks_before_stopping(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The mechanism tests above each build their own `Agent`. This one runs
    the program, because five lines in `__main__.py` each connect something
    this chapter built, and each could be deleted with every other test in
    this file green: the plan handed to the tool, the tool calls reported to
    the plan, the stop check handed to the loop, the paragraph that gets the
    tool used at all, and the list printed at the end."""
    import minicodex.__main__ as cli

    (tmp_path / "calc.py").write_text("x = 1\n", encoding="utf-8")
    started = steps(("read calc.py", "in_progress"), ("update README", "pending"))
    read_it = steps(("read calc.py", "completed"), ("update README", "pending"))
    requests = _scripted_cli(
        monkeypatch,
        [
            [("c1", "update_plan", {"plan": started})],
            [("c2", "read_file", {"path": "calc.py"})],
            [("c3", "update_plan", {"plan": read_it})],
            "that is everything",
            "really, everything",
        ],
    )
    monkeypatch.chdir(tmp_path)
    assert cli.main(["ask", "go", "--yes", "--session-dir", str(tmp_path / "s")]) == 0

    out = capsys.readouterr().out
    # The paragraph without which the tool mostly goes unused reached the model.
    assert PLAN_INSTRUCTIONS in requests[0][0]["content"]
    # The second update closed a step, which is only accepted if the read in
    # between was reported to the plan -- and both updates edited the plan the
    # end of the run prints.
    assert "[x] read calc.py\n[ ] update README" in out
    assert "[plan: 1/2 step(s) completed, 2 update(s)]" in out
    # Five model calls: plan, read, plan, the attempt to stop, and the answer
    # to the stop check.
    assert "completed after 5 turn(s)" in out


def test_the_cli_counts_a_remote_tool_call_as_work(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """`watching()` has to wrap the table *after* the MCP tools are merged in.

    That ordering lives in one line of `__main__.py`. Wrapped one step earlier,
    everything works and a remote call silently stops counting: a step whose
    only evidence is an MCP call can then never be marked completed. Recorded
    as an open item when this chapter was first written; this is the test that
    was missing from it.
    """
    import sys

    import minicodex.__main__ as cli

    server = Path(__file__).resolve().parent.parent / "mcp_servers" / "notes_server.py"
    config = tmp_path / "mcp.json"
    config.write_text(
        json.dumps({"servers": {"notes": {"command": [sys.executable, str(server)]}}}),
        encoding="utf-8",
    )
    started = steps(("look in the notes", "in_progress"), ("write it up", "pending"))
    looked = steps(("look in the notes", "completed"), ("write it up", "pending"))
    _scripted_cli(
        monkeypatch,
        [
            [("c1", "update_plan", {"plan": started})],
            [("c2", "mcp__notes__search", {"query": "tool"})],
            [("c3", "update_plan", {"plan": looked})],
            "done",
            "done",
        ],
    )
    monkeypatch.chdir(tmp_path)
    argv = ["ask", "go", "--yes", "--mcp", str(config), "--session-dir", str(tmp_path / "s")]
    assert cli.main(argv) == 0

    assert "[x] look in the notes" in capsys.readouterr().out


def test_a_resumed_session_still_has_its_plan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The plan is an object so that compaction cannot delete it. An object
    does not survive the process either.

    Before `restore_plan`, `--resume` built an empty `TaskPlan`: the model
    could still read its old plan in the history, and the harness had none --
    `[plan: none]`, no stop check, and a first update that was not held to the
    evidence rule because it looked like the first. Found by resuming a
    session that had a plan and reading the last line.
    """
    import minicodex.__main__ as cli

    sessions = str(tmp_path / "s")
    monkeypatch.chdir(tmp_path)
    _scripted_cli(monkeypatch, [[("c1", "update_plan", {"plan": HALF_DONE})], "stopping", "stop"])
    assert cli.main(["ask", "go", "--yes", "--session-dir", sessions]) == 0
    capsys.readouterr()

    _scripted_cli(monkeypatch, ["nothing more to do", "still nothing"])
    assert (
        cli.main(["ask", "carry on", "--yes", "--session-dir", sessions, "--resume", "last"]) == 0
    )

    out = capsys.readouterr().out
    assert "[plan: 1/2 step(s) completed, 1 update(s)]" in out
    assert "[ ] update README" in out
    assert "completed after 2 turn(s)" in out  # it was asked about the open step


def test_restoring_a_plan_replays_only_the_updates_that_were_accepted() -> None:
    from minicodex.agent_types import ToolCall
    from minicodex.plan import restore_plan

    def plan_call(call_id: str, *pairs: tuple[str, str]) -> ToolCall:
        arguments = {"plan": steps(*pairs)}
        return ToolCall(call_id, "update_plan", arguments, json.dumps(arguments))

    history = History()
    history.add_user("do it")
    history.add_assistant("", (plan_call("c1", ("a", "in_progress"), ("b", "pending")),))
    history.add_tool_result("c1", "Plan updated.\n[>] a\n[ ] b\n2 step(s) to go.")
    history.add_assistant("", (plan_call("c2", ("a", "completed"), ("b", "in_progress")),))
    history.add_tool_result("c2", "Error: marking ['a'] completed, but nothing has run ...")
    read = ToolCall("c3", "read_file", {"path": "x"}, "{}")
    history.add_assistant("", (read,))
    history.add_tool_result("c3", "contents")

    plan = TaskPlan()
    restore_plan(plan, history)

    assert plan.render() == "[>] a\n[ ] b"  # the refused update did not happen
    assert plan.updates == 1
    assert plan.work_since_update == 1  # the read after it still counts


def test_restoring_from_a_history_with_no_plan_leaves_it_empty() -> None:
    from minicodex.plan import restore_plan

    history = History()
    history.add_user("hello")
    plan = TaskPlan()
    restore_plan(plan, history)
    assert plan.steps == () and plan.updates == 0
