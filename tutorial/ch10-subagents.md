# 第 10 章 · 子 Agent

> **代码**：`steps/step10_subagents/`
> **分支**：`feat/subagents`
> **产出**：Agent 能把一件"可以独立干完"的活交给第二个 Agent——它有自己的对话、自己的工具表、自己的会话文件，
> 交回来的是一个**带着结局、有长度上限**的答案
> **前置**：做完第 9 章。测试全部不联网。探针 `probe_subagent.py` 十二段里八段不联网，四段要真的请求模型（需要 `OPENAI_API_KEY`）；
> 没有 key 照着读正文里的输出即可。
> **这一章很长**，可以分三次读：§1–§8 是"交给它什么"，§9–§14 是"它会出什么事"，之后是接进命令行和验证。

---

## §0 开工前的准备

### 0.1 本章会遇到的新概念

- **子 Agent（sub-agent）**：在一次工具调用的内部运行的另一个 Agent。发起它的那个叫**父 Agent**。
- **契约（contract）**：父 Agent 交给子 Agent 的那几样东西——任务、约束、想要的输出形状——而不是整段对话。
- **深度（depth）**：嵌套了几层。用户直接面对的 Agent 是第 0 层，它的子 Agent 是第 1 层，子 Agent 的子 Agent 是第 2 层。
- **结局（outcome）**：一个子任务是怎么结束的——做完了、轮数用完了、超时了、被拒绝了……
- **循环导入（circular import）**：模块 A 导入 B，B 又导入 A。Python 会在导入时直接报错。
- **worktree**：git 的一个功能，让同一个仓库同时有多份互不干扰的工作目录。

### 0.2 本章会遇到的新 Python 写法

| 写法 | 意思 |
|---|---|
| `Literal["ok", "empty"]` | 一种类型标注：这个值只能是这几个字符串之一 |
| `@dataclass(frozen=True)` 里放一个 `list` 字段 | "冻结"的意思是字段不能**重新赋值**；字段指向的列表本身仍然可以 `append` |
| `field(default_factory=list)` | 每个新对象得到一个**新的**空列表（直接写 `= []` 会让所有对象共用同一个） |
| `dataclasses.replace(obj, depth=1)` | 复制一个 dataclass 对象，只改指定的字段；其余字段**原样带过去**（列表带过去的是同一个列表） |
| `asyncio.ensure_future(协程)` | 让协程作为一个独立的任务开始运行，返回这个任务 |
| `asyncio.shield(任务)` | 保护这个任务：外面的等待被取消时，任务本身不被取消 |
| `asyncio.wait_for(x, 秒数)` | 最多等这么久；到时间就取消 `x` 并抛 `TimeoutError` |
| `with contextlib.suppress(异常类型):` | 这个 `with` 里如果抛出这种异常，就当没发生 |
| `nonlocal calls` | 在内层函数里声明：`calls` 是外层函数的那个变量，我要改它 |
| `lambda x, fp=fp: ...` | 用默认参数把**此刻**的 `fp` 固定进 lambda（否则循环结束后所有 lambda 看到的都是最后一个值） |

### 0.3 开分支

```bash
git switch main
git pull
git switch -c feat/subagents
```

---

## §1 这一章要做出来的东西

到第 9 章为止，Agent 是一个人在干活：所有的事都发生在一条对话里。工具可以来自别人的进程，但**思考只有一份**。

这一章加的东西一句话能说清：**让一次工具调用的内部，是另一个 Agent 的一整段对话。**

为什么要这样？

- **上下文**。第 6 章讲的是历史快满了怎么压缩。子 Agent 是另一个方向的答案：与其把"读了十二个文件才找到那一行"的全过程留在主对话里，
  不如让另一个 Agent 去读那十二个文件，只把那一行拿回来。
- **注意力**。一件事里夹着五件不相干的小事，分出去，主对话就只剩主线。

---

## §2 定需求，猜故障

需求：

- 模型可以调用一个工具 `spawn_agent`，把一件任务交给另一个 Agent，拿回一个简短的答案；
- 子 Agent 用的是同一套工具、同一套权限；
- 子 Agent 不管怎么出问题，父 Agent 都得拿到一个说得清的结果；
- 事后能找到子 Agent 当时做了什么。

动工前的猜测清单，十二条：

| 编号 | 猜测 | 怎么判断 |
|---|---|---|
| F10-01 | 父 Agent 刚改的文件还没写到磁盘，子 Agent 读到旧的 | 构造 |
| F10-02 | 两个子 Agent 改同一个文件，互相覆盖 | 让两个真的改 |
| F10-03 | 把整段历史交给子 Agent：token 翻倍，还泄露了不相干的内容 | 量 |
| F10-04 | 只交任务：子 Agent 不知道用户定过的规矩，自己编 | 真模型测 |
| F10-05 | 子 Agent 交回五千字，父 Agent 的上下文照样被撑满 | 真模型测 |
| F10-06 | 子 Agent 再启动子 Agent，无限递归 | 让模型每一轮都这么做 |
| F10-07 | 子 Agent 卡住不回来，父 Agent 永远等下去 | 让一个工具睡一小时 |
| F10-08 | 父 Agent 在等子 Agent 时，没法回应用户 | 看有没有这个通道 |
| F10-09 | 子 Agent 需要审批——问谁？ | 构造 |
| F10-10 | "这件事做不到"和"我没做完"，父 Agent 分不出来 | 构造 + 真模型测 |
| F10-11 | 给每个子 Agent 一份独立的 worktree，合并时冲突没人处理 | 用真的 git 做一次 |
| F10-12 | 父子两个 Agent 的记录混在一个文件里 | 让它们写同一个文件 |

先说结果里最出乎意料的四件：

- **F10-06 不是"会很慢"，是"停不下来"**：一个每轮都启动子 Agent 的模型，一分多钟嵌套了一百多万层，中间还把本该叫停它的那个定时器弄坏了。
- **`asyncio.wait_for` 给一个 Agent 设不了超时**：到时间了，它不抛异常，而是安安静静地交回一个空答案。
- **深度上限管得住深度，管不住宽度**：上限是 2 的时候，一个任务花了 72 次模型调用。这一条是一个写错了数字的断言逼出来的。
- **F10-04 开的药（把规矩写进任务）只有写对地方才管用**；而规矩真的被遵守之后，任务变得做不成了。

---

## §3 先写最简单的：十五行

一个子 Agent 需要什么？一个模型、一张工具表、一句任务。这三样都是现成的：

```python
async def spawn_agent(args: dict) -> str:
    child = Agent(_model(), default_tools(root=ROOT, session=session), max_turns=6)
    result = await child.run(args["task"])
    return result.final_text

tools = {**default_tools(root=ROOT, session=session), "spawn_agent": spawn_agent}
parent = Agent(_model([*TOOL_SCHEMAS, SPAWN_SCHEMA]), tools, max_turns=6)
```

`spawn_agent` 就是一个普通的工具处理函数：造一个新的 `Agent`，让它跑完任务，把最后说的话当作工具的结果交回去。**子 Agent 不是一种新东西，就是第 0 章的那个 `Agent` 类，在一个工具调用里被 `await`。**

这是探针 `probe_subagent.py` 的第一段（整份探针在 §19）。真的跑一次（Windows，gpt-4o-mini，2026-10-01）：

```
$ uv run python probe_subagent.py naive
The `src/minicodex/scheduler.py` file is responsible for managing the scheduling of tool calls in a concurrent environment, focusing on avoiding conflicts between those calls by utilizing a system of footprints that represent resource interactions. It organizes tool calls into batches that can run concurrently without interference, allowing for safe and efficient execution.

The `src/minicodex/paths.py` file handles file path resolution within a specific directory structure, ensuring that paths correspond to actual files in the repository and filtering out irrelevant directories. It provides functionality to resolve paths accurately, offering suggestions for matching files when a provided path does not exist, thereby facilitating precise file management and user feedback.

[completed after 2 turn(s)]
  UserMessage        161 chars
  AssistantMessage   0 chars
  ToolResult         2008 chars
  ToolResult         2115 chars
  AssistantMessage   782 chars
```

能用：父 Agent 两轮完成，两个文件各由一个子 Agent 去读。两个文件加起来八千多个字符，进到父 Agent 历史里的是两段各两千字符的总结。

也已经能看到第一个问题：问的是"各用一句话说它是干什么的"，每个子 Agent 交回来的是两千个字符。

下面的每一节，都是这十五行里**没有**的一样东西。

---

## §4 动工前先收拾：第三份一样的代码

子 Agent 交回来的东西需要一个长度上限（§8）。"太长就留头留尾、中间说明省略了多少"这件事，前面已经写过两遍：

- 第 2 章，`shell.py` 里的 `_clip`，给 shell 的输出用；
- 第 9 章，`registry.py` 的 `normalise` 结尾那四行，给远程工具的结果用。

两份除了常量的名字，一个字符都不差。现在要写第三份。

**两份一样的代码可以忍，第三份出现的时候，就知道它该长什么样了**：每个调用的地方要的都是同样两样东西（一个字符数上限，被告知省略了多少），从来没有谁要过别的。

新建 `src/minicodex/clip.py`：

```python
"""Keeping the head and the tail of something too long.

The fourth call site is what created this module.  Chapter 2 wrote `_clip` for
shell output; chapter 9 wrote the same six lines again for MCP results (same
20,000 limit, same half-and-half split, a different omission message); chapter
10 needs it a third time for what a sub-agent returns.  Two copies is a
tolerable duplicate with a TODO on it, which is what chapter 9 left.  Three is
where the shape stops being a guess: every caller wants the same two things
(a character limit, and to be told what was dropped), and nobody has ever
wanted anything else.

`compaction.clip_item` is deliberately *not* folded in here.  It looks similar
and is not: it budgets in tokens rather than characters, and it takes and
returns a `HistoryItem` rather than a string.  Merging them would mean one
function with a mode flag, which is two functions wearing a coat.
"""

from __future__ import annotations

# What chapters 2 and 9 both independently picked.  Kept as the default so that
# extracting this function changes no behaviour anywhere.
DEFAULT_LIMIT = 20_000


def clip(text: str, limit: int = DEFAULT_LIMIT) -> str:
    """Keep the first half and the last half, say how much went missing.

    Head *and* tail, which is F02-03: a failing test run puts the traceback
    near the top and the summary line at the bottom, and tail-only truncation
    throws away exactly the part that explains the failure.

    Slicing a `str` is safe: Python strings are sequences of characters, so
    there is no multi-byte character to cut in half (F02-11).  The same code
    over `bytes` would produce mojibake at both seams.
    """
    if len(text) <= limit:
        return text
    head = limit // 2
    tail = limit - head
    omitted = len(text) - head - tail
    return f"{text[:head]}\n... ({omitted} characters omitted) ...\n{text[-tail:]}"


__all__ = ["DEFAULT_LIMIT", "clip"]
```

> - **`clip(text, limit)`**：不超过就原样返回；超过就留前一半、后一半，中间一行说省略了多少个字符。
> - 留头**和**尾，是第 2 章量出来的：一次失败的测试，报错在靠前的地方，汇总在最后一行；只留尾巴，丢掉的正好是解释失败原因的那部分。
> - docstring 里说了**没有**合并进来的那个：第 6 章的 `clip_item` 看起来相似，但它按 token 算、处理的是历史里的条目。
>   合并就得加一个"模式"参数——那是两个函数穿了一件外套。

`shell.py` 的 `_clip` 变成一行（开头多一行 `from minicodex.clip import clip`，原来的 `HEAD_CHARS`、`TAIL_CHARS` 两个常量删掉）：

```python
def _clip(text: str) -> str:
    """Keep the first half and the last half, drop the middle.

    A pytest run that fails puts the traceback near the top and the summary
    line at the bottom; everything in between is PASSED lines nobody reads.
    Keeping only the tail throws away exactly the line that explains the
    failure.

    The body moved to `clip.py` in chapter 10, when a third caller wanted it.
    The wrapper stays: `MAX_OUTPUT_CHARS` is this module's own number -- the
    shell's ceiling is not the sub-agent's ceiling -- and the tests that pin
    F02-03 name this function.
    """
    return clip(text, MAX_OUTPUT_CHARS)
```

> 这个函数留着不删：`MAX_OUTPUT_CHARS` 是 shell 自己的数字，而且第 2 章的测试是按这个函数的名字写的。

`registry.py` 的 `normalise` 最后五行变成一行：

```python
    return clip(text, MAX_RESULT_CHARS)
```

```python
def test_clip_keeps_both_ends() -> None:
    text = "START" + "x" * 100 + "END"
    clipped = clip(text, 40)
    assert clipped.startswith("START")
    assert clipped.endswith("END")
    assert "characters omitted" in clipped


def test_clip_leaves_short_text_alone() -> None:
    assert clip("short", 40) == "short"


def test_clip_never_grows_the_text() -> None:
    """A clipper that adds more marker than it removes is worse than none.
    Chapter 6 shipped a compaction that made a history *bigger* (seed 235)."""
    for limit in (40, 200, DEFAULT_LIMIT):
        text = "y" * (limit * 3)
        assert len(clip(text, limit)) < len(text)
```

> 第三个测试来自第 6 章的教训：一个截断函数，加上去的说明比删掉的内容还长，比没有更糟。

这三个测试写在这一章的测试文件 `tests/test_faults_ch10.py` 里。它的开头和帮手：

```python
"""Chapter 10: an agent inside a tool call.

Nothing here touches the network. The child agents are real `Agent`s driven by
a scripted model, the rollout files are real files, and the shell sessions are
real shell sessions -- the only fake thing is the model's judgement.

Test names carry the fault IDs from FAULTS.md.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest

from minicodex.agent import Agent
from minicodex.agent_types import ToolCall
from minicodex.approval import AllowAll, ApprovalReply, ApprovalRequest, Session
from minicodex.clip import DEFAULT_LIMIT, clip
from minicodex.model import Completed, TextDelta, ToolCallDelta
from minicodex.rollout import (
    RolloutError,
    RolloutWriter,
    SessionMeta,
    read_rollout,
    resolve,
    rollout_path,
)
from minicodex.scheduler import STATEFUL, batches, conflicts
from minicodex.shell import ShellSession
from minicodex.subagent import (
    MAX_DEPTH,
    MAX_TASK_RESULT_CHARS,
    SubAgentContext,
    TaskResult,
    TaskSpec,
    child_tools,
    describe_children,
    run_task,
    spawn_agent,
    spawn_spec,
)
from minicodex.tools import footprint_of, tool_context

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


class ScriptedModel:
    """Replays a fixed list of turns and remembers what it was sent.

    `sent` is the point of the class here: most of this chapter is about what
    the child's request contains, which is not observable from its answer.
    """

    def __init__(self, turns: Sequence[Any]) -> None:
        self.turns = list(turns)
        self.sent: list[list[dict[str, Any]]] = []

    async def stream(self, messages: Sequence[dict[str, Any]]) -> Any:
        self.sent.append([dict(m) for m in messages])
        turn = self.turns[min(len(self.sent) - 1, len(self.turns) - 1)]
        if isinstance(turn, str):
            yield TextDelta(turn)
        else:
            for index, (call_id, name, arguments) in enumerate(turn):
                yield ToolCallDelta(
                    call_id=call_id, index=index, name=name, arguments=json.dumps(arguments)
                )
        yield Completed("stop")


def context_for(
    root: Path,
    model: Any,
    *,
    session: Session | None = None,
    shell: ShellSession | None = None,
    **kwargs: Any,
) -> SubAgentContext:
    return SubAgentContext(
        build_model=lambda _schemas: model,
        root=root,
        session=session or Session(mode="workspace-write", approver=AllowAll()),
        parent_shell=shell or ShellSession(),
        **kwargs,
    )


def a_call(name: str, **arguments: Any) -> ToolCall:
    return ToolCall("call_1", name, arguments, json.dumps(arguments))
```

> - **`ScriptedModel`**：一个假模型，按事先写好的脚本回答——每一轮要么说一句话，要么发起几个工具调用。和前几章测试里用的是同一种东西，多了一个 `sent`：
>   **记下每次被发送的全部消息**。这一章有一半的测试要问的是"子 Agent 收到了什么"，而这从它的回答里看不出来。
> - **`context_for(root, model, ...)`**：造一个 `SubAgentContext`（§9）。`build_model=lambda _schemas: model`：不管给什么工具列表，都返回这个假模型。
> - **`a_call(name, **arguments)`**：造一个 `ToolCall`。

跑一遍全部测试确认什么都没变，然后**单独提交**：

```bash
uv run pytest
git add src/minicodex/clip.py src/minicodex/shell.py src/minicodex/registry.py tests/test_faults_ch10.py
git commit -m "refactor(clip): one head-and-tail clipper instead of two copies and a third on the way"
```

> **重构和新功能分开提交。** 这个提交不改变任何行为，审查的人只需要确认"前后一样"。和后面的新功能混在一起，这件事就没法确认了。

---

## §5 再收拾一处：把"造工具表"拆成两步

第 4 章以来，`tools.py` 里的 `default_tools(root, session)` 一步做完两件事：造出"这段对话的上下文"（仓库根目录、shell、权限），再把每个工具绑到它上面。

子 Agent 需要在这两步**中间**插进去：它要自己决定用哪个 shell（§9），还要往工具表里加一个 `spawn_agent`（§16）。所以拆开：

```python
def tool_context(
    root: Path | None = None,
    session: Session | None = None,
    shell: ShellSession | None = None,
) -> ToolContext:
    """One conversation's tool context.

    A fresh `ShellSession` per call, because its state (cwd, env) belongs to
    one conversation and not to the process.  A fresh `Session` for the same
    reason -- and its default is the restrictive one, so a caller that forgets
    to pass permissions gets an agent that can read and nothing else.

    `shell` became an argument in chapter 10.  A sub-agent is a second
    conversation that has to start where the first one is standing: the parent's
    `cd src` lives in a Python attribute and nowhere else, so a child that
    builds its own `ShellSession` silently starts at the process working
    directory.  Passing the object rather than the string on purpose -- the
    environment allowlist and the timeout are part of "where the parent is" too.
    """
    return ToolContext(
        root=(root or Path.cwd()).resolve(),
        shell=shell or ShellSession(),
        session=session or Session(),
    )


def bind_all(context: ToolContext) -> dict[str, ToolFn]:
    """Every spec in `tool_specs()`, bound to one context."""
    return {spec.name: spec.bind(context) for spec in tool_specs()}


def default_tools(
    root: Path | None = None,
    session: Session | None = None,
    shell: ShellSession | None = None,
) -> dict[str, ToolFn]:
    """Bind every spec to one repository, one shell session and one permission set."""
    return bind_all(tool_context(root, session, shell))
```

> - **`tool_context(root, session, shell)`**：造上下文。新的第三个参数 `shell`：不给就新建一个，给了就用给的。
> - **`bind_all(context)`**：把每个工具绑到一个上下文上，返回"名字 → 处理函数"的表。
> - **`default_tools(...)`**：原来的名字还在，变成前两个的组合。前面九章的测试一个都不用改。

（文件末尾的 `__all__` 里加上 `"bind_all"` 和 `"tool_context"`。）

```bash
uv run pytest
git add src/minicodex/tools.py
git commit -m "refactor(tools): building a tool context and binding tools to it are two steps"
```

---

## §6 F10-03：交给它什么——不是整段历史

最省事的做法是把父 Agent 的整段历史交给子 Agent："它什么都知道"。量一下代价。

探针造了一个"已经干了一会儿活"的父 Agent：一条系统消息、一条用户消息、读过三个文件。然后比较两种交法（不联网）：

```
$ uv run python probe_subagent.py leak
parent history             8 items
whole history to child     4473 tokens
task only                   638 tokens
ratio                         7x

what travels that the child's task does not mention:
  system        876 chars  You are a coding agent working in a user's repository.  # What you a
  user          119 chars  The retry logic in model.py must not retry a 400. Fix it, and do not
  assistant      21 chars  Reading scheduler.py.
  tool         4422 chars  """Deciding which tool calls in one turn may run at the same time.
  assistant      17 chars  Reading paths.py.
  tool         3807 chars  """Turning what the model sent into a file we can open.  Chapter 3 m
  assistant      23 chars  Reading shell_parse.py.
  tool         5892 chars  """Splitting one command string into the pieces a policy can judge.
```

