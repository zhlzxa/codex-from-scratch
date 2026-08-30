"""Chapter 14 -- evaluation and regression testing.

Every test here is offline.  That is not a convenience: F14-05 is the entry
that says a suite which needs a provider is a suite people switch off, and a
chapter about testing that shipped tests needing an API key would be arguing
against itself.  The real-model measurements live in `probe_eval.py` and run
nightly, where nobody is waiting for them.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any, ClassVar

import pytest

from minicodex import stub
from minicodex.agent import COMPACT_AT, Wiring
from minicodex.agent_types import ToolCall, ToolSet
from minicodex.approval import AllowAll, Session
from minicodex.evals import (
    TASKS,
    Arm,
    Result,
    Task,
    Trajectory,
    answer_has,
    by_name,
    calls,
    declared_files,
    file_has,
    regressions,
    run_task,
)
from minicodex.history import History
from minicodex.replay import (
    RecordedModel,
    ReplayDrift,
    ReplayError,
    call_record,
    divergence,
    failure_record,
    load,
    recorded_tools,
)
from minicodex.retry import ModelFailed, RetryPolicy
from minicodex.tools import tool_context

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _event(kind: str, payload: dict[str, Any], seq: int = 0) -> str:
    return json.dumps({"seq": seq, "ts": 0.0, "kind": kind, "payload": payload})


def _recording(path: Path, *events: str) -> Path:
    path.write_text("\n".join(events) + "\n", encoding="utf-8")
    return path


def _trajectory(**kwargs: Any) -> Trajectory:
    base: dict[str, Any] = {
        "calls": (),
        "final_text": "",
        "stop_reason": "completed",
        "turns": 1,
        "files": {},
    }
    base.update(kwargs)
    return Trajectory(**base)


def _result(task: Task, ok: bool) -> Result:
    trajectory = _trajectory()
    return Result(task, trajectory, ("check",) if ok else (), () if ok else ("check",))


# ---------------------------------------------------------------------------
# F14-01  assert the trajectory, not the words
# ---------------------------------------------------------------------------


def test_F14_01_a_text_check_and_a_file_check_disagree_about_the_same_run() -> None:
    """Two runs that did the identical thing and said it differently.

    Both wrote `def subtract` to the file.  One narrated it as "I have added
    the subtract function", the other as "Done." -- and the check on the words
    calls the second one a failure.  Measured against gpt-4o-mini in
    `probe_eval.py trajectory`; the phrasings below are two of the answers it
    actually produced.
    """
    wordy = _trajectory(
        final_text="I have added the `subtract` function to `calc.py`.",
        files={"calc.py": "def subtract(a, b):\n    return a - b\n"},
    )
    terse = _trajectory(
        final_text="Done.",
        files={"calc.py": "def subtract(a, b):\n    return a - b\n"},
    )

    on_disk = file_has("calc.py", "def subtract")
    in_prose = answer_has("I have added")

    assert on_disk.holds(wordy) and on_disk.holds(terse)
    assert in_prose.holds(wordy)
    assert not in_prose.holds(terse)


def test_F14_01_a_trajectory_check_names_the_tool_not_the_sentence() -> None:
    """`calls("apply_patch")` is true of a run that edited and false of one
    that only talked about editing -- which is the distinction the answer text
    cannot make, because a model that failed to edit will still describe the
    edit it meant to make."""
    edited = _trajectory(calls=(("apply_patch", {"edits": []}),))
    talked = _trajectory(final_text="I will add a subtract function to calc.py.")
    assert calls("apply_patch").holds(edited)
    assert not calls("apply_patch").holds(talked)


# ---------------------------------------------------------------------------
# F14-03  the request body, pinned by something that runs the real wiring
# ---------------------------------------------------------------------------


def test_F14_03_the_cli_sends_a_system_message_at_all(
    tmp_path: Path, stub_url: str, served_requests: list[dict], monkeypatch: Any
) -> None:
    """Deleting the system prompt from the CLI left all 1525 tests green.

    Measured, not imagined: replacing `instructions=_instructions(session,
    tools)` with `instructions=None` in `__main__` -- which switches off
    chapter 5's permission block, chapter 11's plan paragraph and chapter 13's
    whole system prompt in one line -- turned nothing red.  Every test of
    `_instructions` calls `_instructions`; none of them checks that anybody
    calls it.

    The check has to run the composition, so it runs `main()`.
    """
    from minicodex.__main__ import main

    monkeypatch.chdir(tmp_path)
    code = main(["ask", "what does this define?", "--base-url", stub_url, "--yes"])

    assert code == 0
    first = served_requests[0]["messages"]
    assert first[0]["role"] == "system", first[0]
    assert "coding agent" in first[0]["content"]
    # ...and the volatile half is still last, which is the arrangement chapter
    # 13 measured at 94% cached against 0% (F13-07).
    from minicodex.approval import permissions_block

    assert first[0]["content"].endswith(permissions_block(Session(), can_request=True))


async def test_F14_03_compaction_fires_at_the_documented_fraction() -> None:
    """`COMPACT_AT = 0.75` had nothing depending on it.

    Setting it to 0.95 -- a real behaviour change, since it decides when a
    session starts paying for a summary -- left all 1525 tests green.  Chapter
    6 has thirteen faults' worth of tests about *how* a history is cut and not
    one about *when* the cutting starts.

    Two runs on the same history, with the window chosen so that the estimate
    sits either side of the threshold.  The assertion is on both sides on
    purpose: `assert compactions == 1` alone is satisfied by a constant.

    The 0.75 below is a **literal**, and the first version of this test used
    `COMPACT_AT` instead -- which computed both windows from the very constant
    it was checking, so the mutation moved the thresholds and the test with
    them.  It survived, in the test written to catch it.  Chapter 6's rule
    ("a test that reimplements the code under test measures the copy") applies
    to reading a constant just as much as to copying a loop.
    """

    async def summarise(request: Any) -> str:
        return "## Done\nthings happened"

    async def one_run(window: int) -> int:
        history = History()
        history.add_user("start")
        for index in range(12):
            history.add_assistant(f"step {index} " + "x" * 400)
        model = _Answers(["finished"])
        agent = Wiring(context_window=window, summariser=summarise).agent(
            model, ToolSet(handlers={}, schemas=[]), resume_from=history
        )
        return len((await agent.run("carry on")).compactions)

    estimate = _estimate_of()
    assert COMPACT_AT == 0.75, "the two windows below are computed from this number"
    assert await one_run(int(estimate / 0.85)) == 1  # estimate is 85% of the window
    assert await one_run(int(estimate / 0.65)) == 0  # ...and 65% of this one


def _estimate_of() -> int:
    """The size of the history the test above builds, by the program's own ruler."""
    from minicodex.compaction import Sizer

    history = History()
    history.add_user("start")
    for index in range(12):
        history.add_assistant(f"step {index} " + "x" * 400)
    history.add_user("carry on")
    return Sizer().messages(history.to_wire())


