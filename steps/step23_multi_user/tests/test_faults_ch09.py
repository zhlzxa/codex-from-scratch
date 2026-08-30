"""Chapter 9: many tools, from servers this process does not control.

Every test here starts real subprocesses and speaks the real protocol to them.
The servers live in `mcp_servers/` and are ours, which is the point: a test
that borrows somebody else's MCP server cannot ask it to fail on cue.

Nothing in this file touches the network.
"""

from __future__ import annotations

import asyncio
import json
import sys
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from minicodex.agent_types import ToolCall
from minicodex.approval import AllowAll, ApprovalReply, DenyAll, Session
from minicodex.composition import local_tools, with_remote_tools
from minicodex.mcp import McpClient, McpError, RemoteTool, ServerConfig
from minicodex.registry import (
    MAX_RESULT_CHARS,
    McpRegistry,
    connect,
    elicitation_handler,
    is_remote,
    load_config,
    model_name,
    normalise,
    sanitize,
)
from minicodex.scheduler import STATEFUL, Footprint, batches, conflicts
from minicodex.tokens import estimate_messages

SERVERS = Path(__file__).resolve().parent.parent / "mcp_servers"


def config(script: str, name: str, **env: str) -> ServerConfig:
    return ServerConfig(
        name=name,
        command=(sys.executable, str(SERVERS / script)),
        env=env,
        startup_timeout=20.0,
        tool_timeout=20.0,
    )


def files(name: str = "files", **env: str) -> ServerConfig:
    return config("files_server.py", name, **env)


def notes(name: str = "notes", **env: str) -> ServerConfig:
    return config("notes_server.py", name, **env)


def a_call(tool: str, **arguments: Any) -> ToolCall:
    """A `ToolCall` for a tool named `tool`.

    The parameter is not called `name`, which is what it was called first:
    `call("mcp__files__stat", name="a.py")` is a `TypeError`, because `name`
    is also the argument that tool takes.
    """
    return ToolCall("call_1", tool, arguments, json.dumps(arguments))


async def started(*configs: ServerConfig, **kwargs: Any) -> tuple[McpRegistry, list[McpClient]]:
    registry = McpRegistry(**kwargs.pop("registry_kwargs", {}))
    clients = await connect(list(configs), registry, **kwargs)
    return registry, clients


def _scratch(name: str) -> Path:
    """A file the notes server will write to, removed before and after.

    Built here rather than inline in the test because `Path.exists()` inside
    an `async def` is ruff's ASYNC240 -- blocking IO on the event loop, the
    same rule chapter 0 turned on and chapter 2 kept paying attention to.
    """
    path = Path(__file__).resolve().parent / name
    path.unlink(missing_ok=True)
    return path


async def stop(clients: list[McpClient]) -> None:
    for client in clients:
        await client.close()


# -- F09-01: two servers, one name -------------------------------------------


async def test_F09_01_a_flat_table_loses_a_tool_and_answers_with_the_other() -> None:
    """The naive merge, reproduced: not a crash, a wrong answer.

    The catalogue predicted a collision would announce itself.  It does not.
    One `search` overwrites the other, the model is offered three tools where
    four were declared, and asking for a *file* returns an answer about
    *notes* -- correctly formatted, confidently wrong.
    """
    registry, clients = await started(files(), notes())
    try:
        flat: dict[str, tuple[McpClient, RemoteTool]] = {}
        for registration in registry.registrations.values():
            flat[registration.tool.name] = (registration.client, registration.tool)

        assert len(registry.registrations) == 5
        assert len(flat) == 4  # `search` declared twice, kept once
        client, tool = flat["search"]
        assert tool.server == "notes"

        answer = normalise(await client.call("search", {"query": "hatchling"}))
        assert answer == "no notes match"  # the file containing it was never looked at
    finally:
        await stop(clients)


