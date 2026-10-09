# 第 6 章 · 上下文压缩

> **代码**：`steps/step06_compaction/`
> **分支**：`feat/compaction`
> **产出**：会话变长之后，Agent 自己把中间那一段换成一份摘要，然后继续干活
> **前置**：做完第 5 章。全部测试都不需要联网。本章有六个"探针"脚本会真的向 OpenAI 发请求（需要 `OPENAI_API_KEY`）；
> 没有 key 也没关系——每个探针的输出都贴在正文里了，照着读即可。
> **这一章很长**，可以分三次读：§1–§8 是"切在哪里"，§9–§12 是"什么时候切"，§13–§21 是"切掉的那段换成什么"，之后是验证。

---

## §0 开工前的准备

### 0.1 本章会遇到的新概念

- **上下文窗口（context window）**：模型一次请求最多能读多少内容。超过了，服务端直接拒绝。
- **token**：模型计量文字的单位。不是字符，也不是单词——一个英文单词大约 1 个多 token，一个汉字常常不止 1 个。
  窗口大小、收费，都按 token 算。
- **压缩（compaction）**：把对话历史里的一段删掉，换成一份摘要。
- **切点（cut point）**：从历史的第几条开始保留。切点之前的被删掉。
- **属性测试（property test）**：不是"给这个输入，期望这个输出"，而是"随机生成很多输入，断言某条性质对每一个都成立"。
- **校准（calibration）**：先估一个数，再用事后得到的真实值修正估法。

### 0.2 本章会遇到的新 Python 写法

| 写法 | 意思 |
|---|---|
| `isinstance(item, ToolResult)` | `item` 是不是 `ToolResult` 这个类的对象 |
| `@staticmethod` | 写在类里、但不需要 `self` 的函数，用 `类名.函数名(...)` 调用 |
| `@property` | 把一个方法伪装成属性：写 `c.ratio`，实际执行的是 `ratio()` 方法 |
| `text.partition("\n")` | 在第一个换行处把字符串切成三段：前面、换行本身、后面 |
| `reversed(列表)` | 从后往前遍历 |
| `random.Random(seed)` | 一个"种子固定"的随机数生成器：种子相同，生成的序列就相同，失败可以重现 |
| `os.environ.get("NAME", "默认")` | 读环境变量，没有就用默认值 |
| `getattr(obj, "tools", ())` | 取 `obj.tools`；没有这个属性就得到 `()` |
| `Callable[[A], Awaitable[B]]` | 类型标注："一个接收 A、可以被 `await`、最后得到 B 的函数" |
| `httpx.MockTransport(handler)` | 一个假的"网络"：请求不发出去，交给 `handler` 函数返回预先写好的响应 |

### 0.3 开分支

```bash
git switch main
git pull
git switch -c feat/compaction
```

---

## §1 这一章要做出来的东西

第 5 章结束时，Agent 能问、能记、能干活。但它有一个上限，而且这个上限不在我们的代码里，在服务端：**上下文窗口**。

一个真实的编码任务长什么样？读 5 个文件、跑 3 次测试、改 2 处代码、再跑一次。每一步的输出都进历史，
而每一轮请求都把**整个历史**重新发一遍。第 20 轮的那次请求，装着前 19 轮的全部内容。

所以问题不是"会不会满"，而是"满了之后怎么办"。

这一章要做的事，一句话：**在请求装不下之前，把中间那段删掉，换成一份摘要。**

听起来就是给列表做一次切片。事情没那么简单，而且难的那一半不在你以为的地方。

---

## §2 定需求，猜故障

需求：

- 历史快装不下时，自动把中间一段换成摘要；
- 换完之后的历史必须还能发出去；
- 换完之后 Agent 还得知道自己在干什么；
- 需要一个办法，在请求**发出去之前**估出它有多大。

动工前的猜测清单，十三条：

| 编号 | 猜测 | 怎么判断 |
|---|---|---|
| F06-01 | 删掉最老的一半，会删掉"调用"、留下"结果"，服务端返回 400 | 真发一次 |
| F06-02 | 按 token 数切，切点落在一次工具调用中间 | 构造这样的切点 |
| F06-03 | "只留最后 N 条"，第一条正好是个结果 | 同上 |
| F06-04 | 摘要丢掉了用户的约束和已经做出的决定 | 种几个事实，看还在不在 |
| F06-05 | 压缩后模型"失忆"，把做完的事重做一遍 | 看它下一步调用什么工具 |
| F06-06 | 系统提示或用户的第一条消息被压掉了 | 构造并真发 |
| F06-07 | 本地估算差 30%，压缩触发得太晚 | 和服务端报的数字对比 |
| F06-08 | 做摘要的那次模型调用自己失败了 | 让它抛异常 |
| F06-09 | 一条工具输出就比整个窗口还大 | 构造 |
| F06-10 | 图片之类的内容，估算时完全没算 | 构造 |
| F06-11 | 压缩正好发生在一次流式输出的中间 | 看代码结构 |
| F06-12 | 压缩完还是超 | 构造 |
| F06-13 | 摘要的摘要的摘要：一代代损失信息 | 连续压几代，数事实 |

先说结果：**F06-01 没有按预测的样子出现——而它没出现的方式，比出现了更值得学。**

---

## §3 最直白的版本

历史太长，砍掉最老的一半：

```python
def compact_v1(messages: list[dict]) -> list[dict]:
    return messages[len(messages) // 2 :]
```

一行。看起来没有任何可以出错的地方。

要看它到底行不行，别猜，**真发给服务端**。这一章第一个探针脚本：造一段"真实形状"的历史——第 5 章的 Agent 跑一个小任务会留下的东西——
用几种朴素的办法切它，每种都发出去，打印服务端的回答。

新建 `probe_naive_cut.py`（放在项目根目录，和 `pyproject.toml` 同一层）：

```python
"""What actually happens when you drop the oldest half of a conversation.

Builds a realistic agent history (system note, user, then three assistant/tool
round trips), cuts it three different naive ways, and posts each one to a real
server.  Prints the status and the first part of the body.

    python probe_naive_cut.py                # OpenAI, needs OPENAI_API_KEY
    python probe_naive_cut.py --base-url http://localhost:11434/v1 --model gemma4:31b-cloud
"""

from __future__ import annotations

import argparse
import json
import os
import sys

import httpx


def build_history() -> list[dict]:
    """Eight messages: one system, one user, then three call/result pairs."""
    messages: list[dict] = [
        {"role": "system", "content": "You are a coding agent working in /repo."},
        {"role": "user", "content": "Find out which Python version this project supports."},
    ]
    steps = [
        ("call_a1", "run_shell", '{"command": "ls"}', "pyproject.toml\nsrc\ntests\n"),
        ("call_b2", "read_file", '{"path": "pyproject.toml"}', 'requires-python = ">=3.10"\n'),
        ("call_c3", "run_shell", '{"command": "python --version"}', "Python 3.13.0\n"),
    ]
    for call_id, name, args, output in steps:
        messages.append(
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": call_id,
                        "type": "function",
                        "function": {"name": name, "arguments": args},
                    }
                ],
            }
        )
        messages.append({"role": "tool", "tool_call_id": call_id, "content": output})
    return messages


TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "run_shell",
            "description": "Run a shell command.",
            "parameters": {
                "type": "object",
                "properties": {"command": {"type": "string"}},
                "required": ["command"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read a file.",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
            },
        },
    },
]


def post(messages: list[dict], *, base_url: str, model: str, key: str | None) -> tuple[int, str]:
    headers = {"Content-Type": "application/json"}
    if key:
        headers["Authorization"] = f"Bearer {key}"
    body = {"model": model, "messages": messages, "tools": TOOLS, "stream": False}
    resp = httpx.post(f"{base_url}/chat/completions", json=body, headers=headers, timeout=120.0)
    return resp.status_code, resp.text


def shape(messages: list[dict]) -> str:
    """A one-line picture of the message list, so the cut is visible."""
    out = []
    for m in messages:
        if m["role"] == "assistant" and m.get("tool_calls"):
            out.append("A[" + ",".join(c["id"] for c in m["tool_calls"]) + "]")
        elif m["role"] == "tool":
            out.append("T[" + m["tool_call_id"] + "]")
        else:
            out.append(m["role"][0].upper())
    return " ".join(out)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base-url", default="https://api.openai.com/v1")
    ap.add_argument("--model", default="gpt-4o-mini")
    args = ap.parse_args()
    key = os.environ.get("OPENAI_API_KEY") if "openai.com" in args.base_url else None

    full = build_history()

    cuts = {
        "F06-01 drop oldest half": full[len(full) // 2 :],
        "F06-03 keep last 4": full[-4:],
        "F06-02 cut at a token count (here: 5 messages)": full[-5:],
        "(control) uncut": full,
    }

    for label, messages in cuts.items():
        print(f"\n=== {label}")
        print(f"    {shape(messages)}")
        status, text = post(messages, base_url=args.base_url, model=args.model, key=key)
        print(f"    HTTP {status}")
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            print(f"    {text[:300]}")
            continue
        if "error" in payload:
            print(f"    error.message: {payload['error']['message'][:400]}")
        else:
            choice = payload["choices"][0]["message"]
            answer = choice.get("content") or ""
            calls = [c["function"]["name"] for c in choice.get("tool_calls") or []]
            print(f"    content: {answer[:200]!r}")
            print(f"    tool_calls: {calls}")
            print(f"    usage: {payload.get('usage', {}).get('prompt_tokens')} prompt tokens")
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

> - **`build_history()`**：八条消息——一条 system，一条 user，然后三对"助手发起调用 / 工具返回结果"。
> - **`shape(messages)`**：把消息列表画成一行，方便一眼看出切在了哪里。`S` 是 system，`U` 是 user，
>   `A[x]` 是发起了调用 x 的助手消息，`T[x]` 是调用 x 的结果。**这个记号全章都用。**
> - **`TOOLS`**：两个玩具工具的 schema，只为让请求像一次真的 Agent 请求。
> - **`post(...)`**：发一次**非流式**请求（`"stream": False`），返回状态码和响应的原文。探针只关心"收不收"和"答的是什么"，用不着流式。
> - `main()`：三种朴素切法各发一次，再发一次不切的作对照。

没切的历史是这个形状：

```
S U A[call_a1] T[call_a1] A[call_b2] T[call_b2] A[call_c3] T[call_c3]
```

跑它（2026-10-01，gpt-4o-mini）：

```
$ uv run python probe_naive_cut.py
=== F06-01 drop oldest half
    A[call_b2] T[call_b2] A[call_c3] T[call_c3]
    HTTP 200
    content: 'Your current Python version is 3.13.0, which is compatible with the requirement specified in your `pyproject.toml` file (requires Python >= 3.10).'
    tool_calls: []
    usage: 122 prompt tokens

=== F06-03 keep last 4
    A[call_b2] T[call_b2] A[call_c3] T[call_c3]
    HTTP 200
    content: 'Your current Python version is 3.13.0, which is compatible with the requirement specified in your `pyproject.toml` file (Python version >= 3.10).'
    tool_calls: []
    usage: 122 prompt tokens

=== F06-02 cut at a token count (here: 5 messages)
    T[call_a1] A[call_b2] T[call_b2] A[call_c3] T[call_c3]
    HTTP 400
    error.message: Invalid parameter: messages with role 'tool' must be a response to a preceeding message with 'tool_calls'.

=== (control) uncut
    S U A[call_a1] T[call_a1] A[call_b2] T[call_b2] A[call_c3] T[call_c3]
    HTTP 200
    content: 'The project supports Python version >= 3.10. You are currently using Python 3.13.0, which is compatible with the project.'
    tool_calls: []
    usage: 176 prompt tokens
```

砍掉一半：**200**。token 从 176 降到 122。

**猜测清单说这里应该是 400。**

---

## §4 清单猜错了，而猜错的方式更有用

F06-01 的推理本身没问题：砍一半，切点落在 `A[x]` 和 `T[x]` 之间，`T[x]` 就成了没有主人的结果。
但这次没有——八条消息的一半是下标 4，而下标 4 正好是 `A[call_b2]`，一个合法的位置。

**它没出错，是因为 8 是偶数。**

这种"碰巧对了"是最糟的一类：它能通过测试，能通过审查，然后在某个用户的第 9 条消息上出事。

所以别猜，把**每一个切点**都问一遍服务端。新建 `probe_cut_points.py`：

```python
"""Which cut points does a real server accept?

Takes the same eight-message history and cuts it at every index, then posts each
suffix.  The answer is not "keep N messages" and not "keep N tokens" -- it is a
property of the shape at the cut.

    python probe_cut_points.py
"""

from __future__ import annotations

import argparse
import json
import os
import sys

import httpx
from probe_naive_cut import TOOLS, build_history, shape


