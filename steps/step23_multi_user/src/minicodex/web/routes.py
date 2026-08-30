"""The console's HTTP and WebSocket surface.

Deliberately thin. Everything that knows about `minicodex` is in runtime.py,
everything that knows about JSON files is in store.py, and everything that
knows about socket ordering is in channel.py. What is left here is turning a
request into a call on one of those three and its result into JSON.

Two things in this file are load-bearing rather than plumbing, and both are
about a turn being a *background* task that outlives its request:

`_busy` is claimed **before** `create_task`, not inside the coroutine. The
first version tested membership in the handler and added inside `go()`, which
does not start until the handler has already returned -- so two requests
arriving together both passed the check and started two runs on one thread
directory, with two `RolloutWriter`s racing for the same lock and
`resolve("last", ...)` picking whichever landed first.

`_running` keeps the task object, which is what makes a stop button possible at
all, and is also the strong reference `asyncio` does not keep for you.
"""

from __future__ import annotations

import asyncio
import logging
import shlex
from pathlib import Path
from typing import Any

import httpx2
from fastapi import APIRouter, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel

from minicodex.approval import ApprovalReply
from minicodex.mcp_oauth import OwnerTokenStorage
from minicodex.memory import BODY_FILE, SUMMARY_FILE, usage
from minicodex.memory import load as load_memory
from minicodex.memory_jobs import JobStore
from minicodex.policy import APPROVAL_POLICIES, SANDBOX_MODES, policy_tables
from minicodex.remote import RemoteConfigError
from minicodex.rollout import RolloutError, _dump_item, fork, list_sessions, read_rollout, resolve
from minicodex.rules import RuleStore
from minicodex.skills import discover as discover_skills
from minicodex.tools import TOOL_SCHEMAS

from . import oauth as mcp_oauth_flow
from .accounts import MIN_PASSWORD_LENGTH, AccountError
from .auth import AuthContext, clear_session_cookie, require
from .auth import resolve as resolve_auth
from .auth import set_session_cookie as _set_cookie
from .channel import turn_key
from .mcp_config import record_kind, server_config
from .quota import QuotaExceeded
from .runtime import resolve_api_key, run_turn
from .store import DEFAULT_THREAD_SETTINGS, adopt_legacy_records, validate_settings

log = logging.getLogger(__name__)

router = APIRouter()


def _console(request: Request) -> Any:
    """The one `Console` this app was built with. See app.py."""
    return request.app.state.console


def _auth(request: Request) -> AuthContext:
    """Who is calling. Resolved by the middleware; this is the lookup.

    Written as a plain call rather than a `Depends(...)` on every signature,
    which would look more idiomatic and would be the wrong shape: a dependency
    is opt-in, and the whole argument in `auth.py` is that a route which
    forgets to ask must not be a route that answers. Here the gate has already
    run before this module is reached, and this function only fails if the
    route was never behind it.
    """
    return require(request)


def _store(request: Request) -> Any:
    """The caller's records. There is no other store to reach from a route."""
    return _console(request).store_for(_auth(request).owner)


# -- signing in ---------------------------------------------------------------


class Credentials(BaseModel):
    key: str
    password: str


class Bootstrap(Credentials):
    token: str


class NewAccount(Credentials):
    admin: bool = False
    workspace_roots: list[str] = []


class NewPassword(BaseModel):
    current: str
    replacement: str


def _sign_in(request: Request, account: Any) -> JSONResponse:
    """Mint a session and put it in a cookie. The one place that happens."""
    console = _console(request)
    token, _session = console.sessions.create(
        account.key, user_agent=request.headers.get("user-agent", "")
    )
    response = JSONResponse({"account": account.public()})
    _set_cookie(response, token, secure=request.url.scheme == "https")
    return response


@router.get("/api/auth/status")
def auth_status(request: Request) -> dict[str, Any]:
    """Public, and it says as little as it can get away with.

    The frontend has exactly one decision to make before anybody is signed in:
    render the bootstrap form or the login form. So this answers that -- has
    this console got an account yet -- and whether the caller is already
    signed in. It does not list accounts, and `has_account` is a boolean
    rather than a count, because "how many people use this server" is not
    something an anonymous caller has any business learning.

    It resolves the session by hand because it is on `auth.PUBLIC_PATHS`: the
    gate skipped this request, so `request.state.auth` is not set. That is the
    cost of a public route on a deny-by-default server, and it is a cost worth
    seeing rather than hiding behind a helper that works either way.
    """
    console = _console(request)
    auth = resolve_auth(
        request.scope.get("headers") or [], console.accounts, console.sessions, console.home
    )
    if auth is None:
        return {"has_account": not console.accounts.empty(), "signed_in": False, "account": None}
    return {
        "has_account": True,
        "signed_in": True,
        "account": auth.account.public(),
        "quota": console.quota.snapshot(
            auth.account.key,
            auth.account.quota_limits(),
            running=console.running_for(auth.account.key),
        ),
    }


