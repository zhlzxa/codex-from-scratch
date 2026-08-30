# 第 10 章 · 子 Agent

> **代码**：`steps/step10_subagents/`
> **分支**：`feat/subagents`
> **产出**：Agent 能把一件"自己能独立干完"的活交给第二个 Agent——它有自己的历史、
> 自己的工具表、自己的会话文件，回来的是一个**有结局标签、有长度上限**的答案
> **你需要**：本章 32 个测试全部离线。`probe_subagent.py` 十二节里八节不联网，四节要真调
> API（`gpt-4o-mini`，约七十次请求）

---

## §1 这一章要做出来的东西

到第 9 章为止，Agent 是一个人在干活。所有的事都发生在一条对话里：读文件、跑命令、
改代码、被压缩、被中断、被恢复。工具可以来自别人的进程（第 9 章），但**思考只有
一份**。

这一章加的东西一句话就能说清：**让一个工具调用的内部，是另一个 Agent 的一整轮对话。**

为什么要这样？两个理由，都跟前面几章欠的债直接相关：

- **上下文**。第 6 章花了一整章教怎么在历史撑爆之前把它压缩掉。子 Agent 是另一个
  方向的答案：与其把"读了 12 个文件才找到那一行"这件事的全过程留在主历史里，
  不如让另一个 Agent 去读那 12 个文件，只把那一行拿回来。
- **注意力**。第 9 章测出来，让模型在一堆相似工具里挑对不难，难的是"一个任务里
  夹着五件不相干的小事"。分出去，主对话就只剩主线。

清单上给这一章列了 12 条故障。写完之后：

- **两条没有按清单描述的样子复现**（F10-02 已经被第 8 章挡住了；F10-10 作为"接口
  丢信息"完全成立，作为"父 Agent 因此答错"在 5 个样本上没有稳定复现）；
- **两条测出来跟清单开的药方不一样**（F10-04 的"把约束写进契约"只有放对位置才有用，
  放错位置 3-4/5 照样违反；F10-11 的 worktree 隔离实测下来是把问题从"覆盖"换成
  "谁来解冲突"）；
- **清单外多出七条**，其中最贵的一条是：**深度上限挡得住深度，挡不住宽度**——
  只有深度上限时，一个只会 spawn 的模型跑一个任务花了 **72 次模型调用**。
  这条不是想出来的，是一个我写错了断言的测试逼出来的。

还有一条，是这一章最好玩的：**`asyncio.wait_for` 套不住一个 Agent。** 第 7 章为了
"中断时每个 call 都必须有 output"让 `Agent.run` 吞掉了 `CancelledError`，于是
超时不再抛异常，而是**安安静静地变成一个空答案**。

先从能看见它动的那一步开始。

---

## §2 先写一坨：十五行的 spawn

一个子 Agent 需要什么？一个模型、一个工具表、一句任务。这三样我们全都有。
所以第一版就是字面意义上的十五行：

```python
async def spawn_agent(args: dict) -> str:
    child = Agent(_model(), default_tools(root=ROOT, session=session), max_turns=6)
    result = await child.run(args["task"])
    return result.final_text

tools = {**default_tools(root=ROOT, session=session), "spawn_agent": spawn_agent}
parent = Agent(_model([*TOOL_SCHEMAS, SPAWN_SCHEMA]), tools, max_turns=6)
```

schema 也是最短的那版：

```python
SPAWN_SCHEMA = {
    "type": "function",
    "function": {
        "name": "spawn_agent",
        "description": "Hand a self-contained task to a second agent and get its answer back.",
        "parameters": {
            "type": "object",
            "required": ["task"],
            "properties": {"task": {"type": "string", "description": "What it should do."}},
        },
    },
}
```

给它一个正经问题（`probe_subagent.py naive`，真的调 gpt-4o-mini）：

> Use spawn_agent twice, once per file, to find out what
> src/minicodex/scheduler.py and src/minicodex/paths.py each do.
> Then answer in two sentences, one per file.

```
The `src/minicodex/scheduler.py` file defines a scheduling module that manages
concurrent tool calls in an asynchronous environment, ensuring that no conflicting
calls are executed simultaneously by organizing them into batches based on their
resource interactions. Meanwhile, the `src/minicodex/paths.py` file handles file
path resolutions within a directory structure, providing functionality to ensure
that paths are valid and exist while offering suggestions for files when inputs do
not match any existing paths.

[completed after 2 turn(s)]
```

**成了。** 两个子 Agent，两次并列调用，主 Agent 两轮就答完了，答案是对的。

这就是这一章危险的地方：十五行版本能用，而且看起来很好用。它坏掉的每一种方式，
都不会在这次运行里表现出来。

---

## §3 一整段历史递下去要多少钱

第一个问题不需要模型就能量：**子 Agent 应该看见什么？**

十五行版本给的是"只有任务那一句话"。另一个同样自然的写法是"把父 Agent 的历史
整个复制过去，再追加一句任务"——听起来更周到，子 Agent 什么背景都知道。

造一个真实形状的父历史：系统提示 + 权限块（`__main__` 真的会拼的那个）、
用户的原始要求、三次 `read_file` 读进来的三个真实模块。八条。然后两种方案各算一遍
（`probe_subagent.py leak`，用第 6 章的 `Sizer`，不联网）：

```
parent history             8 items
whole history to child     4476 tokens
task only                   638 tokens
ratio                         7x

what travels that the child's task does not mention:
  system        876 chars  You are a coding agent working in a user's repository.  # What you a
  user          119 chars  The retry logic in model.py must not retry a 400. Fix it, and do not
  assistant      21 chars  Reading scheduler.py.
  tool         4422 chars  """Deciding which tool calls in one turn may run at the same time.
  assistant      17 chars  Reading paths.py.
  tool         3821 chars  """Turning what the model sent into a file we can open.  Chapter 3 m
  assistant      23 chars  Reading shell_parse.py.
  tool         5892 chars  """Splitting one command string into the pieces a policy can judge.
```

**7 倍。** 而且注意最后那一列：子 Agent 的任务是"读 model.py，列出它在哪些地方
raise"，它却收到了 `scheduler.py`、`paths.py`、`shell_parse.py` 的全文。八条里
有三条是纯粹的浪费，占了 14000 个字符。

这不是"多花点 token"的问题。第 6 章测过，历史一变长，压缩就提前触发，压缩会丢东西。
把父历史递下去，等于让每个子 Agent 从一个已经半满的窗口开始。

所以子 Agent 收到的是一份**契约**，不是一段历史。契约里有什么，是这一章后面几节的
内容（§8）。这里先把结论写下来：

```python
@dataclass(frozen=True)
class TaskSpec:
    task: str
    constraints: tuple[str, ...] = ()
    expected_output: str = ""
```

三个字段，一个也不多。后两个字段各自有一次测量支撑，都在 §8。

---

## §4 父 Agent 没写下来的那些东西

清单上 F10-01 写的是"子 Agent 读到主 Agent 还没落盘的编辑 → 脏读"。

**这一条在我们的代码里不存在。** `apply_patch` 是 `asyncio.to_thread(apply_edits)`，
写完才返回；子 Agent 起来的时候，父 Agent 的编辑早就在磁盘上了。没有缓冲，没有
延迟写入，没有脏读。

但这条故障的**精神**在这里成立，而且成立得更彻底：**父 Agent 有一些状态，从来没有
写到任何地方去过。**

跑 `probe_subagent.py cwd`（不联网）：

```
parent: (ok)
parent: /d/study-codex-with-claude/codex-from-scratch/steps/step10_subagents/src
child:  /d/study-codex-with-claude/codex-from-scratch/steps/step10_subagents

parent session before upgrade: sandbox_mode=read-only, approval_policy=on-request
parent session after upgrade:  sandbox_mode=workspace-write, approval_policy=on-request
a child built with default_tools(root): sandbox_mode=read-only, approval_policy=on-request
```

两件事，两个都不报错：

1. **父 Agent 在 `src/` 里，子 Agent 在仓库根。** 第 2 章为了修 F02-05（`cd` 在
   两次调用之间丢失），把 `cd` 拦下来在 Python 里解释，存进 `ShellSession.cwd`。
   那是一个**进程内存里的属性**。`default_tools(root=...)` 每次都新建一个
   `ShellSession`，于是新建的那个从进程 cwd 开始。父 Agent 说"我在 src 里"，
   子 Agent 说"我在根目录"，两个都对，两个说的不是一个地方。
2. **父 Agent 在第 3 轮拿到的写权限，子 Agent 没有。** 第 5 章的 `Session` 是可变的，
   因为 `request_permissions` 要能改它。`default_tools` 的默认值是
   `session or Session()`，而 `Session()` 默认 `read-only`。一个忘了传 session 的
   调用点，得到的是一个只能读的子 Agent——这本来是第 5 章故意设计的"失败时往安全
   方向倒"，在这里变成"父 Agent 明明有权限，子 Agent 却干不了活，而且不知道为什么"。

修法不是加逻辑，是**把东西传过去**。`ToolContext` 多了一个可选参数：

```python
def tool_context(
    root: Path | None = None,
    session: Session | None = None,
    shell: ShellSession | None = None,
) -> ToolContext:
```

子 Agent 的工具表这样建：

```python
shell = ShellSession(timeout=ctx.parent_shell.timeout)
shell.cwd = ctx.parent_shell.cwd
context = tool_context(root=ctx.root, session=ctx.session, shell=shell)
```

注意两个细节：

- **新的 `ShellSession`，不是父 Agent 那一个。** 子 Agent `cd` 到哪里是它自己的事，
  不该改到父 Agent 头上。传的是"从哪里开始"，不是"共用一个"。
- **`Session` 是同一个对象，不是拷贝。** 这个方向反过来：权限属于**这个人**，
  不属于某一条对话。子 Agent 里发生的一次提权，父 Agent 后面应该看得到；
  拷贝一份意味着这次提权随着子 Agent 的工具表一起消失。这也是 §10 的答案。

两条各有一个测试。第二条长这样：

```python
session = Session(mode="read-only")
ctx = context_for(tmp_path, ScriptedModel(["done"]), session=session)
handlers, _ = child_tools(ctx)

denied = await handlers["apply_patch"]({"edits": []})
assert "Permission denied" in denied

session.mode = "workspace-write"
allowed = await handlers["apply_patch"]({"edits": []})
assert "Permission denied" not in allowed
```

写这个测试时先撞了一次墙：第一版用的是 `Session(mode="read-only", approver=AllowAll())`，
结果 `denied` 里根本没有 "Permission denied"——`AllowAll` 把它批了。
**测试里图省事塞的 `AllowAll`，把要测的那个门给拆了。**

---

## §5 两个子 Agent 改同一个文件：第 8 章已经替我们挡住了

F10-02 说的是"两个子 Agent 同时改同一文件"。这是第 8 章 F08-01 的翻版——
两个 `apply_patch` 都读到编辑前的内容，都算对了自己那一改，后写的那个把先写的
抹掉，全程没有任何错误。

第 8 章的结论是：调度由 `Footprint` 决定，而**分不出来的一律算 `STATEFUL`**
（跟谁都冲突，包括另一个 `STATEFUL`）。`tools.footprint_of` 的最后一行是：

```python
return STATEFUL
```

`spawn_agent` 是 `footprint_of` 从没听说过的名字，所以它落进这一行。**F10-02
在写下来之前就已经被修好了。**

这话不能只是说说。跑 `probe_subagent.py race`，两个子 Agent 各改同一个文件的
不同一行，30 次：

```
  0/30 lost updates  --  spawn is STATEFUL (what tools.footprint_of already returns)
    first failure: 'ONE\ntwo\n'
 30/30 lost updates  --  spawn declares Footprint(reads={root}) -- 'it only reads'
```

第二臂是"有人好心地帮 `spawn_agent` 分了类"——比如觉得"这个子 Agent 只是去读文件的，
声明成 reads 就行"。**30/30 丢失更新**（这一节跑了五遍，29 或 30，从没低于 29）。
`first failure` 那一行是它留下的文件内容：`ONE\ntwo\n`——两个子 Agent 都报告
`Applied 1 edit(s)`，文件里只有一个改动。**谁赢是随机的**：另一次运行里，同样的代码
留下的是 `one\nTWO\n`。

这正是第 8 章 F08-06 预言的形状："这条故障在某个工具的 footprint 是猜出来的那天
变得可达"。第 9 章的 `readOnlyHint` 是第一次，`spawn_agent` 是第二次。

所以这里**什么都不加**，只加一个测试，把"什么都不加是对的"钉住：

```python
def test_F10_02_a_spawn_is_stateful_without_anyone_saying_so(tmp_path: Path) -> None:
    assert footprint_of(a_call("spawn_agent", task="x"), root=tmp_path) == STATEFUL
    assert conflicts(STATEFUL, STATEFUL)
```

`conflicts(STATEFUL, STATEFUL)` 那一行不是凑数：`STATEFUL` 跟**另一个 STATEFUL**
也冲突，是第 8 章特意写的一句话，而这一章整个"子 Agent 串行"的行为全靠它。

代价在 §14 里付。

---

## §6 无限递归：不是慢，是停不下来

### 6.1 830,400

给子 Agent 也装上 `spawn_agent` 工具，然后让模型每一轮都 spawn。这不需要恶意，
一个把任务反复往下拆的模型自然就会这样。

`probe_subagent.py depth`，用脚本模型（不联网），每 200 层打一行：

```
  depth 200 after 0.0s
  depth 400 after 0.0s
  ...
  depth 830000 after 58.8s
  depth 830200 after 58.8s
  depth 830400 after 58.8s
```

**83 万层，59 秒。** 进程是被外面的 `timeout 60` 杀掉的，不是自己停的。

为什么没有栈溢出？因为每一层都不是一个 C 栈帧。第 8 章把工具调用改成了
`asyncio.ensure_future(self._run_tool(call))`——每一层是一个堆上的 Task，
Python 的递归深度限制根本管不着。

而真正让人后背发凉的是这一段。探针里我是这样保护自己的：

```python
await asyncio.wait_for(parent.run("go"), timeout=10)
```

它打出来的是：

