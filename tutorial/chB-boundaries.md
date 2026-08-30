# 插曲 B · 第二次重构：把边界钉死

第 10 章末尾我留了一张便条：

> **`spawn_agent` 活在 `tool_specs()` 外面。** 把 handler 放在 handler 该在的
> 地方是一个循环 import；第 10 章走了便宜的路，插曲 B 处理没有便宜路可走的情况。

这一章回来还这笔债。

但真正值钱的东西不是那个环——环会自己报错，是本书 164 条故障里最容易的那 23 条
之一。值钱的是**绕开它的那个动作留下了什么**：一个第二装配点，它给 `Agent` 传了
十个参数里的三个，而另一个装配点传七个。没有任何东西会告诉你这件事。

这一章要做的：

- 把两个环都真的造出来，贴真实报错；
- 量出第 10 章那个绕法的价格（三个静默故障，一次测量）；
- 用依赖倒置把箭头去掉；
- **把边界写成一个 CI 检查**，并且给每条规则一次真实的失败证明；
- 顺便回答一个更难的问题：**为了打断环而引入的抽象，什么时候是必要的，什么时候
  只是仪式**。

---

## §0 开工前

分支 `refactor/boundaries`，预计 5 个 commit。

规矩和插曲 A 一样，而且这一次会被真的考验到：**纯重构的 commit 和改行为的 commit
不许混在一起**。这一章两样都有——`ToolSet` / `Wiring` 的抽取是纯重构，"子 Agent
现在会压缩、会录制、会并发调度"是行为变更。它们分在不同的 commit 里，理由在 §15。

一件事先说清楚：**我不知道动手之前这一章会得到什么结论。** 插曲 A 计划里的重构
（拆 600 行的 `agent.py`）在量完之后被取消了——212 行、依赖图无环。所以这一次也
先量。

---

## §1 先量图

写一个 60 行的脚本，读 `src/minicodex/*.py` 的 AST，建有向图，跑一遍 Tarjan：

```bash
uv run python probe_boundaries.py graph
```

```
26 modules, 72 edges
cycles: none

level  module              out   in
    0  __init__              0    3
    0  agent_types           0    8
    0  clip                  0    3
    0  mcp                   0    2
    0  model                 0    3
    0  recorder              0    2
    0  shell_parse           0    2
    0  stub                  0    1
    0  tokens                0    3
    0  tool_errors           0    7
    1  history               1    3
    1  paths                 1    2
    1  policy                1    4
    1  scheduler             1    3
    1  shell                 2    3
    2  compaction            4    2
    2  patch                 2    1
    2  rollout               2    3
    2  rules                 1    2
    3  agent                 8    3
    3  approval              5    5
    4  registry              8    2
    4  subagent              7    2
    4  tools                 7    2
    5  composition           7    1
    6  __main__             15    0
```

（这是**改完之后**的图，`composition` 已经在里面了——改之前是 25 个模块、
`subagent` 在第 5 层，因为它 import 了 `tools`。）

`cycles: none`。

于是插曲 B 面对和插曲 A 一样的局面：**计划里要打断的那个环，不存在。**

但这次结论不一样，因为不存在的原因不一样。插曲 A 里 `agent.py` 不臃肿，是因为它
本来就不臃肿。这里没有环，是因为**环被人为绕开了**，而且绕开的痕迹是可见的：

```
$ grep -rn "循环\|circular" src/minicodex/*.py | head
subagent.py:  ... makes `tools` import `subagent` import `tools`, and the
              package stops importing at all.
tests/test_boundaries.py:  三个测试，专门守着这条边
```

一个只靠"大家记得别这么写"维持的不变量，和一个真的不成立的不变量，在图上长得一模
一样。区别只有在有人写下那一行的时候才会出现。

所以这一章不问"有没有环"，问两个别的问题：

1. **要造一个环有多容易？**
2. **不造那个环，代价是什么？**

---

## §2 第一个环：一行谁都会写的 `__init__.py`

先回答第一个问题，而且不从第 10 章预告的那个环开始——从一个**没人预告过**的开始。

现在的 `src/minicodex/__init__.py` 是这样的（第 -1 章写的，之后基本没动）：

```python
"""minicodex -- a Codex-like coding agent, built from scratch."""

from pathlib import Path

__all__ = ["__version__", "compaction_prompt", "permissions_prompt", "system_prompt"]

__version__ = "0.0.1"

_PROMPTS = Path(__file__).parent / "prompts"


def system_prompt() -> str: ...
def permissions_prompt() -> str: ...
def compaction_prompt() -> str: ...
```

它是一个叶子：不 import 包里任何东西。

现在做一件每个 Python 包迟早都会做的事——给它一个好用的顶层 API，这样使用者可以
写 `from minicodex import Agent` 而不用知道 `Agent` 在哪个模块：

```python
from pathlib import Path

from minicodex.agent import Agent      # ← 加了这一行
```

一行。review 的时候没人会拦。

```bash
uv run python probe_boundaries.py init-cycle
```

```
--- import minicodex
    exit 1
        from minicodex.agent import Agent
      File "...\src\minicodex\agent.py", line 23, in <module>
        from minicodex.compaction import CompactionResult, Sizer, Summariser, compact
      File "...\src\minicodex\compaction.py", line 41, in <module>
        from minicodex import compaction_prompt
    ImportError: cannot import name 'compaction_prompt' from partially initialized
    module 'minicodex' (most likely due to a circular import) (...\__init__.py)

--- import minicodex.compaction
    exit 1
    （同样的报错）

--- import minicodex.tools
    exit 1
    （同样的报错）
```

**整个包不能 import 了。** 不是某个模块，是每一个。

环长这样：

```
minicodex/__init__.py  ──→  agent.py  ──→  compaction.py
        ↑                                        │
        └────────────────────────────────────────┘
              from minicodex import compaction_prompt
```

值得停一下看看这三个模块的关系：

- `__init__.py` 是**包**，不是模块。大多数人画依赖图的时候根本不画它。
- `compaction.py` 依赖它，是因为第 6 章把压缩 prompt 放进了 `prompts/` 目录，
  而读那个文件的函数在 `__init__.py` 里（第 -1 章放的，理由是"数据文件是证明打包
  没坏的最便宜方式"）。
- `agent.py` 和 `__init__.py` 从来没听说过对方。

**三个互不相干的模块，一行便利 import，整个包报废。** 而且报错指着
`compaction_prompt`——一个和 `Agent` 毫无关系的名字。你要读三层 traceback 才能
明白问题出在你刚加的那一行。

### 2.1 检查器自己的第一个 bug

这一节最该记住的不是上面那个环，是下面这件事。

我在 §1 里写的那个图脚本，跑在**同一棵坏掉的树**上，说：

```
cycles: none
```

为什么？它是这么解析 import 的：

```python
elif isinstance(node, ast.ImportFrom) and node.module:
    names.add(node.module)
...
known = set(graph)                       # 只保留图里认识的节点
mods[k] &= known
```

`from minicodex import compaction_prompt` 的 `node.module` 是 `"minicodex"`。
脚本去找 `minicodex.py` 这个模块，找不到，于是**把这条边扔了**。

`minicodex` 这个包本身不在节点表里，因为节点表是 `*.py` 文件名建的，
`__init__.py` 被映射成了 `minicodex`——名字对得上，但只有在 `&= known` 之前把包
名当成节点才行，而我写的是"找一个叫 `compaction_prompt` 的模块"。

于是：**一个报告"没有环"的环检查器，跑在一个装不起来的包上。**

这是 FB-02 的真正形状，比清单上写的"三个月后边界又被人破坏"更早、更便宜、也更常
见：不是规则被违反，是**规则本身没在测量它以为在测量的东西**。第 5、6、9、10 章
各有一次"测试名字说的比断言多"，这一次轮到检查器。

修法一行：

```python
if node.module == package:
    # `from minicodex import compaction_prompt` -- 这个名字可能是子模块，
    # 也可能是包的属性。两种情况下这条边都指向**包**，而包的 __init__
    # 必须先跑完。
    found.add(package)
else:
    found.add(node.module)
```

修完之后，同一棵树：

```
and the graph checker on that same tree says:
cycles = [['minicodex', 'minicodex.agent', 'minicodex.compaction']]
```

**§11 里那个检查器的每一条规则，都会有一次这样的证明。** 不是因为我谨慎，是因为
这一条我没证明，它就骗了我。

---

## §3 第二个环：第 10 章预告的那个

现在造第 10 章说的那个。把 `spawn_agent` 的 handler 放回 handler 该在的地方，也
就是让 `tools.py` 需要 `run_task`：

```python
# tools.py
from minicodex.subagent import run_task     # ← 加这一行
```

`subagent.py` 不用改，它本来就有：

```python
# subagent.py（第 10 章的样子）
from minicodex.tools import ToolContext, ToolSpec, bind_all, tool_context, tool_schemas
```

```bash
uv run python probe_boundaries.py tool-cycle
```

```
--- import minicodex.tools
    exit 1
      File "...\minicodex\tools.py", line 31, in <module>
        from minicodex.subagent import run_task
      File "...\minicodex\subagent.py", line 67, in <module>
        from minicodex.tools import bind_all
    ImportError: cannot import name 'bind_all' from partially initialized module
    'minicodex.tools' (most likely due to a circular import) (...\tools.py)

--- import minicodex.subagent
    exit 1
      File "...\minicodex\subagent.py", line 67, in <module>
        from minicodex.tools import bind_all
      File "...\minicodex\tools.py", line 31, in <module>
        from minicodex.subagent import run_task
    ImportError: cannot import name 'run_task' from partially initialized module
    'minicodex.subagent' (most likely due to a circular import) (...\subagent.py)

--- import minicodex
    exit 0
```

前两行是预期内的：**报错怪的是先被加载的那个模块**，所以从两个入口进去看到的是
两条完全不同的错误消息。这就是循环 import 难查的原因——错误信息取决于谁先被
import，而"谁先"取决于调用方，不取决于 bug。

### 3.1 第三行才是这一节的重点

```
--- import minicodex
    exit 0
```

**`import minicodex` 成功了。**

为什么？因为 `__init__.py` 是叶子（§2 里那一行还没加）。顶层 import 只跑
`__init__.py`，它不碰 `tools` 也不碰 `subagent`，所以什么都没发生。

那么：

```yaml
- name: Smoke test
  run: python -c "import minicodex"
```

这是**最自然的 CI 冒烟检查**，也是我一开始会写的那个。它在一个所有模块都互相
装不起来的包上返回 0。

这条观察直接决定了 §11 里检查器的形状：它必须**逐个模块、每个开一个新解释器**去
import，而不是 import 一次包就算数。（"每个开一个新解释器"也不是讲究：一旦某个
模块进了 `sys.modules`，环就对之后所有代码隐形了——这也是为什么在一个已经
`import minicodex.tools` 过的 shell 里，测试套件是绿的。）

---

## §4 四种修法，一个一个试

环有了。修它有四种标准做法，本书前面用过其中两种。

### (a) 把共享的**类型**下沉

第 1 章的答案。`history.py` 要 `ToolCall`，`agent.py` 要 `History`，于是
`ToolCall` 搬进 `agent_types.py`，环没了。插曲 A 对 `ToolFn` 又做了一次。