@router.post("/api/auth/bootstrap")
def bootstrap(request: Request, body: Bootstrap) -> JSONResponse:
    """Redeem the token the server printed and become the first account."""
    console = _console(request)
    try:
        account = console.accounts.redeem_bootstrap(body.token, body.key, body.password)
    except AccountError as exc:
        raise HTTPException(400, str(exc)) from exc
    moved = adopt_legacy_records(console.data_dir, account.key)
    if moved:
        log.info("adopted pre-chapter-23 records into %s: %s", account.key, ", ".join(moved))
    return _sign_in(request, account)


@router.post("/api/auth/login")
def login(request: Request, body: Credentials) -> JSONResponse:
    """One message for every way this can fail.

    Unknown account, wrong password and disabled account are the same 401 with
    the same text. `AccountStore.verify` has already made them cost the same
    number of milliseconds; saying "no such account" here would give back for
    free what that was paying for.
    """
    console = _console(request)
    account = console.accounts.verify(body.key, body.password)
    if account is None:
        raise HTTPException(401, "that account name and password do not match")
    return _sign_in(request, account)


@router.post("/api/auth/logout")
def logout(request: Request) -> JSONResponse:
    auth = _auth(request)
    _console(request).sessions.revoke(auth.token)
    response = JSONResponse({"ok": True})
    clear_session_cookie(response)
    return response


@router.get("/api/auth/sessions")
def list_sessions_for_me(request: Request) -> list[dict[str, Any]]:
    auth = _auth(request)
    console = _console(request)
    return [
        session.public(current=session.id == auth.session.id)
        for session in console.sessions.sessions_for(auth.account.key)
    ]


@router.delete("/api/auth/sessions/{session_id}")
def revoke_one_session(request: Request, session_id: str) -> dict[str, bool]:
    auth = _auth(request)
    if not _console(request).sessions.revoke_id(auth.account.key, session_id):
        raise HTTPException(404, "no such session")
    return {"ok": True}


def _revoke_everything(console: Any, account_key: str) -> dict[str, int]:
    """Sign an account out everywhere, and stop what it left running.

    Revoking the sessions alone is the version that looks finished and is not:
    a turn is a background task that outlived the request which started it, so
    after "sign out everywhere" the agent carries on editing files and running
    commands on behalf of an account that no longer has a single valid
    credential. Nobody is watching the socket, which is exactly why it is
    worse rather than better.

    Cancelling is the same call the stop button makes, so the turn ends the way
    chapter 7 promises: `mark("interrupted")`, a resumable session file.
    """
    sessions = console.sessions.revoke_account(account_key)
    prefix = f"{account_key}/"
    cancelled = 0
    for key, task in list(console.running.items()):
        if key.startswith(prefix) and not task.done():
            task.cancel()
            cancelled += 1
    console.broker_for(account_key).fail_all("signed out")
    return {"sessions": sessions, "turns": cancelled}


@router.post("/api/auth/sessions/revoke-all")
def revoke_all_sessions(request: Request) -> JSONResponse:
    auth = _auth(request)
    counts = _revoke_everything(_console(request), auth.account.key)
    response = JSONResponse(counts)
    clear_session_cookie(response)
    return response


@router.post("/api/auth/password")
def change_password(request: Request, body: NewPassword) -> JSONResponse:
    """Change it, then end every session including this one, then sign in.

    A password change that leaves the old sessions alive is a password change
    that does not evict whoever the change was *for*. So: revoke everything,
    mint one new session for the browser that asked, and let every other device
    log in again with the new password.
    """
    auth = _auth(request)
    console = _console(request)
    if console.accounts.verify(auth.account.key, body.current) is None:
        raise HTTPException(403, "the current password does not match")
    if len(body.replacement) < MIN_PASSWORD_LENGTH:
        raise HTTPException(400, f"a password needs at least {MIN_PASSWORD_LENGTH} characters")
    try:
        console.accounts.set_password(auth.account.key, body.replacement)
    except AccountError as exc:
        raise HTTPException(400, str(exc)) from exc
    _revoke_everything(console, auth.account.key)
    account = console.accounts.get(auth.account.key)
    return _sign_in(request, account)


# -- accounts, for whoever bootstrapped the console ---------------------------


def _admin(request: Request) -> AuthContext:
    auth = _auth(request)
    if not auth.account.admin:
        raise HTTPException(403, "this needs an administrator account")
    return auth


@router.get("/api/accounts")
def list_accounts(request: Request) -> list[dict[str, Any]]:
    _admin(request)
    return [a.public() for a in _console(request).accounts.all()]


@router.post("/api/accounts")
def create_account(request: Request, body: NewAccount) -> dict[str, Any]:
    _admin(request)
    roots = tuple(str(Path(r).expanduser().resolve()) for r in body.workspace_roots)
    try:
        account = _console(request).accounts.create(
            body.key, body.password, admin=body.admin, workspace_roots=roots
        )
    except AccountError as exc:
        raise HTTPException(400, str(exc)) from exc
    return account.public()


