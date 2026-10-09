# 插曲 A · 第一次重构

> **代码**：`steps/stepA_refactor/`
> **分支**：`refactor/tool-specs`
> **产出**：没有任何新功能。一个工具只需声明一次；以及一套能证明"行为没变"的测试
> **前置**：做完第 4 章。不需要模型。

这一章做三件事：判断哪里**该**重构（以及哪里不该），在动手之前把"现在的行为"写下来，
然后证明改完之后行为确实没变。

---

## §0 开工前的准备

### 0.1 本章会遇到的新概念

- **重构（refactor）**：**只改代码的组织方式，不改它的行为。** 用户、模型、测试看到的一切都不变。
- **耦合**：两段代码"改一个就得同时想着另一个"的程度。
- **依赖图**：每个模块 import 了哪些别的模块，画成箭头。
- **变异测试（mutation testing）**：故意把代码改坏，看测试会不会失败。前几章"把修复改回去"就是它的手工版。
- **characterization test（表征测试）**：记录代码**现在实际**怎么做的测试，§5.3 细讲。

### 0.2 本章会遇到的新 Python 写法

| 写法 | 意思 |
|---|---|
| `lambda ctx: ...` | 一个没有名字的小函数，`lambda 参数: 返回值` |
| `Callable[[A], B]` | 类型标注："一个接收 A、返回 B 的可调用对象" |
| `{k: f(k) for k in items}` | 字典推导式 |
| `json.dumps(x, indent=2)` | 把数据转成缩进整齐的 JSON 文本 |

### 0.3 现在的代码长什么样

前面几章攒到现在，是这样一个包：

| 文件 | 干什么的 | 哪一章加的 |
|---|---|---|
| `agent.py` | 主循环：问模型 → 跑它要的工具 → 把结果放回去 → 再问 | 第 0 章 |
| `agent_types.py` | `ToolCall`，agent 和 history 共用 | 第 1 章 |
| `history.py` | 对话历史，存"事实"，拒绝不合法的状态 | 第 1 章 |
| `model.py` | HTTP 客户端，把两家服务的差异挡在这里 | 第 1 章 |
| `recorder.py` | 把每次请求和回复写进文件 | 第 0 章 |
| `shell.py` | `run_shell`：超时、截断、进程组 | 第 2 章 |
| `paths.py` | 路径解析，不许跑出仓库 | 第 3 章 |
| `tool_errors.py` | 三段式错误信息 | 第 3 章 |
| `patch.py` | `apply_patch` 的定位与写入 | 第 4 章 |
| `tools.py` | 工具的实现、工具表、给模型看的描述 | 第 0 章起 |
| `stub.py` | 录音服务 | 第 0 章 |
| `__main__.py` | 命令行入口 | 第 -1 章 |

```
$ cd steps/step04_apply_patch
$ uv run pytest
120 passed, 7 skipped in 14.58s
```

（Windows 上实测；7 个跳过的是第 2 章的 F02-10。本章所有测试数字都是 Windows 上的实测。）

记住 **120** 这个数，这一章会反复回到它。

```bash
git switch main
git pull
git switch -c refactor/tool-specs
```

---

## §1 一条纪律：这一章不加任何功能

> **纯重构的 PR 里不许夹带功能改动。**

为什么这条值得单独说？因为它违反直觉。重构一个函数时，你会看到旁边有个小 bug；顺手改掉只要三行，
不改反而要专门记一笔、以后再来一趟。**顺手改看起来是效率，实际是把两件事搅在一起。**

搅在一起的代价，在出问题时才结账：

- 一个 400 行的改动里藏着 3 行行为改动，审查的人**看不见**——他的脑子已经切换到"这些都是等价搬迁"的模式了；
- 出了问题要撤销，只能整个撤销，连那 397 行没问题的搬迁一起；
- 一个月后用 `git bisect`（在历史里二分查找引入问题的那个提交）定位到这个提交，message 写着
  "refactor: extract tool spec"，没人会想到问题出在那 3 行。

所以这一章的规矩是：**看到的问题可以记下来，但不在这里改。** 真的必须现在改的，**单独一个提交，
排在重构前面**——和第 4 章"先修 bug 再加功能"是同一条规矩。

---

## §2 先量一量：到底该不该重构

写了五章，加了三个工具，代码"应该"已经很乱了吧？这个念头通常是对的。但**"应该"不是证据**，
所以先量。最容易量的是行数：

```
$ wc -l src/minicodex/*.py | sort -rn
 1913 total
  275 src/minicodex/stub.py
  251 src/minicodex/shell.py
  225 src/minicodex/tools.py
  212 src/minicodex/agent.py
  200 src/minicodex/patch.py
  191 src/minicodex/model.py
  178 src/minicodex/history.py
   99 src/minicodex/recorder.py
   91 src/minicodex/paths.py
   90 src/minicodex/__main__.py
   45 src/minicodex/tool_errors.py
   31 src/minicodex/agent_types.py
   25 src/minicodex/__init__.py
```

> `wc -l` 数每个文件有几行，`sort -rn` 按数字从大到小排。

最大的是 `stub.py`——一堆录下来的回复，本来就该长。真正的逻辑文件最大 251 行。
**按行数看，没有任何一个文件到了该拆的程度。**

这里有个常见的陷阱：既然计划里写着"这周重构"，那总得拆点什么——**于是拆了一个本来不需要拆的东西，
造出一条本来不存在的边界。**

行数好量，但它量的是**体积**，而重构真正要解决的是**耦合**：

> 一个 800 行、只有一处调用、依赖关系笔直向下的文件，
> 比两个各 100 行、互相 import 的文件安全得多。

前者可以整块读完、整块替换、整块删掉；后者动任何一边都要同时想着另一边。

所以行数在这里给出了一个**否定的答案**，而且这个答案有用：它排除了"拆大文件"这条路。

---

## §3 那到底怎么判断该不该拆

四个问题，按顺序问。它们的共同点是**都能真的算出来**，不靠感觉。

### 3.1 第一问：依赖的方向对不对

我们要一张**依赖图**：每个模块 import 了包里的哪些模块。不用装工具——第 1 章写
`test_boundaries.py` 时已经有现成的做法了（用 `ast` 找出 import）。拿它把整个包扫一遍：

```
$ uv run python - <<'EOF'
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

> `python - <<'EOF' ... EOF` 是 bash 的写法：把两个 `EOF` 之间的内容作为脚本交给 Python。
> PowerShell 里可以把这段存成一个 `.py` 文件再运行。
> `(leaf)` 表示"叶子"：不依赖包里任何别的模块。

这张图看三件事：

**一、有没有环。** 顺着箭头走，能不能绕回起点。不能——`tool_errors` 谁也不依赖，`paths` 只依赖它，
`patch` 依赖这两个，一层一层往上，没有回头路。这叫**有向无环图（DAG）**。**环是最该修的东西**，
因为它意味着两个模块谁也无法单独理解、单独测试、单独替换。这里一个都没有。

**二、`__main__` 依赖了一大堆。** 这是对的：**入口就应该是那个知道所有零件、负责把它们接起来的地方。**
危险的是**中间层**依赖一大堆。

**三、`agent` 和 `tools` 是两棵不相交的子树。** `agent` 往下走到 `agent_types / history / model / recorder`；
`tools` 往下走到 `patch / paths / shell / tool_errors`。两组之间**一条边都没有**。
**`agent.py` 完全不认识 `tools.py`**——它只知道自己拿到了一个 `dict[str, ToolFn]`，
至于是谁给的、里面是什么，一概不问。这是好设计。

### 3.2 第二问：影响面有多大

改一个东西要动几个分散的文件？动 1 个正常，动 3 个要注意，**动超过 5 个分散的文件，通常说明缺了一层**——
那 5 处在各自维护同一个概念。

### 3.3 第三问：这是第几次了

**三次法则**：第一次直接写；第二次容忍复制；**第三次才抽象。**

为什么不是第二次？因为两个例子看不出真正的共同点。你会把两处的**偶然相同**当成本质相同，抽出一个错的接口——
而**错的抽象比没有抽象更难拆**，因为后来的代码会一层层长在它上面。

### 3.4 第四问：这条边界能不能写成测试

第 1 章说过：只写在文字里的架构规则，迟早会被违反。**能写成测试的边界才是真边界。**

### 3.5 四问的答案

| 问题 | 答案 |
|---|---|
| 依赖方向 | 干净的 DAG，没有环 |
| 影响面 | 加一个工具动 1 个文件 |
| 第几次 | **已经三个工具了**：`read_file`、`run_shell`、`apply_patch` |
| 边界能写成测试吗 | 能，而且已经有了 |

前两问说"没事"，**第三问说"到点了"**。所以问题变成：这三个工具里，有什么被写了三遍？

> **结论：`agent.py` 不拆。** 重构的触发条件是耦合，不是行数。

---

## §4 两张表

打开 `tools.py`，翻到最后。那里有两个函数。

**第一个**，把工具名对应到真正执行的函数：

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

**第二个**，生成给模型看的描述（只贴骨架，完整的有一百行）：

```python
def tool_schemas(timeout: float = DEFAULT_TIMEOUT) -> list[dict[str, Any]]:
    return [
        {"type": "function", "function": {"name": "read_file",   ...}},
        {"type": "function", "function": {"name": "apply_patch", ...}},
        {"type": "function", "function": {"name": "run_shell",   ...}},
    ]
