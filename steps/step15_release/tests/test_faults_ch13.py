"""Chapter 13: the system prompt, and AGENTS.md as a second, human-authored one.

Nothing here touches the network -- the agent-loop tests use a scripted model.
The provider measurements this chapter's write-up quotes (F13-01, F13-02
through F13-05, F13-07, F13-12) came from real Ollama and OpenAI calls and
live in `probe_system_prompt.py`, not here; a unit test cannot assert what a
real model does, only what this program does with what it is given.

Test names carry the fault IDs from FAULTS.md.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest

from minicodex import compaction_prompt, permissions_prompt, system_prompt
from minicodex.agent import Agent, Wiring
from minicodex.agents_md import (
    MAX_BYTES,
    REMOVAL_NOTICE,
    REPLACEMENT_NOTICE,
    AgentsMdWatcher,
    find_project_root,
    load_project_docs,
    watch,
)
from minicodex.approval import AllowAll, Session
from minicodex.compaction import SUMMARY_MARKER, Sizer, SummaryRequest, compact
from minicodex.composition import sub_context
from minicodex.history import DeveloperNote, History
from minicodex.model import Completed, TextDelta, ToolCallDelta
from minicodex.plan import PLAN_INSTRUCTIONS
from minicodex.rollout import RolloutWriter, SessionMeta, read_rollout
from minicodex.shell import ShellSession
from minicodex.subagent import SubAgentContext, TaskSpec, run_task

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


def _scripted_cli(
    monkeypatch: pytest.MonkeyPatch, turns: Sequence[Any]
) -> list[list[dict[str, Any]]]:
    """Make every model client the CLI builds replay `turns` instead of
    calling a server -- the parent's, a sub-agent's and the summariser's are
    all the same class, so they share the list. Everything else in `main()`
    runs for real. Returns the requests, in the order they were made."""
    from minicodex.model import ChatCompletionsModel

    requests: list[list[dict[str, Any]]] = []

    async def stream(self: Any, messages: Sequence[dict[str, Any]]) -> Any:
        requests.append([dict(m) for m in messages])
        turn = turns[min(len(requests) - 1, len(turns) - 1)]
        if isinstance(turn, str):
            yield TextDelta(turn)
        else:
            for index, (call_id, name, arguments) in enumerate(turn):
                yield ToolCallDelta(
                    call_id=call_id, index=index, name=name, arguments=json.dumps(arguments)
                )
        yield Completed("stop")

    monkeypatch.setattr(ChatCompletionsModel, "stream", stream)
    return requests


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
    # Each file under its own heading, so the model can tell which file said what.
    assert "# AGENTS.md\n" in docs.text and "# backend/AGENTS.md\n" in docs.text
    # Root first, subdirectory second -- the more specific file reads as the
    # later, more specific word on the subject rather than something the
    # root file overrides.
    assert docs.text.index("Use uv") < docs.text.index("DB access")


def test_F13_11_a_nested_repository_is_its_own_project(tmp_path: Path) -> None:
    """The `.git` marker is what `find_project_root` is *for*, and until this
    test nothing exercised it: every other test puts `.git` at the sandbox
    root, where the search ends anyway. Deleting the marker check left the
    whole suite green.

    A repository checked out inside another one (a vendored library, a git
    submodule) is a different project with different conventions. Standing in
    it, the outer project's AGENTS.md does not apply."""
    root = _repo(tmp_path)
    (root / "AGENTS.md").write_text("OUTER RULE\n", encoding="utf-8")
    inner = root / "vendor" / "lib"
    (inner / ".git").mkdir(parents=True)
    (inner / "AGENTS.md").write_text("INNER RULE\n", encoding="utf-8")
    deeper = inner / "src"
    deeper.mkdir()

    assert find_project_root(deeper, root) == inner
    docs = load_project_docs(root, deeper)
    assert docs.sources == ("vendor/lib/AGENTS.md",)
    assert "OUTER RULE" not in docs.text


