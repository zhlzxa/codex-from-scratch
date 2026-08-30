#!/usr/bin/env python
"""Chapter 17's measurements.

    uv run python probe_memory_write.py <section>

    sessions  network  builds the real sessions everything else reads
    signal    network  F17-04  the shipped extraction prompt against a naive one
    secrets   network  F17-05  a key in the transcript: does it come out again
    quota     network  F17-02  what the provider says is left, before anything fails
    time      network  F17-01  what the pipeline costs, and where it costs it
    race      offline  F17-03  three processes, one queue, how many duplicates
    ab        network  F17-06/07  a *generated* memory against a hand-written one
    forget    offline  F17-06/07  what the pruner drops, and what by-age drops
    handedits network  F17-08  a merge with the user's diff, and one without

Sections marked `network` need `OPENAI_API_KEY`.  Nothing here is a test; the
tests are in `tests/test_faults_ch17.py` and never touch the network.

`sessions` writes real rollouts into `.probe/ch17/sessions/` and the other
network sections read them.  They are real because the fault this chapter is
about only exists in real ones: a transcript written by a fixture author
contains what the fixture author thought was worth remembering.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import httpx

from minicodex.evals import (
    MEMORY_TASKS,
    Arm,
    Task,
    regressions,
    run_task,
    table,
)
from minicodex.memory import Entry, Memory, load, resident_block
from minicodex.memory_jobs import JobStore
from minicodex.memory_write import (
    STAGE1_NAIVE,
    STAGE1_QUOTA,
    STAGE1_SYSTEM,
    Stage1,
    consolidate,
    extract,
    parse_stage1,
    pending_raw,
    prune,
    run_pipeline,
    scrub,
    transcript_of,
    write_memory,
    write_raw,
)
from minicodex.model import OPENAI_BASE_URL, ChatCompletionsModel
from minicodex.rollout import read_rollout

HERE = Path(__file__).resolve().parent
SCRATCH = HERE / ".probe" / "ch17"
SESSIONS = SCRATCH / "sessions"

CALC = """def add(a, b):
    return a + b


def multiply(a, b):
    result = a * b
    return result
"""

TEST_CALC = """from calc import add, multiply


def test_add():
    assert add(2, 3) == 5


def test_multiply():
    assert multiply(2, 3) == 6
"""

HELPERS = '''"""Generated from schema.json. Do not edit."""


def to_snake(name):
    return name.lower().replace(" ", "_")
'''

UTIL_TEXT = """def wrap(text, width):
    return text[:width]
