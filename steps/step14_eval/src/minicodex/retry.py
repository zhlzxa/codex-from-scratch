"""Deciding what a failed request means, and whether to send it again.

Three chapters of measurement were only possible because the *probe scripts*
had a retry loop:

    # probe_subagent.py, written in chapter 10
    async def _retry(make, attempts=4):
        ...
        except (httpx.ConnectError, httpx.RemoteProtocolError, httpx.ReadError):
            await asyncio.sleep(2 * (attempt + 1))

Two copies of it, in two probes, with a comment saying "not a fix for anything
-- chapter 12 is where retries are designed".  The shipped program had none:
one dropped connection ended the run, printed a traceback, and left whatever
the model had already done on disk with no note anywhere saying why it stopped.

The naive repair -- wrap the call in `for attempt in range(5)` -- is worse than
it looks, and the reason is measured rather than argued.  Against a request
the provider will never accept (a tool name with a space in it), five attempts
sent five byte-identical bodies, took nine seconds, and got the same answer
five times.  Against a real 429 the account's own headers said `retry-after:
46`, and a 1-2-4-8-16 backoff makes all five of its attempts inside 31 seconds
-- every one of them before the window opens, and every one of them another
request against the limit that is already exhausted.

So this module answers one question -- *what kind of failure is this* -- and
the answer has three values, because there are three different things to do:

    retry   the same request may work later
    shrink  the request is too big; the conversation must lose weight first
    fatal   sending this again cannot succeed

`status` cannot produce that answer: `context_length_exceeded` and "your tool
name has a space in it" are both HTTP 400 with `type: invalid_request_error`,
and they are the two extremes of the table.  `code` produces it.
"""

from __future__ import annotations

import random
import re
from dataclasses import dataclass
from typing import Literal

import httpx

from minicodex.agent_types import IncompleteStreamError
from minicodex.model import ModelHTTPError

Disposition = Literal["retry", "shrink", "fatal"]


@dataclass(frozen=True)
class Failure:
    """One failed attempt, in the terms the loop can act on.

    `kind` is a slug rather than the provider's `code` verbatim: the loop and
    the tests need a word that means the same thing across providers, and the
    provider's own `code` is kept in `detail` for the human who has to go and
    look it up.

    `retry_after` is seconds, and it comes from the server or it is `None`.  It
    is deliberately not "our backoff, but at least this long": if the server
    says 45 seconds and the caller's whole budget is 30, the answer is to stop
    -- waiting 30 and asking anyway is the behaviour that extends the outage.
    """

    disposition: Disposition
    kind: str
    detail: str
    retry_after: float | None = None
    request_id: str | None = None
    # (limit, used), when the server said so.  A rejected request is the only
    # place a provider ever states the true token count of a request it did not
    # process -- there is no usage chunk on a failure, so chapter 6's
    # calibration has no other source for the one request that mattered most.
    stated_tokens: tuple[int, int] | None = None


# Transport failures that are worth another attempt: the request may not have
# arrived, or the answer may not have got back.  `httpx.TransportError` covers
# the timeouts, the network errors and `RemoteProtocolError` (a server that
# hung up mid-stream).  Two of its subclasses are excluded because they are not
# weather, they are bugs in this program: `LocalProtocolError` is a request we
# built wrongly, and `UnsupportedProtocol` is a URL with no scheme.  Retrying
# either produces the same failure at a slower rate.
_OUR_FAULT = (httpx.LocalProtocolError, httpx.UnsupportedProtocol)

# Provider `code` values, from the five recorded failures plus the two shapes
# every provider of this API sends.  Anything not listed falls through to the
# status-code rules below, which are deliberately conservative in the fatal
# direction: see `classify`.
_BY_CODE: dict[str, tuple[Disposition, str]] = {
    "rate_limit_exceeded": ("retry", "rate_limit"),
    "context_length_exceeded": ("shrink", "context_length"),
    "invalid_api_key": ("fatal", "auth"),
    "model_not_found": ("fatal", "unknown_model"),
    "insufficient_quota": ("fatal", "quota"),
    "server_error": ("retry", "server_error"),
}

# "This model's maximum context length is 128000 tokens. However, your messages
# resulted in 160008 tokens."  Two integers in one sentence, in that order.
_TOKENS = re.compile(r"maximum context length is (\d+) tokens.*?resulted in (\d+) tokens", re.S)


