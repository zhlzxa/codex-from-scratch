# minicodex — chapter 20: the sandbox (permission was a judgement, not a boundary)

For nineteen chapters `policy.judge_command` read a command, decided how risky
it looked, and either ran it or asked a human. What it never did was **stop**
anything: once the verdict was `ALLOW`, the string went to a subprocess with
the whole filesystem and the whole network behind it.

Chapter 5 knew, and wrote both halves down in its own fault list:

- **F05-04** — `python -c "open('x','w')"` writes in `read-only` mode.
  *"Tokenising is perfect here and tells you nothing — the danger is inside a
  string argument."*
- **F05-05** — `curl` exfiltrates. Classified `NETWORK`, and *"recognised
  rather than blocked … a real network policy needs the OS sandbox this
  chapter does not build."*

This chapter builds it, on Linux, with bubblewrap. Measured, not asserted:

```
  no sandbox        exit=0  file exists: True
  read-only         exit=1  file exists: False
                    OSError: [Errno 30] Read-only file system: '/tmp/.../outside.txt
  workspace-write   exit=0  wrote inside: True
  workspace-write   exit=1  wrote outside: False
```

```bash
uv sync --all-extras
uv run pytest                                 # 1822 tests
uv run python scripts/check_layers.py
uv run python probe_mutations_ch20.py         # 26 mutations, 0 survivors

# the boundary itself only means anything on Linux:
wsl -d Ubuntu -- bash -lc "cd $(pwd) && python3 probe_sandbox.py all"
```

## What is new

| Path | What it does |
|---|---|
| `src/minicodex/sandbox.py` | `SandboxSpec` → a `bwrap` command line. Pure function, so the whole module is testable on a machine with no kernel to test against |
| `src/minicodex/tenancy.py` | `Owner` — the seam that makes "whose memory is this" a question the type system can hold |
| `probe_sandbox.py` | six sections, all of them Linux-only and honest about it |
| `probe_mutations_ch20.py` | 26 mutations, chosen to attack the claims rather than the syntax |

Two lines changed in `shell.py`, one branch in `policy.py`. The sandbox ships
**off** (`ShellSession(sandbox=None)`), which is why all 1822 tests — nineteen
chapters of them — still describe the program they were written for.

## The payoff nobody expects from a sandbox: fewer prompts

`_SHELL_ALLOWED_BY_MODE` grants `workspace-write` exactly `{READ}`. Writing
asks, **even in the mode named for writing**, because *where* the bytes landed
was unknowable — `rm -rf build` and `rm -rf ../../..` are the same shape.

Under a boundary the kernel answers that, so a second table applies:

| mode | unconfined | confined |
|---|---|---|
| `read-only` | `READ` | `READ`, `INTERPRETER` |
| `workspace-write` | `READ` | `READ`, `WRITE`, `INTERPRETER` |

F05-06 measured approval fatigue at 2–4 prompts per task. The prompts this
removes are exactly the ones that existed because the answer was unknowable —
and those are the ones a user learns to click through without reading.

`NETWORK` stays out of both. `--unshare-net` makes a network command
*harmless*, not *successful*, and F05-09 measured what a silent denial costs:
the model reads it as a broken environment and retries forever.

## What the measurements changed

**The bind set is measured, not chosen.** "Bind only the workspace" is not a
tighter sandbox, it is a broken one:

```
  exit=1   --ro-bind <ws> <ws>                                bwrap: execvp /bin/sh: No such file
  exit=1   --ro-bind <ws> <ws> --ro-bind /bin /bin            bwrap: execvp /bin/sh: No such file
  exit=0   --ro-bind <ws> <ws> --ro-bind /bin /bin
           --ro-bind /lib /lib --ro-bind /lib64 /lib64        ok
  exit=0   --ro-bind / /                                      ok
```

The interpreter has to be reachable or nothing runs. Bind `/` read-only, then
punch writable holes — and that shape fails safe: forgetting a writable path
denies a write, where forgetting a readable path denies *everything*.

**bwrap's failures and the command's failures share an exit code.**

```
  exit=1   chdir outside the bind set     detected as: wrapper
  exit=1   bind a source that is absent   detected as: wrapper
  exit=1   interpreter unreachable        detected as: wrapper
  exit=1   an unknown flag                detected as: wrapper
  exit=42  the command exits 42           detected as: command
```

`bwrap: Can't chdir to /tmp/elsewhere: No such file or directory` — for a
directory that exists perfectly well on the host. The message names the wrong
cause, and exit 1 is what a command that ran and failed also returns. So
`setup_failure` reads the prefix, because the exit code cannot.

