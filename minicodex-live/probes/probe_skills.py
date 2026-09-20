#!/usr/bin/env python
"""Chapter 18's measurements.

    uv run python probe_skills.py <section>

    cost     offline  F18-05  what a catalog costs per request, against the bodies
    reach    network  F18-07  five arms: off / catalog / +read_file / +tool / bodies

`reach` is the chapter's whole question.  Progressive disclosure is a bet:
that a name and one sentence are enough for a model to *notice* a skill
applies, and that it will then spend a round trip to read the rest.  Chapter 16
measured a structurally identical bet and lost it -- `memory_search` was
offered as an optional tool and called in 0 of 18 runs (F16-09).  Assuming
that result carries over is not measuring it, so this runs the arms.

There are five arms rather than four because F18-14 split the question the
old fourth arm was answering.  "Read the rest" has two implementations: open
the path the catalog printed with the ordinary file tool -- which is what
codex tells a model to do for a `file` entry
(`ext/skills/src/catalog_prompt.rs:7`), and what this chapter now ships -- or
call a purpose-built `read_skill`, which is what this chapter used to ship and
what codex reserves for skills that are not on a filesystem at all.  Both are
run, because the tool arm was kept in the codebase on the strength of this
number and a number kept for that reason has to be current.

It did not survive being current.  Two runs, `gpt-4o-mini`, 20 runs per arm
pooled (8 + 12):

    off                 0/20   --
    catalog             0/20   40 read_file attempts, every one refused
    catalog+read_file  18/20   body fetched 20/20
    catalog+tool       17/20   body fetched 20/20
    bodies             11/20   --

18 against 17 is not a difference, and the first of the two runs had it the
other way round (8/8 against 6/8) before the second reversed it (10/12 against
11/12) -- which is what a coin looks like when you only flip it eight times.
Both mechanisms fetch the body in **every single run**.  The old justification
for keeping `read_skill` was that it measured better than the alternative;
measured against the *right* alternative it measures the same, so that
justification is gone, and what breaks the tie is that one of the two is what
codex does.  See F18-14.

Two findings survive that have nothing to do with which tool is used:

* `catalog` alone is 0/20 and makes **40 refused `read_file` calls** -- the
  model reaches for the path in every run without being told twice.  What
  costs it the arm is not being unwilling to fetch, it is not being *allowed*
  to.  The behaviour progressive disclosure needs is already there.
* `bodies` -- every skill's full text resident, the arm built as the upper
  bound -- is the **worst** of the three arms that can see a skill at all,
  11/20, consistently across both runs (4/8, then 7/12).  It is also the
  cheapest in turns (3.0 against 4.2).  A model handed the instruction for
  free follows it *less* often than one that had to go and get it.

The network section needs `OPENAI_API_KEY`, read from the environment and
never printed.  Nothing here is a test; the tests are in
`tests/test_faults_ch18.py` and never touch the network.
"""

from __future__ import annotations

import argparse
import asyncio
import shutil
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

# `_openai_key` lives in `probe_memory` and is imported rather than copied for
# one reason: it is the only place in this repository that touches the
# variable, and a second copy is a second place for a key to end up somewhere
# it is printed.
from probe_memory import PROVIDERS, _openai_key

from minicodex import system_prompt
from minicodex.evals import (
    CALC,
    TEST_CALC,
    Arm,
    Result,
    Task,
    file_has,
    finished,
    run_task,
    shell_command_has,
)
from minicodex.model import ChatCompletionsModel
from minicodex.skills import (
    CATALOG_TOKEN_BUDGET,
    Skill,
    Skills,
    catalog_block,
    discover,
    render_catalog,
    skill_toolset,
    skills_instructions,
)
from minicodex.tokens import estimate_messages

HERE = Path(__file__).resolve().parent
SCRATCH = HERE / ".probe" / "ch18"


def _once(block: str | None) -> Callable[[], str | None] | None:
    """`SkillsWatcher`'s contract, without the class.

    The arms deliver different *content* (a catalog, or every body, or
    nothing), so they cannot all use `SkillsWatcher` -- but they must all
    deliver it the same way, or the arm is measuring the delivery too. Says
    it once, then `None`, which is what the shipped watcher does.
    """
    if block is None:
        return None
    said = False

    def refresh() -> str | None:
        nonlocal said
        if said:
            return None
        said = True
        return block

    return refresh


def build(provider: str, model: str | None = None):
    base, default = PROVIDERS[provider]

    def factory(tools: list[dict[str, Any]]) -> ChatCompletionsModel:
        return ChatCompletionsModel(
            base_url=base,
            model=model or default,
            api_key=_openai_key() if provider == "openai" else None,
            tools=tools,
        )

    return factory


