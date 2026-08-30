# 插曲 A · 第一次重构

> 这一章不加任何新功能。
>
> 它做三件事：判断哪里**该**重构（以及哪里不该），在动手之前把"现在的行为"
> 写下来，然后证明改完之后行为确实没变。
>
> 中间会撞上两个从第 2 章一路带到现在的 bug。其中一个，在这本书自己推荐的
> Python 版本上，是 100% 复现的死锁。

---

## §0 开始之前：现在的代码长什么样

前面四章的东西攒到现在，是这样一个包。**你不需要回头翻前面几章**，这张表就够
你跟上这一章了：

| 文件 | 干什么的 | 哪一章加的 |
|---|---|---|
| `agent.py` | 主循环：问模型 → 跑它要的工具 → 把结果喂回去 → 再问 | Ch00 |
| `agent_types.py` | `ToolCall`，被 agent 和 history 共用 | Ch01 |
| `history.py` | 对话历史。存"事实"而不是 JSON，拒绝进入非法状态 | Ch01 |
| `model.py` | HTTP 客户端，把两家供应商的差异挡在这里 | Ch01 |
| `recorder.py` | 把每次请求/响应落盘，调试用 | Ch-1 |
| `shell.py` | `run_shell` 工具：超时、截断、进程组 | Ch02 |
| `paths.py` | 路径解析与"不许跑出仓库"的校验 | Ch03 |
| `tool_errors.py` | 三段式错误信息：出了什么事 / 你发了什么 / 接下来该干嘛 | Ch03 |
| `patch.py` | `apply_patch` 的匹配与写盘 | Ch04 |
| `tools.py` | 工具的实现、工具表、给模型看的 schema | Ch00→Ch04 |
| `stub.py` | 录制下来的模型响应，可回放 | Ch00 |
| `__main__.py` | 命令行入口 | Ch-1 |

跑起来是这样：

```
$ cd steps/step04_apply_patch
$ uv run pytest
125 passed in 5.23s
```

记住 **125** 这个数。这一章会反复回到它。

> **本节确定了什么**：起点是 11 个模块、125 个测试、全绿。

---

## §1 开工前：这一章不加任何功能

先把纪律立在最前面，因为它是这一章唯一的硬约束：

> **纯重构 PR 里不许夹带功能改动。**

为什么这条值得单独说？因为它违反直觉。你重构一个函数的时候，会看到它旁边有个
小 bug；顺手改掉只要三行，不改反而要专门记一笔、开个 issue、以后再来一趟。
**顺手改看起来是效率，实际是把两件事搅在一起。**

搅在一起的代价，在 diff 出问题的时候才结账：

- 一个 400 行的 diff 里藏着 3 行行为改动，review 的人**看不见**——他的大脑已经
  切换到"这些都是等价变换"的模式了；
- 出了问题要回滚，你只能整个回滚，包括那 397 行本来没问题的搬迁；
- 一个月后 `git bisect` 定位到这个 commit，commit message 写的是
  "refactor: extract tool spec"，没有人会想到问题出在那 3 行。

所以这一章的规矩是：**看到的问题可以记下来，但不在这个 PR 里改。**
真的必须现在改的，**单独开一个 commit，排在重构前面**——这是第 4 章那条
"先修 bug 再加功能"的同一条规矩。这一章会真的用到两次。

> **本节确定了什么**：一条纪律。后面每一个决定都会回来对照它。

---

## §2 先量一量：到底该不该重构

写了五章了，加了三个工具，代码"应该"已经很乱了吧。

这是每个项目干到第五周时都会冒出来的念头。它通常是对的。但**"应该"不是证据**，
所以先量。最容易量的是行数：

```
$ wc -l src/minicodex/*.py | sort -rn
 1864 total
  273 src/minicodex/stub.py
  225 src/minicodex/tools.py
  215 src/minicodex/shell.py
  212 src/minicodex/agent.py
  191 src/minicodex/model.py
  189 src/minicodex/patch.py
  178 src/minicodex/history.py
   99 src/minicodex/recorder.py
   91 src/minicodex/paths.py
   90 src/minicodex/__main__.py
   45 src/minicodex/tool_errors.py
   31 src/minicodex/agent_types.py
   25 src/minicodex/__init__.py
```

**注意看最大的那个文件：273 行**，而且它是 `stub.py`——一堆录制下来的响应数据，
本来就该长。真正的逻辑文件最大 225 行。

按行数看，**没有任何一个文件到了该拆的程度**。

那这一章是不是就没事干了？先别急着下结论，但也**别急着找活干**。这里有个很常见
的陷阱：既然计划里写着"这周重构"，那就总得拆点什么出来——**于是拆一个本来不需要
拆的东西，制造出一个本来不存在的边界。**

行数是个非常诱人的指标，因为它好量。但它量的是**体积**，而重构真正要解决的是
**耦合**：

> 一个 800 行、但只有一处调用、依赖关系笔直向下的文件，
> 比两个各 100 行、互相 import 的文件安全得多。

前者你可以整块读完、整块替换、整块删掉；后者你动任何一边都要同时想着另一边，
而且加个第三方进来就成环了。

所以行数这个指标，在这里给出的是一个**否定的答案**，而且这个否定答案是有用的：
它排除了"拆大文件"这条路。真正的问题还没找到，得换个量法。

> **本节确定了什么**：不拆 `agent.py`。重构的触发条件是耦合，不是行数。
> 下面用一个能量耦合的办法再找一遍。

---

## §3 那到底怎么判断该不该拆

有四个问题，按顺序问。它们的共同点是**都能被真的算出来**，不靠感觉。

### 3.1 第一问：依赖的方向对不对

这是四个里最重要的一个，也是唯一一个可以完全自动化的。

我们要的是一张**依赖图**：每个模块 import 了包内的哪些模块。好消息是不用装工具，
第 1 章写测试的时候已经有现成的函数了——`tests/test_boundaries.py` 里的
`imported_modules()`，当时是用来断言"agent 不许 import httpx"的：

```python
def imported_modules(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
    return names
```

> **写工具之前先翻一遍自己的测试目录。** 测试里为了断言某件事，经常已经把
> "怎么算出这件事"写好了。

拿它把整个包扫一遍：

```
$ python - <<'EOF'
import ast, pathlib
src = pathlib.Path('src/minicodex')
for p in sorted(src.glob('*.py')):
    deps = set()
    for n in ast.walk(ast.parse(p.read_text(encoding='utf-8'))):
        if isinstance(n, ast.ImportFrom) and n.module and n.module.startswith('minicodex'):
            deps.add(n.module.replace('minicodex.', '') or '__init__')
    print(f'{p.stem:15} -> {", ".join(sorted(deps)) or "(leaf)"}')
EOF

__init__        -> (leaf)
__main__        -> agent, minicodex, model, recorder, stub, tools
agent           -> agent_types, history, model, recorder
agent_types     -> (leaf)
history         -> agent_types
model           -> (leaf)
patch           -> paths, tool_errors
paths           -> tool_errors
recorder        -> (leaf)
shell           -> tool_errors
stub            -> (leaf)
tool_errors     -> (leaf)
tools           -> patch, paths, shell, tool_errors
```

这张图要怎么读？看三件事：

**一、有没有环。** 顺着箭头走，能不能绕回起点。这里不能——`tool_errors` 谁也不
依赖，`paths` 只依赖它，`patch` 依赖这两个，一层一层往上，没有任何一条路回头。
这叫**有向无环图**（DAG）。**环是最该修的东西**，因为它意味着两个模块谁也无法
单独理解、单独测试、单独替换。这里一个都没有。

**二、`__main__` 依赖了一大堆。** 这不是问题，反而是对的：**入口就应该是那个
知道所有零件、负责把它们接起来的地方**。真正危险的是**中间层**依赖了一大堆。

**三、`agent` 和 `tools` 是两棵不相交的子树。**

这一条值得停下来看清楚。`agent` 往下走到 `agent_types / history / model /
recorder`；`tools` 往下走到 `patch / paths / shell / tool_errors`。两组之间
**一条边都没有**。

也就是说：**`agent.py` 完全不认识 `tools.py`。** 它只知道自己拿到了一个
`dict[str, ToolFn]`，至于这个 dict 是谁给的、里面是什么，它一概不问。

这是好设计。第 1 章那条"归一化到客户端为止"的思路在这里又生效了一次。

### 3.2 第二问：影响面有多大

