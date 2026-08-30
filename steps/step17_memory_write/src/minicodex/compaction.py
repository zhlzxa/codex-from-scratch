"""Making a long conversation shorter without making it invalid.

The naive versions all have the same bug, and it is not the one the fault list
predicted.  Measured against gpt-4o-mini on 2026-08-10 (`probe_cut_points.py`),
cutting one eight-message history at every possible index:

        cut  kept                                          status
          0  S U A[a1] T[a1] A[b2] T[b2] A[c3] T[c3]       200
          1    U A[a1] T[a1] A[b2] T[b2] A[c3] T[c3]       200
          2      A[a1] T[a1] A[b2] T[b2] A[c3] T[c3]       200
          3            T[a1] A[b2] T[b2] A[c3] T[c3]       400
          4                  A[b2] T[b2] A[c3] T[c3]       200
          5                        T[b2] A[c3] T[c3]       400
          6                              A[c3] T[c3]       200
          7                                    T[c3]       400

The 400s say `messages with role 'tool' must be a response to a preceeding
message with 'tool_calls'`.  They are the easy half: loud, immediate, and
impossible to ship.

The dangerous half is the 200 column.  The user asked *which Python version
this project supports*.  Cut 0 answers it: "the project supports Python 3.10
and above".  Cut 6 is equally valid, costs a third of the tokens, and answers a
different question -- "you are using Python version 3.13.0" -- because the
message saying what was asked is no longer there.  Same history, same model, no
error, fluent wrong answer.

So compaction has two jobs.  Keep the conversation renderable, and keep the
parts that say what it is for.  This module is `boundaries()` for the first and
`Protected` for the second; everything else is about the summary that replaces
what was cut.
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Any

from minicodex import compaction_prompt
from minicodex.history import (
    AssistantMessage,
    History,
    HistoryItem,
    SystemNote,
    ToolResult,
    UserMessage,
)
from minicodex.model import ModelHTTPError
from minicodex.retry import ModelFailed, classify
from minicodex.tokens import CHARS_PER_TOKEN, estimate_messages

# Prefix on the system note that carries a summary.  A marker rather than a
# separate item type: the model must read it as an instruction-shaped note like
# any other, and a new `HistoryItem` variant would need a rendering rule in
# every dialect for no gain.  It is parsed back out only to count generations.
SUMMARY_MARKER = "[compacted transcript"

# How much of the budget the summary itself may occupy.  Not a round number for
# its own sake: the summary is re-sent on every subsequent turn for the rest of
# the session, so it is the one item whose cost is multiplied by everything
# that follows it.
SUMMARY_TOKEN_BUDGET = 700

# A single tool result larger than this is clipped before it is allowed into
# the kept tail (F06-09).  Chapter 2 already clips shell output at the tool;
# this is the second line, for everything that is not the shell.
MAX_ITEM_TOKENS = 2000


class CompactionError(RuntimeError):
    """Compaction could not produce a history that fits."""


@dataclass(frozen=True)
class SummaryRequest:
    """Everything the summariser is allowed to see.

    Three fields rather than one transcript string, and the third one was
    bought with a wrong answer.  The first version passed only the region being
    destroyed, which is exactly the region that does **not** contain the
    protected prefix -- so the model was asked to write a `## Goal` section
    while the message stating the goal was deliberately withheld from it.  It
    did what a model does with a required heading and no evidence: it inferred
    one.  Measured, 3/3 on gpt-4o-mini, the summary of a session about *adding
    retry logic to src/net.py* opened with

        ## Goal
        Record the change in CHANGELOG.md.

    which is not the goal, it is the last remaining task.  That note then sits
    in the history one line below the real user message, contradicting it, with
    the authority of a system note.

    `context` fixes it by handing over the protected prefix as read-only
    material.  It is not summarised and not replaced -- it is still in the
    rebuilt history verbatim -- it is there so the summary can be *consistent*
    with it.
    """

    transcript: str
    """The region about to be destroyed.  The only part being replaced."""

    context: str
    """The protected prefix, verbatim.  Survives on its own; shown for
    agreement, not for compression."""

    established: str | None
    """The previous generation's summary, if this is not the first compaction."""

    generation: int
    """How many compactions this session has already survived."""


