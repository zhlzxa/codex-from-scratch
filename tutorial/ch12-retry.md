# 第 12 章 · 请求失败了怎么办

> **代码**：`steps/step12_retry/`
> **分支**：`feat/retry`
> **产出**：向模型发请求的那一步失败时，程序知道这是哪一种失败——值得再试、得先把对话压小、还是再发也没用——并且照着做；最后实在不行，用户看到的是三行话，不是二十行报错
> **前置**：做完第 11 章。测试全部不联网（它们对着一个本地的假服务器，走真的网络连接）。探针 `probe_retry.py` 七段里四段要真的请求 OpenAI（需要 `OPENAI_API_KEY`，一共十几个请求）；没有 key 照着读正文里的输出即可。
> **这一章可以分三次读**：§1–§7 是"失败长什么样、怎么分类、等多久"，§8–§12 是"重试套在哪一层，以及它带出来的四件事"，之后是清单外的发现和验证。

---

## §0 开工前的准备

### 0.1 本章会遇到的新概念

- **状态码**：HTTP 的回答开头的那个数字。200 是成功；4 开头是"你的请求有问题"（400 请求不对、401 没有权限、404 找不到、429 太频繁）；5 开头是"服务器自己出了问题"。
- **限流（rate limit）**：服务商规定一分钟里你最多能发多少请求、多少 token。超过了，回答是 429。
- **重试（retry）**：同一个请求再发一次。**退避（backoff）**：每次重试之前等一会儿，而且越等越长（1 秒、2 秒、4 秒……）。
- **抖动（jitter）**：给等待的时间加一点随机，免得好几个程序在同一刻一起醒来。
- **`Retry-After`**：服务器在回答的"头"里写的一个数：请等这么久再来。
- **幂等（idempotent）**：同一件事做一次和做两次，结果一样。
- **预算（budget）**：这一章里指"为了一次回答，最多愿意花多少秒"。

### 0.2 本章会遇到的新 Python 写法

| 写法 | 意思 |
|---|---|
| `class ModelFailed(RuntimeError):` 里写 `def __init__(self, failure, ...)` | 自己定义的异常，可以带上自己的字段（`exc.failure`） |
| `raise B(...) from exc` | 抛出 B，并记下"它是由 exc 引起的" |
| `try: ... except X: ... else: ...` | `else` 里的代码只在 `try` **没有**抛异常时执行 |
| `except Exception as exc:` 对比 `except BaseException` | `Exception` 不包括 Ctrl-C 和"被取消"；`BaseException` 包括 |
| `time.monotonic()` | 一个只会往前走的秒表读数，用来量"过了多久" |
| `random.uniform(0.5, 1.0)` | 0.5 到 1.0 之间的一个随机小数 |
| `d.get(key, 默认)` | 字典里有就取，没有就给默认值 |
| `(m := 正则.search(s))` | 一边赋值一边判断：搜到了，`m` 就是结果 |
| `pytest.approx(45.175)` | 比较小数时允许一点点误差 |
| `@pytest.fixture` 写在测试文件里，和 `conftest.py` 里的同名 | 这个文件里用自己的这一个，**盖住**公共的那个 |

### 0.3 开分支

```bash
git switch main
git pull
git switch -c feat/retry
```

---

## §1 这一章要做出来的东西

前三章的测量之所以做得下来，是因为**探针脚本**里有一个重试循环：连接断了，等两秒，再来。每个探针里都抄了一份，旁边写着"这不是在修什么，第 12 章才设计重试"。

而发布出去的程序本身，**一个都没有**。一次断线，这次运行就结束了：屏幕上是一段报错，模型已经做了的事留在磁盘上，没有任何记录说它为什么停。

这一章给程序补上这一块。

---

## §2 定需求，猜故障

需求：

- 值得再试的失败，自动再试；再试也没用的，立刻停下并说清楚；
- 服务器说了要等多久，就按它说的等；
- 重试不能让任何有副作用的事（改文件、跑命令）发生两次；
- 最终失败时，人看到的是原因和下一步，不是一屏报错。

动工前的猜测清单，九条：

| 编号 | 猜测 | 怎么判断 |
|---|---|---|
| F12-01 | 400（请求本身不对）被一遍遍重试 | 对着一个永远不会成功的请求试 |
| F12-02 | 服务器给的 `Retry-After` 被无视 | 弄一个真的 429 来看 |
| F12-03 | 流断在一半，"收到一半的工具调用"状态说不清 | 把流从中间砍断 |
| F12-04 | 重试没有幂等保护，被收两次钱 | 量服务商的幂等键管不管用 |
| F12-05 | "上下文太长"的错误被当成没救了 | 弄一个真的来看 |
| F12-06 | 工具的超时和整轮的超时互相打架 | 把两个钟设成一样，看会怎样 |
| F12-07 | 退避等待的时候，Ctrl-C 没反应 | 在等待中间取消 |
| F12-08 | "HTTP 500"原样交给了模型 | 找它能走的路 |
| F12-09 | 重试成功之后，历史里留着失败的残渣 | 比较"失败过"和"没失败过"的历史 |

先说结果：

- **按清单的说法不会发生的两条**（F12-03、F12-09），而且原因是同一个——重试套在了哪一层。
- **清单开的药量出来完全没用的一条**（F12-04）：幂等键，同一个键发两次，两个不同的回答，两次扣费。
- **修法本身出了大问题的一条**（F12-05）：一个"顺手"的好主意，让一次运行在 12 轮里压缩了 11 次。
- 其余的都成立，各有各的转折。

清单外的发现里，改写这一章时新增了三件：**"顺手的好主意"修过之后，仍然重复了第 6 章修过一次的错**；**"一次请求最多 120 秒"这句话不成立**——那个数字管的是"多久没动静"，不是"一共多久"；**探针里有一段，标题写着"现在的程序"，量的其实是这一章之前的程序**。

---

## §3 现状：一条从来没人走过的路

在 `src` 和 `tests` 里搜 `ModelHTTPError`（服务器回答的不是 200 时，模型客户端抛的那个异常），这一章之前的结果是两行，都在 `model.py` 里：定义它的那一行，和抛出它的那一行。

**这个客户端唯一的一条出错的路，十一章下来没有测试、没有人接、没有人调用。** 不是覆盖率不够，是从来没有人走过。

用户看到的是这样的（这一章最初的记录；用一个假的 key 真的跑了一次）：

```
$ OPENAI_API_KEY=sk-not-a-real-key uv run minicodex ask "what is in this directory" --provider openai
  File "...\minicodex\agent.py", line 440, in run
    turn = await self._collect(self.model.stream(messages))
  File "...\minicodex\agent.py", line 275, in _collect
    async for event in stream:
  File "...\minicodex\model.py", line 199, in stream
    raise ModelHTTPError(f"HTTP {resp.status_code} from {url}: {detail}")
minicodex.model.ModelHTTPError: HTTP 401 from https://api.openai.com/v1/chat/completions: {
  "error": {
    "message": "Incorrect API key provided: sk-not-a*****-key. You can find your API key at https://platform.openai.com/account/api-keys.",
    "type": "invalid_request_error",
    "code": "invalid_api_key",
    "param": null
  },
  "status": 401
}
```

（上面还有十几行。）一个"key 填错了"的问题，补救办法一句话就能说完。

---

## §4 先去看失败长什么样

动手之前，先向真的服务商要几个真的失败（Windows，api.openai.com，2026-10-01；请求编号各不相同，这里略去）：

```
$ uv run python probe_retry.py shapes
--- bad api key
  status  401
  body    { "error": { "message": "Incorrect API key provided: sk-not-a*****-key. ...",
                       "type": "invalid_request_error", "code": "invalid_api_key", "param": null }, "status": 401 }
--- unknown model
  status  404
  body    { "error": { "message": "The model `gpt-4o-mini-does-not-exist` does not exist or you do not have access to it.",
                       "type": "invalid_request_error", "param": null, "code": "model_not_found" } }
--- bad tool schema
  status  400
  body    { "error": { "message": "Invalid 'tools[0].function.name': string does not match pattern. Expected a string that matches the pattern '^[a-zA-Z0-9_-]+$'.",
                       "type": "invalid_request_error", "param": "tools[0].function.name", "code": "invalid_value" } }
--- bad parameter
  status  400
  body    { "error": { "message": "Invalid 'max_tokens': integer below minimum value. Expected a value >= 1, but got -1 instead.",
                       "type": "invalid_request_error", "param": "max_tokens", "code": "integer_below_min_value" } }
```

（只列了状态码和正文；正文为了排版折了行，内容没有改。）每一个回答都带着一个 `x-request-id` 头——一串编号，是唯一能让别人帮你查"那次请求到底怎么了"的东西。

再要一个"太长了"的。gpt-4o-mini 的窗口是 128,000 个 token；发一条 72 万个字符的消息：

```
$ uv run python probe_retry.py overlong
--- overlong (720000 chars)
  status  400
  x-ratelimit-limit-tokens: 200000
  x-ratelimit-remaining-tokens: 19998
  x-ratelimit-reset-tokens: 54s
  body    { "error": { "message": "This model's maximum context length is 128000 tokens. However, your messages resulted in 160008 tokens. Please reduce the length of the messages.",
                       "type": "invalid_request_error", "param": "messages", "code": "context_length_exceeded" } }
```

两件事要记住：

- **它也是 400，`type` 也是 `invalid_request_error`**——和上面那个"工具名里有空格"的一模一样。
- **看 `x-ratelimit-remaining-tokens`**：前一个请求时是 199,996，现在是 **19,998**。这个请求**被拒绝了**，一个字都没处理，**而它的 16 万个 token 照样从这一分钟的额度里扣掉了**。

### 4.1 一个免费的、真的 429

这个账号的限额是每分钟 1 万个请求、20 万个 token。靠发小请求永远撞不到。但上面那件事给了一条路：被拒绝的超长请求照样扣额度，所以**一分钟里发两次**，第二次就超了——一个真的 429，账单是零。

```
$ uv run python probe_retry.py ratelimit
--- overlong attempt 1
  status  429
  retry-after: 46
  retry-after-ms: 45190
  x-ratelimit-remaining-tokens: 29367
  x-ratelimit-reset-tokens: 51.189s
  body    { "error": { "message": "Rate limit reached for gpt-4o-mini in organization org-XXXX on tokens per min (TPM): Limit 200000, Used 170633, Requested 180002. Please try again in 45.19s. ...",
                       "type": "tokens", "param": null, "code": "rate_limit_exceeded" } }
```

（紧跟在上一段之后跑的，所以第一次就是 429。组织编号改成了 `org-XXXX`。）

服务器把"等多久"说了**三遍**：`retry-after: 46`（整秒，往上取整）、`retry-after-ms: 45190`（毫秒）、正文里的 `Please try again in 45.19s`。这一章最初的记录是 45175 毫秒——两次差了 15 毫秒。

把这五种失败列在一起：

| 状态码 | `code` | `type` | 该怎么办 |
|---|---|---|---|
| 401 | `invalid_api_key` | `invalid_request_error` | 再发也没用 |
| 404 | `model_not_found` | `invalid_request_error` | 再发也没用 |
| 400 | `invalid_value` | `invalid_request_error` | 再发也没用 |
| 400 | `context_length_exceeded` | `invalid_request_error` | **把对话压小，再发** |
| 429 | `rate_limit_exceeded` | `tokens` | **等一等，再发** |

第三行和第四行：**状态码一样，`type` 一样，该做的事正相反。** 能把它们分开的只有 `code`。这张表决定了后面代码的形状。

---

## §5 一个会按要求出错的假服务器

真的 429 不能放进测试里（要钱，要网，还不稳定）。第 -1 章就有一个本地的假服务器 `stub.py`，会重放事先录下来的回答。这一章教它**按要求出错**。

`stub.py` 里新增的部分（开头多一行 `import time`）：

```python
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
# the whole of chapter 12 is about a second request that knows about the first.
_QUEUES: dict[str, list[dict[str, Any]]] = {}

# Every request body this process has served, in order.  A retry is only
# observable from the server's side -- "was the same thing sent twice" is not a
# question the client can answer about itself -- and F12-09 is exactly that
# question asked of the history instead.
REQUESTS: list[dict[str, Any]] = []


def reset() -> None:
    _QUEUES.clear()
    REQUESTS.clear()

```

> - 四段**录下来的**失败：`RATE_LIMITED`、`CONTEXT_LENGTH_EXCEEDED`、`BAD_TOOL_SCHEMA`、`BAD_API_KEY`，原样来自这一章最初的测量（2026-08-12）。
>   第五个 `SERVER_ERROR` 是**编的**，定义的地方明说了：500 没法"要"一个来。
> - **`_QUEUES`**：测试可以给一个编号排一队"先回这几个失败"，每来一个请求用掉一个，用完了才回正常的录音。这样"先两次 429，然后正常回答"在一个地方就写清楚了。
>   它是模块里的全局变量，因为假服务器每来一个请求就新造一个处理对象，记不住上一次。
> - **`REQUESTS`**：这个进程收到过的每一个请求的内容，按顺序。**"同一个东西是不是被发了两次"，只有从服务器这一边才看得见。**
> - `reset()`：清空上面两样。

处理请求的那个类里，多两个方法，`do_POST` 里多一段：

```python
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
                # errors because the whole question this chapter asks is what
                # the *second* attempt does, and `stub_cut` (chapter 0) applies
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
```

> - 请求里带着 `stub_fail`（一个编号和一队失败）时，先看这个编号的队里还有没有；有，就回那个失败。
> - 队里的一项如果是 `{"stream_cut": 7}`，意思不是回一个错误，而是**回一个成功、发 7 块之后把连接断掉**——"流断在一半"是一种以成功的样子到来的失败。
> - `stub_delay`：先睡这么多秒再回答，用来模拟"连上了但不说话"的服务器。

`tests/conftest.py` 多一个 fixture：

```python
@pytest.fixture
def served_requests(stub_url: str) -> Iterator[list[dict]]:
    """Every request body the stub has served this test, in order.

    A retry is only observable from the other side: "was the same thing sent
    twice" is not a question a client can answer about itself.  The list is
    module state on the stub, so it is cleared here rather than in each test --
    a counter that survives one test into the next reports the previous test's
    retries as this one's.

    Depends on `stub_url`, which chapter 12's own test module *overrides* with
    a private, threaded server. That is not a detail: a chapter whose tests
    deliberately abandon connections cannot share a single-threaded server with
    everybody else, and the symptom of trying was a suite that failed in a
    different place on every run.
    """
    stub.reset()
    yield stub.REQUESTS
    stub.reset()
```

这一章的测试文件 `tests/test_faults_ch12.py`，开头和帮手：

```python
"""Chapter 12: retries and error classification.

Every failure body in here is a verbatim recording from api.openai.com on
2026-08-12 (`probe_retry.py shapes` / `overlong` / `ratelimit`), served over a
real socket by `stub.py`.  The one exception is `SERVER_ERROR`, which cannot be
asked for and is marked as invented where it is defined.
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Any

import httpx
import pytest

from minicodex import stub
from minicodex.agent import Agent
from minicodex.agent_types import IncompleteStreamError
from minicodex.compaction import SummaryRequest
from minicodex.mcp import DEFAULT_TOOL_TIMEOUT
from minicodex.model import DEFAULT_ATTEMPT_TIMEOUT, ChatCompletionsModel, ModelHTTPError
from minicodex.retry import (
    DEFAULT_POLICY,
    Failure,
    ModelFailed,
    RetryPolicy,
    classify,
    explain,
    wait_for,
)
from minicodex.shell import DEFAULT_TIMEOUT as SHELL_TIMEOUT
from minicodex.subagent import DEFAULT_TASK_TIMEOUT, TaskResult
from minicodex.tools import TOOL_SCHEMAS

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

NO_WAIT = RetryPolicy(attempts=4, base=0.001, cap=0.001, budget=90.0)

# A 429 with no `retry-after` header at all.  Used wherever a test needs the
# retry to actually happen: the *recorded* 429 asks for 45.175 seconds and the
# loop honours it, so a test driving the real recording through the loop would
# sit through 45 seconds to assert something about classification.  That is not
# a workaround, it is the measurement in `test_F12_02_only_one_retry_fits`:
# with real numbers the default policy allows one retry of a rate limit, not
# four.  Providers that send no header exist, which is why `wait_for` has a
# fallback schedule at all.
BARE_429 = {
    "status": 429,
    "body": {"error": {"message": "Rate limit reached", "code": "rate_limit_exceeded"}},
}


def failing(stub_url: str, queue_id: str, *responses: dict[str, Any], **extra: Any) -> Any:
    """A client whose next requests hit the queued failures, then a recording."""
    return ChatCompletionsModel(
        base_url=stub_url,
        tools=TOOL_SCHEMAS,
        extra_body={"stub_fail": {"id": queue_id, "responses": list(responses)}, **extra},
    )


def http(recorded: dict[str, Any]) -> ModelHTTPError:
    """Rebuild the exception the client would raise for a recorded response."""
    import json as _json

    return ModelHTTPError(
        status=recorded["status"],
        url="https://api.openai.com/v1/chat/completions",
        headers={**recorded.get("headers", {}), "x-request-id": "req_abc"},
        body=_json.dumps(recorded["body"]),
    )


async def _tool(args: dict[str, Any]) -> str:
    return '__version__ = "0.0.1"'


TOOLS = {"read_file": _tool}


@pytest.fixture
def stub_url() -> Any:
    """A fresh, threaded stub server for every test in this module.

    This deliberately **overrides** `conftest`'s session-scoped one, and the
    reason is a flaky suite rather than a preference. Chapter 12's tests abort
    connections on purpose -- that is what a timeout, a cancellation and a
    truncated stream all look like from the server's side -- and the shared
    stub is a single-threaded `HTTPServer` whose module-level request log is
    process-wide. Sharing it produced a suite that failed in a *different
    place* on each run: an abandoned request from one test being served during
    the next one, adding a request nobody counted on.

    F14-02 says flakiness is a defect. Three runs of the same file failing
    three different ways is that defect arriving early enough to fix.
    """
    import threading
    from http.server import ThreadingHTTPServer

    stub.reset()
    server = ThreadingHTTPServer(("127.0.0.1", 0), stub._Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}/v1"
    finally:
        server.shutdown()
        server.server_close()
        stub.reset()


def long_history() -> Any:
    """A conversation with something in it worth cutting.

    Several turns rather than one message, because the interesting behaviour of
    a forced compaction is what it *drops*, and a one-message history has
    nothing to drop -- which is now its own failure (`nothing left to
    compact`), not a compaction that reports success and changes nothing.
    """
    from minicodex.agent_types import ToolCall
    from minicodex.history import History

    history = History()
    history.add_user("go through the modules and tell me what they do")
    for n in range(6):
        call = ToolCall(f"call_{n}", "read_file", {"path": f"m{n}.py"}, "{}")
        history.add_assistant(f"reading module {n}", (call,))
        body = "code = 1\n" * 200
        history.add_tool_result(f"call_{n}", f"# module {n}\n{body}")
    return history

```

> - **`NO_WAIT`**：一份"几乎不等"的重试策略，测试用。
> - **`BARE_429`**：一个**不带** `retry-after` 头的 429。注释解释了为什么需要它：录下来的那个真的 429 要求等 45 秒，循环会照办——用它测"会不会重试"，每个测试得坐 45 秒。
> - **`failing(stub_url, 编号, *失败们)`**：造一个模型客户端，它的前几个请求会撞上排好的失败，之后是正常的录音。
> - **`http(recorded)`**：把一段录下来的失败，变回客户端当时会抛的那个异常。
> - **这个文件自己的 `stub_url`**：每个测试一个新的、**多线程**的假服务器，**盖住**了 `conftest.py` 里全体共用的那个。原因在 §13.5。
> - **`long_history()`**：一段有东西可删的对话（六轮读文件），§9 用。

