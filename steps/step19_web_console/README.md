# minicodex — chapter 19: the web console (a browser, and nothing the agent noticed)

The same agent, driven from a browser instead of a terminal. Sessions grouped by
workspace, the whole 3×3 sandbox-and-approval matrix on screen and editable, the
approval prompt as a card that really suspends the tool call, memory and skills
switched on per session, and a stop button that lands as `mark("interrupted")` in
the rollout file.

The headline is what it cost the agent: **zero lines**. Thirty of thirty
top-level modules are byte-identical to chapter 18. Every seam the console needed
was already there for a different chapter's reason — chapter 5's `Approver`
protocol, chapter 7's `RolloutWriter` observer and its `resolve("last", dir)`,
interlude B's single composition root. `__main__.py` grew 57 lines, all of them
the `serve` subcommand.

```bash
uv sync --all-extras
npm --prefix frontend ci
npm --prefix frontend run build

uv run minicodex serve                          # http://127.0.0.1:8000
npm --prefix frontend run dev                   # or: 5173, hot reload, proxied

uv run pytest                                   # 1774 tests, all offline
uv run python scripts/check_layers.py           # now reads subpackages too
uv run python probe_mutations_ch19.py           # 32 mutations, 0 survivors
uv run python probe_web.py core-diff            # offline: the headline
uv run python probe_web.py order                # offline: F19-03, 0% then 100%
uv run python probe_web.py bundle               # offline: what the rewrite cost
uv run python probe_web.py stream               # network: OPENAI_API_KEY, a few cents
```

## What is new

| Path | What it does |
|---|---|
| `src/minicodex/web/runtime.py` | one turn, wired exactly as `__main__._ask` wires one — recorder, compaction summariser bound to parent *and* child, memory read/write, skills, resume, the background writer |
| `src/minicodex/web/channel.py` | one queue and one pump per thread: ordering by construction, not by luck |
| `src/minicodex/web/approver.py` | `WebApprover` — chapter 5's protocol, suspended on an `asyncio.Future` a later request resolves |
| `src/minicodex/web/events.py` | `StreamingRolloutWriter` — an event reaches the browser iff it was fsynced first |
| `src/minicodex/web/routes.py` | 28 routes: threads, settings, approvals, rules, memory, skills, fork, cancel |
| `src/minicodex/web/store.py` | providers / MCP / threads / per-thread settings, fsynced |
| `src/minicodex/web/app.py` | the composition root; static mount last so `/api` wins |
| `src/minicodex/__main__.py` | `minicodex serve --host --port --data-dir`, imported lazily |
| `frontend/` | React + Vite. The design is the previous console's, ported unchanged; the panels are new |
| `scripts/check_layers.py` | `rglob` instead of `glob`, relative imports resolved, one new forbidden edge |
| `tests/test_faults_ch19.py` | 40 tests, F19-01…F19-11, all offline |
| `probe_web.py` | `core-diff`, `order`, `bundle` (offline) and `stream` (network) |
| `probe_mutations_ch19.py` | 32 mutations, three of them against `check_layers.py` itself |

## The headline: nothing in the agent changed

```
    30 top-level modules byte-identical to chapter 18
     4 changed only by chapter 15's fixes: agent.py, registry.py, replay.py, subagent.py
     0 agent modules changed for the console  <- the headline

  __main__.py: +57 lines, all of it the `serve` subcommand
  new backend  (8 modules): 1898 lines
  new frontend (9 files):   2216 lines
```

The four back-ported files are chapter 15's release fixes, carried back because
this step reads *before* chapter 15 and shipping a console with a known
Python-3.10 timeout bug in it to keep a number tidy would be a strange trade.

## What was measured, and what broke while measuring it

**The naive fan-out is not flaky. It is 0%, and then 100%.**
`probe_web.py order`, 20 trials × 200 events:

| sends | naive fan-out | one queue, one pump |
|---|---|---|
| every send costs one loop turn | **0** inversions, 0/20 runs | 0 |
| one send in three costs two | **20** inversions, 20/20 runs | 0 |

An inversion is an event arriving after one emitted later. On a laptop, one
tab, a local model, the broken version is perfectly ordered — which is why it
shipped. Give it a real network and a tool result the size of a file and it
reorders every run. This is a fault you cannot find by using the thing.

**Per-item streaming costs the reader 2.2 seconds.** `probe_web.py stream`,
12 requests, `gpt-4o-mini`, both numbers off the same request:

| | seconds |
|---|---|
| first token off the wire | 1.97 |
| assistant item complete (what this console shows) | 4.20 |
| **the reader waits** | **2.23 average, 3.64 worst** |

That is not a small number and the chapter does not pretend it is. Streaming per
history item buys durability for free — an event exists in the browser if and
only if it exists on disk, because there is one call site and it does both — and
the price is paid entirely on long prose answers. A tool-heavy turn is already
chunky, because every call and every result is its own item.

**32 mutations, 0 survivors**, including three against `check_layers.py`.

That line was wrong when it was first written, and the way it was wrong is the
chapter's own lesson pointed back at it. Three mutations survived: the event
fan-out, the thread hop on an approval reply, and the second-answer guard —
that is, both of the mechanisms the table above advertises. Tests for all three
existed and passed with the mechanism deleted:

- the ordering test gave every send the same cost, so a hundred fan-out tasks
  resumed in creation order and arrived sorted by the scheduler's FIFO habit
  rather than by the pump;
- the second-answer test relied on `resolve` popping, so it returned on the
  first half of `if future is None or future.done()` and never exercised the
  second — the half that matters is a reply arriving in the window after
  `wait_for` cancelled the future and before `discard` removed it;
- nothing at all covered `call_soon_threadsafe`, because a data race returns
  the right answer in a test every time.

`test_F19_03_order_holds_when_the_early_events_are_the_slow_ones`,
`test_F19_02_a_reply_that_lost_the_race_with_the_timeout_is_refused` and
`test_F19_02_a_reply_from_a_worker_thread_goes_through_the_loop` close them.
The count above is now measured rather than asserted.

## Deliberately not done

- **Token-level streaming.** The measurement above is the argument for it, and
  it would be the first change to the agent this chapter did not have to make.
  Written up rather than built, because a second path out of the model client
  needs its own ordering and its own failure story.
- **Authentication.** `serve` binds to loopback and warns if you move it. A
  process that runs shell commands on request has no business on `0.0.0.0`
  behind nothing, and a login form would imply it did.
- **Multi-writer anything.** One JSON file per kind, one process, one lock —
  `steps/step07_resume` and `steps/step08_concurrency` already show what a real
  multi-writer store costs, and this deployment does not have that problem.
- **A plugin tab that does anything.** codex has a plugin mechanism; this
  package has no module for one, so the tab says so instead of showing an empty
  list that implies it looked.
