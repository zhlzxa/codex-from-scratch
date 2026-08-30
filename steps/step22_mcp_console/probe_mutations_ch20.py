"""Do chapter 20's tests fail when chapter 20's code is wrong?

Same script as chapters 9 through 19: break one line, run the suite, see
whether anything turns red.

This chapter's mutations are worth more than most, and chapter 19 is the
reason.  Its README claimed **32 mutations, 0 survivors** while three stood --
and the three were its own two headline mechanisms, each with a test that
passed happily with the mechanism deleted.  A security boundary has exactly
that shape: the code looks right, the test is green, and nothing is enforced.

So the targets below are chosen to attack the *claims*, not the syntax.  Every
one of them turns a real boundary off:

    --unshare-net removed          -> the network switch does nothing
    --bind removed                 -> workspace-write cannot write
    --ro-bind / / -> --bind / /    -> everything is writable, silently
    --unshare-pid removed          -> the timeout leaves orphans
    the confined table widened     -> unprompted capability

    uv run python probe_mutations_ch20.py

Note that most of these are caught by argv assertions, which run on any
platform.  That is the point of testing the command line rather than the
kernel: the mutation that removes `--unshare-net` is caught on Windows, where
no sandbox could ever run.
"""

from __future__ import annotations

import atexit
import re
import signal
import subprocess
import sys
from pathlib import Path

MUTATIONS = [
    # ---- the boundary itself -------------------------------------------------
    (
        "src/minicodex/sandbox.py",
        "the network switch stops doing anything",
        '        if not self.spec.network:\n            argv.append("--unshare-net")',
        '        if False:\n            argv.append("--unshare-net")',
    ),
    (
        "src/minicodex/sandbox.py",
        "everything is bound writable, so read-only is a name and nothing else",
        'argv += ["--ro-bind", _ROOT, _ROOT]',
        'argv += ["--bind", _ROOT, _ROOT]',
    ),
    (
        "src/minicodex/sandbox.py",
        "workspace-write loses its writable hole",
        "        for path in self.spec.writable():\n"
        '            argv += ["--bind", str(path), str(path)]',
        '        for path in ():\n            argv += ["--bind", str(path), str(path)]',
    ),
    (
        "src/minicodex/sandbox.py",
        "the pid namespace goes away, so a timeout leaves the payload's children",
        'argv += ["--unshare-pid", "--die-with-parent"]',
        'argv += ["--die-with-parent"]',
    ),
    (
        "src/minicodex/sandbox.py",
        "read-only gains a writable workspace",
        '        if self.mode == "workspace-write":\n            return (self.root,)',
        '        if self.mode in ("workspace-write", "read-only"):\n'
        "            return (self.root,)",
    ),
    (
        "src/minicodex/sandbox.py",
        "full-access is wrapped after all, so ps lies about what is enforced",
        '        if self.spec.mode == "full-access" and self.spec.network:\n'
        "            return None",
        "        if False:\n            return None",
    ),
    (
        "src/minicodex/sandbox.py",
        "network follows the mode everywhere, collapsing four combinations to two",
        '            network=(mode == "full-access"),',
        "            network=False,",
    ),
    # ---- the read roots (F20-07) --------------------------------------------
    (
        "src/minicodex/sandbox.py",
        "extra read roots are dropped, so the shell cannot see chapter 16's memory",
        "        for path in self.spec.read_roots:\n"
        '            argv += ["--ro-bind-try", str(path), str(path)]',
        '        for path in ():\n            argv += ["--ro-bind-try", str(path), str(path)]',
    ),
    (
        "src/minicodex/sandbox.py",
        "a read root uses --ro-bind, so an absent one stops the whole session",
        '            argv += ["--ro-bind-try", str(path), str(path)]',
        '            argv += ["--ro-bind", str(path), str(path)]',
    ),
    (
        "src/minicodex/sandbox.py",
        "read roots are bound before the writable holes, so a hole re-opens them",
        "        for path in self.spec.writable():\n"
        '            argv += ["--bind", str(path), str(path)]',
        "        for path in self.spec.read_roots:\n"
        '            argv += ["--ro-bind-try", str(path), str(path)]\n'
        "        for path in self.spec.writable():\n"
        '            argv += ["--bind", str(path), str(path)]',
    ),
    # ---- telling the wrapper's failure from the command's (F20-05) ----------
    (
        "src/minicodex/sandbox.py",
        "a bwrap setup failure is reported to the model as ordinary command output",
        "    if first.startswith(_BWRAP_ERROR_PREFIX):",
        "    if False:",
    ),
    (
        "src/minicodex/sandbox.py",
        "every exit code is examined, so a command exiting 42 can be called a wrapper failure",
        "    if returncode != 1:\n        return None",
        "    if returncode == 0:\n        return None",
    ),
    (
        "src/minicodex/sandbox.py",
        "an unreachable cwd is not detected before running",
        "        return resolved.is_dir()",
        "        return True",
    ),
    # ---- fail closed (F20-02) ------------------------------------------------
    (
        "src/minicodex/sandbox.py",
        "a missing bwrap is reported as fine",
        "        if found is None:",
        "        if False:",
    ),
    (
        "src/minicodex/sandbox.py",
        "for_mode falls back to no sandbox when bubblewrap is missing",
        "    spec = SandboxSpec.for_mode(mode, root, read_roots=read_roots)",
        "    if shutil.which(BWRAP) is None:\n"
        "        return NoSandbox()\n"
        "    spec = SandboxSpec.for_mode(mode, root, read_roots=read_roots)",
    ),
    # ---- the shell seam ------------------------------------------------------
    (
        "src/minicodex/shell.py",
        "the sandbox is ignored, so a confined session is not one",
        "        argv = self.sandbox.wrap(command, cwd=self.cwd)"
        " if self.sandbox is not None else None",
        "        argv = None",
    ),
    (
        "src/minicodex/shell.py",
        "the wrapper's own failure reaches the model as a command result",
        "            failure = setup_failure(out, proc.returncode)"
        " if self.sandbox is not None else None",
        "            failure = None",
    ),
    (
        "src/minicodex/sandbox.py",
        "the shell is dropped from the argv, so pipes and && stop meaning anything",
        '        argv += ["/bin/sh", "-c", command]',
        "        argv += [command]",
    ),
    # ---- the judgement layer's new permission (the payoff) -------------------
    (
        "src/minicodex/policy.py",
        "confinement is ignored, so the sandbox buys no fewer prompts",
        "    allowed = _SHELL_ALLOWED_BY_MODE[mode]\n"
        "    if confined:\n"
        "        allowed = _CONFINED_ALLOWED_BY_MODE[mode]",
        "    allowed = _SHELL_ALLOWED_BY_MODE[mode]",
    ),
    (
        "src/minicodex/policy.py",
        "confinement is on by default, quietly silencing prompts nobody disabled",
        "    confined: bool = False,",
        "    confined: bool = True,",
    ),
    (
        "src/minicodex/policy.py",
        "a confined session may reach the network without being asked",
        '    "workspace-write": frozenset({Risk.READ, Risk.WRITE, Risk.INTERPRETER}),',
        '    "workspace-write": frozenset('
        "{Risk.READ, Risk.WRITE, Risk.INTERPRETER, Risk.NETWORK}),",
    ),
    # ---- ownership (F20-08 / F20-09) ----------------------------------------
    (
        "src/minicodex/tenancy.py",
        "every owner shares one directory again -- the leak this chapter closes",
        "        if self.key is None:\n            return self.home\n"
        "        return self.home / TENANTS_DIR / self.key",
        "        return self.home",
    ),
    (
        "src/minicodex/tenancy.py",
        "an owner key is not checked, so it can climb out of its directory",
        "        if not _SAFE_KEY.match(self.key):",
        "        if False:",
    ),
    (
        "src/minicodex/tenancy.py",
        "the key pattern is widened to allow upper case, so two spellings "
        "become one directory on a case-insensitive filesystem",
        '_SAFE_KEY = re.compile(r"\\A[a-z0-9][a-z0-9_-]{0,63}\\Z")',
        '_SAFE_KEY = re.compile(r"\\A[A-Za-z0-9][A-Za-z0-9_-]{0,63}\\Z")',
    ),
    (
        "src/minicodex/tenancy.py",
        "the merge lock is global again, so one owner's merge blocks another's",
        '        return self.root() / "memory_merge.lock"',
        '        return self.home / "memory_merge.lock"',
    ),
    (
        "src/minicodex/tenancy.py",
        "the jobs table is shared, so one owner marks another's session extracted",
        '        return self.root() / "memory_jobs.sqlite3"',
        '        return self.home / "memory_jobs.sqlite3"',
    ),
]

