"""What `run_shell` must survive, pinned so it stays survived.

Fault IDs match FAULTS.md.  Every number here was measured directly against
a real subprocess before it was fixed -- see chapter 2 for the traces.
"""

from __future__ import annotations

import asyncio
import os
import sys
import time

import pytest

from minicodex.shell import MAX_OUTPUT_CHARS, ShellSession, _clip

STUBBORN = os.path.join(os.path.dirname(__file__), "fixtures", "stubborn.py")

# F02-10, and the first time this project has run on Windows.  `run_shell`
# kills process groups with `os.killpg` and `start_new_session`, both of which
# are POSIX-only, and its tests drive a POSIX shell (`sleep`, `cat`, `pwd`,
# `yes`).  On Windows the killpg call raises AttributeError *after the command
# has already run*.
#
# Marked rather than fixed.  A Windows port means job objects instead of
# process groups, which is a real piece of work, not a two-line fallback --
# and a partial fallback (`proc.kill()`) would be worse than the gap, because
# it kills the shell and silently leaves the grandchildren running, which is
# the exact orphan bug F02-08 exists to prevent.
#
# The point of the marker is that the gap now says its own name in the test
# output instead of appearing as seven mysterious red tests.
posix_only = pytest.mark.skipif(
    sys.platform == "win32",
    reason="F02-10: POSIX process groups and POSIX shell builtins; see FAULTS.md",
)


# ---------------------------------------------------------------------------
# F02-01  a command that never returns
# ---------------------------------------------------------------------------


@posix_only
async def test_F02_01_a_hanging_command_is_killed_on_timeout() -> None:
    session = ShellSession(timeout=1)
    start = time.monotonic()
    out = await session.run("sleep 30")
    elapsed = time.monotonic() - start

    assert elapsed < 5, f"took {elapsed}s -- the timeout did not fire"
    # Asserts that the kill was reported, not the exact sentence. Chapter 3
    # rewrote every error message and this was the only test that broke --
    # it was pinning wording that test_schemas.py now checks properly.
    assert "killed" in out


# ---------------------------------------------------------------------------
# F02-02  100MB of output does not blow up memory
# ---------------------------------------------------------------------------


async def _bounded(session: ShellSession, command: str, *, limit: float = 20) -> str:
    """Run a command, but fail rather than hang if it never comes back.

    An assertion on the wall clock placed *after* the call is worth nothing
    against a call that can hang: the assert never runs, and a hanging test
    reports nothing at all until someone gets bored and presses Ctrl-C.  The
    bound has to be on the await itself.  This helper exists because the
    ceiling bug below hid behind exactly that mistake for three chapters.
    """
    return await asyncio.wait_for(session.run(command), timeout=limit)


@posix_only
async def test_F02_02_a_firehose_is_capped_not_buffered_whole() -> None:
    session = ShellSession(timeout=10)
    out = await _bounded(session, "yes | head -c 100000000")
    # _clip() guarantees the upper bound regardless of how much the process
    # produced before the read ceiling stopped it.
    assert len(out) <= MAX_OUTPUT_CHARS + 200


@posix_only
async def test_ceiling_does_not_leave_wait_blocked_on_a_dead_process() -> None:
    """Hitting the read ceiling must return, not block forever.

    `proc.wait()` resolves when the process has exited AND every pipe has
    reached EOF.  Giving up on the ceiling leaves unread bytes in stdout, so
    without closing that pipe the wait never resolves -- on a process SIGKILL
    has already killed.

    Measured before the fix, 12 samples per interpreter: 0/12 hang on python
    3.10, 12/12 on 3.11, 4/12 on 3.12 and 3.13.  Chapters 2 to 4 were written
    on 3.10, which is why three chapters shipped with this in them.

    2,000,000 characters rather than the 100,000,000 above: just past the
    1,000,000-character ceiling is the smallest input that reaches the bug,
    and a small one keeps the run fast.
    """
    session = ShellSession(timeout=10)
    out = await _bounded(session, "yes | head -c 2000000")
    assert "killed" in out


# ---------------------------------------------------------------------------
# F02-03  head+tail truncation keeps both ends
# ---------------------------------------------------------------------------


