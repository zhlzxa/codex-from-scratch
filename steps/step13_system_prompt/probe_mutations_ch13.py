"""Do chapter 13's tests fail when chapter 13's code is wrong?

Same script as chapters 9 through 12, pointed at `agents_md.py`, the
`DeveloperNote` sites in `history.py`, `rollout.py` and `compaction.py`, the
`on_turn_start` wiring in `agent.py` and `subagent.py`, and the lines of
`__main__.py` that connect them.  Each entry is an edit that should break
something; the script applies it, runs the suite, restores the file, and
reports how many tests noticed.

Restores from `atexit` and a signal handler, and refuses to start on a tree
that already carries one of its own mutations.  This chapter is why the second
half exists here: the first run of this script was moved to the background
mid-mutation, and left `history.add_system_note(note)` in `agent.py` -- the
one edit that silently undoes the chapter's own measured choice of role.

    uv run python probe_mutations_ch13.py
"""

from __future__ import annotations

import atexit
import re
import signal
import subprocess
import sys
from pathlib import Path

MUTATIONS = [
    # -- finding and reading the files ---------------------------------------
    (
        "agents_md.py",
        "a cwd that escaped the sandbox falls back to the root's own docs",
        "    except ValueError:\n        return []",
        "    except ValueError:\n        return [root]",
    ),
    (
        "agents_md.py",
        "the project-root search walks past the sandbox root",
        "        if current == sandbox_root:\n            return sandbox_root",
        "        if False:\n            return sandbox_root",
    ),
    (
        "agents_md.py",
        "the byte ceiling is checked but never enforced",
        "        remaining = max_bytes - total\n"
        "        if remaining <= 0:\n"
        "            truncated = True\n"
        "            break",
        "        remaining = max_bytes - total\n"
        "        if False:\n"
        "            truncated = True\n"
        "            break",
    ),
    (
        "agents_md.py",
        "an oversized file is read whole",
        "        if len(encoded) > remaining:",
        "        if False:",
    ),
    # -- saying what changed --------------------------------------------------
    (
        "agents_md.py",
        "change detection ignores edited content when the file list is unchanged",
        "        if current.sources == self._last.sources and current.text == self._last.text:",
        "        if current.sources == self._last.sources:",
    ),
    (
        "agents_md.py",
        "a removal notice is sent even when nothing changed",
        "        if not current:\n            # `had_content`",
        "        if True:\n            # `had_content`",
    ),
    (
        "agents_md.py",
        "the first-ever injection is wrapped in a REPLACEMENT notice for nothing",
        "        if not had_content:\n            return block",
        "        if False:\n            return block",
    ),
    # -- a resumed session ----------------------------------------------------
    (
        "agents_md.py",
        "a resumed session is told the same conventions a second time",
        "        if block in inherited:\n            return None",
        "        if False:\n            return None",
    ),
    (
        "agents_md.py",
        "a removal already on record is announced again",
        "            return None if was_removed else REMOVAL_NOTICE",
        "            return REMOVAL_NOTICE",
    ),
    (
        "agents_md.py",
        "conventions that return after a removal are framed as a replacement",
        "        if was_removed:\n            return block",
        "        if False:\n            return block",
    ),
    (
        "agents_md.py",
        "every check after a resume is treated as the first one",
        "            inherited, self._inherited = self._inherited, None",
        "            inherited = self._inherited",
    ),
    (
        "agents_md.py",
        "a resumed watcher forgets what its first check found",
        "            self._last = current\n            return self._first_check_after_resume",
        "            return self._first_check_after_resume",
    ),
    (
        "agents_md.py",
        "any note on record counts as a conventions note",
        "            if _BLOCK_HEADING in text or text.startswith(REMOVAL_NOTICE):",
        "            if True:",
    ),
    (
        "history.py",
        "the history reports no AGENTS.md notes to a resuming process",
        "        return tuple(item.text for item in self._items "
        "if isinstance(item, DeveloperNote))",
        "        return ()",
    ),
    (
        "__main__.py",
        "the command line does not tell the watcher what a resumed session saw",
        "root, shown=resume_from.developer_notes() if resume_from is not None else ()",
        "root, shown=()",
    ),
    # -- the wire role, and the session file ----------------------------------
    (
        "history.py",
        "a developer note renders with the same wire role as a system note",
        "    if isinstance(item, DeveloperNote):\n"
        '        return {"role": "developer", "content": item.text}',
        "    if isinstance(item, DeveloperNote):\n"
        '        return {"role": "system", "content": item.text}',
    ),
    (
        "rollout.py",
        "a developer note crashes the writer instead of round-tripping through a resume",
        "    if isinstance(item, DeveloperNote):\n"
        '        return {"type": "developer_note", "text": item.text}',
        "    if False:\n        pass",
    ),
    # -- the loop -------------------------------------------------------------
    (
        "agent.py",
        "on_turn_start is never called, so AGENTS.md updates never reach the model",
        "            if self.on_turn_start is not None:\n"
        "                note = self.on_turn_start()",
        "            if False:\n                note = self.on_turn_start()",
    ),
    (
        "agent.py",
        "the AGENTS.md note is appended as a system note, undoing the choice of role",
        "                if note is not None:\n"
        "                    history.add_developer_note(note)",
        "                if note is not None:\n                    history.add_system_note(note)",
    ),
    (
        "agent.py",
        "Wiring.agent drops the hook it was handed",
        "            on_turn_start=on_turn_start,",
        "            on_turn_start=None,",
    ),
    # -- compaction -----------------------------------------------------------
    (
        "compaction.py",
        "compaction cannot rebuild a history that has an AGENTS.md note in it",
        "        elif isinstance(item, DeveloperNote):\n"
        "            history.add_developer_note(item.text)\n",
        "",
    ),
    (
        "compaction.py",
        "AGENTS.md is summarised away with everything else the cut removes",
        "    _replay(rebuilt, carried_notes(dropped))\n",
        "",
    ),
    (
        "compaction.py",
        "carried notes come back in reverse order",
        "    return [item for item in dropped if isinstance(item, DeveloperNote)]",
        "    return [item for item in reversed(dropped) if isinstance(item, DeveloperNote)]",
    ),
    (
        "compaction.py",
        "the plan does not count the notes it is about to carry",
        "        return head + carried_notes(items[protected_count:cut])",
        "        return head",
    ),
    (
        "compaction.py",
        "the carried notes are placed after the summary",
        "    _replay(rebuilt, carried_notes(dropped))\n    rebuilt.add_system_note(note)\n",
        "    rebuilt.add_system_note(note)\n    _replay(rebuilt, carried_notes(dropped))\n",
    ),
    # -- sub-agents -----------------------------------------------------------
    (
        "subagent.py",
        "a sub-agent is never shown AGENTS.md",
        "        on_turn_start=ctx.on_turn_start_for(shell) if ctx.on_turn_start_for else None,",
        "        on_turn_start=None,",
    ),
    (
        "subagent.py",
        "a sub-agent's watcher follows its parent's shell",
        "        on_turn_start=ctx.on_turn_start_for(shell) if ctx.on_turn_start_for else None,",
        "        on_turn_start=ctx.on_turn_start_for(ctx.parent_shell)\n"
        "        if ctx.on_turn_start_for\n"
        "        else None,",
    ),
    (
        "subagent.py",
        "a sub-agent's tools and its watcher are given different shells",
        "    tools = child_tools(ctx, shell)\n",
        "    tools = child_tools(ctx)\n",
    ),
    (
        "__main__.py",
        "the command line gives sub-agents no watcher",
        "        on_turn_start_for=lambda shell: watch(root, shell),\n",
        "",
    ),
    # -- the command line -----------------------------------------------------
    (
        "__main__.py",
        "the watcher is asked about the directory the process started in",
        "agents_watcher.refresh(Path(context.shell.cwd))",
        "agents_watcher.refresh(root)",
    ),
    (
        "__main__.py",
        "the permission state is put in front of the fixed prompt",
        '    parts.append(block)\n    return "\\n\\n".join(parts)',
        '    parts.insert(0, block)\n    return "\\n\\n".join(parts)',
    ),
]

