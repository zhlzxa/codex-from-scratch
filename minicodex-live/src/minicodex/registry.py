"""Where remote tools become tools this agent can offer, call and schedule.

`mcp.py` knows about subprocesses and about somebody else's HTTPS.  `tools.py`
knows about this repository.  This module is the seam: it takes `RemoteTool`s
from any number of servers and produces the three things the rest of the
program already understands -- a schema list for the model, a `{name: handler}`
table for the loop, and a `Footprint` for the scheduler's scheduler.

Four decisions here are the registry's, and each was measured before it was
written:

  - **Names are rewritten, calls are not.**  Two servers both call their tool
    `search`; merging them into one dict silently loses one of them and
    answers with the other (01).  The model sees `mcp__notes__search`; the
    wire still sees `search`.
  - **Schemas are shown until they do not fit, then replaced by an index.**
    Sixty schemas cost more tokens than everything else in the request put
    together (03), on every turn, forever.  But deferring them behind a
    `tool_search` is a token fix that *costs accuracy*: measured on the same
    sixty-one tools, showing every schema found the right one 3/3 and hiding
    them found it 0/6.  So the trigger is a token budget, not a tool count.
  - **Results are normalised.**  A server may answer with text, an image, an
    embedded resource, structured JSON, or a flag saying the whole thing
    failed (07).  The loop's contract is that a tool returns one string.
  - **A remote footprint is a promise, not a fact.**  The scheduler refused to let
    a tool guess what it touches; a server's `readOnlyHint` is exactly such a
    guess, made by somebody else's code.

What this module adds is at the edges: `load_config` now reads a `url` beside
`command` and refuses a server that declares both or neither (codex's own
`RawMcpServerConfig` folds the same two shapes into one struct,
`codex-rs/config/src/mcp_types.rs:274-291`), and `connect` passes a sandbox and
an owner through to whichever kind of `McpClient` gets built.  Nothing about
*this* module's four decisions changed: a remote tool still gets a rewritten
name, still competes for the same schema budget, still comes back through the
same `normalise`, and its footprint is trusted exactly as little as the registry
trusted a local server's.
"""

from __future__ import annotations

import asyncio  # for `asyncio.TimeoutError` (the 3.10-portable spelling; see mcp.py)
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from minicodex.agent_types import ToolCall, ToolFn
from minicodex.approval import ApprovalRequest, Session
from minicodex.clip import clip
from minicodex.mcp import (
    AnyServerConfig,
    McpClient,
    McpError,
    RemoteTool,
    RequestHandler,
    ServerConfig,
)
from minicodex.policy import Risk
from minicodex.remote import RemoteOAuthConfig, RemoteServerConfig
from minicodex.scheduler import STATEFUL, Footprint
from minicodex.tenancy import DEFAULT_OWNER, Owner
from minicodex.tokens import estimate_messages
from minicodex.tool_errors import tool_error

if TYPE_CHECKING:  # pragma: no cover - typing only
    from minicodex.remote import CallbackHandler, RedirectHandler
    from minicodex.sandbox import Sandbox

# The model-visible name of every remote tool starts here, so that "is this
# tool remote?" is a string test anywhere in the program and does not need the
# registry to answer it.
PREFIX = "mcp"
DELIMITER = "__"

# Measured, not read: `probe_mcp.py names` sends real tool names to the real
# API and reads the 400s.  The pattern is `^[a-zA-Z0-9_-]+$` -- a dot, a space
# or a slash in a server name is a rejected *request*, not a mangled tool name,
# so `sanitize()` is load-bearing rather than tidy.  The length limit is 128,
# which is worth writing down because the number this constant was first given
# was 64, from memory, and 65 characters went through without complaint.
MAX_TOOL_NAME = 128

# How much of a remote result the model is allowed to see.  The shell clips
# its output and compaction clips history items; an MCP result reaches the
# history through neither, which is the unbounded-tool-result hole in a new
# shape.
MAX_RESULT_CHARS = 20_000