async def test_F09_01_namespacing_keeps_both_and_routes_each_to_its_own_server() -> None:
    registry, clients = await started(files(), notes())
    try:
        handlers = registry.handlers()
        assert set(handlers) == {
            "mcp__files__search",
            "mcp__files__stat",
            "mcp__notes__search",
            "mcp__notes__render",
            "mcp__notes__remember",
        }
        from_files = await handlers["mcp__files__search"]({"query": "hatchling"})
        from_notes = await handlers["mcp__notes__search"]({"query": "idea"})
        assert "pyproject.toml" in from_files
        assert "A tool nobody can find" in from_notes
    finally:
        await stop(clients)


def test_F09_01_the_wire_name_is_not_the_model_name() -> None:
    """Renaming for the model must not rename what goes to the server."""
    assert model_name("notes", "search") == "mcp__notes__search"
    assert is_remote("mcp__notes__search")
    assert not is_remote("read_file")


def test_F09_01_names_are_sanitised_because_the_provider_rejects_the_request() -> None:
    """Measured against the real API: the pattern is `^[a-zA-Z0-9_-]+$`.

    A dot in a server name is not a cosmetic problem.  It is an HTTP 400 for
    the whole request, so every tool in the session stops working, including
    the ones that have nothing to do with MCP.
    """
    assert sanitize("server.one") == "server_one"
    assert sanitize("my server/v2") == "my_server_v2"
    assert model_name("server.one", "tool.two-three") == "mcp__server_one__tool_two-three"
    for character in ".", " ", "/", "@":
        assert character not in model_name(f"a{character}b", f"c{character}d")


def test_F09_01_two_raw_names_that_sanitise_to_one_do_not_silently_merge() -> None:
    """`a.b` and `a b` both become `a_b`; the second is refused, not blended.

    The first version of this test used `a.b` and `a-b`, which do *not*
    collide -- the measured pattern allows a hyphen, so `sanitize` leaves it
    alone.  Worth keeping the correction visible: the set of characters that
    have to be replaced is the one the provider rejects, not the one that
    looks unusual.
    """
    registry = McpRegistry()
    registry.add(
        None,  # type: ignore[arg-type]
        [
            RemoteTool("s", "a.b", "first", {"type": "object"}),
            RemoteTool("s", "a b", "second", {"type": "object"}),
        ],
    )
    assert list(registry.registrations) == ["mcp__s__a_b"]
    assert registry.registrations["mcp__s__a_b"].tool.description == "first"
    assert "collides" in registry.retired["mcp__s__a_b"]
    assert model_name("s", "a-b") == "mcp__s__a-b"  # a hyphen is legal, left alone


# -- the transport: a response is not "the next line" ------------------------


async def test_a_notification_is_not_the_answer_to_the_last_request() -> None:
    """The fault the naive client had, and the reason the reader is a task.

    `MCP_LOG_NOISE` makes the server emit a legal `notifications/message`
    before every reply.  A client that reads one line per request reads that
    notification as the result of `initialize`, and every answer after it is
    off by one -- with no exception raised anywhere.
    """
    client = McpClient(files(MCP_LOG_NOISE="1"))
    await client.start()
    try:
        assert client.server_info.get("name") == "files"
        tools = {tool.name for tool in await client.list_tools()}
        assert tools == {"search", "stat"}
        assert normalise(await client.call("stat", {"name": "pyproject.toml"})).isdigit()
    finally:
        await client.close()


async def test_a_server_request_this_client_does_not_handle_fails_fast_not_forever() -> None:
    """The property that survived the protocol change (see F09-16).

    The original worry was that a server-initiated request nobody answers
    leaves the server waiting forever, and with it the call the model is
    blocked on.  Under protocol `2026-07-28` the request cannot be sent at
    all, so the shape of the failure changed -- but the *property* is the one
    that mattered and it still holds: the attempt ends promptly and says why,
    rather than hanging until a timeout expires.

    `McpError` rather than an error result, because this is a transport-level
    refusal and not a tool that ran and failed -- the distinction chapter 0
    drew and `call()` still keeps.
    """
    client = McpClient(notes(MCP_ELICIT="1"))
    await client.start()
    try:
        with pytest.raises(McpError, match="back-channel"):
            await client.call("remember", {"name": "x", "text": "y"})
    finally:
        await client.close()


