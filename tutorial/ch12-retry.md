# 第 12 章 · 重试与错误分类

> **代码**：`steps/step12_retry/`
> **分支**：`feat/retry`
> **产出**：一个只回答一个问题的模块——**这次失败是哪一种**——以及由此长出来的
> 三个处置：再发一次、先瘦身再发、别再发了
> **你需要**：本章 44 个测试全部离线。`probe_retry.py` 七节里三节离线，四节要真调
> API——但其中三节**一分钱都不花**，因为它们全都是被拒的请求

---

## §1 这一章要做出来的东西

前十一章有一个共同的假设，而且从来没有人把它写下来过：**请求总会有回应**。

这个假设在 `model.py` 里长这样：

```python
if resp.status_code != 200:
    detail = (await resp.aread()).decode("utf-8", "replace")[:1000]
    raise ModelHTTPError(f"HTTP {resp.status_code} from {url}: {detail}")
```

一行 `raise`，往上没有任何人接。会话结束，栈回溯二十行，磁盘上留着模型已经改过的
文件，没有任何一处写着为什么停了。

这一章要做的事只有一件：**在 `raise` 之前，先问一句这是哪一种失败**。答案有三个值，
因为要做的事有三种：

```
retry    同样的请求过一会儿可能成功
shrink   请求太大了，对话得先瘦下来
fatal    再发一次也不可能成功
```

清单给这一章列了 9 条故障。写完之后：

- **F12-01 的关键不在"要分类"，在于用什么分类**。实测五种真实失败：
  `context_length_exceeded` 和"你的工具名里有空格"**都是 HTTP 400，`type` 也一模一样
  （`invalid_request_error`）**，而它们是这张表的两个极端——一个必须压缩后重试，
  一个永远不能再发。**status 分不了，`type` 也分不了，`code` 可以。**
- **F12-02 实测到一个真的 429**，而且是免费拿到的：一个因为超长被拒的请求
  **照样扣掉了账号的 token 配额**（剩余 199,996 → 19,998），两个就够触发限流。
  它的头里写着 `retry-after: 46`、`retry-after-ms: 45175`，正文里还写了第三遍。
  而教科书式的 1-2-4-8-16 退避，五次尝试全部落在 31 秒以内——**一次都没等到窗口打开，
  而每一次又都在给那个已经耗尽的限额上再加一笔**。
- **F12-03 和 F12-09 一行代码都没写**，因为第 0 章的一个决定（`[DONE]` 之前什么都不提交）
  已经让它们不可能发生。这一章的价值是**说清楚为什么**，以及为什么重试的边界必须
  画在那个位置。
- **F12-04 清单开的药方实测无效**。`Idempotency-Key` 在 `/v1/chat/completions` 上
  什么都没做：同一个 key 发两次，两个不同的答案、两个 request id、扣两次。
  真正的界限在别处。
- **F12-05 是这一章最贵的一条**，而且贵在我自己写的一个"顺手的好主意"上：
  从错误正文里读出服务器说的真实 token 数、喂给第 6 章的校准器——然后**一次运行
  12 轮压缩了 11 次**，永远跑不完。
- **清单外多出 6 条**，其中一条是第 11 章亲手留下的：那一章的正文写着"这 19 条变异
  跑在 postmerge.yml 里"，而 `postmerge.yml` 里只有第 9 章和第 10 章的步骤。

先看现状。

---

## §2 现状：一个 429 就把一次会话打完了

`stub.py` 从第 0 章起就是一个能按录音回放的假服务器。这一章给它加了一个队列，
让它可以先失败几次再正常回答（后面 §3 会说这些失败长什么样、从哪来的）。

先让它在一次正常的两轮对话前面放一个 429：

```
$ uv run python probe_retry.py naive
```

```
--- (a) today, one 429 before a two-turn conversation
  ModelHTTPError: HTTP 429 from http://127.0.0.1:9479/v1/chat/completions: {"error":
  {"message": "Rate limit reached for gpt-4o-mini in or...
  requests the server saw: 1
```

**一个请求，会话结束。** 那次对话本来只要再等半分钟就能跑完。

而下面这段，是写这本书的过程中最难堪的一个发现。第 10 章和第 11 章的探针脚本里，
各有一份这个：

```python
# probe_subagent.py，第 10 章写的
async def _retry(make: Any, attempts: int = 4) -> Any:
    """Run one sample, surviving a dropped connection.

    Not a fix for anything -- chapter 12 is where retries are designed.  This
    is here because a probe that dies on request 40 of 60 wastes the first 39,
    and `httpx.ConnectError` / `RemoteProtocolError` both happened while these
    numbers were being collected.
    """
```

**三章的测量之所以能做完，是因为测量工具会重试；而被测量的程序不会。** 那句
"chapter 12 is where retries are designed" 是我自己写的注释，写的时候心安理得，
现在读起来像一张欠条。

还有一条，是准备写这一章时 grep 出来的：

```
$ grep -rn "ModelHTTPError" src tests
src/minicodex/model.py:91:class ModelHTTPError(RuntimeError):
src/minicodex/model.py:199:                    raise ModelHTTPError(f"HTTP {resp.status_code} ...")
```

两行，都在 `model.py` 里。**这个客户端唯一的错误路径，十一章下来没有一个测试、
没有一个 catch、没有一个调用者。** 它不是覆盖率不够——是从来没有人走过。

用户看到的东西是这样的（真的跑了一次，key 是假的）：

```
$ OPENAI_API_KEY=sk-not-a-real-key uv run minicodex ask "what is in this directory" --provider openai
```

```
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

二十行栈回溯，用来说一件一句话的事：**你的 key 不对。**

---

## §3 先去看失败长什么样

写分类之前得先知道要分什么。这本书的规矩是**实测，不读文档**（§7.5 第 10 条），
所以第一件事是去真的把每一种失败撞出来一次。

```
$ uv run python probe_retry.py shapes
```

```
--- bad api key
  status  401
  x-request-id: req_487c1f2f8e144a929e20357ee62b10f5
  body    {
  "error": {
    "message": "Incorrect API key provided: sk-not-a*****-key. ...",
    "type": "invalid_request_error",
    "code": "invalid_api_key",
    "param": null
  },
  "status": 401
}

--- unknown model
  status  404
  x-request-id: req_3acab07cd1ee49b28c4d75ea841969e3
  body    {
    "error": {
        "message": "The model `gpt-4o-mini-does-not-exist` does not exist or you do not have access to it.",
        "type": "invalid_request_error",
        "param": null,
        "code": "model_not_found"
    }
}

--- bad tool schema
  status  400
  x-ratelimit-limit-requests: 10000
  x-ratelimit-remaining-requests: 9999
  x-ratelimit-limit-tokens: 200000
  x-ratelimit-remaining-tokens: 199996
  x-request-id: req_f7b214036e9d49e4a88b3911f4a39596
  openai-processing-ms: 18
  body    {
  "error": {
    "message": "Invalid 'tools[0].function.name': string does not match pattern. Expected a string that matches the pattern '^[a-zA-Z0-9_-]+$'.",
    "type": "invalid_request_error",
    "param": "tools[0].function.name",
    "code": "invalid_value"
  }
}
```

再加一个"消息太长"（下一节要用）：

```
$ uv run python probe_retry.py overlong
```

```
--- overlong (720000 chars)
  status  400
  x-ratelimit-remaining-tokens: 19998
  x-ratelimit-reset-tokens: 54s
  x-request-id: req_a126cb97e99049ce93e6630dc36df8f0
  body    {
  "error": {
    "message": "This model's maximum context length is 128000 tokens. However, your
                messages resulted in 160008 tokens. Please reduce the length of the messages.",
    "type": "invalid_request_error",
    "param": "messages",
    "code": "context_length_exceeded"
  }
}
```

四种失败摆在一起，能读出三件事：

| status | `type` | `code` | 该怎么办 |
|---|---|---|---|
| 401 | `invalid_request_error` | `invalid_api_key` | 别再发了，去换 key |
| 404 | `invalid_request_error` | `model_not_found` | 别再发了，去改 `--model` |
| 400 | `invalid_request_error` | `invalid_value` | 别再发了，schema 是错的 |
| 400 | `invalid_request_error` | `context_length_exceeded` | **压缩之后再发** |

1. **最后两行是同一个 status、同一个 `type`，而处置完全相反。** 一个是"这个请求
   永远不会被接受"，另一个是"这个请求太大了，改小就行"。**任何读 status 的分类器
   在这两行上必错一半。**
2. **每一条都带 `code`。** 这是唯一一个机器可读、且真的区分得开的字段。
3. **401 和 404 没有任何 `x-ratelimit-*` 头，400 有。** 认证发生在限流之前——
   这条不影响分类，但它说明这些头不是"总会有"的东西，读它们的代码必须能接受没有。

还有一个字段值得单独说：**`x-request-id` 每一条都有**，成功的也有。它是整个失败里
唯一一个能让别人去查"到底发生了什么"的字符串，而十一章以来它跟其他所有头一起被丢掉了。

### 3.1 免费拿到一个真的 429

429 不好撞——你得真的把限额打满。但上面那次超长请求的头里藏着一条路：

```
x-ratelimit-remaining-tokens: 19998      （之前是 199996）
```

**这个请求被拒绝了，token 桶照样扣了 16 万。** 那么第二个同样的请求就该撞上限流，
而且两个请求都因为超长被拒，所以**一分钱不花**：

```
$ uv run python probe_retry.py ratelimit
```

```
--- overlong attempt 1
  status  400   ... code=context_length_exceeded
  x-ratelimit-remaining-tokens: 19998

--- overlong attempt 2
  status  429
  retry-after: 46
  retry-after-ms: 45175
  x-ratelimit-remaining-tokens: 29417
  x-ratelimit-reset-tokens: 51.174s
  x-request-id: req_c07776230e2e415c9d69be995edfaf71
  body    {
    "error": {
        "message": "Rate limit reached for gpt-4o-mini in organization org-XXXX on tokens
                    per min (TPM): Limit 200000, Used 170583, Requested 180002.
                    Please try again in 45.175s. ...",
        "type": "tokens",
        "param": null,
        "code": "rate_limit_exceeded"
    }
}
```

三件事：

- **服务器把该等多久说了三遍**：`retry-after: 46`（整秒，向上取整）、
  `retry-after-ms: 45175`（毫秒）、正文里的 `Please try again in 45.175s`。
- **`type` 是 `tokens`**，不是 `rate_limit_error`。第五种失败，第五个 `type` 写法。
  这一条本身就足以说明为什么不能用 `type` 分类。
- 而这条 429 是**被一个不可能成功的请求造出来的**。记住这个因果，它是下一节的全部论点。

这五个响应体一字不改地存进了 `stub.py`，本章所有测试都跑在它们上面：

```python
RATE_LIMITED = {
    "status": 429,
    "headers": {"retry-after": "46", "retry-after-ms": "45175"},
    "body": {"error": {"message": "Rate limit reached for gpt-4o-mini ...",
                       "type": "tokens", "code": "rate_limit_exceeded"}},
}
```

唯一一个不是录音的是 500——**500 是求不来的**，所以它在源码里明写着是编的：

```python
# No recording exists for this one and there will not be one: a 500 cannot be
# asked for.  The shape is the provider's documented envelope with the fields
# an outage actually leaves empty.
SERVER_ERROR = {...}
```

---

## §4 F12-01：无脑重试的代价，量出来

清单说"400 也重试，永远不会成功还烧额度"。这句话对，但"烧额度"是个抽象词，
所以先把它变成数字。

最自然的修法是把调用套进 `for attempt in range(5)`，什么错都重试。拿一个
**永远不会被接受**的请求（工具名里有空格）跑一次：

```
--- (b) retry-everything, against a request that can never succeed
  requests the server saw: 5
  seconds spent: 9.1
  the body was byte-identical every time, and so was the answer
