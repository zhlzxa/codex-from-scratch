# 插曲 B · 第二次重构

> **代码**：`steps/stepB_boundaries/`
> **分支**：`refactor/boundaries`
> **产出**：没有新功能。包里只剩**一个**造 `Agent` 的地方；子 Agent 和父 Agent 用同一套东西装起来；模块之间谁能导入谁，由一个脚本在 CI 里检查
> **前置**：做完第 10 章。这一章完全不联网，不需要任何 key。
> **这一章可以分两次读**：§1–§7 是"量"（现在的包长什么样、第 10 章那两个临时办法花了多少钱），§8 之后是"改"和"守住"。

---

## §0 开工前的准备

### 0.1 本章会遇到的新概念

- **导入图（import graph）**：把每个模块看成一个点，"A 导入了 B"画成一条从 A 指向 B 的箭头，得到的图。
- **环（cycle）**：顺着箭头走，能回到出发的那个模块。A 导入 B、B 又导入 A 是最短的环。
- **叶子（leaf）**：不导入包里任何其他模块的模块。
- **装配（composition）**：把各处造出来的零件（工具、模型客户端、会话文件……）组合成一个能跑的 Agent 的那段代码。专门做这件事的模块叫**装配根**。
- **依赖倒置（inversion）**：A 本来要导入 B 才能拿到某样东西；改成"谁创建 A，谁把那样东西递给 A"，A 就不用导入 B 了。
- **值类型**和**接口**：值类型是"一包数据"（比如一个 dataclass）；接口是"一组约定好的方法，由不同的类各自实现"。
- **AST（抽象语法树）**：Python 把源码解析成的一棵树。读它就能知道一个文件里写了哪些 import，而**不必运行**这个文件。

### 0.2 本章会遇到的新 Python 写法

| 写法 | 意思 |
|---|---|
| `ast.parse(源码)` / `ast.walk(tree)` | 把源码解析成语法树 / 遍历树上的每一个节点 |
| `isinstance(node, ast.ImportFrom)` | 这个节点是不是一句 `from x import y` |
| `subprocess.run([sys.executable, "-c", "import x"])` | 另开一个全新的 Python 进程，执行一句代码 |
| `shutil.copytree(a, b)` | 把整个目录 `a` 复制成 `b` |
| `def __post_init__(self):` | dataclass 的对象一造出来就自动执行的方法，常用来检查字段之间是否自洽 |
| `frozenset({...})` | 不可变的集合 |
| `def f(*, a, b, **rest):` | `*` 之后的参数必须写名字；`**rest` 把其余带名字的参数收成一个字典 |
| `importlib.util.spec_from_file_location(...)` | 按文件路径（而不是按模块名）导入一个 `.py` 文件 |
| `getattr(模块, "函数名")` | 按名字从模块里取出一个函数 |
| `monkeypatch.setattr(类, "方法名", 新函数)` | 测试期间临时换掉一个类的方法，测试结束自动换回 |

### 0.3 开分支

```bash
git switch main
git pull
git switch -c refactor/boundaries
```

---

## §1 这一章要做的事

第 10 章留下两笔账，都写在那一章的结尾：

1. `spawn_agent` 这个工具，本来该和其他工具的处理函数一起放在 `tools.py` 里。放进去，整个包就导入不了了（循环导入）。那一章的办法是**把它往上搬**。
2. 子 Agent 是在 `subagent.py` 里用另一行 `Agent(...)` 造出来的，传的参数比 `__main__.py` 里造父 Agent 的那一行少得多。
   那一章量到过一次后果：一个子 Agent 把自己的对话读到超过了模型的窗口。

这一章不加任何功能，只收拾这两笔账。和插曲 A 一样，规矩是：**先量，再改；改的每一步，测试都得是绿的。**

---

## §2 定需求，猜故障

需求：

- 包里造 `Agent` 的地方只有一处；
- 子 Agent 拿到的东西和父 Agent 一样（压缩、调度、录制）；
- `spawn_agent` 不再因为"放在哪"而导致循环导入；
- 以后有人再把模块之间的方向弄反，机器会说出来。

动工前的猜测清单，三条：

| 编号 | 猜测 | 怎么判断 |
|---|---|---|
| FB-01 | 循环导入，而且是运行到一半才发现的 | 在一份拷贝里把它造出来 |
| FB-02 | 边界修好了，过三个月又被人弄坏 | 把边界写成检查，再看检查自己靠不靠得住 |
| FB-03 | 为了打破环而引入的"接口"，最后只有一个实现 | 数一数 |

先说结果：**三条都成立**——上一次三条全中还是第 2 章。而且各有一个没猜到的转折：

- FB-01 有**两个**环，第二个是由一行所有人都会写的代码造成的；
- FB-02 第一次出问题，是出在**为了防它而写的那个检查器**身上；
- 这一章自己的修复，在真的程序里**没有生效**——是给它写测试的时候才发现的。

---

## §3 先量：这个包现在长什么样

探针 `probe_boundaries.py`（整份在 §16）的第一段，读每个文件的 import，画出导入图。先对着**第 10 章结束时**的代码跑：

```
25 modules, 65 edges
cycles: none

level  module              out   in
    0  __init__              0    3
    0  agent_types           0    7
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
    1  shell                 2    2
    2  compaction            4    2
    2  patch                 2    1
    2  rollout               2    3
    2  rules                 1    2
    3  agent                 8    2
    3  approval              5    4
    4  registry              8    1
    4  tools                 7    2
    5  subagent              8    1
    6  __main__             14    0
```

怎么读这张表：

- **`out`**：这个模块导入了包里几个别的模块。**`in`**：有几个模块导入了它。
- **`level`**：从它出发，顺着 import 最多能往下走几步。第 0 层是叶子，谁都不导入。
- 25 个模块，65 条箭头，**没有环**。

看起来挺健康。有两处值得多看一眼：

- `agent_types` 被 7 个模块导入，自己什么都不导入。这是第 1 章和插曲 A 两次"把共用的类型往下挪"的结果，也正是它该有的样子。
- `subagent` 在第 5 层，导入了 8 个模块，其中有 `tools`。**这一条箭头是后面所有事情的起点。**

"没有环"是因为第 10 章躲开了。下面把它躲开的东西造出来。

> **探针里所有要改源码的实验，都是把 `src/` 复制到一个临时目录、在那份拷贝里改、另开一个 Python 进程去导入它。**
> 第 6 章为"直接改工作目录、中途被打断"付过学费，第 9 章又付了一次。

---

## §4 FB-01 的第一个环：一行谁都会写的代码

几乎每个 Python 包长大之后，都会有人在 `__init__.py` 里加这么一行，好让使用的人少打几个字（`from minicodex import Agent`）：

```python
from minicodex.agent import Agent
```

在拷贝里加上它，然后分别从三个入口导入（不联网；Windows，Linux 上除路径外相同）：

```
$ uv run python probe_boundaries.py init-cycle
--- import minicodex
    exit 1
        from minicodex.agent import Agent
      File "...\src\minicodex\agent.py", line 23, in <module>
        from minicodex.compaction import CompactionResult, Sizer, Summariser, compact
      File "...\src\minicodex\compaction.py", line 41, in <module>
        from minicodex import compaction_prompt
    ImportError: cannot import name 'compaction_prompt' from partially initialized module 'minicodex' (most likely due to a circular import) (...\src\minicodex\__init__.py)

--- import minicodex.compaction
    exit 1
    （同一个 ImportError）

--- import minicodex.tools
    exit 1
    （同一个 ImportError）

and the graph checker on that same tree says: cycles = [['minicodex', 'minicodex.agent', 'minicodex.compaction']]
```

**整个包都导入不了了**，连和这一行毫无关系的 `minicodex.tools` 也不行。

发生了什么？顺着那段报错从上往下读：

1. Python 开始执行 `__init__.py`，碰到新加的那一行，去导入 `agent.py`；
2. `agent.py` 要导入 `compaction.py`；
3. `compaction.py` 里有一行 `from minicodex import compaction_prompt`——它要的那个函数**定义在 `__init__.py` 里**（第 6 章放的）；
4. 而 `__init__.py` 这时候才执行到一半，`compaction_prompt` 还没定义出来。报错。

**三个互相都没提到对方的模块，组成了一个环。** `agent.py` 里没有 `__init__`，`compaction.py` 里没有 `agent`。加的那一行，在代码审查里没有人会多看一眼。

### 4.1 检查器自己的第一个 bug

上面最后一行，图检查器说找到了环。**它的第一版说的是"没有环"。**

第一版读 import 的函数是这么判断的：`from minicodex.agent import Agent` 是一条指向 `minicodex.agent` 的箭头；
而 `from minicodex import compaction_prompt`——`compaction_prompt` 不是一个模块，是一个函数，于是这一句被跳过了。

跳过的恰恰是组成环的那条箭头：这一句的意思是"**我依赖 `minicodex` 这个包本身**，它的 `__init__.py` 得先执行完"。

于是出现了这样的场面：包一个模块都导入不了，检查器报告"没有环"。

> **一个从来没有被看到过失败的检查，是一个没有人知道管不管用的检查。**
> 这句话在第 5、6、9、10 章说的都是测试；这里它第一次说的是"用来检查代码的代码"。FB-02 猜的是"三个月后有人把边界弄坏"——实际上第一个把它弄坏的，是检查器的作者，在写它的当天。

---

## §5 FB-01 的第二个环：第 10 章躲开的那个

现在造第 10 章的那个环。在拷贝里加回两条箭头：`tools.py` 导入 `subagent.run_task`（这样 `spawn_agent` 的处理函数就能和别的处理函数放在一起），
`subagent.py` 导入 `tools.bind_all`（造子 Agent 的工具表要用）。

```
$ uv run python probe_boundaries.py tool-cycle
--- import minicodex.tools
    exit 1
        import minicodex.tools
      File "...\src\minicodex\tools.py", line 31, in <module>
        from minicodex.subagent import run_task
      File "...\src\minicodex\subagent.py", line 67, in <module>
        from minicodex.tools import bind_all
    ImportError: cannot import name 'bind_all' from partially initialized module 'minicodex.tools' (most likely due to a circular import) (...\src\minicodex\tools.py)

--- import minicodex.subagent
    exit 1
        import minicodex.subagent
      File "...\src\minicodex\subagent.py", line 67, in <module>
        from minicodex.tools import bind_all
      File "...\src\minicodex\tools.py", line 31, in <module>
        from minicodex.subagent import run_task
    ImportError: cannot import name 'run_task' from partially initialized module 'minicodex.subagent' (most likely due to a circular import) (...\src\minicodex\subagent.py)

--- import minicodex
    exit 0
```

前两个结果是第 10 章说过的：导入不了，而且**报错怪的是谁，取决于先从哪个模块进来**。

### 5.1 第三个结果才是这一节的重点

**`import minicodex` 成功了，退出码是 0。**

两个模块互相导入不了，而"导入这个包"照常成功——因为 `__init__.py` 是一个叶子，它不导入 `tools` 也不导入 `subagent`，导入包的时候根本碰不到坏掉的那一部分。

`python -c "import minicodex"` 是 CI 里最常见的一种"冒烟测试"：包能导入就算过。**在这里，它会在一个不能用的包上通过。**

所以后面的检查器（§14）不导入包，而是**每个模块各开一个新进程，单独导入一遍**。

---

## §6 四种修法，一个一个试

```
$ uv run python probe_boundaries.py repairs
```

**(a) 把共用的类型往下挪。** 第 1 章对 `ToolCall`、插曲 A 对 `ToolFn` 用的都是这个办法，两次都管用。
这次不行：`tools.py` 想从 `subagent.py` 拿的是 `run_task`——一个会造 Agent、运行它、解读结果的函数。它不是一个类型，是一整段行为。把它往下挪，等于把整个模块往下挪。

**(b) 把工具往上搬。** 第 10 章的办法。管用。代价在 §7。

**(c) 把那句 import 挪进函数里面。** 这是打破循环导入最常见的偏方：import 写在函数体里，就要等到函数**被调用**时才执行。

```
(c) defer the import into a function body:
--- import minicodex.tools
    exit 0
    imported fine

    and the AST checker still sees the edge: cycles = [['minicodex.subagent', 'minicodex.tools']]
    The cycle did not go away. It moved from startup to the first call.
```

包能导入了。**环还在**——检查器读的是整个文件里的每一句 import（`ast.walk` 会走进函数体），所以它照样看得见。
这个修法没有消除问题，只是把它从"启动时"挪到了"第一次调用时"：更晚，当着用户的面，出现在一段讲别的事情的报错里。

**(d) 倒置。** `subagent.py` 为什么要导入 `tools.py`？为了造子 Agent 的工具表。那就不让它自己造：**谁创建子 Agent 的上下文，谁把"怎么造工具表"递给它。**

```
(d) invert: subagent takes `build_tools` from its caller. ...
    cycles with only the tools -> subagent arrow added: none
```

`subagent → tools` 这条箭头没有了。这时即使 `tools.py` 真的去导入 `subagent`，也没有环。这是最后采用的办法（§10）。

---

## §7 办法 (b) 的价钱：两个造 Agent 的地方

第 10 章把工具往上搬，结果是包里有了**两处** `Agent(...)`：

```
__main__ Agent( calls: 1   subagent: 1
```

- `__main__.py` 造父 Agent：模型、工具表，再加 7 个带名字的参数（录制器、系统消息、窗口大小、会话文件、从哪恢复、总结函数、脚印函数）。
- `subagent.py` 的 `run_task` 造子 Agent：模型、工具表，再加 3 个（轮数上限、系统消息、会话文件）。

少传的那几个会怎样？第 10 章碰到过一次（一个子 Agent 读超了窗口）。这里把它完整地量出来。同一个假模型、同一份脚本（读同一个 6 万字符的文件三次；另一份脚本在同一轮里读两个小文件）、同样的工具，
分别按"父 Agent 的造法"和"子 Agent 的造法"跑，**用的是第 10 章结束时的代码**：

```
arm         final request  compactions  recorded  peak    wall
parent             76,896            1      True     2   0.21s
child             181,008            0     False     1   0.41s
```

（Windows，2026-10-01。`final request` 是最后一次发给模型的请求有多少个字符；`peak` 是同一时刻最多有几个工具调用在跑。）

| | 父 Agent | 子 Agent |
|---|---|---|
| 最后一个请求 | 76,896 字符 | **181,008 字符** |
| 压缩过几次 | 1 | **0** |
| 录制文件里有没有这段对话 | 有 | **没有** |
| 同一轮的两个读，同时跑了几个 | 2 | **1**（多花一倍时间） |

**子 Agent 从不压缩，从不被录制，工具调用一个一个排队。** 第 6 章、第 -1 章、第 8 章各花一整章做的东西，对程序里每一个子 Agent 都是关着的。

而且：

- **没有任何报错。**
- **单独读 `subagent.py`，它每一行都是对的。** 它只是和三个模块之外的另一行代码不一致，而没有任何东西会把这两行放在一起看。
- 最后一项（排队）之所以只是"慢"而不是"丢数据"，纯粹是因为第 8 章的默认值——认不出的调用当作和一切冲突。**一个保守的默认值，把一次没人发现的遗漏变成了变慢，而不是数据竞争。**

> **循环导入是这一章唯一会自己喊出来的故障。真正贵的是为了躲开它而做的那件事——而那件事不喊。**

所以这一章要改的不只是"把环打开"。两处造 Agent 的地方得变成一处，而且得让"少传一个"这件事**做不出来**。

---

## §8 `ToolSet`：三张表必须一起走

### 8.1 少掉的第三张表

一个 Agent 的"工具"，其实是三样东西：

1. **处理函数表**：名字 → 真正干活的函数（给循环用）；
2. **说明列表**：给模型看的（名字、说明、参数）；
3. **脚印函数**：一个调用会碰什么（给第 8 章的调度器用）。

第 4 章为前两样"各走各的"付过学费——有处理函数没有说明，模型永远不会调用它；有说明没有处理函数，模型调用后得到第 0 章那句"没有这个工具"。
那一章用 `ToolSpec` 把这两样捆在了一起。

**第三样从来没有被捆进去。** 第 8 章加调度器时，脚印函数是单独传给 `Agent` 的；第 9 章为了把远程工具的脚印接上，写了一个 `route_footprint`；
第 10 章的 `child_tools` 返回的是"处理函数表和说明列表"这一对——**没有第三样**，所以 `run_task` 手里根本没有脚印函数可以传。§7 表里的"排队"就是这么来的。

三样必须一致的东西，就该是**一个**值。

### 8.2 它该放在哪：第三次往下挪

`ToolSet` 要提到 `Footprint`（脚印的类型），而 `Footprint` 定义在 `scheduler.py` 里。`subagent.py` 要用 `ToolSet`，就得为了**一个词**去导入 `scheduler.py`——一个它在行为上完全不依赖的模块。

这是第 1 章（`ToolCall`）和插曲 A（`ToolFn`）处理过的同一种情况，办法也一样：**把类型挪到最底下的 `agent_types.py`。** 第三次了。
三次，是"我们又挪了一个类型"从一次偶然变成一条规矩的地方：

> **两层都要用的类型，住在这两层的下面。一个模块拥有行为，不必同时拥有它的调用者说的词。**

`agent_types.py` 的开头说明里加了一段，记下这条规矩；导入里多一个 `field`。然后是从 `scheduler.py` 原样搬过来的 `Footprint`、`STATEFUL`、`FootprintFn`，和新的 `ToolSet`：

