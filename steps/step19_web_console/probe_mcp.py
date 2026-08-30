"""What chapter 9 measured, and how.

Run one section at a time:

    uv run python probe_mcp.py collide      # F09-01, no network
    uv run python probe_mcp.py tokens       # F09-03, no network
    uv run python probe_mcp.py names        # tool-name limits, real API
    uv run python probe_mcp.py selection    # F09-02, real API, ~30 requests
    uv run python probe_mcp.py roundtrips   # F09-09, real API

Anything with "real API" costs money and needs OPENAI_API_KEY.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from typing import Any

import httpx

from minicodex.registry import McpRegistry, model_name
from minicodex.tokens import estimate_messages

MODEL = "gpt-4o-mini"
URL = "https://api.openai.com/v1/chat/completions"
SAMPLES = 3


def _key() -> str:
    key = os.environ.get("OPENAI_API_KEY")
    if not key:
        print("OPENAI_API_KEY is not set; this section needs it.", file=sys.stderr)
        raise SystemExit(1)
    return key


async def _ask(
    client: httpx.AsyncClient,
    tools: list[dict[str, Any]],
    prompt: str | list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """One non-streaming request.  Returns the tool calls it asked for."""
    body = {
        "model": MODEL,
        "messages": [{"role": "user", "content": prompt}] if isinstance(prompt, str) else prompt,
        "tools": tools,
        "temperature": 1,
    }
    resp = await client.post(
        URL, json=body, headers={"Authorization": f"Bearer {_key()}"}, timeout=90
    )
    if resp.status_code != 200:
        return [{"__http__": resp.status_code, "__body__": resp.text}]
    message = resp.json()["choices"][0]["message"]
    return [
        {"name": c["function"]["name"], "arguments": c["function"]["arguments"]}
        for c in (message.get("tool_calls") or [])
    ]


# -- fake catalogues, shaped like real ones ---------------------------------


# Round 1 used sixty tools called `operation_NN` and the model found the needle
# 3/3 at every size.  That measured almost nothing: no distractor was about
# notes, searching, meetings or text, so "pick the only relevant tool" was the
# task, not "pick the right one among relevant ones".
#
# Round 2 made every distractor plausible -- and repeated ten of them six times
# each, which turned out to be its own artefact: `tool_search("meeting")`
# returned five *copies* of one tool.  Round 3 is sixty distinct tools across
# six servers, which is what a machine with six MCP servers configured actually
# looks like.
_DOCS = [
    ("search_documents", "Full-text search across indexed documents and return matching passages."),
    ("get_document", "Fetch one document by its exact identifier."),
    ("list_documents", "List indexed documents with their titles."),
    ("index_document", "Add a document to the search index."),
    ("export_document", "Export a document as PDF."),
    ("diff_documents", "Show the differences between two document revisions."),
    ("tag_document", "Attach a tag to a document."),
    ("share_document", "Create a shareable link for a document."),
    ("archive_document", "Move a document out of the active set."),
    ("restore_document", "Bring an archived document back."),
]
_CAL = [
    ("find_meeting", "Look up a meeting by title, attendee or date and return its record."),
    ("summarise_meeting", "Summarise a meeting given its identifier."),
    ("search_calendar", "Search calendar entries by keyword and return matching events."),
    ("create_event", "Add an event to the calendar."),
    ("cancel_event", "Cancel a calendar event and notify attendees."),
    ("list_attendees", "List everyone invited to an event."),
    ("find_free_slot", "Find a time when everyone is available."),
    ("reschedule_event", "Move an event to a new time."),
    ("get_agenda", "Return the agenda attached to a meeting."),
    ("record_minutes", "Save minutes against a meeting record."),
]
_MEM = [
    ("recall_memory", "Search remembered facts from previous sessions by keyword."),
    ("store_memory", "Remember a fact for future sessions."),
    ("forget_memory", "Delete a remembered fact."),
    ("list_memories", "List everything remembered about this project."),
    ("summarise_memories", "Summarise what is remembered about a topic."),
    ("link_memories", "Record that two remembered facts are related."),
    ("pin_memory", "Mark a remembered fact as always relevant."),
    ("expire_memory", "Set an expiry date on a remembered fact."),
    ("export_memories", "Export all remembered facts as JSON."),
    ("import_memories", "Load remembered facts from a JSON file."),
]
_CHAT = [
    ("search_messages", "Search chat messages by keyword and return the matching thread."),
    ("post_message", "Post a message to a channel."),
    ("list_channels", "List the channels this workspace has."),
    ("get_thread", "Fetch one conversation thread by id."),
    ("react_to_message", "Add an emoji reaction to a message."),
    ("pin_message", "Pin a message in its channel."),
    ("invite_user", "Invite someone to a channel."),
    ("mute_channel", "Stop notifications from a channel."),
    ("upload_file", "Upload a file to a channel."),
    ("search_files", "Search files shared in chat by name."),
]
_CODE = [
    ("grep_workspace", "Search the workspace for a literal string and return matching lines."),
    ("open_pull_request", "Open a pull request from a branch."),
    ("list_branches", "List the branches in a repository."),
    ("review_diff", "Return the diff of a pull request."),
    ("run_pipeline", "Trigger a CI pipeline for a branch."),
    ("get_build_log", "Fetch the log of one build."),
    ("list_issues", "List open issues in a repository."),
    ("comment_on_issue", "Add a comment to an issue."),
    ("assign_issue", "Assign an issue to somebody."),
    ("close_issue", "Close an issue with a reason."),
]
_WIKI = [
    ("query_notes_index", "Query the notes index and return note identifiers ranked by relevance."),
    ("get_note", "Fetch one note by its exact identifier."),
    ("list_notes", "List every saved note with its name and first line."),
    ("create_page", "Create a new wiki page."),
    ("edit_page", "Edit an existing wiki page."),
    ("search_wiki", "Search wiki pages by keyword."),
    ("list_spaces", "List the wiki spaces available."),
    ("watch_page", "Get notified when a page changes."),
    ("page_history", "Return the revision history of a page."),
    ("move_page", "Move a page to another space."),
]
CATALOGUE = [
    ("docs", _DOCS),
    ("calendar", _CAL),
    ("memory", _MEM),
    ("chat", _CHAT),
    ("code", _CODE),
    ("wiki", _WIKI),
]
NEAR_MISSES = [(server, name, desc) for server, tools in CATALOGUE for name, desc in tools]


def _catalogue(count: int, *, hard: bool = False) -> list[dict[str, Any]]:
    """`count` tools with the schema shape a real MCP server produces."""
    tools = []
    for index in range(count):
        if hard:
            server, base, description = NEAR_MISSES[index % len(NEAR_MISSES)]
            name = model_name(server, base)
        else:
            name = model_name("bigcorp", f"operation_{index:02d}")
            description = (
                f"Run operation {index} against the configured backend and return "
                "a structured report describing every record it touched."
            )
        tools.append(
            {
                "type": "function",
                "function": {
                    "name": name,
                    "description": description,
                    "parameters": {
                        "type": "object",
                        "required": ["target"],
                        "properties": {
                            "target": {
                                "type": "string",
                                "description": "Identifier of the object to operate on.",
                            },
                            "dry_run": {
                                "type": "boolean",
                                "description": "Report what would happen without doing it.",
                            },
                            "limit": {
                                "type": "integer",
                                "description": "Maximum number of records to touch.",
                            },
                        },
                    },
                },
            }
        )
    return tools


NEEDLE = {
    "type": "function",
    "function": {
        "name": model_name("notes", "search"),
        "description": "Search saved notes by keyword and return the matching note.",
        "parameters": {
            "type": "object",
            "required": ["query"],
            "properties": {"query": {"type": "string", "description": "Keyword."}},
        },
    },
}

TASK = "Find the note about the meeting. Use a tool."


# -- sections ----------------------------------------------------------------


def collide() -> None:
    """F09-01, with no server and no network: it is a dict, and dicts overwrite."""
    print("=== F09-01: two servers, one table ===")
    naive: dict[str, str] = {}
    for server, tools in (("files", ["search", "stat"]), ("notes", ["search", "render"])):
        for tool in tools:
            naive[tool] = server
    print(f"    naive dict:      {len(naive)} tools from 4 declared -> {naive}")

    registry = McpRegistry()
    names = [
        model_name(server, tool)
        for server, tools in (("files", ["search", "stat"]), ("notes", ["search", "render"]))
        for tool in tools
    ]
    print(f"    namespaced:      {len(set(names))} tools -> {sorted(set(names))}")
    print(f"    registry prefix: {registry.__class__.__name__} uses {model_name('S', 'T')}")


def tokens() -> None:
    """F09-03: what the tool list costs, per turn, before anyone says anything."""
    print("=== F09-03: schema cost, measured with chapter 6's estimator ===")
    conversation = [
        {"role": "system", "content": "You are a coding agent working in a repository."},
        {"role": "user", "content": "Find the note about the meeting and summarise it."},
    ]
    baseline = estimate_messages(conversation, [])
    print(f"    conversation alone:                       {baseline:>6} tokens")
    for count in (4, 12, 30, 60, 120):
        catalogue = _catalogue(count)
        total = estimate_messages(conversation, catalogue)
        schema_cost = total - baseline
        share = 100 * schema_cost / total
        print(
            f"    + {count:>3} tool schemas: {schema_cost:>6} tokens  "
            f"({share:.1f}% of the request, {schema_cost / count:.0f} per tool)"
        )
    deferred = estimate_messages(conversation, [NEEDLE])
    print(f"    deferred (1 search tool only):            {deferred - baseline:>6} tokens")
    print("    Every number above is paid on every turn, whether or not a tool is used.")


async def names() -> None:
    """What the providers actually accept as a function name."""
    print("=== tool-name limits, measured against the real API ===")
    async with httpx.AsyncClient() as client:
        for label, name in (
            ("64 chars", "a" * 64),
            ("65 chars", "a" * 65),
            ("128 chars", "a" * 128),
            ("256 chars", "a" * 256),
            ("512 chars", "a" * 512),
            ("dots", "mcp.notes.search"),
            ("spaces", "mcp notes search"),
            ("slash", "mcp/notes/search"),
            ("double underscore", model_name("notes", "search")),
        ):
            tool = {
                "type": "function",
                "function": {
                    "name": name,
                    "description": "A tool.",
                    "parameters": {"type": "object", "properties": {}},
                },
            }
            calls = await _ask(client, [tool], "Call the tool.")
            first = calls[0] if calls else {}
            if "__http__" in first:
                status = first["__http__"]
                try:
                    detail = json.loads(first["__body__"])["error"]["message"]
                except (ValueError, KeyError):
                    detail = first["__body__"]
                print(f"    {label:<20} HTTP {status}: {detail[:190]}")
            else:
                print(f"    {label:<20} accepted, model called {first.get('name', '(nothing)')!r}")


async def selection() -> None:
    """F09-02: does a big catalogue actually degrade the choice?"""
    print(f"=== F09-02: finding one tool among N ({MODEL}, {SAMPLES} samples each) ===")
    async with httpx.AsyncClient() as client:
        for hard in (False, True):
            print(f"    --- distractors: {'plausible near-misses' if hard else 'irrelevant'} ---")
            for count in (0, 8, 30, 60):
                catalogue = [*_catalogue(count, hard=hard), NEEDLE]
                hits = 0
                wrong: list[str] = []
                for _ in range(SAMPLES):
                    calls = await _ask(client, catalogue, TASK)
                    chosen = calls[0]["name"] if calls else "(no tool call)"
                    if chosen == NEEDLE["function"]["name"]:
                        hits += 1
                    else:
                        wrong.append(chosen)
                noise = f"   picked instead: {', '.join(sorted(set(wrong)))}" if wrong else ""
                print(f"      {count + 1:>3} tools offered:  correct {hits}/{SAMPLES}{noise}")

        # If the variable is really "is there a plausible alternative" and not
        # "how many tools are there", then chapter 3's fix for two overlapping
        # tools (F03-04/F03-05: say what a tool is *not* for) should work at
        # sixty just as it worked at two.
        print("    --- 61 hard tools, needle carries a chapter-3 disambiguating clause ---")
        sharpened = {
            "type": "function",
            "function": {
                **NEEDLE["function"],
                "description": (
                    "Search saved notes by keyword and return the matching note. This is "
                    "the only tool that reads the user's own saved notes. Do not use the "
                    "document, calendar, message or memory search tools for a note."
                ),
            },
        }
        for count in (8, 60):
            catalogue = [*_catalogue(count, hard=True), sharpened]
            hits = 0
            wrong = []
            for _ in range(SAMPLES):
                calls = await _ask(client, catalogue, TASK)
                chosen = calls[0]["name"] if calls else "(no tool call)"
                if chosen == NEEDLE["function"]["name"]:
                    hits += 1
                else:
                    wrong.append(chosen)
            noise = f"   picked instead: {', '.join(sorted(set(wrong)))}" if wrong else ""
            print(f"      {count + 1:>3} tools offered:  correct {hits}/{SAMPLES}{noise}")

        print("    --- the same 61 hard tools, deferred behind tool_search ---")
        searched = revealed_hits = 0
        for _ in range(SAMPLES):
            registry = _deferred_registry(hard=True)
            first = await _ask(client, [registry.search_schema()], TASK)
            if not first or first[0]["name"] != "tool_search":
                print(f"      turn 1: model did not search, it called {first}")
                continue
            searched += 1
            query = json.loads(first[0]["arguments"]).get("query", "")
            output = registry.search(query, 5)
            revealed = [s["function"]["name"] for s in registry.visible]
            # The real message sequence, not the search result pasted into a
            # user turn: the first version of this probe did the latter, and
            # "the model made no tool call" is exactly the artefact a wrong
            # message shape produces.
            second = await _ask(
                client,
                [registry.search_schema(), *registry.visible],
                [
                    {"role": "user", "content": TASK},
                    {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [
                            {
                                "id": "call_1",
                                "type": "function",
                                "function": {
                                    "name": "tool_search",
                                    "arguments": first[0]["arguments"],
                                },
                            }
                        ],
                    },
                    {"role": "tool", "tool_call_id": "call_1", "content": output},
                ],
            )
            chosen = second[0]["name"] if second else "(no tool call)"
            ok = chosen == NEEDLE["function"]["name"]
            revealed_hits += ok
            print(
                f"      query={query!r:<44} revealed {len(revealed)} "
                f"-> {'correct' if ok else chosen}"
            )
        print(f"      searched {searched}/{SAMPLES}, then correct {revealed_hits}/{SAMPLES}")


def _deferred_registry(*, hard: bool) -> McpRegistry:
    """A registry holding the probe's fake catalogue, everything deferred.

    Built by hand rather than from a server: this section is about what the
    model does with a tool list, and starting sixty subprocesses to measure
    that would be measuring something else.
    """
    from minicodex.mcp import RemoteTool

    registry = McpRegistry(schema_budget=0)
    tools = []
    for schema in [*_catalogue(60, hard=hard), NEEDLE]:
        function = schema["function"]
        server, _, bare = function["name"].removeprefix("mcp__").partition("__")
        tools.append(RemoteTool(server, bare, function["description"], function["parameters"]))
    registry.registrations = {}
    registry.add(None, tools)  # type: ignore[arg-type]
    return registry


async def roundtrips() -> None:
    """F09-09: how many turns does a ten-item batch job take?"""
    print(f"=== F09-09: batching, {MODEL} ===")
    tool = {
        "type": "function",
        "function": {
            "name": model_name("files", "stat"),
            "description": "Return the size in bytes of one indexed file.",
            "parameters": {
                "type": "object",
                "required": ["name"],
                "properties": {"name": {"type": "string"}},
            },
        },
    }
    files = [f"chapter_{i:02d}.md" for i in range(10)]
    prompt = "Get the size of every one of these files: " + ", ".join(files)
    async with httpx.AsyncClient() as client:
        print("    --- the ten targets are known up front ---")
        for _ in range(SAMPLES):
            calls = await _ask(client, [tool], prompt)
            print(f"      {len(calls)} call(s) in the first response")

        # The version that cannot be batched: the model does not know what to
        # ask for until a previous call answers.  This is the shape F09-09 is
        # actually about, and it is the shape parallel tool calls cannot help.
        print("    --- the targets have to be discovered first ---")
        listing = {
            "type": "function",
            "function": {
                "name": model_name("files", "list"),
                "description": "List the names of every indexed file.",
                "parameters": {"type": "object", "properties": {}},
            },
        }
        for _ in range(SAMPLES):
            messages: list[dict[str, Any]] = [
                {
                    "role": "user",
                    "content": "Find the largest indexed file. Report its name and size.",
                }
            ]
            turns = total = 0
            for turn in range(12):
                calls = await _ask(client, [listing, tool], messages)
                if not calls or "__http__" in calls[0]:
                    break
                turns = turn + 1
                total += len(calls)
                messages.append(
                    {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [
                            {
                                "id": f"c{turn}_{i}",
                                "type": "function",
                                "function": {"name": c["name"], "arguments": c["arguments"]},
                            }
                            for i, c in enumerate(calls)
                        ],
                    }
                )
                for i, call in enumerate(calls):
                    if call["name"].endswith("__list"):
                        answer = "\n".join(files)
                    else:
                        name = json.loads(call["arguments"] or "{}").get("name", "")
                        answer = str(1000 + 37 * (hash(name) % 40))
                    messages.append(
                        {"role": "tool", "tool_call_id": f"c{turn}_{i}", "content": answer}
                    )
            print(f"      {turns} round trip(s), {total} call(s) total")


SECTIONS = {
    "collide": collide,
    "tokens": tokens,
    "names": names,
    "selection": selection,
    "roundtrips": roundtrips,
}


def main() -> None:
    chosen = sys.argv[1:] or ["collide", "tokens"]
    for name in chosen:
        section = SECTIONS[name]
        if asyncio.iscoroutinefunction(section):
            asyncio.run(section())
        else:
            section()
        print()


if __name__ == "__main__":
    main()