```

五个一模一样的请求，九秒钟，五个一模一样的回答。这还只是账面上的损失。真正的账在
§3.1 那条头里：**一个被拒的请求照样扣配额**。也就是说——

> 重试一个不可能成功的请求，不只是白费；**它是把下一个本来能成功的请求推进 429 的那只手**。

这就是为什么这一章的分类必须先答"能不能"，再答"等多久"。

### 4.1 三个处置，不是两个

清单写的是"可重试 / 不可重试"。写到 §3 那张表的时候，第三种自己冒出来了：
`context_length_exceeded` 既不是"再发一次就行"，也不是"没救了"——它是
**"把请求改小再发"**。

```python
Disposition = Literal["retry", "shrink", "fatal"]


@dataclass(frozen=True)
class Failure:
    disposition: Disposition
    kind: str
    detail: str
    retry_after: float | None = None
    request_id: str | None = None
    stated_tokens: tuple[int, int] | None = None
```

字段的理由，一个一个说：

- **`disposition`** 是循环唯一要看的东西。三个值，因为要做的事有三种。
- **`kind`** 是我们自己的词（`rate_limit` / `auth` / `context_length`……），
  不是供应商的 `code` 原文。理由：循环和测试需要一个**跨供应商含义相同**的词，
  而供应商的 `code` 属于那个要去查文档的人——它留在 `detail` 里。
- **`retry_after`** 是秒，来自服务器，或者是 `None`。它**不是**"我们的退避，但至少这么久"，
  §5 会说为什么。
- **`request_id`** 是 §3 里那个唯一能对外查的字符串。
- **`stated_tokens`** 只有一种失败会填，§8 讲。

### 4.2 解析放在边界上，不放在接住它的人那里

`classify()` 要读 `code`，就得先有人把 JSON 解出来。放哪儿？

第 1 章已经回答过一次这个问题：**信任边界上的东西只解析一次**。`ModelHTTPError`
是这个边界的产物，所以解析写在它的构造函数里：

```python
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
    self.request_id = self.headers.get("x-request-id")
    super().__init__(f"HTTP {status} from {url}: {self.message or body[:500]}")
```

两处防御，都不是想象出来的：

- **`body` 解析之后还整个留着。** 一个代理或者网关根本不返回这个信封，它返回 HTML。
  假设是 JSON 的解析器会把**别人家的故障**变成我们自己错误处理里的 `JSONDecodeError`。
  这条有测试：一个 `<html>...502 Bad Gateway...` 的 body，`code is None`，
  `classify` 仍然给出 `retry`。
- **`except ValueError`** 而不是 `except json.JSONDecodeError`——后者是前者的子类，
  但写父类的那一版能同时接住 `json.loads(b"...")` 之类的意外。

### 4.3 分类表

```python
_BY_CODE: dict[str, tuple[Disposition, str]] = {
    "rate_limit_exceeded": ("retry", "rate_limit"),
    "context_length_exceeded": ("shrink", "context_length"),
    "invalid_api_key": ("fatal", "auth"),
    "model_not_found": ("fatal", "unknown_model"),
    "insufficient_quota": ("fatal", "quota"),
    "server_error": ("retry", "server_error"),
}
```

`code` 不认识的时候才落到 status 上：429 重试，5xx 和 408 重试，**其余 4xx 一律 fatal**。

非 HTTP 的失败：

```python
def classify(exc: BaseException) -> Failure:
    if isinstance(exc, ModelHTTPError):
        return _http(exc)
    if isinstance(exc, IncompleteStreamError):
        return Failure("retry", "incomplete_stream", str(exc))
    if isinstance(exc, _OUR_FAULT):
        return Failure("fatal", "client_bug", f"{type(exc).__name__}: {exc}")
    if isinstance(exc, httpx.TransportError):
        return Failure("retry", "transport", f"{type(exc).__name__}: {exc}")
    return Failure("fatal", "unexpected", f"{type(exc).__name__}: {exc}")
```

`_OUR_FAULT` 那一行是这段代码里唯一一处需要解释的：

```python
_OUR_FAULT = (httpx.LocalProtocolError, httpx.UnsupportedProtocol)
```

`httpx.TransportError` 底下既有"天气"（超时、连接断、服务器半路挂电话），
也有**我们自己的 bug**：`LocalProtocolError` 是请求本身拼错了，
`UnsupportedProtocol` 是 URL 没有 scheme。重试这两个只会把同一个错误发生得更慢一点。

### 4.4 不认识的东西，默认是 fatal——和第 5 章反着来

第 5 章遇到不认识的 shell 语法时的规则是**"不认识就问人"**。这里的默认是
**"不认识就停"**。同样是"未知"，答案相反，理由要说清楚，否则下一章就会有人照抄错的那条：

1. **第 5 章有一个人可以问，重试循环里没有。** 一个"不认识就重试"的默认，
   等价于把决定权交给了下一次网络抖动。
2. **两个方向的错代价不对称。** 把可重试的判成 fatal：这次运行结束，并且**屏幕上写着为什么**。
   把 fatal 的判成可重试：**安静地烧配额**，而且按 §4 那条因果，
   烧掉的是下一次请求要用的那份。

这段论证直接写进了 `classify` 的 docstring，因为它是这个函数唯一一个"看起来太保守"的决定。

---

## §5 F12-02：服务器已经把答案写在头里了

`retry-after: 46`。我们自己的退避会等多久？

```
--- (c) exponential backoff vs the number the server sent
  attempt 1: slept   1.0s, elapsed   1.0s
  attempt 2: slept   2.0s, elapsed   3.0s
  attempt 3: slept   4.0s, elapsed   7.0s
  attempt 4: slept   8.0s, elapsed  15.0s
  attempt 5: slept  16.0s, elapsed  31.0s
  the header said: retry-after 46 (retry-after-ms 45175)
  five attempts fit inside 31s; the window had not opened at any of them
```

**五次尝试，五次都在窗口打开之前，五次都在给限额再加一笔。** 教科书上的指数退避，
在一个已经把答案告诉你的服务器面前，是纯粹的伤害。

```python
def _retry_after(headers: dict[str, str]) -> float | None:
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

三个决定：

- **毫秒的那个优先。** 不是为了那 0.8 秒：整秒的 `46` 是 `45.175` **向上取整**来的，
  一个已经被别人四舍五入过的数字，是一个别人已经宣布它不精确的数字。
- **RFC 9110 允许这里放一个 HTTP 日期。** 不解析。理由是 §7.5 第 7 条——
  这本书测过的供应商没有一个发过它。读不懂的头返回 `None`，
  然后走本地退避，**和"根本没有这个头"落到同一个分支**。
- **`continue` 而不是 `raise`。** 变异测试里有一条专门验这个：把 `continue` 改成
  `raise`，一个测试变红。

### 5.1 等不起就不等——而不是少等一会儿

```python
if failure.retry_after is not None:
    wait = failure.retry_after
else:
    wait = min(policy.cap, policy.base * 2**attempt)
    if jitter:
        wait *= random.uniform(0.5, 1.0)

if elapsed + wait > policy.budget:
    return None
```

关键是最后那三行的**语义**：预算不够时返回 `None`（也就是"放弃"），
而不是"那就等预算允许的那么久"。

> 服务器说要 45 秒，你只剩 30 秒——**等 30 秒然后照发不误，就是让停机时间变长的那个动作。**
> 那一发必然又是 429，又扣一次配额，又把重置时间往后推。

拿真实数字跑一遍，得到一个我没预料到的结论：

```python
def test_F12_02_only_one_retry_of_a_real_rate_limit_fits_the_budget() -> None:
    failure = classify(http(stub.RATE_LIMITED))
    waited, attempts = 0.0, 1
    while (wait := wait_for(failure, attempts - 1, elapsed=waited)) is not None:
        waited += wait
        attempts += 1
    assert attempts == 2
    assert waited == pytest.approx(45.175)
```

**默认策略写着 4 次尝试，对上真实的 429 只有 2 次**：45.175 秒能塞进 90 秒的预算，
两次（90.35 秒）不能。"四次尝试"从来就不是对行为的描述——**预算说了算，
而预算由服务器决定**。

抖动是半到全（`uniform(0.5, 1.0)`），不是没有：这个程序可以有一个父 Agent 和几个
子 Agent 共用一个账号、共用一个时钟，同一个限额把它们同时打回来，
然后它们会同时醒。`jitter=False` 是给测试用的，好让断言能写具体的时刻表。

---

## §6 重试该套在哪一层——F12-03 和 F12-09 是免费的

清单里 F12-03（"流中途断，部分 tool_call 状态不明"）和 F12-09（"重试成功了但历史里
留了失败的痕迹"）都是**没有写一行代码就成立**的。原因是同一个，而且它决定了重试循环
放在哪里。

`model.stream()` 是一个异步生成器。连接断掉的时候，它**已经 yield 过一串
`TextDelta` 了**，而 yield 出去的东西是收不回来的。所以重试**不能**放在它里面。

再往上一层是 `_collect()`，第 0 章的 F00-04 给它定的规矩是：

> 在 `[DONE]` 到达之前什么都不返回。

于是"流 + 组装"这个组合有一个别处没有的性质：**一次失败的尝试什么都没产生**。
没有半个 turn，没有孤儿 tool_call，历史根本没被碰过。这正是让第二次尝试成为
**重发**而不是**重复**的那个性质。

所以重试循环的位置不是审美问题：

```python
async def _respond(
    self, history: History, turn_index: int
) -> tuple[ModelTurn, History, int, CompactionResult | None]:
    began = time.monotonic()
    shrunk = False
    forced: CompactionResult | None = None

    for attempt in range(self.retry_policy.attempts):
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
            if turn.usage is not None:
                self.calibration.observe(estimated=estimated, actual=turn.usage.prompt_tokens)
            return turn, history, estimated, forced
        ...
```

`except Exception` 而不是 `except BaseException`，是第 7 章 F07-04 那条教训的另一面：
`CancelledError` 自 3.8 起是 `BaseException`，在那一章它**逃出了**唯一一个该接住它的函数；
在这里如果接住了它，一次 Ctrl-C 会被当成失败**重试**。两个方向的错，同一个知识点。

历史干净到什么程度？直接断言两次运行的请求体完全一致：

```python
async def test_F12_09_retries_leave_no_trace_in_the_history(...):
    clean = await run()
    bumpy = await run(stub.SERVER_ERROR, BARE_429, {"stream_cut": 3})

    assert bumpy.history.to_wire("chat_completions") == clean.history.to_wire("chat_completions")
```

一次经历了三种失败（500、429、流被砍断）的对话，和一次一帆风顺的对话，
**渲染出来的请求体一个字节都不差**。

这不是整洁的问题。历史是**下一个请求的原材料**，历史里的残渣不是脏，
是一段模型会读到的话。

但反过来，**录制里必须看得见**：

```python
async def test_F12_09_but_the_transcript_does_show_them(...):
    failures = [e for e in events if e["kind"] == "model_failure"]
    assert len(failures) == 1
```

两个读者，两种需求：模型不该读到重试，凌晨四点排查的人必须能读到。
第 -1 章那份 JSONL 录制（F-1-04）在这里第二次派上用场。

---

## §7 F12-04：幂等键实测无效

清单给 F12-04 开的药方是"重试要带幂等标识"。OpenAI 确实支持 `Idempotency-Key`，
所以先测：同一个 key、同一个 body，发两次，`temperature: 2.0` 好让答案容易不一样。

```
$ uv run python probe_retry.py idempotency
```

```
--- idempotency attempt 1
  x-request-id: req_390982be146f4a729675606f62ab54ff
  body    answer='Nymbria.'

--- idempotency attempt 2
  x-request-id: req_ea5ac0006c0b4bb4ba058b5500b1bf2a
  body    answer='Falnitz.'
```

**两个不同的答案，两个 request id，没有 `idempotent-replayed` 头，配额扣了两次。**
在这个端点上它什么都没做。

清单开错药方不是新鲜事（第 9 章的两级加载是负收益），要做的是回去问：
**这条故障真正的形状是什么？**

它有两半，而且这本书的代码里，两半的答案都已经在了：

1. **模型调用可以重发，代价是钱不是正确性。** 一次重试就是把同一份 body 再发一次
   （测试直接断言 `REQUESTS[0]["messages"] == REQUESTS[1]["messages"]`），
   服务器那边不会因此多做一件有副作用的事，只会多算一次钱。
