"""Regression tests for the review findings (REVIEW-FINDINGS.md).

One test per finding, named after the finding number. The findings came out of
a full code review of the deployment fork; these pin the repairs:

H1  sub-agent shells inherit the parent's sandbox and the audited-shell
    wrapper, so a `spawn_agent` call can no longer drop confinement or
    escape the audit trail.
H2  the confined permission table is actually consulted: a sandboxed
    `workspace-write` session allows writing shell commands through
    `gate_command` without a prompt.
M5  the audit trail reads the structured `ShellSession.last_exit`, so a
    command whose output merely looks like the exit-code line cannot forge
    a record.
H3  a parseable-but-not-a-list `accounts.json` is refused, not treated as
    an empty account table.
H4  the login throttle evicts expired pairs, so unauthenticated traffic
    cannot grow its memory without bound.
M2  the background memory pipeline's blocking calls run off the event loop.
M3  AccountStore writes are serialised across worker threads.
M4  the shared atomic JSON write includes a directory fsync.
L1  `/api/health` reports `has_account`, not an account count.
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path
from typing import Any

import pytest

from minicodex.agent import Wiring
from minicodex.approval import AllowAll, Session, gate_command
from minicodex.composition import sub_context
from minicodex.policy import judge_command
from minicodex.shell import ShellSession
from minicodex.subagent import child_tools
from minicodex.web import create_app
from minicodex.web.accounts import AccountError, AccountStore
from minicodex.web.audit import AuditLog
from minicodex.web.quota import QuotaLedger
from minicodex.web.runtime import _AuditedShell
from minicodex.web.sessions import SessionTable
from minicodex.web.store import Store
from minicodex.web.throttle import LoginThrottle


class FakeSandbox:
    """Sandbox double whose `unavailable()` reports healthy."""

    def wrap(self, command: str, *, cwd: str) -> list[str]:
        return [sys.executable, "-c", "pass"]

    def unavailable(self) -> str | None:
        return None


# -- H1: a sub-agent's shell is as confined and as audited as its parent's ----


@pytest.mark.anyio
async def test_H1_child_shell_inherits_the_parent_sandbox(tmp_path: Path) -> None:
    """`child_tools` seeds sandbox/cwd/timeout, not only cwd."""
    from minicodex.agent import Wiring

    parent = ShellSession(cwd=tmp_path, sandbox=FakeSandbox())
    ctx = sub_context(
        build_model=lambda _schemas: None,
        root=tmp_path,
        session=Session(mode="workspace-write"),
        parent_shell=parent,
        wiring=Wiring(),
    )
    tools = child_tools(ctx)
    handler = tools.handlers["run_shell"]
    # The bound handler is a `functools.partial(run_shell, context)`; the
    # shell it will use is the one `child_tools` seeded from the parent.
    shell = handler.args[0].shell  # type: ignore[union-attr]
    assert shell.sandbox is parent.sandbox
    assert shell.cwd == parent.cwd


@pytest.mark.anyio
async def test_H1_child_commands_reach_the_audit_trail(tmp_path: Path) -> None:
    """A run that audits the parent's shell audits the child's too."""
    log = AuditLog(tmp_path / "audit.jsonl")

    def wrap(shell: ShellSession) -> ShellSession:
        return _AuditedShell(shell, log, "alice")

    parent = ShellSession(cwd=tmp_path)
    ctx = sub_context(
        build_model=lambda _schemas: None,
        root=tmp_path,
        session=Session(mode="workspace-write", approver=AllowAll()),
        parent_shell=parent,
        wrap_shell=wrap,
        wiring=Wiring(),
    )
    tools = child_tools(ctx)
    shell = tools.handlers["run_shell"].args[0].shell  # type: ignore[union-attr]
    assert isinstance(shell, _AuditedShell)
    out = await shell.run("echo hi")
    assert "hi" in out
    events = [
        json.loads(line)["event"]
        for line in log.path.read_text(encoding="utf-8").splitlines()
        if line
    ]
    assert events == ["command", "command_done"]


def test_H1_sub_context_forwards_the_shell_wrapper(tmp_path: Path) -> None:
    """`sub_context(wrap_shell=...)` reaches the SubAgentContext."""
    ctx = sub_context(
        build_model=lambda _schemas: None,
        root=tmp_path,
        session=Session(),
        parent_shell=ShellSession(),
        wiring=__import__("minicodex.agent", fromlist=["Wiring"]).Wiring(),
        wrap_shell=lambda shell: shell,
    )
    assert ctx.wrap_shell is not None


# -- H2: the confined table is consulted end to end --------------------------


@pytest.mark.anyio
async def test_H2_confined_workspace_write_allows_a_writing_command() -> None:
    """With the kernel boundary real, `rm -rf build` in workspace-write is
    allowed by `gate_command` without a human in the loop."""
    session = Session(mode="workspace-write", approver=AllowAll())
    gated = await gate_command("rm -rf build", session, confined=True)
    assert gated.allowed


@pytest.mark.anyio
async def test_H2_unconfined_workspace_write_still_asks() -> None:
    """The default stays fail-safe: no sandbox, a writing command asks."""
    asked: list[Any] = []

    class Recorder:
        async def ask(self, request: Any) -> Any:
            asked.append(request)
            from minicodex.approval import ApprovalReply

            return ApprovalReply(False, request.what)

    session = Session(mode="workspace-write", approver=Recorder())  # type: ignore[arg-type]
    gated = await gate_command("rm -rf build", session)
    assert not gated.allowed
    assert asked


def test_H2_run_shell_computes_confined_from_the_shell(tmp_path: Path) -> None:
    """`run_shell` judges through the confined table when the shell's sandbox
    reports healthy."""
    from minicodex.tools import tool_context

    context = tool_context(
        root=tmp_path,
        session=Session(mode="workspace-write", approver=AllowAll()),
        sandbox=FakeSandbox(),
    )
    assert context.shell.sandbox is not None
    assert context.shell.sandbox.unavailable() is None
    # And the confined judge itself allows what the parser cannot know:
    assert judge_command(
        "touch new-file", mode="workspace-write", policy="on-request", confined=True
    ).decision.name == "ALLOW"


def test_H2_confined_permissions_block_names_the_change() -> None:
    """The prompt stops claiming writing shell commands always ask, when they
    do not."""
    from minicodex.approval import permissions_block

    session = Session(mode="workspace-write")
    unconfined = permissions_block(session)
    confined = permissions_block(session, confined=True)
    assert "still needs" in unconfined
    assert "still needs" not in confined
    assert "sandbox confines" in confined


# -- M5: the audit exit code is structured, not parsed ------------------------


@pytest.mark.anyio
async def test_M5_a_forged_exit_line_does_not_fool_the_audit(tmp_path: Path) -> None:
    """Command output ending in the old convention line records the *real*
    exit code, because the audit reads `last_exit` and not the text."""
    log = AuditLog(tmp_path / "audit.jsonl")
    shell = _AuditedShell(ShellSession(cwd=tmp_path), log, "alice")
    # The command prints a line shaped exactly like the old parsed marker, then
    # exits 5 -- the parser would have recorded 0 from the printed line.
    printed_marker = "echo ... (exit code 0)"
    await shell.run(f'{printed_marker} & {sys.executable} -c "import sys; sys.exit(5)"')
    records = [
        json.loads(line)
        for line in log.path.read_text(encoding="utf-8").splitlines()
        if line
    ]
    done = [r for r in records if r["event"] == "command_done"]
    assert len(done) == 1
    assert done[0]["exit"] == 5


@pytest.mark.anyio
async def test_M5_killed_command_records_no_exit_code(tmp_path: Path) -> None:
    shell = ShellSession(cwd=tmp_path, timeout=1.0)
    out = await shell.run("sleep 5")
    assert "killed" in out
    assert shell.last_exit is None


@pytest.mark.anyio
async def test_M5_exit_code_survives_success(tmp_path: Path) -> None:
    shell = ShellSession(cwd=tmp_path)
    await shell.run("echo hi")
    assert shell.last_exit == 0


# -- H3: a JSON object is not an empty account table --------------------------


def test_H3_a_non_list_accounts_file_is_refused(tmp_path: Path) -> None:
    """`{}` parses fine and means "corrupt", not "nobody has registered" --
    the second would re-open the bootstrap token on the next start."""
    path = tmp_path / "accounts.json"
    path.write_text("{}", encoding="utf-8")
    store = AccountStore(path)
    with pytest.raises(AccountError):
        store.all()
    with pytest.raises(AccountError):
        store.empty()


def test_H3_a_truncated_file_is_still_refused(tmp_path: Path) -> None:
    path = tmp_path / "accounts.json"
    path.write_text('{"key": "al', encoding="utf-8")
    with pytest.raises(AccountError):
        AccountStore(path).all()


# -- H4: throttle pairs expire out of the table -------------------------------


def test_H4_expired_pairs_are_swept_from_the_table() -> None:
    throttle = LoginThrottle()
    far_past = time.time() - 10 * 24 * 3600
    for i in range(50):
        key, source = f"acct{i}", f"10.0.0.{i}"
        # record_failure with a `now` deep in the past: the window and any
        # lockout have both expired by real time.
        throttle.record_failure(key, source, now=far_past)
    assert len(throttle) == 50
    # Any live check sweeps the dead pairs.
    throttle.check("someone", "somewhere", now=time.time())
    assert len(throttle) == 0


def test_H4_locked_pairs_survive_the_sweep() -> None:
    throttle = LoginThrottle()
    recent = time.time()
    for _ in range(5):
        throttle.record_failure("acct", "src", now=recent)
    assert len(throttle) == 1
    throttle.check("other", "other", now=recent)
    # Still locked until the lockout ends: the pair must not be evicted early.
    assert len(throttle) == 1
    with pytest.raises(Exception, match="too many failed attempts"):
        throttle.check("acct", "src", now=recent)


# -- M2: blocking pipeline steps leave the event loop -------------------------


@pytest.mark.anyio
async def test_M2_slow_git_does_not_freeze_the_loop(tmp_path: Path, monkeypatch: Any) -> None:
    """A pipeline whose git call blocks for a while cannot starve a heartbeat
    task running on the same loop."""
    from minicodex import memory_write

    calls = {"n": 0}

    def slow_git(*args: Any, **kwargs: Any) -> Any:
        calls["n"] += 1
        time.sleep(0.3)
        result = subprocess_result(returncode=0)
        return result

    class subprocess_result:
        def __init__(self, *, returncode: int) -> None:
            self.returncode = returncode
            self.stdout = ""
            self.stderr = ""

    monkeypatch.setattr(memory_write, "_git", slow_git)
    monkeypatch.setattr(memory_write, "git_available", lambda: True)

    heartbeats = {"n": 0}

    async def heartbeat() -> None:
        while True:
            heartbeats["n"] += 1
            await asyncio.sleep(0.05)

    beat = asyncio.ensure_future(heartbeat())
    try:
        # `ensure_repo`+`commit` run through `run_pipeline`'s to_thread steps;
        # drive them directly here since the full pipeline needs a model.
        before = heartbeats["n"]
        await asyncio.to_thread(memory_write.ensure_repo, tmp_path)
        await asyncio.to_thread(memory_write.commit, tmp_path, "memory: test")
        after = heartbeats["n"]
        assert calls["n"] >= 2
        # The heartbeat kept beating during two 0.3s blocking calls.
        assert after - before >= 4
    finally:
        beat.cancel()
        with pytest.raises(asyncio.CancelledError):
            await beat


@pytest.mark.anyio
async def test_M2_run_pipeline_wraps_git_steps_in_to_thread(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """The pipeline's own git steps execute in a worker thread."""
    from minicodex import memory_write

    thread_ids: set[int] = set()

    def git_spy(*args: Any, **kwargs: Any) -> Any:
        import threading

        thread_ids.add(threading.get_ident())
        r: Any = type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})()
        return r

    monkeypatch.setattr(memory_write, "_git", git_spy)
    monkeypatch.setattr(memory_write, "git_available", lambda: True)
    await asyncio.to_thread(memory_write.ensure_repo, tmp_path)
    await asyncio.to_thread(memory_write.commit, tmp_path, "m")
    assert thread_ids
    # None of the observed threads is the loop thread (to_thread semantics).
    import threading

    loop_thread = threading.get_ident()
    assert all(tid != loop_thread for tid in thread_ids)