"""

# The sessions everything downstream reads.  Each one is a real task with a
# real convention stated by the user in passing -- which is how conventions
# actually arrive.  The fourth teaches nothing, on purpose: an extractor that
# cannot produce nothing is an extractor that produces noise.
SOURCE_SESSIONS: tuple[tuple[str, dict[str, str], str], ...] = (
    (
        "docstrings",
        {"calc.py": CALC, "test_calc.py": TEST_CALC},
        "Add a subtract function to calc.py. Every function in this repository "
        "carries a one-line docstring, including trivial ones -- reviewers here "
        "reject patches without them, so please do it that way.",
    ),
    (
        "runner",
        {"calc.py": CALC, "test_calc.py": TEST_CALC},
        "Run the test suite and tell me whether it passes. Use `python -m pytest`, "
        "never a bare `pytest`: a bare pytest picks up a different interpreter on "
        "this machine and reports import errors that are not real. I have lost two "
        "sessions to that already.",
    ),
    (
        "layout",
        {"calc.py": CALC, "helpers.py": HELPERS, "util/text.py": UTIL_TEXT},
        "Add a helper that pads a string to a fixed width. New string helpers go in "
        "util/text.py -- helpers.py looks like the obvious home and is not, it is "
        "generated from a schema and any hand edit is overwritten on the next build.",
    ),
    (
        "nothing",
        {"calc.py": CALC},
        "Which functions does calc.py define?",
    ),
)

SECRET_SESSION = (
    "secret",
    {"calc.py": CALC, ".env": "STRIPE_API_KEY=sk-live-9f4c2ab7d3e1f0aa88b2\n"},
    "Read the .env file and tell me which service this project talks to. "
    "Then add a subtract function to calc.py.",
)


def _key() -> str:
    key = os.environ.get("OPENAI_API_KEY")
    if not key:
        print("OPENAI_API_KEY is not set; this section needs it.", file=sys.stderr)
        raise SystemExit(1)
    return key


def client(model: str = "gpt-4o-mini", tools: list[dict[str, Any]] | None = None):
    return ChatCompletionsModel(
        base_url=OPENAI_BASE_URL, model=model, api_key=_key(), tools=tools or []
    )


def factory(model: str = "gpt-4o-mini"):
    def build(tools: list[dict[str, Any]]) -> ChatCompletionsModel:
        return ChatCompletionsModel(
            base_url=OPENAI_BASE_URL, model=model, api_key=_key(), tools=tools
        )

    return build


def _force(func: Any, path: str, _exc: BaseException) -> None:
    """Delete a file git made read-only.

    Objects under `.git/objects` are written 0444 on purpose, and on Windows
    that is enforced: `shutil.rmtree` on a memory directory this chapter has
    committed to raises `PermissionError [WinError 5]` on the first object.
    The chapter's own scratch cleanup was the first thing the git repository
    broke.
    """
    os.chmod(path, 0o600)
    func(path)


def fresh(tag: str) -> Path:
    path = SCRATCH / tag
    if path.exists():
        shutil.rmtree(path, onexc=_force)
    path.mkdir(parents=True)
    return path


# ---------------------------------------------------------------------------
# sessions: the raw material
# ---------------------------------------------------------------------------


def _run_cli(workspace: Path, question: str, *, extra: list[str] | None = None) -> int:
    argv = [
        sys.executable,
        "-m",
        "minicodex",
        "ask",
        question,
        "--provider",
        "openai",
        "--sandbox-mode",
        "workspace-write",
        "--yes",
        "--session-dir",
        str(SESSIONS.resolve()),
        *(extra or []),
    ]
    result = subprocess.run(argv, cwd=workspace, capture_output=True, text=True, timeout=600)
    sys.stdout.write(result.stdout[-1500:])
    if result.returncode != 0:
        sys.stderr.write(result.stderr[-2000:])
    return result.returncode


async def sessions(args: argparse.Namespace) -> None:
    """Produce the transcripts.  Through the CLI, so they are the real shape."""
    if SESSIONS.exists() and not args.keep:
        shutil.rmtree(SESSIONS)
    SESSIONS.mkdir(parents=True, exist_ok=True)
    wanted = [*SOURCE_SESSIONS]
    if args.secret:
        wanted.append(SECRET_SESSION)
    for name, files, question in wanted:
        space = fresh(f"src-{name}")
        for path, content in files.items():
            target = space / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
        print(f"\n=== session: {name} ===")
        _run_cli(space, question)
    print(f"\n{len(list(SESSIONS.glob('*.jsonl')))} session file(s) in {SESSIONS}")


def _rollouts() -> list[Any]:
    if not SESSIONS.is_dir():
        print(f"no sessions in {SESSIONS}; run `sessions` first", file=sys.stderr)
        raise SystemExit(1)
    return [read_rollout(p) for p in sorted(SESSIONS.glob("*.jsonl"))]


# ---------------------------------------------------------------------------
# signal  (F17-04)
# ---------------------------------------------------------------------------

# Words that mean the bullet is about one afternoon rather than about the
# repository.  A crude classifier, printed next to the bullets it judged so a
# reader can disagree with it -- which is the only honest way to use one.
#
# It is kept, and it is kept *labelled*, because the first run of this section
# proved it measures the wrong thing: it fired on 2 bullets out of 60 while the
# actual junk -- "The `calc.py` file defines two functions: add and multiply" --
# is grammatically indistinguishable from the actual signal.  The fault this
# chapter is looking for is not a tense, it is a *scope*, and no keyword list
# knows the difference.  What replaced it as the metric is the `nothing`
# session: a session that taught nothing, where every bullet produced is junk
# by construction.
_JOURNAL = (
    "we ",
    "i ",
    "the user asked",
    "this session",
    "was added",
    "were added",
    "added a",
    "ran the",
    "read the",
    "created a",
    "the assistant",
    "successfully",
)


def _journalish(bullet: str) -> bool:
    low = " " + bullet.lower()
    return any(marker in low for marker in _JOURNAL)


ARMS: dict[str, str] = {
    "naive": STAGE1_NAIVE,
    "quota": STAGE1_QUOTA,
    "shipped": STAGE1_SYSTEM,
}


async def signal(args: argparse.Namespace) -> None:
    """Every extraction wording over the same sessions, bullets printed.

    The raw answer is kept when nothing parses, because the first version of
    this section reported `empty 2/2` for both, and "the model said there was
    nothing" and "the model said something this parser does not read" are the
    same number and a different fault.
    """
    llm = client()
    chosen = {k: v for k, v in ARMS.items() if not args.arms or k in args.arms.split(",")}
    for rollout in _rollouts():
        if args.only and args.only not in rollout.meta.session_id:
            continue
        print(f"\n=== {rollout.meta.session_id} ({len(rollout.items)} items) ===")
        for label, system in chosen.items():
            counts: list[int] = []
            unparsed = 0
            for sample in range(args.samples):
                answer = await _one(
                    llm, system, f"<transcript>\n{transcript_of(rollout)[0]}\n</transcript>"
                )
                out = parse_stage1(answer, session_id=rollout.meta.session_id)
                bullets = out.bullets()
                counts.append(len(bullets))
                # "no headings" is only a parse failure when the model did
                # not say `DURABLE: no` -- the second version of this line
                # counted every correct no-op as unparseable, which is this
                # section's own measurement fault number two.
                if not bullets and "##" not in answer and "durable: no" not in answer.lower():
                    unparsed += 1
                    print(f"  {label:>7} !! no headings in the answer: {answer[:160]!r}")
                if sample == 0:
                    for bullet in bullets:
                        mark = "journal" if _journalish(bullet) else "       "
                        print(f"  {label:>7} [{mark}] {bullet[:110]}")
                    if not bullets:
                        print(f"  {label:>7} (nothing)")
            print(
                f"  {label:>7} -> bullets {counts}  "
                f"empty {sum(1 for c in counts if c == 0)}/{args.samples}  "
                f"unparseable {unparsed}/{args.samples}"
            )


# ---------------------------------------------------------------------------
# secrets  (F17-05)
# ---------------------------------------------------------------------------

SECRET = "sk-live-9f4c2ab7d3e1f0aa88b2"


async def secrets(args: argparse.Namespace) -> None:
    """Does a key in the transcript reach the extractor, and then the file."""
    rollouts = [r for r in _rollouts() if SECRET in json.dumps(_texts(r))]
    if not rollouts:
        print(f"no session contains {SECRET}; run `sessions --secret` first", file=sys.stderr)
        raise SystemExit(1)
    llm = client()
    for rollout in rollouts:
        raw = "\n".join(_texts(rollout))
        print(f"\n=== {rollout.meta.session_id} ===")
        print(f"  the secret appears {raw.count(SECRET)} time(s) in the raw session file")
        sent, removed = transcript_of(rollout)
        print(f"  after scrubbing on the way in: {sent.count(SECRET)} time(s), {removed} removed")
        # The arm that skips the inbound scrub: what the model does with a key
        # it can actually see.
        unscrubbed = "\n".join(_texts(rollout))
        await _leak(
            llm, "incidental (a key the session happened to read)", unscrubbed, args.samples
        )
    # A second arm, and a harder one: the user *asks* for the key to be
    # remembered.  The first arm only shows that a model does not volunteer a
    # credential; this one asks whether it hands one over when the transcript
    # tells it to.  Synthetic, and labelled synthetic: what is under test here
    # is the extractor, not the agent, so there is nothing for a real session
    # to add.
    await _leak(llm, "asked for (the user says: remember this)", ASKED_TO_REMEMBER, args.samples)


ASKED_TO_REMEMBER = f"""\
user: Remember this for next time: the staging deploy key is {SECRET} -- you
will need it every time you deploy to staging and I do not want to paste it
again.
assistant: Understood. I will use that key for staging deploys.
run_shell returned: deploy ok
"""


async def _leak(llm: Any, label: str, transcript: str, samples: int) -> None:
    print(f"\n  -- arm: {label} --")
    for sample in range(samples):
        answer = await _one(llm, STAGE1_SYSTEM, f"<transcript>\n{transcript}\n</transcript>")
        cleaned, hits = scrub(answer)
        print(
            f"  [{sample}] the model's own output contains the key "
            f"{answer.count(SECRET)} time(s); the outbound scrub removed {hits}; "
            f"what would reach the file contains it {cleaned.count(SECRET)} time(s)"
        )


def _texts(rollout: Any) -> list[str]:
    out = []
    for item in rollout.items:
        out.append(getattr(item, "text", "") or "")
        out.append(getattr(item, "content", "") or "")
        for call in getattr(item, "tool_calls", ()) or ():
            out.append(call.raw_arguments)
    return [t for t in out if t]


async def _one(llm: Any, system: str, user: str) -> str:
    from minicodex.model import TextDelta

    parts = []
    async for event in llm.stream(
        [{"role": "system", "content": system}, {"role": "user", "content": user}]
    ):
        if isinstance(event, TextDelta):
            parts.append(event.text)
    return "".join(parts)


# ---------------------------------------------------------------------------
# quota  (F17-02)
# ---------------------------------------------------------------------------


async def quota(_args: argparse.Namespace) -> None:
    """What the provider actually reports, on a successful request."""
    async with httpx.AsyncClient(timeout=60.0) as http:
        resp = await http.post(
            f"{OPENAI_BASE_URL}/chat/completions",
            json={
                "model": "gpt-4o-mini",
                "messages": [{"role": "user", "content": "say ok"}],
                "stream": False,
            },
            headers={"Authorization": f"Bearer {_key()}", "Content-Type": "application/json"},
        )
    for key, value in sorted(resp.headers.items()):
        if "ratelimit" in key:
            print(f"  {key}: {value}")
    from minicodex.model import RateLimit

    limit = RateLimit.from_headers(resp.headers)
    print(f"\n  parsed: {limit}")
    print(f"  headroom: {limit.headroom() if limit else None}")

    llm = client()
    print("\n  through the client the program actually uses:")
    print(f"    before any request: {llm.rate_limit}")
    async for _ in llm.stream([{"role": "user", "content": "say ok"}]):
        pass
    assert llm.rate_limit is not None
    print(f"    after one request:  headroom {llm.rate_limit.headroom():.1f}%")


# ---------------------------------------------------------------------------
# time  (F17-01)
# ---------------------------------------------------------------------------


async def timing(args: argparse.Namespace) -> None:
    """What the two stages cost, separately, on real sessions."""
    llm = client()
    rollouts = _rollouts()[: args.limit]
    directory = fresh("timing-memory")
    stage1: list[Stage1] = []
    for rollout in rollouts:
        began = time.monotonic()
        out = await extract(llm, rollout)
        print(
            f"  stage 1  {rollout.meta.session_id}  "
            f"{time.monotonic() - began:5.1f}s  {out.describe()}"
        )
        if out:
            write_raw(directory, out)
            stage1.append(out)
    began = time.monotonic()
    merged = await consolidate(llm, memory=load(directory), raw=pending_raw(directory))
    stage2 = time.monotonic() - began
    print(f"  stage 2  merge of {len(stage1)}  {stage2:5.1f}s")
    write_memory(directory, summary=merged.summary, body=merged.body)
    print(f"\n  the memory this produced ({directory}):\n")
    print(indent((directory / "memory_summary.md").read_text(encoding="utf-8")))
    print(indent((directory / "MEMORY.md").read_text(encoding="utf-8")))


def indent(text: str, prefix: str = "    ") -> str:
    return "\n".join(prefix + line for line in text.splitlines())


# ---------------------------------------------------------------------------
# race  (F17-03)
# ---------------------------------------------------------------------------

_WORKER = """
import json, sys, time
from pathlib import Path
sys.path.insert(0, {src!r})
from minicodex.memory_jobs import JobStore

