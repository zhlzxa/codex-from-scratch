# Design History and Measurements

This file absorbs the narrative, fault numbers and measurements that were
removed from source comments. The code keeps only live constraints; this
keeps *why the code is shaped the way it is*. Organised by module — search
with `Ctrl+F` for a fault number (F05-04, …) or a LIVE number.

---

## shell (`src/minicodex/shell.py`)

- **One fresh subprocess per command; `cd` intercepted in Python.**
  Measured: after `cd /tmp`, a second command's `pwd` still returns the
  original directory (the subshell dies with the command). Persistence of
  `cd foo && pytest` is out of scope; a genuinely persistent shell (one
  long-lived process, commands piped to stdin) is much larger machinery this
  class does not claim.
- **Environment allowlist (`ENV_ALLOWLIST`)**: `subprocess` inherits all of
  `os.environ` by default, including `OPENAI_API_KEY`. `SYSTEMROOT` must be
  on the list or Windows Winsock fails to initialise and
  `python -m pytest` dies with WinError 10106 — the model reads the
  traceback, decides it is an environment problem, and reports the task
  done. A too-narrow allowlist does not fail closed here; it fails as
  somebody else's bug.
- **Timeout kills the process group, not just the shell**: with
  `shell=True` the command is the shell's child; killing the shell alone
  leaves orphans (measured with a `sleep 3600` outliving its timeout).
  `killpg` does not exist on Windows — a documented, unfixed gap.
- **The `proc.wait()` hang**: CPython's `_call_connection_lost` is the only
  resolver of `wait()`; an abandoned pipe never reaches EOF. Fix: close the
  pipe before waiting. Measurement (`yes | head -c 2000000` against a
  1,000,000 ceiling, 12 runs each): 3.10.20 0/12 hang, 3.11.15 12/12,
  3.12.3 4/12, 3.13.13 4/12; after closing the pipe, 0/12 everywhere. A
  read-timeout wrapper on `wait()` was tried and rejected: still 3–12/12
  hangs, and it paid the full timeout each time.
- **`read(n)` not `readline()`**: `readline()` raises `LimitOverrunError`
  after filling its internal 64KB buffer without a newline (measured with a
  40,000-character `print(..., end="")`).
- **Output clipping**: keep head and tail, drop the middle — a failing
  pytest run puts the traceback at the top and the summary at the bottom;
  tail-only truncation discards the line that explains the failure.
- **Trailing `&` refused**: measured — it returns in milliseconds while the
  process keeps running, outside every tracking/killing mechanism here.
  Supporting it needs a job registry and polling; refusing is honest.
- The schema sentence "commands are killed after N seconds" changed nothing
  (6/6 runs still sent two-minute commands); the message after the kill is
  what redirects the model.

## sandbox (`src/minicodex/sandbox.py`)

- Before this module, `policy.judge_command` was the only "sandbox" — a
  classifier, not a boundary. F05-04 (`python -c "open('x','w')"` writes in
  read-only mode) and F05-05 (curl exfiltrates) were on the fault list with
  the note "a real policy needs the OS sandbox this chapter does not build".
- **Why bwrap, not landlock directly**: landlock from Python means ctypes
  against raw syscall numbers and hand-assembled seccomp BPF. bwrap's
  arguments *are* the policy — readable, testable, printable.
- **The bind set is measured, not chosen** (`probe_sandbox.py binds`):
  binding only the workspace gives `execvp /bin/sh: No such file` (no shell
  at all). The working shape is `--ro-bind / /` plus writable holes, which
  also fails safe: a forgotten writable path denies a write; in the inverse
  design a forgotten readable path denies everything.
- **`_ROOT = "/"` must be a string literal**: `str(Path("/"))` is `"\\"` on
  Windows, so argv built on Windows named a directory bwrap has never heard
  of — caught by the bind-set assertion test.
- **`--unshare-pid` makes the timeout reliable**: bwrap is pid 1 inside the
  namespace; killing pid 1 takes the whole namespace with it
  (`probe_sandbox.py kill`: 5 descendants before, 0 after).
  `--die-with-parent` provides the same guarantee from the other end.
