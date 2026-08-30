"""Chapter 17 -- memory, the write path: extraction, merging, forgetting.

Offline, all of it, for chapter 14's reason (F14-05).  Two model calls sit in
the middle of this subsystem and both are replaced here by a scripted answer:
what a real one produces is measured in `probe_memory_write.py`, with the
sample counts written down, and what is pinned here is everything that is a
fact about the program -- what is claimed, what is written, what is refused,
what is forgotten and in what order.

The one thing worth saying about the split: the *prompts* are pinned here as
snapshots even though their effect is measured elsewhere.  That is chapter 3's
F03-10 discipline (an edit to a measured wording must be a visible edit), and
this chapter has two wordings whose difference was bought at 3/3 against 0/3.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import time
from pathlib import Path
from typing import Any

import pytest

from minicodex.approval import AllowAll, Session
from minicodex.composition import top_level_tools
from minicodex.memory import (
    BODY_FILE,
    FORMAT_VERSION,
    SUMMARY_FILE,
    SUMMARY_TOKEN_BUDGET,
    USAGE_FILE,
    CitedEntry,
    Entry,
    load,
    record_uses,
)
from minicodex.memory_jobs import (
    BACKOFF_BASE_SECONDS,
    MAX_ATTEMPTS,
    MAX_PER_STARTUP,
    JobStore,
    pending_sessions,
)
from minicodex.memory_write import (
    BODY_MARK,
    MAX_ENTRIES,
    MIN_RATE_LIMIT_REMAINING_PERCENT,
    NOTE_NAME,
    NOTES_DIR,
    RAW_DIR,
    REDACTED,
    STAGE1_QUOTA,
    STAGE1_SYSTEM,
    STAGE2_SYSTEM,
    SUMMARY_MARK,
    MemoryWriteError,
    Stage1,
    commit,
    ensure_repo,
    extract,
    git_available,
    hand_edits,
    merge_request,
    note_toolset,
    parse_merged,
    parse_stage1,
    pending_notes,
    pending_raw,
    propose,
    prune,
    run_pipeline,
    scrub,
    transcript_of,
    trim_summary,
    write_memory,
    write_raw,
)
from minicodex.model import RateLimit
from minicodex.rollout import RolloutWriter, SessionMeta, read_rollout
from minicodex.subagent import SubAgentContext

DAY = 86400.0

needs_git = pytest.mark.skipif(not git_available(), reason="git is not on PATH")


# ---------------------------------------------------------------------------
# F17-01  the writer runs beside the next session, not at the end of this one
# ---------------------------------------------------------------------------


def test_F17_01_the_pipeline_consumes_earlier_sessions_and_never_this_one(tmp_path: Path) -> None:
    """The whole shape of the fix in one assertion.

    Generating memory at the end of a run means the user waits for it -- 15.4
    measured seconds for four sessions.  Generating it at the *start* of the
    next run costs nobody anything, and the price is that the material is
    always one session out of date.  The current session is excluded by name
    rather than by "it has no lock yet", because it does have a lock: it is
    open.
    """
    from minicodex.history import UserMessage

    sessions = tmp_path / "sessions"
    finished = _write_session(sessions, "old", ["remember: use uv"])
    current = SessionMeta(session_id="current")
    with RolloutWriter(sessions / "current.jsonl", current) as writer:
        writer.append(UserMessage("what am I doing"))
        # Open, locked, and therefore invisible even without `exclude`.
        assert [s for s, _ in pending_sessions(sessions)] == ["old"]
    assert sorted(s for s, _ in pending_sessions(sessions)) == ["current", "old"]
    assert [s for s, _ in pending_sessions(sessions, exclude=["current"])] == ["old"]
    assert finished.exists()


def test_F17_01_at_most_two_sessions_are_consumed_per_startup(tmp_path: Path) -> None:
    """A user who switches this on after a month has ninety sessions waiting."""
    store = JobStore(tmp_path / "jobs.db")
    store.enrol([(f"s{i}", Path(f"s{i}.jsonl")) for i in range(10)])
    assert len(store.claim()) == MAX_PER_STARTUP
    store.close()


def test_F17_01_a_session_that_is_still_open_is_not_eligible(tmp_path: Path) -> None:
    """Chapter 7's lock file, reused as "has this finished".

    A footer record would be the other way to answer it, and a crashed run
    never writes a footer -- so the sessions with the most interesting
    failures in them would be the ones never extracted.
    """
    sessions = tmp_path / "sessions"
    _write_session(sessions, "done", ["hello"])
    writer = RolloutWriter(sessions / "live.jsonl", SessionMeta(session_id="live"))
    try:
        assert [s for s, _ in pending_sessions(sessions)] == ["done"]
    finally:
        writer.release()


def test_F17_01_a_sub_agents_session_is_not_a_users_session(tmp_path: Path) -> None:
    """Not on the list, and found by running four questions through the CLI.

    They left *seven* session files, because one task spawned two children.
    Extracted, the children restate what the parent already said, and the half
    of stage 1 that is about what the user prefers has no user in it at all.
    Chapter 7's `resolve("last")` makes the same exclusion for the same reason.
    """
    sessions = tmp_path / "sessions"
    _write_session(sessions, "parent", ["do the thing"])
    _write_session(sessions, "child", ["sub-task"], parent="parent")
    assert [s for s, _ in pending_sessions(sessions)] == ["parent"]


# ---------------------------------------------------------------------------
# F17-02  background work gives way to foreground work
# ---------------------------------------------------------------------------


def test_F17_02_no_quota_left_means_the_writer_does_not_run(tmp_path: Path) -> None:
    report = asyncio.run(
        run_pipeline(
            _Scripted([]),
            directory=tmp_path / "mem",
            sessions_dir=tmp_path / "sessions",
            jobs_path=tmp_path / "jobs.db",
            headroom=5.0,
        )
    )
    assert report.skipped
    assert "5%" in report.describe()
    assert not (tmp_path / "mem").exists()


def test_F17_02_an_unknown_headroom_is_not_read_as_empty(tmp_path: Path) -> None:
    """`None` means the provider does not report, which ollama does not.

    Treating unknown as zero switches the feature off against every server
    that sends no rate-limit headers; treating it as full is the honest
    default and is written down as a choice rather than left to be inferred.
    """
    report = asyncio.run(
        run_pipeline(
            _Scripted([]),
            directory=tmp_path / "mem",
            sessions_dir=tmp_path / "sessions",
            jobs_path=tmp_path / "jobs.db",
            headroom=None,
        )
    )
    assert not report.skipped


def test_F17_02_the_headers_are_read_off_a_real_response() -> None:
    """Driven through the real `stream()`, not through `from_headers` alone.

    Chapter 6 paid for this distinction: a test that reimplements the parsing
    it is checking stays green when the parsing is deleted.  Mutation testing
    found the same hole here -- `self.rate_limit = None` left every other
    assertion in this file passing, because nothing else in the suite makes
    the client read a response.
    """
    import httpx

    from minicodex.model import ChatCompletionsModel

    body = (
        'data: {"choices":[{"delta":{"content":"ok"},"finish_reason":null}]}\n\n'
        'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}\n\ndata: [DONE]\n\n'
    )

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            text=body,
            headers={
                "content-type": "text/event-stream",
                "x-ratelimit-limit-requests": "10000",
                "x-ratelimit-remaining-requests": "2000",
                "x-ratelimit-limit-tokens": "200000",
                "x-ratelimit-remaining-tokens": "40000",
            },
        )

    model = ChatCompletionsModel(transport=httpx.MockTransport(handler))
    assert model.rate_limit is None

    async def drain() -> None:
        async for _ in model.stream([{"role": "user", "content": "hi"}]):
            pass

    asyncio.run(drain())
    assert model.rate_limit is not None
    assert model.rate_limit.headroom() == pytest.approx(20.0)


def test_F17_02_the_threshold_is_the_tighter_of_the_two_windows() -> None:
    """Requests and tokens are separate buckets; running out of either stops you."""
    limit = RateLimit(
        remaining_requests=9987,
        limit_requests=10000,
        remaining_tokens=20000,
        limit_tokens=200000,
    )
    assert limit.headroom() == pytest.approx(10.0)
    assert limit.headroom() < MIN_RATE_LIMIT_REMAINING_PERCENT

    # A real header set, copied from `probe_memory_write.py quota`.
    healthy = RateLimit.from_headers(
        {
            "x-ratelimit-limit-requests": "10000",
            "x-ratelimit-limit-tokens": "200000",
            "x-ratelimit-remaining-requests": "9987",
            "x-ratelimit-remaining-tokens": "199996",
        }
    )
    assert healthy is not None
    assert healthy.headroom() == pytest.approx(99.87, abs=0.01)

    assert RateLimit.from_headers({"content-type": "application/json"}) is None


# ---------------------------------------------------------------------------
# F17-03  claim, lease, back off
# ---------------------------------------------------------------------------


def test_F17_03_a_claimed_job_is_not_claimed_twice(tmp_path: Path) -> None:
    store = JobStore(tmp_path / "jobs.db")
    store.enrol([("a", Path("a.jsonl")), ("b", Path("b.jsonl"))])
    first = store.claim(limit=1, now=100.0)
    second = store.claim(limit=1, now=100.0)
    assert [j.session_id for j in first] == ["a"]
    assert [j.session_id for j in second] == ["b"]
    assert store.claim(limit=5, now=100.0) == ()
    store.close()


def test_F17_03_an_expired_lease_is_taken_back(tmp_path: Path) -> None:
    """The only reason a lease exists rather than a flag.

    The process that claimed the job may have been killed -- or, in this
    program, simply cancelled when the foreground run finished first -- and a
    killed process releases nothing.
    """
    store = JobStore(tmp_path / "jobs.db")
    store.enrol([("a", Path("a.jsonl"))])
    store.claim(now=100.0, lease=60.0)
    assert store.claim(now=150.0, lease=60.0) == ()
    retaken = store.claim(now=200.0, lease=60.0)
    assert [j.session_id for j in retaken] == ["a"]
    assert retaken[0].attempts == 2
    store.close()


def test_F17_03_failure_backs_off_and_then_gives_up(tmp_path: Path) -> None:
    """Three attempts, doubling, and then the job stops coming back.

    A rollout that fails extraction three times is not going to start working:
    the causes are properties of the file, not of the moment.  Coming back
    forever is how a background job becomes a background cost.
    """
    store = JobStore(tmp_path / "jobs.db")
    store.enrol([("a", Path("a.jsonl"))])
    now = 100.0
    for attempt in range(1, MAX_ATTEMPTS):
        assert store.claim(now=now) != (), attempt
        store.fail("a", "boom", now=now)
        assert store.state_of("a") == "pending"
        # Not available until the backoff has passed...
        assert store.claim(now=now + 1) == ()
        now += BACKOFF_BASE_SECONDS * (2 ** (attempt - 1)) + 1
    assert store.claim(now=now) != ()
    store.fail("a", "boom", now=now)
    assert store.state_of("a") == "failed"
    assert store.claim(now=now + 10 * DAY) == ()
    store.close()


def test_F17_03_the_attempt_is_counted_when_it_is_claimed(tmp_path: Path) -> None:
    """Counted on claim rather than on failure, and that is the load-bearing bit.

    A process that dies between claiming and failing never calls `fail`.  If
    the count lived there, a rollout that crashes the extractor would be
    claimed forever by whoever starts next.
    """
    store = JobStore(tmp_path / "jobs.db")
    store.enrol([("a", Path("a.jsonl"))])
    job = store.claim(now=100.0)[0]
    assert job.attempts == 1
    assert store.rows()[0]["attempts"] == 1
    store.close()


def test_F17_03_the_merge_lock_names_the_process_holding_it(tmp_path: Path) -> None:
    """Chapter 7's lock, second implementation, same refusal.

    The message has to name the pid: a stale lock is otherwise a program that
    stopped working with no way to find out why.
    """
    lock = tmp_path / "merge.lock"
    write_memory(tmp_path / "mem", summary="- a", body="## A\n\nb\n", lock_path=lock)
    assert not lock.exists(), "the lock is released when the write finishes"

    lock.write_text("4242", encoding="utf-8")
    with pytest.raises(MemoryWriteError) as caught:
        write_memory(tmp_path / "mem", summary="- a", body="## A\n\nb\n", lock_path=lock)
    assert "4242" in str(caught.value)
    assert "Nothing was written" in str(caught.value)


# ---------------------------------------------------------------------------
# F17-04  a format with slots in it is a format a model fills
# ---------------------------------------------------------------------------


def test_F17_04_the_shipped_prompt_asks_for_a_verdict_before_it_shows_a_shape() -> None:
    """A snapshot, chapter 3's F03-10 shape, and it is pinning a measurement.

    The version without these two clauses -- `STAGE1_QUOTA`, still in the
    module -- produced three bullets for *every* session it was shown,
    including one whose entire content was "which functions does calc.py
    define": 0/3 no-ops where the answer should have been nothing at all.  The
    shipped wording answered `DURABLE: no` 3/3 on the same session.

    Both clauses are load-bearing and both look like padding:
    """
    assert "DURABLE: yes" in STAGE1_SYSTEM
    assert "DURABLE: no" in STAGE1_SYSTEM
    assert "thirty seconds" in STAGE1_SYSTEM
    assert "There is no minimum" in STAGE1_SYSTEM
    # ...and the thing that had to go: a quota is filled.
    assert "at most three bullets" in STAGE1_QUOTA
    assert "at most three bullets" not in STAGE1_SYSTEM


def test_F17_04_a_no_verdict_empties_the_answer() -> None:
    """The judgement is enforced, not trusted.

    A model that answers `DURABLE: no` and then fills the sections anyway is
    the shape talking, and the shape does not get a vote.
    """
    answer = (
        "DURABLE: no\n\n## Preference signals\n- The user likes tests.\n"
        "## Reusable knowledge\n- calc.py has two functions.\n"
    )
    assert not parse_stage1(answer)
    assert parse_stage1(answer.replace("no", "yes")).bullets()


def test_F17_04_nothing_to_keep_is_a_success_not_a_failure(tmp_path: Path) -> None:
    """An empty extraction finishes the job and writes no file.

    Writing an empty raw file instead would make every merge read a page of
    empty headings, and re-queueing the session would mean paying for the same
    negative answer forever.
    """
    sessions = tmp_path / "sessions"
    _write_session(sessions, "s1", ["what does calc.py define?"])
    jobs = tmp_path / "jobs.db"
    report = asyncio.run(
        run_pipeline(
            _Scripted(["DURABLE: no"]),
            directory=tmp_path / "mem",
            sessions_dir=sessions,
            jobs_path=jobs,
            lock_path=tmp_path / "merge.lock",
        )
    )
    assert report.sessions == ("s1",)
    assert report.empty == ("s1",)
    assert not report.merged
    assert pending_raw(tmp_path / "mem") == ()
    with JobStore(jobs) as store:
        assert store.state_of("s1") == "done"


def test_F17_04_a_placeholder_bullet_is_not_a_memory() -> None:
    """`- ...` is in the prompt's example, and one sample in three copied it.

    An entry whose whole text is an ellipsis survives for a year, because
    nobody can tell what it was meant to say.
    """
    answer = "DURABLE: yes\n## Preference signals\n- ...\n- none\n- Use uv.\n"
    assert parse_stage1(answer).preferences == ("Use uv.",)


def test_F17_04_material_outside_a_known_heading_is_dropped() -> None:
    answer = (
        "Here is what I found.\n- a stray bullet\n"
        "## Something else\n- not one of ours\n"
        "## Reusable knowledge\n- Releases are cut with `make ship`.\n"
    )
    out = parse_stage1(answer)
    assert out.knowledge == ("Releases are cut with `make ship`.",)
    assert out.preferences == ()


def test_F17_04_the_raw_file_says_it_is_temporary(tmp_path: Path) -> None:
    """codex writes the same warning into the same file, and it is not tidiness.

    An intermediate product that does not say it is intermediate is one
    somebody starts maintaining.
    """
    path = write_raw(tmp_path, Stage1(session_id="s1", preferences=("Use uv.",)))
    text = path.read_text(encoding="utf-8")
    assert text.splitlines()[0] == FORMAT_VERSION
    assert "Temporary file" in text
    assert "Input for stage 2" in text
    assert path.parent.name == RAW_DIR


# ---------------------------------------------------------------------------
# F17-05  redaction, on the way in and on the way out
# ---------------------------------------------------------------------------


def test_F17_05_a_key_is_removed_from_the_transcript_before_it_is_sent(tmp_path: Path) -> None:
    sessions = tmp_path / "sessions"
    _write_session(sessions, "s1", ["the key is sk-live-9f4c2ab7d3e1f0aa88b2, do not lose it"])
    rollout = read_rollout(sessions / "s1.jsonl")
    text, removed = transcript_of(rollout)
    assert "sk-live-9f4c2ab7d3e1f0aa88b2" not in text
    assert REDACTED in text
    assert removed == 1


def test_F17_05_the_models_own_answer_is_scrubbed_too(tmp_path: Path) -> None:
    """The half that is easy to leave out, and the half that was measured.

    Given a transcript in which the user *asks* for a key to be remembered,
    gpt-4o-mini reproduced it in its extraction output 3 times out of 3.  A
    scrub on the way in cannot help with that: the model was asked to write
    the key down and it did.  Only the outbound pass stops it reaching a file
    that is injected into every future request.
    """
    sessions = tmp_path / "sessions"
    _write_session(sessions, "s1", ["hello"])
    rollout = read_rollout(sessions / "s1.jsonl")
    leak = "DURABLE: yes\n## Reusable knowledge\n- The deploy key is sk-live-aaaabbbbccccdddd.\n"
    out = asyncio.run(extract(_Scripted([leak]), rollout))
    assert "sk-live-aaaabbbbccccdddd" not in "".join(out.bullets())
    assert REDACTED in "".join(out.bullets())
    assert out.secrets_removed == 1


def test_F17_05_an_assignment_is_caught_by_its_name_not_only_its_shape() -> None:
    """`env` output and a `.env` file are the two ways this arrives."""
    text, hits = scrub("DATABASE_PASSWORD=hunter2hunter2\nGITHUB_TOKEN: ghp_abcdefghijklmnopqrst")
    assert "hunter2hunter2" not in text
    assert "ghp_abcdefghijklmnopqrst" not in text
    assert hits == 2


def test_F17_05_the_count_is_of_secrets_not_of_regexes() -> None:
    """One key matched by two patterns was reported as two.

    A number about the pattern list rather than about the transcript -- the
    same shape as every measurement-tool fault in this book, in the shipped
    code this time.
    """
    _, hits = scrub("STRIPE_API_KEY=sk-live-9f4c2ab7d3e1f0aa88b2")
    assert hits == 1


def test_F17_05_the_merge_answer_is_scrubbed_before_it_is_parsed() -> None:
    merged = parse_merged(
        f"{SUMMARY_MARK}\n- Deploy with token ghp_zzzzyyyyxxxxwwwwvvvv.\n{BODY_MARK}\n## A\n\nb\n"
    )
    assert "ghp_zzzzyyyyxxxxwwwwvvvv" not in merged.summary
    assert merged.secrets_removed == 1


# ---------------------------------------------------------------------------
# F17-06 / F17-07  forgetting
# ---------------------------------------------------------------------------


def _entry(slug: str, title: str) -> Entry:
    return Entry(f"{BODY_FILE}#{slug}", title, f"The rule about {title}.", (1, 2))


def test_F17_07_the_old_and_used_entry_outlives_the_new_and_unused_one() -> None:
    """The listed fix for F17-06 is a window of unused days; taken literally it
    is F17-07.  Nine months old and consulted every week is exactly what an
    age rule deletes first.
    """
    now = time.time()
    entries = (_entry("deploy", "Deploy"), _entry("colours", "Colours"))
    counts = {
        f"{BODY_FILE}#deploy": {
            "count": 34,
            "last_used": now - 3 * DAY,
            "first_seen": now - 270 * DAY,
        },
        f"{BODY_FILE}#colours": {"count": 0, "last_used": 0, "first_seen": now - 60 * DAY},
    }
    result = prune(entries, counts, now=now, max_entries=1)
    assert [e.title for e in result.kept] == ["Deploy"]
    assert [e.title for e, _ in result.dropped] == ["Colours"]


def test_F17_06_an_uncited_entry_leaves_only_after_the_window() -> None:
    now = time.time()
    entries = (_entry("new", "New"), _entry("old", "Old"))
    counts = {
        f"{BODY_FILE}#new": {"count": 0, "first_seen": now - 1 * DAY},
        f"{BODY_FILE}#old": {"count": 0, "first_seen": now - 60 * DAY},
    }
    result = prune(entries, counts, now=now)
    assert [e.title for e in result.kept] == ["New"]
    assert "never cited in 60 days" in result.dropped[0][1]


def test_F17_07_zero_citations_alone_never_drops_anything() -> None:
    """Chapter 16 measured the bias this compensates for.

    A model reports the memory that shaped its *answer* and not the memory
    that shaped its *actions*: on the `runner` task it ran `python -m pytest`
    -- a string only memory knew -- and then wrote `none`, 3/3.  So a zero in
    the citation column is not evidence of an unused entry, and it takes zero
    citations *and* a month *and* the cap being over.
    """
    now = time.time()
    entries = tuple(_entry(f"e{i}", f"E{i}") for i in range(5))
    counts = {e.entry_id: {"count": 0, "first_seen": now - 1 * DAY} for e in entries}
    assert prune(entries, counts, now=now).dropped == ()


def test_F17_06_an_entry_with_no_record_at_all_is_new_not_ancient() -> None:
    """The first run against a memory written before the counter existed.

    The other way round deletes all of it.
    """
    now = time.time()
    entries = (_entry("a", "A"),)
    assert prune(entries, {}, now=now).dropped == ()


def test_F17_06_the_cap_is_a_cap(tmp_path: Path) -> None:
    now = time.time()
    entries = tuple(_entry(f"e{i}", f"E{i}") for i in range(MAX_ENTRIES + 5))
    counts = {e.entry_id: {"count": 0, "first_seen": now} for e in entries}
    result = prune(entries, counts, now=now)
    assert len(result.kept) == MAX_ENTRIES
    assert all("cap" in why for _, why in result.dropped)


def test_F17_07_the_ranking_decides_what_survives_not_what_order_it_is_in() -> None:
    """Sorted back into file order before writing.

    Re-ordering the file on every merge makes the git diff -- the mechanism
    F17-08 depends on -- unreadable, and a diff nobody can read is a diff
    nobody uses.
    """
    now = time.time()
    entries = (_entry("a", "A"), _entry("b", "B"), _entry("c", "C"))
    counts = {
        f"{BODY_FILE}#a": {"count": 1, "first_seen": now},
        f"{BODY_FILE}#b": {"count": 9, "first_seen": now},
        f"{BODY_FILE}#c": {"count": 5, "first_seen": now},
    }
    assert [e.title for e in prune(entries, counts, now=now).kept] == ["A", "B", "C"]


def test_F17_06_forgetting_an_entry_forgets_its_usage_row_too(tmp_path: Path) -> None:
    """Otherwise a heading reused a year later inherits somebody else's count.

    `record_uses` takes `CitedEntry` values rather than bare ids since chapter
    16 adopted codex's real citation format (F16-13): a citation now names a
    line range, and only the ones that resolve to a real entry count.
    """
    now = time.time()
    gone = _entry("gone", "Gone")
    record_uses(
        tmp_path,
        (CitedEntry(path=BODY_FILE, line_start=1, line_end=2, note="why", entry=gone),),
        now=now - 90 * DAY,
    )
    raw = json.loads((tmp_path / USAGE_FILE).read_text(encoding="utf-8"))
    raw[f"{BODY_FILE}#gone"]["first_seen"] = now - 90 * DAY
    (tmp_path / USAGE_FILE).write_text(json.dumps(raw), encoding="utf-8")

    write_memory(
        tmp_path,
        summary="- keep",
        body="## Keep\n\nthe only survivor\n",
        now=now,
        lock_path=tmp_path / "merge.lock",
    )
    after = json.loads((tmp_path / USAGE_FILE).read_text(encoding="utf-8"))
    assert f"{BODY_FILE}#gone" not in after
    assert after[f"{BODY_FILE}#keep"]["first_seen"] == now


def test_F17_06_the_written_summary_is_inside_chapter_sixteens_cap(tmp_path: Path) -> None:
    """Through `write_memory`, not through `trim_summary`.

    The direct test below was green with the call to it deleted: it tested the
    function, and what has to hold is that **nothing this program writes needs
    the read path to truncate it**.  A merge that produces forty bullets is not
    an error, it is an over-long resident block, and the place that catches it
    is the writer.

    1500 bullets, not the 200 this was written with.  Chapter 16's rewrite took
    the budget from a number this book invented (400) to codex's real one
    (2500, `ext/memories/src/lib.rs:16`), and 200 bullets now fit -- so the
    fixture stopped reaching the bound, and both mutations of the
    `trim_summary` call it exists to catch survived.  Caught by
    `probe_mutations_ch17.py`, which is the second time in two chapters that a
    changed constant quietly retired a test rather than breaking it.
    """
    from minicodex.memory import resident_block
    from minicodex.tokens import estimate_messages

    write_memory(
        tmp_path,
        summary="\n".join(f"- rule number {i} about something or other" for i in range(1500)),
        body="## A\n\nb\n",
        lock_path=tmp_path / "merge.lock",
    )
    block = resident_block(load(tmp_path)) or ""
    assert "[... memory truncated" not in block, "the read path had to truncate what we wrote"
    written = (tmp_path / SUMMARY_FILE).read_text(encoding="utf-8")
    assert estimate_messages([{"role": "user", "content": written}]) <= SUMMARY_TOKEN_BUDGET


def test_F17_06_the_resident_half_is_trimmed_by_code_not_by_request() -> None:
    """The merge prompt asks for at most eight bullets.  A prompt is a request.

    The resident block is sent with every request forever, so chapter 16's cap
    is enforced on the way in as well as on the way out.

    The fixture is 1500 bullets rather than the 200 it was written with, and
    that is chapter 16's rewrite arriving here: the budget went from a number
    this book invented (400) to codex's real one (2500,
    `ext/memories/src/lib.rs:16`), and 200 bullets now fit inside it.  A test
    whose input no longer reaches the bound it is testing passes for the wrong
    reason -- it did, until this line changed.
    """
    long_summary = "\n".join(f"- rule number {i} about something" for i in range(1500))
    trimmed = trim_summary(long_summary)
    assert len(trimmed) < len(long_summary)
    from minicodex.tokens import estimate_messages

    assert estimate_messages([{"role": "user", "content": trimmed}]) <= SUMMARY_TOKEN_BUDGET
    # ...and the same call, told what else is going into the block, cuts more.
    # One ruler: the question asked is the read path's own (F06-12 again).
    entries = tuple(_entry(f"e{i}", f"Section {i}") for i in range(30))
    assert len(trim_summary(long_summary, entries)) < len(trimmed)


# ---------------------------------------------------------------------------
# F17-08  the user's own edits
# ---------------------------------------------------------------------------


@needs_git
def test_F17_08_a_hand_edit_shows_up_as_a_diff(tmp_path: Path) -> None:
    """What the merge is shown about what a person did.

    Everything uncommitted is by definition not this program's work: it writes
    and commits in one operation.
    """
    directory = tmp_path / "mem"
    assert ensure_repo(directory)
    write_memory(
        directory, summary="- a", body="## A\n\noriginal\n", lock_path=tmp_path / "merge.lock"
    )
    assert commit(directory, "baseline")
    assert hand_edits(directory) == ""

    (directory / BODY_FILE).write_text(
        f"{FORMAT_VERSION}\n\n## A\n\noriginal\nand a line a person added\n", encoding="utf-8"
    )
    diff = hand_edits(directory)
    assert "+and a line a person added" in diff


@needs_git
def test_F17_08_a_deletion_is_only_visible_in_the_diff(tmp_path: Path) -> None:
    """The measured reason the git repository is not ceremony.

    An *addition* survives a merge whether or not there is a diff, because the
    edited file is what the merge is shown.  A **deletion** is not in the file
    -- that is what deleting means -- so the diff is the only evidence it ever
    happened.  Measured against gpt-4o-mini: with the diff, the deleted
    section stayed deleted 3/3; without it, the merge put it back 3/3.
    """
    directory = tmp_path / "mem"
    ensure_repo(directory)
    write_memory(
        directory,
        summary="- a",
        body="## Keep\n\nkept\n\n## Drop\n\nthe user does not want this\n",
        lock_path=tmp_path / "merge.lock",
    )
    commit(directory, "baseline")
    (directory / BODY_FILE).write_text(f"{FORMAT_VERSION}\n\n## Keep\n\nkept\n", encoding="utf-8")

    diff = hand_edits(directory)
    assert "-the user does not want this" in diff
    request = merge_request(memory=load(directory), raw=(), notes=(), hand_edits=diff)
    assert "the user does not want this" in request
    assert "These are the user's own words" in request
    # ...and with no diff, nothing anywhere in the request mentions it.
    assert "the user does not want this" not in merge_request(
        memory=load(directory), raw=(), notes=(), hand_edits=""
    )


@needs_git
def test_F17_08_the_writer_commits_what_it_wrote(tmp_path: Path) -> None:
    directory = tmp_path / "mem"
    ensure_repo(directory)
    write_memory(directory, summary="- a", body="## A\n\nb\n", lock_path=tmp_path / "merge.lock")
    assert commit(directory, "first")
    log = subprocess.run(
        ["git", "-C", str(directory), "log", "--oneline"],
        capture_output=True,
        text=True,
        check=True,
    )
    assert "first" in log.stdout


def test_F17_08_no_git_is_a_degradation_not_a_failure(tmp_path: Path) -> None:
    """A memory directory that is not a repository still merges."""
    directory = tmp_path / "mem"
    directory.mkdir()
    assert hand_edits(directory) == ""
    assert not commit(directory, "nothing to commit")


def test_F17_08_the_jobs_database_is_not_in_the_repository() -> None:
    """A binary rewritten on every operation, in the one directory whose whole
    point is that its diffs are readable."""
    from minicodex.memory import DEFAULT_MEMORY_DIR, MINICODEX_HOME
    from minicodex.memory_jobs import DEFAULT_JOBS_PATH

    assert DEFAULT_MEMORY_DIR not in DEFAULT_JOBS_PATH.parents
    assert DEFAULT_JOBS_PATH.parent == MINICODEX_HOME


# ---------------------------------------------------------------------------
# F17-09  the model proposes; one writer disposes
# ---------------------------------------------------------------------------


def test_F17_09_the_note_tool_cannot_touch_the_memory_files(tmp_path: Path) -> None:
    """The whole of F17-09 in one assertion.

    `remember_this` is the only write access a model has anywhere in this
    program, and what it writes is a file in `notes/` that nothing reads as
    memory.  Letting it edit `MEMORY.md` instead is chapter 4's F04-02 with no
    reviewer: a model asked to add one line rewrites the file and silently
    drops the parts it did not re-emit.
    """
    write_memory(tmp_path, summary="- a", body="## A\n\nb\n", lock_path=tmp_path / "merge.lock")
    before = (tmp_path / BODY_FILE).read_text(encoding="utf-8")
    tools = note_toolset(tmp_path)
    answer = asyncio.run(tools.handlers[NOTE_NAME]({"note": "The linter is `ruff check --fix`."}))

    assert "Proposed" in answer
    assert (tmp_path / BODY_FILE).read_text(encoding="utf-8") == before
    assert (tmp_path / SUMMARY_FILE).exists()
    notes = pending_notes(tmp_path)
    assert len(notes) == 1
    assert notes[0][1] == "The linter is `ruff check --fix`."
    assert notes[0][0].parent.name == NOTES_DIR


def test_F17_09_a_note_never_overwrites_another_note(tmp_path: Path) -> None:
    """Append-only, applied to a directory instead of to a file (chapter 7)."""
    first = propose(tmp_path, "one", now=1000.0)
    second = propose(tmp_path, "two", now=1000.0)
    assert first != second
    assert {p.read_text(encoding="utf-8").strip() for p, _ in pending_notes(tmp_path)} == {
        "one",
        "two",
    }


def test_F17_09_a_note_is_scrubbed_and_bounded(tmp_path: Path) -> None:
    tools = note_toolset(tmp_path)
    asyncio.run(tools.handlers[NOTE_NAME]({"note": "token ghp_abcdefghijklmnopqrst " + "x" * 900}))
    text = pending_notes(tmp_path)[0][1]
    assert "ghp_abcdefghijklmnopqrst" not in text
    assert len(text) <= 400


def test_F17_09_one_session_may_not_propose_fifty_notes(tmp_path: Path) -> None:
    tools = note_toolset(tmp_path, limit=2)
    for _ in range(2):
        asyncio.run(tools.handlers[NOTE_NAME]({"note": "a rule"}))
    refusal = asyncio.run(tools.handlers[NOTE_NAME]({"note": "a rule"}))
    assert "limit" in refusal
    assert len(pending_notes(tmp_path)) == 2


def test_F17_09_a_bad_argument_is_chapter_threes_three_part_error(tmp_path: Path) -> None:
    tools = note_toolset(tmp_path)
    answer = asyncio.run(tools.handlers[NOTE_NAME]({"note": ""}))
    assert "you sent" in answer.lower()
    assert "Example:" in answer
    assert pending_notes(tmp_path) == ()


def test_F17_09_the_writer_refuses_to_replace_a_memory_with_nothing(tmp_path: Path) -> None:
    """Chapter 6's empty-summary rule (F06-08) on a file that outlives the run.

    An empty merge answer is not a smaller memory, it is a destroyed one, and
    validation happens before anything is opened for writing -- chapter 4's
    two-phase apply at a different scale.
    """
    write_memory(tmp_path, summary="- a", body="## A\n\nb\n", lock_path=tmp_path / "merge.lock")
    with pytest.raises(MemoryWriteError):
        write_memory(tmp_path, summary="  ", body="", lock_path=tmp_path / "merge.lock")
    assert "## A" in (tmp_path / BODY_FILE).read_text(encoding="utf-8")


def test_F17_09_a_merge_answer_without_its_markers_is_refused() -> None:
    """The tempting fallback -- treat the whole thing as the body -- writes the
    model's commentary into a file injected into every future request."""
    with pytest.raises(MemoryWriteError) as caught:
        parse_merged("Sure! Here is your updated memory:\n\n## Tests\n\nRun pytest.\n")
    assert "markers" in str(caught.value)


