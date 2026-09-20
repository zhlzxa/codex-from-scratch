"""Stand-ins for two servers that both claim to speak /v1/chat/completions.

Every chunk below is a verbatim copy of what Ollama actually returned for
`gemma4:31b` on 2026-08-06, ids and all.  It is not a model and does not
pretend to be one -- it replays those exact bytes so that client code, CI and
readers without a GPU all exercise the same wire format.

Two recordings, two shapes for the same tool call:

  OLLAMA_*  one chunk, arguments complete
  OPENAI_*  fourteen chunks, and only the first carries the id and the name

Run with `minicodex serve-stub [--openai]`, or point at the real thing:

    minicodex ask "..." --base-url http://localhost:11434/v1 --model qwen3
    minicodex ask "..." --provider openai --model gpt-4o-mini
"""

from __future__ import annotations

import json
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any

MODEL = "gemma4:31b"


def _text(chat_id: str, s: str) -> dict[str, Any]:
    return {
        "id": chat_id,
        "object": "chat.completion.chunk",
        "created": 1786021399,
        "model": MODEL,
        "system_fingerprint": "fp_ollama",
        "choices": [
            {"index": 0, "delta": {"role": "assistant", "content": s}, "finish_reason": None}
        ],
    }


def _call(chat_id: str, tid: str, index: int, name: str, arguments: str) -> dict[str, Any]:
    return {
        "id": chat_id,
        "object": "chat.completion.chunk",
        "created": 1786021400,
        "model": MODEL,
        "system_fingerprint": "fp_ollama",
        "choices": [
            {
                "index": 0,
                "delta": {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {
                            "id": tid,
                            "index": index,
                            "type": "function",
                            "function": {"name": name, "arguments": arguments},
                        }
                    ],
                },
                "finish_reason": None,
            }
        ],
    }


def _finish(chat_id: str, reason: str) -> dict[str, Any]:
    return {
        "id": chat_id,
        "object": "chat.completion.chunk",
        "created": 1786021400,
        "model": MODEL,
        "system_fingerprint": "fp_ollama",
        "choices": [
            {"index": 0, "delta": {"role": "assistant", "content": ""}, "finish_reason": reason}
        ],
    }


# Recorded 2026-08-06 against gemma4:31b, asked "What does
# src/minicodex/__init__.py define?" with a system prompt telling it to say one
# sentence before each tool call.  Note the chunk sizes: prose does not arrive
# one token at a time, it arrives in whatever slices the server felt like.
NARRATE_THEN_CALL = [
    *[
        _text("chatcmpl-864", w)
        for w in [
            "I",
            " will read the contents of",
            " the file",
            " `",
            "src/minicodex",
            "/__init__.py`.",
        ]
    ],
    _call(
        "chatcmpl-864",
        "call_rgpykfbt",
        0,
        "read_file",
        '{"path":"src/minicodex/__init__.py"}',
    ),
    _finish("chatcmpl-864", "tool_calls"),
]

# Recorded 2026-08-06: the answer once the file contents were handed back.
# `"minicodex/prom"` and `"pts/system.md"` are two separate chunks -- a path cut
# in half mid-word, which is why nothing may be interpreted before joining.
FINAL_ANSWER = [
    *[
        _text("chatcmpl-975", w)
        for w in [
            "`",
            "src/minicodex",
            "/__init__.py`",
            " defines the following",
            ":\n\n-",
            " **`__version__",
            "`**: The",
            " current",
            " version of the package (`",
            "0.0.1",
            "`).\n- **`",
            "system_prompt()`**:",
            " A function that reads and",
            " returns the agent's",
            " system prompt from a file",
            " located at `src/",
            "minicodex/prom",
            "pts/system.md",
            "`.\n- **`",
            "__all__`**:",
            " An export list containing `",
            "__version__` and",
            " `system_prompt`.",
        ]
    ],
    _finish("chatcmpl-975", "stop"),
]

# Recorded 2026-08-06 from a separate probe -- three things asked for at once,
# to a weather API rather than a file.  Kept because the file question never
# produced parallel calls and this one did: three chunks, three ids, index 0/1/2.
THREE_CALLS = [
    _call("chatcmpl-908", "call_cz5zx7jy", 0, "get_temperature", '{"city":"New York"}'),
    _call("chatcmpl-908", "call_3zvh467n", 1, "get_conditions", '{"city":"New York"}'),
    _call("chatcmpl-908", "call_zfz547ah", 2, "get_temperature", '{"city":"London"}'),
    _finish("chatcmpl-908", "tool_calls"),
]


