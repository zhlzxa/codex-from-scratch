"""The tools the agent may call, and the schema the model is shown.

The wording in `TOOL_SCHEMAS` is not decoration. Chapter 3 measured what
changes when it changes, and what does not:

  - Saying "relative to the repository root" fixed the path both models got
    wrong -- but only while the example in the description happened to be the
    answer. The rule is enforced in `paths.resolve()` for that reason.
  - Saying "commands are killed after 30 seconds" changed nothing at all: six
    runs out of six across both providers sent a two-minute command anyway.
    The sentence stays because it costs almost nothing and helps a human
    reading the request log, but the thing that actually redirects the model
    is the error message it gets after the kill.
  - Saying "do not use this to read a file" on `run_shell` was measured and
    made no difference here, because `read_file` and `run_shell` already say
    what they are. It is not added. Two tools whose names did NOT say what
    they were (`fetch_content` / `get_text`) did need it -- both providers
    picked the wrong one 3/3 until each description said what it was not for.
"""

from __future__ import annotations

import asyncio
import functools
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from minicodex.agent_types import ToolCall, ToolFn
from minicodex.approval import UPGRADES, Session, gate_command, gate_write, request_upgrade
from minicodex.patch import Edit, apply_edits
from minicodex.paths import resolve
from minicodex.scheduler import STATEFUL, Footprint
from minicodex.shell import DEFAULT_TIMEOUT, ShellSession
from minicodex.tool_errors import tool_error


@dataclass(frozen=True)
class ToolContext:
    """Everything a handler needs that belongs to one conversation.

    `root` and `shell` already existed as loose arguments threaded through
    `functools.partial`; this only gave them a name.  `root` decides whether a
    path is inside the repository, `shell` carries the working directory across
    calls.  Neither belongs to the process, which is why neither is a
    module-level constant.

    `session` arrived in chapter 5 and is the odd one out: it is mutable, and
    it is mutable because `request_permissions` can change what the rest of the
    conversation is allowed to do.  It sits here rather than being a fourth
    argument to every handler for the same reason the other two do.
    """

    root: Path
    shell: ShellSession
    session: Session


@dataclass(frozen=True)
class ToolSpec:
    """One tool, described once.

    Before this existed there were two tables: a `{name: handler}` dict and a
    hand-written list of schemas, with nothing tying them together.  They met
    only in `__main__`, as two separate arguments, and disagreeing was silent
    in both directions -- a handler with no schema is never called, and a
    schema with no handler makes the model receive the "no tool named X" error
    that chapter 0 wrote for *hallucinated* tool names.

    `bind` rather than a ready-made handler because the two halves have
    different lifetimes.  The description and the parameters are static: the
    same for the whole process, and needed at import time to build
    `TOOL_SCHEMAS`.  The handler is per-conversation, because it closes over a
    repository root and a shell session.  Storing a bound handler here would
    drag `Path.cwd()` and a `ShellSession` into import time to satisfy a field
    that schema rendering never reads.
    """

    name: str
    description: str
    parameters: dict[str, Any]
    bind: Callable[[ToolContext], ToolFn]

    def schema(self) -> dict[str, Any]:
        """Exactly the shape both providers expect, and the only place it is built."""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


async def read_file(root: Path, args: dict[str, Any]) -> str:
    """Read a UTF-8 text file from inside the repository.

    `Path.read_text()` blocks, and a blocking call inside `async def` stops the
    whole event loop rather than just this task.  With one tool at a time nobody
    notices; once chapter 8 runs tools concurrently the concurrency quietly
    turns into a queue.  Ruff's ASYNC240 rejects the direct call, which is why
    those rules are enabled.

    `errors="replace"` for the same reason chapter 2 put it on the subprocess
    output: a file with one bad byte should come back slightly wrong, not as
    an exception that discards the whole read.
    """
    path, error = resolve(args.get("path"), root)
    if error is not None:
        return error
    assert path is not None
    return await asyncio.to_thread(_read, path)


async def run_shell(ctx: ToolContext, args: dict[str, Any]) -> str:
    """Run a shell command, if the gate lets it through.

    The gate is first, before the argument is even checked for type, and that
    order is deliberate: every path from here to a subprocess passes through
    `gate_command`, and putting anything above it invites a future edit that
    returns early and skips it.
    """
    command = args.get("command")
    if not isinstance(command, str):
        return tool_error(
            'run_shell needs a "command" argument, a string',
            you_sent=repr(args.get("command")),
            do_this='Example: {"command": "pytest -q"}',
        )

    gated = await gate_command(command, ctx.session)
    if not gated.allowed:
        assert gated.denial is not None
        return gated.denial

    output = await ctx.shell.run(gated.command)
    # The user may have run something else entirely.  A model that is not told
    # will read this output as the result of the command it asked for and
    # report accordingly -- which is a wrong answer produced by a mechanism
    # that was working correctly.
    return f"{gated.note}\n\n{output}" if gated.note else output


