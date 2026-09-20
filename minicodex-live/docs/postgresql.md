# PostgreSQL console storage

The PostgreSQL backend uses SQLAlchemy 2.x, psycopg 3, and Alembic. Set
`MINICODEX_DATABASE_URL` to enable it. An unset variable keeps the legacy JSON
backend; a configured but unreachable, unmigrated, or outdated database fails
startup rather than falling back to files. Install with `uv sync --all-extras`
or `pip install '.[web,postgres]'`.

## Scope and schema

| Table | Keys and contents |
|---|---|
| `accounts` | `key` PK; password hash, timestamps, admin/disabled flags; JSONB workspace roots and quota limits |
| `login_sessions` | token hash PK; account FK, created/last_seen timestamps, user agent; account/expiry lookup indexes |
| `threads` | `(account_key, id)` PK; account FK, workspace, title, creation time, JSONB settings; owner/time index |
| `providers` | `(account_key, id)` PK; account FK, provider configuration and API key; partial unique index permits at most one active provider per account |
| `mcp_servers` | `(account_key, id)` PK; account FK, name/kind/enabled, JSONB transport configuration |
| `quota_starts` | generated bigint PK; account FK, start timestamp; owner/time index |
| `legacy_imports` | source directory PK, import time and verified row counts |
| `alembic_version` | installed schema revision |

Times retain the existing API's Unix-second representation. Tenant queries
include the account key for reads, updates and deletes. ORM objects do not
leave their transaction; public response filtering still hides password hashes
and provider/bearer credentials. These secrets are not encrypted at rest by
this change: database access and backups must be protected accordingly.

Rollout/recording JSONL, audit JSONL, MCP OAuth token files, memories, and the
SQLite memory-job queue keep their existing paths. Back up BOTH PostgreSQL
and `/data`; a database dump alone cannot recover conversation bodies.

## Docker deployment

Create a git-ignored `.env` with a randomly generated database password and a
matching URL (percent-encode reserved characters in the URL password):

```dotenv
POSTGRES_PASSWORD=<random-password>
MINICODEX_DATABASE_URL=postgresql+psycopg://minicodex:<url-encoded-password>@postgres:5432/minicodex
```

Use both compose files for every command below. This preserves the existing
`minicodex-data` volume and adds a separate PostgreSQL volume. PostgreSQL does
not publish a host port. Do not run `down -v` when keeping data.

```bash
docker compose -f compose.yaml -f compose.postgres.yaml stop minicodex
docker compose -f compose.yaml -f compose.postgres.yaml build minicodex
docker compose -f compose.yaml -f compose.postgres.yaml up -d postgres
docker compose -f compose.yaml -f compose.postgres.yaml run --rm minicodex python -m minicodex.web.database upgrade
```

For an existing JSON installation, back up `/data` while stopped, then import
using the SAME path used by the console:

```bash
docker compose -f compose.yaml -f compose.postgres.yaml run --rm minicodex python -m minicodex.web.database import-json --data-dir /data/console
```

For a fresh installation with no accounts, create the first administrator
offline instead (password is prompted without echo). This also satisfies the
existing console startup gate requiring an account before binding 0.0.0.0:

```bash
docker compose -f compose.yaml -f compose.postgres.yaml run --rm minicodex python -m minicodex.web.database bootstrap --key alice
```

Then start the console:

```bash
docker compose -f compose.yaml -f compose.postgres.yaml up -d minicodex
```

Existing imports retain account passwords, unexpired browser sessions, thread
IDs, provider selection and the current hourly quota. The importer writes all
rows and its completion marker in one transaction and verifies table counts.
Invalid JSON, duplicate keys, orphan references and constraint failures roll
everything back. Source files are never renamed or deleted. Repeating an
already completed import from the same absolute path returns its saved counts;
it does not resynchronize later JSON edits. A nonempty target from another
source is rejected. Pre-tenancy top-level records must first be adopted through
the existing file-mode bootstrap workflow; the importer never guesses an owner.

Keep the Web process stopped throughout import. Retain the same mounted data
directory when switching to PostgreSQL so rollout paths still resolve. Startup
detects old JSON files without an import marker and refuses to ignore them.

## Local development and schema changes

Set `MINICODEX_DATABASE_URL` in the shell (PowerShell:
`$env:MINICODEX_DATABASE_URL='postgresql+psycopg://...'`), then:

```bash
uv run python -m minicodex.web.database upgrade
uv run python -m minicodex.web.database import-json --data-dir .minicodex/console
uv run minicodex serve
```

Skip import if there is no old state. On loopback, the existing browser
bootstrap flow also works. To evolve the schema, edit `db_models.py`, then:

```bash
uv run alembic revision --autogenerate -m "describe the schema change"
# Review generated SQL operations, backfills, renames, and downgrade behavior.
uv run alembic upgrade head
uv run alembic check
```

Migrations are shipped inside the Python package and also work from installed
wheels via `python -m minicodex.web.database upgrade`. The initial migration is
a frozen snapshot, independent of current ORM metadata. Schema upgrades are
serialized by a PostgreSQL advisory transaction lock. Apply migrations as a
deployment step with the console stopped; startup only checks the revision.

## Operations and boundaries

Run exactly one Web process. Busy turns, cancellations, approval brokers,
WebSocket channels, pending OAuth flows, and login throttling remain local to
that process. PostgreSQL does not enable multiple workers or replicas yet.
The hourly quota uses an account row lock for atomic check-and-charge;
concurrent-turn counting still uses the single process's `busy` set. Refunds
retain the existing synchronous claim/refund API, not distributed job semantics.

Database access uses a bounded connection pool and short synchronous sessions,
matching the existing store API. No transaction spans model/tool execution.
Some async request handlers still make synchronous store calls; slow database
I/O can delay the event loop. This phase is for single-process deployments,
not a claim of high-throughput asynchronous database support.

Before cutover, restore a backup into a separate environment and verify login,
thread history, and provider settings. Before accepting new writes, switching
back to the preserved JSON snapshot is possible. After PostgreSQL has accepted
new writes, switching the variable off would load stale files: there is no
reverse exporter in this phase. Recover the database and `/data` from consistent
backups instead. A schema downgrade is not a data backup.

## Validation

Set `MINICODEX_TEST_DATABASE_URL` to a disposable PostgreSQL database, then run
`uv run pytest tests/test_postgres.py`. Each test creates and drops only its own
random schema. CI provisions PostgreSQL 17 and executes these tests alongside
the legacy backend suite. Coverage includes schema/model agreement, tenant
isolation, foreign keys, concurrent writes and quota, bootstrap races, session
revocation, restart, API redaction, and import rollback/idempotence.