Summariser = Callable[["SummaryRequest"], Awaitable[str]]


# ---------------------------------------------------------------------------
# where a cut is allowed
# ---------------------------------------------------------------------------


def boundaries(items: Sequence[HistoryItem]) -> tuple[int, ...]:
    """Every index at which the conversation may be cut.

    A cut at index `i` keeps `items[i:]`.  It is legal exactly when no call
    issued before `i` is answered after it -- a statement about the shape of
    the list, not about how many messages or tokens sit on either side.  Both
    naive rules ("drop the oldest half", "keep the last N") are right only by
    luck, and the luck is parity: on the eight-message history above, half is
    index 4 and legal; on a nine-message one it is index 4 or 5, and one of
    those is a 400.

    Verified against the server rather than against the docs: this function's
    output for that history is `(0, 1, 2, 4, 6, 8)`, and indices 0-7 are
    exactly the ones the API answered 200 to.
    """
    open_calls = 0
    result = []
    for index, item in enumerate(items):
        if open_calls == 0:
            result.append(index)
        if isinstance(item, AssistantMessage):
            open_calls += len(item.tool_calls)
        elif isinstance(item, ToolResult):
            open_calls -= 1
    if open_calls == 0:
        result.append(len(items))
    return tuple(result)


@dataclass(frozen=True)
class Protected:
    """The prefix compaction is not allowed to touch.

    Two things, for two different reasons:

    * the **system notes at the front** -- the instructions and the permission
      block.  An agent that forgets those does not degrade, it becomes a
      different agent, and chapter 5 measured what one does when it no longer
      knows its own permissions;
    * the **first user message** -- the only record of what was asked.  Cut 6
      in the table above is what its absence looks like, and it looks like
      success.

    Later system notes are deliberately *not* protected.  Chapter 0's
    turn-budget warning is a `SystemNote` too, and it is worth exactly one
    turn; protecting by type rather than by position would accumulate every
    stale warning forever.
    """

    count: int

    @staticmethod
    def of(items: Sequence[HistoryItem]) -> Protected:
        index = 0
        while index < len(items) and isinstance(items[index], SystemNote):
            index += 1
        if index < len(items) and isinstance(items[index], UserMessage):
            index += 1
        return Protected(index)


# ---------------------------------------------------------------------------
# rendering the part that is about to be destroyed
# ---------------------------------------------------------------------------


def render_transcript(items: Sequence[HistoryItem]) -> str:
    """The dropped region, as text for the summariser.

    Not `to_wire()`: this is going into a prompt as *content*, and the wire
    format's nesting would spend tokens on JSON punctuation the summariser does
    not need.  Tool results keep their call's name, because "the output of
    `read_file`" and "the output of `run_shell`" mean different things to
    whoever reads this next.
    """
    lines: list[str] = []
    for item in items:
        if isinstance(item, UserMessage):
            lines.append(f"USER: {item.text}")
        elif isinstance(item, SystemNote):
            lines.append(f"SYSTEM: {item.text}")
        elif isinstance(item, AssistantMessage):
            if item.text:
                lines.append(f"ASSISTANT: {item.text}")
            for call in item.tool_calls:
                lines.append(f"ASSISTANT CALLS {call.name}({call.raw_arguments})")
        elif isinstance(item, ToolResult):
            lines.append(f"RESULT OF {item.name}:\n{item.content}")
    return "\n".join(lines)