def post(messages: list[dict], *, base_url: str, model: str, key: str | None) -> tuple[int, str]:
    headers = {"Content-Type": "application/json"}
    if key:
        headers["Authorization"] = f"Bearer {key}"
    body = {"model": model, "messages": messages, "tools": TOOLS, "stream": False}
    resp = httpx.post(f"{base_url}/chat/completions", json=body, headers=headers, timeout=120.0)
    return resp.status_code, resp.text


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base-url", default="https://api.openai.com/v1")
    ap.add_argument("--model", default="gpt-4o-mini")
    args = ap.parse_args()
    key = os.environ.get("OPENAI_API_KEY") if "openai.com" in args.base_url else None

    full = build_history()
    print(f"full history: {shape(full)}\n")
    print(f"{'cut at':>7}  {'kept shape':<44} {'status':<7} {'note'}")
    print("-" * 100)

    for i in range(len(full)):
        kept = full[i:]
        status, text = post(kept, base_url=args.base_url, model=args.model, key=key)
        note = ""
        payload = json.loads(text)
        if "error" in payload:
            note = payload["error"]["message"][:52]
        else:
            note = (payload["choices"][0]["message"].get("content") or "")[:52].replace("\n", " ")
        print(f"{i:>7}  {shape(kept):<44} {status:<7} {note}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

> 它复用了上一个脚本的 `build_history`、`shape` 和 `TOOLS`（`from probe_naive_cut import ...`），
> 对 0 到 7 每一个下标 `cut`，发送 `messages[cut:]`，记下状态码。

```
$ uv run python probe_cut_points.py
full history: S U A[call_a1] T[call_a1] A[call_b2] T[call_b2] A[call_c3] T[call_c3]

 cut at  kept shape                                   status  note
----------------------------------------------------------------------------------------------------
      0  S U A[call_a1] T[call_a1] A[call_b2] T[call_b2] A[call_c3] T[call_c3] 200     The project supports Python versions **greater than
      1  U A[call_a1] T[call_a1] A[call_b2] T[call_b2] A[call_c3] T[call_c3] 200     The project supports Python versions **3.10 and abov
      2  A[call_a1] T[call_a1] A[call_b2] T[call_b2] A[call_c3] T[call_c3] 200     The project requires Python version 3.10 or higher,
      3  T[call_a1] A[call_b2] T[call_b2] A[call_c3] T[call_c3] 400     Invalid parameter: messages with role 'tool' must be
      4  A[call_b2] T[call_b2] A[call_c3] T[call_c3]  200     Your Python version is 3.13.0, which meets the requi
      5  T[call_b2] A[call_c3] T[call_c3]             400     Invalid parameter: messages with role 'tool' must be
      6  A[call_c3] T[call_c3]                        200     You have Python version 3.13.0 installed.
      7  T[call_c3]                                   400     Invalid parameter: messages with role 'tool' must be
```

400 的完整报错：

```
Invalid parameter: messages with role 'tool' must be a response to a preceeding message with 'tool_calls'.
```

（`preceeding` 是服务端自己拼错的，原样贴出来。这个细节值得留意：它说明这条消息是人手写的字符串，
所以**别指望靠匹配它的文字来写程序判断**。）

先看 400 那三条：下标 3、5、7，全是切在 `T[...]` 上的。规律很清楚。

**然后看 200 那五条。**

---

## §5 200 的那一半

用户问的是：**这个项目支持哪个 Python 版本？**

| 切点 | 状态 | 回答 |
|---|---|---|
| 0 | 200 | The project supports Python versions **greater than**… |
| 2 | 200 | The project requires Python version 3.10 or higher |
| 4 | 200 | **Your** Python version is 3.13.0, which meets the requi… |
| 6 | 200 | **You have Python version 3.13.0 installed.** |

切点 0 回答的是用户的问题。**切点 6 回答的是另一个问题。**

中间没有任何一个环节报错。切点 6 的历史完全合法，模型完全自信，句子完全通顺，token 少了一大半——
**而它回答的东西，用户没问。**

为什么？切点 6 只剩下 `A[call_c3] T[call_c3]`，也就是"执行了 `python --version`，输出 Python 3.13.0"。
模型能看到的全部信息就是这个，它据此推断用户在问当前装的是什么版本。这是它能做出的最合理的推断。

> **删掉一条消息，和删掉一个问题，是两件事。而它们看起来一模一样。**

切点 4 更隐蔽：它提到了项目的要求（3.10），但主语已经从"这个项目"变成了"你的"。这种半对的答案，实际使用中根本发现不了。

所以压缩有**两个**任务，不是一个：

1. 让剩下的东西**发得出去**（400 的那一半）；
2. 让剩下的东西**还知道自己在干什么**（200 的那一半）。

清单上的 F06-01、02、03 全是第一个任务。第二个任务在清单上只有 F06-06 一行，而它占了这一章一半的代码。

> **对照清单**：F06-02（切在调用中间）**成立**，上面第三个实验就是 400。F06-03（留最后 N 条）在这个历史上**碰巧没出事**，
> 原因和 F06-01 一样。F06-06（问题被压掉）**成立，而且是 🟡 静默的**——只有读了答案才发现。

---

## §6 合法的切点是什么

回到 400 的那一半，把规律写成代码。新建 `src/minicodex/compaction.py`，先放进这个函数（文件开头的 docstring 和 import 到 §21 一起给）：

```python
def boundaries(items: Sequence[HistoryItem]) -> tuple[int, ...]:
    """Every index at which the conversation may be cut.

    A cut at index `i` keeps `items[i:]`.  It is legal exactly when no call
    issued before `i` is answered after it -- a statement about the shape of
    the list, not about how many messages or tokens sit on either side.  Both
    naive rules ("drop the oldest half", "keep the last N") are right only by
    luck, and the luck is parity: on the eight-message history above, half is
    index 4 and legal; on a nine-message one it is index 4 or 5, and one of
    those is a 400.

    Verified against the server rather than against the docs: this function's
    output for that history is `(0, 1, 2, 4, 6, 8)`, and indices 0-7 are
    exactly the ones the API answered 200 to.
    """
    open_calls = 0
    result = []
    for index, item in enumerate(items):
        if open_calls == 0:
            result.append(index)
        if isinstance(item, AssistantMessage):
            open_calls += len(item.tool_calls)
        elif isinstance(item, ToolResult):
            open_calls -= 1
    if open_calls == 0:
        result.append(len(items))
    return tuple(result)
```

> - 参数 `items` 是第 1 章 `History` 里的那些对象（`UserMessage`、`AssistantMessage`、`ToolResult`、`SystemNote`），不是发给服务端的字典。
> - **`open_calls`**："已经发起、还没被回答的调用"有几个。遇到助手消息，加上它发起的调用数；遇到一个结果，减 1。
> - 走到下标 `index` 时，如果 `open_calls == 0`，说明前面发起的调用全都回答完了——从这里切，后面不会有没主人的结果。
> - 循环结束后再检查一次：`len(items)`（"全部切掉"）也是一个合法切点。

它表达的规则是：

> 切点 `i` 合法，当且仅当：`i` 之前发起的调用，没有一个是在 `i` 之后才被回答的。

对上面那个八条的历史跑一下（用 `History` 的 API 搭一个同样形状的）：

```
items       8
boundaries  (0, 1, 2, 4, 6, 8)
```

`8` 是"全部切掉"，探针没测。其余 `0, 1, 2, 4, 6`，**和服务端返回 200 的下标逐个吻合**。

### 6.1 为什么不写成"别切在结果上"

更短的写法是：

```python
def boundaries_v1(items):
    return [i for i, item in enumerate(items) if not isinstance(item, ToolResult)]
```

改写这一章时实测了一下：把 `boundaries()` 换成这个版本，**这一章的全部测试——包括后面 1000 个随机生成的历史——照样全绿。**
原因是：只要历史是通过第 1 章的 `History` 搭出来的，这两个说法就是等价的——`History` 不允许助手在还有调用没回答时再说话，
所以"下一条不是结果"和"没有未回答的调用"永远同时成立。

那为什么还用计数器？因为它写的是**规则本身**，而短的那个写的是规则在合法历史上的**表现**。
读代码的人看到 `open_calls == 0`，马上知道这段在守护什么；看到 `not isinstance(item, ToolResult)`，得自己再推一遍为什么这就够了。
这是可读性上的选择，不是正确性上的——**如实说清楚这一点，比假装短的那个有 bug 更好。**

### 6.2 "砍一半"和"留最后 N 条"错在哪

现在可以精确地说了：**这两条规则不是"有时候对"，而是"从来不看"。** 它们碰对和碰错时，执行的是同一行代码，
没有任何分支能让你在日志里区分这两种情况。按 token 数切是同一个问题换了个变量：token 数和消息的边界毫无关系。

---

## §7 不许动的那一段

现在处理 200 的那一半。切点 6 丢掉的是用户的问题，所以第一条规则很明显：**用户的第一条消息不能删。**

还有第二样东西：第 5 章放在最前面的系统消息，里面有权限状态。第 5 章实测过，模型不知道自己有什么权限时会去申请最大的那一档。
一个把系统消息压掉的 Agent 不是"变差了"，而是**变成了另一个 Agent**。

所以受保护的是开头的一段——一个**前缀**：

```python
@dataclass(frozen=True)
class Protected:
    """The prefix compaction is not allowed to touch.

    Two things, for two different reasons:

    * the **system notes at the front** -- the instructions and the permission
      block.  An agent that forgets those does not degrade, it becomes a
      different agent, and chapter 5 measured what one does when it no longer
      knows its own permissions;
    * the **first user message** -- the only record of what was asked.  Cut 6
      in the table above is what its absence looks like, and it looks like
      success.

    Later system notes are deliberately *not* protected.  Chapter 0's
    turn-budget warning is a `SystemNote` too, and it is worth exactly one
    turn; protecting by type rather than by position would accumulate every
    stale warning forever.
    """

    count: int

    @staticmethod
    def of(items: Sequence[HistoryItem]) -> Protected:
        index = 0
        while index < len(items) and isinstance(items[index], SystemNote):
            index += 1
        if index < len(items) and isinstance(items[index], UserMessage):
            index += 1
        return Protected(index)
```

> - `Protected` 只有一个字段 `count`：开头有多少条是受保护的。
> - **`Protected.of(items)`**：从头数，先跳过连续的 `SystemNote`，再跳过紧接着的**一条** `UserMessage`，数到几就是几。
>   `@staticmethod` 让它可以写成 `Protected.of(...)`，像一个"造对象的工厂"。

### 7.1 为什么按"位置"，不按"类型"

直觉的写法是"保护所有 `SystemNote`"，看起来更干净、更不容易漏。

但第 0 章的轮次预算警告也是 `SystemNote`："You have 2 tool-calling turn(s) left..."，快到上限时每一轮加一条。
按类型保护，等于**永久保留每一轮的过期警告**：压缩之后历史里会躺着"还剩 2 轮""还剩 1 轮"，而当前早就不是那个数了。

按位置保护，这些警告都在前缀之后，该丢就丢。

> 这是"保护**什么**"和"保护**哪里**"的区别。前者是关于内容的判断，内容的种类越多越难维护；
> 后者是关于结构的判断，以后加多少种 `SystemNote` 都不用改。

---

## §8 重建：走第 1 章那扇门

知道了切在哪、留什么，剩下的是把新历史造出来。最省事的写法是直接拼列表：

```python
new_items = items[:protected] + [summary_note] + items[cut:]
```

**不要这样写。**

第 1 章花了一整章把 `History` 做成"进不了非法状态"：规则在**放进去的时候**检查，而不是发送的时候。
直接拼列表，等于绕过那扇门。压缩如果有 bug，会变成一个从服务端回来的 400，带着一句拼错单词的英文，
而错误堆栈里没有任何一行指向做决定的那段代码。

所以重建的方式是：把每一条**重新放进去**。

```python
def _replay(history: History, items: Sequence[HistoryItem]) -> None:
    """Rebuild a history by putting every item back through the front door.

    This is the whole reason chapter 1 enforced its invariant on append rather
    than checking it on send.  A compaction that produces an orphaned result
    does not travel to a provider and come back as a 400 with a spelling
    mistake in it; it raises `HistoryError` here, in this process, naming the
    call id, on the line that planned the cut.
    """
    for item in items:
        if isinstance(item, UserMessage):
            history.add_user(item.text)
        elif isinstance(item, SystemNote):
            history.add_system_note(item.text)
        elif isinstance(item, AssistantMessage):
            history.add_assistant(item.text, item.tool_calls)
        elif isinstance(item, ToolResult):
            history.add_tool_result(item.call_id, item.content)
        else:  # pragma: no cover
            raise AssertionError(f"unreplayable item: {item!r}")
```

> 对每一条，按它的类型调用 `History` 对应的 `add_*` 方法。最后的 `else` 是"不可能走到"的分支
> （`# pragma: no cover` 告诉覆盖率工具别统计它）；万一以后加了新类型忘了改这里，它会立刻报错。

代价：多遍历一次，几十微秒。收益：**这个模块不可能产出一个非法的历史**——不是"我们小心地不产出"，而是产不出。

试着重放一个从下标 3 开始的片段（第一条就是没主人的结果）：

```
HistoryError: no unanswered call with id 'call_0'; awaiting (none)
```

本地、立刻、带着调用的 id。对比服务端那句话——它连是哪一条都不告诉你。

> 第 1 章做的那层抽象，到这一章才第一次真正回本。当时只是把"调用必须有回答"这条规则收进了一个类，
> 没有为压缩做任何设计；但压缩来的时候，**正确的做法自动成了最省事的做法**。

### 8.1 这一段的测试

新建 `tests/test_compaction.py`。文件开头（import 和三个小帮手）：

```python
"""Chapter 6: one or more tests per fault, named after it.

The naming is the point: `grep -rn F06_07 .` finds the fault entry, the code and
the test that pins it, and none of the three can be quietly removed.
"""

from __future__ import annotations

import httpx
import pytest

from minicodex.agent import COMPACT_AT, Agent
from minicodex.agent_types import ToolCall
from minicodex.compaction import (
    MAX_ITEM_TOKENS,
    Protected,
    Sizer,
    SummaryRequest,
    boundaries,
    clip_item,
    compact,
    plan,
    render_transcript,
    unused_call_ids,
)
from minicodex.history import (
    AssistantMessage,
    History,
    HistoryError,
    SystemNote,
    ToolResult,
    UserMessage,
)
from minicodex.model import ChatCompletionsModel, Completed, TextDelta, Usage
from minicodex.tokens import (
    CHARS_PER_TOKEN,
    PER_MESSAGE_TOKENS,
    Calibration,
    UncountableContent,
    estimate_messages,
)

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def history_with(pairs: int, *, output: str = "ok", lead: bool = True) -> History:
    h = History()
    if lead:
        h.add_system_note("You are a coding agent.")
        h.add_user("Which Python version does this project support?")
    for i in range(pairs):
        call_id = f"call_{i}"
        h.add_assistant("", [ToolCall(call_id, "run_shell", {}, '{"command": "ls"}')])
        h.add_tool_result(call_id, output)
    return h


async def constant_summary(request: SummaryRequest) -> str:
    return "## Goal\nfind out\n## Done\nran ls\n## Open\nnothing\n"


def sse_transport(body: str) -> httpx.MockTransport:
    """Serve one canned server-sent-events response to the real client.

    The point is that the bytes take the same path they take in production:
    through `stream()`, its status check, its `aiter_lines`, and its parser.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=body, headers={"content-type": "text/event-stream"})

    return httpx.MockTransport(handler)

```

> - import 了这一章所有要测的东西；和第 5 章一样，**动手时先只保留已经写出来的那些**，后面逐节补上。
> - **`history_with(pairs, output=..., lead=...)`**：搭一个测试用的历史——可选的开头（一条系统消息 + 一条用户消息），
>   加上 `pairs` 对"调用 / 结果"。`output` 控制每个结果有多长，后面要靠它造"很大的历史"。
> - **`constant_summary`**：一个假的"摘要器"，永远返回同一段文字。真正的摘要器要调用模型，测试里用不着。
> - **`sse_transport(body)`**：§12 才用到，到时候再讲。

然后是"切点"和"受保护前缀"的测试：

```python
def test_F06_01_02_03_boundaries_match_what_the_server_accepts():
    """The cut points this code calls legal are the ones the API answered 200 to.

    Recorded against gpt-4o-mini, 2026-08-10 (`probe_cut_points.py`): indices
    0, 1, 2, 4 and 6 returned 200; 3, 5 and 7 returned 400 with `messages with
    role 'tool' must be a response to a preceeding message with 'tool_calls'`.
    """
    items = history_with(3).items
    assert len(items) == 8
    assert boundaries(items) == (0, 1, 2, 4, 6, 8)


def test_F06_01_dropping_the_oldest_half_is_right_only_by_parity():
    """The listed fault says half-cutting orphans a result.  On an even history
    it does not -- which is worse, because it means the bug ships."""
    even = history_with(3).items  # 8 items, half is index 4: legal
    assert len(even) // 2 in boundaries(even)

    odd = history_with(3)
    odd.add_assistant("thinking out loud")  # 9 items; half is index 4, still legal
    odd_items = odd.items
    assert len(odd_items) // 2 in boundaries(odd_items)
    # ...and keeping the last four lands on a result, which is a 400.  Neither
    # rule knows which case it is in, because neither rule looks.
    assert len(odd_items) - 4 not in boundaries(odd_items)


def test_F06_02_token_count_cut_lands_inside_a_turn():
    items = history_with(3).items
    # "Keep roughly the last 5 messages", which is what a token-driven cut
    # degenerates into, is illegal here and legal one message either side.
    assert 3 not in boundaries(items)
    assert 4 in boundaries(items)


def test_F06_03_keep_last_n_can_start_on_a_result():
    items = history_with(3).items
    assert unused_call_ids(items[-1:]) == ("call_2",)
    assert unused_call_ids(items[-2:]) == ()


async def test_F06_01_rebuilding_an_illegal_cut_raises_locally():
    """The invariant from chapter 1 is what makes this a local failure.

    An orphaned result does not reach a provider and come back as a 400 -- the
    replay refuses it here, naming the id.
    """
    from minicodex.compaction import _replay

    items = history_with(3).items
    assert isinstance(items[3], ToolResult), "index 3 is the cut the server answered 400 to"
    with pytest.raises(HistoryError) as excinfo:
        _replay(History(), items[3:])
    assert "no unanswered call" in str(excinfo.value)
    assert items[3].call_id in str(excinfo.value)


def test_F06_06_protected_prefix_is_notes_then_first_user_message():
    items = history_with(2).items
    assert Protected.of(items).count == 2
    assert isinstance(items[0], SystemNote)
    assert isinstance(items[1], UserMessage)


def test_F06_06_later_system_notes_are_not_protected():
    """Chapter 0's turn-budget warning is a SystemNote and is worth one turn."""
    h = history_with(1)
    h.add_system_note("You have 2 turn(s) left.")
    h.add_user("carry on")
    assert Protected.of(h.items).count == 2


async def test_an_assistant_message_with_no_calls_is_a_boundary():
    h = history_with(2)
    h.add_assistant("here is what I found")
    assert len(h.items) in boundaries(h.items)
    assert isinstance(h.items[-1], AssistantMessage)
```

> - **第一个**把"我们算出的合法切点"和"服务端实测接受的切点"绑在一起，docstring 里写明了是哪天、对哪个模型、用哪个脚本测的。
>   **这个测试的价值不在于它会红**，而在于：以后服务端行为变了，你能很快知道该重跑什么。
> - **第二个**：八条的历史砍一半合法；加一条变成九条，砍一半（下标 4）仍然合法，但"留最后四条"（下标 5）落在结果上。
>   两条规则都不知道自己处在哪种情况里。
> - **第三、四个**：下标 3 不合法、下标 4 合法；只留最后一条时，那一条是没主人的结果。
>   `unused_call_ids(items)` 是一个诊断用的小函数，列出"结果在、调用不在"的 id，代码在 §21。
> - **第五个**：把从下标 3 开始的片段重放进一个空的 `History`，在本地抛 `HistoryError`，消息里有那个调用的 id。
> - **第六、七个**：受保护的是"开头的系统消息 + 第一条用户消息"；后来的系统消息不受保护。
> - **最后一个**：一条没有发起调用的助手消息之后，也是合法切点。

这时 `tests/test_compaction.py` 里 `unused_call_ids` 还没写出来，先把 §21 里的那个函数抄进 `compaction.py`（十来行）。

```bash
git add src/minicodex/compaction.py tests/test_compaction.py probe_naive_cut.py probe_cut_points.py
git commit -m "feat(compaction): find every legal cut point, and never cut the question"
```

---

## §9 什么时候压缩：先得估出请求有多大

切法解决了，还剩一个问题：**怎么知道该压了？**

压缩必须在请求**发出去之前**触发，所以需要一个服务端还没算过的数。最常见的做法是"字符数除以 4"：

```python
def estimate(messages: list[dict]) -> int:
    chars = 0
    for m in messages:
        chars += len(m.get("content") or "")
        for call in m.get("tool_calls") or []:
            chars += len(call["function"]["name"]) + len(call["function"]["arguments"])
    return chars // 4
```

这个 4 是哪来的？"英文大约 4 个字符一个 token"。它对不对，不该查文档，该**量**。新建 `probe_tokens.py`：

```python
"""How wrong is chars/4?

Posts several histories of different shapes and compares the local estimate
with the `prompt_tokens` the server reports.  `max_tokens=1` because the
completion is irrelevant -- only the prompt side is being measured.

    python probe_tokens.py
"""

from __future__ import annotations

import argparse
import json
import os
import sys

import httpx
from probe_naive_cut import TOOLS, build_history

CHARS_PER_TOKEN = 4


def estimate(messages: list[dict]) -> int:
    """The obvious first estimator: count the characters, divide by four."""
    chars = 0
    for m in messages:
        chars += len(m.get("content") or "")
        for call in m.get("tool_calls") or []:
            chars += len(call["function"]["name"]) + len(call["function"]["arguments"])
    return chars // CHARS_PER_TOKEN


CASES: dict[str, list[dict]] = {
    "prose only": [{"role": "user", "content": "Explain what a tool call is, briefly."}],
    "long prose": [{"role": "user", "content": "The quick brown fox. " * 200}],
    "agent history": build_history(),
    "shell output": [
        {"role": "user", "content": "what is here"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "c1",
                    "type": "function",
                    "function": {"name": "run_shell", "arguments": '{"command": "ls -la"}'},
                }
            ],
        },
        {
            "role": "tool",
            "tool_call_id": "c1",
            "content": "\n".join(
                f"-rw-r--r--  1 me  staff   {i * 137:>6} Aug  6 12:0{i % 10} file_{i}.py"
                for i in range(40)
            ),
        },
    ],
    "json blob": [
        {"role": "user", "content": json.dumps({"k" + str(i): {"v": i} for i in range(80)})}
    ],
    "cjk": [{"role": "user", "content": "这是一个中文的工具调用说明。" * 40}],
    "source code": [
        {
            "role": "user",
            "content": "def f(x: int) -> int:\n    return x * 2 + 1\n\n" * 40,
        }
    ],
}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base-url", default="https://api.openai.com/v1")
    ap.add_argument("--model", default="gpt-4o-mini")
    ap.add_argument("--with-tools", action="store_true", help="send the tool schemas too")
    args = ap.parse_args()
    key = os.environ.get("OPENAI_API_KEY") if "openai.com" in args.base_url else None
    headers = {"Content-Type": "application/json"}
    if key:
        headers["Authorization"] = f"Bearer {key}"

    print(f"{'case':<16} {'chars':>7} {'est':>7} {'actual':>7} {'est/actual':>11}")
    print("-" * 54)
    for label, messages in CASES.items():
        body = {"model": args.model, "messages": messages, "stream": False, "max_tokens": 1}
        if args.with_tools:
            body["tools"] = TOOLS
        resp = httpx.post(
            f"{args.base_url}/chat/completions", json=body, headers=headers, timeout=120.0
        )
        payload = resp.json()
        if "error" in payload:
            print(f"{label:<16} {payload['error']['message'][:40]}")
            continue
        actual = payload["usage"]["prompt_tokens"]
        est = estimate(messages)
        chars = sum(len(m.get("content") or "") for m in messages)
        print(f"{label:<16} {chars:>7} {est:>7} {actual:>7} {est / actual:>10.2f}x")
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

> - `CASES`：七种不同内容的消息——短句、长的英文散文、上一节那段 Agent 历史、`ls -la` 的输出、一大块 JSON、中文、Python 源码。
> - 每种都发一次非流式请求，`max_tokens` 设为 1（我们不关心回答，只要服务端在 `usage.prompt_tokens` 里报出**输入**有多少 token）。
> - 打印：字符数、我们的估计、服务端的真实值、两者之比。

```
$ uv run python probe_tokens.py
case               chars     est  actual  est/actual
------------------------------------------------------
prose only            37       9      16       0.56x
long prose          4200    1050    1008       1.04x
agent history        158      64     127       0.50x
shell output        2281     577    1185       0.49x
json blob           1420     355     807       0.44x
cjk                  560     140     327       0.43x
source code         1760     440     767       0.57x
```

**"英文散文 4 个字符一个 token"是对的**——`long prose` 那行 1.04 倍，几乎完美。**别的全都不对。**

| 内容 | 实际每个 token 的字符数 |
|---|---|
| 英文散文 | 4.17 |
| Python 源码 | 2.29 |
| `ls -la` 的输出 | 1.93 |
| JSON | 1.76 |
| 中文 | 1.71 |

两端差 2.4 倍。而**一个 Agent 的历史几乎全在密集的那一端**——命令输出、源码、JSON 参数、错误堆栈。

> **没有一个除数是对的。** 调出一个"更准的常数"，只是把它调到了手头这批样本上，换一个任务又偏了。

更要命的是**方向**。七行里有六行小于 1，也就是**估少了**。估少意味着压缩**触发得太晚**，而"太晚"在这里的意思是：请求已经超了。

**F06-07 成立，而且比猜的严重：不是差 30%，是最多差到一半以上，而且总是往危险的方向差。**

### 9.1 还漏算了两样东西

同一个脚本，加上 `--with-tools`（把工具的 schema 也带上）：

```
$ uv run python probe_tokens.py --with-tools
case               chars     est  actual  est/actual
------------------------------------------------------
prose only            37       9      69       0.13x
long prose          4200    1050    1061       0.99x
agent history        158      64     176       0.36x
...
```

同样一条 37 个字符的消息，带上两个玩具工具的 schema，真实的 token 数从 16 涨到 69。**多出来的 53 个，估计器一个都没算。**

工具的 schema 不在 `messages` 里，所以很容易被当成"不属于对话"而排除在外。这句话是对的，也是没用的：
它**每一轮都发**，历史压缩了它**也不会变小**。

第二样是每条消息的固定开销。不带工具时，37 个字符估 9、实际 16——差的 7 个是 role、分隔符和服务端加的包装。

---

## §10 让它自己纠正自己

补上那两项之后还是不准，因为除数的问题没解决。但有一个信息一直摆在那里没被用：

**服务端每次都会告诉你，这次请求实际用了多少 token。**

那个数比任何本地估计都权威，而且它对应的请求只比当前这次早一轮。一次会话里内容的成分基本不变（都是同一个项目的代码和命令输出），
所以上一轮"**实际值 ÷ 估计值**"的比例，对这一轮基本适用。

新建 `probe_calibration.py`，比较三个估计器在一段逐渐变长的会话上的表现：

```python
"""Does the previous turn's reported usage predict this turn's?

A raw estimate is wrong by a factor that depends on what is in the history --
prose tokenises at ~4.2 chars/token, JSON at ~1.8.  No constant fixes that.
The question is whether the number the server already told us is a usable
correction for the next turn.

Three estimators are compared as the history grows:

    v1   chars/4 over message content and tool-call arguments
    v2   v1 plus the two inputs v1 forgets: the tool schemas, which are sent
         on every request, and a per-message framing cost
    v2c  v2 corrected by the ratio observed on the previous turn

    python probe_calibration.py
"""

from __future__ import annotations

import argparse
import json
import os
import sys

import httpx
from probe_naive_cut import TOOLS

CHARS_PER_TOKEN = 4.0
PER_MESSAGE_TOKENS = 4  # role, separators, and the framing the server adds


def estimate_v1(messages: list[dict], tools: list[dict]) -> float:
    chars = 0
    for m in messages:
        chars += len(m.get("content") or "")
        for call in m.get("tool_calls") or []:
            chars += len(call["function"]["name"]) + len(call["function"]["arguments"])
    return chars / CHARS_PER_TOKEN


def estimate_v2(messages: list[dict], tools: list[dict]) -> float:
    base = estimate_v1(messages, tools)
    base += PER_MESSAGE_TOKENS * len(messages)
    base += len(json.dumps(tools)) / CHARS_PER_TOKEN
    return base


OUTPUTS = [
    "pyproject.toml\nsrc\ntests\nREADME.md\nuv.lock\n",
    json.dumps({"deps": {f"pkg{i}": f">=1.{i}" for i in range(30)}}),
    "\n".join(f"src/minicodex/mod_{i}.py:{i * 7}: def handler_{i}(x):" for i in range(30)),
    "Traceback (most recent call last):\n" + '  File "a.py", line 3, in f\n' * 20,
    "这是一个中文的说明文件，描述了工具的用途。\n" * 15,  # noqa: RUF001 -- CJK punctuation is the measurement
    "def handler(x: int) -> int:\n    return x * 2\n\n" * 25,
    "\n".join(f"{i:>4}  commit {i:040x}  fix: something in module {i}" for i in range(25)),
    "PASSED tests/test_shell.py::test_one\n" * 30,
]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base-url", default="https://api.openai.com/v1")
    ap.add_argument("--model", default="gpt-4o-mini")
    args = ap.parse_args()
    key = os.environ.get("OPENAI_API_KEY") if "openai.com" in args.base_url else None
    headers = {"Content-Type": "application/json"}
    if key:
        headers["Authorization"] = f"Bearer {key}"

    messages: list[dict] = [
        {"role": "system", "content": "You are a coding agent working in /repo."},
        {"role": "user", "content": "Summarise this project's dependencies."},
    ]
    ratio = 1.0

    print(
        f"{'turn':>4} {'actual':>7} | {'v1':>7} {'err':>7} | {'v2':>7} {'err':>7} "
        f"| {'v2c':>7} {'err':>7}"
    )
    print("-" * 70)
    errs: dict[str, list[float]] = {"v1": [], "v2": [], "v2c": []}

    for turn, output in enumerate(OUTPUTS):
        v1 = estimate_v1(messages, TOOLS)
        v2 = estimate_v2(messages, TOOLS)
        v2c = v2 * ratio

        body = {
            "model": args.model,
            "messages": messages,
            "tools": TOOLS,
            "stream": False,
            "max_tokens": 64,
        }
        resp = httpx.post(
            f"{args.base_url}/chat/completions", json=body, headers=headers, timeout=120.0
        )
        payload = resp.json()
        if "error" in payload:
            print(payload["error"]["message"][:200])
            return 1
        actual = payload["usage"]["prompt_tokens"]

        row = {"v1": v1, "v2": v2, "v2c": v2c}
        for name, value in row.items():
            errs[name].append((value - actual) / actual)
        print(
            f"{turn:>4} {actual:>7} | {v1:>7.0f} {(v1 - actual) / actual:>6.0%} "
            f"| {v2:>7.0f} {(v2 - actual) / actual:>6.0%} "
            f"| {v2c:>7.0f} {(v2c - actual) / actual:>6.0%}"
        )

        ratio = actual / v2 if v2 else 1.0

        call_id = f"call_{turn}"
        messages.append(
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": call_id,
                        "type": "function",
                        "function": {
                            "name": "run_shell",
                            "arguments": json.dumps({"command": f"step {turn}"}),
                        },
                    }
                ],
            }
        )
        messages.append({"role": "tool", "tool_call_id": call_id, "content": output})

    print("-" * 70)
    for name, values in errs.items():
        worst = max(values, key=abs)
        under = min(values)
        print(f"{name:>4}  worst {worst:>7.0%}   most negative {under:>7.0%}")
    print(
        "\nNegative means underestimating, which is the direction that overflows"
        "\nthe window before compaction ever fires."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

> - `estimate_v1`：原始的字符数除以 4。`estimate_v2`：补上"每条消息的固定开销"和"工具 schema"。
> - `OUTPUTS`：八段不同类型的工具输出（文件列表、JSON、搜索结果、错误堆栈、中文、源码、提交记录、测试输出），每一轮往历史里加一段。
> - 每一轮：先算三个估计（`v2c` 是 `v2` 乘上**上一轮**观察到的比例），再真发一次请求拿到实际值，然后用这一轮的"实际 ÷ v2"更新比例。
> - 最后打印每个估计器最差的一次。

```
$ uv run python probe_calibration.py
turn  actual |      v1     err |      v2     err |     v2c     err
----------------------------------------------------------------------
   0      77 |      20   -75% |     125    62% |     125    62%
   1     116 |      38   -67% |     151    30% |      93   -20%
   2     443 |     185   -58% |     307   -31% |     235   -47%
   3    1007 |     543   -46% |     673   -33% |     972    -4%
   4    1299 |     700   -46% |     837   -36% |    1253    -4%
   5    1518 |     790   -48% |     935   -38% |    1451    -4%
   6    1942 |    1084   -44% |    1238   -36% |    2010     3%
   7    2690 |    1608   -40% |    1770   -34% |    2776     3%
```

- `v1`（字符数除以 4）：**稳定估少 40%–75%，八轮没有一轮是对的。**
- `v2`（补上两项）：前两轮估多了，之后稳定估少 31%–38%。除数的问题还在。
- `v2c`（用上一轮的比例修正）：**从第 3 轮起，误差在 4% 以内。**

前两轮 `v2c` 跳得很厉害，因为观察样本只有一个，而且历史很小。**这不要紧**：只有两条消息的历史撑不爆窗口。
压缩真正需要准确的时候，恰好是它已经准了的时候。

### 10.1 `tokens.py`

新建 `src/minicodex/tokens.py`，全部内容：

```python
"""Guessing how big a request is, and then correcting the guess.

Compaction has to fire *before* the request is sent, so it needs a number the
server has not produced yet.  Every local estimate is wrong; what matters is
how wrong, and in which direction.

Measured against gpt-4o-mini on 2026-08-10 (`probe_tokens.py`), comparing
`chars // 4` with the `prompt_tokens` the server reported:

    case              chars     est  actual   est/actual
    prose only           37       9      16       0.56x
    long prose         4200    1050    1008       1.04x
    agent history       158      64     127       0.50x
    shell output       2281     577    1185       0.49x
    json blob          1420     355     807       0.44x
    cjk                 560     140     327       0.43x
    source code        1760     440     767       0.57x

English prose really does run at about 4.2 characters per token.  Nothing else
does: JSON runs at 1.8, CJK at 1.7, `ls -la` output at 1.9.  **There is no
divisor that works**, because the spread between the extremes is 2.4x, and an
agent's history is mostly the dense end -- command output, source code, JSON
arguments.  A tuned constant would be tuned for whatever happened to be in the
sample.

Worse, every error above is in the same direction: the estimate is *low*.  A
low estimate means compaction fires late, and "late" here means the request is
already over the limit.

So this module does three things:

1. counts the inputs `chars // 4` forgets -- the tool schemas, which are re-sent
   on every single turn, and a per-message framing cost;
2. **corrects itself from the number the server already told us**;
3. refuses to guess about content it does not model, rather than returning a
   confidently small number for it.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any

# The prose figure, used as the starting point.  It is deliberately the
# optimistic end: `Calibration` moves it, and a starting point that is too
# pessimistic wastes the whole window on turn one, before any observation
# exists to correct it.
CHARS_PER_TOKEN = 4.0

# Every message costs a few tokens beyond its content: the role, the
# separators, and whatever framing the server wraps it in.  Measured: a
# 37-character user message reports 16 prompt tokens, where the content alone
# accounts for 9.
PER_MESSAGE_TOKENS = 4


class UncountableContent(RuntimeError):
    """A message this module cannot size, and will not pretend to.

    The multi-modal shape -- `content` as a list of parts rather than a string
    -- is the case that matters.  `len()` of a two-element list is 2, which
    divided by four is 0, so an image would be counted as free.  A budget that
    silently values its largest item at zero is worse than no budget: it fires
    late *and* reports that everything is fine.

    Per-modality estimation is F06-10 and is not implemented here -- an image's
    token cost depends on its dimensions and the provider's tiling rules, and
    this project has no image path to measure against.  Raising is the honest
    version of not having done it.
    """


def _content_chars(content: Any) -> int:
    if content is None:
        return 0
    if isinstance(content, str):
        return len(content)
    raise UncountableContent(
        f"cannot size message content of type {type(content).__name__}; "
        "only text is modelled (F06-10). Add a per-modality estimator before "
        "sending this."
    )


def estimate_messages(
    messages: Sequence[dict[str, Any]],
    tools: Sequence[dict[str, Any]] = (),
    *,
    chars_per_token: float = CHARS_PER_TOKEN,
) -> int:
    """The size of one request, in tokens, before it is sent.

    `tools` is not optional in practice.  It was left out of the first version
    on the grounds that it is "not part of the conversation", which is true and
    irrelevant: it is part of every request, it does not shrink when the
    history is compacted, and at 53 tokens for two toy schemas it is the entire
    budget of a short session.  Chapter 9 makes this the dominant term.
    """
    chars = 0
    for message in messages:
        chars += _content_chars(message.get("content"))
        for call in message.get("tool_calls") or []:
            function = call.get("function") or {}
            chars += len(function.get("name") or "")
            chars += len(function.get("arguments") or "")
    total = chars / chars_per_token + PER_MESSAGE_TOKENS * len(messages)
    if tools:
        total += len(json.dumps(list(tools))) / chars_per_token
    return int(total)


class Calibration:
    """The correction factor, learned from what the server charged.

    The server reports `prompt_tokens` for the request it just answered.  That
    request is one turn older than the one being sized now, and within a
    session the content mix barely changes, so the ratio carries over.

    Measured over an eight-turn session (`probe_calibration.py`), error against
    the reported figure:

        turn   actual   raw estimate   corrected
           0       77           +62%        +62%
           1      116           +30%        -20%
           2      443           -31%        -47%
           3     1007           -33%         -4%
           4     1299           -36%         -4%
           5     1518           -38%         -4%
           6     1942           -36%         +3%
           7     2690           -34%         +3%

    Two things to read out of that.  The correction converges -- from turn 3 on
    it is within 4%, against a raw estimate stuck at -35%.  And it is useless
    for the first two turns, which is fine, because a two-message history is
    not what overflows a window.

    It is not a general tokeniser and does not try to be.  It is a running
    answer to one question: for *this* conversation, how wrong am I?
    """

    def __init__(self) -> None:
        self._ratio: float | None = None
        self.observations = 0

    @property
    def ratio(self) -> float:
        """1.0 until the server has said something.  Never a guess dressed up
        as a measurement."""
        return self._ratio if self._ratio is not None else 1.0

    @property
    def calibrated(self) -> bool:
        return self._ratio is not None

    def observe(self, *, estimated: int, actual: int) -> None:
        """Record one (guess, truth) pair.

        Guarded rather than trusted: `actual` arrives from a parsed HTTP
        response, and a provider that omits usage sends 0, which would set the
        ratio to 0 and make every future estimate free.
        """
        if estimated <= 0 or actual <= 0:
            return
        self._ratio = actual / estimated
        self.observations += 1

    def correct(self, estimated: int) -> int:
        return int(estimated * self.ratio)

    def describe(self) -> str:
        if not self.calibrated:
            return "uncalibrated (no usage reported yet)"
        return f"x{self.ratio:.2f} from {self.observations} observation(s)"
```

> - **开头的 docstring** 把 §9 的实测表抄了进去。以后有人想把 4.0 "调准一点"，会先读到为什么调不准。
> - **`CHARS_PER_TOKEN = 4.0`**：故意选最乐观的那一端。为什么不选保守的 1.7？在有观察之前，1.7 会让估计虚高一倍多，
>   **第一轮就触发压缩**，把一个刚开始的会话压掉。"乐观的起点 + 尽快校准"比"永远保守"划算。这是权衡，不是定理，所以写在常量旁边。
> - **`PER_MESSAGE_TOKENS = 4`**：每条消息的固定开销。
> - **`UncountableContent` 和 `_content_chars`**：内容是 `None` 算 0（助手发起工具调用时内容就是空的）；是字符串就数长度；
>   **是别的东西就抛异常**。为什么抛，§18 讲。
> - **`estimate_messages(messages, tools, ...)`**：字符数（内容 + 工具调用的名字和参数）除以 4，加上每条消息的开销，
>   再加上工具 schema 的字符数除以 4。`json.dumps(list(tools))` 把 schema 变回它发送时的文字来数长度。
>   参数里单独的 `*` 表示它后面的参数只能写成 `chars_per_token=...` 的形式。
> - **`Calibration`**：记住"实际 ÷ 估计"的比例。
>   - `ratio`：还没有任何观察时是 `1.0`——**不把猜测伪装成测量**。`@property` 让它用起来像一个属性。
>   - `observe(estimated=..., actual=...)`：记下一组（估计，实际）。**`if estimated <= 0 or actual <= 0: return`** 不是走形式：
>     `actual` 来自解析出来的 HTTP 响应，一个不报用量的服务端会让它变成 0，比例跟着变成 0，
>     **从此每个请求都被估成 0 个 token，压缩永远不触发**。一个兜底值悄悄关掉一个功能——这本书里反复出现的形状。
>   - `correct(estimated)`：估计值乘以比例。`describe()`：给人看的一句话。

测试（加到 `tests/test_compaction.py`）：

```python
def test_F06_07_estimate_counts_the_tool_schemas():
    """The first version did not, and the schemas are re-sent every turn."""
    messages = [{"role": "user", "content": "hello"}]
    tools = [{"type": "function", "function": {"name": "run_shell", "description": "x" * 400}}]
    assert estimate_messages(messages, tools) > estimate_messages(messages) + 90


def test_F06_07_estimate_counts_a_cost_per_message_not_just_per_character():
    """Also a mutation-testing find: setting `PER_MESSAGE_TOKENS = 0` left all
    247 tests green, so the framing cost was a constant nothing depended on.

    Measured: a 37-character user message reports 16 prompt tokens, where the
    content alone accounts for 9.  Splitting the same text across more messages
    therefore costs more, and an agent history is many small messages.
    """
    one = [{"role": "user", "content": "abcd" * 20}]
    many = [{"role": "user", "content": "abcd" * 4} for _ in range(5)]
    assert sum(len(m["content"]) for m in one) == sum(len(m["content"]) for m in many)
    assert estimate_messages(many) > estimate_messages(one)
    assert estimate_messages(many) - estimate_messages(one) == PER_MESSAGE_TOKENS * 4


def test_F06_07_calibration_is_inert_until_the_server_speaks():
    calibration = Calibration()
    assert calibration.calibrated is False
    assert calibration.ratio == 1.0
    assert calibration.correct(500) == 500
    assert "uncalibrated" in calibration.describe()


def test_F06_07_calibration_uses_the_reported_number():
    calibration = Calibration()
    calibration.observe(estimated=700, actual=1007)  # measured turn 3
    assert calibration.correct(700) == 1007
    assert "x1.44" in calibration.describe()


@pytest.mark.parametrize("actual", [0, -1])
def test_F06_07_a_missing_usage_field_cannot_zero_the_ratio(actual):
    """A provider that omits usage parses as 0, and `estimate * 0` is a budget
    that says every request is free."""
    calibration = Calibration()
    calibration.observe(estimated=500, actual=actual)
    assert calibration.ratio == 1.0


def test_chars_per_token_is_the_prose_figure_not_a_tuned_one():
    """4.0 is what English prose measured at (4200 chars / 1008 tokens).

    It is deliberately the optimistic end: `Calibration` moves it, and a
    pessimistic starting point wastes the whole window on turn one, before any
    observation exists to correct it.
    """
    assert CHARS_PER_TOKEN == 4.0
```

> - 带上工具 schema 后估计明显变大；
> - **同样多的字符，拆成更多条消息，估计必须更大**，而且正好大 `PER_MESSAGE_TOKENS × 多出来的条数`。
>   docstring 记下了它的来历：变异测试把这个常量改成 0，当时所有测试全绿——它曾经是一个没有任何东西依赖的数字；
> - 没有观察时校准什么都不做；观察到（700，1007）之后，700 被修正成 1007；
> - 实际值是 0 或负数时，比例不动；
> - `CHARS_PER_TOKEN` 就是散文的那个数，不是调出来的。

```bash
git add src/minicodex/tokens.py tests/test_compaction.py probe_tokens.py probe_calibration.py
git commit -m "feat(tokens): estimate a request, then correct the estimate from usage"
```

---

## §11 意外：校准的数据根本没送过来

上面整套设计有一个前提：服务端会告诉我们用量。

§9、§10 的探针用的都是**非流式**请求（`"stream": False`）。**而我们的客户端是流式的**（第 1 章定的，为了边生成边显示）。
流式响应里有用量吗？测一下（2026-10-01，gpt-4o-mini）：

```python
for label, extra in [
    ("stream, as chapter 1 sends it", {}),
    ("stream + include_usage", {"stream_options": {"include_usage": True}}),
]:
    body = {"model": "gpt-4o-mini", "messages": [{"role": "user", "content": "Say hi."}],
            "stream": True, **extra}
    seen, last = [], None
    with httpx.stream("POST", url, json=body, headers=headers, timeout=60) as r:
        for line in r.iter_lines():
            if line.startswith("data: ") and line[6:] != "[DONE]":
                chunk = json.loads(line[6:])
                last = chunk
                if chunk.get("usage"):
                    seen.append(chunk["usage"]["prompt_tokens"])
    print(f"{label:<32} usage chunks: {len(seen)}  {seen}")
    print(f"{'':<32} last chunk's choices: {last['choices']}")
```

```
stream, as chapter 1 sends it    usage chunks: 0  []
                                 last chunk's choices: [{'index': 0, 'delta': {}, 'logprobs': None, 'finish_reason': 'stop'}]
stream + include_usage           usage chunks: 1  [10]
                                 last chunk's choices: []
```

**零。** 流式请求默认**完全不返回用量**，必须在请求里明确要：`"stream_options": {"include_usage": true}`。

这条故障的形状值得看清楚：

- 没有异常，没有警告，没有 400；
- `Calibration` 老老实实保持 `ratio = 1.0`，`describe()` 老老实实说"uncalibrated (no usage reported yet)"；
- 估计值就那么一直偏低，压缩一直触发得太晚。

**§10 的整套机制存在、正确，并且从未运行过。**

这是 🟡 静默错误里比较隐蔽的一种：不是代码写错了，而是**代码的输入从来没有来过**，而代码对"没有输入"的处理又完全合理。
这条不在清单上。

---

## §12 意外：打开它，第 1 章的解析代码崩了

再看一眼上面输出的第四行：**带用量的那个数据块，`choices` 是空列表。**

而 `model.py` 的解析循环里有这么一行，从第 1 章到现在都没出过问题：

```python
choice = chunk["choices"][0]
```

对空列表取下标 0，是 `IndexError: list index out of range`。它之前一直是对的，因为之前每个数据块都有 `choices`。

发生的时机也值得注意：这个数据块是**最后一个**，在 `[DONE]` 之前。也就是说，**回答已经完整地流完了**，用户已经看到全部输出，
然后程序崩溃。第 2 章的 F02-07（命令的输出不是合法 UTF-8）是同一个形状：事情做完了，崩在处理结果上。

### 12.1 `model.py` 的四处改动

**一，一个新的事件类型**，放在 `Completed` 后面，并加进 `StreamEvent`：

```python
@dataclass(frozen=True)
class Usage:
    """What the server says the request actually cost.

    The only ground truth about token counts this program will ever have, and
    chapter 6 needs it: every local estimate was measured 34-38% low, always in
    the direction that lets a window overflow before compaction fires.

    It has to be asked for.  A streaming request returns **no usage at all**
    unless `stream_options.include_usage` is set -- verified against
    gpt-4o-mini: 0 usage chunks without it, 1 with it.  That is the quiet
    version of this fault.  Nothing errors; the calibration source simply never
    arrives, the ratio stays 1.0, and the estimate stays a third low forever.
    """

    prompt_tokens: int
    completion_tokens: int


StreamEvent = TextDelta | ToolCallDelta | Completed | Usage
```

**二，`ChatCompletionsModel.__init__` 多两个参数** `report_usage: bool = True` 和 `transport: httpx.AsyncBaseTransport | None = None`，
并保存下来：

```python
        # A flag rather than always-on, because it is a request field and not
        # every server speaking this API has to understand it.  Default on: the
        # cost of asking is one key in the body, and the cost of not asking is
        # a compaction trigger that never calibrates.
        self.report_usage = report_usage
        # A seam for tests, added because the alternative was worse.  The first
        # test for the empty-`choices` chunk mirrored the parsing loop below
        # into the test file instead of driving it -- so deleting the guard
        # from *this* module left that test green.  A test that reimplements
        # the code under test measures the copy.  With a transport, the bytes
        # go through the real `stream()`.
        self.transport = transport
```

> - `report_usage` 做成开关而不是写死：它是请求里的一个字段，不是每个说这套协议的服务端都必须认识。默认打开——
>   要的代价是请求里多一个键，不要的代价是一个永远不校准的压缩触发器。
> - `transport` 是给测试用的，§12.2 讲。

**三，`request_body` 里加两行**（在 `body.update(self.extra_body)` 之前）：

```python
        if self.report_usage:
            body["stream_options"] = {"include_usage": True}
```

**四，`stream()` 里**，创建客户端时把 `transport` 传进去：

```python
        async with httpx.AsyncClient(timeout=self.timeout, transport=self.transport) as client:
```

以及在 `chunk = json.loads(payload)` 之后、`choice = chunk["choices"][0]` 之前，加上：

```python
                    # The usage chunk arrives with `"choices": []`, so the
                    # `chunk["choices"][0]` that worked for five chapters
                    # becomes an IndexError the moment usage is switched on.
                    # Verified against gpt-4o-mini: the second-to-last chunk
                    # has an empty choices list and a populated `usage`.
                    if chunk.get("usage"):
                        yield Usage(
                            chunk["usage"].get("prompt_tokens", 0),
                            chunk["usage"].get("completion_tokens", 0),
                        )
                    if not chunk.get("choices"):
                        continue

                    choice = chunk["choices"][0]
```

> **顺序有讲究：先取用量，再判断"没有 choices 就跳过"。** 反过来写，用量永远取不到——带用量的数据块正好就是 `choices` 为空的那个。
> `continue`：跳过这一轮循环剩下的部分，直接处理下一个数据块。

### 12.2 一个测试，和它没测到的东西

这个修复的第一版测试，是在测试文件里**照着 `stream()` 抄了一份解析循环**，然后喂给它两个假的数据块——
因为真正的解析循环在 `stream()` 里面，而 `stream()` 要建立网络连接。测试绿了，看起来很合理。

后来做变异测试，把 `model.py` 里"没有 choices 就跳过"那两行删掉：**0 个测试失败。**

因为那个测试测的是**抄的那一份**。`model.py` 里的保护删了，测试文件里的副本还在，测试照样绿。

> 这和第 5 章那句话是同一件事：**一个专门用来"检查别的东西"的东西，最容易犯的错就是以为自己检查过了。**

正确的修法不是"抄得再仔细一点"，而是**让数据走真正的那条路**。`httpx` 提供了 `MockTransport`：一个假的"网络"，
请求不会真的发出去，而是交给你写的函数来回答。这就是 `transport` 参数的用途，也是 §8.1 里那个 `sse_transport` 帮手做的事：
把一段写好的流式响应交给**真正的** `stream()` 去解析。

```python
def test_F06_07_usage_must_be_requested_explicitly():
    """Measured: a streaming request returns 0 usage chunks without this, and
    1 with it.  Nothing errors -- the calibration source simply never arrives."""
    body = ChatCompletionsModel().request_body([{"role": "user", "content": "hi"}])
    assert body["stream_options"] == {"include_usage": True}
    off = ChatCompletionsModel(report_usage=False).request_body([])
    assert "stream_options" not in off


async def test_F06_07_the_usage_chunk_has_no_choices():
    """The chunk carrying usage arrives with `"choices": []`.

    `chunk["choices"][0]` worked for five chapters and becomes an IndexError
    the moment usage is switched on -- after the answer has already streamed.

    Driven through the real `stream()` over a mock transport.  The first
    version of this test mirrored the parsing loop into the test file, which
    made it green with the guard deleted from the module: mutation testing
    caught it, and it is the same fault as chapter 5's -- a check that does not
    reach the thing it claims to check.
    """
    body = (
        'data: {"choices":[{"delta":{"content":"hi"},"finish_reason":"stop"}]}\n\n'
        'data: {"choices":[],"usage":{"prompt_tokens":77,"completion_tokens":9}}\n\n'
        "data: [DONE]\n\n"
    )
    model = ChatCompletionsModel(transport=sse_transport(body))
    events = [event async for event in model.stream([{"role": "user", "content": "hi"}])]
    assert Usage(77, 9) in events
    assert TextDelta("hi") in events
    assert Completed("stop") in events
```

> - **第一个**：默认的请求里有 `stream_options`；关掉开关就没有。
> - **第二个**：三行假的流式数据——一个带文字的块、一个 `choices` 为空但带用量的块、`[DONE]`。
>   `[event async for event in model.stream(...)]` 是"异步的列表推导"，把流里的事件全部收集起来。
>   断言：用量事件、文字事件、结束事件都在。**现在删掉那两行保护，这个测试会红。**

> **"为了测试给正式代码加一个参数"** 常被说成"测试污染了正式代码"。这里它是对的，理由不是"方便测试"，而是**没有它就没有测试**——
> 另一个选择是抄一份解析逻辑，而那份副本会独立地一直正确下去，和真代码无关。

```bash
git add src/minicodex/model.py tests/test_compaction.py
git commit -m "fix(model): ask for usage, and survive the chunk that carries it"
```

---

## §13 摘要该写什么

切掉的那一段不能凭空消失，要换成一份摘要。最直接的做法：

```python
NAIVE_PROMPT = "Summarise the conversation above so it can be continued later."
```

这句话哪里不对？它没说**摘要是给谁看的、拿来干什么**。模型会写出一份给人看的会议纪要：流畅、连贯、按时间顺序。
而下一轮真正需要的是：

- 已经做完的事（**别重做**）；
- 已经定下的决定（**别重新讨论**）；
- 用户说过的约束（**别违反**）；
- 花了代价才试出来的具体字符串（**别再试一遍**）。

这四样在"流畅的纪要"里全是可有可无的细节。所以提示词要求六个固定的小节。新建 `src/minicodex/prompts/compaction.md`，
下面是它的最终版本（其中有两处是后面两节的故事逼出来的，到时候会指给你看）：

```markdown
You are compressing the middle of an agent's working transcript so the session
can continue past its context limit. The result replaces the messages below
permanently. Nothing that is not in your output can be recovered.

Write these six sections, in this order, with these exact headings. Write
`(none)` under a heading rather than omitting it -- a missing heading reads as
"this was not discussed", and the reader cannot tell that apart from "this was
lost".

## Goal
What the user asked for, in their terms. **Take this from the `<context>`
block, which holds the opening of the conversation. Do not infer it from the
transcript.** The transcript is the middle of a session: its last few steps look
like the goal and are not. If `<context>` states the task, restate that task.

## Done
Work that is **finished and must not be repeated**. Name the concrete artefact
for each one -- the file that now exists, the command that now passes, the
value that was found. A step with no artefact named is not done.

## Decisions
Choices that were made and are now settled, each with the reason. These are the
ones that get silently re-litigated after compaction if they are not written
down.

## Constraints
Requirements stated by the user, and limits discovered by running things:
versions, paths, things that failed and why, things that must not be touched.

## Open
What is still unfinished, and the immediate next action.

## Key data
Exact strings that would cost a tool call to obtain again: paths, identifiers,
error messages, versions, command output that was hard to get. Quote them.

You are given up to three blocks. Only one of them is being replaced:

- `<context>` -- the opening of the conversation. **It is not being deleted**
  and does not need compressing. It is here so your summary agrees with it.
- `<established>` -- the surviving record of an earlier compaction, if any.
- `<transcript>` -- the messages that are about to be destroyed. This is the
  only thing you are summarising.

Rules:

- Facts only. No advice, no summary of your own reasoning, no "the agent then
  decided to". If it is not something the next turn needs, leave it out.
- Preserve exact strings exactly. A path retyped from memory is a bug.
- If an **established** block appears below, it is the surviving record of an
  earlier compaction. Its content has already outlived the transcript it came
  from. Carry it forward into the sections above, unchanged in meaning and
  unchanged in its exact strings. Do not compress it further and do not drop an
  item because it looks old.
```

几句不显眼但要紧的话：

- **"A step with no artefact named is not done."**（没点名产物的步骤不算做完。）不这么要求，模型会写"实现了重试逻辑"，
  下一轮读到这句，没法判断文件到底存不存在。要求点名产物，这句话就变成"写了 `src/net.py`：加了 `retry()`"，可以核对。
- **"Write `(none)` under a heading rather than omitting it."**（没有内容就写"无"，不许省略小节。）
  少一个小节，读起来像"这件事没讨论过"，而它真正的意思可能是"这件事丢了"。读的人分不出这两种。
- **"Exact strings that would cost a tool call to obtain again."** 这是 `## Key data` 存在的唯一理由。
  值不值得写进摘要，标准不是"重不重要"，而是"重新拿到它要花几次工具调用"。

`src/minicodex/__init__.py` 里加一个读它的函数（并把名字加进 `__all__`）：

```python
def compaction_prompt() -> str:
    """The instructions given to the model that summarises a dropped transcript.

    A file rather than a string constant, for the same reason as the other two:
    it is prose that will be edited by someone reading it as prose, and chapter
    13 puts every one of these under a snapshot test.
    """
    return (_PROMPTS / "compaction.md").read_text(encoding="utf-8")
```

---

## §14 意外：它给我编了一个目标

这一节讲的是提示词里 `## Goal` 那一段和"三个块"那一段的来历。

提示词的第一版没有那两段，交给摘要模型的只有"将要被删掉的那一段"。接上真模型跑，第一份摘要是这样开头的
（2026-08-10 的原始记录，gpt-4o-mini，3 次里 3 次）：

```markdown
## Goal
Record the change in CHANGELOG.md.
```

而会话真正的目标是：

> Add retry logic to src/net.py. It has to stay compatible with Python 3.9, so no match statements.
> Do not touch src/legacy.py under any circumstances.

**`## Goal` 写的是"在 CHANGELOG 里记一笔"——那不是目标，那是剩下的最后一步。**

为什么？回头看 §7：用户的第一条消息是**受保护的**，所以它**不在**交给摘要模型的那一段里。
我们要求模型写一个 `## Goal` 小节，同时把说明目标的那条消息从它眼前拿走了。

它做了一个模型在这种处境下必然会做的事：**从看得见的部分推断出一个目标。** 而看得见的部分，结尾正好是"还剩 CHANGELOG 没写"。

然后这份摘要作为一条系统消息放进历史，**紧挨着真正的用户消息**，两者互相矛盾。

> 这条不在清单上。它是"保护前缀"（F06-06）和"结构化摘要"（F06-04）两个做法**互相作用**出来的——两个单独看都是对的。
> **这类故障没法在设计阶段想出来，只能跑出来。**

### 14.1 修法：给它看，但不让它压

摘要模型需要看到受保护的前缀——**不是为了压缩它**（它本来就原样留着），而是为了让摘要**和它一致**。

于是交给摘要器的东西从一段文字变成了好几样。好几个位置参数很难读，收成一个对象：

```python
@dataclass(frozen=True)
class SummaryRequest:
    """Everything the summariser is allowed to see.

    Three fields rather than one transcript string, and the third one was
    bought with a wrong answer.  The first version passed only the region being
    destroyed, which is exactly the region that does **not** contain the
    protected prefix -- so the model was asked to write a `## Goal` section
    while the message stating the goal was deliberately withheld from it.  It
    did what a model does with a required heading and no evidence: it inferred
    one.  Measured, 3/3 on gpt-4o-mini, the summary of a session about *adding
    retry logic to src/net.py* opened with

        ## Goal
        Record the change in CHANGELOG.md.

    which is not the goal, it is the last remaining task.  That note then sits
    in the history one line below the real user message, contradicting it, with
    the authority of a system note.

    `context` fixes it by handing over the protected prefix as read-only
    material.  It is not summarised and not replaced -- it is still in the
    rebuilt history verbatim -- it is there so the summary can be *consistent*
    with it.
    """

    transcript: str
    """The region about to be destroyed.  The only part being replaced."""

    context: str
    """The protected prefix, verbatim.  Survives on its own; shown for
    agreement, not for compression."""

    established: str | None
    """The previous generation's summary, if this is not the first compaction."""

    generation: int
    """How many compactions this session has already survived."""
```

> - `transcript`：将要被删掉的那一段，唯一真正被替换的东西。
> - `context`：受保护的前缀，原样。给它看是为了保持一致，不是为了压缩。
> - `established`：上一次压缩留下的摘要（§20 讲）；第一次压缩时是 `None`。
> - `generation`：这个会话已经压缩过几次。
> - 字段下面的字符串是"字段的 docstring"——一种给每个字段写说明的习惯写法。
> - 类的 docstring 把这次事故完整记了下来。**以后有人想"简化"回只传一段文字，会先读到这个。**

紧接着定义"摘要器"的类型：

```python
Summariser = Callable[["SummaryRequest"], Awaitable[str]]
```

> 意思是：摘要器是"接收一个 `SummaryRequest`、可以被 `await`、最后得到字符串"的函数。测试里的 `constant_summary` 就符合这个形状。

提示词里对应地加了两段：`## Goal` 一节明确说"**从 `<context>` 里取，不要从 transcript 推断**"，并且补了一句直接描述模型犯的那个错的话——
"The transcript is the middle of a session: its last few steps look like the goal and are not."（transcript 是会话的中段，它最后几步看起来像目标，但不是。）
末尾的"三个块"一段，说明每个块会被怎么处置。

把历史里的对象变成给摘要模型读的文字，用这个函数：

```python
def render_transcript(items: Sequence[HistoryItem]) -> str:
    """The dropped region, as text for the summariser.

    Not `to_wire()`: this is going into a prompt as *content*, and the wire
    format's nesting would spend tokens on JSON punctuation the summariser does
    not need.  Tool results keep their call's name, because "the output of
    `read_file`" and "the output of `run_shell`" mean different things to
    whoever reads this next.
    """
    lines: list[str] = []
    for item in items:
        if isinstance(item, UserMessage):
            lines.append(f"USER: {item.text}")
        elif isinstance(item, SystemNote):
            lines.append(f"SYSTEM: {item.text}")
        elif isinstance(item, AssistantMessage):
            if item.text:
                lines.append(f"ASSISTANT: {item.text}")
            for call in item.tool_calls:
                lines.append(f"ASSISTANT CALLS {call.name}({call.raw_arguments})")
        elif isinstance(item, ToolResult):
            lines.append(f"RESULT OF {item.name}:\n{item.content}")
    return "\n".join(lines)
```

> 不用 `to_wire()`：这段文字是要放进提示词里当**内容**的，发送格式里的层层嵌套只会白花 token。
> 每个结果前面带着产生它的工具名（`RESULT OF read_file:`），因为"`read_file` 的输出"和"`run_shell` 的输出"对读的人意义不同。

真正调用模型的摘要器：

```python
def make_summariser(model: Any, *, dialect: str = "chat_completions") -> Summariser:
    """A `Summariser` backed by one non-agentic model call.

    Its own `History`, not the session's: the summariser must not see the tool
    schemas, must not be able to call anything, and must not have its output
    land in the conversation it is summarising.  Chapter 10 makes this shape
    general; here it is one function.
    """

    async def summarise(request: SummaryRequest) -> str:
        scratch = History()
        scratch.add_system_note(compaction_prompt())
        blocks = [f"<context>\n{request.context}\n</context>"]
        if request.established:
            blocks.append(f"<established>\n{request.established}\n</established>")
        blocks.append(f"<transcript>\n{request.transcript}\n</transcript>")
        scratch.add_user("\n\n".join(blocks))

        parts: list[str] = []
        saw_end = False
        from minicodex.model import Completed, TextDelta

        async for event in model.stream(scratch.to_wire(dialect)):
            if isinstance(event, TextDelta):
                parts.append(event.text)
            elif isinstance(event, Completed):
                saw_end = True
        if not saw_end:
            # The same rule as chapter 0's loop: a stream that stopped early is
            # not a short summary, it is an unknown one.  Half a summary that
            # replaces a whole transcript is worse than admitting the loss.
            raise CompactionError("summariser stream ended without a [DONE] sentinel")
        return "".join(parts)

    return summarise
```

> - 它返回一个函数（`summarise`），那个函数才是摘要器。这种"函数里定义函数再返回"的写法，是为了把 `model` 记在里面。
> - **它用自己的一个新 `History`**，不是会话的那个：摘要模型不该看到工具的 schema，不该能调用任何工具，它的输出也不该落进正在被它摘要的对话里。
> - 把三个块拼成一条用户消息：`<context>`、可选的 `<established>`、`<transcript>`。
> - 读流：收集文字；**没看到结束事件就抛异常**。这和第 0 章是同一条规则——中途断掉的流不是"一份很短的摘要"，而是"一份不知道内容的摘要"。
>   半份摘要替换掉整段历史，比承认丢失更糟，因为它看起来是成功的。
> - `from minicodex.model import ...` 写在函数里面，是为了避免模块之间互相导入绕成一圈。

测试：

```python
async def test_F06_04_summariser_is_given_the_region_being_destroyed():
    seen: list[SummaryRequest] = []

    async def capture(request: SummaryRequest) -> str:
        seen.append(request)
        return "## Done\nnothing\n"

    h = history_with(12, output="z" * 300)
    await compact(h, summarise=capture, budget=600)
    assert len(seen) == 1
    assert "RESULT OF run_shell" in seen[0].transcript


async def test_F06_04_summariser_is_also_given_the_protected_prefix():
    """Found by measurement, not by review.

    Without `context`, gpt-4o-mini wrote `## Goal: Record the change in
    CHANGELOG.md` for a session whose actual goal was adding retry logic --
    3/3.  It had been ordered to produce a Goal section and shown everything
    except the goal, so it inferred one from the most recent work.
    """
    seen: list[SummaryRequest] = []

    async def capture(request: SummaryRequest) -> str:
        seen.append(request)
        return "## Goal\nx\n"

    h = history_with(12, output="z" * 300)
    await compact(h, summarise=capture, budget=600)
    assert "Which Python version does this project support?" in seen[0].context
    # ...and it is context, not material: it stays in the history verbatim.
    assert "RESULT OF" not in seen[0].context


def test_F06_05_prompt_requires_an_artefact_for_every_finished_step():
    from minicodex import compaction_prompt

    text = compaction_prompt()
    for heading in ("## Goal", "## Done", "## Decisions", "## Constraints", "## Open"):
        assert heading in text
    assert "must not be repeated" in text
    assert "A step with no artefact named is not done." in text


def test_F06_05_render_transcript_keeps_the_tool_that_produced_each_result():
    h = history_with(1, output="Python 3.13.0")
    text = render_transcript(h.items)
    assert "RESULT OF run_shell:\nPython 3.13.0" in text
    assert "ASSISTANT CALLS run_shell" in text
```

> - 前两个用一个"记录自己收到了什么"的假摘要器（`capture`）：它收到了被删掉的那一段；**它也收到了受保护的前缀**，
>   而且前缀里没有工具结果（证明它只是参考材料）。这两个测试用到了 `compact()`，它在 §21 才完整出现。
> - 第三个把提示词里几句关键的话钉住；第四个检查渲染出来的文字带着工具名。

---

## §15 量一下：结构化到底值多少

改对了一个例子，不等于改对了。要量，就得有能判定的指标——"摘要质量"没法判定，"某个具体的事实还在不在"可以。

在一段真实形状的会话里**种五个事实**，每一个都是下一轮确实需要的：

| | 事实 | 为什么下一轮需要 |
|---|---|---|
| A | `Python 3.9`，不许用 `match` | 用户的约束，违反了就得返工 |
| B | 不许动 `src/legacy.py` | 用户的禁令，违反了是事故 |
| C | 测试必须加 `-p no:randomly` | **失败过一次才试出来的**，丢了就要再失败一次 |
| D | `src/net.py` 已经写完、测试通过 | 丢了会重做 |
| E | 还剩 `CHANGELOG.md` 没写 | 丢了就不知道接下来干什么 |

新建 `probe_summary.py`：

```python
"""What survives a summary, and what a model does after one.

Three measurements, all on a transcript with five facts planted in it that the
next turn provably needs:

    A  the user's constraint          "Python 3.9 ... no match statements"
    B  the file that must not change  "src/legacy.py"
    C  a flag discovered by failing   "-p no:randomly"
    D  work already finished          "src/net.py" written, tests pass
    E  the one thing still open       "CHANGELOG.md"

    1. retention  -- which of the five appear in the summary, naive vs structured
    2. behaviour  -- given only the compacted history, does the model redo D
    3. decay      -- how many survive after 1, 2, 3, 4, 5 generations

    python probe_summary.py [--samples 3] [--model gpt-4o-mini]

The transcript is padded to a realistic size on purpose.  `plan()` refuses to
compact when the summary would cost more than the region it replaces, and the
first version of this transcript was small enough to hit that guard once it
existed: every run then reported "all facts kept" for both prompts, because
nothing had been compacted at all.  `compacted()` below aborts instead of
printing a table about a compaction that did not happen.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import re

from minicodex import compaction_prompt
from minicodex.agent_types import ToolCall
from minicodex.compaction import (
    CompactionResult,
    SummaryRequest,
    compact,
    make_summariser,
    render_transcript,
)
from minicodex.history import History
from minicodex.model import ChatCompletionsModel

NAIVE_PROMPT = "Summarise the conversation above so it can be continued later."

FACTS = {
    "A constraint": [r"3\.9", r"match statement"],
    "B do-not-touch": [r"legacy\.py"],
    "C discovered flag": [r"no:randomly"],
    "D done": [r"net\.py"],
    "E open": [r"CHANGELOG"],
}


def build_history() -> History:
    h = History()
    h.add_system_note("You are a coding agent working in /repo.")
    h.add_user(
        "Add retry logic to src/net.py. It has to stay compatible with Python 3.9, "
        "so no match statements. Do not touch src/legacy.py under any circumstances."
    )
    steps = [
        ("run_shell", '{"command": "ls src"}', "legacy.py\nnet.py\n__init__.py\n"),
        (
            "read_file",
            '{"path": "src/net.py"}',
            "import httpx\n\n\ndef fetch(url):\n    return httpx.get(url)\n" + NET_PY_REST,
        ),
        (
            "run_shell",
            '{"command": "pytest tests/test_net.py"}',
            "ERROR: plugin randomly reorders these tests and the fixture is "
            "order-dependent\nHINT: rerun with -p no:randomly\n",
        ),
        (
            "run_shell",
            '{"command": "pytest tests/test_net.py -p no:randomly"}',
            "2 passed in 0.31s\n",
        ),
        (
            "apply_patch",
            '{"path": "src/net.py"}',
            "wrote src/net.py: added retry() with exponential backoff and jitter\n",
        ),
        (
            "run_shell",
            '{"command": "pytest tests/test_net.py -p no:randomly"}',
            "5 passed in 0.44s\n",
        ),
    ]
    for index, (name, args, output) in enumerate(steps):
        call_id = f"call_{index}"
        text = ""
        if name == "apply_patch":
            text = (
                "The server sends Retry-After on 429, so I will use exponential "
                "backoff with jitter and honour that header rather than a fixed sleep."
            )
        h.add_assistant(text, [ToolCall(call_id, name, {}, args)])
        h.add_tool_result(call_id, output)
    h.add_assistant(
        "src/net.py now has retry() and the tests pass. Still to do: "
        "record the change in CHANGELOG.md."
    )
    h.add_user("Carry on.")
    return h


# The rest of a plausible src/net.py.  It contains none of the five facts; it
# is here so the dropped region is big enough to be worth a summary.
NET_PY_REST = "".join(
    f"\n\ndef endpoint_{i}(client, payload):\n"
    f'    """Call service {i} and return the decoded body."""\n'
    f'    response = client.post("/v1/service/{i}", json=payload, timeout=10)\n'
    f"    response.raise_for_status()\n"
    f"    return response.json()\n"
    for i in range(24)
)


async def compacted(history: History, summariser) -> CompactionResult:
    result = await compact(history, summarise=summariser, budget=200)
    if result.plan.drops == 0:
        raise SystemExit(
            "nothing was compacted, so there is nothing to measure -- "
            "the transcript is too small for plan() to consider it worth a summary"
        )
    return result


def found(summary: str) -> dict[str, bool]:
    return {
        label: any(re.search(p, summary, re.I) for p in patterns)
        for label, patterns in FACTS.items()
    }


def make_naive_summariser(model):
    async def summarise(request: SummaryRequest) -> str:
        from minicodex.model import Completed, TextDelta

        scratch = History()
        scratch.add_user(f"{request.transcript}\n\n{NAIVE_PROMPT}")
        parts = []
        async for event in model.stream(scratch.to_wire()):
            if isinstance(event, TextDelta):
                parts.append(event.text)
            elif isinstance(event, Completed):
                pass
        return "".join(parts)

    return summarise


async def continuation(model, history: History) -> tuple[str, list[str]]:
    """What the model does next, given only what compaction left behind."""
    from minicodex.model import Completed, TextDelta, ToolCallDelta

    text, calls = [], []
    async for event in model.stream(history.to_wire()):
        if isinstance(event, TextDelta):
            text.append(event.text)
        elif isinstance(event, ToolCallDelta):
            calls.append(f"{event.name}({event.arguments})")
        elif isinstance(event, Completed):
            pass
    return "".join(text), calls


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default="gpt-4o-mini")
    ap.add_argument("--base-url", default="https://api.openai.com/v1")
    ap.add_argument("--samples", type=int, default=3)
    ap.add_argument("--generations", type=int, default=5)
    args = ap.parse_args()

    key = os.environ.get("OPENAI_API_KEY") if "openai.com" in args.base_url else None
    llm = ChatCompletionsModel(base_url=args.base_url, model=args.model, api_key=key)
    # The agent's tools deliberately stay attached for the continuation test:
    # "did it redo the work" is a question about tool calls.
    from minicodex.tools import TOOL_SCHEMAS

    tooled = ChatCompletionsModel(
        base_url=args.base_url, model=args.model, api_key=key, tools=TOOL_SCHEMAS
    )

    history = build_history()
    print(f"transcript: {len(history)} items, {len(render_transcript(history.items))} chars\n")

    # -- 1. retention ------------------------------------------------------
    print("=== 1. which planted facts survive, measured on the WHOLE compacted")
    print("       history -- A and B live in the protected prefix, so looking")
    print("       only at the summary text scores them as lost when they are not")
    print(f"{'prompt':<12} {'sample':<7} " + " ".join(f"{k:<17}" for k in FACTS))
    kept: dict[str, list[int]] = {"naive": [], "structured": []}
    structured_summaries: list[str] = []
    for label, summariser in (
        ("naive", make_naive_summariser(llm)),
        ("structured", make_summariser(llm)),
    ):
        for sample in range(args.samples):
            result = await compacted(history, summariser)
            hits = found(render_transcript(result.history.items))
            kept[label].append(sum(hits.values()))
            if label == "structured":
                structured_summaries.append(result.summary)
            marks = " ".join(f"{'yes' if hits[k] else 'NO':<17}" for k in FACTS)
            print(f"{label:<12} {sample:<7} {marks}")
    for label, counts in kept.items():
        print(f"  {label:<12} {sum(counts)}/{len(counts) * len(FACTS)} facts kept")

    # -- 2. behaviour ------------------------------------------------------
    print("\n=== 2. given only the compacted history, what does it do next")
    print("   (redoing the patch on src/net.py means the summary failed at 'Done')")
    for label, summariser in (
        ("naive", make_naive_summariser(llm)),
        ("structured", make_summariser(llm)),
    ):
        for sample in range(args.samples):
            result = await compacted(history, summariser)
            _, calls = await continuation(tooled, result.history)
            redid = any("net.py" in c and "apply_patch" in c for c in calls)
            print(f"{label:<12} {sample:<7} redid={redid!s:<6} {calls[:2]}")

    # -- 3. decay ----------------------------------------------------------
    print("\n=== 3. summarising the summary, N times over")
    print("   each generation gets a fresh block of unrelated work to summarise,")
    print("   so the older material is only ever reachable through the summary")
    summariser = make_summariser(llm)
    current = history
    for generation in range(1, args.generations + 1):
        result = await compacted(current, summariser)
        hits = found(render_transcript(result.history.items))
        print(
            f"  gen {generation}  {sum(hits.values())}/5  "
            + " ".join(k.split()[0] for k, v in hits.items() if not v)
            + ("  (all kept)" if all(hits.values()) else "  <- lost")
        )
        # Fresh filler work, so the next compaction has something new to drop
        # and the old facts survive only via the summary it just wrote.
        current = result.history
        for i in range(4):
            call_id = f"gen{generation}_{i}"
            current.add_assistant("", [ToolCall(call_id, "run_shell", {}, '{"command": "ls"}')])
            current.add_tool_result(call_id, f"unrelated output {i}\n" + "x" * 600)

    print("\n--- structured prompt, first sample, verbatim ---")
    print(structured_summaries[0][:1400])
    print(f"\n(compaction prompt: {len(compaction_prompt())} chars)")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
```

> - `build_history()`：那段种了五个事实的会话。`NET_PY_REST` 是给"读文件"那一步的输出加的一大段无关代码，为了让会话有真实的大小（原因见下）。
> - `found(text)`：用正则表达式在文字里找五个事实，返回每个在不在。
> - `make_naive_summariser`：用那句朴素提示词的摘要器，拿来对照。
> - `continuation(model, history)`：只给模型压缩后的历史，看它下一步调用什么工具。
> - `main()` 做三个测量：**哪些事实还在**；**模型会不会重做已完成的工作**；**连续压缩好几代之后还剩多少**。

### 15.1 指标量错了对象，一次

第一版是在**摘要的文字**里找这五个事实。结果 A 和 B 稳定"丢失"。

但 A 和 B 在用户的第一条消息里，那条消息是**受保护的**，压缩之后原样还在。它们本来就不该出现在摘要里——出现了反而浪费 token。

> **指标量错了对象，会给出一个和真相相反的结论。** 改成在**压缩后的整个历史**里找。

### 15.2 探针自己过期了，又一次

改写这一章时重跑这个探针，得到的是：

```
  naive        15/15 facts kept
  structured   15/15 facts kept
```

两种提示词全部满分，而且打印出来的"结构化摘要原文"是空的。

**什么都没被压缩。** 原因是 §22 才会讲的一个保护：`plan()` 发现"压缩之后并不会变小"时就什么都不做。
这个保护是后来加的；探针里那段会话太短（一千多个字符），加了保护之后它根本不值得压缩。
于是探针忠实地报告"所有事实都在"——在一份**没被压缩过**的历史里，它们当然都在。

另外，朴素摘要器的函数签名还是 §14 之前的两个参数的旧样子，真被调用的话会直接出错——只是因为压缩没发生，才没人发现。

修了三处：给会话加了一段真实大小的文件内容；朴素摘要器改成接收 `SummaryRequest`；加了一个 `compacted()`，
**如果什么都没被压缩，就直接报错退出**，而不是打印一张关于"没发生的压缩"的表。

> 这一章已经是第三次遇到同一个形状了：流式请求不返回用量（§11）、测试测的是副本（§12.2）、探针量的是没发生的事。
> **一个报告"没问题"的工具，要先问它：你确定你测到东西了吗？**

### 15.3 结果

两次测量，都是真的，**而且它们对"差多少"的回答不一样**：

| | 朴素提示词 | 六小节提示词 |
|---|---|---|
| 2026-08-10，各 5 个样本（原来那段短会话） | 15/25 | 23/25 |
| 2026-10-01，各 10 个样本（加长后的会话） | 46/50 | 49/50 |

第二次的明细（节选）：

```
prompt       sample  A constraint      B do-not-touch    C discovered flag D done            E open
naive        0       yes               yes               NO                yes               yes
naive        1       yes               yes               yes               yes               yes
naive        2       yes               yes               NO                yes               yes
...
naive        5       yes               yes               NO                yes               yes
naive        6       yes               yes               NO                yes               yes
...
structured   6       yes               yes               NO                yes               yes
...
  naive        46/50 facts kept
  structured   49/50 facts kept
```

能说的和不能说的：

- **方向一致**：两次都是六小节的提示词保住得更多。
- **差距不稳定**：第一次是 15 比 23，第二次是 46 比 49。第一次里朴素提示词 5 次里 5 次丢掉了 E（"接下来干什么"），第二次一次都没丢。
  会话不同、日期不同、样本很少——**"结构化提示词把保留率从 60% 提高到 92%"这种话，这两组数据撑不起来。**
- **每次丢的都是同一个：C。** 那个"失败过一次才试出来"的选项。它是五个里最贵的（丢了要再失败一次），也是唯一一个两种提示词都保不住的：
  第二次测量里，朴素的丢了 4/10，结构化的丢了 1/10——而提示词的 `## Key data` 一节已经明确要求写下这类字符串了。

所以结论是：

> **摘要是有损的，而且丢什么带有随机性。** 任何"压缩之后信息都还在"的假设都是错的。
> 更好的提示词能减少损失，减少多少要看具体的会话，**减不到零**。

这直接决定了下一章（中断与恢复）往磁盘上写什么：**原文，而不是摘要。** 摘要只是"当前上下文"的替代品，不是历史的替代品。

**F06-04 成立。**

### 15.4 F06-05 没有复现

F06-05 猜的是：压缩之后模型"失忆"，把做完的事重做一遍。第二个测量就是测这个：只给模型压缩后的历史，看它下一步调用什么。
重新对 `src/net.py` 调用 `apply_patch` 就算重做。

```
=== 2. given only the compacted history, what does it do next
naive        0       redid=False  ['read_file({"path":"CHANGELOG.md"})']
naive        1       redid=False  ['read_file({"path":"src/net.py"})']
...
structured   3       redid=False  ['read_file({"path": "src/net.py"})', 'read_file({"path": "CHANGELOG.md"})']
structured   4       redid=False  ['read_file({"path":"src/net.py"})']
```

**20 次里 20 次都没有重做**（2026-10-01，两种提示词各 10 次；2026-08-10 的 10 次也一样）。
它们做的是先 `read_file`——**去看一眼**自己写完的东西，或者去看还没写的 CHANGELOG。这不是重做，是核对。

> **F06-05：没有复现。** 没有为它写任何防御代码。`## Done` 那一节留着，但它的依据是上面那张表，不是 F06-05。
>
> 第 3 章立的规矩：**没复现的故障不写代码。** 写了也没法验证它有没有用，只会变成以后没人敢删的一段。

```bash
git add src/minicodex/prompts/compaction.md src/minicodex/__init__.py src/minicodex/compaction.py tests/test_compaction.py probe_summary.py
git commit -m "feat(compaction): a six-section summary that is shown the goal it must agree with"
```

---

## §16 F06-08：摘要器自己挂了

做摘要要调用一次模型。这次调用会失败——超时、限流、连接断开。

而这个失败发生在**最坏的时刻**：压缩是被"装不下了"触发的，所以"稍后重试"这个选项不存在——下一个请求就是那个超限的请求。

真正的选择只有两个：**悄悄地硬删**，什么都不说；或者**硬删，并且告诉模型**。
选前者，模型会在一个缺了一大块的历史上继续，并且**完全不知道自己缺了东西**——它会假设之前的步骤都成功了，因为没有任何相反的信息。

```python
def _hard_summary(items: Sequence[HistoryItem], reason: str) -> str:
    """What to say when the summariser could not be reached (F06-08).

    Compaction is triggered by being out of room, so "try again later" is not
    available -- the next request is the one that does not fit.  The fallback
    is deterministic and local: state that context was lost, state how much,
    and say so *to the model*, because the alternative is an agent that quietly
    forgets and confidently proceeds.
    """
    kinds: dict[str, int] = {}
    for item in items:
        kinds[type(item).__name__] = kinds.get(type(item).__name__, 0) + 1
    breakdown = ", ".join(f"{count} {name}" for name, count in sorted(kinds.items()))
    return (
        "## Goal\n(lost)\n\n"
        "## Done\n(lost)\n\n"
        "## Decisions\n(lost)\n\n"
        "## Constraints\n(lost)\n\n"
        "## Open\n(lost)\n\n"
        "## Key data\n(lost)\n\n"
        f"**This summary could not be generated** ({reason}). "
        f"{len(items)} earlier message(s) were discarded unread ({breakdown}). "
        "Do not assume any earlier step succeeded. Before continuing, re-check "
        "the current state with a tool -- read the files you believe you wrote, "
        "re-run the command you believe passed -- and ask the user if the goal "
        "is no longer clear."
    )
```

> - 先数一数被丢掉的那一段里每种消息各有多少条（`type(item).__name__` 是对象所属的类的名字）。
> - 返回一段固定格式的文字：六个小节，每个写 `(lost)`；然后一段话说明原因、丢了多少、**接下来该做什么**。

真实的输出（让摘要器抛一个连接错误）：

```
[compacted transcript | generation 1 | 24 message(s) replaced | 2026-10-01 09:57]
## Goal
(lost)

## Done
(lost)

## Decisions
(lost)

## Constraints
(lost)

## Open
(lost)

## Key data
(lost)

**This summary could not be generated** (ConnectionError: [Errno 111] Connection refused). 24 earlier message(s) were discarded unread (12 AssistantMessage, 12 ToolResult). Do not assume any earlier step succeeded. Before continuing, re-check the current state with a tool -- read the files you believe you wrote, re-run the command you believe passed -- and ask the user if the goal is no longer clear.
```

几个故意的设计：

- **六个 `(lost)` 小节都留着。** 格式和成功时一样——读的人（模型）是通过**结构**知道"这里本该有东西"的。
- **"Do not assume any earlier step succeeded."** 后面跟着三件具体的事：读你以为写过的文件、重跑你以为通过的命令、目标不清楚就问用户。
  这是第 5 章的教训：让模型改变行为的不是"告诉它出事了"，而是**给它一件具体的事做**。
- **异常的类型和消息原样带上。** 对模型没什么用，对**看录像的人**有用。

还有一种失败不抛异常：摘要器返回了空字符串。**一个空摘要不是一份很短的摘要，而是一段被悄悄销毁的历史。** 它也算失败——处理它的代码在 §21 的 `compact()` 里。

```python
async def test_F06_08_a_failed_summariser_degrades_instead_of_raising():
    async def unreachable(request: SummaryRequest) -> str:
        raise ConnectionError("summariser unreachable")

    h = history_with(12, output="w" * 300)
    result = await compact(h, summarise=unreachable, budget=600)
    assert result.degraded
    result.history.to_wire()  # still a legal conversation


async def test_F06_08_the_model_is_told_that_context_was_lost():
    async def unreachable(request: SummaryRequest) -> str:
        raise TimeoutError("no response in 30s")

    h = history_with(12, output="w" * 300)
    result = await compact(h, summarise=unreachable, budget=600)
    note = result.history.items[2].text
    assert "could not be generated" in note
    assert "TimeoutError" in note
    assert "Do not assume any earlier step succeeded" in note
    assert "message(s) were discarded unread" in note


async def test_F06_08_an_empty_summary_counts_as_a_failure():
    """A summariser that returns "" is not a very short summary; it is a
    silently destroyed transcript."""

    async def says_nothing(request: SummaryRequest) -> str:
        return "   \n  "

    h = history_with(12, output="w" * 300)
    result = await compact(h, summarise=says_nothing, budget=600)
    assert result.degraded
```

> 摘要器抛异常时，压缩不抛异常，结果标记为 `degraded`（降级），历史仍然合法；那条替代摘要里有原因、有异常类型、有"不要假设前面成功了"；
> 只返回空白的摘要器，同样算降级。

---

## §17 F06-09：一条比整个窗口还大的输出

这条不能靠压缩解决，原因很具体：

> 压缩的手段是**丢掉旧的**。但装不下的那个东西是**最新一轮**里的一个 400KB 的错误堆栈，而最新一轮恰恰是最不能丢的。
> 把所有旧消息丢光，那一条还在。

所以需要另一个动作：**把单条内容截短。**

```python
def clip_item(item: HistoryItem, *, max_tokens: int = MAX_ITEM_TOKENS) -> HistoryItem:
    """Shrink a single tool result that is too big to keep whole (F06-09).

    Compaction cannot help here.  Dropping older turns does nothing when the
    thing that does not fit is one 400KB stack trace in the most recent turn,
    and it is precisely the most recent turn that must be kept.

    Head and tail, like chapter 2's `_clip`, and for the same measured reason:
    a traceback puts the exception at the top and the summary line at the
    bottom, and tail-only truncation deletes the half that says what broke.
    """
    if not isinstance(item, ToolResult):
        return item
    limit_chars = max_tokens * 4
    if len(item.content) <= limit_chars:
        return item
    head = limit_chars // 2
    tail = limit_chars - head
    omitted = len(item.content) - head - tail
    clipped = (
        item.content[:head] + f"\n... ({omitted} characters omitted by compaction; "
        "re-run a narrower command if you need the middle) ...\n" + item.content[-tail:]
    )
    return ToolResult(item.call_id, item.name, clipped)
```

> - 不是工具结果的，原样返回；没超过上限的，原样返回。
> - 超过了：**头和尾各留一半**，中间换成一句说明。理由和第 2 章截断 shell 输出时一样——错误堆栈的异常在最上面，总结在最下面，
>   只留尾巴等于删掉了说明原因的那一半。
> - 说明里有一句 `re-run a narrower command if you need the middle`：第 3 章的教训，**截断提示本身就是写给模型看的**，要说清下一步能做什么。

第 2 章已经在 shell 工具那里截过一次了，为什么还要一道？那一道在**工具里**，只管 shell。`read_file` 读一个大文件、
以后别的工具返回一大块 JSON，都不经过它。**这一道在历史这一层，管所有工具。**

```python
def test_F06_09_an_oversized_result_is_clipped_at_both_ends():
    body = "TOP: ValueError\n" + "middle\n" * 5000 + "BOTTOM: 3 failed\n"
    clipped = clip_item(ToolResult("c1", "run_shell", body), max_tokens=100)
    assert "TOP: ValueError" in clipped.content
    assert "BOTTOM: 3 failed" in clipped.content
    assert "characters omitted by compaction" in clipped.content
    assert len(clipped.content) < len(body) / 10


def test_F06_09_only_results_are_clipped():
    note = SystemNote("x" * 100_000)
    assert clip_item(note, max_tokens=10) is note


async def test_F06_09_a_single_huge_result_is_clipped_even_when_nothing_can_be_dropped():
    """Dropping older turns cannot help when the thing that does not fit is the
    most recent turn, and the most recent turn is the one that must be kept."""
    h = history_with(1, output="q" * 200_000)
    # A budget that comfortably holds a clipped result but not a raw one: the
    # only way to satisfy it is to shrink the item rather than drop the turn.
    result = await compact(h, summarise=constant_summary, budget=MAX_ITEM_TOKENS * 2)
    kept = [i for i in result.history.items if isinstance(i, ToolResult)]
    assert kept, "the most recent turn was dropped instead of being clipped"
    assert len(kept[0].content) <= MAX_ITEM_TOKENS * 4 + 200
    assert "characters omitted by compaction" in kept[0].content
```

> 头尾都还在、中间有省略说明、长度小了十倍以上；系统消息再长也不截（`is note`：返回的就是同一个对象）；
> 只有一轮、而这一轮的结果有 20 万字符时，这一轮没有被丢掉，而是被截短了。

---

## §18 F06-10：拒绝去猜自己不认识的东西

F06-10 猜的是：图片之类的内容，估算时完全没算。这个项目没有图片功能，所以最诚实的做法是什么？

先看不做任何事会怎样。带图片的消息，`content` 不是字符串，而是一个列表：

```python
{"role": "user", "content": [
    {"type": "text", "text": "what is this"},
    {"type": "image_url", "image_url": {"url": "data:image/png;base64,..."}},
]}
```

对一个两个元素的列表取 `len()`，是 **2**。除以 4，是 **0**。**一张图片被算成免费的。**

这比"没算"糟得多：预算不但触发得晚，还会**报告一切正常**。一个把最大的一项估成零的预算，比没有预算更危险。

所以 `tokens.py` 里的 `_content_chars` 遇到不是字符串的内容，直接抛 `UncountableContent`，消息里带着 `F06-10` 这个编号。

> **"不实现"有两种：悄悄返回一个很小的数，和大声说"我不知道"。** 前者是 bug，后者是缺口。
> 缺口会在第一次真有人传图片时，在本地、带着编号、指着这个类报错。
>
> 为什么不干脆实现？一张图片占多少 token 取决于它的尺寸和服务商的切块规则，而这个项目里没有任何东西可以拿来对着量。
> 照着文档写一个公式，就是在为没观察过的行为写代码。

```python
def test_F06_10_multimodal_content_is_refused_not_guessed():
    """`len()` of a two-part content list is 2, which over four is 0.

    An image counted as free is worse than an uncounted one: the budget fires
    late *and* reports that everything is fine.
    """
    messages = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "what is this"},
                {"type": "image_url", "image_url": {"url": "data:image/png;base64,..."}},
            ],
        }
    ]
    with pytest.raises(UncountableContent) as excinfo:
        estimate_messages(messages)
    assert "F06-10" in str(excinfo.value)


def test_F06_10_absent_content_is_zero_not_an_error():
    assert estimate_messages([{"role": "assistant", "content": None}]) >= 0
```

**这是全书第三次用同一个办法**：第 2 章的 F02-10（Windows 不支持）、第 5 章的 `workspace-write`（不做操作系统沙箱）、这里的图片。
**做不到的事，要说出自己的名字。**

---

## §19 F06-12：摘要自己也占地方

摘要替换掉了几十条消息，但它自己也占地方，而且比看起来贵：

> 摘要会在**这次会话剩下的每一轮**里被重新发送。它是唯一一项成本要乘以"后面所有轮数"的东西。

所以给它一个预算（`SUMMARY_TOKEN_BUDGET = 700`），并且这个预算必须在**选切点之前**就留出来。为什么不能事后检查？因为顺序不可逆：

> 先定切点、再发现摘要装不下——这时候**可以用来做另一个决定的那些消息，已经交给摘要模型并且扔掉了**。没有第二次机会。
> 预先留出来，是让"压缩完还是超、得再压一遍"变成**不需要**，而不只是**少见**。

### 19.1 两把尺子

摘要写出来之后要裁到预算以内。第一版是这样的：

```python
def _fit_summary(text: str, budget: int) -> str:
    limit = budget * 4
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "\n... (summary truncated to fit its budget)"
```

`budget * 4`——用"4 个字符一个 token"换算。**而这一章的前半部分就是在证明这个换算不可靠。**
更糟的是，摘要恰恰是最密集的那类内容：提示词明确要求它写路径、命令、报错原文、版本号。

问题的根子不是这一行算错了，而是：

> 选切点时用的是校准过的估计；裁摘要时用的是没校准的 `len // 4`。**留出来的位置，和填进去的东西，是用两把不同的尺子量的。**
> 错的不是其中一把，是**有两把**。

所以把"量大小"收成一个对象，它同时知道工具的 schema 和校准比例：

```python
@dataclass(frozen=True)
class Sizer:
    """One place that answers "how big is this", including the correction.

    It exists because the correction has to reach **both** decisions and the
    first version only reached one.  `plan()` used the calibrated number to
    pick a cut, and `_fit_summary()` used a bare `len(text) // 4` to trim the
    summary -- so the summary was budgeted with the estimator this whole
    chapter exists to distrust, and a summary made of paths and error strings
    (which is what the prompt asks for) overshoots its budget by the full
    measured 2.3x.  The bug is not that one call site was wrong; it is that
    there were two call sites at all.
    """

    tools: tuple[dict[str, Any], ...] = ()
    ratio: float = 1.0

    def messages(self, messages: Sequence[dict[str, Any]]) -> int:
        return int(estimate_messages(messages, self.tools) * self.ratio)

    def text(self, text: str) -> int:
        """Size a bare string -- a candidate summary, not yet a message.

        No tools and no per-message cost: this is asking what the string costs,
        not what a request containing it costs.
        """
        return int(len(text) / CHARS_PER_TOKEN * self.ratio)

    def clip_text(self, text: str, budget: int) -> str:
        if self.text(text) <= budget:
            return text
        keep = int(budget * CHARS_PER_TOKEN / self.ratio)
        return text[:keep].rstrip() + "\n... (summary truncated to fit its budget)"
```

> - `messages(messages)`：一次请求有多大（带工具 schema，乘上比例）。
> - `text(text)`：一段文字有多大（不带工具，也不算每条消息的开销——问的是这段字本身）。
> - `clip_text(text, budget)`：超了就从末尾裁。保留的字符数是 `budget × 4 ÷ 比例`——**比例越大（内容越密），留得越少**。
>   从末尾裁是故意的：提示词把小节按"下一轮多需要它"排了序。

> **第 5 章的那条新规则在这里又用了一次**："一条规则必须只在一个地方执行"。
> "所有的大小判断必须用同一把尺子"——不收进一个对象，就会有好几处各自维护，而且**漏掉的那一处不会报错**。

### 19.2 `plan()`：先做决定，再动手

所有决定都在一个不碰模型的纯函数里做完：

```python
@dataclass(frozen=True)
class Plan:
    """Where to cut, decided before anything is destroyed.

    Separate from doing it so it can be printed, asserted on and tested without
    a model: every decision in compaction is made here, and the execution below
    is mechanical.
    """

    protected: int
    cut: int
    kept_tokens: int
    fits: bool
    saving: int = 0
    """Tokens this plan expects to remove.  Zero means "do nothing", and that
    is a real outcome rather than a failure -- see `plan()`."""

    @property
    def drops(self) -> int:
        return self.cut - self.protected


def plan(
    items: Sequence[HistoryItem],
    *,
    budget: int,
    sizer: Sizer | None = None,
    summary_budget: int = SUMMARY_TOKEN_BUDGET,
    max_item_tokens: int = MAX_ITEM_TOKENS,
) -> Plan:
    """Choose the earliest legal cut whose remainder fits.

    Earliest, not latest: the goal is to destroy as little as possible.  The
    loop walks the legal boundaries in order and stops at the first one that
    fits, so the kept tail is the largest one that can be kept.

    `summary_budget` is reserved up front rather than checked afterwards.
    Deciding the cut and *then* discovering that the summary does not fit is
    F06-12 -- and by then the messages needed to make a different decision have
    been handed to a model and thrown away.  Reserving is what makes the second
    pass unnecessary rather than merely rare.

    **A plan that would not save anything cuts nothing.**  That guard was not
    designed in; a property test found it at seed 235, where compacting an
    89-token history produced a 93-token one.  Compaction is not free: it
    deletes some messages and inserts a summary, and when the deleted region is
    small the summary costs more than it replaced.  Left in, the failure is not
    one wasted call -- the estimate never drops below the trigger, so the next
    turn compacts again, and the session converges on a history made entirely
    of summaries of summaries.
    """
    sizer = sizer or Sizer()
    protected_count = Protected.of(items).count
    head = list(items[:protected_count])
    current = _size(head, items[protected_count:], sizer)
    candidates = [b for b in boundaries(items) if b >= protected_count]
    if not candidates:  # pragma: no cover -- len(items) is always a boundary
        raise CompactionError("no legal cut point at or after the protected prefix")

    def _do_nothing(fits: bool) -> Plan:
        return Plan(protected_count, protected_count, current, fits=fits, saving=0)

    for cut in candidates:
        tail = [clip_item(i, max_tokens=max_item_tokens) for i in items[cut:]]
        size = _size(head, tail, sizer) + summary_budget
        if size <= budget:
            if size >= current and cut > protected_count:
                return _do_nothing(fits=current <= budget)
            if cut == protected_count:
                # Nothing is dropped, so no summary is written and its
                # reservation is not spent.  Reporting `current + reservation`
                # here would be a number describing a request that never
                # happens.
                return _do_nothing(fits=True)
            return Plan(protected_count, cut, size, fits=True, saving=max(current - size, 0))

    # Nothing fits, including dropping everything droppable.  Report it rather
    # than raise: the caller still has to send something, and a plan that says
    # `fits=False` lets it decide what, with the numbers in hand.
    cut = candidates[-1]
    tail = [clip_item(i, max_tokens=max_item_tokens) for i in items[cut:]]
    size = _size(head, tail, sizer) + summary_budget
    if size >= current and cut > protected_count:
        return _do_nothing(fits=False)
    return Plan(protected_count, cut, size, fits=False, saving=max(current - size, 0))


def _size(head: Sequence[HistoryItem], tail: Sequence[HistoryItem], sizer: Sizer) -> int:
    scratch = History()
    _replay(scratch, list(head) + list(tail))
    return sizer.messages(scratch.to_wire())
```

> - **`Plan`**：一次压缩的计划——前缀有几条受保护、切在哪、切完估计多大、装不装得下、能省多少。
>   `drops` 是一个算出来的属性：被丢掉的条数。**把"决定"和"执行"分开**，决定就可以打印、可以断言、可以不用模型来测试。
> - **`plan()`**：
>   1. 算出受保护的前缀、当前的大小、所有"在前缀之后"的合法切点。
>   2. **从前往后**试每个切点（越靠前，丢掉的越少）：保留下来的尾巴先把过大的单条截短（§17），再量大小，**加上给摘要留的预算**。
>      第一个装得下的就用它。
>   3. 里面有两个"什么都不做"的出口（`_do_nothing`），其中"切了反而不会变小"那一个，是 §22 的故事。
>   4. 一个都装不下：不抛异常，返回一个 `fits=False` 的计划——调用的人**总得发点什么出去**，让它拿着数字自己决定。
> - **`_size(head, tail, sizer)`**：把"前缀 + 尾巴"重放进一个临时的 `History`，变成发送格式，再量。
>   量的是**真正会发出去的东西**，而不是对象列表的某种近似。

```python
async def test_F06_12_an_oversized_summary_is_trimmed():
    async def verbose(request: SummaryRequest) -> str:
        return "PATH /repo/src/thing.py\n" * 1000

    h = history_with(12, output="w" * 300)
    result = await compact(h, summarise=verbose, budget=600, summary_budget=100)
    assert result.plan.drops > 0, "nothing was dropped, so nothing was summarised"
    assert "truncated to fit its budget" in result.summary
    assert Sizer().text(result.summary) <= 120


async def test_F06_12_the_summary_budget_is_reserved_before_the_cut_is_chosen():
    """Choosing a cut and then discovering the summary does not fit is a
    decision that cannot be revisited: the messages are already gone."""
    items = history_with(40, output="w" * 300).items
    tight = plan(items, budget=3000, summary_budget=1500)
    loose = plan(items, budget=3000, summary_budget=0)
    assert tight.cut > loose.cut
    assert tight.drops > loose.drops


def test_F06_12_the_summary_is_measured_with_the_same_sizer_as_the_cut():
    """The bug this pins: `plan()` used the calibrated estimate and the summary
    trimmer used a bare `len(text) // 4`, so the reservation and the thing
    filling it were measured with different rulers."""
    dense = Sizer(ratio=2.0)
    text = "x" * 4000
    assert dense.text(text) == 2000
    assert Sizer().text(text) == 1000
    assert len(dense.clip_text(text, 100)) < len(Sizer().clip_text(text, 100))


def test_plan_reports_when_nothing_can_be_made_to_fit():
    """A budget smaller than the protected prefix is unsatisfiable, and saying
    so beats raising: the caller still has to send something."""
    items = history_with(4, output="u" * 200).items
    assert plan(items, budget=1).fits is False
```

> - 啰嗦的摘要会被裁短，并带上"truncated"的说明；
> - 给摘要留的预算越大，切点越靠后、丢掉的越多——证明预算是**先**留的；
> - **比例为 2 的尺子，量同一段文字得到两倍的大小，裁出来的也更短**。这是唯一能发现"两把尺子"那个 bug 的测试；
> - 预算小到连受保护的前缀都装不下时，`fits` 是 `False`，而不是抛异常。

---

## §20 F06-13：摘要的摘要的摘要

一次长会话会压缩不止一次。第二次压缩时，第一次的摘要就在历史里，会和别的消息一起被压掉——**摘要的摘要**。
F06-13 猜这会让信息一代代流失。

三个做法：

**一，开头的问题永远不参与。** 用户的第一条消息受保护，不会进入任何一代摘要。无论压多少代，"用户要什么"的来源始终是原文。

**二，上一代的摘要单独交给模型，并要求原样带过去。** 这就是 `SummaryRequest.established` 和提示词最后一条规则：
已经活过一次压缩的内容，"不要进一步压缩，也不要因为它看起来旧就丢掉"。模型做摘要时天然偏向保留最近的东西，
而"已经活过一次压缩的事实"恰恰最该保留——它还在，就是因为它一直有用。

**三，数出这是第几代。**

```python
def _previous_summary(items: Sequence[HistoryItem]) -> tuple[str | None, int]:
    """The surviving summary from an earlier compaction, and its generation.

    Generation is counted rather than inferred because it is the only visible
    handle on decay: a summary of a summary of a summary is still one system
    note, and nothing about its text says how many times it has been through
    this.
    """
    for item in reversed(list(items)):
        if isinstance(item, SystemNote) and item.text.startswith(SUMMARY_MARKER):
            header, _, body = item.text.partition("\n")
            # Anchored to the word before it, not "the digits in the header".
            # The loose version read the *last* number in
            # `[compacted transcript | generation 1 | 24 message(s) replaced |
            # 2026-08-10 06:22]` and returned 24, then 22 -- a generation
            # counter that climbed by whatever happened to be in the timestamp.
            # Found by a test that asserted the second compaction was
            # generation 2 and got 24.
            tokens = header.replace("]", " ").split()
            generation = 1
            for index, token in enumerate(tokens[:-1]):
                if token == "generation" and tokens[index + 1].isdigit():
                    generation = int(tokens[index + 1])
                    break
            return body.strip() or None, generation
    return None, 0
```

> - 从后往前找第一条以 `[compacted transcript` 开头的系统消息——那是上一次压缩留下的。
> - `partition("\n")` 把它切成"第一行（标题）"和"其余（摘要正文）"。
> - 从标题里读出代数；没找到摘要，就返回 `(None, 0)`。

### 20.1 数代数的那段代码，第一版是错的

第一版是这样读代数的：

```python
generation = 1
for token in header.replace("]", " ").split():
    if token.isdigit():
        generation = int(token)
```

"标题里的数字就是代数。" 而标题长这样：

```
[compacted transcript | generation 1 | 24 message(s) replaced | 2026-08-10 06:22]
```

里面的数字有 **1** 和 **24**（日期和时间因为带着 `-` 和 `:`，`isdigit()` 是 `False`）。循环留下的是**最后一个**：24。

代码里的注释记下了它是怎么被发现的：一个测试断言第二次压缩是第 2 代，实际得到的是 24。
**读代码时，"找标题里的数字"听起来完全合理；只有把标题的实际内容摆出来，它才明显是错的。**

现在的写法把它锚定在 `generation` 这个词上：找到这个词，取它后面那一个。

> 如果标题是结构化的数据（比如 JSON），这个 bug 不会存在。用给人读的字符串来装数据，就是在给以后的自己准备一个解析 bug。
> 这里仍然用字符串，是因为**模型要读它**——它得知道自己在看一份摘要——代价就是这段解析代码和它的测试。

### 20.2 量：六代之后还剩什么

`probe_summary.py` 的第三个测量：同样五个事实，连续压缩六代，每两代之间插入一批无关的新内容（这样旧事实只能靠摘要传下去）。

```
2026-08-10                     2026-10-01
  gen 1  4/5  C  <- lost         gen 1  5/5    (all kept)
  gen 2  4/5  C  <- lost         gen 2  5/5    (all kept)
  gen 3  4/5  C  <- lost         gen 3  5/5    (all kept)
  gen 4  4/5  C  <- lost         gen 4  5/5    (all kept)
  gen 5  4/5  C  <- lost         gen 5  5/5    (all kept)
  gen 6  4/5  C  <- lost         gen 6  5/5    (all kept)
```

两次的形状一样：

> **损失不是一代代往下滑，而是要么在第 1 代丢掉，要么就一直在。**

第一次，C 在第 1 代丢了，之后的五代再也没丢别的。第二次，C 在第 1 代活了下来，之后六代都在。
`<established>` 那一段提示词是直接原因：每一代看到的不是"上一代的原始对话"，而是"上一代已经筛过的结论，必须原样带走"。

> **F06-13：按猜测的样子（逐代衰减）没有出现。** 想减少损失，该改进的是**第一份**摘要的质量，而不是限制压缩的次数。
> 原来设想的解法"限制压缩代数"**没有实现**，因为测量不支持它。

```python
async def test_F06_13_generations_are_counted():
    h = history_with(12, output="w" * 300)
    first = await compact(h, summarise=constant_summary, budget=600)
    assert first.generation == 1

    grown = first.history
    for i in range(12):
        call_id = f"more_{i}"
        grown.add_assistant("", [ToolCall(call_id, "run_shell", {}, "{}")])
        grown.add_tool_result(call_id, "w" * 300)
    second = await compact(grown, summarise=constant_summary, budget=600)
    assert second.generation == 2


async def test_F06_13_the_previous_summary_is_carried_forward_not_re_summarised():
    """What stops decay compounding: generation N+1 is told that generation N's
    content has already survived one compaction and must be preserved."""
    seen: list[SummaryRequest] = []

    async def capture(request: SummaryRequest) -> str:
        seen.append(request)
        return "## Constraints\nmust stay on Python 3.9\n"

    h = history_with(12, output="w" * 300)
    first = await compact(h, summarise=capture, budget=600)
    assert seen[0].established is None
    assert seen[0].generation == 0

    grown = first.history
    for i in range(12):
        call_id = f"more_{i}"
        grown.add_assistant("", [ToolCall(call_id, "run_shell", {}, "{}")])
        grown.add_tool_result(call_id, "w" * 300)
    await compact(grown, summarise=capture, budget=600)
    assert seen[1].established is not None
    assert "Python 3.9" in seen[1].established
    assert seen[1].generation == 1


def test_F06_13_the_prompt_tells_the_model_established_facts_outrank_age():
    from minicodex import compaction_prompt

    text = compaction_prompt()
    assert "<established>" in text
    assert "Do not compress it further" in text
    assert "because it looks old" in text
```

> 第一次压缩是第 1 代，再压一次是第 2 代（就是当初得到 24 的那个测试）；第二次压缩时，摘要器在 `established` 里收到了上一代摘要的内容；
> 提示词里那几句话被钉住。

---

## §21 把它们接起来：`compact()`

```python
@dataclass(frozen=True)
class CompactionResult:
    history: History
    plan: Plan
    summary: str
    generation: int
    degraded: bool

    def describe(self) -> str:
        state = "degraded" if self.degraded else "summarised"
        return (
            f"compacted: dropped {self.plan.drops} item(s), {state}, "
            f"generation {self.generation}, ~{self.plan.kept_tokens} tokens"
        )


async def compact(
    history: History,
    *,
    summarise: Summariser,
    budget: int,
    sizer: Sizer | None = None,
    summary_budget: int = SUMMARY_TOKEN_BUDGET,
    max_item_tokens: int = MAX_ITEM_TOKENS,
) -> CompactionResult:
    """Replace the middle of the conversation with a summary of it.

    The order matters and is not the obvious one: **plan, then summarise, then
    rebuild.**  Summarising first and deciding the cut afterwards means the
    summary describes a region that may not be the one removed.
    """
    sizer = sizer or Sizer()
    items = history.items
    the_plan = plan(
        items,
        budget=budget,
        sizer=sizer,
        summary_budget=summary_budget,
        max_item_tokens=max_item_tokens,
    )
    dropped = items[the_plan.protected : the_plan.cut]
    previous, generation = _previous_summary(items)

    if not dropped:
        # Nothing may be removed, but an oversized single item may still be
        # clipped -- and if it cannot, the caller needs to know that no amount
        # of compaction will help.
        rebuilt = History()
        _replay(
            rebuilt,
            list(items[: the_plan.cut])
            + [clip_item(i, max_tokens=max_item_tokens) for i in items[the_plan.cut :]],
        )
        return CompactionResult(rebuilt, the_plan, "", generation, degraded=False)

    try:
        summary = await summarise(
            SummaryRequest(
                transcript=render_transcript(dropped),
                context=render_transcript(items[: the_plan.protected]),
                established=previous,
                generation=generation,
            )
        )
        degraded = False
        if not summary.strip():
            raise CompactionError("summariser returned an empty summary")
    except Exception as exc:
        summary = _hard_summary(dropped, f"{type(exc).__name__}: {exc}")
        degraded = True

    # The summary is trimmed with the *same* sizer that chose the cut (F06-12).
    # A model told to write six sections will sometimes write six long ones, and
    # a summary over its reservation puts the history straight back where it
    # started -- except that now the messages it was made from are gone, so
    # there is nothing left to compact.  Trimming from the end is deliberate:
    # the prompt orders the sections by how much the next turn needs them.
    summary = sizer.clip_text(summary, summary_budget)
    generation += 1
    note = (
        f"{SUMMARY_MARKER} | generation {generation} | "
        f"{len(dropped)} message(s) replaced | {time.strftime('%Y-%m-%d %H:%M')}]\n"
        f"{summary}"
    )

    rebuilt = History()
    _replay(rebuilt, items[: the_plan.protected])
    rebuilt.add_system_note(note)
    _replay(rebuilt, [clip_item(i, max_tokens=max_item_tokens) for i in items[the_plan.cut :]])
    return CompactionResult(rebuilt, the_plan, summary, generation, degraded)
```

> - **`CompactionResult`**：新的历史、当时的计划、摘要、第几代、是不是降级的。`describe()` 给人看一句话。
> - **`compact()` 的顺序不是最显然的那个：先计划，再摘要，最后重建。** 先摘要、后决定切点的话，摘要描述的可能不是真正被删掉的那一段。
> - `dropped = items[protected : cut]`：真正要删的那一段。
> - **没有东西可删**（`if not dropped`）：仍然把过大的单条截短，重建一个历史返回，摘要为空。
> - **有东西可删**：调用摘要器。**空摘要在 `try` 里面主动 `raise`**，再被下面的 `except` 接住——看起来绕，但这样"降级摘要"只在一个地方构造，
>   `degraded = True` 只在一个地方设置。
> - 摘要用**同一把尺子**裁到预算内（§19.1）。
> - 拼出那条系统消息：标题一行（标记、第几代、替换了多少条、时间），然后是摘要。
> - 重建：前缀重放 → 加上摘要 → 尾巴（截短之后）重放。全部走 `History` 的 `add_*`（§8）。

`compaction.py` 到这里写完了。整个文件：

```python
"""Making a long conversation shorter without making it invalid.

The naive versions all have the same bug, and it is not the one the fault list
predicted.  Measured against gpt-4o-mini on 2026-08-10 (`probe_cut_points.py`),
cutting one eight-message history at every possible index:

        cut  kept                                          status
          0  S U A[a1] T[a1] A[b2] T[b2] A[c3] T[c3]       200
          1    U A[a1] T[a1] A[b2] T[b2] A[c3] T[c3]       200
          2      A[a1] T[a1] A[b2] T[b2] A[c3] T[c3]       200
          3            T[a1] A[b2] T[b2] A[c3] T[c3]       400
          4                  A[b2] T[b2] A[c3] T[c3]       200
          5                        T[b2] A[c3] T[c3]       400
          6                              A[c3] T[c3]       200
          7                                    T[c3]       400

The 400s say `messages with role 'tool' must be a response to a preceeding
message with 'tool_calls'`.  They are the easy half: loud, immediate, and
impossible to ship.

The dangerous half is the 200 column.  The user asked *which Python version
this project supports*.  Cut 0 answers it: "the project supports Python 3.10
and above".  Cut 6 is equally valid, costs a third of the tokens, and answers a
different question -- "you are using Python version 3.13.0" -- because the
message saying what was asked is no longer there.  Same history, same model, no
error, fluent wrong answer.

So compaction has two jobs.  Keep the conversation renderable, and keep the
parts that say what it is for.  This module is `boundaries()` for the first and
`Protected` for the second; everything else is about the summary that replaces
what was cut.
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Any

from minicodex import compaction_prompt
from minicodex.history import (
    AssistantMessage,
    History,
    HistoryItem,
    SystemNote,
    ToolResult,
    UserMessage,
)
from minicodex.tokens import CHARS_PER_TOKEN, estimate_messages

# Prefix on the system note that carries a summary.  A marker rather than a
# separate item type: the model must read it as an instruction-shaped note like
# any other, and a new `HistoryItem` variant would need a rendering rule in
# every dialect for no gain.  It is parsed back out only to count generations.
SUMMARY_MARKER = "[compacted transcript"

# How much of the budget the summary itself may occupy.  Not a round number for
# its own sake: the summary is re-sent on every subsequent turn for the rest of
# the session, so it is the one item whose cost is multiplied by everything
# that follows it.
SUMMARY_TOKEN_BUDGET = 700

# A single tool result larger than this is clipped before it is allowed into
# the kept tail (F06-09).  Chapter 2 already clips shell output at the tool;
# this is the second line, for everything that is not the shell.
MAX_ITEM_TOKENS = 2000


class CompactionError(RuntimeError):
    """Compaction could not produce a history that fits."""


@dataclass(frozen=True)
class SummaryRequest:
    """Everything the summariser is allowed to see.

    Three fields rather than one transcript string, and the third one was
    bought with a wrong answer.  The first version passed only the region being
    destroyed, which is exactly the region that does **not** contain the
    protected prefix -- so the model was asked to write a `## Goal` section
    while the message stating the goal was deliberately withheld from it.  It
    did what a model does with a required heading and no evidence: it inferred
    one.  Measured, 3/3 on gpt-4o-mini, the summary of a session about *adding
    retry logic to src/net.py* opened with

        ## Goal
        Record the change in CHANGELOG.md.

    which is not the goal, it is the last remaining task.  That note then sits
    in the history one line below the real user message, contradicting it, with
    the authority of a system note.

    `context` fixes it by handing over the protected prefix as read-only
    material.  It is not summarised and not replaced -- it is still in the
    rebuilt history verbatim -- it is there so the summary can be *consistent*
    with it.
    """

    transcript: str
    """The region about to be destroyed.  The only part being replaced."""

    context: str
    """The protected prefix, verbatim.  Survives on its own; shown for
    agreement, not for compression."""

    established: str | None
    """The previous generation's summary, if this is not the first compaction."""

    generation: int
    """How many compactions this session has already survived."""


Summariser = Callable[["SummaryRequest"], Awaitable[str]]


# ---------------------------------------------------------------------------
# where a cut is allowed
# ---------------------------------------------------------------------------


def boundaries(items: Sequence[HistoryItem]) -> tuple[int, ...]:
    """Every index at which the conversation may be cut.

    A cut at index `i` keeps `items[i:]`.  It is legal exactly when no call
    issued before `i` is answered after it -- a statement about the shape of
    the list, not about how many messages or tokens sit on either side.  Both
    naive rules ("drop the oldest half", "keep the last N") are right only by
    luck, and the luck is parity: on the eight-message history above, half is
    index 4 and legal; on a nine-message one it is index 4 or 5, and one of
    those is a 400.

    Verified against the server rather than against the docs: this function's
    output for that history is `(0, 1, 2, 4, 6, 8)`, and indices 0-7 are
    exactly the ones the API answered 200 to.
    """
    open_calls = 0
    result = []
    for index, item in enumerate(items):
        if open_calls == 0:
            result.append(index)
        if isinstance(item, AssistantMessage):
            open_calls += len(item.tool_calls)
        elif isinstance(item, ToolResult):
            open_calls -= 1
    if open_calls == 0:
        result.append(len(items))
    return tuple(result)


@dataclass(frozen=True)
class Protected:
    """The prefix compaction is not allowed to touch.

    Two things, for two different reasons:

    * the **system notes at the front** -- the instructions and the permission
      block.  An agent that forgets those does not degrade, it becomes a
      different agent, and chapter 5 measured what one does when it no longer
      knows its own permissions;
    * the **first user message** -- the only record of what was asked.  Cut 6
      in the table above is what its absence looks like, and it looks like
      success.

    Later system notes are deliberately *not* protected.  Chapter 0's
    turn-budget warning is a `SystemNote` too, and it is worth exactly one
    turn; protecting by type rather than by position would accumulate every
    stale warning forever.
    """

    count: int

    @staticmethod
    def of(items: Sequence[HistoryItem]) -> Protected:
        index = 0
        while index < len(items) and isinstance(items[index], SystemNote):
            index += 1
        if index < len(items) and isinstance(items[index], UserMessage):
            index += 1
        return Protected(index)


# ---------------------------------------------------------------------------
# rendering the part that is about to be destroyed
# ---------------------------------------------------------------------------


def render_transcript(items: Sequence[HistoryItem]) -> str:
    """The dropped region, as text for the summariser.

    Not `to_wire()`: this is going into a prompt as *content*, and the wire
    format's nesting would spend tokens on JSON punctuation the summariser does
    not need.  Tool results keep their call's name, because "the output of
    `read_file`" and "the output of `run_shell`" mean different things to
    whoever reads this next.
    """
    lines: list[str] = []
    for item in items:
        if isinstance(item, UserMessage):
            lines.append(f"USER: {item.text}")
        elif isinstance(item, SystemNote):
            lines.append(f"SYSTEM: {item.text}")
        elif isinstance(item, AssistantMessage):
            if item.text:
                lines.append(f"ASSISTANT: {item.text}")
            for call in item.tool_calls:
                lines.append(f"ASSISTANT CALLS {call.name}({call.raw_arguments})")
        elif isinstance(item, ToolResult):
            lines.append(f"RESULT OF {item.name}:\n{item.content}")
    return "\n".join(lines)


def _previous_summary(items: Sequence[HistoryItem]) -> tuple[str | None, int]:
    """The surviving summary from an earlier compaction, and its generation.

    Generation is counted rather than inferred because it is the only visible
    handle on decay: a summary of a summary of a summary is still one system
    note, and nothing about its text says how many times it has been through
    this.
    """
    for item in reversed(list(items)):
        if isinstance(item, SystemNote) and item.text.startswith(SUMMARY_MARKER):
            header, _, body = item.text.partition("\n")
            # Anchored to the word before it, not "the digits in the header".
            # The loose version read the *last* number in
            # `[compacted transcript | generation 1 | 24 message(s) replaced |
            # 2026-08-10 06:22]` and returned 24, then 22 -- a generation
            # counter that climbed by whatever happened to be in the timestamp.
            # Found by a test that asserted the second compaction was
            # generation 2 and got 24.
            tokens = header.replace("]", " ").split()
            generation = 1
            for index, token in enumerate(tokens[:-1]):
                if token == "generation" and tokens[index + 1].isdigit():
                    generation = int(tokens[index + 1])
                    break
            return body.strip() or None, generation
    return None, 0


# ---------------------------------------------------------------------------
# clipping one oversized item
# ---------------------------------------------------------------------------


def clip_item(item: HistoryItem, *, max_tokens: int = MAX_ITEM_TOKENS) -> HistoryItem:
    """Shrink a single tool result that is too big to keep whole (F06-09).

    Compaction cannot help here.  Dropping older turns does nothing when the
    thing that does not fit is one 400KB stack trace in the most recent turn,
    and it is precisely the most recent turn that must be kept.

    Head and tail, like chapter 2's `_clip`, and for the same measured reason:
    a traceback puts the exception at the top and the summary line at the
    bottom, and tail-only truncation deletes the half that says what broke.
    """
    if not isinstance(item, ToolResult):
        return item
    limit_chars = max_tokens * 4
    if len(item.content) <= limit_chars:
        return item
    head = limit_chars // 2
    tail = limit_chars - head
    omitted = len(item.content) - head - tail
    clipped = (
        item.content[:head] + f"\n... ({omitted} characters omitted by compaction; "
        "re-run a narrower command if you need the middle) ...\n" + item.content[-tail:]
    )
    return ToolResult(item.call_id, item.name, clipped)


# ---------------------------------------------------------------------------
# the plan
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Sizer:
    """One place that answers "how big is this", including the correction.

    It exists because the correction has to reach **both** decisions and the
    first version only reached one.  `plan()` used the calibrated number to
    pick a cut, and `_fit_summary()` used a bare `len(text) // 4` to trim the
    summary -- so the summary was budgeted with the estimator this whole
    chapter exists to distrust, and a summary made of paths and error strings
    (which is what the prompt asks for) overshoots its budget by the full
    measured 2.3x.  The bug is not that one call site was wrong; it is that
    there were two call sites at all.
    """

    tools: tuple[dict[str, Any], ...] = ()
    ratio: float = 1.0

    def messages(self, messages: Sequence[dict[str, Any]]) -> int:
        return int(estimate_messages(messages, self.tools) * self.ratio)

    def text(self, text: str) -> int:
        """Size a bare string -- a candidate summary, not yet a message.

        No tools and no per-message cost: this is asking what the string costs,
        not what a request containing it costs.
        """
        return int(len(text) / CHARS_PER_TOKEN * self.ratio)

    def clip_text(self, text: str, budget: int) -> str:
        if self.text(text) <= budget:
            return text
        keep = int(budget * CHARS_PER_TOKEN / self.ratio)
        return text[:keep].rstrip() + "\n... (summary truncated to fit its budget)"


@dataclass(frozen=True)
class Plan:
    """Where to cut, decided before anything is destroyed.

    Separate from doing it so it can be printed, asserted on and tested without
    a model: every decision in compaction is made here, and the execution below
    is mechanical.
    """

    protected: int
    cut: int
    kept_tokens: int
    fits: bool
    saving: int = 0
    """Tokens this plan expects to remove.  Zero means "do nothing", and that
    is a real outcome rather than a failure -- see `plan()`."""

    @property
    def drops(self) -> int:
        return self.cut - self.protected


def plan(
    items: Sequence[HistoryItem],
    *,
    budget: int,
    sizer: Sizer | None = None,
    summary_budget: int = SUMMARY_TOKEN_BUDGET,
    max_item_tokens: int = MAX_ITEM_TOKENS,
) -> Plan:
    """Choose the earliest legal cut whose remainder fits.

    Earliest, not latest: the goal is to destroy as little as possible.  The
    loop walks the legal boundaries in order and stops at the first one that
    fits, so the kept tail is the largest one that can be kept.

    `summary_budget` is reserved up front rather than checked afterwards.
    Deciding the cut and *then* discovering that the summary does not fit is
    F06-12 -- and by then the messages needed to make a different decision have
    been handed to a model and thrown away.  Reserving is what makes the second
    pass unnecessary rather than merely rare.

    **A plan that would not save anything cuts nothing.**  That guard was not
    designed in; a property test found it at seed 235, where compacting an
    89-token history produced a 93-token one.  Compaction is not free: it
    deletes some messages and inserts a summary, and when the deleted region is
    small the summary costs more than it replaced.  Left in, the failure is not
    one wasted call -- the estimate never drops below the trigger, so the next
    turn compacts again, and the session converges on a history made entirely
    of summaries of summaries.
    """
    sizer = sizer or Sizer()
    protected_count = Protected.of(items).count
    head = list(items[:protected_count])
    current = _size(head, items[protected_count:], sizer)
    candidates = [b for b in boundaries(items) if b >= protected_count]
    if not candidates:  # pragma: no cover -- len(items) is always a boundary
        raise CompactionError("no legal cut point at or after the protected prefix")

    def _do_nothing(fits: bool) -> Plan:
        return Plan(protected_count, protected_count, current, fits=fits, saving=0)

    for cut in candidates:
        tail = [clip_item(i, max_tokens=max_item_tokens) for i in items[cut:]]
        size = _size(head, tail, sizer) + summary_budget
        if size <= budget:
            if size >= current and cut > protected_count:
                return _do_nothing(fits=current <= budget)
            if cut == protected_count:
                # Nothing is dropped, so no summary is written and its
                # reservation is not spent.  Reporting `current + reservation`
                # here would be a number describing a request that never
                # happens.
                return _do_nothing(fits=True)
            return Plan(protected_count, cut, size, fits=True, saving=max(current - size, 0))

    # Nothing fits, including dropping everything droppable.  Report it rather
    # than raise: the caller still has to send something, and a plan that says
    # `fits=False` lets it decide what, with the numbers in hand.
    cut = candidates[-1]
    tail = [clip_item(i, max_tokens=max_item_tokens) for i in items[cut:]]
    size = _size(head, tail, sizer) + summary_budget
    if size >= current and cut > protected_count:
        return _do_nothing(fits=False)
    return Plan(protected_count, cut, size, fits=False, saving=max(current - size, 0))


def _size(head: Sequence[HistoryItem], tail: Sequence[HistoryItem], sizer: Sizer) -> int:
    scratch = History()
    _replay(scratch, list(head) + list(tail))
    return sizer.messages(scratch.to_wire())


# ---------------------------------------------------------------------------
# doing it
# ---------------------------------------------------------------------------


def _replay(history: History, items: Sequence[HistoryItem]) -> None:
    """Rebuild a history by putting every item back through the front door.

    This is the whole reason chapter 1 enforced its invariant on append rather
    than checking it on send.  A compaction that produces an orphaned result
    does not travel to a provider and come back as a 400 with a spelling
    mistake in it; it raises `HistoryError` here, in this process, naming the
    call id, on the line that planned the cut.
    """
    for item in items:
        if isinstance(item, UserMessage):
            history.add_user(item.text)
        elif isinstance(item, SystemNote):
            history.add_system_note(item.text)
        elif isinstance(item, AssistantMessage):
            history.add_assistant(item.text, item.tool_calls)
        elif isinstance(item, ToolResult):
            history.add_tool_result(item.call_id, item.content)
        else:  # pragma: no cover
            raise AssertionError(f"unreplayable item: {item!r}")


def _hard_summary(items: Sequence[HistoryItem], reason: str) -> str:
    """What to say when the summariser could not be reached (F06-08).

    Compaction is triggered by being out of room, so "try again later" is not
    available -- the next request is the one that does not fit.  The fallback
    is deterministic and local: state that context was lost, state how much,
    and say so *to the model*, because the alternative is an agent that quietly
    forgets and confidently proceeds.
    """
    kinds: dict[str, int] = {}
    for item in items:
        kinds[type(item).__name__] = kinds.get(type(item).__name__, 0) + 1
    breakdown = ", ".join(f"{count} {name}" for name, count in sorted(kinds.items()))
    return (
        "## Goal\n(lost)\n\n"
        "## Done\n(lost)\n\n"
        "## Decisions\n(lost)\n\n"
        "## Constraints\n(lost)\n\n"
        "## Open\n(lost)\n\n"
        "## Key data\n(lost)\n\n"
        f"**This summary could not be generated** ({reason}). "
        f"{len(items)} earlier message(s) were discarded unread ({breakdown}). "
        "Do not assume any earlier step succeeded. Before continuing, re-check "
        "the current state with a tool -- read the files you believe you wrote, "
        "re-run the command you believe passed -- and ask the user if the goal "
        "is no longer clear."
    )


@dataclass(frozen=True)
class CompactionResult:
    history: History
    plan: Plan
    summary: str
    generation: int
    degraded: bool

    def describe(self) -> str:
        state = "degraded" if self.degraded else "summarised"
        return (
            f"compacted: dropped {self.plan.drops} item(s), {state}, "
            f"generation {self.generation}, ~{self.plan.kept_tokens} tokens"
        )


async def compact(
    history: History,
    *,
    summarise: Summariser,
    budget: int,
    sizer: Sizer | None = None,
    summary_budget: int = SUMMARY_TOKEN_BUDGET,
    max_item_tokens: int = MAX_ITEM_TOKENS,
) -> CompactionResult:
    """Replace the middle of the conversation with a summary of it.

    The order matters and is not the obvious one: **plan, then summarise, then
    rebuild.**  Summarising first and deciding the cut afterwards means the
    summary describes a region that may not be the one removed.
    """
    sizer = sizer or Sizer()
    items = history.items
    the_plan = plan(
        items,
        budget=budget,
        sizer=sizer,
        summary_budget=summary_budget,
        max_item_tokens=max_item_tokens,
    )
    dropped = items[the_plan.protected : the_plan.cut]
    previous, generation = _previous_summary(items)

    if not dropped:
        # Nothing may be removed, but an oversized single item may still be
        # clipped -- and if it cannot, the caller needs to know that no amount
        # of compaction will help.
        rebuilt = History()
        _replay(
            rebuilt,
            list(items[: the_plan.cut])
            + [clip_item(i, max_tokens=max_item_tokens) for i in items[the_plan.cut :]],
        )
        return CompactionResult(rebuilt, the_plan, "", generation, degraded=False)

    try:
        summary = await summarise(
            SummaryRequest(
                transcript=render_transcript(dropped),
                context=render_transcript(items[: the_plan.protected]),
                established=previous,
                generation=generation,
            )
        )
        degraded = False
        if not summary.strip():
            raise CompactionError("summariser returned an empty summary")
    except Exception as exc:
        summary = _hard_summary(dropped, f"{type(exc).__name__}: {exc}")
        degraded = True

    # The summary is trimmed with the *same* sizer that chose the cut (F06-12).
    # A model told to write six sections will sometimes write six long ones, and
    # a summary over its reservation puts the history straight back where it
    # started -- except that now the messages it was made from are gone, so
    # there is nothing left to compact.  Trimming from the end is deliberate:
    # the prompt orders the sections by how much the next turn needs them.
    summary = sizer.clip_text(summary, summary_budget)
    generation += 1
    note = (
        f"{SUMMARY_MARKER} | generation {generation} | "
        f"{len(dropped)} message(s) replaced | {time.strftime('%Y-%m-%d %H:%M')}]\n"
        f"{summary}"
    )

    rebuilt = History()
    _replay(rebuilt, items[: the_plan.protected])
    rebuilt.add_system_note(note)
    _replay(rebuilt, [clip_item(i, max_tokens=max_item_tokens) for i in items[the_plan.cut :]])
    return CompactionResult(rebuilt, the_plan, summary, generation, degraded)


# ---------------------------------------------------------------------------
# the summariser that actually calls a model
# ---------------------------------------------------------------------------


def make_summariser(model: Any, *, dialect: str = "chat_completions") -> Summariser:
    """A `Summariser` backed by one non-agentic model call.

    Its own `History`, not the session's: the summariser must not see the tool
    schemas, must not be able to call anything, and must not have its output
    land in the conversation it is summarising.  Chapter 10 makes this shape
    general; here it is one function.
    """

    async def summarise(request: SummaryRequest) -> str:
        scratch = History()
        scratch.add_system_note(compaction_prompt())
        blocks = [f"<context>\n{request.context}\n</context>"]
        if request.established:
            blocks.append(f"<established>\n{request.established}\n</established>")
        blocks.append(f"<transcript>\n{request.transcript}\n</transcript>")
        scratch.add_user("\n\n".join(blocks))

        parts: list[str] = []
        saw_end = False
        from minicodex.model import Completed, TextDelta

        async for event in model.stream(scratch.to_wire(dialect)):
            if isinstance(event, TextDelta):
                parts.append(event.text)
            elif isinstance(event, Completed):
                saw_end = True
        if not saw_end:
            # The same rule as chapter 0's loop: a stream that stopped early is
            # not a short summary, it is an unknown one.  Half a summary that
            # replaces a whole transcript is worse than admitting the loss.
            raise CompactionError("summariser stream ended without a [DONE] sentinel")
        return "".join(parts)

    return summarise


def unused_call_ids(items: Sequence[HistoryItem]) -> tuple[str, ...]:
    """Diagnostic: results whose call is not in the same list.

    Nothing in this module calls it -- `_replay` makes the state unreachable.
    It exists so the tests can point at a naive slice and name what is wrong
    with it, in the same vocabulary the server uses.
    """
    issued = {
        call.call_id
        for item in items
        if isinstance(item, AssistantMessage)
        for call in item.tool_calls
    }
    return tuple(i.call_id for i in items if isinstance(i, ToolResult) and i.call_id not in issued)


__all__ = [
    "MAX_ITEM_TOKENS",
    "SUMMARY_MARKER",
    "SUMMARY_TOKEN_BUDGET",
    "CompactionError",
    "CompactionResult",
    "Plan",
    "Protected",
    "boundaries",
    "clip_item",
    "compact",
    "make_summariser",
    "plan",
    "render_transcript",
]
```

> 前面没单独讲过的部分：
>
> - **开头的 docstring**：§4、§5 的实测表和"两个任务"。
> - **三个常量**：`SUMMARY_MARKER`（摘要那条系统消息的开头标记——用标记而不是新造一种消息类型，因为模型应该像读别的系统消息一样读它）、
>   `SUMMARY_TOKEN_BUDGET`、`MAX_ITEM_TOKENS`。
> - **`CompactionError`**：摘要器内部用的异常。
> - **`unused_call_ids(items)`**：诊断用。先收集这段里所有"发起过的调用"的 id（花括号里的写法是"集合推导"），
>   再找出那些"调用不在这段里"的结果。模块自己从不调用它——`_replay` 让那种状态根本出现不了。它存在，是为了让测试能用和服务端一样的说法，指出一个朴素切片错在哪。
> - **`__all__`**：这个模块对外提供的名字。

还剩几个测试：

```python
async def test_F06_06_user_question_survives_compaction():
    h = history_with(12, output="x" * 300)
    result = await compact(h, summarise=constant_summary, budget=600)
    assert isinstance(result.history.items[0], SystemNote)
    assert isinstance(result.history.items[1], UserMessage)
    assert "Which Python version" in result.history.items[1].text


async def test_F06_06_compaction_never_produces_an_invalid_history():
    for pairs in range(1, 14):
        h = history_with(pairs, output="y" * 200)
        result = await compact(h, summarise=constant_summary, budget=500)
        assert unused_call_ids(result.history.items) == ()
        result.history.to_wire()  # raises if a call went unanswered


async def test_compaction_shrinks_the_thing_it_was_called_about():
    h = history_with(20, output="v" * 400)
    before = Sizer().messages(h.to_wire())
    result = await compact(h, summarise=constant_summary, budget=1500)
    after = Sizer().messages(result.history.to_wire())
    assert after < before / 2
    assert result.plan.fits
```

> 压缩之后，前两条仍然是系统消息和用户的问题；从 1 对到 13 对"调用 / 结果"的历史，压缩之后都没有没主人的结果，都能变成发送格式；
> 一个大历史压缩后，大小不到原来的一半。

```bash
git add src/minicodex/compaction.py tests/test_compaction.py
git commit -m "feat(compaction): plan the cut, summarise, rebuild -- and say so when the summary fails"
```

---

## §22 接进循环

### 22.1 F06-11：只在两轮之间压缩

F06-11 猜的是：压缩正好发生在一次流式输出的中间。这条的解法不是加一个标志位，而是**把调用放对位置**。

`agent.py` 的改动。开头的 import 加上：

```python
from minicodex.compaction import CompactionResult, Sizer, Summariser, compact
from minicodex.model import Completed, StreamEvent, TextDelta, ToolCallDelta, Usage
from minicodex.tokens import Calibration
```

`ModelTurn` 多一个字段 `usage: Usage | None = None`，`RunResult` 多一个字段 `compactions: tuple[CompactionResult, ...] = ()`。
两个新常量：

```python
# Compact when the estimate crosses this fraction of the window, down to that
# one.  Two numbers rather than one, because compacting *to* the trigger point
# means compacting again next turn: the gap is what buys the turns in between.
COMPACT_AT = 0.75
COMPACT_TO = 0.45
```

> **两个数，不是一个。** 估计值超过窗口的 75% 时触发，压到 45% 为止。如果压到的目标就是触发线，**下一轮马上又会触发**——
> 每轮多一次模型调用、每轮丢一点信息、每轮多一代摘要。中间那段差距，买到的是"接下来能安静干几轮活"。
> 这两个数没有任何理论依据，是定的；`README` 的"故意没做"里写着"没有对着任何东西调过"。

`Agent.__init__` 多两个参数 `context_window: int | None = None` 和 `summariser: Summariser | None = None`，并保存：

```python
        # None means "never compact", which is what chapters 0-5 did.  Kept as
        # the default so that every test written before this chapter still
        # describes the behaviour it was written for; a run that wants
        # compaction says how big its window is, because nothing else in this
        # program can find that out.
        self.context_window = context_window
        self.summariser = summariser
        self.calibration = Calibration()
```

> `context_window` 默认是 `None`，意思是"从不压缩"——也就是前面所有章的行为。所以之前写的测试一个都不用改。
> 想要压缩，调用的人必须**自己说窗口有多大**：这个程序没有任何办法自己发现这个数，猜一个默认值，就是给用户一个悄悄出错的预算。

`_collect` 里多认一种事件：遇到 `Usage` 就记下来，最后放进 `ModelTurn`（`elif isinstance(event, Usage): usage = event`）。

然后是三个新方法：

```python
    def _sizer(self) -> Sizer:
        tools = tuple(getattr(self.model, "tools", ()) or ())
        return Sizer(tools=tools, ratio=self.calibration.ratio)

    def _raw_estimate(self, messages: Sequence[dict[str, Any]]) -> int:
        """The estimate before any correction: the number `Calibration` is fed.

        Not `_sizer().messages()`.  That one is already multiplied by the
        current ratio, and the ratio of the truth to an already-corrected guess
        is not the correction, it is the correction's *error*.  Feeding it back
        made the ratio swing between the real factor and 1.0 on alternate
        turns: against a server that always charged 1.5x the raw estimate it
        read 1.50, 1.00, 1.50, 1.00.  Nothing failed.  The estimate was simply
        uncorrected every other turn.
        """
        return Sizer(tools=self._sizer().tools).messages(messages)

    async def _maybe_compact(self, history: History) -> tuple[History, CompactionResult | None]:
        """Shrink the conversation if the next request would not fit.

        Called from exactly one place: the top of the loop, before the request
        is built.  That is F06-11, and it is enforced by where the call is
        rather than by a flag -- there is no path from inside `_collect` to
        here, so compaction cannot land in the middle of a stream and leave
        half a turn describing a history that no longer exists.

        The size that matters is the size of the *next* request, which is this
        history plus the tool schemas, corrected by whatever the server has
        told us so far.
        """
        if self.context_window is None or self.summariser is None:
            return history, None

        sizer = self._sizer()
        estimated = sizer.messages(history.to_wire(self.dialect))
        if estimated <= self.context_window * COMPACT_AT:
            return history, None

        result = await compact(
            history,
            summarise=self.summariser,
            budget=int(self.context_window * COMPACT_TO),
            sizer=sizer,
        )
        self.recorder.record(
            "compaction",
            {
                "before_tokens": estimated,
                "dropped": result.plan.drops,
                "generation": result.generation,
                "degraded": result.degraded,
                "calibration": self.calibration.describe(),
                "fits": result.plan.fits,
            },
        )
        return result.history, result
```

> - **`_sizer()`**：造一把"当前的尺子"——模型带的工具 schema，加上目前的校准比例。
> - **`_raw_estimate()`**：没乘校准比例的估计。它为什么存在，是 §22.2 的故事。
> - **`_maybe_compact(history)`**：没设窗口或没有摘要器，原样返回；估计值没过触发线，原样返回；否则调用 `compact()`，
>   往录像里记一条 `compaction` 事件，返回新的历史。
>   docstring 说了 F06-11 是怎么被挡住的：**这个方法只在一个地方被调用——循环的最上面，请求还没开始构造的时候。**
>   `_collect`（读流的那个方法）里没有任何一条路能走到这里。这不是纪律，是**代码的结构**。

`run()` 改完之后：

```python
    async def run(self, user_message: str) -> RunResult:
        history = History()
        if self.instructions is not None:
            history.add_system_note(self.instructions)
        history.add_user(user_message)
        final_text = ""
        compactions: list[CompactionResult] = []

        for turn_index in range(self.max_turns):
            remaining = self.max_turns - turn_index

            history, compaction = await self._maybe_compact(history)
            if compaction is not None:
                compactions.append(compaction)

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
            estimated = self._sizer().messages(messages)
            self.recorder.record("request", {"turn": turn_index, "messages": messages})

            turn = await self._collect(self.model.stream(messages))

            # The one moment the guess can be checked against the truth.  It is
            # done unconditionally, not only when compaction is enabled: a run
            # that never compacts still produces the observation that tells the
            # next one how wrong its estimator is.
            if turn.usage is not None:
                self.calibration.observe(
                    estimated=self._raw_estimate(messages), actual=turn.usage.prompt_tokens
                )

            self.recorder.record(
                "response",
                {
                    "turn": turn_index,
                    "text": turn.text,
                    "finish_reason": turn.finish_reason,
                    "tool_calls": [{"id": c.call_id, "name": c.name} for c in turn.tool_calls],
                    "estimated_prompt_tokens": estimated,
                    "actual_prompt_tokens": turn.usage.prompt_tokens if turn.usage else None,
                    "calibration": self.calibration.describe(),
                },
            )

            history.add_assistant(turn.text, turn.tool_calls)
            if turn.text:
                final_text = turn.text

            # Text is not a stop signal.  Models narrate before acting, and
            # stopping on the narration leaves the work undone while looking
            # exactly like success.
            if not turn.tool_calls:
                return RunResult(
                    final_text, "completed", turn_index + 1, history, tuple(compactions)
                )

            # Every call runs and every call is answered.  add_tool_result is
            # what makes "answered" mean something: it refuses an id that was
            # never issued, and refuses to answer the same id twice.
            for call in turn.tool_calls:
                output = await self._run_tool(call)
                history.add_tool_result(call.call_id, output)

        return RunResult(final_text, "turn_limit", self.max_turns, history, tuple(compactions))
```

> 相对第 5 章，多了这些：
>
> - 每一轮开头调用 `_maybe_compact`，压缩过就记进 `compactions`；
> - 发请求之前算出 `estimated`（校准后的估计），写进录像；
> - **拿到回答之后，如果带着用量，就校准一次**。注意这一步**不在**"开启了压缩才做"的条件里：
>   一次从不压缩的运行，照样会留下"我的估计偏了多少"这个观察；
> - 录像的 `response` 里多了三项：估计值、实际值、校准状态；
> - 两处 `return RunResult(...)` 都带上 `compactions`。

### 22.2 意外：校准比例在来回跳

上面 `run()` 里校准的那一行，现在是：

```python
                self.calibration.observe(
                    estimated=self._raw_estimate(messages), actual=turn.usage.prompt_tokens
                )
```

它原来是：

```python
                self.calibration.observe(estimated=estimated, actual=turn.usage.prompt_tokens)
```

看起来天经地义：估计值、实际值，交给校准器。改写这一章时，为了看校准怎么收敛，写了一个假的模型——
它报的用量**永远正好是原始估计的 1.5 倍**——然后把每一轮喂进去的数和得到的比例打印出来：

```
  fed estimate=    4  actual=    6  -> ratio 1.50
  fed estimate=  322  actual=  322  -> ratio 1.00
  fed estimate=  427  actual=  640  -> ratio 1.50
  fed estimate=  956  actual=  957  -> ratio 1.00
  fed estimate=  849  actual= 1273  -> ratio 1.50
  fed estimate= 1589  actual= 1590  -> ratio 1.00
  fed estimate= 1272  actual= 1908  -> ratio 1.50
```

**1.50，1.00，1.50，1.00。** 真实的比例一直是 1.5，而校准器隔一轮就把它忘掉一次。

原因在 `estimated` 是什么。它是 `self._sizer().messages(messages)` 算的，而 `_sizer()` **已经乘过当前的比例了**：

- 第 1 轮：比例是 1.0，估 4，实际 6 → 比例变成 1.5。对。
- 第 2 轮：原始估计 215，乘 1.5 得 322；实际 322 → "实际 ÷ 估计" = 1.0。**比例被改回了 1.0。**
- 第 3 轮：比例是 1.0，估 427，实际 640 → 1.5。……

**"实际值 ÷ 已经修正过的估计"不是修正系数，而是修正系数的误差。** 估得越准，这个数越接近 1，于是校准器把自己关掉；
下一轮估计又不准了，它再打开。

症状：没有。没有异常，没有测试失败——原来那个测试只跑一轮，而第一轮的比例是 1.0，正好是唯一不出错的一轮。
`probe_calibration.py` 也测不到它，因为探针是自己算比例的（算对了）。**估计只是每隔一轮就没被修正。**
这个 bug 从这一章的代码写成那天起就在，一直带到了全书最后一章的代码里。

修法就是 `_raw_estimate()`：校准器吃的必须是**没修正过的**估计。它的 docstring 记下了上面这组数字。测试：

```python
async def test_F06_07_the_agent_feeds_usage_back_into_the_estimate():
    class ModelWithUsage:
        tools = ()

        async def stream(self, messages):
            yield TextDelta("done")
            yield Usage(1000, 5)
            yield Completed("stop")

    agent = Agent(ModelWithUsage())
    await agent.run("hello")
    assert agent.calibration.calibrated
    assert agent.calibration.ratio > 1


async def test_F06_07_the_ratio_is_measured_against_the_uncorrected_estimate():
    """Found by printing the ratio turn by turn, long after this chapter shipped.

    The agent sized each request with the *corrected* estimate and fed that
    same number to `Calibration.observe`.  The truth divided by an
    already-corrected guess is not the correction, it is the correction's
    error -- so against a server that always charges 1.5x the raw estimate the
    ratio read 1.50, 1.00, 1.50, 1.00: right on one turn, switched off on the
    next.  Nothing failed, and the one-turn test above stayed green, because
    the first observation is the only one made with a ratio of 1.0.
    """
    from minicodex.model import ToolCallDelta

    ratios: list[float] = []

    class HalfAgain:
        tools = ()

        def __init__(self):
            self.turn = 0

        async def stream(self, messages):
            self.turn += 1
            ratios.append(agent.calibration.ratio)
            if self.turn <= 5:
                yield ToolCallDelta(f"c{self.turn}", 0, "run_shell", '{"command": "ls"}')
            else:
                yield TextDelta("done")
            yield Usage(int(estimate_messages(messages) * 1.5), 1)
            yield Completed("stop")

    async def tool(args):
        return "o" * 400

    agent = Agent(HalfAgain(), {"run_shell": tool})
    await agent.run("go")
    assert ratios[0] == 1.0, "nothing has been observed before the first request"
    assert len(ratios) == 6
    for ratio in ratios[1:]:
        assert ratio == pytest.approx(1.5, abs=0.02)
```

> - 第一个是原来就有的：跑一轮，校准器有了观察，比例大于 1。**它一直是绿的。**
> - 第二个是新加的：一个永远多收一半的假模型，连续六轮；每次请求发出时把当前的比例记下来。
>   第一次是 1.0（还没有任何观察），**之后每一次都应该是 1.5 左右**（`pytest.approx(1.5, abs=0.02)`：允许 0.02 的误差，因为取整）。
>   `ratios.append(agent.calibration.ratio)` 里的 `agent` 是在类定义**后面**才创建的——函数体里的名字是到执行时才查找的，所以没问题。

修完之后在真服务端上看到的样子（§22.4 那次运行的录像）：`est=1004 act=1004`。

> **这条和这一章别的静默故障是同一类**：一个"会纠正自己"的机制，没有任何东西在检查**它有没有在纠正**。
> 发现它的办法也一样朴素：把中间的数打印出来看。

### 22.3 命令行

`__main__.py` 改三处。`_ask` 多一个参数，传给 `Agent`，结束时多打印两行：

```python
async def _ask(
    question: str,
    *,
    provider: str,
    base_url: str | None,
    model: str | None,
    session: Session,
    context_window: int | None,
) -> int:
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
    agent = Agent(
        llm,
        default_tools(session=session),
        recorder=recorder,
        instructions=_instructions(session),
        context_window=context_window,
        # The summariser shares the client, and therefore the provider and the
        # key, but not the tools: `make_summariser` builds its own history.
        summariser=make_summariser(llm) if context_window else None,
    )

    result = await agent.run(question)

    print(result.final_text or "(no answer)")
    print(f"\n[{llm.model} | {result.stop_reason} after {result.turns_used} turn(s)]")
    print(f"[{session.describe()}]")
    for event in result.compactions:
        print(f"[{event.describe()}]")
    print(f"[tokens: {agent.calibration.describe()}]")
    print(f"[transcript: {recorder.path}]")
    return 0
```

> - `summariser=make_summariser(llm) if context_window else None`：摘要器和 Agent 用同一个模型客户端（同一个服务商、同一个 key），
>   但它用自己的历史，不带工具（§14.1）。
> - 结束时打印每一次压缩的描述，以及校准状态。

`ask` 子命令多一个选项（加在 `--model` 后面），`main()` 里调用 `_ask` 时多传 `context_window=args.context_window`：

```python
    ask.add_argument(
        "--context-window",
        type=int,
        default=None,
        help=(
            "size of the model's context window in tokens. Compaction is off "
            "without it: nothing in this program can discover the number, and "
            "a guessed one is a silently wrong budget"
        ),
    )
```

开头的 import 加一行 `from minicodex.compaction import make_summariser`。

测试：

```python
async def test_F06_11_compaction_happens_between_turns_and_not_inside_one():
    """Enforced by where the call is, not by a flag.

    `_maybe_compact` is reachable only from the top of the loop.  Nothing in
    `_collect` can reach it, so a stream cannot be interrupted by a history
    that changes underneath it.
    """
    from minicodex.model import ToolCallDelta

    class Chatty:
        tools = ()

        def __init__(self):
            self.turn = 0

        async def stream(self, messages):
            self.turn += 1
            if self.turn <= 3:
                yield TextDelta("working")
                yield ToolCallDelta(f"c{self.turn}", 0, "run_shell", '{"command": "ls"}')
                yield Completed("tool_calls")
            else:
                yield TextDelta("done")
                yield Completed("stop")

    async def big_tool(args):
        return "o" * 4000

    compactions: list[str] = []

    async def watch(request: SummaryRequest) -> str:
        compactions.append(request.transcript)
        return "## Done\nsome work\n"

    agent = Agent(
        Chatty(),
        {"run_shell": big_tool},
        context_window=1500,
        summariser=watch,
        max_turns=6,
    )
    result = await agent.run("go")
    assert result.compactions
    # One compaction per turn at most, and each one observed a whole number of
    # turns: no transcript ends on an unanswered call.
    for transcript in compactions:
        assert transcript.count("ASSISTANT CALLS") == transcript.count("RESULT OF")


async def test_F06_11_no_compaction_below_the_trigger():
    class Quiet:
        tools = ()

        async def stream(self, messages):
            yield TextDelta("hi")
            yield Completed("stop")

    async def never(request: SummaryRequest) -> str:  # pragma: no cover
        raise AssertionError("compaction fired below the trigger")

    agent = Agent(Quiet(), context_window=1_000_000, summariser=never)
    result = await agent.run("hello")
    assert result.compactions == ()


def test_F06_11_the_trigger_and_the_target_are_not_the_same_number():
    """Compacting down to the trigger point means compacting again next turn."""
    from minicodex.agent import COMPACT_TO

    assert COMPACT_TO < COMPACT_AT
```

> - 一个每轮都调用工具、工具每次返回 4000 个字符的假模型，窗口设成 1500：压缩发生了；每一次交给摘要器的那一段里，
>   "调用"和"结果"的数量相等——没有一段是停在一次调用中间的。
> - 窗口有一百万时，摘要器一次都不该被调用（被调用就抛 `AssertionError`）。
> - 目标比触发线低。

### 22.4 真跑两次

**第一次，窗口够用。** 让它依次读五个文件，窗口设成 8000：

```
$ uv run minicodex ask "Read these files one at a time, in this order, and after each one note its single most important function: src/minicodex/paths.py, src/minicodex/recorder.py, src/minicodex/tool_errors.py, src/minicodex/tokens.py, src/minicodex/history.py. Do not read two files in the same turn. At the end, list the five functions." --provider openai --context-window 8000 --sandbox-mode read-only --yes
...
Now, here are the five most important functions from the files you requested:

1. `resolve` (in `src/minicodex/paths.py`)
2. `record` (method in `src/minicodex/recorder.py`)
3. `tool_error` (in `src/minicodex/tool_errors.py`)
4. `estimate_messages` (in `src/minicodex/tokens.py`)
5. `add_assistant` (method in `src/minicodex/history.py`)

[gpt-4o-mini | completed after 6 turn(s)]
[sandbox_mode=read-only, approval_policy=on-request]
[compacted: dropped 8 item(s), summarised, generation 1, ~3162 tokens]
[tokens: x0.86 from 6 observation(s)]
[transcript: .minicodex\recordings\session-1790819580.jsonl]
```

录像里每一轮的估计和实际：

```
turn 0  est=906   act=656   x0.72
turn 1  est=1358  act=1594  x0.85
turn 2  est=2451  act=2527  x0.88
turn 3  est=3397  act=3476  x0.90
turn 4  est=5156  act=5375  x0.93
        COMPACT  before_tokens=6991  dropped=8  generation=1  fits=True
turn 5  est=2672  act=2464  x0.86
```

第 5 轮开始前，估计值 6991 超过了触发线（8000 × 0.75 = 6000），压缩丢掉了前四个文件的八条消息，换成一份摘要。
**最后的答案里，前四个函数来自那份摘要**——那些文件的内容已经不在历史里了。校准后的估计和实际值，从第 2 轮起误差都在 10% 以内。

（这里的比例小于 1：这次会话里占大头的是系统提示和工具 schema，补上它们之后估计偏高——和 §10 那张表前两轮的方向一样。
**估计往哪边偏，取决于会话里装的是什么**，这正是需要校准而不是调常数的原因。）

**第二次，窗口不够用。** 原来这一章的示例是 3000 的窗口，让它读 `tokens.py` 和 `compaction.py` 两个文件：

```
$ uv run minicodex ask "List every .py file under src/minicodex, then read src/minicodex/tokens.py and src/minicodex/compaction.py and tell me what CHARS_PER_TOKEN is and why boundaries() exists." --provider openai --context-window 3000 --sandbox-mode read-only --yes
(no answer)

[gpt-4o-mini | turn_limit after 12 turn(s)]
[sandbox_mode=read-only, approval_policy=on-request]
[compacted: dropped 4 item(s), summarised, generation 1, ~1316 tokens]
[compacted: dropped 5 item(s), summarised, generation 2, ~1424 tokens]
[compacted: dropped 5 item(s), summarised, generation 3, ~1424 tokens]
...
[compacted: dropped 6 item(s), summarised, generation 11, ~1429 tokens]
[tokens: x0.84 from 12 observation(s)]
```

**12 轮，11 次压缩，没有答案。** 录像里每一轮都一样：

```
REQ turn=1 roles=sus
  RESP calls=['run_shell', 'read_file', 'read_file'] est=853 act=1004
  COMPACT {'before_tokens': 7954, 'dropped': 5, 'generation': 2, 'fits': False}
REQ turn=2 roles=sus
  RESP calls=['run_shell', 'read_file', 'read_file'] est=1004 act=1004
  COMPACT {'before_tokens': 7952, 'dropped': 5, 'generation': 3, 'fits': False}
```

两个文件读回来大约 8000 个 token，窗口只有 3000。压缩把刚读回来的文件内容整段换成了摘要；模型要回答问题，需要文件内容，于是**再读一遍**；
读回来又装不下，再压缩……每一轮的请求都是同一个形状（`sus`：系统消息、用户消息、摘要）。

（2026-08-10 的那次记录是 2 轮就答出来了——那次模型直接从摘要里作答。同样的命令，换一天，结果不同。）

> **这是一条清单上没有的故障，而且这一章没有解决它：** 任务的**某一步**需要的材料比窗口还大时，压缩帮不上忙，反而会和模型形成一个循环——
> 压缩删掉，模型取回，压缩再删掉。现在唯一拦住它的是第 0 章的轮次上限。
>
> 录像里其实已经有信号了：`fits: False`——`plan()` 明确说了"我没法让它装下"，而 `Agent` 目前只是把它记下来，没有据此做任何事。
> 记进 `FAULTS.md` 和 `README` 的"故意没做"。

```bash
git add src/minicodex/agent.py src/minicodex/__main__.py tests/test_compaction.py
git commit -m "feat(agent): compact between turns, and calibrate against the uncorrected estimate"
```

---

## §23 属性测试，和它撞出来的东西

到这里，清单上的十三条都有了着落，例子测试全绿。但这一章有一个前面几章没有的性质：

> **这是全书第一个"有意思的输入是一个形状，而不是一个值"的模块。**

前面写的每个例子测试，用的都是自己搭的历史——而搭它的人已经知道合法切点在哪了。这些测试证明的是"代码在我想得到的形状上是对的"。

所以加一类测试：**随机生成历史，断言性质。** 新建 `tests/test_properties.py`：

```python
"""Properties that must hold for *every* history, not the four in the examples.

Compaction is the first thing in this project where the interesting input is a
shape rather than a value.  The example tests all use histories this file
wrote, which means they all use histories whose author already knew where the
boundaries were.  A generator does not know that, which is the point.

No `hypothesis`: a seeded generator is enough here and costs no dependency.
The trade is real and worth stating -- there is no shrinking, so a failure
prints a seed rather than a minimal example, and reproducing it means running
`MINICODEX_PROPERTY_SEED=<n>`.  Chapter 14 revisits this.

CI runs this file with `MINICODEX_PROPERTY_CASES=2000`; the default is small
enough that nobody is tempted to skip it locally.
"""

from __future__ import annotations

import os
import random

import pytest

from minicodex.agent_types import ToolCall
from minicodex.compaction import (
    Protected,
    Sizer,
    SummaryRequest,
    boundaries,
    compact,
    plan,
    unused_call_ids,
)
from minicodex.history import History

CASES = int(os.environ.get("MINICODEX_PROPERTY_CASES", "200"))
BASE_SEED = int(os.environ.get("MINICODEX_PROPERTY_SEED", "0"))


def random_history(rng: random.Random) -> History:
    """A conversation of arbitrary shape that is nonetheless always valid.

    Built through `History`'s own API, so the generator cannot produce an
    illegal input even by accident -- which is what makes a failure downstream
    unambiguous: the compactor did it.
    """
    h = History()
    for _ in range(rng.randint(0, 2)):
        h.add_system_note("note " + "n" * rng.randint(0, 200))
    if rng.random() < 0.9:
        h.add_user("task " + "u" * rng.randint(0, 300))
    for _ in range(rng.randint(0, 14)):
        roll = rng.random()
        if roll < 0.12:
            h.add_assistant("thinking " + "t" * rng.randint(0, 200))
        elif roll < 0.2:
            h.add_user("follow-up " + "f" * rng.randint(0, 100))
        elif roll < 0.26:
            h.add_system_note("You have 2 turn(s) left.")
        else:
            # One assistant message may issue several calls at once; each one
            # must be answered before the next assistant message.
            calls = [
                ToolCall(f"c{rng.randrange(10**9)}", "run_shell", {}, '{"command": "ls"}')
                for _ in range(rng.randint(1, 3))
            ]
            h.add_assistant("", calls)
            for call in calls:
                h.add_tool_result(call.call_id, "o" * rng.randint(0, 3000))
    return h


async def stub_summary(request: SummaryRequest) -> str:
    return "## Goal\ng\n## Done\nd\n## Open\no\n"


def seeds() -> list[int]:
    return [BASE_SEED + i for i in range(CASES)]


@pytest.mark.parametrize("seed", seeds())
def test_property_boundaries_are_exactly_the_renderable_prefixes(seed):
    """A cut is legal iff what remains has no orphaned result.

    Two independent definitions -- the counter in `boundaries()` and the
    set-difference in `unused_call_ids()` -- must agree on every index of every
    history.  Agreement is the evidence; either alone is just an assertion
    about itself.
    """
    items = random_history(random.Random(seed)).items
    legal = set(boundaries(items))
    for cut in range(len(items) + 1):
        orphans = unused_call_ids(items[cut:])
        assert (cut in legal) == (orphans == ()), (
            f"seed={seed} cut={cut} legal={cut in legal} orphans={orphans}"
        )


@pytest.mark.parametrize("seed", seeds())
async def test_property_compaction_always_yields_a_sendable_history(seed):
    rng = random.Random(seed)
    history = random_history(rng)
    budget = rng.choice([1, 50, 200, 600, 2000, 20000])
    result = await compact(history, summarise=stub_summary, budget=budget)
    # The invariant from chapter 1, restated as a property: whatever the shape
    # and whatever the budget, the thing that comes out can be sent.
    result.history.to_wire()
    result.history.to_wire("ollama_native")
    assert unused_call_ids(result.history.items) == ()


@pytest.mark.parametrize("seed", seeds())
async def test_property_the_protected_prefix_is_never_touched(seed):
    rng = random.Random(seed)
    history = random_history(rng)
    protected = Protected.of(history.items)
    before = history.items[: protected.count]
    result = await compact(history, summarise=stub_summary, budget=rng.choice([1, 100, 5000]))
    after = result.history.items[: protected.count]
    assert before == after, f"seed={seed}"


@pytest.mark.parametrize("seed", seeds())
async def test_property_compaction_never_grows_the_history(seed):
    """The summary note is one item; it must not cost more than it saved.

    Not trivially true -- a summary is inserted, so a history where nothing
    could be dropped would grow by one item and by the summary's tokens.  The
    empty-`dropped` branch exists for exactly that case, and this is what pins
    it.
    """
    rng = random.Random(seed)
    history = random_history(rng)
    sizer = Sizer()
    before = sizer.messages(history.to_wire())
    result = await compact(history, summarise=stub_summary, budget=rng.choice([1, 300, 3000]))
    after = sizer.messages(result.history.to_wire())
    assert after <= before, f"seed={seed} before={before} after={after}"


@pytest.mark.parametrize("seed", seeds())
def test_property_the_plan_only_ever_cuts_on_a_boundary(seed):
    rng = random.Random(seed)
    items = random_history(rng).items
    the_plan = plan(items, budget=rng.choice([1, 100, 1000, 10000]))
    assert the_plan.cut in boundaries(items), f"seed={seed}"
    assert the_plan.cut >= the_plan.protected
```

> - **`CASES`、`BASE_SEED`**：跑多少个随机历史、从哪个种子开始，都可以用环境变量改。默认 200 个。
> - **`random_history(rng)`**：随机搭一段历史——0 到 2 条系统消息，多半有一条用户消息，然后 0 到 14 步，
>   每一步随机是"助手自言自语""用户追问""一条轮次警告"或者"一次发起 1 到 3 个调用并全部回答"。
>   **关键是它用 `History` 自己的方法来搭**：生成器不可能造出非法的输入，所以下游一旦失败，责任方只有一个——压缩。
> - **`seeds()`** 和 `@pytest.mark.parametrize("seed", seeds())`：每个种子是一个独立的测试用例，失败时名字里带着种子号，可以单独重跑。
> - **五条性质**：
>   1. **合法切点正好是"切完没有没主人的结果"的那些位置。** 这条最值得学：它不是"断言函数返回了我期望的值"，
>      而是**让两个独立写出来的定义互相验证**——`boundaries()` 里的计数器，和 `unused_call_ids()` 里的集合。
>      一个人用两种不同的写法得到同一个错误答案，概率很低。
>   2. 不管什么形状、什么预算，压缩出来的历史**一定能发出去**（两种发送格式都试）。
>   3. 受保护的前缀**一个字都没变**。
>   4. 压缩**不会让历史变大**。
>   5. 计划的切点一定是合法切点，而且不在前缀里面。
> - 开头的 docstring 说了为什么没用 `hypothesis`（Python 里做属性测试的常用库）：一个带种子的生成器在这里够用，还省一个依赖。
>   代价是真的——`hypothesis` 在失败时会自动把输入缩到最小，这里只会打印一个种子号。

默认 200 个种子，1000 个用例，一两秒，全绿。

### 23.1 把种子数调到 5000

第 4 条性质（压缩不会让历史变大）不是一开始就成立的。当时 `plan()` 里还没有"不省就不切"的那个出口。
把那两处保护删掉，用 5000 个种子重跑（改写时在一份临时拷贝里实测）：

```
$ MINICODEX_PROPERTY_CASES=5000 uv run pytest tests/test_properties.py
FAILED tests/test_properties.py::test_property_compaction_never_grows_the_history[235]
FAILED tests/test_properties.py::test_property_compaction_never_grows_the_history[238]
FAILED tests/test_properties.py::test_property_compaction_never_grows_the_history[285]
...
E       AssertionError: seed=235 before=89 after=93
57 failed, 24943 passed in 76.17s (0:01:16)
```

**压缩把历史变大了：89 个 token 进去，93 个出来。**

原因一句话就能说清：**压缩不是免费的**。它删掉一段消息，插入一条摘要。被删的那段很小时，摘要比它替换掉的东西还贵。
当时的 `plan()` 只会找"第一个装得下的切点"，从来没问过"这么切到底省不省"。

如果只是浪费一次调用，这算性能问题。但它不止：

> 估计值降不到触发线以下 → 下一轮再触发 → 再删一小段、再插一条摘要 → 还是降不下来 → ……
> **会话收敛到一个全是"摘要的摘要"的历史，真正的工作内容一路被磨掉。**

修法就是 §19.2 里 `plan()` 的那几行：切完的大小（含摘要预算）不比现在小，就什么都不做。docstring 里写明了它**不是设计出来的**，
是种子 235 撞出来的。

### 23.2 属性测试抓到的，属性测试不一定守得住

这里有个陷阱。发现 bug 的那次运行是 **5000 个种子**，而默认是 **200 个**。**种子 235 不在默认会跑的范围里。**

实测：把保护删掉之后跑默认的 `pytest`，属性测试那 1000 个用例**全绿**。也就是说，如果只有属性测试守着，以后有人把保护删了，本地不会红。

所以属性测试抓到东西之后，**必须补一个写死输入的例子测试**：

```python
@pytest.mark.parametrize(
    ("pairs", "chars", "budget", "summary_budget"),
    [
        (1, 20, 1, 700),  # nothing fits at all: the fallback at the end of plan()
        (12, 300, 1450, 400),  # a cut fits and costs more than it saves: the
    ],  # guard inside the loop.  This one is 1099 tokens, so a
)  # 400-token summary buys back less than it costs.
async def test_a_compaction_that_would_not_save_anything_does_nothing(
    pairs, chars, budget, summary_budget
):
    """Found by the property test at seed 235: compacting an 89-token history
    produced a 93-token one.

    The summary has a fixed cost, so a small dropped region is a net loss --
    and a net loss is not merely wasted, it is a loop: the estimate stays above
    the trigger and the next turn compacts again.

    Both branches, because the first version of this test only reached the
    fallback.  Mutation testing found that: disabling the guard *inside* the
    loop left every test green.  And the property run that found the bug in the
    first place used 5000 cases, while the default is 200 -- seed 235 is not in
    the set that runs on an ordinary `pytest`.  A property test that catches
    something is not the same as a test that keeps catching it.
    """
    small = history_with(pairs, output="w" * chars)
    before = Sizer().messages(small.to_wire())
    result = await compact(
        small, summarise=constant_summary, budget=budget, summary_budget=summary_budget
    )
    assert result.plan.drops == 0
    assert result.plan.saving == 0
    assert Sizer().messages(result.history.to_wire()) == before
    assert result.summary == ""
```

> - 两组参数，对应 `plan()` 里的两个出口：`(1, 20, 1, 700)` 走的是函数末尾"什么都装不下"的那个；
>   `(12, 300, 1450, 400)` 走的是循环里面"装得下、但不省"的那个。
> - 断言：没有丢任何东西、省下的是 0、大小没变、没有摘要。
> - docstring 记下了第二组参数的来历：第一版只有第一组，变异测试发现循环里面的保护删掉后全绿；
>   第二组参数是**搜出来的，不是想出来的**——凭推理写的参数，走的不是以为的那条分支。

> **判断一个测试走到了哪条分支，不要靠读代码推理。** 要么让变异测试告诉你，要么打印出来看。

### 23.3 CI 里加一步

`.github/workflows/ci.yml` 末尾加上：

```yaml
      # Added in chapter 6, and only in chapter 6, because this is the first
      # module whose interesting input is a *shape* rather than a value: a
      # history can be legal or illegal in ways no example test enumerates.
      #
      # It runs separately from the suite above with a much larger case count.
      # The local default (200) is small enough that nobody skips it; 2000 here
      # is cheap because CI is not waiting for a human. The bug this exists for
      # -- compaction that makes a history *bigger* -- first appeared at seed
      # 235, which the local default does not reach.
      - name: Property tests
        env:
          MINICODEX_PROPERTY_CASES: "2000"
        run: uv run pytest tests/test_properties.py
```

> 本地默认 200 个种子，快到没人想跳过；CI 里用 2000 个，因为 CI 不占人的时间。分层的道理和第 -1 章一样。
> （本机实测：2000 个种子、10000 个用例，11 秒。）

```bash
git add tests/test_properties.py tests/test_compaction.py src/minicodex/compaction.py .github/workflows/ci.yml
git commit -m "fix(compaction): a compaction that would not save anything cuts nothing"
```

---

## §24 逐条验证

### 24.1 全量

```
$ uv run pytest
1251 passed, 8 skipped in 10.90s
```

```
$ uv run ruff check
All checks passed!
$ uv run ruff format --check
42 files already formatted
```

（Windows 上的结果。）1251 个里，1000 个是属性测试（200 个种子 × 5 条性质），205 个是前面几章留下来的，46 个是这一章的例子测试。
8 个跳过和第 5 章一样。

### 24.2 变异测试

和第 5 章一样的问题：**这些测试真的有用吗？** 这次把变异脚本写成项目里的一个文件。新建 `probe_mutations.py`：

```python
"""Break one decision at a time; check a test notices.

The output of this is not "the tests are good". It is a distribution: which
decisions are pinned by many tests, which by exactly one, and which by none.
The last group is the interesting one -- code nothing tests is code nobody can
change safely, and it looks exactly like code that works.

Chapter 5's version of this script reported all-green because it counted the
wrong lines, so two things are non-negotiable here: count `FAILED`, and abort
loudly if the mutation did not actually apply.

    python probe_mutations.py
"""

from __future__ import annotations

import atexit
import pathlib
import signal
import subprocess
import sys

SRC = pathlib.Path("src/minicodex")

# (label, file, find, replace)
MUTATIONS: list[tuple[str, str, str, str]] = [
    (
        "boundaries: every index is a legal cut",
        "compaction.py",
        "        if open_calls == 0:\n            result.append(index)",
        "        if True:\n            result.append(index)",
    ),
    (
        "boundaries: forget that a result closes a call",
        "compaction.py",
        "        elif isinstance(item, ToolResult):\n            open_calls -= 1",
        "        elif isinstance(item, ToolResult):\n            open_calls -= 0",
    ),
    (
        "protected: stop protecting the first user message",
        "compaction.py",
        "        if index < len(items) and isinstance(items[index], UserMessage):\n"
        "            index += 1",
        "        if False:\n            index += 1",
    ),
    (
        "plan: do not reserve room for the summary",
        "compaction.py",
        "        size = _size(head, tail, sizer) + summary_budget",
        "        size = _size(head, tail, sizer)",
    ),
    (
        "plan: take the last legal cut instead of the earliest",
        "compaction.py",
        "    for cut in candidates:",
        "    for cut in reversed(candidates):",
    ),
    (
        "sizer: ignore the calibration when trimming the summary",
        "compaction.py",
        "        keep = int(budget * CHARS_PER_TOKEN / self.ratio)",
        "        keep = int(budget * CHARS_PER_TOKEN)",
    ),
    (
        "clip_item: keep the tail only",
        "compaction.py",
        "    head = limit_chars // 2\n    tail = limit_chars - head",
        "    head = 0\n    tail = limit_chars",
    ),
    (
        "compact: accept an empty summary",
        "compaction.py",
        "        if not summary.strip():\n            raise CompactionError("
        '"summariser returned an empty summary")',
        "        pass",
    ),
    (
        "compact: do not show the summariser the protected prefix",
        "compaction.py",
        "                context=render_transcript(items[: the_plan.protected]),",
        '                context="",',
    ),
    (
        "generation: parse the last number in the header again",
        "compaction.py",
        '                if token == "generation" and tokens[index + 1].isdigit():\n'
        "                    generation = int(tokens[index + 1])\n"
        "                    break",
        "                if tokens[index + 1].isdigit():\n"
        "                    generation = int(tokens[index + 1])",
    ),
    (
        "plan: compact even when it would not save anything",
        "compaction.py",
        "            if size >= current and cut > protected_count:\n"
        "                return _do_nothing(fits=current <= budget)",
        "            if False:\n                return _do_nothing(fits=current <= budget)",
    ),
    (
        "tokens: stop counting the tool schemas",
        "tokens.py",
        "    if tools:\n        total += len(json.dumps(list(tools))) / chars_per_token",
        "    if False:\n        total += len(json.dumps(list(tools))) / chars_per_token",
    ),
    (
        "tokens: drop the per-message framing cost",
        "tokens.py",
        "PER_MESSAGE_TOKENS = 4",
        "PER_MESSAGE_TOKENS = 0",
    ),
    (
        "tokens: let a zero usage report set the ratio",
        "tokens.py",
        "        if estimated <= 0 or actual <= 0:\n            return",
        "        if estimated <= 0:\n            return",
    ),
    (
        "tokens: size unknown content as empty instead of refusing",
        "tokens.py",
        "    raise UncountableContent(",
        "    return 0\n    raise UncountableContent(",
    ),
    (
        "model: stop asking for usage",
        "model.py",
        '        if self.report_usage:\n            body["stream_options"] = '
        '{"include_usage": True}',
        "        pass",
    ),
    (
        "model: index into an empty choices list again",
        "model.py",
        '                    if not chunk.get("choices"):\n                        continue',
        "                    pass",
    ),
    (
        "agent: compact down to the trigger point",
        "agent.py",
        "COMPACT_TO = 0.45",
        "COMPACT_TO = 0.75",
    ),
    (
        "agent: calibrate against the corrected estimate",
        "agent.py",
        "estimated=self._raw_estimate(messages), actual=turn.usage.prompt_tokens",
        "estimated=estimated, actual=turn.usage.prompt_tokens",
    ),
]


_ORIGINALS: dict[pathlib.Path, str] = {}


def _restore_everything() -> None:
    """Put every touched file back, whatever happens.

    A `try/finally` around one mutation is not enough, and finding that out
    cost a corrupted working tree: Ctrl-C during the pytest subprocess killed
    this process before `finally` ran, and left `if False:` sitting in
    `tokens.py`.  The suite was still green afterwards -- the mutation only
    breaks tests that assert on schema size -- so nothing announced it.

    A tool that edits your source is a tool that can leave your source edited.
    Snapshot up front, restore from an exit hook and a signal handler, and make
    "restore" idempotent so running it twice is free.
    """
    for path, text in _ORIGINALS.items():
        if path.read_text(encoding="utf-8") != text:
            path.write_text(text, encoding="utf-8")
            print(f"restored {path}")


def run_tests() -> tuple[int, str]:
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "--no-header", "-p", "no:cacheprovider"],
        capture_output=True,
        text=True,
    )
    failed = sum(1 for line in proc.stdout.splitlines() if line.startswith("FAILED"))
    names = [
        line.split("::", 1)[-1].split(" ")[0]
        for line in proc.stdout.splitlines()
        if line.startswith("FAILED")
    ]
    return failed, ", ".join(sorted(set(names))[:3])


