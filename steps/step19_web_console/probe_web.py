"""What the web console cost, measured.

    uv run python probe_web.py <section>
        core-diff  offline  how much of the agent had to change to grow a browser
        order      network* how often the naive fan-out delivers events out of order
        bundle     offline  what replacing one HTML file with a React build costs
        stream     network  per-history-item streaming vs per-token: what a reader waits

    * `order` opens no socket: "network" here means only that it drives a real
      event loop. It is offline like everything else but `stream`.

`stream` is the one that needs a real model, and it is the one that decides
whether this chapter's central shortcut is defensible. The console streams one
event per *history item* because that is what `History`'s observer hook already
gives it -- an event reaches the browser if and only if it was also fsynced to
disk, for free, with no new code in the agent. The alternative is a second path
out of the model client with its own ordering and its own failure modes. That
trade is only worth taking if the reader does not pay much for it, and how much
a reader pays is a number nobody has.

Needs `OPENAI_API_KEY` for `stream`, read from the environment and never
printed. Nothing here is a test; the tests are in `tests/test_faults_ch19.py`
and never touch the network.
"""

from __future__ import annotations

import asyncio
import itertools
import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SRC = ROOT / "src" / "minicodex"
STEPS = ROOT.parent
BASELINE = STEPS / "step18_skills" / "src" / "minicodex"
RELEASE = STEPS / "step15_release" / "src" / "minicodex"


# ---------------------------------------------------------------------------
# core-diff -- the headline
# ---------------------------------------------------------------------------


def _diff_lines(a: Path, b: Path) -> int:
    """Changed lines between two files, as `diff` counts them."""
    if not a.exists() or not b.exists():
        return -1
    old = a.read_text(encoding="utf-8").splitlines()
    new = b.read_text(encoding="utf-8").splitlines()
    import difflib

    return sum(
        1
        for line in difflib.unified_diff(old, new, n=0)
        if line.startswith(("+", "-")) and not line.startswith(("+++", "---"))
    )


def core_diff() -> None:
    """How many lines of the agent did the console need changed?

    The claim the chapter makes is that the answer is zero, because every seam
    the console needed was already there for another chapter's reason: chapter
    5's `Approver` protocol, chapter 7's `RolloutWriter` observer and its
    `resolve("last", dir)`, interlude B's single composition root.

    A claim like that is worth measuring rather than asserting, because it is
    the kind that quietly stops being true. So this diffs every top-level
    module against chapter 18's, and then separates the two reasons a file can
    differ: because the console needed it, or because chapter 15's release
    fixes were carried back (this step reads *before* chapter 15, so it does
    not inherit them, and shipping a console with a known 3.10 timeout bug in
    it to make a number look better would be a strange trade).
    """
    console_changed: dict[str, int] = {}
    backported: list[str] = []
    unchanged = 0

    for path in sorted(SRC.glob("*.py")):
        name = path.name
        delta = _diff_lines(BASELINE / name, path)
        if delta == 0:
            unchanged += 1
            continue
        if delta == -1:
            console_changed[name] = -1
            continue
        # A file that now matches chapter 15 exactly is a back-port, not a
        # change this chapter made for the browser.
        if (RELEASE / name).exists() and _diff_lines(RELEASE / name, path) == 0:
            backported.append(name)
            continue
        console_changed[name] = delta

    web = sorted((SRC / "web").glob("*.py"))
    web_lines = sum(len(p.read_text(encoding="utf-8").splitlines()) for p in web)
    front = sorted((ROOT / "frontend" / "src").rglob("*.ts*"))
    front_lines = sum(len(p.read_text(encoding="utf-8").splitlines()) for p in front)

    # `__main__.py` is reported apart from the rest, because it is not the
    # agent: it is the entry point, and what it grew is one subcommand that
    # starts a server. Counting that as "the agent changed" would make the
    # number look worse than the truth; hiding it would make it look better.
    entry = console_changed.pop("__main__.py", 0)

    print(f"baseline: {BASELINE.relative_to(STEPS.parent)}\n")
    print(f"  {unchanged:>4} top-level modules byte-identical to chapter 18")
    print(f"  {len(backported):>4} changed only by chapter 15's fixes: {', '.join(backported)}")
    print(f"  {len(console_changed):>4} agent modules changed for the console", end="")
    if console_changed:
        print(":")
        for name, delta in sorted(console_changed.items()):
            print(f"         {name}: {delta} line(s)")
    else:
        print("  <- the headline")
    print(f"\n  __main__.py: +{entry} lines, all of it the `serve` subcommand")
    print(f"  new backend  ({len(web)} modules): {web_lines} lines")
    print(f"  new frontend ({len(front)} files):   {front_lines} lines")


