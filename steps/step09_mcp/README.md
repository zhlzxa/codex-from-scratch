# minicodex — chapter 9: tools from somewhere else

The agent from chapter 8 has four tools, all of them written here. This one
can also use tools it did not write, from MCP servers it starts as
subprocesses and does not control — under names that cannot collide, with
results normalised into the one string chapter 0 promised, and with a schema
budget so that sixty tools do not silently become the whole request.

The protocol comes from the official SDK (`mcp>=2.1`), as it does in codex
(`rmcp = "=3.0.0"`, `codex-rs/Cargo.toml:393`). The rule this book follows:
**hand-roll what you are teaching, depend on what you are not.** What this
chapter teaches is everything the SDK does *not* decide — name collisions,
the schema budget, footprints, result normalisation, two separate timeout
budgets, the environment a spawned server may see, and the trust boundary
around a program you did not write. Those are `registry.py` and the thin
`McpClient` around `mcp.Client`.

`FAULTS.md` F09-10…F09-16 are the places the dependency decided differently
from this project, or did not decide at all: an `env` argument that unions
rather than replaces (F09-10), typed returns mirrored into `structuredContent`
so every result arrived twice (F09-12), a shutdown cap that was *nearly*
long enough and orphaned ten processes (F09-13), and a protocol revision that
forbids server-initiated requests outright (F09-16).

```bash
uv sync --all-extras
uv run pytest

uv run minicodex ask "search my notes for the one about meetings" \
    --provider openai --mcp mcp.example.json
```

## What is new

| Path | What it does |
|---|---|
| `src/minicodex/mcp.py` | `McpClient` — a thin adapter over the official SDK's `mcp.Client`: lifecycle, deadlines, the environment allowlist, and a dead server's last words |
| `src/minicodex/registry.py` | `McpRegistry` — namespacing, staged schema exposure, `tool_search`, result normalisation, remote `Footprint`s, elicitation routed to chapter 5's approver |
| `src/minicodex/__main__.py` | `--mcp CONFIG`: start the servers, merge the tools, shut the servers down |
| `mcp_servers/` | two real MCP servers, ours, each able to fail on request |
| `tests/test_faults_ch09.py` | 43 tests, against real MCP servers, none networked |
| `mcp_servers/*.py` | two real servers built on the SDK, each with switches that make it misbehave on cue |
| `probe_mcp.py` | the measurements: token cost, provider name rules, tool selection, round trips |
| `probe_mutations_ch09.py` | 12 one-line mutations that must turn the suite red |

## Configuration

```json
{
  "servers": {
    "files": {"command": ["python", "mcp_servers/files_server.py"]},
    "notes": {"command": ["python", "mcp_servers/notes_server.py"], "env": {"TOKEN": "..."}}
  }
}
```

A server subprocess inherits an allowlist, not the environment — chapter 2's
F02-09 one process further out. Credentials go in `env`, one at a time.

## What was measured

**Function names, against the real API.** The pattern is `^[a-zA-Z0-9_-]+$`
and the maximum length is 128 — both enforced with an HTTP 400 that fails the
whole request, not just the offending tool. A dot in a server name breaks
every tool in the session. The constant in the code said 64 until it was
measured.

**Schema cost.** Sixty realistic tool schemas are 8187 tokens, 99.6% of a
short request, re-sent on every turn. The same sixty as a name-plus-one-line
index: 1399 tokens, 19%.

**Whether hiding them helps.** It does not:

```
61 tools, irrelevant distractors, all schemas shown       correct 3/3, 3/3, 3/3
61 tools, plausible near-misses, all schemas shown        correct 3/3
61 tools, deferred behind tool_search (single turn)       correct 0/3
61 tools, deferred, allowed to loop for four turns        correct 0/6
```

Deferring is a **token** fix that costs accuracy, so `DEFAULT_SCHEMA_BUDGET`
is a token budget rather than a tool count: show every schema until they do
not fit, then show an index.

## What did not reproduce

**F09-02** (selection collapses at sixty tools). Two earlier rounds of this
probe appeared to reproduce it and both were artefacts of the catalogue —
first because every distractor was irrelevant, then because ten distractors
were repeated six times each. With sixty *distinct* plausible tools,
gpt-4o-mini chose correctly 3/3. What does degrade selection is a second tool
that plausibly answers the same question, which is chapter 3's F03-05 and is
fixed the same way — except that at sixty tools you do not own the
descriptions.

**F09-09** (twenty round trips for one batch job). Ten independent calls
arrive in a single response, 3/3; the version that has to discover its targets
first takes two round trips, 3/3. Parallel tool calls plus chapter 8's
scheduler already cost what code mode was meant to save.

## Deliberately not done

- **No code mode.** Running model-written scripts against the tool catalogue
  needs a sandboxed interpreter, which is chapter 5's unfinished business —
  and F09-09 did not reproduce.
- **No HTTP or SSE transport.** stdio only. Adding one is a transport, not a
  redesign: nothing above `McpClient` knows a subprocess is involved.
- **No OAuth.** Elicitation is implemented and routes to chapter 5's approver;
  a full OAuth device flow with token storage is a separate piece of work.
- **A remote tool's `readOnlyHint` is trusted for exactly one conclusion** —
  two read-only calls to the same server may overlap. It never buys an empty
  `Footprint()`, because a remote tool may touch a local file and nothing
  here can prove otherwise.
- **MCP servers are not approval-gated.** They are started from a config file
  the user wrote, not from anything the model said. Their *tools* are called
  by the model, and those results pass through `normalise()`; the sandbox
  modes of chapter 5 do not reach into another process.
