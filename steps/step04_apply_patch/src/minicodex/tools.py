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
from pathlib import Path
from typing import Any

from minicodex.patch import Edit, apply_edits
from minicodex.paths import resolve
from minicodex.shell import DEFAULT_TIMEOUT, ShellSession
from minicodex.shell import run_shell as _run_shell
from minicodex.tool_errors import tool_error


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


async def apply_patch(root: Path, args: dict[str, Any]) -> str:
    """Replace an exact block of text in one or more files.

    The `edits` list is validated in full before anything is written --
    measured in chapter 4: writing as it goes leaves the repository
    half-edited when the third of five hunks does not match.
    """
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


def default_tools(root: Path | None = None) -> dict[str, Any]:
    """Build a fresh tool table bound to one repository and one shell session.

    Both tools are `functools.partial` now. `ShellSession` holds state (cwd,
    env) that belongs to one conversation; `root` is what makes it possible to
    decide whether an absolute path is inside the repository, and to name a
    near-miss file in the error. Neither belongs to the process, so neither
    lives at module level.
    """
    root = (root or Path.cwd()).resolve()
    session = ShellSession()
    return {
        "read_file": functools.partial(read_file, root),
        "run_shell": functools.partial(_run_shell, session),
        "apply_patch": functools.partial(apply_patch, root),
    }


def tool_schemas(timeout: float = DEFAULT_TIMEOUT) -> list[dict[str, Any]]:
    """What the model is shown.

    A function taking the timeout rather than a module-level constant with the
    number typed into the string: chapter 2 shipped `f"...killed after {30}
    seconds"` next to `DEFAULT_TIMEOUT = 30.0`, which agreed only because both
    were written on the same afternoon. Nothing would have caught the day
    someone changed one of them. Now there is only one number, and a test
    asserts the sentence matches it.
    """
    return [
        {
            "type": "function",
            "function": {
                "name": "read_file",
                "description": (
                    "Read a UTF-8 text file from the repository and return its contents."
                ),
                "parameters": {
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
            },
        },
        {
            "type": "function",
            "function": {
                "name": "apply_patch",
                "description": (
                    "Edit files by replacing exact blocks of text. Every edit is "
                    "checked before any file is written: if one fails, nothing is "
                    "written."
                ),
                "parameters": {
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
            },
        },
        {
            "type": "function",
            "function": {
                "name": "run_shell",
                "description": (
                    "Run a shell command and return its combined stdout and stderr. "
                    "The working directory persists across calls within one session "
                    "(cd changes it for subsequent calls). Backgrounded commands "
                    "(trailing '&') are not supported. Long-running or silent "
                    f"commands are killed after {timeout:.0f} seconds."
                ),
                "parameters": {
                    "type": "object",
                    "required": ["command"],
                    "properties": {
                        "command": {
                            "type": "string",
                            "description": "The shell command to run.",
                        }
                    },
                },
            },
        },
    ]


# Kept as a module-level name because `__main__` and the tests both want the
# default, and calling it once here is cheaper than threading it through.
TOOL_SCHEMAS: list[dict[str, Any]] = tool_schemas()

__all__ = ["TOOL_SCHEMAS", "default_tools", "read_file", "tool_error", "tool_schemas"]
