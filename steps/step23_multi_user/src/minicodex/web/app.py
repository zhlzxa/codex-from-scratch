"""Assemble the console: accounts, sessions, one store per owner, one router.

A `Console` object rather than module-level globals, for the reason every other
composition root in this package exists (`composition.py`, interlude B): state
that is created once per process and reached for by name is state that cannot
be created twice in one interpreter, which means it cannot be tested. Every
route reaches its dependencies through `request.app.state.console`, so a test
builds an app on a `tmp_path` and gets a whole console with nothing shared.

Chapter 23 changed what "nothing shared" means inside one process. Three
things that were single objects are now one per account:

* the record store, so there is no table with everybody's threads in it;
* the approval broker, so `POST /api/approvals/{id}/respond` cannot reach an
  approval that belongs to somebody else;
* the event channel's keys, so two threads that happened to be issued the same
  id would still be two streams.

None of those three is a permission check, and that is the design. A check is
a line you can forget to write; a namespace that does not contain other
people's records cannot be indexed into by accident. Where a shared table was
genuinely unavoidable -- the session list, which has to be one table so that
"sign out everywhere" can exist -- the lookup takes the account *and* the id
as a pair, and a mismatch is simply not found.

The static mount is last and at `/`. Starlette matches routes in registration
order, so every `/api/...` and `/ws/...` above is claimed first and only what
nothing else wanted falls through to the built frontend.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

from minicodex import __version__
from minicodex.memory import MINICODEX_HOME
from minicodex.tenancy import TENANTS_DIR, Owner

from .accounts import AccountStore
from .approver import ApprovalBroker
from .auth import AuthMiddleware
from .channel import Channel
from .oauth import PendingOAuthTable
from .quota import QuotaLedger
from .routes import router
from .sessions import SessionTable
from .store import DEFAULT_DATA_DIR, Store

log = logging.getLogger(__name__)

#: Where `npm run build` puts the console. Two directories up from this module
#: is `src/minicodex`, so this resolves to the step's own `frontend/dist`.
FRONTEND_DIST = Path(__file__).resolve().parents[3] / "frontend" / "dist"

_NO_BUILD_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>minicodex console - not built</title>
<style>
  body {{ font: 15px/1.6 ui-sans-serif, system-ui, sans-serif; max-width: 46rem;
         margin: 12vh auto; padding: 0 1.5rem; color: #10182B; background: #EDF1FA; }}
  code {{ background: #fff; border: 1px solid #CCD6EC; border-radius: 6px;
          padding: .15em .4em; font-family: ui-monospace, Consolas, monospace; }}
  pre {{ background: #fff; border: 1px solid #CCD6EC; border-radius: 9px; padding: 1rem;
         overflow-x: auto; }}
  h1 {{ font-size: 1.4rem; }}
  @media (prefers-color-scheme: dark) {{
    body {{ color: #EAF0FF; background: #0A1120; }}
    code, pre {{ background: #15203A; border-color: #26365C; }}
  }}
</style></head><body>
<h1>The console has no build yet</h1>
<p>The API is up &mdash; this is <code>minicodex serve</code> {version} answering. What is
missing is the frontend bundle, which is not committed to the repository.</p>
<pre>cd {root}
npm ci
npm run build</pre>
<p>Then reload this page. While working on the frontend itself, run
<code>npm run dev</code> instead and use the Vite server on port 5173; it proxies
<code>/api</code> and <code>/ws</code> back here.</p>
<p>Expected at: <code>{dist}</code></p>
</body></html>"""


