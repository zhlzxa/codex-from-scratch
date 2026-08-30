# minicodex — step 0: the loop

Ask a question the agent can only answer by reading a file first.

```bash
uv sync --all-extras

# Against your own Ollama:
uv run minicodex ask "What does src/minicodex/__init__.py define?"

# Or, with no GPU and no Ollama, against the recorded responses:
uv run minicodex serve-stub &
uv run minicodex ask "What does src/minicodex/__init__.py define?" \
    --base-url http://127.0.0.1:11435/v1

uv run pytest
```

| Path | What it is |
|---|---|
| `src/minicodex/model.py` | the HTTP client: SSE parsing, two granularities, two terminators |
| `src/minicodex/agent.py` | stream assembly, tool dispatch and the loop, together on purpose |
| `src/minicodex/stub_ollama.py` | responses recorded from a real Ollama on 2026-08-06, replayed verbatim |
| `src/minicodex/recorder.py` | append-only transcript of everything crossing the model boundary |
| `tests/test_agent.py` | one test per guarantee; `_naive_run` keeps the first version's bugs reproducible |

The stub is not a model. It replays bytes that were actually received, so the
client, the SSE parsing and the loop are all exercised for real — only the
model's judgement is missing.

## Deliberately unfinished

- history is `list[dict]`; nothing enforces one output per call — chapter 1
- tools run one after another — chapter 8
- `IncompleteStreamError` ends the session instead of retrying — chapter 12
