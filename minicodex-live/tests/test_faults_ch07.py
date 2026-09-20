"""Chapter 7: what has to survive the process not surviving.

Test names carry the fault ids from FAULTS.md.  Nothing here touches a network
or a real model: a crash is simulated by dropping the process's memory on the
floor, which is exactly what a crash does.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
from collections.abc import AsyncIterator, Sequence
from pathlib import Path
from typing import Any

import pytest

from minicodex.agent import Agent
from minicodex.agent_types import ToolCall
from minicodex.history import AssistantMessage, History, SystemNote, ToolResult, UserMessage
from minicodex.model import Completed, StreamEvent, TextDelta, ToolCallDelta
from minicodex.rollout import (
    ROLLOUT_VERSION,
    RolloutError,
    RolloutWriter,
    SessionMeta,
    environment_note,
    fork,
    interrupted_note,
    list_sessions,
    new_session_id,
    read_rollout,
    resolve,
)

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def meta(session_id: str = "s1", **kwargs: Any) -> SessionMeta:
    return SessionMeta(session_id=session_id, **kwargs)


def call(call_id: str, name: str = "run_shell", **arguments: Any) -> ToolCall:
    raw = json.dumps(arguments)
    return ToolCall(call_id, name, arguments, raw)


def three_turn_history() -> History:
    """A history in the shape a real run leaves behind: notes, calls, results."""
    history = History()
    history.add_system_note("You are a coding agent.")
    history.add_user("Add a retry to the client.")
    history.add_assistant("Reading it first.", [call("call_1", "read_file", path="client.py")])
    history.add_tool_result("call_1", "def send(): ...")
    history.add_assistant("Patching.", [call("call_2", "apply_patch", path="client.py")])
    history.add_tool_result("call_2", "applied")
    return history


class ScriptedModel:
    """Replays a fixed list of turns.  Same shape as interlude A's."""

    def __init__(self, turns: Sequence[Any]) -> None:
        self.turns = list(turns)
        self.sent: list[list[dict[str, Any]]] = []

    async def stream(self, messages: Sequence[dict[str, Any]]) -> AsyncIterator[StreamEvent]:
        self.sent.append([dict(m) for m in messages])
        turn = self.turns[len(self.sent) - 1]
        if isinstance(turn, str):
            yield TextDelta(turn)
        else:
            for delta in turn:
                yield delta
        yield Completed("stop")


def delta(call_id: str, index: int, name: str, **arguments: Any) -> ToolCallDelta:
    return ToolCallDelta(call_id=call_id, index=index, name=name, arguments=json.dumps(arguments))


# ---------------------------------------------------------------------------
# F07-01  the process exits and the history is gone
# ---------------------------------------------------------------------------


async def test_F07_01_history_survives_the_process(tmp_path: Path) -> None:
    """Everything the agent said reaches disk while it is being said.

    Not "at the end of the run": the run ending normally is the case that did
    not need saving.
    """
    path = tmp_path / "s.jsonl"
    model = ScriptedModel([[delta("call_1", 0, "read_file", path="x.py")], "done"])

    async def read_file(args: dict[str, Any]) -> str:
        return "contents"

    with RolloutWriter(path, meta()) as writer:
        agent = Agent(model, {"read_file": read_file}, rollout=writer)
        result = await agent.run("what is in x.py")

    assert result.stop_reason == "completed"
    loaded = read_rollout(path)
    # user, assistant(call), tool result, assistant(text)
    assert [type(i).__name__ for i in loaded.items] == [
        "UserMessage",
        "AssistantMessage",
        "ToolResult",
        "AssistantMessage",
    ]
    restored, dropped = loaded.history()
    assert dropped == 0
    assert restored.to_wire() == result.history.to_wire()


