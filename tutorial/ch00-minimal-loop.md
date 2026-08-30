# 第 0 章 · 让它先去查，再回答

> **代码**：`steps/step00_minimal_loop/`
> **分支**：`feat/agent-loop`
> **产出**：一个必须先读文件、才能回答问题的程序
> **你需要**：本地跑一个 [Ollama](https://ollama.com)，随便拉一个支持工具调用的模型
> （`qwen3`、`gemma4` 都行）。没有也能跟——仓库里带了真实响应的录音。

---

## §1 这一章要做出来的东西

一条命令：

```bash
minicodex ask "What does src/minicodex/__init__.py define?"
```

关键在于：**这个问题，程序不查是答不出来的。** 它必须先打开那个文件。

这一句话就是 Agent 和聊天机器人的全部区别：**它能要求外界替它做事，拿到结果，再
继续。** 后面十六章讲的所有东西——沙箱、上下文压缩、子 Agent——都是在给这个能力加码。

而这一章会撞上一个错得极其安静的地方：**它说它读了文件，其实没读。**

> **关于这一章的所有代码和输出**
>
> 每一段流、每一条报错、每一句模型说的话，都是真跑出来的。模型是本地 Ollama 上的
> `gemma4:31b`，录制于 2026-08-06。仓库里的桩服务**逐字节回放**这些响应，所以你没有
> GPU 也能跑完全章，而且跑出来和书上一模一样。
>
> 唯一是假的东西是模型的判断力。HTTP、SSE 解析、循环、工具执行，全是真的。

---

## §2 先把目标翻译成待办

沿用上一章的方法：**把目标里的每个词拆开问"这需要什么"**，然后对每一项追问两遍——
**我怎么验证？** 和 **明天/换个环境还成吗？**

| 目标里的词 | 追问 | 需要做的事 |
|---|---|---|
| 回答 | 谁来组织语言？ | 连上一个**模型** |
| 必须先读文件 | 程序怎么知道该读哪个？ | 模型得能**要求我们做事**——工具调用 |
| 先……再…… | 一次问答不够 | 一个**循环**：问 → 干活 → 把结果给它 → 再问 |
| （追问）我怎么验证它真读了？ | 它说读了就算读了吗？ | 不知道。**得能观察到工具真的被调用** |
| （追问）出问题怎么查？ | 模型是黑盒 | 不知道，**等真出问题再说** |

前三行是需求直接推出来的。第四行是追问逼出来的。

**第五行我故意留白了**，因为这才是真实情况：**在你第一次被卡住之前，你不知道自己需要
什么调试手段。** §7 会填上它。

### 一个必须现在做的决定：async

Python 的 async 有函数染色问题——`async def` 只能被 `await`，而 `await` 只能写在
`async def` 里。把同步调用链改成异步，要改的是**从入口到叶子的每一个函数和它们所有的
调用点**。

而"这个程序几乎全部时间在等 IO"不是猜测，是已知事实：等模型（秒级）、等子进程
（分钟级）、等文件（毫秒级）。

对照一下**不该现在决定**的：工具并发还是串行？不知道，而且以后改只动一个函数。

> **判断标准不是"以后会不会变"，是"以后改要动多少个地方"。**
> async 要动每一个调用点。工具调度只动一个函数。

---

## §3 先让它动起来

这一节没有设计，只有一个念头：**我想看见模型真的回我一句话。**

```python
import asyncio
import httpx

URL = "http://localhost:11434/v1/chat/completions"
TOOLS = [{"type": "function", "function": {
    "name": "read_file", "description": "Read a UTF-8 text file",
    "parameters": {"type": "object", "required": ["path"],
                   "properties": {"path": {"type": "string"}}}}}]


async def main():
    payload = {
        "model": "gemma4:31b",
        "messages": [
            {"role": "system", "content":
             "You are a coding agent. Before every tool call, first say one "
             "short sentence about what you are doing."},
            {"role": "user", "content": "What does src/minicodex/__init__.py define?"},
        ],
        "tools": TOOLS,
        "stream": True,
    }
    async with httpx.AsyncClient() as client:
        async with client.stream("POST", URL, json=payload) as resp:
            async for line in resp.aiter_lines():
                if line:
                    print(line)


asyncio.run(main())
```

不到三十行。没有类，没有抽象，就是把每一行原样打出来。

> **为什么用 Ollama 的 `/v1/chat/completions`。** 它是 OpenAI 兼容端点，本地跑不要钱，
> 而且同一份客户端代码换个 `base_url` 就能对着别家跑。Ollama 另有一套原生
> `/api/chat`，形状不同（参数直接给 dict）；两条路我都试过，都能通，选兼容层是因为
> 后面接第二家供应商时代码改动最小。

跑：

```
data: {"id":"chatcmpl-864",...,"choices":[{"index":0,"delta":{"role":"assistant","content":"I"},"finish_reason":null}]}
data: {"id":"chatcmpl-864",...,"choices":[{"index":0,"delta":{"role":"assistant","content":" will read the contents of"},"finish_reason":null}]}
data: {"id":"chatcmpl-864",...,"choices":[{"index":0,"delta":{"role":"assistant","content":" the file"},"finish_reason":null}]}
data: {"id":"chatcmpl-864",...,"choices":[{"index":0,"delta":{"role":"assistant","content":" `"},"finish_reason":null}]}
data: {"id":"chatcmpl-864",...,"choices":[{"index":0,"delta":{"role":"assistant","content":"src/minicodex"},"finish_reason":null}]}
data: {"id":"chatcmpl-864",...,"choices":[{"index":0,"delta":{"role":"assistant","content":"/__init__.py`."},"finish_reason":null}]}
data: {"id":"chatcmpl-864",...,"choices":[{"index":0,"delta":{"role":"assistant","content":"","tool_calls":[{"id":"call_rgpykfbt","index":0,"type":"function","function":{"name":"read_file","arguments":"{\"path\":\"src/minicodex/__init__.py\"}"}}]},"finish_reason":null}]}
data: {"id":"chatcmpl-864",...,"choices":[{"index":0,"delta":{"role":"assistant","content":""},"finish_reason":"tool_calls"}]}
data: [DONE]
```

**它动了。** 而且这九行里有三件事，是我坐在这儿想不出来、只能看出来的。

### 3.1 三个只能看出来的事实

**一、文本不是按 token 来的，是按任意长度的碎片来的。**

```
"I"
" will read the contents of"
" the file"
" `"
"src/minicodex"
"/__init__.py`."
```

第一片一个字母，第二片五个词。第二轮更狠——路径 `src/minicodex/prompts/system.md`
被劈成了 `"minicodex/prom"` 和 `"pts/system.md"` 两片。

> 我原本以为是一个 token 一片，还差点把这句话写进注释里。**任何一片单独看都可能是半个
> 词，所以在拼完之前不能对文本做任何判断。**

**二、工具调用是一次性给完的，不是碎片。**

```json
"function": {"name": "read_file", "arguments": "{\"path\":\"src/minicodex/__init__.py\"}"}
```

`arguments` 是一整个 JSON **字符串**，完整的。

这一条很重要，因为它决定了我**不用写**什么代码。如果参数是碎片，我就得攒起来再解析；
既然不是，那套累积逻辑就是凭空想出来的代码——正是这本书反复说不要写的那种。

> **注意这不是普遍规律，是这个供应商的行为。** 别家可能真的分片流传。等第 1 章接第二家
> 的时候我们会去测，测出来什么样就写什么样。**现在不为没见过的情况写代码。**

**三、有两个终止信号，不是一个。**

```
"finish_reason":"tool_calls"     ← 模型为什么停：它要调工具
data: [DONE]                     ← HTTP 流结束了
```

这两个是分开的，而且 `finish_reason` 在一个**内容为空的独立 chunk** 里。

一个流如果没有 `[DONE]` 就结束了，那它不是"说完了"，是**被切断了**。这个区别在 §7.3
会变成一条硬规则。

---

## §4 把文本取出来

打印原始行只能看一眼，接下来要用它。最直接的写法：

```python
async for line in resp.aiter_lines():
    if not line:
        continue
    chunk = json.loads(line.removeprefix("data: "))
    delta = chunk["choices"][0]["delta"]
    print(delta.get("content", ""), end="")
```

跑：

```
I will read the contents of the file `src/minicodex/__init__.py`.
Traceback (most recent call last):
  ...
  File "/usr/lib/python3.10/json/decoder.py", line 355, in raw_decode
    raise JSONDecodeError("Expecting value", s, err.value) from None
json.decoder.JSONDecodeError: Expecting value: line 1 column 2 (char 1)
```

**注意它先把整句话正确打出来了，然后才崩。**

这种"部分成功再崩溃"比一上来就崩难查得多——你的第一反应是"输出是对的呀，哪儿有问
题"。而且报错本身完全没提是哪一行出的事。

罪魁是 `[DONE]`。**它不是 JSON。**

```python
if not line.startswith("data: "):
    continue
payload = line[len("data: "):]

# The sentinel is not JSON.  Parsing before checking for it is the first
# thing that breaks, and it breaks *after* printing a perfectly good answer.
if payload == "[DONE]":
    ...
```

顺带一个小坑：工具调用那个 chunk 里 `content` 是 `""`，不是缺失。所以判断要用
`if delta.get("content"):`（真值判断），不能用 `if "content" in delta:`（成员判断），
否则会往文本里塞一堆空串。

### 4.1 先给流里的东西起名字

在把代码收成模块之前，得先解决一个问题：**上面这个循环产出的是什么？**

现在它什么都不产出，只是 `print`。可 §5 的循环需要拿到"这一轮模型说了什么、要调什么
工具"。最省事的写法是直接把服务端的 dict 往外扔：

```python
yield chunk["choices"][0]["delta"]     # 直接把 delta 扔出去
```

**不行，理由很具体。** 那个 dict 长这样：

```json
{"role": "assistant", "content": "", "tool_calls": [
    {"id": "call_x", "index": 0, "type": "function",
     "function": {"name": "read_file", "arguments": "{...}"}}]}
```

调用方拿到它之后，每次想知道"这片是文本还是工具调用"，都得写
`if d.get("content"): ... elif d.get("tool_calls"): ...`，还得记住 `content` 是空串
不是 None、`name` 埋在 `function` 里面两层。**这些知识会从这个文件漏到每一个用它的
地方**，而且没有任何东西提醒你漏了。

所以在这里立一道边界：**服务端的 JSON 到此为止，往外走的是我自己定义的类型。**

> 这是 Ch-1 那三条抽象例外里的第二条：**跨越信任边界的地方必须有一层。**
> 模型返回的是任意 JSON，字段可能缺、可能多、可能换名字。把它翻译成本地类型，
> 翻译失败就在这一个函数里失败，不会渗进循环、渗进历史、渗进测试。

那定几个类型？**照着 §3.1 那三个观察来，一个观察一个类型：**

| §3.1 的观察 | 对应的类型 |
|---|---|
| 文本是任意长度的碎片，必须先拼 | `TextDelta` |
| 工具调用是完整的，一次给完 | `ToolCallDelta` |
| 有两个终止信号，`[DONE]` 才是流真的结束 | `Completed` |

```python
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
```

**逐个说清楚为什么长这样。**

**为什么是三个类而不是一个带可选字段的类。** 一个类的话会长成
`Event(text=None, call=None, done=False)`，调用方还是得靠判空来区分，而且能构造出
`Event(text="hi", done=True)` 这种没有意义的组合。三个类让**非法状态无法表示**——
你拿到一个 `TextDelta`，它就一定有 `text`，不可能同时是终止信号。

**为什么 `frozen=True`。** 冻结之后实例不可改。这些是"已经发生的事实"——服务端已经把
这片字节发过来了，任何代码都不该有机会改它。而且冻结的对象可以放进 set、做字典的键、
在测试里直接比较相等（后面 `assert done == [Completed("tool_calls")]` 靠的就是
dataclass 自动生成的 `__eq__`）。

**为什么 `TextDelta` 只有一个字段还要包一层。** 直接 `yield "I"` 也能跑。但那样
`async for` 的循环体里就只能靠 `isinstance(event, str)` 来判断类型，而字符串是个太
通用的类型——将来任何别的东西也可能是字符串。**包一层的成本是三行，收益是类型检查器能
替你穷举所有分支。**

**为什么 `ToolCallDelta` 同时要 `call_id` 和 `index`。** 两个都来自服务端，用途不同：
`call_id`（`call_rgpykfbt`）是**给对方看的**，下一轮回传结果时要用它配对；`index`
（0/1/2）是**给我看的**，多个调用时用来排顺序。§8.1 会用到 index，§8.1 的不变量测试
会用到 call_id。

**为什么叫 `Delta` 而它其实不是碎片。** 因为 wire 上那个字段就叫 `delta`
（`choices[0].delta.tool_calls`）。**和协议保持一致，胜过一个孤立看更准确的名字**——
否则读代码的人要在两套词汇之间做翻译。docstring 里明写了这个别扭之处。这一条在
§11 的 code review 里被人提出来了，我的回答也是这个。

**`StreamEvent` 这个联合类型是干嘛的。** 它就是 `TextDelta | ToolCallDelta | Completed`
的别名，用来标注"`stream()` 会吐出这三种之一"。写了它之后，`_collect` 里如果漏处理一
个分支，类型检查器会指出来。**这是 Ch-1 里说"写 Agent 特别需要类型"的第一次兑现：
模型返回任意 JSON，而这个联合类型是那堆任意性唯一被钉死的地方。**

### 4.2 收工：`model.py` 的完整样子

把 §3 的三十行、§4 的修补、上面三个类型收成一个模块。这是
`src/minicodex/model.py` 的**全部内容**，一百五十六行：

```python
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
```

三处值得单说：

**`request_body()` 单独成方法。** 它不发请求，只返回将要 POST 的那个 dict。现在没人用
它，看着像多余的一层。加它的理由很具体：**第 6 章要 diff 两次请求体看压缩改了什么，
第 13 章要给它做快照测试。** 两处都需要"拿到请求体但不发网络请求"，而如果它埋在
`stream()` 里，就只能靠 mock 掉 httpx 才能看见。

一行方法换掉两章的麻烦，这是"留缝不留抽象"的典型：**没有定接口，只是把可能被单独需要
的东西拿出来放好。**

**`call_id=raw.get("id") or f"call_{raw.get('index', 0)}"`。** 观测到的响应每次都带
`id`，但文档里原生 API 的示例不带。既然两边说法不一致，就给个兜底——用 index 编一个。
**这不是"为将来准备"，是对着一个已知的不确定性做防御。**

**`ModelHTTPError` 单独定义。** 现在只是把非 200 的响应体截 500 字符扔出来。第 12 章
讲重试时，"哪些错该重试"的分类会长在这个类型上。

**`stream` 是个 async generator，不是返回列表的函数。** 注意它用的是 `yield` 而不是
`return [...]`。区别在于：`yield` 版本**每收到一片就交出去一片**，调用方可以边收边处理
（比如打字机效果地打印出来）；返回列表的版本要等整个响应收完才返回。现在的循环并不需要
这个能力——`_collect` 反正要等全部收完。留着 `yield` 是因为**它不比返回列表更贵，却保住
了一个以后可能想要的能力**：这又是"留缝"，不是"留抽象"。

---

## §5 组装和循环

新文件 `src/minicodex/agent.py`。

§4 那个 `stream()` 吐出来的是一串**碎片**：十几个 `TextDelta`、一个 `ToolCallDelta`、
一个 `Completed`。而循环需要的是**一整轮**：这轮模型说了什么、要调哪些工具。

中间缺一步组装。这一节做两件事：定义"一整轮"长什么样，然后写那个组装函数和循环。

### 5.1 三个装数据的类型

```python
@dataclass(frozen=True)
class ToolCall:
    call_id: str
    name: str
    arguments: dict[str, Any] | None  # None when the model emitted invalid JSON
    raw_arguments: str


@dataclass(frozen=True)
class ModelTurn:
    text: str
    tool_calls: tuple[ToolCall, ...]
    finish_reason: str | None = None


@dataclass
class RunResult:
    final_text: str
    stop_reason: str  # "completed" | "turn_limit"
    turns_used: int
    history: list[dict[str, Any]] = field(default_factory=list)
```

三个类型，三个不同的"一次"：

| 类型 | 装的是 | 谁产出它 | 谁消费它 |
|---|---|---|---|
| `ToolCall` | **一个**工具调用（可用的形式） | `_collect` | 循环，拿去执行 |
| `ModelTurn` | **一轮**完整响应 | `_collect` | 循环，拿去决定下一步 |
| `RunResult` | **一整次** `run()` 的结果 | `run()` | 调用方（CLI、测试） |

**`ToolCall` 和 §4 的 `ToolCallDelta` 有什么区别？** 一个是 wire 上的，一个是能用的。
`ToolCallDelta.arguments` 是**字符串** `'{"path":"a.py"}'`，而工具函数要的是
**dict** `{"path": "a.py"}`。中间隔着一次 `json.loads`，而那次解析**可能失败**。

所以 `ToolCall` 才有两个参数字段：

```python
arguments: dict[str, Any] | None   # 解析成功的结果，失败时是 None
raw_arguments: str                 # 模型原样发的那个字符串
```

**为什么解析失败还要留着原文？** 因为失败的时候我要告诉模型"你发的这个不是合法 JSON"，
而要说清楚就得把**它实际发了什么**回显出来。解析一失败，`arguments` 就成了 `None`，
原文没别处可找。§8.4 会用到这个字段。

> **这个字段是被错误信息的需求逼出来的，不是设计出来的。** "为了能说清楚出了什么事，
> 得多留一份原始数据"——这个模式后面还会遇到好几次。

**`ModelTurn` 为什么要 `finish_reason`？** 它现在没人用。留着是因为 §3.1 观察到它是个
**独立的信号**（模型为什么停：说完了？要调工具？还是被截断了？），扔掉的话以后想用就得
回头改 `_collect` 的签名。**留一个已经拿到手的字段，成本是一行。**

**`RunResult` 为什么不直接返回一个字符串？** 因为调用方需要知道的不只是答案：
`stop_reason` 区分"正常答完"和"撞上轮次上限"（§8.5 会用到），`turns_used` 用来观察
效率，`history` 是测试断言的对象——§6 那个"文件到底读没读"的判断就是查 history 里有没有
`role: tool` 的条目。

**为什么 `RunResult` 没有 `frozen=True`，另外两个有？** 因为 §4 那三个和这里的
`ToolCall`/`ModelTurn` 都是"已经发生的事实"，不该被改；而 `RunResult` 是给调用方的
返回值，冻结它没有收益，反而挡住"拿到之后补个字段再往下传"这类正常用法。

**`tuple[ToolCall, ...]` 为什么是元组不是列表？** 同一个理由：`ModelTurn` 是 frozen 的，
但如果里面装一个 `list`，别人照样能 `turn.tool_calls.append(...)` 把它改了。
**元组让"不可变"这件事一路到底。**

### 5.2 一个 Protocol

现在**确实有两个模型实现**：测试要一个说话可预测的，用户要真的那个。所以需要一个类型来
表达"这两个可以互换"。

```python
class Model(Protocol):
    """Anything that can stream a response given a history.

    A Protocol rather than a base class: structural typing, no inheritance, so
    the cost of the abstraction is close to zero.  It earns its place because
    swapping the model is a requirement today -- the tests need a deterministic
    one -- not a guess about tomorrow.
    """

    def stream(self, history: Sequence[dict[str, Any]]) -> AsyncIterator[StreamEvent]: ...
```

**`Protocol` 和基类的区别，以及为什么这里选它。** 如果写成基类，`OllamaModel` 就得
`class OllamaModel(Model):` 显式继承；而 `Protocol` 是**结构化类型**——任何有一个
`stream(history)` 方法的对象都自动满足它，不需要继承、不需要 import、不需要知道
`Model` 存在。

具体的好处在 §8.5 会看到：那个测试里我临时写了个十行的包装类，什么都没继承，直接就能
传给 `Agent`。

**为什么现在就值得定它。** 按 Ch-1 的规则，抽象默认不做。这条落在第一个例外里——
**变化是需求本身**：不是"以后可能想换模型"，是**今天就有两个实现，而且必须都能跑**。

方法体是 `...`（字面量的省略号），因为 Protocol 只声明形状，不提供实现。

### 5.3 组装

现在写那个把碎片变成 `ModelTurn` 的函数。因为 §3.1 那两条观察，它天然要**两个累加器**：

```python
async def _collect(self, stream: AsyncIterator[StreamEvent]) -> ModelTurn:
    text_parts: list[str] = []
    by_index: dict[int, ToolCallDelta] = {}
    completed: Completed | None = None

    async for event in stream:
        if isinstance(event, TextDelta):
            text_parts.append(event.text)
        elif isinstance(event, ToolCallDelta):
            by_index[event.index] = event
        elif isinstance(event, Completed):
            completed = event

    calls = []
    for index in sorted(by_index):
        raw = by_index[index]
        try:
            parsed = json.loads(raw.arguments) if raw.arguments else {}
            arguments = parsed if isinstance(parsed, dict) else None
        except json.JSONDecodeError:
            arguments = None
        calls.append(ToolCall(raw.call_id, raw.name, arguments, raw.arguments))

    return ModelTurn("".join(text_parts), tuple(calls), completed.reason)
```

逐段说：

**`text_parts` 是列表，最后 `"".join()`。** 不写成 `text += event.text`，因为 Python
的字符串不可变，每次 `+=` 都要复制一遍整个已有内容——十几片无所谓，几千片就是明显的
开销。先收集再一次 join 是标准写法。

**`by_index` 是字典不是列表。** 键是服务端给的 `index`。用字典而不是
`calls.append(event)` 的理由：如果某个供应商真的把一个调用分成几片发（每片带同一个
index），字典至少是**同一个键被覆盖**，而列表会变成**三个残缺的调用**。

> 这行在 §11 的 code review 里被质疑了：覆盖也是丢数据。**是的，而且我没修**——
> 观测到的行为是一次给完，为一个没见过的行为写累积逻辑就是凭空臆想。理由和去向都写在
> 那一节。

**`completed` 初始是 `None`。** 现在这个变量只是被赋值，没人读它。§8.2 会读——
"它还是 `None` 吗"就是"流有没有被切断"的判断。**留着它是因为 `[DONE]` 那个事实已经
在 §3.1 观察到了，只是还不知道拿它干嘛。**

**`sorted(by_index)`** 按 index 排序，而不是按到达顺序。这一行是防御性的，而且
§9 的变异验证证明了**目前没有测试能抓住它**——录音里本来就是有序的。我保留它并在
§8.1 说明了理由。

**参数在这里解析，不在工具里解析。** 因为解析失败是**协议层面**的问题（模型发了坏
JSON），不是业务问题。让每个工具各自 `json.loads` 一遍，就等于把同一个错误处理复制
到每个工具里。

**`isinstance(parsed, dict)` 这道检查是干嘛的。** `json.loads("[1,2]")` 不会抛异常，
它成功返回一个列表。但工具函数签名要的是 `dict[str, Any]`。**合法 JSON ≠ 我要的
形状**，所以解析成功之后还要再判一次类型。

§8.2 会给这个函数加一道检查——现在还不知道要加什么。

### 5.4 循环

最后是 `Agent` 类本身。

**为什么是个类而不是一个函数？** 因为循环每一轮都要用到同一批东西：模型、工具表、轮次
上限、录制器。写成函数的话这四个参数要一路往下传；写成类，它们在 `__init__` 里存一次。
**这不是"面向对象设计"，是省掉四个反复出现的参数。**

```python
DEFAULT_MAX_TURNS = 12


class Agent:
    def __init__(
        self,
        model: Model,
        tools: dict[str, ToolFn] | None = None,
        *,
        max_turns: int = DEFAULT_MAX_TURNS,
        recorder: Recorder = NULL_RECORDER,
    ) -> None:
        self.model = model
        self.tools = tools or {}
        self.max_turns = max_turns
        self.recorder = recorder

    async def run(self, user_message: str) -> RunResult:
        history: list[dict[str, Any]] = [{"role": "user", "content": user_message}]
        final_text = ""

        for turn_index in range(self.max_turns):
            turn = await self._collect(self.model.stream(history))

            history.append(
                {
                    "role": "assistant",
                    "content": turn.text,
                    "tool_calls": [
                        {
                            "id": c.call_id,
                            "type": "function",
                            "function": {"name": c.name, "arguments": c.raw_arguments},
                        }
                        for c in turn.tool_calls
                    ],
                }
            )
            if turn.text:
                final_text = turn.text

            if turn.text:                       # ← 停止条件
                return RunResult(final_text, "completed", turn_index + 1, history)

            for call in turn.tool_calls:
                output = await self.tools[call.name](call.arguments)
                history.append(
                    {"role": "tool", "tool_call_id": call.call_id, "content": output}
                )

        return RunResult(final_text, "turn_limit", self.max_turns, history)
```

问模型 → 它说话了就结束 → 它要工具就跑，把结果塞回历史 → 再问。

**这就是 Agent。** 剩下十六章都是在处理这个循环出错的各种方式。

几个细节：

**`history` 从一条 user 消息开始，然后只增不减。** 每轮往里追加：模型说的话（assistant）
和工具的结果（tool）。**下一轮把整个 history 重新发一遍**——模型自己不记得任何事，
"上下文"就是这个列表。第 6 章讲上下文压缩，处理的就是这个列表长到装不下的时候。

**`final_text` 单独存一份。** 因为 `turn.text` 每轮都会被覆盖，而最后一轮**可能是空
的**（模型只调工具不说话）。存一份"最后一次非空的文本"，才有东西还给用户。

**`turn_index + 1` 而不是 `turn_index`。** `range` 从 0 开始，但用户想看的是"用了 2
轮"不是"用了第 1 轮"。这种一眼看不出对错的地方，正是测试该钉住的
（§6 那个 `assert result.turns_used == 2`）。

**`max_turns` 有默认值 12，且可以覆盖。** 默认值给正常使用，覆盖是为了测试——§8.5 那个
"循环永不结束"的测试用的是 `max_turns=5`，不然它得跑满 12 轮才结束。**一个能被测试
调小的上限，比一个写死的常量好测得多。**

这一版有四个地方是错的，我一个都还不知道。§6 找出第一个，§8 找出另外三个。

> **`history` 是 `list[dict]`。** 我知道不好。但现在还看不出它该长什么样——只有一处代码
> 往里塞东西。**这时候定一套类型，定出来的一定是猜的。** 第 1 章会有具体理由逼我改它。
>
> 注意历史里 assistant 那条的形状是**照抄请求格式的**（`tool_calls` 里嵌
> `function.arguments` 字符串）。不是我设计的，是因为下一轮要把整个 history 原样发回
> 去，所以它必须长成服务端认识的样子。**协议渗透进了数据结构**——这正是第 1 章要处理的
> 事。

---

## §6 我怎么知道它真的读了

§2 清单里那个看起来像抬杠的追问。

跑一下 `ask`，输出很漂亮：

```
`src/minicodex/__init__.py` defines the following:

- **`__version__`**: The current version of the package (`0.0.1`).
- **`system_prompt()`**: A function that reads and returns the agent's system prompt
  from a file located at `src/minicodex/prompts/system.md`.
- **`__all__`**: An export list containing `__version__` and `system_prompt`.
```

答得完全正确。但**我不知道这是读出来的，还是猜出来的**——毕竟这个文件长得很常规，模型
完全可能瞎编一个像样的答案。

所以给工具加一行日志，只看它被调了没有：

```python
async def read_file(args):
    log.append(f"read_file:{args.get('path')}")
    return ...
```

用同一份录音跑两版循环：

```
--- A. 停止条件 = 有文本 ---
    返回: 'I will read the contents of the file `src/minicodex/__init__.py`.'
    工具日志: []

--- B. 停止条件 = 没有工具调用 ---
    返回: '`src/minicodex/__init__.py` defines the following: ...'
    工具日志: ['read_file:src/minicodex/__init__.py']
```

**A 的工具日志是空的。文件从来没被打开过。**

而 A 返回的那句话——"我这就去读 `src/minicodex/__init__.py` 的内容"——是模型真说的，
语法正确，语气笃定，退出码 0。

用户拿到的是**一个自信的非答案**。

### 6.1 根因

罪魁是 §5 那行：

```python
if turn.text:      # 模型说话了就算完事
```

而 §3 的录音里，模型**在同一个响应里先说了一句，再调工具**。这不是巧合——系统提示里
就要求它这么做，而真实的编码 Agent 几乎都会这么要求，因为用户想看见它在干嘛。

### 6.2 修

```python
# Text is not a stop signal.  Models narrate before acting, and stopping on
# the narration leaves the work undone while looking exactly like success.
if not turn.tool_calls:
    return RunResult(final_text, "completed", turn_index + 1, history)
```

**停止信号是"没有工具调用"，不是"有文本"。** 一个词的改动，语义完全相反。

注释是这时候补的。理由：这行字面上完全看不出为什么不能写 `if turn.text`——**一行反直觉
的代码如果不解释，下一个人会"顺手修好"它。**

### 6.3 这类 bug 的形状

- **不报错。** 没有栈回溯，没有非零退出码。
- **结果看起来合理。** 旁白本来就是通顺的句子。
- **只有对比"它说的"和"它做的"才能发现。**

我不是"发现"它的，我是**去找**它的。方法就是那一行 `log`。

> **一条可以带走的规则**：**永远不要相信模型对自己行为的描述。**
> 断言可观测的副作用——哪个工具被调了、传了什么参数、文件是不是真的变了。
> 第 14 章会把这条扩展成整套测试策略的地基。

---

## §7 但我复现不了

修完 §6，我想顺着往下查——**这行 `if turn.text` 是我凭直觉写的，直觉在这里错了，那我
还有哪些地方是凭直觉写的？**

于是问题来了：**每查一次，就要真调一次模型。**

一次两三秒，云端模型还要花钱。更麻烦的是这个：

```
$ 同一个问题问五遍
run 1: The sky is blue because shorter blue wavelengths of sunlight are scattered in all directions by the gases and particles in Earth's atmosphere more than other colors.
run 2: The sky appears blue because shorter blue wavelengths of sunlight are scattered in all directions by the gases and particles in Earth's atmosphere more than other colors.
run 3: The sky is blue because shorter blue wavelengths of sunlight are scattered in all directions by the gases and particles in Earth's atmosphere more than other colors.
run 4: The sky is blue because shorter blue wavelengths of sunlight are scattered in all directions by the gases and particles in Earth's atmosphere more than other colors.
run 5: The sky is blue because shorter blue wavelengths of sunlight are scattered in all directions by the gases in Earth's atmosphere more than other colors.
```

五次里四次逐字相同。第五次少了 "and particles"。

**这比"每次都不一样"更糟。**

如果每次都变，你第一天就知道必须想办法固定它。而"大部分时候一样、偶尔不一样"意味
着——**你改完一个 bug 跑一遍通过了，你会以为自己修好了，其实只是这次运气好。**

run 5 那个差异如果发生在工具参数上，就是一次没人察觉的行为变化。

### 7.1 先把发生过的事记下来

在解决"复现"之前，得先解决一个更基础的问题：**我根本不知道模型收到了什么。**

终端里只有模型说的话，而这一章刚证明了那东西不可信。我需要看的是**那一刻发给它的完整
历史**——而这个东西进程一退就没了。

最笨的版本，八行：

```python
class Recorder:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def record(self, kind, payload):
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({"kind": kind, "payload": payload}) + "\n")
```

够用了。它后来长出四处东西，每处都有具体来由：

| 长出来的 | 因为 |
|---|---|
| `fh.flush()` + `os.fsync()` | Ctrl-C 掉一次跑到一半的会话，去看文件——最后几条不在里面。而**这份记录的全部价值就是崩溃之后仍然完整**，丢的恰好是最想看的那几条 |
| 逐行读，坏行即边界 | 我手贱在文件末尾补了半行模拟被 kill 的进程，结果整个文件读不出来了——可前面那些完整的行明明是好的 |
| 键名脱敏 | 打算把一份记录贴进这本书当例子，贴之前扫了一眼，看见了环境变量 |
| `except OSError: pass` | 磁盘满的时候它抛异常，把 Agent 一起带走了 |

最后一条值得多说一句：**一个能把被调试程序搞崩的调试工具，比没有更糟。**

> 顺带解释为什么是 JSONL 而不是一个 JSON 数组：被 kill 的进程留下的半个 JSON 文档完全
> 不可解析；半个 JSONL 文件是**可解析的前缀加一行垃圾**。第 7 章会把这条性质变成整个
> 崩溃恢复机制的地基。现在它只是那个 `break` 的理由。

脱敏的局限我写在 docstring 里了，没藏着：只看**键名**，藏在普通字段值里的密钥漏网。
修它需要给每家供应商的密钥格式写正则、误伤正常代码、还会破坏这个工具存在的意义。
**承认、记录、不修，也是一种合法的结局**——前提是写在未来的人会看到的地方。

长完之后是这样，`src/minicodex/recorder.py` 的全部内容：

```python
"""A written record of everything that crosses the model boundary.

The first question when an agent misbehaves is always "what did the model
actually receive", and it is unanswerable after the process exits: the history
lived in memory and the memory is gone.  So it gets written down as it happens.

Deliberately dumb.  Append-only JSONL, one event per line, and it never raises
into its caller -- a debugging aid that can crash the program it exists to
debug is worse than no debugging aid at all.
"""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import Any

