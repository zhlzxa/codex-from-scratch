"""Interlude B: the cycle, the drift it was dodged with, and the checker.

Three faults, and the order matters. FB-01 is a circular import -- the one
thing in this chapter that announces itself. FB-02 is the same boundary being
broken again three months later, which is the reason FB-01's fix is a script
and not a paragraph. FB-03 is the abstraction that gets introduced to break a
cycle and then turns out to have one implementation.

Nothing here touches the network.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest

from minicodex.agent import Wiring
from minicodex.agent_types import STATEFUL, Footprint, ToolCall, ToolSet
from minicodex.approval import AllowAll, Session
from minicodex.composition import local_tools, sub_context, top_level_tools
from minicodex.model import Completed, TextDelta, ToolCallDelta
from minicodex.recorder import Recorder
from minicodex.shell import ShellSession
from minicodex.subagent import TaskSpec, child_tools, run_task, spawn_toolset

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"
PKG = SRC / "minicodex"
CHECKER = ROOT / "scripts" / "check_layers.py"


def _load_checker() -> Any:
    """Import `scripts/check_layers.py` by path.

    It lives outside the package on purpose -- it is a development tool, and
    shipping it in the wheel would mean users install a thing that reads our
    source tree. That makes it un-importable by name, so it gets loaded the
    long way here rather than being moved somewhere convenient.
    """
    spec = importlib.util.spec_from_file_location("check_layers", CHECKER)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


checker = _load_checker()


def a_copy_of_src(tmp_path: Path, *edits: tuple[str, str, str]) -> Path:
    """A copy of `src/minicodex` with edits applied, for breaking on purpose.

    A copy rather than the working tree, and that is chapter 6's lesson rather
    than caution: the mutation script there restored from a `finally`, which
    does not run when the parent is killed, and left `if False:` sitting in
    `tokens.py`.
    """
    copy = tmp_path / "src" / "minicodex"
    shutil.copytree(PKG, copy)
    for filename, old, new in edits:
        path = copy / filename
        text = path.read_text(encoding="utf-8")
        assert old in text, f"{filename}: pattern not found"
        path.write_text(text.replace(old, new, 1), encoding="utf-8")
    return copy


# The two arrows that make a cycle: `tools` wanting `run_task` so that the
# spawn handler can live with the other handlers, and `subagent` wanting
# `bind_all` to build a child's tool table. Either alone is fine. Together
# the package stops importing.
BOTH_ARROWS = (
    (
        "tools.py",
        "from minicodex.agent_types import",
        "from minicodex.subagent import run_task  # noqa: F401\nfrom minicodex.agent_types import",
    ),
    (
        "subagent.py",
        "from minicodex.agent import Model, Wiring",
        "from minicodex.agent import Model, Wiring\nfrom minicodex.tools import bind_all"
        "  # noqa: F401",
    ),
)


class ScriptedModel:
    def __init__(self, turns: Sequence[Any]) -> None:
        self.turns = list(turns)
        self.sent: list[list[dict[str, Any]]] = []
        self.model = "scripted"

    async def stream(self, messages: Sequence[dict[str, Any]]) -> Any:
        self.sent.append([dict(m) for m in messages])
        turn = self.turns[min(len(self.sent) - 1, len(self.turns) - 1)]
        if isinstance(turn, str):
            yield TextDelta(turn)
        else:
            for index, (call_id, name, arguments) in enumerate(turn):
                yield ToolCallDelta(
                    call_id=call_id, index=index, name=name, arguments=json.dumps(arguments)
                )
        yield Completed("stop")


def a_context(root: Path, model: Any, wiring: Wiring) -> Any:
    return sub_context(
        build_model=lambda _schemas: model,
        root=root,
        session=Session(mode="workspace-write", approver=AllowAll()),
        parent_shell=ShellSession(),
        wiring=wiring,
        max_turns=8,
    )


# ---------------------------------------------------------------------------
# FB-01  the circular import, and the price of stepping around one
# ---------------------------------------------------------------------------


def test_FB_01_subagent_does_not_import_tools() -> None:
    """The arrow interlude B inverted.

    `subagent.py` used to import `bind_all`, `tool_context`, `tool_schemas` and
    `ToolSpec` from `tools.py`. One arrow, and it made the obvious home for the
    `spawn_agent` handler -- next to every other handler -- a circular import
    that stops the package loading:

        ImportError: cannot import name 'ToolContext' from partially
        initialized module 'minicodex.tools' (most likely due to a circular
        import)

    Chapter 10 dodged it by moving the tool up into `__main__`. This asserts
    the arrow is gone, which is what makes the dodge unnecessary.
    """
    imports = checker.intra_package_imports(PKG / "subagent.py")
    assert "minicodex.tools" not in imports


def test_FB_01_a_child_is_given_the_parents_recorder(tmp_path: Path) -> None:
    """F-1-04, off for exactly the conversation a human never watches.

    Before this, `run_task` built its `Agent` with three of the ten arguments
    `__main__` passes, and `recorder` was not one of them. A sub-agent's whole
    conversation existed nowhere afterwards.
    """
    recorder = Recorder(tmp_path / "rec")
    model = ScriptedModel(["done"])
    ctx = a_context(tmp_path, model, Wiring(recorder=recorder))
    asyncio.run(run_task(TaskSpec("count the files", "a number"), ctx))

    written = recorder.path.read_text(encoding="utf-8")
    assert "count the files" in written


def test_FB_01_a_child_compacts_like_its_parent(tmp_path: Path) -> None:
    """The measured half: 181,040 characters against 76,896, same script."""
    (tmp_path / "big.txt").write_text("x" * 60_000, encoding="utf-8")
    script = [
        [("c1", "read_file", {"path": "big.txt"})],
        [("c2", "read_file", {"path": "big.txt"})],
        [("c3", "read_file", {"path": "big.txt"})],
        "done",
    ]

    async def summarise(_request: Any) -> str:
        return "## Done: read the file three times"

    def final_size(wiring: Wiring) -> int:
        model = ScriptedModel(script)
        asyncio.run(
            run_task(TaskSpec("read it three times", "ok"), a_context(tmp_path, model, wiring))
        )
        return sum(len(json.dumps(m)) for m in model.sent[-1])

    unwired = final_size(Wiring())
    wired = final_size(Wiring(context_window=32_000, summariser=summarise))
    assert wired < unwired / 2, (unwired, wired)


def test_FB_01_a_child_gets_the_schedulers_footprints(tmp_path: Path) -> None:
    """The third channel, which travelled with neither of the other two.

    `child_tools` used to return `(handlers, schemas)`, so `run_task` had no
    `footprint_of` to pass and every sub-agent ran its tools one at a time.
    Safe -- chapter 8's default for an unclassified call is `STATEFUL` -- and
    silently slower, which is the kind of wrong nothing reports.
    """
    (tmp_path / "a.txt").write_text("a", encoding="utf-8")
    tools = child_tools(a_context(tmp_path, ScriptedModel(["ok"]), Wiring()))
    call = ToolCall("c1", "read_file", {"path": "a.txt"}, "{}")
    footprint = tools.footprint_of(call)
    assert footprint != STATEFUL
    assert footprint.reads == frozenset({str((tmp_path / "a.txt").resolve())})


def test_FB_01_a_toolset_refuses_a_handler_with_no_schema() -> None:
    """The invariant that makes `ToolSet` more than a named tuple.

    Chapter 4 paid for this pair drifting; here it becomes impossible to
    construct rather than a thing to remember. It fired for real on its first
    run, on a *test* -- chapter 10's timeout test patched a fake `sleep`
    handler in and gave it no schema.
    """
    with pytest.raises(ValueError, match="handler with no schema"):
        ToolSet(handlers={"ghost": None}, schemas=[])  # type: ignore[dict-item]


def test_FB_01_a_toolset_refuses_a_schema_with_no_handler() -> None:
    """The other direction produces chapter 0's error for a tool that exists."""
    schema = {"type": "function", "function": {"name": "ghost", "parameters": {}}}
    with pytest.raises(ValueError, match="schema with no handler"):
        ToolSet(handlers={}, schemas=[schema])


