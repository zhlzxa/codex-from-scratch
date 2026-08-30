# 第 6 章 · 上下文压缩

> **代码**：`steps/step06_compaction/`
> **分支**：`feat/compaction`
> **产出**：会话变长之后，Agent 自己把中间那段换成一份摘要，然后继续干活
> **你需要**：一个 API key（本章的每张表都是真发出去测的）。没有 key 也能跑测试，
> 1250 个测试里没有一个需要联网。

---

## §1 这一章要做出来的东西

第 5 章结束时，Agent 能问、能记、能干活。但它有一个上限，而且这个上限不在代码里，
在服务端：**上下文窗口**。

一个真实的编码任务长什么样？读 5 个文件、跑 3 次测试、改 2 处代码、再跑一次。
每一步的输出都进历史，每一轮都把**整个历史**重新发一遍。第 20 轮的那次请求，
装着前 19 轮的全部内容。

所以问题不是"会不会满"，是"满了之后怎么办"。

这一章要做的事，一句话：**在请求装不下之前，把中间那段删掉，换成一份摘要。**

听起来是个 `list` 切片。这一章有 13 条清单故障，其中 3 条就是"切片切错了"。
但真正值钱的不是那 3 条——

> 清单预测 F06-01 会"删了 tool_call 留下 output → API 400"。
> 实测：**同一个历史，一半的切法返回 400，另一半返回 200——而 200 那一半里，
> 有一个答的是另一个问题。**
>
> 400 是这一章最容易的一半。

---

## §2 先写一坨

最直接的写法。历史太长，砍掉最老的一半：

```python
def compact_v1(messages: list[dict]) -> list[dict]:
    return messages[len(messages) // 2 :]
```

一行。看起来没有任何可以出错的地方。

为了看见它动，先造一个真实形状的历史——第 5 章的 Agent 跑一个小任务会留下的东西：

```python
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
```

八条消息。为了能一眼看出形状，再写个渲染函数：

```python
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
```

```
S U A[call_a1] T[call_a1] A[call_b2] T[call_b2] A[call_c3] T[call_c3]
```

`S` 是 system，`U` 是 user，`A[x]` 是发起了调用 x 的 assistant 消息，
`T[x]` 是 x 的结果。**这个记号后面全章都用。**

现在真发出去。不 mock，不看文档，直接问服务器答不答应
（`probe_naive_cut.py`）：

```
=== F06-01 drop oldest half
    A[call_b2] T[call_b2] A[call_c3] T[call_c3]
    HTTP 200
    content: 'Your current Python version is 3.13.0, which is compatible with the required version (>=3.10) specified in the `pyproject.toml` file.'
    usage: 122 prompt tokens

=== (control) uncut
    S U A[call_a1] T[call_a1] A[call_b2] T[call_b2] A[call_c3] T[call_c3]
    HTTP 200
    content: 'This project supports Python versions **3.10 and above** as specified in the `pyproject.toml` file. You are currently using **Python 3.13.0**, which is compatible with this project.'
    usage: 176 prompt tokens
```

200。答案也对。token 从 176 降到 122。

**清单说这里应该 400 的。**

---

## §3 清单错了，而错法比对了更有用

`PLAN.md` 里 F06-01 那一行写的是：

> 删掉最老一半消息 → **删了 tool_call 留下 output** → API 400

推理没问题：砍一半，切点落在 `A[x]` 和 `T[x]` 中间，`T[x]` 就成了孤儿。
但这次没有，因为八条消息的一半是索引 4，而索引 4 正好是 `A[call_b2]`——
一个合法的切点。

**它是对的，因为 8 是偶数。**

这种"碰巧对了"是最糟的一类：它会通过你的测试，会通过 code review，
然后在某个用户的第 9 条消息上炸掉。

所以不要猜。把每一个切点都问一遍服务器（`probe_cut_points.py`）：

```
full history: S U A[call_a1] T[call_a1] A[call_b2] T[call_b2] A[call_c3] T[call_c3]

 cut  kept shape                                   status  note
------------------------------------------------------------------------------
   0  S U A[call_a1] T[call_a1] A[call_b2] ...     200     The project supports Python versions **3.10 and abov
   1  U A[call_a1] T[call_a1] A[call_b2] ...       200     The project supports Python versions greater than or
   2  A[call_a1] T[call_a1] A[call_b2] ...         200     The project requires Python version 3.10 or higher,
   3  T[call_a1] A[call_b2] T[call_b2] ...         400     Invalid parameter: messages with role 'tool' must be
   4  A[call_b2] T[call_b2] A[call_c3] T[call_c3]  200     Your current Python version is 3.13.0, which meets t
   5  T[call_b2] A[call_c3] T[call_c3]             400     Invalid parameter: messages with role 'tool' must be
   6  A[call_c3] T[call_c3]                        200     You are using Python version 3.13.0.
   7  T[call_c3]                                   400     Invalid parameter: messages with role 'tool' must be
```

完整的报错原文：

```
Invalid parameter: messages with role 'tool' must be a response to a
preceeding message with 'tool_calls'.
```

（`preceeding` 是服务端拼错的，原样贴出来。这种细节值得留意——
它说明这条消息是人手写的字符串，不是从 schema 生成的，
所以别指望能靠它做程序判断。）

先看 400 那三条。索引 3、5、7，全是落在 `T[...]` 上的切法。规律很清楚。

**然后看 200 那四条。**

---

## §4 200 那一半

用户问的是：**这个项目支持哪个 Python 版本？**

| 切点 | 返回 | 答案 |
|---|---|---|
| 0 | 200 | The project supports Python versions **3.10 and above** |
| 2 | 200 | The project requires Python version 3.10 or higher |
| 4 | 200 | **Your current** Python version is 3.13.0, which meets t… |
| 6 | 200 | **You are using Python version 3.13.0.** |

切点 0 答的是用户的问题。切点 6 答的是另一个问题。

中间没有任何一个环节报错。切点 6 的历史完全合法，模型完全自信，
句子完全通顺，token 少了三分之二——**而它回答的东西，用户没问。**

为什么？因为切点 6 只剩下 `A[call_c3] T[call_c3]`，也就是
"跑了 `python --version`，输出 Python 3.13.0"。模型看到的全部信息就是这个，
它据此推断用户在问当前解释器版本。这是它能做的最合理的推断。

> **删掉一条消息和删掉一个问题，是两件事，而它们看起来一模一样。**

切点 4 更阴险：它答对了一半（提到了 3.10），但主语已经从"项目"漂到了"你当前的"。
这种半对的答案在真实使用里根本发现不了。

所以压缩有**两个**任务，不是一个：

1. 让剩下的东西**发得出去**（400 那一半）；
2. 让剩下的东西**还知道自己在干什么**（200 那一半）。

清单上的 F06-01/02/03 全是第一个任务。第二个任务在清单上只有 F06-06 一行，
而它才是这一章一半的代码。

---

## §5 合法的切点是什么

回到 400 那一半，规律要写成代码。

第一版容易写成这样：

```python
def boundaries_v1(items):
    return [i for i, item in enumerate(items) if not isinstance(item, ToolResult)]
```

"不要切在结果上"。对这个历史，它给出 `0,1,2,4,6`——和实测一致。

但它是**描述现象**，不是**表达规则**。规则其实是：

> 切点 `i` 合法，当且仅当：`i` 之前发起的调用，没有一个是在 `i` 之后被应答的。

这两句在这个例子上等价，在别的例子上不等价。一条 assistant 消息可以一次发起
**三个**调用（第 0 章的 F00-03 就是这个），后面跟三条结果：

```
A[x,y,z] T[x] T[y] T[z]
```

`boundaries_v1` 会说 `T[y]` 前面那个位置不是结果……不对，它会说索引 1、2、3 都是
结果所以都不合法，索引 4 合法。碰巧也对。但换成

```
A[x,y] T[x] A[w] ...
```

这个历史根本不可能出现（第 1 章的 `History` 不让 assistant 在有未应答调用时再说话），
但如果你的历史是手工拼的 dict 列表，它就可能出现，而 `boundaries_v1` 会给出错误答案。

**用计数器表达真正的规则**：

```python
def boundaries(items: Sequence[HistoryItem]) -> tuple[int, ...]:
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

十行。跑一下：

```
items       8
boundaries  (0, 1, 2, 4, 6, 8)
measured200 (0, 1, 2, 4, 6)
```

`8` 是"全部切掉"，探针没测（那会发一个空的 messages 列表，是另一种错误）。
其余**逐个吻合**。

这条吻合关系直接写成测试，而不是写在注释里：

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
```

> **这个测试的价值不在于它会红。**它的价值是把"我们对服务端行为的假设"和
> "代码"绑在一起，并且注明了这个假设是哪天、对哪个模型、用哪个脚本测出来的。
> 三个月后服务端改了行为，你能在 30 秒内知道该重跑什么。

### 5.1 "砍一半"和"留最后 N 条"，错法不一样

现在可以精确说清那两条朴素规则错在哪：

```python
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
```

**两条规则都不是"有时候对"，是"从来不看"。**它们碰对的时候和碰错的时候，
执行的是同一段代码，没有任何分支能让你在日志里区分。

按 token 数切（F06-02）是同一个问题换了个变量：token 数和消息边界毫无关系，
所以它退化成"留最后大约 N 条"，然后落在哪儿全看运气。

---

## §6 不许动的那一段

现在处理 200 那一半。切点 6 丢掉的是用户的问题，所以规则的第一条显然是：
**用户的第一条消息不能删。**

但还有第二样东西。第 5 章的系统提示里有权限状态：

```
# What you are allowed to do right now
...
```

第 5 章实测过：模型不知道自己有什么权限时，3/3 会去要 `unrestricted`。
一个把系统提示压掉的 Agent 不是"变差了"，是**变成了另一个 Agent**。

所以受保护的是一段**前缀**：

```python
@dataclass(frozen=True)
class Protected:
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

### 6.1 为什么是"位置"不是"类型"

这里有一个容易写错的地方，而且写错了不会有任何症状。

直觉写法是"保护所有 `SystemNote`"。看起来更干净、更不容易漏。

但第 0 章的轮次预算警告也是 `SystemNote`：

```python
history.add_system_note(
    f"You have {remaining} tool-calling turn(s) left. "
    "Wrap up and give your best answer now."
)
```

它每一轮都往历史里加一条。按类型保护，等于**永久保留每一轮的过期警告**——
压缩之后历史里会躺着 "You have 2 turn(s) left"、"You have 1 turn(s) left"，
而当前轮次早就不是那个数了。模型会读到六条互相矛盾的预算通知。

按位置保护，这些都在前缀之后，该丢就丢：

```python
def test_F06_06_later_system_notes_are_not_protected():
    """Chapter 0's turn-budget warning is a SystemNote and is worth one turn."""
    h = history_with(1)
    h.add_system_note("You have 2 turn(s) left.")
    h.add_user("carry on")
    assert Protected.of(h.items).count == 2