DEFAULT_DIR = Path(".minicodex") / "recordings"

# Recordings get pasted into bug reports.  Values under these keys never reach
# disk.  Key-name based, so a credential hidden inside an innocent-looking
# value still gets through; that limit is documented rather than hidden.
_REDACTED_KEYS = frozenset({"api_key", "authorization", "token", "secret", "password"})

_REDACTED = "<redacted>"


def _redact(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            k: (_REDACTED if k.lower() in _REDACTED_KEYS else _redact(v))
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [_redact(v) for v in value]
    return value


class Recorder:
    """Append-only JSONL sink for model-boundary events.

    Not a logger.  Logs are prose for a human watching a terminal; this is a
    machine-readable transcript that chapter 14 replays to turn an intermittent
    failure into a deterministic test.
    """

    def __init__(self, path: Path | None = None, *, enabled: bool = True) -> None:
        self.enabled = enabled
        self._seq = 0
        self._lock = threading.Lock()
        if path is None:
            path = DEFAULT_DIR / f"session-{int(time.time())}.jsonl"
        self.path = Path(path)
        if self.enabled:
            self.path.parent.mkdir(parents=True, exist_ok=True)

    def record(self, kind: str, payload: dict[str, Any]) -> int | None:
        """Append one event.  Returns its sequence number, None if disabled."""
        if not self.enabled:
            return None
        with self._lock:
            self._seq += 1
            seq = self._seq
            event = {"seq": seq, "ts": time.time(), "kind": kind, "payload": _redact(payload)}
            try:
                with self.path.open("a", encoding="utf-8") as fh:
                    fh.write(json.dumps(event, ensure_ascii=False) + "\n")
                    fh.flush()
                    # The transcript is worth having *because* the process
                    # crashed.  Buffered writes lose the last few events, which
                    # are the ones worth reading.
                    os.fsync(fh.fileno())
            except OSError:
                return seq
        return seq

    def read_all(self) -> list[dict[str, Any]]:
        """Read the transcript back, tolerating a truncated final line.

        A process killed mid-write leaves half a JSON object behind.  One
        document per line means the surviving prefix is still readable; chapter
        7 turns that property into the whole recovery mechanism.
        """
        if not self.path.exists():
            return []
        events: list[dict[str, Any]] = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                break
        return events


NULL_RECORDER = Recorder(path=Path(os.devnull), enabled=False)
```

八行长到九十九行。除了上面那四条，还多出三样没在表里的：

**`seq` 和 `ts`。** 第一次翻这个文件找"第 23 轮"的时候，发现只能靠数行。加个序号和
时间戳，一行成本。

**`threading.Lock`。** 严格说现在用不上——只有一个协程在写。加它是因为
`os.fsync` 那段是**读-改-写**（`self._seq += 1` 然后写文件），而第 8 章一并发就会有两个
调用者。这一条我承认是防御性的，理由是**它现在是对的，将来也是对的，代价一行**。

**`NULL_RECORDER`。** 一个 null object，省掉调用点上到处的 `if recorder is not None`。
它不是"为将来做准备"，是**现在就在解决一个具体的可读性问题**——这就是"留缝"和"留抽象"
的区别。

现在跑一次 `ask`，看它记了什么：

```
1 request  {"turn": 0, "history": [{"role": "user", "content": "What does src/minicodex/__init__.py define?"}]}
2 response {"turn": 0, "text": "I will read the contents of the file `src/minicodex/__init__.py`.", "finish_reason": "tool_calls", ...
3 request  {"turn": 1, "history": [{"role": "user", ...}, {"role": "assistant", ...
4 response {"turn": 1, "text": "`src/minicodex/__init__.py` defines the following:\n\n- **`__version__`**: ...
```

第 3 行就是全书后面最常盯的东西：**第二轮请求里，模型收到的完整历史。**

### 7.2 然后：把记下来的东西播回去

有了录音，"复现"就不再是运气问题了——**把那次真实响应原样播一遍就行。**

```python
# Recorded 2026-08-06 against gemma4:31b, asked "What does
# src/minicodex/__init__.py define?" with a system prompt telling it to say one
# sentence before each tool call.  Note the chunk sizes: prose does not arrive
# one token at a time, it arrives in whatever slices the server felt like.
NARRATE_THEN_CALL = [
    *[_text("chatcmpl-864", w) for w in [
        "I",
        " will read the contents of",
        " the file",
        " `",
        "src/minicodex",
        "/__init__.py`.",
    ]],
    _call("chatcmpl-864", "call_rgpykfbt", 0, "read_file",
          '{"path":"src/minicodex/__init__.py"}'),
    _finish("chatcmpl-864", "tool_calls"),
]
```

这不是我编的脚本，是 §3 那次真实响应的**逐字节拷贝**，连 `call_rgpykfbt` 这个 id 都是
原来的。

它被包在一个几十行的 HTTP 服务里，测试通过**真实的 socket** 连它：

```python
@pytest.fixture(scope="session")
def stub_url() -> Iterator[str]:
    """A local server replaying the recorded Ollama responses.

    Real HTTP over a real socket: the client's SSE parsing, status handling and
    connection teardown are all exercised.  Only the model's judgement is fake.
    """
```

**为什么要走真 socket，而不是直接 mock 掉 `httpx`？** 因为我想测的恰恰是 §4 那种坑——
`[DONE]` 不是 JSON、`content` 是空串、SSE 的 `data: ` 前缀。mock 掉 httpx 就把这些全
绕过去了，剩下的测试只能证明"我的假数据和我的解析器互相同意"。

> **假模型不是这一章的规划，是这一章的调查工具。** 我先撞见问题，发现查不动，才去
> 造它。顺序反过来的话，你会在还不知道要测什么的时候先花半小时写测试脚手架。
>
> 这也是 codex 自己的做法：`codex-rs/core/tests/suite/` 下 80 多个测试文件全部跑在一个
> 假的 responses server 上。第 14 章细讲。

代价也得说清楚：**录音会过期。** Ollama 换个版本、模型换一版，wire format 可能就变了，
而你的测试全绿——因为它们在跟一份 2026 年 8 月的化石对话。第 14 章会讲怎么定期用真模型
重新录制来对冲这一点。

---

## §8 顺着查：还有什么在骗我

有了确定性回放，现在可以系统地过一遍**所有我凭直觉写的地方**：问"如果现实不像我想的
那样呢"。

### 8.1 我假设了模型一次只要一件事

用另一次真实探测的录音——那次我一口气要了三个结果：

```
index 0  call_cz5zx7jy  get_temperature(New York)
index 1  call_3zvh467n  get_conditions(New York)
index 2  call_zfz547ah  get_temperature(London)
```

三个独立 chunk，三个不同的 id。而我的循环……写的是 `calls[0]`。

```python
await _naive_run(model(stub_url, stub_mode="three"), make_tools(log))
assert log == ["get_temperature:New York"], "two calls were dropped without a word"
```

丢掉两个调用已经够糟。但**真正的伤害在下一轮**：模型发了三个调用，历史里只有一个结果。
另外两个成了**没人应答的调用**。

那么下一次请求会怎样？**对着我们现在用的 Ollama，什么都不会发生——它照样返回 200。**

```
$ 故意发一个有 tool_calls 没有对应结果的历史给 Ollama
HTTP 200
data: {..."delta":{"content":" symbiotic"}...}
data: {..."delta":{"content":" with"}...}
data: {..."delta":{"content":" the"}...}
data: {..."delta":{"content":" user"}...}
data: {..."delta":{"content":"'"}...}
data: {..."delta":{"content":"s"}...}
data: {..."delta":{"content":" request"}...}
```

"symbiotic with the user's request"。**历史坏了，服务端不管，模型自己开始胡言乱语。**

而同一个历史发给 OpenAI：

```
HTTP 400
{
  "error": {
    "message": "An assistant message with 'tool_calls' must be followed by tool messages responding to each 'tool_call_id'. The following tool_call_ids did not have response messages: call_probe01",
    "type": "invalid_request_error",
    "param": "messages",
    "code": null
  }
}
```

**同一个 bug，两种命运。** 宽松的供应商让你一路绿灯开发，严格的那个在上线时给你一屏
400。第 1 章整章都在处理这件事。

**症状和病灶隔了一轮**——你在第 N+1 轮看到 400，bug 在第 N 轮。这是 Agent 调试里最费
时间的一类问题。

> **一个坦白。** 这段 OpenAI 报错，本书初稿里我是**凭记忆写的**，只有前半句，而且从没
> 验证过——这违反了本书自己的规矩（外部 API 行为必须实测）。写第 1 章时补测了，结果是：
> 前半句一字不差，但我**漏掉了最有用的后半句**——`The following tool_call_ids did not
> have response messages: call_probe01`，它直接告诉你是哪个 id 出的事。
>
> 前半句蒙对是运气，不是本事。**留着这条坦白，是因为"我记得 API 是这么说的"是每个人都
> 会犯的错，而它恰好是最不该犯的那一类。**

修法直白：每个调用都跑，每个都产出结果。然后把规则本身钉住：

```python
async def test_F00_03_outputs_pair_one_to_one_with_calls(stub_url: str) -> None:
    """The invariant chapter 1 turns into a type, asserted by hand for now."""
    issued = [c["id"] for i in result.history if i["role"] == "assistant" for c in i["tool_calls"]]
    answered = [i["tool_call_id"] for i in result.history if i["role"] == "tool"]
    assert issued == answered
```

> 那句 docstring 是给第 1 章留的路标。**"每个调用恰好一个结果"是一条不变量**，而现在
> 它靠一个手写断言维持。手写断言不 scale——每多一处修改历史的代码，就多一个忘记维护它
> 的机会。

**顺带一个诚实的坦白。** 我在组装时写了 `for index in sorted(by_index)`，按 index 排序
而不是按到达顺序。做变异验证时（§9），把它改成 `list(by_index)` **测试照样全绿**——因为
录音里三个调用本来就是按 0/1/2 到达的。

这行排序是**防御性的，目前不是承重的**。它防的是一个我没观测到的情况，代价一行。哪天
真观测到乱序，那时才会有对应的测试。**假装它被验证过就是骗人。**

### 8.2 我假设了流会正常结束

`async for` 跑完就算完。网络不这么认为。

§3.1 那个观察在这里兑现了——**`finish_reason` 和 `[DONE]` 是两个信号**。流如果在
`[DONE]` 之前断掉，你可能拿到一个完整的工具调用，却不知道模型是不是还想说别的、还想
调别的。

```python
if completed is None:
    raise IncompleteStreamError(
        f"stream ended after {len(text_parts)} text chunk(s) and "
        f"{len(by_index)} tool call(s) without a [DONE] sentinel"
    )
```

关键不在这个 `raise`，在于**所有碎片都攒在函数的局部变量里**。抛异常 → 局部变量随栈
销毁 → **历史一个字节都没被污染**。

这不是"加了个检查"，是**结构上的保证**：半成品根本没有渠道能到达历史。

```python
async def test_F00_04_nothing_from_a_cut_stream_is_executed(stub_url: str) -> None:
    """The dangerous version does not crash.  It commits a partial turn and
    fails on the next request, somewhere else entirely."""
    ...
    assert log == [], "a call from an unfinished stream must never run"
```

最后那句断言值得想一下：**如果这是 `apply_patch` 而不是 `read_file` 呢？**

### 8.3 我假设了模型只调存在的工具

模型会调 `read_fileZ`、`readFile`、`read_files`。训练数据里有别的工具、描述写得不清楚、
上下文太长记混了——原因很多。

`tools[name]` → `KeyError` → 会话结束。

### 8.4 我假设了工具不会失败

磁盘满、文件是二进制、权限不够。异常一路冒泡出 `run()`，Agent 死了。

8.3 和 8.4 是同一个问题，一条规则解决。原来循环里那句
`await self.tools[call.name](call.arguments)` 抽成一个方法：

```python
    async def _run_tool(self, call: ToolCall) -> str:
        """Execute one call.  Always returns text; never raises.

        One rule covers every way this goes wrong: whatever happened inside a
        tool is information the model needs, so it comes back as output.  A
        raised exception ends the session; a returned error lets the model try
        something else.

        Each message says three things -- what went wrong, what is available,
        and what to do next.  The model reads these and acts on them, so they
        are prompts whether or not anyone calls them that.
        """
        if call.name not in self.tools:
            available = ", ".join(sorted(self.tools)) or "(none)"
            return (
                f"Error: no tool named {call.name!r}. "
                f"Available tools: {available}. "
                "Call one of those instead."
            )

        if call.arguments is None:
            return (
                f"Error: arguments for {call.name!r} were not valid JSON. "
                f"Received: {call.raw_arguments[:200]!r}. "
                "Send a single JSON object."
            )

        try:
            return await self.tools[call.name](call.arguments)
        except Exception as exc:  # deliberately broad; see the docstring
            return f"Error: {call.name} raised {type(exc).__name__}: {exc}"
```

站在模型角度想就很清楚：**工具失败是一条信息，不是一场灾难。** 模型完全有能力读到
"这个文件不存在"然后换个路径。它没有能力处理"你的进程已经退出了"。

三条错误信息长这样：

```
Error: no tool named 'read_fileZ'. Available tools: read_file. Call one of those instead.
Error: arguments for 'read_file' were not valid JSON. Received: '{not json'. Send a single JSON object.
Error: read_file raised PermissionError: [Errno 13] Permission denied: 'x'
```

前两条是三段式：**出了什么错 · 你能用什么 · 下一步做什么。** 第三条只有一段，因为异常
是什么类型我事先不知道，编不出"下一步"。

> **注意 `raw_arguments` 在这里兑现了**（§5 定 `ToolCall` 时留的那个字段）。要告诉模型
> "你发的东西不是合法 JSON"，就得把它发的东西回显出来——而解析一失败，那个字符串在
> `arguments` 里就变成 `None` 了。**为了能说清楚出了什么事，得多留一份原始数据。**
> 这个模式后面还会遇到好几次。

那句 `except Exception` 通常是坏味道，这里不是——所以注释写明了理由。

> **一条会贯穿全书的原则**：**工具的错误信息就是 prompt。** 一条只说"什么坏了"的错误
> 让模型原样重试；一条说了"该怎么办"的错误让它一次就对。第 3 章整章展开。

顺便把工具本身也补全。`src/minicodex/tools.py`，全部内容：

```python
"""The tools the agent may call, and the schema the model is shown."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any


def _read(path: Path) -> str:
    if not path.exists():
        return f"Error: {path} does not exist. Check the path and try again."
    if path.is_dir():
        return f"Error: {path} is a directory, not a file."
    return path.read_text(encoding="utf-8")


async def read_file(args: dict[str, Any]) -> str:
    """Read a UTF-8 text file relative to the current working directory.

    `Path.read_text()` blocks, and a blocking call inside `async def` stops the
    whole event loop rather than just this task.  With one tool at a time nobody
    notices; once chapter 8 runs tools concurrently the concurrency quietly
    turns into a queue.  Ruff's ASYNC240 rejects the direct call, which is why
    those rules are enabled.
    """
    path = args.get("path")
    if not isinstance(path, str):
        return 'Error: read_file needs a "path" argument, a string. Example: {"path": "a.py"}'
    return await asyncio.to_thread(_read, Path(path))


DEFAULT_TOOLS = {"read_file": read_file}

# What the model is shown.  Chapter 3 is about how much this wording matters.
TOOL_SCHEMAS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read a UTF-8 text file and return its contents.",
            "parameters": {
                "type": "object",
                "required": ["path"],
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Path to the file, relative to the working directory.",
                    }
                },
            },
        },
    }
]
```

注意工具**自己也在检查参数**（`isinstance(path, str)`），尽管 schema 里已经写了
`"required": ["path"]`。因为 schema 是给模型的**建议**，不是服务端强制的约束——模型完全
可能发一个 `{"path": 123}` 过来。**任何来自模型的东西都要当外部输入对待。**

`TOOL_SCHEMAS` 这段描述文字现在看着无关紧要，第 3 章会证明它决定了模型出错的频率。

### 8.5 我假设了循环会自己停

模型陷入一个模式：搜索、读文件、再搜索、再读。每轮单看都合理，整体是死循环。

上限要有。但**光有上限不够**：跑到第 12 轮，进程被砍断，用户拿到一句
"turn limit exceeded" 和零个有用的字。模型正做到一半，被杀了。

**它不知道自己快没时间了。没人告诉它。**

```python
# A budget the model cannot see is one it spends freely and is then
# killed by, mid-thought, with nothing to show.
if remaining <= BUDGET_WARNING_AT:
    history.append({
        "role": "system",
        "content": (
            f"You have {remaining} tool-calling turn(s) left. "
            "Wrap up and give your best answer now."
        ),
    })
```

模型不知道自己是循环里的一次迭代，不知道有"第 13 轮"这回事。你不说，它就按无限时间
规划。

> **这是本章第二次遇到同一个模式，后面还会遇到很多次**：
> **系统里有个状态，只有代码知道，模型不知道。**
> 第 5 章的权限状态、第 6 章的 token 预算、第 11 章的计划进度，全是这个。
> 解法永远一样——**注入上下文。**

### 8.6 还有两个是工具替我发现的

**忘了 `await`。**

```python
result = agent.run("go")     # 少了 await
```

不报错。构造一个协程对象，赋值，丢掉，**函数体一行都没跑**。唯一的信号是垃圾回收时
的 `RuntimeWarning: coroutine 'run' was never awaited`，而在一片绿色的测试输出里，这
句话等于不存在。

```python
def test_F00_08_a_forgotten_await_runs_nothing_and_says_nothing() -> None:
    async def work() -> int:
        raise AssertionError("this body must never run")

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        coro = work()  # the missing await
        del coro
        gc.collect()

    assert any("never awaited" in str(w.message) for w in caught)
```

注意 `work()` 的函数体是 `raise AssertionError`，而测试通过了——**因为它根本没跑。**

修法两行配置：

```toml
filterwarnings = ["error::RuntimeWarning"]
```

**阻塞 IO。** 我写读文件工具时的第一版是最自然的那个：

```python
async def read_file(args):
    return Path(args["path"]).read_text(encoding="utf-8")
```

```
$ ruff check .
ASYNC240 Async functions should not use pathlib.Path methods, use trio.Path or anyio.path
  --> src/minicodex/tools.py:24:16
```

`Path.read_text()` 是阻塞的，写在 `async def` 里阻塞的**不是这个任务，是整个事件循环**。
现在一个工具串行跑完全看不出来；到第 8 章工具开始并发，"并发"会悄悄退化成排队，而你会
以为是模型变慢了。

> 这验证了 Ch-1 里一个当时看着没道理的决定：**在还没有任何异步代码的时候就打开
> `ASYNC` 那组 lint 规则。** 它在这个 bug 被写出来的那一秒就拦住了它。

这两条是 ⚪ 类：**静态工具发现的，最便宜的一种。**

---

### 8.7 收工：`run()` 的最终样子

四处修复（§6 的停止条件、§8.1 的全部执行、§8.2 的哨兵检查、§8.5 的预算提示）加上
§7.1 的录制，`run()` 长成了这样：

```python
BUDGET_WARNING_AT = 2


    async def run(self, user_message: str) -> RunResult:
        history: list[dict[str, Any]] = [{"role": "user", "content": user_message}]
        final_text = ""

        for turn_index in range(self.max_turns):
            remaining = self.max_turns - turn_index

            # A budget the model cannot see is one it spends freely and is then
            # killed by, mid-thought, with nothing to show.
            if remaining <= BUDGET_WARNING_AT:
                history.append(
                    {
                        "role": "system",
                        "content": (
                            f"You have {remaining} tool-calling turn(s) left. "
                            "Wrap up and give your best answer now."
                        ),
                    }
                )

            self.recorder.record("request", {"turn": turn_index, "history": history})
            turn = await self._collect(self.model.stream(history))
            self.recorder.record(
                "response",
                {
                    "turn": turn_index,
                    "text": turn.text,
                    "finish_reason": turn.finish_reason,
                    "tool_calls": [{"id": c.call_id, "name": c.name} for c in turn.tool_calls],
                },
            )

            history.append(
                {
                    "role": "assistant",
                    "content": turn.text,
                    "tool_calls": [
                        {
                            "id": c.call_id,
                            "type": "function",
                            "function": {"name": c.name, "arguments": c.raw_arguments},
                        }
                        for c in turn.tool_calls
                    ],
                }
            )
            if turn.text:
                final_text = turn.text

            # Text is not a stop signal.  Models narrate before acting, and
            # stopping on the narration leaves the work undone while looking
            # exactly like success.
            if not turn.tool_calls:
                return RunResult(final_text, "completed", turn_index + 1, history)

            # Every call runs and every call is answered.  Skipping one leaves
            # it unanswered in history, and the *next* request is what fails.
            for call in turn.tool_calls:
                output = await self._run_tool(call)
                history.append(
                    {"role": "tool", "tool_call_id": call.call_id, "content": output}
                )

        return RunResult(final_text, "turn_limit", self.max_turns, history)
```

`_collect()` 也补上了 §8.2 那道检查，完整版：

```python
    async def _collect(self, stream: AsyncIterator[StreamEvent]) -> ModelTurn:
        """Turn a stream of events into one finished turn, or refuse to.

        Two accumulators because the wire has two granularities: prose arrives
        in arbitrary slices and gets joined, tool calls arrive whole and get
        placed by `index`.  Sorting by index rather than trusting arrival order
        costs one line and removes a question nobody wants to answer later.

        Nothing is returned until `Completed` arrives.  A stream that dies
        halfway therefore leaves nothing behind -- there is no half-built turn
        that could reach the history by accident.
        """
        text_parts: list[str] = []
        by_index: dict[int, ToolCallDelta] = {}
        completed: Completed | None = None

        async for event in stream:
            if isinstance(event, TextDelta):
                text_parts.append(event.text)
            elif isinstance(event, ToolCallDelta):
                by_index[event.index] = event
            elif isinstance(event, Completed):
                completed = event

        if completed is None:
            raise IncompleteStreamError(
                f"stream ended after {len(text_parts)} text chunk(s) and "
                f"{len(by_index)} tool call(s) without a [DONE] sentinel"
            )

        calls = []
        for index in sorted(by_index):
            raw = by_index[index]
            try:
                parsed = json.loads(raw.arguments) if raw.arguments else {}
                arguments = parsed if isinstance(parsed, dict) else None
            except json.JSONDecodeError:
                arguments = None
            calls.append(ToolCall(raw.call_id, raw.name, arguments, raw.arguments))

        return ModelTurn("".join(text_parts), tuple(calls), completed.reason)
```

加上 §5 的类型定义、§8.4 的 `_run_tool`，`src/minicodex/agent.py` 就完整了，226 行。

**注意 `run()` 里那四段注释。** 它们解释的都是**反直觉的一行**——为什么不是
`if turn.text`、为什么不能只跑第一个、为什么预算要说出来。这几行代码字面上都看不出理由，
而没有理由的反直觉代码，下一个人一定会"顺手修好"它。

---

## §8.8 清点：这一章一共写了什么

到这里，核心代码全部出现在正文里了。清点一下：

| 文件 | 行数 | 完整代码在 |
|---|---|---|
| `src/minicodex/model.py` | 156 | §4.1 定义三个事件类型，§4.2 给完整文件 |
| `src/minicodex/recorder.py` | 99 | §7.1 |
| `src/minicodex/tools.py` | 54 | §8.4 |
| `src/minicodex/agent.py` | 226 | §5.1–5.4（类型、Protocol、首版组装与循环）→ §8.4（`_run_tool`）→ §8.7（最终 `run` 和 `_collect`） |
| `src/minicodex/stub_ollama.py` | 185 | §7.2 给了录音本体；`_text` / `_call` / `_finish` 三个包装函数和几十行 HTTP 样板见仓库 |
| `src/minicodex/__main__.py` | 68 | 只是 argparse 加两个子命令，见仓库 |
| `tests/` | 435 + 42 | 正文引了十来个关键测试，全部见仓库 |

**`agent.py` 是分三次给的，因为它是分三次长出来的。** §5 那版有四个错，我当时一个都不
知道。如果一上来就贴 226 行的成品，你看到的是结论，看不到那四个错是怎么被找出来的——
而那才是这一章唯一教得会的东西。

没进正文的三样，理由都是"它们不是这一章的内容"：`stub_ollama.py` 的 HTTP 样板、
`__main__.py` 的 argparse、以及测试文件里那些和已引用的测试同构的部分。**除此之外没有
任何隐藏的代码**——你照着正文敲，能敲出一个能跑的 `minicodex`。

---

## §9 验证这些测试真的有用

一个绿色的测试不能证明它有用。**把每个修复改回去，看对应的测试红不红。**

| 改回去 | 抓住它的测试 |
|---|---|
| `if not turn.tool_calls:` → `if turn.text:` | `test_F00_02_absence_of_tool_calls_is_the_stop_signal` |
| `for call in turn.tool_calls:` → `[:1]` | `test_F00_03_outputs_pair_one_to_one_with_calls` |
| `if completed is None:` → `if False:` | `test_F00_04_nothing_from_a_cut_stream_is_executed` |
| `except Exception` → `except ZeroDivisionError` | `test_F00_05_a_failing_call_does_not_cancel_its_siblings` |
| 删掉可用工具列表 | `test_F00_06_unknown_tool_says_what_to_call_instead` |
| 关掉预算提示 | `test_F00_01_model_is_warned_before_the_budget_runs_out` |
| **`sorted(by_index)` → `list(by_index)`** | **没有测试抓住（见 §8.1）** |

六红一绿。**那个绿的比六个红的更值得记住**——它告诉我哪一行代码是我一厢情愿加上去的。

```
$ uv run pytest
.......................................                                  [100%]
39 passed in 2.18s
```

> 上一章的规则，这一章照做：**一个对着坏代码也能通过的测试是装饰品。** 三十秒的成本。

---

## §10 交给 git

### 分支

```bash
git checkout -b feat/agent-loop
```

上一章直接提在 `main` 上，因为脚手架没有"行为"可以 review。这一章有。

### 提交序列

```
c1324a9 feat: stream responses from an OpenAI-compatible endpoint
37b4833 test: replay recorded Ollama responses instead of inventing them
b39b60a feat: add the loop, one tool, and the ask command
3497594 test: pin the loop's guarantees, keeping the first version runnable
b36c2c4 chore: fail the suite on RuntimeWarning, add httpx
```

**注意第二条排在循环前面**，这是真实顺序：我先有了录音才敢写测试，而不是先写完循环再
补测试。commit 顺序**照着实际发生的顺序**，不是照着"应该的样子"。

### 三条 message，三种不同的活

**第一条**在记录"我为什么不信文档"：

```
feat: stream responses from an OpenAI-compatible endpoint

Written against a recorded response rather than the documentation, because
the documentation and the wire disagreed twice: the native /api/chat examples
show tool calls without ids, and the wire has them.

Prose and tool calls stream at different granularities. Prose arrives in
arbitrary slices -- one response split the path
src/minicodex/prompts/system.md across two chunks as "minicodex/prom" and
"pts/system.md" -- while a tool call arrives whole, arguments already a
complete JSON string. One accumulator does not fit both.

There are two terminators. finish_reason says why the model stopped; the
[DONE] sentinel says the HTTP stream is over. [DONE] is not JSON, so parsing
before checking for it fails -- after printing a perfectly good answer.
```

**"文档和实际返回对不上"这句话值钱**，因为半年后有人照着文档改代码时，这就是拦住他的
东西。

**第二条**在解释一个方法论选择：

```
test: replay recorded Ollama responses instead of inventing them

Hand-written fixtures drift away from the wire silently. Recorded ones are
wrong only in ways the recording was wrong, which is a much smaller surface.
```

**第四条**在承认一件没做到的事：

```
Each guarantee was checked by mutation -- revert the guard, watch the named
test fail. One guard was not caught: sorting tool calls by index rather than
arrival order changes nothing against a recording that already arrives in
order. It is defensive, it is not currently load-bearing, and saying so is
cheaper than pretending otherwise.
```

**把"这条没验证到"写进 commit message**，比它烂在你脑子里强。下一个人读到这段，就知道
那行 `sorted` 是可疑的、可以质疑的。

### 中途撞的报错要不要 commit

§4 那个 `JSONDecodeError`、§6 那个空 log——都是中间状态，两条都**不提交**。commit 的
粒度是"一个能独立通过测试的完整想法"，撞报错的中间态两条都不满足。

但**报错的过程要留下来**——留在 commit message 的 body 里，或者留在代码注释里。

> **区分这两件事**：git 历史记录的是**状态**，注释和 message 记录的是**理由**。
> 把探索过程塞进 git 历史（`wip`、`try again`、`fix fix`）两边都做不好。

---

## §11 Code review

顺序：**正确性 → 边界情况 → 可测试性 → 命名 → 风格。** 风格最容易看见、最有产出感、
也最不重要，放最后是为了逼自己先看前四项。

---

**1 · 正确性** — `_collect` 用 `by_index[event.index] = event` 覆盖写。如果供应商真的
分片传参数（每片带同一个 index），后一片会**覆盖**前一片，而不是拼接。静默丢数据。

> **作者**：真实风险，不在这里修。这个供应商观测到的行为是一次给完，为一个没见过的
> 行为写累积逻辑就是写死代码去伺候想象。第 1 章要接第二家，那时会先测再写。
> **已加进第 1 章清单，不加 TODO——一个没有 owner 的 TODO 就是一个愿望。**

**结局：延后，但给了目的地。** "第 1 章"可以检查，"以后"不能。

---

**2 · 正确性** — 预算提示在**问模型之前**追加。如果模型这一轮直接结束，历史里就留着
一条"抓紧时间"给一个已经结束的模型。

> **作者**：接受它无害，但给理由不是耸肩。这条提示在 `return` 之后就是死数据，没有代码
> 会读循环结束后的历史。移到模型调用之后的话，它会落在它本该领先的那条 assistant 消息
> **后面**，那更糟。保持原样，加注释。

---

**3 · 边界情况** — `sorted(by_index)` 没有测试覆盖。

> **作者**：对，而且我自己在变异验证里发现了（§8.1、§9）。写进了 commit message。
> 不删，因为代价一行；不假装它被验证过。

**结局：承认，记录，保留。** 这是 review 里最健康的一种交换——reviewer 提出一个真问题，
作者给出一个诚实的、不完美的答案。

---

**4 · 可测试性** — `_naive_run` 在测试文件里复制了一份循环逻辑。`Agent` 改了形状，它就
会漂移，然后什么都证明不了。

> **作者**：代价已知、接受、有边界。它的存在让两个 bug 可执行而不是口头传说，这比重复
> 的代价值。边界写在 commit message 和 README 里：只演示这两个 bug，不许长。第 1 章
> 如果让它漂了，**删掉，不修。**

---

**5 · 命名** — `ToolCallDelta` 叫 Delta，但它不是碎片，是完整的调用。名字撒谎。

> **作者**：接受问题，拒绝改名。`delta` 是 wire 上那个字段的名字
> （`choices[0].delta.tool_calls`）。**和协议保持一致，胜过一个孤立看更准确的名字**——
> 否则读代码的人要在两套词汇之间做翻译。加了 docstring 说明：
> "Named `Delta` because that is the field it arrives in, not because it is a fragment."

**结局：拒绝，附理由。** reviewer 被推翻是正常结局。让它成立的是**理由被写在了下一个
人会看到的地方**。

---

### 这次 review 没做的事

它**没有要求拆分模块**。`agent.py` 228 行，混了四种职责，每个本能都在说该提一句。

但 PR 里已经写清楚了：单文件是个选择，理由是什么，在哪里偿还。在这个前提下，"现在就
拆"等于要求作者去猜边界——而他刚说了他还看不出边界在哪。

> **一条要求对方去猜的 review 意见，比不提更糟。**
> 它会产出一个没人相信的抽象，而那恰恰是最难拆掉的东西。

正确时机是 **Interlude A**，那时候调用点足够多，边界是**被观察到的**，不是被发明的。

---

## §12 本章给 CI 加了什么

**什么都没加。**

新增的保护——`filterwarnings = ["error::RuntimeWarning"]`——住在 `pyproject.toml` 里，
现有的 `Test` 步骤自动就跑到了。

值得专门说一句，因为条件反射是每章给 CI 加点东西。

> **一个检查凭什么进"能挡住合并"的 CI？凭它拦下过东西。**

上一章加过一个检查（打开 wheel 看内容），理由是**当场吃了亏**。这一章没吃到需要新检查
才能防住的亏。

**但这一章加了一个运行时依赖**：`httpx`。Ch-1 的 `dependencies = []` 到此为止。理由
很实在——要跟模型说话就要发 HTTP。这是全书第一个，我希望它长得很慢。

---

## §13 回头看：这一章撞到了什么

| 编号 | 故障 | 怎么暴露的 | 挡住它的东西 |
|---|---|---|---|
| — | `[DONE]` 不是 JSON，解析器崩在最后一行 | 🔴 崩溃（但**先正确打印了整句话**） | 先判哨兵，再解析 |
| F00-01 | 循环永不结束；或到点被砍死，交付零个字 | 🔵 长跑 | 轮次上限 + **模型看得见的**预算提示 |
| F00-02 | **循环停在旁白上，活没干，结果看起来像成功** | 🟡 静默 | 停止信号 = 没有工具调用 |
| F00-03 | 三个并发调用只跑了第一个，其余成孤儿 | 🔴 下一轮 400 | 每个调用都跑、都产出结果 |
| F00-04 | 流在 `[DONE]` 之前断，半个回合进历史 | 🔴 崩溃 | 局部累积，收到哨兵才提交 |
| F00-05 | 工具抛异常，整个会话没了 | 🔴 崩溃 | 异常转成 output 交还模型 |
| F00-06 | 模型编了个工具名 | 🔴 `KeyError` | 错误信息里列出可用工具 |
| F00-07 | **偶尔跑出不同结果，修复只是运气** | 🟡 静默 | 录制真实响应，逐字节回放 |
| F-1-04 | 出了问题不知道模型收到了什么 | 🟠 可观测性 | 请求/响应全量落盘 |
| F00-08 | 忘了 `await`，什么都没发生 | ⚪ 静态 | `error::RuntimeWarning` |
| F00-09 | `async def` 里做阻塞 IO，事件循环被卡住 | ⚪ 静态 | ruff `ASYNC240` |

标记：🔴 崩溃 · 🟡 静默 · 🟢 边界测试 · 🔵 长跑 · 🟠 看日志 · 🟣 review · ⚫ 用户报告 ·
⚪ lint/类型

**五条 🔴 是自己跳出来的。剩下六条不是。**

而这一章最重要的两条——F00-02 和 F00-07——都是 🟡。一个是活没干但答得漂亮，一个是
偶尔才出错所以你以为修好了。**两条都不报错，两条都得主动去找。**

---

## §14 codex 是怎么做的

**流的终止事件。** codex 对着 OpenAI 的 Responses API 等 `response.completed`，
`codex-rs/core/src/client.rs` 里是完整的流状态机。少了终止事件整个响应作废——和我们的
`IncompleteStreamError` 是同一条规则。

**假模型 + 录制回放是它的主力测试手段。** `codex-rs/core/tests/suite/` 下 80 多个测试
文件跑在一个假 responses server 上。文件名本身就是一份故障清单：`abort_tasks.rs`、
`compact_resume_fork.rs`、`quota_exceeded.rs`、`pending_input.rs`、
`mcp_refresh_cleanup.rs`。第 14 章会把这个列表完整摆出来。

**"模型看不见的状态"这个问题有多大。** `codex-rs/core/src/context/` 下面有**三十多个
模块**，每一个都是一段会被注入模型上下文的片段：

- `token_budget_context.rs` —— 预算状态
- `rollout_budget.rs` —— 剩余额度
- `turn_aborted.rs` —— "你上一轮被打断了"

外加 `prompts/templates/goals/budget_limit.md` 专门处理预算耗尽时怎么收尾。

**三十多个模块**说明的是：**"让模型知道自己的处境"不是一行 if，是一个子系统。** 我们这
一章写了它的第一个成员。

**工具错误即 prompt。** codex 有一整个 `consequential_tool_message_templates.json`，还有
`mcp_tool_approval_templates.rs`——**错误和提示信息是被模板化管理的资产**，不是散在代码
里的字符串。

**"一坨"要不要拆？** 看 `core/src/` 就知道答案不是"一开始就拆好"：

```
core/src/tools/handlers/multi_agents/       ← v1
core/src/tools/handlers/multi_agents_v2/    ← v2，和 v1 并存
core/src/compact_remote.rs
core/src/compact_remote_v2.rs
core/src/compact_remote_v2_attempt.rs
```

`multi_agents` 和 `multi_agents_v2` **同时躺在代码库里**。上下文压缩三代同堂。

**这是 OpenAI 的团队。他们也是先写一版、发现不行、再写第二版，而且不敢删掉第一版。**

---

## §15 三条线各自留下了什么

### 主线 A · 怎么把需求变成代码

**先看见它动，再谈设计。** 这一章的第一段代码是"把每一行打出来"，不是架构。那九行原始
输出里有三个我想不出来只能看出来的事实（碎片长度不规则、工具调用是原子的、两个终止
信号），而这三个事实**直接决定了后面每一个函数的形状**。

**不为没见过的情况写代码。** 参数不分片，我就不写累积逻辑。别家可能分片，等接到别家再
说。这一条我在 review 里被质疑了（§11 第 1 条），答案还是一样。

**抽象决策**，这一章做了五个：

| 东西 | 决定 | 理由 |
|---|---|---|
| async | **现在就做** | 改起来动到每个调用点，且需求确定 |
| `Model` Protocol | **现在就做** | 换模型是今天的需求：测试要确定性的，用户要真的 |
| `request_body()` 独立成方法 | **现在就做** | 第 6、13 章要打印/diff/断言它，而不发网络请求 |
| 历史结构 | **现在不做**（`list[dict]`） | 只有一处写入，形状是猜的 |
| 拆分 `agent.py` | **现在不做** | 边界是猜的，错的边界比没边界更难拆 |

### 主线 B · 工程化的行为

| 动作 | 本章的规则 |
|---|---|
| commit 顺序 | 照实际发生的顺序，不照"应该的样子" |
| 中途的报错 | 不提交。**git 记状态，注释和 message 记理由** |
| message body | 记录"文档和实际对不上"；记录"这条没验证到" |
| review 的结局 | 改 / 拒绝（附理由）/ 承认（写文档）/ 延后（给目的地），四种都合法 |
| review 不该做的 | 不要提一条要求对方去猜的意见 |
| CI | 没吃到相应的亏，就不加检查 |
| 依赖 | 第一个运行时依赖到第 0 章才出现 |

### 主线 C · 故障的预防与发现

**第一招：**

> **永远不要相信模型对自己行为的描述。**
> 断言可观测的副作用——哪个工具被调了、传了什么参数、文件是不是真的变了。

**第二招：**

> **"偶尔才复现"比"每次都错"更危险**，因为它让你误以为修好了。
> 解法不是重跑几次，是**把那次真实响应录下来，逐字节播回去**。

**第三招：**

> 撞到一个凭直觉写错的地方之后，**顺着查一遍所有凭直觉写的地方**——
> "如果现实不像我想的那样呢"。§8 那五条全是这么找出来的。

**第四招（从 Ch-1 延续，但这次有新收获）：**

> 写完修复，把修复改回去，确认测试会红。
> **没红的那一条比红了的六条更值钱**——它指出了哪行代码是一厢情愿。

---

## 如果你只记住三件事

1. **先看见它动。** 打开真实的流看九行原始输出，比想一小时有用。这一章后面所有代码的
   形状都是那九行决定的。

2. **模型说它做了，不等于它做了。** F00-02 里那句"我这就去读那个文件"说得非常漂亮，
   文件从来没被打开过。断言副作用，不是断言措辞。

3. **假模型是调查工具，不是准备工作。** 你先撞见问题，发现每次跑结果都不太一样、查不
   动了，才去录制和回放。顺序反过来，你会在还不知道要测什么的时候先造测试脚手架。

---

## 动手

```bash
cd steps/step00_minimal_loop
uv sync --all-extras

# 对着你自己的 Ollama：
uv run minicodex ask "What does src/minicodex/__init__.py define?"

# 或者对着录音，不需要 GPU：
uv run minicodex serve-stub &
uv run minicodex ask "What does src/minicodex/__init__.py define?" \
    --base-url http://127.0.0.1:11435/v1

uv run pytest
```

**建议自己做一遍的四件事**：

1. **先跑 §3 那三十行**（对着你自己的 Ollama），把原始流打出来看一遍。这是本章的起点，
   也是你以后面对任何陌生 API 该做的第一件事。
2. 打开 `.minicodex/recordings/` 里的文件，找到**第二轮的 `request`**，看清楚模型在
   回答之前收到了什么。这是全书后面最常做的动作。
3. 把 `agent.py` 里 `if not turn.tool_calls:` 改成 `if turn.text:`，跑测试看哪个红。
   然后跑 `ask`——**注意它的输出看起来仍然完全正常。**
4. 用真 Ollama 把同一个问题问五遍，看它变不变。**这是 §7 那个"偶尔才复现"的第一手
   体验**，比读五遍管用。

---

**下一章**：Ch01 · 先定协议，再写逻辑——接第二家供应商，然后发现 `list[dict]` 和
"参数是完整 JSON 字符串"这两个假设同时崩掉。
---

# 附录 · 代码到底怎么敲出来的（新手向，逐段精讲）

跟第 13 章附录同一个规矩：正文讲"为什么"，这里补"怎么写"。默认你已经读过
前面几章的附录（`dataclasses`、`asyncio`、`Path` 基础），这里只讲这一章
正文明确说"没进正文"的三样（§8.8 自述）：`stub_ollama.py` 的 HTTP 样板、
`__main__.py` 的 argparse、以及它们各自的写法细节。代码摘自
`steps/step00_minimal_loop/src/minicodex/`，逐段核对过。

先把范围说死：

1. 本附录只解释第 0 章在 `steps/step00_minimal_loop/` 里新增的代码。
   正文 §8.8 的文件清点表说得很清楚：`model.py`、`recorder.py`、`tools.py`、
   `agent.py` 的完整代码都已经在正文里了，**不再重复**；这里只补正文明说
   "见仓库"的 `stub_ollama.py` 和 `__main__.py`。
2. 完整函数代码块里不使用代表遗漏实现的省略号，也不把关键分支写成
   "此处略"。示例字符串或命令文本里出现的三个点，如果本来就是源码的
   一部分，会原样保留。
3. 代码块按当前源码核对。

## K1 · `stub_ollama.py`：一个会背台词的假 Ollama

正文 §7.2 给了"录音本体"（`NARRATE_THEN_CALL` / `FINAL_ANSWER` /
`THREE_CALLS` 三个列表里的实际文本），但三个包装函数（`_text` / `_call` /
`_finish`）和整个 HTTP 服务器（`_Handler` / `serve`）被归为"HTTP 样板，
见仓库"。附录把它们补全。

### K1.1 三个包装函数：把一次对话的"台词"写成 chunk

```python
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
```

新手最容易忽略的三个点：

1. **三个函数是"chunk 生成器"**——每个都返回一个**完整的 SSE chunk**
   （`dict`，待会 `json.dumps` 后发出去）。`_text` 产生一段散文
   （`delta.content` 非空、无 `tool_calls`），`_call` 产生一次工具调用
   （`content` 空、`tool_calls` 带 id/index/name/arguments），`_finish`
   产生结束标记（`finish_reason` 非空）。
2. **`"created": 1786021399` 是写死的常数**。它不是当前时间——这是
   2026-08-06 录制的真实时间戳。假模型不该每次运行都生成不同的时间戳，
   否则"回放"就没有意义了。
3. **`_call` 的 `arguments` 参数是字符串**（`'{"path":"src/minicodex/__init__.py"}'`），
   不是 dict——和 OpenAI API 的 wire 格式一致，正文 §7.2 里就是这么写的。

### K1.2 录音本体：一次真实对话的逐字拷贝

```python
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
```

- **`*[ ... ]` 是列表解包**：把前面 `_text` 生成的一串 chunk 和后面的
  `_call`、`_finish` 拼进同一个列表。
- **chunk 大小是"服务器觉得怎么切就怎么切"**（注释里专门说了）：`"I"`、
  `" will read the contents of"`、`" the file"`…… 不是一个词一个词来的，
  是 Ollama 当天实际切的。这保证客户端代码**不能假设 chunk 边界**——正文
  说"路径被切成两半（`src/minicodex/prom` / `pts/system.md`），所以任何
  东西都不能在拼接之前被解释"。
- `FINAL_ANSWER` 和 `THREE_CALLS` 是同样的结构（正文 §7.2 给了文本）。

### K1.3 `_Handler`：一个只能处理 POST 的 HTTP 服务器

```python
class _Handler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        messages = body["messages"]

        mode = body.get("stub_mode", "narrate")
        cut = body.get("stub_cut")

        if any(m.get("role") == "tool" for m in messages):
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
```

六个细节：

1. **`int(self.headers["Content-Length"])`**——读请求体必须先知道多长，
   `self.rfile.read(n)` 精确读 n 字节。HTTP 的 body 没有"读到 EOF"这一说。
2. **选录音的逻辑是"看消息里有没有 tool 结果"**：`any(m.get("role") ==
   "tool" for m in messages)`——客户端把工具结果发回来（role=tool）就说明
   该回答最终答案了，否则（第一轮）走"先叙述再调用"。
3. **`stub_mode` / `stub_cut` 是 `stub_*` 前缀的扩展键**——它们不是 OpenAI
    schema 的一部分，真服务器会忽略它们；这个假服务器用它们让测试选择
   录音（`mode == "three"` 选三连调用）或截断流（`cut` 切前 N 个 chunk）。
4. **SSE 格式是 `data: {json}\n\n`**：每行 `data: ` 前缀 + JSON + 空行。
   `b"data: " + json.dumps(chunk).encode() + b"\n\n"` ——字节拼接，因为
   `wfile.write` 要 bytes。
5. **`cut is None` 才写 `[DONE]`**：截断的流**没有** sentinel（F00-04
   就是靠这个复现的——客户端在 `[DONE]` 之前断流）。
6. **`log_message` 被覆写成空**：不打印每个请求的访问日志，测试输出干净。

### K1.4 `serve`：把 handler 跑起来

```python
def serve(port: int = 11435) -> None:
    server = HTTPServer(("127.0.0.1", port), _Handler)
    print(f"stub listening on http://127.0.0.1:{port}/v1  (Ctrl-C to stop)")
    server.serve_forever()
```

三行：造服务器、打印地址、`serve_forever` 阻塞。`HTTPServer` 是单线程
同步的（每个请求新建一个 `_Handler` 实例）——对测试够用，正文第 13 章
专门讨论过"共享的单线程 stub"的代价。

## K2 · `__main__.py`：CLI 入口

正文 §8.8 说 `__main__.py` 只是"argparse 加两个子命令，见仓库"。完整
实现：

```python
async def _ask(question: str, *, base_url: str, model: str) -> int:
    recorder = Recorder()
    llm = OllamaModel(base_url=base_url, model=model, tools=TOOL_SCHEMAS)
    agent = Agent(llm, DEFAULT_TOOLS, recorder=recorder)

    result = await agent.run(question)

    print(result.final_text or "(no answer)")
    print(f"\n[{model} | {result.stop_reason} after {result.turns_used} turn(s)]")
    print(f"[transcript: {recorder.path}]")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="minicodex")
    parser.add_argument(
        "--version",
        action="store_true",
        help="print the version and enough environment detail to file a bug report",
    )
    sub = parser.add_subparsers(dest="command")

    ask = sub.add_parser("ask", help="ask a question that requires reading a file")
    ask.add_argument("question")
    ask.add_argument("--base-url", default=DEFAULT_BASE_URL)
    ask.add_argument("--model", default=DEFAULT_MODEL)

    stub = sub.add_parser("serve-stub", help="replay recorded Ollama responses on a local port")
    stub.add_argument("--port", type=int, default=11435)

    args = parser.parse_args(argv)

    if args.version:
        print(f"minicodex {__version__}")
        print(f"python    {platform.python_version()} ({sys.platform})")
        return 0

    if args.command == "ask":
        return asyncio.run(_ask(args.question, base_url=args.base_url, model=args.model))

    if args.command == "serve-stub":
        from minicodex.stub_ollama import serve

        serve(args.port)
        return 0

    parser.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

新手最容易卡住的四个点：

1. **`async def _ask` + `asyncio.run` 的分离**。`_ask` 是 async（里面
   `await agent.run(question)`），`main` 是同步的——`asyncio.run(_ask(...))`
   在调用点启动事件循环。这是"CLI 是同步的、核心是 async 的"标准接法。
2. **`main(argv: list[str] | None = None)` 接受参数列表**。测试直接调
   `main(["ask", "..."])`，不用动 `sys.argv`。`parser.parse_args(argv)`
   用传入的列表（`None` 时 argparse 自己读 `sys.argv[1:]`）。
3. **`serve-stub` 的 import 放在分支里**（`from minicodex.stub_ollama
   import serve`）——`__main__` 只在用户真的跑 serve-stub 时才 import
   stub 模块，平时启动不需要（也避免可能的导入成本）。
4. **`print_help()` + `return 0`**：第 0 章还没有"没子命令退出 2"的修复
   （那是第 15 章的事），此时帮助上 stdout、退出 0 是"老行为"，正文
   §4.2 记录了后来怎么改的。

## K3 · 新手常见报错/坑对照表

| 症状 | 原因 | 修法 |
|---|---|---|
| stub 收到请求但 `rfile.read` 卡住 | 没按 `Content-Length` 读 | `int(self.headers["Content-Length"])` 再 `read(n)` |
| 客户端解析最后一行崩了 | stub 没发 `[DONE]` | `cut is None` 才写 sentinel（截断的流故意没有） |
| 录音文本在拼接前被解释 | chunk 边界是任意的 | 客户端先收集所有 chunk 再拼（正文 F00-04） |
| `stub_mode` 不生效 | 扩展键没从 body 里取 | `body.get("stub_mode", "narrate")`，`stub_*` 前缀 |
| `wfile.write` 报类型错误 | 写了 str 不是 bytes | `b"data: " + json.dumps(chunk).encode() + b"\n\n"` |
| 测试输出被访问日志刷屏 | 没关 `log_message` | 覆写成 `pass` |
| `_ask` 里直接调 `await` 报错 | 在同步函数里 await | `_ask` 是 `async def`，`main` 里 `asyncio.run(_ask(...))` |
