"""Do chapter 14's tests fail when chapter 14's code is wrong?

Same script as chapters 9 through 13, and this time it is also the chapter's
own subject: the first six entries below are the mutations that **survived the
whole 1525-test suite** before this chapter existed, which is how the chapter
found out what it had to write.

    uv run python probe_mutations_ch14.py
"""

from __future__ import annotations

import atexit
import re
import signal
import subprocess
import sys
from pathlib import Path

MUTATIONS = [
    # ---- the two that survived everything, before chapter 14 ----------------
    (
        "__main__.py",
        "the CLI sends no system message at all",
        "        instructions=_instructions(session, tools),",
        "        instructions=None,",
    ),
    (
        "agent.py",
        "compaction starts at 95% of the window instead of 75%",
        "COMPACT_AT = 0.75",
        "COMPACT_AT = 0.95",
    ),
    # ---- the recording has to carry enough to be re-run ---------------------
    (
        "replay.py",
        "a tool call is recorded without its arguments, as before this chapter",
        '    return {"id": call.call_id, "name": call.name, "arguments": call.raw_arguments}',
        '    return {"id": call.call_id, "name": call.name}',
    ),
    (
        "replay.py",
        "the parsed arguments are recorded instead of the bytes the model sent",
        '    return {"id": call.call_id, "name": call.name, "arguments": call.raw_arguments}',
        '    return {"id": call.call_id, "name": call.name, "arguments": str(call.arguments)}',
    ),
    (
        "replay.py",
        "a failure is recorded as prose only, as before this chapter",
        '        return {"status": exc.status, "url": exc.url, '
        '"headers": exc.headers, "body": exc.body}',
        '        return {"status": 0}',
    ),
    (
        "replay.py",
        "a recording missing its arguments is filled in with an empty object",
        '        if "arguments" not in call:',
        "        if False:",
    ),
    (
        "replay.py",
        "the request is replayed without being compared with the recorded one",
        "        if self.strict:",
        "        if False:",
    ),
    (
        "replay.py",
        "the drift report shows the first 90 characters instead of the difference",
        "                    was, now = _window(b.get(key), a.get(key))",
        "                    was, now = _short(b.get(key)), _short(a.get(key))",
    ),
    (
        "replay.py",
        "a tool output is matched by name alone, ignoring the arguments",
        "            key = (name, json.dumps(arguments, sort_keys=True, ensure_ascii=False))",
        "            key = (name, json.dumps({}, sort_keys=True, ensure_ascii=False))",
    ),
    (
        "replay.py",
        "drift becomes an ordinary Exception again, and the loop swallows it",
        "class ReplayDrift(BaseException):",
        "class ReplayDrift(Exception):",
    ),
    # ---- the eval harness ---------------------------------------------------
    (
        "tools.py",
        "the shell starts in the process's directory rather than at the root",
        "        shell=shell or ShellSession(cwd=resolved),",
        "        shell=shell or ShellSession(),",
    ),
    (
        "evals.py",
        "a regression is reported only when the total gets worse",
        "        if now < was:",
        "        if False:",
    ),
    (
        "evals.py",
        "a task passes when any check passes rather than when none fails",
        "        return not self.failed and self.error is None",
        "        return bool(self.passed) and self.error is None",
    ),
    (
        "evals.py",
        "the workspace keeps whatever was already in the directory",
        "    workspace.mkdir(parents=True, exist_ok=True)\n    for name, content in files.items():",
        "    workspace.mkdir(parents=True, exist_ok=True)\n    for name, content in []:",
    ),
]

SRC = Path("src/minicodex")
ORIGINALS = {name: (SRC / name).read_text(encoding="utf-8") for name, *_ in MUTATIONS}
SUITES = [
    "tests/test_faults_ch14.py",
    "tests/test_agent.py",
    "tests/test_compaction.py",
]


def restore() -> None:
    for name, text in ORIGINALS.items():
        path = SRC / name
        if path.read_text(encoding="utf-8") != text:
            path.write_text(text, encoding="utf-8")


atexit.register(restore)
signal.signal(signal.SIGINT, lambda *_: sys.exit(130))


def main() -> None:
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
                timeout=900,
                capture_output=True,
                text=True,
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
