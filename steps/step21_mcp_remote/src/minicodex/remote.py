"""Reaching a server that is not a subprocess: headers, a bearer token, or OAuth.

`mcp.Client` already knows how to speak to a URL -- chapter 9's line still
holds: hand-roll what you are teaching, depend on what you are not, and a
streamable-HTTP client is exactly the kind of thing not to hand-roll twice.
What the SDK does not know is which headers this deployment sends, whether a
token comes from an environment variable or a browser, and whose disk a
refresh token is allowed to sit on. That is this module's job, plus
`mcp_oauth.py`'s for the credential half.

Two things here were measured against the installed SDK (2.1.1) rather than
assumed, because the first draft of this chapter assumed both and was wrong
about one:

  - `mcp.Client(url_string)` always builds a `streamable_http_client` --
    `Client.__post_init__` has no branch toward the deprecated SSE transport
    for a bare URL, and `mode='auto'` is about protocol-version negotiation
    (probe `server/discover`, fall back to `initialize`), not transport
    choice. codex's own remote transport is exclusively streamable HTTP too --
    `RawMcpServerConfig` (`codex-rs/config/src/mcp_types.rs:274-291`) has a
    `// streamable_http` comment directly above `url`/`bearer_token_env_var`,
    with no SSE variant beside it. So `RemoteServerConfig` below has no
    transport field: there is exactly one remote transport, in both projects.

  - `streamable_http_client(url, *, http_client=, terminate_on_close=)` does
    **not** take `headers=` or `auth=` -- unlike `sse_client`, which still
    does. Its own docstring names the door instead: "To configure headers,
    authentication, or other HTTP settings, create an httpx2.AsyncClient and
    pass it here." So headers, the bearer token and OAuth are all wired onto
    an `httpx2.AsyncClient` this module builds, handed over as `http_client=`.
"""

from __future__ import annotations

import contextlib
import os
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

import httpx2
from mcp.client.auth import AuthorizationCodeResult, OAuthClientProvider
from mcp.client.streamable_http import streamable_http_client
from mcp.shared.auth import OAuthClientMetadata

from minicodex.mcp_oauth import (
    DEFAULT_CALLBACK_PORT,
    OwnerTokenStorage,
    default_redirect_handler,
    make_loopback_callback,
    preload_tokens,
)
from minicodex.tenancy import DEFAULT_OWNER, Owner

# Same defaults as `mcp.py`'s stdio `ServerConfig`.  Not imported from there:
# `mcp.py` imports this module for its remote-dispatch branch, and importing
# back would be a cycle for two numbers.
DEFAULT_STARTUP_TIMEOUT = 30.0
DEFAULT_TOOL_TIMEOUT = 60.0

# The recommended timeouts `streamable_http_client`'s own docstring describes
# for the default client it builds when `http_client=` is omitted: 30s for an
# ordinary request, 300s of read time for a POST whose reply is a long-lived
# SSE stream. Written out rather than imported from `mcp.shared._httpx_utils`
# (where the SDK keeps its own copy, `create_mcp_http_client`): a
# leading-underscore module is not a published API, and this project depends
# on published ones -- the same rule chapter 9 applied to the SDK's own
# environment allowlist, in the other direction.
_CONNECT_TIMEOUT = 30.0
_READ_TIMEOUT = 300.0

RedirectHandler = Callable[[str], Awaitable[None]]
CallbackHandler = Callable[[], Awaitable[AuthorizationCodeResult]]


class RemoteConfigError(ValueError):
    """A `RemoteServerConfig` was asked to authenticate two contradictory ways."""


@dataclass(frozen=True)
class RemoteOAuthConfig:
    """Enough to run the SDK's OAuth flow for one server.

    `client_id`, when given, is written into this server's token storage
    before the first connection, so `OAuthClientProvider` finds a
    `client_info` already on file and its own dynamic-registration step
    (RFC 7591) never runs. That is codex's posture: it has no registration
    code at all (grepping `register_client|DynamicClient|/register` across
    `codex-rs/rmcp-client/src` returns nothing) and instead carries a
    `client_id` per server it blesses (`perform_oauth_login.rs`'s
    `oauth_client_id` parameter). Leaving `client_id` unset here is what lets
    the SDK attempt RFC 7591 against the authorization server -- the real
    divergence this chapter measures rather than assumes, as F21-08b.
    """

    client_id: str | None = None
    redirect_port: int = DEFAULT_CALLBACK_PORT
    scope: str | None = None


@dataclass(frozen=True)
class RemoteServerConfig:
    """One MCP server reached over HTTPS instead of a subprocess.

    Mirrors codex's `RawMcpServerConfig` streamable_http fields
    (`codex-rs/config/src/mcp_types.rs:274-291`): `url`, `http_headers`,
    `env_http_headers`, `bearer_token_env_var`. There is no `bearer_token`
    field, though codex has one at `mcp_types.rs:290` -- marked
    `#[schemars(skip)]`, hidden from its own generated config schema -- for
    the reason chapter 9 already gives for `bearer_token_env_var` existing at
    all: a config file gets committed, pasted into an issue, read over a
    shoulder. Only the *name* of an environment variable is accepted here.
    """

    name: str
    url: str
    bearer_token_env_var: str | None = None
    http_headers: dict[str, str] = field(default_factory=dict)
    #: header name -> environment variable name, resolved at connect time.
    #: Matches codex's `env_http_headers` (`rmcp-client/src/utils.rs`): a
    #: blank or unset variable contributes no header, not one with an empty
    #: value.
    env_http_headers: dict[str, str] = field(default_factory=dict)
    oauth: RemoteOAuthConfig | None = None
    startup_timeout: float = DEFAULT_STARTUP_TIMEOUT
    tool_timeout: float = DEFAULT_TOOL_TIMEOUT

    def __post_init__(self) -> None:
        if self.bearer_token_env_var and self.oauth is not None:
            raise RemoteConfigError(
                f"{self.name}: a server is authenticated one way or the other -- "
                "'bearer_token_env_var' and OAuth cannot both be configured"
            )

    @property
    def kind(self) -> str:
        return "http"


