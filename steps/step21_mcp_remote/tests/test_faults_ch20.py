"""Chapter 20: the OS boundary, and who owns the state behind it.

The tests here split in two, and the split is deliberate rather than a
compromise.

**Most of them assert on argv.** `BubblewrapSandbox.wrap` is a pure function
from a spec to a command line, so "does `read-only` refuse to bind anything
writable" is a question about a list of strings and needs no kernel. Those run
on Windows, on macOS, in CI, everywhere the other twenty chapters run.

**A few of them assert on a kernel**, and those are guarded by
`needs_bwrap`. The guard names the missing thing -- `bwrap` on PATH -- rather
than the platform, because "skipped: not Linux" is a sentence that stops
anybody asking why the Linux CI box skipped it too. A security test that
skips quietly is worse than a security test that does not exist: the absent
one is a known gap, the quiet one is a green tick over a hole.

What is *not* here: proof that bubblewrap isolates correctly. That is the
kernel's job and `probe_sandbox.py`'s measurement, not a unit test's claim.
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

import pytest

from minicodex.policy import Decision, Risk, judge_command, policy_tables
from minicodex.sandbox import (
    BubblewrapSandbox,
    NoSandbox,
    SandboxSpec,
    for_mode,
    setup_failure,
)
from minicodex.shell import ShellSession
from minicodex.tenancy import DEFAULT_OWNER, Owner, OwnerError

needs_bwrap = pytest.mark.skipif(
    shutil.which("bwrap") is None,
    reason="no bwrap on this machine -- see probe_sandbox.py, which measures the kernel",
)


def _wrap(mode: str, root: Path, command: str = "echo hi", **kw: object) -> list[str]:
    sandbox = BubblewrapSandbox(SandboxSpec.for_mode(mode, root, **kw))  # type: ignore[arg-type]
    argv = sandbox.wrap(command, cwd=str(root))
    assert argv is not None
    return argv


def _pairs(argv: list[str], flag: str) -> list[tuple[str, str]]:
    """Every `(src, dst)` for one bind flag, in the order bwrap will apply them."""
    return [
        (argv[i + 1], argv[i + 2])
        for i, tok in enumerate(argv)
        if tok == flag and i + 2 < len(argv)
    ]


# ---------------------------------------------------------------------------
# F20-01  the judgement layer allowed things nothing then stopped
# ---------------------------------------------------------------------------


def test_F20_01_read_only_binds_nothing_writable(tmp_path: Path) -> None:
    """The mode's whole promise, as a property of the command line.

    F05-04 is the reason this is worth a test rather than a comment: chapter
    5 measured `python -c "open('x','w')"` writing happily in this mode, and
    its own note says tokenising "tells you nothing" about a path inside a
    string argument. Nothing about the *command* can fix that. What fixes it
    is that there is no writable path in the namespace at all.
    """
    argv = _wrap("read-only", tmp_path)
    assert _pairs(argv, "--bind") == []
    assert ("/", "/") in _pairs(argv, "--ro-bind")


def test_F20_01_workspace_write_binds_the_workspace_and_nothing_else(tmp_path: Path) -> None:
    argv = _wrap("workspace-write", tmp_path)
    assert _pairs(argv, "--bind") == [(str(tmp_path.resolve()), str(tmp_path.resolve()))]


def test_F20_01_full_access_is_not_wrapped_at_all(tmp_path: Path) -> None:
    """An honest absence rather than a decorative presence.

    Wrapping `full-access` with everything bound read-write would spend a
    process and a namespace to enforce nothing, and would put `bwrap` in `ps`
    output for a session that has no boundary. Somebody reading that output
    would conclude the wrong thing.
    """
    sandbox = BubblewrapSandbox(SandboxSpec.for_mode("full-access", tmp_path))
    assert sandbox.wrap("echo hi", cwd=str(tmp_path)) is None


@needs_bwrap
def test_F20_01_an_interpreter_cannot_write_in_read_only(tmp_path: Path) -> None:
    """F05-04's payload, against a kernel. Measured: `OSError: [Errno 30]`."""
    import subprocess

    target = tmp_path / "escaped.txt"
    argv = _wrap("read-only", tmp_path, f"python3 -c \"open('{target}','w').write('x')\"")
    got = subprocess.run(argv, capture_output=True, text=True, timeout=30)
    assert got.returncode != 0
    assert not target.exists()


# ---------------------------------------------------------------------------
# F20-02  a sandbox that is missing must not be a sandbox that is quiet
# ---------------------------------------------------------------------------


