"""Memory, write side: extract, merge, and forget.

Chapter 16 read a memory a person had written.  This module is what writes one,
and it is two stages with a temporary file between them, because they are two
different jobs:

* **Stage 1, per session.**  One finished conversation in, at most a handful of
  durable statements out.  Cheap, parallelisable, and allowed -- encouraged --
  to produce nothing at all.  codex runs its equivalent eight at a time
  (`CONCURRENCY_LIMIT: usize = 8`, `memories/write/src/lib.rs:81`); this one
  runs them one after another, because two is the batch size and a pool for
  two items is a pool for nobody.
* **Stage 2, per memory.**  Every pending stage-1 output, plus whatever the
  model proposed during a session, plus whatever the *user* edited by hand,
  merged into the two files chapter 16 reads.  Runs under a lock, once.

The seam between them is `raw/<session id>.md`, and it says in its own first
lines that it is temporary.  codex writes the same kind of file with the same
warning -- "Temporary file: merged raw memories from Phase 1. Input for Phase
2." (`memories/write/templates/memories/consolidation.md:29`) -- and the
reason is not tidiness: an intermediate product that does not say it is
intermediate is one somebody starts maintaining.

Three rules that shape everything below.

**The model never writes the memory files.**  It writes stage-1 output, it can
append a note, and that is the whole of its access.  Both of those are
*proposals*; this module is the only thing that opens `MEMORY.md` for writing,
it validates before it does, and it holds a lock while it does.  Chapter 7
refused two writers on a rollout and chapter 8 refused two concurrent writers
of a file; this is the same rule arriving a third time, and F17-09 is what it
looks like when it is missing.

**Forgetting is a feature, not a maintenance script.**  A memory that only
grows is one nobody dares read and nobody dares delete.  Entries leave by
citation count first (chapter 16's `usage.json`), recency second, and age only
as a tie-breaker -- the nine-month-old entry consulted every week is the one a
by-age rule deletes first (F17-07).

**Nothing here is on by default.**  `--remember` is a flag, the directory is
somewhere a user can see, and one command deletes all of it (F17-11).
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from minicodex.agent_types import ToolSet
from minicodex.clip import clip
from minicodex.history import (
    AssistantMessage,
    ToolResult,
    UserMessage,
)
from minicodex.memory import (
    BODY_FILE,
    FORMAT_VERSION,
    MINICODEX_HOME,
    SUMMARY_FILE,
    SUMMARY_TOKEN_BUDGET,
    USAGE_FILE,
    Entry,
    Memory,
    overflows,
    parse_entries,
)
from minicodex.rollout import Rollout
from minicodex.tokens import estimate_messages
from minicodex.tool_errors import tool_error

# Stage-1 output, one file per session.  Inside the memory directory rather
# than beside the sessions, because it is memory-shaped material and because
# `--forget-all` has to be able to find it: a user who deletes their memory and
# leaves a directory of extracted preferences behind has not deleted their
# memory.
RAW_DIR = "raw"

# What a *model* may add, and the only thing it may add.  codex calls the same
# directory `extensions/ad_hoc/notes/` and its read-path prompt states the rule
# in as many words: propose, never edit.  One file per note, named by time and
# pid, so two writers cannot collide and nothing is ever overwritten -- chapter
# 7's append-only, applied to a directory instead of to a file.
NOTES_DIR = "notes"

# The single-writer lock for stage 2.  Outside the memory directory, next to
# the jobs database, for the same reason: the memory directory is a git
# repository and its contents are the material being diffed.
#
# Under `MINICODEX_HOME` rather than under the repository, and that moved in
# this chapter's rewrite along with the memory directory itself (F17-12).  It
# has to: the lock guards writes to `~/.minicodex/memories`, which is one
# directory shared by every checkout on the machine, so a per-repository lock
# would let two repositories merge the same memory at the same time -- the
# exact race the lock exists to prevent, reintroduced by the lock being in the
# wrong place.  codex keeps the same separation for the same reason: the
# memory root is global (`memories/write/src/lib.rs:116-117`) and its job and
# lease state is global too, in its own database
# (`state/memory_migrations/0001_memories.sql`), never inside the directory
# the model reads.
#
# This is the *second* implementation of chapter 7's `O_EXCL` lock file and it
# is a deliberate copy, not an extraction.  The rule of three says the second
# occurrence is copied with a note pointing at the first; a third site --
# something else in this program needing "one process at a time, and say whose
# pid it is" -- is what would justify a `locks.py`.  Two sites of twelve lines
# do not.
MERGE_LOCK = MINICODEX_HOME / "memory_merge.lock"

# How much of one session goes to the extractor.  Sessions are long, the useful
# part of them is not, and the alternative to a bound here is chapter 6 all
# over again in a place with no compaction loop.
TRANSCRIPT_TOKEN_BUDGET = 6000

# How much of one tool result survives into the transcript.  Chapter 2's clip,
# at chapter 2's shape (head and tail): the beginning of a stack trace is where
# the failure is named and the end is where the exit code is.
TOOL_RESULT_CHARS = 800

# How many entries the body file may hold, and how long an uncited entry lives.
# Both are ceilings rather than targets -- a memory of eight good sections
# never approaches either.
MAX_ENTRIES = 40
UNUSED_DAYS = 30.0

# Below this much remaining quota, the background writer does not run at all.
# codex's own number and its own default: `DEFAULT_MEMORIES_MIN_RATE_LIMIT_
# REMAINING_PERCENT: i64 = 25` (`config/src/types.rs:49`), configurable as
# `memories.min_rate_limit_remaining_percent` (`config/src/types.rs:314,333`)
# and checked before the pipeline runs at all -- `guard::rate_limits_ok`
# (`memories/write/src/guard.rs:9,38`), which reads it, asks the backend for a
# rate-limit snapshot, and skips the whole run if the remaining capacity is
# under it.  The general rule it encodes is worth more than the number:
# **background work gives way to foreground work**, and the failure it prevents
# is the one where a user's actual task fails because the program was busy
# writing notes to itself.
MIN_RATE_LIMIT_REMAINING_PERCENT = 25.0


class MemoryWriteError(RuntimeError):
    """The write path refused to do something."""


# ---------------------------------------------------------------------------
# Redaction: on the way in, and again on the way out
# ---------------------------------------------------------------------------

# Shapes that are secret regardless of what they are called.  Chapter -1's
# recorder redacts by *key name* (`api_key`, `authorization`), which is right
# for a JSON request body and useless here: a transcript is prose, and the
# thing that reaches this module is `OPENAI_API_KEY=sk-proj-…` sitting in the
# output of `env`, or a token a user pasted into a question.
_SECRET_SHAPES = (
    re.compile(r"\bsk-[A-Za-z0-9_\-]{16,}"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{16,}"),
    re.compile(r"\bAKIA[0-9A-Z]{12,}"),
    re.compile(r"\bxox[baprs]-[A-Za-z0-9\-]{10,}"),
    re.compile(r"\beyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.S),
)

# `NAME=value` where the name says it is a credential.  The value is taken up
# to whitespace or a quote, which is what an `env` dump and a `.env` file both
# look like.
_SECRET_ASSIGNMENT = re.compile(
    r"(?i)\b([A-Z0-9_]*(?:KEY|TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIAL)[A-Z0-9_]*)"
    r"\s*[=:]\s*[\"']?([^\s\"'\n]{6,})"
)

REDACTED = "<redacted>"


def scrub(text: str) -> tuple[str, int]:
    """Remove anything credential-shaped.  Returns the text and how many hits.

    Applied **twice**, on purpose, and that is the whole of F17-05: once to the
    transcript before it is sent to the extractor, and once to the extractor's
    answer before it is written to a file that will be injected into every
    future request.  Doing only the first is the version that feels sufficient
    -- if the model never saw the key it cannot repeat it -- and it is wrong in
    two directions: the merge step also reads hand-written notes and a
    user-edited memory file, and a model asked to summarise a session can
    reconstruct a partially-masked value from context.

    A count is returned rather than a boolean because it goes in the report:
    "3 secret(s) removed" is a line that makes somebody look, and a silent
    redaction is a redaction nobody audits.
    """
    hits = 0
    for pattern in _SECRET_SHAPES:
        text, n = pattern.subn(REDACTED, text)
        hits += n

    def _assignment(match: re.Match[str]) -> str:
        nonlocal hits
        if match.group(2) == REDACTED:
            # Already handled by a shape above.  Without this the count is of
            # *substitutions* rather than of secrets, and the probe reported
            # "2 removed" for one key -- a number about the regex list, not
            # about the transcript.
            return match.group(0)
        hits += 1
        return f"{match.group(1)}={REDACTED}"

    text = _SECRET_ASSIGNMENT.sub(_assignment, text)
    return text, hits


# ---------------------------------------------------------------------------
# Stage 1: one session in, at most a few durable statements out
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Stage1:
    """What one session was worth remembering.

    Three fields rather than a free-text blob, because the sections are what
    the prompt is enforcing and a parsed shape is what makes "it produced
    nothing" a fact this program can act on rather than a judgement it has to
    make about prose.

    The section names are chapter 6's structured-summary result (F06-04: six
    mandatory headings kept 23/25 planted facts against 15/25 for "summarise
    this") one subsystem later.  What changed is the *selection* rule: a
    compaction summary must keep everything still in play, and this must keep
    almost nothing.
    """

    session_id: str = ""
    preferences: tuple[str, ...] = ()
    knowledge: tuple[str, ...] = ()
    failures: tuple[str, ...] = ()
    secrets_removed: int = 0

    def __bool__(self) -> bool:
        return bool(self.preferences or self.knowledge or self.failures)

    def bullets(self) -> tuple[str, ...]:
        return self.preferences + self.knowledge + self.failures

    def describe(self) -> str:
        if not self:
            return f"{self.session_id}: nothing worth keeping"
        return (
            f"{self.session_id}: {len(self.preferences)} preference(s), "
            f"{len(self.knowledge)} fact(s), {len(self.failures)} failure(s)"
        )


_SECTIONS = (
    ("preferences", "Preference signals"),
    ("knowledge", "Reusable knowledge"),
    ("failures", "Failures and how to do differently"),
)

# The three wordings this chapter measured, weakest first.  All three are here
# rather than only the winner, for chapter 16's reason: a claim that one
# wording is better than another, with the other one deleted, is a claim
# nobody can re-run.
#
# **STAGE1_NAIVE** -- one sentence, the version anybody writes first.
STAGE1_NAIVE = (
    "Summarise what was learned in this session that would be useful next time. "
    "Use the same three headings:\n\n"
    f"## {_SECTIONS[0][1]}\n## {_SECTIONS[1][1]}\n## {_SECTIONS[2][1]}\n"
)

_KINDS = """\
Write only high-signal material, which means exactly three kinds of thing:

