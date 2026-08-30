"""Command line entry point."""

from __future__ import annotations

import argparse
import asyncio
import os
import platform
import sys
import time
from dataclasses import replace
from pathlib import Path
from typing import Any

from minicodex import __version__, system_prompt
from minicodex.agent import Wiring
from minicodex.agent_types import ToolSet
from minicodex.agents_md import AgentsMdWatcher
from minicodex.approval import AllowAll, CliApprover, Session, permissions_block
from minicodex.compaction import make_summariser
from minicodex.composition import sub_context, top_level_tools, watching, with_remote_tools
from minicodex.mcp import McpError
from minicodex.model import OLLAMA_BASE_URL, OPENAI_BASE_URL, ChatCompletionsModel
from minicodex.plan import PLAN_INSTRUCTIONS, TaskPlan, unfinished_note
from minicodex.policy import APPROVAL_POLICIES, SANDBOX_MODES
from minicodex.recorder import Recorder
from minicodex.registry import (
    McpRegistry,
    connect,
    elicitation_handler,
    load_config,
)
from minicodex.retry import ModelFailed
from minicodex.rollout import (
    DEFAULT_DIR,
    RolloutError,
    RolloutWriter,
    SessionMeta,
    environment_note,
    fork,
    interrupted_note,
    list_sessions,
    new_session_id,
    read_rollout,
    resolve,
    rollout_path,
)
from minicodex.rules import DEFAULT_RULES_PATH, RuleStore
from minicodex.subagent import describe_children
from minicodex.tools import TOOL_SCHEMAS, tool_context

PROVIDERS = {
    "ollama": (OLLAMA_BASE_URL, "gemma4:31b-cloud"),
    "openai": (OPENAI_BASE_URL, "gpt-4o-mini"),
}


def _instructions(session: Session, tools: ToolSet | None = None) -> str:
    """The system message: what the agent is, then what it may currently do.

    Permission state goes last, and that is not a layout preference.  It is the
    only part of this string that changes during a session -- `request_permissions`
    rewrites it -- and providers cache a prompt by its prefix. Volatile content
    near the top invalidates the cache on the turn it changes. Chapter 13 has
    the measurements; the ordering costs nothing to get right now (F13-07).

    The plan paragraph is here rather than in `prompts/system.md` because it is
    conditional on the tool existing (F05-10: a prompt that names a tool the
    configuration does not have gets the model to call something that is not
    there, measured at 2/3 in chapter 5), and because without it the tool is
    largely unused -- 2 of 5 runs never called it, chapter 11 §4.
    """
    can_request = any(tool["function"]["name"] == "request_permissions" for tool in TOOL_SCHEMAS)
    block = permissions_block(session, can_request=can_request)
    parts = [system_prompt().rstrip()]
    if tools is not None and "update_plan" in tools.handlers:
        parts.append(PLAN_INSTRUCTIONS)
    parts.append(block)
    return "\n\n".join(parts)