- **`chdir_is_reachable`**: with `--ro-bind / /` the only unreachable chdir
  is one that does not exist. An unreachable `--chdir` makes bwrap print
  `Can't chdir to ...` and exit 1 — indistinguishable from a failing
  command; `setup_failure` is what tells those apart.
- **`setup_failure` heuristic edge**: all four known bwrap setup failures
  exit 1 while `sh -c 'exit 42'` propagates 42 — exit codes cannot tell
  them apart, so the `bwrap: ` prefix decides. A command whose own output
  starts with `bwrap: ` would be misread; `--info-fd` is strictly better
  and left as future work (0 misfires measured).
- **What enforcement buys**: with the kernel boundary present,
  `workspace-write` can allow WRITE and INTERPRETER (the confined table in
  `policy.py`); F05-06 measured approval fatigue at 2–4 prompts per task,
  and the removed prompts are the ones nobody was reading.

## policy (`src/minicodex/policy.py`)

- **`SandboxMode` / `ApprovalPolicy` separation** borrowed from codex: the
  first answers "what does the task need", the second "is there a human at
  the keyboard". CI wants read-only+never; interactive development wants
  workspace-write+on-request. One merged setting makes four useful
  combinations unreachable.
- **String literals, not classes** (three values, no behaviour): dispatch
  is a `match` in one place; a class per value would be three files where
  three strings do. Reconsider at the third repetition with real behaviour.
- **`INTERPRETERS` is the non-allowlist exception**: it exists to stop a
  rule from being remembered on an interpreter prefix — the same judgement
  as codex's "Banned prefix_rules".
- **git is not one command**: `status` reads; `push --force` rewrites
  someone else's history. Subcommand classification mirrors codex's
  `is_safe_command.rs`; `-C`/`-c` and friends change what git operates on
  or executes, and a checker that stops at the first bare word approves
  both.
- **`_SHELL_ALLOWED_BY_MODE` grants `workspace-write` only READ**, on
  purpose: where a shell command's bytes land is unknowable before running
  it (`rm -rf $HOME`/`build`/`../../..` are the same shape). The confined
  table relaxes this only when the kernel enforces it.
- **NETWORK stays out of the confined table**: under `--unshare-net` a
  network command cannot reach anything but *will fail*, and the model
  reads that as a broken environment and retries forever (measured cost,
  F05-09). Asking gives the user the chance to turn the network on.
- `apply_patch` goes through `paths.resolve()` (symlinks and `..` resolved,
  anything outside the root refused) — the only write path where "inside
  the workspace" is provable in advance.

## approval (`src/minicodex/approval.py`)

- **Single entry point**: no second route to a subprocess — one rule
  enforced once beats eight call sites maintaining it separately.
- **Remembered rule shape** (program + up to two subcommand words) follows
  codex's examples (`["cargo","test"]`, `["gh","pr","check"]`): a single
  verb is too broad, full arguments never match the next invocation.
- **`CliApprover` uses `to_thread`**: `input()` blocks, and a blocking call
  in `async def` stalls the loop (same rule as ch1's `read_file` and ch2's
  `subprocess.Popen`).
- **Denial messages do not name `request_permissions` under
  policy=never**: measured — a model told to call it ran it as a *shell
  command* instead. Naming a capability is an instruction to use it; the
  `can_request` branch in `permissions_block` is the same rule (2/3
  spurious calls when the tool was absent).
- **`session.mode` mutability** is what makes `request_permissions`
  possible: freezing and rebuilding would thread a new object back through
  every tool handler.

## subagent (`src/minicodex/subagent.py`)

- **Structural difference from codex (two collaboration models)**: codex's
  spawn returns immediately with an agent id; children run in the
  background and the parent polls `wait_agent` (plus send_input /
  resume_agent / close_agent; up to 4 concurrent per session by default;
  `agent_max_depth` default 1). Here `spawn_agent` blocks until the child
  finishes and the tool output *is* the result. Blocking is what preserves
  the "every issued call answered exactly once" invariant in one process
  and one event loop. Default depth: codex 1, this implementation 2.