def resolved_headers(config: RemoteServerConfig) -> dict[str, str]:
    """Static headers, then env-sourced ones, then the bearer token.

    codex's own order (`rmcp-client/src/utils.rs`'s `build_default_headers`):
    static `http_headers` first, `env_http_headers` layered on top with a
    blank or unset variable contributing nothing rather than an empty header.
    The bearer token is this project's own addition on top of that -- codex
    keeps it as a separate field entirely, but the wire effect (one more
    `Authorization` header, sourced from the environment) is the same shape.
    """
    headers = dict(config.http_headers)
    for header_name, env_var in config.env_http_headers.items():
        value = os.environ.get(env_var, "")
        if value.strip():
            headers[header_name] = value
    if config.bearer_token_env_var:
        token = os.environ.get(config.bearer_token_env_var) or None
        if token:
            headers["Authorization"] = f"Bearer {token}"
    return headers


async def build_oauth_provider(
    config: RemoteServerConfig,
    *,
    owner: Owner = DEFAULT_OWNER,
    redirect_handler: RedirectHandler | None = None,
    callback_handler: CallbackHandler | None = None,
) -> OAuthClientProvider:
    """An `OAuthClientProvider` for one server, storage bound to one `Owner`.

    `redirect_handler`/`callback_handler` default to the real ones
    (`mcp_oauth.default_redirect_handler`, a fresh loopback server per call) --
    injected here rather than looked up globally so a test never has to touch
    a browser or a real port, the same shape chapter 5 gave `Session.approver`.

    Async, and eager, for two reasons that are not this function's own to fix
    and both need to happen before this provider ever sees a request:

      - `config.oauth.client_id`, when set, has to be in storage *before*
        the SDK's own `_initialize()` would look for it -- `seed_client_id`
        is what makes the codex-style "never attempt RFC 7591" posture
        actually take effect, rather than being a config field nothing reads.
      - `mcp_oauth.preload_tokens` (F21-15): without it, a token this project
        knows perfectly well has expired -- it wrote the timestamp itself --
        looks valid to the SDK until the resource server says otherwise, and
        *that* 401 does not try the refresh token sitting right there; it
        opens a browser.
    """
    assert config.oauth is not None  # the caller has already checked
    storage = OwnerTokenStorage(owner, server=config.name)
    redirect_uri = f"http://127.0.0.1:{config.oauth.redirect_port}/callback"
    if config.oauth.client_id:
        await storage.seed_client_id(config.oauth.client_id, redirect_uri=redirect_uri)
    provider = OAuthClientProvider(
        server_url=config.url,
        client_metadata=OAuthClientMetadata(
            redirect_uris=[redirect_uri],  # type: ignore[list-item]
            scope=config.oauth.scope,
        ),
        storage=storage,
        redirect_handler=redirect_handler or default_redirect_handler,
        callback_handler=callback_handler or make_loopback_callback(config.oauth.redirect_port),
    )
    await preload_tokens(provider, storage)
    return provider


@contextlib.asynccontextmanager
async def _owned_http_client_transport(url: str, client: httpx2.AsyncClient) -> AsyncIterator[Any]:
    """Bridge an `httpx2.AsyncClient` this module builds into a `Transport`.

    `streamable_http_client` only manages the lifecycle of a client it built
    itself -- `client_provided = http_client is not None`, and the exit stack
    inside it enters the client only when that is `False` (read from the
    source, not assumed). Handing over one built here means this function
    owns closing it, which is what the outer `async with client` is for.
    """
    async with client, streamable_http_client(url, http_client=client) as streams:
        yield streams


async def build_transport(
    config: RemoteServerConfig,
    *,
    owner: Owner = DEFAULT_OWNER,
    redirect_handler: RedirectHandler | None = None,
    callback_handler: CallbackHandler | None = None,
) -> Any:
    """A `Transport` for `mcp.Client`, wired with this project's headers and,
    when configured, OAuth.

    `sandbox` never reaches this function, and that asymmetry is the chapter's
    own point (F21-14): a subprocess is something this process can confine,
    and somebody else's web server is not. What a remote server gets instead
    is chapter 20's network boundary one level up -- reached from inside
    `--unshare-net`, it is simply unreachable, which is correct and worth
    saying out loud rather than discovering.
    """
    auth: httpx2.Auth | None = None
    if config.oauth is not None:
        auth = await build_oauth_provider(
            config,
            owner=owner,
            redirect_handler=redirect_handler,
            callback_handler=callback_handler,
        )
    client = httpx2.AsyncClient(
        headers=resolved_headers(config) or None,
        auth=auth,
        timeout=httpx2.Timeout(_CONNECT_TIMEOUT, read=_READ_TIMEOUT),
    )
    return _owned_http_client_transport(config.url, client)


__all__ = [
    "DEFAULT_STARTUP_TIMEOUT",
    "DEFAULT_TOOL_TIMEOUT",
    "CallbackHandler",
    "RedirectHandler",
    "RemoteConfigError",
    "RemoteOAuthConfig",
    "RemoteServerConfig",
    "build_oauth_provider",
    "build_transport",
    "resolved_headers",
]
