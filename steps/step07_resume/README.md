# minicodex — chapter 7: interruption and resumption

The agent from chapter 6 survives a long conversation. This one survives its own
process dying: the session is written to disk while it happens, a session that
ends in the middle of a turn comes back as a conversation that can still be
sent, and Ctrl-C stops the work without leaving a tool call unanswered.

```bash
uv sync --all-extras
uv run pytest

uv run minicodex ask "add a retry to the client"
uv run minicodex sessions
uv run minicodex ask "carry on" --resume last
uv run minicodex fork last --upto 6
```

## What is new

| Path | What it does |
|---|---|
| `src/minicodex/rollout.py` | the session on disk: append-only JSONL, one writer, version + migrations, fork |
| `src/minicodex/history.py` | an observer hook, so writing to disk is not four call sites to forget |
| `src/minicodex/agent.py` | resumes from a loaded history; answers every issued call when cancelled |
| `src/minicodex/shell.py` | kills the process group on cancellation, not only on timeout |
| `src/minicodex/__main__.py` | `--resume`, `sessions`, `fork` |
| `tests/test_faults_ch07.py` | 34 tests, one per fault plus the shapes around them |

## The measurement this chapter turns on

An agent-shaped writer — one line per history item, a tool call that takes
80ms — killed at 20 slightly different moments (`probe_rollout.py`):

```
=== an agent-shaped writer, killed at 20 random-ish moments
    ends on assistant    20/20   UNSENDABLE: call with no result
```

Not "sometimes the file ends badly". **Every time.** The wall-clock of a turn is
spent inside the tool, so that is where the kill lands, so the last thing on
disk is a call whose result never arrived — the exact shape chapter 1 refuses
to send.

Recovery is therefore not an edge case, it is the normal path.

## What could not be reproduced

The classic "killed mid-write leaves half a JSON line" did not happen, in three
configurations, on Windows/NTFS:

```
=== killed mid-write: small lines (200 bytes), flushed          torn tail: False
=== killed mid-write: one 400KB line per write, flushed         torn tail: False
=== killed mid-write: small lines, default buffering, no flush  torn tail: False
```

The line-by-line reader is kept anyway (three lines, and the failure it guards
against is unrecoverable), labelled unmeasured rather than ticked off.

What *did* corrupt a file is two processes writing one:

```
=== two processes appending to one file, 4000 records each
    records written: 8000
    lines on disk:   5559   from A: 2699   from B: 2832
    unparseable:     28
    records lost:    2469
```

Not interleaving — **deletion**. 2469 records overwritten and gone. Hence the
lock file.

## Measured against a real model

After a crash during `apply_patch`, the patch may or may not have been written,
and the recovered history says nothing about it. What does the model do first
when told to carry on? (`probe_resume.py`, gpt-4o-mini, "verify" = looks before
touching)

| what the history says | verified first |
|---|---|
| nothing | 0/13 |
| "This session was resumed from a file on disk." (placebo) | 0/5 |
| a four-sentence note with the count and two instructions | 10/19 |
| one sentence, no count | 10/11 |
| **one sentence + the count** (shipped) | **11/11** |

The placebo arm is what makes the rest mean anything: it is the warning that
works, not the presence of a system note. And the careful four-sentence version
lost to the plain one, twice, on independent runs.

## Deliberately not done

- **resume writes a new file** rather than appending to the old one, so the
  discarded tail and the recovered prefix never share a file
- no compaction of a resumed session across processes beyond the baseline
  marker — the pre-compaction turns stay in the file and are skipped on load
- the lock is currently unreachable from the CLI (every run gets its own
  filename); it is reachable from the library, which is where `fork` and any
  future append-to-same-file design live
- `killpg` is still POSIX-only (F02-10), so the cancellation test is skipped on
  Windows and says so
