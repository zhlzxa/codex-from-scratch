"""Applying an edit the model described, to a file it cannot see while it types.

Everything here exists because of something measured (see docs/history.md).

`str.replace()` is not one of the options: asked to change one of three
identical guard clauses, it changes all three and reports success.

Matching is graded. A model retyping a block gets the characters right and
the whitespace approximately right, so an exact match is tried first, then
one that ignores trailing whitespace, then one that ignores indentation
entirely. Each level must find exactly one site. Finding several is an error
at every level -- guessing which one was meant is how the wrong function
gets edited, silently.

Line endings are normalised on the way in and restored on the way out. A
CRLF file read with universal newlines matches happily and then writes back
as LF, so a one-line edit rewrites every line ending in the file. That is
invisible in the tool output and enormous in the diff.
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from minicodex.paths import resolve
from minicodex.tool_errors import tool_error


@dataclass(frozen=True)
class Edit:
    """One replacement, as the model described it."""

    path: str
    old_text: str
    new_text: str


# -- reading and writing without disturbing what we did not touch -----------


def read_source(path: Path) -> tuple[str, str]:
    """(text with LF endings, the ending the file actually uses).

    `newline=""` stops Python translating on read, which is what makes it
    possible to notice CRLF at all. `Path.read_text()` grew a `newline`
    parameter in 3.13; this has to work on 3.10, so it uses `open`.
    """
    with open(path, encoding="utf-8", newline="") as fh:
        raw = fh.read()
    if "\r\n" in raw:
        return raw.replace("\r\n", "\n"), "\r\n"
    return raw, "\n"


def write_source(path: Path, text: str, ending: str) -> None:
    if ending != "\n":
        text = text.replace("\n", ending)
    with open(path, "w", encoding="utf-8", newline="") as fh:
        fh.write(text)


# -- finding the site -------------------------------------------------------


def _exact(content: str, old: str) -> list[tuple[int, int]]:
    spans, i = [], content.find(old)
    while i != -1:
        spans.append((i, i + len(old)))
        i = content.find(old, i + 1)
    return spans


def _trailing_insensitive(content: str, old: str) -> list[tuple[int, int]]:
    """Same text, but trailing whitespace on each line need not agree."""
    pattern = "\n".join(re.escape(ln.rstrip()) + r"[ \t]*" for ln in old.split("\n"))
    return [(m.start(), m.end()) for m in re.finditer(pattern, content)]


def _indentation_insensitive(content: str, old: str) -> list[tuple[int, int]]:
    """Last resort: the lines are right, the indentation is not.

    Reindenting is a real thing models do when quoting a nested block back,
    and refusing the edit over it wastes a turn. Blank lines inside the block
    are dropped from the pattern because a model rarely reproduces them
    exactly.
    """
    parts = [re.escape(ln.strip()) for ln in old.split("\n") if ln.strip()]
    if not parts:
        return []
    pattern = r"[ \t]*" + r"[ \t]*\n[ \t]*".join(parts) + r"[ \t]*"
    return [(m.start(), m.end()) for m in re.finditer(pattern, content)]


_LEVELS = (
    ("exactly", _exact),
    ("ignoring trailing whitespace", _trailing_insensitive),
    ("ignoring indentation", _indentation_insensitive),
)


def locate(content: str, old: str) -> tuple[tuple[int, int] | None, str | None]:
    """(span, None) on a unique hit, else (None, why it failed)."""
    for how, find in _LEVELS:
        spans = find(content, old)
        if len(spans) == 1:
            return spans[0], None
        if len(spans) > 1:
            lines = [content[:s].count("\n") + 1 for s, _ in spans]
            return None, (
                f"that text appears {len(spans)} times (matching {how}), on lines "
                f"{', '.join(map(str, lines))}"
            )
    return None, "that text is not in the file"


# -- checking the result ----------------------------------------------------


def syntax_error(path: Path, text: str) -> str | None:
    """A parse error, for the file types we can parse. `None` otherwise."""
    if path.suffix != ".py":
        return None
    try:
        ast.parse(text)
    except SyntaxError as exc:
        return f"{exc.msg} at line {exc.lineno}"
    return None


# -- the whole patch, all or nothing ---------------------------------------


def apply_edits(edits: list[Edit], root: Path, audit: Any = None, actor: str = "-") -> str:
    """Validate every edit, then write every edit, or write nothing.

    Measured: writing as it goes leaves the repository half-edited when the
    third of five hunks does not match -- a state the model did not ask for
    and cannot see. One failure, nothing written, one error to act on.

    `audit` is the console's append-only trail, passed from the shell session
    that carries it (see `web/runtime._AuditedShell`).  Written only after
    every edit is on disk -- a refused or failed patch is not a file change,
    and the audit trail records what happened to files, not what was
    attempted.  `None` for every non-console caller, which is the shape "this
    feature is the server's, the core stays silent" has taken since the web
    20's sandbox.
    """
    if not edits:
        return tool_error(
            "the patch contained no edits",
            do_this="Send at least one edit with path, old_text and new_text.",
        )

    planned: list[tuple[Path, str, str]] = []
    for index, edit in enumerate(edits, 1):
        where = f"edit {index} of {len(edits)} ({edit.path})" if len(edits) > 1 else edit.path

        path, error = resolve(edit.path, root)
        if error is not None:
            return error
        assert path is not None

        content, ending = read_source(path)
        span, why = locate(content, edit.old_text)
        if span is None:
            return tool_error(
                f"{where}: {why}",
                you_sent=edit.old_text,
                do_this=(
                    "Re-read the file and copy the text to replace exactly as it "
                    "appears, including enough surrounding lines to make it unique."
                ),
            )

        start, end = span
        updated = content[:start] + edit.new_text + content[end:]

        broken = syntax_error(path, updated)
        if broken is not None:
            return tool_error(
                f"{where}: the edit would leave the file unparseable: {broken}",
                do_this=(
                    "Nothing has been written. Re-read the file and send an edit "
                    "that leaves it syntactically valid."
                ),
            )

        planned.append((path, updated, ending))

    for path, text, ending in planned:
        write_source(path, text, ending)

    names = ", ".join(sorted({e.path for e in edits}))
    if audit is not None:
        audit.append("patch", actor, files=sorted({e.path for e in edits}), edits=len(edits))
    return f"Applied {len(edits)} edit(s) to {names}."
