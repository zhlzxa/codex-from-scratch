# minicodex — chapter 15: shipping it

Seventeen chapters of tests, and not one of them had ever installed the thing.
This chapter builds the wheel, puts it in an empty environment, runs it the way
a stranger would, and reads the files fourteen of its own past versions left
behind.

Five faults came out of doing that, and four of them are in code from earlier
chapters rather than in this one's.

```bash
uv sync --all-extras
uv run pytest                                  # 1745 tests, all offline
uv run python probe_mutations_ch15.py          # 27 mutations

uv run python probe_release.py compat          # 14 past versions write, this one reads
uv run python probe_release.py wheel           # build, look inside, install clean
uv run python probe_release.py firstrun        # the first five minutes, exit codes and all
uv run python probe_release.py deprecated      # what deleting a spelling would cost

uv run minicodex --version                     # what to paste into a bug report
uv run python scripts/check_release.py v0.1.0  # what release.yml runs before publishing
```

## What is new

| Path | What it does |
|---|---|
| `src/minicodex/release.py` | `SURFACES` (the files that outlive a release), `DEPRECATIONS` + `apply_deprecations()`, `environment_report()` |
| `pyproject.toml` | one version literal, a licence, an issue tracker, a front page for users |
| `PACKAGE.md` | what the wheel's long description should have been for seventeen chapters |
| `CHANGELOG.md` | with **Breaking (files)** as its own heading — the 0.x escape hatch does not cover a user's directory |
| `CONTRIBUTING.md` / `SECURITY.md` / `.github/ISSUE_TEMPLATE/` | what happens when other people arrive |
| `.github/workflows/release.yml` | tag → checks → tests → build → install somewhere empty → publish, in that order |
| `.github/workflows/ci.yml` | a two-entry matrix: the floor `requires-python` promises, and the latest |
| `scripts/check_release.py` | the two facts that do not exist until somebody types a tag |
| `tests/fixtures/compat/` | artefacts written by **fourteen real past versions**, committed |
| `tests/test_faults_ch15.py` | 54 tests, F15-01…F15-05 |
| `probe_release.py` / `probe_mutations_ch15.py` | four sections; 27 mutations |

## What was measured

**Fourteen builds, one version number.** Every step directory in this book from
chapter 5 onward is an installed, runnable version of this program. All
fourteen answer `minicodex 0.0.1`. The recording format changed inside that
range.

**Old files, read by today's code.** Each past version was run for real against
chapter 0's stub, and the artefacts it wrote were handed to this one:

```
  stepA_refactor             session: -                  recording: REFUSED: tool call 'read_file' …
  step05_approval            session: -                  recording: REFUSED: …
  step07_resume              session: ok, 5 item(s)      recording: REFUSED: …
  …
  step14_eval                session: ok, 5 item(s)      recording: ok, 2 turn(s)
  step16_memory_read         session: ok, 5 item(s)      recording: ok, 2 turn(s)
  step17_memory_write        session: ok, 5 item(s)      recording: ok, 2 turn(s)
```

**11/11 sessions readable, 11 of 14 recordings not.** And the format that broke
changed without a line changing in the module that owns it: `recorder.py` is
byte-for-byte identical in **twenty** consecutive versions, chapter 0 to here
(`md5 60014dd3`), and the format it writes changed twice. The payloads are
built at the call sites in `agent.py`. **A diff cannot tell you whether a
format changed.**

**Nine of those refusals were a traceback.** `KeyError: 'attempt'` — chapter
14's loader was written against the one old shape that was in front of it.
Fixed by distinguishing the two cases: `arguments` is *gone* and is refused;
`attempt` was *never written down* and means "one attempt", so it migrates.

**The metadata promised an interpreter nothing had ever run.**
`requires-python = ">=3.10"` since chapter -1. First run there:

```
FAILED tests/test_faults_ch10.py::test_F10_07_a_hanging_child_is_stopped_and_says_so
1 failed, 1655 passed, 9 skipped
```

`asyncio.wait_for` raises `asyncio.TimeoutError` on 3.10, which is **not** the
builtin `TimeoutError` — they were only unified in 3.11. So `except
TimeoutError` never fired and a hanging sub-agent was never stopped, on the
oldest platform this package claims. `mcp.py` had caught both since chapter 9;
the knowledge did not travel.

**The first five minutes, as a stranger has them.** `minicodex` with no
subcommand printed help on stdout and exited **0**, and a failing run announced
`attempt 5 of 4` and then slept for a backoff before giving up:

```
  $ minicodex ask hello          [exit 1]
    out | [transport: waiting 1s, attempt 2 of 4]
    out | [transport: waiting 2s, attempt 3 of 4]
    out | [transport: waiting 4s, attempt 4 of 4]
    out | [transport: waiting 7s, attempt 5 of 4]
```

Both live, both in the shipped program, neither visible from any test — every
test calls `main([...])` with arguments it chose and reads a return value.

**What deleting a spelling would cost, counted.** In this repository alone,
before anyone else has typed either of them:

```
  '--yes' -> '--dangerously-approve-all': 17 occurrence(s) in 13 file(s)
  'minicodex forget' -> 'minicodex rules --forget': 8 occurrence(s) in 5 file(s)
```

## Deliberately not done

- **No 1.0.** 0.x says the Python API can change at any time, which is honest
  and covers the wrong thing: the promise that actually binds is the one made
  to a user's `.minicodex/` directory, and that directory does not read version
  numbers. `CHANGELOG.md` gives file compatibility its own heading instead.
- **No migration of old recordings.** The tool call arguments were never
  written down; there is nothing to migrate from. Refused with a sentence.
- **No full Python matrix.** The floor and the latest, two entries. A break
  that only appears on 3.11 or 3.12 is possible and is not worth doubling the
  cost of every pull request forever; `postmerge.yml` is where that would go.
- **`release.yml` has never run.** No package named `minicodex` is published
  anywhere. The file is written out in full because the *ordering* is the
  lesson, and is labelled a draft in its own footer — as `nightly.yml` has been
  since chapter 14.
- **No `Surface` machinery.** `SURFACES` is a list of facts, not an
  abstraction: nothing dispatches on it, there is no protocol, and no code path
  is generic over it. Chapter 17's distinction — the rule of three is about
  code, and this is data.
- **The probe's own cleanup was incomplete.** `compat` restored the recording
  files it created in each step directory and left the empty `.minicodex/`
  behind in twelve of them. Fixed; recorded because it is the third
  probe-cleanup fault in this book and the fix is one boolean.
- **The compat corpus is fourteen versions of one conversation.** The same
  two-turn `read_file` exchange every time. It answers "can the reader still
  read the writer", and it does not answer "can it read a session that
  compacted, forked, or ran an MCP tool".
