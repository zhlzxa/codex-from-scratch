# 第 1 章 · 先定协议，再写逻辑

> **代码**：`steps/step01_protocol/`
> **分支**：`feat/protocol-layer`
> **产出**：同一个 Agent，能对着两家形状完全不同的供应商跑
> **你需要**：Ollama（同前）。有 OpenAI key 更好，几分钱；没有也能跟——两家的响应都录好了。

---

## §1 这一章要做出来的东西

一条命令，多一个参数：

```bash
minicodex ask "What does src/minicodex/__init__.py define?" --provider openai
```

听起来只是换个 `base_url`。**实际上 Ch00 的代码会当场废掉**，而且不是崩溃，是**默默产出
垃圾**。

这一章要处理三件在真机上量出来的事，每一件都推翻了 Ch00 的一个假设。

---

## §2 先把目标翻译成待办

老方法：把目标拆开问"这需要什么"，然后追问两遍。

| 目标里的词 | 追问 | 需要做的事 |
|---|---|---|
| 换一家供应商 | 除了 URL 还有什么不一样？ | **不知道，得先测** |
| 同一个 Agent | 循环、工具、历史都不用改？ | 待验证 |
| （追问）我怎么验证它真的两家都对？ | 跑通了就算对吗？ | 要能对比两家产出的**内部形状** |
| （追问）换第三家会怎样？ | ？ | 先别管，两家还没跑通 |

第一行就是空的。**这一章的第一件事不是写代码，是去量。**

> **Ch00 结尾留了个悬念**：那一章的 code review 里，第 1 条意见是
> "如果供应商真的分片传参数，`by_index[...] = event` 这个覆盖会静默丢数据"。
> 我当时的回答是"没观测到，第 1 章接第二家时先测再写"。
>
> **这一章就是来兑现那句话的。** 一条被延后的 review 意见，到期了。

---

## §3 先去量

不读文档，直接问两边同一个问题，把原始流打出来。用的还是 Ch00 §3 那三十行，只改
`base_url` 和 key。

### 3.1 Ollama 怎么给一个工具调用

Ch00 已经看过了，一个 chunk 给完：

```json
"tool_calls":[{"id":"call_yfo64477","index":0,"type":"function",
               "function":{"name":"read_file",
                           "arguments":"{\"path\":\"src/minicodex/__init__.py\"}"}}]
```

### 3.2 OpenAI 怎么给同一个工具调用

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

**十四个 chunk。而且只有第一个带 `id` 和 `name`，后面十三个只有 `index` 和一片参数。**

拼起来正好是 `{"path":"src/minicodex/__init__.py"}`。

顺带两个小差异，也是量出来的：

- 工具调用那个 chunk 里，Ollama 的 `content` 是 `""`，**OpenAI 的是 `null`**。
  Ch00 用的是 `if delta.get("content"):`（真值判断），两个都能过——**这次是运气**，
  如果当时写的是 `if "content" in delta:`，OpenAI 这边就会往文本里塞一个 `None`。
- OpenAI 的 chunk 里还有 `refusal`、`service_tier`、`obfuscation`、`logprobs` 几个
  Ollama 没有的字段。我们全都不看，这没问题——**多出来的字段可以忽略，少掉的字段不行。**

### 3.3 拿真实分片喂 Ch00 的代码

Ch00 的 `_collect` 里那一行：

```python
by_index[event.index] = event      # 覆盖
```

十四片全是 `index=0`，所以留下的是**最后一片**。

```
$ 把 OpenAI 的十四片真实分片喂给 Ch00 的 _collect
name           = ''
call_id        = 'call_0'
raw_arguments  = '"}'
arguments      = None
```

工具名没了，参数没了，`call_id` 是兜底编的。

**而它不崩。** `_run_tool` 会走"没有叫 `''` 的工具"那条分支，返回一条错误给模型，
Agent 继续跑，最后产出一段基于错误信息瞎编的答案。

> 这就是 Ch00 那条 review 意见说的"静默丢数据"。当时我写的回复是：
>
> > 真实风险，不在这里修。这个供应商观测到的行为是一次给完，为一个没见过的行为写累积
> > 逻辑就是写死代码去伺候想象。第 1 章要接第二家，那时会先测再写。
>
> **回头看，这个决定是对的**——不是因为运气，而是因为它附带了一个可检查的到期时间。
> 如果当时凭想象写了累积逻辑，写出来的多半是"每片都带 id"那种版本，照样接不上真实的
> OpenAI。**猜一个没见过的形状，猜对的概率不比不猜高。**

### 3.4 第二件事：历史的方言不一样

Ch00 的 `history` 是这么攒的：

```python
history.append({
    "role": "assistant",
    "content": turn.text,
    "tool_calls": [{"id": c.call_id, "type": "function",
                    "function": {"name": c.name, "arguments": c.raw_arguments}}],
})
```

**这是照抄请求格式的**——Ch00 §5.4 那条注释已经预告过："协议渗透进了数据结构。"

那这份历史发给别人会怎样？拿 Ollama 的原生 API（`/api/chat`）试：

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

两个方向都是 400，**两条报错都毫无帮助**——一条在说 JSON 括号，一条在漏 Go 的内部结构。

差异其实只有两处，但足够致命：

| | `/v1/chat/completions` | Ollama `/api/chat` |
|---|---|---|
| `arguments` | JSON **字符串** `'{"path":"a.py"}'` | **对象** `{"path": "a.py"}` |
| 结果怎么配对 | `tool_call_id` | `tool_name` |

### 3.5 第三件事，也是最要命的：只有一部分服务端会检查

Ch00 §8.1 说过，丢掉一个工具调用会让历史里出现"没人应答的调用"。那具体会怎样？

**故意构造一个有 `tool_calls` 却没有对应结果的历史：**

```
Ollama:   HTTP 200
          data: {..."content":" symbiotic"...}
          data: {..."content":" with"...}
          data: {..."content":" the"...}
          data: {..."content":" user"...}
          ...

OpenAI:   HTTP 400
          "An assistant message with 'tool_calls' must be followed by tool messages
           responding to each 'tool_call_id'. The following tool_call_ids did not
           have response messages: call_probe01"
```

