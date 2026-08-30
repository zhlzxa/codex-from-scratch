"""What this program promises to people who are not its author.

Three kinds of promise, and they have different lifetimes, which is the whole
of this module:

  * the **package version** -- one literal, in `__init__.py`, which
    `pyproject.toml` reads rather than repeats;
  * the **on-disk surfaces** -- the files a user still has after upgrading.
    These are the promises nobody writes down, and they outlive every release
    that made them;
  * the **spellings** -- the flags and commands people typed into scripts.
    A rename is free for the author and is not free for them, so each one
    carries a removal version that a test enforces.

None of it is about the agent.  It is about the fact that there is now somebody
on the other end.
"""

from __future__ import annotations

import os
import platform
import sys
from dataclasses import dataclass
from pathlib import Path

from minicodex import __version__
from minicodex.memory import BODY_FILE, DEFAULT_MEMORY_DIR, SUMMARY_FILE
from minicodex.memory_jobs import DEFAULT_JOBS_PATH
from minicodex.recorder import DEFAULT_DIR as RECORDINGS_DIR
from minicodex.rollout import DEFAULT_DIR as SESSIONS_DIR
from minicodex.rules import DEFAULT_RULES_PATH

# Where to send a bug.  In the metadata as `Project-URL` too, and that is the
# copy that matters: a user who has the wheel and not the repository has to be
# able to find this from the artefact alone.
ISSUES_URL = "https://github.com/example/minicodex/issues"


# ---------------------------------------------------------------------------
# The surfaces
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Surface:
    """One file this program reads that some earlier version of it wrote.

    The field that earns the type is `policy`: what happens when the file on
    disk is older than the code reading it.  Chapter 15 measured all five of
    these against fourteen real past versions, and the answers are not
    interchangeable --

        migrate   the information is there and its meaning is not in doubt
        refuse    the information is gone, and filling the gap with a default
                  produces a plausible run of something that never happened
        rewrite   the file is regenerable; the old one is replaced wholesale
        recreate  it was only ever a cache

    -- so each surface says which one it does, in the one place that also says
    where the file lives.

    `policy` is one word from that list and `note` is the prose, rather than
    one field holding both.  The first version had only the prose, and the
    test that wanted to check the policy had to parse a verb back out of a
    sentence -- inventing, in the test, a taxonomy the type did not have.  A
    field whose values a test has to interpret is a field with two meanings.
    """

    label: str
    path: Path
    version_marker: str
    policy: str
    note: str = ""


SURFACES: tuple[Surface, ...] = (
    Surface("sessions", SESSIONS_DIR, "type_version (chapter 7)", "refuse", "per record"),
    Surface("recordings", RECORDINGS_DIR, "none", "refuse", "names the missing field"),
    Surface(
        "memory",
        DEFAULT_MEMORY_DIR,
        f"`v1` on line 1 of {SUMMARY_FILE} and {BODY_FILE}",
        "rewrite",
        "a wrong line-1 marker means the whole file is regenerated, not patched",
    ),
    Surface(
        "approvals",
        DEFAULT_RULES_PATH,
        "none",
        "refuse",
        "a bad file yields the empty set of rules, never a partial one",
    ),
    # `version_marker` is "none" and not "sqlite user_version", which is what
    # the first draft said.  Nothing in `memory_jobs.py` ever sets
    # `user_version`; that entry was a description of what a careful person
    # would have done rather than of what this program does.  Every cell in a
    # table like this is an assertion, and an aspirational one reads exactly
    # like a true one.  Chapter 17 deleted a whole field (`ephemeral`) for the
    # same reason.
    Surface("memory jobs", DEFAULT_JOBS_PATH, "none", "recreate", "it is a cache"),
)


# ---------------------------------------------------------------------------
# The spellings
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Deprecation:
    """An old spelling that still works, and the release in which it stops.

    `remove_in` is not documentation.  `test_faults_ch15.py` compares it with
    `__version__` and goes red once the version reaches it, so the choice at
    that moment is to delete the alias or to move the date **on purpose**.  A
    deprecation with no enforced end is a second spelling with extra steps:
    every one of them survives, and the surface only grows.
    """

    old: tuple[str, ...]
    new: tuple[str, ...]
    since: str
    remove_in: str
    why: str

    @property
    def message(self) -> str:
        old = " ".join(self.old)
        new = " ".join(self.new)
        return (
            f"warning: `{old}` is deprecated since {self.since} and will be removed in "
            f"{self.remove_in}; use `{new}` instead ({self.why})"
        )


DEPRECATIONS: tuple[Deprecation, ...] = (
    Deprecation(
        old=("--yes",),
        new=("--dangerously-approve-all",),
        since="0.1.0",
        remove_in="0.3.0",
        why="it reads as an answer to one question and it is an answer to all of them",
    ),
    Deprecation(
        old=("forget",),
        new=("rules", "--forget"),
        since="0.1.0",
        remove_in="0.3.0",
        why="chapter 17 gave `forget` a second meaning, and it is the louder one",
    ),
)


