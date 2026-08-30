# 第 8 章 · 工具并发

> **代码**：`steps/step08_concurrency/`
> **分支**：`feat/scheduler`
> **产出**：模型一轮里发起三个互不相干的调用，真的同时跑；同一个文件的两次编辑，
> 或者同一轮里的两条 shell 命令，仍然一次只跑一个
> **你需要**：什么都不需要。本章 28 个测试没有一个联网，其中两个是真实的文件系统竞态，
> 用一个可控的小延迟把它们从"通常会撞见"变成"每次都撞见"。

---

## §1 这一章要做出来的东西

第 7 章结束的时候，Agent 能崩、能续、能被 Ctrl-C 干净地打断。它处理一轮里
多个工具调用的方式，从第 0 章到现在没变过一行：

```python
for index, call in enumerate(turn.tool_calls):
    output = await self._run_tool(call)
    ...
    history.add_tool_result(call.call_id, output)
```

一个接一个。模型说"读 `a.py`，读 `b.py`，读 `c.py`"，三个 `read_file`
互相之间没有任何关系——`b.py` 的内容不取决于 `a.py` 读没读过——但 Agent
还是排队等第一个的磁盘 IO 返回，才发起第二个。

这一章要做的事：**该同时跑的，同时跑；不该同时跑的，一次也不能重叠。**

第二句和第一句一样重要。上一章刚刚把"每个 call 恰好一个 output"从一条规则
变成了在异常路径上也成立的不变量；这一章如果只顾着让调用变快，很容易在别处
撞出一个新问题——两个 `apply_patch` 同时改一个文件，后写的把先写的悄悄冲掉，
两边都报告"成功"。

先看这个故障长什么样。

---

## §2 先写一坨：全部丢给 `gather()`

最直接的写法。`for` 循环换成 `asyncio.gather()`：

```python
outputs = await asyncio.gather(*(self._run_tool(c) for c in turn.tool_calls))
for call, output in zip(turn.tool_calls, outputs):
    history.add_tool_result(call.call_id, output)
```

三行改两行。跑一遍三个 `read_file` 的历史记录，确实比原来快——三次磁盘 IO
重叠在一起，而不是排队。看起来这一章到这就能收工。

那就拿它去撞一下"两个 `apply_patch` 改同一个文件"这个场景，`apply_edits()`
是第 4 章写的，读文件、算好新内容、写回去，中间没有任何锁：

```python
edit_a = Edit(str(path), "A = 1", "A = 100")
edit_b = Edit(str(path), "B = 2", "B = 200")

result_a, result_b = await asyncio.gather(
    asyncio.to_thread(apply_edits, [edit_a], root),
    asyncio.to_thread(apply_edits, [edit_b], root),
)
```

`probe_scheduler.py` 把这个场景跑了 30 遍：

```
=== F08-01 / F08-09: does the naive race need a deliberate delay? (n=30) ===
    read_source delay= 0.000s   edit lost: 30/30
    read_source delay= 0.001s   edit lost: 30/30
    read_source delay= 0.050s   edit lost: 30/30
```

**30 次全丢。** 而且不是"跑出异常"——两次调用都老老实实返回了
`Applied 1 edit(s).`，模型会认为两处修改都成功了。文件里实际只有一处改动，
另一处凭空消失,没有任何报错、任何警告。这正是 §6.2 那张分类表里最贵的一类：
🟡 静默错误。

清单原本预测这条故障"只在慢机器上、竞态窗口够宽的时候才会撞见"（F08-09）。
把 `read_source` 里人为插的延迟从 0.05 秒一路调到 0 秒——**丢失率没有变**，
还是 30/30。这台机器上，`open()`、`locate()`、线程切换本身花的时间就已经
足够两个线程都读到改动前的内容。故障方法论那张表说"发现方式决定了故障有多
容易被漏掉"；这条故障属于最容易漏掉的一类，不是因为它难以复现，而是因为它
从不主动报错——你必须专门去读写完之后的文件内容，才会发现两处改动只剩了一处。

`for` 循环换成 `gather()` 这一步，删掉的不是"慢"，是"谁先谁后"这件事本身
存在的证据。原来的顺序执行里，`apply_patch` 从来不会跟另一个 `apply_patch`
同时跑，所以这个 bug 一直都在代码里，只是从第 4 章到现在，从来没有一个入口
能让它发生。

---

## §3 谁来决定"能不能一起跑"

`gather()` 版本缺的不是并发，是一个能回答"这两个调用能不能放一起"的东西。

要回答这个问题，需要知道每个调用"碰了什么"——`read_file(a.py)` 碰的是
`a.py`；两个 `read_file` 碰的是两个不同的东西，能一起跑；一个
`apply_patch(a.py)` 和一个 `read_file(a.py)` 碰的是同一个东西，其中一个
还要写，不能一起跑。

这是本章要抽的第一个新模块：`scheduler.py`。先看它是否符合第二条主线的
三条抽象例外：

- **不变量需要被强制**：「两个冲突的调用不能同时在飞」是一条规则，如果不
  收进一个函数，就会有 N 个调用点各自判断"这两个是不是冲突"，写法各不相同，
  迟早有一处判断错。
- **跨越信任边界**：不算——`Footprint` 是本进程自己算出来的，不是模型的
  输出，也不是外部输入。