**Killing bwrap takes the tree.** `--unshare-pid` makes it pid 1 inside the
namespace: five `sleep 300` descendants before `killpg`, zero after. Chapter
2's F02-01 timeout depends on this.

## Compared with codex

| | codex | here |
|---|---|---|
| backends | `SandboxType::{MacosSeatbelt, LinuxSeccomp, WindowsRestrictedToken}` (`sandboxing/src/manager.rs:35-40`) | Linux only, and refuses rather than pretends elsewhere |
| Linux mechanism | landlock ABI V5 (`linux-sandbox/src/landlock.rs:140`) + seccomp + bubblewrap | bubblewrap only |
| bwrap binary | bundled, extracted at runtime (`linux-sandbox/src/bundled_bwrap.rs`) | `shutil.which` — packaging is not a teaching problem |
| network vs filesystem | separate types: `NetworkSandboxPolicy{Restricted,Enabled}` beside per-path `FileSystemAccessMode{Read,Write,Deny}` (`protocol/src/permissions.rs:78-118`) | **same split**, and copied deliberately |
| policy language | Starlark DSL, `prefix_rule`/`network_rule` (`execpolicy/`) | three modes, no DSL |
| egress control | MITM proxy, per-domain rules, credential broker (`network-proxy/`, 15k lines) | on/off |

Where this book goes **beyond** codex: nothing. Every mechanism here is a
subset. Where it falls short is the whole right-hand column.

## Whose data is it

`web/runtime.py` documented one shared global memory directory as *correct*:

> Memory is global — `~/.minicodex/memories` … so every workspace this server
> hosts shares one, exactly as every project on one machine shares one in codex.

Every clause is true and the conclusion leaks. "Every project on one machine"
is one person's projects. "Every workspace this server hosts" is everybody's.
The port was faithful; the trust model moved underneath it, and nothing in the
type system noticed because there was never a type for *who*.

`Owner` is that type. `Owner()` — the default — resolves to exactly the paths
chapters 16 and 17 already use, which is asserted rather than hoped:

```python
assert DEFAULT_OWNER.memories() == DEFAULT_MEMORY_DIR
assert DEFAULT_OWNER.jobs_db() == DEFAULT_JOBS_PATH
assert DEFAULT_OWNER.merge_lock() == MERGE_LOCK
```

## Deliberately not done

- **Authentication, sessions, quotas.** This is the *seam*, not a multi-user
  system. `Owner.key` is trusted completely by whoever constructs it; a server
  deriving one from an unauthenticated header has moved the leak, not closed
  it. Chapter 22.
- **macOS and Windows.** codex has both. Refusing beats a `NoSandbox` that
  keeps the mode's name and loses its meaning (F20-02).
- **A policy language.** Three modes and a bind set, not `execpolicy`'s
  Starlark.
- **Per-domain network rules.** `--unshare-net` is on or off. codex's
  `network-proxy` — MITM, per-domain permissions, a credential broker that
  keeps real secrets out of the sandboxed process entirely — is the shape this
  would grow into, and it is 15,000 lines.
- **`--info-fd`.** `setup_failure` reads a `bwrap: ` prefix, which a command
  whose own output starts that way would defeat. A separate status pipe is
  strictly better and costs a second reader in `shell.run`, for a case
  measured at zero occurrences. Left as an exercise.

## Testing something a kernel enforces

Most tests here assert on **argv**: `wrap()` is a pure function, so "does
`read-only` bind anything writable" is a question about a list of strings.
Those run on Windows, where no sandbox could ever run — which is the point.
The mutation that deletes `--unshare-net` is caught on a machine with no
network namespaces.

The few that need a real kernel are guarded by `needs_bwrap`, whose reason
names **the missing thing** rather than the platform:

```
SKIPPED [1] no bwrap on this machine -- see probe_sandbox.py, which measures the kernel
```

"skipped: not Linux" is a sentence that stops anyone asking why the Linux CI
box skipped it too. A security test that skips quietly is worse than one that
does not exist: the absent one is a known gap, the quiet one is a green tick
over a hole.

**Which is exactly what happened here.** The first mutation run had two
survivors, both in `shell.py` — "the sandbox is ignored, so a confined session
is not one" and "the wrapper's own failure reaches the model as a command
result". Every test that would have caught them needed `bwrap`, so on the
machine this book is written on they were all skipped and the mutations walked
through a green suite. That is chapter 19's fault repeating one chapter later.
The fix was a `FakeSandbox` that records and substitutes: whether bubblewrap
isolates is a question for a kernel, but whether `shell.run` ever calls it is
a question about two lines of Python, and answering the second must not
require the first.
