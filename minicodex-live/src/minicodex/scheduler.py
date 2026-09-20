"""Deciding which tool calls in one turn may run at the same time.

This module knows nothing about tools. It knows about `Footprint`s -- what a
call reads, what it writes, and whether it is safe to reason about at all --
and turns a list of them into batches that can run one `asyncio.gather` at a
time without two conflicting calls ever being in flight together.

The agent loop runs on one invariant: every issued call is
answered exactly once. This module does not get to spend any of that budget
loosening it. Concurrency changes *when* a call runs, never *whether* it runs
or *whether* it is answered -- that is still `agent.py`'s job, unchanged since
the history module enforces.

Interlude B moved `Footprint`, `STATEFUL` and `FootprintFn` down into
`agent_types.py` and left them importable from here.  Not for tidiness: a
sub-agent's tool table has to name the type of its own `footprint_of`, and
`subagent.py` importing this module for a *word* while depending on none of
its behaviour is the backwards import arrow that would undo the layering
fault on.  Everything you can *do* with a `Footprint` is still here.
"""

from __future__ import annotations

from collections.abc import Sequence

from minicodex.agent_types import STATEFUL, Footprint, FootprintFn, ToolCall

__all__ = ["STATEFUL", "Footprint", "FootprintFn", "batches", "conflicts"]


def conflicts(a: Footprint, b: Footprint) -> bool:
    """Must `a` and `b` never be in flight at the same time?

    Read/read never conflicts -- that is the entire concurrency win this
    loop has to offer, and it is also the only case this function is
    allowed to say no to. Every other combination -- a write touching what
    the other reads or writes, or either side being stateful -- says yes.
    """
    if a.stateful or b.stateful:
        return True
    return bool(a.writes & b.writes) or bool(a.writes & b.reads) or bool(a.reads & b.writes)


def batches(calls: Sequence[ToolCall], footprint_of: FootprintFn) -> list[list[ToolCall]]:
    """Group calls into ordered batches: run batch N fully before batch N+1.

    List scheduling, not a general solver: for each call, in the order the
    model issued them, its batch is one past the latest batch of anything it
    conflicts with among the calls before it. Two calls that conflict can
    never land in the same batch, by construction -- if they did, the second
    one's minimum batch would have been pushed past the first one's. Two
    calls that do not conflict, directly or through anything between them,
    end up in the same batch and run concurrently.

    O(n^2) in the number of calls in one turn. That number is bounded by how
    many tool calls a model can put in one response, which is small enough
    that the readable quadratic version is the right one to ship.
    """
    fps = [footprint_of(call) for call in calls]
    batch_index: list[int] = []
    for i, fp in enumerate(fps):
        earliest = 0
        for j in range(i):
            if conflicts(fp, fps[j]):
                earliest = max(earliest, batch_index[j] + 1)
        batch_index.append(earliest)

    result: list[list[ToolCall]] = []
    for call, index in zip(calls, batch_index, strict=True):
        while len(result) <= index:
            result.append([])
        result[index].append(call)
    return result