class _Answers:
    """A model that says the next thing on its list and asks for no tools."""

    tools: ClassVar[list[dict[str, Any]]] = []

    def __init__(self, turns: list[str]) -> None:
        self.turns = turns
        self.sent: list[Any] = []

    async def stream(self, messages: Any) -> Any:
        from minicodex.model import Completed, TextDelta

        self.sent.append(list(messages))
        yield TextDelta(self.turns[min(len(self.sent) - 1, len(self.turns) - 1)])
        yield Completed("stop")


def test_F14_03_a_recorded_session_replays_with_no_drift(
    tmp_path: Path, stub_url: str, served_requests: list[dict], monkeypatch: Any
) -> None:
    """The whole of chapter 14 in one test: run the CLI, then re-run its own
    recording against the code and require the requests to match.

    It is a snapshot nobody had to write.  A golden-transcript fixture covers
    the wiring somebody remembered to build a fixture for; this covers whatever
    the program did the last time a human ran it.

    Not `async def`, and that is not a style choice: `main()` calls
    `asyncio.run()`, and pytest-asyncio's auto mode would already have a loop
    running.  The replay half gets its own `asyncio.run` below.

    Structural limit, raised in review: this test produces its own input, so it
    can only catch the two sides *disagreeing*, never both being wrong the same
    way.  A replay is a relative check.  The absolute ones are elsewhere and
    are still needed -- `test_F14_03_the_cli_sends_a_system_message_at_all`
    just below, chapter 3's description snapshot, interlude A's golden
    transcript.
    """
    monkeypatch.chdir(tmp_path)
    from minicodex.__main__ import main

    assert main(["ask", "what does this define?", "--base-url", stub_url, "--yes"]) == 0

    recordings = sorted((tmp_path / ".minicodex" / "recordings").glob("*.jsonl"))
    assert len(recordings) == 1
    recording = load(recordings[0])
    assert recording.config is not None
    assert recording.turns == 2

    replayed = recorded_tools(recording)
    tools = ToolSet(
        handlers=replayed.table(recording.config["tools"]),
        schemas=list(recording.config["schemas"]),
    )
    from minicodex.__main__ import _instructions

    session = Session(
        mode=recording.config["sandbox_mode"], policy=recording.config["approval_policy"]
    )
    model = RecordedModel(recording)
    agent = Wiring().agent(model, tools, instructions=_instructions(session, tools))
    result = asyncio.run(agent.run(recording.config["question"]))

    assert result.stop_reason == "completed"
    assert len(model.sent) == 2