def _openai_call_first(chat_id: str, tid: str, index: int, name: str) -> dict[str, Any]:
    """The one fragment that carries the id and the name, with empty arguments."""
    return {
        "id": chat_id,
        "object": "chat.completion.chunk",
        "created": 1786038347,
        "model": "gpt-4o-mini-2024-07-18",
        "choices": [
            {
                "index": 0,
                "delta": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "index": index,
                            "id": tid,
                            "type": "function",
                            "function": {"name": name, "arguments": ""},
                        }
                    ],
                },
                "finish_reason": None,
            }
        ],
    }


def _openai_call_more(chat_id: str, index: int, slice_: str) -> dict[str, Any]:
    """Every later fragment: an index and a slice of the arguments string."""
    return {
        "id": chat_id,
        "object": "chat.completion.chunk",
        "created": 1786038347,
        "model": "gpt-4o-mini-2024-07-18",
        "choices": [
            {
                "index": 0,
                "delta": {"tool_calls": [{"index": index, "function": {"arguments": slice_}}]},
                "finish_reason": None,
            }
        ],
    }


# Recorded 2026-08-06 from api.openai.com, gpt-4o-mini, same question and same
# tool as the Ollama recording above.  Fourteen chunks for one call, and the
# model produced no narration at all despite being asked for one.
_OPENAI_ARG_SLICES = [
    '{"',
    "path",
    '":"',
    "src",
    "/min",
    "ic",
    "od",
    "ex",
    "/__",
    "init",
    "__.",
    "py",
    '"}',
]

OPENAI_FRAGMENTED_CALL = [
    _openai_call_first("chatcmpl-E9wS3", "call_bqv6MLMr9BhXis7T4LB6tGqa", 0, "read_file"),
    *[_openai_call_more("chatcmpl-E9wS3", 0, s) for s in _OPENAI_ARG_SLICES],
    _finish("chatcmpl-E9wS3", "tool_calls"),
]

OPENAI_FINAL_ANSWER = [
    *[
        _text("chatcmpl-E9wS4", w)
        for w in ["It", " defines", " `__version__`", " and", " `system_prompt()`", "."]
    ],
    _finish("chatcmpl-E9wS4", "stop"),
]


# Recorded from api.openai.com on 2026-08-12 (`probe_retry.py shapes`,
# `ratelimit`, `overlong`).  Four failures, and the point of keeping all four
# is what they have in common and what they do not: every one carries a
# machine-readable `code`, and two of them are HTTP 400 with the same
# `type` -- one of which must be retried after shrinking the request and the
# other of which must never be sent again.
RATE_LIMITED = {
    "status": 429,
    # `retry-after` in whole seconds and `retry-after-ms` alongside it, plus
    # the same number a third time in the prose.  The account's limits were
    # 10,000 requests and 200,000 tokens per minute; this is the token bucket.
    "headers": {"retry-after": "46", "retry-after-ms": "45175"},
    "body": {
        "error": {
            "message": (
                "Rate limit reached for gpt-4o-mini in organization org-XXXX on tokens "
                "per min (TPM): Limit 200000, Used 170583, Requested 180002. Please try "
                "again in 45.175s. Visit https://platform.openai.com/account/rate-limits "
                "to learn more."
            ),
            "type": "tokens",
            "param": None,
            "code": "rate_limit_exceeded",
        }
    },
}

CONTEXT_LENGTH_EXCEEDED = {
    "status": 400,
    "body": {
        "error": {
            "message": (
                "This model's maximum context length is 128000 tokens. However, your "
                "messages resulted in 160008 tokens. Please reduce the length of the "
                "messages."
            ),
            "type": "invalid_request_error",
            "param": "messages",
            "code": "context_length_exceeded",
        }
    },
}

BAD_TOOL_SCHEMA = {
    "status": 400,
    "body": {
        "error": {
            "message": (
                "Invalid 'tools[0].function.name': string does not match pattern. "
                "Expected a string that matches the pattern '^[a-zA-Z0-9_-]+$'."
            ),
            "type": "invalid_request_error",
            "param": "tools[0].function.name",
            "code": "invalid_value",
        }
    },
}

