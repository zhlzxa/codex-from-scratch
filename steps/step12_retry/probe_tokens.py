"""How wrong is chars/4?

Posts several histories of different shapes and compares the local estimate
with the `prompt_tokens` the server reports.  `max_tokens=1` because the
completion is irrelevant -- only the prompt side is being measured.

    python probe_tokens.py
"""

from __future__ import annotations

import argparse
import json
import os
import sys

import httpx
from probe_naive_cut import TOOLS, build_history

CHARS_PER_TOKEN = 4


def estimate(messages: list[dict]) -> int:
    """The obvious first estimator: count the characters, divide by four."""
    chars = 0
    for m in messages:
        chars += len(m.get("content") or "")
        for call in m.get("tool_calls") or []:
            chars += len(call["function"]["name"]) + len(call["function"]["arguments"])
    return chars // CHARS_PER_TOKEN


CASES: dict[str, list[dict]] = {
    "prose only": [{"role": "user", "content": "Explain what a tool call is, briefly."}],
    "long prose": [{"role": "user", "content": "The quick brown fox. " * 200}],
    "agent history": build_history(),
    "shell output": [
        {"role": "user", "content": "what is here"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "c1",
                    "type": "function",
                    "function": {"name": "run_shell", "arguments": '{"command": "ls -la"}'},
                }
            ],
        },
        {
            "role": "tool",
            "tool_call_id": "c1",
            "content": "\n".join(
                f"-rw-r--r--  1 me  staff   {i * 137:>6} Aug  6 12:0{i % 10} file_{i}.py"
                for i in range(40)
            ),
        },
    ],
    "json blob": [
        {"role": "user", "content": json.dumps({"k" + str(i): {"v": i} for i in range(80)})}
    ],
    "cjk": [{"role": "user", "content": "这是一个中文的工具调用说明。" * 40}],
    "source code": [
        {
            "role": "user",
            "content": "def f(x: int) -> int:\n    return x * 2 + 1\n\n" * 40,
        }
    ],
}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base-url", default="https://api.openai.com/v1")
    ap.add_argument("--model", default="gpt-4o-mini")
    ap.add_argument("--with-tools", action="store_true", help="send the tool schemas too")
    args = ap.parse_args()
    key = os.environ.get("OPENAI_API_KEY") if "openai.com" in args.base_url else None
    headers = {"Content-Type": "application/json"}
    if key:
        headers["Authorization"] = f"Bearer {key}"

    print(f"{'case':<16} {'chars':>7} {'est':>7} {'actual':>7} {'est/actual':>11}")
    print("-" * 54)
    for label, messages in CASES.items():
        body = {"model": args.model, "messages": messages, "stream": False, "max_tokens": 1}
        if args.with_tools:
            body["tools"] = TOOLS
        resp = httpx.post(
            f"{args.base_url}/chat/completions", json=body, headers=headers, timeout=120.0
        )
        payload = resp.json()
        if "error" in payload:
            print(f"{label:<16} {payload['error']['message'][:40]}")
            continue
        actual = payload["usage"]["prompt_tokens"]
        est = estimate(messages)
        chars = sum(len(m.get("content") or "") for m in messages)
        print(f"{label:<16} {chars:>7} {est:>7} {actual:>7} {est / actual:>10.2f}x")
    return 0


if __name__ == "__main__":
    sys.exit(main())
