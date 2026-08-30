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


def tool_error(problem: str, *, you_sent: str | None = None, do_this: str) -> str:
    """Build the three-part message: what went wrong, what was received, what
    to do next.

    `you_sent` is optional because some failures have no input worth quoting
    back (a missing argument, say). `do_this` is not optional.
    """
    parts = [f"Error: {problem}"]
    if you_sent is not None:
        shown = you_sent if len(you_sent) <= 200 else you_sent[:200] + "..."
        parts.append(f"You sent: {shown}")
    parts.append(do_this)
    return " ".join(parts)
