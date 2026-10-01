# 第 8 章 · 工具并发

> **代码**：`steps/step08_concurrency/`
> **分支**：`feat/scheduler`
> **产出**：模型一轮里发起三个互不相干的调用，真的同时跑；而对同一个文件的两次编辑、同一轮里的两条 shell 命令，仍然一次只跑一个
> **前置**：做完第 7 章。全部测试都不联网；探针 `probe_scheduler.py` 也不需要网络。
> **这一章的实测在 Windows 和 Linux 上各做了一遍**，有一组数字两边不一样。

---

## §0 开工前的准备

### 0.1 本章会遇到的新概念

- **并发（concurrency）**：几件事的执行时间互相重叠，而不是一件做完再做下一件。
- **竞态（race）**：两件同时进行的事，结果取决于谁先谁后——而谁先谁后不由你控制。
- **调度（scheduling）**：决定哪些事可以一起做、哪些必须排队。
- **批次（batch）**：一组可以一起跑的调用。一批全部跑完，才开始下一批。
- **信号量（semaphore）**：一个"同时最多 N 个"的计数器。拿到名额才能进，出来时归还；名额用完了，后来的就等着。
- **锁（lock）**：名额只有 1 个的信号量。
- **临界区**：一段"同一时间只允许一个人在里面"的代码。

### 0.2 本章会遇到的新 Python 写法

| 写法 | 意思 |
|---|---|
| `asyncio.gather(a, b, c)` | 同时运行几个协程，等它们**全部**结束，结果按传入的顺序排成列表返回 |
| `asyncio.gather(..., return_exceptions=True)` | 同上，但某个协程出了异常时不往外抛，而是把异常对象放在结果里它对应的位置 |
| `asyncio.to_thread(函数, 参数...)` | 把一个普通（会卡住的）函数放到另一个线程里去跑，自己可以被 `await` |
| `asyncio.Semaphore(8)` / `async with sem:` | 信号量 / 进入时拿一个名额，离开时归还 |
| `asyncio.Lock()` / `async with lock:` | 锁 / 进入时上锁，离开时解锁 |
| `threading.Lock()` / `with lock:` | 线程之间用的锁（`asyncio.Lock` 只能在协程之间用） |
| `a & b`（两个集合） | 交集：同时在两个集合里的元素。空集合当条件用是 `False` |
| `zip(a, b, strict=True)` | 把两个列表一一配对；`strict=True` 表示长度不一样就报错 |
| `task.cancelled()` | 这个任务是不是被取消的 |
| `for ... else:` / `try ... else:` | `else` 部分在"循环正常走完"/"没有发生异常"时执行 |
| `functools.partial(f, root=x)` | 把函数 `f` 的 `root` 参数先固定成 `x`，得到一个少一个参数的新函数 |

### 0.3 开分支

```bash
git switch main
git pull
git switch -c feat/scheduler
```

---

## §1 这一章要做出来的东西

到第 7 章为止，Agent 处理一轮里多个工具调用的方式，从第 0 章起就没变过：一个接一个。

模型说"读 `a.py`，读 `b.py`，读 `c.py`"，这三次读互相之间没有任何关系——`b.py` 的内容不取决于 `a.py` 读没读过——
但 Agent 还是等第一个返回了，才开始第二个。

这一章要做的事：**该同时跑的，同时跑；不该同时跑的，一次也不能重叠。**

第二句和第一句一样重要。上一章刚把"每个调用都有且只有一个结果"变成了一条被打断时也成立的规则；
这一章如果只顾着快，很容易撞出一个新问题：两个 `apply_patch` 同时改一个文件，后写的把先写的悄悄盖掉，两边都报告"成功"。

---

## §2 定需求，猜故障

需求：

- 一轮里互不相干的调用同时执行；
- 会互相影响的调用不重叠；
- 同时执行的数量有上限；
- 第 7 章的规则不变：每个调用都被回答，被打断时也是。

动工前的猜测清单，九条：

| 编号 | 猜测 | 怎么判断 |
|---|---|---|
| F08-01 | 两个调用同时改一个文件，后写的盖掉先写的 | 真的让它们同时跑，读文件 |
| F08-02 | 一个在读、一个在写同一个文件，读到一半的内容 | 数"同时在碰这个文件的"有几个 |
| F08-03 | 两条有状态的 shell 命令同时跑 | 同上 |
| F08-04 | 一个调用抛异常，同一批里别的调用的结果丢了 | 构造 |
| F08-05 | 结果按"谁先跑完"排，和模型发出的顺序不一样 | 让后发出的先跑完 |
| F08-06 | 模型默认了一个先后顺序，而调度器不知道 | 构造 |
| F08-07 | 并发没有上限 | 数同时在跑的有几个 |
| F08-08 | 取消没有传到每一个正在跑的调用 | 取消一批 |
| F08-09 | 竞态只在慢机器上才出现，测试时好时坏 | 调延迟，数出现的次数 |

---

## §3 最直白的版本：全部丢给 `gather()`

把循环换成 `asyncio.gather()`：

```python
outputs = await asyncio.gather(*(self._run_tool(c) for c in turn.tool_calls))
for call, output in zip(turn.tool_calls, outputs):
    history.add_tool_result(call.call_id, output)
```

> `*(...)`：把一串协程展开，作为 `gather` 的多个参数。

三个互不相干的读，确实同时进行了。看起来这一章到这里就能收工。

那拿它去试"两个 `apply_patch` 改同一个文件"。`apply_edits()` 是第 4 章写的：读文件、算出新内容、写回去，中间没有任何保护。
这一章的探针把这个场景跑 30 遍。新建 `probe_scheduler.py`：

```python
"""Measures F08-09: does the race in test_F08_01 need the deliberate delay?

Runs the same naive two-writer race from tests/test_scheduler.py many times,
with and without the delay inserted in `read_source`, and reports how often
each version actually loses an edit. Not part of the test suite -- a flaky
assertion that "usually" fails is worse than no assertion at all, so the
delayed version is what ships, and this script is the receipt for why.

    uv run python probe_scheduler.py
"""

from __future__ import annotations

import asyncio
import shutil
import tempfile
import time
from pathlib import Path

from minicodex.patch import Edit, apply_edits, read_source


async def one_trial(delay: float) -> bool:
    """True if the edit was lost -- i.e. the race actually happened."""
    tmp = Path(tempfile.mkdtemp())
    try:
        path = tmp / "shared.txt"
        path.write_text("A = 1\nB = 2\n")

        original = read_source

        def maybe_slow(p: Path) -> tuple[str, str]:
            text, ending = original(p)
            if delay:
                time.sleep(delay)
            return text, ending

        import minicodex.patch as patch_mod

        patch_mod.read_source = maybe_slow
        try:
            await asyncio.gather(
                asyncio.to_thread(apply_edits, [Edit(str(path), "A = 1", "A = 100")], tmp),
                asyncio.to_thread(apply_edits, [Edit(str(path), "B = 2", "B = 200")], tmp),
            )
        finally:
            patch_mod.read_source = original

        final = path.read_text()
        return not ("A = 100" in final and "B = 200" in final)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


async def measure(delay: float, n: int) -> int:
    return sum([await one_trial(delay) for _ in range(n)])


async def main() -> None:
    n = 30
    print(f"=== F08-01 / F08-09: does the naive race need a deliberate delay? (n={n}) ===")
    for delay in (0.0, 0.001, 0.05):
        lost = await measure(delay, n)
        print(f"    read_source delay={delay:>6.3f}s   edit lost: {lost}/{n}")


if __name__ == "__main__":
    asyncio.run(main())
```

> - **`one_trial(delay)`**：建一个临时目录和一个两行的文件（`A = 1`、`B = 2`）；把 `patch.py` 里的 `read_source` 临时换成一个"读完之后再睡 `delay` 秒"的版本；
>   然后用 `gather` 同时跑两次 `apply_edits`——一个把 `A = 1` 改成 `A = 100`，一个把 `B = 2` 改成 `B = 200`。
>   最后读文件：两处修改没有同时在，就返回 `True`（丢了一个）。
> - `patch_mod.read_source = maybe_slow`：直接把模块里的那个函数换掉；`finally` 里再换回来。
> - **`measure(delay, n)`**：跑 `n` 次，数丢了几次（`True` 当数字用是 1）。
> - `asyncio.to_thread(apply_edits, ...)`：`apply_edits` 是普通函数，放到线程里跑，两次才会真的重叠。

```
Windows
=== F08-01 / F08-09: does the naive race need a deliberate delay? (n=30) ===
    read_source delay= 0.000s   edit lost: 30/30
    read_source delay= 0.001s   edit lost: 30/30
    read_source delay= 0.050s   edit lost: 30/30

Linux
    read_source delay= 0.000s   edit lost: 26/30
    read_source delay= 0.001s   edit lost: 30/30
    read_source delay= 0.050s   edit lost: 30/30
```

**几乎每次都丢。** 而且不是"抛了异常"——两次调用都老老实实返回了 `Applied 1 edit(s)`，模型会认为两处修改都成功了。
文件里只有一处，另一处凭空消失，没有任何报错。🟡 静默错误。**F08-01 成立。**

道理很简单：两边都先读到了**修改前**的内容，各自在上面改了自己的那一处，然后先后写回去——后写的那份里，没有先写的那一处。

再看延迟为 0 的那一行，它回答的是 F08-09（"只在慢机器上才出现"）：

- Windows：一点人为的延迟都不加，照样 30/30。
- Linux：不加延迟是 **26/30**——30 次里有 4 次，两个线程碰巧一前一后，没有撞上。

所以 F08-09 的答案是：**在这两台机器上，这个竞态不需要"慢"才出现；但"要不要人为的延迟才能每次都出现"，两台机器的答案不一样。**
这直接决定了测试怎么写：一个 30 次里有 4 次不出现的现象，不能直接写成断言——那会是一个偶尔变红的测试。
测试里要插入那 0.05 秒的延迟，把"通常会撞上"变成"每次都撞上"。**探针就是这个延迟的凭据。**

> 把循环换成 `gather()`，删掉的不是"慢"，而是"谁先谁后"这件事。原来一个接一个地执行时，`apply_patch` 从来不会和另一个 `apply_patch` 同时跑，
> 所以这个问题从第 4 章起就在代码里，只是一直没有一个入口能让它发生。

---

## §4 谁来决定"能不能一起跑"

`gather()` 版本缺的不是并发，而是一个能回答"这两个调用能不能放在一起"的东西。

要回答，得知道每个调用"碰了什么"：`read_file(a.py)` 碰的是 `a.py`；两个读碰的是两个不同的东西，能一起跑；
一个写 `a.py`、一个读 `a.py`，碰的是同一个东西，而且其中一个要写，不能一起跑。

这是这一章的新模块。**值不值得单独成一个模块？** 按第 5 章补进清单的那条：**一条规则必须只在一个地方执行**。
"两个会冲突的调用不能同时在跑"是一条规则，不收进一处，以后每个要并发的地方都会各自判断一遍。

新建 `src/minicodex/scheduler.py`，先放这三样（开头的 docstring 和 import 在 §5 末尾和整个文件一起给）：

