# 第 9 章 · 工具太多

> **代码**：`steps/step09_mcp/`
> **分支**：`feat/mcp`
> **产出**：Agent 能用它自己没写的工具——从配置文件里启动的 MCP server 里来，
> 名字不会撞车，结果被归一成一个字符串，六十个工具的 schema 不会悄悄变成整个请求
> **你需要**：本章的测试不联网。
> `probe_mcp.py` 里有四节要真的调 API（`gpt-4o-mini`，几十次请求）。
> 依赖多一个：官方 MCP SDK（`mcp>=2.1`）。

---

## §1 这一章要做出来的东西

到第 8 章为止，Agent 有四个工具：`read_file`、`apply_patch`、`run_shell`、
`request_permissions`。四个都是我们自己写的，所以每一个问题都有一个我们说了算的
答案——参数是什么形状、返回什么、碰了哪些文件、出错时说什么话。

这一章要接上的东西，一条都不成立。

MCP（Model Context Protocol）是一个约定：一个独立的进程，通过 stdio 或者 HTTP
说 JSON-RPC 2.0，告诉你它有哪些工具、每个工具的 JSON Schema 长什么样，然后接受
你的调用。装一个就多一批工具。装六个，你的 Agent 突然有六十个工具，其中
五十六个的描述是别人写的，参数命名是别人定的，什么时候崩是别人决定的。

清单上给这一章列了九条故障。写完之后，**九条里有两条完全没复现**，其中一条
（F09-02）我用三种不同的构造去撞它，前两次"复现成功"事后都被证明是我自己的
测量方法造出来的假象。而这一章最重要的发现，恰恰是在追查那两次假象的过程中
掉出来的——它说的是：**清单上给 F09-02 开的那副药（两级加载），实测下来
不但没治病，还让病更重了。**

先从能看见它动的那一步开始。

---

## §2 协议归 SDK，剩下的归你

要跟一个 MCP server 说话，得先有一个 MCP server。

这里有一个选择：装一个现成的（`@modelcontextprotocol/server-filesystem` 之类），
还是自己写一个。**自己写。** 理由和第 8 章为什么要自己插延迟是同一个：
借来的 server 你没法让它在指定的时刻崩。这一章要撞的九条故障里，
至少有四条需要一个"按要求出故障"的 server——启动慢的、跑到一半死的、
反过来问你问题的、返回一堆奇怪 content block 的。

codex 自己也是这么干的：`rmcp-client/src/bin/` 下面躺着四个
`test_*_server.rs`，全是 OpenAI 自己写的测试用 MCP server。

客户端那边不用自己写。协议有官方 SDK，装上就行：

```python
from mcp import Client

async with Client(params) as client:
    tools = await client.list_tools()
    result = await client.call_tool("stat", {"name": "pyproject.toml"})
```

`initialize`、版本协商、能力交换、`notifications/initialized`、
帧的收发、请求 id 的分配——一行都不用写。

> **对照 codex**：它也不自己写。workspace 里钉的是官方 Rust SDK
> （`rmcp = { version = "=3.0.0" }`，`codex-rs/Cargo.toml:393`）。

这本书从第 -1 章起就依赖 `httpx` 而不是自己写 HTTP，依赖 `pytest` 而不是
自己写测试框架。分界线一直都在，这里说清楚一次：

> **教什么，就手写什么；不教的，就依赖它。**

这一章教的**不是**消息分帧。是下面这些：

| 这一章要自己决定的 | 在哪 |
|---|---|
| 两个 server 都叫 `search` 怎么办 | §4、§5 |
| 工具名的长度上限和字符集（供应商说了算，不是你） | §5 |
| 六十个工具的 schema 花多少钱，超了怎么办 | §6、§7 |
| 一个远程结果有七种形状，怎么变成一个字符串 | §8 |
| 启动超时和调用超时是**两个**预算 | §9.1 |
| 别人的进程死了，怎么跟模型解释 | §9.2 |
| 子进程能看见哪些环境变量 | §9.4 |
| 一个你没写的 server，能碰到什么 | §11 |

`mcp.py` 里剩下的就是这些——499 行，其中一半是解释「为什么」的注释；
`registry.py` 是它们的大头。

### 2.1 一个顺带的好处：测试里可以跑真 server

`mcp.Client` 还接受**一个 server 对象**，进程内直连：

```python
async with Client(server) as client:      # server 是一个 MCPServer 实例
```

测的仍然是**真的协议**——真分帧、真 initialize、真错误形状——只是没有进程边界，
所以快得多。

**但这一章的测试没有用它**，理由值得说清楚，因为它就是这一章的主题：
这里九条故障里有一半需要一个**会死的进程**——启动时卡住的、跑到一半退出的、
崩溃时往 stderr 写一行的。进程内的 server 死不了，它就是你自己。

还有一个更硬的边界，是量出来的：进程内那条传输**没有反向通道**。

```
NoBackChannelError: Cannot send 'elicitation/create': this transport
context has no back-channel for server-initiated requests.
```

所以：**进程内那条路适合测逻辑，不适合测进程。** 这个边界值得在你围着
那个快选项设计整套测试之前就知道。

---

## §3 一个依赖是一个断言，断言要有测试

MCP 的那根管道上跑的不只是"你问一句、它答一句"。server 可以随时发
**通知**（`notifications/message`，日志；`notifications/tools/list_changed`，
工具列表变了），也可以**反过来向你发请求**（`elicitation/create`，
问用户要个确认）。三种消息共用一根管道。

一个"写一行、读一行"的客户端会把日志通知当成 `initialize` 的结果，
然后**每一个答案都往后错一格，从头到尾没有任何异常**——`result` 这个 key
根本不存在，`.get("serverInfo")` 安静地返回 `None`，Agent 带着一个空的
工具表继续跑。这是 §6.2 分类表里的 🟡：不报错，结果错。

分帧是 SDK 的事，所以这个故障不会发生。**但这一章仍然为它留了一条测试**：

```python
async def test_a_notification_is_not_the_answer_to_the_last_request() -> None:
    client = McpClient(files(MCP_LOG_NOISE="1"))
```

`MCP_LOG_NOISE` 让 server 在每次回复前先发一条完全合法的日志通知
（`await ctx.info(...)`），然后断言工具表和调用结果都还是对的。

这是这一章的第二条规矩：**一个依赖是一个断言，而断言要有测试。**
"分帧是对的"现在是别人的承诺，而承诺要验。将来 SDK 升级、行为变了，
这条测试会红——而不是某天在某个用户那里表现为"工具表是空的"。

### 3.1 生命周期：唯一要自己接的一处

`mcp.Client` 是个异步上下文管理器，而 `McpClient` 要的是显式的
`start()` / `close()`——一个会话同时开着好几个 server，在**会话**结束时才关，
不是在某个 `with` 块退出时。`AsyncExitStack` 是这座桥，**而它就是全部的适配**：

```python
stack = AsyncExitStack()
client = await asyncio.wait_for(
    stack.enter_async_context(Client(target, ...)),
    timeout=self.config.startup_timeout,
)
```

整个适配就这三行。超时是我们加的——SDK 不强加期限，而 F09-04 说必须有。

---

## §4 两个 server 都叫 `search`

现在接第二个 server。`mcp_servers/notes_server.py`，管笔记的，它也有一个
工具叫 `search`——搜笔记。两个 server 由两拨互不相识的人写的，
都给最显然的工具起了最显然的名字，谁都没错。

最直接的合并方式，一个 dict：

```python
table = {}
for c in clients:
    await c.start()
    for tool in await c.list_tools():
        table[tool.name] = (c, tool)
```

```
tools the model is shown: ['remember', 'render', 'search', 'stat']
search belongs to: notes
model asks for a file containing 'hatchling':
  -> {'content': [{'type': 'text', 'text': 'no notes match'}]}
```

清单把 F09-01 标成 🔴 崩溃。**它不崩溃。** 五个工具声明进来，模型看到四个；
`files` 的 `search` 被 `notes` 的 `search` 覆盖掉，从此不存在；然后模型问
"哪个文件里有 hatchling"，得到一句格式完整、语气笃定的 `no notes match`。

`pyproject.toml` 里当然有 `hatchling`。没有人会知道。

`probe_mcp.py collide` 把这一步固定下来（不需要联网）：

```
=== F09-01: two servers, one table ===
    naive dict:      3 tools from 4 declared -> {'search': 'notes', 'stat': 'files', 'render': 'notes'}
    namespaced:      4 tools -> ['mcp__files__search', 'mcp__files__stat', 'mcp__notes__render', 'mcp__notes__search']
```

---

## §5 名字要重写，调用不能重写

修法一句话就能说清：模型看到的名字里带上 server 名。

```python
def model_name(server: str, tool: str) -> str:
    return f"{PREFIX}{DELIMITER}{sanitize(server)}{DELIMITER}{sanitize(tool)}"
```

`mcp__files__search` 和 `mcp__notes__search`。

但这里有一个必须想清楚的地方：**改的只是模型看到的名字，发给 server 的
名字一个字都不能变。** server 认识的是 `search`，你把 `mcp__notes__search`
发过去它只会说不认识。所以 `RemoteTool.name` 存的是原始名字，
`Registration.model_name` 存的是改写后的名字，两者分开存，
调用的时候用前者：

```python
result = await registration.client.call(registration.tool.name, arguments)
```

那 `sanitize()` 是干什么的？我第一版写它的时候，理由是"名字里可能有奇怪
字符，规整一下比较好"——这是一个凭感觉写的防御，按第七点五条纪律，
这种代码要么有实测支撑，要么不该存在。

于是去量。`probe_mcp.py names` 直接把各种形状的函数名发给真实 API 看它怎么骂：

```
=== tool-name limits, measured against the real API ===
    64 chars             accepted, model called 'aaaa...'
    65 chars             accepted, model called 'aaaa...'
    128 chars            accepted, model called 'aaaa...'
    256 chars            HTTP 400: Invalid 'tools[0].function.name': string too long. Expected a string with maximum length 128, but got a string with length 256 instead.
    512 chars            HTTP 400: Invalid 'tools[0].function.name': string too long. Expected a string with maximum length 128, but got a string with length 512 instead.
    dots                 HTTP 400: Invalid 'tools[0].function.name': string does not match pattern. Expected a string that matches the pattern '^[a-zA-Z0-9_-]+$'.
    spaces               HTTP 400: Invalid 'tools[0].function.name': string does not match pattern. Expected a string that matches the pattern '^[a-zA-Z0-9_-]+$'.
    slash                HTTP 400: Invalid 'tools[0].function.name': string does not match pattern. Expected a string that matches the pattern '^[a-zA-Z0-9_-]+$'.
    double underscore    accepted, model called 'mcp__notes__search'
```

三件事：

1. **`sanitize()` 是硬需求，不是洁癖。** 一个 server 名字里带个点，
   不是"这个工具的名字有点丑"，是**整个请求 400**——这一轮里所有工具，
   包括 `read_file` 和 `apply_patch`，一起消失。别人的 server 名字能
   炸掉你自己的工具。
2. **长度上限是 128，不是 64。** 我写常量的时候凭记忆写了 `MAX_TOOL_NAME = 64`。
   65 个字符测下来毫无问题。这条常量现在带着它的来历：

   ```python
   # Measured, not read: `probe_mcp.py names` sends real tool names to the real
   # API and reads the 400s.  ... The length limit is 128,
   # which is worth writing down because the number this constant was first given
   # was 64, from memory, and 65 characters went through without complaint.
   MAX_TOOL_NAME = 128
   ```
3. **`-` 是合法的。** 这一条后来还打了我一次脸，见 §14。

`sanitize()` 是有损的——`a.b` 和 `a b` 都变成 `a_b`。所以有可能两个不同的
原始名字撞成同一个模型名。这时候的选择是"截断/覆盖"还是"拒绝"？拒绝：
覆盖就是 F09-01 本人，只不过这次是我们自己当凶手。

```python
if name in self.registrations:
    self.retired[name] = (
        f"{tool.server}/{tool.name} was not registered: its name collides "
        f"with {self.registrations[name].tool.name} after sanitising"
    )
    continue
```

---

## §6 六十个工具要花多少钱

第 6 章有一个当时没人细想的发现：`estimate_messages()` 一开始把 tools 漏掉了，
理由是"schema 不是对话的一部分"。这句话是对的，也是完全没用的——它是每一个
请求的一部分，压缩历史不会让它变小。第 6 章的原话是「**Chapter 9 makes this
the dominant term.**」

现在到第 9 章了。`probe_mcp.py tokens`，不需要联网，用的就是第 6 章那把尺子：

```
=== F09-03: schema cost, measured with chapter 6's estimator ===
    conversation alone:                           32 tokens
    +   4 tool schemas:    545 tokens  (94.5% of the request, 136 per tool)
    +  12 tool schemas:   1635 tokens  (98.1% of the request, 136 per tool)
    +  30 tool schemas:   4092 tokens  (99.2% of the request, 136 per tool)
    +  60 tool schemas:   8187 tokens  (99.6% of the request, 136 per tool)
    + 120 tool schemas:  16387 tokens  (99.8% of the request, 137 per tool)
    deferred (1 search tool only):                68 tokens
    Every number above is paid on every turn, whether or not a tool is used.
```

一个工具 136 token。六十个工具 8187 token，占整个请求的 99.6%。

清单上 F09-03 写的是"光工具 schema 就占 3 万 token"。按这个 schema 形状，
3 万要 220 个工具才够。数字对不上不影响结论——**这笔钱每一轮都付，
用不用都付，而且第 6 章的压缩机制对它完全无能为力**：压缩删的是历史消息，
schema 不在历史里。一个 8k 窗口的模型，光工具列表就吃掉了它的全部。

清单给的解法是「两级加载：先名字+一句话，需要时再拉完整 schema」。
听起来是对的，量一下：

```
61 full schemas:              7488 tokens
61 name + one-line index:     1399 tokens   (19% of full)
61 names only:                 469 tokens
```

索引是全量的 19%。一句话描述占了索引的四分之三，但它是让"搜索"这件事
成为可能的那一部分，得留着。

到这里为止，一切都很顺。

---

## §7 那把它们藏起来会怎么样

这一节是本章最长的一节，因为我在这里错了三次。

清单上 F09-02 说：「60 个工具，模型选择准确率断崖下跌」，发现方式 🔵 长跑。
解法：工具分组 / 按需暴露。§6 刚刚证明按需暴露能省 81% 的 token，
两件事看起来是一件事：藏起来，又省钱又准。

### 7.1 第一轮：3/3，什么也没测出来

造一个 60 个工具的假目录，每个叫 `operation_NN`，描述是"对配置的后端执行
第 N 号操作"。再放一个针：`mcp__notes__search`，"按关键词搜索保存的笔记"。
任务：「Find the note about the meeting. Use a tool.」

```
    --- distractors: irrelevant ---
        1 tools offered:  correct 3/3
        9 tools offered:  correct 3/3
       31 tools offered:  correct 3/3
       61 tools offered:  correct 3/3
```

61 个工具，3/3 全对。

**这个测量什么也没测出来。** 六十个干扰项里没有一个跟笔记、搜索、会议、
文本有任何关系，所以模型的任务不是"在相关的工具里挑对的那个"，
是"挑出唯一一个相关的"。这跟第 4 章 F04-02 犯的错是同一个——
用一个 20 行的文件去测"模型会不会偷偷删代码"，测出来 3/3 没删，
然后发现 20 行的文件模型可以整个背下来。

### 7.2 第二轮：0/3，然后发现是我自己造的

第二轮把每个干扰项都做成一个**貌似能回答同一个问题**的工具：
`search_documents`、`find_meeting`、`query_notes_index`、`get_note`、
`list_notes`、`search_calendar`、`grep_workspace`、`recall_memory`……
十个，每个复制六份凑够六十。

```
    --- distractors: plausible near-misses ---
        1 tools offered:  correct 3/3
        9 tools offered:  correct 0/3   picked instead: mcp__bigcorp__query_notes_index_0, mcp__bigcorp__search_documents_0
       31 tools offered:  correct 0/3   picked instead: mcp__bigcorp__list_notes_0, mcp__bigcorp__query_notes_index_0
       61 tools offered:  correct 1/3   picked instead: mcp__bigcorp__query_notes_index_0
```

复现了！而且拐点在 9 个工具，不在 60 个。

再加一个对照臂：给针的描述补上第 3 章那种"这个工具不是用来干什么的"从句
（F03-04 / F03-05 的解法），别的都不动：

```
    --- 61 hard tools, needle carries a chapter-3 disambiguating clause ---
        9 tools offered:  correct 2/3   picked instead: mcp__bigcorp__search_documents_0
       61 tools offered:  correct 3/3
```

一句话把 61 个工具下的 1/18 拉回 3/3。看起来结论已经很漂亮了：
**F09-02 不是"工具太多"，是第 3 章的工具重叠（F03-05）在六十个工具的
规模上重演。**

然后我去看 `tool_search` 那一臂为什么是 0/3，打印了它到底搜到了什么：

```
QUERY: meeting
REVEALED: ['mcp__bigcorp__find_meeting_0', 'mcp__bigcorp__find_meeting_1',
           'mcp__bigcorp__find_meeting_2', 'mcp__bigcorp__find_meeting_3',
           'mcp__bigcorp__find_meeting_4']
```

五个候选，是同一个工具的五份复制品。

我的假目录把十个概念各复制了六份。搜索按分数排序取前五，同分的按字母序，
于是五个名额全被同一个概念占满。**第二轮的"复现"里，至少有一部分是我
自己的目录构造出来的。**

### 7.3 第三轮：六十个各不相同的工具

第三轮换成六个 server、每个十个工具、六十个全不重样——`docs`、`calendar`、
`memory`、`chat`、`code`、`wiki`，这是一台真的配了六个 MCP server 的机器
长的样子。

```
    --- distractors: irrelevant ---
        1 tools offered:  correct 3/3
        9 tools offered:  correct 3/3
       31 tools offered:  correct 3/3
       61 tools offered:  correct 3/3
    --- distractors: plausible near-misses ---
        1 tools offered:  correct 3/3
        9 tools offered:  correct 3/3
       31 tools offered:  correct 3/3
       61 tools offered:  correct 3/3
```

