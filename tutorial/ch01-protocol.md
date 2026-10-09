# 第 1 章 · 先定协议，再写逻辑

> **代码**：`steps/step01_protocol/`
> **分支**：`feat/protocol-layer`
> **产出**：同一个 Agent，能对着两家"说法不同"的服务跑
> **前置**：做完第 0 章，`feat/agent-loop` 已经合并进 `main`。
> 有 OpenAI 的 API key 更好（本章用到的请求总共花不了几分钱）；没有也能跟完，两家的回复都录好了。

---

## §0 开工前的准备

### 0.1 本章会遇到的新概念

**API key（密钥）。** 本地的 Ollama 谁都能连；OpenAI 这样的云服务要知道"是谁在调用、该向谁收费"，
所以每个请求都要带一个密钥。它通常放在 HTTP 请求的**请求头（header）**里：

```
Authorization: Bearer sk-...
```

请求头是随请求一起发送的"附加信息"，一行一个 `名字: 值`。第 0 章的请求里 httpx 自动加了
`Content-Type: application/json`（说明请求体是 JSON），这一章要自己再加一个 `Authorization`。

**密钥绝对不能写进代码、也不能写进命令行参数。** 写进代码会被提交进 git（第 -1 章说过，
进了 git 历史就删不掉）；写进命令行参数会留在 shell 的历史记录里，也能被同一台机器上的
其他用户通过进程列表看到。正确的做法是放在**环境变量**里，程序运行时去读：

```bash
# macOS / Linux，只对当前终端有效
export OPENAI_API_KEY="sk-..."

# Windows PowerShell，只对当前终端有效
$env:OPENAI_API_KEY = "sk-..."
```

Python 里用 `os.environ.get("OPENAI_API_KEY")` 读取，没设置时得到 `None`。

**协议和方言。** 很多服务都说自己"兼容 OpenAI 的接口"，就像很多人都说自己"会说中文"——
但口音、用词可能完全不同。本章会亲眼看到，同一个工具调用，两家发回来的样子差别有多大。

### 0.2 本章会遇到的新 Python 写法

| 写法 | 意思 |
|---|---|
| `Literal["a", "b"]` | 类型标注：值只能是 `"a"` 或 `"b"` 这几个字符串之一 |
| `d.setdefault(k, v)` | 字典里有键 `k` 就返回它的值；没有就先放进 `v`，再返回 `v` |
| `d.pop(k, None)` | 取出并删除键 `k` 的值；键不存在时返回 `None` 而不报错 |
| `@property` | 让一个方法可以像属性一样读取：写 `obj.items`，不用写 `obj.items()` |
| `ast.parse(文本)` | 把 Python 源代码解析成一棵语法树，可以用程序检查"这个文件 import 了什么" |
| `sys.modules` | Python 已经导入过的模块都记在这里；第二次 import 同一个模块时直接从这里取 |

### 0.3 准备工作

```bash
git switch main
git pull                             # 确认拿到了合并后的第 0 章代码
git switch -c feat/protocol-layer
```

第 0 章的录音服务在这一章会被扩充，读者如果还开着上一章的 `serve-stub`，先 Ctrl-C 停掉。

---

## §1 这一章要做出来的东西

一条命令，多一个参数：

```bash
minicodex ask "What does src/minicodex/__init__.py define?" --provider openai
```

听起来只是换个地址。**实际上第 0 章的代码会当场失效**，而且不是崩溃，是**悄悄产出垃圾**。

第 0 章的代码审查里有一条意见被延后了：

> 如果某家服务真的分片发送参数（每片带同一个 index），`by_index[event.index] = event`
> 会用后一片覆盖前一片，悄悄丢数据。
>
> 回答：这家服务观察到的行为是一次发完。接第二家供应商时会先测、再写。

**这一章就是来兑现这句话的。** 一条被延后的审查意见，到期了。

---

## §2 定需求

| 目标里的词 | 追问 | 需要做的事 |
|---|---|---|
| 换一家供应商 | 除了地址还有什么不一样？ | **不知道，得先测** |
| OpenAI | 它要什么我们没给过的东西？ | 带上 API key |
| 同一个 Agent | 循环、工具、历史都不用改？ | 待验证 |
| `--provider` | 从命令行选 | 给 `ask` 加一个参数 |

再加第 -1 章的两个追问：

> **追问一：我怎么验证它两家都对？** 跑通了就算对吗？→ 要能对比两家**在程序内部**产出的东西是否一样。
>
> **追问二：换第三家会怎样？** → 现在还不知道，但至少要保证：换供应商时，不用去改循环。

第一行就是"不知道"。**所以这一章的第一件事不是写代码，是去测。**

---

## §3 先去测

不读文档，直接问两边同一个问题，把原始的流打印出来。用的还是第 0 章 那个临时脚本，
只改三处：地址、模型名，以及加上密钥。

```python
# explore.py -- a throwaway script; the changes from chapter 0 are marked
import asyncio
import os

import httpx

URL = "https://api.openai.com/v1/chat/completions"                          # changed
HEADERS = {"Authorization": f"Bearer {os.environ['OPENAI_API_KEY']}"}        # new
TOOLS = [{"type": "function", "function": {
    "name": "read_file", "description": "Read a UTF-8 text file",
    "parameters": {"type": "object", "required": ["path"],
                   "properties": {"path": {"type": "string"}}}}}]


async def main():
    payload = {
        "model": "gpt-4o-mini",                                              # changed
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
        async with client.stream("POST", URL, json=payload, headers=HEADERS) as resp:
            async for line in resp.aiter_lines():
                if line:
                    print(line)


asyncio.run(main())
```

> `os.environ['OPENAI_API_KEY']` 用方括号取值：没设置环境变量时直接报 `KeyError`。
> 对一个临时脚本来说，这比悄悄用 `None` 去请求、拿回一个看不懂的 401 更好。
> 和第 0 章一样，这个脚本看完就删，不提交。

### 3.1 Ollama 怎么发一个工具调用

第 0 章已经看过了，一个 chunk 发完：

```json
"tool_calls":[{"id":"call_yfo64477","index":0,"type":"function",
               "function":{"name":"read_file",
                           "arguments":"{\"path\":\"src/minicodex/__init__.py\"}"}}]
```

### 3.2 OpenAI 怎么发同一个工具调用

下面只摘出每个 chunk 里 `tool_calls` 的那一项：

```json
{"index":0,"id":"call_bqv6MLMr9BhXis7T4LB6tGqa","type":"function",
 "function":{"name":"read_file","arguments":""}}
{"index":0,"function":{"arguments":"{\""}}
{"index":0,"function":{"arguments":"path"}}
{"index":0,"function":{"arguments":"\":\""}}
{"index":0,"function":{"arguments":"src"}}
{"index":0,"function":{"arguments":"/min"}}
{"index":0,"function":{"arguments":"ic"}}
{"index":0,"function":{"arguments":"od"}}
{"index":0,"function":{"arguments":"ex"}}
{"index":0,"function":{"arguments":"/__"}}
{"index":0,"function":{"arguments":"init"}}
{"index":0,"function":{"arguments":"__."}}
{"index":0,"function":{"arguments":"py"}}
{"index":0,"function":{"arguments":"\"}"}}
```

（2026-08-06 对着 api.openai.com 的 `gpt-4o-mini` 实测。）

**十四个 chunk。而且只有第一个带 `id` 和 `name`，后面十三个只有 `index` 和一小片参数。**
十四片拼起来，正好是 `{"path":"src/minicodex/__init__.py"}`。

还有一件事：system 提示明明要求"调工具之前先说一句"，这次 OpenAI 的模型**一句话都没说**，
直接调了工具。

顺带两个小差异，也是测出来的：

- 工具调用那个 chunk 里，Ollama 的 `content` 是 `""`，**OpenAI 的是 `null`**（Python 里就是 `None`）。
  第 0 章用的是 `if delta.get("content"):`（真值判断），两种都能正确跳过——**这次是运气好**。
  如果当时写的是 `if "content" in delta:`，OpenAI 这边就会往文字里塞一个 `None`。
- OpenAI 的 chunk 里还有 `refusal`、`service_tier`、`obfuscation`、`logprobs` 几个 Ollama 没有的字段。
  我们都不看，这没问题——**多出来的字段可以忽略，少掉的字段不行。**

### 3.3 把真实的碎片喂给第 0 章的代码

第 0 章 `_collect` 里的那一行：

```python
by_index[event.index] = event      # overwrite
```

而第 0 章的 `stream()` 每见到一个 `tool_calls` 就交出一个 `ToolCallDelta`。十四片全是
`index=0`，所以 `by_index` 里最后留下的是**最后一片**：

```
$ 把 OpenAI 的十四片真实碎片喂给第 0 章的 stream() 和 _collect
name           = ''
call_id        = 'call_0'
raw_arguments  = '"}'
arguments      = None
```

工具名没了，参数只剩最后两个字符，`call_id` 是兜底编出来的。

**而它不崩。** `_run_tool` 会走"没有叫 `''` 的工具"那条分支，返回一条错误信息给模型，
Agent 继续跑，最后产出一段基于错误信息瞎编的答案。这就是那条审查意见说的"悄悄丢数据"。

> **回头看，第 0 章不写拼接逻辑的决定是对的**——不是因为运气，而是因为它附带了一个可以检查的
> 期限（"接第二家时先测"）。如果当时凭想象写了拼接逻辑，多半会写成"每一片都带 id 和 name"
> 的版本，照样接不上真实的 OpenAI。**猜一个没见过的形状，猜对的概率并不比不猜高。**

### 3.4 第二件事：历史的"方言"也不一样

第 0 章的历史是这么攒的：

```python
history.append({
    "role": "assistant",
    "content": turn.text,
    "tool_calls": [{"id": c.call_id, "type": "function",
                    "function": {"name": c.name, "arguments": c.raw_arguments}}],
})
```

第 0 章 说过，这个形状**是照抄请求格式的**。那这份历史发给另一种格式的服务会怎样？
Ollama 除了 OpenAI 兼容的 `/v1`，还有一套自己的原生接口 `/api/chat`。把 `/v1` 格式的历史
发给原生接口：

```
HTTP 400
{"error":"Value looks like object, but can't find closing '}' symbol"}
```

反过来，把原生格式的历史发给 `/v1`：

```
HTTP 400
{"error":{"message":"json: cannot unmarshal object into Go struct field
 .messages.tool_calls.function.arguments of type string ...","type":"invalid_request_error"}}
```

两个方向都是 400，**两条报错都帮不上忙**——一条在说 JSON 括号，一条漏出了服务端内部的
Go 语言结构名。

差异其实只有两处，但足够致命：

| | `/v1/chat/completions` | Ollama `/api/chat` |
|---|---|---|
| `arguments` | JSON **字符串** `'{"path":"a.py"}'` | **对象** `{"path": "a.py"}` |
| 工具结果怎么对应到调用 | 用 `tool_call_id` | 用 `tool_name` |

### 3.5 第三件事，也是最要命的：只有一部分服务会检查

第 0 章 见过：历史里有一个"没人回应的调用"时，Ollama 返回 200 然后胡言乱语，
OpenAI 返回 400。这次把另外两种坏历史也试一遍：

| 坏历史 | Ollama | OpenAI |
|---|---|---|
| 有调用没结果 | 200 + 胡言乱语（"symbiotic with the user's request"） | 400 |
| 同一个 id 有两条结果 | 200 | 400 |
| 结果的 id 对不上任何调用 | 200 | 400 |

**三种坏历史，Ollama 全部放行，OpenAI 全部拒绝。**

测试方法很简单：手写一个坏掉的 `messages` 列表，直接发出去。比如"有调用没结果"：

```python
messages = [
    {"role": "user", "content": "What does a.py define?"},
    {"role": "assistant", "content": "", "tool_calls": [
        {"id": "call_probe01", "type": "function",
         "function": {"name": "read_file", "arguments": "{\"path\":\"a.py\"}"}}]},
    # no {"role": "tool", ...} message answering call_probe01
    {"role": "user", "content": "Go on."},
]
```

这个结果描述的是一种非常具体的失败：

