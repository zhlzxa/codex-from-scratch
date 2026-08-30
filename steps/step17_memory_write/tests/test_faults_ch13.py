"""Chapter 13: the system prompt, and AGENTS.md as a second, human-authored one.

Nothing here touches the network -- the agent-loop tests use a scripted model.
The provider measurements this chapter's write-up quotes (F13-01, F13-02
through F13-05, F13-07, F13-12) came from real Ollama and OpenAI calls and
live in `probe_system_prompt.py`, not here; a unit test cannot assert what a
real model does, only what this program does with what it is given.

Test names carry the fault IDs from FAULTS.md.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest

from minicodex import compaction_prompt, permissions_prompt, system_prompt
from minicodex.agent import Agent
from minicodex.agents_md import (
    MAX_BYTES,
    REMOVAL_NOTICE,
    REPLACEMENT_NOTICE,
    AgentsMdWatcher,
    find_project_root,
    load_project_docs,
)
from minicodex.history import DeveloperNote, History
from minicodex.model import Completed, TextDelta, ToolCallDelta

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


class ScriptedModel:
    """Replays a fixed list of turns and remembers what it was sent."""

    def __init__(self, turns: Sequence[Any]) -> None:
        self.turns = list(turns)
        self.sent: list[list[dict[str, Any]]] = []

    async def stream(self, messages: Sequence[dict[str, Any]]) -> Any:
        self.sent.append([dict(m) for m in messages])
        turn = self.turns[min(len(self.sent) - 1, len(self.turns) - 1)]
        if isinstance(turn, str):
            yield TextDelta(turn)
        else:
            for index, (call_id, name, arguments) in enumerate(turn):
                yield ToolCallDelta(
                    call_id=call_id, index=index, name=name, arguments=json.dumps(arguments)
                )
        yield Completed("tool_calls" if not isinstance(turn, str) else "stop")


async def _noop_tool(_args: dict[str, Any]) -> str:
    return "ok"


def _repo(tmp_path: Path) -> Path:
    (tmp_path / ".git").mkdir()
    return tmp_path


# ---------------------------------------------------------------------------
# F13-11: project-root discovery, bounded at the sandbox root
# ---------------------------------------------------------------------------


def test_F13_11_finds_the_marker_from_a_nested_subdirectory(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    nested = root / "a" / "b" / "c"
    nested.mkdir(parents=True)
    assert find_project_root(nested, root) == root


def test_F13_11_never_searches_above_the_sandbox_root(tmp_path: Path) -> None:
    """The marker sits two levels above the sandbox root. A search that walks
    from the filesystem root down (or fails to stop at the boundary) would
    find it; this one must not -- `paths.resolve()` draws exactly this line
    for `read_file` and `apply_patch` (F04-12, F05-03), and AGENTS.md gets no
    wider a window onto the filesystem than a file-editing tool does."""
    (tmp_path / ".git").mkdir()
    sandbox = tmp_path / "workspaces" / "this-one"
    sandbox.mkdir(parents=True)
    assert find_project_root(sandbox, sandbox) == sandbox


def test_F13_11_a_cwd_that_has_escaped_the_sandbox_gets_no_docs(tmp_path: Path) -> None:
    """`cd` is not containment-checked (chapter 2's `ShellSession._handle_cd`
    tracks state, not permission), so the shell's cwd can end up outside the
    sandboxed root the ordinary way: an agent just changed directory out of
    it. The first version of `load_project_docs` treated that cwd as if it
    were still standing at the root, and silently handed back the root's own
    AGENTS.md as though it described somewhere else entirely -- caught by
    `probe_system_prompt.py agentsmd`, not by this test, which now pins it."""
    root = _repo(tmp_path)
    (root / "AGENTS.md").write_text("Use uv, not pip.\n", encoding="utf-8")
    outside = tmp_path.parent / "definitely-not-the-repo"
    outside.mkdir(exist_ok=True)

    docs = load_project_docs(root, outside)

    assert not docs
    assert docs.text == ""
    assert docs.sources == ()


def test_F13_11_subdirectory_conventions_are_not_lost(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    (root / "AGENTS.md").write_text("Use uv, not pip.\n", encoding="utf-8")
    backend = root / "backend"
    backend.mkdir()
    (backend / "AGENTS.md").write_text("All DB access goes through repo.py.\n", encoding="utf-8")

    docs = load_project_docs(root, backend)

    assert docs.sources == ("AGENTS.md", "backend/AGENTS.md")
    assert "Use uv, not pip." in docs.text
    assert "All DB access goes through repo.py." in docs.text
    # Root first, subdirectory second -- the more specific file reads as the
    # later, more specific word on the subject rather than something the
    # root file overrides.
    assert docs.text.index("Use uv") < docs.text.index("DB access")


def test_F13_11_no_agents_md_anywhere_is_not_an_error(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    docs = load_project_docs(root, root)
    assert not docs
    assert docs.sources == ()


# ---------------------------------------------------------------------------
# F13-10: a single combined byte ceiling, with truncation, not a crash
# ---------------------------------------------------------------------------


def test_F13_10_oversized_agents_md_is_truncated_not_swallowed_whole(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    (root / "AGENTS.md").write_text("x" * (MAX_BYTES * 3), encoding="utf-8")

    docs = load_project_docs(root, root)

    assert docs.truncated is True
    assert len(docs.text.encode("utf-8")) <= MAX_BYTES + 200  # + the truncation note itself
    assert "truncated" in docs.text


def test_F13_10_the_ceiling_is_shared_across_the_whole_chain_not_per_file(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    (root / "AGENTS.md").write_text("a" * (MAX_BYTES - 100), encoding="utf-8")
    sub = root / "sub"
    sub.mkdir()
    (sub / "AGENTS.md").write_text("b" * 10_000, encoding="utf-8")

    docs = load_project_docs(root, sub, max_bytes=MAX_BYTES)

    assert docs.truncated is True
    # The subdirectory file is present at all (truncated), not dropped --
    # sources still names it, even though most of its bytes did not fit.
    assert "sub/AGENTS.md" in docs.sources


def test_F13_10_a_file_reached_after_the_ceiling_is_already_full_is_skipped_entirely(
    tmp_path: Path,
) -> None:
    """Different code path from the test above: there the ceiling is crossed
    *while reading* a file (it gets a partial entry); here the ceiling is
    already exactly full *before* the next file is even opened, and that file
    must not appear in `sources` at all. Mutation testing caught this gap --
    `if remaining <= 0` mutated to `if False` left every other F13-10 test
    green, because none of them made `remaining` reach zero exactly between
    two files."""
    root = _repo(tmp_path)
    (root / "AGENTS.md").write_text("a" * MAX_BYTES, encoding="utf-8")
    sub = root / "sub"
    sub.mkdir()
    (sub / "AGENTS.md").write_text("more conventions\n", encoding="utf-8")

    docs = load_project_docs(root, sub, max_bytes=MAX_BYTES)

    assert docs.truncated is True
    assert docs.sources == ("AGENTS.md",)  # sub/AGENTS.md never even opened


def test_F13_10_a_small_file_is_not_truncated(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    (root / "AGENTS.md").write_text("Use uv, not pip.\n", encoding="utf-8")
    docs = load_project_docs(root, root)
    assert docs.truncated is False
    assert "truncated" not in docs.text


# ---------------------------------------------------------------------------
# F13-09: append-only history, so a stale AGENTS.md is superseded, not erased
# ---------------------------------------------------------------------------


def test_F13_09_first_check_with_no_docs_yields_nothing(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    watcher = AgentsMdWatcher(root)
    assert watcher.refresh(root) is None


def test_F13_09_first_check_with_docs_injects_once(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    (root / "AGENTS.md").write_text("Use uv, not pip.\n", encoding="utf-8")
    watcher = AgentsMdWatcher(root)

    first = watcher.refresh(root)
    assert first is not None
    assert "Use uv, not pip." in first
    assert REPLACEMENT_NOTICE not in first  # nothing to replace yet

    second = watcher.refresh(root)
    assert second is None  # unchanged cwd, unchanged file: no repeat


def test_F13_09_a_cd_to_a_different_convention_set_is_a_replacement_not_a_silent_swap(
    tmp_path: Path,
) -> None:
    root = _repo(tmp_path)
    (root / "AGENTS.md").write_text("Use uv, not pip.\n", encoding="utf-8")
    backend = root / "backend"
    backend.mkdir()
    (backend / "AGENTS.md").write_text("All DB access goes through repo.py.\n", encoding="utf-8")
    watcher = AgentsMdWatcher(root)

    watcher.refresh(root)
    moved = watcher.refresh(backend)

    assert moved is not None
    assert moved.startswith(REPLACEMENT_NOTICE)
    assert "repo.py" in moved


def test_F13_09_a_subdirectory_with_no_own_file_still_inherits_the_root_notice_is_noop(
    tmp_path: Path,
) -> None:
    """Not a removal: a subdirectory with no `AGENTS.md` of its own still
    inherits the root's, because `_chain` always starts at the root. Nothing
    changed, so nothing should be sent -- this is the case the test below
    used to get wrong, by using this directory instead of one outside the
    sandbox entirely."""
    root = _repo(tmp_path)
    (root / "AGENTS.md").write_text("Use uv, not pip.\n", encoding="utf-8")
    empty = root / "no_own_file_here"
    empty.mkdir()
    watcher = AgentsMdWatcher(root)

    watcher.refresh(root)
    still = watcher.refresh(empty)

    assert still is None


def test_F13_09_cding_out_of_the_sandbox_entirely_sends_a_removal_notice(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    (root / "AGENTS.md").write_text("Use uv, not pip.\n", encoding="utf-8")
    outside = tmp_path.parent / "outside-the-sandbox"
    outside.mkdir(exist_ok=True)
    watcher = AgentsMdWatcher(root)

    watcher.refresh(root)
    left = watcher.refresh(outside)

    assert left == REMOVAL_NOTICE


def test_F13_09_content_reappearing_after_a_removal_is_not_framed_as_a_replacement(
    tmp_path: Path,
) -> None:
    """The removal notice already said "there is nothing". Content that shows
    up on the next check should read as fresh, not as "the thing I just told
    you did not exist no longer applies"."""
    root = _repo(tmp_path)
    (root / "AGENTS.md").write_text("Use uv, not pip.\n", encoding="utf-8")
    empty = tmp_path.parent / "outside-again"
    empty.mkdir(exist_ok=True)
    watcher = AgentsMdWatcher(root)

    watcher.refresh(root)
    removed = watcher.refresh(empty)
    assert removed == REMOVAL_NOTICE

    back = watcher.refresh(root)
    assert back is not None
    assert not back.startswith(REPLACEMENT_NOTICE)
    assert "Use uv, not pip." in back


