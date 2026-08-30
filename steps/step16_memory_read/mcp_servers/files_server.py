"""A real MCP server, built on the official SDK, that can be told to misbehave.

This file used to hand-roll the *server* side of JSON-RPC the way
`minicodex/mcp.py` used to hand-roll the client side. Both were rewritten for
the same reason (see that module's docstring): codex pins the official SDK
(`rmcp = "=3.0.0"`, `codex-rs/Cargo.toml:393`), and framing was never what
chapter 9 is teaching.

Written rather than installed, though, and that part has not changed. codex
ships its own test server for exactly this reason
(`codex-rs/rmcp-client/src/bin/test_stdio_server.rs`): a test that borrows
somebody else's MCP server is also testing their server, and cannot ask it to
fail on cue. Every fault in this chapter needs a server that does one specific
wrong thing.

What the SDK does and does not let this server do is itself worth noticing.
The good behaviour -- initialize, capability exchange, tool listing, the
`isError` result shape -- is now three decorators. The *bad* behaviour is what
still needs code, and a couple of shapes are no longer reachable at all: the
SDK will not emit malformed JSON or a duplicate request id, because it does
not build the frames by hand any more either. Those cases moved to
`tests/test_faults_ch09.py`, which drives `normalise` directly and says so.

Environment variables, all optional, each one a fault this server reproduces:

  MCP_STARTUP_DELAY   seconds to sleep before serving (F09-04)
  MCP_DIE_AFTER       exit(1) after this many `tools/call` requests (F09-05)
  MCP_CRASH_AFTER     die with a traceback on stderr, after this many calls
  MCP_TOOL_PREFIX     prefix every tool name, to build a 60-tool server (F09-02)
  MCP_EXTRA_TOOLS     add N filler tools with realistic schemas (F09-02/F09-03)
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

from mcp.server.mcpserver import Context, MCPServer

ROOT = Path(os.environ.get("MCP_ROOT", ".")).resolve()
PREFIX = os.environ.get("MCP_TOOL_PREFIX", "")

server = MCPServer("files")
_calls = 0


def _budget() -> None:
    """Die on schedule, if this run was told to.

    Called at the top of every tool, because the two ways a server can leave
    are different faults: `sys.exit` is a clean disappearance with nothing on
    stderr, and an unhandled error leaves a traceback. A client that can only
    report the first has nothing to tell the user (F09-05).
    """
    global _calls
    _calls += 1
    die_after = int(os.environ.get("MCP_DIE_AFTER", "0"))
    if die_after and _calls > die_after:
        # `os._exit`, not `sys.exit`: inside the SDK's request handling a
        # `SystemExit` is just another exception and gets turned into a
        # perfectly polite error result. This fault needs the process gone.
        os._exit(1)
    crash_after = int(os.environ.get("MCP_CRASH_AFTER", "0"))
    if crash_after and _calls > crash_after:
        sys.stderr.write("MemoryError: index too large to load\n")
        sys.stderr.flush()
        os._exit(70)


async def _noise(ctx: Context) -> None:
    """Emit a perfectly legal log notification before answering.

    `MCP_LOG_NOISE` is how this chapter proves the pipe carries more than
    answers.  A server may send `notifications/message` whenever it likes, and
    the naive client this chapter opened with read that notification as the
    result of `initialize` -- every reply after it off by one, nothing raised.

    Keeping the switch after the SDK rewrite is the point: the *fault* did not
    go away, the *fix* moved.  Demultiplexing responses from notifications
    from server-initiated requests is now the SDK's job, and the test that
    used to prove our reader did it correctly now proves theirs does.
    """
    if os.environ.get("MCP_LOG_NOISE"):
        await ctx.info("handling a call")


@server.tool(name=f"{PREFIX}search")
async def search(query: str, ctx: Context) -> str:
    """Search the indexed files for a literal string."""
    await _noise(ctx)
    _budget()
    hits = [
        p.name
        for p in sorted(ROOT.glob("*"))
        if p.is_file() and query in p.read_text(encoding="utf-8", errors="replace")
    ]
    return "\n".join(hits) if hits else "no matches"


@server.tool(name=f"{PREFIX}stat", annotations={"readOnlyHint": True})
async def stat(name: str, ctx: Context) -> str:
    """Return the size in bytes of one indexed file."""
    await _noise(ctx)
    _budget()
    target = ROOT / name
    if not target.is_file():
        # Raising is what produces `isError: true` under the SDK, and it is
        # the shape chapter 0's rule needs: a failed tool is text the model
        # acts on, not a dead connection.
        raise FileNotFoundError(f"no such file: {target.name}")
    return str(target.stat().st_size)


def _add_filler(index: int) -> None:
    """One more tool with a realistic schema, for the budget measurements."""

    def _filler(target: str, dry_run: bool = False, limit: int = 0) -> str:
        _budget()
        return f"filler_{index:02d} ok"

    # The description is set here rather than as a docstring because an
    # f-string is not one: `f"""..."""` inside a function body is an
    # expression that is evaluated and discarded, so the tool would ship with
    # no description at all and the schema-budget measurements -- which are
    # about how much description costs -- would quietly measure nothing.
    _filler.__doc__ = (
        f"Filler tool number {index}. Performs operation {index} against the "
        "configured backend and returns a structured report of what it did."
    )
    server.tool(name=f"{PREFIX}filler_{index:02d}")(_filler)


for _index in range(int(os.environ.get("MCP_EXTRA_TOOLS", "0"))):
    _add_filler(_index)


if __name__ == "__main__":
    delay = float(os.environ.get("MCP_STARTUP_DELAY", "0"))
    if delay:
        # Before `run()`, so the client's startup deadline is what expires --
        # the server has not begun speaking the protocol at all yet, which is
        # exactly F09-04's shape.
        time.sleep(delay)
    server.run()
