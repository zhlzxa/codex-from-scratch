"""Chapter 15 -- releasing it, and living with the releases.

Every other test file in this repository tests the *program*.  These test the
**artefact and its promises**, and the distinction is the reason the chapter
exists: `pytest` imports `minicodex` from a tree that was synced two seconds
ago, and a user unzips a wheel onto a machine that has files written by last
month's build.  Nothing in 1656 tests can see the gap between those two.

Three of the fixtures here are not written by hand.  `tests/fixtures/compat/`
holds artefacts produced by **fourteen real past versions of this program**,
each one run for real against chapter 0's stub by `probe_release.py compat`.
That is the only kind of fixture that can answer "does the new code read the
old code's files", because the old code is the only thing that knows what it
used to write.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import zipfile
from importlib.metadata import version as installed_version
from pathlib import Path

import pytest
import yaml

import minicodex
from minicodex.release import (
    DEPRECATIONS,
    ISSUES_URL,
    SURFACES,
    apply_deprecations,
    environment_report,
    version_parts,
)
from minicodex.replay import ReplayError
from minicodex.replay import load as load_recording
from minicodex.rollout import read_rollout

REPO_ROOT = Path(__file__).resolve().parent.parent
COMPAT = Path(__file__).resolve().parent / "fixtures" / "compat"
PAST_VERSIONS = sorted(p.name for p in COMPAT.iterdir()) if COMPAT.is_dir() else []


# ---------------------------------------------------------------------------
# F15-01 -- one version number, and the files it does not cover
# ---------------------------------------------------------------------------
def test_F15_01_the_version_has_exactly_one_source() -> None:
    """`pyproject.toml` reads the literal in `__init__.py` rather than repeating it.

    Two literals is the arrangement this project shipped for seventeen
    chapters, and the failure it invites is silent in the worst possible
    place: the number in every bug report stops being the number that was
    built.  The same two-rulers fault has already cost a compaction budget
    (F06-12) and a truncated memory (chapter 17).

    Both the installed metadata *and* a freshly built wheel, and the second one
    is the one that works.  Mutation testing put a literal `version` back into
    `pyproject.toml` and this test stayed green: an editable install's metadata
    was generated at `uv sync` time, so it still agreed with the module.  The
    disagreement only exists in an artefact built after the edit -- which is
    the whole reason this chapter builds one.
    """
    assert installed_version("minicodex") == minicodex.__version__


def test_F15_01_the_version_is_no_longer_the_one_every_build_shared() -> None:
    """Fourteen past builds of this program all answered `minicodex 0.0.1`.

    Measured, not assumed: `probe_release.py compat` runs each of them.  The
    recording format changed inside that range, so "which version wrote this"
    was unanswerable for the whole history.
    """
    assert minicodex.__version__ != "0.0.1"
    assert version_parts(minicodex.__version__) > (0, 0, 1)


def test_F15_01_version_parts_orders_by_number_and_not_by_string() -> None:
    assert version_parts("0.10.0") > version_parts("0.9.0")
    assert "0.10.0" < "0.9.0"  # the comparison this replaces


@pytest.mark.skipif(not PAST_VERSIONS, reason="run probe_release.py compat --corpus first")
@pytest.mark.parametrize("past", PAST_VERSIONS)
def test_F15_01_a_session_written_by_any_past_version_still_loads(past: str) -> None:
    """The additive-change claim, checked against every version that made one.

    `SessionMeta` gained `forked_from` and then `parent` without bumping
    `type_version`, on the argument that a new optional field is not a
    breaking change.  That argument is correct and was never executed until
    this test: `from_json` drops unknown keys and missing keys take their
    default, so the only way it fails is if somebody *reinterprets* a field
    instead of adding one -- which is exactly what F07-09's bump was for.
    """
    session = COMPAT / past / "session.jsonl"
    if not session.exists():
        pytest.skip(f"{past} predates chapter 7's rollout file")
    history, dropped = read_rollout(session).history()
    assert history.items, f"{past}: session loaded as empty"
    assert dropped == 0


@pytest.mark.skipif(not PAST_VERSIONS, reason="run probe_release.py compat --corpus first")
@pytest.mark.parametrize("past", PAST_VERSIONS)
def test_F15_01_a_recording_from_any_past_version_replays_or_refuses_in_words(past: str) -> None:
    """Never a traceback.  Either it replays or it says which field is missing.

    This is the test that found the chapter's first fault.  `load()` was
    written in chapter 14 with a careful refusal for the one old shape that
    was in front of it -- a tool call recorded without its arguments -- and
    crashed with a bare `KeyError: 'attempt'` on anything older than chapter
    12, which is nine of the fourteen versions below.  A guard written against
    the old file you happen to have is a guard against that file.
    """
    recording = COMPAT / past / "recording.jsonl"
    if not recording.exists():
        pytest.skip(f"{past} produced no recording")
    try:
        loaded = load_recording(recording)
    except ReplayError as exc:
        assert "arguments" in str(exc), f"{past}: refused without naming the missing field"
    else:
        assert loaded.turns > 0


def test_F15_01_a_recording_older_than_the_attempt_field_is_migrated_not_refused(
    tmp_path: Path,
) -> None:
    """The other half of the same decision, and the distinction the chapter is about.

    Missing `arguments` is *information that is gone*: filling it with `{}`
    replays a run in which the model asked to patch nothing, which is a green
    test for a conversation that never happened.  Missing `attempt` is
    information that was **never written down** because the concept did not
    exist yet, and its value is not in doubt -- there was one attempt.  So one
    refuses and one migrates.
    """
    path = tmp_path / "old.jsonl"
    events = [
        {"seq": 1, "kind": "request", "payload": {"turn": 0, "messages": [{"role": "user"}]}},
        {"seq": 2, "kind": "response", "payload": {"turn": 0, "text": "hi", "tool_calls": []}},
    ]
    path.write_text("\n".join(json.dumps(e) for e in events), encoding="utf-8")
    loaded = load_recording(path)
    assert loaded.turns == 1
    assert loaded.attempts[0].attempt == 0


def test_F15_01_every_surface_says_what_happens_to_a_file_it_cannot_read() -> None:
    """A list of the files that outlive a release, in one place, with a policy each.

    Not an abstraction -- there is no `SurfaceProtocol` and nothing dispatches
    on these.  It is a *list of facts* written once instead of five times, and
    the distinction is chapter 17's: the rule of three is about code, and this
    is data.

    The labels are enumerated and the paths are compared with the constants the
    rest of the program actually uses, and both of those came from mutation
    testing.  The first version said `len(SURFACES) >= 4` and "the label is
    truthy", under which deleting the recordings entry -- the one surface in
    this chapter that broke -- left the test green, and so did pointing a
    surface at a directory this program has never written to.  A list is only
    checked by naming what is supposed to be in it.
    """
    from minicodex.memory import DEFAULT_MEMORY_DIR
    from minicodex.memory_jobs import DEFAULT_JOBS_PATH
    from minicodex.recorder import DEFAULT_DIR as RECORDINGS_DIR
    from minicodex.rollout import DEFAULT_DIR as SESSIONS_DIR
    from minicodex.rules import DEFAULT_RULES_PATH

    expected = {
        "sessions": SESSIONS_DIR,
        "recordings": RECORDINGS_DIR,
        "memory": DEFAULT_MEMORY_DIR,
        "approvals": DEFAULT_RULES_PATH,
        "memory jobs": DEFAULT_JOBS_PATH,
    }
    assert {s.label for s in SURFACES} == set(expected)
    for surface in SURFACES:
        assert surface.policy in {"migrate", "refuse", "rewrite", "recreate"}
        assert surface.path == expected[surface.label], (
            f"{surface.label} names a path nothing in this program reads or writes"
        )


# ---------------------------------------------------------------------------
# F15-02 -- what is actually in the box
# ---------------------------------------------------------------------------
def _data_files(repo_root: Path) -> list[str]:
    package = repo_root / "src" / "minicodex"
    return sorted(
        str(p.relative_to(package.parent)).replace("\\", "/")
        for p in package.rglob("*")
        if p.is_file() and p.suffix not in (".py", ".pyc") and "__pycache__" not in p.parts
    )


@pytest.fixture(scope="module")
def built_wheel(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Built once for the three tests below.

    `repo_root` is function-scoped, so this takes the path the same way that
    fixture does rather than widening it -- a module-scoped fixture is a
    decision about *this* file and should not change one every other test file
    shares.
    """
    out = tmp_path_factory.mktemp("wheel")
    subprocess.run(
        ["uv", "build", "--wheel", "-o", str(out), str(REPO_ROOT)],
        check=True,
        capture_output=True,
    )
    return next(out.glob("*.whl"))