# -- F09-03: what the schemas cost -------------------------------------------


def _tool(index: int) -> RemoteTool:
    return RemoteTool(
        "bigcorp",
        f"operation_{index:02d}",
        f"Run operation {index} against the configured backend and report on it.",
        {
            "type": "object",
            "required": ["target"],
            "properties": {
                "target": {"type": "string", "description": "What to operate on."},
                "dry_run": {"type": "boolean", "description": "Do not actually do it."},
            },
        },
    )


def test_F09_03_schemas_that_fit_the_budget_are_all_shown() -> None:
    registry = McpRegistry(schema_budget=4000)
    registry.add(None, [_tool(i) for i in range(5)])  # type: ignore[arg-type]
    assert registry.deferred == set()
    assert len(registry.visible) == 5
    assert all(schema["function"]["name"] != "tool_search" for schema in registry.visible)


def test_F09_03_schemas_that_do_not_fit_are_replaced_by_an_index() -> None:
    registry = McpRegistry(schema_budget=4000)
    registry.add(None, [_tool(i) for i in range(60)])  # type: ignore[arg-type]

    assert len(registry.deferred) == 60
    assert len(registry.visible) == 1
    search = registry.visible[0]
    assert search["function"]["name"] == "tool_search"

    full = estimate_messages((), [registry.schema_for(n) for n in sorted(registry.registrations)])
    deferred = estimate_messages((), registry.visible)
    assert deferred < full
    # The index is the cheap half of two-stage loading and the useful half:
    # without the names in the description, the model has to guess a query.
    for name in sorted(registry.deferred)[:3]:
        assert name in search["function"]["description"]


def test_F09_03_the_budget_counts_this_projects_own_tools_too() -> None:
    local = [
        {
            "type": "function",
            "function": {"name": "read_file", "description": "x" * 8000, "parameters": {}},
        }
    ]
    registry = McpRegistry(schema_budget=1000, local=local)
    registry.add(None, [_tool(0)])  # type: ignore[arg-type]
    assert registry.deferred == {"mcp__bigcorp__operation_00"}
    assert [schema["function"]["name"] for schema in registry.visible] == [
        "read_file",
        "tool_search",
    ]


async def test_F09_03_tool_search_reveals_into_the_same_list_the_model_client_holds() -> None:
    """The mistake this is named after: handing over a copy.

    `llm.tools = list(registry.visible)` type-checks, runs, and quietly makes
    `tool_search` do nothing at all -- the tool is revealed into a list the
    request builder is not reading.
    """
    registry = McpRegistry(schema_budget=200)
    registry.add(None, [_tool(i) for i in range(60)])  # type: ignore[arg-type]
    what_the_client_holds = registry.visible  # by reference, exactly as __main__ does

    output = await registry.search_handler()({"query": "operation 07"})
    assert "mcp__bigcorp__operation_07" in output
    assert "mcp__bigcorp__operation_07" in [
        schema["function"]["name"] for schema in what_the_client_holds
    ]
    assert "mcp__bigcorp__operation_07" not in registry.deferred


async def test_F09_03_a_search_that_matches_nothing_says_what_to_do_next() -> None:
    registry = McpRegistry(schema_budget=200)
    registry.add(None, [_tool(i) for i in range(60)])  # type: ignore[arg-type]
    output = await registry.search_handler()({"query": "photosynthesis"})
    assert "No tool matched" in output
    assert "search again" in output.lower()
    assert len(registry.deferred) == 60


