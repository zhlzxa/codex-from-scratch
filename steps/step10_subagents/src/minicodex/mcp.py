"""Talking to an MCP server, through the official SDK.

The protocol -- framing, `initialize`, version negotiation, telling a response
apart from a notification apart from a server-initiated request -- comes from
`mcp`, the official Python SDK. codex does the same with the Rust one
(`rmcp = { version = "=3.0.0" }`, `codex-rs/Cargo.toml:393`).

The line this book draws:

    **Hand-roll what you are teaching. Depend on what you are not.**

This project already depends on `httpx` rather than writing HTTP, on `pytest`
rather than writing a test runner. Chapter 9 is about tool-name collisions, a
schema budget, footprints, result normalisation, timeouts, and trusting
somebody else's process -- none of which is message framing. `registry.py`
holds most of that; this module holds the rest.

What is this module's own work, and why none of it is the SDK's:

  - **The environment allowlist.** The SDK ships one
    (`DEFAULT_INHERITED_ENV_VARS`), and it is not this one. Ours was arrived
    at by a real failure -- chapter 11's `SYSTEMROOT`/Winsock hang -- and
    keeping it is not stubbornness: an SDK's default is a reasonable guess
    about every caller, and a fault list is a fact about this one (F09-10).

  - **Two timeouts, not one.** A server that hangs during startup hangs the
    agent; a server that hangs during a call hangs a turn. Different failures,
    different budgets (F09-04).

  - **`call()` never raises for a failed tool.** Chapter 0's rule -- a tool's
    failure is information the model needs, not an exception that ends the
    session -- applies just as much when the failure happened in somebody
    else's process.

  - **A dead server has to be able to say why.** The SDK reports
    `Connection closed`; the model needs more than that, so stderr is still
    drained and kept (F09-05).
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import tempfile
from collections.abc import Awaitable, Callable
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from mcp import Client
from mcp.client.stdio import (
    DEFAULT_INHERITED_ENV_VARS,
    StdioServerParameters,
    stdio_client,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from mcp_types import CallToolResult

# Long enough for a server that compiles something on first run, short enough
# that a broken one does not hold the agent hostage.  codex uses 30s for the
# same budget (`rmcp-client/src/rmcp_client.rs`).
DEFAULT_STARTUP_TIMEOUT = 30.0
DEFAULT_TOOL_TIMEOUT = 60.0

# How long to let the SDK take the child process down before giving up on it.
#
# Not a round number, and not a guess: `mcp.client.stdio` terminates in stages
# -- `PROCESS_TERMINATION_TIMEOUT = 2.0` for a polite exit, then
# `FORCE_KILL_TIMEOUT = 2.0` for the kill -- so a server that ignores both
# needs a shade over four seconds to be gone.
# A 5s cap is *nearly* enough, and that is worse
# than plainly too short: `asyncio.wait_for` cancels the cleanup it was
# waiting on, so a shutdown interrupted at second five leaves the child
# running and nothing left to kill it.  The test suite ended with ten orphaned
# Python processes and a pytest that would not exit -- F02-08 again, two
# chapters and one dependency later (F09-13).
SHUTDOWN_TIMEOUT = 15.0

# What an MCP subprocess is allowed to see.  Same allowlist idea as chapter 2's
# `ENV_ALLOWLIST` (F02-09) and for the same reason -- except an MCP server
# usually *does* need a credential, so `env` in the config adds to this
# explicitly, one variable at a time, rather than the process handing over
# everything it happens to have.
#
# The SDK has its own list and we do not use it (F09-10).  `mcp.client.stdio`
# exports `DEFAULT_INHERITED_ENV_VARS`:
#
#     APPDATA HOMEDRIVE HOMEPATH LOCALAPPDATA PATH PATHEXT
#     PROCESSOR_ARCHITECTURE SYSTEMDRIVE SYSTEMROOT TEMP USERNAME USERPROFILE
#
# Wider than ours on Windows (`USERNAME`, `USERPROFILE`, `TEMP`, `APPDATA` --
# four more ways for a server to learn who is running it and where their files
# are) and narrower on POSIX, where it has no `HOME`, no `LANG`, no `TZ`.
# Neither list is wrong; they answer different questions.  Theirs is a good
# guess about every caller they will ever have.  Ours is a fact about this one,
# and `SYSTEMROOT` is on it because chapter 11 watched `import asyncio` fail
# inside a subprocess without it.
#
# **And passing ours does not replace theirs.**  `mcp/client/stdio.py` builds
# the child's environment as
#
#     env=get_default_environment() | (server.env or {})
#
# -- a union.  Measured, with an allowlist of five variables actually present
# on this machine, the child received fourteen:
#
#     EXTRA: APPDATA HOMEDRIVE HOMEPATH LOCALAPPDATA PROCESSOR_ARCHITECTURE
#            SYSTEMDRIVE TEMP USERNAME USERPROFILE
#
# Nine variables this chapter had deliberately left out, arriving because a
# dependency had an opinion.  The boundary is bounded -- an unrelated secret
# in `os.environ` did *not* come through, because the union is with a fixed
# list rather than with the whole environment -- but "bounded" is not
# "chosen", and F02-09 was about choosing.
#
# So `_subprocess_env` blanks them (F09-10).  A key set to the empty string
# still arrives, and that is the best the union allows: the server learns that
# `USERNAME` exists and nothing about who it is.
ENV_ALLOWLIST = ("PATH", "HOME", "LANG", "LC_ALL", "TERM", "TZ", "SYSTEMROOT", "PATHEXT")


class McpError(RuntimeError):
    """The server could not be reached, started, or spoken to.

    Distinct from a tool that ran and failed: that comes back as text for the
    model.  This one means the transport itself is not usable, which is a
    fact about the session rather than about the call.
    """


@dataclass(frozen=True)
class ServerConfig:
    name: str
    command: tuple[str, ...]
    env: dict[str, str] = field(default_factory=dict)
    cwd: str | None = None
    startup_timeout: float = DEFAULT_STARTUP_TIMEOUT
    tool_timeout: float = DEFAULT_TOOL_TIMEOUT


@dataclass(frozen=True)
class RemoteTool:
    """One tool as the server describes it, before anything renames it.

    `name` is the raw MCP name and is what goes back on the wire.  The name the
    model sees is derived from this and lives in `registry.py`; keeping the two
    apart is F09-01's fix, and keeping the raw one is what makes a call still
    reach the right tool after the rename.

    `read_only` is the server's `annotations.readOnlyHint`, kept as
    `bool | None` rather than defaulting to False: "the server said it is not
    read-only" and "the server said nothing" are different states, and chapter
    8's scheduler is about to have to decide what to do with the difference.
    """

    server: str
    name: str
    description: str
    input_schema: dict[str, Any]
    read_only: bool | None = None


# Answering a server-initiated request.  Kept as a name because `registry.py`
# builds one (`elicitation_handler`) and because the shape -- params in, result
# out, raise to refuse -- is this project's, not the SDK's.  `_as_callback`
# below adapts it to what `mcp.Client` wants.
RequestHandler = Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]


def _subprocess_env(config: ServerConfig) -> dict[str, str]:
    """What the server process is allowed to see.

    Two passes, and the order between them is what makes the result an upper
    bound rather than a lower one.

    The comprehension is F02-09 one process further out: the allowlist decides
    what may be copied out of *this* process, because an MCP server is
    somebody else's program, started by us, with no use for the key this agent
    talks to its model with.

    The blanking pass is F09-10, and it exists only because the SDK unions its
    own `DEFAULT_INHERITED_ENV_VARS` into whatever it is given -- so an
    allowlist handed over as-is would be a floor, not a ceiling.  Naming each
    unwanted key with an empty value is the only override the union permits.
    """
    env = {key: os.environ[key] for key in ENV_ALLOWLIST if key in os.environ}
    env.update(config.env)
    for key in DEFAULT_INHERITED_ENV_VARS:
        if key not in env:
            env[key] = ""
    return env


def _as_callback(handler: RequestHandler) -> Any:
    """Wrap this project's `RequestHandler` as the SDK's elicitation callback.

    An adapter rather than a second implementation, because the
    thing that module knows -- that a server's question goes to chapter 5's
    approver, that a multi-field schema is declined rather than guessed at --
    is unaffected by which library carries the message.  Only the envelope
    differs.
    """
    from mcp_types import ElicitResult

    async def callback(context: Any, params: Any) -> Any:
        raw = {
            "message": getattr(params, "message", "") or "",
            "requestedSchema": getattr(params, "requestedSchema", None)
            or getattr(params, "requested_schema", None)
            or {},
        }
        result = await handler(raw)
        action = result.get("action", "decline")
        content = result.get("content")
        if action == "accept" and content is not None:
            return ElicitResult(action="accept", content=content)
        return ElicitResult(action=action)

    return callback


class McpClient:
    """One connection to one server.

    Not thread-safe and not meant to be: it belongs to one event loop.

    The lifecycle is still `start()` / `close()` rather than `async with`,
    and that is deliberate.  `mcp.Client` is an async context manager, which
    is the right shape for a script; a session holds several servers open
    across many turns and closes them when the *session* ends, not when a
    block exits.  `AsyncExitStack` is the bridge, and it is the whole of the
    adaptation -- three lines.
    """

    def __init__(
        self,
        config: ServerConfig,
        *,
        handlers: dict[str, RequestHandler] | None = None,
        server: Any | None = None,
    ) -> None:
        self.config = config
        self.handlers = handlers or {}
        # `server` is the in-process escape hatch: `mcp.Client` accepts an
        # `MCPServer` object as readily as a command line, so the tests can
        # run a *real* server in this process with no subprocess and no
        # network.  Production never passes it.
        self._server = server
        self._client: Client | None = None
        self._stack: AsyncExitStack | None = None
        self._stderr_path: Path | None = None
        # The last thing the server said before it stopped saying anything.
        self._last_words = ""
        self.server_info: dict[str, Any] = {}
        # Why the connection ended, for the message the model gets when it
        # calls a tool on a server that is no longer there.  A dead server that
        # cannot say why produces a support question nobody can answer.
        self.failure: str | None = None

    # -- lifecycle -----------------------------------------------------------

    @property
    def alive(self) -> bool:
        return self._client is not None

    def _target(self, stack: AsyncExitStack) -> Any:
        """What `mcp.Client` should connect to, and where stderr goes.

        The stderr detour is F09-05.  `Client` given a
        `StdioServerParameters` builds its own transport with
        `errlog=sys.stderr`, which sends a dying server's traceback to the
        terminal and leaves this client holding `Connection closed` -- true,
        and useless to a model that has to explain itself to a user.

        `Client` also accepts a `Transport`, and `stdio_client` takes an
        `errlog`, so the capture is an *argument* rather than a fork of the
        SDK.  A file and not a `StringIO`: `errlog` ends up as
        `anyio.open_process(stderr=...)`, which needs a real file descriptor.
        """
        if self._server is not None:
            return self._server

        # Not a `with`: the file has to outlive this function and stay open
        # for as long as the child writes to it.  The exit stack owns it.
        handle = tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", suffix=".mcp-stderr", delete=False
        )
        self._stderr_path = Path(handle.name)
        stack.callback(self._collect_last_words)
        stack.enter_context(handle)

        params = StdioServerParameters(
            command=self.config.command[0],
            args=list(self.config.command[1:]),
            env=_subprocess_env(self.config),
            cwd=self.config.cwd,
        )
        return stdio_client(params, errlog=handle)

    def _refresh_last_words(self) -> None:
        """Re-read the server's stderr without disturbing it.

        Called from `_why`, not only from the exit stack, and that is the
        whole of F09-05 on the live path: when a call fails with `Connection
        closed`, the connection is gone but the *stack is not unwound yet* --
        the session still holds this client open.  Reading only on close would
        put the explanation in a variable nobody looks at until after the
        message that needed it was already sent.
        """
        path = self._stderr_path
        if path is None:
            return
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:  # pragma: no cover - the file was never created
            return
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        if lines:
            self._last_words = lines[-1][:400]

    def _collect_last_words(self) -> None:
        """Final read, then drop the file.  Runs on the exit stack."""
        self._refresh_last_words()
        path, self._stderr_path = self._stderr_path, None
        if path is not None:
            with contextlib.suppress(OSError):
                path.unlink()

    async def start(self) -> None:
        """Connect to the server and complete the MCP handshake.

        The handshake itself is the SDK's -- version negotiation, capability
        exchange, `notifications/initialized` -- and none of it is written
        here any more.  What is written here is the deadline around it, which
        the SDK does not impose and which F09-04 says has to exist.
        """
        stack = AsyncExitStack()
        callbacks: dict[str, Any] = {}
        elicit = self.handlers.get("elicitation/create")
        if elicit is not None:
            callbacks["elicitation_callback"] = _as_callback(elicit)

        try:
            target = self._target(stack)
            client = await asyncio.wait_for(
                stack.enter_async_context(
                    Client(target, read_timeout_seconds=self.config.tool_timeout, **callbacks)
                ),
                timeout=self.config.startup_timeout,
            )
        except (TimeoutError, asyncio.TimeoutError) as exc:
            self.failure = (
                f"{self.config.name} did not answer initialize within "
                f"{self.config.startup_timeout:.0f}s"
            )
            await self._unwind(stack)
            raise McpError(self.failure) from exc
        except Exception as exc:
            await self._unwind(stack)
            # After the unwind, because that is what reads stderr: a server
            # that failed to start usually said why on the way out, and the
            # message without it is "could not start" and nothing else.
            detail = f"{exc}"
            if self._last_words:
                detail = f"{detail}: {self._last_words}"
            self.failure = f"could not start {self.config.name}: {detail}"
            raise McpError(self.failure) from exc

        self._stack = stack
        self._client = client
        info = getattr(client, "server_info", None)
        if info is not None:
            self.server_info = {
                "name": getattr(info, "name", "") or "",
                "version": getattr(info, "version", "") or "",
            }

    async def _unwind(self, stack: AsyncExitStack) -> None:
        """Close a stack that may be half-built, without raising from cleanup.

        A failed `start()` has already decided what went wrong; letting the
        unwind raise something else on top replaces a diagnosis with an
        artefact of the tidying-up.
        """
        with contextlib.suppress(Exception):
            await asyncio.wait_for(stack.aclose(), timeout=SHUTDOWN_TIMEOUT)

    async def close(self) -> None:
        """Shut the server down.

        Closing a subprocess transport cleanly -- stdin first so a well-behaved
        server exits on EOF, then a wait, then a kill, then the pipes themselves
        so the Windows proactor loop does not raise from `__del__` -- is the
        SDK's job, and a better place for it: that is subprocess plumbing, not
        anything about agents.
        """
        stack, self._stack = self._stack, None
        self._client = None
        if stack is None:
            return
        try:
            await asyncio.wait_for(stack.aclose(), timeout=SHUTDOWN_TIMEOUT)
        except (TimeoutError, asyncio.TimeoutError):  # pragma: no cover - slow child
            # Said out loud rather than swallowed.  Past this point the child
            # is beyond this process's reach, and a session that leaks one
            # should be able to name it -- silence here is how ten of them
            # accumulate before anybody notices.
            self.failure = self.failure or (
                f"{self.config.name} did not shut down within "
                f"{SHUTDOWN_TIMEOUT:.0f}s and may still be running"
            )
        except Exception:  # pragma: no cover - the child died on its own
            pass

    # -- the two things anyone actually calls --------------------------------

    async def list_tools(self) -> list[RemoteTool]:
        client = self._require()
        try:
            result = await asyncio.wait_for(client.list_tools(), timeout=self.config.tool_timeout)
        except (TimeoutError, asyncio.TimeoutError):
            raise McpError(f"{self.config.name} did not answer tools/list in time") from None
        except Exception as exc:
            raise McpError(self._why(exc)) from exc

        tools: list[RemoteTool] = []
        for raw in result.tools:
            annotations = getattr(raw, "annotations", None)
            hint = getattr(annotations, "read_only_hint", None) if annotations else None
            tools.append(
                RemoteTool(
                    server=self.config.name,
                    name=raw.name,
                    description=str(getattr(raw, "description", "") or ""),
                    # SDK 2.x is snake_case on the Python side and still
                    # `inputSchema` on the wire.  Reading the attribute rather
                    # than the wire key is the point of using it.
                    input_schema=dict(raw.input_schema or {"type": "object", "properties": {}}),
                    read_only=hint if isinstance(hint, bool) else None,
                )
            )
        return tools

    async def call(self, name: str, arguments: dict[str, Any]) -> CallToolResult:
        """Invoke one tool.

        Raises only for transport failures, never for a tool that ran and
        reported an error -- that comes back in the result, with `is_error`
        set, and `registry.normalise` turns it into text the model can act on.
        """
        client = self._require()
        try:
            return await asyncio.wait_for(
                client.call_tool(name, arguments), timeout=self.config.tool_timeout
            )
        except (TimeoutError, asyncio.TimeoutError):
            raise McpError(
                f"{self.config.name}.{name} did not answer within {self.config.tool_timeout:.0f}s"
            ) from None
        except Exception as exc:
            raise McpError(self._why(exc)) from exc

    def _require(self) -> Client:
        client = self._client
        if client is None:
            raise McpError(self.failure or f"{self.config.name} is not running")
        return client

    def _why(self, exc: Exception) -> str:
        """The message the model gets when the transport failed, and the point
        at which this client admits it is no longer connected.

        The SDK says `Connection closed`, which is true and useless: it does
        not say whether the server crashed, was killed, or exited cleanly, and
        the traceback that would have said went to a pipe.  F09-05 is that
        gap, and it is still this module's to fill -- so the server's stderr
        is re-read here and appended.

        Dropping `_client` is the other half (F09-14).  There is no
        `returncode` to ask about -- the transport keeps the process handle --
        so liveness has to be *inferred* from a call failing, and the inference
        has to happen here: otherwise `registry`'s `if not client.alive` never
        fires and a dead server is never restarted.  "The client object still
        exists" is true of a client whose server died ten seconds ago.
        """
        self._refresh_last_words()
        self._client = None
        detail = f"{self.config.name}: {exc}"
        if self._last_words:
            return f"{detail}: {self._last_words}"
        return detail


__all__ = [
    "DEFAULT_STARTUP_TIMEOUT",
    "DEFAULT_TOOL_TIMEOUT",
    "ENV_ALLOWLIST",
    "McpClient",
    "McpError",
    "RemoteTool",
    "RequestHandler",
    "ServerConfig",
]