```python
@dataclass(frozen=True)
class Footprint:
    """What one call touches, as far as the scheduler is willing to guess.

    `reads` and `writes` are resource keys -- for the tools this project has,
    a resource key is a resolved absolute path, so two calls naming the same
    file collide and two calls naming different files do not. The scheduler
    never looks inside a key; it only compares them for equality, which is
    what lets `tools.py` own the one question that actually needs tool
    knowledge (what does "the same resource" mean for *this* tool) while this
    module owns none of it.

    `stateful=True` means "conflicts with everything, including another
    stateful call" -- the answer for a tool whose effect cannot be named as a
    set of resources (`run_shell`: the working directory it carries across
    calls, the network, the rest of the filesystem) and the answer for a call
    the scheduler could not classify at all. Unclassifiable defaults to
    stateful, not to "no footprint" -- an empty `Footprint()` would claim the
    call touches nothing, which is the one claim that is never safe to guess.
    """

    reads: frozenset[str] = frozenset()
    writes: frozenset[str] = frozenset()
    stateful: bool = False


# The one Footprint value that means "do not run this next to anything else."
# A named constant rather than `Footprint(stateful=True)` written out at every
# call site, so grepping for STATEFUL finds every place that gives up on
# classifying a call.
STATEFUL = Footprint(stateful=True)

FootprintFn = Callable[[ToolCall], Footprint]


def conflicts(a: Footprint, b: Footprint) -> bool:
    """Must `a` and `b` never be in flight at the same time?

    Read/read never conflicts -- that is the entire concurrency win this
    chapter has to offer, and it is also the only case this function is
    allowed to say no to. Every other combination -- a write touching what
    the other reads or writes, or either side being stateful -- says yes.
    """
    if a.stateful or b.stateful:
        return True
    return bool(a.writes & b.writes) or bool(a.writes & b.reads) or bool(a.reads & b.writes)
```

> - **`Footprint`**（脚印）：一个调用碰了什么。`reads` 是它读的东西，`writes` 是它写的东西，各是一个"资源名"的集合。
>   对这个项目现有的工具来说，资源名就是一个文件的完整路径。**调度器从不关心资源名里写的是什么，只比较相不相等**——
>   所以"什么算同一个资源"这个问题完全交给知道工具细节的 `tools.py`，这个模块一个工具都不认识。
> - **`stateful=True`**："这个调用碰了什么，说不清楚"。它和任何调用都冲突，包括另一个 `stateful` 的。
> - **`STATEFUL`**：上面那个值的名字。以后搜这个词，就能找到每一处"放弃分类"的地方。
> - **`FootprintFn`**：一个类型名——"给一个调用，返回它的脚印"的函数。
> - **`conflicts(a, b)`**：有一边是 `stateful`，冲突。否则看三种交集：都写同一个东西；a 写的 b 在读；a 读的 b 在写。**"读和读"是唯一不冲突的组合**——
>   这一章能提供的全部加速，就来自这一种情况。

**一个不起眼但最要紧的默认值**：分类不了的调用，返回的是 `STATEFUL`，**不是**空的 `Footprint()`。
空的脚印等于宣称"这个调用什么都不碰"——这是唯一一句猜错了后果最严重的话。

> 这是第 2 章的环境变量白名单、第 5 章的"看不懂就问"的同一条规矩：**猜不出来时，往安全的方向猜，而不是往方便的方向猜。**

---

## §5 排批次

有了 `conflicts()`，还差一步：把"一轮里的 N 个调用"变成"若干批，同一批里一起跑，批与批之间按顺序"。

```python
def batches(calls: Sequence[ToolCall], footprint_of: FootprintFn) -> list[list[ToolCall]]:
    """Group calls into ordered batches: run batch N fully before batch N+1.

    List scheduling, not a general solver: for each call, in the order the
    model issued them, its batch is one past the latest batch of anything it
    conflicts with among the calls before it. Two calls that conflict can
    never land in the same batch, by construction -- if they did, the second
    one's minimum batch would have been pushed past the first one's. Two
    calls that do not conflict, directly or through anything between them,
    end up in the same batch and run concurrently.

    O(n^2) in the number of calls in one turn. That number is bounded by how
    many tool calls a model can put in one response, which is small enough
    that the readable quadratic version is the right one to ship.
    """
    fps = [footprint_of(call) for call in calls]
    batch_index: list[int] = []
    for i, fp in enumerate(fps):
        earliest = 0
        for j in range(i):
            if conflicts(fp, fps[j]):
                earliest = max(earliest, batch_index[j] + 1)
        batch_index.append(earliest)

    result: list[list[ToolCall]] = []
    for call, index in zip(calls, batch_index, strict=True):
        while len(result) <= index:
            result.append([])
        result[index].append(call)
    return result
```

> - 先给每个调用算出脚印（`fps`）。
> - 按模型给出的顺序处理每个调用：看它前面的每一个调用，**和谁冲突，就得排在谁的后面一批**（`batch_index[j] + 1`）；和好几个冲突，取最靠后的那个。
>   和谁都不冲突，就是第 0 批。
> - 最后按批次号把调用分进各自的列表。`while len(result) <= index: result.append([])`：批次列表不够长就补空列表。

两个冲突的调用不可能落进同一批：后一个的批次号至少是前一个的加 1。

这是个两层循环，调用越多越慢。但一轮里模型能发出的调用就几个到十几个，一眼能看懂的写法是对的选择。

### 5.1 一个被测试纠正的想当然

给 `batches()` 写的第一个测试是这样的：`a` 和 `b` 都要写同一个文件，`c` 读一个不相干的文件，顺序是 `[a, b, c]`。
当时的直觉是：模型给的顺序会被保留，所以应该是三批 `[[a], [b], [c]]`。实际：

```
  order asked for: [a, b, c]   plan: [['a', 'c'], ['b']]
```

`c` 和 `a` 分到了同一批，跑在了 `b` 的**前面**。想清楚之后，这是对的：**一个调用只会被和它冲突的调用往后推。**
`c` 和谁都不冲突，没有任何理由等。

"模型给的顺序会被保留"这句话是错的。真正成立的是弱一些的一句：
**两个互相冲突的调用，后一个一定不会跑在前一个之前。** 而这正是 F08-01 需要的全部保证。

那个测试留了下来，名字和断言都改成了它实际证明的东西。

整个 `scheduler.py`：

```python
"""Deciding which tool calls in one turn may run at the same time.

This module knows nothing about tools. It knows about `Footprint`s -- what a
call reads, what it writes, and whether it is safe to reason about at all --
and turns a list of them into batches that can run one `asyncio.gather` at a
time without two conflicting calls ever being in flight together.

Chapter 7 spent its whole trust budget on one invariant: every issued call is
answered exactly once. This module does not get to spend any of that budget
loosening it. Concurrency changes *when* a call runs, never *whether* it runs
or *whether* it is answered -- that is still `agent.py`'s job, unchanged since
chapter 0.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass

from minicodex.agent_types import ToolCall


@dataclass(frozen=True)
class Footprint:
    """What one call touches, as far as the scheduler is willing to guess.

    `reads` and `writes` are resource keys -- for the tools this project has,
    a resource key is a resolved absolute path, so two calls naming the same
    file collide and two calls naming different files do not. The scheduler
    never looks inside a key; it only compares them for equality, which is
    what lets `tools.py` own the one question that actually needs tool
    knowledge (what does "the same resource" mean for *this* tool) while this
    module owns none of it.

    `stateful=True` means "conflicts with everything, including another
    stateful call" -- the answer for a tool whose effect cannot be named as a
    set of resources (`run_shell`: the working directory it carries across
    calls, the network, the rest of the filesystem) and the answer for a call
    the scheduler could not classify at all. Unclassifiable defaults to
    stateful, not to "no footprint" -- an empty `Footprint()` would claim the
    call touches nothing, which is the one claim that is never safe to guess.
    """

    reads: frozenset[str] = frozenset()
    writes: frozenset[str] = frozenset()
    stateful: bool = False


# The one Footprint value that means "do not run this next to anything else."
# A named constant rather than `Footprint(stateful=True)` written out at every
# call site, so grepping for STATEFUL finds every place that gives up on
# classifying a call.
STATEFUL = Footprint(stateful=True)

FootprintFn = Callable[[ToolCall], Footprint]


def conflicts(a: Footprint, b: Footprint) -> bool:
    """Must `a` and `b` never be in flight at the same time?

    Read/read never conflicts -- that is the entire concurrency win this
    chapter has to offer, and it is also the only case this function is
    allowed to say no to. Every other combination -- a write touching what
    the other reads or writes, or either side being stateful -- says yes.
    """
    if a.stateful or b.stateful:
        return True
    return bool(a.writes & b.writes) or bool(a.writes & b.reads) or bool(a.reads & b.writes)


def batches(calls: Sequence[ToolCall], footprint_of: FootprintFn) -> list[list[ToolCall]]:
    """Group calls into ordered batches: run batch N fully before batch N+1.

    List scheduling, not a general solver: for each call, in the order the
    model issued them, its batch is one past the latest batch of anything it
    conflicts with among the calls before it. Two calls that conflict can
    never land in the same batch, by construction -- if they did, the second
    one's minimum batch would have been pushed past the first one's. Two
    calls that do not conflict, directly or through anything between them,
    end up in the same batch and run concurrently.

    O(n^2) in the number of calls in one turn. That number is bounded by how
    many tool calls a model can put in one response, which is small enough
    that the readable quadratic version is the right one to ship.
    """
    fps = [footprint_of(call) for call in calls]
    batch_index: list[int] = []
    for i, fp in enumerate(fps):
        earliest = 0
        for j in range(i):
            if conflicts(fp, fps[j]):
                earliest = max(earliest, batch_index[j] + 1)
        batch_index.append(earliest)

    result: list[list[ToolCall]] = []
    for call, index in zip(calls, batch_index, strict=True):
        while len(result) <= index:
            result.append([])
        result[index].append(call)
    return result
```

### 5.2 测试

新建 `tests/test_scheduler.py`。开头和几个帮手：

```python
"""Chapter 8: which tool calls in one turn may run at the same time.

Test names carry the fault ids from FAULTS.md. The races in here are real --
real OS threads via `asyncio.to_thread`, real files on disk -- with just
enough of a delay inserted at the one line that matters to make the
interleaving land the same way every run instead of most runs. That delay,
and why a bare `time.sleep`-based race is the wrong thing to check in, is
F08-09.
"""

from __future__ import annotations

import asyncio
import functools
import io
import json
import threading
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest

from minicodex.agent import Agent
from minicodex.agent_types import ToolCall
from minicodex.approval import Session
from minicodex.history import ToolResult
from minicodex.model import Completed, TextDelta, ToolCallDelta
from minicodex.patch import Edit, apply_edits
from minicodex.scheduler import STATEFUL, Footprint, batches, conflicts
from minicodex.tools import default_tools, footprint_of

# ---------------------------------------------------------------------------
# helpers -- same shapes as tests/test_faults_ch07.py
# ---------------------------------------------------------------------------


def call(call_id: str, name: str, **arguments: Any) -> ToolCall:
    raw = json.dumps(arguments)
    return ToolCall(call_id, name, arguments, raw)


def delta(call_id: str, index: int, name: str, **arguments: Any) -> ToolCallDelta:
    return ToolCallDelta(call_id=call_id, index=index, name=name, arguments=json.dumps(arguments))


class ScriptedModel:
    """Replays a fixed list of turns. Same shape as interlude A's and chapter 7's."""

    def __init__(self, turns: Sequence[Any]) -> None:
        self.turns = list(turns)
        self.sent: list[list[dict[str, Any]]] = []

    async def stream(self, messages: Sequence[dict[str, Any]]) -> Any:
        self.sent.append([dict(m) for m in messages])
        turn = self.turns[len(self.sent) - 1]
        if isinstance(turn, str):
            yield TextDelta(turn)
        else:
            for d in turn:
                yield d
        yield Completed("stop")


def make_agent(model: Any, root: Path, *, max_concurrent_tools: int = 8) -> Agent:
    """A real agent, wired to real files, with chapter 8's scheduler switched on.

    This is the one non-default call site in the whole test suite:
    `footprint_of` is bound to `root` explicitly, exactly as `__main__.py`
    binds it. Every other Agent built in this project's tests still gets the
    old fully-serial default, on purpose -- see `agent.py`'s docstring.
    """
    session = Session(mode="workspace-write")
    tools = default_tools(root=root, session=session)
    return Agent(
        model,
        tools,
        footprint_of=functools.partial(footprint_of, root=root),
        max_concurrent_tools=max_concurrent_tools,
    )


class PeakTracker:
    """How many conflicting sections were inside their critical section at once.

    A `threading.Lock`, not an `asyncio.Lock`: the code under test runs
    through `asyncio.to_thread`, on real OS threads, and an asyncio primitive
    is not safe to touch from there.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._active = 0
        self.peak = 0

    def enter(self) -> None:
        with self._lock:
            self._active += 1
            self.peak = max(self.peak, self._active)

    def exit(self) -> None:
        with self._lock:
            self._active -= 1

```