```

> 这是一个很小的决定，但它是"**保护什么**"和"**保护哪里**"的区别。
> 前者是关于内容的判断，会随着内容种类增加而越来越难维护；
> 后者是关于结构的判断，加多少种 `SystemNote` 都不用改。

---

## §7 重建：走第 1 章那扇门

现在知道了切在哪、留什么。剩下的是把新历史造出来。

最省事的写法是直接切列表：

```python
new_items = items[:protected] + [summary_note] + items[cut:]
```

**不要这样写。**

第 1 章花了一整章把 `History` 做成"不可能进入非法状态"——不变量在 `append` 的时候
强制，不是在发送的时候检查。理由当时写在那一章里：

> 它是在进来的路上强制，而不是出去的时候检查，这样 traceback 指向的是
> 破坏它的那段代码，而不是三层之外的一个序列化函数。

直接拼列表，等于**绕过那扇门**。压缩的 bug 会变成一个 400，从服务器回来，
带着一句拼错了单词的英文，而堆栈里没有任何一行指向 `plan()`。

所以重建的方式是：把每一条**重新塞回去**。

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

代价：一次多余的遍历，几十微秒。
收益：**这个模块不可能产出一个非法历史**——不是"我们小心地不产出"，是产不出。

试着造一个孤儿：

```
HistoryError: no unanswered call with id 'call_a1'; awaiting []
```

本地、立刻、带着 id。对比一下服务端那句 `messages with role 'tool' must be a
response to a preceeding message with 'tool_calls'`——它连是哪一条都不告诉你。

> 第 1 章做的抽象，在第 6 章才第一次真正付账。
> **这就是"留缝不留抽象"里那个"缝"的样子**：当时只是把配对规则收进了一个类，
> 没有为压缩做任何设计，但压缩来的时候，正确的做法自动是最省事的做法。

---

## §8 什么时候压缩

切法解决了，还剩一个问题：**怎么知道该压了？**

压缩必须在请求**发出去之前**触发，所以需要一个服务器还没算过的数。

标准做法，`chars / 4`：

```python
def estimate(messages: list[dict]) -> int:
    chars = 0
    for m in messages:
        chars += len(m.get("content") or "")
        for call in m.get("tool_calls") or []:
            chars += len(call["function"]["name"]) + len(call["function"]["arguments"])
    return chars // 4
```

这个数字从哪来的？"英文大约 4 个字符一个 token"。

它对不对，不该查文档，该量（`probe_tokens.py`，`max_tokens=1`，只看 prompt 侧）：

```
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

**"英文散文 4 个字符一个 token" 是对的**——`long prose` 那行 1.04x，几乎完美。

**别的全都不对。**

| 内容 | 实际字符/token |
|---|---|
| 英文散文 | 4.17 |
| Python 源码 | 2.29 |
| `ls -la` 输出 | 1.93 |
| JSON | 1.76 |
| 中文 | 1.71 |

两端差 2.4 倍。而**一个 Agent 的历史几乎全是密集的那一端**——命令输出、源码、
JSON 参数、报错堆栈。散文只占系统提示那几行。

所以：

> **没有一个除数是对的。**调一个"更准的常数"出来，只是把它调到你样本的分布上，
> 换一个任务就又偏了。

更要命的是方向。上面 7 行里有 6 行 `est/actual < 1`，也就是**低估**。
低估意味着压缩**触发得太晚**，而"太晚"在这里的意思是：请求已经超了。

### 8.1 少算了两样东西

在换方案之前，先看看这个估计器有没有算漏。同一个脚本，加上 `--with-tools`：

```
=== no tools
prose only            37       9      16       0.56x

=== with tools
prose only            37       9      69       0.13x
```

同样一条 37 字符的消息，带上两个玩具工具的 schema，实际 token 从 16 涨到 69。

**多出来的 53 个 token，估计器一个都没算。**

工具 schema 不在 `messages` 里，所以第一版把它排除在外，理由是
"它不属于对话"。这句话是对的，也是没用的：

- 它**每一轮都发**；
- 历史被压缩了它**不会变小**；
- 两个玩具工具就 53 个 token，第 9 章 MCP 一进来，它会变成最大的一项。

第二样是每条消息的固定开销。`prose only` 那行：37 字符内容，估计 9，实际 16。
差的 7 个 token 里有 53 是工具（那是带工具的情况），不带工具时差 7——
role、分隔符、服务端的包装。

补上这两项：

```python
CHARS_PER_TOKEN = 4.0
PER_MESSAGE_TOKENS = 4


def estimate_messages(
    messages: Sequence[dict[str, Any]],
    tools: Sequence[dict[str, Any]] = (),
    *,
    chars_per_token: float = CHARS_PER_TOKEN,
) -> int:
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
```

---

## §9 让它自己纠正自己

补完之后还是不准——因为除数问题没解决。但有一个信息一直摆在那里没被用：

**服务器每次都会告诉你这次请求实际花了多少。**

那个数比任何本地估计都权威，而且它对应的请求只比当前这次早一轮。
一次会话里内容成分基本不变（都是同一个项目的代码和命令输出），
所以上一轮的**偏差比例**对这一轮基本适用。

三个估计器，同一个逐渐变长的会话，八轮（`probe_calibration.py`）：

```
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

- `v1` = 原始的 `chars/4`：**稳定低估 40–75%，八轮没有一轮是对的。**
- `v2` = 补上工具和每条开销：前两轮高估，之后稳定在 −31…−38%。除数问题还在。
- `v2c` = 用上一轮观测到的比例修正 `v2`：**从第 3 轮起，误差在 ±4% 以内。**

前两轮 `v2c` 跳得很厉害（+62%、−20%、−47%），因为观测样本只有一个而且历史很小。
**这不要紧**：两条消息的历史不会撑爆窗口。压缩真正需要准确度的时候，
恰好是它已经准了的时候。

写成一个类：

```python
class Calibration:
    def __init__(self) -> None:
        self._ratio: float | None = None
        self.observations = 0

    @property
    def ratio(self) -> float:
        """1.0 until the server has said something.  Never a guess dressed up
        as a measurement."""
        return self._ratio if self._ratio is not None else 1.0

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
```

那个 `actual <= 0` 的判断不是防御性编程的仪式。`actual` 来自
`chunk["usage"].get("prompt_tokens", 0)`——一个不报 usage 的服务端会让它变成 0，
然后 `ratio` 变成 0，然后**从此每一个请求的估计都是 0 个 token**，压缩永远不触发。

一个 fallback 值把一个功能静默关掉，这是本书里反复出现的形状。

```python
@pytest.mark.parametrize("actual", [0, -1])
def test_F06_07_a_missing_usage_field_cannot_zero_the_ratio(actual):
    calibration = Calibration()
    calibration.observe(estimated=500, actual=actual)
    assert calibration.ratio == 1.0
```

### 9.1 起点为什么选乐观的那一端

`CHARS_PER_TOKEN = 4.0` 是散文的数字，也就是**最乐观**的那个。
密集内容实际是 1.76，选 4.0 意味着第一轮一定低估。

为什么不选保守的 1.7？因为在有观测之前，1.7 会让估计值虚高 2.4 倍，
**第一轮就触发压缩**——把一个刚开始的会话压掉，纯粹是浪费窗口。

选乐观值 + 尽快校准，比选保守值 + 永远保守，在真实会话里更划算。
但这是个权衡，不是定理，所以它写在常量旁边而不是藏在心里：

```python
# The prose figure, used as the starting point.  It is deliberately the
# optimistic end: `Calibration` moves it, and a starting point that is too
# pessimistic wastes the whole window on turn one, before any observation
# exists to correct it.
CHARS_PER_TOKEN = 4.0
```

---

## §10 校准数据根本没送过来

上面整套设计有一个前提：服务器会告诉我们 `usage`。

第 8 章那些表都是用 `"stream": False` 测的。**而我们的客户端是流式的**
（第 1 章定的，为了边生成边显示）。

流式响应里有 usage 吗？测：

```python
for label, extra in [('stream, as chapter 1 sends it', {}),
                     ('stream + include_usage', {'stream_options':{'include_usage':True}})]:
    body = {'model':'gpt-4o-mini','messages':[...],'stream':True, **extra}
    seen = []
    with httpx.stream(...) as r:
        for line in r.iter_lines():
            if line.startswith('data: ') and line[6:] != '[DONE]':
                c = json.loads(line[6:])
                if c.get('usage'): seen.append(c['usage'])
    print(f'{label:<32} usage chunks: {len(seen)}')
```

```
stream, as chapter 1 sends it    usage chunks: 0  []
stream + include_usage           usage chunks: 1  [{'prompt_tokens': 9, ...}]
```

**零。**

流式请求默认**完全不返回 usage**，必须显式要。

这条故障的形状值得停一下看清楚：

- 没有异常
- 没有警告
- 没有 400
- `Calibration` 老老实实地保持 `ratio = 1.0`，`describe()` 老老实实地说
  `uncalibrated (no usage reported yet)`
- 而估计值就那么一直低 35%，压缩一直触发得太晚

**整个第 9 节的机制存在、正确、并且从未运行过。**

这是 🟡 静默错误里比较高级的一种：不是代码写错了，是**代码的输入从来没来过**，
而代码对"没有输入"的处理是完全合理的。

修复只有一行：

```python
if self.report_usage:
    body["stream_options"] = {"include_usage": True}
```

以及一个测试，把"必须显式要"这件事钉死：

```python
def test_F06_07_usage_must_be_requested_explicitly():
    """Measured: a streaming request returns 0 usage chunks without this, and
    1 with it.  Nothing errors -- the calibration source simply never arrives."""
    body = ChatCompletionsModel().request_body([{"role": "user", "content": "hi"}])
    assert body["stream_options"] == {"include_usage": True}
    off = ChatCompletionsModel(report_usage=False).request_body([])
    assert "stream_options" not in off
```

为什么留一个开关而不是写死？因为它是请求体里的一个字段，不是每个说这套协议的
服务端都必须认识。默认打开，因为要的代价是一个 key，不要的代价是一个永远不校准的
压缩触发器。

---

## §11 打开它，第 1 章的解析器炸了

改完那一行，跑起来：

```
IndexError: list index out of range
```

看一眼那个 chunk：

```python
p = json.loads(lines[-2])
print('choices:', p['choices'])
print('usage  :', p['usage']['prompt_tokens'])
```

```
choices: []
usage  : 9
```

**带 usage 的那个 chunk，`choices` 是空数组。**

而第 1 章的解析循环里有这么一行，五章没出过问题：

```python
chunk = json.loads(payload)
choice = chunk["choices"][0]
```

它在这一章之前一直是对的，因为在这一章之前，每个 chunk 都有 choices。

发生的时机也值得注意：这个 chunk 是**倒数第二个**，在 `[DONE]` 之前。
也就是说，**答案已经完整地流完了**，用户已经看到全部输出了，然后程序崩溃。
第 2 章的 F02-07（非 UTF-8 字节导致解码崩溃）是同一个形状：
命令已经跑完了，崩在处理结果上。

修法：

```python
chunk = json.loads(payload)

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

顺序有讲究：先取 usage，再判空跳过。反过来写的话，usage 永远取不到——
因为带 usage 的 chunk 正好就是 choices 为空的那个。

### 11.1 一个测试，和它没测到的东西

第一版测试是这么写的：

```python
async def test_F06_07_the_usage_chunk_has_no_choices():
    chunks = [
        '{"choices":[{"delta":{"content":"hi"},"finish_reason":null}]}',
        '{"choices":[],"usage":{"prompt_tokens":77,"completion_tokens":9}}',
    ]

    class FakeResponse:
        async def aiter_lines(self):
            for chunk in chunks:
                yield f"data: {chunk}"
            yield "data: [DONE]"

    events = [event async for event in _replay_stream(FakeResponse())]
    assert Usage(77, 9) in events
```

`_replay_stream` 是我在测试文件里**照着 `stream()` 抄的一份解析循环**——
因为真正的解析循环在 `stream()` 里面，而 `stream()` 要开 socket。

绿了。看起来很合理。

后面 §23 的变异测试把这行守卫删掉：

```
model: index into an empty choices list again              0
```

**0 个测试失败。**

因为那个测试测的是**我抄的那一份**。`model.py` 里的守卫删掉，测试里的副本还在，
所以测试照样绿。

这和第 5 章那条一模一样：

> 一个专门用来"检查别的东西"的模块，最容易犯的错就是以为自己检查过了。

正确的修法不是"再抄仔细一点"，是**让字节走真正那条路**。`httpx` 有
`MockTransport`，客户端开一个注入口就行：

```python
self.transport = transport
...
async with httpx.AsyncClient(timeout=self.timeout, transport=self.transport) as client:
```

测试变成：