**Ollama 返回 200，然后模型开始胡言乱语**（"symbiotic with the user's request"）。

再试两个变体：

| 坏历史 | Ollama | OpenAI |
|---|---|---|
| 有调用没结果 | 200 + 胡言乱语 | 400 |
| 同一个 id 两条结果 | 200 | 400 |
| 结果的 id 对不上任何调用 | 200 | 400 |

**三个场景，Ollama 全放行，OpenAI 全拒绝。**

这个组合是本章最重要的发现，因为它描述的是一种非常具体的开发方式失败：

> **你对着宽松的那家开发，一路绿灯；上线换成严格的那家，满屏 400。**
> 不变量从头到尾都是坏的，只是宽松的那一家从来不告诉你。

而且注意 Ollama 那个 200 的性质——它不是"能用"，是**产出了垃圾**。历史坏了，服务端不
管，模型自己乱编。🟡 静默错误的教科书样本。

---

## §4 三件事，三个决定

量完了，现在才轮到设计。三件事各自逼出一个决定：

| 量到的 | 决定 |
|---|---|
| 参数分片，且只有第一片带 id/name | **在客户端内部攒完再交出去**，调用方只见一种形状 |
| 两家的历史方言不同 | **历史不能存 wire 格式**，得存"事实"，发送时才翻译 |
| 只有一部分服务端检查不变量 | **规则必须自己守**，不能指望服务端告诉你 |

第一条和第二、三条的层次不同，值得说清楚：

**第一条是"归一化"**——把两种形状变成一种。**第二、三条是"分层"**——把"我记住了什么"和
"我发出去什么"拆开。

> **Ch-1 那三条抽象例外，这一章一次性用上了两条：**
> 第二条（**跨越信任边界**）催生了归一化——两家供应商的分歧到客户端为止；
> 第三条（**不变量需要强制**）催生了 `History`——因为服务端不替你守。
>
> 而 Ch00 里我明确说了第三条"现在还不该抽，只有一处代码维护它"。现在是第二处了
> （加上"发送前校验"），而且有了一个**服务端不管**的硬理由。**条件变了，答案就变了。**

---

## §5 归一化：分歧到客户端为止

先解决分片。

**关键决定是：在哪儿攒？** 两个选择——

| 方案 | 后果 |
|---|---|
| 在 `_collect`（agent.py）里攒 | 供应商的怪癖泄进了循环。将来第三家有第三种怪癖，`_collect` 就会长成一堆 if |
| **在 `stream()`（model.py）里攒** | 怪癖到此为止。上面的代码永远只见一种 `ToolCallDelta` |

选第二个。这就是"协议层"这个词的实际含义：**不是多一个类，是划一条线，让脏东西过不去。**

代价是 `stream()` 不能再一见到 `tool_calls` 就吐出去了——**它得等到 `[DONE]` 才知道一个
调用收完了**。文本仍然是边收边吐（打字机效果不受影响），工具调用改成最后一次性吐出。

先定一个私有的"半成品"类型：

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

**为什么是 `@dataclass` 而不是 `frozen=True`。** §4 那三个事件类型都冻结了，因为它们是
"已经发生的事实"。这个不是——它是**正在被组装的中间态**，存在的全部意义就是被反复改。
名字前面加 `_` 表示私有，docstring 里写明"外面永远见不到它"。

**为什么每个字段都写成条件赋值。** 这是整章最关键的三行。`if raw.get("id")` 而不是
`self.call_id = raw.get("id")`——因为后面十三片的 `id` 是**缺失**的，直接赋值会把第一片
拿到的 id 覆盖成 `None`。

**这正是 Ch00 那个 bug 的形状**：Ch00 是整个对象覆盖，这里如果写成无条件赋值，就是逐个
字段覆盖。**同一个错误的两种写法。**

`parts.append(fn.get("arguments") or "")` 里那个 `or ""` 也不是多余：第一片的
`arguments` 就是空字符串 `""`，`.get()` 返回它，`or ""` 让它安全地进列表。

然后是 `stream()` 里的改动：

```python
        pending: dict[int, _PartialCall] = {}

        ...
                    if payload == "[DONE]":
                        # Tool calls are emitted here, not as they arrive: only
                        # now is every fragment known to have been received.
                        for index in sorted(pending):
                            yield pending[index].finish()
                        yield Completed(finish_reason)
                        return
        ...
                    for raw in delta.get("tool_calls") or []:
                        index = raw.get("index", 0)
                        pending.setdefault(index, _PartialCall(index)).absorb(raw)
```

`setdefault` 那一行：**没见过这个 index 就新建一个，见过就拿出来吸收。** 三行代码同时
处理了"一片给完"和"十四片给完"两种情况——**因为一片也是"第一片"。**

> **注意 Ch00 里 `_collect` 完全没动。** 它仍然写着 `by_index[event.index] = event`，
> 而现在这个覆盖是安全的，因为客户端保证了每个 index 只吐一次。
>
> 这是分层的直接好处：**改动被关在一个模块里。** 如果当初在 `_collect` 里攒，这次改动
> 就会同时动 `model.py` 和 `agent.py`。

跑一下，两家的产出：

```
[narrate ] name='read_file' args={'path': 'src/minicodex/__init__.py'}
[openai  ] name='read_file' args={'path': 'src/minicodex/__init__.py'}
```

**一个 chunk 和十四个 chunk，产出一模一样。** 上面的代码不知道、也不需要知道区别。

对应的测试：

```python
async def test_F01_01_both_providers_produce_the_same_tool_call(stub_url: str) -> None:
    """The whole point of normalising at the boundary: above `model.py`, the two
    recordings are indistinguishable."""

    async def call_from(mode: str) -> ToolCallDelta:
        events = [...]
        return next(e for e in events if isinstance(e, ToolCallDelta))

    ollama = await call_from("narrate")
    openai = await call_from("openai")

    assert ollama.name == openai.name == "read_file"
    assert json.loads(ollama.arguments) == json.loads(openai.arguments)
```

注意最后一行用的是 `json.loads(...) == json.loads(...)` 而不是直接比字符串——因为两家的
**空白和转义可能不同**，我关心的是"解析出来是同一个东西"，不是"字节完全相同"。