def test_F17_09_what_is_written_is_readable_by_chapter_sixteen(tmp_path: Path) -> None:
    """The two halves of the subsystem meet at one file format, so the write
    path's output is checked with the read path's parser."""
    write_memory(
        tmp_path,
        summary="- Run tests with `python -m pytest`.",
        body="## Running tests\n\nUse `python -m pytest`.\n\n## Layout\n\nHelpers live in util/.\n",
        lock_path=tmp_path / "merge.lock",
    )
    memory = load(tmp_path)
    assert memory.summary == "- Run tests with `python -m pytest`."
    assert [e.title for e in memory.entries] == ["Running tests", "Layout"]
    assert (tmp_path / SUMMARY_FILE).read_text(encoding="utf-8").startswith(FORMAT_VERSION)
    assert (tmp_path / BODY_FILE).read_text(encoding="utf-8").startswith(FORMAT_VERSION)


def test_F17_09_the_version_line_is_the_programs_claim_not_the_models() -> None:
    """The model is never asked to emit `v1`, so it can never get it wrong."""
    assert FORMAT_VERSION not in STAGE2_SYSTEM
    assert SUMMARY_MARK in STAGE2_SYSTEM
    assert BODY_MARK in STAGE2_SYSTEM


# ---------------------------------------------------------------------------
# F17-10  the writer does not read its own output
# ---------------------------------------------------------------------------