def main() -> int:
    for _, filename, _, _ in MUTATIONS:
        path = SRC / filename
        _ORIGINALS.setdefault(path, path.read_text(encoding="utf-8"))
    atexit.register(_restore_everything)
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: sys.exit(130))

    baseline, _ = run_tests()
    if baseline:
        print(f"baseline is not green ({baseline} failures); fix that first")
        return 1
    print(f"baseline: green\n\n{'mutation':<52} {'failed':>7}  first tests to notice")
    print("-" * 110)

    survivors = []
    for label, filename, find, replace in MUTATIONS:
        path = SRC / filename
        original = path.read_text(encoding="utf-8")
        if find not in original:
            print(f"{label:<52} {'ABORT':>7}  pattern not found in {filename}")
            return 2
        try:
            path.write_text(original.replace(find, replace, 1), encoding="utf-8")
            failed, names = run_tests()
        finally:
            _restore_everything()
        if failed == 0:
            survivors.append(label)
        print(f"{label:<52} {failed:>7}  {names}")

    print("-" * 110)
    if survivors:
        print(f"{len(survivors)} mutation(s) survived -- nothing tests these decisions:")
        for label in survivors:
            print(f"  - {label}")
    else:
        print("every mutation was caught")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

> - **`MUTATIONS`**：十九条，每条是"标签、文件名、要找的文字、换成什么"。每条都是把这一章的一个决定撤销掉。
> - 开头的 docstring 点名了第 5 章的教训，两条规矩写进了代码：**数 `FAILED` 开头的行**（`run_tests`）；
>   **要找的文字不在文件里，立刻中止**（`ABORT ... pattern not found`）。
> - **`_restore_everything()`** 和 `main()` 开头那几行，是这个脚本自己出过的一次事故留下的——下面讲。