SRC = Path("src/minicodex")
ORIGINALS = {name: (SRC / name).read_text(encoding="utf-8") for name, *_ in MUTATIONS}
SUITES = [
    "tests/test_faults_ch13.py",
    "tests/test_agent.py",
    "tests/test_history.py",
    "tests/test_compaction.py",
]


def restore() -> None:
    for name, text in ORIGINALS.items():
        path = SRC / name
        if path.read_text(encoding="utf-8") != text:
            path.write_text(text, encoding="utf-8")


atexit.register(restore)
signal.signal(signal.SIGINT, lambda *_: sys.exit(130))


def refuse_if_already_mutated() -> None:
    """Do not start on a tree a killed run left dirty (chapter 9's lesson)."""
    dirty = [
        f"{name}: looks like {label!r} is still applied"
        for name, label, before, after in MUTATIONS
        if after and before not in ORIGINALS[name] and after in ORIGINALS[name]
    ]
    if dirty:
        print("refusing to run: the working tree is already mutated\n")
        for line in dirty:
            print(f"  {line}")
        print("\nRestore it (git checkout / re-copy) before running this again.")
        raise SystemExit(2)


def main() -> None:
    refuse_if_already_mutated()
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
            )
        except subprocess.TimeoutExpired:
            # Not "caught".  A run that did not finish measured nothing.
            print(f"  !! the suite did not finish: {label}")
            survivors.append(label)
            continue
        finally:
            restore()
        failed = len(re.findall(r"^FAILED ", result.stdout, re.M))
        errors = len(re.findall(r"^ERROR ", result.stdout, re.M))
        caught = failed + errors
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
