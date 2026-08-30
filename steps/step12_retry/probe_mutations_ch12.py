"""Do chapter 12's tests fail when chapter 12's code is wrong?

Same script as chapters 9, 10 and 11, pointed at `retry.py`, the retry loop in
`agent.py`, the error parsing in `model.py`, the narrowed degrade branch in
`compaction.py` and the new outcome in `subagent.py`.  Each entry is an edit
that should break something; the script applies it, runs the suite, restores
the file, and reports how many tests noticed.

Restores from `atexit` and a signal handler, not from `finally` alone -- that
is chapter 6's lesson, learned by leaving `if False:` in `tokens.py` after a
Ctrl-C during a pytest run.

    uv run python probe_mutations_ch12.py
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
        "retry.py",
        "a context-length refusal is terminal like any other 400",
        '"context_length_exceeded": ("shrink", "context_length"),',
        '"context_length_exceeded": ("fatal", "context_length"),',
    ),
    (
        "retry.py",
        "classification reads the status code and nothing else",
        'disposition, kind = _BY_CODE.get(exc.code or "", ("", ""))',
        'disposition, kind = ("", "")',
    ),
    (
        "retry.py",
        "an unrecognised failure is retried instead of reported",
        'return Failure("fatal", "unexpected", f"{type(exc).__name__}: {exc}")',
        'return Failure("retry", "unexpected", f"{type(exc).__name__}: {exc}")',
    ),
    (
        "retry.py",
        "a request this program built wrongly is retried as if it were weather",
        "    if isinstance(exc, _OUR_FAULT):",
        "    if False:",
    ),
    (
        "retry.py",
        "the server's Retry-After is ignored in favour of our own backoff",
        "    if failure.retry_after is not None:",
        "    if False:",
    ),
    (
        "retry.py",
        "the millisecond header loses to the rounded-up whole-second one",
        'for name, scale in (("retry-after-ms", 0.001), ("retry-after", 1.0)):',
        'for name, scale in (("retry-after", 1.0), ("retry-after-ms", 0.001)):',
    ),
    (
        "retry.py",
        "an unparseable Retry-After raises instead of falling back",
        "        except ValueError:\n            continue",
        "        except ValueError:\n            raise",
    ),
    (
        "retry.py",
        "the budget is advisory: wait the server's number even if it does not fit",
        "    if elapsed + wait > policy.budget:\n        return None",
        "    if False:\n        return None",
    ),
    (
        "agent.py",
        "attempts are unbounded",
        "for attempt in range(self.retry_policy.attempts):",
        # Six rather than a hundred, and the number was measured.  With
        # `range(100)` the suite does not go red -- it goes *slow*: every test
        # that expects a failure sits through a 1-2-4-...-30 backoff until it
        # hits the 90-second budget, and the whole run passed the runner's
        # 900-second limit and died.  A mutation whose symptom is a timeout
        # tells you nothing about the tests, because none of them finished.
        "for attempt in range(6):",
    ),
    (
        "retry.py",
        "the failure is reported without a next action",
        '    action = _ACTIONS.get(failure.kind, "")',
        '    action = ""',
    ),
    (
        "retry.py",
        "the request id is dropped again",
        "    if failure.request_id:",
        "    if False:",
    ),
    (
        "model.py",
        "the error body is not parsed, only formatted",
        '        if isinstance(payload, dict) and isinstance(payload.get("error"), dict):',
        "        if False:",
    ),
    (
        "model.py",
        "a non-JSON body from a proxy takes the error handling down with it",
        "        except ValueError:  # includes JSONDecodeError; see the docstring\n"
        "            payload = None",
        "        except ValueError:  # includes JSONDecodeError; see the docstring\n"
        "            raise",
    ),
    (
        "model.py",
        "one attempt may take as long as a whole sub-task",
        "DEFAULT_ATTEMPT_TIMEOUT = 120.0",
        "DEFAULT_ATTEMPT_TIMEOUT = 300.0",
    ),
    (
        "agent.py",
        "a cancellation is classified as a failure and retried",
        "            except Exception as exc:  # not BaseException: a Ctrl-C is not a failure",
        "            except BaseException as exc:  # noqa: BLE001",
    ),
    (
        "agent.py",
        "the turn is compacted as many times as the provider asks",
        "        if shrunk:",
        "        if False:",
    ),
    (
        "agent.py",
        "a compaction that saves nothing still counts, and the request is re-sent",
        "        if result.plan.saving <= 0:",
        "        if False:",
    ),
    (
        "agent.py",
        "the stated token count is fed to the calibration unclamped",
        "            if 0 < correction <= MAX_REFUSAL_CORRECTION:",
        "            if True:",
    ),
    (
        "agent.py",
        "the retry budget counts sleeping only, not the whole turn",
        "            elapsed = time.monotonic() - began",
        "            elapsed = 0.0",
    ),
    (
        "agent.py",
        "failures are not recorded in the transcript",
        '                "model_failure",',
        '                "ignored",',
    ),
    (
        "compaction.py",
        "a fatal provider failure during compaction is degraded like a dropped connection",
        "        if isinstance(exc, ModelHTTPError):",
        "        if False:",
    ),
    (
        "compaction.py",
        "every summariser failure is fatal, including a bug in the summariser",
        '            failure = classify(exc)\n            if failure.disposition == "fatal":',
        "            failure = classify(exc)\n            if True:",
    ),
    (
        "subagent.py",
        "a provider failure inside a child reaches the parent model as a traceback",
        "    except ModelFailed as exc:",
        "    except _NeverRaised as exc:",
    ),
]

SRC = Path("src/minicodex")
ORIGINALS = {name: (SRC / name).read_text(encoding="utf-8") for name, *_ in MUTATIONS}
SUITES = [
    "tests/test_faults_ch12.py",
    "tests/test_agent.py",
    "tests/test_compaction.py",
    "tests/test_faults_ch10.py",
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
            # Not "caught".  A run that did not finish measured nothing, and
            # chapter 6's rule about unapplied mutations applies here too: the
            # only honest report is that this one is unknown.
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