2. **有副作用的东西一次也不许重发。** 而它们全都在边界的另一侧：
   `_respond` 返回之后，turn 才被提交，工具才会跑。**失败只能打断模型调用。**
   这条有测试：三次 500 之后成功的一轮里，工具只跑了一次。

第 9 章其实已经替这一章做过一次同样的决定：MCP server 中途死掉时，
**在途的那次调用不重发**——因为它的副作用发生没发生是未知的，
而"未知"不能靠再做一次来解决。

所以 F12-04 的最终形态不是"加一个 header"，是一条关于**边界位置**的性质。
测量的价值在于：如果我没测，我会加上那个 header，然后以为问题解决了。

---

## §8 F12-05：400 里唯一一条要"改自己"的

`code=context_length_exceeded` 是所有失败里最特别的一个：**它是服务器在告诉你一件
关于你自己的事实**。第 6 章的压缩是按**本地估算**触发的，而这条 400 说的是估算错了。

```python
if failure.disposition == "shrink":
    history, forced = await self._shrink(history, failure, estimated, shrunk=shrunk)
    shrunk = True
    continue
```

`_shrink` 做四件事，每一件都有一条理由：

**（一）强制压缩，而不是"再判断一次要不要压缩"。** `_maybe_compact` 会去看那个
**刚刚被证明是错的**估算，然后得出"没什么要做的"。

**（二）一轮只压一次。**

```python
if shrunk:
    raise ModelFailed(replace(failure, detail=f"{failure.detail} (still too long after compacting once)"))
```

压完还是太长，说明**摘要本身就不够小**，再压一次是一个带账单的死循环。

**（三）压不动就直接失败。**

```python
if result.plan.saving <= 0:
    raise ModelFailed(replace(failure, detail=f"{failure.detail} (nothing left to compact)"))
```

这一条是被一个失败的测试逼出来的。第一版没有它，于是一个**只有一条消息**的对话
被服务器判为超长时，`compact()` 什么都没剪，返回一个"压缩成功"的结果，
然后把**一模一样的请求**又发了一次——F12-01，前面加了一道压缩仪式。

真正的情况是 F06-09：**单独一条消息就超过了整个窗口**，压缩帮不上忙。

**（四）没有压缩能力时，说清楚人该干什么。**

```python
if self.context_window is None or self.summariser is None:
    raise ModelFailed(failure)
```

配上 `explain()` 里那条：`the conversation no longer fits. Pass --context-window so
compaction can run, or start a new session.`

### 8.1 我顺手写的那个好主意，跑出来是灾难

错误正文里写着 `your messages resulted in 160008 tokens`。这是**唯一一处**服务器
告诉你一个**被拒绝的请求**的真实大小——被拒的请求没有 usage 分片，
第 6 章的校准器对这次请求本来一个观测都拿不到。

于是我把它解析出来喂了进去：

```python
_TOKENS = re.compile(r"maximum context length is (\d+) tokens.*?resulted in (\d+) tokens", re.S)
```

```python
if failure.stated_tokens is not None:
    _limit, actual = failure.stated_tokens
    self.calibration.observe(estimated=estimated, actual=actual)   # ← 第一版
```

看起来无懈可击。跑一次：

```
completed?  no
stop_reason turn_limit   turns 12
summaries   11
compactions 12
requests    13
```

**一次运行，12 轮，压缩了 11 次，永远跑不完。**

原因在第 6 章的 `Calibration.observe` 里，一行：

```python
self._ratio = actual / estimated
```

**它保留的是最后一次观测，没有平滑。** 那次被拒的请求在测试里只有几百个 token，
而错误正文说的是 160,008——比值 **x250**。从那一刻起，之后每一轮的估算都是真实值的
250 倍，于是每一轮都超窗口，于是每一轮都压缩、都调一次摘要模型、
然后估算**还是**超窗口。

这里有一个诚实的边界要交代：**这个比值之所以荒唐，是因为假服务器返回的错误
和真实发出的请求对不上**。真实场景里估算 12 万、服务器说 16 万，比值 1.33，无害。

但这次事故暴露的东西是真的：**这个观测和 usage 分片来的观测不是一回事**，
而 `observe` 对它们一视同仁。中间只要有一个代理、一个不同的模型、一个撒谎的 body，
一次观测就能永久污染这次会话的尺子。

修法是一个钳位，而且它的数字来自第 6 章自己的测量：

```python
MAX_REFUSAL_CORRECTION = 4.0
```

```python
correction = actual / estimated if estimated > 0 else 0.0
if 0 < correction <= MAX_REFUSAL_CORRECTION:
    self.calibration.observe(estimated=estimated, actual=actual)
else:
    self.recorder.record(
        "calibration_rejected",
        {"estimated": estimated, "stated": actual, "correction": correction},
    )
```

第 6 章量过这个程序的原始估算器：英文散文 1.04x、JSON 0.44x、中日韩 0.43x、
shell 输出 0.49x——**跨度 2.4 倍**。所以一次要求 4 倍以上修正的观测，
不是"这次会话的内容成分变了"这种新信息，是别的什么东西。

那个数字没有被丢掉，它进了录制。**给不了尺子，就给看尺子的人。**

### 8.2 压缩自己失败的时候

第 6 章的 F06-08 给"摘要调用失败"写了一条降级路径：拼一份确定性的硬摘要，
标上 `degraded`，继续跑。这一章要把它**收窄**：

```python
except Exception as exc:
    if isinstance(exc, ModelHTTPError):
        failure = classify(exc)
        if failure.disposition == "fatal":
            raise ModelFailed(failure) from exc
    summary = _hard_summary(dropped, f"{type(exc).__name__}: {exc}")
    degraded = True
```

理由：降级对一次断线是对的，对一个 401 是错的。**下一个请求会因为同样的原因失败，
所以先降级等于在一次注定要结束的运行路上顺手把 transcript 毁掉**，
而且用户看到的原因还隔了一层。

`isinstance(exc, ModelHTTPError)` 那一层收窄是刻意的，也写在注释里：
`classify` 会把它不认识的一切判成 fatal，而"它不认识的一切"里包括
**传进来的那个 `summarise` 自己的 bug**——那正是 F06-08 当初写这条分支的场景。

这个收窄有两个测试守着，一个正一个反。**而反的那个是变异测试逼出来的**：
最初只有一个"连接断了要降级"的测试，它抛的是 `httpx.ConnectError`，
根本走不到 `classify` 那一行——于是"只有致命的才上浮"这句话**没有任何东西在验**。
补的那个测试让摘要器抛一个 500。

---

## §9 F12-06：四个时钟，其中两个设成了同一个数

清单说"工具超时与 turn 超时层级不清，互相打架"。把这个程序里所有的时钟列出来：

| 时钟 | 数字 | 写在哪一章 |
|---|---|---|
| 一条 shell 命令 | 30s | 第 2 章 |
| 一次 MCP 工具调用 | 60s | 第 9 章 |
| 一次模型请求 | **300s** | 第 0 章 |
| 一个子任务的全部 | **300s** | 第 10 章 |

**最后两个是同一个数字。** 一个子 Agent 的模型请求卡住时，它自己的截止时间和
请求的截止时间在同一瞬间到期，谁先响决定了父 Agent 看到的是一个 `timeout` 结局
还是一个异常。

把这两个数按比例缩小到秒级，各跑 10 次：

```
$ uv run python probe_retry.py nesting
```

```
--- equal    attempt 1.0 + budget 0.9 vs task 1.0  (300/300, as shipped)
  outcomes over 10 trials: {'timeout': 10}

--- nested   attempt 0.4 + budget 0.3 vs task 1.0  (120/90/300, now)
  outcomes over 10 trials: {'error': 10}
```

同一个故障（服务器不回话），两种结局。**只有第二种能说出发生了什么**——
第一种里，父 Agent 的秒表先响，它只知道"孩子没在规定时间内回来"。

于是模型请求的超时从 300 降到 120，并且这条顺序**写成断言**而不是注释：

```python
def test_F12_06_the_timeouts_are_strictly_nested() -> None:
    assert SHELL_TIMEOUT < DEFAULT_TOOL_TIMEOUT
    assert DEFAULT_TOOL_TIMEOUT < DEFAULT_ATTEMPT_TIMEOUT
    assert DEFAULT_ATTEMPT_TIMEOUT + DEFAULT_POLICY.budget < DEFAULT_TASK_TIMEOUT
```

四个数字住在四个文件里，改其中一个的人不会去读另外三个。

### 9.1 而这条断言一开始是假的

上面那条断言写出来的时候，`budget` 的含义是**"睡了多久"**。而一个挂住的服务器
**一秒都不睡**：四次尝试各超时 120 秒，就是八分钟，`budget` 一次都没被查过。

第一次跑 `nesting` 的结果是两条臂都 `{'timeout': 10}`——**修改前后没有区别**，
因为在起作用的一直是孩子自己的截止时间，重试预算是个摆设。

所以 `budget` 改成**整轮的墙上时钟**：

```python
began = time.monotonic()
...
elapsed = time.monotonic() - began
wait = wait_for(failure, attempt, self.retry_policy, elapsed=elapsed)
```

改完之后，这个程序能说出口的保证只有一句，但它是真的：

> **`budget` 之后不再开始新的尝试，一次尝试最多 `DEFAULT_ATTEMPT_TIMEOUT`，
> 所以一轮最多 90 + 120 = 210 秒，装得进子任务的 300 秒。**

这句话就是那条断言的内容。它不精确（最后一次尝试可以在预算刚好用完前一刻开始），
但它是一个**能算出来的上界**，而不是一个愿望。

---

## §10 F12-07：退避里的 Ctrl-C

清单说"重试等待期间用户按 Ctrl-C 没反应"。这条不需要信号也能测——
"Ctrl-C 能不能落地"和"这段时间里**还有没有别的事情能发生**"是同一个问题，
而后者用一个每 50 毫秒跳一次的心跳任务就能量：

```
$ uv run python probe_retry.py interrupt
```

```
--- asyncio.sleep(1.0)
  the cancel could be issued 0.21s in (it was asked for at 0.20s)
  and landed     0.1 ms after that
  longest gap between heartbeats    62.8 ms (3 beats)
  total 0.27s

--- time.sleep(1.0)
  the cancel could be issued 1.00s in (it was asked for at 0.20s)
  and landed     0.0 ms after that
  longest gap between heartbeats  1000.5 ms (1 beats)
  total 1.07s
```

阻塞版本里，**取消这个动作本身在一秒钟之内根本发不出去**，因为发它的那段代码
也在同一个事件循环上。心跳停了整整一秒。

而这一秒里停掉的不只是取消。这个程序的事件循环上跑着：并发的工具调用（第 8 章）、
MCP 的 stderr 抽水任务（第 9 章）、子 Agent（第 10 章）。**退避里的这个 bug 会比
它要修的那个 bug 更大。**

值得注意的是，这一条**静态检查就抓得到**：

```
ASYNC251 Async functions should not call `time.sleep`
   --> probe_retry.py:348:17
```

`ruff` 的 ASYNC 规则组从第 -1 章就开着（第 0 章的 F00-09 就是被它抓下来的）。
探针里那一行是**故意**留着的，加了单行豁免，好把代价量出来而不是断言出来。

### 10.1 一个不出声的等待，和卡死没有区别

真实的数字是 46 秒。46 秒里屏幕上什么都没有，用户会按 Ctrl-C——
**而那正好是我们刚刚花力气让它能中断的那个动作**。

```python
self._say(
    f"[{failure.kind}: waiting {wait:.0f}s, attempt "
    f"{attempt + 2} of {self.retry_policy.attempts}]"
)
```

`_say` 需要一个能说话的地方，而 `Agent` 没有。加在哪儿是一个真正的决定，§12 讲。

---

## §11 F12-08：两个读者

清单写的是"错误原文 `HTTP 500` 塞给模型，模型不知道该干嘛"。第 3 章的 F03-07
已经处理过这件事的工具版（"错误信息即 prompt"）。但在这个程序里，
**模型根本看不到模型调用的错误**——看到的是人。

所以这一条要分成两半，两个读者，两种"下一步该干什么"。

### 11.1 人这一半

```python
def explain(failure: Failure, *, waited: float = 0.0, attempts: int = 1) -> str:
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
    ...
}
```

