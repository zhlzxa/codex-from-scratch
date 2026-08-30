"""Do chapter 19's tests fail when chapter 19's code is wrong?

Same script as chapters 9 through 18: break one line, run the suite, and see
whether anything turns red.  A mutation nothing notices is not a mutation that
does not matter -- it is a hole in the tests, and the ones this script found on
its first run are written up in the chapter under their own fault numbers
rather than quietly patched.

    uv run python probe_mutations_ch19.py

One difference from every previous chapter: the targets are not all under
`src/`.  Three of them are in `scripts/check_layers.py`, because three of this
chapter's faults were in the *checks* rather than in the code -- and a repaired
check that nothing would notice regressing is exactly the state that let the
original slip through eighteen chapters of green ticks.
"""

from __future__ import annotations

import atexit
import re
import signal
import subprocess
import sys
from pathlib import Path

MUTATIONS = [
    # ---- the event stream ----------------------------------------------------
    (
        "src/minicodex/web/channel.py",
        "events are fanned out as independent tasks again, so order is luck",
        "                        await socket.send_text(payload)",
        "                        _ = asyncio.get_running_loop().create_task(\n"
        "                            socket.send_text(payload)\n"
        "                        )",
    ),
    (
        "src/minicodex/web/channel.py",
        "the stop sentinel is ignored, so a thread's queue outlives its sockets",
        "                if payload is None:\n                    return",
        "                if payload is None:\n                    continue",
    ),
    (
        "src/minicodex/web/channel.py",
        "the empty socket set is left behind on the last unsubscribe",
        "        self._sockets.pop(thread_id, None)",
        "        pass",
    ),
    (
        "src/minicodex/web/channel.py",
        "a socket that raises takes the whole pump down with it",
        "                    try:\n                        await socket.send_text(payload)\n"
        "                    except Exception:\n"
        "                        self._sockets.get(thread_id, set()).discard(socket)",
        "                    await socket.send_text(payload)",
    ),
    # ---- approvals -----------------------------------------------------------
    (
        "src/minicodex/web/approver.py",
        "an approval is resolved straight from whichever thread answered",
        "            loop.call_soon_threadsafe(future.set_result, reply)",
        "            future.set_result(reply)",
    ),
    (
        "src/minicodex/web/approver.py",
        "a second answer to the same approval is accepted",
        "        if future is None or future.done():",
        "        if future is None:",
    ),
    (
        "src/minicodex/web/approver.py",
        "the approval timeout is caught under the 3.10 spelling that never fires",
        "        except asyncio.TimeoutError:\n"
        "            # See channel.py: the builtin spelling does not fire on 3.10.",
        "        except asyncio.CancelledError:\n"
        "            # See channel.py: the builtin spelling does not fire on 3.10.",
    ),
    (
        "src/minicodex/web/approver.py",
        "a timed-out approval comes back approved",
        "            return ApprovalReply(False, request.what)",
        "            return ApprovalReply(True, request.what)",
    ),
    # ---- claiming a turn -----------------------------------------------------
    (
        "src/minicodex/web/routes.py",
        "the busy flag is claimed inside the task again, not before it",
        "    console.busy.add(thread_id)",
        "    pass",
    ),
    (
        "src/minicodex/web/routes.py",
        "settings can be edited underneath a turn that has already read them",
        "        if thread_id in console.busy:\n"
        '            raise HTTPException(409, "cannot change settings '
        'while this thread is answering")',
        "        pass",
    ),
    # ---- where a path enters the program -------------------------------------
    (
        "src/minicodex/web/routes.py",
        "a relative workspace path is accepted",
        "    if not path.is_absolute():",
        "    if False:",
    ),
    (
        "src/minicodex/web/routes.py",
        "a workspace that does not exist is accepted and created later",
        "    if not path.exists():",
        "    if False:",
    ),
    (
        "src/minicodex/web/routes.py",
        "a regular file is accepted as a workspace",
        "    if not path.is_dir():",
        "    if False:",
    ),
    (
        "src/minicodex/web/routes.py",
        "an unknown remember scope reaches the rule store",
        '    if body.remember not in (None, "session", "project"):',
        "    if False:",
    ),
    (
        "src/minicodex/web/routes.py",
        "the api key is echoed back to the browser",
        '    return {**provider, "api_key": bool(provider.get("api_key"))}',
        "    return dict(provider)",
    ),
    # ---- settings, and what a default means ----------------------------------
    (
        "src/minicodex/web/store.py",
        "an invented sandbox mode is accepted and lands on an unpredictable default",
        '    if merged["sandbox_mode"] not in SANDBOX_MODES:',
        "    if False:",
    ),
    (
        "src/minicodex/web/store.py",
        "an invented approval policy is accepted",
        '    if merged["approval_policy"] not in APPROVAL_POLICIES:',
        "    if False:",
    ),
    (
        "src/minicodex/web/store.py",
        "memory reading is on by default -- F17-11, in a browser",
        '    "memory": False,',
        '    "memory": True,',
    ),
    (
        "src/minicodex/web/store.py",
        "memory writing is on by default",
        '    "remember": False,',
        '    "remember": True,',
    ),
    (
        "src/minicodex/web/store.py",
        "a session starts with more permission than the CLI gives it",
        '    "sandbox_mode": "read-only",',
        '    "sandbox_mode": "workspace-write",',
    ),
    (
        "src/minicodex/web/store.py",
        "the settings file is written in place, so a crash mid-write truncates it",
        "        tmp.replace(path)",
        "        pass",
    ),
    (
        "src/minicodex/web/store.py",
        "an unreadable json file takes the server down on start-up",
        "        except (OSError, json.JSONDecodeError):",
        "        except KeyboardInterrupt:",
    ),
    # ---- what the browser is told --------------------------------------------
    (
        "src/minicodex/web/events.py",
        "marks are written to disk but never reach the browser",
        '        self._emit({"type": "mark", "mark": kind, "payload": payload})',
        "        pass",
    ),
    (
        "src/minicodex/web/runtime.py",
        "a permission escalation mid-turn is not reported",
        '        if session.mode != last["mode"]:',
        "        if False:",
    ),
    (
        "src/minicodex/web/runtime.py",
        "the plan is only reported once the turn is over",
        '        if current_plan != last["plan"]:',
        "        if False:",
    ),
    (
        "src/minicodex/web/runtime.py",
        "an unchanged plan re-announces itself after every history item",
        '        if current_plan != last["plan"]:',
        "        if True:",
    ),
    (
        "src/minicodex/web/runtime.py",
        "the console's system prompt loses the skills paragraph",
        "    if skills is not None:\n        skill_tool = tools is not None",
        "    if False:\n        skill_tool = tools is not None",
    ),
    (
        "src/minicodex/web/runtime.py",
        "the console's system prompt loses the memory paragraph",
        "        parts.append(memory_instructions(memory.directory, dedicated_tools=dedicated))",
        "        pass",
    ),
    (
        "src/minicodex/web/runtime.py",
        "the volatile permissions block moves off the end of the cached prefix",
        '    parts.append(block)\n    return "\\n\\n".join(parts)',
        '    parts.insert(0, block)\n    return "\\n\\n".join(parts)',
    ),
    # ---- the checks themselves -----------------------------------------------
    (
        "scripts/check_layers.py",
        "the layer checker goes back to seeing one level, and misses web/ entirely",
        '    for path in sorted(pkg.rglob("*.py")):',
        '    for path in sorted(pkg.glob("*.py")):',
    ),
    (
        "scripts/check_layers.py",
        "relative imports stop counting as edges again",
        "            if node.level:",
        "            if False:",
    ),
    (
        "scripts/check_layers.py",
        "every module becomes an entry point, so the forbidden edge means nothing",
        'TOP_LEVEL_ENTRY_POINTS = {"__main__"}',
        "TOP_LEVEL_ENTRY_POINTS = set()",
    ),
]

#: Mutations that change the code without changing any behaviour a test could
#: legitimately assert.  Named rather than deleted: an equivalent mutant is a
#: fact about the code, and a reader who spots one and cannot tell whether it
#: was considered has to redo the analysis.
EQUIVALENT: set[str] = set()

ROOT = Path(".")
ORIGINALS = {name: (ROOT / name).read_text(encoding="utf-8") for name, *_ in MUTATIONS}
SUITES = [
    "tests/test_faults_ch19.py",
    "tests/test_packaging.py",
    "tests/test_boundaries.py",
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
                # Not `text=True` alone: that decodes with the *locale's*
                # codec, which on a zh-CN Windows box is GBK, and a suite
                # whose failure output contains CJK then dies with a
                # `UnicodeDecodeError` inside subprocess's reader thread --
                # leaving `result.stdout` as `None` and the counting below
                # crashing on it. Chapter 2's F02-07 rule ("a bad byte should
                # come back slightly wrong, not as an exception that discards
                # the whole read") applies to this program's own tools too.
                encoding="utf-8",
                errors="replace",
            )
        except subprocess.TimeoutExpired:
            # Chapter 12's rule: a suite that hangs is a survivor, not a pass.
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
