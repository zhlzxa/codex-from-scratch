"""The tools the agent may call, and the schema the model is shown."""

from __future__ import annotations

import asyncio
import functools
from pathlib import Path
from typing import Any

from minicodex.shell import ShellSession
from minicodex.shell import run_shell as _run_shell


def _read(path: Path) -> str:
    if not path.exists():
        return f"Error: {path} does not exist. Check the path and try again."
    if path.is_dir():
        return f"Error: {path} is a directory, not a file."
    return path.read_text(encoding="utf-8")


async def read_file(args: dict[str, Any]) -> str:
    """Read a UTF-8 text file relative to the current working directory.

    `Path.read_text()` blocks, and a blocking call inside `async def` stops the
    whole event loop rather than just this task.  With one tool at a time nobody
    notices; once chapter 8 runs tools concurrently the concurrency quietly
    turns into a queue.  Ruff's ASYNC240 rejects the direct call, which is why
    those rules are enabled.
    """
    path = args.get("path")
    if not isinstance(path, str):
        return 'Error: read_file needs a "path" argument, a string. Example: {"path": "a.py"}'
    return await asyncio.to_thread(_read, Path(path))


def default_tools() -> dict[str, Any]:
    """Build a fresh tool table with its own `ShellSession`.

    A function instead of a module-level dict: `ShellSession` holds state
    (`cwd`, `env`) that belongs to one conversation, not to the process.  Two
    agents sharing one session would see each other's `cd`.  `functools.partial`
    binds that session into `run_shell` while keeping the signature every tool
    needs -- `Callable[[dict], Awaitable[str]]` -- the same one `read_file`
    already has.
    """
    session = ShellSession()
    return {
        "read_file": read_file,
        "run_shell": functools.partial(_run_shell, session),
    }


# What the model is shown.  Chapter 3 is about how much this wording matters.
TOOL_SCHEMAS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read a UTF-8 text file and return its contents.",
            "parameters": {
                "type": "object",
                "required": ["path"],
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Path to the file, relative to the working directory.",
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
                f"commands are killed after {30} seconds."
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