- **变化是需求本身**：算——"哪些调用能并发"本来就是这一章的主题，不是
  顺手加的功能。

第一条已经够了。这不是"写两次就抽象"的三次法则在起作用——这是第一次写，
就已经落进了例外里：**先讲清楚"合法状态"是什么，再决定谁能同时发生**，
和第 1 章"先定协议再写逻辑"是同一个理由。

```python
@dataclass(frozen=True)
class Footprint:
    reads: frozenset[str] = frozenset()
    writes: frozenset[str] = frozenset()
    stateful: bool = False


STATEFUL = Footprint(stateful=True)


def conflicts(a: Footprint, b: Footprint) -> bool:
    if a.stateful or b.stateful:
        return True
    return bool(a.writes & b.writes) or bool(a.writes & b.reads) or bool(a.reads & b.writes)
```

`reads` / `writes` 是资源键的集合——对本章仅有的四个工具来说，一个资源键
就是一个解析过的绝对路径，所以两个调用碰同一个文件就是同一个键，碰不同文件
就是不同的键。`conflicts()` 从不关心键里装的是什么，只比较相不相等，
这正是为什么"什么算同一个资源"这个问题可以完全交给 `tools.py`，
`scheduler.py` 自己不需要知道任何工具的存在。

`stateful=True` 是唯一一个不能用 `reads`/`writes` 表达的情况——它的意思是
"这个调用碰了什么，我说不清楚，所以当它跟任何东西都冲突"。空的
`Footprint()`（不读也不写任何东西）是不允许被猜出来的默认值：那等于宣称
"这个调用什么都不碰"，而这是唯一一句在猜错的时候后果最严重的话。§5 会看到
`footprint_of()` 每一处猜不出来的分支，返回的都是 `STATEFUL`，不是
`Footprint()`。

---

## §4 排批次

有了 `conflicts()`，还差一个把"一轮里的 N 个调用"变成"若干组，组内并发、
组间顺序执行"的算法。

```python
def batches(calls: Sequence[ToolCall], footprint_of: FootprintFn) -> list[list[ToolCall]]:
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

规则很朴素：按模型给出的顺序处理每个调用，它的批次号是——它跟前面哪个调用
冲突，就取那个调用的批次号 +1 里最大的一个；如果跟谁都不冲突，批次号是 0。
两个互相冲突的调用，构造上就不可能落进同一批：如果它们落进了同一批，
后一个的最小批次号早就该被前一个的批次号 +1 顶上去了。

$O(n^2)$，不是什么厉害的算法。一轮里模型能发起的调用数量本来就有限
（几个到十几个），比起写一个真正的调度器，这个能一眼看懂的二重循环是对的
选择。

### §4.1 一个被测试打脸的假设

写完 `batches()`，我给它配了这样一个测试：`a` 和 `b` 都要写同一个文件，
`c` 读一个不相关的文件，顺序是 `[a, b, c]`。我当时的直觉是——`c` 虽然跟
谁都不冲突，但它排在 `a` 和 `b` 中间，"应该"落在它们中间那一批，
于是断言：

```python
assert plan == [[a], [b], [c]]
```

跑起来：

```
AssertionError: assert [[a, c], [b]] == [[a], [b], [c]]
```

`c` 跟 `a` 分到了同一批。想清楚原因之后发现这是对的：`batches()` 只做
一件事——**某个调用只会被它冲突的东西推迟**，从不会被不冲突的东西提前
或推迟。`c` 跟 `a` 不冲突，跟 `b` 也不冲突，它没有任何理由等到第二批才跑。
"模型给出的顺序会被保留"这句话是错的；真正成立的是一句弱一点的话：
**两个互相冲突的调用，后一个一定不会跑在前一个之前**——而这正是
F08-01 需要的全部保证。

```python
def test_F08_05_a_conflicting_pair_serialises_even_with_an_unrelated_call_between_them() -> None:
    plan = batches([a, b, c], lambda call: fps[call.call_id])
    assert plan == [[a, c], [b]]
```

这条测试留在了套件里，名字换成了它实际证明的东西，而不是我原来以为它会
证明的东西。§7.5 的纪律说"代码必须在页面上长出来"；这条测试是它的一个
镜像版本——**理解也要在页面上长出来**，包括我猜错的那一版。

---

## §5 谁负责说"这个调用碰了什么"

`scheduler.py` 不认识任何工具。真正知道 `read_file` 碰了哪个文件、
`apply_patch` 写了哪些文件的，是 `tools.py`——它本来就是唯一一处知道
"一个工具调用长什么样"的地方。

```python
def footprint_of(call: ToolCall, root: Path) -> Footprint:
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

四个工具，两种命运：

- `read_file` 读一个路径，`apply_patch` 写它 `edits` 里列出的每一个路径。
  路径解析复用第 4 章的 `paths.resolve()`——同一个函数，同一套"必须在
  仓库内、必须真实存在"的规则，`footprint_of()` 不重新发明一遍。
- `run_shell` 的工作目录和影响范围本来就没法用一组资源键说清楚；
  `request_permissions` 改的是整个会话接下来能干什么，其他每个工具的
  gate 都要读这个状态。两个都返回 `STATEFUL`。任何这个函数不认识的
  工具名，同样返回 `STATEFUL`——"不认识就当作会跟一切冲突"，
  跟第 2 章的环境变量白名单（F02-09）、第 5 章的"语法看不懂就问"
  （F05-01）是同一条纪律：**猜不出来的时候，往安全的方向猜，
  而不是往方便的方向猜。**