```python
async def test_F06_07_the_usage_chunk_has_no_choices():
    """...

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

现在删掉守卫，测试红。

> **关于"为测试加一个参数"**：`transport` 是一个只为测试存在的构造参数，
> 很多人会说这是"测试污染生产代码"。
> 这里它是对的，理由不是"方便测试"，是**没有它就没有测试**——
> 替代方案是抄一份解析逻辑，而那份副本会独立地正确下去，与真代码无关。
> 一个只有一个实现的接口是仪式（第 B 章的 FB-03），
> 但一个让真代码被真正执行的注入口不是。

---

## §12 摘要该写什么

切掉的那一段不能凭空消失，要换成一份摘要。

最直接的做法：

```python
NAIVE_PROMPT = "Summarise the conversation above so it can be continued later."
```

这句话哪里不对？它没有说**摘要是给谁看的、要拿来干什么**。

模型会写出一份给人看的会议纪要：流畅、连贯、按时间顺序、有起承转合。
而下一轮真正需要的东西是：

- 已经做完的事（**别重做**）
- 已经定下的决定（**别重新讨论**）
- 用户说过的约束（**别违反**）
- 花了代价才试出来的具体字符串（**别重新试一遍**）

这四样在"流畅的纪要"里全是可有可无的细节。

所以 prompt 要求六个固定小节（`prompts/compaction.md`）：

```markdown
## Goal
What the user asked for, in their terms. ...

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
```

几个不显眼但要紧的措辞：

**"A step with no artefact named is not done."**
——不要求证据的话，模型会写 "implemented the retry logic"，
而下一轮读到这句没法判断文件到底存不存在。要求点名产物，
这句话就变成 "wrote `src/net.py`: added `retry()`"，可验证。

**"Write `(none)` under a heading rather than omitting it."**
——缺一个小节读起来像"这件事没讨论过"，而它真正的意思可能是"这件事丢了"。
读者分不出这两种，所以不许省。

**"Exact strings that would cost a tool call to obtain again."**
——这是 `## Key data` 存在的唯一理由。摘要的性价比标准不是"重要程度"，
是"重新拿到它要花几次工具调用"。

---

## §13 它给我编了一个目标

写完 prompt，接上真模型，跑一遍。第一份摘要（gpt-4o-mini）：

```markdown
## Goal
Record the change in CHANGELOG.md.

## Done
- Updated `src/net.py` to include `retry()` with exponential backoff and jitter.
...
```

会话的真实目标是：

> Add retry logic to src/net.py. It has to stay compatible with Python 3.9,
> so no match statements. Do not touch src/legacy.py under any circumstances.

**`## Goal` 写的是"在 CHANGELOG 里记一笔"——那不是目标，那是剩下的最后一步。**

3/3，稳定复现。

为什么？回头看 §6：用户的第一条消息是**受保护的**，所以它**不在**交给摘要器的那段
里。我要求模型写一个 `## Goal` 小节，同时把说明目标的那条消息从它眼前拿走了。

它做了一个模型在这种处境下必然做的事：**从看得见的部分推断一个目标出来。**
而看得见的部分末尾正好是"还剩 CHANGELOG 没写"。

然后这份摘要作为一条 `SystemNote` 被放进历史，**紧挨着真实的用户消息**，
两者内容互相矛盾，而 `SystemNote` 有系统消息的权威。

> 这条故障不在清单上。它是"保护前缀"（F06-06）和"结构化摘要"（F06-04）
> 两个修复**互相作用**产生的，两个单独看都是对的。
>
> **这类故障没法在设计阶段想出来，只能跑出来。**

### 13.1 修法：给它看，但不让它压

摘要器需要看到受保护的前缀——**不是为了压缩它**（它本来就原样留着），
而是为了让摘要**和它一致**。

这意味着摘要器的入参从两个变成四个。四个位置参数是不可读的，所以收成一个对象：

```python
@dataclass(frozen=True)
class SummaryRequest:
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

prompt 里加一段，把三个块的**处置方式**说清楚：

```markdown
You are given up to three blocks. Only one of them is being replaced:

- `<context>` -- the opening of the conversation. **It is not being deleted**
  and does not need compressing. It is here so your summary agrees with it.
- `<established>` -- the surviving record of an earlier compaction, if any.
- `<transcript>` -- the messages that are about to be destroyed. This is the
  only thing you are summarising.
```

以及 `## Goal` 那一节改成：

```markdown
## Goal
What the user asked for, in their terms. **Take this from the `<context>`
block, which holds the opening of the conversation. Do not infer it from the
transcript.** The transcript is the middle of a session: its last few steps look
like the goal and are not.
```

最后那句是这次修复真正的内容：**"transcript 的最后几步看起来像目标，但它们不是。"**
这句话直接描述了模型犯的那个错。

再跑：

```markdown
## Goal
Add retry logic to src/net.py. It has to stay compatible with Python 3.9, so no
match statements. Do not touch src/legacy.py under any circumstances.

## Done
- Wrote `src/net.py`: added `retry()` with exponential backoff and jitter.
- Passed tests after modification: `5 passed in 0.44s` using
  `pytest tests/test_net.py -p no:randomly`.

## Decisions
- Use exponential backoff with jitter for retry logic and honor the
  `Retry-After` header for 429 responses.

## Constraints
- Must remain compatible with Python 3.9 (no match statements).
- Do not modify `src/legacy.py` under any circumstances.

## Open
Record the change in `CHANGELOG.md`.

## Key data
- Path of modified file: `src/net.py`
- Command for testing after changes: `pytest tests/test_net.py -p no:randomly`
```


---

## §14 量一下：结构化到底值多少

改对了一个例子不等于改对了。要量，就得有可判定的指标——
"摘要质量"没法判定，"某个具体事实还在不在"可以。

在一段真实形状的会话里**种五个事实**，每一个都是下一轮确实需要的
（`probe_summary.py`）：

| | 事实 | 为什么下一轮需要 |
|---|---|---|
| A | `Python 3.9`，不许用 `match` | 用户的约束，违反了就是返工 |
| B | 不许动 `src/legacy.py` | 用户的禁令，违反了是事故 |
| C | 测试必须加 `-p no:randomly` | **失败过一次才试出来的**，丢了要再失败一次 |
| D | `src/net.py` 已经写完、测试通过 | 丢了会重做 |
| E | 还剩 `CHANGELOG.md` 没写 | 丢了就不知道接下来干什么 |

指标怎么定，这里有个坑，我第一次就踩了：

> 第一版是在**摘要文本**里 grep 这五个事实。结果 A 和 B 稳定"丢失"。
>
> 但 A 和 B 在用户的第一条消息里，那条消息是**受保护的**，压缩之后原样还在。
> 它们不该出现在摘要里——出现了才是浪费 token。
>
> **指标测错了对象，会给出一个和真相相反的结论。**

改成在**压缩后的整个历史**里检查。5 个样本：

```
prompt       sample  A constraint  B do-not-touch  C discovered flag  D done  E open
naive        0       yes           yes             NO                 yes     NO
naive        1       yes           yes             NO                 yes     NO
naive        2       yes           yes             NO                 yes     NO
naive        3       yes           yes             NO                 yes     NO
naive        4       yes           yes             NO                 yes     NO
structured   0       yes           yes             NO                 yes     yes
structured   1       yes           yes             yes                yes     yes
structured   2       yes           yes             yes                yes     yes
structured   3       yes           yes             NO                 yes     yes
structured   4       yes           yes             yes                yes     yes
  naive        15/25 facts kept
  structured   23/25 facts kept
```

三件事：

**1. 朴素摘要 5/5 丢掉了 E（剩下要干什么）。**
它写的是一份回顾，而回顾天然是朝后看的。"接下来干什么"不是回顾的一部分。
`## Open` 这个小节的存在，就是这 5/5 的全部理由。

**2. 朴素摘要 5/5 丢掉了 C（`-p no:randomly`）。**
这个最贵：它是**测试失败过一次**才试出来的。丢了它，下一轮会再失败一次，
再花一次工具调用重新发现。结构化摘要 3/5 保住了。

**3. C 是唯一一个结构化也保不住的。**3/5，不是 5/5。

第三点值得单独说。`## Key data` 那一节已经明确要求
"exact strings that would cost a tool call to obtain again"，
`-p no:randomly` 完全符合这个描述，而且它就在 transcript 里明晃晃地写着
`HINT: rerun with -p no:randomly`。

**还是有 2/5 丢了。**

所以结论不是"结构化 prompt 解决了这个问题"，是：

> 结构化 prompt 把丢失率从 100% 降到 40%。**摘要是有损的，而且损失是随机的。**
> 任何"压缩之后信息都还在"的设计假设都是错的。

这直接决定了下一章（中断与恢复）为什么要把**原始 rollout 落盘**：
摘要不是历史的替代品，它只是**当前上下文**的替代品。

### 14.1 F06-05 没有复现

清单上 F06-05 是：

> 压缩后模型"失忆"，重做已完成的工作 | 🔵 | → 摘要必须明写"已完成"

第二组测量就是测这个：只给模型压缩后的历史，看它下一步调什么工具。
重新 `apply_patch` 到 `src/net.py` 就算重做。

```
=== 2. given only the compacted history, what does it do next
naive        0       redid=False  ['read_file({"path":"src/net.py"})']
naive        1       redid=False  ['read_file({"path":"src/net.py"})']
...
structured   3       redid=False  ['read_file({"path": "src/net.py"})', 'read_file({"path": "CHANGELOG.md"})']
structured   4       redid=False  ['read_file({"path":"src/net.py"})']
```

**10/10 都没有重做**，两种 prompt 都一样。

它们做的是先 `read_file("src/net.py")`——**去看一眼自己写完的东西**。
这不是重做，这是核对，而且是合理行为（第 13 章的 F13-03 会专门要求这种核对）。

所以：

> **F06-05 · NOT REPRODUCED**，在 gpt-4o-mini 上，这个 transcript 上，10/10。
>
> 没有为它写任何防御代码。`## Done` 那一节留着，但它的**实测依据**是
> §14 那张表里的 E（剩余任务）和 C（关键字符串），不是 F06-05。

这是第 3 章立下的规矩：**没复现的故障不写代码**。
写了也没法验证它有没有用，只会变成后人不敢删的一段。

---

## §15 摘要器自己挂了

压缩本身要调一次 LLM。这次调用会失败——超时、限流、连接断掉。

而这个失败发生在**最坏的时刻**：压缩是被"装不下了"触发的，
所以"稍后重试"这个选项不存在，下一个请求就是那个超限的请求。

第一反应是往上抛，让调用方处理。但调用方是 Agent 循环，它也没有别的办法。

真正的选择只有两个：

1. **静默硬截断**：直接丢掉那一段，什么都不说；
2. **硬截断 + 告诉模型**。

选 1 的话，模型会在一个缺了一大块的历史上继续，并且**完全不知道自己缺了东西**。
它会假设之前的步骤都成功了，因为没有任何相反的信息。

```python
def _hard_summary(items: Sequence[HistoryItem], reason: str) -> str:
    """What to say when the summariser could not be reached (F06-08).

    Compaction is triggered by being out of room, so "try again later" is not
    available -- the next request is the one that does not fit.  The fallback
    is deterministic and local: state that context was lost, state how much,
    and say so *to the model*, because the alternative is an agent that quietly
    forgets and confidently proceeds.
    """
```

真实输出：

```
[compacted transcript | generation 1 | 24 message(s) replaced | 2026-08-10 09:03]
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

**This summary could not be generated** (ConnectionError: [Errno 111] Connection
refused). 24 earlier message(s) were discarded unread (12 AssistantMessage,
12 ToolResult). Do not assume any earlier step succeeded. Before continuing,
re-check the current state with a tool -- read the files you believe you wrote,
re-run the command you believe passed -- and ask the user if the goal is no
longer clear.
```

几个刻意的设计：

**六个 `(lost)` 小节保留着。**格式和成功时完全一样。
这是 §12 那条 "write `(none)` rather than omitting it" 的同一个道理——
读的人（模型）通过**结构**判断"这里本该有东西"。

**"Do not assume any earlier step succeeded."**
这是第 5 章的教训在这里的应用：让模型改变行为的不是"告诉它出事了"，
是**给它一件具体的事做**。这里给的三件事是：读你以为写过的文件、
重跑你以为通过的命令、目标不清就问用户。

