"""Chapter 10: an agent inside a tool call.

Nothing here touches the network. The child agents are real `Agent`s driven by
a scripted model, the rollout files are real files, and the shell sessions are
real shell sessions -- the only fake thing is the model's judgement.

Test names carry the fault IDs from FAULTS.md.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from minicodex.agent import Agent, Wiring
from minicodex.agent_types import ToolCall
from minicodex.approval import AllowAll, ApprovalReply, ApprovalRequest, Session
from minicodex.clip import DEFAULT_LIMIT, clip
from minicodex.composition import sub_context
from minicodex.model import Completed, TextDelta, ToolCallDelta
from minicodex.rollout import (
    RolloutError,
    RolloutWriter,
    SessionMeta,
    read_rollout,
    resolve,
    rollout_path,
)
from minicodex.scheduler import STATEFUL, batches, conflicts
from minicodex.shell import ShellSession
from minicodex.subagent import (
    MAX_DEPTH,
    MAX_TASK_RESULT_CHARS,
    SubAgentContext,
    TaskResult,
    TaskSpec,
    child_tools,
    run_task,
    spawn_agent,
    spawn_toolset,
)
from minicodex.tools import footprint_of

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


class ScriptedModel:
    """Replays a fixed list of turns and remembers what it was sent.

    `sent` is the point of the class here: most of this chapter is about what
    the child's request contains, which is not observable from its answer.
    """

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
        yield Completed("stop")


def context_for(
    root: Path,
    model: Any,
    *,
    session: Session | None = None,
    shell: ShellSession | None = None,
    **kwargs: Any,
) -> SubAgentContext:
    real = session or Session(mode="workspace-write", approver=AllowAll())
    # `sub_context` rather than `SubAgentContext(...)`: interlude B's whole
    # point is that a child is wired by the same code that wires its parent,
    # and a test helper that builds one by hand is a second assembly site
    # again -- with the difference that this one would go green.
    return sub_context(
        build_model=lambda _schemas: model,
        root=root,
        session=real,
        parent_shell=shell or ShellSession(),
        wiring=kwargs.pop("wiring", Wiring()),
        **kwargs,
    )


def _sleep_schema() -> dict[str, Any]:
    return {
        "type": "function",
        "function": {"name": "sleep", "description": "block", "parameters": {"type": "object"}},
    }


def a_call(name: str, **arguments: Any) -> ToolCall:
    return ToolCall("call_1", name, arguments, json.dumps(arguments))


# ---------------------------------------------------------------------------
# F10-01  state the parent never wrote down
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_F10_01_a_child_starts_where_the_parent_is_standing(tmp_path: Path) -> None:
    """`cd src` lives in a Python attribute, not on disk.

    Measured with the real tools: parent `cd src`, parent `pwd` -> .../src,
    child `pwd` -> the repository root. No error anywhere.
    """
    (tmp_path / "src").mkdir()
    parent_shell = ShellSession()
    parent_shell.cwd = str(tmp_path)
    await parent_shell.run("cd src")
    assert parent_shell.cwd == str(tmp_path / "src")

    ctx = context_for(tmp_path, ScriptedModel(["done"]), shell=parent_shell)
    handlers = child_tools(ctx).handlers
    # The handler closes over the child's shell; the only way to ask it where
    # it is standing is to run something.
    where = await handlers["run_shell"]({"command": "pwd"})
    # Compared by tail, not by `Path.resolve()`: the shell here is msys bash,
    # which answers `/tmp/...` for a path Python calls `C:\Users\...\Temp\...`.
    # Resolving that string on the Python side produces `D:\tmp\...`, which is
    # a different, non-existent directory. The two runtimes do not share a
    # spelling for the same place -- F02-10, one more time.
    assert where.strip().replace("\\", "/").endswith("/src")


@pytest.mark.asyncio
async def test_F10_01_a_child_shares_the_permissions_the_parent_was_granted(
    tmp_path: Path,
) -> None:
    """A child built with `default_tools(root)` gets a fresh, read-only `Session`.

    Sharing the object rather than copying its fields, so that a permission
    granted after the child was built is granted to the child too -- and, more
    importantly, so that a permission granted *to* a child is visible to the
    parent afterwards rather than being lost with the child's tool table.
    """
    # The default approver is `DenyAll`, which is the point: read-only means
    # denied unless somebody says otherwise.
    session = Session(mode="read-only")
    ctx = context_for(tmp_path, ScriptedModel(["done"]), session=session)
    handlers = child_tools(ctx).handlers

    denied = await handlers["apply_patch"]({"edits": []})
    assert "Permission denied" in denied

    session.mode = "workspace-write"
    allowed = await handlers["apply_patch"]({"edits": []})
    assert "Permission denied" not in allowed


# ---------------------------------------------------------------------------
# F10-02  two children, one file
# ---------------------------------------------------------------------------


def test_F10_02_a_spawn_is_stateful_without_anyone_saying_so(tmp_path: Path) -> None:
    """Chapter 8's default already covers a tool it has never heard of.

    Worth a test rather than a shrug: the measured alternative -- classifying
    a spawn as "it only reads" -- loses one of two edits 30 times out of 30.
    """
    assert footprint_of(a_call("spawn_agent", task="x"), root=tmp_path) == STATEFUL
    assert conflicts(STATEFUL, STATEFUL)


def test_F10_02_two_spawns_land_in_different_batches(tmp_path: Path) -> None:
    calls = [
        ToolCall("c1", "spawn_agent", {"task": "a"}, "{}"),
        ToolCall("c2", "spawn_agent", {"task": "b"}, "{}"),
    ]
    plan = batches(calls, lambda call: footprint_of(call, root=tmp_path))
    assert [[c.call_id for c in batch] for batch in plan] == [["c1"], ["c2"]]


# ---------------------------------------------------------------------------
# F10-03  the contract, not the history
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_F10_03_the_child_never_sees_the_parents_conversation(tmp_path: Path) -> None:
    model = ScriptedModel(["found it"])
    ctx = context_for(tmp_path, model)
    await run_task(TaskSpec(task="count the files"), ctx)

    sent = model.sent[0]
    assert [m["role"] for m in sent] == ["system", "user"]
    assert sent[-1]["content"] == "count the files"
    # The child's system message is its own, not the parent's.
    assert "coding agent working in a user's repository" not in sent[0]["content"]


@pytest.mark.asyncio
async def test_F10_03_the_childs_own_system_note_is_short(tmp_path: Path) -> None:
    """A bound, not a preference: the whole point is that this does not grow
    into a second copy of the parent's prompt."""
    model = ScriptedModel(["ok"])
    ctx = context_for(tmp_path, model)
    await run_task(TaskSpec(task="t"), ctx)
    assert len(model.sent[0][0]["content"]) < 500