# How many tools `tool_search` returns when the model does not say.
DEFAULT_SEARCH_LIMIT = 5

# When the rendered schemas cost more than this, they are replaced by an index
# and a `tool_search`.  A **token** budget rather than a tool count, and that
# is the whole finding of the registry's largest measurement: with sixty-one
# realistic tools, showing every schema found the right one 3/3 while deferring
# them behind a search found it 0/6.  Deferring is not an accuracy improvement
# to reach for at some tool count; it is what you do when the schemas will not
# fit, and it costs something every time.
#
# 4000 is a budget, not a cliff: it is what this project is willing to spend on
# tool schemas out of a small model's window, on every single turn.  A larger
# window can afford a larger number, which is exactly why it is a constructor
# argument.
DEFAULT_SCHEMA_BUDGET = 4000

_UNSAFE = re.compile(r"[^a-zA-Z0-9_-]")


def sanitize(part: str) -> str:
    """Make one name component legal as part of a function name.

    Lossy on purpose, and the loss is why `RemoteTool.name` is kept: `a.b` and
    `a-b` both become `a_b`, so the sanitized name cannot be turned back into
    the name the server knows.  Only the forward direction is ever needed.
    """
    return _UNSAFE.sub("_", part)


def model_name(server: str, tool: str) -> str:
    return f"{PREFIX}{DELIMITER}{sanitize(server)}{DELIMITER}{sanitize(tool)}"


def is_remote(name: str) -> bool:
    return name.startswith(f"{PREFIX}{DELIMITER}")


@dataclass(frozen=True)
class Registration:
    """One remote tool, under the name the model will use for it."""

    model_name: str
    tool: RemoteTool
    client: McpClient


def _field(block: Any, *names: str, default: Any = None) -> Any:
    """One block's field, whichever spelling this object uses.

    The SDK hands back typed objects with snake_case attributes
    (`is_error`, `input_schema`) for fields the wire spells in camelCase
    (`isError`, `inputSchema`).  Tests and `load_config` still deal in plain
    dicts, and a server may send a shape the SDK models as an "unknown"
    block.  Rather than convert everything to one representation at the door
    -- which would mean choosing which of the two is real -- this reads
    whichever is there.

    Not cleverness for its own sake: 07's whole point is that this
    function must survive shapes it did not expect, and an attribute lookup
    that raises on a dict is exactly the brittleness that fault is about.
    """
    for name in names:
        if isinstance(block, dict):
            if name in block:
                return block[name]
        elif hasattr(block, name):
            value = getattr(block, name)
            if value is not None:
                return value
    return default


def _echoes(structured: Any, parts: list[str]) -> bool:
    """Does `structuredContent` only repeat what the text blocks already said?

    12, and it arrived with the SDK rather than from any server.  MCP says
    a server sending `structuredContent` SHOULD also send the same data as a
    text block, so older clients still see an answer -- and the SDK obliges
    automatically for any tool with a typed return.  A tool that returns
    `"no notes match"` therefore comes back as *both* that text and
    `{"result": "no notes match"}`, and rendering both gives:

        no notes match
        structuredContent: {"result": "no notes match"}

    Twice the tokens, on every call to every remote tool, saying one thing.
    Nothing raised; the tests that caught it were asserting on the answer.

    The rule is deliberately narrow: drop the structured line only when it is
    a single-key wrapper whose value the text already carries verbatim.  A
    genuine structured payload -- several fields, or fields the text does not
    mention -- is exactly the case `structuredContent` exists for, and still
    goes through.  Guessing more aggressively would throw away the machine-
    readable half of a result to save a few tokens.
    """
    if not isinstance(structured, dict) or len(structured) != 1:
        return False
    (value,) = structured.values()
    if not isinstance(value, str | int | float | bool) or isinstance(value, bool):
        return False
    return str(value) in parts