def _previous_summary(items: Sequence[HistoryItem]) -> tuple[str | None, int]:
    """The surviving summary from an earlier compaction, and its generation.

    Generation is counted rather than inferred because it is the only visible
    handle on decay: a summary of a summary of a summary is still one system
    note, and nothing about its text says how many times it has been through
    this.
    """
    for item in reversed(list(items)):
        if isinstance(item, SystemNote) and item.text.startswith(SUMMARY_MARKER):
            header, _, body = item.text.partition("\n")
            # Anchored to the word before it, not "the digits in the header".
            # The loose version read the *last* number in
            # `[compacted transcript | generation 1 | 24 message(s) replaced |
            # 2026-08-10 06:22]` and returned 24, then 22 -- a generation
            # counter that climbed by whatever happened to be in the timestamp.
            # Found by a test that asserted the second compaction was
            # generation 2 and got 24.
            tokens = header.replace("]", " ").split()
            generation = 1
            for index, token in enumerate(tokens[:-1]):
                if token == "generation" and tokens[index + 1].isdigit():
                    generation = int(tokens[index + 1])
                    break
            return body.strip() or None, generation
    return None, 0


# ---------------------------------------------------------------------------
# clipping one oversized item
# ---------------------------------------------------------------------------


def clip_item(item: HistoryItem, *, max_tokens: int = MAX_ITEM_TOKENS) -> HistoryItem:
    """Shrink a single tool result that is too big to keep whole (F06-09).

    Compaction cannot help here.  Dropping older turns does nothing when the
    thing that does not fit is one 400KB stack trace in the most recent turn,
    and it is precisely the most recent turn that must be kept.

    Head and tail, like chapter 2's `_clip`, and for the same measured reason:
    a traceback puts the exception at the top and the summary line at the
    bottom, and tail-only truncation deletes the half that says what broke.
    """
    if not isinstance(item, ToolResult):
        return item
    limit_chars = max_tokens * 4
    if len(item.content) <= limit_chars:
        return item
    head = limit_chars // 2
    tail = limit_chars - head
    omitted = len(item.content) - head - tail
    clipped = (
        item.content[:head] + f"\n... ({omitted} characters omitted by compaction; "
        "re-run a narrower command if you need the middle) ...\n" + item.content[-tail:]
    )
    return ToolResult(item.call_id, item.name, clipped)


# ---------------------------------------------------------------------------
# the plan
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Sizer:
    """One place that answers "how big is this", including the correction.

    It exists because the correction has to reach **both** decisions and the
    first version only reached one.  `plan()` used the calibrated number to
    pick a cut, and `_fit_summary()` used a bare `len(text) // 4` to trim the
    summary -- so the summary was budgeted with the estimator this whole
    chapter exists to distrust, and a summary made of paths and error strings
    (which is what the prompt asks for) overshoots its budget by the full
    measured 2.3x.  The bug is not that one call site was wrong; it is that
    there were two call sites at all.
    """

    tools: tuple[dict[str, Any], ...] = ()
    ratio: float = 1.0

    def messages(self, messages: Sequence[dict[str, Any]]) -> int:
        return int(estimate_messages(messages, self.tools) * self.ratio)

    def text(self, text: str) -> int:
        """Size a bare string -- a candidate summary, not yet a message.

        No tools and no per-message cost: this is asking what the string costs,
        not what a request containing it costs.
        """
        return int(len(text) / CHARS_PER_TOKEN * self.ratio)

    def clip_text(self, text: str, budget: int) -> str:
        if self.text(text) <= budget:
            return text
        keep = int(budget * CHARS_PER_TOKEN / self.ratio)
        return text[:keep].rstrip() + "\n... (summary truncated to fit its budget)"


@dataclass(frozen=True)
class Plan:
    """Where to cut, decided before anything is destroyed.

    Separate from doing it so it can be printed, asserted on and tested without
    a model: every decision in compaction is made here, and the execution below
    is mechanical.
    """

    protected: int
    cut: int
    kept_tokens: int
    fits: bool
    saving: int = 0
    """Tokens this plan expects to remove.  Zero means "do nothing", and that
    is a real outcome rather than a failure -- see `plan()`."""

    @property
    def drops(self) -> int:
        return self.cut - self.protected