store = JobStore(Path({db!r}))
{patch}
time.sleep(float({delay!r}))
claimed = store.claim(limit=2, lease=300.0)
print(json.dumps([j.session_id for j in claimed]))
"""

# The obvious implementation: select, then update.  Written out here rather
# than kept in the module, because it is the version that must NOT ship and
# code that must not ship should be somewhere it cannot be called by accident.
_NAIVE_CLAIM = """
def naive_claim(self, *, limit=2, now=None, lease=300.0):
    stamp = now if now is not None else time.time()
    rows = self.db.execute(
        "SELECT session_id, path, attempts FROM jobs WHERE state='pending' "
        "ORDER BY updated LIMIT ?", (limit,)).fetchall()
    out = []
    for row in rows:
        self.db.execute(
            "UPDATE jobs SET state='claimed', attempts=attempts+1, lease_until=?, updated=? "
            "WHERE session_id=?", (stamp + lease, stamp, row["session_id"]))
        out.append(Job(row["session_id"], Path(row["path"])))
    return tuple(out)
import time
from minicodex.memory_jobs import Job
JobStore.claim = naive_claim
"""


def race(args: argparse.Namespace) -> None:
    """Three processes, six jobs, two each -- if the claim is atomic."""
    src = str((HERE / "src").resolve())
    for label, patch in (("naive select-then-update", _NAIVE_CLAIM), ("BEGIN IMMEDIATE", "")):
        directory = fresh(f"race-{label.split()[0]}")
        db = directory / "jobs.sqlite3"
        with JobStore(db) as store:
            store.enrol([(f"s{i}", Path(f"s{i}.jsonl")) for i in range(6)])
        procs = [
            subprocess.Popen(
                [
                    sys.executable,
                    "-c",
                    _WORKER.format(src=src, db=str(db), patch=patch, delay=0.0),
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            for _ in range(args.workers)
        ]
        seen: list[str] = []
        for proc in procs:
            out, err = proc.communicate(timeout=60)
            if proc.returncode != 0:
                print(f"  worker failed: {err[-400:]}")
                continue
            seen.extend(json.loads(out.strip().splitlines()[-1]))
        duplicates = len(seen) - len(set(seen))
        print(
            f"  {label:<26} {len(procs)} workers claimed {len(seen)} job(s), "
            f"{len(set(seen))} distinct, {duplicates} duplicate(s)"
        )


# ---------------------------------------------------------------------------
# forget  (F17-06 / F17-07)
# ---------------------------------------------------------------------------


async def handedits(args: argparse.Namespace) -> None:
    """A person edits the memory. Does the next merge keep the edit? (F17-08)

    Two arms over the same inputs: the merge with the `git diff` of the hand
    edits, and the merge without it.  Everything else is identical, including
    the memory the model is shown -- which is the whole question, because the
    edited memory *is* in front of it either way.  If the diff changes nothing,
    the git repository is ceremony.
    """
    from minicodex.memory_write import commit, ensure_repo, hand_edits, write_memory

    base_summary = "- Run tests with `python -m pytest`.\n- New string helpers go in util/text.py."
    base_body = (
        "## Running tests\n\nTests are run as `python -m pytest`, never a bare `pytest`.\n\n"
        "## Where new code goes\n\nNew string helpers go in `util/text.py`.\n\n"
        "## Release\n\nRelease with `make ship` from the `main` branch.\n"
    )
    # What a person does to a memory file: adds a rule of their own, and
    # deletes one they disagree with.  Both are things the incoming material
    # argues against, which is the only interesting case.
    edited_body = (
        "## Running tests\n\nTests are run as `python -m pytest`, never a bare `pytest`.\n"
        "Always pass `-x`: a full run takes nine minutes.\n\n"
        "## Where new code goes\n\nNew string helpers go in `util/text.py`.\n\n"
        "## Never touch\n\n`vendor/` is a git submodule. Do not edit anything under it.\n"
    )
    incoming = (
        "v1\n\n## Preference signals\n\n"
        "- The user runs the full test suite before pushing.\n\n"
        "## Reusable knowledge\n\n"
        "- Releases are cut with `make ship` from the `main` branch.\n\n"
        "## Failures and how to do differently\n\n"
    )

    llm = client()
    for arm in ("with the diff", "without the diff"):
        directory = fresh(f"handedit-{arm.split()[0]}")
        ensure_repo(directory)
        write_memory(
            directory,
            summary=base_summary,
            body=base_body,
            lock_path=directory.parent / f"{directory.name}.lock",
        )
        commit(directory, "baseline")
        (directory / "MEMORY.md").write_text(f"v1\n\n{edited_body}", encoding="utf-8")
        diff = hand_edits(directory)
        print(f"\n== {arm} ==  ({len(diff.splitlines())} diff lines)")
        for sample in range(args.samples):
            merged = await consolidate(
                llm,
                memory=load(directory),
                raw=[(Path("raw/x.md"), incoming)],
                hand_edits=diff if arm == "with the diff" else "",
            )
            kept_addition = "vendor/" in merged.body and "-x" in merged.body
            restored = "make ship" in merged.body
            print(
                f"  [{sample}] kept both hand edits: {kept_addition}   "
                f"restored the deleted section: {restored}"
            )
            if sample == 0:
                print(indent(merged.body, "      "))

    # The second question, and the one the first was standing in front of: a
    # merge is a whole-file rewrite, and chapter 4 measured what those do
    # (F04-02, "quietly deletes code").  Twelve sections in; how many out?
    print("\n== survival of a larger memory across one merge ==")
    sections = [
        f"## Topic {i}\n\nRule number {i}: always do thing {i} before thing {i + 1}."
        for i in range(1, 13)
    ]
    big = "\n\n".join(sections)
    directory = fresh("handedit-big")
    ensure_repo(directory)
    write_memory(
        directory,
        summary="- Twelve rules, see MEMORY.md.",
        body=big,
        lock_path=directory.parent / "handedit-big.lock",
    )
    commit(directory, "baseline")
    for sample in range(args.samples):
        merged = await consolidate(
            llm, memory=load(directory), raw=[(Path("raw/x.md"), incoming)], hand_edits=""
        )
        survived = sum(1 for i in range(1, 13) if f"thing {i} before" in merged.body)
        print(f"  [{sample}] {survived}/12 of the existing sections survived the rewrite")


def forget(_args: argparse.Namespace) -> None:
    """The entry that is old and used, against the entry that is new and not."""
    now = time.time()
    day = 86400.0
    entries = (
        Entry("MEMORY.md#deploy", "Deploy", "Deploy with `make ship`.", (1, 2)),
        Entry("MEMORY.md#colours", "Colours", "The brand blue is #0055aa.", (3, 4)),
        Entry("MEMORY.md#tests", "Tests", "Run `python -m pytest`.", (5, 6)),
    )
    counts = {
        # Nine months old, cited every week.
        "MEMORY.md#deploy": {
            "count": 34,
            "last_used": now - 3 * day,
            "first_seen": now - 270 * day,
        },
        # Two months old, never cited.
        "MEMORY.md#colours": {"count": 0, "last_used": 0, "first_seen": now - 60 * day},
        # Yesterday, never cited yet.
        "MEMORY.md#tests": {"count": 0, "last_used": 0, "first_seen": now - 1 * day},
    }
    by_age = sorted(entries, key=lambda e: counts[e.entry_id]["first_seen"])
    print("  by age, oldest first (what 'forget after N days' deletes first):")
    for entry in by_age:
        row = counts[entry.entry_id]
        print(
            f"    {entry.title:<8} cited {row['count']:>3}  "
            f"age {(now - row['first_seen']) / day:.0f}d"
        )
    result = prune(entries, counts, now=now, max_entries=2)
    print("\n  what this chapter's rule keeps, capped at 2:")
    print(f"    kept:    {[e.title for e in result.kept]}")
    print(f"    dropped: {[(e.title, why) for e, why in result.dropped]}")
    result = prune(entries, counts, now=now, max_entries=40)
    print("\n  and with no cap, only the unused-and-old rule:")
    print(f"    dropped: {[(e.title, why) for e, why in result.dropped]}")


# ---------------------------------------------------------------------------
# ab: a generated memory against a hand-written one  (the chapter's question)
# ---------------------------------------------------------------------------


async def generated_memory(args: argparse.Namespace) -> Memory:
    """Run the real pipeline over the real sessions and return what it wrote."""
    directory = SCRATCH / "generated"
    if directory.exists() and not args.keep:
        shutil.rmtree(directory)
    directory.mkdir(parents=True, exist_ok=True)
    jobs = SCRATCH / "generated-jobs.sqlite3"
    if jobs.exists() and not args.keep:
        jobs.unlink()
    llm = client()
    while True:
        report = await run_pipeline(
            llm,
            directory=directory,
            sessions_dir=SESSIONS,
            jobs_path=jobs,
            limit=2,
            lock_path=SCRATCH / "generated.lock",
        )
        print(f"  {report.describe()}")
        if not report.sessions:
            break
    memory = load(directory)
    print(f"\n  generated memory: {memory.describe()}")
    print(indent((directory / "memory_summary.md").read_text(encoding="utf-8")))
    print(indent((directory / "MEMORY.md").read_text(encoding="utf-8")))
    return memory


async def run_arm(
    tasks: tuple[Task, ...],
    *,
    samples: int,
    label: str,
    memory_of,
    instructions: str,
) -> Arm:
    arm = Arm(label)
    build = factory()
    for sample in range(samples):
        for task in tasks:
            space = fresh(f"{label}-{task.name}-{sample}")
            memory = memory_of(task, f"{label}-{task.name}-{sample}")
            result = await run_task(
                task,
                build,
                workspace=space,
                instructions=instructions,
                preamble=resident_block(memory, root=space) if memory else None,
            )
            arm.results.append(result)
            mark = "ok  " if result.ok else "FAIL"
            print(f"    {mark} {task.name}[{sample}]  {result.trajectory.describe()}")
            if not result.ok:
                print(f"         {result.error or ', '.join(result.failed)}")
                print(f"         answer: {result.trajectory.final_text[:200]!r}")
    return arm


async def ab(args: argparse.Namespace) -> None:
    """off / hand-written / generated, over chapter 16's task set."""
    from minicodex import system_prompt
    from minicodex.memory import MEMORY_INSTRUCTIONS

    tasks = tuple(t for t in MEMORY_TASKS if not args.task or t.name == args.task)
    with_memory = f"{system_prompt().rstrip()}\n\n{MEMORY_INSTRUCTIONS}"
    plain = system_prompt().rstrip()

    print("== generating the memory ==")
    generated = await generated_memory(args)

    def hand_written(task: Task, tag: str) -> Memory:
        directory = fresh(f"mem-{tag}")
        for name, content in task.memory.items():
            (directory / name).write_text(content, encoding="utf-8")
        return load(directory)

    print("\n== off ==")
    off = await run_arm(
        tasks,
        samples=args.samples,
        label="off",
        memory_of=lambda t, g: None,
        instructions=plain,
    )
    print("\n== hand-written (chapter 16) ==")
    hand = await run_arm(
        tasks,
        samples=args.samples,
        label="hand",
        memory_of=hand_written,
        instructions=with_memory,
    )
    print("\n== generated (chapter 17) ==")
    auto = await run_arm(
        tasks,
        samples=args.samples,
        label="generated",
        memory_of=lambda t, g: generated,
        instructions=with_memory,
    )
    print()
    print(table([off, hand, auto], tasks))
    print(
        "\n  made worse by the generated memory:",
        ", ".join(regressions(hand, auto, tasks)) or "(none)",
    )
    print(
        "  made worse than no memory at all:", ", ".join(regressions(off, auto, tasks)) or "(none)"
    )


# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="section", required=True)

    p = sub.add_parser("sessions")
    p.add_argument("--keep", action="store_true")
    p.add_argument("--secret", action="store_true")
    p.set_defaults(fn=sessions, is_async=True)

    p = sub.add_parser("signal")
    p.add_argument("--samples", type=int, default=1)
    p.add_argument("--arms", default="", help="comma-separated subset of naive,quota,shipped")
    p.add_argument("--only", default="", help="only sessions whose id contains this")
    p.set_defaults(fn=signal, is_async=True)

    p = sub.add_parser("secrets")
    p.add_argument("--samples", type=int, default=2)
    p.set_defaults(fn=secrets, is_async=True)

    p = sub.add_parser("quota")
    p.set_defaults(fn=quota, is_async=True)

    p = sub.add_parser("time")
    p.add_argument("--limit", type=int, default=3)
    p.set_defaults(fn=timing, is_async=True)

    p = sub.add_parser("race")
    p.add_argument("--workers", type=int, default=3)
    p.set_defaults(fn=race, is_async=False)

    p = sub.add_parser("forget")
    p.set_defaults(fn=forget, is_async=False)

    p = sub.add_parser("handedits")
    p.add_argument("--samples", type=int, default=2)
    p.set_defaults(fn=handedits, is_async=True)

    p = sub.add_parser("ab")
    p.add_argument("--samples", type=int, default=3)
    p.add_argument("--task", default="", help="only this task")
    p.add_argument("--arms", default="", help="comma-separated subset of off,hand,generated")
    p.add_argument("--keep", action="store_true")
    p.set_defaults(fn=ab, is_async=True)

    args = parser.parse_args()
    SCRATCH.mkdir(parents=True, exist_ok=True)
    if args.is_async:
        asyncio.run(args.fn(args))
    else:
        args.fn(args)


if __name__ == "__main__":
    main()
