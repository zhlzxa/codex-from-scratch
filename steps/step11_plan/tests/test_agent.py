"""What the loop must guarantee, pinned so it stays true.

Every test runs against the recorded Ollama responses in `stub_ollama`, served
over a real socket.  Test names carry the fault IDs from FAULTS.md.
"""

from __future__ import annotations

import asyncio
import gc
import json
import time
import warnings
from pathlib import Path
from typing import Any

import pytest

from minicodex.agent import Agent, IncompleteStreamError
from minicodex.agent_types import ToolCall
from minicodex.history import AssistantMessage, ToolResult
from minicodex.model import ChatCompletionsModel, Completed, TextDelta, ToolCallDelta
from minicodex.recorder import Recorder
from minicodex.tools import TOOL_SCHEMAS

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def make_tools(log: list[str] | None = None) -> dict[str, Any]:
    """Tools that record what actually ran.

    The log is the point.  Asserting on the model's prose only proves the model
    said something.
    """
    seen = log if log is not None else []

    async def read_file(args: dict[str, Any]) -> str:
        seen.append(f"read_file:{args.get('path')}")
        return '__version__ = "0.0.1"'

    async def get_temperature(args: dict[str, Any]) -> str:
        seen.append(f"get_temperature:{args.get('city')}")
        return "22C"

    async def get_conditions(args: dict[str, Any]) -> str:
        seen.append(f"get_conditions:{args.get('city')}")
        return "Sunny"

    async def explode(args: dict[str, Any]) -> str:
        raise ValueError("disk on fire")

    return {
        "read_file": read_file,
        "get_temperature": get_temperature,
        "get_conditions": get_conditions,
        "explode": explode,
    }


def model(stub_url: str, **extra: Any) -> ChatCompletionsModel:
    return ChatCompletionsModel(base_url=stub_url, tools=TOOL_SCHEMAS, extra_body=extra)


async def _naive_run(llm: Any, tools: dict[str, Any], max_turns: int = 6) -> str:
    """The loop as first written, kept executable.

    Two bugs live here deliberately: it stops as soon as the model says
    anything, and it runs only the first tool call.  Keeping it runnable is what
    makes those bugs reproducible rather than anecdotal.  It demonstrates
    exactly two bugs and does not grow.
    """
    messages: list[dict[str, Any]] = [{"role": "user", "content": "go"}]
    for _ in range(max_turns):
        text_parts: list[str] = []
        calls: list[ToolCallDelta] = []
        async for event in llm.stream(messages):
            if isinstance(event, TextDelta):
                text_parts.append(event.text)
            elif isinstance(event, ToolCallDelta):
                calls.append(event)
        text = "".join(text_parts)
        messages.append({"role": "assistant", "content": text})

        if text:  # bug: narration mistaken for an answer
            return text
        if calls:
            call = calls[0]  # bug: the rest are dropped
            out = await tools[call.name](json.loads(call.arguments))
            messages.append({"role": "tool", "tool_call_id": call.call_id, "content": out})
    return ""


# ---------------------------------------------------------------------------
# The question the chapter is built around: did the work actually happen?
# ---------------------------------------------------------------------------


async def test_F00_02_naive_loop_answers_without_doing_the_work(stub_url: str) -> None:
    """The model narrates, the loop treats that as the answer, the tool never
    runs, and the run looks like a success."""
    log: list[str] = []

    answer = await _naive_run(model(stub_url), make_tools(log))

    assert answer == "I will read the contents of the file `src/minicodex/__init__.py`."
    assert log == [], "the file was never opened"


async def test_F00_02_absence_of_tool_calls_is_the_stop_signal(stub_url: str) -> None:
    log: list[str] = []
    agent = Agent(model(stub_url), make_tools(log))

    result = await agent.run("What does src/minicodex/__init__.py define?")

    assert result.final_text.startswith("`src/minicodex/__init__.py` defines the following:")
    assert "system_prompt()" in result.final_text
    assert result.stop_reason == "completed"
    assert result.turns_used == 2
    assert log == ["read_file:src/minicodex/__init__.py"]


# ---------------------------------------------------------------------------
# Every call runs; every call is answered
# ---------------------------------------------------------------------------


async def test_F00_03_naive_loop_drops_all_but_the_first_call(stub_url: str) -> None:
    log: list[str] = []

    await _naive_run(model(stub_url, stub_mode="three"), make_tools(log))

    assert log == ["get_temperature:New York"], "two calls were dropped without a word"


async def test_F00_03_every_call_runs_in_the_order_the_model_gave(stub_url: str) -> None:
    log: list[str] = []
    agent = Agent(model(stub_url, stub_mode="three"), make_tools(log))

    await agent.run("temperature and conditions for New York, temperature for London")

    assert log == [
        "get_temperature:New York",
        "get_conditions:New York",
        "get_temperature:London",
    ]