async def _ask(
    question: str,
    *,
    provider: str,
    base_url: str | None,
    model: str | None,
    session: Session,
    context_window: int | None,
    resume: str | None,
    session_dir: Path,
    mcp_config: Path | None,
) -> int:
    default_url, default_model = PROVIDERS[provider]
    recorder = Recorder()

    def make_model(tools: list[dict[str, Any]]) -> ChatCompletionsModel:
        """One client per tool list.

        A sub-agent is shown a different set of tools from its parent, and a
        chat-completions client carries its tool list, so "the model" is not
        one object in a program that has sub-agents.  A factory rather than a
        `replace()` on the parent's client, because the child must not share
        the list object the registry mutates (chapter 9).
        """
        return ChatCompletionsModel(
            base_url=base_url or default_url,
            model=model or default_model,
            # Read from the environment, never from a flag: a key in argv shows
            # up in shell history and in `ps`.
            api_key=os.environ.get("OPENAI_API_KEY") if provider == "openai" else None,
            tools=tools,
        )

    root = Path.cwd().resolve()
    # The parent's own shell object, created here and handed to the sub-agent
    # context, so that a child can be seeded from where the parent is standing.
    context = tool_context(root=root, session=session)
    wiring = Wiring(
        recorder=recorder,
        context_window=context_window,
        # The one place in the program that has a terminal.  A backoff that
        # says nothing is a program that looks hung, and the measured wait a
        # real provider asked for was 46 seconds.
        announce=print,
        # The summariser shares the client, and therefore the provider and the
        # key, but not the tools: `make_summariser` builds its own history.
        # Bound below, once `llm` exists -- `replace()` rather than a second
        # `Wiring`, so there is still exactly one object to pass down.
        summariser=None,
    )
    current = SessionMeta(
        session_id=new_session_id(),
        created=time.time(),
        cwd=os.getcwd(),
        provider=provider,
        model=model or default_model,
        sandbox_mode=session.mode,
        approval_policy=session.policy,
    )
    sub_ctx = sub_context(
        build_model=make_model,
        root=root,
        session=session,
        # The parent's own shell, so a child starts in the directory the parent
        # is standing in rather than the one the process started in.
        parent_shell=context.shell,
        wiring=wiring,
        sessions_dir=session_dir,
        parent_session_id=current.session_id,
        provider=provider,
        model=model or default_model,
        announce=print,
    )
    # One plan per run, created here and handed to two places: the tool that
    # edits it, and the loop's stop check that reads it.  Nothing else in the
    # program holds one -- a sub-agent has a task, not a plan (chapter 10).
    task_plan = TaskPlan()
    tools = top_level_tools(root, session, sub_ctx, plan=task_plan)

    # The registry owns the whole tool list, this project's own tools included,
    # because `llm.tools` is that same list object: `tool_search` reveals a
    # schema by appending to it, and the next request picks the change up
    # without anything having to be rebuilt or re-passed.
    registry = McpRegistry(local=tools.schemas)
    clients = []
    if mcp_config is not None:
        clients = await connect(
            load_config(mcp_config),
            registry,
            handlers={"elicitation/create": elicitation_handler(session)},
        )
        for name, reason in registry.failures.items():
            print(f"[mcp: {name} unavailable -- {reason}]", file=sys.stderr)
        for client in clients:
            offered = sum(1 for r in registry.registrations.values() if r.client is client)
            print(f"[mcp: {client.config.name} connected, {offered} tool(s)]")
        if registry.deferred:
            print(f"[mcp: {len(registry.deferred)} tool(s) deferred behind tool_search]")

    tools = watching(with_remote_tools(tools, registry), task_plan)
    llm = make_model(tools.schemas)
    if context_window:
        wiring = replace(wiring, summariser=make_summariser(llm))
        sub_ctx = replace(sub_ctx, wiring=wiring)

    resume_from = None
    if resume is not None:
        try:
            path = resolve(resume, session_dir)
        except RolloutError as exc:
            print(exc, file=sys.stderr)
            return 1
        loaded = read_rollout(path)
        resume_from, dropped = loaded.history()
        if loaded.truncated_at is not None:
            print(f"[session file damaged from line {loaded.truncated_at}; using what precedes it]")
        if dropped:
            resume_from.add_system_note(interrupted_note(dropped))
            print(f"[resumed: {dropped} incomplete message(s) discarded]")
        note = environment_note(loaded.meta, current)
        if note is not None:
            resume_from.add_system_note(note)
            print("[environment changed since this session was recorded]")
        current = replace(current, forked_from=loaded.meta.session_id)
        # A resumed session is written to a *new* file rather than appended to
        # the old one.  Appending would mean the recovered prefix and the
        # discarded tail share a file, so the next recovery would have to
        # rediscover which of the two it was looking at.
        print(f"[resumed {len(resume_from)} message(s) from {path}]")

    writer = RolloutWriter(rollout_path(current.session_id, session_dir), current)
    # The same two objects a sub-agent is built from, one level up.  Before
    # interlude B this call passed seven keyword arguments and the sub-agent's
    # passed three, and no test compared them because nothing put them next to
    # each other.
    # Bound to *this* run's shell, not the child's -- see `Wiring.agent`'s
    # docstring on why a sub-agent gets no watcher of its own.
    agents_watcher = AgentsMdWatcher(root)
    agent = wiring.agent(
        llm,
        tools,
        instructions=_instructions(session, tools),
        rollout=writer,
        resume_from=resume_from,
        on_stop=unfinished_note(task_plan),
        on_turn_start=lambda: agents_watcher.refresh(Path(context.shell.cwd)),
    )

    try:
        result = await agent.run(question)
    except ModelFailed as exc:
        # The one place a provider failure becomes something a person reads.
        # Before this, it was a twenty-line traceback ending in a JSON blob --
        # for a fault whose whole remedy is usually one sentence long.
        print(f"\n{exc}", file=sys.stderr)
        return 1
    finally:
        # Servers are subprocesses this process started, and chapter 2 already
        # paid for the lesson about leaving those behind (F02-08).  In a
        # `finally`, because the interesting exits are the ones that were not
        # planned.
        for client in clients:
            await client.close()
        # Chapter 7 takes an `O_EXCL` lock next to the session file and this
        # line used to sit at the very end of the function, so every crash left
        # one behind.  `RolloutWriter` has had `__enter__`/`__exit__` since the
        # chapter that introduced it, and `run_task` has released in a
        # `finally` since chapter 10 -- the correct pattern already existed in
        # the repository, one module over, exactly like chapter 11's
        # `SYSTEMROOT`.
        writer.release()

    print(result.final_text or "(no answer)")
    if task_plan.steps:
        # Printed after the answer and before the machine lines, because it is
        # the one thing on screen the model did not write.  A run whose plan
        # still has open steps says so here even when the answer does not.
        print()
        print(task_plan.render())
    print(f"\n[{llm.model} | {result.stop_reason} after {result.turns_used} turn(s)]")
    print(f"[{task_plan.describe()}]")
    print(f"[{session.describe()}]")
    for event in result.compactions:
        print(f"[{event.describe()}]")
    print(f"[tokens: {agent.calibration.describe()}]")
    print(f"[transcript: {recorder.path}]")
    for line in describe_children(sub_ctx.children):
        print(line)
    print(f"[session: {writer.path}  (resume with: minicodex ask ... --resume last)]")
    return 0