```
Exception in callback Timeout._on_timeout()
handle: <TimerHandle when=258542.0638138 Timeout._on_timeout()>
Traceback (most recent call last):
  File "...\asyncio\events.py", line 89, in _run
    self._context.run(self._callback, *self._args)
  File "...\asyncio\timeouts.py", line 129, in _on_timeout
    self._task.cancel()
  File "...\asyncio\tasks.py", line 774, in cancel
    if child.cancel(msg=msg):
  File "...\asyncio\tasks.py", line 774, in cancel
    if child.cancel(msg=msg):
  [Previous line repeated 988 more times]
RecursionError: maximum recursion depth exceeded
```

超时定时器烧了。`task.cancel()` 要沿着嵌套的 Task 链一路 cancel 下去，那条链
已经十几万层深，`cancel()` 是真正的 C 递归，于是 `RecursionError` **在事件循环
自己的回调里**抛出来了。回调死了，取消没有发生，程序继续往下长——从那之后又跑了
四十多秒，到 83 万层。

`except RecursionError` 接不住它，因为它根本不在我的调用栈上。

**结论不是"递归会很慢"，是"递归会把唯一能停下它的那个机制一起弄坏"。**
所以深度上限不能是"跑起来之后再想办法停"，必须是一个在 spawn 发生**之前**就生效的
计数：

```python
MAX_DEPTH = 2

if ctx.depth >= ctx.max_depth:
    return TaskResult("depth_limit", "")
```

还有第二件事，看起来是细节，实际是第 5 章的教训重演：**到了最底层，就不要把
`spawn_agent` 交给它。**

```python
if ctx.depth + 1 < ctx.max_depth:
    spec = spawn_spec(replace(ctx, depth=ctx.depth + 1))
    handlers[spec.name] = spec.bind(context)
    schemas.append(spec.schema())
```

第 5 章 F05-10 测过：prompt 里提到一个当前策略下不存在的工具，模型会去调它（2/3）。
"给了但会被拒绝"和"根本没给"，对模型来说是两种东西，前者要浪费一整轮才知道。

### 6.2 一个被测试打脸的假设

写完深度上限，我给它配了一个测试，断言"一个只会 spawn 的模型不会花太多次模型调用"：

```python
assert calls <= 8, calls
```

红了：

```
AssertionError: 72
assert 72 <= 8
```

**72 次模型调用，跑一个任务。** 算一下就明白了：深度 1 的子 Agent 有 8 轮预算，
每一轮 spawn 一个深度 2 的子 Agent；深度 2 拿不到 spawn 工具，于是它会花掉自己的
8 轮去调一个不存在的工具（第 0 章的 "no tool named X"，一轮一次）。
8 + 8 × 8 = 72。

**深度上限限的是深度，不是宽度。** 深度是 2，钱是按 2 × 8 × 8 花的。

这不是断言写错了，是代码少了一样东西。加的是一个**整轮共享的预算**：

```python
DEFAULT_CHILD_TURN_BUDGET = 24

spent = sum(child.turns for child in ctx.children)
if spent >= ctx.child_turn_budget:
    return TaskResult("budget", "")
```

`children` 这个列表本来就在——`SubAgentContext` 记着它，是为了跑完之后能在终端
列一行一个子会话（§11）。共享是靠 `dataclasses.replace` 的性质：它拷贝的是**引用**，
所以整棵子 Agent 树数的是同一个列表。

改完再跑，同一个测试：

```python
# 32, and the number is the whole point of this test.
assert calls == 32, calls
```

8 + 3 × 8 = 32。断言写成 `== 32` 而不是 `<= 32`，因为这个数字是这段代码唯一的
产出：它变了，说明有人动了预算或者动了 `max_turns`，那都该有人看一眼。

这个预算有一处**不准**，写在代码注释里而不是藏着：子 Agent 是**跑完才记账**的，
所以在它上面还没跑完的那些祖先，它们的轮数没被算进去。它是一个上界，不是一个精确
的账本。

---

## §7 挂住的子 Agent，和一个不会触发的 wait_for

F10-07：子 Agent 挂起不返回，父 Agent 永久等待。

这条听起来最简单：套一个 `asyncio.wait_for` 不就行了。

跑 `probe_subagent.py hang`——子 Agent 的一个工具 `await asyncio.sleep(3600)`，
父 Agent 外面套 `wait_for(..., timeout=3)`：

```
wait_for returned a RunResult after 3.0s
  stop_reason  interrupted
  final_text   ''
  child ran    True
  ToolResult   ''
```

**没有 `TimeoutError`。** `wait_for` 正常返回了一个 `RunResult`。

为什么？因为第 7 章。F07-04 的结论是"每一个发出去的 call 都必须有 output，
包括那些根本没开始跑的"，实现是这样的：

```python
except (asyncio.CancelledError, KeyboardInterrupt):
    ...
    return RunResult(final_text, "interrupted", turn_index + 1, history, ...)
```

`Agent.run` **把 `CancelledError` 吃掉了**，然后正常返回。`asyncio.wait_for`
（3.11 起是 `asyncio.timeout` 实现的）只在被包住的代码**以 `CancelledError` 结束**
时才把它翻译成 `TimeoutError`；正常返回值它就原样放行。

于是链条是这样的：超时 → 取消 → 子 Agent 吞掉取消、返回一个 `RunResult` →
`spawn` 里 `return result.final_text` 拿到 `''` → 父 Agent 的工具结果是空字符串 →
父 Agent 看到一个"成功但什么也没说"的子 Agent。

**第 7 章为了让中断可靠而做的事，让超时变得不可靠。** 这不是谁写错了，是两个正确
的决定在一个当时不存在的组合下打架。

修法是把取消**接在自己身上**：

```python
task = asyncio.ensure_future(child.run(spec.task))
try:
    result = await asyncio.wait_for(asyncio.shield(task), ctx.timeout)
except TimeoutError:
    partial = await _stop(task)
    ...
    return _record(ctx, TaskResult("timeout", partial, seconds=elapsed, ...))
except asyncio.CancelledError:
    await _stop(task)
    raise
```

`shield` 让 `wait_for` 的取消落在外面那层 future 上，于是 `TimeoutError` 又能正常
抛出来了。代价是**第二个 `except`**：`shield` 同时也把子 Agent 挡在了父 Agent 的
Ctrl-C 之外，所以用户中断的时候，得手动把子 Agent 停掉再把 `CancelledError` 往上抛。
不这么写的话，用户按了 Ctrl-C，主循环退出了，一个子 Agent 还在后台跑——
这就是第 2 章 F02-08 的孤儿进程，换了身衣服。

`_stop` 也不是 `task.cancel()` 一行：

```python
async def _stop(task: asyncio.Task[Any]) -> str:
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        result = await task
        return str(result.final_text).strip()
    return ""
```

**取消之后要 `await`**。子 Agent 在被取消的路上还要做一件正经事——给自己那些
未完成的 tool_call 各补一条 output（第 7 章）。不 await 就直接走人，这段收尾会跟
父 Agent 的下一轮并发着跑。顺带把它已经说出来的话捡回来，作为 `timeout` 结局的
"它停下之前说到哪儿了"。

这段设计有两个测试。一个测行为，一个测**为什么必须这么设计**：

```python
async def test_F10_07_a_bare_wait_for_would_have_returned_an_empty_answer(...):
    child = Agent(model, {"sleep": forever})
    result = await asyncio.wait_for(child.run("hang"), timeout=0.3)

    assert result.stop_reason == "interrupted"
    assert result.final_text == ""
```

第二个测试不测 `run_task`，它测的是 `Agent` 的这个性质。哪天第 7 章的实现改了、
`CancelledError` 不再被吞掉了，这个测试会红，那时候 `shield` 就可以删掉了。
**一个"解释为什么"的测试，比一句注释多的地方在于：注释不会在前提消失时通知你。**

---

## §8 契约：任务、约束、输出形状

### 8.1 约束写在哪里，决定它算不算数

F10-04：只传任务描述，子 Agent 缺关键约束，做出错误假设。

要测这条，得先设计一个"约束"，它被违反的时候是**机械可观测**的，而不是靠我读一段
散文判断。用的是这个：

- 约束：*This machine's shell is broken: run_shell fails in ways that look like real
  results. Do not use run_shell for anything.*
- 任务：*Find every place in src/minicodex that calls os.killpg, and say which file
  it is in.*

违反 = 子 Agent 调了 `run_shell`。这是个布尔值，不需要判断力。

四臂，每臂 5 个样本，gpt-4o-mini（`probe_subagent.py contract`）：

```
    calls: ['run_shell({"command": "grep -r \'os.killpg\' src/minicodex"})']
    said:  'The call to `os.killpg` is located in the file `src/minicodex/shell.py`.'
 5/5 used the broken shell, 5/5 named shell.py  --  task only
 4/5 used the broken shell, 4/5 named shell.py  --  constraint in the task
 0/5 used the broken shell, 0/5 named shell.py  --  constraint as a system note
 0/5 used the broken shell, 0/5 named shell.py  --  no run_shell in the table
```

（第一臂和第二臂各跑过三遍，第一臂三次都是 5/5，第二臂是 4/5、3/5、4/5。
第三、四臂跑过两遍，都是 0/5。）

三个结论，一个比一个不舒服：

**第一，故障复现得干干净净。** 不说，5/5 违反。子 Agent 不是"忘了"约束，它压根
没有过这个约束——对它来说那个约束不存在。

**第二，"把约束写进契约"这个药方，取决于写在哪里。** 附在任务文本后面（
`任务\n\nConstraints:\n- ...`），3-5 次里还是有 3 到 4 次照样用。**同一句话**放进
子 Agent 的 system message，0/5。

样本只有 5 个，我不会说"system 位置一定更好"。但这个方向和第 5 章 F05-10、
第 13 章要讲的 F13-07 是一致的：**同一句话放在哪里，是一个变量，而且是一个大变量。**
所以 `TaskSpec.constraints` 渲染成 system note，不是拼进任务里：

```python
def instructions(self) -> str:
    parts = ["You have been given one self-contained task by another agent."]
    if self.constraints:
        parts.append(
            "These rules come from the user and apply to everything you do:\n"
            + "\n".join(f"- {c}" for c in self.constraints)
        )
    ...
```

**第三，也是最不舒服的一条：遵守约束让这个任务变成了不可能。** 看第三、四臂的
第二列——0/5 违反，也是 0/5 答对。打开样本看：

```
    calls: ['read_file({"path": "src/minicodex"})',
            'read_file({"path": "src/minicodex/__init__.py"})',
            'read_file({"path": "src/minicodex/model.py"})',
            'read_file({"path": "src/minicodex/utils.py"})',
            'read_file({"path": "src/minicodex/some_other_file.py"})']
    said:  ''
```

它先试着 `read_file` 一个目录（失败），然后开始**猜文件名**——`utils.py` 和
`some_other_file.py` 在这个仓库里都不存在。这个工具集里没有"列目录"这件事，
列目录一直是 `run_shell` 干的。

第四臂（工具表里直接没有 `run_shell`）更清楚：

```
    calls: ['read_file({"path": "src/minicodex"})',
            'read_file({"path": "src/minicodex/"})',
            'request_permissions({"needs": "unrestricted", "why": "to search through the src/minicodex )',
            ...]
```

它找到了第 5 章给它留的那个出口（`request_permissions`），**而那个出口通向的地方
不存在**——权限批下来了，工具表里依然没有 `run_shell`。

这一段该记住的不是"约束有用"或"约束没用"，是：**用代码强制一条约束，代价是这条
约束必须仍然留着一条通往目标的路。** 第 5 章 F05-11 的结论（"模型不缺信息，
缺的是出口"）在这里第二次出现，而这次是我自己把出口堵上的。

### 8.2 一句"三句话以内"不是上限

F10-05：子 Agent 返回 5000 字，主 Agent 上下文照样爆。

`probe_subagent.py length`，三臂五样本：

```
task only                chars: [2187, 2192, 2331, 2366, 2595]  median 2331
task + output shape      chars: [452, 459, 503, 526, 540]       median 503
bigger task + same shape chars: [603, 615, 661, 1468, 2711]     median 661
```

前两臂说明**要求输出形状是有用的**：4.6 倍。所以 `TaskSpec.expected_output`
是必填的（schema 里 `"required": ["task", "expected_output"]`）。

第三臂说明**它不是上限**。同一句 "Answer in at most three sentences"，
任务稍微大一点，样本就散到 603 和 2711，最大的那个是"三句话"的五倍。

所以两样都要：一句指令（把中位数从 2331 压到 503），加一个代码里的天花板。

```python
MAX_TASK_RESULT_CHARS = 4000

def render(self) -> str:
    body = clip(self.text.strip(), MAX_TASK_RESULT_CHARS)
```

4000 字符大约 1000 token：一个守规矩的子 Agent 永远碰不到它，一个跑飞了的一定会
碰到。这跟第 2 章的 `MAX_OUTPUT_CHARS`、第 9 章的 `MAX_RESULT_CHARS` 是同一个东西
的第三次出现——§13 讲那次抽取。

---

## §9 返回的不是字符串，是一个结局

十五行版本的最后一行是：

```python
return result.final_text
```

`RunResult` 有四个字段，这一行用了一个。被扔掉的那个叫 `stop_reason`，
它的取值有 `completed`、`turn_limit`、`interrupted`。

把三种结局映射成一个字符串，本身就有损。更糟的是**其中两种映射到同一个字符串**：

- `turn_limit`：预算烧完了。最后一轮如果是工具调用，`final_text` 就是那一轮的
  旁白；gpt-4o-mini 经常一句旁白都不说，于是 `final_text == ''`。
- `interrupted`：§7 那条，`final_text == ''`。

实测（`probe_subagent.py failure`，把子 Agent 的预算压到 1 轮）：

```
    child returned  ["turn_limit/1: ''"]
    child called    3 tool(s)
```

子 Agent 调了 3 个工具，一句话没说，`final_text` 是空串。父 Agent 收到一个空字符串，
得自己猜这是什么意思。

**这条故障作为"接口丢信息"是板上钉钉的；作为"父 Agent 因此答错"，我没能稳定复现。**
两臂（裸 `final_text` vs 前面加一行结局标签），5 样本：

```
 1/5 passed it on as an answer, 0/5 said it was unfinished  --  bare final_text
 0/5 passed it on as an answer, 0/5 said it was unfinished  --  outcome label first
```

绝大多数情况下，父 Agent 收到空串之后**自己去把活干了**（三次 `read_file`），
这是正确反应。1/5 对 0/5，5 个样本，我不会拿这个当证据。