async def test_F00_03_outputs_pair_one_to_one_with_calls(stub_url: str) -> None:
    """Chapter 0 asserted this by hand and said chapter 1 would turn it into a
    type.  It did: `History` refuses the invalid states outright, so this test
    now only confirms the loop uses it correctly."""
    agent = Agent(model(stub_url, stub_mode="three"), make_tools())

    result = await agent.run("go")

    issued = [
        c.call_id for i in result.history if isinstance(i, AssistantMessage) for c in i.tool_calls
    ]
    answered = [i.call_id for i in result.history if isinstance(i, ToolResult)]
    assert issued == answered
    assert result.history.unanswered() == ()


# ---------------------------------------------------------------------------
# A stream that stops is not a stream that finished
# ---------------------------------------------------------------------------


async def test_F00_04_stream_without_the_done_sentinel_is_refused(stub_url: str) -> None:
    """`stub_cut` truncates the recording and withholds `[DONE]`."""
    agent = Agent(model(stub_url, stub_cut=7), make_tools())

    with pytest.raises(IncompleteStreamError) as excinfo:
        await agent.run("go")

    assert "without a [DONE] sentinel" in str(excinfo.value)


async def test_F00_04_nothing_from_a_cut_stream_is_executed(stub_url: str) -> None:
    """The dangerous version does not crash.  It commits a partial turn and
    fails on the next request, somewhere else entirely."""
    log: list[str] = []
    agent = Agent(model(stub_url, stub_cut=7), make_tools(log))

    with pytest.raises(IncompleteStreamError):
        await agent.run("go")

    assert log == [], "a call from an unfinished stream must never run"


async def test_F00_04_the_done_sentinel_is_not_json(stub_url: str) -> None:
    """Parsing every line before checking for the sentinel is the first thing
    that breaks -- and it breaks after printing a perfectly good answer."""
    with pytest.raises(json.JSONDecodeError):
        json.loads("[DONE]".removeprefix("data: "))


# ---------------------------------------------------------------------------
# Two granularities in one stream
# ---------------------------------------------------------------------------


async def test_prose_is_joined_and_tool_calls_are_not(stub_url: str) -> None:
    events = [e async for e in model(stub_url).stream([{"role": "user", "content": "go"}])]

    texts = [e for e in events if isinstance(e, TextDelta)]
    calls = [e for e in events if isinstance(e, ToolCallDelta)]
    done = [e for e in events if isinstance(e, Completed)]

    assert len(texts) == 6, "prose arrives in arbitrary slices"
    assert "".join(t.text for t in texts) == (
        "I will read the contents of the file `src/minicodex/__init__.py`."
    )
    assert len(calls) == 1, "a tool call arrives whole, in a single chunk"
    assert calls[0].arguments == '{"path":"src/minicodex/__init__.py"}', (
        "arguments are one complete JSON string, not fragments"
    )
    assert done == [Completed("tool_calls")]


async def test_tool_calls_are_ordered_by_index_not_arrival(stub_url: str) -> None:
    agent = Agent(model(stub_url, stub_mode="three"), make_tools())
    turn = await agent._collect(model(stub_url, stub_mode="three").stream([]))

    assert [c.name for c in turn.tool_calls] == [
        "get_temperature",
        "get_conditions",
        "get_temperature",
    ]
    assert turn.finish_reason == "tool_calls"


# ---------------------------------------------------------------------------
# Tool failures are information, not disasters
# ---------------------------------------------------------------------------


async def test_F00_05_tool_exception_comes_back_as_output(stub_url: str) -> None:
    agent = Agent(model(stub_url), make_tools())
    call = ToolCall("call_1", "explode", {}, "{}")

    output = await agent._run_tool(call)

    assert "ValueError" in output
    assert "disk on fire" in output


async def test_F00_05_a_failing_call_does_not_cancel_its_siblings(stub_url: str) -> None:
    """Reuses the three-call recording but points one name at a broken tool."""
    log: list[str] = []
    tools = make_tools(log)
    tools["get_conditions"] = tools["explode"]
    agent = Agent(model(stub_url, stub_mode="three"), tools)

    result = await agent.run("go")

    assert log == ["get_temperature:New York", "get_temperature:London"]
    assert len([i for i in result.history if isinstance(i, ToolResult)]) == 3


async def test_F00_06_unknown_tool_says_what_to_call_instead(stub_url: str) -> None:
    agent = Agent(model(stub_url), make_tools())
    call = ToolCall("call_1", "get_temperatureZ", {"city": "X"}, "{}")

    message = await agent._run_tool(call)

    assert "get_temperatureZ" in message
    assert "explode, get_conditions, get_temperature" in message
    assert "Call one of those instead" in message


