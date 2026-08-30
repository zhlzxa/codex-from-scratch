"""A second MCP server, which also happens to expose a tool called `search`.

That collision is the whole point (F09-01). Two servers written by two teams
who never met both pick the most obvious name for the most obvious tool, and
neither is wrong.

It also exposes the two shapes chapter 9 needs that `files_server.py` does not:

  - `render`: a result with an image block and an embedded resource block, so
    the normalising adapter has something other than plain text to normalise
    (F09-07).
  - `remember`: a tool that *writes*, is not read-only, and needs consent
    before it runs -- the server asks for it with an `elicitation/create`
    request of its own (F09-06).

Rewritten onto the official SDK along with everything else in this chapter;
see `minicodex/mcp.py`'s docstring for why.

**Why this is a subprocess and not an in-process server.** `mcp.Client` also
accepts a server *object* and speaks to it inside the calling process, which
is faster and still exercises the real protocol. It is not usable here for
two reasons, and both are this chapter's subject:

  - half of chapter 9's faults need a process that can **die** -- hang at
    startup, exit mid-call, leave a traceback on stderr. An in-process server
    cannot; it is the test.
  - that transport has **no back-channel**, so `remember`'s question to the
    client raises there regardless of protocol version:

        NoBackChannelError: Cannot send 'elicitation/create': this transport
        context has no back-channel for server-initiated requests.

The rule worth carrying away: **the in-process transport tests logic, not
processes.** Worth knowing before designing a suite around the fast option.

Environment variables:

  MCP_ELICIT      `remember` asks the client for confirmation before writing
  MCP_NOTES_DB    where `remember` writes (default: `notes.json` in cwd)
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
from pathlib import Path

from mcp.server.mcpserver import Context, MCPServer
from mcp_types import (
    CallToolResult,
    EmbeddedResource,
    ImageContent,
    TextContent,
    TextResourceContents,
)
from pydantic import BaseModel

NOTES = {
    "meeting": "Ship chapter 9 before the scheduler work goes stale.",
    "idea": "A tool nobody can find is a tool nobody calls.",
}

# A real 1x1 transparent PNG, so `render` returns an actual base64 image block
# rather than a made-up string that the adapter would never have to survive.
PIXEL = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGA"
    "hKmMIQAAAABJRU5ErkJggg=="
)
assert base64.b64decode(PIXEL).startswith(b"\x89PNG")

server = MCPServer("notes")


class Confirm(BaseModel):
    """The one-boolean schema chapter 5's approver can actually answer.

    `registry.elicitation_handler` declines anything wider than this on
    purpose: it can ask a human yes or no, and inventing a value to fill a
    richer schema would put made-up data into somebody else's system.
    """

    confirm: bool


@server.tool()
def search(query: str) -> str:
    """Search saved notes for a word."""
    needle = query.lower()
    hits = [f"{k}: {v}" for k, v in NOTES.items() if needle in k or needle in v.lower()]
    return "\n".join(hits) or "no notes match"


@server.tool()
def render(name: str) -> CallToolResult:
    """Render one note as text, an image and an embedded resource.

    Returns a whole `CallToolResult` rather than a list of blocks, because
    F09-07 needs `structuredContent` *alongside* the blocks and returning a
    list only fills in `content`.  A server that has both is the shape the
    normaliser has to survive.
    """
    return CallToolResult(
        content=[
            TextContent(type="text", text=f"note {name!r} rendered"),
            ImageContent(type="image", data=PIXEL, mimeType="image/png"),
            EmbeddedResource(
                type="resource",
                resource=TextResourceContents(
                    uri=f"notes://{name}",
                    mimeType="text/plain",
                    text=NOTES.get(name, ""),
                ),
            ),
        ],
        structuredContent={"note": name, "bytes": len(NOTES.get(name, ""))},
    )


@server.tool()
async def remember(name: str, text: str, ctx: Context) -> str:
    """Save a note to disk. Not read-only."""
    if os.environ.get("MCP_ELICIT"):
        answer = await ctx.elicit(f"Save note {name!r} to disk?", schema=Confirm)
        accepted = answer.action == "accept" and getattr(answer.data, "confirm", False)
        if not accepted:
            # Raising rather than returning: `isError` is what says "this tool
            # ran and refused", and chapter 0's rule is that the model reads
            # that as information rather than as a broken connection.
            raise PermissionError("not saved: the user declined")

    # Through a thread: this is an `async def`, and blocking IO on the event
    # loop is the rule chapter 0 turned on and ruff's ASYNC240 enforces. The
    # same reason `_scratch` exists in the test file.
    await asyncio.to_thread(_save, name, text)
    return f"saved {name!r}"


def _save(name: str, text: str) -> None:
    path = Path(os.environ.get("MCP_NOTES_DB", "notes.json"))
    db = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    db[name] = text
    path.write_text(json.dumps(db), encoding="utf-8")


if __name__ == "__main__":
    server.run()