def normalise(result: Any) -> str:
    """Turn one MCP result into the single string the loop promised.

    MCP results are a list of typed blocks plus two optional extras, and
    servers disagree about which of them carries the answer.  Every shape has
    to survive this function, including the ones that carry no text at all:
    a result rendered as an empty string is indistinguishable from a tool that
    succeeded and had nothing to say, and the model treats that as an answer.

    Binary payloads are described, not included.  A base64 PNG is tokens the
    model cannot read (unless it is multi-modal, which the token estimator refuses
    business) and it would land in the history at full length.

    Takes `Any` rather than `CallToolResult`: the client returns the SDK's
    typed result, but this function is also handed plain
    dicts by tests that need to express a shape no well-behaved server would
    produce.  Accepting both is what lets 07's table stay a table.
    """
    blocks = _field(result, "content", default=None)
    parts: list[str] = []
    if isinstance(blocks, list):
        for block in blocks:
            if not isinstance(block, dict) and not hasattr(block, "type"):
                parts.append(f"[non-object content block: {type(block).__name__}]")
                continue
            kind = _field(block, "type")
            if kind == "text":
                parts.append(str(_field(block, "text", default="")))
            elif kind == "image":
                mime = _field(block, "mimeType", "mime_type", default="image/?")
                size = len(str(_field(block, "data", default="")))
                parts.append(f"[image omitted: {mime}, {size} base64 characters]")
            elif kind == "audio":
                mime = _field(block, "mimeType", "mime_type", default="audio/?")
                parts.append(f"[audio omitted: {mime}]")
            elif kind == "resource":
                resource = _field(block, "resource", default=None) or {}
                uri = _field(resource, "uri", default="?")
                body = _field(resource, "text", default=None)
                if isinstance(body, str):
                    parts.append(f"[resource {uri}]\n{body}")
                else:
                    parts.append(f"[binary resource omitted: {uri}]")
            elif kind == "resource_link":
                parts.append(f"[resource link: {_field(block, 'uri', default='?')}]")
            else:
                parts.append(f"[unsupported content block of type {kind!r}]")

    structured = _field(result, "structuredContent", "structured_content", default=None)
    if structured is not None:
        if hasattr(structured, "model_dump"):
            structured = structured.model_dump(mode="json")
        if not _echoes(structured, parts):
            parts.append("structuredContent: " + json.dumps(structured, ensure_ascii=False))

    text = "\n".join(part for part in parts if part) or "(the tool returned no content)"

    if _field(result, "isError", "is_error", default=False):
        # The call succeeded at the protocol level and failed at the tool
        # level.  Two different failures, one of which the loop already has a
        # rule for: it is text the model acts on.  Saying so is the difference
        # between the model retrying and the model believing it.
        text = f"The tool reported an error.\n{text}"

    return clip(text, MAX_RESULT_CHARS)