def test_F13_11_something_named_agents_md_that_cannot_be_read_as_text(tmp_path: Path) -> None:
    """This runs at the top of every turn, so anything it raises ends the run.
    A directory that happens to be called AGENTS.md is skipped; a file that is
    not valid UTF-8 is read with the bad bytes replaced."""
    root = _repo(tmp_path)
    (root / "AGENTS.md").mkdir()
    assert not load_project_docs(root, root)

    sub = root / "sub"
    sub.mkdir()
    (sub / "AGENTS.md").write_bytes(b"Use uv, not pip. \xff\xfe\n")
    docs = load_project_docs(root, sub)
    assert docs.sources == ("sub/AGENTS.md",)
    assert "Use uv, not pip." in docs.text


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


def test_F13_10_nothing_is_read_after_a_file_that_was_cut(tmp_path: Path) -> None:
    """The ceiling is in bytes and a cut can land inside a character. Four
    three-byte characters against a ten-byte ceiling keep three of them: nine
    bytes, one to spare. Without the `break` after a truncated file, the next
    file is opened to fill that one byte and contributes a heading and a
    single letter."""
    root = _repo(tmp_path)
    (root / "AGENTS.md").write_text("好好好好", encoding="utf-8")
    sub = root / "sub"
    sub.mkdir()
    (sub / "AGENTS.md").write_text("abc", encoding="utf-8")

    docs = load_project_docs(root, sub, max_bytes=10)

    assert docs.truncated is True
    assert docs.sources == ("AGENTS.md",)
    assert "好好好" in docs.text and "好好好好" not in docs.text


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
    # The block names its source files and says who wrote it -- the sentence
    # that tells the model this outranks its own habits.
    assert first.startswith("# Project conventions (AGENTS.md)")
    assert "A person wrote this" in first

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


def test_F13_12_developer_note_survives_the_session_file_as_what_it_is(tmp_path: Path) -> None:
    """A person did not type this in chat, and a recorded session has to be
    able to say so: which lines the user typed and which came from a file on
    disk. Written to a session file and read back, the two are still two
    different things, in the order they happened.

    (The test that stood here before built a `DeveloperNote` and asserted it
    was a `DeveloperNote`. It could not fail.)"""
    path = tmp_path / "s.jsonl"
    with RolloutWriter(path, SessionMeta(session_id="s1")) as writer:
        history = History(observer=writer.append)
        history.add_user("add a dependency")
        history.add_developer_note("Use uv, not pip.")

    loaded, dropped = read_rollout(path).history()

    assert dropped == 0
    assert [type(item).__name__ for item in loaded.items] == ["UserMessage", "DeveloperNote"]
    assert loaded.developer_notes() == ("Use uv, not pip.",)


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
async def test_F13_07_the_note_is_added_before_the_request_is_sized() -> None:
    """The note is part of the request, so it has to be in the history when
    the loop asks "does this fit". A note of about a thousand tokens against a
    1,200-token window is over the compaction threshold by itself: the size
    check has to notice. With the two steps the other way round the check sees
    only "hello", and the note goes out unmeasured."""

    async def summarise(_request: SummaryRequest) -> str:
        return "## Done"

    agent = Agent(
        ScriptedModel(["done"]),
        {"noop": _noop_tool},
        context_window=1200,
        summariser=summarise,
        on_turn_start=lambda: "rule " * 800,
    )
    result = await agent.run("hello")

    assert len(result.compactions) == 1


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
# `Wiring.agent` neither invents an `on_turn_start` nor drops one. The first
# version of this section pinned "a child gets no watcher" as intentional;
# the sub-agent section below is what that decision cost, and where a child's watcher now
# comes from. These two still hold: the hook is per-agent, never shared
# through `Wiring`.
# ---------------------------------------------------------------------------


def test_F13_wiring_agent_invents_no_watcher_of_its_own() -> None:
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


PROMPT_FINGERPRINTS = {
    "permissions.md": "d11aa76a4f0937b316be714f651289bc36671baaf6e0aa5031ff72c88bbfb2b1",
    "compaction.md": "803568f1b0d20ff328b10c66c66c3f14b8c95d17ce56b100fab15b38f9f340d4",
}


