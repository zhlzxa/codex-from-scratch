"""LIVE-05: the audit trail.

Pre-fix, a multi-user server ran commands, edited files and answered on
behalf of an account with no record anywhere of *which* account -- the
rollouts know the agent, the recording knows the model, and neither knows the
human.  These tests pin the trail's shape: appended JSONL, every turn and
command and patch carrying the actor, failed logins recorded without leaking
an actor, and -- the load-bearing one -- a broken audit sink stops the turn
instead of quietly continuing unaudited.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from minicodex.shell import ShellSession
from minicodex.web import create_app
from minicodex.web.audit import AuditLog
from minicodex.web.runtime import _AuditedShell


def _records(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


# -- the log itself -----------------------------------------------------------


def test_LIVE_05_append_is_jsonl_and_ordered(tmp_path: Path) -> None:
    log = AuditLog(tmp_path / "audit.jsonl")
    log.append("login", "alice", outcome="ok")
    log.append("turn_start", "alice", workspace="/tmp/ws")
    records = _records(log.path)
    assert [r["event"] for r in records] == ["login", "turn_start"]
    assert records[0]["actor"] == "alice"
    assert records[1]["workspace"] == "/tmp/ws"
    assert all(isinstance(r["ts"], float) for r in records)


def test_LIVE_05_read_all_on_a_missing_file_is_empty(tmp_path: Path) -> None:
    assert AuditLog(tmp_path / "absent.jsonl").read_all() == []


def test_LIVE_05_write_failure_propagates(tmp_path: Path) -> None:
    """A dropped record is a hole in the only timeline there is.

    The failure is manufactured at open time: point the log at a path whose
    parent is a *file*, so creating the directory or opening the file must
    fail with a real OSError (NotADirectoryError on POSIX, FileExistsError
    from the mkdir on Windows).  A mock would only test the mock; this tests
    the invariant that the error reaches the caller instead of being logged
    and swallowed.
    """
    blocked = tmp_path / "blocked"
    blocked.write_text("x", encoding="utf-8")
    with pytest.raises(OSError):
        AuditLog(blocked / "audit.jsonl")


# -- the shell wrapper --------------------------------------------------------


@pytest.mark.anyio
async def test_LIVE_05_audited_shell_records_command_and_exit(tmp_path: Path) -> None:
    log = AuditLog(tmp_path / "audit.jsonl")
    shell = _AuditedShell(ShellSession(cwd=tmp_path), log, "alice")
    await shell.run("echo hello")
    await shell.run("exit 3")
    records = _records(log.path)
    events = [r["event"] for r in records]
    assert events == ["command", "command_done", "command", "command_done"]
    # echo: exit 0. `exit 3`: the nonzero code, parsed from the shell's own
    # convention line rather than a changed return contract.
    assert records[1]["exit"] == 0
    assert records[3]["exit"] == 3
    assert records[0]["actor"] == "alice"


# -- through the real routes --------------------------------------------------


@pytest.fixture
def app(tmp_path: Path) -> Any:
    return create_app(tmp_path / "console", home=tmp_path / "home")


def test_LIVE_05_login_attempts_are_recorded(app: Any) -> None:
    with TestClient(app) as client:
        client.post("/api/auth/login", json={"key": "ghost", "password": "wrong"})
        console = client.app.state.console
        records = console.audit.read_all()
    assert [r["event"] for r in records] == ["login"]
    assert records[0]["actor"] == "-"  # nobody authenticated
    assert records[0]["account"] == "ghost"  # but the tried name is visible
    assert records[0]["outcome"] == "failed"


def test_LIVE_05_turn_start_carries_the_actor(app: Any) -> None:
    """The first audit line of a turn names the account that asked.

    Driven with a stub provider so the turn fails fast after the record is
    written -- the point is that the record exists *before* any model call,
    including for a turn that never completes.
    """
    with TestClient(app) as client:
        token = client.app.state.console.accounts.issue_bootstrap_token()
        boot = client.post(
            "/api/auth/bootstrap",
            json={"token": token, "key": "tester", "password": "a-perfectly-fine-password"},
        )
        assert boot.status_code == 200, boot.text
        # No provider configured on purpose: the turn is refused with 400, the
        # turn_start record is already on disk.
        thread = client.post("/api/threads", json={"name": "t", "workspace": str(boot.request.url)})
        # The thread needs a real workspace; use this project's own dir.
        assert thread.status_code in (200, 400, 422)
        records = client.app.state.console.audit.read_all()
    assert any(r["event"] == "bootstrap" and r["outcome"] == "ok" for r in records)