1. A **stable preference** the user stated or clearly implied about how work is
   done here -- a command they insist on, a style rule, a thing they do not want
   touched. Something they would have to say again in the next session if it is
   not written down.
2. **Reusable knowledge** about this repository that was expensive to find and
   will be needed again -- where something lives, how a thing is built or run,
   what a non-obvious file is for.
3. A **failure shield**: something that went wrong, and the one sentence that
   would stop it happening again.

Do not write:

- What happened. "Ran the tests", "read three files", "added a function" are
  facts about one afternoon, not about this repository.
- Anything true only of the task in this session.
- Anything already obvious from opening the repository.
- Credentials, tokens, keys, or anything that looks like one, even partially.
- Praise, narration, or a description of what you are about to do.
"""

_SHAPE = f"""\
## {_SECTIONS[0][1]}
- ...

## {_SECTIONS[1][1]}
- ...

## {_SECTIONS[2][1]}
- ...
"""

# **STAGE1_QUOTA** -- the careful version, and the one this chapter shipped
# first.  It says everything correct: what to keep, what not to keep, and that
# "producing nothing is a normal and preferred outcome" in bold, which is the
# sentence codex's own stage-one prompt carries.
#
# It produced three bullets for **every session it was shown, including the one
# that taught nothing**: 0/2 no-ops on a session whose whole content is "which
# functions does calc.py define". The bullets it invented for it were
# "The user prefers concise summaries of function definitions" and "The calc.py
# file defines two functions". Both true. Both worthless. Both would have been
# injected into every future request in this repository.
#
# The cause is the last paragraph, and it is not the one that says nothing is
# preferred: **"write at most three bullets per section" is a quota, and a
# format with slots in it is a format a model fills.**
#
# Chapter 16 met the same mechanism from the other side, and what it found
# there is now a sharper version of this result than the one this comment used
# to cite.  Its first draft claimed that asking for a citation block
# unconditionally took compliance from 0/9 to 18/18 -- and that measurement did
# not survive its rewrite.  Asking unconditionally does still produce a block
# almost every time; what the stricter format showed is that **0 of 9 of those
# blocks contained a line that resolved to anything real.**  The model filled
# the slot with invented line numbers.
#
# So the two chapters agree, and they agree on something worse than "a required
# shape is filled": a required shape is filled *with plausible material*
# whether or not there is anything to put in it.  A quota produces a bullet
# about `calc.py` defining two functions; a citation format produces
# `MEMORY.md:12-14` for lines nobody read.  Neither failure announces itself,
# and in both cases the fix is the same shape -- make the model commit to a
# verdict before it is shown the slot (`DURABLE: yes/no` below), rather than
# hoping it declines to fill one.
STAGE1_QUOTA = f"""\
You are reading the transcript of one finished coding session. Your job is to
decide what, if anything, about it is worth remembering in three months, and to
write that down. You are not summarising the session.