# ---------------------------------------------------------------------------
# F10-04  constraints
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_F10_04_constraints_arrive_as_a_system_note_not_in_the_task(
    tmp_path: Path,
) -> None:
    """Where the constraint goes is the measurement.

    Appended to the task text, 3 and 4 of 5 runs broke it anyway; as a system
    note, 0 of 5. This test pins the placement, which is the part code can
    guarantee -- the obedience is the model's and is measured in the probe.
    """
    model = ScriptedModel(["ok"])
    ctx = context_for(tmp_path, model)
    await run_task(TaskSpec(task="find killpg", constraints=("do not use the shell",)), ctx)

    system, user = model.sent[0]
    assert "do not use the shell" in system["content"]
    assert "do not use the shell" not in user["content"]


def test_F10_04_a_spec_with_no_constraints_still_says_there_is_no_user() -> None:
    text = TaskSpec(task="t").instructions()
    assert "cannot answer questions" in text


# ---------------------------------------------------------------------------
# F10-05  how much comes back
# ---------------------------------------------------------------------------


def test_F10_05_a_long_answer_is_clipped_at_the_boundary() -> None:
    """The instruction is not a bound. Measured: the same "three sentences"
    instruction produced 603, 615, 661, 1468 and 2711 characters."""
    rendered = TaskResult("ok", "x" * 10_000, 3).render()
    assert len(rendered) < MAX_TASK_RESULT_CHARS + 100
    assert "characters omitted" in rendered