async def test_F09_03_a_search_result_says_that_other_tools_remain() -> None:
    """Measured: without this sentence the model settles for a near-miss.

    Six runs out of six, given only the tools its query matched, gpt-4o-mini
    picked an approximately-right tool or asked the user to narrow the task
    down -- never searching a second time.  The sentence is a prompt, and
    chapter 3 (F03-07) is the rule it follows: say what to do next.
    """
    registry = McpRegistry(schema_budget=200)
    registry.add(None, [_tool(i) for i in range(60)])  # type: ignore[arg-type]
    output = await registry.search_handler()({"query": "operation 07"})
    assert "still unloaded" in output
    assert "tool_search again" in output


# -- F09-04: a server that will not start ------------------------------------


async def test_F09_04_a_slow_server_does_not_hold_the_agent_hostage() -> None:
    registry = McpRegistry()
    slow = ServerConfig(
        name="slow",
        command=(sys.executable, str(SERVERS / "files_server.py")),
        env={"MCP_STARTUP_DELAY": "30"},
        startup_timeout=1.0,
    )
    started_at = asyncio.get_running_loop().time()
    clients = await connect([slow], registry)
    elapsed = asyncio.get_running_loop().time() - started_at

    assert clients == []
    assert registry.registrations == {}
    assert "did not answer initialize within 1s" in registry.failures["slow"]
    assert elapsed < 10  # the timeout, not the server's 30 seconds


async def test_F09_04_one_broken_server_does_not_take_the_working_ones_with_it() -> None:
    """An agent that refuses to run because one optional server is down is
    worse at its job than one that runs with the rest."""
    registry, clients = await started(
        ServerConfig("ghost", ("definitely-not-a-program-xyz",)),
        notes(),
    )
    try:
        assert "ghost" in registry.failures
        assert [c.config.name for c in clients] == ["notes"]
        assert "mcp__notes__search" in registry.registrations
    finally:
        await stop(clients)


# -- F09-05: a server that dies mid-session ----------------------------------


async def test_F09_05_a_silent_death_is_all_the_sdk_can_tell_us() -> None:
    """What the SDK cost, asserted rather than glossed (F09-15).

    This test used to assert `"exited with code 1"`, built from
    `proc.returncode` by a client that owned the subprocess.  The SDK owns it
    now and does not lend it out: `stdio_client` yields streams, and the
    process handle stays inside.  So for a server that exits cleanly and says
    nothing on the way out, the honest report really is only that the
    connection closed.

    Kept as a test, and named for the loss, because the alternative is a gap
    nobody sees until they need it.  The informative half of F09-05 still
    works and is the test below: a server that *crashes* leaves a traceback on
    stderr, and that is the case where "why" exists at all.

    The trade this chapter made -- HTTP, OAuth, resources and prompts for an
    exit code -- is worth it, and it is not free, and a book that only
    reported the first half would be selling something.
    """
    registry, clients = await started(files(MCP_DIE_AFTER="1"))
    try:
        handler = registry.handlers()["mcp__files__stat"]
        assert (await handler({"name": "pyproject.toml"})).isdigit()
        second = await handler({"name": "pyproject.toml"})
        # The connection is reported as gone...
        assert "Connection closed" in second
        # ...and this is the part that is no longer knowable.
        assert "exited with code" not in second
    finally:
        await stop(clients)


async def test_F09_05_the_reason_comes_from_the_servers_stderr_not_just_its_exit_code() -> None:
    """The mutation that survived the first mutation run.

    `test_F09_05_a_dead_server_reports_why...` looked like it covered this and
    did not: "exited with code 1" is built from `returncode`, so replacing
    `stderr=PIPE` with `stderr=DEVNULL` left every test in this file green.
    A server that crashes rather than exiting cleanly is what tells them
    apart -- its explanation exists only on the pipe.
    """
    registry, clients = await started(files(MCP_CRASH_AFTER="1"))
    try:
        handler = registry.handlers()["mcp__files__stat"]
        assert (await handler({"name": "pyproject.toml"})).isdigit()
        second = await handler({"name": "pyproject.toml"})
        assert "MemoryError: index too large to load" in second
    finally:
        await stop(clients)


