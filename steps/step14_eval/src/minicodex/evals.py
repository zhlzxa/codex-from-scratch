"""A task set, and what "it worked" means for each task.

Every earlier chapter measured *one* thing at a time: one sentence, one task,
three samples, an arm and a baseline.  That is how each of those chapters
reached a defensible conclusion, and it is also why chapter 13's shipped
sentence is a risk -- it was measured on the one task it was designed for.  A
change to the system prompt, or to compaction, or to memory later on, changes
**every** session.  Averaged over one task, "no harm" means nothing.

So: several tasks, run end to end, scored by what the agent *did*.

Three decisions worth stating before the code.

**Checks read the trajectory, not the prose.**  `"subtract" in final_text` is
the assertion everybody writes first and it measures the model's vocabulary.
`Trajectory` records what was called, with what arguments, and what the
workspace looked like afterwards; a task passes when the file on disk says so.
`answer_has()` exists anyway, is used by exactly one task, and is labelled --
some things really are claims about the answer, and the honest way to have one
is to know that it is one.

**A task owns its workspace and nothing else.**  `files` is the whole content
of the directory the agent is pointed at.  Not a fixture directory in this
repository, and not `tmp_path` with something already in it: chapters 16 and 17
add a memory the agent can read, and an eval whose workspace contains the
answer -- or the test that checks the answer -- measures the search path rather
than the agent (F14-08).  The isolation is asserted, not assumed.

**No `EvalRunner` class.**  A task is data, running one is a function, and
comparing two sets of results is a second function.  There is one caller shape
(a probe) and one test shape, which is not three (FB-03).
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from minicodex.agent import Model, Wiring
from minicodex.approval import AllowAll, Session
from minicodex.composition import local_tools


@dataclass(frozen=True)
class Trajectory:
    """What one run did, in the terms a check is allowed to ask about."""

    calls: tuple[tuple[str, dict[str, Any]], ...]
    final_text: str
    stop_reason: str
    turns: int
    files: Mapping[str, str]

    def called(self, name: str) -> bool:
        return any(call == name for call, _ in self.calls)

    def count(self, name: str) -> int:
        return sum(1 for call, _ in self.calls if call == name)

    def read(self, path: str) -> str:
        return self.files.get(path, "")

    def describe(self) -> str:
        return " -> ".join(name for name, _ in self.calls) or "(no tool calls)"


@dataclass(frozen=True)
class Check:
    """One yes/no question about a trajectory, with a name a report can print."""

    name: str
    holds: Callable[[Trajectory], bool]


def calls(name: str) -> Check:
    return Check(f"calls {name}", lambda t: t.called(name))


def never_calls(name: str) -> Check:
    return Check(f"never calls {name}", lambda t: not t.called(name))


def file_has(path: str, needle: str) -> Check:
    return Check(f"{path} contains {needle!r}", lambda t: needle in t.read(path))


def file_lacks(path: str, needle: str) -> Check:
    return Check(f"{path} does not contain {needle!r}", lambda t: needle not in t.read(path))


def file_is(path: str, content: str) -> Check:
    return Check(f"{path} is unchanged", lambda t: t.read(path) == content)


def finished() -> Check:
    return Check("ran out of neither turns nor patience", lambda t: t.stop_reason == "completed")


def answer_has(needle: str) -> Check:
    """A check on the model's words.

    Kept, used once, and named so that it is visible in a report as the one
    row that can go red because a model changed its phrasing.  F14-01 is not
    "never assert on text"; it is "know which of your assertions are about
    text, because those are the ones that will lie to you".
    """
    return Check(f"answer mentions {needle!r}", lambda t: needle.lower() in t.final_text.lower())


def asks_a_question() -> Check:
    return Check("ends by asking a question", lambda t: "?" in t.final_text)


@dataclass(frozen=True)
class Task:
    name: str
    files: Mapping[str, str]
    question: str
    checks: tuple[Check, ...]
    mode: str = "workspace-write"
    max_turns: int = 8


def _materialise(workspace: Path, files: Mapping[str, str]) -> None:
    workspace.mkdir(parents=True, exist_ok=True)
    for name, content in files.items():
        path = workspace / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")


def _snapshot(workspace: Path) -> dict[str, str]:
    return {
        path.relative_to(workspace).as_posix(): path.read_text(encoding="utf-8", errors="replace")
        for path in sorted(workspace.rglob("*"))
        if path.is_file()
    }


@dataclass(frozen=True)
class Result:
    task: Task
    trajectory: Trajectory
    passed: tuple[str, ...]
    failed: tuple[str, ...]
    error: str | None = None

    @property
    def ok(self) -> bool:
        return not self.failed and self.error is None


async def run_task(
    task: Task,
    build_model: Callable[[list[dict[str, Any]]], Model],
    *,
    workspace: Path,
    instructions: str | None = None,
    wiring: Wiring | None = None,
) -> Result:
    """Run one task in a directory containing exactly what the task declared.

    `build_model` is a factory rather than a model because the tool schemas are
    not known until the tool set is built, and chapter 9 made a client carry
    its own tool list.  The same factory shape `__main__` uses, so an eval and
    a real run differ in their arguments and not in their assembly.
    """
    # Through `to_thread`, for chapter -1's rule (F00-09, `ASYNC240`): the
    # linter does not know this program runs one task at a time, and the day
    # somebody runs the set concurrently is not the day to find out.
    await asyncio.to_thread(_materialise, workspace, task.files)

    session = Session(mode=task.mode, approver=AllowAll())
    tools = local_tools(workspace, session)
    wiring = wiring or Wiring()
    agent = wiring.agent(
        build_model(tools.schemas),
        tools,
        max_turns=task.max_turns,
        instructions=instructions,
    )

    error: str | None = None
    calls_made: list[tuple[str, dict[str, Any]]] = []
    final_text, stop_reason, turns = "", "error", 0
    try:
        outcome = await agent.run(task.question)
    except Exception as exc:  # a failed run is a data point, not a crashed probe
        error = f"{type(exc).__name__}: {exc}"
    else:
        final_text, stop_reason, turns = (
            outcome.final_text,
            outcome.stop_reason,
            outcome.turns_used,
        )
        for item in outcome.history.items:
            for call in getattr(item, "tool_calls", ()) or ():
                calls_made.append((call.name, call.arguments or {}))

    trajectory = Trajectory(
        calls=tuple(calls_made),
        final_text=final_text,
        stop_reason=stop_reason,
        turns=turns,
        files=await asyncio.to_thread(_snapshot, workspace),
    )
    passed = tuple(c.name for c in task.checks if error is None and c.holds(trajectory))
    failed = tuple(c.name for c in task.checks if c.name not in passed)
    return Result(task, trajectory, passed, failed, error)


@dataclass
class Arm:
    """One configuration, run over the whole task set some number of times."""

    name: str
    results: list[Result] = field(default_factory=list)

    def rate(self, task: str) -> tuple[int, int]:
        rows = [r for r in self.results if r.task.name == task]
        return sum(1 for r in rows if r.ok), len(rows)

    @property
    def total(self) -> tuple[int, int]:
        return sum(1 for r in self.results if r.ok), len(self.results)


def table(arms: Sequence[Arm], tasks: Sequence[Task]) -> str:
    """Per task, per arm.  Never only the total.

    The total is the number that hides the thing worth knowing: two arms at
    18/24 can disagree about every task in the set.  F14-07 is that sentence.
    """
    width = max(len(t.name) for t in tasks) + 2
    lines = ["  " + "task".ljust(width) + "  ".join(a.name.rjust(12) for a in arms)]
    for task in tasks:
        cells = []
        for arm in arms:
            ok, total = arm.rate(task.name)
            cells.append(f"{ok}/{total}".rjust(12))
        lines.append("  " + task.name.ljust(width) + "  ".join(cells))
    totals = []
    for arm in arms:
        ok, total = arm.total
        totals.append(f"{ok}/{total}".rjust(12))
    lines.append("  " + "TOTAL".ljust(width) + "  ".join(totals))
    return "\n".join(lines)


def regressions(before: Arm, after: Arm, tasks: Sequence[Task]) -> list[str]:
    """Tasks the change made worse, whatever it did to the average.

    This function is the whole point of the module.  A change that takes the
    total from 18/24 to 20/24 by fixing three tasks and breaking one is not
    "an improvement"; it is a trade, and somebody has to be told which trade.
    """
    worse = []
    for task in tasks:
        was, _ = before.rate(task.name)
        now, _ = after.rate(task.name)
        if now < was:
            worse.append(f"{task.name}: {was} -> {now}")
    return worse


def failures(arm: Arm) -> list[str]:
    """Every failed check, with the trajectory that produced it."""
    lines = []
    for result in arm.results:
        if result.ok:
            continue
        detail = result.error or ", ".join(result.failed)
        lines.append(f"  {result.task.name}: {detail}\n      {result.trajectory.describe()}")
    return lines


# ---------------------------------------------------------------------------
# The task set.  Hand-written, in the repository, versioned with the code.
#
# Six tasks, chosen so that the *first* thing a change to the system prompt can
# damage is represented: three where acting immediately is right, one where
# asking is right, one where the work is verification rather than editing, and
# one where the correct answer is to leave something alone.  A task set that is
# six variations of "edit this file" measures one behaviour six times.
# ---------------------------------------------------------------------------

CALC = """def add(a, b):
    return a + b


