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
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from typing import Any, Protocol

log = logging.getLogger(__name__)


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
        # Last one out: drop the set, the queue and the pump together. A
        # sentinel rather than `task.cancel()`, so anything already queued is
        # still delivered to whoever is left before the pump stops.
        self._sockets.pop(thread_id, None)
        queue = self._queues.get(thread_id)
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
        task = asyncio.get_running_loop().create_task(self._pump(thread_id, queue))
        self._pumps[thread_id] = task  # the strong reference; see the docstring
        task.add_done_callback(lambda _t, tid=thread_id: self._pumps.pop(tid, None))

    async def _pump(self, thread_id: str, queue: asyncio.Queue[str | None]) -> None:
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
                for socket in list(self._sockets.get(thread_id, ())):
                    try:
                        await socket.send_text(payload)
                    except Exception:
                        self._sockets.get(thread_id, set()).discard(socket)
        except asyncio.CancelledError:
            raise
        except Exception:
            # A pump that dies takes the thread's whole event stream with it,
            # silently, while the turn keeps running. Logged rather than
            # swallowed, because "the browser stopped updating" is otherwise
            # indistinguishable from "the model is thinking".
            log.exception("event pump for thread %s stopped", thread_id)
        finally:
            self._queues.pop(thread_id, None)

    async def aclose(self) -> None:
        """Stop every pump. Called from the app's shutdown hook."""
        for queue in list(self._queues.values()):
            queue.put_nowait(None)
        for task in list(self._pumps.values()):
            # `asyncio.TimeoutError` and not the builtin: on 3.10, which
            # `requires-python` promises, they are unrelated classes and the
            # builtin never fires. Chapter 15 paid for that one twice already
            # (F15-02) -- writing it correctly the first time here is free.
            with contextlib.suppress(asyncio.CancelledError, asyncio.TimeoutError):
                await asyncio.wait_for(asyncio.shield(task), 2.0)