async def test_F09_05_the_call_after_the_death_reconnects_and_succeeds() -> None:
    registry, clients = await started(files(MCP_DIE_AFTER="1"))
    try:
        handler = registry.handlers()["mcp__files__stat"]
        assert (await handler({"name": "pyproject.toml"})).isdigit()
        await handler({"name": "pyproject.toml"})  # the one that finds it dead
        third = await handler({"name": "pyproject.toml"})
        assert third.isdigit()
    finally:
        await stop(clients)


async def test_F09_05_the_in_flight_call_is_not_retried() -> None:
    """Deliberate: whether its side effect happened is unknown.

    A retry here would be the idempotency fault (F12-04) chosen on purpose.
    The *next* call is retried instead, because that one provably never ran.
    """
    registry, clients = await started(files(MCP_DIE_AFTER="1"))
    try:
        handler = registry.handlers()["mcp__files__search"]
        await handler({"query": "hatchling"})
        failed = await handler({"query": "hatchling"})
        assert "did not return" in failed
        assert "Try a different approach" in failed
    finally:
        await stop(clients)


async def test_F09_05_a_restart_re_reads_the_tool_list_rather_than_restoring_it() -> None:
    registry, clients = await started(files(MCP_DIE_AFTER="1"))
    try:
        handler = registry.handlers()["mcp__files__stat"]
        await handler({"name": "pyproject.toml"})
        await handler({"name": "pyproject.toml"})
        report = await registry.reconnect("files")
        assert "restarted" in report
        assert "mcp__files__stat" in registry.registrations
    finally:
        await stop(clients)


# -- F09-06: the server asks us something ------------------------------------


async def test_F09_06_the_protocol_removed_the_mechanism_this_fault_was_fixed_with() -> None:
    """The sharpest thing the SDK rewrite found (F09-16).

    F09-06 was written against protocol `2025-06-18`, where a server could
    send the *client* a JSON-RPC request mid-call -- `elicitation/create` --
    and `registry.elicitation_handler` answered it with chapter 5's approver.
    That worked, was measured, and shipped.

    Protocol `2026-07-28` forbids server-initiated requests outright. The
    SDK's own dispatcher says so in as many words
    (`mcp/server/runner.py:551-558`, `_NoServerRequestsDispatchContext`:
    *"the modern protocol forbids server-initiated JSON-RPC requests"*), and a
    server that tries gets `NoBackChannelError` rather than an answer.

    **The hand-rolled client could not have found this.** It pinned
    `PROTOCOL_VERSION = "2025-06-18"`, so it would have kept negotiating a
    version where the mechanism still existed, kept passing this chapter's
    tests, and kept working -- until a server declined to speak a two-year-old
    revision. Freezing the transport froze the calendar.

    What is asserted here is what a client can still guarantee: the call fails
    **cleanly and quickly**, it says why, and the side effect does not happen.
    A hang would have been the bad outcome, and F09-04's deadline is what
    rules it out.
    """
    session = Session(mode="workspace-write", policy="on-request", approver=AllowAll())
    db = _scratch("_notes_approved.json")
    registry, clients = await started(
        notes(MCP_ELICIT="1", MCP_NOTES_DB=str(db)),
        handlers={"elicitation/create": elicitation_handler(session)},
    )
    try:
        result = await registry.handlers()["mcp__notes__remember"]({"name": "x", "text": "hi"})
        assert "back-channel" in result
        # The note was never written: the tool raised before touching disk.
        assert not db.exists()
    finally:
        await stop(clients)
        await asyncio.to_thread(db.unlink, True)