# ---------------------------------------------------------------------------
# order -- how bad was the fan-out, really
# ---------------------------------------------------------------------------


class _NaiveChannel:
    """The first version: one independent task per event per socket.

    Kept here rather than in git history because "it was wrong" and "it was
    wrong 4% of the time" are different claims, and only the second one tells
    you whether the bug would have been found by using the thing.
    """

    def __init__(self) -> None:
        self.sockets: list[object] = []

    def emit(self, payload: str) -> None:
        for socket in self.sockets:
            asyncio.get_running_loop().create_task(socket.send_text(payload))  # type: ignore[attr-defined]


class _Sink:
    def __init__(self, jitter: bool) -> None:
        self.seen: list[int] = []
        self._jitter = jitter

    async def send_text(self, data: str) -> None:
        # A real socket awaits at least once per send. `sleep(0)` is the
        # smallest honest model of that; the jittered version stands in for a
        # send that sometimes takes two turns of the loop instead of one,
        # which is what a real write buffer does.
        await asyncio.sleep(0)
        if self._jitter and json.loads(data)["n"] % 3 == 0:
            await asyncio.sleep(0)
        self.seen.append(json.loads(data)["n"])


def _inversions(seen: list[int]) -> int:
    return sum(1 for a, b in itertools.pairwise(seen) if b < a)


async def _order_trial(events: int, jitter: bool) -> tuple[int, int]:
    from minicodex.web.channel import Channel

    naive, sink = _NaiveChannel(), _Sink(jitter)
    naive.sockets.append(sink)
    for i in range(events):
        naive.emit(json.dumps({"n": i}))
    await asyncio.sleep(0.2)

    fixed_sink = _Sink(jitter)
    channel = Channel()
    channel.subscribe("t", fixed_sink)  # type: ignore[arg-type]
    for i in range(events):
        channel.emit("t", {"n": i})
    await asyncio.sleep(0.2)
    await channel.aclose()

    return _inversions(sink.seen), _inversions(fixed_sink.seen)


def order(trials: int = 20, events: int = 200) -> None:
    async def run() -> None:
        for jitter in (False, True):
            naive_total = fixed_total = 0
            naive_bad = 0
            for _ in range(trials):
                n, f = await _order_trial(events, jitter)
                naive_total += n
                fixed_total += f
                naive_bad += 1 if n else 0
            label = "uneven sends" if jitter else "even sends"
            print(
                f"  {label:>14}: naive {naive_total:>5} inversions "
                f"({naive_bad}/{trials} runs affected)   queue {fixed_total} inversions"
            )

    print(f"{trials} trials x {events} events, one subscriber\n")
    asyncio.run(run())
    print(
        "\n  An inversion is one event arriving after an event that was emitted later.\n"
        "\n"
        "  The result is not 'flaky', which is what was expected. It is 0 and then\n"
        "  100%. When every send costs exactly one turn of the loop, the naive\n"
        "  fan-out is perfectly ordered -- 0 inversions in every run -- because the\n"
        "  tasks were created in order and each finishes before the next resumes.\n"
        "  Make one send in three cost two turns instead of one, which is what a\n"
        "  write buffer that occasionally fills does, and every run reorders.\n"
        "\n"
        "  So this is a fault you cannot find by using the thing. A laptop, one\n"
        "  browser tab and a local model is the left column; a real network, two\n"
        "  tabs and a tool result the size of a file is the right one."
    )


# ---------------------------------------------------------------------------
# bundle -- what the rewrite cost the reader
# ---------------------------------------------------------------------------


