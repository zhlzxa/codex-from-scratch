"""Where a run is assembled: tools in, one `ToolSet` out, for parents and children.

This module exists because there were two places that built an agent and they
disagreed, silently, for a whole chapter.  `__main__` composed three sources of
tools with three unrelated expressions -- a dict splat for handlers, a list
splat for schemas, a `route_footprint()` wrapper for the scheduler -- and
`subagent.child_tools` composed one source with two of the three.  Measured on
the same scripted model, the same three files and the same tools:

    child   final request 181,040 chars   compactions 0   transcript: none
    parent  final request  76,896 chars   compactions 1   transcript: written

Nothing raised.  Nobody was going to find that by reading `subagent.py`,
because `subagent.py` is *correct* on its own terms -- it is only wrong next
to a call site three modules away that nothing puts it next to.

So this module owns the composition and both sites call it.  It is the top of
the package: it imports `agent`, `registry`, `subagent` and `tools`, and none
of them imports it.  That direction is what lets `subagent` stop importing
`tools`, which is what makes the `spawn_agent` handler movable again.

Deliberately not here: argument parsing, printing, the session file, the
approval prompt.  Those stayed in `__main__`.  A composition root that also
does IO is a composition root you cannot call from a test, which is the state
this package was in when the drift above went unnoticed for a chapter.
"""

from __future__ import annotations

import functools
from collections.abc import Callable
from pathlib import Path
from typing import Any

from minicodex.agent import Wiring
from minicodex.agent_types import ToolCall, ToolFn, ToolSet
from minicodex.approval import Session
from minicodex.plan import TaskPlan, plan_toolset
from minicodex.registry import McpRegistry, is_remote
from minicodex.shell import ShellSession
from minicodex.subagent import SubAgentContext, spawn_toolset
from minicodex.tools import bind_all, footprint_of, tool_context, tool_schemas


def local_tools(
    root: Path,
    session: Session,
    shell: ShellSession | None = None,
) -> ToolSet:
    """This project's own tools, bound to one repository and one conversation.

    The three tables that used to be assembled separately -- `bind_all()`,
    `tool_schemas()`, `functools.partial(footprint_of, root=root)` -- built in
    one expression, so that a fourth tool cannot arrive in two of them.
    """
    context = tool_context(root=root, session=session, shell=shell)
    return ToolSet(
        handlers=bind_all(context),
        schemas=list(tool_schemas()),
        footprint_of=functools.partial(footprint_of, root=context.root),
    )


def child_tools_builder(root: Path, session: Session) -> Callable[[ShellSession], ToolSet]:
    """What `SubAgentContext.build_tools` gets: a child's local tools, given its shell.

    A partially applied function rather than the `SubAgentContext` itself,
    because the shell is the one thing the sub-agent module has to make (it
    seeds a fresh one from the parent's working directory) and everything else
    is decided up here.  `spawn_agent` is *not* added by this function --
    `child_tools` adds it, or does not, depending on depth, which is the one
    decision that genuinely belongs down there.

    Sub-agents get no MCP tools, which is chapter 10's decision and is left
    alone: a remote tool's `Footprint` is a promise made by somebody else's
    code, and handing a child a resource the parent's scheduler cannot see is
    F08-06 with a second process in the way.  Written here, as an absence in
    one function, instead of being implied by which module imports what.
    """
    return lambda shell: local_tools(root, session, shell)


def with_remote_tools(base: ToolSet, registry: McpRegistry) -> ToolSet:
    """Add chapter 9's registry to a local tool set.

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
    # set whose schemas are missing every local tool -- which `ToolSet` does
    # refuse, but with a message about four handlers rather than about the two
    # lines that are in the wrong order.
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
) -> ToolSet:
    """Local tools plus `spawn_agent`, before any MCP server is consulted.

    Split from `with_remote_tools` because of the ordering above: the registry
    needs this set's schemas as its `local=` argument, so it cannot be built
    until this exists.

    `plan` is optional and defaults to absent, so every test written before
    chapter 11 keeps describing the tool table it was written for.  A child
    never gets one: `child_tools_builder` does not call this function at all,
    which is the same shape as the missing MCP tools above -- an absence in one
    place instead of a flag in several.
    """
    tools = local_tools(root, session, sub_context.parent_shell).plus(spawn_toolset(sub_context))
    if plan is not None:
        tools = tools.plus(plan_toolset(plan))
    return tools


def sub_context(
    *,
    root: Path,
    session: Session,
    parent_shell: ShellSession,
    build_model: Callable[[list[dict[str, Any]]], Any],
    wiring: Wiring,
    **rest: Any,
) -> SubAgentContext:
    """A `SubAgentContext` with `build_tools` already wired to this run.

    One function so that "a child gets the same tools and the same wiring as
    its parent" is a default rather than a thing to remember at each call
    site.  `**rest` carries the fields that are genuinely per-run -- session
    directory, provider and model names, the announcer -- straight through.
    """
    return SubAgentContext(
        build_model=build_model,
        root=root,
        session=session,
        parent_shell=parent_shell,
        build_tools=child_tools_builder(root, session),
        wiring=wiring,
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