def test_F15_02_every_data_file_the_code_reads_is_in_the_wheel(
    repo_root: Path, built_wheel: Path
) -> None:
    """Chapter -1's test named one file as a string literal.  Three exist now.

    A list computed from the source tree cannot fall behind the source tree,
    which a literal can and did: `permissions.md` and `compaction.md` arrived
    in chapters 5 and 6 and nothing has ever checked that either one is
    shipped.  They are, as it happens.  The point is that nobody knew.
    """
    names = set(zipfile.ZipFile(built_wheel).namelist())
    missing = [f for f in _data_files(repo_root) if f not in names]
    assert not missing, f"the wheel users install is missing {missing}"


def _metadata(wheel: Path) -> str:
    archive = zipfile.ZipFile(wheel)
    name = next(n for n in archive.namelist() if n.endswith(".dist-info/METADATA"))
    return archive.read(name).decode("utf-8")


def test_F15_02_the_artefact_says_who_may_use_it_and_where_to_complain(
    built_wheel: Path,
) -> None:
    """Both absent for seventeen chapters, and both are only visible from the wheel.

    A package with no licence is one a company's legal review removes; a
    package with no `Project-URL` is one whose user cannot find its author
    without already knowing where the source is.  The second is the root of
    F15-03: a report nobody can send is not a report that was worded badly.
    """
    metadata = _metadata(built_wheel)
    assert re.search(r"^License-Expression: \S+", metadata, re.M), "no licence in the metadata"
    assert "Project-URL: Issues" in metadata
    assert ISSUES_URL in metadata
    # The other half of test_F15_01_the_version_has_exactly_one_source: this is
    # the copy of the number that a user ends up with.
    assert f"Version: {minicodex.__version__}" in metadata