> - `call(...)`、`delta(...)`、`ScriptedModel`：和第 7 章测试里的一样。
> - **`make_agent(model, root)`**：一个接着真实文件、**打开了调度器**的 Agent。`footprint_of=functools.partial(footprint_of, root=root)` 这一句就是"打开"——§6、§7 讲。
>   会话用 `workspace-write` 模式，这样 `apply_patch` 不用问人。
> - **`PeakTracker`**：数"同时在里面的最多有几个"。进去时 `enter()`，出来时 `exit()`，`peak` 记下见过的最大值。
>   它用的是 `threading.Lock` 而不是 `asyncio.Lock`：被测的代码有一部分是用 `to_thread` 放在真线程里跑的，协程用的锁在那里不安全。
>   后面好几个测试靠它。

```python
def test_read_read_never_conflicts() -> None:
    a = Footprint(reads=frozenset({"f.txt"}))
    b = Footprint(reads=frozenset({"f.txt"}))
    assert not conflicts(a, b)


def test_F08_01_write_write_same_key_conflicts() -> None:
    a = Footprint(writes=frozenset({"f.txt"}))
    b = Footprint(writes=frozenset({"f.txt"}))
    assert conflicts(a, b)


def test_F08_02_write_read_same_key_conflicts_either_way_round() -> None:
    w = Footprint(writes=frozenset({"f.txt"}))
    r = Footprint(reads=frozenset({"f.txt"}))
    assert conflicts(w, r)
    assert conflicts(r, w)


def test_disjoint_keys_do_not_conflict() -> None:
    a = Footprint(writes=frozenset({"a.txt"}))
    b = Footprint(writes=frozenset({"b.txt"}))
    assert not conflicts(a, b)


def test_F08_03_stateful_conflicts_with_everything_including_another_stateful_call() -> None:
    assert conflicts(STATEFUL, Footprint(reads=frozenset({"f.txt"})))
    assert conflicts(STATEFUL, Footprint())
    assert conflicts(STATEFUL, STATEFUL)


def test_batches_groups_disjoint_calls_into_one_batch() -> None:
    """The entire concurrency win this chapter has to offer, in one example."""
    calls = [call("c1", "read_file", path="a.txt"), call("c2", "read_file", path="b.txt")]
    fps = {"c1": Footprint(reads=frozenset({"a.txt"})), "c2": Footprint(reads=frozenset({"b.txt"}))}

    plan = batches(calls, lambda c: fps[c.call_id])

    assert plan == [calls]


def test_F08_05_a_conflicting_pair_serialises_even_with_an_unrelated_call_between_them() -> None:
    """a and b both write f.txt, with c -- an unrelated read of a different
    file -- issued between them. b must still land strictly after a, because
    a call is only ever pushed *later* by something it conflicts with among
    the calls before it; c does not conflict with either, so it is free to
    join a's batch instead of waiting its turn in the list. "Preserve
    submission order" is true of conflicting calls, not of the whole list --
    a weaker guarantee than it sounds, and the one this chapter actually
    needs (F08-01)."""
    a = call("a", "apply_patch", edits=[{"path": "f.txt"}])
    b = call("b", "apply_patch", edits=[{"path": "f.txt"}])
    c = call("c", "read_file", path="g.txt")
    fps = {
        "a": Footprint(writes=frozenset({"f.txt"})),
        "b": Footprint(writes=frozenset({"f.txt"})),
        "c": Footprint(reads=frozenset({"g.txt"})),
    }

    plan = batches([a, b, c], lambda call: fps[call.call_id])

    assert plan == [[a, c], [b]]


def test_batches_of_an_empty_turn_is_empty() -> None:
    assert batches([], lambda call: STATEFUL) == []


def test_F08_03_an_unclassifiable_call_serialises_with_its_neighbours() -> None:
    """Two read_file calls that would otherwise batch together, with a
    run_shell call (always STATEFUL) issued between them."""
    a = call("a", "read_file", path="x.txt")
    b = call("b", "run_shell", command="true")
    c = call("c", "read_file", path="y.txt")
    fps = {
        "a": Footprint(reads=frozenset({"x.txt"})),
        "b": STATEFUL,
        "c": Footprint(reads=frozenset({"y.txt"})),
    }

    plan = batches([a, b, c], lambda call: fps[call.call_id])

    assert plan == [[a], [b], [c]]
```

> 这九个测试都是纯逻辑，不需要事件循环，也不碰文件：读和读不冲突；写和写冲突；写和读（两个方向）冲突；不同的东西不冲突；
> `STATEFUL` 和一切冲突，包括空脚印和另一个 `STATEFUL`；两个不相干的读进同一批；§5.1 的那个例子；空的一轮；
> 两个读中间夹一个 `STATEFUL`，变成三批。

```bash
git add src/minicodex/scheduler.py tests/test_scheduler.py probe_scheduler.py
git commit -m "feat(scheduler): decide which tool calls in a turn may run together"
```

---

## §6 谁负责说"这个调用碰了什么"

`scheduler.py` 不认识任何工具。真正知道 `read_file` 碰了哪个文件、`apply_patch` 写了哪些文件的，是 `tools.py`。在里面加一个函数：

```python
def footprint_of(call: ToolCall, root: Path) -> Footprint:
    """What one call touches, for the scheduler in chapter 8.

    Only two tools can be named as a set of resources at all: `read_file`
    reads the one path it was given, `apply_patch` writes every path named in
    its edits. Everything else -- `run_shell`'s working directory and
    unbounded reach, `request_permissions` mutating the session every other
    tool call reads from, any tool call this function does not recognise --
    returns `STATEFUL`, which the scheduler treats as conflicting with
    everything, including another stateful call.

    This function re-resolves every path it looks at, and the actual handler
    resolves it again a moment later. Threading the resolved `Path` through
    from here to there would save that second `stat()`, and was tried; it
    also means every tool signature carries a scheduling detail forever, for
    a save too small to measure. The two stay independent on purpose: the
    scheduler is allowed to be conservative (and re-check) about a call it is
    not going to run yet, without the tool that actually runs it inheriting
    any of that.

    A call this function cannot make sense of -- unresolvable arguments,
    unresolvable paths, an `apply_patch` edit that names no path at all --
    resolves to `STATEFUL` rather than raising. The handler is the one place
    that gets to reject bad arguments with an error the model can act on;
    the scheduler's only two moves are "run this next to other things" or
    "don't", and guessing wrong on "don't" costs nothing but concurrency.
    """
    if call.arguments is None:
        return STATEFUL

    if call.name == "read_file":
        path, error = resolve(call.arguments.get("path"), root)
        if error is not None or path is None:
            return STATEFUL
        return Footprint(reads=frozenset({str(path)}))

    if call.name == "apply_patch":
        raw = call.arguments.get("edits")
        if not isinstance(raw, list) or not raw:
            return STATEFUL
        touched: set[str] = set()
        for item in raw:
            if not isinstance(item, dict):
                return STATEFUL
            path, error = resolve(item.get("path"), root)
            if error is not None or path is None:
                return STATEFUL
            touched.add(str(path))
        return Footprint(writes=frozenset(touched))

    return STATEFUL
```

并在开头的 import 里加上 `ToolCall`（来自 `agent_types`）和 `from minicodex.scheduler import STATEFUL, Footprint`，在末尾的 `__all__` 里加上 `"footprint_of"`。

> 四个工具，两种结果：
>
> - **`read_file`** 读一个路径；**`apply_patch`** 写它 `edits` 里列出的每一个路径。路径用第 4 章的 `paths.resolve()` 解析——
>   同一个函数，同一套"必须在仓库里、必须存在"的规则。
> - **`run_shell`** 的影响范围没法用一组资源名说清；**`request_permissions`** 改的是整个会话接下来能做什么。两个都落到最后一行的 `STATEFUL`。
>   **任何这个函数不认识的工具名，也一样。**
> - 中间每一处"看不明白"的分支——参数没解析出来、路径解析失败、`edits` 不是列表、某条编辑没写路径——返回的全是 `STATEFUL`，没有一处是空的 `Footprint()`。
>   docstring 的最后一段说了为什么这里不报错：把话说给模型听是工具自己的事；调度器只有两个选择，"和别的一起跑"或者"不"，在"不"上猜错，损失的只有速度。

用解析后的完整路径当资源名，顺带解决了"同一个文件的不同写法"（在 Windows 上实测）：

```
  x.txt          -> <root>\x.txt
  X.TXT          -> <root>\x.txt
  ./x.txt        -> <root>\x.txt
  sub/../x.txt   -> <root>\x.txt
```

四种写法是同一个资源名，所以它们之间的冲突调度器看得见。（`X.TXT` 那一行是 Windows 的行为——那里文件名不分大小写；在 Linux 上它是另一个文件，不存在，会被归为 `STATEFUL`。）

> docstring 里还记了一个试过又撤销的优化：这个函数解析了一遍路径，真正执行的工具马上又要解析一遍。把解析好的结果传过去能省一次，
> 代价是每个工具的参数里都要多带一个"给调度用的东西"。**调度器可以对一个还没执行的调用保守一点、多查一遍，不需要让真正执行它的工具也背上这件事。**