def test_F13_09_editing_the_file_in_place_is_detected_even_with_the_same_path(
    tmp_path: Path,
) -> None:
    """Same source list, different bytes -- the change-detection in `refresh`
    has to compare content, not just which files were found. Comparing only
    `sources` is the mutation that would make this pass silently: found by
    running `probe_mutations_ch13.py` against a first draft of this test file,
    which had no case where the file list stays the same but the text does
    not."""
    root = _repo(tmp_path)
    (root / "AGENTS.md").write_text("Use uv, not pip.\n", encoding="utf-8")
    watcher = AgentsMdWatcher(root)

    watcher.refresh(root)
    (root / "AGENTS.md").write_text("Use uv, not pip. Also: no bare except.\n", encoding="utf-8")
    edited = watcher.refresh(root)

    assert edited is not None
    assert "no bare except" in edited


def test_F13_09_the_old_note_is_never_deleted_from_history_only_superseded(tmp_path: Path) -> None:
    """History is append-only (chapter 7): the fix cannot be "edit the earlier
    message", only "say plainly that it no longer applies". Both messages must
    still be on record afterwards."""
    root = _repo(tmp_path)
    (root / "AGENTS.md").write_text("Use uv, not pip.\n", encoding="utf-8")
    backend = root / "backend"
    backend.mkdir()
    (backend / "AGENTS.md").write_text("All DB access goes through repo.py.\n", encoding="utf-8")
    watcher = AgentsMdWatcher(root)

    history = History()
    first = watcher.refresh(root)
    history.add_developer_note(first)
    second = watcher.refresh(backend)
    history.add_developer_note(second)

    notes = [item.text for item in history.items if isinstance(item, DeveloperNote)]
    assert len(notes) == 2
    assert "Use uv, not pip." in notes[0]
    assert notes[1].startswith(REPLACEMENT_NOTICE)


