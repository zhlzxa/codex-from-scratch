"""Interlude B: measuring the shape of the package before changing it.

Six sections, each runnable on its own:

    uv run python probe_boundaries.py graph        the import graph as it stands
    uv run python probe_boundaries.py init-cycle   one ordinary line in __init__.py
    uv run python probe_boundaries.py tool-cycle   the cycle chapter 10 stepped around
    uv run python probe_boundaries.py repairs      four repairs, each tried
    uv run python probe_boundaries.py drift        how many places build an Agent
    uv run python probe_boundaries.py drift-live   what the wiring is worth, measured

Sections that edit source do it in a *copy* of `src/` inside a temporary
directory, and run the experiment in a subprocess pointed at that copy.
Chapter 6 paid for the version that edited the working tree and was killed
before it put it back.
"""

from __future__ import annotations

import ast
import shutil
import subprocess
import sys
import tempfile
from collections import defaultdict
from pathlib import Path

HERE = Path(__file__).resolve().parent
SRC = HERE.parent / "src"
PKG = SRC / "minicodex"


# ---------------------------------------------------------------------------
# the graph
# ---------------------------------------------------------------------------


def imports_of(path: Path, package: str = "minicodex") -> set[str]:
    """Every intra-package module this file imports, at any nesting level.

    `ast.walk`, not a scan of the top-level body: an import written inside a
    function is still an edge.  It is a *deferred* edge, which is exactly why
    it is worth seeing -- deferring an import is the standard way to make a
    cycle stop failing at import time and start failing on the first call.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(a.name for a in node.names if a.name.split(".")[0] == package)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            if node.module.split(".")[0] != package:
                continue
            if node.module == package:
                # `from minicodex import compaction_prompt` -- the name may be
                # a submodule or an attribute of the package.  Either way the
                # edge is to the package, whose __init__ has to run first.
                found.add(package)
            else:
                found.add(node.module)
    return found


def build_graph(pkg: Path = PKG) -> dict[str, set[str]]:
    graph: dict[str, set[str]] = {}
    for path in sorted(pkg.glob("*.py")):
        name = "minicodex" if path.stem == "__init__" else f"minicodex.{path.stem}"
        graph[name] = imports_of(path)
    known = set(graph)
    return {k: {v for v in vs if v in known and v != k} for k, vs in graph.items()}


def cycles(graph: dict[str, set[str]]) -> list[list[str]]:
    """Tarjan, iterative -- the recursive version dies on a package this size
    only if the graph is deep, but a cycle finder that can itself blow up is
    not the tool you want when the graph is already wrong."""
    index: dict[str, int] = {}
    low: dict[str, int] = {}
    on_stack: set[str] = set()
    stack: list[str] = []
    out: list[list[str]] = []
    counter = 0

    for root in graph:
        if root in index:
            continue
        work: list[tuple[str, list[str]]] = [(root, list(graph[root]))]
        index[root] = low[root] = counter
        counter += 1
        stack.append(root)
        on_stack.add(root)
        while work:
            node, pending = work[-1]
            if pending:
                child = pending.pop()
                if child not in index:
                    index[child] = low[child] = counter
                    counter += 1
                    stack.append(child)
                    on_stack.add(child)
                    work.append((child, list(graph[child])))
                elif child in on_stack:
                    low[node] = min(low[node], index[child])
            else:
                work.pop()
                if work:
                    low[work[-1][0]] = min(low[work[-1][0]], low[node])
                if low[node] == index[node]:
                    comp = []
                    while True:
                        top = stack.pop()
                        on_stack.discard(top)
                        comp.append(top)
                        if top == node:
                            break
                    if len(comp) > 1 or node in graph[node]:
                        out.append(sorted(comp))
    return out


def levels(graph: dict[str, set[str]]) -> dict[str, int]:
    depth: dict[str, int] = {}

    def walk(node: str, seen: frozenset[str]) -> int:
        if node in depth:
            return depth[node]
        if node in seen:
            return 0
        d = max((1 + walk(c, seen | {node}) for c in graph[node]), default=0)
        depth[node] = d
        return d

    return {n: walk(n, frozenset()) for n in graph}


def section_graph() -> None:
    graph = build_graph()
    fan_in: dict[str, int] = defaultdict(int)
    for deps in graph.values():
        for dep in deps:
            fan_in[dep] += 1
    depth = levels(graph)
    print(f"{len(graph)} modules, {sum(len(v) for v in graph.values())} edges")
    print(f"cycles: {cycles(graph) or 'none'}")
    print()
    print(f"{'level':>5}  {'module':18} {'out':>4} {'in':>4}")
    for name in sorted(graph, key=lambda n: (depth[n], n)):
        short = name.split(".")[-1] if "." in name else "__init__"
        print(f"{depth[name]:>5}  {short:18} {len(graph[name]):>4} {fan_in[name]:>4}")


# ---------------------------------------------------------------------------
# experiments that need an edited copy of the source
# ---------------------------------------------------------------------------


def in_a_copy(edits: dict[str, tuple[str, str]], command: str) -> subprocess.CompletedProcess[str]:
    """Copy `src/`, apply `{filename: (old, new)}`, run `python -c command`."""
    with tempfile.TemporaryDirectory() as tmp:
        copy = Path(tmp) / "src"
        shutil.copytree(SRC, copy)
        for filename, (old, new) in edits.items():
            path = copy / "minicodex" / filename
            text = path.read_text(encoding="utf-8")
            assert old in text, f"{filename}: pattern not found -- {old[:60]!r}"
            path.write_text(text.replace(old, new, 1), encoding="utf-8")
        return subprocess.run(
            [sys.executable, "-c", command],
            capture_output=True,
            text=True,
            cwd=tmp,
            env={**_clean_env(), "PYTHONPATH": str(copy)},
        )


def _clean_env() -> dict[str, str]:
    import os

    keep = ("PATH", "SYSTEMROOT", "TEMP", "TMP", "HOME", "USERPROFILE", "APPDATA")
    return {k: v for k, v in os.environ.items() if k in keep}


def _report(label: str, result: subprocess.CompletedProcess[str]) -> None:
    print(f"--- {label}")
    print(f"    exit {result.returncode}")
    tail = (result.stderr or result.stdout).strip().splitlines()
    for line in tail[-6:]:
        print(f"    {line}")
    print()


# ---------------------------------------------------------------------------
# 2 -- one ordinary line in __init__.py
# ---------------------------------------------------------------------------

NICE_API = (
    "from pathlib import Path",
    "from pathlib import Path\n\nfrom minicodex.agent import Agent",
)


def section_init_cycle() -> None:
    print("Adding `from minicodex.agent import Agent` to __init__.py -- the line")
    print("every package grows when someone wants `from minicodex import Agent`.")
    print()
    for entry in ("import minicodex", "import minicodex.compaction", "import minicodex.tools"):
        _report(entry, in_a_copy({"__init__.py": NICE_API}, entry))

    with tempfile.TemporaryDirectory() as tmp:
        copy = Path(tmp) / "src"
        shutil.copytree(SRC, copy)
        path = copy / "minicodex" / "__init__.py"
        path.write_text(path.read_text(encoding="utf-8").replace(*NICE_API), encoding="utf-8")
        graph = build_graph(copy / "minicodex")
        print(f"and the graph checker on that same tree says: cycles = {cycles(graph) or 'none'}")


# ---------------------------------------------------------------------------
# 3 -- the cycle chapter 10 stepped around
# ---------------------------------------------------------------------------

# Interlude B removed one of the two arrows, so reproducing chapter 10's cycle
# now means putting it back.  Both edits together are the state the package was
# in before this unit: `subagent` needing `bind_all` to build a child's tool
# table, `tools` needing `run_task` so that the spawn handler can live with the
# other handlers.  Either arrow alone is fine.
SUBAGENT_NEEDS_TOOLS = (
    "subagent.py",
    (
        "from minicodex.agent import Model, Wiring",
        "from minicodex.agent import Model, Wiring\nfrom minicodex.tools import bind_all",
    ),
)
TOOLS_NEEDS_SUBAGENT = (
    "tools.py",
    (
        "from minicodex.agent_types import ToolCall, ToolFn",
        "from minicodex.agent_types import ToolCall, ToolFn\n"
        "from minicodex.subagent import run_task",
    ),
)
BOTH_ARROWS = dict([SUBAGENT_NEEDS_TOOLS, TOOLS_NEEDS_SUBAGENT])


def section_tool_cycle() -> None:
    print("Putting the spawn_agent handler where handlers live, in the package as")
    print("chapter 10 left it: `tools.py` gains `from minicodex.subagent import")
    print("run_task`, and `subagent.py` still imports `tools` for bind_all.")
    print()
    for entry in ("import minicodex.tools", "import minicodex.subagent", "import minicodex"):
        _report(entry, in_a_copy(BOTH_ARROWS, entry))
    print("The third line is the one to look at. `import minicodex` exits 0 on a")
    print("package whose modules cannot import each other, because __init__ is a")
    print("leaf -- so the obvious CI smoke test passes on a broken tree.")


# ---------------------------------------------------------------------------
# 4 -- four repairs
# ---------------------------------------------------------------------------

DEFERRED = dict(
    [
        SUBAGENT_NEEDS_TOOLS,
        (
            "tools.py",
            (
                "from minicodex.agent_types import ToolCall, ToolFn",
                "from minicodex.agent_types import ToolCall, ToolFn\n\n\n"
                "def _run_task(*args: object, **kwargs: object) -> object:\n"
                "    from minicodex.subagent import run_task\n\n"
                "    return run_task(*args, **kwargs)",
            ),
        ),
    ]
)


def _cycles_after(edits: dict[str, tuple[str, str]]) -> object:
    with tempfile.TemporaryDirectory() as tmp:
        copy = Path(tmp) / "src"
        shutil.copytree(SRC, copy)
        for filename, (old, new) in edits.items():
            path = copy / "minicodex" / filename
            path.write_text(path.read_text(encoding="utf-8").replace(old, new, 1), encoding="utf-8")
        return cycles(build_graph(copy / "minicodex")) or "none"


def section_repairs() -> None:
    print("(a) move the shared *type* down -- chapter 1's answer for ToolCall and")
    print("    interlude A's for ToolFn. What tools.py needs from subagent.py is")
    print("    `run_task`: a coroutine function that builds an Agent, runs it and")
    print("    interprets the result. Moving it down moves the module.")
    print()

    print("(b) move the *tool* up -- chapter 10's answer, into __main__. Works.")
    print("    Price measured in `drift`: a second place that builds an agent.")
    print()

    print("(c) defer the import into a function body:")
    _report(
        "import minicodex.tools",
        in_a_copy(DEFERRED, "import minicodex.tools; print('imported fine')"),
    )
    print(f"    and the AST checker still sees the edge: cycles = {_cycles_after(DEFERRED)}")
    print("    The cycle did not go away. It moved from startup to the first call.")
    print()

    print("(d) invert: subagent takes `build_tools` from its caller. What the two")
    print("    modules had to exchange is a *value* -- three tables that must")
    print("    agree -- so it went into agent_types.py with the other shared")
    print("    vocabulary, and the arrow disappeared. This is what shipped.")
    only_one = _cycles_after(dict([TOOLS_NEEDS_SUBAGENT]))
    print(f"    cycles with only the tools -> subagent arrow added: {only_one}")
    print()


# ---------------------------------------------------------------------------
# 5 -- what the second assembly site forgets
# ---------------------------------------------------------------------------


def _calls_to(path: Path, name: str) -> int:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return sum(
        1
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == name
    )


def section_drift() -> None:
    """How many places build an `Agent`, and how much each of them knows.

    Before interlude B: two, passing seven and three of the same ten keyword
    arguments.  After: one, `Wiring.agent`, which both call.
    """
    import inspect

    sys.path.insert(0, str(SRC))
    from minicodex.agent import Agent, Wiring

    accepted = set(inspect.signature(Agent.__init__).parameters) - {"self", "model", "tools"}
    carried = {f for f in Wiring.__dataclass_fields__} | {"footprint_of"}
    passed_per_call = set(inspect.signature(Wiring.agent).parameters) - {"self", "model", "tools"}

    sites = {
        path.name: _calls_to(path, "Agent")
        for path in sorted(PKG.glob("*.py"))
        if _calls_to(path, "Agent")
    }
    print(f"Agent.__init__ takes {len(accepted)} arguments beyond model and tools.")
    print(f"`Agent(...)` call sites in src/: {sites}")
    print()
    print(f"  carried by Wiring / ToolSet ({len(carried)}): {', '.join(sorted(carried))}")
    print(f"  chosen per call ({len(passed_per_call)}):      {', '.join(sorted(passed_per_call))}")
    missing = accepted - carried - passed_per_call
    print(f"  in neither ({len(missing)}):                {', '.join(sorted(missing)) or '-'}")


# ---------------------------------------------------------------------------
# 6 -- the same drift, measured by running it instead of reading it
# ---------------------------------------------------------------------------


def section_drift_live() -> None:
    """The same sub-agent, run twice: with the parent's wiring and without it.

    Both arms go through the code as it stands after interlude B.  The first
    arm passes `Wiring()` -- an empty one, which is exactly what a child used
    to get, because nothing passed it anything.  The second passes the wiring
    a top-level run builds.  Same model script, same files, same tools.
    """
    import asyncio
    import json
    import time
    from typing import Any

    sys.path.insert(0, str(SRC))
    from minicodex.agent import Wiring
    from minicodex.approval import AllowAll, Session
    from minicodex.composition import sub_context
    from minicodex.model import Completed, TextDelta, ToolCallDelta
    from minicodex.recorder import Recorder
    from minicodex.shell import ShellSession
    from minicodex.subagent import TaskSpec, run_task

    class Scripted:
        def __init__(self, turns: list[Any]) -> None:
            self.turns = turns
            self.sent: list[list[dict[str, Any]]] = []
            self.model = "scripted"

        async def stream(self, messages: list[dict[str, Any]]) -> Any:
            self.sent.append([dict(m) for m in messages])
            turn = self.turns[min(len(self.sent) - 1, len(self.turns) - 1)]
            if isinstance(turn, str):
                yield TextDelta(turn)
            else:
                for i, (cid, name, args) in enumerate(turn):
                    yield ToolCallDelta(call_id=cid, index=i, name=name, arguments=json.dumps(args))
            yield Completed("stop")

    async def summarise(_request: Any) -> str:
        return "## Done: read the file three times"

    script = [
        [("c1", "read_file", {"path": "big.txt"})],
        [("c2", "read_file", {"path": "big.txt"})],
        [("c3", "read_file", {"path": "big.txt"})],
        "done",
    ]

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "big.txt").write_text("x" * 60_000, encoding="utf-8")
        (root / "a.txt").write_text("a", encoding="utf-8")
        (root / "b.txt").write_text("b", encoding="utf-8")

        def run(label: str, wiring: Wiring, model: Any, task: str) -> Any:
            ctx = sub_context(
                build_model=lambda _schemas: model,
                root=root,
                session=Session(mode="workspace-write", approver=AllowAll()),
                parent_shell=ShellSession(),
                wiring=wiring,
                max_turns=8,
            )
            return asyncio.run(run_task(TaskSpec(task, "ok"), ctx))

        print(f"{'arm':26} {'final request':>15} {'recorded':>10} {'peak':>6} {'wall':>7}")
        for label, wiring in (
            ("Wiring()  (the old child)", Wiring()),
            (
                "the parent's wiring",
                Wiring(
                    recorder=Recorder(root / "rec"),
                    context_window=32_000,
                    summariser=summarise,
                ),
            ),
        ):
            model = Scripted(script)
            run(label, wiring, model, "read it three times")
            chars = sum(len(json.dumps(m)) for m in model.sent[-1])
            path = getattr(wiring.recorder, "path", None)
            recorded = "read it" in path.read_text(encoding="utf-8") if path is not None else False

            # two independent reads in one turn, timed
            overlap = {"peak": 0, "now": 0}
            import minicodex.tools as tools_mod

            original = tools_mod.read_file

            async def slow(
                root_: Path,
                args: dict[str, Any],
                _o: Any = original,
                _seen: dict[str, int] = overlap,
            ) -> str:
                _seen["now"] += 1
                _seen["peak"] = max(_seen["peak"], _seen["now"])
                await asyncio.sleep(0.2)
                try:
                    return await _o(root_, args)
                finally:
                    _seen["now"] -= 1

            tools_mod.read_file = slow
            try:
                began = time.monotonic()
                run(
                    label,
                    wiring,
                    Scripted(
                        [
                            [
                                ("c1", "read_file", {"path": "a.txt"}),
                                ("c2", "read_file", {"path": "b.txt"}),
                            ],
                            "done",
                        ]
                    ),
                    "read both",
                )
                seconds = time.monotonic() - began
            finally:
                tools_mod.read_file = original

            print(f"{label:26} {chars:>15,} {recorded!s:>10} {overlap['peak']:>6} {seconds:>6.2f}s")

        print()
        print("Both arms schedule the two reads together (peak 2, one 0.2s wait rather")
        print("than two): `footprint_of` travels with the ToolSet, not with the Wiring,")
        print("so that fix is not one a caller can decline. Before interlude B the same")
        print("run measured peak 1 and 0.42s.")


SECTIONS = {
    "graph": section_graph,
    "init-cycle": section_init_cycle,
    "tool-cycle": section_tool_cycle,
    "repairs": section_repairs,
    "drift": section_drift,
    "drift-live": section_drift_live,
}


if __name__ == "__main__":
    names = sys.argv[1:] or list(SECTIONS)
    for name in names:
        print(f"===== {name} " + "=" * (60 - len(name)))
        SECTIONS[name]()
        print()