实测（在一份临时拷贝里跑，不在工作目录里）：

```
mutation                                              failed  first tests to notice
------------------------------------------------------------------------------------
boundaries: every index is a legal cut                   808  test_F06_01_02_03_boundaries_match...
boundaries: forget that a result closes a call           202  test_F06_01_02_03_boundaries_match...
protected: stop protecting the first user message          5  test_F06_04_summariser_is_also_given_the_protected_prefix...
plan: do not reserve room for the summary                  2  test_F06_12_the_summary_budget_is_reserved...
plan: take the last legal cut instead of the earliest      3  test_F06_09_a_single_huge_result_is_clipped...
sizer: ignore the calibration when trimming the summary    1  test_F06_12_the_summary_is_measured_with_the_same_sizer_as_the_cut
clip_item: keep the tail only                              1  test_F06_09_an_oversized_result_is_clipped_at_both_ends
compact: accept an empty summary                           1  test_F06_08_an_empty_summary_counts_as_a_failure
compact: do not show the summariser the protected prefix   1  test_F06_04_summariser_is_also_given_the_protected_prefix
generation: parse the last number in the header again      2  test_F06_13_generations_are_counted...
plan: compact even when it would not save anything         1  test_a_compaction_that_would_not_save_anything_does_nothing[12-300-1450-400]
tokens: stop counting the tool schemas                     1  test_F06_07_estimate_counts_the_tool_schemas
tokens: drop the per-message framing cost                  2  test_F06_07_estimate_counts_a_cost_per_message...
tokens: let a zero usage report set the ratio              2  test_F06_07_a_missing_usage_field_cannot_zero_the_ratio...
tokens: size unknown content as empty instead of refusing  1  test_F06_10_multimodal_content_is_refused_not_guessed
model: stop asking for usage                               1  test_F06_07_usage_must_be_requested_explicitly
model: index into an empty choices list again              1  test_F06_07_the_usage_chunk_has_no_choices
agent: compact down to the trigger point                   1  test_F06_11_the_trigger_and_the_target_are_not_the_same_number
agent: calibrate against the corrected estimate            1  test_F06_07_the_ratio_is_measured_against_the_uncorrected_estimate
------------------------------------------------------------------------------------
every mutation was caught
```

