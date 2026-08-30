"""Types shared by the agent and the history.

Extracted from `agent.py` for one reason only: `history.py` needs `ToolCall`,
and `agent.py` needs `History`.  Leaving `ToolCall` where it was would make the
two modules import each other.

This is the smallest possible answer to a circular import -- move the shared
thing down, not sideways.  Interlude B deals with a case where the answer is
not this easy.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


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