（Windows 和 Linux 上数字相同。）

**7 倍**，不是清单猜的 2 倍。而且看带过去的是什么：八条里有三条是父 Agent 读过的文件的全文——子 Agent 的任务（"读 `model.py`，列出它在哪些地方抛异常"）一个字都没提到它们。

这只是一个读了三个文件的父 Agent。真的干了二十轮的，这个倍数只会更大。**F10-03 成立。**

所以交过去的是一份**契约**。新建 `src/minicodex/subagent.py`，开头：

```python
"""Running an agent inside a tool call.

A sub-agent is not a new kind of object.  It is `Agent` -- the same class the
loop has used since chapter 0 -- with a different history, a different tool
table and a smaller budget, awaited from inside a tool handler.  Nothing here
subclasses anything.

What this module owns is the *boundary*: four things cross it, and each one is
here because something measurable went wrong when it did not.

  **In: a contract, not a history.**  Handing the child the parent's history
  costs 7x the tokens (measured on an eight-item parent: 4476 against 638) and
  carries the contents of every file the parent has read into a conversation
  that never asked for them.  The child gets a task, the constraints that
  apply to it, and the shape of the answer wanted back.

  **In: state the parent never wrote down.**  The parent's working directory
  lives in a `ShellSession` attribute and its permissions live in a `Session`
  object.  A child built from scratch starts at the process cwd and at
  `read-only`, so `cd src` in the parent is invisible to the child and a
  permission the user granted on turn 3 is not inherited.  Both measured;
  neither raises.

  **Out: an outcome, not a string.**  `RunResult` already distinguishes
  `completed` / `turn_limit` / `interrupted`; `return result.final_text`
  throws that away, and two of those three produce the *same* empty string.

  **Around: a bound on everything.**  Depth, turns, wall clock, result size.
  Without a depth limit an agent that spawns itself reached **830,400 levels**
  in 59 seconds and had to be killed from outside -- and the `asyncio.wait_for`
  that should have stopped it died of the nesting first, with a `RecursionError`
  raised inside the event loop's own timer callback.

This module imports `agent.py` and `tools.py`; neither imports it.  That
direction is not an accident -- putting the `spawn_agent` handler next to the
other handlers in `tools.py`, which is where a handler obviously belongs, makes
`tools` import `subagent` import `tools`, and the package stops importing at
all.  Chapter 10 solves it by moving the tool out; interlude B is about the
case where moving it is not enough.

Deliberately not here: a sub-agent gets this project's own tools and no MCP
tools.  Chapter 9's registry belongs to one session, and a remote tool's
`Footprint` is a promise made by somebody else's code; giving a child a
resource it can touch without the parent's scheduler knowing is F08-06 with a
second process in the way.  Said out loud rather than left to be discovered.
"""

from __future__ import annotations

import asyncio
import contextlib
import functools
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Literal

from minicodex.agent import Agent, Model
from minicodex.agent_types import ToolFn
from minicodex.approval import Session
from minicodex.clip import clip
from minicodex.rollout import NULL_WRITER, RolloutWriter, SessionMeta, new_session_id, rollout_path
from minicodex.shell import ShellSession
from minicodex.tool_errors import tool_error
from minicodex.tools import ToolContext, ToolSpec, bind_all, tool_context, tool_schemas

# How deep the nesting may go.  2 means: the top-level agent may spawn, and its
# children may spawn, and *their* children may not.  Small on purpose -- every
# level multiplies the number of model calls, and the measured behaviour of the
# unbounded version is not "slow", it is "unstoppable".
MAX_DEPTH = 2

# One sub-task's own turn budget.  Smaller than the parent's, because a
# sub-task that needs twelve turns was not a sub-task.
DEFAULT_TASK_TURNS = 8

# How many model calls *every* sub-agent in one run may cost between them.
# This one was not designed; a test produced it.  With only a depth limit in
# place, a model that answers every turn with another spawn cost **72 model
# calls**: eight turns at depth 1, each spawning a depth-2 child that spends
# its own eight turns.  A depth limit bounds the depth and not the width, and
# 2 x 8 x 8 is what the width costs.  Counted from `SubAgentContext.children`,
# which was already being kept for the end-of-run listing.
DEFAULT_CHILD_TURN_BUDGET = 24

# Wall clock for one sub-task, including every tool it runs.  A number the
# parent's own timeouts do not imply: chapter 2 bounds a single command at 30s
# and nothing bounds eight turns of them.
DEFAULT_TASK_TIMEOUT = 300.0

# How much of a sub-agent's answer reaches the parent's history.  Measured
# against gpt-4o-mini: asked to explain one module with no shape requested it
# returned a median of 2331 characters; asked for three sentences, 503.  The
# instruction is worth having -- and it is not a bound.  The same instruction on
# a slightly larger task produced 603, 615, 661, 1468 and 2711 characters, so
# the ceiling is here, in code, where a number means what it says.
MAX_TASK_RESULT_CHARS = 4000

Outcome = Literal["ok", "empty", "turn_limit", "timeout", "interrupted", "depth_limit", "budget"]
```

> - 开头那段长长的 docstring 是这个模块的目录：四样东西穿过父子之间的边界，每一样都是因为不这样做时量到了问题。这一章接下来就是按它展开。
> - **导入**里值得看的一行：这个模块导入 `agent.py` 和 `tools.py`，**反过来不行**。为什么，§16。
> - **五个常量**，每个上面的注释说了它的数字是哪来的。后面用到时再讲。
> - **`Outcome`**：七种结局的名字（§14）。

契约本身：

```python
@dataclass(frozen=True)
class TaskSpec:
    """What the parent is handing down.

    Three fields, and the second and third are the ones that were measured.

    `constraints` are things that are true of the session and not of the task
    -- "the shell on this machine is broken", "do not touch the streaming
    parser".  They go into the child's *system* message rather than into its
    task text, which is not a style preference: with the constraint appended to
    the task, 3 and 4 of 5 runs violated it anyway; as a system note, 0 of 5.

    `expected_output` is what the parent wants back and how long it may be.
    Without it the child writes an essay the parent then pays for on every
    subsequent turn.
    """

    task: str
    constraints: tuple[str, ...] = ()
    expected_output: str = ""

    def instructions(self) -> str:
        """The child's system message.

        Deliberately *not* the parent's system prompt.  The child is not a
        smaller copy of the parent; it is one task with one set of rules, and
        inheriting "you are a coding agent working in a user's repository"
        plus a permissions block is how the 7x token measurement happens one
        message at a time.

        The last paragraph is the one that is easy to leave out.  A sub-agent
        has no user: chapter 5's approval prompts reach the human through the
        parent's terminal, but "shall I go on?" written as prose reaches
        nobody, and the child then waits for an answer by spending its whole
        turn budget on politeness.
        """
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

> - 三个字段：**任务**、**约束**（用户定过的、对这个任务也适用的规矩）、**想要的输出形状**。后两个的位置和写法是量出来的（§7、§8）。
> - **`instructions()`**：子 Agent 的系统消息。**故意不是父 Agent 的系统提示词**——子 Agent 不是父 Agent 的缩小版，它是一件任务加一套规矩。
>   - 第一句：你从另一个 Agent 那里接到一件独立的任务。
>   - 有约束就列出来；有输出形状就说"只交回这个"。
>   - **最后一段最容易漏**：子 Agent 没有用户。它写一句"要我继续吗？"，没有任何人会看到——然后它会把剩下的轮数全花在等一个不会来的回答上。
>     所以明说：除了派你来的那个 Agent，没有人会读你说的话，它也回答不了问题；做不了就用一句话说做不了、是什么拦住了你。

```python
@pytest.mark.asyncio
async def test_F10_03_the_child_never_sees_the_parents_conversation(tmp_path: Path) -> None:
    model = ScriptedModel(["found it"])
    ctx = context_for(tmp_path, model)
    await run_task(TaskSpec(task="count the files"), ctx)

    sent = model.sent[0]
    assert [m["role"] for m in sent] == ["system", "user"]
    assert sent[-1]["content"] == "count the files"
    # The child's system message is its own, not the parent's.
    assert "coding agent working in a user's repository" not in sent[0]["content"]


@pytest.mark.asyncio
async def test_F10_03_the_childs_own_system_note_is_short(tmp_path: Path) -> None:
    """A bound, not a preference: the whole point is that this does not grow
    into a second copy of the parent's prompt."""
    model = ScriptedModel(["ok"])
    ctx = context_for(tmp_path, model)
    await run_task(TaskSpec(task="t"), ctx)
    assert len(model.sent[0][0]["content"]) < 500
```

> - 第一个：子 Agent 收到的第一个请求里只有两条消息——系统消息和任务。系统消息里没有父 Agent 提示词里的那句话。
>   （`run_task` 和 `ctx` 在 §9 之后才完整；这里先看断言。）
> - 第二个：子 Agent 的系统消息不到 500 个字符。**这是一个上限，不是一个偏好**——它存在的全部意义，就是不长成父 Agent 提示词的第二份拷贝。

---

## §7 F10-04：规矩写在哪里，决定它算不算数

只交任务的代价，是清单上的 F10-04：用户在对话开头说过的规矩，子 Agent 不知道。

设计一个测得出来的版本。一条规矩："这台机器的 shell 是坏的，`run_shell` 会返回看起来像真的、其实是错的结果，不要用它。"
一件任务："找出 `src/minicodex` 里所有调用 `os.killpg` 的地方。"——这件事最顺手的做法恰好是用 shell 搜一下。

四种做法，每种 5 次（gpt-4o-mini）：

```
$ uv run python probe_subagent.py contract
    calls: ['run_shell({"command": "grep -r \'os.killpg\' src/minicodex"})']
    said:  'The calls to `os.killpg` are found in the following files:\n\n1. `src/minicodex/shell.py` - Line contains: `os.killpg(proc.pid, signal.SIGKILL)`\n2. `src/minicodex'
 5/5 used the broken shell, 5/5 named shell.py  --  task only
    calls: ['read_file({"path": "src/minicodex/model.py"})', 'read_file({"path": "src/minicodex/another_module.py"})', 'read_file({"path": "src/minicodex/utils.py"})', 'read_file({"path": "src/minicodex/some_other_file.py"})', 'read_file({"path": "src/minicodex/commands.py"})']
    said:  ''
 2/5 used the broken shell, 2/5 named shell.py  --  constraint in the task
    calls: ['read_file({"path": "src/minicodex"})', 'read_file({"path": "src/minicodex/__init__.py"})', 'read_file({"path": "src/minicodex/model.py"})', 'read_file({"path": "src/minicodex/agent.py"})', 'read_file({"path": "src/minicodex/utils.py"})', 'read_file({"path": "src/minicodex/another_file.py"})', 'read_file({"path": "src/minicodex/tools.py"})']
    said:  ''
 0/5 used the broken shell, 0/5 named shell.py  --  constraint as a system note
    calls: ['read_file({"path": "src/minicodex"})', 'read_file({"path": "src/minicodex/model.py"})', 'read_file({"path": "src/minicodex/utils.py"})', 'read_file({"path": "src/minicodex/setup.py"})', 'read_file({"path": "src/minicodex/__init__.py"})']
    said:  ''
 0/5 used the broken shell, 0/5 named shell.py  --  no run_shell in the table
```

（Windows，2026-10-01。每组下面的 `calls` 和 `said` 是那一组第一次运行的样子。）

| 做法 | 5 次里用了 shell 的 | 5 次里答对的 |
|---|---|---|
| 只交任务（没人告诉它规矩） | 5 | 5 |
| 规矩写在任务文字的后面 | 2 | 2 |
| 规矩写在子 Agent 的系统消息里 | 0 | 0 |
| 工具表里根本没有 `run_shell` | 0 | 0 |

三件事：

1. **不告诉它，它一定会违反**：5/5。这不是模型的错，它不知道。
2. **写在任务后面，5 次里还有 2 次违反。** 这一章最初的记录里，同一组跑过两遍，是 3/5 和 4/5。**同一句话写进系统消息：0/5**，那时和今天都是。
3. **最不舒服的一件：规矩被遵守之后，任务做不成了。** 看后两组的 `calls`：不能用 shell，就没有办法列出目录里有哪些文件，于是子 Agent 开始**猜文件名**——
   `utils.py`、`another_file.py`、`setup.py`，没有一个存在——然后什么都没答（`said: ''`）。答对的次数是 0/5。
   **用代码或规矩禁掉一条路的时候，你就欠下了"留一条能走到目的地的路"。** 这一章没有还这笔债（比如给子 Agent 一个列目录的工具），只是把它量了出来。

**F10-04 成立，而清单开的药只有放对地方才管用**：同一句话，写在任务文字的后面，和写进子 Agent 的**系统消息**，效果不一样。
所以 `TaskSpec.instructions()` 把约束放在系统消息里（§6）。

```python
@pytest.mark.asyncio
async def test_F10_04_constraints_arrive_as_a_system_note_not_in_the_task(
    tmp_path: Path,
) -> None:
    """Where the constraint goes is the measurement.

    Appended to the task text, 3 and 4 of 5 runs broke it anyway; as a system
    note, 0 of 5. This test pins the placement, which is the part code can
    guarantee -- the obedience is the model's and is measured in the probe.
    """
    model = ScriptedModel(["ok"])
    ctx = context_for(tmp_path, model)
    await run_task(TaskSpec(task="find killpg", constraints=("do not use the shell",)), ctx)

    system, user = model.sent[0]
    assert "do not use the shell" in system["content"]
    assert "do not use the shell" not in user["content"]


def test_F10_04_a_spec_with_no_constraints_still_says_there_is_no_user() -> None:
    text = TaskSpec(task="t").instructions()
    assert "cannot answer questions" in text
```

> 第一个测试钉住的是**位置**：约束出现在系统消息里，不出现在任务里。位置是代码能保证的那一半；模型听不听，是模型的事，那一半在探针里量。

---

## §8 F10-05：交回来多少

§3 里已经看到：要一句话，得到两千个字符。这些字符会留在父 Agent 的历史里，之后的**每一轮**都重发一遍。

量一下"告诉它要多短"管不管用（gpt-4o-mini，每种 5 次，交回的字符数从小到大）：

```
$ uv run python probe_subagent.py length
task only                chars: [1868, 2137, 2250, 2395, 2746]  median 2250
task + output shape      chars: [449, 471, 491, 511, 546]  median 491
bigger task + same shape chars: [0, 518, 562, 582, 622]  median 562
```

（Windows，2026-10-01。）

- 不说要多短：中位数 2250 个字符。
- 加一句"最多三句话，不要开场白、不要列表、不要代码"：491。**说了管用。**
- 同一句话，换一个稍大的任务（"列出每个模块并各用一行说明"）：今天这五次在 0 到 622 之间。**这一章最初的记录里，同一组是 603、615、661、1468、2711**——
  "最多三句话"的要求下出现过两千七百个字符。

**一句要求不是一个上限。** 它在大多数时候管用，而上限的意义就在于"大多数时候"之外的那几次。所以两样都要：

- `expected_output` 是 `spawn_agent` 的**必填**参数（§16），进子 Agent 的系统消息；
- 交回来的文字过一遍 `clip()`，上限 `MAX_TASK_RESULT_CHARS = 4000`——写在代码里，一个数字说多少就是多少。

今天那一组里还有一个 **0**：子 Agent 结束了，一个字都没说。这不是"一个很短的答案"，§14 专门处理它。

```python
def test_F10_05_a_long_answer_is_clipped_at_the_boundary() -> None:
    """The instruction is not a bound. Measured: the same "three sentences"
    instruction produced 603, 615, 661, 1468 and 2711 characters."""
    rendered = TaskResult("ok", "x" * 10_000, 3).render()
    assert len(rendered) < MAX_TASK_RESULT_CHARS + 100
    assert "characters omitted" in rendered


def test_F10_05_a_short_answer_is_returned_untouched() -> None:
    assert TaskResult("ok", "42", 1).render() == "42"


@pytest.mark.asyncio
async def test_F10_05_expected_output_reaches_the_child(tmp_path: Path) -> None:
    model = ScriptedModel(["ok"])
    ctx = context_for(tmp_path, model)
    await run_task(TaskSpec(task="t", expected_output="one line, path:line"), ctx)
    assert "one line, path:line" in model.sent[0][0]["content"]
```

> 前两个用到 `TaskResult(...).render()`，§14 讲；这里只看结论：一万个字符被截到四千出头，`42` 原样返回。

---

## §9 F10-01、F10-09：父 Agent 没写下来的东西

清单上的 F10-01 说：父 Agent 刚改的文件还没写到磁盘，子 Agent 读到旧的。

**按这个说法，它不会发生**：第 4 章的 `apply_patch` 是写完磁盘才返回的，没有"还没写下去"的状态。

但顺着这个方向找，找到了一个更糟的：**父 Agent 从来没往任何地方写的东西。**

```
$ uv run python probe_subagent.py cwd
parent: (ok)
parent: /home/qpdyl/lab/probe_step10_subagents/src
child:  /home/qpdyl/lab/probe_step10_subagents

parent session before upgrade: sandbox_mode=read-only, approval_policy=on-request
parent session after upgrade:  sandbox_mode=workspace-write, approval_policy=on-request
a child built with default_tools(root): sandbox_mode=read-only, approval_policy=on-request
```

（Linux；Windows 上结果相同，路径不同。）

两件事，都没有任何报错：

- **当前目录**。父 Agent 执行了 `cd src`，它自己 `pwd` 得到 `.../src`。照 §3 那样新造的子 Agent，`pwd` 得到的是仓库根目录。
  第 2 章讲过为什么：`cd` 不是真的改了哪个进程的目录，而是记在 `ShellSession.cwd` 这个 Python 属性里。新的工具表，新的 `ShellSession`，那个属性从头开始。
- **权限**。用户在第 3 轮同意了"可以写文件"，这件事记在 `Session` 对象里。新造的子 Agent 拿到一个新的 `Session`，回到了 `read-only`。

所以需要一个东西，把"这次运行里所有子 Agent 共用的东西"装在一起：

```python
@dataclass(frozen=True)
class SubAgentContext:
    """Everything a sub-agent needs that belongs to the run it is part of.

    The same shape as `ToolContext` and for the same reason: these travel
    together, and a call site that passes six of seven is a call site that
    compiles.

    `build_model` is a factory rather than a model because the child is shown a
    different tool list from the parent -- no `spawn_agent` at the bottom level
    -- and a chat-completions client carries its tool list.

    `children` is a list and this class is frozen, which is not a contradiction
    but is worth saying: the field cannot be reassigned, and the list is
    appended to.  It is here for the same reason `Session` is mutable -- the
    caller needs to know afterwards what happened, and threading a return value
    back out through a tool handler that must return a string is not possible.
    """

    build_model: Callable[[list[dict[str, Any]]], Model]
    root: Path
    session: Session
    parent_shell: ShellSession
    depth: int = 0
    max_depth: int = MAX_DEPTH
    max_turns: int = DEFAULT_TASK_TURNS
    # Shared by every sub-agent in the run, because `children` is one list
    # passed down by reference: `replace(ctx, depth=...)` copies the reference,
    # not the list.  In-flight ancestors are not counted -- a child is recorded
    # when it finishes -- so this undercounts by the turns of whichever
    # sub-agents are still running above.  Bounded, not exact, and said so
    # rather than implied.
    child_turn_budget: int = DEFAULT_CHILD_TURN_BUDGET
    timeout: float = DEFAULT_TASK_TIMEOUT
    sessions_dir: Path | None = None
    parent_session_id: str = ""
    # Recorded in the child's session header.  Empty by default and filled in
    # by the CLI: a transcript that does not say which model wrote it is a
    # transcript nobody can compare with another one, and the first version of
    # this printed `? | read-only` for every sub-agent in `minicodex sessions`.
    provider: str = ""
    model: str = ""
    # Where "[sub-agent ...]" lines go.  A sub-agent that leaves no trace in the
    # terminal is a minute of silence the user cannot interpret.
    announce: Callable[[str], None] | None = None
    children: list[TaskResult] = field(default_factory=list)
