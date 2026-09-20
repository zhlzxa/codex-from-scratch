"""Turn what the agent already writes down into what the browser reads.

`History` calls an observer on every accepted item -- that hook is what makes
`RolloutWriter` work at all. Subclassing the writer rather than adding a hook
to the agent loop buys the console a property it could not get any other way:
**an event reaches the browser if and only if it was also fsynced to disk.**
The live view and the resumable transcript cannot disagree, because there is
one call site and it does both.

That is also why the console streams per history item rather than per token.
Token streaming is a second, parallel path out of the model client with its
own ordering and its own failure modes, and it has nothing to write to disk.

`mark` is forwarded too. The marks the agent writes -- `compacted`,
`budget_exhausted`, `interrupted` -- are the only signal that the conversation
the user is reading is no longer the conversation the model is being sent;
dropping one means a run can compact away half its history with nothing on
screen to say so.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from minicodex.rollout import RolloutWriter, SessionMeta, dump_item

EventSink = Callable[[dict[str, Any]], None]


class StreamingRolloutWriter(RolloutWriter):
    """A `RolloutWriter` that also says, out loud, what it just wrote."""

    def __init__(self, path: Path, meta: SessionMeta, emit: EventSink) -> None:
        super().__init__(path, meta)
        self._emit = emit
        #: Called after each item is durable, to let the run report state that
        #: is not itself a history item -- the plan, and the sandbox mode.
        #: Assigned by `runtime.run_turn`; see `_derived` there.
        self.after_item: Callable[[], None] | None = None

    def append(self, item: Any) -> None:  # type: ignore[override]
        super().append(item)
        self._emit({"type": "history_item", "item": dump_item(item)})
        if self.after_item is not None:
            self.after_item()

    def mark(self, kind: str, **payload: Any) -> None:  # type: ignore[override]
        super().mark(kind, **payload)
        self._emit({"type": "mark", "mark": kind, "payload": payload})
