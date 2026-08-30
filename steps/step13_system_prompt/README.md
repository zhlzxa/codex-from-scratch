# minicodex — chapter 12: retries and error classification

The program used to end on the first failed request. Not on the first
*unrecoverable* request — on the first failed one: a dropped connection, a 429,
a 500, all of them printed a twenty-line traceback and stopped.

This chapter adds one question — *what kind of failure is this* — and three
answers, because there are three different things to do about one:

```
retry    the same request may work later
shrink   it is too big; the conversation has to lose weight first
fatal    sending this again cannot succeed
```

```bash
uv sync --all-extras
uv run pytest
uv run python probe_retry.py naive          # offline
uv run python probe_retry.py interrupt      # offline
uv run python probe_retry.py nesting        # offline
uv run python probe_mutations_ch12.py
```

## What is new

| Path | What it does |
|---|---|
| `src/minicodex/retry.py` | `classify()`, `Failure`, `RetryPolicy`, `wait_for()`, `explain()`, `ModelFailed` |
| `src/minicodex/model.py` | `ModelHTTPError` carries `status` / `code` / `message` / `headers` / `request_id`; one attempt is bounded at 120s, not 300 |
| `src/minicodex/agent.py` | `_respond()` — the retry loop, around stream *plus* assembly; `_shrink()` for the one failure that is a fact about us; `Wiring.retry_policy` and `Wiring.announce` |
| `src/minicodex/compaction.py` | a *fatal* provider failure inside the summariser is no longer degraded into a note |
| `src/minicodex/subagent.py` | outcome `error`: a provider failure inside a child reaches the parent as prose instead of as a traceback |
| `src/minicodex/__main__.py` | the failure is translated for a human; the session lock is released in a `finally` |
| `src/minicodex/agent_types.py` | `IncompleteStreamError` moved down so `retry.py` can classify it (fourth application of chapter 1's rule) |
| `src/minicodex/stub.py` | five recorded failure bodies, and a queue so a test can say "429 twice, then answer" |
| `tests/test_faults_ch12.py` | 44 tests, F12-01…F12-09 |
| `tests/test_packaging.py` | every `probe_mutations*.py` must be referenced by a workflow |
| `probe_retry.py` | seven sections, four of them against the real API |
| `probe_mutations_ch12.py` | 23 mutations across five modules |

## What was measured

**Status code cannot classify a failure; `code` can.** Five recorded failures
from api.openai.com, 2026-08-12:

```
401  code=invalid_api_key          type=invalid_request_error   no rate-limit headers
404  code=model_not_found          type=invalid_request_error   no rate-limit headers
400  code=invalid_value            type=invalid_request_error   -> never send again
400  code=context_length_exceeded  type=invalid_request_error   -> compact, then retry
429  code=rate_limit_exceeded      type=tokens                  retry-after: 46
```

Two 400s with the same `type` sit at opposite ends of the table.

**The server said 45.175 seconds.** A 1-2-4-8-16 backoff makes all five of its
attempts inside 31 — every one of them before the window opens, and every one
of them another request against a limit that is already exhausted:

```
retry-after: 46      retry-after-ms: 45175
"Rate limit reached ... Limit 200000, Used 170583, Requested 180002.
 Please try again in 45.175s."
```

With the real numbers, the default policy allows **one** retry of a rate limit,
not four: 45.175s fits in a 90-second budget and a second one does not.

**A rejected request still costs you the quota.** One 160k-token request that
was refused for length took the account's remaining tokens from 199,996 to
19,998. Retrying what cannot succeed is what pushes the *next* request into a
429 — which is how the 429 above was produced, deliberately, for free.

**`Idempotency-Key` does nothing here.** Two identical requests with the same
key returned `'Nymbria.'` and `'Falnitz.'`, two request ids, two debits, and no
`idempotent-replayed` header. The listed fix for F12-04 does not apply to this
endpoint, so the honest bound is elsewhere: a failure can only interrupt the
*model* call, and by the time a tool runs the turn is already committed.

**A blocking backoff stops the whole process.** Same wait, measured from inside
the loop with a heartbeat task:

```
asyncio.sleep(1.0)   longest gap between heartbeats     62.8 ms
time.sleep(1.0)      longest gap between heartbeats   1000.5 ms
```

**Two clocks set to the same number decide nothing.** A sub-agent whose task
deadline equals its request timeout, ten trials each:

```
attempt 1.0 + budget 0.9  vs task 1.0   ->  {'timeout': 10}
attempt 0.4 + budget 0.3  vs task 1.0   ->  {'error':   10}
```

Same failure, and only the second one can say what happened.

## Deliberately not done

- **No generic `with_retries(fn, policy)`.** The loop needs the history, the
  compactor and the recorder; a callable that takes three callbacks is a worse
  interface than twenty lines in the one place that has them (FB-03).
- **No retry of a tool call, an MCP call or a sub-agent.** Chapter 9 already
  decided not to re-send an in-flight MCP call: whether its side effect
  happened is unknown, and "unknown" is not resolved by doing it again.
- **No `Retry-After` HTTP-date parsing.** RFC 9110 allows one; no provider
  measured in this book has ever sent one. An unreadable header falls back to
  the local schedule, which is what a missing one does.
- **No per-provider policy.** One `RetryPolicy`, and a child gets its parent's.
- **The attempt budget does not bound a turn exactly.** No new attempt starts
  after `budget` and an attempt lasts at most `DEFAULT_ATTEMPT_TIMEOUT`, so a
  turn ends within 90 + 120 = 210 seconds. That is the whole guarantee.
