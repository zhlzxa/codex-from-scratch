# minicodex — chapter 17: memory, part two (writing it, and forgetting it)

Chapter 16 read a memory a person had written by hand, and measured that it was
worth having: 4/12 → 12/12 on gpt-4o-mini. This chapter writes one.

Two stages with a temporary file between them, a claim table so two processes
cannot do the same work, a redaction pass on both sides of the model, and a
forgetting rule that starts from citation counts rather than from age. All of
it off unless you ask for it.

**This chapter was resynchronised after chapter 16 was rewritten.** Chapter 16's
first draft was designed without reading codex's memory source and got four
mechanisms wrong; correcting them moved the memory directory out of the
repository, changed the citation format, and retired `Agent.preamble`. The
design in *this* chapter — the two stages, the claim table, the leases, the
quota gate, the forgetting order — was built against
`codex-rs/memories/write/` in the first place and did not change. What changed
is where its files live (F17-12), and one finding that only became visible once
chapter 16 re-measured its own citation format (F17-13). Both are below.

```bash
uv sync --all-extras
uv run pytest                                   # 1682 tests, all offline
uv run python probe_mutations_ch17.py           # 44 mutations
uv run python probe_memory_write.py race        # offline, F17-03
uv run python probe_memory_write.py forget      # offline, F17-06/07

uv run minicodex ask "..." --remember           # off unless you ask for it
uv run minicodex memory                         # what it knows, and where
uv run minicodex memory --jobs                  # which sessions it has read
uv run minicodex memory --remember-now          # run the writer, and wait
uv run minicodex memory --forget-all            # all of it, including raw/
```

## What is new

| Path | What it does |
|---|---|
| `src/minicodex/memory_write.py` | `extract()`, `consolidate()`, `write_memory()`, `prune()`, `note_toolset()`, `scrub()`, `run_pipeline()` |
| `src/minicodex/memory_jobs.py` | `JobStore` — claim, lease, backoff; `pending_sessions()` |
| `src/minicodex/model.py` | `RateLimit` off the response headers, on a *successful* request |
| `src/minicodex/composition.py` | `top_level_tools(remember=...)` — the one tool a model gets |
| `src/minicodex/__main__.py` | `--remember`, `memory --jobs / --remember-now`, a wider `--forget-all` |
| `tests/test_faults_ch17.py` | 56 tests, F17-01…F17-11 |
| `probe_memory_write.py` | nine sections: sessions, signal, secrets, quota, time, race, handedits, ab, forget |
| `probe_mutations_ch17.py` | 44 mutations |

## The headline: a generated memory against a hand-written one

Chapter 16's six tasks, three arms, three samples each. The hand-written arm is
chapter 16's fixture — **a memory tailored to each task**. The generated arm is
**one memory for all six**, produced by running this chapter's pipeline over
four real sessions in a different workspace.

```
  task                 off          hand     generated
  convention           0/3           2/3           3/3
  runner               0/3           3/3           3/3
  layout               0/3           3/3           3/3
  stale                1/3           3/3           0/3
  unrelated            3/3           3/3           3/3
  poisoned             3/3           3/3           3/3
  TOTAL               7/18         17/18         15/18
```

On the five tasks the pipeline had material for the generated memory scores
**15/15**, against the hand-written fixture's 14/15 — it wins `convention`,
which chapter 16 measured at 3/3 and which the hand arm dropped a sample of
here. The gap in the total is one task, `stale`,
and the reason is worth more than the total: `stale`'s memory is *wrong on
purpose* (it names an entry point that does not exist), so the hand-written arm
wins it by triggering chapter 16's drift note. The generated memory has nothing
to say about entry points, so nothing fires, and the arm scores what `off`
scores. Re-measured on its own with 3 more samples: off 1/3, hand 3/3,
generated 2/3 — a task this noisy cannot tell 0/3 from 2/3 either.

## What was measured

**A format with slots in it is a format a model fills.** The careful extraction
prompt — the one that says "producing nothing is a normal and preferred
outcome" in bold, which is codex's own wording — produced three bullets for
*every* session it was shown, including one whose entire content was "which
functions does calc.py define":

```
  quota   [ ] The user prefers to have functions clearly defined and documented.
  quota   [ ] The `calc.py` file defines two functions: `add(a, b)` and `multiply(a, b)`.
  quota   [ ] To avoid confusion, always read the necessary files directly.
  quota   -> bullets [3, 3, 3]  empty 0/3
```

All true. All worthless. All of it would have been injected into every future
request in that repository. The cause is one line further down the same prompt
— "write at most three bullets per section" — which is a quota.

