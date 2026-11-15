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

Every `history_item` carries its `index`: where it sits in the history this
file will load back as. A resumed turn starts by replaying the whole
conversation into its new file (`agent._attach`), and a compaction writes its
new baseline the same way -- both through `append`, both indistinguishable
here from a new item. Streamed without an index, every follow-up message drew
the previous turns on screen a second time; with it, the browser can tell "an
item I already show" from "a new one" without the agent knowing a browser
exists. (Found when chapter 19 was rewritten.)
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
        #: How many items the history this file loads back as holds so far.
        #: `read_rollout` keeps what follows the last `compacting` marker, so
        #: that marker is where counting starts again.
        self._position = 0

    def append(self, item: Any) -> None:  # type: ignore[override]
        super().append(item)
        index, self._position = self._position, self._position + 1
        self._emit({"type": "history_item", "index": index, "item": dump_item(item)})
        if self.after_item is not None:
            self.after_item()

    def mark(self, kind: str, **payload: Any) -> None:  # type: ignore[override]
        super().mark(kind, **payload)
        if kind == "compacting":
            self._position = 0
        self._emit({"type": "mark", "mark": kind, "payload": payload, "position": self._position})