def bundle() -> None:
    """Replacing one file that worked with a build that needs Node.

    The old console was 1,523 lines of HTML with inlined CSS and JS, no build
    step, and it ran from a `file://` URL. This one type-checks, escapes by
    construction, and needs `npm ci`. That is a real trade and the honest way
    to present it is with both numbers.
    """
    dist = ROOT / "frontend" / "dist"
    if not dist.is_dir():
        print("  no build yet: cd frontend && npm ci && npm run build")
        return

    assets = sorted(p for p in (dist / "assets").glob("*") if p.suffix in {".js", ".css"})
    total = sum(p.stat().st_size for p in assets) + (dist / "index.html").stat().st_size
    print("  built assets (source maps excluded -- they are not served to a viewer):")
    for path in assets:
        print(f"    {path.name:<28} {path.stat().st_size:>8,} bytes")
    print(f"    {'index.html':<28} {(dist / 'index.html').stat().st_size:>8,} bytes")
    print(f"    {'':<28} {total:>8,} bytes total")

    src = sorted((ROOT / "frontend" / "src").rglob("*"))
    src_files = [p for p in src if p.is_file()]
    print(
        f"\n  from {len(src_files)} source files, "
        f"{sum(len(p.read_text(encoding='utf-8').splitlines()) for p in src_files):,} lines"
    )
    modules = ROOT / "frontend" / "node_modules"
    installed = len(list(modules.glob("*"))) if modules.is_dir() else 0
    print(f"  node_modules to build it: {installed} entries")


# ---------------------------------------------------------------------------
# stream -- the one that needs a model
# ---------------------------------------------------------------------------

PROMPTS = [
    "Explain in two sentences what a rollout file is for.",
    "Name three things that can go wrong when resuming a session.",
    "What is the difference between a sandbox mode and an approval policy?",
    "Summarise why compaction can make a history bigger.",
]


async def _measure(prompt: str) -> tuple[float, float]:
    """Both numbers from one request: first token out, and item complete.

    One call rather than two, which matters more than it looks: the first
    version measured the arms in separate requests and the difference between
    them was smaller than the variance *between* requests -- so it reported the
    per-item arm as 0.19s **faster** than the per-token arm, which is not a
    slow result, it is an impossible one. Two numbers off one stream cannot
    disagree about which came first.

    - first `TextDelta` is what a token-streaming console could paint.
    - `Completed` is when `History` accepts the assistant item, which is when
      `RolloutWriter.append` fires, which is when this console emits. That is
      the whole shortcut, timed.
    """
    from minicodex.model import OPENAI_BASE_URL, ChatCompletionsModel, Completed, TextDelta

    llm = ChatCompletionsModel(
        base_url=OPENAI_BASE_URL,
        model="gpt-4o-mini",
        api_key=os.environ.get("OPENAI_API_KEY"),
        tools=[],
    )
    began = time.monotonic()
    first: float | None = None
    async for event in llm.stream([{"role": "user", "content": prompt}]):
        if isinstance(event, TextDelta) and event.text and first is None:
            first = time.monotonic() - began
        elif isinstance(event, Completed):
            break
    done = time.monotonic() - began
    return (first if first is not None else done), done


def stream(samples: int = 3) -> None:
    if not os.environ.get("OPENAI_API_KEY"):
        print("  stream needs OPENAI_API_KEY (a few cents); nothing was sent.")
        return

    async def run() -> None:
        rows = []
        for prompt in PROMPTS:
            for _ in range(samples):
                rows.append(await _measure(prompt))
        rows.sort(key=lambda r: r[1] - r[0])
        token_avg = sum(r[0] for r in rows) / len(rows)
        item_avg = sum(r[1] for r in rows) / len(rows)
        waits = [r[1] - r[0] for r in rows]
        print(f"  {len(rows)} requests, gpt-4o-mini\n")
        print(f"    first token visible : {token_avg:6.2f}s")
        print(f"    item complete       : {item_avg:6.2f}s")
        print(f"    the reader waits    : {item_avg - token_avg:6.2f}s longer on average")
        print(f"    best / worst        : {min(waits):6.2f}s / {max(waits):6.2f}s\n")
        print(
            "  That gap is what per-history-item streaming costs a reader, and it is\n"
            "  the whole argument for or against writing a second streaming path out\n"
            "  of the model client. It scales with how much the model says, not with\n"
            "  how fast it starts -- so the number to look at is the worst one."
        )

    asyncio.run(run())


SECTIONS = {"core-diff": core_diff, "order": order, "bundle": bundle, "stream": stream}


def main() -> None:
    if len(sys.argv) != 2 or sys.argv[1] not in SECTIONS:
        print(__doc__)
        raise SystemExit(2)
    name = sys.argv[1]
    print(f"== {name} ==\n")
    SECTIONS[name]()


if __name__ == "__main__":
    main()