```bash
git add src/minicodex/stub.py tests/conftest.py tests/test_faults_ch12.py
git commit -m "test(stub): replay five recorded provider failures on request"
```

---

## §6 F12-01：这是哪一种失败

### 6.1 "全都重试"的代价

最顺手的修法是 `for attempt in range(5):`，不是 200 就再来。对着一个永远不会成功的请求（工具名里有空格）量一下（不联网）：

```
$ uv run python probe_retry.py naive

--- (a) one attempt only: one 429 before a two-turn conversation
  ModelFailed: the model call failed: Rate limit reached for gpt-4o-mini in organization org-XXXX on tokens per min (TPM): Li...
  requests the server saw: 1

--- (b) retry-everything, against a request that can never succeed
  requests the server saw: 5
  seconds spent: 8.5
  the body was byte-identical every time, and so was the answer

--- (c) exponential backoff vs the number the server sent
  attempt 1: slept   1.0s, elapsed   1.0s
  attempt 2: slept   2.0s, elapsed   3.0s
  attempt 3: slept   4.0s, elapsed   7.0s
  attempt 4: slept   8.0s, elapsed  15.0s
  attempt 5: slept  16.0s, elapsed  31.0s
  the header said: retry-after 46 (retry-after-ms 45175)
  five attempts fit inside 31s; the window had not opened at any of them
```

- **(a)** 不重试（这一章之前的程序）：一个 429，就结束了一段本来两轮就能完成的对话。
- **(b)** 全都重试：五个一模一样的请求，八秒半，五个一模一样的拒绝。而 §4 量过，被拒绝的请求照样扣额度——**重试一个不可能成功的请求，正是把下一个请求推进 429 的东西**。
- **(c)** 自己定的退避（1、2、4、8、16 秒）对着真的 429：五次尝试全在 31 秒之内，而服务器说的是 46 秒——**每一次都发在窗口打开之前，每一次都是又一次扣额度。**

所以这一章要回答的第一个问题不是"重试几次"，而是：**这是哪一种失败。** 答案有三个值，因为要做的事有三种：

| | 意思 | 做什么 |
|---|---|---|
| `retry` | 同一个请求，过一会儿可能就行了 | 等，再发 |
| `shrink` | 请求太大 | 先把对话压小，再发 |
| `fatal` | 再发也不可能成功 | 停，说清楚 |

### 6.2 在边界上把失败读明白

第 1 章的规矩：外面来的东西，形状只在边界上处理一次。出错的回答也是外面来的东西。`model.py`：

```python
# How long one attempt may go without hearing from the server -- `httpx` applies
# it per read, so it bounds silence, not the length of a stream that keeps
# arriving (see `RetryPolicy`).  It was 300 seconds, chosen when this was the
# only clock in the program, and 300 is also `subagent.DEFAULT_TASK_TIMEOUT` --
# so a child whose model call hung reached its own deadline and the request's
# deadline at the same instant, and which of the two fired first decided
# whether the parent saw a `timeout` outcome or an exception.  Chapter 12 makes
# the nesting strict: 120 for one attempt, plus a 90-second retry budget, fits
# inside the 300 a sub-task gets.  `test_F12_06_*` asserts the ordering rather
# than leaving it to whoever edits one of the numbers next.
DEFAULT_ATTEMPT_TIMEOUT = 120.0
```

（这个常量 §10 用。）

```python
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
```

> - 十一章里它只是一句拼出来的字符串。现在它带着字段：`status`、`headers`（名字全转成小写）、`body`，以及从正文里读出来的 `code`、`type`、`message`，还有 **`request_id`**。
> - **正文读不成 JSON 也不能出事**：`try ... except ValueError`。挡在你和服务商之间的代理或者网关出问题时，回来的不是那种 JSON，而是一页 HTML。
>   一个以为"一定是 JSON"的解析，会把别人的故障变成从**我们自己的错误处理**里抛出来的一个解析错误。
> - 正文原样留着一份（`body`），读不懂的时候至少还能给人看。

`stream()` 里抛它的地方相应地变成：

```python
                if resp.status_code != 200:
                    detail = (await resp.aread()).decode("utf-8", "replace")[:2000]
                    raise ModelHTTPError(
                        status=resp.status_code,
                        url=url,
                        headers=resp.headers,
                        body=detail,
                    )
```

第 0 章的 `IncompleteStreamError`（流没等到结束标记就断了）要被新模块 `retry.py` 认出来，而 `agent.py` 又要导入 `retry.py`——于是这个类型搬到了两者的下面，`agent_types.py`。这是那条"两层都要用的类型往下挪"的规矩第四次用上，也是第一次不需要讨论：

```python
class IncompleteStreamError(RuntimeError):
    """The connection ended before the server sent its `[DONE]` sentinel.

    Raised by `agent.py`, classified by `retry.py`, which is the fourth
    application of the rule at the top of this file and the first one that
    needed no discussion.  `agent.py` re-exports the name, so the eleven
    chapters' worth of tests that import it from there keep working.
    """
```

（`agent.py` 里原来定义它的地方，换成一行说明和一个 `__all__`：名字仍然可以从 `agent` 导入，前面十一章的测试不用动。）

### 6.3 `retry.py`：分类

新建 `src/minicodex/retry.py`。开头：

```python
"""Deciding what a failed request means, and whether to send it again.

Three chapters of measurement were only possible because the *probe scripts*
had a retry loop:

    # probe_subagent.py, written in chapter 10
    async def _retry(make, attempts=4):
        ...
        except (httpx.ConnectError, httpx.RemoteProtocolError, httpx.ReadError):
            await asyncio.sleep(2 * (attempt + 1))

Two copies of it, in two probes, with a comment saying "not a fix for anything
-- chapter 12 is where retries are designed".  The shipped program had none:
one dropped connection ended the run, printed a traceback, and left whatever
the model had already done on disk with no note anywhere saying why it stopped.

The naive repair -- wrap the call in `for attempt in range(5)` -- is worse than
it looks, and the reason is measured rather than argued.  Against a request
the provider will never accept (a tool name with a space in it), five attempts
sent five byte-identical bodies, took nine seconds, and got the same answer
five times.  Against a real 429 the account's own headers said `retry-after:
46`, and a 1-2-4-8-16 backoff makes all five of its attempts inside 31 seconds
-- every one of them before the window opens, and every one of them another
request against the limit that is already exhausted.

So this module answers one question -- *what kind of failure is this* -- and
the answer has three values, because there are three different things to do:

    retry   the same request may work later
    shrink  the request is too big; the conversation must lose weight first
    fatal   sending this again cannot succeed

`status` cannot produce that answer: `context_length_exceeded` and "your tool
name has a space in it" are both HTTP 400 with `type: invalid_request_error`,
and they are the two extremes of the table.  `code` produces it.
"""

from __future__ import annotations

import random
import re
from dataclasses import dataclass
from typing import Literal

import httpx

from minicodex.agent_types import IncompleteStreamError
from minicodex.model import ModelHTTPError

Disposition = Literal["retry", "shrink", "fatal"]
```

```python
@dataclass(frozen=True)
class Failure:
    """One failed attempt, in the terms the loop can act on.

    `kind` is a slug rather than the provider's `code` verbatim: the loop and
    the tests need a word that means the same thing across providers, and the
    provider's own `code` is kept in `detail` for the human who has to go and
    look it up.

    `retry_after` is seconds, and it comes from the server or it is `None`.  It
    is deliberately not "our backoff, but at least this long": if the server
    says 45 seconds and the caller's whole budget is 30, the answer is to stop
    -- waiting 30 and asking anyway is the behaviour that extends the outage.
    """

    disposition: Disposition
    kind: str
    detail: str
    retry_after: float | None = None
    request_id: str | None = None
    # (limit, used), when the server said so.  A rejected request is the only
    # place a provider ever states the true token count of a request it did not
    # process -- there is no usage chunk on a failure, so chapter 6's
    # calibration has no other source for the one request that mattered most.
    stated_tokens: tuple[int, int] | None = None
```

> - **`Failure`**：一次失败，换成循环用得上的说法。`disposition` 是那三个值之一；`kind` 是一个我们自己起的短名字（`rate_limit`、`auth`……），不照抄服务商的 `code`——循环和测试需要一个在不同服务商之间意思相同的词；`detail` 给人看。
> - `retry_after`：服务器要求等的秒数，**来自服务器，否则就是 `None`**。它故意不是"我们的退避，但至少这么久"（§7）。
> - `stated_tokens`：服务器在拒绝时说出的"(上限, 你发了多少)"——§9 用。

```python
# Transport failures that are worth another attempt: the request may not have
# arrived, or the answer may not have got back.  `httpx.TransportError` covers
# the timeouts, the network errors and `RemoteProtocolError` (a server that
# hung up mid-stream).  Two of its subclasses are excluded because they are not
# weather, they are bugs in this program: `LocalProtocolError` is a request we
# built wrongly, and `UnsupportedProtocol` is a URL with no scheme.  Retrying
# either produces the same failure at a slower rate.
_OUR_FAULT = (httpx.LocalProtocolError, httpx.UnsupportedProtocol)

# Provider `code` values, from the five recorded failures plus the two shapes
# every provider of this API sends.  Anything not listed falls through to the
# status-code rules below, which are deliberately conservative in the fatal
# direction: see `classify`.
_BY_CODE: dict[str, tuple[Disposition, str]] = {
    "rate_limit_exceeded": ("retry", "rate_limit"),
    "context_length_exceeded": ("shrink", "context_length"),
    "invalid_api_key": ("fatal", "auth"),
    "model_not_found": ("fatal", "unknown_model"),
    "insufficient_quota": ("fatal", "quota"),
    "server_error": ("retry", "server_error"),
}

# "This model's maximum context length is 128000 tokens. However, your messages
# resulted in 160008 tokens."  Two integers in one sentence, in that order.
_TOKENS = re.compile(r"maximum context length is (\d+) tokens.*?resulted in (\d+) tokens", re.S)
```

```python
def classify(exc: BaseException) -> Failure:
    """Turn whatever came back into one of three decisions.

    The default for something unrecognised is `fatal`, which is the opposite of
    chapter 5's rule for unrecognised shell syntax ("unknown asks").  The two
    situations look alike and are not: chapter 5 has a human at the prompt to
    ask, and this loop has nobody.  The two mistakes are not symmetric either.
    Calling something fatal that was retryable costs one run and prints the
    reason.  Calling something retryable that was fatal costs the quota, in
    silence, and the measured cost is not one request: a rejected 160k-token
    request still debited its 160k tokens from the account's token bucket
    (remaining went 199,996 -> 19,998), so the retries of a request that cannot
    succeed are what pushes the *next* request into a 429.
    """
    if isinstance(exc, ModelHTTPError):
        return _http(exc)
    if isinstance(exc, IncompleteStreamError):
        # Nothing was committed: `_collect` returns only on `[DONE]`, so a
        # stream that died halfway left no half-turn anywhere (F00-04, and
        # F12-03 for free).  The whole attempt can be made again.
        return Failure("retry", "incomplete_stream", str(exc))
    if isinstance(exc, _OUR_FAULT):
        return Failure("fatal", "client_bug", f"{type(exc).__name__}: {exc}")
    if isinstance(exc, httpx.TransportError):
        return Failure("retry", "transport", f"{type(exc).__name__}: {exc}")
    return Failure("fatal", "unexpected", f"{type(exc).__name__}: {exc}")


def _http(exc: ModelHTTPError) -> Failure:
    detail = exc.message or exc.body[:300]
    if exc.code:
        detail = f"{detail} [{exc.code}]"

    disposition, kind = _BY_CODE.get(exc.code or "", ("", ""))
    if not disposition:
        if exc.status == 429:
            disposition, kind = "retry", "rate_limit"
        elif exc.status >= 500 or exc.status == 408:
            disposition, kind = "retry", "server_error"
        else:
            disposition, kind = "fatal", f"http_{exc.status}"

    stated = None
    if kind == "context_length" and (m := _TOKENS.search(exc.message or "")):
        stated = (int(m.group(1)), int(m.group(2)))

    return Failure(
        disposition,  # type: ignore[arg-type]
        kind,
        detail,
        retry_after=_retry_after(exc.headers),
        request_id=exc.request_id,
        stated_tokens=stated,
    )
```

> - **`classify(exc)`**：不管抛出来的是什么，变成一个 `Failure`。
>   - HTTP 层面的失败：交给 `_http`。
>   - 流断在一半：`retry`。
>   - 连接层面的问题（超时、断线、对方挂了电话）：`retry`——**除了两种**：请求本身被我们造错了、网址没写协议。那不是天气，是我们的 bug；重试只会用更慢的速度得到同一个失败。
>   - **其余一切：`fatal`。**
> - **`_http(exc)`**：先按 `code` 查表（§4 的那张表）；表里没有的，再按状态码兜底——429 重试，5 开头和 408 重试，**别的都是 `fatal`**。"太长了"的那种，顺便用正则把正文里的两个数字读出来。
> - **不认识的，默认是 `fatal`——和第 5 章正相反。** 第 5 章对不认识的 shell 写法的回答是"问人"；这里没有人可问。而且两种错不对称：
>   把一个本来可以重试的当成没救了，代价是这一次运行，并且屏幕上写着原因；把一个没救的当成可以重试，代价是额度，悄悄地。

```python
def test_F12_01_status_alone_cannot_classify_a_failure() -> None:
    """The measurement this whole module is shaped by.

    Two recorded failures, both HTTP 400, both `type: invalid_request_error`,
    and the correct reactions are at opposite ends of the table: one must be
    retried after the conversation loses weight, the other must never be sent
    again.  Whatever decides between them cannot be reading the status code.
    """
    too_long = http(stub.CONTEXT_LENGTH_EXCEEDED)
    bad_schema = http(stub.BAD_TOOL_SCHEMA)

    assert too_long.status == bad_schema.status == 400
    assert too_long.type == bad_schema.type == "invalid_request_error"

    assert classify(too_long).disposition == "shrink"
    assert classify(bad_schema).disposition == "fatal"


@pytest.mark.parametrize(
    ("recorded", "disposition", "kind"),
    [
        (stub.RATE_LIMITED, "retry", "rate_limit"),
        (stub.SERVER_ERROR, "retry", "server_error"),
        (stub.CONTEXT_LENGTH_EXCEEDED, "shrink", "context_length"),
        (stub.BAD_TOOL_SCHEMA, "fatal", "http_400"),
        (stub.BAD_API_KEY, "fatal", "auth"),
    ],
)
def test_F12_01_every_recorded_failure_has_one_answer(
    recorded: dict[str, Any], disposition: str, kind: str
) -> None:
    failure = classify(http(recorded))
    assert (failure.disposition, failure.kind) == (disposition, kind)


def test_F12_01_an_unrecognised_exception_is_fatal_not_retryable() -> None:
    """The default leans the other way from chapter 5's, on purpose.

    Chapter 5 answers "unknown shell syntax" with *ask the human*.  There is no
    human inside a backoff, and the two mistakes are not symmetric: a wrongly
    terminal failure costs one run and prints why, a wrongly retried one costs
    the quota in silence.
    """
    assert classify(ValueError("something local")).disposition == "fatal"
    assert classify(httpx.ConnectError("no route to host")).disposition == "retry"
    # ...but not the two transport errors that are our own bug rather than
    # weather.  Retrying a malformed request produces the same failure slower.
    assert classify(httpx.LocalProtocolError("bad header")).disposition == "fatal"
    assert classify(httpx.UnsupportedProtocol("no scheme")).disposition == "fatal"


def test_F12_03_an_incomplete_stream_is_retryable() -> None:
    failure = classify(IncompleteStreamError("stream ended ... without a [DONE] sentinel"))
    assert failure.disposition == "retry"
    assert failure.kind == "incomplete_stream"


def test_the_error_path_of_the_model_client_now_has_a_test() -> None:
    """Recorded here because of how it was found rather than what it asserts.

    `grep -rn ModelHTTPError src tests` returned two lines, both in `model.py`.
    The only error path in the client had no test, no caller and no handler for
    eleven chapters -- the single `raise` that every failure in this chapter
    comes out of.
    """
    error = http(stub.BAD_API_KEY)
    assert error.status == 401
    assert error.code == "invalid_api_key"
    assert error.request_id == "req_abc"
    assert "Incorrect API key" in str(error)


def test_a_body_that_is_not_json_does_not_break_the_error_handling() -> None:
    """A proxy or a gateway does not return the provider's envelope.  It
    returns HTML, and a parser that assumes JSON turns somebody else's outage
    into a `JSONDecodeError` raised from inside our own error handling."""
    error = ModelHTTPError(
        status=502,
        url="http://gateway/v1/chat/completions",
        headers={},
        body="<html><head><title>502 Bad Gateway</title></head></html>",
    )
    assert error.code is None
    assert classify(error).disposition == "retry"
    assert "502 Bad Gateway" in str(error)
```

> - 第一个就是 §4 那张表的第三、四行：两个都是 400、`type` 相同，一个 `shrink`，一个 `fatal`。
> - 第二个用 `parametrize` 把五段录音各过一遍。
> - 最后两个：那条"从来没人走过的路"现在有测试了；一页 502 的 HTML 不会让错误处理自己出错，而且被归为 `retry`。

---

## §7 F12-02：等多久，服务器已经说了

```python
def _retry_after(headers: dict[str, str]) -> float | None:
    """How long the server asked for, in seconds.

    Both headers are read because both were sent: `retry-after: 46` alongside
    `retry-after-ms: 45175`.  The whole-second one is rounded *up*, so the
    millisecond one is preferred where it exists -- not for the 0.8 seconds,
    but because a number that has been rounded is a number somebody has already
    decided is approximate.

    RFC 9110 also allows an HTTP date here.  No provider measured in this book
    has ever sent one, so it is not parsed: an unreadable header returns `None`
    and the caller falls back to its own backoff, which is the same thing it
    does for a 429 with no header at all.
    """
    for name, scale in (("retry-after-ms", 0.001), ("retry-after", 1.0)):
        raw = headers.get(name)
        if raw is None:
            continue
        try:
            return max(0.0, float(raw.strip()) * scale)
        except ValueError:
            continue
    return None
```

> 两个头都读，**毫秒的那个优先**：整秒的那个是往上取整过的（45.19 → 46）。读不懂的（比如标准还允许的另一种写法：一个日期）返回 `None`，和"没有这个头"落在同一个地方。