# ---------------------------------------------------------------------------
# F13-12: rendered as its own wire role, distinct from both SystemNote and
# UserMessage -- see `history.DeveloperNote`'s docstring for the measurement
# that picked "developer" over "user".
# ---------------------------------------------------------------------------


def test_F13_12_developer_note_renders_as_role_developer() -> None:
    history = History()
    history.add_developer_note("Use uv, not pip.")
    wire = history.to_wire()
    assert wire == [{"role": "developer", "content": "Use uv, not pip."}]


def test_F13_12_developer_note_is_not_a_user_message() -> None:
    """A person did not type this in chat. Conflating the two would make a
    recorded session unreadable -- there would be no way to tell, later,
    which lines came from the user and which came from a file on disk."""
    history = History()
    history.add_developer_note("Use uv, not pip.")
    assert not any(hasattr(item, "text") and item.text == "" for item in history.items)
    item = history.items[0]
    assert isinstance(item, DeveloperNote)
    assert type(item).__name__ != "UserMessage"


# ---------------------------------------------------------------------------
# F13-07 (structural half): a fresh AGENTS.md check does not touch the
# existing system message -- it always arrives as a new item, appended after
# whatever was already fixed.  (The cache-hit numbers this claim is *for* are
# a real-provider measurement: `probe_system_prompt.py cache`.)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_F13_07_on_turn_start_appends_a_new_item_never_edits_the_system_note() -> None:
    calls = {"n": 0}

    def on_turn_start() -> str | None:
        calls["n"] += 1
        return f"AGENTS.md check #{calls['n']}" if calls["n"] == 1 else None

    model = ScriptedModel(["all done"])
    agent = Agent(model, {"noop": _noop_tool}, instructions="SYSTEM", on_turn_start=on_turn_start)
    result = await agent.run("hello")

    kinds = [type(item).__name__ for item in result.history.items]
    assert kinds == ["SystemNote", "UserMessage", "DeveloperNote", "AssistantMessage"]
    system_texts = [
        item.text for item in result.history.items if type(item).__name__ == "SystemNote"
    ]
    assert system_texts == ["SYSTEM"]  # untouched