@router.post("/api/accounts/{key}/disable")
def disable_account(request: Request, key: str) -> dict[str, Any]:
    """Disable, never delete.

    There is no delete route, and the absence is deliberate: an account key is
    a directory name under two different trees, one of which holds OAuth
    tokens for other people's servers. Removing the row while the directories
    stand would let the same key be handed out again and inherit them. A
    disabled account cannot sign in, its live sessions are revoked here, and
    what it owns stays where it is until somebody decides on purpose what to
    do with it.
    """
    admin = _admin(request)
    if key == admin.account.key:
        raise HTTPException(400, "an administrator cannot disable their own account")
    console = _console(request)
    account = console.accounts.update(key, disabled=True)
    if account is None:
        raise HTTPException(404, f"no account {key!r}")
    _revoke_everything(console, key)
    return account.public()


# -- policy / tools: what the core supports, so the UI never invents a value --


@router.get("/api/policy")
def get_policy() -> dict[str, Any]:
    return {
        "sandbox_modes": list(SANDBOX_MODES),
        "approval_policies": list(APPROVAL_POLICIES),
        "defaults": dict(DEFAULT_THREAD_SETTINGS),
        "tables": policy_tables(),
    }


@router.get("/api/tools")
def get_tools() -> list[dict[str, str]]:
    """Informational: minicodex has no per-tool switch, so neither does this."""
    return [
        {
            "name": t["function"]["name"],
            "description": (t["function"].get("description") or "").split("\n")[0],
        }
        for t in TOOL_SCHEMAS
    ]


# -- workspaces and threads --------------------------------------------------


class NewThread(BaseModel):
    workspace: str
    title: str = "New session"
    settings: dict[str, Any] = {}


class ThreadPatch(BaseModel):
    title: str | None = None
    settings: dict[str, Any] | None = None


def _validated_workspace(raw: str, account: Any) -> str:
    """An absolute path that exists, and that this account may open.

    `run_turn` does `root.mkdir(parents=True, exist_ok=True)` because the agent
    has to be able to work in a directory that is being created around it. That
    is the wrong behaviour for *this* string, which is a person typing a path
    into a browser: a typo used to silently create a directory tree anywhere
    the server user could write, and then run an agent inside it.

    The last check is chapter 23's, and it is the one that stops the rest of
    the chapter from being decorative. Everything else this chapter separated
    -- records, tokens, memories, approvals -- lives under a key. A workspace
    does not: it is any absolute path on the server, and the entire purpose of
    the thing being pointed at it is to read files and run commands there. Two
    accounts with unrestricted workspaces are two accounts that can each open a
    thread on the other's home directory, and no amount of tenancy underneath
    changes that.

    Empty `workspace_roots` still means unrestricted, because that is what a
    one-operator install is and this console is still normally that. It becomes
    a decision the operator makes when they create the second account, which is
    why `POST /api/accounts` takes the roots and why they are shown on the
    account page rather than kept in a config file.
    """
    text = raw.strip()
    if not text:
        raise HTTPException(400, "workspace path cannot be empty")
    path = Path(text).expanduser()
    if not path.is_absolute():
        raise HTTPException(400, "workspace must be an absolute path")
    if not path.exists():
        raise HTTPException(400, f"no such directory: {path}")
    if not path.is_dir():
        raise HTTPException(400, f"not a directory: {path}")
    resolved = path.resolve()
    roots = getattr(account, "workspace_roots", ()) or ()
    if roots and not any(_within(resolved, Path(root)) for root in roots):
        raise HTTPException(
            403,
            f"{resolved} is outside the directories this account may use: " + ", ".join(roots),
        )
    return str(resolved)


def _within(path: Path, root: Path) -> bool:
    """Is `path` inside `root`, with both resolved first.

    `Path.is_relative_to` rather than a string prefix, which says yes to
    `/srv/data-of-someone-else` for the root `/srv/data`. Both sides are
    resolved before the comparison, so a symlink pointing out of the root is
    followed first rather than after -- chapter 20 (F20-06) learned the same
    thing about a sandbox's writable roots.
    """
    try:
        return path.resolve().is_relative_to(root.expanduser().resolve())
    except (OSError, ValueError):
        return False


@router.get("/api/workspaces")
def get_workspaces(request: Request) -> list[dict[str, Any]]:
    console = _console(request)
    auth = _auth(request)
    store = console.store_for(auth.owner)
    groups = store.workspaces()
    for group in groups:
        for thread in group["threads"]:
            sessions = list_sessions(store.thread_dir(thread["id"]))
            thread["message_count"] = len(sessions[0].items) if sessions else 0
            thread["model"] = sessions[0].meta.model if sessions else None
            thread["busy"] = turn_key(auth.account.key, thread["id"]) in console.busy
    return groups


@router.post("/api/threads")
def create_thread(request: Request, body: NewThread) -> dict[str, Any]:
    auth = _auth(request)
    workspace = _validated_workspace(body.workspace, auth.account)
    try:
        settings = validate_settings(body.settings)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    store = _console(request).store_for(auth.owner)
    return store.new_thread(workspace, body.title.strip() or "New session", settings)


@router.get("/api/threads/{thread_id}")
def get_thread(request: Request, thread_id: str) -> dict[str, Any]:
    console = _console(request)
    auth = _auth(request)
    record = console.require_thread(auth.owner, thread_id)
    directory = console.store_for(auth.owner).thread_dir(thread_id)
    items: list[dict[str, Any]] = []
    marks: list[dict[str, Any]] = []
    try:
        loaded = read_rollout(resolve("last", directory))
    except RolloutError:
        pass  # no turns yet
    else:
        history, _dropped = loaded.history()
        items = [_dump_item(item) for item in history.items]
        marks = list(getattr(loaded, "marks", []) or [])
    return {
        **record,
        "items": items,
        "marks": marks,
        "busy": turn_key(auth.account.key, thread_id) in console.busy,
        "turns": len(list_sessions(directory)),
    }