改一个东西要动几个分散的文件？动 1 个文件是正常，动 3 个是要注意，
**动超过 5 个分散的文件，通常说明缺了一层**——那 5 处在各自维护同一个概念。

### 3.3 第三问：这是第几次了

**三次法则**：第一次写死；第二次容忍复制粘贴（留个 TODO）；**第三次才抽象。**

为什么不是第二次？因为两个样本看不出真正的共性。你会把两处的**偶然相同**当成
本质相同，抽出一个错的接口——而**错的抽象比没有抽象更难拆**，因为后来的代码会
把它当约束，一层层长在上面。

### 3.4 第四问：这条边界能不能写成测试

如果一条架构规则只能写在文档里、写在 code review 的口头约定里，那它 100% 会烂掉。
**能写成测试的边界才是真边界。** 第 1 章已经这么干过：`test_boundaries.py` 里
那句 `assert "httpx" not in imported_modules(SRC / "agent.py")`，就是把"循环层
不许碰网络"这条规则钉死了。

### 3.5 四问的答案

| 问题 | 答案 |
|---|---|
| 依赖方向 | 干净的 DAG，没有环 |
| 影响面 | 加一个工具动 1 个文件 |
| 第几次 | **三个工具了**：`read_file`(Ch00) / `run_shell`(Ch02) / `apply_patch`(Ch04) |
| 边界能写成测试吗 | 能，而且已经有两条了 |

前两问说"没事"，**第三问说"到点了"**。

三次法则刚好在这一章触发。所以问题变成：这三个工具里，有什么东西被写了三遍？

> **本节确定了什么**：`agent.py` 不动。但工具已经有三个了，
> 三次法则到点，得去看看它们之间在重复什么。

---

## §4 两张表

打开 `tools.py`，翻到最后。那里有两个函数。

**第一个**，把工具名映射到真正执行的函数：

```python
def default_tools(root: Path | None = None) -> dict[str, Any]:
    root = (root or Path.cwd()).resolve()
    session = ShellSession()
    return {
        "read_file": functools.partial(read_file, root),
        "run_shell": functools.partial(_run_shell, session),
        "apply_patch": functools.partial(apply_patch, root),
    }
```

**第二个**，生成给模型看的 schema（这里只贴骨架，完整版有 100 行）：

```python
def tool_schemas(timeout: float = DEFAULT_TIMEOUT) -> list[dict[str, Any]]:
    return [
        {"type": "function", "function": {"name": "read_file",   ...}},
        {"type": "function", "function": {"name": "apply_patch", ...}},
        {"type": "function", "function": {"name": "run_shell",   ...}},
    ]
```

请注意这两个函数之间的关系：**没有关系。** 一个返回 dict，一个返回 list，
两边各自把工具名当字符串写了一遍，没有任何代码、任何类型、任何测试要求它们一致。

它们唯一见面的地方，是 `__main__.py` 的第 32 到 34 行——**作为两个互不相干的参数**：

```python
llm   = ChatCompletionsModel(..., tools=TOOL_SCHEMAS)   # 模型看见的
agent = Agent(llm, default_tools(), recorder=recorder)  # 真正会跑的
```

这就是那个被写了三遍的东西：**每加一个工具，要在两个地方各写一次它的名字。**
第一次（`read_file`）没问题，第二次（`run_shell`）也还行，现在是第三次。

### 4.1 写漏一边会怎样

光说"会不一致"没有说服力。真跑一次看看。

下面这段不改任何源码，只是**按"手滑漏了一行"的样子**把两张表拼出来，
然后让一个必定会调用 `apply_patch` 的假模型去撞它，看模型收到什么：

```python
class CallsApplyPatch:
    """一个模型：第一轮调用 apply_patch，然后就闭嘴。"""
    async def stream(self, messages):
        self.saw = [dict(m) for m in messages]
        self.turn += 1
        if self.turn == 1:
            yield ToolCallDelta(call_id="call_1", index=0, name="apply_patch",
                                arguments=json.dumps({"edits": [...]}))
        yield Completed("stop")
```

三个场景：两张表一致（今天的样子）、schema 有而 dict 没有、dict 有而 schema 没有。

```
--- both tables agree (today) ---
  handler table (what runs)      : ['apply_patch', 'read_file', 'run_shell']
  schema table  (what model sees): ['apply_patch', 'read_file', 'run_shell']
  model was told  : 'Applied 1 edit(s) to a.py.'
  file on disk    : 'x = 2\n'

--- in schema, NOT in handler table ---
  handler table (what runs)      : ['read_file', 'run_shell']
  schema table  (what model sees): ['apply_patch', 'read_file', 'run_shell']
  model was told  : "Error: no tool named 'apply_patch'. Available tools: read_file, run_shell. Call one of those instead."
  file on disk    : 'x = 1\n'

--- in handler table, NOT in schema ---
  handler table (what runs)      : ['apply_patch', 'read_file', 'run_shell']
  schema table  (what model sees): ['read_file', 'run_shell']
  model was told  : 'Applied 1 edit(s) to a.py.'
  file on disk    : 'x = 2\n'
```

**注意看中间那一段模型收到的话：**

```
Error: no tool named 'apply_patch'. Available tools: read_file, run_shell.
Call one of those instead.
```

这句话是第 0 章写的，用来对付 **F00-06：模型瞎编了一个不存在的工具名**。
当时的设计很讲究——不光说"没这个工具"，还列出有哪些、还告诉它下一步该干嘛。

现在它被一个**我们自己的接线错误**触发了。于是：

> **我们的 bug 穿着模型犯错的衣服回来了。**

后果比"报个错"严重得多。模型是个很听话的东西：你告诉它"没有 `apply_patch`
这个工具，改用 `read_file` 或 `run_shell`"，**它就真的不再尝试了**。它会去用
`run_shell` 里的 `sed` 硬改文件，或者干脆告诉用户这件事做不到。你在日志里看到
的是"模型能力不行"，实际是你少写了一行。

**而第三个场景更安静：什么错都没有。** 上面那段之所以显示"改成功了"，是因为
我强行让假模型去调用它；真实的模型**根本不会调用一个它没被告知存在的工具**。
所以真实后果是：这个工具永远不会被用到，永远没有任何报错，Agent 只是**莫名其妙
地笨一点**。

| 漏在哪边 | 现象 | 发现方式 |
|---|---|---|
| dict 里没有 | 模型收到"工具不存在"，然后放弃这条路 | 🟡 静默（错误信息在撒谎） |
| schema 里没有 | 什么都不会发生 | 🟡 静默（**根本没有信号**） |

两个方向都不崩溃。这就是第 4.2 节标记里那个 🟡 —— 全书 164 条里占了将近一半的
那一类。

> **本节确定了什么**：真正的债是"一个工具要在两个地方各声明一次"，
> 而写漏任何一边都没有报错。这是这一章要还的债。

---

## §5 动手之前：先把"现在的行为"写下来

找到该改的地方了。**但还不能改。**

因为"重构"这个词的定义是：**改结构，不改行为**。
而"行为没变"这句话，只有在**有东西描述了原来的行为**时才能被验证。

那我们不是有 125 个测试吗？

### 5.1 先证明覆盖是薄的

问一个很具体的问题：这 125 个测试里，有几个测到了 `default_tools()`？

```
$ grep -rn "default_tools" tests/
$
```

**没有输出。一个都没有。**

作为对照，看看它那位双胞胎兄弟：

```
$ grep -rn "TOOL_SCHEMAS\|tool_schemas" tests/
tests/test_agent.py:24:   from minicodex.tools import TOOL_SCHEMAS
tests/test_agent.py:63:   return ChatCompletionsModel(base_url=stub_url, tools=TOOL_SCHEMAS, ...)
tests/test_agent.py:387:  body = ChatCompletionsModel(model="m", tools=TOOL_SCHEMAS).request_body(
tests/test_schemas.py:21: from minicodex.tools import TOOL_SCHEMAS, tool_schemas
tests/test_schemas.py:27: return next(t["function"] for t in TOOL_SCHEMAS if ...)
tests/test_schemas.py:156:other = tool_schemas(timeout=90.0)
tests/test_schemas.py:224:for tool in TOOL_SCHEMAS:
tests/test_schemas.py:247:for tool in TOOL_SCHEMAS:
```

**schema 那张表：8 处。处理器那张表：0 处。**

这个不对称本身就说明问题了。schema 是"看得见"的东西——它是文本，会被贴进请求体，
第 3 章整整一章都在研究它的措辞，所以它被测得很仔细。而处理器表是"接线"，
看不见，于是没有人给它写测试。

