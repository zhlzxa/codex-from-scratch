"""What shipping this thing actually costs, measured against the thing itself.

    uv run python probe_release.py compat        # old versions write, this one reads
    uv run python probe_release.py wheel         # build it, look inside, install it clean
    uv run python probe_release.py firstrun      # the first five minutes, exit codes and all
    uv run python probe_release.py deprecated    # what deleting a spelling would cost

Every other chapter's probes measure the program.  These measure the
**artefact** -- the file a stranger downloads -- and the difference turns out to
matter, because three of the four sections below found something that seventeen
chapters of tests are structurally unable to see.  The tests import
`minicodex`; a user installs a wheel.  The tests call functions; a user reads a
terminal.  The tests run one version; a user has files written by the last one.

`compat` is the interesting one and it is only possible because of an accident
of how this book is delivered: `steps/step05_approval` through
`steps/step17_memory_write` are thirteen complete, installed, runnable versions
of this program, in order.  That is a release history with working interpreters
attached, so "does the new code read the old code's files" is a question that
can be *run* here rather than reasoned about.
"""

from __future__ import annotations

import argparse
import os
import platform
import shutil
import subprocess
import sys
import time
import zipfile
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
STEPS = HERE.parent

# The stub (chapter 0) replays real Ollama bytes, and what it asks for is
# `read_file("src/minicodex/__init__.py")`.  Every version below is run with
# its own directory as cwd, so every one of them has that file to read -- which
# is why the recorded conversation is identical across versions and any
# difference in the artefacts is a difference in the *program*.
STUB_PORT = 11477
QUESTION = "What does src/minicodex/__init__.py define?"


def _venv_python(step: Path) -> Path | None:
    for candidate in ("Scripts/python.exe", "bin/python"):
        path = step / ".venv" / candidate
        if path.exists():
            return path
    return None


# Release order, written out.  The first version of this function sorted the
# directory names and called the result "book order", which put
# `stepA_refactor` and `stepB_boundaries` at the end -- seven and six chapters
# after where they belong.  The output still looked like a table, and the two
# rows that were furthest out of place were the two whose position carried the
# finding.  A version order is not a string order, which is the entire reason
# semantic versioning is a specification and not a naming convention.
_ORDER = (
    "step-1_setup",
    "step00_minimal_loop",
    "step01_protocol",
    "step02_shell_tool",
    "step03_tool_descriptions",
    "step04_apply_patch",
    "stepA_refactor",
    "step05_approval",
    "step06_compaction",
    "step07_resume",
    "step08_concurrency",
    "step09_mcp",
    "step10_subagents",
    "stepB_boundaries",
    "step11_plan",
    "step12_retry",
    "step13_system_prompt",
    "step14_eval",
    "step16_memory_read",
    "step17_memory_write",
)


def _historical_steps() -> list[Path]:
    """Every sibling step that has an installed environment, in release order."""
    out = []
    for name in _ORDER:
        step = STEPS / name
        if step != HERE and step.is_dir() and _venv_python(step):
            out.append(step)
    return out