def test_F15_02_the_front_page_is_written_for_someone_who_just_installed_it(
    built_wheel: Path,
) -> None:
    """The long description was the step README: "chapter 17: memory, part two".

    That is the right first sentence for a reader of the book and the wrong
    one for the person looking at a package page trying to work out what they
    have.  Asserted on content rather than on the filename, because renaming
    the file back is exactly the change this should catch.
    """
    metadata = _metadata(built_wheel)
    body = metadata.split("\n\n", 1)[1]
    assert "pip install minicodex" in body
    assert ISSUES_URL in body
    assert ".minicodex/" in body, "the front page must say where it puts things"


def test_F15_02_ci_runs_the_oldest_python_the_metadata_promises(repo_root: Path) -> None:
    """`requires-python` is a claim.  This is the thing that makes it true.

    It said `>=3.10` from chapter -1 and no interpreter older than the
    author's laptop ever ran the suite, so the claim was untested for
    seventeen chapters -- and false: `asyncio.wait_for` raises a different
    class on 3.10, and F10-07's timeout handler never fired there.
    """
    try:
        import tomllib
    except ModuleNotFoundError:  # Python 3.10 itself
        import tomli as tomllib

    with (repo_root / "pyproject.toml").open("rb") as fh:
        floor = tomllib.load(fh)["project"]["requires-python"].lstrip(">=")

    wf = yaml.safe_load((repo_root / ".github/workflows/ci.yml").read_text(encoding="utf-8"))
    matrix = wf["jobs"]["check"]["strategy"]["matrix"]["python-version"]
    assert floor in matrix, f"requires-python promises {floor}; CI runs {matrix}"


def test_F15_02_no_asyncio_timeout_is_caught_with_the_builtin(repo_root: Path) -> None:
    """The 3.10 fault, encoded as the rule rather than as the one instance.

    On 3.11+ `asyncio.TimeoutError` *is* the builtin, so both spellings work
    and the wrong one cannot be distinguished from the right one by running
    the tests on a modern interpreter.  Only one spelling works on both, so
    the rule is "use the one that always works" and this is what enforces it.
    Two sites had the builtin; `mcp.py` had had it right since chapter 9.
    """
    package = repo_root / "src" / "minicodex"
    offenders = []
    for path in sorted(package.glob("*.py")):
        text = path.read_text(encoding="utf-8")
        for number, line in enumerate(text.splitlines(), 1):
            stripped = line.strip()
            if not stripped.startswith("except"):
                continue
            # `except (TimeoutError, asyncio.TimeoutError)` is fine: the
            # portable spelling is present. What is not fine is the builtin
            # alone.
            if "asyncio.TimeoutError" in stripped:
                continue
            if re.search(r"\bexcept\s*\(?[^)]*\bTimeoutError\b", stripped):
                offenders.append(f"{path.name}:{number}: {stripped}")
    assert not offenders, "on 3.10 these never fire:\n" + "\n".join(offenders)


