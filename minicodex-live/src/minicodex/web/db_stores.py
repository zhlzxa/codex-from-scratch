"""PostgreSQL implementations of the console stores.

Only detached domain values leave a transaction. Tenant record queries always
include the owner, including updates and deletes. In-flight turns still belong
to one Web process; the durable hourly quota is serialized in PostgreSQL.
"""

from __future__ import annotations

import hmac
import secrets
import time
from pathlib import Path
from typing import Any

from sqlalchemy import delete, func, or_, select, text, update
from sqlalchemy.exc import IntegrityError

from .accounts import (
    _HANDLE,
    RESERVED_KEYS,
    Account,
    AccountError,
    AccountStore,
    _from_json,
    hash_password,
)
from .db_engine import Database
from .db_models import (
    AccountRow,
    LoginSessionRow,
    McpRow,
    ProviderRow,
    QuotaStartRow,
    ThreadRow,
)
from .quota import WINDOW_SECONDS, Limits, QuotaExceeded, QuotaLedger
from .sessions import ABSOLUTE_TTL_SECONDS, IDLE_TTL_SECONDS, Session, SessionTable, hash_token
from .store import SEED_PROVIDERS, Store, _new_id, validate_settings


def account_value(row: AccountRow) -> Account:
    return _from_json({column.name: getattr(row, column.name) for column in row.__table__.columns})


def new_account(key: str, password: str, **fields: Any) -> AccountRow:
    key = key.strip().lower()
    if not _HANDLE.fullmatch(key) or key in RESERVED_KEYS:
        raise AccountError("invalid or reserved account name")
    return AccountRow(key=key, password_hash=hash_password(password), created=time.time(), **fields)


class DatabaseAccounts(AccountStore):
    def __init__(self, db: Database) -> None:
        super().__init__(Path("accounts.json"))  # Domain methods never use this path.
        self.db = db

    def all(self) -> list[Account]:
        with self.db.sessions() as session:
            return [
                account_value(r)
                for r in session.scalars(select(AccountRow).order_by(AccountRow.key))
            ]

    def get(self, key: str) -> Account | None:
        with self.db.sessions() as session:
            row = session.get(AccountRow, key)
            return account_value(row) if row else None

    def empty(self) -> bool:
        with self.db.sessions() as session:
            return session.scalar(select(AccountRow.key).limit(1)) is None

    def create(
        self, key: str, password: str, *, admin: bool = False, workspace_roots: tuple[str, ...] = ()
    ) -> Account:
        row = new_account(key, password, admin=admin, workspace_roots=list(workspace_roots))
        try:
            with self.db.sessions.begin() as session:
                session.add(row)
                session.flush()
                return account_value(row)
        except IntegrityError:
            raise AccountError(f"account {row.key!r} already exists") from None

    def update(self, key: str, **changes: Any) -> Account | None:
        allowed = {"password_hash", "disabled", "admin", "workspace_roots", "limits"}
        if changes.keys() - allowed:
            raise AccountError("unsupported account update")
        with self.db.sessions.begin() as session:
            row = session.get(AccountRow, key, with_for_update=True)
            if row is None:
                return None
            for field, value in changes.items():
                setattr(row, field, value)
            if "password_hash" in changes or changes.get("disabled"):
                session.execute(delete(LoginSessionRow).where(LoginSessionRow.account_key == key))
            session.flush()
            return account_value(row)

    def redeem_bootstrap(self, token: str, key: str, password: str) -> Account:
        with self._lock:
            if self.bootstrap_token is None or not hmac.compare_digest(token, self.bootstrap_token):
                raise AccountError("that is not the bootstrap token this server printed")
            row = new_account(key, password, admin=True)
            with self.db.sessions.begin() as session:
                # Also safe if two independently started consoles hold bootstrap tokens.
                session.execute(text("LOCK TABLE accounts IN SHARE ROW EXCLUSIVE MODE"))
                if session.scalar(select(AccountRow.key).limit(1)) is not None:
                    raise AccountError("this console already has an account; sign in instead")
                session.add(row)
                session.flush()
                value = account_value(row)
            self.bootstrap_token = None
            return value


RECORD_MODELS = {"threads.json": ThreadRow, "providers.json": ProviderRow, "mcp.json": McpRow}


def record_value(row: Any) -> dict[str, Any]:
    result = {
        c.name: getattr(row, c.name) for c in row.__table__.columns if c.name != "account_key"
    }
    if isinstance(row, McpRow):
        config = result.pop("config")
        result = {**config, **result}
    return result


