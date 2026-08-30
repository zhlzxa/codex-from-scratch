"""Turning one real run back into a deterministic test.

Chapter -1 built the recorder for a human: when an agent misbehaves the first
question is *what did the model actually receive*, and a file on disk is the
only thing that survives the process.  Its docstring has said since then that
"chapter 14 replays it".  It could not.  Two things were missing, and both were
missing for the same reason -- the transcript was written to be **read**, not
to be **re-run**:

    tool call arguments   `{"id": ..., "name": "apply_patch"}` and nothing else.
                          The whole edit -- the payload the bug is usually
                          *in* -- was never written down.
    the failure itself    a 429 was recorded as prose (`kind`, `detail`), not
                          as the status, headers and body a second attempt
                          would have to see.

Both are fixed in `agent.py`, at the two `recorder.record()` calls, and neither
can be fixed for a recording made before this chapter.  A loader that quietly
filled the gap with `{}` would replay a run in which the model asked to patch
nothing, which is worse than refusing: it is a green test for a conversation
that never happened.  So `load()` refuses, and says which field is absent.

What a replay is faithful about, and what it is not:

  * **faithful**: the message list the loop built, every tool call and its
    arguments, the finish reason, the reported prompt tokens, and the failures
    in the order they happened -- including the retry that followed one.
  * **not faithful**: the *chunking*.  The recorder stores the assembled turn,
    so replaying it emits one `TextDelta` where the wire had forty.  A bug in
    fragment assembly (F01-01) cannot be reproduced from a recording; that is
    what `stub.py`'s byte-level recordings are for.  Two artefacts, two
    layers, and it is worth knowing which one answers which question.

The tool side is replayed from the same file without a workspace: request N+1
contains the results of turn N's calls, so `recorded_tools()` reads the outputs
out of the following request.  A replay therefore touches no files, starts no
subprocesses, and needs no API key -- everything between the two boundaries is
exercised, and nothing outside them is.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

from minicodex.agent_types import IncompleteStreamError, ToolCall, ToolFn
from minicodex.model import Completed, ModelHTTPError, StreamEvent, TextDelta, ToolCallDelta, Usage


class ReplayError(RuntimeError):
    """This recording cannot be replayed, and the message says which field."""


# ---------------------------------------------------------------------------
# The writing half.  It lives here, next to the reading half, and `agent.py`
# imports it -- which points an arrow from the loop to its own debug tooling
# and is worth one sentence of defence.  The alternative is a dict literal at
# each `recorder.record()` call site and a parser over here, which is exactly
# the arrangement that produced this chapter: two halves of one format, in two
# modules, agreeing until one of them is edited.  Chapter 1 put the wire format
# in one place for the same reason.  `replay.py` imports nothing from `agent`,
# so the arrow is not a cycle and cannot become one.
# ---------------------------------------------------------------------------


def call_record(call: ToolCall) -> dict[str, Any]:
    """One tool call, as the transcript has to store it to be re-runnable.

    `raw_arguments` and not the parsed dict, for chapter 7's reason (F07-09):
    `{"path": "x.py"}` and `{"path":"x.py"}` mean the same thing and are not
    the same bytes, and a replay that re-renders the parsed form produces a
    request the provider never saw.
    """
    return {"id": call.call_id, "name": call.name, "arguments": call.raw_arguments}


def failure_record(exc: BaseException) -> dict[str, Any]:
    """The parts of a failure that a *second attempt* would have seen.

    `Failure` (retry.py) is the classification -- a slug and a sentence for a
    human.  This is the evidence it was made from.  Keeping only the
    classification is the same mistake as keeping only a tool call's name: it
    is enough to read and not enough to re-run, and which of the two you needed
    is decided months later by somebody looking at a bug that happens once a
    week.
    """
    if isinstance(exc, ModelHTTPError):
        return {"status": exc.status, "url": exc.url, "headers": exc.headers, "body": exc.body}
    # Not an HTTP answer at all.  `status: 0` rather than a missing key,
    # because "no status" and "this recording predates chapter 14" have to
    # stay distinguishable -- the loader refuses on the second.
    return {"status": 0}


class ReplayDrift(BaseException):
    """The code under test no longer sends what it sent when this was recorded.

    A `BaseException`, which looks like an over-reaction and is not.  The first
    version subclassed `AssertionError`, and the first real drift came out of
    the CLI as this:

        minicodex.retry.ModelFailed: the model call failed: ReplayDrift: ...

    `_respond` catches `Exception` and hands whatever it caught to `classify()`
    -- that is the net that turns every provider failure into a retry decision,
    and a test double's assertion went into it like everything else.  Chapter
    12's conservative default (an unrecognised failure is `fatal`) is the only
    reason this stopped instead of being retried five times.

    This is chapter 7's F07-04 shape for the third time: a broad `except` is
    the right tool for failures and the wrong tool for signals, and the fix is
    on the signal's side.  A double's assertion has to be louder than the code
    it is testing.
    """


class ReplayExhausted(ReplayError):
    """The loop asked for one more turn than the recording has."""


@dataclass(frozen=True)
class RecordedAttempt:
    """One trip to the model boundary: what was sent, and what came back.

    An *attempt*, not a turn, because chapter 12's retry loop can make several
    per turn and the interesting recordings are exactly those: the 429, the
    wait, and the answer that followed.  Replaying a turn would silently drop
    the failure and with it the only reason the recording was kept.
    """

    turn: int
    attempt: int
    request: tuple[dict[str, Any], ...]
    text: str = ""
    tool_calls: tuple[ToolCallDelta, ...] = ()
    finish_reason: str | None = None
    prompt_tokens: int | None = None
    # Present instead of the three fields above when this attempt failed.
    failure: dict[str, Any] | None = None


@dataclass(frozen=True)
class Recording:
    path: Path
    attempts: tuple[RecordedAttempt, ...]
    # What produced the conversation, as opposed to the conversation itself.
    # Chapter 7 learned this once already: `SessionMeta` records cwd, model,
    # sandbox mode and policy "because the history mentions none of them while
    # depending on all of them" (F07-07).  Nothing propagated that to the
    # recorder, so for thirteen chapters the transcript could not say which
    # model wrote it.  `None` for a recording made before this chapter.
    config: dict[str, Any] | None = None
    # A session that compacted cannot be replayed: `make_summariser` calls the
    # model directly and that call never reaches the recorder.  Refused rather
    # than half-served, for the reason `load()` refuses a missing argument.
    compacted: bool = False

    @property
    def turns(self) -> int:
        return len({a.turn for a in self.attempts})

    def describe(self) -> str:
        failed = sum(1 for a in self.attempts if a.failure is not None)
        calls = sum(len(a.tool_calls) for a in self.attempts)
        return (
            f"{self.turns} turn(s), {len(self.attempts)} attempt(s) "
            f"({failed} failed), {calls} tool call(s)"
        )


def load(path: Path | str) -> Recording:
    """Read a transcript, or refuse and say what is missing.

    Events arrive interleaved -- `request`, then either `response` or
    `model_failure`, plus `compaction` and `nudge` lines this does not care
    about -- so the pairing is done on `(turn, attempt)` rather than on
    adjacency.  A `compaction` event between a request and its response is
    normal and must not break the pairing; adjacency would have made it.
    """
    path = Path(path)
    requests: dict[tuple[int, int], tuple[dict[str, Any], ...]] = {}
    outcomes: dict[tuple[int, int], tuple[str, dict[str, Any]]] = {}
    config: dict[str, Any] | None = None
    compacted = False
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            # A recording is written by a process that may be killed; the
            # surviving prefix is still a recording.  Chapter 7's rule.
            break
        kind, payload = event.get("kind"), event.get("payload", {})
        if kind == "config":
            config = payload
        elif kind == "compaction":
            compacted = True
        elif kind == "request":
            key = (payload["turn"], payload["attempt"])
            requests[key] = tuple(payload["messages"])
        elif kind in ("response", "model_failure"):
            # A `response` carries no `attempt`: it is written once the attempt
            # that worked has already returned, so it belongs to the last
            # request issued for its turn.
            attempt = payload.get("attempt")
            if attempt is None:
                attempt = max(
                    (a for (t, a) in requests if t == payload["turn"]),
                    default=0,
                )
            # Two levels, two dicts.  The first version flattened them with
            # `{"kind": kind, **payload}` -- and a `model_failure` payload has
            # its own `kind` (the classification slug, "rate_limit"), which
            # overwrote the envelope's.  Every recorded failure then read as a
            # recorded *response* with no text and no calls: no error anywhere,
            # a replay that quietly turns a 429 into a blank answer.
            outcomes[(payload["turn"], attempt)] = (kind, payload)

    attempts: list[RecordedAttempt] = []
    for key in sorted(requests):
        found = outcomes.get(key)
        if found is None:
            # The process died between sending and hearing back.  That is a
            # real recording of a real event, and there is nothing to replay
            # after it, so it ends the replay rather than failing it.
            break
        event, outcome = found
        turn, attempt = key
        if event == "model_failure":
            attempts.append(
                RecordedAttempt(turn, attempt, requests[key], failure=_failure(outcome, path))
            )
            continue
        attempts.append(
            RecordedAttempt(
                turn,
                attempt,
                requests[key],
                text=outcome.get("text", ""),
                tool_calls=_calls(outcome, path),
                finish_reason=outcome.get("finish_reason"),
                prompt_tokens=outcome.get("actual_prompt_tokens"),
            )
        )
    if not attempts:
        raise ReplayError(f"{path}: no complete request/response pair in this recording")
    return Recording(path, tuple(attempts), config=config, compacted=compacted)


def _calls(payload: dict[str, Any], path: Path) -> tuple[ToolCallDelta, ...]:
    calls = []
    for index, call in enumerate(payload.get("tool_calls", [])):
        if "arguments" not in call:
            raise ReplayError(
                f"{path}: tool call {call.get('name')!r} was recorded without its "
                "arguments, so this run cannot be replayed. Recordings made before "
                "chapter 14 stored only the id and the name."
            )
        calls.append(
            ToolCallDelta(
                call_id=call["id"],
                index=index,
                name=call["name"],
                arguments=call["arguments"],
            )
        )
    return tuple(calls)


def _failure(payload: dict[str, Any], path: Path) -> dict[str, Any]:
    if "status" not in payload:
        raise ReplayError(
            f"{path}: a {payload.get('kind')} failure was recorded as prose only "
            f"('{payload.get('detail', '')[:60]}...'), without the status and body "
            "a second attempt would see. Recordings made before chapter 14 cannot "
            "replay a failure."
        )
    return payload


def divergence(sent: Sequence[dict[str, Any]], recorded: Sequence[dict[str, Any]]) -> str | None:
    """The first place two message lists stop agreeing, in one line.

    Not `assert sent == recorded`: the useful output of a failing snapshot is
    *where*, and a full dump of two 6000-token message lists is not that.  The
    fields are compared one at a time so the report can name the one that
    moved -- a role, a missing message, or a body that changed.
    """
    for index, (a, b) in enumerate(zip(sent, recorded, strict=False)):
        if a.get("role") != b.get("role"):
            return f"message {index}: role was {b.get('role')!r}, now {a.get('role')!r}"
        if a != b:
            for key in sorted(set(a) | set(b)):
                if a.get(key) != b.get(key):
                    was, now = _window(b.get(key), a.get(key))
                    return (
                        f"message {index} ({a.get('role')}): {key} changed\n"
                        f"      was: {was}\n"
                        f"      now: {now}"
                    )
    if len(sent) != len(recorded):
        counts = f"{len(recorded)} messages recorded, {len(sent)} sent"
        if len(sent) < len(recorded):
            return f"{counts}; missing {recorded[len(sent)].get('role')!r}"
        return f"{counts}; extra {sent[len(recorded)].get('role')!r}"
    return None


def _short(value: Any) -> str:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    text = text.replace("\n", "\\n")
    return text if len(text) <= 90 else text[:87] + "..."


def _window(was: Any, now: Any, *, before: int = 20, width: int = 70) -> tuple[str, str]:
    """Two excerpts starting where the two values first disagree.

    The first version of this printed the first 90 characters of each, which on
    the first real drift -- one sentence appended to a system prompt -- printed
    the *same* 90 characters twice under the labels `was` and `now`.  A diff
    that shows the part that did not change is not a diff.
    """
    a = (was if isinstance(was, str) else json.dumps(was, ensure_ascii=False)).replace("\n", "\\n")
    b = (now if isinstance(now, str) else json.dumps(now, ensure_ascii=False)).replace("\n", "\\n")
    at = next((i for i in range(min(len(a), len(b))) if a[i] != b[i]), min(len(a), len(b)))
    start = max(0, at - before)
    lead = "..." if start else ""
    return (
        lead + a[start : start + width] + ("..." if start + width < len(a) else ""),
        lead + b[start : start + width] + ("..." if start + width < len(b) else ""),
    )


class RecordedModel:
    """A `Model` that answers exactly what one real provider answered once.

    `strict=True` (the default) also makes it a snapshot: every request is
    compared with the one that was recorded, and the first difference raises
    `ReplayDrift`.  That is the same artefact doing two jobs, and it is the
    reason this is worth building at all -- a hand-written golden transcript
    covers the wiring somebody remembered to write a fixture for, and a
    recording covers whatever the program actually did last Tuesday.
    """

    def __init__(self, recording: Recording, *, strict: bool = True) -> None:
        self.recording = recording
        self.strict = strict
        self.sent: list[list[dict[str, Any]]] = []
        self._next = 0
        # `Agent._sizer` reads this off whatever object it was handed, so a
        # replay sizes its requests the way the real client did.
        self.tools: list[dict[str, Any]] = []

    async def stream(self, messages: Sequence[dict[str, Any]]) -> AsyncIterator[StreamEvent]:
        if self._next >= len(self.recording.attempts):
            raise ReplayExhausted(
                f"{self.recording.path}: the loop asked for attempt "
                f"{self._next + 1}; the recording has {len(self.recording.attempts)}"
            )
        step = self.recording.attempts[self._next]
        self._next += 1
        self.sent.append([dict(m) for m in messages])

        if self.strict:
            difference = divergence(messages, step.request)
            if difference is not None:
                raise ReplayDrift(
                    f"turn {step.turn} attempt {step.attempt}: the request is no "
                    f"longer what was recorded in {self.recording.path.name}\n"
                    f"      {difference}"
                )

        if step.failure is not None:
            raise _raise_from(step.failure)

        if step.text:
            yield TextDelta(step.text)
        for call in step.tool_calls:
            yield call
        if step.prompt_tokens is not None:
            # completion_tokens is not recorded and nothing reads it: chapter 6
            # calibrates on the prompt side, which is the side the estimator
            # estimates.  Zero rather than a guess.
            yield Usage(prompt_tokens=step.prompt_tokens, completion_tokens=0)
        yield Completed(step.finish_reason)


def _raise_from(failure: dict[str, Any]) -> BaseException:
    status = failure.get("status")
    if status == 0:
        # Not an HTTP failure.  Two shapes have ever reached `classify` from
        # below: a stream that stopped before `[DONE]`, and a transport error.
        if failure.get("kind") == "incomplete_stream":
            return IncompleteStreamError(failure.get("detail", ""))
        return httpx.TransportError(failure.get("detail", ""))
    return ModelHTTPError(
        status=int(status or 500),
        url=failure.get("url", "replay://recorded"),
        headers=failure.get("headers") or {},
        body=failure.get("body", ""),
    )


@dataclass
class ReplayedTools:
    """The tool table as the recording saw it: same calls in, same text out.

    Outputs are recovered from the *next* request, which is where a tool result
    ends up one moment after it is produced.  Keyed by `(name, arguments)` and
    consumed in order, so a run that reads the same file twice replays both
    reads and a run that reads two different files cannot get them the wrong
    way round.

    A call the recording has no output for is not an error here -- it returns
    a message saying so, because a tool that raises would end the run inside
    the machinery under test rather than at the boundary where the gap is.

    Known bound, raised in review: consuming a queue in order assumes the
    *execution* order of two identical calls is the order they were recorded
    in.  Chapter 8's scheduler can put two calls in different batches, so that
    assumption is not free.  It is left as an assumption rather than defended,
    because the failure is loud: a call would receive the next call's output,
    and `strict` mode reports the divergence on the very next request.
    """

    outputs: dict[tuple[str, str], list[str]] = field(default_factory=dict)
    missed: list[str] = field(default_factory=list)

    def handler(self, name: str) -> ToolFn:
        async def run(arguments: dict[str, Any]) -> str:
            key = (name, json.dumps(arguments, sort_keys=True, ensure_ascii=False))
            queue = self.outputs.get(key)
            if queue:
                return queue.pop(0)
            self.missed.append(f"{name}({_short(arguments)})")
            return f"Error: this call was not in the recording ({name})."

        return run

    def table(self, names: Iterable[str]) -> dict[str, ToolFn]:
        return {name: self.handler(name) for name in names}


def recorded_tools(recording: Recording) -> ReplayedTools:
    """Rebuild the tool side of a run out of the requests that followed it."""
    by_id: dict[str, str] = {}
    calls: dict[str, tuple[str, str]] = {}
    for step in recording.attempts:
        for message in step.request:
            if message.get("role") == "assistant":
                for call in message.get("tool_calls") or []:
                    function = call.get("function", {})
                    arguments = function.get("arguments", "")
                    try:
                        parsed = json.loads(arguments) if arguments else {}
                    except json.JSONDecodeError:
                        parsed = {}
                    calls[call.get("id", "")] = (
                        function.get("name", ""),
                        json.dumps(parsed, sort_keys=True, ensure_ascii=False),
                    )
            elif message.get("role") == "tool":
                by_id[message.get("tool_call_id", "")] = message.get("content", "")

    replayed = ReplayedTools()
    for call_id, key in calls.items():
        if call_id in by_id:
            replayed.outputs.setdefault(key, []).append(by_id[call_id])
    return replayed


def names_called(recording: Recording) -> list[str]:
    """Every tool name the recording mentions, in first-seen order."""
    seen: list[str] = []
    for step in recording.attempts:
        for call in step.tool_calls:
            if call.name not in seen:
                seen.append(call.name)
    return seen
