"""What chapter 12 measured, and how.

Run one section at a time:

    uv run python probe_retry.py shapes       # F12-01/F12-08, real API, ~5 requests
    uv run python probe_retry.py overlong     # F12-05, real API, 1 large rejected request
    uv run python probe_retry.py idempotency  # F12-04, real API, 2 requests
    uv run python probe_retry.py ratelimit    # F12-02, real API, may cost a few cents
    uv run python probe_retry.py naive        # F12-01, no network
    uv run python probe_retry.py interrupt    # F12-07, no network
    uv run python probe_retry.py nesting      # F12-06, no network
    uv run python probe_retry.py debris       # F12-09, no network

Anything with "real API" needs OPENAI_API_KEY.
"""

from __future__ import annotations

import asyncio
import itertools
import json
import os
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx

MODEL = "gpt-4o-mini"
ROOT = Path(__file__).resolve().parent
OPENAI = "https://api.openai.com/v1"

# The headers worth printing.  A 429 that carries none of these is a different
# fault from a 429 that carries all of them, and the whole of F12-02 is which
# one a real provider sends.
INTERESTING = (
    "retry-after",
    "retry-after-ms",
    "x-ratelimit-limit-requests",
    "x-ratelimit-remaining-requests",
    "x-ratelimit-reset-requests",
    "x-ratelimit-limit-tokens",
    "x-ratelimit-remaining-tokens",
    "x-ratelimit-reset-tokens",
    "x-request-id",
    "idempotent-replayed",
    "openai-processing-ms",
)


def _key() -> str:
    key = os.environ.get("OPENAI_API_KEY")
    if not key:
        print("OPENAI_API_KEY is not set; this section needs it.", file=sys.stderr)
        raise SystemExit(1)
    return key


def _show(label: str, resp: httpx.Response, body: str) -> None:
    print(f"\n--- {label}")
    print(f"  status  {resp.status_code}")
    for name in INTERESTING:
        if name in resp.headers:
            print(f"  {name}: {resp.headers[name]}")
    print(f"  body    {body[:600]}")


async def _post(
    body: dict[str, Any],
    *,
    key: str | None = None,
    headers: dict[str, str] | None = None,
) -> tuple[httpx.Response, str]:
    hdrs = {"Content-Type": "application/json"}
    hdrs["Authorization"] = f"Bearer {key if key is not None else _key()}"
    hdrs.update(headers or {})
    async with httpx.AsyncClient(timeout=120.0) as client:
        async with client.stream(
            "POST", f"{OPENAI}/chat/completions", json=body, headers=hdrs
        ) as resp:
            text = (await resp.aread()).decode("utf-8", "replace")
    return resp, text


def _tiny(**extra: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "model": MODEL,
        "messages": [{"role": "user", "content": "Say OK."}],
        "stream": True,
        "max_tokens": 1,
    }
    body.update(extra)
    return body


# ---------------------------------------------------------------------------
# F12-01 / F12-08: what the failures actually look like
# ---------------------------------------------------------------------------


def shapes() -> None:
    async def go() -> None:
        resp, text = await _post(_tiny(), key="sk-not-a-real-key")
        _show("bad api key", resp, text)

        resp, text = await _post(_tiny(model="gpt-4o-mini-does-not-exist"))
        _show("unknown model", resp, text)

        # A schema the provider rejects: `type` must be one of a fixed set.
        resp, text = await _post(
            _tiny(
                tools=[
                    {
                        "type": "function",
                        "function": {
                            "name": "read file",  # a space is not allowed in a name
                            "parameters": {"type": "object", "properties": {}},
                        },
                    }
                ]
            )
        )
        _show("bad tool schema", resp, text)

        resp, text = await _post(_tiny(max_tokens=-1))
        _show("bad parameter", resp, text)

        resp, text = await _post(_tiny())
        _show("success (headers only)", resp, text[:200])

    asyncio.run(go())


# ---------------------------------------------------------------------------
# F12-05: the context-length error
# ---------------------------------------------------------------------------


def overlong() -> None:
    async def go() -> None:
        # gpt-4o-mini's window is 128k tokens.  ~4 characters per token of
        # English prose, so 700k characters is comfortably over and is rejected
        # before any of it is processed.
        filler = "The quick brown fox jumps over the lazy dog. " * 16000
        resp, text = await _post(
            {
                "model": MODEL,
                "messages": [{"role": "user", "content": filler}],
                "stream": True,
                "max_tokens": 1,
            }
        )
        _show(f"overlong ({len(filler)} chars)", resp, text)

    asyncio.run(go())


# ---------------------------------------------------------------------------
# F12-04: does the provider deduplicate a retried request
# ---------------------------------------------------------------------------


