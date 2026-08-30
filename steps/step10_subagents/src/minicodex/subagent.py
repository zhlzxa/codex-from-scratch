"""Running an agent inside a tool call.

A sub-agent is not a new kind of object.  It is `Agent` -- the same class the
loop has used since chapter 0 -- with a different history, a different tool
table and a smaller budget, awaited from inside a tool handler.  Nothing here
subclasses anything.

What this module owns is the *boundary*: four things cross it, and each one is
here because something measurable went wrong when it did not.

  **In: a contract, not a history.**  Handing the child the parent's history
  costs 7x the tokens (measured on an eight-item parent: 4476 against 638) and
  carries the contents of every file the parent has read into a conversation
  that never asked for them.  The child gets a task, the constraints that
  apply to it, and the shape of the answer wanted back.

  **In: state the parent never wrote down.**  The parent's working directory
  lives in a `ShellSession` attribute and its permissions live in a `Session`
  object.  A child built from scratch starts at the process cwd and at
  `read-only`, so `cd src` in the parent is invisible to the child and a
  permission the user granted on turn 3 is not inherited.  Both measured;
  neither raises.

  **Out: an outcome, not a string.**  `RunResult` already distinguishes
  `completed` / `turn_limit` / `interrupted`; `return result.final_text`
  throws that away, and two of those three produce the *same* empty string.

  **Around: a bound on everything.**  Depth, turns, wall clock, result size.
  Without a depth limit an agent that spawns itself reached **830,400 levels**
  in 59 seconds and had to be killed from outside -- and the `asyncio.wait_for`
  that should have stopped it died of the nesting first, with a `RecursionError`
  raised inside the event loop's own timer callback.

This module imports `agent.py` and `tools.py`; neither imports it.  That
direction is not an accident -- putting the `spawn_agent` handler next to the
other handlers in `tools.py`, which is where a handler obviously belongs, makes
`tools` import `subagent` import `tools`, and the package stops importing at
all.  Chapter 10 solves it by moving the tool out; interlude B is about the
case where moving it is not enough.

Deliberately not here: a sub-agent gets this project's own tools and no MCP
tools.  Chapter 9's registry belongs to one session, and a remote tool's
`Footprint` is a promise made by somebody else's code; giving a child a
resource it can touch without the parent's scheduler knowing is F08-06 with a
second process in the way.  Said out loud rather than left to be discovered.
"""

from __future__ import annotations

import asyncio
import contextlib
import functools
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Literal

from minicodex.agent import Agent, Model
from minicodex.agent_types import ToolFn
from minicodex.approval import Session
from minicodex.clip import clip
from minicodex.rollout import NULL_WRITER, RolloutWriter, SessionMeta, new_session_id, rollout_path
from minicodex.shell import ShellSession
from minicodex.tool_errors import tool_error
from minicodex.tools import ToolContext, ToolSpec, bind_all, tool_context, tool_schemas

# How deep the nesting may go.  2 means: the top-level agent may spawn, and its
# children may spawn, and *their* children may not.  Small on purpose -- every
# level multiplies the number of model calls, and the measured behaviour of the
# unbounded version is not "slow", it is "unstoppable".
MAX_DEPTH = 2

# One sub-task's own turn budget.  Smaller than the parent's, because a
# sub-task that needs twelve turns was not a sub-task.
DEFAULT_TASK_TURNS = 8

# How many model calls *every* sub-agent in one run may cost between them.
# This one was not designed; a test produced it.  With only a depth limit in
# place, a model that answers every turn with another spawn cost **72 model
# calls**: eight turns at depth 1, each spawning a depth-2 child that spends
# its own eight turns.  A depth limit bounds the depth and not the width, and
# 2 x 8 x 8 is what the width costs.  Counted from `SubAgentContext.children`,
# which was already being kept for the end-of-run listing.
DEFAULT_CHILD_TURN_BUDGET = 24

# Wall clock for one sub-task, including every tool it runs.  A number the
# parent's own timeouts do not imply: chapter 2 bounds a single command at 30s
# and nothing bounds eight turns of them.
DEFAULT_TASK_TIMEOUT = 300.0

# How much of a sub-agent's answer reaches the parent's history.  Measured
# against gpt-4o-mini: asked to explain one module with no shape requested it
# returned a median of 2331 characters; asked for three sentences, 503.  The
# instruction is worth having -- and it is not a bound.  The same instruction on
# a slightly larger task produced 603, 615, 661, 1468 and 2711 characters, so
# the ceiling is here, in code, where a number means what it says.
MAX_TASK_RESULT_CHARS = 4000

