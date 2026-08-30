"""Do chapter 15's tests fail when chapter 15's promises are broken?

    uv run python probe_mutations_ch15.py

Same script as chapters 9 through 17 with one change: the paths are relative to
the repository root rather than to `src/minicodex`.  Half of what this chapter
ships is not Python -- it is `pyproject.toml`, two workflow files and an issue
template -- and those are exactly the files where "nobody would ever change
that" has been wrong before.  A mutation script that can only edit the package
can only check the half of the chapter that is code.
"""

from __future__ import annotations

import atexit
import re
import signal
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PKG = "src/minicodex"

MUTATIONS = [
    # ---- one version number (F15-01) ----------------------------------------
    (
        "pyproject.toml",
        "the version is a literal here as well as in the module",
        'dynamic = ["version"]',
        'version = "0.0.9"',
    ),
    (
        f"{PKG}/release.py",
        "version comparison is lexical, so 0.10 sorts below 0.9",
        "    return tuple(int(part) for part in version.split('-')[0].split('.'))".replace(
            "'", '"'
        ),
        '    return tuple(version.split("-")[0].split("."))  # type: ignore[return-value]',
    ),
    # ---- old files (F15-01) -------------------------------------------------
    (
        f"{PKG}/replay.py",
        "a recording older than the attempt field crashes instead of migrating",
        'key = (payload["turn"], payload.get("attempt", 0))',
        'key = (payload["turn"], payload["attempt"])',
    ),
    (
        f"{PKG}/replay.py",
        "a tool call with no arguments is filled in with an empty object",
        '        if "arguments" not in call:',
        "        if False:",
    ),
    (
        f"{PKG}/release.py",
        "the list of on-disk surfaces loses the one nobody thinks about",
        '    Surface("recordings", RECORDINGS_DIR, "none", "refuse", "names the missing field"),',
        "",
    ),
    # ---- what is in the box (F15-02) ----------------------------------------
    (
        "pyproject.toml",
        "the artefact ships without a licence",
        'license = "MIT"',
        "",
    ),
    (
        "pyproject.toml",
        "the artefact ships with no way to reach its author",
        "urls = { Homepage",
        "_urls = { Homepage",
    ),
    (
        "pyproject.toml",
        "the package front page goes back to being a chapter of a book",
        'readme = "PACKAGE.md"',
        'readme = "README.md"',
    ),
    (
        ".github/workflows/ci.yml",
        "CI stops running the oldest python the metadata promises",
        'python-version: ["3.10", "3.13"]',
        'python-version: ["3.13"]',
    ),
    (
        f"{PKG}/subagent.py",
        "a sub-agent timeout is caught with the builtin, which 3.10 never raises",
        "    except asyncio.TimeoutError:\n        partial = await _stop(task)",
        "    except TimeoutError:\n        partial = await _stop(task)",
    ),
    # ---- a report somebody can act on (F15-03) ------------------------------
    (
        f"{PKG}/release.py",
        "--version stops naming the recording that reproduces the run",
        '        lines.append(f"latest    {newest}")',
        "        pass",
    ),
    (
        f"{PKG}/release.py",
        "--version stops saying where to send it",
        '    lines.append(f"report    {ISSUES_URL}")',
        "    pass",
    ),
    (
        f"{PKG}/__main__.py",
        "no arguments is a success again",
        "    parser.print_help(sys.stderr)\n    return 2",
        "    parser.print_help()\n    return 0",
    ),
    (
        f"{PKG}/agent.py",
        "the loop asks for a backoff after its last attempt",
        "        last = attempt + 1 >= self.retry_policy.attempts",
        "        last = False",
    ),
    # ---- a rename with an end date (F15-04) ---------------------------------
    (
        f"{PKG}/release.py",
        "a deprecation outlives the release it was to be removed in",
        '        remove_in="0.3.0",\n        why="it reads as an answer',
        '        remove_in="0.1.0",\n        why="it reads as an answer',
    ),
    (
        f"{PKG}/release.py",
        "the old spellings stop working",
        "            out.extend(rule.new)",
        "            out.append(token)",
    ),
    (
        f"{PKG}/release.py",
        "a command word is rewritten wherever it appears, including inside a question",
        "            if positional and index != 0:",
        "            if False:",
    ),
    (
        f"{PKG}/release.py",
        "the warning repeats once per occurrence",
        "            if rule.old not in seen:",
        "            if True:",
    ),
    (
        f"{PKG}/__main__.py",
        "the deprecation warning goes to stdout, where a script reads it",
        "        print(warning, file=sys.stderr)",
        "        print(warning)",
    ),
    # ---- other people (F15-05) ----------------------------------------------
    (
        "scripts/check_release.py",
        "a tag that disagrees with the version publishes anyway",
        "        if tag != version:",
        "        if False:",
    ),
    (
        "scripts/check_release.py",
        "a release with no changelog entry publishes anyway",
        "    if not re.search(",
        "    if False and re.search(",
    ),
    (
        ".github/workflows/release.yml",
        "the upload happens before the artefact has been installed anywhere",
        "      - name: Install the artefact somewhere empty and run it",
        "      - name: zzz-moved-below-publish",
    ),
    # ---- deliberately aimed at things no test was written for ---------------
    #
    # The twenty-two above were written after the tests, by the same person, on
    # the same afternoon, which is the weakest form of this check: of course
    # they are caught.  These five were chosen the other way round -- by asking
    # what this chapter changed that nothing asserts -- and are the only ones
    # here that could tell me something I did not already know.
    (
        f"{PKG}/release.py",
        "--version reports the *oldest* recording, which is never the one that broke",
        '        (root / RECORDINGS_DIR).glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True',  # noqa: E501
        '        (root / RECORDINGS_DIR).glob("*.jsonl"), key=lambda p: p.stat().st_mtime',
    ),
    (
        f"{PKG}/release.py",
        "--version reports a session count of zero however many there are",
        "def _count(directory: Path, pattern: str) -> int:\n    return len(list(directory.glob(pattern))) if directory.is_dir() else 0",  # noqa: E501
        "def _count(directory: Path, pattern: str) -> int:\n    return 0",
    ),
    (
        f"{PKG}/__main__.py",
        "the renamed flag no longer reaches the approver",
        '        dest="approve_all",',
        "",
    ),
    (
        f"{PKG}/release.py",
        "a surface points at a path this program does not use",
        '    Surface("sessions", SESSIONS_DIR,',
        '    Surface("sessions", Path("nowhere"),',
    ),
    (
        ".github/ISSUE_TEMPLATE/config.yml",
        "blank issues are enabled again, so the template is optional",
        # The leading newline anchors this to the setting rather than to the
        # comment above it that quotes the setting.  Without it, `replace(...,
        # 1)` edited the comment, the file's behaviour did not change, and this
        # script reported a survivor -- a mutation that was never applied,
        # presented as a gap in the tests.  The other direction of the same
        # mistake is two entries up the file: nine rows reported as caught by a
        # failure that had nothing to do with them.
        "\nblank_issues_enabled: false",
        "\nblank_issues_enabled: true",
    ),
]

