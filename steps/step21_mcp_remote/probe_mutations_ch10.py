"""Do chapter 10's tests fail when chapter 10's code is wrong?

Same script as chapter 9's, pointed at `subagent.py`, `clip.py` and the two
one-line changes chapters 2 and 7 needed.  Each entry is an edit that should
break something; the script applies it, runs the suite, restores the file, and
reports how many tests noticed.

Restores from `atexit` and a signal handler, not from `finally` alone -- that
is chapter 6's lesson, learned by leaving `if False:` in `tokens.py` after a
Ctrl-C during a pytest run.

    uv run python probe_mutations_ch10.py
"""

from __future__ import annotations

import atexit
import re
import signal
import subprocess
import sys
from pathlib import Path

MUTATIONS = [
    (
        "subagent.py",
        "the child gets a fresh shell instead of the parent's directory",
        "shell.cwd = ctx.parent_shell.cwd",
        "shell.cwd = shell.cwd",
    ),
    (
        "subagent.py",
        "constraints are appended to the task instead of the system note",
        '"These rules come from the user and apply to everything you do:\\n"',
        '"" if True else "These rules come from the user:\\n"',
    ),
    (
        "subagent.py",
        "the result is returned whole, with no ceiling",
        "body = clip(self.text.strip(), MAX_TASK_RESULT_CHARS)",
        "body = self.text.strip()",
    ),
    (
        "subagent.py",
        "the depth limit is not enforced",
        "if ctx.depth >= ctx.max_depth:",
        "if False:",
    ),
    (
        "subagent.py",
        "the bottom level is handed a spawn tool it may not use",
        "if ctx.depth + 1 < ctx.max_depth:",
        "if True:",
    ),
    (
        "subagent.py",
        "the shared turn budget is not enforced",
        "if spent >= ctx.child_turn_budget:",
        "if False:",
    ),
    (
        "subagent.py",
        "a bare wait_for, which chapter 7 turns into a silent empty answer",
        "result = await asyncio.wait_for(asyncio.shield(task), ctx.timeout)",
        "result = await asyncio.wait_for(task, ctx.timeout)",
    ),
    (
        "subagent.py",
        "a cancelled parent abandons its running child",
        "        await _stop(task)\n        raise",
        "        raise",
    ),
    (
        "subagent.py",
        "an empty answer counts as success",
        "    elif not text:",
        "    elif False:",
    ),
    (
        "subagent.py",
        "turn_limit is reported as an ordinary answer",
        'if result.stop_reason == "turn_limit":',
        "if False:",
    ),
    (
        "subagent.py",
        "the child writes into its parent's session file",
        "session_id=new_session_id(),\n        created=time.time(),",
        "session_id=ctx.parent_session_id or new_session_id(),\n        created=time.time(),",
    ),
    (
        "subagent.py",
        "the parent link is not recorded",
        "parent=ctx.parent_session_id or None,",
        "parent=None,",
    ),
    # The next three were added when the chapter was rewritten: each is a
    # line that could be deleted with every test green.
    (
        "subagent.py",
        "an interrupted child is reported as an empty answer",
        '    elif result.stop_reason == "interrupted":',
        "    elif False:",
    ),
    (
        "subagent.py",
        "the child's shell forgets the parent's command timeout",
        "shell = ShellSession(timeout=ctx.parent_shell.timeout)",
        "shell = ShellSession()",
    ),
    (
        "subagent.py",
        "finished children are not recorded, so the shared budget never fills",
        "    ctx.children.append(result)\n",
        "    pass\n",
    ),
    (
        "clip.py",
        "tail-only truncation, which is F02-03",
        'return f"{text[:head]}\\n... ({omitted} characters omitted) ...\\n{text[-tail:]}"',
        "return text[-limit:]",
    ),
]

SRC = Path("src/minicodex")
ORIGINALS = {name: (SRC / name).read_text(encoding="utf-8") for name, *_ in MUTATIONS}
SUITES = ["tests/test_faults_ch10.py", "tests/test_shell.py", "tests/test_faults_ch09.py"]


def restore() -> None:
    for name, text in ORIGINALS.items():
        path = SRC / name
        if path.read_text(encoding="utf-8") != text:
            path.write_text(text, encoding="utf-8")


atexit.register(restore)
signal.signal(signal.SIGINT, lambda *_: sys.exit(130))


def refuse_if_already_mutated() -> None:
    """Do not start on a tree a killed run left dirty.

    `atexit` and the `SIGINT` handler restore the source on every graceful
    exit and neither runs on a `SIGKILL`.  Chapter 9's snapshot shipped with
    one of its own mutations applied for exactly that reason; this is the
    guard its script grew afterwards.  Both halves are checked -- the original
    text missing *and* the mutated text present -- because several mutations
    replace an expression with a simpler one that occurs elsewhere anyway.
    """
    dirty = [
        f"{name}: looks like {label!r} is still applied"
        for name, label, before, after in MUTATIONS
        if before not in ORIGINALS[name] and after in ORIGINALS[name]
    ]
    if dirty:
        print("refusing to run: the working tree is already mutated\n")
        for line in dirty:
            print(f"  {line}")
        print("\nRestore it (git checkout / re-copy) before running this again.")
        raise SystemExit(2)


def main() -> None:
    refuse_if_already_mutated()
    print(f"{len(MUTATIONS)} mutations, {' '.join(SUITES)}\n")
    survivors = []
    for name, label, before, after in MUTATIONS:
        path = SRC / name
        source = ORIGINALS[name]
        if before not in source:
            print(f"  !! could not apply: {label}")
            survivors.append(label)
            continue
        path.write_text(source.replace(before, after, 1), encoding="utf-8")
        try:
            result = subprocess.run(
                [sys.executable, "-m", "pytest", *SUITES, "-q", "--no-header"],
                timeout=600,
                capture_output=True,
                text=True,
            )
        finally:
            restore()
        failed = len(re.findall(r"^FAILED ", result.stdout, re.M))
        errors = len(re.findall(r"^ERROR ", result.stdout, re.M))
        caught = failed + errors
        print(f"  {caught:>3} test(s) fail  <-  {label}")
        if not caught:
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
