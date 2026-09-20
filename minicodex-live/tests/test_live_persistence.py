"""LIVE-06: a restart no longer signs everybody out or forgives the hour.

Chapter 23 argued sessions should be memory-only because the console was a
tool *you* restart.  The deployment fork runs on a server somebody else keeps
alive, where a restart is maintenance or a crash or an operator's mistake --
and where "quota in memory" meant stop/start reset the hourly window, a
loophole anyone who can reach the host could force.  These tests pin: the
tables survive a full app rebuild from the same data dir, damaged files start
empty without taking the server down, tokens are on disk hashed, and the
shutdown hook flushes.
"""

from __future__ import annotations

import json
from collections import deque
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from minicodex.web import create_app
from minicodex.web.quota import Limits, QuotaLedger
from minicodex.web.sessions import SESSION_COOKIE, SessionTable, hash_token


@pytest.fixture
def data_dir(tmp_path: Path) -> Path:
    return tmp_path / "console"


def _bootstrap(app: Any, client: TestClient) -> str:
    """Create the first account; return the account key."""
    token = client.app.state.console.accounts.issue_bootstrap_token()
    response = client.post(
        "/api/auth/bootstrap",
        json={"token": token, "key": "tester", "password": "a-perfectly-fine-password"},
    )
    assert response.status_code == 200, response.text
    return "tester"


def _new_app(data_dir: Path) -> Any:
    return create_app(data_dir, home=data_dir.parent / "home")


# -- sessions -----------------------------------------------------------------


def test_LIVE_06_session_survives_a_full_app_restart(data_dir: Path) -> None:
    with TestClient(_new_app(data_dir)) as first:
        _bootstrap(first.app.state.console, first)
        cookie = first.cookies[SESSION_COOKIE]
    # Second app instance over the same data dir: the "restart".
    with TestClient(_new_app(data_dir)) as second:
        response = second.get("/api/auth/status")
        assert response.json()["signed_in"] is False  # no cookie sent yet
        second.cookies.set(SESSION_COOKIE, cookie)
        response = second.get("/api/auth/status")
        assert response.json()["signed_in"] is True


def test_LIVE_06_sessions_on_disk_are_hashed(data_dir: Path) -> None:
    with TestClient(_new_app(data_dir)) as client:
        _bootstrap(client.app.state.console, client)
        cookie = client.cookies[SESSION_COOKIE]
        client.app.state.console.sessions.flush()
    raw = (data_dir / "sessions.json").read_text(encoding="utf-8")
    assert cookie not in raw  # the raw credential never lands
    assert hash_token(cookie) in json.loads(raw).get("sessions", [{}])[0].get("token_hash", "")


def test_LIVE_06_restarting_revokes_sessions_of_deleted_accounts(data_dir: Path) -> None:
    """A persisted table must not outlive its account list.

    The session survives the restart, but `resolve` still checks the account
    exists and is enabled on every request -- so the cookie stops working the
    moment its account does, exactly as chapter 23 promised.
    """
    with TestClient(_new_app(data_dir)) as first:
        _bootstrap(first.app.state.console, first)
        cookie = first.cookies[SESSION_COOKIE]
        first.app.state.console.accounts.update("tester", disabled=True)
    with TestClient(_new_app(data_dir)) as second:
        second.cookies.set(SESSION_COOKIE, cookie)
        assert second.get("/api/auth/status").json()["signed_in"] is False


def test_LIVE_06_damaged_sessions_file_starts_empty_and_loudly(
    data_dir: Path, caplog: pytest.LogCaptureFixture
) -> None:
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / "sessions.json").write_text('{"sessions": [trunc', encoding="utf-8")
    table = SessionTable.load(data_dir / "sessions.json")
    assert len(table) == 0
    assert any("unreadable" in r.getMessage() for r in caplog.records)


def test_LIVE_06_in_memory_default_is_untouched() -> None:
    """`SessionTable()` with no path stays memory-only: every pre-existing
    test keeps its meaning."""
    table = SessionTable()
    table.create("alice")
    table.flush()
    assert table._path is None


# -- quota --------------------------------------------------------------------


def test_LIVE_06_quota_window_survives_a_restart(data_dir: Path) -> None:
    with TestClient(_new_app(data_dir)) as first:
        _bootstrap(first.app.state.console, first)
        ledger = first.app.state.console.quota
        ledger.claim("tester", Limits(), running=0)
        ledger.flush()
    with TestClient(_new_app(data_dir)) as second:
        assert len(second.app.state.console.quota._starts.get("tester", ())) == 1


def test_LIVE_06_stale_quota_entries_are_swept_on_touch(data_dir: Path) -> None:
    path = data_dir / "quota.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"saved_at": 0.0, "starts": {"tester": [1.0, 2.0, 3.0]}}),
        encoding="utf-8",
    )
    ledger = QuotaLedger.load(path)
    window = ledger._window("tester", now=1.0e9)
    assert window == deque()  # everything older than an hour is gone
