"""minicodex -- a Codex-like coding agent, built from scratch.

There is no agent yet.  There is a package that installs, runs, tests and
ships.  Everything after this chapter is built on top of it.
"""

from __future__ import annotations

from pathlib import Path

__all__ = ["__version__", "compaction_prompt", "permissions_prompt", "system_prompt"]

# The one place this number is written.  `pyproject.toml` declares the version
# `dynamic` and reads it from here, because for seventeen chapters it was
# written in both files and nothing compared them -- and the two-rulers fault
# has now cost this project a compaction budget (F06-12) and a truncated
# memory (chapter 17) already.  A version that disagrees with its own metadata
# is worse than either: it is the number in every bug report.
#
# 0.1.0 and not 1.0.0, deliberately.  Semantic versioning's 0.x escape hatch
# says the public API may change at any time -- which is honest about the
# Python API and says nothing at all about the files on disk, because a user's
# `.minicodex/` directory does not read the version number.  Chapter 15
# measures what that distinction costs.
__version__ = "0.1.0"

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