这次不行，理由不是"我不想"，是可以说清楚的：

**`tools.py` 要的 `run_task` 不是一个类型，是一个行为。**

```python
async def run_task(spec: TaskSpec, ctx: SubAgentContext) -> TaskResult:
    ...
    tools = child_tools(ctx)
    writer = _writer(ctx)
    child = ctx.wiring.agent(...)
    task = asyncio.ensure_future(child.run(spec.task))
    result = await asyncio.wait_for(asyncio.shield(task), ctx.timeout)
    ...
```

它构造一个 `Agent`、跑它、解释结果、处理超时和取消。把它下沉到 `agent_types.py`
就等于把整个 `subagent.py` 下沉。**没有类型可以搬，因为要搬的东西不是类型。**

这是一个可以直接用的判据：

> 循环里两个模块交换的是**名词**（类型、常量、协议），下沉；交换的是**动词**
> （函数、行为），下沉解决不了，得倒置。

### (b) 把**工具**上移

第 10 章的答案：handler 不放 `tools.py`，放到已经同时依赖两边的 `__main__.py`。

能用。这就是这一步开始时的状态。价格在 §5。

### (c) 把 import 挪进函数体

最省事的那种：

```python
def _run_task(*args, **kwargs):
    from minicodex.subagent import run_task    # ← 挪进来了

    return run_task(*args, **kwargs)
```

```bash
uv run python probe_boundaries.py repairs
```

```
(c) defer the import into a function body:
--- import minicodex.tools
    exit 0
    imported fine

    and the AST checker still sees the edge:
    cycles = [['minicodex.subagent', 'minicodex.tools']]
    The cycle did not go away. It moved from startup to the first call.
```

包能 import 了。**环还在。**

它现在的失败时刻从"启动时"变成了"第一次调用时"——也就是更晚、在用户面前、并且
出现在一个关于别的东西的 stack trace 里。

这条修法在真实项目里非常常见，而且大部分时候确实能一直跑下去。我不打算说它绝对
不能用；我要说的是**它会骗过检查器**。所以 §11 的规则一是：

```python
tree = ast.parse(path.read_text(encoding="utf-8"))
for node in ast.walk(tree):      # walk，不是 tree.body
```

`ast.walk` 走整棵树，函数体里的 import 照样算一条边。**一个只读文件开头的检查器
是在奖励那个把问题藏起来的修法。**

### (d) 依赖倒置

`subagent.py` 不再自己去 `tools.py` 拿东西，而是**由构造它的人把它需要的东西递
进来**：

```python
@dataclass(frozen=True)
class SubAgentContext:
    build_model: Callable[[list[dict[str, Any]]], Model]
    build_tools: Callable[[ShellSession], ToolSet]     # ← 新的
    ...
```

于是 `subagent → tools` 这条边消失，`tools → subagent` 变成合法的。

```
(d) cycles with only the tools -> subagent arrow added: none
```

这是最后选的。但在写它之前，得先回答一个问题：**(b) 明明能用，为什么要换？**

---

## §5 (b) 的价格：第二个装配点

第 10 章把 handler 上移到 `__main__.py`。这个动作看起来只是挪了个位置，实际上它
创造了一个东西：**第二个"知道怎么造一个 Agent"的地方**。

第一个在 `__main__.py`：

```python
agent = Agent(
    llm,
    handlers,
    recorder=recorder,
    instructions=_instructions(session),
    context_window=context_window,
    rollout=writer,
    resume_from=resume_from,
    summariser=make_summariser(llm) if context_window else None,
    footprint_of=route_footprint(registry, functools.partial(footprint_of, root=root)),
)
```

第二个在 `subagent.run_task`：

```python
child = Agent(
    ctx.build_model(schemas),
    handlers,
    max_turns=ctx.max_turns,
    instructions=spec.instructions(),
    rollout=writer,
)
```

用 AST 数一下两边传了什么：

```
Agent.__init__ has 10 optional wiring parameters.
  __main__.py         passes  7: context_window, footprint_of, instructions,
                                 recorder, resume_from, rollout, summariser
  subagent.run_task   passes  3: instructions, max_turns, rollout

  only at the top:  context_window, footprint_of, recorder, resume_from, summariser
  only in children: max_turns
  neither:          dialect, max_concurrent_tools
```

`resume_from` 只在顶层是对的（子 Agent 不恢复）。`max_turns` 只在子 Agent 是对的
（第 10 章有 `DEFAULT_TASK_TURNS`）。剩下四个不对。

### 5.1 把它跑出来

AST 差异只是提示。真正的问题是这四个缺失各自意味着什么。跑一遍：同一个脚本模型、
同一批文件、同一套工具，只换 `Wiring`。

```bash
uv run python probe_boundaries.py drift-live
```

```
arm                          final request   recorded   peak    wall
Wiring()  (the old child)          181,040      False      2   0.22s
the parent's wiring                 77,244       True      2   0.23s
```

（`wall` 那一列是墙钟，每次跑都会抖零点零几秒；前三列是确定的。）

三件事：

**一、子 Agent 从不压缩。** 让它连读三次同一个 60KB 的文件——这是子 Agent 最典型
的工作，因为父 Agent 把"去搞清楚 X"这种活派下来正是为了不让那些文件进自己的历史。
最后一次请求 **181,040 字符（约 45,000 token）**，带上父 Agent 的 wiring 之后是
**77,244**。

（第一次测量是拿一个真的顶层 `Agent` 跑同一个脚本，得到 **76,896 字符、压缩一
次**——那个数字留在了 `Wiring` 和 `composition.py` 的 docstring 里，因为它才是
"父 vs 子"的原始对比。上面表里的 77,244 是同一个子 Agent 拿到父 Agent 的 wiring
之后的数字，两者差 0.5%，来自系统消息不同。两个都是真的，分清楚它们各自是什么比
统一成一个好看。）

`context_window` 和 `summariser` 都没传，所以第 6 章整章对子 Agent 是关闭的。

**二、子 Agent 的对话没有被录下来。** `recorder` 没传，
`.minicodex/recordings/` 里只有父 Agent 的那一份。

这条比第一条更难受。第 -1 章立的 F-1-04 是"出了问题不知道模型到底收到了什么"，
整本书的调试基建都建在它上面。而它偏偏对**唯一一个你在终端里看不见的对话**是关
的。子 Agent 的输出只有一行 `[sub-agent depth 1: ok in 2 turn(s)]`；它中间读了
什么、传了什么参数、模型回了什么，不在任何地方。

**三、子 Agent 的工具全部串行。** `footprint_of` 没传，`Agent.__init__` 的默认值
是：

```python
self._footprint_of: FootprintFn = footprint_of or (lambda call: STATEFUL)
```

一切都 STATEFUL，一切都互斥，第 8 章的调度器对子 Agent 也是关闭的。改之前同一个
测量是 **peak 1、0.42 秒**；改之后 peak 2、0.22 秒。

这一条**没有造成错误**，只是慢。而它没造成错误是因为第 8 章那个默认值选对了——
"分不出来就当 STATEFUL"。如果当时默认值写的是 `Footprint()`（什么都不碰），这里
就是第 8 章 F08-01 的数据竞争，30/30 丢编辑。

> 第 8 章为一个保守默认值写了一整段辩护。这里是那段辩护第一次被真的用上，而且是
> 以"一个我们不知道自己犯了的错误没有变成事故"的形式。

### 5.2 为什么没人能发现

这三条都不报错。但更关键的是第二点：

**`subagent.py` 单独读是对的。**

它构造了一个 `Agent`，传了它自己关心的四个参数，每一个都传得有道理。你把这个文件
从头读到尾找不出问题。它错只错在**和三个模块以外的另一个调用点放在一起看的时候**，
而没有任何东西把这两处放在一起。

这才是"第二个装配点"真正的成本。不是重复代码——重复代码 grep 得到。是**一致性
要求不在任何一个文件里**。

这也是为什么它值得一次重构，而 (b) 那个绕法尽管"能用"必须退役：绕开一个环的代价
是把一条不变量从编译器手里交到了人的记忆里。

---

## §6 `ToolSet`：三张表必须一起走

先修工具那一半。

### 6.1 一个已经存在一半的抽象

第 4 章为了同一件事引入过 `ToolSpec`：

```python
@dataclass(frozen=True)
class ToolSpec:
    """One tool, described once.

    Before this existed there were two tables: a `{name: handler}` dict and a
    hand-written list of schemas, with nothing tying them together. ...
    disagreeing was silent in both directions -- a handler with no schema is
    never called, and a schema with no handler makes the model receive the
    "no tool named X" error that chapter 0 wrote for *hallucinated* tool names.
    """
    name: str
    description: str
    parameters: dict[str, Any]
    bind: Callable[[ToolContext], ToolFn]
```

它把 **handler** 和 **schema** 绑在一起了。

第 8 章加了第三张表——`footprint_of`——但**没有并进去**。它单独存在
`tools.py` 里，在 `__main__` 里被 `route_footprint` 单独包一层，然后在
`run_task` 里根本没传。

所以这不是"引入一个新抽象"，是**把一个已经证明过自己的抽象补完**。三张表，两张
已经在一起了，第三张漏了，漏的那张造成了 §5.1 的第三条。

### 6.2 那它应该是一个接口吗

这是 FB-03 的问题，而且必须现在回答，因为答案决定写什么。

条件反射的写法是一个 `Protocol`：

```python
class ToolProvider(Protocol):
    def handlers(self, context: ToolContext) -> dict[str, ToolFn]: ...
    def schemas(self) -> list[dict[str, Any]]: ...
    def footprint(self, call: ToolCall) -> Footprint: ...
```

然后 `tools`、`subagent`、`registry` 各实现一个。三个实现，看起来正好过了三次
法则。

我把它设计出来了，然后砍掉了。理由：

**那三个"实现"没有一个需要是对象。** `tools.py` 里是三个模块级函数，
`registry.py` 里是一个已经存在的类的三个方法，`subagent.py` 里是一个闭包。它们
不需要**是**同一种东西，它们需要**返回**同一种形状。

把三个函数包成三个类去满足一个 `Protocol`，是在给一个值起接口的名字。而且它有
真实成本：`registry.McpRegistry` 会被迫多长三个方法，只为了对上签名；
`spawn_agent` 会被迫从一个 `functools.partial` 变成一个类。

所以 `ToolSet` 是一个**值类型**，不是接口：

```python
@dataclass(frozen=True)
class ToolSet:
    handlers: dict[str, ToolFn]
    schemas: list[dict[str, Any]]
    footprint_of: FootprintFn = _stateful
    callable_without_schema: frozenset[str] = field(default_factory=frozenset)
```

> **接口有实现可以数，值类型没有。** 「这个抽象只有一个实现」这句批评，对值类型
> 根本不成立——它衡量的是错的东西。这不是钻空子；这是说，当你发现自己在数实现
> 个数的时候，先问一遍你需要的是不是一个接口。

### 6.3 那一条规则

`ToolSet` 如果只是把三个字段放一起，就是一个有名字的元组，不值得一个类。它值得
一个类是因为这个：