async def test_F14_03_drift_names_the_message_that_changed(tmp_path: Path) -> None:
    """One sentence appended to the system prompt, reported as one line."""
    path = _recording(
        tmp_path / "r.jsonl",
        _event("config", {"question": "hi", "tools": [], "schemas": []}),
        _event(
            "request", {"turn": 0, "attempt": 0, "messages": [{"role": "system", "content": "A"}]}
        ),
        _event("response", {"turn": 0, "text": "ok", "finish_reason": "stop", "tool_calls": []}),
    )
    model = RecordedModel(load(path))
    agent = Wiring().agent(model, ToolSet(handlers={}, schemas=[]), instructions="A and B")

    with pytest.raises(ReplayDrift) as caught:
        await agent.run("hi")
    assert "message 0 (system): content changed" in str(caught.value)


def test_F14_03_the_drift_report_shows_the_part_that_differs() -> None:
    """The first version printed the first 90 characters of each side.

    On the first real drift -- a sentence appended to a 400-character system
    prompt -- that printed the *same* 90 characters twice, under the labels
    `was` and `now`.  A diff that shows the identical part is not a diff.
    """
    shared = "You are a coding agent working in a user's repository. " * 3
    report = divergence(
        [{"role": "system", "content": shared + "Always answer in haiku."}],
        [{"role": "system", "content": shared + "Follow the conventions."}],
    )
    assert report is not None
    was, now = [line.strip() for line in report.splitlines()[1:]]
    assert "haiku" in now
    assert "conventions" in was
    assert was != now


def test_F14_03_divergence_reports_a_missing_message_rather_than_comparing_pairs() -> None:
    sent = [{"role": "user", "content": "hi"}]
    recorded = [{"role": "system", "content": "S"}, {"role": "user", "content": "hi"}]
    assert "role was 'system'" in (divergence(sent, recorded) or "")
    assert "missing 'user'" in (divergence(recorded[:1], recorded) or "")


# ---------------------------------------------------------------------------
# F14-03 / F-1-04  the recording has to carry enough to be re-run
# ---------------------------------------------------------------------------


def test_F14_03_a_recording_without_arguments_is_refused_not_guessed(tmp_path: Path) -> None:
    """Chapters -1 to 13 recorded `{"id": ..., "name": "apply_patch"}`.

    Filling the gap with `{}` would replay a conversation in which the model
    asked to patch nothing, and the test built on it would be green about a run
    that never happened.
    """
    path = _recording(
        tmp_path / "old.jsonl",
        _event("request", {"turn": 0, "attempt": 0, "messages": []}),
        _event(
            "response",
            {"turn": 0, "text": "", "tool_calls": [{"id": "c1", "name": "apply_patch"}]},
        ),
    )
    with pytest.raises(ReplayError, match="without its arguments"):
        load(path)


