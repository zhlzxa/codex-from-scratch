"""Assemble the console: one store, one channel, one broker, one router.

A `Console` object rather than module-level globals, for the reason every other
composition root in this package exists (`composition.py`, interlude B): state
that is created once per process and reached for by name is state that cannot
be created twice in one interpreter, which means it cannot be tested. Every
route reaches its dependencies through `request.app.state.console`, so a test
builds an app on a `tmp_path` and gets a whole console with nothing shared.

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

from .approver import ApprovalBroker
from .channel import Channel
from .oauth import PendingOAuthTable
from .routes import router
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

    store: Store
    channel: Channel = field(default_factory=Channel)
    broker: ApprovalBroker = field(default_factory=ApprovalBroker)
    #: Threads with a turn in flight. Claimed synchronously in the route, which
    #: is the whole point -- see routes.py.
    busy: set[str] = field(default_factory=set)
    #: The task per busy thread, so the stop button has something to cancel and
    #: so nothing collects a running task.
    running: dict[str, asyncio.Task[None]] = field(default_factory=dict)
    #: Chapter 22's OAuth attempts that have a `start` but no `callback` yet.
    mcp_oauth: PendingOAuthTable = field(default_factory=PendingOAuthTable)

    def require_thread(self, thread_id: str) -> dict[str, Any]:
        record = self.store.thread(thread_id)
        if record is None:
            raise HTTPException(404, f"no thread {thread_id!r}")
        return record


def create_app(data_dir: Path = DEFAULT_DATA_DIR, *, frontend: Path | None = None) -> FastAPI:
    store = Store(data_dir)
    store.seed_providers()

    app = FastAPI(title="minicodex console", version=__version__)
    app.state.console = Console(store=store)
    app.include_router(router)

    dist = FRONTEND_DIST if frontend is None else Path(frontend)

    @app.on_event("shutdown")
    async def _stop_everything() -> None:
        console: Console = app.state.console
        # Cancel in-flight turns before the loop goes away. Each one is inside
        # `agent.run`, which writes `mark("interrupted")` on the way out, so
        # Ctrl-C on the server leaves resumable threads rather than truncated
        # ones -- the same guarantee chapter 7 gives the CLI.
        for task in list(console.running.values()):
            task.cancel()
        for task in list(console.running.values()):
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        console.broker.fail_all("server shutting down")
        await console.channel.aclose()

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

    This process runs shell commands and edits files on behalf of whoever can
    reach it, with an approval prompt as the only gate, and it has no
    authentication of any kind. `0.0.0.0` is a decision for a person to make on
    purpose, not a default to inherit.
    """
    try:
        import uvicorn
    except ImportError:
        print("the console needs its extra: uv sync --extra web")
        return 1

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    if host not in ("127.0.0.1", "localhost", "::1"):
        log.warning(
            "listening on %s: this server runs commands and edits files, and has no auth", host
        )
    uvicorn.run(create_app(data_dir), host=host, port=port, log_level="info")
    return 0
