"""Whose credential this is, and where it goes when nobody is watching a browser.

The SDK's `OAuthClientProvider` (`mcp.client.auth.oauth2`, 786 lines) is
discovery, PKCE, token exchange, refresh and RFC 7591 dynamic registration, all
in one `httpx2.Auth` subclass. None of that is rewritten here. What it asks its
caller for is a `TokenStorage` -- four async methods, `get_tokens` /
`set_tokens` / `get_client_info` / `set_client_info` -- and two callbacks for
the one step a library cannot do unattended: showing a URL to a person and
waiting for what comes back.

That is the whole of this module. `TokenStorage` answers "whose disk", bound to
chapter 20's `Owner` the same way `Owner.mcp_tokens()` was already shaped to
expect (`tenancy.py`, added ahead of this chapter for exactly this). The two
handlers answer "how does a URL reach a person and a code come back", with a
real default for a terminal with a browser next to it, and every test in this
chapter driving an injected fake instead -- the same shape chapter 5 gave
`Session.approver`: a Protocol a human answers, replaced in tests by a scripted
double.

What used to be here and no longer is: a discovery function pair
(protected-resource metadata, then authorization-server metadata, two
well-known paths each), a PKCE generator, a token-exchange function, a refresh
function, and a lock around it. All five are `OAuthClientProvider`'s job now.
The two things that remain unowned by the SDK are the same two this project
would have had to write for itself either way: which files a credential sits
in, and how a person actually sees a URL.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import webbrowser
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from mcp.client.auth import OAuthClientProvider
from mcp.shared.auth import AuthorizationCodeResult, OAuthClientInformationFull, OAuthToken
from mcp.shared.auth_utils import calculate_token_expiry

from minicodex.tenancy import DEFAULT_OWNER, Owner

# codex's loopback callback listens on 1455, falling back to 1457
# (`login/src/server.rs:59-62`). This project picks one port rather than a
# fallback pair -- a second server to try is a second thing that can be
# wrong -- and the exact number matters only in that it must match what was
# registered with the authorization server, which is exactly why dynamic
# registration (RFC 7591) is convenient when it is available.
DEFAULT_CALLBACK_PORT = 1456

# How long the loopback server waits for the one request it exists to receive,
# before deciding nobody is coming back.  Long enough for a person to read a
# consent screen and click through it; short enough that a session which will
# never see that click does not hang until the tool timeout notices instead.
CALLBACK_TIMEOUT_SECONDS = 300.0


class OAuthStorageError(RuntimeError):
    """The on-disk credential file could not be read or written."""


class OwnerTokenStorage:
    """The SDK's `TokenStorage` protocol, keyed by `Owner` and by server name.

    One file per owner (`Owner.mcp_tokens()`), holding every server that owner
    has authenticated to -- not one file per server, because a leaked
    directory listing should not itself be the list of every remote tool a
    person uses. Inside it, `{server: {"tokens": ..., "client_info": ...}}`:
    two keys the SDK asks for separately, stored together because they are
    read and written together by the same flow and a partial write of one
    without the other is not a state this format needs to be able to express.

    File mode 0600 mirrors codex's `auth.json`
    (`codex-rs/login/src/auth/storage.rs:213`, `options.mode(0o600)`). On
    Windows the `os.chmod` call is a no-op and the file inherits directory
    ACLs -- said out loud, the same honesty chapter 20 already owes about
    bubblewrap being Linux-only, because a security property that silently
    does not hold on one platform is worse than one that was never claimed.
    """

    def __init__(self, owner: Owner = DEFAULT_OWNER, *, server: str) -> None:
        self.owner = owner
        self.server = server

    @property
    def path(self) -> Path:
        return self.owner.mcp_tokens()

    def _read_all(self) -> dict[str, Any]:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return raw if isinstance(raw, dict) else {}

    def _write_all(self, data: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
        with contextlib.suppress(OSError):
            os.chmod(self.path, 0o600)

    # -- the SDK's TokenStorage protocol --------------------------------------

    async def get_tokens(self) -> OAuthToken | None:
        raw = (self._read_all().get(self.server) or {}).get("tokens")
        if not isinstance(raw, dict):
            return None
        try:
            return OAuthToken.model_validate(raw)
        except ValueError:
            return None

    async def set_tokens(self, tokens: OAuthToken) -> None:
        data = self._read_all()
        entry = data.setdefault(self.server, {})
        entry["tokens"] = tokens.model_dump(mode="json", exclude_none=True)
        # An *absolute* expiry, alongside the token's own relative
        # `expires_in`.  Not part of the SDK's `TokenStorage` protocol, and
        # necessary anyway (F21-15): `OAuthToken.expires_in` is RFC 6749's
        # wire field, seconds remaining *at the moment the server answered*,
        # and it does not decay once written to disk.  Storing it verbatim
        # and nothing else is exactly the gap `preload_tokens` below exists
        # to close.
        entry["expires_at"] = calculate_token_expiry(tokens.expires_in)
        self._write_all(data)

    async def get_expiry(self) -> float | None:
        """The absolute expiry saved alongside the last `set_tokens` call.

        `None` for a token stored before this field existed, or one with no
        `expires_in` at all -- both mean "no known expiry", the same thing
        the SDK's own `OAuthContext.token_expiry_time` means by `None`.
        """
        raw = (self._read_all().get(self.server) or {}).get("expires_at")
        return float(raw) if isinstance(raw, int | float) else None

    async def get_client_info(self) -> OAuthClientInformationFull | None:
        raw = (self._read_all().get(self.server) or {}).get("client_info")
        if not isinstance(raw, dict):
            return None
        try:
            return OAuthClientInformationFull.model_validate(raw)
        except ValueError:
            return None

    async def set_client_info(self, client_info: OAuthClientInformationFull) -> None:
        data = self._read_all()
        entry = data.setdefault(self.server, {})
        entry["client_info"] = client_info.model_dump(mode="json", exclude_none=True)
        self._write_all(data)

    # -- the one thing the protocol does not offer ----------------------------

    async def forget(self) -> bool:
        """Remove this server's entry.  Returns whether there was one."""
        data = self._read_all()
        if self.server not in data:
            return False
        del data[self.server]
        self._write_all(data)
        return True

    async def seed_client_id(self, client_id: str, *, redirect_uri: str) -> None:
        """Pre-register a client id, codex-style, so dynamic registration never runs.

        `OAuthClientProvider` only attempts RFC 7591 when `get_client_info()`
        comes back empty (`oauth2.py`, step 4: `if not self.context.client_info`).
        Writing one first is the whole of what it takes to skip that step --
        no flag, no branch in the SDK to disable, just the same file it would
        have written itself, arriving early.
        """
        existing = await self.get_client_info()
        if existing is not None and existing.client_id == client_id:
            return
        await self.set_client_info(
            OAuthClientInformationFull(client_id=client_id, redirect_uris=[redirect_uri])  # type: ignore[list-item]
        )