def test_F14_03_a_failure_recorded_as_prose_is_refused(tmp_path: Path) -> None:
    """A 429 stored as `kind` and `detail` cannot be made to happen again."""
    path = _recording(
        tmp_path / "old.jsonl",
        _event("request", {"turn": 0, "attempt": 0, "messages": []}),
        _event(
            "model_failure",
            {
                "turn": 0,
                "attempt": 0,
                "disposition": "retry",
                "kind": "rate_limit",
                "detail": "Rate limit reached for gpt-4o-mini ...",
            },
        ),
    )
    with pytest.raises(ReplayError, match="recorded as prose only"):
        load(path)


def test_F14_03_call_record_keeps_the_bytes_the_model_sent() -> None:
    """`{"path": "x.py"}` and `{"path":"x.py"}` are the same object and not the
    same request.  Chapter 7 bumped a rollout version over this exact
    distinction (F07-09); a recording has the same obligation."""
    call = ToolCall("c1", "read_file", {"path": "x.py"}, '{"path":"x.py"}')
    assert call_record(call)["arguments"] == '{"path":"x.py"}'


def test_F14_03_failure_record_keeps_the_status_and_the_headers() -> None:
    from minicodex.model import ModelHTTPError

    exc = ModelHTTPError(
        status=429,
        url="https://api.openai.com/v1/chat/completions",
        headers={"retry-after": "46"},
        body=json.dumps(stub.RATE_LIMITED["body"]),
    )
    record = failure_record(exc)
    assert record["status"] == 429
    assert record["headers"]["retry-after"] == "46"
    assert "rate_limit_exceeded" in record["body"]


async def test_F14_03_a_recorded_rate_limit_replays_as_a_rate_limit(tmp_path: Path) -> None:
    """The point of recording the evidence: an intermittent failure becomes a
    test that fails the same way every time.

    Two attempts for turn 0 -- a 429 and then the answer -- and the loop is
    required to do what it did on the day: classify, wait, retry, finish.
    """
    messages = [{"role": "user", "content": "hi"}]
    path = _recording(
        tmp_path / "r.jsonl",
        _event("request", {"turn": 0, "attempt": 0, "messages": messages}),
        _event(
            "model_failure",
            {
                "turn": 0,
                "attempt": 0,
                "disposition": "retry",
                "kind": "rate_limit",
                "detail": "rate limited",
                "status": 429,
                "headers": {},
                "body": json.dumps(stub.RATE_LIMITED["body"]),
            },
        ),
        _event("request", {"turn": 0, "attempt": 1, "messages": messages}),
        _event(
            "response",
            {"turn": 0, "text": "done", "finish_reason": "stop", "tool_calls": []},
        ),
    )
    recording = load(path)
    assert len(recording.attempts) == 2

    model = RecordedModel(recording, strict=False)
    agent = Wiring(retry_policy=RetryPolicy(base=0.0)).agent(
        model, ToolSet(handlers={}, schemas=[])
    )
    result = await agent.run("hi")
    assert result.final_text == "done"
    assert len(model.sent) == 2


async def test_F14_03_asking_for_one_more_turn_than_was_recorded_says_so(tmp_path: Path) -> None:
    path = _recording(
        tmp_path / "r.jsonl",
        _event(
            "request",
            {"turn": 0, "attempt": 0, "messages": [{"role": "user", "content": "x"}]},
        ),
        _event(
            "response",
            {
                "turn": 0,
                "text": "",
                "finish_reason": "tool_calls",
                "tool_calls": [{"id": "c1", "name": "noop", "arguments": "{}"}],
            },
        ),
    )
    model = RecordedModel(load(path), strict=False)

    async def noop(_: dict[str, Any]) -> str:
        return "ok"

    tools = ToolSet(
        handlers={"noop": noop},
        schemas=[{"type": "function", "function": {"name": "noop", "parameters": {}}}],
    )
    agent = Wiring().agent(model, tools)
    # It arrives wrapped: `ReplayExhausted` is an ordinary `Exception`, so the
    # loop classifies it (`fatal`, unrecognised) and raises `ModelFailed`.
    # That is the right treatment for this one -- running out of recording is
    # a fact about the request, unlike drift, which is a fact about the code.
    with pytest.raises(ModelFailed) as failure:
        await agent.run("x")
    assert "the recording has 1" in str(failure.value)