同一条命令，改完之后：

```
$ OPENAI_API_KEY=sk-not-a-real-key uv run minicodex ask "what is in this directory" --provider openai

the model call failed: Incorrect API key provided: sk-not-a*****-key. You can find your
API key at https://platform.openai.com/account/api-keys. [invalid_api_key]
check OPENAI_API_KEY, or pass --provider ollama to run against a local model.
provider request id: req_f07918595554434c8f429fdad1bb1547
```

三行，退出码 1。第一行是供应商写的，**第二行是唯一属于我们的那一行**，
第三行是别人来查这件事时唯一有用的字符串。

`ModelFailed.__str__` 直接就是这段解释，所以**最省事的调用方也能打印出有用的东西**——
这不是礼貌：十一章以来最省事的调用方就是所有调用方，而它打印的是栈回溯。

### 11.2 模型那一半：一扇一直开着的门

`run_task` 的 docstring 从第 10 章起就写着：

> Never raises for anything the sub-agent did.

这句话是假的，而且假在唯一一件子 Agent 完全无法控制的事情上。子 Agent 的模型调用
失败时，异常从 `child.run()` 出来、穿过 `run_task`、掉进 `_run_tool` 那个宽泛的
`except Exception`，然后变成一条**交给父模型去读**的字符串：

```
Error: spawn_agent raised ModelHTTPError: HTTP 429 from https://api.openai.com/v1/chat/completions: {"error": {"message": "Rate limit reached for gpt-4o-mini in organization org-...
```

**F12-08 清单上写的那个形状，在这个程序里真实存在，而且这是它唯一的入口。**
一段为终端写的话，被交给了一个会读它、并据此决定下一步的东西。

修法是给 `TaskResult` 加第八个 outcome：

```python
"error": "could not run: {detail}",
```

```python
"error": (
    "This is a fault in the tooling, not in the task. Do not delegate it again; "
    "do the work yourself or tell the user what stopped you."
),
```

第 10 章那套"每个结局都要说下一步该干什么"的表，第八行。渲染时它是特殊的：

```python
if self.outcome == "error":
    # No "what it had said before that point": there is no before.
    return f"{header}\n{advice}"
```

其余七种结局会把孩子说到一半的话附上（"这不是结论"），
而一次连 turn 都没产生的失败**没有"之前"**，硬套那个模板就是给一段空引文加个标题。

写这个 handler 的时候还撞到一个小的：第一版在 `except ModelFailed` 里调了
`await _stop(task)`，而那个 task **已经带着这个异常结束了**，`_stop` 里的
`await task` 把它又抛了一遍——从那个专门用来接住它的处理器里抛出去。

---

## §12 这一章加了什么抽象，以及为什么没加另外三个

### 12.1 加了一个模块：`retry.py`

它只回答一个问题，答案有三个值。350 行，其中 200 行是注释和 docstring，
因为这个模块里的每一个判断都对应一次测量。

**为什么是一个模块而不是几个函数？** 因为分类是一件**跨越信任边界**的事
（§二的第二条例外）：外面那个世界给你一个 status、一个 body、一个可能存在的头，
里面这个世界只认三个词。这一层不存在的时候，`agent.py` 就得知道 429 是什么。

### 12.2 没做通用的 `with_retries(fn, policy)`

这是最诱人的那个抽象，也是插曲 B 的 FB-03 专门警告过的形状。

重试循环需要三样东西：**历史**（重试要重新渲染）、**压缩器**（`shrink` 要用）、
**录制器**（每次失败都要记）。一个通用的 `with_retries` 要么接三个回调，
要么把这三样都变成参数——**那个接口比"在唯一拥有这三样东西的地方写二十行"更难读**。

调用点数一下：一个。按三次法则，连讨论的资格都没有。

### 12.3 `IncompleteStreamError` 下沉——第 4 次

`retry.py` 要分类它，`agent.py` 要抛它，而 `agent.py` 已经 import 了 `retry.py`。
第 1 章的规则（F01-08）：**两层都要的类型，放到两层下面**。

```python
# agent_types.py
class IncompleteStreamError(RuntimeError):
    """..."""
```

```python
# agent.py
__all__ = ["Agent", "IncompleteStreamError", "ModelTurn", "RunResult", "Wiring"]
```

第 1 章 `ToolCall`、后来 `ToolFn`、插曲 B 的 `Footprint` 和 `ToolSet`，
这是**第四次**。插曲 B 说"三次是它从事故变成规则的地方"——第四次的样子应该是
**没有讨论**：五分钟，一次 re-export（好让十一章的测试继续 import 同一个名字），
一个断言那个 re-export 有效的测试，完事。

### 12.4 `Wiring` 从 5 个字段变成 7 个，而那个测试变红是对的

插曲 B 给 `Wiring` 上了一道锁：

```python
def test_FB_03_wiring_carries_only_what_two_call_sites_both_need() -> None:
    fields = set(Wiring.__dataclass_fields__)
    assert fields == {"recorder", "context_window", "summariser",
                      "max_concurrent_tools", "dialect"}
```

加两个字段，它就红。**这不是它挡路，这是它工作。** 它逼着我说清楚为什么这两个
属于 `Wiring` 而不是属于那四个"父子本来就该不同"的关键字参数：

- **`retry_policy`**：一个用着自己那套重试策略的子 Agent，就是插曲 B 量到的那种漂移
  （子 Agent 不压缩、不录制、不并发），只不过这次是**故意**摆出来的。
- **`announce`**：一个没人被告知的等待就是卡死——不管它发生在父 Agent 的循环里，
  还是三层之下的某个子 Agent 里。

理由写进了测试的 docstring，因为下一个想加第八个字段的人会先读到它。

---

## §13 清单外的六条

### 13.1 崩溃会漏一个 session 锁——而正确的写法早就在仓库里了

第 7 章在 session 文件旁边开了一个 `O_EXCL` 锁（F07-08）。释放它的那行
`writer.release()` 是 `_ask` 的**最后一行**：

```python
print(f"[session: {writer.path}  ...]")
writer.release()
return 0
```

**成功才释放，崩了就不释放。** 于是每一次崩溃都在 sessions 目录里留下一个
`.lock`，永远不会有人清：

```
$ ls .minicodex/sessions/
20260812T031426-17552.jsonl
20260812T031426-17552.jsonl.lock      ← 上一次 401 留下的
```

它今天不会导致任何故障（每次运行都拿一个新文件名），所以十一章的栈回溯没人注意到。
修法一行——`finally: writer.release()`。

扎人的地方和第 11 章的 `SYSTEMROOT` 一模一样：**正确的写法在这个仓库里已经有了。**
`RolloutWriter` 从写它的那一章起就有 `__enter__`/`__exit__`；
`run_task` 从第 10 章起就是在 `finally` 里 `writer.release()` 的。
**同一份知识在一个仓库里存在两份，其中一份是对的**——这是第二次。

测试驱动的是 `main()` 而不是 `Agent`，因为漏的地方在装配里：一个测 `Agent` 的测试
在坏版本上照样绿。

### 13.2 第 11 章的变异脚本，从来没进过 CI

第 11 章正文里写着：

> **CI 没有加新步骤。** 这一章新增的检查（19 条变异）跑在 `postmerge.yml` 里……

而 `postmerge.yml` 里只有第 9 章和第 10 章的两个步骤。脚本是对的，正文说的话也
本来该是对的，**只是没有人把这两件事接起来**——这正是一句写在散文里的承诺会有的
失效方式，而一条断言没有。

所以这一章的 CI 改动不是"加两个步骤"，是**把那条规则交给会执行它的东西**：

```python
def test_F_1_05_every_mutation_script_runs_somewhere(repo_root: Path) -> None:
    scripts = sorted(p.name for p in repo_root.glob("probe_mutations*.py"))
    workflow = (repo_root / ".github/workflows/postmerge.yml").read_text(encoding="utf-8")
    missing = [name for name in scripts if name not in workflow]
    assert not missing, f"mutation scripts nothing runs: {missing}"
```

第一次跑它，报出来的不是一个文件，是两个：`probe_mutations.py`（第 6 章那份）
也从来没进过 CI。

这是插曲 B 的层次检查器、第 3 章的描述快照之后，同一条规律第三次出现：
**散文里的承诺不是机制。**

#### 补记：上面这一段后来变成了它自己讲的那个笑话

检查报了两个文件，而这一章只接了一个。`probe_mutations_ch12.py` 进了
`postmerge.yml`；`probe_mutations.py` 被写进了上面那一句话，然后就留在那儿了。

于是这个 step 从此带着一条红测试出厂——**把发现写进散文、没有交给机制**，
正是这一节标题在说的那件事，发生在引入这条检查的那次提交里：

```
FAILED tests/test_packaging.py::test_F_1_05_every_mutation_script_runs_somewhere
AssertionError: mutation scripts nothing runs: ['probe_mutations.py']
```

第 13 章跑整套测试（而不是只跑当章的）时又撞上同一条断言，修好了第 13 章
往后的每一个 step，并在那一章的清单里如实记了一笔：那次修复**没有**把脚本
真正跑到底验过。

现在 `postmerge.yml` 里那一行"Mutation check (chapter 5, generic)"，就是那次
修复回填到检查诞生的这个 step。理由不是补一段历史，是这本书对每个 step 的
承诺——**自包含、跑得起来**。一个自己的测试是红的 step，不满足那个承诺。

第 9、10、11 章和插曲 B 的 step 里，这个脚本同样没接进 CI，而它们**没有被
改动**：让"没接进 CI"变成缺陷的那条规则是这一章才有的，往前回填等于给那几章
安上它们当时没有的标准——和"不重排既有故障编号"是同一条纪律。而且第 11 章的
那次遗漏，正是这条检查存在的理由，抹掉它就把理由抹掉了。

### 13.3 `wait_for` 里有一条死代码，变异测试挖出来的

第一版的 `wait_for` 开头是：

```python
if attempt + 1 >= policy.attempts:
    return None
```

变异测试把它删掉——**44 个测试全绿**。原因不是缺测试，是 `_respond` 里的
`for attempt in range(self.retry_policy.attempts)` **已经在管这件事了**。

一条规则有两个执行点，其中一个必然是装饰。留下的是循环那个，
因为它是读循环的人看得见的那个。删掉的那一段在 docstring 里留了名。

### 13.4 一条让测试变慢而不是变红的变异

上面那条删掉之后，要给"尝试次数不受限"重新写一条变异，我写的是
`for attempt in range(100)`。结果整个变异脚本**在 900 秒的超时上死了**。

原因很实在：每一个期待失败的测试，都会坐在 1-2-4-…-30 的退避里直到撞上 90 秒预算。
测试不是变红，是**变慢**——慢到运行器先死了。

两处修改：

- 变异改成 `range(6)`，一个照样违反策略、但跑得完的数字；
- 变异脚本本身接住 `subprocess.TimeoutExpired`，把它记成**幸存**而不是让脚本崩掉：

```python
except subprocess.TimeoutExpired:
    # Not "caught".  A run that did not finish measured nothing, and
    # chapter 6's rule about unapplied mutations applies here too.
    print(f"  !! the suite did not finish: {label}")
    survivors.append(label)
    continue
```

这是第 6 章那条规则（"没打上的变异算没抓到"）的第二种形态：
**没跑完的变异也算没抓到**。一次报告全绿的变异运行，可能意味着"看不见失败"。

### 13.5 共享的那个假服务器，被这一章的测试撞出问题了

`conftest.py` 里的 `stub_url` 是一个 **session 级、单线程**的 `HTTPServer`，
十一章用得好好的——因为**别的章的测试都会让请求跑完**。

这一章不会。这一章的测试专门做三件事：把流从中间砍断、让客户端 100 毫秒超时、
让服务器睡一秒。从服务器那一侧看，这三件事都是**连接被丢下不管了**。
于是一个还在睡的 handler 挡住了下一个测试的请求，而 `stub.REQUESTS` 是进程级的，
上一个测试遗留的请求会被算进下一个测试的计数里。

症状不是"某个测试红了"，是**每次跑红在不同的地方**。第 14 章的 F14-02 说
flaky 是缺陷；这一次它出现得足够早，还能修。