def fresh(tag: str) -> Path:
    path = SCRATCH / tag
    if path.exists():
        shutil.rmtree(path)
    path.mkdir(parents=True)
    return path


# ---------------------------------------------------------------------------
# the fixture: two tasks, each with one skill that is the only place the
# right answer is written down
# ---------------------------------------------------------------------------
# The marker in each skill body is a string that appears *nowhere else* -- not
# in the question, not in the workspace, not in the catalog line. That is what
# makes the check honest: if it turns up in the trajectory, it came out of the
# skill body and nowhere else. Chapter 14's F14-08, applied to a new feature.

RUN_TESTS_BODY = """\
# Running this repository's test suite

Always run the suite as:

    python -m pytest -q --tb=no

The `--tb=no` is not optional. A bare `pytest` resolves to a different
interpreter on the build machines and reports import errors that are not
real; two afternoons have already been lost to chasing them.
"""

RELEASE_BODY = """\
# Cutting a release note

Add the note to `RELEASE.md`, under the heading that is already there. The
last line of every release note in this repository is exactly:

    Approved-By: release-bot

Nothing merges without it.
"""

# `RELEASE.md` is in the workspace, and it did not use to be. The first
# version of this fixture asked for a file that did not exist, and the model
# read the skill, wrote a correct note with the right last line, and then
# tried to deliver it as an `apply_patch` edit with `old_text:
# "Approved-By: release-bot"` against a file with no text in it at all. That
# is chapter 4's tool refusing to create files, three chapters away from
# anything this chapter built -- and the probe was about to report it as
# "the model ignored the skill". Fixture changed rather than the finding
# hidden; the finding is in the chapter.
RELEASE_MD = """\
# Release notes

<!-- newest first -->
"""

DIFF = """\
--- a/calc.py
+++ b/calc.py
@@
-def add(a, b):
+def add(a, b, *, clamp=False):
"""

SKILL_TASKS: tuple[tuple[Task, tuple[Skill, ...]], ...] = (
    (
        Task(
            name="runner",
            files={"calc.py": CALC, "test_calc.py": TEST_CALC},
            question="run the test suite and tell me whether it passes",
            checks=(shell_command_has("--tb=no"), finished()),
        ),
        (
            Skill(
                name="run-tests",
                description=(
                    "How to run this repository's test suite. Read this before running any tests."
                ),
                body=RUN_TESTS_BODY,
                path=Path("run-tests/SKILL.md"),
            ),
        ),
    ),
    (
        Task(
            name="release",
            files={"calc.py": CALC, "change.diff": DIFF, "RELEASE.md": RELEASE_MD},
            question="write a release note for the change in change.diff",
            checks=(file_has("RELEASE.md", "Approved-By: release-bot"), finished()),
            max_turns=8,
        ),
        (
            Skill(
                name="release-note",
                description=(
                    "How to write and where to put a release note for this "
                    "repository. Read this before writing one."
                ),
                body=RELEASE_BODY,
                path=Path("release-note/SKILL.md"),
            ),
        ),
    ),
)


def followed(result: Result) -> bool:
    """Did the run do the thing that is only written in the skill body?

    The first check of every task above *is* that question, so this asks the
    task rather than asking the trajectory again. The first version of this
    function re-implemented the check here and looked for the shell tool
    under the name `shell`; the tool is called `run_shell`, so it returned
    False for all twenty-four runs and the probe printed a tidy 0/6 in every
    arm -- including the arm where the instruction was fully resident, which
    is the arm that cannot fail. Chapter 6's rule for the fifth time: a
    measurement that reimplements the thing it measures measures the copy.
    """
    return bool(result.task.checks) and result.task.checks[0].name in result.passed


