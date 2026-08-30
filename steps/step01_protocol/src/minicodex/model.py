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


StreamEvent = TextDelta | ToolCallDelta | Completed


class ModelHTTPError(RuntimeError):
    """The server answered, but not with a stream."""


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
        timeout: float = 300.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.api_key = api_key
        self.tools = tools or []
        self.extra_body = extra_body or {}
        self.timeout = timeout

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

        async with httpx.AsyncClient(timeout=self.timeout) as client:
            async with client.stream(
                "POST", url, json=self.request_body(messages), headers=self._headers()
            ) as resp:
                if resp.status_code != 200:
                    detail = (await resp.aread()).decode("utf-8", "replace")[:1000]
                    raise ModelHTTPError(f"HTTP {resp.status_code} from {url}: {detail}")

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