async def request_permissions(ctx: ToolContext, args: dict[str, Any]) -> str:
    """Ask the user for more than the sandbox currently gives."""
    needs = args.get("needs")
    why = args.get("why")
    if not isinstance(needs, str) or not isinstance(why, str):
        return tool_error(
            'request_permissions needs "needs" and "why", both strings',
            you_sent=repr(args)[:200],
            do_this=(
                'Example: {"needs": "write-files", "why": "the test suite writes to .pytest_cache"}'
            ),
        )
    return await request_upgrade(ctx.session, needs=needs, why=why)


async def apply_patch(root: Path, session: Session, args: dict[str, Any]) -> str:
    """Replace an exact block of text in one or more files.

    The `edits` list is validated in full before anything is written --
    measured in chapter 4: writing as it goes leaves the repository
    half-edited when the third of five hunks does not match.
    """
    gated = await gate_write(f"edit files in {root.name}/", session)
    if not gated.allowed:
        assert gated.denial is not None
        return gated.denial

    raw = args.get("edits")
    if not isinstance(raw, list):
        return tool_error(
            'apply_patch needs an "edits" argument, a list of objects',
            you_sent=repr(args.get("edits")),
            do_this=(
                'Example: {"edits": [{"path": "src/a.py", '
                '"old_text": "    return 1", "new_text": "    return 2"}]}'
            ),
        )

    edits: list[Edit] = []
    for index, item in enumerate(raw, 1):
        if not isinstance(item, dict):
            return tool_error(
                f"edit {index} is not an object",
                you_sent=repr(item),
                do_this='Each edit needs "path", "old_text" and "new_text".',
            )
        missing = [k for k in ("path", "old_text", "new_text") if not isinstance(item.get(k), str)]
        if missing:
            return tool_error(
                f"edit {index} is missing {', '.join(missing)}",
                you_sent=repr(item)[:200],
                do_this='Each edit needs "path", "old_text" and "new_text", all strings.',
            )
        edits.append(Edit(item["path"], item["old_text"], item["new_text"]))

    return await asyncio.to_thread(apply_edits, edits, root)


def footprint_of(call: ToolCall, root: Path) -> Footprint:
    """What one call touches, for the scheduler in chapter 8.

    Only two tools can be named as a set of resources at all: `read_file`
    reads the one path it was given, `apply_patch` writes every path named in
    its edits. Everything else -- `run_shell`'s working directory and
    unbounded reach, `request_permissions` mutating the session every other
    tool call reads from, any tool call this function does not recognise --
    returns `STATEFUL`, which the scheduler treats as conflicting with
    everything, including another stateful call.

    This function re-resolves every path it looks at, and the actual handler
    resolves it again a moment later. Threading the resolved `Path` through
    from here to there would save that second `stat()`, and was tried; it
    also means every tool signature carries a scheduling detail forever, for
    a save too small to measure. The two stay independent on purpose: the
    scheduler is allowed to be conservative (and re-check) about a call it is
    not going to run yet, without the tool that actually runs it inheriting
    any of that.

    A call this function cannot make sense of -- unresolvable arguments,
    unresolvable paths, an `apply_patch` edit that names no path at all --
    resolves to `STATEFUL` rather than raising. The handler is the one place
    that gets to reject bad arguments with an error the model can act on;
    the scheduler's only two moves are "run this next to other things" or
    "don't", and guessing wrong on "don't" costs nothing but concurrency.
    """
    if call.arguments is None:
        return STATEFUL

    if call.name == "read_file":
        path, error = resolve(call.arguments.get("path"), root)
        if error is not None or path is None:
            return STATEFUL
        return Footprint(reads=frozenset({str(path)}))

    if call.name == "apply_patch":
        raw = call.arguments.get("edits")
        if not isinstance(raw, list) or not raw:
            return STATEFUL
        touched: set[str] = set()
        for item in raw:
            if not isinstance(item, dict):
                return STATEFUL
            path, error = resolve(item.get("path"), root)
            if error is not None or path is None:
                return STATEFUL
            touched.add(str(path))
        return Footprint(writes=frozenset(touched))

    return STATEFUL


