"""One user turn, wired exactly the way `minicodex ask` (`__main__._ask`) wires one.

Same wiring: the `Wiring`, the recorder, the compaction summariser, the
`tool_context`, `top_level_tools` with plan/memory/remember/skills, the MCP
registry and its elicitation handler, `watching(with_remote_tools(...))`, the
system-prompt assembly order, the `on_turn_start` watchers, the background
memory writer, the resume path and every post-run report.

Different, and only these four:

1. The approver is `WebApprover` rather than `CliApprover`.
2. `announce` and every post-run line go to an event sink instead of `print`.
3. "Continue this conversation" resumes from *this thread's own* last rollout
   file, so one browser tab can never resume into another tab's conversation.
4. The run happens in the workspace the thread was created for, not the
   process cwd. Nothing here chdirs: a server has one working directory for
   every thread it is running.
"""

from __future__ import annotations

import asyncio
import os
import time
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Any

from minicodex.agent import RunResult, Wiring
from minicodex.agent_types import ToolSet
from minicodex.agents_md import AgentsMdWatcher
from minicodex.approval import Session
from minicodex.compaction import make_summariser
from minicodex.composition import (
    instructions as composition_instructions,
)
from minicodex.composition import sub_context, top_level_tools, watching, with_remote_tools
from minicodex.history import History
from minicodex.mcp_oauth import OwnerTokenStorage
from minicodex.memory import (
    Memory,
    MemoryLoadError,
    MemoryWatcher,
    parse_citations,
    record_uses,
)
from minicodex.memory import load as load_memory
from minicodex.memory_write import MemoryWriteError, run_pipeline
from minicodex.model import ChatCompletionsModel
from minicodex.plan import TaskPlan, unfinished_note
from minicodex.recorder import Recorder
from minicodex.registry import McpRegistry, connect, elicitation_handler
from minicodex.retry import ModelFailed
from minicodex.rollout import (
    RolloutError,
    SessionMeta,
    new_session_id,
    resolve,
    resume_from_rollout,
    rollout_path,
)
from minicodex.rules import RuleStore
from minicodex.sandbox import Sandbox
from minicodex.sandbox import for_mode as sandbox_for_mode
from minicodex.shell import ShellSession
from minicodex.skills import DEFAULT_SKILLS_DIR, Skills, SkillsWatcher
from minicodex.skills import discover as discover_skills
from minicodex.subagent import describe_children
from minicodex.tenancy import DEFAULT_OWNER, Owner
from minicodex.tools import tool_context

from .approver import ApprovalBroker, WebApprover
from .audit import AuditLog
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
    confined: bool = False,
) -> str:
    """`composition.instructions` under this module's historical name.

    Prompt assembly is a core concern; both entry points call the one copy.
    """
    return composition_instructions(session, tools, memory, skills, confined)


def load_feature_dirs(
    root: Path, settings: dict[str, Any], owner: Owner = DEFAULT_OWNER
) -> tuple[Memory | None, Path | None, Skills | None, list[str]]:
    """Resolve the memory/remember/skills switches against one workspace.

    Memory is per *owner* (`owner.memories()`); skills stay per-project and
    resolve against the workspace root. A server is where that difference is
    visible: the CLI has one cwd and cannot tell a global directory from a
    relative one that happens to resolve there.

    Returns the notices as data rather than printing them: an empty memory
    directory degrading to "off" is something the browser has to be told, and
    `__main__` tells the terminal at exactly this point.
    """
    notices: list[str] = []
    memory: Memory | None = None
    if settings.get("memory"):
        try:
            loaded = load_memory(owner.memories())
        except MemoryLoadError as exc:
            notices.append(f"memory: not loaded -- {exc}")
        else:
            if loaded:
                memory = loaded
            else:
                notices.append(f"memory: {loaded.describe()}; running without it")

    # `--remember` without `--memory` is a real configuration: writing is a
    # separate consent from reading, so this is not an `elif`.
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
    it raises.
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
    except (MemoryWriteError, MemoryLoadError, OSError, ModelFailed) as exc:
        return f"memory writer: failed, nothing written ({type(exc).__name__}: {exc})"


