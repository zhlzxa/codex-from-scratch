"""Turn what the agent already writes down into what the browser reads.

`History` calls an observer on every accepted item -- that hook is what makes
`RolloutWriter` work at all (chapter 7). Subclassing the writer rather than
adding a hook to the agent loop buys the console a property it could not get
any other way: **an event reaches the browser if and only if it was also
fsynced to disk.** The live view and the resumable transcript cannot disagree,
because there is one call site and it does both.

That is also why the console streams per history item rather than per token.
Token streaming is a second, parallel path out of the model client with its own
ordering and its own failure modes, and it has nothing to write to disk;
`probe_web.py stream` measures what the reader pays for that choice.

`mark` is forwarded too, and it is the reason this class is not just three
lines. The three marks the agent writes -- `compacted`, `budget_exhausted`,
`interrupted` -- are the only signal that the conversation the user is reading
is no longer the conversation the model is being sent. The first version of
this console emitted them and the frontend dropped every one, so a run could
compact away half its history with nothing on screen to say so.

Every `history_item` carries its `index`: where it sits in the history this
file will load back as. That number arrived when chapter 19 was rewritten, and
it exists because the writer is told about items the browser already has. A
resumed turn starts by replaying the whole conversation into the new file
(`agent._attach`), and a compaction writes its new baseline the same way --
both through `append`, both indistinguishable here from a new item. The first
version streamed all of it, so every follow-up message drew the previous turns
on screen a second time. The index is what lets the browser tell "the 7th item
of this conversation, which I already show" from "the 8th, which is new",
without the agent having to learn that a browser exists.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from minicodex.rollout import RolloutWriter, SessionMeta, _dump_item

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
        self._emit({"type": "history_item", "index": index, "item": _dump_item(item)})
        if self.after_item is not None:
            self.after_item()

    def mark(self, kind: str, **payload: Any) -> None:  # type: ignore[override]
        super().mark(kind, **payload)
        if kind == "compacting":
            self._position = 0
        self._emit({"type": "mark", "mark": kind, "payload": payload, "position": self._position})