```

> - 和第 4 章的 `ToolContext` 是同一个想法：这些东西总是一起出现，装在一个对象里，就不会有哪个调用的地方"七样只传了六样"。
> - **`build_model`**：是一个"造模型客户端的函数"，不是一个模型客户端。因为子 Agent 看到的工具列表和父 Agent 不一样（§12），
>   而第 1 章的模型客户端是带着工具列表的。
> - **`session`**：父 Agent 的那个 `Session` **对象本身**。**`parent_shell`**：父 Agent 的 shell。
> - **`depth`、`max_depth`、`max_turns`、`child_turn_budget`、`timeout`**：各种上限（§12、§13）。
> - **`sessions_dir`、`parent_session_id`、`provider`、`model`**：子 Agent 的会话文件要用（§15）。
> - **`announce`**：往哪里打印"子 Agent 开始了 / 结束了"。一个在终端上不留任何痕迹的子 Agent，是用户没法解读的一分钟沉默。
> - **`children`**：这次运行里每个结束了的子 Agent 的结果。这个类是 `frozen` 的，而这个字段是一个会被 `append` 的列表——不矛盾：
>   冻结的是"字段不能重新赋值"，列表本身可以加东西。它为什么在这里，§12。

子 Agent 的工具表：

```python
def _say(ctx: SubAgentContext, message: str) -> None:
    if ctx.announce is not None:
        ctx.announce(message)


def child_tools(ctx: SubAgentContext) -> tuple[dict[str, ToolFn], list[dict[str, Any]]]:
    """The child's handler table and the schemas that match it, built together.

    Together because chapter 4 paid for the version where they were two lists:
    a handler with no schema is never called, and a schema with no handler
    produces chapter 0's "no tool named X", which was written for names the
    model *invented*.

    The child's shell starts where the parent's shell is now, not where the
    process started.  `cd src` in the parent and then `pwd` in a freshly built
    child returns the repository root -- measured, silent, and wrong.

    At the bottom of the allowed depth the child is not given `spawn_agent` at
    all, rather than being given it and refused.  Chapter 5 measured what
    happens when a prompt names a tool the policy will not allow: the model
    calls it (2/3) and spends a turn finding out.
    """
    shell = ShellSession(timeout=ctx.parent_shell.timeout)
    shell.cwd = ctx.parent_shell.cwd
    context = tool_context(root=ctx.root, session=ctx.session, shell=shell)

    handlers = bind_all(context)
    schemas = list(tool_schemas())

    if ctx.depth + 1 < ctx.max_depth:
        spec = spawn_spec(replace(ctx, depth=ctx.depth + 1))
        handlers[spec.name] = spec.bind(context)
        schemas.append(spec.schema())
    return handlers, schemas
```

> - **一个新的 `ShellSession`，但从父 Agent 现在站的地方开始**：`shell.cwd = ctx.parent_shell.cwd`。新的，是因为子 Agent 自己 `cd` 到哪里是它自己的事，不该影响父 Agent；
>   起点相同，是因为任务是在父 Agent 站的那个地方交出来的。
> - **同一个 `Session` 对象**（`session=ctx.session`），不是把它的字段抄一份。权限属于**人**，不属于某一段对话：之后用户再批准什么，子 Agent 也有；
>   子 Agent 里批准的，父 Agent 之后也看得见。
> - **处理函数表和工具说明一起造、一起返回**——第 4 章为"它们是两张分开的表"付过学费。
> - 最后那个 `if`：到了最底层，**根本不给** `spawn_agent`（§12）。

```python
@pytest.mark.asyncio
async def test_F10_01_a_child_starts_where_the_parent_is_standing(tmp_path: Path) -> None:
    """`cd src` lives in a Python attribute, not on disk.

    Measured with the real tools: parent `cd src`, parent `pwd` -> .../src,
    child `pwd` -> the repository root. No error anywhere.
    """
    (tmp_path / "src").mkdir()
    parent_shell = ShellSession()
    parent_shell.cwd = str(tmp_path)
    await parent_shell.run("cd src")
    assert parent_shell.cwd == str(tmp_path / "src")

    ctx = context_for(tmp_path, ScriptedModel(["done"]), shell=parent_shell)
    handlers, _schemas = child_tools(ctx)
    # The handler closes over the child's shell; the only way to ask it where
    # it is standing is to run something.
    where = await handlers["run_shell"]({"command": "pwd"})
    # Compared by tail, not by `Path.resolve()`: the shell here is msys bash,
    # which answers `/tmp/...` for a path Python calls `C:\Users\...\Temp\...`.
    # Resolving that string on the Python side produces `D:\tmp\...`, which is
    # a different, non-existent directory. The two runtimes do not share a
    # spelling for the same place -- F02-10, one more time.
    assert where.strip().replace("\\", "/").endswith("/src")


@pytest.mark.asyncio
async def test_F10_01_a_child_shares_the_permissions_the_parent_was_granted(
    tmp_path: Path,
) -> None:
    """A child built with `default_tools(root)` gets a fresh, read-only `Session`.

    Sharing the object rather than copying its fields, so that a permission
    granted after the child was built is granted to the child too -- and, more
    importantly, so that a permission granted *to* a child is visible to the
    parent afterwards rather than being lost with the child's tool table.
    """
    # The default approver is `DenyAll`, which is the point: read-only means
    # denied unless somebody says otherwise.
    session = Session(mode="read-only")
    ctx = context_for(tmp_path, ScriptedModel(["done"]), session=session)
    handlers, _ = child_tools(ctx)

    denied = await handlers["apply_patch"]({"edits": []})
    assert "Permission denied" in denied

    session.mode = "workspace-write"
    allowed = await handlers["apply_patch"]({"edits": []})
    assert "Permission denied" not in allowed
```

> - 第一个：父 shell 先 `cd src`；子 Agent 的工具表里 `run_shell` 执行 `pwd`，结果以 `/src` 结尾。
>   用"结尾"比较而不直接比较完整路径，注释里说了原因：Windows 上 shell 说的路径（`/tmp/...`）和 Python 说的路径（`C:\Users\...`）是同一个地方的两种写法。
> - 第二个：`read-only` 时子 Agent 的 `apply_patch` 被拒绝；把**同一个** `session` 改成 `workspace-write`，不重新造工具表，再调用就不被拒绝了。
>   测试里用的是默认的审批者（一律拒绝）。这个测试的第一版用了 `AllowAll()`——它会批准那次写入，于是"只读模式会拒绝"这件事根本没被测到。
>   **测试里图方便用的替身，可能正好拿掉了这个测试要测的那道门。**

### 9.1 F10-09：子 Agent 要审批，问谁

一个人，一个终端。子 Agent 的 `Session` 就是父 Agent 的 `Session`，所以子 Agent 里触发的审批，到的是同一个 `Approver`——和其他所有审批问题在同一个地方回答。

不新加任何机制，理由和第 9 章一样：同一种问题有第二种问法，用户就学会不看内容直接回答了。第 8 章加的那把锁（一次只问一个）在这里也照样管用。

```python
@pytest.mark.asyncio
async def test_F10_09_a_childs_approval_request_reaches_the_parents_approver(
    tmp_path: Path,
) -> None:
    """There is one human and one terminal. The child's `Session` is the
    parent's `Session`, so a prompt raised inside a sub-agent is answered in
    the same place as every other prompt."""
    seen: list[ApprovalRequest] = []

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

**没做的一点**：审批的提示里不说"这是一个子 Agent 要的"。没量过加上这句话有没有用，所以没加，记为欠着的。

---

## §10 `run_task`：跑一个子任务

现在可以看核心了。先整个放出来，后面四节各讲其中一段：

```python
async def run_task(spec: TaskSpec, ctx: SubAgentContext) -> TaskResult:
    """Run one sub-task to completion, or to one of the ways it can fail.

    Never raises for anything the sub-agent did -- chapter 0's rule, one level
    down.  It *does* re-raise `CancelledError`, because a Ctrl-C belongs to the
    parent's loop and chapter 7 already knows what to do with one.
    """
    if ctx.depth >= ctx.max_depth:
        return TaskResult("depth_limit", "")

    spent = sum(child.turns for child in ctx.children)
    if spent >= ctx.child_turn_budget:
        return TaskResult("budget", "")

    handlers, schemas = child_tools(ctx)
    writer = _writer(ctx)
    child = Agent(
        ctx.build_model(schemas),
        handlers,
        max_turns=ctx.max_turns,
        instructions=spec.instructions(),
        rollout=writer,
    )

    # The first line of the task, for the terminal.  `split`, not
    # `splitlines()[0]`: a task that starts with a blank line announced
    # itself as nothing at all, and an empty one raised IndexError.
    title = spec.task.strip().split("\n", 1)[0][:70]
    _say(ctx, f"[sub-agent depth {ctx.depth + 1}: {title}]")
    began = time.monotonic()
    # `ensure_future` + `shield` rather than a bare `wait_for`, and the reason
    # is measured rather than stylistic: `Agent.run` catches `CancelledError`
    # and returns a normal `RunResult` (chapter 7 -- every issued call must be
    # answered before unwinding).  A bare `wait_for` therefore *cancels the
    # child and returns its value*, raising no `TimeoutError` at all: the
    # timeout silently becomes an empty answer.  The shield makes the
    # cancellation land on this function's own await instead.
    task = asyncio.ensure_future(child.run(spec.task))
    try:
        result = await asyncio.wait_for(asyncio.shield(task), ctx.timeout)
    except TimeoutError:
        partial = await _stop(task)
        elapsed = time.monotonic() - began
        _say(ctx, f"[sub-agent depth {ctx.depth + 1}: stopped after {elapsed:.0f}s]")
        return _record(ctx, TaskResult("timeout", partial, seconds=elapsed, session_id=_id(writer)))
    except asyncio.CancelledError:
        # The shield protected the child from the parent's cancellation, so the
        # child is still running.  Stopping it here is chapter 7's rule about
        # subprocesses in a different costume: work nobody is waiting for is
        # not allowed to keep going.
        await _stop(task)
        raise
    finally:
        writer.release()

    elapsed = time.monotonic() - began
    text = result.final_text.strip()
    outcome: Outcome
    if result.stop_reason == "turn_limit":
        outcome = "turn_limit"
    elif result.stop_reason == "interrupted":
        outcome = "interrupted"
    elif not text:
        # Not "a very short answer".  An agent that stopped calling tools and
        # said nothing produced no result, and rendering that as `""` tells the
        # parent it succeeded -- the argument F06-08 makes about an empty
        # summary and F09-07 about an empty MCP result.
        outcome = "empty"
    else:
        outcome = "ok"
    _say(ctx, f"[sub-agent depth {ctx.depth + 1}: {outcome} in {result.turns_used} turn(s)]")
    return _record(ctx, TaskResult(outcome, text, result.turns_used, elapsed, _id(writer)))


def _record(ctx: SubAgentContext, result: TaskResult) -> TaskResult:
    ctx.children.append(result)
    return result


async def _stop(task: asyncio.Task[Any]) -> str:
    """Cancel a child and collect whatever it had already said.

    Awaiting the cancelled task rather than dropping it: the child answers its
    own outstanding tool calls on the way out, and abandoning it here would
    leave that unwinding to run alongside the parent's next turn.

    `return ""` at the end is not dead code, though it looks it.  `Agent.run`
    *currently* swallows `CancelledError` and returns a `RunResult`, so the
    `return` inside the `with` is the path taken today; the day that changes,
    the await raises, `suppress` catches it, and the last line runs.  Both
    paths are correct.  The test that pins the current behaviour is
    `test_F10_07_a_bare_wait_for_would_have_returned_an_empty_answer`.
    """
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        result = await task
        return str(result.final_text).strip()
    return ""


def _id(writer: RolloutWriter) -> str:
    return writer.meta.session_id if writer is not NULL_WRITER else ""
```

从上到下：

1. **两个"动工之前就拒绝"的检查**：深度（§12）、整次运行的预算（§12）。
2. **造子 Agent**：工具表（§9）、会话文件（§15）、一个 `Agent`——`max_turns` 是子任务自己的轮数上限，`instructions` 是 §6 的系统消息。
3. **在终端上说一声**，记下开始时间。
4. **让它跑，最多等 `ctx.timeout` 秒**——`ensure_future` + `shield` 那几行，和两个 `except`（§13）。
5. **`finally: writer.release()`**：不管怎么结束，释放子 Agent 会话文件的锁（§15）。
6. **判断结局**（§14），在终端上说一声，记进 `ctx.children`，返回。

> **`run_task` 不会因为子 Agent 做的任何事而抛异常**——第 0 章的规矩，往下一层。唯一的例外是 `CancelledError`：用户按了 Ctrl-C，那属于父 Agent 的循环，第 7 章已经知道怎么处理。

---

## §11 F10-02、F10-08：两个子 Agent 改同一个文件

模型在同一轮里发起两个 `spawn_agent`，两个子 Agent 各改同一个文件的一行。第 8 章的调度器会让它们同时跑吗？

第 8 章给过一个默认：**不认识的工具，当作"和一切都冲突"**。`spawn_agent` 就是一个它不认识的名字。量一下这个默认值多少钱（不联网，30 次）：

```
$ uv run python probe_subagent.py race
  0/30 lost updates  --  spawn is STATEFUL (what tools.footprint_of already returns)
    first failure: 'one\nTWO\n'
 29/30 lost updates  --  spawn declares Footprint(reads={root}) -- 'it only reads'
```

（Windows。Linux 上是 0/30 和 23/30。）

- 什么都不做：0/30。**F10-02 在动工之前就被挡住了——三章之前挡的。**
- 如果有人"好心"地给 `spawn_agent` 声明一个脚印，说"它只是读仓库"：30 次里丢 29 次（Linux 上 23 次）。两个子 Agent 都报告"改好了"，文件里只有一个的改动。

第 8 章说过"没有复现"的那条（调度器以为不相干、其实有关联），要等一个**脚印可能是错的**工具出现。第 9 章的 `readOnlyHint` 是第一次，这是第二次：
一个子 Agent 会碰什么，在它跑完之前没有人知道。

```python
def test_F10_02_a_spawn_is_stateful_without_anyone_saying_so(tmp_path: Path) -> None:
    """Chapter 8's default already covers a tool it has never heard of.

    Worth a test rather than a shrug: the measured alternative -- classifying
    a spawn as "it only reads" -- loses one of two edits 30 times out of 30.
    """
    assert footprint_of(a_call("spawn_agent", task="x"), root=tmp_path) == STATEFUL
    assert conflicts(STATEFUL, STATEFUL)


def test_F10_02_two_spawns_land_in_different_batches(tmp_path: Path) -> None:
    calls = [
        ToolCall("c1", "spawn_agent", {"task": "a"}, "{}"),
        ToolCall("c2", "spawn_agent", {"task": "b"}, "{}"),
    ]
    plan = batches(calls, lambda call: footprint_of(call, root=tmp_path))
    assert [[c.call_id for c in batch] for batch in plan] == [["c1"], ["c2"]]
```

> 什么代码都不用写，但要写测试：现在的安全靠的是"没有人给它声明脚印"，测试把这个偶然变成一个有人守着的事实。

### 11.1 这个安全的价钱，和 F10-08

"和一切都冲突"意味着：**子 Agent 一个一个跑。**

```
$ uv run python probe_subagent.py serial
two 1.0s sub-agents, spawn = STATEFUL             2.01s
two 1.0s sub-agents, spawn = no declared conflict 1.00s
```

两个各一秒的子 Agent，排着队 2.01 秒，一起跑 1.00 秒。这笔时间这一章**没有省**。要省，得先让每个子 Agent 改的东西互不相干——那是 §18 的话题。

清单上的 F10-08（父 Agent 在等子 Agent 时没法回应用户）在这个程序里**没有东西可测**：`minicodex ask` 运行期间没有任何接收用户输入的通道。

---

## §12 F10-06：无限递归——不是慢，是停不下来

### 12.1 一分多钟，一百多万层

给子 Agent 也装上 `spawn_agent`，然后让模型每一轮都调用它。这不需要恶意：一个把任务一再往下拆的模型，自然就会这样。

探针用一个每轮都 `spawn_agent` 的假模型（不联网），每 200 层打印一行，并且**给整件事套了一个 10 秒的超时**：

```python
await asyncio.wait_for(parent.run("go"), timeout=10)
```

Linux 上，75 秒后从外面强行结束时的最后几行：

```
  depth 1100200 after 74.6s
  depth 1100400 after 74.6s
  depth 1100600 after 74.6s
```

**一百一十万层。那个 10 秒的超时没有起作用**，程序是被外面杀掉的。

为什么没有"栈溢出"？因为第 8 章把每个工具调用都变成了一个独立的任务，每一层嵌套不占调用栈，Python 的递归深度限制管不到它。

那个 10 秒的超时去哪了？Windows 上跑 30 秒，把输出全部存下来，里面有这么一段：

```
Exception in callback Timeout._on_timeout()
handle: <TimerHandle when=851861.8138138 Timeout._on_timeout()>
Traceback (most recent call last):
  File "...\asyncio\events.py", line 89, in _run
    self._context.run(self._callback, *self._args)
  File "...\asyncio\timeouts.py", line 129, in _on_timeout
    self._task.cancel()
  File "...\asyncio\tasks.py", line 774, in cancel
    if child.cancel(msg=msg):
  File "...\asyncio\tasks.py", line 774, in cancel
    if child.cancel(msg=msg):
  File "...\asyncio\tasks.py", line 774, in cancel
    if child.cancel(msg=msg):
  [Previous line repeated 988 more times]
RecursionError: maximum recursion depth exceeded
```

10 秒到了，定时器去取消最外层的任务。取消要沿着那条嵌套的链一层层传下去——那条链已经十几万层深，而"传下去"是真正的函数递归。
于是 `RecursionError` **在事件循环自己的定时器回调里**抛了出来。回调死了，取消没有发生，程序继续往下长：30 秒被杀掉时到了 478,600 层。

探针里写了 `except RecursionError`，没有用：这个异常根本不在我们的代码的调用路径上。

**结论不是"递归会很慢"，而是"递归会把唯一能叫停它的那个机制一起弄坏"。** 所以深度上限不能是"跑起来之后再想办法停"，必须是在启动子 Agent **之前**就生效的一个计数——
`run_task` 开头的第一个检查，`MAX_DEPTH = 2`。

第二件事，是第 5 章的教训再来一次：**到了最底层，不要把 `spawn_agent` 交给它**（`child_tools` 结尾的那个 `if`）。
第 5 章量过：提示里出现一个当前不让用的工具，模型会去调用它，然后花一整轮才知道不行。"给了但会被拒绝"和"根本没给"，对模型是两回事。

```python
def test_F10_06_the_bottom_level_is_not_given_the_tool(tmp_path: Path) -> None:
    """Not "given it and refused". Chapter 5 measured what happens when a
    prompt names a tool the policy will not allow: the model calls it."""
    top = context_for(tmp_path, ScriptedModel(["ok"]), depth=0, max_depth=2)
    handlers, schemas = child_tools(top)
    assert "spawn_agent" in handlers
    assert any(s["function"]["name"] == "spawn_agent" for s in schemas)

    bottom = context_for(tmp_path, ScriptedModel(["ok"]), depth=1, max_depth=2)
    handlers, schemas = child_tools(bottom)
    assert "spawn_agent" not in handlers
    assert not any(s["function"]["name"] == "spawn_agent" for s in schemas)


@pytest.mark.asyncio
async def test_F10_06_a_spawn_past_the_limit_refuses_without_running_anything(
    tmp_path: Path,
) -> None:
    model = ScriptedModel(["should never be asked"])
    ctx = context_for(tmp_path, model, depth=MAX_DEPTH, max_depth=MAX_DEPTH)
    result = await run_task(TaskSpec(task="go deeper"), ctx)
    assert result.outcome == "depth_limit"
    assert model.sent == []
    assert "yourself" in result.render()
```