async def test_F00_06_malformed_arguments_become_output_not_a_crash(stub_url: str) -> None:
    agent = Agent(model(stub_url), make_tools())
    call = ToolCall("call_1", "get_temperature", None, "{not json")

    message = await agent._run_tool(call)

    assert "not valid JSON" in message
    assert "{not json" in message


# ---------------------------------------------------------------------------
# The loop ends, and the model knows it is ending
# ---------------------------------------------------------------------------


async def test_F00_01_runaway_loop_is_bounded(stub_url: str) -> None:
    """The recording asks for a tool every time it has not seen a tool result,
    so a loop that never feeds results back would run forever."""

    class NeverSatisfied:
        """Strips tool results out, so the model always asks again."""

        def __init__(self, inner: ChatCompletionsModel) -> None:
            self.inner = inner
            self.calls = 0

        def stream(self, messages: list[dict[str, Any]]):
            self.calls += 1
            return self.inner.stream([m for m in messages if m.get("role") != "tool"])

    llm = NeverSatisfied(model(stub_url))
    agent = Agent(llm, make_tools(), max_turns=5)

    result = await agent.run("go")

    assert result.stop_reason == "turn_limit"
    assert result.turns_used == 5
    assert llm.calls == 5, "the bound must apply to model calls, not loop iterations"


async def test_F00_01_model_is_warned_before_the_budget_runs_out(stub_url: str) -> None:
    """Being killed mid-thought produces nothing.  Being told produces an answer."""
    seen: list[list[dict[str, Any]]] = []

    class Watching:
        def __init__(self, inner: ChatCompletionsModel) -> None:
            self.inner = inner

        def stream(self, messages: list[dict[str, Any]]):
            seen.append([dict(m) for m in messages])
            return self.inner.stream([m for m in messages if m.get("role") != "tool"])

    agent = Agent(Watching(model(stub_url)), make_tools(), max_turns=4)
    await agent.run("go")

    notices = [m for m in seen[-1] if m["role"] == "system"]
    assert notices, "the model was never told it was running out of turns"
    # Chapter 11 split this in two. The second-to-last turn gets a count; the
    # last one gets a different message, because on the last turn the loop
    # stops running tool calls and the model has to be told that rather than
    # asked to wrap up while a tool is still on the table.
    assert "2 tool-calling turn(s) left" in seen[-2][-1]["content"]
    assert "last turn" in notices[-1]["content"]
    assert "will not be run" in notices[-1]["content"]


# ---------------------------------------------------------------------------
# Reproducibility, and the transcript that makes debugging possible
# ---------------------------------------------------------------------------


async def test_F00_07_the_recorded_stream_gives_the_same_run_every_time(stub_url: str) -> None:
    """A real model mostly repeats itself and occasionally does not, which is
    worse than always differing: a fix looks confirmed when it was only lucky.
    Replaying a recording removes the luck."""
    first = await Agent(model(stub_url), make_tools()).run("go")
    second = await Agent(model(stub_url), make_tools()).run("go")

    assert first.history.items == second.history.items


async def test_F_1_04_transcript_captures_what_the_model_was_sent(
    stub_url: str, tmp_path: Path
) -> None:
    recorder = Recorder(tmp_path / "run.jsonl")
    agent = Agent(model(stub_url), make_tools(), recorder=recorder)

    await agent.run("go")

    events = recorder.read_all()
    assert [e["kind"] for e in events] == ["request", "response", "request", "response"]
    assert events[0]["payload"]["messages"][0] == {"role": "user", "content": "go"}


def test_F_1_04_transcript_survives_a_truncated_final_line(tmp_path: Path) -> None:
    path = tmp_path / "run.jsonl"
    recorder = Recorder(path)
    recorder.record("request", {"n": 1})
    with path.open("a", encoding="utf-8") as fh:
        fh.write('{"seq": 2, "kind": "resp')  # process killed here

    assert len(recorder.read_all()) == 1


def test_F_1_04_transcript_redacts_credentials(tmp_path: Path) -> None:
    recorder = Recorder(tmp_path / "run.jsonl")
    recorder.record("request", {"headers": [{"Authorization": "Bearer leak-me"}]})

    assert "leak-me" not in (tmp_path / "run.jsonl").read_text(encoding="utf-8")


def test_F_1_04_transcript_never_raises_into_its_caller(tmp_path: Path) -> None:
    recorder = Recorder(tmp_path / "run.jsonl")
    recorder.path = tmp_path / "no-such-dir" / "run.jsonl"
    assert recorder.record("request", {"n": 1}) == 1  # no exception