def test_F13_08_the_other_two_prompts_are_pinned_too() -> None:
    """`compaction_prompt()`'s docstring has said since chapter 6 that chapter
    13 "puts every one of these under a snapshot test". It put one of the
    three under one; this test only checked that the other two were not empty.

    A fingerprint rather than the full text, because these two are long. The
    point is the same: a changed prompt is a red test, and making it green
    again is a decision someone took on purpose."""
    actual = {
        "permissions.md": hashlib.sha256(permissions_prompt().encode("utf-8")).hexdigest(),
        "compaction.md": hashlib.sha256(compaction_prompt().encode("utf-8")).hexdigest(),
    }
    assert actual == PROMPT_FINGERPRINTS


# ---------------------------------------------------------------------------
# Not on the list: compaction met a kind of history item it had never been told about
# ---------------------------------------------------------------------------

CONVENTIONS = "# Project conventions (AGENTS.md)\n\nUse uv, not pip."


def _long_history(*notes: str) -> History:
    """Instructions, a task, the given AGENTS.md notes, then enough chatter
    that a small budget has to cut most of it."""
    history = History()
    history.add_system_note("instructions")
    history.add_user("the task")
    for note in notes:
        history.add_developer_note(note)
    for i in range(12):
        history.add_user(f"question {i} " + "x" * 400)
        history.add_assistant(f"answer {i} " + "y" * 400, [])
    return history


@pytest.mark.asyncio
async def test_compaction_carries_agents_md_across_the_cut() -> None:
    """Before the fix this raised `AssertionError: unreplayable item`: the
    first compaction of any run in a project that has an AGENTS.md."""
    seen: list[SummaryRequest] = []

    async def summarise(request: SummaryRequest) -> str:
        seen.append(request)
        return "## Done: twelve questions answered"

    history = _long_history(CONVENTIONS)
    result = await compact(history, summarise=summarise, budget=1200)

    assert result.plan.drops > 0, "the test must actually cut something"
    wire = result.history.to_wire()
    kept = [m for m in wire if m["role"] == "developer"]
    # Whole, once, and still under the role chapter 13 chose for it.
    assert [m["content"] for m in kept] == [CONVENTIONS]
    # In front of the summary: the summary describes work done under it.
    summary_at = next(i for i, m in enumerate(wire) if SUMMARY_MARKER in m["content"])
    assert wire.index(kept[0]) < summary_at
    # And it was not handed to the summariser to paraphrase.
    assert "Use uv" not in seen[0].transcript


@pytest.mark.asyncio
async def test_compaction_keeps_every_note_in_the_order_it_was_said() -> None:
    """All of them, not the newest: the second opens with "the conventions
    shown earlier no longer apply", which only reads right after the first."""

    async def summarise(_request: SummaryRequest) -> str:
        return "## Done"

    second = f"{REPLACEMENT_NOTICE}\n\n# Project conventions (AGENTS.md, pkg/AGENTS.md)\n\nTabs."
    result = await compact(_long_history(CONVENTIONS, second), summarise=summarise, budget=1200)

    assert result.plan.drops > 0
    assert list(result.history.developer_notes()) == [CONVENTIONS, second]


@pytest.mark.asyncio
async def test_compaction_plan_counts_what_it_carries() -> None:
    """A carried note takes room. If `plan()` sized the result without it, the
    plan would say "fits" about a history that does not."""

    async def summarise(_request: SummaryRequest) -> str:
        return "## Done"

    big = "# Project conventions (AGENTS.md)\n\n" + "rule " * 600  # ~750 tokens
    history = _long_history(big)
    budget = 1800
    result = await compact(history, summarise=summarise, budget=budget, summary_budget=200)

    assert result.plan.drops > 0
    assert big in result.history.developer_notes()
    actual = Sizer().messages(result.history.to_wire())
    assert actual <= result.plan.kept_tokens <= budget, (actual, result.plan.kept_tokens)