class McpRegistry:
    """Every remote tool this session has, and what the model may see of them.

    Owns `visible`, the list of schemas the model is shown.  That list is
    handed to the model client *by reference* and mutated in place when
    `tool_search` reveals something: the client reads it when it builds each
    request, so a tool revealed on turn 3 is callable on turn 4 without
    anything having to rebuild anything.  Handing over a copy compiles, runs,
    and silently defeats the entire mechanism -- there is a test named after
    that mistake.
    """

    def __init__(
        self,
        *,
        schema_budget: int = DEFAULT_SCHEMA_BUDGET,
        local: list[dict[str, Any]] | None = None,
    ) -> None:
        self.schema_budget = schema_budget
        # This project's own tools, always shown and never deferred.  They are
        # in here rather than concatenated by the caller so that `visible` is
        # the *whole* tool list -- one object, handed to the model client once,
        # correct on every turn.  Two lists that have to be concatenated at
        # each call site is how one of them gets forgotten.
        self.local = list(local or [])
        self.registrations: dict[str, Registration] = {}
        self.visible: list[dict[str, Any]] = list(self.local)
        self.deferred: set[str] = set()
        # Servers that were configured and did not come up, kept so that the
        # failure can be reported once at startup and again -- with the reason
        # -- if the model asks for something that server would have provided.
        self.failures: dict[str, str] = {}
        # Server name -> the raw tool names it offered, so a call to a tool
        # that no longer exists can say what happened to it (08).
        self.retired: dict[str, str] = {}

    # -- building it ---------------------------------------------------------

    def add(self, client: McpClient, tools: list[RemoteTool]) -> list[str]:
        """Register one server's tools.  Returns the names that were added."""
        added: list[str] = []
        for tool in tools:
            name = model_name(tool.server, tool.name)
            if len(name) > MAX_TOOL_NAME:
                # Refused rather than truncated: truncation is how two distinct
                # tools become one name, which is 01 again with the
                # registry as the culprit instead of the servers.
                self.retired[name[:MAX_TOOL_NAME]] = (
                    f"{tool.server}/{tool.name} was not registered: its name is "
                    f"{len(name)} characters and the limit is {MAX_TOOL_NAME}"
                )
                continue
            if name in self.registrations:
                # Same server, same sanitized name, different raw names --
                # `a.b` and `a-b` both sanitize to `a_b`.  Same rule as above.
                self.retired[name] = (
                    f"{tool.server}/{tool.name} was not registered: its name collides "
                    f"with {self.registrations[name].tool.name} after sanitising"
                )
                continue
            self.registrations[name] = Registration(name, tool, client)
            added.append(name)
        self._restage()
        return added

    def forget(self, server: str, reason: str | None = None) -> None:
        """Drop a server's tools, remembering that they were once there.

        Remembering matters more than dropping.  The model has the old names
        in its context and will use them; the loop's "no tool named X" was
        written for names a model *invented*, and telling it that about a tool
        it genuinely had ten seconds ago is a lie that makes it try harder.
        """
        because = reason or f"the MCP server {server!r} is no longer connected"
        for name, registration in list(self.registrations.items()):
            if registration.tool.server == server:
                self.retired[name] = because
                del self.registrations[name]
        self._restage()

    async def reconnect(self, server: str) -> str:
        """Restart one server and re-read its tool list.

        Re-read, not restore: a server that has been restarted may come back
        with a different set of tools, and pretending otherwise leaves
        registrations pointing at names the new process does not answer to.
        The tool list is the server's to declare, every time it starts.
        """
        registrations = [r for r in self.registrations.values() if r.tool.server == server]
        client = registrations[0].client if registrations else None
        if client is None:
            return f"{server} is not a server this session knows about"
        await client.close()
        client.failure = None
        try:
            await client.start()
            tools = await client.list_tools()
        except (McpError, asyncio.TimeoutError, OSError) as exc:
            self.failures[server] = str(exc)
            self.forget(server, f"the MCP server {server!r} stopped and would not restart: {exc}")
            return f"{server} could not be restarted: {exc}"
        before = {r.model_name for r in registrations}
        self.forget(server, f"the MCP server {server!r} restarted without this tool")
        self.add(client, tools)
        after = {name for name, r in self.registrations.items() if r.tool.server == server}
        # Deliberately not silent.  A tool set that changed under the model is
        # the thing that makes its next call fail, and a log line here is the
        # only place a human ever finds out it happened.
        gone = sorted(before - after)
        new = sorted(after - before)
        changes = []
        if gone:
            changes.append(f"gone: {', '.join(gone)}")
        if new:
            changes.append(f"new: {', '.join(new)}")
        return f"{server} restarted ({'; '.join(changes) or 'same tools'})"

    def _restage(self) -> None:
        """Decide what is shown in full and what is shown only by name.

        `visible` is mutated in place rather than rebound, because the model
        client holds a reference to this exact list object.

        The `tool_search` schema is rebuilt every time the deferred set
        changes -- its description carries the index of what is still hidden,
        so the index cannot go stale while the list it describes does not.
        """
        names = sorted(self.registrations)
        schemas = [self.schema_for(name) for name in names]
        # All or nothing, not a greedy fill.  A partial list is the worst of
        # both: the model pays for schemas *and* has to know that what it can
        # see is not everything, and which half it got is decided by
        # alphabetical order, which means nothing to anybody.  The budget is
        # measured against the whole list the model receives, this project's
        # own tools included -- they are not free either.
        if not names or estimate_messages((), self.local + schemas) <= self.schema_budget:
            self.deferred = set()
            self.visible[:] = self.local + schemas
        else:
            self.deferred = set(names)
            self.visible[:] = [*self.local, self.search_schema()]

    def reveal(self, names: list[str]) -> list[str]:
        """Move tools from name-only to full schema in the model's tool list."""
        revealed = []
        for name in names:
            if name in self.deferred:
                self.deferred.discard(name)
                self.visible.append(self.schema_for(name))
                revealed.append(name)
        if revealed:
            for index, schema in enumerate(self.visible):
                if schema["function"]["name"] == "tool_search":
                    self.visible[index] = self.search_schema()
                    break
        return revealed

    def schema_for(self, name: str) -> dict[str, Any]:
        registration = self.registrations[name]
        return {
            "type": "function",
            "function": {
                "name": name,
                "description": registration.tool.description,
                "parameters": registration.tool.input_schema,
            },
        }

    # -- using it ------------------------------------------------------------

    def handlers(self) -> dict[str, ToolFn]:
        """A handler per registered tool, deferred ones included.

        Deferring is about the *schema list*, not about what may be called: a
        tool the model learned of through `tool_search` has to be callable on
        the very next turn, and a tool it remembers from before a refresh has
        to reach a real error rather than the loop's "no tool named" message,
        which was written for names the model invented.
        """
        return {name: self._handler(name) for name in self.registrations}

    def _handler(self, name: str) -> ToolFn:
        async def handler(arguments: dict[str, Any]) -> str:
            registration = self.registrations.get(name)
            if registration is None:  # forgotten between rendering and calling
                return tool_error(
                    f"{name} is no longer available",
                    you_sent=json.dumps(arguments)[:200],
                    do_this=self.explain(name),
                )
            stopped = registration.client.failure
            if not registration.client.alive:
                # One restart, here, rather than a background supervisor: a
                # server is only worth restarting when something wants to use
                # it, and doing it on the call keeps the retry inside the
                # turn budget the model can see.  `reconnect` may return a
                # different tool set, which is why the registration is looked
                # up again below instead of being reused.
                report = await self.reconnect(registration.tool.server)
                registration = self.registrations.get(name)  # type: ignore[assignment]
                if registration is None:
                    return tool_error(
                        f"{name} could not be called",
                        you_sent=json.dumps(arguments)[:200],
                        do_this=(
                            f"The server providing it stopped ({stopped or 'no reason reported'})"
                            f" and {report}. Use a local tool instead, or tell the user."
                        ),
                    )
            try:
                result = await registration.client.call(registration.tool.name, arguments)
            except (asyncio.TimeoutError, McpError) as exc:
                # Not retried, on purpose.  The call above was in flight when
                # the server stopped answering, so whether its side effect
                # happened is unknown -- and "unknown" is not a state to
                # resolve by doing it again.  The *next* call finds a dead
                # client and restarts it, which is a retry of something that
                # provably never started.
                return tool_error(
                    f"{name} did not return",
                    you_sent=json.dumps(arguments)[:200],
                    do_this=f"The server said: {exc}. Try a different approach.",
                )
            return normalise(result)

        return handler

    def explain(self, name: str) -> str:
        """What to tell the model about a name that is not registered."""
        if name in self.retired:
            return f"{self.retired[name]}. Use one of the tools you were given instead."
        return "Use one of the tools you were given instead."

    def footprint_of(self, call: ToolCall) -> Footprint:
        """What a remote call touches -- as far as anyone here can honestly say.

        The scheduler's rule was that a footprint must be resolved, not guessed,
        and this is the first tool set where that is impossible: the work
        happens in another process, on machines this one cannot see.

        The one thing a server does tell us is `annotations.readOnlyHint`, and
        that is a promise rather than an observation.  It is trusted for
        exactly one conclusion -- two read-only calls *to the same server* may
        overlap -- and for nothing else.  In particular a read-only remote tool
        is **not** given an empty footprint: it may still read a file this
        turn's `apply_patch` is writing, and nothing here can prove otherwise.
        Anything without the hint is `STATEFUL`, which is what the scheduler's
        default already said about tools it did not recognise.
        """
        registration = self.registrations.get(call.name)
        if registration is None or registration.tool.read_only is not True:
            return STATEFUL
        return Footprint(reads=frozenset({f"mcp:{registration.tool.server}"}))

    # -- the search tool -----------------------------------------------------

    def search(self, query: str, limit: int = DEFAULT_SEARCH_LIMIT) -> str:
        """Rank the deferred tools against a query and reveal the best ones.

        Substring scoring over name and description, not embeddings: the
        catalogue is tens of tools, the query is the model's own words, and
        every extra moving part here is a thing that can be subtly wrong in a
        way nobody notices.  codex's own `tool_search` is the same idea with a
        larger catalogue behind it.
        """
        terms = [term for term in re.split(r"[^a-zA-Z0-9]+", query.lower()) if term]
        scored: list[tuple[int, str]] = []
        for name in sorted(self.deferred):
            registration = self.registrations[name]
            haystack = f"{name} {registration.tool.description}".lower()
            score = sum(1 for term in terms if term in haystack)
            if score:
                scored.append((-score, name))
        scored.sort()
        chosen = [name for _, name in scored[: max(1, limit)]]
        remaining = len(self.deferred) - len(chosen)
        if not chosen:
            return (
                f"No tool matched {query!r}. {len(self.deferred)} tools are still "
                "unloaded; their names and descriptions are in this tool's own "
                "description. Search again using words from that list."
            )
        self.reveal(chosen)
        rendered = "\n".join(
            json.dumps({"name": name, **self.schema_for(name)["function"]}, ensure_ascii=False)
            for name in chosen
        )
        # The last sentence is not decoration.  Measured: given only the
        # matches, the model treats a near-miss as the best available answer
        # and stops -- 0/3, every run ending in "could you narrow this down"
        # while the tool it wanted sat unloaded.  A search result is a prompt,
        # and this one has to say what to do when the results are wrong
        # (the tool_error convention).
        return (
            f"{len(chosen)} tool(s) loaded and now callable:\n"
            f"<functions>\n{rendered}\n</functions>\n"
            f"{remaining} other tool(s) are still unloaded. If none of the above is "
            "right for the task, call tool_search again with different words from "
            "the list in its description -- do not settle for a tool that is only "
            "approximately right, and do not ask the user for something the "
            "unloaded tools could find."
        )

    def index(self) -> str:
        """Every deferred tool, one line each: the name and its first sentence.

        This is the half of two-stage loading that the first version left out,
        and leaving it out was measured: with nothing but a search box, the
        model wrote a query from the *task* ("meeting") rather than from what
        exists, got five tools that matched that word, and never saw the one
        it needed -- 0/6, and in every one of those runs it then asked the user
        for help rather than searching again.

        The names are not free.  Measured on the sixty-tool catalogue: full
        schemas 7488 tokens, this index 1399, names alone 469.  The one-line
        descriptions are four fifths of the cost of the index and are what
        makes a query possible at all, so they stay.
        """
        lines = []
        for name in sorted(self.deferred):
            description = self.registrations[name].tool.description.strip()
            first = description.split(". ")[0].rstrip(".")
            lines.append(f"- {name}: {first}" if first else f"- {name}")
        return "\n".join(lines)

    def search_schema(self) -> dict[str, Any]:
        sources = sorted({r.tool.server for r in self.registrations.values()})
        return {
            "type": "function",
            "function": {
                "name": "tool_search",
                "description": (
                    "Load the full parameter schema for tools that are listed below by "
                    "name only, so that you can call them. You cannot call a tool from "
                    "this list until you have loaded it. Sources connected: "
                    f"{', '.join(sources) or '(none)'}.\n\n"
                    f"Tools available but not yet loaded:\n{self.index()}"
                ),
                "parameters": {
                    "type": "object",
                    "required": ["query"],
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": (
                                "Words from the names or descriptions listed above. "
                                "Example: notes search keyword"
                            ),
                        },
                        "limit": {
                            "type": "integer",
                            "description": (
                                f"How many tools to reveal. Defaults to {DEFAULT_SEARCH_LIMIT}."
                            ),
                        },
                    },
                },
            },
        }

    def search_handler(self) -> ToolFn:
        async def handler(arguments: dict[str, Any]) -> str:
            query = arguments.get("query")
            if not isinstance(query, str):
                return tool_error(
                    'tool_search needs a "query" argument, a string',
                    you_sent=repr(arguments.get("query")),
                    do_this='Example: {"query": "search saved notes by keyword"}',
                )
            limit = arguments.get("limit")
            return self.search(query, limit if isinstance(limit, int) else DEFAULT_SEARCH_LIMIT)

        return handler


