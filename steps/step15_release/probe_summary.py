"""What survives a summary, and what a model does after one.

Three measurements, all on a transcript with five facts planted in it that the
next turn provably needs:

    A  the user's constraint          "Python 3.9 ... no match statements"
    B  the file that must not change  "src/legacy.py"
    C  a flag discovered by failing   "-p no:randomly"
    D  work already finished          "src/net.py" written, tests pass
    E  the one thing still open       "CHANGELOG.md"

    1. retention  -- which of the five appear in the summary, naive vs structured
    2. behaviour  -- given only the compacted history, does the model redo D
    3. decay      -- how many survive after 1, 2, 3, 4, 5 generations

    python probe_summary.py [--samples 3] [--model gpt-4o-mini]
"""

from __future__ import annotations

import argparse
import asyncio
import os
import re

from minicodex import compaction_prompt
from minicodex.agent_types import ToolCall
from minicodex.compaction import compact, make_summariser, render_transcript
from minicodex.history import History
from minicodex.model import ChatCompletionsModel

NAIVE_PROMPT = "Summarise the conversation above so it can be continued later."

FACTS = {
    "A constraint": [r"3\.9", r"match statement"],
    "B do-not-touch": [r"legacy\.py"],
    "C discovered flag": [r"no:randomly"],
    "D done": [r"net\.py"],
    "E open": [r"CHANGELOG"],
}


def build_history() -> History:
    h = History()
    h.add_system_note("You are a coding agent working in /repo.")
    h.add_user(
        "Add retry logic to src/net.py. It has to stay compatible with Python 3.9, "
        "so no match statements. Do not touch src/legacy.py under any circumstances."
    )
    steps = [
        ("run_shell", '{"command": "ls src"}', "legacy.py\nnet.py\n__init__.py\n"),
        (
            "read_file",
            '{"path": "src/net.py"}',
            "import httpx\n\n\ndef fetch(url):\n    return httpx.get(url)\n",
        ),
        (
            "run_shell",
            '{"command": "pytest tests/test_net.py"}',
            "ERROR: plugin randomly reorders these tests and the fixture is "
            "order-dependent\nHINT: rerun with -p no:randomly\n",
        ),
        (
            "run_shell",
            '{"command": "pytest tests/test_net.py -p no:randomly"}',
            "2 passed in 0.31s\n",
        ),
        (
            "apply_patch",
            '{"path": "src/net.py"}',
            "wrote src/net.py: added retry() with exponential backoff and jitter\n",
        ),
        (
            "run_shell",
            '{"command": "pytest tests/test_net.py -p no:randomly"}',
            "5 passed in 0.44s\n",
        ),
    ]
    for index, (name, args, output) in enumerate(steps):
        call_id = f"call_{index}"
        text = ""
        if name == "apply_patch":
            text = (
                "The server sends Retry-After on 429, so I will use exponential "
                "backoff with jitter and honour that header rather than a fixed sleep."
            )
        h.add_assistant(text, [ToolCall(call_id, name, {}, args)])
        h.add_tool_result(call_id, output)
    h.add_assistant(
        "src/net.py now has retry() and the tests pass. Still to do: "
        "record the change in CHANGELOG.md."
    )
    h.add_user("Carry on.")
    return h


def found(summary: str) -> dict[str, bool]:
    return {
        label: any(re.search(p, summary, re.I) for p in patterns)
        for label, patterns in FACTS.items()
    }


def make_naive_summariser(model):
    async def summarise(transcript: str, previous: str | None) -> str:
        from minicodex.model import Completed, TextDelta

        scratch = History()
        scratch.add_user(f"{transcript}\n\n{NAIVE_PROMPT}")
        parts = []
        async for event in model.stream(scratch.to_wire()):
            if isinstance(event, TextDelta):
                parts.append(event.text)
            elif isinstance(event, Completed):
                pass
        return "".join(parts)

    return summarise