```python
def test_F08_01_footprint_of_read_file_is_the_resolved_path(tmp_path: Path) -> None:
    (tmp_path / "x.txt").write_text("hi")

    fp = footprint_of(call("c1", "read_file", path="x.txt"), tmp_path)

    assert fp.reads == frozenset({str((tmp_path / "x.txt").resolve())})
    assert fp.writes == frozenset()
    assert not fp.stateful


def test_F08_01_footprint_of_apply_patch_is_every_edited_path(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_text("a")
    (tmp_path / "b.txt").write_text("b")
    edits = [{"path": "a.txt", "old_text": "a", "new_text": "aa"}, {"path": "b.txt"}]

    fp = footprint_of(call("c1", "apply_patch", edits=edits), tmp_path)

    assert fp.writes == {str((tmp_path / "a.txt").resolve()), str((tmp_path / "b.txt").resolve())}
    assert not fp.stateful


def test_F08_03_footprint_of_run_shell_is_stateful(tmp_path: Path) -> None:
    assert footprint_of(call("c1", "run_shell", command="ls"), tmp_path) == STATEFUL


def test_footprint_of_request_permissions_is_stateful(tmp_path: Path) -> None:
    assert footprint_of(call("c1", "request_permissions", needs="x", why="y"), tmp_path) == STATEFUL


def test_footprint_of_unknown_tool_is_stateful(tmp_path: Path) -> None:
    assert footprint_of(call("c1", "get_weather", city="nyc"), tmp_path) == STATEFUL


def test_footprint_of_malformed_arguments_is_stateful(tmp_path: Path) -> None:
    """Same call site chapter 0 uses for `arguments is None`: the model sent
    something that did not parse as a JSON object, so there is nothing to
    classify. The handler still gives the model a real error; the scheduler
    just refuses to guess and runs the call alone."""
    unparsed = ToolCall("c1", "read_file", None, "{not json")
    assert footprint_of(unparsed, tmp_path) == STATEFUL


def test_footprint_of_a_path_outside_the_repository_is_stateful(tmp_path: Path) -> None:
    """`resolve()` refuses the path; the scheduler inherits that refusal
    rather than deciding it means "touches nothing" (Footprint()) or crashing."""
    fp = footprint_of(call("c1", "read_file", path="../../etc/passwd"), tmp_path)
    assert fp == STATEFUL


def test_footprint_of_apply_patch_with_no_path_in_an_edit_is_stateful(tmp_path: Path) -> None:
    fp = footprint_of(
        call("c1", "apply_patch", edits=[{"old_text": "x", "new_text": "y"}]), tmp_path
    )
    assert fp == STATEFUL
```

> 读文件的脚印是解析后的路径；`apply_patch` 的脚印是它涉及的每一个文件；其余六种情况——`run_shell`、`request_permissions`、不认识的工具、
> 没解析出来的参数、仓库外面的路径、没写路径的编辑——全是 `STATEFUL`。

```bash
git add src/minicodex/tools.py tests/test_scheduler.py
git commit -m "feat(tools): declare what read_file and apply_patch touch"
```

---

## §7 接进循环

`agent.py` 里那段从第 0 章起就在的"逐个执行工具"的循环，这一章第一次真的改了。

开头的 import 加一行 `from minicodex.scheduler import STATEFUL, FootprintFn, batches`。一个新常量：

```python
# How many tool calls may actually be running at once, across every batch this
# process ever schedules. A model that reads fifty files in one turn produces
# fifty non-conflicting calls -- one legal batch -- and fifty threads/sockets
# in flight at once is how F08-07 happens without ever mis-scheduling anything.
DEFAULT_MAX_CONCURRENT_TOOLS = 8
```

`Agent.__init__` 多两个参数 `footprint_of: FootprintFn | None = None` 和 `max_concurrent_tools: int = DEFAULT_MAX_CONCURRENT_TOOLS`：

```python
        # What one call touches, for deciding which calls may run together.
        # Defaulting to "everything is STATEFUL" -- never batch anything with
        # anything else -- rather than to some real classifier: every test
        # written before this chapter builds an `Agent` without this argument,
        # and "unknown tool means run it alone" is precisely the conservative
        # rule chapter 2 and chapter 5 already use for input the code cannot
        # reason about (F02-09's environment allowlist, F05-01's "unknown
        # syntax asks"). A caller opts into real concurrency by passing
        # `tools.footprint_of` bound to its own repository root; nothing here
        # runs two calls together it was not told it could.
        self._footprint_of: FootprintFn = footprint_of or (lambda call: STATEFUL)
        self._concurrency = asyncio.Semaphore(max_concurrent_tools)
```

> **默认是"每个调用都是 `STATEFUL`"——也就是谁也不和谁一起跑，和以前完全一样。** 所以以前所有的测试描述的行为一个字都没变。
> 想要并发，创建 Agent 的人得**明确交出**一个 `footprint_of`。`lambda call: STATEFUL`：一个不管给什么都返回 `STATEFUL` 的小函数。

`run()` 改完之后（整个方法）：

```python
    async def run(self, user_message: str) -> RunResult:
        if self.resume_from is not None:
            history = self._attach(self.resume_from)
        else:
            history = History(observer=self.rollout.append)
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

            # Every call runs and every call is answered -- unchanged since
            # chapter 0.  What changed is *when*: `batches()` groups this
            # turn's calls so that two whose footprints do not conflict share
            # one `asyncio.gather()`, and two that do (same file, or either
            # one `STATEFUL`) land in different batches and never overlap.
            # Results are collected by call_id and appended in the model's
            # original order, so the persisted transcript does not gain a
            # second source of nondeterminism on top of "which one happened
            # to finish first."
            plan = batches(turn.tool_calls, self._footprint_of)
            outputs: dict[str, str] = {}
            interrupted_at: int | None = None

            for batch_index, batch in enumerate(plan):
                tasks = [asyncio.ensure_future(self._run_tool(call)) for call in batch]
                try:
                    results = await asyncio.gather(*tasks)
                except (asyncio.CancelledError, KeyboardInterrupt):
                    # gather() cancels every task it is still waiting on when
                    # the await on gather() itself is cancelled -- but reading
                    # .cancelled()/.result() before those cancellations have
                    # actually finished unwinding races the tools that were
                    # mid-flight.  return_exceptions=True waits for that to
                    # settle instead of raising a second time.
                    settled = await asyncio.gather(*tasks, return_exceptions=True)
                    for call, task, outcome in zip(batch, tasks, settled, strict=True):
                        if task.cancelled():
                            outputs[call.call_id] = (
                                "Error: interrupted by the user before this finished. "
                                "It may have run partially, or not at all."
                            )
                        elif isinstance(outcome, BaseException):
                            outputs[call.call_id] = f"Error: interrupted by the user: {outcome}"
                        else:
                            outputs[call.call_id] = outcome
                    interrupted_at = batch_index
                    break
                else:
                    for call, output in zip(batch, results, strict=True):
                        outputs[call.call_id] = output

            if interrupted_at is not None:
                for later_batch in plan[interrupted_at + 1 :]:
                    for call in later_batch:
                        outputs[call.call_id] = (
                            "Error: interrupted by the user before this started."
                        )
                for call in turn.tool_calls:
                    history.add_tool_result(call.call_id, outputs[call.call_id])
                self.rollout.mark("interrupted", turn=turn_index)
                history.add_system_note(interrupted_note())
                return RunResult(
                    final_text, "interrupted", turn_index + 1, history, tuple(compactions)
                )

            for call in turn.tool_calls:
                history.add_tool_result(call.call_id, outputs[call.call_id])

        return RunResult(final_text, "turn_limit", self.max_turns, history, tuple(compactions))
```

> 相对第 7 章，只有最后"执行工具"的那一段变了：
>
> - `plan = batches(turn.tool_calls, self._footprint_of)`：先排好批次。
> - 对每一批：给每个调用建一个任务（`ensure_future`），然后 `gather` 等这一批**全部**结束。**上一批的 `gather` 没返回，下一批的任务根本不会被创建**——
>   所以冲突的调用之间不是"重叠的窗口很窄"，而是**没有窗口**。
> - 结果先收进一个字典 `outputs`（调用 id → 输出），最后**按 `turn.tool_calls` 原来的顺序**写进历史。
> - 被打断的那条分支，§10 讲。

改的是"什么时候跑"，没改"每个调用都会跑、都会被回答"。

### 7.1 回到 F08-01

```python
async def test_F08_01_naive_concurrent_writes_to_one_file_lose_an_edit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The thing you would reach for first: run every call through
    asyncio.gather() and let the threads sort themselves out.

    `read_source` gets a real, small, deterministic delay so both threads are
    guaranteed to have read the *original* content before either one writes --
    which is not a contrived worst case, it is what "both calls started at
    roughly the same time" already looks like without it; the delay only
    removes the small chance this run happened not to land there.
    """
    path = tmp_path / "shared.txt"
    path.write_text("A = 1\nB = 2\n")

    import minicodex.patch as patch_mod

    original_read_source = patch_mod.read_source

    def slow_read_source(p: Path) -> tuple[str, str]:
        text, ending = original_read_source(p)
        time.sleep(0.05)
        return text, ending

    monkeypatch.setattr(patch_mod, "read_source", slow_read_source)

    edit_a = Edit(str(path), "A = 1", "A = 100")
    edit_b = Edit(str(path), "B = 2", "B = 200")

    result_a, result_b = await asyncio.gather(
        asyncio.to_thread(apply_edits, [edit_a], tmp_path),
        asyncio.to_thread(apply_edits, [edit_b], tmp_path),
    )

    # Both calls reported success. Neither one is lying about what *it* did --
    # each computed its own edit correctly from what it read.
    assert "Applied 1 edit" in result_a
    assert "Applied 1 edit" in result_b

    final = path.read_text()
    survived = ("A = 100" in final, "B = 200" in final)
    assert survived != (True, True), (
        f"expected the race to lose one edit; both survived this run "
        f"(final content: {final!r}) -- the delay was not enough on this machine"
    )


async def test_F08_01_the_scheduler_serialises_writes_to_the_same_file(tmp_path: Path) -> None:
    """The fix: the same two edits, run through Agent.run() instead of a bare
    gather(). batches() puts them in separate batches -- checked directly --
    and, because agent.py never starts batch N+1 until batch N's gather() has
    returned, the second write can only ever see the first one's result."""
    path = tmp_path / "shared.txt"
    path.write_text("A = 1\nB = 2\n")

    plan = batches(
        [
            call(
                "call_1",
                "apply_patch",
                edits=[{"path": "shared.txt", "old_text": "x", "new_text": "y"}],
            ),
            call(
                "call_2",
                "apply_patch",
                edits=[{"path": "shared.txt", "old_text": "x", "new_text": "y"}],
            ),
        ],
        functools.partial(footprint_of, root=tmp_path),
    )
    assert len(plan) == 2, "two writes to the same file must never share a batch"

    model = ScriptedModel(
        [
            [
                delta(
                    "call_1",
                    0,
                    "apply_patch",
                    edits=[{"path": "shared.txt", "old_text": "A = 1", "new_text": "A = 100"}],
                ),
                delta(
                    "call_2",
                    1,
                    "apply_patch",
                    edits=[{"path": "shared.txt", "old_text": "B = 2", "new_text": "B = 200"}],
                ),
            ],
            "done",
        ]
    )
    agent = make_agent(model, tmp_path)

    result = await agent.run("apply both edits")

    assert result.stop_reason == "completed"
    final = path.read_text()
    assert "A = 100" in final
    assert "B = 200" in final
```

> - **第一个把 §3 的故障本身钉住**：不经过调度器，直接用 `gather` 让两次 `apply_edits` 同时跑，断言"两处修改没有同时留下"。
>   它在 `read_source` 里插了 0.05 秒的延迟——§3 的探针说明了为什么需要它：没有它，在 Linux 上 30 次里有 4 次撞不上，这个测试就会偶尔变红。
>   `monkeypatch.setattr(模块, "名字", 新函数)`：测试期间把模块里的一个函数换掉，测试结束自动换回来。
> - **第二个是同样两处修改，经过 Agent**：先直接检查 `batches()` 把它们分成了两批；再跑一轮，两处修改都在。

