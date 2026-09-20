"""Assemble the console: accounts, sessions, one store per owner, one router.

A `Console` object rather than module-level globals, for the reason every other
composition root in this package exists (`composition.py`): state that is
created once per process and reached for by name is state that cannot be
created twice in one interpreter, which means it cannot be tested. Every
route reaches its dependencies through `request.app.state.console`, so a test
builds an app on a `tmp_path` and gets a whole console with nothing shared.

"Nothing shared" inside one process: three things that could be single
objects are one per account instead.

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
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

from minicodex import __version__
from minicodex.memory import MINICODEX_HOME
from minicodex.sandbox import for_mode as sandbox_for_mode
from minicodex.tenancy import TENANTS_DIR, Owner

from .accounts import AccountStore
from .approver import ApprovalBroker
from .audit import AuditLog
from .auth import AuthMiddleware
from .channel import Channel
from .oauth import PendingOAuthTable
from .quota import QuotaLedger
from .routes import router
from .sessions import SessionTable
from .store import DEFAULT_DATA_DIR, Store
from .throttle import LoginThrottle

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
    database: Any = None
    #: Where tenants' home directories live: `<home>/tenants/<key>/` holds the
    #: memories, the memory-job table and the MCP tokens that `Owner`
    #: (`tenancy.py`) names. Defaults to `~/.minicodex`, so a single-account
    #: console on a laptop shares one home with the CLI, byte for byte.
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
    #: OAuth attempts that have a `start` but no `callback` yet.  One table,
    #: because an attempt already carries the owner it belongs to and
    #: `PendingOAuthTable.pop` is already the only lookup that is trusted.
    mcp_oauth: PendingOAuthTable = field(default_factory=PendingOAuthTable)
    #: Failed-login accounting, by (account, source).  Checked before the
    #: password is hashed; see `throttle.py`.
    logins: LoginThrottle = field(default_factory=LoginThrottle)
    #: The audit trail: who made this server do what.  One JSONL file under
    #: the data dir; write failures are fatal to the operation being audited
    #: rather than swallowed -- see `audit.py`.
    audit: AuditLog = field(default_factory=lambda: AuditLog(Path("audit-unused.jsonl")))
    #: When the process started, for `/api/health`'s uptime figure.
    started_at: float = field(default_factory=time.time)
    #: Turn counters.  `turns_total` counts claimed turns including failures
    #: -- the health probe's consumers are uptime checks and dashboards, and
    #: a success-only count would make the worst hour look like an idle one.
    turns_total: int = 0
    turns_failed: int = 0

    _stores: dict[str, Store] = field(default_factory=dict, repr=False)
    _brokers: dict[str, ApprovalBroker] = field(default_factory=dict, repr=False)

    # -- per-owner state ------------------------------------------------------

    def store_for(self, owner: Owner) -> Store:
        """This owner's records, seeded on first touch.

        `~/.minicodex/tenants/<key>/` (`tenancy.Owner.root`) and
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
            if self.database is not None:
                from .db_stores import DatabaseStore

                store = DatabaseStore(self.database, key, root)
            else:
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
    database_url: str | None = None,
) -> FastAPI:
    data_dir = Path(data_dir)
    url = os.environ.get("MINICODEX_DATABASE_URL", "") if database_url is None else database_url
    database = None
    if url:
        from .db_engine import Database
        from .db_stores import DatabaseAccounts, DatabaseQuota, DatabaseSessions
        from .import_json import require_imported

        database = Database(url)
        try:
            require_imported(database, data_dir)
        except BaseException:
            database.close()
            raise
        accounts = DatabaseAccounts(database)
        sessions = DatabaseSessions(database)
        quota = DatabaseQuota(database)
    else:
        accounts = AccountStore(data_dir / "accounts.json")
        sessions = SessionTable.load(data_dir / "sessions.json")
        quota = QuotaLedger.load(data_dir / "quota.json")

    app = FastAPI(title="minicodex console", version=__version__)
    console = Console(
        data_dir=data_dir,
        accounts=accounts,
        database=database,
        home=Path(home),
        sessions=sessions,
        quota=quota,
        audit=AuditLog(data_dir / "audit.jsonl"),
    )
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
        # ones -- the same guarantee the CLI gives.
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
        # Last write of both tables, before the process is gone.  The debounce
        # means at most one second of logins/quota is at risk on a kill; a
        # clean stop risks nothing.
        live.sessions.flush()
        live.quota.flush()
        if live.database is not None:
            live.database.close()

    if dist.is_dir() and (dist / "index.html").exists():
        app.mount("/", StaticFiles(directory=dist, html=True), name="frontend")
    else:

        @app.get("/", response_class=HTMLResponse)
        def _needs_a_build() -> str:
            # A page that says what to run, not a 404. The first thing anyone
            # does with a new checkout is start the server and open the port.
            return _NO_BUILD_PAGE.format(version=__version__, root=dist.parent, dist=dist)

    return app