async def _oauth_redirect_refused(url: str) -> None:
    """The `redirect_handler` a turn's `connect()` call is given.

    Never reached in practice -- `run_turn` only hands `connect()` configs
    whose token storage already has a token (see `resolve_mcp_configs`). It
    exists so that a stored token and refresh token both dying mid-turn makes
    `OAuthClientProvider`'s 401 branch fail loudly with a sentence pointing at
    the console's own Connect button, instead of trying to open a browser on
    the server, which would reach nobody.
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
    case this turn refuses outright rather than attempting and failing: the
    only `redirect_handler`/`callback_handler` a turn could hand `connect()`
    are `_oauth_redirect_refused`/`_oauth_callback_refused`, which exist to
    raise rather than to open a browser this process cannot show anyone.
    Reported once, by name, as the status the console's MCP tab already
    renders ("needs connection") rather than a stack trace that says nothing
    about what to click.

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


class _AuditedShell(ShellSession):
    """A `ShellSession` whose `run` writes one audit line per command.

    Subclassed rather than wrapped: `context.shell` is reached as
    `shell.cwd`, `shell.timeout`, `shell.sandbox` (all three are seeded into
    sub-agent shells by `subagent.child_tools`) and replaced wholesale in
    `tool_context`, so a wrapper would have to re-expose every attribute and
    would miss one silently. Inheriting keeps the session *is-a* shell for
    every existing consumer and needs exactly one override.

    Sub-agents ride the same trail: `sub_context` receives `wrap_shell` so
    every shell the run builds -- parent or child -- is one of these.

    The command record is written before the command runs, and the outcome in
    a second line after it: a command that hangs and is killed has still
    *run*, and a trail that only records successes is a trail for the one
    case that needs it least.
    """

    def __init__(self, inner: ShellSession, audit: AuditLog, actor: str) -> None:
        # `ShellSession.__init__` is cooperative about state: cwd/env/timeout/
        # sandbox are plain attributes, set from the session we are standing in
        # for so the audit shell starts exactly where the real one did.
        super().__init__(cwd=inner.cwd, timeout=inner.timeout, sandbox=inner.sandbox)
        self.env = dict(inner.env)
        # Read back by `apply_patch` (tools.py) through `getattr(ctx.shell,
        # ...)`: the patch path is a thread-pool call, not a shell command, so
        # it cannot ride `run`; it rides the session that knows the actor.
        self.audit = audit
        self.actor = actor

    async def run(self, command: str) -> str:
        self.audit.append("command", self.actor, command=command[:500], cwd=self.cwd)
        output = await super().run(command)
        # `last_exit` is the structured outcome `ShellSession.run` records;
        # `None` for a cd-only call or a killed command, which the audit line
        # reports as such rather than guessing.
        self.audit.append(
            "command_done",
            self.actor,
            command=command[:500],
            exit=self.last_exit,
        )
        return output


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
    audit: AuditLog | None = None,
    actor: str = "-",
) -> RunResult:
    # Already absolute, already resolved, already known to exist:
    # `routes._validated_workspace` did all three when the thread was created.
    # Validation belongs where the string enters the program, not where it is
    # used -- doing it here is how a typed-in typo silently created a
    # directory tree anywhere the server user could write.
    root = workspace_root
    context_window = settings.get("context_window")

    session = Session(
        mode=settings["sandbox_mode"],
        policy=settings["approval_policy"],
        rules=RuleStore(root / ".minicodex" / "rules.json"),
        approver=WebApprover(broker, emit, audit=audit, actor=actor),
    )

    # The turn itself is the first audit record: everything after it happened
    # because this account asked.  Written *before* any model call, so the
    # trail shows the ask even when the turn dies before answering -- and an
    # OSError from this append propagates, which is the invariant: audit
    # records cannot be dropped to keep a turn moving.
    if audit is not None:
        audit.append(
            "turn_start",
            actor,
            workspace=str(root),
            question=question[:200],
            sandbox_mode=settings["sandbox_mode"],
        )

    # The turn itself is the first audit record: everything after it happened
    # because this account asked. Written *before* any model call, so the
    # trail shows the ask even when the turn dies before answering. An
    # OSError from this append propagates, which is the invariant: audit
    # records cannot be dropped to keep a turn moving.
    if audit is not None:
        audit.append(
            "turn_start",
            actor,
            workspace=str(root),
            question=question[:200],
            sandbox_mode=settings["sandbox_mode"],
        )

    # Every command the model runs goes through `context.shell`; a shell
    # built without a sandbox runs on the host, so the kernel boundary the
    # mode names arrives here or not at all.
    #
    # The read roots are the directories the *tools* legitimately reach
    # outside the workspace: memory is per owner and lives under the tenant
    # home, so a confined shell must still be able to read it there or the
    # `read_file` path and the shell's kernel path disagree. Skills stay
    # per-project inside the workspace and need nothing extra.
    if settings["sandbox_mode"] == "full-access":
        # Not wrapped at all by `wrap()`, so there is nothing to build or
        # probe: the mode *is* the decision to run without a boundary, made
        # on the thread's settings where the approvals for it are made too.
        sandbox: Sandbox | None = None
        emit({"type": "notice", "text": "sandbox: full-access -- commands run unconfined"})
    else:
        sandbox = sandbox_for_mode(
            settings["sandbox_mode"],
            root,
            read_roots=(owner.root(),),
        )
    # Whether the boundary is real, as opposed to configured: an unavailable
    # sandbox (no bwrap) degrades every confined session to asking, which is
    # the fail-safe direction.
    confined = sandbox is not None and sandbox.unavailable() is None

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

    def audited(shell: ShellSession) -> ShellSession:
        # Every shell of this run -- the parent's and every sub-agent's --
        # passes through here, so the audit trail records child commands too.
        shell.sandbox = sandbox
        return _AuditedShell(shell, audit, actor)

    if audit is not None:
        context.shell = audited(context.shell)
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
        wrap_shell=audited if audit is not None else None,
        sessions_dir=thread_dir,
        parent_session_id=current.session_id,
        provider=provider_name,
        model=model,
        announce=announce,
    )

    task_plan = TaskPlan()
    tools = top_level_tools(
        root,
        session,
        sub_ctx,
        plan=task_plan,
        memory=memory,
        remember=remember,
        skills=skills,
        sandbox=sandbox,
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
                sandbox=sandbox,
                owner=owner,
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
        # Both, not just the parent: a child whose wiring differs from its
        # parent's is a child that never compacts, which is the version of
        # drift that costs money.
        sub_ctx = replace(sub_ctx, wiring=wiring)

    resume_from: History | None = None
    try:
        previous_path = resolve("last", thread_dir)
    except RolloutError:
        pass  # first turn in this thread
    else:
        outcome = resume_from_rollout(previous_path, current)
        resume_from = outcome.history
        for line in outcome.notes:
            announce(line)
        current = replace(current, forked_from=outcome.meta.session_id)

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
    # Three watchers, one hook -- `__main__._ask`'s arrangement. Each watcher
    # decides for itself whether it has anything to say this turn.
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

    # Two things the browser must see that are not history items: the plan,
    # and the sandbox mode `request_permissions` can rewrite mid-turn.
    # Derived by comparing against the last value after every item is
    # durable, rather than by a callback the core would have to grow. It
    # costs one tuple comparison per item and it means neither feature needed
    # a hook.
    writer.after_item = _derived(task_plan, session, emit)

    agent = wiring.agent(
        llm,
        tools,
        instructions=_instructions(session, tools, memory=memory, skills=skills, confined=confined),
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
                jobs_path=owner.jobs_db(),
                lock_path=owner.merge_lock(),
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

    A key that came from `OPENAI_API_KEY` is read at call time and never
    written down, so clearing the form in the browser cannot copy the
    environment's key into `providers.json`.
    """
    if provider.get("api_key"):
        return str(provider["api_key"])
    if provider.get("provider") == "openai":
        return os.environ.get("OPENAI_API_KEY")
    return None
