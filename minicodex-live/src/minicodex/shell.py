"""Run shell commands and hand the output back."""

from __future__ import annotations

import asyncio
import os
import signal
import time

from minicodex.clip import clip
from minicodex.sandbox import Sandbox, setup_failure
from minicodex.tool_errors import tool_error

DEFAULT_TIMEOUT = 30.0
MAX_OUTPUT_CHARS = 20_000
READ_CEILING_CHARS = MAX_OUTPUT_CHARS * 50

# Everything a command is allowed to see.  `subprocess`/`asyncio.subprocess`
# inherit the whole of `os.environ` by default, including anything the agent
# process itself was started with -- `OPENAI_API_KEY` among them.  A command
# the model asked to run has no business seeing it.
#
# `SYSTEMROOT` is on the list, and that is the price of an allowlist rather
# than a bug in one: without it, Winsock cannot initialise, so `import
# asyncio` inside any subprocess on Windows dies with
#
#     OSError: [WinError 10106] ... service provider ...
#
# which means `python -m pytest` -- the way the agent checks its own work --
# fails for a reason that has nothing to do with the work.  An allowlist that
# is too narrow does not fail closed here; it fails as somebody else's bug.
# Listed unconditionally rather than under a platform branch: the variable
# does not exist on POSIX, so the filter drops it anyway, and one list is
# easier to reason about than two.
ENV_ALLOWLIST = ("PATH", "HOME", "LANG", "LC_ALL", "TERM", "TZ", "SYSTEMROOT")


def _clip(text: str) -> str:
    """Keep the first half and the last half, drop the middle.

    A pytest run that fails puts the traceback near the top and the summary
    line at the bottom; everything in between is PASSED lines nobody reads.
    Keeping only the tail throws away exactly the line that explains the
    failure.

    The body lives in `clip.py`; this wrapper stays because `MAX_OUTPUT_CHARS`
    is this module's own number -- the shell's ceiling is not the sub-agent's
    ceiling.
    """
    return clip(text, MAX_OUTPUT_CHARS)


def _kill_group(proc: asyncio.subprocess.Process) -> None:
    """Kill the command and everything it started.

    `shell=True` means the command is the shell's child, so killing the shell
    alone leaves a grandchild running with no parent watching it.

    `killpg` is POSIX-only.  On Windows this does nothing, which is a known,
    documented gap (see docs/history.md); saying so in one place beats an
    `AttributeError` raised from three.
    """
    if not hasattr(os, "killpg"):  # pragma: no cover - platform branch
        return
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):  # pragma: no cover
        pass