def idempotency() -> None:
    async def go() -> None:
        import uuid

        key = f"minicodex-probe-{uuid.uuid4()}"
        body = {
            "model": MODEL,
            "messages": [{"role": "user", "content": "Invent one surname. One word only."}],
            "stream": False,
            "max_tokens": 6,
            "temperature": 2.0,
        }
        for attempt in (1, 2):
            resp, text = await _post(body, headers={"Idempotency-Key": key})
            answer = ""
            try:
                answer = json.loads(text)["choices"][0]["message"]["content"]
            except Exception:
                answer = text[:200]
            _show(f"idempotency attempt {attempt}", resp, f"answer={answer!r}")

        # And the control: the same body with no key at all.
        for attempt in (1, 2):
            resp, text = await _post(body)
            answer = json.loads(text)["choices"][0]["message"]["content"]
            _show(f"no key attempt {attempt}", resp, f"answer={answer!r}")

    asyncio.run(go())


# ---------------------------------------------------------------------------
# F12-02: what a 429 carries
# ---------------------------------------------------------------------------


def ratelimit() -> None:
    """Trigger a real 429 without paying for it.

    The account's limits are 10,000 requests and 200,000 tokens per minute, so
    a burst of small requests will never get there.  What does: the overlong
    request from the section above, which is **rejected** for length and still
    debits its 160k tokens from the token bucket (measured: remaining-tokens
    went 199,996 -> 19,998 on one rejected request).  Two of those inside a
    minute is a rate limit reached with a bill of zero.
    """

    async def go() -> None:
        filler = "The quick brown fox jumps over the lazy dog. " * 16000
        body = {
            "model": MODEL,
            "messages": [{"role": "user", "content": filler}],
            "stream": True,
            "max_tokens": 1,
        }
        for attempt in range(1, 6):
            resp, text = await _post(body)
            _show(f"overlong attempt {attempt}", resp, text)
            if resp.status_code == 429:
                return
        print("\n  no 429 in five attempts")

    asyncio.run(go())


# ---------------------------------------------------------------------------
# offline: a local server that fails on cue
# ---------------------------------------------------------------------------


