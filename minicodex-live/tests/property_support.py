"""Turning a property failure into something a person can read.

Chapter 6 wrote the generator and said what it was giving up: "no shrinking, so
a failure prints a seed rather than a minimal example, and reproducing it means
running `MINICODEX_PROPERTY_SEED=<n>`.  Chapter 14 revisits this."

The cost of that trade was paid once and is worth quoting.  The bug that
justified the whole property suite -- compaction making a history *bigger* --
was found at seed 235, whose history has fourteen items.  Thirteen of them are
scenery.  What the report said was the number 235, and what the author then did
was print the history and read it.

`minimise` does the reading.  It is delta debugging in its simplest useful
form: keep removing items while the failure survives, halving-first so a long
run of irrelevant turns disappears in one step rather than fourteen.

It is deliberately not a general shrinker.  It shrinks *this* project's one
generated type, using `History`'s own API to rebuild -- so a candidate is never
an illegal history, and a failure it reports is never an artefact of the
shrinker having built something the generator could not have produced.
"""

from __future__ import annotations

from collections.abc import Callable

from minicodex.history import (
    AssistantMessage,
    DeveloperNote,
    History,
    SystemNote,
    ToolResult,
    UserMessage,
)


def rebuild(items: list) -> History | None:
    """A history containing exactly these items, or `None` if that is illegal.

    Chapter 1's invariant lives in `History`, so dropping a tool call and
    keeping its result is refused here rather than producing a candidate that
    fails for a reason the property test was not asking about.
    """
    history = History()
    try:
        for item in items:
            if isinstance(item, UserMessage):
                history.add_user(item.text)
            elif isinstance(item, AssistantMessage):
                history.add_assistant(item.text, item.tool_calls)
            elif isinstance(item, ToolResult):
                history.add_tool_result(item.call_id, item.content)
            elif isinstance(item, SystemNote):
                history.add_system_note(item.text)
            elif isinstance(item, DeveloperNote):
                history.add_developer_note(item.text)
    except Exception:
        return None
    return history


def minimise(history: History, fails: Callable[[History], bool], *, rounds: int = 40) -> History:
    """The smallest history this can reach that still fails.

    `fails` is the property, inverted: it returns True when the candidate
    reproduces the problem.  Chunk sizes halve from "everything but one half"
    down to single items, which is the standard ddmin schedule and is what
    stops a fourteen-item history needing fourteen passes.
    """
    items = list(history.items)
    size = max(len(items) // 2, 1)
    for _ in range(rounds):
        shrunk = False
        index = 0
        while index < len(items):
            candidate_items = items[:index] + items[index + size :]
            candidate = rebuild(candidate_items) if candidate_items else History()
            if candidate is not None and fails(candidate):
                items = candidate_items
                shrunk = True
            else:
                index += size
        if not shrunk:
            if size == 1:
                break
            size = max(size // 2, 1)
    rebuilt = rebuild(items)
    return rebuilt if rebuilt is not None else history
