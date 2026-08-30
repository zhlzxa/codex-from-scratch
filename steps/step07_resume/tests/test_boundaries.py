"""Boundaries that only stay true if something checks them.

Chapter -1 said an architecture rule written only in prose will rot. These are
the first two rules cheap enough to encode, so they are encoded.
"""

from __future__ import annotations

import ast
import importlib
import subprocess
import sys
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parent.parent / "src" / "minicodex"


def imported_modules(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
    return names


# ---------------------------------------------------------------------------
# F01-05  the loop must not know which provider it is talking to
# ---------------------------------------------------------------------------


def test_F01_05_the_agent_does_not_import_an_http_client() -> None:
    """`agent.py` talks to a `Model`, never to a socket.

    If this ever fails, some provider-specific handling has leaked upward and
    the normalisation in `model.py` is no longer doing its job.
    """
    assert "httpx" not in imported_modules(SRC / "agent.py")


@pytest.mark.parametrize("module", ["agent.py", "history.py", "agent_types.py"])
def test_F01_05_no_provider_names_above_the_client(module: str) -> None:
    """Provider names belong in `model.py`, `stub.py` and the CLI.

    "ollama_native" appears in history.py as a dialect name, which is the one
    legitimate exception: rendering is exactly where a dialect must be named.
    """
    text = (SRC / module).read_text(encoding="utf-8")
    code = "\n".join(line for line in text.splitlines() if not line.strip().startswith("#"))
    for banned in ("api.openai.com", "localhost:11434", "OPENAI_API_KEY"):
        assert banned not in code, f"{module} should not know about {banned}"


# ---------------------------------------------------------------------------
# F01-08  the circular import
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "first,second",
    [("minicodex.agent", "minicodex.history"), ("minicodex.history", "minicodex.agent")],
)
def test_F01_08_modules_import_in_either_order(first: str, second: str) -> None:
    """A circular import only fails in one direction, so both are tried.

    Run in a subprocess: once a module is in sys.modules the cycle is hidden,
    and every other test in this file has already imported both.
    """
    result = subprocess.run(
        [sys.executable, "-c", f"import {first}; import {second}"],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr


def test_F01_08_agent_types_depends_on_nothing_of_ours() -> None:
    """The shared module is only a solution while it stays a leaf."""
    ours = {m for m in imported_modules(SRC / "agent_types.py") if m.startswith("minicodex")}
    assert ours == set(), f"agent_types must stay a leaf, but imports {ours}"


# ---------------------------------------------------------------------------
# FA-04  the cycle the tool refactor could have started
# ---------------------------------------------------------------------------


def test_FA_04_tools_does_not_import_the_agent() -> None:
    """`tools.py` needed `ToolFn`, and `agent.py` was where it lived.

    Importing it from there would have worked and every test would have
    stayed green, because `agent.py` does not import `tools.py` -- there is no
    cycle *today*.  What there would have been is an arrow pointing the wrong
    way: the loop on top, the tool machinery underneath, and underneath
    depending on on-top.

    That arrow is how the cycle gets built.  The natural next step is to give
    `Agent` a default tool table, at which point `agent.py` imports `tools.py`,
    the ring closes, and the import error blames whichever module happened to
    be loaded second.  `ToolFn` moved down to `agent_types.py` instead, which
    is the same answer chapter 1 reached for `ToolCall`.

    Encoded as a test because chapter -1 said an architecture rule kept only
    in prose will rot, and this is one line to check.
    """
    imports = imported_modules(SRC / "tools.py")
    assert "minicodex.agent" not in imports, (
        "tools.py must not depend on agent.py -- put the shared type in "
        "agent_types.py, as chapter 1 did with ToolCall"
    )


def test_importlib_is_used() -> None:
    assert importlib.import_module("minicodex.history") is not None