---

## §6 历史：存事实，不存 JSON

分片解决了，但 §3.4 和 §3.5 那两条还在。

### 6.1 先想清楚要存什么

Ch00 存的是 dict，因为下一轮要原样发回去。这个理由在只有一家供应商时成立，现在不成立
了——**同一段对话，两家要的形状不一样。**

那存什么？**存"发生了什么"，而不是"该怎么发出去"。**

一次对话里只会发生四种事：

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
```

和 §4 那三个事件类型是同一个套路：**一件事一个类型，非法状态无法表示。**

`SystemNote` 值得单说。Ch00 里那条轮次预算提示是这么写的：

```python
history.append({"role": "system", "content": "You have 1 turn(s) left..."})
```

`role: "system"` 是**wire 上的角色名**。但它在概念上是什么？是**代码知道而模型不知道的
状态**——Ch00 §8.5 已经点出这个模式了，说第 5、6、11 章还会遇到。

给它一个自己的类型，是因为**将来它们的渲染方式可能不一样**：有的供应商没有 `system`
角色，得塞进 user 消息里；有的要求 system 必须在最前面。**现在只是一行 `role: system`，
但概念上它和用户说的话是两回事。**

`ToolResult` 里为什么有 `name`？发给 `/v1` 时用不上（那边用 `call_id`），但发给 Ollama
原生 API 时**必须有**（那边用 `tool_name`）。**一个字段只被一种方言用，这正是"存事实"
的意思**：事实是"read_file 这个工具返回了这段内容"，至于怎么配对是方言的事。

### 6.2 然后是那个不会进入错误状态的容器

四个类型只是"能表示"，不能"防止"。§3.5 那三种坏历史用这四个类型照样能拼出来。

所以再要一个容器，**它的职责就是拒绝**：

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
```

**核心是 `_unanswered` 这个字典。** 它是"还欠着几笔账"。三个写入方法围着它转：

```python
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
```

逐条对应 §3.5 量到的三种坏历史：

| §3.5 的坏历史 | 谁拦住它 |
|---|---|
| 有调用没结果 | `to_wire()` 拒绝渲染（见下） |
| 同一个 id 两条结果 | `add_tool_result` 里 `pop` 之后再来一次就是 `None` |
| 结果 id 对不上任何调用 | 同上，而且错误信息会说**它在等哪些 id** |

加上一条**服务端也管不了**的：模型还欠着账，代码却又追加了一条 assistant 消息。
`add_assistant` 开头那个检查拦的就是这个——它对应的是"循环写错了，某一轮忘了跑工具"。

**`pop` 这个写法值得注意。** 它一步完成了"检查存在"和"标记已答"：

```python
call = self._unanswered.pop(call_id, None)
if call is None:
    raise ...
```

而不是先 `if call_id not in self._unanswered: raise` 再 `del`。**两步写法有个窗口**——
将来如果这里变成并发的（第 8 章），两个协程可能同时通过检查。`pop` 是原子的。
现在还不并发，但这个写法不比两步贵。

### 6.3 校验放在哪：`to_wire()`

```python
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

**为什么校验放在渲染里，而不是放在循环里？**

因为循环不止一条路径。今天只有 `run()` 会发请求；第 6 章会有压缩后重发，第 7 章会有
崩溃恢复后重发，第 10 章会有子 Agent 发。**每条路径都可能忘记校验一次，但每条路径都
必须经过 `to_wire()`。**

> **一条通用的判断**：把检查放在**所有路径的交汇点**，不是放在你今天想得到的那条路径上。
> 这个交汇点通常就是"数据离开你的控制范围的那一刻"。

错误信息里带上工具名（`call_1 (read_file)`）而不只是 id，因为 `call_bqv6MLMr9BhXis7T4LB6tGqa`
这种 id 你看不出是什么。这是 Ch00 那条"错误信息就是 prompt"的同一个道理，只不过这次
读者是人。

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

**为什么是一个模块级函数而不是每个类型上的 `to_wire()` 方法？**

因为方言不属于消息。`UserMessage` 是"用户说了这句话"，它不该知道 Ollama 原生 API 长什么
样。**把渲染放在外面，四个数据类型就保持了对协议的无知**——将来加第三种方言，改一个函数，
四个类型一行不动。

（这是访问者模式的最朴素形态。不需要给它起名字。）

**`c.arguments or {}` 那一行是 `raw_arguments` 的第二次兑现。** Ch00 里留这个字段是为了
错误信息回显；现在它还负责 `/v1` 方言的渲染——**发回去的必须是模型原本发过来的字符串**，
而不是我们解析后再序列化的版本（那样空白和键顺序都可能变，某些供应商会因此认不出）。

一个 `ToolCall` 同时带着"解析好的"和"原样的"两份，这在 Ch00 看着像冗余，现在是两个不同
用途各用一份。

最后这行也值得一提：

```python
raise AssertionError(f"unrenderable history item: {item!r}")  # pragma: no cover
```

它永远不该被执行。留着是因为**将来有人加了第五种 `HistoryItem` 却忘了在这里加分支**——
那时候它会立刻炸，而不是静默返回 `None` 然后在 HTTP 层报一个莫名其妙的错。

### 6.5 循环怎么改

`run()` 的改动比想象的小：

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

三处 `history.append({...})` 变成了三个有名字的方法。**代码量差不多，但现在拼错一个键
名是不可能的**——Ch00 里 `"tool_call_id"` 写成 `"tool_id"` 会一路跑到服务端才炸。

`recorder.record` 那行的 key 从 `"history"` 改成了 `"messages"`，因为记的确实是渲染后
的消息，不是历史本身。**名字要说实话。**

---

## §7 一个真的循环依赖，和最便宜的解法

写到这儿撞了个错：

```
ImportError: cannot import name 'ToolCall' from partially initialized module
'minicodex.agent' (most likely due to a circular import)
```

`history.py` 需要 `ToolCall`（它在 `agent.py` 里），而 `agent.py` 需要 `History`。
**两个模块互相 import。**

三个常见解法：

| 解法 | 评价 |
|---|---|
| 在函数内部 import | 能跑，但把问题藏起来了，而且每次调用都有开销 |
| `TYPE_CHECKING` 块 + 字符串注解 | 只解决类型注解的循环，这里是运行时真的要用 |
| **把共享的东西往下移** | 新建一个谁都不依赖的模块 |

选第三个：

```python
"""Types shared by the agent and the history.

Extracted from `agent.py` for one reason only: `history.py` needs `ToolCall`,
and `agent.py` needs `History`.  Leaving `ToolCall` where it was would make the
two modules import each other.

This is the smallest possible answer to a circular import -- move the shared
thing down, not sideways.  Interlude B deals with a case where the answer is
not this easy.
"""
```

`agent_types.py`，31 行，只有一个 `ToolCall`。

> **循环依赖是个信号，不是个麻烦。** 它在说"这两个模块共享了某个概念，而这个概念没有
> 自己的位置"。找到那个概念，给它一个不依赖任何人的家，循环自然消失。
>
> **只有当共享的不是"一个类型"而是"一段逻辑"时，才需要依赖倒置那种重武器**——那是
> Interlude B 的内容。这里不需要，所以不用。

### 7.1 把边界写成测试

Ch-1 里说过一句话：**口头约定的架构规则 100% 会烂掉**，并且举了 codex 那个
`verify_tui_core_boundary.py` 的例子——一个 CI 脚本，专门验证 tui 不许直接 import core。

这一章第一次有了值得这么保护的边界，而且是两条：

**第一条：`agent.py` 不许知道自己在跟谁说话。**

这正是 §5 那个决定的可执行版本。如果哪天有人图省事，在循环里写一句
`if provider == "openai": ...`，归一化就白做了——而且**不会有任何测试变红**，因为功能
还是对的。

```python
def test_F01_05_the_agent_does_not_import_an_http_client() -> None:
    """`agent.py` talks to a `Model`, never to a socket.

    If this ever fails, some provider-specific handling has leaked upward and
    the normalisation in `model.py` is no longer doing its job.
    """
    assert "httpx" not in imported_modules(SRC / "agent.py")


