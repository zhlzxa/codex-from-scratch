"""Talking to a model over HTTP.

Written against Ollama's OpenAI-compatible endpoint, because that is what a
local Ollama exposes at http://localhost:11434/v1 and what most hosted
providers speak too.

The shapes below are not guesses.  They were read off a real response:

    data: {"choices":[{"delta":{"content":"I"},"finish_reason":null}]}
    ...
    data: {"choices":[{"delta":{"content":"","tool_calls":[
             {"id":"call_yfo64477","index":0,"type":"function",
              "function":{"name":"get_temperature",
                          "arguments":"{\"city\":\"New York\"}"}}]},
           "finish_reason":null}]}
    data: {"choices":[{"delta":{"content":""},"finish_reason":"tool_calls"}]}
    data: [DONE]

Two things in there are easy to get wrong and cost nothing to get right once
you have seen them:

* **Prose and tool calls stream at different granularities.**  Prose arrives in
  arbitrary slices -- one recorded response cut the path
  `src/minicodex/prompts/system.md` across two chunks as `"minicodex/prom"` and
  `"pts/system.md"` -- so no slice may be interpreted before joining.  A tool
  call, by contrast, arrives whole in a single chunk, arguments already a
  complete JSON string.  One accumulator does not fit both.
* **There are two terminators.**  `finish_reason` says why the model stopped;
  the `[DONE]` sentinel says the HTTP stream is over.  A stream that ends
  without `[DONE]` did not finish -- it was cut.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass
from typing import Any

import httpx

DEFAULT_BASE_URL = "http://localhost:11434/v1"
DEFAULT_MODEL = "gemma4:31b-cloud"


@dataclass(frozen=True)
class TextDelta:
    """A slice of prose.  Not a token, not a word -- whatever the server sent."""

    text: str


@dataclass(frozen=True)
class ToolCallDelta:
    """One complete tool call.

    Named `Delta` because that is the field it arrives in, not because it is a
    fragment: `arguments` is the entire JSON string.
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


class OllamaModel:
    def __init__(
        self,
        *,
        base_url: str = DEFAULT_BASE_URL,
        model: str = DEFAULT_MODEL,
        tools: list[dict[str, Any]] | None = None,
        extra_body: dict[str, Any] | None = None,
        timeout: float = 300.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.tools = tools or []
        # Used by tests to pick a stub recording.  A real server ignores keys it
        # does not know, so this costs nothing in production.
        self.extra_body = extra_body or {}
        self.timeout = timeout

    def request_body(self, history: Sequence[dict[str, Any]]) -> dict[str, Any]:
        """The exact JSON that will be posted.

        Separate from `stream()` so it can be printed, recorded, diffed and
        asserted on without making a network call.
        """
        body: dict[str, Any] = {
            "model": self.model,
            "messages": list(history),
            "stream": True,
        }
        if self.tools:
            body["tools"] = self.tools
        body.update(self.extra_body)
        return body

    async def stream(self, history: Sequence[dict[str, Any]]) -> AsyncIterator[StreamEvent]:
        url = f"{self.base_url}/chat/completions"
        finish_reason: str | None = None

        async with httpx.AsyncClient(timeout=self.timeout) as client:
            async with client.stream("POST", url, json=self.request_body(history)) as resp:
                if resp.status_code != 200:
                    detail = (await resp.aread()).decode("utf-8", "replace")[:500]
                    raise ModelHTTPError(f"HTTP {resp.status_code} from {url}: {detail}")

                async for line in resp.aiter_lines():
                    if not line.strip():
                        continue
                    if not line.startswith("data: "):
                        continue
                    payload = line[len("data: ") :]

                    # The sentinel is not JSON.  Parsing before checking for it
                    # is the first thing that breaks, and it breaks *after*
                    # printing a perfectly good answer.
                    if payload == "[DONE]":
                        yield Completed(finish_reason)
                        return

                    chunk = json.loads(payload)
                    choice = chunk["choices"][0]
                    if choice.get("finish_reason"):
                        finish_reason = choice["finish_reason"]

                    delta = choice.get("delta") or {}
                    # `content` is "" on tool-call chunks, so truthiness is the
                    # test, not membership.
                    if delta.get("content"):
                        yield TextDelta(delta["content"])
                    for raw in delta.get("tool_calls") or []:
                        fn = raw.get("function") or {}
                        yield ToolCallDelta(
                            call_id=raw.get("id") or f"call_{raw.get('index', 0)}",
                            index=raw.get("index", 0),
                            name=fn.get("name", ""),
                            arguments=fn.get("arguments", ""),
                        )