{_KINDS}
**Producing nothing is a normal and preferred outcome.** Most sessions teach
nothing durable. If this one did not, return the three headings with no bullets
under them. An empty answer costs nothing; a wrong one is injected into every
future session in this repository.

Each bullet must be one sentence, standing on its own, understandable by
someone who has not read this transcript. Write at most three bullets per
section, and fewer is better.

Answer with exactly this shape and nothing else:

{_SHAPE}"""

# **STAGE1_SYSTEM** -- what ships.  Two changes from the version above, and
# both are structural rather than emphatic.
#
# The first is the verdict line. The model must decide, in one word, before it
# is shown the shape it could fill -- and the decision is *enforced* by
# `parse_stage1` rather than trusted, which is this book's standing rule about
# anything that can be settled in code.
#
# The second is the test. "Would a competent engineer opening this repository
# be unable to work this out in thirty seconds" is a question with an answer,
# where "is this durable?" is a question with a vibe.
STAGE1_SYSTEM = f"""\
You are reading the transcript of one finished coding session. Your job is to
decide what, if anything, about it is worth remembering in three months, and to
write that down. You are not summarising the session.

First decide whether this session taught anything durable at all. Most did not.
Apply this test to each candidate line, and write it down only if it passes:

  Would a competent engineer, opening this repository for the first time, be
  unable to work this out for themselves in thirty seconds?

If they could work it out -- what a file defines, what a function does, that
tests should be written -- it is not memory. It is a sentence that will be sent
with every future request in this repository, forever, and will teach nobody
anything.

{_KINDS}
Begin your answer with one line, exactly `DURABLE: yes` or `DURABLE: no`.

Answer `DURABLE: no` and leave every section empty unless there is something
that passes the test above. That is the normal outcome, and an empty answer
costs nothing: a wrong line costs every future session.

If and only if you answered yes, fill in the sections below. One sentence per
bullet, standing on its own, understandable by someone who has not read this
transcript. There is no minimum; one bullet is a good answer.

Answer with exactly this shape and nothing else:

DURABLE: yes
{_SHAPE}"""

# The verdict line, and the one place it is spelled.  Checked case-insensitively
# and anywhere in the first few lines, because a model that decides to be
# helpful writes `**DURABLE: no**`.
_VERDICT = re.compile(r"^\W*durable\W*:\W*(yes|no)\b", re.IGNORECASE | re.MULTILINE)

_BULLET = re.compile(r"^\s*[-*]\s+(.*\S)\s*$")
_H2 = re.compile(r"^##\s+(.+?)\s*$")


def parse_stage1(text: str, *, session_id: str = "") -> Stage1:
    """Read the extractor's answer, keeping only what is under a known heading.

    Tolerant in one direction and strict in the other: unknown headings and
    prose between sections are dropped, and nothing outside a section is ever
    promoted into one.  A model that ignores the format produces an empty
    `Stage1`, which is the same outcome as a model that had nothing to say --
    and that is the right collapse, because both mean "there is nothing here to
    merge".

    `DURABLE: no` empties the answer, whatever else is in it.  The model is
    asked to make one judgement before it is shown a shape it could fill, and
    this line is what makes that judgement binding rather than advisory: it
    said no, so the bullets under the headings are the shape talking.  Measured
    disagreement -- said no and wrote bullets anyway -- is in the chapter.
    """
    verdict = _VERDICT.search(text)
    if verdict is not None and verdict.group(1).lower() == "no":
        return Stage1(session_id=session_id)
    wanted = {title.lower(): key for key, title in _SECTIONS}
    found: dict[str, list[str]] = {key: [] for key, _ in _SECTIONS}
    current: str | None = None
    for line in text.splitlines():
        heading = _H2.match(line)
        if heading:
            current = wanted.get(heading.group(1).strip().lower())
            continue
        if current is None:
            continue
        bullet = _BULLET.match(line)
        if bullet:
            body = bullet.group(1).strip()
            # A model told to leave a section empty sometimes writes "- none"
            # rather than nothing at all.  Treating that as an entry is how a
            # memory ends up with a section that says "none" in it forever.
            #
            # `...` is on this list because it happened: the shape at the
            # bottom of the prompt is `- ...`, and one sample in three copied
            # the placeholder through into its answer.  A memory entry whose
            # entire text is an ellipsis is the kind of thing that survives
            # for a year because nobody can tell what it was meant to say.
            if body.lower().strip(" .") in {"none", "n/a", "nothing", "(none)", ""}:
                continue
            found[current].append(body)
    return Stage1(
        session_id=session_id,
        preferences=tuple(found["preferences"]),
        knowledge=tuple(found["knowledge"]),
        failures=tuple(found["failures"]),
    )


def render_stage1(stage1: Stage1) -> str:
    """The `raw/<session>.md` file, which says what it is in its first lines.

    The warning is not decoration.  codex's equivalent carries the same one --
    "Temporary file: … Input for Phase 2" -- and the fault it prevents is a
    slow one: a file in a memory directory that looks like memory, and that
    somebody eventually edits, or greps, or copies into a bug report.
    """
    lines = [
        FORMAT_VERSION,
        "",
        f"<!-- Temporary file: stage-1 output for session {stage1.session_id or '?'}.",
        "     Input for stage 2. Deleted once it has been merged.",
        "     Nothing reads this as memory; edit MEMORY.md instead. -->",
        "",
    ]
    for key, title in _SECTIONS:
        lines.append(f"## {title}")
        for item in getattr(stage1, key):
            lines.append(f"- {item}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def transcript_of(rollout: Rollout, *, budget: int = TRANSCRIPT_TOKEN_BUDGET) -> tuple[str, int]:
    """One session as text, redacted and clipped.  Returns text and secret count.

    System and developer notes are **left out**, and that is a decision rather
    than an omission.  They are this program's own words -- the permissions
    block, chapter 0's "you have 2 turns left", chapter 13's `AGENTS.md`
    injection, chapter 16's memory block itself -- and an extractor shown them
    dutifully reports the program's instructions back as things worth
    remembering.  The most direct form of that is the last one in the list: a
    memory that was injected into the session is a memory the session then
    re-learns, which is a loop with no fixed point and no error message.

    One thing is lost with them and it is worth saying out loud, because it
    looks like an oversight: chapter 13's `AGENTS.md` arrives as a developer
    note, and it is the most convention-shaped material in a session.  It is
    dropped **because it is already in the repository**, in a file a person
    maintains and a team can read.  Extracting it would store the same sentence
    in two places, and the two places drift -- which is the whole argument for
    the three-layer table in chapter 16 §2.1.
    """
    parts: list[str] = []
    for item in rollout.items:
        if isinstance(item, UserMessage):
            parts.append(f"user: {item.text}")
        elif isinstance(item, AssistantMessage):
            if item.text.strip():
                parts.append(f"assistant: {item.text.strip()}")
            for call in item.tool_calls:
                if call.name == NOTE_NAME:
                    continue
                parts.append(f"assistant calls {call.name} {call.raw_arguments}")
        elif isinstance(item, ToolResult):
            if item.name == NOTE_NAME:
                # The writer does not read its own output.  A `remember_this`
                # call is *already* an input to stage 2 by another route
                # (`notes/`), so leaving it in the transcript delivers the same
                # sentence twice -- measured: 3/3 extractions of a session that
                # proposed a note re-derived the note as a bullet.
                #
                # This is F17-10's shape, in the one place this program can
                # reach it: the memory system reading a transcript of the
                # memory system.  The listed version of that fault needs the
                # writer to be an agent with a session of its own, which this
                # one is not (see the chapter).
                continue
            parts.append(f"{item.name} returned: {clip(item.content, TOOL_RESULT_CHARS)}")
    text = "\n".join(parts)
    text, secrets = scrub(text)
    # Head and tail, chapter 2's shape: the user's actual request is at the top
    # of a session and the thing that finally worked is at the bottom, and a
    # tail-only clip loses the first of those every time.
    if estimate_messages([{"role": "user", "content": text}]) > budget:
        text = clip(text, budget * 4)
    return text, secrets


async def extract(
    model: Any,
    rollout: Rollout,
    *,
    system: str = STAGE1_SYSTEM,
) -> Stage1:
    """Stage 1 for one session.  One model call, no tools, no loop.

    Not an `Agent`.  There is nothing to iterate: the input is a transcript
    that already exists and the output is text.  A tool loop here would add
    every failure mode chapters 0 to 12 exist to handle, in exchange for
    nothing -- and it would also give a model somewhere to write, which is the
    one thing F17-09 says it must not have.
    """
    text, secrets = transcript_of(rollout)
    answer = await _ask(
        model,
        [
            {"role": "system", "content": system},
            {"role": "user", "content": f"<transcript>\n{text}\n</transcript>"},
        ],
    )
    # The second scrub.  The first one was on the way in; this one is on the
    # way to a file that gets injected into every future request.
    answer, more = scrub(answer)
    stage1 = parse_stage1(answer, session_id=rollout.meta.session_id)
    return Stage1(
        session_id=stage1.session_id,
        preferences=stage1.preferences,
        knowledge=stage1.knowledge,
        failures=stage1.failures,
        secrets_removed=secrets + more,
    )


async def _ask(model: Any, messages: list[dict[str, Any]]) -> str:
    """Collect one non-streaming answer out of a streaming client.

    The same shape `make_summariser` uses in chapter 6, including the deferred
    import, and it is duplicated rather than shared: that one builds a
    `History` and renders it, this one has two literal messages and wants no
    history at all.  Two six-line functions that look alike is not the same
    thing as two call sites of one function.

    The `[DONE]` check is chapter 0's rule, third application.  A stream that
    ended early is not a shorter answer -- here it is half a memory, and half a
    memory is written to disk and read back forever.
    """
    from minicodex.model import Completed, TextDelta

    out: list[str] = []
    finished = False
    async for event in model.stream(messages):
        if isinstance(event, TextDelta):
            out.append(event.text)
        elif isinstance(event, Completed):
            finished = True
    if not finished:
        raise MemoryWriteError("the model stream ended without a [DONE] sentinel")
    return "".join(out)


# ---------------------------------------------------------------------------
# Notes: the only thing a model may add, and it may only add
# ---------------------------------------------------------------------------

NOTE_NAME = "remember_this"

NOTE_SCHEMA: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": NOTE_NAME,
        "description": (
            "Propose one durable fact about this repository for the memory used by "
            "future sessions. Use it when the user states a convention, or when you "
            "find something expensive to discover that will be needed again. Do not "
            "use it for what you did in this session, for anything true only of this "
            "task, or for anything you have not confirmed. The note is a proposal: "
            "it is reviewed and merged later, not written into memory now."
        ),
        "parameters": {
            "type": "object",
            "required": ["note"],
            "properties": {
                "note": {
                    "type": "string",
                    "description": (
                        "One sentence, standing on its own, understandable by someone "
                        "who has not read this conversation. Example: this project is "
                        "run with `uv run`, never with a bare `python`."
                    ),
                }
            },
        },
    },
}

# A note is a sentence.  Anything longer is a session transcript arriving by
# another door, and the merge step reads all of these on every run.
MAX_NOTE_CHARS = 400

# What the system prompt says when the tool is present, and only then --
# chapter 5's F05-10, which measured a model calling a tool the prompt named
# and the policy did not have, 2/3.
#
# The last sentence is the one worth arguing about.  Everything the session
# does is extracted afterwards anyway, so this tool is not the only route into
# memory; it is the route for the things a transcript does not show, which is
# mostly the user saying "always do X".  Saying that plainly is cheaper than
# hoping the model infers the division of labour.
REMEMBER_INSTRUCTIONS = (
    f"You can propose something for this repository's long-term memory with "
    f"`{NOTE_NAME}`. Use it when the user tells you how things are done here, or "
    "when you find something that cost you several steps to discover and will be "
    "needed again. One sentence, written for a session that cannot see this "
    "conversation. Do not use it to record what you did. Everything else in this "
    "session is reviewed for memory automatically afterwards, so a note is only "
    "worth writing for something a reader of the transcript would miss."
)


def remember_instructions() -> str:
    return REMEMBER_INSTRUCTIONS


def note_toolset(directory: Path, *, limit: int = 5) -> ToolSet:
    """`remember_this`: append a proposal, and nothing else.

    This is the whole of F17-09.  The obvious design -- let the model edit
    `MEMORY.md`, it is only markdown -- gives a model that has been told to add
    one line the ability to rewrite the file it is adding it to, and the
    failure is not that it refuses: it is that it re-emits the parts it
    remembers and drops the rest, silently, in a file nobody diffs.  Chapter
    4's F04-02 with no reviewer.

    So the model gets an append-only outbox.  One file per note; the name
    carries time and pid so two processes never choose the same one; nothing is
    ever opened for writing twice.  The merge step reads the directory and is
    the only thing that touches memory itself.

    `limit` is per tool set, which is per run: a model that finds the tool
    rewarding can otherwise turn one session into fifty proposals, and the
    merge step pays for every one of them.
    """
    written = 0

    async def do_note(args: dict[str, Any]) -> str:
        nonlocal written
        note = args.get("note")
        if not isinstance(note, str) or not note.strip():
            return tool_error(
                f'{NOTE_NAME} needs a "note" argument, a non-empty string',
                you_sent=repr(args.get("note")),
                do_this='Example: {"note": "Tests are run with `python -m pytest`."}',
            )
        if written >= limit:
            return tool_error(
                f"{limit} note(s) is the limit for one session",
                do_this="Continue with the task; what you have proposed is enough.",
            )
        body, _ = scrub(note.strip()[:MAX_NOTE_CHARS])
        written += 1
        path = propose(Path(directory), body)
        return (
            f"Proposed. It is not memory yet: it goes to {path.parent.name}/ and is "
            "reviewed when memory is next merged."
        )

    return ToolSet(handlers={NOTE_NAME: do_note}, schemas=[NOTE_SCHEMA])


def propose(directory: Path, note: str, *, now: float | None = None) -> Path:
    """Write one proposal file.  Never overwrites, never edits, never merges."""
    stamp = now if now is not None else time.time()
    notes = Path(directory) / NOTES_DIR
    notes.mkdir(parents=True, exist_ok=True)
    for suffix in range(100):
        name = f"{int(stamp)}-{os.getpid()}-{suffix}.md"
        path = notes / name
        if not path.exists():
            path.write_text(note.strip() + "\n", encoding="utf-8")
            return path
    raise MemoryWriteError(f"cannot find an unused note name in {notes}")  # pragma: no cover


def pending_notes(directory: Path) -> tuple[tuple[Path, str], ...]:
    notes = Path(directory) / NOTES_DIR
    if not notes.is_dir():
        return ()
    out = []
    for path in sorted(notes.glob("*.md")):
        try:
            out.append((path, path.read_text(encoding="utf-8").strip()))
        except OSError:  # pragma: no cover - unreadable proposal is not fatal
            continue
    return tuple(out)


def pending_raw(directory: Path) -> tuple[tuple[Path, str], ...]:
    raw = Path(directory) / RAW_DIR
    if not raw.is_dir():
        return ()
    out = []
    for path in sorted(raw.glob("*.md")):
        try:
            out.append((path, path.read_text(encoding="utf-8")))
        except OSError:  # pragma: no cover
            continue
    return tuple(out)


def write_raw(directory: Path, stage1: Stage1) -> Path:
    raw = Path(directory) / RAW_DIR
    raw.mkdir(parents=True, exist_ok=True)
    path = raw / f"{stage1.session_id or 'unknown'}.md"
    path.write_text(render_stage1(stage1), encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# Stage 2: merge everything pending into the two files chapter 16 reads
# ---------------------------------------------------------------------------

SUMMARY_MARK = "=== memory_summary.md ==="
BODY_MARK = "=== MEMORY.md ==="

STAGE2_SYSTEM = f"""\
You maintain one repository's memory: a short resident summary that is sent
with every request, and a longer body of sections that is consulted when
needed. You are given what is there now, new material extracted from recent
sessions, notes proposed during those sessions, and any edits a human made to
the files by hand.

