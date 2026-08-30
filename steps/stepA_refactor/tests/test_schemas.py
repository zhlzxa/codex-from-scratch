"""What the model is told, and what happens when it misreads it.

Fault IDs match FAULTS.md. Chapter 3 is unusual: most of the faults it went
looking for did not reproduce on either provider, and the tests here reflect
that -- there is no test for a fault that was never observed. What is tested
is the three that did reproduce, plus the two drift problems that are
deterministic and do not need a model at all.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

from minicodex.paths import resolve
from minicodex.shell import DEFAULT_TIMEOUT
from minicodex.tool_errors import tool_error
from minicodex.tools import TOOL_SCHEMAS, tool_schemas

SRC = Path(__file__).resolve().parent.parent / "src" / "minicodex"


def schema(name: str) -> dict:
    return next(t["function"] for t in TOOL_SCHEMAS if t["function"]["name"] == name)


# ---------------------------------------------------------------------------
# F03-02  the path the model actually sends
# ---------------------------------------------------------------------------


def test_F03_02_an_absolute_path_inside_the_repo_is_accepted(tmp_path: Path) -> None:
    """What gpt-4o-mini sent 3/3 with a vague description. It is unambiguous,
    so it is resolved rather than bounced back."""
    (tmp_path / "src").mkdir()
    target = tmp_path / "src" / "model.py"
    target.write_text("x = 1")

    got, error = resolve(str(target), tmp_path)

    assert error is None
    assert got == target.resolve()


def test_F03_02_an_absolute_path_outside_the_repo_is_refused(tmp_path: Path) -> None:
    got, error = resolve("/etc/hosts", tmp_path)
    assert got is None
    assert error is not None
    assert "outside the repository" in error


def test_F03_02_a_bare_filename_names_the_file_it_meant(tmp_path: Path) -> None:
    """What gemma4:31b sent 3/3: `model.py`, relative but not the file. One
    match exists, so the error can simply say which one."""
    (tmp_path / "src" / "minicodex").mkdir(parents=True)
    (tmp_path / "src" / "minicodex" / "model.py").write_text("x = 1")

    got, error = resolve("model.py", tmp_path)

    assert got is None
    assert error is not None
    assert "src/minicodex/model.py" in error


def test_F03_02_several_matches_are_all_offered(tmp_path: Path) -> None:
    for sub in ("a", "b"):
        (tmp_path / sub).mkdir()
        (tmp_path / sub / "utils.py").write_text("x = 1")

    _, error = resolve("utils.py", tmp_path)

    assert error is not None
    assert "a/utils.py" in error and "b/utils.py" in error


def test_F03_02_ignored_directories_are_not_offered(tmp_path: Path) -> None:
    """A match inside .git or __pycache__ is never what the model meant."""
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "config.py").write_text("x = 1")

    _, error = resolve("config.py", tmp_path)

    assert error is not None
    assert ".git" not in error


# ---------------------------------------------------------------------------
# F03-07  the error message is a prompt
# ---------------------------------------------------------------------------


def test_F03_07_an_error_always_says_what_to_do_next() -> None:
    """`do_this` is keyword-only and required: the measured difference between
    a model that recovers and one that resends the same call is whether the
    message contains an instruction."""
    with pytest.raises(TypeError):
        tool_error("something broke")  # type: ignore[call-arg]


def test_F03_07_the_three_parts_are_all_present() -> None:
    message = tool_error(
        "no such file in the repository",
        you_sent="model.py",
        do_this="Send this instead: src/minicodex/model.py",
    )
    assert message.startswith("Error: ")
    assert "You sent: model.py" in message
    assert "Send this instead: src/minicodex/model.py" in message


def test_F03_07_a_long_input_is_truncated_not_echoed_whole() -> None:
    message = tool_error("too long", you_sent="x" * 5000, do_this="Send less.")
    assert len(message) < 400


@pytest.mark.parametrize("module", ["tools.py", "shell.py", "paths.py"])
def test_F03_07_no_module_builds_an_error_string_by_hand(module: str) -> None:
    """The rule is only worth anything if every error goes through it.

    Checked with `ast`, not a regex over the source: the word "Error:" appears
    in docstrings and comments in these files, and a text search would flag
    those. This walks actual string literals in `return` statements.
    """
    tree = ast.parse((SRC / module).read_text(encoding="utf-8"))

    offenders = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Return) or node.value is None:
            continue
        for piece in ast.walk(node.value):
            if isinstance(piece, ast.Constant) and isinstance(piece.value, str):
                if piece.value.lstrip().startswith("Error:"):
                    offenders.append(f"line {piece.lineno}: {piece.value[:60]!r}")

    assert not offenders, f"{module} returns a hand-built error string: {offenders}"


# ---------------------------------------------------------------------------
# description drift -- deterministic, no model needed
# ---------------------------------------------------------------------------


def test_the_timeout_in_the_description_matches_the_timeout_in_the_code() -> None:
    """Chapter 2 shipped `f"...killed after {30} seconds"` beside
    `DEFAULT_TIMEOUT = 30.0`. They agreed because both were typed the same
    afternoon, and nothing would have noticed the day one of them changed."""
    described = re.findall(r"\b(\d+)\s*seconds?\b", schema("run_shell")["description"])
    assert described == [f"{DEFAULT_TIMEOUT:.0f}"]


def test_changing_the_timeout_changes_the_sentence() -> None:
    """The real assertion: the number is generated, not typed."""
    other = tool_schemas(timeout=90.0)
    described = next(
        t["function"]["description"] for t in other if t["function"]["name"] == "run_shell"
    )
    assert "90 seconds" in described
    assert "30 seconds" not in described


# ---------------------------------------------------------------------------
# F03-10  a description edit degrades unrelated tasks
# ---------------------------------------------------------------------------

# Every word the model is shown. Chapter 3 measured that changing a single
# example in a description flipped both providers from 3/3 correct to 3/3
# wrong, so a description edit is a behaviour change and should not be
# possible to make by accident. Updating this dict is the confirmation.
#
# This is the cheap version of F03-10. The real one -- run a task set before
# and after and compare -- needs the harness chapter 14 builds.
EXPECTED_DESCRIPTIONS = {
    "read_file": "Read a UTF-8 text file from the repository and return its contents.",
    "read_file.path": (
        "Path to the file, relative to the repository root. Example: src/minicodex/model.py"
    ),
    "apply_patch": (
        "Edit files by replacing exact blocks of text. Every edit is checked before "
        "any file is written: if one fails, nothing is written."
    ),
    "apply_patch.edits": "The edits to apply, in any order.",
    "apply_patch.edits[].path": (
        "Path to the file, relative to the repository root. Example: src/minicodex/model.py"
    ),
    "apply_patch.edits[].old_text": (
        "The text to replace, copied from the file. It must appear exactly once -- "
        "include whole surrounding lines until it does. Do not write line numbers "
        "or diff markers."
    ),
    "apply_patch.edits[].new_text": "What to put in its place.",
    "run_shell": (
        "Run a shell command and return its combined stdout and stderr. The working "
        "directory persists across calls within one session (cd changes it for "
        "subsequent calls). Backgrounded commands (trailing '&') are not supported. "
        "Long-running or silent commands are killed after 30 seconds."
    ),
    "run_shell.command": "The shell command to run.",
}


def collect_descriptions() -> dict[str, str]:
    """Every description the model is shown, including nested ones.

    The first version of this walked only `properties`, one level deep. When
    chapter 4 added `apply_patch`, whose `edits` parameter is an array of
    objects, the descriptions of `path`, `old_text` and `new_text` were not
    collected at all -- including the sentence that carries the whole
    uniqueness requirement. A snapshot that does not reach the text is a
    snapshot of nothing.
    """
    found: dict[str, str] = {}

    def walk(prefix: str, spec: dict) -> None:
        if "description" in spec:
            found[prefix] = spec["description"]
        for param, sub in spec.get("properties", {}).items():
            walk(f"{prefix}.{param}", sub)
        if "items" in spec:
            walk(f"{prefix}[]", spec["items"])

    for tool in TOOL_SCHEMAS:
        fn = tool["function"]
        found[fn["name"]] = fn["description"]
        for param, spec in fn["parameters"]["properties"].items():
            walk(f"{fn['name']}.{param}", spec)
    return found


def test_F03_10_descriptions_are_pinned() -> None:
    assert collect_descriptions() == EXPECTED_DESCRIPTIONS


# ---------------------------------------------------------------------------
# schema shape
# ---------------------------------------------------------------------------


def test_every_parameter_has_a_description() -> None:
    for key, text in collect_descriptions().items():
        assert text.strip(), f"{key} has an empty description"


def test_every_required_parameter_exists_in_properties() -> None:
    for tool in TOOL_SCHEMAS:
        params = tool["function"]["parameters"]
        for name in params.get("required", []):
            assert name in params["properties"], f"{tool['function']['name']}: {name}"