Chapter 16 met the same mechanism from the other side, and its rewrite made the
result sharper than the version this README used to cite. The claim used to be
that asking for a citation block *unconditionally* took compliance from 0/9 to
18/18. That measurement did not survive: under codex's real citation format,
asking unconditionally does still produce a block almost every time — and
**0 of 9 of those blocks contained a line that resolved to anything real.** The
model filled the slot with invented line numbers.

So the two chapters agree on something worse than "a required shape gets
filled": **a required shape gets filled with plausible material** whether or
not there is anything to put in it. A quota produces a bullet about `calc.py`
defining two functions; a citation format produces `MEMORY.md:12-14` for lines
nobody read. Neither failure announces itself.

What fixed it was not more emphasis. It was a verdict the model must give
before it is shown the shape, enforced in code rather than trusted:

```
  shipped !! DURABLE: no
  shipped -> bullets [0, 0, 0]  empty 3/3
```

3/3 no-ops on the session that taught nothing, and the conventions still
extracted from the three sessions that taught something.

**A secret does not leak until somebody asks for it.** A key the session
happened to read (`.env`, via `read_file`) was never repeated by the extractor:
0/3. A key the *user asked to have remembered* came back 3/3, once or twice per
answer:

```
  -- arm: incidental (a key the session happened to read) --
  [0] the model's own output contains the key 0 time(s); … final 0
  -- arm: asked for (the user says: remember this) --
  [0] the model's own output contains the key 1 time(s); the outbound scrub removed 1; final 0
  [2] the model's own output contains the key 2 time(s); the outbound scrub removed 2; final 0
```

The inbound scrub cannot help with the second one — the model was asked to
write the key down and it did. Only the pass on the way *out* keeps it out of a
file that is injected into every future request. Test the easy arm only and you
ship one-sided redaction and a green result.

**Select-then-update is not a claim.** Three processes, six jobs, the obvious
implementation:

```
  naive select-then-update   3 workers claimed 6 job(s), 2 distinct, 4 duplicate(s)
  BEGIN IMMEDIATE            3 workers claimed 6 job(s), 6 distinct, 0 duplicate(s)
```

4/4 runs, identical. Python's `sqlite3` opens a transaction on the first DML
statement and not on a `SELECT`, so all three workers read the same two rows
and all three did the same work, with no error anywhere.

**A deletion is only visible in a diff.** A person edits `MEMORY.md`: adds a
rule, deletes another. Then a merge runs, with the `git diff` and without it:

```
  == with the diff ==        kept both hand edits 3/3   restored the deleted section 0/3
  == without the diff ==     kept both hand edits 3/3   restored the deleted section 3/3
```

The *addition* survives either way, because the edited file is what the merge
is shown. The **deletion** is not in the file — that is what deleting means —
so the diff is the only evidence it happened. That one column is what the git
repository buys.

**Twelve sections in, twelve sections out.** F04-02's shape (a whole-file
rewrite quietly dropping things) did not reproduce at this size: 12/12 survived
a merge, 3/3 samples.

**The cost, and where it is.** Stage 1 is 2.4–2.8s per session; stage 2 is
5.0s; four sessions end to end is 15.4s. That is the number the user does not
wait for, because the pipeline runs at the *start* of the next session, beside
the first request, and not at the end of this one.

**The note tool is a channel for "remember this", not a way of noticing.** The
same convention, stated two ways, three runs each:

```
  "…and nobody commits without it. Please remember that for future sessions."   3/3 notes
  "…and nobody commits without it."                                             0/3 notes
```

## What chapter 16's rewrite changed here

**F17-12 — the lock and the jobs database followed the memory out of the
repository.** Chapter 16 moved memory to `~/.minicodex/memories`, matching
codex's `memory_root()` = `codex_home.join("memories")`
(`memories/write/src/lib.rs:116-117`). The two things this chapter owns had to
move with it, and not for tidiness: they are *about* that one now-global
directory. A per-repository `MERGE_LOCK` guarding a shared memory means one
lock per checkout and no mutual exclusion between them — the exact race the
lock exists to prevent, reintroduced by the lock being in the wrong place. A
per-repository jobs table means the same session is extracted once per
checkout. Both are absolute paths now, and `test_F17_12_nothing_defaults_into_
the_working_directory` pins that, because a relative default is a default that
follows the cwd.

The structural separation did not change and did not need to: the jobs database
was already a *sibling* of the memory directory rather than inside it, because
the memory directory is a git repository and a binary rewritten on every
operation has no readable diff. codex keeps the same split — global memory root,
separate SQLite database (`state/memory_migrations/0001_memories.sql`) — so this
was one of the places the first draft already had right.

**F17-13 — the signal this chapter's forgetting ranks on is one chapter 16 has
now measured as unreliable.** `prune()` ranks on citation counts. Chapter 16's
re-measurement of its own citation format found **0 of 9 citations resolving to
a real entry**: asked for `path:line_start-line_end`, the model supplies
plausible line numbers it never looked up, and a citation that does not resolve
never becomes a count here.

