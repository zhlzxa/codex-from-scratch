"""Which cut points does a real server accept?

Takes the same eight-message history and cuts it at every index, then posts each
suffix.  The answer is not "keep N messages" and not "keep N tokens" -- it is a
property of the shape at the cut.

    python probe_cut_points.py
"""

from __future__ import annotations

import argparse
import json
import os
import sys

import httpx
from probe_naive_cut import TOOLS, build_history, shape


def post(messages: list[dict], *, base_url: str, model: str, key: str | None) -> tuple[int, str]:
    headers = {"Content-Type": "application/json"}
    if key:
        headers["Authorization"] = f"Bearer {key}"
    body = {"model": model, "messages": messages, "tools": TOOLS, "stream": False}
    resp = httpx.post(f"{base_url}/chat/completions", json=body, headers=headers, timeout=120.0)
    return resp.status_code, resp.text


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base-url", default="https://api.openai.com/v1")
    ap.add_argument("--model", default="gpt-4o-mini")
    args = ap.parse_args()
    key = os.environ.get("OPENAI_API_KEY") if "openai.com" in args.base_url else None

    full = build_history()
    print(f"full history: {shape(full)}\n")
    print(f"{'cut at':>7}  {'kept shape':<44} {'status':<7} {'note'}")
    print("-" * 100)

    for i in range(len(full)):
        kept = full[i:]
        status, text = post(kept, base_url=args.base_url, model=args.model, key=key)
        note = ""
        payload = json.loads(text)
        if "error" in payload:
            note = payload["error"]["message"][:52]
        else:
            note = (payload["choices"][0]["message"].get("content") or "")[:52].replace("\n", " ")
        print(f"{i:>7}  {shape(kept):<44} {status:<7} {note}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