def test_F17_10_a_note_call_is_not_part_of_the_transcript(tmp_path: Path) -> None:
    """The one place this program can reach F17-10's shape.

    A `remember_this` call is already an input to stage 2 through `notes/`.
    Leaving it in the transcript delivers the same sentence twice -- measured,
    3/3 extractions of a session that proposed a note re-derived the note as a
    bullet of its own.
    """
    sessions = tmp_path / "sessions"
    _write_session(
        sessions,
        "s1",
        ["remember: the linter is ruff"],
        calls=[(NOTE_NAME, '{"note": "The linter is ruff."}'), ("read_file", '{"path": "a.py"}')],
    )
    text, _ = transcript_of(read_rollout(sessions / "s1.jsonl"))
    assert NOTE_NAME not in text
    assert "read_file" in text
    assert "remember: the linter is ruff" in text


def test_F17_10_system_and_developer_notes_are_not_extracted_from(tmp_path: Path) -> None:
    """The program's own words are not the session's content.

    An extractor shown the permissions block, the turn-budget warning or -- the
    interesting one -- the *memory block itself* reports them back as things
    worth remembering, which is a loop with no fixed point and no error
    message.
    """
    sessions = tmp_path / "sessions"
    _write_session(
        sessions,
        "s1",
        ["hello"],
        notes=["You have 2 turns left.", "<memory>\n- Always use uv.\n</memory>"],
    )
    text, _ = transcript_of(read_rollout(sessions / "s1.jsonl"))
    assert "2 turns left" not in text
    assert "Always use uv" not in text
    assert "hello" in text