def plan(
    items: Sequence[HistoryItem],
    *,
    budget: int,
    sizer: Sizer | None = None,
    summary_budget: int = SUMMARY_TOKEN_BUDGET,
    max_item_tokens: int = MAX_ITEM_TOKENS,
) -> Plan:
    """Choose the earliest legal cut whose remainder fits.

    Earliest, not latest: the goal is to destroy as little as possible.  The
    loop walks the legal boundaries in order and stops at the first one that
    fits, so the kept tail is the largest one that can be kept.

    `summary_budget` is reserved up front rather than checked afterwards.
    Deciding the cut and *then* discovering that the summary does not fit is
    F06-12 -- and by then the messages needed to make a different decision have
    been handed to a model and thrown away.  Reserving is what makes the second
    pass unnecessary rather than merely rare.

    **A plan that would not save anything cuts nothing.**  That guard was not
    designed in; a property test found it at seed 235, where compacting an
    89-token history produced a 93-token one.  Compaction is not free: it
    deletes some messages and inserts a summary, and when the deleted region is
    small the summary costs more than it replaced.  Left in, the failure is not
    one wasted call -- the estimate never drops below the trigger, so the next
    turn compacts again, and the session converges on a history made entirely
    of summaries of summaries.
    """
    sizer = sizer or Sizer()
    protected_count = Protected.of(items).count
    head = list(items[:protected_count])
    current = _size(head, items[protected_count:], sizer)
    candidates = [b for b in boundaries(items) if b >= protected_count]
    if not candidates:  # pragma: no cover -- len(items) is always a boundary
        raise CompactionError("no legal cut point at or after the protected prefix")

    def _do_nothing(fits: bool) -> Plan:
        return Plan(protected_count, protected_count, current, fits=fits, saving=0)

    for cut in candidates:
        tail = [clip_item(i, max_tokens=max_item_tokens) for i in items[cut:]]
        size = _size(head, tail, sizer) + summary_budget
        if size <= budget:
            if size >= current and cut > protected_count:
                return _do_nothing(fits=current <= budget)
            if cut == protected_count:
                # Nothing is dropped, so no summary is written and its
                # reservation is not spent.  Reporting `current + reservation`
                # here would be a number describing a request that never
                # happens.
                return _do_nothing(fits=True)
            return Plan(protected_count, cut, size, fits=True, saving=max(current - size, 0))

    # Nothing fits, including dropping everything droppable.  Report it rather
    # than raise: the caller still has to send something, and a plan that says
    # `fits=False` lets it decide what, with the numbers in hand.
    cut = candidates[-1]
    tail = [clip_item(i, max_tokens=max_item_tokens) for i in items[cut:]]
    size = _size(head, tail, sizer) + summary_budget
    if size >= current and cut > protected_count:
        return _do_nothing(fits=False)
    return Plan(protected_count, cut, size, fits=False, saving=max(current - size, 0))


def _size(head: Sequence[HistoryItem], tail: Sequence[HistoryItem], sizer: Sizer) -> int:
    scratch = History()
    _replay(scratch, list(head) + list(tail))
    return sizer.messages(scratch.to_wire())


# ---------------------------------------------------------------------------
# doing it
# ---------------------------------------------------------------------------


def _replay(history: History, items: Sequence[HistoryItem]) -> None:
    """Rebuild a history by putting every item back through the front door.

    This is the whole reason chapter 1 enforced its invariant on append rather
    than checking it on send.  A compaction that produces an orphaned result
    does not travel to a provider and come back as a 400 with a spelling
    mistake in it; it raises `HistoryError` here, in this process, naming the
    call id, on the line that planned the cut.
    """
    for item in items:
        if isinstance(item, UserMessage):
            history.add_user(item.text)
        elif isinstance(item, SystemNote):
            history.add_system_note(item.text)
        elif isinstance(item, AssistantMessage):
            history.add_assistant(item.text, item.tool_calls)
        elif isinstance(item, ToolResult):
            history.add_tool_result(item.call_id, item.content)
        else:  # pragma: no cover
            raise AssertionError(f"unreplayable item: {item!r}")