Outcome = Literal["ok", "empty", "turn_limit", "timeout", "interrupted", "depth_limit", "budget"]


@dataclass(frozen=True)
class TaskSpec:
    """What the parent is handing down.

    Three fields, and the second and third are the ones that were measured.

    `constraints` are things that are true of the session and not of the task
    -- "the shell on this machine is broken", "do not touch the streaming
    parser".  They go into the child's *system* message rather than into its
    task text, which is not a style preference: with the constraint appended to
    the task, 3 and 4 of 5 runs violated it anyway; as a system note, 0 of 5.

    `expected_output` is what the parent wants back and how long it may be.
    Without it the child writes an essay the parent then pays for on every
    subsequent turn.
    """

    task: str
    constraints: tuple[str, ...] = ()
    expected_output: str = ""

    def instructions(self) -> str:
        """The child's system message.

        Deliberately *not* the parent's system prompt.  The child is not a
        smaller copy of the parent; it is one task with one set of rules, and
        inheriting "you are a coding agent working in a user's repository"
        plus a permissions block is how the 7x token measurement happens one
        message at a time.

        The last paragraph is the one that is easy to leave out.  A sub-agent
        has no user: chapter 5's approval prompts reach the human through the
        parent's terminal, but "shall I go on?" written as prose reaches
        nobody, and the child then waits for an answer by spending its whole
        turn budget on politeness.
        """
        parts = ["You have been given one self-contained task by another agent."]
        if self.constraints:
            parts.append(
                "These rules come from the user and apply to everything you do:\n"
                + "\n".join(f"- {c}" for c in self.constraints)
            )
        if self.expected_output:
            parts.append(f"Return exactly this and nothing else: {self.expected_output}")
        parts.append(
            "Nobody will read anything you say except the agent that sent you, and it "
            "cannot answer questions. If the task cannot be done, say so in one "
            "sentence and say what stopped you."
        )
        return "\n\n".join(parts)


_HEADLINE: dict[str, str] = {
    "empty": "finished without answering",
    "turn_limit": "ran out of turns after {turns}",
    "timeout": "still running after {seconds:.0f}s and was stopped",
    "interrupted": "interrupted",
    "depth_limit": "refused: sub-agents may not spawn sub-agents this deep",
    "budget": "refused: this run has spent its whole sub-agent budget",
}

# Every one of these says what to do next, not only what happened.  That is
# chapter 3's finding (F03-07) one process-shaped layer out: a message naming
# only the failure gets retried verbatim.
_ADVICE: dict[str, str] = {
    "empty": "Do not report this as a finding. Split the task, or do it yourself.",
    "turn_limit": (
        "Do not report this as a finding. Give it a smaller task, or do the "
        "remaining part yourself."
    ),
    "timeout": (
        "Do not report this as a finding. Anything it had already changed on "
        "disk is still changed; check before repeating it."
    ),
    "interrupted": "The user stopped it. Do not start it again unless asked.",
    "depth_limit": "Do this part yourself instead of delegating it further.",
    "budget": "Do the rest yourself. Delegating again will get the same answer.",
}


@dataclass(frozen=True)
class TaskResult:
    """How a sub-task ended, kept apart from what it said.

    `outcome` exists because `final_text` cannot carry it.  A child that ran
    out of turns and a child that was interrupted both return `''` -- measured,
    not reasoned about -- and a child that finished returns prose that looks
    exactly like the prose of a child that gave up halfway.
    """

    outcome: Outcome
    text: str
    turns: int = 0
    seconds: float = 0.0
    session_id: str = ""

    @property
    def ok(self) -> bool:
        return self.outcome == "ok"

    def render(self) -> str:
        """What the parent's history actually receives.

        A successful task returns its answer and nothing else: a header on
        every single result is a header the model learns to skip.  Everything
        else returns a labelled block saying what happened and what to do
        about it.
        """
        body = clip(self.text.strip(), MAX_TASK_RESULT_CHARS)
        if self.outcome == "ok":
            return body

        headline = _HEADLINE[self.outcome].format(turns=self.turns, seconds=self.seconds)
        header = f"[sub-agent: {headline}]"
        advice = _ADVICE[self.outcome]
        if not body:
            return f"{header}\nIt produced no answer at all. {advice}"
        return (
            f"{header}\nWhat it had said before that point, which is not a "
            f"conclusion:\n\n{body}\n\n{advice}"
        )


