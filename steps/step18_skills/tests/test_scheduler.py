"""Chapter 8: which tool calls in one turn may run at the same time.

Test names carry the fault ids from FAULTS.md. The races in here are real --
real OS threads via `asyncio.to_thread`, real files on disk -- with just
enough of a delay inserted at the one line that matters to make the
interleaving land the same way every run instead of most runs. That delay,
and why a bare `time.sleep`-based race is the wrong thing to check in, is
F08-09.
"""

from __future__ import annotations

import asyncio
import functools
import json
import threading
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest

from minicodex.agent import Agent
from minicodex.agent_types import ToolCall
from minicodex.approval import Session
from minicodex.history import ToolResult
from minicodex.model import Completed, TextDelta, ToolCallDelta
from minicodex.patch import Edit, apply_edits
from minicodex.scheduler import STATEFUL, Footprint, batches, conflicts
from minicodex.tools import default_tools, footprint_of

# ---------------------------------------------------------------------------
# helpers -- same shapes as tests/test_faults_ch07.py
# ---------------------------------------------------------------------------


def call(call_id: str, name: str, **arguments: Any) -> ToolCall:
    raw = json.dumps(arguments)
    return ToolCall(call_id, name, arguments, raw)


def delta(call_id: str, index: int, name: str, **arguments: Any) -> ToolCallDelta:
    return ToolCallDelta(call_id=call_id, index=index, name=name, arguments=json.dumps(arguments))


class ScriptedModel:
    """Replays a fixed list of turns. Same shape as interlude A's and chapter 7's."""

    def __init__(self, turns: Sequence[Any]) -> None:
        self.turns = list(turns)
        self.sent: list[list[dict[str, Any]]] = []

    async def stream(self, messages: Sequence[dict[str, Any]]) -> Any:
        self.sent.append([dict(m) for m in messages])
        turn = self.turns[len(self.sent) - 1]
        if isinstance(turn, str):
            yield TextDelta(turn)
        else:
            for d in turn:
                yield d
        yield Completed("stop")


def make_agent(model: Any, root: Path, *, max_concurrent_tools: int = 8) -> Agent:
    """A real agent, wired to real files, with chapter 8's scheduler switched on.

    This is the one non-default call site in the whole test suite:
    `footprint_of` is bound to `root` explicitly, exactly as `__main__.py`
    binds it. Every other Agent built in this project's tests still gets the
    old fully-serial default, on purpose -- see `agent.py`'s docstring.
    """
    session = Session(mode="workspace-write")
    tools = default_tools(root=root, session=session)
    return Agent(
        model,
        tools,
        footprint_of=functools.partial(footprint_of, root=root),
        max_concurrent_tools=max_concurrent_tools,
    )


