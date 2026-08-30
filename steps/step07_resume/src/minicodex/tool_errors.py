"""How a tool tells the model that something went wrong.

Measured, not preferred. Chapter 3 sent the same failed call back to two
models with two different error messages:

    bare:       "ValueError: invalid path"
    three-part: "Error: path must be relative to the repository root, not
                 absolute. You sent: /home/dev/... Send this instead: src/..."

gemma4:31b, given the bare one, sent an absolute path again three times out
of three -- it was trying to fix it (it dropped two directory levels) but had
no way to know which direction was wrong. Given the three-part one, it
recovered three times out of three.

gpt-4o-mini recovered from both. That is the trap: test the wording against
the stronger model only and every wording looks fine.

So the parts are not a style guide. They are the difference between a loop
that recovers on the next turn and one that spends its whole turn budget
resending the same call. `do_this` is a required keyword argument for that
reason -- an error message that does not say what to do next is the one that
measurably does not work.

This module imports nothing of ours. `tools.py` and `shell.py` both need it,
and `tools.py` already imports `shell.py`; putting it in either would build
the circular import that chapter 1 broke with `agent_types.py`. Same shape,
same answer, second time -- move the shared thing down to a leaf.
"""

from __future__ import annotations

ERROR_PREFIX = "Error:"
PERMISSION_PREFIX = "Permission denied:"


def _three_part(prefix: str, problem: str, you_sent: str | None, do_this: str) -> str:
    parts = [f"{prefix} {problem}"]
    if you_sent is not None:
        shown = you_sent if len(you_sent) <= 200 else you_sent[:200] + "..."
        parts.append(f"You sent: {shown}")
    parts.append(do_this)
    return " ".join(parts)


def tool_error(problem: str, *, you_sent: str | None = None, do_this: str) -> str:
    """Build the three-part message: what went wrong, what was received, what
    to do next.

    `you_sent` is optional because some failures have no input worth quoting
    back (a missing argument, say). `do_this` is not optional.
    """
    return _three_part(ERROR_PREFIX, problem, you_sent, do_this)


def permission_error(problem: str, *, you_sent: str | None = None, do_this: str) -> str:
    """The same three parts, under a prefix that means something different.

    Chapter 5 added this because the two kinds of failure need opposite
    responses and, written the same way, the model cannot tell them apart.

    An ordinary error is about the command: the path was wrong, the test
    failed, the argument was missing. The right response is to try something
    else, and retrying a *changed* version is exactly right.

    A permission decision is not about the command at all. It is about what
    this session is allowed to do, and it will not change because the model
    tried harder. Retrying is guaranteed to fail, and a model that treats it as
    an ordinary error spends its entire turn budget re-sending the same string.

    Two things carry the distinction: a prefix that is not the word "Error",
    and a `do_this` that says the retry will fail and names the one action that
    can change the answer. Both are load-bearing; the wording was measured, and
    what was measured is in the chapter.
    """
    return _three_part(PERMISSION_PREFIX, problem, you_sent, do_this)