@router.patch("/api/threads/{thread_id}")
def patch_thread(request: Request, thread_id: str, body: ThreadPatch) -> dict[str, Any]:
    console = _console(request)
    auth = _auth(request)
    record = console.require_thread(auth.owner, thread_id)
    changes: dict[str, Any] = {}
    if body.title is not None and body.title.strip():
        changes["title"] = body.title.strip()
    if body.settings is not None:
        if turn_key(auth.account.key, thread_id) in console.busy:
            raise HTTPException(409, "cannot change settings while this thread is answering")
        try:
            changes["settings"] = validate_settings({**record["settings"], **body.settings})
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
    if not changes:
        return record
    updated = console.store_for(auth.owner).update("threads.json", thread_id, **changes)
    assert updated is not None
    return updated


@router.delete("/api/threads/{thread_id}")
def delete_thread(request: Request, thread_id: str) -> dict[str, bool]:
    console = _console(request)
    auth = _auth(request)
    if turn_key(auth.account.key, thread_id) in console.busy:
        raise HTTPException(409, "cannot delete a thread while it is answering")
    return {"ok": console.store_for(auth.owner).delete("threads.json", thread_id)}


@router.get("/api/threads/{thread_id}/sessions")
def thread_sessions(request: Request, thread_id: str) -> list[dict[str, Any]]:
    """Every rollout file this thread has written, newest first.

    A thread is a directory of sessions, not one file: every turn writes a new
    one and resumes from the last (chapter 7). This is what the fork menu picks
    from.
    """
    console = _console(request)
    auth = _auth(request)
    console.require_thread(auth.owner, thread_id)
    return [
        {
            "session_id": s.meta.session_id,
            "created": s.meta.created,
            "model": s.meta.model,
            "messages": len(s.items),
            "forked_from": s.meta.forked_from,
        }
        for s in list_sessions(console.store_for(auth.owner).thread_dir(thread_id))
    ]


class ForkBody(BaseModel):
    session: str = "last"
    upto: int | None = None
    title: str | None = None


@router.post("/api/threads/{thread_id}/fork")
def fork_thread(request: Request, thread_id: str, body: ForkBody) -> dict[str, Any]:
    """Copy a prefix of this thread's history into a brand new thread.

    `rollout.fork` is chapter 7's, unchanged; all this adds is that the copy
    lands in a *different* thread directory, so the original is still there to
    go back to.
    """
    console = _console(request)
    auth = _auth(request)
    record = console.require_thread(auth.owner, thread_id)
    store = console.store_for(auth.owner)
    source_dir = store.thread_dir(thread_id)
    fresh = store.new_thread(
        record["workspace"],
        body.title or f"{record['title']} (fork)",
        record["settings"],
    )
    try:
        path = fork(body.session, upto=body.upto, directory=source_dir)
    except RolloutError as exc:
        store.delete("threads.json", fresh["id"])
        raise HTTPException(400, str(exc)) from exc
    target = store.thread_dir(fresh["id"]) / path.name
    target.write_bytes(path.read_bytes())
    path.unlink()
    return fresh


# -- sending, and stopping ---------------------------------------------------


class SendMessage(BaseModel):
    text: str


@router.post("/api/threads/{thread_id}/messages", status_code=202)
async def send_message(request: Request, thread_id: str, body: SendMessage) -> dict[str, Any]:
    console = _console(request)
    auth = _auth(request)
    record = console.require_thread(auth.owner, thread_id)
    store = console.store_for(auth.owner)
    if not body.text.strip():
        raise HTTPException(400, "message cannot be empty")

    key = turn_key(auth.account.key, thread_id)

    # Claimed here, synchronously, before anything can await. See this module's
    # docstring: doing it inside the task is the race.
    if key in console.busy:
        raise HTTPException(409, "this thread is still answering the previous message")

    # And the quota in the same synchronous stretch, for the same reason: two
    # requests that both awaited before charging would both be charged after
    # both had started. `running_for` counts `busy`, which this handler has not
    # added to yet, so the count is of turns other than this one.
    try:
        console.quota.claim(
            auth.account.key,
            auth.account.quota_limits(),
            running=console.running_for(auth.account.key),
        )
    except QuotaExceeded as exc:
        raise HTTPException(429, str(exc), headers={"Retry-After": str(exc.retry_after)}) from exc
    console.busy.add(key)

    provider = store.active_provider()
    if provider is None:
        console.busy.discard(key)
        # Refunded: nothing ran. A 400 for a console that was never configured
        # must not spend the hour it would have taken to answer.
        console.quota.refund(auth.account.key)
        raise HTTPException(400, "no active model provider -- add one under Extensions")

    servers = [s for s in store.all("mcp.json") if s.get("enabled", True)]
    emit = console.channel.emitter(key)
    emit({"type": "turn_started", "text": body.text})

    async def go() -> None:
        try:
            await run_turn(
                thread_dir=store.thread_dir(thread_id),
                workspace_root=Path(record["workspace"]),
                question=body.text,
                settings=record["settings"],
                provider_name=provider["provider"],
                base_url=provider["base_url"],
                model=provider["model"],
                api_key=resolve_api_key(provider),
                mcp_servers=servers,
                broker=console.broker_for(auth.account.key),
                owner=auth.owner,
                emit=emit,
            )
        except asyncio.CancelledError:
            pass  # the stop button; run_turn already said so and marked the file
        except Exception as exc:
            # `ModelFailed` has already emitted its own, more specific line from
            # inside run_turn. This is the catch-all for everything else -- a
            # bad MCP command, a permission error on the workspace -- so that a
            # thread never simply goes quiet on the browser.
            emit({"type": "error", "text": f"{type(exc).__name__}: {exc}"})
        finally:
            console.busy.discard(key)
            console.running.pop(key, None)
            emit({"type": "turn_finished"})

    console.running[key] = asyncio.get_running_loop().create_task(go())
    return {"accepted": True}