### 5.2 用变异测试量化它

`grep` 只能说明"没人提到它"，不能说明"改坏了没人发现"。想证明后者，用**变异测试**：

> **变异测试**：故意把代码改坏，然后跑测试。
> 测试变红 = 这段代码真的被测到了；
> 测试还是绿的 = 这段代码**处在"改了没人知道"的状态**。

设计三个变异，每一个都是一次手滑就能造成的：

| 变异 | 干了什么 | 对应现实 |
|---|---|---|
| `rename` | 把 key 写成 `"read_flie"` | 打错字 |
| `drop` | 删掉 `apply_patch` 那一行 | 合并冲突时解错了 |
| `add` | 多加一个 schema 里没有的 `delete_file` | 加工具时只改了一半 |

跑（step04 原样，Python 3.10）：

```
baseline           125 passed in 5.22s
rename             125 passed in 5.10s
drop               125 passed in 5.12s
add                125 passed in 5.12s
restored           125 passed in 5.10s
```

**四行数字一模一样。**

把工具表改烂——改名、删掉、多塞一个——125 个测试**一个都没红**。

> 这就是 **FA-02**：不是"我担心覆盖不足"，是量出来的。
> 而且它恰好落在我们马上要动手改的那个地方。

### 5.3 characterization test 是什么

要补的测试，和这个仓库里已有的 125 个不太一样。它有个名字：
**characterization test**（有时译"表征测试"、"固化测试"）。

区别是这样的：

| | 普通测试 | characterization test |
|---|---|---|
| 断言的是 | 代码**应该**做什么 | 代码**现在实际**做什么 |
| 写的时候看着 | 需求 | 代码和它的实际输出 |
| 行为有缺陷时 | 应该红 | **也照样钉住，红不了** |
| 用来干嘛 | 保证正确 | 保证**不变** |

最后一行是重点。characterization test **不判断对错，只负责记录现状**。

这本书里其实已经有一个了，只是当时没给它起名字。第 4 章那个：

```python
def test_several_edits_to_one_file_all_land():
    ...
```

它钉的是"同一个文件的两处编辑，最后一个赢"——而 Ch04 的 code review 第 1 条
已经承认了**这是个 bug**。测试照样把它钉住了，理由当时也写清楚了：

> 在见到真实案例之前定合并语义，就是在凭空发明规格。……
> **下次有人改它，测试会红，那时候就有真实案例了。**

这就是 characterization test 的全部用意：**先把现状变成一个会说话的东西**，
至于现状对不对，是下一步的事。

**为什么重构之前必须先有它**：重构 = 行为不变。没有东西描述"当前行为"，
"不变"这个断言就是空的。你只能靠"我觉得我没改坏"——而这正是 FA-01
（重构顺手改坏了一个行为）发生的方式。

### 5.4 补三样东西

**第一样：整个 schema 的字节快照。**

第 3 章已经有一个描述快照 `test_F03_10_descriptions_are_pinned` 了，为什么还要一个？
因为它只钉**描述文字**。而类型、`required` 列表、嵌套结构、工具出现的顺序——
这些**每一轮都会被完整发给模型**，所以它们**全都是行为**。

```python
def test_FA_02_the_entire_schema_is_pinned_not_only_its_descriptions() -> None:
    expected = (FIXTURES / "tool_schemas.json").read_text(encoding="utf-8")
    assert _canonical(TOOL_SCHEMAS) == expected
```

快照存成一个单独的 `.json` 文件，而不是塞进 Python 源码里。这样它既是测试的
参照物，也是**待会儿可以直接拿来 `diff` 的东西**。

**第二样：两张表必须一致。**

```python
def test_FA_02_every_advertised_tool_has_a_handler(tmp_path: Path) -> None:
    missing = _advertised() - set(default_tools(tmp_path))
    assert not missing, f"advertised to the model but not runnable: {sorted(missing)}"


def test_FA_02_every_handler_is_advertised(tmp_path: Path) -> None:
    extra = set(default_tools(tmp_path)) - _advertised()
    assert not extra, f"runnable but never shown to the model: {sorted(extra)}"
```

两个方向各一个。§4 已经演示过，它们防的是两种完全不同的事故。

**第三样：一整轮对话的 golden transcript。**

这是最重要的一个，也是待会儿真正用来判定"行为有没有变"的那个。

思路：用一个**完全确定性**的假模型，按剧本走三轮，把**每一轮发出去的请求体
原样存下来**。

```python
class ScriptedModel:
    """一个没有主见的模型：照着固定剧本念。

    刻意做成确定性的。要钉住的就是这份 transcript，任何会在两次运行之间
    变化的东西——真模型、时钟、子进程——都会让这个钉子失去意义。
    """
    async def stream(self, messages):
        self.sent.append([dict(m) for m in messages])
        ...
```

剧本要覆盖循环真正负责的那几件事，**一轮里全占齐**：

| 剧本 | 覆盖什么 |
|---|---|
| 第 1 轮：两个 `read_file`，一个存在一个不存在 | 一轮多个调用、顺序、失败的工具照样要有 output |
| 第 2 轮：一个 `apply_patch` | 真的改了磁盘 |
| 第 3 轮：只有文字，没有调用 | 停止条件 |

存下来的东西长这样（节选第 2 轮，也就是第一轮结果回填之后）：

```json
[
  {"role": "user", "content": "change x to 2 in a.py"},
  {"role": "assistant", "content": "",
   "tool_calls": [
     {"id": "call_a1", "type": "function",
      "function": {"name": "read_file", "arguments": "{\"path\": \"a.py\"}"}},
     {"id": "call_a2", "type": "function",
      "function": {"name": "read_file", "arguments": "{\"path\": \"missing.py\"}"}}
   ]},
  {"role": "tool", "tool_call_id": "call_a1", "content": "x = 1\n"},
  {"role": "tool", "tool_call_id": "call_a2",
   "content": "Error: no such file in the repository You sent: missing.py ..."}
]
```

**注意看两条 `role: tool`**：两个调用，两个结果，`call_id` 一一对应，
顺序和模型发出来的顺序一致，而且**失败的那个也有 output**——这正是第 0 章
F00-03 和第 1 章 F01-02 拿命换来的不变量，现在被完整地钉在一个文件里了。

还有一件事值得指出。最后一轮的末尾会多出这么一条：

```json
{"role": "system",
 "content": "You have 2 tool-calling turn(s) left. Wrap up and give your best answer now."}
```

这是第 0 章的轮次预算警告（F00-01）。它**在第几轮出现**也是行为，
也被这份 transcript 钉住了。

有一个 `run_shell` 没有出现在剧本里，是故意的：它要起一个 POSIX shell，
而**一份只在一个平台上成立的快照，等于没有快照**。

### 5.5 再变异一次

测试补完了。现在把刚才那三个变异**原封不动**再跑一遍：

```
baseline           132 passed in 5.15s
rename             3 failed, 129 passed in 5.10s
drop               2 failed, 130 passed in 5.10s
add                1 failed, 131 passed in 5.09s
restored           132 passed in 5.10s
```

对比一下：

| 变异 | 补测试之前 | 补测试之后 |
|---|---|---|
| `rename` | `125 passed` | **3 failed** |
| `drop` | `125 passed` | **2 failed** |
| `add` | `125 passed` | **1 failed** |

三个变异，从全绿变成 3 / 2 / 1 个红。**现在可以动手了。**

> **本节确定了什么**：先把现状钉住，再用变异证明钉子真的钉住了。
> 顺序不能反——测试写在重构之后，钉的就是新行为，那什么也证明不了。

---

## §6 现在动手：`ToolSpec`

要解决的问题很具体：**一个工具，得在两个地方各声明一次名字。**
那就让它只声明一次。

### 6.1 先想清楚它有几个"部分"

一个工具，从代码的角度看有四样东西：名字、描述、参数 schema、真正执行的函数。

但这四样**寿命不一样**，这一点很关键：

- 名字 / 描述 / 参数：**静态的**。整个进程里都一样，而且 `TOOL_SCHEMAS`
  是在 import 的时候就要算出来的。
- 执行函数：**跟着一次会话走**。它闭包了两样东西——仓库根目录 `root`
  （用来判断路径有没有跑到仓库外），和一个 `ShellSession`（用来让 `cd` 在多次
  调用之间保持住）。这两样都不属于进程，属于**这一次对话**。