async def test_F09_06_the_client_still_offers_the_capability_it_can_honour() -> None:
    """`elicitation_handler` is kept, and this says why rather than leaving it
    looking like dead code.

    The adapter from an MCP elicitation to chapter 5's approver is unaffected
    by which protocol revision carries the question -- it maps a message and a
    schema onto "ask a human yes or no", and declines anything wider. What
    changed is only the envelope. Chapter 21 adds a transport where the
    question can arrive again; deleting the answer in the meantime would mean
    rediscovering chapter 5's rule about a second prompt style for the same
    question.
    """
    session = Session(mode="workspace-write", policy="on-request", approver=AllowAll())
    handler = elicitation_handler(session)
    reply = await handler(
        {
            "message": "Save note 'x' to disk?",
            "requestedSchema": {
                "type": "object",
                "required": ["confirm"],
                "properties": {"confirm": {"type": "boolean"}},
            },
        }
    )
    assert reply == {"action": "accept", "content": {"confirm": True}}

    denied = elicitation_handler(
        Session(mode="workspace-write", policy="on-request", approver=DenyAll())
    )
    assert (await denied({"message": "?", "requestedSchema": {}}))["action"] == "decline"


async def test_F09_06_a_request_for_a_shape_we_cannot_answer_declines() -> None:
    """Filling in a schema this client cannot ask a human about would mean
    inventing a value and putting it into somebody else's system."""
    asked: list[str] = []

    class Recording:
        async def ask(self, request: Any) -> ApprovalReply:
            asked.append(request.what)
            return ApprovalReply(True, request.what)

    handler = elicitation_handler(
        Session(mode="workspace-write", policy="on-request", approver=Recording())
    )
    answer = await handler(
        {
            "message": "what is your API key?",
            "requestedSchema": {
                "type": "object",
                "properties": {"api_key": {"type": "string"}},
            },
        }
    )
    assert answer == {"action": "decline"}
    assert asked == []


# -- F09-07: every shape a result can take -----------------------------------


def test_F09_07_text_blocks_are_joined() -> None:
    assert normalise(
        {"content": [{"type": "text", "text": "a"}, {"type": "text", "text": "b"}]}
    ) == ("a\nb")


def test_F09_07_binary_blocks_are_described_not_included() -> None:
    out = normalise({"content": [{"type": "image", "data": "A" * 4000, "mimeType": "image/png"}]})
    assert out == "[image omitted: image/png, 4000 base64 characters]"
    assert "AAA" not in out


def test_F09_07_an_embedded_resource_keeps_its_text_and_its_uri() -> None:
    out = normalise(
        {
            "content": [
                {
                    "type": "resource",
                    "resource": {"uri": "notes://idea", "text": "the note body"},
                }
            ]
        }
    )
    assert "notes://idea" in out
    assert "the note body" in out


def test_F09_07_structured_content_is_not_dropped() -> None:
    out = normalise({"content": [], "structuredContent": {"bytes": 46}})
    assert '"bytes": 46' in out


def test_F09_07_an_empty_result_says_so_rather_than_being_an_empty_string() -> None:
    """An empty string reads as "the tool succeeded and had nothing to say"."""
    assert normalise({}) == "(the tool returned no content)"
    assert normalise({"content": []}) == "(the tool returned no content)"


def test_F09_07_is_error_is_stated_because_the_transport_succeeded() -> None:
    out = normalise({"content": [{"type": "text", "text": "no such file"}], "isError": True})
    assert out.startswith("The tool reported an error.")
    assert "no such file" in out


def test_F09_07_an_unknown_block_type_is_reported_not_skipped() -> None:
    out = normalise({"content": [{"type": "hologram", "data": "..."}]})
    assert "hologram" in out


def test_F09_07_a_huge_result_is_clipped_at_both_ends() -> None:
    """The MCP path reaches the history through neither chapter 2's shell clip
    nor chapter 6's per-item clip; it needs its own."""
    body = "START" + "x" * 500_000 + "END"
    out = normalise({"content": [{"type": "text", "text": body}]})
    assert len(out) < MAX_RESULT_CHARS + 200
    assert out.startswith("START")
    assert out.endswith("END")
    assert "characters omitted" in out