Produce the new contents of both files.

Rules:

- **Human edits win and are preserved.** If the diff of hand edits shows a line
  a person wrote, changed or removed, honour it. Do not restore something a
  person deleted. Do not reword something a person wrote.
- Merge duplicates. If new material says the same thing as an existing entry,
  keep one, worded as precisely as the better of the two.
- If new material contradicts an existing entry, the new material is more
  recent and wins -- but say so in one clause, so the change is visible.
- Never invent. Every line must come from something you were given.
- No credentials, tokens or keys, ever, in either file.
- The summary holds only what is worth sending on every single request: at most
  eight short bullets. Everything else belongs in a section of the body.
- The body is `## Heading` sections, each a few lines, each about one thing.
  **Name each section after its subject** -- `## Running tests`,
  `## Where new code goes` -- because the heading is what a future session
  searches for and cites.
- The headings in the material you were given (`Preference signals`,
  `Reusable knowledge`, `Failures and how to do differently`) are the
  extractor's filing categories. They are not subjects and must not appear in
  what you write.
- Say each thing once. If a preference and a failure are the same rule from two
  angles, they are one section.
- Keep it small. Drop any line a competent engineer opening this repository
  could work out for themselves in thirty seconds -- what a file contains, what
  a function does, that code should have tests.

Answer with exactly this shape and nothing else -- no code fences, no
commentary:

{SUMMARY_MARK}
- ...
{BODY_MARK}
## ...