```python
    def __post_init__(self) -> None:
        declared = {s["function"]["name"] for s in self.schemas}
        handled = set(self.handlers) - self.callable_without_schema
        if declared - handled:
            raise ValueError(f"schema with no handler: {sorted(declared - handled)}")
        if handled - declared:
            raise ValueError(f"handler with no schema: {sorted(handled - declared)}")
```

第 4 章说"这两张表不许不一致"，靠的是把它们从同一个 `ToolSpec` 派生出来——一个
约定。现在它是**构造失败**。

### 6.4 它第一次运行就抓到了一个东西

改完之后跑全量测试，第一个红的不是产品代码，是第 10 章的超时测试：

```
src\minicodex\subagent.py:344: in run_task
    tools = child_tools(ctx)
tests\test_faults_ch10.py:363: in patched
    return replace(tools, handlers={**tools.handlers, "sleep": forever})
...
    def __post_init__(self) -> None:
        ...
>           raise ValueError(f"handler with no schema: {sorted(handled - declared)}")
E           ValueError: handler with no schema: ['sleep']
```

那个测试往子 Agent 的工具表里塞了一个假的 `sleep` handler（第 10 章：唯一能让子
Agent 挂住而不真的起一个进程的办法），**没给它 schema**。

这个抄近路本身无害——那个 `sleep` 只被脚本模型调用，模型不需要看 schema。但它是
一处不变量不成立的地方，而不变量成立与否是这一节的全部主题。补一个 schema 就好：

```python
    def patched(c: SubAgentContext) -> Any:
        # A schema as well as a handler, because `ToolSet` refuses the pair
        # when they disagree -- including when the disagreement is a test
        # taking a shortcut.
        tools = original(c)
        return replace(
            tools,
            handlers={**tools.handlers, "sleep": forever},
            schemas=[*tools.schemas, _sleep_schema()],
        )
```

> 一个不变量落地的当天就抓到东西，是它值得存在的最好证据。抓到的是测试而不是产
> 品代码，不影响这个结论：测试里的抄近路正是"这条规则在这里不成立"的地方。

### 6.5 组合：`plus`，以及路由

三个来源（本项目工具、`spawn_agent`、MCP）在 `__main__` 里原本是三个互不相干的
表达式：

```python
registry = McpRegistry(local=[*TOOL_SCHEMAS, *spawn_schemas])       # schema：列表 splat
handlers = {**bind_all(context), **spawn_handlers, **registry.handlers()}   # handler：dict splat
footprint_of=route_footprint(registry, functools.partial(footprint_of, root=root))  # footprint：包一层
```

**三种说"这三个来源合成一张工具表"的方式，就是三个"加第四个来源时更新了其中两个"
的机会。**

`plus` 把它变成一个：

```python
    def plus(self, other: ToolSet) -> ToolSet:
        clash = sorted(set(self.handlers) & set(other.handlers))
        if clash:
            raise ValueError(f"two tool sets both declare {clash}")
        mine, theirs = frozenset(self.handlers), frozenset(other.handlers)

        def footprint_of(call: ToolCall) -> Footprint:
            if call.name in mine:
                return self.footprint_of(call)
            if call.name in theirs:
                return other.footprint_of(call)
            return STATEFUL

        return ToolSet(
            handlers={**self.handlers, **other.handlers},
            schemas=[*self.schemas, *other.schemas],
            footprint_of=footprint_of,
            callable_without_schema=self.callable_without_schema | other.callable_without_schema,
        )
```

两个细节：

**重名是拒绝，不是覆盖。** 第 9 章为两个 sanitize 之后撞车的远程工具名做过这个决
定（F09-01：五个声明的工具变成四个，然后模型自信地回答了错误的东西）。本地工具和
远程工具撞车是同一件事。

**footprint 按"谁声明的"路由，不按名字长相路由。** 原来的 `route_footprint` 是：

```python
def footprint(call: ToolCall) -> Footprint:
    return registry.footprint_of(call) if is_remote(call.name) else local(call)
```

`is_remote()` 看名字有没有 `mcp__` 前缀。能用，但每加一个工具来源就要加一个分支。
按所有权路由不需要分支——`plus` 已经知道谁声明了什么。

（`route_footprint` 被删掉了。删掉一个函数也是重构结果，值得写一个测试钉住，
不然过一阵它会被人重新加在替代它的东西旁边。见 §10。）

### 6.6 一个例外，明说出来

第 9 章的 registry 有一种工具：**有 handler，没有可见的 schema**。那是
`tool_search` 后面延迟加载的工具——模型通过搜索得知它存在之后必须能在下一轮就调
用它，所以 handler 一直在，schema 要等 `reveal()`。

这直接违反 §6.3 的规则。两个选择：

- 把规则放松成"handler 可以多于 schema"——那样规则就管不住 §5.1 的第三条了；
- 把例外**命名**：

```python
    # Names this set is responsible for even though no schema declares them.
    # Empty for every set except the one chapter 9's registry produces, where
    # a deferred tool is callable before its schema has been revealed -- said
    # out loud as an exception rather than by weakening the rule for everyone.
    callable_without_schema: frozenset[str] = field(default_factory=frozenset)
```

选第二个。一个字段名 + 一段注释，换来规则对其他所有人保持严格。

### 6.7 `ToolSet` 放哪：第三次下沉

`subagent.py` 需要说出 `ToolSet` 这个词（它的 `build_tools` 返回这个）。
`tools.py` 也需要（它生产这个）。所以 `ToolSet` 不能住在 `tools.py`——那就是要被
去掉的那条边。

放 `agent_types.py`。这是第 1 章那条规则的**第三次**应用：

| 什么时候 | 搬了什么 | 为什么 |
|---|---|---|
| 第 1 章 | `ToolCall` | `history` 要它，`agent` 要 `History` |
| 插曲 A | `ToolFn` | `tools` 要它，住在 `agent.py` 是反向箭头 |
| 插曲 B | `Footprint` / `STATEFUL` / `FootprintFn` / `ToolSet` | `subagent` 要说出 footprint 的类型 |

第三次是"这是个规则"而不是"这是个修法"的分界线。所以我把它写进了
`agent_types.py` 的 docstring：

```
`Footprint` and `ToolSet` arrived in interlude B, by the same rule for the
third time.  Three applications is where "we moved a type down" stops being an
incident and starts being the layering rule this package is checked against:
**a type two layers need lives below both of them, and a module that owns
behaviour does not also own the vocabulary its callers speak.**  `scheduler.py`
still owns everything you can *do* with a `Footprint`; it stopped owning the
word.
```

`scheduler.py` 剩下的就是行为，加一行再导出，让所有既有 import 不用改：

```python
from minicodex.agent_types import STATEFUL, Footprint, FootprintFn, ToolCall

__all__ = ["STATEFUL", "Footprint", "FootprintFn", "batches", "conflicts"]


def conflicts(a: Footprint, b: Footprint) -> bool: ...
def batches(calls: Sequence[ToolCall], footprint_of: FootprintFn) -> list[list[ToolCall]]: ...
```

**词搬走了，行为没搬。** 这一点有测试钉着（§10）——因为"把类型下沉"很容易顺手把
函数也带下去，那就变成了 `agent_types.py` 开始长逻辑，而它必须是叶子。

---

## §7 `Wiring`：整个包里唯一造 `Agent` 的地方

工具那一半修完了。剩下四个参数——`recorder` / `context_window` / `summariser` /
`max_concurrent_tools`——是"这次运行怎么配置"，和工具无关。

最省事的修法是给 `SubAgentContext` 加四个字段，然后在 `__main__` 里填上。这确实能
让今天的测量变绿。但它没有修掉产生这个 bug 的东西：`Agent.__init__` 有十个可选参
数，有两个地方在调它，**下次加第十一个参数时同样的事会再发生一次**。

所以修的是"有两个地方在调它"：

```python
@dataclass(frozen=True)
class Wiring:
    """Everything an `Agent` is given that is neither its model nor its tools."""

    recorder: Recorder = NULL_RECORDER
    context_window: int | None = None
    summariser: Summariser | None = None
    max_concurrent_tools: int = DEFAULT_MAX_CONCURRENT_TOOLS
    dialect: Dialect = "chat_completions"

    def agent(
        self,
        model: Model,
        tools: ToolSet,
        *,
        max_turns: int = DEFAULT_MAX_TURNS,
        instructions: str | None = None,
        rollout: RolloutWriter = NULL_WRITER,
        resume_from: History | None = None,
    ) -> Agent:
        return Agent(
            model,
            tools.handlers,
            footprint_of=tools.footprint_of,
            max_turns=max_turns,
            recorder=self.recorder,
            dialect=self.dialect,
            instructions=instructions,
            context_window=self.context_window,
            summariser=self.summariser,
            rollout=rollout,
            resume_from=resume_from,
            max_concurrent_tools=self.max_concurrent_tools,
        )
```

### 7.1 哪四个留在外面

剩在 `agent()` 关键字参数里的四个，是**父子之间本来就该不一样**的：

| 参数 | 为什么父子不同 |
|---|---|
| `max_turns` | 子任务的预算更小（第 10 章 `DEFAULT_TASK_TURNS = 8`） |
| `instructions` | 子 Agent 的系统消息是任务契约，不是通用 prompt（F10-04：约束放错地方 5/5 被无视） |
| `rollout` | 各自的会话文件（F10-12） |
| `resume_from` | 只有顶层会恢复 |

用 AST 数一遍，确认没有第十一个漏网的：

```
Agent.__init__ takes 10 arguments beyond model and tools.
`Agent(...)` call sites in src/: {'agent.py': 1}

  carried by Wiring / ToolSet (6): context_window, dialect, footprint_of,
                                   max_concurrent_tools, recorder, summariser
  chosen per call (4):      instructions, max_turns, resume_from, rollout
  in neither (0):                -
```

`Agent(...)` 在整个 `src/` 里只剩一处，就在 `Wiring.agent` 里面。

> **一个参数只有一个地方可能忘记传的时候，它就不再是"容易忘"的参数了。**

这句话是 §11 里的第四条 CI 规则：`src/` 里 `Agent(` 只许出现在 `agent.py`。

### 7.2 `Wiring` 会不会长成一个垃圾桶

会。这是这类类的标准死法：叫 `Config` 或者 `Context` 或者 `Wiring`，一开始四个
字段，两年后四十个，谁都不敢删。

所以它有一个测试：

```python
def test_FB_03_wiring_carries_only_what_two_call_sites_both_need() -> None:
    fields = set(Wiring.__dataclass_fields__)
    assert fields == {
        "recorder",
        "context_window",
        "summariser",
        "max_concurrent_tools",
        "dialect",
    }
```

这个测试不阻止你加字段，它逼你在加的时候改一行测试并说明理由。这是能做到的最便宜
的门槛，也差不多是唯一有效的那种。

（`dialect` 是第五个，它不是"子 Agent 缺的"，它是"父子之间绝对不能不一样的"——
父子用不同的 wire 方言是一个没人想 debug 的故障。同一个类里放这两种理由是可以的，
因为它们的结论一样：这个值必须往下传。）

---

## §8 倒置：`subagent.py` 不再认识 `tools.py`

现在做 §4(d)。

`subagent.py` 原来从 `tools.py` 拿五样东西：

```python
from minicodex.tools import ToolContext, ToolSpec, bind_all, tool_context, tool_schemas
```