@pytest.mark.asyncio
async def test_F13_07_on_turn_start_returning_none_adds_nothing() -> None:
    model = ScriptedModel(["done"])
    agent = Agent(model, {"noop": _noop_tool}, on_turn_start=lambda: None)
    result = await agent.run("hello")
    assert not any(type(item).__name__ == "DeveloperNote" for item in result.history.items)


@pytest.mark.asyncio
async def test_F13_07_absent_by_default_is_unchanged_from_every_earlier_chapter() -> None:
    model = ScriptedModel(["done"])
    agent = Agent(model, {"noop": _noop_tool})
    result = await agent.run("hello")
    assert not any(type(item).__name__ == "DeveloperNote" for item in result.history.items)


# ---------------------------------------------------------------------------
# on_turn_start absence for sub-agents (the shape of F10's missing-MCP-tools
# decision, applied here): nothing in this chapter builds one for a child,
# and this pins that as intentional rather than an oversight to "discover"
# and silently patch later.
# ---------------------------------------------------------------------------


def test_F13_child_agents_get_no_watcher_unless_one_is_explicitly_passed() -> None:
    from minicodex.agent import Wiring
    from minicodex.agent_types import ToolSet

    wiring = Wiring()
    child = wiring.agent(ScriptedModel(["x"]), ToolSet(handlers={}, schemas=[]))
    assert child.on_turn_start is None


def test_F13_wiring_agent_passes_on_turn_start_through_when_given() -> None:
    """The other half of the test above: `Wiring.agent()` must not just
    default this to `None`, it must actually forward one it was handed --
    otherwise the top-level run in `__main__.py` would silently lose its
    AGENTS.md watcher the same way interlude B's drift lost half of a
    sub-agent's wiring for a whole chapter."""
    from minicodex.agent import Wiring
    from minicodex.agent_types import ToolSet

    def hook() -> str | None:
        return "hi"

    wiring = Wiring()
    parent = wiring.agent(
        ScriptedModel(["x"]), ToolSet(handlers={}, schemas=[]), on_turn_start=hook
    )
    assert parent.on_turn_start is hook


# ---------------------------------------------------------------------------
# F13-08: the prompt is prose someone will edit; pin it so an edit is a
# decision, not an accident that silently changes what every future run says.
# ---------------------------------------------------------------------------


SYSTEM_PROMPT_SNAPSHOT = (
    "You are a coding agent working in a user's repository.\n"
    "\n"
    "If a request is genuinely ambiguous -- more than one reasonable "
    "interpretation, and picking wrong would waste real work -- ask one "
    "specific question before acting instead of guessing. Do not ask about "
    "anything you could find out yourself by reading the repository.\n"
    "\n"
    "You may be shown project-specific conventions as a separate message, "
    "written by a person rather than by you. Follow them.\n"
)


def test_F13_08_system_prompt_is_snapshotted() -> None:
    """An exact match, not a substring check -- chapter 3's lesson (F03-10):
    a description edit that flips a model's behaviour 3/3 -> 0/3 should never
    land silently. Only one sentence was added this chapter beyond chapter -1's
    placeholder, and it earned its place by measurement, not by assumption --
    see `probe_system_prompt.py ask`, and the three candidate sentences that
    did *not* make it in, recorded in FAULTS.md as NOT REPRODUCED."""
    assert system_prompt() == SYSTEM_PROMPT_SNAPSHOT


def test_F13_08_permissions_and_compaction_prompts_still_load() -> None:
    # Regression only: chapter 13 did not touch these files, and a snapshot
    # suite that does not even try to load them would not notice if it had.
    assert permissions_prompt()
    assert compaction_prompt()