修法是这一章的测试模块**覆盖掉那个 fixture**：

```python
@pytest.fixture
def stub_url() -> Any:
    """A fresh, threaded stub server for every test in this module. ..."""
    stub.reset()
    server = ThreadingHTTPServer(("127.0.0.1", 0), stub._Handler)
    ...
```

值得记的是**中间那一版**：一开始我只把那个"让服务器睡一秒"的测试单独挪到私有服务器上，
其余照旧共享。那一版当时是绿的——它只解决了我**看见的**那一次冲突，
没有解决"这一章的测试和一个单线程服务器不匹配"这件事本身。

### 13.6 另外两条

- **`ModelHTTPError` 的错误路径十一章没有测试**（§2）。现在有了，而且它是本章
  所有失败的来源。
- **压缩的降级路径会吞掉致命错误**（§8.2），以及守着它的那个测试其实什么都没守——
  也是变异测试发现的。

---

## §14 codex 是怎么做的

### 14.1 它把"能不能重试"写成了一个穷举 match

`codex-rs/protocol/src/error.rs`：

```rust
pub fn is_retryable(&self) -> bool {
    match self.details() {
        CodexErrorDetails::TurnAborted
        | CodexErrorDetails::SessionBudgetExceeded
        | CodexErrorDetails::Interrupted
        | CodexErrorDetails::QuotaExceeded
        | CodexErrorDetails::InvalidRequest(_)
        | CodexErrorDetails::RetryLimit(_)
        | CodexErrorDetails::ContextWindowExceeded
        | CodexErrorDetails::UsageLimitReached(_)
        | CodexErrorDetails::ServerOverloaded
        | ... => false,
        CodexErrorDetails::Stream(..)
        | CodexErrorDetails::Timeout
        | CodexErrorDetails::RequestTimeout
        | CodexErrorDetails::UnexpectedStatus(_)
        | CodexErrorDetails::ConnectionFailed(_)
        | CodexErrorDetails::InternalServerError
        | ... => true,
    }
}
```

三十多个变体，一个一个回答，**没有默认分支**——Rust 的编译器会强制这张表保持完整。
这是"status 分不了类"这句话最强的一个版本：他们干脆放弃了从 HTTP 层推断，
改成为每一种**语义**上的失败单独回答。

有两个地方和我们的选择相反，值得看：

- **`UnexpectedStatus(_) => true`**：不认识的状态码**重试**。我们的默认是 fatal。
  他们有理由：codex 面对的是几十个供应商（Azure、Bedrock、各种代理），
  不认识的状态码大概率是中间某一跳的抖动。我们的程序面对两个供应商，
  实测过五种失败，所以我选另一边——**默认的方向取决于你对"未知"的实际分布知道多少**。
- **`ServerOverloaded => false`**：不重试，直接告诉用户。

### 14.2 它从**正文**里读该等多久，而不是从头里

`codex-rs/codex-api/src/sse/responses.rs`：

```rust
fn try_parse_retry_after(err: &Error) -> Option<Duration> {
    if err.code.as_deref() != Some("rate_limit_exceeded") {
        return None;
    }
    let re = rate_limit_regex();   // r"(?i)try again in\s*(\d+(?:\.\d+)?)\s*(s|ms|seconds?)"
    ...
}
```

也就是 §3.1 里那句 `Please try again in 45.175s`。他们连测试都为 Azure 的写法
单独写了一个（`test_try_parse_retry_after_azure`）。

**同一个数字，他们读正文，我们读头。** 两条路都通，而且都只在
`code == rate_limit_exceeded` 时才认——注意这一点：**连他们也是按 `code` 分派的。**

### 14.3 退避、次数、以及一个他们没有的东西

```rust
// codex-rs/core/src/util.rs
const INITIAL_DELAY_MS: u64 = 200;
const BACKOFF_FACTOR: f64 = 2.0;

pub fn backoff(attempt: u64) -> Duration {
    let exp = BACKOFF_FACTOR.powi(attempt.saturating_sub(1) as i32);
    let base = (INITIAL_DELAY_MS as f64 * exp) as u64;
    let jitter = rand::rng().random_range(0.9..1.1);
    Duration::from_millis((base as f64 * jitter) as u64)
}
```

```rust
// codex-rs/model-provider-info/src/lib.rs
const DEFAULT_STREAM_IDLE_TIMEOUT_MS: u64 = 300_000;
const DEFAULT_STREAM_MAX_RETRIES: u64 = 5;
const DEFAULT_REQUEST_MAX_RETRIES: u64 = 4;
const MAX_STREAM_MAX_RETRIES: u64 = 100;    // 用户配置的硬上限
```

- 起步 200ms（我们 1s），抖动 0.9–1.1（我们 0.5–1.0，因为我们有子 Agent 同时醒的问题，
  他们那边的并发形状不一样）。
- **流和请求分成两个次数**（5 和 4）——"流开起来了但中途断"和"请求根本没通"
  在他们那里是两件事。
- **用户配置有硬上限。** 一个能配的数字就是一个会被配成 10000 的数字。
- **没有总时间预算。** 只有次数。§9 那条"90+120=210 秒"的算术在 codex 里不存在，
  兜底的是 `stream_idle_timeout`（300 秒）和上层的取消。

### 14.4 压缩时又撞上上下文超限

`codex-rs/core/src/compact.rs`，和我们 §8 的 `(nothing left to compact)` 是同一个问题，
答案更耐心：

```rust
Err(e) if matches!(e.details(), CodexErrorDetails::ContextWindowExceeded) => {
    if turn_input_len > 1 {
        // Trim from the beginning to preserve cache (prefix-based) and keep recent messages intact.
        history.remove_first_item();
        retries = 0;
        continue;
    }
    // 只剩一条了，放弃
```

**删掉最老的一条、把重试次数归零、再来一次**，直到只剩一条。注释里那句
"preserve cache (prefix-based)" 是第 13 章的伏笔：从头删是为了保住前缀缓存。

我们没有做这个循环——`_shrink` 一轮只压一次然后放弃。差别在于成本：他们那个循环
每一轮都可能再调一次摘要模型，而我们已经有一条更便宜的路（第 6 章的 `boundaries()`
一次就能选好切点）。这条差异写在 `_shrink` 的注释里，不是漏掉的。

---

## §15 装上之后是什么样

一次两轮对话，中间塞两个 429（无头版，好让它别真的等 45 秒）：

```
[rate_limit: waiting 1s, attempt 2 of 4]
[rate_limit: waiting 2s, attempt 3 of 4]
`src/minicodex/__init__.py` defines `__version__` and `system_prompt()`.

[gemma4:31b | completed after 2 turn(s)]
```

一次没救的：

```
the model call failed: Incorrect API key provided: sk-not-a*****-key. ... [invalid_api_key]
check OPENAI_API_KEY, or pass --provider ollama to run against a local model.
provider request id: req_f07918595554434c8f429fdad1bb1547
```

### 文件清点

| 文件 | 行数 | 完整代码在哪一节 |
|---|---|---|
| `src/minicodex/retry.py` | 350 | §4.1 / §4.3 / §5 / §11.1（分段给全） |
| `src/minicodex/agent.py` | 771（+211） | §6（`_respond`）/ §8（`_shrink`） |
| `src/minicodex/model.py` | 303（+63） | §4.2 |
| `src/minicodex/stub.py` | 419（+146） | §3（录音）+ 队列 |
| `src/minicodex/compaction.py` | +14 | §8.2 |
| `src/minicodex/subagent.py` | +45 | §11.2 |
| `src/minicodex/__main__.py` | +18 | §11.1 / §13.1 |
| `tests/test_faults_ch12.py` | 983 | 全章散见 |
| `probe_retry.py` | 472 | 输出散见，源码不进正文 |
| `probe_mutations_ch12.py` | 233 | §13.4 给了关键的一段 |

没进正文的：`stub.py` 里那个失败队列的 HTTP handler（三十行样板），
`probe_retry.py` 的全文（它是七段互不相干的测量脚本，逐段贴没有意义）。

---

## §16 验证

### 16.1 44 个测试

```
$ uv run pytest -q tests/test_faults_ch12.py
............................................                             [100%]
```

全离线，全部跑在录下来的响应体上。最慢的一个 1.41 秒。

### 16.2 23 条变异

```
$ uv run python probe_mutations_ch12.py
23 mutations, tests/test_faults_ch12.py tests/test_agent.py tests/test_compaction.py tests/test_faults_ch10.py

    7 test(s) fail  <-  a context-length refusal is terminal like any other 400
   10 test(s) fail  <-  classification reads the status code and nothing else
    1 test(s) fail  <-  an unrecognised failure is retried instead of reported
    1 test(s) fail  <-  a request this program built wrongly is retried as if it were weather
    4 test(s) fail  <-  the server's Retry-After is ignored in favour of our own backoff
    3 test(s) fail  <-  the millisecond header loses to the rounded-up whole-second one
    1 test(s) fail  <-  an unparseable Retry-After raises instead of falling back
    3 test(s) fail  <-  the budget is advisory: wait the server's number even if it does not fit
    1 test(s) fail  <-  the failure is reported without a next action
    1 test(s) fail  <-  the request id is dropped again
   11 test(s) fail  <-  the error body is not parsed, only formatted
    ...
```

第一次跑，**三条幸存**，而且三条都不是"补个测试"就完事的：

1. `attempts are unbounded` → 那条检查本身是死的（§13.3），删掉重复。
2. `the retry budget counts sleeping only` → 预算的定义是错的（§9.1），改定义。
3. `every summariser failure is fatal` → 守它的测试根本走不到那条分支（§8.2），
   补一个走得到的。

**三条里只有一条的修法是"写测试"。** 这是变异测试最值钱的地方：
它报告的不总是覆盖率，有时候是设计。

### 16.3 全套

```
$ uv run pytest
1498 passed, 9 skipped

$ uv run python scripts/check_layers.py
ok  no import cycles
ok  __init__ is a leaf
ok  forbidden edges
ok  one Agent construction site
ok  every module imports alone
```

---

## §17 收工：commit、PR、review

### commit 序列

七个，每一个都能单独通过测试：

```
1  feat(model): parse the provider's error envelope at the boundary

   The client raised one formatted string and nobody caught it -- `grep -rn
   ModelHTTPError src tests` returned two lines, both in model.py. Every
   caller that wanted to know *what kind* of failure it was would have had to
   parse the message back out.

   Fields chosen from five recorded failures (probe_retry.py shapes): two of
   them are HTTP 400 with the same `type` and opposite correct reactions, so
   `code` is the only field that separates them.

   The body is kept whole as well as parsed: a gateway returns HTML, not this
   envelope, and a parser that assumes JSON turns somebody else's outage into
   a JSONDecodeError inside our own error handling.

2  feat(retry): classify a failure into retry / shrink / fatal

   Three values because there are three actions. Unrecognised is fatal, which
   is the opposite of chapter 5's rule for unknown shell syntax: there is no
   human inside a backoff, and the two mistakes are not symmetric -- a wrongly
   terminal failure costs one run and prints why; a wrongly retried one costs
   the quota in silence, and a rejected 160k-token request was measured
   debiting its 160k tokens anyway.

3  feat(agent): retry around stream-plus-assembly, not inside stream()

   `stream()` has already yielded TextDeltas by the time a connection drops
   and cannot un-yield them. `_collect` returns nothing until [DONE], so this
   is the innermost boundary at which a failed attempt has produced nothing.
   F12-03 and F12-09 fall out of that property with no code.

   `except Exception`, not BaseException: chapter 7's F07-04 the other way
   round -- a Ctrl-C classified as a failure would be retried.

4  feat(agent): compact and retry when the provider says the request is too long

   The only 400 that is a fact about us rather than about the request. Forced
   rather than reconsidered -- `_maybe_compact` would consult the estimate
   that just proved wrong. Once per turn; a second identical refusal means the
   summary is what does not fit.

   The stated token count is clamped at 4x before it reaches chapter 6's
   calibration. Unclamped, one observation moved the ratio to x250 and the run
   compacted on 11 of its 12 turns without finishing.

5  fix(subagent): a provider failure is an outcome, not a traceback

   `run_task` has documented "never raises for anything the sub-agent did"
   since chapter 10 and it was not true for the one thing a sub-agent cannot
   control. A 429 inside a child reached the *parent model* as
   `Error: spawn_agent raised ModelHTTPError: HTTP 429 ...` plus a JSON blob.

6  fix(cli): translate the failure, and release the session lock on the way out

   Three lines and an exit code instead of twenty lines of traceback. The
   lock: `writer.release()` was the last statement of `_ask`, so it ran on
   success and never on failure. `run_task` has released in a `finally` since
   chapter 10 -- the correct pattern was already in the repository, one module
   over, exactly like chapter 11's SYSTEMROOT.

7  test(ci): assert that every mutation script is actually run

   Chapter 11's text says its nineteen mutations run in postmerge.yml. The
   workflow had steps for 9 and 10 only, and so did nothing for chapter 6's.
   A promise written in prose is not a mechanism.
```