```python
@dataclass(frozen=True)
class Footprint:
    """What one call touches, as far as the scheduler is willing to guess.

    `reads` and `writes` are resource keys -- for the tools this project has,
    a resource key is a resolved absolute path, so two calls naming the same
    file collide and two calls naming different files do not. The scheduler
    never looks inside a key; it only compares them for equality, which is
    what lets `tools.py` own the one question that actually needs tool
    knowledge (what does "the same resource" mean for *this* tool) while
    `scheduler.py` owns none of it.

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


def _stateful(_call: ToolCall) -> Footprint:
    return STATEFUL


@dataclass(frozen=True)
class ToolSet:
    """One agent's tools: the handlers, the schemas, and what each one touches.

    Three things that have to agree, kept in one value so that agreeing is not
    something anybody has to remember.  Chapter 4 already paid for two of them
    drifting -- a handler with no schema is never called, a schema with no
    handler produces chapter 0's "no tool named X" error, which was written for
    names the model *invented* -- and `ToolSpec` fixed that pair for this
    project's own tools.  The third channel, the scheduler's `footprint_of`,
    was never folded in: it travelled separately, was assembled separately in
    `__main__`, and was simply **not passed at all** at the second assembly
    site.  A sub-agent therefore ran every tool call serially, which is safe
    only because chapter 8's default for an unrecognised call is `STATEFUL`.

    `__post_init__` makes disagreement a `ValueError` at build time rather than
    a wrong answer at run time.  It is the one rule in this class and it is the
    reason the class exists; without it this is a tuple with names.

    Not frozen all the way down: `schemas` is deliberately a live `list`,
    because chapter 9's registry hands the model client that exact object and
    reveals a deferred tool by appending to it.  Copying it here would make
    `tool_search` stop working, silently, on the next turn, so the registry's
    set is built last and keeps the list it owns (`composition.with_remote_tools`).
    """

    handlers: dict[str, ToolFn]
    schemas: list[dict[str, Any]]
    footprint_of: FootprintFn = _stateful
    # Names this set is responsible for even though no schema declares them.
    # Empty for every set except the one chapter 9's registry produces, where
    # a deferred tool is callable before its schema has been revealed -- said
    # out loud as an exception rather than by weakening the rule for everyone.
    callable_without_schema: frozenset[str] = field(default_factory=frozenset)

    def __post_init__(self) -> None:
        declared = {s["function"]["name"] for s in self.schemas}
        handled = set(self.handlers) - self.callable_without_schema
        if declared - handled:
            raise ValueError(f"schema with no handler: {sorted(declared - handled)}")
        if handled - declared:
            raise ValueError(f"handler with no schema: {sorted(handled - declared)}")

    def plus(self, other: ToolSet) -> ToolSet:
        """Add another set, routing each call's footprint back to its owner.

        A name declared twice is refused rather than resolved.  Chapter 9 made
        that call for two remote tools whose names sanitise to one thing; the
        same answer applies when the collision is between a local tool and a
        remote one, and for the same reason -- silently keeping one of them is
        how five declared tools become four (F09-01).

        The new schema list is a new object.  Chapter 9's registry hands the
        model client a list it later appends to, so the one set that must keep
        its list identity is the registry's, and it keeps it by being the last
        word rather than by being merged: `McpRegistry(local=...)` takes these
        schemas as input and owns the list that comes out.
        """
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

> - **`Footprint`、`STATEFUL`、`FootprintFn`**：第 8 章的东西，一个字没改，只是换了住址。
> - **`_stateful`**：默认的脚印函数——什么调用都答"和一切冲突"。没人给脚印函数的 `ToolSet`，就退回到最保守的做法。
> - **`ToolSet`** 的四个字段：处理函数表、说明列表、脚印函数、以及"哪些名字允许没有说明"（见下）。
> - **`__post_init__`：这个类存在的理由。** 对象一造出来就检查：说明里有、处理函数表里没有的名字——报错；反过来——也报错。
>   **不一致的工具表造不出来**，而不是造出来之后在运行时给一个错的答案。没有这个方法，`ToolSet` 就只是一个带了名字的元组。
> - **`callable_without_schema`**：唯一的例外，明说出来。第 9 章里被藏在 `tool_search` 后面的工具，有处理函数、暂时没有给模型看的说明——这是故意的。
>   与其把规则放松成"差不多一致就行"，不如让这一种情况**报上名字**；别的所有情况，规则照样严格。
> - **`schemas` 是一个普通的 `list`，没有冻住**：注释解释了原因。第 9 章的注册表把这个列表对象交给模型客户端，靠往里追加来加载工具；这里要是复制一份，`tool_search` 会悄悄失效。
> - **`plus(other)`**：把两个 `ToolSet` 合成一个。
>   - **同一个名字两边都有：拒绝**，不悄悄留下其中一个——第 9 章对两个撞名的远程工具就是这么判的。
>   - 合并后的脚印函数**按"这个名字是谁的"分流**：我的名字问我的脚印函数，对方的问对方的，都不是的答"和一切冲突"。

`scheduler.py` 的开头相应地变成：

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

Interlude B moved `Footprint`, `STATEFUL` and `FootprintFn` down into
`agent_types.py` and left them importable from here.  Not for tidiness: a
sub-agent's tool table has to name the type of its own `footprint_of`, and
`subagent.py` importing this module for a *word* while depending on none of
its behaviour is the backwards arrow chapter 1 and interlude A both spent a
fault on.  Everything you can *do* with a `Footprint` is still here.
"""

from __future__ import annotations

from collections.abc import Sequence

from minicodex.agent_types import STATEFUL, Footprint, FootprintFn, ToolCall

__all__ = ["STATEFUL", "Footprint", "FootprintFn", "batches", "conflicts"]
```

> 类型搬走了，但这三个名字仍然可以从 `scheduler` 导入（`__all__` 里列着），前面几章的代码和测试一行都不用改。
> **`conflicts` 和 `batches` 没有动**：能对一个脚印**做**的事，仍然全在这里。搬走的是词，不是行为。

### 8.3 测试

这一章的测试文件 `tests/test_faults_chB.py`，开头和帮手：

```python
"""Interlude B: the cycle, the drift it was dodged with, and the checker.

Three faults, and the order matters. FB-01 is a circular import -- the one
thing in this chapter that announces itself. FB-02 is the same boundary being
broken again three months later, which is the reason FB-01's fix is a script
and not a paragraph. FB-03 is the abstraction that gets introduced to break a
cycle and then turns out to have one implementation.

Nothing here touches the network.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest

from minicodex.agent import Wiring
from minicodex.agent_types import STATEFUL, Footprint, ToolCall, ToolSet
from minicodex.approval import AllowAll, Session
from minicodex.composition import local_tools, sub_context, top_level_tools
from minicodex.model import Completed, TextDelta, ToolCallDelta
from minicodex.recorder import NULL_RECORDER, Recorder
from minicodex.shell import ShellSession
from minicodex.subagent import TaskSpec, child_tools, run_task, spawn_toolset

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"
PKG = SRC / "minicodex"
CHECKER = ROOT / "scripts" / "check_layers.py"


def _load_checker() -> Any:
    """Import `scripts/check_layers.py` by path.

    It lives outside the package on purpose -- it is a development tool, and
    shipping it in the wheel would mean users install a thing that reads our
    source tree. That makes it un-importable by name, so it gets loaded the
    long way here rather than being moved somewhere convenient.
    """
    spec = importlib.util.spec_from_file_location("check_layers", CHECKER)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


checker = _load_checker()


def a_copy_of_src(tmp_path: Path, *edits: tuple[str, str, str]) -> Path:
    """A copy of `src/minicodex` with edits applied, for breaking on purpose.

    A copy rather than the working tree, and that is chapter 6's lesson rather
    than caution: the mutation script there restored from a `finally`, which
    does not run when the parent is killed, and left `if False:` sitting in
    `tokens.py`.
    """
    copy = tmp_path / "src" / "minicodex"
    shutil.copytree(PKG, copy)
    for filename, old, new in edits:
        path = copy / filename
        text = path.read_text(encoding="utf-8")
        assert old in text, f"{filename}: pattern not found"
        path.write_text(text.replace(old, new, 1), encoding="utf-8")
    return copy


# The two arrows that make a cycle: `tools` wanting `run_task` so that the
# spawn handler can live with the other handlers, and `subagent` wanting
# `bind_all` to build a child's tool table. Either alone is fine. Together
# the package stops importing.
BOTH_ARROWS = (
    (
        "tools.py",
        "from minicodex.agent_types import",
        "from minicodex.subagent import run_task  # noqa: F401\nfrom minicodex.agent_types import",
    ),
    (
        "subagent.py",
        "from minicodex.agent import Model, Wiring",
        "from minicodex.agent import Model, Wiring\nfrom minicodex.tools import bind_all"
        "  # noqa: F401",
    ),
)


class ScriptedModel:
    def __init__(self, turns: Sequence[Any]) -> None:
        self.turns = list(turns)
        self.sent: list[list[dict[str, Any]]] = []
        self.model = "scripted"

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


def a_context(root: Path, model: Any, wiring: Wiring) -> Any:
    return sub_context(
        build_model=lambda _schemas: model,
        root=root,
        session=Session(mode="workspace-write", approver=AllowAll()),
        parent_shell=ShellSession(),
        wiring=wiring,
        max_turns=8,
    )
```

> - **`_load_checker()`**：§14 的检查器脚本放在包的外面（它是开发工具，不该被装到用户机器上），所以没法按名字导入，只能按文件路径加载。
> - **`a_copy_of_src(tmp_path, *edits)`**：把 `src/minicodex` 复制一份，在拷贝里按给定的"哪个文件、把什么换成什么"改几处。**要故意弄坏代码，永远在拷贝里弄。**
> - **`BOTH_ARROWS`**：§5 的那两条箭头，写成两处修改。`# noqa: F401` 是告诉 ruff"这个 import 没被使用是故意的"。
> - **`ScriptedModel`**：和第 10 章的一样。**`a_context(root, model, wiring)`**：造一个子 Agent 的上下文（§11 的 `sub_context`）。

```python
def test_FB_01_a_toolset_refuses_a_handler_with_no_schema() -> None:
    """The invariant that makes `ToolSet` more than a named tuple.

    Chapter 4 paid for this pair drifting; here it becomes impossible to
    construct rather than a thing to remember. It fired for real on its first
    run, on a *test* -- chapter 10's timeout test patched a fake `sleep`
    handler in and gave it no schema.
    """
    with pytest.raises(ValueError, match="handler with no schema"):
        ToolSet(handlers={"ghost": None}, schemas=[])  # type: ignore[dict-item]


def test_FB_01_a_toolset_refuses_a_schema_with_no_handler() -> None:
    """The other direction produces chapter 0's error for a tool that exists."""
    schema = {"type": "function", "function": {"name": "ghost", "parameters": {}}}
    with pytest.raises(ValueError, match="schema with no handler"):
        ToolSet(handlers={}, schemas=[schema])


def test_FB_01_a_deferred_tool_is_the_one_named_exception() -> None:
    """Chapter 9's registry: callable before its schema is visible."""
    schema = {"type": "function", "function": {"name": "ghost", "parameters": {}}}
    tools = ToolSet(
        handlers={"ghost": None, "hidden": None},  # type: ignore[dict-item]
        schemas=[schema],
        callable_without_schema=frozenset({"hidden"}),
    )
    assert "hidden" in tools.handlers


def test_FB_01_plus_refuses_a_duplicate_name(tmp_path: Path) -> None:
    """F09-01's answer, applied to a local/remote collision."""
    session = Session(mode="read-only", approver=AllowAll())
    base = local_tools(tmp_path, session)
    with pytest.raises(ValueError, match="both declare"):
        base.plus(base)


def test_FB_01_plus_routes_a_footprint_to_the_set_that_owns_it(tmp_path: Path) -> None:
    """Composition must not silently drop one side's classifier.

    The old `route_footprint` did the same job for MCP with an `is_remote()`
    name test. `plus` does it by ownership, so a third source needs no new
    branch anywhere.
    """
    (tmp_path / "a.txt").write_text("a", encoding="utf-8")
    session = Session(mode="read-only", approver=AllowAll())
    ctx = a_context(tmp_path, ScriptedModel(["ok"]), Wiring())
    combined = top_level_tools(tmp_path, session, ctx)

    read = combined.footprint_of(ToolCall("c1", "read_file", {"path": "a.txt"}, "{}"))
    spawn = combined.footprint_of(ToolCall("c2", "spawn_agent", {"task": "t"}, "{}"))
    unknown = combined.footprint_of(ToolCall("c3", "nothing", {}, "{}"))

    assert read.reads and not read.stateful
    assert spawn == STATEFUL
    assert unknown == STATEFUL
```

> 两个方向的不一致都造不出来；报了名字的例外可以；`plus` 拒绝重名；合并后的脚印各归各的（`read_file` 得到一个真的脚印，`spawn_agent` 和不认识的名字都是"和一切冲突"）。

```bash
uv run pytest
git add src/minicodex/agent_types.py src/minicodex/scheduler.py tests/test_faults_chB.py
git commit -m "refactor(types): Footprint moves down, and ToolSet makes three tables one value"
```

---

## §9 `Wiring`：包里唯一造 `Agent` 的地方

`Agent(...)` 除了模型和工具，还收十个带名字的参数。§7 的问题是：两个地方各传了其中的一部分，而且是**不同**的一部分。

把十个分一分：

- **父子之间本来就该不同的，四个**：轮数上限（子任务的更小）、系统消息（子 Agent 有自己的）、会话文件（各写各的）、从哪恢复（子 Agent 不恢复）。
- **父子之间应该相同的，五个**：录制器、窗口大小、总结函数、同时最多跑几个工具、和服务商说话的格式。
- **跟着工具走的，一个**：脚印函数——它现在在 `ToolSet` 里。

"应该相同的那五个"，装成一个对象：

```python
@dataclass(frozen=True)
class Wiring:
    """Everything an `Agent` is given that is neither its model nor its tools.

    Interlude B's whole point in one class.  Before it, an `Agent` was built in
    two places -- once in `__main__` for the top-level run, once in
    `subagent.run_task` for a child -- and the two calls passed *different
    subsets of the same ten keyword arguments*.  Nothing failed.  What happened
    instead, measured on the same scripted model and the same three files:

        child   final request 181,040 chars   compactions 0
        parent  final request  76,896 chars   compactions 1

    plus no transcript on disk for the child at all (F-1-04, off for exactly
    the conversation a human never sees), and every child tool call run
    serially because `footprint_of` was never passed (chapter 8, off -- safe
    only by luck, since the default for an unclassified call is `STATEFUL`).

    A frozen value rather than a builder object: it is created once per run and
    handed down.  `agent()` is the only place in `src/` that calls `Agent(...)`,
    which is checked rather than promised (`scripts/check_layers.py`).  A
    forgettable argument stops being forgettable when there is one call site
    left to forget it at.
    """

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
        """Build the one `Agent` this run gets, from the one `ToolSet` it gets.

        The four keyword arguments left here are the ones that genuinely differ
        between a parent and a child: a child has a smaller turn budget, its own
        task-shaped instructions, its own rollout file, and never resumes.
        Everything a child was previously missing is in `self`, so the way to
        forget it now is to build a second `Wiring`, which is a visible act.
        """
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

> - 五个字段，每个都有默认值（默认是"没有"）。`frozen=True`：一次运行造一个，往下传，不改。
> - **`agent(model, tools, ...)`：从现在起，整个 `src/` 里只有这一处写着 `Agent(...)`。** 它收一个 `ToolSet`（于是处理函数表和脚印函数一起来），
>   加上那四个"本来就该不同"的参数，其余五个从自己身上拿。
> - 这样一来，"忘了给子 Agent 传录制器"这件事做不出来了——除非另造一个 `Wiring`，而那是一个**看得见的动作**。
>   **一个容易忘的参数，在只剩一个调用的地方可以忘的时候，就不容易忘了。**

（`agent.py` 开头的导入里多一个 `ToolSet`。）

"只有这一处"不是靠自觉，§14 的检查器会数。

```python
def test_FB_03_wiring_carries_only_what_two_call_sites_both_need() -> None:
    """A guard against the other failure mode: a bag that grows.

    `Wiring` earns its existence by being the difference between two agent
    constructions, not by being "config". Every field on it is something a
    sub-agent was measured to be missing, or -- `dialect` -- something that
    must not differ between a parent and its child. Four keyword arguments
    stayed outside it, because those are the ones that genuinely differ.
    """
    fields = set(Wiring.__dataclass_fields__)
    assert fields == {
        "recorder",
        "context_window",
        "summariser",
        "max_concurrent_tools",
        "dialect",
    }
```

> 断言 `Wiring` **恰好**是这五个字段。它防的是另一种失败：一个叫"配置"的口袋，谁有新东西都往里塞。
> 往 `Wiring` 里加第六个字段的人，得来改这个测试——也就得回答一句"它是父子都该有的吗"。

```bash
git add src/minicodex/agent.py tests/test_faults_chB.py
git commit -m "refactor(agent): Wiring carries what a parent and a child must share"
```

---

## §10 倒置：`subagent.py` 不再认识 `tools.py`

现在做 §6 的办法 (d)。`subagent.py` 开头那段说明里，讲导入方向的那一段换成了：

```python
This module used to import `tools.py`, for `bind_all` / `tool_context` /
`tool_schemas` / `ToolSpec`, and that single arrow is what made putting the
`spawn_agent` handler next to the other handlers -- where a handler obviously
belongs -- a circular import that stops the package loading:

    ImportError: cannot import name 'ToolContext' from partially initialized
    module 'minicodex.tools' (most likely due to a circular import)

Chapter 10 dodged it by moving the tool up into `__main__`, and interlude B
measured the price: a second place that builds an agent, passing three of the
ten arguments the first place passes.  The arrow is now gone.  This module
takes `build_tools` and `wiring` from whoever constructs its `SubAgentContext`,
and imports nothing from `tools.py` at all.
```

导入里，`from minicodex.tools import ...` **整行没有了**；从 `agent` 导入的换成 `Model, Wiring`，从 `agent_types` 导入的换成 `STATEFUL, Footprint, ToolCall, ToolSet`。

`SubAgentContext` 多两个字段：

```python
    # How a child's own tools get built.  A callable supplied from above rather
    # than a call to `tools.bind_all` here, and that inversion is the whole of
    # interlude B: written the direct way, `subagent` imports `tools`, which
    # forbids `tools` from ever holding the `spawn_agent` handler -- the module
    # where every other handler lives.  Chapter 10 moved the tool up to
    # `__main__` to dodge that; the price was a second assembly site with a
    # different, shorter idea of what an agent needs.
    #
    # Given the child's shell -- seeded from the parent's, so a child starts
    # where the parent is standing rather than where the process started.
    build_tools: Callable[[ShellSession], ToolSet] = lambda _shell: ToolSet({}, [])
    # Recorder, context window, summariser, concurrency cap: the four things a
    # child used to silently not have.  One object, so "the child gets what the
    # parent got" is the default rather than a checklist.
    wiring: Wiring = field(default_factory=Wiring)
