"""Real PostgreSQL tests. Set MINICODEX_TEST_DATABASE_URL to a disposable DB.

Each test owns a randomly named schema; no existing table is truncated.
CI supplies PostgreSQL so these do not silently disappear from validation.
"""

# ruff: noqa: E402 -- Optional backend dependencies must be checked before imports.

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sa = pytest.importorskip("sqlalchemy")
pytest.importorskip("psycopg")
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import func, select
from sqlalchemy.engine import make_url
from sqlalchemy.exc import IntegrityError

from minicodex.history import UserMessage
from minicodex.rollout import RolloutWriter, SessionMeta
from minicodex.web import create_app
from minicodex.web.accounts import AccountError, hash_password
from minicodex.web.database import Database, upgrade
from minicodex.web.db_models import AccountRow, Base, LoginSessionRow, ThreadRow
from minicodex.web.db_stores import DatabaseAccounts, DatabaseQuota, DatabaseSessions, DatabaseStore
from minicodex.web.import_json import import_json
from minicodex.web.quota import Limits, QuotaExceeded
from minicodex.web.sessions import SESSION_COOKIE, hash_token


@pytest.fixture
def pg_url():
    url = os.environ.get("MINICODEX_TEST_DATABASE_URL")
    if not url:
        pytest.skip("MINICODEX_TEST_DATABASE_URL is not configured")
    parsed = make_url(url).set(drivername="postgresql+psycopg")
    engine = sa.create_engine(parsed)
    schema = "test_" + uuid.uuid4().hex
    with engine.begin() as connection:
        connection.execute(sa.schema.CreateSchema(schema))
    # libpq options are also used by the production engine for timeout settings.
    test_url = parsed.update_query_dict({"options": f"-c search_path={schema}"})
    try:
        yield test_url.render_as_string(hide_password=False)
    finally:
        with engine.begin() as connection:
            connection.execute(sa.schema.DropSchema(schema, cascade=True))
        engine.dispose()


@pytest.fixture
def db(pg_url):
    upgrade(pg_url)
    database = Database(pg_url)
    try:
        yield database
    finally:
        database.close()


def account(db, name="alice"):
    return DatabaseAccounts(db).create(name, "correct-horse-battery")


def test_migration_version_and_model_agreement(pg_url):
    with pytest.raises(RuntimeError, match="schema is not current"):
        Database(pg_url)
    upgrade(pg_url)
    upgrade(pg_url)
    database = Database(pg_url)
    try:
        with database.engine.connect() as connection:
            assert compare_metadata(MigrationContext.configure(connection), Base.metadata) == []
    finally:
        database.close()


def test_packaged_migration_cli_and_alembic_check(pg_url, tmp_path):
    env = {**os.environ, "MINICODEX_DATABASE_URL": pg_url}
    result = subprocess.run(
        [sys.executable, "-m", "minicodex.web.database", "upgrade"],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    assert "schema is current" in result.stdout
    config = Path(__file__).resolve().parents[1] / "alembic.ini"
    subprocess.run(
        [sys.executable, "-m", "alembic", "-c", str(config), "check"],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )


def test_tenant_crud_and_constraints(db, tmp_path):
    account(db)
    account(db, "bob")
    alice = DatabaseStore(db, "alice", tmp_path / "alice")
    bob = DatabaseStore(db, "bob", tmp_path / "bob")
    thread = alice.new_thread(str(tmp_path), "private")
    assert alice.thread(thread["id"])["title"] == "private"
    assert bob.thread(thread["id"]) is None
    assert bob.update("threads.json", thread["id"], title="stolen") is None
    assert not bob.delete("threads.json", thread["id"])
    assert alice.update("threads.json", thread["id"], title="changed")["title"] == "changed"
    with pytest.raises(IntegrityError), db.sessions.begin() as session:
        session.add(
            ThreadRow(
                account_key="nobody", id="orphan", title="x", workspace="x", created=0, settings={}
            )
        )
    assert alice.delete("threads.json", thread["id"])


def test_concurrent_records_and_single_active_provider(db, tmp_path):
    account(db)

    def add(index):
        return DatabaseStore(db, "alice", tmp_path).new_thread(str(tmp_path), str(index))

    with ThreadPoolExecutor(max_workers=6) as pool:
        list(pool.map(add, range(20)))
    store = DatabaseStore(db, "alice", tmp_path)
    assert len(store.all("threads.json")) == 20
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(lambda _: DatabaseStore(db, "alice", tmp_path).seed_providers(), range(8)))
    providers = store.providers()
    assert len(providers) == 2
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(store.activate_provider, [p["id"] for p in providers] * 4))
    assert sum(p["active"] for p in store.providers()) == 1
    assert not store.activate_provider("missing")
    assert sum(p["active"] for p in store.providers()) == 1


