"""Measures F08-09: does the race in test_F08_01 need the deliberate delay?

Runs the same naive two-writer race from tests/test_scheduler.py many times,
with and without the delay inserted in `read_source`, and reports how often
each version actually loses an edit. Not part of the test suite -- a flaky
assertion that "usually" fails is worse than no assertion at all, so the
delayed version is what ships, and this script is the receipt for why.

    uv run python probe_scheduler.py
"""

from __future__ import annotations

import asyncio
import shutil
import tempfile
import time
from pathlib import Path

from minicodex.patch import Edit, apply_edits, read_source


async def one_trial(delay: float) -> bool:
    """True if the edit was lost -- i.e. the race actually happened."""
    tmp = Path(tempfile.mkdtemp())
    try:
        path = tmp / "shared.txt"
        path.write_text("A = 1\nB = 2\n")

        original = read_source

        def maybe_slow(p: Path) -> tuple[str, str]:
            text, ending = original(p)
            if delay:
                time.sleep(delay)
            return text, ending

        import minicodex.patch as patch_mod

        patch_mod.read_source = maybe_slow
        try:
            await asyncio.gather(
                asyncio.to_thread(apply_edits, [Edit(str(path), "A = 1", "A = 100")], tmp),
                asyncio.to_thread(apply_edits, [Edit(str(path), "B = 2", "B = 200")], tmp),
            )
        finally:
            patch_mod.read_source = original

        final = path.read_text()
        return not ("A = 100" in final and "B = 200" in final)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


async def measure(delay: float, n: int) -> int:
    return sum([await one_trial(delay) for _ in range(n)])


async def main() -> None:
    n = 30
    print(f"=== F08-01 / F08-09: does the naive race need a deliberate delay? (n={n}) ===")
    for delay in (0.0, 0.001, 0.05):
        lost = await measure(delay, n)
        print(f"    read_source delay={delay:>6.3f}s   edit lost: {lost}/{n}")


if __name__ == "__main__":
    asyncio.run(main())
