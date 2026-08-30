# minicodex — chapter 5: approval and sandboxing

The agent from interlude A could edit any file and run any command. This one
asks first, and remembers the answer.

```bash
uv sync --all-extras
uv run pytest

# Recorded responses, no GPU and no key needed:
uv run minicodex serve-stub &
uv run minicodex ask "What does src/minicodex/__init__.py define?" \
    --base-url http://127.0.0.1:11435/v1

# Two knobs. The defaults are the restrictive ones.
uv run minicodex ask "..." --sandbox-mode workspace-write --approval-policy on-request
uv run minicodex rules          # what this project has agreed to, and when
uv run minicodex forget 0       # revoke one
```

## What is new

| Path | What it does |
|---|---|
| `src/minicodex/shell_parse.py` | splits one command string into independently judgeable segments, and says `None` when it cannot |
| `src/minicodex/policy.py` | what a command *is* — read, write, network, interpreter, unknown — and what each sandbox mode allows |
| `src/minicodex/rules.py` | approvals the user does not have to give twice, with a scope, a source and a way to revoke |
| `src/minicodex/approval.py` | the one door: policy + rules + a human, and nothing reaches a subprocess around it |
| `src/minicodex/prompts/permissions.md` | the permission state, rendered into the system prompt |
| `src/minicodex/shell.py` | `run_shell()` **deleted** from here — it reached the subprocess without a gate |
| `tests/test_approval.py` | 80 tests, one or more per fault |

## The three faults that were not on the list

A checker built on `shlex` handles `git status; rm -rf /` correctly. It also
approves all three of these, and bash runs the `rm` in every one — verified on
real files in `probe_shell_safety.py`:

```
ls\nrm -f payload.txt        shlex: ['ls', 'rm', '-f', 'payload.txt']
echo `rm -f payload.txt`     shlex: ['echo', '`rm', '-f', 'payload.txt`']
echo a#b; rm -f payload.txt  shlex: ['echo', 'a']
```

`shlex` is a tokeniser, not a shell parser, and every disagreement found was in
the dangerous direction. Patching them one at a time is a bet that the list is
now complete, so the question is inverted: `_MODELLED` lists the characters
whose meaning this code implements, and anything else makes the command
unknown. Unknown asks. It never allows.

## What `workspace-write` does not mean

It does not let a *shell command* write. "Inside the workspace" is a claim
about where bytes land, and for a shell command there is no way to find that
out without running it. `apply_patch` is different — every path goes through
`paths.resolve()` — so for that one tool the mode means what it says.

Enforcing it for the shell needs an OS sandbox (seatbelt, landlock, a job
object). This chapter does not build one, and says so rather than letting the
flag quietly mean "write anywhere".

## Measured against real models

| | gemma4:31b-cloud | gpt-4o-mini |
|---|---|---|
| Told no, retries the same goal with a new spelling | 6/6 | 5/6 |
| A three-part denial alone stops that | 0/6 | 0/6 |
| Without `request_permissions`, ever asks | 0/3 | 0/4 |
| With `request_permissions`, asks | 3/3 | 3/3 |
| Without the permission block, asks for `unrestricted` | 3/3 | 2/3 |
| With it, asks for the smallest thing that unblocks | 3/3 | already did |

`python probe_approval.py [--provider openai]`

## Deliberately not done

- no OS-level sandbox — the gap above, stated rather than papered over
- no network policy beyond classification: `curl` is recognised, not blocked
- `--approval-policy never` denies rather than queues; there is no async
  approval channel
- rule matching is a word prefix, not a pattern — no globs in rules