The tempting repair is to rank on chapter 16's *other* signal instead —
`usage_kind_for_call`, which watches the model actually open `MEMORY.md` and
cannot be lied to. **codex does not do that, and neither does this.** It keeps
the two strictly apart:

```
  behavioural  ->  a telemetry counter, and nothing else
                   core/src/memory_usage.rs:9-27
  citation     ->  the database, and therefore policy
                   core/src/stream_events_utils.rs:184
                   -> state/src/runtime/memories.rs:55-73  (usage_count, last_usage)
                   -> state/src/runtime/memories.rs:389-413 (retention pruning)
```

The reason is granularity, and it is checkable rather than stylistic: the
behavioural signal names a *file*, and every decision `prune()` makes is about
one *entry*. Substituting it would keep every entry alive whenever any entry was
read, which is not a forgetting policy.

So the signal stays, and what changed is how much weight it may carry. `prune()`
already refused to drop on zero citations alone — it takes zero citations *and*
a month past the window *and* the cap exceeded. That was written as
belt-and-braces when the reporting *bias* was the only known problem. With the
resolution rate at 0/9 it is the only thing standing between an unreliable
signal and deleting a good entry, and it is now load-bearing rather than
defensive. `test_F17_13_zero_citations_alone_never_drops_an_entry` pins it.

Also inherited: because ids are derived from headings, an entry the merge
*rewords* is a new `entry_id` and loses its citation history. That is paid in
the safe direction — it looks new, not unused — which is what
`test_F17_13_an_entry_whose_heading_the_merge_reworded_is_new_not_ancient`
checks.

## Deliberately not done

- **No sub-agent for the merge.** codex runs phase 2 as an `ephemeral = true`,
  no-network, `AskForApproval::Never` internal agent. Ours is one model call,
  because every input fits in one request — and because a merge agent needs a
  tool that writes memory, which is the one thing F17-09 says a model must not
  have.
- **No `ephemeral` flag.** It was written, for F17-10, and deleted before this
  chapter shipped: nothing sets it. The writer is not an agent, so it has no
  session of its own to mark. The fault's *shape* turned up twice anyway, in
  places the list did not mention — sub-agent sessions in the queue, and the
  extractor reading a `remember_this` call it had already been given by another
  route — and both of those are fixed.
- **No `locks.py`.** The merge lock is chapter 7's `O_EXCL` lock, copied. Two
  sites of twelve lines is the *second* occurrence, and the rule of three says
  the second one is copied with a note pointing at the first.
- **No retry around the two model calls.** Chapter 12's retry lives in the
  agent loop, and the pipeline is not in it. The job table's attempt counter
  and backoff are the retry, at a coarser grain and a slower clock — which is
  the right grain for work nobody is waiting for. A transient `ConnectError`
  during a probe run is what made this explicit rather than accidental.
- **Nothing tells the next merge what was pruned.** `prune()` runs inside
  `write_memory()`, so an entry the merge has just produced can be dropped
  immediately, and the next merge will propose it again. `MAX_ENTRIES = 40` is
  far above any real memory, so the loop does not start in practice — but a
  loop that does not start is still a loop, and the fix (tell stage 2 what was
  dropped and why) is unpaid.
- **`AGENTS.md` is deliberately not extracted.** `transcript_of` drops
  developer notes, and chapter 13's project conventions arrive as one. They are
  already in the repository, in a file a person maintains: extracting them into
  memory stores the same sentence in two places that will then drift.
- **The junk that survives.** The generated memory in the demo contains
  `## calc.py Functions` — "the file contains basic arithmetic functions" —
  which fails the extraction prompt's own thirty-second test. The verdict gate
  moved the decision from per-bullet to per-session, and per-session is the
  only granularity that was measured to work. Per-bullet is unpaid.
- **No second provider.** gpt-4o-mini only, as in chapters 9–14: the local
  ollama is not running on this machine. Every number above is one provider's.
- **The citation signal is not repaired, only survived.** F17-13 above records
  that `prune()`'s primary key resolves 0/9. The conservatism that makes this
  survivable is real, but it is a floor, not a fix: an entry that genuinely
  *is* dead still needs a month and a full cap before it leaves, and an entry
  that is used every day is indistinguishable from one that is never used. The
  repair codex has and this does not is `<rollout_ids>` — a citation naming a
  *session*, resolvable without the model knowing any line numbers. That needs
  a durable per-rollout row (codex's `stage1_outputs`, keyed by `thread_id`),
  and this chapter's stage-1 files are consumed and deleted at merge time. The
  cheapest honest version would be to keep the raw file and give it an id;
  unpaid, and named here rather than left as an absence.