有一个细节值得说一句：这个函数把路径又解析了一遍，而真正执行调用的
`read_file`/`apply_patch` handler 马上还要再解析一次。把已经算好的
`Path` 从这里传到那里能省下第二次 `stat()`，我试过，换来的是每个工具的
签名都要多背一个"调度用的细节"，为了省一次几乎测不出来的开销。
两边保持独立是故意的：调度器允许对一个还没真的执行的调用悲观（并且
重新检查一遍），不需要让真正执行它的工具继承这份悲观。

---

## §6 接进循环

`agent.py` 里从第 0 章开始就没变过的那段 `for` 循环，这一章第一次真的动了。

```python
plan = batches(turn.tool_calls, self._footprint_of)
outputs: dict[str, str] = {}
interrupted_at: int | None = None

for batch_index, batch in enumerate(plan):
    tasks = [asyncio.ensure_future(self._run_tool(call)) for call in batch]
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
    else:
        for call, output in zip(batch, results, strict=True):
            outputs[call.call_id] = output

if interrupted_at is not None:
    for later_batch in plan[interrupted_at + 1 :]:
        for call in later_batch:
            outputs[call.call_id] = "Error: interrupted by the user before this started."
    for call in turn.tool_calls:
        history.add_tool_result(call.call_id, outputs[call.call_id])
    self.rollout.mark("interrupted", turn=turn_index)
    history.add_system_note(interrupted_note())
    return RunResult(final_text, "interrupted", turn_index + 1, history, tuple(compactions))

for call in turn.tool_calls:
    history.add_tool_result(call.call_id, outputs[call.call_id])
```

改动的是"什么时候跑"，没改"每个调用都会跑、都会被回答"——这条规则从
第 0 章活到现在，这一章也不例外。三个值得挑出来说的地方：

**结果按提交顺序落盘，不按完成顺序。** 无论批次内部谁先完成，写进
`outputs` 字典之后，最后落进 `history` 的顺序永远是 `turn.tool_calls`
本来的顺序。call_id 匹配（第 1 章，F01-03）已经让"谁先完成"这件事
在协议层面是安全的；但持久化到磁盘的会话文件不需要因此多一个
不确定性来源。§9.2 有一个真的让后提交的调用先完成的测试，证明这一点。

**取消要打断"这一批和后面所有批次"，不只是当前这一批。** 第 7 章的
F07-04 建立的规则是"每个已发起的调用都要被回答，包括根本没来得及跑的"；
这一章把它从"逐个调用"推广到"逐个批次"。`asyncio.gather()` 有一个不算
广为人知的行为：等待它的协程被取消时，它会自动取消自己还在等的每一个
子任务——不需要手写传播逻辑。真正需要手写的是取消发生之后怎么安全地
读结果：直接读 `task.result()` 可能跟"取消还没传播到位"赛跑，所以先用
`return_exceptions=True` 再等一轮，让每个任务真正落定，再去看它是被
取消了、还是自己出了别的异常、还是已经正常返回了。

**信号量放在 `_run_tool` 里面，卡在"不认识的工具名"和"参数解析失败"
这两个提前返回之后：**

```python
async with self._concurrency:
    try:
        return await self.tools[call.name](call.arguments)
    except Exception as exc:
        return f"Error: {call.name} raised {type(exc).__name__}: {exc}"
```

这两个提前返回不执行任何工具代码，不该占一个并发名额。§8 量了不加这道
限制会怎样。

---

## §7 并发要有上限

一批里放几个调用，取决于模型这一轮发起了多少个互不冲突的调用——模型说
"把这十个文件都读一遍"，`batches()` 会把十个 `read_file` 全部塞进同一批。
不加限制，就是十次磁盘 IO（或者，换成 MCP 工具之后，十个 HTTP 连接）
同时砸出去。

```python
DEFAULT_MAX_CONCURRENT_TOOLS = 8
```

`Agent.__init__` 用它建一个 `asyncio.Semaphore`，`_run_tool` 用
`async with self._concurrency:` 包住真正的调用。测过两种配置：

```
六个互不冲突的调用，max_concurrent_tools=6（相当于不设限）    实测同时在飞：6
六个互不冲突的调用，max_concurrent_tools=2                    实测同时在飞：2
```

信号量卡的是"同时真正在跑"的数量，不是"一批里有多少个"——`batches()`
决定的是顺序约束（谁必须等谁），信号量决定的是资源约束（同时最多几个）。
两者互不干扰：一批里可以有十个调用，其中同时执行的永远不超过
`max_concurrent_tools` 个，多出来的排队等一个名额空出来。

---

## §8 F08-02 / F08-03：读写互斥、shell 全程序列化

`conflicts()` 已经在 §3 用纯逻辑证明了"写 f.txt 和读 f.txt 冲突"、
"两个 `run_shell` 冲突"；这两条测试量的是接进 `agent.py` 之后，
这两条规则在真实的调用里是不是真的生效。

量的办法是一个"临界区峰值"计数器：进真正会碰文件/会跑命令的那一小段代码
之前 `+1`，出来之后 `-1`，全程记录峰值。