def classify(exc: BaseException) -> Failure:
    """Turn whatever came back into one of three decisions.

    The default for something unrecognised is `fatal`, which is the opposite of
    chapter 5's rule for unrecognised shell syntax ("unknown asks").  The two
    situations look alike and are not: chapter 5 has a human at the prompt to
    ask, and this loop has nobody.  The two mistakes are not symmetric either.
    Calling something fatal that was retryable costs one run and prints the
    reason.  Calling something retryable that was fatal costs the quota, in
    silence, and the measured cost is not one request: a rejected 160k-token
    request still debited its 160k tokens from the account's token bucket
    (remaining went 199,996 -> 19,998), so the retries of a request that cannot
    succeed are what pushes the *next* request into a 429.
    """
    if isinstance(exc, ModelHTTPError):
        return _http(exc)
    if isinstance(exc, IncompleteStreamError):
        # Nothing was committed: `_collect` returns only on `[DONE]`, so a
        # stream that died halfway left no half-turn anywhere (F00-04, and
        # F12-03 for free).  The whole attempt can be made again.
        return Failure("retry", "incomplete_stream", str(exc))
    if isinstance(exc, _OUR_FAULT):
        return Failure("fatal", "client_bug", f"{type(exc).__name__}: {exc}")
    if isinstance(exc, httpx.TransportError):
        return Failure("retry", "transport", f"{type(exc).__name__}: {exc}")
    return Failure("fatal", "unexpected", f"{type(exc).__name__}: {exc}")


def _http(exc: ModelHTTPError) -> Failure:
    detail = exc.message or exc.body[:300]
    if exc.code:
        detail = f"{detail} [{exc.code}]"

    disposition, kind = _BY_CODE.get(exc.code or "", ("", ""))
    if not disposition:
        if exc.status == 429:
            disposition, kind = "retry", "rate_limit"
        elif exc.status >= 500 or exc.status == 408:
            disposition, kind = "retry", "server_error"
        else:
            disposition, kind = "fatal", f"http_{exc.status}"

    stated = None
    if kind == "context_length" and (m := _TOKENS.search(exc.message or "")):
        stated = (int(m.group(1)), int(m.group(2)))

    return Failure(
        disposition,  # type: ignore[arg-type]
        kind,
        detail,
        retry_after=_retry_after(exc.headers),
        request_id=exc.request_id,
        stated_tokens=stated,
    )


def _retry_after(headers: dict[str, str]) -> float | None:
    """How long the server asked for, in seconds.

    Both headers are read because both were sent: `retry-after: 46` alongside
    `retry-after-ms: 45175`.  The whole-second one is rounded *up*, so the
    millisecond one is preferred where it exists -- not for the 0.8 seconds,
    but because a number that has been rounded is a number somebody has already
    decided is approximate.

    RFC 9110 also allows an HTTP date here.  No provider measured in this book
    has ever sent one, so it is not parsed: an unreadable header returns `None`
    and the caller falls back to its own backoff, which is the same thing it
    does for a 429 with no header at all.
    """
    for name, scale in (("retry-after-ms", 0.001), ("retry-after", 1.0)):
        raw = headers.get(name)
        if raw is None:
            continue
        try:
            return max(0.0, float(raw.strip()) * scale)
        except ValueError:
            continue
    return None


@dataclass(frozen=True)
class RetryPolicy:
    """How many times, how long, and how long in total.

    `budget` is the field that does the real work and is the one a plain
    attempt count does not have.  Four attempts of "whatever the server asked
    for" is an unbounded wait: chapter 10's sub-agent gets 300 seconds for a
    whole task, and a parent that sits in backoff for six minutes has spent a
    turn budget on sleeping.  With a budget, the answer to "the server said 45s
    and I have 20s left" is to stop and say so, rather than to wait 20 and ask
    anyway.

    It is measured as **wall clock for the whole turn**, not as time spent
    sleeping.  The first version counted only the waits, and against a hanging
    server that bounds nothing at all -- four attempts that each time out spend
    eight minutes without a single `sleep`.  What that buys is the one
    arithmetic statement this program can make about how long a turn takes: no
    new attempt starts after `budget`, and an attempt lasts at most
    `DEFAULT_ATTEMPT_TIMEOUT`, so a turn ends within `budget + attempt` --
    90 + 120 = 210 seconds, inside the 300 a sub-task gets.

    The defaults are nested inside the timeouts that already exist (F12-06):

        shell command      30s   (chapter 2)
        MCP tool call      60s   (chapter 9)
        one model attempt 120s   (this chapter; it was 300)
        retry budget       90s   (this chapter)
        one sub-task      300s   (chapter 10)

    120 + 90 < 300, so a sub-agent's model call can fail, back off and succeed
    inside the deadline its parent gave it.  Before this chapter the model's
    own timeout was 300s -- exactly the sub-task deadline -- which meant a
    hung request and the deadline expired at the same instant, and which of the
    two happened first decided whether the parent saw a timeout or an
    exception.
    """

    attempts: int = 4
    base: float = 1.0
    cap: float = 30.0
    budget: float = 90.0