如果 `ToolSpec` 直接存一个"已经绑好的函数"，就等于把 `Path.cwd()` 和一个
`ShellSession` 拖进 import 阶段——只为了填一个渲染 schema 时**根本不会读**的字段。

所以存的不是函数，是**怎么绑出函数**：

```python
@dataclass(frozen=True)
class ToolContext:
    """一次会话里，handler 需要的全部东西。"""
    root: Path
    shell: ShellSession


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    parameters: dict[str, Any]
    bind: Callable[[ToolContext], ToolFn]

    def schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }
```

`ToolContext` 里的两个字段不是新东西——它俩本来就存在，只是以散装参数的形式被
`functools.partial` 串来串去。**这里只是给它们起了个名字。**

### 6.2 一张表，两个派生

```python
def tool_specs(timeout: float = DEFAULT_TIMEOUT) -> list[ToolSpec]:
    return [
        ToolSpec(
            name="read_file",
            description="Read a UTF-8 text file from the repository and return its contents.",
            parameters={...},
            bind=lambda ctx: functools.partial(read_file, ctx.root),
        ),
        ToolSpec(name="apply_patch", ..., bind=lambda ctx: functools.partial(apply_patch, ctx.root)),
        ToolSpec(name="run_shell",   ..., bind=lambda ctx: functools.partial(_run_shell, ctx.shell)),
    ]


def default_tools(root: Path | None = None) -> dict[str, ToolFn]:
    context = ToolContext(root=(root or Path.cwd()).resolve(), shell=ShellSession())
    return {spec.name: spec.bind(context) for spec in tool_specs()}


def tool_schemas(timeout: float = DEFAULT_TIMEOUT) -> list[dict[str, Any]]:
    return [spec.schema() for spec in tool_specs(timeout)]
```

**注意这两个函数现在各自只有一行。** 它们不再"各自维护一份工具清单"，
而是**同一份清单的两种投影**。

于是 §4 那两种事故**在类型层面就不可能了**：`tool_specs()` 里加一个条目，
它同时变得可见且可执行；不加，两边就都没有。**没有第二个地方可以忘。**

### 6.3 `ToolFn` 该放哪：一个差点造出来的环

写到这里撞上一个问题。`ToolSpec.bind` 的类型要写 `Callable[[ToolContext], ToolFn]`，
所以 `tools.py` 需要 `ToolFn` 这个类型。而 `ToolFn` 定义在 `agent.py` 里：

```python
ToolFn = Callable[[dict[str, Any]], Awaitable[str]]
```

那就 `from minicodex.agent import ToolFn` 好了？

**这样写能跑，而且 131 个测试全绿。** 因为今天确实没有环——回头看 §3.1 那张图，
`agent` 根本不 import `tools`，箭头只有一个方向。

但它是一支**指错方向的箭头**：`agent.py` 是最上面的循环，`tools.py` 是底下的
零件，**底下不该依赖上面**。

而这种"暂时没事"的反向箭头，正是环的前半截。设想一下最自然的下一步——
有人觉得 `Agent()` 每次都要外面传工具表太啰嗦，给它加个默认值：

```python
class Agent:
    def __init__(self, model, tools=None, ...):
        self.tools = tools if tools is not None else default_tools()   # ← 环成了
```

这一行看起来完全无害。但此刻 `agent → tools → agent` 闭合，import 报错，
而且报错会指向**碰巧后加载的那个模块**，跟真正的原因隔着十万八千里。
那就是 **FA-04**。

解法第 1 章已经给过了：**共享的类型往下沉，不要横着依赖。**
当时 `ToolCall` 就是这么处理的，`agent_types.py` 这个模块就是那次留下的。
`ToolFn` 现在走同一条路：

```python
# agent_types.py
ToolFn = Callable[[dict[str, Any]], Awaitable[str]]
```

改完之后的依赖图，只多了一条边，而且方向朝下：

```
agent_types     -> (leaf)          ← 依然是叶子
agent           -> agent_types, history, model, recorder
tools           -> agent_types, patch, paths, shell, tool_errors
                   ^^^^^^^^^^^
```

### 6.4 把这条规则钉成测试

依赖方向这种事，光在 docstring 里写"请不要 import agent"是留不住的
（第 -1 章的原话：只写在文档里的架构规则 100% 会烂掉）。它只有一行，写成测试：

```python
def test_FA_04_tools_does_not_import_the_agent() -> None:
    imports = imported_modules(SRC / "tools.py")
    assert "minicodex.agent" not in imports, (
        "tools.py must not depend on agent.py -- put the shared type in "
        "agent_types.py, as chapter 1 did with ToolCall"
    )
```

它有用吗？变异一下——把 import 改回 `from minicodex.agent import ToolFn`：

```
as written            125 passed, 7 skipped
arrow reversed        1 failed, 124 passed, 7 skipped
restored              125 passed, 7 skipped
```

**注意看：只有 1 个红，其余全绿。** 这恰恰是重点——**除了这个测试，
没有任何东西会注意到箭头反了。** 程序照跑，功能照常，
唯一的信号就是这一行断言。这就是"把边界写成测试"的意思。

> **本节确定了什么**：一张表两个派生，两种漏写事故在结构上消失；
> 顺手挡掉了一个还没长成的循环依赖，并把这条边界钉成了测试。

---

## §7 怎么证明行为没变

改完了。现在轮到 **FA-01**——怎么保证这次重构没有顺手改坏什么。

答案不是"我保证"，是**机制**。三道。

### 7.1 第一道：schema 逐字节相同

§5.4 存的那个快照现在派上用场了。模型每一轮看到的字节，必须和改之前**一模一样**：

```
$ python -c "import json, minicodex.tools as t; \
    print(json.dumps(t.TOOL_SCHEMAS, indent=2, ensure_ascii=False))" > /tmp/after.json

$ diff tests/fixtures/tool_schemas.json /tmp/after.json
$
```

**没有输出，就是没有差异。**

为什么这道特别重要？因为第 3 章测出来过：**描述里改一个例子，两家供应商就从
3/3 对翻成 3/3 错。** 也就是说 schema 里任何一个字节的变化都是行为变化。
这一道拦的就是"重构时顺手把描述文字整理了一下"。

### 7.2 第二道：整轮对话逐条相同

字节相同只说明"发给模型的工具清单"没变，不能说明**循环本身**没变。
golden transcript 管这个：三轮请求体、每一条消息、最终文本、停止原因、
用了几轮、磁盘上文件的最终内容——全部对得上。

### 7.3 第三道：全量测试

```
$ uv run pytest
132 passed in 5.16s
```

### 7.4 有一样东西确实变了

三道机制都过了，但**我得主动说一件它们没拦住的事**——因为纯重构的诚实，
不在于"什么都没变"，而在于**变了的东西你说出来了**。

处理器 dict 的**插入顺序**变了：

```
was : ['read_file', 'run_shell', 'apply_patch']
now : ['read_file', 'apply_patch', 'run_shell']
```

原因很简单：现在只有一张表了，而它按 schema 的顺序排（schema 顺序不能动，
§7.1 那道是逐字节的）。一张表没法同时有两种顺序。

**这个变化能不能被观察到？** 去数这个 dict 被用到的每一处——一共三处，
全在 `agent.py`：

```python
73:  self.tools = tools or {}                            # 存下来
136: if call.name not in self.tools:                     # 成员判断
137: available = ", ".join(sorted(self.tools)) or "(none)"   # 排过序
```

赋值、`in` 判断、`sorted()`。**没有任何一处依赖插入顺序。**

所以结论是"不可观察"，但要注意这个结论的性质：它不是三道机制**证明**出来的，
是我**去数了一遍调用点**得出来的。区别在于——如果哪天有人在 `agent.py` 里写了
一句按 dict 顺序遍历工具的代码，这三道机制**不会**告诉我这里有隐患。

> **纯重构的产出不只是"测试全绿"，还包括一句
> "有 X 变了，我查过为什么没关系"。**
> 前者是自动的，后者只能靠人。

> **本节确定了什么**：零行为变更靠三道机制 + 一次人工交代，
> 不靠"我觉得我没改坏"。

---

## §8 那两个不属于这一章的 bug

这一章还撞上了两个东西，都不是这次重构造成的，都是**从第 2 章一路带过来的**。

按 §1 那条纪律，它们**不进重构的 commit**，各自单独一个，排在前面。

### 8.1 第一次在别的机器上跑

前面所有数字都是 Python 3.10 跑出来的。换个版本试试——注意 README 里推荐的是
**3.11+**：