**全绿。F09-02 不复现。**

六十一个真实形状的工具、里面有 `search_documents` / `query_notes_index` /
`search_wiki` / `recall_memory` 这些明摆着抢生意的邻居，gpt-4o-mini
依然 3/3 找对。第二轮那个"拐点在 9 个"的漂亮结论，是六份复制品堆出来的。

一条故障，我用三种方法去撞它，前两种都给了我一个我想要的答案。
第 6 章统计过，那一章 25 条故障里有 5 条出在验证工具自己身上。
这一条是同一类，只是这次坏的不是脚本，是**样本设计**。

### 7.4 而藏起来的那一臂，一直是 0

三轮下来，全量展示始终 3/3。两级加载那一臂：

```
    --- the same 61 hard tools, deferred behind tool_search ---
      query='meeting'   revealed 5 -> (no tool call)
      query='meeting'   revealed 5 -> (no tool call)
      query='meeting'   revealed 5 -> (no tool call)
      searched 3/3, then correct 0/3
```

模型 3/3 都调了 `tool_search`。然后 0/3。

我先怀疑是我构造第二轮消息的方式不对——第一版把搜索结果直接塞进 user
消息里。改成真实的 assistant tool_call + tool result 消息对：还是 0/6。
再怀疑是探针给非目标工具返回的桩结果太像失败，改成中性的 `no results found.`：
还是 0/6。再让它像真正的 Agent 循环那样最多跑四轮：

```
    stopped on turn 3: 'It seems that there are no notes or records about any meeting found in'
    stopped on turn 2: 'I found the tools related to meetings, but I need to identify a specif'
    ran out of turns
    stopped on turn 3: "It seems I couldn't find any notes related to meetings at this time. I"
    ran out of turns
    stopped on turn 3: 'It seems that there are no records of a meeting with the keyword "meet'
    correct 0/6
```

**0/6。** 而且注意每一次它停下来的方式：不是选错了工具，是**放弃了**，
回过头来问用户能不能说得更具体一点。按第 0 章的终止规则（没有 tool_call
就是结束），这在真实运行里就是任务结束、什么也没做成。

机制看一眼就清楚了：

```
TURN2 content: 'I found several tools that can help with meetings, but I need a specific
identifier (like the title, attendee, or date) to find the appropriate meeting note.
Could you please provide more details to narrow down the search?'
```

模型的查询词是 `meeting`——从**任务**里挑出来的那个显眼的词。
针的描述是「Search saved notes by keyword and return the matching note」，
里面根本没有 `meeting` 这个词，所以无论 limit 设多大都搜不到它。

我按第 3 章的规矩试着修：把还没加载的工具的**名字和一句话描述放进
`tool_search` 的 description**（这才是清单原文说的"先名字+一句话"，
我第一版做成了"什么都不给，全靠搜"）；又给搜索结果加上一句
"还有 N 个工具没加载，如果上面这些都不对就换个词再搜一次，
不要将就一个只是大概对的工具，也不要去问用户那些还没加载的工具能查到的东西"。

索引确实进去了，4395 个字符，`mcp__notes__search: Search saved notes by
keyword and return the matching note` 就明晃晃地列在那儿。

模型还是搜 `meeting`。还是 0/6。

### 7.5 结论：两级加载是省钱手段，不是提准手段

三种探针设计，两轮各 3 次和 6 次采样，结论一致：

| 配置 | 正确率 |
|---|---|
| 61 个工具，无关干扰项，全量 schema | 3/3、3/3、3/3 |
| 61 个工具，近义干扰项，全量 schema | 3/3 |
| 61 个工具，藏在 `tool_search` 后面，单轮 | 0/3 |
| 61 个工具，藏在后面，允许循环四轮 | 0/6 |

所以代码里那个开关，从"工具数量"改成了"token 预算"：

```python
# When the rendered schemas cost more than this, they are replaced by an index
# and a `tool_search`.  A **token** budget rather than a tool count, and that
# is the whole finding of this chapter's largest measurement: with sixty-one
# realistic tools, showing every schema found the right one 3/3 while deferring
# them behind a search found it 0/6.  Deferring is not an accuracy improvement
# to reach for at some tool count; it is what you do when the schemas will not
# fit, and it costs something every time.
DEFAULT_SCHEMA_BUDGET = 4000
```

**能全量展示就全量展示，装不下了才退到索引。** 这跟第 6 章的压缩是同一种
东西：压缩不是优化，是不得已；每压一次都丢东西，只是丢的比爆窗口少。

还有一个次级决定：装不下的时候是**全藏**还是**藏一半**？

```python
# All or nothing, not a greedy fill.  A partial list is the worst of
# both: the model pays for schemas *and* has to know that what it can
# see is not everything, and which half it got is decided by
# alphabetical order, which means nothing to anybody.
```

半藏是两头不讨好：token 照付，模型还得知道"我看见的不是全部"，
而它看见的是哪一半由字母序决定，对谁都没有意义。

---

## §8 结果长什么样都可能

第 0 章定下的契约是：一个工具返回一个字符串。MCP 的返回不是字符串，
是一个 content block 数组，外加两个可选字段：

```json
{
  "content": [
    {"type": "text", "text": "note 'idea' rendered"},
    {"type": "image", "data": "iVBORw0KGgo...", "mimeType": "image/png"},
    {"type": "resource", "resource": {"uri": "notes://idea", "text": "..."}}
  ],
  "structuredContent": {"note": "idea", "bytes": 46},
  "isError": false
}
```

`normalise()` 把这一切压成一个字符串。几个不那么显然的决定：

**二进制描述，不嵌入。** 一张 base64 的 PNG 对一个不支持图片输入的模型
来说是纯噪音，而且它会**原样进历史**，往后每一轮都重发一遍。
所以变成 `[image omitted: image/png, 96 base64 characters]`。
（真正按模态估算 token 是第 6 章挂着的 F06-10，这里没解决，只是没让它更糟。）

**空结果必须说自己是空的。**

```python
text = "\n".join(part for part in parts if part) or "(the tool returned no content)"
```

一个空字符串在模型看来跟"这个工具成功了，但没什么好说的"完全一样，
它会当成一个答案继续往下走。

**`isError` 要说出来。**

```python
if result.get("isError"):
    text = f"The tool reported an error.\n{text}"
```

这里有两种不同的失败：传输失败（server 没了、超时）和工具失败
（`isError: true`，协议层面这次调用是成功的）。后者是第 0 章 F00-05
管的那一类——工具的失败是模型需要的信息，不是异常。但如果不加这一句，
`no such file: nope.toml` 和一句正常的返回在模型眼里没有区别。

**MCP 的结果也要截断。** 第 2 章截 shell 输出，第 6 章截历史条目，
而 MCP 的结果**两个都不经过**：

```python
# How much of a remote result the model is allowed to see.  Chapter 2 clips
# shell output and chapter 6 clips history items; an MCP result reaches the
# history through neither, which is F06-09's hole in a new shape.
MAX_RESULT_CHARS = 20_000
```

一个远端返回 400KB JSON 的工具，会用一次调用打爆整个窗口。
头尾各留一半，中间标注省略了多少——跟第 2 章的 `_clip()` 同一个做法，
理由也一样：只留尾巴会丢掉开头那句真正解释问题的话。

---

## §9 server 会死，而且不会告诉你为什么

### 9.1 起不来的

`connect()` 的第一版对启动失败是抛异常的。这在配了五个 server、
其中一个坏了的时候，意味着整个 Agent 拒绝启动——它比"用剩下四个跑"
差得多。

```python
except (McpError, TimeoutError, OSError) as exc:
    registry.failures[config.name] = str(exc)
    await client.close()
    continue
```

三种起不来的形态，都测了：

```
--- startup timeout ---
clients: [] failures: {'slow': 'slow did not answer initialize within 1s'}
--- server that is not there ---
failures: {'ghost': 'could not start ghost: [WinError 2] 系统找不到指定的文件。}
```

启动超时和调用超时是**两个预算**，因为它们是两种不同的故障：
前者拖的是整个 Agent 的启动，后者拖的是一轮。codex 的启动超时是 30 秒
（`rmcp_client.rs: DEFAULT_STARTUP_TIMEOUT`），这里抄的同一个数。

### 9.2 跑到一半死的，和一个 `DEVNULL` 藏起来的答案

给 server 加一个 `MCP_DIE_AFTER`，让它在第 N 次调用之后 `sys.exit(1)`。
第一次跑，`connect()` 两个 server，结果 `files` 那个直接就没了：

```
files search -> Error: mcp__files__search did not return  You sent: {"query": "hatchling"}
The server said: files closed the connection. Try a different approach.
```

"files closed the connection"。**这句话是真的，也是彻底没用的。**
为什么关的？我把 server 单独拿出来手动喂一遍才知道：

```
UnicodeDecodeError: 'gbk' codec can't decode byte 0x80 in position 1835: illegal multibyte sequence
```

`search` 工具去 `read_text()` 一个二进制文件，抛异常，整个 server 死了。
traceback 打在 stderr 上——而我在 `create_subprocess_exec` 里写的是
`stderr=asyncio.subprocess.DEVNULL`。

两处要改，各修各的：

**server 那边**：一次调用失败是一次调用失败，不是一个死掉的 server。
MCP 规范就是这么要求的——而在 SDK 上，这件事是**默认行为**：工具里
`raise` 出来的异常变成一个 `is_error` 的结果，连接毫发无损。这是依赖换来的
第二样东西：一整类"server 作者忘了 try/except"的故障消失了。

**client 那边**：别人的 server 不一定这么懂事，所以要能转述它的遗言。

默认情况下 `Client` 自己建传输，用的是 `errlog=sys.stderr`——遗言打到你的
终端上，而这个 client 手里只剩一句 `Connection closed`。真到用户那里，
终端上那段 traceback 谁也看不见。

但 `Client` 也接受**任何 `Transport`**，而 `stdio_client` 收一个 `errlog`：

```python
handle = tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", delete=False)
...
return stdio_client(params, errlog=handle)
```

**捕获是一个参数，不是一个 fork。** 这是判断一个依赖好不好用的实际标准：
它有没有把自己的默认值，做成一个你能换掉的参数。

一个细节是量出来的：必须是**真文件**，不能是 `StringIO`。`errlog` 最后会走到
`anyio.open_process(stderr=...)`，那里要一个真的文件描述符。

### 9.3 但"为什么死"只有一半拿得到

`stdio_client` 交出来的是流，**进程句柄留在里面**。所以退出码这个客户端
拿不到——能拿到的只有 server 自己写进 stderr 的东西。

于是分成两种：

| server 怎么死的 | 客户端能说什么 |
|---|---|
| 崩溃，traceback 落在 stderr | `MemoryError: index too large to load` |
| 干干净净 `exit(1)`，什么都不写 | 只有 `Connection closed` |

第二行是这一章的一个**已知缺口**（F09-15），没有绕过去，而是写成了断言——
测试的名字就叫 `a_silent_death_is_all_the_sdk_can_tell_us`，
同时钉住"还成立的"和"不成立的"：

```python
assert "Connection closed" in second
assert "exited with code" not in second
```

把缺口写成测试，比写进注释可靠：注释不会在有人以为自己修好了它的时候变红。

### 9.3 重连，以及一个故意不做的重试

F09-05 清单上的解法是「重连 + 工具列表刷新」。重连做在调用点上，
不做后台守护进程：一个 server 只有在有人要用它的时候才值得重启，
而且这样重试就落在模型看得见的轮次预算里。

```
call 0: 3164
call 1: Error: mcp__files__stat did not return ... The server said: files exited with code 1.
call 2: 3164
```

第 1 次调用**没有**被重试，这是故意的：

```python
# Not retried, on purpose.  The call above was in flight when
# the server stopped answering, so whether its side effect
# happened is unknown -- and "unknown" is not a state to
# resolve by doing it again.  The *next* call finds a dead
# client and restarts it, which is a retry of something that
# provably never started.
```

这是第 12 章 F12-04（重试不带幂等标识导致重复执行）的提前一次相遇。
一个 `remember` 调用发出去之后 server 死了，笔记到底写没写进去，
这边没有任何办法知道。**"不知道"不是一个靠再做一次来消除的状态。**

重连的时候还有一个容易写错的地方：重启后**重新读工具列表**，不是恢复
原来那份。

```python
# Re-read, not restore: a server that has been restarted may come back
# with a different set of tools, and pretending otherwise leaves
# registrations pointing at names the new process does not answer to.
```

而重新读的结果可能是工具变少了——这就直接接到下一条。

---

## §10 server 反过来问你，以及协议怎么回答这件事

MCP 里 server 可以向 client 要一个答复：调用执行到一半，需要一个确认、
一个缺失的字段、一次 OAuth 同意。剩下的问题只有一个：**谁来回答？**

> **先说协议现状，因为它决定了这一节能做到哪。** 老的做法是服务器直接发一个
> `elicitation/create` 请求给客户端。协议 `2026-07-28` **禁止服务器发起
> JSON-RPC 请求**——SDK 的分发器把这句话写在注释里
> （`mcp/server/runner.py:551-558`：*"the modern protocol forbids
> server-initiated JSON-RPC requests"*），一个调 `ctx.elicit` 的 server
> 拿到的是 `NoBackChannelError`，不是答案。
>
> 这不是 SDK 的取舍，是**协议改了**：一个双向的 JSON-RPC 通道，要求每条
> 传输都能反向送消息，而 HTTP 那条做不到。协议把自己收窄到了所有传输
> 都能满足的形状。
>
> 所以这一节的机制现在**测的是失败得干不干净**（F09-16）：调用**快速失败
> 并说明原因**，而不是挂在那里等一个永远不会来的答复。挂住才是真正糟糕的
> 结果，而 F09-04 那个调用超时就是为它准备的。
>
> 下面这套适配**留着**——第 21 章会加一条问题还能到达的传输，而且它跟
> 协议版本无关：它做的是"把一条消息和一个 schema 映射成'问人一个是非题'"。
> 现在断言的是那个还成立的性质：调用**快速失败并说明原因**，而不是挂住。

答案是第 5 章那个 `Approver`，一行新的审批逻辑都不加：

```python
def elicitation_handler(session: Session) -> RequestHandler:
    async def handle(params: dict[str, Any]) -> dict[str, Any]:
        ...
        reply = await session.approver.ask(
            ApprovalRequest(
                what=message,
                reason="an MCP server is asking before it acts",
                risk=Risk.UNKNOWN,
                suggested_rule=None,
            )
        )
```

理由写在 docstring 里：

```
There is no new approval machinery here on purpose: chapter 5 already
decided who gets asked and how, and a second prompt style for the same
question is how a user learns to answer without reading.
```

第 5 章 F05-06 花了很大力气对付"审批疲劳"——每条都问，用户一路 yes，
等于没有审批。如果这里另起一套问法，那套努力就白费了一半。

有一类请求是直接拒绝的：

```python
if len(properties) != 1 or not booleans:
    return {"action": "decline"}
```

server 可以要求一整个对象的字段，而这个 client 只会问一种问题：是或否。
`decline` 是 MCP 里合法的回答，意思是"用户不同意"。另一个选项是
编一个值把 schema 填满——**那是把一个凭空捏造的值写进别人的系统。**

跑一遍，两个方向都验：

```
approve: "saved 'x'"   file exists: True
deny   : 'The tool reported an error.\nnot saved: the user declined'   file exists: False
```

F09-06 清单上写的是 OAuth。完整的 OAuth 设备流加 token 存储是另一件工程，
没做；做了的是它底下那个机制——server 能问，client 能答，答案来自第 5 章。

---

## §11 第 8 章的 Footprint 遇上别人的承诺

第 8 章结尾留了一句话：

> 这条故障（F08-06）要活过来，需要一个"声明的 `Footprint` 可能是错的"的
> 工具——换句话说，需要一个不是从真实路径解析出来、而是猜出来的资源键。
> 第 9 章要接的 MCP 工具正好是这个形状。

到了。

MCP 的工具可以声明 `annotations.readOnlyHint`。codex 直接信它：

```rust
fn supports_parallel_tool_calls(&self) -> bool {
    // Correctly implemented MCP servers should tolerate parallel calls to
    // tools that advertise themselves as read-only.
    self.tool_info.supports_parallel_tool_calls
        || self.tool_info.tool.annotations.as_ref()
            .and_then(|annotations| annotations.read_only_hint)
            .unwrap_or(false)
}
```

注意那句注释的语气：「**Correctly implemented** MCP servers should...」。
这是一句关于别人的假设。

这里的做法是：信，但只信到一个结论为止。

```python
registration = self.registrations.get(call.name)
if registration is None or registration.tool.read_only is not True:
    return STATEFUL
return Footprint(reads=frozenset({f"mcp:{registration.tool.server}"}))
```

`readOnlyHint: true` 换来的是一个**按 server 分的读资源键**，
意思正好是"同一个 server 上的两个只读调用可以重叠"，
一个字都不多。特别是——它换不来一个空的 `Footprint()`：

```
It is a promise from a process this one cannot inspect, so it buys "two
read-only calls to the same server may overlap".  It does not buy an
empty `Footprint()`: a remote tool may still touch a file this turn's
`apply_patch` is writing, and nothing here can prove it does not.
```

一个远端的只读工具，"只读"是对它自己那边说的。它完全可能读的正是本地
仓库里这一轮 `apply_patch` 正在写的那个文件。第 8 章那句
「空的 `Footprint()` 是唯一一句在猜错的时候后果最严重的话」，
在这里第一次遇到一个真的有人想让你说它的场合。

还有一件必须做的事：`tools.footprint_of` 认得本地工具，
`registry.footprint_of` 认得远端工具，谁来分流？

```python
def route_footprint(registry: McpRegistry, local: FootprintFn) -> FootprintFn:
    def footprint(call: ToolCall) -> Footprint:
        return registry.footprint_of(call) if is_remote(call.name) else local(call)
    return footprint
```