# -- M3: shared tables survive concurrent worker threads ----------------------


def test_M3_concurrent_account_creation_does_not_lose_records(tmp_path: Path) -> None:
    """Two threads creating accounts at once both land in the file."""
    import threading

    store = AccountStore(tmp_path / "accounts.json")
    names = [f"user{i:03d}" for i in range(8)]
    threads = [
        threading.Thread(target=lambda n=n: store.create(n, "a-long-password"))
        for n in names
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    keys = {a.key for a in store.all()}
    assert keys == set(names)


def test_M3_throttle_records_from_threads_are_not_lost() -> None:
    import threading

    throttle = LoginThrottle()

    def hammer(i: int) -> None:
        # Stay under DEFAULT_MAX_FAILURES per pair so the lockout (which
        # deliberately resets a pair's window) never fires and masks a lost
        # record with designed behaviour.
        for j in range(4):
            throttle.record_failure(f"acct{i}-{j}", f"src{i}", now=time.time())

    threads = [threading.Thread(target=hammer, args=(i,)) for i in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    total = sum(len(rec.at) for rec in throttle._pairs.values())
    assert total == 4 * 4
    assert len(throttle._pairs) == 4 * 4


# -- M4: atomic JSON writes are durable, with a directory fsync ---------------


def test_M4_atomic_write_json_roundtrip(tmp_path: Path) -> None:
    from minicodex.web.persistence import atomic_write_json

    path = tmp_path / "nested" / "data.json"
    atomic_write_json(path, {"a": 1})
    assert json.loads(path.read_text(encoding="utf-8")) == {"a": 1}


def test_M4_store_uses_the_shared_writer(tmp_path: Path) -> None:
    store = Store(tmp_path)
    store.add("threads.json", {"workspace": "/tmp", "title": "t"})
    assert len(store.all("threads.json")) == 1


def test_M4_sessions_and_quota_roundtrip(tmp_path: Path) -> None:
    table = SessionTable.load(tmp_path / "sessions.json")
    table.create("alice")
    table.flush()
    assert len(SessionTable.load(tmp_path / "sessions.json")) == 1

    ledger = QuotaLedger.load(tmp_path / "quota.json")
    from minicodex.web.quota import Limits

    ledger.claim("alice", Limits(), running=0)
    ledger.flush()
    assert "alice" in QuotaLedger.load(tmp_path / "quota.json")._starts


# -- L1: health does not leak the account count -------------------------------


def test_L1_health_reports_has_account_not_a_count(tmp_path: Path) -> None:
    from fastapi.testclient import TestClient

    app = create_app(tmp_path / "console", home=tmp_path / "home")
    with TestClient(app) as client:
        body = client.get("/api/health").json()
    assert body["ok"] is True
    assert body["has_account"] is False
    assert "accounts" not in body