@router.post("/api/threads/{thread_id}/cancel")
async def cancel_turn(request: Request, thread_id: str) -> dict[str, bool]:
    console = _console(request)
    auth = _auth(request)
    console.require_thread(auth.owner, thread_id)
    task = console.running.get(turn_key(auth.account.key, thread_id))
    if task is None or task.done():
        raise HTTPException(409, "this thread is not answering anything")
    task.cancel()
    return {"ok": True}


@router.websocket("/ws/threads/{thread_id}")
async def thread_socket(ws: WebSocket, thread_id: str) -> None:
    """The event stream, behind the same gate as everything else.

    `AuthMiddleware` has already refused an unauthenticated or cross-origin
    handshake before this coroutine exists, which is why there is no cookie
    parsing here. What is left is the question the gate cannot answer: this
    caller is signed in, but is this *their* thread. `require_thread` looks it
    up in their own store, so the answer is a lookup rather than a comparison.

    The check has to happen before `accept()`. Accepting first and closing
    after tells the browser it was connected, and a client written against
    that will retry a socket it was never allowed to have.
    """
    console = ws.app.state.console
    auth = ws.scope["state"]["auth"]
    if console.store_for(auth.owner).thread(thread_id) is None:
        await ws.close(code=1008, reason="no such thread")
        return
    key = turn_key(auth.account.key, thread_id)
    await ws.accept()
    console.channel.subscribe(key, ws)
    try:
        while True:
            # The console never sends anything over this socket. The loop is
            # here to notice a close: `receive_text` raises on disconnect.
            await ws.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        console.channel.unsubscribe(key, ws)


# -- approvals ---------------------------------------------------------------


class ApprovalResponse(BaseModel):
    approved: bool
    command: str | None = None
    remember: str | None = None  # "session" | "project" | None


@router.post("/api/approvals/{approval_id}/respond")
async def respond_to_approval(
    request: Request, approval_id: str, body: ApprovalResponse
) -> dict[str, bool]:
    """`async def` on purpose -- see approver.py.

    A `def` route runs in an anyio worker thread, and resolving an
    `asyncio.Future` from a thread that is not the loop's is a race the loop
    usually wins and occasionally does not. The broker also goes through
    `call_soon_threadsafe`, but the route being `async` is what makes that the
    belt rather than the braces.
    """
    console = _console(request)
    auth = _auth(request)
    if body.remember not in (None, "session", "project"):
        raise HTTPException(400, f"unknown remember scope {body.remember!r}")
    reply = ApprovalReply(
        approved=body.approved,
        command=body.command or "",
        remember=body.remember,  # type: ignore[arg-type]
    )
    if not console.broker_for(auth.account.key).resolve(approval_id, reply):
        raise HTTPException(404, "no pending approval with that id (it may have timed out)")
    return {"ok": True}


# -- remembered rules --------------------------------------------------------


def _rules(request: Request, thread_id: str) -> RuleStore:
    """Rules stay in the workspace, and that is right rather than an oversight.

    A remembered "always allow `pytest`" is a fact about a checkout, not about
    a person -- chapter 5 put it in `<workspace>/.minicodex/rules.json` so the
    CLI and the console would find the same one, and two accounts working in
    the same directory are two people who have agreed the same thing about the
    same repository. What chapter 23 adds is only that you have to own the
    thread to get here at all.
    """
    auth = _auth(request)
    record = _console(request).require_thread(auth.owner, thread_id)
    return RuleStore(Path(record["workspace"]) / ".minicodex" / "rules.json")


@router.get("/api/threads/{thread_id}/rules")
def get_rules(request: Request, thread_id: str) -> list[dict[str, Any]]:
    """What "always allow" has actually agreed to, so it can be taken back.

    Only project-scoped rules survive a process, so this lists what is on disk
    for the workspace. A rule that can only be revoked by finding and editing a
    JSON file is a rule that stays -- chapter 5 says so about the CLI's
    `minicodex forget N`, and it is no less true with a browser in front of it.
    """
    store = _rules(request, thread_id)
    return [
        {
            "index": i,
            "words": list(rule.words),
            "scope": rule.scope,
            "prompted_by": rule.prompted_by,
            "created_at": rule.created_at,
            "describe": rule.describe(),
        }
        for i, rule in enumerate(store.all())
    ]


