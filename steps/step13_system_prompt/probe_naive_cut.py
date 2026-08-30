"""What actually happens when you drop the oldest half of a conversation.

Builds a realistic agent history (system note, user, then four assistant/tool
round trips), cuts it three different naive ways, and posts each one to a real
server.  Prints the status and the first part of the body.

    python probe_naive_cut.py                # OpenAI, needs OPENAI_API_KEY
    python probe_naive_cut.py --base-url http://localhost:11434/v1 --model gemma4:31b-cloud
"""

from __future__ import annotations

import argparse
import json
import os
import sys

import httpx


def build_history() -> list[dict]:
    """Eight messages: one system, one user, then three call/result pairs."""
    messages: list[dict] = [
        {"role": "system", "content": "You are a coding agent working in /repo."},
        {"role": "user", "content": "Find out which Python version this project supports."},
    ]
    steps = [
        ("call_a1", "run_shell", '{"command": "ls"}', "pyproject.toml\nsrc\ntests\n"),
        ("call_b2", "read_file", '{"path": "pyproject.toml"}', 'requires-python = ">=3.10"\n'),
        ("call_c3", "run_shell", '{"command": "python --version"}', "Python 3.13.0\n"),
    ]
    for call_id, name, args, output in steps:
        messages.append(
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": call_id,
                        "type": "function",
                        "function": {"name": name, "arguments": args},
                    }
                ],
            }
        )
        messages.append({"role": "tool", "tool_call_id": call_id, "content": output})
    return messages


TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "run_shell",
            "description": "Run a shell command.",
            "parameters": {
                "type": "object",
                "properties": {"command": {"type": "string"}},
                "required": ["command"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read a file.",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
            },
        },
    },
]


def post(messages: list[dict], *, base_url: str, model: str, key: str | None) -> tuple[int, str]:
    headers = {"Content-Type": "application/json"}
    if key:
        headers["Authorization"] = f"Bearer {key}"
    body = {"model": model, "messages": messages, "tools": TOOLS, "stream": False}
    resp = httpx.post(f"{base_url}/chat/completions", json=body, headers=headers, timeout=120.0)
    return resp.status_code, resp.text


def shape(messages: list[dict]) -> str:
    """A one-line picture of the message list, so the cut is visible."""
    out = []
    for m in messages:
        if m["role"] == "assistant" and m.get("tool_calls"):
            out.append("A[" + ",".join(c["id"] for c in m["tool_calls"]) + "]")
        elif m["role"] == "tool":
            out.append("T[" + m["tool_call_id"] + "]")
        else:
            out.append(m["role"][0].upper())
    return " ".join(out)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base-url", default="https://api.openai.com/v1")
    ap.add_argument("--model", default="gpt-4o-mini")
    args = ap.parse_args()
    key = os.environ.get("OPENAI_API_KEY") if "openai.com" in args.base_url else None

    full = build_history()

    cuts = {
        "F06-01 drop oldest half": full[len(full) // 2 :],
        "F06-03 keep last 4": full[-4:],
        "F06-02 cut at a token count (here: 5 messages)": full[-5:],
        "(control) uncut": full,
    }

    for label, messages in cuts.items():
        print(f"\n=== {label}")
        print(f"    {shape(messages)}")
        status, text = post(messages, base_url=args.base_url, model=args.model, key=key)
        print(f"    HTTP {status}")
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            print(f"    {text[:300]}")
            continue
        if "error" in payload:
            print(f"    error.message: {payload['error']['message'][:400]}")
        else:
            choice = payload["choices"][0]["message"]
            answer = choice.get("content") or ""
            calls = [c["function"]["name"] for c in choice.get("tool_calls") or []]
            print(f"    content: {answer[:200]!r}")
            print(f"    tool_calls: {calls}")
            print(f"    usage: {payload.get('usage', {}).get('prompt_tokens')} prompt tokens")
    return 0


if __name__ == "__main__":
    sys.exit(main())
