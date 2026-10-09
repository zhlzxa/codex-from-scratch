# minicodex — interlude A: the first refactor

No new features. The same agent as step 4, with one duplicated declaration
removed, and tests that prove the behaviour did not change.

```bash
uv sync --all-extras
uv run pytest

# Recorded responses, no GPU and no key needed (run the stub in a second
# terminal, or append `&` on macOS/Linux):
uv run minicodex serve-stub
uv run minicodex ask "What does src/minicodex/__init__.py define?" \
    --base-url http://127.0.0.1:11435/v1
```

## What changed

| Path | What changed |
|---|---|
| `src/minicodex/tools.py` | `ToolSpec` — a tool is declared once; the schema list and the handler table are both derived from it |
| `src/minicodex/agent_types.py` | `ToolFn` moved down here, so `tools.py` never has to import `agent.py` |
| `tests/test_characterization.py` | new: what the code did *before* the refactor, so "unchanged" is checkable |
| `tests/fixtures/tool_schemas.json` | the exact bytes the model is shown |
| `tests/fixtures/golden_transcript.json` | every request body of a three-turn run |
| `tests/test_boundaries.py` | `tools.py` may not import `agent.py` |

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
entry — each left all 120 tests green. `default_tools` was mentioned by
exactly zero of them. With the characterization tests: 3, 2 and 1 failures.

## Found along the way, fixed earlier

The first edition of this interlude also found three inherited problems:
`.minicodex/` was not ignored by git, the shell tool's read ceiling hung
forever on Python 3.11+, and seven tests failed on Windows without saying
why. The rewrite fixes them in chapters 0 and 2; `FAULTS.md` still records
where they were first found.

## Deliberately not done

- `system_prompt()` still has no caller — that is a behaviour change, chapter 13
- `apply_patch` still validates its arguments by hand — first occurrence, so
  not yet an abstraction
- `_collect` still lives in `agent.py` — one call site; moving it would be
  ceremony
- `run_shell` is still POSIX-only (F02-10); on Windows those seven tests skip
  and say so