EQUIVALENT: set[str] = set()

ROOT = Path(".")
ORIGINALS = {name: (ROOT / name).read_text(encoding="utf-8") for name, *_ in MUTATIONS}
SUITES = [
    "tests/test_faults_ch20.py",
    "tests/test_approval.py",
    "tests/test_shell.py",
]


def restore() -> None:
    for name, text in ORIGINALS.items():
        path = ROOT / name
        if path.read_text(encoding="utf-8") != text:
            path.write_text(text, encoding="utf-8")


atexit.register(restore)
signal.signal(signal.SIGINT, lambda *_: sys.exit(130))


def main() -> None:
    print(f"{len(MUTATIONS)} mutations, {' '.join(SUITES)}\n")
    survivors = []
    for name, label, before, after in MUTATIONS:
        path = ROOT / name
        source = ORIGINALS[name]
        if before not in source:
            print(f"  !! could not apply: {label}")
            survivors.append(label)
            continue
        path.write_text(source.replace(before, after, 1), encoding="utf-8")
        try:
            result = subprocess.run(
                [sys.executable, "-m", "pytest", *SUITES, "-q", "--no-header", "-p", "no:warnings"],
                timeout=900,
                capture_output=True,
                text=True,
                # See chapter 19's probe: `text=True` alone decodes with the
                # locale's codec, which on a zh-CN Windows box is GBK, and a
                # suite whose output contains CJK then dies inside subprocess's
                # reader thread -- leaving `result.stdout` as `None`.
                encoding="utf-8",
                errors="replace",
            )
        except subprocess.TimeoutExpired:
            print(f"  !! the suite did not finish: {label}")
            survivors.append(label)
            continue
        finally:
            restore()
        failed = len(re.findall(r"^FAILED ", result.stdout, re.M))
        errors = len(re.findall(r"^ERROR ", result.stdout, re.M))
        caught = failed + errors
        mark = "   (equivalent mutant, expected)" if label in EQUIVALENT else ""
        print(f"  {caught:>3} test(s) fail  <-  {label}{mark}")
        if not caught and label not in EQUIVALENT:
            survivors.append(label)

    print()
    if survivors:
        print(f"{len(survivors)} mutation(s) nothing noticed:")
        for label in survivors:
            print(f"  - {label}")
        raise SystemExit(1)
    print("every mutation was caught.")


if __name__ == "__main__":
    main()