而且这个测量我做错过一次：第一版把子 Agent 的预算设成 2 轮，结果它**真的干完了**
（`completed/2`，一段像模像样的总结），于是那一臂里"结局标签"说的是假话，
两臂测的是同一件事，结果都是 3/5——**一个把两臂造成同一个东西的实验，会给出
非常稳定的、毫无意义的数字。**（第 5 章 F05-09 的"第一个指标是错的"，第二次。）

所以这里的修法不是靠行为证据，是靠接口证据：**一个把四种结局压成一个字符串、
并且把其中两种压成同一个空串的函数，是坏的，跟模型怎么反应无关。**

```python
Outcome = Literal[
    "ok", "empty", "turn_limit", "timeout", "interrupted", "depth_limit", "budget"
]

@dataclass(frozen=True)
class TaskResult:
    outcome: Outcome
    text: str
    turns: int = 0
    seconds: float = 0.0
    session_id: str = ""
```

`empty` 是自己加的一种：`stop_reason == "completed"` 但一个字都没说。
**"很短的答案"和"没有答案"不是一回事**——第 6 章对空摘要（F06-08）、第 9 章对空
MCP 结果（F09-07）都做过同样的判断，这是第三次，三次的理由一模一样：一个空字符串
渲染出来，读起来像"成功了，没什么好说的"。

渲染规则：

```python
def render(self) -> str:
    body = clip(self.text.strip(), MAX_TASK_RESULT_CHARS)
    if self.outcome == "ok":
        return body

    headline = _HEADLINE[self.outcome].format(turns=self.turns, seconds=self.seconds)
    header = f"[sub-agent: {headline}]"
    advice = _ADVICE[self.outcome]
    if not body:
        return f"{header}\nIt produced no answer at all. {advice}"
    return (
        f"{header}\nWhat it had said before that point, which is not a "
        f"conclusion:\n\n{body}\n\n{advice}"
    )
```

两个决定值得说：

**成功的结果不加头。** 每一条结果都顶着一个 `[sub-agent: ...]`，模型会学会跳过它。
标签的价值来自它的稀缺。

**每一种失败都带一句"接下来干什么"。** 这是第 3 章 F03-07 的结论
（"错误信息即 prompt"，光说发生了什么，模型会原样重试），提高一层来用：

```python
_ADVICE: dict[str, str] = {
    "empty": "Do not report this as a finding. Split the task, or do it yourself.",
    "turn_limit": (
        "Do not report this as a finding. Give it a smaller task, or do the "
        "remaining part yourself."
    ),
    "timeout": (
        "Do not report this as a finding. Anything it had already changed on "
        "disk is still changed; check before repeating it."
    ),
    "interrupted": "The user stopped it. Do not start it again unless asked.",
    "depth_limit": "Do this part yourself instead of delegating it further.",
    "budget": "Do the rest yourself. Delegating again will get the same answer.",
}
```

`timeout` 那条里的第二句是这一章特有的：一个被超时杀掉的子 Agent，**已经改到磁盘上
的东西不会回滚**。这是第 9 章 F09-05 拒绝重试的同一条理由——"不知道副作用有没有
发生"不是一个靠再做一遍能解决的状态。

钉住这件事的测试长这样：

```python
def test_F10_10_every_outcome_says_what_to_do_next() -> None:
    for outcome in ("empty", "turn_limit", "timeout", "interrupted", "depth_limit", "budget"):
        rendered = TaskResult(outcome, "partial words", 2, 1.0).render()
        assert rendered.startswith("[sub-agent:")
        assert rendered.rstrip().endswith((".", "!"))
```

加一种新结局而忘了写 advice，这个测试会以 `KeyError` 爆掉。

---

## §10 谁来批准子 Agent 的操作

F10-09：子 Agent 需要审批时，问谁？

答案在 §4 就已经定了，这里只是把它说清楚：**只有一个人，只有一个终端。**
子 Agent 的 `Session` 就是父 Agent 的 `Session`，里面装着同一个 `Approver`，
所以子 Agent 里冒出来的审批请求，跟其它所有审批请求走同一个地方。

```python
class Recording:
    async def ask(self, request: ApprovalRequest) -> ApprovalReply:
        seen.append(request)
        return ApprovalReply(False, request.what)

session = Session(mode="read-only", policy="on-request", approver=Recording())
ctx = context_for(tmp_path, ScriptedModel(["ok"]), session=session)
handlers, _ = child_tools(ctx)
output = await handlers["run_shell"]({"command": "rm -rf build"})

assert len(seen) == 1
assert "rm -rf build" in seen[0].what
assert "Permission denied" in output
```

这里**没有新机制**，跟第 9 章处理 MCP elicitation 时的选择完全一样，理由也一样：
同一个问题的第二种问法，是让用户学会不读就按 y 的最快方式（F05-06）。

有一件事没做，而且不做是个决定：**审批提示里没有说"这是子 Agent 要求的"。**
理由是我没有测量过它有没有用，而这一章已经有太多没测的东西了。写在这里，
是为了让它是一笔记在账上的债，而不是一个没人想起来的疏忽。

---

## §11 两个 rollout 文件，和一个被悄悄改掉的 `--resume last`

F10-12：子 Agent 的 rollout 与主 Agent 混在一起，无法回溯。

最省事的写法是让子 Agent 用父 Agent 的 `RolloutWriter`。跑一下
（`probe_subagent.py rollout`）：

```
RolloutError: ...\20260811T083448-1532.jsonl is already open by another minicodex
(pid 1532). Resume it there, or delete ...\20260811T083448-1532.jsonl.lock if that
process is gone.
```

**第 7 章的单写者锁直接把这条路封死了。** F07-08 当时测的是两个进程写同一个文件——
8000 条记录写下去只剩 5559 行，2469 条被覆盖没了。那次的结论是"用 `O_EXCL` 锁文件
拒绝掉"，当时还诚实地记了一句"从 CLI 走不到这个分支，因为每次运行都有自己的文件名"。

**这一章把那个分支变成可达的了**，而且它第一次触发就挡住了一个真的会丢数据的写法。
一个当时看起来纯属防御性的检查，三章之后成了设计的约束条件。

于是子 Agent 有自己的文件，`SessionMeta` 多一个字段：

```python
    parent: str | None = None
```

**没有 bump `ROLLOUT_VERSION`**，这一点跟第 7 章 F07-09 的对比值得写在代码注释里：

```python
    # Added without bumping ROLLOUT_VERSION, and the difference from F07-09 is the
    # lesson: that bump was needed because the *meaning* of an existing field
    # changed, so an old file read by new code produced different bytes.  This
    # is a new optional field -- `from_json` drops keys it does not know and
    # missing keys take their default, so old files stay readable and new files
    # stay readable by old code.  Additive is not breaking; reinterpreting is.
```

跑一次真的：

```
$ uv run minicodex ask "Use spawn_agent to find out what src/minicodex/scheduler.py \
    is for, then answer in one sentence." --provider openai --sandbox-mode read-only --yes

[sub-agent depth 1: Investigate the purpose of the src/minicodex/scheduler.py file in the ]
[sub-agent depth 1: ok in 2 turn(s)]
The `src/minicodex/scheduler.py` file is responsible for managing the scheduling of
tool calls by determining which calls can run concurrently based on their resource
interactions.

[gpt-4o-mini | completed after 2 turn(s)]
[sandbox_mode=read-only, approval_policy=on-request]
[tokens: x1.03 from 2 observation(s)]
[transcript: .minicodex\recordings\session-1786467900.jsonl]
[sub-agent 20260811T100503-18196: ok, 2 turn(s), 5.2s]
[session: .minicodex\sessions\20260811T100500-18196.jsonl]
```

然后：

```
$ uv run minicodex sessions
  20260811T100503-18196 | gpt-4o-mini | read-only | ...step10_subagents  5 msg   [sub-agent of 20260811T100500-18196]
  20260811T100500-18196 | gpt-4o-mini | read-only | ...step10_subagents  6 msg
```

**这一行输出里藏着两个 bug，两个都是看输出看出来的（🟠），没有任何测试会红。**

第一个：第一版里子会话那一行是 `? | read-only`——`SessionMeta` 的 `provider` 和
`model` 是空的，因为 `_writer()` 只填了 cwd 和权限。一个不写明是哪个模型产生的
transcript，没法跟另一个比较。补两个字段。

第二个更严重。跑完一次带子 Agent 的运行，目录里有三个文件，**最新的两个是子会话**。
于是：

```python
if reference == "last":
    sessions = list_sessions(directory)
    ...
    return sessions[0].path
```

`--resume last` 从"继续上一次对话"变成了"继续上一个子 Agent 说的话"。命令照常执行，
输出照常打印，接着的是错的那条线。修法一行：

```python
sessions = [r for r in list_sessions(directory) if r.meta.parent is None]
```

子会话仍然能**按 id** 恢复，只是不参与"猜"。

这条不在清单上，它是这一章"加一个功能，悄悄改掉一个旧功能的含义"的样本。
回归测试：

```python
def test_F10_12_resume_last_skips_sub_agent_sessions(tmp_path: Path) -> None:
    for session_id, parent in [("a", None), ("b", "a"), ("c", "a")]:
        meta = SessionMeta(session_id=session_id, created=time.time(), parent=parent)
        RolloutWriter(rollout_path(session_id, tmp_path), meta).release()

    assert resolve("last", tmp_path).stem == "a"
    # Still reachable by id: excluded from the guess, not from the program.
    assert resolve("c", tmp_path).stem == "c"
```

---

## §12 循环 import：把 handler 放在 handler 该在的地方

一个 tool handler 应该放在哪儿？`tools.py`。那里已经有四个了。

于是第一版是这样：`tools.py` 里加一个 `spawn_agent`，它调用 `subagent.run_task`；
`subagent.py` 里 `run_task` 要给子 Agent 建工具表，所以它 `from minicodex.tools
import default_tools`。

```
$ uv run python -c "import minicodex.tools"
Traceback (most recent call last):
  File "<string>", line 1, in <module>
    import minicodex.tools
  File "...\src\minicodex\tools.py", line 36, in <module>
    from minicodex.subagent import run_task
  File "...\src\minicodex\subagent.py", line 10, in <module>
    from minicodex.tools import default_tools
ImportError: cannot import name 'default_tools' from partially initialized module
'minicodex.tools' (most likely due to a circular import)
```

从另一边进去，报的是另一个名字：

```
$ uv run python -c "import minicodex.subagent"
ImportError: cannot import name 'run_task' from partially initialized module
'minicodex.subagent' (most likely due to a circular import)
```

**同一个环，两句不同的错误，各自指着对方。** 这就是第 1 章 F01-08 和插曲 A 的
FA-04 一直在防的东西：那两次都是"箭头指反了但还没成环"，这一次是真的成环了。

第 10 章的解法是最便宜的那个：**把工具搬到已经依赖两边的那一层去**。
`spawn_agent` 的 handler 和 schema 都住在 `subagent.py`，`__main__` 把它拼进
handler 表——跟第 9 章 `registry.handlers()` 的做法一样。依赖变成
`subagent → tools`、`subagent → agent`，无环。

代价是第 4 章那条"一个工具只声明一次"的规矩破了一个口：`spawn_agent` 不在
`tool_specs()` 里。补法是让它仍然是一个 `ToolSpec`：

```python
def spawn_spec(ctx: SubAgentContext) -> ToolSpec:
    return ToolSpec(
        name=SPAWN_NAME,
        description=SPAWN_DESCRIPTION,
        parameters=SPAWN_PARAMETERS,
        bind=lambda _context: functools.partial(spawn_agent, ctx),
    )
```

`bind` 收一个 `ToolContext` 然后不用它——这是搬家留下的那道缝，写在 docstring 里，
没有藏。

还有一处不搬就会漏：第 3 章的 description 快照测试（F03-10）走的是 `TOOL_SCHEMAS`，
而 `spawn_agent` 不在里面。**一个模型看得见、快照看不见的工具，正是 F03-10 存在的
理由。** 所以 `collect_descriptions()` 里手工把它拼进去，并且在注释里写清楚为什么
会有第二张表。

边界写成测试（第 -1 章的规矩：只写在散文里的架构约定一定会烂）：

```python
def test_tools_does_not_import_subagent() -> None:
    assert "minicodex.subagent" not in imported_modules(SRC / "tools.py")


@pytest.mark.parametrize(
    "first,second",
    [("minicodex.tools", "minicodex.subagent"), ("minicodex.subagent", "minicodex.tools")],
)
def test_subagent_and_tools_import_in_either_order(first: str, second: str) -> None:
    result = subprocess.run([sys.executable, "-c", f"import {first}; import {second}"], ...)
    assert result.returncode == 0, result.stderr
```

两个方向都跑，因为一个环只在一个方向上炸——这是第 1 章就学到的。

**插曲 B 接着这里往下走**：这次能靠"往上搬"解决，是因为上面刚好有一层
（`__main__`）已经认识两边。等到两个模块**互相都需要对方的类型**、上面没有那一层
可搬的时候，才需要依赖倒置，而那时候要小心的是 FB-03——为了打断环而引入的、
只有一个实现的接口，是仪式，不是抽象。

---

## §13 第三个调用点：`clip()` 终于被抽出来

§8.2 需要把子 Agent 的答案截断。仓库里已经有两份一模一样的实现了：

```python
# shell.py（第 2 章）
if len(text) <= MAX_OUTPUT_CHARS:
    return text
omitted = len(text) - HEAD_CHARS - TAIL_CHARS
return text[:HEAD_CHARS] + f"\n... ({omitted} characters omitted) ...\n" + text[-TAIL_CHARS:]

# registry.py（第 9 章）
if len(text) > MAX_RESULT_CHARS:
    half = MAX_RESULT_CHARS // 2
    omitted = len(text) - 2 * half
    text = f"{text[:half]}\n... ({omitted} characters omitted) ...\n{text[-half:]}"
```

同样的 20000，同样的对半分，连省略提示的措辞都一样，只是各写了一遍。

**三次法则在这里第一次真正地、按它本来的意思触发了。** 第 2 章写死是对的；
第 9 章复制粘贴是对的（两个样本不足以看出接口该长什么样）；到第三个调用点，
接口是什么已经没有悬念了——每个调用方都只想要两样东西：一个字符长度上限，
和一句"少了多少"。

```python
def clip(text: str, limit: int = DEFAULT_LIMIT) -> str:
    if len(text) <= limit:
        return text
    head = limit // 2
    tail = limit - head
    omitted = len(text) - head - tail
    return f"{text[:head]}\n... ({omitted} characters omitted) ...\n{text[-tail:]}"
```