用在两个地方：`child_tools()` 造子 Agent 的工具表，`spawn_spec()` 描述
`spawn_agent` 这个工具本身。

### 8.1 `build_tools`

`SubAgentContext` 已经有一个先例了——`build_model`：

```python
    # `build_model` is a factory rather than a model because the child is shown a
    # different tool list from the parent -- no `spawn_agent` at the bottom level
    # -- and a chat-completions client carries its tool list.
    build_model: Callable[[list[dict[str, Any]]], Model]
```

第 10 章已经为"模型怎么造"做过一次倒置。工具怎么造是同一形状的问题，用同一个办法：

```python
    # How a child's own tools get built.  A callable supplied from above rather
    # than a call to `tools.bind_all` here, and that inversion is the whole of
    # interlude B: written the direct way, `subagent` imports `tools`, which
    # forbids `tools` from ever holding the `spawn_agent` handler -- the module
    # where every other handler lives.
    #
    # Given the child's shell -- seeded from the parent's, so a child starts
    # where the parent is standing rather than where the process started.
    build_tools: Callable[[ShellSession], ToolSet] = lambda _shell: ToolSet({}, [])
    wiring: Wiring = field(default_factory=Wiring)
```

参数是 `ShellSession` 而不是整个 context，因为 shell 是**唯一一样必须在这里造**
的东西：子 Agent 要一个新的 `ShellSession`，但它的 `cwd` 要从父 Agent 那里继承
（F10-01：父 `cd src` 之后子 Agent `pwd` 回仓库根，静默，错）。其余全在上面决定。

`child_tools` 于是变成：

```python
def child_tools(ctx: SubAgentContext) -> ToolSet:
    shell = ShellSession(timeout=ctx.parent_shell.timeout)
    shell.cwd = ctx.parent_shell.cwd

    tools = ctx.build_tools(shell)
    if ctx.depth + 1 < ctx.max_depth:
        tools = tools.plus(spawn_toolset(replace(ctx, depth=ctx.depth + 1)))
    return tools
```

深度判断留在这里，因为它是**唯一真正属于这一层的决定**：只有 `subagent.py` 知道
现在是第几层。（"到底了就不给这个工具，而不是给了再拒绝"是第 5 章 F05-10 的结论：
prompt 里出现一个策略不允许的工具名，模型会去调它，2/3。）

### 8.2 `spawn_spec` → `spawn_toolset`

原来 `spawn_agent` 借用了 `tools.ToolSpec`：

```python
def spawn_spec(ctx: SubAgentContext) -> ToolSpec:
    return ToolSpec(
        name=SPAWN_NAME,
        description=SPAWN_DESCRIPTION,
        parameters=SPAWN_PARAMETERS,
        bind=lambda _context: functools.partial(spawn_agent, ctx),
    )
```

注意那个 `bind=lambda _context: ...`——它接一个 `ToolContext` 然后**扔掉**，因为
子 Agent 的上下文是另一个对象。第 10 章自己在注释里承认了这是个接缝：

> `bind` takes the `ToolContext` every spec takes and ignores it, because a
> sub-agent's context is a different object -- **the one seam left over from
> moving this handler out of `tools.py`**.

现在这个接缝可以直接消失，因为 `ToolSet` 就是要交换的那个形状：

```python
def spawn_toolset(ctx: SubAgentContext) -> ToolSet:
    return ToolSet(
        handlers={SPAWN_NAME: functools.partial(spawn_agent, ctx)},
        schemas=[
            {
                "type": "function",
                "function": {
                    "name": SPAWN_NAME,
                    "description": SPAWN_DESCRIPTION,
                    "parameters": SPAWN_PARAMETERS,
                },
            }
        ],
        footprint_of=spawn_footprint,
    )
```

顺手把一件"第 10 章蒙对的事"变成明说的：

```python
def spawn_footprint(_call: ToolCall) -> Footprint:
    """A spawn touches whatever its child touches, which is unknowable here.

    Chapter 10 got this right by accident: `tools.footprint_of` returns
    `STATEFUL` for any name it does not recognise, and `spawn_agent` was such
    a name.  Accidents are worth converting into statements -- the measured
    alternative, calling a spawn read-only, loses one of two concurrent edits
    29-30 times out of 30.
    """
    return STATEFUL
```

第 10 章 F10-02 的结论是"第 8 章的默认值已经替我们挡住了"。挡住了，但挡住的方式是
"没人给它分类"。现在有人给它分类了，分的还是同一个答案——区别是删掉那行会有测试
变红。

### 8.3 `run_task` 里那一行

```python
    tools = child_tools(ctx)
    writer = _writer(ctx)
    # The parent's `Wiring`, not a fresh one: recorder, context window,
    # summariser and concurrency cap all cross the boundary as one object.
    # This line is the fix for interlude B's whole measured fault, and it is
    # one line only because `Wiring` exists.
    child = ctx.wiring.agent(
        ctx.build_model(tools.schemas),
        tools,
        max_turns=ctx.max_turns,
        instructions=spec.instructions(),
        rollout=writer,
    )
```

§5.1 那三条静默故障，全部在这一行里修完。

### 8.4 箭头没了之后

```bash
uv run python probe_boundaries.py graph
```

`subagent` 从第 5 层降到第 4 层，和 `tools`、`registry` 平级。`tools → subagent`
现在是合法的。

**然后我没有把 `spawn_agent` 搬回去。**

这是这一章我最想让人记住的一个决定。整章都在讲"第 10 章为了绕开环把工具放错了地
方"，现在环没了，把它搬回来看起来是这个故事的结局。

但：

- 它现在在 `subagent.py` 里，和 `run_task`、`TaskSpec`、`TaskResult` 在一起，
  读起来完全合理；
- 搬回 `tools.py` 需要 `tools` 认识 `SubAgentContext`，那是一条新的边；
- 唯一的收益是"handler 都在一个文件里"，那是整齐，不是正确。

> **一个重构变得可能，不等于它变得必要。** 环是要修的，因为它有真实代价（§5）。
> 位置是审美的。把两件事分开，是这一章和"顺手把代码改好看"之间的全部区别——插曲
> A 的 FA-01 说的就是这个。

---

## §9 `composition.py`：装配根

`build_tools` 和 `wiring` 得有人递进去。递的人是谁？

第 10 章的答案是 `__main__.py`。但 `__main__.py` 是 419 行的 argparse + async
main + 一堆 print，**没有任何测试碰得到它**——这正是 §5 那个 bug 能活一整章的直接
原因：那两个装配点里，有一个在测试的射程之外。

所以把装配的部分抽出来：

```python
"""Where a run is assembled: tools in, one `ToolSet` out, for parents and children.
...
Deliberately not here: argument parsing, printing, the session file, the
approval prompt.  Those stayed in `__main__`.  A composition root that also
does IO is a composition root you cannot call from a test, which is the state
this package was in when the drift above went unnoticed for a chapter.
"""
```

完整内容（171 行，去掉 docstring 后大约 60 行代码）：

```python
def local_tools(root: Path, session: Session, shell: ShellSession | None = None) -> ToolSet:
    """This project's own tools, bound to one repository and one conversation."""
    context = tool_context(root=root, session=session, shell=shell)
    return ToolSet(
        handlers=bind_all(context),
        schemas=list(tool_schemas()),
        footprint_of=functools.partial(footprint_of, root=context.root),
    )


def child_tools_builder(root: Path, session: Session) -> Callable[[ShellSession], ToolSet]:
    """What `SubAgentContext.build_tools` gets: a child's local tools, given its shell."""
    return lambda shell: local_tools(root, session, shell)


def top_level_tools(root: Path, session: Session, sub_context: SubAgentContext) -> ToolSet:
    """Local tools plus `spawn_agent`, before any MCP server is consulted."""
    return local_tools(root, session, sub_context.parent_shell).plus(spawn_toolset(sub_context))


def with_remote_tools(base: ToolSet, registry: McpRegistry) -> ToolSet:
    """Add chapter 9's registry to a local tool set."""
    if not registry.local:
        raise ValueError(
            "McpRegistry must be constructed with local=<the base set's schemas>; "
            "it owns the list the model client holds"
        )

    handlers = {**base.handlers, **registry.handlers()}
    if registry.deferred:
        handlers["tool_search"] = registry.search_handler()

    def routed(call: ToolCall) -> Any:
        return registry.footprint_of(call) if is_remote(call.name) else base.footprint_of(call)

    return ToolSet(
        handlers=handlers,
        schemas=registry.visible,
        footprint_of=routed,
        callable_without_schema=frozenset(registry.deferred),
    )


def sub_context(*, root, session, parent_shell, build_model, wiring, **rest) -> SubAgentContext:
    """A `SubAgentContext` with `build_tools` already wired to this run."""
    return SubAgentContext(
        build_model=build_model,
        root=root,
        session=session,
        parent_shell=parent_shell,
        build_tools=child_tools_builder(root, session),
        wiring=wiring,
        **rest,
    )
```

### 9.1 `with_remote_tools` 为什么不是 `plus`

这是整套装配里唯一一处不对称，而且它是**撞出来的**，不是设计出来的。

第 9 章的 registry 拥有一个**活的** schema 列表：

```python
        # `visible` is mutated in place rather than rebound, because the model
        # client holds a reference to this exact list object.
        self.visible: list[dict[str, Any]] = list(self.local)
```

`tool_search` 揭示一个延迟工具的方式是往这个对象里 `append`，下一轮请求自动带上。
而 `plus` 会造一个**新列表**（`schemas=[*self.schemas, *other.schemas]`），一旦
registry 的集合被 `plus` 过一次，`tool_search` 就静默失效了。

所以顺序是：本地工具 → `plus(spawn)` → 把结果的 schemas 交给
`McpRegistry(local=...)` → registry 产出的那个集合是最后的答案。

我是怎么知道要写这个 `if not registry.local` 的？把第 9 章的一个测试改到新 API
的时候写反了：

```python
    registry = McpRegistry()          # 忘了 local=
    combined = with_remote_tools(base, registry)
```

```
ValueError: handler with no schema: ['apply_patch', 'read_file',
                                     'request_permissions', 'run_shell']
```

`ToolSet` 确实拒绝了——但它抱怨的是四个 handler，而真正的问题是两行代码的顺序。
所以加一条前置条件，让消息指向病因：

```python
    # The one ordering rule in this module, said out loud. Building the
    # registry before the local set, or forgetting `local=`, produces a tool
    # set whose schemas are missing every local tool -- which `ToolSet` does
    # refuse, but with a message about four handlers rather than about the two
    # lines that are in the wrong order.
```

> 第 3 章 F03-07 的规则（"错误信息即 prompt"）是写给模型的。同一条规则对人一样：
> **一个正确但指向症状的错误消息，和一个指向病因的错误消息，差一次调试。**

### 9.2 `__main__.py` 现在长什么样