@router.delete("/api/threads/{thread_id}/rules/{index}")
def revoke_rule(request: Request, thread_id: str, index: int) -> dict[str, str]:
    store = _rules(request, thread_id)
    try:
        rule = store.forget(index)
    except IndexError as exc:
        raise HTTPException(404, f"no rule {index}") from exc
    return {"revoked": " ".join(rule.words)}


# -- memory ------------------------------------------------------------------


@router.get("/api/threads/{thread_id}/memory")
def get_memory(request: Request, thread_id: str) -> dict[str, Any]:
    """The memory this thread's turns actually read (F23-05).

    For four chapters this route answered with `<workspace>/.minicodex/
    memories`, and no turn has ever read that directory: chapter 16 moved
    memory to the home directory, and `runtime.load_feature_dirs` has loaded it
    from there since chapter 19. So the panel showed an empty tab on a machine
    with a full memory file, and "forget everything" deleted nothing, and both
    were silent about it.

    `owner.memories()` is now the only spelling in this file, and `run_turn`
    takes the same `Owner`, which is what makes the two agree by construction
    rather than by both being edited on the same day.
    """
    auth = _auth(request)
    _console(request).require_thread(auth.owner, thread_id)
    directory = auth.owner.memories()
    try:
        memory = load_memory(directory)
    except Exception as exc:
        return {"directory": str(directory), "error": str(exc), "entries": []}
    counts = usage(directory)
    return {
        "directory": str(directory),
        "summary": memory.summary,
        "describe": memory.describe(),
        "empty_reason": memory.empty_reason,
        "files": {"body": BODY_FILE, "summary": SUMMARY_FILE},
        "entries": [
            {
                "id": entry.entry_id,
                "title": entry.title,
                "headline": entry.headline(),
                "lines": entry.lines,
                "usage": counts.get(entry.entry_id),
            }
            for entry in memory.entries
        ],
    }


@router.get("/api/threads/{thread_id}/memory/jobs")
def get_memory_jobs(request: Request, thread_id: str) -> dict[str, Any]:
    auth = _auth(request)
    _console(request).require_thread(auth.owner, thread_id)
    path = auth.owner.jobs_db()
    if not path.exists():
        return {"counts": {}, "rows": []}
    with JobStore(path) as jobs:
        return {"counts": jobs.counts(), "rows": jobs.rows()}


@router.post("/api/threads/{thread_id}/memory/forget-all")
def forget_all_memory(request: Request, thread_id: str) -> dict[str, Any]:
    """The one-click reset, and it is not optional.

    codex ships `Reset all memories?` next to `Enable memories?` in the same
    dialog, and the reason is the same reason this console asks twice before
    switching writing on: a feature that accumulates what it learned about you
    has to come with a way to end that, in the place you turned it on.
    """
    auth = _auth(request)
    _console(request).require_thread(auth.owner, thread_id)
    directory = auth.owner.memories()
    removed = []
    for name in (BODY_FILE, SUMMARY_FILE, "usage.json"):
        target = directory / name
        if target.exists():
            target.unlink()
            removed.append(name)
    jobs_path = auth.owner.jobs_db()
    if jobs_path.exists():
        with JobStore(jobs_path) as jobs:
            jobs.forget_all()
    return {"removed": removed}


# -- skills ------------------------------------------------------------------


@router.get("/api/threads/{thread_id}/skills")
def get_skills(request: Request, thread_id: str) -> dict[str, Any]:
    """Real, as of chapter 18. This tab used to say the subsystem did not exist.

    Workspace-relative, and unlike the memory route above that is correct:
    skills stayed per-project when memory moved to the home directory, so this
    is the directory `load_feature_dirs` reads. The two panels were written
    side by side with the same path shape and only one of them was right.
    """
    auth = _auth(request)
    record = _console(request).require_thread(auth.owner, thread_id)
    directory = Path(record["workspace"]) / ".minicodex" / "skills"
    found = discover_skills(directory)
    return {
        "supported": True,
        "directory": str(directory),
        "describe": found.describe(),
        "skipped": list(found.skipped),
        "items": [
            {
                "name": skill.name,
                "description": skill.description,
                "body": skill.body,
                "path": str(skill.path),
                "catalog_line": skill.catalog_line(),
            }
            for skill in found.skills
        ],
    }


@router.get("/api/plugins")
def get_plugins() -> dict[str, Any]:
    """Still not real, and saying so is the point.

    codex has a plugin/bundle mechanism; minicodex has no module for one. An
    empty list would let the tab imply it had looked and found nothing.
    """
    return {"supported": False, "items": []}


# -- providers ---------------------------------------------------------------


class ProviderIn(BaseModel):
    name: str
    provider: str = "openai"
    base_url: str
    model: str
    api_key: str | None = None


def _public(provider: dict[str, Any]) -> dict[str, Any]:
    """A key never leaves the process, not even to the browser that typed it."""
    return {**provider, "api_key": bool(provider.get("api_key"))}


