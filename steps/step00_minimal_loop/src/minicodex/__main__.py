"""Command line entry point."""

from __future__ import annotations

import argparse
import asyncio
import platform
import sys

from minicodex import __version__
from minicodex.agent import Agent
from minicodex.model import DEFAULT_BASE_URL, DEFAULT_MODEL, OllamaModel
from minicodex.recorder import Recorder
from minicodex.tools import DEFAULT_TOOLS, TOOL_SCHEMAS


async def _ask(question: str, *, base_url: str, model: str) -> int:
    recorder = Recorder()
    llm = OllamaModel(base_url=base_url, model=model, tools=TOOL_SCHEMAS)
    agent = Agent(llm, DEFAULT_TOOLS, recorder=recorder)

    result = await agent.run(question)

    print(result.final_text or "(no answer)")
    print(f"\n[{model} | {result.stop_reason} after {result.turns_used} turn(s)]")
    print(f"[transcript: {recorder.path}]")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="minicodex")
    parser.add_argument(
        "--version",
        action="store_true",
        help="print the version and enough environment detail to file a bug report",
    )
    sub = parser.add_subparsers(dest="command")

    ask = sub.add_parser("ask", help="ask a question that requires reading a file")
    ask.add_argument("question")
    ask.add_argument("--base-url", default=DEFAULT_BASE_URL)
    ask.add_argument("--model", default=DEFAULT_MODEL)

    stub = sub.add_parser("serve-stub", help="replay recorded Ollama responses on a local port")
    stub.add_argument("--port", type=int, default=11435)

    args = parser.parse_args(argv)

    if args.version:
        print(f"minicodex {__version__}")
        print(f"python    {platform.python_version()} ({sys.platform})")
        return 0

    if args.command == "ask":
        return asyncio.run(_ask(args.question, base_url=args.base_url, model=args.model))

    if args.command == "serve-stub":
        from minicodex.stub_ollama import serve

        serve(args.port)
        return 0

    parser.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