第 4 个 commit 的 body 值得单独说一句：它写的是**为什么钳位**，而不是"加了一个钳位"。
diff 里能看到钳位；看不到的是没有钳位时那 11 次压缩。

### PR 描述

```markdown
## What

One question -- *what kind of failure is this* -- and three answers: retry,
shrink, fatal. Plus everything that falls out of putting the question in the
right place.

## Why

The program ended on the first failed request, of any kind, with a twenty-line
traceback. Meanwhile the *probe scripts* of chapters 10 and 11 each carry a
private retry loop, with a comment saying "chapter 12 is where retries are
designed". Three chapters of measurement worked because the measuring tools
retried and the measured program did not.

## How

Measured before designed. Five real failures from api.openai.com, recorded
verbatim into stub.py:

* two HTTP 400s with the same `type` and opposite correct reactions, which is
  why classification reads `code`;
* a real 429 obtained for free, because a request rejected for length still
  debits its tokens -- so two of them inside a minute is a rate limit with a
  bill of zero;
* that 429's headers say `retry-after: 46` / `retry-after-ms: 45175`, against
  which a 1-2-4-8-16 backoff fits all five of its attempts inside 31 seconds.

`Idempotency-Key` was measured doing nothing on this endpoint (two answers,
two request ids, two debits), so F12-04 is answered by where the retry
boundary sits instead of by a header.

## Testing

44 new tests, all offline, all against the recorded bodies. 23 mutations,
three of which survived the first run -- two of those were design faults, not
missing tests (a duplicated attempt check, and a retry budget that counted
only sleeping).

`uv run pytest` -> 1498 passed, 9 skipped.

## Notes for the reviewer

* `Wiring` goes from five fields to seven and interlude B's assertion goes red
  on purpose; the reasoning is in the test's docstring.
* `DEFAULT_ATTEMPT_TIMEOUT` drops from 300 to 120 so that the four timeouts in
  this program are strictly nested. `probe_retry.py nesting` is the before/after.
* `.github/workflows/postmerge.yml` grows two steps that should have been
  added in chapters 6 and 11.
```

### Code review

我扮演 reviewer，五条，按"正确性 > 边界 > 可测试性 > 命名 > 风格"排：

> **1（正确性）**：`_shrink` 在 `plan.saving <= 0` 时抛 `ModelFailed`，但那之前
> 已经 `recorder.record("compaction", ...)` 了。录制里会出现一次实际上没发生的压缩。

接受，但**不改**，理由写进注释：录制的语义是"这一步被尝试了"，
而"尝试压缩然后发现没得压"正是排查时想看到的事件。真正该改的是它没有 `saving`
字段——已经有了（`fits` / `dropped`）。

> **2（边界）**：`wait_for` 的 `retry_after` 没有上限。一个（恶意或者出错的）服务器
> 发 `retry-after: 86400`，我们会等一天吗？

不会——`budget` 拦住它（86400 > 90 → 返回 `None` → 放弃）。**但这是个好问题，
因为它靠的是两个字段的相互作用而不是一个显式的检查**，而 §5 的测试
`test_F12_02_waiting_less_than_asked_is_not_an_option` 正好覆盖它。补一句注释指过去。

> **3（可测试性）**：`test_F12_01_a_hanging_server_...` 自己起了一个 HTTP server，
> 和 `conftest` 的 fixture 重复。

不合并，理由写在测试里：共享的那个 stub 是**单线程**的，一个被要求睡一秒的 handler
会把整个测试套件后面的请求堵在它后面（第一次写的时候真的堵了，
`test_F12_03` 因此变红）。这是"便利的共享装置"的代价，值得留在页面上。

> **4（命名）**：`Failure.kind` 和 `Failure.disposition` 容易混。

保留。`disposition` 是**要做什么**（三个值），`kind` 是**是什么**（十来个值）。
两者一对多，合并成一个就会出现 `retry_rate_limit` / `retry_server_error` 这种
把两个维度编码进一个字符串的名字。

> **5（风格）**：`_respond` 返回四元组。

同意难看，**仍然不改**，理由写在调用点的注释里：一个 `Response` 类会有恰好一个
构造点和一个读取点，那正是 FB-03 说的"仪式性抽象"。第二个读取点出现的那天再说。

### Merge 与 CI

squash 成一个 commit 进 `main`。

**CI 加了两个步骤，但都在 `postmerge.yml` 里**，而且加的方式是先写断言再补步骤
（§13.2）。第 -1 章那条 `assert len(steps) <= 6` 这次仍然没有响——**因为这一章
根本没往挡 merge 的那六步里加东西**，这是第 9 章定下的分层第一次被自动地遵守。

---

## §18 回头看：这一章撞到了什么

清单 9 条：

| ID | 结果 |
|---|---|
| F12-01 | 复现。关键不是"要分类"，是**用 `code` 而不是 status** |
| F12-02 | 复现，并且量到了确切的数字：45.175s vs 我们的 31s |
| F12-03 | **结构上不可能发生**，因为第 0 章的 F00-04。零行代码 |
| F12-04 | **药方实测无效**（`Idempotency-Key` 无作用），换了一个关于边界位置的答案 |
| F12-05 | 复现，并且**修它的过程本身制造了这一章最贵的故障**（x250 → 12 轮压 11 次） |
| F12-06 | 复现，形态是"两个时钟设成了同一个数"；断言写出来时**是假的** |
| F12-07 | 复现，`ruff` 静态就能抓；真正的代价是整个事件循环停一秒 |
| F12-08 | 复现，而且**清单写的那个形状真实存在**——通过子 Agent 那扇门 |
| F12-09 | **结构上不可能发生**，同 F12-03 |

清单外 6 条：崩溃漏 session 锁、`ModelHTTPError` 十一章无测试、第 11 章的变异脚本
从没进 CI（连带第 6 章的）、`wait_for` 里的死检查、一条让套件超时而不是变红的变异、
压缩降级路径吞掉致命错误。

发现方式的分布（15 条）：

| 方式 | 条数 |
|---|---|
| ⚪ 静态 / 变异测试 | 4 |
| 🟠 可观测性 | 3 |
| 🟣 review / 读代码 | 3 |
| 🟡 静默 | 2 |
| — 结构上不可能，或药方实测无效 | 3 |

**没有一条是"自己跳出来"的。** 这一章里唯一一个会自己跳出来的东西，
是 §2 那条二十行的栈回溯——而它不是这一章发现的故障，它是这一章要修的**现状**：
一个从第 0 章就在那里、十一章没有人接过的 `raise`。

值得单独说的是 ⚪ 那一栏占了最多。前面几章的 ⚪ 基本都是 linter，
这一章有三条来自**变异测试**，而且其中两条的修法不是"补一个测试"，
是"删掉一处重复"和"改掉一个字段的定义"。**变异测试报告的不总是覆盖率，
有时候是设计。**

---

## 如果你只记住三件事

1. **状态码不能给失败分类，`code` 可以。** 两条 HTTP 400、同一个 `type`，
   一条要压缩后重试、一条永远不能再发。任何"5xx 重试、4xx 不重试"的规则，
   在你的供应商真实的失败集合面前都是猜的——**去把它们撞出来看一眼，一共花不了十分钟**。

2. **重试的边界要画在"什么都还没提交"的那个位置。** 画对了，
   "流断了怎么办""历史里会不会有残渣""工具会不会跑两次"这三个问题一起消失，
   一行代码都不用写。画错了，你要为每一个都单独写一套补救。

3. **服务器说了要等多久，就等那么久；等不起就别等。** 少等一会儿然后照发不误，
   不是"尽力而为"，是**把停机时间变长的那个动作**——它会再吃一次配额，
   再把重置时间往后推。同一条道理的一般形式：
   **对方已经告诉你的事情，不要用自己的启发式去覆盖。**

---

## 动手练习

1. **把钳位退回去。** 删掉 `_shrink` 里的 `MAX_REFUSAL_CORRECTION` 判断，
   跑 `test_F12_05_an_absurd_stated_count_does_not_move_the_ruler`。
   然后把 `context_window` 调大、看录制里的 `calibration` 字段，
   亲眼看它从 `x1.00` 跳到 `x250` 之后每一轮都在压缩。

2. **让服务器说一个荒唐的数字。** 在 `stub.RATE_LIMITED` 的头里把
   `retry-after` 改成 `86400`，跑一次带重试的对话。它应该**立刻放弃**并且
   在消息里说服务器要求 86400 秒。如果它开始睡，说明你把 `budget` 的检查删掉了。

3. **加一个供应商特有的 `code`。** 假设某个供应商在过载时返回
   `code: "engine_overloaded"`、status 503。在 `_BY_CODE` 里加一行，
   再写一个断言 `explain()` 会给出人能执行的下一步的测试。
   然后**去掉那一行**，确认 status 兜底规则（5xx → retry）仍然让它可重试——
   这就是那张表和那个兜底各自的分工。

4. **把 `except Exception` 改成 `except BaseException`。** 跑
   `test_F12_07_a_cancellation_is_never_classified_as_a_failure`。
   然后想一想：第 7 章的 F07-04 是同一个知识点的哪一面？

5. **（难）给 `_shrink` 加上 codex 的那个循环**：压完还是超长时，
   删掉最老的一条历史、重试次数归零、再来一次，直到只剩一条。
   写一个测试证明它比"一轮只压一次"多救回了哪一类会话，
   再算一下最坏情况下它多花几次模型调用——然后决定要不要留着。
---

# 附录 · 代码到底怎么敲出来的（新手向，逐段精讲）

跟第 11 章附录同一个规矩：正文讲"为什么"，这里补"怎么写"。默认你已经读过
第 9、10、11 章的附录（尤其是 `asyncio` 和 `dataclasses.replace` 的部分），
这里不重复那些基础，只讲这一章新出现的、容易让新手卡住的写法。代码摘自
`steps/step12_retry/src/minicodex/`，逐段核对过。

先把范围说死，避免这次又出现"看起来讲了，实际漏了一截"：

1. 本附录只解释第 12 章在 `steps/step12_retry/` 里新增或修改的代码。
   第 0～11 章已经存在、这一章没有改动的协议、历史、调度器和 MCP 实现，
   不再整文件复制；但本章调用它们时，会把参数形状、返回值和边界写清楚。
2. 下面标成"完整函数"的代码块里不使用代表遗漏实现的省略号，也不把关键
   分支写成"此处略"。类型标注、示例字符串或命令文本里出现的三个点，
   如果本来就是源码的一部分，会原样保留。如果一段代码只展示一个变更点，
   会明确说它是"变更片段"，不把片段冒充成完整文件。
3. 代码块按当前源码核对：实现来源是 `steps/step12_retry/src/minicodex/`，
   测试来源是 `steps/step12_retry/tests/`。正文里的实验探针是测量工具，
   不是运行时必需模块；附录会解释它们怎样覆盖代码，但不会把机械的临时
   工作区搭建伪装成产品实现。

## D0 · 这一章新出现的写法，先过一遍

**1. `time.monotonic()` 而不是 `time.time()`。** 这一章要量"从开始到现在
过了多久"，然后拿它和预算比。`time.time()` 是墙上时钟，机器改时间、NTP
校时、夏令时都会让它跳，一跳到过去，预算检查就会得出"还早，继续等"。
`monotonic()` 只增不减，是"量一段时长"的唯一正确工具：

