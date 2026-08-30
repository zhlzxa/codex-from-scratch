# minicodex — chapter 21: connecting to somebody else's MCP server

Chapter 9 replaced hand-rolled JSON-RPC with the official `mcp` SDK. The SDK
already includes a remote transport — `streamable_http_client`, content
negotiation, session ids, protocol-version headers — none of which this
chapter writes. What the SDK does not decide is who to trust, where a
credential lives, and whether a concurrent refresh is safe. That is this
chapter.

```bash
uv sync --all-extras
uv run pytest                                  # 1854 tests, all offline
uv run python scripts/check_layers.py
uv run python probe_mutations_ch21.py          # 11 mutations, 0 survivors
uv run python probe_mcp_remote.py all          # network: two public MCP
                                                # endpoints, read-only
```

## What is new

| Path | What it does |
|---|---|
| `src/minicodex/remote.py` | `RemoteServerConfig` (mirrors codex's `RawMcpServerConfig` url/header/bearer-token fields) and the code that wires it into `mcp.Client` |
| `src/minicodex/mcp_oauth.py` | `OwnerTokenStorage` — the SDK's `TokenStorage` protocol, per `Owner`, 0600 — plus the default `redirect_handler`/`callback_handler` and `preload_tokens` |
| `src/minicodex/mcp.py` | one added branch (`AnyServerConfig` dispatch), one added function (`_sandboxed_stdio_params`) |
| `src/minicodex/registry.py` | `load_config` now accepts `url` beside `command`; `connect` passes a sandbox and an owner through |
| `tests/mcp_http_stub.py` | a stateless streamable-HTTP MCP stub and a combined authorization-server/resource-server stub, `asyncio.start_server` and nothing else |
| `probe_mcp_remote.py` | four sections measuring real servers: an unauthenticated challenge, `.well-known` discovery (including whether GitHub advertises RFC 7591), a bearer-token connection (skips without `GITHUB_MCP_TOKEN`), and network latency |
| `probe_mutations_ch21.py` | 11 mutations against this chapter's own code |

Deleted, not kept "just in case": `transport.py` (347 lines), `http_transport.py`
(290 lines), and the discovery/PKCE/refresh internals of the old `mcp_oauth.py`
(~300 lines) — all superseded by the SDK.

## Three decisions the SDK does not make

**1. Dynamic client registration.** codex has none — `perform_oauth_login.rs`
takes a preset `client_id` and there is no `/register` code anywhere in
`rmcp-client`. The SDK's `OAuthClientProvider` attempts RFC 7591 whenever no
`client_id` is on file. Measured against GitHub's real authorization server:

```
registration_endpoint  (absent)
```

GitHub does not advertise it. `RemoteOAuthConfig.client_id`, seeded into
storage before the flow starts, is this project's way of taking codex's
posture when a server needs it.

**2. Where a credential lives.** `OwnerTokenStorage` binds the SDK's
`TokenStorage` protocol to chapter 20's `Owner` — one file per owner, 0600
(Windows: a no-op, said honestly, same as chapter 20's bubblewrap note).

**3. Concurrent refresh, and what happens after a restart.** Measured, not
assumed: `OAuthClientProvider.async_auth_flow` holds one `anyio.Lock` around
the *entire* request, not just the refresh — four concurrent calls trigger
exactly one refresh. Separately, and unexpectedly: a token reloaded from disk
has no remembered expiry, and a 401 does **not** fall back to a refresh token
sitting right there — it opens a browser. Reproduced against the SDK directly,
then fixed by persisting an absolute expiry and restoring it before the first
request (`mcp_oauth.preload_tokens`). See the tutorial's §9 for the full
measurement.

## What real GitHub said

`probe_mcp_remote.py wellknown`, read-only, no token:

```
hop 1  https://api.githubcopilot.com/.well-known/oauth-protected-resource/mcp/
hop 2  authorization_servers = ['https://github.com/login/oauth']

    200   USED  https://github.com/.well-known/oauth-authorization-server/login/oauth
    404   no    https://github.com/.well-known/oauth-authorization-server
```

Only the path-suffixed well-known URI answers — exactly the shape codex's own
`perform_oauth_login.rs` tries first, for the same reason.

`probe_mcp_remote.py latency`:

```
deepwiki   connect+initialize in 1.13s (unauthenticated: ok)
```

Chapter 9's 30s startup budget was sized around a subprocess that might
compile something on first run. A real network round trip is a low single
digit — the budget is unchanged because it was already generous enough.

## What did not run

`probe_mcp_remote.py bearer` needs a real GitHub personal access token
(`GITHUB_MCP_TOKEN`) to mean anything and skips gracefully without one:

```
GITHUB_MCP_TOKEN is not set; skipping (this section needs a real token to
mean anything)
```

The offline stub proves the mechanism — a static bearer token is a default
header from the very first request, no discovery round trip, no 401. Whether
GitHub's real MCP server accepts a personal access token this way was not
verified in this environment; said so rather than assumed.

## A note on what this step inherited

`step21_mcp_remote` had not been carried through chapter 9's SDK migration
the way the other twelve downstream steps had — `mcp_servers/*.py` were still
hand-rolled JSON-RPC servers, `tests/test_faults_ch09.py` still asserted on
raw dicts (`result.get("isError")`) instead of the SDK's typed
`CallToolResult`, and `registry.py` still exported the pre-Interlude-B
`route_footprint`. These are not this chapter's changes to make, so they were
synced from `step20_sandbox` (the most recent step with the migration
applied) rather than rewritten here.

## Compared with codex

| | codex | here |
|---|---|---|
| transport | `rmcp` crate | `mcp` SDK |
| RFC 7591 | not implemented; needs a preset `client_id` | SDK implements it; `RemoteOAuthConfig.client_id` opts into codex's posture instead |
| concurrent refresh | `refresh_lock.rs` + `refresh_transaction.rs` + `store_lock.rs` (588 lines) | the SDK's own `anyio.Lock`, measured sufficient |
| token expiry across a restart | `StoredOAuthTokens.expires_at` | `OwnerTokenStorage`'s `expires_at` — independently arrived at, same idea |
| stdio server sandboxing | `executor_process_transport.rs`, separate from `run_shell`'s sandbox | `_sandboxed_stdio_params`, same separation |
| network isolation for a remote server | `network-proxy` (15k lines, MITM, per-domain rules) | chapter 20's `--unshare-net`, coarser, said so |
| auth/sessions/quotas | present | chapter 22, same boundary chapter 20 already drew |

Nothing here goes beyond codex. The one place this project's own measurement
led somewhere codex had already gone (persisting an absolute token expiry) is
noted as independent convergence, not copying.