```
$ uv run --python 3.11 pytest
（没有输出。等了两分半，还是没有输出。）
```

**不是变慢，是死锁。** 挂在 `test_F02_02_a_firehose_is_capped_not_buffered_whole`
——第 2 章那个"100MB 输出不能撑爆内存"的测试。

### 8.2 定位：一个 asyncio 的契约细节

把这段逻辑从项目里剥出来，用一个 20 行的脚本重现。剥出来之后发现：
`os.killpg` 瞬间返回，卡住的是**下一行**的 `await proc.wait()`。

一个已经被 `SIGKILL` 杀掉的进程，`wait()` 为什么会等不到？

去读 CPython 的 `asyncio/base_subprocess.py`：

```python
def _try_finish(self):
    if self._returncode is None:
        return
    if all(p is not None and p.disconnected          # ← 每一个管道都要到 EOF
           for p in self._pipes.values()):
        self._call(self._call_connection_lost, None) # ← 唤醒 wait() 的唯一入口
```

**注意第二个 `if`。** `proc.wait()` 被唤醒的条件不是"进程退出了"，而是
**"进程退出了，并且每一个管道都读到了 EOF"**。`_call_connection_lost` 是
唯一会去 resolve `wait()` 那个 future 的地方。

再看我们的代码干了什么：读到 100 万字符的上限，**主动不读了**，killpg，然后 wait。

管道里还剩着没读完的字节。我们不读，EOF 就永远不会到；EOF 不到，
`p.disconnected` 就一直是 False；于是 `_try_finish` 永远不放行——
**在一个早就死透了的进程上，`wait()` 一直等下去。**

那为什么第 2 到 4 章一直没事？因为它是**竞态**：如果恰好在撞上限的那一刻管道
已经被读空了，EOF 就到了，一切正常。跑一个矩阵，每个版本 12 次：

```
python 3.10.20    0/12 hang
python 3.11.15   12/12 hang
python 3.12.3     4/12 hang
python 3.13.13    4/12 hang
```

**3.10 一次都不挂，3.11 次次都挂。** 第 2 到 4 章是在 3.10 上写的——
所以这个 bug 安安静静地跟着走了三章，而且它偏偏在这本书**自己推荐的版本**上
是必现的。

### 8.3 修复：两个方案，选测出来的那个

**方案 A**：给 `wait()` 加个超时，等不到就算了。
**方案 B**：既然决定不读了，就**先把读端关掉**，再 wait。

各跑 12 次：

```
control  4/12 HUNG      slowest wait:  6.01s    ← 现在的代码
close    OK             slowest wait:  0.00s    ← 方案 B
bounded  6/12 HUNG      slowest wait:  2.00s    ← 方案 A
```

**方案 A 并没有修好它**，只是把损失从"永远"压到"2 秒"，而且每次触发都要白等
满这 2 秒。方案 B 是 0/12，且 `wait()` 立刻返回。

差别在于：A 是**盖住症状**，B 是**消除原因**——asyncio 要求"管道到 EOF"，
我们就明确地告诉它"这个管道我不要了"。

```python
os.killpg(proc.pid, signal.SIGKILL)

pipe = proc._transport.get_pipe_transport(1)
if pipe is not None:
    pipe.close()

await proc.wait()
```

四个版本全部 0/12。

### 8.4 更值得说的是：测试为什么没抓住它

第 2 章那个测试是这么写的：

```python
async def test_F02_02_a_firehose_is_capped_not_buffered_whole() -> None:
    session = ShellSession(timeout=10)
    out = await session.run("yes | head -c 100000000")
    assert len(out) <= MAX_OUTPUT_CHARS + 200
```

它断言了输出被截断，**但没有对时间设任何限制**。所以 bug 发生时，
这个测试**不会变红，它会挂住**。

我第一次修的时候，本能地在后面加了一句：

```python
    out = await session.run(...)
    elapsed = time.monotonic() - start
    assert elapsed < 8            # ← 完全没用
```

**这一句永远不会被执行。** 上一行的 `await` 就再也没返回过，代码根本走不到断言。

正确的写法是把界限加在 **`await` 本身**上：

```python
async def _bounded(session: ShellSession, command: str, *, limit: float = 20) -> str:
    return await asyncio.wait_for(session.run(command), timeout=limit)
```

差别有多大？把修复撤掉，两种写法各跑一次：

| 测试写法 | 结果 |
|---|---|
| 断言写在 `await` 后面 | 挂住，没有任何输出，直到有人按 Ctrl-C |
| 界限加在 `await` 上 | `3 failed`，而且点名了是哪三个 |

```
=========================== short test summary info ============================
FAILED tests/test_shell.py::test_F02_02_a_firehose_is_capped_not_buffered_whole
FAILED tests/test_shell.py::test_ceiling_does_not_leave_wait_blocked_on_a_dead_process
FAILED tests/test_shell.py::test_F02_08_a_stubborn_child_that_ignores_the_pipe_closing_is_still_killed
```

> **一个挂住的测试，比一个失败的测试糟糕得多。**
> 失败会报告、会计数、会让 CI 变红；挂住什么都不产出，
> 在 CI 里的表现是"这个 job 今天怎么跑了 40 分钟"。

顺带一提，第三个红的 `test_F02_08` 说明超时路径和上限路径**共享同一个缺陷**，
所以它也加了同样的界限。

### 8.5 Windows：一个已知缺口第一次被真的量到

这一章还第一次在 Windows 上跑了这个项目。7 个测试红了：

```
AttributeError: module 'os' has no attribute 'killpg'
```

`os.killpg` 和 `start_new_session` 都是 POSIX 专有的。这件事**早就写在
`FAULTS.md` 里**了（F02-10），原话是"没有 Windows 环境可供测量，作为明确缺口记录"。
现在有环境了。

**修不修？不修。** Windows 上要做进程组级别的 kill，得用 job object，
是一整块工作。而随手写个"降级方案"更糟：

```python
proc.kill()     # 看起来能用，其实是灾难
```

它杀掉的是 shell，**shell fork 出来的孙子进程会继续跑**——这恰恰就是
F02-08 那条"孤儿进程活得比 Agent 还久"，第 2 章花了很大力气才堵上。
**一个半吊子的实现比一个诚实的缺口更危险。**

那就让缺口**自己报出名字**来：

```python
posix_only = pytest.mark.skipif(
    sys.platform == "win32",
    reason="F02-10: POSIX process groups and POSIX shell builtins; see FAULTS.md",
)
```

```
$ uv run pytest          # Windows
125 passed, 7 skipped in 9.89s
```

**从 7 个莫名其妙的红，变成 7 个说清楚了理由的 skip。** 代码能力一点没变，
但这个仓库现在对"它在 Windows 上不完整"这件事是**诚实的**，
而不是让每个 Windows 用户自己去 debug 一遍 `AttributeError`。

> **本节确定了什么**：两个继承来的 bug，各自单独 commit，排在重构前面。
> 顺带学到：挂住的测试比失败的测试糟糕，而半吊子的跨平台支持比明确的缺口糟糕。

---

## §9 看到了，但没有改的四件事

这是这一章最难的一节，因为**重构最难的部分是停手**。

重构的时候，你的注意力被调到了最敏感的档位，于是满眼都是问题。每一个都"只要
改一点点"。而它们加在一起，就是 FA-01 说的那个"藏在大 diff 里的行为改动"。

这四件事我都看见了，都没改，每一条都有理由：

| 看到的 | 为什么不改 |
|---|---|
| **`system_prompt()` 至今没有任何调用方。** `__init__.py` 里定义了它，`prompts/system.md` 也随包发布了，但 Agent 从来没有发过一条 system 消息 | **这是行为变更，不是重构。** 加上它，模型收到的东西就变了，§7 那三道机制会全部变红——而且它们**应该**变红。系统提示词是第 13 章的题目 |
| **`apply_patch` 手写了 35 行参数校验**，一层层检查 `edits` 是不是 list、每项是不是 dict、三个字段在不在。下一个需要复杂参数的工具必然复制粘贴 | **三次法则：这是第一次。** 只有一个工具有这个问题。第二次容忍复制，第三次才抽象。现在就抽，抽出来的一定是照着 `apply_patch` 的形状长的 |
| **`_collect` 是流装配，不是循环逻辑**，`agent.py` 自己的 docstring 都承认了边界是猜的 | **它只有一个调用方。** 移动它不会减少任何耦合，只会让 diff 变长。按 §3 那四问，它一条都不满足——**用自己刚定的标准判自己** |
| **同一文件两处编辑"最后一个赢"**（Ch04 review 第 1 条已经记账） | 定合并语义需要真实案例，而**到现在还是没有**。凭空发明规格的成本，比再等一章高 |