class PeakTracker:
    """How many conflicting sections were inside their critical section at once.

    A `threading.Lock`, not an `asyncio.Lock`: the code under test runs
    through `asyncio.to_thread`, on real OS threads, and an asyncio primitive
    is not safe to touch from there.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._active = 0
        self.peak = 0

    def enter(self) -> None:
        with self._lock:
            self._active += 1
            self.peak = max(self.peak, self._active)

    def exit(self) -> None:
        with self._lock:
            self._active -= 1


# ---------------------------------------------------------------------------
# conflicts() / batches() -- pure, fast, no event loop needed
# ---------------------------------------------------------------------------


def test_read_read_never_conflicts() -> None:
    a = Footprint(reads=frozenset({"f.txt"}))
    b = Footprint(reads=frozenset({"f.txt"}))
    assert not conflicts(a, b)


def test_F08_01_write_write_same_key_conflicts() -> None:
    a = Footprint(writes=frozenset({"f.txt"}))
    b = Footprint(writes=frozenset({"f.txt"}))
    assert conflicts(a, b)


def test_F08_02_write_read_same_key_conflicts_either_way_round() -> None:
    w = Footprint(writes=frozenset({"f.txt"}))
    r = Footprint(reads=frozenset({"f.txt"}))
    assert conflicts(w, r)
    assert conflicts(r, w)


def test_disjoint_keys_do_not_conflict() -> None:
    a = Footprint(writes=frozenset({"a.txt"}))
    b = Footprint(writes=frozenset({"b.txt"}))
    assert not conflicts(a, b)


def test_F08_03_stateful_conflicts_with_everything_including_another_stateful_call() -> None:
    assert conflicts(STATEFUL, Footprint(reads=frozenset({"f.txt"})))
    assert conflicts(STATEFUL, Footprint())
    assert conflicts(STATEFUL, STATEFUL)


def test_batches_groups_disjoint_calls_into_one_batch() -> None:
    """The entire concurrency win this chapter has to offer, in one example."""
    calls = [call("c1", "read_file", path="a.txt"), call("c2", "read_file", path="b.txt")]
    fps = {"c1": Footprint(reads=frozenset({"a.txt"})), "c2": Footprint(reads=frozenset({"b.txt"}))}

    plan = batches(calls, lambda c: fps[c.call_id])

    assert plan == [calls]


def test_F08_05_a_conflicting_pair_serialises_even_with_an_unrelated_call_between_them() -> None:
    """a and b both write f.txt, with c -- an unrelated read of a different
    file -- issued between them. b must still land strictly after a, because
    a call is only ever pushed *later* by something it conflicts with among
    the calls before it; c does not conflict with either, so it is free to
    join a's batch instead of waiting its turn in the list. "Preserve
    submission order" is true of conflicting calls, not of the whole list --
    a weaker guarantee than it sounds, and the one this chapter actually
    needs (F08-01)."""
    a = call("a", "apply_patch", edits=[{"path": "f.txt"}])
    b = call("b", "apply_patch", edits=[{"path": "f.txt"}])
    c = call("c", "read_file", path="g.txt")
    fps = {
        "a": Footprint(writes=frozenset({"f.txt"})),
        "b": Footprint(writes=frozenset({"f.txt"})),
        "c": Footprint(reads=frozenset({"g.txt"})),
    }

    plan = batches([a, b, c], lambda call: fps[call.call_id])

    assert plan == [[a, c], [b]]


def test_batches_of_an_empty_turn_is_empty() -> None:
    assert batches([], lambda call: STATEFUL) == []


def test_F08_03_an_unclassifiable_call_serialises_with_its_neighbours() -> None:
    """Two read_file calls that would otherwise batch together, with a
    run_shell call (always STATEFUL) issued between them."""
    a = call("a", "read_file", path="x.txt")
    b = call("b", "run_shell", command="true")
    c = call("c", "read_file", path="y.txt")
    fps = {
        "a": Footprint(reads=frozenset({"x.txt"})),
        "b": STATEFUL,
        "c": Footprint(reads=frozenset({"y.txt"})),
    }

    plan = batches([a, b, c], lambda call: fps[call.call_id])

    assert plan == [[a], [b], [c]]


# ---------------------------------------------------------------------------
# tools.footprint_of() -- what the four built-in tools declare
# ---------------------------------------------------------------------------


def test_F08_01_footprint_of_read_file_is_the_resolved_path(tmp_path: Path) -> None:
    (tmp_path / "x.txt").write_text("hi")

    fp = footprint_of(call("c1", "read_file", path="x.txt"), tmp_path)

    assert fp.reads == frozenset({str((tmp_path / "x.txt").resolve())})
    assert fp.writes == frozenset()
    assert not fp.stateful


def test_F08_01_footprint_of_apply_patch_is_every_edited_path(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_text("a")
    (tmp_path / "b.txt").write_text("b")
    edits = [{"path": "a.txt", "old_text": "a", "new_text": "aa"}, {"path": "b.txt"}]

    fp = footprint_of(call("c1", "apply_patch", edits=edits), tmp_path)

    assert fp.writes == {str((tmp_path / "a.txt").resolve()), str((tmp_path / "b.txt").resolve())}
    assert not fp.stateful


def test_F08_03_footprint_of_run_shell_is_stateful(tmp_path: Path) -> None:
    assert footprint_of(call("c1", "run_shell", command="ls"), tmp_path) == STATEFUL


def test_footprint_of_request_permissions_is_stateful(tmp_path: Path) -> None:
    assert footprint_of(call("c1", "request_permissions", needs="x", why="y"), tmp_path) == STATEFUL


def test_footprint_of_unknown_tool_is_stateful(tmp_path: Path) -> None:
    assert footprint_of(call("c1", "get_weather", city="nyc"), tmp_path) == STATEFUL


def test_footprint_of_malformed_arguments_is_stateful(tmp_path: Path) -> None:
    """Same call site chapter 0 uses for `arguments is None`: the model sent
    something that did not parse as a JSON object, so there is nothing to
    classify. The handler still gives the model a real error; the scheduler
    just refuses to guess and runs the call alone."""
    unparsed = ToolCall("c1", "read_file", None, "{not json")
    assert footprint_of(unparsed, tmp_path) == STATEFUL


def test_footprint_of_a_path_outside_the_repository_is_stateful(tmp_path: Path) -> None:
    """`resolve()` refuses the path; the scheduler inherits that refusal
    rather than deciding it means "touches nothing" (Footprint()) or crashing."""
    fp = footprint_of(call("c1", "read_file", path="../../etc/passwd"), tmp_path)
    assert fp == STATEFUL


def test_footprint_of_apply_patch_with_no_path_in_an_edit_is_stateful(tmp_path: Path) -> None:
    fp = footprint_of(
        call("c1", "apply_patch", edits=[{"old_text": "x", "new_text": "y"}]), tmp_path
    )
    assert fp == STATEFUL


# ---------------------------------------------------------------------------
# F08-01  two patches to one file: last write wins
# ---------------------------------------------------------------------------


async def test_F08_01_naive_concurrent_writes_to_one_file_lose_an_edit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The thing you would reach for first: run every call through
    asyncio.gather() and let the threads sort themselves out.

    `read_source` gets a real, small, deterministic delay so both threads are
    guaranteed to have read the *original* content before either one writes --
    which is not a contrived worst case, it is what "both calls started at
    roughly the same time" already looks like without it; the delay only
    removes the small chance this run happened not to land there.
    """
    path = tmp_path / "shared.txt"
    path.write_text("A = 1\nB = 2\n")

    import minicodex.patch as patch_mod

    original_read_source = patch_mod.read_source

    def slow_read_source(p: Path) -> tuple[str, str]:
        text, ending = original_read_source(p)
        time.sleep(0.05)
        return text, ending

    monkeypatch.setattr(patch_mod, "read_source", slow_read_source)

    edit_a = Edit(str(path), "A = 1", "A = 100")
    edit_b = Edit(str(path), "B = 2", "B = 200")

    result_a, result_b = await asyncio.gather(
        asyncio.to_thread(apply_edits, [edit_a], tmp_path),
        asyncio.to_thread(apply_edits, [edit_b], tmp_path),
    )

    # Both calls reported success. Neither one is lying about what *it* did --
    # each computed its own edit correctly from what it read.
    assert "Applied 1 edit" in result_a
    assert "Applied 1 edit" in result_b

    final = path.read_text()
    survived = ("A = 100" in final, "B = 200" in final)
    assert survived != (True, True), (
        f"expected the race to lose one edit; both survived this run "
        f"(final content: {final!r}) -- the delay was not enough on this machine"
    )


