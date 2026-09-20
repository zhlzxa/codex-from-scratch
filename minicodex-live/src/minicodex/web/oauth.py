"""Interactive OAuth for a browser that is not on the machine running the agent.

Upstream's OAuth is written for a CLI: `build_transport` hands
`OAuthClientProvider` a `redirect_handler` that prints a URL and opens a local
browser, and a `callback_handler` that opens a loopback socket on the same
machine and blocks until the one request it is waiting for arrives. Both
assume the process asking for authorization and the browser doing the
authorizing share a machine -- true for a terminal, false for this console:
the agent runs on a server, the browser is wherever the person reading this
is, and it can reach that server only over the port this console already
listens on.

So the shape has to be two HTTP requests to *this console's own routes*
instead of one blocking call. `POST /api/mcp/oauth/start` (`routes.py`) does
everything `OAuthClientProvider` would do before it needs a person -- discover
the authorization server, register or seed a client, generate PKCE -- and
hands back a URL instead of opening one. `GET /api/mcp/oauth/callback` is
where the authorization server's redirect actually lands, arbitrarily later,
quite possibly on a different worker thread of this same process; it has
nothing to go on except the query string the browser carries back, and
`PendingOAuthTable` is what lets it find its way back to the attempt `start`
began.

That two-request split means the generator inside `OAuthClientProvider.
async_auth_flow` -- which only ever runs the whole way through in response to
one live 401 -- is not reusable here as-is: this module drives the same steps
directly instead, through the SDK's own (unexported but public, undocumented
only in the sense that it lives one module below the package's `__init__`)
building blocks in `mcp.client.auth.oauth2` and `mcp.client.auth.utils`:
`OAuthContext` for the request/response shapes and its already-correct
`prepare_token_auth`/`get_resource_url` methods, `PKCEParameters.generate()`
for the challenge, and the free functions for protected-resource discovery,
authorization-server discovery and dynamic registration. None of RFC 7591,
RFC 8414 or RFC 7636 is reimplemented; what is new here is only the seam that
splits one blocking call into two HTTP requests a browser can sit between.
"""

from __future__ import annotations

import secrets
import time
from dataclasses import dataclass
from urllib.parse import urlencode, urljoin

import httpx2
from mcp.client.auth import OAuthFlowError, OAuthRegistrationError, OAuthTokenError
from mcp.client.auth.oauth2 import OAuthContext, PKCEParameters, check_registration_usable
from mcp.client.auth.utils import (
    build_oauth_authorization_server_metadata_discovery_urls,
    build_protected_resource_metadata_discovery_urls,
    create_client_registration_request,
    create_oauth_metadata_request,
    get_client_metadata_scopes,
    handle_auth_metadata_response,
    handle_protected_resource_response,
    handle_registration_response,
    handle_token_response_scopes,
    validate_authorization_response_iss,
)
from mcp.shared.auth import OAuthClientMetadata

from minicodex.mcp_oauth import OwnerTokenStorage
from minicodex.remote import RemoteServerConfig
from minicodex.tenancy import DEFAULT_OWNER, Owner

# One round trip per discovery hop, at most a handful of hops (upstream's own
# two-URL fallback for each of the two well-known documents). 30s matches
# `remote.py`'s own `_CONNECT_TIMEOUT` -- there is no reason a metadata fetch
# should be allowed to take longer than an actual MCP connection is.
_DISCOVERY_TIMEOUT = 30.0

# How long a `start` that nobody ever finished authorizing is allowed to sit in
# memory.  Upstream's loopback `CALLBACK_TIMEOUT_SECONDS` has twice
# (300s): the loopback case times out the moment the *agent process* gives up
# waiting, one await; this one is a browser tab a person may have alt-tabbed
# away from, reached through a redirect rather than a direct socket, so it
# gets more slack rather than the same number reused without thinking about
# it.
DEFAULT_PENDING_TTL_SECONDS = 600.0


class OAuthStartError(RuntimeError):
    """`begin()` could not reach a usable authorization URL for this server."""