```python
@dataclass(frozen=True)
class RetryPolicy:
    """How many times, how long, and how long in total.

    `budget` is the field that does the real work and is the one a plain
    attempt count does not have.  Four attempts of "whatever the server asked
    for" is an unbounded wait: chapter 10's sub-agent gets 300 seconds for a
    whole task, and a parent that sits in backoff for six minutes has spent a
    turn budget on sleeping.  With a budget, the answer to "the server said 45s
    and I have 20s left" is to stop and say so, rather than to wait 20 and ask
    anyway.

    It is measured as **wall clock for the whole turn**, not as time spent
    sleeping.  The first version counted only the waits, and against a hanging
    server that bounds nothing at all -- four attempts that each time out spend
    eight minutes without a single `sleep`.  What that buys is one statement
    about a server that has *stopped talking*: no new attempt starts after
    `budget`, and a silent attempt is abandoned after `DEFAULT_ATTEMPT_TIMEOUT`,
    so a turn against a dead server ends within `budget + attempt` -- 90 + 120
    = 210 seconds, inside the 300 a sub-task gets.

    It is **not** a bound on a server that keeps talking, and this docstring
    used to say it was.  `DEFAULT_ATTEMPT_TIMEOUT` is handed to `httpx` as a
    timeout per read, so it limits the silence between two chunks, not the
    length of the stream: measured, a response that sent a chunk every 0.3
    seconds for 3 seconds finished normally under a 0.5-second timeout.  A long
    answer is a legitimate thing for a model to produce, so that is the right
    behaviour -- and it means the only limit on a slow but live stream is the
    sub-task deadline for a child, and nothing at all for the top-level run.

    The defaults are nested inside the timeouts that already exist (F12-06):

        shell command      30s   (chapter 2)
        MCP tool call      60s   (chapter 9)
        one model attempt 120s   (this chapter; it was 300)
        retry budget       90s   (this chapter)
        one sub-task      300s   (chapter 10)

    120 + 90 < 300, so a sub-agent's model call can fail, back off and succeed
    inside the deadline its parent gave it.  Before this chapter the model's
    own timeout was 300s -- exactly the sub-task deadline -- which meant a
    hung request and the deadline expired at the same instant, and which of the
    two happened first decided whether the parent saw a timeout or an
    exception.
    """

    attempts: int = 4
    base: float = 1.0
    cap: float = 30.0
    budget: float = 90.0

DEFAULT_POLICY = RetryPolicy()
```

> 四个数：最多试几次（4）、退避从几秒开始（1）、单次最多等几秒（30）、**为这一次回答一共愿意花多少秒（90）**。
> 真正起作用的是最后一个，§10 细说；docstring 里关于几个钟怎么套在一起的那张表也在 §10。

```python
def wait_for(
    failure: Failure,
    attempt: int,
    policy: RetryPolicy = DEFAULT_POLICY,
    *,
    elapsed: float = 0.0,
    jitter: bool = True,
) -> float | None:
    """How long to wait before attempt number `attempt + 1`, or `None` to stop.

    `None` means the same thing in both of its cases -- out of budget, and "the
    server asked for longer than the whole budget" -- because the caller does
    the same thing with either: it gives up and reports the failure it already
    has.  What differs is the sentence a human reads, and that is `explain()`'s
    job, not this function's.

    The *attempt count* is deliberately not checked here.  It was, for about an
    hour, and mutation testing found the check dead: `Agent._respond` iterates
    `range(policy.attempts)`, so deleting the guard from this function changed
    no behaviour and broke no test.  Two enforcement points for one rule means
    one of them is decoration, and the one to keep is the loop -- it is what
    the reader of the loop can see.

    The jitter is half-to-full rather than none, and it is not superstition:
    this program can have a parent and several sub-agents talking to one
    provider through one account, and a shared limit plus a shared clock means
    they all wake up together.  `jitter=False` exists so tests can assert the
    schedule instead of a range.
    """
    if failure.retry_after is not None:
        wait = failure.retry_after
    else:
        wait = min(policy.cap, policy.base * 2**attempt)
        if jitter:
            wait *= random.uniform(0.5, 1.0)

    if elapsed + wait > policy.budget:
        return None
    return wait
```

> 下一次尝试之前等多久；返回 `None` 表示"别等了，停"。
> - 服务器说了就用服务器的；没说才用自己的退避（1、2、4……封顶 30），再乘一个 0.5 到 1 之间的随机数。
>   抖动不是迷信：这个程序可以有一个父 Agent 和好几个子 Agent 用同一个账号，同一个限额加同一个钟，它们会一起醒。`jitter=False` 是给测试用的。
> - **已经花掉的时间加上这次要等的，超过预算：`None`。** 服务器说 45 秒而预算只剩 20 秒时，答案是**停**，不是"等 20 秒然后再问"——早到的那一次既花了一个请求，又把被限流的时间拖长。
> - docstring 记着一处被删掉的代码：这里曾经也检查"试了几次"，变异测试发现那个检查是**死的**——循环自己就是 `for attempt in range(次数)`，这里的那一份删掉之后什么都不变。
>   **同一条规矩有两个执行的地方，其中一个就是装饰。** 留下的是循环里的那个：读循环的人看得见它。

```python
def test_F12_02_retry_after_is_read_from_the_recorded_headers() -> None:
    """`retry-after: 46` and `retry-after-ms: 45175` both arrived.

    The millisecond one wins: the whole-second header is rounded up, and a
    number somebody has already rounded is a number they have already called
    approximate.
    """
    failure = classify(http(stub.RATE_LIMITED))
    assert failure.retry_after == pytest.approx(45.175)


def test_F12_02_the_server_beats_our_own_backoff() -> None:
    """Exponential backoff makes five attempts inside 31 seconds.

    The window measured on a real 429 was 45.175 seconds wide, so all five
    would have been sent while it was shut -- and each one is another request
    against a limit that is already exhausted.
    """
    failure = classify(http(stub.RATE_LIMITED))
    assert wait_for(failure, 0, jitter=False) == pytest.approx(45.175)

    # No header: our own schedule, with the cap applied.
    bare = Failure("retry", "rate_limit", "429 with no headers")
    assert [wait_for(bare, n, jitter=False) for n in range(3)] == [1.0, 2.0, 4.0]


def test_F12_02_only_one_retry_of_a_real_rate_limit_fits_the_budget() -> None:
    """A measurement that only appears once the real numbers are in place.

    The default policy says four attempts.  Against the recorded 429 it gets
    **two**: 45.175 seconds is honoured once, and a second one would take the
    total to 90.35 against a 90-second budget.  "Four attempts" was never a
    description of what happens -- the budget decides, and the server decides
    the budget.
    """
    failure = classify(http(stub.RATE_LIMITED))
    waited = 0.0
    attempts = 1
    # A bounded loop, not `while wait_for(...) is not None`. The first version
    # was the unbounded one, and mutation testing turned it into a hang rather
    # than a failure: with the budget check deleted this loop never ends, the
    # whole suite times out, and a mutation whose symptom is a timeout has
    # measured nothing. Interlude A's rule, one layer up -- the bound goes on
    # the thing being tested, not after it.
    for _ in range(10):
        wait = wait_for(failure, attempts - 1, elapsed=waited)
        if wait is None:
            break
        waited += wait
        attempts += 1
    assert attempts == 2
    assert waited == pytest.approx(45.175)


def test_F12_02_waiting_less_than_asked_is_not_an_option() -> None:
    """Two waits of 45s do not fit in a 90s budget, and half a wait is worse
    than none: it spends the request and arrives before the window opens."""
    failure = classify(http(stub.RATE_LIMITED))
    assert wait_for(failure, 0, elapsed=50.0) is None
    assert wait_for(failure, 1, elapsed=45.175) is None


def test_F12_02_an_unreadable_header_falls_back_rather_than_crashing() -> None:
    """RFC 9110 also allows an HTTP date here.  No provider in this book has
    ever sent one, so it is not parsed -- and an unparsed header must land in
    the same place as a missing one."""
    weird = ModelHTTPError(
        status=429,
        url="http://x/v1/chat/completions",
        headers={"retry-after": "Wed, 21 Oct 2026 07:28:00 GMT"},
        body='{"error": {"code": "rate_limit_exceeded"}}',
    )
    failure = classify(weird)
    assert failure.retry_after is None
    assert wait_for(failure, 0, jitter=False) == 1.0


def test_F12_02_jitter_stays_inside_half_to_full() -> None:
    """A parent and its sub-agents share one account and one clock."""
    bare = Failure("retry", "server_error", "500")
    waits = [wait_for(bare, 2) for _ in range(200)]
    assert all(w is not None and 2.0 <= w <= 4.0 for w in waits)
    assert len({round(w, 3) for w in waits if w is not None}) > 1


def test_F12_06_the_retry_budget_bounds_the_whole_turn() -> None:
    """An attempt count alone does not bound anything: four attempts of
    "whatever the server asked for" is an unbounded wait."""
    slow = Failure("retry", "rate_limit", "429", retry_after=40.0)
    assert wait_for(slow, 0, elapsed=0.0) == 40.0
    assert wait_for(slow, 1, elapsed=40.0) == 40.0
    assert wait_for(slow, 2, elapsed=80.0) is None
```

> 第三个是把真的数字放进去之后才看得见的一件事：默认策略写的是"4 次"，而对着那个真的 429，它只有 **2 次**——等一个 45 秒可以，再等一个就是 90.35 秒，超过了 90 秒的预算。
> **"4 次"从来不是在描述会发生什么。预算说了算，而预算怎么花，服务器说了算。**
> 它的循环写成 `for _ in range(10)` 而不是 `while`，注释说了来历：`while` 的那一版在变异测试下变成了**死循环**——一个症状是"跑不完"的变异，什么也没量到。

```bash
git add src/minicodex/model.py src/minicodex/agent_types.py src/minicodex/agent.py src/minicodex/retry.py tests/test_faults_ch12.py
git commit -m "feat(retry): classify a failed request by its code, and wait as long as the server said"
```

---

## §8 重试套在哪一层——F12-03、F12-09、F12-04

### 8.1 位置决定了三件事

重试的循环可以套在三个地方：模型客户端里面（`model.stream()`）、循环的一整轮外面、或者两者之间。选错了，清单上的 F12-03 和 F12-09 就都是真的；选对了，它们根本不会发生。

- **套在 `model.stream()` 里面不行。** `stream()` 是一边收一边往外交的：连接断掉的时候，它已经交出去了几段文字，**交出去的东西收不回来**。在里面重试，外面看到的就是"前半句、然后又从头来一遍"。
- **套在一整轮外面也不行。** 一轮里包含执行工具；重来一整轮，工具就可能跑两次。
- **正好的位置是"收流，加上把它拼成一轮"这一段的外面。** 第 0 章的 `_collect` 有一条规矩：不等到结束标记，什么都不返回。所以这一段是**最靠里的、失败了就什么都没留下的地方**。
  失败的那一次尝试什么都没产生，于是第二次尝试是"再做一遍"，而不是"多做了一遍"。

`agent.py`（导入多了 `time`、`replace`，和 `retry` 里的六个名字）：

```python
    async def _respond(
        self, history: History, turn_index: int
    ) -> tuple[ModelTurn, History, int, CompactionResult | None]:
        """Get one finished turn out of the provider, or raise `ModelFailed`.

        The retry boundary is here, around *stream plus assembly*, and not one
        level down inside `model.stream()`.  That is not tidiness: `stream()`
        is a generator that has already yielded `TextDelta`s by the time a
        connection drops, and there is no way to un-yield them.  `_collect`
        returns nothing until `[DONE]` arrives (chapter 0, F00-04), so this is
        the innermost place where a failed attempt has produced *nothing* --
        which is what makes a second attempt a repeat rather than a duplicate.
        F12-03 and F12-09 are both consequences of that one property, and
        neither needed code.

        The history is a return value because one disposition changes it:
        `shrink` compacts and tries again, so the caller must not go on using
        the object it passed in.
        """
        # Wall clock for the whole turn, not just the sleeping.  The first
        # version counted only the waits, which made `budget` a bound on
        # nothing: four attempts that each time out after 120 seconds spend
        # eight minutes without sleeping once.  Measured in `probe_retry.py
        # nesting`, where a sub-agent given a 1.0s deadline reported `timeout`
        # 10/10 in *both* configurations -- the child's own deadline was doing
        # all the bounding and the retry budget was decorative.
        began = time.monotonic()
        shrunk = False
        forced: CompactionResult | None = None

        for attempt in range(self.retry_policy.attempts):
            # to_wire() refuses to render a history with unanswered calls, so a
            # loop that forgets to answer one fails here -- locally, with the
            # offending ids named -- instead of as a 400 from whichever provider
            # happens to be strict.  Rebuilt every attempt, because `shrink`
            # replaces the history under us.
            messages = history.to_wire(self.dialect)
            estimated = self._sizer().messages(messages)
            self.recorder.record(
                "request", {"turn": turn_index, "attempt": attempt, "messages": messages}
            )

            try:
                turn = await self._collect(self.model.stream(messages))
            except Exception as exc:  # not BaseException: a Ctrl-C is not a failure
                failure = classify(exc)
            else:
                # The one moment the guess can be checked against the truth.  It
                # is done unconditionally, not only when compaction is enabled:
                # a run that never compacts still produces the observation that
                # tells the next one how wrong its estimator is.
                if turn.usage is not None:
                    self.calibration.observe(
                        estimated=self._raw_estimate(messages), actual=turn.usage.prompt_tokens
                    )
                return turn, history, estimated, forced

            self.recorder.record(
                "model_failure",
                {
                    "turn": turn_index,
                    "attempt": attempt,
                    "disposition": failure.disposition,
                    "kind": failure.kind,
                    "detail": failure.detail,
                    "retry_after": failure.retry_after,
                    "request_id": failure.request_id,
                },
            )

            if failure.disposition == "shrink":
                history, forced = await self._shrink(history, failure, estimated, shrunk=shrunk)
                shrunk = True
                continue

            elapsed = time.monotonic() - began
            if failure.disposition == "fatal":
                raise ModelFailed(failure, attempts=attempt + 1, waited=elapsed)

            wait = wait_for(failure, attempt, self.retry_policy, elapsed=elapsed)
            if wait is None:
                raise ModelFailed(failure, attempts=attempt + 1, waited=elapsed)

            self._say(
                f"[{failure.kind}: waiting {wait:.0f}s, attempt "
                f"{attempt + 2} of {self.retry_policy.attempts}]"
            )
            # `asyncio.sleep`, so a Ctrl-C during the wait lands immediately
            # (F12-07).  The equivalent `time.sleep` would block the event loop
            # for the whole backoff -- and would also stop every other tool,
            # sub-agent and MCP reader in the process, which is a bigger fault
            # than the one it is inside.
            await asyncio.sleep(wait)

        raise ModelFailed(
            failure, attempts=self.retry_policy.attempts, waited=time.monotonic() - began
        )
```

> - **`for attempt in range(次数)`**：每次尝试——把历史渲染成要发的消息、估算大小、记进录制文件、发出去并收齐一轮。
> - **`except Exception`，不是 `except BaseException`**：Ctrl-C 和"被取消"不是失败。把它们也接住，就是把用户的"停"当成一次可以重试的错误（§11）。
> - **成功**（`else`）：和以前一样用服务商报的真实 token 数校准估算，返回。
> - **失败**：`classify`，记进录制文件（`model_failure`），然后看是哪一种——
>   - `shrink`：压缩，`continue`（§9）；
>   - `fatal`：抛 `ModelFailed`；
>   - `retry`：问 `wait_for` 等多久。`None` 就抛 `ModelFailed`；否则在终端上说一声（§11），`await asyncio.sleep(wait)`，下一次。
> - 次数用完：抛 `ModelFailed`。
> - 它返回四样东西，历史是其中之一：`shrink` 会把历史换掉，调用的人不能再用原来的那个。

`run()` 里原来"渲染、估算、记录、发送、校准"的那十几行，换成了：

```python
            # Four values rather than a small class: a `Response` object would
            # have exactly one construction site and one read site, which is
            # interlude B's FB-03 in miniature.  The fourth is a compaction the
            # *provider* forced, which the run still has to report.
            turn, history, estimated, forced = await self._respond(history, turn_index)
            if forced is not None:
                compactions.append(forced)
```

### 8.2 F12-03 和 F12-09：不用写代码

- **F12-03（流断在一半，状态说不清）**：断掉的那次尝试没有交出任何东西，没有"一半"可言。
- **F12-09（重试成功后历史里有残渣）**：失败的尝试没有往历史里写过东西，所以没有残渣。

两条都是"位置"的后果。但"不会发生"值得写成断言，而不是留成一句解释：

```python
async def test_F12_03_a_cut_stream_leaves_nothing_to_reconcile(
    stub_url: str, served_requests: list[dict[str, Any]]
) -> None:
    """The listed fault -- "partial call state ambiguous" -- cannot arise.

    `_collect` returns only on `[DONE]` (chapter 0, F00-04), so an attempt that
    dies halfway has committed nothing at all.  That is what makes the second
    attempt a repeat rather than a duplicate, and it is why the retry boundary
    is around stream *plus assembly* and not inside `model.stream()`, which has
    already yielded events it cannot take back.
    """
    log: list[str] = []

    async def watched(args: dict[str, Any]) -> str:
        log.append("ran")
        return '__version__ = "0.0.1"'

    agent = Agent(
        failing(stub_url, "t3", {"stream_cut": 7}),
        {"read_file": watched},
        retry_policy=NO_WAIT,
    )

    result = await agent.run("What does __init__.py define?")

    assert result.stop_reason == "completed"
    # The cut stream contained a complete `read_file` call in its first seven
    # chunks.  It ran once, not twice, and not zero times.
    assert log == ["ran"]
    assert len(served_requests) == 3


async def test_F12_09_retries_leave_no_trace_in_the_history(
    stub_url: str, served_requests: list[dict[str, Any]]
) -> None:
    """A conversation that survived three failures must be indistinguishable
    from one that had none.

    Not a promise about tidiness: the history is what the *next* request is
    built from, so debris in it is not a cosmetic problem, it is a message the
    model reads.
    """

    async def run(*failures: dict[str, Any]) -> Any:
        stub.reset()
        agent = Agent(
            failing(stub_url, f"t13-{len(failures)}", *failures), TOOLS, retry_policy=NO_WAIT
        )
        return await agent.run("What does __init__.py define?")

    clean = await run()
    bumpy = await run(stub.SERVER_ERROR, BARE_429, {"stream_cut": 3})

    assert bumpy.history.to_wire("chat_completions") == clean.history.to_wire("chat_completions")


async def test_F12_09_but_the_transcript_does_show_them(stub_url: str, tmp_path: Path) -> None:
    """Transparent to the history, visible in the recording.

    The two are different audiences: the model must not read about a retry, and
    the person debugging at four in the morning must be able to.
    """
    from minicodex.recorder import Recorder

    recorder = Recorder(tmp_path / "run.jsonl")
    agent = Agent(
        failing(stub_url, "t14", stub.SERVER_ERROR),
        TOOLS,
        recorder=recorder,
        retry_policy=NO_WAIT,
    )
    await agent.run("What does __init__.py define?")

    events = [json.loads(line) for line in recorder.path.read_text(encoding="utf-8").splitlines()]
    failures = [e for e in events if e["kind"] == "model_failure"]
    assert len(failures) == 1
    assert failures[0]["payload"]["kind"] == "server_error"
    assert failures[0]["payload"]["attempt"] == 0


async def test_F12_09_a_failed_attempt_writes_nothing_to_the_session_file(
    stub_url: str, tmp_path: Path
) -> None:
    """The rollout is the file a resume is rebuilt from.  A failed attempt has
    produced no item, so there is nothing to write -- which is a consequence of
    where the retry boundary sits rather than of anything written here."""
    from minicodex.rollout import RolloutWriter, SessionMeta, new_session_id, rollout_path

    meta = SessionMeta(session_id=new_session_id(), created=0.0, cwd=str(tmp_path))
    writer = RolloutWriter(rollout_path(meta.session_id, tmp_path), meta)
    try:
        agent = Agent(
            failing(stub_url, "t15", stub.SERVER_ERROR, stub.SERVER_ERROR),
            TOOLS,
            rollout=writer,
            retry_policy=NO_WAIT,
        )
        await agent.run("What does __init__.py define?")
    finally:
        writer.release()

    lines = writer.path.read_text(encoding="utf-8").splitlines()
    # header + user + assistant(call) + result + assistant(answer)
    assert len(lines) == 5
