"""What chapter 11 measured, and how.

Run one section at a time:

    uv run python probe_plan.py naive        # the fifteen-line version, real API
    uv run python probe_plan.py drift        # F11-01, real API, 5 samples x 2 arms
    uv run python probe_plan.py grain        # F11-02 / F11-03, real API
    uv run python probe_plan.py stale        # F11-04, real API
    uv run python probe_plan.py theatre      # F11-05, real API
    uv run python probe_plan.py evidence     # F11-08, real API
    uv run python probe_plan.py winddown     # F11-07, real API
    uv run python probe_plan.py restate      # F11-06, real API
    uv run python probe_plan.py compact      # unlisted, no network

Anything with "real API" costs money and needs OPENAI_API_KEY.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

import httpx

from minicodex import system_prompt
from minicodex.agent import Agent, RunResult
from minicodex.approval import AllowAll, Session, permissions_block
from minicodex.model import OPENAI_BASE_URL, ChatCompletionsModel
from minicodex.shell import ShellSession
from minicodex.tools import TOOL_SCHEMAS, default_tools

MODEL = "gpt-4o-mini"
ROOT = Path(__file__).resolve().parent
SAMPLES = 5


def _key() -> str:
    key = os.environ.get("OPENAI_API_KEY")
    if not key:
        print("OPENAI_API_KEY is not set; this section needs it.", file=sys.stderr)
        raise SystemExit(1)
    return key


def _model(tools: list[dict[str, Any]]) -> ChatCompletionsModel:
    return ChatCompletionsModel(base_url=OPENAI_BASE_URL, model=MODEL, api_key=_key(), tools=tools)


async def _retry(make: Callable[[], Any], attempts: int = 4) -> Any:
    for attempt in range(attempts):
        try:
            return await make()
        except (httpx.ConnectError, httpx.RemoteProtocolError, httpx.ReadError) as exc:
            if attempt == attempts - 1:
                raise
            print(f"    (retrying after {type(exc).__name__})", file=sys.stderr)
            await asyncio.sleep(2 * (attempt + 1))


# ---------------------------------------------------------------------------
# the workspace every section works in
# ---------------------------------------------------------------------------

CALC = '''"""A very small calculator."""


def add(a, b):
    return a + b


def multiply(a, b):
    return a * b
'''

TEST_CALC = """from calc import add, multiply


def test_add():
    assert add(2, 3) == 5


def test_multiply():
    assert multiply(2, 3) == 6
"""

README = """# calc

Operations:

- add
- multiply
"""

TASK = (
    "Extend the calculator in this repository. Do all five of these:\n"
    "1. Add a subtract(a, b) function to calc.py.\n"
    "2. Add a divide(a, b) function to calc.py that raises "
    "ValueError when b is 0.\n"
    "3. Add a test for subtract to test_calc.py.\n"
    "4. Add a test to test_calc.py that checks divide raises ValueError on a zero divisor.\n"
    "5. Update README.md so its list of operations names all four.\n"
    "Finally run `python -m pytest test_calc.py -q` and make sure it passes."
)


# The same five requirements, said the way a person says them.  `TASK` is a
# numbered list, and a numbered list is already a plan -- measuring "does a
# plan help" against it measures nothing, because the model can copy the
# question into the answer.  This is the version where the decomposition has
# to be done by somebody.
VAGUE_TASK = (
    "Bring the calculator up to scratch. It should support all four basic "
    "arithmetic operations, dividing by zero has to raise ValueError instead "
    "of blowing up, every operation needs a test, and the README should not "
    "lie about what the thing does. `python -m pytest test_calc.py -q` has to "
    "pass when you are finished."
)


def _workspace() -> Path:
    work = Path(tempfile.mkdtemp(prefix="ch11_"))
    (work / "calc.py").write_text(CALC, encoding="utf-8")
    (work / "test_calc.py").write_text(TEST_CALC, encoding="utf-8")
    (work / "README.md").write_text(README, encoding="utf-8")
    return work


def _score(work: Path) -> dict[str, bool]:
    """Which of the five requirements are true of the files on disk.

    Deterministic, and deliberately generous: anything that looks like the
    requirement counts.  The question is not whether the model wrote good
    code, it is whether it remembered that the requirement existed.
    """
    calc = (work / "calc.py").read_text(encoding="utf-8")
    tests = (work / "test_calc.py").read_text(encoding="utf-8")
    readme = (work / "README.md").read_text(encoding="utf-8")
    passing = subprocess.run(
        [sys.executable, "-m", "pytest", "test_calc.py", "-q", "-p", "no:cacheprovider"],
        cwd=work,
        capture_output=True,
        text=True,
    )
    if passing.returncode != 0 and os.environ.get("CH11_DEBUG"):
        print(passing.stdout[-2000:])
        print(passing.stderr[-2000:])
    # `returncode == 0` on its own is a free point: the two tests that shipped
    # with the fixture pass, so a run that did nothing at all scores it.  The
    # first version of this scorer had that hole and two samples walked
    # straight through it -- one turn, zero tool calls, "green".
    ran = 0
    for word in passing.stdout.split():
        if word == "passed" or word == "passed,":
            break
        if word.isdigit():
            ran = int(word)
    return {
        "subtract": "def subtract" in calc,
        "divide": "def divide" in calc and "ValueError" in calc,
        "test_subtract": "subtract" in tests,
        "test_divide": "divide" in tests and "ValueError" in tests,
        "readme": "subtract" in readme and "divide" in readme,
        "green": passing.returncode == 0 and ran >= 4,
    }


def _instructions(session: Session, extra: str = "") -> str:
    block = permissions_block(session, can_request=False)
    head = system_prompt().rstrip()
    if extra:
        head = f"{head}\n\n{extra}"
    return f"{head}\n\n{block}"


# ---------------------------------------------------------------------------
# the plan tool, in its first form: a list of steps and a status each
# ---------------------------------------------------------------------------

PLAN_SCHEMA: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "update_plan",
        "description": (
            "Record or update the step-by-step plan for the current task. "
            "Provide the whole plan every time, with a status for each step."
        ),
        "parameters": {
            "type": "object",
            "required": ["plan"],
            "properties": {
                "explanation": {
                    "type": "string",
                    "description": "Optional explanation for this plan update.",
                },
                "plan": {
                    "type": "array",
                    "description": "The list of steps.",
                    "items": {
                        "type": "object",
                        "required": ["step", "status"],
                        "properties": {
                            "step": {"type": "string", "description": "Task step text."},
                            "status": {
                                "type": "string",
                                "enum": ["pending", "in_progress", "completed"],
                                "description": "Step status.",
                            },
                        },
                    },
                },
            },
        },
    },
}


class Recording:
    """The naive plan tool: accept anything, answer `Plan updated`.

    This is codex's handler, near enough: it parses the arguments, emits an
    event for the UI, and returns a fixed string.  Nothing is validated and
    nothing is fed back to the model.  Everything this chapter adds is a
    reaction to something measured against this version.
    """

    def __init__(self) -> None:
        from minicodex.plan import TaskPlan

        self.updates: list[list[dict[str, str]]] = []
        self.explanations: list[str] = []
        self.rejected: list[str] = []
        self.task_plan = TaskPlan()
        self._real: Any = None

    def wrap(self, handler: Any) -> None:
        self._real = handler

    async def __call__(self, args: dict[str, Any]) -> str:
        plan = args.get("plan")
        if isinstance(plan, list):
            self.updates.append([dict(item) for item in plan])
            self.explanations.append(str(args.get("explanation") or ""))
        if self._real is not None:
            answer = await self._real(args)
            if answer.startswith("Error"):
                self.rejected.append(answer)
            return answer
        if not isinstance(plan, list):
            return "Error: plan must be a list of steps."
        return "Plan updated"


def _describe(plan: Sequence[dict[str, str]]) -> str:
    marks = {"pending": "[ ]", "in_progress": "[>]", "completed": "[x]"}
    return "\n".join(
        f"      {marks.get(item.get('status', ''), '[?]')} {item.get('step', '')}" for item in plan
    )


async def _run(
    work: Path,
    *,
    with_plan: bool,
    task: str = TASK,
    max_turns: int = 14,
    extra_instructions: str = "",
    real_tool: bool = False,
    on_stop: Any = None,
) -> tuple[RunResult, Recording, list[str]]:
    """One sample: a real agent, real files, optionally the plan tool.

    `real_tool` swaps the fifteen-line recorder for the module this chapter
    builds, so the second half of the measurements run against the thing that
    ships rather than against the sketch.
    """
    session = Session(mode="workspace-write", approver=AllowAll())
    shell = ShellSession()
    shell.cwd = str(work)
    tools = default_tools(root=work, session=session, shell=shell)
    schemas = list(TOOL_SCHEMAS)
    recording = Recording()
    if with_plan:
        if real_tool:
            from minicodex.plan import PLAN_SCHEMA as REAL_SCHEMA
            from minicodex.plan import plan_toolset

            built = plan_toolset(recording.task_plan)
            tools = {**tools, **built.handlers}
            schemas = [*schemas, REAL_SCHEMA]
            recording.wrap(built.handlers["update_plan"])
            tools["update_plan"] = recording
        else:
            tools = {**tools, "update_plan": recording}
            schemas = [*schemas, PLAN_SCHEMA]

    called: list[str] = []
    for name, fn in list(tools.items()):
        tools[name] = _watch(name, fn, called, recording if real_tool else None)

    agent = Agent(
        _model(schemas),
        tools,
        max_turns=max_turns,
        instructions=_instructions(session, extra_instructions),
        on_stop=on_stop(recording.task_plan) if on_stop is not None else None,
    )
    result = await agent.run(task)
    return result, recording, called


def _watch(name: str, fn: Any, log: list[str], recording: Recording | None = None) -> Any:
    async def wrapped(args: dict[str, Any]) -> str:
        log.append(name)
        if recording is not None:
            recording.task_plan.record_work(name)
        return await fn(args)

    return wrapped


# ---------------------------------------------------------------------------
# naive: see it move
# ---------------------------------------------------------------------------


async def naive() -> None:
    work = _workspace()
    try:
        result, recording, called = await _run(work, with_plan=True)
        print(result.final_text.strip()[:600])
        print(f"\n[{result.stop_reason} after {result.turns_used} turn(s)]")
        print(f"tool calls: {called}")
        for index, plan in enumerate(recording.updates, 1):
            print(f"\n  update {index}:")
            print(_describe(plan))
        print("\nrequirements:", _score(work))
    finally:
        shutil.rmtree(work, ignore_errors=True)


# ---------------------------------------------------------------------------
# F11-01: does a plan change what gets finished?
# ---------------------------------------------------------------------------


# codex's own wording, trimmed to what applies here.  Chapter 13 takes the
# whole prompt apart; this is the one paragraph that decides whether the tool
# built in this chapter is ever called.
USE_THE_PLAN = (
    "You have access to an `update_plan` tool which tracks steps and progress "
    "and renders them to the user. Use it for any task with more than one "
    "part. To create a plan, call `update_plan` with a short list of steps, "
    "each with a status (`pending`, `in_progress`, or `completed`). Keep it "
    "current: mark each finished step `completed` and the next one "
    "`in_progress` as you go. There should always be exactly one `in_progress` "
    "step until everything is done."
)


async def drift() -> None:
    arms = (
        ("no plan tool", False, ""),
        ("plan, silent", True, ""),
        ("plan, told", True, USE_THE_PLAN),
    )
    for label, with_plan, extra in arms:
        totals: list[dict[str, bool]] = []
        for sample in range(SAMPLES):
            work = _workspace()
            try:
                result, recording, called = await _retry(
                    lambda w=work, p=with_plan, e=extra: _run(
                        w, with_plan=p, task=VAGUE_TASK, extra_instructions=e
                    )
                )
                score = _score(work)
                totals.append(score)
                done = sum(1 for value in score.values() if value)
                work_calls = [c for c in called if c != "update_plan"]
                print(
                    f"  {label:<13} sample {sample + 1}: {done}/6  "
                    f"{result.stop_reason:<10} {result.turns_used:>2} turns  "
                    f"{len(work_calls):>2} work calls  "
                    f"{len(recording.updates)} plan updates  "
                    f"missing={[k for k, v in score.items() if not v]}"
                )
            finally:
                shutil.rmtree(work, ignore_errors=True)
        keys = list(totals[0])
        print(f"  {label}: per requirement")
        for key in keys:
            hits = sum(1 for score in totals if score[key])
            print(f"      {key:<14} {hits}/{SAMPLES}")
        print()


# ---------------------------------------------------------------------------
# F11-02 / F11-03: what granularity does it choose
# ---------------------------------------------------------------------------

GRAIN_GUIDANCE = (
    "When you use update_plan, write 3 to 7 steps. Each step is one short "
    "phrase of no more than 7 words, and each one has to be something you "
    "could show is done. Do not make a step for reading a single file."
)


async def grain() -> None:
    for label, extra in (("no guidance", ""), ("guidance", GRAIN_GUIDANCE)):
        for sample in range(3):
            work = _workspace()
            try:
                _, recording, _ = await _retry(
                    lambda w=work, e=extra: _run(w, with_plan=True, extra_instructions=e)
                )
                if not recording.updates:
                    print(f"  {label:<12} sample {sample + 1}: no plan at all")
                    continue
                first = recording.updates[0]
                words = [len(item.get("step", "").split()) for item in first]
                print(
                    f"  {label:<12} sample {sample + 1}: {len(first)} steps, "
                    f"{sum(words) / len(words):.1f} words/step "
                    f"(min {min(words)}, max {max(words)})"
                )
                print(_describe(first))
            finally:
                shutil.rmtree(work, ignore_errors=True)
        print()


# ---------------------------------------------------------------------------
# F11-04: reality moves; does the plan
# ---------------------------------------------------------------------------

STALE_TASK = (
    "Extend the calculator in this repository. Do all three of these:\n"
    "1. Add a subtract(a, b) function to calc.py.\n"
    "2. Add a divide(a, b) function to divide.py.\n"
    "3. Update README.md so its list of operations names all four.\n"
    "Make a plan with update_plan before you start."
)


async def stale() -> None:
    """Step 2 of the plan names a file that does not exist.

    The model will make a three-step plan, discover that `divide.py` is not
    there, and have to do something else.  The question is only whether the
    plan on record still says what it said at the start.
    """
    for sample in range(SAMPLES):
        work = _workspace()
        try:
            result, recording, _ = await _retry(
                lambda w=work: _run(w, with_plan=True, task=STALE_TASK, max_turns=12)
            )
            if not recording.updates:
                print(f"  sample {sample + 1}: no plan at all")
                continue
            first, last = recording.updates[0], recording.updates[-1]
            steps_changed = [i.get("step") for i in first] != [i.get("step") for i in last]
            explained = any(recording.explanations[1:])
            print(
                f"  sample {sample + 1}: {len(recording.updates)} update(s), "
                f"step text changed: {steps_changed}, explanation given: {explained}, "
                f"{result.stop_reason}"
            )
            print("    first:")
            print(_describe(first))
            print("    last:")
            print(_describe(last))
        finally:
            shutil.rmtree(work, ignore_errors=True)


# ---------------------------------------------------------------------------
# F11-05: plan updates instead of work
# ---------------------------------------------------------------------------


async def theatre() -> None:
    """How much of the turn budget goes into the plan rather than the task."""
    for sample in range(SAMPLES):
        work = _workspace()
        try:
            result, _, called = await _retry(lambda w=work: _run(w, with_plan=True))
            updates = called.count("update_plan")
            work_calls = len(called) - updates
            print(
                f"  sample {sample + 1}: {updates} plan update(s), {work_calls} work call(s), "
                f"{result.turns_used} turns, ratio {updates / max(work_calls, 1):.2f}"
            )
        finally:
            shutil.rmtree(work, ignore_errors=True)


# ---------------------------------------------------------------------------
# F11-08: completed without having been done
# ---------------------------------------------------------------------------


async def evidence() -> None:
    """The baseline for F11-08: the naive tool, nothing checked.

    Same task and same instructions as the `nudge` arms, so the three are
    comparable: this one accepts every update, `nudge`'s first arm accepts
    only updates with work behind them, and its second arm also asks a
    question before the run is allowed to end.
    """
    lies = 0
    for sample in range(SAMPLES):
        work = _workspace()
        try:
            result, recording, _ = await _retry(
                lambda w=work: _run(
                    w, with_plan=True, task=VAGUE_TASK, extra_instructions=USE_THE_PLAN
                )
            )
            score = _score(work)
            if not recording.updates:
                print(f"  sample {sample + 1}: no plan at all")
                continue
            last = recording.updates[-1]
            claimed = sum(1 for item in last if item.get("status") == "completed")
            open_steps = len(last) - claimed
            lying = open_steps == 0 and not all(score.values())
            lies += lying
            print(
                f"  sample {sample + 1}: plan claims {claimed}/{len(last)} completed, "
                f"disk says {sum(score.values())}/6, {result.stop_reason} after "
                f"{result.turns_used} turns, plan-claims-done-but-is-not: {lying}"
            )
            print(_describe(last))
            print(f"    missing on disk: {[k for k, v in score.items() if not v]}")
        finally:
            shutil.rmtree(work, ignore_errors=True)
    print(f"\n  {lies}/{SAMPLES} runs ended with a plan that says done and a disk that says not")


# ---------------------------------------------------------------------------
# F11-07: the budget runs out
# ---------------------------------------------------------------------------


async def winddown() -> None:
    """A task that cannot fit in the budget, with and without a wind-down note.

    Chapter 0 already warns the model that it has two turns left.  What is
    measured here is whether that warning produces anything a user can use.
    """
    for label, turns in (("6 turns", 6),):
        for sample in range(SAMPLES):
            work = _workspace()
            try:
                result, _, called = await _retry(
                    lambda w=work, t=turns: _run(w, with_plan=True, max_turns=t)
                )
                score = _score(work)
                text = result.final_text.strip()
                print(
                    f"  {label} sample {sample + 1}: {result.stop_reason}, "
                    f"{sum(1 for v in score.values() if v)}/6 done, "
                    f"final text {len(text)} chars, "
                    f"{len(called)} calls"
                )
                print(f"    {text[:300]!r}")
            finally:
                shutil.rmtree(work, ignore_errors=True)


# ---------------------------------------------------------------------------
# F11-06: twenty turns on an invented sub-problem
# ---------------------------------------------------------------------------

DISTRACTION = (
    "The repository also contains a file called notes.md with a long list of "
    "unrelated ideas in it. Read it early."
)

NOTES = """# notes