**19 条全被抓住。但"全被抓住"不是这张表的产出。** 产出是右边那一列的分布：

- 头两条有 808 和 202 个测试失败——属性测试的每个种子都会红。这类核心决定是被测试**淹没**的。
- **十一条只有 1 个测试能发现。** 删掉那一个测试，那个决定就再也没人守着了。

其中有四条，是这一章**真的发生过**的 bug：两把尺子、不看省不省、空的 `choices`、对着修正过的估计校准。

### 24.3 变异脚本自己的事故

第 5 章的变异脚本数错了行，报告全绿。这一章的脚本出了**另一件事**（原始记录）：

跑到一半，进程被 Ctrl-C 打断了。脚本里有 `try/finally` 负责把文件改回去，但被打断时正在跑的是 pytest 子进程，
父进程被直接结束，`finally` 没有执行。**结果：`src/minicodex/tokens.py` 里留着一行 `if False:`。**

然后跑 `pytest`，**全绿**——那条变异当时正好还没有测试守着。

> **一个会改你源码的工具，就是一个会把你的源码改坏的工具。**
> 而"改坏之后测试还是绿的"，恰恰是它想帮你发现的那类问题的一个实例。

修法不是"记得别按 Ctrl-C"，而是 `_restore_everything()`：一开始就把所有要动的文件存一份，退出时（`atexit`）和收到中断信号时（`signal`）
都执行还原，并且还原可以重复执行（内容没变就不写）。