def test_F20_02_a_missing_binary_is_reported_before_anything_runs(tmp_path: Path) -> None:
    """`unavailable()` exists so the check happens at startup, not at the
    first command -- by then the user has been told the session is confined."""
    sandbox = BubblewrapSandbox(
        SandboxSpec.for_mode("read-only", tmp_path), binary="definitely-not"
    )
    why = sandbox.unavailable()
    assert why is not None
    assert "definitely-not" in why
    assert "full-access" in why  # names the way out, not just the problem


def test_F20_02_for_mode_does_not_silently_fall_back(tmp_path: Path) -> None:
    """The fault this whole fault-id is about.

    A factory that returned `NoSandbox()` when bubblewrap is missing would be
    the tidy version and the dangerous one: `read-only` would keep its name
    and lose its meaning. `for_mode` always returns the real thing and lets
    `unavailable()` speak.
    """
    sandbox = for_mode("read-only", tmp_path)
    assert isinstance(sandbox, BubblewrapSandbox)


def test_F20_02_no_sandbox_says_so_when_asked(tmp_path: Path) -> None:
    assert NoSandbox().wrap("echo hi", cwd=str(tmp_path)) is None
    assert NoSandbox().unavailable() is not None


# ---------------------------------------------------------------------------
# F20-04  network is its own switch
# ---------------------------------------------------------------------------


def test_F20_04_network_is_off_by_default_in_every_confined_mode(tmp_path: Path) -> None:
    for mode in ("read-only", "workspace-write"):
        assert "--unshare-net" in _wrap(mode, tmp_path)


def test_F20_04_the_four_combinations_are_all_reachable(tmp_path: Path) -> None:
    """codex keeps these apart at the type level -- `NetworkSandboxPolicy` is
    its own enum beside a per-path `FileSystemAccessMode`
    (`codex-rs/protocol/src/permissions.rs:78-118`). Folding network into the
    filesystem mode would make two of these four unreachable, and both of the
    unreachable ones are real configurations: read the repo while fetching
    docs, write the repo with no egress.
    """
    seen = set()
    for mode in ("read-only", "workspace-write"):
        for network in (False, True):
            spec = SandboxSpec(mode=mode, root=tmp_path, network=network)  # type: ignore[arg-type]
            argv = BubblewrapSandbox(spec).wrap("echo hi", cwd=str(tmp_path))
            assert argv is not None
            seen.add((mode, "--unshare-net" not in argv))
    assert len(seen) == 4


def test_F20_04_full_access_keeps_its_name(tmp_path: Path) -> None:
    """The one place network follows the mode: a `full-access` session with
    the network taken away would be a lie told by a constant."""
    assert SandboxSpec.for_mode("full-access", tmp_path).network is True
    assert SandboxSpec.for_mode("read-only", tmp_path).network is False


# ---------------------------------------------------------------------------
# F20-05  bwrap's failures and the command's failures share an exit code
# ---------------------------------------------------------------------------


def test_F20_05_a_wrapper_failure_is_told_apart_from_a_command_failure() -> None:
    """Measured (`probe_sandbox.py exits`): every bwrap setup failure exits 1,
    and so does a command that ran and returned 1. The exit code cannot
    separate them, so the prefix has to."""
    assert setup_failure("bwrap: Can't chdir to /nope: No such file or directory", 1) is not None
    assert setup_failure("make: *** No targets specified.  Stop.", 1) is None


def test_F20_05_only_exit_code_one_is_examined() -> None:
    """A command exiting 42 is a command, whatever it printed."""
    assert setup_failure("bwrap: something", 42) is None
    assert setup_failure("bwrap: something", 0) is None


def test_F20_05_the_message_survives_leading_whitespace() -> None:
    assert setup_failure("\n  bwrap: Unknown option --nope\n", 1) == "Unknown option --nope"


def test_F20_05_an_unreachable_cwd_is_detected_before_running(tmp_path: Path) -> None:
    """Chapter 13 recorded that `cd` is not containment-checked and a cwd can
    wander outside the sandbox root. For nineteen chapters that was harmless;
    with a fixed bind set it makes every later command fail with a message
    naming the wrong cause."""
    sandbox = BubblewrapSandbox(SandboxSpec.for_mode("workspace-write", tmp_path))
    assert sandbox.chdir_is_reachable(str(tmp_path)) is True
    assert sandbox.chdir_is_reachable(str(tmp_path / "nope" / "nothing")) is False


# ---------------------------------------------------------------------------
# F20-06  the timeout has to reach through the wrapper
# ---------------------------------------------------------------------------