```python
import time
began = time.monotonic()      # 开始时刻
elapsed = time.monotonic() - began   # 现在距开始，秒
```

**2. `dataclasses.replace(obj, 字段=新值)`。** 第 10 章附录 B0 讲过它给
`frozen=True` 的 dataclass"改"字段。这一章在 `_shrink` 里用它给 `Failure`
追加说明文字，而 `Failure` 是 frozen 的，不能原地改：

```python
from dataclasses import replace
raise ModelFailed(
    replace(failure, detail=f"{failure.detail} (still too long after compacting once)")
)
```

`replace` 是浅拷贝：`failure` 里的 `retry_after`、`request_id`、`stated_tokens`
这些字段照抄，只有 `detail` 换掉。这正是"失败还是那个失败，只是对读者的
说明更长了"的意思。

**3. `random.uniform(0.5, 1.0)` 做抖动。** 指数退避本身会让所有同时开始
重试的客户端在同一秒醒来（父 Agent 和几个子 Agent 共用同一个账户、同一个
时钟）。抖动把等待时间乘上一个随机数，让它们错开。`uniform(a, b)` 返回
`[a, b)` 之间的均匀随机浮点数，比 `random.random()`（永远在 `[0,1)`）好读：

```python
wait *= random.uniform(0.5, 1.0)   # 半倍到一倍之间
```

**4. 正则的 `re.S` 标志。** `_TOKENS` 要匹配的句子跨两行
（"maximum context length is 128000 tokens. However, your messages
resulted in 160008 tokens."），默认 `.` 不匹配换行，`re.S` 让 `.` 连换行
一起吃掉：

```python
_TOKENS = re.compile(r"maximum context length is (\d+) tokens.*?resulted in (\d+) tokens", re.S)
```

注意 `.*?` 是**非贪婪**：它匹配到第一个 "resulted in" 为止，而不是最后一个。

## D1 · 产品的重试边界在哪：探针里有 `_retry`，产品里没有

正文 §2 引了第 10、11 章探针脚本里那份 `_retry`。先把它放在正确的坐标系里：

- 那份 `_retry` 是**测量工具**的一部分，不是产品代码。它在
  `probe_*.py` 里，作用只有一个：让探针跑完 60 个样本时，不因为第 40 个
  样本的网络抖动而丢掉前 39 个。
- 产品的重试**不在** `retry.py` 里。`retry.py` 只回答"这次失败是哪种"，
  循环本身在 `agent.py` 的 `_respond` 里（本附录 D6）。

先看探针那份长什么样。它最早写在 `steps/step10_subagents/probe_subagent.py`
（第 11 章从第 10 章复制过去，正文引的就是这份），docstring 之外，
函数体是这样的：

```python
async def _retry(make: Any, attempts: int = 4) -> Any:
    """Run one sample, surviving a dropped connection.

    Not a fix for anything -- chapter 12 is where retries are designed.  This
    is here because a probe that dies on request 40 of 60 wastes the first 39,
    and `httpx.ConnectError` / `RemoteProtocolError` both happened while these
    numbers were being collected.
    """
    for attempt in range(attempts):
        try:
            return await make()
        except (httpx.ConnectError, httpx.RemoteProtocolError, httpx.ReadError) as exc:
            if attempt == attempts - 1:
                raise
            print(f"    (retrying after {type(exc).__name__})", file=sys.stderr)
            await asyncio.sleep(2 * (attempt + 1))
```

三个值得注意的细节，新手容易抄漏：

1. **`except ... as exc:` 把异常接住，但最后一次失败要把原异常**原样**再抛
   （`raise` 不带参数 = 重新抛正在处理的这个）。** 探针的调用方（probe 的
   `main`）不检查返回值，它靠异常判断"这个样本彻底失败了"，所以循环自己
   不能把最后一次失败吞掉。
2. **`if attempt == attempts - 1: raise` 放在 `sleep` 之前。** 顺序反了的话，
   最后一次失败也会先睡 2×attempt 秒再抛，白白多等。
3. **它只接 `httpx` 的传输层异常。** 前面讲过，`ModelHTTPError` 不在这个
   列表里——探针不需要接它，因为探针的样本不会触发"服务器答了个错误"
   （它只关心"连接没通"这种抖动）。

新手最容易在这里犯的错是**把探针里的循环直接搬进产品**。搬过去之后，它
对 `ModelHTTPError`（HTTP 非 200）一个都接不住，因为 `ModelHTTPError` 不是
`httpx` 的传输层异常——它是 `model.stream()` 在收到 4xx/5xx 时**自己**抛的。
这也是正文 §2 那句"三章的测量之所以能做完，是因为测量工具会重试；而被
测量的程序不会"的技术含义：探针重试的是"连接没通"，产品要处理的是
"服务器答了，但答的是一个错误"。

## D2 · `Failure`：三种处置，六个字段

正文 §4.1 已经给了 `Failure` 的完整定义和每个字段的理由，这里只补一段
"怎么写"的视角：**字段名就是循环的分支条件**。`disposition` 只有三个值，
`_respond` 里对它的判断也只有三处（`shrink` / `fatal` / 其他），一一对应：

```python
Disposition = Literal["retry", "shrink", "fatal"]
```

`Literal` 不是运行时守卫（第 10 章附录 B0 讲过），但类型检查器会在你
写错字符串时报警，这就是它在这里的全部作用。

`stated_tokens` 这个字段是这一章独有的：它只在 `context_length` 失败时
被填，携带的是"服务器在拒绝你的时候顺便告诉你的真实 token 数"——被拒的
请求没有 usage 分片（第 6 章），这是校准器唯一能拿到那个请求真实大小的
渠道。写成 `tuple[int, int] | None`（上限、实际），因为错误正文里两个数
是"maximum ... is 128000 ... resulted in 160008"，顺序固定。

## D3 · `classify`：把异常变成 `Failure`

正文 §4.3 给了 `classify` 的完整函数，这里补两段正文没展开的细节：

**第一，`isinstance` 的检查顺序就是优先级顺序。** `IncompleteStreamError`
和 `httpx.TransportError` 之间有先后：流被掐断（没有 `[DONE]`）是
"重试，因为什么都没留下"（F00-04），而 `httpx.TransportError` 里既有
"天气"（超时、连接断）也有"我们自己的 bug"（`LocalProtocolError`——
请求拼错了）。所以 `_OUR_FAULT` 必须在 `httpx.TransportError` **之前**
检查：`LocalProtocolError` 也是 `TransportError` 的子类，顺序反了，
"我们拼错请求"会被错判成"值得再试一次"。

**第二，默认值为什么是 fatal。** 正文 §4.4 讲了两个方向的代价不对称。
代码层面它只有一行：

```python
return Failure("fatal", "unexpected", f"{type(exc).__name__}: {exc}")
```

`f"{type(exc).__name__}: {exc}"` 这个格式是有意的：`type(exc).__name__`
给你类名（`ValueError`、`KeyError`……），`exc` 给你 str 化的消息。
两者拼在一起，一个不认识的东西至少能被认出来"是哪个类"。

## D4 · `_http`：把 `ModelHTTPError` 变成 `Failure`

正文 §4.2 只讲了"解析放在边界上"的思路，`_http` 的完整实现没进正文。
它是 `classify` 里最长的分支，也是全章最容易写歪的一段：

```python
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

逐段拆：

**（一）`detail` 的组装。** 先把 `message` 拿来当主体；没有 `message` 就
退回 body 的前 300 个字符（防止把整个 HTML 错误页塞进 `Failure`）。有
`code` 时拼上 `[code]`——这就是用户最后在终端看到的
`[invalid_api_key]` 的来源。

**（二）查表优先，status 兜底。** `_BY_CODE.get(exc.code or "", ("", ""))`
：查得到就用查到的；查不到（`code` 为空或不认识），`disposition` 是空串
（falsy），落到下面的 status 规则。**两张表的分工是：`code` 是供应商的
词，`status` 是 HTTP 本身的词。** 429 和 408 都算"重试"，5xx 算
"服务器有难，值得再试"，其余 4xx 一律 fatal，并把 status 数字本身作为
`kind`（`http_400`、`http_401`……），这样"不认识"也留下可查的痕迹。

**（三）`stated_tokens` 的提取。** 只有 `kind == "context_length"` 才去
正则里搜，而且搜索对象是 `exc.message` 而不是 body——正文 §8 的例子
证明数字在 message 里。`(m := _TOKENS.search(...))` 是海象运算符：赋值
表达式，把匹配结果同时用于判断和取组。`int(m.group(1))` 是上限，
`int(m.group(2))` 是实际值，顺序和正则里的两个捕获组一一对应。

**（四）`# type: ignore[arg-type]` 那一行。** `disposition` 在查表分支里
被推断成 `str`，而 `Failure` 的第一个字段要 `Literal["retry", "shrink",
"fatal"]`。运行时无所谓（就是三个字符串之一），但类型检查器会叫。这里
的选择是加一行注释承认"运行时保证它合法"。

## D5 · `_retry_after` 与 `RetryPolicy`、`wait_for`：怎么等、等多久

正文 §5 给了 `_retry_after` 的完整代码，§5.1 给了 `wait_for` 的片段。
`RetryPolicy` 的完整定义和 `wait_for` 的完整实现是附录补的部分。

**`RetryPolicy`：四个数字，其中 `budget` 是真正干活的。**

```python
@dataclass(frozen=True)
class RetryPolicy:
    attempts: int = 4
    base: float = 1.0
    cap: float = 30.0
    budget: float = 90.0
```

`frozen=True` 让它和 `Failure`、`Wiring` 一样"造出来就不许改"——策略在
一个 run 里是常量，想改就造一个新的。`attempts` 是次数上限，`base` 是
指数退避的底（`2**attempt` 的系数），`cap` 是单次等待上限，`budget` 是
**整个 turn 的墙钟预算**。正文 §5.1 讲过的核心结论再强调一遍：
`budget` 量的是"从第一次尝试开始的全部时间"，不是"睡了多少"——一个
每次 120 秒超时的请求，四次尝试花 8 分钟，一次 `sleep` 都没有。

**`wait_for`：返回等多久，或返回 `None` 表示别等了。**

```python
def wait_for(
    failure: Failure,
    attempt: int,
    policy: RetryPolicy = DEFAULT_POLICY,
    *,
    elapsed: float = 0.0,
    jitter: bool = True,
) -> float | None:
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

三个新手最容易写错的点：

1. **两个入口，一个出口。** 服务器说了 `retry-after` 就用它，没说才用
   本地退避——但**两者都要过预算检查**。第一版只给本地退避过检查，于是
   一个说 "retry-after: 86400" 的服务器会让循环睡一天。正文 §5.1 那句
   "等不起就不等"就是这个检查。
2. **`None` 的两种含义是同一件事。** "预算耗尽"和"服务器要求的时长
   超过整个预算"都返回 `None`，调用方对两者做同一件事：放弃，报告手上
   已有的失败。区别只在人读的句子（`explain` 管这个），不在控制流。
3. **`attempt` 从 0 开始数。** `2**attempt` 给出 1、2、4、8、16……的
   退避序列；`attempt=0` 是第一次失败后等 `min(30, 1.0)` 秒。

## D6 · `_respond`：重试循环真正住的地方

正文 §6 讲了"重试该套在哪一层"的理由，这里给完整函数并逐段讲怎么写。
这是全章最长的一段代码，拆成四块看。

```python
async def _respond(
    self, history: History, turn_index: int
) -> tuple[ModelTurn, History, int, CompactionResult | None]:
    began = time.monotonic()
    shrunk = False
    forced: CompactionResult | None = None

    for attempt in range(self.retry_policy.attempts):
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
            if turn.usage is not None:
                self.calibration.observe(estimated=estimated, actual=turn.usage.prompt_tokens)
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
        await asyncio.sleep(wait)

    raise ModelFailed(
        failure, attempts=self.retry_policy.attempts, waited=time.monotonic() - began
    )