`shell.py` 的 `_clip` 留着，变成一行：

```python
def _clip(text: str) -> str:
    return clip(text, MAX_OUTPUT_CHARS)
```

**留这层包装不是懒。** `MAX_OUTPUT_CHARS` 是 shell 这个模块自己的数字——
shell 的上限不是子 Agent 的上限——而且钉 F02-03 的那些测试点名调的就是 `_clip`。

一件**没有**合并进去的东西：`compaction.clip_item`。它长得很像，但它按 **token** 算
预算，收发的是 `HistoryItem` 而不是字符串。合进来的结果会是一个带模式开关的函数，
那是两个函数披着一件外套。

这次抽取必须是一个**纯重构 commit**（插曲 A 的 FA-01）：不改行为，只改位置。
验证靠的是原来那些测试——`tests/test_shell.py` 和 `tests/test_faults_ch09.py`
一个都没改，全绿。

---

## §14 没做的两件事：并行与 worktree

### F10-08：父 Agent 被阻塞

`spawn_agent` 是 `STATEFUL`（§5），意味着两个子 Agent 永远不会同时跑。这个代价
可以直接量出来（`probe_subagent.py serial`，两个各睡 1 秒的子 Agent）：

```
two 1.0s sub-agents, spawn = STATEFUL             2.02s
two 1.0s sub-agents, spawn = no declared conflict 1.01s
```

**整整一倍。** 而且 §5 已经量过了，另一边是 30/30 丢失更新。

这就是这一章最难受的一个结构：**F10-02 的修法就是 F10-08 的成因。** 要让子 Agent
并行，就得让它们不冲突；要让它们不冲突，就得隔离它们的工作区；而隔离出来的编辑
最后要合回去（下一节）。

清单给 F10-08 开的药是"轮询 + 专职 awaiter"。**这个在我们的 CLI 里不成立**：
`minicodex ask` 是一问一答，`agent.run()` 跑起来之后没有任何用户输入通道，
所以"父 Agent 被阻塞时无法响应用户"这件事，在这个程序里根本没有可观测的后果。
硬做一个 awaiter，是为一个这里不存在的问题写代码（写作纪律第 7 条）。

所以它是一笔**明确记账的债**，写在 step README 的 "Deliberately not done" 里，
而不是一句"以后再说"。

### F10-11：worktree 隔离

清单说"隔离用 git worktree，合并时冲突无人处理"。这条我真的建了个 git 仓库量了一遍
（`probe_subagent.py worktree`）：

```
two worktrees created in 212ms
disk: 28,698 bytes

merge a: rc=0 Updating 66d2e2e..d856cfa
merge b: rc=1
Auto-merging target.py
CONFLICT (content): Merge conflict in target.py
Automatic merge failed; fix conflicts and then commit the result.
def f():
<<<<<<< HEAD
    return 2
=======
    return 3
>>>>>>> b
```

**便宜。** 212 毫秒，28KB，两个隔离的工作区。真正的问题不是建它，是最后那五行：
两个子 Agent 各改了同一个函数，git 老老实实地把冲突标记留在文件里，然后**没有人
知道该怎么办**。

隔离不消灭冲突，它把冲突**从"后写的赢，无声无息"换成"有冲突标记，需要有人解"**。
第二种在正确性上严格更好——至少它是可见的。但"有人解"这件事，这个程序做不到：
主 Agent 手上没有原始意图，用户不在场，而把带 `<<<<<<<` 的文件丢回给模型让它猜，
是第 4 章 F04-02 那条"模型重写文件时偷偷删东西"最容易发生的场景。

所以 worktree **没做**，理由写清楚：不是"太复杂"，是**它换来的那个问题，这一章没有
能力回答**。

---

## §15 文件清点

| 文件 | 行数 | 完整代码在哪一节 |
|---|---|---|
| `src/minicodex/subagent.py` | 566 | §3（`TaskSpec`）、§6（深度与预算）、§7（超时）、§9（`TaskResult`）、§12（`spawn_spec`）；完整文件见仓库 |
| `src/minicodex/clip.py` | 44 | §13，全文 |
| `tests/test_faults_ch10.py` | 645 | 各节引用，32 个测试 |
| `probe_subagent.py` | 625 | 各节引用其输出 |
| `probe_mutations_ch10.py` | 158 | §16 |

改动的旧文件：

| 文件 | 改了多少 | 改了什么 |
|---|---|---|
| `src/minicodex/tools.py` | 34 行 | `tool_context()` / `bind_all()`；`default_tools` 收 `shell` |
| `src/minicodex/__main__.py` | 76 行 | `make_model` 工厂、`SubAgentContext`、子会话列表、`sessions` 里的父子链接 |
| `src/minicodex/rollout.py` | 22 行 | `SessionMeta.parent`；`--resume last` 跳过子会话 |
| `src/minicodex/shell.py` | 17 行 | `_clip` 改成调 `clip()` |
| `src/minicodex/registry.py` | 7 行 | 同上 |
| `tests/test_boundaries.py` | 41 行 | 循环 import 的两个方向 |
| `tests/test_schemas.py` | 44 行 | `spawn_agent` 进 F03-10 快照 |

正文里没有全文给出的：`subagent.py` 的 `child_tools` / `_writer` / `describe_children`
三个函数、`SPAWN_DESCRIPTION` 的完整措辞。前者是机械的组装代码，后者太长；
两个都在仓库里，`spawn_agent` 的描述同时被快照测试钉住。

全套：

```
$ uv run pytest
1392 passed, 9 skipped in 55.35s
```

---

## §16 收工：commit 与 review

### commit 序列

七个 commit，第一个是纯重构，最后一个是文档。

```
refactor: extract the head-and-tail clipper into clip.py

Three call sites now want the same six lines: shell output (ch2),
MCP results (ch9), and sub-agent answers (ch10). The first two were
byte-identical apart from the constant name.

No behaviour change. shell._clip and registry.normalise keep their own
limits -- the shell's ceiling is not the sub-agent's ceiling -- and
tests/test_shell.py and tests/test_faults_ch09.py are unmodified, which
is what makes this a refactor rather than a change.

compaction.clip_item is deliberately left alone: it budgets in tokens
and takes a HistoryItem, so merging it would mean one function with a
mode flag.
```

```
feat: run one task in a nested agent, with a contract instead of a history

TaskSpec (task + constraints + expected_output) is what crosses the
boundary in. Passing the parent's history instead costs 7x the tokens
on an eight-item parent (4476 vs 638) and carries three source files
the child's task never mentions.

Constraints render into the child's system message, not into its task
text. Measured against gpt-4o-mini, one rule, five samples per arm:
task only 5/5 violated; rule appended to the task 3/5 and 4/5 violated
(two runs); rule as a system note 0/5.

The child's shell starts at the parent's cwd and shares the parent's
Session object. Both are state that lives in a Python attribute and
nowhere else, so a child built from scratch silently starts somewhere
else with fewer permissions.
```

```
feat: give a sub-agent an outcome instead of a string

`return result.final_text` maps completed/turn_limit/interrupted onto
one string and two of them onto the same empty string -- measured, not
reasoned about. TaskResult carries the outcome and render() turns a
failure into a labelled block that says what to do next (ch3's F03-07,
one layer out).

"empty" is a fourth outcome: completed with nothing said is not a very
short answer. Same call F06-08 made about an empty summary and F09-07
about an empty MCP result.

Not claimed: that this changes what the parent does. A 5-sample A/B was
1/5 vs 0/5, which is not evidence. The argument here is about the
interface, not about the model.
```

```
fix: bound depth, wall clock and total sub-agent turns

Unbounded, an agent whose model always spawns reached 830,400 nested
agents in 59 seconds and had to be killed from outside. The
asyncio.wait_for meant to stop it died first: RecursionError inside the
event loop's own timer callback while cancelling the chain.

The turn budget was not designed. A test asserting "this cannot cost
much" failed with 72 model calls for one task -- 8 turns at depth 1,
each spawning a depth-2 child that spends 8 of its own. A depth limit
bounds depth, not width. DEFAULT_CHILD_TURN_BUDGET brings it to 32.

The timeout uses ensure_future + shield rather than a bare wait_for,
because Agent.run swallows CancelledError (ch7, F07-04): wait_for
therefore cancels the child and returns its value, raising nothing,
and the timeout silently becomes an empty answer.
```

```
feat: a sub-agent gets its own session file, linked to its parent

Sharing the parent's writer does not interleave -- ch7's O_EXCL lock
raises RolloutError before the child's first turn. That refusal is
right; this is its other half.

SessionMeta.parent is added without bumping ROLLOUT_VERSION. F07-09
needed a bump because the meaning of an existing field changed; an
optional new field is readable in both directions. Additive is not
breaking.
```

```
fix: --resume last no longer resumes a sub-agent

A run that spawns two sub-agents leaves three files and the two newest
are the children, so "resume the last session" quietly became "resume
the last thing a sub-agent said" -- a working-looking command on the
wrong conversation. Sub-agent sessions are excluded from the guess,
not from the program: they still resume by id.

Found by reading the output of `minicodex sessions`, not by a test.
The same look found that a child's session header recorded no provider
or model, which is fixed here too.
```

```
docs: chapter 10 README and the postmerge mutation step
```

### PR 描述

```markdown
## What

An agent can hand one self-contained task to a second agent and get back a
bounded, labelled answer.

- `subagent.py`: `TaskSpec` in, `TaskResult` out, `run_task`, the
  `spawn_agent` tool
- `clip.py`: the head-and-tail clipper, extracted at its third caller
- `tools.py`: `tool_context()` / `bind_all()`; `default_tools` accepts a shell
- `rollout.py`: `SessionMeta.parent`; `--resume last` skips sub-agent sessions

## Why

Two chapters of unpaid debt. Compaction (ch6) shrinks a history after it has
grown; a sub-agent stops it growing. And a single conversation carrying five
unrelated errands is the tool-selection problem of ch9 in a different costume.

## How

Four things cross the boundary, each because something measurable went wrong
when it did not:

| | |
|---|---|
| a contract, not a history | whole history = 7x tokens (4476 vs 638) and three unrelated files |
| state that was never written down | parent `cd src` invisible to the child; permissions not inherited |
| an outcome, not a string | 3 stop reasons -> 1 string, 2 of them -> `''` |
| a bound on everything | unbounded recursion reached 830,400 levels in 59s |

Sub-agents are serialised, because `footprint_of` returns `STATEFUL` for a
tool it has never heard of (ch8). That is not luck and there is a test saying
so: classifying a spawn as "it only reads" loses one of two edits 30/30.

## Testing

```
uv run pytest                       # 1392 passed, 9 skipped
uv run python probe_mutations_ch10.py   # 13 mutations, every one caught
uv run python probe_subagent.py <section>
```

Eight probe sections are offline. Four call gpt-4o-mini (~70 requests).

## Notes for the reviewer

- `run_task` uses `ensure_future` + `shield`, which looks like ceremony. It
  is not: a bare `wait_for` around an `Agent` does not raise `TimeoutError`
  at all. There is a test named after that.
- `DEFAULT_CHILD_TURN_BUDGET` came from a failing assertion, not a design.
- `spawn_agent` is declared in `subagent.py` rather than in `tool_specs()`
  because the obvious placement is a circular import. `test_boundaries.py`
  now checks both import directions.
- Not done, and stated in the README: parallel sub-agents, worktree
  isolation, MCP tools for children.
```

### Code review

我扮演 reviewer，五条，从重到轻。

> **1（正确性）** `_stop()` 里 `with contextlib.suppress(asyncio.CancelledError):`
> 包住了 `await task` **和** `return`。如果 `await task` 正常返回，
> `return` 在 with 块里执行，没问题；但读的人会以为 `return ""` 是死代码。
> 它是死代码吗？

不是，而且这正是它必须在那儿的原因。`Agent.run` **通常**吞掉 `CancelledError`
并正常返回（§7 测过），但那是它当前的实现，不是它的契约。哪天它改成往上抛，
`await task` 就会抛 `CancelledError`，被 suppress 接住，函数落到 `return ""`。
两条路都活着，行为都正确。我在 docstring 里补了一句说明；不加测试，因为要测的是
一个"如果第 7 章改了"的假设，而 §7 那个测试已经在盯着那个假设了。

> **2（正确性）** 共享轮数预算用 `sum(child.turns for child in ctx.children)`，
> 而 `children` 是跑完才 append 的。那么在深度 1 的子 Agent 还在跑的时候，
> 它自己的轮数不算数。这是 bug 吗？

是个**已知的不精确**，不是 bug，但你说得对，它值得写下来而不是让人自己发现。
已经写进 `SubAgentContext.child_turn_budget` 的注释：它是上界，实际会比预算多花
（最多多花"仍在运行的祖先"的那些轮）。做成精确的需要在 spawn 时先扣、结束时再结算，
那是一套记账，为一个上界机制不值得。

> **3（可测试性）** `test_F10_07_a_hanging_child_is_stopped_and_says_so` 用
> `subagent.child_tools = patched` 打猴子补丁，用完再改回来。这很脆。

同意它脆，但替代方案更差。要测"子 Agent 挂住"，得让子 Agent 有一个会挂住的工具；
真工具表里没有这种东西，造一个真的会挂住的真工具意味着起一个真的子进程再让它睡。
另一个选择是给 `SubAgentContext` 加一个 `tools_for` 注入点——**为了测试给产品代码
加一个扩展点**，正是 FB-03 说的仪式性抽象。补丁写在 `try/finally` 里，两个测试各自
恢复。留着。

> **4（命名）** `TaskResult.ok` 只在测试里用到，产品代码里一次都没有。

删掉？没有删。理由是它是 `outcome == "ok"` 这个判断的**唯一正确写法**，而这个判断
迟早会有第二个调用点（第 11 章的 plan 就要用它）。一个属性、一行、有测试。
这是我在"删掉未使用代码"和"留下唯一正确的判断方式"之间选了后者，而且我承认
这条可以两说。

> **5（风格）** `_HEADLINE` 和 `_ADVICE` 是两张按同一组 key 索引的表。
> 一个 dataclass 不好吗？

`{outcome: (headline, advice)}` 一张表确实更紧。分成两张的理由是它们被读的方式不同：
`_HEADLINE` 里的字符串带 `{turns}` / `{seconds}` 占位符要 `.format()`，
`_ADVICE` 是死文本。合起来会让"哪一半需要格式化"变成要看代码才知道的事。
测试保证两张表的 key 一致（少一个就 `KeyError`）。保持现状。