def test_request_body_can_be_inspected_without_a_network_call() -> None:
    """Chapter 6 diffs this; chapter 13 snapshots it.  Both need it separable."""
    body = ChatCompletionsModel(model="m", tools=TOOL_SCHEMAS).request_body(
        [{"role": "user", "content": "hi"}]
    )
    assert body["stream"] is True
    assert body["messages"] == [{"role": "user", "content": "hi"}]
    assert body["tools"][0]["function"]["name"] == "read_file"


# ---------------------------------------------------------------------------
# Async footguns
# ---------------------------------------------------------------------------


def test_F00_08_a_forgotten_await_runs_nothing_and_says_nothing() -> None:
    async def work() -> int:
        raise AssertionError("this body must never run")

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        coro = work()  # the missing await
        del coro
        gc.collect()

    assert any(issubclass(w.category, RuntimeWarning) for w in caught)
    assert any("never awaited" in str(w.message) for w in caught)


def test_F00_08_pytest_promotes_that_warning_to_a_failure(repo_root: Path) -> None:
    try:
        import tomllib
    except ModuleNotFoundError:  # Python 3.10
        import tomli as tomllib

    with (repo_root / "pyproject.toml").open("rb") as fh:
        cfg = tomllib.load(fh)

    assert "error::RuntimeWarning" in cfg["tool"]["pytest"]["ini_options"]["filterwarnings"]


async def test_F00_09_blocking_io_in_a_tool_serialises_the_loop() -> None:
    async def blocking() -> None:
        time.sleep(0.05)  # noqa: ASYNC251 -- reproducing the fault on purpose

    async def cooperative() -> None:
        await asyncio.to_thread(time.sleep, 0.05)

    start = time.perf_counter()
    await asyncio.gather(blocking(), blocking())
    blocking_elapsed = time.perf_counter() - start

    start = time.perf_counter()
    await asyncio.gather(cooperative(), cooperative())
    cooperative_elapsed = time.perf_counter() - start

    assert blocking_elapsed > 0.09, "expected the blocking version to serialise"
    assert cooperative_elapsed < 0.09, "expected the offloaded version to overlap"


# ---------------------------------------------------------------------------
# F01-01  the same tool call, two shapes
# ---------------------------------------------------------------------------


async def test_F01_01_openai_fragments_are_reassembled(stub_url: str) -> None:
    """Recorded from api.openai.com: fourteen chunks for one call, and only the
    first carries the id and the name.  Chapter 0 overwrote by index and was
    left with `name=''` and `arguments='"}'`."""
    events = [
        e
        async for e in ChatCompletionsModel(
            base_url=stub_url, extra_body={"stub_mode": "openai"}
        ).stream([{"role": "user", "content": "go"}])
    ]

    calls = [e for e in events if isinstance(e, ToolCallDelta)]

    assert len(calls) == 1, "fourteen chunks must arrive as one call"
    assert calls[0].name == "read_file"
    assert calls[0].call_id == "call_bqv6MLMr9BhXis7T4LB6tGqa"
    assert calls[0].arguments == '{"path":"src/minicodex/__init__.py"}'


async def test_F01_01_both_providers_produce_the_same_tool_call(stub_url: str) -> None:
    """The whole point of normalising at the boundary: above `model.py`, the two
    recordings are indistinguishable."""

    async def call_from(mode: str) -> ToolCallDelta:
        events = [
            e
            async for e in ChatCompletionsModel(
                base_url=stub_url, extra_body={"stub_mode": mode}
            ).stream([{"role": "user", "content": "go"}])
        ]
        return next(e for e in events if isinstance(e, ToolCallDelta))

    ollama = await call_from("narrate")
    openai = await call_from("openai")

    assert ollama.name == openai.name == "read_file"
    assert json.loads(ollama.arguments) == json.loads(openai.arguments)


async def test_F01_01_a_fragmented_call_runs_the_right_tool(stub_url: str) -> None:
    """End to end: the agent does not know or care which provider it is."""
    log: list[str] = []
    agent = Agent(model(stub_url, stub_mode="openai"), make_tools(log))

    result = await agent.run("What does src/minicodex/__init__.py define?")

    assert log == ["read_file:src/minicodex/__init__.py"]
    assert result.stop_reason == "completed"


async def test_F01_03_the_request_is_rendered_from_the_history(
    stub_url: str, tmp_path: Path
) -> None:
    """The invariant check lives on the send path, not beside it.

    `run()` never assembles a message list by hand; it calls `to_wire()`, which
    is what refuses a history with unanswered calls.  Comparing the recorded
    request against `to_wire()` is what pins the check to that path.
    """
    recorder = Recorder(tmp_path / "run.jsonl")
    agent = Agent(model(stub_url), make_tools(), recorder=recorder)

    result = await agent.run("go")

    first_request = recorder.read_all()[0]["payload"]["messages"]
    assert first_request == [{"role": "user", "content": "go"}]
    assert result.history.unanswered() == ()