@pytest.mark.parametrize("module", ["agent.py", "history.py", "agent_types.py"])
def test_F01_05_no_provider_names_above_the_client(module: str) -> None:
    text = (SRC / module).read_text(encoding="utf-8")
    code = "\n".join(line for line in text.splitlines() if not line.strip().startswith("#"))
    for banned in ("api.openai.com", "localhost:11434", "OPENAI_API_KEY"):
        assert banned not in code, f"{module} should not know about {banned}"
```

`imported_modules` 用 `ast.parse` 而不是正则——因为 `# import httpx` 这样的注释不该算
违规，而正则分不清。**检查代码的工具要用解析器，不要用字符串匹配。**（那行过滤注释的
`code = ...` 是同一个理由的补丁：docstring 里提到 `api.openai.com` 是合法的。）

**第二条：`agent_types.py` 必须保持是叶子。**

它存在的唯一理由是打断循环。**它一旦 import 了 `agent` 或 `history`，就不再是解法，
而是循环的第三个环节。**

```python
def test_F01_08_agent_types_depends_on_nothing_of_ours() -> None:
    """The shared module is only a solution while it stays a leaf."""
    ours = {m for m in imported_modules(SRC / "agent_types.py") if m.startswith("minicodex")}
    assert ours == set(), f"agent_types must stay a leaf, but imports {ours}"
```

还有一条测循环本身的，写法有个坑：

```python
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
```

**为什么必须开子进程。** 循环 import 只在**第一次**导入时炸；模块进了 `sys.modules`
之后再 import 就是直接取缓存。而这个文件里前面的测试早就把两个模块都导入过了——
**在同一个进程里写这个测试，它对着一个真正循环的代码库也会绿。**

> 这是"一个对着坏代码也能通过的测试是装饰品"的又一个实例，而且这次的陷阱藏得很深：
> 测试逻辑完全正确，错的是**运行环境**。

---

## §8 验证

### 8.1 变异

老规矩，把每个修复改回去：

| 改回去 | 抓住它的测试 |
|---|---|
| `if raw.get("id"):` → 无条件赋值 | `test_F01_01_openai_fragments_are_reassembled` |
| `if fn.get("name"):` → 无条件赋值 | `test_F01_01_a_fragmented_call_runs_the_right_tool` |
| `to_wire` 里的欠账检查 → `if False` | `test_F01_02_the_error_names_every_unanswered_call` |
| `add_tool_result` 的 `pop` 检查失效 | `test_F01_06_a_result_for_an_unknown_id_is_refused` |
| 原生方言渲染 → 渲染成 `/v1` | `test_F01_04_ollama_native_dialect` |
| `agent.py` 里 `import httpx` | `test_F01_05_the_agent_does_not_import_an_http_client` |
| `agent_types.py` 里 import `history` | **整个套件 collection error** |

七个变异，六次红加一次更彻底的失败。

最后一条值得说：把 `ToolCall` 搬回去制造循环 import 之后，pytest 没有"某个测试红了"，
而是 `Interrupted: 2 errors during collection`——**测试根本收集不起来**。

循环 import 坏得太早，早到测试框架都启动不了。这也是为什么那个测试必须开子进程：
在进程里它根本没机会跑。

**注意前两个是分开的。** 我本来只想测一个"分片能拼对"，但 id 和 name 是两条独立的
条件赋值——只测一个，另一个写错了照样绿。**一个变异对应一条逻辑，不是对应一个函数。**

### 8.2 全量

```
$ uv run pytest
.............................................................            [100%]
61 passed in 3.26s
```

Ch00 是 39 个，这一章加了 22 个。

### 8.3 端到端

```
$ minicodex serve-stub &
$ 同一个 Agent，两种录音

[narrate ] name='read_file' args={'path': 'src/minicodex/__init__.py'}
           `src/minicodex/__init__.py` defines the following: ...

[openai  ] name='read_file' args={'path': 'src/minicodex/__init__.py'}
           It defines `__version__` and `system_prompt()`.
```

有 key 的话，最后一行可以是真的：

```bash
minicodex ask "What does src/minicodex/__init__.py define?" --provider openai
```