```

> - **`build_tools`**：一个函数——给它子 Agent 的 shell，它交回子 Agent 的 `ToolSet`。**这就是"倒置"的全部**：`subagent.py` 不再自己调用 `tools.bind_all`，而是用别人递进来的这个函数。
>   它的默认值是"一个空的工具表"。
> - **`wiring`**：父 Agent 的那个 `Wiring`。

`child_tools` 和 `run_task` 里造 Agent 的那一段：

```python
def child_tools(ctx: SubAgentContext) -> ToolSet:
    """The child's tools: handlers, schemas and footprints, built together.

    Together because chapter 4 paid for the version where the first two were
    separate lists -- a handler with no schema is never called, a schema with
    no handler produces chapter 0's "no tool named X", which was written for
    names the model *invented*.  Interlude B added the third: this function
    used to return a pair, so the child's `Agent` got no `footprint_of` and
    chapter 8's scheduler was off for every sub-agent in the program.
    `ToolSet` makes all three one value and refuses to be built if the first
    two disagree.

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

    tools = ctx.build_tools(shell)
    if ctx.depth + 1 < ctx.max_depth:
        tools = tools.plus(spawn_toolset(replace(ctx, depth=ctx.depth + 1)))
    return tools
```

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

> - `child_tools` 现在返回一个 `ToolSet`：新的 shell（从父 Agent 站的地方开始，和第 10 章一样）→ `ctx.build_tools(shell)` → 没到最底层就 `plus` 上 `spawn_agent`。
> - **`child = ctx.wiring.agent(...)`：整个 §7 的修复，就是这一行。** 它之所以能只有一行，是因为 `Wiring` 存在。

`spawn_agent` 这个工具自己，不再借用 `tools.py` 的 `ToolSpec`（那个类住在这个模块不能再导入的地方），而是直接造一个只有一个工具的 `ToolSet`：

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


def spawn_toolset(ctx: SubAgentContext) -> ToolSet:
    """`spawn_agent` as a one-tool `ToolSet`: handler, schema and footprint.

    A `ToolSet` rather than a `ToolSpec`, and that is the visible edge of
    interlude B's inversion.  `ToolSpec` lives in `tools.py`, and this module
    no longer imports `tools.py` -- because it is the import that forbade
    `tools.py` from ever holding this handler.  What the two sites needed to
    exchange turned out to be a *value* (three tables that must agree), not an
    interface, so the shared thing went into `agent_types.py` with everything
    else two layers both need.

    The same function serves the top-level agent and every level below it: the
    only difference is which `ctx` is closed over, and `child_tools` passes one
    with `depth + 1`.
    """
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

> - **`spawn_footprint`**：一次 `spawn_agent` 的脚印——"和一切冲突"。第 10 章这件事是**碰巧**对的（调度器不认识这个名字，于是用了默认值）。
>   碰巧对的事值得写成一句明说的话：量过的另一种做法，30 次里丢 29 次修改。
> - **`spawn_toolset(ctx)`**：处理函数、说明、脚印，三样一起。最上层的 Agent 和每一层子 Agent 用的是同一个函数，区别只是传进来的 `ctx` 深度不同。
> - 第 10 章的 `spawn_spec` 和 `spawn_tools` 删掉了（`__all__` 里换成 `spawn_footprint` 和 `spawn_toolset`）。

### 10.1 新的规则第一次运行，抓到的是一个测试

改完跑测试，第一个红的不是新代码，是第 10 章的一个测试：

```
ValueError: handler with no schema: ['sleep']
```

那个测试为了模拟"卡住的工具"，往子 Agent 的处理函数表里塞了一个叫 `sleep` 的假工具——**只塞了处理函数，没有给说明**。以前没人管；现在 `ToolSet` 拒绝被造出来。

这个偷懒本身无害。但它说明了一件事：**测试里的每一处偷懒，都是真实的规则不成立的一个地方。** 测试改成连说明一起给。

第 10 章的测试文件 `tests/test_faults_ch10.py` 因此改了一批。帮手：

```python
def context_for(
    root: Path,
    model: Any,
    *,
    session: Session | None = None,
    shell: ShellSession | None = None,
    **kwargs: Any,
) -> SubAgentContext:
    real = session or Session(mode="workspace-write", approver=AllowAll())
    # `sub_context` rather than `SubAgentContext(...)`: interlude B's whole
    # point is that a child is wired by the same code that wires its parent,
    # and a test helper that builds one by hand is a second assembly site
    # again -- with the difference that this one would go green.
    return sub_context(
        build_model=lambda _schemas: model,
        root=root,
        session=real,
        parent_shell=shell or ShellSession(),
        wiring=kwargs.pop("wiring", Wiring()),
        **kwargs,
    )


def _sleep_schema() -> dict[str, Any]:
    return {
        "type": "function",
        "function": {"name": "sleep", "description": "block", "parameters": {"type": "object"}},
    }
```

> **`context_for` 现在调用 `sub_context(...)`**（§11），而不是自己写 `SubAgentContext(...)`。注释说了为什么：这一章的全部意思，就是子 Agent 由"装父 Agent 的同一段代码"来装；
> 一个自己动手造上下文的测试帮手，是又一个"第二个装配的地方"——而且这一个永远是绿的。

改了的测试（改动都在"怎么拿到子 Agent 的工具表"和"怎么塞一个假工具"上，断言没变）：

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
    handlers = child_tools(ctx).handlers
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
    handlers = child_tools(ctx).handlers

    denied = await handlers["apply_patch"]({"edits": []})
    assert "Permission denied" in denied

    session.mode = "workspace-write"
    allowed = await handlers["apply_patch"]({"edits": []})
    assert "Permission denied" not in allowed


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
    handlers = child_tools(ctx).handlers

    # Three seconds of work against half a second of patience. No assertion
    # on elapsed time: on Windows the command is not actually killed (F02-10),
    # so the call returns when the command ends -- but it still reports that
    # the deadline passed, and the deadline is what is being tested.
    output = await handlers["run_shell"]({"command": f"{sys.executable} slow.py"})

    assert "killed: still running" in output


def test_F10_06_the_bottom_level_is_not_given_the_tool(tmp_path: Path) -> None:
    """Not "given it and refused". Chapter 5 measured what happens when a
    prompt names a tool the policy will not allow: the model calls it."""
    top = child_tools(context_for(tmp_path, ScriptedModel(["ok"]), depth=0, max_depth=2))
    assert "spawn_agent" in top.handlers
    assert any(s["function"]["name"] == "spawn_agent" for s in top.schemas)

    bottom = child_tools(context_for(tmp_path, ScriptedModel(["ok"]), depth=1, max_depth=2))
    assert "spawn_agent" not in bottom.handlers
    assert not any(s["function"]["name"] == "spawn_agent" for s in bottom.schemas)


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
        # A schema as well as a handler, because `ToolSet` refuses the pair
        # when they disagree -- including when the disagreement is a test
        # taking a shortcut. Interlude B's invariant found this on its first
        # run: the fake tool went in as a handler alone and was rejected.
        tools = original(c)
        return replace(
            tools,
            handlers={**tools.handlers, "sleep": forever},
            schemas=[*tools.schemas, _sleep_schema()],
        )

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
        # A schema as well as a handler, because `ToolSet` refuses the pair
        # when they disagree -- including when the disagreement is a test
        # taking a shortcut. Interlude B's invariant found this on its first
        # run: the fake tool went in as a handler alone and was rejected.
        tools = original(c)
        return replace(
            tools,
            handlers={**tools.handlers, "sleep": slow},
            schemas=[*tools.schemas, _sleep_schema()],
        )

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
    handlers = child_tools(ctx).handlers
    output = await handlers["run_shell"]({"command": "rm -rf build"})

    assert len(seen) == 1
    assert "rm -rf build" in seen[0].what
    assert "Permission denied" in output


def test_the_schema_and_the_handler_come_from_one_object(tmp_path: Path) -> None:
    """Chapter 4's rule, for a tool declared outside `tool_specs()`.

    Interlude B made this structural rather than conventional: `ToolSet`
    refuses to be constructed at all when the two disagree, so the assertion
    below is now about the *value*, not about a promise in a docstring.
    """
    ctx = context_for(tmp_path, ScriptedModel(["ok"]))
    tools = spawn_toolset(ctx)
    assert set(tools.handlers) == {s["function"]["name"] for s in tools.schemas}
    assert callable(tools.handlers["spawn_agent"])


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
        tools = original(c)
        return replace(
            tools,
            handlers={**tools.handlers, "sleep": stopped},
            schemas=[*tools.schemas, _sleep_schema()],
        )

    monkeypatch.setattr(subagent, "child_tools", patched)
    ctx = context_for(tmp_path, ScriptedModel([[("c", "sleep", {})], "done"]))
    result = await run_task(TaskSpec(task="t"), ctx)

    assert result.outcome == "interrupted"
    assert "Do not start it again unless asked" in result.render()
```

> - `child_tools(ctx)` 现在是一个 `ToolSet`，所以写 `child_tools(ctx).handlers`。
> - 塞假工具的三处，用 `replace(tools, handlers={...}, schemas=[...])` 造一个新的 `ToolSet`——处理函数和说明一起加。
> - `test_the_schema_and_the_handler_come_from_one_object`：以前断言的是一句写在 docstring 里的承诺，现在断言的是一个值。

第 10 章那两个"检查命令行接线"的测试，随着 `__main__.py` 里那几行的消失而删掉了；替代它们的在 §12。

这一章自己的测试：

```python
def test_FB_01_subagent_does_not_import_tools() -> None:
    """The arrow interlude B inverted.

    `subagent.py` used to import `bind_all`, `tool_context`, `tool_schemas` and
    `ToolSpec` from `tools.py`. One arrow, and it made the obvious home for the
    `spawn_agent` handler -- next to every other handler -- a circular import
    that stops the package loading:

        ImportError: cannot import name 'ToolContext' from partially
        initialized module 'minicodex.tools' (most likely due to a circular
        import)

    Chapter 10 dodged it by moving the tool up into `__main__`. This asserts
    the arrow is gone, which is what makes the dodge unnecessary.
    """
    imports = checker.intra_package_imports(PKG / "subagent.py")
    assert "minicodex.tools" not in imports


def test_FB_01_a_child_is_given_the_parents_recorder(tmp_path: Path) -> None:
    """F-1-04, off for exactly the conversation a human never watches.

    Before this, `run_task` built its `Agent` with three of the ten arguments
    `__main__` passes, and `recorder` was not one of them. A sub-agent's whole
    conversation existed nowhere afterwards.
    """
    recorder = Recorder(tmp_path / "rec")
    model = ScriptedModel(["done"])
    ctx = a_context(tmp_path, model, Wiring(recorder=recorder))
    asyncio.run(run_task(TaskSpec("count the files", "a number"), ctx))

    written = recorder.path.read_text(encoding="utf-8")
    assert "count the files" in written


def test_FB_01_a_child_compacts_like_its_parent(tmp_path: Path) -> None:
    """The measured half: 181,040 characters against 76,896, same script."""
    (tmp_path / "big.txt").write_text("x" * 60_000, encoding="utf-8")
    script = [
        [("c1", "read_file", {"path": "big.txt"})],
        [("c2", "read_file", {"path": "big.txt"})],
        [("c3", "read_file", {"path": "big.txt"})],
        "done",
    ]

    async def summarise(_request: Any) -> str:
        return "## Done: read the file three times"

    def final_size(wiring: Wiring) -> int:
        model = ScriptedModel(script)
        asyncio.run(
            run_task(TaskSpec("read it three times", "ok"), a_context(tmp_path, model, wiring))
        )
        return sum(len(json.dumps(m)) for m in model.sent[-1])

    unwired = final_size(Wiring())
    wired = final_size(Wiring(context_window=32_000, summariser=summarise))
    assert wired < unwired / 2, (unwired, wired)


def test_FB_01_a_child_gets_the_schedulers_footprints(tmp_path: Path) -> None:
    """The third channel, which travelled with neither of the other two.

    `child_tools` used to return `(handlers, schemas)`, so `run_task` had no
    `footprint_of` to pass and every sub-agent ran its tools one at a time.
    Safe -- chapter 8's default for an unclassified call is `STATEFUL` -- and
    silently slower, which is the kind of wrong nothing reports.
    """
    (tmp_path / "a.txt").write_text("a", encoding="utf-8")
    tools = child_tools(a_context(tmp_path, ScriptedModel(["ok"]), Wiring()))
    call = ToolCall("c1", "read_file", {"path": "a.txt"}, "{}")
    footprint = tools.footprint_of(call)
    assert footprint != STATEFUL
    assert footprint.reads == frozenset({str((tmp_path / "a.txt").resolve())})
```

> - 第一个：`subagent.py` 的导入里没有 `minicodex.tools`。
> - 第二个：给子 Agent 的 `Wiring` 里有录制器，跑完之后录制文件里有那句任务。
> - 第三个：§7 的那个测量，写成测试——同一份脚本，有窗口和总结函数的子 Agent，最后一个请求不到没有的那个的一半。
> - 第四个：子 Agent 的 `read_file` 有一个真的脚印（读那个文件），不再是"和一切冲突"。

---

## §11 `composition.py`：装配的地方

倒置之后，"怎么造子 Agent 的工具表"得有人来提供。谁？一个新的、在所有这些模块**上面**的模块：

```python
"""Where a run is assembled: tools in, one `ToolSet` out, for parents and children.

This module exists because there were two places that built an agent and they
disagreed, silently, for a whole chapter.  `__main__` composed three sources of
tools with three unrelated expressions -- a dict splat for handlers, a list
splat for schemas, a `route_footprint()` wrapper for the scheduler -- and
`subagent.child_tools` composed one source with two of the three.  Measured on
the same scripted model, the same three files and the same tools:

    child   final request 181,040 chars   compactions 0   transcript: none
    parent  final request  76,896 chars   compactions 1   transcript: written

Nothing raised.  Nobody was going to find that by reading `subagent.py`,
because `subagent.py` is *correct* on its own terms -- it is only wrong next
to a call site three modules away that nothing puts it next to.

So this module owns the composition and both sites call it.  It is the top of
the package: it imports `agent`, `registry`, `subagent` and `tools`, and none
of them imports it.  That direction is what lets `subagent` stop importing
`tools`, which is what makes the `spawn_agent` handler movable again.