### 7.2 F08-05：结果按什么顺序写进历史

并发之后，谁先跑完不确定了。第 1 章已经让"结果和调用靠 id 对应、不靠位置"，所以对服务端来说顺序不重要。
但写进历史（也就是写进会话文件）的顺序如果跟着"谁先跑完"或者"批次怎么排"变，同样的一轮对话就可能留下不同的记录——平白多一个不确定的来源。
所以规矩是：**按模型发出的顺序写。**

原来守着这条规矩的测试是这样的：两个调用在同一批里，用一个 `asyncio.Event` 让第二个先跑完，然后断言历史里仍然是第一个在前。

**这个测试不可能失败。** `asyncio.gather()` 返回结果的顺序就是传给它的任务的顺序，不管谁先跑完——就算代码直接按批次的顺序把结果写进历史，这个测试看到的也一模一样。
变异测试里把写历史的那两行改成"按 `outputs` 字典的顺序写"，全部测试照样是绿的。

顺序真正可能走样的地方，是 §5.1 说的那种情况：**批次把调用的先后换了**。模型要的是 a、b、c；a 和 b 写同一个文件，c 读另一个；
计划是 `[[a, c], [b]]`，c 在第一批，跑在 b 前面。这时历史里仍然必须是 a、b、c：

```python
async def test_F08_05_history_order_follows_submission_not_completion(tmp_path: Path) -> None:
    """call_2 is made to finish before call_1. If the loop appended results as
    they completed, the transcript would read call_2 then call_1 -- correct by
    chapter 1's rules (matched by call_id, not position) but a second,
    needless source of nondeterminism in what gets written to disk."""
    (tmp_path / "a.txt").write_text("a")
    (tmp_path / "b.txt").write_text("b")

    order: list[str] = []

    async def tracked_read(root: Path, args: dict[str, Any]) -> str:
        # call_1 (a.txt) waits; call_2 (b.txt) finishes immediately and wakes it.
        if args["path"] == "a.txt":
            await release.wait()
        order.append(args["path"])
        return args["path"]

    release = asyncio.Event()

    async def read_a(args: dict[str, Any]) -> str:
        return await tracked_read(tmp_path, {"path": "a.txt"})

    async def read_b(args: dict[str, Any]) -> str:
        result = await tracked_read(tmp_path, {"path": "b.txt"})
        release.set()
        return result

    model = ScriptedModel([[delta("call_1", 0, "read_a"), delta("call_2", 1, "read_b")], "done"])
    agent = Agent(
        model,
        {"read_a": read_a, "read_b": read_b},
        footprint_of=lambda c: Footprint(reads=frozenset({c.name})),
    )

    result = await agent.run("go")

    assert order == ["b.txt", "a.txt"], "call_2 must finish first for this test to mean anything"
    tool_results = [i for i in result.history.items if isinstance(i, ToolResult)]
    assert [r.call_id for r in tool_results] == ["call_1", "call_2"]


async def test_F08_05_history_order_follows_submission_not_the_batch_plan(tmp_path: Path) -> None:
    """The test above cannot fail: `asyncio.gather()` hands results back in the
    order the tasks were given, whichever finished first, so appending them in
    batch order looks identical.  Found by a mutation that did exactly that.

    The order can only really leak when the plan *reorders* calls.  The model
    asks for a, b, c; a and b write the same file and c reads another one, so
    the plan is [[a, c], [b]] -- c runs in the first batch, ahead of b.  The
    history must still read a, b, c.
    """
    (tmp_path / "f.txt").write_text("A = 1\nB = 2\n")
    (tmp_path / "g.txt").write_text("unrelated\n")
    calls = [
        delta(
            "a",
            0,
            "apply_patch",
            edits=[{"path": "f.txt", "old_text": "A = 1", "new_text": "A = 9"}],
        ),
        delta(
            "b",
            1,
            "apply_patch",
            edits=[{"path": "f.txt", "old_text": "B = 2", "new_text": "B = 9"}],
        ),
        delta("c", 2, "read_file", path="g.txt"),
    ]
    model = ScriptedModel([calls, "done"])
    agent = make_agent(model, tmp_path)

    result = await agent.run("go")

    tool_results = [i for i in result.history.items if isinstance(i, ToolResult)]
    assert [r.call_id for r in tool_results] == ["a", "b", "c"]
```

> 第一个是原来的测试，留着（它确认了"后发出的可以先跑完"这件事本身）。第二个是新加的，docstring 说明了前一个为什么守不住这条规矩。

---

## §8 F08-07：并发要有上限

一批里放几个调用，取决于模型这一轮发了多少个互不冲突的调用。模型说"把这五十个文件都读一遍"，`batches()` 会把五十个读全放进一批。
不加限制，就是五十个线程同时去读盘。

`_run_tool` 改完之后：

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

        async with self._concurrency:
            try:
                return await self.tools[call.name](call.arguments)
            except Exception as exc:  # deliberately broad; see the docstring
                return f"Error: {call.name} raised {type(exc).__name__}: {exc}"
```

> - `self._concurrency` 是 `__init__` 里建的 `asyncio.Semaphore(max_concurrent_tools)`，默认 8 个名额。
> - **`async with self._concurrency:` 只包住真正调用工具的那一小段。** 前面"没有这个工具""参数不是合法 JSON"两个提前返回不执行任何工具代码，不该占名额。

`batches()` 管的是**顺序**（谁必须等谁），信号量管的是**数量**（同时最多几个）。两件事互不干扰：一批里可以有五十个调用，同时在执行的永远不超过 8 个。

```python
async def test_F08_07_without_a_cap_every_call_in_a_batch_runs_at_once(tmp_path: Path) -> None:
    tracker = PeakTracker()
    n = 6

    async def slow_probe(args: dict[str, Any]) -> str:
        tracker.enter()
        try:
            await asyncio.sleep(0.03)
            return "ok"
        finally:
            tracker.exit()

    calls = [delta(f"c{i}", i, "probe", n=i) for i in range(n)]
    model = ScriptedModel([calls, "done"])
    agent = Agent(
        model,
        {"probe": slow_probe},
        footprint_of=lambda c: Footprint(reads=frozenset({c.call_id})),
        max_concurrent_tools=n,  # explicitly wide open, for this measurement
    )

    await agent.run("go")

    assert tracker.peak == n


async def test_F08_07_the_default_cap_bounds_concurrency(tmp_path: Path) -> None:
    tracker = PeakTracker()
    n, cap = 6, 2

    async def slow_probe(args: dict[str, Any]) -> str:
        tracker.enter()
        try:
            await asyncio.sleep(0.03)
            return "ok"
        finally:
            tracker.exit()

    calls = [delta(f"c{i}", i, "probe", n=i) for i in range(n)]
    model = ScriptedModel([calls, "done"])
    agent = Agent(
        model,
        {"probe": slow_probe},
        footprint_of=lambda c: Footprint(reads=frozenset({c.call_id})),
        max_concurrent_tools=cap,
    )

    await agent.run("go")

    assert tracker.peak == cap
```

> 六个互不冲突的调用：名额放到 6 个时，`PeakTracker` 量到同时在跑的最多是 6；名额设成 2 时，最多是 2。**F08-07 成立**（不设上限就是全部同时），上限生效。

---

## §9 F08-02、F08-03：量"有没有重叠"

`conflicts()` 的纯逻辑测试已经证明了"写和读冲突""`STATEFUL` 和一切冲突"。但那只证明了**判断**是对的。
这两个测试量的是：接进 Agent 之后，那两件事**真的没有同时发生**。

办法就是 `PeakTracker`：把真正碰文件、真正跑命令的那一小段包起来，进去加一、出来减一，看峰值。

```python
async def test_F08_02_a_read_and_a_write_to_the_same_file_never_overlap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "f.txt"
    path.write_text("before\n")

    import minicodex.patch as patch_mod
    import minicodex.tools as tools_mod

    tracker = PeakTracker()

    original_write_source = patch_mod.write_source

    def tracked_write_source(p: Path, text: str, ending: str) -> None:
        tracker.enter()
        try:
            time.sleep(0.03)
            original_write_source(p, text, ending)
        finally:
            tracker.exit()

    original_read = tools_mod._read

    def tracked_read(p: Path) -> str:
        tracker.enter()
        try:
            time.sleep(0.03)
            return original_read(p)
        finally:
            tracker.exit()

    monkeypatch.setattr(patch_mod, "write_source", tracked_write_source)
    monkeypatch.setattr(tools_mod, "_read", tracked_read)

    model = ScriptedModel(
        [
            [
                delta(
                    "call_1",
                    0,
                    "apply_patch",
                    edits=[{"path": "f.txt", "old_text": "before", "new_text": "after"}],
                ),
                delta("call_2", 1, "read_file", path="f.txt"),
            ],
            "done",
        ]
    )
    agent = make_agent(model, tmp_path)

    result = await agent.run("patch then read")

    assert result.stop_reason == "completed"
    assert tracker.peak == 1, "the read and the write held the file's critical section at once"


