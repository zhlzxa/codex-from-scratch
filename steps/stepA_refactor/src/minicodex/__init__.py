"""minicodex -- a Codex-like coding agent, built from scratch.

There is no agent yet.  There is a package that installs, runs, tests and
ships.  Everything after this chapter is built on top of it.
"""

from __future__ import annotations

from pathlib import Path

__all__ = ["__version__", "system_prompt"]

__version__ = "0.0.1"

_PROMPTS = Path(__file__).parent / "prompts"


def system_prompt() -> str:
    """Read the agent's system prompt from a file shipped inside the package.

    Nothing uses this yet.  It exists because a data file is the cheapest way
    to prove that packaging works: code that only imports .py files will pass
    every test even when the wheel is broken.
    """
    return (_PROMPTS / "system.md").read_text(encoding="utf-8")