```python
    root = Path.cwd().resolve()
    context = tool_context(root=root, session=session)
    wiring = Wiring(recorder=recorder, context_window=context_window, summariser=None)
    ...
    sub_ctx = sub_context(
        build_model=make_model,
        root=root,
        session=session,
        parent_shell=context.shell,
        wiring=wiring,
        sessions_dir=session_dir,
        parent_session_id=current.session_id,
        provider=provider,
        model=model or default_model,
        announce=print,
    )
    tools = top_level_tools(root, session, sub_ctx)

    registry = McpRegistry(local=tools.schemas)
    ...
    tools = with_remote_tools(tools, registry)
    llm = make_model(tools.schemas)
    if context_window:
        wiring = replace(wiring, summariser=make_summariser(llm))
        sub_ctx = replace(sub_ctx, wiring=wiring)
    ...
    agent = wiring.agent(
        llm,
        tools,
        instructions=_instructions(session),
        rollout=writer,
        resume_from=resume_from,
    )
```

那个 `replace(wiring, summariser=...)` 有点丑，理由是真的：summariser 需要
`llm`，`llm` 需要工具列表，工具列表需要 registry。三者有一个真实的先后顺序。
`replace` 而不是造第二个 `Wiring`，是为了保住"只有一个 wiring 对象往下传"这件事
——`sub_ctx` 也跟着 `replace` 一次，是因为子 Agent 必须拿到带 summariser 的那个。

---

## §10 两个被删掉的抽象

FB-03 说的是"为打断循环而引入的接口只有一个实现，纯属噪音"。这一章的答案是：不但
没引入，还删了两个。

**一、`route_footprint`。** 存在于 `registry.py`，只有一个调用者，每加一种工具
来源要加一个分支。被 `ToolSet.plus` 的所有权路由取代。删掉之后钉一个测试：

```python
def test_FB_03_footprints_did_not_get_their_own_abstraction() -> None:
    """The rejected inversion, pinned so it stays rejected.
    ...
    Removing a function is a refactor result worth asserting -- otherwise
    somebody re-adds it next to the thing that replaced it.
    """
    import minicodex.registry as registry_module

    assert not hasattr(registry_module, "route_footprint")
```

**二、`spawn_spec` 借用的 `ToolSpec`。** 它存在的唯一目的是从"环所在的那个模块"
借一个类。

以及一个**被设计出来然后砍掉**的：§6.2 的 `ToolProvider` Protocol。

三样东西的判据是同一个：

> 问"这个抽象有几个实现"之前，先问"我需要的是接口还是形状"。
> **需要接口的时候，实现少于两个是噪音；需要形状的时候，这个问题根本不适用。**

再钉一个测试，防止 `ToolSet` 变成假的三次法则：

```python
def test_FB_03_the_shared_thing_is_a_value_with_three_producers(tmp_path: Path) -> None:
    produced = [
        local_tools(tmp_path, session),
        spawn_toolset(ctx),
        child_tools(ctx),
    ]
    assert all(isinstance(t, ToolSet) for t in produced)
    assert len({tuple(sorted(t.handlers)) for t in produced}) == 3
```

最后一行是重点：三个生产者产出的是**三张不同的表**。如果它们内容一样，那就不是
三个样本，是一个样本抄了三遍——第三次法则要的是三个不同的样本。

还有一个守着 §6.7 那次下沉的：

```python
def test_FB_03_the_footprint_type_stayed_where_behaviour_can_use_it() -> None:
    """Moving a type down must not move the behaviour with it."""
    import minicodex.scheduler as scheduler_module

    assert scheduler_module.Footprint is Footprint
    assert callable(scheduler_module.conflicts)
    assert "conflicts" not in dir(sys.modules["minicodex.agent_types"])
```

---

## §11 FB-02：把边界写成检查

到这里，代码是对的。问题是它三个月后还对不对。

第 10 章其实已经做了正确的事——它把那条边写成了测试：

```python
def test_tools_does_not_import_subagent() -> None:
    assert "minicodex.subagent" not in imported_modules(SRC / "tools.py")
```

而且这条测试一直是绿的，规则守住了。所以 FB-02 清单上写的"边界修好了，三个月后
又被人破坏"，在这个项目里**没有以那个形式发生**。

发生的是另一件事，而且更难防：**规则守住了，但检查规则的东西在测量错的东西。**

- §2.1：环检查器在一个装不起来的包上报告 `none`；
- §3.1：`import minicodex` 在一个所有模块都装不起来的包上退出 0；
- §4(c)：把 import 挪进函数体能骗过任何只读文件头部的检查。

三条都是"检查通过了，因为检查看不见"。所以这一节做的不是"加一条规则"，是**做一个
能被证明会失败的检查器**。

### 11.1 为什么是脚本不是 pytest

第 10 章的版本是 `tests/test_boundaries.py`，跑在 pytest 里。这一章把它变成
`scripts/check_layers.py`，pytest 里只留一个"这个脚本退出 0"和若干"每条规则都能
红"的测试。

理由有三条，只有第一条重要：

1. **它得能跑在别的树上。** `--src` 指一个临时目录，是 §11.5 那些变异测试唯一
   可行的写法。写成 pytest 测试就只能测自己这棵树，而自己这棵树永远是绿的。
2. 它要开 25 个子进程，3 秒。放在 lint 步骤里比混在 1400 个测试里合适。
3. codex 自己就是这么做的（§12）。

### 11.2 五条规则

每一条对应一次真实发生过的事：

| 规则 | 来自 | 一句话 |
|---|---|---|
| `no import cycles` | FB-01、F01-08、FA-04 | AST 全树遍历，函数体里的 import 也算 |
| `__init__ is a leaf` | §2 | 单独一条，因为**消息**必须不一样 |
| `forbidden edges` | FA-04、F10-06、FB-01、F01-08 | 五条具名的边，每条带故障号 |
| `one Agent construction site` | §5 | `Agent(` 只许出现在 `agent.py` |
| `every module imports alone` | §3.1 | 每个模块一个新解释器 |

第二条值得解释一下，因为它和第一条重叠：环检查已经能抓住 `__init__` 那个环了。
留着它是因为**消息**：

```
no import cycles: import cycle: minicodex -> minicodex.agent -> minicodex.compaction
```

这句话是对的，而且完全没告诉你该删哪一行。而：

```
__init__ is a leaf: __init__.py imports ['minicodex.agent'] from its own package.
Anything doing `from minicodex import <name>` now depends on all of it, and the
package stops importing (FB-01).
```

这句话告诉你去哪、删什么、为什么。**一条规则的价值有一半在它失败时说的话里。**

### 11.3 禁边表，和每条边的身份证

```python
FORBIDDEN: list[tuple[str, str, str]] = [
    ("tools", "agent", "FA-04: the loop is above the tool machinery, not below it"),
    ("tools", "subagent", "F10-06: putting the spawn handler with the handlers"),
    (
        "subagent",
        "tools",
        "FB-01: the arrow that made the line above impossible. Interlude B "
        "inverted it -- subagent takes `build_tools` from its caller",
    ),
    ("history", "agent", "F01-08: the first cycle in this project"),
    ("agent_types", "*", "F01-08: the shared-type module only works while it is a leaf"),
]
```

第三个字段不是注释，是有测试管的：

```python
def test_FB_02_every_forbidden_edge_names_the_fault_it_came_from() -> None:
    """The rule that keeps the rule list honest.

    An architecture check accumulates entries, and an entry with no reason is
    an entry nobody can ever delete -- it will be obeyed forever by people
    guessing at what it was for. Every edge in `FORBIDDEN` carries a fault ID
    from FAULTS.md, so each one can be looked up, and argued with.
    """
    for source, target, why in checker.FORBIDDEN:
        assert any(marker in why for marker in ("F0", "F1", "FA-", "FB-")), (source, target, why)
```

架构检查是会积累条目的东西。**一条没有理由的规则是一条永远删不掉的规则**——后来
的人只能猜它是干嘛的，然后继续遵守它。要求每条边带一个能查的故障号，是让这张表
在三年后还能被反驳的唯一办法。

### 11.4 它失败的样子

把 §2 和 §5 的两个错误同时放回去（都在一份临时副本里），然后：

```bash
uv run python scripts/check_layers.py --src /tmp/broken/minicodex --quiet
```

```
no import cycles: import cycle: minicodex -> minicodex.agent -> minicodex.compaction
__init__ is a leaf: __init__.py imports ['minicodex.agent'] from its own package.
  Anything doing `from minicodex import <name>` now depends on all of it, and the
  package stops importing (FB-01).
one Agent construction site: subagent.py constructs an Agent. Build it through
  `Wiring.agent` instead: a second construction site is how a child ended up
  without a recorder, without compaction and without the scheduler, silently,
  for a chapter (FB-01).
every module imports alone: 25 modules do not import on their own: ImportError:
  cannot import name 'compaction_prompt' from partially initialized module
  'minicodex' (most likely due to a circular import) (...\__init__.py)
exit 1
```

最后一行原本是**二十五段一模一样的话**。`__init__` 里的环让每一个模块都以同一句
话失败，第一版就老老实实全打出来了。一份没人会读到底的报告等于没有报告：

```python
    # Grouped by message, because a cycle through `__init__` fails *every*
    # module with the same line: the first version of this printed twenty-five
    # identical paragraphs, which is a report nobody reads to the end.
    problems = []
    for message, modules in failures.items():
        if len(modules) > 3:
            problems.append(f"{len(modules)} modules do not import on their own: {message}")
        else:
            problems.extend(f"{m} does not import on its own: {message}" for m in modules)
```

### 11.5 每条规则都要被证明会红

```python
@pytest.mark.parametrize(
    "filename,old,new,rule",
    [
        ("subagent.py", ..., "from minicodex.tools import bind_all", "check_forbidden"),
        ("agent_types.py", ..., "from minicodex.tool_errors import tool_error", "check_forbidden"),
        ("subagent.py", "child = ctx.wiring.agent(", "... Agent(", "check_one_agent_construction"),
    ],
)
def test_FB_02_each_rule_can_fail(filename: str, old: str, new: str, rule: str) -> None:
    """A check nobody has seen fail is a check nobody knows works.

    Chapters 5, 6, 9 and 10 each shipped a test whose name claimed more than
    its assertions, every one of them found by mutation rather than by
    reading. A boundary checker is the same shape of thing -- it reports
    "ok" by default -- so each of its rules gets a mutation that must turn it
    red.
    """
    with tempfile.TemporaryDirectory() as tmp:
        broken = a_copy_of_src(Path(tmp), (filename, old, new))
        assert getattr(checker, rule)(broken), f"{rule} did not notice {filename}"
```

另外三条各有自己的测试，因为它们要断言的不只是"红了"：

- `test_FB_02_a_convenience_import_in_init_is_caught` —— 顺便断言环检查**也**看得见
  （§2.1 的那个 bug 就是这一条没写造成的）；
- `test_FB_02_a_package_smoke_test_would_not_have_caught_the_other_one` —— 先断言
  `import minicodex` 退出 0，**再**断言逐模块检查不放过它。这个测试的前半段在断
  言"显而易见的做法是不够的"，那是它真正的内容；
- `test_FB_02_a_deferred_import_is_still_an_edge` —— 断言包能 import（"这就是陷
  阱"）**并且**AST 检查照样报环。

所有这些都在 `src/` 的**副本**上做。第 6 章的变异脚本用 `try/finally` 还原，而
`finally` 不会在父进程被 SIGINT 打死的时候跑——结果 `if False:` 在 `tokens.py` 里
待了一段时间，套件是绿的。**一个会改你源码的工具，就是一个可能把你源码改坏了留在
那儿的工具。**