def record_fields(name: str, record: dict[str, Any]) -> dict[str, Any]:
    result = dict(record)
    if name == "mcp.json":
        result.setdefault("kind", "remote" if result.get("url") else "stdio")
        result.setdefault("enabled", True)
        common = {k: result.pop(k) for k in ("id", "name", "kind", "enabled")}
        return {**common, "config": result}
    if name == "threads.json":
        result["settings"] = validate_settings(result.get("settings", {}))
    if name == "providers.json":
        result.setdefault("active", False)
        result.setdefault("api_key", None)
    return result


class DatabaseStore(Store):
    def __init__(self, db: Database, owner: str, data_dir: Path) -> None:
        super().__init__(data_dir)
        self.db, self.owner = db, owner

    def _query(self, name: str) -> Any:
        model = RECORD_MODELS[name]
        return select(model).where(model.account_key == self.owner)

    def all(self, name: str) -> list[dict[str, Any]]:
        with self.db.sessions() as session:
            return [
                record_value(row)
                for row in session.scalars(self._query(name).order_by(RECORD_MODELS[name].id))
            ]

    def get(self, name: str, item_id: str) -> dict[str, Any] | None:
        with self.db.sessions() as session:
            row = session.scalar(self._query(name).where(RECORD_MODELS[name].id == item_id))
            return record_value(row) if row else None

    def add(self, name: str, record: dict[str, Any]) -> dict[str, Any]:
        fields = record_fields(name, {"id": _new_id(), **record})
        row = RECORD_MODELS[name](account_key=self.owner, **fields)
        with self.db.sessions.begin() as session:
            session.add(row)
            session.flush()
            return record_value(row)

    def update(self, name: str, item_id: str, **changes: Any) -> dict[str, Any] | None:
        if {"id", "account_key"} & changes.keys():
            raise ValueError("record identity cannot change")
        with self.db.sessions.begin() as session:
            row = session.scalar(
                self._query(name).where(RECORD_MODELS[name].id == item_id).with_for_update()
            )
            if row is None:
                return None
            fields = record_fields(name, {**record_value(row), **changes})
            for key, value in fields.items():
                setattr(row, key, value)
            session.flush()
            return record_value(row)

    def delete(self, name: str, item_id: str) -> bool:
        model = RECORD_MODELS[name]
        with self.db.sessions.begin() as session:
            return bool(
                session.execute(
                    delete(model).where(model.account_key == self.owner, model.id == item_id)
                ).rowcount
            )

    def activate_provider(self, provider_id: str) -> bool:
        with self.db.sessions.begin() as session:
            session.get(AccountRow, self.owner, with_for_update=True)
            row = session.get(ProviderRow, (self.owner, provider_id))
            if row is None:
                return False
            session.execute(
                update(ProviderRow)
                .where(ProviderRow.account_key == self.owner)
                .values(active=False)
            )
            row.active = True
            session.flush()
            return True

    def seed_providers(self) -> None:
        with self.db.sessions.begin() as session:
            session.get(AccountRow, self.owner, with_for_update=True)
            if session.scalar(self._query("providers.json").limit(1)) is not None:
                return
            session.add_all(
                [
                    ProviderRow(
                        account_key=self.owner, id=_new_id(), **seed, api_key=None, active=False
                    )
                    for seed in SEED_PROVIDERS
                ]
            )


def session_value(row: LoginSessionRow) -> Session:
    return Session(row.token_hash, row.account_key, row.created, row.last_seen, row.user_agent)


def expired(now: float) -> Any:
    return or_(
        LoginSessionRow.created < now - ABSOLUTE_TTL_SECONDS,
        LoginSessionRow.last_seen < now - IDLE_TTL_SECONDS,
    )