async def test_F08_03_two_shell_calls_in_one_turn_never_overlap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`run_shell` is STATEFUL unconditionally, which is a blunter answer than
    "detect that `cd` mutates state" -- and a correct one, since a command
    running concurrently with another can corrupt more than the working
    directory this test-set knows how to check (arbitrary files, network,
    processes). What is checked here is only the part that is checkable:
    two shell calls issued in one turn are never both mid-flight."""
    import minicodex.shell as shell_mod

    tracker = PeakTracker()
    original_run = shell_mod.ShellSession.run

    async def tracked_run(self: Any, command: str) -> str:
        tracker.enter()
        try:
            await asyncio.sleep(0.03)
            return await original_run(self, command)
        finally:
            tracker.exit()

    monkeypatch.setattr(shell_mod.ShellSession, "run", tracked_run)

    model = ScriptedModel(
        [
            [
                delta("call_1", 0, "run_shell", command="echo one"),
                delta("call_2", 1, "run_shell", command="echo two"),
            ],
            "done",
        ]
    )
    agent = make_agent(model, tmp_path)

    result = await agent.run("run both")

    assert result.stop_reason == "completed"
    assert tracker.peak == 1
```

> - 第一个：一轮里一个 `apply_patch(f.txt)`、一个 `read_file(f.txt)`。用 `monkeypatch.setattr` 把写文件的函数和读文件的函数都换成"先登记、睡 0.03 秒、再做事"的版本。峰值必须是 1。
> - 第二个：一轮里两个 `run_shell`，把 `ShellSession.run` 同样包起来。峰值必须是 1。

两个数都是 1，而且不是"多半是 1"——它们被分在不同的批次，前一批没结束，后一批不会开始。

`run_shell` 一律当成 `STATEFUL`，比"分析一下命令，真的用到 `cd` 时才排斥别的调用"粗得多。这是故意的：
那种分析正是第 5 章开头犯过的错——去猜一段 shell 文字的含义。

**F08-02、F08-03：动工前就挡住了。**

---

## §10 F08-08：取消要到达每一个调用，和每一批

第 7 章的规则是：被 Ctrl-C 打断时，每一个已经发出的调用都要有回答，包括还没来得及开始的。这一章要把它从"一个接一个"推广到"一批一批"。

`run()` 里的这一段：

```python
                try:
                    results = await asyncio.gather(*tasks)
                except (asyncio.CancelledError, KeyboardInterrupt):
                    settled = await asyncio.gather(*tasks, return_exceptions=True)
                    for call, task, outcome in zip(batch, tasks, settled, strict=True):
                        if task.cancelled():
                            outputs[call.call_id] = (
                                "Error: interrupted by the user before this finished. "
                                "It may have run partially, or not at all."
                            )
                        elif isinstance(outcome, BaseException):
                            outputs[call.call_id] = f"Error: interrupted by the user: {outcome}"
                        else:
                            outputs[call.call_id] = outcome
                    interrupted_at = batch_index
                    break
```

三件事：

**一，取消会自己传下去。** 正在等 `gather()` 的协程被取消时，`gather()` 会取消它还在等的每一个任务——这部分不用手写。

**二，取消之后怎么读结果，要手写。** 取消不是一瞬间完成的：被取消的任务还要走完自己的收尾。直接去问每个任务"你怎么样了"，可能问早了。
所以**再 `gather` 一次**，带上 `return_exceptions=True`：它会等每个任务真正落定，而且不会再抛一次异常。然后才逐个看——
是被取消的（`task.cancelled()`）、出了别的问题的、还是**在取消到达之前就已经正常做完了的**。

**三，已经做完的，要报告它真实的结果。** 同一批里，一个调用还在跑、另一个已经结束了，这时按下 Ctrl-C：结束了的那个真的做完了事，
不能被一句"被打断了"盖掉——否则模型会以为那个文件没改，而它其实改了。

批次循环结束之后：

```python
            if interrupted_at is not None:
                for later_batch in plan[interrupted_at + 1 :]:
                    for call in later_batch:
                        outputs[call.call_id] = (
                            "Error: interrupted by the user before this started."
                        )
                for call in turn.tool_calls:
                    history.add_tool_result(call.call_id, outputs[call.call_id])
                self.rollout.mark("interrupted", turn=turn_index)
                history.add_system_note(interrupted_note())
                return RunResult(
                    final_text, "interrupted", turn_index + 1, history, tuple(compactions)
                )
```

> 后面还没开始的批次里的每个调用，回答"还没开始就被打断了"——和"没做完"是两句不同的话，因为模型需要分清这两种状态。
> 然后和第 7 章一样：全部回答、写标记、加说明、结束这次运行。

```python
async def test_F08_08_cancelling_mid_batch_answers_that_batch_and_every_later_one(
    tmp_path: Path,
) -> None:
    """Three calls, forced into three separate batches by STATEFUL: the first
    is cancelled while in flight, the second and third never start. All three
    must come back answered -- chapter 7's invariant does not get a
    concurrency-shaped exception."""
    started = asyncio.Event()

    async def slow(args: dict[str, Any]) -> str:
        started.set()
        await asyncio.sleep(60)
        return "never"

    async def never_reached(args: dict[str, Any]) -> str:
        raise AssertionError("a later batch must not start once the run is cancelled")

    model = ScriptedModel(
        [[delta("c1", 0, "slow"), delta("c2", 1, "later"), delta("c3", 2, "later")], "done"]
    )
    agent = Agent(model, {"slow": slow, "later": never_reached})
    task = asyncio.ensure_future(agent.run("go"))
    await started.wait()
    task.cancel()
    result = await task

    assert result.stop_reason == "interrupted"
    assert result.history.unanswered() == ()
    outputs = {i.call_id: i.content for i in result.history.items if isinstance(i, ToolResult)}
    assert "finished" in outputs["c1"]
    assert "before this started" in outputs["c2"]
    assert "before this started" in outputs["c3"]
    assert result.history.to_wire()


async def test_F08_08_a_concurrent_batch_mate_that_already_finished_keeps_its_real_result(
    tmp_path: Path,
) -> None:
    """The call that shares a batch with the one being cancelled, but finishes
    on its own before the cancellation reaches it, must report what it
    actually did -- not get overwritten with an interrupted message it did
    not earn."""
    (tmp_path / "a.txt").write_text("real content")
    started = asyncio.Event()

    async def slow(args: dict[str, Any]) -> str:
        started.set()
        await asyncio.sleep(60)
        return "never"

    async def fast(args: dict[str, Any]) -> str:
        return "finished before the cancel arrived"

    model = ScriptedModel([[delta("c1", 0, "slow"), delta("c2", 1, "fast")], "done"])
    agent = Agent(
        model,
        {"slow": slow, "fast": fast},
        footprint_of=lambda c: Footprint(reads=frozenset({c.name})),  # same batch
    )
    task = asyncio.ensure_future(agent.run("go"))
    await started.wait()
    await asyncio.sleep(0.01)  # give `fast` a chance to actually complete first
    task.cancel()
    result = await task

    outputs = {i.call_id: i.content for i in result.history.items if isinstance(i, ToolResult)}
    assert outputs["c2"] == "finished before the cancel arrived"
    assert "interrupted" in outputs["c1"]
```

> - 第一个：三个调用被分成三批（没给 `footprint_of`，全是 `STATEFUL`）；第一个正在跑时取消。三个都得到了回答：第一个是"没做完"，后两个是"还没开始"，
>   而且后两个的工具函数从未被调用（被调用就抛 `AssertionError`）。
> - 第二个：两个调用在同一批，一个很快做完，一个还在睡；这时取消。做完的那个保留了它真实的输出，另一个是"被打断"。

第 7 章的那几个取消测试一行没改，在这一章的代码上照样通过。**F08-08：动工前就挡住了。**

---

## §11 没有复现的两条

### F08-04：一个调用抛异常，会不会连累同一批里别的调用？

猜的是会：`gather()` 在某个任务抛异常时，会把异常往外抛，别的任务的结果就拿不到了。实际：

```python
async def test_F08_04_a_raising_call_does_not_cancel_its_concurrent_siblings(
    tmp_path: Path,
) -> None:
    """`_run_tool` already turns every ordinary exception into a returned
    string (chapter 0, F00-05) -- which means the coroutine `asyncio.gather()`
    awaits here never raises Exception for an ordinary tool failure, and
    gather's fail-fast/cancel-siblings behaviour, which only ever triggers on
    a raised exception, simply never sees one. Concurrency does not reopen
    F00-05; this pins that down rather than assuming it."""
    (tmp_path / "a.txt").write_text("a")
    (tmp_path / "b.txt").write_text("b")

    async def explode(args: dict[str, Any]) -> str:
        raise ValueError("disk on fire")

    model = ScriptedModel(
        [
            [
                delta("call_1", 0, "read_file", path="a.txt"),
                delta("call_2", 1, "explode"),
                delta("call_3", 2, "read_file", path="b.txt"),
            ],
            "done",
        ]
    )
    session = Session(mode="workspace-write")
    tools = default_tools(root=tmp_path, session=session)
    tools["explode"] = explode
    agent = Agent(model, tools, footprint_of=functools.partial(footprint_of, root=tmp_path))

    result = await agent.run("go")

    outputs = {i.call_id: i.content for i in result.history.items if isinstance(i, ToolResult)}
    assert outputs["call_1"] == "a"
    assert outputs["call_3"] == "b"
    assert "ValueError" in outputs["call_2"]
    assert "disk on fire" in outputs["call_2"]
```

三个调用在一批，中间那个抛 `ValueError`。另外两个的结果完好，中间那个得到一条包含 `ValueError` 的错误消息。

原因不是运气，是结构：第 0 章就把 `_run_tool()` 写成了"普通的异常一律变成一条返回的字符串"。`gather()` 等的那些协程，对一次普通的工具失败来说**根本不会抛异常**。
这条故障不是"这一章顺手修好了"，而是"这一章根本没有制造让它发生的条件"。测试钉住的是这句话。

### F08-06：模型默认了一个顺序，调度器不知道

设想的场景：模型以为"先改这个、再读那个"会按顺序发生，而调度器认为两者互不相干，同时执行了。

在这一章的工具里，这个场景搭不出来。`read_file` 和 `apply_patch` 的脚印都是解析出来的真实路径；改一个已经存在的文件，不可能改变另一个不相干的文件的内容。

```python
async def test_F08_06_unrelated_files_really_are_independent(tmp_path: Path) -> None:
    """NOT REPRODUCED, and not reproducible with this tool set: F08-06 asks
    what happens when two calls the scheduler thinks are independent actually
    have a hidden dependency. For that to happen here, some tool's declared
    footprint would have to be wrong -- name a resource it does not really
    touch, or omit one it does. `read_file` and `apply_patch` both resolve a
    real path and declare exactly that path; there is no way to construct two
    calls to different, already-existing files where editing one changes the
    other. The fault becomes live the day a tool is added whose footprint is
    a guess rather than a resolved path -- an MCP tool in chapter 9 is exactly
    that shape."""
    (tmp_path / "a.txt").write_text("a")
    (tmp_path / "b.txt").write_text("original-b")

    model = ScriptedModel(
        [
            [
                delta(
                    "call_1",
                    0,
                    "apply_patch",
                    edits=[{"path": "a.txt", "old_text": "a", "new_text": "changed-a"}],
                ),
                delta("call_2", 1, "read_file", path="b.txt"),
            ],
            "done",
        ]
    )
    agent = make_agent(model, tmp_path)

    result = await agent.run("go")

    outputs = {i.call_id: i.content for i in result.history.items if isinstance(i, ToolResult)}
    assert outputs["call_2"] == "original-b"
    assert (tmp_path / "a.txt").read_text() == "changed-a"
```

这条故障要成立，需要一个**脚印可能是错的**工具——资源名不是从真实路径解析出来的，而是猜的，或者是别人声称的。
下一章要接的远程工具正是这个形状。所以这一条记为**"没有复现，也还没有能让它复现的工具"**，而不是"不存在"。

---

## §12 意外：两个审批问题同时出现在终端上

到这里清单上的九条都有了着落。改写这一章时，把调度器和前面几章的东西挨个对了一遍，问的是：**现在哪些以前不可能同时发生的事，可以同时发生了？**

找到一件。两个 `apply_patch` 改**两个不同的文件**——互不冲突，调度器把它们放进同一批，这是对的。
但如果会话是 `read-only` 模式，改文件要先问人（第 5 章）。于是两个调用**同时去问**。

用第 5 章的 `CliApprover` 实测。输入准备的是先 `y` 后 `n`——用户的意思是"第一个问题同意，第二个不同意"：

```
--- what the terminal showed ---

  the agent wants to run:
    edit files in tmpyzakgw57/
  editing files, which read-only does not permit
  [y] once  [n] no  [e] edit

  the agent wants to run:
    edit files in tmpyzakgw57/
  editing files, which read-only does not permit
  [y] once  [n] no  [e] edit

--- after answering 'y' then 'n' ---
{'a.txt': 'x', 'b.txt': 'y'}
```

三件事，一件比一件糟：

1. **两个问题在任何一个得到回答之前，都已经印在屏幕上了。**
2. **两个问题长得一模一样**——都只说"编辑这个目录里的文件"，没说是哪个文件。
3. **用户对第一个问题的 `y`，批准的是第二个调用**：`a.txt` 没变，被改的是 `b.txt`。两个线程都在等键盘，谁先读到算谁的。

调度器没有错：这两次**编辑**确实互不冲突。冲突的是那两个**问题**——它们争的是同一个终端，和终端前面的同一个人。
这个冲突不在任何一个调用的"脚印"里，因为它不是文件。

这是一个安全问题，不只是界面问题：**用户批准的东西，和实际被执行的东西，不是同一个。** 第 5 章整章在保证的，就是这两者是同一个。

修法是让终端一次只问一个问题。`approval.py` 的 `CliApprover` 改两处：

```python
    def __init__(self, *, stream_in=None, stream_out=None) -> None:
        self._in = stream_in
        self._out = stream_out
        # One question on the terminal at a time. ...
        self._one_at_a_time = asyncio.Lock()

    async def ask(self, request: ApprovalRequest) -> ApprovalReply:
        async with self._one_at_a_time:
            return await asyncio.to_thread(self._ask_blocking, request)
```

改完的整个类：

```python
class CliApprover:
    """Ask on the terminal.

    `input()` blocks, and a blocking call inside `async def` stalls the whole
    event loop -- the same rule chapter 1 hit with `Path.read_text()` and
    chapter 2 with `subprocess.Popen`.  Here it is arguably harmless, since
    there is nothing to do while a human decides, but ruff's ASYNC rules do not
    know that and neither will the next reader.  `to_thread` costs one line.
    """

    def __init__(self, *, stream_in=None, stream_out=None) -> None:
        self._in = stream_in
        self._out = stream_out
        # One question on the terminal at a time.  Chapter 8 lets two calls run
        # together, and two edits to two different files do not conflict -- so
        # in a session where editing needs approval, both asked at once.  Two
        # identical prompts were printed before either answer was read, and the
        # answers went to whichever thread reached `readline()` first: measured,
        # the user's "y" to the first question on screen edited the *second*
        # file.  The lock lives here rather than on the session because the
        # thing that cannot be shared is the terminal, and every session that
        # asks through this object shares it.
        self._one_at_a_time = asyncio.Lock()

    async def ask(self, request: ApprovalRequest) -> ApprovalReply:
        async with self._one_at_a_time:
            return await asyncio.to_thread(self._ask_blocking, request)

    def _ask_blocking(self, request: ApprovalRequest) -> ApprovalReply:
        import sys

        out = self._out or sys.stdout
        read = self._in.readline if self._in is not None else sys.stdin.readline

        print(f"\n  the agent wants to run:\n    {request.what}", file=out)
        print(f"  {request.reason}", file=out)
        choices = ["[y] once", "[n] no", "[e] edit"]
        if request.suggested_rule is not None:
            prefix = " ".join(request.suggested_rule)
            choices[1:1] = [
                f"[s] always this session ({prefix})",
                f"[p] always in this project ({prefix})",
            ]
        print("  " + "  ".join(choices), file=out)
        out.flush()

        answer = (read() or "n").strip().lower()

        if answer == "e":
            print("  new command: ", file=out, end="")
            out.flush()
            edited = (read() or "").strip()
            # An empty edit is not an approval of the original.  Treating it as
            # one turns a slip of the return key into a yes.
            return ApprovalReply(bool(edited), edited or request.what)
        if answer == "s" and request.suggested_rule is not None:
            return ApprovalReply(True, request.what, remember="session")
        if answer == "p" and request.suggested_rule is not None:
            return ApprovalReply(True, request.what, remember="project")
        return ApprovalReply(answer == "y", request.what)
```

> - 一把 `asyncio.Lock`：第一个来问的拿到锁，问完、得到回答、放开锁，第二个才能开始问。等锁的人按到达的先后排队，
>   所以问题出现的顺序就是模型发出调用的顺序。
> - **锁放在 `CliApprover` 上，而不是放在会话上或者调度器里**：不能共用的东西是**终端**，而所有通过这个对象问问题的地方共用的就是它。
> - 测试用的 `AllowAll`、`DenyAll` 不需要锁：它们不碰终端。

```python
class SlowTyping(io.StringIO):
    """A terminal whose user takes a moment to answer, like a real one."""

    def readline(self, *args: Any) -> str:
        time.sleep(0.03)
        return super().readline(*args)


async def test_two_calls_that_both_need_approval_ask_one_at_a_time(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two edits to two different files do not conflict, so they share a batch.

    In a session where editing needs approval, each one asks before it runs --
    and before `CliApprover` had a lock, both asked at once: two identical
    prompts on screen before either answer was read, and the answers handed to
    whichever thread reached `readline()` first.  Measured with the answers
    "y" then "n": the *second* file was the one edited.  The scheduler was
    right that the edits do not conflict.  The two questions do -- over the one
    terminal and the one human reading it.
    """
    from minicodex.approval import CliApprover

    (tmp_path / "a.txt").write_text("x\n")
    (tmp_path / "b.txt").write_text("x\n")

    tracker = PeakTracker()
    original = CliApprover._ask_blocking

    def tracked(self: Any, request: Any) -> Any:
        tracker.enter()
        try:
            return original(self, request)
        finally:
            tracker.exit()

    monkeypatch.setattr(CliApprover, "_ask_blocking", tracked)

    approver = CliApprover(stream_in=SlowTyping("y\nn\n"), stream_out=io.StringIO())
    session = Session(mode="read-only", policy="on-request", approver=approver)
    model = ScriptedModel(
        [
            [
                delta(
                    f"call_{name}",
                    index,
                    "apply_patch",
                    edits=[{"path": name, "old_text": "x", "new_text": "y"}],
                )
                for index, name in enumerate(("a.txt", "b.txt"))
            ],
            "done",
        ]
    )
    agent = Agent(
        model,
        default_tools(root=tmp_path, session=session),
        footprint_of=functools.partial(footprint_of, root=tmp_path),
    )
    plan = batches(
        [
            call(f"call_{name}", "apply_patch", edits=[{"path": name}])
            for name in ("a.txt", "b.txt")
        ],
        functools.partial(footprint_of, root=tmp_path),
    )
    assert len(plan) == 1, "different files: the scheduler is right to run these together"

    await agent.run("edit both")

    assert tracker.peak == 1, "two approval prompts were waiting on the terminal at once"
    # The first question shown is the first call's, and "y" was the answer to it.
    assert (tmp_path / "a.txt").read_text() == "y\n"
    assert (tmp_path / "b.txt").read_text() == "x\n"