### 11.6 放在哪个 CI 步骤：第 -1 章的护栏又拦了一次

第 -1 章给 blocking CI 立了个规矩，写成断言：

```python
assert len(steps) <= 6, "the blocking suite is meant to stay fast"
```

现在 `ci.yml` 正好 6 步。加第七步会红。

第 9 章撞过一次同样的墙，当时的答案是**开第二个 workflow**（`postmerge.yml`），
理由是变异测试衡量的是测试质量而不是这次改动的正确性，它不该挡合并。

这一次答案不一样，而且必须不一样：

- 一个环会让整个包装不起来。它**必须**挡合并。
- 它是一个 3 秒的静态检查。

所以它进 lint 步骤，不新开步骤：

```yaml
      - name: Lint
        run: |
          uv run ruff format --check .
          uv run ruff check .
          # Interlude B. In this step and not a step of its own: chapter -1's
          # guard caps the blocking workflow at six steps, and a static check
          # that finishes in three seconds is what a lint step is. Chapter 9
          # answered the same question the other way (a second workflow),
          # because a mutation run measures the tests and not the change.
          #
          # What it stops: an import cycle, including the one an ordinary
          # convenience import in __init__.py creates, and including one
          # deferred into a function body so that startup still works.
          # `import minicodex` -- the obvious smoke test -- exits 0 on a
          # package whose modules cannot import each other, which is why this
          # imports every module in its own interpreter.
          uv run python scripts/check_layers.py --quiet
```

> 一条护栏第二次拦住你，答案和第一次不一样，说明它拦的是对的东西。如果每次的答案
> 都是"把上限从 6 改成 7"，那这条护栏从来没起过作用。

### 11.7 没写的规则

这一节和写了的规则一样重要，而且直接写在脚本文件底部，不是写在这里：

```python
# **A declared layer number for every module.**  Designed, then dropped.  It
# would catch every arrow the list above catches and more, at the cost of an
# entry per module forever, and the "more" is hypothetical: three backwards
# arrows have actually happened in this project and all three are named above.
# A rule whose maintenance is certain and whose value is speculative is the
# ceremonial half of FB-03.
#
# **A maximum module size.**  codex's AGENTS.md has one -- a file over roughly
# 800 LoC should get new functionality in a new module rather than be extended
# -- and interlude A measured why it does not belong here: `agent.py` was 212
# lines and perfectly healthy, and the duplication
# that actually mattered was three copies of a clipper across three files that
# were each small.  Line count measures volume, not coupling.
#
# **A maximum fan-in or fan-out.**  `agent_types` has a fan-in of eight, which
# is the *point* of it.  A number would have to be tuned until it stopped
# firing, which means it was never measuring anything.
```

第一条最想说：**分层表是这一章最容易犯的过度设计。** 我已经算出每个模块的层号了
（§1 那张表），把它固化成规则只要十行。但那意味着以后每加一个模块都要改这张表，
换来的是"防住一些还没发生过的箭头"。已经发生过的三条，全都在 `FORBIDDEN` 里，
一条五个字。

**维护成本确定、收益靠猜的规则**，就是 FB-03 说的仪式性抽象，只不过换了个形状。

---

## §12 codex 是怎么做的

codex 有一模一样的东西，而且几乎是同一个形状。

`.github/scripts/verify_tui_core_boundary.py`：

```python
"""Verify codex-tui does not depend on or import codex-core directly."""

FORBIDDEN_PACKAGE = "codex-core"
FORBIDDEN_SOURCE_PATTERNS = (
    re.compile(r"\bcodex_core::"),
    re.compile(r"\buse\s+codex_core\b"),
    re.compile(r"\bextern\s+crate\s+codex_core\b"),
)
```

它查两样东西：`Cargo.toml` 里的依赖声明（包括 `dev-dependencies` 和
`build-dependencies`，以及每个 target 的），和 `**/*.rs` 里的源码模式。

在 `.github/workflows/repo-checks.yml` 里，和另外两个同类检查排在一起：

```yaml
      - name: Verify codex-rs Cargo manifests inherit workspace settings
        run: python3 .github/scripts/verify_cargo_workspace_manifests.py

      - name: Verify codex-tui does not import codex-core directly
        run: python3 .github/scripts/verify_tui_core_boundary.py

      - name: Verify Bazel clippy flags match Cargo workspace lints
        run: python3 .github/scripts/verify_bazel_clippy_lints.py
```

三件事和这一章一致：**是脚本不是测试；查的是具体的一条边而不是一张分层表；失败
时打印的是怎么办**——

```python
    print("codex-tui must not depend on or import codex-core directly.")
    print(
        "Use the app-server protocol/client boundary instead; temporary embedded "
        "startup gaps belong behind codex_app_server_client::legacy_core."
    )
```

### 12.1 那个逃生门

最值得看的是最后半句：`codex_app_server_client::legacy_core`。

```rust
/// Transitional access to core-only embedded app-server types.
///
/// New TUI behavior should prefer the app-server protocol methods. This
/// module exists so clients can remove a direct `codex-core` dependency
/// while legacy startup/config paths are migrated to RPCs.
pub mod legacy_core {
    pub mod config {
        pub use codex_core::config::*;

        pub mod edit {
            pub use codex_core::config::edit::*;
        }
    }
}
```

也就是说：**边界是强制的，但不是没有例外的。** 例外全部走一扇有名字的门，于是
它们可数：

```bash
$ grep -rn "legacy_core" codex-rs/tui/src/ | wc -l
101
```

101 处。TUI 依然在用 core 的 config 类型，但这件事不是散落在 101 个 `use
codex_core::` 里，而是集中成一个模块名，任何人都能一行命令数出还欠多少。

这和这一章 §6.6 的 `callable_without_schema` 是同一个手法，也和第 2 章
F02-10 那个 `skipif` 是同一个手法：

> **一个规则挡不住的现实，要么写成一个具名的例外，要么把规则删掉。**
> 最差的选择是把规则放松到"技术上没有违反"。

（顺带：这个检查只查 `codex-tui` 的**直接**依赖。`codex-app-server-client`
自己是 depend 在 `codex-core` 上的，所以 core 依然在 TUI 的传递依赖树里。检查器
查的是箭头，不是可达性——这是有意的，而且和 §11 那五条规则一样，是一个"知道自己
在测什么"的检查。）

---

## §13 文件清点

| 文件 | 行数 | 这一章做了什么 | 完整代码在 |
|---|---|---|---|
| `agent_types.py` | 45 → 170 | `Footprint` / `STATEFUL` / `FootprintFn` 下沉；新增 `ToolSet` 与 `plus` | §6.3、§6.5、§6.6 |
| `scheduler.py` | 100 → 73 | 只剩 `conflicts` / `batches`，类型改为再导出 | §6.7 |
| `agent.py` | 424 → 489 | 新增 `Wiring` 与 `Wiring.agent`；`Agent` 本身一行未改 | §7 |
| `subagent.py` | 573 → 614 | 去掉 `tools` 依赖；`build_tools` / `wiring` 字段；`spawn_spec` → `spawn_toolset` | §8 |
| `composition.py` | 新增 171 | 装配根 | §9 |
| `registry.py` | 670 → 655 | 删掉 `route_footprint` | §6.5 |
| `__main__.py` | 419 → 424 | 改为调用 `composition` 和 `wiring.agent` | §9.2 |
| `scripts/check_layers.py` | 新增 302 | 五条规则 | §11.2–11.7 |
| `tests/test_faults_chB.py` | 新增 490 | 22 个测试 | 散见各节 |
| `probe_boundaries.py` | 新增 512 | 六个测量段 | 散见各节 |

没进正文的：`check_layers.py` 里的 Tarjan 实现（教科书算法，只有"写成迭代版"这个
选择值得一提——一个自己会栈溢出的环检查器不是你在图已经坏了的时候想要的工具）；
`probe_boundaries.py` 里 `in_a_copy` 的实现细节。

`Agent` 类本身**一行没改**，这是有意的：这一章改的全部是"谁来造它、拿什么造"。

---

## §14 验证

```bash
$ uv run pytest
1414 passed, 9 skipped in 63.76s
```

改之前是 1392。新增 22 个，**一个既有测试都没有因为"行为变了"而改断言**——改动
的只有测试的**构造方式**（`context_for` 改用 `sub_context`，两个 patched 工具表
补 schema，一个 ch09 测试改用 `with_remote_tools`）。

```bash
$ uv run python scripts/check_layers.py
ok  no import cycles
ok  __init__ is a leaf
ok  forbidden edges
ok  one Agent construction site
ok  every module imports alone
```

```bash
$ uv run ruff format --check . && uv run ruff check .
65 files already formatted
All checks passed!
```

---

## §15 收工：commit 与 review

### commit 序列

五个，顺序是有讲究的：

```
1. test: measure what each assembly site builds today

   Interlude B is about two places that build an Agent and disagree. Before
   changing either, write down what they build.

   In probe_boundaries.py rather than in tests/, and that is not laziness:
   the interesting comparison is between the code as it is and the code as
   it will be, and a pytest test can only be written against one of those.
   `drift` reads both Agent(...) call sites with ast and diffs their kwargs;
   `drift-live` runs the same scripted model through a parent and a child
   and reports the request size, the transcript and the peak concurrency.

   Also here: the two cycle reproductions (init-cycle, tool-cycle) and the
   four repairs, each run rather than reasoned about. All of them edit a
   *copy* of src/ in a temp directory -- chapter 6 paid for the version that
   edited the working tree and was killed before putting it back.

2. refactor: one ToolSet value for handlers, schemas and footprints

   No behaviour change. ToolSpec (ch04) already bound a handler to its
   schema; the scheduler's footprint_of (ch08) was never folded in, so it
   was assembled separately in __main__ and not passed at all in run_task.

   Footprint/STATEFUL/FootprintFn move down to agent_types.py, which is the
   third application of chapter 1's rule and therefore the point at which it
   is written down as a rule. scheduler.py re-exports them and keeps every
   function.

   route_footprint is deleted: ToolSet.plus routes by ownership, which needs
   no is_remote() name test and no new branch per tool source.

   Rejected: a ToolProvider Protocol with handlers()/schemas()/footprint().
   The three producers do not need to be objects, they need to return the
   same shape. An interface has implementations to count; a value does not.

3. refactor: build every Agent through Wiring.agent

   Still no behaviour change at the top level -- __main__ passes the same
   seven things, now via one object. What it buys is that there is exactly
   one Agent(...) in src/, so the next optional argument cannot be added to
   one call site and forgotten at the other.

4. fix: a sub-agent inherits its parent's wiring and footprints

   The behaviour change, on its own, after the two refactors that make it
   one line. subagent.py stops importing tools.py and takes build_tools from
   its SubAgentContext -- the inversion, and the reason tools.py could never
   hold the spawn_agent handler.

   Measured, same scripted model and files:

       child   181,040 chars, 0 compactions, no transcript, serial tools
       parent   76,896 chars, 1 compaction,  transcript,    concurrent

   Chapter 8's conservative default (unclassified -> STATEFUL) is why the
   third one was slow rather than a data race. That default earned itself
   here for the first time.

   The measurements from commit 1 become tests here, because only now is
   there an API in which both arms can be expressed: Wiring() is exactly
   what a child used to get, and the parent's wiring is what it gets now.

   spawn_agent deliberately NOT moved back into tools.py. The cycle is gone
   and the move is now legal, which is not a reason to make it.

5. ci: check the layering, and prove each rule can fail

   scripts/check_layers.py, five rules, each from a fault that happened.
   In the lint step: chapter -1's six-step cap forced the question and a
   three-second static check is what a lint step is. Chapter 9 answered the
   same question with a second workflow because a mutation run is not a lint.

   The first cycle finder reported "none" on a package that could not be
   imported, because it dropped `from minicodex import <name>` edges. Every
   rule now has a mutation test proving it goes red.
```