def test_F10_05_a_short_answer_is_returned_untouched() -> None:
    assert TaskResult("ok", "42", 1).render() == "42"


@pytest.mark.asyncio
async def test_F10_05_expected_output_reaches_the_child(tmp_path: Path) -> None:
    model = ScriptedModel(["ok"])
    ctx = context_for(tmp_path, model)
    await run_task(TaskSpec(task="t", expected_output="one line, path:line"), ctx)
    assert "one line, path:line" in model.sent[0][0]["content"]


# ---------------------------------------------------------------------------
# F10-06  depth
# ---------------------------------------------------------------------------


def test_F10_06_the_bottom_level_is_not_given_the_tool(tmp_path: Path) -> None:
    """Not "given it and refused". Chapter 5 measured what happens when a
    prompt names a tool the policy will not allow: the model calls it."""
    top = child_tools(context_for(tmp_path, ScriptedModel(["ok"]), depth=0, max_depth=2))
    assert "spawn_agent" in top.handlers
    assert any(s["function"]["name"] == "spawn_agent" for s in top.schemas)

    bottom = child_tools(context_for(tmp_path, ScriptedModel(["ok"]), depth=1, max_depth=2))
    assert "spawn_agent" not in bottom.handlers
    assert not any(s["function"]["name"] == "spawn_agent" for s in bottom.schemas)


@pytest.mark.asyncio
async def test_F10_06_a_spawn_past_the_limit_refuses_without_running_anything(
    tmp_path: Path,
) -> None:
    model = ScriptedModel(["should never be asked"])
    ctx = context_for(tmp_path, model, depth=MAX_DEPTH, max_depth=MAX_DEPTH)
    result = await run_task(TaskSpec(task="go deeper"), ctx)
    assert result.outcome == "depth_limit"
    assert model.sent == []
    assert "yourself" in result.render()


@pytest.mark.asyncio
async def test_F10_06_a_model_that_only_ever_spawns_stops_at_the_limit(tmp_path: Path) -> None:
    """The unbounded version reached 830,400 levels in 59 seconds.

    Counting model calls rather than depth, because the depth is what the code
    under test decides and the number of conversations is what it costs.
    """
    spawning = [[("c", "spawn_agent", {"task": "again", "expected_output": "x"})]]
    calls = 0

    class CountingModel(ScriptedModel):
        async def stream(self, messages: Sequence[dict[str, Any]]) -> Any:
            nonlocal calls
            calls += 1
            async for event in super().stream(messages):
                yield event

    ctx = context_for(tmp_path, CountingModel(spawning), depth=0, max_depth=2)
    result = await run_task(TaskSpec(task="start"), ctx)

    # 32, and the number is the whole point of this test. The depth-1 child
    # spends its own 8 turns; each of those turns spawns a depth-2 child that
    # spends 8 more. With the depth limit alone that was 8 + 8 * 8 = **72**
    # model calls for one task -- bounded depth, unbounded width. The shared
    # budget stops the fourth depth-2 child: 8 + 3 * 8 = 32.
    assert calls == 32, calls
    assert result.outcome in {"ok", "turn_limit"}
    assert any(child.outcome != "ok" for child in ctx.children) or calls == 32


@pytest.mark.asyncio
async def test_F10_06_the_shared_budget_refuses_without_calling_the_model(
    tmp_path: Path,
) -> None:
    """The budget is spent by the whole run, not by one branch of it."""
    model = ScriptedModel(["never asked"])
    ctx = context_for(tmp_path, model, child_turn_budget=10)
    ctx.children.append(TaskResult("ok", "earlier work", turns=10))

    result = await run_task(TaskSpec(task="one more"), ctx)
    assert result.outcome == "budget"
    assert model.sent == []
    assert "yourself" in result.render()