一个函数，不是让两边互相认识：`tools.py` 不该知道 MCP 是什么，
registry 不该知道这个仓库的根目录在哪。这也是 `is_remote()` 存在的
全部理由——"这是不是远端工具"是一个字符串判断，任何地方都能问，
不需要 registry 在场。

---

## §12 历史里有个工具，现在没有了

F09-08：历史里引用了一个已经不存在的工具。

第 1 章的设计在这里帮了大忙：历史存的是**事实**，发送的时候才渲染
（F01-04）。所以一次旧的 call + result 配对，本身怎么渲染都没问题。
真正会出事的是模型**接着再调一次**那个名字——它在上下文里看得清清楚楚，
十轮之前刚用过。

这时候第 0 章那句 `Error: no tool named 'X'. Available tools: ...`
就变成了一句谎话。那句话是给**模型编出来的工具名**写的。

```python
def forget(self, server: str, reason: str | None = None) -> None:
    """Drop a server's tools, remembering that they were once there.

    Remembering matters more than dropping.  The model has the old names
    in its context and will use them; chapter 0's "no tool named X" was
    written for names a model *invented*, and telling it that about a tool
    it genuinely had ten seconds ago is a lie that makes it try harder.
    """
```

于是有一张 `retired` 表，每一条记着这个名字为什么没了：

```
Error: mcp__notes__search is no longer available  You sent: {"query": "idea"}
the MCP server 'notes' is no longer connected. Use one of the tools you were given instead.
```

三种进 `retired` 的方式，都在测试里：server 断开、server 重启后不再提供
这个工具、名字被 sanitize 撞掉。

---

## §13 没复现的那一条：code mode

F09-09：「逐个调工具太慢（20 次往返），应该批量」，解法是 code-mode——
让模型写脚本批量调用。

先量往返次数。十个目标提前就知道：

```
    --- the ten targets are known up front ---
      10 call(s) in the first response
      10 call(s) in the first response
      10 call(s) in the first response
```

一轮，十个调用。再量必须先发现目标的那种——"找出最大的那个文件"，
得先 list 再 stat：

```
    --- the targets have to be discovered first ---
      2 round trip(s), 11 call(s) total
      2 round trip(s), 11 call(s) total
      2 round trip(s), 11 call(s) total
```

两轮。**不是二十轮，是两轮。**

原因不神秘：现在的供应商支持一次响应里发多个 tool_call，而第 8 章刚刚
让这些调用真的并发跑起来了。清单写 F09-09 的时候设想的那个成本——
"二十次往返"——**在第 8 章就已经被付掉了**，只是当时没人从这个角度看它。

所以 code mode 没有做。做它需要一个能安全跑模型写的脚本的解释器沙箱，
那是第 5 章明确没做的那一半（F05-05 记着"真正的网络策略需要一个这一章
不打算建的 OS 沙箱"）。**为一条没复现的故障建一个沙箱，是这本书从第 3 章
起就一直在拒绝的那种工作。**

---

## §14 一次被测试打脸，和一次被变异测试打脸

### 14.1 `a-b` 不需要 sanitize

写完 `sanitize()`，我给它配了一条测试：`a.b` 和 `a-b` 都会变成 `a_b`，
所以第二个必须被拒绝而不是覆盖第一个。

```
AssertionError: assert ['mcp__s__a_b', 'mcp__s__a-b'] == ['mcp__s__a_b']
  Left contains one more item: 'mcp__s__a-b'
```

`-` 在 `^[a-zA-Z0-9_-]+$` 里是合法的，`sanitize()` 根本不碰它。
我写这条测试的时候，脑子里的规则是"奇怪的字符要替换掉"，
而实际的规则是"**供应商拒绝的字符要替换掉**"。这两条规则大部分时候
重合，`-` 就是它们分开的地方。

测试改成 `a.b` 和 `a b`，并且把这次纠正留在测试的 docstring 里：

```python
"""`a.b` and `a b` both become `a_b`; the second is refused, not blended.

The first version of this test used `a.b` and `a-b`, which do *not*
collide -- the measured pattern allows a hyphen, so `sanitize` leaves it
alone.  Worth keeping the correction visible: the set of characters that
have to be replaced is the one the provider rejects, not the one that
looks unusual.
"""
```

### 14.2 一个 42 个测试都没看见的洞

43 个测试全绿之后，按第 5 章和第 6 章的规矩跑变异测试：把代码改坏
十二处，看有几处能被测出来。

```
12 mutations, tests/test_faults_ch09.py

   19 test(s) fail  <-  no namespacing: every server's tool keeps its own name
  !! could not apply: isError is not mentioned to the model
  !! could not apply: an empty result renders as an empty string
    1 test(s) fail  <-  a read-only remote tool is assumed to touch nothing
    1 test(s) fail  <-  the schema budget ignores this project's own tools
    1 test(s) fail  <-  tool_search reveals into a copy instead of the live list
    1 test(s) fail  <-  a retired tool gets chapter 0's 'no tool named' treatment
  !! could not apply: the deferred index is left out of tool_search's description
    4 test(s) fail  <-  the client reads one line per request instead of demultiplexing
    0 test(s) fail  <-  stderr is thrown away, so a dead server cannot say why
    1 test(s) fail  <-  no startup timeout
    1 test(s) fail  <-  the host environment is handed to every server

4 mutation(s) nothing noticed
```

三个 `could not apply` 是我自己的脚本问题（模式串里的 `\n` 被 Python
当转义处理了，要写成 raw string）。第 6 章记过一模一样的一条：
**变异脚本报"全绿"可能意味着"没能看见失败"**，所以脚本必须报告
"这处变异根本没打上"，而不是把它算成通过。

剩下那一条是真的：**把 server 的 stderr 丢掉，43 个测试没有一个变红。**

去看当时那条测试：

```python
async def test_F09_05_a_dead_server_reports_why_instead_of_closed_the_connection() -> None:
    ...
    assert "exited with code 1" in second
```

`exited with code 1` 是从 `proc.returncode` 拼出来的，**跟 stderr 一点关系
都没有**。这条测试的名字里写着 "reports why"，读起来像是在测那件事，
实际上没有。这和第 6 章那条「测试把解析循环抄进了测试文件，
所以删掉被测代码里的守卫依然全绿」是同一个形状：
**一条测试的名字不是它测了什么的证据。**

补法是给 server 加一个"死的时候留下 stderr"的模式——干净退出什么都不写，
崩溃才写：

```python
crash_after = int(os.environ.get("MCP_CRASH_AFTER", "0"))
if crash_after and _calls > crash_after:
    # The difference between a client that can say why the server
    # is gone and one that can only say that it is.
    sys.stderr.write("MemoryError: index too large to load\n")
    sys.stderr.flush()
    os._exit(70)
```

> 这个区分不是可有可无的。§9.3 说过，退出码这个客户端**拿不到**——
> 于是 `MCP_CRASH_AFTER` 这个模式（死之前往 stderr 写点东西）是
> "server 能解释自己"仅剩的那条路。变异测试逼出来的这个 server 模式，
> 今天是 F09-05 唯一还站得住的半边。

```python
second = await handler({"name": "pyproject.toml"})
assert "MemoryError: index too large to load" in second
```

补完重跑：

```
   20 test(s) fail  <-  no namespacing: every server's tool keeps its own name
    2 test(s) fail  <-  isError is not mentioned to the model
    1 test(s) fail  <-  an empty result renders as an empty string
    1 test(s) fail  <-  a read-only remote tool is assumed to touch nothing
    1 test(s) fail  <-  the schema budget ignores this project's own tools
    1 test(s) fail  <-  tool_search reveals into a copy instead of the live list
    1 test(s) fail  <-  a retired tool gets chapter 0's 'no tool named' treatment
    1 test(s) fail  <-  the deferred index is left out of tool_search's description
    1 test(s) fail  <-  the server's stderr goes to the terminal, so a dead
                        server cannot say why
    1 test(s) fail  <-  structuredContent that only repeats the text is emitted anyway
    1 test(s) fail  <-  no startup timeout
    1 test(s) fail  <-  the host environment is handed to every server

every mutation was caught.
```

### 14.3 变异脚本自己咬了自己一口

这一章后来还撞到一条，值得写下来，因为它**看起来完全像是代码回归**。

变异脚本用 `atexit` 和一个 `SIGINT` handler 在退出时把源码改回去。
两者都只在**优雅退出**时跑——`SIGKILL` 不给你这个机会。于是一次被超时
掐掉的变异跑，会把一处变异**留在源码里**。

后果是：接下来的测试跑会把它当成代码的错。而这次更糟一点——那份被改坏的
`mcp.py` 被同步到了下游十二个 step，于是**十二个 step 报同一条测试失败**。
一整轮验证看起来像是回归，实际上是一个已经死掉的进程留下的残渣。

（同一件事的另一半：**变异脚本和测试跑不能同时对着同一棵工作树跑。**
一个在改文件，一个在读文件。）

修法是在写第一个字节之前先看一眼：

```python
dirty = [
    f"{name}: looks like {label!r} is still applied"
    for name, label, before, after in MUTATIONS
    if before not in ORIGINALS[name] and after in ORIGINALS[name]
]
```

**两个条件都要**——原文不在、且变异后的文本在。只判断后者会误报：
有几处变异是"把一个表达式换成一个更简单的"，而那个更简单的形式在同一个
文件里本来就合法地出现过。第一版只判断了后者，立刻在 `registry.py` 上
报了两条假警报。

**一个会乱叫的守卫，是一个会被人删掉的守卫。**

（这条守卫写完的当天就抓到了第三处残留——一个更早的被杀进程留下的
`return Footprint()`。它不是理论上的风险。）

---

## §15 接进 CLI，以及一个共享的列表

`__main__.py` 里，registry 拿走了**整张工具表**——本地的四个工具也放进去：

```python
registry = McpRegistry(local=list(TOOL_SCHEMAS))
```

```python
# This project's own tools, always shown and never deferred.  They are
# in here rather than concatenated by the caller so that `visible` is
# the *whole* tool list -- one object, handed to the model client once,
# correct on every turn.  Two lists that have to be concatenated at
# each call site is how one of them gets forgotten.
```

然后交给模型客户端的是**这个列表对象本身**，不是它的副本：

```python
llm = ChatCompletionsModel(..., tools=registry.visible)
```

因为 `tool_search` 加载一个工具的方式，就是往这个列表里 append 一个
schema。`ChatCompletionsModel.request_body()` 每次构造请求都会重新读
`self.tools`，所以第 3 轮加载的工具第 4 轮就能调。

**写成 `tools=list(registry.visible)` 会怎样？** 类型检查通过，
运行不报错，`tool_search` 返回"已加载 5 个工具，现在可以调用了"——
而模型永远也看不到它们。这是一条完美的 🟡：整个机制静默失效，
每一层的日志都说一切正常。所以有一条以这个错误命名的测试：

```python
async def test_F09_03_tool_search_reveals_into_the_same_list_the_model_client_holds() -> None:
    """The mistake this is named after: handing over a copy.

    `llm.tools = list(registry.visible)` type-checks, runs, and quietly makes
    `tool_search` do nothing at all -- the tool is revealed into a list the
    request builder is not reading.
    """
    ...
    what_the_client_holds = registry.visible  # by reference, exactly as __main__ does
```

配置是一个 JSON 文件，形状照抄 codex 的 `mcp_servers`：

```json
{
  "servers": {
    "files": {"command": ["python", "mcp_servers/files_server.py"]},
    "notes": {"command": ["python", "mcp_servers/notes_server.py"], "env": {"TOKEN": "..."}}
  }
}
```

`env` 是加法，不是全量：

```python
ENV_ALLOWLIST = ("PATH", "HOME", "LANG", "LC_ALL", "TERM", "TZ", "SYSTEMROOT", "PATHEXT")

def _subprocess_env(config: ServerConfig) -> dict[str, str]:
    env = {key: os.environ[key] for key in ENV_ALLOWLIST if key in os.environ}
    env.update(config.env)
    return env
```

第 2 章的 F02-09（宿主的 API key 被传进子进程）在这里往外挪了一个进程。
区别是 MCP server 通常**真的需要**一个凭据，所以配置里能一条一条加，
而不是把整个 `os.environ` 递过去。

### 9.4.1 但这个白名单不是白名单

把上面那个 dict 交给 SDK，然后问 server 它到底看见了什么。
交出去五个变量，**它收到十四个**：

```
we passed         : ['HOME', 'PATH', 'PATHEXT', 'SYSTEMROOT', 'TERM']
child actually saw: ['APPDATA', 'HOME', 'HOMEDRIVE', 'HOMEPATH', 'LOCALAPPDATA',
                     'PATH', 'PATHEXT', 'PROCESSOR_ARCHITECTURE', 'SYSTEMDRIVE',
                     'SYSTEMROOT', 'TEMP', 'TERM', 'USERNAME', 'USERPROFILE']
```

原因在 `mcp/client/stdio.py` 一行里：

```python
env=get_default_environment() | (server.env or {})
```

**是并集，不是替换。** 你给的是下界，不是上界。多出来的九个里有
`USERNAME`、`USERPROFILE`、`APPDATA`——"谁在跑这个进程、他的文件在哪"。

好消息是它有边界：我在环境里塞了一个 `A_SECRET_OF_MINE`，**没有**漏过去，
因为并的是一张固定的表，不是整个环境。但"有边界"和"你选的"是两回事，
而 F02-09 讲的正是**选**。

能做的只有一件事——并集允许的唯一覆盖方式是把不想要的键**显式置空**：

```python
for key in DEFAULT_INHERITED_ENV_VARS:
    if key not in env:
        env[key] = ""
```

server 还是会看见 `USERNAME` 这个名字，但看不到是谁（F09-10）。

这一条值得单独记住，因为它跟 MCP 无关：**你没选的默认值，
也是你发布的决定。** 依赖不是托管。

真跑一次，真模型，真 server：

```
$ uv run minicodex ask "Use the notes tools: search my notes for 'meeting' and quote the note back to me." \
    --provider openai --mcp mcp.example.json --yes

[mcp: files connected, 2 tool(s)]
[mcp: notes connected, 3 tool(s)]
The note I found states: "Ship chapter 9 before the scheduler work goes stale."

[gpt-4o-mini | completed after 2 turn(s)]
```

---

## §16 文件清点

| 文件 | 行数 | 新增/修改 | 完整代码在 |
|---|---|---|---|
| `src/minicodex/mcp.py` | 420 | 新增 | §2、§3、§9 |
| `src/minicodex/registry.py` | 673 | 新增 | §5、§7、§8、§10、§11、§12 |
| `src/minicodex/__main__.py` | +58 | `--mcp`、启动/合并/关闭 | §15 |
| `mcp_servers/files_server.py` | 198 | 新增（真的 MCP server） | §2、§3、§9 |
| `mcp_servers/notes_server.py` | 198 | 新增 | §4、§8、§10 |
| `tests/test_faults_ch09.py` | 735 | 43 个测试 | 全章分散 |
| `probe_mcp.py` | 514 | 新增 | §4、§5、§6、§7、§13 |
| `probe_mutations_ch09.py` | 147 | 新增（12 处变异） | §14.2 |
| `.github/workflows/postmerge.yml` | 41 | 新增（第二层 CI） | §17 CI |
| `tests/test_packaging.py` | +19 | 一条断言：第二层挡不住 merge | §17 CI |

```
$ uv run pytest
1357 passed, 9 skipped

$ uv run pytest tests/test_faults_ch09.py -q
43 passed

$ uv run python probe_mutations_ch09.py
every mutation was caught.

$ uv run ruff check .
All checks passed!
```

跳过的 9 个仍然是 `killpg` 的 POSIX 分支（F02-10）。

---

## §17 收工：commit 与 review

### commit 序列

```
feat(mcp): speak JSON-RPC 2.0 to one MCP server over its stdio

McpClient: subprocess, handshake, tools/list, tools/call. The reader is a
background task with a table of pending futures rather than a readline()
after each write, because three kinds of message share that pipe: responses
carry an id we issued, requests carry an id the server issued, notifications
carry none. A client that assumes the next line is its answer reads a log
notification as the result of initialize and every answer after it is off
by one, silently -- reproduced with MCP_LOG_NOISE.

_request() is deliberately not `async def`: the future has to be in _pending
before the caller can be cancelled, or a request goes out with nobody waiting
for it.
```

```
feat(mcp): read the server's stderr, and close what we opened

stderr was DEVNULL. A server that died left the client holding "closed the
connection" and nothing else, with the traceback that explained it in a pipe
nobody read -- F02-12 one process further away. The drain task also stops a
chatty server blocking on a full pipe.

close() shuts down in the order that leaves nothing running: cancel the
readers, close stdin (a well-behaved server exits on EOF), wait, kill, then
close every transport explicitly. Letting the GC do the last step raises
ValueError: I/O operation on closed pipe from a __del__, after the program's
otherwise correct output.
```

```
feat(registry): namespace remote tools; the wire name is not the model name

Two servers both call their tool `search`. Merging into one dict is not a
crash -- measured: five declared tools become four, and asking for a file
returns "no notes match" from the notes server, correctly formatted and
wrong. The model sees mcp__files__search; the server still sees search.

sanitize() is measured rather than tidy (probe_mcp.py names): the provider
pattern is ^[a-zA-Z0-9_-]+$ and a dot in a *server* name is an HTTP 400 for
the whole request, taking read_file and apply_patch down with it. The length
limit is 128; this constant said 64 from memory until it was measured.
```

```
feat(registry): normalise every shape an MCP result can take

Text, image, audio, embedded resource, resource link, structuredContent,
isError -- into the one string chapter 0 promised. Binary is described, not
included: a base64 PNG is unreadable tokens that would sit in the history at
full length forever. An empty result says it is empty, because "" reads to a
model as "succeeded, nothing to say". isError is stated, because the
transport succeeded and only the tool failed -- two different failures.

Clipped at 20k characters: an MCP result reaches the history through neither
chapter 2's shell clip nor chapter 6's per-item clip.
```

