"""Break one decision at a time; check a test notices.

The output of this is not "the tests are good". It is a distribution: which
decisions are pinned by many tests, which by exactly one, and which by none.
The last group is the interesting one -- code nothing tests is code nobody can
change safely, and it looks exactly like code that works.

Chapter 5's version of this script reported all-green because it counted the
wrong lines, so two things are non-negotiable here: count `FAILED`, and abort
loudly if the mutation did not actually apply.

    python probe_mutations.py
"""

from __future__ import annotations

import atexit
import pathlib
import signal
import subprocess
import sys

SRC = pathlib.Path("src/minicodex")

# (label, file, find, replace)
MUTATIONS: list[tuple[str, str, str, str]] = [
    (
        "boundaries: every index is a legal cut",
        "compaction.py",
        "        if open_calls == 0:\n            result.append(index)",
        "        if True:\n            result.append(index)",
    ),
    (
        "boundaries: forget that a result closes a call",
        "compaction.py",
        "        elif isinstance(item, ToolResult):\n            open_calls -= 1",
        "        elif isinstance(item, ToolResult):\n            open_calls -= 0",
    ),
    (
        "protected: stop protecting the first user message",
        "compaction.py",
        "        if index < len(items) and isinstance(items[index], UserMessage):\n"
        "            index += 1",
        "        if False:\n            index += 1",
    ),
    (
        "plan: do not reserve room for the summary",
        "compaction.py",
        "        size = _size(head, tail, sizer) + summary_budget",
        "        size = _size(head, tail, sizer)",
    ),
    (
        "plan: take the last legal cut instead of the earliest",
        "compaction.py",
        "    for cut in candidates:",
        "    for cut in reversed(candidates):",
    ),
    (
        "sizer: ignore the calibration when trimming the summary",
        "compaction.py",
        "        keep = int(budget * CHARS_PER_TOKEN / self.ratio)",
        "        keep = int(budget * CHARS_PER_TOKEN)",
    ),
    (
        "clip_item: keep the tail only",
        "compaction.py",
        "    head = limit_chars // 2\n    tail = limit_chars - head",
        "    head = 0\n    tail = limit_chars",
    ),
    (
        "compact: accept an empty summary",
        "compaction.py",
        "        if not summary.strip():\n            raise CompactionError("
        '"summariser returned an empty summary")',
        "        pass",
    ),
    (
        "compact: do not show the summariser the protected prefix",
        "compaction.py",
        "                context=render_transcript(items[: the_plan.protected]),",
        '                context="",',
    ),
    (
        "generation: parse the last number in the header again",
        "compaction.py",
        '                if token == "generation" and tokens[index + 1].isdigit():\n'
        "                    generation = int(tokens[index + 1])\n"
        "                    break",
        "                if tokens[index + 1].isdigit():\n"
        "                    generation = int(tokens[index + 1])",
    ),
    (
        "plan: compact even when it would not save anything",
        "compaction.py",
        "            if size >= current and cut > protected_count:\n"
        "                return _do_nothing(fits=current <= budget)",
        "            if False:\n                return _do_nothing(fits=current <= budget)",
    ),
    (
        "tokens: stop counting the tool schemas",
        "tokens.py",
        "    if tools:\n        total += len(json.dumps(list(tools))) / chars_per_token",
        "    if False:\n        total += len(json.dumps(list(tools))) / chars_per_token",
    ),
    (
        "tokens: drop the per-message framing cost",
        "tokens.py",
        "PER_MESSAGE_TOKENS = 4",
        "PER_MESSAGE_TOKENS = 0",
    ),
    (
        "tokens: let a zero usage report set the ratio",
        "tokens.py",
        "        if estimated <= 0 or actual <= 0:\n            return",
        "        if estimated <= 0:\n            return",
    ),
    (
        "tokens: size unknown content as empty instead of refusing",
        "tokens.py",
        "    raise UncountableContent(",
        "    return 0\n    raise UncountableContent(",
    ),
    (
        "model: stop asking for usage",
        "model.py",
        '        if self.report_usage:\n            body["stream_options"] = '
        '{"include_usage": True}',
        "        pass",
    ),
    (
        "model: index into an empty choices list again",
        "model.py",
        '                    if not chunk.get("choices"):\n                        continue',
        "                    pass",
    ),
    (
        "agent: compact down to the trigger point",
        "agent.py",
        "COMPACT_TO = 0.45",
        "COMPACT_TO = 0.75",
    ),
]


_ORIGINALS: dict[pathlib.Path, str] = {}


def _restore_everything() -> None:
    """Put every touched file back, whatever happens.

    A `try/finally` around one mutation is not enough, and finding that out
    cost a corrupted working tree: Ctrl-C during the pytest subprocess killed
    this process before `finally` ran, and left `if False:` sitting in
    `tokens.py`.  The suite was still green afterwards -- the mutation only
    breaks tests that assert on schema size -- so nothing announced it.

    A tool that edits your source is a tool that can leave your source edited.
    Snapshot up front, restore from an exit hook and a signal handler, and make
    "restore" idempotent so running it twice is free.
    """
    for path, text in _ORIGINALS.items():
        if path.read_text(encoding="utf-8") != text:
            path.write_text(text, encoding="utf-8")
            print(f"restored {path}")


def run_tests() -> tuple[int, str]:
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "--no-header", "-p", "no:cacheprovider"],
        capture_output=True,
        text=True,
    )
    failed = sum(1 for line in proc.stdout.splitlines() if line.startswith("FAILED"))
    names = [
        line.split("::", 1)[-1].split(" ")[0]
        for line in proc.stdout.splitlines()
        if line.startswith("FAILED")
    ]
    return failed, ", ".join(sorted(set(names))[:3])


def main() -> int:
    for _, filename, _, _ in MUTATIONS:
        path = SRC / filename
        _ORIGINALS.setdefault(path, path.read_text(encoding="utf-8"))
    atexit.register(_restore_everything)
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: sys.exit(130))

    baseline, _ = run_tests()
    if baseline:
        print(f"baseline is not green ({baseline} failures); fix that first")
        return 1
    print(f"baseline: green\n\n{'mutation':<52} {'failed':>7}  first tests to notice")
    print("-" * 110)

    survivors = []
    for label, filename, find, replace in MUTATIONS:
        path = SRC / filename
        original = path.read_text(encoding="utf-8")
        if find not in original:
            print(f"{label:<52} {'ABORT':>7}  pattern not found in {filename}")
            return 2
        try:
            path.write_text(original.replace(find, replace, 1), encoding="utf-8")
            failed, names = run_tests()
        finally:
            _restore_everything()
        if failed == 0:
            survivors.append(label)
        print(f"{label:<52} {failed:>7}  {names}")

    print("-" * 110)
    if survivors:
        print(f"{len(survivors)} mutation(s) survived -- nothing tests these decisions:")
        for label in survivors:
            print(f"  - {label}")
    else:
        print("every mutation was caught")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