**更正确的做法是根本不碰工作目录**：把项目拷到一个临时目录，在那里变异。这一章和第 5 章的实测都是这么跑的。
脚本本身仍然是"原地改、再还原"，这是一个**记在账上的缺口**。

### 24.4 对照清单

| 编号 | 猜测 | 结果 |
|---|---|---|
| F06-01 | 砍一半 → 400 | **没按猜的出现**：八条的历史上砍一半是合法的——因为 8 是偶数。真正的问题是这条规则"从来不看" |
| F06-02 | 按 token 数切，落在调用中间 | **成立**（400） |
| F06-03 | 留最后 N 条，开头是结果 | **成立**，取决于 N |
| F06-04 | 摘要丢掉约束和决定 | **成立**；六小节提示词减少了损失，减不到零 |
| F06-05 | 压缩后重做已完成的工作 | **没有复现**（30 次里 0 次），没有为它写代码 |
| F06-06 | 问题被压掉 | **成立，而且是静默的**：200，答的是另一个问题 |
| F06-07 | 估算差 30% | **成立，更严重**：最多差一半以上，没有除数能修；而且用量数据默认不返回 |
| F06-08 | 摘要器失败 | **动工前就挡住**：降级摘要，并告诉模型 |
| F06-09 | 单条输出比窗口大 | **动工前就挡住**：头尾截断 |
| F06-10 | 图片没算 | **没有实现，改成大声拒绝** |
| F06-11 | 压缩发生在流中间 | **动工前就挡住**：靠调用的位置 |
| F06-12 | 压缩完还超 | **动工前就挡住**：先留摘要预算；过程中发现了"两把尺子" |
| F06-13 | 逐代衰减 | **没按猜的出现**：要么第 1 代丢，要么一直在 |