- **Never inherit the parent's history**: 4476 vs 638 tokens on the same
  eight-item task (7x), and the contents of every file the parent read
  pours into a conversation that never asked.
- **Unbounded self-spawning, measured**: 830,400 nesting levels in 59
  seconds; the `wait_for` that should have stopped it died first
  (`RecursionError` inside the loop's own timer callback).
- **`ensure_future` + `shield`, not a bare `wait_for`**: `Agent.run` catches
  `CancelledError` and returns a `RunResult`, so a bare `wait_for` cancels
  the child and returns its value — no `TimeoutError`, the timeout silently
  becomes an empty answer. Pinned by
  `test_F10_07_a_bare_wait_for_would_have_returned_an_empty_answer`.
- **3.10 portability**: the builtin `TimeoutError` and
  `asyncio.TimeoutError` are unrelated classes on 3.10; `except
  TimeoutError` never fires there and a hanging child is never stopped.
  Keep the asyncio spelling.
- **`ModelFailed` translation**: a provider 429 inside a child used to
  reach the *parent model* as a raw traceback plus a JSON blob. No
  `_stop(task)` on that path: the task already finished with the
  exception, and `_stop`'s await would re-raise it.
- **Where the width budget comes from**: with only a depth limit, a model
  that spawns every turn cost 72 model calls (8 turns × 8-turn children).
  The `children` count undercounts in-flight ancestors: bounded, not exact.
- **`constraints` go in the system message, not the task text**: appended
  to the task, 3–4 of 5 runs violated them; as a system note, 0/5.
  `expected_output`: without a shape, median 2331 chars (gpt-4o-mini);
  with one, 503 — but the ceiling is `MAX_TASK_RESULT_CHARS`, not the
  wording (603–2711 on larger tasks with the same instruction).
- **No `spawn_agent` schema at the bottom depth** rather than give-and-
  refuse: a prompt naming a tool the policy forbids makes the model call it
  (2/3) and spend a turn finding out.
- **Footprint**: a spawn touches whatever its child touches — unknowable —
  so STATEFUL. Treating spawn as read-only lost one of two concurrent
  edits 29–30 times out of 30.
- **No MCP tools for children**: a remote tool's Footprint is a promise
  made by someone else's code.
- **Children get their own session files** (`parent` field links them):
  two writers on one rollout are refused by the O_EXCL lock — sharing a
  file raises before the child's first turn.
- `_stop`'s trailing `return ""` is not dead code: today `Agent.run`
  swallows cancellation and returns a `RunResult`; if that changes, the
  await raises, `suppress` catches it, and the last line runs. Both paths
  correct.

## history / rollout / recorder

Three projections of one run — not duplication:

- `History.add_*` ← every accepted fact (invariant: every call answered
  exactly once); the observer hook feeds `RolloutWriter.append` (durable,
  resumable).
- `Recorder.record` ← every event crossing the model boundary
  (request/response/model_failure), called by the loop directly.
- A 429 retry, as an example: the rollout has only the successful turns
  (failed attempts do not exist in the session file); the recorder has the
  full attempt sequence (status/headers/body/request_id). Conversely the
  rollout owns `meta` and `mark` (drop-tail and compaction baselines), and
  its items replay back into a legal conversation via `_load_item` +
  `History.add_*`.
- **Two reasons not to merge them**: (1) different stability contracts —
  the rollout format has a version-migration promise, the recorder none;
  (2) different failure models — the Recorder swallows OSError, the
  RolloutWriter must raise on lock conflicts.
- Accepted costs: the recorder re-records the full request every turn
  (O(n²), required by byte-exact divergence checks); the config event and
  SessionMeta duplicate each other.
- **rollout version 2**: v1 stored parsed `arguments` instead of the
  model's `raw_arguments` string, so re-rendering a resumed history
  produced different bytes (valid JSON, same meaning, different string).
  Old files migrate through `_migrate`; the re-encoding is not the original
  string (key order and whitespace are ours), recorded rather than
  pretended away.
- **Additive fields do not bump the version** (`parent`): unknown keys
  dropped, missing keys default — readable both directions. F07-09 needed a
  bump because the *meaning* of an existing field changed.
- **Two-writer corruption, measured**: 245/131,818 lines broken, records
  belonging to two sessions — refused with an O_EXCL lock file (which
  records the pid, for a human to arbitrate).
- **One JSON document per line, read line by line**: the first bad line
  ends the file, keeping a real prefix over a plausible fiction. Tearing
  was measured at 0 in 5 forced kills (`probe_rollout.py`) — the guard is
  written and honestly labelled as insurance.
- **`newline=""`**: Windows text mode turns `\n` into `\r\n`, and a torn
  `\r\n` pair is the one corruption concurrent writers actually produced.
- **`history()` rebuild rule**: not "find the last complete turn" — replay
  everything, remember the last point with nothing outstanding. The cut
  falls out of `History`'s own invariant, so it cannot disagree with it.
- **`resolve("last")` skips sub-agent sessions**: one parent + two children
  = three files, and "resume last" would silently continue a child. Still
  resumable by id; only excluded from the guess.
- **`interrupted_note` wording, measured** (gpt-4o-mini; "verify" = reads
  the file before touching it): no note 0/13; placebo note ("resumed from a
  file on disk") 0/5; four-sentence first version 10/19; one sentence, no
  count 10/11; one sentence + the count 6/6 (shipped). The placebo arm is
  what makes the rest mean something: the note's presence is not the
  mechanism, the warning is.

## memory (`src/minicodex/memory*.py`)

- **`--memory` / `--remember` are two switches** (F17-11): reading and
  writing are separate consents; codex asks the same question with an
  `Enable memories?` dialog and a `default_enabled: false` feature flag.
  Default-on would silently change every subsequent request.
- **`--dedicated-tools`** is codex's `memories.dedicated_tools`, also
  default false.
- **`MemoryError_`** (trailing underscore, avoiding the builtin) was
  renamed `MemoryLoadError`.
- **The pipeline runs at the start of the next session** (F17-01): the same
  work at the *end* of a run is measured seconds between the answer and the
  user's prompt. Alongside the run it costs nothing anybody waits for, and
  it consumes *previous* sessions (the current one excluded by name).
- **Rate-limit headroom is passed as a question, not a number**: the task
  is created before any request has seen a rate-limit header; passing the
  number means passing `None` forever. `None` (provider reports nothing)
  proceeds — the alternative switches the feature off on every server that
  reports nothing, ollama included. Below 25% the whole run yields (F17-02:
  the user's task must not get the 429 the memory writer earned).
- **Lease, not flag** (F17-03): cancellation is a normal outcome; the claim
  stays in the database with a lease, and lease expiry is how the next run
  picks the work up. Nothing needs undoing — the only write is the last
  thing the pipeline does.
- **The git baseline** (F17-08): the merge step needs to know what a human
  changed, and `git diff` against "since this program last committed" is
  the direct answer; a shadow copy + diff is exactly a shadow copy + diff.
  `user.email`/`user.name` are configured locally in the repo (CI images
  have no global identity; the failure appears at the first commit).
- **`usage.json` is rebuilt, not subtracted** (F16-08 shape): entries can
  also leave because the merge stopped emitting them, and that path leaves
  no row in `pruned.dropped`. Subtracting accumulated counts for headings
  that no longer exist.
- **`raw/` files declare themselves temporary** (F17-04): codex writes the
  same warning; an intermediate product that does not say so is one
  somebody starts maintaining.
- **Citation and behaviour signals** (F16-14/F17-13): citations are the
  policy signal, measured unreliable; `usage_kinds_touched` is the
  behavioural signal and does not depend on the model volunteering a
  citation. Printed together, used apart — the unreliable one decides (see
  `prune`), and printing both side by side is what made that visible.
- **Memory per-owner, skills per-project**: an earlier text claimed
  "every workspace this server hosts shares one, exactly as every project
  on one machine shares one" — the first half was true and the second was
  a data-leak conclusion. "Every project on one machine" is one person's;
  "every workspace this server hosts" is everybody's. `Owner` was built
  before anyone could answer "whose is this"; multi-user tenancy is that
  answer arriving: one parameter with a default.

## Mechanism comparison with codex

Baseline: upstream codex, compared file-by-file. Three classes: **A =
deliberate deviation (recorded, not changed)**; **B = philosophical
difference (valid in both directions)**; **C = worth aligning or at least
documenting**.

### A — deliberate deviations

| # | Mechanism | codex | minicodex | Basis |
|---|---|---|---|---|
| A1 | apply_patch interface | freeform grammar (`*** Begin Patch`), Add/Delete File and move | JSON `{edits:[{path,old_text,new_text}]}`, replace only | freeform needs the Responses API custom tool; chat completions cannot. Real loss: no "create file" semantics |
| A2 | shell | `unified_exec`: persistent PTY, `exec_command(yield_time_ms)` + `write_stdin` | fresh subprocess per command; `cd` intercepted in Python; backgrounded `&` refused | teaching trade-off; `cd x && y` non-persistence documented |
| A3 | compaction | replacement (`replace_compacted_history`; remote compaction, hooks, manual) | replay-based (plan → cut → summary → History rebuild; rollout marker) | the necessary shape of append-only history; `boundaries()` (real 200/400 table) is an original contribution |
| A4 | retry classification | semantic enum (`is_retryable` over `CodexErrorDetails`) | provider `code` string table | the chat-completions ecosystem has no semantic enum; equivalent capability. codex also has a WS→HTTPS transport fallback this program lacks — recorded, not planned |
| A5 | ContextWindowExceeded | no retry, marked full, compaction is a separate lifecycle | `_shrink` auto-compacts and retries | scenario fit: unattended server vs interactive UI |
| A6 | tool_search surface | global (extension/plugin/MCP defer; `defer_loading` on ToolSpec); MCP parallelism opt-in + readOnlyHint | MCP tools only (local never defers); token-budget trigger; MCP always STATEFUL | scale; the mechanism is the same, this one more conservative |
| A7 | memory/skills constants | — | all verified true: `SUMMARY_TOKEN_BUDGET=2500` (ext/memories/src/lib.rs:16), `MAX_ROLLOUTS_PER_STARTUP=2` (config/types.rs:46), `MIN_RATE_LIMIT=25%` (:49), skills `MAX_DESCRIPTION=1024` (ext/skills/render.rs:21), citation format `<oai-mem-citation>` (read_path.md:75-80), behaviour/citation signal split (usage.rs) | the best-disciplined comparison in the project |
| A8 | skills tool | `skills.list/read` (authority/package/resource) | default path is `read_file` over the catalog; `read_skill` only behind a flag (measured even, self-reported) | documented honestly |

### B — philosophical differences (code-enforced here, prose-only there)

| # | Rule | codex | minicodex |
|---|---|---|---|
| B1 | at most one `in_progress` | stated in the tool description only, unchecked | enforced in code (plan `_parse`) |
| B2 | intermediate work before completed | does not exist | `work_since_update==0 && updates>0` refused |
| B3 | step cap | none | `MAX_STEPS=12` |
| B4 | echo | fixed "Plan updated" (has a TUI panel) | echoes the full plan + remaining count (no panel; history text is the continuation) |
| B5 | does the loop read the plan | never | `unfinished_note` nudges once before stopping |

Reading: codex uses prompts + model discipline (with server/panel
backstops); this program enforces in code (append-only history, no panel,
"write the plan and immediately close it" measured harmful). The citations
are accurate and the deviations are measured — honest deviations.

### C — differences needing documentation

- **AGENTS.md role**: codex delivers it as a `role: "user"` fragment
  (`user_instructions.rs: role() -> "user"`), re-sent every turn as part of
  the world-state diff with replacement copy. This program uses
  `role: "developer"`, said once (F13-12 measurement: against a fortified
  system prompt, developer 3/3 vs user 1/3). Delivery follows each
  program's history model — replaceable context vs append-only.
- **No transport fallback** (A4): codex has WS→HTTPS
  (`responses_retry.rs:33-45`).
- **Freeform patch Add/Delete File**: possible follow-up — give
  `apply_patch` an `old_text: ""` = create-file semantic (measure first).

---

## Web console (`src/minicodex/web/`)

### Accounts and auth

- **Why not "sign in with GitHub"**: (1) OAuth 2.0 is an *authorization*
  protocol — "holding a token that reads a repo" is not "the person at this
  browser is alice"; treating one as the other is a documented bug class —
  a token minted by any other client, or lifted from any other app, logs
  in. (2) Measured: GitHub publishes no `registration_endpoint` (F21-08b),
  so a GitHub login needs the operator to register an OAuth app *before the
  first person can sign in* — a self-hosted tool that cannot start without
  paperwork on someone else's website is not self-hosted.
- **scrypt at 2^14** (RFC 7914 §2 interactive): 16 MiB per verification, so
  an attacker with a stolen `accounts.json` pays it per guess. Parameters
  are written into every stored hash — raising the cost later does not
  invalidate existing passwords.
- **Password length ≥ 10, no composition rules** — NIST SP 800-63B dropped
  them; they push people to `Passw0rd!`.
- **Unknown-account timing side channel**: a plaintext miss answers in
  microseconds, a hit in tens of milliseconds — the miss timing enumerates
  accounts, and an account key is also a directory name and a tenancy.
  Defence: a miss verifies against a dummy hash nobody knows and discards
  the answer. The dummy cache is keyed by cost parameter — a cheap dummy
  read back after a cost raise reintroduces exactly the channel this exists
  to close (caught once by the F23-06 timing test).
- **Bootstrap token**: memory-only, reissued per restart, burned on
  redemption. The alternatives are worse: a default password is a published
  credential; an unauthenticated create-first-account route makes whoever
  reaches the port the operator; a token on disk is a password file with
  the door open. Jupyter reached the same conclusion.
- **`--host 0.0.0.0` with no account refuses to start** (LIVE-03): a first
  account created over a network you do not own is not a bootstrap, it is a
  race. Login decides *who* drives the agent; the bind address decides who
  reaches the port. Removing the second because the first exists trades a
  boundary for a credential.
- **Tokens stored hashed (SHA-256, not scrypt)**: a session token is 32
  random bytes — nothing to guess — and verification runs on every request;
  scrypt's cost is for low-entropy passwords. A raw-token table on disk is
  one careless `cat` from live credentials.
- **Two TTLs**: absolute 12h (a thief cannot extend it by refreshing) +
  idle 2h (walked-away sessions end).
- **Password change revokes everything**: sessions left alive are the
  eviction the change was *for*.
- **Disable, never delete**: an account key is a directory name in two
  trees (one holding other people's OAuth tokens); removing the row while
  directories stand would let the key be handed out again and inherit them.
- **Health anonymity**: the account *count* was removed in favour of a
  `has_account` boolean — a count tells an attacker whether the bootstrap
  is closed; the frontend needs only the boolean.

### Throttle / quota / audit

- **Per (account, source)**: throttling per account alone is a free DoS on
  a chosen user. An attacker behind many sources still gets many tries —
  true of every source-scoped defence; this is a rate limit, not a
  substitute for a strong password.
- **Source = socket peer, resolved by the caller**: `X-Forwarded-For` is an
  assertion by the forwarder; trusting it with no proxy is an IP-spoofing
  primitive. The wrong configuration throttles the proxy — the correct
  failure direction for a default.
- **Lockout independent of the window** (15 minutes each): a lockout that
  ends when one attempt ages out lets an attacker pace guesses at exactly
  the limited rate. Throttle check runs before scrypt.
- **Quota charged at claim time**: a failed turn costs the same call,
  subprocess and seconds as a successful one; a success-only quota is no
  quota against retries. Turns, not tokens: `Calibration.correct()` is
  documented useless for a session's first two turns — a metered number
  with no defence. Turns are exact and known at the only moment enforcement
  can happen.
- **A restart does not forgive the last hour**: on a server somebody else
  keeps running, stop/start resets the window — and anyone who can reach
  the host can force a stop/start.
- **Audit write failures are fatal to the operation**: a dropped record is
  not less logging, it is a hole in the only after-the-fact timeline — and
  once holes are possible, every quiet record is unprovable. "Rather stop
  than lie."
- **JSONL appended, not atomically replaced**: like codex's
  message-history — a single append means a reader sees a prefix, never a
  torn record. The opposite of the accounts.json pattern (latest content
  vs all content).
- **Throttle eviction**: pairs whose window and lockout have both expired
  are swept lazily — unauthenticated traffic cannot grow the table without
  bound.

### Runtime / routes / composition

- **`_AuditedShell` is subclassed, not wrapped**: `context.shell` is read
  by attribute (cwd/timeout/sandbox) and replaced wholesale in
  `tool_context`; a wrapper would re-expose every attribute and silently
  miss one.
- **Structured exit codes**: the audit used to parse `"... (exit code N)"`
  off the output's last line — rewording or output that merely ends that
  way silently recorded a wrong code. Now `ShellSession.last_exit`.
- **Workspace validation where the string enters** (F19-09 family):
  `run_turn` used to `resolve()+mkdir` itself — a typo silently created a
  directory tree anywhere writable and ran an agent in it.
- **MCP OAuth callback lands on console routes**: upstream's handlers
  assume the agent process and the browser share a machine; a hosted
  console cannot. `state` is looked up, never trusted; a mismatched owner
  gets the same answer as an unknown state. Status polling instead of
  postMessage (popup blockers, `noopener`, early tab close all break
  postMessage).
- **`__main__`↔web duplication**: instructions/resume/provider tables were
  once two verbatim copies kept equal by tests; all three now live in core
  modules and both entry points import them.
- **A composition root does no IO**: a root that prints and writes cannot
  be called from a test — the measured cost was two assembly sites drifting
  silently (child 181,040 vs parent 76,896 request chars, 0 compactions, no
  transcript; nothing raised).

### Layers / packaging

- **check_layers rules**: tools→agent (FA-04), tools↛subagent and
  subagent↛tools (F10-06/FB-01), history↛agent (F01-08, the first cycle),
  agent_types is a leaf, `*`↛web (F19-08: the agent never grows a browser
  dependency), `__main__` and `web` are entry points. Every rule was a real
  failure first.
- **Mutation probes must actually run in CI** (F23-11): the check once
  matched the workflow's whole text, so a YAML comment naming a script
  counted as "running" it — deleting the step left the test green.
- **Eval workspaces stay out of ruff**: one A/B run left 36 directories of
  model-written Python inside the tree; 21 of 23 files `ruff format
  --check` wanted were the model's homework (F14-08's shape: the system
  under test's workspace inside the tooling).

---

## Deployment fork (minicodex-live's own deltas)

- **LIVE-01**: the console's `sandbox_mode` gated approval questions only —
  `run_turn` never built a sandbox, so every command ran on the host. Fixed
  with the runtime wiring plus the startup gate (LIVE-02: refuse without
  bwrap unless `--allow-unsandboxed`).
- **`MINICODEX_DATA_DIR`**: container state belongs on a mounted volume;
  "the cwd of a process started by a service manager" is not a place.
  `--data-dir` still wins.
- **Sessions survive restarts**: an earlier docstring argued against it;
  the deployment reverses that. Debounced persistence (≤1s writes) answers
  the write-frequency objection; `auth.resolve` revoking dead accounts'
  sessions on first touch answers the outliving objection; the shutdown
  hook fails all brokers and turn files land `interrupted` — a surviving
  cookie points only at threads the account already owns. A half-written
  `threads.json` loses one session; a half-written `accounts.json` loses
  everyone's login — hence accounts does not use the Store table.
- **Let's Encrypt IP certificates / TLS profile**: see the compose stack
  and the deployment handbook.