# ---------------------------------------------------------------------------
# arms
# ---------------------------------------------------------------------------
# Five, because "does progressive disclosure work" is several questions
# wearing one coat, and chapter 16's F16-09 is what happens when they are not
# separated:
#
#   off              nothing. Proves the marker cannot be guessed.
#   catalog          the model is told the skill exists and has no way to read
#                    it. Measures the risk nobody talks about: does it invent
#                    the body from the one-sentence description?
#   catalog+read_file  the shipped design (F18-14). The catalog prints a real
#                    path and the ordinary file tool can reach it.
#   catalog+tool     the opt-in deviation: a purpose-built `read_skill`.
#   bodies           every skill's full text resident, no second step. The
#                    upper bound progressive disclosure is trying to buy
#                    cheaply.
#
# The two middle arms differ in exactly one thing -- how the body is fetched.
# Same catalog, same fixture, same instruction *shape*; only the sentence
# naming the mechanism and the tool table change. That is the F16-09 lesson
# applied rather than restated: an arm that moves two things measures their
# sum.
CATALOG_ONLY_INSTRUCTIONS = (
    "You have access to a catalog of skills for this repository, included "
    "below as data. Each line is a skill's name and a one-sentence "
    "description. If a task clearly matches a skill's description, follow it. "
    "If nothing in the catalog matches, ignore it and continue as normal."
)
# The shipped wordings, imported rather than re-typed. Chapter 16 spent a
# measurement on a probe that described a tool its arm did not have; the way
# not to repeat it is for the arm to ask the module what it says.
CATALOG_AND_READ_FILE_INSTRUCTIONS = skills_instructions()
CATALOG_AND_TOOL_INSTRUCTIONS = skills_instructions(skill_tool=True)
BODIES_INSTRUCTIONS = (
    "The instructions for this repository's skills are included below as "
    "data. If a task clearly matches one, follow it. If nothing matches, "
    "ignore them and continue as normal."
)

ARMS = ("off", "catalog", "catalog+read_file", "catalog+tool", "bodies")
CATALOG_ARMS = ("catalog", "catalog+read_file", "catalog+tool")


def _instructions(arm: str) -> str:
    text = system_prompt().rstrip()
    paragraph = {
        "catalog": CATALOG_ONLY_INSTRUCTIONS,
        "catalog+read_file": CATALOG_AND_READ_FILE_INSTRUCTIONS,
        "catalog+tool": CATALOG_AND_TOOL_INSTRUCTIONS,
        "bodies": BODIES_INSTRUCTIONS,
    }.get(arm)
    return f"{text}\n\n{paragraph}" if paragraph else text


def _block(arm: str, skills: Skills) -> str | None:
    """What this arm delivers as a developer note before the question.

    `run_task` takes an `on_turn_start` callable now, not a `preamble` string
    -- chapter 16 retired the parameter (F16-11) -- so the caller below wraps
    this in a once-only closure, which is what `SkillsWatcher` is.
    """
    if arm in CATALOG_ARMS:
        return catalog_block(skills)
    if arm == "bodies":
        joined = "\n\n".join(f"## {s.name}\n{s.body}" for s in skills.skills)
        return f"<skill-instructions>\n{joined}\n</skill-instructions>"
    return None


def _on_disk(directory: Path, skills: Skills) -> Skills:
    """Write the fixture's skills out, so the catalog's paths are real.

    The `catalog+read_file` arm cannot be faked: its whole claim is that the
    path in the catalog line is one the model can open. Every other arm is
    indifferent to whether the files exist, and gets the same on-disk fixture
    anyway -- an arm that differs in two ways measures two things.
    """
    for skill in skills.skills:
        skill_dir = directory / skill.name
        skill_dir.mkdir(parents=True, exist_ok=True)
        (skill_dir / "SKILL.md").write_text(
            f"---\nname: {skill.name}\ndescription: {skill.description}\n---\n{skill.body}\n",
            encoding="utf-8",
        )
    return discover(directory)