```

> - 第一个：流在第 7 块被砍断，而那 7 块里已经有一个**完整的** `read_file` 调用。重试之后，那个工具跑了**一次**——不是两次，也不是零次。
> - 第二个：一段经历了"500、429、流被砍断"的对话，和一段什么都没经历的对话，渲染出来**一字不差**。这不是为了整洁：历史是下一个请求的原料，里面的残渣是模型会读到的话。
> - 第三个是反过来的：**录制文件里看得到**那次失败。两个读者——模型不该读到重试；凌晨四点查问题的人必须读得到。
> - 第四个：会话文件里也没有多出来的行。

循环层面的 F12-01：

```python
async def test_F12_01_a_terminal_failure_is_sent_exactly_once(
    stub_url: str, served_requests: list[dict[str, Any]]
) -> None:
    """The fault as listed: 400s retried forever.

    Five attempts against a request the provider will never accept sent five
    byte-identical bodies and took nine seconds (`probe_retry.py naive`).  The
    cost is not only the nine seconds: a rejected 160k-token request still
    debited its tokens from the account's rate limit, so retrying what cannot
    succeed is what pushes the *next* request into a 429.
    """
    agent = Agent(failing(stub_url, "t1", *[stub.BAD_TOOL_SCHEMA] * 8), TOOLS)

    with pytest.raises(ModelFailed) as excinfo:
        await agent.run("go")

    assert len(served_requests) == 1
    assert excinfo.value.attempts == 1
    assert excinfo.value.failure.disposition == "fatal"


async def test_F12_01_a_retryable_failure_finishes_the_run(
    stub_url: str, served_requests: list[dict[str, Any]]
) -> None:
    """Two 429s in front of a conversation that works: four requests, one answer."""
    agent = Agent(
        failing(stub_url, "t2", BARE_429, BARE_429),
        TOOLS,
        retry_policy=NO_WAIT,
    )

    result = await agent.run("What does __init__.py define?")

    assert result.stop_reason == "completed"
    assert "__version__" in result.final_text
    # two rejected, then the tool-calling turn, then the answer
    assert len(served_requests) == 4


async def test_F12_01_a_retryable_failure_is_not_retried_forever(
    stub_url: str, served_requests: list[dict[str, Any]]
) -> None:
    """A server that is down stays down, and "retryable" is not "unlimited".

    Written after mutation testing: deleting the attempt check from `wait_for`
    left every other test in this file green, because they all reach an answer
    or run out of budget before they run out of attempts.
    """
    agent = Agent(
        failing(stub_url, "t1b", *[stub.SERVER_ERROR] * 20),
        TOOLS,
        retry_policy=NO_WAIT,
    )

    with pytest.raises(ModelFailed) as excinfo:
        await agent.run("go")

    assert len(served_requests) == NO_WAIT.attempts == 4
    assert excinfo.value.attempts == 4


async def test_F12_01_a_hanging_server_runs_out_of_budget_without_sleeping(
    stub_url: str,
) -> None:
    """The other half of the same bound, and the reason `budget` is wall clock.

    Also written after mutation testing: with the budget counting only the time
    spent in `asyncio.sleep`, a server that accepts the connection and then
    says nothing is retried the full attempt count with a full timeout each --
    eight minutes at the shipped numbers, none of it asleep.  Every test here
    was green against that.
    """
    model = ChatCompletionsModel(
        base_url=stub_url,
        tools=TOOL_SCHEMAS,
        timeout=0.1,
        extra_body={"stub_delay": 1.0},
    )
    agent = Agent(
        model,
        TOOLS,
        retry_policy=RetryPolicy(attempts=8, base=0.001, cap=0.001, budget=0.4),
    )

    began = time.monotonic()
    with pytest.raises(ModelFailed) as excinfo:
        await agent.run("go")
    elapsed = time.monotonic() - began

    assert elapsed < 2.0, "the budget must bound a hang, not only a backoff"
    # The seconds alone do not pin it. Eight attempts at 0.1s each is under a
    # second on a fast machine, so with the budget counting only sleep this
    # test stayed green on Linux and went red on Windows, where an abandoned
    # connection costs more. Counting attempts does not depend on the machine:
    # a 0.4s budget cannot have paid for all eight.
    assert excinfo.value.attempts < 8, "every attempt was made; the budget bounded nothing"
```

> - 没救的请求只发**一次**（从服务器那边数的）。
> - 两个 429 之后对话照常完成：服务器一共见到 4 个请求。
> - 一直 500 的服务器：正好 4 次，然后停。这个测试是变异测试之后补的——别的测试要么成功、要么先用完预算，没有一个是"用完次数"的。
> - 最后一个也是变异测试之后补的，§10 讲。

第 0 章的 `tests/test_agent.py` 里有两个测试断言"流断了就抛 `IncompleteStreamError`"。现在流断了会先重试，最后抛的是 `ModelFailed`：

```python
async def test_F00_04_stream_without_the_done_sentinel_is_refused(stub_url: str) -> None:
    """`stub_cut` truncates the recording and withholds `[DONE]`.

    Rewritten in chapter 12, and the rewrite is the point.  The refusal is
    unchanged -- nothing is committed and the message still names the sentinel
    -- but a dropped stream is now a *retryable* failure, so what reaches the
    caller is `ModelFailed` wrapping four attempts of it.  `attempts=1` asks
    for chapter 0's behaviour exactly; without it this test would sit through
    a 1-2-4 second backoff to assert something that has nothing to do with
    waiting.
    """
    agent = Agent(model(stub_url, stub_cut=7), make_tools(), retry_policy=RetryPolicy(attempts=1))

    with pytest.raises(ModelFailed) as excinfo:
        await agent.run("go")

    assert excinfo.value.failure.kind == "incomplete_stream"
    assert "without a [DONE] sentinel" in str(excinfo.value)


async def test_F00_04_nothing_from_a_cut_stream_is_executed(stub_url: str) -> None:
    """The dangerous version does not crash.  It commits a partial turn and
    fails on the next request, somewhere else entirely."""
    log: list[str] = []
    agent = Agent(
        model(stub_url, stub_cut=7), make_tools(log), retry_policy=RetryPolicy(attempts=1)
    )

    with pytest.raises(ModelFailed):
        await agent.run("go")

    assert log == [], "a call from an unfinished stream must never run"


async def test_F00_04_collect_still_raises_the_original_type(stub_url: str) -> None:
    """The type chapter 0 introduced has not gone anywhere.

    It moved down to `agent_types` so `retry.py` can classify it, and
    `agent.py` re-exports the name -- which is what keeps the import at the top
    of this file working, and is worth asserting rather than assuming.
    """
    agent = Agent(model(stub_url, stub_cut=7), make_tools())

    with pytest.raises(IncompleteStreamError):
        await agent._collect(agent.model.stream([{"role": "user", "content": "go"}]))
```

> 前两个加了 `retry_policy=RetryPolicy(attempts=1)`——"只试一次"，也就是第 0 章的行为；不加的话，它们得坐完 1、2、4 秒的退避，去断言一件和等待无关的事。
> 第三个是新的：直接调用 `_collect`，抛出来的仍然是第 0 章的那个类型。

### 8.3 F12-04：幂等键，量出来没用

清单的担心：重试一个其实已经成功了的请求，被收两次钱。开的药是"幂等键"——每个请求带一个唯一的编号，服务器见到重复的编号就不再做第二遍。

量一下这个服务商认不认（让模型"编一个姓氏"，随机性调到最大，同一个键发两次）：

```
$ uv run python probe_retry.py idempotency
--- idempotency attempt 1
  status  200
  x-request-id: req_5943624eada44fedbeb0ee48c4fa9bb4
  body    answer='Clevallen'
--- idempotency attempt 2
  status  200
  x-request-id: req_7098cfae36ef4a6499047aef80545e10
  body    answer='Elysian.'
--- no key attempt 1
  body    answer='Ranthorpe'
--- no key attempt 2
  body    answer='Galdere.'
```

（只列了相关的行。）同一个键，两个不同的回答，两个不同的请求编号，没有任何"这是重放"的标记。**这个接口上，幂等键什么也不做。**（八月的记录里是 `'Nymbria.'` 和 `'Falnitz.'`，结论相同。）

所以模型调用这一步**没法**靠服务商去重，诚实的界限是另一条，而且又是"位置"给的：

- 重试发的是**同一个请求**，不是一个新的——代价最多是钱，不是第二个副作用；
- 一切真有副作用的东西（工具、MCP 调用、子 Agent）都在这个循环的**外面**，永远不会被它重做。第 9 章对"调用发出去之后连接断了"的 MCP 调用做的是同一个决定：做没做成不知道，而"不知道"不靠再做一遍来消除。

```python
async def test_F12_04_a_retry_re_sends_the_same_request(
    stub_url: str, served_requests: list[dict[str, Any]]
) -> None:
    """Measured, and it is the reason the listed fix does not apply here.

    `Idempotency-Key` on `/v1/chat/completions` was measured to do nothing: the
    same key twice returned two different answers, two request ids and two
    debits.  So the model call cannot be deduplicated by the provider, and the
    honest bound is the one this test asserts -- a retry sends *the same thing*
    rather than a new thing, so the cost of getting it wrong is money, not a
    second side effect.  Everything that does have a side effect (tools, MCP
    calls, sub-agents) sits on the other side of the boundary and is never
    re-run by this loop.
    """
    agent = Agent(
        failing(stub_url, "t4", stub.SERVER_ERROR, stub.SERVER_ERROR),
        TOOLS,
        retry_policy=NO_WAIT,
    )

    await agent.run("What does __init__.py define?")

    first, second = served_requests[0], served_requests[1]
    assert first["messages"] == second["messages"]


async def test_F12_04_no_tool_runs_twice_because_of_a_retry(stub_url: str) -> None:
    """The side-effecting half of idempotency, stated as a property of the loop.

    A failure can only interrupt the *model* call: by the time a tool runs, the
    turn has been assembled and committed, and nothing after that point is
    retried.
    """
    log: list[str] = []

    async def watched(args: dict[str, Any]) -> str:
        log.append("ran")
        return '__version__ = "0.0.1"'

    agent = Agent(
        failing(stub_url, "t5", stub.SERVER_ERROR, stub.SERVER_ERROR, stub.SERVER_ERROR),
        {"read_file": watched},
        retry_policy=NO_WAIT,
    )

    await agent.run("What does __init__.py define?")
    assert log == ["ran"]
```

```bash
git add src/minicodex/agent.py tests/test_faults_ch12.py tests/test_agent.py
git commit -m "feat(agent): retry around stream-plus-assembly, where a failed attempt has left nothing behind"
```

---

## §9 F12-05：唯一一种要"改自己"的 400

"你的消息有 160008 个 token，上限是 128000"——这是五种失败里唯一一个关于**我们**的事实。第 6 章按一个**估算**来决定什么时候压缩；这个 400 是在说：估低了。

而且这是真相唯一会走的一条路：被拒绝的请求没有"用量"那一块，第 6 章的校准没有别的办法知道这次——偏偏是最要紧的这次——到底有多大。

```python
# The largest correction one provider refusal may make to chapter 6's token
# ruler.  See `_shrink`: without it, a single stated token count moved the
# ratio to x250 and the run compacted on 11 of its 12 turns.
MAX_REFUSAL_CORRECTION = 4.0
```

```python
    async def _shrink(
        self, history: History, failure: Failure, estimated: int, *, shrunk: bool
    ) -> tuple[History, CompactionResult]:
        """React to "your messages resulted in 160008 tokens".

        This is the one failure the provider hands back that is a fact about
        *us*: chapter 6 compacts on an estimate, and the estimate was low.  A
        400 saying so is ground truth arriving through the error channel, which
        is also the only channel that carries it -- a rejected request produces
        no usage chunk, so the calibration that chapter 6 lives on has no other
        way to learn about the request that mattered most.

        Compaction is forced rather than reconsidered: `_maybe_compact` would
        look at the same estimate that just proved wrong and decide there is
        nothing to do.  Once per turn, because a second identical failure means
        the summary itself does not fit and trying again is a loop.
        """
        if shrunk:
            raise ModelFailed(
                replace(
                    failure,
                    detail=f"{failure.detail} (still too long after compacting once)",
                )
            )
        if self.context_window is None or self.summariser is None:
            raise ModelFailed(failure)

        if failure.stated_tokens is not None:
            _limit, actual = failure.stated_tokens
            # Clamped, and the clamp was written after watching what happens
            # without it.  `Calibration.observe` keeps the *latest* ratio, and
            # this observation does not come from a usage chunk about a request
            # the server answered -- it comes from an error body about a request
            # it refused.  With a stubbed refusal whose stated size has nothing
            # to do with the request actually sent, one observation moved the
            # ratio to x250, and the run then compacted on **11 of its 12
            # turns** and never finished: a correction that large makes every
            # later estimate exceed the window, so compaction fires, cuts,
            # summarises, and the next estimate is still over.
            #
            # Chapter 6 measured this program's raw estimator at 0.43x to 1.04x
            # of the truth, so a single turn asking for more than a 4x
            # correction is not new information about the content mix, it is
            # something else -- a proxy, a different model, a lying body.
            #
            # Measured against the *raw* estimate, not `estimated`.  That one
            # has already been multiplied by the current ratio, and chapter 6
            # found out what feeding a corrected guess back into the ruler
            # does: the ratio of the truth to an already-corrected guess is the
            # correction's error, not the correction.  This site was written
            # after that fix and repeated the mistake it fixed -- with a ratio
            # of 1.5 in place and a refusal stating twice the raw estimate, the
            # ruler was set to 1.33 instead of 2.0.
            raw = self._raw_estimate(history.to_wire(self.dialect))
            correction = actual / raw if raw > 0 else 0.0
            if 0 < correction <= MAX_REFUSAL_CORRECTION:
                self.calibration.observe(estimated=raw, actual=actual)
            else:
                self.recorder.record(
                    "calibration_rejected",
                    {"estimated": raw, "stated": actual, "correction": correction},
                )

        result = await compact(
            history,
            summarise=self.summariser,
            budget=int(self.context_window * COMPACT_TO),
            sizer=self._sizer(),
        )
        self.recorder.record(
            "compaction",
            {
                "forced_by": failure.kind,
                "before_tokens": estimated,
                "dropped": result.plan.drops,
                "generation": result.generation,
                "degraded": result.degraded,
                "calibration": self.calibration.describe(),
                "fits": result.plan.fits,
            },
        )
        if result.plan.saving <= 0:
            # There was nothing to cut.  The request is too big *by itself* --
            # one enormous tool result, or a window smaller than the system
            # prompt (F06-09) -- and sending the identical thing again is
            # F12-01 with a compaction in front of it.
            raise ModelFailed(
                replace(failure, detail=f"{failure.detail} (nothing left to compact)")
            )

        self._say(f"[context too long for the provider; compacted {result.plan.drops} message(s)]")
        return self._rebaseline(result), result
```

> - **一轮里只压一次。** 压过了还是太长，说明装不下的是那份总结本身，再压就是一个带着账单的死循环。
> - 没开压缩（没有窗口大小或者没有总结函数）：什么也做不了，`ModelFailed`——**但不重发**那个一模一样、一样太长的请求。
> - 服务器说出了真实的大小（`stated_tokens`）：拿它去校准那把尺子——**有上限**，见下。
> - **强制压缩**，不是"再考虑一下要不要压"：`_maybe_compact` 会去看那个刚刚被证明是错的估算，然后决定什么都不用做。
> - 压完发现**什么都没省下来**（比如整段对话只有一条巨大的消息）：`ModelFailed`，说"没有可以压的了"。这一支的第一版会报告"压缩了"、其实什么都没变、然后重发同一个请求。

### 9.1 一个顺手的好主意，跑出来是灾难

"服务器告诉了我们真实的 token 数，拿去校准"——这是写这段代码时顺手加的，看起来只有好处。

第一版没有上限。测试用的是那段录下来的"160008 个 token"，而测试里实际发出去的请求只有几百个 token。于是校准得到的比例是 **250 倍**。
此后每一次估算都乘以 250，每一次都"超过窗口"，于是压缩、总结、再估算——还是超。**那次运行在 12 轮里压缩了 11 次，没有做完任何事。**

第 6 章量过，这个程序的估算和真实值之间的比例在 0.43 到 1.04 之间。一次就要求修正 4 倍以上，那不是关于内容的新信息，是别的什么——中间有个代理、换了个模型、正文在说谎。
所以超过 `MAX_REFUSAL_CORRECTION = 4.0` 的不采纳，只记进录制文件（`calibration_rejected`），留给人看。

### 9.2 意外：修过之后，它又犯了第 6 章修过的那个错

改写第 6 章时修过一个问题：校准要喂的是**修正之前**的估算。喂修正之后的，学到的"比例"就不是比例，而是上一次修正的误差——那时候量到的是比例在 1.5 和 1.0 之间来回跳。

`_shrink` 是在那之后写的，而它拿去校准的 `estimated`，是**已经乘过比例**的那个数。同一个错，换了个地方。

这一章原有的每一个测试都从"还没校准过"的 Agent 开始——那时比例是 1，修正前后是同一个数，所以**没有一个测试看得见**。造一个看得见的：尺子已经读 1.5 倍，服务器说的大小正好是原始估算的 2 倍：

```
E       assert 1.3333333333333333 == 2.0 ± 0.02
FAILED tests/test_faults_ch12.py::test_F12_05_a_refusal_corrects_the_raw_estimate_not_the_corrected_one
```

该是 2.0，得到的是 1.33（2 ÷ 1.5）——既不是原来的值，也不是对的值。修法是 `_shrink` 里那三行：从历史重新算一遍**原始**估算，用它来算修正幅度、用它去校准。

> **一个 bug 被修掉之后，值得在整个代码里搜一遍"还有谁在做同一件事"。** 第 11 章的 `SYSTEMROOT`、这一章 §13 的"锁没释放"，都是"对的写法已经在仓库里了，只是在另一个文件"；这一次是反过来——**错的写法被修掉了，然后在另一个地方被重新写了一遍**。

### 9.3 压缩自己失败的时候

第 6 章有一条"降级"的路：总结用的那次模型调用失败了，就不用模型、机械地拼一份说明，接着跑。这对断线是对的。**对 401 是错的**：下一个请求会因为同样的原因失败，所以先降级，等于在一次注定要结束的运行的路上，把对话记录毁了。

`compaction.py`（导入多了 `ModelHTTPError`、`ModelFailed`、`classify`）：

```python
    except Exception as exc:
        # Chapter 12: not every failure of this call deserves the degraded
        # path.  A dropped connection does -- the transcript is rebuilt without
        # a model and the run carries on.  A 401 does not: the very next
        # request fails for the same reason, so degrading here **destroys the
        # transcript on the way to a run that was going to end anyway**, and
        # the reason the user is shown is one layer away from the truth.
        #
        # Narrowed to `ModelHTTPError` on purpose.  `classify` calls anything
        # it does not recognise fatal, and "anything it does not recognise"
        # includes a bug in whatever was passed as `summarise` -- which is
        # precisely the case F06-08 built this branch for.
        if isinstance(exc, ModelHTTPError):
            failure = classify(exc)
            if failure.disposition == "fatal":
                raise ModelFailed(failure) from exc