def test_FB_01_a_deferred_tool_is_the_one_named_exception() -> None:
    """Chapter 9's registry: callable before its schema is visible."""
    schema = {"type": "function", "function": {"name": "ghost", "parameters": {}}}
    tools = ToolSet(
        handlers={"ghost": None, "hidden": None},  # type: ignore[dict-item]
        schemas=[schema],
        callable_without_schema=frozenset({"hidden"}),
    )
    assert "hidden" in tools.handlers


def test_FB_01_plus_refuses_a_duplicate_name(tmp_path: Path) -> None:
    """F09-01's answer, applied to a local/remote collision."""
    session = Session(mode="read-only", approver=AllowAll())
    base = local_tools(tmp_path, session)
    with pytest.raises(ValueError, match="both declare"):
        base.plus(base)


def test_FB_01_plus_routes_a_footprint_to_the_set_that_owns_it(tmp_path: Path) -> None:
    """Composition must not silently drop one side's classifier.

    The old `route_footprint` did the same job for MCP with an `is_remote()`
    name test. `plus` does it by ownership, so a third source needs no new
    branch anywhere.
    """
    (tmp_path / "a.txt").write_text("a", encoding="utf-8")
    session = Session(mode="read-only", approver=AllowAll())
    ctx = a_context(tmp_path, ScriptedModel(["ok"]), Wiring())
    combined = top_level_tools(tmp_path, session, ctx)

    read = combined.footprint_of(ToolCall("c1", "read_file", {"path": "a.txt"}, "{}"))
    spawn = combined.footprint_of(ToolCall("c2", "spawn_agent", {"task": "t"}, "{}"))
    unknown = combined.footprint_of(ToolCall("c3", "nothing", {}, "{}"))

    assert read.reads and not read.stateful
    assert spawn == STATEFUL
    assert unknown == STATEFUL