# ---------------------------------------------------------------------------
# F17-11  nobody has agreed to be remembered
# ---------------------------------------------------------------------------


def test_F17_11_writing_is_off_unless_asked_for(tmp_path: Path) -> None:
    """Two switches, not one: reading what you wrote and letting a model write
    about you are different consents."""
    tools = top_level_tools(tmp_path, Session(approver=AllowAll()), _sub_context(tmp_path))
    assert NOTE_NAME not in tools.handlers
    with_writing = top_level_tools(
        tmp_path, Session(approver=AllowAll()), _sub_context(tmp_path), remember=tmp_path / "mem"
    )
    assert NOTE_NAME in with_writing.handlers


def test_F17_11_the_prompt_describes_the_tools_that_exist(tmp_path: Path) -> None:
    """Chapter 5's F05-10: a prompt that names a tool the configuration does
    not have gets the model to call it, measured at 2/3."""
    from minicodex.__main__ import _instructions

    session = Session(approver=AllowAll())
    without = top_level_tools(tmp_path, session, _sub_context(tmp_path))
    with_writing = top_level_tools(
        tmp_path, session, _sub_context(tmp_path), remember=tmp_path / "mem"
    )
    assert NOTE_NAME not in _instructions(session, without)
    assert NOTE_NAME in _instructions(session, with_writing)