第三条值得多说一句。`agent.py` 开头那段 docstring 是这么写的：

> The boundaries between stream assembly, tool dispatch and the loop are
> guesses right now... **Interlude A pays this off**, once there are enough
> call sites for the boundaries to be observed rather than invented.

**这一章就是 Interlude A，而这笔账我没有还。** 因为条件没到——那句话的后半段
说得很清楚："等到调用点多到边界可以被**观察**出来，而不是被**发明**出来"。
现在 `_collect` 仍然只有一个调用点。

> 一句写在代码里的"以后再说"，到期时该做的判断是**重新评估**，
> 不是**无条件执行**。

---

## §10 验证

| 检查 | 结果 |
|---|---|
| Linux，Python 3.10 / 3.11 / 3.12 / 3.13 | `132 passed` ×4 |
| Windows，Python 3.10 | `125 passed, 7 skipped` |
| `ruff check` | `All checks passed!` |
| `ruff format --check` | `25 files already formatted` |
| schema 字节 diff | 无差异 |

四个解释器都是同一个数字，这件事本身就是 §8 那个修复的验收——
**在这一章开始的时候，3.11 上这个套件根本跑不完。**

变异验证（每一条修复都要能被自己的测试抓住）：

| 把什么改回去 | 结果 |
|---|---|
| 删掉 `pipe.close()` | `3 failed`（不再是挂住） |
| 反转 `ToolFn` 的 import 方向 | `1 failed` |
| 工具表改名 / 删项 / 多项 | `3 / 2 / 1 failed`（原来是 0 / 0 / 0） |

测试数从 125 到 132，加了 7 个：1 个上限死锁回归测试、4 个 characterization、
1 个依赖方向、1 个 `.gitignore` 守门（见 §11 最后一行）。

---

## §11 文件清点

| 文件 | 改动 |
|---|---|
| `src/minicodex/tools.py` | `ToolSpec` / `ToolContext` / `tool_specs()`；两张表变成一张表的两个派生 |
| `src/minicodex/agent_types.py` | `ToolFn` 下沉到这里 |
| `src/minicodex/agent.py` | 删掉 `ToolFn` 定义，改成 import |
| `src/minicodex/shell.py` | give-up 之后先关管道再 wait |
| `tests/test_characterization.py` | 新增，4 个测试 |
| `tests/fixtures/tool_schemas.json` | 新增，schema 字节快照 |
| `tests/fixtures/golden_transcript.json` | 新增，三轮请求体 |
| `tests/test_shell.py` | `_bounded()` 辅助函数、上限回归测试、7 个 `posix_only` 标记 |
| `tests/test_boundaries.py` | 依赖方向测试 |
| `tests/test_packaging.py` | `.gitignore` 守门清单加一项 |
| `.gitignore` | 加 `.minicodex/` |
| `README.md` | 重写（它从第 1 章之后就没更新过） |

最后三行都是**收尾时才发现的**，顺手记一下。

**`README.md`**：`step04` 的第一行至今写着 **"step 1: the protocol layer"**。
文档从第 1 章之后就停止更新了，而**没有任何人发现**——因为没有任何东西会去测
散文。这里不追溯修改，只把这一步的写对。

**`.gitignore`**：跑完 §10 那次端到端验证之后，我顺手 `ls -a` 了一下工作目录，
多出来一个 `.minicodex/`：

```
$ uv run minicodex ask "..." --base-url http://127.0.0.1:11466/v1
...
[transcript: .minicodex\recordings\session-1786176270.jsonl]
```

第 -1 章建的那个 recorder，**每跑一次就写一份完整对话**——prompt、模型的每句
回复、以及 Agent 读过的每个文件的全部内容。而 `.gitignore` 里没有它。

第 -1 章的 `.gitignore` 覆盖了 `.env`、`*.key`、`*.pem`——**都是"一看就知道是
机密"的东西**。漏掉的这个不一样：它不是你放进去的，是**程序自己创建的**。
F-1-04 那个脱敏只处理凭据，不处理源代码。

> 这也解释了为什么一直没人发现：要撞见它，得**真的把 Agent 跑起来，
> 然后去看 `git status`**。跑测试不会创建它（`NULL_RECORDER` 写 `os.devnull`）。

修法是一行，但更要紧的是把它加进第 -1 章那个守门测试的清单里——
否则下一个 step 复制过去的时候又会漏。

---

## §12 收工：commit 与 review

### commit 序列

```
fix:      close the pipe before waiting on a killed process
test:     characterize the tool tables before moving them
refactor: declare each tool once, derive both tables from it
chore:    ignore the recordings directory the agent writes
```

**四个 commit，顺序是有讲究的：**

1. **先修 bug。** 它跟重构没关系，而且**可以单独 cherry-pick 回 step02**——
   那个 bug 从第 2 章就在了。混进重构的 diff 里，这个能力就没了。
2. **再补测试。** 这个 commit 里**一行产品代码都没有**。它单独存在的价值是：
   任何人都可以只 checkout 到这里，跑一次变异，亲眼看到 3/2/1 个红。
3. **最后重构。** 到这一步，前面两个 commit 已经把安全网架好了。
4. **收尾发现的那个 `.gitignore`，还是单独一个。** 它跟前面三个都没关系。
   一行改动 + 一行测试，混进任何一个 commit 都会让那个 commit 的 message 说谎。

每一个都能单独通过测试，每一个都能单独 revert。

> 第 4 个 commit 最能说明 §1 那条纪律的实际形状：它小到"顺手塞进上一个"
> 完全没有心理负担。**而正是这种小改动最容易把 commit 历史变成一笔糊涂账**——
> 大的改动谁都知道要单独提。

commit message 的 body 写**为什么**，不写**改了什么**（改了什么 diff 里有）。
第三个的 body：

```
The handler table and the schema list were two independent declarations of
the same three tools, meeting only in __main__ as two arguments. Dropping a
tool from either side was silent: from the handler table the model gets the
"no tool named X" error chapter 0 wrote for hallucinated names, and from the
schema it simply never learns the tool exists.

Not done: the argument validation in apply_patch is still hand-written. That
is the first occurrence, not the third.
```

### Code review

**1 · `bind` 用 lambda，堆栈里全是 `<lambda>`，出错难查。**

> 作者：这条说得对，是个真实代价。没改的理由是**目前不可能出错**——`bind`
> 只做一次 `functools.partial`，不含任何逻辑，抛不出异常。真正跑起来会抛异常的
> 是 partial 包住的那个函数，它有正常的名字。如果哪天 `bind` 里开始有逻辑
> （比如第 5 章的审批策略要往里塞东西），就该换成具名函数。**记在这里。**

**2 · `ToolContext` 只有两个字段，是不是过早抽象？现在完全可以直接传 `(root, session)`。**

> 作者：想过，结论是**它不算新抽象，只是给已有的东西命名**。这两个值本来就以
> 散装参数的形式在 `default_tools` 里被 partial 串来串去，字段数没变、生命周期
> 没变。真正的检验标准是：删掉 `ToolContext` 会怎样？`bind` 的签名变成
> `Callable[[Path, ShellSession], ToolFn]`，每个工具还是得写全两个参数，
> 只是少了个名字。**换不来什么。** 但请注意我**没有**给它加"以后 Ch05 要放
> 审批策略"这种字段——那才是过早抽象。

**3 · `tool_schemas()` 现在每次调用都要构造一遍全部 `ToolSpec`，比原来贵。**

> 作者：确实。但 `TOOL_SCHEMAS` 是模块级常量，**整个进程只算一次**；
> `default_tools()` 一次会话调一次。这是启动时的一次性开销，量级在微秒。
> 拿"每加一个工具就有一处可能忘记"去换它，我认为很划算。而且**如果哪天这真的
> 成了热点，缓存是三行的事，两张表却不是**。

**4 · `posix_only` 是不是在掩盖问题？skip 掉就看不见了。**

> 作者：这条最值得聊。skip 和"删掉测试"的区别在于——**skip 会在输出里报数**：
> `125 passed, 7 skipped`，而且 `-rs` 能打出理由，理由里写了 `F02-10`，
> 顺着编号能查到 `FAULTS.md` 里那一整段。
> 反过来说，让它们保持红也不是"更诚实"：**一个长期红的 CI 等于没有 CI**，
> 两周之内所有人都会学会无视它。真正不诚实的做法是**改断言让它在 Windows 上
> 蒙混过关**——那才是把问题藏起来。

