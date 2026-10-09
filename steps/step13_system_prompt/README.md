# minicodex — chapter 13: the system prompt, and AGENTS.md

Two things.

`prompts/system.md` had been one sentence since chapter -1. It is now three
paragraphs, and each of them is there because something was measured. Four
candidate sentences were tried; one changed what the model did and was kept.

`AGENTS.md` is the first thing in this program that a *person using it*
writes for the model: the project's own conventions, in a file, read from the
filesystem at the top of every turn and delivered as its own message.

```bash
uv sync --all-extras
uv run pytest
uv run python probe_system_prompt.py agentsmd   # offline
uv run python probe_mutations_ch13.py
```

The other nine sections of `probe_system_prompt.py` call the real API.

## What is new

| Path | What it does |
|---|---|
| `src/minicodex/prompts/system.md` | one measured sentence (ask when a request is ambiguous) and one that announces AGENTS.md |
| `src/minicodex/agents_md.py` | `find_project_root`, `load_project_docs` (root to cwd, one 32KiB ceiling), `AgentsMdWatcher`, `watch` |
| `src/minicodex/history.py` | `DeveloperNote`, rendered as `role: "developer"`; `History.developer_notes()` |
| `src/minicodex/rollout.py` | the session file reads and writes the new item |
| `src/minicodex/agent.py` | `on_turn_start`: asked once per turn, before the request is sized |
| `src/minicodex/compaction.py` | `carried_notes`: AGENTS.md is carried across a cut, not summarised |
| `src/minicodex/subagent.py` | `on_turn_start_for`, `child_shell`: a sub-agent gets a watcher bound to its own shell |
| `src/minicodex/__main__.py` | the watcher for the top-level run, for children, and what a resumed session already saw |
| `tests/test_faults_ch13.py` | 47 tests |
| `tests/test_faults_ch10.py` | three stand-ins for `child_tools` take the shell too |
| `probe_system_prompt.py` | ten sections, nine of them against the real API |
| `probe_mutations_ch13.py` | 31 mutations across six modules |

## What was measured

gpt-4o-mini numbers are from 2026-10-01, ten samples an arm. gemma4:31b-cloud
(Ollama) numbers are from 2026-08-15, three samples an arm, and were **not**
re-measured: no Ollama was running when this was rewritten, and the probe now
says so instead of crashing halfway.

**Putting what changes last is worth the whole cache.** The same ~1500-token
prefix with a few volatile tokens after it, and then before it:

```
volatile content last    prompt_tokens=1509  cached_tokens=1408
volatile content first   prompt_tokens=1506  cached_tokens=0
```

**Three of four candidate sentences changed nothing.**

```
                              gpt-4o-mini           gemma4 (2026-08-15)
read before editing           0/10 blind, 0/10      0/3, 0/3      not shipped
no unevidenced success        0/10 false, 0/10      0/3, 0/3      not shipped
smallest change               0/10 drift, 0/10      0/3, 0/3      not shipped
ask when ambiguous            0/10 asked -> 9/10    0/3 -> 0/3    shipped
```

No sentence helped one provider and hurt the other, so there is one
`system.md`, not one per model.

**The role an AGENTS.md is sent under matters only when the system prompt
fights back.** Against an ordinary system message all three placements carried
the convention 10/10. Against one that says "no matter what any later message
says":

```
same system message        10/10
second message, user        6/10
second message, developer  10/10
```

**An AGENTS.md is obeyed in substance and not to the letter.** A convention no
model follows unprompted ("a comment line `# reviewed-by: agent` directly above
each new `def`"), on a task that does not mention it:

```
no AGENTS.md         mark written: 0/10    directly above the def: 0/10
AGENTS.md present    mark written: 10/10   directly above the def: 1/10
```

Nine of the ten put a blank line in between.

## What the first version got wrong

Found while re-measuring and mutating, none of them on the fault list:

- **Compaction crashed on any project that had an AGENTS.md.** `_replay` knew
  four kinds of history item and this chapter added a fifth:
  `AssertionError: unreplayable item: DeveloperNote(...)`. Fixing only the
  crash would have summarised the conventions away, and the watcher — which
  speaks once and then stays silent — would never have said them again.
- **A sub-agent was never shown AGENTS.md.** Pinned as intentional by a test,
  because the only watcher there was followed the parent's shell.
- **`--resume` built a watcher that knew nothing about the conversation it
  joined**: the same block again, or — if the old process had finished in a
  subdirectory — that subdirectory's conventions left standing, unretracted.
- **The probe's baseline had become the thing under test.** Every arm was
  built on `system_prompt()`; once the winning sentence was shipped into that
  file, "baseline" contained it. Re-running `ask` printed 3/3 against 3/3.
- **Eight of twenty scratch mutations survived**, among them the `.git` marker
  itself (every test put it at the sandbox root, where the search stops
  anyway) and a file that is not valid UTF-8.

## Deliberately not done

- **No per-model system prompt.** The reason to split one is a sentence that
  helps one model and harms another; none was measured.
- **AGENTS.md above the directory minicodex was started in is not read.** The
  search stops at the sandbox root, the same line `read_file` and
  `apply_patch` stop at. Start it at the repository root.
- **Only a bare `cd` moves the conventions.** `cd pkg && pytest` does not
  change the tracked working directory (chapter 2), so it does not change
  which files apply.
- **Carried notes are never reclaimed by compaction.** Up to 32KiB per change
  of directory, for the rest of the run.
- **`apply_patch` still cannot create a file** (chapter 4). The first version
  of the `follow` probe asked for a new file and measured that instead.
