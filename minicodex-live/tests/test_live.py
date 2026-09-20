"""The deployment fork's own faults, numbered LIVE-xx.

Same discipline as `test_faults_chNN.py` -- one test per fault, the fault
reproduced before it is fixed -- but the faults here are the live server's:
things that were true of every tutorial step and only became dangerous the
moment the console got operators.

LIVE-01  The console's `sandbox_mode` setting gated approval questions and
         nothing else: `run_turn` never built a sandbox, so every command ran
         on the host.  UI promised a boundary; the kernel never saw one.
LIVE-02  A server without bubblewrap used to start anyway, and the first
         sign-in was the first time anybody found out.  The gate now refuses
         at startup unless the operator writes `--allow-unsandboxed`.
LIVE-03  `--host 0.0.0.0` with no account yet used to print the bootstrap
         token to a terminal (fine) and wait for *anyone* to reach the port
         to redeem it (not fine).  Now refused.
LIVE-07  There was no health endpoint: an uptime check could not tell the
         difference between a working server and a hung one.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest

from minicodex.composition import local_tools
from minicodex.sandbox import BubblewrapSandbox, SandboxSpec, for_mode
from minicodex.shell import ShellSession
from minicodex.tools import tool_context


class FakeSandbox:
    """Records what it was asked to wrap; the argv runs anywhere.

    The same shape `test_faults_ch20.py` uses, for the same reason: whether
    bubblewrap isolates is a kernel question, whether the shell ever reaches
    for the sandbox is two lines of Python, and only the second belongs in a
    suite that runs on every platform.
    """

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def wrap(self, command: str, *, cwd: str) -> list[str]:
        self.calls.append((command, cwd))
        script = "import sys; sys.stdout.write('wrapped'); sys.exit(0)"
        return [sys.executable, "-c", script]

    def unavailable(self) -> str | None:
        return None


# -- LIVE-01: the setting finally reaches the kernel -------------------------


@pytest.mark.anyio
async def test_LIVE_01_tool_context_with_sandbox_builds_a_confined_shell(
    tmp_path: Path,
) -> None:
    """`tool_context(sandbox=...)` builds a shell that actually wraps."""
    context = tool_context(root=tmp_path, sandbox=FakeSandbox())
    assert context.shell.sandbox is not None
    out = await context.shell.run("echo hi")
    assert context.shell.sandbox.calls == [("echo hi", str(tmp_path))]  # type: ignore[attr-defined]
    assert "wrapped" in out


@pytest.mark.anyio
async def test_LIVE_01_local_tools_forwards_the_sandbox_to_the_parent_shell(
    tmp_path: Path,
) -> None:
    """The console passes a *shell* (chapter 10's shape) plus a sandbox.

    The pre-fix world silently ignored the sandbox whenever a shell was
    supplied -- which is exactly the console's combination, which is exactly
    why LIVE-01 survived chapter 20.  Explicit object wins over the shell's
    own (None) value.
    """
    shell = ShellSession(cwd=tmp_path)
    assert shell.sandbox is None
    sandbox = FakeSandbox()
    tools = local_tools(tmp_path, session=None, shell=shell, sandbox=sandbox)  # type: ignore[arg-type]
    assert "run_shell" in tools.handlers  # bound, not exploded
    assert shell.sandbox is sandbox


def test_LIVE_01_unset_sentinel_keeps_the_preexisting_shell_alone(tmp_path: Path) -> None:
    """Leaving `sandbox` unset adopts the shell's own boundary, not a new one.

    The sub-agent path hands the parent's shell over precisely to inherit
    "where the parent is"; replacing that shell's `None` with anything here
    would quietly change what a child runs.
    """
    shell = ShellSession(cwd=tmp_path)
    tool_context(root=tmp_path, shell=shell)
    assert shell.sandbox is None
    # And the sentinel is not `None`: a caller that *says* None decided.
    tool_context(root=tmp_path, shell=shell, sandbox=None)
    assert shell.sandbox is None  # same value, but now it is a decision


def test_LIVE_01_for_mode_builds_the_spec_the_thread_asked_for(tmp_path: Path) -> None:
    """The runtime builds `for_mode(mode, root, read_roots=(owner.root(),))`.

    Asserting on the spec rather than on kernel behaviour keeps this test
    green on any platform while still catching the wiring -- a mode, root or
    read-root that stops arriving is a red test, not a silent absence.
    """
    owner_root = tmp_path / "owner"
    sandbox = for_mode("workspace-write", tmp_path / "ws", read_roots=(owner_root,))
    assert isinstance(sandbox, BubblewrapSandbox)
    spec: SandboxSpec = sandbox.spec
    assert spec.mode == "workspace-write"
    assert spec.root == (tmp_path / "ws").resolve()
    assert spec.read_roots == (owner_root.resolve(),)
    assert spec.network is False


# -- LIVE-02: the startup gate ------------------------------------------------


def test_LIVE_02_gate_refuses_to_start_without_a_sandbox(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """No bubblewrap and no override: serve exits before binding anything."""
    from minicodex.web import app as web_app

    monkeypatch.setattr(BubblewrapSandbox, "unavailable", lambda self: "no bwrap on this machine")
    rc = web_app._startup_gate("127.0.0.1", unsandboxed_ok=False)
    assert rc == 1
    out = capsys.readouterr().out
    assert "cannot confine" in out
    assert "--allow-unsandboxed" in out


def test_LIVE_02_gate_accepts_an_explicit_override(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """`--allow-unsandboxed` starts, loudly."""
    from minicodex.web import app as web_app

    monkeypatch.setattr(BubblewrapSandbox, "unavailable", lambda self: "no bwrap on this machine")
    rc = web_app._startup_gate("127.0.0.1", unsandboxed_ok=True)
    assert rc is None
    # A warning log line, not a print: the operator accepted a degraded start,
    # and a degraded start belongs in whatever collects this server's logs.
    # `caplog` catches it because pytest's root logger is configured for it.
    assert any("starting anyway" in record.getMessage() for record in caplog.records)


def test_LIVE_02_gate_passes_when_the_sandbox_can_enforce(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from minicodex.web import app as web_app

    monkeypatch.setattr(BubblewrapSandbox, "unavailable", lambda self: None)
    assert web_app._startup_gate("127.0.0.1", unsandboxed_ok=False) is None


def test_LIVE_02_gate_is_wired_into_serve(monkeypatch: pytest.MonkeyPatch) -> None:
    """Deleting the `_startup_gate` call inside `serve` must fail this test.

    A gate that nothing calls is a comment.  Asserted by inspecting the
    source rather than by launching uvicorn -- the call sits before the bind,
    and the only honest way to test that ordering without a server is to read
    it.
    """
    import inspect

    from minicodex.web import app as web_app

    source = inspect.getsource(web_app.serve)
    assert "_startup_gate(" in source
    assert source.index("_startup_gate(") < source.index("uvicorn.run(")


# -- LIVE-03: the bootstrap-token race ----------------------------------------


def test_LIVE_03_serve_refuses_non_loopback_with_no_account(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """First account must be created from a terminal, not from the network."""
    from minicodex.web import app as web_app

    monkeypatch.setattr(BubblewrapSandbox, "unavailable", lambda self: None)
    console_box: dict[str, Any] = {}

    class _EmptyAccounts:
        def empty(self) -> bool:
            return True

        def all(self) -> list[Any]:
            return []

        def issue_bootstrap_token(self) -> str | None:
            return "tok"

    class _FakeConsole:
        accounts = _EmptyAccounts()
        home = tmp_path / "home"

        class audit:
            path = tmp_path / "audit.jsonl"

    def _fake_create_app(data_dir: Path, **kwargs: Any) -> Any:
        console_box["app"] = True
        return type("A", (), {"state": type("S", (), {"console": _FakeConsole()})()})()

    monkeypatch.setattr(web_app, "create_app", _fake_create_app)
    # `serve` imports uvicorn inside the function body.  Stub the module so
    # the import succeeds but `run` is a recorder -- reaching it at all is the
    # failure this test exists to catch.
    ran: dict[str, Any] = {}

    def _record_run(*args: Any, **kwargs: Any) -> None:
        ran["called"] = True

    monkeypatch.setitem(sys.modules, "uvicorn", type("U", (), {"run": staticmethod(_record_run)}))
    rc = web_app.serve("0.0.0.0", 8000, tmp_path)
    assert rc == 1
    assert "no account exists yet" in capsys.readouterr().out
    assert "called" not in ran  # refused before the bind, not after


# -- LIVE-07: /api/health -----------------------------------------------------


def test_LIVE_07_health_is_public_and_process_only(tmp_path: Path) -> None:
    """Anonymous callers get process facts, and nothing about any account."""
    from fastapi.testclient import TestClient

    from minicodex.web import create_app

    app = create_app(tmp_path / "console", home=tmp_path / "home")
    with TestClient(app) as client:
        response = client.get("/api/health")
        assert response.status_code == 200
        body = response.json()
    assert body["ok"] is True
    assert body["turns_running"] == 0
    assert body["turns_total"] == 0
    # An anonymous caller may know *whether* the console has an account (the
    # frontend renders the bootstrap form from it), not *how many*.
    assert body["has_account"] is False
    assert "accounts" not in body
    assert "uptime_seconds" in body
    # And nothing that names a user, a thread, or a workspace:
    rendered = json.dumps(body)
    assert "tester" not in rendered
    assert "workspace" not in rendered


def test_LIVE_07_health_counts_claimed_turns_including_failures(tmp_path: Path) -> None:
    """The counter counts claims, not successes.

    Driven by setting the console fields directly rather than by running a
    failing turn: the subject is what the endpoint reports, and a real failing
    turn is LIVE-05's subject (it is already counted there).
    """
    from fastapi.testclient import TestClient

    from minicodex.web import create_app

    app = create_app(tmp_path / "console", home=tmp_path / "home")
    with TestClient(app) as client:
        console = client.app.state.console
        console.turns_total = 12
        console.turns_failed = 3
        body = client.get("/api/health").json()
    assert body["turns_total"] == 12
    assert body["turns_failed"] == 3
