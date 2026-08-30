# minicodex — chapter 22: connecting a server from the console

Chapter 21 gave the library remote MCP and OAuth. The console could not reach
any of it — its add-server form asked for a command and nothing else, and
`runtime.py` turned every stored record into a `ServerConfig` without asking
what shape it was. A capability nobody can call from the interface most people
actually use is not a shipped capability. This chapter closes that.

```bash
uv sync --all-extras
uv run pytest                                  # 1881 tests, all offline
uv run python scripts/check_layers.py
uv run python probe_mutations_ch22.py          # 12 mutations, 0 survivors
cd frontend && npm run typecheck && npm test   # tsc clean, 4 tests
```

## What is new

| Path | What it does |
|---|---|
| `src/minicodex/web/oauth.py` | `PendingOAuthTable`, `begin()`/`finish()` — one blocking OAuth call split into two HTTP requests with a browser between them |
| `src/minicodex/web/mcp_config.py` | one stored `mcp.json` record → the config object `mcp.connect` takes; called from `routes.py` and `runtime.py`, written once |
| `src/minicodex/web/routes.py` | `McpIn` now expresses either shape; `POST /api/mcp/oauth/start`, `GET /api/mcp/oauth/callback`, `GET /api/mcp/{id}/oauth/status`; bearer tokens redacted on the way out |
| `src/minicodex/web/runtime.py` | `resolve_mcp_configs` — dispatches on record shape and refuses an unconnected OAuth server by name |
| `frontend/src/mcpOauth.ts` | the browser side of one connection, every side effect injected so it tests without a popup or a clock |
| `frontend/src/components/Extensions.tsx` | local/remote toggle, two authentication paths, a Connect GitHub shortcut |
| `tests/test_faults_ch22.py` | 24 tests, including the full `start → authorize → callback → storage` chain against a stub authorization server |
| `probe_mutations_ch22.py` | 12 mutations against this chapter's own code |

## The one architectural fact this chapter is about

Chapter 21's OAuth is written for a terminal. `redirect_handler` opens a
browser **on this machine**; `callback_handler` binds a loopback socket **on
this machine** and blocks until the redirect lands on it. Both assume the
process asking for authorization and the browser granting it share a machine.

A server-hosted console breaks that in both directions — the server has no
screen to open a browser on, and a remote browser cannot reach the server's
`127.0.0.1`. So one blocking call becomes two of this console's own routes,
and everything the first learned has to survive until the second arrives:

```
POST /api/mcp/oauth/start     discovery + registration/preset client + PKCE
                              → returns the authorize URL, does not open it
   [ new tab → authorization server → the person clicks approve ]
GET  /api/mcp/oauth/callback  looks the state up, exchanges the code,
                              writes the token, renders a result page
```

**No RFC is reimplemented.** `web/oauth.py` drives the SDK's own
`OAuthContext`, `PKCEParameters` and discovery helpers. Only the seam is new,
because only the seam is about where your browser is.

Measured against the offline stub — note `redirect_uri`, derived from
`request.base_url` rather than from a setting, so one record works unchanged
in local dev and behind a real hostname:

```
authorize_url query:
  response_type          code
  client_id              preset-client
  redirect_uri           http://console.local/api/mcp/oauth/callback
  code_challenge_method  S256
  state                  1v5-bvcFBXED...  (43 chars)
  code_challenge         FmX_kDlX0Ke8...  (43 chars)
```

## `state` is looked up, never believed

The callback route's only input is a query string a stranger can write. So
`state` is server-generated, and `PendingOAuthTable.pop` is the one lookup
anything downstream is allowed to trust: unknown, expired and absent all get
the same answer, and nothing past that line reads the query string for *which
server this is*. Popped rather than read — one state buys exactly one
callback, the same way an authorization code buys one token exchange.

```python
first = await api.get("/api/mcp/oauth/callback", params=query)  # connected to gh
second = await api.get("/api/mcp/oauth/callback", params=query)  # unknown or has expired
assert auth.token_calls == 1, "the code must not be exchanged twice"
```

## Refused at the door

```
command + url  -> 400  a server is reached one way or the other -- set 'command' or 'url', not both
neither        -> 400  give this server either a command (stdio) or a url (remote)
token + oauth  -> 400  a server is authenticated one way or the other -- a bearer token or OAuth, not both
```

`mcp.load_config` already applies the same rule to a config *file* (F21-01).
Applied twice on purpose: the library's job is to raise for its caller, the
route's job is to name the field a person should fix.

A token that goes in does not come back out:

```
POST reply bearer_token = True
GET  reply bearer_token = True
on disk                 = 'ghp_a_real_looking_secret'
```

`True` means "there is one", not what it is — chapter 19's `_public` rule
applied to a new field. The on-disk line is cleartext and is stated rather
than glossed: the console's store has been cleartext since chapter 19's
`providers.json`, and this chapter puts a second kind of key in it.

## Why polling and not `postMessage`

`window.open(url, "_blank", "noopener")` is what actually runs, and `noopener`
means the new tab has no reference back to message. A `postMessage` handshake
would only work if a *less safe* call were made. Polling
`GET /api/mcp/{id}/oauth/status` also survives a popup blocker, a tab closed
early, and a page reload — none of which a handshake does.

## GitHub, honestly, in two halves

**A personal access token works today.** Pick "paste a token" in the remote
form, paste it, done. No flow, no browser, nothing to register.

**Interactive OAuth needs one manual step first.** GitHub's authorization
server advertises no `registration_endpoint` (measured in chapter 21,
F21-08b), so the SDK's dynamic registration has nothing to call, and whoever
runs this console must register a GitHub OAuth App and put its client id in
the form. The button cannot remove that step; it makes every step after it a
click. The form says so.

**What was not verified here:** the full interactive OAuth flow against real
GitHub. This environment has no registered GitHub OAuth App. The whole
`start → authorize → callback → storage` chain is proven against the stub
authorization server, in both the DCR-supporting and DCR-refusing shapes —
that is a real test of the architecture, and it is not the same claim as
"GitHub was connected".

## Compared with codex

**codex has no web console.** It is a CLI and a TUI, so it has no
browser-mediated OAuth, no pending-attempt table and no server-rendered
callback page — there is nothing in `codex-rs` to compare those against, and
saying so is more useful than finding a similar-looking file that is not the
same thing.

The parts that do trace to codex are chapter 21's and unchanged: the remote
server config's field shape (`codex-rs/config/src/mcp_types.rs:274-291`) and
the preset-`client_id` posture (`rmcp-client/src/perform_oauth_login.rs`).

## Still single-tenant

OAuth tokens are stored under `DEFAULT_OWNER` through chapter 20's `Owner`
seam — the right shape with exactly one occupant. Authentication, sessions and
quotas for this project's own multi-user deployment are chapter 23's; until
they exist, "who is this browser" has one answer.