**异常类型和消息原样带上。**`ConnectionError: [Errno 111] Connection refused`
对模型没什么用，但对**看录像的人**有用。

### 15.1 空摘要也是失败

还有一种失败不抛异常：摘要器返回了空字符串。

```python
try:
    summary = await summarise(...)
    degraded = False
    if not summary.strip():
        raise CompactionError("summariser returned an empty summary")
except Exception as exc:
    summary = _hard_summary(dropped, f"{type(exc).__name__}: {exc}")
    degraded = True
```

在 `try` 里面 raise 再被自己的 `except` 接住，看起来是个绕弯。
写成 `if not summary.strip(): summary = _hard_summary(...)` 也行，
但那样就有两处构造降级摘要的代码，两处都要记得设 `degraded = True`。

> **"一个空摘要不是一份很短的摘要，是一段被静默销毁的 transcript。"**
> 这句话是这个判断存在的全部理由，它写在测试的 docstring 里：
>
> ```python
> async def test_F06_08_an_empty_summary_counts_as_a_failure():
>     """A summariser that returns "" is not a very short summary; it is a
>     silently destroyed transcript."""
> ```

还有一个同类的：流断了。`make_summariser` 里：

```python
if not saw_end:
    # The same rule as chapter 0's loop: a stream that stopped early is
    # not a short summary, it is an unknown one.  Half a summary that
    # replaces a whole transcript is worse than admitting the loss.
    raise CompactionError("summariser stream ended without a [DONE] sentinel")
```

和第 0 章 F00-04 是同一条规则：**没看到 `[DONE]` 就什么都不要**。
半份摘要替换掉整段 transcript，比承认丢失更糟——因为它看起来是成功的。

---

## §16 一条比整个窗口还大的输出

清单 F06-09：单个工具输出就超过整个窗口。

这条不能靠压缩解决，而且原因很具体：

> 压缩的手段是**丢掉旧的**。但装不下的那个东西是**最新一轮**的一个 400KB 堆栈，
> 而最新一轮恰恰是最不能丢的。
>
> 丢光所有旧消息，那条还在。

所以需要一个不同的动作：**把单条截断**。

```python
def clip_item(item: HistoryItem, *, max_tokens: int = MAX_ITEM_TOKENS) -> HistoryItem:
    if not isinstance(item, ToolResult):
        return item
    limit_chars = max_tokens * 4
    if len(item.content) <= limit_chars:
        return item
    head = limit_chars // 2
    tail = limit_chars - head
    omitted = len(item.content) - head - tail
    clipped = (
        item.content[:head]
        + f"\n... ({omitted} characters omitted by compaction; "
        "re-run a narrower command if you need the middle) ...\n"
        + item.content[-tail:]
    )
    return ToolResult(item.call_id, item.name, clipped)
```

**头尾各留一半**，和第 2 章的 `_clip` 一样，理由也一样：
堆栈的异常在最上面，摘要行在最下面，只留尾巴等于删掉了说明原因的那一半。

第 2 章已经在 shell 工具那里截过一次了，这里为什么还要一道？
因为第 2 章那道在**工具里**，只管 shell。`read_file` 读一个大文件、
MCP 工具返回一个大 JSON（第 9 章）、子 Agent 返回 5000 字（第 10 章的 F10-05），
都不经过它。

**这一道在历史层，管所有人。**

那句 `re-run a narrower command if you need the middle` 是第 3 章的教训：
截断信息本身就是给模型看的 prompt，要说清下一步能做什么。

---

## §17 拒绝去猜自己不认识的东西

清单 F06-10：图片/多模态内容 token 估算完全没算。

这个项目没有多模态。所以最诚实的做法是什么？

先看不做任何事会发生什么。多模态的 `content` 是一个 list：

```python
{"role": "user", "content": [
    {"type": "text", "text": "what is this"},
    {"type": "image_url", "image_url": {"url": "data:image/png;base64,..."}},
]}
```

`len()` 一个两元素的 list 是 **2**。除以 4 是 **0**。

**一张图片被算成免费的。**

这比"没算"糟得多：预算不但触发得晚，还会**报告一切正常**。
一个把最大的一项估值为零的预算系统，比没有预算系统更危险。

```python
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
```

> **"Raising is the honest version of not having done it."**
>
> 这一章不实现按模态估算，因为没有任何东西可以对着量——图片的 token 数取决于
> 尺寸和供应商的分块规则，凭文档写一个公式出来，就是 `PLAN.md` §7.5 第 7 条禁止的
> "为没观测到的行为写代码"。
>
> 但"不实现"有两种：**静默返回一个小数字**，和**大声说不知道**。
> 前者是 bug，后者是缺口。缺口会在第一次真有人传图片的时候，
> 在本地、带着 `F06-10` 这个编号、指着这个类炸掉。

`None` 仍然返回 0，因为那是真的没有内容（assistant 发起工具调用时 content 是 null）。

---

## §18 摘要自己超预算

摘要替换掉了 24 条消息，但它自己也占地方。而且它占的地方比看起来贵：

> 摘要会在**这次会话剩下的每一轮**里被重新发送。
> 它是唯一一项成本要乘以后续所有轮次的东西。

所以要给它一个预算，并且这个预算必须在**选切点之前**就预留出来：

```python
for cut in candidates:
    tail = [clip_item(i, max_tokens=max_item_tokens) for i in items[cut:]]
    size = _size(head, tail, sizer) + summary_budget
    if size <= budget:
        ...
```

为什么不能事后检查？因为顺序不可逆：

> 先定切点、再发现摘要装不下，这时候**用来做另一个决定的那些消息已经交给模型
> 并且扔掉了**。没有第二次机会。
>
> 预留出来，是让 F06-12 的"第二遍压缩"变成**不必要**，而不是变成**罕见**。

### 18.1 两把尺子

摘要写出来之后要裁到预算内。第一版：

```python
def _fit_summary(text: str, budget: int) -> str:
    limit = budget * 4
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "\n... (summary truncated to fit its budget)"
```

`budget * 4`——用 `chars/4` 换算。

**而这一整章的前半部分就是在证明 `chars/4` 会低估 35%。**

更糟的是，摘要恰恰是最密集的那类内容：prompt 明确要求它写路径、命令、
报错原文、版本号。按 §8 的表，这类内容实际是 1.8 字符/token。
**一份"裁到 700 token"的摘要，实际可能是 1600。**

问题的根子不是这一行算错了，是：

> `plan()` 用校准过的估计选切点，`_fit_summary()` 用裸的 `len//4` 裁摘要。
> **预留的位置和填进去的东西，是用两把不同的尺子量的。**
>
> 错的不是其中一把，是**有两把**。

所以把"量尺寸"收成一个对象，它同时知道工具 schema 和校准比例：

```python
@dataclass(frozen=True)
class Sizer:
    """One place that answers "how big is this", including the correction.

    It exists because the correction has to reach **both** decisions and the
    first version only reached one. ...  The bug is not that one call site was
    wrong; it is that there were two call sites at all.
    """

    tools: tuple[dict[str, Any], ...] = ()
    ratio: float = 1.0

    def messages(self, messages: Sequence[dict[str, Any]]) -> int:
        return int(estimate_messages(messages, self.tools) * self.ratio)

    def text(self, text: str) -> int:
        return int(len(text) / CHARS_PER_TOKEN * self.ratio)

    def clip_text(self, text: str, budget: int) -> str:
        if self.text(text) <= budget:
            return text
        keep = int(budget * CHARS_PER_TOKEN / self.ratio)
        return text[:keep].rstrip() + "\n... (summary truncated to fit its budget)"
```

> **什么时候该抽象？**这里满足了 `PLAN.md` §2 的"不变量需要被强制"那一条：
> "所有尺寸判断必须用同一把尺子"这条规则，不封进一个对象，
> 就会有 N 处各自维护，而且**漏掉的那一处不会报错**。
>
> 三个调用点（`plan`、`clip_text`、`Agent._maybe_compact`），三次法则也满足了。

这条 bug 有它自己的测试，而且这个测试是**唯一**能发现它的：

