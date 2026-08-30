# minicodex — chapter 10: an agent inside a tool call

The agent from chapter 9 does everything itself, in one conversation. This one
can hand a self-contained piece of work to a second agent — with its own
history, its own tool table, its own session file — and get back one bounded
answer that says how it ended.

```bash
uv sync --all-extras
uv run pytest

uv run minicodex ask "Use spawn_agent to find out what src/minicodex/scheduler.py \
is for, then answer in one sentence." --provider openai --sandbox-mode read-only --yes
```

```
[sub-agent depth 1: Investigate the purpose of the src/minicodex/scheduler.py file in the ]
[sub-agent depth 1: ok in 2 turn(s)]
The `src/minicodex/scheduler.py` file is responsible for managing the scheduling of
tool calls by determining which calls can run concurrently based on their resource
interactions.

[gpt-4o-mini | completed after 2 turn(s)]
[sub-agent 20260811T100503-18196: ok, 2 turn(s), 5.2s]
[session: .minicodex\sessions\20260811T100500-18196.jsonl]
```

## What is new

| Path | What it does |
|---|---|
| `src/minicodex/subagent.py` | `TaskSpec` (the contract in), `TaskResult` (the outcome out), `run_task`, and the `spawn_agent` tool |
| `src/minicodex/clip.py` | the head-and-tail clipper, extracted at its third caller |
| `src/minicodex/tools.py` | `tool_context()` / `bind_all()`; `default_tools` takes a `ShellSession` |
| `src/minicodex/rollout.py` | `SessionMeta.parent`; `--resume last` skips sub-agent sessions |
| `tests/test_faults_ch10.py` | 32 tests, no network |
| `probe_subagent.py` | the measurements: 12 sections, 8 of them offline |
| `probe_mutations_ch10.py` | 13 one-line mutations that must turn the suite red |

## What was measured

**A whole history is 7x a contract.** An eight-item parent rendered for a
child costs 4476 tokens against 638 — and carries the contents of three source
files the child's task never mentions.

**Where a constraint goes decides whether it is obeyed.** One task, one rule
("the shell on this machine is broken"), five samples per arm, gpt-4o-mini:

```
task only                            5/5 used the shell
constraint appended to the task      3/5 and 4/5 used it anyway  (two runs)
constraint as the child's system note  0/5
run_shell not in the child's table     0/5
```

**An instruction is not a bound.** Asked to explain one module with no shape
requested, the child returned a median of 2331 characters; asked for three
sentences, 503. The same three-sentence instruction on a slightly larger task
produced 603, 615, 661, 1468 and **2711**. Hence `MAX_TASK_RESULT_CHARS`.

**Unbounded recursion is not slow, it is unstoppable.** An agent whose model
always spawns reached **830,400 nested agents in 59 seconds** and was killed
from outside. The `asyncio.wait_for(10s)` that should have stopped it died
first — `RecursionError` inside the event loop's own timer callback, while
cancelling the chain.

**A depth limit bounds depth, not width.** With only `MAX_DEPTH = 2` in place,
the same runaway model cost **72 model calls** for one task (8 turns at depth
1, each spawning a depth-2 child that spends 8 of its own). The shared
`DEFAULT_CHILD_TURN_BUDGET` brings it to 32. That number came from a test, not
from a design.

**`asyncio.wait_for` does not time out an agent.** `Agent.run` catches
`CancelledError` and returns a normal `RunResult` (chapter 7). So `wait_for`
cancels the child and *returns its value*: no `TimeoutError`, an empty answer,
`stop_reason="interrupted"`, and a parent that cannot tell that from success.
`run_task` shields the child and cancels it by hand.

**Serialising sub-agents costs exactly what it says.** Two one-second
sub-agents: 2.02s as `STATEFUL`, 1.01s with no declared conflict — and the
unserialised version loses one of two edits **29-30 times out of 30**, across
five runs. Which of the two edits survives is a coin flip.

## Deliberately not done

- **Sub-agents are serial, and that is F10-08 unpaid.** `spawn_agent` is
  `STATEFUL`, so two of them never overlap. Running them concurrently needs
  isolation this chapter does not build (see below), and the parent is blocked
  while a child runs. codex's answer was a dedicated `awaiter` role — which is
  commented out in `core/src/agent/role.rs` with `// Awaiter is temp removed`,
  while `awaiter.toml` is still compiled in.
- **No git worktree isolation.** Measured: two worktrees cost 212ms and 28KB,
  and the merge of two edits to one function is an ordinary
  `CONFLICT (content)` that somebody has to resolve. Isolation moves the
  problem from "last write wins" to "who resolves this", and nothing here is
  able to.
- **A sub-agent gets this project's own tools and no MCP tools.** A remote
  tool's `Footprint` is somebody else's promise (F08-06); handing one to a
  child puts a resource outside the parent scheduler's view.
- **The shared budget undercounts in-flight ancestors.** A child is recorded
  when it finishes, so the turns of sub-agents still running above it are not
  counted. Bounded, not exact.
- **`spawn_agent` lives outside `tool_specs()`.** Putting the handler where
  handlers live is a circular import; chapter 10 takes the cheap way out and
  interlude B deals with the case where there is no cheap way out.
