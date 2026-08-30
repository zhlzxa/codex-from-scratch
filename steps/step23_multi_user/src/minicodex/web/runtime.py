"""One user turn, wired exactly the way `minicodex ask` wires one.

This module is the console's whole claim to being real, so it is worth stating
what "exactly" means and where it stops.

Same as `__main__._ask`: the `Wiring`, the recorder, the compaction summariser
and the fact that it is bound to *both* the parent wiring and `sub_ctx`, the
`tool_context`, `top_level_tools` with plan/memory/remember/skills, the MCP
registry and its elicitation handler, `watching(with_remote_tools(...))`, the
system-prompt assembly order, the three `on_turn_start` watchers that deliver
the AGENTS.md, memory and skill blocks, the background memory writer started
before the first request, the resume path including the damage note and the
environment note, and every post-run report.

Different, and only these four:

1. The approver is `WebApprover` rather than `CliApprover` -- chapter 5's seam,
   used, not modified.
2. `announce` and every post-run line go to an event sink instead of `print`.
3. "Continue this conversation" means resuming from *this thread's own* last
   rollout file rather than from a `--resume` flag, so one browser tab can
   never resume into another tab's conversation.
4. The run happens in the workspace the thread was created for, not in the
   process's own cwd. `ShellSession` and `tool_context` already take a root;
   nothing here chdirs, because a server that chdirs has one working directory
   for every thread it is running.

Nothing in `src/minicodex/*.py` outside this subpackage was changed to make any
of it work; `probe_web.py core-diff` is what checks that claim rather than this
docstring making it.
"""

from __future__ import annotations

import asyncio
import os
import time
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Any

from minicodex import system_prompt
from minicodex.agent import RunResult, Wiring
from minicodex.agent_types import ToolSet
from minicodex.agents_md import AgentsMdWatcher
from minicodex.approval import Session, permissions_block
from minicodex.compaction import make_summariser
from minicodex.composition import sub_context, top_level_tools, watching, with_remote_tools
from minicodex.history import History
from minicodex.mcp_oauth import OwnerTokenStorage
from minicodex.memory import (
    Memory,
    MemoryError_,
    MemoryWatcher,
    memory_instructions,
    parse_citations,
    record_uses,
)
from minicodex.memory import load as load_memory
from minicodex.memory_jobs import DEFAULT_JOBS_PATH
from minicodex.memory_write import NOTE_NAME, MemoryWriteError, remember_instructions, run_pipeline
from minicodex.model import ChatCompletionsModel
from minicodex.plan import PLAN_INSTRUCTIONS, TaskPlan, unfinished_note
from minicodex.recorder import Recorder
from minicodex.registry import McpRegistry, connect, elicitation_handler
from minicodex.retry import ModelFailed
from minicodex.rollout import (
    RolloutError,
    SessionMeta,
    environment_note,
    interrupted_note,
    new_session_id,
    read_rollout,
    resolve,
    rollout_path,
)
from minicodex.rules import RuleStore
from minicodex.skills import DEFAULT_SKILLS_DIR, Skills, SkillsWatcher, skills_instructions
from minicodex.skills import discover as discover_skills
from minicodex.subagent import describe_children
from minicodex.tenancy import DEFAULT_OWNER, Owner
from minicodex.tools import TOOL_SCHEMAS, tool_context

from .approver import ApprovalBroker, WebApprover
from .events import EventSink, StreamingRolloutWriter
from .mcp_config import record_kind, server_config

#: The same grace `__main__` gives the background memory writer once the answer
#: is on screen, and for the same reason.
WRITER_GRACE_SECONDS = 30.0