def _hard_summary(items: Sequence[HistoryItem], reason: str) -> str:
    """What to say when the summariser could not be reached (F06-08).

    Compaction is triggered by being out of room, so "try again later" is not
    available -- the next request is the one that does not fit.  The fallback
    is deterministic and local: state that context was lost, state how much,
    and say so *to the model*, because the alternative is an agent that quietly
    forgets and confidently proceeds.
    """
    kinds: dict[str, int] = {}
    for item in items:
        kinds[type(item).__name__] = kinds.get(type(item).__name__, 0) + 1
    breakdown = ", ".join(f"{count} {name}" for name, count in sorted(kinds.items()))
    return (
        "## Goal\n(lost)\n\n"
        "## Done\n(lost)\n\n"
        "## Decisions\n(lost)\n\n"
        "## Constraints\n(lost)\n\n"
        "## Open\n(lost)\n\n"
        "## Key data\n(lost)\n\n"
        f"**This summary could not be generated** ({reason}). "
        f"{len(items)} earlier message(s) were discarded unread ({breakdown}). "
        "Do not assume any earlier step succeeded. Before continuing, re-check "
        "the current state with a tool -- read the files you believe you wrote, "
        "re-run the command you believe passed -- and ask the user if the goal "
        "is no longer clear."
    )


@dataclass(frozen=True)
class CompactionResult:
    history: History
    plan: Plan
    summary: str
    generation: int
    degraded: bool

    def describe(self) -> str:
        state = "degraded" if self.degraded else "summarised"
        return (
            f"compacted: dropped {self.plan.drops} item(s), {state}, "
            f"generation {self.generation}, ~{self.plan.kept_tokens} tokens"
        )


async def compact(
    history: History,
    *,
    summarise: Summariser,
    budget: int,
    sizer: Sizer | None = None,
    summary_budget: int = SUMMARY_TOKEN_BUDGET,
    max_item_tokens: int = MAX_ITEM_TOKENS,
) -> CompactionResult:
    """Replace the middle of the conversation with a summary of it.

    The order matters and is not the obvious one: **plan, then summarise, then
    rebuild.**  Summarising first and deciding the cut afterwards means the
    summary describes a region that may not be the one removed.
    """
    sizer = sizer or Sizer()
    items = history.items
    the_plan = plan(
        items,
        budget=budget,
        sizer=sizer,
        summary_budget=summary_budget,
        max_item_tokens=max_item_tokens,
    )
    dropped = items[the_plan.protected : the_plan.cut]
    previous, generation = _previous_summary(items)

    if not dropped:
        # Nothing may be removed, but an oversized single item may still be
        # clipped -- and if it cannot, the caller needs to know that no amount
        # of compaction will help.
        rebuilt = History()
        _replay(
            rebuilt,
            list(items[: the_plan.cut])
            + [clip_item(i, max_tokens=max_item_tokens) for i in items[the_plan.cut :]],
        )
        return CompactionResult(rebuilt, the_plan, "", generation, degraded=False)

    try:
        summary = await summarise(
            SummaryRequest(
                transcript=render_transcript(dropped),
                context=render_transcript(items[: the_plan.protected]),
                established=previous,
                generation=generation,
            )
        )
        degraded = False
        if not summary.strip():
            raise CompactionError("summariser returned an empty summary")
    except Exception as exc:
        # Chapter 12: not every failure of this call deserves the degraded
        # path.  A dropped connection does -- the transcript is rebuilt without
        # a model and the run carries on.  A 401 does not: the very next
        # request fails for the same reason, so degrading here **destroys the
        # transcript on the way to a run that was going to end anyway**, and
        # the reason the user is shown is one layer away from the truth.
        #
        # Narrowed to `ModelHTTPError` on purpose.  `classify` calls anything
        # it does not recognise fatal, and "anything it does not recognise"
        # includes a bug in whatever was passed as `summarise` -- which is
        # precisely the case F06-08 built this branch for.
        if isinstance(exc, ModelHTTPError):
            failure = classify(exc)
            if failure.disposition == "fatal":
                raise ModelFailed(failure) from exc
        summary = _hard_summary(dropped, f"{type(exc).__name__}: {exc}")
        degraded = True

    # The summary is trimmed with the *same* sizer that chose the cut (F06-12).
    # A model told to write six sections will sometimes write six long ones, and
    # a summary over its reservation puts the history straight back where it
    # started -- except that now the messages it was made from are gone, so
    # there is nothing left to compact.  Trimming from the end is deliberate:
    # the prompt orders the sections by how much the next turn needs them.
    summary = sizer.clip_text(summary, summary_budget)
    generation += 1
    note = (
        f"{SUMMARY_MARKER} | generation {generation} | "
        f"{len(dropped)} message(s) replaced | {time.strftime('%Y-%m-%d %H:%M')}]\n"
        f"{summary}"
    )

    rebuilt = History()
    _replay(rebuilt, items[: the_plan.protected])
    rebuilt.add_system_note(note)
    _replay(rebuilt, [clip_item(i, max_tokens=max_item_tokens) for i in items[the_plan.cut :]])
    return CompactionResult(rebuilt, the_plan, summary, generation, degraded)