# ---------------------------------------------------------------------------
# compat -- old versions write, this one reads
# ---------------------------------------------------------------------------
def _run_one_version(step: Path, out: Path) -> dict[str, Any]:
    """Run one historical version against the stub and collect what it wrote.

    Recordings have no `--session-dir`-style flag: `.minicodex/recordings/` is
    hard-coded, so this has to run in the step's own directory and take the
    file out afterwards.  Which files were already there is snapshotted first,
    because a probe that deletes somebody else's artefacts is chapter 6's
    mutation script all over again -- a tool that edits your tree is a tool
    that can leave your tree edited.
    """
    python = _venv_python(step)
    assert python is not None
    recordings = step / ".minicodex" / "recordings"
    # Whether the *directory* was already there, not only which files were.
    # The first version restored the files and left an empty `.minicodex/` in
    # six step directories that had never had one -- a smaller version of the
    # same mistake it was written to avoid, and the third time in this book
    # that a probe has left something behind.
    existed = recordings.exists()
    before = {p.name for p in recordings.glob("*.jsonl")} if existed else set()

    sessions = out / "sessions"
    base = [str(python), "-m", "minicodex", "ask", QUESTION]
    base += ["--base-url", f"http://127.0.0.1:{STUB_PORT}/v1"]
    # `--session-dir` arrived with rollouts in chapter 7, so the two versions
    # before that reject it with exit 2.  Falling back rather than skipping
    # them: those are precisely the versions whose *recordings* are furthest
    # from the current reader, which is the thing being measured.
    result = subprocess.run(
        [*base, "--session-dir", str(sessions)],
        cwd=step,
        capture_output=True,
        text=True,
        timeout=180,
    )
    if result.returncode == 2 and "--session-dir" in (result.stderr or ""):
        result = subprocess.run(base, cwd=step, capture_output=True, text=True, timeout=180)
    record: dict[str, Any] = {"step": step.name, "ran": result.returncode == 0}
    if result.returncode != 0:
        record["error"] = (result.stderr or result.stdout).strip().splitlines()[-1:]
        return record

    after = {p.name for p in recordings.glob("*.jsonl")} if recordings.exists() else set()
    new = sorted(after - before)
    if new:
        source = recordings / new[-1]
        shutil.copy2(source, out / "recording.jsonl")
        for name in new:  # leave the directory as it was found
            (recordings / name).unlink()
        record["recording"] = "recording.jsonl"
    if not existed and recordings.exists() and not any(recordings.parent.rglob("*.jsonl")):
        shutil.rmtree(recordings.parent)
    written = sorted(sessions.glob("*.jsonl")) if sessions.exists() else []
    if written:
        record["session"] = written[-1].name
    return record


def _read_with_current(out: Path, record: dict[str, Any]) -> dict[str, str]:
    """Read those artefacts with the code in *this* directory."""
    from minicodex.replay import ReplayError, load
    from minicodex.rollout import RolloutError, read_rollout

    verdicts: dict[str, str] = {}

    # `except Exception` and not the declared error types, on purpose: the
    # question this probe asks is "what happens", and a reader that crashes
    # with a KeyError is a different answer from one that refuses with a
    # sentence.  Catching only `ReplayError` would have hidden the first
    # finding of this section behind a traceback that killed the sweep.
    if "session" in record:
        try:
            rollout = read_rollout(out / "sessions" / record["session"])
            history, _ = rollout.history()
            verdicts["session"] = f"ok, {len(history.items)} item(s)"
        except RolloutError as exc:
            verdicts["session"] = f"REFUSED: {exc}"
        except Exception as exc:
            verdicts["session"] = f"CRASH: {type(exc).__name__}: {exc}"
    else:
        verdicts["session"] = "-"

    if "recording" in record:
        try:
            recording = load(out / record["recording"])
            verdicts["recording"] = f"ok, {recording.turns} turn(s)"
        except ReplayError as exc:
            verdicts["recording"] = f"REFUSED: {str(exc).split(': ', 1)[-1][:60]}"
        except Exception as exc:
            verdicts["recording"] = f"CRASH: {type(exc).__name__}: {exc}"
    else:
        verdicts["recording"] = "-"
    return verdicts