def test_sessions_revocation_expiry_and_password_transaction(db):
    account(db)
    account(db, "bob")
    first, second = DatabaseSessions(db), DatabaseSessions(db)
    token, value = first.create("alice")
    assert second.touch(token).account_key == "alice"
    assert not second.revoke_id("bob", value.id)
    assert second.revoke_id("alice", value.id)
    assert first.touch(token) is None
    token, _ = first.create("alice")
    DatabaseAccounts(db).set_password("alice", "a-new-correct-password")
    assert second.touch(token) is None
    token, value = first.create("alice")
    with db.sessions.begin() as session:
        session.get(LoginSessionRow, value.token_hash).last_seen = time.time() - 8000
    assert first.touch(token) is None
    assert len(first) == 0


def test_failed_account_update_does_not_revoke_sessions(db):
    account(db)
    sessions = DatabaseSessions(db)
    token, _ = sessions.create("alice")
    with pytest.raises(IntegrityError):
        DatabaseAccounts(db).update("alice", password_hash=None)
    assert sessions.touch(token) is not None
    assert DatabaseAccounts(db).verify("alice", "correct-horse-battery") is not None


def test_hourly_quota_atomic_across_connections(db):
    account(db)
    limits = Limits(concurrent_turns=50, turns_per_hour=3)

    def claim(_):
        try:
            DatabaseQuota(db).claim("alice", limits, running=0)
            return True
        except QuotaExceeded:
            return False

    with ThreadPoolExecutor(max_workers=8) as pool:
        assert sum(pool.map(claim, range(20))) == 3
    ledger = DatabaseQuota(db)
    assert ledger.snapshot("alice", limits, running=0)["used_this_hour"] == 3
    ledger.refund("alice")
    ledger.claim("alice", limits, running=0)
    assert ledger.snapshot("alice", limits, running=0)["used_this_hour"] == 3


def test_bootstrap_race_only_one_first_account(db):
    stores = [DatabaseAccounts(db), DatabaseAccounts(db)]
    tokens = [store.issue_bootstrap_token() for store in stores]

    def redeem(index):
        try:
            stores[index].redeem_bootstrap(tokens[index], f"user{index}", "a-strong-test-password")
            return True
        except AccountError:
            return False

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sum(pool.map(redeem, range(2))) == 1


def legacy_files(root: Path):
    root.mkdir()
    now = time.time()
    (root / "accounts.json").write_text(
        json.dumps(
            [
                {
                    "key": "alice",
                    "password_hash": hash_password("correct-horse-battery"),
                    "created": now,
                    "admin": True,
                    "disabled": False,
                    "workspace_roots": [],
                    "limits": {},
                }
            ]
        )
    )
    tenant = root / "tenants" / "alice"
    tenant.mkdir(parents=True)
    with RolloutWriter(tenant / "sessions" / "thread1" / "old.jsonl", SessionMeta("old")) as writer:
        writer.append(UserMessage("retained conversation"))
    (tenant / "providers.json").write_text(
        json.dumps(
            [
                {
                    "id": "provider1",
                    "name": "old-provider",
                    "provider": "openai",
                    "base_url": "https://example.com",
                    "model": "old-model",
                    "api_key": "old-key",
                    "active": True,
                }
            ]
        )
    )
    (tenant / "mcp.json").write_text(
        json.dumps(
            [
                {
                    "id": "mcp1",
                    "name": "old-mcp",
                    "kind": "remote",
                    "enabled": True,
                    "url": "https://example.com/mcp",
                    "bearer_token": "old-bearer",
                    "oauth": None,
                }
            ]
        )
    )
    (tenant / "threads.json").write_text(
        json.dumps(
            [
                {
                    "id": "thread1",
                    "workspace": str(root),
                    "title": "old",
                    "created": now,
                    "settings": {},
                }
            ]
        )
    )
    (root / "sessions.json").write_text(
        json.dumps(
            {
                "sessions": [
                    {
                        "token_hash": hash_token("old-cookie"),
                        "account_key": "alice",
                        "created": now,
                        "last_seen": now,
                    }
                ]
            }
        )
    )
    (root / "quota.json").write_text(json.dumps({"starts": {"alice": [now]}}))


