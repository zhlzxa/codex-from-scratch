"""Offline, all-or-nothing import. Source files and rollout paths are untouched."""

from __future__ import annotations

import json
import math
import re
import time
from pathlib import Path
from typing import Any

from sqlalchemy import func, select, text

from .accounts import _HANDLE, RESERVED_KEYS, _from_json, _to_json
from .db_engine import Database
from .db_models import AccountRow, ImportRow, LoginSessionRow, QuotaStartRow
from .db_stores import RECORD_MODELS, record_fields
from .quota import WINDOW_SECONDS
from .sessions import Session


def read_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        raise ValueError(f"Cannot import invalid or unreadable JSON: {path}") from None


def finite(value: Any) -> float:
    number = float(value)
    if not math.isfinite(number):
        raise ValueError("timestamps must be finite")
    return number


def import_json(db: Database, data_dir: Path) -> dict[str, int]:
    data_dir = data_dir.resolve()
    if not data_dir.is_dir():
        raise ValueError("Source data directory does not exist")
    source = str(data_dir)
    now = time.time()
    counts = {
        "accounts": 0,
        "login_sessions": 0,
        "expired_sessions": 0,
        "threads": 0,
        "providers": 0,
        "mcp_servers": 0,
        "quota_starts": 0,
    }
    with db.sessions.begin() as session:
        session.execute(
            text(
                "LOCK TABLE accounts, login_sessions, threads, providers, mcp_servers, "
                "quota_starts, legacy_imports IN SHARE ROW EXCLUSIVE MODE"
            )
        )
        previous = session.get(ImportRow, source)
        if previous:
            return previous.counts
        for model in (
            AccountRow,
            LoginSessionRow,
            QuotaStartRow,
            ImportRow,
            *RECORD_MODELS.values(),
        ):
            if session.scalar(select(func.count()).select_from(model)):
                raise ValueError(
                    "Import requires an empty database; existing rows will not be overwritten"
                )
        # Pre-tenancy records need an explicit owner; guessing can leak another
        # person's conversations. The old bootstrap path handles this format.
        if any((data_dir / name).exists() for name in (*RECORD_MODELS, "sessions")):
            raise ValueError(
                "Pre-tenancy data found. Bootstrap/adopt it in file mode before importing"
            )
        accounts = read_json(data_dir / "accounts.json", [])
        if not isinstance(accounts, list):
            raise ValueError("accounts.json must contain an array")
        owners = set()
        for raw in accounts:
            account = _from_json(raw)
            if not _HANDLE.fullmatch(account.key) or account.key in RESERVED_KEYS:
                raise ValueError("Invalid account key in source")
            if account.key in owners:
                raise ValueError("Duplicate account in source")
            if not account.password_hash.startswith("scrypt$"):
                raise ValueError("Unrecognized password hash in source")
            owners.add(account.key)
            payload = _to_json(account)
            payload["created"] = finite(payload["created"])
            session.add(AccountRow(**payload))
            counts["accounts"] += 1
        session.flush()
        tenants = data_dir / "tenants"
        if tenants.exists():
            for directory in tenants.iterdir():
                if directory.is_dir() and directory.name not in owners and any(directory.iterdir()):
                    raise ValueError(f"Tenant directory has no account: {directory.name}")
        for owner in owners:
            directory = tenants / owner
            for name, model in RECORD_MODELS.items():
                records = read_json(directory / name, [])
                if not isinstance(records, list):
                    raise ValueError(f"{name} must contain an array")
                for record in records:
                    if not re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", record["id"]):
                        raise ValueError("Invalid record ID in source")
                    fields = record_fields(name, record)
                    if name == "threads.json":
                        fields["created"] = finite(fields["created"])
                    session.add(model(account_key=owner, **fields))
                    counts[model.__tablename__] += 1
        raw_sessions = read_json(data_dir / "sessions.json", {"sessions": []})
        entries = raw_sessions["sessions"]
        if not isinstance(entries, list):
            raise ValueError("sessions.json must contain a sessions array")
        for raw in entries:
            value = Session(
                token_hash=raw["token_hash"],
                account_key=raw["account_key"],
                created=finite(raw["created"]),
                last_seen=finite(raw["last_seen"]),
                user_agent=str(raw.get("user_agent", ""))[:200],
            )
            if value.account_key not in owners:
                raise ValueError("Session references an unknown account")
            if not re.fullmatch(r"[0-9a-f]{64}", value.token_hash):
                raise ValueError("Invalid session token hash")
            if value.expired(now):
                counts["expired_sessions"] += 1
            else:
                session.add(LoginSessionRow(**vars(value)))
                counts["login_sessions"] += 1
        starts = read_json(data_dir / "quota.json", {"starts": {}})["starts"]
        if not isinstance(starts, dict):
            raise ValueError("quota.json must contain a starts object")
        for owner, timestamps in starts.items():
            if owner not in owners or not isinstance(timestamps, list):
                raise ValueError("Invalid quota owner or timestamps")
            for stamp in timestamps:
                stamp = finite(stamp)
                if stamp >= now - WINDOW_SECONDS:
                    session.add(QuotaStartRow(account_key=owner, started=stamp))
                    counts["quota_starts"] += 1
        session.flush()
        for model in (AccountRow, LoginSessionRow, QuotaStartRow, *RECORD_MODELS.values()):
            if (
                session.scalar(select(func.count()).select_from(model))
                != counts[model.__tablename__]
            ):
                raise ValueError("Imported row counts do not match the source")
        session.add(ImportRow(source=source, imported_at=now, counts=counts))
    return counts


def require_imported(db: Database, data_dir: Path) -> None:
    """Avoid an empty or unrelated DB silently replacing existing file state."""
    paths = [data_dir / name for name in ("accounts.json", "sessions.json", "quota.json")]
    paths += list((data_dir / "tenants").glob("*/*.json"))
    paths += [data_dir / name for name in RECORD_MODELS]
    if not any(path.exists() for path in paths):
        return
    with db.sessions() as session:
        if session.get(ImportRow, str(data_dir.resolve())) is None:
            raise RuntimeError(
                "Existing JSON state needs explicit import. Stop the console and run "
                "python -m minicodex.web.database import-json --data-dir <same-data-dir>"
            )