> **你对着宽松的那家开发，一路绿灯；上线换成严格的那家，满屏 400。**
> 历史从头到尾都是坏的，只是宽松的那家从来不告诉你。

而 Ollama 那个 200 并不代表"能用"，它**产出了垃圾**：历史坏了，服务端不管，模型自己乱编。
这是 🟡 静默错误的典型样本。

---

## §4 测完了，再想会坏在哪

这一章的故障大部分不是"猜"出来的，是 §3 **直接测出来**的。按第 -1 章的规矩，先把它们
和"还没测到、但我担心的"一起列成表：

| 编号 | 问题 | 来源 | 怎么判断它被解决了 |
|---|---|---|---|
| F01-01 | OpenAI 的工具调用分十四片发，第 0 章的代码只留下最后一片，而且不崩 | §3.3 **已经测到** | 两家的录音产出同一个工具调用 |
| F01-04 | 历史存的是一家的格式，换一家两个方向都是 400 | §3.4 **已经测到** | 同一份历史能渲染成两种格式 |
| F01-02 | 有调用没结果：一家 200 胡言乱语，一家 400 | §3.5 **已经测到** | 这种历史在本地就发不出去 |
| F01-06 | 同一个 id 两条结果，或者结果对不上任何调用 | §3.5 **已经测到** | 同上 |
| F01-05 | 为了支持第二家，有人在循环里写 `if provider == "openai"` | 担心 | 循环的代码里不出现任何供应商相关的东西 |
| F01-03 | 以后不止一个地方会发请求，每个地方都可能忘了检查历史 | 担心 | 检查放在一个所有请求都必须经过的地方 |
| F01-07 | 代码加进历史的提示（比如第 0 章的剩余轮数提醒）和用户说的话混成同一种东西 | 担心 | 它有自己的类型 |

"已经测到"的四条，§5、§6 直接修。"担心"的三条是设计时的预防，§6、§7 会看到它们怎么被
挡住——它们在本章不会真的发生，要等以后的某一天有人写错。

> 这一章和第 0 章的区别值得注意：第 0 章先猜、再去撞；这一章先测，测出来的东西直接就是
> 故障。**能测的就不要猜。**

---

## §5 归一化：分歧到客户端为止

先解决 F01-01，工具调用的碎片。

### 5.1 先把 OpenAI 的回复也录进录音服务

要修一个 bug，先得能随时复现它。第 0 章的录音服务只会模仿 Ollama，现在让它也能模仿 OpenAI。
它不再只是"假 Ollama"了，所以先改个名：

```bash
git mv src/minicodex/stub_ollama.py src/minicodex/stub.py
```

> `git mv` 和普通的改名一样，只是同时告诉 git"这是同一个文件换了名字"，历史不会断。

然后在 `stub.py` 里加上 OpenAI 的录音。这是它的全部内容：

```python
"""Stand-ins for two servers that both claim to speak /v1/chat/completions.

Every chunk below is a verbatim copy of what Ollama actually returned for
`gemma4:31b` on 2026-08-06, ids and all.  It is not a model and does not
pretend to be one -- it replays those exact bytes so that client code, CI and
readers without a GPU all exercise the same wire format.

Two recordings, two shapes for the same tool call:

  OLLAMA_*  one chunk, arguments complete
  OPENAI_*  fourteen chunks, and only the first carries the id and the name

Run with `minicodex serve-stub`.  A request picks its recording with the
`stub_mode` key (`narrate`, `three`, `openai`), which tests send through
`extra_body`.  Or point at the real thing:

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
```

和第 0 章相比，新加的是：

> - **开头的 docstring**：说明现在有两种录音，以及怎么选（请求里的 `stub_mode`）。
> - **`_openai_call_first`**：第一片。`content` 是 `None`（OpenAI 发的是 `null`），
>   `tool_calls` 里有 `id` 和 `name`，但 `arguments` 是空字符串。
> - **`_openai_call_more`**：后面每一片，只有 `index` 和一小片 `arguments`，别的都没有。
> - **`"created"` 和 `"model"`** 是从 api.openai.com 录下的真实值，和 Ollama 录音的时间戳、
>   模型名区分开。
> - **`_OPENAI_ARG_SLICES`**：十三片参数的真实切法。注意 `minicodex` 这个词被切成了
>   `/min`、`ic`、`od`、`ex` 四片。
> - **`OPENAI_FRAGMENTED_CALL`**：第一片 + 十三片 + 结束片。**`OPENAI_FINAL_ANSWER`** 是把
>   文件内容交回去之后，OpenAI 的最终回答，复用了第 0 章的 `_text`。
> - **`_Handler.do_POST`**：多了 `mode == "openai"` 这个分支。"历史里有没有工具结果"这个判断
>   两个分支都要用，所以提成了一个变量 `answered`，免得某个分支忘了判断。

改了模块名，用到它的两个地方也要跟着改。`tests/conftest.py` 里：

```python
from minicodex import stub
```

和

```python
    server = HTTPServer(("127.0.0.1", port), stub._Handler)
```

`__main__.py` 的 `serve-stub` 分支里：

```python
        from minicodex.stub import serve
```

跑一下测试，确认改名没弄坏什么，然后提交：

```bash
git add .
git commit -m "test: add OpenAI's fragmented tool call to the stub"
```

### 5.2 让客户端能和 OpenAI 说话

第 0 章的 `OllamaModel` 只会连 Ollama：没有密钥，默认地址写死。改动几处：

**改名。** 它其实能连任何说 `/v1/chat/completions` 的服务，所以叫 `ChatCompletionsModel`。
两个默认地址也分开：

```python
OLLAMA_BASE_URL = "http://localhost:11434/v1"
OPENAI_BASE_URL = "https://api.openai.com/v1"
```

**加一个 `api_key` 参数：**

```python
class ChatCompletionsModel:
    """A client for any server speaking /v1/chat/completions."""

    def __init__(
        self,
        *,
        base_url: str = OLLAMA_BASE_URL,
        model: str = "gemma4:31b-cloud",
        api_key: str | None = None,
        tools: list[dict[str, Any]] | None = None,
        extra_body: dict[str, Any] | None = None,
        timeout: float = 300.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.api_key = api_key
        self.tools = tools or []
        self.extra_body = extra_body or {}
        self.timeout = timeout
```

**请求头：** 有密钥时加上 `Authorization`。

```python
    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers
```

`stream()` 里发请求时带上它：

```python
            async with client.stream(
                "POST", url, json=self.request_body(messages), headers=self._headers()
            ) as resp:
```

还有两处小改动：

> - `request_body` 和 `stream` 的参数名从 `history` 改成 `messages`。§6 会给"历史"一个专门的
>   类型，而这里收到的是已经整理好、准备发出去的**消息列表**。**名字要说实话。**
> - 出错时截取的响应内容从 500 字符放宽到 1000。OpenAI 的错误信息比 Ollama 的长，
>   §3.5 那条 400 的关键信息就在后半句。

**命令行加 `--provider`。** 这是 `__main__.py` 的全部内容：

```python
"""Command line entry point."""

from __future__ import annotations

import argparse
import asyncio
import os
import platform
import sys

from minicodex import __version__
from minicodex.agent import Agent
from minicodex.model import OLLAMA_BASE_URL, OPENAI_BASE_URL, ChatCompletionsModel
from minicodex.recorder import Recorder
from minicodex.tools import DEFAULT_TOOLS, TOOL_SCHEMAS

PROVIDERS = {
    "ollama": (OLLAMA_BASE_URL, "gemma4:31b-cloud"),
    "openai": (OPENAI_BASE_URL, "gpt-4o-mini"),
}


async def _ask(question: str, *, provider: str, base_url: str | None, model: str | None) -> int:
    default_url, default_model = PROVIDERS[provider]
    recorder = Recorder()
    llm = ChatCompletionsModel(
        base_url=base_url or default_url,
        model=model or default_model,
        # Read from the environment, never from a flag: a key in argv shows up
        # in shell history and in `ps`.
        api_key=os.environ.get("OPENAI_API_KEY") if provider == "openai" else None,
        tools=TOOL_SCHEMAS,
    )
    agent = Agent(llm, DEFAULT_TOOLS, recorder=recorder)

    result = await agent.run(question)

    print(result.final_text or "(no answer)")
    print(f"\n[{llm.model} | {result.stop_reason} after {result.turns_used} turn(s)]")
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
    ask.add_argument("--provider", choices=sorted(PROVIDERS), default="ollama")
    ask.add_argument("--base-url", default=None)
    ask.add_argument("--model", default=None)

    stub = sub.add_parser("serve-stub", help="replay recorded Ollama responses on a local port")
    stub.add_argument("--port", type=int, default=11435)

    args = parser.parse_args(argv)

    if args.version:
        print(f"minicodex {__version__}")
        print(f"python    {platform.python_version()} ({sys.platform})")
        return 0

    if args.command == "ask":
        return asyncio.run(
            _ask(
                args.question,
                provider=args.provider,
                base_url=args.base_url,
                model=args.model,
            )
        )

    if args.command == "serve-stub":
        from minicodex.stub import serve

        serve(args.port)
        return 0

    parser.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

> - **`PROVIDERS`**：一张表，`供应商名: (默认地址, 默认模型)`。加第三家供应商 = 在表里加一行。
> - **`choices=sorted(PROVIDERS)`**：`--provider` 只能填表里有的名字，填错了 argparse 自己会报错。
>   `sorted(字典)` 得到的是排好序的键列表。
> - **`--base-url` 和 `--model` 默认是 `None`**，在 `_ask` 里用 `base_url or default_url` 处理：
>   用户显式给了就用用户的（比如指向录音服务），没给就用这家供应商的默认值。
>   `None or "x"` 的结果是 `"x"`。
> - **密钥只从环境变量读，而且只在 `provider == "openai"` 时读。** 注释写明了为什么不用命令行参数。
> - 最后打印的模型名改成 `llm.model`，也就是实际用的那个，而不是用户可能没填的参数。

测试文件里用到旧类名的地方也要改：`tests/test_agent.py` 里所有的 `OllamaModel` 换成
`ChatCompletionsModel`（import 那一行、`model()` 辅助函数、两个测试里的包装类、
`test_request_body_can_be_inspected_without_a_network_call`）。编辑器的"全部替换"就能做到。

```bash
git add .
git commit -m "feat: talk to any chat-completions server; choose one with --provider"
```

现在 `--provider openai` 已经能发出请求了。**但 §3.3 那个 bug 还在**，下一步修它。

### 5.3 先让测试红

按第 0 章的做法：先写一个会失败的测试，证明 bug 存在，再修。在 `tests/test_agent.py` 末尾加：

```python
async def test_F01_01_openai_fragments_are_reassembled(stub_url: str) -> None:
    """Recorded from api.openai.com: fourteen chunks for one call, and only the
    first carries the id and the name.  Chapter 0 overwrote by index and was
    left with `name=''` and `arguments='"}'`."""
    events = [
        e
        async for e in ChatCompletionsModel(
            base_url=stub_url, extra_body={"stub_mode": "openai"}
        ).stream([{"role": "user", "content": "go"}])
    ]

    calls = [e for e in events if isinstance(e, ToolCallDelta)]

    assert len(calls) == 1, "fourteen chunks must arrive as one call"
    assert calls[0].name == "read_file"
    assert calls[0].call_id == "call_bqv6MLMr9BhXis7T4LB6tGqa"
    assert calls[0].arguments == '{"path":"src/minicodex/__init__.py"}'
