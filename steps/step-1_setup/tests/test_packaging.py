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
    from the same commit, which makes 'works on my machine' unfalsifiable."""
    try:
        import tomllib
    except ModuleNotFoundError:  # Python 3.10
        import tomli as tomllib

    with (repo_root / "pyproject.toml").open("rb") as fh:
        cfg = tomllib.load(fh)

    dev = cfg["project"]["optional-dependencies"]["dev"]
    assert dev
    for spec in dev:
        assert any(op in spec for op in (">=", "==", "~=")), f"unbounded: {spec}"


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
@pytest.mark.parametrize("pattern", [".env", "__pycache__/", "*.key", ".venv/"])
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


def test_F_1_05_ci_job_is_bounded(repo_root: Path) -> None:
    """A hung job blocks every pull request queued behind it."""
    wf = yaml.safe_load((repo_root / ".github/workflows/ci.yml").read_text(encoding="utf-8"))
    assert wf["jobs"]["check"]["timeout-minutes"] <= 15