def test_F17_11_forget_all_deletes_the_extracted_material_too(tmp_path: Path) -> None:
    """ "Delete everything" that leaves `raw/` behind has not deleted everything.

    Stage-1 output is memory in all but name -- it is the user's preferences,
    extracted -- and the job records are what would make a regenerated memory
    come back empty.
    """
    from minicodex.__main__ import _memory

    directory = tmp_path / "mem"
    jobs = tmp_path / "jobs.db"
    write_memory(directory, summary="- a", body="## A\n\nb\n", lock_path=tmp_path / "merge.lock")
    write_raw(directory, Stage1(session_id="s1", preferences=("Use uv.",)))
    propose(directory, "a proposal")
    with JobStore(jobs) as store:
        store.enrol([("s1", Path("s1.jsonl"))])

    assert _memory(directory, forget_all=True, jobs_path=jobs) == 0
    assert not (directory / SUMMARY_FILE).exists()
    assert not (directory / BODY_FILE).exists()
    assert not (directory / USAGE_FILE).exists()
    assert pending_raw(directory) == ()
    assert pending_notes(directory) == ()
    with JobStore(jobs) as store:
        assert store.rows() == []


# ---------------------------------------------------------------------------
# the pipeline, end to end, with a scripted model
# ---------------------------------------------------------------------------


