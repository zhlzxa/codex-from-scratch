"""The loop: ask, run what it asks for, feed the results back, ask again.

Everything lives in this one module on purpose.  The boundaries between stream
assembly, tool dispatch and the loop are guesses right now, and a boundary in
the wrong place is harder to remove than no boundary.  Interlude A pays this
off, once there are enough call sites for the boundaries to be observed rather
than invented.

Async is decided here rather than later: an agent spends nearly all its wall
clock waiting on IO, and converting a synchronous call chain to async in Python
means touching every caller on the path.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncIterator, Callable, Sequence
from dataclasses import dataclass, field, replace
from typing import Any, Protocol

from minicodex.agent_types import IncompleteStreamError, ToolCall, ToolFn, ToolSet
from minicodex.compaction import CompactionResult, Sizer, Summariser, compact
from minicodex.history import Dialect, History
from minicodex.model import Completed, StreamEvent, TextDelta, ToolCallDelta, Usage
from minicodex.recorder import NULL_RECORDER, Recorder
from minicodex.replay import call_record, failure_record
from minicodex.retry import (
    DEFAULT_POLICY,
    Failure,
    ModelFailed,
    RetryPolicy,
    classify,
    wait_for,
)
from minicodex.rollout import NULL_WRITER, RolloutWriter, interrupted_note, replay
from minicodex.scheduler import STATEFUL, FootprintFn, batches
from minicodex.tokens import Calibration

# Re-exported, not defined here: `retry.py` has to classify it and `agent.py`
# imports `retry.py`, so the type lives below both (`agent_types.py`).  Eleven
# chapters of tests import the name from this module and keep working.
__all__ = ["Agent", "IncompleteStreamError", "ModelTurn", "RunResult", "Wiring"]


class TurnInterrupted(RuntimeError):
    """A turn was cancelled from outside and has been closed off cleanly.

    Raised nowhere and caught nowhere: it exists as the name of the state the
    loop unwinds into, so the two interruption levels have different words.
    Cancelling a *tool* is recoverable -- the call gets an output saying it was
    interrupted, and the model can decide what to do.  Cancelling a *turn* ends
    the run.  Chapter 7 measures what happens when the two are the same thing.
    """


@dataclass(frozen=True)
class ModelTurn:
    text: str
    tool_calls: tuple[ToolCall, ...]
    finish_reason: str | None = None
    usage: Usage | None = None


class Model(Protocol):
    """Anything that can stream a response given a history.

    A Protocol rather than a base class: structural typing, no inheritance, so
    the cost of the abstraction is close to zero.  It earns its place because
    swapping the model is a requirement today -- the tests need a deterministic
    one -- not a guess about tomorrow.
    """

    def stream(self, messages: Sequence[dict[str, Any]]) -> AsyncIterator[StreamEvent]: ...


@dataclass
class RunResult:
    final_text: str
    stop_reason: str  # "completed" | "turn_limit"
    turns_used: int
    history: History = field(default_factory=History)
    compactions: tuple[CompactionResult, ...] = ()


DEFAULT_MAX_TURNS = 12
BUDGET_WARNING_AT = 2

# What the model is told as the budget runs out, and what happens on the last
# turn.  Chapter 0 wrote one sentence here ("Wrap up and give your best answer
# now") and chapter 11 measured what it produces: on a task that does not fit,
# **four runs out of five ended with a final answer of zero characters.**  Not
# an abrupt summary -- nothing at all.  The mechanism is not the wording: the
# model spends its last turn on a tool call, the loop runs it, appends the
# result nobody will read, and falls out of the `for`.  A budget warning cannot
# fix that, because by the time the warning is true the model still has a tool
# it can call.
#
# So the last turn is reserved by the loop instead of requested in prose: the
# model is told that a tool call now will not run, and it does not run.  The
# wording of the wind-down itself is codex's (`ext/goal/templates/goals/
# budget_limit.md`): do not start new work, summarise progress, name the
# blockers, leave one clear next step.
BUDGET_WARNING = (
    "You have {remaining} tool-calling turn(s) left. Do not start new work. "
    "Finish or abandon what is in progress, then answer: what you did, what is "
    "left, and the one next step."
)
FINAL_TURN_WARNING = (
    "This is your last turn, and any tool call you make now will not be run. "
    "Answer in text. Say what you did, what is still undone, and the one next "
    "step. If the task is unfinished, say so plainly instead of implying it is not."
)
# What an unexecuted last-turn call is told.  Every issued call still gets an
# output -- chapter 7's rule (F07-04), which chapter 8 generalised to whole
# batches and which has no exception for "we decided not to run it".
BUDGET_DENIAL = "Error: the turn budget ended before this could run. It did not run."

# Compact when the estimate crosses this fraction of the window, down to that
# one.  Two numbers rather than one, because compacting *to* the trigger point
# means compacting again next turn: the gap is what buys the turns in between.
COMPACT_AT = 0.75
COMPACT_TO = 0.45

# The largest correction one provider refusal may make to chapter 6's token
# ruler.  See `_shrink`: without it, a single stated token count moved the
# ratio to x250 and the run compacted on 11 of its 12 turns.
MAX_REFUSAL_CORRECTION = 4.0

# How many tool calls may actually be running at once, across every batch this
# process ever schedules. A model that reads fifty files in one turn produces
# fifty non-conflicting calls -- one legal batch -- and fifty threads/sockets
# in flight at once is how F08-07 happens without ever mis-scheduling anything.
DEFAULT_MAX_CONCURRENT_TOOLS = 8


@dataclass(frozen=True)
class Wiring:
    """Everything an `Agent` is given that is neither its model nor its tools.

    Interlude B's whole point in one class.  Before it, an `Agent` was built in
    two places -- once in `__main__` for the top-level run, once in
    `subagent.run_task` for a child -- and the two calls passed *different
    subsets of the same ten keyword arguments*.  Nothing failed.  What happened
    instead, measured on the same scripted model and the same three files:

        child   final request 181,040 chars   compactions 0
        parent  final request  76,896 chars   compactions 1

    plus no transcript on disk for the child at all (F-1-04, off for exactly
    the conversation a human never sees), and every child tool call run
    serially because `footprint_of` was never passed (chapter 8, off -- safe
    only by luck, since the default for an unclassified call is `STATEFUL`).

    A frozen value rather than a builder object: it is created once per run and
    handed down.  `agent()` is the only place in `src/` that calls `Agent(...)`,
    which is checked rather than promised (`scripts/check_layers.py`).  A
    forgettable argument stops being forgettable when there is one call site
    left to forget it at.
    """

    recorder: Recorder = NULL_RECORDER
    context_window: int | None = None
    summariser: Summariser | None = None
    max_concurrent_tools: int = DEFAULT_MAX_CONCURRENT_TOOLS
    dialect: Dialect = "chat_completions"
    # Chapter 12 added the sixth and seventh, and the test pinning this class
    # at five went red, which is that test working rather than being in the
    # way.  Both qualify under the rule above -- something a child must not
    # differ from its parent in.  A child with its own retry policy is
    # interlude B's measured drift arranged deliberately; and a wait nobody is
    # told about is indistinguishable from a hang, whether it happens in the
    # parent's loop or three levels down inside a sub-agent.
    retry_policy: RetryPolicy = DEFAULT_POLICY
    announce: Callable[[str], None] | None = None

    def agent(
        self,
        model: Model,
        tools: ToolSet,
        *,
        max_turns: int = DEFAULT_MAX_TURNS,
        instructions: str | None = None,
        rollout: RolloutWriter = NULL_WRITER,
        resume_from: History | None = None,
        on_stop: Callable[[], str | None] | None = None,
        on_turn_start: Callable[[], str | None] | None = None,
    ) -> Agent:
        """Build the one `Agent` this run gets, from the one `ToolSet` it gets.

        The keyword arguments left here are the ones that genuinely differ
        between a parent and a child: a child has a smaller turn budget, its own
        task-shaped instructions, its own rollout file, never resumes, and (from
        chapter 11) has no plan of its own to be measured against.  Everything a
        child was previously missing is in `self`, so the way to forget it now
        is to build a second `Wiring`, which is a visible act.

        There used to be a `preamble` parameter here, for material delivered
        once before the first request without being folded into `instructions`
        -- chapter 16's memory block, at the time. It is gone (F16-11): it was
        built to avoid `on_turn_start`, on the theory that `on_turn_start`
        means "re-ask every turn" -- and that theory was wrong. Chapter 13's
        `AgentsMdWatcher.refresh()` already proved a per-turn hook can say
        nothing on a turn where nothing changed, and `memory.MemoryWatcher`
        below reuses exactly that contract: say the block once, then `None`
        forever. A second delivery mechanism for "developer-role content,
        established once" was never a requirement; it was chapter 16 not
        checking whether `on_turn_start` already did the job.

        `on_turn_start` joins `on_stop` here rather than in `Wiring` itself for
        the same reason: chapter 13's AGENTS.md watcher is bound to *one run's*
        shell, and a `Wiring` value is the object shared between a parent and
        every child it spawns.  A child gets no watcher at all -- the same
        absence chapter 10 gave it for MCP tools -- rather than one silently
        bound to its parent's working directory.
        """
        return Agent(
            model,
            tools.handlers,
            footprint_of=tools.footprint_of,
            max_turns=max_turns,
            recorder=self.recorder,
            dialect=self.dialect,
            instructions=instructions,
            context_window=self.context_window,
            summariser=self.summariser,
            rollout=rollout,
            resume_from=resume_from,
            max_concurrent_tools=self.max_concurrent_tools,
            on_stop=on_stop,
            on_turn_start=on_turn_start,
            retry_policy=self.retry_policy,
            announce=self.announce,
        )


class Agent:
    def __init__(
        self,
        model: Model,
        tools: dict[str, ToolFn] | None = None,
        *,
        max_turns: int = DEFAULT_MAX_TURNS,
        recorder: Recorder = NULL_RECORDER,
        dialect: Dialect = "chat_completions",
        instructions: str | None = None,
        context_window: int | None = None,
        summariser: Summariser | None = None,
        rollout: RolloutWriter = NULL_WRITER,
        resume_from: History | None = None,
        footprint_of: FootprintFn | None = None,
        max_concurrent_tools: int = DEFAULT_MAX_CONCURRENT_TOOLS,
        on_stop: Callable[[], str | None] | None = None,
        on_turn_start: Callable[[], str | None] | None = None,
        retry_policy: RetryPolicy = DEFAULT_POLICY,
        announce: Callable[[str], None] | None = None,
    ) -> None:
        self.model = model
        self.tools = tools or {}
        self.max_turns = max_turns
        self.recorder = recorder
        self.dialect = dialect
        # What one call touches, for deciding which calls may run together.
        # Defaulting to "everything is STATEFUL" -- never batch anything with
        # anything else -- rather than to some real classifier: every test
        # written before this chapter builds an `Agent` without this argument,
        # and "unknown tool means run it alone" is precisely the conservative
        # rule chapter 3 and chapter 5 already use for input the code cannot
        # reason about (F02-09's environment allowlist, F05-01's "unknown
        # syntax asks"). A caller opts into real concurrency by passing
        # `tools.footprint_of` bound to its own repository root; nothing here
        # runs two calls together it was not told it could.
        self._footprint_of: FootprintFn = footprint_of or (lambda call: STATEFUL)
        self._concurrency = asyncio.Semaphore(max_concurrent_tools)
        # None means "never compact", which is what chapters 0-5 did.  Kept as
        # the default so that every test written before this chapter still
        # describes the behaviour it was written for; a run that wants
        # compaction says how big its window is, because nothing else in this
        # program can find that out.
        self.context_window = context_window
        self.summariser = summariser
        # Where the conversation is written down as it happens.  Defaults to a
        # writer that writes nothing, so every test from chapters 0-6 keeps
        # describing the behaviour it was written for.
        self.rollout = rollout
        # A history loaded from disk, or None for a fresh conversation.  The
        # agent does not know how it was loaded and does not do the loading:
        # `rollout.read_rollout(...).history()` is a pure function of a file,
        # which is why chapter 7 can test recovery without an agent at all.
        self.resume_from = resume_from
        self.calibration = Calibration()
        # The system message, or None for the chapters 0-4 behaviour of sending
        # none at all.  Optional rather than mandatory because every test
        # written before chapter 5 asserts the exact message list, and making
        # this unconditional would have rewritten all of them to prove a point
        # about a feature they are not testing.
        self.instructions = instructions
        # Asked once, at the moment the model stops asking for tools: is there
        # anything on record saying the task is not finished?  A callable of no
        # arguments returning a note or `None`, rather than anything the loop
        # would have to understand: chapter 11 hands it a plan, and `agent.py`
        # stays a module that has never heard of one.  `None` is chapter 0's
        # behaviour exactly -- absence of tool calls ends the run.
        self.on_stop = on_stop
        # Asked once at the top of every turn, before the request is sized:
        # is there fresh AGENTS.md content for wherever the shell's `cd` has
        # left it (chapter 13). `None` is every earlier chapter's behaviour --
        # no message the code did not already know it was sending.
        self.on_turn_start = on_turn_start
        self.retry_policy = retry_policy
        # Where "waiting 46 seconds because the provider said so" is said out
        # loud.  `None` rather than `print` as the default so that every test
        # written before this chapter keeps its output, and so that nothing
        # embedding this class gets written to on stdout without asking.
        self.announce = announce

    def _say(self, message: str) -> None:
        if self.announce is not None:
            self.announce(message)

    # -- assembling one response --------------------------------------------

    async def _collect(self, stream: AsyncIterator[StreamEvent]) -> ModelTurn:
        """Turn a stream of events into one finished turn, or refuse to.

        Two accumulators because the wire has two granularities: prose arrives
        one token at a time and gets joined, tool calls arrive whole and get
        placed by `index`.  Sorting by index rather than trusting arrival order
        costs one line and removes a question nobody wants to answer later.

        Nothing is returned until `Completed` arrives.  A stream that dies
        halfway therefore leaves nothing behind -- there is no half-built turn
        that could reach the history by accident.
        """
        text_parts: list[str] = []
        by_index: dict[int, ToolCallDelta] = {}
        completed: Completed | None = None
        usage: Usage | None = None

        async for event in stream:
            if isinstance(event, TextDelta):
                text_parts.append(event.text)
            elif isinstance(event, ToolCallDelta):
                by_index[event.index] = event
            elif isinstance(event, Usage):
                usage = event
            elif isinstance(event, Completed):
                completed = event

        if completed is None:
            raise IncompleteStreamError(
                f"stream ended after {len(text_parts)} text chunk(s) and "
                f"{len(by_index)} tool call(s) without a [DONE] sentinel"
            )

        calls = []
        for index in sorted(by_index):
            raw = by_index[index]
            try:
                parsed = json.loads(raw.arguments) if raw.arguments else {}
                arguments = parsed if isinstance(parsed, dict) else None
            except json.JSONDecodeError:
                arguments = None
            calls.append(ToolCall(raw.call_id, raw.name, arguments, raw.arguments))

        return ModelTurn("".join(text_parts), tuple(calls), completed.reason, usage)

    # -- running one tool ----------------------------------------------------

    async def _run_tool(self, call: ToolCall) -> str:
        """Execute one call.  Always returns text; never raises.

        One rule covers every way this goes wrong: whatever happened inside a
        tool is information the model needs, so it comes back as output.  A
        raised exception ends the session; a returned error lets the model try
        something else.

        Each message says three things -- what went wrong, what is available,
        and what to do next.  The model reads these and acts on them, so they
        are prompts whether or not anyone calls them that.
        """
        if call.name not in self.tools:
            available = ", ".join(sorted(self.tools)) or "(none)"
            return (
                f"Error: no tool named {call.name!r}. "
                f"Available tools: {available}. "
                "Call one of those instead."
            )

        if call.arguments is None:
            return (
                f"Error: arguments for {call.name!r} were not valid JSON. "
                f"Received: {call.raw_arguments[:200]!r}. "
                "Send a single JSON object."
            )

        async with self._concurrency:
            try:
                return await self.tools[call.name](call.arguments)
            except Exception as exc:  # deliberately broad; see the docstring
                return f"Error: {call.name} raised {type(exc).__name__}: {exc}"

    # -- keeping the request inside the window ------------------------------

    def _sizer(self) -> Sizer:
        tools = tuple(getattr(self.model, "tools", ()) or ())
        return Sizer(tools=tools, ratio=self.calibration.ratio)

    async def _maybe_compact(self, history: History) -> tuple[History, CompactionResult | None]:
        """Shrink the conversation if the next request would not fit.

        Called from exactly one place: the top of the loop, before the request
        is built.  That is F06-11, and it is enforced by where the call is
        rather than by a flag -- there is no path from inside `_collect` to
        here, so compaction cannot land in the middle of a stream and leave
        half a turn describing a history that no longer exists.

        The size that matters is the size of the *next* request, which is this
        history plus the tool schemas, corrected by whatever the server has
        told us so far.
        """
        if self.context_window is None or self.summariser is None:
            return history, None

        sizer = self._sizer()
        estimated = sizer.messages(history.to_wire(self.dialect))
        if estimated <= self.context_window * COMPACT_AT:
            return history, None

        result = await compact(
            history,
            summarise=self.summariser,
            budget=int(self.context_window * COMPACT_TO),
            sizer=sizer,
        )
        self.recorder.record(
            "compaction",
            {
                "before_tokens": estimated,
                "dropped": result.plan.drops,
                "generation": result.generation,
                "degraded": result.degraded,
                "calibration": self.calibration.describe(),
                "fits": result.plan.fits,
            },
        )
        # The rollout is append-only, so a history that has been *replaced*
        # cannot be expressed by editing what is already on disk.  It is
        # expressed by writing a marker and then the new baseline after it;
        # `read_rollout` restarts its item list at the last marker.  The old
        # turns stay in the file, unread by the loader and available to anyone
        # reading it as a record.
        self.rollout.mark("compacted", generation=result.generation, replaced=result.plan.drops)
        return self._attach(result.history), result

    def _attach(self, history: History) -> History:
        """Re-point a history at the rollout, writing it out as it goes.

        `compact()` builds a plain `History` -- it has no business knowing about
        files -- so the agent hands the new one the observer and replays it.
        """
        rebuilt = History(observer=self.rollout.append)
        for item in history.items:
            replay(rebuilt, item)
        return rebuilt

    # -- one response, however many attempts it takes ------------------------

    async def _respond(
        self, history: History, turn_index: int
    ) -> tuple[ModelTurn, History, int, CompactionResult | None]:
        """Get one finished turn out of the provider, or raise `ModelFailed`.

        The retry boundary is here, around *stream plus assembly*, and not one
        level down inside `model.stream()`.  That is not tidiness: `stream()`
        is a generator that has already yielded `TextDelta`s by the time a
        connection drops, and there is no way to un-yield them.  `_collect`
        returns nothing until `[DONE]` arrives (chapter 0, F00-04), so this is
        the innermost place where a failed attempt has produced *nothing* --
        which is what makes a second attempt a repeat rather than a duplicate.
        F12-03 and F12-09 are both consequences of that one property, and
        neither needed code.

        The history is a return value because one disposition changes it:
        `shrink` compacts and tries again, so the caller must not go on using
        the object it passed in.
        """
        # Wall clock for the whole turn, not just the sleeping.  The first
        # version counted only the waits, which made `budget` a bound on
        # nothing: four attempts that each time out after 120 seconds spend
        # eight minutes without sleeping once.  Measured in `probe_retry.py
        # nesting`, where a sub-agent given a 1.0s deadline reported `timeout`
        # 10/10 in *both* configurations -- the child's own deadline was doing
        # all the bounding and the retry budget was decorative.
        began = time.monotonic()
        shrunk = False
        forced: CompactionResult | None = None

        for attempt in range(self.retry_policy.attempts):
            # to_wire() refuses to render a history with unanswered calls, so a
            # loop that forgets to answer one fails here -- locally, with the
            # offending ids named -- instead of as a 400 from whichever provider
            # happens to be strict.  Rebuilt every attempt, because `shrink`
            # replaces the history under us.
            messages = history.to_wire(self.dialect)
            estimated = self._sizer().messages(messages)
            self.recorder.record(
                "request", {"turn": turn_index, "attempt": attempt, "messages": messages}
            )

            try:
                turn = await self._collect(self.model.stream(messages))
            except Exception as exc:  # not BaseException: a Ctrl-C is not a failure
                failure = classify(exc)
                # Inside the clause, not after it: Python deletes the name
                # bound by `except ... as` when the block ends, and ruff said
                # so (F821) before any test could.
                evidence = failure_record(exc)
            else:
                # The one moment the guess can be checked against the truth.  It
                # is done unconditionally, not only when compaction is enabled:
                # a run that never compacts still produces the observation that
                # tells the next one how wrong its estimator is.
                if turn.usage is not None:
                    self.calibration.observe(estimated=estimated, actual=turn.usage.prompt_tokens)
                return turn, history, estimated, forced

            self.recorder.record(
                "model_failure",
                {
                    "turn": turn_index,
                    "attempt": attempt,
                    "disposition": failure.disposition,
                    "kind": failure.kind,
                    "detail": failure.detail,
                    "retry_after": failure.retry_after,
                    "request_id": failure.request_id,
                    # ...and the evidence, not only the verdict.  Chapter 14:
                    # a 429 recorded as a sentence is a 429 nobody can make
                    # happen again.
                    **evidence,
                },
            )

            if failure.disposition == "shrink":
                history, forced = await self._shrink(history, failure, estimated, shrunk=shrunk)
                shrunk = True
                continue

            elapsed = time.monotonic() - began
            if failure.disposition == "fatal":
                raise ModelFailed(failure, attempts=attempt + 1, waited=elapsed)

            wait = wait_for(failure, attempt, self.retry_policy, elapsed=elapsed)
            if wait is None:
                raise ModelFailed(failure, attempts=attempt + 1, waited=elapsed)

            self._say(
                f"[{failure.kind}: waiting {wait:.0f}s, attempt "
                f"{attempt + 2} of {self.retry_policy.attempts}]"
            )
            # `asyncio.sleep`, so a Ctrl-C during the wait lands immediately
            # (F12-07).  The equivalent `time.sleep` would block the event loop
            # for the whole backoff -- and would also stop every other tool,
            # sub-agent and MCP reader in the process, which is a bigger fault
            # than the one it is inside.
            await asyncio.sleep(wait)

        raise ModelFailed(
            failure, attempts=self.retry_policy.attempts, waited=time.monotonic() - began
        )

    async def _shrink(
        self, history: History, failure: Failure, estimated: int, *, shrunk: bool
    ) -> tuple[History, CompactionResult]:
        """React to "your messages resulted in 160008 tokens".

        This is the one failure the provider hands back that is a fact about
        *us*: chapter 6 compacts on an estimate, and the estimate was low.  A
        400 saying so is ground truth arriving through the error channel, which
        is also the only channel that carries it -- a rejected request produces
        no usage chunk, so the calibration that chapter 6 lives on has no other
        way to learn about the request that mattered most.

        Compaction is forced rather than reconsidered: `_maybe_compact` would
        look at the same estimate that just proved wrong and decide there is
        nothing to do.  Once per turn, because a second identical failure means
        the summary itself does not fit and trying again is a loop.
        """
        if shrunk:
            raise ModelFailed(
                replace(
                    failure,
                    detail=f"{failure.detail} (still too long after compacting once)",
                )
            )
        if self.context_window is None or self.summariser is None:
            raise ModelFailed(failure)

        if failure.stated_tokens is not None:
            _limit, actual = failure.stated_tokens
            # Clamped, and the clamp was written after watching what happens
            # without it.  `Calibration.observe` keeps the *latest* ratio, and
            # this observation does not come from a usage chunk about a request
            # the server answered -- it comes from an error body about a request
            # it refused.  With a stubbed refusal whose stated size has nothing
            # to do with the request actually sent, one observation moved the
            # ratio to x250, and the run then compacted on **11 of its 12
            # turns** and never finished: a correction that large makes every
            # later estimate exceed the window, so compaction fires, cuts,
            # summarises, and the next estimate is still over.
            #
            # Chapter 6 measured this program's raw estimator at 0.43x to 1.04x
            # of the truth, so a single turn asking for more than a 4x
            # correction is not new information about the content mix, it is
            # something else -- a proxy, a different model, a lying body.
            correction = actual / estimated if estimated > 0 else 0.0
            if 0 < correction <= MAX_REFUSAL_CORRECTION:
                self.calibration.observe(estimated=estimated, actual=actual)
            else:
                self.recorder.record(
                    "calibration_rejected",
                    {"estimated": estimated, "stated": actual, "correction": correction},
                )

        result = await compact(
            history,
            summarise=self.summariser,
            budget=int(self.context_window * COMPACT_TO),
            sizer=self._sizer(),
        )
        self.recorder.record(
            "compaction",
            {
                "forced_by": failure.kind,
                "before_tokens": estimated,
                "dropped": result.plan.drops,
                "generation": result.generation,
                "degraded": result.degraded,
                "calibration": self.calibration.describe(),
                "fits": result.plan.fits,
            },
        )
        if result.plan.saving <= 0:
            # There was nothing to cut.  The request is too big *by itself* --
            # one enormous tool result, or a window smaller than the system
            # prompt (F06-09) -- and sending the identical thing again is
            # F12-01 with a compaction in front of it.
            raise ModelFailed(
                replace(failure, detail=f"{failure.detail} (nothing left to compact)")
            )

        self._say(f"[context too long for the provider; compacted {result.plan.drops} message(s)]")
        self.rollout.mark("compacted", generation=result.generation, replaced=result.plan.drops)
        return self._attach(result.history), result

    # -- the loop ------------------------------------------------------------

    async def run(self, user_message: str) -> RunResult:
        if self.resume_from is not None:
            history = self._attach(self.resume_from)
        else:
            history = History(observer=self.rollout.append)
            if self.instructions is not None:
                history.add_system_note(self.instructions)
        history.add_user(user_message)
        final_text = ""
        compactions: list[CompactionResult] = []
        nudged = False

        for turn_index in range(self.max_turns):
            remaining = self.max_turns - turn_index

            # Before the request is sized, not after: a fresh AGENTS.md block
            # is part of what might need compacting, same as everything else
            # in the history (F13-10's byte cap keeps any single check small,
            # but nothing stops the *history* from accumulating several).
            if self.on_turn_start is not None:
                note = self.on_turn_start()
                if note is not None:
                    history.add_developer_note(note)

            history, compaction = await self._maybe_compact(history)
            if compaction is not None:
                compactions.append(compaction)

            # A budget the model cannot see is one it spends freely and is then
            # killed by, mid-thought, with nothing to show.  Two messages, not
            # one: the last turn is a different thing from the second-to-last,
            # because on the last turn the loop takes the tools away.
            if remaining == 1:
                history.add_system_note(FINAL_TURN_WARNING)
            elif remaining <= BUDGET_WARNING_AT:
                history.add_system_note(BUDGET_WARNING.format(remaining=remaining))

            # Four values rather than a small class: a `Response` object would
            # have exactly one construction site and one read site, which is
            # interlude B's FB-03 in miniature.  The fourth is a compaction the
            # *provider* forced, which the run still has to report.
            turn, history, estimated, forced = await self._respond(history, turn_index)
            if forced is not None:
                compactions.append(forced)

            self.recorder.record(
                "response",
                {
                    "turn": turn_index,
                    "text": turn.text,
                    "finish_reason": turn.finish_reason,
                    "tool_calls": [call_record(c) for c in turn.tool_calls],
                    "estimated_prompt_tokens": estimated,
                    "actual_prompt_tokens": turn.usage.prompt_tokens if turn.usage else None,
                    "calibration": self.calibration.describe(),
                },
            )

            history.add_assistant(turn.text, turn.tool_calls)
            if turn.text:
                final_text = turn.text

            # Text is not a stop signal.  Models narrate before acting, and
            # stopping on the narration leaves the work undone while looking
            # exactly like success.
            if not turn.tool_calls:
                # Chapter 11: the model has stopped asking for tools, which is
                # the only stop signal chapter 0 had.  If something on record
                # says the task is not finished, say so and let it carry on --
                # **once**.  The bound is here, in the loop, and not in the
                # callback: a nudge that can renew itself is a turn budget
                # spent arguing, and the caller cannot be trusted to count.
                if self.on_stop is not None and not nudged and remaining > 1:
                    nudged = True
                    note = self.on_stop()
                    if note is not None:
                        history.add_system_note(note)
                        self.recorder.record("nudge", {"turn": turn_index, "note": note})
                        continue
                return RunResult(
                    final_text, "completed", turn_index + 1, history, tuple(compactions)
                )

            # The last turn is reserved for an answer.  The model was told so
            # (`FINAL_TURN_WARNING`) and it asked for a tool anyway, which is
            # the case that produced four empty answers out of five before this
            # existed: the loop would run the call, append a result nobody
            # reads, and fall out of the `for` with `final_text` still "".
            # Every issued call is still answered -- F07-04's rule has no
            # exception for a call the loop chose not to make.
            if remaining == 1:
                for call in turn.tool_calls:
                    history.add_tool_result(call.call_id, BUDGET_DENIAL)
                self.rollout.mark("budget_exhausted", turn=turn_index)
                return RunResult(
                    final_text, "turn_limit", turn_index + 1, history, tuple(compactions)
                )

            # Every call runs and every call is answered -- unchanged since
            # chapter 0.  What changed is *when*: `batches()` groups this
            # turn's calls so that two whose footprints do not conflict share
            # one `asyncio.gather()`, and two that do (same file, or either
            # one `STATEFUL`) land in different batches and never overlap.
            # Results are collected by call_id and appended in the model's
            # original order, so the persisted transcript does not gain a
            # second source of nondeterminism on top of "which one happened
            # to finish first."
            plan = batches(turn.tool_calls, self._footprint_of)
            outputs: dict[str, str] = {}
            interrupted_at: int | None = None

            for batch_index, batch in enumerate(plan):
                tasks = [asyncio.ensure_future(self._run_tool(call)) for call in batch]
                try:
                    results = await asyncio.gather(*tasks)
                except (asyncio.CancelledError, KeyboardInterrupt):
                    # gather() cancels every task it is still waiting on when
                    # the await on gather() itself is cancelled -- but reading
                    # .cancelled()/.result() before those cancellations have
                    # actually finished unwinding races the tools that were
                    # mid-flight.  return_exceptions=True waits for that to
                    # settle instead of raising a second time.
                    settled = await asyncio.gather(*tasks, return_exceptions=True)
                    for call, task, outcome in zip(batch, tasks, settled, strict=True):
                        if task.cancelled():
                            outputs[call.call_id] = (
                                "Error: interrupted by the user before this finished. "
                                "It may have run partially, or not at all."
                            )
                        elif isinstance(outcome, BaseException):
                            outputs[call.call_id] = f"Error: interrupted by the user: {outcome}"
                        else:
                            outputs[call.call_id] = outcome
                    interrupted_at = batch_index
                    break
                else:
                    for call, output in zip(batch, results, strict=True):
                        outputs[call.call_id] = output

            if interrupted_at is not None:
                for later_batch in plan[interrupted_at + 1 :]:
                    for call in later_batch:
                        outputs[call.call_id] = (
                            "Error: interrupted by the user before this started."
                        )
                for call in turn.tool_calls:
                    history.add_tool_result(call.call_id, outputs[call.call_id])
                self.rollout.mark("interrupted", turn=turn_index)
                history.add_system_note(interrupted_note())
                return RunResult(
                    final_text, "interrupted", turn_index + 1, history, tuple(compactions)
                )

            for call in turn.tool_calls:
                history.add_tool_result(call.call_id, outputs[call.call_id])

        return RunResult(final_text, "turn_limit", self.max_turns, history, tuple(compactions))