async def test_F08_01_the_scheduler_serialises_writes_to_the_same_file(tmp_path: Path) -> None:
    """The fix: the same two edits, run through Agent.run() instead of a bare
    gather(). batches() puts them in separate batches -- checked directly --
    and, because agent.py never starts batch N+1 until batch N's gather() has
    returned, the second write can only ever see the first one's result."""
    path = tmp_path / "shared.txt"
    path.write_text("A = 1\nB = 2\n")

    plan = batches(
        [
            call(
                "call_1",
                "apply_patch",
                edits=[{"path": "shared.txt", "old_text": "x", "new_text": "y"}],
            ),
            call(
                "call_2",
                "apply_patch",
                edits=[{"path": "shared.txt", "old_text": "x", "new_text": "y"}],
            ),
        ],
        functools.partial(footprint_of, root=tmp_path),
    )
    assert len(plan) == 2, "two writes to the same file must never share a batch"

    model = ScriptedModel(
        [
            [
                delta(
                    "call_1",
                    0,
                    "apply_patch",
                    edits=[{"path": "shared.txt", "old_text": "A = 1", "new_text": "A = 100"}],
                ),
                delta(
                    "call_2",
                    1,
                    "apply_patch",
                    edits=[{"path": "shared.txt", "old_text": "B = 2", "new_text": "B = 200"}],
                ),
            ],
            "done",
        ]
    )
    agent = make_agent(model, tmp_path)

    result = await agent.run("apply both edits")

    assert result.stop_reason == "completed"
    final = path.read_text()
    assert "A = 100" in final
    assert "B = 200" in final