def test_F07_01_written_during_the_run_not_at_the_end(tmp_path: Path) -> None:
    """The distinction the fault is about, asserted directly.

    A run that is killed halfway has no "end", so a writer that flushes at the
    end writes nothing at all.  Reading the file with the history still live is
    the cheapest way to state that this one does not.
    """
    path = tmp_path / "s.jsonl"
    with RolloutWriter(path, meta()) as writer:
        history = History(observer=writer.append)
        history.add_user("first")
        assert len(read_rollout(path).items) == 1
        history.add_assistant("second")
        assert len(read_rollout(path).items) == 2


# ---------------------------------------------------------------------------
# F07-02  a crash mid-tool leaves a call with no output
# ---------------------------------------------------------------------------


def test_F07_02_unanswered_call_is_truncated_on_load(tmp_path: Path) -> None:
    """The file ends after a call and before its result: a shape no server accepts."""
    path = tmp_path / "s.jsonl"
    with RolloutWriter(path, meta()) as writer:
        history = History(observer=writer.append)
        history.add_user("go")
        history.add_assistant("running it", [call("call_1")])
        # ...and here the process dies.

    loaded = read_rollout(path)
    assert len(loaded.items) == 2

    restored, dropped = loaded.history()
    assert dropped == 1
    assert [type(i).__name__ for i in restored.items] == ["UserMessage"]
    # The real assertion: what comes back can be sent.
    assert restored.to_wire() == [{"role": "user", "content": "go"}]


def test_F07_02_only_the_incomplete_tail_is_dropped(tmp_path: Path) -> None:
    """Two finished turns and one unfinished one: the finished work is kept."""
    path = tmp_path / "s.jsonl"
    with RolloutWriter(path, meta()) as writer:
        history = History(observer=writer.append)
        for item in three_turn_history().items:
            _replay(history, item)
        history.add_assistant("Running the tests.", [call("call_3")])

    restored, dropped = read_rollout(path).history()
    assert dropped == 1
    assert len(restored) == 6
    assert restored.unanswered() == ()


def test_F07_02_a_partially_answered_turn_is_dropped_whole(tmp_path: Path) -> None:
    """Two calls in one turn, one answered: the turn is not half-kept.

    Keeping the answered half would leave an assistant message whose second
    call has no result -- the invalid shape this whole mechanism exists to
    avoid -- so the boundary is "nothing outstanding", not "drop the last
    item".
    """
    path = tmp_path / "s.jsonl"
    with RolloutWriter(path, meta()) as writer:
        history = History(observer=writer.append)
        history.add_user("go")
        history.add_assistant("two things", [call("call_1"), call("call_2")])
        history.add_tool_result("call_1", "ok")

    restored, dropped = read_rollout(path).history()
    assert dropped == 2
    assert [type(i).__name__ for i in restored.items] == ["UserMessage"]


# ---------------------------------------------------------------------------
# F07-03  a damaged line ends the file
# ---------------------------------------------------------------------------


def test_F07_03_a_truncated_last_line_is_the_boundary(tmp_path: Path) -> None:
    path = tmp_path / "s.jsonl"
    with RolloutWriter(path, meta()) as writer:
        history = History(observer=writer.append)
        history.add_user("go")
        history.add_assistant("here you go")
    with path.open("a", encoding="utf-8", newline="") as fh:
        fh.write('{"type": "user", "te')

    loaded = read_rollout(path)
    assert loaded.truncated_at == 4  # meta, user, assistant, junk
    assert len(loaded.items) == 2
    assert loaded.history()[0].to_wire()[0] == {"role": "user", "content": "go"}


def test_F07_03_damage_in_the_middle_does_not_resurrect_the_tail(tmp_path: Path) -> None:
    """A bad line stops the read; later lines are not skipped past.

    Skipping would produce a history with a hole in it -- a result whose call
    is missing -- which is the F07-02 shape arriving by a different route.
    """
    path = tmp_path / "s.jsonl"
    with RolloutWriter(path, meta()) as writer:
        history = History(observer=writer.append)
        history.add_user("go")
        history.add_assistant("calling", [call("call_1")])
        history.add_tool_result("call_1", "output")

    lines = path.read_text(encoding="utf-8").splitlines()
    lines.insert(2, "}not json{")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="")

    loaded = read_rollout(path)
    assert loaded.truncated_at == 3
    assert len(loaded.items) == 1
    assert loaded.history()[1] == 0