第 1 个 commit 值得单说一句：**它交付的是一个探针，不是测试。**

插曲 A 的做法是先写 characterization test 再动手。这一章做不到，原因很具体：
要钉住的行为是"子 Agent 缺了什么"，而在旧代码里"子 Agent 带着 wiring 跑"这个
arm **根本没有 API 可以表达**——`SubAgentContext` 上没有那个字段。一个只能表达
一半对比的测试，写出来是绿的，而且绿得毫无意义。

所以先量、后改、再把量到的东西变成测试。判据是：

> **characterization test 钉的是"不许变的行为"。**
> 这一章要证明的是"必须变的行为"，那不是 characterization test，那是测量。
> 分不清这两者，就会写出一个把 bug 钉住的测试。

（这个区分插曲 A 里没出现，因为那一章确实是纯重构。这里两样都有。）

2、3 是纯重构，4 是行为变更。它们分开的实际好处，在 §14 那句"一个既有测试都没有
因为行为变了而改断言"里——如果 2、3、4 混在一个 commit 里，那句话就没法说了，
因为你分不清一个测试改了是因为重构还是因为行为。

### PR 描述

```markdown
## What

Breaks the tools ↔ subagent import cycle by inversion, and encodes five
architecture rules as a CI check.

## Why

Chapter 10 left a note: putting the `spawn_agent` handler where handlers live
is a circular import, so it went into `__main__` instead. Going back for it
turned up something worse than the cycle.

The workaround created a second place that builds an `Agent`. `__main__`
passed seven of ten optional arguments; `subagent.run_task` passed three.
Measured on one scripted model and three identical files:

| | final request | compactions | transcript | tool scheduling |
|---|---|---|---|---|
| child  | 181,040 chars | 0 | none | serial |
| parent |  76,896 chars | 1 | written | concurrent |

Nothing raised. `subagent.py` reads correctly on its own — it is only wrong
next to a call site three modules away, and nothing put them next to each
other.

Two cycles were reproduced, not just described:

- the one chapter 10 predicted (`tools` ⇄ `subagent`), and
- one nobody predicted: `from minicodex.agent import Agent` in `__init__.py`
  — the convenience import every package grows — puts three modules that
  never mention each other in a ring and stops the whole package importing.

## How

- `ToolSet` (agent_types.py): handlers, schemas and footprints as one value
  that refuses construction when they disagree. `Footprint` moves down with
  it — chapter 1's rule for the third time.
- `Wiring` (agent.py): the four things a child was missing, plus `dialect`.
  `Wiring.agent()` is the only `Agent(...)` in `src/`.
- `composition.py`: the composition root, out of `__main__` so a test can
  reach it. Both assembly sites call it.
- `subagent.py` takes `build_tools` from its caller and imports nothing from
  `tools.py`.
- `scripts/check_layers.py` in the lint step.

Deleted: `route_footprint` (one caller, needed a branch per tool source).
Designed and dropped: a `ToolProvider` Protocol. Not done: moving
`spawn_agent` back into `tools.py`, and a declared layer number per module.

## Testing

1414 passed (was 1392). No existing test changed an assertion because
behaviour changed — only construction (`context_for` uses `sub_context`, two
patched tool tables gained schemas, one ch09 test uses `with_remote_tools`).

Each of the checker's rules has a mutation test that must turn it red. The
first cycle finder reported "none" on the broken `__init__` tree; that is
the reason those tests exist.

## Notes for the reviewer

Commits 2 and 3 are pure refactors; commit 4 is the behaviour change and is
one line because of them. Reviewing in order is much cheaper than reviewing
the diff.
```

### Code review

**R1 — `ToolSet.schemas` 是可变 `list`，但类是 `frozen=True`，这是不是自欺欺人？**

是，而且必须是。第 9 章的 registry 把 `visible` 这个**对象**交给了 model client，
`tool_search` 靠往里 `append` 来揭示延迟工具。复制它 = `tool_search` 静默失效。

我在 docstring 里写明了，并且把这条不对称集中在一个函数（`with_remote_tools`）里
而不是散开。`frozen=True` 挡的是"字段被重新绑定"，那个是真的挡住了。

> 回应的原则：**不为一个真实约束辩解成"设计如此"，也不为了形式上的纯粹去破坏它。
> 写清楚它是什么、只有一处、为什么。**

**R2 — `SubAgentContext.build_tools` 的默认值是 `lambda _shell: ToolSet({}, [])`，
一个没有工具的子 Agent。这是不是一个会静默出错的默认值？**

好问题，而且这正是这一章反对的那种东西。

我想过两个替代：必填字段（没有默认值），或者默认抛异常。必填会让第 10 章那 32 个
测试全部要改；抛异常在类型上更诚实但没有实际区别。

最后留默认值的理由是：`sub_context()` 是**唯一**的构造入口，它总是填这个字段，
而"直接 `SubAgentContext(...)`"这条路只有测试会走。但这是一个约定，不是强制。

**这条我接受一半：** 加了一条测试断言 `sub_context()` 填了它，但没有把字段改成
必填。如果以后出现第二个真实的构造点，这个决定要重新做。

**R3 — `Wiring` 里 `dialect` 和另外四个不是一类东西。**

对。另外四个是"子 Agent 测出来缺的"，`dialect` 是"父子之间绝对不能不一样的"。

放在一起是因为**结论相同**：这个值必须原样往下传。但这个区别值得写在类里，我加了
一行注释。如果以后出现第三种理由，这个类就该拆了——那时它就变成配置袋了。

**R4 — 为什么不干脆让 `Agent.__init__` 只接受 `ToolSet` 和 `Wiring`？**

那是最干净的形状，我也想要。代价是 1392 个既有测试里凡是
`Agent(model, {"read_file": fn}, recorder=...)` 的都要改。

这就是插曲 A 那条纪律的直接后果：一个纯重构 PR 不许夹带 1000 处调用点的改动，因为
那样 diff 里就没人能看见真正的变化了。`Wiring.agent()` 是同样效果的一半成本版本
——`Agent.__init__` 保持不变，但整个 `src/` 里只有一处在调它，而这一点是**检查器
强制的**，不是靠约定。

`Agent.__init__` 收窄可以作为一个后续 PR，独立评估。

**R5 — `check_layers.py` 开 25 个子进程要 3 秒，每次 lint 都跑，值吗？**

值，而且这 3 秒买的正是别的检查买不到的东西：`import minicodex` 在一个所有模块都
装不起来的包上退出 0（§3.1 实测）。静态 AST 检查也漏——它只能看见 import 语句，
看不见"这个模块在 import 时执行了会失败的代码"。

如果它以后涨到 30 秒，那就该挪进 `postmerge.yml`，理由和第 9 章的变异检查一样。
现在不是。

### Merge

Squash。五个 commit 在 review 里各有各的用，合进 main 之后"插曲 B：打断循环依赖"
是一个原子的东西——如果它要被 revert，五个一起。

### CI

这一章给 CI 加了一行（在 lint 步骤里），理由和放置决策在 §11.6。

blocking 步骤数：**仍然是 6**。

---

## §16 回头看：这一章撞到了什么

| 编号 | 清单上写的 | 实际发生的 |
|---|---|---|
| FB-01 | 循环 import，运行时才炸 | **两个都复现了。** 清单预告的那个（`tools` ⇄ `subagent`）如期而至；没预告的那个是一行 `__init__.py` 便利 import，牵连三个互不认识的模块 |
| FB-02 | 边界修好了，三个月后又被人破坏 | **没以那个形式发生**——第 10 章的测试守住了那条边。发生的是**检查器本身在测量错的东西**：环检查器在一个装不起来的包上报告 `none` |
| FB-03 | 为打断环引入的接口只有一个实现 | **靠测量避开了。** `ToolProvider` Protocol 设计出来然后砍掉；最后删了两个抽象（`route_footprint`、借用的 `ToolSpec`），一个都没加 |

清单外的五条：

1. **绕法比环贵。** 三个静默故障（不压缩、不录制、串行），一次测量。
2. **第三个通道从来没有家。** `ToolSpec` 绑了 handler 和 schema，第 8 章的
   `footprint_of` 没并进去，于是它在一处被单独包装、在另一处根本没传。
3. **不变量落地当天抓到一个测试。** 第 10 章超时测试的假 `sleep` handler 没有
   schema。
4. **`with_remote_tools` 的顺序规则是撞出来的**，而且 `ToolSet` 的报错指向症状
   （四个 handler）而不是病因（两行代码的顺序）。
5. **把类型下沉是第三次了。** 三次是"这是规则"和"这是修法"的分界线。

一个关于比例的观察：这一章三条清单故障**全部复现**，是第 2 章以来第一次。原因不
难猜——**循环 import 是本书少数几个纯粹关于代码结构、和模型无关的故障**。它不依赖
供应商、不依赖 prompt、不依赖运气，所以它能被预测。第 3、9、10 章那些"没复现"的
条目，几乎全是关于模型行为的猜测。

> 你能提前写进清单里的故障，通常是确定性系统里的故障。
> 非确定性系统里的故障清单，写出来主要是为了被推翻。

---

## 如果你只记住三件事

**一、循环 import 里两个模块交换的是名词就下沉，是动词就倒置。**
第 1 章和插曲 A 用了三次"把共享类型搬下去"，那是名词。这一章 `tools.py` 要的是
`run_task`——一个构造 Agent、跑它、解释结果的行为。没有类型可搬。把 import 挪进
函数体不算修好：包能装了，环还在，失败时刻从启动挪到了第一次调用。

**二、绕开一个环的代价，是把一条不变量从编译器手里交到人的记忆里。**
第 10 章把工具上移，完全合理，代码也没错。结果是第二个装配点，给 `Agent` 传三个
参数而不是七个，于是子 Agent 不压缩（181,040 vs 76,896 字符）、不被录制、工具全
串行。这三条都不报错，而且 `subagent.py` 单独读起来是对的——它错只错在和三个模块
以外的另一段代码放在一起看的时候，而没有任何东西把它们放在一起。

**三、一条没被看见失败过的检查，是一条没人知道有没有用的检查。**
这一章的环检查器第一版在一个整个包都装不起来的树上报告 `none`；
`python -c "import minicodex"` 这个最自然的冒烟检查，在同样的树上退出 0。检查器
和被检查的代码一样需要变异测试，而且更需要——它默认输出的就是"ok"。同理，一条禁
止规则必须带着它的故障编号，否则三年后没人敢删它，只能继续猜着遵守。

