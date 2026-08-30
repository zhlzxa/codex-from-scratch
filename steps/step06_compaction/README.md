# minicodex — chapter 6: context compaction

The agent from chapter 5 works until the conversation stops fitting. This one
notices that it is about to stop fitting, and replaces the middle of the
transcript with a summary of it.

```bash
uv sync --all-extras
uv run pytest

# Compaction is off unless you say how big the window is. Nothing in this
# program can discover that number, and a guessed one is a silently wrong budget.
uv run minicodex ask "..." --provider openai --context-window 8000
```

## What is new

| Path | What it does |
|---|---|
| `src/minicodex/compaction.py` | where a cut is legal, what may never be cut, and what replaces what was |
| `src/minicodex/tokens.py` | how big a request is before it is sent, and the correction learned from what it actually cost |
| `src/minicodex/prompts/compaction.md` | the six sections a summary must have, and which block is being replaced |
| `src/minicodex/model.py` | asks for `stream_options.include_usage`, survives the chunk that arrives with it |
| `src/minicodex/agent.py` | compacts between turns, never inside one; feeds reported usage back into the estimate |
| `tests/test_properties.py` | the shape questions, over generated histories |

## The measurement this chapter turns on

Cutting one eight-message history at every possible index, posted to
gpt-4o-mini (`probe_cut_points.py`):

```
 cut  kept                                          status
   0  S U A[a1] T[a1] A[b2] T[b2] A[c3] T[c3]       200
   1    U A[a1] T[a1] A[b2] T[b2] A[c3] T[c3]       200
   2      A[a1] T[a1] A[b2] T[b2] A[c3] T[c3]       200
   3            T[a1] A[b2] T[b2] A[c3] T[c3]       400
   4                  A[b2] T[b2] A[c3] T[c3]       200
   5                        T[b2] A[c3] T[c3]       400
   6                              A[c3] T[c3]       200
   7                                    T[c3]       400
```

The 400s are the easy half. The user asked *which Python version this project
supports*: cut 0 answers it ("the project supports Python 3.10 and above"), and
cut 6 answers a different question ("you are using Python version 3.13.0")
because the message stating what was asked is gone. Both are 200.

`boundaries()` returns `(0, 1, 2, 4, 6, 8)` for that history — the 200 set
exactly. `Protected` is the other half of the answer.

## The estimator is wrong, and knows it

`chars // 4` against reported `prompt_tokens` (`probe_tokens.py`):

| | chars | est | actual | est/actual |
|---|---|---|---|---|
| long prose | 4200 | 1050 | 1008 | 1.04x |
| source code | 1760 | 440 | 767 | 0.57x |
| shell output | 2281 | 577 | 1185 | 0.49x |
| json | 1420 | 355 | 807 | 0.44x |
| CJK | 560 | 140 | 327 | 0.43x |

Prose is 4.2 chars/token, JSON is 1.8. No divisor works, and every error is in
the direction that lets the window overflow before compaction fires. So the
number the server already reported is used as a correction — within 4% from the
third turn on (`probe_calibration.py`).

That number has to be asked for: a streaming request returns **zero** usage
chunks without `stream_options.include_usage`. Switching it on makes the last
chunk arrive with `"choices": []`, which turns chapter 1's `chunk["choices"][0]`
into an `IndexError` after the answer has already streamed.

## Measured against a real model

Five facts planted in a transcript, checked in the compacted history
(`probe_summary.py`, gpt-4o-mini, 5 samples):

| | naive "summarise this" | the six-section prompt |
|---|---|---|
| facts kept | 15/25 | 23/25 |
| the flag discovered by a failing test | 0/5 | 3/5 |
| the one remaining task | 0/5 | 5/5 |

Across six generations of summarising the summary, the surviving four facts
stayed at four: decay was a single loss at generation 1, not a slide.

**F06-05 did not reproduce.** Neither prompt made the model redo finished work
in 10/10 continuations — it read the file it had already written before doing
anything else, which is verification, not repetition.

## Deliberately not done

- no per-modality token estimation — `estimate_messages` **raises** on
  non-string content rather than counting an image as free
- the summariser is one model call with no retry; if it fails, the fallback is
  a note telling the model what it just lost
- no cross-session compaction, no reloading a summary from disk (chapter 7)
- rule of thumb constants (`0.75` trigger, `0.45` target, 700-token summary)
  are not tuned against anything but a handful of sessions