...
"""


@dataclass(frozen=True)
class Merged:
    """The proposed new contents of the two files, before anything is written."""

    summary: str
    body: str
    secrets_removed: int = 0

    def entries(self) -> tuple[Entry, ...]:
        return parse_entries(self.body)


def parse_merged(text: str) -> Merged:
    """Split the merge answer into the two files.

    Refuses rather than guesses.  A merge answer that does not carry both
    markers is one this program cannot place, and the tempting fallback --
    treat the whole thing as the body -- writes the model's commentary into a
    file that is injected into every future request.
    """
    if SUMMARY_MARK not in text or BODY_MARK not in text:
        raise MemoryWriteError(
            f"merge answer is missing its markers ({SUMMARY_MARK} / {BODY_MARK}); "
            f"nothing was written. First 200 characters: {text[:200]!r}"
        )
    _, rest = text.split(SUMMARY_MARK, 1)
    summary, body = rest.split(BODY_MARK, 1)
    summary, hits_a = scrub(summary.strip())
    body, hits_b = scrub(body.strip())
    return Merged(summary=summary, body=body, secrets_removed=hits_a + hits_b)


def merge_request(
    *,
    memory: Memory,
    raw: Sequence[tuple[Path, str]],
    notes: Sequence[tuple[Path, str]],
    hand_edits: str,
) -> str:
    """Everything stage 2 is shown, as one message.

    The hand-edit diff is last and labelled, because it is the input with the
    highest authority and the one most easily read as more of the same.
    """
    parts = [
        "# Memory as it stands\n",
        f"## {SUMMARY_FILE}\n{memory.summary or '(empty)'}",
        f"## {BODY_FILE}\n" + ("\n\n".join(e.render() for e in memory.entries) or "(empty)"),
        "\n# New material extracted from recent sessions\n",
    ]
    parts.extend(text for _, text in raw)
    if notes:
        parts.append("\n# Notes proposed during those sessions\n")
        parts.extend(f"- {text}" for _, text in notes)
    if hand_edits.strip():
        parts.append(
            "\n# Edits a human made by hand since the last merge (git diff)\n"
            "These are the user's own words. Preserve them.\n\n" + hand_edits
        )
    return "\n\n".join(parts)


async def consolidate(
    model: Any,
    *,
    memory: Memory,
    raw: Sequence[tuple[Path, str]],
    notes: Sequence[tuple[Path, str]] = (),
    hand_edits: str = "",
    system: str = STAGE2_SYSTEM,
) -> Merged:
    """Stage 2: one model call, and the answer is a proposal, not a write."""
    answer = await _ask(
        model,
        [
            {"role": "system", "content": system},
            {
                "role": "user",
                "content": merge_request(
                    memory=memory, raw=raw, notes=notes, hand_edits=hand_edits
                ),
            },
        ],
    )
    return parse_merged(answer)


# ---------------------------------------------------------------------------
# Forgetting
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Prune:
    kept: tuple[Entry, ...]
    dropped: tuple[tuple[Entry, str], ...]

    def describe(self) -> str:
        if not self.dropped:
            return f"{len(self.kept)} entr(ies), nothing forgotten"
        reasons = ", ".join(f"{e.title} ({why})" for e, why in self.dropped)
        return f"{len(self.kept)} entr(ies), forgot {len(self.dropped)}: {reasons}"


def prune(
    entries: Sequence[Entry],
    counts: dict[str, dict[str, Any]],
    *,
    now: float,
    max_entries: int = MAX_ENTRIES,
    unused_days: float = UNUSED_DAYS,
) -> Prune:
    """Decide what leaves.  Citations first, recency second, age last.

    The listed fix for F17-06 is "a window of unused days, a total cap, and a
    periodic prune", and taken literally that is F17-07: the entry that is nine
    months old and consulted every week is exactly the one an age rule deletes
    first.  So age is only ever a tie-breaker here, and the primary key is
    chapter 16's citation counter.

    **That primary key is a signal chapter 16 has now measured as unreliable,
    and this is the fault it lands on (F17-13).**  Two separate things went
    wrong with it, and only the first was known when this function was written:

    * The *bias*, measured in chapter 16's first draft and inherited whole: a
      model reports the memory that shaped its **answer** and not the memory
      that shaped its **actions**.  On the `runner` task it ran `python -m
      pytest` -- a string only memory knew -- and then cited nothing, 3/3.
    * The *resolution rate*, measured after chapter 16 adopted codex's real
      citation format: asked for `path:line_start-line_end`, the model supplies
      plausible line numbers it did not look up, and **0 of 9 citations
      resolved to a real entry**.  A citation that does not resolve never
      reaches `record_uses`, so it never becomes a count here.

    The tempting repair is to rank on chapter 16's other signal instead --
    `usage_kind_for_call`, which sees the model actually open `MEMORY.md` and
    cannot be lied to.  **codex does not do that, and neither does this.**  It
    keeps the two strictly apart: the behavioural signal feeds a telemetry
    counter and stops there (`core/src/memory_usage.rs:9-27`), while retention
    ranking and retention pruning read only the citation-fed columns
    (`core/src/stream_events_utils.rs:184` ->
    `state/src/runtime/memories.rs:55-73`, consumed at
    `state/src/runtime/memories.rs:389-413`).  The reason is not ceremony: a
    counter that says "the model opened MEMORY.md" cannot say *which section
    mattered*, and every decision made here is per-entry.  Substituting a
    file-granularity signal for an entry-granularity one would keep every entry
    alive whenever any entry was read, which is not a forgetting policy.

    So the signal stays, and what changes is how much weight it is allowed to
    carry.  **Zero citations alone never drops anything** -- it takes zero
    citations *and* a month of not being consulted *and* the cap being over.
    That was written as belt-and-braces when the bias was the only known
    problem; with the resolution rate at 0/9 it is the only thing standing
    between an unreliable signal and deleting a good entry, and it is now
    load-bearing.  `test_F17_13_*` pins it.

    An entry with no record at all is treated as new rather than as ancient.
    The other way round deletes every entry the first time this runs on a
    memory written before the counter existed -- and, since the merge step
    rewrites headings and a reworded heading is a new `entry_id`, it would also
    delete every entry the merge improved.
    """
    scored: list[tuple[float, float, float, int, Entry]] = []
    for index, entry in enumerate(entries):
        row = counts.get(entry.entry_id, {})
        cited = float(row.get("count", 0) or 0)
        last_used = float(row.get("last_used", 0) or 0)
        first_seen = float(row.get("first_seen", 0) or 0) or now
        scored.append((-cited, -last_used, -first_seen, index, entry))
    scored.sort()

    kept: list[Entry] = []
    dropped: list[tuple[Entry, str]] = []
    for cited, _, _, _, entry in scored:
        row = counts.get(entry.entry_id, {})
        first_seen = float(row.get("first_seen", 0) or 0) or now
        age_days = max(0.0, (now - first_seen) / 86400.0)
        if len(kept) >= max_entries:
            dropped.append((entry, f"over the {max_entries}-entry cap"))
        elif cited == 0 and age_days > unused_days:
            dropped.append((entry, f"never cited in {age_days:.0f} days"))
        else:
            kept.append(entry)
    # Sorted back into file order: the ranking decides *what* survives, and
    # letting it decide the order as well would reshuffle the whole file on
    # every merge, which makes the git diff -- the mechanism F17-08 depends on
    # -- useless.
    order = {entry.entry_id: i for i, entry in enumerate(entries)}
    kept.sort(key=lambda e: order[e.entry_id])
    return Prune(kept=tuple(kept), dropped=tuple(dropped))


def trim_summary(
    summary: str,
    entries: Sequence[Entry] = (),
    *,
    budget: int = SUMMARY_TOKEN_BUDGET,
) -> str:
    """Keep the resident half inside chapter 16's cap, deterministically.

    The prompt asks for at most eight bullets and the prompt is a request.  A
    resident block is sent with every request forever, so the bound is here as
    well, in code, and it cuts at a line.

    `entries` is the part that had to be added after a mutation survived, and
    the reason is chapter 6's sentence about `Sizer`: **the fault is not that
    one ruler was wrong, it is that there were two.**  The first version
    trimmed the summary to the budget on its own; the read path then budgets
    the same number over the summary *and* the heading index *and* the fences,
    so a summary trimmed to exactly the cap came back truncated by the reader.
    Nothing failed -- the block simply arrived with `[... memory truncated ...]`
    in it.  So the question asked here is the read path's own: trim until
    `overflows()` says no.

    What that marker *means* changed in chapter 16's rewrite and this function
    did not have to.  It used to decide whether the dedicated search tools got
    mounted; that gate was this book's invention and is gone (F16-12,
    `composition.top_level_tools`).  `overflows()` survives as what it always
    literally was -- "did the reader have to cut anything" -- which is the only
    thing this function ever asked it.  A budget that rose from 400 to codex's
    real 2500 (`ext/memories/src/lib.rs:16`) also makes it fire far less often,
    and it is kept for the reason the cap itself is kept: the summary is sent
    with every request forever, and the prompt asking for eight bullets is a
    request.
    """
    lines = summary.splitlines()
    while lines and overflows(
        Memory(directory=Path("."), summary="\n".join(lines), entries=tuple(entries)),
        budget=budget,
    ):
        lines.pop()
    return "\n".join(lines).rstrip()


# ---------------------------------------------------------------------------
# The single writer
# ---------------------------------------------------------------------------


def _lock(path: Path = MERGE_LOCK) -> int:
    """chapter 7's `O_EXCL` lock, copied deliberately.  See `MERGE_LOCK`."""
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        holder = ""
        try:
            holder = path.read_text(encoding="utf-8", errors="replace").strip()
        except OSError:  # pragma: no cover
            pass
        raise MemoryWriteError(
            f"memory is being merged by another minicodex (pid {holder or '?'}). "
            f"Nothing was written. Delete {path} if that process is gone."
        ) from None
    os.write(fd, str(os.getpid()).encode())
    return fd