# ---------------------------------------------------------------------------
# F08-02  a read overlapping a write
# ---------------------------------------------------------------------------


async def test_F08_02_a_read_and_a_write_to_the_same_file_never_overlap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "f.txt"
    path.write_text("before\n")

    import minicodex.patch as patch_mod
    import minicodex.tools as tools_mod

    tracker = PeakTracker()

    original_write_source = patch_mod.write_source

    def tracked_write_source(p: Path, text: str, ending: str) -> None:
        tracker.enter()
        try:
            time.sleep(0.03)
            original_write_source(p, text, ending)
        finally:
            tracker.exit()

    original_read = tools_mod._read

    def tracked_read(p: Path) -> str:
        tracker.enter()
        try:
            time.sleep(0.03)
            return original_read(p)
        finally:
            tracker.exit()

    monkeypatch.setattr(patch_mod, "write_source", tracked_write_source)
    monkeypatch.setattr(tools_mod, "_read", tracked_read)

    model = ScriptedModel(
        [
            [
                delta(
                    "call_1",
                    0,
                    "apply_patch",
                    edits=[{"path": "f.txt", "old_text": "before", "new_text": "after"}],
                ),
                delta("call_2", 1, "read_file", path="f.txt"),
            ],
            "done",
        ]
    )
    agent = make_agent(model, tmp_path)

    result = await agent.run("patch then read")

    assert result.stop_reason == "completed"
    assert tracker.peak == 1, "the read and the write held the file's critical section at once"


# ---------------------------------------------------------------------------
# F08-03  a stateful command running next to anything else
# ---------------------------------------------------------------------------