def test_F20_06_the_pid_namespace_is_requested(tmp_path: Path) -> None:
    """Chapter 2's `_kill_group` kills a process group. With `--unshare-pid`
    bwrap is pid 1 inside the namespace, so killing it takes everything with
    it -- measured at five descendants before, zero after."""
    argv = _wrap("workspace-write", tmp_path)
    assert "--unshare-pid" in argv
    assert "--die-with-parent" in argv


# ---------------------------------------------------------------------------
# F20-07  two mechanisms, one intention
# ---------------------------------------------------------------------------


def test_F20_07_extra_read_roots_reach_the_bind_set(tmp_path: Path) -> None:
    """Chapter 16 gave `read_file` a second boundary so it could reach
    `~/.minicodex/memories`. That boundary is enforced in Python. The shell's
    is enforced by the kernel, and neither knows about the other -- a path
    added to one and not the other produces a tool that can read a file its
    sibling cannot.
    """
    memories = tmp_path / "home" / "memories"
    memories.mkdir(parents=True)
    argv = _wrap("read-only", tmp_path / "ws", read_roots=(memories,))
    assert (str(memories.resolve()), str(memories.resolve())) in _pairs(argv, "--ro-bind-try")


def test_F20_07_a_read_root_is_bound_after_the_writable_holes(tmp_path: Path) -> None:
    """Order matters to bwrap -- last bind wins. A read root that lands before
    the writable workspace would be re-mounted read-write by it, silently
    widening the very boundary it was added to narrow."""
    ws = tmp_path / "ws"
    ws.mkdir()
    inner = ws / "memories"
    inner.mkdir()
    argv = _wrap("workspace-write", ws, read_roots=(inner,))
    assert argv.index("--bind") < argv.index("--ro-bind-try")


def test_F20_07_a_missing_read_root_does_not_stop_the_session(tmp_path: Path) -> None:
    """`--ro-bind-try`, not `--ro-bind`: measured, binding an absent source
    makes bwrap refuse to start at all. A user who has never written a memory
    would get no shell rather than no memory."""
    argv = _wrap("read-only", tmp_path, read_roots=(tmp_path / "never-created",))
    assert "--ro-bind-try" in argv
    assert "--ro-bind-try" not in argv[: argv.index("--ro-bind-try")]


# ---------------------------------------------------------------------------
# the payoff: enforcement lets the judgement layer stop asking
# ---------------------------------------------------------------------------


def test_F20_01_confinement_lets_workspace_write_allow_writes() -> None:
    """F05-06 measured approval fatigue at 2-4 prompts per task. The prompts
    this removes are the ones that existed only because *where* the bytes
    landed was unknowable."""
    unconfined = judge_command("rm -rf build", mode="workspace-write", policy="on-request")
    confined = judge_command(
        "rm -rf build", mode="workspace-write", policy="on-request", confined=True
    )
    assert unconfined.decision is Decision.ASK
    assert confined.decision is Decision.ALLOW
    assert confined.risk is Risk.WRITE


def test_F20_01_confinement_does_not_open_the_network() -> None:
    """`--unshare-net` makes a network command harmless, not successful. It
    still asks, because F05-09 measured what a silent denial costs: the model
    reads it as a broken environment and retries."""
    confined = judge_command(
        "curl https://example.com", mode="workspace-write", confined=True, policy="on-request"
    )
    assert confined.decision is Decision.ASK


def test_F20_01_confinement_is_off_by_default() -> None:
    """A caller who has not thought about it gets nineteen chapters of
    behaviour, not a quieter agent it never asked for."""
    assert judge_command("rm -rf build", mode="workspace-write", policy="on-request").decision is (
        Decision.ASK
    )


def test_both_mode_tables_are_pinned() -> None:
    """An entry quietly added to the confined table grants an unprompted
    capability to every sandboxed session, and looks exactly like an entry
    that belongs there. Chapter 5 pinned the classification tables for this
    reason; chapter 20 makes the difference between two tables a security
    property."""
    tables = policy_tables()
    assert tables["SHELL_ALLOWED_BY_MODE"] == [
        "full-access: INTERPRETER, NETWORK, READ, UNKNOWN, WRITE",
        "read-only: READ",
        "workspace-write: READ",
    ]
    assert tables["CONFINED_ALLOWED_BY_MODE"] == [
        "full-access: INTERPRETER, NETWORK, READ, UNKNOWN, WRITE",
        "read-only: INTERPRETER, READ",
        "workspace-write: INTERPRETER, READ, WRITE",
    ]


