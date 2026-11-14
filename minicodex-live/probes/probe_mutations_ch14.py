"""Do chapter 14's tests fail when chapter 14's code is wrong?

Same script as chapters 9 through 13, and this time it is also the chapter's
own subject.  The first two entries are the mutations that survived the whole
suite when this chapter was first written, which is how it found out what it
had to write.  (Chapters 11 and 13 have since grown CLI-level tests that catch
both on their own; they stay here because this chapter's tests have to catch
them too.)  The last nine were added when the chapter was rewritten, one per
defect found in the replay, the shrinker and the flake checker themselves.

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
    # ---- found while rewriting the chapter: the replay saw less than it said -
    (
        "agent.py",
        "every agent signs its events with the same number",
        "        self.conversation = next(_CONVERSATIONS)",
        "        self.conversation = 0",
    ),
    (
        "replay.py",
        "a sub-agent's requests are loaded as though they were its parent's",
        '            if payload.get("conversation") != top:\n                elsewhere += 1',
        "            if False:\n                elsewhere += 1",
    ),
    (
        "replay.py",
        "a repeated (turn, attempt) silently replaces the earlier request",
        "            if key in requests:",
        "            if False:",
    ),
    (
        "agent.py",
        "a replay sleeps through every wait it was recorded with",
        "            if self.retry_policy.serve_waits:",
        "            if True:",
    ),
    (
        "agent.py",
        "a wait that was not served is not counted against the budget either",
        "                skipped += wait",
        "                pass",
    ),
    (
        "__main__.py",
        "a run that stops before the recording does is reported as ok",
        "    if model.unused:",
        "    if False:",
    ),
    (
        "__main__.py",
        "--loose still prints 'match' for requests it never compared",
        '    verdict = "match" if strict else "sent, not compared"',
        '    verdict = "match"',
    ),
    # ---- the two tools that check the tests ----------------------------------
    (
        "../../tests/property_support.py",
        "the shrinker offers a history with an unanswered call as a candidate",
        "        history.to_wire()\n    except Exception:",
        "        pass\n    except Exception:",
    ),
    (
        "../../probes/probe_flaky.py",
        "a test that is missing from one run is counted as stable",
        "if count < len(runs))",
        "if False)",
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
