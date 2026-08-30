"""Do chapter 9's tests fail when chapter 9's code is wrong?

A green suite proves nothing until you have seen it go red on purpose.  Each
entry below is a one-line edit that should break something; the script applies
it, runs the suite, restores the file, and reports how many tests noticed.

Restores from `atexit` and a signal handler, not from `finally` alone -- that
is chapter 6's lesson, learned by leaving `if False:` in `tokens.py` after a
Ctrl-C during a pytest run.
"""

from __future__ import annotations

import atexit
import contextlib
import re
import signal
import subprocess
import sys
from pathlib import Path

MUTATIONS = [
    (
        "registry.py",
        "no namespacing: every server's tool keeps its own name",
        'return f"{PREFIX}{DELIMITER}{sanitize(server)}{DELIMITER}{sanitize(tool)}"',
        "return sanitize(tool)",
    ),
    (
        "registry.py",
        "isError is not mentioned to the model",
        r'text = f"The tool reported an error.\n{text}"',
        "text = text",
    ),
    (
        "registry.py",
        "an empty result renders as an empty string",
        r'text = "\n".join(part for part in parts if part) or "(the tool returned no content)"',
        r'text = "\n".join(part for part in parts if part)',
    ),
    (
        "registry.py",
        "a read-only remote tool is assumed to touch nothing",
        'return Footprint(reads=frozenset({f"mcp:{registration.tool.server}"}))',
        "return Footprint()",
    ),
    (
        "registry.py",
        "the schema budget ignores this project's own tools",
        "if not names or estimate_messages((), self.local + schemas) <= self.schema_budget:",
        "if not names or estimate_messages((), schemas) <= self.schema_budget:",
    ),
    (
        "registry.py",
        "tool_search reveals into a copy instead of the live list",
        "self.visible.append(self.schema_for(name))",
        "list(self.visible).append(self.schema_for(name))",
    ),
    (
        "registry.py",
        "a retired tool gets chapter 0's 'no tool named' treatment",
        'return f"{self.retired[name]}. Use one of the tools you were given instead."',
        'return "Use one of the tools you were given instead."',
    ),
    (
        "registry.py",
        "the deferred index is left out of tool_search's description",
        r'f"Tools available but not yet loaded:\n{self.index()}"',
        r'"Tools available but not yet loaded: (ask and find out)"',
    ),
    # The demultiplexing mutation that used to live here is gone with the code
    # it attacked: telling a response from a notification from a
    # server-initiated request is the SDK's job now. What is left to mutate is
    # what this project still decides for itself -- which is the honest test
    # of whether a dependency was the right call.
    (
        "mcp.py",
        "the server's stderr goes to the terminal, so a dead server cannot say why",
        "return stdio_client(params, errlog=handle)",
        "return stdio_client(params)",
    ),
    (
        "registry.py",
        "structuredContent that only repeats the text is emitted anyway",
        "        if not _echoes(structured, parts):\n            parts.append(",
        "        if True:\n            parts.append(",
    ),
    (
        "mcp.py",
        "no startup timeout",
        "timeout=self.config.startup_timeout,",
        "timeout=None,",
    ),
    (
        "mcp.py",
        "the host environment is handed to every server",
        "env = {key: os.environ[key] for key in ENV_ALLOWLIST if key in os.environ}",
        "env = dict(os.environ)",
    ),
]

SRC = Path("src/minicodex")
ORIGINALS = {name: (SRC / name).read_text(encoding="utf-8") for name, *_ in MUTATIONS}


def restore() -> None:
    for name, text in ORIGINALS.items():
        path = SRC / name
        if path.read_text(encoding="utf-8") != text:
            path.write_text(text, encoding="utf-8")


atexit.register(restore)
signal.signal(signal.SIGINT, lambda *_: sys.exit(130))


def refuse_if_already_mutated() -> None:
    """Do not start on a tree somebody else's run left dirty.

    `atexit` and a `SIGINT` handler restore the source on every *graceful*
    exit, and neither runs on a `SIGKILL` -- so a probe killed by a timeout,
    a CI cancellation or an impatient `taskkill` leaves a mutation applied.

    That is not a hypothetical.  It happened here twice: the second time,
    the mutated `mcp.py` was copied to twelve downstream steps, and every one
    of them reported the *same* failing test.  A whole test sweep looked like
    a regression in the code and was an artefact of a dead process.

    The cheap guard is to look before writing.  The test is **both** halves --
    the original text missing *and* the mutated text present -- because
    `after` alone gives false positives: several mutations replace a specific
    expression with a simpler one that occurs legitimately elsewhere in the
    same file, and a guard that cries wolf is a guard somebody deletes.
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
    # Line-buffered even when stdout is a file.  Each mutation runs the whole
    # suite, so a full run is tens of minutes; with the default block
    # buffering that a redirect turns on, `probe > log.txt` shows *nothing*
    # until the process exits, and a run that has to be killed leaves no
    # record of how far it got -- which is exactly when you want one.
    with contextlib.suppress(AttributeError, ValueError):  # pragma: no cover
        sys.stdout.reconfigure(line_buffering=True)

    refuse_if_already_mutated()
    print(f"{len(MUTATIONS)} mutations, tests/test_faults_ch09.py\n")
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
                [sys.executable, "-m", "pytest", "tests/test_faults_ch09.py", "-q", "--no-header"],
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