BAD_API_KEY = {
    "status": 401,
    "body": {
        "error": {
            "message": (
                "Incorrect API key provided: sk-not-a*****-key. You can find your API "
                "key at https://platform.openai.com/account/api-keys."
            ),
            "type": "invalid_request_error",
            "code": "invalid_api_key",
            "param": None,
        },
        "status": 401,
    },
}

# No recording exists for this one and there will not be one: a 500 cannot be
# asked for.  The shape is the provider's documented envelope with the fields
# an outage actually leaves empty.
SERVER_ERROR = {
    "status": 500,
    "body": {"error": {"message": "The server had an error", "type": "server_error"}},
}


# Failures a test has asked for, by id: a queue of responses to serve *instead
# of* a recording, one per request, until it runs out.  Module state rather
# than a field on the handler because `HTTPServer` builds a new handler per
# request -- the same reason the real thing is stateful across requests, and
# the retry machinery is about a second request that knows about the first.
_QUEUES: dict[str, list[dict[str, Any]]] = {}

# Every request body this process has served, in order.  A retry is only
# observable from the server's side -- "was the same thing sent twice" is not a
# question the client can answer about itself -- and replaying an immediate
# question asked of the history instead.
REQUESTS: list[dict[str, Any]] = []


def reset() -> None:
    _QUEUES.clear()
    REQUESTS.clear()


class _Handler(BaseHTTPRequestHandler):
    def _next_failure(self, plan: dict[str, Any]) -> dict[str, Any] | None:
        """The next queued failure for this id, if there is one.

        The queue is registered on the first request carrying an id and
        consumed one entry per request afterwards, so a body saying "429 twice
        then answer" describes the whole sequence in one place instead of
        needing the test to count requests.
        """
        queue_id = plan["id"]
        if queue_id not in _QUEUES:
            _QUEUES[queue_id] = [dict(r) for r in plan.get("responses", [])]
        queue = _QUEUES[queue_id]
        return queue.pop(0) if queue else None

    def _serve_failure(self, response: dict[str, Any]) -> None:
        body = json.dumps(response.get("body", {"error": {"message": "stub failure"}})).encode()
        self.send_response(response.get("status", 500))
        self.send_header("Content-Type", "application/json")
        for name, value in (response.get("headers") or {}).items():
            self.send_header(name, str(value))
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
        self.wfile.flush()

    def do_POST(self) -> None:
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        messages = body["messages"]
        REQUESTS.append(body)

        # `stub_*` keys are not part of the OpenAI schema.  A real server would
        # ignore them; this one uses them so tests can select a recording.
        mode = body.get("stub_mode", "narrate")
        cut = body.get("stub_cut")

        if body.get("stub_fail"):
            failure = self._next_failure(body["stub_fail"])
            if failure is not None:
                # `stream_cut` is a failure that arrives as a *success*: status
                # 200, some chunks, and then the connection ends without the
                # sentinel.  It has to live in the same queue as the HTTP
                # errors because the whole question here is what
                # the *second* attempt does, and `stub_cut` applies
                # to every request a client makes rather than to one of them.
                if "stream_cut" in failure:
                    cut = failure["stream_cut"]
                else:
                    self._serve_failure(failure)
                    return
        if body.get("stub_delay"):
            time.sleep(float(body["stub_delay"]))

        answered = any(m.get("role") == "tool" for m in messages)
        if mode == "openai":
            chunks = OPENAI_FINAL_ANSWER if answered else OPENAI_FRAGMENTED_CALL
        elif answered:
            chunks = FINAL_ANSWER
        elif mode == "three":
            chunks = THREE_CALLS
        else:
            chunks = NARRATE_THEN_CALL

        if cut is not None:
            chunks = chunks[:cut]

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        for chunk in chunks:
            self.wfile.write(b"data: " + json.dumps(chunk).encode() + b"\n\n")
            self.wfile.flush()
        if cut is None:  # a cut stream never gets its sentinel
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()

    def log_message(self, *args: Any) -> None:
        pass


def serve(port: int = 11435) -> None:
    server = HTTPServer(("127.0.0.1", port), _Handler)
    print(f"stub listening on http://127.0.0.1:{port}/v1  (Ctrl-C to stop)")
    server.serve_forever()