def test_F07_03_an_unknown_record_type_is_also_a_boundary(tmp_path: Path) -> None:
    """Well-formed JSON this version does not understand is not skipped either."""
    path = tmp_path / "s.jsonl"
    with RolloutWriter(path, meta()) as writer:
        writer.append(UserMessage("go"))
    with path.open("a", encoding="utf-8", newline="") as fh:
        fh.write(json.dumps({"type": "reasoning_summary", "text": "hm"}) + "\n")
        fh.write(json.dumps({"type": "user", "text": "later"}) + "\n")

    loaded = read_rollout(path)
    assert loaded.truncated_at == 3
    assert len(loaded.items) == 1


def test_F07_03_a_file_without_a_header_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "not-a-session.jsonl"
    path.write_text(json.dumps({"type": "user", "text": "hi"}) + "\n", encoding="utf-8")
    with pytest.raises(RolloutError, match="no session header"):
        read_rollout(path)


# ---------------------------------------------------------------------------
# F07-04  Ctrl-C: the tool, or the turn?
# ---------------------------------------------------------------------------


async def test_F07_04_cancelling_a_tool_answers_every_issued_call(tmp_path: Path) -> None:
    """The invariant survives the interrupt.

    `_run_tool` promises never to raise, and `except Exception` does not catch
    `CancelledError`.  Without the explicit clause, the loop unwinds leaving
    calls unanswered and the history unsendable.
    """
    path = tmp_path / "s.jsonl"
    model = ScriptedModel(
        [[delta("call_1", 0, "slow", x=1), delta("call_2", 1, "slow", x=2)], "done"]
    )
    started = asyncio.Event()

    async def slow(args: dict[str, Any]) -> str:
        started.set()
        await asyncio.sleep(60)
        return "never"

    with RolloutWriter(path, meta()) as writer:
        agent = Agent(model, {"slow": slow}, rollout=writer)
        task = asyncio.ensure_future(agent.run("go"))
        await started.wait()
        task.cancel()
        result = await task

    assert result.stop_reason == "interrupted"
    assert result.history.unanswered() == ()
    # Both calls answered, both saying so.
    outputs = [i for i in result.history.items if isinstance(i, ToolResult)]
    assert len(outputs) == 2
    assert all("interrupted by the user" in o.content for o in outputs)
    # And the next request would be legal.
    assert result.history.to_wire()


async def test_F07_04_the_run_ends_rather_than_continuing(tmp_path: Path) -> None:
    """Cancelling the tool does not mean "skip this tool and carry on".

    The user pressed Ctrl-C to stop something.  A loop that answers the call and
    then asks the model what to do next has spent money to ignore them.
    """
    model = ScriptedModel([[delta("call_1", 0, "slow")], "should never be reached"])
    started = asyncio.Event()

    async def slow(args: dict[str, Any]) -> str:
        started.set()
        await asyncio.sleep(60)
        return "never"

    agent = Agent(model, {"slow": slow})
    task = asyncio.ensure_future(agent.run("go"))
    await started.wait()
    task.cancel()
    result = await task

    assert result.stop_reason == "interrupted"
    assert len(model.sent) == 1


async def test_F07_04_an_interrupt_between_turns_is_not_swallowed() -> None:
    """Cancellation outside a tool call still cancels.

    The clause added for F07-04 catches `CancelledError` around one `await`.
    If it were written around the whole loop instead, a Ctrl-C while waiting on
    the *model* would be reported as a clean interrupted run -- and the task
    would not actually be cancelled, which is a lie to whoever awaits it.
    """

    class SlowModel:
        async def stream(self, messages: Sequence[dict[str, Any]]) -> AsyncIterator[StreamEvent]:
            await asyncio.sleep(60)
            yield Completed("stop")

    agent = Agent(SlowModel(), {})
    task = asyncio.ensure_future(agent.run("go"))
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


