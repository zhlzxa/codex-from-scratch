"""The gate: nothing reaches a route until this module says who is calling.

**Deny by default, and the default is the entire point.** The obvious way to
add authentication to a FastAPI app is a dependency -- `def get_thread(auth:
Auth = Depends(require_login))` -- and it works perfectly on every route that
has it. The failure mode is a route that does not: adding an endpoint and
forgetting the parameter produces an open endpoint, no error, no failing test,
and a code review that has to notice an *absence*. Twenty-two chapters of this
console have thirty-odd routes; the ones written after this chapter will be
written by somebody who has not read it.

So the gate is a middleware that refuses everything under `/api` and `/ws`,
and the exceptions are a list of paths in this file. Forgetting now fails
closed: a new route that needs to be public and is not on the list returns 401
on the first request, which is a bug that reports itself. `tests/
test_faults_ch23.py` walks `app.routes` and asserts that every one of them is
either named here or answers 401 to an anonymous caller, so the list cannot
quietly grow either.

**It is raw ASGI rather than `BaseHTTPMiddleware`, because of the socket.**
`BaseHTTPMiddleware` dispatches `scope["type"] == "http"` and passes anything
else straight through, so a console guarded that way authenticates every REST
call and leaves `/ws/threads/{id}` -- the socket that streams the model's
output, the shell commands it runs and their results -- open to anybody who
knows a thread id. That is not a hypothetical: `probe_ws_auth.py` in this step
runs both middlewares against the same app and prints what each one does.

**The socket also gets an `Origin` check, which the REST routes do not need.**
A cookie is enough to stop cross-site form posts once it is `SameSite=Lax`,
but the WebSocket handshake is not a fetch: it is exempt from CORS, so a page
on any origin can open `ws://your-console/ws/threads/...` and the browser will
attach your cookies. Cross-site WebSocket hijacking is the name; checking that
`Origin` matches `Host` is the defence, and it has to be here rather than in
the route because a route that has already accepted the socket has already
lost.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from fastapi import HTTPException, Request

from minicodex.memory import MINICODEX_HOME
from minicodex.tenancy import Owner

from .accounts import Account, AccountStore
from .sessions import SESSION_COOKIE, Session, SessionTable

#: Reachable without a session. Every one of these is either about *becoming*
#: authenticated or about telling a browser which of those two it should show.
#:
#: `/api/auth/status` is public and returns almost nothing on purpose: whether
#: this console has any account yet (so the frontend knows to render the
#: bootstrap form instead of the login form) and whether the caller is signed
#: in. It does not say which accounts exist.
PUBLIC_PATHS = frozenset(
    {
        "/api/auth/status",
        "/api/auth/login",
        "/api/auth/bootstrap",
    }
)

#: Guarded prefixes. Everything else -- `/`, `/assets/...`, the built frontend
#: -- is served without a session, because the login page has to come from
#: somewhere and a bundle of JavaScript is not a secret. What it can *do*
#: without a session is nothing: every call it makes lands under `/api`.
GUARDED_PREFIXES = ("/api", "/ws")


@dataclass(frozen=True)
class AuthContext:
    """Who this request is, resolved once, at the door."""

    account: Account
    session: Session
    #: The raw cookie, kept so `logout` can revoke exactly this session
    #: without the route having to read the header again.
    token: str
    #: Where this server keeps its tenants' home directories.
    home: Path = MINICODEX_HOME

    @property
    def owner(self) -> Owner:
        """The tenancy. The whole reason chapter 20 wrote this type."""
        return self.account.owner(self.home)


def resolve(
    scope_headers: list[tuple[bytes, bytes]],
    accounts: AccountStore,
    table: SessionTable,
    home: Path = MINICODEX_HOME,
) -> AuthContext | None:
    """Cookie -> session -> account, or `None` at the first step that fails.

    Returns `None` for a missing cookie, an unknown or expired session, a
    session whose account has been deleted, and a session whose account has
    been disabled -- one answer for all five, and the last two matter: a
    session table that outlives the account list would keep somebody signed in
    after they were removed, which is how "we revoked their access" turns into
    a still-working browser tab.
    """
    token = _cookie(scope_headers, SESSION_COOKIE)
    if not token:
        return None
    session = table.touch(token)
    if session is None:
        return None
    account = accounts.get(session.account_key)
    if account is None or account.disabled:
        table.revoke(token)
        return None
    return AuthContext(account=account, session=session, token=token, home=home)


def _cookie(headers: list[tuple[bytes, bytes]], name: str) -> str | None:
    for key, value in headers:
        if key.lower() != b"cookie":
            continue
        for part in value.decode("latin-1").split(";"):
            head, _, tail = part.strip().partition("=")
            if head == name:
                return tail
    return None


def _header(headers: list[tuple[bytes, bytes]], name: str) -> str | None:
    wanted = name.lower().encode("ascii")
    for key, value in headers:
        if key.lower() == wanted:
            return value.decode("latin-1")
    return None


def same_origin(headers: list[tuple[bytes, bytes]]) -> bool:
    """Is this handshake from a page the console itself served?

    No `Origin` at all is allowed through: that is a non-browser client (a
    script, a test, `websocat`), which is not the attack this check exists
    for. The attack needs a *browser* to attach the cookie, and a browser
    always sends `Origin` on a WebSocket handshake.
    """
    origin = _header(headers, "origin")
    if origin is None:
        return True
    host = _header(headers, "host")
    if not host:
        return False
    return urlsplit(origin).netloc.lower() == host.lower()


class AuthMiddleware:
    """Raw ASGI, so `http` and `websocket` go through the same decision."""

    def __init__(self, app: Any, *, console_of: Any) -> None:
        self.app = app
        # A callable rather than the console itself: the middleware is built
        # while the app is being assembled, and `app.state.console` is set in
        # the same function. Reaching for it per request also means a test that
        # swaps the console on a live app is not lying to the gate.
        self._console_of = console_of

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return

        path = scope.get("path", "")
        if not path.startswith(GUARDED_PREFIXES) or path in PUBLIC_PATHS:
            await self.app(scope, receive, send)
            return

        console = self._console_of()
        headers = scope.get("headers") or []

        if scope["type"] == "websocket" and not same_origin(headers):
            await _reject_socket(send, "cross-origin websocket handshake refused")
            return

        auth = resolve(headers, console.accounts, console.sessions, console.home)
        if auth is None:
            if scope["type"] == "websocket":
                await _reject_socket(send, "not signed in")
            else:
                await _reject_http(send)
            return

        # `scope["state"]` is what Starlette turns into `request.state`, and
        # writing it here is what makes `require(request)` below a lookup
        # rather than a second resolution. One authentication per request.
        scope.setdefault("state", {})["auth"] = auth
        await self.app(scope, receive, send)


async def _reject_http(send: Any) -> None:
    body = json.dumps({"detail": "not signed in"}).encode("utf-8")
    await send(
        {
            "type": "http.response.start",
            "status": 401,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode("ascii")),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})


async def _reject_socket(send: Any, reason: str) -> None:
    """Close without accepting.

    `websocket.close` before `websocket.accept` is an HTTP 403 on the
    handshake, which is what a browser's `WebSocket` constructor reports as an
    error rather than as a socket that opens and immediately closes. Accepting
    first and closing after would have let the client believe, for one round
    trip, that it was connected.
    """
    await send({"type": "websocket.close", "code": 1008, "reason": reason})


def require(request: Request) -> AuthContext:
    """The authenticated caller, or a 500 that means the gate did not run.

    Not a 401. If this raises, the request reached a guarded route without
    passing the middleware, which is a wiring bug in this program and not
    something the caller did wrong -- answering 401 would send a person off to
    log in again over a mistake no login can fix.
    """
    auth = getattr(request.state, "auth", None)
    if auth is None:  # pragma: no cover - the middleware makes this unreachable
        raise HTTPException(500, "this route is not behind the authentication gate")
    return auth


def set_session_cookie(response: Any, token: str, *, secure: bool) -> None:
    """`HttpOnly`, `SameSite=Lax`, and `Secure` only when it can be.

    `HttpOnly` keeps the token away from `document.cookie`, so an injected
    script on this origin cannot read it out and use it elsewhere. `Lax` is
    what makes a cross-site `POST /api/threads/{id}/messages` from an
    attacker's page arrive without a cookie, which is this console's CSRF
    defence -- the console has no token-in-a-form scheme and does not need one
    while every state-changing route is a POST, PATCH or DELETE.

    `Secure` is conditional, and that is a real trade rather than laziness: set
    unconditionally, the cookie is discarded by the browser on plain
    `http://localhost:8000`, which is how this console is normally run, and
    logging in would appear to succeed and then not work. Set from the
    request's own scheme, it is on wherever it means anything.
    """
    response.set_cookie(
        SESSION_COOKIE,
        token,
        httponly=True,
        samesite="lax",
        secure=secure,
        path="/",
    )


def clear_session_cookie(response: Any) -> None:
    response.delete_cookie(SESSION_COOKIE, path="/")


__all__ = [
    "GUARDED_PREFIXES",
    "PUBLIC_PATHS",
    "AuthContext",
    "AuthMiddleware",
    "clear_session_cookie",
    "require",
    "resolve",
    "same_origin",
    "set_session_cookie",
]