```
feat(registry): stage schemas on a token budget, not on a tool count

Measured (probe_mcp.py tokens): 60 tool schemas are 8187 tokens, 99.6% of a
short request, re-sent every turn, and compaction cannot touch them.

But measured also (probe_mcp.py selection, three catalogue designs): hiding
them behind tool_search costs accuracy. 61 realistic tools with all schemas
shown, correct 3/3; the same 61 deferred, 0/3 single-turn and 0/6 allowed to
loop. The model writes its query from the task, not from the index, and then
gives up and asks the user. Two earlier "reproductions" of F09-02 were
artefacts of the catalogue and are documented as such.

So: show every schema until they do not fit, then show an index. All or
nothing, never a greedy half -- a partial list costs tokens *and* requires
the model to know that what it sees is not everything.
```

```
feat(registry): survive servers that will not start, and ones that die

A server that fails to start is recorded and skipped, not raised: an agent
that refuses to run because one of five optional servers is broken is worse
at its job than one that runs with four. Startup and per-call timeouts are
separate budgets because they are separate failures.

A dead server is restarted on the next call to one of its tools, and its
tool list is re-read rather than restored -- a restarted server may come
back with different tools. The call that was in flight when it died is NOT
retried: whether its side effect happened is unknown, and "unknown" is not
a state to resolve by doing it again (F12-04, chosen deliberately).

Tools that go away are retired with a reason. Chapter 0's "no tool named X"
was written for names a model invented; saying it about a tool the model
genuinely used ten turns ago is a lie that makes it try harder.
```

```
feat(registry): answer elicitation with chapter 5's approver

An MCP server can ask the client a question mid-call. No new approval
machinery: chapter 5 already decided who is asked and how, and a second
prompt style for the same question is how a user learns to answer without
reading. A request for a shape this client cannot ask a human about is
declined rather than filled in -- inventing a value puts it into somebody
else's system.
```

```
feat(registry): a remote footprint is a promise, not a resolved path

Chapter 8 refused to let a tool guess what it touches; annotations.readOnlyHint
is exactly such a guess, made by somebody else's code. It is trusted for one
conclusion -- two read-only calls to the same server may overlap -- and never
for an empty Footprint(): a read-only remote tool may still read a file this
turn's apply_patch is writing.

This is F08-06 becoming reachable, exactly as chapter 8 predicted. codex
trusts the same hint (supports_parallel_tool_calls) with a comment that says
"correctly implemented MCP servers should" -- which is an assumption about
somebody else.
```

```
test(mcp): 12 mutations, one of which nothing noticed

stderr=PIPE -> DEVNULL left all 43 tests green. The test named
"reports why instead of closed the connection" asserted "exited with code 1",
which is built from returncode and has nothing to do with stderr. Added
MCP_CRASH_AFTER, which raises rather than exiting, so the explanation exists
only on the pipe. A test's name is not evidence of what it tests.
```

```
ci: put the mutation check in a second, non-blocking workflow

Adding it as a seventh step to ci.yml turned
test_F_1_05_ci_is_valid_yaml_and_stays_small red -- chapter -1's guard,
"the blocking suite is meant to stay fast", doing exactly what it was
written for. Raising the cap to seven skips the question it exists to ask,
and the answer here is that this check does not get to block a merge: it
measures the quality of the tests, not the correctness of a change.

postmerge.yml, push-to-main only, no pull_request trigger -- asserted in
test_packaging.py rather than stated in a comment, because a comment does
not survive somebody adding `pull_request: {}` to make it run sooner.
```

### PR 描述

```markdown
## What
The agent can use tools it did not write. MCP servers are started from a
config file, their tools are namespaced, their results normalised into one
string, their schemas staged against a token budget, their failures survived,
and their questions answered by chapter 5's approver.

## Why
Six MCP servers is sixty tools whose descriptions, parameter names, failure
modes and uptime all belong to somebody else. Every assumption chapters 0-8
were allowed to make about a tool stops holding at once.

## How
- `mcp.py`: one subprocess, JSON-RPC 2.0 over stdio, demultiplexing reader,
  deadlines on every await, stderr kept so a dead server can say why.
- `registry.py`: namespacing, staged exposure + tool_search, normalise(),
  remote Footprints, reconnect + tool-list refresh, elicitation.
- `__main__.py`: `--mcp CONFIG`; the registry owns the whole tool list and
  the model client holds that same object by reference.
- `mcp_servers/`: two real MCP servers, ours, each able to fail on request.

## Testing
43 new tests, all against real subprocesses, none networked. 12 mutations,
all caught (one of them only after a test was found not to test what its
name said).

    1356 passed, 9 skipped in 55.74s

## Notes for the reviewer
- **F09-02 does not reproduce** and two earlier rounds that appeared to were
  artefacts of my own catalogue. Details in FAULTS.md and §7.
- **Two-stage loading makes selection worse**, measured 3/3 -> 0/6. It ships
  as a *token* mechanism with a token trigger, and the chapter says so.
- **F09-09 does not reproduce**: ten independent calls arrive in one response
  and chapter 8 already runs them concurrently. No code mode.
- The mutation check is in a **new, non-blocking** workflow, not in ci.yml --
  chapter -1's six-step guard is what forced that decision, and it was right.
```

### Code review

我扮演 reviewer，五条真实意见。

**#1（设计）`llm.tools` 和 `registry.visible` 是同一个 list 对象。
这是一种远距离作用（action at a distance），一般是要避免的。为什么不让
`Model.stream()` 每轮接受一个 tools 参数？**

> 那是更正确的设计，也是 codex 的做法——它每一轮从 registry 重新构造
> 工具列表。这里没有那么做的原因是影响面：`Model` 是一个 Protocol，
> 第 0 章到第 8 章的测试里有几十个手写的假 model，全都定义成
> `stream(self, messages)`。为了本章一个功能去改协议签名，等于让前面
> 八章的测试全部重写一遍，而它们描述的行为一个字都没变——这正是
> "纯重构不夹带功能改动"那条纪律的镜像。
>
> 折中的办法是把这件事**说出来并钉住**：`McpRegistry` 的 docstring 明写
> "这个列表是按引用交出去的"，`__main__` 那一行有注释，
> 并且有一条以"传副本"这个错误命名的测试。什么时候该改成参数传递？
> 第二个需要动态工具列表的功能出现的时候——那就是三次法则的第二次，
> 还不到抽象的门槛，但该留 TODO 了。

**#2（正确性）`reconnect()` 在调用路径上同步做，一个启动慢的 server
会把这一轮卡住 30 秒。为什么不放后台？**

> 因为后台重连要回答一个更难的问题：重连成功之后，工具列表变了，
> 谁去通知正在跑的那一轮？现在这个写法里，重连和"发现工具变了"
> 是同一个函数里的两行，不存在中间状态。
>
> 30 秒的担心是真的，但它已经被两层预算挡住了：`startup_timeout`
> 是 per-server 可配的，`tool_timeout` 管住调用本身。真正该改的信号是
> "有人配了一个启动要 30 秒的 server 并且抱怨每次重连都卡"——
> 那时候要做的是异步预热（codex 有 `mcp_prewarm.rs`），
> 不是把重连挪到后台。

**#3（安全）MCP server 是子进程，但它完全绕过了第 5 章的审批门。
一个恶意的配置文件就是任意代码执行。**

> 是的，而且这一点在 README 的 "deliberately not done" 里写明了。
> 分界线是这样的：**server 是用户在配置文件里写的，不是模型说的**——
> 跟用户在 shell 里敲一条命令是同一个信任级别，第 5 章的门是拦
> "模型想跑什么"的，不是拦"用户配了什么"。
>
> 真正在门里的是**调用**：模型调哪个远端工具、传什么参数，
> 都走 `Agent._run_tool`。但这里有一个诚实的缺口要说清楚：
> 第 5 章的 sandbox mode（read-only / workspace-write）**管不到另一个
> 进程**。一个 `read-only` 的会话，配上一个能写文件的 MCP server，
> 模型就能写文件。要真正堵上，需要的是 OS 级别的沙箱把 server 也框进去，
> 那正是 F05-05 记着没做的那件事。这条应该进 FAULTS.md，我加了。

**#4（可测试性）`normalise()` 有九个分支，测试里一个分支一条。
这些断言看起来很像在重复实现被测代码。**

> 第 6 章有一条故障正是这个形状（测试把解析循环抄进了测试文件，
> 删掉被测代码里的守卫依然全绿），所以这条意见问得对。
> 这里的区别在于断言的是**输出的语义**而不是**过程**：
> `assert "AAA" not in out` 说的是"base64 不许进历史"，
> 不管 `normalise` 内部怎么实现；`assert normalise({}) ==
> "(the tool returned no content)"` 说的是"空不能渲染成空串"。
>
> 而且这一组有真实的 server 端到端验证兜底：
> `test_F09_07_a_real_server_returning_three_block_types_survives`
> 走的是真的子进程、真的协议、真的 base64 PNG。
> 变异测试里 `isError` 和空结果那两处各被 2 个和 1 个测试抓住。

**#5（措辞）`tool_search` 的结果里那一大段"如果都不对就再搜一次，
不要将就，不要去问用户"——这是在 prompt 里写"请不要……"，
§4.4 那条总原则说这种东西应该用代码强制。**

> 说得对，而且我确实先试了代码那一侧：索引进 description（让模型
> 有东西可搜）、调大 limit（让候选更多）。两个都试过，都不管用——
> 因为查询词是模型从任务里挑的，跟目录里有什么无关，limit 再大也
> 捞不出一个不含那个词的工具。
>
> 所以这段话留下了，但它的定位不是"修好了"，是"在一个已经量出来
> 修不好的机制上，尽量减少损失"。真正的结论写在常量的注释里：
> **这套机制本身就是有代价的，能不用就别用**——所以触发条件是
> token 预算，不是工具数量。一段没能把 0/6 拉回来的 prompt，
> 它的诚实位置是"这条路走不通"的证据的一部分，不是解法。

### CI：一个没能加进 blocking 的步骤

这一章要把变异测试加进 CI。理由和第 6 章加属性测试那一步一样——
**是真实测出来的需要，不是仪式**：43 个测试全绿的状态下，有一处真实的
缺口（stderr）没有任何测试覆盖，是变异测试把它找出来的。而这个缺口的
形状（一条测试的名字声称它测了某件事，实际断言的是另一件）不是一次性的：
第 5 章一次，第 6 章一次，这是第三次。

于是往 `ci.yml` 里加了第七步。跑测试：

```
FAILED tests/test_packaging.py::test_F_1_05_ci_is_valid_yaml_and_stays_small
1 failed, 1355 passed, 9 skipped in 60.12s
```

```python
    assert len(steps) <= 6, "the blocking suite is meant to stay fast"
```

**第 -1 章的护栏拦住了第 9 章。**

这条断言是 F-1-05 的产物——「一开始配全套 CI，被卡到最后关掉 CI」。
它防的正是现在这个动作：每一步单独看都有充分理由，加着加着就没人愿意等了。

最省事的做法是把 6 改成 7。没改，因为改之前得先回答一个问题：
**这一步凭什么挡住 merge？**

答不上来。变异测试量的是**测试本身的质量**，不是这次改动的正确性。
一个 PR 把代码改对了、但留下一处变异没被抓住，这件事值得被看见，
不值得把它拦在门外。

所以它去了一个新文件，`postmerge.yml`——推到 main 之后跑，没有
`pull_request` 触发器。这是 codex 那套分层的第一次出现（`blocking-ci.yml`
挡 merge、7 项 vs `postmerge-ci.yml` 不挡 merge、跑重的），
只不过这里不是照着抄的，是被一条三章之前写下的断言逼出来的。

「它挡不住 merge」这句话本身也写成了断言，不是注释：

```python
    triggers = wf.get("on", wf.get(True))
    assert "pull_request" not in triggers
    assert "push" in triggers
```

注释拦不住后来某个人为了"让它早点跑"加一行 `pull_request: {}`。

十二处变异跑完约 40 秒——比第 6 章那步 2000 个 case 的属性测试还便宜。
**它没进 blocking CI 不是因为它慢，是因为它回答的不是"这次改动对不对"。**

---

## §18 codex 是怎么做的

对照真实的 codex 源码（`codex-rs/`）：

- **协议也是用官方 SDK**：`rmcp = { version = "=3.0.0" }`
  （`Cargo.toml:393`）。两边同一个判断——协议不是自己该写的东西。

- **命名空间的形状一模一样**：`mcp__{server}__{tool}`，而且**原始名字
  被完整保留**。`codex-mcp/src/connection_manager_tests.rs` 里有一条
  测试把这件事钉得很死：

  ```rust
  assert_eq!(tool.server_name, "server.one");
  assert_eq!(tool.callable_namespace, "mcp__server_one");
  assert_eq!(tool.callable_name, "tool_two_three");
  assert_eq!(tool.tool.name, "tool.two-three");   // 发给 server 的还是这个
  ```

  旁边还有一条断言 `is_code_mode_compatible_tool_name`——要求模型可见的
  名字里只有 ASCII 字母数字和下划线。理由和 §5 量出来的一样。

- **两级加载在 codex 里叫 `ToolExposure`**，而且不是两级，是**六档**
  （`tools/src/tool_executor.rs`）：`Direct`、`Deferred`、
  `DeferredModelOnly`、`DirectModelOnly`、`CodeModeOnly`、`Hidden`。
  多出来的四档全是为了 code mode——一个工具可以"模型能直接调、但 code mode
  里看不见"，也可以反过来。跟本章相关的是三档：

  ```rust
  /// Include this tool in the initial model-visible tool list.
  Direct,
  /// Register this tool for later discovery, but omit it from the initial
  /// model-visible tool list. Deferred tools must provide search metadata via
  /// [`ToolExecutor::search_info`].
  Deferred,
  /// Keep this tool registered for dispatch without exposing it to the model.
  Hidden,
  ```

  注意 `Hidden` 的注释：**注册了、能被调用、但模型看不到**。
  本章 `handlers()` 把 deferred 的工具也返回出去，理由完全一样——
  藏起来的是 schema，不是可调用性。

  搜索工具就叫 `tool_search`（`tools/src/tool_discovery.rs:
  TOOL_SEARCH_TOOL_NAME`），默认返回 8 个（`TOOL_SEARCH_DEFAULT_LIMIT`，
  本章是 5）。触发条件是分开的两件事：`core/src/mcp_tool_exposure.rs` 里
  `Deferred` 与否取决于 `search_tool_enabled` 这个开关，
  而 `Hidden` 取决于**字节预算**：

  ```rust
  const MAX_AGENT_PLUGIN_MCP_SPEC_BYTES: usize = 8_000;
  const MAX_AGENT_PLUGIN_MCP_TOTAL_BYTES: usize = 64_000;
  ```

  后一半跟本章 `DEFAULT_SCHEMA_BUDGET` 是同一个思路——**按体积管，
  不按数量管**。前一半（一个开关决定要不要 defer）本章没有对应物，
  §7 那次测量的结论是这件事不该有开关，该有的是预算。

- **codex 也把索引放在 description 里**，而且放的是"源"而不是每个工具：
  `create_tool_search_tool()` 会把每个 server 的名字和描述渲染进
  `tool_search` 的 description，预算是 512KB。本章的索引粒度更细
  （每个工具一行），代价是 §7 量出来的 1399 token。

- **启动超时 30 秒**：`codex-mcp/src/rmcp_client.rs:91`，
  `DEFAULT_STARTUP_TIMEOUT: Duration = Duration::from_secs(30)`。
  本章抄的这个数。

- **结果归一化的一半是相同的，另一半 codex 做得更细**：
  `sanitize_mcp_tool_result_for_model()` 按模型支持的模态决定
  image / audio block 是保留还是换成一句占位文字：

  ```rust
  return serde_json::json!({
      "type": "text",
      "text": "<image content omitted because you do not support image input>",
  });
  ```

  本章无条件把图片换成描述（因为 F06-10 还没做，估不准多模态的 token）。
  截断那一半是一样的：`truncate_mcp_tool_result_for_event` 把超大结果
  压成一段文本预览。

- **`readOnlyHint` 被信任，理由写在注释里**（见 §11）。这是本章跟 codex
  处理方式最接近、但**结论更保守**的一处：codex 信它就直接允许并发，
  本章信它只换来"同一个 server 上的只读调用之间"可以并发。

- **`core/tests/suite/` 里 MCP 相关的测试文件名，本身就是一份故障清单**：
  `mcp_refresh_cleanup.rs`、`mcp_tool_cache.rs`、`mcp_tool_exposure.rs`、
  `mcp_auth_elicitation.rs`、`mcp_auth_refresh.rs`、
  `mcp_startup_refresh_http_proxy.rs`、`rmcp_client.rs`。
  第 7 章说过测试文件名是最诚实的故障档案——`mcp_refresh_cleanup`
  这个名字，读起来就是本章 §9.3 那条"重连之后旧的注册怎么办"。

---

## 如果你只记住三件事

1. **接入外部工具的第一个动作是重命名，第二个动作是不重命名。**
   两个 server 都叫 `search` 不是小概率事件，是必然事件；而合并成一个
   dict 不会崩，会给你一个格式完整的错答案。模型看到的名字必须带
   namespace，发给 server 的名字必须一个字不改——这两件事同时成立，
   靠的是把它们**分成两个字段存**，而不是靠某处小心翼翼地转换。
   顺带：那个 namespace 里能放什么字符，是**供应商用 400 定的**，
   不是你觉得好看就行——一个点号能让整轮请求里所有工具一起消失。

2. **"工具太多所以藏起来"是一个听起来正确、量出来是负收益的动作。**
   六十一个真实工具全量展示，选对 3/3；藏在 `tool_search` 后面，
   0/6——而且失败方式不是选错，是模型放弃并回头问用户。
   索引放进 description 试过，加"再搜一次"的指令试过，让它循环四轮试过，
   都是 0。所以两级加载在这份代码里的定位是**省 token 的手段，
   触发条件是 token 预算**：装得下就全展示，装不下才退。
   顺带记住我在这条上错了三次——前两次"复现成功"都是我自己的样本
   设计造出来的，第三次换成六十个各不相同的工具，故障消失了。
   **一条故障"复现"了的时候，先问自己复现的是它还是你的探针。**

