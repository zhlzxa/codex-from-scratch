# minicodex — chapter 8: concurrent tools

The agent from chapter 7 survives a crash. This one stops running three
independent tool calls one at a time when nothing requires it: a model that
asks to read five unrelated files gets five real files back in the time one
takes, and two edits to two different files land at the same time instead of
in sequence — while two edits to the *same* file, or two shell commands in one
turn, still never overlap.

```bash
uv sync --all-extras
uv run pytest

uv run minicodex ask "read a.py and b.py and summarise both"
```

## What is new

| Path | What it does |
|---|---|
| `src/minicodex/scheduler.py` | `Footprint`, `conflicts()`, `batches()` — a tool-agnostic scheduler that groups one turn's calls so conflicting ones never share a batch |
| `src/minicodex/tools.py` | `footprint_of()` — what `read_file`/`apply_patch` touch as resolved paths; everything else is `STATEFUL` |
| `src/minicodex/agent.py` | the tool-execution loop batches and gathers instead of awaiting one call at a time; a semaphore bounds real concurrency; cancellation answers every call in every batch, not just the current one |
| `src/minicodex/__main__.py` | binds `footprint_of` to the run's root — the one call site that turns the scheduler on |
| `tests/test_scheduler.py` | 28 tests: pure scheduling logic, `footprint_of()`, and nine agent-level measurements, one per fault |
| `probe_scheduler.py` | measures whether F08-01's race needs an artificial delay to reproduce (it does not) |

## The default is still fully serial

Every `Agent` built without `footprint_of=` — which is every test written
before this chapter, and every `Agent` this project has ever tested — treats
every call as `STATEFUL`: never share a batch with anything. Concurrency is
opt-in, at exactly one call site (`__main__.py`), bound to the run's actual
root. Nothing runs two calls together it was not explicitly told it could.

## What was measured

Two patches to the same file, raced with a bare `asyncio.gather()` and no
scheduler (`probe_scheduler.py`, 30 trials per configuration):

```
=== F08-01 / F08-09: does the naive race need a deliberate delay? (n=30) ===
    read_source delay= 0.000s   edit lost: 30/30
    read_source delay= 0.001s   edit lost: 30/30
    read_source delay= 0.050s   edit lost: 30/30
```

Both calls report `Applied 1 edit(s)`. One of them is lying, and nothing says
so — the file just quietly has one change in it instead of two. Not "usually
loses an edit under load": 30/30, with the artificial delay removed entirely.
The chapter went in assuming this fault would need a widened race window to
reproduce reliably (F08-09, "only on slow machines"); it did not need one at
all, at least not for this fault, on this machine.

Running the same two edits through `Agent.run()` with the scheduler wired in
never loses either one: `batches()` puts them in separate batches, and
`agent.py` never starts the second batch's `asyncio.gather()` until the
first one has returned, so the two writes cannot be in flight together.

## What did not reproduce

**F08-06** (implicit ordering the model assumes but the scheduler does not
know) could not be constructed with this tool set. `read_file` and
`apply_patch` both resolve a real path and declare exactly that path as their
footprint; there is no way for editing one already-existing file to change
the content of an unrelated one. The fault needs a tool whose declared
footprint can be wrong — an MCP tool in chapter 9 is exactly that shape, which
is why this entry stays open rather than closed.

**F08-04** (one call raising loses its concurrent siblings) does not
reproduce either, and the reason is structural rather than lucky:
`_run_tool()` has turned every ordinary exception into a returned string
since chapter 0, so the coroutine `asyncio.gather()` awaits here never raises
for a failing tool. Pinned by a test rather than assumed.

## Deliberately not done

- **No lock-free reader/writer distinction finer than one path.** Two reads
  of the same file could safely overlap and do (`conflicts()` says no); a
  read and a write of the same file cannot and never do. There is no
  partial-file locking (byte ranges, line ranges) — the resource key is
  always a whole resolved path.
- **`run_shell` is `STATEFUL` unconditionally**, not "parse the command and
  work out what it touches." The chapter 5 shell parser answers "is this
  dangerous"; teaching it to also answer "what files might this touch" is a
  second, much harder question this chapter does not take on.
- **No per-path lock object, no `asyncio.Lock`.** The scheduler is list
  scheduling over a turn's calls, decided once before any of them run, not a
  runtime lock two calls could contend for. Simpler to test, and correct for
  exactly the shape of concurrency this project has: a handful of calls in
  one turn, not a long-lived pool of overlapping turns.
