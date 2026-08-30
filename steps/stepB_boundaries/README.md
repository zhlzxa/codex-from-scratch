# minicodex — interlude B: the second refactor

Chapter 10 left a note: putting the `spawn_agent` handler where the other
handlers live is a circular import, so it went into `__main__` instead. This
unit goes back for it.

The cycle is real and reproducible. The interesting part is what the workaround
cost — a second place that builds an agent, passing three of the ten arguments
the first place passes — and the fact that nothing anywhere would have said so.

```bash
uv sync --all-extras
uv run pytest
uv run python scripts/check_layers.py
uv run python probe_boundaries.py
```

```
ok  no import cycles
ok  __init__ is a leaf
ok  forbidden edges
ok  one Agent construction site
ok  every module imports alone
```

## What is new

| Path | What it does |
|---|---|
| `src/minicodex/agent_types.py` | `ToolSet` — handlers, schemas and footprints as one value that refuses to be built if they disagree; `Footprint`/`STATEFUL`/`FootprintFn` moved down from `scheduler.py` |
| `src/minicodex/agent.py` | `Wiring` — recorder, context window, summariser, concurrency cap; `Wiring.agent()` is the only `Agent(...)` in `src/` |
| `src/minicodex/composition.py` | the composition root: `local_tools`, `top_level_tools`, `with_remote_tools`, `sub_context`. Both assembly sites call it |
| `src/minicodex/subagent.py` | no longer imports `tools.py`. Takes `build_tools` and `wiring` from its caller |
| `src/minicodex/registry.py` | `route_footprint` deleted — `ToolSet.plus` routes by ownership |
| `scripts/check_layers.py` | five architecture rules, in the CI lint step |
| `tests/test_faults_chB.py` | 22 tests, FB-01…FB-03 |
| `probe_boundaries.py` | the measurements: graph, both cycles, four repairs, the drift |

## What was measured

**One ordinary line in `__init__.py` breaks the package.**
`from minicodex.agent import Agent`, so that callers can write
`from minicodex import Agent`, makes `import minicodex.tools` fail:

```
ImportError: cannot import name 'compaction_prompt' from partially
initialized module 'minicodex' (most likely due to a circular import)
```

because `compaction.py` does `from minicodex import compaction_prompt` while
the package is still half-built. Three modules that never mention each other.

**The first version of the cycle checker reported "none" on that tree.** It
dropped `from minicodex import <name>` edges, because the name is not a module.
A checker that has never been seen to fail is a checker nobody knows works.

**`import minicodex` exits 0 on a package whose modules cannot import each
other.** With `tools` and `subagent` importing each other, the top-level import
touches neither, because `__init__` is a leaf. The obvious CI smoke test passes.

**Deferring the import into a function body does not remove the cycle.** The
package imports cleanly; the AST checker still reports
`[['minicodex.subagent', 'minicodex.tools']]`. The failure moved from startup
to the first call.

**The workaround's price, on one scripted model and three identical files:**

```
arm                          final request   recorded   peak    wall
Wiring()  (the old child)          181,040      False      2   0.22s
the parent's wiring                 77,244       True      2   0.23s
```

(`wall` jitters by a few hundredths between runs; the first three columns do
not. A real top-level `Agent` on the same script measures 76,896 and one
compaction — that is the parent-vs-child number the docstrings quote.)

A sub-agent never compacted, was never written to the transcript, and ran every
tool call serially. Nothing raised. `subagent.py` was correct on its own terms
— it was wrong next to a call site three modules away that nothing put it next
to.

**The invariant fired on its first run, on a test.** Chapter 10's timeout test
patches a fake `sleep` handler into the child's table and gave it no schema;
`ToolSet.__post_init__` refused to build.

## Deliberately not done

- **`spawn_agent` did not move back into `tools.py`.** The cycle is gone and
  the move is now legal, which is not a reason to make it. A refactor that
  becomes possible is not a refactor that became necessary.
- **No layer number per module.** Designed, dropped: it would catch every
  arrow the five-entry `FORBIDDEN` list catches and more, at the cost of an
  entry per module forever, and the "more" is hypothetical. Three backwards
  arrows have actually happened in this project and all three are named.
- **No module size limit.** codex's `AGENTS.md` has one; interlude A measured
  why it does not belong here.
- **Sub-agents still get no MCP tools.** Chapter 10's decision, unchanged —
  but now written as one absence in `composition.child_tools_builder` instead
  of being implied by which module imports what.
- **`__main__.py` is still 400 lines and still untested.** The composition
  came out of it; the argument parsing and the printing did not.
