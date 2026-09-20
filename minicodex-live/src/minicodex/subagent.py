"""Running an agent inside a tool call.

A sub-agent is `Agent` itself -- the same class the top-level loop uses --
with a different history, a different tool table and a smaller budget,
awaited from inside a tool handler.

What this module owns is the *boundary*:

  **In: a contract, not a history.**  The child gets a task, the constraints
  that apply to it, and the shape of the answer wanted back.  Handing down
  the parent's history carries the contents of every file the parent has
  read into a conversation that never asked for them (see `docs/history.md`
  for the measurements behind the shapes here).

  **In: state the parent never wrote down.**  The child's shell is seeded
  from the parent's -- cwd, timeout, sandbox -- and its permission `Session`
  travels with the context.  A child built from scratch would start at the
  process cwd and at default permissions.

  **Out: an outcome, not a string.**  `RunResult` distinguishes
  `completed` / `turn_limit` / `interrupted`; `return result.final_text`
  throws that away, and two of those three produce the *same* empty string.

  **Around: a bound on everything.**  Depth, turns, wall clock, result size.
  Without a depth limit an agent that spawns itself does not get slow, it
  gets unstoppable.

This module imports nothing from `tools.py`: it takes `build_tools` and
`wiring` from whoever constructs its `SubAgentContext`, which is what allows
the `spawn_agent` handler to live here and the package to stay acyclic.

**Relation to upstream codex.**  This is the largest structural difference
between the two, and it is a difference in collaboration *models*, not an
implementation detail.  codex's spawn returns immediately with an agent id and
the child runs in the background; the parent polls `wait_agent` (and can
`send_input`, `resume_agent`, `close_agent`), several children run at once,
and the default depth limit of 1 says "solve the task yourself".  Here,
`spawn_agent` blocks until the child finishes and its tool output *is* the
final text.  The blocking shape is what preserves the agent loop's invariant
that every issued call is answered exactly once, in one process and one event
loop; the default depth of 2 is correspondingly deeper because a synchronous
child cannot multiply into a worker pool.
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

from minicodex.agent import Model, Wiring
from minicodex.agent_types import STATEFUL, Footprint, ToolCall, ToolSet
from minicodex.approval import Session
from minicodex.clip import clip
from minicodex.retry import ModelFailed
from minicodex.rollout import NULL_WRITER, RolloutWriter, SessionMeta, new_session_id, rollout_path
from minicodex.shell import ShellSession
from minicodex.tool_errors import tool_error

# How deep the nesting may go.  2 means: the top-level agent may spawn, and its
# children may spawn, and *their* children may not.  Every level multiplies the
# number of model calls, and the unbounded version is not "slow" but
# unstoppable (see docs/history.md).
MAX_DEPTH = 2

# One sub-task's own turn budget.  Smaller than the parent's, because a
# sub-task that needs twelve turns was not a sub-task.
DEFAULT_TASK_TURNS = 8

# How many model calls *every* sub-agent in one run may cost between them.
# A depth limit bounds the depth and not the width; this bounds the width.
# Counted from `SubAgentContext.children`, which is kept for the end-of-run
# listing anyway.  In-flight ancestors are not counted, so the bound is not
# exact -- bounded, not exact, and said so rather than implied.
DEFAULT_CHILD_TURN_BUDGET = 24

# Wall clock for one sub-task, including every tool it runs.  The parent's
# own timeouts do not imply this: a single command is bounded far tighter,
# and nothing else bounds several turns of them.
DEFAULT_TASK_TIMEOUT = 300.0

# How much of a sub-agent's answer reaches the parent's history.  The
# `expected_output` instruction below shapes length but does not bound it;
# this ceiling is where the bound actually lives.
MAX_TASK_RESULT_CHARS = 4000

Outcome = Literal[
    "ok", "empty", "turn_limit", "timeout", "interrupted", "depth_limit", "budget", "error"
]


@dataclass(frozen=True)
class TaskSpec:
    """What the parent is handing down.

    `constraints` are things that are true of the session and not of the task
    -- "the shell on this machine is broken", "do not touch the streaming
    parser".  They go into the child's *system* message rather than into its
    task text: appended to the task, they are routinely violated; as a system
    note, they hold (see docs/history.md).

    `expected_output` is what the parent wants back and how long it may be.
    Without it the child writes an essay the parent then pays for on every
    subsequent turn.
    """

    task: str
    constraints: tuple[str, ...] = ()
    expected_output: str = ""

    def instructions(self) -> str:
        """The child's system message.

        Deliberately *not* the parent's system prompt.  The child is one task
        with one set of rules; inheriting the parent's full identity and
        permission block is how the context bloat happens one message at a
        time.

        The last paragraph is the one that is easy to leave out.  A sub-agent
        has no user: "shall I go on?" written as prose reaches nobody, and
        the child then waits for an answer by spending its whole turn budget
        on politeness.
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
    # A provider failure inside the child must not reach the *parent model* as
    # a raw traceback plus a JSON blob; this outcome translates it into the
    # same shape every other outcome has.  See the `error` arm of `run_task`.
    "error": "could not run: {detail}",
}