# ---------------------------------------------------------------------------
# the summariser that actually calls a model
# ---------------------------------------------------------------------------


def make_summariser(model: Any, *, dialect: str = "chat_completions") -> Summariser:
    """A `Summariser` backed by one non-agentic model call.

    Its own `History`, not the session's: the summariser must not see the tool
    schemas, must not be able to call anything, and must not have its output
    land in the conversation it is summarising.  Chapter 10 makes this shape
    general; here it is one function.
    """

    async def summarise(request: SummaryRequest) -> str:
        scratch = History()
        scratch.add_system_note(compaction_prompt())
        blocks = [f"<context>\n{request.context}\n</context>"]
        if request.established:
            blocks.append(f"<established>\n{request.established}\n</established>")
        blocks.append(f"<transcript>\n{request.transcript}\n</transcript>")
        scratch.add_user("\n\n".join(blocks))

        parts: list[str] = []
        saw_end = False
        from minicodex.model import Completed, TextDelta

        async for event in model.stream(scratch.to_wire(dialect)):
            if isinstance(event, TextDelta):
                parts.append(event.text)
            elif isinstance(event, Completed):
                saw_end = True
        if not saw_end:
            # The same rule as chapter 0's loop: a stream that stopped early is
            # not a short summary, it is an unknown one.  Half a summary that
            # replaces a whole transcript is worse than admitting the loss.
            raise CompactionError("summariser stream ended without a [DONE] sentinel")
        return "".join(parts)

    return summarise


def unused_call_ids(items: Sequence[HistoryItem]) -> tuple[str, ...]:
    """Diagnostic: results whose call is not in the same list.

    Nothing in this module calls it -- `_replay` makes the state unreachable.
    It exists so the tests can point at a naive slice and name what is wrong
    with it, in the same vocabulary the server uses.
    """
    issued = {
        call.call_id
        for item in items
        if isinstance(item, AssistantMessage)
        for call in item.tool_calls
    }
    return tuple(i.call_id for i in items if isinstance(i, ToolResult) and i.call_id not in issued)


__all__ = [
    "MAX_ITEM_TOKENS",
    "SUMMARY_MARKER",
    "SUMMARY_TOKEN_BUDGET",
    "CompactionError",
    "CompactionResult",
    "Plan",
    "Protected",
    "boundaries",
    "clip_item",
    "compact",
    "make_summariser",
    "plan",
    "render_transcript",
]
