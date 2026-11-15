"""One ordered stream of events per thread, and the sockets watching it.

The first version of this was three lines inside a route: for every event, for
every socket, `asyncio.create_task(ws.send_text(payload))`. It is worth saying
exactly what is wrong with that, because nothing about it ever raises.

**Order is not preserved.** Each `create_task` schedules an independent
coroutine, and the loop is free to run them in any order once any of them
awaits -- which `send_text` does, on the first byte. So `history_item` can
reach the browser *after* the `turn_complete` that follows it, and the console
renders in arrival order. There is no exception, no dropped event and no log
line: the transcript is simply, occasionally, in the wrong order.

**A task nobody holds can be collected.** `asyncio.create_task` returns a task
the caller is expected to keep; the event loop holds only a weak reference, so
CPython is entitled to collect a running task mid-await. The documented fix is
to keep a strong reference until it is done, which is what `_pump_tasks` is.

**The subscriber table grows forever.** Discarding a socket from a set leaves
the empty set behind, one per thread ever opened, for the life of the process.

So: one `asyncio.Queue` per thread, one pump task draining it, and each send
awaited in turn. Ordering then comes from the queue rather than from luck, the
pump is the strong reference, and the queue and its set are dropped together
when the last socket goes.

`emit` stays synchronous on purpose. It is called from inside the agent -- from
`History`'s observer, three frames below `agent.run` -- and the thing it is
called from cannot await. `put_nowait` on an unbounded queue is the only shape
that fits there without the core learning what a WebSocket is.

**A pump belongs to its own queue and its own set of sockets**, which arrived
when chapter 19 was rewritten. The first version looked both up by thread id
every time. When the last socket left, it queued the stop sentinel and left the
queue in the table until the pump got round to reading it -- and a socket that
subscribed in between (a reload, a second tab, a pump still busy sending a
backlog) found that queue, was told a pump existed, and joined a pump that was
about to stop. From then on every event for that thread was dropped at `emit`,
with a subscriber registered and nothing logged: the "page stopped updating"
failure this module exists to prevent, rebuilt one layer down. Now the last
socket out detaches the queue and the set at once, so a newcomer always starts
a fresh pump.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from typing import Any, Protocol

log = logging.getLogger(__name__)


def turn_key(account_key: str, thread_id: str) -> str:
    """The key one running turn is filed under: this channel, `busy`, `running`.

    Added with multi-account tenancy, and the prefix is not about id collisions -- a thread
    id is a `uuid4().hex[:12]`. It is that `busy` and `running` are the tables
    the quota counts, and "how many turns does alice have in flight" has to be
    answerable without a second table mapping threads back to accounts.
    """
    return f"{account_key}/{thread_id}"


class Socket(Protocol):
    """What this module needs from a `fastapi.WebSocket`.

    A Protocol rather than the real class so the ordering test can drive the
    pump with a list-appending fake and assert on the list. The test that
    catches an ordering bug must not itself depend on a network.
    """

    async def send_text(self, data: str) -> None: ...


class Channel:
    """Fan one thread's events out to its sockets, in the order they happened."""

    def __init__(self) -> None:
        self._queues: dict[str, asyncio.Queue[str | None]] = {}
        self._sockets: dict[str, set[Socket]] = {}
        self._pumps: dict[str, asyncio.Task[None]] = {}

    # -- subscription --------------------------------------------------------

    def subscribe(self, thread_id: str, socket: Socket) -> None:
        self._sockets.setdefault(thread_id, set()).add(socket)
        self._ensure_pump(thread_id)

    def unsubscribe(self, thread_id: str, socket: Socket) -> None:
        sockets = self._sockets.get(thread_id)
        if sockets is None:
            return
        sockets.discard(socket)
        if sockets:
            return
        # Last one out: detach the set and the queue *now*, so the next
        # subscriber starts its own pump instead of joining this one on its
        # way out. A sentinel rather than `task.cancel()`, so the pump finishes
        # what it was doing before it stops.
        self._sockets.pop(thread_id, None)
        queue = self._queues.pop(thread_id, None)
        if queue is not None:
            queue.put_nowait(None)

    def subscriber_count(self, thread_id: str) -> int:
        return len(self._sockets.get(thread_id, ()))

    # -- emission ------------------------------------------------------------

    def emit(self, thread_id: str, event: dict[str, Any]) -> None:
        """Queue one event. Safe to call from anywhere inside the run."""
        queue = self._queues.get(thread_id)
        if queue is None:
            # Nobody is watching this thread. The turn still runs, and the
            # browser reconstructs what it missed from the rollout file when it
            # reconnects (`GET /api/threads/{id}`), which is the same source
            # the events were derived from. Dropping here rather than buffering
            # forever is what stops a closed tab from being a memory leak.
            return
        queue.put_nowait(json.dumps(event, ensure_ascii=False, default=str))

    def emitter(self, thread_id: str) -> Any:
        """A one-argument `emit` bound to a thread, for handing down into a run."""

        def send(event: dict[str, Any]) -> None:
            self.emit(thread_id, event)

        return send

    # -- the pump ------------------------------------------------------------

    def _ensure_pump(self, thread_id: str) -> None:
        if thread_id in self._queues:
            return
        queue: asyncio.Queue[str | None] = asyncio.Queue()
        self._queues[thread_id] = queue
        sockets = self._sockets.setdefault(thread_id, set())
        task = asyncio.get_running_loop().create_task(self._pump(thread_id, queue, sockets))
        self._pumps[thread_id] = task  # the strong reference; see the docstring
        task.add_done_callback(lambda done, tid=thread_id: self._forget_pump(tid, done))

    def _forget_pump(self, thread_id: str, task: asyncio.Task[None]) -> None:
        # Only if it is still the current one: a newer pump may already have
        # taken this thread's slot.
        if self._pumps.get(thread_id) is task:
            del self._pumps[thread_id]

    async def _pump(
        self, thread_id: str, queue: asyncio.Queue[str | None], sockets: set[Socket]
    ) -> None:
        try:
            while True:
                payload = await queue.get()
                if payload is None:
                    return
                # Awaited one socket at a time. Slower than a fan-out, and that
                # is the trade being made: a console driven by one person does
                # not have enough sockets for the difference to be measurable,
                # and out-of-order transcripts are not a thing a person can
                # debug from the browser.
                for socket in list(sockets):
                    try:
                        await socket.send_text(payload)
                    except Exception:
                        sockets.discard(socket)
        except asyncio.CancelledError:
            raise
        except Exception:
            # A pump that dies takes the thread's whole event stream with it,
            # silently, while the turn keeps running. Logged rather than
            # swallowed, because "the browser stopped updating" is otherwise
            # indistinguishable from "the model is thinking".
            log.exception("event pump for thread %s stopped", thread_id)
        finally:
            if self._queues.get(thread_id) is queue:
                del self._queues[thread_id]

    async def aclose(self) -> None:
        """Stop every pump. Called from the app's shutdown hook."""
        for queue in list(self._queues.values()):
            queue.put_nowait(None)
        for task in list(self._pumps.values()):
            # `asyncio.TimeoutError` and not the builtin: on 3.10, which
            # `requires-python` promises, they are unrelated classes and the
            # builtin never fires on 3.10 -- writing it correctly the first
            # time here is free.
            with contextlib.suppress(asyncio.CancelledError, asyncio.TimeoutError):
                await asyncio.wait_for(asyncio.shield(task), 2.0)