def test_the_pipeline_extracts_merges_prunes_and_cleans_up(tmp_path: Path) -> None:
    sessions = tmp_path / "sessions"
    _write_session(sessions, "s1", ["always use uv"])
    directory = tmp_path / "mem"
    propose(directory, "The linter is `ruff check --fix`.")

    model = _Scripted(
        [
            "DURABLE: yes\n## Preference signals\n- This project is run with `uv`.\n",
            f"{SUMMARY_MARK}\n- Use uv.\n{BODY_MARK}\n## Tooling\n\nRun everything with `uv`.\n",
        ]
    )
    report = asyncio.run(
        run_pipeline(
            model,
            directory=directory,
            sessions_dir=sessions,
            jobs_path=tmp_path / "jobs.db",
            lock_path=tmp_path / "merge.lock",
        )
    )
    assert report.merged
    assert report.sessions == ("s1",)
    memory = load(directory)
    assert [e.title for e in memory.entries] == ["Tooling"]
    # Consumed only after the write succeeded.
    assert pending_raw(directory) == ()
    assert pending_notes(directory) == ()
    # ...and the merge was shown both inputs.
    assert "ruff check --fix" in model.requests[1][1]["content"]
    assert "This project is run with `uv`." in model.requests[1][1]["content"]


def test_the_raw_file_survives_a_merge_that_fails(tmp_path: Path) -> None:
    """Extraction is the expensive half, so it is never thrown away for free.

    The other order -- delete the raw files, then merge -- loses a model call
    per session on any failure in the second stage.
    """
    sessions = tmp_path / "sessions"
    _write_session(sessions, "s1", ["always use uv"])
    directory = tmp_path / "mem"
    model = _Scripted(
        [
            "DURABLE: yes\n## Preference signals\n- This project is run with `uv`.\n",
            "I am afraid I cannot do that.",
        ]
    )
    with pytest.raises(MemoryWriteError):
        asyncio.run(
            run_pipeline(
                model,
                directory=directory,
                sessions_dir=sessions,
                jobs_path=tmp_path / "jobs.db",
                lock_path=tmp_path / "merge.lock",
            )
        )
    assert len(pending_raw(directory)) == 1


