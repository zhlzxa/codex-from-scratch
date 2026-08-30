#!/usr/bin/env python
"""The two release facts that only exist at release time.

    uv run python scripts/check_release.py v0.1.0

Everything else worth checking before publishing is already a test, and is
therefore already run by `ci.yml` on the commit being tagged: the wheel
contains every data file the code reads, no deprecation has outlived its
removal version, the metadata declares a licence and an issue tracker.
Repeating those here would be a second ruler, and this project has paid for
that mistake twice (F06-12, and the memory summary in chapter 17).

What is *not* a test, because it does not exist until somebody types it:

  1. the tag and the version agree.  `git tag v0.2.0` on a tree whose
     `__version__` says 0.1.0 publishes 0.1.0 under a name that says otherwise,
     and the artefact is immutable on PyPI five seconds later.
  2. the changelog has an entry for it.  A release with no entry is a release
     whose users have to read the diff, which is the thing a changelog exists
     to stop.

Exit 0 in silence, exit 1 naming the disagreement.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def declared_version(root: Path) -> str:
    text = (root / "src" / "minicodex" / "__init__.py").read_text(encoding="utf-8")
    match = re.search(r'^__version__ = "([^"]+)"', text, re.MULTILINE)
    if match is None:  # pragma: no cover - only if somebody edits the literal away
        raise SystemExit("no __version__ in src/minicodex/__init__.py")
    return match.group(1)


def main(argv: list[str]) -> int:
    # `--root` exists so that the changelog half of this can be tested against
    # a tree that is missing an entry, which cannot be arranged in the real one
    # without editing the real changelog.  Mutation testing is what asked for
    # it: deleting the changelog check left every test green, because the only
    # test called this script on a repository whose changelog was correct.
    root = ROOT
    if argv and argv[0] == "--root":
        root = Path(argv[1])
        argv = argv[2:]

    version = declared_version(root)
    problems = []

    if argv:
        # `v0.1.0` and `0.1.0` are the same release.  Accepting both is not
        # leniency for its own sake -- the tag convention is a habit and the
        # version is a fact, and disagreeing about the `v` is not a mistake
        # worth failing a release for.  Disagreeing about the number is.
        tag = argv[0].lstrip("v")
        if tag != version:
            problems.append(f"tag {argv[0]} does not match __version__ {version}")

    changelog = (root / "CHANGELOG.md").read_text(encoding="utf-8")
    if not re.search(rf"^## {re.escape(version)} — \d{{4}}-\d{{2}}-\d{{2}}", changelog, re.M):
        problems.append(f"CHANGELOG.md has no dated `## {version}` section")

    for problem in problems:
        print(f"release check: {problem}", file=sys.stderr)
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