def tool_specs(timeout: float = DEFAULT_TIMEOUT) -> list[ToolSpec]:
    """Every tool the agent has, described once.

    This list is the only place a tool is declared.  `tool_schemas()` renders
    it for the model and `default_tools()` binds it for the loop, so the two
    cannot drift: adding a tool here makes it both visible and runnable, and
    there is no longer a second place to forget.

    The timeout is still a parameter rather than a number typed into the
    string. Chapter 2 shipped `f"...killed after {30} seconds"` next to
    `DEFAULT_TIMEOUT = 30.0`, which agreed only because both were written on
    the same afternoon; nothing would have caught the day someone changed one
    of them. There is one number, and a test asserts the sentence matches it.
    """
    return [
        ToolSpec(
            name="read_file",
            description="Read a UTF-8 text file from the repository and return its contents.",
            parameters={
                "type": "object",
                "required": ["path"],
                "properties": {
                    "path": {
                        "type": "string",
                        "description": (
                            "Path to the file, relative to the repository root. "
                            "Example: src/minicodex/model.py"
                        ),
                    }
                },
            },
            bind=lambda ctx: functools.partial(read_file, ctx.root),
        ),
        ToolSpec(
            name="apply_patch",
            description=(
                "Edit files by replacing exact blocks of text. Every edit is "
                "checked before any file is written: if one fails, nothing is "
                "written."
            ),
            parameters={
                "type": "object",
                "required": ["edits"],
                "properties": {
                    "edits": {
                        "type": "array",
                        "description": "The edits to apply, in any order.",
                        "items": {
                            "type": "object",
                            "required": ["path", "old_text", "new_text"],
                            "properties": {
                                "path": {
                                    "type": "string",
                                    "description": (
                                        "Path to the file, relative to the repository "
                                        "root. Example: src/minicodex/model.py"
                                    ),
                                },
                                "old_text": {
                                    "type": "string",
                                    "description": (
                                        "The text to replace, copied from the file. It "
                                        "must appear exactly once -- include whole "
                                        "surrounding lines until it does. Do not write "
                                        "line numbers or diff markers."
                                    ),
                                },
                                "new_text": {
                                    "type": "string",
                                    "description": "What to put in its place.",
                                },
                            },
                        },
                    }
                },
            },
            bind=lambda ctx: functools.partial(apply_patch, ctx.root, ctx.session),
        ),
        ToolSpec(
            name="run_shell",
            description=(
                "Run a shell command and return its combined stdout and stderr. "
                "The working directory persists across calls within one session "
                "(cd changes it for subsequent calls). Backgrounded commands "
                "(trailing '&') are not supported. Long-running or silent "
                f"commands are killed after {timeout:.0f} seconds."
            ),
            parameters={
                "type": "object",
                "required": ["command"],
                "properties": {
                    "command": {
                        "type": "string",
                        "description": "The shell command to run.",
                    }
                },
            },
            bind=lambda ctx: functools.partial(run_shell, ctx),
        ),
        ToolSpec(
            name="request_permissions",
            description=(
                "Ask the user to grant permissions this session does not have. Use "
                "this after a 'Permission denied:' response, when the task cannot be "
                "finished within the current permissions. Do not use it for a command "
                "that merely failed -- that is not a permissions problem."
            ),
            parameters={
                "type": "object",
                "required": ["needs", "why"],
                "properties": {
                    "needs": {
                        "type": "string",
                        "enum": sorted(UPGRADES),
                        "description": (
                            "write-files: edit files in this repository. "
                            "unrestricted: no restrictions at all."
                        ),
                    },
                    "why": {
                        "type": "string",
                        "description": (
                            "One sentence, shown to the user verbatim, saying what "
                            "you need it for. Example: the test suite writes to "
                            ".pytest_cache."
                        ),
                    },
                },
            },
            bind=lambda ctx: functools.partial(request_permissions, ctx),
        ),
    ]


def tool_context(
    root: Path | None = None,
    session: Session | None = None,
    shell: ShellSession | None = None,
) -> ToolContext:
    """One conversation's tool context.

    A fresh `ShellSession` per call, because its state (cwd, env) belongs to
    one conversation and not to the process.  A fresh `Session` for the same
    reason -- and its default is the restrictive one, so a caller that forgets
    to pass permissions gets an agent that can read and nothing else.

    `shell` became an argument in chapter 10.  A sub-agent is a second
    conversation that has to start where the first one is standing: the parent's
    `cd src` lives in a Python attribute and nowhere else, so a child that
    builds its own `ShellSession` silently starts at the process working
    directory.  Passing the object rather than the string on purpose -- the
    environment allowlist and the timeout are part of "where the parent is" too.
    """
    return ToolContext(
        root=(root or Path.cwd()).resolve(),
        shell=shell or ShellSession(),
        session=session or Session(),
    )


def bind_all(context: ToolContext) -> dict[str, ToolFn]:
    """Every spec in `tool_specs()`, bound to one context."""
    return {spec.name: spec.bind(context) for spec in tool_specs()}


def default_tools(
    root: Path | None = None,
    session: Session | None = None,
    shell: ShellSession | None = None,
) -> dict[str, ToolFn]:
    """Bind every spec to one repository, one shell session and one permission set."""
    return bind_all(tool_context(root, session, shell))


def tool_schemas(timeout: float = DEFAULT_TIMEOUT) -> list[dict[str, Any]]:
    """What the model is shown.  Rendered from the same list, never written twice."""
    return [spec.schema() for spec in tool_specs(timeout)]


# Kept as a module-level name because `__main__` and the tests both want the
# default, and calling it once here is cheaper than threading it through.
TOOL_SCHEMAS: list[dict[str, Any]] = tool_schemas()

__all__ = [
    "TOOL_SCHEMAS",
    "ToolContext",
    "ToolSpec",
    "apply_patch",
    "bind_all",
    "default_tools",
    "footprint_of",
    "read_file",
    "request_permissions",
    "run_shell",
    "tool_context",
    "tool_error",
    "tool_schemas",
    "tool_specs",
]