DEFAULT_POLICY = RetryPolicy()


def wait_for(
    failure: Failure,
    attempt: int,
    policy: RetryPolicy = DEFAULT_POLICY,
    *,
    elapsed: float = 0.0,
    jitter: bool = True,
) -> float | None:
    """How long to wait before attempt number `attempt + 1`, or `None` to stop.

    `None` means the same thing in both of its cases -- out of budget, and "the
    server asked for longer than the whole budget" -- because the caller does
    the same thing with either: it gives up and reports the failure it already
    has.  What differs is the sentence a human reads, and that is `explain()`'s
    job, not this function's.

    The *attempt count* is deliberately not checked here.  It was, for about an
    hour, and mutation testing found the check dead: `Agent._respond` iterates
    `range(policy.attempts)`, so deleting the guard from this function changed
    no behaviour and broke no test.  Two enforcement points for one rule means
    one of them is decoration, and the one to keep is the loop -- it is what
    the reader of the loop can see.

    The jitter is half-to-full rather than none, and it is not superstition:
    this program can have a parent and several sub-agents talking to one
    provider through one account, and a shared limit plus a shared clock means
    they all wake up together.  `jitter=False` exists so tests can assert the
    schedule instead of a range.
    """
    if failure.retry_after is not None:
        wait = failure.retry_after
    else:
        wait = min(policy.cap, policy.base * 2**attempt)
        if jitter:
            wait *= random.uniform(0.5, 1.0)

    if elapsed + wait > policy.budget:
        return None
    return wait


def explain(failure: Failure, *, waited: float = 0.0, attempts: int = 1) -> str:
    """What a human is told when the run ends here.

    Chapter 3 measured the model-facing version of this (F03-07): an error
    naming only the failure gets retried verbatim, and one naming the failure,
    the cause and the next action does not.  The reader here is a person, so
    the "next action" is a different kind of thing -- a person can rotate a
    key, top up an account or use a smaller window, and none of those are
    available to a model.  What does not change is that a message with no next
    action in it is a message that gets stared at.

    The request id is included whenever there is one.  It is the only string in
    the whole failure that lets somebody else look up what happened, and it was
    being dropped with the rest of the headers for eleven chapters.
    """
    action = _ACTIONS.get(failure.kind, "")
    lines = [f"the model call failed: {failure.detail}"]
    if attempts > 1:
        lines.append(f"gave up after {attempts} attempts and {waited:.0f}s of waiting")
    elif failure.retry_after is not None:
        lines.append(f"the server asked for {failure.retry_after:.0f}s, longer than this run waits")
    if action:
        lines.append(action)
    if failure.request_id:
        lines.append(f"provider request id: {failure.request_id}")
    return "\n".join(lines)


class ModelFailed(RuntimeError):
    """The end of a run that could not get an answer out of the provider.

    One exception type for all three endings -- fatal, out of attempts, out of
    budget -- because everything above the loop does the same thing with it:
    stop, and say `str(exc)`.  The distinction that matters is carried inside,
    on `.failure`, for the two callers that do look: `__main__` prints it, and
    `subagent.run_task` turns it into a `TaskResult` instead of letting a
    traceback reach a *model*.

    `str(exc)` is already the human-readable explanation, so a caller that does
    the laziest possible thing still prints something useful.  That is not
    politeness: for eleven chapters the laziest possible thing was what every
    caller did, and what it printed was a twenty-line traceback.
    """

    def __init__(self, failure: Failure, *, attempts: int = 1, waited: float = 0.0) -> None:
        self.failure = failure
        self.attempts = attempts
        self.waited = waited
        super().__init__(explain(failure, waited=waited, attempts=attempts))


_ACTIONS: dict[str, str] = {
    "auth": "check OPENAI_API_KEY, or pass --provider ollama to run against a local model.",
    "unknown_model": "check --model against the models this account can use.",
    "quota": "this account is out of credit; the request will not succeed until that changes.",
    "rate_limit": "wait for the window to reset, or run against a provider with more headroom.",
    "context_length": (
        "the conversation no longer fits. Pass --context-window so compaction can run, "
        "or start a new session."
    ),
    "client_bug": "this one is ours, not the provider's -- the request was built wrongly.",
    "transport": "the connection did not survive; check the network or the base URL.",
    "server_error": "the provider is having trouble; this one is worth trying again later.",
}


__all__ = [
    "DEFAULT_POLICY",
    "Disposition",
    "Failure",
    "ModelFailed",
    "RetryPolicy",
    "classify",
    "explain",
    "wait_for",
]
