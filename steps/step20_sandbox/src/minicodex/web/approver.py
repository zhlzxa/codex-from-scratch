"""The browser's `Approver`, and the table of what it is currently waiting on.

`minicodex.approval.Approver` is a Protocol with exactly one method:

    async def ask(self, request: ApprovalRequest) -> ApprovalReply: ...

The CLI's implementation blocks on `input()`. This one blocks on an
`asyncio.Future` that a later HTTP request resolves. Nothing in
`minicodex.approval`, `minicodex.policy` or `minicodex.agent` changed to make
that possible -- chapter 5 wrote the seam for a reason that had nothing to do
with browsers, and the tool call really is suspended mid-turn either way.

The subtlety is which thread resolves the future. `asyncio.Future` is not
thread-safe, and a FastAPI route declared `def` rather than `async def` runs in
an anyio worker thread -- so `future.set_result(...)` from a plain `def` route
is a data race against the event loop, on every reply. It does not usually
lose; it loses under load, once, and the turn hangs until the timeout. Two
defences, because one of them is a convention and conventions travel badly:
the route is `async def` (see `routes.py`), and `resolve` goes through
`loop.call_soon_threadsafe` regardless of who calls it.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Callable
from typing import Any

from minicodex.approval import ApprovalReply, ApprovalRequest

EventSink = Callable[[dict[str, Any]], None]

#: How long a pending approval waits before the turn gives up and treats it as
#: a decline. Long enough that a person who looks away does not lose the turn;
#: bounded, because a turn that can wait forever is a hang, and chapter 2 (
#: F02-01) already established that an unbounded wait is not a safe default
#: just because the thing being waited on is a person.
APPROVAL_TIMEOUT_SECONDS = 600.0


class ApprovalBroker:
    """Every approval this process is currently suspended on."""

    def __init__(self) -> None:
        self._pending: dict[str, asyncio.Future[ApprovalReply]] = {}
        self._loop: asyncio.AbstractEventLoop | None = None

    def register(self, approval_id: str) -> asyncio.Future[ApprovalReply]:
        # `get_running_loop`, not `get_event_loop`: the latter is deprecated
        # from 3.10 and, when there is no running loop, creates one -- so the
        # future would belong to a loop nothing ever runs.
        loop = asyncio.get_running_loop()
        self._loop = loop
        future: asyncio.Future[ApprovalReply] = loop.create_future()
        self._pending[approval_id] = future
        return future

    def pending(self) -> list[str]:
        return [key for key, fut in self._pending.items() if not fut.done()]

    def discard(self, approval_id: str) -> None:
        self._pending.pop(approval_id, None)

    def resolve(self, approval_id: str, reply: ApprovalReply) -> bool:
        future = self._pending.pop(approval_id, None)
        if future is None or future.done():
            return False
        loop = self._loop
        if loop is None or loop is _running_loop():
            future.set_result(reply)
        else:
            loop.call_soon_threadsafe(future.set_result, reply)
        return True

    def fail_all(self, reason: str) -> None:
        """Decline everything outstanding, e.g. when the turn is cancelled.

        A cancelled turn must not leave a future nobody will ever resolve: the
        `ask` coroutine is inside the tool call being cancelled, so the
        cancellation lands there anyway -- this is for the entries whose
        coroutine is already gone, so the table does not grow.
        """
        for approval_id in list(self._pending):
            future = self._pending.pop(approval_id, None)
            if future is not None and not future.done():
                future.set_exception(RuntimeError(reason))


def _running_loop() -> asyncio.AbstractEventLoop | None:
    try:
        return asyncio.get_running_loop()
    except RuntimeError:
        return None


class WebApprover:
    """Suspend the tool call, ask the browser, resume with what it said."""

    def __init__(self, broker: ApprovalBroker, emit: EventSink) -> None:
        self._broker = broker
        self._emit = emit

    async def ask(self, request: ApprovalRequest) -> ApprovalReply:
        approval_id = uuid.uuid4().hex[:12]
        future = self._broker.register(approval_id)
        self._emit(
            {
                "type": "approval_request",
                "id": approval_id,
                "what": request.what,
                "reason": request.reason,
                "risk": request.risk.name,
                "suggested_rule": (
                    list(request.suggested_rule) if request.suggested_rule is not None else None
                ),
            }
        )
        try:
            reply = await asyncio.wait_for(future, timeout=APPROVAL_TIMEOUT_SECONDS)
        except asyncio.TimeoutError:
            # See channel.py: the builtin spelling does not fire on 3.10.
            self._broker.discard(approval_id)
            self._emit({"type": "approval_timeout", "id": approval_id})
            return ApprovalReply(False, request.what)
        except asyncio.CancelledError:
            self._broker.discard(approval_id)
            raise
        self._emit(
            {
                "type": "approval_resolved",
                "id": approval_id,
                "approved": reply.approved,
                "remember": reply.remember,
            }
        )
        return reply