```

> 它直接调用 `stream()`，让录音服务回放 OpenAI 的那段，然后断言：只收到**一个**工具调用，
> 名字、id、参数都完整。

跑一下，失败：现在的 `stream()` 每见到一个 `tool_calls` 就交出一个 `ToolCallDelta`，
所以收到的是十四个，第一个断言就不成立。

### 5.4 在哪里拼？

**关键的决定是：碎片在哪里拼起来？** 两个选择：

| 方案 | 后果 |
|---|---|
| 在 `agent.py` 的 `_collect` 里拼 | 供应商的怪癖漏进了循环。以后第三家有第三种怪癖，`_collect` 就会长成一堆 if |
| **在 `model.py` 的 `stream()` 里拼** | 怪癖到此为止。上面的代码永远只见到一种完整的 `ToolCallDelta` |

选第二个。这就是第 0 章 说的**信任边界**：外面进来的数据，在进门的地方整理成统一的样子。
**"协议层"这个词的实际含义，不是多一个类，而是划一条线，让不整齐的东西过不去。**

代价是 `stream()` 不能一见到 `tool_calls` 就交出去了——**它得等到 `[DONE]` 才知道一个调用
收完了**。文字仍然是边收边交（逐字打印不受影响），工具调用改成最后一次性交出。

### 5.5 一个"组装中"的类型

先定义一个只在 `model.py` 内部用的类型，表示"正在拼的一个工具调用"：

```python
@dataclass
class _PartialCall:
    """One tool call under construction.

    Mutable and private: it exists only between the first fragment and `[DONE]`.
    Nothing outside this module ever sees one.
    """

    index: int
    call_id: str | None = None
    name: str | None = None
    parts: list[str] = field(default_factory=list)

    def absorb(self, raw: dict[str, Any]) -> None:
        # Only the first fragment carries id and name; later ones carry neither,
        # so every field is written conditionally rather than assigned.
        if raw.get("id"):
            self.call_id = raw["id"]
        fn = raw.get("function") or {}
        if fn.get("name"):
            self.name = fn["name"]
        self.parts.append(fn.get("arguments") or "")

    def finish(self) -> ToolCallDelta:
        return ToolCallDelta(
            # A call with no id is a provider bug, not a shape to support: the
            # fallback keeps the pairing invariant satisfiable rather than
            # pretending the id was there.
            call_id=self.call_id or f"call_{self.index}",
            index=self.index,
            name=self.name or "",
            arguments="".join(self.parts),
        )
```

**为什么这里的 `@dataclass` 没有 `frozen=True`。** 第 0 章的几个类型都冻结了，因为它们是
"已经发生的事实"。这个不是——它是**正在拼装的半成品**，存在的全部意义就是被反复修改。
名字前面加 `_` 表示只在模块内部用，docstring 里写明"外面永远看不到它"。

**`parts: list[str] = field(default_factory=list)`**：和第 0 章 `RunResult` 一样，列表默认值
要用 `default_factory`。

**为什么每个字段都是"有才写"。** 这是整章最关键的三行。写 `if raw.get("id"):` 而不是
`self.call_id = raw.get("id")`，因为后面十三片**没有** `id`，直接赋值会把第一片拿到的 id
覆盖成 `None`。

**这正是第 0 章那个 bug 的形状**：第 0 章是整个对象被覆盖，这里如果写成直接赋值，就是一个个
字段被覆盖。**同一个错误的两种写法。**

**`self.parts.append(fn.get("arguments") or "")`**：每一片的参数都放进列表，最后
`"".join(...)` 拼起来。`or ""` 保证即使某片没有 `arguments`，放进列表的也是空字符串而不是 `None`
（`"".join` 遇到 `None` 会报错）。

**`finish()`**：拼好之后，变成一个普通的、冻结的 `ToolCallDelta`。`call_id` 缺失时仍然用
index 编一个，注释说明了原因：没有 id 是对方的 bug，不是要支持的形状；编一个是为了让后面
"每个调用都有结果"的检查还能进行下去。

### 5.6 `stream()` 怎么改

`stream()` 开头加一个字典，存所有"正在拼"的调用：

```python
        pending: dict[int, _PartialCall] = {}
```

遇到 `tool_calls` 时不再立刻交出，而是交给对应的半成品去"吸收"：

```python
                    for raw in delta.get("tool_calls") or []:
                        index = raw.get("index", 0)
                        pending.setdefault(index, _PartialCall(index)).absorb(raw)
```

收到 `[DONE]` 时，才把拼好的调用一个个交出去：

```python
                    if payload == "[DONE]":
                        # Tool calls are emitted here, not as they arrive: only
                        # now is every fragment known to have been received.
                        for index in sorted(pending):
                            yield pending[index].finish()
                        yield Completed(finish_reason)
                        return
```

**`setdefault` 那一行**：这个 index 没见过，就新建一个半成品放进去；见过，就取出已有的那个。
然后让它吸收这一片。**这一行同时处理了"一片发完"和"十四片发完"两种情况——因为一片也是
"第一片"。**

改完之后 `model.py` 的全部内容：

```python
"""Talking to a model over HTTP, and normalising what comes back.

Two providers, both speaking "the OpenAI chat completions API", disagree about
how a tool call arrives.  Recorded 2026-08-06:

    Ollama (gemma4:31b) -- one chunk, arguments complete:
      "tool_calls":[{"id":"call_yfo64477","index":0,"type":"function",
                     "function":{"name":"read_file",
                                 "arguments":"{\"path\":\"a.py\"}"}}]

    OpenAI (gpt-4o-mini) -- fourteen chunks, and only the first carries the
    id and the name:
      "tool_calls":[{"index":0,"id":"call_bqv6...","type":"function",
                     "function":{"name":"read_file","arguments":""}}]
      "tool_calls":[{"index":0,"function":{"arguments":"{\""}}]
      "tool_calls":[{"index":0,"function":{"arguments":"path"}}]
      ...
      "tool_calls":[{"index":0,"function":{"arguments":"\"}"}}]

Chapter 0 assumed the first shape and overwrote by index, which against OpenAI
leaves `name=""` and `arguments='"}'`.

The fix is not to make callers handle both.  It is to make the difference stop
here: fragments are buffered inside `stream()` and a `ToolCallDelta` is emitted
only once it is whole.  Everything above this module sees one shape.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass, field
from typing import Any

import httpx

OLLAMA_BASE_URL = "http://localhost:11434/v1"
OPENAI_BASE_URL = "https://api.openai.com/v1"


@dataclass(frozen=True)
class TextDelta:
    """A slice of prose.  Not a token, not a word -- whatever the server sent."""

    text: str


@dataclass(frozen=True)
class ToolCallDelta:
    """One complete tool call, assembled.

    Still called `Delta` because that is the field it arrives in.  It is emitted
    only when the whole call has been received, however many chunks that took.
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


@dataclass
class _PartialCall:
    """One tool call under construction.

    Mutable and private: it exists only between the first fragment and `[DONE]`.
    Nothing outside this module ever sees one.
    """

    index: int
    call_id: str | None = None
    name: str | None = None
    parts: list[str] = field(default_factory=list)

    def absorb(self, raw: dict[str, Any]) -> None:
        # Only the first fragment carries id and name; later ones carry neither,
        # so every field is written conditionally rather than assigned.
        if raw.get("id"):
            self.call_id = raw["id"]
        fn = raw.get("function") or {}
        if fn.get("name"):
            self.name = fn["name"]
        self.parts.append(fn.get("arguments") or "")

    def finish(self) -> ToolCallDelta:
        return ToolCallDelta(
            # A call with no id is a provider bug, not a shape to support: the
            # fallback keeps the pairing invariant satisfiable rather than
            # pretending the id was there.
            call_id=self.call_id or f"call_{self.index}",
            index=self.index,
            name=self.name or "",
            arguments="".join(self.parts),
        )