async def test_F09_07_a_real_server_returning_three_block_types_survives() -> None:
    registry, clients = await started(notes())
    try:
        out = await registry.handlers()["mcp__notes__render"]({"name": "idea"})
        assert "rendered" in out
        assert "[image omitted: image/png" in out
        assert "notes://idea" in out
        assert '"bytes": 46' in out
    finally:
        await stop(clients)


# -- F09-08: a tool the history remembers and the session no longer has ------


async def test_F09_08_a_retired_tool_says_what_happened_to_it() -> None:
    """Chapter 0's "no tool named X" was written for names the model made up.

    Telling a model that about a tool it genuinely used ten turns ago is a
    lie, and the model responds to it by trying harder.
    """
    registry, clients = await started(notes())
    try:
        handler = registry.handlers()["mcp__notes__search"]
        registry.forget("notes")
        answer = await handler({"query": "idea"})
        assert "no longer available" in answer
        assert "no longer connected" in answer
    finally:
        await stop(clients)


async def test_F09_08_a_server_that_will_not_restart_retires_its_tools() -> None:
    registry, clients = await started(files(MCP_DIE_AFTER="1"))
    try:
        handler = registry.handlers()["mcp__files__stat"]
        await handler({"name": "pyproject.toml"})
        await handler({"name": "pyproject.toml"})
        registry.registrations["mcp__files__stat"].client.config = ServerConfig(
            "files", ("definitely-not-a-program-xyz",)
        )
        answer = await handler({"name": "pyproject.toml"})
        assert "could not be called" in answer
        assert "could not be restarted" in answer
        assert registry.registrations == {}
    finally:
        await stop(clients)


def test_F09_08_an_unknown_name_that_was_never_registered_gets_the_plain_advice() -> None:
    registry = McpRegistry()
    assert "Use one of the tools you were given" in registry.explain("mcp__ghost__thing")


# -- chapter 8 meets chapter 9: a footprint that is somebody else's promise ---


async def test_F09_footprint_of_a_remote_tool_without_a_hint_is_stateful() -> None:
    registry, clients = await started(files())
    try:
        assert registry.footprint_of(a_call("mcp__files__search", query="x")) == STATEFUL
    finally:
        await stop(clients)


async def test_F09_footprint_of_a_read_only_tool_allows_only_read_read_overlap() -> None:
    """`readOnlyHint` is trusted for one conclusion and no others.

    It is a promise from a process this one cannot inspect, so it buys "two
    read-only calls to the same server may overlap".  It does not buy an
    empty `Footprint()`: a remote tool may still touch a file this turn's
    `apply_patch` is writing, and nothing here can prove it does not.
    """
    registry, clients = await started(files())
    try:
        read_only = registry.footprint_of(a_call("mcp__files__stat", name="a"))
        assert read_only == Footprint(reads=frozenset({"mcp:files"}))
        assert not conflicts(read_only, read_only)
        assert conflicts(read_only, STATEFUL)
        assert read_only != Footprint()
    finally:
        await stop(clients)


async def test_F09_two_read_only_calls_to_one_server_share_a_batch() -> None:
    registry, clients = await started(files())
    try:
        plan = batches(
            [
                ToolCall("c1", "mcp__files__stat", {"name": "a"}, "{}"),
                ToolCall("c2", "mcp__files__stat", {"name": "b"}, "{}"),
                ToolCall("c3", "mcp__files__search", {"query": "x"}, "{}"),
            ],
            registry.footprint_of,
        )
        assert [[c.call_id for c in batch] for batch in plan] == [["c1", "c2"], ["c3"]]
    finally:
        await stop(clients)


