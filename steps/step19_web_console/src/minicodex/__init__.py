"""minicodex -- a Codex-like coding agent, built from scratch.

There is no agent yet.  There is a package that installs, runs, tests and
ships.  Everything after this chapter is built on top of it.
"""

from __future__ import annotations

from pathlib import Path

__all__ = ["__version__", "compaction_prompt", "permissions_prompt", "system_prompt"]

__version__ = "0.0.1"

_PROMPTS = Path(__file__).parent / "prompts"


def system_prompt() -> str:
    """Read the agent's system prompt from a file shipped inside the package.

    Written in chapter -1 with the note "nothing uses this yet" -- it existed
    because a data file is the cheapest way to prove that packaging works: code
    that only imports .py files will pass every test even when the wheel is
    broken.  Chapter 5 gives it a job, because the permission state has to
    reach the model somehow and a system message is where it goes.
    """
    return (_PROMPTS / "system.md").read_text(encoding="utf-8")


def permissions_prompt() -> str:
    """The template for the block that says what the agent may currently do."""
    return (_PROMPTS / "permissions.md").read_text(encoding="utf-8")


def compaction_prompt() -> str:
    """The instructions given to the model that summarises a dropped transcript.

    A file rather than a string constant, for the same reason as the other two:
    it is prose that will be edited by someone reading it as prose, and chapter
    13 puts every one of these under a snapshot test.
    """
    return (_PROMPTS / "compaction.md").read_text(encoding="utf-8")