# ---------------------------------------------------------------------------
# F10-07  a child that never comes back
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_F10_07_a_hanging_child_is_stopped_and_says_so(tmp_path: Path) -> None:
    async def forever(args: dict[str, Any]) -> str:
        await asyncio.sleep(3600)
        return "never"

    model = ScriptedModel([[("c", "sleep", {})], "done"])
    ctx = context_for(tmp_path, model, timeout=0.3)
    began = time.monotonic()
    # Patching the tool table is the only way in: `child_tools` builds the real
    # one, and a sleeping real tool would mean a real subprocess.
    original = child_tools

    def patched(c: SubAgentContext) -> Any:
        # A schema as well as a handler, because `ToolSet` refuses the pair
        # when they disagree -- including when the disagreement is a test
        # taking a shortcut. Interlude B's invariant found this on its first
        # run: the fake tool went in as a handler alone and was rejected.
        tools = original(c)
        return replace(
            tools,
            handlers={**tools.handlers, "sleep": forever},
            schemas=[*tools.schemas, _sleep_schema()],
        )

    import minicodex.subagent as subagent

    subagent.child_tools = patched  # type: ignore[assignment]
    try:
        result = await run_task(TaskSpec(task="hang"), ctx)
    finally:
        subagent.child_tools = original  # type: ignore[assignment]

    assert result.outcome == "timeout"
    assert time.monotonic() - began < 3
    rendered = result.render()
    assert "still running" in rendered
    assert "Do not report this as a finding" in rendered


@pytest.mark.asyncio
async def test_F10_07_a_bare_wait_for_would_have_returned_an_empty_answer(
    tmp_path: Path,
) -> None:
    """Why `run_task` shields the child instead of wrapping it.

    `Agent.run` catches `CancelledError` and returns a normal `RunResult`
    (chapter 7: every issued call is answered before unwinding). So
    `wait_for(child.run(...))` cancels the child and *returns its value* --
    no `TimeoutError` is raised at all, and the timeout silently becomes an
    empty answer. This test pins the behaviour that forces the design; if it
    ever changes, the shield can go.
    """

    async def forever(args: dict[str, Any]) -> str:
        await asyncio.sleep(3600)
        return "never"

    model = ScriptedModel([[("c", "sleep", {})], "done"])
    child = Agent(model, {"sleep": forever})
    result = await asyncio.wait_for(child.run("hang"), timeout=0.3)

    assert result.stop_reason == "interrupted"
    assert result.final_text == ""


@pytest.mark.asyncio
async def test_F10_07_cancelling_the_parent_does_not_leave_the_child_running(
    tmp_path: Path,
) -> None:
    """The shield protects the child from the parent's Ctrl-C too, so the
    cancellation has to be passed on by hand."""
    running = asyncio.Event()
    finished = asyncio.Event()

    async def slow(args: dict[str, Any]) -> str:
        running.set()
        try:
            await asyncio.sleep(3600)
        finally:
            finished.set()
        return "never"

    model = ScriptedModel([[("c", "sleep", {})], "done"])
    ctx = context_for(tmp_path, model, timeout=60)
    original = child_tools

    def patched(c: SubAgentContext) -> Any:
        # A schema as well as a handler, because `ToolSet` refuses the pair
        # when they disagree -- including when the disagreement is a test
        # taking a shortcut. Interlude B's invariant found this on its first
        # run: the fake tool went in as a handler alone and was rejected.
        tools = original(c)
        return replace(
            tools,
            handlers={**tools.handlers, "sleep": slow},
            schemas=[*tools.schemas, _sleep_schema()],
        )

    import minicodex.subagent as subagent

    subagent.child_tools = patched  # type: ignore[assignment]
    try:
        task = asyncio.ensure_future(run_task(TaskSpec(task="t"), ctx))
        await running.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        subagent.child_tools = original  # type: ignore[assignment]

    assert finished.is_set(), "the child's tool was never unwound"