def _instructions(
    session: Session,
    tools: ToolSet | None = None,
    memory: Memory | None = None,
    skills: Skills | None = None,
) -> str:
    """`__main__._instructions`, verbatim in behaviour.

    Duplicated rather than imported because importing it would make this
    subpackage depend on `__main__`, and `__main__` is the layer *above*
    composition, not beside it -- `scripts/check_layers.py` forbids the edge.
    The duplication is checked instead of promised: `test_faults_ch19.py`
    asserts the two produce the same string for the same inputs, which is the
    same repair chapter 3 made for the tool descriptions.

    Both halves take the loaded `Memory`/`Skills` rather than a pair of bools,
    which is chapter 16's rewrite arriving here: the memory paragraph now names
    the directory it describes, so a flag no longer carries enough to build it.
    """
    can_request = any(tool["function"]["name"] == "request_permissions" for tool in TOOL_SCHEMAS)
    block = permissions_block(session, can_request=can_request)
    parts = [system_prompt().rstrip()]
    if tools is not None and "update_plan" in tools.handlers:
        parts.append(PLAN_INSTRUCTIONS)
    if memory is not None:
        dedicated = tools is not None and "memory_search" in tools.handlers
        parts.append(memory_instructions(memory.directory, dedicated_tools=dedicated))
    if skills is not None:
        skill_tool = tools is not None and "read_skill" in tools.handlers
        parts.append(skills_instructions(skill_tool=skill_tool))
    if tools is not None and NOTE_NAME in tools.handlers:
        parts.append(remember_instructions())
    parts.append(block)
    return "\n\n".join(parts)


def load_feature_dirs(
    root: Path, settings: dict[str, Any], owner: Owner = DEFAULT_OWNER
) -> tuple[Memory | None, Path | None, Skills | None, list[str]]:
    """Resolve the memory and skills switches against one workspace.

    The two resolve *differently*, and after chapter 16's rewrite that is the
    whole point of this function rather than an inconsistency in it. Memory is
    per *owner* -- `owner.memories()`, which for the CLI's
    `DEFAULT_OWNER` is byte for byte `~/.minicodex/memories`, codex's
    `codex_home.join("memories")` (`memories/read/src/lib.rs:13-15`). Skills
    stayed per-project, so they still resolve against the workspace root, the
    thing that path was always relative *to*.

    A server is where that difference is visible for the first time: the CLI
    has one cwd and cannot tell a global directory from a relative one that
    happens to resolve there.

    Chapter 20 wrote the paragraph this replaces, and every sentence of it was
    true: "every workspace this server hosts shares one, exactly as every
    project on one machine shares one in codex". The conclusion was a data
    leak, because "every project on one machine" is one person's and "every
    workspace this server hosts" is everybody's. Chapter 20 built `Owner` for
    it and could not use it, because there was nobody to ask who this was.
    Chapter 23 is that answer arriving, and the whole change here is one
    parameter with a default.

    Returns the notices as data rather than printing them: an empty memory
    directory degrading to "off" is something the browser has to be told, and
    `__main__` tells the terminal at exactly this point.
    """
    notices: list[str] = []
    memory: Memory | None = None
    if settings.get("memory"):
        try:
            loaded = load_memory(owner.memories())
        except MemoryError_ as exc:
            notices.append(f"memory: not loaded -- {exc}")
        else:
            if loaded:
                memory = loaded
            else:
                notices.append(f"memory: {loaded.describe()}; running without it")

    # `--remember` without `--memory` is a real configuration: writing is a
    # separate consent from reading (F17-11), so this is not an `elif`.
    remember = owner.memories() if settings.get("remember") else None

    skills: Skills | None = None
    if settings.get("skills"):
        found = discover_skills(root / DEFAULT_SKILLS_DIR)
        if found.skipped:
            notices.append(f"skills: skipped {', '.join(found.skipped)}")
        if found:
            skills = found
        else:
            notices.append(f"skills: {found.describe()}; running without them")
    return memory, remember, skills, notices


async def _finish_writer(task: asyncio.Task[Any], *, grace: float = WRITER_GRACE_SECONDS) -> str:
    """`__main__._finish_writer`, with the 3.10 timeout spelling corrected.

    The original catches the builtin `TimeoutError` around an `asyncio.wait_for`
    -- which on 3.10 is an unrelated class, so the grace period does not end,
    it raises. Same fault as F15-02, one module over, still there.
    """
    if not task.done():
        try:
            await asyncio.wait_for(asyncio.shield(task), grace)
        except asyncio.TimeoutError:
            task.cancel()
            return f"memory writer: still going after {grace:.0f}s, left for the next run"
    try:
        return (await task).describe()
    except asyncio.CancelledError:
        return "memory writer: stopped, left for the next run"
    except (MemoryWriteError, MemoryError_, OSError, ModelFailed) as exc:
        return f"memory writer: failed, nothing written ({type(exc).__name__}: {exc})"