### 12.2 一个写错了数字的断言

有了深度上限，给它配一个测试：一个只会 `spawn_agent` 的模型，不会花太多次模型调用。当时的断言是：

```python
assert calls <= 8, calls
```

红了：

```
AssertionError: 72
assert 72 <= 8
```

**72 次模型调用，为了一件任务。** 算一下就明白：第 1 层的子 Agent 有 8 轮，每一轮启动一个第 2 层的子 Agent；第 2 层拿不到 `spawn_agent`，
但假模型还是每轮都调用它，于是每一轮得到第 0 章那句"没有这个工具"，花光自己的 8 轮。8 + 8 × 8 = 72。

**深度上限管的是深度，不是宽度。** 深度是 2，钱是按 8 × 8 花的。

不是断言写错了，是代码少了一样东西：一个**整次运行共用**的预算。

```python
DEFAULT_CHILD_TURN_BUDGET = 24

spent = sum(child.turns for child in ctx.children)
if spent >= ctx.child_turn_budget:
    return TaskResult("budget", "")
```

`children` 这个列表本来就在（§9）——记着它，是为了运行结束时能列出每个子 Agent。它怎么做到"整次运行共用"？
`child_tools` 里往下一层传的是 `replace(ctx, depth=ctx.depth + 1)`：复制一个上下文，只改深度。**`replace` 复制的是"指向那个列表"，不是列表本身**，
所以整棵子 Agent 树往里记、从里数的，是同一个列表。

改完，今天重新量一遍：

```
没有预算：model calls=72; children recorded: 9 个，每个 ('turn_limit', 8)
有预算：  model calls=32; children recorded: 4 个，每个 ('turn_limit', 8)
```

8 + 3 × 8 = 32：第 4 个第 2 层的子 Agent 被预算拒绝了。

```python
@pytest.mark.asyncio
async def test_F10_06_a_model_that_only_ever_spawns_stops_at_the_limit(tmp_path: Path) -> None:
    """The unbounded version reached 830,400 levels in 59 seconds.

    Counting model calls rather than depth, because the depth is what the code
    under test decides and the number of conversations is what it costs.
    """
    spawning = [[("c", "spawn_agent", {"task": "again", "expected_output": "x"})]]
    calls = 0

    class CountingModel(ScriptedModel):
        async def stream(self, messages: Sequence[dict[str, Any]]) -> Any:
            nonlocal calls
            calls += 1
            async for event in super().stream(messages):
                yield event

    ctx = context_for(tmp_path, CountingModel(spawning), depth=0, max_depth=2)
    result = await run_task(TaskSpec(task="start"), ctx)

    # 32, and the number is the whole point of this test. The depth-1 child
    # spends its own 8 turns; each of those turns spawns a depth-2 child that
    # spends 8 more. With the depth limit alone that was 8 + 8 * 8 = **72**
    # model calls for one task -- bounded depth, unbounded width. The shared
    # budget stops the fourth depth-2 child: 8 + 3 * 8 = 32.
    assert calls == 32, calls
    assert result.outcome == "turn_limit"
    # What ran, in the order it finished: three depth-2 children, then the
    # depth-1 child that spawned them.  The refusals after that are not in the
    # list -- nothing ran, so there is nothing to record.  (The line that
    # stood here before, `assert any(...) or calls == 32`, came straight after
    # `assert calls == 32` and so could not fail.)
    assert [(child.outcome, child.turns) for child in ctx.children] == [("turn_limit", 8)] * 4


@pytest.mark.asyncio
async def test_F10_06_the_shared_budget_refuses_without_calling_the_model(
    tmp_path: Path,
) -> None:
    """The budget is spent by the whole run, not by one branch of it."""
    model = ScriptedModel(["never asked"])
    ctx = context_for(tmp_path, model, child_turn_budget=10)
    ctx.children.append(TaskResult("ok", "earlier work", turns=10))

    result = await run_task(TaskSpec(task="one more"), ctx)
    assert result.outcome == "budget"
    assert model.sent == []
    assert "yourself" in result.render()
```

> - 第一个：断言写成 `== 32` 而不是 `<= 32`。这个数字是这段代码唯一的产出：它变了，说明有人动了预算或者轮数上限，都该有人看一眼。
>   `CountingModel` 继承 `ScriptedModel`，每次被调用就把外层的 `calls` 加一（`nonlocal calls`）。
> - 最后一行断言是改写这一章时换的。原来那一行是 `assert any(...) or calls == 32`，紧跟在 `assert calls == 32` 后面——**它不可能失败**。
>   现在断言的是实际发生的事：记下来的是 4 个子 Agent，每个都用完了 8 轮。
> - 第二个：预算已经花完时，模型一次都没被调用，交回的话里告诉父 Agent"剩下的自己做"。

这个预算有一处**不准**，写在 `SubAgentContext` 的注释里而不是藏着：子 Agent 是**跑完才记账**的，所以正在跑的那些上层子 Agent 的轮数还没算进去。
它是一个有边界的数，不是一本精确的账。

> **把你相信的那个上限写成断言，让它红一次，就是一次测量。** 这一章最贵的一个问题，不是审查出来的，也不是探针量出来的，是一个猜错了的数字逼出来的。

---

## §13 F10-07：卡住的子 Agent，和一个不抛异常的超时

子 Agent 的某个工具永远不返回，父 Agent 就永远等下去。最直接的办法，是给 `await child.run(...)` 套一个 `asyncio.wait_for`。探针里试了（不联网；子 Agent 的工具睡一小时，超时设 3 秒）：

```
$ uv run python probe_subagent.py hang
wait_for returned a RunResult after 3.0s
  stop_reason  interrupted
  final_text   ''
  child ran    True
  ToolResult   ''
```

3 秒到了，**`wait_for` 没有抛 `TimeoutError`，而是正常返回了一个结果**：`stop_reason` 是 `interrupted`，`final_text` 是空的。父 Agent 历史里那个工具结果，是一个空字符串。

**超时了，而得到的是一个空答案。** 没有任何东西说"超时"。

原因是第 7 章做的一件正确的事。那一章为了"被打断时，每个发出去的工具调用都得有一条结果"，让 `Agent.run` 自己接住 `CancelledError`，收拾好，**正常返回**。
而 `wait_for` 的工作方式是：到时间就取消里面的东西，然后看它是不是以"被取消"结束——是，才抛 `TimeoutError`。`Agent.run` 被取消后正常返回了，于是 `wait_for` 把那个返回值原样交了出来。

两个各自正确的设计，合在一起是一个不会响的闹钟。

修法是 `run_task` 里那几行：

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

> - `ensure_future`：让子 Agent 作为一个独立的任务跑起来。
> - `shield(task)`：`wait_for` 到时间要取消的，是"对这个任务的等待"，**不是任务本身**。于是 `TimeoutError` 照常抛出来。
> - 抛出来之后，子 Agent 还在跑（它被保护着）。`_stop(task)`：亲手取消它，**并且等它收拾完**——它要给自己发出去的工具调用补上结果，
>   不等的话，那些收尾会和父 Agent 的下一轮同时进行。
> - **第二个 `except` 是这个保护的另一面**：`shield` 也挡住了用户的 Ctrl-C。不加这一段，用户打断了父 Agent，子 Agent 还在后面继续跑——
>   第 2 章那个"没人管的子进程"，换了一身衣服。
> - `_stop` 结尾的 `return ""` 看起来像永远执行不到的代码，docstring 解释了它为什么留着：`Agent.run` **现在**会接住取消并正常返回，
>   哪天它不这样了，`await task` 会抛出来，`suppress` 接住，走到最后一行。两条路都是对的。

```python
@pytest.mark.asyncio
async def test_F10_07_a_hanging_child_is_stopped_and_says_so(tmp_path: Path) -> None:
    async def forever(args: dict[str, Any]) -> str:
        await asyncio.sleep(3600)
        return "never"

    model = ScriptedModel([[("c", "sleep", {})], "done"])
    ctx = context_for(tmp_path, model, timeout=0.3)
    began = time.monotonic()
    # Patching the tool table is the only way in: `child_tools` builds the real
    # one, and a sleeping real tool would mean a real subprocess.
    original = child_tools

    def patched(c: SubAgentContext) -> Any:
        handlers, schemas = original(c)
        handlers["sleep"] = forever
        return handlers, schemas

    import minicodex.subagent as subagent

    subagent.child_tools = patched  # type: ignore[assignment]
    try:
        result = await run_task(TaskSpec(task="hang"), ctx)
    finally:
        subagent.child_tools = original  # type: ignore[assignment]

    assert result.outcome == "timeout"
    assert time.monotonic() - began < 3
    rendered = result.render()
    assert "still running" in rendered
    assert "Do not report this as a finding" in rendered


@pytest.mark.asyncio
async def test_F10_07_a_bare_wait_for_would_have_returned_an_empty_answer(
    tmp_path: Path,
) -> None:
    """Why `run_task` shields the child instead of wrapping it.

    `Agent.run` catches `CancelledError` and returns a normal `RunResult`
    (chapter 7: every issued call is answered before unwinding). So
    `wait_for(child.run(...))` cancels the child and *returns its value* --
    no `TimeoutError` is raised at all, and the timeout silently becomes an
    empty answer. This test pins the behaviour that forces the design; if it
    ever changes, the shield can go.
    """

    async def forever(args: dict[str, Any]) -> str:
        await asyncio.sleep(3600)
        return "never"

    model = ScriptedModel([[("c", "sleep", {})], "done"])
    child = Agent(model, {"sleep": forever})
    result = await asyncio.wait_for(child.run("hang"), timeout=0.3)

    assert result.stop_reason == "interrupted"
    assert result.final_text == ""


@pytest.mark.asyncio
async def test_F10_07_cancelling_the_parent_does_not_leave_the_child_running(
    tmp_path: Path,
) -> None:
    """The shield protects the child from the parent's Ctrl-C too, so the
    cancellation has to be passed on by hand."""
    running = asyncio.Event()
    finished = asyncio.Event()

    async def slow(args: dict[str, Any]) -> str:
        running.set()
        try:
            await asyncio.sleep(3600)
        finally:
            finished.set()
        return "never"

    model = ScriptedModel([[("c", "sleep", {})], "done"])
    ctx = context_for(tmp_path, model, timeout=60)
    original = child_tools

    def patched(c: SubAgentContext) -> Any:
        handlers, schemas = original(c)
        handlers["sleep"] = slow
        return handlers, schemas

    import minicodex.subagent as subagent

    subagent.child_tools = patched  # type: ignore[assignment]
    try:
        task = asyncio.ensure_future(run_task(TaskSpec(task="t"), ctx))
        await running.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        subagent.child_tools = original  # type: ignore[assignment]

    assert finished.is_set(), "the child's tool was never unwound"
```

> - 第一个：子 Agent 的工具睡一小时，超时 0.3 秒。结局是 `timeout`，3 秒内返回，交回的话里说了"还在运行、已被停止"和"不要把这当作结论"。
>   往工具表里塞一个睡觉的工具，办法是临时把 `subagent.child_tools` 换成一个包了一层的版本，`finally` 里换回来。
> - 第二个**钉住的是逼出这个设计的那个行为**：直接 `wait_for(child.run(...))`，得到的是 `interrupted` 和空字符串，不是异常。
>   哪天这个行为变了，这个测试会红，`shield` 就可以拿掉了。
> - 第三个：取消父任务，子 Agent 的工具确实被收拾了（`finally` 里的 `finished.set()` 执行了）。

### 13.1 换成真的命令再试一次

上面的测试里，"卡住的工具"是一个 `asyncio.sleep`。真实的情况是一条跑了很久的 shell 命令。改写这一章时用真的试了一次：
子 Agent 用 `run_shell` 启动一个程序，它睡 4 秒后写一个文件；子任务的超时设成 1 秒。

```
Linux:
run_task returned after 1.0s: outcome='timeout' text=''
[sub-agent: still running after 1s and was stopped]
It produced no answer at all. Do not report this as a finding. Anything it had already changed on disk is still changed; check before repeating it.
marker written by the abandoned command: False

Windows:
run_task returned after 1.0s: outcome='timeout' text=''
marker written by the abandoned command: True
```

Linux 上，那个文件没有被写出来：取消一路传到了 `run_shell`，第 7 章加的那段代码杀掉了整个进程组。

**Windows 上，文件被写出来了**：子 Agent"被停止"之后 3 秒，那条命令自己跑完了。这不是这一章的新问题——第 2 章就记过：杀进程组的办法只在 POSIX 系统上有，
Windows 上那个函数什么都不做。但它在这里换了一个更难看的样子：父 Agent 被告知"它已经被停止了"，而它没有。
这一章**没有修**（修它是第 2 章那条的事）；交回的话里那句"它已经改了的东西还在，重做之前先检查"，在 Windows 上要多读一层意思。

---

## §14 F10-10：交回来的不是一个字符串，是一个结局

§3 的十五行里，子 Agent 的结果是 `result.final_text`。量一下它丢了什么：

- 子 Agent 轮数用完了：`final_text` 是 `''`。
- 子 Agent 被打断了：`final_text` 是 `''`（§13 刚看到）。
- 子 Agent 做完了：一段话。
- 子 Agent 做到一半放弃了：也是一段话，看起来和做完了的一模一样。

**三种结束方式变成一个字符串，其中两种是同一个空字符串。** 而一个空字符串在父 Agent 看来，和"成功了，没什么可说的"一模一样——第 6 章对空的总结、第 9 章对空的工具结果，说的是同一件事。

父 Agent 真的会因此答错吗？量了（gpt-4o-mini，每种 5 次：子 Agent 只给 1 轮，必然做不完）：

```
$ uv run python probe_subagent.py failure
    child returned  ["turn_limit/1: ''"]
    child called    3 tool(s)
    parent called   ['read_file({"path": "src/minicodex/shell.py"})', 'read_file({"path": "src/minicodex/paths.py"})', 'read_file({"path": "src/minicodex/policy.py"})']
 1/5 passed it on as an answer, 0/5 said it was unfinished  --  bare final_text
 0/5 passed it on as an answer, 0/5 said it was unfinished  --  outcome label first
```

不加标签：5 次里 1 次，父 Agent 把一个没做完的子任务当成答案交了出去。加了标签：0 次。**1/5 对 0/5，5 个样本分不出这两个数。**
多数时候父 Agent 的反应是对的：拿到空结果，自己把那三个文件读了一遍。

所以如实说：**作为"接口丢了信息"，F10-10 完全成立；作为"父 Agent 因此答错"，没有稳定复现。** 修它的理由是前一半。

（这个测量的第一版什么都没量到：子 Agent 的轮数给够了，它其实做完了，于是"没做完"的标签本身是假的，两组量的是同一件事。）

```python
_HEADLINE: dict[str, str] = {
    "empty": "finished without answering",
    "turn_limit": "ran out of turns after {turns}",
    "timeout": "still running after {seconds:.0f}s and was stopped",
    "interrupted": "interrupted",
    "depth_limit": "refused: sub-agents may not spawn sub-agents this deep",
    "budget": "refused: this run has spent its whole sub-agent budget",
}

# Every one of these says what to do next, not only what happened.  That is
# chapter 3's finding (F03-07) one process-shaped layer out: a message naming
# only the failure gets retried verbatim.
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

> - **`outcome`**，七种：`ok`、`empty`（结束了但什么都没说）、`turn_limit`、`timeout`、`interrupted`、`depth_limit`、`budget`。
> - **`render()`**：父 Agent 的历史里实际收到的字符串。
>   - **成功：只有答案，没有任何标头。** 每一个结果上都有的标头，是模型学会跳过的标头。
>   - **其他情况：一个带标签的块**——`[sub-agent: ...]` 说发生了什么；如果它之前说过什么，附上，并说明"这不是结论"；最后是 `_ADVICE` 里对应的那句**接下来怎么办**。
> - `_ADVICE` 的每一句都在说下一步，而不只是说发生了什么。这是第 3 章量出来的：只说"失败了"的错误信息，模型会原样重试。
>   比如 `timeout` 那句："它已经在磁盘上改了的东西还在；重做之前先检查。"

```python
@pytest.mark.asyncio
async def test_F10_10_a_child_that_ran_out_of_turns_does_not_look_finished(
    tmp_path: Path,
) -> None:
    """Measured: a one-turn child returns `''` and `stop_reason='turn_limit'`,
    and so does an interrupted one. `return result.final_text` maps three
    different endings onto one string, two of them onto the same string."""
    model = ScriptedModel([[("c", "read_file", {"path": "nope.py"})]])
    ctx = context_for(tmp_path, model, max_turns=1)
    result = await run_task(TaskSpec(task="t"), ctx)

    assert result.outcome == "turn_limit"
    rendered = result.render()
    assert "ran out of turns" in rendered
    assert "Do not report this as a finding" in rendered


@pytest.mark.asyncio
async def test_F10_10_an_empty_answer_is_not_a_success(tmp_path: Path) -> None:
    """Not "a very short answer". F06-08 made the same call about an empty
    summary and F09-07 about an empty MCP result."""
    model = ScriptedModel([""])
    ctx = context_for(tmp_path, model)
    result = await run_task(TaskSpec(task="t"), ctx)
    assert result.outcome == "empty"
    assert not result.ok
    assert "no answer at all" in result.render()


def test_F10_10_every_outcome_says_what_to_do_next() -> None:
    """The rule chapter 3 measured (F03-07), applied to a failure that is a
    whole conversation rather than one call."""
    for outcome in ("empty", "turn_limit", "timeout", "interrupted", "depth_limit", "budget"):
        rendered = TaskResult(outcome, "partial words", 2, 1.0).render()  # type: ignore[arg-type]
        assert rendered.startswith("[sub-agent:")
        assert rendered.rstrip().endswith((".", "!"))


def test_F10_10_a_successful_result_carries_no_header() -> None:
    """A header on every result is a header the model learns to skip."""
    assert not TaskResult("ok", "the answer", 2).render().startswith("[sub-agent:")
```

> - 第一个：只给 1 轮的子 Agent，结局是 `turn_limit`，交回的话里有"轮数用完了"和"不要把这当作结论"。
> - 第二个：模型什么都没说就结束了，结局是 `empty`，**不是** `ok`。§8 那次测量里的那个 0，走的就是这条路。
> - 第三个：六种不成功的结局，每一种的文字都以 `[sub-agent:` 开头、以一句完整的话结尾。
> - 第四个：成功的结果不带标头。

到这里，`subagent.py` 的核心都在了。提交：

```bash
git add src/minicodex/subagent.py tests/test_faults_ch10.py
git commit -m "feat(subagent): run an agent inside a tool call, with a contract in and an outcome out"
```

---

## §15 F10-12：两个会话文件

第 7 章给每次运行一个会话文件，记下发生的每一件事。子 Agent 的记录写到哪？

最省事的想法：写进父 Agent 的那个文件。清单猜这样会"两边的记录混在一起"。试一下（不联网）：

```
$ uv run python probe_subagent.py rollout
RolloutError: /tmp/tmp5p74m7qk/20261001T122026-96.jsonl is already open by another minicodex (pid 96). Resume it there, or delete /tmp/tmp5p74m7qk/20261001T122026-96.jsonl.lock if that process is gone.
```

**不是混在一起，是直接报错。** 第 7 章给会话文件加过一把锁：一个文件同时只能有一个写的人。那一章如实写过，这把锁"从命令行走不到"——
当时没有任何办法让两个写的人碰到同一个文件。**到这一章，它第一次被走到了，并且在第一次尝试时就拦下了一个会丢数据的设计。**

所以：每个子 Agent 一个自己的文件，文件头里记着它的父会话是谁。

```python
def _writer(ctx: SubAgentContext) -> RolloutWriter:
    """The child's own session file, linked to its parent.

    Not the parent's file.  Chapter 7 refuses two writers on one rollout with
    an `O_EXCL` lock, so the naive version does not merely interleave -- it
    raises `RolloutError` before the child's first turn.  That refusal is the
    right answer, and this is its other half: a separate file, and a `parent`
    field so a human can put the two back together.
    """
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
        parent=ctx.parent_session_id or None,
    )
    return RolloutWriter(rollout_path(meta.session_id, ctx.sessions_dir), meta)