**5 · golden transcript 把 `Error: no such file...` 这句原文钉死了，
以后改错误信息措辞，这个测试就会红。**

> 作者：对，而且**这正是我要的**。第 3 章测出来错误信息就是 prompt——
> gemma4 在一句话的错误上 3/3 没救回来，在三段式的错误上 3/3 救回来了。
> **错误信息的措辞是行为**，改它就应该有东西红一下。
> 代价是以后改文案要更新这个 fixture，一条命令的事；
> 收益是没有人能在"顺手润色一下"的时候悄悄改掉模型的行为。

### Merge

squash 成一个？**不。** 这三个 commit 的顺序本身就是这一章的论点：
**先修 bug、再补网、最后动刀。** 压成一个就把这个信息扔了。
本教程默认 squash-and-merge，但这是那种应该保留提交历史的 PR。

---

## §13 本章给 CI 加了什么

只加了一条：**依赖方向检查**（§6.4 那个测试）。它跟着 `pytest` 一起跑，
不需要新的 CI 步骤。

**没有加的**：一个"全包无环"的通用检查。理由是现在没有环，这条规则**还没有
被违反过一次**。第 -1 章那条"CI 只配 3 步、按证据生长"的原则在这里同样适用——
等真的出现一个环（Interlude B 会），再把它一般化。

同理，也没有加"每个模块不许超过 N 行"这种检查。**这一章的结论恰恰是行数不是
触发条件**，配一个基于行数的门禁，等于把刚推翻的东西写进 CI。

---

## §14 codex 是怎么做的

**codex 也没有一次拆干净。** 仓库里 `multi_agents/` 和 `multi_agents_v2/`
是**并存**的——先写了 v1，发现不行，写了 v2，**但不敢直接删 v1**。
`agent/control/legacy.rs`、`features/src/legacy.rs` 也是一样：旧路径长期共存，
迁移是渐进的。

> 这跟"重构就要一步到位改干净"的想象差很远，但它是真实的。

**codex 的 `AGENTS.md` 里有一条**：「模块超过 800 行就别再往里加」。

这跟这一章 §2 的结论——"行数不是触发条件"——看起来是矛盾的。值得说清楚：

这两件事**处理的问题不一样**。800 行那条是一个**上界**，是在一个几十万行、
几十个人的仓库里，防止某个模块变成谁也不敢碰的黑洞；它是**止损线**，不是
**触发线**。而这一章问的是"该不该重构"，那是触发线的问题。

**上界可以用行数，触发线不能。** 一个 212 行的文件没有触及任何上界，
而它该不该拆，得看耦合。

顺带一提，`AGENTS.md` 里那句「**resist adding code to codex-core**」，
和这一章 §9 那四条"看到了但没改"是同一种东西：**写下来的克制**。
它们都不是一开始的设计，是吃过亏之后立的护栏。

**codex 的 CI 里有一个 `verify_tui_core_boundary.py`**，专门验证 tui 层不许直接
import core。跟 §6.4 那条测试是同一个思路，只是规模大一号：**架构边界靠 CI 强制，
不靠口头约定。** Interlude B 会把这件事做到底。

---

## §15 回头看：这一章撞到了什么

| 故障 | 怎么暴露的 | 挡住它的东西 |
|---|---|---|
| 工具表和 schema 表可以不一致，两个方向都不报错 | 🟡 静默（一个方向错误信息在撒谎，一个方向连信息都没有） | `ToolSpec`：声明一次，两边派生 |
| **测试全绿，但即将被重构的那张表零覆盖** | 🟡 静默（`grep` 出来是空的） | 先补 characterization test，再动手 |
| `ToolFn` 从 `agent.py` 拿会造出一支反向箭头 | 🟣 review（能跑，131 个测试全绿） | 类型下沉到 `agent_types.py`，边界写成测试 |
| **上限路径永远不返回**，在 3.11 上 12/12 复现 | 🔵 长跑 / 换机器 | give-up 后先关管道再 wait |
| 那个测试抓不住它，因为界限加在 `await` 后面 | 🟣 review（修的时候自己撞上的） | 界限加在 `await` 本身上 |
| Windows 上 `os.killpg` 不存在，7 个测试红 | 🔴 崩溃（换平台才出现） | 不实现，`skipif` + 写明 F02-10 |
| 每一步的 `README.md` 还写着 "step 1" | 🟠 看文件时发现 | 只修这一步的；没有东西会去测散文 |
| **`.minicodex/recordings/` 没被 `.gitignore` 覆盖**。每跑一次 `ask` 就写一份，里面是完整的对话——prompt、模型回复、以及 Agent 读过的每个文件的内容 | 🟠 跑完之后看了一眼工作目录 | 加进 `.gitignore`，并加进第 -1 章那个守门测试的清单 |

标记：🔴 崩溃 · 🟡 静默 · 🟢 边界测试 · 🔵 长跑 · 🟠 看日志 · 🟣 review · ⚫ 用户报告 · ⚪ lint/类型

**这一章的七条里，有五条不是这次重构造成的。** 它们都是**在为重构做准备的过程中**
被翻出来的：量依赖图的时候、数测试覆盖的时候、换个 Python 版本跑的时候。

> 重构本身可能一个 bug 都改不掉。
> 但**为了安全地重构而必须先做的那些准备**，会翻出一堆。

---

## 如果你只记住三件事

1. **重构的触发条件是耦合，不是行数。**
   `agent.py` 212 行，不用拆；而真正的债——一个工具要在两个地方各声明一次——
   在任何行数统计里都是看不见的。**排期上写着"这周重构"，不构成重构的理由；
   为了交差而制造一个本来不存在的边界，比不重构更贵。**

2. **重构之前，先证明你的测试能发现问题。**
   不是"跑一遍看看绿不绿"——它当然是绿的。是**故意改坏，看它红不红**。
   工具表改名、删项、多塞一项，125 个测试的回答是 `125 passed / 125 passed /
   125 passed`。**在那一刻，那 125 个绿点关于这次重构什么也没保证。**

3. **纯重构的产出，包括一份"我改了什么、以及为什么它不算行为变更"的说明。**
   三道机制能自动证明 schema 字节没变、transcript 没变、测试全绿；
   但 dict 插入顺序变了这件事，是我**去数了三个调用点**才敢说没关系的。
   **能自动化的部分要自动化，不能自动化的部分要说出来**——
   而不是指望没人问起。

---

## 动手练习

1. **把 §5 的顺序倒过来做一遍。** 先做 `ToolSpec` 重构，做完之后再补
   characterization test，然后跑那三个变异。测试会全绿——因为你钉的是新行为。
   **然后想清楚：这种情况下，那些绿点到底证明了什么？**

2. **给 `tool_specs()` 加第四个工具**（比如 `list_files`），故意只写一半——
   `ToolSpec` 建好了但从返回的 list 里漏掉。看哪个测试会红、报的是什么。
   然后回头看 §4 那三段实测输出，**判断这次重构到底消灭了哪一类错误、
   又没消灭哪一类。**（提示：没消灭的那类仍然存在，只是从两处变成了一处。）

3. **把 `pipe.close()` 删掉，在 Python 3.11 上跑 `tests/test_shell.py`。**
   然后把 `_bounded()` 里的 `asyncio.wait_for` 也去掉，再跑一次。
   **对比这两次你分别等了多久、看到了什么。** 这是"挂住的测试比失败的测试糟糕"
   最直接的一次体感。

4. **在你自己的项目里跑一遍 §3.1 那段依赖图脚本。** 如果出现了环，
   先别急着修——**先去数这个环里的模块被多少个测试覆盖着**。
   §5 的结论在那里同样成立：**先看安全网，再动手。**

---

下一章：Ch05 · 审批与沙箱——Agent 现在能改任何文件、跑任何命令，包括 `rm -rf`。
---

# 附录 · 代码到底怎么敲出来的（新手向，逐段精讲）

跟第 8 章附录同一个规矩：正文讲"为什么"，这里补"怎么写"。默认你已经读过
前面几章的附录（`dataclasses`、`functools.partial`、`Callable` 基础），
这里只讲这一章正文只有片段的部分。代码摘自
`steps/stepA_refactor/src/minicodex/`，逐段核对过。

先把范围说死：