```python
def test_F06_12_the_summary_is_measured_with_the_same_sizer_as_the_cut():
    """The bug this pins: `plan()` used the calibrated estimate and the summary
    trimmer used a bare `len(text) // 4`, so the reservation and the thing
    filling it were measured with different rulers."""
    dense = Sizer(ratio=2.0)
    text = "x" * 4000
    assert dense.text(text) == 2000
    assert Sizer().text(text) == 1000
    assert len(dense.clip_text(text, 100)) < len(Sizer().clip_text(text, 100))
```

---

## §19 摘要的摘要的摘要

一次长会话会压缩不止一次。第二次压缩的时候，第一次的摘要就在历史里，
它会被当成普通消息一起压掉——**摘要的摘要**。

清单 F06-13 说这会导致信息逐代衰减。

先给出机制，再量它。机制有两条：

**1. 锚点永不参与。**用户的第一条消息是受保护的，它永远不会进入任何一代摘要。
所以无论压多少代，"用户要什么"这件事的信息源始终是原文，不是转述的转述。

**2. 上一代摘要单独传，并且要求原样带过去。**

```python
blocks = [f"<context>\n{request.context}\n</context>"]
if request.established:
    blocks.append(f"<established>\n{request.established}\n</established>")
blocks.append(f"<transcript>\n{request.transcript}\n</transcript>")
```

prompt 里：

```markdown
- If an **established** block appears below, it is the surviving record of an
  earlier compaction. Its content has already outlived the transcript it came
  from. Carry it forward into the sections above, unchanged in meaning and
  unchanged in its exact strings. Do not compress it further and do not drop an
  item because it looks old.
```

`Do not drop an item because it looks old` 这句是针对一个具体倾向的：
模型压缩时天然偏向保留最近的东西，而"已经活过一次压缩的事实"恰恰是**最该保留的**——
它之所以还在，是因为它一直有用。

**3. 代数要数出来。**

### 19.1 数代数的那段代码是错的

第一版这么写：

```python
generation = 1
for token in header.replace("]", " ").split():
    if token.isdigit():
        generation = int(token)
```

"header 里的数字就是代数"。

header 长这样：

```
[compacted transcript | generation 1 | 24 message(s) replaced | 2026-08-10 06:22]
```

里面有 **1**、**24**、**2026**、**08**、**10**、**06**、**22**。
循环取的是**最后一个**。

```
E       AssertionError: assert 24 == 2
E        +  where 24 = SummaryRequest(..., generation=24).generation
```

第二次压缩的代数是 24。第三次会是时间戳里的分钟数。

这个 bug 是测试发现的，不是 review 发现的——因为读代码的时候
"找 header 里的数字"听起来完全合理。**只有在把 header 的实际内容摆出来的时候，
它才显然是错的。**

修法是把它锚定在前面那个词上：

```python
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
```

> 顺带一提：如果这个 header 是结构化的（比如 JSON），这个 bug 不会存在。
> 用人可读的字符串当数据载体，就是在给未来的自己准备一个解析 bug。
> 这里保留字符串是因为**模型要读它**——它得知道自己在看一份摘要——
> 但代价是这段解析代码，以及它的测试。

### 19.2 量：六代之后还剩什么

同一批种下的五个事实，连续压缩六代，每一代之间插入一批无关的新工作
（这样旧事实只能通过摘要传递）：

```
=== 3. summarising the summary, N times over
  gen 1  4/5  C  <- lost
  gen 2  4/5  C  <- lost
  gen 3  4/5  C  <- lost
  gen 4  4/5  C  <- lost
  gen 5  4/5  C  <- lost
  gen 6  4/5  C  <- lost
```

结论比预期的好，而且形状不一样：

> **衰减不是渐进的滑坡，是第 1 代掉了一个，然后六代不再掉。**

丢的还是 C（`-p no:randomly`），和 §14 的 2/5 丢失率一致——
它是在**第一次**压缩时丢的，之后就再也不可能回来了。

剩下四个，六代之后一个没少。`<established>` 那段 prompt 是这个结果的直接原因：
每一代看到的不是"上一代的 transcript"，而是"上一代已经筛选过的结论 + 必须原样带走"。

所以 F06-13 的实测结论是：

> **信息损失集中发生在第一次压缩，不是累积在每一次。**
> 想减少损失，该优化的是第一代摘要的质量，不是限制压缩代数。
> 清单里写的"限制压缩代数"这个解法**没有实现**，因为测量不支持它。

---

## §20 接进循环：只在轮次之间

清单 F06-11：压缩恰好发生在流式输出中间。

这条的修法不是加一个标志位，是**放对位置**：

```python
async def _maybe_compact(self, history: History) -> tuple[History, CompactionResult | None]:
    """Shrink the conversation if the next request would not fit.

    Called from exactly one place: the top of the loop, before the request
    is built.  That is F06-11, and it is enforced by where the call is
    rather than by a flag -- there is no path from inside `_collect` to
    here, so compaction cannot land in the middle of a stream and leave
    half a turn describing a history that no longer exists.
    """
```

循环里：

```python
for turn_index in range(self.max_turns):
    remaining = self.max_turns - turn_index

    history, compaction = await self._maybe_compact(history)
    if compaction is not None:
        compactions.append(compaction)
    ...
    messages = history.to_wire(self.dialect)
    estimated = self._sizer().messages(messages)
    turn = await self._collect(self.model.stream(messages))
```

`_collect` 里没有任何一条路径能到 `_maybe_compact`。这不是纪律，是**拓扑**。

### 20.1 两个阈值，不是一个

```python
COMPACT_AT = 0.75
COMPACT_TO = 0.45
```

为什么不用一个数？

> 压到触发线，意味着**下一轮又会触发**。中间那段差距，就是买到的轮数。

如果 `COMPACT_TO == COMPACT_AT`，会话会进入每轮压缩一次的状态：
每轮多一次 LLM 调用、每轮丢一点信息、每轮多一代摘要。

```python
def test_F06_11_the_trigger_and_the_target_are_not_the_same_number():
    """Compacting down to the trigger point means compacting again next turn."""
    from minicodex.agent import COMPACT_TO

    assert COMPACT_TO < COMPACT_AT
```

这两个数没有任何理论依据，是拍的。所以它们在 README 的
"Deliberately not done" 里明确标着"没有对任何东西调过参"。

### 20.2 校准是无条件做的

```python
if turn.usage is not None:
    self.calibration.observe(estimated=estimated, actual=turn.usage.prompt_tokens)
```

注意这段**不在** `if self.context_window is not None` 里面。
一次从不压缩的运行，照样会产生"我的估计器偏了多少"这个观测，
并且写进录像里：

```python
            self.recorder.record(
                "response",
                {
                    "turn": turn_index,
                    ...
                    "estimated_prompt_tokens": estimated,
                    "actual_prompt_tokens": turn.usage.prompt_tokens if turn.usage else None,
                    "calibration": self.calibration.describe(),
                },
            )
```

> 这是第 -1 章那条"请求/响应全量录制"在这一章的兑现方式：
> **每一次运行都在为下一次运行积累证据，哪怕这一次用不上。**

### 20.3 真跑一次

```
$ uv run minicodex ask "List every .py file under src/minicodex, then read
  src/minicodex/tokens.py and src/minicodex/compaction.py and tell me what
  CHARS_PER_TOKEN is and why boundaries() exists."
  --provider openai --context-window 3000 --sandbox-mode read-only --yes
```

```
### Details from `src/minicodex/tokens.py`
- **`CHARS_PER_TOKEN`**: Defined as `4.0`.

### Details from `src/minicodex/compaction.py`
- **`boundaries()`**: This function determines the valid cut points for
conversation compaction, ensuring that any call issued before a specific cut
index does not receive an answer after that index. ...

[gpt-4o-mini | completed after 2 turn(s)]
[sandbox_mode=read-only, approval_policy=on-request]
[compacted: dropped 4 item(s), summarised, generation 1, ~1316 tokens]
[tokens: x1.20 from 2 observation(s)]
[transcript: .minicodex\recordings\session-1786367981.jsonl]
```

压缩在中间触发了，答案跨过压缩仍然正确，校准比例 x1.20——
也就是实际比估计高 20%，方向和 §9 那张表一致。

`--context-window` 为什么必须手填、没有默认值？因为**这个程序无法发现这个数字**。
猜一个默认值出来，等于给用户一个静默错误的预算。help 文本直接把这句话写出来：

```
size of the model's context window in tokens. Compaction is off without it:
nothing in this program can discover the number, and a guessed one is a
silently wrong budget
```

---

## §21 属性测试，和它撞出来的东西

到这里，13 条清单故障都有代码和测试了，例子测试全绿。

但这一章有个别的章没有的性质：

> **这是本书第一个"有意思的输入是一个形状，而不是一个值"的模块。**

我写的每一个例子测试，用的都是我自己造的历史——而我造它的时候
已经知道边界在哪了。这些测试证明的是"代码在我想到的形状上是对的"。

所以加一类测试：随机生成历史，断言**性质**。

```python
def random_history(rng: random.Random) -> History:
    """A conversation of arbitrary shape that is nonetheless always valid.

    Built through `History`'s own API, so the generator cannot produce an
    illegal input even by accident -- which is what makes a failure downstream
    unambiguous: the compactor did it.
    """
```

用 `History` 的 API 来生成，是关键的一步：生成器**不可能**造出非法输入，
所以下游一旦失败，责任方唯一。

五条性质里最值得学的是第一条：

```python
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
```

它不是"断言 `boundaries()` 返回了我期望的东西"，
而是**让两个独立实现的定义互相验证**。计数器写错、集合差写错，都会让它们分歧。
一个人同时用两种错法写出同一个错误答案，概率很低。

其余四条：压缩后一定能发出去（两种方言都试）、受保护前缀逐字不变、
压缩不会让历史变大、`plan` 的切点一定在 `boundaries` 里。

默认 200 个种子，跑起来 1.2 秒，1000 个断言。全绿。

**然后把种子数调到 5000。**

```
FAILED tests/test_properties.py::test_property_compaction_never_grows_the_history[235]
FAILED tests/test_properties.py::test_property_compaction_never_grows_the_history[238]
FAILED tests/test_properties.py::test_property_compaction_never_grows_the_history[285]
... (数十个)
```

```
E       AssertionError: seed=235 before=89 after=93
E       assert 93 <= 89
```

**压缩把历史变大了。**89 token 进去，93 token 出来。

### 21.1 为什么会变大，以及为什么这不只是浪费

原因一句话就能说清：**压缩不是免费的**。它删掉一段消息，插入一条摘要。
当被删的那段很小的时候，摘要比它替换掉的东西还贵。

`plan()` 当时的逻辑是"找到第一个能装下的切点"，它从来没有问过
"这么切到底省不省"。

如果只是浪费一次调用，这算个性能问题。但它不是：

> 估计值降不到触发线以下 → 下一轮再次触发 → 再删一小段、再插一条摘要 →
> 估计值还是降不下来 →……
>
> **会话收敛到一个全是"摘要的摘要"的历史，而真正的工作内容一路被磨掉。**

一个偶尔多花几十个 token 的性能问题，和一个吃掉整个会话的死循环，
是同一行代码。

修法：

```python
def _do_nothing(fits: bool) -> Plan:
    return Plan(protected_count, protected_count, current, fits=fits, saving=0)

for cut in candidates:
    tail = [clip_item(i, max_tokens=max_item_tokens) for i in items[cut:]]
    size = _size(head, tail, sizer) + summary_budget
    if size <= budget:
        if size >= current and cut > protected_count:
            return _do_nothing(fits=current <= budget)
        ...
```

以及 docstring 里把它的来历写清楚——包括它**不是设计出来的**：

```
**A plan that would not save anything cuts nothing.**  That guard was not
designed in; a property test found it at seed 235, where compacting an
89-token history produced a 93-token one.  ...  Left in, the failure is not
one wasted call -- the estimate never drops below the trigger, so the next
turn compacts again, and the session converges on a history made entirely
of summaries of summaries.
```

改完，5000 种子全绿。加到 20000：

```
100000 passed in 306.28s (0:05:06)
```

### 21.2 属性测试抓到的，属性测试不一定守得住

这里有个陷阱，而且我掉进去了。

发现 bug 的那次运行是 **5000 个种子**。而默认是 **200 个**。
**seed 235 不在默认跑的集合里。**

也就是说：修完之后，`pytest` 依然全绿——但它绿是因为它根本没测到那个 case，
不是因为 bug 修好了。下次有人把守卫删掉，本地测试不会红。

所以属性测试抓到东西之后，**必须补一个例子测试**：

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
    """...
    Both branches, because the first version of this test only reached the
    fallback.  Mutation testing found that: disabling the guard *inside* the
    loop left every test green.  And the property run that found the bug in the
    first place used 5000 cases, while the default is 200 -- seed 235 is not in
    the set that runs on an ordinary `pytest`.  A property test that catches
    something is not the same as a test that keeps catching it.
    """
```

那个 `(12, 300, 1450, 400)` 是**搜出来的，不是想出来的**——
我第一次凭推理写了一组参数，结果它打的是函数末尾那个 fallback 分支，
循环里的守卫根本没被执行（变异测试告诉我的）。第二次也错了，
因为我把参数搜索用的 `output='w'*300` 写成了测试里的 `'w'*20`。

> **两次都是"我以为这组参数会走那条分支"。**
> 判断一个测试打到了哪条分支，不要靠读代码推理，
> 要么让变异测试告诉你，要么打印出来看。

### 21.3 CI 里加一步

这是第 6 章唯一的 CI 变更：

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

分层的道理和第 -1 章一样：**本地跑的那份要快到没人想跳过，
CI 跑的那份可以慢，因为它不占人的时间。**

### 21.4 没用 hypothesis

`hypothesis` 是 Python 属性测试的标准答案，它有 shrinking——
失败时能自动把 case 缩到最小。这里没用，理由和代价都写在文件头：

```python
"""...
No `hypothesis`: a seeded generator is enough here and costs no dependency.
The trade is real and worth stating -- there is no shrinking, so a failure
prints a seed rather than a minimal example, and reproducing it means running
`MINICODEX_PROPERTY_SEED=<n>`.  Chapter 14 revisits this.
"""
```

这个取舍在这里成立，是因为生成的历史本来就小（最多 14 轮），
seed 235 那个 case 直接就是可读的。历史再复杂一点，shrinking 就值那个依赖了。

---

## §22 变异测试

和第 5 章一样，最后一步是问：**这些测试真的有用吗？**

18 个变异，每个改一个决定，看有没有测试红：

```
mutation                                              failed  first tests to notice
--------------------------------------------------------------------------------------
boundaries: every index is a legal cut                   808  test_F06_01_02_03_boundaries_match...
boundaries: forget that a result closes a call           202  test_F06_01_02_03_boundaries_match...
protected: stop protecting the first user message          5  test_F06_04_summariser_is_also_given...
plan: do not reserve room for the summary                  2  test_F06_12_the_summary_budget_is_reserved...
plan: take the last legal cut instead of the earliest      3  test_F06_09_a_single_huge_result...
sizer: ignore the calibration when trimming the summary    1  test_F06_12_the_summary_is_measured_with_the_same_sizer
clip_item: keep the tail only                              1  test_F06_09_an_oversized_result_is_clipped_at_both_ends
compact: accept an empty summary                           1  test_F06_08_an_empty_summary_counts_as_a_failure
compact: do not show the summariser the protected prefix   1  test_F06_04_summariser_is_also_given_the_protected_prefix
generation: parse the last number in the header again      2  test_F06_13_generations_are_counted
plan: compact even when it would not save anything         1  test_a_compaction_that_would_not_save_anything_does_nothing
tokens: stop counting the tool schemas                     1  test_F06_07_estimate_counts_the_tool_schemas
tokens: drop the per-message framing cost                  1  test_F06_07_estimate_counts_a_cost_per_message
tokens: let a zero usage report set the ratio              2  test_F06_07_a_missing_usage_field_cannot_zero_the_ratio
tokens: size unknown content as empty instead of refusing  1  test_F06_10_multimodal_content_is_refused_not_guessed
model: stop asking for usage                               1  test_F06_07_usage_must_be_requested_explicitly
model: index into an empty choices list again              1  test_F06_07_the_usage_chunk_has_no_choices
agent: compact down to the trigger point                   1  test_F06_11_the_trigger_and_the_target_are_not_the_same_number
--------------------------------------------------------------------------------------
every mutation was caught
```

**18 条全被抓住。但"全被抓住"不是这张表的产出。**

产出是右边那一列的分布：

- 头两条各有 808 和 202 个测试失败——因为属性测试的每一个种子都会红。
  这类核心决定是被**淹没**在测试里的。
- **11 条只有 1 个测试能发现。**

第 5 章的那句话在这里依然成立：

> 变异测试的产出不是"测试有效"这个结论，是**这张分布图**。

那 11 条里的每一条，都意味着：删掉那一个测试，那个决定就再也没人守了。
而它们里面有 3 条，是这一章**实际发生过**的 bug（两把尺子、
不看是否省、空 choices）。

### 22.1 第一次跑，两条活了下来

第一版跑出来是这样：

```
2 mutation(s) survived -- nothing tests these decisions:
  - tokens: drop the per-message framing cost
  - model: index into an empty choices list again
```

第二条就是 §11.1 那个照抄解析循环的测试。第一条更简单：
`PER_MESSAGE_TOKENS = 4` 改成 `0`，全部测试仍然绿——
说明那个常数是个**没有任何东西依赖的数字**。

补测试的时候，注意它测的是什么：

```python
def test_F06_07_estimate_counts_a_cost_per_message_not_just_per_character():
    one = [{"role": "user", "content": "abcd" * 20}]
    many = [{"role": "user", "content": "abcd" * 4} for _ in range(5)]
    assert sum(len(m["content"]) for m in one) == sum(len(m["content"]) for m in many)
    assert estimate_messages(many) > estimate_messages(one)
    assert estimate_messages(many) - estimate_messages(one) == PER_MESSAGE_TOKENS * 4
```

**同样多的字符，拆成更多条消息，估算必须更贵。**
这才是那个常数存在的意义，而不是"它等于 4"。

### 22.2 变异脚本自己的事故

第 5 章的变异脚本有个 bug：它数错了行，报告全绿。这一章的第一条纪律就是从那儿来的
——数 `FAILED` 行、变异没生效就报错退出。

然后这一章的脚本出了**另一件事**。

跑到一半，进程被 Ctrl-C 打断了。脚本里有 `try/finally` 还原文件，
但 SIGINT 打断的是 pytest 子进程，父进程被直接杀掉，`finally` 没执行。

结果：`src/minicodex/tokens.py` 里留着一行 `if False:`。

然后我跑 `pytest`，**全绿**——因为那个变异当时正好还没有测试守着（就是 §22.1 那条）。

> **一个会改你源码的工具，是一个会把你的源码改坏的工具。**
> 而"改坏之后测试还是绿的"这件事，恰恰是它想帮你发现的问题的实例。

修法不是"记得别按 Ctrl-C"：

```python
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
```

开头快照全部文件，`atexit` 和 signal handler 都挂上，还原做成幂等的。

（真正正确的做法是在 git worktree 或临时副本里做变异，源码树根本不碰。
这里没做，因为教程的每个 step 目录不是独立的 git 仓库。这是一个**记在账上的缺口**。）

---

## §23 完整代码

### 23.1 `src/minicodex/tokens.py`（174 行）

模块级的两个常量和一个异常已经在 §8、§9、§17 给过了，这里是完整的两个函数和一个类：

```python
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
    ...
    It is not a general tokeniser and does not try to be.  It is a running
    answer to one question: for *this* conversation, how wrong am I?
    """

    def __init__(self) -> None:
        self._ratio: float | None = None
        self.observations = 0

    @property
    def ratio(self) -> float:
        return self._ratio if self._ratio is not None else 1.0

    @property
    def calibrated(self) -> bool:
        return self._ratio is not None

    def observe(self, *, estimated: int, actual: int) -> None:
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

