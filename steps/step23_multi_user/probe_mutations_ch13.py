"""Do chapter 13's tests fail when chapter 13's code is wrong?

Same script as chapters 9 through 12, pointed at `agents_md.py`, the two
`DeveloperNote` render/serialise sites in `history.py` and `rollout.py`, and
the `on_turn_start` wiring in `agent.py`.

    uv run python probe_mutations_ch13.py
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
        "agents_md.py",
        "a cwd that escaped the sandbox falls back to the root's own docs",
        "    except ValueError:\n        return []",
        "    except ValueError:\n        return [root]",
    ),
    (
        "agents_md.py",
        "the project-root search walks past the sandbox root",
        "        if current == sandbox_root:\n            return sandbox_root",
        "        if False:\n            return sandbox_root",
    ),
    (
        "agents_md.py",
        "the byte ceiling is checked but never enforced",
        "        remaining = max_bytes - total\n"
        "        if remaining <= 0:\n"
        "            truncated = True\n"
        "            break",
        "        remaining = max_bytes - total\n"
        "        if False:\n"
        "            truncated = True\n"
        "            break",
    ),
    (
        "agents_md.py",
        "change detection ignores edited content when the file list is unchanged",
        "        if current.sources == self._last.sources and current.text == self._last.text:",
        "        if current.sources == self._last.sources:",
    ),
    (
        "agents_md.py",
        "a removal notice is sent even when nothing changed",
        "        if not current:",
        "        if True:",
    ),
    (
        "agents_md.py",
        "the first-ever injection is wrapped in a REPLACEMENT notice for nothing",
        "        if not had_content:\n            return block",
        "        if False:\n            return block",
    ),
    (
        "history.py",
        "a developer note renders with the same wire role as a system note",
        "    if isinstance(item, DeveloperNote):\n"
        '        return {"role": "developer", "content": item.text}',
        "    if isinstance(item, DeveloperNote):\n"
        '        return {"role": "system", "content": item.text}',
    ),
    (
        "agent.py",
        "on_turn_start is never called, so AGENTS.md updates never reach the model",
        "            if self.on_turn_start is not None:\n"
        "                note = self.on_turn_start()",
        "            if False:\n                note = self.on_turn_start()",
    ),
    (
        "agent.py",
        "the AGENTS.md note is appended as a system note, defeating F13-12's role choice",
        "                if note is not None:\n"
        "                    history.add_developer_note(note)",
        "                if note is not None:\n                    history.add_system_note(note)",
    ),
    (
        "rollout.py",
        "a developer note crashes the writer instead of round-tripping through a resume",
        "    if isinstance(item, DeveloperNote):\n"
        '        return {"type": "developer_note", "text": item.text}',
        "    if False:\n        pass",
    ),
]

SRC = Path("src/minicodex")
ORIGINALS = {name: (SRC / name).read_text(encoding="utf-8") for name, *_ in MUTATIONS}
SUITES = [
    "tests/test_faults_ch13.py",
    "tests/test_agent.py",
    "tests/test_history.py",
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