---

## §25 收工

### 25.1 这一章的文件

| 文件 | 状态 | 在哪一节 |
|---|---|---|
| `src/minicodex/compaction.py` | 新增 | §6–§8、§14、§16–§21（全文在 §21） |
| `src/minicodex/tokens.py` | 新增 | §10.1 |
| `src/minicodex/prompts/compaction.md` | 新增 | §13 |
| `src/minicodex/model.py` | 改四处 | §12.1 |
| `src/minicodex/agent.py` | 改动 | §22.1、§22.2 |
| `src/minicodex/__main__.py` | 改三处 | §22.3 |
| `src/minicodex/__init__.py` | 加一个函数 | §13 |
| `tests/test_compaction.py` | 新增 | 分散在各节 |
| `tests/test_properties.py` | 新增 | §23 |
| `probe_naive_cut.py`、`probe_cut_points.py` | 新增 | §3、§4 |
| `probe_tokens.py`、`probe_calibration.py` | 新增 | §9、§10 |
| `probe_summary.py` | 新增 | §15 |
| `probe_mutations.py` | 新增 | §24.2 |
| `.github/workflows/ci.yml` | 加一步 | §23.3 |

### 25.2 提交、推送、PR

这一章边做边提交了七次：

```
feat(compaction): find every legal cut point, and never cut the question
feat(tokens): estimate a request, then correct the estimate from usage
fix(model): ask for usage, and survive the chunk that carries it
feat(compaction): a six-section summary that is shown the goal it must agree with
feat(compaction): plan the cut, summarise, rebuild -- and say so when the summary fails
feat(agent): compact between turns, and calibrate against the uncorrected estimate
fix(compaction): a compaction that would not save anything cuts nothing
```