3. **别人的进程说的话，是承诺，不是事实。** `readOnlyHint: true` 换来
   "同一个 server 上两个只读调用可以重叠"，仅此而已——它换不来一个空的
   `Footprint()`，因为一个远端的"只读"工具完全可能读的是本地这一轮正在
   被 `apply_patch` 写的文件。同样的道理：server 会崩、会起不来、
   会重启之后少几个工具、会在 stderr 上留一句只有你去读才看得到的遗言。
   第 8 章说"猜不出来的时候往安全的方向猜"，第 9 章加一句：
   **别人告诉你的，和你自己算出来的，不是同一种知识。**

> 还有半条，关于依赖：**教什么就手写什么，不教的就依赖它**——协议归 SDK，
> 这一章归你。但依赖不是托管：`env=` 是并集不是替换（F09-10）、
> 带类型的返回值会被镜像成两份（F09-12）、关闭预算得照着人家的常量定
> （F09-13）。**你没选的默认值，也是你发布的决定。**

---

## 动手练习

1. 把 `mcp.py` 的 `_read_loop` 换回三十行版本的 `readline()`——
   写一行、读一行、返回。跑 `uv run pytest tests/test_faults_ch09.py`，
   看哪四条红了。然后把 `files_server.py` 的 `MCP_LOG_NOISE` 关掉再跑一遍：
   **四条全绿。** 这说明了什么关于"我的测试覆盖了这个 bug"和
   "我的测试环境恰好不触发这个 bug"之间的区别？

2. 把 `__main__.py` 里的 `tools=registry.visible` 改成
   `tools=list(registry.visible)`，跑全套测试。哪一条红了？
   然后真的跑一次带 `--mcp` 的会话，配一个工具多到会被 defer 的
   server（`MCP_EXTRA_TOOLS=60`），观察模型调完 `tool_search` 之后
   下一轮发生了什么。这个故障在日志里长什么样？

3. `probe_mcp.py selection` 现在测的是 `gpt-4o-mini`。改成你能访问的
   另一个模型（或者本地 ollama 的一个），把三个臂都跑一遍。
   两级加载在你的模型上是不是也是负收益？如果不是——那说明
   `DEFAULT_SCHEMA_BUDGET` 这个数字应该是**按模型配的**，
   而不是一个全局常量。把它改成按模型配，并说清楚你依据的是哪次测量。

4. 给 `notes_server.py` 加一个工具，声明 `readOnlyHint: true`，
   但它实际上会往磁盘写一个文件。然后在一轮里同时发起这个工具和一个
   `apply_patch`，看调度器怎么排（提示：本章的 `footprint_of` 会让它们
   落进**同一批**吗？为什么？）。这就是 F08-06 真正活过来的样子。
   想清楚之后回答：有没有任何一种办法，能让 client 端验证这个 hint？

5. 读一遍 `probe_mutations_ch09.py` 的十二处变异，然后**自己再加三处**——
   挑你觉得"这段代码删了肯定有测试会红"的地方。跑一遍。
   有几处是你猜对的？第 5、6、9 三章各有一条故障是变异测试找出来的，
   形状都是"一条测试的名字声称它测了某件事，断言的却是另一件"。
   你新加的三处里，有没有一处也撞上了这个？

---

下一章：Ch10 · 子 Agent——这一章的工具来自另一个**进程**，
下一章的工具来自另一个**Agent**。`readOnlyHint` 至少还是一个静态的声明；
一个子 Agent 会碰什么，在它跑完之前谁也不知道，包括它自己。

---

# 附录 · 代码到底怎么敲出来的（新手向，逐段精讲）

正文讲的是"为什么这样做"和"量出来是什么结果"，默认你已经能把一段 Python
翻译成脑子里的执行过程。这份附录反过来：假设你是第一次接触 MCP、第一次
认真写 `asyncio` 子进程通信，把正文里跳过的"具体怎么敲"一步步补上。

不重复正文已经讲透的**动机**（比如为什么要 namespace、为什么两级加载会
拖垮准确率），只讲**怎么把动机变成能跑的代码**。建议对照
`steps/step09_mcp/` 下的真实文件读——本附录里的代码块，绝大多数就是从那些
文件里原样摘出来的，摘的时候我会说清楚摘自哪个文件的哪个函数。

## A0 · 动手之前，五个概念要先在脑子里过一遍

如果这五条你都清楚，可以直接跳到 A1。

**1. 子进程 + stdio 是什么。** `asyncio.create_subprocess_exec(...)` 会启动
一个新的操作系统进程，你的程序和它之间靠三根"管子"通信：`stdin`（你写它读）、
`stdout`（它写你读）、`stderr`（它的报错信息单独一根管子）。MCP 用的就是
这三根管子里的前两根——你往 `stdin` 写一行 JSON，它往 `stdout` 写一行 JSON
作为回应。`stderr` 不参与协议，但正文 §9.2 会告诉你为什么不能不管它。

**2. JSON-RPC 2.0 长什么样。** 一种极简的"喊话格式"，三种消息形状：

```python
# 1) 请求：我想问你点什么，带一个 id，我等着这个 id 的答案
{"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}

# 2) 响应：对某个 id 的请求的回答（成功用 result，失败用 error）
{"jsonrpc": "2.0", "id": 1, "result": {"tools": [...]}}
{"jsonrpc": "2.0", "id": 1, "error": {"code": -32601, "message": "..."}}

# 3) 通知：单纯地说一句话，没有 id，不需要任何人回它
{"jsonrpc": "2.0", "method": "notifications/message", "params": {...}}
```

看 `"id" in message` 和 `"method" in message` 这两个条件，就能分辨出这是
哪一种——这是 §3、A3 里 `_read_loop` 分流逻辑的全部理论基础，记住这张表，
后面那段 if/elif/else 就不再是"背下来的写法"，而是"自己也会这么写"。

**3. `asyncio` 里协程、Task、Future 的关系。**
- `async def foo(): ...` 定义一个**协程函数**；调用 `foo()` 得到一个
  **协程对象**，它什么都不会做，直到有人 `await` 它或者把它包进一个 Task。
- `asyncio.ensure_future(foo())` 把协程包成一个 **Task**，它会在后台
  "自己跑起来"，不需要谁去 `await` 它才推进——本章的读循环、drain stderr
  循环都是这么起来的。
- `loop.create_future()` 造一个**空的 Future**：一个还没有值的盒子。别人
  可以 `await` 它（等到盒子被填上为止），也可以在任何时候
  `future.set_result(x)` 或 `future.set_exception(e)` 把盒子填上。
  这是本章"发出去一个请求，不知道答案什么时候回来"这件事的核心工具：
  发请求的人拿到一个空盒子就先去等着，读线程收到对应的答案后再把盒子填上。

**4. 为什么大量用 `@dataclass(frozen=True)`。** `frozen=True` 让这个对象
建好之后不能再改字段——`ServerConfig`、`RemoteTool` 这些"描述一件事实"的
对象没有理由被谁悄悄改掉，写成 frozen 之后，如果哪里手滑写了
`tool.name = "x"`，Python 会直接抛异常，而不是让一个 bug 安安静静地存在。

**5. 用到的正则表达式只有一处，别被"正则"两个字吓到。**
`re.compile(r"[^a-zA-Z0-9_-]")` 的意思是"匹配任何一个不是字母、数字、
下划线、连字符的字符"，配合 `.sub("_", part)` 就是"把这些字符全部换成
下划线"。全篇就这一个正则，看懂这一个就够用了。

---

## A1 · 第一步：先写一个能跑的 MCP server（不装现成的）

正文 §2 已经说了为什么自己写。这里从空文件开始，一行一行搭。

> **下面这四十行是拿来读的，不是拿来留的。** 项目里的两个 server 都建在
> SDK 上（三个装饰器就够了，见 A1.1）。但把协议手写一遍能让你看清它到底
> 长什么样——**读一遍**和**维护一辈子**是两回事，这一节是前者。

先写一个比 `files_server.py` 更简单的玩具版，只有一个工具、没有任何故障
开关，目标是先把协议骨架吃透。新建 `toy_server.py`：

```python
import json
import sys

def _tools() -> list[dict]:
    return [
        {
            "name": "echo",
            "description": "Return the text you were given, unchanged.",
            "inputSchema": {
                "type": "object",
                "required": ["text"],
                "properties": {"text": {"type": "string"}},
            },
        }
    ]

def _call(name: str, arguments: dict) -> dict:
    if name == "echo":
        return {"content": [{"type": "text", "text": str(arguments.get("text", ""))}]}
    return {"content": [{"type": "text", "text": f"unknown tool: {name}"}], "isError": True}

def _send(message: dict) -> None:
    sys.stdout.write(json.dumps(message))
    sys.stdout.write("\n")
    sys.stdout.flush()          # 不 flush，对方永远收不到这一行

def main() -> None:
    for line in sys.stdin:                       # 逐行读，阻塞等下一行
        line = line.strip()
        if not line:
            continue
        message = json.loads(line)
        if "id" not in message:                   # 收到通知：不回
            continue

        method = message.get("method")
        if method == "initialize":
            result = {
                "protocolVersion": "2025-06-18",
                "capabilities": {"tools": {"listChanged": True}},
                "serverInfo": {"name": "toy", "version": "0.0.1"},
            }
        elif method == "tools/list":
            result = {"tools": _tools()}
        elif method == "tools/call":
            params = message.get("params") or {}
            result = _call(params.get("name", ""), params.get("arguments") or {})
        else:
            _send({"jsonrpc": "2.0", "id": message["id"],
                   "error": {"code": -32601, "message": f"method not found: {method}"}})
            continue

        _send({"jsonrpc": "2.0", "id": message["id"], "result": result})

if __name__ == "__main__":
    main()
```

逐处说明新手容易卡住的地方：

- **为什么用 `for line in sys.stdin` 而不是 `input()`？** 两者效果差不多，
  但 `for line in sys.stdin` 在对方关掉 `stdin`（EOF）的时候会自然结束
  循环，不需要额外捕获异常；`files_server.py` 用的就是这个写法。
- **`sys.stdout.flush()` 不能省。** Python 默认会给标准输出加缓冲；如果
  这个进程的 stdout 连着一个管道（而不是终端），缓冲区可能攒到几 KB
  才真正写出去。客户端那边会一直卡在 `readline()`，看起来像"卡死了"，
  其实只是对方的话还没吐出来。
- **收到的每一条消息先看有没有 `id`。** 没有 `id` 就是通知，协议规定
  **通知不需要回复，也不应该回复**——回复一条没人等待的消息，客户端会
  收到一条它压根没请求过的 "response"，这正是 §3 里 `MCP_LOG_NOISE`
  那个故障的反面：这次是服务端自己没搞清楚"发了什么算通知"。

单独验证这个 server 能不能正确说话，不需要写 client，用两条系统命令就够：

```bash
printf '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}\n{"jsonrpc":"2.0","id":2,"method":"tools/list","params":{}}\n' | python toy_server.py
```

看到两行 JSON 分别是 `serverInfo` 和 `tools` 列表，说明协议骨架是对的。
这一步比直接上手写 client 更快定位问题——如果连这一步都不对，去调 client
只会白费时间。

### A1.1 · 同一个 server，项目里实际的写法

上面那四十行看懂之后，就可以扔掉了。项目里的 `files_server.py` 是这样的：

```python
from mcp.server.mcpserver import MCPServer

server = MCPServer("toy")

@server.tool()
def echo(text: str) -> str:
    """Return the text you were given, unchanged."""
    return text

if __name__ == "__main__":
    server.run()
```

`inputSchema` 是从类型标注生成的，描述是从 docstring 来的，
`initialize` / `tools/list` / `tools/call` / 通知不回复 / flush ——
全都不用写。上面那一整节讲的每一个坑，这里一个都碰不到。

还有一个白送的行为，正文 §9.2 专门提过：工具里 `raise` 出来的异常自动变成
一个 `isError` 的结果，**连接不受影响**。手写那四十行里没有这一层——
一次 `UnicodeDecodeError` 就能把整个 server 连同它上面所有工具一起带走，
而这正是 MCP 规范要求 server 不能做的事。

---

## A2 · 第二步：一个只有 30 行的笨 client

有了能说话的 server，接下来写一个刚好够用、但正文会证明它不够好的 client，
先把"能跑通"这件事拿到手上，再去修它的毛病。

```python
import asyncio
import json

async def main() -> None:
    proc = await asyncio.create_subprocess_exec(
        "python", "toy_server.py",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
    )

    async def call(method: str, params: dict, request_id: int) -> dict:
        request = {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}
        proc.stdin.write((json.dumps(request) + "\n").encode("utf-8"))
        await proc.stdin.drain()          # 把上面写的字节真正推进管道
        line = await proc.stdout.readline()
        return json.loads(line)

    print(await call("initialize", {}, 1))
    print(await call("tools/list", {}, 2))
    print(await call("tools/call", {"name": "echo", "arguments": {"text": "hi"}}, 3))

    proc.terminate()

asyncio.run(main())
```

三个动作要记住顺序：**写（`write`）→ 推（`await drain()`）→ 读
（`await readline()`）**。`write()` 只是把字节放进一个内存缓冲区，
不保证立刻发出去；`await proc.stdin.drain()` 才是"等到对方真的收得下、
数据确实被发出去了"这一步，新手最容易漏掉它，漏了之后偶尔会出现"发出去的
请求，对方好像没收到"的诡异现象（尤其是消息比较大的时候）。

跑起来能看到三行正确的字典输出，跟正文 §2 描述的一样。同样会看到 Windows
下那串 `ValueError: I/O operation on closed pipe` 的收尾报错——因为
`proc.terminate()` 只是"发个信号让它退出"，既没有 `await proc.wait()`
等它真正退出，也没有关掉底层的 transport。这个笨 client 到这里为止，
够用来验证协议，但不能用在真正的 Agent 里，下一节修它。

---

## A3 · 第三步：把笨 client 升级成能扛住"通知"和"反向提问"的样子

### A3.1 为什么 `readline()` 这个假设本身就错了

`call()` 里 `line = await proc.stdout.readline()` 隐含一个假设：
**我发出去的下一次请求，对方吐出来的下一行，就是它的答案。** 这个假设在
§3 的 `MCP_LOG_NOISE` 实验里被打破：server 完全合规地在每次回复前先发一条
通知，笨 client 会把这条通知当成答案，`result` 这个 key 根本不存在，
`.get("result")` 悄悄返回 `None`，没有任何异常。

修法不是"跳过通知那一种特殊情况"，而是承认这根管道上一共有三种消息，
永远按 A0 第 2 条那张表分流。写代码之前先把这句话变成一个函数：

```python
def classify(message: dict) -> str:
    if "id" in message and "method" not in message:
        return "response"      # 我发出去的某个请求，答案回来了
    if "id" in message:
        return "request"       # 对方主动问我一个问题，我要答
    return "notification"      # 单纯的广播，丢掉
```

这个 `classify` 只是帮助理解，真正的实现直接把这个判断写成 `_read_loop`
里的 if/elif/else（下面 A3.4）。

### A3.2 数据结构：`ServerConfig`、`RemoteTool`、`McpError`

来自 `mcp.py`：

```python
class McpError(RuntimeError):
    """连不上、启动失败、或者协议层面说不通——跟"工具跑了但失败了"是两回事。"""

@dataclass(frozen=True)
class ServerConfig:
    name: str
    command: tuple[str, ...]
    env: dict[str, str] = field(default_factory=dict)
    cwd: str | None = None
    startup_timeout: float = DEFAULT_STARTUP_TIMEOUT
    tool_timeout: float = DEFAULT_TOOL_TIMEOUT

@dataclass(frozen=True)
class RemoteTool:
    server: str
    name: str                       # 发给 server 的原始名字，一个字都不能改
    description: str
    input_schema: dict[str, Any]
    read_only: bool | None = None   # server 没说 vs server 说了"不是只读"，两种状态
```

新手容易问的问题：**为什么 `env` 用 `field(default_factory=dict)` 而不是
直接写 `env: dict = {}`？** 因为 Python 里函数/类的默认值只会被求值
**一次**，如果直接写 `= {}`，所有没传 `env` 的 `ServerConfig` 实例会共享
**同一个** 字典对象——改了一个，其他全跟着变。`default_factory=dict`
告诉 dataclass"每次建实例都新造一个空字典"，这是写 dataclass 时的
标准反射动作，记住它，以后看到可变默认值就该条件反射地换成
`field(default_factory=...)`。

`read_only: bool | None`，不是 `bool = False`，是因为"server 没提供这个
标注"和"server 明确说了 `readOnlyHint: false`"是两件不同的事——前者是
"不知道"，后者是"知道，而且是否定的"。第 8 章的调度器要区分对待这两种
状态，所以数据结构上就不能把它们压成一个默认值。

### A3.3 `McpClient.__init__` 和 `start()`

```python
def __init__(self, config, *, handlers=None):
    self.config = config
    self.handlers = handlers or {}
    self._proc: asyncio.subprocess.Process | None = None
    self._reader: asyncio.Task | None = None
    self._stderr: asyncio.Task | None = None
    self._pending: dict[int, asyncio.Future] = {}   # id -> 还没填的盒子
    self._next_id = 0
    self._last_words = ""
    self.server_info: dict = {}
    self.failure: str | None = None
```

`_pending` 就是 A0 第 3 条说的"盒子的登记表"：`_request()` 发一个请求时，
先造一个空 Future 塞进这张表，key 是这次请求的 `id`；`_read_loop` 收到
一条 response，就凭它的 `id` 去表里找到对应的 Future，把答案填进去。

`start()` 分三段：起进程、起两个后台读取任务、握手。

