"""Properties that must hold for *every* history, not the four in the examples.

Compaction is the first thing in this project where the interesting input is a
shape rather than a value.  The example tests all use histories this file
wrote, which means they all use histories whose author already knew where the
boundaries were.  A generator does not know that, which is the point.

No `hypothesis`: a seeded generator is enough here and costs no dependency.
The trade was real -- for eight chapters a failure printed a seed and nothing
else, and reproducing it meant running `MINICODEX_PROPERTY_SEED=<n>` and
reading a fourteen-item history by eye.  Chapter 14 wrote the shrinker
(`property_support.minimise`), and then shipped it connected to nothing: this
file went on printing seeds.  `shrunk()` below is the connection.

CI runs this file with `MINICODEX_PROPERTY_CASES=2000`; the default is small
enough that nobody is tempted to skip it locally.
"""

from __future__ import annotations

import asyncio
import os
import random
from collections.abc import Awaitable, Callable

import pytest

from minicodex.agent_types import ToolCall
from minicodex.compaction import (
    Protected,
    Sizer,
    SummaryRequest,
    boundaries,
    compact,
    plan,
    unused_call_ids,
)
from minicodex.history import History
from property_support import describe, minimise

CASES = int(os.environ.get("MINICODEX_PROPERTY_CASES", "200"))
BASE_SEED = int(os.environ.get("MINICODEX_PROPERTY_SEED", "0"))


def random_history(rng: random.Random) -> History:
    """A conversation of arbitrary shape that is nonetheless always valid.

    Built through `History`'s own API, so the generator cannot produce an
    illegal input even by accident -- which is what makes a failure downstream
    unambiguous: the compactor did it.
    """
    h = History()
    for _ in range(rng.randint(0, 2)):
        h.add_system_note("note " + "n" * rng.randint(0, 200))
    if rng.random() < 0.9:
        h.add_user("task " + "u" * rng.randint(0, 300))
    for _ in range(rng.randint(0, 14)):
        roll = rng.random()
        if roll < 0.12:
            h.add_assistant("thinking " + "t" * rng.randint(0, 200))
        elif roll < 0.2:
            h.add_user("follow-up " + "f" * rng.randint(0, 100))
        elif roll < 0.26:
            h.add_system_note("You have 2 turn(s) left.")
        else:
            # One assistant message may issue several calls at once; each one
            # must be answered before the next assistant message.
            calls = [
                ToolCall(f"c{rng.randrange(10**9)}", "run_shell", {}, '{"command": "ls"}')
                for _ in range(rng.randint(1, 3))
            ]
            h.add_assistant("", calls)
            for call in calls:
                h.add_tool_result(call.call_id, "o" * rng.randint(0, 3000))
    return h


async def stub_summary(request: SummaryRequest) -> str:
    return "## Goal\ng\n## Done\nd\n## Open\no\n"


def seeds() -> list[int]:
    return [BASE_SEED + i for i in range(CASES)]


async def shrunk(history: History, still_fails: Callable[[History], Awaitable[bool]]) -> str:
    """The smallest history that still fails, ready to go in a failure message.

    Only ever reached once a property has already failed, so its cost is paid
    by the run that needs it.  `minimise` is synchronous and the properties
    are not, and a test is already inside a running event loop -- hence the
    worker thread, which gets a loop of its own for each candidate.
    """

    def fails(candidate: History) -> bool:
        return asyncio.run(still_fails(candidate))

    smallest = await asyncio.to_thread(minimise, history, fails)
    return f"smallest history that still fails ({len(smallest.items)} item(s)):\n" + describe(
        smallest
    )


@pytest.mark.parametrize("seed", seeds())
def test_property_boundaries_are_exactly_the_renderable_prefixes(seed):
    """A cut is legal iff what remains has no orphaned result.

    Two independent definitions -- the counter in `boundaries()` and the
    set-difference in `unused_call_ids()` -- must agree on every index of every
    history.  Agreement is the evidence; either alone is just an assertion
    about itself.
    """
    items = random_history(random.Random(seed)).items
    legal = set(boundaries(items))
    for cut in range(len(items) + 1):
        orphans = unused_call_ids(items[cut:])
        assert (cut in legal) == (orphans == ()), (
            f"seed={seed} cut={cut} legal={cut in legal} orphans={orphans}"
        )


@pytest.mark.parametrize("seed", seeds())
async def test_property_compaction_always_yields_a_sendable_history(seed):
    rng = random.Random(seed)
    history = random_history(rng)
    budget = rng.choice([1, 50, 200, 600, 2000, 20000])
    result = await compact(history, summarise=stub_summary, budget=budget)
    # The invariant from chapter 1, restated as a property: whatever the shape
    # and whatever the budget, the thing that comes out can be sent.
    result.history.to_wire()
    result.history.to_wire("ollama_native")
    assert unused_call_ids(result.history.items) == ()


@pytest.mark.parametrize("seed", seeds())
async def test_property_the_protected_prefix_is_never_touched(seed):
    rng = random.Random(seed)
    history = random_history(rng)
    protected = Protected.of(history.items)
    before = history.items[: protected.count]
    result = await compact(history, summarise=stub_summary, budget=rng.choice([1, 100, 5000]))
    after = result.history.items[: protected.count]
    assert before == after, f"seed={seed}"


@pytest.mark.parametrize("seed", seeds())
async def test_property_compaction_never_grows_the_history(seed):
    """The summary note is one item; it must not cost more than it saved.

    Not trivially true -- a summary is inserted, so a history where nothing
    could be dropped would grow by one item and by the summary's tokens.  The
    empty-`dropped` branch exists for exactly that case, and this is what pins
    it.
    """
    rng = random.Random(seed)
    history = random_history(rng)
    sizer = Sizer()
    budget = rng.choice([1, 300, 3000])

    async def grows(candidate: History) -> bool:
        before = sizer.messages(candidate.to_wire())
        result = await compact(candidate, summarise=stub_summary, budget=budget)
        return sizer.messages(result.history.to_wire()) > before

    if await grows(history):
        # The seed is still printed -- it is how to get this exact run back --
        # but it is no longer the whole report.
        pytest.fail(
            f"seed={seed} budget={budget}: compaction made this history bigger\n"
            + await shrunk(history, grows)
        )


@pytest.mark.parametrize("seed", seeds())
def test_property_the_plan_only_ever_cuts_on_a_boundary(seed):
    rng = random.Random(seed)
    items = random_history(rng).items
    the_plan = plan(items, budget=rng.choice([1, 100, 1000, 10000]))
    assert the_plan.cut in boundaries(items), f"seed={seed}"
    assert the_plan.cut >= the_plan.protected
