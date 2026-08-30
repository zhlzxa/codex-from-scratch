"""Talking to a model over HTTP, and normalising what comes back.

Two providers, both speaking "the OpenAI chat completions API", disagree about
how a tool call arrives.  Recorded 2026-08-06:

    Ollama (gemma4:31b) -- one chunk, arguments complete:
      "tool_calls":[{"id":"call_yfo64477","index":0,"type":"function",
                     "function":{"name":"read_file",
                                 "arguments":"{\"path\":\"a.py\"}"}}]

    OpenAI (gpt-4o-mini) -- fourteen chunks, and only the first carries the
    id and the name:
      "tool_calls":[{"index":0,"id":"call_bqv6...","type":"function",
                     "function":{"name":"read_file","arguments":""}}]
      "tool_calls":[{"index":0,"function":{"arguments":"{\""}}]
      "tool_calls":[{"index":0,"function":{"arguments":"path"}}]
      ...
      "tool_calls":[{"index":0,"function":{"arguments":"\"}"}}]

Chapter 0 assumed the first shape and overwrote by index, which against OpenAI
leaves `name=""` and `arguments='"}'`.

The fix is not to make callers handle both.  It is to make the difference stop
here: fragments are buffered inside `stream()` and a `ToolCallDelta` is emitted
only once it is whole.  Everything above this module sees one shape.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass, field
from typing import Any

import httpx

OLLAMA_BASE_URL = "http://localhost:11434/v1"
OPENAI_BASE_URL = "https://api.openai.com/v1"

# How long one attempt may take.  It was 300 seconds, chosen when this was the
# only clock in the program, and 300 is also `subagent.DEFAULT_TASK_TIMEOUT` --
# so a child whose model call hung reached its own deadline and the request's
# deadline at the same instant, and which of the two fired first decided
# whether the parent saw a `timeout` outcome or an exception.  Chapter 12 makes
# the nesting strict: 120 for one attempt, plus a 90-second retry budget, fits
# inside the 300 a sub-task gets.  `test_F12_06_*` asserts the ordering rather
# than leaving it to whoever edits one of the numbers next.
DEFAULT_ATTEMPT_TIMEOUT = 120.0


@dataclass(frozen=True)
class TextDelta:
    """A slice of prose.  Not a token, not a word -- whatever the server sent."""

    text: str


@dataclass(frozen=True)
class ToolCallDelta:
    """One complete tool call, assembled.

    Still called `Delta` because that is the field it arrives in.  It is emitted
    only when the whole call has been received, however many chunks that took.
    """

    call_id: str
    index: int
    name: str
    arguments: str


@dataclass(frozen=True)
class Completed:
    """The `[DONE]` sentinel, carrying the `finish_reason` seen before it."""

    reason: str | None


@dataclass(frozen=True)
class Usage:
    """What the server says the request actually cost.

    The only ground truth about token counts this program will ever have, and
    chapter 6 needs it: every local estimate was measured 34-38% low, always in
    the direction that lets a window overflow before compaction fires.

    It has to be asked for.  A streaming request returns **no usage at all**
    unless `stream_options.include_usage` is set -- verified against
    gpt-4o-mini: 0 usage chunks without it, 1 with it.  That is the quiet
    version of this fault.  Nothing errors; the calibration source simply never
    arrives, the ratio stays 1.0, and the estimate stays a third low forever.
    """

    prompt_tokens: int
    completion_tokens: int


StreamEvent = TextDelta | ToolCallDelta | Completed | Usage


class ModelHTTPError(RuntimeError):
    """The server answered, but not with a stream.

    For eleven chapters this was one formatted string, and every caller that
    wanted to know anything about the failure would have had to parse it back
    out.  Nobody did, because nobody caught it: the only error path in this
    module had no test and no handler, and a 401 reached the user as a
    twenty-line traceback ending in a JSON blob.

    Parsed here rather than by whoever catches it, for chapter 1's reason: this
    is a trust boundary, and the whole point of a boundary is that the shape
    outside it is dealt with exactly once.  Four fields are what the four
    recorded failures have in common (`probe_retry.py shapes`):

        401  code=invalid_api_key         type=invalid_request_error
        404  code=model_not_found         type=invalid_request_error
        400  code=invalid_value           type=invalid_request_error
        400  code=context_length_exceeded type=invalid_request_error
        429  code=rate_limit_exceeded     type=tokens

    Two of those are HTTP 400 with the same `type`, and one of them must be
    retried while the other must never be sent again -- so `status` alone
    cannot classify a failure and neither can `type`.  `code` can.

    The body is kept whole as well as parsed.  A proxy or a gateway does not
    return this envelope at all; it returns HTML, and a parser that assumes
    JSON turns somebody else's outage into a `JSONDecodeError` from inside our
    own error handling.
    """

    def __init__(self, *, status: int, url: str, headers: Any, body: str) -> None:
        self.status = status
        self.url = url
        self.headers = {str(k).lower(): str(v) for k, v in dict(headers).items()}
        self.body = body
        error: dict[str, Any] = {}
        try:
            payload = json.loads(body)
        except ValueError:  # includes JSONDecodeError; see the docstring
            payload = None
        if isinstance(payload, dict) and isinstance(payload.get("error"), dict):
            error = payload["error"]
        self.code: str | None = str(error.get("code") or "") or None
        self.type: str | None = str(error.get("type") or "") or None
        self.message: str | None = str(error.get("message") or "").strip() or None
        # The one field a human needs and a program cannot reconstruct.  It is
        # on every response measured, success and failure alike, and it was
        # being thrown away with the rest of the headers.
        self.request_id = self.headers.get("x-request-id")
        super().__init__(f"HTTP {status} from {url}: {self.message or body[:500]}")


@dataclass
class _PartialCall:
    """One tool call under construction.

    Mutable and private: it exists only between the first fragment and `[DONE]`.
    Nothing outside this module ever sees one.
    """

    index: int
    call_id: str | None = None
    name: str | None = None
    parts: list[str] = field(default_factory=list)

    def absorb(self, raw: dict[str, Any]) -> None:
        # Only the first fragment carries id and name; later ones carry neither,
        # so every field is written conditionally rather than assigned.
        if raw.get("id"):
            self.call_id = raw["id"]
        fn = raw.get("function") or {}
        if fn.get("name"):
            self.name = fn["name"]
        self.parts.append(fn.get("arguments") or "")

    def finish(self) -> ToolCallDelta:
        return ToolCallDelta(
            # A call with no id is a provider bug, not a shape to support: the
            # fallback keeps the pairing invariant satisfiable rather than
            # pretending the id was there.
            call_id=self.call_id or f"call_{self.index}",
            index=self.index,
            name=self.name or "",
            arguments="".join(self.parts),
        )


class ChatCompletionsModel:
    """A client for any server speaking /v1/chat/completions."""

    def __init__(
        self,
        *,
        base_url: str = OLLAMA_BASE_URL,
        model: str = "gemma4:31b-cloud",
        api_key: str | None = None,
        tools: list[dict[str, Any]] | None = None,
        extra_body: dict[str, Any] | None = None,
        timeout: float = DEFAULT_ATTEMPT_TIMEOUT,
        report_usage: bool = True,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.api_key = api_key
        self.tools = tools or []
        self.extra_body = extra_body or {}
        self.timeout = timeout
        # A flag rather than always-on, because it is a request field and not
        # every server speaking this API has to understand it.  Default on: the
        # cost of asking is one key in the body, and the cost of not asking is
        # a compaction trigger that never calibrates.
        self.report_usage = report_usage
        # A seam for tests, added because the alternative was worse.  The first
        # test for the empty-`choices` chunk mirrored the parsing loop below
        # into the test file instead of driving it -- so deleting the guard
        # from *this* module left that test green.  A test that reimplements
        # the code under test measures the copy.  With a transport, the bytes
        # go through the real `stream()`.
        self.transport = transport

    def request_body(self, messages: Sequence[dict[str, Any]]) -> dict[str, Any]:
        """The exact JSON that will be posted.

        Separate from `stream()` so it can be printed, recorded, diffed and
        asserted on without making a network call.
        """
        body: dict[str, Any] = {
            "model": self.model,
            "messages": list(messages),
            "stream": True,
        }
        if self.tools:
            body["tools"] = self.tools
        if self.report_usage:
            body["stream_options"] = {"include_usage": True}
        body.update(self.extra_body)
        return body

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers

    async def stream(self, messages: Sequence[dict[str, Any]]) -> AsyncIterator[StreamEvent]:
        url = f"{self.base_url}/chat/completions"
        finish_reason: str | None = None
        pending: dict[int, _PartialCall] = {}

        async with httpx.AsyncClient(timeout=self.timeout, transport=self.transport) as client:
            async with client.stream(
                "POST", url, json=self.request_body(messages), headers=self._headers()
            ) as resp:
                if resp.status_code != 200:
                    detail = (await resp.aread()).decode("utf-8", "replace")[:2000]
                    raise ModelHTTPError(
                        status=resp.status_code,
                        url=url,
                        headers=resp.headers,
                        body=detail,
                    )

                async for line in resp.aiter_lines():
                    if not line.strip() or not line.startswith("data: "):
                        continue
                    payload = line[len("data: ") :]

                    if payload == "[DONE]":
                        # Tool calls are emitted here, not as they arrive: only
                        # now is every fragment known to have been received.
                        for index in sorted(pending):
                            yield pending[index].finish()
                        yield Completed(finish_reason)
                        return

                    chunk = json.loads(payload)

                    # The usage chunk arrives with `"choices": []`, so the
                    # `chunk["choices"][0]` that worked for five chapters
                    # becomes an IndexError the moment usage is switched on.
                    # Verified against gpt-4o-mini: the second-to-last chunk
                    # has an empty choices list and a populated `usage`.
                    if chunk.get("usage"):
                        yield Usage(
                            chunk["usage"].get("prompt_tokens", 0),
                            chunk["usage"].get("completion_tokens", 0),
                        )
                    if not chunk.get("choices"):
                        continue

                    choice = chunk["choices"][0]
                    if choice.get("finish_reason"):
                        finish_reason = choice["finish_reason"]

                    delta = choice.get("delta") or {}
                    # Ollama sends "" on tool-call chunks, OpenAI sends null.
                    # Truthiness covers both; membership would not.
                    if delta.get("content"):
                        yield TextDelta(delta["content"])
                    for raw in delta.get("tool_calls") or []:
                        index = raw.get("index", 0)
                        pending.setdefault(index, _PartialCall(index)).absorb(raw)