async def continuation(model, history: History) -> tuple[str, list[str]]:
    """What the model does next, given only what compaction left behind."""
    from minicodex.model import Completed, TextDelta, ToolCallDelta

    text, calls = [], []
    async for event in model.stream(history.to_wire()):
        if isinstance(event, TextDelta):
            text.append(event.text)
        elif isinstance(event, ToolCallDelta):
            calls.append(f"{event.name}({event.arguments})")
        elif isinstance(event, Completed):
            pass
    return "".join(text), calls


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default="gpt-4o-mini")
    ap.add_argument("--base-url", default="https://api.openai.com/v1")
    ap.add_argument("--samples", type=int, default=3)
    ap.add_argument("--generations", type=int, default=5)
    args = ap.parse_args()

    key = os.environ.get("OPENAI_API_KEY") if "openai.com" in args.base_url else None
    llm = ChatCompletionsModel(base_url=args.base_url, model=args.model, api_key=key)
    # The agent's tools deliberately stay attached for the continuation test:
    # "did it redo the work" is a question about tool calls.
    from minicodex.tools import TOOL_SCHEMAS

    tooled = ChatCompletionsModel(
        base_url=args.base_url, model=args.model, api_key=key, tools=TOOL_SCHEMAS
    )

    history = build_history()
    print(f"transcript: {len(history)} items, {len(render_transcript(history.items))} chars\n")

    # -- 1. retention ------------------------------------------------------
    print("=== 1. which planted facts survive, measured on the WHOLE compacted")
    print("       history -- A and B live in the protected prefix, so looking")
    print("       only at the summary text scores them as lost when they are not")
    print(f"{'prompt':<12} {'sample':<7} " + " ".join(f"{k:<17}" for k in FACTS))
    kept: dict[str, list[int]] = {"naive": [], "structured": []}
    structured_summaries: list[str] = []
    for label, summariser in (
        ("naive", make_naive_summariser(llm)),
        ("structured", make_summariser(llm)),
    ):
        for sample in range(args.samples):
            result = await compact(history, summarise=summariser, budget=200)
            hits = found(render_transcript(result.history.items))
            kept[label].append(sum(hits.values()))
            if label == "structured":
                structured_summaries.append(result.summary)
            marks = " ".join(f"{'yes' if hits[k] else 'NO':<17}" for k in FACTS)
            print(f"{label:<12} {sample:<7} {marks}")
    for label, counts in kept.items():
        print(f"  {label:<12} {sum(counts)}/{len(counts) * len(FACTS)} facts kept")

    # -- 2. behaviour ------------------------------------------------------
    print("\n=== 2. given only the compacted history, what does it do next")
    print("   (redoing the patch on src/net.py means the summary failed at 'Done')")
    for label, summariser in (
        ("naive", make_naive_summariser(llm)),
        ("structured", make_summariser(llm)),
    ):
        for sample in range(args.samples):
            result = await compact(history, summarise=summariser, budget=200)
            _, calls = await continuation(tooled, result.history)
            redid = any("net.py" in c and "apply_patch" in c for c in calls)
            print(f"{label:<12} {sample:<7} redid={redid!s:<6} {calls[:2]}")

    # -- 3. decay ----------------------------------------------------------
    print("\n=== 3. summarising the summary, N times over")
    print("   each generation gets a fresh block of unrelated work to summarise,")
    print("   so the older material is only ever reachable through the summary")
    summariser = make_summariser(llm)
    current = history
    for generation in range(1, args.generations + 1):
        result = await compact(current, summarise=summariser, budget=200)
        hits = found(render_transcript(result.history.items))
        print(
            f"  gen {generation}  {sum(hits.values())}/5  "
            + " ".join(k.split()[0] for k, v in hits.items() if not v)
            + ("  (all kept)" if all(hits.values()) else "  <- lost")
        )
        # Fresh filler work, so the next compaction has something new to drop
        # and the old facts survive only via the summary it just wrote.
        current = result.history
        for i in range(4):
            call_id = f"gen{generation}_{i}"
            current.add_assistant("", [ToolCall(call_id, "run_shell", {}, '{"command": "ls"}')])
            current.add_tool_result(call_id, f"unrelated output {i}\n" + "x" * 600)

    print("\n--- structured prompt, first sample, verbatim ---")
    print(structured_summaries[0][:1400])
    print(f"\n(compaction prompt: {len(compaction_prompt())} chars)")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