async def _oauth_redirect_refused(url: str) -> None:
    """The `redirect_handler` a turn's `connect()` call is given for any
    remote server with OAuth configured.

    It is never actually reached in practice -- `run_turn` only ever hands
    `connect()` a config whose token storage already has a token (see below)
    -- and it exists so that if a stored token and its refresh token both stop
    working mid-turn, `OAuthClientProvider`'s 401 branch fails loudly with a
    sentence pointing at the console's own Connect button instead of trying to
    open a browser on the server (F22-01, again: that is exactly the thing
    this chapter exists because chapter 21's default cannot do here).
    """
    raise RuntimeError(
        "this server's OAuth session needs to be renewed from the console's "
        "MCP tab -- open Connect there, then send the message again"
    )


async def _oauth_callback_refused() -> Any:
    raise RuntimeError("no interactive OAuth callback exists inside a turn")


async def resolve_mcp_configs(
    enabled: list[dict[str, Any]], emit: EventSink, owner: Owner = DEFAULT_OWNER
) -> list[Any]:
    """Every enabled server record, minus the ones this turn cannot connect.

    A remote server with OAuth configured but no token on file yet is the one
    case this turn refuses outright rather than attempting and failing
    (F22-01): the only `redirect_handler`/`callback_handler` a turn could hand
    `connect()` are `_oauth_redirect_refused`/`_oauth_callback_refused` above,
    which exist to raise rather than to open a browser this process has no
    way to show anyone. Reported once, by name, as a status the console's MCP
    tab already renders -- "needs connection" -- rather than a stack trace
    that says nothing about what to click.

    Split out of `run_turn` so it can be tested on its own: what it decides
    does not depend on a model, an approval broker or a workspace, only on the
    stored records and whether `OwnerTokenStorage` already has a token.
    """
    configs = []
    for s in enabled:
        config = server_config(s)
        if record_kind(s) == "remote" and config.oauth is not None:
            storage = OwnerTokenStorage(owner, server=config.name)
            if await storage.get_tokens() is None:
                emit(
                    {
                        "type": "mcp_status",
                        "name": config.name,
                        "status": "needs_connection",
                        "detail": "not connected yet -- use Connect in the MCP tab",
                    }
                )
                continue
        configs.append(config)
    return configs


