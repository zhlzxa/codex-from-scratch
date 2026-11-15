"""The browser's `Approver`, and the table of what it is currently waiting on.

`minicodex.approval.Approver` is a Protocol with exactly one method:

    async def ask(self, request: ApprovalRequest) -> ApprovalReply: ...

The CLI's implementation blocks on `input()`. This one blocks on an
`asyncio.Future` that a later HTTP request resolves. Nothing in
`minicodex.approval`, `minicodex.policy` or `minicodex.agent` changed to make
that possible -- the tool call really is suspended mid-turn either way.

The subtlety is which thread resolves the future. `asyncio.Future` is not
thread-safe, and a FastAPI route declared `def` rather than `async def` runs in
an anyio worker thread -- so `future.set_result(...)` from a plain `def` route
is a data race against the event loop, on every reply. It does not usually
lose; it loses under load, once, and the turn hangs until the timeout. Two
defences, because one of them is a convention and conventions travel badly:
the route is `async def` (see `routes.py`), and `resolve` goes through
`loop.call_soon_threadsafe` regardless of who calls it.

The broker also keeps *what* it is waiting on, per thread, and that arrived
when chapter 19 was rewritten. The request reaches the browser as one event;
an event sent while no tab was watching that thread is dropped (channel.py),
and a tab that reconnects rebuilds itself from `GET /api/threads/{id}`. The
first version's reply to that request had no approvals in it, so a reload, a
dropped socket or switching to another thread and back made the card vanish
while the turn stayed suspended on it -- for `APPROVAL_TIMEOUT_SECONDS`, after
which it counted as a refusal nobody had made.
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
#: bounded, because a turn that can wait forever is a hang, and an unbounded
#: wait is not a safe default just because the thing being waited on is a
#: person.
APPROVAL_TIMEOUT_SECONDS = 600.0


class ApprovalBroker:
    """Every approval this process is currently suspended on."""

    def __init__(self) -> None:
        self._pending: dict[str, asyncio.Future[ApprovalReply]] = {}
        #: The `approval_request` event for each pending id, and which thread asked.
        self._asked: dict[str, tuple[str | None, dict[str, Any]]] = {}
        self._loop: asyncio.AbstractEventLoop | None = None

    def register(
        self, approval_id: str, *, thread: str | None = None, event: dict[str, Any] | None = None
    ) -> asyncio.Future[ApprovalReply]:
        # `get_running_loop`, not `get_event_loop`: the latter is deprecated
        # from 3.10 and, when there is no running loop, creates one -- so the
        # future would belong to a loop nothing ever runs.
        loop = asyncio.get_running_loop()
        self._loop = loop
        future: asyncio.Future[ApprovalReply] = loop.create_future()
        self._pending[approval_id] = future
        if event is not None:
            self._asked[approval_id] = (thread, event)
        return future

    def pending(self) -> list[str]:
        return [key for key, fut in self._pending.items() if not fut.done()]

    def asked(self, thread: str) -> list[dict[str, Any]]:
        """The requests `thread` is still waiting on an answer for."""
        return [
            event
            for approval_id, (who, event) in self._asked.items()
            if who == thread
            and approval_id in self._pending
            and not self._pending[approval_id].done()
        ]

    def discard(self, approval_id: str) -> None:
        self._pending.pop(approval_id, None)
        self._asked.pop(approval_id, None)

    def resolve(self, approval_id: str, reply: ApprovalReply) -> bool:
        self._asked.pop(approval_id, None)
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
        self._asked.clear()
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
    """Suspend the tool call, ask the browser, resume with what it said.

    Every resolved request also lands in the audit log, approved or not: an
    approval is the human moment of this whole system, and "who was asked,
    and what did they say" is exactly the question an audit trail exists to
    answer.  The log is passed in as an optional collaborator rather than
    reached for globally, so the CLI-shaped tests that build a `WebApprover`
    with just a broker keep working -- and a *server* that forgets to pass it
    fails the audit test instead of failing open.
    """

    def __init__(
        self,
        broker: ApprovalBroker,
        emit: EventSink,
        audit: Any = None,
        actor: str = "-",
        thread: str | None = None,
    ) -> None:
        self._broker = broker
        self._emit = emit
        self._audit = audit
        self._actor = actor
        #: Which thread is asking, so a reconnecting tab can be told (see the
        #: module docstring). `None` for a broker nobody will ever query.
        self._thread = thread

    async def ask(self, request: ApprovalRequest) -> ApprovalReply:
        approval_id = uuid.uuid4().hex[:12]
        event = {
            "type": "approval_request",
            "id": approval_id,
            "what": request.what,
            "reason": request.reason,
            "risk": request.risk.name,
            "suggested_rule": (
                list(request.suggested_rule) if request.suggested_rule is not None else None
            ),
        }
        future = self._broker.register(approval_id, thread=self._thread, event=event)
        self._emit(event)
        try:
            reply = await asyncio.wait_for(future, timeout=APPROVAL_TIMEOUT_SECONDS)
        except asyncio.TimeoutError:
            # `asyncio.TimeoutError`, not the builtin: the two are unrelated
            # classes on 3.10, so the builtin spelling never fires there.
            self._broker.discard(approval_id)
            self._emit({"type": "approval_timeout", "id": approval_id})
            _audit_permission(self._audit, self._actor, request.what, "timeout")
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
        _audit_permission(
            self._audit,
            self._actor,
            request.what,
            "approved" if reply.approved else "declined",
        )
        return reply


def _audit_permission(audit: Any, actor: str, what: str, outcome: str) -> None:
    """Write one `permission` record, tolerating an absent log.

    The two callers inside `ask` that land here after a *decline* are audit
    records about something that did not happen -- losing one is a gap in the
    story, not a hole in a chain of custody, and an exception thrown from a
    decline path would turn a refusal the user already made into a turn error
    they did not ask for.  The approved path is audited with the same helper;
    its failures still propagate, because an approved action that cannot be
    recorded must not silently proceed (the invariant lives in `audit.py`).
    """
    if audit is None or outcome != "approved":
        return
    audit.append("permission", actor, what=what, outcome=outcome)