@router.get("/api/providers")
def list_providers(request: Request) -> list[dict[str, Any]]:
    return [_public(p) for p in _store(request).providers()]


@router.post("/api/providers")
def add_provider(request: Request, body: ProviderIn) -> dict[str, Any]:
    if body.provider not in ("openai", "ollama"):
        raise HTTPException(400, f"unknown provider kind {body.provider!r}")
    record = _store(request).add("providers.json", {**body.model_dump(), "active": False})
    return _public(record)


@router.post("/api/providers/{provider_id}/activate")
def activate_provider(request: Request, provider_id: str) -> dict[str, bool]:
    if not _store(request).activate_provider(provider_id):
        raise HTTPException(404, "no such provider")
    return {"ok": True}


@router.delete("/api/providers/{provider_id}")
def delete_provider(request: Request, provider_id: str) -> dict[str, bool]:
    return {"ok": _store(request).delete("providers.json", provider_id)}


# -- MCP servers -------------------------------------------------------------
#
# Two shapes behind one record, the same split `mcp.load_config` already reads
# from a config file (F21-01's dual validation, done again here because a
# library rejecting a bad shape raises for the caller and a web form rejecting
# one has to become a sentence a person reads -- F22-04): a stdio server has a
# `command`; a remote one has a `url` and one of two authentication paths,
# a bearer token or OAuth, never both.


class McpIn(BaseModel):
    name: str
    # stdio
    command: str | None = None  # shell-style; split with shlex, see below
    env: dict[str, str] = {}
    cwd: str | None = None
    # remote
    url: str | None = None
    bearer_token: str | None = None
    http_headers: dict[str, str] = {}
    env_http_headers: dict[str, str] = {}
    oauth: bool = False
    oauth_client_id: str | None = None
    oauth_scope: str | None = None
    # both
    startup_timeout: float = 30.0
    tool_timeout: float = 60.0


def _public_mcp(record: dict[str, Any]) -> dict[str, Any]:
    """A bearer token never leaves the process, not even to the browser that
    typed it in -- `_public` (below, for providers) drew this line first."""
    if record.get("bearer_token"):
        return {**record, "bearer_token": True}
    return record


@router.get("/api/mcp")
def list_mcp(request: Request) -> list[dict[str, Any]]:
    return [_public_mcp(r) for r in _store(request).all("mcp.json")]


@router.post("/api/mcp")
def add_mcp(request: Request, body: McpIn) -> dict[str, Any]:
    has_command = bool(body.command and body.command.strip())
    has_url = bool(body.url and body.url.strip())
    if has_command and has_url:
        raise HTTPException(
            400, "a server is reached one way or the other -- set 'command' or 'url', not both"
        )
    if not has_command and not has_url:
        raise HTTPException(400, "give this server either a command (stdio) or a url (remote)")
    if body.startup_timeout <= 0 or body.tool_timeout <= 0:
        raise HTTPException(400, "timeouts must be positive")

    if has_command:
        assert body.command is not None
        command = shlex.split(body.command)
        if not command:
            raise HTTPException(400, "command cannot be empty")
        record = {
            "kind": "stdio",
            "name": body.name,
            "command": command,
            "env": body.env,
            "cwd": body.cwd,
            "startup_timeout": body.startup_timeout,
            "tool_timeout": body.tool_timeout,
            "enabled": True,
        }
    else:
        if body.bearer_token and body.oauth:
            raise HTTPException(
                400,
                "a server is authenticated one way or the other -- "
                "a bearer token or OAuth, not both",
            )
        record = {
            "kind": "remote",
            "name": body.name,
            "url": body.url,
            "bearer_token": body.bearer_token or None,
            "http_headers": body.http_headers,
            "env_http_headers": body.env_http_headers,
            "oauth": (
                {"client_id": body.oauth_client_id or None, "scope": body.oauth_scope or None}
                if body.oauth
                else None
            ),
            "startup_timeout": body.startup_timeout,
            "tool_timeout": body.tool_timeout,
            "enabled": True,
        }
    return _public_mcp(_store(request).add("mcp.json", record))


class McpToggle(BaseModel):
    enabled: bool


@router.post("/api/mcp/{server_id}/toggle")
def toggle_mcp(request: Request, server_id: str, body: McpToggle) -> dict[str, Any]:
    record = _store(request).update("mcp.json", server_id, enabled=body.enabled)
    if record is None:
        raise HTTPException(404, "no such server")
    return _public_mcp(record)


@router.delete("/api/mcp/{server_id}")
def delete_mcp(request: Request, server_id: str) -> dict[str, bool]:
    return {"ok": _store(request).delete("mcp.json", server_id)}


# -- MCP OAuth: the console's own architecture, not chapter 21's -------------
#
# See `web/oauth.py`'s module docstring for why chapter 21's `redirect_handler`
# /`callback_handler` do not carry over (F22-01). The short version lives here
# too: those two assume the agent process and the browser share a machine, and
# a server-hosted console cannot assume that, so the callback has to land on
# one of *this console's own* routes instead of a loopback port, correlated to
# a pending attempt by a server-generated `state` (F22-02) that expires if
# nobody ever finishes authorizing (F22-03).


