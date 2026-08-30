"""Chapter 23: who is using this console. Every test here is offline.

Nothing in this file opens a socket to anything but itself. `TestClient` speaks
ASGI in process, the authorization server of chapter 22 is not needed, and the
one test that measures wall-clock time (F23-06) does so against two calls into
the same function.

The faults divide into three kinds and the split is the chapter's argument.

**Is this request anybody?** F23-01 through F23-03: a gate that cannot be
forgotten, a gate that covers the socket as well as the routes, and a
handshake that is not exempt from the rule just because CORS is.

**Is this object theirs?** F23-04 and F23-09: every route in this console
identifies a thing by an id, and for twenty-two chapters "whose" was not a
question anybody could ask. The fix is mostly not a check -- it is removing the
shared table that made the check necessary.

**And the ones that only appear once there are two of you.** F23-05 is four
chapters old and was covered by a green test that copied the same wrong path.
F23-07 and F23-08 are about a turn being a background task: revoking access
that leaves work running has not revoked anything, and a quota charged on
completion is a quota a failing loop does not have.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from console_login import CONSOLE_ACCOUNT, CONSOLE_PASSWORD, sign_in
from minicodex.tenancy import Owner
from minicodex.web import accounts as accounts_mod
from minicodex.web import create_app
from minicodex.web.accounts import (
    MIN_PASSWORD_LENGTH,
    AccountError,
    AccountStore,
    hash_password,
    verify_password,
)
from minicodex.web.auth import GUARDED_PREFIXES, PUBLIC_PATHS, same_origin
from minicodex.web.channel import turn_key
from minicodex.web.quota import Limits, QuotaExceeded, QuotaLedger
from minicodex.web.runtime import load_feature_dirs
from minicodex.web.sessions import SessionTable
from minicodex.web.store import adopt_legacy_records

SECOND_PASSWORD = "another-perfectly-fine-password"


@pytest.fixture
def app(tmp_path: Path) -> Any:
    return create_app(tmp_path / "console", home=tmp_path / "home")


@pytest.fixture
def anon(app: Any) -> Any:
    """A client that has never signed in."""
    with TestClient(app) as client:
        yield client


@pytest.fixture
def console(app: Any) -> Any:
    with TestClient(app) as client:
        sign_in(client)
        yield client


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    path = tmp_path / "workspace"
    path.mkdir()
    return path


def _second_account(console: TestClient, key: str = "bob", **extra: Any) -> TestClient:
    """Create another account and return a client signed in as it."""
    response = console.post(
        "/api/accounts", json={"key": key, "password": SECOND_PASSWORD, **extra}
    )
    assert response.status_code == 200, response.text
    other = TestClient(console.app)
    signed = other.post("/api/auth/login", json={"key": key, "password": SECOND_PASSWORD})
    assert signed.status_code == 200, signed.text
    return other


def _every_route(app: Any) -> list[Any]:
    """Flatten the app's route table, including the included router.

    This version of FastAPI wraps `include_router` in a `_IncludedRouter` that
    holds its own `.routes`, so walking `app.routes` one level deep finds six
    entries -- `/openapi.json`, the docs, and the static fallback -- and none of
    the thirty-odd routes the console actually serves. The first version of the
    walk below did exactly that and passed, which is the failure mode chapter
    19 (F19-06) already had once: a check that silently stops covering the code
    it names. The `checked > 20` assertion at the bottom of the test is there
    because of it.
    """
    found: list[Any] = []
    stack = list(getattr(app, "routes", []))
    while stack:
        route = stack.pop()
        # `routes` for a `Mount`, `original_router` for the `_IncludedRouter`
        # this FastAPI version puts in the table for `include_router`. Both
        # spellings, because which one exists is a fact about the installed
        # version and this test is about the console.
        for attr in ("routes", "original_router"):
            child = getattr(route, attr, None)
            if child is not None:
                stack.extend(child if isinstance(child, list) else getattr(child, "routes", []))
        if hasattr(route, "path"):
            found.append(route)
    return found


def _thread(client: TestClient, workspace: Path, **settings: Any) -> str:
    response = client.post(
        "/api/threads",
        json={"workspace": str(workspace), "title": "test", "settings": settings},
    )
    assert response.status_code == 200, response.text
    return response.json()["id"]


# ---------------------------------------------------------------------------
# F23-01  the gate denies by default; being public is a list, not an omission
# ---------------------------------------------------------------------------


def test_F23_01_every_guarded_route_refuses_an_anonymous_caller(anon: TestClient) -> None:
    """Walk the app's own route table rather than a list written by hand.

    This is the mechanism, not a spot check. A route added next year is in
    `app.routes` the moment it is written, so it is in this test the moment it
    is written, and the only way to make it public is to put its path in
    `PUBLIC_PATHS` -- where a reviewer looking for the console's anonymous
    surface will find it, because that is the whole list.
    """
    checked = 0
    for route in _every_route(anon.app):
        path = getattr(route, "path", "")
        if not path.startswith(GUARDED_PREFIXES) or path in PUBLIC_PATHS:
            continue
        if "{" in path:
            # Path parameters get a value that cannot exist, so a 401 is the
            # gate answering rather than the route 404ing on the way past it.
            path = path.replace("{thread_id}", "nope").replace("{server_id}", "nope")
            path = path.replace("{provider_id}", "nope").replace("{approval_id}", "nope")
            path = path.replace("{session_id}", "nope").replace("{key}", "nope")
            path = path.replace("{index}", "0")
        methods = getattr(route, "methods", None) or {"GET"}
        method = sorted(methods - {"HEAD", "OPTIONS"})[0]
        response = anon.request(method, path, json={})
        assert response.status_code == 401, f"{method} {path} answered {response.status_code}"
        checked += 1
    assert checked > 20, f"only {checked} guarded routes were found; the walk is not working"


def test_F23_01_the_public_list_is_short_and_is_all_about_signing_in(anon: TestClient) -> None:
    assert PUBLIC_PATHS == {
        "/api/auth/status",
        "/api/auth/login",
        "/api/auth/bootstrap",
    }
    for path in PUBLIC_PATHS:
        method = "GET" if path.endswith("status") else "POST"
        assert anon.request(method, path, json={}).status_code != 401


def test_F23_01_the_frontend_bundle_is_not_behind_the_gate(anon: TestClient) -> None:
    """The login page has to come from somewhere.

    Everything outside `/api` and `/ws` is served without a session, and what
    it can do without one is nothing: every call it makes is guarded.
    """
    assert anon.get("/").status_code == 200


def test_F23_01_status_says_whether_a_console_is_claimed_and_nothing_else(
    anon: TestClient,
) -> None:
    fresh = anon.get("/api/auth/status").json()
    assert fresh == {"has_account": False, "signed_in": False, "account": None}
    sign_in(anon)
    claimed = anon.get("/api/auth/status").json()
    assert claimed["signed_in"] is True
    assert claimed["account"]["key"] == CONSOLE_ACCOUNT
    assert "password_hash" not in claimed["account"]


# ---------------------------------------------------------------------------
# F23-02  the gate covers the WebSocket, which a dispatch middleware does not
# ---------------------------------------------------------------------------


def test_F23_02_an_unauthenticated_socket_is_refused(anon: TestClient, tmp_path: Path) -> None:
    with pytest.raises(Exception):  # noqa: B017 - any refusal; the point is it does not open
        with anon.websocket_connect("/ws/threads/whatever"):
            pass


def test_F23_02_a_signed_in_socket_opens_on_its_own_thread(
    console: TestClient, workspace: Path
) -> None:
    thread_id = _thread(console, workspace)
    with console.websocket_connect(f"/ws/threads/{thread_id}") as ws:
        assert ws is not None


def test_F23_02_the_probe_that_measured_this_is_in_the_step(repo_root: Path) -> None:
    """`auth.py` says raw ASGI "because of the socket" and cites a probe.

    A docstring that cites a measurement has to be able to point at it, or it
    is an assertion wearing a measurement's clothes. Chapters 6, 14, 16, 17, 18
    and 20 each found their own measuring tool wrong; the cheapest defence
    against the next one is that the tool exists and is runnable.
    """
    probe = repo_root / "probe_ws_auth.py"
    assert probe.exists()
    assert "BaseHTTPMiddleware" in probe.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# F23-03  a WebSocket handshake is not subject to CORS; check Origin
# ---------------------------------------------------------------------------


def test_F23_03_a_cross_origin_handshake_is_refused(console: TestClient, workspace: Path) -> None:
    """The cookie would be attached by the browser; the origin is what stops it.

    `SameSite=Lax` covers cross-site form posts, and this is the hole it does
    not cover: a page on any origin may open a WebSocket to this console, and
    the browser sends the cookie with the handshake. So the handshake is where
    the origin is compared to the host.
    """
    thread_id = _thread(console, workspace)
    with pytest.raises(Exception):  # noqa: B017
        with console.websocket_connect(
            f"/ws/threads/{thread_id}", headers={"Origin": "https://evil.example"}
        ):
            pass


def test_F23_03_the_consoles_own_page_is_same_origin(console: TestClient, workspace: Path) -> None:
    thread_id = _thread(console, workspace)
    with console.websocket_connect(
        f"/ws/threads/{thread_id}", headers={"Origin": "http://testserver"}
    ) as ws:
        assert ws is not None


def test_F23_03_a_handshake_with_no_origin_at_all_is_allowed() -> None:
    """A non-browser client is not the attack this check exists for.

    `websocat`, a script, this test suite: none of them is carrying somebody
    else's cookies, because nothing is filling a cookie jar for them. The
    attack needs a browser, and a browser always sends `Origin`.
    """
    assert same_origin([(b"host", b"console.local")]) is True
    assert same_origin([(b"host", b"console.local"), (b"origin", b"http://console.local")]) is True
    assert same_origin([(b"host", b"console.local"), (b"origin", b"http://evil.test")]) is False


def test_F23_03_an_origin_with_no_host_header_is_refused() -> None:
    """Nothing to compare against is not a reason to allow it."""
    assert same_origin([(b"origin", b"http://console.local")]) is False


# ---------------------------------------------------------------------------
# F23-04  every route looked a thing up by id and never asked whose it was
# ---------------------------------------------------------------------------


def test_F23_04_another_account_cannot_read_your_thread(
    console: TestClient, workspace: Path
) -> None:
    """404, not 403, and the status code is the honest one.

    There is no ownership comparison anywhere in this path: `store_for` handed
    the route a store rooted at bob's own directory, and alice's thread is not
    in it. The thread is not forbidden to bob, it does not exist for him.
    """
    thread_id = _thread(console, workspace)
    bob = _second_account(console)
    assert bob.get(f"/api/threads/{thread_id}").status_code == 404
    assert bob.patch(f"/api/threads/{thread_id}", json={"title": "mine now"}).status_code == 404
    assert bob.delete(f"/api/threads/{thread_id}").json() == {"ok": False}
    assert bob.get("/api/workspaces").json() == []


def test_F23_04_another_account_cannot_open_your_event_socket(
    console: TestClient, workspace: Path
) -> None:
    """The socket is where the model's output and every command it ran go."""
    thread_id = _thread(console, workspace)
    bob = _second_account(console)
    with pytest.raises(Exception):  # noqa: B017
        with bob.websocket_connect(f"/ws/threads/{thread_id}"):
            pass


