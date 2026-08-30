#!/usr/bin/env python
"""Chapter 14's measurements.

    uv run python probe_eval.py <section>

    replay      offline  what a pre-chapter-14 recording can and cannot do
    trajectory  network  text assertions vs trajectory assertions, N samples
    ab          network  chapter 13's shipped sentence over the whole task set
    focus       network  one task, both arms, enough samples to see a small effect
    judge       network  an LLM judge, asked the same question twice
    cost        network  what one eval pass costs against what the suite costs
    leak        offline  what an eval workspace contains (F14-08)

Sections marked `network` need `OPENAI_API_KEY`, and `--provider ollama` uses
whatever is listening on localhost:11434.  Nothing here is a test; the tests
are in `tests/test_faults_ch14.py` and never touch the network.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import statistics
import sys
import time
from pathlib import Path
from typing import Any

from minicodex import system_prompt
from minicodex.evals import TASKS, Arm, Task, by_name, failures, regressions, run_task, table
from minicodex.model import OLLAMA_BASE_URL, OPENAI_BASE_URL, ChatCompletionsModel

HERE = Path(__file__).resolve().parent
SCRATCH = HERE / ".probe" / "ch14"

PROVIDERS = {
    "openai": (OPENAI_BASE_URL, "gpt-4o-mini"),
    "ollama": (OLLAMA_BASE_URL, "gemma4:31b-cloud"),
}

# The sentence chapter 13 shipped, measured there on one task with one
# provider.  This chapter's job is to find out what it does to the other five.
ASK_SENTENCE = (
    "If a request is genuinely ambiguous -- more than one reasonable "
    "interpretation, and picking wrong would waste real work -- ask one specific "
    "question before acting instead of guessing. Do not ask about anything you "
    "could find out yourself by reading the repository."
)


def build(provider: str, model: str | None = None):
    base, default = PROVIDERS[provider]

    def factory(tools: list[dict[str, Any]]) -> ChatCompletionsModel:
        return ChatCompletionsModel(
            base_url=base,
            model=model or default,
            api_key=os.environ.get("OPENAI_API_KEY") if provider == "openai" else None,
            tools=tools,
        )

    return factory


def instructions(with_sentence: bool) -> str:
    """The system message an eval run gets, with the one sentence on or off.

    `system.md` minus the permission block and minus the plan paragraph: an
    eval task set is built from `local_tools`, which has no `update_plan`, and
    chapter 11's paragraph is conditional on the tool being there (F05-10).
    Every arm runs `workspace-write` with everything approved, so the
    permission block is a constant, and a constant is not an arm.
    """
    text = system_prompt().rstrip()
    if not with_sentence:
        text = text.replace(ASK_SENTENCE, "").strip()
    return text


def workspace(tag: str) -> Path:
    path = SCRATCH / tag
    if path.exists():
        shutil.rmtree(path)
    path.mkdir(parents=True)
    return path


async def run_set(
    tasks: tuple[Task, ...],
    provider: str,
    *,
    samples: int,
    with_sentence: bool,
    label: str,
    verbose: bool = False,
) -> Arm:
    arm = Arm(label)
    factory = build(provider)
    text = instructions(with_sentence)
    for sample in range(samples):
        for task in tasks:
            result = await run_task(
                task,
                factory,
                workspace=workspace(f"{label}-{task.name}-{sample}"),
                instructions=text,
            )
            arm.results.append(result)
            if verbose:
                mark = "ok  " if result.ok else "FAIL"
                print(f"    {mark} {task.name}[{sample}]  {result.trajectory.describe()}")
                if not result.ok:
                    print(f"         {result.error or ', '.join(result.failed)}")
    return arm


# ---------------------------------------------------------------------------
# replay
# ---------------------------------------------------------------------------


def section_replay(args: argparse.Namespace) -> None:
    from minicodex.replay import ReplayError, load

    old = {
        "seq": 2,
        "kind": "response",
        "payload": {
            "turn": 0,
            "text": "",
            "finish_reason": "tool_calls",
            "tool_calls": [{"id": "call_1", "name": "apply_patch"}],
        },
    }
    request = {
        "seq": 1,
        "kind": "request",
        "payload": {"turn": 0, "attempt": 0, "messages": [{"role": "user", "content": "hi"}]},
    }
    path = SCRATCH / "old.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(request) + "\n" + json.dumps(old) + "\n",
        encoding="utf-8",
    )
    print("a recording in the format chapters -1 to 13 wrote:")
    print(f"  {json.dumps(old['payload']['tool_calls'])}")
    try:
        load(path)
    except ReplayError as exc:
        print(f"  load() -> {exc}")

    new = json.loads(json.dumps(old))
    new["payload"]["tool_calls"][0]["arguments"] = '{"edits":[{"path":"a.py"}]}'
    path.write_text(json.dumps(request) + "\n" + json.dumps(new) + "\n", encoding="utf-8")
    recording = load(path)
    print("\nthe same recording with chapter 14's one extra field:")
    print(f"  load() -> {recording.describe()}")
    print(f"  first call -> {recording.attempts[0].tool_calls[0]}")


# ---------------------------------------------------------------------------
# trajectory
# ---------------------------------------------------------------------------


async def section_trajectory(args: argparse.Namespace) -> None:
    """The same run, scored two ways."""
    task = by_name("add-function")
    factory = build(args.provider)
    phrases = ["I have added", "subtract", "successfully"]
    hits = dict.fromkeys(phrases, 0)
    trajectory_ok = 0
    answers = []
    for sample in range(args.samples):
        result = await run_task(
            task,
            factory,
            workspace=workspace(f"traj-{sample}"),
            instructions=instructions(True),
        )
        answers.append(result.trajectory.final_text)
        for phrase in phrases:
            if phrase.lower() in result.trajectory.final_text.lower():
                hits[phrase] += 1
        if result.ok:
            trajectory_ok += 1
        print(f"  sample {sample}: {result.trajectory.describe()}")
        print(f"    answer opens: {result.trajectory.final_text[:70]!r}")
    print(f"\n  trajectory checks (file on disk):   {trajectory_ok}/{args.samples}")
    for phrase, count in hits.items():
        print(
            f"  answer contains {phrase!r}:{' ' * max(1, 18 - len(phrase))}{count}/{args.samples}"
        )
    lengths = [len(a) for a in answers]
    print(f"  answer length: min {min(lengths)} max {max(lengths)}")


# ---------------------------------------------------------------------------
# ab
# ---------------------------------------------------------------------------


async def section_ab(args: argparse.Namespace) -> None:
    print(f"== {args.provider} ==  {args.samples} sample(s) per task per arm")
    without = await run_set(
        TASKS,
        args.provider,
        samples=args.samples,
        with_sentence=False,
        label="without",
        verbose=args.verbose,
    )
    with_it = await run_set(
        TASKS,
        args.provider,
        samples=args.samples,
        with_sentence=True,
        label="with",
        verbose=args.verbose,
    )
    print()
    print(table([without, with_it], TASKS))
    worse = regressions(without, with_it, TASKS)
    print("\n  made worse by the sentence:", ", ".join(worse) if worse else "(none)")
    print("\n  failures without the sentence:")
    print("\n".join(failures(without)) or "    (none)")
    print("\n  failures with the sentence:")
    print("\n".join(failures(with_it)) or "    (none)")


async def section_focus(args: argparse.Namespace) -> None:
    """One task, both arms, many samples.

    The `ab` section runs six tasks three times each, which is enough to say
    "no task collapsed" and not enough to see a one-in-five effect.  This is
    the follow-up the first `trajectory` run forced: at five samples,
    `add-function` produced a clarifying question once, and that task is not
    ambiguous.
    """
    names = (args.tasks or "add-function").split(",")
    tasks = tuple(by_name(name) for name in names)
    without = await run_set(
        tasks,
        args.provider,
        samples=args.samples,
        with_sentence=False,
        label="without",
        verbose=args.verbose,
    )
    with_it = await run_set(
        tasks,
        args.provider,
        samples=args.samples,
        with_sentence=True,
        label="with",
        verbose=args.verbose,
    )
    print()
    print(table([without, with_it], tasks))
    for arm in (without, with_it):
        asked = sum(1 for r in arm.results if "?" in r.trajectory.final_text)
        edited = sum(1 for r in arm.results if r.trajectory.called("apply_patch"))
        print(f"  {arm.name}: asked a question {asked}/{len(arm.results)}, edited {edited}")


# ---------------------------------------------------------------------------
# judge
# ---------------------------------------------------------------------------

JUDGE_PROMPT = """You are grading two answers to the same question.

