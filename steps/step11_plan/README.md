# minicodex — chapter 11: decomposition and planning

A checklist the model keeps (`update_plan`), and one question the loop asks
because of it: *the model has stopped asking for tools — is anything on its own
plan still open?*

Chapter 0 defined the end of a run as "no tool calls this turn", which knows
nothing about the task. A plan is the first thing in this program that writes
down what finished means **before** the model decides it has finished.

```bash
uv sync --all-extras
uv run pytest
uv run python probe_plan.py compact          # offline
uv run python probe_mutations_ch11.py
```

## What is new

| Path | What it does |
|---|---|
| `src/minicodex/plan.py` | `TaskPlan`, `update_plan`, `plan_toolset`, `unfinished_note`. Two rules the code enforces, one it explicitly does not |
| `src/minicodex/agent.py` | `on_stop` — a zero-argument callback asked once when the model stops. `agent.py` never mentions a plan |
| `src/minicodex/composition.py` | `watching()` — every tool call, local or remote, tells the plan that something happened; `top_level_tools(..., plan=...)` |
| `src/minicodex/shell.py` | `SYSTEMROOT` added to the environment allowlist |
| `src/minicodex/__main__.py` | one `TaskPlan` per run; `PLAN_INSTRUCTIONS` in the system message when — and only when — the tool exists; the plan is printed after the answer |
| `src/minicodex/agent.py` | the last turn no longer runs tool calls, because four budget-exhausted runs in five were delivering an empty string |
| `tests/test_faults_ch11.py` | 38 tests, F11-01…F11-08 |
| `tests/test_schemas.py` | the description snapshot now walks the **assembled** tool set instead of a hand-written list |
| `probe_plan.py` | the measurements: ten sections, one offline |
| `probe_mutations_ch11.py` | 21 mutations across five modules |

## What was measured

**A tool being available is not a tool being used.** Three arms, five samples
each, on a five-requirement task stated as one paragraph rather than as a
numbered list:

```
no plan tool    21/30 requirements     3/5 samples complete
plan, silent    25/30                  3/5      — 2 of 5 samples never called it
plan, told      30/30                  5/5      — every sample called it
```

The middle arm is why a two-arm A/B on availability would have reported "the
plan tool helps a bit" and been wrong about the mechanism.

**The plan does not survive compaction.** Twenty items down to five; the
`update_plan` call is in the dropped region, no error anywhere:

```
before 20 items / 6500 tokens
after  5 items / 927 tokens, dropped 16
the plan survived compaction: False
```

Which is why the plan is an object the run holds, not a message. codex keeps
its plan only in the transcript plus a UI event — structurally the same gap,
except a human can still see the panel.

**The agent could not run its own tests, and blamed the environment.** The
first real run of this chapter ended with the model reporting success while two
tests were red, because `python -m pytest` inside `run_shell` died with
`OSError: [WinError 10106]` — chapter 2's environment allowlist has no
`SYSTEMROOT`, so Winsock cannot initialise and `import asyncio` fails in any
subprocess. The same variable was already in `scripts/check_layers.py`'s
allowlist, written correctly there in interlude B and never propagated.

**A budget-exhausted run delivered zero characters, four times out of five.**
Not an abrupt summary — nothing. The model spends its last turn on a tool call,
the loop runs it, appends a result nobody reads, and falls out of the loop with
`final_text` still `""`. Rewording the warning cannot fix that; taking the tools
away on the last turn does. 0/5 afterwards, and three of the five volunteered
that the tests were still red.

**codex states two rules it does not check.** "At most one step can be
in_progress" appears in the tool description and in five system prompts;
`PlanHandler` validates nothing. Here it is three lines.

## Deliberately not done

- **No `PlanStore`, no `Planner`, no plan strategy.** None of the three
  exceptions in the abstraction rule applies: nobody wants a second backend,
  the trust boundary is already `_parse()`, and the invariants need a function
  rather than an interface.
- **The evidence check is weak on purpose.** `update_plan` refuses to mark a
  step completed when *nothing at all* has run since the last update. It does
  not, and cannot here, check that the right thing ran: "update the README" is
  a claim about the world.
- **The stop check catches an open plan, not a false one.** If the model marks
  every step completed and stops, `outstanding()` is empty and the loop says
  nothing. Measured: one sample in five ended that way in both arms.
- **No periodic goal restatement.** codex has a whole crate for it
  (`ext/goal/`, three templates). See the chapter for what was measured and why
  nothing was built here.
- **`explanation` is not in the schema.** codex has it and it is optional;
  the requirement to say why the plan changed is in the description instead.
