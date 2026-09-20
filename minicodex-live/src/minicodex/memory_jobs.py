"""Which sessions have been turned into memory, which are being worked on, and by whom.

This is the first database in the program, and the reason is worth stating
because "use a database" is the kind of decision that arrives with no
argument attached.  A rollout stores a conversation and chose an append-only
JSONL file: one writer, no updates, and the worst case is a missing tail.
What is stored here is the opposite of that on every axis --

* every row is **updated** (pending -> claimed -> done),
* rows are **contended**: two `minicodex` processes starting at the same moment
  both want the same unprocessed sessions, and one of them must lose,
* the losing has to be **atomic**, or both of them do the work.

A JSON file would need a lock around every read-modify-write, and the lock is
the part that is hard to get right.  `sqlite3` is in the standard library, it
has that lock, and it has been tested by more people than this program will
ever have users.  codex keeps the same two facts in the same kind of place --
`state/memory_migrations/0001_memories.sql`, which creates exactly two tables:
`stage1_outputs` (one row per extracted rollout, keyed by `thread_id`) and
`jobs` (keyed by `(kind, job_key)`, carrying `status`, `lease_until`,
`retry_at`, `retry_remaining` and a pair of watermark columns).  The lease and
retry columns below are that table's, one name at a time.

The jobs database lives **outside** the memory directory.  That directory is a
git repository and a binary file rewritten on every operation is the
one thing that must not be in one: every commit would carry a blob no diff can
read, which is exactly the material the merge step needs a diff of.
"""

from __future__ import annotations

import os
import sqlite3
import time
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from minicodex.memory import MINICODEX_HOME
from minicodex.rollout import RolloutError, read_rollout

# Beside the memory directory, not inside it, and under the user's home rather
# than under the repository -- the same move the memory directory itself made
# and the reason it has to follow: this table
# records which *sessions* have been turned into memory, and the memory it
# feeds is now one global directory, so a per-repository jobs database would
# let the same session be extracted once per checkout.
DEFAULT_JOBS_PATH = MINICODEX_HOME / "memory_jobs.sqlite3"

# How long a claim is good for.  Not "until the process says it is done": a
# process that is killed says nothing, and a flag would leave the session
# unprocessable forever.  Five minutes is long enough for two model calls on a
# slow provider and short enough that a crashed run is retried the same
# afternoon.
LEASE_SECONDS = 300.0

# How many sessions one startup will consume.  codex ships the same number as
# a configurable default -- `DEFAULT_MEMORIES_MAX_ROLLOUTS_PER_STARTUP: usize
# = 2` (`config/src/types.rs:46`), surfaced as `memories.max_rollouts_per_
# startup` (`config/src/types.rs:309,331`) and read into its Phase-1 claim as
# `max_claimed` -- and the number matters less than its existence: a user who
# has not run the writer for a month has ninety sessions waiting, and "catch up
# on all of them" is a program that appears to hang on the day it is switched
# on.
MAX_PER_STARTUP = 2

# Retry, then stop.  A rollout that fails extraction three times is not going
# to start working: the usual cause is that it is enormous, or that the model
# refuses it, and both are properties of the file rather than of the moment.
MAX_ATTEMPTS = 3

# Doubling, from one minute.  The failure this backs off from is usually a
# provider one, and an immediate retry of a
# impossible request costs -- the quota, and then a 429 for the foreground.
BACKOFF_BASE_SECONDS = 60.0

_SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    session_id  TEXT PRIMARY KEY,
    path        TEXT NOT NULL,
    state       TEXT NOT NULL,
    attempts    INTEGER NOT NULL DEFAULT 0,
    lease_until REAL NOT NULL DEFAULT 0,
    claimed_by  TEXT,
    not_before  REAL NOT NULL DEFAULT 0,
    detail      TEXT,
    updated     REAL NOT NULL DEFAULT 0
);
"""


@dataclass(frozen=True)
class Job:
    """One session waiting to be, or being, turned into memory."""

    session_id: str
    path: Path
    attempts: int = 0

    def describe(self) -> str:
        return f"{self.session_id} ({self.path.name}, attempt {self.attempts})"


class JobStore:
    """The claim table.  Every method is safe to call from a second process.

    `isolation_level=None` is not a style choice: it is what makes a claim
    Python's `sqlite3` defaults to opening a transaction on the first DML
    statement and *not* on a `SELECT`, so the obvious implementation of
    `claim()` -- select the pending rows, then update them -- runs its select
    outside any transaction at all.  Two processes select the same rows, both
    update them, and both do the work.  Measured: 4 duplicate claims out of 6
    jobs across 3 processes, with no error anywhere.  Autocommit plus an
    explicit `BEGIN IMMEDIATE` takes the write lock *before* the select, which
    is the only ordering that makes the pair atomic.
    """

    def __init__(self, path: Path = DEFAULT_JOBS_PATH, *, timeout: float = 10.0) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path, timeout=timeout, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        # Both of these are per-connection and both are needed.  WAL lets a
        # reader (`minicodex memory --jobs`) look at the table while a writer
        # holds it; `busy_timeout` turns "database is locked" from an exception
        # into a wait, which is what a second process starting one millisecond
        # later actually wants.
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute(f"PRAGMA busy_timeout={int(timeout * 1000)}")
        self.db.executescript(_SCHEMA)

    # -- writing ------------------------------------------------------------

    def enrol(self, sessions: Iterable[tuple[str, Path]], *, now: float | None = None) -> int:
        """Record sessions this store has not seen.  Returns how many were new.

        `INSERT OR IGNORE` rather than a select-then-insert, for the reason in
        the class docstring one size down: the primary key is the session id,
        so the database decides who was first.
        """
        stamp = now if now is not None else time.time()
        added = 0
        for session_id, path in sessions:
            cursor = self.db.execute(
                "INSERT OR IGNORE INTO jobs(session_id, path, state, updated) "
                "VALUES (?, ?, 'pending', ?)",
                (session_id, str(path), stamp),
            )
            added += cursor.rowcount or 0
        return added

    def claim(
        self,
        *,
        limit: int = MAX_PER_STARTUP,
        now: float | None = None,
        lease: float = LEASE_SECONDS,
    ) -> tuple[Job, ...]:
        """Take up to `limit` jobs, exclusively, for `lease` seconds.

        Also takes back jobs whose lease has expired, which is the only reason
        a lease exists: the process that claimed them may have been killed, and
        a killed process does not release anything.  A job is *not* taken back
        because it is slow -- the lease is longer than the work.
        """
        stamp = now if now is not None else time.time()
        deadline = stamp + lease
        self.db.execute("BEGIN IMMEDIATE")
        try:
            rows = self.db.execute(
                "SELECT session_id, path, attempts FROM jobs "
                " WHERE (state = 'pending' AND not_before <= ?) "
                "    OR (state = 'claimed' AND lease_until <= ?) "
                " ORDER BY updated LIMIT ?",
                (stamp, stamp, limit),
            ).fetchall()
            claimed = []
            for row in rows:
                self.db.execute(
                    "UPDATE jobs SET state='claimed', attempts=attempts+1, lease_until=?, "
                    "claimed_by=?, updated=? WHERE session_id=?",
                    (deadline, str(os.getpid()), stamp, row["session_id"]),
                )
                claimed.append(
                    Job(row["session_id"], Path(row["path"]), attempts=row["attempts"] + 1)
                )
            self.db.execute("COMMIT")
        except BaseException:
            self.db.execute("ROLLBACK")
            raise
        return tuple(claimed)

    def finish(self, session_id: str, *, now: float | None = None) -> None:
        stamp = now if now is not None else time.time()
        self.db.execute(
            "UPDATE jobs SET state='done', lease_until=0, claimed_by=NULL, updated=? "
            "WHERE session_id=?",
            (stamp, session_id),
        )

    def fail(self, session_id: str, detail: str, *, now: float | None = None) -> None:
        """Hand a job back with a delay, or give up on it.

        The attempt count was already incremented by `claim`, on purpose: a
        process that dies between claiming and failing must still have used up
        an attempt, or a rollout that crashes the extractor is claimed forever
        by whoever starts next.
        """
        stamp = now if now is not None else time.time()
        row = self.db.execute(
            "SELECT attempts FROM jobs WHERE session_id=?", (session_id,)
        ).fetchone()
        attempts = row["attempts"] if row else MAX_ATTEMPTS
        if attempts >= MAX_ATTEMPTS:
            self.db.execute(
                "UPDATE jobs SET state='failed', lease_until=0, claimed_by=NULL, "
                "detail=?, updated=? WHERE session_id=?",
                (detail[:500], stamp, session_id),
            )
            return
        self.db.execute(
            "UPDATE jobs SET state='pending', lease_until=0, claimed_by=NULL, "
            "not_before=?, detail=?, updated=? WHERE session_id=?",
            (stamp + BACKOFF_BASE_SECONDS * (2 ** (attempts - 1)), detail[:500], stamp, session_id),
        )

    def forget_all(self) -> None:
        """Part of `--forget-all`: a user deleting their memory means all of it.

        Leaving the job table behind would mean the sessions that produced the
        deleted memory are marked `done`, so regenerating it would produce
        nothing and the user would conclude the feature is broken.
        """
        self.db.execute("DELETE FROM jobs")

    # -- reading ------------------------------------------------------------

    def rows(self) -> list[dict[str, Any]]:
        return [dict(r) for r in self.db.execute("SELECT * FROM jobs ORDER BY updated DESC")]

    def counts(self) -> dict[str, int]:
        return {
            row["state"]: row["n"]
            for row in self.db.execute("SELECT state, COUNT(*) AS n FROM jobs GROUP BY state")
        }

    def state_of(self, session_id: str) -> str | None:
        row = self.db.execute("SELECT state FROM jobs WHERE session_id=?", (session_id,)).fetchone()
        return row["state"] if row else None

    def close(self) -> None:
        self.db.close()

    def __enter__(self) -> JobStore:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


def pending_sessions(
    directory: Path,
    *,
    exclude: Sequence[str] = (),
) -> list[tuple[str, Path]]:
    """Session files that are finished, are a user's, and are not this run's own.

    Two exclusions to start with, each of which is a fault somewhere else in
    the book:

    * **Still open.**  An `O_EXCL` lock file sits next to a rollout
      that is being written.  Its presence is reused here as the answer to
      "has this session finished", because the alternative -- a footer record
      written at the end -- is a footer that a crashed run never writes, and
      the crashed runs are the ones with the interesting failures in them.

    * **This process's own session**, passed in by the caller.  It has not
      finished; it does not even have its question answered yet.

    ...and a third, which was not planned and was found by running the thing:
    **a sub-agent's session**.  Four questions asked through the CLI left seven
    files, because one of them spawned two children, and each child is a
    complete rollout with a `parent` field.  Extracted, they produce a third
    and fourth copy of what the parent's session already says -- measured, the
    two children of one task both yielded "the `pad_string(s, width, fillchar)`
    function is in util/text.py", as did the parent.  They also have no user in
    them: the whole "stable preference the user stated" half of stage 1 is
    inapplicable to a conversation whose only participant is another agent.

    `rollout.resolve("last")` makes the same exclusion for the same reason: a
    run that spawns two sub-agents leaves three files, and the two newest are
    the children.  The lesson is not about sub-agents.  It is that **a session
    directory is not a list of user conversations**, and every new reader of
    that directory has to be told again.
    """
    directory = Path(directory)
    if not directory.is_dir():
        return []
    out: list[tuple[str, Path]] = []
    for path in sorted(directory.glob("*.jsonl")):
        if path.with_suffix(path.suffix + ".lock").exists():
            continue
        try:
            rollout = read_rollout(path)
        except RolloutError:
            continue
        if rollout.meta.parent or rollout.meta.session_id in exclude:
            continue
        if not rollout.items:
            continue
        out.append((rollout.meta.session_id, path))
    return out


__all__ = [
    "BACKOFF_BASE_SECONDS",
    "DEFAULT_JOBS_PATH",
    "LEASE_SECONDS",
    "MAX_ATTEMPTS",
    "MAX_PER_STARTUP",
    "Job",
    "JobStore",
    "pending_sessions",
]