# ---------------------------------------------------------------------------
# F07-05  the model is told it was interrupted
# ---------------------------------------------------------------------------


async def test_F07_05_the_interrupted_run_leaves_a_note(tmp_path: Path) -> None:
    model = ScriptedModel([[delta("call_1", 0, "slow")], "done"])
    started = asyncio.Event()

    async def slow(args: dict[str, Any]) -> str:
        started.set()
        await asyncio.sleep(60)
        return "never"

    agent = Agent(model, {"slow": slow})
    task = asyncio.ensure_future(agent.run("go"))
    await started.wait()
    task.cancel()
    result = await task

    notes = [i.text for i in result.history.items if isinstance(i, SystemNote)]
    assert any("interrupted by the user" in n for n in notes)


def test_F07_05_recovery_states_how_much_was_lost() -> None:
    """The count is the difference between "ignore this" and "redo this"."""
    assert "2 message(s)" in interrupted_note(2)
    assert interrupted_note() != interrupted_note(0)


async def test_F07_05_a_resumed_run_carries_the_note_into_the_request(
    tmp_path: Path,
) -> None:
    """The note has to reach the model, not just the history object."""
    path = tmp_path / "s.jsonl"
    with RolloutWriter(path, meta()) as writer:
        history = History(observer=writer.append)
        history.add_user("go")
        history.add_assistant("running", [call("call_1")])

    restored, dropped = read_rollout(path).history()
    restored.add_system_note(interrupted_note(dropped))

    model = ScriptedModel(["ok"])
    agent = Agent(model, {}, resume_from=restored)
    await agent.run("carry on")

    sent = model.sent[0]
    assert any("were discarded" in m["content"] for m in sent if m["role"] == "system")
    assert sent[-1] == {"role": "user", "content": "carry on"}


# ---------------------------------------------------------------------------
# F07-06  background processes survive the interrupt
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    not hasattr(__import__("os"), "killpg"),
    reason="F02-10: killpg is POSIX-only and the Windows equivalent is not built",
)
async def test_F07_06_cancelling_a_command_kills_it() -> None:
    """A cancelled `run_shell` does not leave the command running.

    Chapter 2 killed the process group on timeout, which was the only way out
    of that function at the time.  Cancellation is a second way out.
    """
    from minicodex.shell import ShellSession

    session = ShellSession(timeout=30)
    marker = Path(".") / f"f07_06_{new_session_id()}.marker"
    task = asyncio.ensure_future(session.run(f"sleep 5; touch {marker}"))
    await asyncio.sleep(0.4)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await asyncio.sleep(1.0)
    assert not marker.exists(), "the command outlived the interrupt"


# ---------------------------------------------------------------------------
# F07-07  the environment moved since the session was written
# ---------------------------------------------------------------------------


def test_F07_07_a_changed_environment_is_reported() -> None:
    before = meta(cwd="/repo/a", model="gpt-4o-mini", sandbox_mode="read-only")
    after = meta("s2", cwd="/repo/b", model="gpt-4o-mini", sandbox_mode="workspace-write")
    note = environment_note(before, after)
    assert note is not None
    assert "working directory" in note and "/repo/b" in note
    assert "sandbox mode" in note
    assert "model" not in note  # unchanged fields are not mentioned


def test_F07_07_an_unchanged_environment_says_nothing() -> None:
    """Silence when nothing moved.

    A note on every resume is a note the model learns to skip, and it costs
    tokens on every turn afterwards.
    """
    before = meta(cwd="/repo", model="m", sandbox_mode="read-only")
    assert (
        environment_note(before, meta("s2", cwd="/repo", model="m", sandbox_mode="read-only"))
        is None
    )


def test_F07_07_unknown_fields_are_not_reported_as_changes() -> None:
    """An old file with no `sandbox_mode` recorded is not a sandbox_mode change."""
    before = meta(cwd="/repo")
    after = meta("s2", cwd="/repo", sandbox_mode="workspace-write")
    assert environment_note(before, after) is None


# ---------------------------------------------------------------------------
# F07-08  two processes, one file
# ---------------------------------------------------------------------------