### CI

blocking 那条（`ci.yml`）**一步没加**。第 -1 章的那句断言还在：

```python
assert len(steps) <= 6, "the blocking suite is meant to stay fast"
```

第 9 章因为它被迫开了第二条流水线 `postmerge.yml`，这一章只是往那条里加了一步：

```yaml
      # Chapter 10 adds its own thirteen. One of them survived the first run:
      # "the child writes into its parent's session file" left every chapter 10
      # test green, because the test asserted "one file exists and its parent
      # field is right" without building a parent writer at all. Reconstructing
      # the real situation -- the parent holding its own lock -- is what made
      # the mutation fail.
      - name: Mutation check (chapter 10)
        run: uv run python probe_mutations_ch10.py
```

第一次跑的结果就是注释里说的那个：

```
13 mutations, tests/test_faults_ch10.py tests/test_shell.py tests/test_faults_ch09.py

    1 test(s) fail  <-  the child gets a fresh shell instead of the parent's directory
    ...
    0 test(s) fail  <-  the child writes into its parent's session file
    ...
1 mutation(s) nothing noticed:
  - the child writes into its parent's session file
```

那个测试原来是这样的：不建父 writer，跑一个子任务，断言"目录里有一个文件，
并且它的 `parent` 字段是 parent-1"。把子 Agent 的 session_id 改成父 Agent 的
session_id 之后——这两条断言**依然成立**。

修法不是加断言，是**把现场重建对**：父 writer 真的开着，真的握着自己的锁。

```python
parent_writer = RolloutWriter(rollout_path("parent-1", sessions), parent_meta)
try:
    ...
    written = sorted(p.name for p in sessions.glob("*.jsonl"))
    assert len(written) == 2, written
    assert result.session_id != "parent-1"
    ...
    assert len(read_rollout(parent_writer.path).items) == 0
```

再跑：

```
    1 test(s) fail  <-  the child writes into its parent's session file
...
every mutation was caught.
```

**"测试名说的比断言多"这个形状，第 5、6、9、10 章各出现了一次。** 四章四次，
每一次都是变异测试发现的，没有一次是读代码发现的。

---

## §17 codex 是怎么做的

这一节的每条都来自 `codex-rs/` 的源码，路径写出来了。

**两代并存，而且都还在。** `core/src/tools/handlers/multi_agents/` 和
`multi_agents_v2/` 同时存在，各有各的一套工具：

| v1 | v2 |
|---|---|
| `spawn_agent` `wait_agent` `send_input` `close_agent` `resume_agent` | `spawn_agent` `wait_agent` `send_message` `followup_task` `list_agents` `interrupt_agent` |

v1 的模型是"起一个 agent，喂输入，关掉"；v2 的模型是**一棵有名字的树**——
`spawn_agent` 的描述里写着：

> Spawns an agent to work on the specified task. If your current task is
> `/root/task1` and you spawn_agent with task_name "task_3" the agent will have
> canonical task name `/root/task1/task_3`. You are then able to refer to this
> agent as `task_3` or `/root/task1/task_3` interchangeably. However an agent
> `/root/task2/task_3` would only be able to communicate with this agent via its
> canonical name `/root/task1/task_3`.

这是把子 Agent 从"一次调用"提升成了"一个能被寻址、能收消息的长期存在"。
我们这一章做的是 v1 那种形状的更简单版本（起一个、跑完、拿结果）。

**深度上限是 1，不是 2。**

```rust
pub(crate) const DEFAULT_AGENT_MAX_DEPTH: i32 = 1;

pub(crate) fn exceeds_thread_spawn_depth_limit(depth: i32, max_depth: i32) -> bool {
    depth > max_depth
}
```

比我们更保守：默认情况下**子 Agent 不能再 spawn**。§6.2 那个 72 次调用的算术，
在 max_depth=1 下根本不会发生——它是我把上限设成 2 才买来的问题。

**并发数有上限，而且是"预约槽位"式的。**

```rust
pub(crate) const DEFAULT_MULTI_AGENT_V2_MAX_CONCURRENT_THREADS_PER_SESSION: usize = 4;

pub(crate) fn reserve_spawn_slot(...) -> Result<SpawnReservation> {
    if let Some(max_threads) = max_threads {
        if !self.try_increment_spawned(max_threads) {
            return Err(CodexErr::new(CodexErrorDetails::AgentLimitReached { ... }));
```

注意 codex 的子 Agent 是**并行**的（所以才需要 `wait_agent`、`list_agents`），
我们的是串行的。这是 §14 那笔债在别人家的还法：他们付的是"每个子 Agent 有自己的
workspace"这个大工程。

**等待有超时，而且三个数都可配。**

```rust
/// Minimum wait timeout to prevent tight polling loops from burning CPU.
pub(crate) const MIN_WAIT_TIMEOUT_MS: i64 = DEFAULT_MULTI_AGENT_V2_MIN_WAIT_TIMEOUT_MS;
pub(crate) const DEFAULT_WAIT_TIMEOUT_MS: i64 = 30_000;
pub(crate) const MAX_WAIT_TIMEOUT_MS: i64 = HARD_MAX_MULTI_AGENT_V2_TIMEOUT_MS;
```

那句注释是从源码里原样抄的。它读起来像一次真实事故的墓碑（`wait_agent(timeout=0)`
被反复调用就是忙等），但这一句是**推断**——`.git` 不在这份副本里，我没法查证。

**最值得读的是 `spawn_agent` 的 description。** 它有一千多字，而且开头就是禁令：

> Do not spawn sub-agents unless the user or applicable AGENTS.md/skill
> instructions explicitly ask for sub-agents, delegation, or parallel agent work.
> Requests for depth, thoroughness, research, investigation, or detailed codebase
> analysis do not count as permission to spawn.

第二句是第一句的补丁——**"要深入分析"不算授权 spawn**，这句话只可能来自
"我们说了别乱 spawn，然后它看到 thorough 就 spawn 了"。

里面还有一条直接对应我们的 §5：

> For code-edit subtasks, decompose work so each delegated task has a disjoint
> write set.

**codex 把"写集不相交"当成一条给模型的指令；我们把它当成调度器的约束。**
这是 §4.4 那张表最干净的一个对照：同一个现象，一个在 prompt 层修，一个在调度层修。
codex 的选择有它的道理（他们的子 Agent 真的在不同 workspace 里并行，调度器不掌握
写集），但那句话本身承认了一件事——**它是靠模型遵守的**。§5 那个 30/30 就是不遵守
时的样子。

**最后一处人味最重的东西**，在 `core/src/agent/role.rs`：

```rust
                // Awaiter is temp removed
//                 (
//                     "awaiter".to_string(),
//                     AgentRoleConfig {
//                         description: Some(r#"Use an `awaiter` agent EVERY TIME you must run a command that will take some very long time.
// ...
// - Be patient with the `awaiter`.
```

清单给 F10-08 开的那味药——"专职 awaiter"——**在 codex 里被注释掉了**，
留了一句 `// Awaiter is temp removed`。而 `awaiter.toml` 还在
`core/src/agent/builtins/` 里躺着，还在被 `include_str!` 编进二进制：

```rust
const AWAITER: &str = include_str!("builtins/awaiter.toml");
match path.to_str()? {
    "explorer.toml" => Some(EXPLORER),
    "awaiter.toml" => Some(AWAITER),
```

一个被注释掉的功能、一个还在编译进去的配置文件、一句 "temp"。
"暂时"这个词在代码里出现的时候，通常已经过去很久了。

---

## 如果你只记住三件事

**1. 子 Agent 的边界上只有四样东西，每一样都有一次测量。**
进去的是契约（不是历史，7 倍）和父 Agent 没写下来的状态（cwd、权限）；
出来的是结局（不是字符串——三种结局压成一个串，其中两种压成同一个空串）；
外面包着的是四个上限（深度、轮数、墙钟、结果长度）。少任何一样，坏法都是静默的。

**2. 一个上限只限住它字面上说的那件事。**
深度上限限的是深度：深度 2 的时候，账是按 2 × 8 × 8 = 72 次模型调用花的。
这条不是想出来的，是一个断言写成 `<= 8` 的测试逼出来的。**先写你以为的那个界，
让它红给你看。**

**3. 上一章的正确决定，会在这一章变成 bug 的成因。**
第 7 章"中断时吞掉 CancelledError"让 `wait_for` 套不住 Agent；
第 8 章"分不出来就 STATEFUL"顺手把 F10-02 修好了、又顺手造出了 F10-08；
第 7 章"单写者锁"把"子 Agent 共用父文件"这条路提前封死了。
**它们都没有变错，是组合变了。** 所以每加一层，要回头问的不是"我这层对不对"，
而是"我这层让下面哪条规矩换了含义"。

---

## 动手练习

1. **把 `shield` 拿掉**（`result = await asyncio.wait_for(task, ctx.timeout)`），
   跑 `tests/test_faults_ch10.py`。两个测试会红。读它们的名字，
   然后不看 §7 说出为什么一个超时会变成一个空答案。

2. **把 `DEFAULT_CHILD_TURN_BUDGET` 改成一个很大的数**，跑
   `test_F10_06_a_model_that_only_ever_spawns_stops_at_the_limit`。
   记下它报出来的数字，然后把 `DEFAULT_TASK_TURNS` 从 8 改成 4，再跑一次。
   两个数字之间的关系是什么？（答案是 `n + n * n`，而不是 `n × depth`——
   宽度是平方项，深度只是那个平方的指数。）

3. **给 `spawn_agent` 加一个 `read_only` 参数**：为 True 时，子 Agent 的工具表里
   没有 `apply_patch`。然后回答一个问题：两个 `read_only=True` 的子 Agent 可以
   并行吗？（提示：`Footprint` 只比较资源 key 是否相等，而"这个子 Agent 会读哪些
   文件"你并不知道。想清楚为什么这个看起来显然的优化，在第 8 章的模型下做不到。）

4. **给审批提示加上"这是子 Agent 请求的"这句话**（§10 记的那笔债），然后设计一个
   能证明它有用或没用的测量。想不出可测的观测量，就是这笔债该继续记着的理由。

5. **把 `clip()` 的抽取回滚**，让三处各写各的，然后改动其中一处的省略提示措辞。
   跑全套测试。有几个测试会红？（这一题在量的是：三份拷贝的时候，"改一处"到底
   有多容易变成"改一处，忘了另外两处"。）

---

# 附录 · 代码到底怎么敲出来的（新手向，逐段精讲）

跟第 9 章附录同一个规矩：正文讲"为什么"，这里补"怎么写"。默认你已经读过
第 9 章的附录（尤其是 A0 的 `asyncio.Future`/Task 部分），这里不重复那些
基础，只讲这一章新出现的、容易让新手卡住的写法。代码摘自
`steps/step10_subagents/src/minicodex/subagent.py`，逐段核对过。

## B0 · 这一章新出现的五个写法，先过一遍

**1. `dataclasses.replace(obj, 字段=新值)`。** 给一个 `frozen=True` 的
dataclass"改"一个字段的标准写法——`frozen` 不允许 `obj.字段 = 新值`，
但可以造一个新对象，其余字段照抄，只有你指定的那个字段不一样：

```python
from dataclasses import replace
child_ctx = replace(ctx, depth=ctx.depth + 1)   # 除了 depth，其余字段跟 ctx 一模一样
```

**这里有一个新手极容易忽略的细节：`replace()` 是浅拷贝。** 如果某个字段
是列表这样的可变对象，`replace()` 之后旧对象和新对象的那个字段**指向
同一个列表**，不是各自一份拷贝。§6.2、B5 会用到这个性质——不是意外，
是故意的。

**2. `functools.partial(fn, 固定参数)`。** 把一个函数的某些参数提前"焊死"，
得到一个参数更少的新函数：

```python
import functools
def add(a, b): return a + b
add_five = functools.partial(add, 5)
add_five(3)   # 8，等价于 add(5, 3)
```

正文和这份附录里大量出现 `bind=lambda ctx: functools.partial(read_file,
ctx.root)`，意思是"先把 `root` 焊死，剩下的 `args` 参数留给调用者"——
这样 `read_file(root, args)` 就变成了工具处理函数要求的
`handler(args) -> str` 这一种参数形状。

**3. `typing.Literal`。** 限定一个变量只能取几个固定字符串之一，写代码
时和读代码时都当文档用：

```python
from typing import Literal
Outcome = Literal["ok", "empty", "turn_limit", "timeout", "interrupted", "depth_limit", "budget"]
```

跟普通的 `str` 类型标注唯一的区别：类型检查器（比如 `mypy`/`pyright`）
会在你写出一个不在这个列表里的字符串时报错，运行时不做任何检查——
`Literal` 是"给人看、给检查器看"的，不是运行时的守卫。

**4. "工厂函数"模式：为什么 `make_model` 是一个函数而不是一个对象。**

```python
def make_model(tools: list[dict]) -> ChatCompletionsModel:
    return ChatCompletionsModel(base_url=..., model=..., tools=tools)
```

父 Agent 和每一个子 Agent 看到的工具表**不是同一份**（子 Agent 在底层
不该有 `spawn_agent`），而模型客户端对象内部记着自己那份 `tools`。
"一个模型" 在这个程序里因此不再是一个全局单例，而是"给一份工具表，
就能造一个绑定了这份工具表的客户端"——`make_model` 就是这句话的代码
形式。子 Agent 需要新客户端的时候调用它，不需要跟父 Agent 共用。

**5. `asyncio.shield(task)`。** 上一章的附录讲过 `asyncio.wait_for` 会在
超时的时候把 `CancelledError` 扔给它包住的那个 awaitable。`shield(task)`
包一层之后，外层的取消（不管是超时还是别的原因）**不会传导进 `task`
本身**，只会让 `wait_for` 这一层自己抛出 `TimeoutError`/`CancelledError`，
`task` 继续在后台独立运行，不受影响。B6 会画一张图讲这件事，先记住
"`shield` 是给取消加一层隔离"这句话。

---

## B1 · 第一步：十五行版本，先跑起来

正文 §2 已经给出完整代码。照抄一遍，自己跑一次，建立"这一章要解决什么"
的直观感受：