async def default_redirect_handler(url: str) -> None:
    """Show a person the authorization URL: print it, and try to open it.

    The terminal is always the fallback -- an SSH session, a container, a CI
    runner all have no browser to open, and printing costs nothing on the
    machines that do. `webbrowser.open` failures are swallowed rather than
    raised: a browser that could not be launched is not this flow's failure,
    it is the URL sitting on the screen waiting to be clicked or copied.
    """
    print(f"[mcp oauth] open this URL to authorize: {url}")
    with contextlib.suppress(Exception):
        webbrowser.open(url)


def make_loopback_callback(
    port: int = DEFAULT_CALLBACK_PORT, *, timeout: float = CALLBACK_TIMEOUT_SECONDS
) -> Callable[[], Awaitable[AuthorizationCodeResult]]:
    """A `callback_handler` that waits on `http://127.0.0.1:<port>/callback`.

    One request, then the server closes -- codex's loopback callback
    (`login/src/server.rs`) is the same shape for the same reason: the
    authorization server was told this exact URL in the authorize request, and
    it is only ever going to be hit once, by the browser that just finished a
    consent screen.

    `asyncio.start_server` and nothing else, matching this project's `stub.py`
    precedent for hand-rolling a server rather than reaching for a web
    framework the core program does not otherwise need.
    """

    async def callback() -> AuthorizationCodeResult:
        loop = asyncio.get_running_loop()
        result: asyncio.Future[AuthorizationCodeResult] = loop.create_future()

        async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            try:
                request_line = (await reader.readline()).decode("latin-1", errors="replace")
                path = request_line.split(" ")[1] if request_line.count(" ") >= 2 else "/"
                while True:
                    header_line = await reader.readline()
                    if header_line in (b"\r\n", b""):
                        break
                params = parse_qs(urlparse(path).query)
                body = b"<html><body>Authorization received. You may close this tab.</body></html>"
                writer.write(
                    b"HTTP/1.1 200 OK\r\nContent-Type: text/html\r\nContent-Length: "
                    + str(len(body)).encode("ascii")
                    + b"\r\nConnection: close\r\n\r\n"
                    + body
                )
                with contextlib.suppress(Exception):
                    await writer.drain()
                if not result.done():
                    result.set_result(
                        AuthorizationCodeResult(
                            code=(params.get("code") or [""])[0],
                            state=(params.get("state") or [None])[0],
                            iss=(params.get("iss") or [None])[0],
                        )
                    )
            finally:
                writer.close()

        server = await asyncio.start_server(handle, "127.0.0.1", port)
        try:
            async with server:
                return await asyncio.wait_for(result, timeout)
        finally:
            server.close()
            with contextlib.suppress(OSError):
                await server.wait_closed()

    return callback