```python
async def start(self) -> None:
    try:
        self._proc = await asyncio.create_subprocess_exec(
            *self.config.command,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,      # 不是 DEVNULL，见 §9.2
            env=_subprocess_env(self.config),
            cwd=self.config.cwd,
        )
    except OSError as exc:
        self.failure = f"could not start {self.config.name}: {exc}"
        raise McpError(self.failure) from exc

    self._reader = asyncio.ensure_future(self._read_loop())
    self._stderr = asyncio.ensure_future(self._drain_stderr())
    try:
        result = await asyncio.wait_for(
            self._request("initialize", {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {"elicitation": {}},
                "clientInfo": {"name": "minicodex", "version": "0.0.1"},
            }),
            timeout=self.config.startup_timeout,
        )
    except (TimeoutError, asyncio.TimeoutError) as exc:
        self.failure = f"{self.config.name} did not answer initialize within {self.config.startup_timeout:.0f}s"
        await self.close()
        raise McpError(self.failure) from exc
    except McpError:
        await self.close()
        raise
    self.server_info = result.get("serverInfo") or {}
    self._notify("notifications/initialized", {})
```

三个新手要留意的点：

1. **`asyncio.ensure_future(coro)` 而不是 `await coro`。** 如果写成
   `await self._read_loop()`，程序会卡在这一行，永远等这个"无限循环读
   下去"的协程跑完——而它设计上根本不会跑完。`ensure_future` 把它扔进
   后台去跑，`start()` 才能继续往下走到握手那一步。
2. **`asyncio.wait_for(fut, timeout=...)`** 是给"等一个 Future"这件事
   加一个超时闹钟。超时了会抛 `TimeoutError`（Python 3.11 之前是
   `asyncio.TimeoutError`，两者本章都捕获了，兼容新旧版本）。
   `except (TimeoutError, asyncio.TimeoutError)` 这种写法就是为了跨版本
   都能接住。
3. **握手失败要记得 `await self.close()` 再 `raise`。** 起了两个后台
   task、开了一个子进程，握手没成功不代表这些资源会自己收拾——不 close
   就 raise，等于把子进程和两个 task 都焅在原地，第 2 章 F02-08 那笔账
   在这里换了个形状重新出现。

### A3.4 `_send` / `_notify` / `_request`：三种"往外发"

```python
def _send(self, message: dict) -> None:
    proc = self._proc
    if proc is None or proc.stdin is None or proc.returncode is not None:
        raise McpError(self.failure or f"{self.config.name} is not running")
    try:
        proc.stdin.write((json.dumps(message) + "\n").encode("utf-8"))
    except (OSError, BrokenPipeError) as exc:
        raise McpError(f"{self.config.name} closed its input: {exc}") from exc

def _notify(self, method: str, params: dict) -> None:
    self._send({"jsonrpc": "2.0", "method": method, "params": params})

def _request(self, method: str, params: dict) -> asyncio.Future:
    self._next_id += 1
    request_id = self._next_id
    future = asyncio.get_running_loop().create_future()
    self._pending[request_id] = future          # 先登记
    try:
        self._send({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params})
    except McpError:
        self._pending.pop(request_id, None)     # 发送失败就撤销登记
        raise
    return future
```

注意 `_notify` 发的消息**没有 `id`**——这是通知和请求在代码层面唯一的
区别，跟 A0 第 2 条那张表对上了。

`_request` 为什么不写成 `async def`？这是本章一个容易写错的地方，
值得对着两种写法看一遍差别：

```python
# 容易写出来的、有 bug 的版本
async def _request_wrong(self, method, params):
    future = asyncio.get_running_loop().create_future()
    self._send({"jsonrpc": "2.0", "id": self._next_id, "method": method, "params": params})
    await asyncio.sleep(0)          # 假装这里有点别的 await
    self._pending[self._next_id] = future   # 登记动作被推迟到这里
    return future
```

如果调用方这样用它：

```python
fut = registry._request_wrong("tools/call", {...})   # 这一步本身也要 await 才启动执行
task = asyncio.ensure_future(fut)
task.cancel()   # 在 "登记" 真正发生之前就把外层任务取消掉
```

`_request_wrong` 是协程，它在被 `await`（或者被塞进 Task 调度）之前
**根本不会开始执行**——真正修法里那句"登记要发生在返回之前"，说的是
"调用方拿到返回值的那一刻，登记必须已经做完"。同步函数天然满足这一点：
调用 `self._request(...)` 这一行代码本身就是普通函数调用，`_pending[id]
= future` 在返回之前已经**同步执行完**，不存在"登记这一步还没轮到"的
窗口期。写成 `async def` 反而要额外操心"是不是有人在登记完成前就把
外层 await 取消了"，这类竞态本身就是新手最容易在 asyncio 里踩的坑之一：
**同步函数里的每一行都是原子的（对协程调度器而言不会被打断），协程函数
里的每一次 `await` 都是一个可能被打断的缝隙。** 能不用 `async def`
就不用，是消灭一整类竞态最省事的办法。

### A3.5 `_read_loop`：三路分流，本章的心脏

```python
async def _read_loop(self) -> None:
    proc = self._proc
    assert proc is not None and proc.stdout is not None
    try:
        while True:
            line = await proc.stdout.readline()
            if not line:                # 空字节串：对方关掉了 stdout
                break
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                continue                 # 一行读不懂就跳过，不是致命错误
            if not isinstance(message, dict):
                continue

            if "id" in message and "method" not in message:
                self._resolve(message)          # 情况一：响应
            elif "id" in message:
                await self._answer(message)     # 情况二：对方主动发来的请求
            # else：情况三，通知，什么都不做
    except asyncio.CancelledError:
        raise
    finally:
        with contextlib.suppress(TimeoutError, asyncio.TimeoutError):
            await asyncio.wait_for(proc.wait(), timeout=1.0)
        if self._stderr is not None:
            with contextlib.suppress(TimeoutError, asyncio.TimeoutError):
                await asyncio.wait_for(asyncio.shield(self._stderr), timeout=1.0)
        returncode = proc.returncode
        if self.failure is None and returncode not in (None, 0):
            self.failure = f"{self.config.name} exited with code {returncode}"
        if self._last_words:
            self.failure = f"{self.failure or ''}: {self._last_words}".lstrip(": ")
        self._settle_pending(self.failure or f"{self.config.name} closed the connection")
```

这个循环会一直跑到 `readline()` 返回空字节串为止——那意味着对方的
`stdout` 被关掉了，通常就是进程退出了。循环一结束（不管是正常退出还是被
取消），`finally` 块要做两件事：**弄清楚死因**（等一下 `returncode`，
再等一下 stderr 那个后台任务把最后一行读完），**通知所有还在等答案的人**
（`_settle_pending`）。这个 `finally` 不能省——如果 server 突然死掉，
`_pending` 里可能还挂着一个没人处理的 Future，没有 `_settle_pending`，
等它的那个调用会一直卡到 `tool_timeout`（默认 60 秒）才因为
`asyncio.wait_for` 的超时而失败，而这本来是可以立刻知道的。

### A3.6 `_resolve` 和 `_answer`：处理响应与处理反向请求

```python
def _resolve(self, message: dict) -> None:
    future = self._pending.pop(message["id"], None)
    if future is None or future.done():
        return
    if "error" in message:
        error = message["error"] or {}
        future.set_exception(McpError(f"{self.config.name}: {error.get('message', 'unknown error')}"))
    else:
        future.set_result(message.get("result") or {})
```

`_resolve` 就是"按 id 找到当初登记的盒子，把答案（或者错误）填进去"。
`future.done()` 这个检查是防御性的——理论上一个 id 不应该被答两次，
但万一 server 有 bug 发了重复的响应，往一个已经填过的 Future 里再
`set_result` 会直接抛 `InvalidStateError`，检查一下更稳。

```python
async def _answer(self, message: dict) -> None:
    method = message.get("method", "")
    handler = self.handlers.get(method)
    if handler is None:
        self._send({"jsonrpc": "2.0", "id": message["id"],
                     "error": {"code": -32601, "message": f"method not supported: {method}"}})
        return
    try:
        result = await handler(message.get("params") or {})
    except Exception as exc:
        self._send({"jsonrpc": "2.0", "id": message["id"],
                     "error": {"code": -32603, "message": str(exc)}})
        return
    self._send({"jsonrpc": "2.0", "id": message["id"], "result": result})
```

`_answer` 处理的是**对方主动发来的请求**（比如 §10 会讲的
`elicitation/create`）。`self.handlers` 是构造 `McpClient` 时传进来的
`{方法名: 处理函数}` 字典——客户端只回答它明确认识的方法，其余一律回
"method not supported"，这样一个 client 不会因为收到一个自己没准备好的
反向请求就崩掉。

### A3.7 `_drain_stderr`：读 stderr 的双重职责

```python
async def _drain_stderr(self) -> None:
    proc = self._proc
    assert proc is not None
    if proc.stderr is None:
        return
    while True:
        line = await proc.stderr.readline()
        if not line:
            return
        text = line.decode("utf-8", errors="replace").strip()
        if text:
            self._last_words = text[:400]
```

这个循环只做一件看起来很小的事：**把 stderr 上最后一行非空文本存下来**。
但它必须存在，理由是两条，不是一条：

1. **让"server 死了"这句话说得出原因。** §9.2 讲过，没有这个循环，
   client 只能说"files closed the connection"，读的人完全不知道为什么。
2. **防止 server 被自己的 stderr 卡死。** 操作系统的管道有容量上限
   （通常几十 KB），如果没有人在另一端读，写满之后**写的一方会被阻塞**。
   一个 traceback 打得比较长、或者一个话多的 server（比如调试模式下
   一直往 stderr 打日志），如果这根管道没人读，它自己会卡在
   `sys.stderr.write()` 上，永远也走不到下一行——即使你根本不关心它的
   日志内容，也必须有人在读，纯粹是为了不让它写满。

`decode("utf-8", errors="replace")` 也值得记一下：stderr 里出现非 UTF-8
字节是完全可能的（§9.2 里那个 `UnicodeDecodeError` 例子就是 server 自己
读到了一个 GBK 编码的文件），`errors="replace"` 让这里的解码永远不会
因为编码问题崩掉——这里的目标只是"尽量看懂最后一句话"，不是"精确还原"。

### A3.8 `_settle_pending` 和 `close()`

```python
def _settle_pending(self, reason: str) -> None:
    for future in self._pending.values():
        if not future.done():
            future.set_exception(McpError(reason))
    self._pending.clear()
```

一句话：把所有还没等到答案的请求，全部立刻判定为失败，而不是让它们
干等到超时。

```python
async def close(self) -> None:
    for task in (self._reader, self._stderr):
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
    self._reader = self._stderr = None

    proc = self._proc
    if proc is None:
        return
    if proc.returncode is None:
        if proc.stdin is not None and not proc.stdin.is_closing():
            with contextlib.suppress(OSError, BrokenPipeError):
                proc.stdin.close()
        try:
            await asyncio.wait_for(proc.wait(), timeout=2.0)
        except (TimeoutError, asyncio.TimeoutError):
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
            await proc.wait()
    for pipe in (proc.stdin, proc.stdout, proc.stderr):
        transport = getattr(pipe, "_transport", None) or getattr(pipe, "transport", None)
        if transport is not None:
            with contextlib.suppress(Exception):
                transport.close()
    self._settle_pending(self.failure or f"{self.config.name} was shut down")
    self._proc = None
```

四步顺序，每一步都对应一个"如果跳过会怎样"：

1. **先取消两个后台 task 并等它们真正结束。** 不这样做，`close()`
   返回之后这两个循环可能还在跑，读着一个即将被关掉的管道。
2. **关 `stdin`，让守规矩的 server 自己因为 EOF 退出，等最多 2 秒。**
   这是"先礼后兵"——大多数写得规矩的进程看到输入流关闭就会主动退出，
   不需要你去强杀它。
3. **2 秒等不到就 `kill()`。** 兵，不能没有——一个卡死的进程不会自己
   走，必须有人动手。
4. **显式关掉每一根管道的 transport。** 这是 §2 里那串
   `ValueError: I/O operation on closed pipe` 报错的根源：如果不做这一步，
   垃圾回收器最终会在某个不确定的时刻（可能是很久以后）替你关，而它是在
   `__del__` 里做的，这时候事件循环可能已经不在了，就会报出那个看起来
   跟你的代码毫无关系的错误。**显式关，永远比等 GC 关靠谱。**

### A3.9 `list_tools()` 和 `call()`：真正会被外部调用的两个方法

```python
async def list_tools(self) -> list[RemoteTool]:
    result = await asyncio.wait_for(self._request("tools/list", {}), timeout=self.config.tool_timeout)
    tools = []
    for raw in result.get("tools") or []:
        if not isinstance(raw, dict) or not isinstance(raw.get("name"), str):
            continue
        annotations = raw.get("annotations") or {}
        hint = annotations.get("readOnlyHint")
        tools.append(RemoteTool(
            server=self.config.name,
            name=raw["name"],
            description=str(raw.get("description") or ""),
            input_schema=raw.get("inputSchema") or {"type": "object", "properties": {}},
            read_only=hint if isinstance(hint, bool) else None,
        ))
    return tools

async def call(self, name: str, arguments: dict) -> dict:
    return await asyncio.wait_for(
        self._request("tools/call", {"name": name, "arguments": arguments}),
        timeout=self.config.tool_timeout,
    )
```

两点值得注意：

- `list_tools()` 对每一条原始工具描述都做了防御性检查（`isinstance`
  一路查下去）——**server 是别人写的**，`tools/list` 返回的数组里混进
  一条形状不对的记录（比如 `name` 缺失或者不是字符串）完全可能发生，
  这里选择"跳过这一条，继续处理其他"，而不是让一整个 `list_tools()`
  因为别人一条脏数据而抛异常。
- `call()` 自己**不做任何"这次调用是不是失败了"的判断**——它只负责
  把请求发出去、等答案回来。至于 `result` 里 `isError` 是不是 `true`，
  留给上一层的 `normalise()`（A5）去处理。这是刻意的分工：`McpClient`
  只管"协议通不通"，"这次工具调用本身是否成功"是另一层的关注点。

到这里，一个完整能用的 `McpClient` 就写完了。它现在能扛住通知、能回答
反向请求、能在任何一步失败时把资源收拾干净、任何等待都有超时兜底。

---

## A4 · 第四步：两个 server 都有一个叫 `search` 的工具，怎么合并

正文 §4、§5 已经讲清楚"为什么"。这里只讲"怎么写"。

```python
PREFIX = "mcp"
DELIMITER = "__"
_UNSAFE = re.compile(r"[^a-zA-Z0-9_-]")

def sanitize(part: str) -> str:
    return _UNSAFE.sub("_", part)

def model_name(server: str, tool: str) -> str:
    return f"{PREFIX}{DELIMITER}{sanitize(server)}{DELIMITER}{sanitize(tool)}"

def is_remote(name: str) -> bool:
    return name.startswith(f"{PREFIX}{DELIMITER}")
```

自己动手跑一遍就能理解 `sanitize` 在干什么：

```python
>>> model_name("notes", "search")
'mcp__notes__search'
>>> model_name("files", "search")
'mcp__files__search'
>>> sanitize("bigcorp.internal")   # 点号会让整轮请求 400，见正文 §5
'bigcorp_internal'
```

`Registration` 是一个"打包"用的 dataclass，把模型看到的名字、原始工具、
它属于哪个 client 存在一起：

```python
@dataclass(frozen=True)
class Registration:
    model_name: str
    tool: RemoteTool
    client: McpClient
```

真正做"合并 + 去重"的是 `add()`：

```python
def add(self, client: McpClient, tools: list[RemoteTool]) -> list[str]:
    added: list[str] = []
    for tool in tools:
        name = model_name(tool.server, tool.name)
        if len(name) > MAX_TOOL_NAME:
            self.retired[name[:MAX_TOOL_NAME]] = (
                f"{tool.server}/{tool.name} was not registered: its name is "
                f"{len(name)} characters and the limit is {MAX_TOOL_NAME}"
            )
            continue
        if name in self.registrations:
            self.retired[name] = (
                f"{tool.server}/{tool.name} was not registered: its name collides "
                f"with {self.registrations[name].tool.name} after sanitising"
            )
            continue
        self.registrations[name] = Registration(name, tool, client)
        added.append(name)
    self._restage()
    return added
```

跟着代码走一遍处理一批工具的过程：对每一个 `RemoteTool`，先算出它的
模型可见名字；太长就拒绝并记进 `retired`（不是截断——截断会把两个不同
的长名字压成同一个短名字，等于自己制造一次 F09-01）；跟已经注册过的
名字撞车也拒绝（这种撞车通常发生在 `sanitize` 把两个不同字符的名字
压成同一个的时候）；两关都过了才真正塞进 `self.registrations`。
最后调一次 `self._restage()`——这一步在 A6 详细讲，它负责决定"这一批
工具的 schema 现在能不能全部塞进模型看到的列表里"。

**记住这条规则：`Registration.model_name` 是给模型看的，
`Registration.tool.name` 是发给 server 的。** 真正调用的地方永远用
后者：

```python
result = await registration.client.call(registration.tool.name, arguments)
```

只要在任何地方手滑把这两个字段用反了，工具调用就会发出去一个 server
根本不认识的名字——这类 bug 编译期、类型检查都发现不了，只有跑起来
才会报"unknown tool"，所以正文 §5 强调"分成两个字段存"是一个纪律，
不是一句空话。

---

## A5 · 第五步：把远端返回值捏成一个字符串

`normalise()` 逐分支讲。先看整体结构，再看每一个 `elif`：

```python
def normalise(result: dict) -> str:
    blocks = result.get("content")
    parts: list[str] = []
    if isinstance(blocks, list):
        for block in blocks:
            if not isinstance(block, dict):
                parts.append(f"[non-object content block: {type(block).__name__}]")
                continue
            kind = block.get("type")
            if kind == "text":
                parts.append(str(block.get("text", "")))
            elif kind == "image":
                mime = block.get("mimeType", "image/?")
                size = len(str(block.get("data", "")))
                parts.append(f"[image omitted: {mime}, {size} base64 characters]")
            elif kind == "audio":
                parts.append(f"[audio omitted: {block.get('mimeType', 'audio/?')}]")
            elif kind == "resource":
                resource = block.get("resource") or {}
                uri = resource.get("uri", "?")
                if isinstance(resource.get("text"), str):
                    parts.append(f"[resource {uri}]\n{resource['text']}")
                else:
                    parts.append(f"[binary resource omitted: {uri}]")
            elif kind == "resource_link":
                parts.append(f"[resource link: {block.get('uri', '?')}]")
            else:
                parts.append(f"[unsupported content block of type {kind!r}]")

    structured = result.get("structuredContent")
    if structured is not None:
        parts.append("structuredContent: " + json.dumps(structured, ensure_ascii=False))

    text = "\n".join(part for part in parts if part) or "(the tool returned no content)"

    if result.get("isError"):
        text = f"The tool reported an error.\n{text}"

    if len(text) > MAX_RESULT_CHARS:
        half = MAX_RESULT_CHARS // 2
        omitted = len(text) - 2 * half
        text = f"{text[:half]}\n... ({omitted} characters omitted) ...\n{text[-half:]}"
    return text
```