def test_the_cli_survives_a_compaction_in_a_project_with_agents_md(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The program, not the function. Five reads of a 60KB file against an
    8,000-token window force a real cut; with an AGENTS.md in the directory
    that used to end the run in a traceback."""
    import minicodex.__main__ as cli

    (tmp_path / "AGENTS.md").write_text("Use uv, not pip.\n", encoding="utf-8")
    (tmp_path / "big.txt").write_text("x" * 60_000, encoding="utf-8")
    requests = _scripted_cli(
        monkeypatch,
        [
            [("c1", "read_file", {"path": "big.txt"})],
            [("c2", "read_file", {"path": "big.txt"})],
            [("c3", "read_file", {"path": "big.txt"})],
            [("c4", "read_file", {"path": "big.txt"})],
            [("c5", "read_file", {"path": "big.txt"})],
            "done",
        ],
    )
    monkeypatch.chdir(tmp_path)
    code = cli.main(
        ["ask", "go", "--yes", "--context-window", "8000", "--session-dir", str(tmp_path / "s")]
    )

    assert code == 0
    assert "compacted" in capsys.readouterr().out
    last = requests[-1]
    assert any(SUMMARY_MARKER in m["content"] for m in last if m["role"] == "system")
    assert ["Use uv, not pip." in m["content"] for m in last if m["role"] == "developer"] == [True]


# ---------------------------------------------------------------------------
# Not on the list: a sub-agent was never shown AGENTS.md
# ---------------------------------------------------------------------------


def _child_context(root: Path, model: Any, shell: ShellSession) -> SubAgentContext:
    return sub_context(
        build_model=lambda _schemas: model,
        root=root,
        session=Session(mode="workspace-write", approver=AllowAll()),
        parent_shell=shell,
        wiring=Wiring(),
        on_turn_start_for=lambda child_shell: watch(root, child_shell),
    )


@pytest.mark.asyncio
async def test_a_sub_agent_is_shown_agents_md(tmp_path: Path) -> None:
    (tmp_path / "AGENTS.md").write_text("Use uv, not pip.\n", encoding="utf-8")
    shell = ShellSession()
    shell.cwd = str(tmp_path)
    model = ScriptedModel(["added it"])

    await run_task(
        TaskSpec(task="add the requests dependency"), _child_context(tmp_path, model, shell)
    )

    sent = model.sent[0]
    assert [m["role"] for m in sent] == ["system", "user", "developer"]
    assert "Use uv, not pip." in sent[-1]["content"]


@pytest.mark.asyncio
async def test_a_sub_agent_follows_its_own_cd_and_not_its_parents(tmp_path: Path) -> None:
    """The reason the first version gave a child no watcher was that the only
    one available was bound to the parent's shell. This is that worry, pinned:
    the child's `cd` moves the child's conventions and nothing else."""
    (tmp_path / "AGENTS.md").write_text("ROOT RULE\n", encoding="utf-8")
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "AGENTS.md").write_text("PKG RULE\n", encoding="utf-8")
    shell = ShellSession()
    shell.cwd = str(tmp_path)
    model = ScriptedModel([[("c1", "run_shell", {"command": "cd pkg"})], "done"])

    await run_task(TaskSpec(task="look in pkg"), _child_context(tmp_path, model, shell))

    first = [m["content"] for m in model.sent[0] if m["role"] == "developer"]
    assert len(first) == 1 and "ROOT RULE" in first[0] and "PKG RULE" not in first[0]
    second = [m["content"] for m in model.sent[1] if m["role"] == "developer"]
    assert len(second) == 2
    assert second[1].startswith(REPLACEMENT_NOTICE) and "PKG RULE" in second[1]
    # The parent has not moved.
    assert shell.cwd == str(tmp_path)


def test_the_cli_gives_a_child_its_watcher(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """One line in `__main__.py` (`on_turn_start_for=`). Without it every test
    above passes and the program behaves as it did before the fix."""
    import minicodex.__main__ as cli

    (tmp_path / "AGENTS.md").write_text("Use uv, not pip.\n", encoding="utf-8")
    requests = _scripted_cli(
        monkeypatch,
        [
            [("c1", "spawn_agent", {"task": "add the requests dependency"})],
            "added it",  # the child's only turn
            "the child added it",
        ],
    )
    monkeypatch.chdir(tmp_path)
    assert cli.main(["ask", "go", "--yes", "--session-dir", str(tmp_path / "s")]) == 0

    child = next(r for r in requests if r[1]["content"] == "add the requests dependency")
    assert ["Use uv, not pip." in m["content"] for m in child if m["role"] == "developer"] == [True]


# ---------------------------------------------------------------------------
# Not on the list: `--resume` starts a new watcher that knows nothing about the old one
# ---------------------------------------------------------------------------


def _two_level_repo(tmp_path: Path) -> tuple[str, str]:
    """A root AGENTS.md and one in pkg/, and the two notes a session that
    walked root -> pkg would have left in its history."""
    (tmp_path / "AGENTS.md").write_text("ROOT RULE\n", encoding="utf-8")
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "AGENTS.md").write_text("PKG RULE\n", encoding="utf-8")
    earlier = AgentsMdWatcher(tmp_path)
    at_root = earlier.refresh(tmp_path)
    in_pkg = earlier.refresh(tmp_path / "pkg")
    assert at_root is not None and in_pkg is not None
    return at_root, in_pkg


def test_resuming_in_the_same_place_does_not_say_it_twice(tmp_path: Path) -> None:
    at_root, _ = _two_level_repo(tmp_path)
    resumed = AgentsMdWatcher(tmp_path, shown=[at_root])
    assert resumed.refresh(tmp_path) is None


def test_resuming_somewhere_else_retracts_what_was_left_standing(tmp_path: Path) -> None:
    """The session ended inside pkg/. The new process starts at the root, so
    the last thing the conversation says about conventions is wrong."""
    at_root, in_pkg = _two_level_repo(tmp_path)
    resumed = AgentsMdWatcher(tmp_path, shown=[at_root, in_pkg])

    note = resumed.refresh(tmp_path)

    assert note is not None and note.startswith(REPLACEMENT_NOTICE)
    assert "ROOT RULE" in note and "PKG RULE" not in note


def test_resuming_after_the_file_was_edited_replaces_it(tmp_path: Path) -> None:
    at_root, _ = _two_level_repo(tmp_path)
    (tmp_path / "AGENTS.md").write_text("ROOT RULE, REVISED\n", encoding="utf-8")
    note = AgentsMdWatcher(tmp_path, shown=[at_root]).refresh(tmp_path)
    assert note is not None and note.startswith(REPLACEMENT_NOTICE) and "REVISED" in note


def test_resuming_after_the_file_was_deleted_says_so(tmp_path: Path) -> None:
    at_root, _ = _two_level_repo(tmp_path)
    (tmp_path / "AGENTS.md").unlink()
    assert AgentsMdWatcher(tmp_path, shown=[at_root]).refresh(tmp_path) == REMOVAL_NOTICE


def test_a_removal_on_record_is_not_removed_again_or_replaced(tmp_path: Path) -> None:
    at_root, _ = _two_level_repo(tmp_path)
    shown = [at_root, REMOVAL_NOTICE]

    # Still nothing here: nothing to add.
    (tmp_path / "AGENTS.md").unlink()
    assert AgentsMdWatcher(tmp_path, shown=shown).refresh(tmp_path) is None

    # Something here now: a plain block, not "the conventions shown earlier no
    # longer apply" about conventions the conversation already retracted.
    (tmp_path / "AGENTS.md").write_text("ROOT RULE\n", encoding="utf-8")
    note = AgentsMdWatcher(tmp_path, shown=shown).refresh(tmp_path)
    assert note is not None and note.startswith("# Project conventions")


def test_notes_that_are_not_about_conventions_are_ignored(tmp_path: Path) -> None:
    (tmp_path / "AGENTS.md").write_text("ROOT RULE\n", encoding="utf-8")
    note = AgentsMdWatcher(tmp_path, shown=["something else entirely"]).refresh(tmp_path)
    assert note is not None and note.startswith("# Project conventions")


def test_after_the_first_check_a_resumed_watcher_is_an_ordinary_one(tmp_path: Path) -> None:
    at_root, _ = _two_level_repo(tmp_path)
    resumed = AgentsMdWatcher(tmp_path, shown=[at_root])
    assert resumed.refresh(tmp_path) is None
    assert resumed.refresh(tmp_path) is None
    moved = resumed.refresh(tmp_path / "pkg")
    assert moved is not None and moved.startswith(REPLACEMENT_NOTICE) and "PKG RULE" in moved
    # And then silent again. The line this pins was found by mutation: with
    # the inherited note never cleared, every later turn in pkg/ repeated the
    # replacement -- and the three assertions above all still passed.
    assert resumed.refresh(tmp_path / "pkg") is None


def test_the_cli_does_not_repeat_agents_md_on_resume(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import minicodex.__main__ as cli

    (tmp_path / "AGENTS.md").write_text("Use uv, not pip.\n", encoding="utf-8")
    sessions = str(tmp_path / "s")
    requests = _scripted_cli(monkeypatch, ["first answer", "second answer"])
    monkeypatch.chdir(tmp_path)

    assert cli.main(["ask", "go", "--yes", "--session-dir", sessions]) == 0
    assert (
        cli.main(["ask", "and then?", "--yes", "--session-dir", sessions, "--resume", "last"]) == 0
    )

    resumed = requests[1]
    assert resumed[-1]["content"] == "and then?"
    assert [m["role"] for m in resumed].count("developer") == 1


# ---------------------------------------------------------------------------
# The command line: the lines in `__main__.py` that connect all of the above
# ---------------------------------------------------------------------------


def test_the_cli_shows_agents_md_and_follows_a_cd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every test of `AgentsMdWatcher` above calls `refresh()` itself. This one
    runs the program, where two lines decide whether any of it happens: the
    watcher being handed to the agent, and being asked about the *shell's*
    directory rather than the one the process started in."""
    import minicodex.__main__ as cli

    (tmp_path / "AGENTS.md").write_text("ROOT RULE\n", encoding="utf-8")
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "AGENTS.md").write_text("PKG RULE\n", encoding="utf-8")
    requests = _scripted_cli(monkeypatch, [[("c1", "run_shell", {"command": "cd pkg"})], "done"])
    monkeypatch.chdir(tmp_path)
    assert cli.main(["ask", "go", "--yes", "--session-dir", str(tmp_path / "s")]) == 0

    first, second = requests[0], requests[1]
    # After the user's message, not merged into the system message (F13-07).
    assert [m["role"] for m in first] == ["system", "user", "developer"]
    assert "ROOT RULE" in first[2]["content"]
    assert "ROOT RULE" not in first[0]["content"]
    # Inside the system message: the part that never changes first, the part
    # that can change (the permission state) last. Chapter 5 chose that order
    # and this chapter measured what it is worth -- 1408 cached tokens against 0.
    system = first[0]["content"]
    assert system.startswith(system_prompt().rstrip())
    assert system.index("# What you are allowed to do right now") > system.index(PLAN_INSTRUCTIONS)
    # The system message is byte-for-byte what it was: the prefix a provider caches.
    assert second[0] == first[0]
    notes = [m["content"] for m in second if m["role"] == "developer"]
    assert len(notes) == 2
    assert notes[1].startswith(REPLACEMENT_NOTICE)
    assert "ROOT RULE" in notes[1] and "PKG RULE" in notes[1]


def test_the_cli_sends_no_developer_message_when_there_is_no_agents_md(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import minicodex.__main__ as cli

    requests = _scripted_cli(monkeypatch, ["done"])
    monkeypatch.chdir(tmp_path)
    assert cli.main(["ask", "go", "--yes", "--session-dir", str(tmp_path / "s")]) == 0
    assert [m["role"] for m in requests[0]] == ["system", "user"]