def apply_deprecations(argv: list[str]) -> tuple[list[str], list[str]]:
    """Rewrite old spellings into current ones and say so, once each.

    Before `parse_args` rather than after, and that is not a detail: argparse
    can hold both spellings itself, and then every branch downstream has to
    remember that two attributes mean one thing.  Translating at the edge is
    chapter 1's rule (normalise at the boundary, and nothing above it knows
    there was ever a second shape) applied to the command line.

    A positional command is only rewritten in first position.  `minicodex ask
    "how do I forget a rule"` contains the word and is not the command.
    """
    out: list[str] = []
    warnings: list[str] = []
    seen: set[tuple[str, ...]] = set()
    for index, token in enumerate(argv):
        replaced = False
        for rule in DEPRECATIONS:
            if len(rule.old) != 1 or token != rule.old[0]:
                continue
            positional = not rule.old[0].startswith("-")
            if positional and index != 0:
                continue
            out.extend(rule.new)
            replaced = True
            if rule.old not in seen:
                seen.add(rule.old)
                warnings.append(rule.message)
            break
        if not replaced:
            out.append(token)
    return out, warnings


# ---------------------------------------------------------------------------
# The bug report
# ---------------------------------------------------------------------------
def _count(directory: Path, pattern: str) -> int:
    return len(list(directory.glob(pattern))) if directory.is_dir() else 0


def _newest_recording(root: Path) -> Path | None:
    found = sorted(
        (root / RECORDINGS_DIR).glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True
    )
    return found[0] if found else None


def environment_report(root: Path | None = None) -> str:
    """Everything a stranger has to paste for a report to be actionable.

    The list is not invented here.  Chapter 7's `SessionMeta` already had to
    decide what about the machine a transcript cannot reconstruct -- cwd,
    model, sandbox mode, approval policy -- because a resumed session depends
    on all four and mentions none of them.  A bug report has exactly that
    problem one level up, so this reports the same facts plus the two things
    only a fresh process knows: which version is installed, and where it was
    installed from.

    The last line is the one that does the work.  Everything above narrows the
    search; `minicodex replay <that file>` **is** the reproduction (chapter
    14), so the most useful thing this command can do is tell somebody which
    file to attach.
    """
    root = Path(root or Path.cwd())
    lines = [
        f"minicodex {__version__}",
        f"python    {platform.python_version()} "
        f"({sys.platform}, {platform.python_implementation()})",
        f"install   {Path(__file__).resolve().parent}",
        f"cwd       {root}",
    ]

    state = [
        f"{_count(root / SESSIONS_DIR, '*.jsonl')} session(s)",
        f"{_count(root / RECORDINGS_DIR, '*.jsonl')} recording(s)",
        f"approvals {'present' if (root / DEFAULT_RULES_PATH).exists() else 'none'}",
    ]
    lines.append(f"state     .minicodex: {', '.join(state)}")

    # Memory is reported on its own line, and that is chapter 16's move showing
    # up in the bug-report surface: it lives in `~/.minicodex/memories`, not
    # under this repository's `.minicodex/`, so listing it beside the sessions
    # would name the wrong directory to whoever is reading the report.  The
    # path is printed rather than implied for the same reason -- "memory
    # present" is not actionable if the reader has to guess where.
    lines.append(
        f"memory    {DEFAULT_MEMORY_DIR}: "
        f"{'present' if (DEFAULT_MEMORY_DIR / SUMMARY_FILE).exists() else 'none'}"
    )

    agents = root / "AGENTS.md"
    if agents.is_file():
        lines.append(f"project   AGENTS.md present ({agents.stat().st_size} bytes)")

    if os.environ.get("MINICODEX_BASE_URL"):  # only when it is not the default
        lines.append(f"base url  {os.environ['MINICODEX_BASE_URL']}")

    newest = _newest_recording(root)
    if newest is not None:
        lines.append(f"latest    {newest}")
        lines.append(f"          reproduce it with: minicodex replay {newest}")
    else:
        lines.append("latest    no recording in this directory yet")
    lines.append(f"report    {ISSUES_URL}")
    return "\n".join(lines)


def version_parts(version: str) -> tuple[int, ...]:
    """`"0.10.0"` -> `(0, 10, 0)`, so that 0.10 sorts above 0.9.

    Written because the obvious string comparison gets that pair backwards,
    and because the probe that sweeps this project's own past versions got the
    same class of thing wrong first: `stepA_refactor` sorts after
    `step13_system_prompt` and belongs seven chapters earlier.  Ordering is
    the one job a version number has.
    """
    return tuple(int(part) for part in version.split("-")[0].split("."))