### 23.2 `src/minicodex/compaction.py`（625 行）的骨架

`boundaries`、`Protected`、`Sizer`、`_replay`、`clip_item`、`_hard_summary`、
`SummaryRequest`、`_previous_summary` 都在前面给全了。剩下三块：

**`plan()` —— 所有决定都在这里做完：**

```python
def plan(
    items: Sequence[HistoryItem],
    *,
    budget: int,
    sizer: Sizer | None = None,
    summary_budget: int = SUMMARY_TOKEN_BUDGET,
    max_item_tokens: int = MAX_ITEM_TOKENS,
) -> Plan:
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
```

`fits=False` 为什么不抛异常？因为调用方**还是得发点什么出去**。
抛异常等于把"没办法"变成"崩溃"，而带着数字的 `fits=False`
让调用方能决定发什么（第 12 章的 F12-05 会用到它去触发一次带压缩的重试）。

**`compact()` —— 顺序是 plan → summarise → rebuild：**

```python
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

**`make_summariser()` —— 一次不带工具的模型调用：**

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
            raise CompactionError("summariser stream ended without a [DONE] sentinel")
        return "".join(parts)

    return summarise
```

**它自己开一个 `History`，不共用会话的**，这一条有三个理由，缺一不可：

1. 摘要器不该看到工具 schema——它没有工具可调，看到只会浪费 token 并诱导它输出调用；
2. 摘要器**不能**调工具——它的任务是读和写，不是干活；
3. 它的输出不能落进它正在总结的那个对话里。

---

## §24 文件清点

| 文件 | 行数 | 新增/修改 | 完整代码在 |
|---|---|---|---|
| `src/minicodex/compaction.py` | 625 | 新增 | §5、§6、§7、§13.1、§15、§16、§18.1、§19、§23.2 |
| `src/minicodex/tokens.py` | 174 | 新增 | §8、§9、§17、§23.1 |
| `src/minicodex/prompts/compaction.md` | 54 | 新增 | §12、§13.1、§19（分三段给全） |
| `src/minicodex/model.py` | 240 | 改 4 处 | §10、§11 |
| `src/minicodex/agent.py` | 306 | 改 6 处 | §20 |
| `src/minicodex/__main__.py` | 203 | 改 3 处 | §20.3 |
| `src/minicodex/__init__.py` | 42 | +1 函数 | — |
| `tests/test_compaction.py` | 666 | 新增，43 个函数 / 45 个用例 | 全章分散 |
| `tests/test_properties.py` | 147 | 新增，5 条性质 | §21 |
| `probe_naive_cut.py` | 141 | 新增 | §2 |
| `probe_cut_points.py` | 57 | 新增 | §3 |
| `probe_tokens.py` | 105 | 新增 | §8 |
| `probe_calibration.py` | 144 | 新增 | §9 |
| `probe_summary.py` | 220 | 新增 | §14、§19.2 |
| `probe_mutations.py` | 224 | 新增 | §22 |
| `.github/workflows/ci.yml` | +14 | 改 | §21.3 |

**没进正文的**：`Plan`、`CompactionResult` 两个 dataclass 的字段定义
（纯数据，每个字段的含义都在用到它的那一节解释过）；
`render_transcript` 的实现（§7 用到，但它是一段 15 行的 `isinstance` 分支，
不含任何决定）；`unused_call_ids`（诊断用，只被测试调用，实现在 §21 的测试里可见）。

自检方式：把本章所有 python 代码块拼起来，逐行 grep 源码。

```
$ uv run pytest
1250 passed, 8 skipped in 12.51s
```

其中 1000 个是属性测试（200 seeds × 5 条性质），205 个是前五章继承下来的，
45 个是本章的例子测试（43 个测试函数，其中两个参数化）。

---

## §25 收工：commit 与 review

### commit 序列

```
feat(history): find every legal cut point in a conversation

A cut is legal iff no call issued before it is answered after it.  Measured
against gpt-4o-mini rather than derived from the docs: cutting one
eight-message history at every index, indices 0/1/2/4/6 return 200 and
3/5/7 return 400.  boundaries() returns exactly that set.

The two naive rules are not "sometimes wrong", they are "never looking":
"drop the oldest half" is correct on this history only because 8 is even.

Probe: probe_cut_points.py
```

```
feat(history): never compact away the instructions or the question

The 400s above are the easy half.  Cut 6 of the same history is a 200 that
answers a different question -- "you are using Python 3.13.0" instead of
"the project supports 3.10 and above" -- because the message stating what
was asked is gone.

Protected is a prefix, not a set of types: chapter 0's turn-budget warning
is a SystemNote too, and protecting by type would accumulate every stale
"you have 2 turns left" forever.
```

```
feat(tokens): estimate a request, then correct the estimate from usage

chars/4 is right for English prose (1.04x) and wrong for everything an
agent history actually contains: JSON 0.44x, CJK 0.43x, shell output 0.49x.
The spread is 2.4x, so no divisor works, and every error is in the
direction that overflows the window before compaction fires.

Rather than tune the constant to this sample, use the number the server
already reported.  Within 4% from the third turn (probe_calibration.py).

Also counts two inputs the first version forgot: the tool schemas, re-sent
every turn, and a per-message framing cost.
```

```
fix(model): ask for usage, and survive the chunk that carries it

A streaming request returns zero usage chunks unless
stream_options.include_usage is set.  Nothing errors; the calibration
source simply never arrives and the estimate stays a third low forever.

Switching it on makes the last chunk arrive with "choices": [], which turns
chapter 1's chunk["choices"][0] into an IndexError -- after the answer has
already streamed.  Same shape as F02-07.

transport= is added for the test.  The first version of that test mirrored
the parsing loop into the test file, which left it green with the guard
deleted; see REVIEW.md.
```

```
feat(compaction): replace the dropped region with a structured summary

Six required sections, measured against a naive "summarise this" on a
transcript with five planted facts (probe_summary.py, 5 samples):
15/25 facts kept vs 23/25.  The naive prompt loses the remaining task 5/5
and the flag that a failing test discovered 5/5.

F06-05 (model redoes finished work) did NOT reproduce: 10/10 continuations
read the file before touching it, which is verification.  No code was
written for it.
```

```
fix(compaction): show the summariser the prefix it is not allowed to see

Protecting the first user message and asking for a "## Goal" section
interact: the model is ordered to state the goal and shown everything
except the goal, so it infers one.  3/3 on gpt-4o-mini, a session about
adding retry logic opened with "## Goal: Record the change in CHANGELOG.md"
-- the last remaining task, not the goal.

SummaryRequest.context carries the prefix as read-only material.  Neither
fix is wrong on its own; the fault is in the interaction.
```

```
fix(compaction): one ruler for both the reservation and the summary

plan() sized the cut with the calibrated estimate and the summary trimmer
used a bare len(text)//4 -- so the space reserved and the thing filling it
were measured differently, and a summary of paths and error strings
overshoots by the full 2.3x.

The bug is not that one call site was wrong, it is that there were two.
```

```
fix(compaction): a compaction that would not save anything cuts nothing

Found by a property test at seed 235: compacting an 89-token history
produced a 93-token one.  The summary has a fixed cost, so a small dropped
region is a net loss.

Not merely wasteful: the estimate never drops below the trigger, so the
next turn compacts again, and the session converges on a history made
entirely of summaries of summaries.

Adds tests/test_properties.py (5 properties over generated histories) and
a CI step running it at 2000 cases -- seed 235 is outside the local
default of 200.
```

八个 commit。每一个都能独立通过测试，每一个的 body 都在回答
**"为什么这么改"**和**"为什么不用另一种方案"**，而不是复述 diff。

注意后三个是 `fix(...)` 而不是 `feat(...)`，而它们修的是**本章自己刚写的代码**。
把它们压进前面的 feat 里会更"干净"，但那样就抹掉了三个真实的发现过程——
其中两个是工具（真机测量、属性测试）发现的，不是我想出来的。

