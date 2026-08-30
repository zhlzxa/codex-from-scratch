"""AGENTS.md: conventions a human wrote down, not a prompt the model wrote.

Every other piece of context in this program is either fixed at startup
(`system_prompt()`) or produced by the model itself (a plan, a compaction
summary). This is the first one a *person* authors, edits with a normal text
editor, and expects to take effect without restarting the agent -- codex reads
it from the filesystem on every turn rather than once at boot, and that
decision is kept here too.

Three failure shapes drive the design, all found by reasoning about the two
mechanisms already in this codebase that are closest to this one:

* Concatenating the text into `system_prompt()` puts human-authored content
  behind the same prefix-caching argument chapter 5 already made for
  permission state (F13-07/F05-10): the block that is *least* likely to
  change (the model's own instructions) would sit in front of the block most
  likely to (a convention file that gets edited, or a `cd` into a directory
  with a different one). So it is not appended to the system message at all;
  it is injected as its own message, appended after the system prompt is
  already fixed, using `History.add_developer_note` -- rendered as
  `role: "developer"`, chosen over `role: "user"` by measurement rather than
  by the wording of the fault list (`history.DeveloperNote`'s docstring has
  the numbers, F13-12).

* The history is append-only (chapter 7). A `cd` into a directory with a
  different `AGENTS.md` cannot un-say the one already on record, so it is not
  silently replaced -- a fresh note says plainly that the old one no longer
  applies (F13-09), the same shape as chapter 7's `environment_note` for a
  resumed session whose environment moved out from under it.

* `run_shell`'s `cd` is not path-checked (chapter 2 intercepts it for state,
  not for containment -- see `shell.ShellSession._handle_cd`), so an agent's
  cwd can end up outside the sandboxed root the same way a shell can always
  wander outside a directory. Walking upward from *there* looking for a
  marker would be F13-11's second failure mode reopened: this module never
  looks above `sandbox_root`, and a cwd that has already escaped it is
  treated as "outside the repository", not as a reason to widen the search.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

FILENAME = "AGENTS.md"
ROOT_MARKER = ".git"

# codex's `project_doc_max_bytes` default (`core/src/agents_md.rs`). Found the
# same way F02-03's head/tail clip and F06-09's per-message clip were: a
# single file is not bounded by anything else in this program, so a 200KB
# convention file would spend the whole context window before any real work
# started (F13-10).
MAX_BYTES = 32 * 1024

_TRUNCATION_NOTE = "\n\n[... AGENTS.md truncated at {max_bytes} bytes ...]"


@dataclass(frozen=True)
class ProjectDocs:
    """What was actually found and read, for one cwd, at one moment."""

    text: str
    truncated: bool
    # Relative to `sandbox_root`, root to cwd, in read order. Empty means no
    # AGENTS.md existed anywhere on the path -- which is different from "read
    # and empty" only in that the latter still counts as "found something",
    # for the change-detection in `AgentsMdWatcher`.
    sources: tuple[str, ...] = field(default_factory=tuple)

    def __bool__(self) -> bool:
        return bool(self.sources)


NONE_FOUND = ProjectDocs(text="", truncated=False, sources=())


def find_project_root(cwd: Path, sandbox_root: Path, *, marker: str = ROOT_MARKER) -> Path:
    """Nearest ancestor of `cwd` (inclusive) carrying `marker`, never above `sandbox_root`.

    Two things this deliberately does not do:

    * It does not walk from the filesystem root down. A marker two levels
      above `cwd` in someone else's home directory is not this repository's
      root just because it is the nearest one -- `sandbox_root` is the
      furthest this search is ever allowed to go, because it is the same
      boundary `paths.resolve()` already enforces for `read_file` and
      `apply_patch` (F04-12, F05-03).

    * It does not require `cwd` to be inside `sandbox_root` at all. `cd` is
      not containment-checked (see the module docstring), so `cwd` may
      already be outside it. In that case there is no ancestor search to do:
      the function returns `sandbox_root` unchanged, and `load_project_docs`
      below will find nothing between it and a `cwd` it does not contain.
    """
    cwd = cwd.resolve()
    sandbox_root = sandbox_root.resolve()
    try:
        cwd.relative_to(sandbox_root)
    except ValueError:
        return sandbox_root

    current = cwd
    while True:
        if (current / marker).exists():
            return current
        if current == sandbox_root:
            return sandbox_root
        current = current.parent


def _chain(root: Path, cwd: Path) -> list[Path]:
    """`root`, then each directory down to `cwd` inclusive -- or `[]` if `cwd`
    is not under `root` at all.

    Empty, not `[root]`: the first version of this function fell back to
    `[root]` here, on the theory that a `cwd` outside the sandbox has "no
    subdirectory to add" so the root's own file should still apply.  Measured
    directly (`probe_system_prompt.py agentsmd`, "cwd wandered outside the
    sandbox root"): an agent whose shell had `cd`'d to `/tmp/somewhere-else`
    was handed `AGENTS.md`'s "Use uv, not pip" as if it described
    `/tmp/somewhere-else`, which it says nothing about.  A cwd this far from
    home gets no project docs at all, not the root's by default.
    """
    cwd = cwd.resolve()
    root = root.resolve()
    try:
        rel = cwd.relative_to(root)
    except ValueError:
        return []
    chain = [root]
    current = root
    for part in rel.parts:
        current = current / part
        chain.append(current)
    return chain


def load_project_docs(sandbox_root: Path, cwd: Path, *, max_bytes: int = MAX_BYTES) -> ProjectDocs:
    """Concatenate every `AGENTS.md` from the project root down to `cwd`.

    Root-to-leaf order, not the other way round: a subdirectory's file is
    read *after* the root's, so a more specific convention appears later in
    the block and (per how these models were measured to treat position, see
    chapter -1's docstring on `system_prompt`) reads as the more specific,
    later word on the subject rather than something the root file overrides.

    One combined byte ceiling across every file in the chain, not one ceiling
    per file: a project root with a reasonable `AGENTS.md` and one enormous
    subdirectory file should still fit inside the same budget a single
    enormous root file would have been capped to.
    """
    root = find_project_root(cwd, sandbox_root)
    parts: list[str] = []
    sources: list[str] = []
    total = 0
    truncated = False

    for directory in _chain(root, cwd):
        candidate = directory / FILENAME
        if not candidate.is_file():
            continue
        raw = candidate.read_text(encoding="utf-8", errors="replace")
        try:
            label = str(candidate.relative_to(sandbox_root))
        except ValueError:
            label = str(candidate)
        label = label.replace("\\", "/")

        remaining = max_bytes - total
        if remaining <= 0:
            truncated = True
            break
        encoded = raw.encode("utf-8")
        if len(encoded) > remaining:
            raw = encoded[:remaining].decode("utf-8", errors="ignore")
            truncated = True

        parts.append(f"# {label}\n\n{raw.strip()}")
        sources.append(label)
        total += len(raw.encode("utf-8"))
        if truncated:
            break

    if not sources:
        return NONE_FOUND

    text = "\n\n---\n\n".join(parts)
    if truncated:
        text += _TRUNCATION_NOTE.format(max_bytes=max_bytes)
    return ProjectDocs(text=text, truncated=truncated, sources=tuple(sources))


def _block(docs: ProjectDocs) -> str:
    listed = ", ".join(docs.sources)
    return (
        f"# Project conventions ({listed})\n\n"
        "A person wrote this, not the model that is talking to you now. Where "
        "it conflicts with your general defaults, follow it -- that is what it "
        "is for.\n\n" + docs.text
    )


# codex's own names for these two notices (`context/world_state/agents_md.rs`),
# kept because a reader who later opens the real source recognises the words.
REPLACEMENT_NOTICE = (
    "The AGENTS.md conventions shown earlier in this conversation no longer "
    "apply -- the working directory changed and a different set is now in "
    "effect. Use the block below instead; do not keep following the old one."
)
REMOVAL_NOTICE = (
    "The AGENTS.md conventions shown earlier in this conversation no longer "
    "apply -- the working directory changed and no AGENTS.md exists here. "
    "There is nothing to replace them with; fall back to your general defaults."
)


class AgentsMdWatcher:
    """Re-checks AGENTS.md once per turn and says what changed, if anything.

    Holds the one piece of state this whole module needs: what was injected
    last. `refresh()` is meant to be handed to `Wiring.agent(on_turn_start=...)`
    as a zero-argument closure -- the same shape as chapter 11's
    `unfinished_note(plan)` -- bound to *this run's* shell, so a sub-agent
    that gets no `on_turn_start` at all (an absence, like its missing MCP
    tools in chapter 10) never confuses its own cwd with its parent's.
    """

    def __init__(self, sandbox_root: Path, *, max_bytes: int = MAX_BYTES) -> None:
        self._root = sandbox_root
        self._max_bytes = max_bytes
        self._last: ProjectDocs = NONE_FOUND

    def refresh(self, cwd: Path) -> str | None:
        current = load_project_docs(self._root, cwd, max_bytes=self._max_bytes)

        if current.sources == self._last.sources and current.text == self._last.text:
            return None

        # Whether the *previous* check found anything -- not a separate flag.
        # An earlier version tracked "has this ever injected something" apart
        # from "did the last check have content", and the two disagree after
        # a removal: content that reappears once it has been removed once
        # would have opened with "no longer apply", referring to a notice
        # that already said there was nothing to replace.
        had_content = bool(self._last)
        self._last = current

        if not current:
            # `had_content` is not checked here, and an earlier version did:
            # `REMOVAL_NOTICE if had_content else None`. It cannot be `False`
            # at this point -- the "nothing changed" check above already
            # caught the one case that would make it so (no docs before, no
            # docs now: `current` and `self._last` are both the same empty
            # `NONE_FOUND` value, equal, and the function returned already).
            # Reaching here with an empty `current` means the previous state
            # was *not* equal to empty, which means it had content. Mutation
            # testing caught the dead branch (`probe_mutations_ch13.py`);
            # chapter 8 has the same shape ("a dead enforcement point").
            return REMOVAL_NOTICE

        block = _block(current)
        if not had_content:
            return block
        return f"{REPLACEMENT_NOTICE}\n\n{block}"