`--provider openai` 从**环境变量**读 key，不从命令行参数读：

```python
        # Read from the environment, never from a flag: a key in argv shows up
        # in shell history and in `ps`.
        api_key=os.environ.get("OPENAI_API_KEY") if provider == "openai" else None,
```

---

## §9 这一章一共写了什么

| 文件 | 行数 | 完整代码在 |
|---|---|---|
| `src/minicodex/model.py` | 191 | §5（`_PartialCall` 和 `stream()` 的改动；其余同 Ch00 §4.2） |
| `src/minicodex/history.py` | 178 | §6.1–6.4 |
| `src/minicodex/agent_types.py` | 31 | §7 |
| `src/minicodex/agent.py` | 212 | §6.5 给了 `run()` 的改动，其余同 Ch00 |
| `src/minicodex/stub.py` | 273 | §3.2 给了 OpenAI 录音的形状；包装函数见仓库 |
| `tests/test_history.py` | 158 | §6.2、§8.1 引了关键的几个，全部见仓库 |
| `tests/test_boundaries.py` | 88 | §7.1 全给了 |

**`agent.py` 只改了 `run()` 的正文和三行 import。** `_collect` 和 `_run_tool` 一行没动。

这不是运气，是 §5 那个决定的直接结果：**归一化放在客户端，改动就关在客户端。**

---

## §10 交给 git

```
c19c472 fix: reassemble tool-call fragments before emitting them
bed3068 feat: store the conversation as facts rather than as one provider's JSON
aeeee2a feat: select a provider on the command line
```

**第一条是 `fix` 不是 `feat`。** 因为它修的是 Ch00 就存在的一个 bug——只是当时没有第二家
供应商，所以没人知道。**"发布时没暴露"不等于"当时不是 bug"。**

它的 message 里有一句专门指回 Ch00：

```
This is the review comment deferred in chapter 0 ("if a provider really
fragments arguments, the overwrite silently loses data"), now with a
measurement instead of a guess.
```

**一条被延后的 review 意见，兑现时要回指。** 这样 `git log` 里能看出那次延后是负责任的
决定，而不是忘了。

第二条的 body 直接引了两条实测：

```
Only some servers check the invariants. A history with an unanswered tool call
got HTTP 200 from Ollama -- which then answered "symbiotic with the user's
request" -- and HTTP 400 from OpenAI.
```

**把"我量到了什么"写进 message**，比写"这个设计更健壮"有用得多。

第三条承认了一次删除：

```
One test was removed outright. It built an Agent, never used it, and asserted
something tests/test_history.py already covers; ruff's F841 is what noticed.
A test that needs scaffolding to prove nothing is worse than no test.
```

> 这条真的发生了。我写了个 `test_F01_03_the_loop_cannot_send_an_unanswered_call`，
> 里面 `ForgetfulAgent` 建好了却没用，实际只在测 `History`。**是 lint 发现的，不是我。**
> `F841 Local variable 'agent' is assigned to but never used`。

---

## §11 Code review

**1 · 正确性** — `_PartialCall.finish()` 里 `call_id or f"call_{self.index}"`。如果供应商
真的不给 id，这个编出来的 id 会被当真发回去，服务端可能认不出。

> **作者**：是，而且没有更好的办法。不给 id 的响应本来就无法正确配对——编一个至少让
> **本地的**不变量能被满足，从而让 `History` 的检查有意义。如果服务端拒绝，那是可见的
> 400，比本地静默乱套强。docstring 里写明了它是"provider bug 的兜底，不是要支持的形状"。

**结局：接受，但把理由写进代码。**

---

**2 · 边界情况** — `to_wire()` 每次调用都重新渲染整个历史。第 30 轮时这是 O(n) 的重复
工作，每轮都做一遍。

> **作者**：真的，而且现在无所谓——量一下，30 条消息渲染大约 0.1ms，而一次模型调用是
> 秒级。**差四个数量级。** 第 6 章上下文压缩会让历史变复杂，那时候如果 profile 显示它
> 上榜了再说。**现在优化它就是拿可读性换一个测不出来的收益。**

**结局：拒绝，附数量级。**

---

**3 · 可测试性** — `History._items` 和 `_unanswered` 都是私有的，测试只能通过公开方法
间接观察。想断言"第 3 条是不是 ToolResult"就得遍历 `items`。

> **作者**：这是故意的，而且 `items` 属性已经开了只读的口子（返回 tuple）。**如果测试
> 需要改内部状态才能构造场景，那说明这个场景在真实使用中也构造不出来**——那种测试测的是
> 我的实现，不是我的契约。

---

**4 · 命名** — `Dialect` 这个 `Literal["chat_completions", "ollama_native"]` 是字符串
字面量，不是枚举。拼错 `"chat_completion"`（少个 s）只有类型检查器会发现，运行时会静默
走到 else 分支。

> **作者**：接受一半。改成 `Enum` 确实更安全，但 `Literal` 让调用点保持
> `to_wire("ollama_native")` 而不是 `to_wire(Dialect.OLLAMA_NATIVE)`，可读性更好。
> **真正的问题不是字面量，是 `_render` 里用了 `if dialect == "..." else`——else 吞掉了
> 一切拼错。** 改法是显式列出所有分支、末尾 raise。第三种方言出现时会一起改。

**结局：问题被重新定位了。** reviewer 指出了症状（字符串易拼错），作者找到了病灶
（else 分支吞错）。**这比直接接受或直接拒绝都有价值。**

---

**5 · 风格** — `agent_types.py` 只有一个类，31 行里 20 行是 docstring。

> **作者**：是。而那 20 行 docstring 正是它存在的全部理由——**一个只有一个类的模块，
> 如果不解释为什么单独存在，下一个人一定会把它合并回去。**

---

### 这次 review 做了、上次没做的事

上一章我说"不要提一条要求对方去猜的意见"。这一章第 2 条（性能）恰好相反——**它要求了
一个数字**，而作者给出了数字。

区别在于：**"这里会不会太慢"是个可以测量的问题，"边界应该划在哪"不是。** 前者值得提，
后者要等观察。

---

## §12 本章给 CI 加了什么

**什么都没加。**