# ---------------------------------------------------------------------------
# FB-02  the boundary broken again, three months later
# ---------------------------------------------------------------------------


def test_FB_02_the_checker_passes_on_this_package() -> None:
    result = subprocess.run(
        [sys.executable, str(CHECKER), "--quiet"], capture_output=True, text=True
    )
    assert result.returncode == 0, result.stderr


def test_FB_02_a_convenience_import_in_init_is_caught() -> None:
    """The measured failure, in one line nobody would flag in review.

    `from minicodex import Agent` is what every Python package grows so that
    callers do not have to know which module a name lives in. Adding it here
    makes `import minicodex.tools` raise, because `compaction.py` does
    `from minicodex import compaction_prompt` while the package is still
    half-built.
    """
    with tempfile.TemporaryDirectory() as tmp:
        broken = a_copy_of_src(
            Path(tmp),
            (
                "__init__.py",
                "from pathlib import Path",
                "from pathlib import Path\n\nfrom minicodex.agent import Agent",
            ),
        )
        problems = checker.check_init_is_a_leaf(broken)
        assert problems and "__init__.py imports" in problems[0]
        assert checker.check_cycles(broken), "the cycle check must see it too"


def test_FB_02_a_package_smoke_test_would_not_have_caught_the_other_one() -> None:
    """Why the checker imports every module and not just the package.

    With `tools` and `subagent` importing each other, `import minicodex`
    **exits 0** -- the package's `__init__` is a leaf, so the top-level import
    never touches the broken part. The obvious CI check passes on a package
    that cannot be used.
    """
    with tempfile.TemporaryDirectory() as tmp:
        broken = a_copy_of_src(Path(tmp), *BOTH_ARROWS)
        env = {**checker._env(), "PYTHONPATH": str(broken.parent)}
        package = subprocess.run(
            [sys.executable, "-c", "import minicodex"],
            capture_output=True,
            text=True,
            env=env,
        )
        assert package.returncode == 0, "the smoke test is supposed to pass here"

        problems = checker.check_every_module_imports(broken)
        assert problems, "the per-module check must not"
        assert any("minicodex.tools" in p for p in problems)


def test_FB_02_a_deferred_import_is_still_an_edge() -> None:
    """The repair that makes a cycle stop failing and start lurking.

    Moving `from minicodex.subagent import run_task` inside a function body
    makes the package import cleanly again. The cycle is still there; it now
    fails on the first call instead of at startup, which is later, in front of
    a user, and in a stack trace about something else.
    """
    with tempfile.TemporaryDirectory() as tmp:
        deferred = a_copy_of_src(
            Path(tmp),
            (
                "tools.py",
                "def bind_all(",
                "def _late() -> None:\n"
                "    from minicodex.subagent import run_task  # noqa: F401\n\n\n"
                "def bind_all(",
            ),
            BOTH_ARROWS[1],
        )
        assert not checker.check_every_module_imports(deferred), "it imports fine, that is the trap"
        assert checker.check_cycles(deferred), "the AST check must still see the edge"


@pytest.mark.parametrize(
    "filename,old,new,rule",
    [
        (
            "subagent.py",
            "from minicodex.agent import Model, Wiring",
            "from minicodex.agent import Model, Wiring\nfrom minicodex.tools import bind_all"
            "  # noqa: F401",
            "check_forbidden",
        ),
        (
            "agent_types.py",
            "from collections.abc import Awaitable, Callable",
            "from collections.abc import Awaitable, Callable\n"
            "from minicodex.tool_errors import tool_error  # noqa: F401",
            "check_forbidden",
        ),
        (
            "subagent.py",
            "child = ctx.wiring.agent(",
            "from minicodex.agent import Agent\n\n    child = Agent(",
            "check_one_agent_construction",
        ),
    ],
)
def test_FB_02_each_rule_can_fail(filename: str, old: str, new: str, rule: str) -> None:
    """A check nobody has seen fail is a check nobody knows works.

    Chapters 5, 6, 9 and 10 each shipped a test whose name claimed more than
    its assertions, every one of them found by mutation rather than by
    reading. A boundary checker is the same shape of thing -- it reports
    "ok" by default -- so each of its rules gets a mutation that must turn it
    red.
    """
    with tempfile.TemporaryDirectory() as tmp:
        broken = a_copy_of_src(Path(tmp), (filename, old, new))
        assert getattr(checker, rule)(broken), f"{rule} did not notice {filename}"