def test_F14_03_recorded_tools_answer_from_the_next_request(tmp_path: Path) -> None:
    """A tool result lives in the request after the one that asked for it, so a
    replay needs no workspace at all."""
    first = [{"role": "user", "content": "read a.py"}]
    second = [
        *first,
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "c1",
                    "type": "function",
                    "function": {"name": "read_file", "arguments": '{"path":"a.py"}'},
                }
            ],
        },
        {"role": "tool", "tool_call_id": "c1", "content": "x = 1"},
    ]
    path = _recording(
        tmp_path / "r.jsonl",
        _event("request", {"turn": 0, "attempt": 0, "messages": first}),
        _event(
            "response",
            {
                "turn": 0,
                "text": "",
                "finish_reason": "tool_calls",
                "tool_calls": [{"id": "c1", "name": "read_file", "arguments": '{"path":"a.py"}'}],
            },
        ),
        _event("request", {"turn": 1, "attempt": 0, "messages": second}),
        _event("response", {"turn": 1, "text": "it defines x", "finish_reason": "stop"}),
    )
    replayed = recorded_tools(load(path))
    handler = replayed.handler("read_file")
    assert asyncio.run(handler({"path": "a.py"})) == "x = 1"
    assert asyncio.run(handler({"path": "b.py"})).startswith("Error: this call was not")
    assert replayed.missed == ['read_file({"path": "b.py"})']


def test_F14_03_drift_is_not_swallowed_by_the_loops_failure_net() -> None:
    """`ReplayDrift` is a `BaseException` and the reason is measured.

    As an `AssertionError` it went straight into `_respond`'s `except
    Exception` -- the net that turns provider failures into retry decisions --
    and came out as `ModelFailed: the model call failed: ReplayDrift: ...`.
    Chapter 12's conservative default (unrecognised means fatal) is the only
    reason it stopped rather than retrying four times.  Chapter 7 learned the
    same thing about `CancelledError`.
    """
    assert not issubclass(ReplayDrift, Exception)
    assert issubclass(ReplayDrift, BaseException)


# ---------------------------------------------------------------------------
# F14-07  the average is not the measurement
# ---------------------------------------------------------------------------


def test_F14_07_a_better_total_can_hide_a_task_that_got_worse() -> None:
    """Two arms, 3/6 against 4/6, and the better one broke something.

    This is the whole reason `regressions()` exists and the whole reason the
    report prints a row per task.  A change that fixes two tasks and breaks one
    is a trade; reporting only the total hides which trade.
    """
    a, b, c = TASKS[0], TASKS[1], TASKS[2]
    before = Arm("before", [_result(a, True), _result(b, False), _result(c, False)])
    after = Arm("after", [_result(a, False), _result(b, True), _result(c, True)])

    assert before.total == (1, 3)
    assert after.total == (2, 3)
    assert regressions(before, after, [a, b, c]) == [f"{a.name}: 1 -> 0"]


def test_F14_07_a_task_with_one_failed_check_has_failed() -> None:
    """`ok` is "nothing failed", not "something passed".

    Found by mutation testing rather than by reading: every other test in this
    file builds a result whose checks all pass or all fail, and the two
    definitions agree on those.  A four-check task that edited the right file
    and also deleted the test file passes under the wrong one.
    """
    mixed = Result(TASKS[0], _trajectory(), passed=("wrote the file",), failed=("left the test",))
    assert not mixed.ok
    assert Arm("a", [mixed]).total == (0, 1)


def test_F14_07_a_task_set_covers_more_than_one_kind_of_right_answer() -> None:
    """A set of six variations on "edit this file" measures one behaviour six
    times.  The one guard that can be automated: at least one task whose
    correct trajectory contains no edit at all."""
    reads_only = [t for t in TASKS if any(c.name == "never calls apply_patch" for c in t.checks)]
    assert len(reads_only) >= 2, [t.name for t in TASKS]