def test_one_unreadable_session_does_not_stop_the_other(tmp_path: Path) -> None:
    """Chapter 0's rule in a place with no model to hand the error to."""
    sessions = tmp_path / "sessions"
    _write_session(sessions, "good", ["always use uv"])
    (sessions / "bad.jsonl").write_text(
        json.dumps({"type": "meta", "session_id": "bad"}) + "\n" + '{"type": "user"}\n',
        encoding="utf-8",
    )
    jobs = tmp_path / "jobs.db"
    model = _Scripted(
        [
            "DURABLE: yes\n## Preference signals\n- This project is run with `uv`.\n",
            f"{SUMMARY_MARK}\n- Use uv.\n{BODY_MARK}\n## Tooling\n\nRun everything with `uv`.\n",
        ]
    )
    report = asyncio.run(
        run_pipeline(
            model,
            directory=tmp_path / "mem",
            sessions_dir=sessions,
            jobs_path=jobs,
            lock_path=tmp_path / "merge.lock",
        )
    )
    # The unreadable one is not even eligible: `read_rollout` refuses it during
    # the scan, so it never becomes a job at all.
    assert report.sessions == ("good",)
    assert report.merged
    with JobStore(jobs) as store:
        assert store.state_of("bad") is None


def test_a_stream_that_ends_early_is_not_half_a_memory(tmp_path: Path) -> None:
    """Chapter 0's `[DONE]` rule, third application.

    Half an answer here is written to a file and read back forever.
    """
    sessions = tmp_path / "sessions"
    _write_session(sessions, "s1", ["hello"])
    with pytest.raises(MemoryWriteError, match=r"\[DONE\]"):
        asyncio.run(
            extract(_Scripted(["half an ans"], finish=False), read_rollout(sessions / "s1.jsonl"))
        )


# ---------------------------------------------------------------------------
# F17-12  the memory root went global, and the write path had to follow
# ---------------------------------------------------------------------------


def test_F17_12_everything_the_write_path_owns_is_under_one_home() -> None:
    """The lock and the jobs database moved with the memory directory.

    Chapter 16 moved memory out of the repository and under the user's home
    (F16-10), matching codex's `memory_root()` =
    `codex_home.join("memories")` (`memories/write/src/lib.rs:116-117`).  The
    two things this chapter owns had to move with it, and the reason is not
    tidiness: they are *about* that one global directory, so leaving them
    per-repository would mean one lock per checkout guarding one shared file.
    """
    from minicodex.memory import DEFAULT_MEMORY_DIR, MINICODEX_HOME
    from minicodex.memory_jobs import DEFAULT_JOBS_PATH
    from minicodex.memory_write import MERGE_LOCK

    assert DEFAULT_MEMORY_DIR.parent == MINICODEX_HOME
    assert MERGE_LOCK.parent == MINICODEX_HOME
    assert DEFAULT_JOBS_PATH.parent == MINICODEX_HOME
    # Beside the memory directory, never inside it: it is a git repository and
    # a binary rewritten on every operation has no readable diff.
    assert DEFAULT_MEMORY_DIR not in MERGE_LOCK.parents
    assert DEFAULT_MEMORY_DIR not in DEFAULT_JOBS_PATH.parents


def test_F17_12_nothing_defaults_into_the_working_directory() -> None:
    """The specific regression: a relative default is read against the cwd.

    Before the move these were `Path(".minicodex") / ...`, so running the
    program from two checkouts gave two locks and two job tables -- each
    believing it was the only writer of the one memory they now share.  An
    absolute default is what makes "the single writer" true across checkouts
    rather than only within one.
    """
    from minicodex.memory import DEFAULT_MEMORY_DIR
    from minicodex.memory_jobs import DEFAULT_JOBS_PATH
    from minicodex.memory_write import MERGE_LOCK

    for path in (DEFAULT_MEMORY_DIR, MERGE_LOCK, DEFAULT_JOBS_PATH):
        assert path.is_absolute(), f"{path} is relative and would follow the cwd"


def test_F17_12_a_stale_project_local_memory_is_not_read(tmp_path: Path) -> None:
    """A leftover `.minicodex/memories` from before the move is not memory.

    The failure this guards is silent: the old directory is still on disk in
    every repository that ran the previous version, and a loader that fell
    back to it would serve a year-old memory to a program that thinks it is
    reading the new one.  Nothing falls back -- `load` reads the directory it
    is given, and the default is absolute.
    """
    from minicodex.memory import load as load_memory

    stale = tmp_path / ".minicodex" / "memories"
    stale.mkdir(parents=True)
    (stale / SUMMARY_FILE).write_text(f"{FORMAT_VERSION}\n\n- old and wrong\n", encoding="utf-8")

    fresh = tmp_path / "home" / ".minicodex" / "memories"
    fresh.mkdir(parents=True)
    (fresh / SUMMARY_FILE).write_text(f"{FORMAT_VERSION}\n\n- current\n", encoding="utf-8")

    assert "current" in load_memory(fresh).summary
    assert "old and wrong" not in load_memory(fresh).summary


