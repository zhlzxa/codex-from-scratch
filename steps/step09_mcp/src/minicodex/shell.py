"""Run shell commands and hand the output back."""

from __future__ import annotations

import asyncio
import os
import signal
import time

from minicodex.tool_errors import tool_error

DEFAULT_TIMEOUT = 30.0
MAX_OUTPUT_CHARS = 20_000
HEAD_CHARS = MAX_OUTPUT_CHARS // 2
TAIL_CHARS = MAX_OUTPUT_CHARS // 2
READ_CEILING_CHARS = MAX_OUTPUT_CHARS * 50

# Everything a command is allowed to see.  `subprocess`/`asyncio.subprocess`
# inherit the whole of `os.environ` by default, which as measured includes
# anything the agent process itself was started with -- `OPENAI_API_KEY`
# among them, since chapter 1 reads it into that same environment.  A
# command the model asked to run has no business seeing it.
ENV_ALLOWLIST = ("PATH", "HOME", "LANG", "LC_ALL", "TERM", "TZ")


def _clip(text: str) -> str:
    """Keep the first half and the last half, drop the middle.

    A pytest run that fails puts the traceback near the top and the summary
    line at the bottom; everything in between is PASSED lines nobody reads.
    Keeping only the tail throws away exactly the line that explains the
    failure.  Slicing a `str` by index is safe here -- Python strings are
    sequences of characters, not bytes, so there is no multi-byte character
    to cut in half.
    """
    if len(text) <= MAX_OUTPUT_CHARS:
        return text
    omitted = len(text) - HEAD_CHARS - TAIL_CHARS
    return text[:HEAD_CHARS] + f"\n... ({omitted} characters omitted) ...\n" + text[-TAIL_CHARS:]