# ---------------------------------------------------------------------------
# F14-08  the eval set and the system under test are not the same directory
# ---------------------------------------------------------------------------


async def test_F14_08_a_task_workspace_holds_exactly_what_the_task_declared(
    tmp_path: Path,
) -> None:
    """The precondition chapters 16 and 17 inherit.

    An agent with a memory reads whatever is in reach.  If the eval's own
    fixture, its expected answer, or the test file that grades it is in reach,
    the score measures the search path.
    """

    class Silent:
        tools: ClassVar[list[dict[str, Any]]] = []

        async def stream(self, messages: Any) -> Any:
            from minicodex.model import Completed, TextDelta

            yield TextDelta("nothing to do")
            yield Completed("stop")

    task = by_name("add-function")
    result = await run_task(task, lambda _: Silent(), workspace=tmp_path / "ws")
    assert set(result.trajectory.files) == set(task.files)


def test_F14_08_the_shell_starts_where_the_other_tools_are_pointed(tmp_path: Path) -> None:
    """`read_file` was bounded by `root` and `run_shell` by the process.

    Twelve chapters of CLI runs never separated the two, because `_ask` passes
    `Path.cwd()` as the root.  The first eval run did separate them, and the
    agent -- pointed at a two-file workspace -- ran `ls -R`, found this
    repository, and tried to `apply_patch` `src/minicodex/evals.py`: the file
    holding the checks it was being graded against.  Chapter 4's containment
    (F04-12) refused the write, which is the only reason this is a story about
    a near miss.
    """
    context = tool_context(root=tmp_path, session=Session(approver=AllowAll()))
    assert Path(context.shell.cwd).resolve() == tmp_path.resolve()


# ---------------------------------------------------------------------------
# F14-05  the tier a real model belongs in
# ---------------------------------------------------------------------------


def test_F14_05_the_nightly_tier_cannot_block_a_merge_and_sets_no_threshold(
    repo_root: Path,
) -> None:
    """A third workflow, for the same reason chapter 9 added a second one.

    The blocking suite stays offline (six steps, chapter -1's cap). The eval
    set needs a provider, so it runs on a schedule -- and it reports a number
    rather than enforcing one, because a threshold on a non-deterministic
    measurement is a red that gets ignored, and F14-02 is the entry about what
    an ignored red does to every other red.
    """
    import yaml

    text = (repo_root / ".github/workflows/nightly.yml").read_text(encoding="utf-8")
    workflow = yaml.safe_load(text)
    triggers = workflow.get("on", workflow.get(True))
    assert "pull_request" not in triggers
    assert "push" not in triggers
    assert "schedule" in triggers
    # No `--fail-under`, no `exit 1` on a score: the job prints a table.
    assert "fail-under" not in text


def test_F14_08_no_task_declares_a_path_out_of_its_workspace() -> None:
    for name in declared_files():
        assert not Path(name).is_absolute()
        assert ".." not in Path(name).parts


# ---------------------------------------------------------------------------
# F14-04  a random failure has to shrink to something a human can read
# ---------------------------------------------------------------------------


def test_F14_04_a_failing_history_is_reported_at_its_smallest() -> None:
    """Chapter 6's property runner prints a seed.

    A seed is reproducible and unreadable: the history that found seed 235 has
    fourteen items and one of them matters.  `minimise` removes turns while the
    failure survives, so the report is a three-item history instead of an
    instruction to go and run the generator again.
    """
    from property_support import minimise

    history = History()
    history.add_user("start")
    for index in range(8):
        history.add_assistant(f"step {index}")
    history.add_user("POISON")
    history.add_assistant("after")

    def fails(candidate: History) -> bool:
        return any(getattr(item, "text", "") == "POISON" for item in candidate.items)

    smaller = minimise(history, fails)
    assert fails(smaller)
    assert len(smaller.items) < len(history.items)
    assert len(smaller.items) == 1