class ShellSession:
    """One agent's view of a shell: a working directory that persists
    between calls.

    Each `run()` starts a brand new subprocess -- there is no long-lived
    shell process underneath.  `cd` inside a command only ever changes the
    directory of that command's own subshell, which is gone by the time the
    next call starts.

    So `cd` is intercepted and handled in Python instead of being handed to
    the shell: `self.cwd` is updated here, and every subsequent call passes
    it as `cwd=`.  This covers the common case (`cd` as its own command) but
    not `cd foo && pytest` -- that `cd` still only affects the subshell for
    that one call.  A real persistent shell (one long-lived process, commands
    piped to its stdin) is a much bigger piece of machinery for a case this
    class does not claim to solve.
    """

    def __init__(
        self,
        *,
        cwd: str | os.PathLike[str] | None = None,
        timeout: float = DEFAULT_TIMEOUT,
        sandbox: Sandbox | None = None,
    ) -> None:
        # Where this conversation is standing.  Deliberately separate from
        # `tool_context`'s `root`: callers point them at different directories
        # (an eval's workspace is a temp dir while the process runs in the
        # repository), and the tools' containment comes from `root`, never
        # from the cwd.
        self.cwd = os.fspath(cwd) if cwd is not None else os.getcwd()
        self.env = {k: v for k, v in os.environ.items() if k in ENV_ALLOWLIST}
        # An instance attribute, not the module constant directly, so tests
        # can ask for a one-second timeout without a monkeypatch reaching
        # into another test's session.
        self.timeout = timeout
        # `None`, not `NoSandbox()`, and the difference is the point.  `None`
        # means "this caller never asked for a boundary"; `NoSandbox()` means
        # somebody decided to run without one.  `None` keeps the
        # `create_subprocess_shell` path below, unchanged, so adding a sandbox
        # changes nothing for anyone who did not ask for one.  A new boundary
        # ships off, and turning it on is an act.
        self.sandbox = sandbox
        # The structured outcome of the most recent `run()`: the subprocess
        # return code, or `None` when no process was started (a `cd` handled
        # here, an argument refusal) or the command was killed on timeout or
        # output ceiling.  Structured rather than parsed back out of the
        # output text, whose last line is an error *message*, not a result
        # record.
        self.last_exit: int | None = None

    def _handle_cd(self, command: str) -> str | None:
        stripped = command.strip()
        if stripped != "cd" and not stripped.startswith("cd "):
            return None
        target = stripped[2:].strip() or self.env.get("HOME", "/")
        new_dir = os.path.normpath(os.path.join(self.cwd, os.path.expanduser(target)))
        if not os.path.isdir(new_dir):
            return tool_error(
                "cd: no such directory",
                you_sent=target,
                do_this="Use ls to see what is here, then cd to a directory that exists.",
            )
        self.cwd = new_dir
        return ""

    async def run(self, command: str) -> str:
        # No subprocess started on these two paths, so `last_exit` stays
        # `None`: there is no exit code to have.
        self.last_exit = None
        cd_result = self._handle_cd(command)
        if cd_result is not None:
            return cd_result

        if command.rstrip().endswith("&"):
            # A trailing `&` returns in milliseconds while the backgrounded
            # process keeps running, outside every mechanism this class has
            # for tracking or killing it -- it is not in `chunks`, its exit
            # code is not `proc.returncode`, and nothing here will ever call
            # `killpg` on it.  Actually supporting this means a registry of
            # background jobs and a way to poll them, which is a real
            # feature, not a two-line fix; refusing it here is honest about
            # the gap rather than silently leaking processes.
            return tool_error(
                "run_shell does not support backgrounded commands (trailing '&')",
                you_sent=command,
                do_this=(
                    "Run it in the foreground, or split the work into steps short "
                    "enough to finish within the timeout."
                ),
            )

        argv = self.sandbox.wrap(command, cwd=self.cwd) if self.sandbox is not None else None

        if argv is None:
            # `asyncio.create_subprocess_shell`, not `subprocess.Popen`: the
            # latter is a blocking call, and a blocking call in an async
            # function stalls the entire event loop, not just this call
            # (ruff ASYNC220 enforces it).
            proc = await asyncio.create_subprocess_shell(
                command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                stdin=asyncio.subprocess.DEVNULL,
                start_new_session=True,
                cwd=self.cwd,
                env=self.env,
            )
        else:
            # `_exec`, not `_shell`, and `cwd=` is gone: the shell that
            # interprets the command now lives *inside* the namespace
            # (`/bin/sh -c` is the tail of `argv`), and the working directory
            # is `--chdir` because it has to be resolved in there too.  Passing
            # `cwd=self.cwd` here as well would resolve it on the host, before
            # the namespace exists, and that is a different directory.
            proc = await asyncio.create_subprocess_exec(
                *argv,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                stdin=asyncio.subprocess.DEVNULL,
                start_new_session=True,
                env=self.env,
            )
        assert proc.stdout is not None

        chunks: list[bytes] = []
        total = 0
        deadline = time.monotonic() + self.timeout
        give_up: str | None = None  # None | "timeout" | "ceiling"

        try:
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    give_up = "timeout"
                    break
                try:
                    # `read(n)`, not `readline()`: `readline()` looks for a
                    # newline before returning and raises `LimitOverrunError`
                    # if it reads past its internal 64KB buffer without
                    # finding one (the exact shape of `print(..., end="")`
                    # with no trailing newline).  `read(n)` has no such
                    # limit; it just returns whatever bytes are available,
                    # up to `n`.
                    #
                    # `wait_for` around it is what makes this cancellable --
                    # a bare blocking read cannot be interrupted while
                    # waiting for a command that produces nothing at all,
                    # and then the timeout never gets checked.
                    chunk = await asyncio.wait_for(proc.stdout.read(4096), timeout=remaining)
                except asyncio.TimeoutError:
                    give_up = "timeout"
                    break
                if not chunk:  # EOF
                    break
                chunks.append(chunk)
                total += len(chunk)
                if total > READ_CEILING_CHARS:
                    give_up = "ceiling"
                    break

            if give_up is not None:
                # Kill the whole process group, not just this subprocess:
                # `shell=True` runs the command as the shell's child, so
                # killing only the shell leaves that child running with no
                # parent watching it.
                _kill_group(proc)

                # ...and then close the pipe we have stopped reading, before
                # waiting.  `proc.wait()` does not resolve when the process
                # dies; it resolves when the process has died AND every pipe
                # has reached EOF.  Giving up on the timeout or the ceiling
                # leaves unread bytes in the pipe, so stdout never reaches
                # EOF, so `wait()` blocks forever on a process that is
                # already dead.  Closing the pipe fixes the cause.
                pipe = proc._transport.get_pipe_transport(1)  # type: ignore[attr-defined]
                if pipe is not None:
                    pipe.close()
            await proc.wait()
        except asyncio.CancelledError:
            # A Ctrl-C while a command is running unwinds through here, and
            # without this clause the command keeps running: the agent process
            # goes away, the subprocess does not, and nothing holds a reference
            # to it any more.
            _kill_group(proc)
            raise
        finally:
            # `asyncio.subprocess.Process` has no public close(); without
            # this, its transport is closed by `__del__` whenever garbage
            # collection gets to it, which can land after the event loop is
            # already closed and print a spurious "Event loop is closed"
            # traceback.
            proc._transport.close()  # type: ignore[attr-defined]

        out = _clip(b"".join(chunks).decode("utf-8", errors="replace"))
        if give_up == "timeout":
            # Killed, not exited: no exit code exists, and inventing one would
            # put a false fact in the audit trail.
            self.last_exit = None
            # This message carries the instruction, not just the fact -- the
            # schema's "killed after N seconds" sentence does not redirect the
            # model; the error it gets after the kill does.
            out += (
                f"\n... (killed: still running after {self.timeout:.0f}s. "
                "Run a smaller piece of the work -- a single test file or a "
                "single directory -- rather than the whole suite.)"
            )
        elif give_up == "ceiling":
            self.last_exit = None
            out += (
                f"\n... (killed: produced more than {READ_CEILING_CHARS} characters. "
                "Narrow the output -- add a filter, a head/tail, or a more "
                "specific path -- and run it again.)"
            )
        else:
            self.last_exit = proc.returncode
            if proc.returncode != 0:
                # Before reporting an exit code, ask whether the *wrapper* failed
                # rather than the command.  bwrap exits 1 for "I could not build
                # the sandbox" and a command exits 1 for "I ran and said no", and
                # the exit code cannot tell them apart -- see
                # `sandbox.setup_failure`.  Handing the raw text to the model
                # reads a containment failure as a business error, and gets
                # retried.
                failure = setup_failure(out, proc.returncode) if self.sandbox is not None else None
                if failure is not None:
                    return tool_error(
                        f"the sandbox could not be built, so the command did not run: {failure}",
                        you_sent=command,
                        do_this=(
                            "This is a configuration problem, not a problem with the "
                            "command -- running it again will fail the same way. Report "
                            "it rather than retrying."
                        ),
                    )
                out += f"\n... (exit code {proc.returncode})"
        return out


# There is deliberately no tool-callable wrapper here: the only route to
# `ShellSession.run()` from a tool is `tools.run_shell`, which passes the
# approval gate.  A second, shorter route to a subprocess is the thing the
# gate exists to prevent; `tests/test_boundaries.py` asserts this stays so.