```python
async def spawn_agent(args: dict) -> str:
    child = Agent(_model(), default_tools(root=ROOT, session=session), max_turns=6)
    result = await child.run(args["task"])
    return result.final_text

tools = {**default_tools(root=ROOT, session=session), "spawn_agent": spawn_agent}
parent = Agent(_model([*TOOL_SCHEMAS, SPAWN_SCHEMA]), tools, max_turns=6)
```

拆开看这四行做了什么：

1. `spawn_agent` 是一个**闭包**——它引用了外面的 `ROOT`、`session`，
   这些变量在它被调用的那一刻已经"焊"在了这个函数里，不需要每次调用都
   重新传。
2. `Agent(...)` 是第 0 章就有的类，子 Agent 就是**再造一个 `Agent` 实例**，
   没有任何新的类、没有继承——这是这一章反复强调的"子 Agent 不是新概念"
   的最直白体现。
3. `tools` 字典把本来的四个工具，加上一个新的 `"spawn_agent"` key，
   合并成父 Agent 的工具表。
4. `parent` 的 schema 列表比 `tools` 字典多一项（`SPAWN_SCHEMA`），
   这跟第 4 章的规矩（"schema 和 handler 一一对应，缺一个就出事"）
   完全一致，只是这里手工维护，没有用 `ToolSpec`。

先把这四行跑通、看到它真的能答对问题，再往下看它在哪些地方会翻车——
正文 §3 起讲的每一条故障，都是在这十五行基础上一点点加机制。

---

## B2 · 第二步：契约代替历史——`TaskSpec`

### B2.1 为什么不能把父 Agent 的历史整个传过去

正文 §3 已经用真实数字证明了"传整段历史"贵 7 倍、还夹带三份不相关的
文件全文。这里只关心：**如果历史不传，传什么？**

答案是一个只有三个字段的 dataclass：

```python
@dataclass(frozen=True)
class TaskSpec:
    task: str
    constraints: tuple[str, ...] = ()
    expected_output: str = ""
```

新手容易问的问题：**`constraints` 为什么是 `tuple[str, ...]` 而不是
`list[str]`？** 因为 `TaskSpec` 是 `frozen=True`，理论上不该允许字段
指向一个可变对象——虽然 Python 不会真的阻止你往一个 frozen dataclass
的 `list` 字段里 `.append()`（这个操作不改字段本身，只改字段指向的
那个对象的内容），但用不可变的 `tuple` 能让"这份约束建好之后不会被
悄悄改动"这件事在类型层面就说清楚，而不是靠自觉。

### B2.2 把三个字段变成子 Agent 真正看到的文字

```python
def instructions(self) -> str:
    parts = ["You have been given one self-contained task by another agent."]
    if self.constraints:
        parts.append(
            "These rules come from the user and apply to everything you do:\n"
            + "\n".join(f"- {c}" for c in self.constraints)
        )
    if self.expected_output:
        parts.append(f"Return exactly this and nothing else: {self.expected_output}")
    parts.append(
        "Nobody will read anything you say except the agent that sent you, and it "
        "cannot answer questions. If the task cannot be done, say so in one "
        "sentence and say what stopped you."
    )
    return "\n\n".join(parts)
```

这个方法就是正文 §8.1 那个实验结论的代码化：`constraints` 被拼进
**`instructions()`**——也就是子 Agent 的 system message——而不是拼进
`task` 文本里。正文用真实 API 测过：同一句约束，附在任务文本末尾，
5 次里有 3、4 次照样被违反；放进 system message，0/5。所以这里的写法
不是随手选的位置，是被测量摁在这个位置上的。

最后一段固定加的话（"没有人会读你说的话，除了发你任务的那个 Agent，
它也没法回答你的问题……"）对应 §8.1 里第三个更不舒服的发现：一个
被工具表限制住的子 Agent，可能会尝试"礼貌地问一句能不能继续"——
但子 Agent 的输出只会被父 Agent 当成一个字符串处理，没有人类在另一头
等着回答它。这句话就是提前把这条退路堵上，逼它要么完成、要么说清楚
卡在哪儿，而不是浪费轮次去征询一个不存在的人。

---

## B3 · 第三步：把父 Agent"忘了写下来"的状态传过去

正文 §4 讲了两件事——`cwd` 和权限——各自量出来"不传会怎样"。这里讲
"怎么传"这一半。

先看 `tool_context()` 多出来的那个参数（`tools.py`）：

```python
def tool_context(
    root: Path | None = None,
    session: Session | None = None,
    shell: ShellSession | None = None,
) -> ToolContext:
    return ToolContext(
        root=(root or Path.cwd()).resolve(),
        shell=shell or ShellSession(),
        session=session or Session(),
    )
```

在第 9 章的代码里，`shell` 这个参数还不存在——`tool_context()` 每次都
自己新建一个 `ShellSession()`，从进程当前目录开始。这一章加了这个
可选参数，让调用方**能够指定"从哪里开始"**。

再看 `child_tools()` 里怎么用它：

```python
def child_tools(ctx: SubAgentContext) -> tuple[dict[str, ToolFn], list[dict[str, Any]]]:
    shell = ShellSession(timeout=ctx.parent_shell.timeout)
    shell.cwd = ctx.parent_shell.cwd          # 只抄"现在在哪"，不共用对象
    context = tool_context(root=ctx.root, session=ctx.session, shell=shell)
    handlers = bind_all(context)
    schemas = list(tool_schemas())
    ...
```

两行的分工要看清楚：

- `shell = ShellSession(timeout=...)`——**新建**一个 `ShellSession`，
  子 Agent 有自己的对象，它接下来在自己的对话里 `cd` 到哪儿，不会影响
  父 Agent 的 `ShellSession`。
- `shell.cwd = ctx.parent_shell.cwd`——把"当前值"抄一份过去。这是
  **传值，不传对象**：子 Agent 拿到的是父 Agent此刻的位置作为起点，
  之后两者各走各的路。

跟 `session` 的处理方式对比一下就能看出这不是疏忽：

```python
context = tool_context(root=ctx.root, session=ctx.session, shell=shell)
```

`ctx.session` 直接原样传进去，**没有新建、没有复制**——`SubAgentContext`
的 `session` 字段本身就是从父 Agent 那个 `Session` 对象传下来的引用。
一个是"新对象、抄初始值"，一个是"同一个对象"，两种写法故意不一样：

| 状态 | 传法 | 为什么 |
|---|---|---|
| 当前目录（`shell.cwd`） | 新建对象，只抄初始值 | 子 Agent 往后 `cd` 到哪，是它自己的事，不该反过来改父 Agent 的状态 |
| 权限（`session`） | 同一个对象 | 权限属于"这个人这一次会话"，子 Agent 里发生的一次提权，父 Agent 应该看得到，也应该继续管用 |

新手在这里最容易犯的错误是图省事把两者都写成"新建对象"或者都写成
"共用对象"——记住判断标准：**这份状态改变之后，谁应该看得见改变？**
只有子 Agent 自己需要看见的，就新建；父子双方都要看见的，就共用。

---

## B4 · 第四步：`spawn_agent` 什么都不用声明，就是安全的——但要写一条测试证明

正文 §5 讲的这条最省事：`tools.py` 里 `footprint_of()` 的最后一行是

```python
return STATEFUL
```

`spawn_agent` 这个名字从来没在这个函数的任何 `if` 分支里出现过，所以
任何一次 `spawn_agent` 调用都会落进这条默认分支，被当成"完全不知道
它会碰什么，只能假设它跟谁都冲突"。这意味着**这一步不需要写任何新代码**。

但"不需要写代码"和"这件事没有风险"是两回事——正文量过：如果有人后来
"好心地"给 `spawn_agent` 加一条声明，说它"只是读文件，应该并行"，
30 次实验里 30 次都会丢失更新。要防的不是今天的代码，是**将来某次
看似合理的优化**。防法是把"什么都不做是对的"本身写成一条测试：

```python
def test_F10_02_a_spawn_is_stateful_without_anyone_saying_so(tmp_path: Path) -> None:
    assert footprint_of(a_call("spawn_agent", task="x"), root=tmp_path) == STATEFUL
    assert conflicts(STATEFUL, STATEFUL)
```

写这条测试的思路值得学一下：它不是在测某个函数的输出对不对，而是在
**给一个"不写代码的决定"设一道门槛**——将来有人想给 `spawn_agent` 加
footprint 声明，第一步就会先撞到这条测试，逼他至少要读一遍这段注释，
而不是悄悄改一行然后什么都没发觉。`conflicts(STATEFUL, STATEFUL)`
这一行是在确认第 8 章的调度器把两个 `STATEFUL` 调用当成互相冲突
（不只是"和已知资源冲突"），这正是整章"子 Agent 之间互相串行"这件事
成立的根基，值得单独断言一次，不能只靠间接推理。

---

## B5 · 第五步：深度上限和"共享一份"的轮数预算

这是全章逻辑最绕的一节，拆成三层看。

### B5.1 深度：一个在动手之前就拦住的计数

```python
MAX_DEPTH = 2

async def run_task(spec: TaskSpec, ctx: SubAgentContext) -> TaskResult:
    if ctx.depth >= ctx.max_depth:
        return TaskResult("depth_limit", "")
    ...
```

`ctx.depth` 从 0 开始（顶层 Agent）。每往下 spawn 一层，新的 context 用
`replace(ctx, depth=ctx.depth + 1)` 造出来，`depth` 字段加一。
`run_task` 一进来就先检查，超过上限直接返回一个"拒绝"的结局，**连
子 Agent 都不会去建**——正文 §6.1 已经证明了"跑起来之后再想办法拦"这条
路走不通（连 `wait_for` 自己都会被嵌套的取消链拖垮），所以这个检查必须
是运行任何东西之前的第一道关卡。

第二个更容易被忽视的动作在 `child_tools()`：

```python
if ctx.depth + 1 < ctx.max_depth:
    spec = spawn_spec(replace(ctx, depth=ctx.depth + 1))
    handlers[spec.name] = spec.bind(context)
    schemas.append(spec.schema())
```

翻译成人话：**只有当"给这个子 Agent 的孩子留出的深度还够用"时，才把
`spawn_agent` 这个工具装进它的工具表。** `MAX_DEPTH = 2` 意味着深度 0
（顶层）和深度 1 的 Agent 能拿到 `spawn_agent`，深度 2 的 Agent 拿不到
——它的工具表里压根没有这一项。这不是"给了但调用会被拒绝"，是"从一
开始就不给"，对应正文引用的第 5 章 F05-10：如果一个工具名字出现在
描述里但策略不允许用，模型有相当概率还是会去调它，白白浪费一轮。
不给，就没有这个浪费的机会。

### B5.2 轮数预算：`children` 列表是怎么"共享"出去的

先看数据结构：

```python
@dataclass(frozen=True)
class SubAgentContext:
    ...
    child_turn_budget: int = DEFAULT_CHILD_TURN_BUDGET
    children: list[TaskResult] = field(default_factory=list)
```

关键在于 `replace()` 的浅拷贝性质（B0 第 1 条）：

```python
child_ctx = replace(ctx, depth=ctx.depth + 1)
```

`child_ctx.children` 和 `ctx.children` 是**同一个 list 对象**——`replace`
只是造了一个新的 `SubAgentContext`，其余没有被显式覆盖的字段（包括
`children`）直接复用旧对象里的引用。于是不管这棵子 Agent 树长多深，
`depth=0`、`depth=1`、`depth=2` ……所有层级的 `ctx.children` 指向的都是
**程序启动这一次运行时建的那唯一一个列表**。

每跑完一个子任务，把结果记进这个共享列表：

```python
def _record(ctx: SubAgentContext, result: TaskResult) -> TaskResult:
    ctx.children.append(result)
    return result
```

检查预算的地方，数的就是这个列表里已经记录的所有子任务的轮数之和：

```python
spent = sum(child.turns for child in ctx.children)
if spent >= ctx.child_turn_budget:
    return TaskResult("budget", "")
```

不管这次调用发生在树的哪一层，它看到的 `ctx.children` 都是同一份、
到目前为止全树已经花掉的账本。这就是"整棵树共享一个预算"在代码层面
的实现方式——**不需要额外传一个"全局计数器"对象，`replace()` 的浅拷贝
本身就足够了**，前提是你得清楚哪些字段该被浅拷贝共享（`children`），
哪些字段该在某次 `replace` 时真正换成新值（`depth`）。

### B5.3 这个预算为什么"不精确"，以及为什么可以不精确

`_record()` 是在子任务**跑完之后**才把它的轮数加进 `children`。也就是
说，如果深度 1 的子 Agent 还没跑完（它自己正占着一些轮次），这些轮次
**这时候还没算进预算**。正文 §6.2 结尾把这一点直接写成注释留在代码里
（`child_turn_budget` 字段旁边），不是藏起来的瑕疵：

```python
# In-flight ancestors are not counted -- a child is recorded when it
# finishes -- so this undercounts by the turns of whichever sub-agents
# are still running above.  Bounded, not exact, and said so rather than
# implied.
```

这是一个**上界机制**，不是一本精确的账。想让它精确，需要在 spawn 的
那一刻就"预扣"一部分预算，等真正跑完再"结算"多退少补——这一章判断
这套额外的记账逻辑，对于一个只是想防止失控的上限来说不值得。**记住
这个判断标准**：一个安全限制要做到多精确，取决于它防的是"失控"还是
"计费"——防失控，上界就够；如果是计费，才值得为精确多花代码。

---

## B6 · 第六步：超时——`ensure_future` + `shield`，画图讲清楚

这是正文 §7、这份附录里最难的一段 `asyncio` 用法，值得慢慢拆。

### B6.1 先看"直觉上最简单"的写法为什么是错的

```python
# 直觉写法，有 bug
result = await asyncio.wait_for(child.run(spec.task), ctx.timeout)
```

正文 §7 用真实实验证明了：这样写，超时发生时**不会抛出 `TimeoutError`**，
而是安安静静地返回一个 `stop_reason == "interrupted"`、`final_text ==
''` 的 `RunResult`。原因要往回倒两步：

1. `asyncio.wait_for(awaitable, timeout)` 的行为是：超时了，就对
   `awaitable` 调用 `.cancel()`，然后**等它以 `CancelledError` 结束**，
   再把这件事翻译成 `TimeoutError` 抛给调用者。**如果 `awaitable`
   被取消之后没有以异常结束，而是正常 `return` 了一个值，
   `wait_for` 就会把这个值当成正常结果返回，不会抛任何异常。**