@dataclass(frozen=True)
class SubAgentContext:
    """Everything a sub-agent needs that belongs to the run it is part of.

    The same shape as `ToolContext` and for the same reason: these travel
    together, and a call site that passes six of seven is a call site that
    compiles.

    `build_model` is a factory rather than a model because the child is shown a
    different tool list from the parent -- no `spawn_agent` at the bottom level
    -- and a chat-completions client carries its tool list.

    `children` is a list and this class is frozen, which is not a contradiction
    but is worth saying: the field cannot be reassigned, and the list is
    appended to.  It is here for the same reason `Session` is mutable -- the
    caller needs to know afterwards what happened, and threading a return value
    back out through a tool handler that must return a string is not possible.
    """

    build_model: Callable[[list[dict[str, Any]]], Model]
    root: Path
    session: Session
    parent_shell: ShellSession
    depth: int = 0
    max_depth: int = MAX_DEPTH
    max_turns: int = DEFAULT_TASK_TURNS
    # Shared by every sub-agent in the run, because `children` is one list
    # passed down by reference: `replace(ctx, depth=...)` copies the reference,
    # not the list.  In-flight ancestors are not counted -- a child is recorded
    # when it finishes -- so this undercounts by the turns of whichever
    # sub-agents are still running above.  Bounded, not exact, and said so
    # rather than implied.
    child_turn_budget: int = DEFAULT_CHILD_TURN_BUDGET
    timeout: float = DEFAULT_TASK_TIMEOUT
    sessions_dir: Path | None = None
    parent_session_id: str = ""
    # Recorded in the child's session header.  Empty by default and filled in
    # by the CLI: a transcript that does not say which model wrote it is a
    # transcript nobody can compare with another one, and the first version of
    # this printed `? | read-only` for every sub-agent in `minicodex sessions`.
    provider: str = ""
    model: str = ""
    # Where "[sub-agent ...]" lines go.  A sub-agent that leaves no trace in the
    # terminal is a minute of silence the user cannot interpret.
    announce: Callable[[str], None] | None = None
    children: list[TaskResult] = field(default_factory=list)


def _say(ctx: SubAgentContext, message: str) -> None:
    if ctx.announce is not None:
        ctx.announce(message)


def child_tools(ctx: SubAgentContext) -> tuple[dict[str, ToolFn], list[dict[str, Any]]]:
    """The child's handler table and the schemas that match it, built together.

    Together because chapter 4 paid for the version where they were two lists:
    a handler with no schema is never called, and a schema with no handler
    produces chapter 0's "no tool named X", which was written for names the
    model *invented*.

    The child's shell starts where the parent's shell is now, not where the
    process started.  `cd src` in the parent and then `pwd` in a freshly built
    child returns the repository root -- measured, silent, and wrong.

    At the bottom of the allowed depth the child is not given `spawn_agent` at
    all, rather than being given it and refused.  Chapter 5 measured what
    happens when a prompt names a tool the policy will not allow: the model
    calls it (2/3) and spends a turn finding out.
    """
    shell = ShellSession(timeout=ctx.parent_shell.timeout)
    shell.cwd = ctx.parent_shell.cwd
    context = tool_context(root=ctx.root, session=ctx.session, shell=shell)

    handlers = bind_all(context)
    schemas = list(tool_schemas())

    if ctx.depth + 1 < ctx.max_depth:
        spec = spawn_spec(replace(ctx, depth=ctx.depth + 1))
        handlers[spec.name] = spec.bind(context)
        schemas.append(spec.schema())
    return handlers, schemas