def _mcp_record(request: Request, server_id: str) -> dict[str, Any]:
    record = _store(request).get("mcp.json", server_id)
    if record is None:
        raise HTTPException(404, "no such server")
    if record_kind(record) != "remote":
        raise HTTPException(400, f"{record['name']} is a local command, not a remote server")
    return record


class OAuthStartIn(BaseModel):
    server_id: str


@router.post("/api/mcp/oauth/start")
async def start_mcp_oauth(request: Request, body: OAuthStartIn) -> dict[str, str]:
    console = _console(request)
    auth = _auth(request)
    record = _mcp_record(request, body.server_id)
    try:
        config = server_config(record)
    except RemoteConfigError as exc:
        raise HTTPException(400, str(exc)) from exc
    if config.oauth is None:
        raise HTTPException(
            400, f"{record['name']} is not set up for OAuth -- enable it when adding the server"
        )
    # Reflects the Host the browser is actually using rather than a
    # configured "public URL" this console does not otherwise need to know,
    # so the same record works unchanged in local dev and behind a reachable
    # host -- whatever answered this request is where the authorization
    # server's redirect has to land too.
    redirect_uri = str(request.base_url) + "api/mcp/oauth/callback"
    try:
        authorize_url, pending = await mcp_oauth_flow.begin(
            config, server_id=record["id"], redirect_uri=redirect_uri, owner=auth.owner
        )
    except (
        mcp_oauth_flow.OAuthFlowError,
        mcp_oauth_flow.OAuthRegistrationError,
        mcp_oauth_flow.OAuthTokenError,
        httpx2.HTTPError,
    ) as exc:
        raise HTTPException(400, f"could not start OAuth for {record['name']}: {exc}") from exc
    console.mcp_oauth.put(pending)
    return {"authorize_url": authorize_url}


_OAUTH_RESULT_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<style>
  body {{ font: 15px/1.6 ui-sans-serif, system-ui, sans-serif; max-width: 32rem;
         margin: 16vh auto; padding: 0 1.5rem; color: #10182B; background: #EDF1FA;
         text-align: center; }}
  h1 {{ font-size: 1.25rem; }}
  @media (prefers-color-scheme: dark) {{ body {{ color: #EAF0FF; background: #0A1120; }} }}
</style></head>
<body><h1>{title}</h1><p>{message}</p><p>You can close this tab.</p></body></html>"""


def _oauth_result_page(*, ok: bool, message: str) -> HTMLResponse:
    title = "Connected" if ok else "Connection failed"
    return HTMLResponse(_OAUTH_RESULT_PAGE.format(title=title, message=message))


@router.get("/api/mcp/oauth/callback")
async def mcp_oauth_callback(
    request: Request,
    state: str | None = None,
    code: str | None = None,
    error: str | None = None,
    error_description: str | None = None,
    iss: str | None = None,
) -> HTMLResponse:
    console = _console(request)
    auth = _auth(request)
    # `state` is looked up, never trusted (F22-02): an unknown, expired or
    # missing value gets the same answer as a value that was never issued at
    # all, and nothing past this line reads anything else the query string
    # says about *which* server this is.
    pending = console.mcp_oauth.pop(state) if state else None
    if pending is None:
        return _oauth_result_page(
            ok=False, message="this authorization link is unknown or has expired"
        )
    # Two independent facts, kept independent. The cookie decided whether this
    # request is answered at all; the pending entry decides whose token this
    # is. They have to agree, and when they do not the answer is the same one
    # an unknown `state` gets -- a redirect that arrives in somebody else's
    # browser must not finish somebody else's connection, however it got there.
    if pending.owner != auth.owner:
        return _oauth_result_page(
            ok=False, message="this authorization link is unknown or has expired"
        )
    if error is not None:
        reason = error_description or error
        return _oauth_result_page(
            ok=False, message=f"{pending.server_name}: authorization was not granted ({reason})"
        )
    if not code:
        return _oauth_result_page(
            ok=False, message=f"{pending.server_name}: no authorization code was returned"
        )
    try:
        await mcp_oauth_flow.finish(pending, code=code, iss=iss)
    except (
        mcp_oauth_flow.OAuthFlowError,
        mcp_oauth_flow.OAuthTokenError,
        httpx2.HTTPError,
    ) as exc:
        return _oauth_result_page(ok=False, message=f"{pending.server_name}: {exc}")
    return _oauth_result_page(ok=True, message=f"connected to {pending.server_name}")


@router.get("/api/mcp/{server_id}/oauth/status")
async def mcp_oauth_status(request: Request, server_id: str) -> dict[str, Any]:
    """Whether this server has a stored token -- what the console polls after
    opening the authorize URL in a new tab, instead of depending on that tab
    successfully messaging back to a window it may not still have a handle
    to (a popup blocker, `noopener`, or the tab simply being closed early all
    break a `postMessage`-based handshake; polling a status a person can also
    just reload the page and still see does not)."""
    auth = _auth(request)
    record = _mcp_record(request, server_id)
    storage = OwnerTokenStorage(auth.owner, server=record["name"])
    tokens = await storage.get_tokens()
    return {"connected": tokens is not None}
