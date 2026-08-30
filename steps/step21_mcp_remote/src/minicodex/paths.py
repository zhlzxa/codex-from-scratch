"""Turning what the model sent into a file we can open.

Chapter 3 measured what two models actually send when asked to read
`src/minicodex/model.py`, given only "Path to the file." as the description:

    gpt-4o-mini    /home/dev/minicodex/model.py     absolute, wrong file
    gemma4:31b     model.py                         relative, wrong file

Both wrong, in different directions. Stating the rule in the description
fixed both -- but only when the example in the description happened to be the
answer. Change the example to a different file, keep the sentence identical,
and both models go back to being wrong, three times out of three. They were
copying the example, not learning the rule.

Which means this is not a description problem. An absolute path inside the
repository is unambiguous and can simply be accepted; a name that matches
exactly one file is unambiguous and can be named in the error. That is
deterministic work, and chapter -1's rule applies: anything that can be
settled in code should not be handed to the model.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from minicodex.tool_errors import tool_error

# Directories whose contents are never what the model meant.
_SKIP = {".git", "__pycache__", ".venv", "venv", "node_modules", ".pytest_cache", ".ruff_cache"}
_MAX_SUGGESTIONS = 5


def _candidates(name: str, root: Path) -> list[str]:
    """Files under `root` whose final component is `name`."""
    found: list[str] = []
    for path in root.rglob(name):
        if any(part in _SKIP for part in path.relative_to(root).parts):
            continue
        if path.is_file():
            found.append(str(path.relative_to(root)).replace("\\", "/"))
            if len(found) > _MAX_SUGGESTIONS:
                break
    return sorted(found)


def _contained(full: Path, root: Path) -> bool:
    try:
        full.relative_to(root)
    except ValueError:
        return False
    return True


def resolve(
    raw: str, root: Path, *, extra_roots: Sequence[Path] = ()
) -> tuple[Path | None, str | None]:
    """(path, None) if it resolves to a file inside `root` (or `extra_roots`), else (None, error).

    `extra_roots` is codex's `helper_readable_roots` (`core/src/config/mod.rs:
    3451,4044-4046`) arriving here: a short list of directories the sandbox
    admits for *reading* on top of the one it was built for, added by the
    caller rather than by this function -- `read_file` passes chapter 16's
    memory directory, `apply_patch` passes nothing, because codex's own
    version of this list only ever widens what the model may read, never what
    it may write. A relative path is still resolved against `root` first, the
    same as before this parameter existed; `extra_roots` is only consulted
    once that lookup is outside every boundary this call was given.

    The error is always a three-part message -- chapter 3 measured what
    happens with the other kind.
    """
    if not isinstance(raw, str) or not raw.strip():
        return None, tool_error(
            'this tool needs a "path" argument, a non-empty string',
            do_this='Example: {"path": "src/minicodex/model.py"}',
        )

    root = root.resolve()
    bounds = (root, *(extra.resolve() for extra in extra_roots))
    candidate = Path(raw)

    # Resolve first, judge second. Chapter 3 branched on `is_absolute()` and
    # only checked containment inside that branch, so `../../../../etc/passwd`
    # went straight through -- it is not absolute, and nothing else looked.
    # An absolute path inside the repository and a relative one that climbs out
    # are the same question asked twice; resolving first answers both.
    #
    full = (root / candidate).resolve()
    if not any(_contained(full, bound) for bound in bounds):
        return None, tool_error(
            "that path is outside the repository, and this tool only touches files inside it",
            you_sent=raw,
            do_this=f"Send a path relative to {root.name}/, for example src/minicodex/model.py",
        )
    if full.is_dir():
        return None, tool_error(
            "that path is a directory, not a file",
            you_sent=raw,
            do_this="Send the path of a file, or use run_shell with ls to list the directory.",
        )
    if not full.exists():
        near = _candidates(Path(raw).name, root)
        if len(near) == 1:
            do_this = f"Send this instead: {near[0]}"
        elif near:
            do_this = "Did you mean one of these? " + ", ".join(near[:_MAX_SUGGESTIONS])
        else:
            do_this = "Use run_shell with ls or find to see what exists, then try again."
        return None, tool_error("no such file in the repository", you_sent=raw, do_this=do_this)

    return full, None