async def reach(provider: str, samples: int, verbose: bool) -> None:
    factory = build(provider)
    print(f"provider={provider}  samples={samples}  tasks={len(SKILL_TASKS)}\n")
    rows: dict[str, dict[str, Any]] = {}
    for arm_name in ARMS:
        arm = Arm(arm_name)
        read_calls = 0
        obeyed = 0
        runs = 0
        # Turns, because the first clean run of this section made it obvious
        # that the price of reading a skill on demand is not paid in tokens
        # but in *round trips*: the two runs that missed had spent their turn
        # budget retrying an edit, one call further along than the arm that
        # was handed the same text for free.
        turns = 0
        unfinished = 0
        for sample in range(samples):
            for task, skill_tuple in SKILL_TASKS:
                space = fresh(f"{arm_name}-{task.name}-{sample}")
                # Outside the workspace on purpose: the skills directory is a
                # sibling, reachable only because `extra_read_roots` says so,
                # which is the containment story F18-14 rests on. A fixture
                # that put it *inside* the workspace would pass whether or not
                # that plumbing worked.
                skills_dir = space.parent / f"{space.name}-skills"
                if skills_dir.exists():
                    shutil.rmtree(skills_dir)
                skills = _on_disk(skills_dir, Skills(directory=skills_dir, skills=skill_tuple))
                block = _block(arm_name, skills)
                result = await run_task(
                    task,
                    factory,
                    workspace=space,
                    instructions=_instructions(arm_name),
                    extra_tools=skill_toolset(skills) if arm_name == "catalog+tool" else None,
                    on_turn_start=_once(block),
                    extra_read_roots=(skills_dir,) if arm_name == "catalog+read_file" else (),
                )
                arm.results.append(result)
                runs += 1
                read_calls += result.trajectory.count("read_skill") + sum(
                    1
                    for args in result.trajectory.arguments("read_file")
                    if "SKILL.md" in str(args.get("path", ""))
                )
                turns += result.trajectory.turns
                unfinished += result.trajectory.stop_reason != "completed"
                if followed(result):
                    obeyed += 1
                if verbose:
                    mark = "yes" if followed(result) else " no"
                    print(
                        f"    followed={mark}  "
                        f"{task.name}[{sample}]  {result.trajectory.describe()}"
                    )
        rows[arm_name] = {
            "runs": runs,
            "read": read_calls,
            "obeyed": obeyed,
            "turns": turns / runs,
            "unfinished": unfinished,
        }
        print(
            f"  {arm_name:<18} followed the skill {obeyed}/{runs}, "
            f"body fetched x{read_calls}, {turns / runs:.1f} turns avg, "
            f"{unfinished} out of turns"
        )

    print("\n| arm | followed the skill | body fetched | turns (avg) | ran out of turns |")
    print("|---|---|---|---|---|")
    for arm_name in ARMS:
        row = rows[arm_name]
        # "Fetched" counts whichever mechanism the arm has: a `read_skill`
        # call, or a `read_file` whose path is a SKILL.md. One column rather
        # than two, because the two arms it distinguishes are the whole point
        # of F18-14 and a column that is blank for most rows says less.
        #
        # The `catalog` arm counts *attempts*, and they are not zero: it is
        # given the same catalog, with the same real paths, and no
        # `extra_read_roots` to make them readable. Every count in that row is
        # a `read_file` the containment check refused. Printing it as `n/a`
        # was the first version of this table, and it hid the most useful
        # single number in the section -- the model does not need to be told
        # to open the path, it needs to be *able* to.
        if arm_name in ("catalog", "catalog+read_file", "catalog+tool"):
            fetched = str(row["read"])
            if arm_name == "catalog":
                fetched += " (refused)"
        else:
            fetched = "n/a"
        print(
            f"| {arm_name} | {row['obeyed']}/{row['runs']} | {fetched} | "
            f"{row['turns']:.1f} | {row['unfinished']}/{row['runs']} |"
        )


# ---------------------------------------------------------------------------
# cost  (F18-05, offline)
# ---------------------------------------------------------------------------


def cost() -> None:
    """What the resident half costs, against what it is standing in for."""
    every_skill = tuple(skill for _, pair in SKILL_TASKS for skill in pair)
    print(f"{len(every_skill)} skills\n")

    def tokens(text: str) -> int:
        return estimate_messages([{"role": "user", "content": text}])

    bodies = "\n\n".join(f"## {s.name}\n{s.body}" for s in every_skill)
    catalog = render_catalog(Skills(directory=Path("."), skills=every_skill)) or ""
    print(f"  full bodies, every request : {tokens(bodies):>5} tokens")
    print(f"  catalog only               : {tokens(catalog):>5} tokens")
    print(f"  budget ceiling             : {CATALOG_TOKEN_BUDGET:>5} tokens")

    # And the shape of the growth, which is the part a two-skill directory
    # hides: the catalog is per-request, so it is multiplied by every turn of
    # every session, and it grows with a directory nobody prunes.
    print("\n  catalog size as a directory fills up (one line each):")
    for count in (2, 10, 50, 200):
        many = tuple(
            Skill(
                name=f"skill-{i}",
                description=every_skill[i % len(every_skill)].description,
                body="",
                path=Path("x"),
            )
            for i in range(count)
        )
        rendered = render_catalog(Skills(directory=Path("."), skills=many)) or ""
        capped = tokens(rendered)
        uncapped = tokens(
            "## Skills\n" + "\n".join(s.catalog_line() for s in many),
        )
        note = "  <- capped" if capped < uncapped else ""
        print(f"    {count:>3} skills : {uncapped:>5} tokens uncapped, {capped:>4} sent{note}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("section", choices=("cost", "reach"))
    parser.add_argument("--provider", default="openai", choices=tuple(PROVIDERS))
    parser.add_argument("--samples", type=int, default=3)
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    if args.section == "cost":
        cost()
        return
    try:
        asyncio.run(reach(args.provider, args.samples, args.verbose))
    except KeyboardInterrupt:
        sys.exit(130)


if __name__ == "__main__":
    main()
