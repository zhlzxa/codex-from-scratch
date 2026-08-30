"""Chapter 12: retries and error classification.

Every failure body in here is a verbatim recording from api.openai.com on
2026-08-12 (`probe_retry.py shapes` / `overlong` / `ratelimit`), served over a
real socket by `stub.py`.  The one exception is `SERVER_ERROR`, which cannot be
asked for and is marked as invented where it is defined.
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Any

import httpx
import pytest

from minicodex import stub
from minicodex.agent import Agent
from minicodex.agent_types import IncompleteStreamError
from minicodex.compaction import SummaryRequest
from minicodex.mcp import DEFAULT_TOOL_TIMEOUT
from minicodex.model import DEFAULT_ATTEMPT_TIMEOUT, ChatCompletionsModel, ModelHTTPError
from minicodex.retry import (
    DEFAULT_POLICY,
    Failure,
    ModelFailed,
    RetryPolicy,
    classify,
    explain,
    wait_for,
)
from minicodex.shell import DEFAULT_TIMEOUT as SHELL_TIMEOUT
from minicodex.subagent import DEFAULT_TASK_TIMEOUT, TaskResult
from minicodex.tools import TOOL_SCHEMAS

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

NO_WAIT = RetryPolicy(attempts=4, base=0.001, cap=0.001, budget=90.0)

# A 429 with no `retry-after` header at all.  Used wherever a test needs the
# retry to actually happen: the *recorded* 429 asks for 45.175 seconds and the
# loop honours it, so a test driving the real recording through the loop would
# sit through 45 seconds to assert something about classification.  That is not
# a workaround, it is the measurement in `test_F12_02_only_one_retry_fits`:
# with real numbers the default policy allows one retry of a rate limit, not
# four.  Providers that send no header exist, which is why `wait_for` has a
# fallback schedule at all.
BARE_429 = {
    "status": 429,
    "body": {"error": {"message": "Rate limit reached", "code": "rate_limit_exceeded"}},
}


def failing(stub_url: str, queue_id: str, *responses: dict[str, Any], **extra: Any) -> Any:
    """A client whose next requests hit the queued failures, then a recording."""
    return ChatCompletionsModel(
        base_url=stub_url,
        tools=TOOL_SCHEMAS,
        extra_body={"stub_fail": {"id": queue_id, "responses": list(responses)}, **extra},
    )


def http(recorded: dict[str, Any]) -> ModelHTTPError:
    """Rebuild the exception the client would raise for a recorded response."""
    import json as _json

    return ModelHTTPError(
        status=recorded["status"],
        url="https://api.openai.com/v1/chat/completions",
        headers={**recorded.get("headers", {}), "x-request-id": "req_abc"},
        body=_json.dumps(recorded["body"]),
    )


async def _tool(args: dict[str, Any]) -> str:
    return '__version__ = "0.0.1"'


TOOLS = {"read_file": _tool}


@pytest.fixture
def stub_url() -> Any:
    """A fresh, threaded stub server for every test in this module.

    This deliberately **overrides** `conftest`'s session-scoped one, and the
    reason is a flaky suite rather than a preference. Chapter 12's tests abort
    connections on purpose -- that is what a timeout, a cancellation and a
    truncated stream all look like from the server's side -- and the shared
    stub is a single-threaded `HTTPServer` whose module-level request log is
    process-wide. Sharing it produced a suite that failed in a *different
    place* on each run: an abandoned request from one test being served during
    the next one, adding a request nobody counted on.

    F14-02 says flakiness is a defect. Three runs of the same file failing
    three different ways is that defect arriving early enough to fix.
    """
    import threading
    from http.server import ThreadingHTTPServer

    stub.reset()
    server = ThreadingHTTPServer(("127.0.0.1", 0), stub._Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}/v1"
    finally:
        server.shutdown()
        server.server_close()
        stub.reset()


def long_history() -> Any:
    """A conversation with something in it worth cutting.

    Several turns rather than one message, because the interesting behaviour of
    a forced compaction is what it *drops*, and a one-message history has
    nothing to drop -- which is now its own failure (`nothing left to
    compact`), not a compaction that reports success and changes nothing.
    """
    from minicodex.agent_types import ToolCall
    from minicodex.history import History

    history = History()
    history.add_user("go through the modules and tell me what they do")
    for n in range(6):
        call = ToolCall(f"call_{n}", "read_file", {"path": f"m{n}.py"}, "{}")
        history.add_assistant(f"reading module {n}", (call,))
        body = "code = 1\n" * 200
        history.add_tool_result(f"call_{n}", f"# module {n}\n{body}")
    return history


# ---------------------------------------------------------------------------
# F12-01: retryable versus terminal
# ---------------------------------------------------------------------------


def test_F12_01_status_alone_cannot_classify_a_failure() -> None:
    """The measurement this whole module is shaped by.

    Two recorded failures, both HTTP 400, both `type: invalid_request_error`,
    and the correct reactions are at opposite ends of the table: one must be
    retried after the conversation loses weight, the other must never be sent
    again.  Whatever decides between them cannot be reading the status code.
    """
    too_long = http(stub.CONTEXT_LENGTH_EXCEEDED)
    bad_schema = http(stub.BAD_TOOL_SCHEMA)

    assert too_long.status == bad_schema.status == 400
    assert too_long.type == bad_schema.type == "invalid_request_error"

    assert classify(too_long).disposition == "shrink"
    assert classify(bad_schema).disposition == "fatal"


@pytest.mark.parametrize(
    ("recorded", "disposition", "kind"),
    [
        (stub.RATE_LIMITED, "retry", "rate_limit"),
        (stub.SERVER_ERROR, "retry", "server_error"),
        (stub.CONTEXT_LENGTH_EXCEEDED, "shrink", "context_length"),
        (stub.BAD_TOOL_SCHEMA, "fatal", "http_400"),
        (stub.BAD_API_KEY, "fatal", "auth"),
    ],
)
def test_F12_01_every_recorded_failure_has_one_answer(
    recorded: dict[str, Any], disposition: str, kind: str
) -> None:
    failure = classify(http(recorded))
    assert (failure.disposition, failure.kind) == (disposition, kind)


async def test_F12_01_a_terminal_failure_is_sent_exactly_once(
    stub_url: str, served_requests: list[dict[str, Any]]
) -> None:
    """The fault as listed: 400s retried forever.

    Five attempts against a request the provider will never accept sent five
    byte-identical bodies and took nine seconds (`probe_retry.py naive`).  The
    cost is not only the nine seconds: a rejected 160k-token request still
    debited its tokens from the account's rate limit, so retrying what cannot
    succeed is what pushes the *next* request into a 429.
    """
    agent = Agent(failing(stub_url, "t1", *[stub.BAD_TOOL_SCHEMA] * 8), TOOLS)

    with pytest.raises(ModelFailed) as excinfo:
        await agent.run("go")

    assert len(served_requests) == 1
    assert excinfo.value.attempts == 1
    assert excinfo.value.failure.disposition == "fatal"


async def test_F12_01_a_retryable_failure_finishes_the_run(
    stub_url: str, served_requests: list[dict[str, Any]]
) -> None:
    """Two 429s in front of a conversation that works: four requests, one answer."""
    agent = Agent(
        failing(stub_url, "t2", BARE_429, BARE_429),
        TOOLS,
        retry_policy=NO_WAIT,
    )

    result = await agent.run("What does __init__.py define?")

    assert result.stop_reason == "completed"
    assert "__version__" in result.final_text
    # two rejected, then the tool-calling turn, then the answer
    assert len(served_requests) == 4


async def test_F12_01_a_retryable_failure_is_not_retried_forever(
    stub_url: str, served_requests: list[dict[str, Any]]
) -> None:
    """A server that is down stays down, and "retryable" is not "unlimited".

    Written after mutation testing: deleting the attempt check from `wait_for`
    left every other test in this file green, because they all reach an answer
    or run out of budget before they run out of attempts.
    """
    agent = Agent(
        failing(stub_url, "t1b", *[stub.SERVER_ERROR] * 20),
        TOOLS,
        retry_policy=NO_WAIT,
    )

    with pytest.raises(ModelFailed) as excinfo:
        await agent.run("go")

    assert len(served_requests) == NO_WAIT.attempts == 4
    assert excinfo.value.attempts == 4


async def test_F12_01_a_hanging_server_runs_out_of_budget_without_sleeping(
    stub_url: str,
) -> None:
    """The other half of the same bound, and the reason `budget` is wall clock.

    Also written after mutation testing: with the budget counting only the time
    spent in `asyncio.sleep`, a server that accepts the connection and then
    says nothing is retried the full attempt count with a full timeout each --
    eight minutes at the shipped numbers, none of it asleep.  Every test here
    was green against that.
    """
    model = ChatCompletionsModel(
        base_url=stub_url,
        tools=TOOL_SCHEMAS,
        timeout=0.1,
        extra_body={"stub_delay": 1.0},
    )
    agent = Agent(
        model,
        TOOLS,
        retry_policy=RetryPolicy(attempts=8, base=0.001, cap=0.001, budget=0.4),
    )

    began = time.monotonic()
    with pytest.raises(ModelFailed):
        await agent.run("go")
    elapsed = time.monotonic() - began

    assert elapsed < 2.0, "the budget must bound a hang, not only a backoff"


def test_F12_01_an_unrecognised_exception_is_fatal_not_retryable() -> None:
    """The default leans the other way from chapter 5's, on purpose.

    Chapter 5 answers "unknown shell syntax" with *ask the human*.  There is no
    human inside a backoff, and the two mistakes are not symmetric: a wrongly
    terminal failure costs one run and prints why, a wrongly retried one costs
    the quota in silence.
    """
    assert classify(ValueError("something local")).disposition == "fatal"
    assert classify(httpx.ConnectError("no route to host")).disposition == "retry"
    # ...but not the two transport errors that are our own bug rather than
    # weather.  Retrying a malformed request produces the same failure slower.
    assert classify(httpx.LocalProtocolError("bad header")).disposition == "fatal"
    assert classify(httpx.UnsupportedProtocol("no scheme")).disposition == "fatal"


# ---------------------------------------------------------------------------
# F12-02: the number the server sent
# ---------------------------------------------------------------------------


def test_F12_02_retry_after_is_read_from_the_recorded_headers() -> None:
    """`retry-after: 46` and `retry-after-ms: 45175` both arrived.

    The millisecond one wins: the whole-second header is rounded up, and a
    number somebody has already rounded is a number they have already called
    approximate.
    """
    failure = classify(http(stub.RATE_LIMITED))
    assert failure.retry_after == pytest.approx(45.175)


def test_F12_02_the_server_beats_our_own_backoff() -> None:
    """Exponential backoff makes five attempts inside 31 seconds.

    The window measured on a real 429 was 45.175 seconds wide, so all five
    would have been sent while it was shut -- and each one is another request
    against a limit that is already exhausted.
    """
    failure = classify(http(stub.RATE_LIMITED))
    assert wait_for(failure, 0, jitter=False) == pytest.approx(45.175)

    # No header: our own schedule, with the cap applied.
    bare = Failure("retry", "rate_limit", "429 with no headers")
    assert [wait_for(bare, n, jitter=False) for n in range(3)] == [1.0, 2.0, 4.0]


def test_F12_02_only_one_retry_of_a_real_rate_limit_fits_the_budget() -> None:
    """A measurement that only appears once the real numbers are in place.

    The default policy says four attempts.  Against the recorded 429 it gets
    **two**: 45.175 seconds is honoured once, and a second one would take the
    total to 90.35 against a 90-second budget.  "Four attempts" was never a
    description of what happens -- the budget decides, and the server decides
    the budget.
    """
    failure = classify(http(stub.RATE_LIMITED))
    waited = 0.0
    attempts = 1
    # A bounded loop, not `while wait_for(...) is not None`. The first version
    # was the unbounded one, and mutation testing turned it into a hang rather
    # than a failure: with the budget check deleted this loop never ends, the
    # whole suite times out, and a mutation whose symptom is a timeout has
    # measured nothing. Interlude A's rule, one layer up -- the bound goes on
    # the thing being tested, not after it.
    for _ in range(10):
        wait = wait_for(failure, attempts - 1, elapsed=waited)
        if wait is None:
            break
        waited += wait
        attempts += 1
    assert attempts == 2
    assert waited == pytest.approx(45.175)


def test_F12_02_waiting_less_than_asked_is_not_an_option() -> None:
    """Two waits of 45s do not fit in a 90s budget, and half a wait is worse
    than none: it spends the request and arrives before the window opens."""
    failure = classify(http(stub.RATE_LIMITED))
    assert wait_for(failure, 0, elapsed=50.0) is None
    assert wait_for(failure, 1, elapsed=45.175) is None


def test_F12_02_an_unreadable_header_falls_back_rather_than_crashing() -> None:
    """RFC 9110 also allows an HTTP date here.  No provider in this book has
    ever sent one, so it is not parsed -- and an unparsed header must land in
    the same place as a missing one."""
    weird = ModelHTTPError(
        status=429,
        url="http://x/v1/chat/completions",
        headers={"retry-after": "Wed, 21 Oct 2026 07:28:00 GMT"},
        body='{"error": {"code": "rate_limit_exceeded"}}',
    )
    failure = classify(weird)
    assert failure.retry_after is None
    assert wait_for(failure, 0, jitter=False) == 1.0


def test_F12_02_jitter_stays_inside_half_to_full() -> None:
    """A parent and its sub-agents share one account and one clock."""
    bare = Failure("retry", "server_error", "500")
    waits = [wait_for(bare, 2) for _ in range(200)]
    assert all(w is not None and 2.0 <= w <= 4.0 for w in waits)
    assert len({round(w, 3) for w in waits if w is not None}) > 1


# ---------------------------------------------------------------------------
# F12-03: a stream that dies halfway
# ---------------------------------------------------------------------------


async def test_F12_03_a_cut_stream_leaves_nothing_to_reconcile(
    stub_url: str, served_requests: list[dict[str, Any]]
) -> None:
    """The listed fault -- "partial call state ambiguous" -- cannot arise.

    `_collect` returns only on `[DONE]` (chapter 0, F00-04), so an attempt that
    dies halfway has committed nothing at all.  That is what makes the second
    attempt a repeat rather than a duplicate, and it is why the retry boundary
    is around stream *plus assembly* and not inside `model.stream()`, which has
    already yielded events it cannot take back.
    """
    log: list[str] = []

    async def watched(args: dict[str, Any]) -> str:
        log.append("ran")
        return '__version__ = "0.0.1"'

    agent = Agent(
        failing(stub_url, "t3", {"stream_cut": 7}),
        {"read_file": watched},
        retry_policy=NO_WAIT,
    )

    result = await agent.run("What does __init__.py define?")

    assert result.stop_reason == "completed"
    # The cut stream contained a complete `read_file` call in its first seven
    # chunks.  It ran once, not twice, and not zero times.
    assert log == ["ran"]
    assert len(served_requests) == 3


def test_F12_03_an_incomplete_stream_is_retryable() -> None:
    failure = classify(IncompleteStreamError("stream ended ... without a [DONE] sentinel"))
    assert failure.disposition == "retry"
    assert failure.kind == "incomplete_stream"


# ---------------------------------------------------------------------------
# F12-04: what a retry costs, and what it must never repeat
# ---------------------------------------------------------------------------


async def test_F12_04_a_retry_re_sends_the_same_request(
    stub_url: str, served_requests: list[dict[str, Any]]
) -> None:
    """Measured, and it is the reason the listed fix does not apply here.

    `Idempotency-Key` on `/v1/chat/completions` was measured to do nothing: the
    same key twice returned two different answers, two request ids and two
    debits.  So the model call cannot be deduplicated by the provider, and the
    honest bound is the one this test asserts -- a retry sends *the same thing*
    rather than a new thing, so the cost of getting it wrong is money, not a
    second side effect.  Everything that does have a side effect (tools, MCP
    calls, sub-agents) sits on the other side of the boundary and is never
    re-run by this loop.
    """
    agent = Agent(
        failing(stub_url, "t4", stub.SERVER_ERROR, stub.SERVER_ERROR),
        TOOLS,
        retry_policy=NO_WAIT,
    )

    await agent.run("What does __init__.py define?")

    first, second = served_requests[0], served_requests[1]
    assert first["messages"] == second["messages"]


async def test_F12_04_no_tool_runs_twice_because_of_a_retry(stub_url: str) -> None:
    """The side-effecting half of idempotency, stated as a property of the loop.

    A failure can only interrupt the *model* call: by the time a tool runs, the
    turn has been assembled and committed, and nothing after that point is
    retried.
    """
    log: list[str] = []

    async def watched(args: dict[str, Any]) -> str:
        log.append("ran")
        return '__version__ = "0.0.1"'

    agent = Agent(
        failing(stub_url, "t5", stub.SERVER_ERROR, stub.SERVER_ERROR, stub.SERVER_ERROR),
        {"read_file": watched},
        retry_policy=NO_WAIT,
    )

    await agent.run("What does __init__.py define?")
    assert log == ["ran"]


# ---------------------------------------------------------------------------
# F12-05: the request that is too big
# ---------------------------------------------------------------------------


async def test_F12_05_a_context_length_refusal_compacts_and_retries(
    stub_url: str, served_requests: list[dict[str, Any]]
) -> None:
    """The provider's 400 is ground truth about a guess chapter 6 makes.

    Compaction fires on a local estimate.  When the estimate is wrong, this is
    how we find out -- and it is the only way we find out, because a rejected
    request produces no usage chunk at all.
    """
    summaries: list[SummaryRequest] = []

    async def summarise(request: SummaryRequest) -> str:
        summaries.append(request)
        return "## Done\nlooked at the modules\n"

    agent = Agent(
        failing(stub_url, "t6", stub.CONTEXT_LENGTH_EXCEEDED),
        TOOLS,
        context_window=8000,
        summariser=summarise,
        retry_policy=NO_WAIT,
        resume_from=long_history(),
    )

    result = await agent.run("What does __init__.py define?")

    assert result.stop_reason == "completed"
    assert len(summaries) == 1, "the refusal forced exactly one compaction"
    assert len(result.compactions) == 1, "and the run reports it"


async def test_F12_05_an_absurd_stated_count_does_not_move_the_ruler(
    stub_url: str, tmp_path: Path
) -> None:
    """ "your messages resulted in 160008 tokens" is a measurement, not prose --
    and feeding it straight into chapter 6's calibration was measurably wrong.

    `Calibration.observe` keeps the *latest* ratio, and this observation does
    not come from a usage chunk about a request the server answered.  The first
    version passed it through unguarded: one observation moved the ratio to
    **x250**, and the run then compacted on **11 of its 12 turns** and never
    finished, because every later estimate was over the window no matter how
    much had just been cut.

    Clamped at 4x, which is far outside the 0.43x-1.04x spread chapter 6
    measured for this program's content.  The number itself is not thrown away
    -- it goes to the transcript, where a human can see it.
    """
    from minicodex.recorder import Recorder

    async def summarise(request: SummaryRequest) -> str:
        return "## Done\nx\n"

    recorder = Recorder(tmp_path / "run.jsonl")
    agent = Agent(
        failing(stub_url, "t7", stub.CONTEXT_LENGTH_EXCEEDED),
        TOOLS,
        context_window=8000,
        summariser=summarise,
        recorder=recorder,
        retry_policy=NO_WAIT,
        resume_from=long_history(),
    )
    await agent.run("What does __init__.py define?")

    assert agent.calibration.ratio < 4.0
    events = [json.loads(line) for line in recorder.path.read_text("utf-8").splitlines()]
    rejected = [e for e in events if e["kind"] == "calibration_rejected"]
    assert rejected and rejected[0]["payload"]["stated"] == 160008


def test_F12_05_the_two_numbers_are_read_out_of_the_message() -> None:
    failure = classify(http(stub.CONTEXT_LENGTH_EXCEEDED))
    assert failure.stated_tokens == (128000, 160008)


async def test_F12_05_without_compaction_it_is_a_fatal_failure_that_says_so(
    stub_url: str,
) -> None:
    """No summariser means nothing can be done about it here.  What must not
    happen is retrying an identical, identically-too-long request."""
    agent = Agent(failing(stub_url, "t8", *[stub.CONTEXT_LENGTH_EXCEEDED] * 4), TOOLS)

    with pytest.raises(ModelFailed) as excinfo:
        await agent.run("go")

    assert excinfo.value.failure.kind == "context_length"
    assert "--context-window" in str(excinfo.value)


async def test_F12_05_a_history_with_nothing_to_cut_is_not_compacted_twice(
    stub_url: str,
) -> None:
    """A one-message conversation that the provider says is too long is a
    conversation whose *single message* is too long (F06-09).

    The first version reported a compaction, changed nothing, and re-sent the
    identical request -- F12-01 with a compaction in front of it.
    """

    async def summarise(request: SummaryRequest) -> str:
        return "## Done\nx\n"

    agent = Agent(
        failing(stub_url, "t9", *[stub.CONTEXT_LENGTH_EXCEEDED] * 4),
        TOOLS,
        context_window=8000,
        summariser=summarise,
        retry_policy=NO_WAIT,
    )

    with pytest.raises(ModelFailed) as excinfo:
        await agent.run("go")

    assert "nothing left to compact" in str(excinfo.value)


async def test_F12_05_compacting_twice_for_one_turn_is_refused(stub_url: str) -> None:
    """If it still does not fit after compacting, the summary is the thing that
    does not fit, and compacting again is a loop with a bill attached."""

    async def summarise(request: SummaryRequest) -> str:
        return "## Done\nx\n"

    agent = Agent(
        failing(stub_url, "t9b", *[stub.CONTEXT_LENGTH_EXCEEDED] * 4),
        TOOLS,
        context_window=8000,
        summariser=summarise,
        retry_policy=NO_WAIT,
        resume_from=long_history(),
    )

    with pytest.raises(ModelFailed) as excinfo:
        await agent.run("go")

    assert "after compacting once" in str(excinfo.value)


async def test_F12_05_a_fatal_failure_inside_compaction_is_not_degraded(
    stub_url: str,
) -> None:
    """Chapter 6 turns a failed summariser call into a deterministic note and
    carries on.  That is right for a dropped connection and wrong for a 401:
    the next request fails identically, so degrading first destroys the
    transcript on the way to a run that was going to end anyway.
    """

    async def summarise(request: SummaryRequest) -> str:
        raise http(stub.BAD_API_KEY)

    agent = Agent(
        failing(stub_url, "t10", stub.CONTEXT_LENGTH_EXCEEDED),
        TOOLS,
        context_window=8000,
        summariser=summarise,
        retry_policy=NO_WAIT,
        resume_from=long_history(),
    )

    with pytest.raises(ModelFailed) as excinfo:
        await agent.run("go")

    assert excinfo.value.failure.kind == "auth"


async def test_F12_05_a_transient_failure_inside_compaction_still_degrades() -> None:
    """The other half of the same branch, so the narrowing stays narrow."""
    from minicodex.compaction import Sizer, compact

    async def summarise(request: SummaryRequest) -> str:
        raise httpx.ConnectError("connection reset")

    result = await compact(long_history(), summarise=summarise, budget=600, sizer=Sizer())
    assert result.degraded


async def test_F12_05_a_retryable_provider_failure_inside_compaction_degrades() -> None:
    """And the case in between, which mutation testing found nothing covering.

    The test above raises something that is not a `ModelHTTPError` at all, so
    it never reaches the `classify` call -- which means "only *fatal* provider
    failures escape" was asserted by nothing.  A 500 from the summariser is a
    provider failure that must still degrade.
    """
    from minicodex.compaction import Sizer, compact

    async def summarise(request: SummaryRequest) -> str:
        raise http(stub.SERVER_ERROR)

    result = await compact(long_history(), summarise=summarise, budget=600, sizer=Sizer())
    assert result.degraded


# ---------------------------------------------------------------------------
# F12-06: the timeouts, in order
# ---------------------------------------------------------------------------


def test_F12_06_the_timeouts_are_strictly_nested() -> None:
    """Four clocks, written in four modules over eleven chapters.

    Before this chapter the model's own timeout was 300 seconds and so was a
    sub-task's, so a hung request and its parent's deadline expired at the same
    instant -- and which fired first decided whether the parent saw a `timeout`
    outcome or an exception.  An assertion rather than a comment, because the
    numbers live in four files and nobody editing one of them will read the
    other three.
    """
    assert SHELL_TIMEOUT < DEFAULT_TOOL_TIMEOUT
    assert DEFAULT_TOOL_TIMEOUT < DEFAULT_ATTEMPT_TIMEOUT
    assert DEFAULT_ATTEMPT_TIMEOUT + DEFAULT_POLICY.budget < DEFAULT_TASK_TIMEOUT


def test_F12_06_the_retry_budget_bounds_the_whole_turn() -> None:
    """An attempt count alone does not bound anything: four attempts of
    "whatever the server asked for" is an unbounded wait."""
    slow = Failure("retry", "rate_limit", "429", retry_after=40.0)
    assert wait_for(slow, 0, elapsed=0.0) == 40.0
    assert wait_for(slow, 1, elapsed=40.0) == 40.0
    assert wait_for(slow, 2, elapsed=80.0) is None


# ---------------------------------------------------------------------------
# F12-07: Ctrl-C during the wait
# ---------------------------------------------------------------------------


async def test_F12_07_a_cancellation_during_backoff_lands_immediately(
    stub_url: str,
) -> None:
    """`asyncio.sleep`, not `time.sleep`.

    The blocking version does not only make the interrupt late; it stops the
    event loop, and with it every other tool, sub-agent and MCP reader in the
    process.  The bug inside the backoff would be bigger than the one the
    backoff is for.
    """
    agent = Agent(
        failing(stub_url, "t11", stub.RATE_LIMITED),
        TOOLS,
        retry_policy=RetryPolicy(attempts=4, base=30.0, cap=30.0, budget=90.0),
    )

    task = asyncio.ensure_future(agent.run("go"))
    await asyncio.sleep(0.3)  # long enough to be inside the wait
    began = time.monotonic()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert time.monotonic() - began < 1.0


async def test_F12_07_a_cancellation_is_never_classified_as_a_failure(
    stub_url: str,
) -> None:
    """`except Exception` and not `except BaseException`.

    Chapter 7 paid for this one already (F07-04): `CancelledError` has been a
    `BaseException` since 3.8, and the one function whose job is turning
    failures into outputs was letting it through.  Here the mistake would run
    the other way -- a Ctrl-C classified as a failure would be *retried*.
    """

    class Cancels:
        tools = ()

        async def stream(self, messages: Any) -> Any:
            raise asyncio.CancelledError
            yield  # pragma: no cover - makes this an async generator

    agent = Agent(Cancels(), TOOLS, retry_policy=NO_WAIT)
    with pytest.raises(asyncio.CancelledError):
        await agent.run("go")


# ---------------------------------------------------------------------------
# F12-08: two audiences
# ---------------------------------------------------------------------------


def test_F12_08_every_explanation_says_what_to_do_next() -> None:
    """Chapter 3's F03-07, for a reader who can rotate a key.

    The failure line alone is what the provider wrote; the second line is the
    only part that is ours, and it is the part that decides whether the message
    gets acted on or stared at.
    """
    for recorded in (stub.BAD_API_KEY, stub.RATE_LIMITED, stub.CONTEXT_LENGTH_EXCEEDED):
        message = explain(classify(http(recorded)))
        assert len(message.splitlines()) >= 2
        assert "req_abc" in message


def test_F12_08_the_request_id_survives_classification() -> None:
    """The only string in the whole failure that lets somebody else look up
    what happened, and it was being dropped with the rest of the headers."""
    assert classify(http(stub.SERVER_ERROR)).request_id == "req_abc"


async def test_F12_08_a_child_failure_reaches_the_parent_as_prose_not_a_traceback(
    stub_url: str, tmp_path: Path
) -> None:
    """The one door in this program through which a provider error reaches a
    *model*.

    `run_task` has documented "never raises for anything the sub-agent did"
    since chapter 10, and a `ModelHTTPError` from the child's own model call
    walked straight through it, through `_run_tool`'s broad `except`, and into
    the parent's history as `Error: spawn_agent raised ModelHTTPError: HTTP 429
    from https://...` followed by a JSON blob.
    """
    from minicodex.agent import Wiring
    from minicodex.approval import AllowAll, Session
    from minicodex.composition import child_tools_builder
    from minicodex.shell import ShellSession
    from minicodex.subagent import SubAgentContext, TaskSpec, run_task

    session = Session(mode="workspace-write", approver=AllowAll())
    ctx = SubAgentContext(
        build_model=lambda schemas: failing(stub_url, "t12", *[BARE_429] * 6),
        root=tmp_path,
        session=session,
        parent_shell=ShellSession(),
        build_tools=child_tools_builder(tmp_path, session),
        wiring=Wiring(retry_policy=NO_WAIT),
        sessions_dir=tmp_path / "sessions",
    )

    result = await run_task(TaskSpec(task="say hello"), ctx)

    assert result.outcome == "error"
    rendered = result.render()
    assert "Traceback" not in rendered and "ModelHTTPError" not in rendered
    assert "could not run" in rendered
    assert "do the work yourself" in rendered


def test_F12_08_the_error_outcome_renders_without_a_before(tmp_path: Path) -> None:
    """A failed provider call has no "what it had said before that point":
    there is no before.  Rendering one would be an empty quotation with a
    heading over it."""
    rendered = TaskResult("error", "", detail="rate limit reached [rate_limit_exceeded]").render()
    assert "before that point" not in rendered
    assert rendered.startswith("[sub-agent: could not run: rate limit reached")


# ---------------------------------------------------------------------------
# F12-09: what a retry leaves behind
# ---------------------------------------------------------------------------


async def test_F12_09_retries_leave_no_trace_in_the_history(
    stub_url: str, served_requests: list[dict[str, Any]]
) -> None:
    """A conversation that survived three failures must be indistinguishable
    from one that had none.

    Not a promise about tidiness: the history is what the *next* request is
    built from, so debris in it is not a cosmetic problem, it is a message the
    model reads.
    """

    async def run(*failures: dict[str, Any]) -> Any:
        stub.reset()
        agent = Agent(
            failing(stub_url, f"t13-{len(failures)}", *failures), TOOLS, retry_policy=NO_WAIT
        )
        return await agent.run("What does __init__.py define?")

    clean = await run()
    bumpy = await run(stub.SERVER_ERROR, BARE_429, {"stream_cut": 3})

    assert bumpy.history.to_wire("chat_completions") == clean.history.to_wire("chat_completions")


async def test_F12_09_but_the_transcript_does_show_them(stub_url: str, tmp_path: Path) -> None:
    """Transparent to the history, visible in the recording.

    The two are different audiences: the model must not read about a retry, and
    the person debugging at four in the morning must be able to.
    """
    from minicodex.recorder import Recorder

    recorder = Recorder(tmp_path / "run.jsonl")
    agent = Agent(
        failing(stub_url, "t14", stub.SERVER_ERROR),
        TOOLS,
        recorder=recorder,
        retry_policy=NO_WAIT,
    )
    await agent.run("What does __init__.py define?")

    events = [json.loads(line) for line in recorder.path.read_text(encoding="utf-8").splitlines()]
    failures = [e for e in events if e["kind"] == "model_failure"]
    assert len(failures) == 1
    assert failures[0]["payload"]["kind"] == "server_error"
    assert failures[0]["payload"]["attempt"] == 0


async def test_F12_09_a_failed_attempt_writes_nothing_to_the_session_file(
    stub_url: str, tmp_path: Path
) -> None:
    """The rollout is the file a resume is rebuilt from.  A failed attempt has
    produced no item, so there is nothing to write -- which is a consequence of
    where the retry boundary sits rather than of anything written here."""
    from minicodex.rollout import RolloutWriter, SessionMeta, new_session_id, rollout_path

    meta = SessionMeta(session_id=new_session_id(), created=0.0, cwd=str(tmp_path))
    writer = RolloutWriter(rollout_path(meta.session_id, tmp_path), meta)
    try:
        agent = Agent(
            failing(stub_url, "t15", stub.SERVER_ERROR, stub.SERVER_ERROR),
            TOOLS,
            rollout=writer,
            retry_policy=NO_WAIT,
        )
        await agent.run("What does __init__.py define?")
    finally:
        writer.release()

    lines = writer.path.read_text(encoding="utf-8").splitlines()
    # header + user + assistant(call) + result + assistant(answer)
    assert len(lines) == 5


# ---------------------------------------------------------------------------
# not on the list
# ---------------------------------------------------------------------------


def test_a_crash_no_longer_leaks_the_session_lock(tmp_path: Path, capsys: Any) -> None:
    """Chapter 7 takes an `O_EXCL` lock next to the session file.

    `writer.release()` was the last line of `_ask`, so it ran on success and
    never on failure: every crashed run left a `.lock` behind for as long as
    the directory exists.  Nothing broke -- which is why eleven chapters of
    tracebacks did not turn it up.  `RolloutWriter` has had `__enter__` /
    `__exit__` since the chapter that introduced it, and `run_task` has
    released in a `finally` since chapter 10: the correct pattern already
    existed in this repository, one module over, exactly like chapter 11's
    `SYSTEMROOT`.

    Driven through `main()` rather than through `Agent`, because the leak was
    in the composition and not in the loop -- a test of `Agent` would have gone
    green against the broken version.
    """
    import socket
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer

    from minicodex.__main__ import main

    class AlwaysFails(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            body = json.dumps(stub.BAD_API_KEY["body"]).encode()
            self.send_response(401)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args: Any) -> None:
            pass

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    server = HTTPServer(("127.0.0.1", port), AlwaysFails)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        code = main(
            [
                "ask",
                "go",
                "--base-url",
                f"http://127.0.0.1:{port}/v1",
                "--session-dir",
                str(tmp_path),
                "--yes",
            ]
        )
    finally:
        server.shutdown()
        server.server_close()

    assert code == 1
    assert list(tmp_path.glob("*.lock")) == []
    assert "the model call failed" in capsys.readouterr().err


def test_the_error_path_of_the_model_client_now_has_a_test() -> None:
    """Recorded here because of how it was found rather than what it asserts.

    `grep -rn ModelHTTPError src tests` returned two lines, both in `model.py`.
    The only error path in the client had no test, no caller and no handler for
    eleven chapters -- the single `raise` that every failure in this chapter
    comes out of.
    """
    error = http(stub.BAD_API_KEY)
    assert error.status == 401
    assert error.code == "invalid_api_key"
    assert error.request_id == "req_abc"
    assert "Incorrect API key" in str(error)


def test_a_body_that_is_not_json_does_not_break_the_error_handling() -> None:
    """A proxy or a gateway does not return the provider's envelope.  It
    returns HTML, and a parser that assumes JSON turns somebody else's outage
    into a `JSONDecodeError` raised from inside our own error handling."""
    error = ModelHTTPError(
        status=502,
        url="http://gateway/v1/chat/completions",
        headers={},
        body="<html><head><title>502 Bad Gateway</title></head></html>",
    )
    assert error.code is None
    assert classify(error).disposition == "retry"
    assert "502 Bad Gateway" in str(error)
