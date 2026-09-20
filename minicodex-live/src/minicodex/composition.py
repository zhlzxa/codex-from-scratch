"""Where a run is assembled: tools in, one `ToolSet` out, for parents and children.

This module exists because there were two places that built an agent and they
disagreed, silently (see docs/history.md for what that cost).  So this module
owns the composition and both entry points call it.  It is the top of the
package: it imports `agent`, `registry`, `subagent` and `tools`, and none of
them imports it.  That direction is what lets `subagent` stop importing
`tools`, which is what makes the `spawn_agent` handler movable again.

Deliberately not here: argument parsing, printing, the session file, the
approval prompt.  A composition root that also does IO is a composition root
you cannot call from a test, which is the state that let the drift above go
unnoticed.
"""

from __future__ import annotations

import functools
from collections.abc import Callable
from pathlib import Path
from typing import Any

from minicodex import system_prompt
from minicodex.agent import Wiring
from minicodex.agent_types import ToolCall, ToolFn, ToolSet
from minicodex.approval import Session, permissions_block
from minicodex.memory import Memory, memory_instructions, memory_toolset
from minicodex.memory_write import NOTE_NAME, note_toolset, remember_instructions
from minicodex.plan import PLAN_INSTRUCTIONS, TaskPlan, plan_toolset
from minicodex.registry import McpRegistry, is_remote
from minicodex.sandbox import Sandbox
from minicodex.shell import ShellSession
from minicodex.skills import Skills, skill_toolset, skills_instructions
from minicodex.subagent import SubAgentContext, spawn_toolset
from minicodex.tools import _UNSET, TOOL_SCHEMAS, bind_all, footprint_of, tool_context, tool_schemas


def instructions(
    session: Session,
    tools: ToolSet | None = None,
    memory: Memory | None = None,
    skills: Skills | None = None,
    confined: bool = False,
) -> str:
    """The system message: what the agent is, then what it may currently do.

    Prompt assembly is a core concern, and the one function both entry points
    call -- it used to exist once per entry point, verbatim, with an equality
    test standing guard over the copy.

    Permission state goes last, and that is not a layout preference.  It is
    the only part of this string that changes during a session -- and
    providers cache a prompt by its prefix, so volatile content near the top
    invalidates the cache on the turn it changes.

    Each conditional paragraph names a tool only when the tool exists: the
    plan/memory/skills/note wording is read off the tool table rather than
    recomputed here, because the two must agree.

    `confined` selects the permission wording for a session whose shell the
    OS sandbox actually confines -- see `approval.permissions_block`.
    """
    can_request = any(tool["function"]["name"] == "request_permissions" for tool in TOOL_SCHEMAS)
    block = permissions_block(session, can_request=can_request, confined=confined)
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


def local_tools(
    root: Path,
    session: Session,
    shell: ShellSession | None = None,
    *,
    extra_read_roots: tuple[Path, ...] = (),
    sandbox: Sandbox | object | None = _UNSET,
) -> ToolSet:
    """This project's own tools, bound to one repository and one conversation.

    The three tables -- handlers, schemas, footprints -- are built in one
    expression, so a fourth tool cannot arrive in two of them.

    `extra_read_roots` widens only what `read_file` may read
    (`paths.resolve`), never what `apply_patch` may write. Memory is the
    caller that uses it.

    `sandbox` is forwarded to `tool_context` untouched, sentinel and all: the
    console is the caller that must be able to say "wrap commands" without
    also having to pre-build the `ShellSession`.  Passing `shell` and leaving
    `sandbox` unset keeps the shell's own sandbox -- the explicit object wins
    only when a shell is *not* supplied.
    """
    context = tool_context(
        root=root,
        session=session,
        shell=shell,
        extra_read_roots=extra_read_roots,
        sandbox=sandbox,
    )
    return ToolSet(
        handlers=bind_all(context),
        schemas=list(tool_schemas()),
        footprint_of=functools.partial(footprint_of, root=context.root),
    )


