# minicodex — interlude A: the first refactor

No new features. The same agent as step 4, with one duplicated declaration
removed and two inherited bugs fixed.

```bash
uv sync --all-extras
uv run pytest

# Recorded responses, no GPU and no key needed:
uv run minicodex serve-stub &
uv run minicodex ask "What does src/minicodex/__init__.py define?" \
    --base-url http://127.0.0.1:11435/v1
```

## What changed

| Path | What changed |
|---|---|
| `src/minicodex/tools.py` | `ToolSpec` — a tool is declared once; the schema list and the handler table are both derived from it |
| `src/minicodex/agent_types.py` | `ToolFn` moved down here, so `tools.py` never has to import `agent.py` |
| `src/minicodex/shell.py` | the read-ceiling path closed a dead process's pipe before waiting; without it `wait()` never returns |
| `tests/test_characterization.py` | new: what the code did *before* the refactor, so "unchanged" is checkable |
| `tests/fixtures/tool_schemas.json` | the exact bytes the model is shown |
| `tests/fixtures/golden_transcript.json` | every request body of a three-turn run |
| `tests/test_boundaries.py` | `tools.py` may not import `agent.py` |
| `.gitignore` | `.minicodex/` — the agent writes a full transcript of every run and nothing was ignoring it |

## Why `ToolSpec`

There were two tables. `default_tools()` returned `{name: handler}`;
`tool_schemas()` returned a separately hand-written list of schemas. Nothing
tied the names together, and they met only in `__main__`, as two arguments.

Disagreeing was silent in both directions:

- **handler missing** — the model calls the tool and is told
  `Error: no tool named 'apply_patch'`, which is the message chapter 0 wrote
  for a model that *invents* a tool name. A wiring mistake arrives wearing the
  model's clothes.
- **schema missing** — the model is never told the tool exists, so it is never
  called, and nothing anywhere reports it.

Three mutations of `default_tools()` — rename a key, drop an entry, add an
entry — each left all 125 tests green. `default_tools` was mentioned by
exactly zero of them.

## The two inherited bugs

**The read ceiling never returned.** `proc.wait()` resolves when the process
has exited *and* every pipe has reached EOF. Giving up on the ceiling leaves
unread bytes in stdout, so the wait blocked forever on an already-killed
process. Measured, 12 samples per interpreter:

| python | before | after |
|---|---|---|
| 3.10.20 | 0/12 hang | 0/12 |
| 3.11.15 | **12/12 hang** | 0/12 |
| 3.12.3 | 4/12 hang | 0/12 |
| 3.13.13 | 4/12 hang | 0/12 |

Chapters 2 to 4 were written on 3.10, which is why it stayed invisible.

**The test could not have caught it**, because a wall-clock assertion placed
after a call that hangs never runs. The bound has to be on the await.

## Deliberately not done

- `system_prompt()` still has no caller — that is a behaviour change, chapter 13
- `apply_patch` still validates its arguments by hand, 35 lines of it — first
  occurrence, so a TODO rather than an abstraction
- `_collect` still lives in `agent.py` — one call site; moving it would be
  ceremony
- `run_shell` is still POSIX-only (F02-10); on Windows those seven tests skip
  and say so