# ---------------------------------------------------------------------------
# backward compatibility: nineteen chapters must not notice
# ---------------------------------------------------------------------------


def test_a_shell_session_without_a_sandbox_is_unchanged(tmp_path: Path) -> None:
    assert ShellSession(cwd=str(tmp_path)).sandbox is None


class FakeSandbox:
    """A sandbox that records and substitutes, so the *seam* can be tested.

    This class exists because of a survivor.  The first run of
    `probe_mutations_ch20.py` deleted the two lines in `shell.py` that reach
    for the sandbox at all -- "the sandbox is ignored, so a confined session is
    not one" -- and **nothing failed**.  Every test that would have noticed
    needed a real `bwrap`, so on the machine this book is written on they were
    all skipped, and the mutation walked through a green suite.

    That is chapter 19's fault repeating itself one chapter later: its README
    claimed 0 survivors while three stood, and the three were its own headline
    mechanisms.  The lesson there was that a passing suite and a verification
    claim are different facts.  The lesson here is narrower and sharper: *the
    wiring is not the mechanism*.  Whether bubblewrap isolates is a question
    for a kernel; whether `shell.run` ever calls it is a question about two
    lines of Python, and answering the second one must not require the first.

    So this returns an argv that runs anywhere -- the current interpreter, with
    a script -- and records what it was asked to wrap.
    """

    def __init__(self, *, stdout: str = "wrapped", exit_code: int = 0) -> None:
        self.calls: list[tuple[str, str]] = []
        self._stdout = stdout
        self._exit = exit_code

    def wrap(self, command: str, *, cwd: str) -> list[str]:
        self.calls.append((command, cwd))
        script = f"import sys; sys.stdout.write({self._stdout!r}); sys.exit({self._exit})"
        return [sys.executable, "-c", script]

    def unavailable(self) -> str | None:
        return None


async def test_the_shell_actually_reaches_for_its_sandbox(tmp_path: Path) -> None:
    """The wiring, on every platform.

    Deleting `shell.py`'s call to `sandbox.wrap` leaves a session that says it
    is confined and is not.  Nothing about that needs a kernel to detect.
    """
    fake = FakeSandbox()
    shell = ShellSession(cwd=str(tmp_path), sandbox=fake)
    out = await shell.run("echo hi")
    assert fake.calls == [("echo hi", str(tmp_path))]
    assert "wrapped" in out


async def test_a_wrapper_failure_does_not_reach_the_model_as_command_output(
    tmp_path: Path,
) -> None:
    """The other survivor.

    `shell.run` asks `setup_failure` whether exit 1 came from the wrapper or
    from the command.  With that branch deleted, a containment failure is
    handed to the model as ordinary output and read as its own mistake -- and
    the model retries it, which is F05-09's shape arriving through a new door.
    """
    fake = FakeSandbox(
        stdout="bwrap: Can't chdir to /nope/nothing: No such file or directory",
        exit_code=1,
    )
    shell = ShellSession(cwd=str(tmp_path), sandbox=fake)
    out = await shell.run("echo hi")
    assert "the sandbox could not be built" in out
    assert "Report it rather than retrying" in out


async def test_an_ordinary_failure_is_still_an_ordinary_failure(tmp_path: Path) -> None:
    """The other half, so the branch above cannot be satisfied by always
    claiming a wrapper failure."""
    fake = FakeSandbox(stdout="make: *** No targets specified.  Stop.", exit_code=1)
    shell = ShellSession(cwd=str(tmp_path), sandbox=fake)
    out = await shell.run("make")
    assert "the sandbox could not be built" not in out
    assert "exit code 1" in out


@needs_bwrap
async def test_shell_syntax_still_works_inside_the_boundary(tmp_path: Path) -> None:
    """`create_subprocess_exec` replaces `create_subprocess_shell` on the
    sandboxed path, so the shell has to be put back explicitly -- `/bin/sh -c`
    is the tail of every argv. Without it, pipes and `&&` would silently stop
    meaning what nineteen chapters of tool descriptions say they mean."""
    shell = ShellSession(
        cwd=str(tmp_path),
        sandbox=BubblewrapSandbox(SandboxSpec.for_mode("workspace-write", tmp_path)),
    )
    assert (await shell.run("echo hello && echo world | tr a-z A-Z")).strip() == "hello\nWORLD"


def test_the_sandboxed_argv_ends_with_the_command(tmp_path: Path) -> None:
    argv = _wrap("read-only", tmp_path, "echo hi")
    assert argv[-3:] == ["/bin/sh", "-c", "echo hi"]