```python
class PeakTracker:
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

用 `threading.Lock`，不是 `asyncio.Lock`——`apply_edits`/`_read` 是通过
`asyncio.to_thread` 跑在真实操作系统线程上的，asyncio 的原语在那里不安全。

一轮里一个 `apply_patch(f.txt)` 加一个 `read_file(f.txt)`，把
`write_source()` 和 `_read()` 都包上这个计数器：

```
peak == 1
```

一轮里两个 `run_shell`，把 `ShellSession.run()` 包上同一个计数器：

```
peak == 1
```

两个数字都是 1，不是"大多数时候是 1"。因为 `batches()` 把它们分到了
不同的批次，而 `agent.py` 在第一批的 `gather()` 返回之前根本不会创建
第二批的任务——**没有重叠的窗口，不是重叠的窗口很窄。**

`run_shell` 整体当 `STATEFUL`，比"解析命令、只在真的用到 `cd` 的时候才
排斥别的调用"要粗暴得多。这不是偷懒——§12 会看到 codex 自己对这个问题
给出的答案，比这一章更粗，不是更细。

---

## §9 没复现的两条

### F08-04：一个调用抛异常，会不会连累同一批里的其他调用？

清单原本的猜测是会——一个任务抛异常，`gather()` 默认会取消其他还没完成
的任务。写测试之前设想的画面是：三个调用挤在一批，中间那个抛了
`ValueError`，另外两个的结果凭空消失。

实际测下来，三个都完好：

```python
outputs = {i.call_id: i.content for i in result.history.items if isinstance(i, ToolResult)}
assert outputs["call_1"] == "a"
assert outputs["call_3"] == "b"
assert "ValueError" in outputs["call_2"]
```

原因不是运气，是结构性的：第 0 章的 F00-05 已经把 `_run_tool()` 写成了
"永远不抛 `Exception`，只返回字符串"。`asyncio.gather()` 的
"一个抛异常就取消其他任务"只在真的有任务抛出未捕获异常时触发——而一次
普通的工具失败，从第 0 章开始就从来没有机会走到这一步。这条故障不是
"这一章顺便修好了"，是"这一章根本没有制造让它发生的条件"。测试锁住的是
这句话，不是猜测。

### F08-06：模型假设的隐式顺序

清单设想的场景：模型以为"先建文件、再读文件"这两步会按顺序发生，
但调度器把它们当成互不冲突，实际并发执行，读到的是文件还不存在时的状态。

这个场景在本章的工具集里搭不出来。`read_file` 和 `apply_patch` 的
`Footprint` 都是从 `paths.resolve()` 解析出来的真实路径——不存在"两个
调用碰的是不同文件，但其中一个的效果其实依赖另一个"这种情况，因为
本章的 `apply_patch` 只能编辑已经存在的文件，没有能创建新文件、让
"隐式依赖"有地方藏身的工具。

```python
async def test_F08_06_unrelated_files_really_are_independent(tmp_path: Path) -> None:
    ...
    outputs = {i.call_id: i.content for i in result.history.items if isinstance(i, ToolResult)}
    assert outputs["call_2"] == "original-b"
    assert (tmp_path / "a.txt").read_text() == "changed-a"
```

这条故障要活过来，需要一个"声明的 `Footprint` 可能是错的"的工具——
换句话说，需要一个不是从真实路径解析出来、而是猜出来的资源键。
第 9 章要接的 MCP 工具正好是这个形状：远程工具的副作用，本章的代码
完全没有办法验证。这条记录留在清单里"未复现"，不是因为它被证明不存在，
是因为现在还没有一个工具能让它存在。

---

## §10 文件清点

| 文件 | 行数 | 新增/修改 | 完整代码在 |
|---|---|---|---|
| `src/minicodex/scheduler.py` | 100 | 新增 | §3、§4 |
| `src/minicodex/tools.py` | +52 | 新增 `footprint_of()` | §5 |
| `src/minicodex/agent.py` | +38 | 换掉工具执行循环，加信号量 | §6、§7 |
| `src/minicodex/__main__.py` | +7 | 在唯一一个知道 `root` 的地方接上调度器 | §11 |
| `tests/test_scheduler.py` | 28 个测试 | 新增 | 全章分散 |
| `probe_scheduler.py` | 68 | 新增 | §2 |

自检方式：把本章所有 Python 代码块拼起来，逐行 grep 源码。

```
$ uv run pytest
1313 passed, 9 skipped in 14.21s

$ uv run pytest tests/test_scheduler.py
28 passed in 0.55s
```

跳过的 9 个和第 7 章一样——`killpg` 只在 POSIX 上有意义（F02-10），
在这台 Windows 机器上按名说明理由地跳过。

---

## §11 CLI 的那一行

调度器默认关闭：`Agent.__init__` 里 `footprint_of` 不传就等于
"每个调用都是 `STATEFUL`，谁也不跟谁一起跑"——第 0 章到第 7 章写的
每一个测试，构造 `Agent` 的时候都没传这个参数，这一章过后它们描述的
行为一个字都没变。

真正打开并发的，是整个项目里唯一一处同时知道"要用真实文件系统"和
"仓库根在哪"的地方——`__main__.py` 里构造 `Agent` 的那一行：

```python
root = Path.cwd().resolve()
agent = Agent(
    llm,
    default_tools(root=root, session=session),
    ...
    footprint_of=functools.partial(footprint_of, root=root),
)
```

`default_tools(root=root, ...)` 早就存在；这一章新加的只是把同一个
`root` 再绑一次给 `footprint_of`。没有第二个入口悄悄打开并发——`serve-stub`
命令、任何测试脚手架，统统拿到的是默认的全串行 `Agent`。

---

## §12 收工：commit 与 review

### commit 序列

```
feat(scheduler): decide which tool calls in a turn may run together

