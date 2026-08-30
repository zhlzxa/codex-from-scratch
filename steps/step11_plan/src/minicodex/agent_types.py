"""Types shared by the agent and the history.

Extracted from `agent.py` for one reason only: `history.py` needs `ToolCall`,
and `agent.py` needs `History`.  Leaving `ToolCall` where it was would make the
two modules import each other.

This is the smallest possible answer to a circular import -- move the shared
thing down, not sideways.  Interlude B deals with a case where the answer is
not this easy.

`ToolFn` arrived here later, by the same rule and before it caused any trouble.
`tools.py` needs to say what a handler is; `agent.py` already said it.  Having
`tools.py` import from `agent.py` would have worked -- there is no cycle today,
because `agent.py` never imports `tools.py` -- but it would point the arrow the
wrong way: `agent.py` is the loop on top, `tools.py` is machinery underneath,
and underneath is not allowed to depend on on-top.  A dependency that is merely
backwards is the one that becomes a cycle later, when somebody adds the
matching import from the other side and finds it already half-built.

`Footprint` and `ToolSet` arrived in interlude B, by the same rule for the
third time.  Three applications is where "we moved a type down" stops being an
incident and starts being the layering rule this package is checked against:
**a type two layers need lives below both of them, and a module that owns
behaviour does not also own the vocabulary its callers speak.**  `scheduler.py`
still owns everything you can *do* with a `Footprint`; it stopped owning the
word.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

# What every tool handler looks like once its context is bound: arguments in,
# text out, never raising.  `agent.py` enforces the "never raising" half.
ToolFn = Callable[[dict[str, Any]], Awaitable[str]]


@dataclass(frozen=True)
class ToolCall:
    """One tool call, in the form the agent can act on.

    `arguments` is the parsed object, or None if the model sent something that
    was not a JSON object.  `raw_arguments` is what it actually sent, kept so
    the error message can quote it back -- and, since chapter 1, so the history
    can be re-serialised without a lossy round trip.
    """

    call_id: str
    name: str
    arguments: dict[str, Any] | None
    raw_arguments: str


@dataclass(frozen=True)
class Footprint:
    """What one call touches, as far as the scheduler is willing to guess.

    `reads` and `writes` are resource keys -- for the tools this project has,
    a resource key is a resolved absolute path, so two calls naming the same
    file collide and two calls naming different files do not. The scheduler
    never looks inside a key; it only compares them for equality, which is
    what lets `tools.py` own the one question that actually needs tool
    knowledge (what does "the same resource" mean for *this* tool) while
    `scheduler.py` owns none of it.

    `stateful=True` means "conflicts with everything, including another
    stateful call" -- the answer for a tool whose effect cannot be named as a
    set of resources (`run_shell`: the working directory it carries across
    calls, the network, the rest of the filesystem) and the answer for a call
    the scheduler could not classify at all. Unclassifiable defaults to
    stateful, not to "no footprint" -- an empty `Footprint()` would claim the
    call touches nothing, which is the one claim that is never safe to guess.
    """

    reads: frozenset[str] = frozenset()
    writes: frozenset[str] = frozenset()
    stateful: bool = False


# The one Footprint value that means "do not run this next to anything else."
# A named constant rather than `Footprint(stateful=True)` written out at every
# call site, so grepping for STATEFUL finds every place that gives up on
# classifying a call.
STATEFUL = Footprint(stateful=True)

FootprintFn = Callable[[ToolCall], Footprint]


def _stateful(_call: ToolCall) -> Footprint:
    return STATEFUL


@dataclass(frozen=True)
class ToolSet:
    """One agent's tools: the handlers, the schemas, and what each one touches.

    Three things that have to agree, kept in one value so that agreeing is not
    something anybody has to remember.  Chapter 4 already paid for two of them
    drifting -- a handler with no schema is never called, a schema with no
    handler produces chapter 0's "no tool named X" error, which was written for
    names the model *invented* -- and `ToolSpec` fixed that pair for this
    project's own tools.  The third channel, the scheduler's `footprint_of`,
    was never folded in: it travelled separately, was assembled separately in
    `__main__`, and was simply **not passed at all** at the second assembly
    site.  A sub-agent therefore ran every tool call serially, which is safe
    only because chapter 8's default for an unrecognised call is `STATEFUL`.

    `__post_init__` makes disagreement a `ValueError` at build time rather than
    a wrong answer at run time.  It is the one rule in this class and it is the
    reason the class exists; without it this is a tuple with names.

    Not frozen all the way down: `schemas` is deliberately a live `list`,
    because chapter 9's registry hands the model client that exact object and
    reveals a deferred tool by appending to it.  Copying it here would make
    `tool_search` stop working, silently, on the next turn, so the registry's
    set is built last and keeps the list it owns (`composition.with_remote_tools`).
    """

    handlers: dict[str, ToolFn]
    schemas: list[dict[str, Any]]
    footprint_of: FootprintFn = _stateful
    # Names this set is responsible for even though no schema declares them.
    # Empty for every set except the one chapter 9's registry produces, where
    # a deferred tool is callable before its schema has been revealed -- said
    # out loud as an exception rather than by weakening the rule for everyone.
    callable_without_schema: frozenset[str] = field(default_factory=frozenset)

    def __post_init__(self) -> None:
        declared = {s["function"]["name"] for s in self.schemas}
        handled = set(self.handlers) - self.callable_without_schema
        if declared - handled:
            raise ValueError(f"schema with no handler: {sorted(declared - handled)}")
        if handled - declared:
            raise ValueError(f"handler with no schema: {sorted(handled - declared)}")

    def plus(self, other: ToolSet) -> ToolSet:
        """Add another set, routing each call's footprint back to its owner.

        A name declared twice is refused rather than resolved.  Chapter 9 made
        that call for two remote tools whose names sanitise to one thing; the
        same answer applies when the collision is between a local tool and a
        remote one, and for the same reason -- silently keeping one of them is
        how five declared tools become four (F09-01).

        The new schema list is a new object.  Chapter 9's registry hands the
        model client a list it later appends to, so the one set that must keep
        its list identity is the registry's, and it keeps it by being the last
        word rather than by being merged: `McpRegistry(local=...)` takes these
        schemas as input and owns the list that comes out.
        """
        clash = sorted(set(self.handlers) & set(other.handlers))
        if clash:
            raise ValueError(f"two tool sets both declare {clash}")
        mine, theirs = frozenset(self.handlers), frozenset(other.handlers)

        def footprint_of(call: ToolCall) -> Footprint:
            if call.name in mine:
                return self.footprint_of(call)
            if call.name in theirs:
                return other.footprint_of(call)
            return STATEFUL

        return ToolSet(
            handlers={**self.handlers, **other.handlers},
            schemas=[*self.schemas, *other.schemas],
            footprint_of=footprint_of,
            callable_without_schema=self.callable_without_schema | other.callable_without_schema,
        )