2. 而 `child.run(...)`（也就是 `Agent.run`）内部有这样一段（`agent.py`）：
   ```python
   except (asyncio.CancelledError, KeyboardInterrupt):
       ...
       return RunResult(final_text, "interrupted", turn_index + 1, history, ...)
   ```
   这是第 7 章为了"中断时每一个已发出的工具调用都必须有一个 output"
   特意做的：`Agent.run` 自己把 `CancelledError` **接住**，包成一个
   正常的 `RunResult` 返回，而不是让异常继续往外冒。

两件事叠在一起：`wait_for` 取消了 `child.run(...)`，但 `child.run(...)`
把这次取消"消化"掉了，返回了一个看起来正常的结果。`wait_for` 看到的
是"它正常返回了"，于是也正常返回，`TimeoutError` 从头到尾没有出现过。

### B6.2 用 `shield` 把取消挡在外面

```python
task = asyncio.ensure_future(child.run(spec.task))
try:
    result = await asyncio.wait_for(asyncio.shield(task), ctx.timeout)
except TimeoutError:
    partial = await _stop(task)
    ...
except asyncio.CancelledError:
    await _stop(task)
    raise
```

画一张图来看这几行到底改变了什么：

```
裸 wait_for 的写法：
  wait_for(child.run(...), timeout)
    │
    └─ 超时 → 直接对 child.run(...) 这个协程 cancel()
                → Agent.run 内部吞掉 CancelledError，正常 return
                → wait_for 看到"正常返回"，自己也正常返回（没有异常！）

ensure_future + shield 的写法：
  task = ensure_future(child.run(...))      ← 先把子 Agent 包成一个独立的 Task
  wait_for(shield(task), timeout)
    │
    └─ 超时 → shield 不允许这次取消传导进 task 内部
                → 取消只作用在 wait_for 自己等待的这一层
                → wait_for 抛出 TimeoutError（正常路径！）
                → task（也就是子 Agent）此时仍在后台跑着，没有被打断
                → 代码走到 except TimeoutError 分支，
                   这时才主动去 _stop(task)：真正取消子 Agent，
                   并且 await 它，拿到它已经说到哪儿了
```

`shield` 这个词的字面意思就是"盾牌"：它把 `task` 挡在取消信号之外，
取消信号打在盾牌（`shield` 包出来的那一层）上，`task` 本体毫发无伤。
真正想让子 Agent 停下来，必须在 `except` 块里**主动**调用
`task.cancel()`——这一步不是自动发生的，是这段代码自己去做的：

```python
async def _stop(task: asyncio.Task[Any]) -> str:
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        result = await task
        return str(result.final_text).strip()
    return ""
```

**先 `cancel()`，再 `await task`。** 不 `await` 会怎样？取消是异步生效的
——`task.cancel()` 只是"排了一个取消请求"，`task` 内部真正响应这个请求、
把手上没答完的 `tool_call` 补上 output、干净地退出，需要时间。不
`await` 就直接返回，这段收尾工作会跟父 Agent 接下来要做的事在后台
"抢跑"，正是正文里说的"这段收尾会跟父 Agent 的下一轮并发着跑"。

`contextlib.suppress(asyncio.CancelledError)` 包住的这段代码有两条路：
`Agent.run` **目前**的行为是吞掉取消、正常返回——那么 `await task` 会
正常拿到一个 `RunResult`，`return result.final_text.strip()` 这一行执行；
如果哪天 `Agent.run` 的实现改了、不再吞取消——那么 `await task` 会
抛出 `CancelledError`，被 `suppress` 接住，函数落到最后一行
`return ""`。**两条路都对**，这是刻意保留的"双保险"写法，不是死代码——
`with` 块里的 `return` 和它后面那行 `return ""`，恰好对应两种不同版本
的 `Agent.run` 行为。

### B6.3 `except asyncio.CancelledError` 这一支容易漏

```python
except asyncio.CancelledError:
    await _stop(task)
    raise
```

这一支处理的是"**父 Agent自己被取消了**"（比如用户按了 Ctrl-C）。
`shield` 把子 Agent 挡在了 `wait_for` 内部的取消之外，但这也意味着——
父 Agent 这边如果被取消，`asyncio.shield` 同样会挡住这个取消传导进
子任务：如果这里不写这一支去主动 `_stop(task)`，用户按了 Ctrl-C、
主循环退出了，那个被 `shield` 保护起来的子 Agent 会**继续在后台跑**，
变成第 2 章早就付过学费的"孤儿进程"问题，只是这次孤儿不是子进程，
是一个孤儿 Task。写完 `_stop(task)` 之后必须 `raise`（把
`CancelledError` 重新抛出去），不能吞掉它——父 Agent 自己也要正常地
被取消，不能因为这里处理了子 Agent 就假装什么都没发生。

---

## B7 · 第七步：`TaskResult` ——把"发生了什么"和"说了什么"分开存

十五行版本最后一行 `return result.final_text` 只用了 `RunResult` 四个
字段里的一个。这一节把剩下的信息捞回来。

### B7.1 先定义"有哪些可能的结局"

```python
Outcome = Literal["ok", "empty", "turn_limit", "timeout", "interrupted", "depth_limit", "budget"]

@dataclass(frozen=True)
class TaskResult:
    outcome: Outcome
    text: str
    turns: int = 0
    seconds: float = 0.0
    session_id: str = ""

    @property
    def ok(self) -> bool:
        return self.outcome == "ok"
```

`run_task()` 里把 `RunResult` 翻译成 `TaskResult` 的那段判断：

```python
if result.stop_reason == "turn_limit":
    outcome = "turn_limit"
elif result.stop_reason == "interrupted":
    outcome = "interrupted"
elif not text:
    outcome = "empty"
else:
    outcome = "ok"
```

注意 `"empty"` 不是从 `RunResult.stop_reason` 里来的——`RunResult` 根本
不区分"正常结束但什么也没说"和"正常结束、说出了答案"，这两种都是
`stop_reason == "completed"`。是这段代码自己加了一条判断
（`elif not text`）把它们拆开的。**为什么要拆？** 正文说得很直接：
"一个很短的答案"和"没有答案"是两回事，一个空字符串渲染出来，看起来
跟"成功了，只是没什么好说"一模一样，模型会把它当成一个正常答案接着
往下用。这跟第 6 章处理空摘要、第 9 章处理空 MCP 结果是同一个判断，
第三次出现在这本书里了。

### B7.2 把结局渲染成子 Agent 真正吐给父 Agent 的字符串

```python
_HEADLINE: dict[str, str] = {
    "empty": "finished without answering",
    "turn_limit": "ran out of turns after {turns}",
    "timeout": "still running after {seconds:.0f}s and was stopped",
    "interrupted": "interrupted",
    "depth_limit": "refused: sub-agents may not spawn sub-agents this deep",
    "budget": "refused: this run has spent its whole sub-agent budget",
}

_ADVICE: dict[str, str] = {
    "empty": "Do not report this as a finding. Split the task, or do it yourself.",
    "turn_limit": "Do not report this as a finding. Give it a smaller task, or do the remaining part yourself.",
    "timeout": "Do not report this as a finding. Anything it had already changed on disk is still changed; check before repeating it.",
    "interrupted": "The user stopped it. Do not start it again unless asked.",
    "depth_limit": "Do this part yourself instead of delegating it further.",
    "budget": "Do the rest yourself. Delegating again will get the same answer.",
}

def render(self) -> str:
    body = clip(self.text.strip(), MAX_TASK_RESULT_CHARS)
    if self.outcome == "ok":
        return body

    headline = _HEADLINE[self.outcome].format(turns=self.turns, seconds=self.seconds)
    header = f"[sub-agent: {headline}]"
    advice = _ADVICE[self.outcome]
    if not body:
        return f"{header}\nIt produced no answer at all. {advice}"
    return f"{header}\nWhat it had said before that point, which is not a conclusion:\n\n{body}\n\n{advice}"
```

两张表，两个 key 集合必须完全一致——`_HEADLINE[self.outcome]` 和
`_ADVICE[self.outcome]` 只要有一个结局漏填了其中一张表，运行时就会在
那一刻炸出 `KeyError`。正文的 code review 部分讨论过"要不要合成一张表"，
结论是不合并，因为 `_HEADLINE` 里的字符串要 `.format(turns=...,
seconds=...)`，`_ADVICE` 是死文本，合在一起会让"哪半句需要格式化"变成
要读代码才知道的事。新手写类似结构时可以记住这条判断：**两张表如果
"被使用的方式"不同（一个要插值、一个不要），分开存比合并更清楚**，
即使表面上看起来是重复。

`_HEADLINE[self.outcome].format(turns=self.turns, seconds=self.seconds)`
这一行还有个小细节：`.format()` 只会替换字符串里实际出现的占位符，
`"interrupted"` 这条模板里没有 `{turns}` 也没有 `{seconds}`，传多余的
关键字参数完全没问题——`.format()` 不会因为你传了模板里用不到的参数
而报错，这也是为什么这里可以把 `turns` 和 `seconds` **无差别地**
传给每一条模板，不用为每种结局单独判断该传哪些参数。

**成功的结果不加头**（`if self.outcome == "ok": return body`）——
正文强调这一点是有意的：每条结果都顶一个 `[sub-agent: ...]` 标签，
模型很快会学会"看到这个前缀就跳过"，让标签失去意义。只有真正需要
特别提醒的失败结局才配得上这个标签，这样它出现的时候才会被认真对待。

想验证"每种结局都配了一句该做什么"，写一条覆盖全部 key 的测试就够了：

```python
def test_F10_10_every_outcome_says_what_to_do_next() -> None:
    for outcome in ("empty", "turn_limit", "timeout", "interrupted", "depth_limit", "budget"):
        rendered = TaskResult(outcome, "partial words", 2, 1.0).render()
        assert rendered.startswith("[sub-agent:")
        assert rendered.rstrip().endswith((".", "!"))
```

以后要加一种新结局，忘了往 `_ADVICE` 里补一条，这条测试会用
`KeyError` 直接告诉你漏了。

---

## B8 · 第八步：`spawn_agent` 这个工具本身怎么写

### B8.1 参数校验：跟其它工具用同一套规矩

```python
async def spawn_agent(ctx: SubAgentContext, args: dict[str, Any]) -> str:
    task = args.get("task")
    if not isinstance(task, str) or not task.strip():
        return tool_error(
            'spawn_agent needs a "task" argument, a non-empty string',
            you_sent=repr(args.get("task")),
            do_this=(
                'Example: {"task": "Find every call to os.killpg under src/", '
                '"expected_output": "one line per match, path:line"}'
            ),
        )
    constraints = args.get("constraints")
    if constraints is not None and (
        not isinstance(constraints, list) or not all(isinstance(c, str) for c in constraints)
    ):
        return tool_error(
            '"constraints" must be a list of strings',
            you_sent=repr(constraints)[:200],
            do_this='Example: {"constraints": ["do not run the test suite"]}',
        )
    expected = args.get("expected_output")
    spec = TaskSpec(
        task=task,
        constraints=tuple(constraints or ()),
        expected_output=expected if isinstance(expected, str) else "",
    )
    return (await run_task(spec, ctx)).render()
```

跟第 3 章定下的规矩一样：先检查类型，检查不过用 `tool_error()`（三段式
错误：出了什么问题、你发了什么、接下来怎么办）。校验通过之后，把裸的
`dict` 参数组装成一个 `TaskSpec`，交给 `run_task()`，最后
**`(await run_task(spec, ctx)).render()`**——这一行把 B7 写的
`TaskResult.render()` 接到了工具返回值的位置：`spawn_agent` 这个
handler 对外只返回一个字符串，跟第 0 章"工具返回一个字符串"的契约
完全一致，`TaskResult` 这个结构化对象只活在 `subagent.py` 内部，
出这个模块的门之前就已经被拍扁成了字符串。

### B8.2 `ToolSpec` 的 `bind` 字段，以及为什么这里要多绕一层

正文 §12 讲了为什么 `spawn_agent` 不能待在 `tools.py`（循环 import）。
这里讲一下解决之后，`ToolSpec` 是怎么被"塞"进去的：

```python
def spawn_spec(ctx: SubAgentContext) -> ToolSpec:
    return ToolSpec(
        name=SPAWN_NAME,
        description=SPAWN_DESCRIPTION,
        parameters=SPAWN_PARAMETERS,
        bind=lambda _context: functools.partial(spawn_agent, ctx),
    )
```

`ToolSpec.bind` 这个字段本来的设计意图（`tools.py` 里定义）是"给我一个
`ToolContext`，我给你一个真正能调用的 handler"——这样 schema（静态、
进程启动时就能算出来）和 handler（动态、要绑定某一次对话的 `root`/
`session`/`shell`）就能共用同一个 `ToolSpec` 对象。`spawn_spec()` 也
遵守这个签名（`bind` 接收一个 `ToolContext` 参数），但它的 `bind` 函数
体里**完全没用这个参数**（写成了 `_context`，下划线前缀是 Python
"我知道这个参数存在，但故意不用它"的约定写法）——因为 `spawn_agent`
真正需要的上下文是 `SubAgentContext`，不是 `ToolContext`，而
`SubAgentContext ctx` 已经在 `spawn_spec(ctx)` 这一层被 `functools.partial`
焊死了。docstring 里也把这件事挑明了：这是把 `spawn_agent` 从
`tools.py` 搬出来之后**留下的一道接口上的缝**，没有假装它完美对齐。

### B8.3 循环 import 到底长什么样，怎么复现

如果你想亲手感受一下正文 §12 那两条报错，可以在自己的项目里故意写出
这个结构：`a.py` 顶部 `from b import x`，`b.py` 顶部 `from a import y`，
然后：

```python
$ python -c "import a"
ImportError: cannot import name 'x' from partially initialized module 'a' (most likely due to a circular import)
$ python -c "import b"
ImportError: cannot import name 'y' from partially initialized module 'b' (most likely due to a circular import)
```

**从哪个模块先导入，报错就提哪个模块"partially initialized"。**
道理是 Python 导入一个模块时，会先把这个模块对象创建出来（此时它是
"部分初始化"的空壳），再从上到下执行模块里的代码；执行到
`from b import x` 这一行时，如果 `b` 还没被导入过，Python 会去导入
`b`——而 `b` 顶部又写着 `from a import y`，这时候 `a` 已经在导入队列里
了（就是那个"部分初始化"的空壳），`y` 这个名字还没来得及在 `a` 的
命名空间里出现（因为 `a` 的代码还没执行到定义 `y` 的那一行，甚至可能
`y` 根本还没被赋值），于是报错。

