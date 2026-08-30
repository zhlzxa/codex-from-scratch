#!/usr/bin/env python
"""Architecture rules that are cheap enough to check, checked.

    uv run python scripts/check_layers.py [--src PATH]

Exit 0 and say nothing when every rule holds; exit 1 and name the rule, the
module and the fault it came from otherwise.

Every rule below was a real failure first.  None of them is here because it
seemed like good practice -- the section at the bottom lists the rules that
were *not* written, and why, which matters as much.

Runs in CI as part of the lint step rather than as a step of its own.  Chapter
-1's guard (`len(steps) <= 6`, "the blocking suite is meant to stay fast")
already forced that question once in chapter 9, and the honest answer here is
different from chapter 9's: this is a static check that takes under a second,
which is what the lint step is.  A new step would have been a way of not
answering.
"""

from __future__ import annotations

import argparse
import ast
import subprocess
import sys
from pathlib import Path

PACKAGE = "minicodex"

# Import edges that are forbidden, and the fault each one came from.  A list of
# specific arrows rather than a table of layer numbers for every module: see
# "rules not written" at the bottom.
FORBIDDEN: list[tuple[str, str, str]] = [
    ("tools", "agent", "FA-04: the loop is above the tool machinery, not below it"),
    ("tools", "subagent", "F10-06: putting the spawn handler with the handlers"),
    (
        "subagent",
        "tools",
        "FB-01: the arrow that made the line above impossible. Interlude B "
        "inverted it -- subagent takes `build_tools` from its caller",
    ),
    ("history", "agent", "F01-08: the first cycle in this project"),
    ("agent_types", "*", "F01-08: the shared-type module only works while it is a leaf"),
]

# Where an `Agent` may be constructed.  One place, because two places passed
# different subsets of the same ten arguments for a whole chapter and no test
# compared them (interlude B).
AGENT_CONSTRUCTION_SITES = {"agent.py"}