def test_FB_02_every_forbidden_edge_names_the_fault_it_came_from() -> None:
    """The rule that keeps the rule list honest.

    An architecture check accumulates entries, and an entry with no reason is
    an entry nobody can ever delete -- it will be obeyed forever by people
    guessing at what it was for. Every edge in `FORBIDDEN` carries a fault ID
    from FAULTS.md, so each one can be looked up, and argued with.
    """
    for source, target, why in checker.FORBIDDEN:
        assert any(marker in why for marker in ("F0", "F1", "FA-", "FB-")), (source, target, why)


def test_FB_02_the_checker_runs_in_ci() -> None:
    """A check that is not wired in is a file.

    In the lint step rather than a step of its own: chapter -1's guard caps
    the blocking workflow at six steps, and a sub-second static check is what
    a lint step is for. Chapter 9 answered the same question differently
    (a second workflow) because a mutation run is not a lint.
    """
    workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    assert "scripts/check_layers.py" in workflow


# ---------------------------------------------------------------------------
# FB-03  the interface with one implementation
# ---------------------------------------------------------------------------


def test_FB_03_the_shared_thing_is_a_value_with_three_producers(tmp_path: Path) -> None:
    """`ToolSet` is a data type, not an interface, and that is the point.

    The reflex when breaking a cycle is to declare a `Protocol` -- something
    like `ToolProvider` with `handlers()`, `schemas()` and `footprint()` --
    and have both sides depend on it. That was designed and dropped: the
    three producers here (`tools`, `subagent`, `registry`) do not need to be
    objects, they need to *return* the same shape. An interface with one real
    implementation is FB-03; three functions returning one value type is not
    an interface at all, and has nothing to subclass.

    This test counts the producers, because "three call sites" is the claim
    the design rests on and a claim in a docstring rots.
    """
    session = Session(mode="read-only", approver=AllowAll())
    ctx = a_context(tmp_path, ScriptedModel(["ok"]), Wiring())

    produced = [
        local_tools(tmp_path, session),
        spawn_toolset(ctx),
        child_tools(ctx),
    ]
    assert all(isinstance(t, ToolSet) for t in produced)
    assert len({tuple(sorted(t.handlers)) for t in produced}) == 3


def test_FB_03_wiring_carries_only_what_two_call_sites_both_need() -> None:
    """A guard against the other failure mode: a bag that grows.

    `Wiring` earns its existence by being the difference between two agent
    constructions, not by being "config". Every field on it is something a
    sub-agent was measured to be missing, or -- `dialect` -- something that
    must not differ between a parent and its child. Four keyword arguments
    stayed outside it, because those are the ones that genuinely differ.

    Chapter 12 took it from five to seven, and this assertion going red is
    what made that a decision rather than a habit. Both additions are the
    `dialect` kind: a child with its own retry policy is exactly the drift
    interlude B measured, arranged on purpose, and a backoff nobody is told
    about is a hang -- whether it happens in the parent's loop or three
    levels down.
    """
    fields = set(Wiring.__dataclass_fields__)
    assert fields == {
        "recorder",
        "context_window",
        "summariser",
        "max_concurrent_tools",
        "dialect",
        "retry_policy",
        "announce",
    }


def test_FB_03_footprints_did_not_get_their_own_abstraction(tmp_path: Path) -> None:
    """The rejected inversion, pinned so it stays rejected.

    `route_footprint(registry, local)` used to exist in `registry.py` for
    exactly one caller. It is gone: `ToolSet.plus` routes by ownership, which
    needs no name test, no import of `is_remote` at the call site, and no
    branch when a fourth source arrives. Removing a function is a refactor
    result worth asserting -- otherwise somebody re-adds it next to the thing
    that replaced it.
    """
    import minicodex.registry as registry_module

    assert not hasattr(registry_module, "route_footprint")


def test_FB_03_the_footprint_type_stayed_where_behaviour_can_use_it() -> None:
    """Moving a type down must not move the behaviour with it.

    `Footprint`, `STATEFUL` and `FootprintFn` live in `agent_types.py` now, so
    that `subagent.py` can name them without importing a module it depends on
    for nothing else. `conflicts` and `batches` did not move: they are what
    the scheduler *is*.
    """
    import minicodex.scheduler as scheduler_module

    assert scheduler_module.Footprint is Footprint
    assert callable(scheduler_module.conflicts)
    assert "conflicts" not in dir(sys.modules["minicodex.agent_types"])
