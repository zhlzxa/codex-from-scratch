"""What the sandbox actually stops -- measured against a kernel, not asserted.

**This probe only means anything on Linux.** Every other probe in this book
runs anywhere; this one asks a kernel to refuse something, and a kernel that
does not implement the refusal cannot be faked. On macOS or Windows it prints
what it would have run and exits without pretending to have measured anything.
That is not politeness -- a security probe that reports success on a machine
where the mechanism does not exist is worse than no probe, because somebody
will read the green output.

The book was written on Windows. These numbers come from WSL2 Ubuntu, kernel
6.6.87.2, bubblewrap 0.9.0:

    wsl -d Ubuntu -- bash -lc "cd /path/to/step20_sandbox && python3 probe_sandbox.py all"

Sections:

    escape   F20-01. Chapter 5's own two payloads (F05-04, F05-05), run first
             with no sandbox and then with one. This is the chapter's demo.
    binds    Why `--ro-bind / /` and not "bind only the workspace".
    exits    F20-05. How bwrap reports its own failures, and why the exit code
             cannot tell them apart from the command's.
    kill     F20-06. Does killing bwrap take the payload's children with it?
    net      F20-04. Network as a switch independent of the filesystem.
    env      Whether the parent's environment reaches the payload.
"""

from __future__ import annotations

import argparse
import os
import platform
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from minicodex.sandbox import BubblewrapSandbox, SandboxSpec, setup_failure

# The payload from F05-04, verbatim from the fault list: "`python -c
# "open('x','w')"` writes in read-only mode". The point of using chapter 5's
# own string rather than a new one is that chapter 5 already measured it
# succeeding, so a failure here is a change in the program, not in the test.
WRITE_PAYLOAD = "python3 -c \"open({path!r},'w').write('escaped')\""

# F05-05: "`curl` exfiltrates". Same reasoning. `-m 5` so a probe run on a
# machine with no egress finishes rather than hanging for the default timeout.
NETWORK_PAYLOAD = "curl -s -m 5 https://example.com -o /dev/null"


def _linux() -> bool:
    return platform.system() == "Linux"


def _require_linux(section: str) -> bool:
    if _linux() and shutil.which("bwrap") is not None:
        return True
    why = "not Linux" if not _linux() else "no bwrap on PATH"
    print(f"  [{section}] skipped: {why}.")
    print("  This section measures a kernel refusing something. There is no")
    print("  offline stand-in for that, so nothing is reported rather than")
    print("  something reassuring.")
    return False


def _run(argv: list[str], **kw: object) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        argv,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=60,
        **kw,  # type: ignore[arg-type]
    )


def _sh(command: str, cwd: str) -> subprocess.CompletedProcess[str]:
    """The unsandboxed path -- what `shell.py` does with `sandbox=None`."""
    return _run(["/bin/sh", "-c", command], cwd=cwd)


