# minicodex — step 1: the protocol layer

The same agent, against two providers that disagree about how a tool call
arrives on the wire.

```bash
uv sync --all-extras

# Recorded responses, no GPU and no key needed:
uv run minicodex serve-stub &
uv run minicodex ask "What does src/minicodex/__init__.py define?" \
    --base-url http://127.0.0.1:11435/v1

# Your own Ollama:
uv run minicodex ask "What does src/minicodex/__init__.py define?"

# OpenAI (reads OPENAI_API_KEY from the environment):
uv run minicodex ask "What does src/minicodex/__init__.py define?" --provider openai

uv run pytest
```

| Path | What it is |
|---|---|
| `src/minicodex/model.py` | the HTTP client; **buffers tool-call fragments so callers see one shape** |
| `src/minicodex/history.py` | the conversation as facts, not JSON; refuses invalid states |
| `src/minicodex/agent_types.py` | `ToolCall`, moved down so agent and history can share it |
| `src/minicodex/agent.py` | the loop, now building a `History` instead of a list of dicts |
| `src/minicodex/stub.py` | responses recorded from Ollama and OpenAI, replayed verbatim |
| `tests/test_history.py` | one test per state the history must refuse |

## What changed from chapter 0, and why

Measured on 2026-08-06 against both providers:

- **OpenAI splits tool-call arguments across chunks**, and only the first chunk
  carries the id and the name. Chapter 0 overwrote by index and ended up with
  `name=''` and `arguments='"}'`.
- **The dialects differ.** Ollama's native API wants `arguments` as an object
  and matches results by `tool_name`; chat-completions wants a string and
  matches by `tool_call_id`.
- **Only some servers check.** An unanswered tool call: Ollama answered HTTP
  200 with nonsense, OpenAI returned HTTP 400.

## Deliberately unfinished

- tools run one after another — chapter 8
- `IncompleteStreamError` and `ModelHTTPError` end the session instead of
  retrying — chapter 12
- nothing yet trims the history when it grows — chapter 6