```

注意这两个函数之间的关系：**没有关系。** 一个返回字典，一个返回列表，两边各自把工具名当字符串写了一遍，
没有任何代码、类型或测试要求它们一致。它们唯一碰面的地方，是 `__main__.py` 里的两个**互不相干的参数**：

```python
llm   = ChatCompletionsModel(..., tools=TOOL_SCHEMAS)   # 模型看得见的
agent = Agent(llm, default_tools(), recorder=recorder)  # 真正会跑的
```

这就是被写了三遍的东西：**每加一个工具，要在两个地方各写一次它的名字。**

### 4.1 写漏一边会怎样

光说"会不一致"没有说服力。真跑一次：不改源码，只是按"手滑漏了一行"的样子拼出两张表，
然后让一个一定会调用 `apply_patch` 的假模型去撞：

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

三种情况：两张表一致（今天的样子）；描述里有、执行表里没有；执行表里有、描述里没有。

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

（原稿实测。）

**看中间那一段模型收到的话。** 这句错误信息是第 0 章写的，用来应对 **F00-06：模型编了一个不存在的工具名**。
现在它被**我们自己的接线错误**触发了：

> **我们的 bug，穿着"模型犯错"的衣服回来了。**

后果比"报个错"严重。模型很听话：你告诉它"没有 `apply_patch`，改用 `read_file` 或 `run_shell`"，
**它就真的不再尝试了**——它会用 `run_shell` 里的 `sed` 硬改文件，或者告诉用户这件事做不到。
你在日志里看到的是"模型能力不行"，实际是少写了一行。

**第三种情况更安静：什么错都没有。** 上面显示"改成功了"，是因为强行让假模型去调用它；真实的模型
**根本不会调用一个没人告诉它存在的工具**。于是这个工具永远不会被用到，永远没有报错，Agent 只是
**莫名其妙地笨一点**。

| 漏在哪边 | 现象 | 怎么暴露 |
|---|---|---|
| 执行表里没有 | 模型收到"工具不存在"，放弃这条路 | 🟡 静默（错误信息在撒谎） |
| 描述里没有 | 什么都不会发生 | 🟡 静默（**根本没有信号**） |

> **结论：真正的债是"一个工具要在两个地方各声明一次"，写漏任何一边都不报错。**

---

## §5 动手之前：先把"现在的行为"写下来

找到该改的地方了。**但还不能改。** 重构的定义是"改结构、不改行为"，而"行为没变"这句话，
只有在**有东西描述了原来的行为**时才能被验证。

那不是有 120 个测试吗？

### 5.1 先证明覆盖是薄的

一个很具体的问题：这 120 个测试里，有几个用到了 `default_tools()`？

```
$ grep -rn "default_tools" tests/
$
```

**没有输出，一个都没有。** 作为对照，看看它的"双胞胎"：

```
$ grep -rn "TOOL_SCHEMAS\|tool_schemas" tests/
tests/test_agent.py:24:from minicodex.tools import TOOL_SCHEMAS
tests/test_agent.py:63:    return ChatCompletionsModel(base_url=stub_url, tools=TOOL_SCHEMAS, extra_body=extra)
tests/test_agent.py:387:    body = ChatCompletionsModel(model="m", tools=TOOL_SCHEMAS).request_body(
tests/test_schemas.py:21:from minicodex.tools import TOOL_SCHEMAS, tool_schemas
tests/test_schemas.py:27:    return next(t["function"] for t in TOOL_SCHEMAS if t["function"]["name"] == name)
tests/test_schemas.py:159:    other = tool_schemas(timeout=90.0)
tests/test_schemas.py:227:    for tool in TOOL_SCHEMAS:
tests/test_schemas.py:250:    for tool in TOOL_SCHEMAS:
```

> `grep -rn 文字 目录`：在目录里递归查找含有这段文字的行，显示文件名和行号。`\|` 表示"或"。

**描述那张表：8 处。执行那张表：0 处。**

这个不对称本身就说明了问题：描述是"看得见"的——它是文字，会被发给模型，第 3 章整章都在研究它的措辞，
所以被测得很仔细。而执行表是"接线"，看不见，于是没人给它写测试。

### 5.2 用变异测试量化它

`grep` 只能说明"没人提到它"，不能说明"改坏了没人发现"。证明后者，用**变异测试**：

> 故意把代码改坏，然后跑测试。测试失败 = 这段代码真的被测到了；
> 测试还是全部通过 = 这段代码**处在"改了也没人知道"的状态**。

三个变异，每一个都是一次手滑就能造成的：

| 变异 | 干了什么 | 对应现实 |
|---|---|---|
| `rename` | 把键写成 `"read_flie"` | 打错字 |
| `drop` | 删掉 `apply_patch` 那一行 | 合并冲突时解错了 |
| `add` | 多加一个描述里没有的 `delete_file` | 加工具时只改了一半 |

跑（step04 原样）：

```
baseline  120 passed, 7 skipped in 14.58s
rename    120 passed, 7 skipped in 12.14s
drop      120 passed, 7 skipped in 11.56s
add       120 passed, 7 skipped in 12.26s
```

（Windows 上实测。做法是把项目复制到临时目录，改一行、跑一遍、再换下一种改法。）

**四行一模一样。** 把工具表改名、删项、多塞一项，120 个测试**一个都没失败**。

> 这就是 **FA-02**："测试全绿，是因为覆盖很薄"。不是担心，是量出来的，
> 而且恰好落在马上要动手改的地方。

### 5.3 characterization test 是什么

要补的测试和已有的 120 个不太一样，它有个名字：**characterization test**（表征测试）。

| | 普通测试 | characterization test |
|---|---|---|
| 断言的是 | 代码**应该**做什么 | 代码**现在实际**做什么 |
| 写的时候看着 | 需求 | 代码和它的实际输出 |
| 现在的行为有缺陷时 | 应该失败 | **照样记录下来** |
| 用来干什么 | 保证正确 | 保证**不变** |

最后一行是重点：它**不判断对错，只负责记录现状**。

这本书里其实早就有过一个，只是没起名字：第 4 章最初的 `test_several_edits_to_one_file_all_land`，
记录的是"同一文件两处编辑，最后一处赢"——一个已知的缺陷。后来发现那是"报告成功却丢数据"，就把它修掉了。
**记录现状和判断现状是两步**：先记下来，才谈得上"改了之后有没有变"。

**为什么重构之前必须先有它**：重构 = 行为不变。没有东西描述"现在的行为"，"不变"就是一句空话，
只能靠"我觉得我没改坏"——而这正是 FA-01（重构时顺手改坏了行为）发生的方式。

### 5.4 补三样东西

新建 `tests/test_characterization.py`：

```python
"""What the code does today, written down before any of it moves.

These are *characterization* tests, and they are a different animal from the
rest of the suite.  An ordinary test says what the code **should** do, and is
written from the requirement.  A characterization test says what the code
**currently does**, is written from the code, and is allowed to pin behaviour
nobody would choose on purpose.  Chapter 4 first shipped one without naming
it: `test_several_edits_to_one_file_all_land` pinned "the last edit wins", a
known defect, until it was recognised as silent data loss and fixed.

They exist because "refactor" means "behaviour does not change", and nothing
can be shown not to have changed unless something describes it first.  Three
mutations of `default_tools()` -- rename a key, drop an entry, add an entry --
each left all 120 tests green before this file existed.  `default_tools` was
mentioned by exactly zero of them.

FA-02 in FAULTS.md.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Sequence
from pathlib import Path
from typing import Any

from minicodex.agent import Agent
from minicodex.model import Completed, StreamEvent, TextDelta, ToolCallDelta
from minicodex.tools import TOOL_SCHEMAS, default_tools

FIXTURES = Path(__file__).parent / "fixtures"


def _canonical(obj: Any) -> str:
    """One formatting, so a diff shows content changes and nothing else."""
    return json.dumps(obj, indent=2, sort_keys=False, ensure_ascii=False) + "\n"


# ---------------------------------------------------------------------------
# FA-02  the schema table, whole
# ---------------------------------------------------------------------------


def test_FA_02_the_entire_schema_is_pinned_not_only_its_descriptions() -> None:
    """`test_F03_10_descriptions_are_pinned` covers every description.

    It does not cover anything else: types, `required` lists, the nesting, the
    order tools appear in.  All of those are sent to the model on every single
    turn, so all of them are behaviour.  This pins the bytes.

    To regenerate deliberately:
        python -c "import json,minicodex.tools as t; \
print(json.dumps(t.TOOL_SCHEMAS, indent=2, ensure_ascii=False))" \
> tests/fixtures/tool_schemas.json
    """
    expected = (FIXTURES / "tool_schemas.json").read_text(encoding="utf-8")
    assert _canonical(TOOL_SCHEMAS) == expected


# ---------------------------------------------------------------------------
# FA-02  the two tables have to agree, and today nothing says so
# ---------------------------------------------------------------------------


def _advertised() -> set[str]:
    return {tool["function"]["name"] for tool in TOOL_SCHEMAS}


def test_FA_02_every_advertised_tool_has_a_handler(tmp_path: Path) -> None:
    """Otherwise the model calls it and is told the tool does not exist.

    Measured: with `apply_patch` removed from the handler table only, a model
    that calls it receives

        Error: no tool named 'apply_patch'. Available tools: read_file,
        run_shell. Call one of those instead.

    which is the message chapter 0 wrote for a model that invents a tool name
    (F00-06).  A wiring mistake arrives wearing the model's clothes, and the
    model duly obeys it and stops trying.
    """
    missing = _advertised() - set(default_tools(tmp_path))
    assert not missing, f"advertised to the model but not runnable: {sorted(missing)}"


def test_FA_02_every_handler_is_advertised(tmp_path: Path) -> None:
    """The other direction is worse, because there is no error at all.

    A handler the schema never mentions is a capability the model is never
    told about, so it is never called, and nothing anywhere reports it.  The
    tool is simply absent, and the only symptom is the agent being a bit worse
    at its job.
    """
    extra = set(default_tools(tmp_path)) - _advertised()
    assert not extra, f"runnable but never shown to the model: {sorted(extra)}"


# ---------------------------------------------------------------------------
# FA-02  a whole run, pinned turn by turn
# ---------------------------------------------------------------------------


class ScriptedModel:
    """A model with no opinions: it replays a fixed list of turns.

    Deterministic on purpose.  The transcript below is the thing being pinned,
    so anything that could vary between runs -- a real model, a clock, a
    subprocess -- would make the pin meaningless.
    """

    def __init__(self, turns: list[list[ToolCallDelta] | str]) -> None:
        self.turns = turns
        self.sent: list[list[dict[str, Any]]] = []

    async def stream(self, messages: Sequence[dict[str, Any]]) -> AsyncIterator[StreamEvent]:
        self.sent.append([dict(m) for m in messages])
        turn = self.turns[len(self.sent) - 1]
        if isinstance(turn, str):
            yield TextDelta(turn)
        else:
            for call in turn:
                yield call
        yield Completed("stop")


def _call(call_id: str, index: int, name: str, **arguments: Any) -> ToolCallDelta:
    """`index` positions the call within its turn; `call_id` identifies it globally.

    They are separate because the wire keeps them separate: `index` restarts at
    0 every turn, and both providers issue ids that never repeat.  The history
    would in fact accept a repeated id -- it only requires ids to be unique
    among the calls currently unanswered -- but a transcript that reuses
    `call_0` every turn would be teaching a habit no real provider has.
    """
    return ToolCallDelta(
        call_id=call_id,
        index=index,
        name=name,
        arguments=json.dumps(arguments),
    )


async def test_FA_02_a_whole_run_is_pinned_message_by_message(tmp_path: Path) -> None:
    """Every request body the loop produces across a three-turn run.

    This is the artefact the refactor is measured against.  It covers, in one
    run, the things the loop is responsible for: several calls in one turn,
    one result per call in the order the calls were made, a tool that fails,
    the turn-budget note appearing at the right moment, and stopping on a turn
    that has prose and no calls.

    `run_shell` is deliberately not exercised: it spawns a POSIX shell, and a
    transcript that only pins on one platform pins nothing on the other.
    """
    (tmp_path / "a.py").write_text("x = 1\n", encoding="utf-8")

    model = ScriptedModel(
        [
            [
                _call("call_a1", 0, "read_file", path="a.py"),
                _call("call_a2", 1, "read_file", path="missing.py"),
            ],
            [
                _call(
                    "call_b1",
                    0,
                    "apply_patch",
                    edits=[{"path": "a.py", "old_text": "x = 1", "new_text": "x = 2"}],
                )
            ],
            "Changed x to 2.",
        ]
    )

    result = await Agent(model, default_tools(tmp_path), max_turns=4).run("change x to 2 in a.py")

    actual = {
        "requests": model.sent,
        "final_text": result.final_text,
        "stop_reason": result.stop_reason,
        "turns_used": result.turns_used,
        "file_after": (tmp_path / "a.py").read_text(encoding="utf-8"),
    }
    expected = (FIXTURES / "golden_transcript.json").read_text(encoding="utf-8")
    assert _canonical(actual) == expected
```

三样东西，逐个说：

**第一样：整个描述的字节快照**（`test_FA_02_the_entire_schema_is_pinned_not_only_its_descriptions`）。

第 3 章已经有一个描述快照了，为什么还要一个？因为它只记**描述文字**。而类型、`required` 列表、
嵌套结构、工具出现的顺序——这些**每一轮都会完整发给模型**，所以**全都是行为**。

快照存成一个单独的文件 `tests/fixtures/tool_schemas.json`，用 docstring 里写的那条命令生成：

```bash
uv run python -c "import json,minicodex.tools as t; print(json.dumps(t.TOOL_SCHEMAS, indent=2, ensure_ascii=False))" > tests/fixtures/tool_schemas.json
```

存成文件而不是写在代码里，是因为它既是测试的参照，也是**待会儿能直接拿来对比（diff）的东西**。
`_canonical` 用统一的格式把数据转成文字，这样比较时只会看到内容的差异。

**第二样：两张表必须一致**（`test_FA_02_every_advertised_tool_has_a_handler` 和
`test_FA_02_every_handler_is_advertised`）。两个方向各一个——§4.1 演示过，它们防的是两种完全不同的事故。
`_advertised()` 取出描述里所有的工具名；`set(default_tools(tmp_path))` 取出执行表里所有的名字；
两个集合相减，就是只在一边出现的那些。

**第三样：一整轮对话的"金标准"记录**（`test_FA_02_a_whole_run_is_pinned_message_by_message`）。

这是最重要的一个，也是待会儿真正用来判定"行为有没有变"的那个。思路：用一个**完全确定**的假模型
`ScriptedModel`，按剧本走三轮，把**每一轮发出去的请求原样存下来**。

> - `ScriptedModel.stream` 每次被调用时，先把收到的消息复制一份存进 `self.sent`，然后按剧本交出这一轮的内容。
>   docstring 写明了为什么必须确定：真模型、时钟、子进程这些每次都可能不同的东西，会让记录失去意义。
> - `_call(...)` 是造一个工具调用的小帮手，`**arguments` 收集所有关键字参数，转成 JSON。

剧本要覆盖循环真正负责的那几件事：

| 剧本 | 覆盖什么 |
|---|---|
| 第 1 轮：两个 `read_file`，一个文件存在、一个不存在 | 一轮多个调用、顺序、失败的调用照样有结果 |
| 第 2 轮：一个 `apply_patch` | 真的改了磁盘 |
| 第 3 轮：只有文字，没有调用 | 停止条件 |

测试把三轮请求、最终答案、停止原因、用了几轮、磁盘上文件的内容，全部和 `tests/fixtures/golden_transcript.json`
比较。第一次运行之前还没有这个文件：可以在测试末尾临时加一行
`(FIXTURES / "golden_transcript.json").write_text(_canonical(actual), encoding="utf-8")`
生成它，**逐条检查内容确实是你期望的**，再删掉那一行。生成出来的内容是：

```json
{
  "requests": [
    [
      {
        "role": "user",
        "content": "change x to 2 in a.py"
      }
    ],
    [
      {
        "role": "user",
        "content": "change x to 2 in a.py"
      },
      {
        "role": "assistant",
        "content": "",
        "tool_calls": [
          {
            "id": "call_a1",
            "type": "function",
            "function": {
              "name": "read_file",
              "arguments": "{\"path\": \"a.py\"}"
            }
          },
          {
            "id": "call_a2",
            "type": "function",
            "function": {
              "name": "read_file",
              "arguments": "{\"path\": \"missing.py\"}"
            }
          }
        ]
      },
      {
        "role": "tool",
        "tool_call_id": "call_a1",
        "content": "x = 1\n"
      },
      {
        "role": "tool",
        "tool_call_id": "call_a2",
        "content": "Error: no such file in the repository You sent: missing.py Use run_shell with ls or find to see what exists, then try again."
      }
    ],
    [
      {
        "role": "user",
        "content": "change x to 2 in a.py"
      },
      {
        "role": "assistant",
        "content": "",
        "tool_calls": [
          {
            "id": "call_a1",
            "type": "function",
            "function": {
              "name": "read_file",
              "arguments": "{\"path\": \"a.py\"}"
            }
          },
          {
            "id": "call_a2",
            "type": "function",
            "function": {
              "name": "read_file",
              "arguments": "{\"path\": \"missing.py\"}"
            }
          }
        ]
      },
      {
        "role": "tool",
        "tool_call_id": "call_a1",
        "content": "x = 1\n"
      },
      {
        "role": "tool",
        "tool_call_id": "call_a2",
        "content": "Error: no such file in the repository You sent: missing.py Use run_shell with ls or find to see what exists, then try again."
      },
      {
        "role": "assistant",
        "content": "",
        "tool_calls": [
          {
            "id": "call_b1",
            "type": "function",
            "function": {
              "name": "apply_patch",
              "arguments": "{\"edits\": [{\"path\": \"a.py\", \"old_text\": \"x = 1\", \"new_text\": \"x = 2\"}]}"
            }
          }
        ]
      },
      {
        "role": "tool",
        "tool_call_id": "call_b1",
        "content": "Applied 1 edit(s) to a.py."
      },
      {
        "role": "system",
        "content": "You have 2 tool-calling turn(s) left. Wrap up and give your best answer now."
      }
    ]
  ],
  "final_text": "Changed x to 2.",
  "stop_reason": "completed",
  "turns_used": 3,
  "file_after": "x = 2\n"
}
```

**注意看第二轮里的两条 `"role": "tool"`**：两个调用、两个结果，`tool_call_id` 一一对应，顺序和模型发出的一致，
而且**失败的那个也有结果**——这正是第 0 章 F00-03 和第 1 章 F01-02 的规则，现在被完整地记在一个文件里。

最后一轮的末尾还有一条 `"role": "system"` 的"还剩 2 轮"提醒（第 0 章的 F00-01）。**它在第几轮出现，
也是行为**，也被记下来了。

剧本里故意没有 `run_shell`：它要启动一个 shell，而**一份只在一个系统上成立的记录，等于没有记录**。

### 5.5 再变异一次

测试补完了。把刚才那三个变异原封不动再跑一遍：

```
baseline  124 passed, 7 skipped in 12.27s
rename    3 failed, 121 passed, 7 skipped in 12.44s
drop      2 failed, 122 passed, 7 skipped in 12.32s
add       1 failed, 123 passed, 7 skipped in 12.38s
```

（Windows 上实测。）

| 变异 | 补测试之前 | 补测试之后 |
|---|---|---|
| `rename` | 全部通过 | **3 个失败** |
| `drop` | 全部通过 | **2 个失败** |
| `add` | 全部通过 | **1 个失败** |

**现在可以动手了。** 这个提交里**一行产品代码都没有**：

```bash
git add tests
git commit -m "test: characterize the tool tables before moving them"
```

> 它单独存在的价值是：任何人都可以只切到这个提交，跑一遍变异，亲眼看到 3 / 2 / 1 个失败。
> **顺序不能反**——测试写在重构之后，记录的就是新行为，那什么也证明不了。

---

## §6 现在动手：`ToolSpec`

要解决的问题很具体：**一个工具，要在两个地方各声明一次名字。** 那就让它只声明一次。

### 6.1 先想清楚它有几个"部分"

一个工具有四样东西：名字、描述、参数描述、真正执行的函数。但这四样**寿命不一样**，这一点很关键：

- 名字、描述、参数：**静态的**。整个程序运行期间都一样，而且 `TOOL_SCHEMAS` 在 import 时就要算出来。
- 执行函数：**跟着一次对话走**。它绑定了仓库根目录 `root` 和一个 `ShellSession`，这两样都属于**这一次对话**。

如果直接存一个"已经绑好的函数"，就等于在 import 时就要去拿 `Path.cwd()`、创建一个 `ShellSession`——
只为了填一个生成描述时**根本不会读**的字段。所以存的不是函数，而是**"怎么绑出函数"**。

### 6.2 一张表，两个派生

`tools.py` 改完之后的全部内容：

```python
"""The tools the agent may call, and the schema the model is shown.

The wording in `TOOL_SCHEMAS` is not decoration. Chapter 3 measured what
changes when it changes, and what does not:

  - Saying "relative to the repository root" fixed the path both models got
    wrong -- but only while the example in the description happened to be the
    answer. The rule is enforced in `paths.resolve()` for that reason.
  - Saying "commands are killed after 30 seconds" changed nothing at all: six
    runs out of six across both providers sent a two-minute command anyway.
    The sentence stays because it costs almost nothing and helps a human
    reading the request log, but the thing that actually redirects the model
    is the error message it gets after the kill.
  - Saying "do not use this to read a file" on `run_shell` was measured and
    made no difference here, because `read_file` and `run_shell` already say
    what they are. It is not added. Two tools whose names did NOT say what
    they were (`fetch_content` / `get_text`) did need it -- both providers
    picked the wrong one 3/3 until each description said what it was not for.
"""

from __future__ import annotations

import asyncio
import functools
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from minicodex.agent_types import ToolFn
from minicodex.patch import Edit, apply_edits
from minicodex.paths import resolve
from minicodex.shell import DEFAULT_TIMEOUT, ShellSession
from minicodex.shell import run_shell as _run_shell
from minicodex.tool_errors import tool_error


@dataclass(frozen=True)
class ToolContext:
    """Everything a handler needs that belongs to one conversation.

    Both fields already existed as loose arguments threaded through
    `functools.partial`; this only gives them a name.  `root` decides whether
    a path is inside the repository, `shell` carries the working directory
    across calls.  Neither belongs to the process, which is why neither is a
    module-level constant.
    """

    root: Path
    shell: ShellSession


@dataclass(frozen=True)
class ToolSpec:
    """One tool, described once.

    Before this existed there were two tables: a `{name: handler}` dict and a
    hand-written list of schemas, with nothing tying them together.  They met
    only in `__main__`, as two separate arguments, and disagreeing was silent
    in both directions -- a handler with no schema is never called, and a
    schema with no handler makes the model receive the "no tool named X" error
    that chapter 0 wrote for *hallucinated* tool names.

    `bind` rather than a ready-made handler because the two halves have
    different lifetimes.  The description and the parameters are static: the
    same for the whole process, and needed at import time to build
    `TOOL_SCHEMAS`.  The handler is per-conversation, because it closes over a
    repository root and a shell session.  Storing a bound handler here would
    drag `Path.cwd()` and a `ShellSession` into import time to satisfy a field
    that schema rendering never reads.
    """

    name: str
    description: str
    parameters: dict[str, Any]
    bind: Callable[[ToolContext], ToolFn]

    def schema(self) -> dict[str, Any]:
        """Exactly the shape both providers expect, and the only place it is built."""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


async def read_file(root: Path, args: dict[str, Any]) -> str:
    """Read a UTF-8 text file from inside the repository.

    `Path.read_text()` blocks, and a blocking call inside `async def` stops the
    whole event loop rather than just this task.  With one tool at a time nobody
    notices; once chapter 8 runs tools concurrently the concurrency quietly
    turns into a queue.  Ruff's ASYNC240 rejects the direct call, which is why
    those rules are enabled.

    `errors="replace"` for the same reason chapter 2 put it on the subprocess
    output: a file with one bad byte should come back slightly wrong, not as
    an exception that discards the whole read.
    """
    path, error = resolve(args.get("path"), root)
    if error is not None:
        return error
    assert path is not None
    return await asyncio.to_thread(_read, path)


async def apply_patch(root: Path, args: dict[str, Any]) -> str:
    """Replace an exact block of text in one or more files.

    The `edits` list is validated in full before anything is written --
    measured in chapter 4: writing as it goes leaves the repository
    half-edited when the third of five hunks does not match.
    """
    raw = args.get("edits")
    if not isinstance(raw, list):
        return tool_error(
            'apply_patch needs an "edits" argument, a list of objects',
            you_sent=repr(args.get("edits")),
            do_this=(
                'Example: {"edits": [{"path": "src/a.py", '
                '"old_text": "    return 1", "new_text": "    return 2"}]}'
            ),
        )

    edits: list[Edit] = []
    for index, item in enumerate(raw, 1):
        if not isinstance(item, dict):
            return tool_error(
                f"edit {index} is not an object",
                you_sent=repr(item),
                do_this='Each edit needs "path", "old_text" and "new_text".',
            )
        missing = [k for k in ("path", "old_text", "new_text") if not isinstance(item.get(k), str)]
        if missing:
            return tool_error(
                f"edit {index} is missing {', '.join(missing)}",
                you_sent=repr(item)[:200],
                do_this='Each edit needs "path", "old_text" and "new_text", all strings.',
            )
        edits.append(Edit(item["path"], item["old_text"], item["new_text"]))

    return await asyncio.to_thread(apply_edits, edits, root)


def tool_specs(timeout: float = DEFAULT_TIMEOUT) -> list[ToolSpec]:
    """Every tool the agent has, described once.

    This list is the only place a tool is declared.  `tool_schemas()` renders
    it for the model and `default_tools()` binds it for the loop, so the two
    cannot drift: adding a tool here makes it both visible and runnable, and
    there is no longer a second place to forget.

    The timeout is still a parameter rather than a number typed into the
    string. Chapter 2 shipped `f"...killed after {30} seconds"` next to
    `DEFAULT_TIMEOUT = 30.0`, which agreed only because both were written on
    the same afternoon; nothing would have caught the day someone changed one
    of them. There is one number, and a test asserts the sentence matches it.
    """
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
                                        "Path to the file, relative to the repository "
                                        "root. Example: src/minicodex/model.py"
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


def default_tools(root: Path | None = None) -> dict[str, ToolFn]:
    """Bind every spec to one repository and one shell session.

    A fresh `ShellSession` per call, because its state (cwd, env) belongs to
    one conversation and not to the process.
    """
    context = ToolContext(root=(root or Path.cwd()).resolve(), shell=ShellSession())
    return {spec.name: spec.bind(context) for spec in tool_specs()}


def tool_schemas(timeout: float = DEFAULT_TIMEOUT) -> list[dict[str, Any]]:
    """What the model is shown.  Rendered from the same list, never written twice."""
    return [spec.schema() for spec in tool_specs(timeout)]


# Kept as a module-level name because `__main__` and the tests both want the
# default, and calling it once here is cheaper than threading it through.
TOOL_SCHEMAS: list[dict[str, Any]] = tool_schemas()

__all__ = [
    "TOOL_SCHEMAS",
    "ToolContext",
    "ToolSpec",
    "default_tools",
    "read_file",
    "tool_error",
    "tool_schemas",
    "tool_specs",
]
```

逐块说：

> - **`ToolContext`**：一次对话里工具需要的全部东西——`root` 和 `shell`。这两个值本来就存在，只是以零散参数的
>   形式被 `functools.partial` 传来传去。**这里只是给它们起了个名字。**
> - **`ToolSpec`**：一个工具的完整声明。`bind` 是一个函数：给它一个 `ToolContext`，它返回绑好的工具函数。
>   `schema()` 把名字、描述、参数拼成发给模型的格式——**这是唯一一个构建这种格式的地方**。
>   docstring 写清了为什么存 `bind` 而不是绑好的函数（§6.1）。
> - **`tool_specs(timeout)`**：**唯一的工具清单**。每个工具一个 `ToolSpec`，`bind=lambda ctx: functools.partial(...)`
>   说明它需要哪个会话对象：`read_file` 和 `apply_patch` 要 `root`，`run_shell` 要 `shell`。
>   参数描述是从原来的 `tool_schemas` 原样搬过来的，一个字都不能改（§7.1 会验证）。
> - **`default_tools(root)`**：造一个 `ToolContext`，然后 `{spec.name: spec.bind(context) for spec in tool_specs()}`。
> - **`tool_schemas(timeout)`**：`[spec.schema() for spec in tool_specs(timeout)]`。

**注意最后这两个函数现在各自只有一两行。** 它们不再"各自维护一份清单"，而是**同一份清单的两种投影**。
§4 那两种事故**在结构上就不可能了**：在 `tool_specs()` 里加一个条目，它同时变得可见、可执行；不加，两边都没有。
**没有第二个地方可以忘。**

### 6.3 `ToolFn` 放在哪：一支指错方向的箭头

写到这里撞上一个问题。`ToolSpec.bind` 的类型要写 `Callable[[ToolContext], ToolFn]`，所以 `tools.py`
需要 `ToolFn`。而 `ToolFn` 定义在 `agent.py` 里。那就 `from minicodex.agent import ToolFn`？

**这样写能跑，测试也全部通过。** 回头看 §3.1 的图：`agent` 根本不 import `tools`，今天没有环。

但这是一支**指错方向的箭头**：`agent.py` 是最上层的循环，`tools.py` 是下面的零件，**下面不该依赖上面**。
而这种"暂时没事"的反向箭头，正是环的前半截。设想最自然的下一步——有人觉得每次都要从外面传工具表太啰嗦，
给 `Agent` 加个默认值：

```python
class Agent:
    def __init__(self, model, tools=None, ...):
        self.tools = tools if tools is not None else default_tools()   # ← 环成了
```

这一行看起来完全无害。但此刻 `agent → tools → agent` 闭合，import 报错，而且报错会指向**碰巧后加载的那个模块**，
和真正的原因隔得很远。这就是 **FA-04**。

解法第 1 章给过了：**共享的类型往下沉，不要横着依赖。** 当时 `ToolCall` 就是这么处理的。`ToolFn` 走同一条路，
`agent_types.py` 的全部内容：

```python
"""Types shared by the agent and the history.

Extracted from `agent.py` for one reason only: `history.py` needs `ToolCall`,
and `agent.py` needs `History`.  Leaving `ToolCall` where it was would make the
two modules import each other.

This is the smallest possible answer to a circular import -- move the shared
thing down, not sideways.  Interlude B deals with a case where the answer is
not this easy.

`ToolFn` arrived here later, by the same rule and before it caused any trouble.
`tools.py` needs to say what a handler is; `agent.py` already said it.  Having
`tools.py` import from `agent.py` would have worked -- there is no cycle today,
because `agent.py` never imports `tools.py` -- but it would point the arrow the
wrong way: `agent.py` is the loop on top, `tools.py` is machinery underneath,
and underneath is not allowed to depend on on-top.  A dependency that is merely
backwards is the one that becomes a cycle later, when somebody adds the
matching import from the other side and finds it already half-built.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

# What every tool handler looks like once its context is bound: arguments in,
# text out, never raising.  `agent.py` enforces the "never raising" half.
ToolFn = Callable[[dict[str, Any]], Awaitable[str]]


@dataclass(frozen=True)
class ToolCall:
    """One tool call, in the form the agent can act on.

    `arguments` is the parsed object, or None if the model sent something that
    was not a JSON object.  `raw_arguments` is what it actually sent, kept so
    the error message can quote it back -- and, since chapter 1, so the history
    can be re-serialised without a lossy round trip.
    """

    call_id: str
    name: str
    arguments: dict[str, Any] | None
    raw_arguments: str
```

`agent.py` 里删掉原来的 `ToolFn` 定义，import 改成：

```python
from minicodex.agent_types import ToolCall, ToolFn
```

（`agent.py` 顶部 `collections.abc` 那一行也不再需要 `Awaitable` 和 `Callable`，一并删掉，否则 ruff 会报未使用的 import。）

改完之后，依赖图只多了一条边，方向朝下：

```
agent_types     -> (leaf)          ← 依然是叶子
agent           -> agent_types, history, model, recorder
tools           -> agent_types, patch, paths, shell, tool_errors
```

### 6.4 把这条规则钉成测试

依赖方向这种事，光在 docstring 里写"请不要 import agent"是留不住的。它只有一行，写成测试，加进
`tests/test_boundaries.py`：

```python
def test_FA_04_tools_does_not_import_the_agent() -> None:
    """`tools.py` needed `ToolFn`, and `agent.py` was where it lived.

    Importing it from there would have worked and every test would have
    stayed green, because `agent.py` does not import `tools.py` -- there is no
    cycle *today*.  What there would have been is an arrow pointing the wrong
    way: the loop on top, the tool machinery underneath, and underneath
    depending on on-top.

    That arrow is how the cycle gets built.  The natural next step is to give
    `Agent` a default tool table, at which point `agent.py` imports `tools.py`,
    the ring closes, and the import error blames whichever module happened to
    be loaded second.  `ToolFn` moved down to `agent_types.py` instead, which
    is the same answer chapter 1 reached for `ToolCall`.

    Encoded as a test because chapter 1 said an architecture rule kept only
    in prose will rot, and this is one line to check.
    """
    imports = imported_modules(SRC / "tools.py")
    assert "minicodex.agent" not in imports, (
        "tools.py must not depend on agent.py -- put the shared type in "
        "agent_types.py, as chapter 1 did with ToolCall"
    )
```

它有用吗？变异一下——把 import 改回 `from minicodex.agent import ToolFn`：

```
as written       125 passed, 7 skipped in 15.56s
arrow reversed   1 failed, 124 passed, 7 skipped in 13.72s
```

（Windows 上实测。）

**只有 1 个失败，其余全部通过。** 这恰恰是重点——**除了这个测试，没有任何东西会注意到箭头反了。**
程序照跑，功能照常，唯一的信号就是这一行断言。

```bash
git add .
git commit
```

```
refactor: declare each tool once, derive both tables from it

The handler table and the schema list were two independent declarations of
the same three tools, meeting only in __main__ as two arguments. Dropping a
tool from either side was silent: from the handler table the model gets the
"no tool named X" error chapter 0 wrote for hallucinated names, and from the
schema it simply never learns the tool exists.

ToolFn moves to agent_types.py so tools.py does not depend on agent.py.

Not done: the argument validation in apply_patch is still hand-written. That
is the first occurrence, not the third.
```

---

## §7 怎么证明行为没变

改完了。现在轮到 **FA-01**——怎么保证这次重构没有顺手改坏什么。答案不是"我保证"，是**机制**。三道。

### 7.1 第一道：描述逐字节相同

§5.4 存的快照派上用场了。模型每一轮看到的字节，必须和改之前**一模一样**：

```
$ uv run python -c "import json, minicodex.tools as t; print(json.dumps(t.TOOL_SCHEMAS, indent=2, ensure_ascii=False))" > after.json
$ diff tests/fixtures/tool_schemas.json after.json
$
```

**没有输出，就是没有差异。**（`after.json` 用完删掉。Windows 上可以用 `fc` 代替 `diff`，或者直接看
`test_FA_02_the_entire_schema_is_pinned_not_only_its_descriptions` 是否通过——它做的是同一件事。）

为什么这道特别重要？第 3 章测过：**描述里改一个例子，两家就从全对变成全错。** 描述里任何一个字节的变化都是行为变化。
这一道拦的是"重构时顺手把描述整理了一下"。

### 7.2 第二道：整轮对话逐条相同

字节相同只说明"工具清单"没变，不能说明**循环本身**没变。金标准记录管这个：三轮请求、每一条消息、最终答案、
停止原因、用了几轮、磁盘上的文件——全部对得上。

### 7.3 第三道：全部测试

```
$ uv run pytest
125 passed, 7 skipped in 11.53s
```

（Windows 上实测。）

### 7.4 有一样东西确实变了

三道都过了，但**我得主动说一件它们没拦住的事**——纯重构的诚实，不在于"什么都没变"，而在于**变了的东西你说出来了**。

执行表字典的**插入顺序**变了：

```
was : ['read_file', 'run_shell', 'apply_patch']
now : ['read_file', 'apply_patch', 'run_shell']
```

原因很简单：现在只有一张表，它按描述的顺序排（描述的顺序不能动，§7.1 那一道是逐字节的）。一张表没法同时有两种顺序。

**这个变化能被观察到吗？** 去数这个字典被用到的每一处——一共三处，都在 `agent.py`：

```python
self.tools = tools or {}                            # 存下来
if call.name not in self.tools:                     # 判断在不在
available = ", ".join(sorted(self.tools)) or "(none)"   # 排过序
```

赋值、`in` 判断、`sorted()`。**没有任何一处依赖插入顺序。**

所以结论是"观察不到"，但要注意这个结论的性质：它不是三道机制**证明**出来的，是我**去数了一遍调用的地方**得出来的。
如果哪天有人在 `agent.py` 里按字典顺序遍历工具，这三道机制**不会**提醒这里有隐患。

> **纯重构的产出不只是"测试全绿"，还包括一句"X 变了，我查过为什么没关系"。**
> 前者是自动的，后者只能靠人。

---

## §8 看到了，但没有改的三件事

这是这一章最难的一节，因为**重构最难的部分是停手**。重构时注意力调到了最敏感的档位，满眼都是问题，
每一个都"只要改一点点"——加在一起，就是 FA-01 说的"藏在大改动里的行为变化"。

| 看到的 | 为什么不改 |
|---|---|
| **`system_prompt()` 到现在都没有人调用。** `__init__.py` 定义了它，`prompts/system.md` 也随包发布了，但 Agent 从来没发过一条 system 消息 | **这是行为变更，不是重构。** 加上它，模型收到的东西就变了，§7 那三道会全部失败——而且它们**应该**失败。系统提示是后面专门的一章 |
| **`apply_patch` 手写了三十多行参数检查**，一层层检查 `edits` 是不是列表、每项是不是字典、三个字段在不在。下一个参数复杂的工具必然复制粘贴 | **三次法则：这是第一次。** 现在就抽，抽出来的一定是照着 `apply_patch` 的样子长的 |
| **`_collect` 是在组装流，不是循环逻辑**，`agent.py` 自己的 docstring 都承认边界是猜的 | **它只有一个调用的地方。** 挪动它不会减少任何耦合，只会让改动变长。按 §3 的四问，它一条都不满足——**用自己刚定的标准判自己** |

第三条值得多说一句。`agent.py` 开头的 docstring 是这么写的：

> The boundaries between stream assembly, tool dispatch and the loop are
> guesses right now... **Interlude A pays this off**, once there are enough
> call sites for the boundaries to be observed rather than invented.

**这一章就是 Interlude A，而这笔账没有还。** 因为条件没到——那句话的后半段说得很清楚："等到调用的地方
多到边界可以被**观察**出来，而不是被**发明**出来"。`_collect` 仍然只有一个调用的地方。

> 一句写在代码里的"以后再说"，到期时该做的是**重新评估**，不是**无条件执行**。

### 8.1 原本在这里发现的三个问题

这本书最初的版本里，这一章还撞上了三个从前面带过来的问题：`.minicodex/` 没有被 git 忽略、
shell 工具在 Python 3.11 上超出上限时永远卡住、以及在 Windows 上 7 个测试莫名失败。它们是在
"为重构做准备"——换一个 Python 版本、换一台机器、跑完之后看一眼 `git status`——的过程中被翻出来的。

改写这本书时，它们已经分别在第 0 章和第 2 章处理了：读者不该带着一个会卡死的工具走过三章。
`FAULTS.md` 里仍然记着它们最初是在哪里被发现的。

这件事本身值得记住：

> 重构本身可能一个 bug 都改不掉。但**为了安全地重构而必须先做的那些准备**——量依赖、数覆盖、
> 换环境跑一遍——常常会翻出一堆。

### 8.2 README

`steps/step04_apply_patch/README.md` 的第一行至今写着 **"step 1: the protocol layer"**。文档从第 1 章之后就
停止更新了，而**没有任何东西发现**——因为没有任何东西会去测文字。这里不回头修改前面几步的，
只把这一步的写对。`README.md` 的全部内容：

````markdown
# minicodex — interlude A: the first refactor

No new features. The same agent as step 4, with one duplicated declaration
removed, and tests that prove the behaviour did not change.

```bash
uv sync --all-extras
uv run pytest

# Recorded responses, no GPU and no key needed (run the stub in a second
# terminal, or append `&` on macOS/Linux):
uv run minicodex serve-stub
uv run minicodex ask "What does src/minicodex/__init__.py define?" \
    --base-url http://127.0.0.1:11435/v1
```

## What changed

| Path | What changed |
|---|---|
| `src/minicodex/tools.py` | `ToolSpec` — a tool is declared once; the schema list and the handler table are both derived from it |
| `src/minicodex/agent_types.py` | `ToolFn` moved down here, so `tools.py` never has to import `agent.py` |
| `tests/test_characterization.py` | new: what the code did *before* the refactor, so "unchanged" is checkable |
| `tests/fixtures/tool_schemas.json` | the exact bytes the model is shown |
| `tests/fixtures/golden_transcript.json` | every request body of a three-turn run |
| `tests/test_boundaries.py` | `tools.py` may not import `agent.py` |

## Why `ToolSpec`

There were two tables. `default_tools()` returned `{name: handler}`;
`tool_schemas()` returned a separately hand-written list of schemas. Nothing
tied the names together, and they met only in `__main__`, as two arguments.

Disagreeing was silent in both directions:

- **handler missing** — the model calls the tool and is told
  `Error: no tool named 'apply_patch'`, which is the message chapter 0 wrote
  for a model that *invents* a tool name. A wiring mistake arrives wearing the
  model's clothes.
- **schema missing** — the model is never told the tool exists, so it is never
  called, and nothing anywhere reports it.

Three mutations of `default_tools()` — rename a key, drop an entry, add an
entry — each left all 120 tests green. `default_tools` was mentioned by
exactly zero of them. With the characterization tests: 3, 2 and 1 failures.

## Found along the way, fixed earlier

The first edition of this interlude also found three inherited problems:
`.minicodex/` was not ignored by git, the shell tool's read ceiling hung
forever on Python 3.11+, and seven tests failed on Windows without saying
why. The rewrite fixes them in chapters 0 and 2; `FAULTS.md` still records
where they were first found.

## Deliberately not done

- `system_prompt()` still has no caller — that is a behaviour change, chapter 13
- `apply_patch` still validates its arguments by hand — first occurrence, so
  not yet an abstraction
- `_collect` still lives in `agent.py` — one call site; moving it would be
  ceremony
- `run_shell` is still POSIX-only (F02-10); on Windows those seven tests skip
  and say so
````

```bash
git add README.md
git commit -m "docs: describe the first refactor in the README"
```

---

## §9 回顾

| 编号 | 问题 | 结果 | 怎么暴露 | 挡住它的东西 |
|---|---|---|---|---|
| FA-01 | 重构"顺手改进"了行为，藏在大改动里 | 🛡 用机制防住（§7） | 🟣 审查 | 描述逐字节相同 + 整轮对话逐条相同；变了的顺序主动说明 |
| FA-02 | 测试全绿，只因为覆盖很薄 | ✅ **强烈复现**：120 个测试对三种改坏都全部通过 | 🟡 | 先补表征测试：3 / 2 / 1 个失败 |
| FA-03 | 一次改太多，合并冲突解不开 | ❌ **这里测不了**：一个作者、一个分支、没有并发修改 | —— | 小步提交照做，但如实记为"没法测量" |
| FA-04 | 拆分之后出现循环 import | ⚠️ **没按预期出现，换了个样子出现**：一支暂时没事的反向箭头 | 🟣 | 共享类型下沉 + 测试 |
| *新* | 每一步的 README 还写着 "step 1" | ⚠️ | 🟠 看文件时发现 | 只修这一步的 |

标记：🔴 崩溃 · 🟡 静默 · 🟢 主动边界测试 · 🔵 长时间运行才出现 · 🟠 看日志发现 · 🟣 代码审查 ·
⚫ 用户报告 · ⚪ lint/类型检查

计划里的"拆掉臃肿的 `agent.py`"**被测量取消了**：212 行，依赖图已经是一棵干净的树。真正的债是另一个——
在任何行数统计里都看不见。

---

## §10 交给 GitHub

```bash
git push -u origin refactor/tool-specs
```

开 PR、等 CI、自己审查。这一章的提交：

```
docs: describe the first refactor in the README
refactor: declare each tool once, derive both tables from it
test: characterize the tool tables before moving them
```

**顺序是有讲究的**：先补测试（任何人都可以切到这里，亲眼看到 3 / 2 / 1 个失败），再重构（安全网已经架好），
最后是和前两个无关的文档。每一个都能单独通过测试，每一个都能单独撤销。

**合并时要不要把它们压成一个（squash）？不要。** 这几个提交的顺序本身就是这一章的论点：**先补网，再动刀。**
压成一个就把这个信息扔掉了。

### 10.1 审查时提出的问题

**1 · `bind` 用 lambda，出错时堆栈里全是 `<lambda>`，不好查。**

> **回答**：说得对，这是真实的代价。没改的理由是**目前它不可能出错**——`bind` 只做一次 `functools.partial`，
> 不含任何逻辑。真正运行起来会出错的是被包住的那个函数，它有正常的名字。哪天 `bind` 里开始有逻辑，
> 就该换成有名字的函数。**记在这里。**

**2 · `ToolContext` 只有两个字段，是不是过早抽象？直接传 `(root, session)` 也行。**

> **回答**：想过，结论是**它不算新抽象，只是给已有的东西起名字**。这两个值本来就被 partial 传来传去，
> 字段数没变、寿命没变。检验标准是：删掉它会怎样？`bind` 的签名变成 `Callable[[Path, ShellSession], ToolFn]`，
> 每个工具还是得写全两个参数，只是少了个名字。**换不来什么。** 但请注意，这里**没有**给它加
> "以后审批功能要用"之类的字段——那才是过早抽象。

**3 · `tool_schemas()` 每次调用都要重新构造全部 `ToolSpec`，比原来贵。**

> **回答**：确实。但 `TOOL_SCHEMAS` 是模块级的，**整个程序只算一次**；`default_tools()` 一次对话调一次。
> 这是微秒级的一次性开销。拿"每加一个工具就少一处可能忘记的地方"去换，很划算。
> **哪天真成了热点，缓存是三行的事，两张表却不是。**

**4 · 金标准记录把 `Error: no such file...` 这句原文钉死了，以后改错误信息的措辞，这个测试就会失败。**

> **回答**：对，**这正是我要的**。第 3 章测过，错误信息就是给模型的提示——同一个失败，一句话的错误信息
> 弱模型 3/3 救不回来，三段式的 3/3 救回来了。**错误信息的措辞是行为**，改它就应该有东西失败一下。
> 代价是以后改措辞要重新生成这个文件；收益是没人能在"顺手润色一下"时悄悄改掉模型的行为。

---

## §11 本章给 CI 加了什么

只加了一条：**依赖方向检查**（§6.4）。它跟着 `pytest` 一起跑，不需要新的 CI 步骤。

**没有加**"整个包不许有环"的通用检查：现在没有环，这条规则**还没被违反过一次**。第 -1 章的原则——
CI 的检查按证据增长——在这里同样适用。等真的出现一个环，再把它推广。

同样没有加"每个模块不许超过 N 行"：**这一章的结论恰恰是行数不是触发条件**，配一个按行数拦截的检查，
等于把刚推翻的东西写进 CI。

---

## §12 三条主线各自留下了什么

### 主线 A · 需求变代码

**重构的触发条件是耦合，不是行数。** `agent.py` 212 行，不用拆；真正的债——一个工具要在两个地方各声明一次——
在任何行数统计里都看不见。

**把寿命不同的东西分开。** 描述是静态的，执行函数是跟着对话走的；所以 `ToolSpec` 存的是"怎么绑"，不是"绑好的"。

### 主线 B · 工程化交付

| 动作 | 本章的规则 |
|---|---|
| 纯重构 | 不夹带任何行为改动；看到的问题记下来 |
| 提交顺序 | 先补测试，再重构；无关的改动单独提交 |
| squash | 提交顺序本身有意义时，不要压成一个 |
| 行为变了的地方 | 主动说出来，并说明为什么没关系 |
| 代码里的"以后再说" | 到期时重新评估，不是无条件执行 |

### 主线 C · 故障

**第一招：重构之前，先证明你的测试能发现问题。** 不是"跑一遍看绿不绿"——它当然是绿的。是**故意改坏，看它失败不失败**。
120 个测试对三种改坏的回答都是"全部通过"。**在那一刻，那 120 个绿点对这次重构什么也不保证。**

**第二招：表征测试记录的是现状，不是对错。** 先记下来，才谈得上"变没变"。

**第三招：能自动化的证明要自动化，不能自动化的要说出来。**

---

## 如果你只记住三件事

1. **重构的触发条件是耦合，不是行数。** 排期上写着"这周重构"不是重构的理由；为了交差造出一条本来不存在的边界，
   比不重构更贵。
2. **重构之前，先证明你的测试能发现问题。** 故意改坏，看它们失败不失败。
3. **纯重构的产出，包括一份"改了什么、为什么它不算行为变化"的说明。** 描述字节、对话记录、测试都能自动证明；
   字典顺序变了没关系这件事，是去数了三个调用的地方才敢说的。

---

## 动手

```bash
cd steps/stepA_refactor
uv sync --all-extras
uv run pytest
```

**建议自己做一遍的三件事：**

1. **把 §5 的顺序倒过来做一遍。** 先做 `ToolSpec` 重构，再补表征测试，然后跑那三个变异。测试会全部通过——
   因为你记录的是新行为。**然后想清楚：这种情况下，那些绿点到底证明了什么？**
2. **给 `tool_specs()` 加第四个工具**（比如 `list_files`），故意只写一半——`ToolSpec` 建好了但忘了放进返回的列表。
   看哪个测试会失败。然后回头看 §4.1，**判断这次重构到底消灭了哪一类错误、又没消灭哪一类。**
   （提示：没消灭的那一类仍然存在，只是从两处变成了一处。）
3. **在你自己的项目里跑一遍 §3.1 那段依赖图脚本。** 如果出现了环，先别急着修——**先去数这个环里的模块被多少个测试覆盖着。**

---

## 选读 · codex 是怎么做的

> 基于写作时（2026 年）的 codex 仓库，以后可能会变。不读不影响后面的内容。

**codex 也没有一次拆干净。** 仓库里 `multi_agents/` 和 `multi_agents_v2/` **同时存在**——先写了第一版，发现不行，
写了第二版，**但没有直接删掉第一版**。旧路径长期共存，迁移是逐步的。这和"重构就要一步改干净"的想象差很远，但它是真实的。

**codex 的 `AGENTS.md` 里有一条："模块超过 800 行就别再往里加"。** 这和本章"行数不是触发条件"看起来矛盾，其实处理的问题不同：
800 行那条是**上限**，在一个很大、很多人的仓库里，防止某个模块变成谁也不敢碰的黑洞——它是**止损线**，不是**触发线**。
本章问的是"该不该重构"，那是触发线的问题。**上限可以用行数，触发线不能。**

**codex 的 CI 里有一个 `verify_tui_core_boundary.py`**，专门检查界面层不许直接 import 核心层。和 §6.4 是同一个思路：
**架构边界靠 CI 强制，不靠口头约定。**

---

**下一章**：[审批与沙箱](ch05-approval.md)——Agent 现在能改任何文件、跑任何命令，包括 `rm -rf`。

---

# 附录 · 对照检查

## T1 · 每个文件是在哪一节改的

本章结束时，你的项目内容应该和 `steps/stepA_refactor/` 一致（测试函数的先后顺序可以不同）。

| 文件 | 在哪改的 |
|---|---|
| `tests/test_characterization.py` | §5.4 |
| `tests/fixtures/tool_schemas.json` | §5.4（用命令生成） |
| `tests/fixtures/golden_transcript.json` | §5.4（由测试生成，人工确认） |
| `src/minicodex/tools.py` | §6.2 |
| `src/minicodex/agent_types.py` | §6.3 |
| `src/minicodex/agent.py` | §6.3（删掉 `ToolFn`，改 import） |
| `tests/test_boundaries.py` | §6.4 |
| `README.md` | §8.2 |

## T2 · 常见报错对照表

| 症状 | 原因 | 修法 |
|---|---|---|
| 加了工具，模型看不到 | 只改了一张表 | 只在 `tool_specs()` 里加 `ToolSpec`，两张表都从它派生 |
| `test_FA_02_the_entire_schema_is_pinned_...` 失败 | 描述里某个字节变了 | 如果是有意的，用 §5.4 的命令重新生成快照；如果不是，改回去 |
| `test_FA_02_a_whole_run_is_pinned_...` 失败 | 循环的行为或错误信息的措辞变了 | 确认是不是有意的；有意的就重新生成金标准记录并逐条检查 |
| `test_FA_04_tools_does_not_import_the_agent` 失败 | `tools.py` 从 `agent.py` import 了东西 | 把共享的类型放进 `agent_types.py` |
| ruff 报 `F401` 未使用的 import | `ToolFn` 搬走后，`agent.py` 里的 `Awaitable`、`Callable` 没删 | 删掉它们 |
| 在 import 时就创建了 `ShellSession` | `ToolSpec` 存了绑好的函数 | 存 `bind`，在 `default_tools()` 里才绑 |
