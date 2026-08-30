"""Two hand-rolled HTTP servers for chapter 21's offline tests.

`asyncio.start_server` and nothing else -- `fastapi` is only the `web` extra
(chapter 19), and a core MCP test that cannot run without a web framework is a
core MCP test that will be skipped on the machine where it matters. Chapter
4's `stub.py` (the replay server) is this project's precedent for hand-rolling
one of these; this file is two more.

`HttpMcpStub` is a streamable-HTTP MCP server, deliberately stateless: it never
issues an `Mcp-Session-Id`. That is not a simplification this project is
hiding -- it is what keeps the tests inside this chapter's own scope. Measured
by reading `mcp.client.streamable_http.StreamableHTTPTransport.handle_get_stream`:
the client only opens a long-lived GET for server-initiated messages when
`self.session_id` is set (`if not self.session_id: return`), so a server that
never hands one out is never asked to hold that stream open, and the SDK's own
SSE-framing and session-id machinery -- exercised by chapter 9's tests already
-- never has to run against this stub at all. What this stub exists to test is
narrower: whether this project's own headers, bearer token and OAuth wiring
reach the wire, and what a server without a capability does to a client that
asks for it anyway.

`OAuthStub` is an authorization server and a protected resource in one
process, because a test does not care that a real deployment would run them on
different hosts. It is deliberately configurable in the one way authorization
servers really differ for this chapter's purposes: whether `/register`
(RFC 7591) exists at all -- that is F21-08b, the real and measured divergence
from codex, which has no dynamic-registration code whatsoever.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import time
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse


def _split_head(head: bytes) -> tuple[str, str, dict[str, str]]:
    lines = head.decode("latin-1").split("\r\n")
    parts = lines[0].split(" ")
    method, path = (parts[0], parts[1]) if len(parts) >= 2 else ("GET", "/")
    headers: dict[str, str] = {}
    for line in lines[1:]:
        if ":" in line:
            key, _, value = line.partition(":")
            headers[key.strip().lower()] = value.strip()
    return method, path, headers


async def _close_server(server: asyncio.AbstractServer | None) -> None:
    """Shut a stub server down without waiting on a connection its own test
    abandoned.

    Measured, not a defensive habit: a test that cancels an in-flight request
    (chapter 21's own tool-timeout test does this on purpose, via
    `asyncio.wait_for`) leaves a connection whose server-side handler task is
    still running -- cancelling the *client's* await never sends a TCP close,
    so the socket stays open from the OS's perspective. Plain
    `server.close()` only stops accepting new connections; `wait_closed()`
    after it waits for every connection's handler task to finish on its own,
    which for an abandoned one never happens, and the fixture's own teardown
    hangs -- not the code under test, the stub built to test it.

    `abort_clients()` (Python 3.13+) forcibly closes every open connection so
    that wait can complete. Older interpreters fall back to a bounded
    `wait_closed()`: still not indefinite, though a delayed handler on a
    pre-3.13 interpreter costs its own delay before this returns rather than
    nothing.
    """
    if server is None:
        return
    server.close()
    abort = getattr(server, "abort_clients", None)
    if callable(abort):
        abort()
        with contextlib.suppress(Exception):
            await server.wait_closed()
        return
    with contextlib.suppress(Exception):
        await asyncio.wait_for(server.wait_closed(), timeout=5.0)


async def _write_response(
    writer: asyncio.StreamWriter,
    status: int,
    headers: dict[str, str],
    body: bytes,
) -> None:
    reasons = {
        200: "OK",
        201: "Created",
        202: "Accepted",
        302: "Found",
        401: "Unauthorized",
        404: "Not Found",
        405: "Method Not Allowed",
    }
    head = [
        f"HTTP/1.1 {status} {reasons.get(status, 'OK')}",
        f"Content-Length: {len(body)}",
        "Connection: close",
    ]
    head += [f"{key}: {value}" for key, value in headers.items()]
    writer.write(("\r\n".join(head) + "\r\n\r\n").encode("latin-1") + body)
    with contextlib.suppress(Exception):
        await writer.drain()
    writer.close()


class HttpMcpStub:
    """One stateless streamable-HTTP MCP endpoint, on a loopback port the OS picks."""

    def __init__(
        self,
        *,
        tools: list[dict[str, Any]] | None = None,
        resources: list[dict[str, Any]] | None = None,
        declare_resources_capability: bool = True,
        require_auth: bool = False,
        challenge: str = "",
        delay: float = 0.0,
    ) -> None:
        #: seconds to sleep before replying to a `tools/call` -- what F21-06's
        #: tool-timeout test needs: a server that is reachable and eventually
        #: answers, just not inside the budget.
        self.delay = delay
        self.tools = (
            tools
            if tools is not None
            else [
                {
                    "name": "echo",
                    "description": "Echo the text back.",
                    "inputSchema": {"type": "object", "properties": {"text": {"type": "string"}}},
                }
            ]
        )
        self.resources = resources
        self.declare_resources_capability = declare_resources_capability
        self.require_auth = require_auth
        self.challenge = challenge

        self._server: asyncio.AbstractServer | None = None
        self.port = 0
        #: Every request's headers, in order.  What the client *sent* is the
        #: thing under test -- a stub that only reports what it answered
        #: cannot show a missing header.
        self.seen: list[dict[str, str]] = []
        self.authorized: list[str | None] = []
        #: every JSON-RPC method this stub was actually asked for -- what
        #: F21-12's "not asked" half asserts on, since `seen` only has headers.
        self.methods_called: list[str] = []

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}/mcp"

    async def __aenter__(self) -> HttpMcpStub:
        self._server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        self.port = self._server.sockets[0].getsockname()[1]
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await _close_server(self._server)

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            head = await reader.readuntil(b"\r\n\r\n")
        except (asyncio.IncompleteReadError, ConnectionError):
            writer.close()
            return
        method, path, headers = _split_head(head)
        self.seen.append(headers)
        self.authorized.append(headers.get("authorization"))

        if path.split("?")[0] != "/mcp":
            await _write_response(writer, 404, {}, b"")
            return

        if method != "POST":
            # The GET stream for server-initiated messages.  Never reached in
            # this stub's own tests (no session id is ever issued -- see the
            # module docstring) but answered honestly rather than hung, in
            # case a future test connects a real client mode that asks anyway.
            await _write_response(writer, 405, {}, b"")
            return

        length = int(headers.get("content-length", "0") or 0)
        body = await reader.readexactly(length) if length else b""
        try:
            message = json.loads(body) if body else {}
        except json.JSONDecodeError:
            message = {}
        if isinstance(message, dict) and isinstance(message.get("method"), str):
            self.methods_called.append(message["method"])

        if self.require_auth and not headers.get("authorization"):
            extra = f'Bearer resource_metadata="{self.challenge}"' if self.challenge else "Bearer"
            await _write_response(writer, 401, {"WWW-Authenticate": extra}, b"")
            return

        if self.delay and message.get("method") == "tools/call":
            await asyncio.sleep(self.delay)

        reply = self._reply_to(message)
        if reply is None:
            # A notification: 202 with no body is the legal answer.
            await _write_response(writer, 202, {}, b"")
            return
        await _write_response(
            writer, 200, {"Content-Type": "application/json"}, json.dumps(reply).encode("utf-8")
        )

    def _reply_to(self, message: dict[str, Any]) -> dict[str, Any] | None:
        method = message.get("method")
        request_id = message.get("id")
        if request_id is None:
            return None
        if method == "initialize":
            capabilities: dict[str, Any] = {"tools": {}}
            if self.declare_resources_capability:
                capabilities["resources"] = {}
            return {
                "jsonrpc": "2.0",
                "id": request_id,
                "result": {
                    "protocolVersion": "2025-06-18",
                    "capabilities": capabilities,
                    "serverInfo": {"name": "stub", "version": "0"},
                },
            }
        if method == "tools/list":
            return {"jsonrpc": "2.0", "id": request_id, "result": {"tools": self.tools}}
        if method == "resources/list":
            return {
                "jsonrpc": "2.0",
                "id": request_id,
                "result": {"resources": self.resources or []},
            }
        if method == "resources/read":
            uri = (message.get("params") or {}).get("uri", "")
            return {
                "jsonrpc": "2.0",
                "id": request_id,
                "result": {"contents": [{"uri": uri, "text": f"contents of {uri}"}]},
            }
        if method == "tools/call":
            params = message.get("params") or {}
            text = (params.get("arguments") or {}).get("text", "")
            return {
                "jsonrpc": "2.0",
                "id": request_id,
                "result": {"content": [{"type": "text", "text": f"echo: {text}"}]},
            }
        # Everything else, `server/discover` included: `mode='auto'` reads any
        # error here as "not a modern server" and falls back to `initialize`,
        # which this stub already answers -- so one branch covers the probe
        # without this stub needing to know the modern protocol at all.
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "error": {"code": -32601, "message": f"method not found: {method}"},
        }


class OAuthStub:
    """An authorization server and a protected resource, in one process.

    `supports_dcr` toggles the one thing F21-08b measures: with it `True`,
    `/register` behaves like a real authorization server implementing
    RFC 7591; with it `False`, `/register` answers 404, which is what a
    server with no dynamic-registration support looks like on the wire.
    """

    def __init__(self, *, supports_dcr: bool = True) -> None:
        self.supports_dcr = supports_dcr
        self._server: asyncio.AbstractServer | None = None
        self.port = 0
        self.register_calls = 0
        self.authorize_calls = 0
        self.token_calls = 0
        self.refresh_calls = 0
        self.issued = 0
        #: refresh tokens already spent -- a second use is how the loser of a
        #: refresh race gets logged out, on a real authorization server.
        self.spent: set[str] = set()
        self._codes: dict[str, dict[str, str]] = {}
        #: the most recently issued access token, for the `/protected` check.
        self._live_access_token: str | None = None
        #: every request this server saw, `(method, path)` -- what F21-08
        #: asserts on: the SDK's own two-hop fallback (a path-based well-known
        #: URI tried and refused, then the root one) is only visible from the
        #: sequence of *requests*, not from any one response.
        self.requests: list[tuple[str, str]] = []

    @property
    def base(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    @property
    def resource_url(self) -> str:
        return f"{self.base}/protected"

    @property
    def prm_url(self) -> str:
        return f"{self.base}/.well-known/oauth-protected-resource"

    async def __aenter__(self) -> OAuthStub:
        self._server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        self.port = self._server.sockets[0].getsockname()[1]
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await _close_server(self._server)

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            head = await reader.readuntil(b"\r\n\r\n")
        except (asyncio.IncompleteReadError, ConnectionError):
            writer.close()
            return
        method, raw_path, headers = _split_head(head)
        length = int(headers.get("content-length", "0") or 0)
        body = (await reader.readexactly(length)).decode("utf-8") if length else ""
        path = raw_path.split("?")[0]
        query = parse_qs(urlparse(raw_path).query)
        self.requests.append((method, path))

        if path == "/.well-known/oauth-protected-resource":
            await self._json(writer, 200, {"authorization_servers": [self.base]})
            return
        if path == "/.well-known/oauth-authorization-server":
            body_json: dict[str, Any] = {
                "issuer": self.base,
                "authorization_endpoint": f"{self.base}/authorize",
                "token_endpoint": f"{self.base}/token",
            }
            if self.supports_dcr:
                body_json["registration_endpoint"] = f"{self.base}/register"
            await self._json(writer, 200, body_json)
            return
        if path == "/register" and method == "POST":
            self.register_calls += 1
            if not self.supports_dcr:
                await _write_response(writer, 404, {}, b"")
                return
            client_id = f"dcr-client-{self.register_calls}"
            payload = json.loads(body) if body else {}
            await self._json(
                writer,
                201,
                {
                    "client_id": client_id,
                    "redirect_uris": payload.get("redirect_uris", []),
                    "token_endpoint_auth_method": "none",
                    "grant_types": payload.get(
                        "grant_types", ["authorization_code", "refresh_token"]
                    ),
                    "response_types": ["code"],
                },
            )
            return
        if path == "/authorize" and method == "GET":
            self.authorize_calls += 1
            params = {k: v[0] for k, v in query.items()}
            code = f"code-{self.authorize_calls}"
            self._codes[code] = {
                "code_challenge": params.get("code_challenge", ""),
                "client_id": params.get("client_id", ""),
            }
            redirect_uri = params.get("redirect_uri", "")
            state = params.get("state", "")
            location = f"{redirect_uri}?{urlencode({'code': code, 'state': state})}"
            await _write_response(writer, 302, {"Location": location}, b"")
            return
        if path == "/token" and method == "POST":
            self.token_calls += 1
            form = dict(part.split("=", 1) for part in body.split("&") if "=" in part)
            grant_type = form.get("grant_type")
            if grant_type == "authorization_code":
                self.issued += 1
                access = f"access-{self.issued}"
                refresh = f"refresh-{self.issued}"
                self._live_access_token = access
                await self._json(
                    writer,
                    200,
                    {
                        "access_token": access,
                        "refresh_token": refresh,
                        "expires_in": 3600,
                        "token_type": "Bearer",
                    },
                )
                return
            if grant_type == "refresh_token":
                self.refresh_calls += 1
                given = form.get("refresh_token", "")
                if given in self.spent:
                    await self._json(writer, 400, {"error": "invalid_grant"})
                    return
                self.spent.add(given)
                self.issued += 1
                access = f"access-{self.issued}"
                refresh = f"refresh-{self.issued}"
                self._live_access_token = access
                await self._json(
                    writer,
                    200,
                    {
                        "access_token": access,
                        "refresh_token": refresh,
                        "expires_in": 3600,
                        "token_type": "Bearer",
                    },
                )
                return
            await self._json(writer, 400, {"error": "unsupported_grant_type"})
            return
        if path == "/protected":
            token = (headers.get("authorization") or "").removeprefix("Bearer ").strip()
            if not token or token != self._live_access_token:
                # A bare `Bearer` challenge, deliberately: no `resource_metadata`
                # parameter, so the client has nothing to go on except its own
                # well-known-URI guesses (RFC 9728's path-based candidate,
                # then the root one) -- which is exactly what F21-08 tests. A
                # header naming the PRM URL directly would make this server
                # more helpful than most and would skip the fallback the SDK
                # actually has to fall back to.
                await _write_response(writer, 401, {"WWW-Authenticate": "Bearer"}, b"")
                return
            await self._json(writer, 200, {"ok": True, "at": time.time()})
            return
        await _write_response(writer, 404, {}, b"")

    async def _json(
        self, writer: asyncio.StreamWriter, status: int, payload: dict[str, Any]
    ) -> None:
        await _write_response(
            writer,
            status,
            {"Content-Type": "application/json"},
            json.dumps(payload).encode("utf-8"),
        )


__all__ = ["HttpMcpStub", "OAuthStub"]