def _add_permission_flags(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--sandbox-mode",
        choices=SANDBOX_MODES,
        default="read-only",
        help="what the agent may do without anyone being asked (default: read-only)",
    )
    parser.add_argument(
        "--approval-policy",
        choices=APPROVAL_POLICIES,
        default="on-request",
        help="what happens to everything else (default: on-request)",
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="approve everything without asking. For scripts you have read.",
    )


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
    ask.add_argument("--provider", choices=sorted(PROVIDERS), default="ollama")
    ask.add_argument("--base-url", default=None)
    ask.add_argument("--model", default=None)
    ask.add_argument(
        "--context-window",
        type=int,
        default=None,
        help=(
            "size of the model's context window in tokens. Compaction is off "
            "without it: nothing in this program can discover the number, and "
            "a guessed one is a silently wrong budget"
        ),
    )
    ask.add_argument(
        "--resume",
        default=None,
        metavar="SESSION",
        help="continue a previous session: its id, its path, or `last`",
    )
    ask.add_argument("--session-dir", type=Path, default=DEFAULT_DIR)
    ask.add_argument(
        "--mcp",
        type=Path,
        default=None,
        metavar="CONFIG",
        help=(
            "JSON file listing MCP servers to start: "
            '{"servers": {"notes": {"command": ["python", "server.py"]}}}'
        ),
    )
    _add_permission_flags(ask)

    sessions = sub.add_parser("sessions", help="list saved sessions, newest first")
    sessions.add_argument("--session-dir", type=Path, default=DEFAULT_DIR)

    fork_cmd = sub.add_parser("fork", help="branch a new session from an old one")
    fork_cmd.add_argument("session", help="session id, path, or `last`")
    fork_cmd.add_argument(
        "--upto",
        type=int,
        default=None,
        help="keep only the first N messages (see `minicodex sessions` for the count)",
    )
    fork_cmd.add_argument("--session-dir", type=Path, default=DEFAULT_DIR)

    stub = sub.add_parser("serve-stub", help="replay recorded Ollama responses on a local port")
    stub.add_argument("--port", type=int, default=11435)

    sub.add_parser("rules", help="list the approvals this project has remembered")

    forget = sub.add_parser("forget", help="revoke a remembered approval by its number")
    forget.add_argument("index", type=int)

    args = parser.parse_args(argv)

    if args.version:
        print(f"minicodex {__version__}")
        print(f"python    {platform.python_version()} ({sys.platform})")
        return 0

    if args.command == "sessions":
        return _list_sessions(args.session_dir)

    if args.command == "fork":
        return _fork(args.session, args.upto, args.session_dir)

    if args.command == "rules":
        return _list_rules()

    if args.command == "forget":
        return _forget_rule(args.index)

    if args.command == "ask":
        session = Session(
            mode=args.sandbox_mode,
            policy=args.approval_policy,
            rules=RuleStore(Path(DEFAULT_RULES_PATH)),
            approver=AllowAll() if args.yes else CliApprover(),
        )
        try:
            configs_ok = args.mcp is None or load_config(args.mcp)
        except (OSError, ValueError, McpError) as exc:
            print(f"--mcp: {exc}", file=sys.stderr)
            return 1
        del configs_ok
        return asyncio.run(
            _ask(
                args.question,
                provider=args.provider,
                base_url=args.base_url,
                model=args.model,
                session=session,
                context_window=args.context_window,
                resume=args.resume,
                session_dir=args.session_dir,
                mcp_config=args.mcp,
            )
        )

    if args.command == "serve-stub":
        from minicodex.stub import serve

        serve(args.port)
        return 0

    parser.print_help()
    return 0