# ---------------------------------------------------------------------------
# F17-13  the signal forgetting ranks on is one chapter 16 measured as unreliable
# ---------------------------------------------------------------------------


def test_F17_13_zero_citations_alone_never_drops_an_entry() -> None:
    """The conservatism that was belt-and-braces is now load-bearing.

    Chapter 16 measured its own citation format resolving **0 of 9** times: the
    model supplies plausible line numbers it never looked up, and a citation
    that does not resolve never becomes a count here.  So "this entry has no
    citations" is not evidence about the entry -- it is the ordinary state of
    every entry.  Dropping on that alone would empty a working memory.
    """
    now = time.time()
    entries = (_entry("a", "A"), _entry("b", "B"))
    # Nothing has ever been cited, and both entries are recent.
    kept = prune(entries, {}, now=now).kept
    assert [e.title for e in kept] == ["A", "B"]

    # Still nothing cited, and now both are far past the window -- but the cap
    # is not exceeded, so age alone does not drop them either... except that
    # age *plus* zero citations is exactly the pair the window is for.
    old = {
        f"{BODY_FILE}#a": {"count": 0, "first_seen": now - 400 * DAY},
        f"{BODY_FILE}#b": {"count": 1, "first_seen": now - 400 * DAY},
    }
    dropped = prune(entries, old, now=now).dropped
    assert [e.title for e, _ in dropped] == ["A"], "one citation is enough to survive the window"


def test_F17_13_the_behavioural_signal_is_not_wired_into_ranking() -> None:
    """codex keeps the two usage signals apart, and so does this.

    The behavioural one (`usage_kind_for_call`) sees the model actually open
    `MEMORY.md` and cannot be lied to -- which makes it tempting as a
    replacement for the citation counter `prune` ranks on.  codex does not do
    that: it feeds a telemetry counter and stops (`core/src/memory_usage.rs:
    9-27`), while retention reads only the citation-fed columns
    (`core/src/stream_events_utils.rs:184` ->
    `state/src/runtime/memories.rs:55-73`, consumed at `:389-413`).

    The reason is granularity, and it is checkable rather than stylistic: the
    behavioural signal names a *file*, and every decision `prune` makes is
    about one *entry*.  This test pins the shape -- a kind is a bare file
    label, carrying no entry id for `prune` to rank by.
    """
    import ast
    import inspect
    import textwrap

    from minicodex import memory_write
    from minicodex.memory import MEMORY_MD_KIND, Memory, usage_kind_for_call

    memory = Memory(directory=Path("/tmp/mem"), summary="s", entries=(_entry("a", "A"),))
    kind = usage_kind_for_call(memory, "read_file", {"path": "/tmp/mem/MEMORY.md"})
    assert kind == MEMORY_MD_KIND
    assert f"{BODY_FILE}#a" not in str(kind), "a kind cannot identify which entry was used"

    # And the ranking function genuinely does not consult it. Parsed rather
    # than grepped, and that is the point: `prune`'s docstring explains at
    # length why it does *not* use this signal, so a substring check over the
    # function's text finds the explanation and reports it as a use. Dropping
    # the docstring node leaves only what runs.
    tree = ast.parse(textwrap.dedent(inspect.getsource(memory_write.prune)))
    fn = tree.body[0]
    assert isinstance(fn, ast.FunctionDef)
    statements = fn.body[1:] if ast.get_docstring(fn) is not None else fn.body
    names = {
        node.id for stmt in statements for node in ast.walk(stmt) if isinstance(node, ast.Name)
    } | {
        node.attr
        for stmt in statements
        for node in ast.walk(stmt)
        if isinstance(node, ast.Attribute)
    }
    assert not any("usage_kind" in name for name in names), sorted(names)


def test_F17_13_an_entry_whose_heading_the_merge_reworded_is_new_not_ancient() -> None:
    """A reworded heading is a new `entry_id`, so its citations do not follow it.

    That is a real cost of deriving ids from headings, and it is paid in the
    safe direction: the entry looks new, not unused.  The other way round would
    delete an entry for the crime of having been improved by the merge that
    just ran.
    """
    now = time.time()
    counts = {f"{BODY_FILE}#running-tests": {"count": 30, "first_seen": now - 400 * DAY}}
    # The merge renamed the section; the old row no longer matches anything.
    renamed = (_entry("how-to-run-tests", "How to run tests"),)
    kept = prune(renamed, counts, now=now).kept
    assert [e.title for e in kept] == ["How to run tests"]


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


class _Scripted:
    """Answers each request with the next canned string, and keeps the requests.

    Not a `ScriptedModel` from an earlier chapter: those speak the agent loop's
    protocol (tool calls, turns).  Stage 1 and stage 2 are single completions
    with no tools at all, and a fixture that modelled turns here would be
    modelling something this subsystem does not have.
    """

    def __init__(self, answers: list[str], *, finish: bool = True) -> None:
        self.answers = list(answers)
        self.finish = finish
        self.requests: list[list[dict[str, Any]]] = []

    async def _stream(self, messages):
        from minicodex.model import Completed, TextDelta

        self.requests.append(list(messages))
        yield TextDelta(self.answers.pop(0) if self.answers else "")
        if self.finish:
            yield Completed("stop")

    def stream(self, messages):
        return self._stream(messages)


def _write_session(
    directory: Path,
    session_id: str,
    users: list[str],
    *,
    parent: str | None = None,
    notes: list[str] | None = None,
    calls: list[tuple[str, str]] | None = None,
) -> Path:
    """A real rollout file, written through the real writer.

    Hand-rolled JSON would be a second implementation of the format, and
    chapter 14 has a whole section on what a fixture that reimplements the
    code under test measures.
    """
    from minicodex.agent_types import ToolCall
    from minicodex.history import AssistantMessage, SystemNote, ToolResult, UserMessage

    meta = SessionMeta(session_id=session_id, parent=parent)
    path = directory / f"{session_id}.jsonl"
    with RolloutWriter(path, meta) as writer:
        for note in notes or []:
            writer.append(SystemNote(note))
        for text in users:
            writer.append(UserMessage(text))
        made = [ToolCall(f"c{i}", name, {}, args) for i, (name, args) in enumerate(calls or [])]
        writer.append(AssistantMessage("on it", tuple(made)))
        for call in made:
            writer.append(ToolResult(call.call_id, call.name, "done"))
        if not made:
            writer.append(AssistantMessage("done", ()))
    return path


def _sub_context(root: Path) -> SubAgentContext:
    from minicodex.composition import sub_context
    from minicodex.shell import ShellSession

    return sub_context(
        build_model=lambda tools: None,
        root=root,
        session=Session(approver=AllowAll()),
        parent_shell=ShellSession(),
        wiring=__import__("minicodex.agent", fromlist=["Wiring"]).Wiring(),
    )