def _unlock(fd: int, path: Path = MERGE_LOCK) -> None:
    os.close(fd)
    try:
        path.unlink()
    except OSError:  # pragma: no cover
        pass


def write_memory(
    directory: Path,
    *,
    summary: str,
    body: str,
    now: float | None = None,
    lock_path: Path = MERGE_LOCK,
) -> Prune:
    """The one function in this program that writes `MEMORY.md`.

    Everything else -- the extractor, the merger, the note tool -- produces a
    proposal.  This validates it, prunes it, writes both files under a lock,
    updates the usage records and commits.  The ordering matters in one place:
    **validation happens before anything is opened for writing**, which is
    chapter 4's two-phase apply (F04-11) at a different scale.  A merge answer
    that parses into no entries at all is refused rather than written, for
    chapter 6's reason about an empty summary (F06-08): that is not a smaller
    memory, it is a destroyed one.
    """
    stamp = now if now is not None else time.time()
    directory = Path(directory)
    entries = parse_entries(body)
    if not entries and not summary.strip():
        raise MemoryWriteError(
            "the merge produced neither a summary nor a section; refusing to "
            "replace the existing memory with nothing"
        )

    counts = _usage(directory)
    kept = prune(entries, counts, now=stamp)
    summary = trim_summary(summary.strip(), kept.kept)

    fd = _lock(lock_path)
    try:
        directory.mkdir(parents=True, exist_ok=True)
        (directory / SUMMARY_FILE).write_text(f"{FORMAT_VERSION}\n\n{summary}\n", encoding="utf-8")
        body_text = "\n\n".join(entry.render() for entry in kept.kept)
        (directory / BODY_FILE).write_text(
            f"{FORMAT_VERSION}\n\n{body_text}\n" if body_text else f"{FORMAT_VERSION}\n",
            encoding="utf-8",
        )
        _record_first_seen(directory, kept, counts, now=stamp)
    finally:
        _unlock(fd, lock_path)
    return kept


def _usage(directory: Path) -> dict[str, dict[str, Any]]:
    path = Path(directory) / USAGE_FILE
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return raw if isinstance(raw, dict) else {}


def _record_first_seen(
    directory: Path, pruned: Prune, counts: dict[str, dict[str, Any]], *, now: float
) -> None:
    """Stamp new entries, forget the rows of dropped ones.

    `usage.json` is chapter 16's file and this is the write path adding a
    field to it, which is worth naming rather than doing quietly: the read side
    increments `count`, the write side sets `first_seen` and deletes rows.  Two
    owners of one file is a thing to be nervous about, and it survives here for
    one reason -- there is no third operation, and splitting it would give the
    forgetting policy two files to read that must agree about which entries
    exist.
    """
    # Rebuilt from the entries that survive, rather than the pruned ones
    # deleted from the old table: an entry can also leave because the *merge*
    # stopped emitting it, and that path leaves no row in `pruned.dropped`.
    # The first version subtracted instead of rebuilding, and `usage.json`
    # accumulated counts for headings that no longer existed -- which the
    # forgetting policy then read.
    updated: dict[str, dict[str, Any]] = {}
    for entry in pruned.kept:
        row = dict(counts.get(entry.entry_id) or {})
        row.setdefault("count", 0)
        row.setdefault("first_seen", now)
        updated[entry.entry_id] = row
    try:
        (Path(directory) / USAGE_FILE).write_text(
            json.dumps(updated, indent=2, sort_keys=True), encoding="utf-8"
        )
    except OSError:  # pragma: no cover
        return


# ---------------------------------------------------------------------------
# git: the merge conflict problem, handed to something that has solved it
# ---------------------------------------------------------------------------


def _git(directory: Path, *args: str, check: bool = False) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(directory), *args],
        capture_output=True,
        text=True,
        check=check,
        timeout=30,
    )


def git_available() -> bool:
    try:
        return _git(Path("."), "--version").returncode == 0
    except (OSError, subprocess.SubprocessError):  # pragma: no cover - git missing
        return False


def ensure_repo(directory: Path) -> bool:
    """Make the memory directory a git repository, if it is not one already.

    codex does the same thing to `~/.codex/memories/`, initialising a git
    baseline under the memory root and diffing the worktree against it to
    build the input its consolidation agent reads.  The reason is F17-08
    rather than version control for its own sake: **the merge step needs to
    know what a human changed**, and "what changed since the last time this
    program wrote the file" is exactly what `git diff` answers.  Writing that
    from scratch means storing a shadow copy and diffing it, which is storing a
    shadow copy and diffing it.

    Returns False when git is not available, and everything downstream then
    degrades to "no hand edits detected" -- which is the safe direction: the
    merge prompt's instruction to preserve what a person wrote still applies to
    the memory it is shown, it simply has no diff to point at.
    """
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    if (directory / ".git").exists():
        return True
    if not git_available():
        return False
    if _git(directory, "init", "--quiet").returncode != 0:  # pragma: no cover
        return False
    # A repository nobody configured cannot commit: `user.email` and
    # `user.name` are not set in CI images, and the failure appears at the
    # first commit rather than here.  Set locally, never globally.
    _git(directory, "config", "user.email", "minicodex@localhost")
    _git(directory, "config", "user.name", "minicodex")
    _git(directory, "config", "commit.gpgsign", "false")
    return True


def hand_edits(directory: Path) -> str:
    """What changed in the memory files since this program last committed.

    Everything in the working tree that is not committed is, by definition,
    not this program's work: it writes and commits in one operation.  So the
    diff is the user's -- or another tool's, or a merge conflict someone left
    behind -- and the merge prompt is told to treat it as authority.

    Empty string when git is unavailable or the repository has no commits yet.
    """
    directory = Path(directory)
    if not (directory / ".git").exists():
        return ""
    result = _git(directory, "diff", "--", SUMMARY_FILE, BODY_FILE)
    if result.returncode != 0:  # pragma: no cover - a broken repository is not fatal
        return ""
    return result.stdout.strip()


def commit(directory: Path, message: str) -> bool:
    """Commit the two memory files.  Never raises; failure means no history."""
    directory = Path(directory)
    if not (directory / ".git").exists():
        return False
    # Only the two files.  `raw/` and `notes/` are temporary by construction
    # and a repository full of them is a repository whose log is unreadable.
    _git(directory, "add", "--", SUMMARY_FILE, BODY_FILE)
    result = _git(directory, "commit", "--quiet", "-m", message)
    return result.returncode == 0


def write_gitignore(directory: Path) -> None:
    """Keep the temporary directories out of the repository the merge diffs."""
    path = Path(directory) / ".gitignore"
    if path.exists():
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"{RAW_DIR}/\n{NOTES_DIR}/\n{USAGE_FILE}\n", encoding="utf-8")