def escape(_args: argparse.Namespace) -> None:
    """F20-01: chapter 5's two admissions, before and after."""
    if not _require_linux("escape"):
        return
    with tempfile.TemporaryDirectory() as tmp:
        ws = Path(tmp) / "workspace"
        ws.mkdir()
        outside = Path(tmp) / "outside.txt"
        inside = ws / "inside.txt"

        print("F05-04 -- an interpreter writing in read-only mode")
        print("  chapter 5's note: 'Tokenising is perfect here and tells you nothing'")
        print()

        got = _sh(WRITE_PAYLOAD.format(path=str(outside)), cwd=str(ws))
        print(f"  no sandbox        exit={got.returncode}  file exists: {outside.exists()}")
        outside.unlink(missing_ok=True)

        ro = BubblewrapSandbox(SandboxSpec.for_mode("read-only", ws))
        argv = ro.wrap(WRITE_PAYLOAD.format(path=str(outside)), cwd=str(ws))
        assert argv is not None
        got = _run(argv)
        merged = ((got.stdout or "") + (got.stderr or "")).strip().splitlines()
        print(f"  read-only         exit={got.returncode}  file exists: {outside.exists()}")
        print(f"                    {merged[-1][:72] if merged else ''}")

        ww = BubblewrapSandbox(SandboxSpec.for_mode("workspace-write", ws))
        argv = ww.wrap(WRITE_PAYLOAD.format(path=str(inside)), cwd=str(ws))
        assert argv is not None
        got = _run(argv)
        print(f"  workspace-write   exit={got.returncode}  wrote inside: {inside.exists()}")

        argv = ww.wrap(WRITE_PAYLOAD.format(path=str(outside)), cwd=str(ws))
        assert argv is not None
        got = _run(argv)
        print(f"  workspace-write   exit={got.returncode}  wrote outside: {outside.exists()}")

        print()
        print("F05-05 -- network, 'recognised rather than blocked'")
        got = _run(ro.wrap(NETWORK_PAYLOAD, cwd=str(ws)) or [])
        print(f"  read-only         exit={got.returncode}  (curl 6 = could not resolve host)")
        opened = SandboxSpec(mode="read-only", root=ws.resolve(), network=True)
        got = _run(BubblewrapSandbox(opened).wrap(NETWORK_PAYLOAD, cwd=str(ws)) or [])
        print(f"  read-only +net    exit={got.returncode}  (0 = fetched)")


def binds(_args: argparse.Namespace) -> None:
    """Why the bind set starts at `/` and subtracts, rather than starting empty."""
    if not _require_linux("binds"):
        return
    with tempfile.TemporaryDirectory() as tmp:
        ws = Path(tmp)

        def ro(*paths: str) -> list[str]:
            return [tok for p in paths for tok in ("--ro-bind", p, p)]

        candidates = [
            ro(str(ws)),
            ro(str(ws), "/bin"),
            ro(str(ws), "/bin", "/lib", "/lib64"),
            ro("/"),
        ]
        print("  the smallest bind set that still has a shell in it")
        print()
        for binds_ in candidates:
            argv = [
                "bwrap",
                *binds_,
                "--dev",
                "/dev",
                "--proc",
                "/proc",
                "/bin/sh",
                "-c",
                "echo ok",
            ]
            got = _run(argv)
            shown = " ".join(binds_).replace(str(ws), "<ws>")
            note = (got.stdout or got.stderr or "").strip().splitlines()[:1]
            print(f"  exit={got.returncode:<3} {shown[:58]:<58} {note[0][:34] if note else ''}")


def exits(_args: argparse.Namespace) -> None:
    """F20-05: bwrap's failures and the command's failures share an exit code."""
    if not _require_linux("exits"):
        return
    with tempfile.TemporaryDirectory() as tmp:
        ws = Path(tmp)
        base = ["bwrap", "--ro-bind", "/", "/", "--dev", "/dev", "--proc", "/proc"]
        cases: list[tuple[str, list[str]]] = [
            ("chdir outside the bind set", [*base, "--chdir", "/nope/nothing", "/bin/true"]),
            (
                "bind a source that is absent",
                [*base, "--bind", "/definitely/not/here", "/definitely/not/here", "/bin/true"],
            ),
            (
                "interpreter unreachable",
                ["bwrap", "--ro-bind", str(ws), str(ws), "--dev", "/dev", "/bin/sh", "-c", "echo"],
            ),
            ("an unknown flag", [*base[:3], "--not-a-real-flag", "/bin/true"]),
            ("the command exits 42", [*base, "/bin/sh", "-c", "exit 42"]),
            ("the command succeeds", [*base, "/bin/sh", "-c", "exit 0"]),
        ]
        print("  every setup failure below exits 1 -- so does a command that returns 1")
        print()
        for label, argv in cases:
            got = _run(argv)
            merged = (got.stdout or "") + (got.stderr or "")
            detected = setup_failure(merged, got.returncode)
            mark = "wrapper" if detected else "command"
            print(f"  exit={got.returncode:<3} {label:<30} detected as: {mark}")
        print()
        print("  `setup_failure` reads the 'bwrap: ' prefix because the exit code cannot.")


