# minicodex — chapter 14: evaluation and regression testing

Thirteen chapters of measurements, and no way to find out whether any of them
still holds.

Two mutations make the point. Both were applied to the code as it stood at the
end of chapter 13, and both left **all 1525 tests green**:

```
instructions=None            # the CLI stops sending a system message at all
COMPACT_AT = 0.95            # compaction starts at 95% instead of 75%
```

The first one switches off chapter 5's permission block, chapter 11's plan
paragraph and chapter 13's whole system prompt — everything three chapters
measured — in one keyword argument. Every test of `_instructions()` calls
`_instructions()`; not one of them checked that anything else did.

This chapter builds the two things that would have noticed, and the task set
that says whether a change made the agent worse.

```bash
uv sync --all-extras
uv run pytest                             # 1549 tests, all offline
uv run python probe_eval.py replay        # offline
uv run python probe_eval.py leak          # offline
uv run python probe_mutations_ch14.py     # 14 mutations
uv run python probe_flaky.py --runs 5     # the same suite, five times

uv run minicodex ask "..." --provider openai      # writes a recording
uv run minicodex replay .minicodex/recordings/session-*.jsonl
```

## What is new

| Path | What it does |
|---|---|
| `src/minicodex/replay.py` | `load()`, `RecordedModel`, `recorded_tools()`, `divergence()` — one real run, re-run offline |
| `src/minicodex/evals.py` | `Task`, `Trajectory`, `Check`, `run_task()`, `Arm`, `regressions()`, and the six-task set |
| `src/minicodex/agent.py` | the two `recorder.record()` calls now write arguments and failure evidence |
| `src/minicodex/__main__.py` | a `config` event at the top of every recording; `minicodex replay` |
| `src/minicodex/shell.py` | `ShellSession(cwd=...)` — the shell starts where the other tools are pointed |
| `src/minicodex/tools.py` | `tool_context(root=X)` passes `X` to the shell (F14-08) |
| `tests/property_support.py` | `minimise()`: a failing random history, shrunk to the part that matters |
| `tests/test_faults_ch12.py` | one `monkeypatch.chdir` — that test had been writing a real transcript into the repository since chapter 12 |
| `pyproject.toml` | `extend-exclude = [".probe"]`, so the linter stops formatting the agent's homework |
| `tests/test_faults_ch14.py` | 24 tests, F14-01…F14-08 |
| `probe_eval.py` | seven sections: replay, trajectory, ab, focus, judge, cost, leak |
| `probe_flaky.py` | the suite N times, same input every time; anything that changes its mind is named |
| `probe_mutations_ch14.py` | 14 mutations |
| `.github/workflows/nightly.yml` | the third tier: the eval set against a real provider, on a schedule, no threshold |

## What was measured

**A recording written for a human cannot be replayed.** Chapter -1's recorder
has said since it was written that "chapter 14 replays it". It stored this:

```json
{"id": "call_oKpFObxlScZmHw9WBqCTAaOu", "name": "apply_patch"}
```

The edit — the payload a bug is usually *in* — was never written down, and a
429 was stored as a sentence rather than as the status and body a second
attempt would see. Both are one field each, and neither can be added
retroactively: `load()` refuses a recording made before this chapter instead of
filling the gap with `{}`.

**The tool side needs no workspace.** Request N+1 contains the results of turn
N, so `recorded_tools()` recovers every tool output from the recording itself.
A replay touches no files, starts no processes and needs no key.

**The eval harness found a bug in the product on its first real run.**
`ShellSession` took its working directory from `os.getcwd()`, and
`tool_context(root=X)` never passed anything else — so `read_file` and
`apply_patch` were bounded by `root` while `run_shell` was bounded by wherever
the process started. In the CLI those are the same directory, which is why
twelve chapters never noticed. Pointed at a two-file workspace, the agent ran
`ls -R`, found *this repository*, and tried to patch
`src/minicodex/evals.py` — the file holding the checks it was being graded
against. Chapter 4's containment (F04-12) refused the write.

**Chapter 13's sentence survives a wider task set.** Six tasks, three samples
per arm, both providers:

```
  task                   without          with
  add-function               3/3           3/3
  fix-failing-test           3/3           3/3
  read-and-answer            3/3           3/3
  leave-it-alone             3/3           3/3
  ambiguous                  0/3           3/3      (openai; ollama 0/3 -> 0/3)
  two-files                  3/3           3/3
  TOTAL                    15/18         18/18
  made worse by the sentence: (none)
```

...with a caveat the table cannot state: five of six tasks are at 3/3 in both
arms, so this set can detect a *break* and has no room to show an improvement,
and at three samples the smallest visible regression is one task in three.

The one anomaly worth recording is one this set could not have resolved. Across
18 later samples of `add-function` in the `with` arm, exactly one asked a
clarifying question on a task that is not ambiguous — and a deliberate
follow-up (10 samples per arm on that task alone) reproduced it **0/10**,
against 0/13 in the `without` arm. One event in 31 runs is not attributable to
anything. That is the honest end of this measurement, and it is also the
measurement's own verdict on itself: the effect worth worrying about here is
smaller than the resolution anyone is going to pay for.

**Both judges preferred the confidently wrong answer, 10/10 each.** Asked which
of two answers better says what `calc.py` defines, with a correct two-line
answer against a fluent one that invents a third function:

```
gpt-4o-mini      picked the wrong answer 10/10   (order-invariant)
gemma4:31b       picked the wrong answer 10/10   (order-invariant)
```

On the pair where *both* answers are correct, the two judges disagree
unanimously and in opposite directions — gpt-4o-mini takes the long one 10/10,
gemma4 takes the short one 10/10. Neither shows position bias; both have a
style.

**A text assertion looks perfectly reliable on one provider.** Five samples of
the same task: gemma4 returned the identical 50-character sentence 5/5, so
`assert "I have added" in answer` would have passed every time. gpt-4o-mini
produced five different answers between 133 and 224 characters, one of which
did not do the task at all.

## Deliberately not done

- **No LLM judge in the harness.** Measured above; a judge is for questions a
  program cannot check, and every check in this task set is a question a
  program can check.
- **No replay of a compacted session.** The summariser calls the model
  directly and that call never reaches the recorder, so a recording with a
  `compaction` event is refused rather than half-replayed. Routing the
  summariser through the recorder is real work and is not done here.
- **No reconstruction of pre-chapter-14 recordings.** The arguments *are*
  recoverable from the following request, which is how `recorded_tools()`
  works — but the last turn's are not, and a reconstruction that silently
  differs from the original is chapter 7's F07-09 with no version field.
- **No score threshold in CI.** `nightly.yml` prints a table. A gate on a
  non-deterministic number is a red that gets ignored, and F14-02 is about
  what an ignored red does to every other red.
- **`minicodex replay` does not reproduce AGENTS.md, the plan or the clock.**
  Anything the loop reads from the machine rather than from the model is not
  in the recording, and shows up as drift rather than as itself.