```

**第一块：墙钟起点和状态。** `began = time.monotonic()` 在循环**外面**——
预算量的是整个 turn，不是每次等待。`shrunk` 和 `forced` 也在外面：
`shrunk` 是"这一轮已经压过一次"的旗标（D7 会看到它的用途），`forced`
是"服务器逼出来的那次压缩"的结果，最后要带回 `run` 汇报。

**第二块：请求-响应，成功就直接返回。** 每次尝试都**重新**把 history
序列化成 wire 消息——因为 `shrink` 会在循环里替换掉 history，用旧的
序列化结果就白压了。`estimated` 是第 6 章校准器的估算值，成功路径上
`turn.usage` 是对它的唯一一次校准机会（`observe`）。`try/except/else`
的结构里，`else` 分支只在 `try` 成功时执行——成功就直接 `return`，
失败才走到下面的分类处理。

**第三块：分类后分三条路。** `shrink` 是唯一一个"改自己"的处置：
压缩后 `continue` 再来一次（`continue` 会重新执行循环体，重新算
`messages`）。`fatal` 直接 `raise ModelFailed`——注意 `attempt + 1`，
因为 `attempt` 从 0 数起，人读的"第几次尝试"要加一。剩下的（`retry`）
才问 `wait_for` 等多久；`wait is None` 说明预算耗尽或服务器要的太久，
同样 `raise ModelFailed`。

**第四块：等，然后让循环自己决定。** `self._say(...)` 是"等待被说出来"
的通道（第 11 章加的 `announce`）。`await asyncio.sleep(wait)` 而不是
`time.sleep`——`time.sleep` 会**阻塞整个事件循环**，让同进程里的其他
工具、子 Agent、MCP 读循环全部卡死（F12-07 的完整代价）。

循环自然耗尽（`range(self.retry_policy.attempts)` 走完）时，最后一次
`failure` 还在作用域里，用它抛 `ModelFailed`。这正是"attempts 次数上限"
的第二道闸——`wait_for` 里刻意**不查**次数，因为查了就是死代码
（正文 §13.3，变异测试挖出来的）。

## D7 · `_shrink`：唯一一个"改自己"的分支

正文 §8 给了 `_shrink` 的四个分支片段，完整函数在这里，逐段讲：

```python
async def _shrink(
    self, history: History, failure: Failure, estimated: int, *, shrunk: bool
) -> tuple[History, CompactionResult]:
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
        correction = actual / estimated if estimated > 0 else 0.0
        if 0 < correction <= MAX_REFUSAL_CORRECTION:
            self.calibration.observe(estimated=estimated, actual=actual)
        else:
            self.recorder.record(
                "calibration_rejected",
                {"estimated": estimated, "stated": actual, "correction": correction},
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
        raise ModelFailed(
            replace(failure, detail=f"{failure.detail} (nothing left to compact)")
        )

    self._say(f"[context too long for the provider; compacted {result.plan.drops} message(s)]")
    self.rollout.mark("compacted", generation=result.generation, replaced=result.plan.drops)
    return self._attach(result.history), result
```

**第一道闸：一轮只压一次。** `shrunk` 旗标在 `_respond` 里置位，第二次
进入 `_shrink` 说明压完还是超长——摘要本身就不够小，再压是带账单的死
循环，直接失败。

**第二道闸：没有压缩能力就失败。** `context_window` 或 `summariser` 为
`None`（第 6 章之前的默认）时，`compact` 无从谈起。注意这里抛的是
**原样** `failure`，没加说明——`explain` 会给它补上"Pass --context-window"
那句下一步。

**第三块：钳位校准。** 正文 §8.1 完整讲了这个 x250 事故。代码层面：
`correction = actual / estimated` 超出 `MAX_REFUSAL_CORRECTION`（4.0）时
**不**调用 `observe`，而是记一条 `calibration_rejected` 到录制里。小于
等于 4 才采纳——第 6 章实测本地估算在 0.43x 到 1.04x 之间，一个单轮
就要 4 倍以上修正的不是"内容变了"，是"别的什么东西"（代理、换模型、
撒谎的 body）。`estimated > 0` 的守卫防止除零。

**第四块：压缩 + 结果检查。** `compact` 返回的是第 6 章那个 `CompactionResult`
（含 `plan`、`generation`、`degraded`）。`result.plan.saving <= 0` 是
第三道闸：什么都没剪下来（一条消息本身就超窗口，F06-09），再发同样的
请求就是 F12-01 加一道压缩仪式，直接失败。成功路径上 `self._attach` 把
新 history 重新挂到 rollout 上（正文 §12.4 提过），`forced` 带回 `run`
汇报。

## D8 · `ModelHTTPError`：从"一个字符串"到"四个字段"

正文 §2 展示了十一行没被 catch 的 `ModelHTTPError`。附录补这个类的完整
实现——它从"格式化字符串"升级成"解析好的失败"，是这一章所有分类的
前提：

```python
class ModelHTTPError(RuntimeError):
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
        self.request_id = self.headers.get("x-request-id")
        super().__init__(f"HTTP {status} from {url}: {self.message or body[:500]}")
```

新手最容易卡住的四行：

1. **`headers` 为什么转小写。** `httpx` 返回的 headers 键是大小写混合的
   （`X-Request-Id`、`Retry-After`），而 `_retry_after` 查的是
   `"retry-after-ms"`。统一转小写，查表就不用猜大小写。
2. **`str(error.get("code") or "") or None` 这串。** `error.get("code")`
   可能是 `None`、空串、数字或字符串。`or ""` 把 falsy 值归一成空串，
   `str(...)` 把数字转成字符串（`"invalid_api_key"` 本身是 str，无伤），
   最后 `or None` 把空串归一成 `None`。三个 `Failure` 的可选字段都用这
   个写法：**"没有"就是 `None`，而不是空串。**
3. **`try/except ValueError` 而不是假设 JSON。** 代理或网关出错时返回
   HTML（正文 docstring 讲了）。`json.loads` 失败就 `payload = None`，
   后面的 `isinstance` 检查自然跳过——**别人的 outage 不能变成我们的
   `JSONDecodeError`**。
4. **`request_id` 单独捡出来。** `x-request-id` 是全书唯一能对外查这次
   失败的字符串，十一章里一直跟其他 header 一起被扔掉。这一章把它存成
   字段，`explain` 里最后一行输出它。

`super().__init__(...)` 拼的是旧版那个"人一眼能看懂的摘要"——`str(exc)`
仍然是"HTTP 429 from https://...: Rate limit reached..."，所有旧调用点
不用改就能打印出有用的东西。

## D9 · `explain` 与 `ModelFailed`：给人读的话

正文 §11.1 给了 `explain` 和 `_ACTIONS`（`_ACTIONS` 正文里带省略号，
这里给全）。`ModelFailed` 的完整实现是附录补的：

```python
class ModelFailed(RuntimeError):
    def __init__(self, failure: Failure, *, attempts: int = 1, waited: float = 0.0) -> None:
        self.failure = failure
        self.attempts = attempts
        self.waited = waited
        super().__init__(explain(failure, waited=waited, attempts=attempts))
```

`ModelFailed` 只有一个构造点，把三样东西打包：`failure`（完整分类信息，
给会去看的调用方——`__main__` 打印它，`subagent.run_task` 把它变成
`TaskResult`）、`attempts` 和 `waited`（给人读的"试了几次、等了多久"）。
`str(exc)` 直接就是 `explain` 的结果，所以最省事的调用方（`print(exc)`）
也能打印出有用的三行，而不是二十行栈回溯。

补上 `_ACTIONS` 完整版（正文 §11.1 贴过带省略号的开头）：

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
```

注意 `_ACTIONS` 是 `dict[str, str]`，键是**我们的 `kind`**，不是供应商的
`code`——`explain` 按 `failure.kind` 查表，查不到就只输出前两行
（"the model call failed: ..." + 尝试次数）。这就是正文 §11.1 说的
"不认识就只说事实"。

## D10 · stub 的失败注入：测试怎么"要求"失败

正文 §13.5 提了共享 stub 的问题，失败注入的实现没进正文。它是这一章
测试的地基：一个测试想测"429 两次，然后成功"，不需要真的去撞限流，
只需要在请求体里说"我要失败两次"。

**模块级状态：请求队列和请求记录。**

```python
_QUEUES: dict[str, list[dict[str, Any]]] = {}
REQUESTS: list[dict[str, Any]] = []


def reset() -> None:
    _QUEUES.clear()
    REQUESTS.clear()
```

`_QUEUES` 按 `stub_fail.id` 记队列；`REQUESTS` 记下所有请求体——F12-09
问"历史里有没有重试的痕迹"，答案是看历史；问"服务器那边是不是收到了
两次一模一样的请求"，答案是看 `REQUESTS`。`reset()` 是测试夹具的
`setup/teardown` 调用的。

**`_Handler` 的两个新方法。**

```python
class _Handler(BaseHTTPRequestHandler):
    def _next_failure(self, plan: dict[str, Any]) -> dict[str, Any] | None:
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
```

`_next_failure` 的三个细节：

1. **队列在第一次见到这个 id 时创建**，之后每次请求弹出一个。这样测试
   写的是"429 两次，然后回答"，而不是去数"这是第几次请求"。
2. **`[dict(r) for r in plan.get("responses", [])]` 是深拷贝**——否则
   `pop` 会改掉测试里那份 `plan`，第二次跑同一个测试就少了一次失败。
3. **返回 `None` 表示队列空了**，调用方照常回放录音，正好表达
   "失败用完了就成功"。

`_serve_failure` 是给 `do_POST` 用的：按 `response` 里的 `status`、
`headers`、`body` 组装一个真正的 HTTP 错误响应。注意 `headers` 循环——
`retry-after` 头就是从这里写进响应的，`_retry_after` 才能测到。

**`do_POST` 里怎么串起来：**

```python
if body.get("stub_fail"):
    failure = self._next_failure(body["stub_fail"])
    if failure is not None:
        if "stream_cut" in failure:
            cut = failure["stream_cut"]
        else:
            self._serve_failure(failure)
            return
```

`stream_cut` 是一种特殊的"失败"：status 200、发几个 chunk、然后**没有**
`[DONE]` 就断流。它必须和 HTTP 错误住在同一个队列里，因为这一章问的是
"**第二次**尝试干什么"，而第 0 章的 `stub_cut` 对每个请求都生效，
没法只对第二次生效。

## D11 · 新手常见报错/坑对照表

| 症状 | 原因 | 修法 |
|---|---|---|
| 报了 429，程序还是立刻重试并再次 429 | 没读 `retry-after`，或读了但没走预算检查 | 确认 `_http` 里 `retry_after=_retry_after(exc.headers)`，`wait_for` 里两个入口都过 `budget` |
| 一个服务器说 "retry-after: 86400"，程序睡了一整天 | 第一版只给本地退避过预算检查 | 服务器要求的时长也过 `elapsed + wait > policy.budget` |
| `wait_for` 里查了 `attempt >= policy.attempts`，测试却全绿 | 这是死代码：循环本身就有次数上限 | 删掉检查，让循环是唯一的执行点（变异测试会帮你确认） |
| `LocalProtocolError` 被重试了 | `_OUR_FAULT` 检查放在了 `httpx.TransportError` 之后 | 把 `_OUR_FAULT` 提到 `TransportError` 之前 |
| `context_length` 失败后校准比值变成 x250，之后每轮都压缩 | 没钳位就把被拒请求的 stated_tokens 喂给 `observe` | `correction > MAX_REFUSAL_CORRECTION` 时不 `observe`，记 `calibration_rejected` |
| 压完还是 400 超长，然后无限压缩 | 少了 `shrunk` 旗标或 `saving <= 0` 检查 | 一轮只压一次（`shrunk`），压不动（`saving <= 0`）直接失败 |
| `time.sleep(wait)` 之后整个程序卡死，连工具都不响应 | `time.sleep` 阻塞事件循环 | 改用 `await asyncio.sleep(wait)` |
| `ModelHTTPError` 被当作 `unexpected` 分类 | `classify` 里 `isinstance(exc, ModelHTTPError)` 分支没写到 | 确认它是第一个分支 |
| stub 的失败队列第二次跑测试就少了 | 队列直接用了测试里的 `plan` 引用 | 创建时 `[dict(r) for r in plan.get("responses", [])]` 拷贝一份 |