def intra_package_imports(path: Path) -> set[str]:
    """Every module inside the package that this file imports, at any depth.

    `ast.walk` rather than a scan of the top-level body, because an import
    written inside a function is still an edge -- and deferring an import is
    the standard trick for making a cycle stop failing at import time and
    start failing on the first call.  A checker that only reads the top of the
    file rewards exactly the repair that hides the problem.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(a.name for a in node.names if a.name.split(".")[0] == PACKAGE)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            if node.module.split(".")[0] != PACKAGE:
                continue
            # `from minicodex import compaction_prompt` is an edge to the
            # *package*, whose __init__ has to finish running first.  The first
            # version of this function dropped that case because the name is
            # not a module -- and reported "no cycles" for a package that could
            # not be imported at all.
            found.add(PACKAGE if node.module == PACKAGE else node.module)
    return found


def build_graph(pkg: Path) -> dict[str, set[str]]:
    graph: dict[str, set[str]] = {}
    for path in sorted(pkg.glob("*.py")):
        name = PACKAGE if path.stem == "__init__" else f"{PACKAGE}.{path.stem}"
        graph[name] = intra_package_imports(path)
    known = set(graph)
    return {k: {v for v in vs if v in known and v != k} for k, vs in graph.items()}


def find_cycles(graph: dict[str, set[str]]) -> list[list[str]]:
    """Every strongly connected component with more than one module in it."""
    colour: dict[str, int] = dict.fromkeys(graph, 0)
    order: list[str] = []

    def visit(node: str) -> None:
        colour[node] = 1
        for child in sorted(graph[node]):
            if colour[child] == 0:
                visit(child)
        colour[node] = 2
        order.append(node)

    for node in sorted(graph):
        if colour[node] == 0:
            visit(node)

    reverse: dict[str, set[str]] = {n: set() for n in graph}
    for node, deps in graph.items():
        for dep in deps:
            reverse[dep].add(node)

    seen: set[str] = set()
    found: list[list[str]] = []
    for node in reversed(order):
        if node in seen:
            continue
        component = []
        stack = [node]
        seen.add(node)
        while stack:
            current = stack.pop()
            component.append(current)
            for parent in sorted(reverse[current]):
                if parent not in seen:
                    seen.add(parent)
                    stack.append(parent)
        if len(component) > 1:
            found.append(sorted(component))
    return found


def check_cycles(pkg: Path) -> list[str]:
    cycles = find_cycles(build_graph(pkg))
    return [f"import cycle: {' -> '.join(c)}" for c in cycles]


def check_forbidden(pkg: Path) -> list[str]:
    problems = []
    for path in sorted(pkg.glob("*.py")):
        stem = PACKAGE if path.stem == "__init__" else path.stem
        imports = intra_package_imports(path)
        for source, target, why in FORBIDDEN:
            if source != stem:
                continue
            if target == "*":
                ours = sorted(imports)
                if ours:
                    problems.append(
                        f"{stem} must import nothing of ours, but imports {ours} ({why})"
                    )
            elif f"{PACKAGE}.{target}" in imports:
                problems.append(f"{stem} must not import {target} ({why})")
    return problems


def check_init_is_a_leaf(pkg: Path) -> list[str]:
    """`__init__.py` importing a submodule is the cheapest cycle in Python.

    One ordinary line -- `from minicodex.agent import Agent`, so that callers
    can write `from minicodex import Agent` -- makes `import minicodex.tools`
    fail, because `compaction.py` does `from minicodex import compaction_prompt`
    and the package is still half-built at that moment.  Measured; the whole
    package stops importing.

    Checked separately from `check_cycles` even though the cycle check catches
    the same thing, because the *message* has to be different.  "import cycle:
    minicodex -> minicodex.agent -> minicodex.compaction" is true and does not
    tell you that the fix is to delete one convenience import from a file
    nobody thinks of as code.
    """
    ours = sorted(intra_package_imports(pkg / "__init__.py"))
    if not ours:
        return []
    return [
        f"__init__.py imports {ours} from its own package. Anything doing "
        f"`from {PACKAGE} import <name>` now depends on all of it, and the "
        f"package stops importing (FB-01)."
    ]


def check_one_agent_construction(pkg: Path) -> list[str]:
    problems = []
    for path in sorted(pkg.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        calls = sum(
            1
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "Agent"
        )
        if calls and path.name not in AGENT_CONSTRUCTION_SITES:
            problems.append(
                f"{path.name} constructs an Agent. Build it through `Wiring.agent` "
                f"instead: a second construction site is how a child ended up "
                f"without a recorder, without compaction and without the "
                f"scheduler, silently, for a chapter (FB-01)."
            )
    return problems


def check_every_module_imports(pkg: Path) -> list[str]:
    """Import each module in a fresh interpreter, one subprocess per module.

    Not redundant with the cycle check, and the reason is measured: with
    `tools` and `subagent` importing each other, `python -c "import minicodex"`
    **exits 0**.  The package's `__init__` is a leaf, so the top-level import
    touches none of the broken part.  A smoke test that imports the package is
    the obvious CI check and it passes on a package that cannot be used.

    One subprocess per module, because once a module is in `sys.modules` the
    cycle is hidden from everything that runs afterwards -- which is also why
    the test suite stays green in a shell where the package has already been
    imported once.
    """
    failures: dict[str, list[str]] = {}
    for path in sorted(pkg.glob("*.py")):
        if path.stem == "__init__":
            continue
        module = f"{PACKAGE}.{path.stem}"
        result = subprocess.run(
            [sys.executable, "-c", f"import {module}"],
            capture_output=True,
            text=True,
            env={**_env(), "PYTHONPATH": str(pkg.parent)},
        )
        if result.returncode != 0:
            last = (result.stderr.strip().splitlines() or ["(no output)"])[-1]
            failures.setdefault(last, []).append(module)

    # Grouped by message, because a cycle through `__init__` fails *every*
    # module with the same line: the first version of this printed twenty-five
    # identical paragraphs, which is a report nobody reads to the end.
    problems = []
    for message, modules in failures.items():
        if len(modules) > 3:
            problems.append(f"{len(modules)} modules do not import on their own: {message}")
        else:
            problems.extend(f"{m} does not import on its own: {message}" for m in modules)
    return problems


def _env() -> dict[str, str]:
    import os

    keep = ("PATH", "SYSTEMROOT", "TEMP", "TMP", "HOME", "USERPROFILE", "APPDATA", "PATHEXT")
    return {k: v for k, v in os.environ.items() if k in keep}


CHECKS = (
    ("no import cycles", check_cycles),
    ("__init__ is a leaf", check_init_is_a_leaf),
    ("forbidden edges", check_forbidden),
    ("one Agent construction site", check_one_agent_construction),
    ("every module imports alone", check_every_module_imports),
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--src",
        type=Path,
        default=Path(__file__).resolve().parent.parent / "src" / PACKAGE,
        help="the package directory to check",
    )
    parser.add_argument("--quiet", action="store_true", help="print nothing when everything passes")
    args = parser.parse_args(argv)

    failed = False
    for label, check in CHECKS:
        problems = check(args.src)
        if problems:
            failed = True
            for problem in problems:
                print(f"{label}: {problem}", file=sys.stderr)
        elif not args.quiet:
            print(f"ok  {label}")
    return 1 if failed else 0


# ---------------------------------------------------------------------------
# Rules not written, and why
# ---------------------------------------------------------------------------
#
# **A declared layer number for every module.**  Designed, then dropped.  It
# would catch every arrow the list above catches and more, at the cost of an
# entry per module forever, and the "more" is hypothetical: three backwards
# arrows have actually happened in this project and all three are named above.
# A rule whose maintenance is certain and whose value is speculative is the
# ceremonial half of FB-03.
#
# **A maximum module size.**  codex's AGENTS.md has one -- a file over roughly
# 800 LoC should get new functionality in a new module rather than be extended
# -- and interlude A measured why it does not belong here: `agent.py` was 212
# lines and perfectly healthy, and the duplication
# that actually mattered was three copies of a clipper across three files that
# were each small.  Line count measures volume, not coupling.
#
# **A maximum fan-in or fan-out.**  `agent_types` has a fan-in of eight, which
# is the *point* of it.  A number would have to be tuned until it stopped
# firing, which means it was never measuring anything.

if __name__ == "__main__":
    raise SystemExit(main())