def compat(args: argparse.Namespace) -> int:
    steps = _historical_steps()
    if not steps:
        print("no sibling steps with an environment; nothing to compare against")
        return 0

    workspace = HERE / ".probe" / "compat"
    if workspace.exists():
        shutil.rmtree(workspace)
    workspace.mkdir(parents=True)

    stub = subprocess.Popen(
        [sys.executable, "-m", "minicodex", "serve-stub", "--port", str(STUB_PORT)],
        cwd=HERE,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    time.sleep(2.0)
    rows = []
    try:
        for step in steps:
            out = workspace / step.name
            out.mkdir()
            record = _run_one_version(step, out)
            if not record["ran"]:
                print(f"  {step.name:<26} did not run: {record.get('error')}")
                continue
            verdicts = _read_with_current(out, record)
            rows.append((step.name, verdicts))
            print(
                f"  {step.name:<26} session: {verdicts['session']:<18} "
                f"recording: {verdicts['recording']}"
            )
    finally:
        stub.terminate()
        stub.wait(timeout=10)

    print()
    print(f"  {len(rows)} version(s) read by this one.")
    broken = [name for name, v in rows if any(x.startswith("REFUSED") for x in v.values())]
    if broken:
        print(f"  refused: {', '.join(broken)}")
    if args.corpus:
        _write_corpus(workspace, rows)
    return 0


def _write_corpus(workspace: Path, rows: list[tuple[str, dict[str, str]]]) -> None:
    """Freeze the oldest readable artefact of each kind into the test fixtures.

    The probe above needs thirteen installed environments and four minutes.
    The *check* has to run in CI on a clean machine in milliseconds, so what
    ships is the files, not the ability to regenerate them.  This is chapter
    14's recording argument at a different scale: an artefact captured once
    from something real beats a fixture somebody typed.
    """
    corpus = HERE / "tests" / "fixtures" / "compat"
    corpus.mkdir(parents=True, exist_ok=True)
    written = []
    for name, _ in rows:
        source = workspace / name
        target = corpus / name
        target.mkdir(exist_ok=True)
        for kind, glob in (("recording.jsonl", "recording.jsonl"), ("session", "sessions/*.jsonl")):
            found = sorted(source.glob(glob))
            if found:
                shutil.copy2(found[-1], target / ("session.jsonl" if kind == "session" else kind))
                written.append(f"{name}/{found[-1].name}")
    print(f"  corpus: {len(written)} file(s) under tests/fixtures/compat/")


# ---------------------------------------------------------------------------
# wheel -- build it, look inside it, install it somewhere clean
# ---------------------------------------------------------------------------
def _data_files_read_at_runtime() -> list[str]:
    """Every non-.py file the shipped package opens, found by reading the code.

    Chapter -1's test names `prompts/system.md` as a string literal, which
    pinned the one file that existed when it was written.  Two more arrived
    later and neither is checked by anything.  A list computed from the source
    tree cannot fall behind the source tree.
    """
    package = HERE / "src" / "minicodex"
    return sorted(
        str(p.relative_to(package.parent)).replace(os.sep, "/")
        for p in package.rglob("*")
        if p.is_file() and p.suffix not in (".py", ".pyc") and "__pycache__" not in p.parts
    )


def wheel(args: argparse.Namespace) -> int:
    out = HERE / ".probe" / "dist"
    if out.exists():
        shutil.rmtree(out)
    subprocess.run(
        ["uv", "build", "--wheel", "-o", str(out), str(HERE)], check=True, capture_output=True
    )
    built = next(out.glob("*.whl"))
    names = set(zipfile.ZipFile(built).namelist())
    print(f"  built {built.name}: {len(names)} entries")

    missing = [f for f in _data_files_read_at_runtime() if f not in names]
    print(
        f"  data files the code reads at runtime: {len(_data_files_read_at_runtime())}, "
        f"missing from the wheel: {len(missing)}"
    )
    for name in missing:
        print(f"    MISSING {name}")

    metadata = ""
    for name in names:
        if name.endswith("METADATA"):
            metadata = zipfile.ZipFile(built).read(name).decode("utf-8", "replace")
    headers = [line.split(":", 1)[0] for line in metadata.splitlines() if ": " in line]
    for field in ("License-Expression", "License", "Project-URL"):
        print(f"  {field:<20} {'present' if field in headers else 'ABSENT'}")

    # The part no test can do, because a test runs inside the environment the
    # tests were written in: put the artefact somewhere that has nothing.
    clean = HERE / ".probe" / "clean"
    if clean.exists():
        shutil.rmtree(clean)
    subprocess.run([sys.executable, "-m", "venv", str(clean)], check=True, capture_output=True)
    python = _venv_python(clean.parent) or (
        clean / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    )
    subprocess.run(
        [str(python), "-m", "pip", "install", "--quiet", str(built)],
        check=True,
        capture_output=True,
    )
    probe = (
        "import importlib, pkgutil, minicodex, sys;"
        "mods=[m.name for m in pkgutil.iter_modules(minicodex.__path__)];"
        "bad=[];\n"
        "for m in mods:\n"
        "    try: importlib.import_module('minicodex.'+m)\n"
        "    except Exception as e: bad.append((m, type(e).__name__, str(e)))\n"
        "print(len(mods), 'modules,', len(bad), 'failed to import');"
        "[print('   ', *b) for b in bad]"
    )
    result = subprocess.run([str(python), "-c", probe], capture_output=True, text=True)
    print(f"  clean venv: {result.stdout.strip() or result.stderr.strip()}")
    version = subprocess.run(
        [str(python), "-m", "minicodex", "--version"], capture_output=True, text=True
    )
    for line in version.stdout.splitlines():
        print(f"    | {line}")
    return 0


# ---------------------------------------------------------------------------
# firstrun -- the first five minutes, as a stranger has them
# ---------------------------------------------------------------------------
FIRST_COMMANDS: list[list[str]] = [
    [],
    ["--version"],
    ["rules"],
    ["sessions"],
    ["memory"],
    ["ask", "hello"],  # nothing is listening on the default base url
]


def firstrun(args: argparse.Namespace) -> int:
    """Run each of those in an empty directory and report what a stranger sees.

    Exit codes are printed because they are the half of a CLI's behaviour that
    no human notices and every script depends on, and stdout/stderr are kept
    apart for the same reason: `minicodex | less` shows one of the two.
    """
    workspace = HERE / ".probe" / "firstrun"
    if workspace.exists():
        shutil.rmtree(workspace)
    workspace.mkdir(parents=True)

    for argv in FIRST_COMMANDS:
        result = subprocess.run(
            [sys.executable, "-m", "minicodex", *argv],
            cwd=workspace,
            capture_output=True,
            text=True,
            timeout=180,
        )
        shown = " ".join(argv) or "(no arguments)"
        print(f"\n  $ minicodex {shown}          [exit {result.returncode}]")
        for stream, text in (("out", result.stdout), ("err", result.stderr)):
            for line in text.strip().splitlines()[: args.lines]:
                print(f"    {stream} | {line}")
    return 0


# ---------------------------------------------------------------------------
# deprecated -- what deleting a spelling would cost
# ---------------------------------------------------------------------------
SPELLINGS = {
    "--yes": "--dangerously-approve-all",
    "minicodex forget": "minicodex rules --forget",
}


def deprecated(args: argparse.Namespace) -> int:
    """Count the call sites of each old spelling in this repository alone.

    This is the cheapest possible answer to "can I just delete it", and it is
    an argument from the *lower bound*: these are the uses by the one person
    who knows the change is coming.  Everything a user wrote is invisible from
    here and is not smaller.
    """
    roots = [HERE, HERE.parent.parent / "tutorial"]
    for old, new in SPELLINGS.items():
        hits: list[tuple[str, int]] = []
        for root in roots:
            if not root.exists():
                continue
            for path in root.rglob("*"):
                if path.suffix not in (".py", ".md", ".yml") or "__pycache__" in path.parts:
                    continue
                if ".venv" in path.parts or ".probe" in path.parts:
                    continue
                try:
                    text = path.read_text(encoding="utf-8")
                except (OSError, UnicodeDecodeError):
                    continue
                count = text.count(old)
                if count:
                    hits.append((str(path.relative_to(HERE.parent.parent)), count))
        total = sum(c for _, c in hits)
        print(f"\n  {old!r} -> {new!r}: {total} occurrence(s) in {len(hits)} file(s)")
        for name, count in sorted(hits, key=lambda h: -h[1])[: args.files]:
            print(f"    {count:>3}  {name}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="section", required=True)

    p = sub.add_parser("compat", help="old versions write, this one reads")
    p.add_argument("--corpus", action="store_true", help="freeze the results into test fixtures")
    p.set_defaults(fn=compat)

    p = sub.add_parser("wheel", help="build it, look inside, install it clean")
    p.set_defaults(fn=wheel)

    p = sub.add_parser("firstrun", help="the first five minutes")
    p.add_argument("--lines", type=int, default=6)
    p.set_defaults(fn=firstrun)

    p = sub.add_parser("deprecated", help="what deleting a spelling would cost")
    p.add_argument("--files", type=int, default=8)
    p.set_defaults(fn=deprecated)

    args = parser.parse_args()
    print(f"\n== {args.section} == ({platform.python_version()})\n")
    return int(args.fn(args))


if __name__ == "__main__":
    raise SystemExit(main())