def test_F23_04_providers_and_mcp_servers_are_per_account(console: TestClient) -> None:
    console.post(
        "/api/mcp", json={"name": "mine", "command": "echo hi", "startup_timeout": 5.0}
    ).raise_for_status()
    bob = _second_account(console)
    assert [s["name"] for s in bob.get("/api/mcp").json()] == []
    # Providers are seeded per account, so bob has the two defaults and none of
    # alice's edits.
    alice_ids = {p["id"] for p in console.get("/api/providers").json()}
    bob_ids = {p["id"] for p in bob.get("/api/providers").json()}
    assert alice_ids and bob_ids and not (alice_ids & bob_ids)


def test_F23_04_an_approval_belongs_to_one_account(console: TestClient) -> None:
    """The brokers are separate objects, so there is no id to guess into.

    Twelve hex characters is not nothing, but "not enough entropy" is the wrong
    reason to be safe. There is no table containing anybody else's approvals.
    """
    live = console.app.state.console
    assert live.broker_for("alice") is not live.broker_for("bob")
    assert live.broker_for("alice") is live.broker_for("alice")


def test_F23_04_a_session_is_revoked_by_the_pair_not_by_the_id() -> None:
    """The one table that has to be shared, so the lookup takes both halves."""
    table = SessionTable()
    _token, alice = table.create("alice")
    _token2, _bob = table.create("bob")
    assert table.revoke_id("bob", alice.id) is False, "bob revoked alice's session"
    assert table.revoke_id("alice", alice.id) is True
    assert len(table) == 1