ORIGINALS = {name: (ROOT / name).read_text(encoding="utf-8") for name, *_ in MUTATIONS}
SUITES = [
    "tests/test_faults_ch15.py",
    "tests/test_packaging.py",
    "tests/test_faults_ch14.py",
]


def restore() -> None:
    for name, text in ORIGINALS.items():
        path = ROOT / name
        if path.read_text(encoding="utf-8") != text:
            path.write_text(text, encoding="utf-8")


atexit.register(restore)
signal.signal(signal.SIGINT, lambda *_: sys.exit(130))


def main() -> None:
    print(f"{len(MUTATIONS)} mutations, {' '.join(SUITES)}\n")
    survivors = []
    for name, label, before, after in MUTATIONS:
        path = ROOT / name
        source = ORIGINALS[name]
        if before not in source:
            print(f"  !! could not apply: {label}")
            survivors.append(label)
            continue
        path.write_text(source.replace(before, after, 1), encoding="utf-8")
        try:
            result = subprocess.run(
                [sys.executable, "-m", "pytest", *SUITES, "-q", "--no-header"],
                timeout=900,
                capture_output=True,
                text=True,
                # Not `text=True` alone: that decodes with the *locale's*
                # codec, which on a zh-CN Windows box is GBK, and a suite
                # whose failure output contains CJK then dies with a
                # `UnicodeDecodeError` inside subprocess's reader thread --
                # leaving `result.stdout` as `None` and the counting below
                # crashing on it. Chapter 2's F02-07 rule ("a bad byte should
                # come back slightly wrong, not as an exception that discards
                # the whole read") applies to this program's own tools too.
                encoding="utf-8",
                errors="replace",
                cwd=ROOT,
            )
        except subprocess.TimeoutExpired:
            print(f"  !! the suite did not finish: {label}")
            survivors.append(label)
            continue
        finally:
            restore()
        caught = len(re.findall(r"^FAILED ", result.stdout, re.M)) + len(
            re.findall(r"^ERROR ", result.stdout, re.M)
        )
        print(f"  {caught:>3} test(s) fail  <-  {label}")
        if not caught:
            survivors.append(label)

    print()
    if survivors:
        print(f"{len(survivors)} mutation(s) nothing noticed:")
        for label in survivors:
            print(f"  - {label}")
        raise SystemExit(1)
    print("every mutation was caught.")


if __name__ == "__main__":
    main()