async def run_task(spec: TaskSpec, ctx: SubAgentContext) -> TaskResult:
    """Run one sub-task to completion, or to one of the ways it can fail.

    Never raises for anything the sub-agent did -- chapter 0's rule, one level
    down.  It *does* re-raise `CancelledError`, because a Ctrl-C belongs to the
    parent's loop and chapter 7 already knows what to do with one.
    """
    if ctx.depth >= ctx.max_depth:
        return TaskResult("depth_limit", "")

    spent = sum(child.turns for child in ctx.children)
    if spent >= ctx.child_turn_budget:
        return TaskResult("budget", "")

    handlers, schemas = child_tools(ctx)
    writer = _writer(ctx)
    child = Agent(
        ctx.build_model(schemas),
        handlers,
        max_turns=ctx.max_turns,
        instructions=spec.instructions(),
        rollout=writer,
    )

    _say(ctx, f"[sub-agent depth {ctx.depth + 1}: {spec.task.splitlines()[0][:70]}]")
    began = time.monotonic()
    # `ensure_future` + `shield` rather than a bare `wait_for`, and the reason
    # is measured rather than stylistic: `Agent.run` catches `CancelledError`
    # and returns a normal `RunResult` (chapter 7 -- every issued call must be
    # answered before unwinding).  A bare `wait_for` therefore *cancels the
    # child and returns its value*, raising no `TimeoutError` at all: the
    # timeout silently becomes an empty answer.  The shield makes the
    # cancellation land on this function's own await instead.
    task = asyncio.ensure_future(child.run(spec.task))
    try:
        result = await asyncio.wait_for(asyncio.shield(task), ctx.timeout)
    except TimeoutError:
        partial = await _stop(task)
        elapsed = time.monotonic() - began
        _say(ctx, f"[sub-agent depth {ctx.depth + 1}: stopped after {elapsed:.0f}s]")
        return _record(ctx, TaskResult("timeout", partial, seconds=elapsed, session_id=_id(writer)))
    except asyncio.CancelledError:
        # The shield protected the child from the parent's cancellation, so the
        # child is still running.  Stopping it here is chapter 7's rule about
        # subprocesses in a different costume: work nobody is waiting for is
        # not allowed to keep going.
        await _stop(task)
        raise
    finally:
        writer.release()

    elapsed = time.monotonic() - began
    text = result.final_text.strip()
    outcome: Outcome
    if result.stop_reason == "turn_limit":
        outcome = "turn_limit"
    elif result.stop_reason == "interrupted":
        outcome = "interrupted"
    elif not text:
        # Not "a very short answer".  An agent that stopped calling tools and
        # said nothing produced no result, and rendering that as `""` tells the
        # parent it succeeded -- the argument F06-08 makes about an empty
        # summary and F09-07 about an empty MCP result.
        outcome = "empty"
    else:
        outcome = "ok"
    _say(ctx, f"[sub-agent depth {ctx.depth + 1}: {outcome} in {result.turns_used} turn(s)]")
    return _record(ctx, TaskResult(outcome, text, result.turns_used, elapsed, _id(writer)))


def _record(ctx: SubAgentContext, result: TaskResult) -> TaskResult:
    ctx.children.append(result)
    return result


async def _stop(task: asyncio.Task[Any]) -> str:
    """Cancel a child and collect whatever it had already said.

    Awaiting the cancelled task rather than dropping it: the child answers its
    own outstanding tool calls on the way out, and abandoning it here would
    leave that unwinding to run alongside the parent's next turn.

    `return ""` at the end is not dead code, though it looks it.  `Agent.run`
    *currently* swallows `CancelledError` and returns a `RunResult`, so the
    `return` inside the `with` is the path taken today; the day that changes,
    the await raises, `suppress` catches it, and the last line runs.  Both
    paths are correct.  The test that pins the current behaviour is
    `test_F10_07_a_bare_wait_for_would_have_returned_an_empty_answer`.
    """
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        result = await task
        return str(result.final_text).strip()
    return ""


def _id(writer: RolloutWriter) -> str:
    return writer.meta.session_id if writer is not NULL_WRITER else ""


def _writer(ctx: SubAgentContext) -> RolloutWriter:
    """The child's own session file, linked to its parent.

    Not the parent's file.  Chapter 7 refuses two writers on one rollout with
    an `O_EXCL` lock, so the naive version does not merely interleave -- it
    raises `RolloutError` before the child's first turn.  That refusal is the
    right answer, and this is its other half: a separate file, and a `parent`
    field so a human can put the two back together.
    """
    if ctx.sessions_dir is None:
        return NULL_WRITER
    meta = SessionMeta(
        session_id=new_session_id(),
        created=time.time(),
        cwd=str(ctx.root),
        provider=ctx.provider,
        model=ctx.model,
        sandbox_mode=ctx.session.mode,
        approval_policy=ctx.session.policy,
        parent=ctx.parent_session_id or None,
    )
    return RolloutWriter(rollout_path(meta.session_id, ctx.sessions_dir), meta)


# -- the tool ---------------------------------------------------------------