1. 本附录只解释插曲 A 在 `steps/stepA_refactor/` 里新增或修改的代码。
   正文 §6 已经给了 `ToolSpec`/`ToolContext`/`tool_specs`/`default_tools`/
   `tool_schemas` 的核心（§6.1/§6.2），§6.3 给了 `ToolFn` 下沉，§8 给了
   `shell.py` 的管道修复——这些**不再重复**。这里补正文 §6.2 里用
   `parameters={...}` 省略掉的完整 `tool_specs`（正文只有形状），以及
   `agent.py` 改动的完整轮廓。
2. 完整函数代码块里不使用代表遗漏实现的省略号，也不把关键分支写成
   "此处略"。示例字符串或命令文本里出现的三个点，如果本来就是源码的
   一部分，会原样保留。
3. 代码块按当前源码核对。正文 §6.2 的 `parameters={...}` 是占位符，
   本附录补上真实参数（`read_file` 的 schema 与第 3/4 章一致，这里
   只贴 `tool_specs` 的三个 `ToolSpec` 构造，schema 内容逐字核对）。

## R1 · `tool_specs` 完整实现

正文 §6.2 给了形状（`parameters={...}` 占位）。完整实现：

```python
def tool_specs(timeout: float = DEFAULT_TIMEOUT) -> list[ToolSpec]:
    return [
        ToolSpec(
            name="read_file",
            description="Read a UTF-8 text file from the repository and return its contents.",
            parameters={
                "type": "object",
                "required": ["path"],
                "properties": {
                    "path": {
                        "type": "string",
                        "description": (
                            "Path to the file, relative to the repository root. "
                            "Example: src/minicodex/model.py"
                        ),
                    }
                },
            },
            bind=lambda ctx: functools.partial(read_file, ctx.root),
        ),
        ToolSpec(
            name="apply_patch",
            description=(
                "Edit files by replacing exact blocks of text. Every edit is "
                "checked before any file is written: if one fails, nothing is "
                "written."
            ),
            parameters={
                "type": "object",
                "required": ["edits"],
                "properties": {
                    "edits": {
                        "type": "array",
                        "description": "The edits to apply, in any order.",
                        "items": {
                            "type": "object",
                            "required": ["path", "old_text", "new_text"],
                            "properties": {
                                "path": {
                                    "type": "string",
                                    "description": (
                                        "Path to the file, relative to the repository root. "
                                        "Example: src/minicodex/model.py"
                                    ),
                                },
                                "old_text": {
                                    "type": "string",
                                    "description": (
                                        "The text to replace, copied from the file. It "
                                        "must appear exactly once -- include whole "
                                        "surrounding lines until it does. Do not write "
                                        "line numbers or diff markers."
                                    ),
                                },
                                "new_text": {
                                    "type": "string",
                                    "description": "What to put in its place.",
                                },
                            },
                        },
                    }
                },
            },
            bind=lambda ctx: functools.partial(apply_patch, ctx.root),
        ),
        ToolSpec(
            name="run_shell",
            description=(
                "Run a shell command and return its combined stdout and stderr. "
                "The working directory persists across calls within one session "
                "(cd changes it for subsequent calls). Backgrounded commands "
                "(trailing '&') are not supported. Long-running or silent "
                f"commands are killed after {timeout:.0f} seconds."
            ),
            parameters={
                "type": "object",
                "required": ["command"],
                "properties": {
                    "command": {
                        "type": "string",
                        "description": "The shell command to run.",
                    }
                },
            },
            bind=lambda ctx: functools.partial(_run_shell, ctx.shell),
        ),
    ]
```

三个要点：

1. **`bind` 是"怎么绑出函数"，不是已绑好的函数**（正文 §6.1 的核心）——
   `lambda ctx: functools.partial(read_file, ctx.root)` 在 `default_tools`
   调用 `spec.bind(context)` 时才真正绑定。`read_file` 绑 `ctx.root`，
   `apply_patch` 绑 `ctx.root`，`run_shell` 绑 `ctx.shell`——每个工具只
   绑它真正需要的那个会话对象。
2. **`parameters` 与第 3/4 章的 schema 逐字节相同**——这是正文 §7.1
   "schema 逐字节相同"那道证明的前提：`tool_schemas()` 现在从
   `tool_specs()` 派生，而旧的 `tool_schemas` 手写内容被完整搬进
   `ToolSpec.parameters`。
3. **`description` 从第 4 章起逐字保留**（"Every edit is checked before
   any file is written"等）——重构不改描述，只改组织方式。`run_shell`
   的描述加了第 3 章那条 "Backgrounded commands (trailing '&') are not
   supported"。

## R2 · `default_tools` 与 `tool_schemas`：一张表，两个派生

正文 §6.2 给了核心，这里补完整：

```python
def default_tools(root: Path | None = None) -> dict[str, ToolFn]:
    context = ToolContext(root=(root or Path.cwd()).resolve(), shell=ShellSession())
    return {spec.name: spec.bind(context) for spec in tool_specs()}


def tool_schemas(timeout: float = DEFAULT_TIMEOUT) -> list[dict[str, Any]]:
    return [spec.schema() for spec in tool_specs(timeout)]
```

- **`default_tools` 一个 `ToolContext` 建一次**（root 和 shell 是每会话
  一个），然后 `{spec.name: spec.bind(context)}` 字典推导——每个 spec 的
  bind 用同一个 context。**加一个工具 = 在 `tool_specs` 加一个 `ToolSpec`**，
  它同时变得可见（schema）且可执行（handler），没有第二个地方可以忘
  （正文 §4 的两种事故在结构上消失）。
- **`tool_schemas(timeout)` 把 timeout 传给 `tool_specs(timeout)`**——注意
  `tool_specs` 的参数只有 `timeout`（`ToolSpec` 的 `parameters` 里没有
  timeout 字段；timeout 是 `run_shell` handler 的，但 schema 里不暴露）。
  第 3 章"描述里的数字从常量生成"的机制保留。

## R3 · `agent.py` 的改动轮廓

正文 §7 用三道证明（schema 字节相同 / 整轮对话逐条相同 / 全量测试）说明
"行为没变"。代码层面的改动只有两处：

```python
# agent.py 顶部：ToolFn 从本地定义改为 import
from minicodex.agent_types import ToolCall, ToolFn

# 删掉了原来的三行
# ToolFn = Callable[[dict[str, Any]], Awaitable[str]]
```

- **`ToolFn` 从 `agent.py` 移到 `agent_types.py`**（正文 §6.3 详述）——
  因为 `tools.py` 的 `ToolSpec.bind` 需要它，而 `tools` 不该依赖
  `agent`（箭头朝上 = 环的前半截）。`agent_types` 是叶子，两个都依赖它，
  方向朝下。
- **`agent.py` 不再定义 `ToolFn`**，改成 `from minicodex.agent_types
  import ToolCall, ToolFn`。`agent` 的 `tools: dict[str, ToolFn]` 类型标注
  不变，只是来源换了。
- 其余（`Agent.__init__` 的签名、`_run_tool`、`run`）**一行没动**——这是
  正文 §7 那三道证明能成立的前提：重构只动了"工具怎么组织"，没动"循环怎么
  跑"。

## R4 · 新手常见报错/坑对照表

| 症状 | 原因 | 修法 |
|---|---|---|
| 加了工具但模型看不到 | 只改了 handler 表没改 schema | `tool_specs()` 是唯一清单，加一个 `ToolSpec` 两边都派生 |
| 工具可调但 schema 没有 / 反之 | 两张表分开维护 | 一张表两个派生（`default_tools`/`tool_schemas` 都从 `tool_specs` 来） |
| `import minicodex.agent` 报循环 | `tools` 依赖 `agent` | 共享类型下沉到 `agent_types.py`（`ToolFn` 同 `ToolCall` 的路） |
| bind 了 `Path.cwd()` 到 import 期 | `ToolSpec` 存已绑函数 | 存 `bind: Callable[[ToolContext], ToolFn]`，`default_tools` 时才绑 |
| 重构后 schema 和以前不同 | 手抄描述抄错 | §7.1 的 `_canonical(TOOL_SCHEMAS) == expected` 字节快照测试 |
| 重构后对话行为变了 | 循环逻辑被顺手改了 | §7.2 的 golden transcript（三轮请求体逐条相同） |
| `wait()` 在 3.11 上挂死 | 管道没读到 EOF 就不唤醒 wait | 先关读端管道再 wait（正文 §8.3 方案 B） |
