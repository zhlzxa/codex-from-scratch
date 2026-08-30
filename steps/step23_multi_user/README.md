# minicodex — chapter 23: who is using this console

Chapter 20 built `tenancy.Owner` and said in its own docstring that it was
"the *seam*, not a multi-user system... `Owner.key` is trusted completely by
whoever constructs it". For three chapters the only thing that constructed one
was `DEFAULT_OWNER`, and `serve()` said the rest out loud: this process runs
shell commands and edits files "on behalf of whoever can reach it, with an
approval prompt as the only gate, and it has no authentication of any kind".

This chapter is the thing that constructs an `Owner`, and the password in
front of it. `tenancy.py` did not change, which was the point of writing it
three chapters early.

```bash
uv sync --all-extras
uv run pytest                                  # 1939 tests, all offline
uv run python scripts/check_layers.py
uv run python probe_mutations_ch23.py          # 28 mutations, 0 survivors
uv run python probe_ws_auth.py                 # the measurement behind F23-02
cd frontend && npm run typecheck && npm test   # tsc clean, 9 tests
```

## What is new

| Path | What it does |
|---|---|
| `src/minicodex/web/accounts.py` | `Account`, `AccountStore`, scrypt via `hashlib`, and the one-time bootstrap token |
| `src/minicodex/web/sessions.py` | `SessionTable` — in-process, hashed tokens, an absolute clock and an idle clock |
| `src/minicodex/web/auth.py` | the gate: raw ASGI, deny by default, three public paths, an `Origin` check on the handshake |
| `src/minicodex/web/quota.py` | `Limits` and `QuotaLedger`, charged when a turn is claimed |
| `src/minicodex/web/app.py` | `Console.store_for` / `broker_for` — one record store and one approval broker per account |
| `src/minicodex/web/routes.py` | `/api/auth/*`, `/api/accounts/*`, and every existing route now resolving through the caller's own store |
| `frontend/src/components/SignIn.tsx` | the bootstrap form and the login form, chosen by `has_account` |
| `frontend/src/components/Account.tsx` | signed-in browsers, the quota, the password, and the account list for an admin |
| `tests/test_faults_ch23.py` | 57 tests, none skipped |
| `tests/test_packaging.py` | the CI wiring check, now asking the workflow's `run:` commands rather than its text (F23-11) |
| `probe_ws_auth.py` | two gates, one socket, printed side by side |
| `probe_mutations_ch23.py` | 28 mutations against this chapter's own code |

## The one idea

**Multi-tenancy is mostly not a permission check.** A check is a line somebody
has to remember to write, on every route, forever, and the failure mode of
forgetting is silence. So the tables that made a check necessary stop being one
table:

| Before | After |
|---|---|
| `console.store` | `console.store_for(owner)` → `<data-dir>/tenants/<key>/` |
| `console.broker` | `console.broker_for(account_key)` |
| `channel.emitter(thread_id)` | `channel.emitter(turn_key(account_key, thread_id))` |

After which `require_thread` contains no ownership comparison at all, and that
is not an omission:

```python
record = self.store_for(owner).thread(thread_id)
if record is None:
    raise HTTPException(404, f"no thread {thread_id!r}")
```

Somebody else's thread is not forbidden, it is **absent** — which is also why
the answer does not leak that the id exists elsewhere. The same argument runs
one level up: authentication is a middleware that refuses everything under
`/api` and `/ws`, not a `Depends(...)` a new route can be written without.

Where a shared table is unavoidable — the session list, so that "sign out
everywhere" can exist — the lookup takes the account *and* the id as a pair.

```
accounts.json
tenants/alice/providers.json
tenants/alice/threads.json
tenants/alice/sessions/3852ac0a6319
```

## The gate covers the socket, and that was measured

`BaseHTTPMiddleware` dispatches `scope["type"] == "http"` and passes everything
else through. `probe_ws_auth.py` puts each gate in front of the same two routes
and knocks with no credential:

```
gate                   GET /api/secret    ws /ws/secret
------------------------------------------------------------------------------
BaseHTTPMiddleware     HTTP 401           OPENED, received 'the socket route ran'
raw ASGI               HTTP 401           refused (WebSocketDisconnect)

Both gates deny every HTTP request. Only one of them is a gate.
```

The socket also gets an `Origin` check that the REST routes do not need: a
WebSocket handshake is exempt from CORS, so a page on any origin can open one
and the browser attaches the cookie. A handshake with no `Origin` at all is
allowed — that is a script, not the attack, which needs a browser.

## A failed login costs what a successful one costs

`hmac.compare_digest` inside the hash comparison buys nothing while
`if account is None: return None` sits in front of it. Measured here, mean of
five:

```
               known account   no such account
without the dummy hash   145.3 ms      0.148 ms
as shipped               142.6 ms    178.548 ms
```

A thousandfold, visible over any network — and an account key here is also a
directory name and a tenancy. So a miss verifies against a hash of something
nobody knows and throws the answer away, and the route says the same sentence
for unknown, wrong and disabled.

## Four chapters of a memory panel reading the wrong directory

`routes.get_memory` answered with `<workspace>/.minicodex/memories`;
`runtime.load_feature_dirs` has loaded `~/.minicodex/memories` since chapter 19,
because chapter 16 moved memory to the home directory. No turn has ever read
the first path. The tab was empty on a machine with a full memory file and
"forget everything" deleted nothing, silently, in steps 19 through 22.

Chapter 19's own regression test was green the whole time, because it wrote its
fixture to the same wrong directory it was testing. A regression test can only
be as right as the assumption it inherits from the code.

The *skills* panel two functions below is workspace-relative and correct, since
skills stayed per-project when memory moved. The two were written side by side
with the same shape and only one of them was right.

## Still not a hosted product

Stated rather than left to be discovered:

- **No TLS termination and no login throttling.** scrypt stops offline brute
  force; it does not stop a slow online one. Something else belongs in front of
  this on a public network.
- **Quotas count turns, not tokens.** `model.py` parses the server's reported
  usage, but the only consumer is `tokens.Calibration`, which keeps a ratio and
  drops the totals — and metering somebody against an estimate documented as
  useless for a session's first two turns is a number with no defence.
- **No audit log.** Rollout files record what the agent did; nothing records
  who signed in, from where, or which workspace they opened.
- **`--host 0.0.0.0` is still only a warning.** A login decides *who* may drive
  the agent; the bind address decides who can reach the port. Dropping the
  second because the first now exists trades a boundary for a credential.

## Compared with codex

**codex has no login.** It is a CLI and a TUI, it authenticates *to* ChatGPT
and never *for* anybody, so it has no session table, no account list and no
quota. There is nothing in `codex-rs` to compare this against, and saying so is
more useful than a strained analogy.

What is borrowed is chapter 20's, unchanged: `Owner`, its key validation, and
the four paths that hang off it — `memories()`, `jobs_db()`, `merge_lock()`,
`mcp_tokens()`.