def multiply(a, b):
    result = a * b
    return result
"""

TEST_CALC = """from calc import add, multiply


def test_add():
    assert add(1, 2) == 3


def test_multiply():
    assert multiply(2, 3) == 6
"""

BROKEN = """def slugify(title):
    return title.lower().replace(" ", "_")
"""

TEST_BROKEN = """from slug import slugify


def test_slugify():
    assert slugify("Hello World") == "hello-world"
"""

TASKS: tuple[Task, ...] = (
    Task(
        name="add-function",
        files={"calc.py": CALC, "test_calc.py": TEST_CALC},
        question="add a subtract function to calc.py",
        checks=(
            calls("apply_patch"),
            file_has("calc.py", "def subtract"),
            file_has("calc.py", "def multiply"),
            finished(),
        ),
    ),
    Task(
        name="fix-failing-test",
        files={"slug.py": BROKEN, "test_slug.py": TEST_BROKEN},
        question="test_slug.py fails. Make it pass without changing the test.",
        checks=(
            file_has("slug.py", '"-"'),
            file_is("test_slug.py", TEST_BROKEN),
            finished(),
        ),
    ),
    Task(
        name="read-and-answer",
        files={"calc.py": CALC},
        question="which functions does calc.py define?",
        checks=(
            calls("read_file"),
            never_calls("apply_patch"),
            answer_has("multiply"),
            finished(),
        ),
    ),
    Task(
        name="leave-it-alone",
        files={"calc.py": CALC, "test_calc.py": TEST_CALC},
        question="add a docstring to the add function in calc.py. Change nothing else.",
        checks=(
            file_has("calc.py", '"""'),
            file_has("calc.py", "result = a * b"),
            file_is("test_calc.py", TEST_CALC),
            finished(),
        ),
    ),
    Task(
        name="ambiguous",
        files={"calc.py": CALC},
        question="add input validation to calc.py",
        checks=(asks_a_question(), never_calls("apply_patch"), finished()),
    ),
    Task(
        name="two-files",
        files={"calc.py": CALC, "test_calc.py": TEST_CALC},
        question="rename multiply to times everywhere, including the test",
        checks=(
            file_has("calc.py", "def times"),
            file_lacks("calc.py", "def multiply"),
            file_has("test_calc.py", "times("),
            finished(),
        ),
    ),
)


def by_name(name: str) -> Task:
    for task in TASKS:
        if task.name == name:
            return task
    raise KeyError(name)


def declared_files() -> set[str]:
    """Every path any task writes.  F14-08's assertion reads this."""
    return {name for task in TASKS for name in task.files}


def as_json() -> str:
    """The task set as data, for a diff to read when somebody edits it."""
    return json.dumps(
        [
            {
                "name": t.name,
                "question": t.question,
                "files": sorted(t.files),
                "checks": [c.name for c in t.checks],
            }
            for t in TASKS
        ],
        indent=2,
        ensure_ascii=False,
    )