def test_F02_03_clip_keeps_head_and_tail_not_just_tail() -> None:
    body = "FAILED first\n" + ("PASSED\n" * 5000) + "1 failed, 5000 passed"
    clipped = _clip(body)

    assert "FAILED first" in clipped, "the failure reason, at the top, must survive"
    assert "1 failed, 5000 passed" in clipped, "the summary, at the bottom, must survive"
    assert len(clipped) < len(body)


def test_F02_03_short_output_is_not_touched() -> None:
    body = "just a few lines\nnothing to clip\n"
    assert _clip(body) == body


# ---------------------------------------------------------------------------
# F02-04  interactive commands wait on stdin forever
# ---------------------------------------------------------------------------


@posix_only
async def test_F02_04_a_command_reading_stdin_gets_eof_immediately() -> None:
    """`python3` with no `-c` is a REPL that reads stdin.  Without
    `stdin=DEVNULL` this hangs until the timeout; measured directly at the
    full 5-second timeout outside of pytest.  With it, the REPL sees EOF and
    exits at once."""
    session = ShellSession(timeout=5)
    start = time.monotonic()
    await session.run(f"{sys.executable}")
    elapsed = time.monotonic() - start
    assert elapsed < 2, f"took {elapsed}s -- looks like it wasn't given DEVNULL stdin"


async def test_F02_04_stdin_is_explicitly_devnull(monkeypatch) -> None:
    """The test above is an integration test, and it has a blind spot: under
    pytest, the parent process's own stdin is often already non-blocking or
    redirected, so a command that inherits it can return quickly even
    without `stdin=DEVNULL` -- measured directly, running `python3` with no
    stdin argument at all took the full 5-second timeout from a plain shell,
    but returned in milliseconds from inside a pytest run. Asserting on the
    actual call to `create_subprocess_shell` does not depend on what stdin
    pytest happens to have."""
    seen_kwargs: dict = {}
    real = asyncio.create_subprocess_shell

    async def spy(*args, **kwargs):
        seen_kwargs.update(kwargs)
        return await real(*args, **kwargs)

    monkeypatch.setattr(asyncio, "create_subprocess_shell", spy)
    session = ShellSession()
    await session.run("echo hi")

    assert seen_kwargs.get("stdin") is asyncio.subprocess.DEVNULL


# ---------------------------------------------------------------------------
# F02-05  cd and export are lost between calls
# ---------------------------------------------------------------------------


@posix_only
async def test_F02_05_cd_persists_to_the_next_call(tmp_path) -> None:
    session = ShellSession()
    await session.run(f"cd {tmp_path}")
    out = await session.run("pwd")
    assert out.strip() == str(tmp_path)


async def test_F02_05_cd_to_a_missing_directory_is_reported_and_does_not_move() -> None:
    session = ShellSession()
    before = session.cwd
    out = await session.run("cd /no/such/directory/at/all")
    assert "no such directory" in out
    assert session.cwd == before


async def test_F02_05_a_second_shell_session_does_not_see_the_first_ones_cd(tmp_path) -> None:
    a = ShellSession()
    b = ShellSession()
    await a.run(f"cd {tmp_path}")
    assert (await b.run("pwd")).strip() != str(tmp_path)


# ---------------------------------------------------------------------------
# F02-06  backgrounded commands are refused, not silently leaked
# ---------------------------------------------------------------------------


async def test_F02_06_a_trailing_ampersand_is_refused() -> None:
    session = ShellSession()
    out = await session.run("sleep 30 &")
    assert "does not support backgrounded" in out


async def test_F02_06_refusing_it_does_not_leave_the_process_running() -> None:
    session = ShellSession()
    await session.run("sleep 5 &")
    await asyncio.sleep(0.5)
    # `pgrep -f` matches against the full command line, which would also
    # match its own invocation if that invocation mentioned the pattern --
    # `[s]leep 5` (bracketing the first letter) is the standard trick to
    # exclude the grep/pgrep process itself from its own match.
    result = await session.run("pgrep -f '[s]leep 5' || echo NONE")
    assert "NONE" in result


# ---------------------------------------------------------------------------
# F02-07  non-UTF-8 bytes must not crash the read
# ---------------------------------------------------------------------------


async def test_F02_07_invalid_utf8_is_replaced_not_raised(tmp_path) -> None:
    bad = tmp_path / "badbytes.bin"
    bad.write_bytes(b"before \xff\xfe after")

    session = ShellSession()
    out = await session.run(f"cat {bad}")  # must not raise UnicodeDecodeError
    assert "before" in out and "after" in out
    assert "�" in out  # the replacement character