async def test_F08_03_two_shell_calls_in_one_turn_never_overlap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`run_shell` is STATEFUL unconditionally, which is a blunter answer than
    "detect that `cd` mutates state" -- and a correct one, since a command
    running concurrently with another can corrupt more than the working
    directory this test-set knows how to check (arbitrary files, network,
    processes). What is checked here is only the part that is checkable:
    two shell calls issued in one turn are never both mid-flight."""
    import minicodex.shell as shell_mod

    tracker = PeakTracker()
    original_run = shell_mod.ShellSession.run

    async def tracked_run(self: Any, command: str) -> str:
        tracker.enter()
        try:
            await asyncio.sleep(0.03)
            return await original_run(self, command)
        finally:
            tracker.exit()

    monkeypatch.setattr(shell_mod.ShellSession, "run", tracked_run)

    model = ScriptedModel(
        [
            [
                delta("call_1", 0, "run_shell", command="echo one"),
                delta("call_2", 1, "run_shell", command="echo two"),
            ],
            "done",
        ]
    )
    agent = make_agent(model, tmp_path)

    result = await agent.run("run both")

    assert result.stop_reason == "completed"
    assert tracker.peak == 1


# ---------------------------------------------------------------------------
# F08-04  one call raising must not lose or stall its batch siblings
# ---------------------------------------------------------------------------


async def test_F08_04_a_raising_call_does_not_cancel_its_concurrent_siblings(
    tmp_path: Path,
) -> None:
    """`_run_tool` already turns every ordinary exception into a returned
    string (chapter 0, F00-05) -- which means the coroutine `asyncio.gather()`
    awaits here never raises Exception for an ordinary tool failure, and
    gather's fail-fast/cancel-siblings behaviour, which only ever triggers on
    a raised exception, simply never sees one. Concurrency does not reopen
    F00-05; this pins that down rather than assuming it."""
    (tmp_path / "a.txt").write_text("a")
    (tmp_path / "b.txt").write_text("b")

    async def explode(args: dict[str, Any]) -> str:
        raise ValueError("disk on fire")

    model = ScriptedModel(
        [
            [
                delta("call_1", 0, "read_file", path="a.txt"),
                delta("call_2", 1, "explode"),
                delta("call_3", 2, "read_file", path="b.txt"),
            ],
            "done",
        ]
    )
    session = Session(mode="workspace-write")
    tools = default_tools(root=tmp_path, session=session)
    tools["explode"] = explode
    agent = Agent(model, tools, footprint_of=functools.partial(footprint_of, root=tmp_path))

    result = await agent.run("go")

    outputs = {i.call_id: i.content for i in result.history.items if isinstance(i, ToolResult)}
    assert outputs["call_1"] == "a"
    assert outputs["call_3"] == "b"
    assert "ValueError" in outputs["call_2"]
    assert "disk on fire" in outputs["call_2"]


# ---------------------------------------------------------------------------
# F08-05  completion order must not become the order the model reads results in
# ---------------------------------------------------------------------------


async def test_F08_05_history_order_follows_submission_not_completion(tmp_path: Path) -> None:
    """call_2 is made to finish before call_1. If the loop appended results as
    they completed, the transcript would read call_2 then call_1 -- correct by
    chapter 1's rules (matched by call_id, not position) but a second,
    needless source of nondeterminism in what gets written to disk."""
    (tmp_path / "a.txt").write_text("a")
    (tmp_path / "b.txt").write_text("b")

    order: list[str] = []

    async def tracked_read(root: Path, args: dict[str, Any]) -> str:
        # call_1 (a.txt) waits; call_2 (b.txt) finishes immediately and wakes it.
        if args["path"] == "a.txt":
            await release.wait()
        order.append(args["path"])
        return args["path"]

    release = asyncio.Event()

    async def read_a(args: dict[str, Any]) -> str:
        return await tracked_read(tmp_path, {"path": "a.txt"})

    async def read_b(args: dict[str, Any]) -> str:
        result = await tracked_read(tmp_path, {"path": "b.txt"})
        release.set()
        return result

    model = ScriptedModel([[delta("call_1", 0, "read_a"), delta("call_2", 1, "read_b")], "done"])
    agent = Agent(
        model,
        {"read_a": read_a, "read_b": read_b},
        footprint_of=lambda c: Footprint(reads=frozenset({c.name})),
    )

    result = await agent.run("go")

    assert order == ["b.txt", "a.txt"], "call_2 must finish first for this test to mean anything"
    tool_results = [i for i in result.history.items if isinstance(i, ToolResult)]
    assert [r.call_id for r in tool_results] == ["call_1", "call_2"]


# ---------------------------------------------------------------------------
# F08-06  implicit ordering the model assumes but the scheduler does not know
# ---------------------------------------------------------------------------


async def test_F08_06_unrelated_files_really_are_independent(tmp_path: Path) -> None:
    """NOT REPRODUCED, and not reproducible with this tool set: F08-06 asks
    what happens when two calls the scheduler thinks are independent actually
    have a hidden dependency. For that to happen here, some tool's declared
    footprint would have to be wrong -- name a resource it does not really
    touch, or omit one it does. `read_file` and `apply_patch` both resolve a
    real path and declare exactly that path; there is no way to construct two
    calls to different, already-existing files where editing one changes the
    other. The fault becomes live the day a tool is added whose footprint is
    a guess rather than a resolved path -- an MCP tool in chapter 9 is exactly
    that shape."""
    (tmp_path / "a.txt").write_text("a")
    (tmp_path / "b.txt").write_text("original-b")

    model = ScriptedModel(
        [
            [
                delta(
                    "call_1",
                    0,
                    "apply_patch",
                    edits=[{"path": "a.txt", "old_text": "a", "new_text": "changed-a"}],
                ),
                delta("call_2", 1, "read_file", path="b.txt"),
            ],
            "done",
        ]
    )
    agent = make_agent(model, tmp_path)

    result = await agent.run("go")

    outputs = {i.call_id: i.content for i in result.history.items if isinstance(i, ToolResult)}
    assert outputs["call_2"] == "original-b"
    assert (tmp_path / "a.txt").read_text() == "changed-a"


# ---------------------------------------------------------------------------
# F08-07  unbounded concurrency
# ---------------------------------------------------------------------------


async def test_F08_07_without_a_cap_every_call_in_a_batch_runs_at_once(tmp_path: Path) -> None:
    tracker = PeakTracker()
    n = 6

    async def slow_probe(args: dict[str, Any]) -> str:
        tracker.enter()
        try:
            await asyncio.sleep(0.03)
            return "ok"
        finally:
            tracker.exit()

    calls = [delta(f"c{i}", i, "probe", n=i) for i in range(n)]
    model = ScriptedModel([calls, "done"])
    agent = Agent(
        model,
        {"probe": slow_probe},
        footprint_of=lambda c: Footprint(reads=frozenset({c.call_id})),
        max_concurrent_tools=n,  # explicitly wide open, for this measurement
    )

    await agent.run("go")

    assert tracker.peak == n


async def test_F08_07_the_default_cap_bounds_concurrency(tmp_path: Path) -> None:
    tracker = PeakTracker()
    n, cap = 6, 2

    async def slow_probe(args: dict[str, Any]) -> str:
        tracker.enter()
        try:
            await asyncio.sleep(0.03)
            return "ok"
        finally:
            tracker.exit()

    calls = [delta(f"c{i}", i, "probe", n=i) for i in range(n)]
    model = ScriptedModel([calls, "done"])
    agent = Agent(
        model,
        {"probe": slow_probe},
        footprint_of=lambda c: Footprint(reads=frozenset({c.call_id})),
        max_concurrent_tools=cap,
    )

    await agent.run("go")

    assert tracker.peak == cap


# ---------------------------------------------------------------------------
# F08-08  cancellation must reach every task, in every batch
# ---------------------------------------------------------------------------


async def test_F08_08_cancelling_mid_batch_answers_that_batch_and_every_later_one(
    tmp_path: Path,
) -> None:
    """Three calls, forced into three separate batches by STATEFUL: the first
    is cancelled while in flight, the second and third never start. All three
    must come back answered -- chapter 7's invariant does not get a
    concurrency-shaped exception."""
    started = asyncio.Event()

    async def slow(args: dict[str, Any]) -> str:
        started.set()
        await asyncio.sleep(60)
        return "never"

    async def never_reached(args: dict[str, Any]) -> str:
        raise AssertionError("a later batch must not start once the run is cancelled")

    model = ScriptedModel(
        [[delta("c1", 0, "slow"), delta("c2", 1, "later"), delta("c3", 2, "later")], "done"]
    )
    agent = Agent(model, {"slow": slow, "later": never_reached})
    task = asyncio.ensure_future(agent.run("go"))
    await started.wait()
    task.cancel()
    result = await task

    assert result.stop_reason == "interrupted"
    assert result.history.unanswered() == ()
    outputs = {i.call_id: i.content for i in result.history.items if isinstance(i, ToolResult)}
    assert "finished" in outputs["c1"]
    assert "before this started" in outputs["c2"]
    assert "before this started" in outputs["c3"]
    assert result.history.to_wire()


async def test_F08_08_a_concurrent_batch_mate_that_already_finished_keeps_its_real_result(
    tmp_path: Path,
) -> None:
    """The call that shares a batch with the one being cancelled, but finishes
    on its own before the cancellation reaches it, must report what it
    actually did -- not get overwritten with an interrupted message it did
    not earn."""
    (tmp_path / "a.txt").write_text("real content")
    started = asyncio.Event()

    async def slow(args: dict[str, Any]) -> str:
        started.set()
        await asyncio.sleep(60)
        return "never"

    async def fast(args: dict[str, Any]) -> str:
        return "finished before the cancel arrived"

    model = ScriptedModel([[delta("c1", 0, "slow"), delta("c2", 1, "fast")], "done"])
    agent = Agent(
        model,
        {"slow": slow, "fast": fast},
        footprint_of=lambda c: Footprint(reads=frozenset({c.name})),  # same batch
    )
    task = asyncio.ensure_future(agent.run("go"))
    await started.wait()
    await asyncio.sleep(0.01)  # give `fast` a chance to actually complete first
    task.cancel()
    result = await task

    outputs = {i.call_id: i.content for i in result.history.items if isinstance(i, ToolResult)}
    assert outputs["c2"] == "finished before the cancel arrived"
    assert "interrupted" in outputs["c1"]