def test_import_atomic_idempotent_and_retains_auth(db, pg_url, tmp_path):
    root = tmp_path / "console"
    legacy_files(root)
    before = {str(p): p.read_bytes() for p in root.rglob("*.json")}
    with pytest.raises(RuntimeError, match="explicit import"):
        create_app(root, database_url=pg_url)
    result = import_json(db, root)
    assert (
        result["accounts"]
        == result["threads"]
        == result["login_sessions"]
        == result["quota_starts"]
        == 1
    )
    assert import_json(db, root) == result
    assert before == {str(p): p.read_bytes() for p in root.rglob("*.json")}
    with TestClient(create_app(root, database_url=pg_url)) as client:
        client.cookies.set(SESSION_COOKIE, "old-cookie")
        assert client.get("/api/auth/status").json()["signed_in"]
        assert client.get("/api/threads/thread1").json()["title"] == "old"
        assert (
            client.get("/api/threads/thread1").json()["items"][0]["text"] == "retained conversation"
        )
        assert client.get("/api/providers").json()[0]["active"] is True
        assert client.get("/api/mcp").json()[0]["bearer_token"] is True
        response = client.post("/api/mcp/mcp1/toggle", json={"enabled": False})
        assert response.status_code == 200
        store = DatabaseStore(db, "alice", root / "tenants" / "alice")
        assert store.get("mcp.json", "mcp1")["bearer_token"] == "old-bearer"
        assert not store.get("mcp.json", "mcp1")["enabled"]


def test_bad_import_rolls_back_everything(db, tmp_path):
    root = tmp_path / "console"
    legacy_files(root)
    (root / "quota.json").write_text('{"starts":{"unknown":[123]}}')
    with pytest.raises(ValueError, match="quota owner"):
        import_json(db, root)
    with db.sessions() as session:
        assert session.scalar(select(func.count()).select_from(AccountRow)) == 0
        assert session.scalar(select(func.count()).select_from(ThreadRow)) == 0


def test_nonempty_database_is_never_overwritten(db, tmp_path):
    account(db, "existing")
    root = tmp_path / "console"
    legacy_files(root)
    with pytest.raises(ValueError, match="empty database"):
        import_json(db, root)
    assert [a.key for a in DatabaseAccounts(db).all()] == ["existing"]


def test_configuration_failure_does_not_claim_busy_or_quota(db, pg_url, tmp_path, monkeypatch):
    account(db)
    app = create_app(tmp_path / "console", database_url=pg_url)
    console = app.state.console
    token, _ = console.sessions.create("alice")
    store = console.store_for(DatabaseAccounts(db).get("alice").owner())
    thread = store.new_thread(str(tmp_path), "outage")

    def fail():
        raise RuntimeError("simulated database outage")

    monkeypatch.setattr(store, "active_provider", fail)
    with TestClient(app, raise_server_exceptions=False) as client:
        client.cookies.set(SESSION_COOKIE, token)
        assert (
            client.post(f"/api/threads/{thread['id']}/messages", json={"text": "hello"}).status_code
            == 500
        )
        assert not console.busy
        assert console.quota.snapshot("alice", Limits(), running=0)["used_this_hour"] == 0


def test_api_restart_provider_redaction_and_account_disable(db, pg_url, tmp_path):
    root = tmp_path / "console"
    with TestClient(create_app(root, database_url=pg_url)) as client:
        token = client.app.state.console.accounts.issue_bootstrap_token()
        response = client.post(
            "/api/auth/bootstrap",
            json={"token": token, "key": "alice", "password": "correct-horse-battery"},
        )
        assert response.status_code == 200
        cookie = client.cookies[SESSION_COOKIE]
        response = client.post(
            "/api/providers",
            json={
                "name": "test",
                "provider": "openai",
                "base_url": "https://example.com",
                "model": "test",
                "api_key": "secret-test-key",
            },
        )
        assert response.status_code == 200
        assert response.json()["api_key"] is True
        assert "secret-test-key" not in response.text
    with TestClient(create_app(root, database_url=pg_url)) as client:
        client.cookies.set(SESSION_COOKIE, cookie)
        assert client.get("/api/auth/status").json()["signed_in"]
        DatabaseAccounts(db).update("alice", disabled=True)
        assert not client.get("/api/auth/status").json()["signed_in"]
    assert not (root / "accounts.json").exists()
    assert not (root / "sessions.json").exists()
