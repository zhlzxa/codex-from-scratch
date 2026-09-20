# minicodex-live — Deployment Handbook

Operational documentation for running minicodex-live as a service. The
[project overview](README.md) covers what it is and how to get started; this
file covers deployment, operations, and the honest list of what it does not
do.

Package/import name: `minicodex`; distribution: `minicodex-live` (currently
`0.1.0`).

## Deployment

### Docker (recommended)

```bash
docker compose up --build
# Open http://127.0.0.1:8000 and create the first account with the
# one-time token printed to the terminal.
```

Pass the model key as an environment variable (`OPENAI_API_KEY=... docker
compose up`) or configure it in the console's Extensions page. Ollama needs
no key.

**Why compose sets `seccomp:unconfined`:** bubblewrap needs to create user
namespaces, and Docker's default seccomp profile blocks
`clone(CLONE_NEWUSER)`. Without those lines the server starts, the startup
gate passes (bwrap is installed), and the **first command** fails. This is a
documented, deliberate relaxation; run on bare metal if you don't want it.
On kernels with unprivileged userns enabled and a custom profile available,
tighten the options back.

### Bare metal (Linux)

```bash
sudo apt install bubblewrap
uv sync --all-extras
uv run minicodex serve            # defaults to 127.0.0.1:8000
```

### Public deployment

The console does not terminate TLS and never will — that is a solved problem
for a reverse proxy. Three paths:

**Nginx + Let's Encrypt IP certificate (compose profile — works with a
public IP, no domain needed)**

Let's Encrypt GA'd **IP address certificates** on 2026-01-15
([announcement](https://letsencrypt.org/2026/01/15/6day-and-ip-general-availability)):
IPv4 and IPv6, requiring the `shortlived` profile — **six-day validity**,
renewed every few days. The pipeline is wired up:

```bash
# Validate against the staging endpoint first (the default; avoids burning
# production rate limits):
SERVER_IP=203.0.113.10 docker compose --profile tls up
# Visit https://203.0.113.10, accept the self-signed staging cert; once the
# challenge path works:

ACME_STAGING=0 SERVER_IP=203.0.113.10 docker compose --profile tls up
# Then force a real issuance:
docker compose --profile tls exec certbot sh -c 'rm -f /letsencrypt-stamp/renew; kill 1'
```

(The last command kills the loop inside the certbot container;
`restart: unless-stopped` brings it back up to re-issue. Production rate
limits are per-IP and six-day certificates are not bound by the ordinary
limits.)

Each stage lives in one file:

| File | Responsibility |
|---|---|
| `nginx/renew.sh` | certbot side: first issuance with `--ip-address --webroot --preferred-profile shortlived`, then `certbot renew` every 12h (a six-day cert inside a 30-day renewal window renews immediately), touching the stamp volume on each success |
| `nginx/entrypoint.sh` | nginx side: envsubst renders `SERVER_IP` into the config template; a new stamp on the volume triggers `nginx -s reload` — reload does not drop connections, so `/ws/` event streams survive certificate replacement |
| `nginx/nginx.conf.template` | certificate paths point at certbot's per-IP live directory |

**No public IP** (pure intranet — ACME cannot validate): `mkcert -install &&
mkcert 192.168.x.x`, place the produced `fullchain.pem`/`privkey.pem` under
`nginx/certs/live/<that IP>/`, and skip the certbot containers (start nginx
without `--profile tls`, or mount your own nginx) — the entrypoint only
requires the files to exist. A raw self-signed cert means every user clicks
through a warning on every visit; warning fatigue is a measured cost here,
so avoid it for multi-user deployments.

**Bare-metal Caddy (easiest with a domain)**:

```
# Caddyfile
your.domain {
    reverse_proxy 127.0.0.1:8000
}
```

The session cookie gets `Secure` automatically on https requests. **Do not
expose port 8000 directly** — the console has authentication but no TLS, and
a password sent in the clear is not a credential, it is a broadcast.

## Data and backups

The optional [PostgreSQL backend](docs/postgresql.md) uses SQLAlchemy 2 and
Alembic for accounts, sessions, tenant records, and hourly quota. Enable it
with `MINICODEX_DATABASE_URL` after applying migrations and importing old JSON
state. Without that variable, the existing file backend remains active.
PostgreSQL does not change the single-Web-process deployment constraint.

With the file backend, all state lives in the `/data` volume:

| Path | Contents | Backup value |
|---|---|---|
| `/data/console/accounts.json` | accounts (password hashes) | essential |
| `/data/console/audit.jsonl` | audit log | essential (append-only; plain rsync works) |
| `/data/console/tenants/<key>/` | per-account threads/providers/mcp config | essential |
| `/data/console/sessions.json` | session table (token hashes) | optional — losing it means re-login |
| `/data/console/quota.json` | quota windows | low — resetting only affects fairness |
| `/data/home/` | memories, job database, MCP tokens | recommended |

## Operations

- `GET /api/health`: public, safe to point an uptime check at (the
  Dockerfile HEALTHCHECK uses it). Reports process facts and a
  `has_account` boolean — never an account count.
- The audit log is append-only JSONL:
  `docker compose exec minicodex tail -f /data/console/audit.jsonl`. Covers
  logins, bootstrap, turn starts, every shell command with its structured
  exit code, every patch, every permission decision, and the server-side
  refusals (quota, throttle, workspace bounds).
- Graceful stop: `docker compose stop` (30s grace) cancels in-flight turns,
  fails all pending approvals, and flushes the session/quota tables; turn
  files land as `interrupted` and are resumable.
- Model keys come from the environment or the Extensions page — never argv,
  never the subprocess environment (the allowlist in `shell.py` decides),
  never written to disk.
- When the sandbox boundary is healthy (bwrap working), `workspace-write`
  threads no longer get approval prompts for writes and interpreters — the
  kernel enforces the boundary instead. If the sandbox is unavailable,
  everything falls back to asking.

## Explicit non-goals

- **Quotas count turns, not tokens.** Nothing bounds how many model calls a
  single turn burns; the frame is 60 turns/hour and 2 concurrent per
  account. Token metering needs the usage totals `Calibration` throws away
  and is its own project.
- **Single replica.** Sessions, quotas, the approval broker and the OAuth
  pending table are all single-process semantics. Horizontal scaling is a
  different system.
- **The approval broker and the OAuth pending table are in-memory.** A
  restart fails all pending approvals and voids in-flight OAuth attempts —
  deliberate (they should live and die with the process), not a bug.
- **No login alerts, no admin-specific rate ceilings, no two-factor auth.**
  Throttling stops brute force; it does not stop phishing.
- **No sandbox on Windows/macOS.** The startup gate refuses, or requires an
  explicit `--allow-unsandboxed`. Production is Linux with bubblewrap — a
  prerequisite, not a preference.
- **No WS→HTTPS transport fallback** (upstream codex has one). This program
  does not open a streaming transport in the first place; the absence is a
  scope decision, noted in `retry.py` and `docs/history.md`.

## Development

```bash
uv sync --all-extras
uv run pytest                    # 1993 tests, all offline (bwrap cases skip off Linux)
uv run ruff check . && uv run ruff format --check .
uv run python scripts/check_layers.py
cd frontend && npm ci && npm run typecheck && npm test && npm run build
```

Comment and documentation conventions: [`docs/comment-style.md`](docs/comment-style.md).
Measurement data and the per-mechanism comparison with codex:
[`docs/history.md`](docs/history.md). When upstream moves, port changes by
reading the diff — there is no automatic sync.