def test_F09_remote_names_never_reach_the_local_classifier(tmp_path: Path) -> None:
    """`route_footprint` was replaced by `ToolSet` composition in interlude B.

    The behaviour it existed for is unchanged and still asserted here: a
    remote call is classified by the registry, a local one by `tools`, and
    neither classifier is ever shown the other's names. What changed is how
    the routing is decided -- by which set declared the name, rather than by
    an `is_remote()` test on the name itself, which needed a new branch every
    time a fourth source of tools appeared.
    """
    seen: list[str] = []
    session = Session(mode="read-only", approver=AllowAll())

    def local(c: ToolCall) -> Footprint:
        seen.append(c.name)
        return Footprint(reads=frozenset({"local"}))

    base = replace(local_tools(tmp_path, session), footprint_of=local)
    # `local=base.schemas` is the ordering rule `with_remote_tools` enforces:
    # the registry owns the list the model client is handed, so it has to be
    # built after the local set and told about it.
    registry = McpRegistry(local=base.schemas)
    combined = with_remote_tools(base, registry)

    assert combined.footprint_of(a_call("read_file", path="a.py")) == Footprint(
        reads=frozenset({"local"})
    )
    assert combined.footprint_of(a_call("mcp__x__y")) == STATEFUL
    assert seen == ["read_file"]  # the remote call never reached the local one


# -- configuration -----------------------------------------------------------


def test_load_config_reads_the_shape_the_readme_documents(tmp_path: Path) -> None:
    path = tmp_path / "mcp.json"
    path.write_text(
        json.dumps(
            {
                "servers": {
                    "notes": {
                        "command": ["python", "notes.py"],
                        "env": {"TOKEN": "x"},
                        "tool_timeout": 5,
                    }
                }
            }
        )
    )
    (server,) = load_config(path)
    assert server.name == "notes"
    assert server.command == ("python", "notes.py")
    assert server.env == {"TOKEN": "x"}
    assert server.tool_timeout == 5.0


def test_load_config_refuses_a_server_with_no_command(tmp_path: Path) -> None:
    """The message widened in chapter 21: a server can now be reached by
    `command` *or* `url`, so "no command" alone would be only half the truth
    about what this entry is missing."""
    path = tmp_path / "mcp.json"
    path.write_text(json.dumps({"servers": {"notes": {}}}))
    with pytest.raises(McpError, match="neither a 'command' list nor a 'url'"):
        load_config(path)


async def test_a_server_subprocess_does_not_inherit_the_hosts_api_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Chapter 2's allowlist (F02-09), one process further out.

    An MCP server is somebody else's program, started by us, and it does not
    need the key this agent talks to its model with.
    """
    monkeypatch.setenv("OPENAI_API_KEY", "sk-must-not-leak")
    from minicodex.mcp import _subprocess_env

    env = _subprocess_env(files())
    assert "OPENAI_API_KEY" not in env
    assert "PATH" in env

    with_credential = _subprocess_env(files(NOTES_TOKEN="explicitly-passed"))
    assert with_credential["NOTES_TOKEN"] == "explicitly-passed"
    assert "OPENAI_API_KEY" not in with_credential


async def test_closing_a_client_leaves_no_process_behind() -> None:
    """F02-08's rule, asserted the only way the SDK still allows.

    This test used to reach for `client._proc` and check `returncode`.  The
    SDK owns the subprocess now and does not hand it out -- `stdio_client`
    yields streams and keeps the handle -- so the process is no longer
    directly observable from here (the same loss F09-15 records).

    What is left is observable and is what actually matters: after `close()`
    the client reports itself shut, and the temporary file its stderr was
    being captured into has been cleaned up.  That second assertion is the
    one with teeth: it can only be true if the exit stack really unwound,
    which is the thing that also terminates the child.
    """
    client = McpClient(files())
    await client.start()
    assert client.alive
    stderr_file = client._stderr_path
    assert stderr_file is not None and stderr_file.exists()

    await client.close()

    assert not client.alive
    assert not stderr_file.exists()