def child_tools_builder(root: Path, session: Session) -> Callable[[ShellSession], ToolSet]:
    """What `SubAgentContext.build_tools` gets: a child's local tools, given its shell.

    A partially applied function rather than the `SubAgentContext` itself,
    because the shell is the one thing the sub-agent module has to make (it
    seeds a fresh one from the parent's shell) and everything else is decided
    up here.  `spawn_agent` is *not* added by this function -- `child_tools`
    adds it, or does not, depending on depth.

    Sub-agents get no MCP tools, which is a decision and not an oversight: a
    remote tool's `Footprint` is a promise made by somebody else's code, and
    handing a child a resource the parent's scheduler cannot see is a
    concurrency hole with a second process in the way.  Written here, as an
    absence in one function, instead of being implied by which module imports
    what.
    """
    return lambda shell: local_tools(root, session, shell)


def with_remote_tools(base: ToolSet, registry: McpRegistry) -> ToolSet:
    """Add the MCP registry to a local tool set.

    Not `base.plus(...)`, and the reason is the one piece of asymmetry in the
    whole assembly: the registry owns a *live* schema list.  `registry.visible`
    is the object the model client holds by reference, and `tool_search`
    reveals a deferred tool by appending to it -- so the schemas of the merged
    set have to *be* that list, not a copy of it.  `McpRegistry(local=...)`
    takes `base.schemas` as input for exactly this reason, which is why the
    registry is constructed after the local set and not before.

    `callable_without_schema` is the honest form of the other half: a deferred
    tool has a handler and no visible schema, which `ToolSet.__post_init__`
    would otherwise refuse.  Naming the exception keeps the rule strict for
    everybody else instead of relaxing it for everybody.
    """
    # The one ordering rule in this module, said out loud. Building the
    # registry before the local set, or forgetting `local=`, produces a tool
    # set whose schemas are missing every local tool.
    if not registry.local:
        raise ValueError(
            "McpRegistry must be constructed with local=<the base set's schemas>; "
            "it owns the list the model client holds"
        )

    handlers = {**base.handlers, **registry.handlers()}
    if registry.deferred:
        handlers["tool_search"] = registry.search_handler()

    def routed(call: ToolCall) -> Any:
        return registry.footprint_of(call) if is_remote(call.name) else base.footprint_of(call)

    return ToolSet(
        handlers=handlers,
        schemas=registry.visible,
        footprint_of=routed,
        callable_without_schema=frozenset(registry.deferred),
    )


def watching(tools: ToolSet, plan: TaskPlan) -> ToolSet:
    """Tell the plan that a tool ran, whichever tool it was.

    The plan's one piece of evidence is "did anything happen between the last
    update and this one", and no single tool can answer that -- `update_plan`
    cannot see `apply_patch` being called, and `apply_patch` has never heard of
    a plan.  The place that can see all of them is the place that assembles
    them, which is here.

    A wrapper over the merged handler dict rather than a hook inside `Agent`:
    the loop already knows about a recorder, a rollout, a scheduler and a
    summariser, and "count tool calls for a feature two layers down" is not a
    fifth thing it should learn.  Applied last, after MCP tools have been
    merged in, so a remote tool counts as work exactly like a local one.
    """

    def watched(name: str, fn: ToolFn) -> ToolFn:
        async def call(args: dict[str, Any]) -> str:
            plan.record_work(name)
            return await fn(args)

        return call

    return ToolSet(
        handlers={name: watched(name, fn) for name, fn in tools.handlers.items()},
        schemas=tools.schemas,
        footprint_of=tools.footprint_of,
        callable_without_schema=tools.callable_without_schema,
    )