async def run_turn(
    *,
    thread_dir: Path,
    workspace_root: Path,
    question: str,
    settings: dict[str, Any],
    provider_name: str,
    base_url: str,
    model: str,
    api_key: str | None,
    mcp_servers: list[dict[str, Any]],
    broker: ApprovalBroker,
    emit: EventSink,
    owner: Owner = DEFAULT_OWNER,
) -> RunResult:
    # Already absolute, already resolved, already known to exist: `routes.
    # _validated_workspace` did all three when the thread was created. This
    # function used to do `resolve()` and `mkdir(parents=True)` itself, which
    # is how a typed-in typo silently created a directory tree anywhere the
    # server user could write and then ran an agent inside it. Validation
    # belongs where the string enters the program, not where it is used.
    root = workspace_root
    context_window = settings.get("context_window")

    session = Session(
        mode=settings["sandbox_mode"],
        policy=settings["approval_policy"],
        rules=RuleStore(root / ".minicodex" / "rules.json"),
        approver=WebApprover(broker, emit),
    )

    def make_model(tools: list[dict[str, Any]]) -> ChatCompletionsModel:
        return ChatCompletionsModel(base_url=base_url, model=model, api_key=api_key, tools=tools)

    def announce(message: str) -> None:
        emit({"type": "notice", "text": message})

    memory, remember, skills, notices = load_feature_dirs(root, settings, owner)
    for notice in notices:
        announce(notice)

    # A path to a *file*, not a directory: `Recorder(path=None)` builds
    # `DEFAULT_DIR / f"session-{int(time.time())}.jsonl"` relative to the
    # process cwd, and the process cwd is not the workspace here.
    recorder = Recorder(root / ".minicodex" / "recordings" / f"session-{int(time.time())}.jsonl")
    context = tool_context(root=root, session=session)
    wiring = Wiring(
        recorder=recorder,
        context_window=context_window,
        announce=announce,
        summariser=None,
    )
    current = SessionMeta(
        session_id=new_session_id(),
        created=time.time(),
        cwd=str(root),
        provider=provider_name,
        model=model,
        sandbox_mode=session.mode,
        approval_policy=session.policy,
    )
    sub_ctx = sub_context(
        build_model=make_model,
        root=root,
        session=session,
        parent_shell=context.shell,
        wiring=wiring,
        sessions_dir=thread_dir,
        parent_session_id=current.session_id,
        provider=provider_name,
        model=model,
        announce=announce,
    )

    task_plan = TaskPlan()
    tools = top_level_tools(
        root, session, sub_ctx, plan=task_plan, memory=memory, remember=remember, skills=skills
    )

    registry = McpRegistry(local=tools.schemas)
    clients = []
    enabled = [s for s in mcp_servers if s.get("enabled", True)]
    if enabled:
        configs = await resolve_mcp_configs(enabled, emit, owner)
        clients = (
            await connect(
                configs,
                registry,
                handlers={"elicitation/create": elicitation_handler(session)},
                redirect_handler=_oauth_redirect_refused,
                callback_handler=_oauth_callback_refused,
            )
            if configs
            else []
        )
        for name, reason in registry.failures.items():
            emit({"type": "mcp_status", "name": name, "status": "error", "detail": reason})
        for client in clients:
            offered = sum(1 for r in registry.registrations.values() if r.client is client)
            emit(
                {
                    "type": "mcp_status",
                    "name": client.config.name,
                    "status": "connected",
                    "tools": offered,
                }
            )
        if registry.deferred:
            announce(f"mcp: {len(registry.deferred)} tool(s) deferred behind tool_search")

    tools = watching(with_remote_tools(tools, registry), task_plan)
    llm = make_model(tools.schemas)
    if context_window:
        wiring = replace(wiring, summariser=make_summariser(llm))
        # Both, not just the parent: interlude B's measured drift is exactly a
        # child whose wiring differs from its parent's, and a child that never
        # compacts is the version of it that costs money.
        sub_ctx = replace(sub_ctx, wiring=wiring)

    resume_from: History | None = None
    try:
        previous_path = resolve("last", thread_dir)
    except RolloutError:
        pass  # first turn in this thread
    else:
        loaded = read_rollout(previous_path)
        resume_from, dropped = loaded.history()
        if loaded.truncated_at is not None:
            announce(
                f"session file damaged from line {loaded.truncated_at}; using what precedes it"
            )
        if dropped:
            resume_from.add_system_note(interrupted_note(dropped))
            announce(f"resumed: {dropped} incomplete message(s) discarded")
        note = environment_note(loaded.meta, current)
        if note is not None:
            resume_from.add_system_note(note)
            announce("environment changed since this session was recorded")
        current = replace(current, forked_from=loaded.meta.session_id)

    writer = StreamingRolloutWriter(rollout_path(current.session_id, thread_dir), current, emit)
    recorder.record(
        "config",
        {
            "provider": provider_name,
            "model": model,
            "cwd": str(root),
            "sandbox_mode": session.mode,
            "approval_policy": session.policy,
            "context_window": context_window,
            "question": question,
            "tools": sorted(tools.handlers),
            "schemas": tools.schemas,
            "memory": str(memory.directory) if memory else None,
            "skills": str(skills.directory) if skills else None,
        },
    )
    agents_watcher = AgentsMdWatcher(root)
    # Three watchers, one hook -- `__main__._ask`'s arrangement, for its
    # reasons. `Wiring.agent(preamble=...)` used to carry the memory and skill
    # blocks here; that parameter is gone (F16-11), and what replaced it is the
    # per-turn hook chapter 13 already had, with each watcher deciding for
    # itself whether it has anything to say this turn.
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

    # Two things the browser must see that are not history items: the plan, and
    # the sandbox mode `request_permissions` can rewrite mid-turn. Derived by
    # comparing against the last value after every item is durable, rather than
    # by a callback the core would have to grow. It costs one tuple comparison
    # per item and it means neither feature needed a hook.
    writer.after_item = _derived(task_plan, session, emit)

    agent = wiring.agent(
        llm,
        tools,
        instructions=_instructions(session, tools, memory=memory, skills=skills),
        rollout=writer,
        resume_from=resume_from,
        on_stop=unfinished_note(task_plan),
        on_turn_start=_on_turn_start,
    )

    writer_task: asyncio.Task[Any] | None = None
    if remember is not None:
        writer_task = asyncio.ensure_future(
            run_pipeline(
                make_model([]),
                directory=remember,
                sessions_dir=thread_dir,
                # Global, like the memory it books work against (F17-12): one
                # queue per machine, not one per workspace the server hosts.
                jobs_path=DEFAULT_JOBS_PATH,
                memory=memory,
                exclude=(current.session_id,),
                headroom=lambda: llm.rate_limit.headroom() if llm.rate_limit else None,
            )
        )

    try:
        result = await agent.run(question)
    except ModelFailed as exc:
        emit({"type": "error", "text": str(exc)})
        if writer_task is not None:
            announce(await _finish_writer(writer_task, grace=0.0))
        raise
    except asyncio.CancelledError:
        # The stop button. `agent.run` writes `mark("interrupted")` on its way
        # out, so the rollout already says where it stopped and the next turn
        # in this thread resumes from there.
        if writer_task is not None:
            writer_task.cancel()
        emit({"type": "notice", "text": "stopped"})
        raise
    finally:
        for client in clients:
            await client.close()
        writer.release()

    answer, cited = result.final_text, None
    if memory:
        cited = parse_citations(result.final_text, memory)
        answer = cited.text
        record_uses(memory.directory, cited.used)

    reports = [
        f"{llm.model} | {result.stop_reason} after {result.turns_used} turn(s)",
        task_plan.describe(),
        session.describe(),
    ]
    if memory:
        reports.append(memory.describe())
        assert cited is not None
        reports.append(cited.describe())
    if writer_task is not None:
        reports.append(await _finish_writer(writer_task))
    reports.extend(event.describe() for event in result.compactions)
    reports.append(f"tokens: {agent.calibration.describe()}")
    reports.extend(line.strip("[]") for line in describe_children(sub_ctx.children))

    emit(
        {
            "type": "turn_complete",
            "final_text": answer,
            "stop_reason": result.stop_reason,
            "turns_used": result.turns_used,
            "model": llm.model,
            "plan": _plan_payload(task_plan),
            "reports": reports,
            "transcript": str(recorder.path),
            "session_file": str(writer.path),
            "citations": list(cited.used) if cited is not None else [],
        }
    )
    return result


