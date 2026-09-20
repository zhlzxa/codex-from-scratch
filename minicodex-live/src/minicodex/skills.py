"""Skills: a name, one sentence and a path on every request; the body read on demand.

A skill is a directory with a `SKILL.md` file in it -- YAML frontmatter
(`name`, `description`), then a markdown body of instructions. codex
discovers these across several layered roots (project `.codex/skills/`, user
`~/.agents/skills/`, a bundled system cache, an admin root, and any plugin
roots -- `ext/skills/src/host_roots.rs:87-145`) and renders only `name` +
`description` + a **source locator** into a catalog; the body is read only
once the model decides a task matches one. This module is the same shape, cut
down to one root: `.minicodex/skills/<name>/SKILL.md`, project-scoped, no user
or system layer, no plugin layer, no `$name` mention sigil
(`skills/src/mentions.rs`). See the module for why those were left out.

**The catalog line carries a path, and that is not decoration**.
codex's line is `- {name}: {description} ({locator_kind}: {locator})`
(`ext/skills/src/render.rs:242-244`), and for a skill on the filesystem the
kind is the literal word `file` (`render.rs:200`). The instructions that go
with it say what to do with it: *"For a `file` entry, open the listed path"*
(`ext/skills/src/catalog_prompt.rs:7`). No dedicated tool is involved. The
`skills.list`/`skills.read` pair exists (`ext/skills/src/tools/{list,read}.rs`)
and the same sentence says exactly who it is for -- *"`environment resource`
and `orchestrator resource` entries must be accessed through `skills.list` and
`skills.read`"* (`catalog_prompt.rs:3`) -- a category of non-filesystem,
executor- or orchestrator-owned resource this program has no concept of at
all.

The first version of this module read codex's source, got progressive
disclosure exactly right, and still missed that sentence: it made `read_skill`
the *only* way to reach a body, which is the memory read side's dedicated-tools trap a second time
(there, `memory_search`/`memory_read` as the only path to memory). The default
here is now what codex's default is -- the catalog prints the path, the model
opens it with `read_file`, and the skills directory is reachable because
`local_tools` hands it to `paths.resolve(..., extra_roots=...)` the same way it
hands over the memory directory.

`read_skill` survives behind `--skill-tool`, and the reason it survives got
weaker when it was re-measured. It was kept because it scored better
than the alternative -- but the alternative it had been compared against was
*no way to read the body at all*, never against opening a path. Against the
real one it is a tie: 17/20 for the tool, 18/20 for `read_file`, both fetching
the body in 20 runs out of 20. So it is not an evidenced deviation any more,
just a demonstration: the shape is here, behind a flag, for a reader who wants
to see what a purpose-built tool costs against the file tool that was already
in the room. When two mechanisms measure the same, the one codex ships wins.

Deliberately unlike `memory.py`: the catalog is not capped by "did the budget
overflow" the way `resident_block` is. Memory has a tier below its dedicated
tools -- a resident summary that fits most of the time. A skill catalog has no
such tier: the catalog line is *all* that is ever resident, by construction,
so reading the body is never a fallback, it is the only way to ever see a
skill's instructions.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from minicodex.agent_types import ToolSet
from minicodex.tokens import estimate_messages
from minicodex.tool_errors import tool_error

# Project-scoped, and *not* moved to `~/.minicodex/` when the read side's memory
# was a design nobody checked. The two are different kinds of thing and codex keeps them
# apart too: its real skill roots are the project's `.codex/skills/`, the
# user's `~/.agents/skills/`, a bundled system cache and an admin root
# (`ext/skills/src/host_roots.rs:87-145`) -- and the memory root is not among
# them. A `skills/<name>/SKILL.md` written by codex's memory consolidation
# agent (`memories/write/templates/memories/consolidation.md`) is an ordinary
# file inside the memory store, reached with the memory tools; it never enters
# the skill catalog. This module got that separation right by instinct in its
# first version; the module now says so with the file:line rather than
# leaving it implicit.
DEFAULT_SKILLS_DIR = Path(".minicodex") / "skills"

SKILL_FILENAME = "SKILL.md"

# The word codex prints for a skill that lives on the filesystem
# (`ext/skills/src/render.rs:200`, `SkillSourceKind::Host => "file"`). Every
# skill this program can discover is one, because the other three kinds
# (`environment resource`, `orchestrator resource`, `custom resource`,
# `render.rs:201-203`) name sources that only exist inside codex's execution
# environment. Kept as the literal string rather than dropped as a constant
# nobody varies: it is what makes the catalog line recognisable to a reader
# who later opens codex's own.
FILE_LOCATOR_KIND = "file"

# codex's own parser caps `description` at 1024 characters
# (`core-skills/src/loader.rs:101`, `MAX_DESCRIPTION_LEN`; the extension's
# loader carries the same number at `ext/skills/src/loader/mod.rs:17`, and its
# renderer caps the catalog copy again at
# `MAX_CATALOG_SKILL_DESCRIPTION_CHARS = 1_024`, `ext/skills/src/render.rs:21`).
# Kept here for the same reason: the description is the part that is resident
# on *every* request, for as long as the skill exists, whether or not it is
# ever used.
MAX_DESCRIPTION_CHARS = 1024

# A ceiling on how many `SKILL.md` files one discovery pass will read, so
# that a directory nobody meant to point this at (a checked-out dependency,
# a mistake) costs a bounded amount of work rather than however long
# `iterdir()` takes to walk it. codex's own discovery walk caps depth and
# entry count for the same reason: `MAX_SCAN_DEPTH = 6` and
# `MAX_SKILLS_DIRS_PER_ROOT = 2000` (`core-skills/src/loader.rs:109-110`,
# duplicated for the extension at `ext/skills/src/loader/mod.rs:24-25`),
# applied together with `MAX_SKILLS_ENTRIES_PER_ROOT = 20_000` in
# `ext/skills/src/loader/discovery.rs:16,67-69`.
MAX_SKILLS = 200

# How much of the catalog (all the lines together) is allowed into every
# single request, the same idea as `memory.py`'s `SUMMARY_TOKEN_BUDGET` and
# for the same reason as the memory summary: a resident block with no cap is a second
# system prompt that grows without anyone deciding it should. codex bounds the
# same block two ways -- `DEFAULT_SKILL_METADATA_CHAR_BUDGET = 8_000` chars or
# `SKILL_METADATA_CONTEXT_WINDOW_PERCENT = 2` percent of the context window
# (`ext/skills/src/render.rs:19-20`) -- and shortens descriptions before it
# drops whole entries.
CATALOG_TOKEN_BUDGET = 400

# How many `read_skill` calls one task may make, when that opt-in tool is
# mounted at all. Mirrors `memory.py`'s `SEARCH_BUDGET` -- an instruction
# ("use it when it matches") is a request, a counter is a fact.
READ_BUDGET = 4

_FRONTMATTER = re.compile(
    r"\A---[ \t]*\r?\n(?P<yaml>.*?)\r?\n---[ \t]*\r?\n?(?P<body>.*)\Z", re.DOTALL
)
_FIELD = re.compile(r"^([A-Za-z_][A-Za-z0-9_-]*):[ \t]*(.*?)[ \t]*$")


@dataclass(frozen=True)
class Skill:
    """One parsed `SKILL.md`."""

    name: str
    description: str
    body: str
    path: Path

    def locator(self) -> str:
        """The absolute path, forward-slashed, as the catalog prints it.

        Absolute, not relative to the skills directory, and that is forced
        from both ends. codex has two catalog dialects and picks between them
        by whether a root alias table was supplied: `SKILLS_INTRO_WITH_
        ABSOLUTE_PATHS` when there is none, `SKILLS_INTRO_WITH_ALIASES` when
        there is (`ext/skills/src/catalog_prompt.rs:1-2`, chosen at
        `render_available_skills_body`, `catalog_prompt.rs:44-47`). This
        program has one root and no alias table, so it is the absolute
        dialect. And `paths.resolve` requires it anyway: a *relative* path is
        joined onto the repository root before anything else is checked, so a
        relative `.minicodex/skills/x/SKILL.md` would only ever be found if
        the skills directory happened to sit inside the repository. An
        absolute path wins that join and is then admitted by whichever
        `extra_roots` bound contains it.
        """
        return str(self.path.resolve()).replace("\\", "/")

    def catalog_line(self) -> str:
        """`- name: description (file: /abs/path/SKILL.md)`.

        The shape is codex's, copied field for field from
        `ext/skills/src/render.rs:244`:
        `format!("- {name}: {description} ({locator_kind}: {locator})")`.
        """
        return f"- {self.name}: {self.description} ({FILE_LOCATOR_KIND}: {self.locator()})"


@dataclass(frozen=True)
class Skills:
    """Every skill discovered under one directory, for one run, at one moment.

    Read once at startup, like `Memory` -- a skill directory that changed
    mid-session would make two turns of the same conversation disagree about
    which skills exist, with nothing in the transcript explaining why.
    """

    directory: Path
    skills: tuple[Skill, ...] = ()
    # Relative paths of `SKILL.md` files that exist but were not usable:
    # malformed frontmatter, a missing `name` or `description`, or a `name`
    # that collided with one already found. Kept rather than dropped
    # silently -- a skill that never shows up and never says why is the
    # `2>&1` trap this whole book keeps finding new copies of.
    skipped: tuple[str, ...] = ()
    empty_reason: str | None = None

    def __bool__(self) -> bool:
        return bool(self.skills)

    @property
    def non_empty(self) -> bool:
        """Named alternative to truthiness, for `and`-chains: a bare
        `skills and x` reads as a `None` check even though it also tests the
        content."""
        return bool(self)

    def by_name(self, name: str) -> Skill | None:
        for skill in self.skills:
            if skill.name == name:
                return skill
        return None

    def describe(self) -> str:
        if not self:
            extra = f", {len(self.skipped)} skipped" if self.skipped else ""
            return f"skills: empty ({self.empty_reason or 'nothing on disk'}{extra})"
        note = f", {len(self.skipped)} skipped" if self.skipped else ""
        return f"skills: {len(self.skills)} found in {self.directory}{note}"


EMPTY = Skills(directory=DEFAULT_SKILLS_DIR, empty_reason="no skills directory")


def _parse_frontmatter(text: str) -> tuple[dict[str, str], str] | None:
    """Split `---\\nkey: value\\n---\\nbody` into fields and body.

    Not a YAML parser. codex's does more -- `skills/src/parser.rs:98-181`
    (`repair_frontmatter_scalar_fields`) even re-quotes scalars a third-party
    skill left ambiguous, so that a `description: Build for AWS: ECS` still
    parses -- because a `SKILL.md` in the wild can nest lists and multi-line
    values under `metadata`. This project's frontmatter has exactly two
    fields, both one-line strings, so a two-line regex either matches that
    shape or the file is treated as unusable -- the same trade the project rules made
    everywhere else in this program: match the shape actually in front of you,
    refuse (not guess at) anything wider.
    """
    match = _FRONTMATTER.match(text)
    if match is None:
        return None
    fields: dict[str, str] = {}
    for line in match["yaml"].splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        field_match = _FIELD.match(stripped)
        if field_match is None:
            continue
        key, value = field_match.groups()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        fields[key] = value
    return fields, match["body"]


def _load_one(skill_md: Path) -> Skill | None:
    try:
        text = skill_md.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    parsed = _parse_frontmatter(text)
    if parsed is None:
        return None
    fields, body = parsed
    name = fields.get("name", "").strip()
    description = fields.get("description", "").strip()
    if not name or not description:
        return None
    if len(description) > MAX_DESCRIPTION_CHARS:
        description = description[: MAX_DESCRIPTION_CHARS - 1] + "…"
    return Skill(name=name, description=description, body=body.strip(), path=skill_md)


def discover(directory: Path = DEFAULT_SKILLS_DIR, *, max_skills: int = MAX_SKILLS) -> Skills:
    """Read a skills directory. Never raises for "there is nothing there".

    Each direct subdirectory of `directory` that contains a `SKILL.md` is one
    skill -- one level, not codex's bounded recursive walk
    (`ext/skills/src/loader/discovery.rs:52-165`), because this project has
    exactly one root to search instead of codex's project/user/system/admin/
    plugin stack (see the module for why the others were not added). A `name`
    that two skills claim is resolved by directory order: the first one found
    keeps the name, the rest are `skipped`, deterministically, rather than
    left to whichever happened to load last.

    `max_skills` is a parameter, not just the `MAX_SKILLS` constant used
    directly, for the same reason `SUMMARY_TOKEN_BUDGET` is a parameter to
    `memory.resident_block`: a cap that can only be exercised by creating
    hundreds of fixture directories is a cap nothing ends up testing.
    """
    directory = Path(directory)
    if not directory.is_dir():
        return Skills(directory=directory, empty_reason="no skills directory")

    found: list[Skill] = []
    skipped: list[str] = []
    seen: set[str] = set()
    for entry in sorted(directory.iterdir()):
        if not entry.is_dir():
            continue
        skill_md = entry / SKILL_FILENAME
        if not skill_md.is_file():
            continue
        if len(found) >= max_skills:
            skipped.append(str(skill_md.relative_to(directory)))
            continue
        skill = _load_one(skill_md)
        if skill is None or skill.name in seen:
            skipped.append(str(skill_md.relative_to(directory)))
            continue
        seen.add(skill.name)
        found.append(skill)

    if not found and not skipped:
        return Skills(directory=directory, empty_reason=f"no {SKILL_FILENAME} files under it")
    return Skills(directory=directory, skills=tuple(found), skipped=tuple(skipped))


# ---------------------------------------------------------------------------
# What the model is told, and what it is shown
# ---------------------------------------------------------------------------

_TRUNCATION_MARK = "[... skills truncated"


def _trim_to_budget(text: str, budget: int) -> str:
    """Cut a catalog down to `budget` tokens, at a line boundary.

    Same shape as `memory._trim_to_budget`: a line is one skill's whole
    entry, and half of one is worse than none -- it names something without a
    readable path, or describes it wrong. codex spends more effort here than
    this does, shortening every description proportionally before it drops any
    entry and warning the user when it has
    (`ext/skills/src/render.rs:24-27`); a whole-line cut is the version that
    fits a skill file.
    """
    if estimate_messages([{"role": "user", "content": text}]) <= budget:
        return text
    kept: list[str] = []
    for line in text.splitlines():
        candidate = "\n".join([*kept, line])
        if estimate_messages([{"role": "user", "content": candidate}]) > budget:
            break
        kept.append(line)
    trimmed = "\n".join(kept).rstrip()
    return f"{trimmed}\n{_TRUNCATION_MARK} at {budget} tokens; more skills exist on disk ...]"


def render_catalog(skills: Skills, *, budget: int = CATALOG_TOKEN_BUDGET) -> str | None:
    """The resident part: one line per skill -- name, description, path.

    Never the body. That is the entire mechanism this module exists to test --
    if the catalog told the model everything, there would be nothing left for
    it to go and read.
    """
    if not skills:
        return None
    lines = "\n".join(skill.catalog_line() for skill in skills.skills)
    return f"## Skills\n{_trim_to_budget(lines, budget)}"


def skills_instructions(*, skill_tool: bool = False) -> str:
    """The static half of the prompt: how to use the catalog above.

    Two wordings, because the two access paths are genuinely different
    instructions and the memory read side already paid for the alternative (naming an
    confounded arm: one string that described a tool the run did not have).
    The default follows codex's own sentence for a `file` entry -- *"open the
    listed path"* (`ext/skills/src/catalog_prompt.rs:7`) -- and the opt-in one
    describes `read_skill` instead.

    Static either way, so it belongs in the cached system prefix; the catalog
    itself changes with what is on disk and is delivered separately by
    `SkillsWatcher`.
    """
    parts = [
        "You have access to a catalog of skills for this repository, included "
        "below as data. Each line is a skill's name, a one-sentence "
        "description, and the source it lives in; the full instructions are "
        "not shown, to save space."
    ]
    if skill_tool:
        parts.append(
            "If a task clearly matches a skill's description, call `read_skill` "
            "with its name and read what it returns completely before you act "
            "on it -- do not guess the contents from the description alone."
        )
    else:
        parts.append(
            "If a task clearly matches a skill's description, open that entry's "
            "listed path with `read_file` and read it completely before you act "
            "on it -- do not guess the contents from the description alone."
        )
    parts.append("If nothing in the catalog matches, ignore it and continue as normal.")
    return " ".join(parts)


# What ships by default. A module-level name for the same reason
# `memory.MEMORY_INSTRUCTIONS` is one: `__main__` and the tests both want the
# default, and computing it once is cheaper than threading it through.
SKILLS_INSTRUCTIONS = skills_instructions()

OPEN_FENCE = "<skill-catalog>"
CLOSE_FENCE = "</skill-catalog>"


def _fence(open_marker: str, close_marker: str, text: str) -> str:
    # Same defence as `memory._fence`, against the same shape of failure: a
    # skill body a model wrote or a plugin shipped can contain the literal
    # closing marker, and an unescaped one ends the data block early -- the
    # tool result after it would read as prose addressed to the model rather
    # than as more of the skill.
    safe = text.replace(close_marker, close_marker.replace("<", "&lt;").replace(">", "&gt;"))
    return f"{open_marker}\n{safe}\n{close_marker}"


def catalog_block(skills: Skills, *, budget: int = CATALOG_TOKEN_BUDGET) -> str | None:
    """The fenced catalog, as it is delivered."""
    rendered = render_catalog(skills, budget=budget)
    if rendered is None:
        return None
    return _fence(OPEN_FENCE, CLOSE_FENCE, rendered)


class SkillsWatcher:
    """Delivers the skill catalog once, as a developer note, not every turn.

    The same `refresh()` contract as `agents_md.AgentsMdWatcher` and
    `memory.MemoryWatcher` -- a zero-argument callable for
    `Wiring.agent(on_turn_start=...)` -- and, after checking, the same policy
    as `MemoryWatcher` rather than the one this module first planned to give
    it (see docs/history.md).

    codex really does re-render this catalog **every turn**, not once:
    `SkillsExtension` implements `TurnInputContributor` and rebuilds the
    catalog fragment in `contribute` (`ext/skills/src/extension.rs:342-435`),
    on top of the once-per-thread `ContextContributor` path
    (`extension.rs:183-240`). So a literal reading of "what does codex do"
    says: speak on every turn. That reading is wrong here, for a reason
    visible one file over.

    What codex re-renders is a `ContextualUserFragment`, and those carry
    open/close markers -- `AvailableSkillsInstructions::type_markers()`
    returns `SKILLS_INSTRUCTIONS_OPEN_TAG`/`_CLOSE_TAG`
    (`ext/skills/src/fragments.rs:43-49`) -- precisely so the harness can find
    the previous copy and *replace* it. Re-rendering every turn costs codex
    nothing cumulative: there is one catalog in the request, always, and it is
    the current one. This program's `History` is append-only by the rollout's
    design, and has no replace-by-marker operation to offer. "Re-render every
    turn" translated literally into an append-only transcript is not codex's
    behaviour, it is N copies of the same catalog by turn N -- the failure
    compaction exists to clean up after, manufactured on purpose.

    So: speak once. It is the same conclusion `MemoryWatcher` reached, and for
    the same underlying reason, but it is worth arriving at separately because
    the surface facts point the other way -- and because the second half of
    codex's per-turn work has no counterpart here either. That other half is
    the full `SKILL.md` body injected for skills the user `$mentioned` this
    turn (`extension.rs:440-498`, `SkillInstructions`), which changes turn to
    turn precisely because the mention does. This program has no mention
    sigil, so its catalog is fixed from startup and there is nothing a second
    delivery could say that the first did not.
    """

    def __init__(self, skills: Skills, *, budget: int = CATALOG_TOKEN_BUDGET) -> None:
        self._skills = skills
        self._budget = budget
        self._delivered = False

    def refresh(self) -> str | None:
        if self._delivered:
            return None
        self._delivered = True
        return catalog_block(self._skills, budget=self._budget)


# ---------------------------------------------------------------------------
# The opt-in tool
# ---------------------------------------------------------------------------
#
# Everything below this line is off unless `--skill-tool` is passed, and it is
# the one piece of this module that is *not* a reproduction of codex.
#
# codex's `skills.list`/`skills.read` (`ext/skills/src/tools/list.rs:74-75`,
# `read.rs:58-59`) take an `authority`, a `package` and a `resource`, and the
# prompt that describes them says who they are for: `environment resource` and
# `orchestrator resource` entries, the ones whose bodies are not on the host
# filesystem at all (`ext/skills/src/catalog_prompt.rs:3,7`). Every skill this
# program can discover is a `file` entry, so codex's own instructions would
# have the model open the path -- which is what the default path above now
# does.
#
# It was kept, behind a flag, because it measured better -- and then it was
# measured against the right thing and stopped measuring better. Pooled over
# two runs of `probe_skills.py reach` (20 per arm, `gpt-4o-mini`): the tool
# arm followed the skill 17/20, the `read_file` arm 18/20, and **both fetched
# the body in every single run**. The number that justified the deviation was
# a comparison against a catalog with no read path at all, which is not the
# choice anybody faces.
#
# So this stays as a demonstration rather than as a recommendation, and the
# flag is what makes that honest: a reader can turn it on and watch a
# purpose-built tool do exactly as well as the file tool that was already
# there. What it is not is the silent default -- which is what it was in this
# first version of the memory dedicated tools, and what `memory_search` was
# in the memory read side's design.

READ_SCHEMA: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "read_skill",
        "description": (
            "Return the full instructions for one skill, by the name shown "
            "in the ## Skills catalog. Call this before following a skill -- "
            "the catalog only has its name and a one-sentence description."
        ),
        "parameters": {
            "type": "object",
            "required": ["name"],
            "properties": {
                "name": {
                    "type": "string",
                    "description": "The skill's name, exactly as printed in the catalog.",
                }
            },
        },
    },
}

SKILL_OPEN_FENCE = "<skill>"
SKILL_CLOSE_FENCE = "</skill>"

_BUDGET_SPENT = (
    "the skill-reading budget for this task is spent ({budget} calls). "
    "Continue with what you already read instead of reading another skill."
)


def skill_toolset(skills: Skills, *, budget: int = READ_BUDGET) -> ToolSet:
    """`read_skill`, bound to one run's discovered skills. Opt-in; see above.

    Looks a skill up by name in the already-parsed `skills.skills` tuple
    rather than rebuilding a path from the model's string and opening it --
    the same choice `memory.by_id` makes for `memory_read`. It is not a
    defence against a particular attack so much as the absence of an
    opportunity for one: there is no `directory / name / SKILL.md` anywhere
    in this function for a `name` of `"../../secrets"` to reach.

    Worth naming now that the default path is `read_file`: that path has no
    such absence, and does not need one. It goes through `paths.resolve`,
    which resolves first and judges second against every boundary it was
    given -- so a model that invents a path out of the skills
    directory is refused there, by the containment check the whole program
    already depends on, rather than by this function's shape.
    """
    spent = 0

    def charge() -> str | None:
        nonlocal spent
        if spent >= budget:
            return tool_error(
                _BUDGET_SPENT.format(budget=budget),
                do_this="Stop calling read_skill and continue with the task.",
            )
        spent += 1
        return None

    async def do_read(args: dict[str, Any]) -> str:
        name = args.get("name")
        if not isinstance(name, str) or not name.strip():
            return tool_error(
                'read_skill needs a "name" argument, a string',
                you_sent=repr(args.get("name")),
                do_this='Example: {"name": "run-tests"}',
            )
        denial = charge()
        if denial is not None:
            return denial
        skill = skills.by_name(name.strip())
        if skill is None:
            listed = ", ".join(s.name for s in skills.skills) or "(no skills available)"
            return tool_error(
                f"no skill named {name!r}",
                do_this=f"Names that exist: {listed}.",
            )
        return _fence(SKILL_OPEN_FENCE, SKILL_CLOSE_FENCE, skill.body)

    return ToolSet(handlers={"read_skill": do_read}, schemas=[READ_SCHEMA])


__all__ = [
    "CATALOG_TOKEN_BUDGET",
    "DEFAULT_SKILLS_DIR",
    "EMPTY",
    "FILE_LOCATOR_KIND",
    "MAX_DESCRIPTION_CHARS",
    "MAX_SKILLS",
    "READ_BUDGET",
    "SKILLS_INSTRUCTIONS",
    "SKILL_FILENAME",
    "Skill",
    "Skills",
    "SkillsWatcher",
    "catalog_block",
    "discover",
    "render_catalog",
    "skill_toolset",
    "skills_instructions",
]