def test_F23_04_a_disabled_account_stops_being_signed_in(console: TestClient) -> None:
    """The session table outlives the account list, so it has to re-check.

    Revoking on `disable` is not enough on its own: a session created in the
    same millisecond, or a table that was never told, would still resolve. So
    `auth.resolve` looks the account up on every request and a disabled one is
    the same answer as a missing cookie.
    """
    bob = _second_account(console)
    assert bob.get("/api/workspaces").status_code == 200
    console.post("/api/accounts/bob/disable").raise_for_status()
    assert bob.get("/api/workspaces").status_code == 401


def test_F23_04_a_live_session_re_checks_the_account_on_every_request(
    console: TestClient,
) -> None:
    """Found by the mutation probe: nothing was testing this line.

    `POST /api/accounts/{key}/disable` revokes the account's sessions on the
    way out, so the disabled check inside `auth.resolve` never ran in any test
    -- breaking it changed no result. It is not redundant, though: the session
    table is in memory and the account list is a file, and anything that
    disables an account without going through that one route (a hand-edited
    `accounts.json`, a future admin tool, a second process) leaves a live
    session pointing at a disabled account.

    So this test disables it the way those would, behind the route, and asks
    whether the cookie still opens the door.
    """
    bob = _second_account(console)
    assert bob.get("/api/workspaces").status_code == 200

    live = console.app.state.console
    live.accounts.update("bob", disabled=True)  # no revoke, on purpose

    assert bob.get("/api/workspaces").status_code == 401
    assert live.sessions.sessions_for("bob") == [], "the stale session is dropped, not just refused"


def test_F23_04_an_admin_cannot_disable_themselves(console: TestClient) -> None:
    """A console with no enabled administrator has no way back."""
    response = console.post(f"/api/accounts/{CONSOLE_ACCOUNT}/disable")
    assert response.status_code == 400


def test_F23_04_a_normal_account_cannot_reach_the_admin_routes(console: TestClient) -> None:
    bob = _second_account(console)
    assert bob.get("/api/accounts").status_code == 403
    assert (
        bob.post("/api/accounts", json={"key": "eve", "password": SECOND_PASSWORD}).status_code
        == 403
    )