async def spawn_agent(ctx: SubAgentContext, args: dict[str, Any]) -> str:
    task = args.get("task")
    if not isinstance(task, str) or not task.strip():
        return tool_error(
            'spawn_agent needs a "task" argument, a non-empty string',
            you_sent=repr(args.get("task")),
            do_this=(
                'Example: {"task": "Find every call to os.killpg under src/", '
                '"expected_output": "one line per match, path:line"}'
            ),
        )
    constraints = args.get("constraints")
    if constraints is not None and (
        not isinstance(constraints, list) or not all(isinstance(c, str) for c in constraints)
    ):
        return tool_error(
            '"constraints" must be a list of strings',
            you_sent=repr(constraints)[:200],
            do_this='Example: {"constraints": ["do not run the test suite"]}',
        )
    expected = args.get("expected_output")
    spec = TaskSpec(
        task=task,
        constraints=tuple(constraints or ()),
        expected_output=expected if isinstance(expected, str) else "",
    )
    return (await run_task(spec, ctx)).render()


SPAWN_NAME = "spawn_agent"

# Constants rather than literals inside `spawn_spec`, so that the chapter 3
# snapshot test (F03-10) can pin this wording without building a whole
# `SubAgentContext` first.  A description is data; only the handler needs a
# context.
SPAWN_DESCRIPTION = (
    "Hand one self-contained piece of work to a second agent, which does it in "
    "its own conversation and returns a short answer. Use this when a step needs "
    "several tool calls of its own and the details do not need to be in this "
    "conversation -- searching a large tree, or reading several files to answer "
    "one question. Do not use it for a single tool call you could make yourself, "
    "and do not use it for work that depends on what you are doing right now: it "
    "starts with nothing but what you write here."
)

SPAWN_PARAMETERS: dict[str, Any] = {
    "type": "object",
    "required": ["task", "expected_output"],
    "properties": {
        "task": {
            "type": "string",
            "description": (
                "The whole task, written for someone who has not read this "
                "conversation. Name the files and name the goal."
            ),
        },
        "expected_output": {
            "type": "string",
            "description": (
                "What you want back and how long it may be. Example: one line per "
                "match, path:line, at most 20 lines."
            ),
        },
        "constraints": {
            "type": "array",
            "items": {
                "type": "string",
                "description": "One rule, in the user's own words where possible.",
            },
            "description": (
                "Rules from the user that apply to this task too. The sub-agent "
                "cannot see this conversation, so anything you were told and do "
                "not repeat here does not exist for it."
            ),
        },
    },
}


def spawn_spec(ctx: SubAgentContext) -> ToolSpec:
    """`spawn_agent`, described once, in the same shape as every other tool.

    A `ToolSpec` even though it is built here rather than in `tool_specs()`:
    the point of that class is that a description and a handler cannot drift
    apart, and that is worth as much for a tool defined in another module.
    `bind` takes the `ToolContext` every spec takes and ignores it, because a
    sub-agent's context is a different object -- the one seam left over from
    moving this handler out of `tools.py`.
    """
    return ToolSpec(
        name=SPAWN_NAME,
        description=SPAWN_DESCRIPTION,
        parameters=SPAWN_PARAMETERS,
        bind=lambda _context: functools.partial(spawn_agent, ctx),
    )


def spawn_tools(
    ctx: SubAgentContext, context: ToolContext
) -> tuple[dict[str, ToolFn], list[dict[str, Any]]]:
    """The top-level agent's spawn handler and its schema, as one pair."""
    spec = spawn_spec(ctx)
    return {spec.name: spec.bind(context)}, [spec.schema()]


def describe_children(results: Sequence[TaskResult]) -> list[str]:
    """One line per sub-agent, for the end of a run.

    F10-12 is not solved by writing separate files; it is solved by being able
    to find them.  A session id printed nowhere is a link nobody follows.
    """
    return [
        f"[sub-agent {r.session_id or '(not recorded)'}: {r.outcome}, "
        f"{r.turns} turn(s), {r.seconds:.1f}s]"
        for r in results
    ]


__all__ = [
    "DEFAULT_CHILD_TURN_BUDGET",
    "DEFAULT_TASK_TIMEOUT",
    "DEFAULT_TASK_TURNS",
    "MAX_DEPTH",
    "MAX_TASK_RESULT_CHARS",
    "SPAWN_DESCRIPTION",
    "SPAWN_NAME",
    "SPAWN_PARAMETERS",
    "SubAgentContext",
    "TaskResult",
    "TaskSpec",
    "child_tools",
    "describe_children",
    "run_task",
    "spawn_agent",
    "spawn_spec",
    "spawn_tools",
]