class DatabaseSessions(SessionTable):
    def __init__(self, db: Database) -> None:
        super().__init__()
        self.db = db

    def flush(self) -> None:
        pass  # Each mutation commits before returning.

    def create(self, account_key: str, *, user_agent: str = "") -> tuple[str, Session]:
        token, now = secrets.token_urlsafe(32), time.time()
        row = LoginSessionRow(
            token_hash=hash_token(token),
            account_key=account_key,
            created=now,
            last_seen=now,
            user_agent=user_agent[:200],
        )
        with self.db.sessions.begin() as session:
            account = session.get(AccountRow, account_key, with_for_update=True)
            if account is None or account.disabled:
                raise AccountError("account is unavailable")
            session.execute(delete(LoginSessionRow).where(expired(now)))
            session.add(row)
            session.flush()
            return token, session_value(row)

    def touch(self, token: str) -> Session | None:
        with self.db.sessions.begin() as session:
            row = session.get(LoginSessionRow, hash_token(token), with_for_update=True)
            if row is None:
                return None
            value = session_value(row)
            now = time.time()
            if value.expired(now):
                session.delete(row)
                return None
            if now - row.last_seen > 1:
                row.last_seen = now
            return session_value(row)

    def revoke(self, token: str) -> bool:
        with self.db.sessions.begin() as session:
            return bool(
                session.execute(
                    delete(LoginSessionRow).where(LoginSessionRow.token_hash == hash_token(token))
                ).rowcount
            )

    def revoke_id(self, account_key: str, session_id: str) -> bool:
        with self.db.sessions.begin() as session:
            return bool(
                session.execute(
                    delete(LoginSessionRow).where(
                        LoginSessionRow.account_key == account_key,
                        func.left(LoginSessionRow.token_hash, 12) == session_id,
                    )
                ).rowcount
            )

    def revoke_account(self, account_key: str) -> int:
        with self.db.sessions.begin() as session:
            return session.execute(
                delete(LoginSessionRow).where(LoginSessionRow.account_key == account_key)
            ).rowcount

    def sessions_for(self, account_key: str) -> list[Session]:
        with self.db.sessions() as session:
            return [
                session_value(r)
                for r in session.scalars(
                    select(LoginSessionRow)
                    .where(LoginSessionRow.account_key == account_key, ~expired(time.time()))
                    .order_by(LoginSessionRow.created.desc())
                )
            ]

    def sweep(self) -> int:
        with self.db.sessions.begin() as session:
            return session.execute(delete(LoginSessionRow).where(expired(time.time()))).rowcount

    def __len__(self) -> int:
        with self.db.sessions() as session:
            return session.scalar(select(func.count()).select_from(LoginSessionRow))


class DatabaseQuota(QuotaLedger):
    def __init__(self, db: Database) -> None:
        super().__init__()
        self.db = db

    def flush(self) -> None:
        pass

    def claim(self, account_key: str, limits: Limits, *, running: int) -> None:
        if running >= limits.concurrent_turns:
            raise QuotaExceeded(
                f"you already have {running} turn(s) running "
                f"(limit {limits.concurrent_turns}); wait for one to finish",
                retry_after=10,
            )
        with self.db.sessions.begin() as session:
            account = session.get(AccountRow, account_key, with_for_update=True)
            if account is None or account.disabled:
                raise AccountError("account is unavailable")
            now = time.time()
            session.execute(
                delete(QuotaStartRow).where(
                    QuotaStartRow.account_key == account_key,
                    QuotaStartRow.started < now - WINDOW_SECONDS,
                )
            )
            count, oldest = session.execute(
                select(func.count(), func.min(QuotaStartRow.started)).where(
                    QuotaStartRow.account_key == account_key
                )
            ).one()
            if count >= limits.turns_per_hour:
                retry = (
                    max(1, int(WINDOW_SECONDS - (now - oldest)) + 1) if oldest else WINDOW_SECONDS
                )
                raise QuotaExceeded(
                    f"you have started {count} turns in the last hour "
                    f"(limit {limits.turns_per_hour})",
                    retry_after=retry,
                )
            session.add(QuotaStartRow(account_key=account_key, started=now))

    def refund(self, account_key: str) -> None:
        # The single-process send route claims/refunds synchronously before
        # yielding. A distributed runner will need a per-turn claim identifier.
        with self.db.sessions.begin() as session:
            session.get(AccountRow, account_key, with_for_update=True)
            row = session.scalar(
                select(QuotaStartRow)
                .where(QuotaStartRow.account_key == account_key)
                .order_by(QuotaStartRow.id.desc())
                .limit(1)
            )
            if row:
                session.delete(row)

    def snapshot(self, account_key: str, limits: Limits, *, running: int) -> dict[str, int]:
        with self.db.sessions() as session:
            count = session.scalar(
                select(func.count())
                .select_from(QuotaStartRow)
                .where(
                    QuotaStartRow.account_key == account_key,
                    QuotaStartRow.started >= time.time() - WINDOW_SECONDS,
                )
            )
        return {
            "running": running,
            "concurrent_limit": limits.concurrent_turns,
            "used_this_hour": count,
            "hourly_limit": limits.turns_per_hour,
        }
