"""Keeping the head and the tail of something too long.

The third call site is what created this module: the shell output clipper,
the MCP result path and the sub-agent result path each wanted the same two
things (a character limit, and to be told what was dropped), and nobody has
ever wanted anything else.

`compaction.clip_item` is deliberately *not* folded in here.  It looks similar
and is not: it budgets in tokens rather than characters, and it takes and
returns a `HistoryItem` rather than a string.  Merging them would mean one
function with a mode flag, which is two functions wearing a coat.
"""

from __future__ import annotations

# What two callers independently picked before this existed.  Kept as the
# default so that extracting this function changes no behaviour anywhere.
DEFAULT_LIMIT = 20_000


def clip(text: str, limit: int = DEFAULT_LIMIT) -> str:
    """Keep the first half and the last half, say how much went missing.

    Head *and* tail: a failing test run puts the traceback near the top and
    the summary line at the bottom, and tail-only truncation throws away
    exactly the part that explains the failure.

    Slicing a `str` is safe: Python strings are sequences of characters, so
    there is no multi-byte character to cut in half.  The same code over
    `bytes` would produce mojibake at both seams.
    """
    if len(text) <= limit:
        return text
    head = limit // 2
    tail = limit - head
    omitted = len(text) - head - tail
    return f"{text[:head]}\n... ({omitted} characters omitted) ...\n{text[-tail:]}"


__all__ = ["DEFAULT_LIMIT", "clip"]