```

> - **`SlowTyping`**：一个"回答要花一点时间"的假键盘（`readline` 先睡 0.03 秒）。真的终端就是这样的——人不会瞬间回答；
>   没有这一点延迟，两个线程的先后就看不出来。
> - 测试先确认前提：这两个调用**确实**被排进了同一批。然后跑一轮，断言两件事：同时在等回答的问题最多只有 1 个（`PeakTracker` 包着 `_ask_blocking`）；
>   `y` 批准的是 `a.txt`，`n` 拒绝的是 `b.txt`。

**这一章没有解决第 2 点**：两个问题的文字仍然一样，都不说是哪个文件。那是第 5 章的设计（门在看参数之前就问）留下的，记进了 `FAULTS.md`。
现在至少问题是一个一个、按顺序来的。

> **给系统加并发时，要问的不只是"哪些数据会被同时碰到"，还有"哪些以前排着队的事，现在会同时发生"。**
> 文件是资源，终端是资源，**人的注意力也是资源**——而最后这一种，不会出现在任何一个函数的参数里。

```bash
git add src/minicodex/agent.py src/minicodex/approval.py tests/test_scheduler.py
git commit -m "feat(agent): run a turn's tool calls in batches, bounded, and ask the user one thing at a time"
```

---

## §13 在命令行里打开它

调度器默认是关着的（§7）。真正打开它的，是整个项目里唯一一处同时知道"用的是真实的文件系统"和"仓库在哪"的地方——`__main__.py` 里创建 Agent 的那一段。

开头的 import 加上 `import functools`，`from minicodex.tools import ...` 那一行加上 `footprint_of`。`_ask` 里：

```python
    root = Path.cwd().resolve()
    agent = Agent(
        llm,
        default_tools(root=root, session=session),
        ...
        # Opting in to chapter 8's scheduler: bound to this run's root, since
        # that is what turns a path in an argument dict into a resource key.
        # Every test suite from chapters 0-7 builds an Agent without this and
        # gets the old fully-serial behaviour -- this is the one call site
        # that asks for anything else.
        footprint_of=functools.partial(footprint_of, root=root),
    )