---

## 动手练习

1. **亲手撞一次 §2 的环。** 在 `src/minicodex/__init__.py` 里加
   `from minicodex.agent import Agent`，然后跑 `uv run pytest`。看清楚是几个测试
   红了、报错指着谁。再跑 `uv run python scripts/check_layers.py`，比较两种消息哪
   一种能让你在十秒内找到那一行。

2. **把检查器改坏，看它还说不说 ok。** 把 `intra_package_imports` 里
   `found.add(PACKAGE if node.module == PACKAGE else node.module)` 改回
   `found.add(node.module)`，再做练习 1。这是 §2.1 那个 bug 的复现。然后跑
   `uv run pytest tests/test_faults_chB.py`，看哪个测试拦住了你。

3. **把 `ast.walk` 改成 `tree.body`。** 然后用 §4(c) 的延迟 import 造一个环。
   检查器会说 ok。这解释了为什么那一行是 `walk`。

4. **删掉 `ToolSet.__post_init__`，跑全量测试。** 数一下有几个红的。再想一想：
   如果这条不变量从来没写过，`composition.py` 里那个"忘了 `local=`"的错误会以什
   么形式出现？（提示：先看 §9.1，再实际把 `local=` 删掉试试。）

5. **把 `Wiring` 拆开**——给 `SubAgentContext` 加四个独立字段
   （`recorder`、`context_window`、`summariser`、`max_concurrent_tools`），
   删掉 `Wiring`。让测试全绿。然后往 `Agent.__init__` 加第十一个可选参数，看看要
   改几个地方、有没有东西会提醒你漏了一处。

6. **给 `FORBIDDEN` 加一条你自己的边**，比如 `("composition", "__main__", ...)`。
   写一个变异测试证明它会红。然后问自己：这条边对应过一次真实事故吗？如果没有，
   它属于 §11.7 那一栏吗？
---

# 附录 · 代码到底怎么敲出来的（新手向，逐段精讲）

跟第 8 章附录同一个规矩：正文讲"为什么"，这里补"怎么写"。默认你已经读过
前面几章的附录（`ast`、`dataclasses`、`subprocess` 基础），这里只讲这一章
正文 §13 明确说"没进正文"的部分：`check_layers.py` 的 Tarjan 实现。
代码摘自 `steps/stepB_boundaries/scripts/check_layers.py`，逐段核对过。

先把范围说死：

1. 本附录只解释插曲 B 在 `steps/stepB_boundaries/` 里新增的代码。正文
   §6/§7/§8/§9 已经给了 `ToolSet`/`Wiring`/`composition.py`/`subagent.py`
   的核心，§11 给了五条检查规则的语义——这些**不再重复**。这里补 §13
   明说"没进正文"的：`check_layers.py` 的实现（正文只讲了五条规则长什么样，
   没给代码）。
2. 完整函数代码块里不使用代表遗漏实现的省略号，也不把关键分支写成
   "此处略"。示例字符串或命令文本里出现的三个点，如果本来就是源码的
   一部分，会原样保留。
3. 代码块按当前源码核对。

## U1 · `intra_package_imports`：用 `ast.walk` 找所有内部 import

```python
def intra_package_imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(a.name for a in node.names if a.name.split(".")[0] == PACKAGE)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            if node.module.split(".")[0] != PACKAGE:
                continue
            found.add(PACKAGE if node.module == PACKAGE else node.module)
    return found
```

三个新手容易漏的点：

1. **用 `ast.walk` 而不是只扫顶层。** 写在**函数内部**的 import 也是依赖
   边——而且延迟 import 正是"让环不在 import 时报错、改在第一次调用时报错"
   的标准手法。只读文件顶部的检查器，奖励的正是掩盖问题的修复（docstring
   明说）。
2. **`ast.Import` 和 `ast.ImportFrom` 分两类处理。** `import minicodex.tools`
   是 `ast.Import`（`a.name` 是完整路径）；`from minicodex.tools import x`
   是 `ast.ImportFrom`（`node.module` 是 `"minicodex.tools"`）。`node.level
   == 0` 排除相对导入（`from . import x` 的 `level` 是 1）。
3. **`from minicodex import compaction_prompt` 是到"包"的边**——`node.module
   == PACKAGE` 时，这条边指向 `minicodex` 包本身（它的 `__init__` 必须先
   跑完）。第一版漏了这个分支，结果报告"没有环"的包其实**根本 import
   不了**（注释里记了这个事故）。

## U2 · `build_graph` 与 `find_cycles`：Tarjan 的迭代版

```python
def build_graph(pkg: Path) -> dict[str, set[str]]:
    graph: dict[str, set[str]] = {}
    for path in sorted(pkg.glob("*.py")):
        name = PACKAGE if path.stem == "__init__" else f"{PACKAGE}.{path.stem}"
        graph[name] = intra_package_imports(path)
    known = set(graph)
    return {k: {v for v in vs if v in known and v != k} for k, vs in graph.items()}


def find_cycles(graph: dict[str, set[str]]) -> list[list[str]]:
    colour: dict[str, int] = dict.fromkeys(graph, 0)
    order: list[str] = []

    def visit(node: str) -> None:
        colour[node] = 1
        for child in sorted(graph[node]):
            if colour[child] == 0:
                visit(child)
        colour[node] = 2
        order.append(node)

    for node in sorted(graph):
        if colour[node] == 0:
            visit(node)

    reverse: dict[str, set[str]] = {n: set() for n in graph}
    for node, deps in graph.items():
        for dep in deps:
            reverse[dep].add(node)

    seen: set[str] = set()
    found: list[list[str]] = []
    for node in reversed(order):
        if node in seen:
            continue
        component = []
        stack = [node]
        seen.add(node)
        while stack:
            current = stack.pop()
            component.append(current)
            for parent in sorted(reverse[current]):
                if parent not in seen:
                    seen.add(parent)
                    stack.append(parent)
        if len(component) > 1:
            found.append(sorted(component))
    return found
```

**这是 Kosaraju 算法**（两次 DFS：第一次按完成序，第二次在反向图上找强连通
分量），不是 Tarjan（一次 DFS + lowlink）。§13 说"Tarjan 实现"是笼统说法；
代码实际是 Kosaraju，它更简单、更容易写成迭代版。逐段拆：

1. **`build_graph` 的 `known` 过滤**：`{v for v in vs if v in known and
   v != k}` 去掉指向包外或指向自己的边（`v != k`——自环不算环）。
2. **第一遍 DFS（`visit`）按完成序收集 `order`**。颜色 0=未访问、1=在栈上、
   2=已完成。递归的 `visit` 是这版唯一的递归（正文 §13 说"只有写成迭代版
   这个选择值得一提"指的是下面的第二遍；第一遍在模块数量小的时候递归
   可接受）。
3. **`reverse` 是反向图**（`reverse[dep].add(node)`——每条边 `node → dep`
   反过来 `dep ← node`）。
4. **第二遍在反向图上按 `reversed(order)` 收集分量**：从每个未 seen 的节点
   出发，沿反向边走，走到的都是同一个强连通分量。**迭代栈替代递归**——
   一个自己会栈溢出的环检查器，不是"图已经坏了"的时候想要的工具（§13
   原话）。
5. **`len(component) > 1` 才报**——单节点分量（无自环）不是环。

## U3 · 五条检查与 `main`

正文 §11 讲了五条规则的语义，`main` 的骨架：

```python
CHECKS = (
    ("no import cycles", check_cycles),
    ("__init__ is a leaf", check_init_is_a_leaf),
    ("forbidden edges", check_forbidden),
    ("one Agent construction site", check_one_agent_construction),
    ("every module imports alone", check_every_module_imports),
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--src",
        type=Path,
        default=Path(__file__).resolve().parent.parent / "src" / PACKAGE,
        help="the package directory to check",
    )
    parser.add_argument("--quiet", action="store_true", help="print nothing when everything passes")
    args = parser.parse_args(argv)

    failed = False
    for label, check in CHECKS:
        problems = check(args.src)
        if problems:
            failed = True
            for problem in problems:
                print(f"{label}: {problem}", file=sys.stderr)
        elif not args.quiet:
            print(f"ok  {label}")
    return 1 if failed else 0
```

- **`CHECKS` 是 `(标签, 函数)` 元组表**——每条检查返回 `list[str]` 的问题
  列表，空 = 通过。加新规则 = 加一个函数 + 加一行表。
- **`--src` 默认指向 `src/minicodex`**（`Path(__file__).resolve().parent
  .parent / "src" / PACKAGE`）——脚本从任何 cwd 跑都能找到包。
- **退出码 1 = 有违规**（`return 1 if failed else 0`），CI 的 lint 步骤靠
  它红。

`check_every_module_imports` 值得一提（正文 §11 讲了"`import minicodex`
退出 0 但包根本不能用"）：

```python
def check_every_module_imports(pkg: Path) -> list[str]:
    failures: dict[str, list[str]] = {}
    for path in sorted(pkg.glob("*.py")):
        if path.stem == "__init__":
            continue
        module = f"{PACKAGE}.{path.stem}"
        result = subprocess.run(
            [sys.executable, "-c", f"import {module}"],
            capture_output=True,
            text=True,
            env={**_env(), "PYTHONPATH": str(pkg.parent)},
        )
        if result.returncode != 0:
            last = (result.stderr.strip().splitlines() or ["(no output)"])[-1]
            failures.setdefault(last, []).append(module)
    problems = []
    for message, modules in failures.items():
        if len(modules) > 3:
            problems.append(f"{len(modules)} modules do not import on their own: {message}")
        else:
            problems.extend(f"{m} does not import on its own: {message}" for m in modules)
    return problems
```

- **每个模块开一个子进程 import**——一旦模块进了 `sys.modules`，环就对
  之后的一切隐藏了（这也是测试套件在"已经 import 过一次"的 shell 里保持
  绿色的原因）。子进程隔离保证每个模块**从零开始** import。
- **按错误消息分组**（`failures.setdefault(last, []).append(module)`）——
  经过 `__init__` 的环会让**每个**模块以同一行报错，第一版打印二十五段
  相同的段落，没人会读完。
- **`_env()` 只保留必要环境变量**（PATH/SYSTEMROOT/TEMP 等），隔离测试
  不受宿主环境干扰。

## U4 · 新手常见报错/坑对照表

| 症状 | 原因 | 修法 |
|---|---|---|
| 检查器报"没有环"但包 import 不了 | `from minicodex import x` 的边被漏 | `node.module == PACKAGE` 时指向包本身 |
| 延迟 import 的环没被发现 | 只扫文件顶层 | `ast.walk` 找所有深度的 import |
| 环检查器自己栈溢出 | 递归 DFS | 第二遍用迭代栈（Kosaraju 反向图） |
| `import minicodex` 退出 0 但包坏了 | `__init__` 是叶子，顶层 import 不碰坏的部分 | `check_every_module_imports` 逐模块子进程 import |
| 二十五段相同的报错 | 按模块报不按消息分组 | `failures` 按 stderr 最后一行分组 |
| 相对导入被当成包内边 | `from . import x` | `node.level == 0` 排除 |
| 新规则没跑 | 只写函数没注册 | `CHECKS` 表加一行 |