# Every one of these says what to do next, not only what happened: a message
# naming only the failure gets retried verbatim.
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
    "error": (
        "This is a fault in the tooling, not in the task. Do not delegate it again; "
        "do the work yourself or tell the user what stopped you."
    ),
}


@dataclass(frozen=True)
class TaskResult:
    """How a sub-task ended, kept apart from what it said.

    `outcome` exists because `final_text` cannot carry it: a child that ran
    out of turns and a child that was interrupted both return `''`, and a
    child that finished returns prose that looks exactly like the prose of a
    child that gave up halfway.
    """

    outcome: Outcome
    text: str
    turns: int = 0
    seconds: float = 0.0
    session_id: str = ""
    # Only `error` fills this in: the one sentence explaining a failure that
    # was nothing to do with the task.  A separate field rather than being
    # folded into `text`, because `text` means "what the child said" and a
    # child that never got an answer out of the provider said nothing.
    detail: str = ""

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

        headline = _HEADLINE[self.outcome].format(
            turns=self.turns, seconds=self.seconds, detail=self.detail
        )
        header = f"[sub-agent: {headline}]"
        advice = _ADVICE[self.outcome]
        if self.outcome == "error":
            # No "what it had said before that point": there is no before.  The
            # request never produced a turn, so the only honest content is the
            # translated failure and what the parent should do instead.
            return f"{header}\n{advice}"
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
    appended to.  It is mutable for the same reason `Session` is -- the caller
    needs to know afterwards what happened, and threading a return value back
    out through a tool handler that must return a string is not possible.
    """

    build_model: Callable[[list[dict[str, Any]]], Model]
    root: Path
    session: Session
    parent_shell: ShellSession
    # How a child's own tools get built.  A callable supplied from above
    # rather than a call to `tools.bind_all` here: written the direct way,
    # `subagent` imports `tools`, which forbids `tools` from ever holding the
    # `spawn_agent` handler.  Given the child's shell -- seeded from the
    # parent's, so a child starts where the parent is standing.
    build_tools: Callable[[ShellSession], ToolSet] = lambda _shell: ToolSet({}, [])
    # Optional outermost wrapper applied to every shell of this run, parent
    # and children alike. The web console passes its audited shell factory so
    # a child's commands reach the audit trail; nothing else sets it.
    wrap_shell: Callable[[ShellSession], ShellSession] | None = None
    # Recorder, context window, summariser, concurrency cap: one object, so
    # "the child gets what the parent got" is the default rather than a
    # checklist.
    wiring: Wiring = field(default_factory=Wiring)
    depth: int = 0
    max_depth: int = MAX_DEPTH
    max_turns: int = DEFAULT_TASK_TURNS
    # Shared by every sub-agent in the run, because `children` is one list
    # passed down by reference: `replace(ctx, depth=...)` copies the reference,
    # not the list.
    child_turn_budget: int = DEFAULT_CHILD_TURN_BUDGET
    timeout: float = DEFAULT_TASK_TIMEOUT
    sessions_dir: Path | None = None
    parent_session_id: str = ""
    # Recorded in the child's session header.  Empty by default and filled in
    # by the CLI: a transcript that does not say which model wrote it is a
    # transcript nobody can compare with another one.
    provider: str = ""
    model: str = ""
    # Where "[sub-agent ...]" lines go.  A sub-agent that leaves no trace in
    # the terminal is a minute of silence the user cannot interpret.
    announce: Callable[[str], None] | None = None
    children: list[TaskResult] = field(default_factory=list)


def _say(ctx: SubAgentContext, message: str) -> None:
    if ctx.announce is not None:
        ctx.announce(message)


def child_tools(ctx: SubAgentContext) -> ToolSet:
    """The child's tools: handlers, schemas and footprints, built together.

    One value rather than three tables, because a handler with no schema is
    never called and a schema with no handler produces the "no tool named X"
    error written for names the model *invented*; `ToolSet` refuses to be
    built if the first two disagree, and carrying the footprint table along
    keeps the scheduler correct for every sub-agent.

    The child's shell is seeded from the parent's -- cwd, timeout *and*
    sandbox.  The sandbox part is load-bearing for the permission model:
    dropping it would give the child an unconfined shell under a parent the
    user set to `workspace-write`, silently undoing the confinement the
    kernel provides.

    At the bottom of the allowed depth the child is not given `spawn_agent`
    at all, rather than being given it and refused: a prompt that names a
    tool the policy will not allow teaches the model to call it and spend a
    turn finding out.
    """
    shell = ShellSession(timeout=ctx.parent_shell.timeout)
    shell.cwd = ctx.parent_shell.cwd
    # Without this the child runs every command on the host while the parent
    # is confined -- the sandbox setting would stop one `spawn_agent` call
    # short of doing anything.
    shell.sandbox = ctx.parent_shell.sandbox
    # A caller that wraps every shell (the web console's audited subclass)
    # hands the wrapper down here, so a child's commands ride the same audit
    # channel as its parent's. `None` means no wrapping anywhere in the run.
    if ctx.wrap_shell is not None:
        shell = ctx.wrap_shell(shell)

    tools = ctx.build_tools(shell)
    if ctx.depth + 1 < ctx.max_depth:
        tools = tools.plus(spawn_toolset(replace(ctx, depth=ctx.depth + 1)))
    return tools


async def run_task(spec: TaskSpec, ctx: SubAgentContext) -> TaskResult:
    """Run one sub-task to completion, or to one of the ways it can fail.

    Never raises for anything the sub-agent did.  It *does* re-raise
    `CancelledError`, because a Ctrl-C belongs to the parent's loop.
    """
    if ctx.depth >= ctx.max_depth:
        return TaskResult("depth_limit", "")

    spent = sum(child.turns for child in ctx.children)
    if spent >= ctx.child_turn_budget:
        return TaskResult("budget", "")

    tools = child_tools(ctx)
    writer = _writer(ctx)
    # The parent's `Wiring`, not a fresh one: recorder, context window,
    # summariser and concurrency cap all cross the boundary as one object.
    child = ctx.wiring.agent(
        ctx.build_model(tools.schemas),
        tools,
        max_turns=ctx.max_turns,
        instructions=spec.instructions(),
        rollout=writer,
    )

    _say(ctx, f"[sub-agent depth {ctx.depth + 1}: {spec.task.splitlines()[0][:70]}]")
    began = time.monotonic()
    # `ensure_future` + `shield` rather than a bare `wait_for`: `Agent.run`
    # catches `CancelledError` and returns a normal `RunResult`, so a bare
    # `wait_for` cancels the child and *returns its value*, raising no
    # `TimeoutError` at all -- the timeout silently becomes an empty answer.
    # The shield makes the cancellation land on this function's own await.
    task = asyncio.ensure_future(child.run(spec.task))
    try:
        result = await asyncio.wait_for(asyncio.shield(task), ctx.timeout)
    # `asyncio.TimeoutError`, not the builtin: on 3.10 -- which
    # `requires-python` promises -- they are unrelated classes, so
    # `except TimeoutError` never fires there and a hanging child is not
    # stopped at all.  Keep the portable spelling.
    except asyncio.TimeoutError:
        partial = await _stop(task)
        elapsed = time.monotonic() - began
        _say(ctx, f"[sub-agent depth {ctx.depth + 1}: stopped after {elapsed:.0f}s]")
        return _record(ctx, TaskResult("timeout", partial, seconds=elapsed, session_id=_id(writer)))
    except asyncio.CancelledError:
        # The shield protected the child from the parent's cancellation, so
        # the child is still running.  Stop it here: work nobody is waiting
        # for is not allowed to keep going.
        await _stop(task)
        raise
    except ModelFailed as exc:
        # Translated rather than propagated: a provider failure inside the
        # child would otherwise reach the *parent model* as a raw traceback
        # plus a JSON blob.  No `_stop(task)` here -- the task has already
        # finished with this exception, and `_stop`'s await would re-raise it
        # out of the handler that exists to catch it.
        elapsed = time.monotonic() - began
        _say(ctx, f"[sub-agent depth {ctx.depth + 1}: {exc.failure.kind}]")
        return _record(
            ctx,
            TaskResult(
                "error",
                "",
                seconds=elapsed,
                session_id=_id(writer),
                detail=exc.failure.detail,
            ),
        )
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
        # said nothing produced no result, and rendering that as `""` tells
        # the parent it succeeded.
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
    paths are correct; the current one is pinned by a test.
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

    Not the parent's file: two writers on one rollout are refused with an
    `O_EXCL` lock, so sharing a file would raise before the child's first
    turn.  A separate file, plus a `parent` field, is what makes the pair
    findable and joinable by a human afterwards.
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

# Constants rather than literals inside `spawn_toolset`, so a snapshot test
# can pin this wording without building a whole `SubAgentContext` first.  A
# description is data; only the handler needs a context.
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


def spawn_footprint(_call: ToolCall) -> Footprint:
    """A spawn touches whatever its child touches, which is unknowable here.

    STATEFUL is the honest answer: treating a spawn as read-only loses one of
    two concurrent edits almost every time it happens.
    """
    return STATEFUL


def spawn_toolset(ctx: SubAgentContext) -> ToolSet:
    """`spawn_agent` as a one-tool `ToolSet`: handler, schema and footprint.

    A `ToolSet` rather than a `ToolSpec`: `ToolSpec` lives in `tools.py` and
    this module must not import `tools.py` -- that arrow is what would keep
    this handler from living beside the others.  The three tables exchanged
    between the sites are a value (`agent_types.ToolSet`), not an interface.

    The same function serves the top-level agent and every level below it: the
    only difference is which `ctx` is closed over, and `child_tools` passes one
    with `depth + 1`.
    """
    return ToolSet(
        handlers={SPAWN_NAME: functools.partial(spawn_agent, ctx)},
        schemas=[
            {
                "type": "function",
                "function": {
                    "name": SPAWN_NAME,
                    "description": SPAWN_DESCRIPTION,
                    "parameters": SPAWN_PARAMETERS,
                },
            }
        ],
        footprint_of=spawn_footprint,
    )


def describe_children(results: Sequence[TaskResult]) -> list[str]:
    """One line per sub-agent, for the end of a run.

    Separate session files only pay off if the run says where they are: a
    session id printed nowhere is a link nobody follows.
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
    "spawn_footprint",
    "spawn_toolset",
]