def test_F07_08_a_second_writer_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "s.jsonl"
    with RolloutWriter(path, meta()):
        with pytest.raises(RolloutError, match="already open"):
            RolloutWriter(path, meta("s2"))


def test_F07_08_the_lock_names_the_process_holding_it(tmp_path: Path) -> None:
    """A stale lock is a decision for the user, and they cannot make it blind."""
    import os

    path = tmp_path / "s.jsonl"
    with RolloutWriter(path, meta()):
        with pytest.raises(RolloutError) as excinfo:
            RolloutWriter(path, meta("s2"))
    assert str(os.getpid()) in str(excinfo.value)
    assert ".lock" in str(excinfo.value)


def test_F07_08_the_lock_is_released_on_close(tmp_path: Path) -> None:
    path = tmp_path / "s.jsonl"
    with RolloutWriter(path, meta()) as first:
        assert first.lock_path.exists()
    assert not first.lock_path.exists()
    RolloutWriter(path, meta("s2")).release()


def test_F07_08_two_real_processes_do_corrupt_a_shared_file(tmp_path: Path) -> None:
    """The measurement the lock exists for, at a size that runs in CI.

    Not a test of minicodex: a test of the assumption underneath it.  If
    concurrent appends were safe, the lock would be ceremony.
    """
    path = tmp_path / "shared.jsonl"
    child = (
        "import json,sys\n"
        "tag=sys.argv[2]\n"
        "fh=open(sys.argv[1],'a',encoding='utf-8')\n"
        "for i in range(4000):\n"
        "    fh.write(json.dumps({'w':tag,'n':i,'p':'x'*200})+'\\n'); fh.flush()\n"
    )
    procs = [subprocess.Popen([sys.executable, "-c", child, str(path), tag]) for tag in ("A", "B")]
    for proc in procs:
        proc.wait(timeout=60)

    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    writers = set()
    broken = 0
    for line in lines:
        try:
            writers.add(json.loads(line)["w"])
        except (json.JSONDecodeError, KeyError):
            broken += 1
    # 8000 records were written.  What is on disk is fewer -- the two writers
    # keep their own file offsets and overwrite each other's records -- and it
    # is a single file containing two unrelated sessions.  Both are unfixable
    # after the fact, which is why the writer refuses to be the second one.
    assert writers == {"A", "B"}
    assert len(lines) < 8000, "concurrent appends lost nothing; the lock would be ceremony"


# ---------------------------------------------------------------------------
# F07-09  the format changed
# ---------------------------------------------------------------------------


