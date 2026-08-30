"""Do chapter 11's tests fail when chapter 11's code is wrong?

Same script as chapters 9 and 10, pointed at `plan.py`, the loop's stop check
in `agent.py`, the wrapper in `composition.py` and the one-word change in
`shell.py`.  Each entry is an edit that should break something; the script
applies it, runs the suite, restores the file, and reports how many tests
noticed.

Restores from `atexit` and a signal handler, not from `finally` alone -- that
is chapter 6's lesson, learned by leaving `if False:` in `tokens.py` after a
Ctrl-C during a pytest run.

    uv run python probe_mutations_ch11.py
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
        "plan.py",
        "the step limit is not enforced",
        "if len(raw) > MAX_STEPS:",
        "if False:",
    ),
    (
        "plan.py",
        "two steps may be in_progress at once, as in codex",
        "if len(running) > 1:",
        "if False:",
    ),
    (
        "plan.py",
        "a step may be closed with nothing behind it",
        "if finished and plan.work_since_update == 0 and plan.updates > 0:",
        "if False:",
    ),
    (
        "plan.py",
        "the first plan is held to the evidence rule too",
        "if finished and plan.work_since_update == 0 and plan.updates > 0:",
        "if finished and plan.work_since_update == 0:",
    ),
    (
        "plan.py",
        "the work counter is not reset, so one edit justifies every later claim",
        "    plan.work_since_update = 0",
        "    plan.work_since_update += 0",
    ),
    (
        "plan.py",
        "an update_plan call counts as work",
        'if name != "update_plan":',
        "if True:",
    ),
    (
        "plan.py",
        "outstanding() counts in_progress as done",
        'step for step in self.steps if step.status != "completed"',
        'step for step in self.steps if step.status == "pending"',
    ),
    (
        "plan.py",
        "a rejected update is applied anyway",
        "    if error is not None:\n        return error",
        "    if error is not None and False:\n        return error",
    ),
    (
        "plan.py",
        "revisions are not kept, so a rewritten plan leaves no trace",
        "plan.revisions.append(plan.render())",
        "pass",
    ),
    (
        "plan.py",
        "the answer is codex's `Plan updated` with no list",
        'return f"Plan updated.\\n{plan.render()}\\n{tail}"',
        'return "Plan updated"',
    ),
    (
        "plan.py",
        "the stop check fires on a finished plan too",
        "        if not left:\n            return None",
        "        if False:\n            return None",
    ),
    (
        "agent.py",
        "the loop never asks the stop question",
        "if self.on_stop is not None and not nudged and remaining > 1:",
        "if False:",
    ),
    (
        "agent.py",
        "the nudge can renew itself every turn",
        "and not nudged and remaining > 1:\n                    nudged = True",
        "and remaining > 1:\n                    nudged = True",
    ),
    (
        "agent.py",
        "the nudge is allowed to spend the last turn",
        "and not nudged and remaining > 1:",
        "and not nudged:",
    ),
    (
        "agent.py",
        "the last turn still runs tool calls, which is F11-07",
        "            if remaining == 1:\n                for call in turn.tool_calls:",
        "            if False:\n                for call in turn.tool_calls:",
    ),
    (
        "agent.py",
        "an unrun last-turn call gets no output",
        "                    history.add_tool_result(call.call_id, BUDGET_DENIAL)",
        "                    pass",
    ),
    (
        "agent.py",
        "the last two turns are told the same thing",
        "== 1:\n                history.add_system_note(FINAL_TURN_WARNING)",
        "== 99:\n                history.add_system_note(FINAL_TURN_WARNING)",
    ),
    (
        "__main__.py",
        "the plan paragraph is sent even when the tool is absent (F05-10)",
        'if tools is not None and "update_plan" in tools.handlers:',
        "if True:",
    ),
    (
        "__main__.py",
        "the plan paragraph is never sent, so the tool goes unused",
        'if tools is not None and "update_plan" in tools.handlers:',
        "if False:",
    ),
    (
        "composition.py",
        "tool calls are not reported to the plan",
        "            plan.record_work(name)",
        "            pass",
    ),
    (
        "shell.py",
        "SYSTEMROOT is dropped again, so the agent cannot run its own tests",
        '"TZ", "SYSTEMROOT")',
        '"TZ")',
    ),
]

SRC = Path("src/minicodex")
ORIGINALS = {name: (SRC / name).read_text(encoding="utf-8") for name, *_ in MUTATIONS}
SUITES = [
    "tests/test_faults_ch11.py",
    "tests/test_agent.py",
    "tests/test_schemas.py",
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
