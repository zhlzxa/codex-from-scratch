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
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

from minicodex.agent_types import ToolCall, ToolFn
from minicodex.compaction import CompactionResult, Sizer, Summariser, compact
from minicodex.history import Dialect, History
from minicodex.model import Completed, StreamEvent, TextDelta, ToolCallDelta, Usage
from minicodex.recorder import NULL_RECORDER, Recorder
from minicodex.rollout import NULL_WRITER, RolloutWriter, interrupted_note, replay
from minicodex.tokens import Calibration


class IncompleteStreamError(RuntimeError):
    """The connection ended before the server sent its `[DONE]` sentinel."""


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

# Compact when the estimate crosses this fraction of the window, down to that
# one.  Two numbers rather than one, because compacting *to* the trigger point
# means compacting again next turn: the gap is what buys the turns in between.
COMPACT_AT = 0.75
COMPACT_TO = 0.45


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
    ) -> None:
        self.model = model
        self.tools = tools or {}
        self.max_turns = max_turns
        self.recorder = recorder
        self.dialect = dialect
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

        for turn_index in range(self.max_turns):
            remaining = self.max_turns - turn_index

            history, compaction = await self._maybe_compact(history)
            if compaction is not None:
                compactions.append(compaction)

            # A budget the model cannot see is one it spends freely and is then
            # killed by, mid-thought, with nothing to show.
            if remaining <= BUDGET_WARNING_AT:
                history.add_system_note(
                    f"You have {remaining} tool-calling turn(s) left. "
                    "Wrap up and give your best answer now."
                )

            # to_wire() refuses to render a history with unanswered calls, so a
            # loop that forgets to answer one fails here -- locally, with the
            # offending ids named -- instead of as a 400 from whichever provider
            # happens to be strict.
            messages = history.to_wire(self.dialect)
            estimated = self._sizer().messages(messages)
            self.recorder.record("request", {"turn": turn_index, "messages": messages})

            turn = await self._collect(self.model.stream(messages))

            # The one moment the guess can be checked against the truth.  It is
            # done unconditionally, not only when compaction is enabled: a run
            # that never compacts still produces the observation that tells the
            # next one how wrong its estimator is.
            if turn.usage is not None:
                self.calibration.observe(estimated=estimated, actual=turn.usage.prompt_tokens)

            self.recorder.record(
                "response",
                {
                    "turn": turn_index,
                    "text": turn.text,
                    "finish_reason": turn.finish_reason,
                    "tool_calls": [{"id": c.call_id, "name": c.name} for c in turn.tool_calls],
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
                return RunResult(
                    final_text, "completed", turn_index + 1, history, tuple(compactions)
                )

            # Every call runs and every call is answered.  add_tool_result is
            # what makes "answered" mean something: it refuses an id that was
            # never issued, and refuses to answer the same id twice.
            # `_run_tool` promises never to raise, and that promise has a hole
            # in it that only shows up here: `except Exception` does not catch
            # `CancelledError`, which is a `BaseException`.  A Ctrl-C during a
            # tool therefore escapes the one function whose whole job is to
            # turn failures into outputs -- leaving the issued call unanswered
            # and the history unsendable.  Answer every call before unwinding.
            for index, call in enumerate(turn.tool_calls):
                try:
                    output = await self._run_tool(call)
                except (asyncio.CancelledError, KeyboardInterrupt):
                    for pending in turn.tool_calls[index:]:
                        history.add_tool_result(
                            pending.call_id,
                            "Error: interrupted by the user before this finished. "
                            "It may have run partially, or not at all.",
                        )
                    self.rollout.mark("interrupted", turn=turn_index)
                    history.add_system_note(interrupted_note())
                    return RunResult(
                        final_text, "interrupted", turn_index + 1, history, tuple(compactions)
                    )
                history.add_tool_result(call.call_id, output)

        return RunResult(final_text, "turn_limit", self.max_turns, history, tuple(compactions))
