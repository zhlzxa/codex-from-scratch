"""Chapter 22: a browser-driven remote MCP connection, from the console.

Every test here is offline, the same discipline chapter 21's own tests hold
to: `OAuthStub` (`tests/mcp_http_stub.py`) plays the authorization server,
`fastapi.testclient.TestClient` plays the browser talking to this console,
and a plain `httpx2` client plays the browser being redirected to the
authorization server and back -- nothing here resolves a real hostname.

`DEFAULT_OWNER` is patched to a `tmp_path`-backed `Owner` in every test that
reaches a route touching it (`console`'s own fixture does this once, for
every test that uses it): the real one resolves under the actual user's home
directory, and a web-layer test that skipped this would leave a stray
`mcp_tokens.json` under `~/.minicodex/` every time it ran.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx2
import pytest
from fastapi.testclient import TestClient
from mcp.client.auth.oauth2 import OAuthContext
from mcp.shared.auth import OAuthClientMetadata, OAuthToken

from console_login import CONSOLE_ACCOUNT, sign_in, sign_in_async
from mcp_http_stub import OAuthStub
from minicodex.mcp_oauth import OwnerTokenStorage
from minicodex.remote import RemoteConfigError, RemoteOAuthConfig, RemoteServerConfig
from minicodex.tenancy import Owner
from minicodex.web import create_app
from minicodex.web import runtime as web_runtime
from minicodex.web.mcp_config import record_kind, server_config
from minicodex.web.oauth import PendingOAuth, PendingOAuthTable, begin, finish


@pytest.fixture
def owner(tmp_path: Path) -> Owner:
    """The tenancy the signed-in test account gets.

    Chapter 22 wrote this as `Owner(home=...)` -- key `None`, the single-tenant
    case -- because the console had one owner and it was the default one.
    Chapter 23 gave the console a login, so the owner a request resolves to is
    the account's, and these tests say which account that is instead of
    patching a module constant.
    """
    return Owner(CONSOLE_ACCOUNT, home=tmp_path / "home")


@pytest.fixture
def console(tmp_path: Path) -> Any:
    with TestClient(create_app(tmp_path / "console", home=tmp_path / "home")) as client:
        sign_in(client)
        yield client


def _async_api(tmp_path: Path, owner: Owner, monkeypatch: pytest.MonkeyPatch) -> httpx2.AsyncClient:
    """An in-process client for the two tests that also run `OAuthStub` --
    a real `asyncio.start_server` -- in the same coroutine.

    `TestClient` answers a request on a *second* thread with its own event
    loop; a test that also `await`s directly on `OAuthStub`'s listening
    socket from the first thread would have to block that thread synchronously
    to get `TestClient`'s answer, which starves the very loop `OAuthStub`
    needs free to accept the connection this console's own outbound request
    is about to make to it. `ASGITransport` calls the app in-process, on
    whichever loop is already running -- one loop, no blocking hand-off, no
    deadlock.
    """
    app = create_app(tmp_path / "console", home=tmp_path / "home")
    return httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app), base_url="http://testserver")


def _dummy_pending(state: str, *, created_at: float, home: Path) -> PendingOAuth:
    """A `PendingOAuth` with no real network behind it -- for the two tests
    (F22-02, F22-03) that only need the table's own bookkeeping, not a
    completed flow."""
    owner = Owner(CONSOLE_ACCOUNT, home=home)
    context = OAuthContext(
        server_url="https://example.test/mcp",
        client_metadata=OAuthClientMetadata(redirect_uris=["https://console.example/cb"]),
        storage=OwnerTokenStorage(owner, server="gh"),
        redirect_handler=None,
        callback_handler=None,
    )
    return PendingOAuth(
        state=state,
        server_id="srv-1",
        server_name="gh",
        owner=owner,
        redirect_uri="https://console.example/cb",
        code_verifier="a-code-verifier-at-least-forty-three-characters-long",
        context=context,
        created_at=created_at,
    )


# ---------------------------------------------------------------------------
# F22-01  the console's own architecture: start and callback are two
# independent HTTP requests, not one blocking call (chapter 21's
# `redirect_handler`/`callback_handler` do not carry over -- see
# `web/oauth.py`'s module docstring for why).
# ---------------------------------------------------------------------------


async def test_F22_01_begin_and_finish_are_two_independent_calls_a_request_can_sit_between(
    tmp_path: Path,
) -> None:
    """`build_oauth_provider` (chapter 21) blocks one coroutine from
    `redirect_handler` straight through to `callback_handler` returning.
    `begin()` returns as soon as it has a URL; `finish()` is a second call
    with nothing shared except `pending` -- proving a real HTTP request (the
    browser's own redirect back) can land between them, which chapter 21's
    shape cannot allow at all.
    """
    owner = Owner(CONSOLE_ACCOUNT, home=tmp_path / "home")
    async with OAuthStub(supports_dcr=False) as auth:
        config = RemoteServerConfig(
            name="gh", url=auth.resource_url, oauth=RemoteOAuthConfig(client_id="preset-client")
        )
        authorize_url, pending = await begin(
            config,
            server_id="srv-1",
            redirect_uri="https://console.example/api/mcp/oauth/callback",
            owner=owner,
        )
        assert authorize_url.startswith(f"{auth.base}/authorize?")
        params = parse_qs(urlparse(authorize_url).query)
        assert params["code_challenge_method"] == ["S256"]
        assert len(params["code_challenge"][0]) >= 43
        assert params["redirect_uri"] == ["https://console.example/api/mcp/oauth/callback"]
        assert params["state"] == [pending.state]

        # The browser's half of the round trip, driven directly the way
        # chapter 21's `ScriptedBrowser` drives it -- this is the "someone
        # else's tab" this console's callback route stands in for.
        async with httpx2.AsyncClient() as client:
            response = await client.get(authorize_url, follow_redirects=False)
        location = response.headers["location"]
        callback_params = parse_qs(urlparse(location).query)

        # A second, independent call -- exactly what `GET /api/mcp/oauth/
        # callback` does with what the query string handed it.
        await finish(
            pending,
            code=callback_params["code"][0],
            iss=(callback_params.get("iss") or [None])[0],
        )
        assert auth.token_calls == 1

    tokens = await OwnerTokenStorage(owner, server="gh").get_tokens()
    assert tokens is not None and tokens.access_token == "access-1"


async def test_F22_01_starting_oauth_against_a_server_with_no_dcr_and_no_client_id_fails_clearly(
    tmp_path: Path, owner: Owner, monkeypatch: pytest.MonkeyPatch
) -> None:
    """GitHub's own authorization server measured in chapter 21
    (`probe_mcp_remote.py wellknown`): no `registration_endpoint`. Reaching
    that with no preset `client_id` has to fail as a form error naming the
    reason, not a stack trace -- the same F22-04-style translation, for a
    library exception this time (`OAuthRegistrationError`) instead of a
    `ValueError`.
    """
    async with (
        OAuthStub(supports_dcr=False) as auth,
        _async_api(tmp_path, owner, monkeypatch) as api,
    ):
        await sign_in_async(api)
        created = (
            await api.post("/api/mcp", json={"name": "gh", "url": auth.resource_url, "oauth": True})
        ).json()
        started = await api.post("/api/mcp/oauth/start", json={"server_id": created["id"]})
        assert started.status_code == 400
        assert "could not start OAuth" in started.json()["detail"]


# ---------------------------------------------------------------------------
# F22-02  `state` is server-generated and server-validated; nothing the
# browser echoes back is trusted past a table lookup.
# ---------------------------------------------------------------------------


def test_F22_02_an_unissued_state_returns_nothing() -> None:
    table = PendingOAuthTable()
    assert table.pop("nobody-ever-issued-this") is None


def test_F22_02_a_state_is_good_for_exactly_one_callback(tmp_path: Path) -> None:
    table = PendingOAuthTable()
    pending = _dummy_pending("state-1", created_at=time.time(), home=tmp_path)
    table.put(pending)
    assert table.pop("state-1") is pending
    assert table.pop("state-1") is None, "a state must not be usable a second time"


async def test_F22_02_two_attempts_never_share_a_state(tmp_path: Path) -> None:
    """A guessable or reused `state` is an open redirect/CSRF surface (the
    fault this ID is named for) -- `begin()` has to draw a fresh one every
    time, not just look one up."""
    owner = Owner(CONSOLE_ACCOUNT, home=tmp_path / "home")
    async with OAuthStub(supports_dcr=False) as auth:
        config = RemoteServerConfig(
            name="gh", url=auth.resource_url, oauth=RemoteOAuthConfig(client_id="preset-client")
        )
        _url_a, pending_a = await begin(
            config, server_id="a", redirect_uri="https://console.example/cb", owner=owner
        )
        _url_b, pending_b = await begin(
            config, server_id="b", redirect_uri="https://console.example/cb", owner=owner
        )
    assert pending_a.state != pending_b.state


def test_F22_02_the_callback_route_answers_an_unknown_state_without_touching_storage(
    console: TestClient,
) -> None:
    response = console.get(
        "/api/mcp/oauth/callback", params={"state": "not-real", "code": "whatever"}
    )
    assert response.status_code == 200
    assert "unknown or has expired" in response.text


def test_F22_02_a_missing_state_is_treated_the_same_as_an_unknown_one(
    console: TestClient,
) -> None:
    response = console.get("/api/mcp/oauth/callback", params={"code": "whatever"})
    assert response.status_code == 200
    assert "unknown or has expired" in response.text


# ---------------------------------------------------------------------------
# F22-03  a pending attempt that nobody ever finished does not sit in memory
# forever.
# ---------------------------------------------------------------------------


def test_F22_03_a_pending_attempt_expires(tmp_path: Path) -> None:
    table = PendingOAuthTable(ttl=1.0)
    pending = _dummy_pending("expired", created_at=time.time() - 2.0, home=tmp_path)
    table.put(pending)
    assert table.pop("expired") is None


def test_F22_03_a_fresh_attempt_survives_the_sweep(tmp_path: Path) -> None:
    table = PendingOAuthTable(ttl=600.0)
    pending = _dummy_pending("fresh", created_at=time.time(), home=tmp_path)
    table.put(pending)
    assert table.pop("fresh") is pending


def test_F22_03_expiry_is_swept_lazily_rather_than_by_a_background_task(tmp_path: Path) -> None:
    """`put`/`pop` are the only two places `_sweep` runs from -- an attempt
    that expired between two calls disappears the moment either is called
    again, with nothing running in the background to notice sooner."""
    table = PendingOAuthTable(ttl=1.0)
    table.put(_dummy_pending("a", created_at=time.time() - 5.0, home=tmp_path))
    table.put(_dummy_pending("b", created_at=time.time(), home=tmp_path))
    assert len(table) == 1  # "a" was already stale when "b" triggered the sweep


# ---------------------------------------------------------------------------
# F22-04  `add_mcp` refuses "both" and "neither", in the web layer's own
# vocabulary -- a form error, not a raised exception. `mcp.load_config` made
# the same call at the library layer for the same shape (F21-01).
# ---------------------------------------------------------------------------


def test_F22_04_add_mcp_refuses_a_command_and_a_url_together(console: TestClient) -> None:
    response = console.post(
        "/api/mcp", json={"name": "x", "command": "true", "url": "https://x.test/mcp"}
    )
    assert response.status_code == 400
    assert "not both" in response.json()["detail"]


def test_F22_04_add_mcp_refuses_neither_a_command_nor_a_url(console: TestClient) -> None:
    response = console.post("/api/mcp", json={"name": "x"})
    assert response.status_code == 400


def test_F22_04_add_mcp_refuses_a_bearer_token_and_oauth_together(console: TestClient) -> None:
    response = console.post(
        "/api/mcp",
        json={"name": "x", "url": "https://x.test/mcp", "bearer_token": "t", "oauth": True},
    )
    assert response.status_code == 400
    assert "not both" in response.json()["detail"]


def test_F22_04_server_config_refuses_the_same_combination_a_second_time(tmp_path: Path) -> None:
    """The same rule enforced again where a record actually turns into
    something that connects, in case one ever reaches here some other way
    than `add_mcp` -- a hand-edited store file, for instance."""
    with pytest.raises(RemoteConfigError):
        server_config(
            {
                "name": "x",
                "kind": "remote",
                "url": "https://x.test/mcp",
                "bearer_token": "t",
                "oauth": {"client_id": "cid"},
            }
        )


def test_a_bearer_token_is_never_sent_back_to_the_browser(console: TestClient) -> None:
    """`_public_mcp` draws the same line chapter 19's `_public` already draws
    for a provider's `api_key`."""
    console.post(
        "/api/mcp", json={"name": "gh", "url": "https://x.test/mcp", "bearer_token": "ghp_secret"}
    )
    body = console.get("/api/mcp").text
    assert "ghp_secret" not in body
    assert '"bearer_token":true' in body.replace(" ", "")


# ---------------------------------------------------------------------------
# `mcp_config.server_config`: one record -> the config that actually connects
# ---------------------------------------------------------------------------


def test_record_kind_is_derived_for_a_record_written_before_chapter_22() -> None:
    assert record_kind({"command": ["true"]}) == "stdio"
    assert record_kind({"url": "https://x.test/mcp"}) == "remote"


def test_server_config_folds_a_raw_bearer_token_into_an_authorization_header() -> None:
    config = server_config(
        {"name": "gh", "kind": "remote", "url": "https://x.test/mcp", "bearer_token": "tok"}
    )
    assert isinstance(config, RemoteServerConfig)
    assert config.http_headers["Authorization"] == "Bearer tok"


def test_server_config_builds_a_stdio_config_for_a_record_with_no_kind_key() -> None:
    config = server_config({"name": "notes", "command": ["python", "-m", "server"]})
    assert config.command == ("python", "-m", "server")


# ---------------------------------------------------------------------------
# `runtime.resolve_mcp_configs`: the point where F22-01's architecture meets
# an actual turn.
# ---------------------------------------------------------------------------


async def test_a_remote_oauth_server_without_a_token_is_skipped_with_a_clear_status(
    owner: Owner, monkeypatch: pytest.MonkeyPatch
) -> None:
    events: list[dict[str, Any]] = []
    record = {
        "kind": "remote",
        "name": "gh",
        "url": "https://x.test/mcp",
        "oauth": {"client_id": None, "scope": None},
    }
    configs = await web_runtime.resolve_mcp_configs([record], events.append, owner)
    assert configs == []
    assert events == [
        {
            "type": "mcp_status",
            "name": "gh",
            "status": "needs_connection",
            "detail": "not connected yet -- use Connect in the MCP tab",
        }
    ]


async def test_a_remote_oauth_server_with_a_token_on_file_is_connected_normally(
    owner: Owner, monkeypatch: pytest.MonkeyPatch
) -> None:
    await OwnerTokenStorage(owner, server="gh").set_tokens(OAuthToken(access_token="a"))
    events: list[dict[str, Any]] = []
    record = {
        "kind": "remote",
        "name": "gh",
        "url": "https://x.test/mcp",
        "oauth": {"client_id": None, "scope": None},
    }
    configs = await web_runtime.resolve_mcp_configs([record], events.append, owner)
    assert len(configs) == 1
    assert configs[0].name == "gh"
    assert events == []


async def test_a_stdio_server_is_unaffected_by_any_of_this() -> None:
    events: list[dict[str, Any]] = []
    record = {"kind": "stdio", "name": "notes", "command": ["python", "-m", "server"]}
    configs = await web_runtime.resolve_mcp_configs([record], events.append, owner)
    assert len(configs) == 1
    assert configs[0].command == ("python", "-m", "server")
    assert events == []


async def test_a_pre_chapter22_stdio_record_with_no_kind_key_still_dispatches() -> None:
    events: list[dict[str, Any]] = []
    record = {"name": "notes", "command": ["python", "-m", "server"], "env": {}, "cwd": None}
    configs = await web_runtime.resolve_mcp_configs([record], events.append, owner)
    assert len(configs) == 1
    assert events == []


# ---------------------------------------------------------------------------
# End to end: state -> callback -> token storage, through the actual routes.
# What proves the architecture (F22-01/02/03) actually holds together, rather
# than each piece merely working in isolation.
# ---------------------------------------------------------------------------


async def test_the_full_state_to_callback_to_token_storage_chain(
    tmp_path: Path, owner: Owner, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with (
        OAuthStub(supports_dcr=False) as auth,
        _async_api(tmp_path, owner, monkeypatch) as api,
    ):
        await sign_in_async(api)
        created = (
            await api.post(
                "/api/mcp",
                json={
                    "name": "gh",
                    "url": auth.resource_url,
                    "oauth": True,
                    "oauth_client_id": "preset-client",
                },
            )
        ).json()
        assert created["kind"] == "remote"

        started = await api.post("/api/mcp/oauth/start", json={"server_id": created["id"]})
        assert started.status_code == 200, started.text
        authorize_url = started.json()["authorize_url"]

        assert started.json()["previous"] is None
        before = (await api.get(f"/api/mcp/{created['id']}/oauth/status")).json()
        assert before == {"connected": False, "token": None}

        # A real network hop, to the stub authorization server -- unlike
        # `api`, which never leaves this process.
        async with httpx2.AsyncClient() as browser:
            response = await browser.get(authorize_url, follow_redirects=False)
        location = response.headers["location"]
        params = parse_qs(urlparse(location).query)

        callback = await api.get(
            "/api/mcp/oauth/callback",
            params={"code": params["code"][0], "state": params["state"][0]},
        )
        assert callback.status_code == 200
        assert "connected to gh" in callback.text

        after = (await api.get(f"/api/mcp/{created['id']}/oauth/status")).json()
        assert after["connected"] is True
        assert after["token"] is not None

    tokens = await OwnerTokenStorage(owner, server="gh").get_tokens()
    assert tokens is not None and tokens.access_token == "access-1"


async def test_a_second_use_of_the_same_callback_link_is_refused(
    tmp_path: Path, owner: Owner, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The state was popped the first time through; a reload of the same
    callback URL (a person double-clicking a stale link, a browser replaying
    a request) must not repeat the token exchange."""
    async with (
        OAuthStub(supports_dcr=False) as auth,
        _async_api(tmp_path, owner, monkeypatch) as api,
    ):
        await sign_in_async(api)
        created = (
            await api.post(
                "/api/mcp",
                json={
                    "name": "gh",
                    "url": auth.resource_url,
                    "oauth": True,
                    "oauth_client_id": "preset-client",
                },
            )
        ).json()
        authorize_url = (
            await api.post("/api/mcp/oauth/start", json={"server_id": created["id"]})
        ).json()["authorize_url"]
        async with httpx2.AsyncClient() as browser:
            response = await browser.get(authorize_url, follow_redirects=False)
        params = parse_qs(urlparse(response.headers["location"]).query)
        query = {"code": params["code"][0], "state": params["state"][0]}

        first = await api.get("/api/mcp/oauth/callback", params=query)
        assert "connected to gh" in first.text
        assert auth.token_calls == 1

        second = await api.get("/api/mcp/oauth/callback", params=query)
        assert "unknown or has expired" in second.text
        assert auth.token_calls == 1, "the code must not be exchanged twice"


# ---------------------------------------------------------------------------
# Found when chapter 22 was rewritten
# ---------------------------------------------------------------------------


async def _connect_through_the_console(
    api: httpx2.AsyncClient, auth: OAuthStub, server_id: str
) -> None:
    """start -> the browser at the authorization server -> callback."""
    authorize_url = (await api.post("/api/mcp/oauth/start", json={"server_id": server_id})).json()[
        "authorize_url"
    ]
    async with httpx2.AsyncClient() as browser:
        response = await browser.get(authorize_url, follow_redirects=False)
    params = parse_qs(urlparse(response.headers["location"]).query)
    callback = await api.get(
        "/api/mcp/oauth/callback", params={"code": params["code"][0], "state": params["state"][0]}
    )
    assert "connected to" in callback.text, callback.text


async def test_a_server_connected_in_the_console_is_usable_in_a_turn(
    tmp_path: Path, owner: Owner, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The chain the tests above stop short of: Connect in the console, then
    a turn's own `resolve_mcp_configs` + `connect()` -- with the refusing
    handlers a turn passes -- reaching a real MCP endpoint with that token.

    The first version's tests ended at "the token is on disk".  Past that
    point, every turn would have failed: chapter 21's `McpClient` lost the
    `Owner` (fixed when chapter 21 was rewritten), and no test here went far
    enough to see it.

    The MCP endpoint and its authorization server are on different ports, and
    only the endpoint's 401 says where the metadata is -- the case `begin()`
    could not handle until it read that 401 (`_discover`)."""
    from mcp_http_stub import HttpMcpStub
    from minicodex.registry import McpRegistry, connect

    async with (
        OAuthStub(supports_dcr=False) as auth,
        HttpMcpStub(require_auth=True, challenge=auth.prm_url, accepts=auth.is_live) as mcp,
        _async_api(tmp_path, owner, monkeypatch) as api,
    ):
        await sign_in_async(api)
        auth.resource = mcp.url
        created = (
            await api.post(
                "/api/mcp",
                json={"name": "gh", "url": mcp.url, "oauth": True, "oauth_client_id": "preset"},
            )
        ).json()
        await _connect_through_the_console(api, auth, created["id"])

        console_app = api._transport.app.state.console  # type: ignore[attr-defined]
        record = console_app.store_for(owner).get("mcp.json", created["id"])
        events: list[dict[str, Any]] = []
        configs = await web_runtime.resolve_mcp_configs([record], events.append, owner)
        assert events == []
        registry = McpRegistry()
        clients = await connect(
            configs,
            registry,
            owner=owner,
            redirect_handler=web_runtime._oauth_redirect_refused,
            callback_handler=web_runtime._oauth_callback_refused,
        )
        try:
            assert registry.failures == {}
            assert any("echo" in name for name in registry.handlers())
        finally:
            for client in clients:
                await client.close()
    assert mcp.authorized[-1] == "Bearer access-1"


def test_the_callback_page_escapes_what_the_authorization_server_sent(
    console: TestClient, tmp_path: Path
) -> None:
    """`error_description` comes from the redirect -- from whoever runs the
    authorization server.  The first version put it into the page as HTML,
    on the console's own origin."""
    app_console = console.app.state.console  # type: ignore[attr-defined]
    app_console.mcp_oauth.put(
        _dummy_pending("s-xss", created_at=time.time(), home=tmp_path / "home")
    )
    page = console.get(
        "/api/mcp/oauth/callback",
        params={
            "state": "s-xss",
            "error": "access_denied",
            "error_description": '<img src=x onerror="alert(1)">',
        },
    ).text
    assert "<img" not in page
    assert "&lt;img src=x onerror=&quot;alert(1)&quot;&gt;" in page


def test_two_servers_cannot_share_a_name(console: TestClient) -> None:
    """A remote server's token is stored under its name; two records with one
    name shared one token."""
    first = console.post("/api/mcp", json={"name": "gh", "url": "https://a.test/mcp"})
    assert first.status_code == 200
    second = console.post("/api/mcp", json={"name": "gh", "url": "https://b.test/mcp"})
    assert second.status_code == 409
    assert "already exists" in second.json()["detail"]


async def test_deleting_a_server_forgets_its_token(
    tmp_path: Path, owner: Owner, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Measured when chapter 22 was rewritten: delete "gh", add a new "gh"
    at another URL, and the new record read "Connected" -- and its first
    request carried the old server's token."""
    async with _async_api(tmp_path, owner, monkeypatch) as api:
        await sign_in_async(api)
        old = (
            await api.post(
                "/api/mcp",
                json={
                    "name": "gh",
                    "url": "https://a.test/mcp",
                    "oauth": True,
                    "oauth_client_id": "c",
                },
            )
        ).json()
        await OwnerTokenStorage(owner, server="gh").set_tokens(OAuthToken(access_token="gho_old"))
        assert (await api.delete(f"/api/mcp/{old['id']}")).json() == {"ok": True}
        assert await OwnerTokenStorage(owner, server="gh").get_tokens() is None

        new = (
            await api.post(
                "/api/mcp",
                json={
                    "name": "gh",
                    "url": "https://b.test/mcp",
                    "oauth": True,
                    "oauth_client_id": "c",
                },
            )
        ).json()
        status = (await api.get(f"/api/mcp/{new['id']}/oauth/status")).json()
        assert status == {"connected": False, "token": None}


async def test_reconnect_reports_a_new_token_not_the_old_one(
    tmp_path: Path, owner: Owner, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`start` says which token was on file before; the status after the
    callback carries a different one.  The first version's status was only
    `{"connected": true}`, which an old, dead token answers just as well."""
    async with (
        OAuthStub(supports_dcr=False) as auth,
        _async_api(tmp_path, owner, monkeypatch) as api,
    ):
        await sign_in_async(api)
        created = (
            await api.post(
                "/api/mcp",
                json={
                    "name": "gh",
                    "url": auth.resource_url,
                    "oauth": True,
                    "oauth_client_id": "p",
                },
            )
        ).json()
        await OwnerTokenStorage(owner, server="gh").set_tokens(OAuthToken(access_token="dead"))
        status_before = (await api.get(f"/api/mcp/{created['id']}/oauth/status")).json()
        started = (await api.post("/api/mcp/oauth/start", json={"server_id": created["id"]})).json()
        assert started["previous"] == status_before["token"] is not None

        async with httpx2.AsyncClient() as browser:
            response = await browser.get(started["authorize_url"], follow_redirects=False)
        params = parse_qs(urlparse(response.headers["location"]).query)
        await api.get(
            "/api/mcp/oauth/callback",
            params={"code": params["code"][0], "state": params["state"][0]},
        )
        status_after = (await api.get(f"/api/mcp/{created['id']}/oauth/status")).json()
    assert status_after["token"] not in (None, started["previous"])


def test_a_record_saved_before_chapter_22_goes_out_with_its_kind(
    console: TestClient, tmp_path: Path
) -> None:
    """`types.ts` promises the frontend a `kind` on every record; a stdio
    record saved before chapter 22 has none on disk."""
    app_console = console.app.state.console  # type: ignore[attr-defined]
    store = app_console.store_for(Owner(CONSOLE_ACCOUNT, home=tmp_path / "home"))
    store.add("mcp.json", {"name": "old", "command": ["true"], "enabled": True})
    (record,) = console.get("/api/mcp").json()
    assert record["kind"] == "stdio"


async def test_a_token_file_that_cannot_be_written_is_a_failed_page_not_a_500(
    tmp_path: Path, owner: Owner, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Chapter 21's rewrite made a broken token file an `OAuthStorageError`
    rather than a silent overwrite; the callback has to say so in a page,
    not answer the browser with an internal error."""
    async with (
        OAuthStub(supports_dcr=False) as auth,
        _async_api(tmp_path, owner, monkeypatch) as api,
    ):
        await sign_in_async(api)
        created = (
            await api.post(
                "/api/mcp",
                json={
                    "name": "gh",
                    "url": auth.resource_url,
                    "oauth": True,
                    "oauth_client_id": "p",
                },
            )
        ).json()
        authorize_url = (
            await api.post("/api/mcp/oauth/start", json={"server_id": created["id"]})
        ).json()["authorize_url"]
        owner.mcp_tokens().write_text("{ not json", encoding="utf-8")
        async with httpx2.AsyncClient() as browser:
            response = await browser.get(authorize_url, follow_redirects=False)
        params = parse_qs(urlparse(response.headers["location"]).query)
        callback = await api.get(
            "/api/mcp/oauth/callback",
            params={"code": params["code"][0], "state": params["state"][0]},
        )
    assert callback.status_code == 200
    assert "Connection failed" in callback.text
    assert "not overwriting" in callback.text


async def test_begin_follows_the_metadata_url_the_server_names_in_its_401(
    tmp_path: Path,
) -> None:
    """The narrow version of the test above: `begin()` alone, against an MCP
    endpoint whose metadata is only discoverable from its challenge.  The
    first version guessed well-known paths on the endpoint's own origin and
    built an authorize URL there."""
    from mcp_http_stub import HttpMcpStub

    async with (
        OAuthStub(supports_dcr=False) as auth,
        HttpMcpStub(require_auth=True, challenge=auth.prm_url) as mcp,
    ):
        auth.resource = mcp.url
        config = RemoteServerConfig(
            name="gh", url=mcp.url, oauth=RemoteOAuthConfig(client_id="preset")
        )
        authorize_url, _ = await begin(
            config,
            server_id="srv-1",
            redirect_uri="http://console.test/api/mcp/oauth/callback",
            owner=Owner(home=tmp_path / "home"),
        )
    assert authorize_url.startswith(f"{auth.base}/authorize?")


async def test_one_unusable_record_is_reported_and_skipped_not_fatal_to_the_turn(
    owner: Owner,
) -> None:
    """A record that cannot become a config -- here a hand-edited one with a
    token *and* OAuth, the case `server_config` refuses a second time -- is
    reported under its own name, and the other servers still connect.  The
    first version raised out of `resolve_mcp_configs`, and every turn failed."""
    records = [
        {
            "name": "broken",
            "kind": "remote",
            "url": "https://x.test/mcp",
            "bearer_token": "t",
            "oauth": {"client_id": "cid"},
        },
        {"name": "files", "command": ["true"]},
    ]
    events: list[dict[str, Any]] = []
    configs = await web_runtime.resolve_mcp_configs(records, events.append, owner)
    assert [c.name for c in configs] == ["files"]
    assert events[0]["name"] == "broken"
    assert events[0]["status"] == "error"
    assert "cannot be used" in events[0]["detail"]
