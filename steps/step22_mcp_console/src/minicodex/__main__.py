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
from minicodex.memory import (
    BODY_FILE,
    DEFAULT_MEMORY_DIR,
    SUMMARY_FILE,
    USAGE_FILE,
    Memory,
    MemoryError_,
    MemoryWatcher,
    memory_instructions,
    parse_citations,
    record_uses,
    usage,
    usage_kinds_touched,
)
from minicodex.memory import load as load_memory  # `replay.load` is already here
from minicodex.memory_jobs import DEFAULT_JOBS_PATH, JobStore, pending_sessions
from minicodex.memory_write import (
    NOTE_NAME,
    NOTES_DIR,
    RAW_DIR,
    MemoryWriteError,
    pending_notes,
    remember_instructions,
    run_pipeline,
)
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
from minicodex.replay import (
    RecordedModel,
    ReplayDrift,
    ReplayError,
    load,
    recorded_tools,
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
from minicodex.skills import DEFAULT_SKILLS_DIR, Skills, SkillsWatcher, skills_instructions
from minicodex.skills import discover as discover_skills
from minicodex.subagent import describe_children
from minicodex.tools import TOOL_SCHEMAS, tool_context

PROVIDERS = {
    "ollama": (OLLAMA_BASE_URL, "gemma4:31b-cloud"),
    "openai": (OPENAI_BASE_URL, "gpt-4o-mini"),
}

# Written here rather than imported from `minicodex.web.store`, because
# importing anything under `minicodex.web` imports `fastapi`, and `fastapi` is
# an optional extra.  A default value for a flag is not worth making the whole
# CLI refuse to start without the console's dependencies installed; the actual
# import happens inside the `serve` branch, where it is allowed to fail.
CONSOLE_DATA_DIR = Path(".minicodex") / "console"


def _instructions(
    session: Session,
    tools: ToolSet | None = None,
    memory: Memory | None = None,
    skills: Skills | None = None,
) -> str:
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

    The memory paragraph joins on the same condition and for the same reason,
    with one addition of its own: it carries `INJECTION_RULE`, which is the
    only thing measured to stop a poisoned memory entry, and it is in the
    *system* message because that message is the cached prefix -- unlike the
    memory content itself, which is `role: "developer"`, delivered once by
    `MemoryWatcher` (see `_ask`).

    The skills paragraph is the same split a third time: how to reach a
    skill's body is static and cached here, while *which* skills exist changes
    with what is on disk and is delivered by `SkillsWatcher`, also as a
    developer note. Which wording it gets depends on whether `read_skill` was
    mounted, read off the tool table for the same reason the memory line above
    reads `dedicated_tools` off it -- describing an access path this run does
    not have is chapter 16's F16-09 confound, and it costs a measurement to
    find.
    """
    can_request = any(tool["function"]["name"] == "request_permissions" for tool in TOOL_SCHEMAS)
    block = permissions_block(session, can_request=can_request)
    parts = [system_prompt().rstrip()]
    if tools is not None and "update_plan" in tools.handlers:
        parts.append(PLAN_INSTRUCTIONS)
    if memory is not None:
        # Whether the dedicated tools exist is decided by `top_level_tools`'s
        # own `dedicated_tools` flag.  Read back off the tool table rather than
        # recomputed here, for chapter 5's reason: the two must agree, and the
        # cheapest way to make them agree is to have one of them ask the other.
        dedicated = tools is not None and "memory_search" in tools.handlers
        parts.append(memory_instructions(memory.directory, dedicated_tools=dedicated))
    if skills is not None:
        skill_tool = tools is not None and "read_skill" in tools.handlers
        parts.append(skills_instructions(skill_tool=skill_tool))
    # Chapter 17's note tool, on the same condition and for the same reason,
    # and read off the tool table rather than off a flag of its own: writing is
    # a separate consent from reading, so `--remember` without `--memory` is a
    # real configuration and the prompt has to describe that one too.
    if tools is not None and NOTE_NAME in tools.handlers:
        parts.append(remember_instructions())
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
    memory: Memory | None,
    remember: Path | None,
    skills: Skills | None = None,
    jobs_path: Path = DEFAULT_JOBS_PATH,
    dedicated_tools: bool = False,
    skill_tool: bool = False,
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
    tools = top_level_tools(
        root,
        session,
        sub_ctx,
        plan=task_plan,
        memory=memory,
        remember=remember,
        skills=skills,
        dedicated_tools=dedicated_tools,
        skill_tool=skill_tool,
    )

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
    # Written before the first request, not after the run: a recording is worth
    # having *because* the process died, and the line saying which model and
    # which sandbox mode produced it is the one a replay cannot do without.
    # This is F07-07's `SessionMeta` arriving at the other transcript, seven
    # chapters late -- the two files record the same run and only one of them
    # has ever said what the run was configured as.
    recorder.record(
        "config",
        {
            "provider": provider,
            "model": model or default_model,
            "cwd": str(root),
            "sandbox_mode": session.mode,
            "approval_policy": session.policy,
            "context_window": context_window,
            "question": question,
            "tools": sorted(tools.handlers),
            "schemas": tools.schemas,
            # Which memory produced this run, if any. The same argument as the
            # `config` event itself (chapter 14): a recording that cannot say
            # what was in the context is a recording that cannot be replayed,
            # and memory is now part of the context.
            "memory": str(memory.directory) if memory else None,
            # Same argument, one chapter later: which skills directory (if
            # any) produced the catalog this run was given.
            "skills": str(skills.directory) if skills else None,
        },
    )
    agents_watcher = AgentsMdWatcher(root)
    # Three watchers, one hook. `on_turn_start` takes a single callable, and
    # `AgentsMdWatcher` (chapter 13), `MemoryWatcher` (chapter 16) and
    # `SkillsWatcher` (chapter 18) answer different questions -- one re-checks
    # the filesystem every turn, two speak exactly once -- so none replaces
    # another; all are asked, and whatever any of them has to say is joined
    # into one developer note. On every turn but the first, at most one of
    # them has anything to say.
    memory_watcher = MemoryWatcher(memory, root=root) if memory is not None else None
    skills_watcher = SkillsWatcher(skills) if skills is not None else None

    def _on_turn_start() -> str | None:
        notes = [agents_watcher.refresh(Path(context.shell.cwd))]
        if memory_watcher is not None:
            notes.append(memory_watcher.refresh())
        if skills_watcher is not None:
            notes.append(skills_watcher.refresh())
        said = [note for note in notes if note is not None]
        return "\n\n".join(said) if said else None

    agent = wiring.agent(
        llm,
        tools,
        instructions=_instructions(session, tools, memory=memory, skills=skills),
        rollout=writer,
        resume_from=resume_from,
        on_stop=unfinished_note(task_plan),
        on_turn_start=_on_turn_start,
    )

    # Started here, before the first request, and left to run beside it.  This
    # is F17-01's whole fix: the same work at the *end* of a run is fourteen
    # measured seconds between the answer and the user's prompt coming back,
    # for something the user did not ask for.  Beside the run it costs nothing
    # anybody waits for, and what it consumes is the *previous* sessions --
    # this one is not finished and is excluded by name.
    #
    # Its own client, built with an empty tool list: the writer is two model
    # calls with no tools, and handing it this run's schemas would pay for
    # them on every request it makes for nothing.
    writer_task: asyncio.Task[Any] | None = None
    if remember is not None:
        writer_task = asyncio.ensure_future(
            run_pipeline(
                make_model([]),
                directory=remember,
                sessions_dir=session_dir,
                jobs_path=jobs_path,
                memory=memory,
                exclude=(current.session_id,),
                # A question, not a number: nothing has seen a rate-limit
                # header yet at this line.  See `run_pipeline`.
                headroom=lambda: llm.rate_limit.headroom() if llm.rate_limit else None,
            )
        )

    try:
        result = await agent.run(question)
    except ModelFailed as exc:
        # The one place a provider failure becomes something a person reads.
        # Before this, it was a twenty-line traceback ending in a JSON blob --
        # for a fault whose whole remedy is usually one sentence long.
        print(f"\n{exc}", file=sys.stderr)
        # No grace at all on this path.  The foreground just failed, and the
        # most likely reason -- a 429, a dead provider, a bad key -- is one the
        # background writer is about to hit twice more, using the same quota,
        # on behalf of nobody.
        if writer_task is not None:
            print(f"[{await _finish_writer(writer_task, grace=0.0)}]", file=sys.stderr)
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

    # The citation block is addressed to this program, not to the user, so it
    # comes off the answer before anything is printed.  `record_uses` is the
    # per-entry signal this chapter's forgetting reads (`memory_write.prune`);
    # `usage_kinds_touched` below is the other one, and codex's own default
    # (F16-14) -- it does not depend on the model volunteering a citation at
    # all.  They are printed together and used apart, which is codex's own
    # split: the behavioural one is observability, the citation one is policy.
    # See `prune`'s docstring for why the unreliable one is still the one that
    # decides (F17-13).
    answer, cited = result.final_text, None
    if memory:
        cited = parse_citations(result.final_text, memory)
        answer = cited.text
        record_uses(memory.directory, cited.used)
    print(answer or "(no answer)")
    if task_plan.steps:
        # Printed after the answer and before the machine lines, because it is
        # the one thing on screen the model did not write.  A run whose plan
        # still has open steps says so here even when the answer does not.
        print()
        print(task_plan.render())
    print(f"\n[{llm.model} | {result.stop_reason} after {result.turns_used} turn(s)]")
    print(f"[{task_plan.describe()}]")
    print(f"[{session.describe()}]")
    if memory:
        print(f"[{memory.describe()}]")
        assert cited is not None
        print(f"[{cited.describe()}]")
        # The behavioural signal, printed beside the citation one so the two can
        # be compared on any single run -- which is how F17-13 became visible at
        # all: a run that opens `MEMORY.md` and then cites nothing prints both
        # facts on adjacent lines.
        calls_made = [
            (call.name, call.arguments or {})
            for item in result.history.items
            for call in getattr(item, "tool_calls", ()) or ()
        ]
        kinds = usage_kinds_touched(memory, calls_made)
        if kinds:
            print(f"[memory usage: {', '.join(kinds)}]")
    if writer_task is not None:
        print(f"[{await _finish_writer(writer_task)}]")
    for event in result.compactions:
        print(f"[{event.describe()}]")
    print(f"[tokens: {agent.calibration.describe()}]")
    print(f"[transcript: {recorder.path}]")
    for line in describe_children(sub_ctx.children):
        print(line)
    print(f"[session: {writer.path}  (resume with: minicodex ask ... --resume last)]")
    return 0


# How long the run waits for the background writer once the answer is on
# screen.  Not zero -- a one-turn session is over before the writer's first
# request comes back, and a feature that only works on long sessions is a
# feature nobody can predict.  Not unbounded either, for the reason the writer
# is in the background at all.
WRITER_GRACE_SECONDS = 30.0


async def _finish_writer(task: asyncio.Task[Any], *, grace: float = WRITER_GRACE_SECONDS) -> str:
    """Collect the background writer's report, or leave it for the next run.

    `wait_for(shield(...))` rather than a bare `wait_for`, which is chapter
    10's measured lesson (F10-07) in a second place: a bare one cancels the
    task it is waiting on, and a cancelled pipeline that has already written
    its raw files would look, from here, exactly like one that did nothing.

    Cancelling is a normal outcome and not an error.  The claim it took is
    still in the database with a lease on it, and the lease expiring is how the
    next run picks the work up -- which is the answer to "why a lease and not a
    flag" (F17-03).  Nothing needs undoing, because the only write that ever
    happens is the last thing the pipeline does.
    """
    if not task.done():
        try:
            await asyncio.wait_for(asyncio.shield(task), grace)
        except TimeoutError:
            task.cancel()
            return f"memory writer: still going after {grace:.0f}s, left for the next run"
    try:
        return (await task).describe()
    except asyncio.CancelledError:
        return "memory writer: stopped, left for the next run"
    except (MemoryWriteError, MemoryError_, OSError, ModelFailed) as exc:
        # Never fatal.  A run that answered the user's question correctly does
        # not become a failed run because a note-taking job could not write a
        # file -- chapter 16's `record_uses` swallows for the same reason, one
        # size up.
        return f"memory writer: failed, nothing written ({type(exc).__name__}: {exc})"


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
    # Off unless asked for, and it is a flag rather than a config default for
    # the reason this chapter spends a whole fault on (F17-11): nobody has
    # agreed to be remembered.  codex gates the same feature behind a
    # feature flag that is `default_enabled: false`, on top of the
    # `[memories]` config table (`config/src/types.rs:290-352`) -- and the
    # cost of default-off is one flag, against a feature that silently
    # changes every request a user ever sends.
    ask.add_argument(
        "--memory",
        action="store_true",
        help=f"read {DEFAULT_MEMORY_DIR} and let the agent read it (off by default)",
    )
    # codex's own second flag, `memories.dedicated_tools`
    # (`config/src/types.rs:299,327,344`), also default off: the default access
    # path is `read_file`, already pointed at the memory directory once
    # `--memory` is on.  This adds `memory_search`/`memory_read` on top, for a
    # reader who wants to see what that path looks like (F16-12).
    ask.add_argument(
        "--dedicated-tools",
        action="store_true",
        help="also add memory_search/memory_read tools (off by default, needs --memory)",
    )
    # A *second* switch, not a mode of the first, and the split is the whole of
    # F17-11's expensive half.  Reading what you wrote by hand and letting a
    # model write things down about you are two different consents, and the
    # cost of asking for them separately is one flag.  codex asks the same
    # question in a dialog box with two buttons.
    ask.add_argument(
        "--remember",
        action="store_true",
        help=(
            "let this session be turned into memory later, and give the agent "
            f"`{NOTE_NAME}` (off by default; see `minicodex memory`)"
        ),
    )
    ask.add_argument("--memory-dir", type=Path, default=DEFAULT_MEMORY_DIR)
    ask.add_argument("--jobs-db", type=Path, default=DEFAULT_JOBS_PATH)
    # Same default-off argument as `--memory`, for the same reason: a skill
    # catalog is more prompt content nobody agreed to, on every request, until
    # they pass this flag.
    ask.add_argument(
        "--skills",
        action="store_true",
        help=f"read {DEFAULT_SKILLS_DIR} and let the agent open one on demand (off by default)",
    )
    ask.add_argument("--skills-dir", type=Path, default=DEFAULT_SKILLS_DIR)
    # Chapter 18's counterpart to `--dedicated-tools`, and the one flag in this
    # program with no codex equivalent whatsoever: codex's `skills.read` serves
    # `environment resource` and `orchestrator resource` skills
    # (`ext/skills/src/catalog_prompt.rs:3`), a kind this program cannot have,
    # and its `file` skills are opened by path with no tool in the way.  Kept
    # because it measured better than that default, off because it is not what
    # is being reproduced (F18-14).
    ask.add_argument(
        "--skill-tool",
        action="store_true",
        help="also add the read_skill tool (off by default, needs --skills)",
    )
    _add_permission_flags(ask)

    memory_cmd = sub.add_parser(
        "memory", help="show what is remembered, where it lives, and how to delete it"
    )
    memory_cmd.add_argument("--memory-dir", type=Path, default=DEFAULT_MEMORY_DIR)
    memory_cmd.add_argument(
        "--forget-all",
        action="store_true",
        help="delete every memory file in that directory, and the job records",
    )
    memory_cmd.add_argument(
        "--jobs",
        action="store_true",
        help="show which sessions have been turned into memory, and which are waiting",
    )
    memory_cmd.add_argument(
        "--remember-now",
        action="store_true",
        help="run the writer in the foreground over what is pending, and wait for it",
    )
    memory_cmd.add_argument("--session-dir", type=Path, default=DEFAULT_DIR)
    memory_cmd.add_argument("--jobs-db", type=Path, default=DEFAULT_JOBS_PATH)
    memory_cmd.add_argument("--provider", choices=sorted(PROVIDERS), default="openai")
    memory_cmd.add_argument("--model", default=None)

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

    # Chapter 19.  Beside `serve-stub` rather than under it: both serve HTTP and
    # neither is the agent, but the stub replaces a *provider* and this replaces
    # the *terminal*.
    console = sub.add_parser("serve", help="run the web console on a local port")
    console.add_argument(
        "--host",
        default="127.0.0.1",
        help="loopback by default: this process runs commands and has no auth",
    )
    console.add_argument("--port", type=int, default=8000)
    console.add_argument(
        "--data-dir",
        type=Path,
        default=CONSOLE_DATA_DIR,
        help="where the console keeps providers, threads and their rollout files",
    )

    replay_cmd = sub.add_parser(
        "replay", help="re-run a recorded session against the current code, offline"
    )
    replay_cmd.add_argument("recording", type=Path)
    replay_cmd.add_argument(
        "--loose",
        action="store_true",
        help="replay the answers without checking that the requests still match",
    )

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

    if args.command == "replay":
        return _replay(args.recording, strict=not args.loose)

    if args.command == "rules":
        return _list_rules()

    if args.command == "forget":
        return _forget_rule(args.index)

    if args.command == "memory":
        return _memory(
            args.memory_dir,
            forget_all=args.forget_all,
            jobs=args.jobs,
            remember_now=args.remember_now,
            session_dir=args.session_dir,
            jobs_path=args.jobs_db,
            provider=args.provider,
            model=args.model,
        )

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
        memory = None
        if args.memory:
            try:
                memory = load_memory(args.memory_dir)
            except MemoryError_ as exc:
                print(f"--memory: {exc}", file=sys.stderr)
                return 1
            # An empty memory is not an error and not a silent no-op either.
            # `--memory` with nothing on disk is the state every user is in on
            # their first day, and the useful thing to say is where to put it.
            if not memory:
                print(f"[{memory.describe()}; run `minicodex memory` to see where it goes]")
                memory = None
        skills = None
        if args.skills:
            found = discover_skills(args.skills_dir)
            if found.skipped:
                # Not fatal -- a malformed `SKILL.md` next to two good ones
                # should not take the other two down -- but silent is exactly
                # the failure mode chapter -1 keeps a rule against: say what
                # was skipped and where, once, at startup.
                print(f"[skills: skipped {', '.join(found.skipped)}]")
            if not found:
                print(f"[{found.describe()}; create {found.directory}/<name>/SKILL.md to use it]")
            else:
                skills = found
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
                memory=memory,
                remember=args.memory_dir if args.remember else None,
                skills=skills,
                jobs_path=args.jobs_db,
                dedicated_tools=args.dedicated_tools,
                skill_tool=args.skill_tool,
            )
        )

    if args.command == "serve-stub":
        from minicodex.stub import serve

        serve(args.port)
        return 0

    if args.command == "serve":
        # Imported here and not at the top: `minicodex.web` needs `fastapi`,
        # which is the `web` extra.  Every other subcommand has to keep working
        # on an install that does not have it.
        try:
            from minicodex.web import serve as serve_console
        except ImportError as exc:
            print(f"the console needs its extra: uv sync --extra web ({exc})", file=sys.stderr)
            return 1

        return serve_console(host=args.host, port=args.port, data_dir=args.data_dir)

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


def _replay(path: Path, *, strict: bool) -> int:
    """Re-run a recorded session against the code as it stands today.

    No network, no API key, no files touched: the model side comes out of the
    recording and the tool side comes out of the requests that followed it.
    What is being tested is everything in between -- the loop, the history, the
    context assembly, the retry decisions -- against a conversation that
    actually happened rather than one a fixture author imagined.

    What a recording does **not** contain, and therefore what replays as drift
    rather than as itself: anything the loop reads from the machine instead of
    from the model.  `AGENTS.md` (chapter 13) is read fresh every turn, the
    plan (chapter 11) lives in an object, and the summariser's own model call
    (chapter 6) never passed through the recorder at all -- so a session that
    compacted is refused rather than half-replayed.
    """
    try:
        recording = load(path)
    except (OSError, ReplayError) as exc:
        print(exc, file=sys.stderr)
        return 1

    config = recording.config
    if config is None:
        print(
            f"{path}: no `config` event. This recording was made before chapter 14 "
            "and does not say which tools, model or sandbox mode produced it.",
            file=sys.stderr,
        )
        return 1
    if recording.compacted:
        print(
            f"{path}: this session compacted, and the summariser's own model call "
            "is not recorded. Replaying it would invent a summary.",
            file=sys.stderr,
        )
        return 1

    # `flush`, because the drift report goes to stderr and these go to stdout:
    # unflushed, a piped run prints the failure before the header it belongs to.
    print(f"{path}\n  {recording.describe()}", flush=True)
    print(f"  recorded against {config.get('provider')}/{config.get('model')}", flush=True)

    replayed = recorded_tools(recording)
    tools = ToolSet(
        handlers=replayed.table(config["tools"]),
        schemas=list(config["schemas"]),
    )
    session = Session(mode=config["sandbox_mode"], policy=config["approval_policy"])
    model = RecordedModel(recording, strict=strict)
    agent = Wiring(context_window=config.get("context_window")).agent(
        model,
        tools,
        instructions=_instructions(session, tools),
    )

    try:
        result = asyncio.run(agent.run(config["question"]))
    except ReplayDrift as exc:
        print(f"\ndrift: {exc}", file=sys.stderr)
        return 1
    except ReplayError as exc:
        print(f"\n{exc}", file=sys.stderr)
        return 1

    for index, sent in enumerate(model.sent):
        print(f"  turn {index}: {len(sent)} message(s) match")
    if replayed.missed:
        print(f"  {len(replayed.missed)} call(s) not in the recording: {replayed.missed[:3]}")
    print(f"\nok  {result.stop_reason} after {result.turns_used} turn(s), same as recorded")
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


def _memory(
    directory: Path,
    *,
    forget_all: bool = False,
    jobs: bool = False,
    remember_now: bool = False,
    session_dir: Path = DEFAULT_DIR,
    jobs_path: Path = DEFAULT_JOBS_PATH,
    provider: str = "openai",
    model: str | None = None,
) -> int:
    """Everything a user needs to answer "what does it know about me".

    Three things, and the order is the order somebody asks them in: where it
    is, what is in it, and how to make it stop. `minicodex rules` (chapter 5)
    is the same command for a different store, and the reason both exist is
    the same one: a thing nobody can see is a thing nobody can revoke.

    Chapter 17 adds two more questions the same person asks next -- *where does
    it come from* (`--jobs`) and *do it now* (`--remember-now`) -- and one
    obligation to the first: `--forget-all` has to delete the extracted
    material and the job records too, or "delete everything" leaves the
    intermediate copies behind and marks every session that produced them as
    already processed.
    """
    if forget_all:
        removed = []
        for name in (SUMMARY_FILE, BODY_FILE, USAGE_FILE):
            path = directory / name
            if path.is_file():
                path.unlink()
                removed.append(name)
        # The two directories chapter 17 writes.  `raw/` holds stage-1 output
        # -- preferences extracted from sessions, in a file that is memory in
        # everything but name -- and `notes/` holds what the model proposed.
        # A `--forget-all` that leaves those is a `--forget-all` that lies.
        for name in (RAW_DIR, NOTES_DIR):
            folder = directory / name
            if folder.is_dir():
                for path in folder.glob("*.md"):
                    path.unlink()
                removed.append(f"{name}/")
        if Path(jobs_path).exists():
            with JobStore(jobs_path) as store:
                store.forget_all()
            removed.append(jobs_path.name)
        print(f"deleted {', '.join(removed) if removed else 'nothing'} from {directory}")
        if (directory / ".git").is_dir():
            # Said, not done.  The whole point of the git repository is that a
            # merge cannot silently eat a hand edit, and quietly deleting the
            # history to satisfy a `--forget-all` would be this program
            # destroying the one record it kept of its own changes.
            print(f"note: the git history in {directory / '.git'} still has every past version.")
            print(f"      remove it with: rm -rf {directory / '.git'}")
        return 0

    if jobs:
        return _memory_jobs(jobs_path, session_dir)

    if remember_now:
        return _remember_now(
            directory,
            session_dir=session_dir,
            jobs_path=jobs_path,
            provider=provider,
            model=model,
        )

    print(f"memory directory: {directory.resolve()}")
    try:
        memory = load_memory(directory)
    except MemoryError_ as exc:
        print(exc, file=sys.stderr)
        return 1
    print(f"  {memory.describe()}")
    if not memory:
        print(f"\nTo start one, write {directory}/{SUMMARY_FILE}:")
        print("  v1\n  - Run tests as `python -m pytest`.")
        print(f"and, optionally, longer sections in {directory}/{BODY_FILE}:")
        print("  v1\n\n  ## Running tests\n\n  ...")
        print("\nMemory is off unless you pass --memory.")
        return 0

    counts = usage(directory)
    for entry in memory.entries:
        row = counts.get(entry.entry_id, {})
        used = row.get("count", 0)
        print(f"  {entry.entry_id:<40} cited {used} time(s)  {entry.headline()[:40]}")
    pending = pending_notes(directory)
    if pending:
        print(f"\n  {len(pending)} proposed note(s) not merged yet, in {directory / NOTES_DIR}")
    print(f"\ndelete everything with: minicodex memory --forget-all --memory-dir {directory}")
    return 0


def _memory_jobs(jobs_path: Path, session_dir: Path) -> int:
    """Which sessions became memory, which are waiting, and which gave up.

    The answer to "why does it not know that yet", which is the first question
    a user asks about a feature that runs when they are not looking.  A
    background job whose state is only in a database nobody can read is a
    background job that gets reported as broken.
    """
    if not Path(jobs_path).exists():
        print(f"no job records at {jobs_path} (nothing has been remembered yet)")
        return 0
    with JobStore(jobs_path) as store:
        rows = store.rows()
        counts = store.counts()
        if not rows:
            print(f"no job records at {jobs_path}")
            return 0
        for row in rows[:20]:
            detail = f"  {row['detail']}" if row["detail"] else ""
            print(f"  {row['session_id']:<28} {row['state']:<8} attempt {row['attempts']}{detail}")
        print(f"\n  {', '.join(f'{k}: {v}' for k, v in sorted(counts.items()))}")
        waiting = len(pending_sessions(session_dir))
        print(f"  {waiting} session file(s) in {session_dir} are eligible")
    return 0


def _remember_now(
    directory: Path,
    *,
    session_dir: Path,
    jobs_path: Path,
    provider: str,
    model: str | None,
) -> int:
    """Run the writer in the foreground, and say what it did.

    The same function the background task runs -- not a second implementation
    of it.  What differs is who waits: here the user asked, so waiting is the
    thing they asked for.
    """
    default_url, default_model = PROVIDERS[provider]
    client = ChatCompletionsModel(
        base_url=default_url,
        model=model or default_model,
        api_key=os.environ.get("OPENAI_API_KEY") if provider == "openai" else None,
        tools=[],
    )
    print(f"memory directory: {directory.resolve()}")
    try:
        report = asyncio.run(
            run_pipeline(
                client,
                directory=directory,
                sessions_dir=session_dir,
                jobs_path=jobs_path,
                # No `exclude`: this command is not a session, so there is no
                # current session to leave out -- and no `headroom` either,
                # because the user is standing here having asked for it. The
                # quota rule is about work nobody requested.
            )
        )
    except (MemoryWriteError, MemoryError_, ModelFailed, OSError) as exc:
        print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    print(f"  {report.describe()}")
    for session_id, detail in report.failed:
        print(f"  failed: {session_id}: {detail}", file=sys.stderr)
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