@dataclass
class PendingOAuth:
    """One `start` call's worth of state, waiting for its `callback`.

    `context` carries everything `finish()` needs that `start()` already
    learned -- discovered endpoints, the scope actually granted, the client
    this attempt is registered (or preset) as -- so the callback never has to
    rediscover anything the browser round trip did not change.
    """

    state: str
    server_id: str
    server_name: str
    owner: Owner
    redirect_uri: str
    code_verifier: str
    context: OAuthContext
    created_at: float


class PendingOAuthTable:
    """Every OAuth attempt this process has started and not yet completed.

    In-process and in-memory, like `ApprovalBroker` (`approver.py`) and for
    the same reason: this console's whole state is file-or-memory (`store.py`
    's own docstring), and an attempt that outlives the process is one nobody
    can click "authorize" fast enough to save anyway. Expiry is swept lazily,
    on the next `put`/`pop`, rather than by a background task -- one fewer
    thing running when nothing is happening, at the cost of a table that can
    hold one extra expired entry between sweeps, which is a cost this project
    is willing to pay the same way `JobStore` already does.
    """

    def __init__(self, ttl: float = DEFAULT_PENDING_TTL_SECONDS) -> None:
        self._ttl = ttl
        self._table: dict[str, PendingOAuth] = {}

    def _sweep(self) -> None:
        now = time.time()
        expired = [state for state, p in self._table.items() if now - p.created_at > self._ttl]
        for state in expired:
            del self._table[state]

    def put(self, pending: PendingOAuth) -> None:
        self._sweep()
        self._table[pending.state] = pending

    def pop(self, state: str) -> PendingOAuth | None:
        """The one lookup the callback route is allowed to trust.

        A `state` that is not a key in this table -- expired, never issued, or
        typed by hand -- returns `None` and nothing about the request is
        believed past that point. Popped rather than merely read: a state is
        good for exactly one callback, the same way an authorization code
        is good for exactly one token exchange.
        """
        self._sweep()
        return self._table.pop(state, None)

    def __len__(self) -> int:
        self._sweep()
        return len(self._table)


async def _discover(context: OAuthContext, client: httpx2.AsyncClient) -> None:
    """Steps 1-2 of `OAuthClientProvider.async_auth_flow`'s 401 branch, run
    directly instead of waiting for a 401 to trigger them.

    Failure here is not fatal in itself -- a server with no protected-resource
    metadata and no authorization-server metadata is legal (SEP-985's fallback
    chain existing at all is the SDK's own admission of this), and the
    fallback endpoints computed from `config.url` alone are exactly what a
    server like that expects to be asked. What is fatal is downstream: no
    `authorization_endpoint` to send anyone to, or `check_registration_usable`
    refusing what got registered.
    """
    for url in build_protected_resource_metadata_discovery_urls(None, context.server_url):
        response = await client.send(create_oauth_metadata_request(url))
        prm = await handle_protected_resource_response(response)
        if prm is not None:
            context.protected_resource_metadata = prm
            if prm.authorization_servers:
                context.auth_server_url = str(prm.authorization_servers[0])
            break

    for url in build_oauth_authorization_server_metadata_discovery_urls(
        context.auth_server_url, context.server_url
    ):
        response = await client.send(create_oauth_metadata_request(url))
        ok, asm = await handle_auth_metadata_response(response)
        if asm is not None:
            context.oauth_metadata = asm
            break
        if not ok:
            break

    context.client_metadata.scope = get_client_metadata_scopes(
        None,
        context.protected_resource_metadata,
        context.oauth_metadata,
        context.client_metadata.grant_types,
    )


