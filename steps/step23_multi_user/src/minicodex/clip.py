"""Keeping the head and the tail of something too long.

The fourth call site is what created this module.  Chapter 2 wrote `_clip` for
shell output; chapter 9 wrote the same six lines again for MCP results (same
20,000 limit, same half-and-half split, a different omission message); chapter
10 needs it a third time for what a sub-agent returns.  Two copies is a
tolerable duplicate with a TODO on it, which is what chapter 9 left.  Three is
where the shape stops being a guess: every caller wants the same two things
(a character limit, and to be told what was dropped), and nobody has ever
wanted anything else.

`compaction.clip_item` is deliberately *not* folded in here.  It looks similar
and is not: it budgets in tokens rather than characters, and it takes and
returns a `HistoryItem` rather than a string.  Merging them would mean one
function with a mode flag, which is two functions wearing a coat.
"""

from __future__ import annotations

# What chapters 2 and 9 both independently picked.  Kept as the default so that
# extracting this function changes no behaviour anywhere.
DEFAULT_LIMIT = 20_000


def clip(text: str, limit: int = DEFAULT_LIMIT) -> str:
    """Keep the first half and the last half, say how much went missing.

    Head *and* tail, which is F02-03: a failing test run puts the traceback
    near the top and the summary line at the bottom, and tail-only truncation
    throws away exactly the part that explains the failure.

    Slicing a `str` is safe: Python strings are sequences of characters, so
    there is no multi-byte character to cut in half (F02-11).  The same code
    over `bytes` would produce mojibake at both seams.
    """
    if len(text) <= limit:
        return text
    head = limit // 2
    tail = limit - head
    omitted = len(text) - head - tail
    return f"{text[:head]}\n... ({omitted} characters omitted) ...\n{text[-tail:]}"


__all__ = ["DEFAULT_LIMIT", "clip"]