Footprint{reads, writes, stateful} plus conflicts() plus batches() -- list
scheduling, not locks. A call the scheduler cannot classify returns STATEFUL
rather than an empty Footprint(), because "touches nothing" is the one guess
that is never safe to make. Pure and synchronous: no event loop needed to
test it.
```

```
feat(tools): declare what read_file and apply_patch touch

footprint_of() resolves every path the same way paths.resolve() already does
(chapter 4) and returns it as a resource key. run_shell and
request_permissions -- and anything this function does not recognise --
return STATEFUL, on purpose: the same "unknown means conservative" rule as
F02-09's environment allowlist and F05-01's "unparseable syntax asks".
```

```
feat(agent): batch tool execution instead of awaiting one call at a time

Measured (probe_scheduler.py): two apply_patch calls to one file, raced with
a bare asyncio.gather(), lose an edit 30/30 -- and 30/30 even with the
artificial delay set to zero. batches() puts conflicting calls in different
batches; agent.py never starts batch N+1 until batch N's gather() has
returned, so the race cannot happen through the scheduled path.

Results are collected by call_id and appended to history in the model's
original order, not completion order, so the transcript does not gain a
second source of nondeterminism.
```

```
fix(agent): answer every call in a cancelled batch, and every batch after it

Generalises F07-04 from "one call at a time" to "one batch at a time, plus
every batch scheduled after it". gather() already cancels every task it is
still awaiting when the await on gather() itself is cancelled; reading
.cancelled()/.result() safely afterwards needs a second
gather(..., return_exceptions=True) rather than reading the tasks directly.

Chapter 7's own two-call cancellation test passes against this unmodified --
it was already exercising "the call that never started is marked
interrupted", the old code just did it per-call instead of per-batch.
```

```
feat(agent): bound real concurrency with a semaphore, and wire it up in main

DEFAULT_MAX_CONCURRENT_TOOLS=8, acquired inside _run_tool around the actual
handler call -- after the "unknown tool" / "bad arguments" early returns,
which run nothing and should not spend a slot. Measured: 6 non-conflicting
calls run 6-at-once uncapped, 2-at-once with max_concurrent_tools=2.

__main__.py is the only call site that passes footprint_of; every Agent
built in this project's test suite still gets the fully-serial default.
```

### PR 描述

```markdown
## What
Tool calls within one turn that do not conflict now run concurrently.
Calls that do conflict -- same file, or either one unclassifiable -- still
never overlap, including two edits to one file and two shell commands in
one turn.

## Why
Every call has waited for the previous one since chapter 0, even when they
had nothing to do with each other. The obvious fix (wrap the loop in
asyncio.gather()) is also the fastest way to reintroduce a bug chapter 4
never had a chance to hit: two concurrent apply_patch calls to the same
file both report success while one of them silently loses its edit --
30/30 in measurement, with no artificial delay needed to reproduce it.

## How
- `scheduler.py`: Footprint + conflicts() + batches() -- list scheduling
  over declared resources, not per-resource locks.
- `tools.py`: footprint_of() -- read_file/apply_patch declare resolved
  paths; run_shell/request_permissions/unknown tools default to STATEFUL.
- `agent.py`: batches this turn's calls, gathers each batch, answers every
  call including ones in batches that never started (cancellation), bounds
  real concurrency with a semaphore.
- `__main__.py`: the one call site that opts in, bound to the run's root.

## Testing
28 new tests, none networked. Two are real filesystem races
(asyncio.to_thread, real OS threads) made deterministic with a small
inserted delay -- probe_scheduler.py shows the delay is not actually
load-bearing on this machine (30/30 either way), kept as insurance for a
slower CI runner.

    1313 passed, 9 skipped in 14.21s