async def preload_tokens(provider: OAuthClientProvider, storage: OwnerTokenStorage) -> None:
    """Restore a provider's in-memory state from storage eagerly, instead of
    letting the SDK do it lazily on the first request.

    This exists because of a real, measured gap (F21-15) rather than as a
    defensive habit. `OAuthClientProvider._initialize()` -- called once, on
    the first request through a freshly constructed provider -- loads
    `current_tokens` and `client_info` from `TokenStorage` but never touches
    `token_expiry_time`, which stays `None`. `is_token_valid()` treats
    `token_expiry_time is None` as "no known expiry, so valid":

        return bool(
            self.current_tokens and self.current_tokens.access_token
            and (not self.token_expiry_time or time.time() <= self.token_expiry_time)
        )

    So a token reloaded after a restart -- the ordinary case for a CLI that
    starts a new process per invocation -- looks valid no matter how long ago
    it actually expired, right up until the resource server answers with a
    401. And a 401 does **not** fall back to `_refresh_token()`: read
    `async_auth_flow`, and the `if response.status_code == 401:` branch goes
    straight to protected-resource discovery and a fresh interactive
    authorization, unconditionally. Measured directly (`probe_mcp_remote.py
    oauth-401`): a provider seeded with an expired token, a live
    `refresh_token` and a stored `client_info` still called `redirect_handler`
    with a fresh `/authorize` URL, and never called the token endpoint with
    `grant_type=refresh_token` at all.

    Reconstructing `token_expiry_time` here, from the absolute timestamp
    `OwnerTokenStorage.set_tokens` persists precisely so it can be
    reconstructed, is what moves that token back onto the *proactive* path --
    `is_token_valid()` correctly returns `False`, `can_refresh_token()` is
    `True`, and the request is refreshed silently before it is ever sent,
    with no interactive step and no `redirect_handler` call at all.

    Setting `provider._initialized = True` afterward is not a workaround for
    a name with a leading underscore; it is the one flag that tells
    `async_auth_flow` its own lazy `_initialize()` has already happened, so
    it does not immediately overwrite `token_expiry_time` back to `None` by
    reloading `current_tokens`/`client_info` a second time and discarding
    what this function just set.
    """
    provider.context.current_tokens = await storage.get_tokens()
    provider.context.client_info = await storage.get_client_info()
    provider.context.token_expiry_time = await storage.get_expiry()
    provider._initialized = True


__all__ = [
    "CALLBACK_TIMEOUT_SECONDS",
    "DEFAULT_CALLBACK_PORT",
    "OAuthStorageError",
    "OwnerTokenStorage",
    "default_redirect_handler",
    "make_loopback_callback",
    "preload_tokens",
]