def elicitation_handler(session: Session) -> RequestHandler:
    """Answer a server's `elicitation/create` with the approval gate's approver.

    MCP lets a server ask the *client* a question mid-call -- a confirmation,
    a missing field, an OAuth consent.  There is no new approval machinery
    here on purpose: the approval module already decided who gets asked and how, and a
    second prompt style for the same question is how a user learns to answer
    without reading.

    Two shapes are refused rather than guessed at.  A server may request a
    whole object of fields, and this client can answer exactly one kind of
    question -- yes or no.  `decline` is a legal MCP answer and means "the
    user said no"; the alternative, making something up to fill the schema,
    would put an invented value into somebody else's system.
    """

    async def handle(params: dict[str, Any]) -> dict[str, Any]:
        message = str(params.get("message") or "a server is asking for confirmation")
        schema = params.get("requestedSchema") or {}
        properties = schema.get("properties") or {}
        booleans = [
            key for key, spec in properties.items() if (spec or {}).get("type") == "boolean"
        ]
        if len(properties) != 1 or not booleans:
            return {"action": "decline"}
        reply = await session.approver.ask(
            ApprovalRequest(
                what=message,
                reason="an MCP server is asking before it acts",
                risk=Risk.UNKNOWN,
                suggested_rule=None,
            )
        )
        if not reply.approved:
            return {"action": "decline"}
        return {"action": "accept", "content": {booleans[0]: True}}

    return handle


