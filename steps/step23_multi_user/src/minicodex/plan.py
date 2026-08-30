"""A checklist the model keeps, and the two things the loop does with it.

The tool is small -- a list of steps, a status each, replaced whole on every
update.  Almost everything in this module is there because of something the
tool cannot do on its own.

**Why it is called `TaskPlan` and not `Plan`.**  `compaction.Plan` already
exists and means "where to cut the history".  Two unrelated plans in one
package is how a reader ends up reading the wrong docstring, and codex has the
same collision: its `update_plan` tool is a checklist, its *Plan mode* is a
different feature entirely, and the handler for the first one carries the line

    "update_plan is a TODO/checklist tool and is not allowed in Plan mode"

which exists because somebody confused them.

**What the harness gets out of it that the model does not.**  A plan is the
only thing in this program that states what "finished" means before the model
decides it is finished.  Chapter 0 defined the end of a run as "the model
stopped asking for tools"; with a plan on record the loop can ask a second
question -- *is anything still outstanding* -- and that question is answerable
in code, which is the whole of `outstanding()`.

**What is deliberately not enforced.**  "This step is done" is a claim about
the world, and this module cannot check it: the step says "update the README"
and nothing here knows what the README should say.  What *is* checked is the
one part that is a fact about the transcript rather than about the world --
whether any work happened at all between the plan being written and a step
being marked done.  That catches the empty case and nothing else, and it says
so.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Literal, get_args

from minicodex.agent_types import ToolSet
from minicodex.tool_errors import tool_error

StepStatus = Literal["pending", "in_progress", "completed"]

STATUSES: tuple[str, ...] = get_args(StepStatus)

# What the terminal and the model both see.  Deliberately the same characters
# in both places: a user comparing what was printed with what the model was
# told should not have to translate.
_MARK = {"pending": "[ ]", "in_progress": "[>]", "completed": "[x]"}

# Above this, a "plan" is a transcript of keystrokes rather than a plan, and
# every step costs tokens on every update because the whole list is re-sent.
MAX_STEPS = 12


@dataclass(frozen=True)
class PlanStep:
    text: str
    status: StepStatus


@dataclass
class TaskPlan:
    """One conversation's checklist.

    Mutable, and per conversation rather than per process -- the same shape and
    the same reason as chapter 5's `Session`, which is the other mutable thing
    in `ToolContext`.  A module-level plan would be shared by every agent in
    the process, including sub-agents, which chapter 10 spent a section
    explaining is not what a child wants.
    """

    steps: tuple[PlanStep, ...] = ()
    updates: int = 0
    #: Non-plan tool calls since the last accepted update.  The only evidence
    #: this module has, and it is evidence about the transcript, not about the
    #: work.  See `update_plan`.
    work_since_update: int = 0
    #: Every version of the plan, oldest first, rendered.  Kept because a plan
    #: that silently changes shape mid-run is F11-04, and the only way to see
    #: that from outside is to have both versions.
    revisions: list[str] = field(default_factory=list)

    def record_work(self, name: str) -> None:
        if name != "update_plan":
            self.work_since_update += 1

    def outstanding(self) -> tuple[PlanStep, ...]:
        return tuple(step for step in self.steps if step.status != "completed")

    def render(self) -> str:
        if not self.steps:
            return "(no plan)"
        return "\n".join(f"{_MARK[s.status]} {s.text}" for s in self.steps)

    def describe(self) -> str:
        """One line for the end of a run.

        Printed whether or not anything is outstanding: a plan that is
        complete is the case where the user most wants to see the list, and
        a plan that is not is the case where the harness knows something the
        model's own summary may not mention.
        """
        if not self.steps:
            return "plan: none"
        done = len(self.steps) - len(self.outstanding())
        return f"plan: {done}/{len(self.steps)} step(s) completed, {self.updates} update(s)"


def _parse(raw: Any) -> tuple[tuple[PlanStep, ...] | None, str | None]:
    """Turn the model's argument into steps, or into a message it can act on.

    Every rejection is chapter 3's three-part shape -- what you sent, what is
    wrong, what to send instead -- because a bare `ValueError` gets retried
    verbatim (F03-07, measured).
    """
    if not isinstance(raw, list) or not raw:
        return None, tool_error(
            'update_plan needs a "plan" argument: a non-empty list of steps',
            you_sent=repr(raw)[:200],
            do_this=(
                'Example: {"plan": [{"step": "add subtract to calc.py", "status": "in_progress"}]}'
            ),
        )
    if len(raw) > MAX_STEPS:
        return None, tool_error(
            f"that is {len(raw)} steps and the limit is {MAX_STEPS}",
            do_this=(
                "Group them. A step is a piece of work you could show is done, not a keystroke."
            ),
        )

    steps: list[PlanStep] = []
    for index, item in enumerate(raw, 1):
        if not isinstance(item, dict):
            return None, tool_error(
                f"step {index} is not an object",
                you_sent=repr(item)[:120],
                do_this='Each step is {"step": "...", "status": "..."}.',
            )
        text = item.get("step")
        status = item.get("status")
        if not isinstance(text, str) or not text.strip():
            return None, tool_error(
                f'step {index} has no "step" text',
                you_sent=repr(item)[:120],
                do_this='Each step is {"step": "...", "status": "..."}.',
            )
        if status not in STATUSES:
            return None, tool_error(
                f"step {index} has status {status!r}, which is not one of the three",
                you_sent=repr(item)[:120],
                do_this=f"Use one of: {', '.join(STATUSES)}.",
            )
        steps.append(PlanStep(text.strip(), status))

    running = [s.text for s in steps if s.status == "in_progress"]
    if len(running) > 1:
        # codex states this rule in the tool description and in the system
        # prompt and does not check it. It is one line here, and a rule the
        # code enforces is a rule that is true.
        return None, tool_error(
            f"{len(running)} steps are in_progress at once: {running}",
            do_this=("Exactly one step may be in_progress. Mark the others pending or completed."),
        )
    return tuple(steps), None


def _newly_completed(old: tuple[PlanStep, ...], new: tuple[PlanStep, ...]) -> list[str]:
    """Steps that were not completed before and are now.

    Matched on the step text, because the model rewrites the list whole and
    there are no ids.  A renamed step therefore reads as a new one, which is
    the safe direction: it is treated as newly completed and has to justify
    itself like any other.
    """
    was_done = {step.text for step in old if step.status == "completed"}
    return [s.text for s in new if s.status == "completed" and s.text not in was_done]


async def update_plan(plan: TaskPlan, args: dict[str, Any]) -> str:
    """Replace the plan.  Whole list every time, never a delta.

    A delta interface (`mark step 3 done`) would need stable ids the model
    keeps track of across twenty turns; replacing the list means the argument
    is self-describing and a garbled one is rejected as a whole rather than
    corrupting what was there. codex made the same call.

    The one refusal that is about content rather than shape: a step cannot go
    to `completed` when nothing at all has happened since the last update.
    That is not a check that the step was really done -- this module has no way
    to know -- it is a check that *something* was done. It catches the plan
    written and immediately closed, and it catches the second `update_plan`
    call in a row that promotes another step. Anything subtler than that is a
    claim about the world and belongs to the human reading `describe()`.
    """
    steps, error = _parse(args.get("plan"))
    if error is not None:
        return error
    assert steps is not None

    finished = _newly_completed(plan.steps, steps)
    if finished and plan.work_since_update == 0 and plan.updates > 0:
        return tool_error(
            f"marking {finished} completed, but nothing has run since the last plan update",
            do_this=(
                "Do the work first, then mark it completed. If it was already "
                "done, say what shows it -- run the test or read the file back."
            ),
        )

    plan.steps = steps
    plan.updates += 1
    plan.work_since_update = 0
    plan.revisions.append(plan.render())

    # The rendered plan rather than codex's "Plan updated".  codex has a UI
    # panel that keeps the list on screen; this program has a terminal and a
    # history, so the only two places the plan can be seen are the two this
    # string reaches. It costs about one token per word of plan, once per
    # update, and it buys the one copy of the plan that sits in the *newest*
    # part of the history -- which is the part chapter 6's compaction keeps.
    outstanding = len(plan.outstanding())
    tail = "Nothing outstanding." if not outstanding else f"{outstanding} step(s) to go."
    return f"Plan updated.\n{plan.render()}\n{tail}"


def unfinished_note(plan: TaskPlan) -> Callable[[], str | None]:
    """The loop's second question, asked once, when the model wants to stop.

    Chapter 0 defined the end of a run as "the model asked for no tools this
    turn", and that definition has no idea what the task was. A plan is the
    first thing in this program that says what finished means *before* the
    model decides it has finished, so the loop can compare the two -- and
    comparing them is `outstanding()`, which is four words of code.

    Returns `None` when there is nothing to say, which is also what it returns
    when there is no plan at all: a run with no plan behaves exactly as it did
    in chapter 0.
    """

    def check() -> str | None:
        left = plan.outstanding()
        if not left:
            return None
        listed = "\n".join(f"{_MARK[s.status]} {s.text}" for s in left)
        return (
            f"You are about to finish, but {len(left)} step(s) of your own plan "
            f"are not marked completed:\n{listed}\n"
            "Either finish them now, or call update_plan to mark what is really "
            "done and then say plainly in your answer what is left and why."
        )

    return check


# The paragraph without which the tool is mostly not used.  Measured, three
# arms of five samples on the same task: 21/30 requirements with no plan tool,
# 25/30 with the tool and nothing said (**two of the five runs never called
# it**), 30/30 with this in the system message.  A tool being available and a
# tool being used are two changes, and only the second one showed up.
#
# It lives here rather than in `prompts/system.md` for F05-10's reason: a
# prompt that names a tool the current configuration does not have is a prompt
# that gets the model to call something that is not there, measured in chapter
# 5 at 2/3.  `__main__` appends it only when a `TaskPlan` was built.
#
# The wording is codex's, trimmed (`core/gpt_5_1_prompt.md`, which says all of
# this twice -- once in the behaviour rules and once in a per-tool section).
PLAN_INSTRUCTIONS = (
    "You have an `update_plan` tool that keeps a short checklist of the task "
    "and shows it to the user. Use it for any task with more than one part: "
    "write the plan before you start, keep exactly one step `in_progress`, and "
    "mark a step `completed` once the work behind it is actually done. Revise "
    "the list when the task turns out to be different from what you assumed. "
    "Do not use it for a one-step task, and do not let updating it stand in "
    "for doing the work."
)


PLAN_SCHEMA: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "update_plan",
        "description": (
            "Record or revise the plan for the current task, as a short "
            "checklist. Send the whole list every time, with a status for "
            "each step. Use it for a task with several parts: write the plan "
            "before starting, mark a step completed once it is actually done, "
            "and revise the list when the task turns out to be different from "
            "what you assumed. Exactly one step may be in_progress. Do not use "
            "it for a task that is one step, and do not use it instead of "
            "doing the work."
        ),
        "parameters": {
            "type": "object",
            "required": ["plan"],
            "properties": {
                "plan": {
                    "type": "array",
                    "description": (
                        f"The whole checklist, 2 to {MAX_STEPS} steps, in the "
                        "order you mean to do them."
                    ),
                    "items": {
                        "type": "object",
                        "required": ["step", "status"],
                        "properties": {
                            "step": {
                                "type": "string",
                                "description": (
                                    "One short phrase naming a piece of work "
                                    "that can be shown to be done. Example: "
                                    "add divide() with a zero check"
                                ),
                            },
                            "status": {
                                "type": "string",
                                "enum": list(STATUSES),
                                "description": "Where that step currently stands.",
                            },
                        },
                    },
                }
            },
        },
    },
}


def plan_toolset(plan: TaskPlan) -> ToolSet:
    """`update_plan`, bound to one conversation's plan.

    Its own `ToolSet` rather than an entry in `tools.tool_specs()`, for the
    same reason `spawn_toolset` is: this tool exists only when somebody hands
    over the state it edits. A sub-agent has one task and no plan, and the way
    to say that is to not build this set for it -- an absence in one function,
    rather than a flag threaded through the tool table.

    No `footprint_of`: the default is `STATEFUL`, which is correct here for
    once without an argument. The plan is mutable state shared with the loop,
    so two `update_plan` calls in one turn must not run together, and neither
    must an `update_plan` and anything else.
    """

    async def handler(args: dict[str, Any]) -> str:
        return await update_plan(plan, args)

    return ToolSet(handlers={"update_plan": handler}, schemas=[PLAN_SCHEMA])


__all__ = [
    "MAX_STEPS",
    "PLAN_INSTRUCTIONS",
    "PLAN_SCHEMA",
    "STATUSES",
    "PlanStep",
    "StepStatus",
    "TaskPlan",
    "plan_toolset",
    "unfinished_note",
    "update_plan",
]