async def begin(
    config: RemoteServerConfig,
    *,
    server_id: str,
    redirect_uri: str,
    owner: Owner = DEFAULT_OWNER,
) -> tuple[str, PendingOAuth]:
    """Discover, register (or confirm a preset client), generate PKCE, and
    build the URL a person needs to open. Returns `(authorize_url, pending)`.

    Raises `OAuthRegistrationError` when the server has no
    `registration_endpoint` and this server's record carries no preset
    `client_id` -- GitHub's own authorization server, measured
    (`probe_mcp_remote.py wellknown`), is exactly this case: it does not
    advertise dynamic client registration, so an interactive "Connect GitHub"
    click only reaches a URL when the console operator has already registered
    a GitHub OAuth App and put its `client_id` on this server's record.
    """
    assert config.oauth is not None
    storage = OwnerTokenStorage(owner, server=config.name)
    if config.oauth.client_id:
        await storage.seed_client_id(config.oauth.client_id, redirect_uri=redirect_uri)

    context = OAuthContext(
        server_url=config.url,
        client_metadata=OAuthClientMetadata(
            redirect_uris=[redirect_uri],  # type: ignore[list-item]
            scope=config.oauth.scope,
        ),
        storage=storage,
        redirect_handler=None,
        callback_handler=None,
    )
    context.client_info = await storage.get_client_info()

    async with httpx2.AsyncClient(timeout=httpx2.Timeout(_DISCOVERY_TIMEOUT)) as client:
        await _discover(context, client)

        if context.client_info is None:
            registration_request = create_client_registration_request(
                context.oauth_metadata,
                context.client_metadata,
                context.get_authorization_base_url(config.url),
            )
            registration_response = await client.send(registration_request)
            client_info = await handle_registration_response(registration_response)
            check_registration_usable(client_info)
            context.client_info = client_info
            await storage.set_client_info(client_info)

    if context.oauth_metadata and context.oauth_metadata.authorization_endpoint:
        auth_endpoint = str(context.oauth_metadata.authorization_endpoint)
    else:
        auth_endpoint = urljoin(context.get_authorization_base_url(config.url) + "/", "authorize")

    pkce = PKCEParameters.generate()
    state = secrets.token_urlsafe(32)
    auth_params = {
        "response_type": "code",
        "client_id": context.client_info.client_id,
        "redirect_uri": redirect_uri,
        "state": state,
        "code_challenge": pkce.code_challenge,
        "code_challenge_method": "S256",
    }
    if context.should_include_resource_param(None):
        auth_params["resource"] = context.get_resource_url()
    if context.client_metadata.scope:
        auth_params["scope"] = context.client_metadata.scope

    authorize_url = f"{auth_endpoint}?{urlencode(auth_params)}"
    pending = PendingOAuth(
        state=state,
        server_id=server_id,
        server_name=config.name,
        owner=owner,
        redirect_uri=redirect_uri,
        code_verifier=pkce.code_verifier,
        context=context,
        created_at=time.time(),
    )
    return authorize_url, pending


async def finish(pending: PendingOAuth, *, code: str, iss: str | None) -> None:
    """The callback's half: exchange the code, persist the tokens.

    Everything needed to build the token request was already learned in
    `begin()` and carried on `pending.context` -- this function makes exactly
    one more network call, to the token endpoint, and one disk write, to
    `OwnerTokenStorage`.
    """
    context = pending.context
    validate_authorization_response_iss(iss, context.oauth_metadata)
    assert context.client_info is not None

    if context.oauth_metadata and context.oauth_metadata.token_endpoint:
        token_url = str(context.oauth_metadata.token_endpoint)
    else:
        token_url = urljoin(context.get_authorization_base_url(context.server_url) + "/", "token")

    token_data: dict[str, str] = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": pending.redirect_uri,
        "client_id": context.client_info.client_id,
        "code_verifier": pending.code_verifier,
    }
    if context.should_include_resource_param(None):
        token_data["resource"] = context.get_resource_url()
    headers = {"Content-Type": "application/x-www-form-urlencoded"}
    token_data, headers = context.prepare_token_auth(token_data, headers)

    async with httpx2.AsyncClient(timeout=httpx2.Timeout(_DISCOVERY_TIMEOUT)) as client:
        response = await client.post(token_url, data=token_data, headers=headers)
    if response.status_code not in (200, 201):
        body = (await response.aread()).decode("utf-8", errors="replace")
        raise OAuthTokenError(f"token exchange failed ({response.status_code}): {body}")
    token = await handle_token_response_scopes(response)
    if token.scope is None:
        token.scope = context.client_metadata.scope
    await context.storage.set_tokens(token)


__all__ = [
    "DEFAULT_PENDING_TTL_SECONDS",
    "OAuthFlowError",
    "OAuthRegistrationError",
    "OAuthStartError",
    "OAuthTokenError",
    "PendingOAuth",
    "PendingOAuthTable",
    "begin",
    "finish",
]