def kill(_args: argparse.Namespace) -> None:
    """F20-06: chapter 2's timeout depends on killing the whole tree."""
    if not _require_linux("kill"):
        return
    import signal
    import time

    with tempfile.TemporaryDirectory() as tmp:
        ws = Path(tmp)
        sandbox = BubblewrapSandbox(SandboxSpec.for_mode("workspace-write", ws))
        argv = sandbox.wrap("sleep 300 & sleep 300", cwd=str(ws))
        assert argv is not None
        proc = subprocess.Popen(argv, start_new_session=True)
        time.sleep(1.0)
        before = _run(["pgrep", "-c", "-f", "sleep 300"]).stdout.strip() or "0"
        os.killpg(proc.pid, signal.SIGKILL)
        time.sleep(1.0)
        after = _run(["pgrep", "-c", "-f", "sleep 300"]).stdout.strip() or "0"
        proc.wait(timeout=5)
        print(f"  'sleep 300' processes before killpg: {before}")
        print(f"  'sleep 300' processes after  killpg: {after}")
        print()
        print("  --unshare-pid makes bwrap pid 1 inside the namespace, so killing it")
        print("  takes the namespace and everything in it. Without it, chapter 2's")
        print("  F02-01 timeout would leave the payload's children running.")


def net(_args: argparse.Namespace) -> None:
    """F20-04: the network switch is independent of the filesystem one."""
    if not _require_linux("net"):
        return
    with tempfile.TemporaryDirectory() as tmp:
        ws = Path(tmp)
        print("  four combinations, because all four are real configurations")
        print()
        for mode in ("read-only", "workspace-write"):
            for network in (False, True):
                spec = SandboxSpec(mode=mode, root=ws.resolve(), network=network)
                argv = BubblewrapSandbox(spec).wrap(NETWORK_PAYLOAD, cwd=str(ws))
                assert argv is not None
                got = _run(argv)
                reach = "reached" if got.returncode == 0 else f"blocked (curl {got.returncode})"
                print(f"  {mode:<16} network={network!s:<6} {reach}")


def env(_args: argparse.Namespace) -> None:
    """Does the agent's own environment reach the payload?"""
    if not _require_linux("env"):
        return
    with tempfile.TemporaryDirectory() as tmp:
        ws = Path(tmp)
        sandbox = BubblewrapSandbox(SandboxSpec.for_mode("read-only", ws))
        argv = sandbox.wrap('echo "FAKE_API_KEY=${FAKE_API_KEY:-<unset>}"', cwd=str(ws))
        assert argv is not None

        leaky = {**os.environ, "FAKE_API_KEY": "sk-should-not-appear"}
        got = _run(argv, env=leaky)
        print(f"  inherited environment: {(got.stdout or '').strip()}")

        filtered = {k: v for k, v in leaky.items() if k in ("PATH", "HOME", "LANG")}
        got = _run(argv, env=filtered)
        print(f"  shell.ENV_ALLOWLIST:   {(got.stdout or '').strip()}")
        print()
        print("  bwrap passes the environment through; the allowlist in shell.py is")
        print("  what keeps OPENAI_API_KEY out, and it predates this chapter. The")
        print("  sandbox does not replace it -- they bound different things.")


SECTIONS = {
    "escape": escape,
    "binds": binds,
    "exits": exits,
    "kill": kill,
    "net": net,
    "env": env,
}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("section", choices=[*SECTIONS, "all"])
    args = parser.parse_args()

    if not _linux():
        print(f"platform: {platform.system()} -- this probe measures Linux kernel behaviour.")
        print("Run it under WSL or a Linux host:")
        print('  wsl -d Ubuntu -- bash -lc "cd $(pwd) && python3 probe_sandbox.py all"')
        print()

    chosen = SECTIONS if args.section == "all" else {args.section: SECTIONS[args.section]}
    for name, fn in chosen.items():
        print(f"--- {name} " + "-" * (68 - len(name)))
        fn(args)
        print()


if __name__ == "__main__":
    main()
