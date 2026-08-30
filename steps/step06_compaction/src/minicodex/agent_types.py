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
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
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