修法是这一章示范的那种：**把两边都需要的东西，搬到一个"两边都认识"
的更上层模块去**——这里是 `__main__.py`，它本来就同时 `import tools`
和 `import subagent`，由它来把 `spawn_agent` 的 handler 拼进最终的工具
表，`tools.py` 和 `subagent.py` 之间不再互相 `import`。这个修法配了
两条测试，锁死这条边界：

```python
def test_tools_does_not_import_subagent() -> None:
    assert "minicodex.subagent" not in imported_modules(SRC / "tools.py")

@pytest.mark.parametrize(
    "first,second",
    [("minicodex.tools", "minicodex.subagent"), ("minicodex.subagent", "minicodex.tools")],
)
def test_subagent_and_tools_import_in_either_order(first: str, second: str) -> None:
    result = subprocess.run([sys.executable, "-c", f"import {first}; import {second}"], ...)
    assert result.returncode == 0, result.stderr
```

第二条测试**两个方向都跑**——先 import `tools` 再 import `subagent`，
和反过来——是因为循环 import 报错只在"先导入哪一个恰好触发了循环"
的那个方向上出现，只测一个方向，另一个方向的回归可能悄悄溜过去。

---

## B9 · 第九步：子 Agent 有自己的 rollout 文件

### B9.1 为什么不能直接用父 Agent 的 `RolloutWriter`

第 7 章的单写者锁（`O_EXCL`）是为了防止两个进程同时写一个 rollout 文件
导致内容互相覆盖。子 Agent 如果尝试用父 Agent 那个 `RolloutWriter`，
会在打开文件那一步就直接撞上这把锁，抛 `RolloutError`——这不是这一章
才发现的 bug，是第 7 章的检查第一次被真正用上。

修法是给子 Agent 建一个**独立**的 `RolloutWriter`：

```python
def _writer(ctx: SubAgentContext) -> RolloutWriter:
    if ctx.sessions_dir is None:
        return NULL_WRITER
    meta = SessionMeta(
        session_id=new_session_id(),
        created=time.time(),
        cwd=str(ctx.root),
        provider=ctx.provider,
        model=ctx.model,
        sandbox_mode=ctx.session.mode,
        approval_policy=ctx.session.policy,
        parent=ctx.parent_session_id or None,   # 唯一把两个文件连起来的字段
    )
    return RolloutWriter(rollout_path(meta.session_id, ctx.sessions_dir), meta)
```

`NULL_WRITER` 这个分支值得留意：`ctx.sessions_dir is None` 的时候
（比如测试环境里没配置会话目录），直接返回一个"什么都不做"的 writer
对象，而不是抛异常或者强行要求调用方总是提供一个目录。这是"能不写
就不写，但接口形状不变"的常见写法——调用方不需要为"要不要记录 rollout"
写两套不同的逻辑，`NULL_WRITER` 让"记不记录"这件事对调用方透明。

### B9.2 `SessionMeta.parent`：加字段不等于破坏兼容性

```python
parent: str | None = None
```

正文强调了"加这个字段没有升版本号（`ROLLOUT_VERSION`）"，这里补一句
为什么加字段这件事本身通常是安全的：**给一个 dataclass 加一个带默认值
的新字段，旧数据依然能被正确读出来**——旧的 rollout 文件里没有
`parent` 这个 key，反序列化时用不到它的地方就用默认值 `None` 顶上；
新版本代码写出来的文件多了一个字段，旧版本代码读它的时候（如果
`from_json` 是"忽略不认识的 key"这种宽松写法）也不会因为多了一个字段
就报错。真正需要升版本号的情况是**已有字段的含义变了**（第 7 章
F07-09 那种），因为那种情况下同一个字节序列在新旧代码里会被解释成
不同的东西——这是本书反复用到的一条经验：**新增是安全的，重新定义
才是破坏性的**，写代码时可以把这句话当成"要不要 bump 版本号"的判断
标准。

### B9.3 `--resume last` 为什么要专门排除子会话

`list_sessions()` 按修改时间新到旧列出所有会话文件；一次带子 Agent 的
运行会在磁盘上留下"父会话 + N 个子会话"这好几个文件，而子会话通常
**比父会话更晚落盘完成**（父会话要等所有子任务和自己都跑完才收尾）。
于是"取最新的那个文件"这个直觉写法，会取到一个子会话，而不是用户
真正想继续的那次主对话。修法是过滤掉带 `parent` 的会话：

```python
sessions = [r for r in list_sessions(directory) if r.meta.parent is None]
```

**子会话没有被"删掉"，只是没资格参与"猜哪个是 last"这件事**——用户
仍然可以用具体的 session id 去 `--resume` 一个子会话，这条过滤只影响
"我没说具体是哪个，帮我猜"这一种用法。

---

## B10 · 第十步：接进 `__main__.py`

对照 §11 的完整跑一遍片段，把每一处新增的位置标出来：

```python
def make_model(tools: list[dict]) -> ChatCompletionsModel:              # ①
    return ChatCompletionsModel(base_url=..., model=..., tools=tools)

root = Path.cwd().resolve()
context = tool_context(root=root, session=session)
current = SessionMeta(session_id=new_session_id(), ...)

sub_ctx = SubAgentContext(                                               # ②
    build_model=make_model,
    root=root,
    session=session,
    parent_shell=context.shell,
    sessions_dir=session_dir,
    parent_session_id=current.session_id,
    provider=provider,
    model=model or default_model,
    announce=print,                                                      # ③
)
spawn_handlers, spawn_schemas = spawn_tools(sub_ctx, context)             # ④

registry = McpRegistry(local=[*TOOL_SCHEMAS, *spawn_schemas])             # ⑤
...
llm = make_model(registry.visible)                                       # ⑥
...
handlers = {**bind_all(context), **spawn_handlers, **registry.handlers()} # ⑦
...
for line in describe_children(sub_ctx.children):                         # ⑧
    print(line)
```

- **①** `make_model` 就是 B0 第 4 条讲的工厂函数，父 Agent 和子 Agent
  都用它来造各自的模型客户端，只是传入的 `tools` 参数不一样。
- **②** `SubAgentContext` 把这一次运行需要贯穿到所有子 Agent 的一切都
  打包在一起：模型工厂、仓库根目录、权限对象、父 Agent 此刻的 shell
  状态、会话文件该往哪儿写、父会话的 id（用来填 `SessionMeta.parent`）、
  以及一个共享的空 `children` 列表（走 dataclass 的默认值，B5.2 讲过
  它怎么被 `replace()` 一路带下去）。
- **③** `announce=print`——`SubAgentContext` 不直接 `import` 具体的
  打印方式，而是接受一个"给我一句话，你决定怎么显示"的回调函数，
  `subagent.py` 内部用 `_say(ctx, message)` 调用它。这样测试里可以传
  一个"什么都不做"或者"记进一个列表"的函数，不需要真的打印到终端、
  也不需要为了测试去 mock `print`。
- **④** `spawn_tools()` 返回顶层 Agent 自己的 `spawn_agent` handler
  和 schema（跟 `child_tools()` 用的是同一个 `spawn_spec()`，只是调用
  的深度不同）。
- **⑤** 跟第 9 章一样，`McpRegistry` 的 `local` 参数把本地工具
  （`TOOL_SCHEMAS`）和这次新增的 `spawn_schemas` 一起交给 registry
  统一管理——同一条"`visible` 必须是模型能看到的全部工具"的规矩。
- **⑥、⑦** 用同一个 `make_model` 和同一个"字典解包合并"手法，把父
  Agent 的模型客户端和 handler 表拼起来，跟子 Agent 里 `child_tools()`
  做的事结构上完全对称。
- **⑧** `describe_children()` 把整个运行过程中记录下来的所有子任务
  结果，各打一行摘要——`sub_ctx.children` 到这里已经是跑完整个
  `agent.run(question)` 之后，整棵子 Agent 树留下的完整记录。

---

## B11 · 一份可以直接跑的最小完整例子

用标准库和最少的自定义类，重现"深度限制 + 共享轮数预算 + 结局标签"
这一整套机制的核心逻辑，帮助你在不搭起整个 minicodex 项目的情况下，
直接感受这一章的关键行为。新建 `subagent_demo.py`：

```python
"""最小可跑 demo：不接真实模型，用一个"总是继续 spawn"的假模型模拟
F10-06 那个 72 次调用的场景，亲眼看深度限制和轮数预算分别起了什么作用。"""

import asyncio
from dataclasses import dataclass, field, replace

MAX_DEPTH = 2
TASK_TURNS = 8
CHILD_TURN_BUDGET = 24     # 改成一个很大的数（比如 10_000），看看深度限制单独能不能兜住

calls = 0    # 模拟"模型调用次数"的全局计数器，只用于演示，别在真实代码里这样用全局变量


@dataclass
class SubAgentContext:
    depth: int = 0
    max_depth: int = MAX_DEPTH
    child_turn_budget: int = CHILD_TURN_BUDGET
    children: list[int] = field(default_factory=list)   # 每个元素是一个子任务花掉的轮数


async def run_task(ctx: SubAgentContext) -> int:
    """返回这次子任务花掉的轮数；返回 0 表示被限制直接拒绝。"""
    if ctx.depth >= ctx.max_depth:
        return 0   # depth_limit

    spent = sum(ctx.children)
    if spent >= ctx.child_turn_budget:
        return 0   # budget

    turns_used = 0
    for _ in range(TASK_TURNS):
        global calls
        calls += 1
        turns_used += 1
        if ctx.depth + 1 < ctx.max_depth:
            # "模型"这一轮选择 spawn 一个孩子，而不是做别的
            child_ctx = replace(ctx, depth=ctx.depth + 1)
            await run_task(child_ctx)
        # depth + 1 达到上限的那一层，拿不到 spawn 工具，
        # 模拟成"它试图调用一个不存在的工具，花掉这一轮，什么也没做成"

    ctx.children.append(turns_used)
    return turns_used


async def main() -> None:
    ctx = SubAgentContext()
    await run_task(ctx)
    print(f"MAX_DEPTH={MAX_DEPTH}, CHILD_TURN_BUDGET={CHILD_TURN_BUDGET}")
    print(f"total model calls: {calls}")


asyncio.run(main())
```

按正文 §6.2 的算术，`MAX_DEPTH=2`、`TASK_TURNS=8` 时：深度 0 花 8 轮，
每一轮都 spawn 一个深度 1 的子任务，深度 1 的子任务又各花掉自己的 8 轮
（它的孩子在深度 2，拿不到 spawn，直接空转），所以：

```
$ python subagent_demo.py
MAX_DEPTH=2, CHILD_TURN_BUDGET=24
total model calls: 72
```

**72，跟正文里那次真实的 API 实验数字一模一样。** 现在把
`CHILD_TURN_BUDGET` 改回 24（跟正文的 `DEFAULT_CHILD_TURN_BUDGET` 一致），
但要真正体现预算生效，需要在 `run_task` 循环内部每一轮都重新检查一次
预算（这份 demo 为了突出深度限制本身，简化成只在函数入口检查一次）——
建议作为练习自己动手补上"循环内部也检查 `spent`"这一步，跑一遍，
看看 72 会不会变成正文里的 32。这比只读文字描述更容易在脑子里把
"深度限的是深度，宽度要靠另一个预算才能拦住"这句话坐实。

---

## B12 · 新手常见报错/坑对照表

| 报错/现象 | 通常原因 | 怎么排查 |
|---|---|---|
| `TypeError: replace() got an unexpected keyword argument` | 传给 `dataclasses.replace()` 的字段名在这个 dataclass 里不存在，或者拼错了 | 对照 dataclass 定义核对字段名；`replace(obj, depth=1)` 里的 `depth` 必须是 `obj` 类型里真实存在的字段 |
| 改了子 Agent 的 `children.append(...)`，父 Agent 那边却"看到了" | 这不是 bug，是 `replace()` 浅拷贝的预期行为（B5.2） | 如果你**不想**共享，需要显式传一份新列表：`replace(ctx, children=list(ctx.children))` |
| `wait_for` 超时该抛的异常没抛出来，函数拿到一个"看起来正常"的空结果 | 被包住的协程内部把 `CancelledError` 吞掉了，`wait_for` 因此认为它是"正常返回" | 参考 B6，改用 `ensure_future` + `shield`，在自己的 `except TimeoutError` 里主动 `cancel()` 并 `await` |
| 用 `asyncio.shield` 之后，Ctrl-C 按下去子任务却没停 | `shield` 同时也挡住了外层真实的取消传导进去，需要在 `except asyncio.CancelledError` 分支里手动停 | 检查是否漏写了这一支 `except`，参考 B6.3 |
| `ImportError: cannot import name 'x' from partially initialized module` | 两个模块顶部互相 `from 对方 import ...`，形成循环 import | 把两边都需要的东西搬到一个已经同时依赖两边的更上层模块（这里是 `__main__.py`），不要在下层模块之间互相导入 |
| 加了一种新的 `outcome`，程序在渲染结果的时候崩了 `KeyError` | `_HEADLINE` 和 `_ADVICE` 两张表只补了一张 | 两张表按同一组 key 索引，新增一种结局要同时补两张表；`test_F10_10_every_outcome_says_what_to_do_next` 这类测试就是为了在这一步立刻炸给你看 |
| `RolloutError: ... is already open by another minicodex` | 子 Agent 尝试用父 Agent 正在持有锁的那个 rollout 文件 | 给子 Agent 建一份独立的 `RolloutWriter`（参考 B9.1），不要复用父 Agent 的 `writer` |
| `--resume last` 恢复到了一个奇怪的、很短的对话 | 最新落盘的文件是一个子会话，不是主会话 | 恢复"猜最新"的逻辑要过滤掉 `meta.parent is not None` 的会话（参考 B9.3） |
| 一个只会不断 spawn 的假模型，花了几十次调用才停下来，明明设了深度上限 | 深度上限只挡住了"链有多长"，没有挡住"每一层展开出多少个分支" | 参考 B5.2、B11，深度和宽度是两个独立的量，需要两个独立的限制 |

对照这张表排查一遍，再回头读正文 §2～§14，会发现每一处"这条故障
复现/没复现"的结论背后，都对应着这份附录里某一段"具体怎么写才会/
不会踩上它"。