def _serve() -> tuple[str, Any]:
    """The recorded stub, on a real socket, able to fail on demand."""
    import socket
    import threading
    from http.server import HTTPServer

    from minicodex import stub

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    server = HTTPServer(("127.0.0.1", port), stub._Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    stub.reset()
    return f"http://127.0.0.1:{port}/v1", server


def _plan(queue_id: str, *responses: dict[str, Any]) -> dict[str, Any]:
    return {"stub_fail": {"id": queue_id, "responses": list(responses)}}


# ---------------------------------------------------------------------------
# F12-01: what the program does today, and what the obvious repair does
# ---------------------------------------------------------------------------


def naive() -> None:
    import time

    from minicodex import stub
    from minicodex.agent import Agent
    from minicodex.model import ChatCompletionsModel, ModelHTTPError

    url, server = _serve()
    try:

        async def go() -> None:
            # (a) one 429 in front of a conversation that would have worked.
            model = ChatCompletionsModel(
                base_url=url, model="gemma4:31b", extra_body=_plan("a", stub.RATE_LIMITED)
            )
            agent = Agent(model, {"read_file": _echo})
            print("\n--- (a) today, one 429 before a two-turn conversation")
            try:
                result = await agent.run("What does __init__.py define?")
                print(f"  {result.stop_reason} after {result.turns_used} turn(s)")
            except ModelHTTPError as exc:
                print(f"  {type(exc).__name__}: {str(exc)[:120]}...")
            print(f"  requests the server saw: {len(stub.REQUESTS)}")

            # (b) the obvious repair: retry anything that is not a 200.
            stub.reset()
            model = ChatCompletionsModel(
                base_url=url,
                model="gemma4:31b",
                extra_body=_plan("b", *[stub.BAD_TOOL_SCHEMA] * 8),
            )
            print("\n--- (b) retry-everything, against a request that can never succeed")
            began = time.monotonic()
            for attempt in range(5):
                try:
                    async for _ in model.stream([{"role": "user", "content": "hi"}]):
                        pass
                except ModelHTTPError:
                    if attempt < 4:
                        await asyncio.sleep(0.5 * 2**attempt)
            print(f"  requests the server saw: {len(stub.REQUESTS)}")
            print(f"  seconds spent: {time.monotonic() - began:.1f}")
            print("  the body was byte-identical every time, and so was the answer")

        asyncio.run(go())
    finally:
        server.shutdown()
        server.server_close()

    # (c) the arithmetic of ignoring what the server said.
    print("\n--- (c) exponential backoff vs the number the server sent")
    waited = 0.0
    for attempt in range(5):
        wait = 1.0 * 2**attempt
        print(f"  attempt {attempt + 1}: slept {wait:>5.1f}s, elapsed {waited + wait:>5.1f}s")
        waited += wait
    print("  the header said: retry-after 46 (retry-after-ms 45175)")
    print(f"  five attempts fit inside {waited:.0f}s; the window had not opened at any of them")


async def _echo(args: dict[str, Any]) -> str:
    return "__version__, system_prompt()"


# ---------------------------------------------------------------------------
# F12-07: what a blocking sleep costs
# ---------------------------------------------------------------------------


def interrupt() -> None:
    """Two backoffs, one of them blocking, measured from inside the loop.

    No signals involved.  The question "can a Ctrl-C land during the wait" has
    the same answer as "can *anything* happen during the wait", and the second
    one can be measured without a terminal: a heartbeat task ticking every 50ms
    alongside the backoff.
    """
    import time as _time

    async def measure(blocking: bool) -> None:
        beats: list[float] = []
        stop = False

        async def heartbeat() -> None:
            while not stop:
                beats.append(_time.monotonic())
                await asyncio.sleep(0.05)

        async def backoff() -> None:
            if blocking:
                # ruff's ASYNC251 flags this line, which is the point worth
                # noticing: the naive version of F12-07 is caught statically by
                # a rule this project has had since chapter -1.  Kept, with the
                # rule switched off for one line, so the cost can be measured
                # rather than asserted.
                _time.sleep(1.0)  # noqa: ASYNC251
            else:
                await asyncio.sleep(1.0)

        beat = asyncio.ensure_future(heartbeat())
        began = _time.monotonic()
        task = asyncio.ensure_future(backoff())
        await asyncio.sleep(0.2)
        cancelled_at = _time.monotonic()
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        landed = _time.monotonic() - cancelled_at
        issued = cancelled_at - began
        stop = True
        await asyncio.sleep(0.06)
        beat.cancel()

        gaps = [b - a for a, b in itertools.pairwise(beats)]
        kind = "time.sleep(1.0)" if blocking else "asyncio.sleep(1.0)"
        print(f"\n--- {kind}")
        print(f"  the cancel could be issued {issued:.2f}s in (it was asked for at 0.20s)")
        print(f"  and landed {landed * 1000:>7.1f} ms after that")
        print(f"  longest gap between heartbeats {max(gaps) * 1000:>7.1f} ms ({len(gaps)} beats)")
        print(f"  total {_time.monotonic() - began:.2f}s")

    async def go() -> None:
        await measure(blocking=False)
        await measure(blocking=True)

    asyncio.run(go())


# ---------------------------------------------------------------------------
# F12-06: two clocks set to the same number
# ---------------------------------------------------------------------------


def nesting() -> None:
    """What a sub-agent reports when its own deadline and its request's are equal.

    The real numbers are 300 and 300; these are 1.0 and 1.0, scaled so the
    section takes seconds.  Ten trials each, against a stub told to take longer
    than both.
    """
    from minicodex.agent import Wiring
    from minicodex.approval import AllowAll, Session
    from minicodex.composition import child_tools_builder
    from minicodex.model import ChatCompletionsModel
    from minicodex.retry import RetryPolicy
    from minicodex.shell import ShellSession
    from minicodex.subagent import SubAgentContext, TaskSpec, run_task

    url, server = _serve()
    root = ROOT

    async def trial(attempt_timeout: float, budget: float, task_timeout: float) -> str:
        session = Session(mode="read-only", approver=AllowAll())
        ctx = SubAgentContext(
            build_model=lambda schemas: ChatCompletionsModel(
                base_url=url,
                model="gemma4:31b",
                timeout=attempt_timeout,
                extra_body={"stub_delay": 3.0},
            ),
            root=root,
            session=session,
            parent_shell=ShellSession(),
            build_tools=child_tools_builder(root, session),
            wiring=Wiring(retry_policy=RetryPolicy(attempts=4, base=0.01, cap=0.01, budget=budget)),
            timeout=task_timeout,
        )
        result = await run_task(TaskSpec(task="say hello"), ctx)
        return result.outcome

    async def go() -> None:
        for label, attempt, budget, task in (
            ("equal    attempt 1.0 + budget 0.9 vs task 1.0  (300/300, as shipped)", 1.0, 0.9, 1.0),
            ("nested   attempt 0.4 + budget 0.3 vs task 1.0  (120/90/300, now)", 0.4, 0.3, 1.0),
        ):
            outcomes: dict[str, int] = {}
            for _ in range(10):
                outcome = await trial(attempt, budget, task)
                outcomes[outcome] = outcomes.get(outcome, 0) + 1
            print(f"\n--- {label}")
            print(f"  outcomes over 10 trials: {outcomes}")

    try:
        asyncio.run(go())
    finally:
        server.shutdown()
        server.server_close()


SECTIONS: dict[str, Callable[[], Any]] = {
    "shapes": shapes,
    "overlong": overlong,
    "idempotency": idempotency,
    "ratelimit": ratelimit,
    "naive": naive,
    "interrupt": interrupt,
    "nesting": nesting,
}


def main(argv: list[str]) -> int:
    if len(argv) != 2 or argv[1] not in SECTIONS:
        print(f"usage: {argv[0]} [{' | '.join(SECTIONS)}]", file=sys.stderr)
        return 2
    SECTIONS[argv[1]]()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
