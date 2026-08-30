"""Do chapter 18's tests fail when chapter 18's code is wrong?

Same script as chapters 9 through 17: break one line, run the suite, and see
whether anything turns red.  A mutation nothing notices is not a mutation
that does not matter -- it is a hole in the tests, and the ones this script
found on its first run are written up in the chapter under their own fault
numbers rather than quietly patched.

    uv run python probe_mutations_ch18.py
"""

from __future__ import annotations

import atexit
import re
import signal
import subprocess
import sys
from pathlib import Path

MUTATIONS = [
    # ---- discovery -----------------------------------------------------------
    (
        "skills.py",
        "a missing skills directory raises instead of coming back empty",
        "    if not directory.is_dir():",
        "    if False:",
    ),
    (
        "skills.py",
        "two skills may claim the same name; both stay in the catalog",
        "        if skill is None or skill.name in seen:",
        "        if skill is None:",
    ),
    (
        "skills.py",
        "the directory order is whatever the filesystem hands back",
        "    for entry in sorted(directory.iterdir()):",
        "    for entry in directory.iterdir():",
    ),
    (
        "skills.py",
        "the cap on how many SKILL.md files one pass reads is not a cap",
        "        if len(found) >= max_skills:",
        "        if False:",
    ),
    (
        "skills.py",
        "an unreadable SKILL.md takes the run down with it",
        '    try:\n        text = skill_md.read_text(encoding="utf-8", errors="replace")\n'
        "    except OSError:\n        return None",
        '    text = skill_md.read_text(encoding="utf-8", errors="replace")',
    ),
    # ---- parsing -------------------------------------------------------------
    (
        "skills.py",
        "a skill with no description is admitted to the catalog",
        "    if not name or not description:",
        "    if not name:",
    ),
    (
        "skills.py",
        "a skill with no name is admitted to the catalog",
        "    if not name or not description:",
        "    if not description:",
    ),
    (
        "skills.py",
        "a file with no frontmatter at all is parsed as a skill",
        "    if parsed is None:\n        return None",
        "    if parsed is None:\n        parsed = ({}, text)",
    ),
    (
        "skills.py",
        "a quoted description keeps its quotes",
        '        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\\"\'":',
        "        if False:",
    ),
    (
        "skills.py",
        "the 1024-character cap on the resident description is not applied",
        "    if len(description) > MAX_DESCRIPTION_CHARS:",
        "    if False:",
    ),
    # ---- the catalog ---------------------------------------------------------
    (
        "skills.py",
        "the catalog carries every skill's full body, so nothing is left to read",
        '    lines = "\\n".join(skill.catalog_line() for skill in skills.skills)',
        '    lines = "\\n".join(\n'
        '        skill.catalog_line() + "\\n" + skill.body for skill in skills.skills\n'
        "    )",
    ),
    (
        "skills.py",
        "the catalog is not capped, so a big skills directory is a second system prompt",
        '    if estimate_messages([{"role": "user", "content": text}]) <= budget:\n'
        "        return text",
        "    if True:\n        return text",
    ),
    (
        "skills.py",
        "the catalog is cut mid-line, naming a skill read_skill cannot find",
        "        kept.append(line)",
        "        kept.append(line[: len(line) // 2])",
    ),
    (
        "skills.py",
        "the trimmed catalog does not say that it was trimmed",
        '    return f"{trimmed}\\n{_TRUNCATION_MARK} at {budget} tokens;'
        ' more skills exist on disk ...]"',
        "    return trimmed",
    ),
    (
        "skills.py",
        "a closing marker inside a skill body ends the data block early",
        "    safe = text.replace(close_marker,"
        ' close_marker.replace("<", "&lt;").replace(">", "&gt;"))',
        "    safe = text",
    ),
    # ---- the tool ------------------------------------------------------------
    (
        "skills.py",
        "read_skill rebuilds a path from the model's string instead of looking it up",
        "        skill = skills.by_name(name.strip())",
        "        _candidate = Path(skills.directory) / name.strip() / SKILL_FILENAME\n"
        "        skill = (\n"
        "            _load_one(_candidate)\n"
        "            if _candidate.is_file()\n"
        "            else skills.by_name(name.strip())\n"
        "        )",
    ),
    (
        "skills.py",
        "read_skill may be called as many times as the model likes",
        "        if spent >= budget:",
        "        if False:",
    ),
    (
        "skills.py",
        "a name that matches nothing comes back as an empty success",
        "        if skill is None:\n            listed =",
        '        if skill is None:\n            return ""\n            listed =',
    ),
    (
        "skills.py",
        "a missing name argument is read as the empty string",
        "        if not isinstance(name, str) or not name.strip():",
        "        if False:",
    ),
    (
        "skills.py",
        "the skill body is handed back unfenced, as prose rather than as data",
        "        return _fence(SKILL_OPEN_FENCE, SKILL_CLOSE_FENCE, skill.body)",
        "        return skill.body",
    ),
    # ---- F18-14: the default path is a path, not a tool -----------------------
    (
        "skills.py",
        "the catalog line drops the path, leaving nothing to open",
        '        return f"- {self.name}: {self.description} '
        '({FILE_LOCATOR_KIND}: {self.locator()})"',
        '        return f"- {self.name}: {self.description}"',
    ),
    (
        "skills.py",
        "the catalog prints a relative path, which resolve() looks for under the repository",
        '        return str(self.path.resolve()).replace("\\\\", "/")',
        '        return str(self.path).replace("\\\\", "/")',
    ),
    (
        "skills.py",
        "the default instructions describe read_skill, a tool the default run does not have",
        "    if skill_tool:",
        "    if True:",
    ),
    (
        "skills.py",
        "the opt-in instructions describe opening a path, with no mention of the tool mounted",
        "    if skill_tool:",
        "    if False:",
    ),
    # ---- F18-15: delivered once, as a developer note --------------------------
    (
        "skills.py",
        "the catalog is re-sent on every turn, accumulating in an append-only history",
        "        if self._delivered:\n            return None",
        "        if False:\n            return None",
    ),
    (
        "skills.py",
        "the watcher never delivers the catalog at all",
        "        self._delivered = True\n        return catalog_block",
        "        self._delivered = True\n        return None  # catalog_block",
    ),
    # ---- wiring --------------------------------------------------------------
    (
        "composition.py",
        "read_skill is offered whether or not any skill exists",
        "    if skills is not None and skills and skill_tool:",
        "    if skills is not None and skill_tool:",
    ),
    (
        "composition.py",
        "read_skill is mounted by default, without anybody asking for it",
        "    if skills is not None and skills and skill_tool:",
        "    if skills is not None and skills:",
    ),
    (
        "composition.py",
        "the skills directory is never made readable, so the catalog's paths cannot be opened",
        "                skills.directory if skills is not None else None,",
        "                None,",
    ),
    (
        "__main__.py",
        "the catalog is assembled but never reaches the model",
        "        if skills_watcher is not None:\n"
        "            notes.append(skills_watcher.refresh())",
        "        if False:\n            notes.append(skills_watcher.refresh())",
    ),
    (
        "__main__.py",
        "the model is shown a catalog with no instructions for what to do with it",
        "    if skills is not None:\n        skill_tool =",
        "    if False:\n        skill_tool =",
    ),
    (
        "__main__.py",
        "the instructions ignore which access path this run actually got",
        '        skill_tool = tools is not None and "read_skill" in tools.handlers',
        "        skill_tool = True",
    ),
]

