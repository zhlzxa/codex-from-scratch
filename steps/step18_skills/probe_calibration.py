"""Does the previous turn's reported usage predict this turn's?

A raw estimate is wrong by a factor that depends on what is in the history --
prose tokenises at ~4.2 chars/token, JSON at ~1.8.  No constant fixes that.
The question is whether the number the server already told us is a usable
correction for the next turn.

Three estimators are compared as the history grows:

    v1   chars/4 over message content and tool-call arguments
    v2   v1 plus the two inputs v1 forgets: the tool schemas, which are sent
         on every request, and a per-message framing cost
    v2c  v2 corrected by the ratio observed on the previous turn

    python probe_calibration.py
"""

from __future__ import annotations

import argparse
import json
import os
import sys

import httpx
from probe_naive_cut import TOOLS

CHARS_PER_TOKEN = 4.0
PER_MESSAGE_TOKENS = 4  # role, separators, and the framing the server adds


def estimate_v1(messages: list[dict], tools: list[dict]) -> float:
    chars = 0
    for m in messages:
        chars += len(m.get("content") or "")
        for call in m.get("tool_calls") or []:
            chars += len(call["function"]["name"]) + len(call["function"]["arguments"])
    return chars / CHARS_PER_TOKEN


def estimate_v2(messages: list[dict], tools: list[dict]) -> float:
    base = estimate_v1(messages, tools)
    base += PER_MESSAGE_TOKENS * len(messages)
    base += len(json.dumps(tools)) / CHARS_PER_TOKEN
    return base


OUTPUTS = [
    "pyproject.toml\nsrc\ntests\nREADME.md\nuv.lock\n",
    json.dumps({"deps": {f"pkg{i}": f">=1.{i}" for i in range(30)}}),
    "\n".join(f"src/minicodex/mod_{i}.py:{i * 7}: def handler_{i}(x):" for i in range(30)),
    "Traceback (most recent call last):\n" + '  File "a.py", line 3, in f\n' * 20,
    "这是一个中文的说明文件，描述了工具的用途。\n" * 15,  # noqa: RUF001 -- CJK punctuation is the measurement
    "def handler(x: int) -> int:\n    return x * 2\n\n" * 25,
    "\n".join(f"{i:>4}  commit {i:040x}  fix: something in module {i}" for i in range(25)),
    "PASSED tests/test_shell.py::test_one\n" * 30,
]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base-url", default="https://api.openai.com/v1")
    ap.add_argument("--model", default="gpt-4o-mini")
    args = ap.parse_args()
    key = os.environ.get("OPENAI_API_KEY") if "openai.com" in args.base_url else None
    headers = {"Content-Type": "application/json"}
    if key:
        headers["Authorization"] = f"Bearer {key}"

    messages: list[dict] = [
        {"role": "system", "content": "You are a coding agent working in /repo."},
        {"role": "user", "content": "Summarise this project's dependencies."},
    ]
    ratio = 1.0

    print(
        f"{'turn':>4} {'actual':>7} | {'v1':>7} {'err':>7} | {'v2':>7} {'err':>7} "
        f"| {'v2c':>7} {'err':>7}"
    )
    print("-" * 70)
    errs: dict[str, list[float]] = {"v1": [], "v2": [], "v2c": []}

    for turn, output in enumerate(OUTPUTS):
        v1 = estimate_v1(messages, TOOLS)
        v2 = estimate_v2(messages, TOOLS)
        v2c = v2 * ratio

        body = {
            "model": args.model,
            "messages": messages,
            "tools": TOOLS,
            "stream": False,
            "max_tokens": 64,
        }
        resp = httpx.post(
            f"{args.base_url}/chat/completions", json=body, headers=headers, timeout=120.0
        )
        payload = resp.json()
        if "error" in payload:
            print(payload["error"]["message"][:200])
            return 1
        actual = payload["usage"]["prompt_tokens"]

        row = {"v1": v1, "v2": v2, "v2c": v2c}
        for name, value in row.items():
            errs[name].append((value - actual) / actual)
        print(
            f"{turn:>4} {actual:>7} | {v1:>7.0f} {(v1 - actual) / actual:>6.0%} "
            f"| {v2:>7.0f} {(v2 - actual) / actual:>6.0%} "
            f"| {v2c:>7.0f} {(v2c - actual) / actual:>6.0%}"
        )

        ratio = actual / v2 if v2 else 1.0

        call_id = f"call_{turn}"
        messages.append(
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": call_id,
                        "type": "function",
                        "function": {
                            "name": "run_shell",
                            "arguments": json.dumps({"command": f"step {turn}"}),
                        },
                    }
                ],
            }
        )
        messages.append({"role": "tool", "tool_call_id": call_id, "content": output})

    print("-" * 70)
    for name, values in errs.items():
        worst = max(values, key=abs)
        under = min(values)
        print(f"{name:>4}  worst {worst:>7.0%}   most negative {under:>7.0%}")
    print(
        "\nNegative means underestimating, which is the direction that overflows"
        "\nthe window before compaction ever fires."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