Ideas, none of them urgent:

- the parser could be faster if it did not allocate
- we should really benchmark the tokeniser
- there is a TODO in calc.py about float precision
- someone should write a CHANGELOG
- the CI matrix is missing 3.13
- migrate to a plugin architecture
- rewrite the README in the third person
- the error messages are inconsistent
"""


async def restate() -> None:
    """Does the original goal survive a long run with a distraction in it."""
    for sample in range(SAMPLES):
        work = _workspace()
        (work / "notes.md").write_text(NOTES, encoding="utf-8")
        (work / "calc.py").write_text(
            CALC.replace("def multiply", "# TODO: float precision is wrong here\ndef multiply"),
            encoding="utf-8",
        )
        try:
            result, _, called = await _retry(
                lambda w=work: _run(
                    w,
                    with_plan=True,
                    task=f"{TASK}\n\n{DISTRACTION}",
                    max_turns=16,
                )
            )
            score = _score(work)
            print(
                f"  sample {sample + 1}: {sum(1 for v in score.values() if v)}/6, "
                f"{result.turns_used} turns, {len(called)} calls, {result.stop_reason}, "
                f"missing={[k for k, v in score.items() if not v]}"
            )
        finally:
            shutil.rmtree(work, ignore_errors=True)


# ---------------------------------------------------------------------------
# unlisted: what compaction does to a plan
# ---------------------------------------------------------------------------


def compact() -> None:
    """Offline.  Where does the plan live once the history has been cut?"""
    from minicodex.agent_types import ToolCall
    from minicodex.compaction import Sizer, SummaryRequest
    from minicodex.compaction import compact as run_compaction
    from minicodex.history import History

    steps = [
        {"step": "add subtract to calc.py", "status": "completed"},
        {"step": "add divide with ValueError", "status": "in_progress"},
        {"step": "test both", "status": "pending"},
        {"step": "update README", "status": "pending"},
    ]
    arguments = json.dumps({"plan": steps})

    history = History()
    history.add_system_note("You are a coding agent working in a user's repository.")
    history.add_user(VAGUE_TASK)
    call = ToolCall("call_1", "update_plan", {"plan": steps}, arguments)
    history.add_assistant("Here is the plan.", (call,))
    history.add_tool_result("call_1", "Plan updated")
    for index in range(2, 10):
        read = ToolCall(f"call_{index}", "read_file", {"path": "calc.py"}, "{}")
        history.add_assistant("", (read,))
        history.add_tool_result(f"call_{index}", CALC * 30)

    async def summarise(request: SummaryRequest) -> str:
        """A summariser that keeps nothing, which is the honest worst case.

        The real one is asked for six headings and none of them is "the plan".
        """
        del request
        return "## Done\n- some work happened\n\n## Remaining\n- unknown"

    sizer = Sizer()
    before = sizer.messages(history.to_wire("chat_completions"))
    result = asyncio.run(run_compaction(history, summarise=summarise, budget=2000, sizer=sizer))
    after = sizer.messages(result.history.to_wire("chat_completions"))
    print(f"before {len(history.items)} items / {before} tokens")
    print(f"after  {len(result.history.items)} items / {after} tokens, dropped {result.plan.drops}")
    survives = any(
        "subtract" in str(item) and "status" in str(item) for item in result.history.items
    )
    print(f"the plan survived compaction: {survives}")
    for item in result.history.items:
        print(f"  {type(item).__name__:<16} {str(getattr(item, 'text', '') or '')[:70]!r}")


# ---------------------------------------------------------------------------
# the fix for F11-08: the loop asks a second question before it stops
# ---------------------------------------------------------------------------


async def nudge() -> None:
    """Same task, same tool, with and without the stop check.

    Both arms run the real `plan.py`, so the refusals in `rejected` are the
    real ones.
    """
    from minicodex.plan import unfinished_note

    for label, stop in (("stop check off", None), ("stop check on", unfinished_note)):
        for sample in range(SAMPLES):
            work = _workspace()
            try:
                result, recording, called = await _retry(
                    lambda w=work, s=stop: _run(
                        w,
                        with_plan=True,
                        task=VAGUE_TASK,
                        extra_instructions=USE_THE_PLAN,
                        real_tool=True,
                        on_stop=s,
                    )
                )
                score = _score(work)
                plan = recording.task_plan
                lying = bool(plan.steps) and not plan.outstanding() and not all(score.values())
                print(
                    f"  {label:<15} sample {sample + 1}: {sum(score.values())}/6 on disk, "
                    f"plan {len(plan.steps) - len(plan.outstanding())}/{len(plan.steps)}, "
                    f"{result.turns_used} turns, {called.count('update_plan')} updates, "
                    f"{len(recording.rejected)} refused, "
                    f"plan-claims-done-but-is-not: {lying}, "
                    f"missing={[k for k, v in score.items() if not v]}"
                )
                for message in recording.rejected:
                    print(f"      refused: {message.splitlines()[0][:110]}")
            finally:
                shutil.rmtree(work, ignore_errors=True)
        print()


# ---------------------------------------------------------------------------
# one sample, with everything printed: what is eating the turns
# ---------------------------------------------------------------------------


async def trace() -> None:
    """One run of the shipping tool, every call and every answer printed.

    Written because the aggregate said something the aggregates before it did
    not: the arm using the real tool spent its whole turn budget where the arm
    using the fifteen-line sketch had finished in nine. A number that moves
    without a reason is a reason to go and look.
    """
    from minicodex.plan import unfinished_note

    work = _workspace()
    log: list[tuple[str, dict[str, Any], str]] = []
    try:
        session = Session(mode="workspace-write", approver=AllowAll())
        shell = ShellSession()
        shell.cwd = str(work)
        tools = default_tools(root=work, session=session, shell=shell)
        recording = Recording()
        from minicodex.plan import PLAN_SCHEMA as REAL_SCHEMA
        from minicodex.plan import plan_toolset

        built = plan_toolset(recording.task_plan)
        recording.wrap(built.handlers["update_plan"])
        tools["update_plan"] = recording

        def spy(name: str, fn: Any) -> Any:
            async def wrapped(args: dict[str, Any]) -> str:
                recording.task_plan.record_work(name)
                answer = await fn(args)
                log.append((name, args, answer))
                return answer

            return wrapped

        tools = {name: spy(name, fn) for name, fn in tools.items()}
        agent = Agent(
            _model([*TOOL_SCHEMAS, REAL_SCHEMA]),
            tools,
            max_turns=14,
            instructions=_instructions(session, USE_THE_PLAN),
            on_stop=unfinished_note(recording.task_plan),
        )
        result = await agent.run(VAGUE_TASK)
        for index, (name, args, answer) in enumerate(log, 1):
            shown = json.dumps(args)[:150]
            print(f"{index:>3} {name:<20} {shown}")
            print(f"    -> {answer.strip()[:220].replace(chr(10), ' | ')}")
        print(f"\n[{result.stop_reason} after {result.turns_used} turn(s)]")
        print(recording.task_plan.render())
        print(_score(work))
    finally:
        shutil.rmtree(work, ignore_errors=True)


# ---------------------------------------------------------------------------
# what the tool answers, and what that costs
# ---------------------------------------------------------------------------


async def echo() -> None:
    """`Plan updated` against the rendered plan, everything else identical.

    Written because the aggregates disagreed in a way the design had not
    predicted: the arm running the shipping tool spent 14 turns out of 14 in
    5 samples out of 5, where the arm running the fifteen-line sketch finished
    in 7 to 12. The only differences are the description and the answer, and
    this section holds the description fixed.
    """
    from minicodex import plan as plan_module

    original = plan_module.update_plan

    async def terse(plan: Any, args: dict[str, Any]) -> str:
        answer = await original(plan, args)
        return "Plan updated" if not answer.startswith("Error") else answer

    for label, handler in (("rendered plan", original), ("Plan updated", terse)):
        plan_module.update_plan = handler  # type: ignore[assignment]
        try:
            for sample in range(SAMPLES):
                work = _workspace()
                try:
                    result, recording, called = await _retry(
                        lambda w=work: _run(
                            w,
                            with_plan=True,
                            task=VAGUE_TASK,
                            extra_instructions=USE_THE_PLAN,
                            real_tool=True,
                        )
                    )
                    score = _score(work)
                    made = recording.task_plan
                    done = len(made.steps) - len(made.outstanding())
                    print(
                        f"  {label:<14} sample {sample + 1}: {sum(score.values())}/6 on disk, "
                        f"{result.stop_reason:<10} {result.turns_used:>2} turns, "
                        f"{called.count('update_plan')} updates, "
                        f"{len(called) - called.count('update_plan')} work calls, "
                        f"plan {done}/{len(made.steps)}"
                    )
                finally:
                    shutil.rmtree(work, ignore_errors=True)
        finally:
            plan_module.update_plan = original  # type: ignore[assignment]
        print()


SECTIONS: dict[str, Callable[[], Any]] = {
    "echo": echo,
    "trace": trace,
    "nudge": nudge,
    "naive": naive,
    "drift": drift,
    "grain": grain,
    "stale": stale,
    "theatre": theatre,
    "evidence": evidence,
    "winddown": winddown,
    "restate": restate,
    "compact": compact,
}


def main(argv: list[str]) -> int:
    if len(argv) != 2 or argv[1] not in SECTIONS:
        print(__doc__)
        return 1
    section = SECTIONS[argv[1]]
    if asyncio.iscoroutinefunction(section):
        asyncio.run(section())
    else:
        section()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