## Notes for the reviewer
- F08-04 (a raising call losing its batch siblings) and F08-06 (implicit
  ordering the scheduler can't see) do not reproduce with this tool set --
  see FAULTS.md for why, not just that.
- run_shell is STATEFUL unconditionally, not "parse cd and reason about
  it". §12 of the chapter compares this to codex's own (coarser) answer.
```

### Code review

我扮演 reviewer，五条真实意见。

**#1（正确性）`batches()` 允许一个不冲突的调用插到两个冲突调用中间，
落进较早那一批——`test_F08_05` 的名字改了才说清楚这一点。这是不是意味着
"模型给出的顺序会被保留"这句话在文档里是错的？**

> 是错的，已经改了措辞。README 和这一章都不再说"顺序被保留"，改成更弱、
> 也更准确的一句：只有互相冲突的调用之间才保证顺序，不冲突的调用可以
> 自由地插到更早的批次里。这条测试留下来的价值就是把这句话钉死，
> 免得以后有人凭直觉重新引入"顺序被保留"这个错误假设。

**#2（可测试性）F08-01/02/03 用真实 `time.sleep` 和真实操作系统线程做竞态，
CI 机器慢或者忙的时候会不会偶尔翻车？**

> 已经用 `probe_scheduler.py` 测过：这三个竞态在这台机器上，延迟设成 0
> 依然 100% 复现，插的那点延迟不是让"偶尔发生"变成"总是发生"，
> 它本来就总是发生。留着 0.05 秒的延迟纯粹是给一台更慢、更忙的 CI 机器
> 上保险；如果哪天真的翻车了，那本身就是一条新故障，值得单独记一条 ID，
> 而不是加大延迟然后假装没看见。

**#3（设计）`run_shell` 整体当 `STATEFUL`，比"解析一下命令，只有真的带
`cd`/`export` 才当作有状态"粗糙很多。为什么不做细一点？**

> 故意的，理由写进了 §12：codex 自己面对这个问题给出的答案甚至更粗——
> 一个工具要么整体允许并发、要么整体不允许，是个进程级的读写锁，
> 不做"这条命令具体碰了什么"的判断，真正的顺序保证下沉到
> `unified_exec` 自己的会话管理里。本章选的"整体当 STATEFUL"是这个思路
> 里最简单的一半（只有"不允许并发"这一档），没做"允许并发、但内部自己
> 排好序"那一半——那需要给 `run_shell` 一整套持久会话的调度机制，
> 是明显更大的工程，记在 README 的"deliberately not done"里。

**#4（措辞）`_run_tool` 里两处"interrupted by the user"的字符串，
一处说"before this finished"，一处说"before this started"，
是不是应该抽成一个常量，避免以后改串的时候漏改一处？**

> 接受观察，暂不改。两句话在语义上不是同一句话的两个参数——"没跑完"
> 和"没跑"是模型需要分辨的两种状态，抽成一个模板反而会让人以为它们
> 是同一件事的措辞变体。真正该抽的是"如果以后需要给这两句话加第三个
> 变体"这个信号出现的时候,不是现在——两处，两次法则的第一次,还没到
> 抽象的门槛。

**#5（一致性）`footprint_of()` 把路径解析了一遍，`read_file`/`apply_patch`
的 handler 马上又解析了一遍。两次 `resolve()`，是不是该把结果传过去？**

> 试过，撤销了，理由写进了 `footprint_of()` 的 docstring：省下来的是一次
> `stat()`，换来的是每个工具的签名都要背一个"调度用的细节"到处传。
> 调度器允许对一个还没真的执行的调用悲观、之后重新检查一遍；
> 真正执行调用的代码不需要继承这份悲观。这不是"懒得优化"，
> 是两边职责本来就不该共享这份状态。

### CI

这一章不加新的 CI 步骤。

理由：本章 28 个测试全部是普通的 `pytest` 测试（含两个真实竞态，
`pytest` 本来就在跑）。`probe_scheduler.py` 是测量工具，回答的是
"这条故障要不要人为延迟才能复现"这一次性的问题,不是回归测试——
进 CI 只会让 CI 变慢,不会防住任何一次真实的回退。

第 6 章加过一步（属性测试跑 2000 个 case），因为那条 bug 在默认的 200
个 case 下抓不到,是真实测出来的需要。这一章没有这种情况：本章的竞态
测试在延迟等于 0 的时候依然 100% 复现,不需要更多样本才能抓住。

---

## §13 codex 是怎么做的

对照真实的 codex 源码（`codex-rs/`）：

- **确实是并发的，逐个调用**：`core/src/session/turn.rs` 里,每个
  `FunctionCall` 一到,就立刻被塞进一个 `FuturesOrdered`（用于保证结果
  按调用顺序被取出,呼应本章 §6 的"结果按提交顺序落盘"）；每个调用真正的
  工作在 `core/src/tools/parallel.rs` 里用 `tokio::spawn` 起一个独立的
  任务,是真的在不同任务上跑,不是本章这种"同一批 `gather()` 里排好队"。
- **但没有本章这种"按资源精细分类"的调度**：codex 给每个工具挂了一个
  粗粒度的布尔开关——`ToolExecutor::supports_parallel_tool_calls()`,
  默认 `false`,由 `shell_command`/`exec_command`/`write_stdin` 这几个
  工具显式覆盖成 `true`。允许并发的工具在一个 `RwLock<()>` 上取**读锁**,
  不允许并发的工具取**写锁**——是一整轮层面的读写互斥,不是"这两个调用
  碰的是不是同一个文件"。本章 `Footprint` 这种按资源路径判断冲突的做法,
  在 codex 源码里**不存在**，这是本章比 codex 更细的地方,不是抄它抄来的。
- **`run_shell` 反而更接近 codex 里"允许并发"的那一档**：`unified_exec`
  模块(`core/src/unified_exec/mod.rs`)管理持久化的 PTY 会话,
  `exec_command`/`write_stdin` 两个工具都声明 `supports_parallel_tool_calls
  = true`——顺序保证被下沉到 `unified_exec` 自己的会话管理里,
  而不是靠一把全局锁。本章的 `run_shell` 没有这一层,只能选更粗的那一半:
  整体当 `STATEFUL`,直接放弃并发。§12 review #3 已经把这条差异记清楚。
- **真的有一个测试套件专门测这个**：`core/tests/suite/tool_parallelism.rs`,
  里面的 `read_file_tools_run_in_parallel`/`shell_tools_run_in_parallel`
  用一个墙钟时间断言证明真的重叠了——两个各睡 300ms 的工具,总耗时断言
  小于 1600ms,而不是简单地"跑通了就算数"。跟本章的 `PeakTracker` 是
  同一个念头的两种写法：**光靠调用返回正确的结果,证明不了两个调用真的
  同时在飞过；要么量墙钟时间,要么量临界区里同时有几个。**
- **`tool_results_grouped` 测试**确认了 call_id 匹配 + "所有 call 排在所有
  output 前面"——跟第 1 章的历史不变量是同一条规则,codex 也是这么做的。

---

## 如果你只记住三件事

1. **`for` 循环换成 `gather()` 只需要一行,但它默认打开的不是"更快",
   是"任意两个调用可以同时碰同一个文件"。** 两个 `apply_patch` 改同一个
   文件、都报告成功、其中一个悄悄丢了——30 次测量,30 次全丢,
   延迟设成 0 也是 30 次全丢。这条故障不需要慢机器、不需要倒霉,
   只需要一个从来没被测过的入口。

2. **"能不能一起跑"不是一个运行时决定,是一个能提前算出来的静态计划。**
   `Footprint` 加 `conflicts()` 加 `batches()` 全是纯函数,不需要事件循环
   就能测。真正需要小心的地方从来不是算法本身,是那个不起眼的默认值:
   猜不出一个调用碰了什么的时候,该往"当它跟一切冲突"猜,不是往
   "当它什么都不碰"猜——空的 `Footprint()` 是唯一一句在猜错的时候
   后果最大的话。

3. **并发不重新定义"每个调用都要被回答"这条规则,只是让它多一个维度。**
   第 7 章确定了"取消要打断这一个调用之后的一切";这一章把"一切"从
   "剩下的调用"改成"这一批剩下的任务,加上后面所有还没开始的批次"。
   规则本身一个字没变——`gather()` 自己会把取消传给它还在等的每个任务,
   真正要写的代码只是"安全地读出结果,不要在取消还没传播完的时候就去看
   它"。第 7 章那条两个调用的取消测试,一行没改,照样通过。

---

## 动手练习

1. 把 `agent.py` 里的 `for batch_index, batch in enumerate(plan):` 那段
   换回第 0 章到第 7 章的逐个调用版本,跑
   `uv run pytest tests/test_scheduler.py`。看清楚哪些测试红了——
   不是全部,`footprint_of()`/`conflicts()`/`batches()` 那些纯逻辑测试
   照样是绿的,因为它们从来没有真的经过 `agent.py`。这说明了什么关于
   "测试调度算法本身"和"测试调度算法真的接进了循环"是两件不同的事。

2. 把 `footprint_of()` 里 `run_shell` 那一行从 `return STATEFUL` 改成
   "解析一下命令,如果不含 `cd`/`export` 就当作没有状态"（可以复用
   `shell_parse.py` 的分词器）。跑 `test_F08_03_two_shell_calls_in_one_turn_never_overlap`,
   看它是不是还是绿的——如果是,再想一个会让它变红的场景:
   两条命令都不含 `cd`,但第二条依赖第一条创建的文件。这正是 F08-06
   在 `run_shell` 世界里的样子,而 `read_file`/`apply_patch` 因为路径是
   真实解析出来的,天生躲开了这个问题。

3. 把 `probe_scheduler.py` 里的三个延迟都改成负的一个荒谬大数
   (比如 2 秒),再跑一遍,记录"丢失率"。再把延迟设成
   一个刚好卡在"read_source 两次调用几乎不可能重叠"的极小值,
   看丢失率会不会开始掉。**在你的机器、你的操作系统、当前的系统负载下**
   量出这条曲线的拐点在哪——这正是本章想让你亲手确认的事:
   竞态复现率不是一个抽象常数,是硬件和调度器共同决定的一个经验分布。

4. 给 `Footprint` 加一个第三种资源类型:不是 `reads`/`writes`,
   是 `appends`(比如一个假想的"追加日志"工具)。想清楚
   `conflicts()` 该怎么改——两个 `appends` 该不该冲突?
   一个 `appends` 和一个 `writes` 同一份资源呢?写完之后,
   把 `test_F08_01_write_write_same_key_conflicts` 之类的测试
   对着新类型各写一遍,确认你的直觉能被测试钉住,而不是只存在于脑子里。

5. 读一遍 `test_F08_08_a_concurrent_batch_mate_that_already_finished_keeps_its_real_result`,
   然后故意把 `agent.py` 里 `settled = await asyncio.gather(*tasks, return_exceptions=True)`
   这一行删掉,直接用 `task.result()`。跑测试,看哪里炸——
   炸的位置能不能对上 §6 里"读取消结果会跟取消传播赛跑"这句话?
   如果一次没炸,多跑几次;这正是一条"删掉保护之后大概率会红,
   但不保证每次都红"的测试,§8 讨论过的"没有人为延迟也 100% 复现"
   在这里不成立,是故意留给你对比的反例。

下一章：Ch09 · 工具太多——一个 MCP server 的工具描述是从远端来的,
`footprint_of()` 拿到的将不再是一个可以用 `paths.resolve()` 验证的路径,
而是一句它自己都不一定说得准的承诺。F08-06 那条"未复现"的故障,
到那时候才第一次有机会真的发生。
---

# 附录 · 代码到底怎么敲出来的（新手向，逐段精讲）

跟第 7 章附录同一个规矩：正文讲"为什么"，这里补"怎么写"。默认你已经读过
前面几章的附录（`asyncio`、`dataclasses`、`Path` 基础），这里只讲这一章
正文只有片段的部分。代码摘自 `steps/step08_concurrency/src/minicodex/`，
逐段核对过。

先把范围说死：

1. 本附录只解释第 8 章在 `steps/step08_concurrency/` 里新增或修改的代码。
   正文 §3/§4 给了 `scheduler.py` 全文（`Footprint`/`STATEFUL`/`conflicts`/
   `batches`），§5 给了 `tools.py` 的 `footprint_of` 全文，§6 给了
   `agent.py` 的工具执行循环——这些**不再重复**。这里补正文只有片段的：
   `_run_tool` 完整（§7 只给了信号量那一段）、`__main__.py` 的接线。
2. 完整函数代码块里不使用代表遗漏实现的省略号，也不把关键分支写成
   "此处略"。示例字符串或命令文本里出现的三个点，如果本来就是源码的
   一部分，会原样保留。
3. 代码块按当前源码核对。

## Q1 · `_run_tool` 完整实现

正文 §7 只给了信号量那段。完整函数：

```python
    async def _run_tool(self, call: ToolCall) -> str:
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

四个要点：

1. **信号量在"不认识的工具名"和"参数解析失败"两个提前返回之后**——正文
   §7 强调过：这两个分支不碰任何共享资源，不需要占并发名额。只有真正
   调用工具的代码在 `async with self._concurrency:` 里。
2. **`asyncio.Semaphore(max_concurrent_tools)` 在 `Agent.__init__` 建**，
   默认 `DEFAULT_MAX_CONCURRENT_TOOLS = 8`（正文 §7 测过：`read_file` 50
   个不冲突调用放一批，8 个信号量限制同时打开的 socket/线程数，F08-07
   就是这么防的）。信号量是**每 Agent 一个**，不是全局——父子 Agent 各
   有各的。
3. **`except Exception` 故意宽**（docstring 明说）——工具抛的任何异常都
   变成给模型的字符串，不能让一个工具的崩溃结束整个会话（F00-05）。注意
   是 `Exception` 不是 `BaseException`——`CancelledError`/`KeyboardInterrupt`
   不该被吞（那是中断语义，正文 §6 的循环用 `except (asyncio.CancelledError,
   KeyboardInterrupt)` 处理）。
4. **两个提前返回都是给模型的错误消息**：不认识的工具名列出可用列表
   （F00-06："error names the available tools"），参数解析失败回显
   `raw_arguments[:200]`（截断，防止把巨量垃圾塞回消息）。

## Q2 · `__main__.py` 的接线

正文 §11 说"在唯一一个知道 root 的地方接上调度器"。完整接线：

```python
    root = Path.cwd().resolve()
    agent = Agent(
        llm,
        default_tools(root=root, session=session),
        recorder=recorder,
        instructions=_instructions(session),
        context_window=context_window,
        rollout=writer,
        resume_from=resume_from,
        summariser=make_summariser(llm) if context_window else None,
        # Opting in to chapter 8's scheduler: bound to this run's root, since
        # that is what turns a path in an argument dict into a resource key.
        # Every test suite from chapters 0-7 builds an Agent without this and
        # gets the old fully-serial behaviour -- this is the one call site
        # that asks for anything else.
        footprint_of=functools.partial(footprint_of, root=root),
    )
```

三个要点：

1. **`footprint_of=functools.partial(footprint_of, root=root)`**——把
   `root` 焊死（`functools.partial` 第 1 章起就用的形状），剩下的参数
   `call` 由 `batches` 调用时提供。`root` 是"把参数 dict 里的路径变成
   资源键"所必需的。
2. **默认关闭。** `Agent.__init__` 里 `footprint_of` 不传就等于
   `lambda call: STATEFUL`（第 12 章附录 D 讲过）——第 0～7 章所有测试
   建的 Agent 都没有它，得到旧的完全串行行为。**这里是唯一一个主动
   要求并发的调用点**（注释明说）。
3. **`root = Path.cwd().resolve()`**——和 `default_tools(root=root)` 用
   同一个 root，保证"执行工具"和"调度器"对"仓库根在哪"的理解一致。

## Q3 · 新手常见报错/坑对照表

| 症状 | 原因 | 修法 |
|---|---|---|
| 并发后工具输出顺序乱了 | 结果按完成顺序收集 | 按 `call_id` 收集，再按模型原始顺序 append（正文 §6） |
| 两个写同一文件的调用同时跑 | `conflicts` 没判 writes∩writes | `bool(a.writes & b.writes)` 等三个交集 |
| `run_shell` 和其他工具并发 | footprint 没标 stateful | `run_shell`/`request_permissions` 返回 `STATEFUL` |
| 读读冲突导致没并发收益 | `conflicts` 对 read/read 返回 True | read/read 是唯一允许说"不冲突"的组合 |
| 中断时 mid-flight 工具竞态 | 直接读 `.result()` | `return_exceptions=True` 再 `gather` 等它们落地 |
| 信号量卡住整个循环 | 参数校验也占名额 | 提前返回的分支在 `async with` 之外 |
| 工具异常结束会话 | 异常往上冒 | `_run_tool` 里 `except Exception` 变字符串 |
| 测试套件变慢 | 全部测试意外并发 | `footprint_of` 默认不传 = 全 STATEFUL，只有 CLI 主动打开 |