理由和 Ch00 一样：新增的保护全在测试里，现有的 `Test` 步骤自动跑到。

**但这一章第一次出现了"CI 跑不到的测试"**：所有对着真 OpenAI 的验证。它们需要 key，
而 CI 里不该放 key。

处理办法是**桩服务里的录音**——CI 跑的是 2026-08-06 那次真实响应的逐字节回放。代价写在
Ch00 §7.2 里了：**录音会过期**。第 14 章会讲怎么定期重录来对冲。

---

## §13 回头看：这一章撞到了什么

| 编号 | 故障 | 怎么暴露的 | 挡住它的东西 |
|---|---|---|---|
| F01-01 | **参数分片，覆盖后只剩最后一片，且不崩** | 🟡 静默 | 客户端内部缓冲，`[DONE]` 时才交出 |
| F01-02 | 有调用没结果：一家 200 胡言乱语，一家 400 | 🟡 / 🔴 | `to_wire()` 拒绝渲染 |
| F01-03 | 循环有多条发送路径，每条都可能忘记校验 | 🟣 review | 校验放在所有路径的交汇点 |
| F01-04 | 历史存的是一家的方言，换一家全是 400 | 🔴 崩溃 | 存事实，发送时才翻译 |
| F01-05 | 换供应商要改一堆代码；或者有人图省事把 `if provider ==` 写进循环 | 🟣 review | 归一化在客户端 + **把边界写成测试** |
| F01-06 | 同一个 id 两条结果 / 结果 id 对不上 | 🟡 / 🔴 | `pop` 一步完成检查与标记 |
| F01-07 | 系统提示和用户消息混成同一种东西 | 🟣 review | `SystemNote` 独立类型 |
| F01-08 | 循环 import（坏到测试都收集不起来） | 🔴 崩溃 | 共享类型下沉，并测试它保持是叶子 |

标记：🔴 崩溃 · 🟡 静默 · 🟢 边界测试 · 🔵 长跑 · 🟠 看日志 · 🟣 review · ⚫ 用户报告 ·
⚪ lint/类型

**这一章有三条是"同一个 bug，两种命运"**——F01-02、F01-06 在 Ollama 上是 🟡，在 OpenAI
上是 🔴。同一份历史，一家静默产出垃圾，一家当场拒绝。

> **这个现象值得单独记住**：你的开发环境越宽松，你的 bug 就越晚被发现。
> 而"晚"通常意味着"在用户那里"。

---

## §14 codex 是怎么做的

**协议层是独立的 crate。** codex 把 `ResponseItem`、`ContentItem` 这些类型放在
`codex-rs/protocol/` 里，和业务逻辑（`core/`）分开。理由和这一章一样：**wire 格式是外部
约定，不该和内部逻辑长在一起。**

**归一化确实发生在边界。** `codex-rs/core/src/client.rs` 是完整的流状态机，
`codex-rs/core/src/event_mapping.rs` 负责把 wire 事件翻译成内部事件。上层拿到的永远是
归一化之后的东西。

**历史的不变量有专门的模块。** `codex-rs/core/src/context_manager/normalize.rs` 就是
干这个的——在历史被送出去之前修正/校验它的形状。而 `context_manager/history.rs` 里
`ContextManager` 那个结构体带着 `history_version: u64` 字段，注释写着：

> Bumped whenever history is rewritten, such as compaction or rollback.

**历史会被重写**，所以需要版本号来发现"你手上这份过期了"。第 6、7 章会遇到这个问题。

**多方言是真实存在的。** codex 里有 `codex-rs/core/src/mcp_tool_call.rs`（MCP 协议）、
`chatgpt/`、`backend-client/`、`ollama/`、`lmstudio/` 好几个供应商适配。第 9 章会展开。

**还有一个细节值得看**：`codex-rs/core/src/client_common.rs` 和 `client.rs` 分开。
**"所有供应商共用的"和"某一家特有的"被拆成了两个文件**——这正是这一章 `_render` 里
`if dialect == ...` 将来该演化成的样子。

---

## §15 三条线各自留下了什么

### 主线 A · 怎么把需求变成代码

**这一章的第一件事不是写代码，是去量。** §2 的待办表第一行是空的——"除了 URL 还有什么
不一样？不知道，得先测"。三次测量出三个事实，三个事实各推出一个决定。

**如果先设计再测量**，最可能的产物是一个"通用适配器基类"，然后发现真实差异根本不在你
设计的那些维度上。

**抽象决策**，这一章做了四个：

| 东西 | 决定 | 理由 |
|---|---|---|
| 客户端内缓冲分片 | **做** | 例外二：跨越信任边界，分歧到此为止 |
| `History` 类型 | **做** | 例外三：不变量需要强制，而**服务端不替你守** |
| `agent_types.py` | **做** | 被循环 import 逼的，最小解法 |
| 方言用 `Literal` 不是 `Enum` | **不做** | 可读性优先，真问题在 else 分支（见 review 第 4 条） |

**注意 `History` 这个决定在 Ch00 被明确否决过。** 当时的理由是"只有一处代码维护这个
不变量"。现在有了第二处（发送前校验），而且有了一个新证据（服务端不管）。

> **条件变了，答案就变了。这不是打脸，这是决策该有的样子。**
> 把"当时为什么不做"记下来，才能在条件变化时知道要重新评估。

### 主线 B · 工程化的行为

| 动作 | 本章的规则 |
|---|---|
| commit 类型 | 修 Ch00 遗留的问题用 `fix`，即使当时没人知道它是 bug |
| 延后的 review 意见 | 兑现时在 message 里**回指原文** |
| message body | 写"我量到了什么"，不写"这个设计更健壮" |
| 删测试 | 删了要说明理由；**是 lint 发现它没用的**，写进 message |
| review | 可以要求一个数字；不可以要求一个猜测 |
| 架构规则 | 值得保护的边界要写成测试，用 `ast` 解析而不是字符串匹配 |
| API key | 从环境变量读，不从命令行参数读 |

### 主线 C · 故障的预防与发现

**第一招：**

> **开发环境越宽松，bug 被发现得越晚。**
> 同一份坏历史，Ollama 返回 200 加胡言乱语，OpenAI 返回 400。
> **不要把"本地跑通了"当成"是对的"。**