# One survivor that stayed a survivor. It is an *equivalent mutant*: letting a
# file with no frontmatter through `_parse_frontmatter` hands `_load_one` an
# empty field dict, and the very next check ("no name, or no description") drops
# it anyway. Both versions of the code skip the file. There is no test that can
# tell them apart, because there is no behaviour that differs -- so it is listed
# here as expected rather than deleted, which would hide the fact that the
# frontmatter check and the required-fields check overlap.
EQUIVALENT = {"a file with no frontmatter at all is parsed as a skill"}

SRC = Path("src/minicodex")
ORIGINALS = {name: (SRC / name).read_text(encoding="utf-8") for name, *_ in MUTATIONS}
SUITES = [
    "tests/test_faults_ch18.py",
    "tests/test_faults_ch17.py",
    "tests/test_faults_ch07.py",
    "tests/test_schemas.py",
]


def restore() -> None:
    for name, text in ORIGINALS.items():
        path = SRC / name
        if path.read_text(encoding="utf-8") != text:
            path.write_text(text, encoding="utf-8")


atexit.register(restore)
signal.signal(signal.SIGINT, lambda *_: sys.exit(130))


def main() -> None:
    print(f"{len(MUTATIONS)} mutations, {' '.join(SUITES)}\n")
    survivors = []
    for name, label, before, after in MUTATIONS:
        path = SRC / name
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
            )
        except subprocess.TimeoutExpired:
            print(f"  !! the suite did not finish: {label}")
            survivors.append(label)
            continue
        finally:
            restore()
        failed = len(re.findall(r"^FAILED ", result.stdout, re.M))
        errors = len(re.findall(r"^ERROR ", result.stdout, re.M))
        caught = failed + errors
        mark = "   (equivalent mutant, expected)" if label in EQUIVALENT else ""
        print(f"  {caught:>3} test(s) fail  <-  {label}{mark}")
        if not caught and label not in EQUIVALENT:
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