```

> 只对 `ModelHTTPError` 这样做，而且只有分类成 `fatal` 的才往外抛。范围故意收得这么窄：`classify` 把不认识的东西都叫 `fatal`，而"不认识的东西"里包括传进来的总结函数自己的 bug——那正是第 6 章造这条降级的路要兜住的情形。

```python
async def test_F12_05_a_context_length_refusal_compacts_and_retries(
    stub_url: str, served_requests: list[dict[str, Any]]
) -> None:
    """The provider's 400 is ground truth about a guess chapter 6 makes.

    Compaction fires on a local estimate.  When the estimate is wrong, this is
    how we find out -- and it is the only way we find out, because a rejected
    request produces no usage chunk at all.
    """
    summaries: list[SummaryRequest] = []

    async def summarise(request: SummaryRequest) -> str:
        summaries.append(request)
        return "## Done\nlooked at the modules\n"

    agent = Agent(
        failing(stub_url, "t6", stub.CONTEXT_LENGTH_EXCEEDED),
        TOOLS,
        context_window=8000,
        summariser=summarise,
        retry_policy=NO_WAIT,
        resume_from=long_history(),
    )

    result = await agent.run("What does __init__.py define?")

    assert result.stop_reason == "completed"
    assert len(summaries) == 1, "the refusal forced exactly one compaction"
    assert len(result.compactions) == 1, "and the run reports it"


async def test_F12_05_an_absurd_stated_count_does_not_move_the_ruler(
    stub_url: str, tmp_path: Path
) -> None:
    """ "your messages resulted in 160008 tokens" is a measurement, not prose --
    and feeding it straight into chapter 6's calibration was measurably wrong.

    `Calibration.observe` keeps the *latest* ratio, and this observation does
    not come from a usage chunk about a request the server answered.  The first
    version passed it through unguarded: one observation moved the ratio to
    **x250**, and the run then compacted on **11 of its 12 turns** and never
    finished, because every later estimate was over the window no matter how
    much had just been cut.

    Clamped at 4x, which is far outside the 0.43x-1.04x spread chapter 6
    measured for this program's content.  The number itself is not thrown away
    -- it goes to the transcript, where a human can see it.
    """
    from minicodex.recorder import Recorder

    async def summarise(request: SummaryRequest) -> str:
        return "## Done\nx\n"

    recorder = Recorder(tmp_path / "run.jsonl")
    agent = Agent(
        failing(stub_url, "t7", stub.CONTEXT_LENGTH_EXCEEDED),
        TOOLS,
        context_window=8000,
        summariser=summarise,
        recorder=recorder,
        retry_policy=NO_WAIT,
        resume_from=long_history(),
    )
    await agent.run("What does __init__.py define?")

    assert agent.calibration.ratio < 4.0
    events = [json.loads(line) for line in recorder.path.read_text("utf-8").splitlines()]
    rejected = [e for e in events if e["kind"] == "calibration_rejected"]
    assert rejected and rejected[0]["payload"]["stated"] == 160008


def test_F12_05_the_two_numbers_are_read_out_of_the_message() -> None:
    failure = classify(http(stub.CONTEXT_LENGTH_EXCEEDED))
    assert failure.stated_tokens == (128000, 160008)


async def test_F12_05_without_compaction_it_is_a_fatal_failure_that_says_so(
    stub_url: str,
) -> None:
    """No summariser means nothing can be done about it here.  What must not
    happen is retrying an identical, identically-too-long request."""
    agent = Agent(failing(stub_url, "t8", *[stub.CONTEXT_LENGTH_EXCEEDED] * 4), TOOLS)

    with pytest.raises(ModelFailed) as excinfo:
        await agent.run("go")

    assert excinfo.value.failure.kind == "context_length"
    assert "--context-window" in str(excinfo.value)


async def test_F12_05_a_history_with_nothing_to_cut_is_not_compacted_twice(
    stub_url: str,
) -> None:
    """A one-message conversation that the provider says is too long is a
    conversation whose *single message* is too long (F06-09).

    The first version reported a compaction, changed nothing, and re-sent the
    identical request -- F12-01 with a compaction in front of it.
    """

    async def summarise(request: SummaryRequest) -> str:
        return "## Done\nx\n"

    agent = Agent(
        failing(stub_url, "t9", *[stub.CONTEXT_LENGTH_EXCEEDED] * 4),
        TOOLS,
        context_window=8000,
        summariser=summarise,
        retry_policy=NO_WAIT,
    )

    with pytest.raises(ModelFailed) as excinfo:
        await agent.run("go")

    assert "nothing left to compact" in str(excinfo.value)


async def test_F12_05_compacting_twice_for_one_turn_is_refused(stub_url: str) -> None:
    """If it still does not fit after compacting, the summary is the thing that
    does not fit, and compacting again is a loop with a bill attached."""

    async def summarise(request: SummaryRequest) -> str:
        return "## Done\nx\n"

    agent = Agent(
        failing(stub_url, "t9b", *[stub.CONTEXT_LENGTH_EXCEEDED] * 4),
        TOOLS,
        context_window=8000,
        summariser=summarise,
        retry_policy=NO_WAIT,
        resume_from=long_history(),
    )

    with pytest.raises(ModelFailed) as excinfo:
        await agent.run("go")

    assert "after compacting once" in str(excinfo.value)


async def test_F12_05_a_fatal_failure_inside_compaction_is_not_degraded(
    stub_url: str,
) -> None:
    """Chapter 6 turns a failed summariser call into a deterministic note and
    carries on.  That is right for a dropped connection and wrong for a 401:
    the next request fails identically, so degrading first destroys the
    transcript on the way to a run that was going to end anyway.
    """

    async def summarise(request: SummaryRequest) -> str:
        raise http(stub.BAD_API_KEY)

    agent = Agent(
        failing(stub_url, "t10", stub.CONTEXT_LENGTH_EXCEEDED),
        TOOLS,
        context_window=8000,
        summariser=summarise,
        retry_policy=NO_WAIT,
        resume_from=long_history(),
    )

    with pytest.raises(ModelFailed) as excinfo:
        await agent.run("go")

    assert excinfo.value.failure.kind == "auth"


async def test_F12_05_a_transient_failure_inside_compaction_still_degrades() -> None:
    """The other half of the same branch, so the narrowing stays narrow."""
    from minicodex.compaction import Sizer, compact

    async def summarise(request: SummaryRequest) -> str:
        raise httpx.ConnectError("connection reset")

    result = await compact(long_history(), summarise=summarise, budget=600, sizer=Sizer())
    assert result.degraded


async def test_F12_05_a_retryable_provider_failure_inside_compaction_degrades() -> None:
    """And the case in between, which mutation testing found nothing covering.

    The test above raises something that is not a `ModelHTTPError` at all, so
    it never reaches the `classify` call -- which means "only *fatal* provider
    failures escape" was asserted by nothing.  A 500 from the summariser is a
    provider failure that must still degrade.
    """
    from minicodex.compaction import Sizer, compact

    async def summarise(request: SummaryRequest) -> str:
        raise http(stub.SERVER_ERROR)

    result = await compact(long_history(), summarise=summarise, budget=600, sizer=Sizer())
    assert result.degraded


async def test_F12_05_a_refusal_corrects_the_raw_estimate_not_the_corrected_one() -> None:
    """Chapter 6 fixed this once: the ruler has to be fed the estimate *before*
    correction, or the ratio it learns is the error of the last correction.

    `_shrink` was written afterwards and fed it the corrected one. With the
    ruler already reading x1.5 and a refusal stating exactly twice the raw
    estimate, it came out at x1.33 -- neither the old value nor the true one.
    Every test of this path started from an uncalibrated agent, where raw and
    corrected are the same number, so none of them could see it.
    """

    async def summarise(request: SummaryRequest) -> str:
        return "## Done\nx\n"

    class NeverCalled:
        tools = ()

    agent = Agent(NeverCalled(), TOOLS, context_window=8000, summariser=summarise)
    history = long_history()
    wire = history.to_wire("chat_completions")
    raw = agent._raw_estimate(wire)

    agent.calibration.observe(estimated=100, actual=150)  # the ruler already reads x1.5
    estimated = agent._sizer().messages(wire)  # ...so this is about 1.5 x raw
    assert estimated > raw

    told = Failure("shrink", "context_length", "too long", stated_tokens=(8000, raw * 2))
    await agent._shrink(history, told, estimated, shrunk=False)

    assert agent.calibration.ratio == pytest.approx(2.0, rel=0.01)
```

> - 第二个：那个 160008 没有把尺子带偏（比例小于 4），而它本身在录制文件里。
> - 倒数第二个的 docstring 记着它的来历：它上面那个测试抛的是 `httpx.ConnectError`，不是 `ModelHTTPError`，**根本走不到**那个 `classify`——"只有 fatal 的才往外抛"这句话当时没有任何断言守着。变异测试发现的。
> - 最后一个是 §9.2 的那个。它直接调用 `_shrink`，因为要造的条件（尺子已经校准过、服务器说的数正好是 2 倍）从假服务器那边造不出来。

```bash
git add src/minicodex/agent.py src/minicodex/compaction.py tests/test_faults_ch12.py
git commit -m "feat(agent): a context-length refusal forces one compaction, and corrects the ruler within bounds"
```

---

## §10 F12-06：四个钟

程序里有四个各管各的超时，分散在四个模块、写于四章：

| 钟 | 多久 | 哪一章 |
|---|---|---|
| 一条 shell 命令 | 30 秒 | 第 2 章 |
| 一次 MCP 工具调用 | 60 秒 | 第 9 章 |
| 一次模型请求 | **300 秒** | 第 1 章 |
| 一个子任务 | **300 秒** | 第 10 章 |

后两个是同一个数。一个子 Agent 的模型请求卡住时，请求的超时和子任务的期限**在同一刻到**，谁先响，决定了父 Agent 看到的是 `timeout` 还是一个异常。

把数字按比例缩小，量十次（不联网；假服务器故意比两个钟都慢）：

```
$ uv run python probe_retry.py nesting

--- equal    attempt 1.0 + budget 0.9 vs task 1.0  (300/300, as shipped)
  outcomes over 10 trials: {'timeout': 10}

--- nested   attempt 0.4 + budget 0.3 vs task 1.0  (120/90/300, now)
  outcomes over 10 trials: {'error': 10}
```

两个钟相等：十次都是 `timeout`——子任务的期限先到，父 Agent 只知道"超时了"。一个套在另一个里面：十次都是 `error`——同一个故障，但这次它**说得出是什么**（"模型请求没有回应"）。

所以一次请求的超时改成 120 秒（`DEFAULT_ATTEMPT_TIMEOUT`，§6.2 里那个常量），重试预算 90 秒，120 + 90 < 300。四个数的大小关系写成断言，因为它们住在四个文件里，改其中一个的人不会去读另外三个：

```python
def test_F12_06_the_timeouts_are_strictly_nested() -> None:
    """Four clocks, written in four modules over eleven chapters.

    Before this chapter the model's own timeout was 300 seconds and so was a
    sub-task's, so a hung request and its parent's deadline expired at the same
    instant -- and which fired first decided whether the parent saw a `timeout`
    outcome or an exception.  An assertion rather than a comment, because the
    numbers live in four files and nobody editing one of them will read the
    other three.
    """
    assert SHELL_TIMEOUT < DEFAULT_TOOL_TIMEOUT
    assert DEFAULT_TOOL_TIMEOUT < DEFAULT_ATTEMPT_TIMEOUT
    assert DEFAULT_ATTEMPT_TIMEOUT + DEFAULT_POLICY.budget < DEFAULT_TASK_TIMEOUT
```

### 10.1 而这条断言，一开始是假的

`attempt + budget < task` 这条不等式第一次写下来的时候，`budget` 管的只是**睡觉的时间**——每次退避等了多久，加起来不超过 90 秒。

对着一个"连上了但不说话"的服务器，这个预算什么都管不住：四次尝试、每次等满超时，八分钟，**一秒都没睡**。上面那个探针的第一版，两种配置都是 10/10 的 `timeout`——真正在起作用的一直是子任务自己的期限，重试预算是摆设。

所以预算改成了**整一轮的挂钟时间**（`_respond` 开头的 `began = time.monotonic()`）：从第一次尝试开始算，过了预算就不再开始新的尝试。§8.2 最后那个测试守的就是它：预算 0.4 秒、每次请求 0.1 秒超时、服务器睡 1 秒——必须在 2 秒内放弃。

### 10.2 意外："一次请求最多 120 秒"也不成立

改完之后，`RetryPolicy` 的说明里有一句很漂亮的话："一轮在 `预算 + 一次请求` 之内结束——90 + 120 = 210 秒。"

改写这一章时去验了那个"120 秒"。让一个服务器**每 0.3 秒发一小块、一共发 3 秒**，客户端的超时设成 0.5 秒：

```
attempt timeout 0.5s; the attempt took 3.3s and finished normally (11 events)
```

超时 0.5 秒，请求跑了 3.3 秒，**正常结束**。

这个超时是交给 `httpx` 的，而 `httpx` 把它用在**每一次读**上：它限制的是"两块之间最多沉默多久"，不是"这个请求一共多久"。一个一直有东西在流出来的回答，可以想流多久流多久。

**这个行为是对的**——一个很长的回答不是故障，按总时长掐掉它才是。（本地跑的小模型一秒钟出几个 token，一段正常的回答就能超过两分钟。）错的是那句话。所以改的是**说法**，不是代码：

- 那句"210 秒"只对**不说话**的服务器成立；
- 对一个说得很慢但一直在说的服务器，子 Agent 的界限是子任务的 300 秒，最上层的运行**没有界限**。

`RetryPolicy` 的说明和 `model.py` 里那个常量上面的注释都改了，并且加了一个测试把真实的语义钉住——免得以后有人照着那句旧话去依赖一个不存在的保证：

```python
async def test_F12_06_the_attempt_timeout_bounds_silence_not_the_length_of_a_stream() -> None:
    """What `DEFAULT_ATTEMPT_TIMEOUT` is, pinned so nobody relies on what it is not.

    The first version of this chapter said a turn ends within `budget +
    attempt`, 210 seconds. That is true of a server that has gone quiet. It is
    not true of one that keeps sending: `httpx` applies the timeout to each
    read, so a stream that delivers a chunk every 0.15 seconds outlives a
    0.4-second timeout by as long as it likes. That is the right behaviour -- a
    long answer is not a failure -- and it means a slow, live stream is bounded
    by the sub-task deadline or by nothing.
    """
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    class Trickle(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            self.rfile.read(int(self.headers.get("Content-Length", 0)))
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            for index in range(6):
                chunk = {"choices": [{"index": 0, "delta": {"content": f"{index} "}}]}
                self.wfile.write(b"data: " + json.dumps(chunk).encode() + b"\n\n")
                self.wfile.flush()
                time.sleep(0.15)
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()

        def log_message(self, *args: Any) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Trickle)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        model = ChatCompletionsModel(
            base_url=f"http://127.0.0.1:{server.server_address[1]}/v1", timeout=0.4
        )
        agent = Agent(model, {}, retry_policy=RetryPolicy(attempts=1))
        began = time.monotonic()
        result = await agent.run("go")
        elapsed = time.monotonic() - began
    finally:
        server.shutdown()
        server.server_close()

    assert result.stop_reason == "completed"
    assert elapsed > 0.4, "the stream ran past the timeout and was not cut"
```

> 一个每 0.15 秒发一块的服务器，客户端超时 0.4 秒：运行正常完成，而且用的时间超过了 0.4 秒。

> **一个数字到底管着什么，要去量，不能看它的名字。** "超时 120 秒"读起来像"最多 120 秒"。codex 给同一个东西起的名字里带着"空闲"（idle）两个字——它量的是沉默。

---

## §11 F12-07：等待的时候按 Ctrl-C

退避的等待用的是 `await asyncio.sleep(wait)`。如果写成 `time.sleep(wait)` 会怎样？探针不用信号，换一个问法："等待期间，别的事还能不能发生"——旁边放一个每 50 毫秒跳一下的"心跳"：

```
$ uv run python probe_retry.py interrupt

--- asyncio.sleep(1.0)
  the cancel could be issued 0.20s in (it was asked for at 0.20s)
  and landed     0.1 ms after that
  longest gap between heartbeats    64.7 ms (3 beats)
  total 0.27s

--- time.sleep(1.0)
  the cancel could be issued 1.00s in (it was asked for at 0.20s)
  and landed     0.0 ms after that
  longest gap between heartbeats  1000.2 ms (1 beats)
  total 1.07s
```

`time.sleep` 的那一版：想在 0.2 秒时取消，**直到 1.0 秒才发得出这个取消**；心跳之间最长的间隔从 65 毫秒变成 1000 毫秒。它不只是让 Ctrl-C 迟到——它把整个事件循环停了，连同这个进程里别的每一个工具、子 Agent、MCP 连接。**退避里的这个 bug，比退避要处理的那个故障还大。**

而这个错误写法，第 -1 章打开的那组检查规则本来就会拦下（探针里那一行得专门加一句"这一行别查"才留得住）。

```python
async def test_F12_07_a_cancellation_during_backoff_lands_immediately(
    stub_url: str,
) -> None:
    """`asyncio.sleep`, not `time.sleep`.

    The blocking version does not only make the interrupt late; it stops the
    event loop, and with it every other tool, sub-agent and MCP reader in the
    process.  The bug inside the backoff would be bigger than the one the
    backoff is for.
    """
    agent = Agent(
        failing(stub_url, "t11", stub.RATE_LIMITED),
        TOOLS,
        retry_policy=RetryPolicy(attempts=4, base=30.0, cap=30.0, budget=90.0),
    )

    task = asyncio.ensure_future(agent.run("go"))
    await asyncio.sleep(0.3)  # long enough to be inside the wait
    began = time.monotonic()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert time.monotonic() - began < 1.0


async def test_F12_07_a_cancellation_is_never_classified_as_a_failure(
    stub_url: str,
) -> None:
    """`except Exception` and not `except BaseException`.

    Chapter 7 paid for this one already (F07-04): `CancelledError` has been a
    `BaseException` since 3.8, and the one function whose job is turning
    failures into outputs was letting it through.  Here the mistake would run
    the other way -- a Ctrl-C classified as a failure would be *retried*.
    """

    class Cancels:
        tools = ()

        async def stream(self, messages: Any) -> Any:
            raise asyncio.CancelledError
            yield  # pragma: no cover - makes this an async generator

    agent = Agent(Cancels(), TOOLS, retry_policy=NO_WAIT)
    with pytest.raises(asyncio.CancelledError):
        await agent.run("go")
```

> 第二个守的是 §8.1 的 `except Exception`：一个一开口就"被取消"的模型，`run()` 抛出来的是取消本身，没有被当成失败去重试。

### 11.1 一个不出声的等待，和卡死没有区别

服务器让等 46 秒。屏幕上 46 秒什么都没有，用户看到的是一个卡死的程序。所以 `Agent` 多一个参数 `announce`（一个"拿到一句话就把它说出来"的函数），等待之前说一声：`[rate_limit: waiting 45s, attempt 2 of 4]`。

```python
    def _say(self, message: str) -> None:
        if self.announce is not None:
            self.announce(message)
```

重试策略和 `announce` 都进了插曲 B 的 `Wiring`——"父子都该一样"的那一包：

```python
    # Chapter 12 added the sixth and seventh, and the test pinning this class
    # at five went red, which is that test working rather than being in the
    # way.  Both qualify under the rule above -- something a child must not
    # differ from its parent in.  A child with its own retry policy is
    # interlude B's measured drift arranged deliberately; and a wait nobody is
    # told about is indistinguishable from a hang, whether it happens in the
    # parent's loop or three levels down inside a sub-agent.
    retry_policy: RetryPolicy = DEFAULT_POLICY
    announce: Callable[[str], None] | None = None