**第二招：**

> 猜一个没见过的形状，猜对的概率不比不猜高。
> Ch00 拒绝为"可能分片"写代码是对的——不是因为运气好，而是因为那个决定**附带了一个
> 可检查的到期时间**（"第 1 章接第二家时先测再写"）。

**第三招：**

> 把检查放在**所有路径的交汇点**，不是放在你今天想得到的那条路径上。
> `to_wire()` 是数据离开你控制范围的最后一道门。

---

## 如果你只记住三件事

1. **"兼容"的 API 不兼容。** 同一个工具调用，一家给一个 chunk，一家给十四个，而且只有
   第一个带 id 和 name。这不是文档能告诉你的，是打开真实的流看出来的。

2. **不变量得自己守。** 三种坏历史，Ollama 全放行（并产出垃圾），OpenAI 全拒绝。
   指望服务端替你检查，等于指望你的开发环境和生产环境一样严格。

3. **归一化放在边界，改动就关在边界。** 这一章 `agent.py` 只动了 `run()` 的正文，
   `_collect` 和 `_run_tool` 一行没改——因为脏东西在 `model.py` 就被拦住了。

---

## 动手

```bash
cd steps/step01_protocol
uv sync --all-extras
uv run minicodex serve-stub &
uv run minicodex ask "What does src/minicodex/__init__.py define?" \
    --base-url http://127.0.0.1:11435/v1
uv run pytest
```

**建议自己做一遍的四件事**：

1. **把 `_PartialCall.absorb` 里的 `if raw.get("id"):` 改成无条件赋值**，跑
   `pytest -k F01_01`。你会看到 Ch00 那个 bug 的另一种形态。
2. 对着**你自己的 Ollama** 跑一次 `probe_protocol.py`（仓库根目录），亲眼看
   HTTP 200 加"symbiotic with the user's request"。**这是本章最重要的一次观察。**
3. 有 key 的话，跑 `probe_openai.py`，对比 F 那一段的十四个 chunk 和 Ollama 的一个。
4. 给 `History` 加第三种方言（比如 Anthropic 的 messages 格式），看看要改几个地方。
   答案应该是**一个函数**。

---

**下一章**：Ch02 · 第一个 shell 工具——`read_file` 已经不够用了，Agent 得能跑
`pytest`。而子进程会挂死、会吐 100MB、会等 stdin。
---

# 附录 · 代码到底怎么敲出来的（新手向，逐段精讲）

跟第 0 章附录同一个规矩：正文讲"为什么"，这里补"怎么写"。默认你已经读过
第 0 章的附录（那里讲了 `stub_ollama.py` 的 `_text`/`_call`/`_finish`/
`_Handler`/`serve` 和 `__main__.py` 的 argparse 基础），这里不重复那些，
只讲这一章新出现的、正文 §9 自述"包装函数见仓库"的部分。代码摘自
`steps/step01_protocol/src/minicodex/`，逐段核对过。

先把范围说死：

1. 本附录只解释第 1 章在 `steps/step01_protocol/` 里新增或修改的代码。
   正文 §9 的清点表说得很清楚：`history.py` 的 `History` 类在 §6.1–6.4
   给了完整实现，`agent_types.py` 的类型在 §6.1 给了，`model.py` 的
   `_PartialCall` 和 `stream()` 在 §5 给了——这些不再重复。这里补正文明说
   "见仓库"的：`stub.py` 的 OpenAI 碎片化录音（§3.2 只给了形状）、
   `agent_types.py` 的 `ToolCall` 完整定义、`__main__.py` 的 provider 接线。
2. 完整函数代码块里不使用代表遗漏实现的省略号，也不把关键分支写成
   "此处略"。示例字符串或命令文本里出现的三个点，如果本来就是源码的
   一部分，会原样保留。
3. 代码块按当前源码核对。

## L1 · `stub.py` 新增的 OpenAI 半边：碎片化工具调用

第 0 章的 stub 只模拟 Ollama（一个 chunk 装下完整 arguments）。第 1 章
接第二家供应商时发现：OpenAI 把一次工具调用切成**十四个 chunk**，只有
第一个带 id 和 name，其余每个只带一小片 arguments（正文 §5 的 F01-01）。
stub 要能回放这种形状，加了两个新包装函数和两组新录音。

### L1.1 两个新包装函数

```python
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
```

和 Ollama 那两个包装函数（第 0 章附录 K1.1）对照着看，差异全在 wire 形状：

1. **`_openai_call_first` 的 `content` 是 `None` 而不是 `""`。** OpenAI 在
   工具调用 chunk 上发 `content: null`，Ollama 发 `""`——正文 §5 的
   `stream()` 里那句 "Truthiness covers both; membership would not" 说的
   就是这个：`if delta.get("content")` 对 `None` 和 `""` 都跳过，而
   `"content" in delta` 会误判。
2. **`_openai_call_first` 里 `arguments` 是空串**——id 和 name 只出现在
   第一个 chunk，arguments 从第二个 chunk 才开始。
3. **`_openai_call_more` 只有 `index` 和 `function.arguments`**——没有 id、
   没有 name、没有 content。这就是 `_PartialCall.absorb` 要"有条件地写每个
   字段"的原因：后来的 chunk 几乎什么都没有。
4. **`"created": 1786038347` 和 `"model": "gpt-4o-mini-2024-07-18"`** 是
   2026-08-06 从 api.openai.com 录的真实值——和 Ollama 录音的 `1786021399`
   / `gemma4:31b` 区分开，回放时客户端能从时间戳看出是哪家。

### L1.2 两组新录音：碎片化调用和最终回答

```python
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
```

- **`_OPENAI_ARG_SLICES` 是真实切法**（十三个切片）：`'{"'`、`"path"`、
  `'":"'`…… 连 `"src"` 都切成 `/min` `ic` `od` `ex`——**一个词被从中间
  劈开**。这正是 `_PartialCall` 存在的全部理由：客户端必须把碎片拼回
  `{"path":"src/minicodex/__init__.py"}` 才能用。
