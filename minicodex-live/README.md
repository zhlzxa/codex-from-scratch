# minicodex-live

A Codex-style coding agent built from scratch in Python — no agent framework —
shipped as a deployable, multi-user web console.

- **Agent core**: talks the chat-completions protocol to any provider (OpenAI-compatible
  or Ollama); tool calling (shell / patch / plan / sub-agents / MCP), context
  compaction, durable sessions with resume, deterministic replay.
- **Console**: FastAPI + React. Accounts (scrypt), session cookies, per-account
  tenancy, approvals in the browser, quotas, an audit log — self-hostable and
  ready to sit behind a reverse proxy.
- **Testing discipline**: ~1000 offline tests, plus a seeded property suite whose
  case count is set by `MINICODEX_PROPERTY_CASES` (200 by default, 2000 in CI) —
  so a bare `pytest` reports a few thousand and the total moves with that
  variable. Every repair is reproduced first, fixed second, pinned by a test.
  38 probe scripts (`probes/`): 15 plant a deliberate defect and check the suite
  goes red, the rest take the measurements that need a real model.

> **Where this sits.** This repository is *从零复刻 Codex* ("Rebuilding Codex from
> scratch"): a 27-chapter tutorial in [`../tutorial/`](../tutorial/), one
> self-contained runnable snapshot per chapter in [`../steps/`](../steps/), and a
> catalogue of 259 numbered faults in [`../FAULTS.md`](../FAULTS.md). Every
> chapter opens by predicting what could go wrong and closes by testing each
> prediction — the ones that hold are fixed and pinned by a test, and the 27 that
> never reproduce are kept as negative results with their evidence. Only 24 of
> the 259 announce themselves with a crash; the other 91% throw nothing at the
> moment they happen.
>
> **This directory is what shipped.** It starts from the last chapter's snapshot
> and adds what a deployable service actually needs: PostgreSQL persistence with
> migrations, login throttling, an audit log, Docker and nginx, and the repairs
> from a full code review ([REVIEW-FINDINGS.md](REVIEW-FINDINGS.md)). The
> tutorial is the reasoning; this is the delivery.
>
> Design history lives in [`docs/history.md`](docs/history.md); source comments
> carry only constraints, invariants, and "why not the tempting alternative"
> (style guide: [`docs/comment-style.md`](docs/comment-style.md)).

## Quick start

### Docker (recommended)

```bash
docker compose up --build
# Open http://127.0.0.1:8000 and create the first account with the
# one-time token printed to the terminal.
```

Pass the model key as an environment variable (`OPENAI_API_KEY=... docker compose up`)
or configure it in the console's Extensions page. Ollama needs no key.

Why compose sets `seccomp:unconfined`: bubblewrap needs to create user
namespaces, and Docker's default seccomp profile blocks
`clone(CLONE_NEWUSER)`. Without those lines the server starts, the startup
gate passes, and the **first command** fails. This is a documented, deliberate
relaxation; run on bare metal if you don't want it (bubblewrap works out of
the box there).

### Bare metal (Linux)

```bash
sudo apt install bubblewrap
uv sync --all-extras
uv run minicodex serve            # defaults to 127.0.0.1:8000
```

No sandbox exists on Windows/macOS: the startup gate refuses to start, or
starts only with an explicit `--allow-unsandboxed`. It never runs silently
unconfined. Production is Linux with bubblewrap installed — a prerequisite,
not a preference.

### CLI

```bash
uv run minicodex ask "where is the entry point of this repo?" --sandbox-mode workspace-write
uv run minicodex serve --host 127.0.0.1 --port 8000
uv run minicodex ask ... --resume last          # continue the last session
uv run minicodex replay <recording.jsonl>       # replay a recording, offline
uv run minicodex --help
```

### Public deployment

The console does not do TLS itself — put a reverse proxy in front. Three
ready-made paths (details in [README-LIVE.md](README-LIVE.md)):

| Path | Use when |
|---|---|
| Built-in nginx + Let's Encrypt **IP certificate** (`--profile tls`) | You have a public IP but no domain. Auto-renewal via the six-day `shortlived` profile |
| mkcert self-signed cert, placed manually | Pure intranet |
| Bare-metal Caddy | You have a domain — the least work |

