"""What we learned about packaging, written down so it stays true.

Every test here corresponds to something that broke, or would have broken
silently, while building this chapter.  Fault IDs match FAULTS.md.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import zipfile
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

import pytest
import yaml

import minicodex


# ---------------------------------------------------------------------------
# The package is really installed, not merely lying around next to the tests
# ---------------------------------------------------------------------------
def test_F_1_01_package_is_installed_not_merely_importable() -> None:
    """Under a flat layout `import minicodex` succeeds from the repo root even
    when nothing was installed, because '' is on sys.path.  Asking for the
    metadata is a question only a real installation can answer.
    """
    try:
        assert version("minicodex")
    except PackageNotFoundError:  # pragma: no cover - only when misconfigured
        pytest.fail("minicodex is not installed; run `uv sync --all-extras`")


def test_F_1_01_import_comes_from_src_not_from_the_working_directory() -> None:
    assert Path(minicodex.__file__).resolve().parent.parent.name == "src"


def test_F_1_01_data_files_are_readable_at_runtime() -> None:
    """Code-only tests pass against a broken wheel: the .py files are always
    there.  Reading a packaged file is what makes packaging observable at all.
    """
    assert "coding agent" in minicodex.system_prompt()


@pytest.mark.skipif(shutil.which("uv") is None, reason="uv is required to build a wheel")
def test_F_1_01_built_wheel_actually_contains_the_data_file(
    repo_root: Path, tmp_path: Path
) -> None:
    """The test above is not enough, and finding that out cost an afternoon.

    An editable install puts the *source tree* on the path, so `system_prompt()`
    reads the file straight off disk and passes no matter what the wheel
    contains.  The only way to know what users receive is to build the artefact
    and look inside it.  Takes about a quarter of a second.
    """
    subprocess.run(
        ["uv", "build", "--wheel", "-o", str(tmp_path), str(repo_root)],
        check=True,
        capture_output=True,
    )
    wheel = next(tmp_path.glob("*.whl"))
    names = zipfile.ZipFile(wheel).namelist()

    assert "minicodex/prompts/system.md" in names, (
        f"the wheel users install is missing the prompt file; it contains {names}"
    )


# ---------------------------------------------------------------------------
# The console script exists and works from anywhere
# ---------------------------------------------------------------------------
def test_console_script_runs_outside_the_project_directory(tmp_path: Path) -> None:
    result = subprocess.run(
        [sys.executable, "-m", "minicodex", "--version"],
        cwd=tmp_path,  # deliberately not the repo: cwd must not be load-bearing
        capture_output=True,
        text=True,
        check=True,
    )
    assert result.stdout.startswith("minicodex ")
    assert "python" in result.stdout


# ---------------------------------------------------------------------------
# Reproducible installs
# ---------------------------------------------------------------------------
def test_F_1_02_every_dev_dependency_has_a_lower_bound(repo_root: Path) -> None:
    """An unbounded dependency lets CI and a laptop resolve different versions
    from the same commit, which makes 'works on my machine' unfalsifiable.

    Every optional group, not just `dev`.  This read `["dev"]` by name for
    eighteen chapters, which was correct while `dev` was the only group there
    was -- and then chapter 19 added `web` with two more dependencies in it,
    and the rule did not extend to them because the rule had been written as a
    lookup rather than as a loop.  The same shape as F19-06 one file over: a
    check that silently stops covering new code, while still passing.
    """
    try:
        import tomllib
    except ModuleNotFoundError:  # Python 3.10
        import tomli as tomllib

    with (repo_root / "pyproject.toml").open("rb") as fh:
        cfg = tomllib.load(fh)

    groups = cfg["project"]["optional-dependencies"]
    assert "dev" in groups, "the test runner has to come from somewhere"
    for name, specs in groups.items():
        assert specs, f"empty dependency group: {name}"
        for spec in specs:
            assert any(op in spec for op in (">=", "==", "~=")), f"unbounded: {name} -> {spec}"


def test_F_1_02_lockfile_is_committed(repo_root: Path) -> None:
    assert (repo_root / "uv.lock").exists(), "uv.lock pins the exact resolution"


def test_F_1_02_build_backend_is_declared(repo_root: Path) -> None:
    """Without [build-system], tools fall back to setuptools -- and the
    fallback differs between them.  An implicit build is an unreproducible one.
    """
    try:
        import tomllib
    except ModuleNotFoundError:
        import tomli as tomllib

    with (repo_root / "pyproject.toml").open("rb") as fh:
        cfg = tomllib.load(fh)

    assert cfg["build-system"]["build-backend"] == "hatchling.build"


# ---------------------------------------------------------------------------
# Secrets
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "pattern",
    [
        ".env",
        "__pycache__/",
        "*.key",
        ".venv/",
        # Added in interlude A. The chapter -1 list covered the things that are
        # obviously secret; this one is a directory the agent creates by itself,
        # on every run, full of everything it read. Nothing had noticed because
        # nothing had run the agent and then looked at `git status`.
        ".minicodex/",
    ],
)
def test_F_1_03_gitignore_covers_the_usual_accidents(repo_root: Path, pattern: str) -> None:
    assert pattern in (repo_root / ".gitignore").read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# CI
# ---------------------------------------------------------------------------
def test_F_1_05_ci_is_valid_yaml_and_stays_small(repo_root: Path) -> None:
    wf = yaml.safe_load((repo_root / ".github/workflows/ci.yml").read_text(encoding="utf-8"))

    # PyYAML parses the key `on:` as the boolean True (a YAML 1.1 quirk).
    triggers = wf.get("on", wf.get(True))
    assert "pull_request" in triggers

    steps = wf["jobs"]["check"]["steps"]
    names = [s.get("name", "") for s in steps]
    assert any("Lint" in n for n in names)
    assert any("Test" in n for n in names)
    assert len(steps) <= 6, "the blocking suite is meant to stay fast"


def test_F_1_05_the_second_tier_cannot_block_a_merge(repo_root: Path) -> None:
    """Chapter 9 added a second workflow rather than a seventh step.

    The six-step cap above is what forced the choice, and it was the right
    forcing function: the mutation check measures the quality of the tests,
    not the correctness of a change, so a pull request that leaves a mutation
    uncaught should be reported and looked at rather than held.

    "It does not block a merge" is a fact about its triggers, so that is what
    is asserted -- a comment saying so would survive somebody adding
    `pull_request: {}` to make it run earlier.
    """
    wf = yaml.safe_load((repo_root / ".github/workflows/postmerge.yml").read_text(encoding="utf-8"))
    triggers = wf.get("on", wf.get(True))
    assert "pull_request" not in triggers
    assert "push" in triggers
    names = [s.get("name", "") for s in wf["jobs"]["mutation"]["steps"]]
    assert any("Mutation" in n for n in names)


def ci_commands(workflow: Path) -> str:
    """Every shell command a workflow actually runs, and nothing else.

    Parsed rather than grepped, so comments, step names and the prose around
    them cannot answer a question about what executes. See F23-11.
    """
    parsed = yaml.safe_load(workflow.read_text(encoding="utf-8"))
    return "\n".join(
        step["run"]
        for job in parsed["jobs"].values()
        for step in job["steps"]
        if isinstance(step.get("run"), str)
    )


def test_F_1_05_every_mutation_script_runs_somewhere(repo_root: Path) -> None:
    """A mutation script nobody runs is a file, not a check.

    Added in chapter 12, after finding that chapter 11's nineteen mutations
    had never been wired in: the chapter text said they ran in `postmerge.yml`
    "like chapter 9's", the script was correct, the workflow had steps for 9
    and 10 only, and every test in the repository was green about it.

    The general shape has now appeared often enough to be the rule this book
    keeps arriving at: a promise written in prose is not a mechanism. This is
    the same repair as interlude B's layer checker and chapter 3's description
    snapshot -- state the rule where something executes it.

    Chapter 23 found a hole in it and it is the funniest one in the book, so
    the repair is written out below rather than folded in quietly. This
    function used to search the workflow's whole *text*:

        workflow = (...).read_text(encoding="utf-8")
        missing = [name for name in scripts if name not in workflow]

    A YAML comment is part of that text. So a file naming `probe_mutations.py`
    in a comment satisfied the check whether or not anything ran it -- and
    chapter 13's own repair added exactly such a comment, immediately above the
    step, in every step from 13 on. Deleting the step and keeping the comment
    left this test green. Measured, not reasoned: the run line was removed and
    the check still reported nothing missing.

    Which makes this the check that exists to enforce "a promise written in
    prose is not a mechanism", satisfied by prose. It is now asked of the
    commands the workflow actually runs.
    """
    scripts = sorted(p.name for p in repo_root.glob("probe_mutations*.py"))
    commands = ci_commands(repo_root / ".github/workflows/postmerge.yml")
    missing = [name for name in scripts if name not in commands]
    assert not missing, f"mutation scripts nothing runs: {missing}"


def test_F23_11_a_comment_naming_a_script_does_not_count_as_running_it(
    repo_root: Path, tmp_path: Path
) -> None:
    """The regression test for the check above, on a workflow built for it.

    Written against a synthetic file rather than by editing the real one,
    because a test that mutates the repository to prove a point is a test that
    can leave the repository mutated -- `probe_mutations.py`'s own docstring
    says so, having done it.
    """
    workflow = tmp_path / "postmerge.yml"
    workflow.write_text(
        "jobs:\n"
        "  check:\n"
        "    steps:\n"
        "      # probe_mutations.py runs here, honest\n"
        "      - name: Something else\n"
        "        run: uv run python probe_mutations_ch09.py\n",
        encoding="utf-8",
    )
    commands = ci_commands(workflow)
    assert "probe_mutations_ch09.py" in commands
    assert "probe_mutations.py" not in commands, "a comment is not a step"


def test_F_1_05_ci_job_is_bounded(repo_root: Path) -> None:
    """A hung job blocks every pull request queued behind it."""
    wf = yaml.safe_load((repo_root / ".github/workflows/ci.yml").read_text(encoding="utf-8"))
    assert wf["jobs"]["check"]["timeout-minutes"] <= 15