# ---------------------------------------------------------------------------
# The whole thing, once, in the background
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class WriteReport:
    """What one run of the pipeline did, in a form that prints in one line.

    A report rather than a log, because the pipeline runs while the user is
    doing something else and the only place it can say anything is one line at
    the end of somebody else's task.  A background job that says nothing is a
    background job nobody can tell is broken -- and this one silently changes
    every future request.
    """

    skipped: str = ""
    sessions: tuple[str, ...] = ()
    empty: tuple[str, ...] = ()
    merged: bool = False
    pruned: Prune | None = None
    secrets_removed: int = 0
    seconds: float = 0.0
    failed: tuple[tuple[str, str], ...] = ()

    def describe(self) -> str:
        if self.skipped:
            return f"memory writer: skipped ({self.skipped})"
        if not self.sessions and not self.merged:
            return "memory writer: nothing to do"
        parts = [f"{len(self.sessions)} session(s) read"]
        if self.empty:
            parts.append(f"{len(self.empty)} had nothing to keep")
        if self.merged and self.pruned is not None:
            parts.append(self.pruned.describe())
        elif not self.merged:
            parts.append("no merge")
        if self.secrets_removed:
            parts.append(f"{self.secrets_removed} secret(s) removed")
        if self.failed:
            parts.append(f"{len(self.failed)} failed")
        return f"memory writer: {', '.join(parts)} in {self.seconds:.1f}s"


async def run_pipeline(
    model: Any,
    *,
    directory: Path,
    sessions_dir: Path,
    jobs_path: Path,
    memory: Memory | None = None,
    limit: int = 2,
    exclude: Sequence[str] = (),
    headroom: float | Callable[[], float | None] | None = None,
    now: float | None = None,
    lock_path: Path = MERGE_LOCK,
) -> WriteReport:
    """Extract from what is pending, merge, prune, commit.

    Runs at the **start** of the next session rather than at the end of this
    one, which is the whole of F17-01: a user who has just been told the answer
    wants their prompt back, not fourteen seconds of a progress line about
    bookkeeping.  Starting it means it overlaps with the first model call
    instead of with nothing.

    Everything in here is bounded: `limit` sessions per run, a lease on each
    one, one merge, one lock.  The unbounded version is a program that appears
    to hang the first time somebody switches it on after a month of sessions.
    """
    began = time.monotonic()
    stamp = now if now is not None else time.time()
    # A callable is allowed, and the reason is a real ordering problem rather
    # than flexibility for its own sake: this task is created *before* the
    # foreground run's first request, so at creation time nobody has seen a
    # rate-limit header yet.  Passing the number would mean passing `None`
    # forever; passing the question means it is answered at the moment it
    # matters.  `None` means "the provider does not say", which is not the
    # same as "there is plenty" -- and the choice made here is to proceed,
    # because the alternative switches the feature off entirely against every
    # server that reports nothing, ollama included.
    if callable(headroom):
        headroom = headroom()
    if headroom is not None and headroom < MIN_RATE_LIMIT_REMAINING_PERCENT:
        # Background work gives way to foreground work.  Not a queue, not a
        # slower rate: nothing at all, until the window resets.  The failure
        # this prevents is the one where a user's actual task gets the 429 that
        # the memory writer earned (F17-02).
        return WriteReport(skipped=f"{headroom:.0f}% quota left", seconds=0.0)

    from minicodex.memory_jobs import JobStore, pending_sessions

    read: list[str] = []
    empty: list[str] = []
    failed: list[tuple[str, str]] = []
    secrets = 0
    with JobStore(jobs_path) as store:
        store.enrol(pending_sessions(sessions_dir, exclude=exclude), now=stamp)
        for job in store.claim(limit=limit, now=stamp):
            try:
                from minicodex.rollout import read_rollout

                stage1 = await extract(model, read_rollout(job.path))
            except Exception as exc:
                # Chapter 0's rule, in a place with no model to hand the error
                # to: one unreadable session cannot be allowed to stop the
                # other one, and it cannot be allowed to take the user's run
                # down either.  It goes back in the queue with a backoff, and
                # after three attempts it stops coming back.
                failed.append((job.session_id, f"{type(exc).__name__}: {exc}"))
                store.fail(job.session_id, f"{type(exc).__name__}: {exc}", now=stamp)
                continue
            secrets += stage1.secrets_removed
            read.append(job.session_id)
            if stage1:
                write_raw(directory, stage1)
            else:
                # Nothing worth keeping is a *success*, and the job is done.
                # Writing an empty raw file instead would make the merge step
                # read a page of empty headings on every run.
                empty.append(job.session_id)
            store.finish(job.session_id, now=stamp)

    raw = pending_raw(directory)
    notes = pending_notes(directory)
    if not raw and not notes:
        return WriteReport(
            sessions=tuple(read),
            empty=tuple(empty),
            secrets_removed=secrets,
            seconds=time.monotonic() - began,
            failed=tuple(failed),
        )

    ensure_repo(directory)
    write_gitignore(directory)
    from minicodex.memory import load as load_memory

    current = memory if memory is not None else load_memory(directory)
    merged = await consolidate(
        model, memory=current, raw=raw, notes=notes, hand_edits=hand_edits(directory)
    )
    pruned = write_memory(
        directory, summary=merged.summary, body=merged.body, now=stamp, lock_path=lock_path
    )
    commit(directory, f"memory: {len(raw)} session(s), {len(notes)} note(s)")
    # Consumed only once the write succeeded.  The other order loses the
    # extraction work on any failure in the merge, and the extraction is the
    # expensive half.
    for path, _ in (*raw, *notes):
        try:
            path.unlink()
        except OSError:  # pragma: no cover
            pass
    return WriteReport(
        sessions=tuple(read),
        empty=tuple(empty),
        merged=True,
        pruned=pruned,
        secrets_removed=secrets + merged.secrets_removed,
        seconds=time.monotonic() - began,
        failed=tuple(failed),
    )


__all__ = [
    "BODY_MARK",
    "MAX_ENTRIES",
    "MAX_NOTE_CHARS",
    "MERGE_LOCK",
    "MIN_RATE_LIMIT_REMAINING_PERCENT",
    "NOTES_DIR",
    "NOTE_NAME",
    "NOTE_SCHEMA",
    "RAW_DIR",
    "REDACTED",
    "REMEMBER_INSTRUCTIONS",
    "STAGE1_NAIVE",
    "STAGE1_QUOTA",
    "STAGE1_SYSTEM",
    "STAGE2_SYSTEM",
    "SUMMARY_MARK",
    "UNUSED_DAYS",
    "MemoryWriteError",
    "Merged",
    "Prune",
    "Stage1",
    "WriteReport",
    "commit",
    "consolidate",
    "ensure_repo",
    "extract",
    "git_available",
    "hand_edits",
    "merge_request",
    "note_toolset",
    "parse_merged",
    "parse_stage1",
    "pending_notes",
    "pending_raw",
    "propose",
    "prune",
    "remember_instructions",
    "render_stage1",
    "run_pipeline",
    "scrub",
    "transcript_of",
    "trim_summary",
    "write_gitignore",
    "write_memory",
    "write_raw",
]