```

> 没给 `sessions_dir` 就不记录（测试里常用）。否则造一个新的会话头：新的会话编号，同样的模型和权限，**`parent` 填父会话的编号**。

`rollout.py` 里，`SessionMeta` 多一个字段：

```python
    # The session that spawned this one, for a sub-agent (chapter 10).  Added
    # without bumping ROLLOUT_VERSION, and the difference from F07-09 is the
    # lesson: that bump was needed because the *meaning* of an existing field
    # changed, so an old file read by new code produced different bytes.  This
    # is a new optional field -- `from_json` drops keys it does not know and
    # missing keys take their default, so old files stay readable and new files
    # stay readable by old code.  Additive is not breaking; reinterpreting is.
    parent: str | None = None
```

（它的 `to_json()` 里相应地多一行 `"parent": self.parent,`。）

> 注释讲的是一件容易做错的事：**加了字段，要不要把文件格式的版本号加一？** 不用。第 7 章加版本号，是因为一个**已有**字段的含义变了——旧文件被新代码读出来会是另一个意思。
> 这里是一个新的、可以没有的字段：旧文件没有它，读出来是 `None`；新文件有它，旧代码读的时候直接忽略。**加东西不破坏兼容，改含义才破坏。**

### 15.1 一个被悄悄改掉意思的命令

真的跑一次带两个子 Agent 的任务（§17），然后看 `minicodex sessions`：

```
  20261001T122144-24948 | gpt-4o-mini | read-only | D:\...\step10_subagents  5 msg   [sub-agent of 20261001T122138-24948]
  20261001T122140-24948 | gpt-4o-mini | read-only | D:\...\step10_subagents  5 msg   [sub-agent of 20261001T122138-24948]
  20261001T122138-24948 | gpt-4o-mini | read-only | D:\...\step10_subagents  6 msg

resume with: minicodex ask '<next instruction>' --resume last
```

三个文件，**最新的两个是子 Agent 的**。而第 7 章的 `--resume last` 的意思是"接着最新的那个会话继续"。

于是 `--resume last` 悄悄变成了"接着某个子 Agent 说的最后一句话继续"——一条照常运行的命令，接上的是错的那段对话。没有任何报错，没有任何测试会红；
它是在这个列表里被看出来的。

```python
def resolve(reference: str, directory: Path = DEFAULT_DIR) -> Path:
    """Accept a path, a session id, or `last`.

    `last` skips sub-agent sessions, which is a change chapter 10 forced and
    nothing would have reported.  A run that spawns two sub-agents leaves
    three files, and the two newest are the children -- so "resume the last
    session" silently became "resume the last thing a sub-agent said", which
    reads as a working command and continues the wrong conversation.  A
    sub-agent's session is still resumable **by id**: it is only excluded from
    the guess.
    """
    if reference == "last":
        sessions = [r for r in list_sessions(directory) if r.meta.parent is None]
        if not sessions:
            raise RolloutError(f"no sessions in {directory}")
        assert sessions[0].path is not None
        return sessions[0].path
    candidate = Path(reference)
    if candidate.exists():
        return candidate
    candidate = rollout_path(reference, directory)
    if candidate.exists():
        return candidate
    raise RolloutError(f"no session {reference!r} (looked in {directory})")
```

> 改的只有一行：`last` 从"没有 `parent` 的会话"里挑。子 Agent 的会话仍然可以**按编号**恢复——它只是不参与"猜你要哪个"。

同一次查看还发现了另一件事：第一版里，子 Agent 的会话在列表里显示成 `? | read-only`——会话头里没有记是哪个服务商、哪个模型。
一份不知道是哪个模型写的记录，没法和别的记录比较。`SubAgentContext` 里的 `provider` 和 `model` 两个字段就是这么来的。

```python
@pytest.mark.asyncio
async def test_F10_12_a_child_writes_its_own_file_naming_its_parent(tmp_path: Path) -> None:
    """The parent's writer is open for the whole test, which is the point.

    The first version of this built no parent writer at all and asserted "one
    file exists, and its `parent` field says parent-1". Both of those are still
    true when the child writes *into the parent's own file* -- the mutation
    that does exactly that left this test green. A test that does not
    reconstruct the situation cannot see the bug in it.
    """
    sessions = tmp_path / "sessions"
    parent_meta = SessionMeta(session_id="parent-1", created=time.time())
    parent_writer = RolloutWriter(rollout_path("parent-1", sessions), parent_meta)
    try:
        ctx = context_for(
            tmp_path,
            ScriptedModel(["done"]),
            sessions_dir=sessions,
            parent_session_id="parent-1",
        )
        result = await run_task(TaskSpec(task="t"), ctx)

        written = sorted(p.name for p in sessions.glob("*.jsonl"))
        assert len(written) == 2, written
        assert result.session_id != "parent-1"
        loaded = read_rollout(rollout_path(result.session_id, sessions))
        assert loaded.meta.parent == "parent-1"
        # The parent's file has its header and nothing else: the child wrote
        # none of its own turns into it.
        assert len(read_rollout(parent_writer.path).items) == 0
        assert sorted(p.name for p in sessions.glob("*.lock")) == ["parent-1.jsonl.lock"], (
            "the child's lock outlived the child"
        )
    finally:
        parent_writer.release()


def test_F10_12_sharing_the_parents_file_is_refused(tmp_path: Path) -> None:
    """Chapter 7's single-writer lock is why a child cannot simply append to
    its parent's rollout: the naive version does not interleave, it raises."""
    meta = SessionMeta(session_id="s1", created=time.time())
    path = rollout_path(meta.session_id, tmp_path)
    parent = RolloutWriter(path, meta)
    try:
        with pytest.raises(RolloutError, match="already open"):
            RolloutWriter(path, meta)
    finally:
        parent.release()


def test_F10_12_an_old_session_file_still_loads(tmp_path: Path) -> None:
    """`parent` was added without bumping `ROLLOUT_VERSION`, so this has to be
    true: additive is not breaking."""
    path = tmp_path / "old.jsonl"
    path.write_text(
        json.dumps({"type": "meta", "session_id": "old", "version": 2, "cwd": "/x"}) + "\n",
        encoding="utf-8",
    )
    loaded = read_rollout(path)
    assert loaded.meta.session_id == "old"
    assert loaded.meta.parent is None


def test_F10_12_resume_last_skips_sub_agent_sessions(tmp_path: Path) -> None:
    """A run that spawns two sub-agents leaves three files, and the two newest
    are the children. `--resume last` silently became "continue the last thing
    a sub-agent said" -- a working-looking command on the wrong conversation.
    Nothing reported it; it was noticed in the output of `minicodex sessions`.
    """
    for session_id, parent in [("a", None), ("b", "a"), ("c", "a")]:
        meta = SessionMeta(session_id=session_id, created=time.time(), parent=parent)
        RolloutWriter(rollout_path(session_id, tmp_path), meta).release()

    assert resolve("last", tmp_path).stem == "a"
    # Still reachable by id: excluded from the guess, not from the program.
    assert resolve("c", tmp_path).stem == "c"
```

> - 第一个：**测试期间父会话的文件一直开着**——这是重点，docstring 记着它的来历。它的第一版根本没有造父会话的写入者，只断言"有一个文件，它的 `parent` 字段是对的"。
>   而把代码改错成"子 Agent 直接写进父会话的文件"，这两句话**仍然成立**，测试照样是绿的。是变异测试发现的（§19）。
>   **一个不把真实情形搭出来的测试，看不见那个情形里的问题。**
>   现在它断言：有两个文件；父会话的文件里没有子 Agent 的任何一条；结束后只剩父会话的那把锁。
> - 第二个：同一个文件上开第二个写入者，抛 `RolloutError`。
> - 第三个：一个没有 `parent` 字段的旧文件，照样读得出来。
> - 第四个：三个会话，后两个是第一个的子 Agent；`last` 选第一个；按编号仍然找得到子 Agent 的。

```bash
git add src/minicodex/subagent.py src/minicodex/rollout.py tests/test_faults_ch10.py
git commit -m "feat(rollout): a sub-agent writes its own session file and names its parent"
```

---

## §16 工具本身，和一个放不进去的地方

### 16.1 `spawn_agent`

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

> 和其他工具同一套规矩：参数不对就返回第 3 章那种三段式的错误（哪里不对、你发来的是什么、该怎么做），不抛异常。
> 然后把参数装成一个 `TaskSpec`，交给 `run_task`，返回 `render()` 的结果。

给模型看的说明：

```python
SPAWN_NAME = "spawn_agent"

# Constants rather than literals inside `spawn_spec`, so that the chapter 3
# snapshot test (F03-10) can pin this wording without building a whole
# `SubAgentContext` first.  A description is data; only the handler needs a
# context.
SPAWN_DESCRIPTION = (
    "Hand one self-contained piece of work to a second agent, which does it in "
    "its own conversation and returns a short answer. Use this when a step needs "
    "several tool calls of its own and the details do not need to be in this "
    "conversation -- searching a large tree, or reading several files to answer "
    "one question. Do not use it for a single tool call you could make yourself, "
    "and do not use it for work that depends on what you are doing right now: it "
    "starts with nothing but what you write here."
)

SPAWN_PARAMETERS: dict[str, Any] = {
    "type": "object",
    "required": ["task", "expected_output"],
    "properties": {
        "task": {
            "type": "string",
            "description": (
                "The whole task, written for someone who has not read this "
                "conversation. Name the files and name the goal."
            ),
        },
        "expected_output": {
            "type": "string",
            "description": (
                "What you want back and how long it may be. Example: one line per "
                "match, path:line, at most 20 lines."
            ),
        },
        "constraints": {
            "type": "array",
            "items": {
                "type": "string",
                "description": "One rule, in the user's own words where possible.",
            },
            "description": (
                "Rules from the user that apply to this task too. The sub-agent "
                "cannot see this conversation, so anything you were told and do "
                "not repeat here does not exist for it."
            ),
        },
    },
}
```

> - **说明的后半段是"什么时候不要用"**——第 3 章量出来的：只说一个工具能干什么，模型会在不该用的时候用它。
>   这里说了两种：自己一次调用就能做的事；依赖你当前正在做的事的活（"它开始时只知道你在这里写的东西"）。
> - **`task` 和 `expected_output` 是必填的**（§8）。`constraints` 的说明直接告诉模型为什么要填：
>   "子 Agent 看不到这段对话，你被告知过而没有在这里重复的任何事，对它来说不存在。"

```python
def spawn_spec(ctx: SubAgentContext) -> ToolSpec:
    """`spawn_agent`, described once, in the same shape as every other tool.

    A `ToolSpec` even though it is built here rather than in `tool_specs()`:
    the point of that class is that a description and a handler cannot drift
    apart, and that is worth as much for a tool defined in another module.
    `bind` takes the `ToolContext` every spec takes and ignores it, because a
    sub-agent's context is a different object -- the one seam left over from
    moving this handler out of `tools.py`.
    """
    return ToolSpec(
        name=SPAWN_NAME,
        description=SPAWN_DESCRIPTION,
        parameters=SPAWN_PARAMETERS,
        bind=lambda _context: functools.partial(spawn_agent, ctx),
    )


def spawn_tools(
    ctx: SubAgentContext, context: ToolContext
) -> tuple[dict[str, ToolFn], list[dict[str, Any]]]:
    """The top-level agent's spawn handler and its schema, as one pair."""
    spec = spawn_spec(ctx)
    return {spec.name: spec.bind(context)}, [spec.schema()]


def describe_children(results: Sequence[TaskResult]) -> list[str]:
    """One line per sub-agent, for the end of a run.

    F10-12 is not solved by writing separate files; it is solved by being able
    to find them.  A session id printed nowhere is a link nobody follows.
    """
    return [
        f"[sub-agent {r.session_id or '(not recorded)'}: {r.outcome}, "
        f"{r.turns} turn(s), {r.seconds:.1f}s]"
        for r in results
    ]
```

> - **`spawn_spec(ctx)`**：把名字、说明、参数、处理函数装成一个 `ToolSpec`——第 4 章的那个类，为的是说明和处理函数不会各走各的。
>   `bind` 那个 lambda 收到一个 `ToolContext` 然后**不用它**：子 Agent 要的上下文是另一个对象（`ctx`）。这是把这个工具搬出 `tools.py` 之后留下的一道缝。
> - **`spawn_tools(ctx, context)`**：最上层的 Agent 用的——处理函数和说明，成对返回。
> - **`describe_children(results)`**：运行结束时每个子 Agent 一行：会话编号、结局、轮数、秒数。**一个没有被打印出来的会话编号，是一条没有人会去点的链接。**

文件最后是 `__all__`：

```python
__all__ = [
    "DEFAULT_CHILD_TURN_BUDGET",
    "DEFAULT_TASK_TIMEOUT",
    "DEFAULT_TASK_TURNS",
    "MAX_DEPTH",
    "MAX_TASK_RESULT_CHARS",
    "SPAWN_DESCRIPTION",
    "SPAWN_NAME",
    "SPAWN_PARAMETERS",
    "SubAgentContext",
    "TaskResult",
    "TaskSpec",
    "child_tools",
    "describe_children",
    "run_task",
    "spawn_agent",
    "spawn_spec",
    "spawn_tools",
]
```

```python
@pytest.mark.asyncio
async def test_bad_arguments_are_answered_not_raised(tmp_path: Path) -> None:
    ctx = context_for(tmp_path, ScriptedModel(["ok"]))
    assert "task" in await spawn_agent(ctx, {})
    assert "list of strings" in await spawn_agent(ctx, {"task": "t", "constraints": "no"})


def test_the_schema_and_the_handler_come_from_one_object(tmp_path: Path) -> None:
    """Chapter 4's rule, for a tool declared outside `tool_specs()`."""
    ctx = context_for(tmp_path, ScriptedModel(["ok"]))
    spec = spawn_spec(ctx)
    context = tool_context(root=tmp_path, session=ctx.session)
    assert spec.schema()["function"]["name"] == spec.name
    assert callable(spec.bind(context))


def test_F10_12_every_child_is_listed_with_the_id_that_finds_its_file() -> None:
    """A session id printed nowhere is a link nobody follows."""
    lines = describe_children(
        [
            TaskResult("ok", "x", turns=2, seconds=1.5, session_id="child-1"),
            TaskResult("timeout", "", seconds=3.0),
        ]
    )
    assert lines == [
        "[sub-agent child-1: ok, 2 turn(s), 1.5s]",
        "[sub-agent (not recorded): timeout, 0 turn(s), 3.0s]",
    ]
```

### 16.2 意外：把它放在"该放的地方"，整个包就导入不了了

工具的处理函数都在 `tools.py` 里，`spawn_agent` 显然也该放那儿。第一版就是这么写的。然后：

```
ImportError: cannot import name 'default_tools' from partially initialized module 'minicodex.tools' (most likely due to a circular import)
```

`tools.py` 要用 `run_task`（在 `subagent.py` 里）；`subagent.py` 要用 `bind_all`（在 `tools.py` 里）来造子 Agent 的工具表。
A 导入 B，B 导入 A——Python 导入 A 到一半去导入 B，B 回头要 A 里一个还没定义出来的名字。

更麻烦的是那句报错**怪的是谁，取决于先导入的是谁**：从 `tools` 进来，它说 `default_tools` 导入不了；从 `subagent` 进来，它说 `run_task` 导入不了。

这一章的解法是便宜的那一种：**把这个工具往上搬**，搬到本来就同时依赖这两个模块的地方——`subagent.py` 自己定义它，`__main__.py` 把它加进工具表（§17）。
第 9 章的远程工具也是这么安排的。"往上搬"不是总有地方可搬；没地方搬的时候怎么办，是后面专门讲重构的那一章的事。

在 `tests/test_boundaries.py` 末尾把这个方向钉住：

```python
def test_tools_does_not_import_subagent() -> None:
    """The obvious place for the `spawn_agent` handler is a real cycle.

    `tools.py` would need `run_task`; `subagent.py` needs `bind_all` to build
    the child's tool table. Written that way, the package does not import at
    all -- and the message blames whichever module was loaded first:

        ImportError: cannot import name 'default_tools' from partially
        initialized module 'minicodex.tools' (most likely due to a circular
        import)

    entering through `tools`, and `cannot import name 'run_task' from
    partially initialized module 'minicodex.subagent'` entering through
    `subagent`. Chapter 10's answer is the cheap one -- the tool moves up to
    the module that already depends on both -- and interlude B is about the
    case where there is nowhere to move it to.
    """
    assert "minicodex.subagent" not in imported_modules(SRC / "tools.py")


