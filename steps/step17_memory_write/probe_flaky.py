"""Run the suite N times and report anything that did not decide the same way twice.

    uv run python probe_flaky.py [--runs 5] [--target tests/]

F14-02's entry says "flakiness is a defect", which is easy to write and needs a
mechanism, because the failure mode is social: a test that fails once in twenty
runs gets re-run, goes green, and is forgotten -- and after that every real
failure has a ready-made explanation.  Chapter 11 recorded one honestly
(`test_F_1_01_built_wheel_actually_contains_the_data_file`, red once in eight
runs, cause unknown) precisely so that it would not become that.

This does not make the suite deterministic.  It makes non-determinism *visible*
and *named*: a test that appears in this report is a defect with a number, not
a mood.

**The property seed is deliberately held fixed**, and the first version got
this wrong in an instructive way.  Varying it per run looked obviously right --
"a suite that is only stable because it runs the same 200 random cases is
stable about nothing" -- but chapter 6's property tests are parametrised *by
the seed*, so the test ids change with it:

    test_property_compaction_never_grows_the_history[7000]

A thousand new ids per run means those thousand tests are never compared with
anything, and the summary line dutifully reported **5548 distinct tests over 5
runs** as though that were a number of tests.  The exploration those seeds buy
is a different job, and it already has a home: the `MINICODEX_PROPERTY_CASES:
2000` step in `ci.yml`.  This tool answers one question -- *is the suite
deterministic* -- and answering it requires holding the input still.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
import time
from collections import defaultdict

# `.+?` and not `\S+`, and that was the third bug in this file rather than a
# style preference: ten parametrised ids in this repository contain spaces
#
#     ...::test_a_malformed_plan_says_what_to_send_instead[argument3-not an
#     object-Each step is] PASSED
#
# so a name pattern of `\S+` stopped at the first space, the status did not
# follow, and the line was dropped -- silently, into the same "I did not see
# it" shape as the two bugs above.  1548 parsed against 1558 reported, which
# nothing would have noticed without counting both.
RESULT = re.compile(r"^(?P<name>tests/.+?)\s+(?P<status>PASSED|FAILED|ERROR|SKIPPED)(\s|$)")


def one_run(target: str) -> tuple[dict[str, str], float]:
    began = time.monotonic()
    result = subprocess.run(
        # `-vv`, not `-v`.  `pyproject.toml` puts `-q` in `addopts`, so `-v`
        # nets back to the default dotted output and this parser sees zero
        # tests -- which the first version then reported as "every test decided
        # the same way every time".  A tool that cannot see failures reports
        # none; chapter 6 wrote that down about its mutation script and it is
        # true of every checker in this repository.
        [sys.executable, "-m", "pytest", target, "-vv", "--no-header"],
        capture_output=True,
        text=True,
    )
    outcomes: dict[str, str] = {}
    for line in result.stdout.splitlines():
        match = RESULT.match(line.replace("\\", "/"))
        if match:
            outcomes[match["name"]] = match["status"]
    if not outcomes:
        raise SystemExit(
            "probe_flaky.py parsed no test results at all. That is a bug in this "
            "script, not a green suite:\n" + result.stdout[-2000:]
        )
    return outcomes, time.monotonic() - began


def verdict(runs: list[dict[str, str]]) -> tuple[dict[str, list[str]], list[str]]:
    """Which tests changed their mind, and which were not there every time.

    The second list is the fourth "I did not see it" in this file, found when
    the chapter was rewritten.  The first three versions only ever asked *did a
    test give two different answers*; a test that passes in run 0 and is simply
    absent from run 1 -- not collected, or its line not parsed -- gave one
    answer, once, and was counted as stable.  There was a `missing` list for
    this, and its condition was `len(statuses) == 1 and not statuses`, which
    no set can satisfy.
    """
    seen: dict[str, set[str]] = defaultdict(set)
    appeared: dict[str, int] = defaultdict(int)
    for outcomes in runs:
        for name, status in outcomes.items():
            seen[name].add(status)
            appeared[name] += 1
    unstable = {name: sorted(statuses) for name, statuses in seen.items() if len(statuses) > 1}
    absent = sorted(name for name, count in appeared.items() if count < len(runs))
    return unstable, absent


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runs", type=int, default=5)
    parser.add_argument("--target", default="tests")
    args = parser.parse_args()

    runs = []
    times = []
    for index in range(args.runs):
        outcomes, elapsed = one_run(args.target)
        runs.append(outcomes)
        times.append(elapsed)
        failed = sum(1 for s in outcomes.values() if s in ("FAILED", "ERROR"))
        print(f"  run {index}: {len(outcomes)} tests, {failed} failed, {elapsed:.1f}s")

    unstable, absent = verdict(runs)
    distinct = len({name for outcomes in runs for name in outcomes})
    print(f"\n  {distinct} distinct tests over {args.runs} runs")
    print(f"  wall clock: {sum(times):.1f}s total, {sum(times) / len(times):.1f}s per run")
    if unstable:
        print(f"\n  {len(unstable)} test(s) did not decide the same way every time:")
        for name, statuses in sorted(unstable.items()):
            print(f"    {name}: {statuses}")
    if absent:
        print(f"\n  {len(absent)} test(s) were not there in every run:")
        for name in absent[:20]:
            print(f"    {name}")
    if unstable or absent:
        return 1
    print("  every test decided the same way every time.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