```

（`Wiring.agent` 里相应地多传两个；`Agent.__init__` 多收两个。）

插曲 B 有一个测试断言 `Wiring` **恰好**是五个字段，它红了。那不是它碍事，是它在工作：加第六、第七个字段的人，被逼着回答了一句"它们是父子都该一样的吗"。是——子 Agent 有一份自己的重试策略，正是插曲 B 量过的那种"两处各传各的"；而一次没人知道的等待，不管发生在父 Agent 还是三层之下，都像卡死。

```python
def test_FB_03_wiring_carries_only_what_two_call_sites_both_need() -> None:
    """A guard against the other failure mode: a bag that grows.

    `Wiring` earns its existence by being the difference between two agent
    constructions, not by being "config". Every field on it is something a
    sub-agent was measured to be missing, or -- `dialect` -- something that
    must not differ between a parent and its child. Four keyword arguments
    stayed outside it, because those are the ones that genuinely differ.

    Chapter 12 took it from five to seven, and this assertion going red is
    what made that a decision rather than a habit. Both additions are the
    `dialect` kind: a child with its own retry policy is exactly the drift
    interlude B measured, arranged on purpose, and a backoff nobody is told
    about is a hang -- whether it happens in the parent's loop or three
    levels down.
    """
    fields = set(Wiring.__dataclass_fields__)
    assert fields == {
        "recorder",
        "context_window",
        "summariser",
        "max_concurrent_tools",
        "dialect",
        "retry_policy",
        "announce",
    }
```

`__main__.py` 里造 `Wiring` 的地方多一行（它是整个程序里唯一有终端的地方）：

```python
        # The one place in the program that has a terminal.  A backoff that
        # says nothing is a program that looks hung, and the measured wait a
        # real provider asked for was 46 seconds.
        announce=print,
```

```python
async def test_F12_07_a_wait_is_said_out_loud(stub_url: str) -> None:
    """A backoff nobody is told about is indistinguishable from a hang."""
    said: list[str] = []
    agent = Agent(
        failing(stub_url, "t16", BARE_429), TOOLS, retry_policy=NO_WAIT, announce=said.append
    )

    await agent.run("What does __init__.py define?")

    assert said == ["[rate_limit: waiting 0s, attempt 2 of 4]"]


def test_wiring_passes_the_retry_policy_and_the_announcer_to_the_agent() -> None:
    """The two fields this chapter added to `Wiring` are only worth having if
    `Wiring.agent` hands them on. Dropping either line left the suite green:
    the test that pins `Wiring` checks which fields exist, not where they go."""
    from minicodex.agent import Wiring
    from minicodex.agent_types import ToolSet

    class NeverCalled:
        tools = ()

    said: list[str] = []
    policy = RetryPolicy(attempts=2)
    agent = Wiring(retry_policy=policy, announce=said.append).agent(NeverCalled(), ToolSet({}, []))

    assert agent.retry_policy is policy
    agent._say("waiting")
    assert said == ["waiting"]


def test_the_cli_says_when_it_is_waiting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: Any
) -> None:
    """`__main__` is the one place with a terminal, and the one line that
    connects a wait to it is `announce=print`. Without it a real run sits
    silent through whatever the provider asked for -- 46 seconds, measured."""
    from minicodex.__main__ import main
    from minicodex.model import Completed, TextDelta

    calls = {"n": 0}

    async def stream(self: Any, messages: Any) -> Any:
        calls["n"] += 1
        if calls["n"] == 1:
            raise httpx.ConnectError("connection refused")
        yield TextDelta("hello")
        yield Completed("stop")

    monkeypatch.setattr(ChatCompletionsModel, "stream", stream)
    monkeypatch.chdir(tmp_path)

    assert main(["ask", "hi", "--yes", "--session-dir", str(tmp_path / "s")]) == 0

    out = capsys.readouterr().out
    assert "[transport: waiting" in out
    assert "hello" in out