@dataclass
class Console:
    """Everything one `minicodex serve` process owns."""

    #: The root of the console's own state. Accounts live here; every record
    #: store lives one level down under `tenants/<key>/`.
    data_dir: Path
    accounts: AccountStore
    #: Where tenants' home directories live: `<home>/tenants/<key>/` holds the
    #: memories, the memory-job table and the MCP tokens that chapter 20's
    #: `Owner` names. Defaults to `~/.minicodex`, so a single-account console
    #: on a laptop shares one home with the CLI, byte for byte.
    home: Path = MINICODEX_HOME
    sessions: SessionTable = field(default_factory=SessionTable)
    quota: QuotaLedger = field(default_factory=QuotaLedger)
    channel: Channel = field(default_factory=Channel)
    #: Turns with a run in flight, keyed by `turn_key`. Claimed synchronously
    #: in the route, which is the whole point -- see routes.py.
    busy: set[str] = field(default_factory=set)
    #: The task per busy turn, so the stop button has something to cancel and
    #: so nothing collects a running task.
    running: dict[str, asyncio.Task[None]] = field(default_factory=dict)
    #: Chapter 22's OAuth attempts that have a `start` but no `callback` yet.
    #: One table, because an attempt already carries the owner it belongs to
    #: and `PendingOAuthTable.pop` is already the only lookup that is trusted.
    mcp_oauth: PendingOAuthTable = field(default_factory=PendingOAuthTable)

    _stores: dict[str, Store] = field(default_factory=dict, repr=False)
    _brokers: dict[str, ApprovalBroker] = field(default_factory=dict, repr=False)

    # -- per-owner state ------------------------------------------------------

    def store_for(self, owner: Owner) -> Store:
        """This owner's records, seeded on first touch.

        `~/.minicodex/tenants/<key>/` (chapter 20's `Owner.root`) and
        `<data-dir>/tenants/<key>/` (here) are two different trees keyed by the
        same string, and they stay separate for the same reason they were
        separate when there was one user: one is the agent's home directory,
        which the CLI shares, and the other is this server's own bookkeeping,
        which the CLI has never heard of.
        """
        key = owner.key or ""
        store = self._stores.get(key)
        if store is None:
            root = self.data_dir if owner.key is None else self.data_dir / TENANTS_DIR / owner.key
            store = Store(root)
            store.seed_providers()
            self._stores[key] = store
        return store

    def broker_for(self, account_key: str) -> ApprovalBroker:
        broker = self._brokers.get(account_key)
        if broker is None:
            broker = ApprovalBroker()
            self._brokers[account_key] = broker
        return broker

    def running_for(self, account_key: str) -> int:
        prefix = f"{account_key}/"
        return sum(1 for key in self.busy if key.startswith(prefix))

    # -- lookups --------------------------------------------------------------

    def require_thread(self, owner: Owner, thread_id: str) -> dict[str, Any]:
        """A thread of *this* owner's, or 404.

        There is no ownership check in this method, and that is not an
        omission: `store_for` returned a store rooted at one directory, so a
        thread belonging to somebody else is not absent-and-forbidden, it is
        absent. The 404 is the truth rather than a cover story, which is also
        why it does not leak whether the id exists elsewhere.
        """
        record = self.store_for(owner).thread(thread_id)
        if record is None:
            raise HTTPException(404, f"no thread {thread_id!r}")
        return record


def create_app(
    data_dir: Path = DEFAULT_DATA_DIR,
    *,
    frontend: Path | None = None,
    home: Path = MINICODEX_HOME,
) -> FastAPI:
    data_dir = Path(data_dir)
    accounts = AccountStore(data_dir / "accounts.json")

    app = FastAPI(title="minicodex console", version=__version__)
    console = Console(data_dir=data_dir, accounts=accounts, home=Path(home))
    app.state.console = console
    app.include_router(router)

    # Added after the router so that it wraps it, and reaching for the console
    # through a callable rather than capturing it so a test may replace
    # `app.state.console` without the gate keeping the old one.
    app.add_middleware(AuthMiddleware, console_of=lambda: app.state.console)

    dist = FRONTEND_DIST if frontend is None else Path(frontend)

    @app.on_event("shutdown")
    async def _stop_everything() -> None:
        live: Console = app.state.console
        # Cancel in-flight turns before the loop goes away. Each one is inside
        # `agent.run`, which writes `mark("interrupted")` on the way out, so
        # Ctrl-C on the server leaves resumable threads rather than truncated
        # ones -- the same guarantee chapter 7 gives the CLI.
        for task in list(live.running.values()):
            task.cancel()
        for task in list(live.running.values()):
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        for broker in list(live._brokers.values()):
            broker.fail_all("server shutting down")
        await live.channel.aclose()

    if dist.is_dir() and (dist / "index.html").exists():
        app.mount("/", StaticFiles(directory=dist, html=True), name="frontend")
    else:

        @app.get("/", response_class=HTMLResponse)
        def _needs_a_build() -> str:
            # A page that says what to run, not a 404. The first thing anyone
            # does with a new checkout is start the server and open the port.
            return _NO_BUILD_PAGE.format(version=__version__, root=dist.parent, dist=dist)

    return app


def serve(host: str = "127.0.0.1", port: int = 8000, data_dir: Path = DEFAULT_DATA_DIR) -> int:
    """`minicodex serve`. Bound to localhost by default, deliberately.

    This process runs shell commands and edits files on behalf of whoever signs
    in to it. Since chapter 23 it does have authentication, and the default is
    still `127.0.0.1`, because those are answers to different questions: a
    login decides *who* may drive the agent, and the bind address decides who
    may reach the port at all. Removing the second because the first now exists
    would be trading a boundary for a credential.
    """
    try:
        import uvicorn
    except ImportError:
        print("the console needs its extra: uv sync --extra web")
        return 1

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    app = create_app(data_dir)
    console: Console = app.state.console

    token = console.accounts.issue_bootstrap_token()
    if token is not None:
        # Printed, not logged. A log line goes wherever logging is configured
        # to go -- a file, a journal, a shipper -- and this is a credential
        # that should live exactly as long as the terminal that started the
        # process. Chapter 19 made the opposite call for the listen address
        # and was right for the opposite reason.
        print()
        print("  No account yet. Open the console and use this one-time token:")
        print(f"      {token}")
        print("  It is not written to disk and a restart issues a new one.")
        print()
    if host not in ("127.0.0.1", "localhost", "::1"):
        log.warning("listening on %s: this server runs commands and edits files", host)
    uvicorn.run(app, host=host, port=port, log_level="info")
    return 0