# ---------------------------------------------------------------------------
# F15-03 -- a report somebody can act on
# ---------------------------------------------------------------------------
def test_F15_03_the_version_block_names_the_file_that_reproduces_the_run(
    tmp_path: Path,
) -> None:
    """The one line that turns a report into a test.

    Chapter 14 made a recording replayable offline with no key.  What was
    missing was anybody knowing which file that is -- it has a timestamp for a
    name and lives in a hidden directory.

    Two recordings, not one, and the assertions are anchored to their lines.
    Both details are repairs: `assert name in report` passed while the `latest`
    line had been deleted, because the name also appears in the `replay`
    suggestion underneath it; and a single file cannot tell "newest" from "any
    of them", which is the difference between naming the run that broke and
    naming the first run the user ever made.
    """
    recordings = tmp_path / ".minicodex" / "recordings"
    recordings.mkdir(parents=True)
    old = recordings / "session-1786000000.jsonl"
    new = recordings / "session-1786884876.jsonl"
    old.write_text("{}\n", encoding="utf-8")
    new.write_text("{}\n", encoding="utf-8")
    os.utime(old, (1786000000, 1786000000))
    os.utime(new, (1786884876, 1786884876))

    report = environment_report(tmp_path)
    latest = next(line for line in report.splitlines() if line.startswith("latest"))
    assert new.name in latest
    assert old.name not in report
    assert f"minicodex replay {new}" in report
    assert "2 recording(s)" in report


def test_F15_03_the_version_block_reports_what_a_session_silently_depends_on(
    tmp_path: Path,
) -> None:
    """The same list chapter 7 had to write down, one level up.

    `SessionMeta` records cwd, model, sandbox and policy because a transcript
    depends on all four and mentions none of them.  A bug report has that
    problem about the machine instead of about the session, so the answer is
    the same answer.
    """
    report = environment_report(tmp_path)
    assert report.startswith(f"minicodex {minicodex.__version__}")
    for expected in ("python", "install", "cwd", "state", "report"):
        assert re.search(rf"^{expected}", report, re.M), f"{expected} missing from --version"
    assert ISSUES_URL in report


def test_F15_03_no_arguments_is_an_error_not_a_menu(tmp_path: Path) -> None:
    """Help on stdout with exit 0 says "that worked" to everything that is not a human.

    Found by running the program in an empty directory the way a stranger
    would (`probe_release.py firstrun`), which no test had done: every test in
    this repository calls `main([...])` with arguments it chose.
    """
    result = subprocess.run(
        [sys.executable, "-m", "minicodex"], cwd=tmp_path, capture_output=True, text=True
    )
    assert result.returncode == 2
    assert not result.stdout.strip()
    assert "usage: minicodex" in result.stderr