```bash
git add probe_mutations.py
git commit -m "test: a mutation probe that restores what it touches"
git push -u origin feat/compaction
```

PR 描述里"没做什么"那一段，这一章要写四条：

> - 没有按内容类型估算 token：遇到不是文字的内容，`estimate_messages` **抛异常**，而不是把图片算成免费（F06-10）。
> - `plan(...).fits == False` 只是被记进录像，`Agent` 没有据此做任何事。一步需要的材料比窗口大时，会出现"压缩—重读"的循环，
>   现在只靠轮次上限拦住。
> - 触发线、目标线和 700 token 的摘要预算，没有对着任何东西调过。
> - F06-05 没有复现，没有为它写代码。

### 25.3 自己审一遍

**1 · `Sizer` 只有一种实现，是不是多余的一层？**

不是。它不是为了"以后换实现"而存在的，而是为了**唯一**："所有大小判断用同一把尺子"这条规则，散在几个调用点时已经破过一次（§19.1）。
判断的办法：把它拆回几个独立的函数，那条规则还成立吗？不成立。

**2 · `transport` 是一个只为测试存在的参数。**

是，而且值得（§12.2）：没有它，测试只能测一份抄来的解析逻辑。

**3 · `plan()` 在装不下时返回 `fits=False` 而不是抛异常，调用的人可能忽略它。**

返回是对的——调用的人总得发点什么出去。但现在 `Agent` 确实忽略了它，§22.4 的第二次运行就是后果。**这是一个真的缺口**，写在了 PR 描述里。

**4 · `clip_item` 只处理工具结果，名字听起来像能处理任何东西。**

它确实接收任何类型，对不是工具结果的原样返回，这是故意的：它被用在列表推导里，在外面加类型判断只会让每个调用点都变丑。
`test_F06_09_only_results_are_clipped` 把这个行为钉住了。

**5 · 校准 bug 藏了这么久，说明什么？**

说明"估计值和实际值都写进了录像"这件事救了它——但只有在有人去看的时候。录像里一直有 `estimated_prompt_tokens` 和 `actual_prompt_tokens`，
任何一份多轮的录像都能看出"隔一轮准一次"。**数据一直在，只是没有任何东西——测试或者人——在看它。**

---

## §26 codex 是怎么做的

（以下是对 codex 源码结构的观察；看不到提交历史的地方标了"推断"。）

- **压缩相关的文件不止一个版本并存**：`compact_remote.rs`、`compact_remote_v2.rs`，旁边还有一个带 `_attempt` 的。
  一个一次就写对的模块不会长成这样。**推断**：压缩被推倒重来过不止一次，而旧的路径没敢删。
- **它的压缩是"remote"的**——发生在服务端。这是我们做不到的选择，但动机可以推断：服务端知道真实的 token 数，也知道自己的分词方式，
  不需要 §9–§12 那一套。**这反过来说明了那半章的性质：本地估算 token 是"拿不到真相所以不得不做"的妥协。能拿到真相的一方不会这么做。**
- **它有一个测试文件叫 `compact_resume_fork.rs`**：压缩、恢复、分叉，三件事写在一个文件名里。
  这说明这三件事的**相互作用**本身就是一类故障。这一章只做了第一件。
- **压缩用的提示词也是一个独立的 md 文件**，和我们一样：它是会被当成文章来读、来改的东西。

---

## §27 回头看：这一章撞到了什么

**预测到了，并且成立的：** F06-02、F06-03（切点落在结果上）、F06-04（摘要丢信息）、F06-06（问题被压掉——比猜的更糟，因为是静默的）、
F06-07（估算偏差——比猜的更糟）。

**预测到了，动工前就挡住的：** F06-08、F06-09、F06-11、F06-12。

**预测了，但没按预测出现的：** F06-01（碰巧合法）、F06-05（没复现）、F06-13（不是逐代衰减）。**没实现、改成大声拒绝的：** F06-10。

**没预测到的：**

| 故障 | 怎么发现的 | 挡住它的东西 |
|---|---|---|
| **切法合法，但答的是另一个问题** | 🟡 静默（读了答案才发现） | 受保护的前缀 |
| 按类型保护系统消息，会让过期的轮次警告永远堆着 | 🟣 设计时 | 按位置，不按类型 |
| **工具 schema 一个 token 都没算** | 🟠 加 `--with-tools` 才看见 | 计入 schema 和每条消息的开销 |
| **流式请求默认不返回用量，校准数据从未到达** | 🟡 静默（机制正确，但从没运行过） | `stream_options.include_usage` |
| 打开用量后 `choices` 为空，`IndexError` | 🔴 崩溃（回答已经流完之后） | 先取用量，再跳过 |
| **摘要模型没看到目标，于是编了一个** | 🔵 真模型 | `SummaryRequest.context` |
| 指标量的是摘要文字，把受保护的事实算成"丢了" | 🟠 结果说不通 | 在压缩后的整个历史里找 |
| 空摘要被当成成功 | 🟢 边界测试 | 明确判断 |
| **留位置和裁摘要用了两把尺子** | 🟣 写第二处时 | `Sizer` |
| 代数读成了标题里的最后一个数字 | 🔴 测试红 | 锚定在 `generation` 这个词上 |
| **压缩把 89 个 token 变成 93 个，会形成压缩循环** | ⚪ 属性测试，种子 235 | 不省就不切 |
| 而种子 235 不在默认的 200 个里 | ⚪ 变异测试 | 补一个写死输入的测试 |
| 例子测试走错了分支 | ⚪ 变异测试 | 搜参数，不靠推理 |
| 测试抄了一份解析循环，测的是副本 | ⚪ 变异测试 | `MockTransport`，走真代码 |
| `PER_MESSAGE_TOKENS` 没有任何测试依赖 | ⚪ 变异测试 | 断言"拆得越碎越贵" |
| 变异脚本被打断，把变异留在了源码里 | ⚫ 自己踩到 | 快照 + 退出时还原 |
| **校准比例隔一轮跳回 1.0** | 🟠 改写本章时，把中间的数打印出来 | 用没修正过的估计去校准 |
| **摘要探针量的是一次没发生的压缩** | 🟠 改写本章时，重跑探针 | 加长会话；没压缩就报错 |
| **窗口装不下一步的材料时，压缩和模型形成循环** | 🔵 改写本章时，真跑 | **没解决**；只有轮次上限 |

标记：🔴 崩溃 · 🟡 静默 · 🟢 边界测试 · 🔵 长跑/真模型 · 🟠 看日志 · 🟣 审查 · ⚫ 用户报告 · ⚪ 工具

崩溃只有两条。而没预测到的这一大半里，**出在验证手段本身的比出在压缩逻辑里的还多**：测试测了副本、测试走错分支、
属性测试的默认规模不够、没人依赖的常量、变异脚本改坏源码、探针量了没发生的事。

> 第 5 章的结论是：一个用来检查别的东西的模块，最容易以为自己检查过了。这一章往前推了一层：
> **检查"那个检查器"的工具，同样会以为自己检查过了。**
>
> 每加一层验证，就多一层"验证本身失效"的可能。出路不是无限地再加一层，而是**让每一层失效的方式互不相同**——
> 属性测试的失效方式（规模不够）和变异测试的失效方式（测了副本）不一样，所以它们各自抓到了对方漏掉的东西；
> 而最后三条，是靠最朴素的办法抓到的：**重新跑一遍，把数打印出来，自己看。**

---

## 如果你只记住三件事

1. **报错的那一半，是容易的那一半。**
   同一段历史，切在结果上是 400，切在调用上是 200——而 200 里有一个答的是另一个问题。
   **每次写一个"删除"的操作，问一遍：删掉之后，剩下的东西还知道自己是干什么的吗？**

2. **拿不到真相的时候，别调参数，去找真相。**
   字符数除以 4 对散文准、对 JSON 差一倍多——没有任何常数是对的。正确的答案是**服务端每次都在告诉你实际用了多少**。
   而这个数据默认不发，得明确去要；要到了，还得确认你喂给校准器的是对的那个数。
   **"我没有这个数据"和"我没去要这个数据"，经常是同一件事。**

3. **一个报告"没问题"的工具，先问它：你确定你量到东西了吗？**
   校准器在没有数据时安静地保持 1.0；测试测着自己抄的副本；探针报告一次没发生的压缩里"所有事实都在"。
   **它们都没有撒谎，它们只是什么都没量到。**

---

## 动手练习

1. 把 `boundaries()` 换成 §6.1 那个短的版本，跑全部测试。**全绿。** 然后想清楚为什么：在什么样的输入上这两个版本才会不一样？
   `History` 为什么不可能产生那样的输入？（提示：第 1 章的 `add_assistant`。）

2. 把 `model.py` 里 `stream_options` 那两行删掉，跑 `uv run pytest`，看哪一个测试红。然后想象没有那个测试：
   用真的 key 跑一次 `minicodex ask ... --context-window 8000`，**不会有任何报错**，只是 `[tokens: ...]` 那一行会写着 `uncalibrated`。

3. 把 `plan()` 里两处 `_do_nothing` 的保护删掉，先跑默认的 `uv run pytest tests/test_properties.py`（全绿），
   再跑 `MINICODEX_PROPERTY_CASES=5000`。记下第一个失败的种子，然后**写一个写死输入的例子测试复现它**。

4. 把 §22.2 的修复撤掉（把 `self._raw_estimate(messages)` 改回 `estimated`），照那一节的办法写一个"永远多收一半"的假模型，
   把每一轮的比例打印出来。**亲眼看一次 1.50、1.00、1.50、1.00。**

5. 想一想 §22.4 那个"压缩—重读"的循环该怎么拦。至少有三个方向：`fits=False` 时直接告诉模型"这一步的材料装不下"；
   发现连续几次压缩丢掉的都是同样的工具调用时停下来；或者读文件的工具支持只读一部分。
   **每个方向各写出它会引入的一个新问题。**

下一章讲中断与恢复：进程没了，历史怎么回来。而这一章刚刚量过——摘要是有损的，所以写到磁盘上的必须是原文，不是摘要。