def load_config(path: Path) -> list[AnyServerConfig]:
    """Read `{"servers": {"name": {...}}}`, stdio or remote.

    A file rather than a flag, and JSON rather than anything cleverer, because
    it is the same shape codex uses (`mcp_servers` in its config) and because
    a command line with three servers on it is a command line nobody types
    twice.

    Which kind a server is is **derived**, not declared -- copied from codex,
    whose `RawMcpServerConfig` (`codex-rs/config/src/mcp_types.rs:274-291`)
    puts `command`/`args`/`env`/`cwd` and `url`/`bearer_token_env_var`/
    `http_headers`/`env_http_headers` in one struct with comments rather than a
    tag.  A user writing a config should not have to name a transport they
    have never heard of.

    Declaring **both** `command` and `url` is refused rather than resolved.
    There is no defensible winner -- picking `url` silently ignores a command
    the user wrote, picking `command` silently ignores a URL -- and either way
    the server that answers is not the one they think they configured.

    There is no `bearer_token` field, though codex has one (`mcp_types.rs:290`,
    and it is `#[schemars(skip)]` -- hidden from its own generated schema).  A
    config file gets committed, pasted into an issue and read over a shoulder;
    only the *name* of an environment variable is accepted here, for a token
    and for each entry of `env_http_headers`.

    OAuth is opted into with `"oauth": {"client_id": "...", "scope": "..."}` --
    absent means no OAuth for that server, present with no `client_id` means
    the SDK is free to attempt RFC 7591 dynamic registration (08b).
    """
    raw = json.loads(path.read_text(encoding="utf-8"))
    servers = raw.get("servers")
    if not isinstance(servers, dict):
        raise McpError(f"{path}: expected an object with a 'servers' key")
    configs: list[AnyServerConfig] = []
    for name, spec in servers.items():
        spec = spec or {}
        command = spec.get("command")
        url = spec.get("url")
        has_command = isinstance(command, list) and bool(command)
        has_url = isinstance(url, str) and bool(url)
        if has_command and has_url:
            raise McpError(
                f"{path}: server {name!r} has both 'command' and 'url'; "
                "a server is reached one way or the other"
            )
        if not has_command and not has_url:
            raise McpError(f"{path}: server {name!r} has neither a 'command' list nor a 'url'")
        if has_url:
            oauth_spec = spec.get("oauth")
            oauth = None
            if isinstance(oauth_spec, dict):
                oauth = RemoteOAuthConfig(
                    client_id=oauth_spec.get("client_id"),
                    scope=oauth_spec.get("scope"),
                )
            configs.append(
                RemoteServerConfig(
                    name=name,
                    url=url,
                    bearer_token_env_var=spec.get("bearer_token_env_var"),
                    http_headers={
                        str(k): str(v) for k, v in (spec.get("http_headers") or {}).items()
                    },
                    env_http_headers={
                        str(k): str(v) for k, v in (spec.get("env_http_headers") or {}).items()
                    },
                    oauth=oauth,
                    startup_timeout=float(spec.get("startup_timeout", 30.0)),
                    tool_timeout=float(spec.get("tool_timeout", 60.0)),
                )
            )
            continue
        configs.append(
            ServerConfig(
                name=name,
                command=tuple(str(part) for part in command),
                env={str(k): str(v) for k, v in (spec.get("env") or {}).items()},
                cwd=spec.get("cwd"),
                startup_timeout=float(spec.get("startup_timeout", 30.0)),
                tool_timeout=float(spec.get("tool_timeout", 60.0)),
            )
        )
    return configs