def _list_sessions(directory: Path) -> int:
    rollouts = list_sessions(directory)
    if not rollouts:
        print(f"no sessions in {directory}")
        return 0
    for rollout in rollouts:
        _, dropped = rollout.history()
        flags = []
        if dropped:
            flags.append(f"{dropped} incomplete")
        if rollout.truncated_at is not None:
            flags.append(f"damaged at line {rollout.truncated_at}")
        if rollout.meta.forked_from:
            flags.append(f"from {rollout.meta.forked_from}")
        # A sub-agent's file is only findable if something prints the link.
        if rollout.meta.parent:
            flags.append(f"sub-agent of {rollout.meta.parent}")
        suffix = f"   [{', '.join(flags)}]" if flags else ""
        print(f"  {rollout.meta.describe()}  {len(rollout.items)} msg{suffix}")
    print("\nresume with: minicodex ask '<next instruction>' --resume last")
    return 0


def _fork(reference: str, upto: int | None, directory: Path) -> int:
    try:
        path = fork(resolve(reference, directory), upto=upto, directory=directory)
    except RolloutError as exc:
        print(exc, file=sys.stderr)
        return 1
    print(f"forked to {path}")
    return 0


def _list_rules() -> int:
    """A rule nobody can see is a rule nobody can revoke."""
    store = RuleStore(Path(DEFAULT_RULES_PATH))
    if not len(store):
        print("no remembered approvals")
        return 0
    for index, rule in enumerate(store.all()):
        print(f"  {index}  {rule.describe()}")
    print(f"\nrevoke with: minicodex forget N   ({DEFAULT_RULES_PATH})")
    return 0


def _forget_rule(index: int) -> int:
    store = RuleStore(Path(DEFAULT_RULES_PATH))
    try:
        rule = store.forget(index)
    except IndexError:
        print(f"no rule numbered {index}; run `minicodex rules` to see them", file=sys.stderr)
        return 1
    print(f"revoked: {rule.describe()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