- **`OPENAI_FRAGMENTED_CALL` = 第一个 chunk + 十三个切片 + finish**，
  一共十五个 chunk，覆盖"OpenAI 发 14 个碎片"的形态。
- **`OPENAI_FINAL_ANSWER` 复用第 0 章的 `_text`**（Ollama 的包装函数）——
  OpenAI 的最终回答和 Ollama 一样是普通文本流，不需要新包装。
- 注意 `_openai_call_more` 调用时传的 `index=0`：这些碎片都属于同一个
  工具调用（index 0），`_PartialCall` 按 index 归组。

### L1.3 `_Handler.do_POST` 的 mode 分发

stub 的 `serve()` 现在支持 `--openai` 模式，`do_POST` 里多了 `mode ==
"openai"` 分支（与第 0 章版本的差异只在选择录音的地方）：

```python
        answered = any(m.get("role") == "tool" for m in messages)
        if mode == "openai":
            chunks = OPENAI_FINAL_ANSWER if answered else OPENAI_FRAGMENTED_CALL
        elif answered:
            chunks = FINAL_ANSWER
        elif mode == "three":
            chunks = THREE_CALLS
        else:
            chunks = NARRATE_THEN_CALL
```

注意 `answered` 从"在 if 里判断"提成了**一个变量**——因为 `mode ==
"openai"` 分支也要用同样的判断（答没答过工具结果决定给哪段录音）。提出来
之后四个分支共享，不会出现"openai 分支忘了判断 answered"的疏漏。

## L2 · `agent_types.py`：`ToolCall` 完整定义

正文 §7 提到 `agent_types.py`（31 行），`ToolCall` 的完整定义：

```python
@dataclass(frozen=True)
class ToolCall:
    call_id: str
    name: str
    arguments: dict[str, Any] | None
    raw_arguments: str
```

这个文件整个存在的理由（docstring 明说）：**`history.py` 需要 `ToolCall`，
`agent.py` 需要 `History`**——把 `ToolCall` 留在 `agent.py` 会让两个模块
互相 import。移到叶子文件是"循环 import 的最小解法"（插曲 B 处理的是更
难的情况）。

`ToolCall` 四个字段里有两个要解释：

- **`arguments` 是解析后的 dict，可能是 `None`**——模型发来的 arguments
  不一定是合法 JSON 对象（可能是数组、字符串、垃圾）。"解析不了"是合法
  状态，用 `None` 表示，而不是抛异常。
- **`raw_arguments` 是原始字符串**——为什么存两份？两件事需要它：错误
  消息要**原样引用**模型发来的东西（第 3 章"错误信息即 prompt"）；以及
  第 1 章开始，回放要能**无损重新序列化**——`{"path":"x.py"}` 和
  `{"path": "x.py"}` 是同一个 dict 但不同字节，第 14 章的回放如果重渲染
  解析后的形式，会产出 provider 从没见过的请求（`replay.call_record` 的
  docstring 讲的 F07-09）。

## L3 · `__main__.py`：provider 接线

正文 §8 给了 key 从环境变量读的那行注释。完整接线：

```python
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
        api_key=os.environ.get("OPENAI_API_KEY") if provider == "openai" else None,
        tools=TOOL_SCHEMAS,
    )
    agent = Agent(llm, DEFAULT_TOOLS, recorder=recorder)

    result = await agent.run(question)

    print(result.final_text or "(no answer)")
    print(f"\n[{llm.model} | {result.stop_reason} after {result.turns_used} turn(s)]")
    print(f"[transcript: {recorder.path}]")
    return 0
```

三个要点：

1. **`PROVIDERS` 是一张 `{名字: (默认 URL, 默认模型)}` 表**——argparse 的
   `choices=sorted(PROVIDERS)` 直接吃它（用户传的名字自动被校验），
   `PROVIDERS[provider]` 解包出默认值。加第三家供应商 = 在表里加一行。
2. **`base_url=base_url or default_url`**——用户显式传了 `--base-url` 就
   用它（指向 stub 或自定义服务器），没传就用 provider 的默认。`model`
   同理。**"显式覆盖、缺省回落"是这个 `or` 的全部含义**，注意 `base_url`
   是 `str | None`，`None or default` 正好取默认。
3. **`api_key` 只在 `provider == "openai"` 时从环境变量读**（`--base-url`
   指向本地 stub 时不需要 key）。注释那句 "a key in argv shows up in shell
   history and in `ps`" 是全书的安全规则：**key 永远不进命令行参数**。

`main` 里对应的 argparse（与第 0 章版本只差三行）：

```python
    ask = sub.add_parser("ask", help="ask a question that requires reading a file")
    ask.add_argument("question")
    ask.add_argument("--provider", choices=sorted(PROVIDERS), default="ollama")
    ask.add_argument("--base-url", default=None)
    ask.add_argument("--model", default=None)
```

注意 `--provider` 有 `choices`（非法值 argparse 自己报错）而
`--base-url`/`--model` 没有（它们接受任意字符串，交给 `_ask` 的 `or` 逻辑
决定是否回落默认）。

## L4 · 新手常见报错/坑对照表

| 症状 | 原因 | 修法 |
|---|---|---|
| OpenAI 录音回放时 arguments 只有最后一片 | 客户端按 chunk 覆盖而不是拼接 | `_PartialCall.parts` 列表 + `"".join`（正文 §5） |
| 工具调用的 id 丢了 | 只有第一个 chunk 带 id | `absorb` 里 `if raw.get("id"): self.call_id = raw["id"]` 条件赋值 |
| `content: null` 被误判成有内容 | `"content" in delta` 对 null 也真 | 用 `delta.get("content")` 的真值判断（第 0 章附录 K1.3 同款） |
| 循环 import | `ToolCall` 留在 `agent.py` | 移到 `agent_types.py` 叶子文件 |
| key 出现在 shell 历史里 | 从命令行参数读 key | `os.environ.get("OPENAI_API_KEY")`，只在 `provider == "openai"` 时 |
| 加新 provider 要改多处 | 默认值散在各处 | `PROVIDERS` 表 + `base_url or default_url` |
| 回放重渲染的 arguments 和原件不同字节 | 只存解析后的 dict | `raw_arguments` 保留原始字符串 |