# ---------------------------------------------------------------------------
# F20-08  whose memory is it
# ---------------------------------------------------------------------------


def test_F20_08_the_default_owner_is_todays_paths(tmp_path: Path) -> None:
    """The whole backward-compatibility claim, as one assertion. Chapters 16
    and 17 name these paths directly; if this drifts, every test written
    before chapter 20 is describing a different program."""
    from minicodex.memory import DEFAULT_MEMORY_DIR
    from minicodex.memory_jobs import DEFAULT_JOBS_PATH
    from minicodex.memory_write import MERGE_LOCK

    assert DEFAULT_OWNER.memories() == DEFAULT_MEMORY_DIR
    assert DEFAULT_OWNER.jobs_db() == DEFAULT_JOBS_PATH
    assert DEFAULT_OWNER.merge_lock() == MERGE_LOCK


def test_F20_08_two_owners_do_not_share_a_memory_directory() -> None:
    """The fault, stated as the thing that was true before it was fixed.

    `web/runtime.py` documented one shared global memory directory as correct
    -- "every workspace this server hosts shares one, exactly as every project
    on one machine shares one in codex". Every clause is true and the
    conclusion leaks: one machine's projects are one person's, one server's
    workspaces are everybody's.
    """
    alice, bob = Owner("alice"), Owner("bob")
    assert alice.memories() != bob.memories()
    assert alice.jobs_db() != bob.jobs_db()
    assert alice.merge_lock() != bob.merge_lock()


def test_F20_08_an_owner_is_its_key(tmp_path: Path) -> None:
    """Two `Owner("alice")` built in different modules must be the same tenant."""
    assert Owner("alice", home=tmp_path) == Owner("alice", home=tmp_path)
    assert Owner("alice", home=tmp_path).memories() == Owner("alice", home=tmp_path).memories()


def test_F20_09_the_merge_lock_is_per_owner() -> None:
    """One lock for the whole machine is correct with one memory directory and
    becomes a queue with many: owner A's merge blocking owner B's, for no
    reason except a shared server."""
    assert Owner("alice").merge_lock() != Owner("bob").merge_lock()
    assert DEFAULT_OWNER.merge_lock() not in (Owner("alice").merge_lock(),)


@pytest.mark.parametrize(
    "key",
    ["../escape", "/absolute", "with space", "", "a" * 65, ".hidden", "trailing/"],
)
def test_F20_08_a_key_that_could_leave_its_directory_is_refused(key: str) -> None:
    """The key becomes a path segment, and chapter 18 already paid for a name
    that arrived as data and was turned into a path (F18-04, F18-12). There it
    was closed by looking the name up in a parsed table; an owner key is
    genuinely new data, so the check is on the characters."""
    with pytest.raises(OwnerError):
        Owner(key)


@pytest.mark.parametrize("key", ["Alice", "UPPER", "MiXeD"])
def test_F20_08_an_upper_case_key_is_refused_because_two_spellings_can_be_one_directory(
    key: str,
) -> None:
    """Split out from the traversal cases above, because the *reason* is
    different and the reason is the whole point.

    `Alice` cannot climb out of anything -- it is a perfectly ordinary
    directory name. It is refused because on a case-insensitive filesystem it
    is not a *second* directory: measured on NTFS, `mkdir alice` then
    `mkdir Alice` raises rather than creating one, leaving a single `alice`.
    So `Owner("Alice")` and `Owner("alice")` would share memories, jobs and a
    merge lock on Windows and on a default macOS volume, while staying
    correctly apart on Linux -- a leak that passes every test on any machine
    the server does not run on.

    Kept as its own test with its own name so that whoever sees it go red
    after widening `_SAFE_KEY` reads why it exists. Folded into the traversal
    list, the message would have been "could leave its directory", which is
    not true of `Alice` and invites exactly the relaxation that reopens this.
    """
    with pytest.raises(OwnerError):
        Owner(key)


@pytest.mark.parametrize("key", ["alice", "a", "team-1", "user_2", "0abc"])
def test_F20_08_ordinary_keys_are_accepted(key: str) -> None:
    assert Owner(key).root().name == key


def test_F20_08_a_tenant_layout_differs_from_the_default_by_a_prefix(tmp_path: Path) -> None:
    """One extra directory level, not a parallel tree: anything that can read
    one layout can read the other."""
    single = Owner(home=tmp_path)
    tenant = Owner("alice", home=tmp_path)
    assert tenant.memories().relative_to(tenant.root()) == single.memories().relative_to(
        single.root()
    )