def _startup_gate(host: str, *, unsandboxed_ok: bool) -> int | None:
    """Refuse to start a server whose commands cannot be confined.

    The remaining way to run unconfined without deciding to is a machine
    without `bwrap` -- so the decision is moved to the one moment an operator
    is actually watching: startup.  Failing closed here costs a typed command;
    failing closed at the first shell call would cost somebody's afternoon and
    every command before the error was already off the leash.

    `unsandboxed_ok` is the explicit out.  It exists for the developer on a
    laptop running against loopback, and it is spelled out rather than
    inferred from the host: an operator who binds a non-loopback address on a
    machine with no sandbox has to have written the word into the command
    line, not merely accepted a default.
    """
    probe = sandbox_for_mode("read-only", Path.cwd())
    reason = probe.unavailable()
    if reason is None:
        return None
    if unsandboxed_ok:
        log.warning("sandbox unavailable (%s) -- starting anyway, you accepted this", reason)
        return None
    print("This server cannot confine the commands it runs:", reason)
    if host not in ("127.0.0.1", "localhost", "::1"):
        print()
        print("  You are binding a non-loopback address. Refusing to start unsandboxed.")
        print("  Install bubblewrap (apt install bubblewrap), run on Linux, or pass")
        print("      --allow-unsandboxed")
        print("  if you accept every signed-in account running commands on this host.")
    else:
        print("Install bubblewrap (apt install bubblewrap) or pass --allow-unsandboxed.")
    return 1


def serve(
    host: str = "127.0.0.1",
    port: int = 8000,
    data_dir: Path = DEFAULT_DATA_DIR,
    *,
    allow_unsandboxed: bool = False,
) -> int:
    """`minicodex serve`. Bound to localhost by default, deliberately.

    This process runs shell commands and edits files on behalf of whoever
    signs in to it.  A login decides *who* may drive the agent; the bind
    address decides who may reach the port at all.  Removing the second
    because the first now exists would be trading a boundary for a
    credential.
    """
    try:
        import uvicorn
    except ImportError:
        print("the console needs its extra: uv sync --extra web")
        return 1

    gated = _startup_gate(host, unsandboxed_ok=allow_unsandboxed)
    if gated is not None:
        return gated

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    app = create_app(data_dir)
    console: Console = app.state.console

    # The startup fact sheet: everything an operator standing at a terminal
    # wants to know before walking away, in one place -- what it would do,
    # where its state lives, and whether anyone can log in yet.
    if not allow_unsandboxed:
        sandbox_line = "read-only / workspace-write confined (bwrap)"
    else:
        sandbox_line = "confined where bwrap exists (--allow-unsandboxed accepted)"
    print()
    print(f"  minicodex console {__version__}")
    print(f"  data dir      {Path(data_dir).resolve()}")
    print(f"  agent home    {console.home}")
    print(f"  sandbox       {sandbox_line}")
    print(f"  audit trail   {console.audit.path}")
    print(
        f"  accounts      {len(console.accounts.all())} "
        f"({'bootstrap needed' if console.accounts.empty() else 'bootstrap closed'})"
    )
    print()

    token = console.accounts.issue_bootstrap_token()
    if token is not None:
        # Printed, not logged. A log line goes wherever logging is configured
        # to go -- a file, a journal, a shipper -- and this is a credential
        # that should live exactly as long as the terminal that started the
        # process.  (The listen address warning is the opposite call, and
        # right for the opposite reason: that one belongs in logs.)
        print()
        print("  No account yet. Open the console and use this one-time token:")
        print(f"      {token}")
        print("  It is not written to disk and a restart issues a new one.")
        print()
    if host not in ("127.0.0.1", "localhost", "::1"):
        if console.accounts.empty():
            # The bootstrap token above is the *only* way to create the first
            # account, and it was printed to this terminal.  A first account
            # created over a network you do not own is not a bootstrap, it is
            # a race.
            print(f"refusing --host {host}: no account exists yet; bootstrap on loopback first")
            return 1
        log.warning("listening on %s: this server runs commands and edits files", host)
    uvicorn.run(app, host=host, port=port, log_level="info")
    return 0