def _kill_group(proc: asyncio.subprocess.Process) -> None:
    """Kill the command and everything it started.

    `shell=True` means the command is the shell's child, so killing the shell
    alone leaves a grandchild running with no parent watching it -- measured in
    chapter 2 with a `sleep 3600` that outlived its own timeout.

    `killpg` is POSIX-only.  On Windows this does nothing, which is F02-10 and
    is still not fixed; saying so in one place beats an `AttributeError` raised
    from three.
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
    next call starts, as measured directly: calling `cd /tmp` and then `pwd`
    in a second call returns the original directory, not `/tmp`.

    So `cd` is intercepted and handled in Python instead of being handed to
    the shell: `self.cwd` is updated here, and every subsequent call passes
    it as `cwd=`.  This covers the common case (`cd` as its own command) but
    not `cd foo && pytest` -- that `cd` still only affects the subshell for
    that one call.  Chapter 2 stops there; a real persistent shell (one
    actual long-lived process, commands piped to its stdin) is a much bigger
    piece of machinery for a case this class does not claim to solve.
    """

    def __init__(self, *, timeout: float = DEFAULT_TIMEOUT) -> None:
        self.cwd = os.getcwd()
        self.env = {k: v for k, v in os.environ.items() if k in ENV_ALLOWLIST}
        # An instance attribute, not the module constant directly, so tests
        # can ask for a one-second timeout without a monkeypatch reaching
        # into another test's session.
        self.timeout = timeout

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
        cd_result = self._handle_cd(command)
        if cd_result is not None:
            return cd_result

        if command.rstrip().endswith("&"):
            # Measured directly: a trailing `&` returns in milliseconds while
            # the backgrounded process keeps running, outside every mechanism
            # this class has for tracking or killing it -- it is not in
            # `chunks`, its exit code is not `proc.returncode`, and nothing
            # here will ever call `killpg` on it.  Actually supporting this
            # means a registry of background jobs and a way to poll them,
            # which is a real feature, not a two-line fix; refusing it here
            # is honest about the gap rather than silently leaking processes.
            return tool_error(
                "run_shell does not support backgrounded commands (trailing '&')",
                you_sent=command,
                do_this=(
                    "Run it in the foreground, or split the work into steps short "
                    "enough to finish within the timeout."
                ),
            )

        # `asyncio.create_subprocess_shell`, not `subprocess.Popen`: the
        # latter is a blocking call, and `ruff`'s ASYNC220 rejects making one
        # inside `async def` for the same reason chapter 1's `read_file`
        # runs `Path.read_text()` in a thread -- a blocking call in an async
        # function stalls the entire event loop, not just this call.
        proc = await asyncio.create_subprocess_shell(
            command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            stdin=asyncio.subprocess.DEVNULL,
            start_new_session=True,
            cwd=self.cwd,
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
                    # finding one -- measured directly with a single `print`
                    # of 40,000 multi-byte characters and no trailing
                    # newline, the exact shape of `print(..., end="")`.
                    # `read(n)` has no such limit; it just returns whatever
                    # bytes are available, up to `n`.
                    #
                    # `wait_for` around it is what makes this cancellable --
                    # unlike a bare `for line in pipe` on a blocking
                    # `Popen.stdout`, which cannot be interrupted while
                    # waiting for a command that produces nothing at all.
                    # Measured directly: `sleep 30` under the blocking
                    # version ran for the full 30 seconds regardless of a
                    # 1-second timeout, because the loop body that checks the
                    # clock never got control back.
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
                # parent watching it, as measured directly with `sleep 3600`
                # outliving a timeout.
                _kill_group(proc)

                # ...and then close the pipe we have stopped reading, before
                # waiting.  `proc.wait()` does not resolve when the process
                # dies; it resolves when the process has died AND every pipe
                # has reached EOF.  From CPython's base_subprocess.py:
                #
                #     def _try_finish(self):
                #         if self._returncode is None:
                #             return
                #         if all(p is not None and p.disconnected
                #                for p in self._pipes.values()):
                #             self._call(self._call_connection_lost, None)
                #
                # and `_call_connection_lost` is the only place the futures
                # behind `wait()` are ever resolved.  Giving up on the ceiling
                # leaves unread bytes in the pipe, so stdout never reaches EOF,
                # so `wait()` blocks forever on a process that is already dead.
                #
                # Measured, 12 samples per interpreter, `yes | head -c 2000000`
                # against a 1,000,000-character ceiling:
                #
                #     python 3.10.20   0/12 hang
                #     python 3.11.15  12/12 hang
                #     python 3.12.3    4/12 hang
                #     python 3.13.13   4/12 hang
                #
                # Chapters 2 to 4 were written on 3.10, which is why this was
                # invisible for three chapters.  A read-timeout ceiling on
                # `wait()` was tried and rejected: it still "hung" 3-12 times
                # out of 12, it just capped the damage, and it paid the full
                # timeout every time it fired.  Closing the pipe fixes the
                # cause -- 0/12 on every interpreter, and `wait()` returns in
                # 0.00s.
                pipe = proc._transport.get_pipe_transport(1)  # type: ignore[attr-defined]
                if pipe is not None:
                    pipe.close()
            await proc.wait()
        except asyncio.CancelledError:
            # A Ctrl-C while a command is running unwinds through here, and
            # without this clause the command keeps running: the agent process
            # goes away, the subprocess does not, and nothing holds a reference
            # to it any more.  That is F02-08 again, arriving through a door
            # that did not exist when F02-08 was fixed -- the timeout path was
            # the only way out of this function when that kill was written.
            _kill_group(proc)
            raise
        finally:
            # `asyncio.subprocess.Process` has no public close(); without
            # this, its transport is closed by `__del__` whenever garbage
            # collection gets to it, which can land after the event loop is
            # already closed and print a spurious "Event loop is closed"
            # traceback -- observed intermittently (roughly one run in
            # three) with this code before the explicit close was added.
            proc._transport.close()  # type: ignore[attr-defined]

        out = _clip(b"".join(chunks).decode("utf-8", errors="replace"))
        if give_up == "timeout":
            # Saying "commands are killed after N seconds" in the tool
            # description was measured in chapter 3 and changed nothing: both
            # providers sent a two-minute command anyway, 6 runs out of 6.
            # This message is the one the model actually acts on, so it has to
            # carry the instruction, not just the fact.
            out += (
                f"\n... (killed: still running after {self.timeout:.0f}s. "
                "Run a smaller piece of the work -- a single test file or a "
                "single directory -- rather than the whole suite.)"
            )
        elif give_up == "ceiling":
            out += (
                f"\n... (killed: produced more than {READ_CEILING_CHARS} characters. "
                "Narrow the output -- add a filter, a head/tail, or a more "
                "specific path -- and run it again.)"
            )
        elif proc.returncode != 0:
            out += f"\n... (exit code {proc.returncode})"
        return out


# The tool-callable wrapper used to live here, taking a `ShellSession` and an
# argument dict.  Chapter 5 moved it to `tools.py`, and the move is the point:
# that function reached `ShellSession.run()` without passing an approval gate,
# and leaving it in place would have left a second, shorter route to a
# subprocess for a future caller to find.  Deleting it is what makes
# "everything goes through the gate" a fact about the code rather than a
# convention.  `tests/test_boundaries.py` asserts it stays deleted.