# ---------------------------------------------------------------------------
# F23-05  the memory panel read a directory no turn has ever used
# ---------------------------------------------------------------------------


def test_F23_05_the_memory_panel_reads_the_directory_the_agent_reads(
    console: TestClient, workspace: Path, tmp_path: Path
) -> None:
    """Written to the owner's home, read through the console. Same directory.

    Before this chapter the route answered with `<workspace>/.minicodex/
    memories` while every turn loaded `~/.minicodex/memories`, so this test
    would have shown an empty panel next to a full memory file -- which is
    exactly what a person would have seen.
    """
    memories = Owner(CONSOLE_ACCOUNT, home=tmp_path / "home").memories()
    memories.mkdir(parents=True)
    (memories / "MEMORY.md").write_text("v1\n\n## thing\nbody\n", encoding="utf-8")

    thread_id = _thread(console, workspace, memory=True)
    payload = console.get(f"/api/threads/{thread_id}/memory").json()
    assert payload["directory"] == str(memories)
    assert [entry["title"] for entry in payload["entries"]] == ["thing"]


def test_F23_05_two_accounts_do_not_share_a_memory(
    console: TestClient, workspace: Path, tmp_path: Path
) -> None:
    """Chapter 20 named this leak and could not close it: there was nobody to
    ask who this was. `load_feature_dirs` now takes an `Owner` and the console
    has one."""
    alice_dir = Owner(CONSOLE_ACCOUNT, home=tmp_path / "home").memories()
    alice_dir.mkdir(parents=True)
    (alice_dir / "MEMORY.md").write_text("v1\n\n## alice's\nsecret\n", encoding="utf-8")

    thread_id = _thread(console, workspace)
    bob = _second_account(console)
    bob_thread = _thread(bob, workspace)

    bob_view = bob.get(f"/api/threads/{bob_thread}/memory").json()
    assert bob_view["directory"] != str(alice_dir)
    assert bob_view["entries"] == []
    alice_view = console.get(f"/api/threads/{thread_id}/memory").json()
    assert [e["title"] for e in alice_view["entries"]] == ["alice's"]


def test_F23_05_a_turn_loads_the_memory_of_the_owner_it_runs_as(tmp_path: Path) -> None:
    """The other half of F23-05, and the mutation probe found it missing.

    Every test above goes through a route, so all of them would still pass
    against a `load_feature_dirs` that ignored its `owner` argument and loaded
    the default owner's memory -- the panel would be right and the turn would
    be wrong, which is the exact shape of the bug this chapter is fixing, one
    function further in.
    """
    alice = Owner("alice", home=tmp_path / "home")
    bob = Owner("bob", home=tmp_path / "home")
    alice.memories().mkdir(parents=True)
    (alice.memories() / "MEMORY.md").write_text("v1\n\n## alice's\nsecret\n", encoding="utf-8")

    memory, remember, _skills, _notices = load_feature_dirs(
        tmp_path, {"memory": True, "remember": True}, alice
    )
    assert memory is not None and [e.title for e in memory.entries] == ["alice's"]
    assert remember == alice.memories()

    empty, bob_remember, _s, _n = load_feature_dirs(
        tmp_path, {"memory": True, "remember": True}, bob
    )
    assert empty is None, "bob must not load alice's memory"
    assert bob_remember == bob.memories()


def test_F23_05_the_skills_panel_was_right_all_along(console: TestClient, workspace: Path) -> None:
    """The two panels were written side by side with the same path shape.

    Skills stayed per-project when memory moved to the home directory, so the
    workspace-relative spelling is correct here and was wrong one function
    above it. The pair is the whole lesson: a shape that is right next door is
    not evidence.
    """
    skills = workspace / ".minicodex" / "skills"
    skills.mkdir(parents=True)
    thread_id = _thread(console, workspace)
    payload = console.get(f"/api/threads/{thread_id}/skills").json()
    assert payload["directory"] == str(skills)


# ---------------------------------------------------------------------------
# F23-06  a failed login must not tell you whether the account exists
# ---------------------------------------------------------------------------


