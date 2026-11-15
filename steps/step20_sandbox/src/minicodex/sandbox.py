"""Making "allowed" mean something: the OS boundary under `run_shell`.

For nineteen chapters `policy.judge_command` has been the sandbox.  It reads a
command, decides how risky it looks, and either runs it or asks a human.  What
it has never done is *stop* anything: once the verdict is `ALLOW`, `shell.run`
hands the string to a subprocess with the whole filesystem and the whole
network behind it.

Chapter 5 knew, and wrote both halves of it down:

* **F05-04** -- `python -c "open('x','w')"` writes in `read-only` mode.  The
  fault list's own words: *"Tokenising is perfect here and tells you nothing --
  the danger is inside a string argument."*
* **F05-05** -- `curl` exfiltrates.  Classified `NETWORK`, and *"recognised
  rather than blocked ... a real network policy needs the OS sandbox this
  chapter does not build."*

`policy.py` says the same thing in a comment above `_SHELL_ALLOWED_BY_MODE`:
the thing that enforces "inside the workspace" is an OS sandbox, and *"this
chapter does not build one"*.  This module is that sandbox, twenty chapters
later, on Linux only.

Two consequences worth stating before the code, because they are the reason
the chapter exists rather than side effects of it:

**Enforcement lets the judgement layer relax.**  `_SHELL_ALLOWED_BY_MODE`
grants `workspace-write` exactly `{Risk.READ}` today -- writing always asks,
even in the mode named for writing, because *where* the bytes land was
unknowable.  Under a sandbox it is knowable, so `workspace-write` can finally
mean what it says.  That is not just safety: F05-06 measured approval fatigue
at 2-4 prompts per task, and the prompts this removes are the ones a user was
answering without reading.

**Enforcement makes ownership answerable.**  A bind set has to name a
directory, which forces the question of whose directory it is -- see
`Owner` in `tenancy.py`.

Why bubblewrap and not landlock directly: codex ships both, and bundles bwrap
itself (`codex-rs/bwrap/`, `codex-rs/linux-sandbox/src/bundled_bwrap.rs`).
Calling landlock from Python means `ctypes` against raw syscall numbers and
hand-assembled seccomp BPF; that is a chapter about an ABI, not about
isolation.  bwrap is a subprocess whose *arguments are the policy*, which is
readable, testable, and printable in a tutorial.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from minicodex.policy import SandboxMode

# The wrapper binary.  Looked up once by name rather than hard-coded to
# `/usr/bin/bwrap`: distributions disagree (Debian and Ubuntu use /usr/bin,
# Nix and Flatpak runtimes do not), and a wrong absolute path fails as "no
# such file" at the first command instead of as "not installed" at startup.
#
# codex resolves this the same way and then goes further, falling back to a
# copy it bundles when the system has none (`bundled_bwrap.rs`).  Bundling a
# binary is a packaging problem, not a teaching one, so this stops at the
# lookup and says so when it fails (F20-02).
BWRAP = "bwrap"

# Measured, not chosen.  `probe_sandbox.py binds` walks up from the smallest
# bind set that could plausibly work:
#
#     --ro-bind <ws> <ws>                          exit=1  execvp /bin/sh: No such file
#     --ro-bind <ws> <ws> --ro-bind /bin /bin      exit=1  execvp /bin/sh: No such file
#     --ro-bind <ws> <ws> --ro-bind /bin /bin
#                         --ro-bind /lib /lib
#                         --ro-bind /lib64 /lib64  exit=0  ok
#     --ro-bind / /                                exit=0  ok
#
# So "bind only the workspace" is not a sandbox, it is a broken one: the
# interpreter has to be reachable or nothing runs at all.  Binding `/`
# read-only and then punching writable holes in it is the shape that works,
# and it is also what makes the failure mode safe -- forgetting to add a
# writable path denies a write, where forgetting to add a readable path in the
# other design denies *everything*, including `sh`.
#
# A plain string, and `Path("/")` was tried first.  It is wrong for a reason
# that only shows up off Linux: `str(Path("/"))` is `"\\"` on Windows, so the
# argv this module builds while running on the machine this book was written
# on named a directory bwrap has never heard of.  The test asserting on the
# bind set is what caught it.  The root of a bwrap namespace is a *Linux path
# literal*, not a path on whatever host happens to be constructing the command
# line, and the type has to say so.
_ROOT = "/"


@dataclass(frozen=True)
class SandboxSpec:
    """What one conversation's commands are allowed to reach.

    `network` is deliberately its own field rather than being derived from
    `mode` at the point of use.  codex separates them at the type level --
    `NetworkSandboxPolicy` is `Restricted | Enabled` and lives beside a
    per-path `FileSystemAccessMode { Read, Write, Deny }`
    (`codex-rs/protocol/src/permissions.rs:78-118`) -- and the reason is that
    the four combinations are all real.  "Read the repo, reach the internet"
    is a research task; "write the repo, no internet" is the default for
    anything touching a package manager.  Folding network into a filesystem
    enum makes two of the four unreachable, which is the same argument
    `policy.py` already makes for keeping `SandboxMode` and `ApprovalPolicy`
    apart.

    `read_roots` carries chapter 16's extra read-only root, and what it does
    is narrower than it looks.  The namespace starts from `--ro-bind / /`, so
    the shell can already *read* every file the host user can -- with or
    without this field.  What a read root adds is protection from *writing*
    when it lies inside a writable hole: a memory directory inside the
    workspace stays read-only.  So the two boundaries are not mirror images.
    `paths.resolve(..., extra_roots=...)` bounds what `read_file` may read, in
    Python; the sandbox bounds what `run_shell` may *write* and whether it may
    reach the network, in the kernel, and does not bound reading at all
    (F20-07).  The first version of this paragraph said a path missing from
    the sandbox would be unreadable by the shell; measured under bwrap, it is
    readable either way.  Found when chapter 20 was rewritten.
    """

    mode: SandboxMode
    root: Path
    read_roots: tuple[Path, ...] = field(default_factory=tuple)
    network: bool = False

    @staticmethod
    def for_mode(
        mode: SandboxMode,
        root: Path,
        *,
        read_roots: tuple[Path, ...] = (),
    ) -> SandboxSpec:
        """The default spec for a mode.

        Network follows the mode here, and this is the *only* place it does.
        `full-access` is the mode that means "no boundary", so denying it the
        network would make the name a lie; everything else starts closed and a
        caller who wants otherwise passes `network=True` explicitly.
        """
        return SandboxSpec(
            mode=mode,
            root=root.resolve(),
            read_roots=tuple(p.resolve() for p in read_roots),
            network=(mode == "full-access"),
        )

    def writable(self) -> tuple[Path, ...]:
        """Paths the payload may write to.

        `read-only` gets nothing, which is the whole point of the name.
        `full-access` gets the whole filesystem: with the network on it is not
        wrapped at all (see `BubblewrapSandbox.wrap`), and with the network off
        it is wrapped only to take the network away.

        That second case arrived when chapter 20 was rewritten.  This method
        used to return nothing for `full-access`, on the assumption that a
        `full-access` session is never wrapped -- but `for_mode(...,
        network=False)` wraps one, and then `--ro-bind / /` with no holes made
        "full access, no network" unable to write anywhere, not even its own
        workspace: stricter than `workspace-write`, under the name that means
        "no boundary".
        """
        if self.mode == "workspace-write":
            return (self.root,)
        if self.mode == "full-access":
            return (Path(_ROOT),)
        return ()


class Sandbox(Protocol):
    """How `shell.run` asks for a boundary.

    Two methods, and the second one is the interesting half.  `wrap` is called
    per command; `unavailable` is called once, at startup, and exists because
    a sandbox that turns out to be missing at the first command has already
    told the user their session was protected (F20-02).
    """

    def wrap(self, command: str, *, cwd: str) -> list[str] | None:
        """argv to run instead of `command`, or `None` to run it unwrapped."""
        ...

    def unavailable(self) -> str | None:
        """Why this sandbox cannot enforce anything, or `None` if it can."""
        ...


class NoSandbox:
    """No boundary, said out loud.

    Not the same thing as `sandbox=None`.  `None` is "this caller predates
    chapter 20"; this class is "somebody decided".  Keeping them distinct is
    what lets `unavailable()` be a startup check rather than a guess -- an
    explicit `NoSandbox` under `read-only` is a misconfiguration worth
    refusing, and an implicit `None` is nineteen chapters of tests.
    """

    def wrap(self, command: str, *, cwd: str) -> list[str] | None:
        return None

    def unavailable(self) -> str | None:
        return "no sandbox was configured, so nothing is enforced"


@dataclass(frozen=True)
class BubblewrapSandbox:
    """A `bwrap` command line built from a `SandboxSpec`.

    Stateless and frozen: `wrap` is a pure function of the spec and the cwd,
    which is what makes the whole of this module testable on a machine with no
    `bwrap` and no Linux (the tests assert on argv; `probe_sandbox.py` asserts
    on kernels).
    """

    spec: SandboxSpec
    binary: str = BWRAP

    def unavailable(self) -> str | None:
        found = shutil.which(self.binary)
        if found is None:
            return (
                f"{self.binary!r} is not on PATH, so commands cannot be confined. "
                f"Install bubblewrap (apt install bubblewrap) or run with "
                f"--sandbox full-access if you accept an unconfined shell."
            )
        return None

    def wrap(self, command: str, *, cwd: str) -> list[str] | None:
        # `full-access` is not wrapped at all.  Wrapping it with everything
        # bound read-write would cost a process and a namespace to achieve
        # exactly nothing, and -- worse -- it would make the sandbox look
        # engaged in `ps` output while enforcing no boundary.  An honest
        # absence beats a decorative presence.
        if self.spec.mode == "full-access" and self.spec.network:
            return None

        argv = [self.binary]

        # Everything readable, nothing writable -- then holes.  See `_ROOT`
        # above for why this is a measurement rather than a preference.
        argv += ["--ro-bind", _ROOT, _ROOT]

        # `as_posix()`, for `_ROOT`'s reason: these are paths inside a Linux
        # namespace, whatever host is building the command line.
        for path in self.spec.writable():
            argv += ["--bind", path.as_posix(), path.as_posix()]

        # Chapter 16's memory directory, and anything else the caller has
        # already granted `read_file`.  Everything is readable already (see
        # `SandboxSpec`), so what this buys is a read-only *window* inside a
        # writable hole -- and that only works because it is bound *after*
        # the holes: bwrap applies binds in order and the last one wins, so a
        # read root bound first would be re-mounted writable by the workspace.
        for path in self.spec.read_roots:
            argv += ["--ro-bind-try", str(path), str(path)]

        # `/proc` and `/dev` are not inherited through a new namespace; a
        # payload without them fails in ways that look like the command's
        # fault rather than the wrapper's (`python` needs /proc for a
        # surprising amount, and anything writing to /dev/null needs /dev).
        argv += ["--dev", "/dev", "--proc", "/proc"]

        if not self.spec.network:
            argv.append("--unshare-net")

        # `--unshare-pid` is what makes the timeout work.  Chapter 2's
        # `_kill_group` kills the process group; with a PID namespace bwrap is
        # pid 1 inside it, and killing pid 1 takes the namespace and every
        # process in it.  Measured (`probe_sandbox.py kill`): five descendants
        # before the kill, zero after.
        #
        # `--die-with-parent` is the same guarantee from the other end -- if
        # the agent process dies without running its own cleanup, the kernel
        # tears the payload down instead of leaving it orphaned.
        argv += ["--unshare-pid", "--die-with-parent"]

        # `--chdir` rather than passing `cwd=` to the subprocess: the path has
        # to be resolved *inside* the namespace, and the two are not the same
        # directory.  Under `--ro-bind / /` every host directory is present,
        # so `--chdir` fails only for one that does not exist -- and then bwrap
        # exits 1 with `bwrap: Can't chdir to ...`, which `setup_failure`
        # below reports as the sandbox's failure rather than the command's.
        argv += ["--chdir", cwd]

        # `/bin/sh -c`, so that everything chapters 2 to 19 assume about shell
        # syntax -- pipes, `&&`, redirects, quoting -- still holds inside the
        # boundary.  Swapping to `create_subprocess_exec` without this would
        # silently change what a command *means*, which is a much bigger
        # behaviour change than adding a sandbox.
        argv += ["/bin/sh", "-c", command]
        return argv


# bwrap reports its own setup failures on stderr and exits 1 -- the same exit
# code as a command that ran and returned 1.  Measured, all of these exit 1:
#
#     bwrap: Can't chdir to /tmp/elsewhere: No such file or directory
#     bwrap: Can't find source path /definitely/not/here: No such file or directory
#     bwrap: execvp /bin/sh: No such file or directory
#     bwrap: Unknown option --not-a-real-flag
#
# while `sh -c 'exit 42'` correctly propagates 42.  So exit codes cannot tell
# the two apart and the prefix has to.  This is a heuristic and it is worth
# being honest about its edge: a command whose own output begins with `bwrap: `
# would be misread.  The alternative -- `--info-fd` on a separate pipe -- is
# strictly better and is left as an exercise, because it costs a second
# reader in `shell.run` for a case measured at zero occurrences.
_BWRAP_ERROR_PREFIX = "bwrap: "


def setup_failure(output: str, returncode: int | None) -> str | None:
    """Did bwrap fail to build the sandbox, rather than the command failing?

    Returns the wrapper's own message, or `None` when this looks like an
    ordinary command result.  The caller turns the former into a tool error
    that names the sandbox -- a model told only "exit code 1" will read a
    containment failure as a bug in its own command and retry it, which is
    F05-09's shape (a sandbox denial mistaken for a business error).
    """
    if returncode != 1:
        return None
    first = output.lstrip().split("\n", 1)[0]
    if first.startswith(_BWRAP_ERROR_PREFIX):
        return first[len(_BWRAP_ERROR_PREFIX) :].strip()
    return None


def for_mode(
    mode: SandboxMode,
    root: Path,
    *,
    read_roots: tuple[Path, ...] = (),
    network: bool | None = None,
) -> Sandbox:
    """The sandbox a mode asks for, on this machine.

    Deliberately *not* a fallback chain.  If bubblewrap is missing this still
    returns a `BubblewrapSandbox`, whose `unavailable()` then says so; the
    caller decides whether that is fatal.  Choosing `NoSandbox` here instead
    would be the failure mode F20-02 exists to prevent -- a `read-only`
    session that quietly is not.
    """
    spec = SandboxSpec.for_mode(mode, root, read_roots=read_roots)
    if network is not None:
        spec = SandboxSpec(
            mode=spec.mode, root=spec.root, read_roots=spec.read_roots, network=network
        )
    return BubblewrapSandbox(spec)


__all__ = [
    "BWRAP",
    "BubblewrapSandbox",
    "NoSandbox",
    "Sandbox",
    "SandboxSpec",
    "for_mode",
    "setup_failure",
]