### PR 描述（节选）

```markdown
## What

The agent now compacts its own history when the next request would not fit:
the middle of the transcript is replaced by a six-section summary, and the
instructions and the original question are never touched.

## Why

Chapter 5's agent works until the conversation stops fitting, and then it
stops working in a way that is hard to read: sometimes a 400 with a
misspelled message, sometimes a fluent answer to a question nobody asked.

## How

Three decisions, each measured rather than reasoned:

1. **Where a cut is legal** -- posted the same history cut at every index to
   a real server. `boundaries()` reproduces the 200 set exactly.
2. **When to compact** -- `chars/4` is 34-38% low on agent content, always
   in the dangerous direction. Corrected from reported usage; within 4%
   from the third turn.
3. **What the summary must contain** -- six sections vs "summarise this":
   23/25 planted facts kept vs 15/25.

## Testing

- 1250 tests (1000 property, 45 new example, 205 inherited)
- 18 mutations, all caught; 11 of them by exactly one test
- property tests clean at 20000 cases (100000 assertions, 5m06s)
- one real end-to-end run against gpt-4o-mini with `--context-window 3000`:
  compaction fired mid-run, the answer survived it, calibration settled at
  x1.20

## Not done

- no per-modality token estimation. `estimate_messages` **raises** on
  non-string content rather than counting an image as free (F06-10)
- `plan(...).fits == False` is recorded but not acted on. The right response
  is a compact-and-retry, which is chapter 12 (F12-05)
- the trigger/target fractions and the 700-token summary budget are not
  tuned against anything
- F06-05 not reproduced, no code written for it
```

### Code review

我扮演 reviewer，五条：

---

**R1（正确性，必须改）**
`_previous_summary` 用"header 里最后一个数字"当代数。header 里有消息数和时间戳，
所以第二次压缩的代数会是 24，第三次是分钟数。

> **回应**：已修，锚定到 `generation` 这个词后面。
> 补充一句：这个 bug 是测试发现的不是 review 发现的，因为读代码时
> "找 header 里的数字"听起来很合理——只有把 header 的实际内容摆出来才显然是错的。

---

**R2（可测试性，必须改）**
`test_F06_07_the_usage_chunk_has_no_choices` 在测试文件里重新实现了一遍
`stream()` 的解析循环。这个测试测的是那份副本。

> **回应**：变异测试独立地发现了同一件事（删掉 `model.py` 里的守卫，
> 该测试仍绿）。已改为通过 `httpx.MockTransport` 驱动真正的 `stream()`。
> 为此在 `ChatCompletionsModel` 上加了 `transport` 参数——
> 一个只为测试存在的参数，这里是值得的，因为**没有它就没有测试**。

---

**R3（设计）**
`Sizer` 只有一个实现，看起来像仪式性抽象（FB-03 警告过的那种）。

> **回应**：不接受。它不是为了多态存在的，是为了**唯一性**：
> "所有尺寸判断必须用同一把尺子"这条不变量，散在三个调用点上时已经破过一次
> （§18.1）。这符合 `PLAN.md` §2 的第三条例外——不变量需要被强制。
> 判断标准：如果把它拆回三个自由函数，那条不变量还成立吗？不成立。

---

**R4（边界情况，接受一半）**
`plan()` 在 `fits=False` 时返回而不是抛异常，调用方可能忽略这个字段。

> **回应**：返回是对的（调用方还是得发东西出去），
> 但目前只有 `recorder` 记录了 `fits`，`Agent` 没有对它做任何事。
> **这是一个真实的缺口**，已记入 PR 的 not-done 和 README：
> 装不下时应该触发一次带压缩的重试，那是第 12 章 F12-05 的内容。
> 现在就写会是一个没有测量支撑的猜测。

---

**R5（命名，不改）**
`clip_item` 只处理 `ToolResult`，名字听起来像处理任意 item。

> **回应**：它确实接受任意 item 并原样返回非 `ToolResult`，这是刻意的：
> 调用点是列表推导，加类型判断会让三个调用点都变丑。
> 不改名，但补了 `test_F06_09_only_results_are_clipped` 把这个行为钉住，
> 这样"它对别的类型是恒等函数"从一个偶然变成一个契约。

---

## §26 codex 是怎么做的

（以下基于源码结构的观察；commit 历史无法考证的部分标注为**推断**。）

**`compact_remote.rs` / `compact_remote_v2.rs` / `compact_remote_v2_attempt.rs` 并存。**
这是 `PLAN.md` 开头那张"人类痕迹"表里的一行：**上下文压缩被推倒重来过不止一次**，
而且 v2 旁边还留着一个 `_attempt`。三个文件同时存在于仓库里，
说明迁移是渐进的，旧路径没敢删（**推断**：因为线上还有会话在用旧格式）。

一个只写过一次就写对的模块，不会长成这个样子。

**codex 的压缩是"remote"的。**文件名里的 `remote` 指压缩发生在服务端而不是客户端。
这是一个我们做不到的架构选择，但它的动机可以推断：服务端知道真实的 token 数
（不需要 §9 那套校准），也知道自己的 tokenizer。

**这恰恰反过来说明了 §8–§10 那半章的性质**：本地估算 token 是一个
"因为拿不到真相所以必须做"的妥协，不是一个值得骄傲的设计。
能拿到真相的一方不会这么做。

**`core/tests/suite/compact_resume_fork.rs`。**
这个测试文件名把三件事连在了一起：压缩、恢复、分叉。
我们这一章只做了第一件；第 7 章会做后两件。文件名的存在说明
**这三件事的交互本身就是一类故障**——一个压缩过的会话被恢复、
再从中间分叉出去，是三个特性两两相乘的组合。

**`prompt_for_compact_command.md`。**codex 把压缩 prompt 也放在独立的 md 文件里，
和它的其它几份 prompt 并列。和我们 §12 的做法一致，理由也应该一样：
它是会被当成散文来读、来改的东西。

---

## §27 回头看：这一章撞到了什么

| 故障 | 怎么暴露的 | 挡住它的东西 |
|---|---|---|
| 砍一半在偶数历史上碰巧合法 | 🟢 逐点实测 | 按"未应答调用数"判定，不按位置 |
| `T[x]` 成孤儿 → 400 | 🔴 真发请求 | 同上 |
| **切法合法但答的是另一个问题** | 🟡 静默（读答案才发现） | 受保护前缀 |
| 按类型保护 `SystemNote` → 过期预算警告永久堆积 | 🟣 想到第 0 章那条 | 按位置不按类型 |
| 直接切列表会绕过第 1 章的不变量 | 🟣 设计时 | `_replay` 走 `add_*` |
| `chars/4` 对 JSON 偏 2.3 倍，且方向是低估 | 🟠 实测 | 用服务端 usage 校准 |
| **工具 schema 一个 token 都没算** | 🟠 加 `--with-tools` 才看见 | 计入 schema + 每条开销 |
| **流式请求默认不返回 usage，校准数据从未到达** | 🟡 静默（机制正确但从未运行） | `stream_options.include_usage` |
| 打开 usage 后 `choices` 为空 → IndexError | 🔴 崩溃（答案已流完） | 判空跳过，先取 usage |
| **摘要器没看到目标，于是编了一个** | 🔵 真机 3/3 | `SummaryRequest.context` |
| 朴素摘要 5/5 丢掉"接下来干什么" | 🔵 真机 5/5 | `## Open` |
| 朴素摘要 5/5 丢掉失败换来的 flag | 🔵 真机 5/5 | `## Key data` |
| 结构化摘要仍有 2/5 丢掉同一个 flag | 🔵 真机 | **未解决**，写进结论 |
| 摘要器挂了 → 静默失忆 | 🟢 边界测试 | 降级摘要 + 明确告知模型 |
| 空摘要被当成成功 | 🟢 边界测试 | 显式判 `strip()` |
| 单条巨大输出，丢多少旧的都没用 | 🟢 边界测试 | 历史层的头尾截断 |
| 多模态 content 被算成 0 token | 🟢 边界测试 | 抛异常，不假装 |
| **摘要预留和摘要裁剪用了两把尺子** | 🟣 写第二处时发现 | `Sizer` |
| 代数解析读到时间戳里的数字 | 🔴 测试红 | 锚定到 `generation` 一词 |
| **压缩把 89 token 变成 93 token → 压缩死循环** | ⚪ 属性测试 seed 235 | 不省就不压 |
| 而 seed 235 不在默认的 200 个种子里 | ⚪ 变异测试 | 补例子测试 |
| 例子测试打错了分支（两次） | ⚪ 变异测试 | 搜参数，不推理 |
| 测试照抄了解析循环，测的是副本 | ⚪ 变异测试 | `MockTransport` 打真代码 |
| `PER_MESSAGE_TOKENS` 没有任何测试依赖 | ⚪ 变异测试 | 断言"拆得越碎越贵" |
| **变异脚本被 Ctrl-C 打断，把变异留在了源码里** | ⚫ 自己踩 | 快照 + atexit + signal |

标记：🔴 崩溃 · 🟡 静默 · 🟢 边界测试 · 🔵 长跑/真机 · 🟠 看日志 · 🟣 review · ⚫ 用户报告 · ⚪ lint/测试工具

**清单上 13 条，实际 25 条。崩溃只有 2 条。**

多出来的 12 条里，**5 条是验证工具自己的问题**（测试测了副本、
测试打错分支、属性测试的默认规模、没人依赖的常量、变异脚本毁坏源码）。

> 第 5 章的结论是"一个用来检查别的东西的模块，最容易以为自己检查过了"。
> 这一章把它推进了一层：
>
> **检查那个检查器的工具，同样会以为自己检查过了。**
>
> - 属性测试跑 200 个种子说没问题，跑 5000 个说有问题
> - 变异测试说"被抓住了"，但抓住它的是测试文件里的一份副本
> - 变异脚本说"全绿"，因为它自己把源码改坏之后没还原
>
> 每加一层验证，就多一层"验证本身失效"的可能。**唯一的出路不是再加一层，
> 是让每一层的失效方式互不相同**——属性测试的失效方式（规模不够）
> 和变异测试的失效方式（测了副本）不一样，所以它们抓到了对方漏掉的东西。

---

## 如果你只记住三件事

1. **报错的那一半是容易的一半。**
   同一个历史，切在结果上返回 400，切在调用上返回 200——而 200 里有一个
   答的是另一个问题。前者三分钟修完，后者要你先想明白"什么信息一旦丢了
   就再也推不回来"。**每次你写一个删除操作，问一遍：删掉之后，
   剩下的东西还知道自己是干什么的吗？**

2. **拿不到真相的时候，别调参数，去找真相。**
   `chars/4` 对散文准、对 JSON 差 2.3 倍，两端差 2.4 倍——没有任何常数是对的。
   正确答案不是调一个更好的常数，是**服务器每次都在告诉你实际花了多少**。
   而这个数据默认不发，得显式要；要了之后还会撞碎五章没动过的解析代码。
   **"我没有这个数据"和"我没去要这个数据"，经常是同一件事。**

3. **验证工具会以为自己验证过了，而且不止一层。**
   属性测试跑 200 个种子说没事、5000 个说有事；变异测试抓到了 bug，
   但抓它的测试是一份副本；变异脚本被打断后把源码改坏了，而测试是绿的。
   **每一层验证都需要一个失效方式和它不同的邻居。**
   这一章 25 条故障里有 5 条出在验证工具本身，比出在压缩逻辑上的还多。

---

## 动手练习

1. 把 `boundaries()` 换成"不要切在 `ToolResult` 上"的版本，跑测试。
   例子测试**全绿**，属性测试红。找到那个种子，看它生成的历史长什么样。
   然后想清楚：为什么我写的 45 个例子测试一个都没发现它？

2. 把 `stream_options` 那一行删掉，用真 key 跑一次
   `minicodex ask ... --context-window 3000`。观察 `[tokens: ...]` 那行。
   **不会有任何报错**，压缩照常工作，只是估计一直偏低。
   然后回头看 §10 那句"整个第 9 节的机制存在、正确、并且从未运行过"。