async def connect(
    configs: list[AnyServerConfig],
    registry: McpRegistry,
    *,
    handlers: dict[str, Any] | None = None,
    sandbox: Sandbox | None = None,
    owner: Owner = DEFAULT_OWNER,
    redirect_handler: RedirectHandler | None = None,
    callback_handler: CallbackHandler | None = None,
) -> list[McpClient]:
    """Start every configured server, and survive the ones that do not start.

    A server that fails is recorded and skipped.  It is not raised: an agent
    that refuses to run because one of five optional servers is broken is
    worse at its job than one that runs with four (04).

    `sandbox` reaches only stdio servers (14); `owner`,
    `redirect_handler` and `callback_handler` reach only remote servers with
    OAuth configured, and their defaults (the single-tenant `Owner`, a
    terminal prompt, a loopback server) are what a single-user CLI run gets
    without asking for anything -- a test passes its own to keep the flow
    offline and deterministic.
    """
    clients: list[McpClient] = []
    for config in configs:
        client = McpClient(
            config,
            handlers=handlers,
            sandbox=sandbox,
            owner=owner,
            redirect_handler=redirect_handler,
            callback_handler=callback_handler,
        )
        try:
            await client.start()
            registry.add(client, await client.list_tools())
        except (McpError, asyncio.TimeoutError, OSError) as exc:
            registry.failures[config.name] = str(exc)
            await client.close()
            continue
        clients.append(client)
    return clients


__all__ = [
    "DEFAULT_SCHEMA_BUDGET",
    "DEFAULT_SEARCH_LIMIT",
    "MAX_RESULT_CHARS",
    "MAX_TOOL_NAME",
    "McpRegistry",
    "Registration",
    "connect",
    "elicitation_handler",
    "is_remote",
    "load_config",
    "model_name",
    "normalise",
    "sanitize",
]