```

> 这三个是改写时加的，对应三行各自可以被删掉而没有测试会红的代码：循环里说那一声的地方；`Wiring.agent` 把两样东西往下传的地方（插曲 B 那个测试查的是"有哪些字段"，不是"它们去了哪"）；`__main__.py` 的 `announce=print`。
> 第三个真的调用 `main()`：把模型客户端的 `stream` 换成"第一次连不上，第二次正常回答"，屏幕上得有 `[transport: waiting`。

---

## §12 F12-08：两个读者

清单说的是"`HTTP 500` 原样交给了模型"。出错的消息有**两种**读者，清单只说了其中一种。

### 12.1 人

```python
def explain(failure: Failure, *, waited: float = 0.0, attempts: int = 1) -> str:
    """What a human is told when the run ends here.

    Chapter 3 measured the model-facing version of this (F03-07): an error
    naming only the failure gets retried verbatim, and one naming the failure,
    the cause and the next action does not.  The reader here is a person, so
    the "next action" is a different kind of thing -- a person can rotate a
    key, top up an account or use a smaller window, and none of those are
    available to a model.  What does not change is that a message with no next
    action in it is a message that gets stared at.

    The request id is included whenever there is one.  It is the only string in
    the whole failure that lets somebody else look up what happened, and it was
    being dropped with the rest of the headers for eleven chapters.
    """
    action = _ACTIONS.get(failure.kind, "")
    lines = [f"the model call failed: {failure.detail}"]
    if attempts > 1:
        lines.append(f"gave up after {attempts} attempts and {waited:.0f}s of waiting")
    elif failure.retry_after is not None:
        lines.append(f"the server asked for {failure.retry_after:.0f}s, longer than this run waits")
    if action:
        lines.append(action)
    if failure.request_id:
        lines.append(f"provider request id: {failure.request_id}")
    return "\n".join(lines)


class ModelFailed(RuntimeError):
    """The end of a run that could not get an answer out of the provider.

    One exception type for all three endings -- fatal, out of attempts, out of
    budget -- because everything above the loop does the same thing with it:
    stop, and say `str(exc)`.  The distinction that matters is carried inside,
    on `.failure`, for the two callers that do look: `__main__` prints it, and
    `subagent.run_task` turns it into a `TaskResult` instead of letting a
    traceback reach a *model*.

    `str(exc)` is already the human-readable explanation, so a caller that does
    the laziest possible thing still prints something useful.  That is not
    politeness: for eleven chapters the laziest possible thing was what every
    caller did, and what it printed was a twenty-line traceback.
    """

    def __init__(self, failure: Failure, *, attempts: int = 1, waited: float = 0.0) -> None:
        self.failure = failure
        self.attempts = attempts
        self.waited = waited
        super().__init__(explain(failure, waited=waited, attempts=attempts))
```

```python
_ACTIONS: dict[str, str] = {
    "auth": "check OPENAI_API_KEY, or pass --provider ollama to run against a local model.",
    "unknown_model": "check --model against the models this account can use.",
    "quota": "this account is out of credit; the request will not succeed until that changes.",
    "rate_limit": "wait for the window to reset, or run against a provider with more headroom.",
    "context_length": (
        "the conversation no longer fits. Pass --context-window so compaction can run, "
        "or start a new session."
    ),
    "client_bug": "this one is ours, not the provider's -- the request was built wrongly.",
    "transport": "the connection did not survive; check the network or the base URL.",
    "server_error": "the provider is having trouble; this one is worth trying again later.",
}


__all__ = [
    "DEFAULT_POLICY",
    "Disposition",
    "Failure",
    "ModelFailed",
    "RetryPolicy",
    "classify",
    "explain",
    "wait_for",
]
```

> - **`explain(failure)`**：给人看的几行。第一行是服务商说的；如果试过几次，说试了几次、等了多久；如果是"服务器要等的比我们愿意等的长"，说出来；
>   然后是 **`_ACTIONS` 里对应的那句下一步**——人能做模型做不了的事：换一个 key、充值、用小一点的窗口。最后是请求编号。
>   第 3 章量过给模型的版本：只说失败的消息会被原样重试。读者换成人，道理不变：**没有下一步的消息，是一条只会被盯着看的消息。**
> - **`ModelFailed`**：一次拿不到回答的运行的结局。三种结局（没救、次数用完、预算用完）用同一个异常，因为上面的每一层对它们做的事相同：停，把 `str(exc)` 说出来。
>   `str(exc)` 本身就是那几行给人看的话——**最偷懒的调用者也会打印出有用的东西**。十一章里，每个调用者做的都是最偷懒的事，而那时打印出来的是二十行报错。

`__main__.py` 里（§13.1 会再看这一段的后半）：

```python
    try:
        result = await agent.run(question)
    except ModelFailed as exc:
        # The one place a provider failure becomes something a person reads.
        # Before this, it was a twenty-line traceback ending in a JSON blob --
        # for a fault whose whole remedy is usually one sentence long.
        print(f"\n{exc}", file=sys.stderr)
        return 1
    finally:
        # Servers are subprocesses this process started, and chapter 2 already
        # paid for the lesson about leaving those behind (F02-08).  In a
        # `finally`, because the interesting exits are the ones that were not
        # planned.
        for client in clients:
            await client.close()
        # Chapter 7 takes an `O_EXCL` lock next to the session file and this
        # line used to sit at the very end of the function, so every crash left
        # one behind.  `RolloutWriter` has had `__enter__`/`__exit__` since the
        # chapter that introduced it, and `run_task` has released in a
        # `finally` since chapter 10 -- the correct pattern already existed in
        # the repository, one module over, exactly like chapter 11's
        # `SYSTEMROOT`.
        writer.release()
```

真的跑一次，用一个假的 key（Windows，2026-10-01）：

```
$ OPENAI_API_KEY=sk-not-a-real-key uv run minicodex ask "hi" --provider openai --yes

the model call failed: Incorrect API key provided: sk-not-a*****-key. You can find your API key at https://platform.openai.com/account/api-keys. [invalid_api_key]
check OPENAI_API_KEY, or pass --provider ollama to run against a local model.
provider request id: req_a727dbf2b3ff42cda475dd6e55f327e0
```

§3 的二十行，变成三行：发生了什么、该做什么、拿什么去问别人。

### 12.2 模型：一扇一直开着的门

第 10 章的 `run_task` 说过"不会因为子 Agent 做的任何事而抛异常"。对**一件子 Agent 完全控制不了的事**，这句话不成立：子 Agent 的模型请求失败时，异常从 `run_task` 里出来，被第 0 章那个"工具出错就变成一句 `Error: ...`"的兜底接住，
于是**父 Agent 的模型**读到的是：

```
Error: spawn_agent raised ModelHTTPError: HTTP 429 from https://... {一段 JSON}
```

一句写给终端的话，交给了一个会读它、并据此决定下一步的东西。清单上的 F12-08 在这个程序里确实发生，而这是它唯一能走的门。

`subagent.py`（导入多一个 `ModelFailed`；`Outcome` 多一种 `"error"`；`_HEADLINE` 和 `_ADVICE` 各多一条）：

```python
@dataclass(frozen=True)
class TaskResult:
    """How a sub-task ended, kept apart from what it said.

    `outcome` exists because `final_text` cannot carry it.  A child that ran
    out of turns and a child that was interrupted both return `''` -- measured,
    not reasoned about -- and a child that finished returns prose that looks
    exactly like the prose of a child that gave up halfway.
    """

    outcome: Outcome
    text: str
    turns: int = 0
    seconds: float = 0.0
    session_id: str = ""
    # Only `error` fills this in: the one sentence explaining a failure that
    # was nothing to do with the task.  A separate field rather than being
    # folded into `text`, because `text` means "what the child said" and a
    # child that never got an answer out of the provider said nothing.
    detail: str = ""

    @property
    def ok(self) -> bool:
        return self.outcome == "ok"

    def render(self) -> str:
        """What the parent's history actually receives.

        A successful task returns its answer and nothing else: a header on
        every single result is a header the model learns to skip.  Everything
        else returns a labelled block saying what happened and what to do
        about it.
        """
        body = clip(self.text.strip(), MAX_TASK_RESULT_CHARS)
        if self.outcome == "ok":
            return body

        headline = _HEADLINE[self.outcome].format(
            turns=self.turns, seconds=self.seconds, detail=self.detail
        )
        header = f"[sub-agent: {headline}]"
        advice = _ADVICE[self.outcome]
        if self.outcome == "error":
            # No "what it had said before that point": there is no before.  The
            # request never produced a turn, so the only honest content is the
            # translated failure and what the parent should do instead.
            return f"{header}\n{advice}"
        if not body:
            return f"{header}\nIt produced no answer at all. {advice}"
        return (
            f"{header}\nWhat it had said before that point, which is not a "
            f"conclusion:\n\n{body}\n\n{advice}"
        )
```

```python
    except ModelFailed as exc:
        # The docstring above has claimed since chapter 10 that this function
        # never raises for anything the sub-agent did.  It was not true for the
        # one thing the sub-agent has no control over at all.  Without this
        # clause a 429 inside a child arrives at the *parent model* as
        # `Error: spawn_agent raised ModelHTTPError: HTTP 429 ...` plus a JSON
        # blob -- a message written for a terminal, handed to something that
        # will read it and decide what to do next.
        #
        # No `_stop(task)` here, and the first version had one: the task has
        # already finished *with this exception*, and `_stop` awaits it, which
        # re-raises it out of the handler that exists to catch it.
        elapsed = time.monotonic() - began
        _say(ctx, f"[sub-agent depth {ctx.depth + 1}: {exc.failure.kind}]")
        return _record(
            ctx,
            TaskResult(
                "error",
                "",
                seconds=elapsed,
                session_id=_id(writer),
                detail=exc.failure.detail,
            ),
        )
```

> - 第八种结局 `error`：标头是"没能运行：原因"，建议是"这是工具链的故障，不是任务的问题。**不要再把它委派出去**；自己做，或者告诉用户是什么拦住了你。"
> - 它渲染时**没有**"在那之前它说过的话"那一段：没有"之前"。请求根本没产生出一轮。
> - `except ModelFailed` 的注释记着一处差点写错的地方：第一版在这里调用了 `_stop(task)`——而这个任务已经**带着这个异常结束了**，`_stop` 会去等它，于是把异常从这个专门用来接住它的地方重新抛了出去。

```python
def test_F12_08_every_explanation_says_what_to_do_next() -> None:
    """Chapter 3's F03-07, for a reader who can rotate a key.

    The failure line alone is what the provider wrote; the second line is the
    only part that is ours, and it is the part that decides whether the message
    gets acted on or stared at.
    """
    for recorded in (stub.BAD_API_KEY, stub.RATE_LIMITED, stub.CONTEXT_LENGTH_EXCEEDED):
        message = explain(classify(http(recorded)))
        assert len(message.splitlines()) >= 2
        assert "req_abc" in message


def test_F12_08_the_request_id_survives_classification() -> None:
    """The only string in the whole failure that lets somebody else look up
    what happened, and it was being dropped with the rest of the headers."""
    assert classify(http(stub.SERVER_ERROR)).request_id == "req_abc"


async def test_F12_08_a_child_failure_reaches_the_parent_as_prose_not_a_traceback(
    stub_url: str, tmp_path: Path
) -> None:
    """The one door in this program through which a provider error reaches a
    *model*.

    `run_task` has documented "never raises for anything the sub-agent did"
    since chapter 10, and a `ModelHTTPError` from the child's own model call
    walked straight through it, through `_run_tool`'s broad `except`, and into
    the parent's history as `Error: spawn_agent raised ModelHTTPError: HTTP 429
    from https://...` followed by a JSON blob.
    """
    from minicodex.agent import Wiring
    from minicodex.approval import AllowAll, Session
    from minicodex.composition import child_tools_builder
    from minicodex.shell import ShellSession
    from minicodex.subagent import SubAgentContext, TaskSpec, run_task

    session = Session(mode="workspace-write", approver=AllowAll())
    ctx = SubAgentContext(
        build_model=lambda schemas: failing(stub_url, "t12", *[BARE_429] * 6),
        root=tmp_path,
        session=session,
        parent_shell=ShellSession(),
        build_tools=child_tools_builder(tmp_path, session),
        wiring=Wiring(retry_policy=NO_WAIT),
        sessions_dir=tmp_path / "sessions",
    )

    result = await run_task(TaskSpec(task="say hello"), ctx)

    assert result.outcome == "error"
    rendered = result.render()
    assert "Traceback" not in rendered and "ModelHTTPError" not in rendered
    assert "could not run" in rendered
    assert "do the work yourself" in rendered


def test_F12_08_the_error_outcome_renders_without_a_before(tmp_path: Path) -> None:
    """A failed provider call has no "what it had said before that point":
    there is no before.  Rendering one would be an empty quotation with a
    heading over it."""
    rendered = TaskResult("error", "", detail="rate limit reached [rate_limit_exceeded]").render()
    assert "before that point" not in rendered
    assert rendered.startswith("[sub-agent: could not run: rate limit reached")
```

```bash
git add src/minicodex/retry.py src/minicodex/agent.py src/minicodex/subagent.py src/minicodex/__main__.py tests/
git commit -m "feat(retry): three lines for a person, an outcome for a model, and a wait that says it is waiting"
```

---

## §13 清单外的

### 13.1 一次崩溃，留下一把锁

第 7 章给每个会话文件配一个 `.lock` 文件，保证同时只有一个写的人。释放它的那一行 `writer.release()`，在 `_ask()` 的**最后一行**——所以它在成功时执行，在失败时从不执行。
每一次崩溃的运行，都在会话目录里留下一把锁，永远留着。

现在没有任何东西因此出问题（每次运行用的是新的文件名），这正是它十一章都没被发现的原因。

修法是 §12.1 那段代码的后半：`try ... finally`。而且——**对的写法早就在这个仓库里了**：`RolloutWriter` 从第 7 章起就能当 `with` 用；第 10 章的 `run_task` 从一开始就是在 `finally` 里释放的。
上一章的 `SYSTEMROOT` 是同一种事：一条知识存在两份，其中一份是对的。

```python
def test_a_crash_no_longer_leaks_the_session_lock(tmp_path: Path, capsys: Any) -> None:
    """Chapter 7 takes an `O_EXCL` lock next to the session file.

    `writer.release()` was the last line of `_ask`, so it ran on success and
    never on failure: every crashed run left a `.lock` behind for as long as
    the directory exists.  Nothing broke -- which is why eleven chapters of
    tracebacks did not turn it up.  `RolloutWriter` has had `__enter__` /
    `__exit__` since the chapter that introduced it, and `run_task` has
    released in a `finally` since chapter 10: the correct pattern already
    existed in this repository, one module over, exactly like chapter 11's
    `SYSTEMROOT`.

    Driven through `main()` rather than through `Agent`, because the leak was
    in the composition and not in the loop -- a test of `Agent` would have gone
    green against the broken version.
    """
    import socket
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer

    from minicodex.__main__ import main

    class AlwaysFails(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            body = json.dumps(stub.BAD_API_KEY["body"]).encode()
            self.send_response(401)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args: Any) -> None:
            pass

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    server = HTTPServer(("127.0.0.1", port), AlwaysFails)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        code = main(
            [
                "ask",
                "go",
                "--base-url",
                f"http://127.0.0.1:{port}/v1",
                "--session-dir",
                str(tmp_path),
                "--yes",
            ]
        )
    finally:
        server.shutdown()
        server.server_close()

    assert code == 1
    assert list(tmp_path.glob("*.lock")) == []
    assert "the model call failed" in capsys.readouterr().err
```

> 它起一个"永远回 401"的小服务器，然后**调用 `main()`**，而不是直接造一个 `Agent`：漏的地方在把东西装起来的那段代码里，不在循环里——对着 `Agent` 写的测试，在有问题的版本上也是绿的。
> 断言：退出码是 1；目录里没有 `.lock`；stderr 上有那句"the model call failed"。

### 13.2 上一章的变异脚本，从来没进过 CI

上一章的正文写过："这些变异和第 9 章的一样，跑在 `postmerge.yml` 里。"那个流程里只有第 9 章和第 10 章的两步。脚本没有错，那句话也不是故意写假的——**它们只是从来没被接到一起**，而每一个测试对此都是绿的。

修法照例是两步：把那一步加上，再加一个让"那句话"不再需要的断言。`tests/test_packaging.py`：

```python
def test_F_1_05_every_mutation_script_runs_somewhere(repo_root: Path) -> None:
    """A mutation script nobody runs is a file, not a check.

    Added in chapter 12, after finding that chapter 11's nineteen mutations
    had never been wired in: the chapter text said they ran in `postmerge.yml`
    "like chapter 9's", the script was correct, the workflow had steps for 9
    and 10 only, and every test in the repository was green about it.

    The general shape has now appeared often enough to be the rule this book
    keeps arriving at: a promise written in prose is not a mechanism. This is
    the same repair as interlude B's layer checker and chapter 3's description
    snapshot -- state the rule where something executes it.
    """
    scripts = sorted(p.name for p in repo_root.glob("probe_mutations*.py"))
    workflow = (repo_root / ".github/workflows/postmerge.yml").read_text(encoding="utf-8")
    missing = [name for name in scripts if name not in workflow]
    assert not missing, f"mutation scripts nothing runs: {missing}"
```

> 目录里每一个 `probe_mutations*.py`，名字都得出现在 `postmerge.yml` 里。

`.github/workflows/postmerge.yml` 新增的部分：

```yaml

      # Chapter 11's twenty-six. The step is here as of chapter 12, and the
      # gap is worth recording rather than quietly closing: chapter 11's own
      # text says "these run in postmerge.yml, like chapter 9's", and the
      # workflow had steps for 9 and 10 only. Nothing was wrong with the
      # script and nothing was wrong with the prose -- they were simply never
      # connected, which is the failure mode a written promise has and an
      # assertion does not. `test_F_1_05_every_mutation_script_runs_somewhere`
      # is the assertion.
      - name: Mutation check (chapter 11)
        run: uv run python probe_mutations_ch11.py

      # Chapter 12's twenty-six. Three survived the first run, and the most
      # useful of the three was not a missing test: `wait_for` and the loop
      # both enforced the attempt count, so deleting either one changed
      # nothing. The repair was to delete the duplicate.
      - name: Mutation check (chapter 12)
        run: uv run python probe_mutations_ch12.py

      # `probe_mutations.py` -- chapter 5's original, generic script, present in
      # every step directory since and, until this line, wired into none of
      # them.
      #
      # This is the chapter's own lesson landing on the chapter. The section
      # above added `test_F_1_05_every_mutation_script_runs_somewhere` under the
      # heading "a promise written in prose is not a mechanism", ran it, and
      # reported in the tutorial that it named *two* files rather than one. Only
      # one of the two was then wired in. The finding was written down and not
      # acted on -- which is the failure the check exists to prevent, committed
      # in the commit that introduced the check, and it left this step shipping
      # a red test.
      #
      # Chapter 13 hit the same assertion while running a full suite and
      # repaired step 13 onward; this line is that repair backported to the step
      # where the check was born, so that this step passes its own tests.
      - name: Mutation check (chapter 5, generic)
        run: uv run python probe_mutations.py
```

> 三步：上一章的、这一章的，以及**第 5 章那份最早的、通用的 `probe_mutations.py`**。最后那段注释记着一件丢人的事：上面那个测试第一次跑的时候，点了**两个**文件的名，当时只接上了其中一个。
> 发现写下来了，没有照着做——**正是这个检查要防的那种事，发生在引入这个检查的那次提交里**，并且让这个快照带着一个红的测试。它是后来跑整套测试时才被撞到、再补回到这里的。

这是这本书第三次走到同一句话：**写成文字的承诺不是机制**（第 3 章的说明快照、插曲 B 的检查器）。

### 13.3 一条"让测试变慢而不是变红"的变异

变异"重试次数不设上限"（把循环改成 100 次）没有让测试变红，而是让它们**变慢**：每个预期失败的测试都要坐完 1、2、4……30 秒的退避直到 90 秒的预算。整个运行超过了时限，被杀掉。

**一个没跑完的运行，什么也没量到。** 所以变异改成了"多试一次"（跑得完），而脚本现在会接住"超时"，把它报告成**没抓到**，而不是抓到了——"改不上的变异算作没抓到"那条规矩的延伸。

被它拖住的那个测试是一个 `while wait_for(...) is not None` 的循环（§7）。现在它有上界：**一个断言"会停下来"的测试，不能靠被测的东西停下来。**

### 13.4 守着"收窄的降级"的测试，走不到那一行

§9.3 说过了：它抛的异常不是那个类型，`classify` 那一行根本没执行。变异测试发现的，不是读出来的。这是连续第五章出现"测试的名字说的比断言多"。

### 13.5 全体共用的假服务器，被这一章的测试撞出了问题

`conftest.py` 里的 `stub_url` 是**整个测试过程共用一个、单线程**的服务器。十一章都好好的——因为别的章的测试都会让请求跑完。

这一章不会。这一章的测试专门做三件事：把流从中间砍断、让客户端 0.1 秒就超时、让服务器睡一秒。从服务器那边看，这三件事是同一件事：**连接被丢下不管了**。
于是一个还在睡的处理过程挡住了下一个测试的请求；而请求记录是进程里全局的，上一个测试留下的请求被数进了下一个测试。

症状不是"某个测试红了"，是**每次跑，红的地方都不一样**。修法是这一章的测试文件**盖住**那个 fixture（§5）：每个测试一个新的、多线程的服务器。

值得记的是中间那一版：一开始只把"让服务器睡一秒"的那个测试单独挪了出去，其余照旧共用。那一版当时是绿的——它解决了**看见的**那一次冲突，没有解决"这一章的测试和一个单线程服务器不匹配"这件事本身。

### 13.6 意外：探针里标着"现在"的那一段，量的是从前

探针 `naive` 的第一小段原来的标题是"(a) today"：现在的程序，一个 429 就结束一段本来能完成的对话。改写时重跑，它打印的是：

```
--- (a) today, one 429 before a two-turn conversation
  completed after 2 turn(s)
  requests the server saw: 3
```

完成了。而且这一小段**闷声跑了 45 秒**。

这段探针是这一章动工**之前**写的，量的是那时的程序。等这一章做完，"现在的程序"已经会重试、会等服务器要求的 45 秒——探针没有跟着变，于是它的标题说的是一件事，量出来的是另一件，而且看起来像是成功。

同一个文件的开头还列着一段 `debris`（"F12-09，不联网"），**文件里根本没有这一段**。

两处都改了：(a) 现在明确地把重试关掉（只试一次）来重现"从前"，标题也改成了它实际做的事；不存在的那一段从说明里删了。
还有一处顺手的：`nesting` 那一段每丢下一个连接，假服务器就往屏幕上印一整段报错，二十个连接印了三屏，真正的两行结果被埋在最底下——现在不印了。

> **探针也是代码，也会过期。** 而它过期的方式很隐蔽：照样能跑，照样有输出，只是量的已经不是标题说的那件事。

```bash
git add src/minicodex/__main__.py tests/ .github/workflows/postmerge.yml probe_retry.py
git commit -m "fix: release the session lock on a crash, and run every mutation script somewhere"
```

---

## §14 逐条验证

### 14.1 整份探针

```python
"""What chapter 12 measured, and how.

Run one section at a time:

    uv run python probe_retry.py shapes       # F12-01/F12-08, real API, ~5 requests
    uv run python probe_retry.py overlong     # F12-05, real API, 1 large rejected request
    uv run python probe_retry.py idempotency  # F12-04, real API, 2 requests
    uv run python probe_retry.py ratelimit    # F12-02, real API, may cost a few cents
    uv run python probe_retry.py naive        # F12-01, no network
    uv run python probe_retry.py interrupt    # F12-07, no network
    uv run python probe_retry.py nesting      # F12-06, no network

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

    class Quiet(HTTPServer):
        """A client that gives up mid-response is the experiment, not an error.

        The default `handle_error` prints a traceback per abandoned connection;
        `nesting` abandons twenty of them, and the two lines of result were
        being printed underneath three screens of `ConnectionAbortedError`.
        """

        def handle_error(self, request: Any, client_address: Any) -> None:
            pass

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    server = Quiet(("127.0.0.1", port), stub._Handler)
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
    from minicodex.retry import ModelFailed, RetryPolicy

    url, server = _serve()
    try:

        async def go() -> None:
            # (a) one 429 in front of a conversation that would have worked,
            # with the loop told to make one attempt only -- which is what the
            # program did before this chapter.  This section was first written
            # against that program and labelled "today"; run against the
            # finished one it sat silent for 45 seconds and reported success,
            # which is a different measurement from the one its label claims.
            model = ChatCompletionsModel(
                base_url=url, model="gemma4:31b", extra_body=_plan("a", stub.RATE_LIMITED)
            )
            agent = Agent(model, {"read_file": _echo}, retry_policy=RetryPolicy(attempts=1))
            print("\n--- (a) one attempt only: one 429 before a two-turn conversation")
            try:
                result = await agent.run("What does __init__.py define?")
                print(f"  {result.stop_reason} after {result.turns_used} turn(s)")
            except ModelFailed as exc:
                print(f"  {type(exc).__name__}: {str(exc).splitlines()[0][:110]}...")
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
```

> - **`INTERESTING`**：值得打印的那些头。**`_show`**：打印一次回答的状态码、头和正文开头。**`_post`**：直接向 OpenAI 发一个请求（不经过这个项目的客户端——这里要看的就是最原始的回答）。**`_tiny`**：一个最小的请求。
> - **`shapes`**、**`overlong`**、**`ratelimit`**（§4）、**`idempotency`**（§8.3）：四段要联网的。
> - **`_serve`**：在一个真的端口上起那个假服务器。**`_plan`**：造"请按这个顺序出错"的那段请求内容。
> - **`naive`**（§6.1）、**`interrupt`**（§11）、**`nesting`**（§10）：三段不联网的。

### 14.2 全部测试，两个系统

```bash
uv run ruff check .
uv run ruff format --check .
uv run python scripts/check_layers.py
uv run pytest
```

Windows（2026-10-01）：

```
All checks passed!
73 files already formatted
1544 passed, 9 skipped in 122.05s (0:02:02)
```

Linux（WSL Ubuntu，Python 3.12，同一天）：

```
1553 passed in 64.45s (0:01:04)
```

（`check_layers.py` 在两个系统上都是五行 `ok`。）

### 14.3 这些测试自己靠得住吗

`probe_mutations_ch12.py`：

```python
"""Do chapter 12's tests fail when chapter 12's code is wrong?

Same script as chapters 9, 10 and 11, pointed at `retry.py`, the retry loop in
`agent.py`, the error parsing in `model.py`, the narrowed degrade branch in
`compaction.py` and the new outcome in `subagent.py`.  Each entry is an edit
that should break something; the script applies it, runs the suite, restores
the file, and reports how many tests noticed.

Restores from `atexit` and a signal handler, not from `finally` alone -- that
is chapter 6's lesson, learned by leaving `if False:` in `tokens.py` after a
Ctrl-C during a pytest run.

    uv run python probe_mutations_ch12.py
"""

from __future__ import annotations

import atexit
import re
import signal
import subprocess
import sys
from pathlib import Path

MUTATIONS = [
    (
        "retry.py",
        "a context-length refusal is terminal like any other 400",
        '"context_length_exceeded": ("shrink", "context_length"),',
        '"context_length_exceeded": ("fatal", "context_length"),',
    ),
    (
        "retry.py",
        "classification reads the status code and nothing else",
        'disposition, kind = _BY_CODE.get(exc.code or "", ("", ""))',
        'disposition, kind = ("", "")',
    ),
    (
        "retry.py",
        "an unrecognised failure is retried instead of reported",
        'return Failure("fatal", "unexpected", f"{type(exc).__name__}: {exc}")',
        'return Failure("retry", "unexpected", f"{type(exc).__name__}: {exc}")',
    ),
    (
        "retry.py",
        "a request this program built wrongly is retried as if it were weather",
        "    if isinstance(exc, _OUR_FAULT):",
        "    if False:",
    ),
    (
        "retry.py",
        "the server's Retry-After is ignored in favour of our own backoff",
        "    if failure.retry_after is not None:",
        "    if False:",
    ),
    (
        "retry.py",
        "the millisecond header loses to the rounded-up whole-second one",
        'for name, scale in (("retry-after-ms", 0.001), ("retry-after", 1.0)):',
        'for name, scale in (("retry-after", 1.0), ("retry-after-ms", 0.001)):',
    ),
    (
        "retry.py",
        "an unparseable Retry-After raises instead of falling back",
        "        except ValueError:\n            continue",
        "        except ValueError:\n            raise",
    ),
    (
        "retry.py",
        "the budget is advisory: wait the server's number even if it does not fit",
        "    if elapsed + wait > policy.budget:\n        return None",
        "    if False:\n        return None",
    ),
    (
        "agent.py",
        "attempts are unbounded",
        "for attempt in range(self.retry_policy.attempts):",
        # Six rather than a hundred, and the number was measured.  With
        # `range(100)` the suite does not go red -- it goes *slow*: every test
        # that expects a failure sits through a 1-2-4-...-30 backoff until it
        # hits the 90-second budget, and the whole run passed the runner's
        # 900-second limit and died.  A mutation whose symptom is a timeout
        # tells you nothing about the tests, because none of them finished.
        "for attempt in range(6):",
    ),
    (
        "retry.py",
        "the failure is reported without a next action",
        '    action = _ACTIONS.get(failure.kind, "")',
        '    action = ""',
    ),
    (
        "retry.py",
        "the request id is dropped again",
        "    if failure.request_id:",
        "    if False:",
    ),
    (
        "model.py",
        "the error body is not parsed, only formatted",
        '        if isinstance(payload, dict) and isinstance(payload.get("error"), dict):',
        "        if False:",
    ),
    (
        "model.py",
        "a non-JSON body from a proxy takes the error handling down with it",
        "        except ValueError:  # includes JSONDecodeError; see the docstring\n"
        "            payload = None",
        "        except ValueError:  # includes JSONDecodeError; see the docstring\n"
        "            raise",
    ),
    (
        "model.py",
        "one attempt may take as long as a whole sub-task",
        "DEFAULT_ATTEMPT_TIMEOUT = 120.0",
        "DEFAULT_ATTEMPT_TIMEOUT = 300.0",
    ),
    (
        "agent.py",
        "a cancellation is classified as a failure and retried",
        "            except Exception as exc:  # not BaseException: a Ctrl-C is not a failure",
        "            except BaseException as exc:  # noqa: BLE001",
    ),
    (
        "agent.py",
        "the turn is compacted as many times as the provider asks",
        "        if shrunk:",
        "        if False:",
    ),
    (
        "agent.py",
        "a compaction that saves nothing still counts, and the request is re-sent",
        "        if result.plan.saving <= 0:",
        "        if False:",
    ),
    (
        "agent.py",
        "the stated token count is fed to the calibration unclamped",
        "            if 0 < correction <= MAX_REFUSAL_CORRECTION:",
        "            if True:",
    ),
    (
        "agent.py",
        "the retry budget counts sleeping only, not the whole turn",
        "            elapsed = time.monotonic() - began",
        "            elapsed = 0.0",
    ),
    (
        "agent.py",
        "failures are not recorded in the transcript",
        '                "model_failure",',
        '                "ignored",',
    ),
    (
        "compaction.py",
        "a fatal provider failure during compaction is degraded like a dropped connection",
        "        if isinstance(exc, ModelHTTPError):",
        "        if False:",
    ),
    (
        "compaction.py",
        "every summariser failure is fatal, including a bug in the summariser",
        '            failure = classify(exc)\n            if failure.disposition == "fatal":',
        "            failure = classify(exc)\n            if True:",
    ),
    (
        "subagent.py",
        "a provider failure inside a child reaches the parent model as a traceback",
        "    except ModelFailed as exc:",
        "    except _NeverRaised as exc:",
    ),
    # The next three were added when the chapter was rewritten.
    (
        "agent.py",
        "a refusal corrects the already-corrected estimate, which chapter 6 fixed once",
        "                self.calibration.observe(estimated=raw, actual=actual)",
        "                self.calibration.observe(estimated=estimated, actual=actual)",
    ),
    (
        "agent.py",
        "Wiring.agent keeps the retry policy to itself",
        "            retry_policy=self.retry_policy,\n",
        "",
    ),
    (
        "__main__.py",
        "a wait is not announced on the terminal",
        "        announce=print,\n        # A client of its own",
        "        # A client of its own",
    ),
]

SRC = Path("src/minicodex")
ORIGINALS = {name: (SRC / name).read_text(encoding="utf-8") for name, *_ in MUTATIONS}
SUITES = [
    "tests/test_faults_ch12.py",
    "tests/test_agent.py",
    "tests/test_compaction.py",
    "tests/test_faults_ch10.py",
]


def restore() -> None:
    for name, text in ORIGINALS.items():
        path = SRC / name
        if path.read_text(encoding="utf-8") != text:
            path.write_text(text, encoding="utf-8")


atexit.register(restore)
signal.signal(signal.SIGINT, lambda *_: sys.exit(130))


def refuse_if_already_mutated() -> None:
    """Do not start on a tree a killed run left dirty (chapter 9's lesson)."""
    dirty = [
        f"{name}: looks like {label!r} is still applied"
        for name, label, before, after in MUTATIONS
        if after and before not in ORIGINALS[name] and after in ORIGINALS[name]
    ]
    if dirty:
        print("refusing to run: the working tree is already mutated\n")
        for line in dirty:
            print(f"  {line}")
        print("\nRestore it (git checkout / re-copy) before running this again.")
        raise SystemExit(2)


def main() -> None:
    refuse_if_already_mutated()
    print(f"{len(MUTATIONS)} mutations, {' '.join(SUITES)}\n")
    survivors = []
    for name, label, before, after in MUTATIONS:
        path = SRC / name
        source = ORIGINALS[name]
        if before not in source:
            print(f"  !! could not apply: {label}")
            survivors.append(label)
            continue
        path.write_text(source.replace(before, after, 1), encoding="utf-8")
        try:
            result = subprocess.run(
                [sys.executable, "-m", "pytest", *SUITES, "-q", "--no-header"],
                timeout=900,
                capture_output=True,
                text=True,
            )
        except subprocess.TimeoutExpired:
            # Not "caught".  A run that did not finish measured nothing, and
            # chapter 6's rule about unapplied mutations applies here too: the
            # only honest report is that this one is unknown.
            print(f"  !! the suite did not finish: {label}")
            survivors.append(label)
            continue
        finally:
            restore()
        failed = len(re.findall(r"^FAILED ", result.stdout, re.M))
        errors = len(re.findall(r"^ERROR ", result.stdout, re.M))
        caught = failed + errors
        print(f"  {caught:>3} test(s) fail  <-  {label}")
        if not caught:
            survivors.append(label)

    print()
    if survivors:
        print(f"{len(survivors)} mutation(s) nothing noticed:")
        for label in survivors:
            print(f"  - {label}")
        raise SystemExit(1)
    print("every mutation was caught.")


if __name__ == "__main__":
    main()
```

> 二十六条。带注释的最后三条是改写时加的（§9.2 的校准、§11.1 的两处接线）。`except subprocess.TimeoutExpired` 那一支是 §13.3。

```
$ uv run python probe_mutations_ch12.py
26 mutations, tests/test_faults_ch12.py tests/test_agent.py tests/test_compaction.py tests/test_faults_ch10.py
    8 test(s) fail  <-  a context-length refusal is terminal like any other 400
   11 test(s) fail  <-  classification reads the status code and nothing else
    1 test(s) fail  <-  an unrecognised failure is retried instead of reported
    1 test(s) fail  <-  a request this program built wrongly is retried as if it were weather
    4 test(s) fail  <-  the server's Retry-After is ignored in favour of our own backoff
    4 test(s) fail  <-  the millisecond header loses to the rounded-up whole-second one
    1 test(s) fail  <-  an unparseable Retry-After raises instead of falling back
    4 test(s) fail  <-  the budget is advisory: wait the server's number even if it does not fit
    1 test(s) fail  <-  attempts are unbounded
    1 test(s) fail  <-  the failure is reported without a next action
    1 test(s) fail  <-  the request id is dropped again
   12 test(s) fail  <-  the error body is not parsed, only formatted
    1 test(s) fail  <-  a non-JSON body from a proxy takes the error handling down with it
    1 test(s) fail  <-  one attempt may take as long as a whole sub-task
    1 test(s) fail  <-  a cancellation is classified as a failure and retried
    1 test(s) fail  <-  the turn is compacted as many times as the provider asks
    1 test(s) fail  <-  a compaction that saves nothing still counts, and the request is re-sent
    2 test(s) fail  <-  the stated token count is fed to the calibration unclamped
    1 test(s) fail  <-  the retry budget counts sleeping only, not the whole turn
    1 test(s) fail  <-  failures are not recorded in the transcript
    1 test(s) fail  <-  a fatal provider failure during compaction is degraded like a dropped connection
    1 test(s) fail  <-  every summariser failure is fatal, including a bug in the summariser
    2 test(s) fail  <-  a provider failure inside a child reaches the parent model as a traceback
    1 test(s) fail  <-  a refusal corrects the already-corrected estimate, which chapter 6 fixed once
    1 test(s) fail  <-  Wiring.agent keeps the retry policy to itself
    1 test(s) fail  <-  a wait is not announced on the terminal
every mutation was caught.
```

（Linux，在一份临时拷贝里跑的。）

这份脚本最初跑的时候有三条没被抓到，其中最有用的一条不是"缺一个测试"，而是 §7 说的那处死代码。

改写时在 Linux 上重跑这份脚本，**有一条没被抓到**：

```
1 mutation(s) nothing noticed:
  - the retry budget counts sleeping only, not the whole turn
```

守着它的是 §10.1 的那个测试，它断言的是"2 秒内放弃"。而八次尝试、每次 0.1 秒超时，在一台快的机器上总共不到一秒——**坏掉的版本也在 2 秒之内**。在 Windows 上，丢下一个连接要慢得多，同一个测试是红的。一个靠秒数的断言，在两台机器上给了两个答案；这份脚本原来报的"全部抓到"，只在其中一台上成立。

现在那个测试另外数**尝试的次数**（`excinfo.value.attempts < 8`）：0.4 秒的预算不可能够八次，这和机器快慢无关。上面那份输出是改完之后的。

改写时另外在一份临时拷贝里做了十五处修改（Linux）：

```
baseline: 0 failed
mutation                                                   failed  first tests to notice
a wait is not announced by the loop                             2  test_F12_07_a_wait_is_said_out_loud, test_the_cli_s
a failed attempt is not written to the transcript               1  test_F12_09_but_the_transcript_does_show_them
a provider-forced compaction is not reported by the run         1  test_F12_05_a_context_length_refusal_compacts_and_r
a rejected calibration leaves no record                         1  test_F12_05_an_absurd_stated_count_does_not_move_th
the forced compaction is not announced                          0
explain() drops the next action                                 1  test_F12_05_without_compaction_it_is_a_fatal_failur
explain() does not say how long it waited                       0
explain() does not say the server asked for too long            0
the child's error outcome loses its reason                      0
a failed child is not recorded among the children               0
cli: a provider failure exits 0                                 1  test_a_crash_no_longer_leaks_the_session_lock
cli: the failure is not printed                                 1  test_a_crash_no_longer_leaks_the_session_lock
jitter is dropped                                               1  test_F12_02_jitter_stays_inside_half_to_full
the millisecond header loses to the whole-second one            3  test_F12_02_only_one_retry_of_a_real_rate_limit_fit
a 5xx with no code is fatal                                     9  test_F12_01_a_retryable_failure_is_not_retried_fore
10/15 caught
  survived: the forced compaction is not announced
  survived: explain() does not say how long it waited
  survived: explain() does not say the server asked for too long
  survived: the child's error outcome loses its reason
  survived: a failed child is not recorded among the children
tree green again: True
```

十五处里五处没有测试会红：强制压缩之后那句告知、`explain()` 里"试了几次、等了多久"和"服务器要等的比我们愿意等的长"两句、子 Agent 的 `error` 结局里的原因、以及失败的子 Agent 有没有被记进那张"这次运行的子 Agent"的清单。

```python
async def test_F12_05_a_forced_compaction_is_said_out_loud(stub_url: str) -> None:
    """A run that quietly threw away most of its conversation because the
    provider refused it is a run whose user should be told."""

    async def summarise(request: SummaryRequest) -> str:
        return "## Done\nlooked at the modules\n"

    said: list[str] = []
    agent = Agent(
        failing(stub_url, "t17", stub.CONTEXT_LENGTH_EXCEEDED),
        TOOLS,
        context_window=8000,
        summariser=summarise,
        retry_policy=NO_WAIT,
        resume_from=long_history(),
        announce=said.append,
    )
    await agent.run("What does __init__.py define?")

    assert any(line.startswith("[context too long for the provider; compacted") for line in said)


def test_F12_08_the_explanation_says_how_the_run_ended() -> None:
    """Two endings that are not the provider's fault and not the user's: the
    attempts ran out, or the server asked for longer than the whole budget.
    Each has one sentence, and each could be deleted with the suite green."""
    gave_up = explain(
        Failure("retry", "server_error", "The server had an error"), waited=12.0, attempts=4
    )
    assert "gave up after 4 attempts and 12s of waiting" in gave_up

    too_long = explain(classify(http(stub.RATE_LIMITED)))
    assert "the server asked for 45s, longer than this run waits" in too_long


async def test_F12_08_a_failed_child_says_why_and_is_counted(stub_url: str, tmp_path: Path) -> None:
    """The `error` outcome has to carry the reason, and the child has to appear
    in the end-of-run listing like any other -- a sub-agent that failed and
    left no line behind is the hardest kind to go looking for."""
    from minicodex.agent import Wiring
    from minicodex.approval import AllowAll, Session
    from minicodex.composition import child_tools_builder
    from minicodex.shell import ShellSession
    from minicodex.subagent import SubAgentContext, TaskSpec, run_task

    session = Session(mode="workspace-write", approver=AllowAll())
    ctx = SubAgentContext(
        build_model=lambda schemas: failing(stub_url, "t18", *[BARE_429] * 6),
        root=tmp_path,
        session=session,
        parent_shell=ShellSession(),
        build_tools=child_tools_builder(tmp_path, session),
        wiring=Wiring(retry_policy=NO_WAIT),
    )

    result = await run_task(TaskSpec(task="say hello"), ctx)

    assert "Rate limit reached" in result.render()
    assert ctx.children == [result]
```

补完之后：

```
baseline: 0 failed
mutation                                                   failed  first tests to notice
a wait is not announced by the loop                             2  test_F12_07_a_wait_is_said_out_loud, test_the_cli_s
a failed attempt is not written to the transcript               1  test_F12_09_but_the_transcript_does_show_them
a provider-forced compaction is not reported by the run         1  test_F12_05_a_context_length_refusal_compacts_and_r
a rejected calibration leaves no record                         1  test_F12_05_an_absurd_stated_count_does_not_move_th
the forced compaction is not announced                          1  test_F12_05_a_forced_compaction_is_said_out_loud
explain() drops the next action                                 1  test_F12_05_without_compaction_it_is_a_fatal_failur
explain() does not say how long it waited                       1  test_F12_08_the_explanation_says_how_the_run_ended
explain() does not say the server asked for too long            1  test_F12_08_the_explanation_says_how_the_run_ended
the child's error outcome loses its reason                      1  test_F12_08_a_failed_child_says_why_and_is_counted
a failed child is not recorded among the children               1  test_F12_08_a_failed_child_says_why_and_is_counted
cli: a provider failure exits 0                                 1  test_a_crash_no_longer_leaks_the_session_lock
cli: the failure is not printed                                 1  test_a_crash_no_longer_leaks_the_session_lock
jitter is dropped                                               1  test_F12_02_jitter_stays_inside_half_to_full
the millisecond header loses to the whole-second one            4  test_F12_02_only_one_retry_of_a_real_rate_limit_fit
a 5xx with no code is fatal                                     9  test_F12_01_a_retryable_failure_is_not_retried_fore
15/15 caught
tree green again: True
```

---

## §15 收工

### 15.1 这一章动了哪些文件

| 文件 | 新增还是改动 | 在哪一节 |
|---|---|---|
| `src/minicodex/retry.py` | 新增 | §6、§7、§12 |
| `src/minicodex/model.py` | `ModelHTTPError` 带上字段；一次请求的超时改成 120 秒 | §6、§10 |
| `src/minicodex/agent_types.py` | `IncompleteStreamError` 搬下来 | §6 |
| `src/minicodex/agent.py` | `_respond`、`_shrink`、`_say`；`Wiring` 多两个字段 | §8、§9、§11 |
| `src/minicodex/compaction.py` | 没救的失败不走降级 | §9 |
| `src/minicodex/subagent.py` | 第八种结局 `error` | §12 |
| `src/minicodex/__main__.py` | 接住 `ModelFailed`；锁在 `finally` 里释放；`announce=print` | §11、§12、§13 |
| `src/minicodex/stub.py` | 五段失败，和按顺序出错的队列 | §5 |
| `tests/test_faults_ch12.py` | 新增 | 分散在各节 |
| `tests/conftest.py`、`tests/test_agent.py`、`tests/test_faults_chB.py`、`tests/test_packaging.py` | 跟着改 | §5、§8、§11、§13 |
| `.github/workflows/postmerge.yml` | 加三步 | §13 |
| `probe_retry.py`、`probe_mutations_ch12.py` | 新增 | §14 |

### 15.2 推送、PR

```bash
git push -u origin feat/retry
```

PR 描述里要如实写的：

> - 一次请求的超时（120 秒）管的是**沉默**，不是总时长；一个一直在说话的慢服务器，对最上层的运行没有时间上限（§10.2）。
> - 对着真的 429，默认策略只够重试**一次**（服务器要等 45 秒，预算 90 秒）。
> - 幂等键在这个接口上不起作用（量过）；重试的代价最多是重复付费，不会重复执行工具。
> - 总结用的那次模型调用**不重试**：它失败了走第 6 章的降级，除非是没救的那种。
> - 压缩后仍然太长就放弃，不循环着一条一条删（codex 会）。
> - 500 的那段失败是编的，不是录的。
> - 不认识的失败一律当作没救，这个方向是按"只面对两个服务商"选的。

### 15.3 自己审一遍

**1 · 为什么不做一个通用的 `with_retries(fn, policy)`？**
因为只有一个调用的地方，而且这个地方的三种处置里有一种（`shrink`）要改历史——一个通用的重试包装不知道"历史"是什么。把它做成通用的，就得再造一个"失败后做点什么"的钩子，最后比现在的四十行长。

**2 · 不认识的失败当作没救，会不会太保守？**
会在一种情形下吃亏：某个代理返回一个我们没见过的状态码，其实等一下就好。代价是这一次运行结束，屏幕上有原因。反过来的错，代价是额度，而且没有声音。codex 选的是另一边（§16），理由是它面对的服务商多得多。

**3 · 预算 90 秒是怎么来的？**
倒推的：子任务 300 秒，一次请求最多沉默 120 秒，剩下的里面留出余量。它不是量出来的最优值；有一个测试守着这几个数的大小关系，而不是守着 90 这个数本身。

**4 · `_shrink` 直接被测试调用了（§9.2），这算不算测了实现细节？**
算。能从外面造出那个条件的话，从外面测更好；造不出来（假服务器回的是固定的录音，没法让它"说的数正好是这次请求的两倍"），就只能进去。这个测试因此比别的更容易在重构时坏掉——记下来。

**5 · 子 Agent 的 `error` 结局告诉父 Agent"不要再委派"。如果只是暂时的限流呢？**
走到 `error` 的时候，子 Agent 自己已经按策略重试过、预算已经用完了。父 Agent 再派一个新的，只是把同一个限额再撞一遍。

---

## §16 codex 是怎么做的

- **"能不能重试"在它那里是一张穷举的表**：三十多种失败，一种一种回答"是"或"否"，**没有默认分支**——编译器会强制这张表保持完整。
  这是"状态码分不了类"这句话最强的版本：它干脆不从 HTTP 层推断，而是给每一种**语义上**的失败单独回答。
- **有两处和这一章选的相反**：不认识的状态码，它**重试**（它面对几十个服务商和各种代理，不认识的状态码多半是中间某一跳的抖动；这一章面对两个服务商、量过五种失败，所以选了另一边——
  **默认往哪边偏，取决于你对"不认识的"到底知道多少**）；"服务器过载"它**不重试**，直接告诉用户。
- **该等多久，它从正文里读**（"try again in 45.175s"那句话），这一章从头里读。两条路都通，而且**都只在 `code` 是限流时才认**——连它也是按 `code` 分派的。
- **退避**：从 200 毫秒起步（这一章是 1 秒），抖动是 0.9 到 1.1 倍（这一章是 0.5 到 1，因为有子 Agent 一起醒的问题）。
  **"流断了"和"请求没通"是两个不同的重试次数**（5 和 4）；用户能配这些数，但配置有一个硬上限——一个能配的数字，是一个会被人配成一万的数字。
- **它没有"总预算"，只有次数。** 兜底的是一个 300 秒的**空闲**超时（名字里就写着"空闲"：它量的是沉默，§10.2）和上层的取消。
- **压缩后还是太长**：它删掉最老的一条、把重试次数清零、再来，直到只剩一条。这一章压一次就放弃。差别在成本：它那个循环每一轮都可能再调一次总结用的模型。

---

## §17 回头看：这一章撞到了什么

**预测到了，并且成立的：** F12-01、F12-02、F12-05、F12-06、F12-07、F12-08（各有转折）。
**按清单的说法不会发生的：** F12-03、F12-09（同一个原因：重试套的位置）。
**清单开的药量出来没用的：** F12-04。

**没预测到的：**

| 故障 | 怎么发现的 | 挡住它的东西 |
|---|---|---|
| 被拒绝的超长请求照样扣额度 | 🟠 看回答的头 | 没救的请求只发一次 |
| 拿服务器说的 token 数去校准：比例 250 倍，12 轮压缩 11 次 | 🔵 跑起来看 | 修正幅度封顶 4 倍 |
| 预算只数睡觉的时间，管不住不说话的服务器 | 🟠 探针两种配置结果一样 | 预算改成整轮的挂钟时间 |
| 崩溃留下会话锁 | 🟠 | `finally` |
| 客户端唯一的出错路径，十一章没人走过 | 🟣 搜了一下 | 在边界上解析；有测试 |
| 上一章的变异脚本没进 CI；第 5 章的那份也没有 | 🟣 | 接上；一个断言 |
| `wait_for` 里的死代码 | ⚪ 变异 | 删掉 |
| 一条让测试跑不完的变异 | ⚪ | "没跑完"算没抓到；测试的循环加上界 |
| 守着降级分支的测试走不到那一行 | ⚪ 变异 | 补一个走得到的 |
| 共用的假服务器让测试每次红在不同地方 | 🔵 | 这个文件用自己的服务器 |
| **校准喂的是修正过的估算——第 6 章修过一次的错，又写了一遍** | 🟣 改写时带着第 6 章的教训回来看 | 用原始估算；一个从"已校准"开始的测试 |
| **"一次请求最多 120 秒"不成立：那个数管的是沉默** | 🟠 改写时去量了那句话 | 改说法；一个测试钉住真实的语义 |
| **探针里标着"现在"的一段量的是从前；说明里列着一段不存在的** | 🟠 改写时重跑探针 | 改探针 |
| **说等待的那一行、把策略往下传的那一行、`announce=print`：删掉都没有测试红** | ⚪ 改写时做的变异 | 三个测试 |
| **守着"预算是挂钟时间"的测试靠秒数判断，在快的机器上坏版本也能过** | ⚪ 改写时在 Linux 上重跑变异脚本 | 改成数尝试的次数 |
| 压缩的告知、`explain()` 的两种结局、失败的子 Agent 的原因和记录：删掉都没有测试红 | ⚪ 改写时做的变异 | 三个测试 |

标记：🔴 崩溃 · 🟡 静默 · 🟢 边界测试 · 🔵 长跑 · 🟠 看日志 · 🟣 审查 · ⚪ 工具

---

## 如果你只记住三件事

1. **先问"这是哪一种失败"，再问"重试几次"。**
   状态码分不出来：两个 400，一个要压小了再发，一个永远别再发。而重试一个不可能成功的请求不是白费力气——被拒绝的请求照样扣额度，它是把**下一个**请求推进限流的东西。

2. **重试套在哪一层，比怎么重试更要紧。**
   套在"失败了就什么都没留下"的那一层，清单上的两条故障直接不存在：没有半截的状态，没有历史里的残渣，没有工具跑两次。套错一层，这三件事都得一件一件去修。

3. **一个数字管着什么，去量；一个 bug 修掉了，去搜还有谁在做同一件事。**
   "超时 120 秒"管的是沉默，不是总时长。"校准要喂原始估算"在第 6 章修过，这一章又写错了一遍。两件事都是带着已经知道的东西回头看才发现的——代码不会因为你在别处学到了什么而自己变对。

---

## 动手练习

1. 把 `retry.py` 的 `_BY_CODE` 里 `"context_length_exceeded"` 那一行删掉，跑 `uv run pytest tests/test_faults_ch12.py`。它现在被归成了什么？哪些测试红了？改回去。
2. 把 `agent.py` 的 `_respond` 里 `except Exception as exc:` 改成 `except BaseException as exc:`，跑测试。红的是哪个？想一想：如果没有那个测试，这个改动在真实使用里会表现成什么？
3. 写一个测试：服务器先回一页 HTML（状态码 503），再正常回答。这次运行应该完成。（提示：`failing(...)` 的队列里可以放 `{"status": 503, "body": ...}`——看一下 `_serve_failure` 是怎么处理 `body` 的，HTML 要怎么放进去？）
4. 有 key 的话，跑 `uv run python probe_retry.py ratelimit`（先跑一次 `overlong`）。你拿到的 `retry-after-ms` 是多少？按 90 秒的预算，默认策略能重试几次？
5. §10.2 留下的那件事：最上层的运行对"一直在说话的慢服务器"没有时间上限。先别写代码，先回答：如果要加一个，它应该量什么——总时长、还是"多久没有产生新的一轮"？各自会误伤什么？

下一章回到一个从第 0 章就在、一直没有仔细看过的东西：系统提示词。它是每一个请求里最先发出去、也最少被改动的那一段。