def test_F07_09_a_version_1_file_still_loads(tmp_path: Path) -> None:
    """Version 1 had no `raw_arguments`.  Refusing to open it is the bad fix."""
    path = tmp_path / "old.jsonl"
    path.write_text(
        "\n".join(
            [
                json.dumps({"type": "meta", "session_id": "old", "version": 1}),
                json.dumps({"type": "user", "text": "go"}),
                json.dumps(
                    {
                        "type": "assistant",
                        "text": "reading",
                        "tool_calls": [
                            {
                                "call_id": "call_1",
                                "name": "read_file",
                                "arguments": {"path": "x.py"},
                            }
                        ],
                    }
                ),
                json.dumps(
                    {
                        "type": "tool_result",
                        "call_id": "call_1",
                        "name": "read_file",
                        "content": "ok",
                    }
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    loaded = read_rollout(path)
    assert loaded.truncated_at is None
    restored, dropped = loaded.history()
    assert dropped == 0
    wire = restored.to_wire()
    assert wire[1]["tool_calls"][0]["function"]["arguments"] == '{"path": "x.py"}'


def test_F07_09_a_version_2_file_keeps_the_bytes_the_model_sent(tmp_path: Path) -> None:
    """The reason the version was bumped, stated as a test.

    A model that sent `{"path":"x.py"}` gets that string back, not this
    program's idea of how to spell it.  Version 1 could not do this, and the
    difference is invisible until something downstream compares bytes.
    """
    path = tmp_path / "new.jsonl"
    raw = '{"path":"x.py"}'
    with RolloutWriter(path, meta()) as writer:
        history = History(observer=writer.append)
        history.add_user("go")
        history.add_assistant("reading", [ToolCall("call_1", "read_file", {"path": "x.py"}, raw)])
        history.add_tool_result("call_1", "ok")

    restored, _ = read_rollout(path).history()
    assert restored.to_wire()[1]["tool_calls"][0]["function"]["arguments"] == raw


def test_F07_09_new_files_carry_the_current_version(tmp_path: Path) -> None:
    path = tmp_path / "s.jsonl"
    RolloutWriter(path, meta()).release()
    header = json.loads(path.read_text(encoding="utf-8").splitlines()[0])
    assert header["version"] == ROLLOUT_VERSION


# ---------------------------------------------------------------------------
# F07-10  a fork must not share writes with its parent
# ---------------------------------------------------------------------------


def test_F07_10_a_fork_copies_rather_than_references(tmp_path: Path) -> None:
    source = tmp_path / "a.jsonl"
    with RolloutWriter(source, meta("parent")) as writer:
        history = History(observer=writer.append)
        for item in three_turn_history().items:
            _replay(history, item)

    forked = fork(source, upto=2, directory=tmp_path)

    # The parent keeps going after the fork...
    with RolloutWriter(source, meta("parent")) as writer:
        writer.append(UserMessage("parent carries on"))

    child = read_rollout(forked)
    assert len(child.items) == 2
    assert all("parent carries on" != getattr(i, "text", None) for i in child.items)
    assert child.meta.forked_from == "parent"
    assert child.meta.forked_at == 2

    # ...and the child can be written to without touching the parent.
    with RolloutWriter(forked, child.meta) as writer:
        writer.append(UserMessage("child goes elsewhere"))
    parent_texts = [getattr(i, "text", "") for i in read_rollout(source).items]
    assert "child goes elsewhere" not in parent_texts


def test_F07_10_forking_a_whole_session_keeps_all_of_it(tmp_path: Path) -> None:
    source = tmp_path / "a.jsonl"
    with RolloutWriter(source, meta("parent")) as writer:
        writer.extend(three_turn_history().items)
    forked = read_rollout(fork(source, directory=tmp_path))
    assert len(forked.items) == 6
    assert forked.meta.session_id != "parent"


def test_F07_10_a_fork_that_cuts_mid_turn_is_still_loadable(tmp_path: Path) -> None:
    """`--upto 3` can land between a call and its result.

    The same rule that recovers a crashed session covers this: the loader drops
    the unfinished tail rather than the fork having to know about turns.
    """
    source = tmp_path / "a.jsonl"
    with RolloutWriter(source, meta("parent")) as writer:
        writer.extend(three_turn_history().items)

    forked = read_rollout(fork(source, upto=3, directory=tmp_path))
    assert len(forked.items) == 3
    restored, dropped = forked.history()
    assert dropped == 1
    assert restored.to_wire()  # sendable


# ---------------------------------------------------------------------------
# the pieces around them
# ---------------------------------------------------------------------------


def test_sessions_are_listed_newest_first(tmp_path: Path) -> None:
    for name in ("20260101T000000-1", "20260102T000000-1"):
        RolloutWriter(tmp_path / f"{name}.jsonl", meta(name)).release()
    listed = [r.meta.session_id for r in list_sessions(tmp_path)]
    assert listed == ["20260102T000000-1", "20260101T000000-1"]


def test_an_unreadable_file_does_not_break_the_listing(tmp_path: Path) -> None:
    RolloutWriter(tmp_path / "good.jsonl", meta("good")).release()
    (tmp_path / "bad.jsonl").write_text("garbage\n", encoding="utf-8")
    assert [r.meta.session_id for r in list_sessions(tmp_path)] == ["good"]


def test_resolve_accepts_an_id_a_path_or_last(tmp_path: Path) -> None:
    path = tmp_path / "20260101T000000-1.jsonl"
    RolloutWriter(path, meta("20260101T000000-1")).release()
    assert resolve("20260101T000000-1", tmp_path) == path
    assert resolve(str(path), tmp_path) == path
    assert resolve("last", tmp_path) == path
    with pytest.raises(RolloutError):
        resolve("nope", tmp_path)


async def test_a_resumed_run_appends_to_a_new_file(tmp_path: Path) -> None:
    """Resuming does not reopen the old file.

    Appending to it would put the recovered prefix and the discarded tail in
    one file, and the next recovery would have to work out which of the two it
    was looking at.
    """
    first = tmp_path / "first.jsonl"
    with RolloutWriter(first, meta("first")) as writer:
        history = History(observer=writer.append)
        history.add_user("go")
        history.add_assistant("calling", [call("call_1")])

    restored, _ = read_rollout(first).history()
    second = tmp_path / "second.jsonl"
    with RolloutWriter(second, meta("second")) as writer:
        agent = Agent(ScriptedModel(["done"]), {}, rollout=writer, resume_from=restored)
        await agent.run("carry on")

    assert len(read_rollout(first).items) == 2  # untouched
    assert [type(i).__name__ for i in read_rollout(second).items] == [
        "UserMessage",  # the replayed one
        "UserMessage",  # the new instruction
        "AssistantMessage",
    ]


async def test_compaction_writes_a_new_baseline(tmp_path: Path) -> None:
    """A compacted session resumes at its compacted size.

    The file is append-only, so a replaced history is expressed by a marker and
    a new baseline after it.  Without the marker, resuming replays the turns
    compaction removed and the session comes back at the size that made it
    compact.
    """
    path = tmp_path / "s.jsonl"
    with RolloutWriter(path, meta()) as writer:
        history = History(observer=writer.append)
        history.add_user("go")
        history.add_assistant("a", [call("call_1")])
        history.add_tool_result("call_1", "x" * 100)
        writer.mark("compacted", generation=1, replaced=2)
        writer.append(UserMessage("go"))
        writer.append(SystemNote("[compacted transcript | generation 1]"))

    loaded = read_rollout(path)
    assert len(loaded.items) == 2
    assert any(m["mark"] == "compacted" for m in loaded.marks)


def _replay(history: History, item: Any) -> None:
    if isinstance(item, UserMessage):
        history.add_user(item.text)
    elif isinstance(item, SystemNote):
        history.add_system_note(item.text)
    elif isinstance(item, AssistantMessage):
        history.add_assistant(item.text, item.tool_calls)
    elif isinstance(item, ToolResult):
        history.add_tool_result(item.call_id, item.content)


def test_F07_05_the_wording_is_pinned() -> None:
    """The note was measured; an edit to it is a change to a measured thing.

    Chapter 3 learned this the expensive way (F03-10): one word changed in a
    description flipped both providers 3/3 to 0/3, and nothing noticed.  Here
    the wording moved a model from 0/13 to 11/11.  A snapshot does not stop
    anyone editing it -- it stops them editing it *by accident*, and it puts
    the number they have to beat in the failure message.
    """
    assert interrupted_note(2) == (
        "The previous session ended without finishing its last turn. "
        "2 message(s) were discarded because they were incomplete."
    ), "measured at 11/11 (probe_resume.py); re-measure before changing it"


def test_F07_02_a_session_with_no_complete_turn_recovers_to_nothing(tmp_path: Path) -> None:
    """Raised in review: `settled` can be empty, and the caller must be able to tell.

    An empty history plus `dropped == len(items)` is a legal result -- it means
    "there is no complete conversation in this file", which is true of a session
    that crashed inside its first turn.  The CLI prints the count for exactly
    this reason.
    """
    path = tmp_path / "s.jsonl"
    with RolloutWriter(path, meta()) as writer:
        writer.append(AssistantMessage("straight in", (call("call_1"),)))

    loaded = read_rollout(path)
    restored, dropped = loaded.history()
    assert dropped == len(loaded.items) == 1
    assert len(restored) == 0
    assert restored.to_wire() == []