```

> `functools.partial(footprint_of, root=root)`：`tools.footprint_of` 要两个参数（调用、仓库根目录），而 Agent 需要的是只要一个参数的函数。
> `partial` 把 `root` 先固定下来。工具和脚印用的是**同一个** `root`。

### 13.1 这一行没有测试守着

变异测试里把上面那一行 `footprint_of=...` 删掉：**所有测试都是绿的。**

想一想这意味着什么。没有这一行，Agent 用的是默认值——每个调用都单独跑。于是这一章的全部东西都还在、都正确、都有测试，**并且在真正使用时一次都不会运行**。
没有任何东西会报错；只是不会变快。

这和第 6 章"校准机制存在、正确、但从来没收到过数据"是同一个形状。补一个测试：

```python
def test_the_cli_is_where_the_scheduler_is_switched_on(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An `Agent` built without `footprint_of` runs every call alone.  That is
    the safe default -- and it means that if the one line in `__main__.py` that
    passes it were deleted, everything in this chapter would still be correct,
    still be tested, and never run.  Found by deleting that line: no test
    noticed.  The same shape as chapter 6's calibration that never received
    its data: a mechanism that exists, works, and is switched off.
    """
    import minicodex.__main__ as cli
    from minicodex.agent import RunResult
    from minicodex.history import History

    seen: dict[str, Any] = {}

    class Spy(Agent):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            seen.update(kwargs)
            super().__init__(*args, **kwargs)

        async def run(self, user_message: str) -> RunResult:
            return RunResult("ok", "completed", 1, History())

    (tmp_path / "x.txt").write_text("hi")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, "Agent", Spy)

    assert cli.main(["ask", "anything", "--session-dir", str(tmp_path / "sessions")]) == 0

    classify = seen.get("footprint_of")
    assert classify is not None, "the CLI built an Agent with the scheduler switched off"
    footprint = classify(call("c1", "read_file", path="x.txt"))
    assert footprint.reads == {str((tmp_path / "x.txt").resolve())}
```

> - 它不联网：用 `monkeypatch.setattr(cli, "Agent", Spy)` 把命令行模块里的 `Agent` 换成一个"记下自己收到了什么参数、`run()` 直接返回"的子类。
>   `class Spy(Agent)`：继承真正的 `Agent`，只改两个方法；`super().__init__(...)` 调用原来的初始化。
> - `monkeypatch.chdir(tmp_path)`：把当前目录临时换到一个空目录，命令行产生的文件（录像、会话）都落在那里。
> - 然后真的调用 `cli.main([...])`，看 Agent 收到的 `footprint_of`：必须有，而且对一个 `read_file` 调用，给出的是当前目录下那个文件的完整路径。

### 13.2 量一下

三个各要 0.2 秒的调用（Windows 实测）：

```
  one at a time (the default)              0.61s
  three reads of three different things    0.20s
```

不打开调度器，0.61 秒（三个 0.2 秒排队）；打开之后，三个互不相干的读，0.20 秒。

```bash
git add src/minicodex/__main__.py tests/test_scheduler.py
git commit -m "feat(cli): switch the scheduler on, and pin that it is on"
```

---

## §14 逐条验证

### 14.1 全量，两个系统

```
Windows
$ uv run pytest
1321 passed, 9 skipped in 13.90s

Linux（WSL，Python 3.12）
$ uv run pytest
1330 passed in 10.91s
```

```
$ uv run ruff check
All checks passed!
$ uv run ruff format --check
49 files already formatted
```

### 14.2 变异测试

十五个决定，每次撤销一个（Linux，临时拷贝）：

```
baseline: 0 failed

mutation                                                     failed  first tests to notice
conflicts: two writes to one file may overlap                     3  test_F08_01_the_scheduler_serialises_writes_to_the_same_file, test_F08_01_write_w
conflicts: a read may overlap a write                             2  test_F08_02_a_read_and_a_write_to_the_same_file_never_overlap, test_F08_02_write_
conflicts: a stateful call may run next to others                 4  test_F08_03_an_unclassifiable_call_serialises_with_its_neighbours, test_F08_03_st
batches: put every call in the first batch                        6  test_F08_01_the_scheduler_serialises_writes_to_the_same_file, test_F08_02_a_read_
footprint: run_shell and unknown tools touch nothing              4  test_F08_03_footprint_of_run_shell_is_stateful, test_F08_03_two_shell_calls_in_on
footprint: unparsed arguments touch nothing                       1  test_footprint_of_malformed_arguments_is_stateful
footprint: a path that does not resolve touches nothing           1  test_footprint_of_a_path_outside_the_repository_is_stateful
footprint: apply_patch declares only its first file               1  test_F08_01_footprint_of_apply_patch_is_every_edited_path
agent: every call is concurrent by default                        1  test_F08_08_cancelling_mid_batch_answers_that_batch_and_every_later_one
agent: no cap on concurrency                                      1  test_F08_07_the_default_cap_bounds_concurrency
agent: results go into the history as they finish                 1  test_F08_05_history_order_follows_submission_not_the_batch_plan
cancel: a batch mate that finished is reported as interrupted      1  test_F08_08_a_concurrent_batch_mate_that_already_finished_keeps_its_real_result
cancel: later batches still run                                   4  test_F07_04_cancelling_a_tool_answers_every_issued_call, test_F07_04_the_run_ends
approval: let two prompts onto the terminal at once               1  test_two_calls_that_both_need_approval_ask_one_at_a_time
cli: do not turn the scheduler on                                 1  test_the_cli_is_where_the_scheduler_is_switched_on

15/15 caught
tree green again: True
```

这张表第一次跑出来时有两条没被抓住，就是 §7.2 和 §13.1 的那两个新测试的来历：
"结果按完成的先后写进历史"和"命令行没有打开调度器"。**两条都是"代码是对的，但没有任何测试在看"。**

### 14.3 对照清单

| 编号 | 猜测 | 结果 |
|---|---|---|
| F08-01 | 两个调用改同一个文件，丢一个 | **成立**（Windows 30/30，Linux 26/30 到 30/30），而且是静默的 |
| F08-02 | 读和写重叠 | **动工前就挡住**：峰值 1 |
| F08-03 | 两条 shell 命令同时跑 | **动工前就挡住**：峰值 1 |
| F08-04 | 一个抛异常，连累同一批 | **没有复现**，原因在第 0 章 |
| F08-05 | 结果的顺序乱了 | **动工前就挡住**；但守着它的测试原来是空的，补了一个 |
| F08-06 | 模型默认的顺序 | **没有复现**，现在的工具搭不出这个场景 |
| F08-07 | 并发没有上限 | **成立**（不设上限 6/6 同时）；加了信号量 |
| F08-08 | 取消没传到每个调用 | **动工前就挡住** |
| F08-09 | 只在慢机器上出现 | **不成立，但也不是"哪里都必然出现"**：Windows 不加延迟 30/30，Linux 26/30 |

---

## §15 收工

### 15.1 这一章的文件

| 文件 | 状态 | 在哪一节 |
|---|---|---|
| `src/minicodex/scheduler.py` | 新增 | §4、§5 |
| `src/minicodex/tools.py` | 加一个函数 | §6 |
| `src/minicodex/agent.py` | 改动 | §7、§8、§10 |
| `src/minicodex/approval.py` | 改两处 | §12 |
| `src/minicodex/__main__.py` | 改几行 | §13 |
| `tests/test_scheduler.py` | 新增 | 分散在各节 |
| `probe_scheduler.py` | 新增 | §3 |

### 15.2 提交、推送、PR

```
feat(scheduler): decide which tool calls in a turn may run together
feat(tools): declare what read_file and apply_patch touch
feat(agent): run a turn's tool calls in batches, bounded, and ask the user one thing at a time
feat(cli): switch the scheduler on, and pin that it is on
```

```bash
git push -u origin feat/scheduler
```

PR 描述里要如实写的：

> - F08-04 和 F08-06 **没有复现**，原因分别是第 0 章的 `_run_tool` 和"现在没有脚印会出错的工具"。
> - `run_shell` 一律当成 `STATEFUL`，不分析命令内容。
> - 审批的提示不说是哪个文件，两个提示的文字可能完全一样。现在保证了它们一个一个、按顺序出现；提示本身没改。
> - 测试里的竞态用了 0.05 秒的人为延迟。探针显示：在 Linux 上去掉它，30 次里有 4 次撞不上。

### 15.3 自己审一遍

**1 · "模型给的顺序会被保留"——这句话到底成不成立？**

**执行**的顺序不保留：不冲突的调用会跑到前面的批次里去（§5.1）。**写进历史**的顺序保留（§7.2）。两句话要分开说，而且各有一个测试。

**2 · 测试里用真的 `time.sleep` 和真的线程造竞态，CI 机器忙的时候会不会偶尔出错？**

用探针量过：加了 0.05 秒延迟之后，两个系统上都是 30/30。如果哪天它真的出错了，那本身就是一条新的故障，值得单独记下来，而不是把延迟调大然后装作没看见。

**3 · `run_shell` 一律不并发，是不是太粗了？**

是粗，而且是故意的。更细的做法需要理解命令的内容。codex 自己对这个问题的答案更粗（§16）。

**4 · `footprint_of()` 解析了一遍路径，工具自己又解析一遍。**

试过把结果传过去，撤销了，理由在 §6。

**5 · 锁放在 `CliApprover` 上。如果以后有另一种问人的方式呢？**

那它得自己保证"一次只问一个"——这个要求属于"占着一个人的注意力的东西"，不属于调度器。现在只有终端这一种，所以只有这一把锁。

---

## §16 codex 是怎么做的

- **它的工具调用确实是并发的**：每个调用一到就被放进一个"保持顺序的队列"，真正的执行各自在独立的任务里。那个"保持顺序"对应的就是 §7.2：结果按调用的顺序取出。
- **但它没有按资源细分的调度**。codex 给每个工具一个粗的开关："这个工具支持并行吗"，默认不支持。支持的工具一起拿一把**读锁**，不支持的拿**写锁**——
  是一整轮范围的读写互斥，而不是"这两个调用碰的是不是同一个文件"。这一章按路径判断冲突的做法，在 codex 的源码里没有；这是这一章比它细的地方。
- **它的 shell 工具反而是允许并行的**：先后顺序由它自己的持久会话管理来保证，而不是靠一把全局的锁。这一章的 `run_shell` 没有那一层，只能选更粗的那一半。
- **它有一组专门测并行的测试**，办法是量时间：两个各睡 300 毫秒的工具，总耗时必须明显少于两者之和。和 `PeakTracker` 是同一个想法的两种写法——
  **光靠"返回了正确的结果"，证明不了两个调用真的同时跑过；要么量时间，要么数同时在里面的有几个。**

---

## §17 回头看：这一章撞到了什么

**预测到了，并且成立的：** F08-01、F08-07。**预测到了，动工前就挡住的：** F08-02、F08-03、F08-05、F08-08。
**没有复现的：** F08-04、F08-06。**预测的方向不对的：** F08-09。

**没预测到的：**

| 故障 | 怎么发现的 | 挡住它的东西 |
|---|---|---|
| "保留模型给的顺序"比听起来弱：不冲突的调用会排到前面去 | 🟢 第一个测试就红了 | 改测试，改说法 |
| **两个审批问题同时出现，回答被张冠李戴** | 🟣 改写本章时，逐个问"现在什么会同时发生" | `CliApprover` 里的锁 |
| 审批的提示不说是哪个文件 | 🟠 看上面那次实测的屏幕 | **没修**，记录 |
| **守着"历史顺序"的测试不可能失败** | ⚪ 变异测试 | 换一个批次会改变先后的场景 |
| **命令行那一行删掉，没有测试会红** | ⚪ 变异测试 | 一个真的调用 `main()` 的测试 |
| "不需要延迟也必然复现"只在一台机器上成立 | 🟠 改写本章时，在 Linux 上重跑探针 | 测试里保留延迟；两组数字都写出来 |

标记：🔴 崩溃 · 🟡 静默 · 🟢 边界测试 · 🔵 长跑 · 🟠 看日志 · 🟣 审查 · ⚪ 工具

---

## 如果你只记住三件事

1. **把循环换成 `gather()` 只要一行，而它默认打开的不是"更快"，是"任意两个调用可以同时碰同一个东西"。**
   两个 `apply_patch` 改同一个文件，都报告成功，其中一个悄悄丢了。这个问题从第 4 章就在，只是一直没有入口。

2. **"能不能一起跑"可以提前算出来，而算的时候最要紧的是那个默认值。**
   脚印、冲突、批次，全是不需要事件循环就能测的纯函数。真正要小心的不是算法，而是：猜不出一个调用碰了什么时，
   当它**和一切都冲突**，而不是当它**什么都不碰**。

3. **并发之后，去找那些"以前排着队、现在会同时发生"的事——包括不是数据的那些。**
   两个审批问题同时印在屏幕上，回答给错了对象。文件有路径，可以写进脚印；一个人的注意力没有。

---

## 动手练习

1. 把 `agent.py` 里按批次执行的那一段，换回第 7 章的逐个执行。跑 `uv run pytest tests/test_scheduler.py`，看哪些红、哪些绿。
   `conflicts()`、`batches()`、`footprint_of()` 的那些测试照样是绿的——想一想这说明了"测调度的算法"和"测调度真的接进了循环"是怎样的两件事。

2. 在你自己的机器上跑 `uv run python probe_scheduler.py`，和 §3 的两组数字比一比。延迟为 0 的那一行是多少？

3. 把 `CliApprover.ask` 里的 `async with self._one_at_a_time:` 去掉，跑 §12 的那个测试，多跑几次。它每次都红吗？
   再把 `SlowTyping` 里的 `time.sleep(0.03)` 也去掉，再跑。想一想为什么测试里需要一个"慢慢打字的用户"。

4. 给 `Footprint` 加第三种资源：`appends`（比如一个假想的"往日志末尾追加一行"的工具）。两个 `appends` 同一个文件，该不该冲突？
   （提示：第 7 章在两个系统上量过两个进程往同一个文件追加会怎样。）一个 `appends` 和一个 `writes` 呢？改 `conflicts()`，并给每种组合写一个测试。

5. §12 里没解决的那一点：让审批的提示说出是哪个文件。先别写代码，先回答：第 5 章为什么把门放在"看参数"之前？
   要说出文件名，门就得先看参数——这会不会让某条路径绕过门？

下一章讲工具太多的时候怎么办：工具不再只是自己写的那几个，还有别的程序提供的。那些工具"碰了什么"，不再是一个可以解析出来的路径，
而是对方的一句话——F08-06 那条"没有复现"的故障，到那时才第一次有机会真的发生。