Question: {question}

Answer A:
{a}

Answer B:
{b}

Which answer is better? Reply with exactly one character: A or B."""

TERSE = "calc.py defines add and multiply."
VERBOSE = """Certainly! I took a careful look at `calc.py` and can confirm the
following. The module defines two public functions:

1. **`add(a, b)`** -- returns the sum of its two arguments.
2. **`multiply(a, b)`** -- returns the product of its two arguments.

Both are straightforward implementations with no side effects. Let me know if
you would like me to add type hints or docstrings!"""

WRONG_CONFIDENT = """I examined calc.py thoroughly. It defines three functions:
add, multiply and divide, all fully tested and documented."""


async def _ask_judge(client: Any, question: str, a: str, b: str) -> str:
    from minicodex.history import History

    history = History()
    history.add_user(JUDGE_PROMPT.format(question=question, a=a, b=b))
    text = []
    async for event in client.stream(history.to_wire()):
        if getattr(event, "text", None):
            text.append(event.text)
    return "".join(text).strip()[:1].upper()


async def section_judge(args: argparse.Namespace) -> None:
    client = build(args.provider)([])
    question = "which functions does calc.py define?"

    pairs = [
        ("terse-correct vs verbose-correct", TERSE, VERBOSE),
        ("terse-correct vs verbose-wrong", TERSE, WRONG_CONFIDENT),
    ]
    for label, first, second in pairs:
        forward = [await _ask_judge(client, question, first, second) for _ in range(args.samples)]
        swapped = [await _ask_judge(client, question, second, first) for _ in range(args.samples)]
        # "A" in the forward run and "B" in the swapped run are the same answer.
        picked_first = forward.count("A") + swapped.count("B")
        print(f"\n  {label}")
        print(f"    as A/B: {forward}")
        print(f"    as B/A: {swapped}")
        print(f"    first answer preferred {picked_first}/{2 * args.samples}")
        agree = sum(1 for f, s in zip(forward, swapped, strict=True) if (f == "A") == (s == "B"))
        print(f"    verdict survives swapping the order: {agree}/{args.samples}")


# ---------------------------------------------------------------------------
# cost
# ---------------------------------------------------------------------------


async def section_cost(args: argparse.Namespace) -> None:
    began = time.monotonic()
    arm = await run_set(
        TASKS, args.provider, samples=1, with_sentence=True, label="cost", verbose=True
    )
    wall = time.monotonic() - began
    ok, total = arm.total
    turns = [r.trajectory.turns for r in arm.results]
    print(f"\n  {len(TASKS)} tasks, one sample each, {args.provider}")
    print(f"  wall clock      {wall:.1f}s")
    print(f"  passed          {ok}/{total}")
    print(f"  turns           total {sum(turns)}, median {statistics.median(turns)}")
    print(f"  model calls     {sum(turns)} (one per turn, plus retries)")


# ---------------------------------------------------------------------------
# leak
# ---------------------------------------------------------------------------


def section_leak(args: argparse.Namespace) -> None:
    """What an agent running an eval task can actually see."""
    from minicodex.evals import declared_files

    path = workspace("leak")
    asyncio.run(_leak(path))
    print(f"\n  declared by the task set: {sorted(declared_files())}")


async def _leak(path: Path) -> None:
    from minicodex.evals import _materialise, _snapshot

    task = by_name("add-function")
    _materialise(path, task.files)
    print(f"  the agent's root contains: {sorted(_snapshot(path))}")
    print(f"  the repository is at:      {HERE}")
    print("  ...and is not reachable through `paths.resolve()` from that root.")


SECTIONS = {
    "replay": section_replay,
    "trajectory": section_trajectory,
    "ab": section_ab,
    "focus": section_focus,
    "judge": section_judge,
    "cost": section_cost,
    "leak": section_leak,
}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("section", choices=sorted(SECTIONS))
    parser.add_argument("--provider", choices=sorted(PROVIDERS), default="openai")
    parser.add_argument("--samples", type=int, default=3)
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument("--tasks", default=None, help="comma-separated subset")
    args = parser.parse_args(argv)

    section = SECTIONS[args.section]
    if asyncio.iscoroutinefunction(section):
        asyncio.run(section(args))
    else:
        section(args)
    return 0


if __name__ == "__main__":
    sys.exit(main())