3. 把 `MINICODEX_PROPERTY_CASES` 调到 50000，跑
   `test_property_compaction_never_grows_the_history`，然后把
   `_do_nothing` 那两个守卫删掉。记下第一个失败的种子，
   **然后写一个例子测试复现它**——不是参数化随机，是写死的输入。
   做完之后你会明白 §21.2 那句"属性测试抓到的，属性测试不一定守得住"，
   以及为什么我第一次和第二次写的参数都打错了分支。

4. 给 `estimate_messages` 实现按模态估算：文本照旧，图片按
   `(width/512) * (height/512) * 170 + 85` 估。
   **然后你会发现你需要图片的尺寸，而 `content` 里只有一个 base64 或一个 URL。**
   那正是 §17 拒绝去猜的原因，也是这个练习真正要让你摸到的东西。

5. 把 `probe_summary.py` 里的五个事实换成你自己项目里的五个，跑一遍。
   **特别注意 C 那一类**——"失败一次才试出来的东西"。
   统计它在你的任务上的存活率，然后决定：它值不值得进受保护前缀。

下一章：Ch07 · 中断与恢复——进程挂了，历史怎么回来。
而这一章刚刚证明了摘要是有损的，所以落盘的必须是原文，不是摘要。
---

# 附录 · 代码到底怎么敲出来的（新手向，逐段精讲）

跟第 4 章附录同一个规矩：正文讲"为什么"，这里补"怎么写"。默认你已经读过
前面几章的附录（`dataclasses`、`asyncio`、`History` 基础），这里只讲这一章
正文 §24 明确说"没进正文"的部分。代码摘自
`steps/step06_compaction/src/minicodex/compaction.py` 和 `tokens.py`，
逐段核对过。

先把范围说死：

1. 本附录只解释第 6 章在 `steps/step06_compaction/` 里新增的代码。正文
   §24 的清点表说得很清楚：`boundaries`/`Protected`/`_replay`/
   `_hard_summary`/`clip_item`/`Sizer`/`plan`/`compact`/`make_summariser`
   都在正文各节给了完整代码，**不再重复**。这里补 §24 明说"没进正文"的：
   `Plan`/`CompactionResult` 两个 dataclass 的字段定义、`render_transcript`
   的实现、`unused_call_ids`；以及正文只有片段或散见各节的
   `_previous_summary`、`_size`、`tokens.py` 的 `estimate_messages` 和
   `Calibration`。
2. 完整函数代码块里不使用代表遗漏实现的省略号，也不把关键分支写成
   "此处略"。示例字符串或命令文本里出现的三个点，如果本来就是源码的
   一部分，会原样保留。
3. 代码块按当前源码核对。

## O1 · `Plan` 与 `CompactionResult`：两个纯数据类的字段

正文 §24 自述"纯数据，每个字段的含义都在用到它的那一节解释过"。完整定义：

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
```

四个字段设计要点：

1. **`Plan` 是"决定"和"执行"分离的产物**（docstring 明说）——所有决策在
   `plan()` 里做完，`compact()` 只是机械执行。所以 `Plan` 能被打印、断言、
   在**没有模型**的情况下测试。
2. **`saving=0` 是"什么都不做"的真实结局，不是失败**——正文 §21 属性测试
   在 seed 235 找到：压缩 89-token 历史反而得到 93-token，于是加了
   "不会节省就切不掉"的守卫。`saving` 默认 0 让"do nothing"成为合法状态。
3. **`drops` 是 `cut - protected` 的属性**——要剪掉多少，由两个下标算出，
   不存多余字段。
4. **`CompactionResult.degraded` 区分"正常摘要"和"降级摘要"**（
   `_hard_summary` 的产物）——`describe()` 把状态说清楚，`compacted:
   dropped 3 item(s), degraded, generation 1, ~1200 tokens`。

## O2 · `render_transcript`：给摘要模型的文本

正文 §7 用到它但没给实现，§24 自述"15 行 isinstance 分支，不含任何决定"。
完整实现：

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

三个要点：

1. **不是 `to_wire()`。** 这段文本要作为**内容**送进摘要 prompt，而
   `to_wire` 的 JSON 嵌套（`{"role": "tool", "content": ...}`）会为摘要
   模型不需要的标点花 token。这里是"给人读的扁文本"。
2. **工具结果保留调用名**（`RESULT OF read_file:` vs `RESULT OF
   run_shell:`）——"read_file 的输出"和"run_shell 的输出"对读它的人含义
   完全不同，丢了名字摘要就分不清。
3. **四种类型四种前缀**：`USER:` / `SYSTEM:` / `ASSISTANT:` /
   `RESULT OF {name}:`。`isinstance` 分支按类型分派，`else` 不需要——四个
   `HistoryItem` 变体全部覆盖。

## O3 · `_previous_summary`：上一代的摘要和代数

正文 §15/§16 提到"generation"，`_previous_summary` 的实现散在正文里
（§15 有片段）。完整实现：

```python
def _previous_summary(items: Sequence[HistoryItem]) -> tuple[str | None, int]:
    for item in reversed(list(items)):
        if isinstance(item, SystemNote) and item.text.startswith(SUMMARY_MARKER):
            header, _, body = item.text.partition("\n")
            tokens = header.replace("]", " ").split()
            generation = 1
            for index, token in enumerate(tokens[:-1]):
                if token == "generation" and tokens[index + 1].isdigit():
                    generation = int(tokens[index + 1])
                    break
            return body.strip() or None, generation
    return None, 0
```

四个细节：

1. **`reversed(list(items))` 从后往前找**——最新的摘要（如果有）在最后。
   `reversed` 需要序列，`list(...)` 包一层保证。
2. **`SUMMARY_MARKER` 是 `"[compacted transcript"` 前缀匹配**——system note
   以这个开头就是摘要。为什么不加一个 `HistoryItem` 变体？docstring 明说：
   模型必须把它读成"指令形状的 note"和别的 note 一样，而新变体要在每个
   方言加渲染规则，无收益。
3. **generation 解析是"锚定 `generation` 这个词"**，不是"读 header 里的
   数字"。注释里记了事故：宽松版读 header 里**最后一个数字**，结果从
   `[compacted transcript | generation 1 | 24 message(s) replaced | 2026-08-10
   06:22]` 读到 `24`，然后 `22`——代数计数器被时间戳里的数字污染了。
   `enumerate(tokens[:-1])` + `token == "generation" and tokens[index+1].isdigit()`
   只认 `generation` 后面那个。
4. **`header.replace("]", " ").split()`**——把 `]` 换成空格再切词，因为
   标题是 `[compacted transcript | generation 1 | ...]`，`]` 贴着最后一个
   token。返回 `(body.strip() or None, generation)`——空 body 归一成
   `None`（没有上一代摘要）。

## O4 · `_size`：一条边界的代价

正文 §15 提到但实现散见：

```python
def _size(head: Sequence[HistoryItem], tail: Sequence[HistoryItem], sizer: Sizer) -> int:
    scratch = History()
    _replay(scratch, list(head) + list(tail))
    return sizer.messages(scratch.to_wire())
```

- **为了量尺寸，先建一个临时 History 再 `to_wire()`**——因为 `sizer` 的
  `messages()` 接收的是 wire 格式消息列表（模型请求的真实形状），而
  `History` 是类型化对象。`_replay` 把类型化条目重新从"前门"放进去
  （保证 invariant 检查照常跑），`to_wire()` 渲染成 wire 形状，`sizer`
  量它。
- 注意 `_replay` 在这里的**第二个用途**（正文 §19 只讲了它在"重建历史"
  的用途）：量尺寸也要走同一个门，保证"量的东西"和"真要发的东西"形状
  一致。

## O5 · `tokens.py`：估算与校准

正文 §8/§9/§17/§23 给了 `estimate_messages` 和 `Calibration` 的片段。完整
实现（注意与正文片段的差异：`_content_chars` 只接受字符串或 `None`，
非文本内容直接抛错，而不是递归处理）：

```python
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
```

三个新手容易漏的点：

1. **`_content_chars` 只建模文本**（docstring 明说 F06-10）：`None` 计 0，
   `str` 量长度，**其他类型抛 `UncountableContent`**——正文 §17 的"拒绝
   去猜"。`len()` 对两元素 list 是 2，除以 4 是 0，图片会被算成免费——
   一个把最大条目静默估成零的预算比没有预算更糟（docstring 原话）。
2. **`estimate_messages` 遍历消息 + 工具调用**：`message.get("tool_calls")`
   里的 function name/arguments 也算进字符数（它们确实会出现在请求里）。
   `tools` 参数用 `json.dumps(list(tools))` 量整个工具列表——docstring 记
   了事故：第一版漏掉 tools，而两个玩具 schema 就占 53 token，是短会话的
   整个预算。
3. **公式是 `chars / chars_per_token + PER_MESSAGE_TOKENS * len(messages)`**
   ——每条约 4 token 的固定开销加上内容字符数。

`Calibration` 的完整实现（正文 §9 只给了 `observe` 一行）：

```python
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

与正文片段不同的细节：

- **`_ratio` 初始是 `None`，`ratio` 属性回落到 1.0**——"没校准"和"校准
  比值恰好是 1.0"是两回事（`calibrated` 用 `is not None` 判断）。
- **`observe` 守卫 `estimated <= 0 or actual <= 0`**：provider 不发 usage
  时 `actual` 是 0，直接设比值会把一切都变成免费。docstring 明说
  "guarded rather than trusted"。
- **`_ratio` 保留最后一次观测、无平滑**——正文 §8.1 的 x250 事故（第 12
  章才加钳位）。
- **`describe` 未校准时说 "uncalibrated (no usage reported yet)"**，校准后
  说 `x1.20 from 3 observation(s)`。

## O6 · `unused_call_ids`：诊断工具

正文 §24 自述"只被测试调用，实现在 §21 的测试里可见"。完整实现：

```python
def unused_call_ids(items: Sequence[HistoryItem]) -> tuple[str, ...]:
    issued = {
        call.call_id
        for item in items
        if isinstance(item, AssistantMessage)
        for call in item.tool_calls
    }
    return tuple(i.call_id for i in items if isinstance(i, ToolResult) and i.call_id not in issued)
```

- 第一行集合推导收集**所有发出过的调用 id**（assistant 消息里的 tool_calls）；
- 第二行找**有结果但没对应调用**的 ToolResult。它存在的意义（docstring）：
  `_replay` 让这个状态在生产里不可达，但测试需要**指向一个天真切片、并
  用服务器同样的词汇说出它哪里错了**。

## O7 · 新手常见报错/坑对照表

| 症状 | 原因 | 修法 |
|---|---|---|
| 压缩后请求 400（tool 没有对应调用） | 切在"有调用没结果"处 | `boundaries()` 用 open_calls 计数，`ToolResult` 减一 |
| 压缩后模型答非所问 | 把第一条 user 消息也切了 | `Protected.of` 保护开头的 system notes + 第一条 user 消息 |
| 摘要和真正丢的区域对不上 | 先摘要后决定切哪 | `compact` 顺序是 plan → summarise → rebuild |
| 摘要超预算，历史还是超长 | 用裸 `len(text)//4` 剪摘要 | `Sizer.clip_text` 用同一个校准过的 sizer |
| 压缩后反而更大了 | 没检查"是否节省" | `saving <= 0` 就 do nothing（属性测试 seed 235 发现） |
| 代数计数器乱跳 | 读 header 里最后一个数字 | 锚定 `generation` 这个词后面那个数字 |
| 图片内容让估算崩掉 | 估算器硬算 | `UncountableContent` 抛出，正文 §17 拒绝去猜 |
| 一次观测把校准比值拉到 x250 | `observe` 无钳位 | 第 12 章加 `MAX_REFUSAL_CORRECTION`（本附录不展开） |
| 摘要流断了一半 | 没检查 `[DONE]` | `make_summariser` 里 `saw_end` 标志，无 sentinel 抛 `CompactionError` |