class ChatCompletionsModel:
    """A client for any server speaking /v1/chat/completions."""

    def __init__(
        self,
        *,
        base_url: str = OLLAMA_BASE_URL,
        model: str = "gemma4:31b-cloud",
        api_key: str | None = None,
        tools: list[dict[str, Any]] | None = None,
        extra_body: dict[str, Any] | None = None,
        timeout: float = 300.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.api_key = api_key
        self.tools = tools or []
        self.extra_body = extra_body or {}
        self.timeout = timeout

    def request_body(self, messages: Sequence[dict[str, Any]]) -> dict[str, Any]:
        """The exact JSON that will be posted.

        Separate from `stream()` so it can be printed, recorded, diffed and
        asserted on without making a network call.
        """
        body: dict[str, Any] = {
            "model": self.model,
            "messages": list(messages),
            "stream": True,
        }
        if self.tools:
            body["tools"] = self.tools
        body.update(self.extra_body)
        return body

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers

    async def stream(self, messages: Sequence[dict[str, Any]]) -> AsyncIterator[StreamEvent]:
        url = f"{self.base_url}/chat/completions"
        finish_reason: str | None = None
        pending: dict[int, _PartialCall] = {}

        async with httpx.AsyncClient(timeout=self.timeout) as client:
            async with client.stream(
                "POST", url, json=self.request_body(messages), headers=self._headers()
            ) as resp:
                if resp.status_code != 200:
                    detail = (await resp.aread()).decode("utf-8", "replace")[:1000]
                    raise ModelHTTPError(f"HTTP {resp.status_code} from {url}: {detail}")

                async for line in resp.aiter_lines():
                    if not line.strip() or not line.startswith("data: "):
                        continue
                    payload = line[len("data: ") :]

                    if payload == "[DONE]":
                        # Tool calls are emitted here, not as they arrive: only
                        # now is every fragment known to have been received.
                        for index in sorted(pending):
                            yield pending[index].finish()
                        yield Completed(finish_reason)
                        return

                    chunk = json.loads(payload)
                    choice = chunk["choices"][0]
                    if choice.get("finish_reason"):
                        finish_reason = choice["finish_reason"]

                    delta = choice.get("delta") or {}
                    # Ollama sends "" on tool-call chunks, OpenAI sends null.
                    # Truthiness covers both; membership would not.
                    if delta.get("content"):
                        yield TextDelta(delta["content"])
                    for raw in delta.get("tool_calls") or []:
                        index = raw.get("index", 0)
                        pending.setdefault(index, _PartialCall(index)).absorb(raw)
```

> 文件开头的 docstring 换成了本章测到的内容：两家服务、两种形状，以及为什么在这里拼。

**注意 `agent.py` 的 `_collect` 一行都没改。** 它仍然写着 `by_index[event.index] = event`，
而现在这个覆盖是安全的，因为客户端保证了每个 index 只交出一次。

这是分层的直接好处：**改动被关在一个模块里。** 如果当初选择在 `_collect` 里拼，这次就得
同时改 `model.py` 和 `agent.py`。

§5.3 的测试现在通过了。再加两个测试，把"两家看起来一样"这件事钉住：

```python
async def test_F01_01_both_providers_produce_the_same_tool_call(stub_url: str) -> None:
    """The whole point of normalising at the boundary: above `model.py`, the two
    recordings are indistinguishable."""

    async def call_from(mode: str) -> ToolCallDelta:
        events = [
            e
            async for e in ChatCompletionsModel(
                base_url=stub_url, extra_body={"stub_mode": mode}
            ).stream([{"role": "user", "content": "go"}])
        ]
        return next(e for e in events if isinstance(e, ToolCallDelta))

    ollama = await call_from("narrate")
    openai = await call_from("openai")

    assert ollama.name == openai.name == "read_file"
    assert json.loads(ollama.arguments) == json.loads(openai.arguments)


async def test_F01_01_a_fragmented_call_runs_the_right_tool(stub_url: str) -> None:
    """End to end: the agent does not know or care which provider it is."""
    log: list[str] = []
    agent = Agent(model(stub_url, stub_mode="openai"), make_tools(log))

    result = await agent.run("What does src/minicodex/__init__.py define?")

    assert log == ["read_file:src/minicodex/__init__.py"]
    assert result.stop_reason == "completed"
```

> - **第一个**：分别从两段录音里取出工具调用，断言名字相同、参数解析后相同。注意最后一行比的是
>   `json.loads(...) == json.loads(...)`，而不是直接比字符串——两家的空格、转义可能不同，
>   我关心的是"解析出来是同一个东西"。
> - `next(e for e in events if ...)`：从列表里取出第一个满足条件的元素。
> - `async def call_from(...)` 定义在测试函数**里面**：一个只在这个测试里用的小辅助函数。
> - **第二个**：端到端。用真正的 `Agent` 对着 OpenAI 的录音跑一遍，断言调用的是正确的工具。
>   docstring 那句话就是这一节的目标："Agent 不知道、也不关心对面是哪家。"

提交。这是一个 `fix`，而不是 `feat`：

```bash
git add .
git commit
```

```
fix: reassemble tool-call fragments before emitting them

OpenAI sends one tool call as fourteen chunks, and only the first carries
the id and the name. Chapter 0 emitted a ToolCallDelta per chunk and the
agent overwrote by index, leaving name='' and arguments='"}' -- and it did
not crash: the loop reported "no tool named ''" to the model and carried on.

Fragments are now buffered inside stream() and each call is emitted once,
on [DONE]. Nothing above model.py changed.

This is the review comment deferred in chapter 0 ("if a provider really
fragments arguments, the overwrite silently loses data"), now with a
measurement instead of a guess.
```

> **为什么是 `fix`？** 因为这个 bug 在第 0 章就存在，只是当时没有第二家供应商，没人知道。
> **"当时没暴露"不等于"当时不是 bug"。**
>
> **最后一段指回了第 0 章那条被延后的审查意见。** 一条被延后的意见兑现时要回指原文，
> 这样 `git log` 里能看出当初的延后是负责任的决定，而不是忘了。

---

## §6 历史：存事实，不存 JSON

碎片解决了，但 §3.4（方言不同）和 §3.5（只有一部分服务会检查）还在。两个都出在同一个
地方：第 0 章的 `history` 是一个 `list[dict]`，而且 dict 长成了 `/v1` 的格式。

第 0 章 说过，历史先不定专门的类型，"等有了具体理由再改"。**现在理由来了，而且有两个。**

### 6.1 这一次为什么值得加一层

第 0 章 列了三种值得加抽象的情况：以后改代价极高、外部数据进来的地方、今天就有多种实现。
这一章遇到了**第四种**：

> **4. 有一条规则必须一直成立，有不止一处代码可能破坏它，而外面没有人替你检查。**

这里的规则是：**每个发出的工具调用，都恰好有一个结果。** 第 0 章用一个手写的测试断言来守它。
而 §3.5 刚测出来：宽松的服务根本不检查，坏了也照样返回 200。**不能指望服务端，只能自己守**，
而且要守在代码里，不是守在测试里。

§3.4 则是第二种情况（信任边界）的另一面：历史最终要**发出去**，发给说不同方言的服务。

### 6.2 先想清楚要存什么

第 0 章存的是 dict，因为下一轮要原样发回去。这个理由在只有一家服务时成立，现在不成立了——
**同一段对话，两家要的格式不一样。**

那存什么？**存"发生了什么"，而不是"该怎么发出去"。**

新建 `src/minicodex/history.py`。先是开头：

```python
"""The conversation, stored as facts rather than as JSON.

Chapter 0 kept the history as a list of dicts in one provider's wire format.
Two things went wrong with that, both measured rather than imagined:

* **The dialects differ.**  Ollama's native API wants `arguments` as an object
  and matches results by `tool_name`; the chat-completions API wants a string
  and matches by `tool_call_id`.  Sending one to the other is a 400 either way,
  and the errors are unhelpful:
  `Value looks like object, but can't find closing '}' symbol`.

* **Only some servers check the invariants.**  Recorded 2026-08-06: a history
  containing an assistant `tool_calls` with no matching result was answered by
  Ollama with HTTP 200 and the words "symbiotic with the user's request", and
  rejected by OpenAI with HTTP 400.  Duplicate results and results with unknown
  ids: the same split.

So the rules cannot be delegated to the server.  `History` refuses to build an
invalid conversation in the first place, and turns into a specific dialect only
at the moment of sending.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from typing import Any, Literal

from minicodex.agent_types import ToolCall

Dialect = Literal["chat_completions", "ollama_native"]

```

> - docstring 记下了 §3.4、§3.5 测到的事实，以及由此得出的结论。
> - **`Dialect = Literal["chat_completions", "ollama_native"]`**：方言只能是这两个字符串之一。
>   `Literal` 只是类型标注，运行时并不检查，但编辑器和类型检查器会在你写错时提醒。
> - `from minicodex.agent_types import ToolCall`——这一行先放一放，§7 会讲它为什么长这样。

一次对话里只会发生四种事，一种一个类型：

```python
@dataclass(frozen=True)
class UserMessage:
    text: str


@dataclass(frozen=True)
class AssistantMessage:
    text: str
    tool_calls: tuple[ToolCall, ...] = ()


@dataclass(frozen=True)
class ToolResult:
    call_id: str
    name: str
    content: str


@dataclass(frozen=True)
class SystemNote:
    """Something the code knows and the model does not.

    Separate from `UserMessage` because the user did not say it.  Chapter 0's
    turn-budget warning was the first; chapters 5, 6 and 11 add more.
    """

    text: str


HistoryItem = UserMessage | AssistantMessage | ToolResult | SystemNote


class HistoryError(RuntimeError):
    """An operation that would have produced a conversation no server accepts."""
```

和第 0 章那三个事件类型是同一个思路：**一件事一个类型，不合理的状态写不出来。**

**`SystemNote` 值得单说。** 第 0 章的剩余轮数提醒是这么写的：

```python
history.append({"role": "system", "content": "You have 1 turn(s) left..."})
```

`role: "system"` 是**发给服务端时用的角色名**。但它在概念上是什么？是**代码知道、模型不知道的
状态**——第 0 章 已经点出了这个模式。给它一个自己的类型，是因为它和用户说的话本来就是
两回事，而且**将来发送的方式可能不一样**：有的服务没有 `system` 角色，得换一种方式塞进去；
有的要求 system 消息必须放在最前面。这就是 §4 表里的 F01-07。

**`ToolResult` 里为什么有 `name`？** 发给 `/v1` 时用不上（那边用 `call_id` 对应），但发给
Ollama 原生接口时**必须有**（那边用 `tool_name` 对应）。**一个字段只被一种方言用，这正是
"存事实"的意思**：事实是"`read_file` 这个工具返回了这段内容"，至于怎么对应到调用，是方言的事。

**`HistoryItem`** 是四种之一的类型别名；**`HistoryError`** 是违反规则时抛出的异常。

### 6.3 一个会拒绝的容器

四个类型只能"表示"，不能"阻止"。§3.5 那三种坏历史，用这四个类型照样能拼出来。所以再要一个
容器，**它的职责就是拒绝**：

```python
class History:
    """An append-only conversation that cannot be put into an invalid state.

    The invariant, in one sentence: **every tool call issued by the assistant is
    answered exactly once, before the next request goes out.**

    It is enforced on the way in rather than checked on the way out, so the
    traceback points at the code that broke it instead of at a serialiser three
    layers away.
    """

    def __init__(self) -> None:
        self._items: list[HistoryItem] = []
        # call_id -> the call awaiting an answer.  Ordered, so the error message
        # can name them in the order the model asked.
        self._unanswered: dict[str, ToolCall] = {}

    # -- reading ------------------------------------------------------------

    def __len__(self) -> int:
        return len(self._items)

    def __iter__(self) -> Iterator[HistoryItem]:
        return iter(self._items)

    @property
    def items(self) -> tuple[HistoryItem, ...]:
        return tuple(self._items)

    def unanswered(self) -> tuple[ToolCall, ...]:
        return tuple(self._unanswered.values())

    # -- writing ------------------------------------------------------------

    def add_user(self, text: str) -> None:
        self._items.append(UserMessage(text))

    def add_system_note(self, text: str) -> None:
        self._items.append(SystemNote(text))

    def add_assistant(self, text: str, tool_calls: Sequence[ToolCall] = ()) -> None:
        if self._unanswered:
            raise HistoryError(
                "the assistant cannot speak again while calls are unanswered: "
                + ", ".join(sorted(self._unanswered))
            )
        for call in tool_calls:
            if call.call_id in self._unanswered:
                raise HistoryError(f"duplicate call_id in one turn: {call.call_id}")
            self._unanswered[call.call_id] = call
        self._items.append(AssistantMessage(text, tuple(tool_calls)))

    def add_tool_result(self, call_id: str, content: str) -> None:
        call = self._unanswered.pop(call_id, None)
        if call is None:
            raise HistoryError(
                f"no unanswered call with id {call_id!r}; "
                f"awaiting {sorted(self._unanswered) or '(none)'}"
            )
        self._items.append(ToolResult(call_id, call.name, content))

    # -- sending ------------------------------------------------------------

    def to_wire(self, dialect: Dialect = "chat_completions") -> list[dict[str, Any]]:
        """Render for one provider, refusing to render something invalid.

        The check lives here because this is the last moment before the bytes
        leave: whatever path built the history, it passes through this door.
        """
        if self._unanswered:
            raise HistoryError(
                "refusing to send: tool calls with no result: "
                + ", ".join(f"{c.call_id} ({c.name})" for c in self._unanswered.values())
            )
        return [_render(item, dialect) for item in self._items]
```

一段一段看。

**`__init__`：两个私有字段。** `_items` 是所有记录；**`_unanswered` 是核心**——"还欠着的账"，
键是 `call_id`，值是那个调用。字典会保持插入顺序，所以报错时能按模型提出的顺序列出它们。

**读取的几个方法：**

> - `__len__` 让 `len(history)` 能用；`__iter__` 让 `for item in history:` 能用。
>   这两个以双下划线开头结尾的方法叫**特殊方法**，Python 在对应的场合自动调用它们。
> - **`items` 是一个 `@property`**：写 `history.items` 就能读取，返回一个**元组**——
>   外面拿到的是一份不能修改的副本，改不到内部的列表。
> - `unanswered()` 返回还欠着的调用，测试会用它检查"最后没有欠账"。

**`add_user` 和 `add_system_note`**：直接追加，没什么可检查的。

**`add_assistant`：模型说话。** 两个检查：

> - 如果还有欠账，就不允许模型再说话。这对应的是"循环写错了，某一轮忘了执行工具"——
>   这种错连服务端都查不出来，因为它发生在请求发出之前。
> - 同一轮里两个调用用了同一个 id，拒绝。

检查通过后，把这一轮的每个调用都记进 `_unanswered`，再追加记录。

**`add_tool_result`：交回一个工具结果。**

```python
        call = self._unanswered.pop(call_id, None)
        if call is None:
            raise HistoryError(...)
```

`pop` 一步完成了两件事：取出这个 id 对应的调用，并从欠账里删掉。如果取不到——要么这个 id
从没出现过，要么已经回应过一次了——就报错，而且**报错信息会列出它实际在等哪些 id**。

> 这比先 `if call_id not in self._unanswered: raise` 再 `del` 少写一步，也不会出现"检查了却
> 忘了删"的情况。（在 asyncio 里，两种写法都不会被别的任务打断，因为中间没有 `await`。
> 选 `pop` 是因为它更短、更不容易写错，不是因为并发。）

逐条对应 §3.5 测到的三种坏历史：

| §3.5 的坏历史 | 谁拦住它 |
|---|---|
| 有调用没结果 | `to_wire()` 拒绝渲染（见下） |
| 同一个 id 两条结果 | 第二次 `pop` 取不到，报错 |
| 结果的 id 对不上任何调用 | 同上，而且报错会说**它在等哪些 id** |

**`to_wire`：发送之前的最后一道门。** 还有欠账，就拒绝生成要发出去的消息列表；否则把每条记录
翻译成指定的方言。

**为什么检查放在 `to_wire` 里，而不是循环里？** 因为以后发请求的不止 `run()` 这一条路：
压缩历史之后要重发，程序崩溃恢复之后要重发，子 Agent 也要发。**每条路都可能忘记检查一次，
但每条路都必须经过 `to_wire()`。** 这就是 §4 表里的 F01-03。

> **一条通用的判断：把检查放在所有路径都必须经过的地方，而不是你今天想得到的那条路上。**
> 这个地方通常就是"数据离开你控制范围的那一刻"。

报错信息里带上工具名（`call_1 (read_file)`），而不只是 id，因为
`call_bqv6MLMr9BhXis7T4LB6tGqa` 这种 id 你看不出是什么。第 0 章说"错误信息就是给模型的提示"，
这里是同一个道理，只不过这次读的人是你自己。

docstring 里还有一句值得注意："在写入时强制，而不是在读出时检查"。这样出错时，报错的位置
就是**写错的那一行代码**，而不是三层之外的某个序列化函数。

### 6.4 翻译：`_render`

```python
def _render(item: HistoryItem, dialect: Dialect) -> dict[str, Any]:
    if isinstance(item, UserMessage):
        return {"role": "user", "content": item.text}

    if isinstance(item, SystemNote):
        return {"role": "system", "content": item.text}

    if isinstance(item, AssistantMessage):
        message: dict[str, Any] = {"role": "assistant", "content": item.text}
        if item.tool_calls:
            if dialect == "ollama_native":
                message["tool_calls"] = [
                    {"function": {"name": c.name, "arguments": c.arguments or {}}}
                    for c in item.tool_calls
                ]
            else:
                message["tool_calls"] = [
                    {
                        "id": c.call_id,
                        "type": "function",
                        "function": {"name": c.name, "arguments": c.raw_arguments},
                    }
                    for c in item.tool_calls
                ]
        return message

    if isinstance(item, ToolResult):
        if dialect == "ollama_native":
            return {"role": "tool", "tool_name": item.name, "content": item.content}
        return {"role": "tool", "tool_call_id": item.call_id, "content": item.content}

    raise AssertionError(f"unrenderable history item: {item!r}")  # pragma: no cover
```

**为什么是一个独立的函数，而不是每个类型上各有一个 `to_wire()` 方法？** 因为方言不属于消息本身。
`UserMessage` 表示"用户说了这句话"，它不该知道 Ollama 原生接口长什么样。**把翻译放在外面，
四个数据类型就和协议完全无关**——以后加第三种方言，改这一个函数，四个类型一行不动。

**`c.arguments or {}` 和 `c.raw_arguments`**：原生方言要对象，就用解析好的 `arguments`
（解析失败时是 `None`，换成空字典）；`/v1` 方言要字符串，就用 `raw_arguments`——**发回去的
必须是模型原本发来的那个字符串**，而不是我们解析后再转回去的版本（空格和键的顺序都可能变，
有的服务会因此对不上）。第 0 章留 `raw_arguments` 是为了在错误信息里回显，现在它有了第二个用处。

**最后一行 `raise AssertionError(...)`**：正常情况下永远不会执行。留着它，是为了防止将来有人
加了第五种 `HistoryItem`、却忘了在这里加分支——那时会立刻报错，而不是悄悄返回 `None`，
然后在发请求时报一个莫名其妙的错。`# pragma: no cover` 告诉覆盖率工具不用统计这一行。

### 6.5 循环怎么改

`agent.py` 里 `run()` 的改动比想象的小：

```python
        history = History()
        history.add_user(user_message)
        ...
            if remaining <= BUDGET_WARNING_AT:
                history.add_system_note(
                    f"You have {remaining} tool-calling turn(s) left. "
                    "Wrap up and give your best answer now."
                )

            # to_wire() refuses to render a history with unanswered calls, so a
            # loop that forgets to answer one fails here -- locally, with the
            # offending ids named -- instead of as a 400 from whichever provider
            # happens to be strict.
            messages = history.to_wire(self.dialect)
            self.recorder.record("request", {"turn": turn_index, "messages": messages})

            turn = await self._collect(self.model.stream(messages))
            ...
            history.add_assistant(turn.text, turn.tool_calls)
            ...
            for call in turn.tool_calls:
                output = await self._run_tool(call)
                history.add_tool_result(call.call_id, output)
```

三处 `history.append({...})` 变成了三个有名字的方法。**代码量差不多，但现在不可能拼错键名了**——
第 0 章里要是把 `"tool_call_id"` 写成 `"tool_id"`，要一路跑到服务端才会发现。

其他几处跟着改：

> - `Agent.__init__` 加一个参数 `dialect: Dialect = "chat_completions"`，存成 `self.dialect`。
> - `RunResult.history` 的类型从 `list[dict]` 变成 `History`，默认值 `field(default_factory=History)`。
> - `Model` Protocol 的 `stream` 参数名从 `history` 改成 `messages`，和 `model.py` 一致。
> - 记录器里记的键从 `"history"` 改成 `"messages"`，因为记的确实是翻译后的消息列表，不是历史本身。
>   **名字要说实话。**

在 `agent.py` 的 import 里加上：

```python
from minicodex.history import Dialect, History
```

然后跑一下——

---

## §7 意外：循环 import

```
ImportError: cannot import name 'ToolCall' from partially initialized module
'minicodex.agent' (most likely due to a circular import)
```

`history.py` 需要 `ToolCall`，而 `ToolCall` 定义在 `agent.py` 里，所以 `history.py` 写了
`from minicodex.agent import ToolCall`。而 `agent.py` 又需要 `History`。**两个模块互相 import。**

> **为什么会报错？** Python 执行 `import minicodex.agent` 时，从上往下运行 `agent.py`。
> 运行到 `from minicodex.history import ...` 这一行，就暂停下来去运行 `history.py`。
> `history.py` 又要 `from minicodex.agent import ToolCall`——可 `agent.py` 才运行到一半，
> `ToolCall` 还没定义出来。报错里的 "partially initialized module"（初始化到一半的模块）
> 说的就是这个。

这是本章没猜到的一个问题（F01-08）。三种常见的解法：

| 解法 | 评价 |
|---|---|
| 把 import 写到函数内部 | 能跑，但把问题藏起来了 |
| 只在类型检查时 import（`TYPE_CHECKING`） | 只解决"类型标注里用到"的情况，这里运行时真的要用 `ToolCall` |
| **把共享的东西往下移** | 新建一个谁都不依赖的模块 |

选第三个。新建 `src/minicodex/agent_types.py`：

```python
"""Types shared by the agent and the history.

Extracted from `agent.py` for one reason only: `history.py` needs `ToolCall`,
and `agent.py` needs `History`.  Leaving `ToolCall` where it was would make the
two modules import each other.

This is the smallest possible answer to a circular import -- move the shared
thing down, not sideways.  Interlude B deals with a case where the answer is
not this easy.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ToolCall:
    """One tool call, in the form the agent can act on.

    `arguments` is the parsed object, or None if the model sent something that
    was not a JSON object.  `raw_arguments` is what it actually sent, kept so
    the error message can quote it back -- and, since chapter 1, so the history
    can be re-serialised without a lossy round trip.
    """

    call_id: str
    name: str
    arguments: dict[str, Any] | None
    raw_arguments: str
```

它只有一个类。把 `ToolCall` 从 `agent.py` 里删掉，`agent.py` 和 `history.py` 都改成从这里
import：

```python
from minicodex.agent_types import ToolCall
```

> **循环 import 是一个信号，不只是一个麻烦。** 它在说："这两个模块共享了某个概念，而这个概念
> 没有自己的位置。"找到那个概念，给它一个不依赖任何人的家，循环自然消失。
>
> 这个文件二十多行 docstring、只有一个类。那段 docstring 正是它存在的全部理由：
> **一个只有一个类的模块，如果不解释为什么单独存在，下一个人一定会把它合并回去。**

改完之后 `agent.py` 的全部内容：

```python
"""The loop: ask, run what it asks for, feed the results back, ask again.

Everything lives in this one module on purpose.  The boundaries between stream
assembly, tool dispatch and the loop are guesses right now, and a boundary in
the wrong place is harder to remove than no boundary.  Interlude A pays this
off, once there are enough call sites for the boundaries to be observed rather
than invented.

Async is decided here rather than later: an agent spends nearly all its wall
clock waiting on IO, and converting a synchronous call chain to async in Python
means touching every caller on the path.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

from minicodex.agent_types import ToolCall
from minicodex.history import Dialect, History
from minicodex.model import Completed, StreamEvent, TextDelta, ToolCallDelta
from minicodex.recorder import NULL_RECORDER, Recorder


class IncompleteStreamError(RuntimeError):
    """The connection ended before the server sent its `[DONE]` sentinel."""


@dataclass(frozen=True)
class ModelTurn:
    text: str
    tool_calls: tuple[ToolCall, ...]
    finish_reason: str | None = None


class Model(Protocol):
    """Anything that can stream a response given a history.

    A Protocol rather than a base class: structural typing, no inheritance, so
    the cost of the abstraction is close to zero.  It earns its place because
    swapping the model is a requirement today -- the tests need a deterministic
    one -- not a guess about tomorrow.
    """

    def stream(self, messages: Sequence[dict[str, Any]]) -> AsyncIterator[StreamEvent]: ...


ToolFn = Callable[[dict[str, Any]], Awaitable[str]]


@dataclass
class RunResult:
    final_text: str
    stop_reason: str  # "completed" | "turn_limit"
    turns_used: int
    history: History = field(default_factory=History)


DEFAULT_MAX_TURNS = 12
BUDGET_WARNING_AT = 2


class Agent:
    def __init__(
        self,
        model: Model,
        tools: dict[str, ToolFn] | None = None,
        *,
        max_turns: int = DEFAULT_MAX_TURNS,
        recorder: Recorder = NULL_RECORDER,
        dialect: Dialect = "chat_completions",
    ) -> None:
        self.model = model
        self.tools = tools or {}
        self.max_turns = max_turns
        self.recorder = recorder
        self.dialect = dialect

    # -- assembling one response --------------------------------------------

    async def _collect(self, stream: AsyncIterator[StreamEvent]) -> ModelTurn:
        """Turn a stream of events into one finished turn, or refuse to.

        Two accumulators because the wire has two granularities: prose arrives
        one token at a time and gets joined, tool calls arrive whole and get
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

    # -- running one tool ----------------------------------------------------

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

    # -- the loop ------------------------------------------------------------

    async def run(self, user_message: str) -> RunResult:
        history = History()
        history.add_user(user_message)
        final_text = ""

        for turn_index in range(self.max_turns):
            remaining = self.max_turns - turn_index

            # A budget the model cannot see is one it spends freely and is then
            # killed by, mid-thought, with nothing to show.
            if remaining <= BUDGET_WARNING_AT:
                history.add_system_note(
                    f"You have {remaining} tool-calling turn(s) left. "
                    "Wrap up and give your best answer now."
                )

            # to_wire() refuses to render a history with unanswered calls, so a
            # loop that forgets to answer one fails here -- locally, with the
            # offending ids named -- instead of as a 400 from whichever provider
            # happens to be strict.
            messages = history.to_wire(self.dialect)
            self.recorder.record("request", {"turn": turn_index, "messages": messages})

            turn = await self._collect(self.model.stream(messages))
            self.recorder.record(
                "response",
                {
                    "turn": turn_index,
                    "text": turn.text,
                    "finish_reason": turn.finish_reason,
                    "tool_calls": [{"id": c.call_id, "name": c.name} for c in turn.tool_calls],
                },
            )

            history.add_assistant(turn.text, turn.tool_calls)
            if turn.text:
                final_text = turn.text

            # Text is not a stop signal.  Models narrate before acting, and
            # stopping on the narration leaves the work undone while looking
            # exactly like success.
            if not turn.tool_calls:
                return RunResult(final_text, "completed", turn_index + 1, history)

            # Every call runs and every call is answered.  add_tool_result is
            # what makes "answered" mean something: it refuses an id that was
            # never issued, and refuses to answer the same id twice.
            for call in turn.tool_calls:
                output = await self._run_tool(call)
                history.add_tool_result(call.call_id, output)

        return RunResult(final_text, "turn_limit", self.max_turns, history)
```

注意 `_collect` 和 `_run_tool` 一行都没动。这一章 `agent.py` 的改动只有：import、`ToolCall`
搬走、`dialect` 参数、`RunResult.history` 的类型、`run()` 的正文。

### 7.1 历史的测试

新建 `tests/test_history.py`，一个测试对应一种必须拒绝的状态：

```python
"""What the history must refuse, pinned so it stays refused.

Fault IDs match FAULTS.md.  Each of these was answered with HTTP 200 by Ollama
and HTTP 400 by OpenAI on 2026-08-06; the point of this file is that neither
answer is needed, because the history never gets into that state.
"""

from __future__ import annotations

import pytest

from minicodex.agent_types import ToolCall
from minicodex.history import History, HistoryError

CALL = ToolCall("call_1", "read_file", {"path": "a.py"}, '{"path":"a.py"}')
CALL2 = ToolCall("call_2", "read_file", {"path": "b.py"}, '{"path":"b.py"}')


def answered_history() -> History:
    h = History()
    h.add_user("what is in a.py?")
    h.add_assistant("Let me look.", [CALL])
    h.add_tool_result("call_1", "contents")
    return h


# ---------------------------------------------------------------------------
# F01-02  an issued call with no result
# ---------------------------------------------------------------------------


def test_F01_02_unanswered_call_blocks_the_next_request() -> None:
    h = History()
    h.add_user("go")
    h.add_assistant("Let me look.", [CALL])

    with pytest.raises(HistoryError) as excinfo:
        h.to_wire()

    assert "call_1 (read_file)" in str(excinfo.value)


def test_F01_02_the_error_names_every_unanswered_call() -> None:
    """OpenAI's own message names them; ours does too, one turn earlier."""
    h = History()
    h.add_user("go")
    h.add_assistant("", [CALL, CALL2])
    h.add_tool_result("call_1", "contents")

    with pytest.raises(HistoryError) as excinfo:
        h.to_wire()

    assert "call_2" in str(excinfo.value)
    assert "call_1" not in str(excinfo.value)


def test_F01_02_the_assistant_cannot_speak_over_an_unanswered_call() -> None:
    h = History()
    h.add_user("go")
    h.add_assistant("", [CALL])

    with pytest.raises(HistoryError, match="unanswered"):
        h.add_assistant("ignoring that, then")


# ---------------------------------------------------------------------------
# F01-06  the same call answered twice
# ---------------------------------------------------------------------------


def test_F01_06_a_call_cannot_be_answered_twice() -> None:
    h = answered_history()

    with pytest.raises(HistoryError, match="no unanswered call"):
        h.add_tool_result("call_1", "different")


def test_F01_06_a_result_for_an_unknown_id_is_refused() -> None:
    h = History()
    h.add_user("go")
    h.add_assistant("", [CALL])

    with pytest.raises(HistoryError) as excinfo:
        h.add_tool_result("call_NOPE", "stray")

    assert "call_NOPE" in str(excinfo.value)
    assert "call_1" in str(excinfo.value), "the error should say what it was waiting for"


def test_F01_06_two_calls_with_the_same_id_in_one_turn_are_refused() -> None:
    h = History()
    h.add_user("go")

    with pytest.raises(HistoryError, match="duplicate call_id"):
        h.add_assistant("", [CALL, CALL])


# ---------------------------------------------------------------------------
# F01-04  the history was stored in one provider's dialect
# ---------------------------------------------------------------------------


def test_F01_04_chat_completions_dialect() -> None:
    wire = answered_history().to_wire("chat_completions")

    assert wire == [
        {"role": "user", "content": "what is in a.py?"},
        {
            "role": "assistant",
            "content": "Let me look.",
            "tool_calls": [
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": "read_file", "arguments": '{"path":"a.py"}'},
                }
            ],
        },
        {"role": "tool", "tool_call_id": "call_1", "content": "contents"},
    ]


def test_F01_04_ollama_native_dialect() -> None:
    """Arguments as an object, results matched by name -- verified 2026-08-06."""
    wire = answered_history().to_wire("ollama_native")

    assert wire == [
        {"role": "user", "content": "what is in a.py?"},
        {
            "role": "assistant",
            "content": "Let me look.",
            "tool_calls": [{"function": {"name": "read_file", "arguments": {"path": "a.py"}}}],
        },
        {"role": "tool", "tool_name": "read_file", "content": "contents"},
    ]


def test_F01_04_the_same_history_renders_to_both() -> None:
    """The point of the separation: one conversation, two wire formats."""
    h = answered_history()

    a = h.to_wire("chat_completions")
    b = h.to_wire("ollama_native")

    assert a != b
    assert [m["role"] for m in a] == [m["role"] for m in b]


def test_F01_07_system_notes_are_not_user_messages() -> None:
    """The turn-budget warning is something the code knows, not something the
    user said.  Chapter 0 wrote it with role 'system' by hand; now the type
    system remembers which is which."""
    h = History()
    h.add_user("go")
    h.add_system_note("1 turn left")

    wire = h.to_wire()
    assert [m["role"] for m in wire] == ["user", "system"]
```

> - 开头的 `CALL`、`CALL2` 是两个现成的工具调用，`answered_history()` 造一段"调用已经回应过"的
>   正常历史，给好几个测试当起点。
> - `with pytest.raises(HistoryError, match="..."):`：断言会抛出这个异常，**而且错误信息里要能
>   找到 `match` 给的文字**。这保证了报错不只是"报了"，还"说对了"。
> - **F01-02** 的三个测试：有欠账时拒绝发送；报错要列出**每一个**欠着的调用；有欠账时模型不能再说话。
> - **F01-06** 的三个测试：同一个调用不能回应两次；回应一个不存在的 id 会被拒绝；同一轮里两个
>   调用 id 相同会被拒绝。
> - **F01-04** 的三个测试：两种方言各自渲染成什么样；同一份历史能渲染成两种方言。
> - **F01-07**：system 提示不会变成 user 消息。

`tests/test_agent.py` 也要跟着改。import 换成：

```python
from minicodex.agent import Agent, IncompleteStreamError
from minicodex.agent_types import ToolCall
from minicodex.history import AssistantMessage, ToolResult
from minicodex.model import ChatCompletionsModel, Completed, TextDelta, ToolCallDelta
from minicodex.recorder import Recorder
from minicodex.tools import TOOL_SCHEMAS
```

`result.history` 不再是字典列表，几个检查历史的测试要改成按类型判断：

```python
async def test_F00_03_outputs_pair_one_to_one_with_calls(stub_url: str) -> None:
    """Chapter 0 asserted this by hand and said chapter 1 would turn it into a
    type.  It did: `History` refuses the invalid states outright, so this test
    now only confirms the loop uses it correctly."""
    agent = Agent(model(stub_url, stub_mode="three"), make_tools())

    result = await agent.run("go")

    issued = [
        c.call_id for i in result.history if isinstance(i, AssistantMessage) for c in i.tool_calls
    ]
    answered = [i.call_id for i in result.history if isinstance(i, ToolResult)]
    assert issued == answered
    assert result.history.unanswered() == ()


async def test_F00_05_a_failing_call_does_not_cancel_its_siblings(stub_url: str) -> None:
    """Reuses the three-call recording but points one name at a broken tool."""
    log: list[str] = []
    tools = make_tools(log)
    tools["get_conditions"] = tools["explode"]
    agent = Agent(model(stub_url, stub_mode="three"), tools)

    result = await agent.run("go")

    assert log == ["get_temperature:New York", "get_temperature:London"]
    assert len([i for i in result.history if isinstance(i, ToolResult)]) == 3


async def test_F00_07_the_recorded_stream_gives_the_same_run_every_time(stub_url: str) -> None:
    """A real model mostly repeats itself and occasionally does not, which is
    worse than always differing: a fix looks confirmed when it was only lucky.
    Replaying a recording removes the luck."""
    first = await Agent(model(stub_url), make_tools()).run("go")
    second = await Agent(model(stub_url), make_tools()).run("go")

    assert first.history.items == second.history.items


async def test_F_1_04_transcript_captures_what_the_model_was_sent(
    stub_url: str, tmp_path: Path
) -> None:
    recorder = Recorder(tmp_path / "run.jsonl")
    agent = Agent(model(stub_url), make_tools(), recorder=recorder)

    await agent.run("go")

    events = recorder.read_all()
    assert [e["kind"] for e in events] == ["request", "response", "request", "response"]
    assert events[0]["payload"]["messages"][0] == {"role": "user", "content": "go"}
```

> - `test_F00_03_outputs_pair_one_to_one_with_calls` 的 docstring 更新了：第 0 章说"下一章会把它
>   变成一个类型"，现在做到了。它现在只检查循环**正确地使用了** `History`。
> - `isinstance(i, AssistantMessage)`、`isinstance(i, ToolResult)` 代替了原来的 `i["role"] == ...`。

两个测试里的包装类，`stream` 的参数名也改成 `messages`：

```python
async def test_F00_01_runaway_loop_is_bounded(stub_url: str) -> None:
    """The recording asks for a tool every time it has not seen a tool result,
    so a loop that never feeds results back would run forever."""

    class NeverSatisfied:
        """Strips tool results out, so the model always asks again."""

        def __init__(self, inner: ChatCompletionsModel) -> None:
            self.inner = inner
            self.calls = 0

        def stream(self, messages: list[dict[str, Any]]):
            self.calls += 1
            return self.inner.stream([m for m in messages if m.get("role") != "tool"])

    llm = NeverSatisfied(model(stub_url))
    agent = Agent(llm, make_tools(), max_turns=5)

    result = await agent.run("go")

    assert result.stop_reason == "turn_limit"
    assert result.turns_used == 5
    assert llm.calls == 5, "the bound must apply to model calls, not loop iterations"


async def test_F00_01_model_is_warned_before_the_budget_runs_out(stub_url: str) -> None:
    """Being killed mid-thought produces nothing.  Being told produces an answer."""
    seen: list[list[dict[str, Any]]] = []

    class Watching:
        def __init__(self, inner: ChatCompletionsModel) -> None:
            self.inner = inner

        def stream(self, messages: list[dict[str, Any]]):
            seen.append([dict(m) for m in messages])
            return self.inner.stream([m for m in messages if m.get("role") != "tool"])

    agent = Agent(Watching(model(stub_url)), make_tools(), max_turns=4)
    await agent.run("go")

    notices = [m for m in seen[-1] if m["role"] == "system"]
    assert notices, "the model was never told it was running out of turns"
    assert "1 tool-calling turn(s) left" in notices[-1]["content"]
```

`test_request_body_can_be_inspected_without_a_network_call` 在 §5.2 已经改过类名：

```python
def test_request_body_can_be_inspected_without_a_network_call() -> None:
    """Chapter 6 diffs this; chapter 13 snapshots it.  Both need it separable."""
    body = ChatCompletionsModel(model="m", tools=TOOL_SCHEMAS).request_body(
        [{"role": "user", "content": "hi"}]
    )
    assert body["stream"] is True
    assert body["messages"] == [{"role": "user", "content": "hi"}]
    assert body["tools"][0]["function"]["name"] == "read_file"
```

最后加一个测试，钉住 F01-03——"发出去的请求确实是从 `to_wire()` 来的"：

```python
async def test_F01_03_the_request_is_rendered_from_the_history(
    stub_url: str, tmp_path: Path
) -> None:
    """The invariant check lives on the send path, not beside it.

    `run()` never assembles a message list by hand; it calls `to_wire()`, which
    is what refuses a history with unanswered calls.  Comparing the recorded
    request against `to_wire()` is what pins the check to that path.
    """
    recorder = Recorder(tmp_path / "run.jsonl")
    agent = Agent(model(stub_url), make_tools(), recorder=recorder)

    result = await agent.run("go")

    first_request = recorder.read_all()[0]["payload"]["messages"]
    assert first_request == [{"role": "user", "content": "go"}]
    assert result.history.unanswered() == ()
```

> 它读出记录器里第一个请求的 `messages`，断言它就是 `to_wire()` 的产物，并且最后没有欠账。
> 如果哪天有人在 `run()` 里手工拼消息列表、绕过了 `to_wire()`，记录下来的请求就会和
> 历史对不上。

提交：

```bash
git add .
git commit
```

```
feat: store the conversation as facts rather than as one provider's JSON

The history was a list of chat-completions dicts. Two measurements made
that untenable. The dialects differ: Ollama's native API wants arguments
as an object and matches results by tool_name, and sending either shape
to the other endpoint is a 400 both ways. And only some servers check the
invariants: a history with an unanswered tool call got HTTP 200 from
Ollama -- which then answered "symbiotic with the user's request" -- and
HTTP 400 from OpenAI.

History stores four kinds of fact, refuses a result for an unknown or
already-answered id, and refuses to render while any call is unanswered.
to_wire() is the one door every request goes through.

ToolCall moves to agent_types.py: history needs it and agent needs
History, and leaving it in agent.py made the two import each other.
```

> **把"我测到了什么"写进 message**，比写"这个设计更健壮"有用得多。

### 7.2 把边界写成测试

**只写在文字里的架构规则，迟早会被违反**：没有人会在每次改代码时回头翻文档。这一章有两条规则，功能测试保护不了它们，所以要专门写成测试：

**第一条：`agent.py` 不许知道自己在跟哪家服务说话。** 这是 §5.4 那个决定的可执行版本。如果哪天
有人图省事，在循环里写一句 `if provider == "openai": ...`，归一化就白做了——而且**不会有任何
测试变红**，因为功能还是对的。这是 §4 表里的 F01-05。

**第二条：`agent_types.py` 必须保持"谁都不依赖"。** 它存在的唯一理由是打断循环。它一旦 import
了 `agent` 或 `history`，就不再是解法，而成了循环的第三个环节。

新建 `tests/test_boundaries.py`：

```python
"""Boundaries that only stay true if something checks them.

An architecture rule that lives only in prose will rot. These are
the first two rules cheap enough to encode, so they are encoded.
"""

from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parent.parent / "src" / "minicodex"


def imported_modules(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
    return names


# ---------------------------------------------------------------------------
# F01-05  the loop must not know which provider it is talking to
# ---------------------------------------------------------------------------


def test_F01_05_the_agent_does_not_import_an_http_client() -> None:
    """`agent.py` talks to a `Model`, never to a socket.

    If this ever fails, some provider-specific handling has leaked upward and
    the normalisation in `model.py` is no longer doing its job.
    """
    assert "httpx" not in imported_modules(SRC / "agent.py")


@pytest.mark.parametrize("module", ["agent.py", "history.py", "agent_types.py"])
def test_F01_05_no_provider_names_above_the_client(module: str) -> None:
    """Provider names belong in `model.py`, `stub.py` and the CLI.

    "ollama_native" appears in history.py as a dialect name, which is the one
    legitimate exception: rendering is exactly where a dialect must be named.
    """
    text = (SRC / module).read_text(encoding="utf-8")
    code = "\n".join(line for line in text.splitlines() if not line.strip().startswith("#"))
    for banned in ("api.openai.com", "localhost:11434", "OPENAI_API_KEY"):
        assert banned not in code, f"{module} should not know about {banned}"


# ---------------------------------------------------------------------------
# F01-08  the circular import
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "first,second",
    [("minicodex.agent", "minicodex.history"), ("minicodex.history", "minicodex.agent")],
)
def test_F01_08_modules_import_in_either_order(first: str, second: str) -> None:
    """A circular import only fails in one direction, so both are tried.

    Run in a subprocess: once a module is in sys.modules the cycle is hidden,
    and every other test in this file has already imported both.
    """
    result = subprocess.run(
        [sys.executable, "-c", f"import {first}; import {second}"],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr


def test_F01_08_agent_types_depends_on_nothing_of_ours() -> None:
    """The shared module is only a solution while it stays a leaf."""
    ours = {m for m in imported_modules(SRC / "agent_types.py") if m.startswith("minicodex")}
    assert ours == set(), f"agent_types must stay a leaf, but imports {ours}"
```

逐段说：

> - **`imported_modules(path)`**：用 `ast.parse` 把源文件解析成语法树，`ast.walk` 走遍每个节点，
>   收集所有 `import x` 和 `from x import ...` 里的模块名。**为什么不用字符串搜索？** 因为
>   `# import httpx` 这样的注释不该算违规，而字符串搜索分不清。**检查代码的工具要用解析器。**
> - **`test_F01_05_the_agent_does_not_import_an_http_client`**：`agent.py` 不能 import `httpx`。
>   它只能通过 `Model` 和模型说话，永远不直接碰网络。
> - **`test_F01_05_no_provider_names_above_the_client`**：`agent.py`、`history.py`、`agent_types.py`
>   的代码里，不能出现 `api.openai.com`、`localhost:11434`、`OPENAI_API_KEY`。那行
>   `code = ...` 先去掉以 `#` 开头的注释行——docstring 和注释里提到这些名字是合理的。
> - **`test_F01_08_modules_import_in_either_order`**：分别按两种顺序 import 两个模块。
>   循环 import 常常只在某一种顺序下报错，所以两种都试。
> - **`test_F01_08_agent_types_depends_on_nothing_of_ours`**：`agent_types.py` 不能 import 任何
>   `minicodex` 开头的模块。

**`test_F01_08_modules_import_in_either_order` 为什么要开一个子进程？** 循环 import 只在**第一次**
导入时报错；模块一旦进了 `sys.modules`，之后再 import 就直接从缓存里取。而这个测试文件运行时，
前面的测试早就把两个模块都导入过了——**如果在同一个进程里检查，它对着一个真正有循环的代码库
也会通过。** 所以用 `subprocess.run([sys.executable, "-c", "..."])` 启动一个全新的 Python 去 import。

> 这是"一个对着坏代码也能通过的测试就是装饰品"的又一个例子，而且这次的陷阱藏得很深：
> 测试逻辑完全正确，错的是**它运行的环境**。

```bash
git add tests/test_boundaries.py
git commit -m "test: pin the provider boundary and the leaf module"
```

最后更新 `README.md`，说明这一步做了什么、为什么：

````markdown
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
````

```bash
git add README.md
git commit -m "docs: describe the protocol layer in the README"
```

---

## §8 验证

### 8.1 把修复改回去

第 -1 章的规矩：把每个修复改回去，看对应的测试红不红。

| 改回去 | 抓住它的测试 |
|---|---|
| `if raw.get("id"):` → 直接赋值 | `test_F01_01_openai_fragments_are_reassembled` |
| `if fn.get("name"):` → 直接赋值 | `test_F01_01_a_fragmented_call_runs_the_right_tool` |
| `to_wire` 里的欠账检查 → `if False` | `test_F01_02_the_error_names_every_unanswered_call` |
| `add_tool_result` 里的 `pop` 检查失效 | `test_F01_06_a_result_for_an_unknown_id_is_refused` |
| 原生方言渲染成 `/v1` 格式 | `test_F01_04_ollama_native_dialect` |
| 在 `agent.py` 里 `import httpx` | `test_F01_05_the_agent_does_not_import_an_http_client` |
| 在 `agent_types.py` 里 import `history` | **整个测试套件收集失败** |

七个改动，六次变红，加一次更彻底的失败。

最后一条值得说：把 `ToolCall` 搬回去、重新制造循环 import 之后，pytest 并没有报"某个测试失败"，
而是 `Interrupted: 2 errors during collection`——**测试根本收集不起来**。循环 import 坏得太早，
早到测试框架都来不及启动。这也是为什么那个检查必须开子进程：在当前进程里，它根本没机会运行。

**注意前两条是分开的。** 本来只想测"碎片能拼对"，但 id 和 name 是两条独立的"有才写"——只测其中
一个，另一个写错了照样全绿。**一个改动对应一条逻辑，而不是对应一个函数。**

### 8.2 全部跑一遍

```
$ uv run pytest
.............................................................            [100%]
61 passed in 8.93s
```

（Windows 上实测。）

### 8.3 端到端

同一个 Agent，分别对着两段录音跑。写一个临时脚本 `e2e.py`（看完就删）：

```python
# e2e.py -- the same agent against both recordings
import asyncio

from minicodex.agent import Agent
from minicodex.model import ChatCompletionsModel
from minicodex.tools import DEFAULT_TOOLS, TOOL_SCHEMAS

URL = "http://127.0.0.1:11435/v1"


async def main() -> None:
    for mode in ["narrate", "openai"]:
        llm = ChatCompletionsModel(base_url=URL, tools=TOOL_SCHEMAS, extra_body={"stub_mode": mode})
        seen = []

        async def read_file(args, seen=seen):
            seen.append(args)
            return await DEFAULT_TOOLS["read_file"](args)

        result = await Agent(llm, {"read_file": read_file}).run(
            "What does src/minicodex/__init__.py define?"
        )
        print(f"[{mode:8}] read_file{seen}")
        print(f"           {result.final_text.splitlines()[0]}")


asyncio.run(main())
```

> - `read_file` 包了一层：记下收到的参数，再调用真正的工具。
> - `seen=seen` 这个默认参数，让每一轮循环里定义的函数记住**那一轮**的 `seen` 列表。
> - `f"{mode:8}"` 把字符串补齐到 8 个字符宽，输出能对齐。

另开一个终端运行 `uv run minicodex serve-stub`，然后：

```
$ uv run python e2e.py
[narrate ] read_file[{'path': 'src/minicodex/__init__.py'}]
           `src/minicodex/__init__.py` defines the following:
[openai  ] read_file[{'path': 'src/minicodex/__init__.py'}]
           It defines `__version__` and `system_prompt()`.
```

（Windows 上实测。）

**一个 chunk 和十四个 chunk，工具收到的参数一模一样。** Agent 不知道、也不需要知道区别。

有 OpenAI 密钥的话，最后可以对着真的跑一次：

```bash
uv run minicodex ask "What does src/minicodex/__init__.py define?" --provider openai
```

---

## §9 回顾：这一章撞到了什么

| 编号 | 问题 | 结果 | 如果不防，它会怎么暴露 | 挡住它的东西 |
|---|---|---|---|---|
| F01-01 | **参数分十四片，覆盖后只剩最后一片，而且不崩** | 📏 **测到**（§3.3） | 🟡 静默 | 客户端内部拼好，`[DONE]` 时才交出 |
| F01-02 | 有调用没结果：一家 200 胡言乱语，一家 400 | 📏 **测到**（§3.5） | 🟡 / 🔴 | `to_wire()` 拒绝渲染 |
| F01-04 | 历史存成一家的格式，换一家两个方向都是 400 | 📏 **测到**（§3.4） | 🔴 | 存事实，发送时才翻译 |
| F01-06 | 同一个 id 两条结果 / 结果对不上任何调用 | 📏 **测到**（§3.5） | 🟡 / 🔴 | `pop` 一步完成取出和标记 |
| F01-03 | 发请求的路径不止一条，每条都可能忘了检查 | 🛡 **预防**（§6.3） | 🟣 代码审查发现 | 检查放在所有路径都经过的 `to_wire()` |
| F01-05 | 有人把 `if provider == ...` 写进循环 | 🛡 **预防**（§5.4、§7.2） | 🟣 代码审查发现 | 归一化在客户端 + 边界测试 |
| F01-07 | 代码加的提示和用户的话混成同一种东西 | 🛡 **预防**（§6.2） | 🟣 代码审查发现 | `SystemNote` 独立类型 |
| F01-08 | *（没猜到）* 循环 import，坏到测试都收集不起来 | ⚠️ **意外**（§7） | 🔴 | 共享类型下沉，并测试它保持不依赖任何人 |

**"🟡 / 🔴"那几条是同一个 bug 的两种命运**：在 Ollama 上是静默的，在 OpenAI 上当场被拒绝。

> **这个现象值得单独记住：你的开发环境越宽松，你的 bug 就越晚被发现。**
> 而"晚"通常意味着"在用户那里"。

和第 0 章比，这一章的四条主要故障都是**测出来的**，不是猜出来的，也不是撞出来的。
§2 的第一行写着"不知道，得先测"——测完之后，要修什么已经一目了然。

---

## §10 交给 GitHub

流程和第 0 章 一样：

```bash
git push -u origin feat/protocol-layer
```

在 GitHub 上开 PR，等 CI 通过，自己在 **Files changed** 里审查一遍，然后合并。

这一章的提交：

```
docs: describe the protocol layer in the README
test: pin the provider boundary and the leaf module
feat: store the conversation as facts rather than as one provider's JSON
fix: reassemble tool-call fragments before emitting them
feat: talk to any chat-completions server; choose one with --provider
test: add OpenAI's fragmented tool call to the stub
```

### 10.1 这次审查提出的问题

**1 · 正确性** — `_PartialCall.finish()` 里的 `call_id or f"call_{self.index}"`：如果服务真的不给 id，
这个编出来的 id 会被当真发回去，服务端可能认不出来。

> **回答**：是，而且没有更好的办法。不给 id 的回复本来就无法正确对应——编一个，至少能让**本地的**
> 规则得到满足，`History` 的检查才有意义。如果服务端因此拒绝，那是一个看得见的 400，比本地悄悄
> 乱套强。代码注释里写明了：这是"对方 bug 的兜底，不是要支持的形状"。

**结局：接受，并把理由写进代码。**

**2 · 边界情况** — `to_wire()` 每次都重新翻译整个历史。第 30 轮时，这是每轮重复一遍的工作。

> **回答**：真的，但现在无所谓。量一下：30 条消息翻译一遍大约 0.1 毫秒，而一次模型调用是秒级，
> **差了四个数量级**。等以后历史变复杂、性能分析显示它真的排上号了再说。**现在优化它，就是拿
> 可读性去换一个测不出来的收益。**

**结局：拒绝，附上数量级。**

**3 · 可测试性** — `History` 的 `_items` 和 `_unanswered` 都是私有的，测试只能通过公开方法间接观察。

> **回答**：这是故意的。`items` 属性已经提供了只读的访问（返回元组）。**如果一个测试必须改内部
> 状态才能构造出某个场景，那说明真实使用中这个场景也构造不出来**——那种测试测的是实现，
> 不是约定。

**4 · 命名** — `Dialect` 用的是字符串字面量，不是枚举。把 `"ollama_native"` 拼错成
`"ollama_nativ"`，只有类型检查器会发现，运行时会悄悄走进 `else` 分支，渲染成 `/v1` 格式。

> **回答**：接受一半。改成枚举确实更安全，但字面量让调用处保持 `to_wire("ollama_native")`，
> 更好读。**真正的问题不在字面量，而在 `_render` 里用了 `if ... else`——`else` 吞掉了一切拼写错误。**
> 该改的是：把每种方言都显式列出来，最后 `raise`。出现第三种方言时一起改。

**结局：问题被重新定位了。** 审查者指出了症状（字符串容易拼错），作者找到了病根（`else` 吞错）。
这比直接接受或直接拒绝都有价值。

**5 · 风格** — `agent_types.py` 只有一个类，一大半是 docstring。

> **回答**：是。而那段 docstring 正是它存在的全部理由（§7）。

**和上一章审查的区别。** 第 0 章说"不要提一条要求对方去猜的意见"。这一章第 2 条（性能）看起来
恰好相反——**它要求了一个数字**，而作者给出了数字。区别在于：**"这里会不会太慢"是一个可以测量
的问题，"边界应该划在哪"不是。** 前者值得提，后者要等观察。

---

## §11 本章给 CI 加了什么

**什么都没加。** 新增的保护全在测试里，CI 现有的"跑测试"一步自动就用上了。

**但这一章第一次出现了"CI 跑不到的验证"**：所有对着真 OpenAI 的测量。它们需要密钥，而 CI 里
不该放密钥。解决办法还是录音——CI 跑的是 2026-08-06 那次真实回复的回放。代价和第 0 章说的一样：
**录音会过时**，以后需要定期用真服务重新录制。

---

## §12 三条主线各自留下了什么

### 主线 A · 需求变代码

**这一章的第一件事不是写代码，是去测。** §2 的第一行是"不知道，得先测"。三次测量得到三个事实，
三个事实各推出一个决定：

| 测到的 | 决定 |
|---|---|
| 参数分片，只有第一片带 id 和 name | **在客户端里拼好再交出**，上面只见一种形状 |
| 两家历史的方言不同 | **历史不存发送格式**，存事实，发送时才翻译 |
| 只有一部分服务检查历史的规则 | **规则自己守**，不指望服务端告诉你 |

**如果先设计再测量**，最可能的产物是一个"通用适配器基类"，然后发现真实的差异根本不在你设计的
那些方面。

**这一章的抽象决定：**

| 东西 | 决定 | 理由 |
|---|---|---|
| 客户端内拼接碎片 | **做** | 信任边界：分歧到此为止 |
| `History` 类型 | **做** | 新的第四种情况：规则必须一直成立，而服务端不替你守 |
| `agent_types.py` | **做** | 被循环 import 逼出来的最小解法 |
| 方言用字面量不用枚举 | **不做** | 可读性优先，真问题在 `else` 分支 |

**注意 `History` 这个决定，第 0 章明确否决过。** 当时的理由是"只有一处代码往里放东西，形状是猜的"。
现在有了测量结果（服务端不管、方言不同）。**条件变了，答案就变了。这不是打脸，是决策该有的样子。**
把"当时为什么不做"记下来，才能在条件变化时知道要重新评估。

### 主线 B · 工程化交付

| 动作 | 本章的规则 |
|---|---|
| 改名 | 用 `git mv`，历史不断 |
| commit 类型 | 修前一章遗留的问题用 `fix`，即使当时没人知道它是 bug |
| 延后的审查意见 | 兑现时在 message 里**回指原文** |
| message 正文 | 写"我测到了什么"，不写"这个设计更健壮" |
| 审查 | 可以要求一个数字；不可以要求一个猜测 |
| 架构规则 | 值得保护的边界写成测试，用 `ast` 解析，不用字符串搜索 |
| API 密钥 | 从环境变量读，不从命令行参数读，也不写进代码 |

### 主线 C · 故障

**第一招：开发环境越宽松，bug 被发现得越晚。** 同一份坏历史，Ollama 返回 200 加胡言乱语，
OpenAI 返回 400。**不要把"本地跑通了"当成"是对的"。**

**第二招：猜一个没见过的形状，猜对的概率不比不猜高。** 第 0 章拒绝为"可能分片"写代码是对的——
因为那个决定**附带了一个可以检查的期限**。

**第三招：把检查放在所有路径都必须经过的地方。** `to_wire()` 是数据离开你控制范围前的最后一道门。

**第四招：能测的就不要猜。** 这一章的主要故障全是测出来的。

---

## 如果你只记住三件事

1. **"兼容"的接口并不兼容。** 同一个工具调用，一家发一个 chunk，一家发十四个，而且只有第一个带
   id 和 name。这不是文档能告诉你的，是打开真实的流看出来的。

2. **规则得自己守。** 三种坏历史，Ollama 全部放行（并产出垃圾），OpenAI 全部拒绝。指望服务端替你
   检查，等于指望你的开发环境和线上环境一样严格。

3. **在边界上统一形状，改动就关在边界里。** 这一章 `agent.py` 的 `_collect` 和 `_run_tool` 一行没改，
   因为不整齐的东西在 `model.py` 就被拦住了。

---

## 动手

```bash
cd steps/step01_protocol
uv sync --all-extras
uv run pytest
```

对着录音服务跑 `ask`（另一个终端里先运行 `uv run minicodex serve-stub`）：

```bash
uv run minicodex ask "What does src/minicodex/__init__.py define?" --base-url http://127.0.0.1:11435/v1
```

有 OpenAI 密钥的话：

```bash
uv run minicodex ask "What does src/minicodex/__init__.py define?" --provider openai
```

**建议自己做一遍的四件事：**

1. **把 `_PartialCall.absorb` 里的 `if raw.get("id"):` 改成直接赋值**，跑 `uv run pytest -k F01_01`。
   你会看到第 0 章那个 bug 的另一种样子。
2. 有 Ollama 的话，把 §3.5 那个"有调用没结果"的 `messages` 发给它（在第 0 章的 `explore.py` 里
   替换 `messages` 即可），亲眼看 HTTP 200 加上胡言乱语。**这是本章最重要的一次观察。**
3. 有 OpenAI 密钥的话，运行 §3 的 `explore.py`，数一数工具调用来了几片。
4. 给 `History` 加第三种方言（比如 Anthropic 的 messages 格式），看看要改几个地方。
   答案应该是**一个函数**。

---

## 选读 · codex 是怎么做的

> 基于写作时（2026 年）的 codex 仓库，文件名和结构以后可能会变。不读不影响后面的内容。

**协议类型是一个单独的包。** codex 把 `ResponseItem`、`ContentItem` 这些发送格式相关的类型放在
`codex-rs/protocol/` 里，和业务逻辑（`core/`）分开。理由和这一章一样：**发送格式是和外部的约定，
不该和内部逻辑长在一起。**

**统一形状确实发生在边界上。** `codex-rs/core/src/client.rs` 处理流，`event_mapping.rs` 负责把
服务端的事件翻译成内部事件。上层拿到的永远是统一之后的东西。

**历史的规则有专门的模块。** `codex-rs/core/src/context_manager/normalize.rs` 就是干这个的——在历史
发出去之前修正、校验它的形状。`context_manager/history.rs` 里的 `ContextManager` 带着一个
`history_version: u64` 字段，注释写着：

> Bumped whenever history is rewritten, such as compaction or rollback.

**历史会被重写**（比如压缩、回滚），所以需要版本号来发现"你手上这份已经过期了"。后面讲上下文压缩
和崩溃恢复时会遇到这个问题。

**"所有供应商共用的"和"某一家特有的"分开放。** `client_common.rs` 和 `client.rs` 是两个文件——这正是
本章 `_render` 里 `if dialect == ...` 将来该演化成的样子。

---

**下一章**：[第一个 shell 工具](ch02-shell-tool.md)——只会读文件已经不够了，Agent 得能跑 `pytest`。
而子进程会卡住不动、会一口气输出 100MB、会等着你往它的输入里敲东西。

---

# 附录 · 对照检查

## T1 · 每个文件是在哪一节改的

本章结束时，你的项目内容应该和 `steps/step01_protocol/` 一致（测试函数的先后顺序可以不同）。

| 文件 | 在哪改的 |
|---|---|
| `src/minicodex/stub.py`（原 `stub_ollama.py`） | §5.1 |
| `src/minicodex/model.py` | §5.2 改名和密钥，§5.5–§5.6 拼接碎片；最终版见 §5.6 |
| `src/minicodex/__main__.py` | §5.1 改 import，§5.2 最终版 |
| `src/minicodex/history.py` | §6.2–§6.4 |
| `src/minicodex/agent_types.py` | §7 |
| `src/minicodex/agent.py` | §6.5、§7；最终版见 §7 |
| `tests/conftest.py` | §5.1 |
| `tests/test_agent.py` | §5.2 改类名，§5.3、§5.6 加 F01-01，§7.1 改 import 和几个测试、加 F01-03 |
| `tests/test_history.py` | §7.1 |
| `tests/test_boundaries.py` | §7.2 |
| `README.md` | §7.2 |

`tools.py`、`recorder.py`、`__init__.py`、`pyproject.toml`、`.gitignore`、CI 配置这一章没有改动。

## T2 · 常见报错对照表

| 症状 | 原因 | 修法 |
|---|---|---|
| `KeyError: 'OPENAI_API_KEY'`（在 `explore.py` 里） | 没设置环境变量 | 按 §0.1 设置，注意只对当前终端有效 |
| `ModelHTTPError: HTTP 401 ...` | 密钥错了或没带上 | 检查环境变量；确认用的是 `--provider openai` |
| `ModuleNotFoundError: No module named 'minicodex.stub_ollama'` | 改了文件名，漏改了 import | `conftest.py` 和 `__main__.py` 里改成 `minicodex.stub`（§5.1） |
| OpenAI 录音下工具名是空的、参数只剩 `"}` | 碎片被覆盖 | 按 §5.5–§5.6 在 `stream()` 里拼接 |
| `ImportError: ... partially initialized module ... circular import` | `history.py` 从 `agent.py` import `ToolCall` | 把 `ToolCall` 移到 `agent_types.py`（§7） |
| `HistoryError: refusing to send: tool calls with no result` | 某一轮有调用没回应 | 检查循环是不是对每个调用都调用了 `add_tool_result` |
| 测试里 `result.history` 报 `TypeError: 'History' object is not subscriptable` 之类 | 还在用字典的方式读历史 | 改成 `isinstance(item, ToolResult)` 这样按类型判断（§7.1） |