把它当成一份"这种情况怎么办"的对照表来读：

| `content` 里的 block 类型 | 怎么处理 | 为什么 |
|---|---|---|
| `text` | 原样取出 `text` 字段 | 这是唯一模型能直接读的类型 |
| `image` | 换成一句 `[image omitted: ...]` | base64 图片是模型读不懂的 token，还会永远赖在历史里 |
| `audio` | 同上思路 | 同上 |
| `resource`，带 `text` | 取出 `text` 内容 | 有文字就当文字用 |
| `resource`，不带 `text`（比如二进制） | 换成占位描述 | 同图片 |
| `resource_link` | 换成占位描述，带上 uri | 只是个指针，不含内容 |
| 未知类型 | 换成 `[unsupported content block of type ...]` | 协议以后可能加新类型，不能因为不认识就崩 |

再往下三个判断，各自对应一件容易被忽略的事：

1. **`"\n".join(...) or "(the tool returned no content)"`**——如果
   `parts` 是空列表（比如 `content` 本身就是 `[]`），`"\n".join([])`
   会得到空字符串 `""`。一个空字符串在模型看来跟"这个工具成功了，
   只是没什么好说的"没有任何区别，它会当成一个正常答案接着往下走。
   所以必须显式给一句"这里真的什么都没有"。
2. **`if result.get("isError")`**——这是"传输成功、工具本身失败"这类
   信息唯一能传给模型的入口。不加这一句，一句正常的返回和一句
   `no such file: nope.toml` 在格式上完全一样，模型分不出来。
3. **超过 `MAX_RESULT_CHARS`（20000）就掐头去尾，中间标注省略了多少**——
   跟第 2 章截 shell 输出的 `_clip()` 是同一个做法：只留尾巴会丢掉最前面
   往往是最关键的那句报错。

自己跑一遍就能验证这几条边界：

```python
>>> normalise({"content": []})
'(the tool returned no content)'
>>> normalise({"content": [{"type": "text", "text": "ok"}], "isError": True})
'The tool reported an error.\nok'
```

---

## A6 · 第六步：token 预算与两级加载——最容易写歪的一节

正文 §6、§7 已经把"为什么触发条件是 token 预算而不是工具数量"讲透了。
这里只关心把这句话变成代码时，容易踩的三个坑。

### A6.1 先搞清楚 `visible` 是谁的、被谁改

```python
class McpRegistry:
    def __init__(self, *, schema_budget=DEFAULT_SCHEMA_BUDGET, local=None):
        self.schema_budget = schema_budget
        self.local = list(local or [])
        self.registrations: dict[str, Registration] = {}
        self.visible: list[dict] = list(self.local)     # 模型现在能看到的 schema 列表
        self.deferred: set[str] = set()                 # 已注册但暂时没进 visible 的工具名
        self.failures: dict[str, str] = {}
        self.retired: dict[str, str] = {}
```

`self.visible` 是一个 **list 对象**。这个对象后面会被原样交给发请求的
模型客户端（`ChatCompletionsModel(..., tools=registry.visible)`）。
关键在于：交给模型客户端的是**这个对象本身**，不是它当时的内容快照。
用一张图来记：

```
registry.visible  ──┐
                     ├──>  同一个 list 对象在内存里只有一份
llm.tools         ──┘

reveal() 往这个对象里 append 一条 schema
        ↓
llm 下次构造请求时重新读 self.tools，读到的是刚刚被 append 过的版本
```

一旦有人写成 `tools=list(registry.visible)`，`list(...)` 会**复制**出
一份新的列表——从这一刻起，`llm.tools` 和 `registry.visible` 就是两个
不同的对象，`tool_search` 之后往 `registry.visible` 里 `append`，
`llm.tools` 那份复制品完全不会跟着变。这段代码**不会报任何错**，类型
检查也通过，`tool_search` 甚至还会正常返回"已加载 N 个工具"——但模型
实际收到的下一轮请求里，那些工具的 schema 根本不存在。这是本章最容易
写错、又最难在测试之外发现的一处，正文 §15 专门给它配了一条以这个错误
命名的测试，值得记住这个教训：**"传引用还是传副本"这种选择，一旦选错，
后果往往是完全沉默的。**

### A6.2 `_restage()`：决定"全展示"还是"退成索引"

```python
def _restage(self) -> None:
    names = sorted(self.registrations)
    schemas = [self.schema_for(name) for name in names]
    if not names or estimate_messages((), self.local + schemas) <= self.schema_budget:
        self.deferred = set()
        self.visible[:] = self.local + schemas
    else:
        self.deferred = set(names)
        self.visible[:] = [*self.local, self.search_schema()]
```

两个写法上的细节：

- **`self.visible[:] = ...` 而不是 `self.visible = ...`。** 前者是
  "原地替换列表内容"（切片赋值），后者是"把 `self.visible` 这个名字
  重新绑定到一个新对象上"。差别正好呼应上一节：`llm.tools` 手里拿的是
  原来那个 list 对象的引用，如果这里写成后者，`self.visible` 这个属性
  名指向了新对象，但 `llm.tools` 还攥着旧对象不放，两者立刻分家。
  `[:] =` 保证不管什么时候调用 `_restage()`，改的都是同一个对象的内容。
- **判断条件用 `estimate_messages` 估算 token，不是数 `len(names)`。**
  这行代码就是正文 §7.5 那个结论的落地：触发两级加载的是"这些 schema
  加起来估出来多少 token"，不是"一共有多少个工具"。`estimate_messages`
  是第 6 章就写好的那把尺子，这里直接复用，不用重新发明一个计数方式。
- **要么全展示要么全部退成索引，没有"展示一半"这个分支。** 代码里
  体现为：满足预算就整批赋值 `self.local + schemas`，不满足就整批换成
  `[*self.local, self.search_schema()]`，中间没有"挑一部分展示"的逻辑，
  这是故意不写的，正文 §7 结尾解释了为什么半藏是两头不讨好。

### A6.3 `schema_for()`：把一个 `RemoteTool` 变成模型认识的 function schema

```python
def schema_for(self, name: str) -> dict:
    registration = self.registrations[name]
    return {
        "type": "function",
        "function": {
            "name": name,                                    # 模型可见名字
            "description": registration.tool.description,
            "parameters": registration.tool.input_schema,     # server 给的 inputSchema 原样搬过来
        },
    }
```

这是 OpenAI 风格的 `tools` 数组里一条记录的标准形状：`type` 固定是
`"function"`，`function.name` 用的是 `model_name`（也就是
`mcp__server__tool` 这种带命名空间的名字），`function.parameters` 直接
用 server 声明的 `inputSchema`——MCP 的 `inputSchema` 本来就是标准
JSON Schema，跟 OpenAI 的 `parameters` 字段格式是兼容的，不需要转换。

### A6.4 `reveal()`：把一个工具从"只有名字"变成"有完整 schema"

```python
def reveal(self, names: list[str]) -> list[str]:
    revealed = []
    for name in names:
        if name in self.deferred:
            self.deferred.discard(name)
            self.visible.append(self.schema_for(name))    # 追加，不是重建
            revealed.append(name)
    if revealed:
        for index, schema in enumerate(self.visible):
            if schema["function"]["name"] == "tool_search":
                self.visible[index] = self.search_schema()  # 重新算一遍索引文字
                break
    return revealed
```

两步都不能漏：

1. **`self.visible.append(...)`**——继续用同一个对象，往里追加，
   这样 `llm.tools` 立刻就能看见新工具，不需要谁重新赋值。
2. **重新生成 `tool_search` 自己的 schema，替换掉旧的那一条。**
   为什么？因为 `tool_search` 的 `description` 里嵌着"当前还没加载的
   工具索引"（下一节 `index()`），如果揭示了几个工具之后不刷新这条
   描述，模型会一直看到一份过时的索引，里面列着已经加载过的工具，
   这是新手实现两级加载时很容易漏掉的一步——只记得往 `visible` 里
   加新工具，忘了同步更新 `tool_search` 自己那条 schema 的文字内容。

### A6.5 `search()`：打分算法，别想复杂了

```python
def search(self, query: str, limit: int = DEFAULT_SEARCH_LIMIT) -> str:
    terms = [term for term in re.split(r"[^a-zA-Z0-9]+", query.lower()) if term]
    scored: list[tuple[int, str]] = []
    for name in sorted(self.deferred):
        registration = self.registrations[name]
        haystack = f"{name} {registration.tool.description}".lower()
        score = sum(1 for term in terms if term in haystack)
        if score:
            scored.append((-score, name))
    scored.sort()                              # 分数取负后排序，分高的排前面
    chosen = [name for _, name in scored[: max(1, limit)]]
    ...
```

这不是向量搜索，不需要 embedding、不需要任何第三方库。步骤就是：

1. 把 query 按非字母数字字符切成单词（`re.split(r"[^a-zA-Z0-9]+", ...)`），
   转小写。
2. 对每个还没展示的工具，把它的名字和描述拼成一个字符串（也转小写），
   数一数 query 里有几个词出现在这个字符串里，这个数字就是它的分数。
3. 按分数从高到低排序（技巧：把分数取负号再正向排序，等价于按分数
   降序），取前 `limit` 个。

正文用真实 API 测过：这种朴素算法配合"名字 + 一句话描述"的索引，
足够让模型在 61 个各不相同的真实工具里找到目标。**先别急着上更复杂的
方案**——本章的测量结论是问题不在搜索算法本身，是"藏起来"这件事本身
在小规模工具集上就不划算（正文 §7.5）。

### A6.6 `index()` 和 `search_schema()`：拼出 `tool_search` 的 description

```python
def index(self) -> str:
    lines = []
    for name in sorted(self.deferred):
        description = self.registrations[name].tool.description.strip()
        first = description.split(". ")[0].rstrip(".")   # 只取第一句
        lines.append(f"- {name}: {first}" if first else f"- {name}")
    return "\n".join(lines)

def search_schema(self) -> dict:
    sources = sorted({r.tool.server for r in self.registrations.values()})
    return {
        "type": "function",
        "function": {
            "name": "tool_search",
            "description": (
                "Load the full parameter schema for tools that are listed below by "
                "name only, so that you can call them. You cannot call a tool from "
                "this list until you have loaded it. Sources connected: "
                f"{', '.join(sources) or '(none)'}.\n\n"
                f"Tools available but not yet loaded:\n{self.index()}"
            ),
            "parameters": {
                "type": "object",
                "required": ["query"],
                "properties": {
                    "query": {"type": "string", "description": "Words from the names or descriptions listed above. Example: notes search keyword"},
                    "limit": {"type": "integer", "description": f"How many tools to reveal. Defaults to {DEFAULT_SEARCH_LIMIT}."},
                },
            },
        },
    }
```

`index()` 只取每个工具描述的**第一句**（用 `". "` 切一刀），是为了控制
索引本身的 token 开销——正文 §7.4 量过，61 个工具的完整索引（名字 +
一行描述）是 1399 token，如果每条都放完整描述会更贵。`search_schema()`
把这份索引直接写进 `tool_search` 自己的 `description` 字段——这是
"先名字 + 一句话，需要时再拉完整 schema"这句话的真正落地方式：模型
**在决定要不要调用 `tool_search` 之前**，就已经能在它的 description
里看到有哪些工具、大概是干什么的。

`search_handler()` 把 `search()` 包成一个真正的 `ToolFn`（`agent_types.py`
定义的"参数进、字符串出"的函数签名），供 Agent 的工具表使用：

```python
def search_handler(self) -> ToolFn:
    async def handler(arguments: dict) -> str:
        query = arguments.get("query")
        if not isinstance(query, str):
            return tool_error(
                'tool_search needs a "query" argument, a string',
                you_sent=repr(arguments.get("query")),
                do_this='Example: {"query": "search saved notes by keyword"}',
            )
        limit = arguments.get("limit")
        return self.search(query, limit if isinstance(limit, int) else DEFAULT_SEARCH_LIMIT)
    return handler
```

注意参数校验用的是 `tool_error()`（A0 之外的一个小知识点：这是第 3 章
定下的"三段式错误"格式——出了什么问题、你发了什么、接下来该怎么做，
定义在 `tool_errors.py`）。哪怕是 `tool_search` 自己收到一个格式错误的
调用，也要按同一套错误消息规范来答，不能因为它是"框架自带的工具"就
搞特殊。

---

## A7 · 第七步：让 Agent 扛得住 server 挂掉、起不来

### A7.1 起不来：`connect()` 里的 try/except

```python
async def connect(configs, registry, *, handlers=None) -> list[McpClient]:
    clients: list[McpClient] = []
    for config in configs:
        client = McpClient(config, handlers=handlers)
        try:
            await client.start()
            registry.add(client, await client.list_tools())
        except (McpError, TimeoutError, OSError) as exc:
            registry.failures[config.name] = str(exc)
            await client.close()
            continue          # 跳过这一个，接着连下一个
        clients.append(client)
    return clients
```

新手写这段代码最容易犯的错误是把 `try` 只包住 `client.start()`，漏了
`client.list_tools()`——如果 `start()` 成功但 `list_tools()` 超时
（比如 server 握手很快但第一次真正列工具时卡住了），不把它也纳入
`try` 块，这个异常会直接冒到 `connect()` 外面，把整个启动过程打断，
跟"一个 server 坏了，剩下四个还能用"这个目标背道而驰。这里的写法是把
"启动 + 拿工具表"当成一个整体的"这个 server 能不能用"来判断。

### A7.2 调用时才重连：`_handler()` 完整逻辑

这是全篇最长的一段判断逻辑，逐段拆开看：

```python
def _handler(self, name: str) -> ToolFn:
    async def handler(arguments: dict) -> str:
        registration = self.registrations.get(name)
        if registration is None:                       # 第一关：这个名字还注册着吗
            return tool_error(
                f"{name} is no longer available",
                you_sent=json.dumps(arguments)[:200],
                do_this=self.explain(name),
            )
        stopped = registration.client.failure
        if not registration.client.alive:                # 第二关：它现在活着吗
            report = await self.reconnect(registration.tool.server)
            registration = self.registrations.get(name)   # 重连可能改变了注册表，重新查一次
            if registration is None:
                return tool_error(
                    f"{name} could not be called",
                    you_sent=json.dumps(arguments)[:200],
                    do_this=(
                        f"The server providing it stopped ({stopped or 'no reason reported'})"
                        f" and {report}. Use a local tool instead, or tell the user."
                    ),
                )
        try:
            result = await registration.client.call(registration.tool.name, arguments)
        except (TimeoutError, McpError) as exc:            # 第三关：这一次调用本身失败了吗
            return tool_error(
                f"{name} did not return",
                you_sent=json.dumps(arguments)[:200],
                do_this=f"The server said: {exc}. Try a different approach.",
            )
        return normalise(result)
    return handler
```

按顺序读，这是一套"三道关卡"：

1. **名字还在注册表里吗？** 不在——可能是被 `forget()` 退休了——直接
   走 `explain(name)` 给出退休原因（A8 之外的一个细节：`explain()`
   会先查 `self.retired` 字典，有记录就把记录的原因说出来）。
2. **对应的 client 现在还活着吗（`registration.client.alive`）？**
   不活——先尝试重连（`self.reconnect(...)`，下面 A7.3），重连之后
   **重新查一次 `self.registrations.get(name)`**——因为重连可能会让
   这个工具彻底消失（server 换了个版本，不再提供它了），这时候旧的
   `registration` 变量已经过时，必须用新查到的结果判断。
3. **调用本身超时或者协议层出错了吗？** 抓住 `TimeoutError` 和
   `McpError`，回一句"服务器说了什么"给模型，并且**这次调用不重试**——
   正文 §9.3 已经解释过为什么：这次调用飞出去的时候到底有没有真的
   执行、有没有产生副作用，这一层根本无法知道，"不知道"不是靠"再打一次"
   能消除的状态。真正的重试机会留给了**下一次**调用同一个工具的时候——
   那时候会走到第 2 关，`alive` 是 `False`，触发一次全新的重连。

### A7.3 `reconnect()`：重启并重新读工具表

```python
async def reconnect(self, server: str) -> str:
    registrations = [r for r in self.registrations.values() if r.tool.server == server]
    client = registrations[0].client if registrations else None
    if client is None:
        return f"{server} is not a server this session knows about"
    await client.close()
    client.failure = None
    try:
        await client.start()
        tools = await client.list_tools()
    except (McpError, TimeoutError, OSError) as exc:
        self.failures[server] = str(exc)
        self.forget(server, f"the MCP server {server!r} stopped and would not restart: {exc}")
        return f"{server} could not be restarted: {exc}"
    before = {r.model_name for r in registrations}
    self.forget(server, f"the MCP server {server!r} restarted without this tool")
    self.add(client, tools)
    after = {name for name, r in self.registrations.items() if r.tool.server == server}
    gone = sorted(before - after)
    new = sorted(after - before)
    changes = []
    if gone:
        changes.append(f"gone: {', '.join(gone)}")
    if new:
        changes.append(f"new: {', '.join(new)}")
    return f"{server} restarted ({'; '.join(changes) or 'same tools'})"
```

关键的一步是 `self.forget(server, ...)` 后面紧跟着 `self.add(client,
tools)`——**先把这个 server 名下所有旧的注册全部退休，再用刚刚拿到的
最新工具表重新注册一遍**，而不是"尝试更新已有的注册"。原因是重启后的
进程完全可能带回一份不同的工具表（版本变了、配置变了），旧的注册表
项如果不清空，会留下一堆指向"新进程根本不提供"的工具名，调用它们会得到
一个更难理解的失败。`before`/`after` 两个集合只是为了算出重启前后
"哪些工具消失了、哪些是新出现的"，写进返回的说明文字里，方便日志或者
模型理解发生了什么。

