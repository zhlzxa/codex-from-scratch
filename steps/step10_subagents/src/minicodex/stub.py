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


class _Handler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        messages = body["messages"]

        # `stub_*` keys are not part of the OpenAI schema.  A real server would
        # ignore them; this one uses them so tests can select a recording.
        mode = body.get("stub_mode", "narrate")
        cut = body.get("stub_cut")

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