def test_F23_06_an_unknown_account_costs_the_same_as_a_wrong_password(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Measured, with the real cost parameter put back for this one test.

    The suite runs at `SCRYPT_N = 2**4` so that signing in is free (see
    `conftest.cheap_password_hashing`); at that cost the difference this test
    is about is inside the noise. Here the production figure is restored, one
    account is created, and the two failure paths are timed.

    The assertion is a ratio rather than an absolute, because absolutes in a
    timing test are how a suite becomes flaky on somebody else's laptop. A
    miss that skipped the hash entirely comes in around a hundred times faster;
    anything inside 3x is the same code path doing the same work.
    """
    monkeypatch.setattr(accounts_mod, "SCRYPT_N", accounts_mod.SCRYPT_N.__class__(2**14))
    store = AccountStore(tmp_path / "accounts.json")
    store.create("alice", "a-real-password-here")

    def timed(key: str) -> float:
        start = time.perf_counter()
        assert store.verify(key, "definitely-not-the-password") is None
        return time.perf_counter() - start

    known = timed("alice")
    unknown = timed("nobody-by-that-name")
    ratio = max(known, unknown) / max(min(known, unknown), 1e-9)
    assert ratio < 3.0, f"known {known * 1000:.0f} ms vs unknown {unknown * 1000:.0f} ms"


def test_F23_06_the_login_route_says_the_same_thing_either_way(anon: TestClient) -> None:
    sign_in(anon)
    wrong = anon.post(
        "/api/auth/login", json={"key": CONSOLE_ACCOUNT, "password": "nope-nope-nope"}
    )
    missing = anon.post("/api/auth/login", json={"key": "ghost", "password": "nope-nope-nope"})
    assert wrong.status_code == missing.status_code == 401
    assert wrong.json() == missing.json()


def test_F23_06_a_disabled_account_is_not_a_distinguishable_answer(console: TestClient) -> None:
    _second_account(console)
    console.post("/api/accounts/bob/disable").raise_for_status()
    third = TestClient(console.app)
    refused = third.post("/api/auth/login", json={"key": "bob", "password": SECOND_PASSWORD})
    ghost = third.post("/api/auth/login", json={"key": "ghost", "password": SECOND_PASSWORD})
    assert refused.status_code == 401
    assert refused.json() == ghost.json()


# ---------------------------------------------------------------------------
# F23-07  revoking access has to stop what that access left running
# ---------------------------------------------------------------------------


def test_F23_07_signing_out_everywhere_cancels_this_accounts_turns(
    console: TestClient, workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A turn is a background task that outlived the request that started it.

    So "sign out everywhere" that only empties the session table leaves the
    agent editing files and running commands for an account with no valid
    credential left, with nobody watching the socket.
    """
    import asyncio

    from minicodex.web import routes

    async def never(**_kwargs: Any) -> None:
        await asyncio.sleep(3600)

    monkeypatch.setattr(routes, "run_turn", never)

    thread_id = _thread(console, workspace)
    provider = console.get("/api/providers").json()[0]
    console.post(f"/api/providers/{provider['id']}/activate")
    assert (
        console.post(f"/api/threads/{thread_id}/messages", json={"text": "go"}).status_code == 202
    )

    live = console.app.state.console
    key = turn_key(CONSOLE_ACCOUNT, thread_id)
    assert key in live.running
    # Held before the revoke: the turn's own `finally` pops it out of
    # `running`, so reading the table afterwards is a race with the cleanup
    # that is supposed to happen.
    task = live.running[key]

    counts = console.post("/api/auth/sessions/revoke-all").json()
    assert counts["sessions"] == 1
    assert counts["turns"] == 1
    assert task.cancelled() or task.cancelling()
    assert console.get("/api/workspaces").status_code == 401


def test_F23_07_disabling_an_account_revokes_its_sessions(console: TestClient) -> None:
    bob = _second_account(console)
    live = console.app.state.console
    assert len(live.sessions.sessions_for("bob")) == 1
    console.post("/api/accounts/bob/disable").raise_for_status()
    assert live.sessions.sessions_for("bob") == []
    assert bob.get("/api/workspaces").status_code == 401


def test_F23_07_changing_a_password_ends_every_other_session(console: TestClient) -> None:
    """Otherwise a password change does not evict whoever it was for."""
    second_browser = TestClient(console.app)
    second_browser.post(
        "/api/auth/login", json={"key": CONSOLE_ACCOUNT, "password": CONSOLE_PASSWORD}
    ).raise_for_status()
    assert second_browser.get("/api/workspaces").status_code == 200

    changed = console.post(
        "/api/auth/password",
        json={"current": CONSOLE_PASSWORD, "replacement": "a-brand-new-password"},
    )
    assert changed.status_code == 200
    assert second_browser.get("/api/workspaces").status_code == 401
    # And the browser that asked keeps working, on a new session.
    assert console.get("/api/workspaces").status_code == 200


def test_F23_07_a_wrong_current_password_changes_nothing(console: TestClient) -> None:
    response = console.post(
        "/api/auth/password", json={"current": "not-it-at-all", "replacement": "a-new-password-x"}
    )
    assert response.status_code == 403
    assert console.get("/api/workspaces").status_code == 200


def test_F23_07_logging_out_ends_exactly_one_session(console: TestClient) -> None:
    second_browser = TestClient(console.app)
    second_browser.post(
        "/api/auth/login", json={"key": CONSOLE_ACCOUNT, "password": CONSOLE_PASSWORD}
    ).raise_for_status()
    console.post("/api/auth/logout").raise_for_status()
    assert console.get("/api/workspaces").status_code == 401
    assert second_browser.get("/api/workspaces").status_code == 200


# ---------------------------------------------------------------------------
# F23-08  a quota is charged when a turn is claimed, not when it succeeds
# ---------------------------------------------------------------------------


def test_F23_08_a_turn_is_charged_before_it_runs() -> None:
    ledger = QuotaLedger()
    limits = Limits(concurrent_turns=99, turns_per_hour=2)
    ledger.claim("alice", limits, running=0)
    ledger.claim("alice", limits, running=0)
    with pytest.raises(QuotaExceeded):
        ledger.claim("alice", limits, running=0)


def test_F23_08_a_failing_turn_is_not_free() -> None:
    """The distinction the whole design turns on.

    Charging on completion means a turn that raises costs nothing, so a retry
    loop against a broken provider is unmetered -- while consuming exactly the
    model calls and subprocesses a successful turn would have.
    """
    ledger = QuotaLedger()
    limits = Limits(turns_per_hour=1)
    ledger.claim("alice", limits, running=0)
    # Nothing reports success or failure to the ledger. There is no such method.
    assert not hasattr(ledger, "complete")
    with pytest.raises(QuotaExceeded):
        ledger.claim("alice", limits, running=0)


def test_F23_08_a_turn_that_never_started_is_refunded() -> None:
    ledger = QuotaLedger()
    limits = Limits(turns_per_hour=1)
    ledger.claim("alice", limits, running=0)
    ledger.refund("alice")
    ledger.claim("alice", limits, running=0)  # the hour is free again


def test_F23_08_the_concurrency_limit_counts_only_this_account() -> None:
    ledger = QuotaLedger()
    limits = Limits(concurrent_turns=1)
    ledger.claim("alice", limits, running=0)
    # bob has one running; alice's own count is what her limit is about.
    ledger.claim("bob", limits, running=0)
    with pytest.raises(QuotaExceeded) as raised:
        ledger.claim("alice", limits, running=1)
    assert raised.value.retry_after > 0


def test_F23_08_the_route_answers_429_with_a_retry_after(
    console: TestClient, workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import asyncio

    from minicodex.web import routes

    async def never(**_kwargs: Any) -> None:
        await asyncio.sleep(3600)

    monkeypatch.setattr(routes, "run_turn", never)
    live = console.app.state.console
    monkeypatch.setattr(
        type(live.accounts.get(CONSOLE_ACCOUNT)),
        "quota_limits",
        lambda self: Limits(concurrent_turns=1, turns_per_hour=99),
    )

    provider = console.get("/api/providers").json()[0]
    console.post(f"/api/providers/{provider['id']}/activate")
    first = _thread(console, workspace)
    second = _thread(console, workspace)
    assert console.post(f"/api/threads/{first}/messages", json={"text": "a"}).status_code == 202
    refused = console.post(f"/api/threads/{second}/messages", json={"text": "b"})
    assert refused.status_code == 429
    assert int(refused.headers["retry-after"]) > 0


def test_F23_08_a_console_with_no_provider_does_not_spend_the_hour(
    console: TestClient, workspace: Path
) -> None:
    """No provider is a 400 and nothing ran, so nothing is charged."""
    thread_id = _thread(console, workspace)
    before = console.get("/api/auth/status").json()["quota"]["used_this_hour"]
    assert console.post(f"/api/threads/{thread_id}/messages", json={"text": "a"}).status_code == 400
    after = console.get("/api/auth/status").json()["quota"]["used_this_hour"]
    assert after == before


# ---------------------------------------------------------------------------
# F23-09  a workspace is any absolute path, which is where the tenancy leaks
# ---------------------------------------------------------------------------


def test_F23_09_an_account_with_roots_cannot_open_a_thread_outside_them(
    console: TestClient, tmp_path: Path
) -> None:
    """Everything else this chapter separates lives under a key. This does not.

    A workspace is a path on the server, and the point of the thing being
    pointed at it is to read files and run commands there.
    """
    allowed = tmp_path / "allowed"
    (allowed / "project").mkdir(parents=True)
    forbidden = tmp_path / "somebody-else"
    forbidden.mkdir()

    bob = _second_account(console, workspace_roots=[str(allowed)])
    ok = bob.post("/api/threads", json={"workspace": str(allowed / "project"), "title": "t"})
    assert ok.status_code == 200
    refused = bob.post("/api/threads", json={"workspace": str(forbidden), "title": "t"})
    assert refused.status_code == 403
    assert str(forbidden) in refused.json()["detail"]


def test_F23_09_a_prefix_that_is_not_a_parent_is_not_inside(
    console: TestClient, tmp_path: Path
) -> None:
    """`/srv/data` must not admit `/srv/data-of-someone-else`.

    A string prefix says yes to that. `Path.is_relative_to` says no, which is
    why the comparison is on paths and not on text.
    """
    root = tmp_path / "data"
    root.mkdir()
    sibling = tmp_path / "data-of-someone-else"
    sibling.mkdir()
    bob = _second_account(console, workspace_roots=[str(root)])
    assert (
        bob.post("/api/threads", json={"workspace": str(sibling), "title": "t"}).status_code == 403
    )


def test_F23_09_no_roots_still_means_unrestricted(console: TestClient, tmp_path: Path) -> None:
    """What a one-operator install is, and this console is still normally that.

    Stated as a test rather than left implicit, because it is the sentence
    somebody will want to check before deciding whether their own deployment is
    safe: an account created without roots may open any directory the server
    process can.
    """
    anywhere = tmp_path / "anywhere"
    anywhere.mkdir()
    assert (
        console.post("/api/threads", json={"workspace": str(anywhere), "title": "t"}).status_code
        == 200
    )


# ---------------------------------------------------------------------------
# passwords, sessions and the first account: decisions rather than faults
# ---------------------------------------------------------------------------


def test_a_stored_hash_carries_the_parameters_it_was_written_with() -> None:
    """Which is what makes raising the cost later a one-line change."""
    encoded = hash_password("a-long-enough-password", n=2**4)
    kind, n, r, p, _salt, _key = encoded.split("$")
    assert (kind, n, r, p) == ("scrypt", "16", "8", "1")
    assert verify_password("a-long-enough-password", encoded)
    assert not verify_password("a-long-enough-passwerd", encoded)


def test_the_password_comparison_is_constant_time(repo_root: Path) -> None:
    """Asserted on the source, and that is the honest place for it.

    `hmac.compare_digest(a, b)` and `a == b` return the same value for every
    input. No behavioural test can tell them apart, and a timing test over a
    32-byte comparison inside CPython measures the interpreter, not the
    comparison -- the mutation probe found exactly this and it is the one
    survivor in chapter 23 that stays a survivor.

    The property is real and is a property of the *text*: this line must use
    the constant-time primitive. So the test reads the line. Chapter 19 already
    does this for a frontend confirmation dialog (F19-11); the shape is the
    same, and its limits are worth naming -- a static assertion cannot tell
    whether the function does what its name says, only that it was called.
    """
    source = (repo_root / "src" / "minicodex" / "web" / "accounts.py").read_text(encoding="utf-8")
    assert "hmac.compare_digest(computed, expected)" in source
    assert "computed == expected" not in source


def test_a_malformed_hash_is_a_failed_login_not_a_crash() -> None:
    assert verify_password("anything", "not-a-hash-at-all") is False
    assert verify_password("anything", "scrypt$$$$$") is False


def test_a_short_password_is_refused(tmp_path: Path) -> None:
    store = AccountStore(tmp_path / "accounts.json")
    with pytest.raises(AccountError) as raised:
        store.create("alice", "x" * (MIN_PASSWORD_LENGTH - 1))
    assert str(MIN_PASSWORD_LENGTH) in str(raised.value)


def test_an_account_key_that_is_not_a_safe_directory_name_is_refused(tmp_path: Path) -> None:
    """The key becomes a path segment under two different trees."""
    store = AccountStore(tmp_path / "accounts.json")
    for bad in ("../escape", "has space", "", "tenants", "x" * 65):
        with pytest.raises(AccountError):
            store.create(bad, "a-long-enough-password")


def test_an_account_key_is_case_folded_rather_than_refused(tmp_path: Path) -> None:
    """Chapter 20 said what to do here, and this is doing it.

    `tenancy._SAFE_KEY` refuses upper case and explains why: two owners are
    separated by the spelling of one path segment, and on NTFS or a default
    macOS volume `Alice` and `alice` are one directory. The comment goes on to
    say that the fix, if the pattern is ever widened, is "to case-fold the key
    before it ever becomes a path, not to trust the filesystem to keep them
    apart". So `create` folds, and the second registration collides on the
    folded name instead of silently sharing a directory with the first.
    """
    store = AccountStore(tmp_path / "accounts.json")
    assert store.create("Alice", "a-long-enough-password").key == "alice"
    with pytest.raises(AccountError) as raised:
        store.create("ALICE", "a-long-enough-password")
    assert "already exists" in str(raised.value)


def test_the_bootstrap_token_is_one_time_and_not_on_disk(tmp_path: Path) -> None:
    store = AccountStore(tmp_path / "accounts.json")
    token = store.issue_bootstrap_token()
    assert token and len(token) > 20
    assert not (tmp_path / "accounts.json").exists()
    store.redeem_bootstrap(token, "alice", "a-long-enough-password")
    with pytest.raises(AccountError):
        store.redeem_bootstrap(token, "eve", "a-long-enough-password")
    # And it is gone from the process, not merely no longer accepted. The
    # second `redeem_bootstrap` above is refused by `self.empty()` either way,
    # so without this line nothing tested the clearing -- the mutation probe
    # found that. A spent credential left in an attribute is one `repr()` or
    # one debug route away from being readable, which is the same argument
    # `sessions.py` makes for storing token hashes in an in-memory table.
    assert store.bootstrap_token is None


def test_a_wrong_bootstrap_token_creates_nothing(tmp_path: Path) -> None:
    store = AccountStore(tmp_path / "accounts.json")
    store.issue_bootstrap_token()
    with pytest.raises(AccountError):
        store.redeem_bootstrap("not-the-token", "eve", "a-long-enough-password")
    assert store.empty()


def test_the_first_account_is_an_admin_and_the_next_one_is_not(console: TestClient) -> None:
    assert console.get("/api/auth/status").json()["account"]["admin"] is True
    _second_account(console)
    accounts = {a["key"]: a for a in console.get("/api/accounts").json()}
    assert accounts["bob"]["admin"] is False
    assert "password_hash" not in accounts["bob"]


def test_an_unreadable_accounts_file_is_not_an_empty_one(tmp_path: Path) -> None:
    """`store.py` degrades a corrupt file to its default; this must not.

    An empty list of threads is a lost session. An empty list of *accounts* is
    a console that decides nobody has registered yet and hands a fresh
    bootstrap token to whoever asks for one.
    """
    path = tmp_path / "accounts.json"
    path.write_text("{ this is not json", encoding="utf-8")
    store = AccountStore(path)
    with pytest.raises(AccountError):
        store.all()


def test_the_session_cookie_is_httponly_and_samesite(anon: TestClient) -> None:
    sign_in(anon)
    header = anon.post(
        "/api/auth/login", json={"key": CONSOLE_ACCOUNT, "password": CONSOLE_PASSWORD}
    ).headers["set-cookie"]
    assert "HttpOnly" in header
    assert "SameSite=lax" in header
    # Not `Secure`, because this request was plain http and a `Secure` cookie
    # on `http://localhost:8000` is dropped by the browser -- a login that
    # appears to succeed and then does not work.
    assert "Secure" not in header


def test_a_session_is_stored_hashed_so_the_table_cannot_leak_one() -> None:
    table = SessionTable()
    token, session = table.create("alice")
    assert token not in repr(table)
    assert session.token_hash != token
    assert table.touch(token) is session


def test_an_expired_session_is_the_same_answer_as_a_forged_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    table = SessionTable()
    token, session = table.create("alice")
    session.last_seen -= 60 * 60 * 24
    assert table.touch(token) is None
    assert table.touch("never-issued") is None
    assert len(table) == 0, "the expired entry is dropped where it is noticed"


def test_pre_chapter_23_records_are_adopted_by_the_first_account(tmp_path: Path) -> None:
    """Four chapters of consoles wrote these at the top of the data directory.

    Leaving them there means the operator's first sign-in shows an empty
    console with all their sessions still on disk one level up, which reads as
    data loss whether or not it is.
    """
    data = tmp_path / "console"
    (data / "sessions" / "old-thread").mkdir(parents=True)
    (data / "threads.json").write_text("[]", encoding="utf-8")
    (data / "providers.json").write_text("[]", encoding="utf-8")

    moved = adopt_legacy_records(data, "alice")
    assert set(moved) == {"threads.json", "providers.json", "sessions"}
    assert (data / "tenants" / "alice" / "threads.json").exists()
    assert (data / "tenants" / "alice" / "sessions" / "old-thread").is_dir()
    assert not (data / "threads.json").exists()


def test_adoption_never_overwrites_what_the_account_already_has(tmp_path: Path) -> None:
    """There is nothing here that knows how to merge two `threads.json` files."""
    data = tmp_path / "console"
    (data / "tenants" / "alice").mkdir(parents=True)
    (data / "tenants" / "alice" / "threads.json").write_text('["mine"]', encoding="utf-8")
    (data / "threads.json").write_text('["older"]', encoding="utf-8")

    assert adopt_legacy_records(data, "alice") == []
    assert (data / "tenants" / "alice" / "threads.json").read_text(encoding="utf-8") == '["mine"]'
    assert (data / "threads.json").exists(), "the older file is left where it was, not deleted"


# ---------------------------------------------------------------------------
# F23-10  a test suite nobody runs is a directory of files
# ---------------------------------------------------------------------------


def test_F23_10_the_frontend_tests_run_in_ci(repo_root: Path) -> None:
    """Chapter 22 wrote four vitest tests and never invoked them.

    They were correct, they passed on a laptop, and `ci.yml` ran
    `npm run typecheck` and `npm run build` and nothing else -- so in the four
    weeks that chapter existed, not one pull request ever executed them.

    This is `test_F_1_05_every_mutation_script_runs_somewhere` (chapter 12) one
    language over, and it is here for the same reason that one is: the Python
    probes have had a mechanism since chapter 12 and it caught this chapter's
    own missing workflow entry within a minute of the file being written. The
    TypeScript side had the prose and not the mechanism.

    Deliberately checking the *runner* rather than each file: `vitest run`
    discovers `**/*.test.ts` itself, so naming individual files here would
    reintroduce the list that goes stale. What has to be true is that the
    runner is invoked at all, and that there is something for it to find.
    """
    suites = sorted(p.name for p in (repo_root / "frontend" / "src").rglob("*.test.ts"))
    assert suites, "no frontend test files; this check has stopped covering anything"

    ci = (repo_root / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    assert "npm --prefix frontend test" in ci, f"nothing in CI runs {suites}"

    package = json.loads((repo_root / "frontend" / "package.json").read_text(encoding="utf-8"))
    assert "vitest" in package["scripts"]["test"]