Do not expose port 8000 directly: authentication is not TLS.

## Architecture at a glance

```
src/minicodex/
  agent.py            the loop: request -> tool dispatch -> results; budget, compaction, retry
  tools.py            run_shell / apply_patch / read_file / request_permissions
  approval.py         the one approval gate: policy tables + rule store + swappable Approver
  policy.py           command risk classification; two mode tables (unconfined / kernel-confined)
  sandbox.py          bwrap wrapping: ro-bind / writable holes / PID+net namespaces
  subagent.py         synchronous nested sub-agents: depth/turn/width budgets, own session files
  rollout.py          append-only session files (versioned, O_EXCL single writer, resumable)
  recorder.py         per-event recording at the model boundary (replay & divergence checks)
  memory*.py          memory read side (injection, citations, usage) and write side (extract/merge/forget)
  registry.py         MCP tool registry: schema budget, deferred loading, footprints
  retry.py            failure classes: retry / shrink / fatal
  web/                FastAPI console: accounts, approval broker, quotas, audit, persistence
```

Layering is enforced by
[`scripts/check_layers.py`](scripts/check_layers.py) (`tools ↛ agent`,
`* ↛ web`, ...); every rule corresponds to a real incident.

## Security model

- **Deny by default.** Everything under `/api` and `/ws` sits behind the auth
  middleware; WebSocket handshakes additionally check `Origin` (CORS does not
  cover WS handshakes — measured, not assumed).
- **Tenancy by namespace, not per-route checks.** Each account gets its own
  store directory, approval broker and event-stream key. Somebody else's
  thread is not *forbidden*, it is *absent* — and the 404 does not leak
  whether the id exists elsewhere.
- **Login throttling + constant-time misses.** Failures are counted per
  (account, source); an unknown account also runs one dummy scrypt so a miss
  costs the same as a hit (pinned by a timing test).
- **Audit.** `<data-dir>/audit.jsonl`, append-only JSONL: logins, bootstrap,
  turn starts, every shell command (structured exit code), every patch, every
  permission decision, and server-side refusals (quota / throttle / workspace
  bounds). A write failure fails the operation — the trail would rather stop
  than lie.
- **Approvals fail closed.** `DenyAll` is the default approver; when the
  sandbox is unavailable the confined permission table does not engage and
  everything falls back to asking.

## Development

```bash
uv sync --all-extras
uv run pytest                                  # offline; count varies with MINICODEX_PROPERTY_CASES
uv run ruff check . && uv run ruff format --check .
uv run python scripts/check_layers.py          # architecture rules
cd frontend && npm ci && npm run typecheck && npm test && npm run build
```

Beyond the test suite, `probes/` holds the measurement scripts that talk to
real providers (prompt effectiveness, compaction cut points, timing, sandbox
behaviour); their conclusions are archived in
[`docs/history.md`](docs/history.md).

## Documentation

| Doc | Contents |
|---|---|
| [README-LIVE.md](README-LIVE.md) | Deployment handbook: public-network setup, backups, ops facts, honest limitations |
| [docs/history.md](docs/history.md) | Design history: measurements, fault numbering, per-mechanism comparison with codex |
| [docs/comment-style.md](docs/comment-style.md) | Comment style: what stays in code, what moves out |
| [REVIEW-FINDINGS.md](REVIEW-FINDINGS.md) | The full-code-review findings list the repairs came from |

## Relation to codex

Mechanisms are compared feature-by-feature against upstream codex (the
comparison is archived in `docs/history.md`): compaction, retries, memory,
skills, MCP, AGENTS.md delivery — most differences are deliberate and each
has a measurement behind it. The largest structural difference is the
**sub-agent collaboration model**: codex runs a background worker pool with
async spawn + polling; this project runs a synchronous nested agent loop,
which is the shape that preserves the "every issued call is answered exactly
once" invariant.

What codex does not have: login, multi-account tenancy, quotas, the audit
log, the health endpoint. Those are this project's console layer — there is
no upstream to compare against, so their security properties are backed by
this repo's tests and probes.

## License

See `LICENSE` in the repository root. (If absent, all rights reserved — add
one before distributing.)