def top_level_tools(
    root: Path,
    session: Session,
    sub_context: SubAgentContext,
    plan: TaskPlan | None = None,
    memory: Memory | None = None,
    remember: Path | None = None,
    skills: Skills | None = None,
    *,
    dedicated_tools: bool = False,
    skill_tool: bool = False,
    sandbox: Sandbox | object | None = _UNSET,
) -> ToolSet:
    """Local tools plus `spawn_agent`, before any MCP server is consulted.

    Split from `with_remote_tools` because of the ordering above: the registry
    needs this set's schemas as its `local=` argument, so it cannot be built
    until this exists.

    `plan` is optional and defaults to absent.  A child never gets one:
    `child_tools_builder` does not call this function at all, which is the
    same shape as the missing MCP tools above -- an absence in one place
    instead of a flag in several.

    `memory` is optional for a second reason on top of that one: it is a
    feature that ships switched **off**. A default-on memory would change what
    every session sends before anybody had agreed to be remembered, and `None`
    here is what "the user did not ask for this" looks like from the
    composition root. A child gets none for the same reason it gets no plan.

    `dedicated_tools` is codex's own flag, `memories.dedicated_tools` (default
    `false` there too). It has nothing to do with whether the memory fits in
    the resident block. The default access path is `read_file`, already
    pointed at the memory directory by `extra_read_roots` below;
    `memory_search`/`memory_read` exist only when this flag is explicitly
    true, exactly like codex's.

    `skills` and `skill_tool` are the same shape one more time: a skill
    catalog prints each entry's path, `extra_read_roots` makes that path
    readable, and the model opens it with `read_file`. `skill_tool` adds
    `read_skill` on top, kept only because it measured better than the default
    path (see docs/history.md).
    """
    tools = local_tools(
        root,
        session,
        sub_context.parent_shell,
        extra_read_roots=tuple(
            directory
            for directory in (
                memory.directory if memory is not None else None,
                skills.directory if skills is not None else None,
            )
            if directory is not None
        ),
        sandbox=sandbox,
    ).plus(spawn_toolset(sub_context))
    if plan is not None:
        tools = tools.plus(plan_toolset(plan))
    # `memory` still has to be non-empty for the pair to be worth adding:
    # this asks "is there anything to search at all", not "did it fit a
    # budget".
    if memory is not None and memory.non_empty and dedicated_tools:
        tools = tools.plus(memory_toolset(memory))
    # The one model-facing write to memory anywhere in this program: it
    # appends a proposal to `notes/` and cannot touch `MEMORY.md`.  A
    # separate switch from `memory=` because reading and writing are separate
    # consents.
    if remember is not None:
        tools = tools.plus(note_toolset(remember))
    # Same two-part condition as the memory pair above, and it is not
    # symmetry for its own sake: `skill_tool` is the consent, `skills` being
    # non-empty is the precondition, and mounting a `read_skill` that can only
    # ever answer "no skill named that" is pointless.
    if skills is not None and skills.non_empty and skill_tool:
        tools = tools.plus(skill_toolset(skills))
    return tools


def sub_context(
    *,
    root: Path,
    session: Session,
    parent_shell: ShellSession,
    build_model: Callable[[list[dict[str, Any]]], Any],
    wiring: Wiring,
    wrap_shell: Callable[[ShellSession], ShellSession] | None = None,
    **rest: Any,
) -> SubAgentContext:
    """A `SubAgentContext` with `build_tools` already wired to this run.

    One function so that "a child gets the same tools and the same wiring as
    its parent" is a default rather than a thing to remember at each call
    site.  `**rest` carries the fields that are genuinely per-run -- session
    directory, provider and model names, the announcer -- straight through.

    `wrap_shell` forwards the caller's outermost shell wrapper to children;
    a run that audits every parent command audits its children's too.
    """
    return SubAgentContext(
        build_model=build_model,
        root=root,
        session=session,
        parent_shell=parent_shell,
        build_tools=child_tools_builder(root, session),
        wiring=wiring,
        wrap_shell=wrap_shell,
        **rest,
    )


__all__ = [
    "child_tools_builder",
    "local_tools",
    "sub_context",
    "top_level_tools",
    "watching",
    "with_remote_tools",
]