@pytest.mark.parametrize(
    "first,second",
    [
        ("minicodex.tools", "minicodex.subagent"),
        ("minicodex.subagent", "minicodex.tools"),
    ],
)
def test_subagent_and_tools_import_in_either_order(first: str, second: str) -> None:
    result = subprocess.run(
        [sys.executable, "-c", f"import {first}; import {second}"],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
```

> - 第一个：`tools.py` 的导入里不能有 `minicodex.subagent`（`imported_modules` 是第 1 章写的帮手，读源码找出所有 import）。
> - 第二个：各开一个新的 Python 进程，按两种顺序导入这两个模块，都得成功。`@pytest.mark.parametrize` 让同一个测试用两组参数各跑一次。

### 16.3 第 3 章的快照漏了它

第 3 章有一个测试，把**每个工具的每一句说明**和一份存下来的文字逐字比较——改了任何一句说明，都得有人看一眼。
它是遍历 `TOOL_SCHEMAS` 来收集说明的，而 `spawn_agent` 不在那个列表里（它在 `subagent.py`）。**一个模型看得见、快照看不见的工具，正是那个测试要堵的那种缺口。**

所以在 `tests/test_schemas.py` 里手工加上。开头多一行导入：`from minicodex.subagent import SPAWN_DESCRIPTION, SPAWN_NAME, SPAWN_PARAMETERS`。
期望的文字多五条：

```python
    "spawn_agent": (
        "Hand one self-contained piece of work to a second agent, which does it in "
        "its own conversation and returns a short answer. Use this when a step needs "
        "several tool calls of its own and the details do not need to be in this "
        "conversation -- searching a large tree, or reading several files to answer "
        "one question. Do not use it for a single tool call you could make yourself, "
        "and do not use it for work that depends on what you are doing right now: it "
        "starts with nothing but what you write here."
    ),
    "spawn_agent.task": (
        "The whole task, written for someone who has not read this "
        "conversation. Name the files and name the goal."
    ),
    "spawn_agent.expected_output": (
        "What you want back and how long it may be. Example: one line per "
        "match, path:line, at most 20 lines."
    ),
    "spawn_agent.constraints": (
        "Rules from the user that apply to this task too. The sub-agent "
        "cannot see this conversation, so anything you were told and do "
        "not repeat here does not exist for it."
    ),
    "spawn_agent.constraints[]": "One rule, in the user's own words where possible.",
```

收集说明的函数里，遍历的列表多一项：

```python
    # `spawn_agent` lives in `subagent.py` rather than in `tool_specs()` --
    # putting its handler next to the others is a circular import -- so it has
    # to be added here by hand.  A tool the model is shown and this snapshot is
    # not is exactly the gap F03-10 exists to close, and the second table is
    # how it happens.
    schemas = [
        *TOOL_SCHEMAS,
        {
            "type": "function",
            "function": {
                "name": SPAWN_NAME,
                "description": SPAWN_DESCRIPTION,
                "parameters": SPAWN_PARAMETERS,
            },
        },
    ]
    for tool in schemas:
        fn = tool["function"]
        found[fn["name"]] = fn["description"]
        for param, spec in fn["parameters"]["properties"].items():
            walk(f"{fn['name']}.{param}", spec)
    return found
```

文件最后那个测试，遍历的列表也同样加上了它：

```python
def test_every_required_parameter_exists_in_properties() -> None:
    for tool in [*TOOL_SCHEMAS, {"function": {"name": SPAWN_NAME, "parameters": SPAWN_PARAMETERS}}]:
        params = tool["function"]["parameters"]
        for name in params.get("required", []):
            assert name in params["properties"], f"{tool['function']['name']}: {name}"
```

`SPAWN_NAME` 这几样是模块里的**常量**，而不是写在 `spawn_spec` 里面，就是为了这个测试：一句说明是数据，不需要先造一整个 `SubAgentContext` 才能读到它。

```bash
git add src/minicodex/subagent.py tests/test_faults_ch10.py tests/test_boundaries.py tests/test_schemas.py
git commit -m "feat(subagent): the spawn_agent tool, described once, defined above tools.py"
```

---

## §17 在命令行里打开它

`__main__.py` 的改动。开头的导入：多了 `from typing import Any`、`from minicodex.subagent import SubAgentContext, describe_children, spawn_tools`，
从 `tools` 导入的换成了 `TOOL_SCHEMAS, bind_all, footprint_of, tool_context`。

`_ask()` 里，开头：

```python
    def make_model(tools: list[dict[str, Any]]) -> ChatCompletionsModel:
        """One client per tool list.

        A sub-agent is shown a different set of tools from its parent, and a
        chat-completions client carries its tool list, so "the model" is not
        one object in a program that has sub-agents.  A factory rather than a
        `replace()` on the parent's client, because the child must not share
        the list object the registry mutates (chapter 9).
        """
        return ChatCompletionsModel(
            base_url=base_url or default_url,
            model=model or default_model,
            # Read from the environment, never from a flag: a key in argv shows
            # up in shell history and in `ps`.
            api_key=os.environ.get("OPENAI_API_KEY") if provider == "openai" else None,
            tools=tools,
        )

    root = Path.cwd().resolve()
    context = tool_context(root=root, session=session)
    current = SessionMeta(
        session_id=new_session_id(),
        created=time.time(),
        cwd=os.getcwd(),
        provider=provider,
        model=model or default_model,
        sandbox_mode=session.mode,
        approval_policy=session.policy,
    )
    sub_ctx = SubAgentContext(
        build_model=make_model,
        root=root,
        session=session,
        # The parent's own shell, so a child starts in the directory the parent
        # is standing in rather than the one the process started in.
        parent_shell=context.shell,
        sessions_dir=session_dir,
        parent_session_id=current.session_id,
        provider=provider,
        model=model or default_model,
        announce=print,
    )
    spawn_handlers, spawn_schemas = spawn_tools(sub_ctx, context)
```

> - **`make_model(tools)`**：给一个工具列表，造一个模型客户端。父 Agent 和每个子 Agent 各要一个，因为它们看到的工具不一样。
>   不直接复制父 Agent 的客户端，是因为子 Agent **不能**共用第 9 章那个会被注册表修改的列表对象。
> - **`context = tool_context(...)`**：父 Agent 的工具上下文——§5 拆出来的第一步。
> - **`current`**：这次会话的文件头，从后面挪到了前面，因为子 Agent 需要知道父会话的编号。
> - **`sub_ctx`**：这次运行唯一的一个 `SubAgentContext`。`parent_shell=context.shell`——父 Agent 自己的那个 shell；`announce=print`。
> - **`spawn_tools(sub_ctx, context)`**：拿到 `spawn_agent` 的处理函数和说明。

往下，四处各改一行：

```python
    registry = McpRegistry(local=[*TOOL_SCHEMAS, *spawn_schemas])
    ...
    llm = make_model(registry.visible)
    ...
    handlers = {**bind_all(context), **spawn_handlers, **registry.handlers()}
    ...
    for line in describe_children(sub_ctx.children):
        print(line)
```

> 说明加进给模型看的列表；模型客户端用 `make_model` 造；处理函数加进循环的表；结束时列出每个子 Agent。

`_list_sessions()` 里，给子 Agent 的会话加一个标记：

```python
        # A sub-agent's file is only findable if something prints the link.
        if rollout.meta.parent:
            flags.append(f"sub-agent of {rollout.meta.parent}")
```

### 17.1 真的跑一次

```
$ uv run minicodex ask "Use spawn_agent twice, once per file, to find out what src/minicodex/clip.py and src/minicodex/paths.py each do. Then answer in two sentences, one per file." --provider openai --yes
[sub-agent depth 1: Read the src/minicodex/clip.py file and summarize its functionality.]
[sub-agent depth 1: ok in 2 turn(s)]
[sub-agent depth 1: Read the src/minicodex/paths.py file and summarize its functionality.]
[sub-agent depth 1: ok in 2 turn(s)]
The `src/minicodex/clip.py` file defines a function `clip` that truncates a given text to a specified character limit, retaining the first half and the last half of the text while indicating how many characters were omitted. The `src/minicodex/paths.py` file contains functionality for resolving file paths in a specified directory, ensuring that the paths are valid, exist within the repository, and are not directories; if a path is invalid, it provides user-friendly error messages, including potential suggestions for correction.

[gpt-4o-mini | completed after 2 turn(s)]
[sandbox_mode=read-only, approval_policy=on-request]
[tokens: x0.76 from 2 observation(s)]
[transcript: .minicodex\recordings\session-1790828498.jsonl]
[sub-agent 20261001T122140-24948: ok, 2 turn(s), 3.9s]
[sub-agent 20261001T122144-24948: ok, 2 turn(s), 3.0s]
[session: .minicodex\sessions\20261001T122138-24948.jsonl  (resume with: minicodex ask ... --resume last)]
```

（Windows，2026-10-01，gpt-4o-mini。）两个子 Agent 一个接一个地跑（§11），各用了 2 轮；结束时各有一行，带着能找到它的会话文件的编号。

### 17.2 意外：这六行，没有一个测试看过

这一章的每个测试都**自己造**一个 `SubAgentContext`。程序里真正造它的地方只有一处，就是上面那段——没有任何测试看过它。

第 8 章发现过"调度器写对了、测过了、命令行里没接上也没人知道"，第 9 章发现过同样的一行。改写这一章时把同一个问题问了第三遍，用变异的办法：
把 `__main__.py` 里的一行改错，跑全部测试。

```
cli: spawn_agent has no handler                                     0
cli: the model is not shown spawn_agent                             0
cli: the child gets a shell of its own, not the parent's            0
cli: sub-agent sessions are not listed at the end                   0
cli: `sessions` does not say which are sub-agents                   0
cli: children are not recorded to disk                              0
cli: children do not name their parent                              0
```

七种改错的办法，红了的测试数都是 **0**。其中任何一条成真，§9 到 §15 的每一样东西都还是对的、都还是测过的——只是在真的程序里没有生效。

```python
def test_the_cli_wires_a_sub_agent_to_the_run_it_belongs_to(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Everything above builds its own `SubAgentContext`. The program builds
    exactly one, in `__main__.py`, and none of these tests looked at it.

    Six lines there could each be wrong with every test in this file green:
    the handler missing from the loop's table, the schema missing from the
    model's list, a fresh shell instead of the parent's, no session directory
    (so no child is ever recorded), no parent id (so no child can be traced
    back), and the end-of-run listing not printed. Chapter 8 and chapter 9
    each found one line of this kind; this is the same question asked a
    third time, and it found six.
    """
    import minicodex.__main__ as cli
    from minicodex.agent import RunResult
    from minicodex.history import History

    seen: dict[str, Any] = {}
    real_spawn_tools = cli.spawn_tools

    def spy_spawn_tools(ctx: SubAgentContext, context: Any) -> Any:
        seen["ctx"], seen["context"] = ctx, context
        return real_spawn_tools(ctx, context)

    class SpyAgent(Agent):
        def __init__(self, llm: Any, handlers: Any, **kwargs: Any) -> None:
            seen["llm"], seen["handlers"], seen["rollout"] = llm, handlers, kwargs["rollout"]
            super().__init__(llm, handlers, **kwargs)

        async def run(self, user_message: str) -> RunResult:
            # Stand in for one sub-agent having finished during the run.
            seen["ctx"].children.append(TaskResult("ok", "x", 2, 1.5, "child-1"))
            return RunResult("ok", "completed", 1, History())

    sessions = tmp_path / "sessions"
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, "spawn_tools", spy_spawn_tools)
    monkeypatch.setattr(cli, "Agent", SpyAgent)

    assert cli.main(["ask", "anything", "--session-dir", str(sessions)]) == 0

    # The model is shown the tool, and the loop can run it.
    assert "spawn_agent" in seen["handlers"]
    assert any(tool["function"]["name"] == "spawn_agent" for tool in seen["llm"].tools)
    # A child starts where the parent's shell is, under the parent's permissions.
    ctx, context = seen["ctx"], seen["context"]
    assert ctx.parent_shell is context.shell
    assert ctx.session is context.session
    # Its session file goes next to the parent's, and names the parent.
    assert ctx.sessions_dir == sessions
    assert ctx.parent_session_id == seen["rollout"].meta.session_id
    # And the run ends by saying where to find it.
    assert "[sub-agent child-1: ok, 2 turn(s), 1.5s]" in capsys.readouterr().out


def test_the_sessions_listing_says_which_ones_are_sub_agents(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The listing is how `--resume last` picking a child was noticed at all."""
    import minicodex.__main__ as cli

    for session_id, parent in [("a", None), ("b", "a")]:
        meta = SessionMeta(session_id=session_id, created=time.time(), parent=parent)
        RolloutWriter(rollout_path(session_id, tmp_path), meta).release()

    assert cli.main(["sessions", "--session-dir", str(tmp_path)]) == 0

    lines = capsys.readouterr().out.splitlines()
    (child,) = [line for line in lines if line.strip().startswith("b ")]
    (parent_line,) = [line for line in lines if line.strip().startswith("a ")]
    assert "sub-agent of a" in child
    assert "sub-agent of" not in parent_line
```

> - 第一个真的调用 `main()`。`spy_spawn_tools` 包住 `spawn_tools`，记下程序造的那个 `SubAgentContext`；`SpyAgent` 记下交给 Agent 的模型客户端、处理函数表和会话文件，
>   `run` 换成"假装有一个子 Agent 跑完了"然后直接返回。`capsys` 是 pytest 提供的，用来读程序打印了什么。
>   断言逐条对应上面那张表：有处理函数、模型看得到；`parent_shell` 和 `session` **就是**（`is`）父 Agent 的那两个对象；会话目录和父会话编号都对；结尾打印了那一行。
> - 第二个：造一个父会话和一个子会话，调用 `minicodex sessions`，子会话那一行有 `sub-agent of a`，父会话那一行没有。

```bash
git add src/minicodex/__main__.py tests/test_faults_ch10.py
git commit -m "feat(cli): give the model spawn_agent, and pin the wiring that makes it real"
```

---

## §18 没做的两件事

### 18.1 F10-11：给每个子 Agent 一份独立的工作目录

§11 的代价是子 Agent 只能排队。清单给的办法是 git 的 worktree：每个子 Agent 在自己的一份目录里改，互不干扰，最后合并。用真的 git 做一次（不联网）：

```
$ uv run python probe_subagent.py worktree
two worktrees created in 19ms
disk: 28,532 bytes

merge a: rc=0 Updating 46bf320..cecca4b
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

（Linux；Windows 上是 161 毫秒、28,698 字节，其余相同。）

建两份工作目录几乎不花钱。两个子 Agent 各改了同一个函数，合并第二个时：一个普通的冲突，文件里留着 `<<<<<<<` 标记。

**没有做，原因不是成本。** 隔离把"后写的悄悄覆盖先写的"换成了"文件里有冲突标记，得有人来解"。而这个程序里**没有这个人**：
父 Agent 不知道两个子 Agent 各自原本想怎样；用户不在场；把一个带冲突标记的文件交回给模型去改，正是第 4 章量过的、模型最容易悄悄改坏的情形。

### 18.2 意外：子 Agent 的对话，没有第 6 章的保护

改写这一章时重跑 §7 的测量，第一次，整段测量死在了半路：

```
minicodex.model.ModelHTTPError: HTTP 400 from https://api.openai.com/v1/chat/completions: {
  "error": {
    "message": "This model's maximum context length is 128000 tokens. However, your messages resulted in 149154 tokens (148761 in the messages, 393 in the functions). Please reduce the length of the messages or functions.",
    "type": "invalid_request_error",
    "param": "messages",
    "code": "context_length_exceeded"
  }
}
```

一个被禁止用 shell 的子 Agent，为了找那个函数调用，一个接一个地读文件——读到它自己的对话超过了模型的窗口。

第 6 章花了一整章防的就是这件事。它为什么在这里不起作用？看 `run_task` 里造子 Agent 的那一行：

```python
    child = Agent(
        ctx.build_model(schemas),
        handlers,
        max_turns=ctx.max_turns,
        instructions=spec.instructions(),
        rollout=writer,
    )
```

五个参数。而 `__main__.py` 里造父 Agent 的那一处，还传了窗口大小、总结用的函数、调度器的脚印函数、录制器。**这些，子 Agent 一样都没有**：
没有压缩，没有并行，没有录制。`subagent.py` 单独读，每一行都是对的；它只是和三个模块之外的另一处"造 Agent 的地方"不一致。

**这一章没有修它**，只做了两件事：探针现在把"读超了窗口"当成一种结果来数，而不是死掉（§19）；这一条如实记在这里。
同一个东西在两个地方各造一遍、各传各的参数——这不是在这里补一个参数就完事的，它正是下一章要收拾的那种问题。

---

## §19 逐条验证

### 19.1 整份探针

```python
"""What chapter 10 measured, and how.

Run one section at a time:

    uv run python probe_subagent.py naive       # the fifteen-line version, real API
    uv run python probe_subagent.py leak        # F10-03, no network
    uv run python probe_subagent.py cwd         # F10-01, no network
    uv run python probe_subagent.py race        # F10-02, no network, 30 trials
    uv run python probe_subagent.py depth       # F10-06, no network
    uv run python probe_subagent.py hang        # F10-07, no network
    uv run python probe_subagent.py rollout     # F10-12, no network
    uv run python probe_subagent.py serial      # F10-08, no network
    uv run python probe_subagent.py worktree    # F10-11, real git, no network
    uv run python probe_subagent.py contract    # F10-04, real API
    uv run python probe_subagent.py length      # F10-05, real API
    uv run python probe_subagent.py failure     # F10-10, real API

Anything with "real API" costs money and needs OPENAI_API_KEY.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import tempfile
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import httpx

from minicodex import system_prompt
from minicodex.agent import Agent
from minicodex.agent_types import ToolCall
from minicodex.approval import AllowAll, Session, permissions_block, request_upgrade
from minicodex.compaction import Sizer
from minicodex.history import History, ToolResult
from minicodex.model import (
    OPENAI_BASE_URL,
    ChatCompletionsModel,
    Completed,
    ModelHTTPError,
    TextDelta,
    ToolCallDelta,
)
from minicodex.rollout import RolloutError, RolloutWriter, SessionMeta, new_session_id, rollout_path
from minicodex.scheduler import STATEFUL, Footprint
from minicodex.tools import TOOL_SCHEMAS, default_tools, footprint_of

MODEL = "gpt-4o-mini"
ROOT = Path(__file__).resolve().parent
TRIALS = 30


def _key() -> str:
    key = os.environ.get("OPENAI_API_KEY")
    if not key:
        print("OPENAI_API_KEY is not set; this section needs it.", file=sys.stderr)
        raise SystemExit(1)
    return key


async def _retry(make: Any, attempts: int = 6) -> Any:
    """Run one sample, surviving a dropped connection or a rate limit.

    Not a fix for anything -- chapter 12 is where retries are designed.  This
    is here because a probe that dies on request 40 of 60 wastes the first 39:
    `httpx.ConnectError` and `RemoteProtocolError` both happened while these
    numbers were being collected, and so did an HTTP 429 that threw away a
    whole section.

    `make` is called again from scratch on every attempt, so it must rebuild
    anything a failed attempt may have half-filled -- see `_fresh`.
    """
    for attempt in range(attempts):
        try:
            return await make()
        except (httpx.ConnectError, httpx.RemoteProtocolError, httpx.ReadError) as exc:
            if attempt == attempts - 1:
                raise
            print(f"    (retrying after {type(exc).__name__})", file=sys.stderr)
            await asyncio.sleep(2 * (attempt + 1))
        except ModelHTTPError as exc:
            if "HTTP 429" not in str(exc) or attempt == attempts - 1:
                raise
            print("    (rate limited; waiting 30s)", file=sys.stderr)
            await asyncio.sleep(30)


def _fresh(agent: Agent, task: str, *logs: list[str]) -> Any:
    """One attempt at one sample, with the call logs emptied first.

    A retried sample must not keep the tool calls of the attempt that failed:
    those logs are what the section counts.
    """

    async def attempt() -> Any:
        for log in logs:
            log.clear()
        return await agent.run(task)

    return attempt


def _model(tools: list[dict] | None = None) -> ChatCompletionsModel:
    return ChatCompletionsModel(
        base_url=OPENAI_BASE_URL,
        model=MODEL,
        api_key=_key(),
        tools=tools if tools is not None else list(TOOL_SCHEMAS),
    )


# -- the fifteen-line version ------------------------------------------------

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


async def naive() -> None:
    """The whole idea, with nothing defended."""
    session = Session(mode="read-only", approver=AllowAll())

    async def spawn_agent(args: dict) -> str:
        child = Agent(_model(), default_tools(root=ROOT, session=session), max_turns=6)
        result = await child.run(args["task"])
        return result.final_text

    tools = {**default_tools(root=ROOT, session=session), "spawn_agent": spawn_agent}
    parent = Agent(_model([*TOOL_SCHEMAS, SPAWN_SCHEMA]), tools, max_turns=6)
    result = await parent.run(
        "Use spawn_agent twice, once per file, to find out what "
        "src/minicodex/scheduler.py and src/minicodex/paths.py each do. "
        "Then answer in two sentences, one per file."
    )
    print(result.final_text)
    print(f"\n[{result.stop_reason} after {result.turns_used} turn(s)]")
    for item in result.history.items:
        body = getattr(item, "text", None) or getattr(item, "content", None) or ""
        print(f"  {type(item).__name__:18} {len(str(body))} chars")


# -- F10-03: what a full history costs, and what is in it --------------------


def _parent_history() -> History:
    """A parent five turns in, shaped like a real one.

    The system note is what `__main__` builds: the system prompt plus the
    permission block.  The tool results are real file contents, because the
    thing being measured is how big a history gets once an agent has read
    anything at all.
    """
    session = Session(mode="workspace-write", approver=AllowAll())
    history = History()
    history.add_system_note(
        f"{system_prompt().rstrip()}\n\n{permissions_block(session, can_request=True)}"
    )
    history.add_user(
        "The retry logic in model.py must not retry a 400. Fix it, and do not "
        "touch the streaming parser while you are in there."
    )
    for index, name in enumerate(["scheduler.py", "paths.py", "shell_parse.py"], 1):
        call = ToolCall(f"call_{index}", "read_file", {"path": f"src/minicodex/{name}"}, "{}")
        history.add_assistant(f"Reading {name}.", (call,))
        history.add_tool_result(call.call_id, (ROOT / "src" / "minicodex" / name).read_text())
    return history


def leak() -> None:
    sizer = Sizer(tools=tuple(TOOL_SCHEMAS))
    parent = _parent_history()

    task = "Read src/minicodex/model.py and list every place it raises."
    everything = [*parent.to_wire("chat_completions"), {"role": "user", "content": task}]
    contract = [{"role": "user", "content": task}]

    print(f"parent history           {len(parent.items):>3} items")
    print(f"whole history to child   {sizer.messages(everything):>6} tokens")
    print(f"task only                {sizer.messages(contract):>6} tokens")
    print(
        f"ratio                    {sizer.messages(everything) / sizer.messages(contract):>6.0f}x"
    )
    print("\nwhat travels that the child's task does not mention:")
    for message in parent.to_wire("chat_completions"):
        content = str(message.get("content") or "")
        head = content.replace("\n", " ")[:68]
        print(f"  {message['role']:<10} {len(content):>6} chars  {head}")


# -- F10-01: state the parent never wrote down -------------------------------


async def cwd() -> None:
    session = Session(mode="workspace-write", approver=AllowAll())
    parent_tools = default_tools(root=ROOT, session=session)

    print("parent:", (await parent_tools["run_shell"]({"command": "cd src"})).strip() or "(ok)")
    print("parent:", (await parent_tools["run_shell"]({"command": "pwd"})).strip())

    child_tools = default_tools(root=ROOT, session=session)
    print("child: ", (await child_tools["run_shell"]({"command": "pwd"})).strip())

    parent_session = Session(mode="read-only", approver=AllowAll())
    print("\nparent session before upgrade:", parent_session.describe())
    await request_upgrade(parent_session, needs="write-files", why="the task edits files")
    print("parent session after upgrade: ", parent_session.describe())
    print("a child built with default_tools(root):", Session().describe())


# -- a model that does exactly what it is told -------------------------------


class ScriptedModel:
    """Replays a fixed list of turns. Same shape as chapters 7 and 8 use."""

    def __init__(self, turns: Sequence[Any]) -> None:
        self.turns = list(turns)
        self.calls = 0

    async def stream(self, messages: Sequence[dict[str, Any]]) -> Any:
        self.calls += 1
        turn = self.turns[min(self.calls - 1, len(self.turns) - 1)]
        if isinstance(turn, str):
            yield TextDelta(turn)
        else:
            for index, (call_id, name, arguments) in enumerate(turn):
                yield ToolCallDelta(
                    call_id=call_id, index=index, name=name, arguments=json.dumps(arguments)
                )
        yield Completed("stop")


def _spawn_schema() -> list[dict[str, Any]]:
    return [*TOOL_SCHEMAS, SPAWN_SCHEMA]


# -- F10-02: two children, one file ------------------------------------------


async def race() -> None:
    """Does chapter 8's scheduler already cover a tool it has never heard of?"""
    for label, spawn_footprint in [
        ("spawn is STATEFUL (what tools.footprint_of already returns)", STATEFUL),
        ("spawn declares Footprint(reads={root}) -- 'it only reads'", None),
    ]:
        lost = 0
        for trial in range(TRIALS):
            with tempfile.TemporaryDirectory() as tmp:
                # Set up through a thread: ASYNC240 rejects blocking filesystem
                # calls inside `async def`, and this probe is the one place a
                # scratch directory is built there.  The rule is right even
                # here -- a `resolve()` on a network path blocks the loop for
                # everything, including the two children this section is timing.
                root = await asyncio.to_thread(lambda: Path(tmp).resolve())
                await asyncio.to_thread(
                    (root / "target.py").write_text, "one\ntwo\n", encoding="utf-8"
                )
                session = Session(mode="workspace-write", approver=AllowAll())

                async def child(
                    args: dict[str, Any], root: Path = root, session: Session = session
                ):
                    word = args["task"]
                    model = ScriptedModel(
                        [
                            [
                                (
                                    "c1",
                                    "apply_patch",
                                    {
                                        "edits": [
                                            {
                                                "path": "target.py",
                                                "old_text": word,
                                                "new_text": word.upper(),
                                            }
                                        ]
                                    },
                                )
                            ],
                            "done",
                        ]
                    )
                    agent = Agent(model, default_tools(root=root, session=session))
                    return (await agent.run(word)).final_text

                def footprint(
                    call: ToolCall,
                    root: Path = root,
                    declared: Footprint | None = spawn_footprint,
                ) -> Footprint:
                    if call.name == "spawn_agent":
                        if declared is not None:
                            return declared
                        return Footprint(reads=frozenset({str(root)}))
                    return footprint_of(call, root=root)

                parent_model = ScriptedModel(
                    [
                        [
                            ("p1", "spawn_agent", {"task": "one"}),
                            ("p2", "spawn_agent", {"task": "two"}),
                        ],
                        "both done",
                    ]
                )
                tools = {**default_tools(root=root, session=session), "spawn_agent": child}
                parent = Agent(parent_model, tools, footprint_of=footprint)
                await parent.run("edit both")

                text = (root / "target.py").read_text(encoding="utf-8")
                if text != "ONE\nTWO\n":
                    lost += 1
                    if trial == 0:
                        print(f"    first failure: {text!r}")
        print(f"{lost:>3}/{TRIALS} lost updates  --  {label}")


# -- F10-06: a child that spawns a child that spawns a child -----------------


async def depth() -> None:
    reached = 0

    begin = time.monotonic()

    async def spawn(args: dict[str, Any]) -> str:
        nonlocal reached
        reached += 1
        if reached % 200 == 0:
            print(f"  depth {reached} after {time.monotonic() - begin:.1f}s")
        model = ScriptedModel([[("c", "spawn_agent", {"task": "again"})], "done"])
        agent = Agent(model, {"spawn_agent": spawn})
        return (await agent.run(args["task"])).final_text

    model = ScriptedModel([[("p", "spawn_agent", {"task": "go"})], "done"])
    parent = Agent(model, {"spawn_agent": spawn})
    try:
        await asyncio.wait_for(parent.run("go"), timeout=10)
    except TimeoutError:
        print(f"wait_for(10s) fired; reached depth {reached}")
        return
    except RecursionError:
        print(f"RecursionError reached my except clause at depth {reached}")
        return
    print(f"finished on its own at depth {reached}")


# -- F10-07: a child that never comes back -----------------------------------


async def hang() -> None:
    started = asyncio.Event()

    async def sleeper(args: dict[str, Any]) -> str:
        started.set()
        await asyncio.sleep(3600)
        return "never"

    async def spawn(args: dict[str, Any]) -> str:
        model = ScriptedModel([[("c", "sleep", {})], "done"])
        agent = Agent(model, {"sleep": sleeper})
        return (await agent.run(args["task"])).final_text

    model = ScriptedModel([[("p", "spawn_agent", {"task": "go"})], "done"])
    parent = Agent(model, {"spawn_agent": spawn})
    begin = time.monotonic()
    try:
        result = await asyncio.wait_for(parent.run("go"), timeout=3)
    except TimeoutError:
        print(f"TimeoutError after {time.monotonic() - begin:.1f}s")
        return
    print(f"wait_for returned a RunResult after {time.monotonic() - begin:.1f}s")
    print(f"  stop_reason  {result.stop_reason}")
    print(f"  final_text   {result.final_text!r}")
    print(f"  child ran    {started.is_set()}")
    for item in result.history.items:
        if isinstance(item, ToolResult):
            print(f"  ToolResult   {item.content!r}")


# -- F10-12: one file, two agents --------------------------------------------


def rollout_probe() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        directory = Path(tmp)
        meta = SessionMeta(session_id=new_session_id(), created=time.time())
        parent = RolloutWriter(rollout_path(meta.session_id, directory), meta)
        try:
            RolloutWriter(rollout_path(meta.session_id, directory), meta)
        except RolloutError as exc:
            print(f"RolloutError: {exc}")
        else:
            print("no error: two writers on one file")
        finally:
            parent.release()


# -- F10-08: what serialising sub-agents costs -------------------------------


async def serial() -> None:
    async def slow(args: dict[str, Any]) -> str:
        await asyncio.sleep(1.0)
        return "ok"

    async def spawn(args: dict[str, Any]) -> str:
        model = ScriptedModel([[("c", "slow", {})], "done"])
        return (await Agent(model, {"slow": slow}).run(args["task"])).final_text

    for label, fp in [("STATEFUL", STATEFUL), ("no declared conflict", Footprint())]:
        model = ScriptedModel(
            [
                [("p1", "spawn_agent", {"task": "a"}), ("p2", "spawn_agent", {"task": "b"})],
                "done",
            ]
        )
        parent = Agent(model, {"spawn_agent": spawn}, footprint_of=lambda call, fp=fp: fp)
        begin = time.monotonic()
        await parent.run("go")
        print(f"two 1.0s sub-agents, spawn = {label:<20} {time.monotonic() - begin:.2f}s")


# -- F10-11: what isolation would actually cost ------------------------------


def worktree() -> None:
    def git(*args: str, cwd: Path) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", *args], cwd=cwd, capture_output=True, text=True, encoding="utf-8"
        )

    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp) / "repo"
        repo.mkdir()
        git("init", "-q", "-b", "main", cwd=repo)
        git("config", "user.email", "probe@example.com", cwd=repo)
        git("config", "user.name", "probe", cwd=repo)
        (repo / "target.py").write_text("def f():\n    return 1\n", encoding="utf-8")
        git("add", "-A", cwd=repo)
        git("commit", "-qm", "initial", cwd=repo)

        begin = time.monotonic()
        for name in ("a", "b"):
            git("worktree", "add", "-q", "-b", name, str(Path(tmp) / name), "main", cwd=repo)
        setup = time.monotonic() - begin
        print(f"two worktrees created in {setup * 1000:.0f}ms")
        print(f"disk: {sum(f.stat().st_size for f in Path(tmp).rglob('*') if f.is_file()):,} bytes")

        for name, body in (("a", "    return 2\n"), ("b", "    return 3\n")):
            path = Path(tmp) / name / "target.py"
            path.write_text(f"def f():\n{body}", encoding="utf-8")
            git("commit", "-qam", f"{name} edit", cwd=Path(tmp) / name)

        merged = git("merge", "a", cwd=repo)
        print(f"\nmerge a: rc={merged.returncode} {merged.stdout.strip().splitlines()[0]}")
        merged = git("merge", "b", cwd=repo)
        print(f"merge b: rc={merged.returncode}")
        print(merged.stdout.strip())
        print((repo / "target.py").read_text(encoding="utf-8"))


# -- F10-04: a constraint the child was never told ---------------------------

SAMPLES = 5


def _watch(root: Path) -> tuple[dict[str, Any], list[str]]:
    """Real tools, plus a log of what was actually called."""
    session = Session(mode="workspace-write", approver=AllowAll())
    seen: list[str] = []
    real = default_tools(root=root, session=session)

    def wrap(name: str, fn: Any) -> Any:
        async def handler(args: dict[str, Any]) -> str:
            seen.append(f"{name}({json.dumps(args, ensure_ascii=False)[:70]})")
            return await fn(args)

        return handler

    return {name: wrap(name, fn) for name, fn in real.items()}, seen


CONSTRAINT = (
    "This machine's shell is broken: run_shell fails in ways that look like "
    "real results. Do not use run_shell for anything."
)
FIND_TASK = "Find every place in src/minicodex that calls os.killpg, and say which file it is in."


async def contract() -> None:
    arms: list[tuple[str, str, str | None, bool]] = [
        ("task only", FIND_TASK, None, True),
        ("constraint in the task", f"{FIND_TASK}\n\nConstraints:\n- {CONSTRAINT}", None, True),
        ("constraint as a system note", FIND_TASK, CONSTRAINT, True),
        ("no run_shell in the table", FIND_TASK, None, False),
    ]
    for label, task, note, with_shell in arms:
        violations = 0
        correct = 0
        overflowed = 0
        for sample in range(SAMPLES):
            tools, seen = _watch(ROOT)
            if not with_shell:
                tools = {k: v for k, v in tools.items() if k != "run_shell"}
            child = Agent(
                _model([s for s in TOOL_SCHEMAS if s["function"]["name"] in tools]),
                tools,
                max_turns=5,
                instructions=note,
            )
            try:
                result = await _retry(_fresh(child, task, seen))
            except ModelHTTPError as exc:
                # A child that reads its way past the model's window.  This
                # killed the whole section the first time it happened; it is a
                # result, not a crash, so it is counted.  These children are
                # bare `Agent`s with no context window -- which is also what
                # `run_task` builds.
                if "context_length_exceeded" not in str(exc):
                    raise
                overflowed += 1
                print(f"    overflowed the window after {len(seen)} call(s): {seen[-2:]}")
                final_text = ""
            else:
                final_text = result.final_text
            if any(s.startswith("run_shell") for s in seen):
                violations += 1
            if "shell.py" in final_text:
                correct += 1
            if sample == 0:
                print(f"    calls: {seen}")
                print(f"    said:  {final_text[:160]!r}")
        spilled = f", {overflowed}/{SAMPLES} overflowed the window" if overflowed else ""
        print(
            f"{violations:>2}/{SAMPLES} used the broken shell,"
            f" {correct}/{SAMPLES} named shell.py{spilled}  --  {label}"
        )


# -- F10-05: how much comes back ---------------------------------------------

EXPLAIN = "Explain what src/minicodex/scheduler.py does. Read it first."
SHAPE = "Answer in at most three sentences. No preamble, no lists, no code."


async def length() -> None:
    big = "List every module in src/minicodex and say in one line what each one does."
    for label, task in [
        ("task only", EXPLAIN),
        ("task + output shape", f"{EXPLAIN}\n\n{SHAPE}"),
        ("bigger task + same shape", f"{big}\n\n{SHAPE}"),
    ]:
        sizes = []
        for _ in range(SAMPLES):
            tools, _seen = _watch(ROOT)
            child = Agent(_model(), tools, max_turns=5)
            result = await _retry(_fresh(child, task))
            sizes.append(len(result.final_text))
        print(f"{label:<24} chars: {sorted(sizes)}  median {sorted(sizes)[len(sizes) // 2]}")


# -- F10-10: a child that ran out of road ------------------------------------

BIG_TASK = (
    "Work out, by reading the files, whether src/minicodex/shell.py can leave "
    "an orphan process behind on Windows. Read shell.py, then paths.py, then "
    "policy.py, then answer."
)


async def failure() -> None:
    """Does the parent notice that a truncated sub-agent is not an answer?"""
    for label, wrap in [
        ("bare final_text", lambda text, turns: text),
        (
            "outcome label first",
            lambda text, turns: (
                f"[sub-agent: turn budget exhausted after {turns} turns -- it did not finish. "
                f"Its last words were not a conclusion.]\n\n{text}"
            ),
        ),
    ]:
        answered_anyway = 0
        hedged = 0
        for sample in range(SAMPLES):
            child_tools, child_seen = _watch(ROOT)
            returned: list[str] = []

            async def spawn(
                args: dict[str, Any],
                child_tools: Any = child_tools,
                returned: Any = returned,
                wrap: Any = wrap,
            ) -> str:
                child = Agent(_model(), child_tools, max_turns=1)
                result = await child.run(args["task"])
                returned.append(
                    f"{result.stop_reason}/{result.turns_used}: {result.final_text[:120]!r}"
                )
                return wrap(result.final_text, result.turns_used)

            parent_tools, parent_seen = _watch(ROOT)
            parent = Agent(
                _model([*TOOL_SCHEMAS, SPAWN_SCHEMA]),
                {**parent_tools, "spawn_agent": spawn},
                max_turns=4,
            )
            result = await _retry(
                _fresh(
                    parent,
                    f"Use spawn_agent for this, then tell me the answer: {BIG_TASK}",
                    parent_seen,
                    child_seen,
                    returned,
                )
            )
            said = result.final_text.lower()
            admits = any(
                w in said
                for w in ("did not finish", "not finish", "incomplete", "budget", "unable to")
            )
            if not parent_seen and not admits:
                answered_anyway += 1
            if admits:
                hedged += 1
            if sample == 0:
                print(f"    child returned  {returned}")
                print(f"    child called    {len(child_seen)} tool(s)")
                print(f"    parent called   {parent_seen}")
                print(f"    parent said     {result.final_text[:200]!r}")
        print(
            f"{answered_anyway:>2}/{SAMPLES} passed it on as an answer,"
            f" {hedged}/{SAMPLES} said it was unfinished  --  {label}"
        )


SECTIONS = {
    "naive": naive,
    "contract": contract,
    "length": length,
    "failure": failure,
    "leak": leak,
    "cwd": cwd,
    "race": race,
    "depth": depth,
    "hang": hang,
    "rollout": rollout_probe,
    "serial": serial,
    "worktree": worktree,
}


def main(argv: list[str]) -> int:
    if len(argv) != 2 or argv[1] not in SECTIONS:
        print(__doc__)
        return 1
    section = SECTIONS[argv[1]]
    if asyncio.iscoroutinefunction(section):
        asyncio.run(section())
    else:
        section()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
```

> - **`_retry(make)`**：跑一个样本；连接断了或者被限流（HTTP 429）就等一会儿重来。限流那一支是改写时加的——一次 429 让一整段测量作废。
> - **`_fresh(agent, task, *logs)`**：造出"一次尝试"。每次尝试之前把记录工具调用的列表清空：重试的样本不该带着上一次失败时留下的记录，那些记录正是这一段要数的东西。
> - **`naive`**（§3）、**`leak`**（§6）、**`cwd`**（§9）、**`race`**（§11）、**`depth`**（§12）、**`hang`**（§13）、**`rollout_probe`**（§15）、**`serial`**（§11）、**`worktree`**（§18）、
>   **`contract`**（§7）、**`length`**（§8）、**`failure`**（§14）：十二段，每段对应正文里的一次测量。
> - **`_watch(root)`**：真的工具，外面包一层，记下每次调用了什么。`contract` 靠它判断"有没有用 shell"。
> - `contract` 里的 `except ModelHTTPError`：§18.2 的那次溢出，现在被数成一种结果。
> - `race` 里有一段注释解释为什么建临时目录要绕到线程里做：第 -1 章配的检查规则不允许在 `async def` 里直接做会阻塞的文件操作。

### 19.2 全部测试，两个系统

```bash
uv run ruff check .
uv run ruff format --check .
uv run pytest
```

Windows（2026-10-01）：

```
All checks passed!
61 files already formatted
1409 passed, 9 skipped in 63.72s (0:01:03)
```

Linux（WSL Ubuntu，Python 3.12，同一天）：

```
1418 passed in 37.36s
```

比第 9 章多 40 个：`test_faults_ch10.py` 的 37 个，加 `test_boundaries.py` 里的 3 个（一个测试用两组参数跑，算两个）。
（第 9 章那个检查命令行的测试，在这个快照里随着 `__main__.py` 的改动换成了 §17.2 的那一个。）

### 19.3 这些测试自己靠得住吗

`probe_mutations_ch10.py`，和第 9 章的那份是同一个写法：

```python
"""Do chapter 10's tests fail when chapter 10's code is wrong?

Same script as chapter 9's, pointed at `subagent.py`, `clip.py` and the two
one-line changes chapters 2 and 7 needed.  Each entry is an edit that should
break something; the script applies it, runs the suite, restores the file, and
reports how many tests noticed.

Restores from `atexit` and a signal handler, not from `finally` alone -- that
is chapter 6's lesson, learned by leaving `if False:` in `tokens.py` after a
Ctrl-C during a pytest run.

    uv run python probe_mutations_ch10.py
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
        "subagent.py",
        "the child gets a fresh shell instead of the parent's directory",
        "shell.cwd = ctx.parent_shell.cwd",
        "shell.cwd = shell.cwd",
    ),
    (
        "subagent.py",
        "constraints are appended to the task instead of the system note",
        '"These rules come from the user and apply to everything you do:\\n"',
        '"" if True else "These rules come from the user:\\n"',
    ),
    (
        "subagent.py",
        "the result is returned whole, with no ceiling",
        "body = clip(self.text.strip(), MAX_TASK_RESULT_CHARS)",
        "body = self.text.strip()",
    ),
    (
        "subagent.py",
        "the depth limit is not enforced",
        "if ctx.depth >= ctx.max_depth:",
        "if False:",
    ),
    (
        "subagent.py",
        "the bottom level is handed a spawn tool it may not use",
        "if ctx.depth + 1 < ctx.max_depth:",
        "if True:",
    ),
    (
        "subagent.py",
        "the shared turn budget is not enforced",
        "if spent >= ctx.child_turn_budget:",
        "if False:",
    ),
    (
        "subagent.py",
        "a bare wait_for, which chapter 7 turns into a silent empty answer",
        "result = await asyncio.wait_for(asyncio.shield(task), ctx.timeout)",
        "result = await asyncio.wait_for(task, ctx.timeout)",
    ),
    (
        "subagent.py",
        "a cancelled parent abandons its running child",
        "        await _stop(task)\n        raise",
        "        raise",
    ),
    (
        "subagent.py",
        "an empty answer counts as success",
        "    elif not text:",
        "    elif False:",
    ),
    (
        "subagent.py",
        "turn_limit is reported as an ordinary answer",
        'if result.stop_reason == "turn_limit":',
        "if False:",
    ),
    (
        "subagent.py",
        "the child writes into its parent's session file",
        "session_id=new_session_id(),\n        created=time.time(),",
        "session_id=ctx.parent_session_id or new_session_id(),\n        created=time.time(),",
    ),
    (
        "subagent.py",
        "the parent link is not recorded",
        "parent=ctx.parent_session_id or None,",
        "parent=None,",
    ),
    # The next three were added when the chapter was rewritten: each is a
    # line that could be deleted with every test green.
    (
        "subagent.py",
        "an interrupted child is reported as an empty answer",
        '    elif result.stop_reason == "interrupted":',
        "    elif False:",
    ),
    (
        "subagent.py",
        "the child's shell forgets the parent's command timeout",
        "shell = ShellSession(timeout=ctx.parent_shell.timeout)",
        "shell = ShellSession()",
    ),
    (
        "subagent.py",
        "finished children are not recorded, so the shared budget never fills",
        "    ctx.children.append(result)\n",
        "    pass\n",
    ),
    (
        "clip.py",
        "tail-only truncation, which is F02-03",
        'return f"{text[:head]}\\n... ({omitted} characters omitted) ...\\n{text[-tail:]}"',
        "return text[-limit:]",
    ),
]

SRC = Path("src/minicodex")
ORIGINALS = {name: (SRC / name).read_text(encoding="utf-8") for name, *_ in MUTATIONS}
SUITES = ["tests/test_faults_ch10.py", "tests/test_shell.py", "tests/test_faults_ch09.py"]


def restore() -> None:
    for name, text in ORIGINALS.items():
        path = SRC / name
        if path.read_text(encoding="utf-8") != text:
            path.write_text(text, encoding="utf-8")


atexit.register(restore)
signal.signal(signal.SIGINT, lambda *_: sys.exit(130))


def refuse_if_already_mutated() -> None:
    """Do not start on a tree a killed run left dirty.

    `atexit` and the `SIGINT` handler restore the source on every graceful
    exit and neither runs on a `SIGKILL`.  Chapter 9's snapshot shipped with
    one of its own mutations applied for exactly that reason; this is the
    guard its script grew afterwards.  Both halves are checked -- the original
    text missing *and* the mutated text present -- because several mutations
    replace an expression with a simpler one that occurs elsewhere anyway.
    """
    dirty = [
        f"{name}: looks like {label!r} is still applied"
        for name, label, before, after in MUTATIONS
        if before not in ORIGINALS[name] and after in ORIGINALS[name]
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
                timeout=600,
                capture_output=True,
                text=True,
            )
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

> - 十六条变异，分布在 `subagent.py` 和 `clip.py`。跑的是三个测试文件（`SUITES`）：这一章的，加上 `clip()` 的另外两个使用者的。
> - `refuse_if_already_mutated()` 是改写时从第 9 章的脚本搬过来的——那一章的快照里留着一条变异残留，这是那件事之后长出来的检查。
> - "the child writes into its parent's session file" 这一条，就是 §15.1 那个测试的来历：第一次跑，它没有被抓到。

```
$ uv run python probe_mutations_ch10.py
16 mutations, tests/test_faults_ch10.py tests/test_shell.py tests/test_faults_ch09.py

    2 test(s) fail  <-  the child gets a fresh shell instead of the parent's directory
    1 test(s) fail  <-  constraints are appended to the task instead of the system note
    1 test(s) fail  <-  the result is returned whole, with no ceiling
    1 test(s) fail  <-  the depth limit is not enforced
    1 test(s) fail  <-  the bottom level is handed a spawn tool it may not use
    2 test(s) fail  <-  the shared turn budget is not enforced
    2 test(s) fail  <-  a bare wait_for, which chapter 7 turns into a silent empty answer
    1 test(s) fail  <-  a cancelled parent abandons its running child
    1 test(s) fail  <-  an empty answer counts as success
    2 test(s) fail  <-  turn_limit is reported as an ordinary answer
    1 test(s) fail  <-  the child writes into its parent's session file
    1 test(s) fail  <-  the parent link is not recorded
    1 test(s) fail  <-  an interrupted child is reported as an empty answer
    1 test(s) fail  <-  the child's shell forgets the parent's command timeout
    1 test(s) fail  <-  finished children are not recorded, so the shared budget never fills
    4 test(s) fail  <-  tail-only truncation, which is F02-03

every mutation was caught.
```

（Linux，在一份临时拷贝里跑的。）十六条都被抓到。

倒数第二到第四条是改写时加的，各对应一个新测试：

```python
@pytest.mark.asyncio
async def test_F10_10_a_child_whose_own_tool_was_cancelled_is_not_an_answer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The one way a child ends `interrupted` while `run_task` itself is not
    cancelled: a tool inside it raises `CancelledError`.

    `Agent.run` reports that as `stop_reason="interrupted"` with an empty
    `final_text` -- the same empty string a child that ran out of turns
    returns. The branch that tells them apart had no test: with it deleted
    the result became `empty`, whose advice is "split the task" -- said about
    work that was stopped, not work that was too big. Found by mutation.
    """
    import minicodex.subagent as subagent

    async def stopped(args: dict[str, Any]) -> str:
        raise asyncio.CancelledError

    original = child_tools

    def patched(c: SubAgentContext) -> Any:
        handlers, schemas = original(c)
        handlers["stopped"] = stopped
        return handlers, schemas

    monkeypatch.setattr(subagent, "child_tools", patched)
    ctx = context_for(tmp_path, ScriptedModel([[("c", "stopped", {})], "done"]))
    result = await run_task(TaskSpec(task="t"), ctx)

    assert result.outcome == "interrupted"
    assert "Do not start it again unless asked" in result.render()


@pytest.mark.asyncio
async def test_F10_01_a_child_keeps_the_command_timeout_the_parent_was_given(
    tmp_path: Path,
) -> None:
    """The third thing a child inherits from where the parent stands.

    The child's shell is a new object, so everything not copied on purpose
    falls back to a default -- here, thirty seconds per command, whatever the
    parent was configured with. Dropping the copy left every test green.
    Found by mutation.
    """
    import sys

    (tmp_path / "slow.py").write_text("import time\ntime.sleep(3)\n", encoding="utf-8")
    parent_shell = ShellSession(timeout=0.5)
    parent_shell.cwd = str(tmp_path)
    ctx = context_for(tmp_path, ScriptedModel(["done"]), shell=parent_shell)
    handlers, _schemas = child_tools(ctx)

    # Three seconds of work against half a second of patience. No assertion
    # on elapsed time: on Windows the command is not actually killed (F02-10),
    # so the call returns when the command ends -- but it still reports that
    # the deadline passed, and the deadline is what is being tested.
    output = await handlers["run_shell"]({"command": f"{sys.executable} slow.py"})

    assert "killed: still running" in output
```

> - 第一个：子 Agent 的一个工具自己抛出了"被取消"——这时 `Agent.run` 交回的是 `interrupted` 和一个空字符串。把 `run_task` 里判断它的那一支删掉，
>   结局会变成 `empty`，给父 Agent 的建议是"把任务拆小"——对一件被叫停的事这么说，是错的。删掉那一支，原来没有测试会红。
>   `monkeypatch.setattr(...)` 是 pytest 提供的"临时替换，测试结束自动换回"。
> - 第二个：子 Agent 的 shell 是一个新对象，没有特意抄过来的东西都会回到默认值——这里是"每条命令最多 30 秒"。
>   父 shell 设成 0.5 秒，子 Agent 跑一个要 3 秒的程序，结果里得有"超时被杀"的那句话。注释说了为什么不断言用了多久：Windows 上那条命令并没有真的被杀（§13.1）。

还有一条变异值得单独说，它**没有**放进脚本，因为它不会结束：把 `child_tools` 里的 `replace(ctx, depth=ctx.depth + 1)` 改成直接传 `ctx`（深度不再加一）。
测试没有变红——测试**停不下来了**，和 §12.1 一模一样。原因在 §12.2 说过的那处"不准"里：预算只数**跑完了**的子 Agent，而一条一直往下钻的链上，没有一个跑完。
**预算挡不住深度，深度上限挡不住宽度，两个都得有。**

`.github/workflows/postmerge.yml` 末尾加一步：

```yaml
      # Chapter 10 adds its own sixteen. One of them survived the first run:
      # "the child writes into its parent's session file" left every chapter 10
      # test green, because the test asserted "one file exists and its parent
      # field is right" without building a parent writer at all. Reconstructing
      # the real situation -- the parent holding its own lock -- is what made
      # the mutation fail.
      - name: Mutation check (chapter 10)
        run: uv run python probe_mutations_ch10.py
```

```bash
git add probe_subagent.py probe_mutations_ch10.py .github/workflows/postmerge.yml tests/test_faults_ch10.py
git commit -m "test(subagent): the probe behind every number, and sixteen mutations the suite must notice"
```

---

## §20 收工

### 20.1 这一章动了哪些文件

| 文件 | 新增还是改动 | 在哪一节 |
|---|---|---|
| `src/minicodex/clip.py` | 新增 | §4 |
| `src/minicodex/shell.py`、`src/minicodex/registry.py` | 各改一处 | §4 |
| `src/minicodex/tools.py` | 拆一个函数 | §5 |
| `src/minicodex/subagent.py` | 新增 | §6 – §16 |
| `src/minicodex/rollout.py` | 加一个字段，改一行 | §15 |
| `src/minicodex/__main__.py` | 改几处 | §17 |
| `tests/test_faults_ch10.py` | 新增 | 分散在各节 |
| `tests/test_boundaries.py`、`tests/test_schemas.py` | 各加几处 | §16 |
| `probe_subagent.py`、`probe_mutations_ch10.py` | 新增 | §19 |
| `.github/workflows/postmerge.yml` | 加一步 | §19 |

### 20.2 推送、PR

```bash
git push -u origin feat/subagents
```

PR 描述里要如实写的：

> - 子 Agent **一个一个跑**（§11）。并行需要先隔离它们改的东西，而隔离之后的冲突没有人解（§18.1）。
> - 子 Agent 的对话**没有压缩、没有并行、没有录制**（§18.2）。量到过一次读超窗口。没有修。
> - 子 Agent 只有这个项目自己的工具，**没有 MCP 的工具**。
> - Windows 上，超时的子 Agent 启动的命令不会被杀掉（§13.1），这是第 2 章就记下的那一条。
> - 审批的提示不说请求来自子 Agent（§9.1）。
> - 整次运行的预算只数跑完了的子 Agent（§12.2）。
> - F10-10 作为"父 Agent 因此答错"没有稳定复现（1/5 对 0/5）；F10-08 在这个程序里没有东西可测。
> - 真模型的测量只有一个服务商、一个模型、每组 5 次。

### 20.3 自己审一遍

**1 · 深度上限为什么是 2 而不是 1？**
1 更安全，§12.2 那 72 次调用在上限为 1 时根本不会发生——那是把上限设成 2 买来的问题。留着 2，是因为"子任务里再分一次"是一个真实的用法；代价是多了一个预算要维护。

**2 · `Session` 是共用的，子 Agent 申请到的权限，父 Agent 也有了。这对吗？**
对，而且是故意的：权限是人给的，给的是"这次运行"，不是某一段对话。反过来设计（子 Agent 的权限只属于它自己）意味着同一个问题会问用户两遍。

**3 · 子 Agent 超时之后，它已经改了的文件怎么办？**
什么都不做，只是说出来（"它已经改了的东西还在"）。撤销需要知道它改了什么，而那需要 §18.1 没做的那种隔离。

**4 · 成功的结果不带任何标头，父 Agent 怎么知道这是子 Agent 说的？**
它是 `spawn_agent` 这个工具调用的结果，位置本身就说明了来源。

**5 · `spawn_agent` 的说明这么长，值得吗？**
第 3 章量过说明里"什么时候不要用"的作用。这一章没有单独量 `spawn_agent` 的说明——这是一个没有测量支撑的地方，如实记下。

---

## §21 codex 是怎么做的

- **它有两代子 Agent 的工具，同时存在。** 第一代是"启动一个、给它输入、关掉"；第二代是**一棵有名字的树**——每个子 Agent 有一个路径一样的名字，可以被寻址、被发消息、被中断。
  这一章做的是第一代那种形状的更简单的版本：启动一个，跑完，拿结果。
- **它的默认深度上限是 1**：子 Agent 默认不能再启动子 Agent。比这一章保守，§12.2 的问题在那个设置下不存在。
- **它的子 Agent 是并行的**，所以有"等某个子 Agent""列出所有子 Agent"这样的工具，同时在跑的数量有上限（默认 4），用"先占一个名额"的办法实现。
  这是 §11 那笔时间在别人那里的还法——代价是每个子 Agent 得有自己的工作区。
- **等待有超时，最小值有下限**，源码里的注释说是为了防止"反复用很短的超时去等"变成空转。
- **最值得读的是它给 `spawn_agent` 写的说明**：很长，而且开头就是禁令——除非用户明确要求，不要启动子 Agent；
  紧接着一句补丁："要深入、要彻底、要调查"**不算**要求启动子 Agent。第二句只可能来自"说了别乱用，然后它看到'彻底'两个字就用了"。
- **同一段说明里有一句**：给子 Agent 分配改代码的任务时，让每个任务改的文件互不相交。
  **codex 把"改的东西不相交"写成给模型的一条要求；这一章把它做成调度器的一条约束。** 同一个现象，一个靠模型遵守，一个不靠。§11 的 29/30 是不遵守时的样子。
- **清单给 F10-08 开的药——一个专门负责等待的子 Agent——在 codex 里被注释掉了**，旁边留着一句"暂时移除"。

---

## §22 回头看：这一章撞到了什么

**预测到了，并且成立的：** F10-03（7 倍，不是 2 倍）、F10-04、F10-05、F10-06、F10-07、F10-12（但不是混在一起，是直接报错）。
**预测到了，早就挡住的：** F10-02（第 8 章的默认值）。**预测到了，动工前挡住的：** F10-09。
**按清单的说法不会发生的：** F10-01（找到的是另一个更糟的）。**没有东西可测的：** F10-08。
**只成立一半的：** F10-10。**量了，决定不做的：** F10-11。

**没预测到的：**

| 故障 | 怎么发现的 | 挡住它的东西 |
|---|---|---|
| 父 Agent 没写下来的状态（当前目录、权限），子 Agent 没有 | 🟡 顺着 F10-01 找 | 传父 shell 的位置，共用 `Session` |
| 10 秒的超时被递归弄坏，`RecursionError` 出在事件循环自己的回调里 | 🔴 探针 | 深度在启动之前检查 |
| **深度上限管不住宽度：72 次模型调用** | 🟢 一个写错了数字的断言 | 整次运行共用的预算 |
| **`wait_for` 给 Agent 设不了超时** | 🟡 探针 | `shield`，再亲手取消 |
| 约束写在任务里 2/5 照样违反；写在系统消息里 0/5 | 🔵 真模型测 | 约束进系统消息 |
| 约束被遵守之后，任务做不成了（编造文件名） | 🔵 同一次测量 | **没修**，记录 |
| `--resume last` 悄悄接上了子 Agent 的对话 | 🟠 看 `minicodex sessions` 的输出 | `last` 跳过子会话 |
| 子会话的文件头没记是哪个模型 | 🟠 同一次查看 | 两个字段 |
| 把工具放在 `tools.py` 里，循环导入 | 🔴 | 往上搬；两个方向都有测试 |
| 第 3 章的快照没覆盖 `spawn_agent` | 🟣 | 手工加进去 |
| 第三份一样的截断代码 | 🟣 | `clip.py` |
| "子 Agent 写自己的文件"的测试，子 Agent 写进父文件时也是绿的 | ⚪ 变异测试 | 把父会话真的搭出来 |
| 用 `AllowAll` 去测"只读会拒绝" | 🟣 | 换成默认的审批者 |
| **子 Agent 没有压缩，读超了模型的窗口** | 🔴 改写时重跑探针，HTTP 400 | **没修**；探针把它数成一种结果 |
| **`__main__.py` 里接上子 Agent 的六行，没有测试看过** | ⚪ 改写时做的变异 | 两个调用 `main()` 的测试 |
| 一个断言紧跟在它自己的更强版本后面，不可能失败 | 🟣 改写时逐行读测试 | 换成断言实际发生的事 |
| "被打断"的结局、子 shell 的超时：删掉都没有测试红 | ⚪ 变异测试 | 各一个测试 |
| 深度不往下传，测试不是变红，而是停不下来 | ⚪ 变异测试 | 记录：预算不能代替深度上限 |
| Windows 上超时的子 Agent 的命令还在跑 | 🟠 改写时用真的命令试 | **没修**（第 2 章那条） |
| 探针把工具结果的长度打印成 0；一次限流让整段作废 | 🟠 重跑探针 | 修探针 |

标记：🔴 崩溃 · 🟡 静默 · 🟢 边界测试 · 🔵 长跑 · 🟠 看日志 · 🟣 审查 · ⚪ 工具

---

## 如果你只记住三件事

1. **子 Agent 不是一种新东西，是同一个 `Agent` 在一个工具调用里；难的全在边界上。**
   进去的是一份契约（任务、约束、输出形状），不是整段历史；还得带上父 Agent 从没写下来的东西（它站在哪、它被允许做什么）。
   出来的是一个结局，不是一个字符串；外面套着深度、轮数、时间、长度四道上限。

2. **两个各自正确的设计，合在一起可能是一个不会响的闹钟。**
   第 7 章让 Agent 被取消时好好收尾、正常返回；`wait_for` 靠"被取消"来判断超时。合起来：超时了，得到一个空答案。
   深度上限和预算也是一对：少了哪一个，另一个都挡不住。

3. **把你相信的数字写成断言。**
   `assert calls <= 8` 红了，红出来一个 72——这一章最贵的问题就是这么找到的。而一个写成 `any(...) or calls == 32` 的断言，永远不会告诉你任何事。

---

## 动手练习

1. 把 `run_task` 里的 `asyncio.wait_for(asyncio.shield(task), ctx.timeout)` 改成 `asyncio.wait_for(task, ctx.timeout)`，跑 `uv run pytest tests/test_faults_ch10.py`。
   哪个测试红了？它得到的结局是什么？改回去。
2. 在你自己的机器上跑 `uv run python probe_subagent.py race` 和 `serial`。"它只是读"那一行，30 次里丢了几次？
3. 把 `MAX_DEPTH` 改成 1，跑测试。哪些红了？其中那个写着 `== 32` 的，现在应该是多少？先算，再跑。
4. `TaskResult.render()` 对成功的结果不加标头。给它加上（比如 `[sub-agent: ok]`），哪个测试会红？读一读那个测试的 docstring，你同意它的理由吗？
5. §18.2 里没修的那一点：给 `SubAgentContext` 加一个 `context_window` 字段，传给子 Agent。先别写代码，先数一数：
   `__main__.py` 里造父 Agent 时传了几个参数？`run_task` 里传了几个？如果以后父 Agent 再多一个参数，谁会记得来改 `run_task`？

下一章不加新功能。这一章为了躲开循环导入，把一个工具"往上搬"了；为了造子 Agent，在第二个地方造了 `Agent`。这两个临时的办法各留下了一笔账，下一章来收拾。