### A7.4 `forget()`：删除时留一个理由

```python
def forget(self, server: str, reason: str | None = None) -> None:
    because = reason or f"the MCP server {server!r} is no longer connected"
    for name, registration in list(self.registrations.items()):
        if registration.tool.server == server:
            self.retired[name] = because
            del self.registrations[name]
    self._restage()
```

`list(self.registrations.items())`——在 Python 里**一边遍历字典一边
删除它的 key 会报 `RuntimeError: dictionary changed size during
iteration`**，这是新手很容易踩的一个坑。`list(...)` 先把当前所有
键值对复制成一个独立的列表，遍历这份复制品、修改原字典，就不会触发
这个错误。删除之前先把原因记进 `self.retired[name]`，是 §12 的核心：
模型的历史记录里可能十轮之前刚调用过这个工具名，直接返回"没有这个
工具"会让模型以为自己记错了名字、试图去猜别的写法，而实际的原因是
"这个工具曾经存在，后来消失了"——两种情况需要模型采取完全不同的行动。

---

## A8 · 第八步：server 反过来问你（elicitation）

```python
def elicitation_handler(session: Session) -> RequestHandler:
    async def handle(params: dict) -> dict:
        message = str(params.get("message") or "a server is asking for confirmation")
        schema = params.get("requestedSchema") or {}
        properties = schema.get("properties") or {}
        booleans = [key for key, spec in properties.items() if (spec or {}).get("type") == "boolean"]
        if len(properties) != 1 or not booleans:
            return {"action": "decline"}
        reply = await session.approver.ask(
            ApprovalRequest(
                what=message,
                reason="an MCP server is asking before it acts",
                risk=Risk.UNKNOWN,
                suggested_rule=None,
            )
        )
        if not reply.approved:
            return {"action": "decline"}
        return {"action": "accept", "content": {booleans[0]: True}}
    return handle
```

一步步看它在做什么：

1. **先看 server 想问的问题长什么形状。** `requestedSchema.properties`
   是一个 JSON Schema，描述 server 想要客户端填哪些字段。这个函数只
   愿意回答**恰好一个字段、且这个字段是布尔类型**的问题——也就是
   "是/否"这种最简单的确认。
2. **形状不对就直接 `decline`。** 比如 server 想要一整个对象（用户名、
   密码、一个日期……），这个客户端压根不知道怎么替用户填，`decline`
   是 MCP 协议里合法的回答，意思就是"用户不同意"。**编一个值把 schema
   填满是绝对不允许的**——那是把一个凭空捏造的值写进别人的系统。
3. **形状对了，就用第 5 章现成的审批机制去问真正的用户（或者按已经
   配置好的策略自动判断）。** `session.approver.ask(ApprovalRequest(...))`
   跟第 5 章批准 shell 命令、批准 apply_patch 用的是**同一个**接口——
   这里没有另起一套"MCP 专属"的确认弹窗，正是为了不让用户在两种问法之间
   养成"看都不看就点同意"的习惯。
4. **`reply.approved` 是 `True` 才回 `accept`，并把布尔字段设成
   `True`；否则回 `decline`。**

如果你对 `Session`、`ApprovalRequest`、`Risk` 这几个类型完全陌生也没
关系——`elicitation_handler` 用到它们的方式很窄：`session.approver`
有一个 `async def ask(request) -> 一个带 .approved 布尔属性的对象`，
`ApprovalRequest` 只是把"要问什么、为什么问、风险等级"打包成一个参数。
把这个函数当成"一座桥"来理解就够了：一边是 MCP 协议的
`elicitation/create`，另一边是第 5 章已经写好的审批系统，这个函数只
负责把两边的形状对上。

---

## A9 · 第九步：远端工具的 footprint（对接第 8 章调度器）

如果你还没做到第 8 章的并发调度，这一节可以先跳过——`footprint_of` 只在
接了第 8 章的 `Agent(..., footprint_of=...)` 参数时才会被用到。

```python
def footprint_of(self, call: ToolCall) -> Footprint:
    registration = self.registrations.get(call.name)
    if registration is None or registration.tool.read_only is not True:
        return STATEFUL
    return Footprint(reads=frozenset({f"mcp:{registration.tool.server}"}))
```

翻译成大白话：**默认假设一个远端工具会改动状态（`STATEFUL`），只有
在它明确声明了 `readOnlyHint: true` 的时候，才给它一个"只读"的
footprint——而且这个"只读"footprint 读的资源键是 `mcp:{server 名字}`
这样一个粗粒度的标记，不是具体某个文件路径。** 为什么不能给一个空的
`Footprint()`（也就是"完全不碰任何东西，可以跟谁并发都行"）？因为一个
远端工具内部到底读了什么、写了什么，这个进程完全看不见——它可能读的
正是本地这一轮 `apply_patch` 正在写的那个文件，只是恰好这一次的调用
没有暴露出来。所以这里的信任是有上限的：**只买"同一个 server 上的
两个只读调用可以互相重叠"，不买"这个工具绝对不会影响其它任何东西"。**

`route_footprint` 负责把"本地工具"和"远端工具"两套 footprint 计算逻辑
接到第 8 章调度器要求的同一个函数签名上：

```python
def route_footprint(registry: McpRegistry, local: FootprintFn) -> FootprintFn:
    def footprint(call: ToolCall) -> Footprint:
        return registry.footprint_of(call) if is_remote(call.name) else local(call)
    return footprint
```

`is_remote(call.name)` 就是 A4 写的那个"名字是不是以 `mcp__` 开头"的
判断——用一个字符串测试来分流，而不是让 `tools.py`（管本地工具的模块）
去 `import` `registry.py`，或者反过来。这样两个模块继续保持互不知道
对方存在，只有 `route_footprint` 这一个函数同时认识两边。

---

## A10 · 第十步：接进 CLI

### A10.1 配置文件长什么样，`load_config()` 怎么读它

```json
{
  "servers": {
    "files": {"command": ["python", "mcp_servers/files_server.py"]},
    "notes": {"command": ["python", "mcp_servers/notes_server.py"], "env": {"TOKEN": "..."}}
  }
}
```

```python
def load_config(path: Path) -> list[ServerConfig]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    servers = raw.get("servers")
    if not isinstance(servers, dict):
        raise McpError(f"{path}: expected an object with a 'servers' key")
    configs = []
    for name, spec in servers.items():
        command = (spec or {}).get("command")
        if not isinstance(command, list) or not command:
            raise McpError(f"{path}: server {name!r} has no 'command' list")
        configs.append(ServerConfig(
            name=name,
            command=tuple(str(part) for part in command),
            env={str(k): str(v) for k, v in ((spec or {}).get("env") or {}).items()},
            cwd=(spec or {}).get("cwd"),
            startup_timeout=float((spec or {}).get("startup_timeout", 30.0)),
            tool_timeout=float((spec or {}).get("tool_timeout", 60.0)),
        ))
    return configs
```

就是"读 JSON、逐字段做类型检查、缺了必填字段就报错、其余字段给默认值"，
没有魔法。唯一值得留意的是每个字段都做了显式的类型转换
（`str(part)`、`float(...)`）——配置文件是用户手写的，字段值完全可能
是字符串形式的数字或者其他类型，转换一次比信任 JSON 解析出来的原始
类型更稳。

### A10.2 子进程能看到哪些环境变量

```python
ENV_ALLOWLIST = ("PATH", "HOME", "LANG", "LC_ALL", "TERM", "TZ", "SYSTEMROOT", "PATHEXT")

def _subprocess_env(config: ServerConfig) -> dict[str, str]:
    env = {key: os.environ[key] for key in ENV_ALLOWLIST if key in os.environ}
    env.update(config.env)
    return env
```

先从一份白名单里挑出运行一个进程本来就需要的基础变量（`PATH` 保证能
找到 `python` 这样的可执行文件，`SYSTEMROOT`/`PATHEXT` 是 Windows
下同样必要的），再把配置文件里 `env` 字段写的内容**叠加**上去
（`.update()`）。**不会**把父进程的整个 `os.environ` 传过去——如果
父进程的环境变量里正好有一个 `OPENAI_API_KEY`，MCP server 不需要
它，也不应该能看到它。需要什么凭据，在配置文件里一条一条显式写清楚。

### A10.3 `__main__.py` 里改动的几处位置

```python
registry = McpRegistry(local=list(TOOL_SCHEMAS))       # ① 本地工具也交给 registry 统一管
clients = []
if mcp_config is not None:
    clients = await connect(
        load_config(mcp_config),
        registry,
        handlers={"elicitation/create": elicitation_handler(session)},   # ② 反向请求怎么答
    )
    for name, reason in registry.failures.items():
        print(f"[mcp: {name} unavailable -- {reason}]", file=sys.stderr)
    for client in clients:
        offered = sum(1 for r in registry.registrations.values() if r.client is client)
        print(f"[mcp: {client.config.name} connected, {offered} tool(s)]")
    if registry.deferred:
        print(f"[mcp: {len(registry.deferred)} tool(s) deferred behind tool_search]")

llm = ChatCompletionsModel(
    ...,
    tools=registry.visible,                             # ③ 同一个 list 对象，见 A6.1
)
...
handlers = {**default_tools(root=root, session=session), **registry.handlers()}   # ④ 本地 + 远端的处理函数合并成一张表
if registry.deferred:
    handlers["tool_search"] = registry.search_handler()

agent = Agent(
    llm, handlers, ...,
    footprint_of=route_footprint(registry, functools.partial(footprint_of, root=root)),  # ⑤
)

try:
    result = await agent.run(question)
finally:
    for client in clients:
        await client.close()                            # ⑥ 不管跑成功还是异常，都要收拾子进程
```

按标号过一遍：

- **①** `McpRegistry` 的构造函数把这个项目本来就有的四个本地工具
  （`local=list(TOOL_SCHEMAS)`）也接管过来，理由在 A6.1 说过：`visible`
  要么是模型能看到的**全部**工具，要么不是——两份列表在调用方手动拼接，
  是"漏掉其中一份"这类 bug 最常见的来源。
- **②** `handlers` 字典告诉 `McpClient`（间接地，通过 `connect()`
  传下去）"如果 server 反过来问我一个 `elicitation/create`，该怎么答"，
  接的正是 A8 写的那个函数。
- **③** `tools=registry.visible`——不是 `list(registry.visible)`，
  见 A6.1 的详细解释。
- **④** 本地工具的处理函数（`default_tools(...)`）和远端工具的处理
  函数（`registry.handlers()`，A7.2 那批）用 `{**a, **b}` 语法合并成
  一张表；`tool_search` 单独加进去，而且只在真的有工具被 defer 的时候
  才加——没有工具被藏起来，就不需要暴露一个搜不出东西的搜索工具。
- **⑤** 把 `route_footprint`（A9）接到第 8 章的调度器参数上。
- **⑥** `finally` 块里逐个关闭每个 client，不管 `agent.run(...)` 是
  正常返回还是抛了异常都会执行——这是第 2 章"别留下孤儿子进程"那笔账
  在 CLI 层面的收尾。

---

## A11 · 一份你现在就能跑起来的最小完整例子

把 A1 的 `toy_server.py` 和 A3 写的 `McpClient` 缩成一个**单文件、
不依赖 minicodex 项目其余任何模块**的例子，方便你直接复制到自己电脑上
跑一遍，亲眼看到真实的握手 + 列工具 + 调工具的完整往返。新建
`mcp_demo.py`：

```python
"""单文件可跑的最小 MCP demo：一个进程里同时定义 server 逻辑和 client 逻辑，
用两个子任务模拟"两个独立进程"，方便你不用建两个文件就能看到完整流程。
真正的 MCP 用法是 server 单独一个文件、单独一个进程，这里为了方便贴合本附录，
把两者放进了同一个脚本，用注释分隔开。"""

import asyncio
import json
import sys

SERVER_CODE = '''
import json, sys
def main():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        message = json.loads(line)
        if "id" not in message:
            continue
        method = message.get("method")
        if method == "initialize":
            result = {"protocolVersion": "2025-06-18", "capabilities": {}, "serverInfo": {"name": "demo"}}
        elif method == "tools/list":
            result = {"tools": [{"name": "echo", "description": "Echo text back.",
                       "inputSchema": {"type": "object", "required": ["text"],
                                        "properties": {"text": {"type": "string"}}}}]}
        elif method == "tools/call":
            params = message.get("params") or {}
            args = params.get("arguments") or {}
            result = {"content": [{"type": "text", "text": str(args.get("text", ""))}]}
        else:
            result = None
        reply = {"jsonrpc": "2.0", "id": message["id"]}
        reply["result"] = result if result is not None else {}
        sys.stdout.write(json.dumps(reply) + "\\n")
        sys.stdout.flush()
if __name__ == "__main__":
    main()
'''

async def main() -> None:
    # 把上面这段 server 代码写成一个临时文件，再当子进程启动它——
    # 这一步纯粹是为了让这份 demo 只有一个文件；正式项目里 server 就是独立的 .py 文件。
    import tempfile, pathlib
    server_path = pathlib.Path(tempfile.gettempdir()) / "mcp_demo_server.py"
    server_path.write_text(SERVER_CODE, encoding="utf-8")

    proc = await asyncio.create_subprocess_exec(
        sys.executable, str(server_path),
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
    )

    next_id = 0
    async def call(method: str, params: dict) -> dict:
        nonlocal next_id
        next_id += 1
        request = {"jsonrpc": "2.0", "id": next_id, "method": method, "params": params}
        proc.stdin.write((json.dumps(request) + "\n").encode("utf-8"))
        await proc.stdin.drain()
        line = await proc.stdout.readline()
        return json.loads(line)

    print("initialize ->", await call("initialize", {}))
    print("tools/list ->", await call("tools/list", {}))
    print("tools/call ->", await call("tools/call", {"name": "echo", "arguments": {"text": "hello mcp"}}))

    proc.stdin.close()
    await asyncio.wait_for(proc.wait(), timeout=2.0)

asyncio.run(main())
```

运行：

```
$ python mcp_demo.py
initialize -> {'jsonrpc': '2.0', 'id': 1, 'result': {'protocolVersion': '2025-06-18', ...}}
tools/list -> {'jsonrpc': '2.0', 'id': 2, 'result': {'tools': [{'name': 'echo', ...}]}}
tools/call -> {'jsonrpc': '2.0', 'id': 3, 'result': {'content': [{'type': 'text', 'text': 'hello mcp'}]}}
```

这份 demo 刻意用的是 A2 那个"笨版本"（没有后台读循环、没有 pending
表），目的只是让你先亲手看到协议往返本身。看懂之后，回头对着 A3 的
`McpClient` 完整版，你会更清楚每一段升级到底在解决什么问题——尤其是
建议你自己动手改一下这份 demo 的 server 代码，在每次回复前加一行
`sys.stdout.write(json.dumps({"jsonrpc": "2.0", "method": "notifications/message", "params": {}}) + "\n")`，
重新跑一遍，亲眼看着这个笨 client 从第二次调用开始就读错行——这正是
正文 §3 讲的那个故障，自己复现一遍比读十遍描述都管用。

---

## A12 · 新手常见报错对照表

| 报错/现象 | 通常原因 | 怎么排查 |
|---|---|---|
| client 卡住不动，既不报错也不返回 | server 那边 `stdout` 没有 `flush()`，或者 client 忘了 `await proc.stdin.drain()` | 先用 A1 结尾那条 `printf ... \| python xxx_server.py` 单独验证 server 能不能正常应答 |
| `ValueError: I/O operation on closed pipe`（出现在程序其它输出之后） | 子进程的管道没有显式 `close()`，靠垃圾回收器兜底 | 检查 `close()` 是否按 A3.8 的四步顺序执行，尤其是最后那段显式关 transport 的循环 |
| 一问一答完全对不上，后面每次都"错一格" | server 在两次响应之间插了一条通知，client 用 `readline()` 简单配对 | 检查读循环是不是按 `"id" in message and "method" not in message` 这条规则分流，而不是假设下一行就是答案 |
| `UnicodeDecodeError` 之类的异常直接把整个 server 打挂 | server 内部某个工具处理逻辑没有 try/except，异常直接冒出协议循环之外 | 按 A1、files_server.py 的写法，把 `tools/call` 的处理逻辑包一层 try/except，转成 `isError: true` 返回，而不是让异常终结进程 |
| server 死了，client 只会说"closed the connection"，看不出原因 | 子进程的 `stderr` 被设成了 `DEVNULL`，或者没有人读它 | 改成 `stderr=asyncio.subprocess.PIPE` 并起一个后台任务持续读（A3.7），把最后一行存下来拼进错误信息 |
| 任务管理器/进程列表里堆了一堆没退出的 `python.exe` | 某处异常路径下漏调了 `client.close()` | 确认所有会启动 client 的地方都有对应的 `finally: await client.close()`，参考 A10.3 第 ⑥ 步 |
| `tool_search` 显示"已加载"，但下一轮模型还是调不到那个工具 | `tools=` 传的是 `registry.visible` 的一份拷贝，而不是同一个对象 | 检查有没有类似 `list(registry.visible)` 的写法，参考 A6.1 |
| 一边遍历字典一边删键，抛 `RuntimeError: dictionary changed size during iteration` | 直接 `for k in some_dict: del some_dict[k]` | 先 `list(some_dict.items())` 复制一份再遍历，参考 A7.4 `forget()` 的写法 |
| 两个 server 的同名工具，模型总是只能用其中一个 | 用一个普通 `dict` 按工具名合并，后注册的覆盖了先注册的 | 参考 A4，给模型看的名字加上 server 前缀（`model_name`），调用时仍然用原始名字 |

对照这张表排查过一遍之后，回头把正文 §2～§15 再读一遍，会发现每一条
"为什么这样设计"背后，都对应着这份附录里的某一段"怎么写才不会翻车"。