# ---------------------------------------------------------------------------
# F10-09  who answers a child's approval prompt
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_F10_09_a_childs_approval_request_reaches_the_parents_approver(
    tmp_path: Path,
) -> None:
    """There is one human and one terminal. The child's `Session` is the
    parent's `Session`, so a prompt raised inside a sub-agent is answered in
    the same place as every other prompt."""
    seen: list[ApprovalRequest] = []

    class Recording:
        async def ask(self, request: ApprovalRequest) -> ApprovalReply:
            seen.append(request)
            return ApprovalReply(False, request.what)

    session = Session(mode="read-only", policy="on-request", approver=Recording())
    ctx = context_for(tmp_path, ScriptedModel(["ok"]), session=session)
    handlers = child_tools(ctx).handlers
    output = await handlers["run_shell"]({"command": "rm -rf build"})

    assert len(seen) == 1
    assert "rm -rf build" in seen[0].what
    assert "Permission denied" in output


# ---------------------------------------------------------------------------
# F10-10  an outcome, not a string
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_F10_10_a_child_that_ran_out_of_turns_does_not_look_finished(
    tmp_path: Path,
) -> None:
    """Measured: a one-turn child returns `''` and `stop_reason='turn_limit'`,
    and so does an interrupted one. `return result.final_text` maps three
    different endings onto one string, two of them onto the same string."""
    model = ScriptedModel([[("c", "read_file", {"path": "nope.py"})]])
    ctx = context_for(tmp_path, model, max_turns=1)
    result = await run_task(TaskSpec(task="t"), ctx)

    assert result.outcome == "turn_limit"
    rendered = result.render()
    assert "ran out of turns" in rendered
    assert "Do not report this as a finding" in rendered


@pytest.mark.asyncio
async def test_F10_10_an_empty_answer_is_not_a_success(tmp_path: Path) -> None:
    """Not "a very short answer". F06-08 made the same call about an empty
    summary and F09-07 about an empty MCP result."""
    model = ScriptedModel([""])
    ctx = context_for(tmp_path, model)
    result = await run_task(TaskSpec(task="t"), ctx)
    assert result.outcome == "empty"
    assert not result.ok
    assert "no answer at all" in result.render()


def test_F10_10_every_outcome_says_what_to_do_next() -> None:
    """The rule chapter 3 measured (F03-07), applied to a failure that is a
    whole conversation rather than one call."""
    for outcome in ("empty", "turn_limit", "timeout", "interrupted", "depth_limit", "budget"):
        rendered = TaskResult(outcome, "partial words", 2, 1.0).render()  # type: ignore[arg-type]
        assert rendered.startswith("[sub-agent:")
        assert rendered.rstrip().endswith((".", "!"))


def test_F10_10_a_successful_result_carries_no_header() -> None:
    """A header on every result is a header the model learns to skip."""
    assert not TaskResult("ok", "the answer", 2).render().startswith("[sub-agent:")


# ---------------------------------------------------------------------------
# F10-12  parent and child transcripts
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_F10_12_a_child_writes_its_own_file_naming_its_parent(tmp_path: Path) -> None:
    """The parent's writer is open for the whole test, which is the point.

    The first version of this built no parent writer at all and asserted "one
    file exists, and its `parent` field says parent-1". Both of those are still
    true when the child writes *into the parent's own file* -- the mutation
    that does exactly that left this test green. A test that does not
    reconstruct the situation cannot see the bug in it.
    """
    sessions = tmp_path / "sessions"
    parent_meta = SessionMeta(session_id="parent-1", created=time.time())
    parent_writer = RolloutWriter(rollout_path("parent-1", sessions), parent_meta)
    try:
        ctx = context_for(
            tmp_path,
            ScriptedModel(["done"]),
            sessions_dir=sessions,
            parent_session_id="parent-1",
        )
        result = await run_task(TaskSpec(task="t"), ctx)

        written = sorted(p.name for p in sessions.glob("*.jsonl"))
        assert len(written) == 2, written
        assert result.session_id != "parent-1"
        loaded = read_rollout(rollout_path(result.session_id, sessions))
        assert loaded.meta.parent == "parent-1"
        # The parent's file has its header and nothing else: the child wrote
        # none of its own turns into it.
        assert len(read_rollout(parent_writer.path).items) == 0
        assert sorted(p.name for p in sessions.glob("*.lock")) == ["parent-1.jsonl.lock"], (
            "the child's lock outlived the child"
        )
    finally:
        parent_writer.release()