def test_F15_03_the_last_attempt_is_not_announced(monkeypatch: pytest.MonkeyPatch) -> None:
    """`[transport: waiting 7s, attempt 5 of 4]`, then a seven-second sleep, then give up.

    Live in the shipped program, found by watching a terminal rather than by
    reading a return value.  Chapter 12 deleted the attempt-count check inside
    `wait_for` after mutation testing proved it dead -- correctly, the loop is
    the one enforcement point -- and did not notice that the loop was still
    *asking*.  The number in the message is also the number a user would put
    in a bug report, and it is wrong.

    Asserted on what the user sees and on the clock, not on the source.  The
    first version of this test grepped `agent.py` for the fix, which is a test
    of the patch rather than of the behaviour: it stays green if somebody
    reintroduces the bug anywhere else, and goes red on a rewording that
    changes nothing.
    """
    import asyncio

    from minicodex.agent import Wiring
    from minicodex.agent_types import ToolSet
    from minicodex.model import ChatCompletionsModel
    from minicodex.retry import ModelFailed, RetryPolicy

    said: list[str] = []
    attempts = 3
    # A port nothing is listening on: every attempt is a real ConnectError, in
    # the same way the first five minutes of a new install are.
    model = ChatCompletionsModel(base_url="http://127.0.0.1:9/v1", model="none", tools=[])
    wiring = Wiring(
        announce=said.append,
        retry_policy=RetryPolicy(attempts=attempts, base=0.05, cap=0.05, budget=90.0),
    )
    agent = wiring.agent(model, ToolSet(schemas=(), handlers={}))

    # Counted rather than timed.  A wall-clock bound would be measuring the
    # three connection attempts as well, which on this machine are 2.3s each
    # and have nothing to do with the fault; what the fault costs is one extra
    # `sleep`, of up to `cap` -- thirty seconds in the shipped policy.
    slept: list[float] = []
    real_sleep = asyncio.sleep

    async def counting_sleep(seconds: float, *args: object, **kwargs: object) -> object:
        slept.append(seconds)
        return await real_sleep(0, *args, **kwargs)

    monkeypatch.setattr(asyncio, "sleep", counting_sleep)
    with pytest.raises(ModelFailed):
        asyncio.run(agent.run("hello"))

    waits = [line for line in said if "waiting" in line]
    assert len(waits) == attempts - 1, f"one wait per gap, not per attempt: {waits}"
    assert len(slept) == attempts - 1, f"slept before giving up: {slept}"
    for line in waits:
        number = int(re.search(r"attempt (\d+) of", line).group(1))
        assert number <= attempts, f"announced an attempt that will not happen: {line}"


# ---------------------------------------------------------------------------
# F15-04 -- a rename with an end date
# ---------------------------------------------------------------------------
def test_F15_04_no_deprecation_outlives_its_own_removal_version() -> None:
    """The calendar, executable.

    This is the entire mechanism.  A deprecation note in a changelog is a
    promise, and this book's recurring finding is that a promise in prose is
    not a mechanism -- chapter 11's mutation scripts sat unrun in CI for a
    chapter because the text said they ran.  When `__version__` reaches
    `remove_in` this goes red, and the choice at that moment is to delete the
    alias or to move the date **on purpose**.
    """
    for rule in DEPRECATIONS:
        assert version_parts(minicodex.__version__) < version_parts(rule.remove_in), (
            f"`{' '.join(rule.old)}` was to be removed in {rule.remove_in} and this is "
            f"{minicodex.__version__}: delete it, or move the date deliberately"
        )


@pytest.mark.parametrize(
    ("old", "new"),
    [
        (["--yes"], ["--dangerously-approve-all"]),
        (["forget", "2"], ["rules", "--forget", "2"]),
        (["ask", "forget"], ["ask", "forget"]),
        (["rules", "--forget", "2"], ["rules", "--forget", "2"]),
    ],
)
def test_F15_04_old_spellings_are_rewritten_and_nothing_else_is(
    old: list[str], new: list[str]
) -> None:
    """Including the case that makes the rewrite safe.

    `forget` is a word as well as a command.  Rewriting it anywhere but the
    first position would turn a question into a different command, which is a
    far worse failure than the rename it is there to soften.
    """
    rewritten, _ = apply_deprecations(old)
    assert rewritten == new


def test_F15_04_a_deprecation_warns_once_per_run() -> None:
    rewritten, warnings = apply_deprecations(["--yes", "--yes"])
    assert rewritten == ["--dangerously-approve-all", "--dangerously-approve-all"]
    assert len(warnings) == 1
    assert "0.3.0" in warnings[0]