# ---------------------------------------------------------------------------
# F02-08  orphaned processes must not outlive the call
# ---------------------------------------------------------------------------


@posix_only
async def test_F02_08_a_timed_out_command_leaves_no_process_behind() -> None:
    """`shell=True` runs the command as a child of `/bin/sh`.  Killing only
    the `Popen`/`Process` object kills the shell, not what it forked --
    measured directly: `sleep 3600` outlived a 1-second timeout and was
    still running afterward.  `os.killpg` on the whole process group is what
    reaches it."""
    session = ShellSession(timeout=1)
    await session.run("sleep 3600")
    await asyncio.sleep(0.5)

    # No live process anywhere on the machine should still be running that
    # command.  The bracket trick keeps this pgrep call from matching its
    # own command line.
    result = await session.run("pgrep -f '[s]leep 3600' || echo NONE")
    assert "NONE" in result


@posix_only
async def test_F02_08_a_stubborn_child_that_ignores_the_pipe_closing_is_still_killed() -> None:
    """A process that catches BrokenPipeError and keeps writing does not
    stop on its own when the read side gives up -- it has to be killed.

    Bounded for the same reason as F02-02: this one reaches the give-up path
    with output still sitting unread in the pipe, so it hangs rather than
    fails if that pipe is not closed before the wait.  The timeout path and
    the ceiling path share the defect and therefore share the bound.
    """
    session = ShellSession(timeout=1)
    out = await _bounded(session, f"{sys.executable} {STUBBORN}")
    assert "killed" in out

    await asyncio.sleep(0.5)
    result = await session.run("pgrep -f '[s]tubborn.py' || echo NONE")
    assert "NONE" in result


# ---------------------------------------------------------------------------
# F02-09  the host's environment (including secrets) is not inherited
# ---------------------------------------------------------------------------


async def test_F02_09_a_variable_outside_the_allowlist_is_not_visible(monkeypatch) -> None:
    monkeypatch.setenv("SOME_SECRET_KEY", "sk-should-not-leak")
    session = ShellSession()
    out = await session.run("echo ${SOME_SECRET_KEY:-NOT_SET}")
    assert "NOT_SET" in out
    assert "sk-should-not-leak" not in out


async def test_F02_09_path_is_still_usable() -> None:
    session = ShellSession()
    out = await session.run("echo $PATH")
    assert out.strip() != ""


# ---------------------------------------------------------------------------
# F02-11  truncation does not split a multi-byte character
# ---------------------------------------------------------------------------


async def test_F02_11_clipping_never_produces_a_decode_error() -> None:
    # More than twice MAX_OUTPUT_CHARS of a 3-byte-per-character string, so
    # the cut point in _clip() is very unlikely to land on a character
    # boundary by luck.
    session = ShellSession(timeout=10)
    script = "print('你好世界' * 10000, end='')"
    out = await session.run(f'{sys.executable} -c "{script}"')

    out.encode("utf-8")  # raises if a surrogate or partial sequence leaked through
    assert len(out) <= MAX_OUTPUT_CHARS + 200


def test_F02_11_clip_slices_by_character_not_by_byte() -> None:
    # _clip operates on `str`, which Python indexes by code point, so a
    # slice boundary can never fall inside a multi-byte character the way a
    # `bytes` slice could.
    text = "你" * (MAX_OUTPUT_CHARS + 100)
    clipped = _clip(text)
    clipped.encode("utf-8")


# ---------------------------------------------------------------------------
# F02-12  a failing command with empty output must say so
# ---------------------------------------------------------------------------


async def test_F02_12_nonzero_exit_with_empty_output_names_the_exit_code() -> None:
    session = ShellSession()
    out = await session.run("exit 1")
    assert "exit code 1" in out


async def test_F02_12_a_successful_silent_command_is_not_confused_with_a_failure() -> None:
    session = ShellSession()
    out = await session.run("true")
    assert "exit code" not in out


# ---------------------------------------------------------------------------
# missing/invalid input
# ---------------------------------------------------------------------------


async def test_run_shell_wrapper_rejects_a_missing_command() -> None:
    from minicodex.shell import run_shell

    session = ShellSession()
    out = await run_shell(session, {})
    assert "needs a" in out