def test_F10_12_sharing_the_parents_file_is_refused(tmp_path: Path) -> None:
    """Chapter 7's single-writer lock is why a child cannot simply append to
    its parent's rollout: the naive version does not interleave, it raises."""
    meta = SessionMeta(session_id="s1", created=time.time())
    path = rollout_path(meta.session_id, tmp_path)
    parent = RolloutWriter(path, meta)
    try:
        with pytest.raises(RolloutError, match="already open"):
            RolloutWriter(path, meta)
    finally:
        parent.release()


def test_F10_12_an_old_session_file_still_loads(tmp_path: Path) -> None:
    """`parent` was added without bumping `ROLLOUT_VERSION`, so this has to be
    true: additive is not breaking."""
    path = tmp_path / "old.jsonl"
    path.write_text(
        json.dumps({"type": "meta", "session_id": "old", "version": 2, "cwd": "/x"}) + "\n",
        encoding="utf-8",
    )
    loaded = read_rollout(path)
    assert loaded.meta.session_id == "old"
    assert loaded.meta.parent is None


# ---------------------------------------------------------------------------
# the tool itself
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bad_arguments_are_answered_not_raised(tmp_path: Path) -> None:
    ctx = context_for(tmp_path, ScriptedModel(["ok"]))
    assert "task" in await spawn_agent(ctx, {})
    assert "list of strings" in await spawn_agent(ctx, {"task": "t", "constraints": "no"})


def test_the_schema_and_the_handler_come_from_one_object(tmp_path: Path) -> None:
    """Chapter 4's rule, for a tool declared outside `tool_specs()`.

    Interlude B made this structural rather than conventional: `ToolSet`
    refuses to be constructed at all when the two disagree, so the assertion
    below is now about the *value*, not about a promise in a docstring.
    """
    ctx = context_for(tmp_path, ScriptedModel(["ok"]))
    tools = spawn_toolset(ctx)
    assert set(tools.handlers) == {s["function"]["name"] for s in tools.schemas}
    assert callable(tools.handlers["spawn_agent"])


# ---------------------------------------------------------------------------
# the extraction that made this chapter's third clipper unnecessary
# ---------------------------------------------------------------------------


def test_clip_keeps_both_ends() -> None:
    text = "START" + "x" * 100 + "END"
    clipped = clip(text, 40)
    assert clipped.startswith("START")
    assert clipped.endswith("END")
    assert "characters omitted" in clipped


def test_clip_leaves_short_text_alone() -> None:
    assert clip("short", 40) == "short"


def test_clip_never_grows_the_text() -> None:
    """A clipper that adds more marker than it removes is worse than none.
    Chapter 6 shipped a compaction that made a history *bigger* (seed 235)."""
    for limit in (40, 200, DEFAULT_LIMIT):
        text = "y" * (limit * 3)
        assert len(clip(text, limit)) < len(text)


def test_F10_12_resume_last_skips_sub_agent_sessions(tmp_path: Path) -> None:
    """A run that spawns two sub-agents leaves three files, and the two newest
    are the children. `--resume last` silently became "continue the last thing
    a sub-agent said" -- a working-looking command on the wrong conversation.
    Nothing reported it; it was noticed in the output of `minicodex sessions`.
    """
    for session_id, parent in [("a", None), ("b", "a"), ("c", "a")]:
        meta = SessionMeta(session_id=session_id, created=time.time(), parent=parent)
        RolloutWriter(rollout_path(session_id, tmp_path), meta).release()

    assert resolve("last", tmp_path).stem == "a"
    # Still reachable by id: excluded from the guess, not from the program.
    assert resolve("c", tmp_path).stem == "c"