def test_F15_04_the_warning_goes_to_stderr_and_the_command_still_works(
    tmp_path: Path,
) -> None:
    """A deprecation that changes a program's output changes its callers' behaviour.

    The whole point of the warning period is that scripts keep working while
    people update them, so the warning must not appear in what a script reads.
    """
    result = subprocess.run(
        [sys.executable, "-m", "minicodex", "forget", "1"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
    )
    assert "deprecated" in result.stderr
    assert "deprecated" not in result.stdout


def test_F15_04_every_deprecation_is_in_the_changelog(repo_root: Path) -> None:
    """A rename nobody is told about is a rename that breaks people in silence."""
    changelog = (repo_root / "CHANGELOG.md").read_text(encoding="utf-8")
    for rule in DEPRECATIONS:
        assert " ".join(rule.old) in changelog
        assert rule.remove_in in changelog


# ---------------------------------------------------------------------------
# F15-05 -- what happens when other people arrive
# ---------------------------------------------------------------------------
def test_F15_05_the_project_says_what_happens_to_a_pull_request(repo_root: Path) -> None:
    """codex's choice, and the reasoning rather than the rule.

    "Not accepted" without the arithmetic reads as rudeness; with it, it reads
    as an unpaid maintainer declining to guess on a stranger's behalf about
    changes whose correctness is not visible in the diff.
    """
    text = (repo_root / "CONTRIBUTING.md").read_text(encoding="utf-8")
    assert "Pull requests" in text
    assert "not accepted" in text.lower()
    assert "recording" in text.lower(), "say what to send instead"


def test_F15_05_the_bug_template_asks_for_exactly_what_version_prints(
    repo_root: Path, tmp_path: Path
) -> None:
    """The form and the program, pinned to each other.

    A template is a document, and documents drift from programs -- this book
    has recorded that four times now.  What stops it is that the form asks for
    `minicodex --version` **by name** and this asserts the command exists and
    produces the two things the form is built around.
    """
    template = yaml.safe_load(
        (repo_root / ".github/ISSUE_TEMPLATE/bug_report.yml").read_text(encoding="utf-8")
    )
    labels = [
        field["attributes"]["label"]
        for field in template["body"]
        if "label" in field.get("attributes", {})
    ]
    assert "minicodex --version" in labels
    assert any("Recording" in label for label in labels)

    report = environment_report(tmp_path)
    assert "report" in report and "latest" in report

    # And the half of CONTRIBUTING.md that executes: with blank issues on, the
    # form is a suggestion. Every field above exists because a report arrived
    # without it, and a suggestion is what those reports were.
    config = yaml.safe_load(
        (repo_root / ".github/ISSUE_TEMPLATE/config.yml").read_text(encoding="utf-8")
    )
    assert config["blank_issues_enabled"] is False


def test_F15_05_a_release_check_refuses_a_tag_that_disagrees_with_the_version(
    repo_root: Path, tmp_path: Path
) -> None:
    """The one check that cannot be a test, tested.

    Publishing is the only irreversible step in this repository: a file on
    PyPI is immutable and yanking does not take it back from anyone who
    already has it.  So the tag and the version have to agree *before* the
    upload, and the workflow runs this script above the publish step.
    """
    script = repo_root / "scripts" / "check_release.py"
    good = subprocess.run(
        [sys.executable, str(script), f"v{minicodex.__version__}"], capture_output=True, text=True
    )
    assert good.returncode == 0, good.stderr

    bad = subprocess.run([sys.executable, str(script), "v9.9.9"], capture_output=True, text=True)
    assert bad.returncode == 1
    assert "does not match" in bad.stderr

    # And the other half, against a tree whose changelog has no entry for the
    # version being released. Deleting that check left every test green until
    # this existed: the only tree the script had ever been pointed at was one
    # whose changelog was correct.
    fake = tmp_path / "repo"
    (fake / "src" / "minicodex").mkdir(parents=True)
    (fake / "src" / "minicodex" / "__init__.py").write_text(
        f'__version__ = "{minicodex.__version__}"' + chr(10), encoding="utf-8"
    )
    (fake / "CHANGELOG.md").write_text("# Changelog\n\n## 0.0.1 — 2026-01-01\n", encoding="utf-8")
    missing = subprocess.run(
        [sys.executable, str(script), "--root", str(fake), f"v{minicodex.__version__}"],
        capture_output=True,
        text=True,
    )
    assert missing.returncode == 1
    assert "CHANGELOG" in missing.stderr


def test_F15_05_the_release_workflow_publishes_last(repo_root: Path) -> None:
    """Ordering, asserted, because it is the only property of that file that matters.

    Every step that can refuse has to be above the one that cannot be undone.
    A comment saying so would survive somebody moving the publish step up to
    "get the artefact out faster while the tests finish".
    """
    wf = yaml.safe_load((repo_root / ".github/workflows/release.yml").read_text(encoding="utf-8"))
    steps = wf["jobs"]["release"]["steps"]
    names = [s.get("name", s.get("uses", "")) for s in steps]
    publish = next(i for i, n in enumerate(names) if "Publish" in n)
    for label in ("Release checks", "Test", "Build", "Install the artefact"):
        index = next(i for i, n in enumerate(names) if n.startswith(label))
        assert index < publish, f"{label!r} runs after the upload"
    assert publish == len(steps) - 1