Deliberately not here: argument parsing, printing, the session file, the
approval prompt.  Those stayed in `__main__`.  A composition root that also
does IO is a composition root you cannot call from a test, which is the state
this package was in when the drift above went unnoticed for a chapter.
"""

from __future__ import annotations

import functools
from collections.abc import Callable
from pathlib import Path
from typing import Any

from minicodex.agent import Wiring
from minicodex.agent_types import ToolCall, ToolSet
from minicodex.approval import Session
from minicodex.registry import McpRegistry, is_remote
from minicodex.shell import ShellSession
from minicodex.subagent import SubAgentContext, spawn_toolset
from minicodex.tools import bind_all, footprint_of, tool_context, tool_schemas


def local_tools(
    root: Path,
    session: Session,
    shell: ShellSession | None = None,
) -> ToolSet:
    """This project's own tools, bound to one repository and one conversation.

    The three tables that used to be assembled separately -- `bind_all()`,
    `tool_schemas()`, `functools.partial(footprint_of, root=root)` -- built in
    one expression, so that a fourth tool cannot arrive in two of them.
    """
    context = tool_context(root=root, session=session, shell=shell)
    return ToolSet(
        handlers=bind_all(context),
        schemas=list(tool_schemas()),
        footprint_of=functools.partial(footprint_of, root=context.root),
    )


def child_tools_builder(root: Path, session: Session) -> Callable[[ShellSession], ToolSet]:
    """What `SubAgentContext.build_tools` gets: a child's local tools, given its shell.

    A partially applied function rather than the `SubAgentContext` itself,
    because the shell is the one thing the sub-agent module has to make (it
    seeds a fresh one from the parent's working directory) and everything else
    is decided up here.  `spawn_agent` is *not* added by this function --
    `child_tools` adds it, or does not, depending on depth, which is the one
    decision that genuinely belongs down there.

    Sub-agents get no MCP tools, which is chapter 10's decision and is left
    alone: a remote tool's `Footprint` is a promise made by somebody else's
    code, and handing a child a resource the parent's scheduler cannot see is
    F08-06 with a second process in the way.  Written here, as an absence in
    one function, instead of being implied by which module imports what.
    """
    return lambda shell: local_tools(root, session, shell)


def with_remote_tools(base: ToolSet, registry: McpRegistry) -> ToolSet:
    """Add chapter 9's registry to a local tool set.

    Not `base.plus(...)`, and the reason is the one piece of asymmetry in the
    whole assembly: the registry owns a *live* schema list.  `registry.visible`
    is the object the model client holds by reference, and `tool_search`
    reveals a deferred tool by appending to it -- so the schemas of the merged
    set have to *be* that list, not a copy of it.  `McpRegistry(local=...)`
    takes `base.schemas` as input for exactly this reason, which is why the
    registry is constructed after the local set and not before.

    `callable_without_schema` is the honest form of the other half: a deferred
    tool has a handler and no visible schema, which `ToolSet.__post_init__`
    would otherwise refuse.  Naming the exception keeps the rule strict for
    everybody else instead of relaxing it for everybody.
    """
    # The one ordering rule in this module, said out loud. Building the
    # registry before the local set, or forgetting `local=`, produces a tool
    # set whose schemas are missing every local tool -- which `ToolSet` does
    # refuse, but with a message about four handlers rather than about the two
    # lines that are in the wrong order.
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


def top_level_tools(
    root: Path,
    session: Session,
    sub_context: SubAgentContext,
) -> ToolSet:
    """Local tools plus `spawn_agent`, before any MCP server is consulted.

    Split from `with_remote_tools` because of the ordering above: the registry
    needs this set's schemas as its `local=` argument, so it cannot be built
    until this exists.
    """
    return local_tools(root, session, sub_context.parent_shell).plus(spawn_toolset(sub_context))


def sub_context(
    *,
    root: Path,
    session: Session,
    parent_shell: ShellSession,
    build_model: Callable[[list[dict[str, Any]]], Any],
    wiring: Wiring,
    **rest: Any,
) -> SubAgentContext:
    """A `SubAgentContext` with `build_tools` already wired to this run.

    One function so that "a child gets the same tools and the same wiring as
    its parent" is a default rather than a thing to remember at each call
    site.  `**rest` carries the fields that are genuinely per-run -- session
    directory, provider and model names, the announcer -- straight through.
    """
    return SubAgentContext(
        build_model=build_model,
        root=root,
        session=session,
        parent_shell=parent_shell,
        build_tools=child_tools_builder(root, session),
        wiring=wiring,
        **rest,
    )


__all__ = [
    "child_tools_builder",
    "local_tools",
    "sub_context",
    "top_level_tools",
    "with_remote_tools",
]
```

> 开头的说明讲了它为什么存在，以及**它不做什么**：解析命令行、打印、会话文件、审批提示都留在 `__main__.py`。一个顺便做输入输出的装配模块，是一个测试没法调用的装配模块——
> §7 的问题能藏一整章，就是因为当时装配和输入输出搅在一起。
>
> - **`local_tools(root, session, shell)`**：这个项目自己的工具。原来分三处拼的三样东西（`bind_all()`、`tool_schemas()`、带着根目录的 `footprint_of`），在一个表达式里造出来——
>   以后加第四个工具，不可能只出现在其中两样里。
> - **`child_tools_builder(root, session)`**：交给 `SubAgentContext.build_tools` 的那个函数。docstring 记着一个**故意的缺席**：子 Agent 没有 MCP 的工具（第 10 章的决定）——
>   现在它是写在一个函数里的一句话，而不是"哪个模块恰好导入了哪个模块"的副作用。
> - **`with_remote_tools(base, registry)`**：把第 9 章的注册表加进来。**不用 `plus`**，因为这是整个装配里唯一不对称的地方：注册表拥有那个"活的"说明列表，
>   合并后的 `ToolSet` 的 `schemas` 必须**就是**那个列表（`schemas=registry.visible`），不能是一份拷贝。
>   - 所以有一条顺序上的规矩：注册表得在本地工具**之后**创建，并且被告知本地工具的说明（`McpRegistry(local=...)`）。
>     这条规矩是把顺序弄反之后发现的——那时 `ToolSet` 确实拒绝了，但报的是"四个处理函数没有说明"，而不是"有两行的顺序反了"。
>     所以开头有一个明说的检查，用这条规矩自己的话报错。
>   - 被藏起来的工具，报进 `callable_without_schema`（§8.2 的那个例外）。
> - **`top_level_tools(root, session, sub_context)`**：最上层的 Agent 在接上远程工具之前的工具——本地的，加上 `spawn_agent`。它用的是 `sub_context.parent_shell`：**父 Agent 的 shell 和"子 Agent 从哪开始"用的是同一个对象。**
> - **`sub_context(...)`**：造一个 `SubAgentContext`，`build_tools` 已经接好。`**rest` 把只和这次运行有关的字段（会话目录、模型名、往哪打印）原样带过去。

`registry.py` 里的 `route_footprint` **删掉了**（连同 `__all__` 里的名字和一个用不着了的导入）。第 9 章那个测试相应地改成：

```python
def test_F09_remote_names_never_reach_the_local_classifier(tmp_path: Path) -> None:
    """`route_footprint` was replaced by `ToolSet` composition in interlude B.

    The behaviour it existed for is unchanged and still asserted here: a
    remote call is classified by the registry, a local one by `tools`, and
    neither classifier is ever shown the other's names. What changed is how
    the routing is decided -- by which set declared the name, rather than by
    an `is_remote()` test on the name itself, which needed a new branch every
    time a fourth source of tools appeared.
    """
    seen: list[str] = []
    session = Session(mode="read-only", approver=AllowAll())

    def local(c: ToolCall) -> Footprint:
        seen.append(c.name)
        return Footprint(reads=frozenset({"local"}))

    base = replace(local_tools(tmp_path, session), footprint_of=local)
    # `local=base.schemas` is the ordering rule `with_remote_tools` enforces:
    # the registry owns the list the model client is handed, so it has to be
    # built after the local set and told about it.
    registry = McpRegistry(local=base.schemas)
    combined = with_remote_tools(base, registry)

    assert combined.footprint_of(a_call("read_file", path="a.py")) == Footprint(
        reads=frozenset({"local"})
    )
    assert combined.footprint_of(a_call("mcp__x__y")) == STATEFUL
    assert seen == ["read_file"]  # the remote call never reached the local one
```

> 行为没变，仍然断言：本地的调用由本地的脚印函数判断，远程的由注册表判断。变的是"怎么分流"。

### 11.1 `__main__.py`

导入：`Agent` 换成 `Wiring`；多一行 `from minicodex.composition import sub_context, top_level_tools, with_remote_tools`；
`route_footprint`、`functools`、`SubAgentContext`、`spawn_tools`、`bind_all`、`footprint_of` 都不再需要。

`_ask()` 里：

```python
    root = Path.cwd().resolve()
    # The parent's own shell object, created here and handed to the sub-agent
    # context, so that a child can be seeded from where the parent is standing.
    context = tool_context(root=root, session=session)
    wiring = Wiring(
        recorder=recorder,
        context_window=context_window,
        # A client of its own, with no tools.  Two reasons, both found by a
        # test of this function rather than of the things it calls:
        #
        # The summariser used to be attached further down, with `replace()`,
        # once the agent's own client existed.  By then the `spawn_agent`
        # handler had been built around *this* object, and `replace()` makes a
        # new one -- so a sub-agent got a context window and no summariser,
        # and compaction needs both.  The wiring has to be complete before
        # anything is handed it.
        #
        # And `make_summariser` has said since chapter 6 that the summariser
        # "must not see the tool schemas".  Handed the agent's client, which
        # sends its tool list with every request, it always did.
        summariser=make_summariser(make_model([])) if context_window else None,
    )
    current = SessionMeta(
        session_id=new_session_id(),
        created=time.time(),
        cwd=os.getcwd(),
        provider=provider,
        model=model or default_model,
        sandbox_mode=session.mode,
        approval_policy=session.policy,
    )
    sub_ctx = sub_context(
        build_model=make_model,
        root=root,
        session=session,
        # The parent's own shell, so a child starts in the directory the parent
        # is standing in rather than the one the process started in.
        parent_shell=context.shell,
        wiring=wiring,
        sessions_dir=session_dir,
        parent_session_id=current.session_id,
        provider=provider,
        model=model or default_model,
        announce=print,
    )
    tools = top_level_tools(root, session, sub_ctx)
```

```python
    registry = McpRegistry(local=tools.schemas)
    ...
    tools = with_remote_tools(tools, registry)
    llm = make_model(tools.schemas)
    ...
    # The same two objects a sub-agent is built from, one level up.  Before
    # interlude B this call passed seven keyword arguments and the sub-agent's
    # passed three, and no test compared them because nothing put them next to
    # each other.
    agent = wiring.agent(
        llm,
        tools,
        instructions=_instructions(session),
        rollout=writer,
        resume_from=resume_from,
    )
```

> 读下来的顺序就是装配的顺序：
> 1. `Wiring`——录制器、窗口大小、总结函数（它那段长注释是 §12 的故事）；
> 2. 会话文件头；`sub_context(...)`——子 Agent 的上下文，带着这个 `wiring`；
> 3. `top_level_tools(...)`——本地工具加 `spawn_agent`；
> 4. 注册表，被告知本地的说明；连上 MCP server 之后 `with_remote_tools(...)`；
> 5. 模型客户端，拿着 `tools.schemas`（就是注册表的那个列表）；
> 6. `wiring.agent(llm, tools, ...)`——和造子 Agent 的是同一个方法，同一个 `wiring`。

```bash
uv run pytest
git add src/minicodex/subagent.py src/minicodex/composition.py src/minicodex/registry.py src/minicodex/__main__.py tests/
git commit -m "refactor: one place assembles an agent, and sub-agents are assembled by it"
```

---

## §12 意外：修好的东西，在真的程序里没有生效

到上面那个提交为止，这一章的每个测试都是绿的，探针的数字也对：

```
$ uv run python probe_boundaries.py drift-live
arm                          final request   recorded   peak    wall
Wiring()  (the old child)          181,040      False      2   0.22s
the parent's wiring                 77,244       True      2   0.22s
```

给子 Agent 父 Agent 的 `Wiring`，它就压缩、被录制、并行。修好了。

第 8、9、10 章各发现过一次"机制是对的、测过了、命令行里没接上"，每次都写了一个去看 `__main__.py` 里那行 `Agent(...)` 的测试。
这一章把那行 `Agent(...)` 删了——**那三个测试也就跟着没了**。所以改写这一章时，把它们对着新的装配重写了一遍。其中一个是新的问题：
带着 `--context-window` 跑，子 Agent 拿到的 `Wiring`，和父 Agent 用的是不是同一个？

```
FAILED tests/test_faults_chB.py::test_the_cli_gives_a_sub_agent_the_same_wiring_as_its_parent
E       AssertionError: the child was handed a different wiring
E       assert Wiring(recorder=<...Recorder object at 0x000001596E1BB820>, context_window=32000, summariser=None, max_concurrent_tools=8, dialect='chat_completions') is Wiring(recorder=<...Recorder object at 0x000001596E1BB820>, ...
```

**不是。子 Agent 拿到的那个：`context_window=32000, summariser=None`。** 有窗口大小，没有总结函数。而第 6 章的压缩要两样都有才会启动。

也就是说：在真的程序里，带着 `--context-window`，**子 Agent 仍然从不压缩**。§7 那张表里最要紧的一项，修了，没有修到。

原因在当时 `__main__.py` 的这几行：

```python
    wiring = Wiring(recorder=recorder, context_window=context_window, summariser=None)
    ...
    sub_ctx = sub_context(..., wiring=wiring, ...)
    tools = top_level_tools(root, session, sub_ctx)     # spawn_agent 的处理函数在这里造出来，拿着 sub_ctx
    ...
    llm = make_model(tools.schemas)
    if context_window:
        wiring = replace(wiring, summariser=make_summariser(llm))
        sub_ctx = replace(sub_ctx, wiring=wiring)
```

总结函数需要模型客户端，模型客户端需要工具列表，工具列表需要 `spawn_agent`，`spawn_agent` 需要子 Agent 的上下文——所以总结函数只能"后面再补"。
补的办法是 `replace()`。而 `replace()` **造的是一个新对象**：变量 `wiring` 和 `sub_ctx` 指向了新的，**`spawn_agent` 的处理函数手里拿着的，还是旧的那个**。

当时的注释还特意写着："用 `replace()` 而不是造第二个 `Wiring`，这样往下传的仍然只有一个对象。"**它造的正是第二个。**

这一章用来防"两个地方各传各的"的那个东西，自己在装配的地方变成了两个。而这一章的每个测试都**自己造**上下文，所以每个都是绿的——和第 10 章是同一个形状，和这一章 §10.1 说 `context_for` 时警告的也是同一个形状。

### 12.1 修法，和顺手发现的另一件事

要让 `Wiring` 在被交给任何人之前就是完整的，就得在一开始造出总结函数——也就是不能等那个带着全部工具的模型客户端。那就给总结函数**一个它自己的、不带任何工具的客户端**：

```python
        summariser=make_summariser(make_model([])) if context_window else None,
```

后面那个 `if context_window:` 的 `replace` 整段删掉。

写这一行时去看了 `make_summariser` 的说明。它从第 6 章起就写着：总结用的那次模型调用"**不能看到工具的说明**"。
而它一直拿到的是 Agent 自己的那个模型客户端——第 1 章的客户端每次请求都会带上自己的工具列表。**所以它一直看得到。**

```
FAILED tests/test_faults_chB.py::test_the_summariser_is_given_a_client_with_no_tools
E       AssertionError: assert [{'type': 'fu...for it.'}}}}}] == []
E         Left contains 5 more items, first extra item: {'type': 'function', 'function': {'name': 'read_file', ...
```

一句说了四章的话，从来没有成立过。现在成立了，顺带每次总结少发几百个 token 的工具说明。

（这一处只在这一章和之后的代码里改了；第 6 到第 10 章的代码快照保持原样——那几章的正文是照着它们写的。）

### 12.2 守住命令行的测试

```python
def _cli_run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *argv: str) -> dict[str, Any]:
    """Run `minicodex ask` for real, up to the first model call, and report
    what the top-level agent was built from.

    Chapters 8, 9 and 10 each found a mechanism that was correct, tested, and
    not connected in `__main__.py` -- and each wrote a test that spied on the
    `Agent(...)` call there. Interlude B deleted that call, so those tests went
    with it; this is what replaces them. The spy sits on `Wiring.agent`, which
    is now the only way an agent gets built.
    """
    import minicodex.__main__ as cli
    from minicodex.agent import Agent, RunResult
    from minicodex.history import History
    from minicodex.registry import McpRegistry
    from minicodex.subagent import TaskResult

    seen: dict[str, Any] = {"summariser_models": []}
    real_agent = Wiring.agent
    real_make_summariser = cli.make_summariser
    real_sub_context = cli.sub_context

    def spy_sub_context(**kwargs: Any) -> Any:
        # The context the `spawn_agent` handler is about to be built around.
        seen["child_ctx"] = real_sub_context(**kwargs)
        return seen["child_ctx"]

    def spy_agent(self: Wiring, model: Any, tools: ToolSet, **kwargs: Any) -> Any:
        seen.update(wiring=self, model=model, tools=tools, kwargs=kwargs)
        return real_agent(self, model, tools, **kwargs)

    def spy_make_summariser(model: Any, **kwargs: Any) -> Any:
        seen["summariser_models"].append(model)
        return real_make_summariser(model, **kwargs)

    async def no_run(self: Agent, user_message: str) -> RunResult:
        # Stand in for one sub-agent having finished during the run.
        seen["child_ctx"].children.append(TaskResult("ok", "x", 2, 1.5, "child-1"))
        return RunResult("ok", "completed", 1, History())

    class SpyRegistry(McpRegistry):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            super().__init__(*args, **kwargs)
            seen["registry"] = self

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(Wiring, "agent", spy_agent)
    monkeypatch.setattr(Agent, "run", no_run)
    monkeypatch.setattr(cli, "McpRegistry", SpyRegistry)
    monkeypatch.setattr(cli, "make_summariser", spy_make_summariser)
    monkeypatch.setattr(cli, "sub_context", spy_sub_context)

    sessions = tmp_path / "sessions"
    assert cli.main(["ask", "anything", "--yes", "--session-dir", str(sessions), *argv]) == 0
    seen["sessions"] = sessions
    return seen


def test_the_cli_switches_the_scheduler_on(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Chapter 8's finding, re-asked of the new assembly."""
    (tmp_path / "x.txt").write_text("hi", encoding="utf-8")
    seen = _cli_run(tmp_path, monkeypatch)

    footprint = seen["tools"].footprint_of(ToolCall("c1", "read_file", {"path": "x.txt"}, "{}"))
    assert footprint.reads == {str((tmp_path / "x.txt").resolve())}


def test_the_cli_hands_the_model_client_the_registrys_own_list(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Chapter 9's finding: `tool_search` appends to one list, and the model
    client has to be holding that list and not a copy of it."""
    seen = _cli_run(tmp_path, monkeypatch)

    assert seen["model"].tools is seen["registry"].visible
    assert seen["tools"].schemas is seen["registry"].visible


def test_the_cli_gives_a_sub_agent_the_same_wiring_as_its_parent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Interlude B's own fix, checked where it is used rather than where it is
    defined -- and it was not there.

    `__main__` built the `spawn_agent` handler first and attached the
    summariser to the wiring afterwards, with `replace()`. `replace()` makes a
    new object; the handler went on holding the old one. So in the real
    program a child got a context window and no summariser, and compaction
    needs both: with `--context-window` set, sub-agents still never compacted.
    Every test of the fix built its own context and passed. Found by writing
    this test.
    """
    seen = _cli_run(tmp_path, monkeypatch, "--context-window", "32000")

    assert seen["wiring"].summariser is not None
    assert seen["child_ctx"].wiring is seen["wiring"], "the child was handed a different wiring"
    # And the one wiring has in it what the command line was given.
    assert seen["wiring"].context_window == 32000
    assert seen["wiring"].recorder is not NULL_RECORDER


def test_the_summariser_is_given_a_client_with_no_tools(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`make_summariser`'s docstring has said since chapter 6 that the
    summariser "must not see the tool schemas". It was handed the agent's own
    client, which sends its tool list with every request -- so it always did.
    """
    seen = _cli_run(tmp_path, monkeypatch, "--context-window", "32000")

    (model,) = seen["summariser_models"]
    assert model.tools == []


def test_the_cli_wires_a_sub_agent_to_the_run_it_belongs_to(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Chapter 10's finding: where a child starts, where its file goes, and
    whose child it says it is."""
    (tmp_path / "sub").mkdir()
    seen = _cli_run(tmp_path, monkeypatch)
    tools, child_ctx = seen["tools"], seen["child_ctx"]

    assert "spawn_agent" in tools.handlers

    assert any(s["function"]["name"] == "spawn_agent" for s in seen["model"].tools)
    # The parent's shell and the one a child is seeded from are one object:
    # move the parent, and the child's starting point moves with it.
    asyncio.run(tools.handlers["run_shell"]({"command": "cd sub"}))
    assert Path(child_ctx.parent_shell.cwd).name == "sub"
    assert child_ctx.sessions_dir == seen["sessions"]
    assert child_ctx.parent_session_id == seen["kwargs"]["rollout"].meta.session_id


def test_the_cli_lists_each_sub_agent_when_the_run_ends(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A session id printed nowhere is a link nobody follows (F10-12)."""
    _cli_run(tmp_path, monkeypatch)

    assert "[sub-agent child-1: ok, 2 turn(s), 1.5s]" in capsys.readouterr().out


def test_the_sessions_listing_says_which_ones_are_sub_agents(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The listing is how `--resume last` picking a child was noticed at all."""
    import time

    import minicodex.__main__ as cli
    from minicodex.rollout import RolloutWriter, SessionMeta, rollout_path

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

> - **`_cli_run(tmp_path, monkeypatch, *argv)`**：真的调用 `main(["ask", ...])`，在第一次请求模型之前停下，交回"最上层的 Agent 是用什么造出来的"。
>   - 探子放在 `Wiring.agent` 上——现在造 Agent 只有这一条路。它记下 `wiring`、模型客户端、`ToolSet` 和其余参数。
>   - `spy_sub_context` 记下子 Agent 的上下文——**就是 `spawn_agent` 的处理函数马上要拿在手里的那一个**。
>   - `spy_make_summariser` 记下总结函数拿到的是哪个模型客户端；`SpyRegistry` 记下注册表。
>   - `Agent.run` 换成"假装有一个子 Agent 跑完了，然后直接返回"。
> - 七个测试，前三章各一个老问题，加这一章的两个新问题：
>   1. 调度器开着（第 8 章）：`read_file` 的脚印是那个文件。
>   2. 模型客户端拿的**就是**注册表的那个列表（第 9 章）：用 `is`。
>   3. **子 Agent 拿到的 `wiring` 就是父 Agent 的那个**（这一章）：用 `is`；并且它有总结函数。
>   4. 总结函数的客户端没有工具（这一章）。
>   5. 子 Agent 的接线（第 10 章）：模型看得到 `spawn_agent`；让**父 Agent 的** `run_shell` 执行 `cd sub`，子 Agent 的起点跟着变了——证明是同一个 shell；会话目录和父会话编号都对。
>   6. 结束时打印了每个子 Agent 的那一行。
>   7. `minicodex sessions` 标出哪些是子 Agent。

```bash
git add src/minicodex/__main__.py tests/test_faults_chB.py
git commit -m "fix(cli): build the wiring complete, before a sub-agent is handed a copy of it"
```

---

## §13 FB-03：没有造出来的那个接口

要让两个模块不再互相导入，最顺手的办法是造一个"接口"：定义一个 `ToolProvider`，约定三个方法（`handlers()`、`schemas()`、`footprint()`），
让 `tools`、`subagent`、`registry` 各写一个类去实现它，两边都只依赖这个接口。

这个设计做了，然后扔了。原因是 FB-03 本身：**这三个模块不需要"是"某种对象，它们只需要"交回"同一种形状的东西。**
三个函数各返回一个 `ToolSet`，没有任何东西需要被继承或实现。一个接口有"实现"可数；一个值类型没有。

这一章反而**删掉**了两个抽象：

- `route_footprint(registry, local)`：只有一个调用者，每多一种工具来源就得加一个分支。`ToolSet.plus` 按"名字是谁的"分流，不需要它。
- `spawn_agent` 用的 `ToolSpec`：它存在只是为了从那个"环"所在的模块里借一个类。

```python
def test_FB_03_the_shared_thing_is_a_value_with_three_producers(tmp_path: Path) -> None:
    """`ToolSet` is a data type, not an interface, and that is the point.

    The reflex when breaking a cycle is to declare a `Protocol` -- something
    like `ToolProvider` with `handlers()`, `schemas()` and `footprint()` --
    and have both sides depend on it. That was designed and dropped: the
    three producers here (`tools`, `subagent`, `registry`) do not need to be
    objects, they need to *return* the same shape. An interface with one real
    implementation is FB-03; three functions returning one value type is not
    an interface at all, and has nothing to subclass.

    This test counts the producers, because "three call sites" is the claim
    the design rests on and a claim in a docstring rots.
    """
    session = Session(mode="read-only", approver=AllowAll())
    ctx = a_context(tmp_path, ScriptedModel(["ok"]), Wiring())

    produced = [
        local_tools(tmp_path, session),
        spawn_toolset(ctx),
        child_tools(ctx),
    ]
    assert all(isinstance(t, ToolSet) for t in produced)
    assert len({tuple(sorted(t.handlers)) for t in produced}) == 3


def test_FB_03_footprints_did_not_get_their_own_abstraction(tmp_path: Path) -> None:
    """The rejected inversion, pinned so it stays rejected.

    `route_footprint(registry, local)` used to exist in `registry.py` for
    exactly one caller. It is gone: `ToolSet.plus` routes by ownership, which
    needs no name test, no import of `is_remote` at the call site, and no
    branch when a fourth source arrives. Removing a function is a refactor
    result worth asserting -- otherwise somebody re-adds it next to the thing
    that replaced it.
    """
    import minicodex.registry as registry_module

    assert not hasattr(registry_module, "route_footprint")


def test_FB_03_the_footprint_type_stayed_where_behaviour_can_use_it() -> None:
    """Moving a type down must not move the behaviour with it.

    `Footprint`, `STATEFUL` and `FootprintFn` live in `agent_types.py` now, so
    that `subagent.py` can name them without importing a module it depends on
    for nothing else. `conflicts` and `batches` did not move: they are what
    the scheduler *is*.
    """
    import minicodex.scheduler as scheduler_module

    assert scheduler_module.Footprint is Footprint
    assert callable(scheduler_module.conflicts)
    assert "conflicts" not in dir(sys.modules["minicodex.agent_types"])
```

> - 第一个**数**了一遍：三个地方各交回一个 `ToolSet`，三个的工具各不相同。"有三个生产者"是这个设计站得住的理由，而写在 docstring 里的理由会过期。
> - 第二个：`registry` 模块里没有 `route_footprint` 了。**删掉一个函数，是值得写成断言的重构结果**——否则迟早有人在替代它的东西旁边把它加回来。
> - 第三个：`scheduler.Footprint` 和 `agent_types.Footprint` 是同一个东西；`conflicts` 还在 `scheduler` 里，没有跟着搬下去。

---

## §14 FB-02：把边界写成检查

§6 之后，`subagent → tools` 这条箭头没有了。三个月后，某个人为了省事在 `subagent.py` 里写一句 `from minicodex.tools import bind_all`——什么会拦住他？

一段写在文档里的"请不要这样做"拦不住。第 3 章的快照测试、第 9 章的"它不挡合并"，这本书已经两次得到同一个结论：**写成文字的承诺不是机制。**

所以是一个脚本，`scripts/check_layers.py`：

```python
#!/usr/bin/env python
"""Architecture rules that are cheap enough to check, checked.

    uv run python scripts/check_layers.py [--src PATH]

Exit 0 and say nothing when every rule holds; exit 1 and name the rule, the
module and the fault it came from otherwise.

Every rule below was a real failure first.  None of them is here because it
seemed like good practice -- the section at the bottom lists the rules that
were *not* written, and why, which matters as much.

Runs in CI as part of the lint step rather than as a step of its own.  Chapter
-1's guard (`len(steps) <= 6`, "the blocking suite is meant to stay fast")
already forced that question once in chapter 9, and the honest answer here is
different from chapter 9's: this is a static check that takes under a second,
which is what the lint step is.  A new step would have been a way of not
answering.
"""

from __future__ import annotations

import argparse
import ast
import subprocess
import sys
from pathlib import Path

PACKAGE = "minicodex"

# Import edges that are forbidden, and the fault each one came from.  A list of
# specific arrows rather than a table of layer numbers for every module: see
# "rules not written" at the bottom.
FORBIDDEN: list[tuple[str, str, str]] = [
    ("tools", "agent", "FA-04: the loop is above the tool machinery, not below it"),
    # Chapter 10 met this cycle first; it is not on that chapter's list
    # (F10-06 there is runaway recursion), so it carries interlude B's ID.
    ("tools", "subagent", "FB-01: putting the spawn handler with the handlers"),
    (
        "subagent",
        "tools",
        "FB-01: the arrow that made the line above impossible. Interlude B "
        "inverted it -- subagent takes `build_tools` from its caller",
    ),
    ("history", "agent", "F01-08: the first cycle in this project"),
    ("agent_types", "*", "F01-08: the shared-type module only works while it is a leaf"),
]

# Where an `Agent` may be constructed.  One place, because two places passed
# different subsets of the same ten arguments for a whole chapter and no test
# compared them (interlude B).
AGENT_CONSTRUCTION_SITES = {"agent.py"}


def intra_package_imports(path: Path) -> set[str]:
    """Every module inside the package that this file imports, at any depth.

    `ast.walk` rather than a scan of the top-level body, because an import
    written inside a function is still an edge -- and deferring an import is
    the standard trick for making a cycle stop failing at import time and
    start failing on the first call.  A checker that only reads the top of the
    file rewards exactly the repair that hides the problem.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(a.name for a in node.names if a.name.split(".")[0] == PACKAGE)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            if node.module.split(".")[0] != PACKAGE:
                continue
            # `from minicodex import compaction_prompt` is an edge to the
            # *package*, whose __init__ has to finish running first.  The first
            # version of this function dropped that case because the name is
            # not a module -- and reported "no cycles" for a package that could
            # not be imported at all.
            found.add(PACKAGE if node.module == PACKAGE else node.module)
    return found


def build_graph(pkg: Path) -> dict[str, set[str]]:
    graph: dict[str, set[str]] = {}
    for path in sorted(pkg.glob("*.py")):
        name = PACKAGE if path.stem == "__init__" else f"{PACKAGE}.{path.stem}"
        graph[name] = intra_package_imports(path)
    known = set(graph)
    return {k: {v for v in vs if v in known and v != k} for k, vs in graph.items()}


def find_cycles(graph: dict[str, set[str]]) -> list[list[str]]:
    """Every strongly connected component with more than one module in it."""
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


def check_cycles(pkg: Path) -> list[str]:
    cycles = find_cycles(build_graph(pkg))
    return [f"import cycle: {' -> '.join(c)}" for c in cycles]


def check_forbidden(pkg: Path) -> list[str]:
    problems = []
    for path in sorted(pkg.glob("*.py")):
        stem = PACKAGE if path.stem == "__init__" else path.stem
        imports = intra_package_imports(path)
        for source, target, why in FORBIDDEN:
            if source != stem:
                continue
            if target == "*":
                ours = sorted(imports)
                if ours:
                    problems.append(
                        f"{stem} must import nothing of ours, but imports {ours} ({why})"
                    )
            elif f"{PACKAGE}.{target}" in imports:
                problems.append(f"{stem} must not import {target} ({why})")
    return problems


def check_init_is_a_leaf(pkg: Path) -> list[str]:
    """`__init__.py` importing a submodule is the cheapest cycle in Python.

    One ordinary line -- `from minicodex.agent import Agent`, so that callers
    can write `from minicodex import Agent` -- makes `import minicodex.tools`
    fail, because `compaction.py` does `from minicodex import compaction_prompt`
    and the package is still half-built at that moment.  Measured; the whole
    package stops importing.

    Checked separately from `check_cycles` even though the cycle check catches
    the same thing, because the *message* has to be different.  "import cycle:
    minicodex -> minicodex.agent -> minicodex.compaction" is true and does not
    tell you that the fix is to delete one convenience import from a file
    nobody thinks of as code.
    """
    ours = sorted(intra_package_imports(pkg / "__init__.py"))
    if not ours:
        return []
    return [
        f"__init__.py imports {ours} from its own package. Anything doing "
        f"`from {PACKAGE} import <name>` now depends on all of it, and the "
        f"package stops importing (FB-01)."
    ]


def check_one_agent_construction(pkg: Path) -> list[str]:
    problems = []
    for path in sorted(pkg.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        calls = sum(
            1
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "Agent"
        )
        if calls and path.name not in AGENT_CONSTRUCTION_SITES:
            problems.append(
                f"{path.name} constructs an Agent. Build it through `Wiring.agent` "
                f"instead: a second construction site is how a child ended up "
                f"without a recorder, without compaction and without the "
                f"scheduler, silently, for a chapter (FB-01)."
            )
    return problems


def check_every_module_imports(pkg: Path) -> list[str]:
    """Import each module in a fresh interpreter, one subprocess per module.

    Not redundant with the cycle check, and the reason is measured: with
    `tools` and `subagent` importing each other, `python -c "import minicodex"`
    **exits 0**.  The package's `__init__` is a leaf, so the top-level import
    touches none of the broken part.  A smoke test that imports the package is
    the obvious CI check and it passes on a package that cannot be used.

    One subprocess per module, because once a module is in `sys.modules` the
    cycle is hidden from everything that runs afterwards -- which is also why
    the test suite stays green in a shell where the package has already been
    imported once.
    """
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

    # Grouped by message, because a cycle through `__init__` fails *every*
    # module with the same line: the first version of this printed twenty-five
    # identical paragraphs, which is a report nobody reads to the end.
    problems = []
    for message, modules in failures.items():
        if len(modules) > 3:
            problems.append(f"{len(modules)} modules do not import on their own: {message}")
        else:
            problems.extend(f"{m} does not import on its own: {message}" for m in modules)
    return problems


def _env() -> dict[str, str]:
    import os

    keep = ("PATH", "SYSTEMROOT", "TEMP", "TMP", "HOME", "USERPROFILE", "APPDATA", "PATHEXT")
    return {k: v for k, v in os.environ.items() if k in keep}


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


# ---------------------------------------------------------------------------
# Rules not written, and why
# ---------------------------------------------------------------------------
#
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

if __name__ == "__main__":
    raise SystemExit(main())
```

从上往下读：

> - **`FORBIDDEN`**：五条被禁止的箭头，**每一条都带着它是从哪次故障来的**。`("agent_types", "*", ...)` 的意思是：`agent_types` 不许导入包里任何模块——它只有保持是叶子才有用。
>   - 第二条的注释是改写时加的：这条箭头的理由原来标着第 10 章清单上的一个编号，而那个编号说的是另一件事（无限递归）。**一条没法查证来历的规则，和没有来历一样。**
> - **`AGENT_CONSTRUCTION_SITES`**：允许出现 `Agent(...)` 的文件。只有 `agent.py`。
> - **`intra_package_imports(path)`**：读一个文件，找出它导入了包里的哪些模块。两处要紧的细节，各对应一个真出过的问题：
>   - 用 `ast.walk` 走遍**整棵树**，而不是只看文件最外层——写在函数里面的 import 也是一条箭头（§6 的办法 c）。只看最外层的检查器，奖励的正是那种把问题藏起来的修法。
>   - `from minicodex import compaction_prompt` 算作一条指向**包本身**的箭头（§4.1 的那个 bug）。
> - **`build_graph(pkg)`**：每个模块 → 它导入的模块们。
> - **`find_cycles(graph)`**：找出所有"互相都能走到对方"的模块组，多于一个模块的就是环。做法是把图走两遍：第一遍记下每个模块"走完"的先后顺序，第二遍把箭头全部反过来、按那个顺序的倒序再走，每次能走到的就是一组。
>   你不需要记住这个算法；需要知道的是它被 §4 和 §5 的两个真的环检验过。
> - **五个检查**，每个返回一串"问题"（空的就是没问题）：
>   1. **`check_cycles`**：没有环。
>   2. **`check_init_is_a_leaf`**：`__init__.py` 不导入包里的任何模块。它抓的东西第一条也抓得到，单独列出来是为了**报错的那句话**："import cycle: minicodex -> agent -> compaction"是真话，
>      但不会让你想到"该删的是 `__init__.py` 里那一行"。
>   3. **`check_forbidden`**：那五条箭头。
>   4. **`check_one_agent_construction`**：`Agent(...)` 只出现在允许的文件里。报错里直接写着该怎么办（用 `Wiring.agent`）和为什么。
>   5. **`check_every_module_imports`**：每个模块各开一个新的 Python 进程单独导入（§5.1）。一个进程一个模块，是因为模块一旦被导入过，环就被藏起来了——
>      这也是为什么在一个已经导入过这个包的环境里，测试会一直是绿的。同样的报错会合并成一行：第一版在 `__init__` 的环上印了二十五段一模一样的话。
> - **`main()`**：全部通过就退出 0；否则在 stderr 上逐条说出是哪条规则、哪个模块、为什么，退出 1。
> - **文件最后那段注释是"没有写的规则"**，和写了的同样重要：
>   - **给每个模块标一个层号**：设计了，扔了。它能抓到上面那张表抓到的一切，还能抓到更多——但"更多"是假想的，而每个模块都要维护一条记录是确定的。到现在为止真的发生过的反向箭头，全都在那张表里。
>   - **限制一个文件的行数**：插曲 A 量过，真正出问题的重复，是散在三个都很小的文件里的。行数量的是体积，不是纠缠。
>   - **限制一个模块被多少模块导入**：`agent_types` 被八个模块导入，而那正是它的**用处**。

它现在说的话：

```
$ uv run python scripts/check_layers.py
ok  no import cycles
ok  __init__ is a leaf
ok  forbidden edges
ok  one Agent construction site
ok  every module imports alone
```

### 14.1 为什么是脚本，不是 pytest 里的测试

因为第五个检查。pytest 在跑任何测试之前，已经把这个包导入了一遍——在那个进程里，"每个模块能不能单独导入"这个问题已经没法问了。
（它当然也**被**测试调用：下面的测试把它当成一个库来用，对着一份份故意弄坏的拷贝。）

### 14.2 每条规则都得被看到失败过

```python
def test_FB_02_the_checker_passes_on_this_package() -> None:
    result = subprocess.run(
        [sys.executable, str(CHECKER), "--quiet"], capture_output=True, text=True
    )
    assert result.returncode == 0, result.stderr


def test_FB_02_a_convenience_import_in_init_is_caught() -> None:
    """The measured failure, in one line nobody would flag in review.

    `from minicodex import Agent` is what every Python package grows so that
    callers do not have to know which module a name lives in. Adding it here
    makes `import minicodex.tools` raise, because `compaction.py` does
    `from minicodex import compaction_prompt` while the package is still
    half-built.
    """
    with tempfile.TemporaryDirectory() as tmp:
        broken = a_copy_of_src(
            Path(tmp),
            (
                "__init__.py",
                "from pathlib import Path",
                "from pathlib import Path\n\nfrom minicodex.agent import Agent",
            ),
        )
        problems = checker.check_init_is_a_leaf(broken)
        assert problems and "__init__.py imports" in problems[0]
        assert checker.check_cycles(broken), "the cycle check must see it too"


def test_FB_02_a_package_smoke_test_would_not_have_caught_the_other_one() -> None:
    """Why the checker imports every module and not just the package.

    With `tools` and `subagent` importing each other, `import minicodex`
    **exits 0** -- the package's `__init__` is a leaf, so the top-level import
    never touches the broken part. The obvious CI check passes on a package
    that cannot be used.
    """
    with tempfile.TemporaryDirectory() as tmp:
        broken = a_copy_of_src(Path(tmp), *BOTH_ARROWS)
        env = {**checker._env(), "PYTHONPATH": str(broken.parent)}
        package = subprocess.run(
            [sys.executable, "-c", "import minicodex"],
            capture_output=True,
            text=True,
            env=env,
        )
        assert package.returncode == 0, "the smoke test is supposed to pass here"

        problems = checker.check_every_module_imports(broken)
        assert problems, "the per-module check must not"
        assert any("minicodex.tools" in p for p in problems)


def test_FB_02_a_deferred_import_is_still_an_edge() -> None:
    """The repair that makes a cycle stop failing and start lurking.

    Moving `from minicodex.subagent import run_task` inside a function body
    makes the package import cleanly again. The cycle is still there; it now
    fails on the first call instead of at startup, which is later, in front of
    a user, and in a stack trace about something else.
    """
    with tempfile.TemporaryDirectory() as tmp:
        deferred = a_copy_of_src(
            Path(tmp),
            (
                "tools.py",
                "def bind_all(",
                "def _late() -> None:\n"
                "    from minicodex.subagent import run_task  # noqa: F401\n\n\n"
                "def bind_all(",
            ),
            BOTH_ARROWS[1],
        )
        assert not checker.check_every_module_imports(deferred), "it imports fine, that is the trap"
        assert checker.check_cycles(deferred), "the AST check must still see the edge"


@pytest.mark.parametrize(
    "filename,old,new,rule",
    [
        (
            "subagent.py",
            "from minicodex.agent import Model, Wiring",
            "from minicodex.agent import Model, Wiring\nfrom minicodex.tools import bind_all"
            "  # noqa: F401",
            "check_forbidden",
        ),
        (
            "agent_types.py",
            "from collections.abc import Awaitable, Callable",
            "from collections.abc import Awaitable, Callable\n"
            "from minicodex.tool_errors import tool_error  # noqa: F401",
            "check_forbidden",
        ),
        (
            "subagent.py",
            "child = ctx.wiring.agent(",
            "from minicodex.agent import Agent\n\n    child = Agent(",
            "check_one_agent_construction",
        ),
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


def test_FB_02_every_forbidden_edge_names_the_fault_it_came_from() -> None:
    """The rule that keeps the rule list honest.

    An architecture check accumulates entries, and an entry with no reason is
    an entry nobody can ever delete -- it will be obeyed forever by people
    guessing at what it was for. Every edge in `FORBIDDEN` carries a fault ID
    from FAULTS.md, so each one can be looked up, and argued with.
    """
    for source, target, why in checker.FORBIDDEN:
        assert any(marker in why for marker in ("F0", "F1", "FA-", "FB-")), (source, target, why)


def test_FB_02_the_checker_runs_in_ci() -> None:
    """A check that is not wired in is a file.

    In the lint step rather than a step of its own: chapter -1's guard caps
    the blocking workflow at six steps, and a sub-second static check is what
    a lint step is for. Chapter 9 answered the same question differently
    (a second workflow) because a mutation run is not a lint.
    """
    workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    assert "scripts/check_layers.py" in workflow
```

> - 第二个：§4 那一行加进 `__init__.py` 的拷贝，"叶子"检查和"环"检查**都**要看见。
> - 第三个：§5 的两条箭头都加上，`import minicodex` 的退出码**是 0**（测试断言了这一点——这个冒烟测试"本来就该在这里通过"），而逐个模块导入的检查必须失败。
> - 第四个：§6 的办法 (c)。逐个导入的检查说"没问题"（这就是陷阱），环检查必须仍然看见。
> - 第五个用 `@pytest.mark.parametrize` 跑三次，每次在拷贝里违反一条规则，对应的检查必须报出问题。
>   **一个默认就说"ok"的检查器，和一个名字说得比断言多的测试，是同一种东西**——所以每条规则都配一次"故意弄坏，看它红"。
> - 第六个：`FORBIDDEN` 里每一条的理由都带着一个故障编号。没有理由的规则，是一条永远没人敢删的规则——以后的人只能猜它是干什么的，然后一直遵守下去。
> - 第七个：检查器接进了 CI。**一个没有接上的检查，只是一个文件。**

---

## §15 放进 CI 的哪一步

第 -1 章给 CI 定过一个上限：会挡住合并的那个流程最多六步。第 9 章想加第七步时撞上过它，当时的答案是另开一个不挡合并的流程——因为变异检查量的是测试的质量，不是这次改动对不对。

这一次同一个问题，**答案不一样**：边界检查查的正是"这次改动有没有把结构弄坏"，它该挡住合并。而它是一个几秒钟的静态检查——这本来就是 lint 那一步的定义。
所以它进了已有的 lint 步骤，没有占一个新的。`.github/workflows/ci.yml`：

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

> 同一道护栏拦了两次，两次的答案不同，两次都是因为先回答了"这一步凭什么挡住合并"。**新开一步，是一种不回答这个问题的办法。**

```bash
git add scripts/check_layers.py .github/workflows/ci.yml tests/test_faults_chB.py
git commit -m "ci: check import direction and the single Agent construction site on every pull request"
```

---

## §16 逐条验证

### 16.1 整份探针

```python
"""Interlude B: measuring the shape of the package before changing it.

Six sections, each runnable on its own:

    uv run python probe_boundaries.py graph        the import graph as it stands
    uv run python probe_boundaries.py init-cycle   one ordinary line in __init__.py
    uv run python probe_boundaries.py tool-cycle   the cycle chapter 10 stepped around
    uv run python probe_boundaries.py repairs      four repairs, each tried
    uv run python probe_boundaries.py drift        how many places build an Agent
    uv run python probe_boundaries.py drift-live   what the wiring is worth, measured

Sections that edit source do it in a *copy* of `src/` inside a temporary
directory, and run the experiment in a subprocess pointed at that copy.
Chapter 6 paid for the version that edited the working tree and was killed
before it put it back.
"""

from __future__ import annotations

import ast
import shutil
import subprocess
import sys
import tempfile
from collections import defaultdict
from pathlib import Path

HERE = Path(__file__).resolve().parent
SRC = HERE / "src"
PKG = SRC / "minicodex"


# ---------------------------------------------------------------------------
# the graph
# ---------------------------------------------------------------------------


def imports_of(path: Path, package: str = "minicodex") -> set[str]:
    """Every intra-package module this file imports, at any nesting level.

    `ast.walk`, not a scan of the top-level body: an import written inside a
    function is still an edge.  It is a *deferred* edge, which is exactly why
    it is worth seeing -- deferring an import is the standard way to make a
    cycle stop failing at import time and start failing on the first call.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(a.name for a in node.names if a.name.split(".")[0] == package)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            if node.module.split(".")[0] != package:
                continue
            if node.module == package:
                # `from minicodex import compaction_prompt` -- the name may be
                # a submodule or an attribute of the package.  Either way the
                # edge is to the package, whose __init__ has to run first.
                found.add(package)
            else:
                found.add(node.module)
    return found


def build_graph(pkg: Path = PKG) -> dict[str, set[str]]:
    graph: dict[str, set[str]] = {}
    for path in sorted(pkg.glob("*.py")):
        name = "minicodex" if path.stem == "__init__" else f"minicodex.{path.stem}"
        graph[name] = imports_of(path)
    known = set(graph)
    return {k: {v for v in vs if v in known and v != k} for k, vs in graph.items()}


def cycles(graph: dict[str, set[str]]) -> list[list[str]]:
    """Tarjan, iterative -- the recursive version dies on a package this size
    only if the graph is deep, but a cycle finder that can itself blow up is
    not the tool you want when the graph is already wrong."""
    index: dict[str, int] = {}
    low: dict[str, int] = {}
    on_stack: set[str] = set()
    stack: list[str] = []
    out: list[list[str]] = []
    counter = 0

    for root in graph:
        if root in index:
            continue
        work: list[tuple[str, list[str]]] = [(root, list(graph[root]))]
        index[root] = low[root] = counter
        counter += 1
        stack.append(root)
        on_stack.add(root)
        while work:
            node, pending = work[-1]
            if pending:
                child = pending.pop()
                if child not in index:
                    index[child] = low[child] = counter
                    counter += 1
                    stack.append(child)
                    on_stack.add(child)
                    work.append((child, list(graph[child])))
                elif child in on_stack:
                    low[node] = min(low[node], index[child])
            else:
                work.pop()
                if work:
                    low[work[-1][0]] = min(low[work[-1][0]], low[node])
                if low[node] == index[node]:
                    comp = []
                    while True:
                        top = stack.pop()
                        on_stack.discard(top)
                        comp.append(top)
                        if top == node:
                            break
                    if len(comp) > 1 or node in graph[node]:
                        out.append(sorted(comp))
    return out


def levels(graph: dict[str, set[str]]) -> dict[str, int]:
    depth: dict[str, int] = {}

    def walk(node: str, seen: frozenset[str]) -> int:
        if node in depth:
            return depth[node]
        if node in seen:
            return 0
        d = max((1 + walk(c, seen | {node}) for c in graph[node]), default=0)
        depth[node] = d
        return d

    return {n: walk(n, frozenset()) for n in graph}


def section_graph() -> None:
    graph = build_graph()
    fan_in: dict[str, int] = defaultdict(int)
    for deps in graph.values():
        for dep in deps:
            fan_in[dep] += 1
    depth = levels(graph)
    print(f"{len(graph)} modules, {sum(len(v) for v in graph.values())} edges")
    print(f"cycles: {cycles(graph) or 'none'}")
    print()
    print(f"{'level':>5}  {'module':18} {'out':>4} {'in':>4}")
    for name in sorted(graph, key=lambda n: (depth[n], n)):
        short = name.split(".")[-1] if "." in name else "__init__"
        print(f"{depth[name]:>5}  {short:18} {len(graph[name]):>4} {fan_in[name]:>4}")


# ---------------------------------------------------------------------------
# experiments that need an edited copy of the source
# ---------------------------------------------------------------------------


def in_a_copy(edits: dict[str, tuple[str, str]], command: str) -> subprocess.CompletedProcess[str]:
    """Copy `src/`, apply `{filename: (old, new)}`, run `python -c command`."""
    with tempfile.TemporaryDirectory() as tmp:
        copy = Path(tmp) / "src"
        shutil.copytree(SRC, copy)
        for filename, (old, new) in edits.items():
            path = copy / "minicodex" / filename
            text = path.read_text(encoding="utf-8")
            assert old in text, f"{filename}: pattern not found -- {old[:60]!r}"
            path.write_text(text.replace(old, new, 1), encoding="utf-8")
        return subprocess.run(
            [sys.executable, "-c", command],
            capture_output=True,
            text=True,
            cwd=tmp,
            env={**_clean_env(), "PYTHONPATH": str(copy)},
        )


def _clean_env() -> dict[str, str]:
    import os

    keep = ("PATH", "SYSTEMROOT", "TEMP", "TMP", "HOME", "USERPROFILE", "APPDATA")
    return {k: v for k, v in os.environ.items() if k in keep}


def _report(label: str, result: subprocess.CompletedProcess[str]) -> None:
    print(f"--- {label}")
    print(f"    exit {result.returncode}")
    tail = (result.stderr or result.stdout).strip().splitlines()
    for line in tail[-6:]:
        print(f"    {line}")
    print()


# ---------------------------------------------------------------------------
# 2 -- one ordinary line in __init__.py
# ---------------------------------------------------------------------------

NICE_API = (
    "from pathlib import Path",
    "from pathlib import Path\n\nfrom minicodex.agent import Agent",
)


def section_init_cycle() -> None:
    print("Adding `from minicodex.agent import Agent` to __init__.py -- the line")
    print("every package grows when someone wants `from minicodex import Agent`.")
    print()
    for entry in ("import minicodex", "import minicodex.compaction", "import minicodex.tools"):
        _report(entry, in_a_copy({"__init__.py": NICE_API}, entry))

    with tempfile.TemporaryDirectory() as tmp:
        copy = Path(tmp) / "src"
        shutil.copytree(SRC, copy)
        path = copy / "minicodex" / "__init__.py"
        path.write_text(path.read_text(encoding="utf-8").replace(*NICE_API), encoding="utf-8")
        graph = build_graph(copy / "minicodex")
        print(f"and the graph checker on that same tree says: cycles = {cycles(graph) or 'none'}")


# ---------------------------------------------------------------------------
# 3 -- the cycle chapter 10 stepped around
# ---------------------------------------------------------------------------

# Interlude B removed one of the two arrows, so reproducing chapter 10's cycle
# now means putting it back.  Both edits together are the state the package was
# in before this unit: `subagent` needing `bind_all` to build a child's tool
# table, `tools` needing `run_task` so that the spawn handler can live with the
# other handlers.  Either arrow alone is fine.
SUBAGENT_NEEDS_TOOLS = (
    "subagent.py",
    (
        "from minicodex.agent import Model, Wiring",
        "from minicodex.agent import Model, Wiring\nfrom minicodex.tools import bind_all",
    ),
)
TOOLS_NEEDS_SUBAGENT = (
    "tools.py",
    (
        "from minicodex.agent_types import ToolCall, ToolFn",
        "from minicodex.agent_types import ToolCall, ToolFn\n"
        "from minicodex.subagent import run_task",
    ),
)
BOTH_ARROWS = dict([SUBAGENT_NEEDS_TOOLS, TOOLS_NEEDS_SUBAGENT])


def section_tool_cycle() -> None:
    print("Putting the spawn_agent handler where handlers live, in the package as")
    print("chapter 10 left it: `tools.py` gains `from minicodex.subagent import")
    print("run_task`, and `subagent.py` still imports `tools` for bind_all.")
    print()
    for entry in ("import minicodex.tools", "import minicodex.subagent", "import minicodex"):
        _report(entry, in_a_copy(BOTH_ARROWS, entry))
    print("The third line is the one to look at. `import minicodex` exits 0 on a")
    print("package whose modules cannot import each other, because __init__ is a")
    print("leaf -- so the obvious CI smoke test passes on a broken tree.")


# ---------------------------------------------------------------------------
# 4 -- four repairs
# ---------------------------------------------------------------------------

DEFERRED = dict(
    [
        SUBAGENT_NEEDS_TOOLS,
        (
            "tools.py",
            (
                "from minicodex.agent_types import ToolCall, ToolFn",
                "from minicodex.agent_types import ToolCall, ToolFn\n\n\n"
                "def _run_task(*args: object, **kwargs: object) -> object:\n"
                "    from minicodex.subagent import run_task\n\n"
                "    return run_task(*args, **kwargs)",
            ),
        ),
    ]
)


def _cycles_after(edits: dict[str, tuple[str, str]]) -> object:
    with tempfile.TemporaryDirectory() as tmp:
        copy = Path(tmp) / "src"
        shutil.copytree(SRC, copy)
        for filename, (old, new) in edits.items():
            path = copy / "minicodex" / filename
            path.write_text(path.read_text(encoding="utf-8").replace(old, new, 1), encoding="utf-8")
        return cycles(build_graph(copy / "minicodex")) or "none"


def section_repairs() -> None:
    print("(a) move the shared *type* down -- chapter 1's answer for ToolCall and")
    print("    interlude A's for ToolFn. What tools.py needs from subagent.py is")
    print("    `run_task`: a coroutine function that builds an Agent, runs it and")
    print("    interprets the result. Moving it down moves the module.")
    print()

    print("(b) move the *tool* up -- chapter 10's answer, into __main__. Works.")
    print("    Price measured in `drift`: a second place that builds an agent.")
    print()

    print("(c) defer the import into a function body:")
    _report(
        "import minicodex.tools",
        in_a_copy(DEFERRED, "import minicodex.tools; print('imported fine')"),
    )
    print(f"    and the AST checker still sees the edge: cycles = {_cycles_after(DEFERRED)}")
    print("    The cycle did not go away. It moved from startup to the first call.")
    print()

    print("(d) invert: subagent takes `build_tools` from its caller. What the two")
    print("    modules had to exchange is a *value* -- three tables that must")
    print("    agree -- so it went into agent_types.py with the other shared")
    print("    vocabulary, and the arrow disappeared. This is what shipped.")
    only_one = _cycles_after(dict([TOOLS_NEEDS_SUBAGENT]))
    print(f"    cycles with only the tools -> subagent arrow added: {only_one}")
    print()


# ---------------------------------------------------------------------------
# 5 -- what the second assembly site forgets
# ---------------------------------------------------------------------------


def _calls_to(path: Path, name: str) -> int:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return sum(
        1
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == name
    )


def section_drift() -> None:
    """How many places build an `Agent`, and how much each of them knows.

    Before interlude B: two, passing seven and three of the same ten keyword
    arguments.  After: one, `Wiring.agent`, which both call.
    """
    import inspect

    sys.path.insert(0, str(SRC))
    from minicodex.agent import Agent, Wiring

    accepted = set(inspect.signature(Agent.__init__).parameters) - {"self", "model", "tools"}
    carried = {f for f in Wiring.__dataclass_fields__} | {"footprint_of"}
    passed_per_call = set(inspect.signature(Wiring.agent).parameters) - {"self", "model", "tools"}

    sites = {
        path.name: _calls_to(path, "Agent")
        for path in sorted(PKG.glob("*.py"))
        if _calls_to(path, "Agent")
    }
    print(f"Agent.__init__ takes {len(accepted)} arguments beyond model and tools.")
    print(f"`Agent(...)` call sites in src/: {sites}")
    print()
    print(f"  carried by Wiring / ToolSet ({len(carried)}): {', '.join(sorted(carried))}")
    print(f"  chosen per call ({len(passed_per_call)}):      {', '.join(sorted(passed_per_call))}")
    missing = accepted - carried - passed_per_call
    print(f"  in neither ({len(missing)}):                {', '.join(sorted(missing)) or '-'}")


# ---------------------------------------------------------------------------
# 6 -- the same drift, measured by running it instead of reading it
# ---------------------------------------------------------------------------


def section_drift_live() -> None:
    """The same sub-agent, run twice: with the parent's wiring and without it.

    Both arms go through the code as it stands after interlude B.  The first
    arm passes `Wiring()` -- an empty one, which is exactly what a child used
    to get, because nothing passed it anything.  The second passes the wiring
    a top-level run builds.  Same model script, same files, same tools.
    """
    import asyncio
    import json
    import time
    from typing import Any

    sys.path.insert(0, str(SRC))
    from minicodex.agent import Wiring
    from minicodex.approval import AllowAll, Session
    from minicodex.composition import sub_context
    from minicodex.model import Completed, TextDelta, ToolCallDelta
    from minicodex.recorder import Recorder
    from minicodex.shell import ShellSession
    from minicodex.subagent import TaskSpec, run_task

    class Scripted:
        def __init__(self, turns: list[Any]) -> None:
            self.turns = turns
            self.sent: list[list[dict[str, Any]]] = []
            self.model = "scripted"

        async def stream(self, messages: list[dict[str, Any]]) -> Any:
            self.sent.append([dict(m) for m in messages])
            turn = self.turns[min(len(self.sent) - 1, len(self.turns) - 1)]
            if isinstance(turn, str):
                yield TextDelta(turn)
            else:
                for i, (cid, name, args) in enumerate(turn):
                    yield ToolCallDelta(call_id=cid, index=i, name=name, arguments=json.dumps(args))
            yield Completed("stop")

    async def summarise(_request: Any) -> str:
        return "## Done: read the file three times"

    script = [
        [("c1", "read_file", {"path": "big.txt"})],
        [("c2", "read_file", {"path": "big.txt"})],
        [("c3", "read_file", {"path": "big.txt"})],
        "done",
    ]

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "big.txt").write_text("x" * 60_000, encoding="utf-8")
        (root / "a.txt").write_text("a", encoding="utf-8")
        (root / "b.txt").write_text("b", encoding="utf-8")

        def run(label: str, wiring: Wiring, model: Any, task: str) -> Any:
            ctx = sub_context(
                build_model=lambda _schemas: model,
                root=root,
                session=Session(mode="workspace-write", approver=AllowAll()),
                parent_shell=ShellSession(),
                wiring=wiring,
                max_turns=8,
            )
            return asyncio.run(run_task(TaskSpec(task, "ok"), ctx))

        print(f"{'arm':26} {'final request':>15} {'recorded':>10} {'peak':>6} {'wall':>7}")
        for label, wiring in (
            ("Wiring()  (the old child)", Wiring()),
            (
                "the parent's wiring",
                Wiring(
                    recorder=Recorder(root / "rec"),
                    context_window=32_000,
                    summariser=summarise,
                ),
            ),
        ):
            model = Scripted(script)
            run(label, wiring, model, "read it three times")
            chars = sum(len(json.dumps(m)) for m in model.sent[-1])
            path = getattr(wiring.recorder, "path", None)
            recorded = "read it" in path.read_text(encoding="utf-8") if path is not None else False

            # two independent reads in one turn, timed
            overlap = {"peak": 0, "now": 0}
            import minicodex.tools as tools_mod

            original = tools_mod.read_file

            async def slow(
                root_: Path,
                args: dict[str, Any],
                _o: Any = original,
                _seen: dict[str, int] = overlap,
            ) -> str:
                _seen["now"] += 1
                _seen["peak"] = max(_seen["peak"], _seen["now"])
                await asyncio.sleep(0.2)
                try:
                    return await _o(root_, args)
                finally:
                    _seen["now"] -= 1

            tools_mod.read_file = slow
            try:
                began = time.monotonic()
                run(
                    label,
                    wiring,
                    Scripted(
                        [
                            [
                                ("c1", "read_file", {"path": "a.txt"}),
                                ("c2", "read_file", {"path": "b.txt"}),
                            ],
                            "done",
                        ]
                    ),
                    "read both",
                )
                seconds = time.monotonic() - began
            finally:
                tools_mod.read_file = original

            print(f"{label:26} {chars:>15,} {recorded!s:>10} {overlap['peak']:>6} {seconds:>6.2f}s")

        print()
        print("Both arms schedule the two reads together (peak 2, one 0.2s wait rather")
        print("than two): `footprint_of` travels with the ToolSet, not with the Wiring,")
        print("so that fix is not one a caller can decline. Before interlude B the same")
        print("run measured peak 1 and 0.42s.")


SECTIONS = {
    "graph": section_graph,
    "init-cycle": section_init_cycle,
    "tool-cycle": section_tool_cycle,
    "repairs": section_repairs,
    "drift": section_drift,
    "drift-live": section_drift_live,
}


if __name__ == "__main__":
    names = sys.argv[1:] or list(SECTIONS)
    for name in names:
        print(f"===== {name} " + "=" * (60 - len(name)))
        SECTIONS[name]()
        print()
```

> - **`imports_of`、`build_graph`**：和检查器里的是同一个想法（探针先写，检查器后写）。**`cycles`**：另一种找环的写法，不用递归——注释说了原因：图已经不对劲的时候，你不想要一个自己也可能崩掉的找环工具。
>   **`levels`**：每个模块在第几层。**`section_graph`**：§3 的那张表。
> - **`in_a_copy(edits, command)`**：复制 `src/`，在拷贝里改，另开一个进程执行 `command`，环境变量只留最少的几个。**`_report`**：打印退出码和最后几行。
> - **`section_init_cycle`**（§4）、**`section_tool_cycle`**（§5）、**`section_repairs`**（§6）。
> - **`section_drift`**：数 `Agent.__init__` 收几个参数，其中几个由 `Wiring`/`ToolSet` 带着、几个每次调用时给、几个两边都不管（应该是 0）。
> - **`section_drift_live`**（§12 开头那张表）：同一个子 Agent 跑两遍，一遍给空的 `Wiring()`（也就是以前子 Agent 实际拿到的），一遍给父 Agent 的。

改完之后，这个包的图：

```
$ uv run python probe_boundaries.py graph
26 modules, 72 edges
cycles: none

level  module              out   in
    0  __init__              0    3
    0  agent_types           0    8
    ...
    3  agent                 8    3
    3  approval              5    5
    4  registry              8    2
    4  subagent              7    2
    4  tools                 7    2
    5  composition           7    1
    6  __main__             15    0
```

和 §3 比：`subagent` 从第 5 层回到了第 4 层，和 `tools`、`registry` 并排——三个互不导入的"工具来源"；它们上面多了一层 `composition`；`agent_types` 被 8 个模块导入。

```
$ uv run python probe_boundaries.py drift
Agent.__init__ takes 10 arguments beyond model and tools.
`Agent(...)` call sites in src/: {'agent.py': 1}

  carried by Wiring / ToolSet (6): context_window, dialect, footprint_of, max_concurrent_tools, recorder, summariser
  chosen per call (4):      instructions, max_turns, resume_from, rollout
  in neither (0):                -
```

十个参数：六个跟着 `Wiring` 或 `ToolSet` 走，四个每次调用时给，**没有一个两边都不管**。

### 16.2 全部测试，两个系统

```bash
uv run ruff check .
uv run ruff format --check .
uv run python scripts/check_layers.py
uv run pytest
```

Windows（2026-10-01）：

```
All checks passed!
65 files already formatted
ok  no import cycles
ok  __init__ is a leaf
ok  forbidden edges
ok  one Agent construction site
ok  every module imports alone
1441 passed, 9 skipped in 79.23s (0:01:19)
```

Linux（WSL Ubuntu，Python 3.12，同一天）：

```
1450 passed in 52.87s
```

### 16.3 这些测试自己靠得住吗

改写这一章时，在一份临时拷贝里做了 36 处"把这一章的一个决定撤掉"的修改，每处改完跑全部测试（Linux）。第一遍：

```
baseline: 0 failed
mutation                                                           caught  first test to notice
Wiring.agent drops the recorder                                       yes  test_FB_01_a_child_is_given_the_parents_rec
Wiring.agent drops the context window                                 yes  test_FB_01_a_child_compacts_like_its_parent
Wiring.agent drops the summariser                                     yes  test_FB_01_a_child_compacts_like_its_parent
Wiring.agent drops the footprints                                      NO
Wiring.agent drops the concurrency cap                                 NO
Wiring.agent drops the dialect                                         NO
ToolSet accepts a handler with no schema                              yes  test_FB_01_a_toolset_refuses_a_handler_with
ToolSet accepts a schema with no handler                              yes  test_FB_01_a_toolset_refuses_a_schema_with_
plus keeps one of two tools with the same name                        yes  test_FB_01_plus_refuses_a_duplicate_name
plus drops the other side's footprints                                yes  test_F02_08_a_timed_out_command_leaves_no_p
plus drops this side's footprints                                     yes  test_FB_01_a_child_gets_the_schedulers_foot
plus forgets which names may lack a schema                             NO
the child runs on an empty Wiring                                     yes  test_FB_01_a_child_is_given_the_parents_rec
the child's tools ignore the shell seeded from the parent             yes  test_F10_01_a_child_starts_where_the_parent
a spawn is classified as touching nothing                             yes  test_FB_01_plus_routes_a_footprint_to_the_s
local tools have no footprints                                        yes  test_FB_01_a_child_gets_the_schedulers_foot
the merged set copies the registry's list                             yes  test_the_cli_hands_the_model_client_the_reg
remote calls are classified by the local classifier                   yes  test_F09_remote_names_never_reach_the_local
tool_search has no handler when tools are deferred                     NO
deferred tools are not named as the exception                          NO
a registry built without local= is accepted                            NO
top level: no spawn_agent                                             yes  test_the_cli_wires_a_sub_agent_to_the_run_i
top level: the parent's tools get a shell of their own                yes  test_the_cli_wires_a_sub_agent_to_the_run_i
sub_context drops the wiring                                          yes  test_FB_01_a_child_is_given_the_parents_rec
sub_context drops build_tools                                         yes  test_F10_01_a_child_starts_where_the_parent
cli: the summariser is attached after the handler is built            yes  test_the_cli_gives_a_sub_agent_the_same_wir
cli: the summariser's client carries the agent's tools                yes  test_the_summariser_is_given_a_client_with_
cli: no context window reaches the wiring                              NO
cli: no recorder reaches the wiring                                    NO
cli: remote tools are never merged                                    yes  test_the_cli_hands_the_model_client_the_reg
cli: the registry is built without the local schemas                  yes  test_the_cli_switches_the_scheduler_on
cli: children are not recorded to disk                                yes  test_the_cli_wires_a_sub_agent_to_the_run_i
cli: children do not name their parent                                yes  test_the_cli_wires_a_sub_agent_to_the_run_i
cli: sub-agent sessions are not listed at the end                     yes  test_the_cli_lists_each_sub_agent_when_the_
cli: `sessions` does not say which are sub-agents                     yes  test_the_sessions_listing_says_which_ones_a
cli: the child is seeded from a shell that is not the parent's         NO
26/36 caught
  survived: Wiring.agent drops the footprints
  survived: Wiring.agent drops the concurrency cap
  survived: Wiring.agent drops the dialect
  survived: plus forgets which names may lack a schema
  survived: tool_search has no handler when tools are deferred
  survived: deferred tools are not named as the exception
  survived: a registry built without local= is accepted
  survived: cli: no context window reaches the wiring
  survived: cli: no recorder reaches the wiring
  survived: cli: the child is seeded from a shell that is not the parent's
tree green again: True
```

36 处里有 10 处，**全部测试照样是绿的**。逐个看：

- **`Wiring.agent` 里把 `footprint_of=tools.footprint_of` 那一行删掉——没有测试红。** 这是全章最要紧的一行之一：`ToolSet` 带着脚印函数，是为了让它到达 `Agent`；
  这一行没了，父 Agent 和每个子 Agent 都退回"一个一个跑"——正是第 10 章的子 Agent 的样子。并发上限和 `dialect` 那两行也一样，删了没人知道。
- **`with_remote_tools` 里给 `tool_search` 装处理函数的两行、把被藏起来的工具报成例外的那一行——都没有测试守着。**
  原因很具体：这个函数的每一个测试用的都是**空的**注册表。而配了六十个工具的时候，这两处任何一处没了，结果都是程序启动时直接报错。
- "注册表必须被告知本地工具"的那句前提、`plus` 合并例外名单的那一行、`__main__.py` 里把窗口大小和录制器放进 `Wiring` 的两行：同样。
- 还有一处**不算**：把 `parent_shell=context.shell` 换成一个新造的 shell，测试是绿的——因为父 Agent 的工具用的就是 `sub_ctx.parent_shell`，换哪个 shell，父子用的仍然是同一个。
  这种"改了但行为不变"的修改，不是测试的漏洞。

给它们各补了测试：

```python
def _two_slow_tools() -> tuple[ToolSet, dict[str, int]]:
    """Two tools that take a moment each and are declared to touch different
    things, plus a count of how many were running at once."""
    seen = {"now": 0, "peak": 0}

    async def slow(_args: dict[str, Any]) -> str:
        seen["now"] += 1
        seen["peak"] = max(seen["peak"], seen["now"])
        await asyncio.sleep(0.05)
        seen["now"] -= 1
        return "ok"

    def schema(name: str) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {"name": name, "description": "wait", "parameters": {"type": "object"}},
        }

    tools = ToolSet(
        handlers={"left": slow, "right": slow},
        schemas=[schema("left"), schema("right")],
        footprint_of=lambda call: Footprint(reads=frozenset({call.name})),
    )
    return tools, seen


def test_FB_01_wiring_passes_the_toolsets_footprints_to_the_agent() -> None:
    """`ToolSet` carrying a `footprint_of` is half of it. The other half is the
    one line in `Wiring.agent` that hands it to the `Agent` -- and deleting
    that line left every test green: both the parent and every child fell back
    to running one call at a time, which is what chapter 10's children did.
    Found by mutation.
    """
    tools, seen = _two_slow_tools()
    asyncio.run(Wiring().agent(ScriptedModel(BOTH_AT_ONCE), tools).run("go"))
    assert seen["peak"] == 2


def test_FB_01_wiring_passes_its_concurrency_cap_to_the_agent() -> None:
    tools, seen = _two_slow_tools()
    wiring = Wiring(max_concurrent_tools=1)
    asyncio.run(wiring.agent(ScriptedModel(BOTH_AT_ONCE), tools).run("go"))
    assert seen["peak"] == 1


def test_FB_01_wiring_passes_its_dialect_to_the_agent() -> None:
    """A child that renders its history for a different provider than its
    parent talks to is not a subtle failure, but it would be a late one."""
    tools, _seen = _two_slow_tools()
    agent = Wiring(dialect="ollama_native").agent(ScriptedModel(["ok"]), tools)
    assert agent.dialect == "ollama_native"


def test_FB_01_plus_remembers_which_names_may_lack_a_schema() -> None:
    """The named exception has to survive being combined, or the first `plus`
    after the registry's set is built turns it back into an error."""

    async def noop(_args: dict[str, Any]) -> str:
        return ""

    def schema(name: str) -> dict[str, Any]:
        return {"type": "function", "function": {"name": name, "parameters": {}}}

    with_hidden = ToolSet(
        handlers={"shown": noop, "hidden": noop},
        schemas=[schema("shown")],
        callable_without_schema=frozenset({"hidden"}),
    )
    other = ToolSet(handlers={"other": noop}, schemas=[schema("other")])

    combined = with_hidden.plus(other)
    assert combined.callable_without_schema == {"hidden"}


def test_FB_01_tools_deferred_behind_tool_search_survive_the_merge(tmp_path: Path) -> None:
    """Chapter 9's deferred mode, through the new assembly.

    Every other test of `with_remote_tools` uses an empty registry, so the two
    lines that only matter when tools are deferred -- the `tool_search`
    handler, and naming the deferred tools as the exception to `ToolSet`'s
    rule -- could each be deleted with the suite green. With sixty tools
    configured, either deletion is a program that refuses to start. Found by
    mutation.
    """
    from minicodex.composition import with_remote_tools
    from minicodex.mcp import RemoteTool
    from minicodex.registry import McpRegistry

    base = local_tools(tmp_path, Session(mode="read-only", approver=AllowAll()))
    registry = McpRegistry(local=base.schemas, schema_budget=0)  # nothing fits: defer all
    registry.add(None, [RemoteTool("notes", "search", "Search notes.", {"type": "object"})])  # type: ignore[arg-type]
    assert registry.deferred == {"mcp__notes__search"}

    combined = with_remote_tools(base, registry)

    shown = {schema["function"]["name"] for schema in combined.schemas}
    assert "tool_search" in combined.handlers and "tool_search" in shown
    assert "mcp__notes__search" in combined.handlers
    assert "mcp__notes__search" not in shown
    assert combined.schemas is registry.visible


def test_FB_01_a_registry_that_was_not_told_about_the_local_tools_is_refused(
    tmp_path: Path,
) -> None:
    """The ordering rule, in the rule's own words rather than `ToolSet`'s."""
    from minicodex.composition import with_remote_tools
    from minicodex.registry import McpRegistry

    base = local_tools(tmp_path, Session(mode="read-only", approver=AllowAll()))
    with pytest.raises(ValueError, match="local="):
        with_remote_tools(base, McpRegistry())
```

> - **`_two_slow_tools()`**：造两个各要等一小会儿的假工具，声明它们碰的是不同的东西，并数"同一时刻最多有几个在跑"。
> - 前三个测试用 `Wiring(...).agent(...)` 造一个 Agent，让模型在同一轮里调用这两个工具：默认的 `Wiring`，峰值是 2（脚印到了 Agent 手里）；上限设成 1，峰值是 1；`dialect` 原样到达。
> - 第五个：注册表的预算设成 0（什么都装不下，全部藏起来），加一个假的远程工具，然后合并。合并后：有 `tool_search` 的处理函数和说明；那个远程工具有处理函数、没有说明；
>   `schemas` **就是**注册表的那个列表。
> - 第六个：没被告知本地工具的注册表，被拒绝，报错里有 `local=`。
> - `__main__.py` 的那两行，在 §12.2 的第三个测试里各加了一句断言（窗口大小是命令行给的那个数；录制器不是那个"什么都不录"的空录制器）。

补完之后再跑一遍（去掉那处不算的）：

```
baseline: 0 failed
mutation                                                           caught  first test to notice
Wiring.agent drops the recorder                                       yes  test_FB_01_a_child_is_given_the_parents_rec
Wiring.agent drops the context window                                 yes  test_FB_01_a_child_compacts_like_its_parent
Wiring.agent drops the summariser                                     yes  test_FB_01_a_child_compacts_like_its_parent
Wiring.agent drops the footprints                                     yes  test_FB_01_wiring_passes_the_toolsets_footp
Wiring.agent drops the concurrency cap                                yes  test_FB_01_wiring_passes_its_concurrency_ca
Wiring.agent drops the dialect                                        yes  test_FB_01_wiring_passes_its_dialect_to_the
ToolSet accepts a handler with no schema                              yes  test_FB_01_a_toolset_refuses_a_handler_with
ToolSet accepts a schema with no handler                              yes  test_FB_01_a_toolset_refuses_a_schema_with_
plus keeps one of two tools with the same name                        yes  test_FB_01_plus_refuses_a_duplicate_name
plus drops the other side's footprints                                 NO
plus drops this side's footprints                                     yes  test_FB_01_a_child_gets_the_schedulers_foot
plus forgets which names may lack a schema                            yes  test_FB_01_plus_remembers_which_names_may_l
the child runs on an empty Wiring                                     yes  test_FB_01_a_child_is_given_the_parents_rec
the child's tools ignore the shell seeded from the parent             yes  test_F10_01_a_child_starts_where_the_parent
a spawn is classified as touching nothing                             yes  test_FB_01_plus_routes_a_footprint_to_the_s
local tools have no footprints                                        yes  test_FB_01_a_child_gets_the_schedulers_foot
the merged set copies the registry's list                             yes  test_FB_01_tools_deferred_behind_tool_searc
remote calls are classified by the local classifier                   yes  test_F09_remote_names_never_reach_the_local
tool_search has no handler when tools are deferred                    yes  test_FB_01_tools_deferred_behind_tool_searc
deferred tools are not named as the exception                         yes  test_FB_01_tools_deferred_behind_tool_searc
a registry built without local= is accepted                           yes  test_FB_01_a_registry_that_was_not_told_abo
top level: no spawn_agent                                             yes  test_the_cli_wires_a_sub_agent_to_the_run_i
top level: the parent's tools get a shell of their own                yes  test_the_cli_wires_a_sub_agent_to_the_run_i
sub_context drops the wiring                                          yes  test_FB_01_a_child_is_given_the_parents_rec
sub_context drops build_tools                                         yes  test_F10_01_a_child_starts_where_the_parent
cli: the summariser is attached after the handler is built            yes  test_the_cli_gives_a_sub_agent_the_same_wir
cli: the summariser's client carries the agent's tools                yes  test_the_summariser_is_given_a_client_with_
cli: no context window reaches the wiring                             yes  test_the_cli_gives_a_sub_agent_the_same_wir
cli: no recorder reaches the wiring                                   yes  test_the_cli_gives_a_sub_agent_the_same_wir
cli: remote tools are never merged                                    yes  test_the_cli_hands_the_model_client_the_reg
cli: the registry is built without the local schemas                  yes  test_the_cli_switches_the_scheduler_on
cli: children are not recorded to disk                                yes  test_the_cli_wires_a_sub_agent_to_the_run_i
cli: children do not name their parent                                yes  test_the_cli_wires_a_sub_agent_to_the_run_i
cli: sub-agent sessions are not listed at the end                     yes  test_the_cli_lists_each_sub_agent_when_the_
cli: `sessions` does not say which are sub-agents                     yes  test_the_sessions_listing_says_which_ones_a
34/35 caught
  survived: plus drops the other side's footprints
tree green again: True
```

35 处里抓到 34 处。剩下的那一处（`plus` 不问对方的脚印函数），**第一遍的表里写的是"抓到了"**——看它是被谁抓到的：
`test_F02_08_a_timed_out_command_leaves_no_process_behind`，第 2 章一个关于杀子进程的测试，和 `plus` 毫无关系。它那一次只是碰巧失败了。
这次变异跑的时候用了"遇到第一个失败就停"，于是谁先失败，功劳就记在谁头上。

真正的原因：守着分流的那个测试，合并的是本地工具和 `spawn_agent`，而 `spawn_agent` 的脚印是"和一切冲突"——**恰好也是 `plus` 对"不认识的名字"的回答**。删掉那一支，答案不变。

```python
def test_FB_01_plus_keeps_both_sides_footprints() -> None:
    """Both halves of the routing, with footprints that can be told apart.

    The test above combines local tools with `spawn_agent`, whose footprint is
    `STATEFUL` -- which is also what `plus` answers for a name it cannot place.
    So deleting the branch that asks the *other* set left it green. The first
    mutation run reported that deletion as caught; the test that went red was
    an unrelated one about killing a subprocess, failing by coincidence. Found
    on the second run.
    """

    async def noop(_args: dict[str, Any]) -> str:
        return ""

    def one(name: str) -> ToolSet:
        schema = {"type": "function", "function": {"name": name, "parameters": {}}}
        return ToolSet(
            handlers={name: noop},
            schemas=[schema],
            footprint_of=lambda _call: Footprint(reads=frozenset({f"owned by {name}"})),
        )

    combined = one("left").plus(one("right"))

    def reads(name: str) -> frozenset[str]:
        return combined.footprint_of(ToolCall("c", name, {}, "{}")).reads

    assert reads("left") == {"owned by left"}
    assert reads("right") == {"owned by right"}
    assert combined.footprint_of(ToolCall("c", "neither", {}, "{}")) == STATEFUL
```

> 两个各只有一个工具的 `ToolSet`，脚印**互相分得出来**（各自写着"是谁的"）。合并后，左边的名字得到左边的脚印，右边的得到右边的，都不是的才是"和一切冲突"。

单独对着这一处再跑一次：

```
FAILED tests/test_faults_chB.py::test_FB_01_plus_keeps_both_sides_footprints
```

35 处全部被抓到。

> 这一章的主题是"两个地方各传各的，没人比较"。而这一章自己最关键的那几行"把东西传过去"的代码，第一遍有十行没人守着。**变异测试问的正是这一章问的那个问题：这一行要是没了，谁会知道？**

第 9 章和第 10 章的变异脚本，对着这一章改过的代码也各跑了一遍——**先确认的是每一条还"改得上"**（第 9 章学到的：改不上的变异必须算作没抓到）：

```
probe_mutations_ch09.py   14 mutations   every mutation was caught.
probe_mutations_ch10.py   16 mutations   every mutation was caught.
```

（Linux。两份脚本都没有报 `could not apply`：它们要找的那些行，这一章都没有动。）

---

## §17 收工

### 17.1 这一章动了哪些文件

| 文件 | 新增还是改动 | 在哪一节 |
|---|---|---|
| `src/minicodex/agent_types.py` | 搬进 `Footprint` 等三样，新增 `ToolSet` | §8 |
| `src/minicodex/scheduler.py` | 类型搬走，名字仍可从这里导入 | §8 |
| `src/minicodex/agent.py` | 新增 `Wiring` | §9 |
| `src/minicodex/subagent.py` | 不再导入 `tools.py`；`build_tools`、`wiring`、`spawn_toolset` | §10 |
| `src/minicodex/composition.py` | 新增 | §11 |
| `src/minicodex/registry.py` | 删掉 `route_footprint` | §11 |
| `src/minicodex/__main__.py` | 改用 `composition` 和 `Wiring`；`Wiring` 一开始就造完整 | §11、§12 |
| `scripts/check_layers.py` | 新增 | §14 |
| `.github/workflows/ci.yml` | lint 步骤里加一行 | §15 |
| `tests/test_faults_chB.py` | 新增 | 分散在各节 |
| `tests/test_faults_ch10.py`、`tests/test_faults_ch09.py`、`tests/test_scheduler.py` | 跟着改 / 删去旧的命令行测试 | §10、§11、§12 |
| `probe_boundaries.py` | 新增 | §16 |

### 17.2 推送、PR

```bash
git push -u origin refactor/boundaries
```

PR 描述里要如实写的：

> - 没有新功能，没有行为上的有意改变——除了两处**修正**：子 Agent 现在会压缩、被录制、并行调度（§7、§10）；总结用的模型调用不再带着工具说明（§12.1）。
> - 这一章自己的修复有过一个版本在真的程序里没有生效（§12）。发现它的测试现在在。
> - 子 Agent 仍然没有 MCP 的工具，仍然一个一个跑（第 10 章的决定，没有动）。
> - 检查器只查**直接**导入的方向，不查"A 最终能不能走到 B"。
> - 没有给每个模块标层号，没有限制文件行数（§14 末尾写了为什么）。

### 17.3 自己审一遍

**1 · `Wiring` 和 `ToolSet` 为什么不合成一个"装 Agent 要的全部东西"？**
因为它们变的时机不一样：`Wiring` 一次运行只有一个，父子共用；`ToolSet` 父子各有各的（子 Agent 没有远程工具，最底层没有 `spawn_agent`）。合在一起，就得在"共用"和"各有各的"之间再切一刀。

**2 · `build_tools` 的默认值是"空的工具表"。忘了传，会不会悄悄得到一个什么都干不了的子 Agent？**
会——如果有人绕过 `composition.sub_context` 直接写 `SubAgentContext(...)`。程序里没有这样的地方，测试的帮手也改成了走 `sub_context`。
默认值不抛异常，是为了让只关心 `TaskSpec`、`TaskResult` 的测试不必造一整套工具。这是一个留着的口子，如实记下。

**3 · `with_remote_tools` 为什么不能就是 `plus`？**
§11 说了：注册表的说明列表必须保持是同一个对象。这是整个装配里唯一不对称的地方，所以它有名字、有一条明说的前提、有一个测试（用 `is`）。

**4 · 检查器会不会变成一张越来越长、没人敢动的清单？**
每一条都带着故障编号，并且有测试守着"必须带"。可以查来历的规则，是可以被争论、被删掉的规则。

**5 · `_cli_run` 把 `Agent.run` 换掉了，它测到的还是"真的程序"吗？**
它测到的是"真的程序把什么交给了 Agent"，没有测 Agent 拿到之后做什么——那一半由别的测试负责。两半之间如果有缝，§12 那样的问题就藏在缝里；所以 §16.3 用变异去找缝。

---

## §18 codex 是怎么做的

codex 有一个几乎同样形状的东西：一个 Python 脚本，检查"终端界面那个包不许直接依赖核心那个包"。它查两样——包的依赖声明，和源码里的导入写法——在一个专门做"仓库检查"的 CI 流程里，和另外两个同类的检查排在一起。

三件事和这一章一致：**是脚本，不是测试；查的是具体的一条箭头，不是一张分层表；失败时打印的是"该怎么办"。**

最值得看的是它报错的最后半句：实在还得用核心包里的东西时，走一个**有名字的模块**（名字里就带着"遗留"）。那个模块只做一件事：把核心包里的几样东西原样再导出一遍。
也就是说：**边界是强制的，但不是没有例外；例外全部走一扇有名字的门，于是它们数得出来**——在它的源码里搜那个名字，是一百来处。

这和 §8 的 `callable_without_schema` 是同一个手法，和第 2 章那个"只在 POSIX 上跑"的标记也是：

> **一条规则挡不住的现实，要么写成一个报了名字的例外，要么把规则删掉。最差的选择，是把规则放松到"技术上没有违反"。**

（它的检查同样只查**直接**依赖：核心包仍然在终端界面的间接依赖里。检查器查的是箭头，不是可达性——一个知道自己在量什么的检查。）

---

## §19 回头看：这一章撞到了什么

**预测到了，并且成立的：** FB-01（两个环，不是一个）、FB-02（第一个把边界弄坏的是检查器自己）。
**预测到了，量过之后躲开的：** FB-03（没有造那个接口；删掉了两个抽象）。

**没预测到的：**

| 故障 | 怎么发现的 | 挡住它的东西 |
|---|---|---|
| `__init__.py` 里一行很平常的导入，让三个互不相识的模块成环 | 🔴 探针 | "`__init__` 是叶子"这条检查 |
| 检查器的第一版对着一个导入不了的包说"没有环" | 🟣 拿它去查一个已知坏掉的拷贝 | 把指向包本身的 import 也算作箭头 |
| `import minicodex` 在模块互相导入不了时退出码是 0 | 🟠 探针 | 每个模块各开一个进程导入 |
| 把 import 挪进函数体：环还在，只是晚一点炸 | 🟠 探针 | 检查器读整棵语法树 |
| **躲开环的办法比环贵**：两处造 Agent，子 Agent 没有压缩、录制、并行 | 🟡 把两处放在一起跑 | `Wiring`，和"只有一处"的检查 |
| 第三张表（脚印）从来没有和另外两张捆在一起 | 🟠 同一次测量 | `ToolSet` |
| `ToolSet` 的规则第一次运行就拒绝了一个测试里的偷懒 | ⚪ | 测试补上说明 |
| 注册表必须在本地工具之后创建；弄反了，报错说的是别的事 | 🔴 弄反了一次 | 一句用这条规矩自己的话写的前提 |
| **这一章的修复在真的程序里没有生效**：`replace()` 造了第二个 `Wiring` | 🟣 改写时给命令行重写测试 | `Wiring` 一开始就造完整；一个用 `is` 的测试 |
| **总结用的模型调用一直带着工具说明**，而说明里写着"不能" | 🟣 修上一条时去读了那句说明 | 给它一个不带工具的客户端 |
| 第 8、9、10 章守命令行的三个测试，随着 `Agent(...)` 那一行一起被删了 | 🟣 改写时 | 七个对着新装配写的测试 |
| 检查器里一条规则标着另一件事的故障编号 | 🟣 改写时逐条查编号 | 改成对的 |
| **这一章自己的 36 处变异里，10 处没有测试会红**（包括把脚印交给 Agent 的那一行） | ⚪ 改写时做的变异 | 六个测试、两句断言 |

标记：🔴 崩溃 · 🟡 静默 · 🟢 边界测试 · 🔵 长跑 · 🟠 看日志 · 🟣 审查 · ⚪ 工具

---

## 如果你只记住三件事

1. **会喊的故障便宜，为了让它不喊而做的事才贵。**
   循环导入当场报错，一分钟就能看见。为了躲开它把工具"往上搬"，留下了第二个造 Agent 的地方——子 Agent 没有压缩、没有录制、没有并行，
   没有任何报错，`subagent.py` 单独读每一行都对。

2. **必须一致的东西，做成一个值，并且让不一致的造不出来。**
   三张表合成 `ToolSet`，造的时候就检查；父子都要的五样合成 `Wiring`，造 Agent 的地方只剩一处，由脚本数着。
   "记得传"变成了"没法不传"。

3. **修复要在它被用到的地方检查，而不是在它被定义的地方。**
   这一章的每个测试都自己造上下文，于是都看不见 `__main__.py` 里那个 `replace()` 造出了第二个对象。
   第 8、9、10 章各撞到过一次同样的形状；这一章在收拾这个形状的时候，自己又撞了一次。

---

## 动手练习

1. 在 `src/minicodex/__init__.py` 里加一行 `from minicodex.agent import Agent`，跑 `uv run python scripts/check_layers.py`。几条检查失败了？各说了什么？
   再跑 `uv run pytest tests/test_faults_ch10.py`——测试还能开始吗？改回去。
2. 在 `subagent.py` 里把 `ctx.wiring.agent(` 改成 `Wiring().agent(`。先猜哪几个测试会红，再跑。检查器会不会说话？为什么？
3. 把 `__main__.py` 里的 `summariser=make_summariser(make_model([])) if context_window else None,` 改回 `summariser=None,`，
   然后在 `llm = make_model(tools.schemas)` 后面加上 §12 里那三行 `replace`。跑测试，红的是哪一个？读它的断言：它为什么用 `is` 而不是 `==`？
4. 给 `FORBIDDEN` 加一条：`("clip", "*", "...")`，让 `clip.py` 也必须是叶子。理由那一栏你会写什么？（提示：那个守着"每条都要有故障编号"的测试会不会让你通过？它在逼你回答什么？）
5. §17.3 第 2 条留着的那个口子：把 `build_tools` 的默认值去掉，让它成为必填。哪些测试需要改？改完之后，§7 那类问题少了一条路，还是只是换了个地方？

下一章回到加功能：让 Agent 在动手之前先把一件大任务拆成几步，并且让用户看得见它拆成了什么。