def _plan_payload(plan: TaskPlan) -> list[dict[str, str]]:
    return [{"text": step.text, "status": step.status} for step in plan.steps]


def _derived(plan: TaskPlan, session: Session, emit: EventSink) -> Callable[[], None]:
    """Emit the plan and the permission state, but only when they change."""
    last: dict[str, Any] = {"plan": _plan_payload(plan), "mode": session.mode}

    def check() -> None:
        current_plan = _plan_payload(plan)
        if current_plan != last["plan"]:
            last["plan"] = current_plan
            emit({"type": "plan_updated", "steps": current_plan, "revisions": plan.updates})
        if session.mode != last["mode"]:
            previous, last["mode"] = last["mode"], session.mode
            emit(
                {
                    "type": "session_mode_changed",
                    "from": previous,
                    "mode": session.mode,
                    "policy": session.policy,
                }
            )

    return check


def resolve_api_key(provider: dict[str, Any]) -> str | None:
    """The pasted key, or the environment, never a file.

    A key typed into the browser is kept in `providers.json` because there is
    nowhere else for it to live; a key that came from `OPENAI_API_KEY` is read
    at call time and never written down, so clearing the form in the browser
    cannot copy the environment's key into the file.
    """
    if provider.get("api_key"):
        return str(provider["api_key"])
    if provider.get("provider") == "openai":
        return os.environ.get("OPENAI_API_KEY")
    return None
