# 第 3 章 · 工具描述工程

> **代码**：`steps/step03_tool_descriptions/`
> **分支**：`feat/tool-descriptions`
> **产出**：模型第一次就把参数传对；传错的时候，第二次能自己纠正
> **你需要**：Ollama（同前）。有 OpenAI key 的话这一章收获会翻倍——**本章最重要的
> 几个结论全部来自两家的差异**，只跑一家会得出错误的结论。

---

## §1 这一章要做出来的东西

前两章做的是"工具能不能正确执行"。这一章做的是**"模型能不能正确调用它"**——
这是完全不同的一件事，而且没有一行代码能直接决定它。

模型看到的不是你的 `read_file` 函数，是这段 JSON：

```json
{"name": "read_file",
 "description": "Read a UTF-8 text file and return its contents.",
 "parameters": {"properties": {"path": {"description": "Path to the file."}}}}
```

**这段文字就是这一章的全部战场。** 它是 prompt，不是文档——每一个词都在参与
一次推理。

问题在于：你没法读代码读出"这句话写得好不好"。**只能测。**

---

## §2 这一章的方法论：先列清单，再一条条打掉

写这一章之前，我按经验和 codex 源码列了一张十条故障的清单（`FAULTS.md` 里的
F03-01 到 F03-10）。听起来很扎实。

然后我实测了。**十条里，只有三条复现了。**

这不是清单没用——是**清单不能照抄**。如果我按清单写代码，会为七个在当前模型上
根本不发生的问题写一堆防御逻辑，还会漏掉三个清单里压根没有的真问题。

> 这一章的元教训，比任何一条具体故障都重要：
> **别人的故障清单（包括这本书的）是假设，不是事实。用之前先在你自己的模型上打一遍。**

方法：每条故障构造一个 A/B——同一个问题、同一个模型，两种措辞，各采样三次。

---

## §3 探测脚本，以及它自己的三个 bug

`probe_descriptions.py`，stdlib 单文件。跑之前先在沙箱里用一个假服务器打了一遍，
假服务器循环返回各种畸形响应（绝对路径、不合法 JSON、发明字段、干脆不调工具）。

这一步逮出了脚本自己的三个问题，每一个都会污染真实数据：

**1. HTTP 失败被归类成了"模型选择用散文回答"。**

```python
except urllib.error.HTTPError:
    return {}          # 然后 first_call({}) 返回 None，被打印成"没有工具调用"
```

网络抖一下，就会被记成一条模型行为。改成一个独立的 `Failed` 类型：

```python
class Failed:
    """The request did not come back. Deliberately not the same thing as "the
    model chose not to call a tool" -- collapsing those two into one bucket
    turns a flaky network into a finding about the model."""
```

**这个改动在第二轮真的救了命**，后面 §5.4 会讲。

**2. 分类器只看预期工具的字段。** 如果模型调了别的工具，`note` 函数还是拿
`run_shell` 的字段去判断，把正常的 `path` 报成"发明的字段"。

**3. JSON 解析失败被算进了"发明字段"。** 那是解析失败，不是发明字段。

> **能在假数据上先打一遍的脚本，就不要拿真数据去打。** 真数据贵——用户的时间、
> API 的钱、模型的排队。而且假数据能构造出你在真实采样里几百次都碰不到一次的畸形
> 输入。

---

## §4 第一轮：七个场景，五个没复现

Ollama `gemma4:31b-cloud` 和 OpenAI `gpt-4o-mini`，各三次采样。

### 4.1 路径基准（F03-02）——复现了，而且两家错法不同

问题：`What does the model module define?`（模型得自己构造出
`src/minicodex/model.py` 这个路径）

```
A1  描述："Path to the file."
    Ollama:   path='model.py'                      ×3
    OpenAI:   path='/home/dev/minicodex/model.py'  ×3

A2  描述："...relative to the repository root -- never absolute.
          Example: src/minicodex/model.py"
    Ollama:   path='src/minicodex/model.py'        ×3
    OpenAI:   path='src/minicodex/model.py'        ×3
```

**两家在 A1 都错了，但错的方向相反。** Ollama 给相对路径（但少了两层目录），
OpenAI 直接从 system prompt 里抓了仓库路径拼成绝对路径。

> 这是 Ch01 那条主线的新形式。Ch01 是"宽松的服务端掩盖了坏历史"，这次是
> **"一家供应商的行为习惯，会让你对模型形成错误的认知"**。只对着 Ollama 开发，
> 你会得出"模型总是给相对路径"，然后代码里不处理绝对路径——换 OpenAI 全崩。

### 4.2 幻觉字段（F03-03）——没复现，而且没复现的方式很有意思

问题："跑测试，但超过 5 秒就放弃"。schema 里**只有** `command` 一个字段。

我预期模型会传 `{"command": "pytest", "timeout": 5}`。实际：

```
Ollama:   command='timeout 5s pytest'                          ×3
OpenAI:   command='timeout 5s pytest'                          ×2
          command='pytest --maxfail=1 --disable-warnings --timeout=5'  ×1
```

**两家都没有发明字段，而是把意图编码进了现有字段。** 模型比我预期的守规矩得多——
它用 shell 自己的 `timeout` 命令达成了目的。

`additionalProperties: false` + `strict: true` 的 B2 变体和 B1 **完全没有差别**，
因为根本没有多余字段需要被拒绝。

### 4.3 enum（F03-06）、重叠工具（F03-05）、参数命名（F03-01）、多行转义（F03-09）

四条，两家，两种写法，**全部没有差别**：

- enum 用散文描述 vs 真 `enum`：都给出 `"append"`，都合法
- `read_file` vs `run_shell` 读文件：都选 `read_file`
- `write_file(filename)` vs `write_file(path)`：都用了各自 schema 声明的那个名字
- 多行 Python 代码：都产出合法 JSON、正确的换行（11-12 行）

### 4.4 错误信息即 prompt（F03-07）——强复现，两家结果相反

这是本章最重要的一次测量。构造一个已经失败的调用（模型传了绝对路径），然后给它
**两种不同的错误信息**，看它第二次怎么做：

```
D1  错误信息："ValueError: invalid path"
    Ollama:   path='/home/dev/minicodex/model.py'   ×3   仍然是绝对路径
    OpenAI:   path='src/minicodex/model.py'         ×3   改对了

D2  错误信息："Error: path must be relative to the repository root, not
              absolute. You sent: /home/dev/minicodex/src/minicodex/model.py.
              Send this instead: src/minicodex/model.py"
    Ollama:   path='src/minicodex/model.py'         ×3   改对了
    OpenAI:   path='src/minicodex/model.py'         ×3   改对了
```

看 Ollama 在 D1 的重试细节：原来失败的是
`/home/dev/minicodex/src/minicodex/model.py`，重试的是
`/home/dev/minicodex/model.py`。

**它删掉了两层目录。** 它不是不想改，它在努力改——只是 `ValueError: invalid path`
没告诉它"绝对路径不行"，于是它猜错了方向，猜成了"路径太深"。

> **如果你只用强模型开发，你会得出"错误信息怎么写都行"的结论。** 换个弱一点的
> 模型，同一份代码会陷进死循环——每一轮都重试同一个错误，直到撞上 Ch00 的轮次
> 预算，产出一段基于错误信息瞎编的答案。
>
> Ch00 的轮次预算兜住了这个场景。**但"兜住"不等于"解决"。**

---

## §5 第二轮：把没复现的场景全部加难

五条没复现，有两种可能：一是这些故障在当前模型上确实不发生，二是**我的场景太好猜了**。

C 场景问"想在末尾加一行"——`append` 几乎是唯一答案；E 场景 `read_file` vs
`run_shell` 名字已经自明。得排除第二种可能才能下结论。

### 5.1 enum 值改成猜不出来的（I）

`u` / `c` / `n` 三个 diff 格式缩写，问题只说"像 git 那样显示"：

```
I1 散文描述:  Ollama: format='u'  ×3     OpenAI: format='n'  ×3
I2 真 enum:   Ollama: format='u'  ×3     OpenAI: format='n'  ×3
```

散文和 enum **仍然没有差别**。但这次有个我的分类器没抓到的东西：

**我的脚本把两家都标成了 `[valid]`——因为 `u` 和 `n` 都在合法集合里。可 git 用的
是 unified diff，正确答案是 `u`。OpenAI 选的 `n` 是错的**，它被 "normal" 这个词
字面误导了（`n` 是 normal format，传统 diff 格式，不是 git 用的那种）。

> **enum 保证值合法，不保证值正确。** 而我的分类器只检查了合法性——
> **只看分类标签不看原始数据，就会漏掉这个。** 这是本书第三次撞到"测量工具本身
> 有盲区"（Ch02 是 pytest 的 stdin，§4.1 是"绝对/相对"的粒度太粗）。

### 5.2 两个说不清自己是干什么的工具（J）——"不要用于"唯一复现的证据

`fetch_content(target)` 和 `get_text(item)`，描述都含糊：

```
J1 两个描述都含糊:
   Ollama: fetch_content ×3      OpenAI: fetch_content ×3     ← 两家都选错

J2 各自加一句"不要用于 X":
   "Retrieve the content of a remote URL over HTTP.
    Do not use this for files on disk -- use get_text for those."
   Ollama: get_text ×3           OpenAI: get_text ×3          ← 两家都改对
```

**干净的复现。** 但要限定结论：第一轮 E 场景（`read_file` vs `run_shell`）没有
复现，因为那两个名字已经说清楚了自己是什么。

> **只有名字本身不足以区分时，才需要"不要用于……"。** 给每个工具都加这句话是在
> 浪费 token——而且描述是每一轮请求都要重发的。

### 5.3 例子的作用是"被抄"，不是"被理解"（M）——推翻了我第一轮的结论

第一轮 A2 加了例子之后两家全对，我当时的结论是"说明基准 + 给例子有效"。

**这个结论是错的。** M 场景把描述文字固定住，只换例子：

```
M1  "Path to the file."（含糊）
    Ollama: model.py                       ×3   错
    OpenAI: /home/dev/minicodex/model.py   ×3   错

M2  "...relative to the repository root -- never absolute.
     Example: src/minicodex/model.py"      ← 例子恰好就是答案
    两家: src/minicodex/model.py           ×3   对

M3  "...relative to the repository root -- never absolute.
     Example: tests/test_agent.py"         ← 同一句话，换个例子
    Ollama: model.py                       ×3   错
    OpenAI: model.py                       ×3   错
```

M2 和 M3 的规则说明**一字不差**。唯一的区别是例子里的文件名。

**M2 的成功不是因为模型理解了"相对于仓库根目录"，是因为它照抄了例子。**

而真实使用中，例子几乎不可能恰好是用户要的那个文件。所以：

> **路径这件事，在描述层解决不了。**
>
> 这正是 PLAN §4.4 那条决策规则：**能在确定性代码里解决的，绝不推给模型。**
> 每次你想在描述里写"请一定要……"，先问一遍这条能不能用代码强制。

### 5.4 描述里的约束对行为零影响（L）——清单上没有的新发现

`run_shell` 的描述里写着 "Long-running or silent commands are killed after 30
seconds."。任务明说要跑两分钟：

```
L1 描述里有这句话:   两家: command='pytest tests/integration'  ×3
L2 描述里没这句话:   两家: command='pytest tests/integration'  ×3
```

**6/6 原样发出，等着被杀。** 有没有这句话，没有任何区别。

把这条和 §4.4 放在一起看，是这一章最有用的一组对照：

| | 效果 |
|---|---|
| **事前**在描述里写约束 | 零 |
| **事后**在错误信息里给指令 | 对弱模型是决定性的 |

而且描述**每一轮都要重发**，错误信息**只在真的出错时发一次**。

> 与其在描述里预防，不如在错误信息里纠正。

### 5.5 两家的"组合工具"策略相反（H）

问"只看 40-60 行"，`read_file` 只有 `path`，没法表达行号：

```
H1 只给 read_file:
   两家: read_file(path='src/minicodex/agent.py')   ×3   读整个文件

H2 额外给一个 run_shell 逃生口:
   Ollama: run_shell("sed -n '40,60p' src/minicodex/agent.py")  ×3
   OpenAI: read_file('src/minicodex/agent.py')                  ×3
```

**更弱的 Ollama 反而更会组合工具**，OpenAI 保守地用最直接的那个，把整个文件读进
上下文。

这也解释了 F03-03 为什么不复现：**模型不发明字段，它们要么凑合，要么换工具。**

### 5.6 K 场景：我自己的 bug，以及那个 `Failed` 类

必填参数埋在五个可选参数后面。第一次跑，两家全部 HTTP 400：

```
K1: !! REQUEST FAILED -- not a model behaviour: HTTP 400:
    "Invalid type for 'tools': expected an array of objects, but got an object instead."
```

我传了 `tool(...)` 而不是 `[tool(...)]`，少了个方括号。**这是我的 bug，不是模型
行为。**

注意它被打印成了 `!! REQUEST FAILED -- not a model behaviour`。**这就是 §3 那个
`Failed` 类**——如果没有那个改动，这 6 次会被记成"模型选择不调用工具"，而我很可能
据此写下"参数一多，模型就不调工具了"这种彻底错误的结论，并且为它写一堆代码。

修好之后：

```
K1 必填参数放最后:  Ollama 3/3 传了    OpenAI 3/3 传了
K2 必填参数放最前:  Ollama 3/3 传了    OpenAI 3/3 传了
```

F03-08 也没复现。但 OpenAI 那几次泄露了别的东西：

```
{"pattern": "asyncio.wait_for", "max_results": 50}
```

描述里写的是 `"Cap on results. Default 50."`，**模型把这个默认值显式传了回来**。
值本身没错——可一旦代码里把默认值改成 100 而描述忘了改，模型会一直传着 50 覆盖掉
新默认值。**比超时那处漂移更隐蔽，因为它永远不会报错。**

---

## §6 不需要模型的那部分：三个确定性发现

有些东西不用问模型，跑一下就知道。

### 6.1 JSON 双重转义是静默的（F03-09 的另一半）

模型写多行代码时，三种写法的命运完全不同：

```python
# 1. 正确转义
'{"content": "line1\\nline2"}'     → 解析成功，2 行

# 2. 双重转义
r'{"content": "line1\\\\nline2"}'  → 解析成功，0 个真换行，
                                      文件里是字面的反斜杠和 n

# 3. 未转义的真换行
'{"content": "line1<真换行>line2"}' → JSONDecodeError:
                                      Invalid control character
```

第三种会被 Ch00 的 `_collect` 挡下（`arguments=None`，返回一条错误给模型）。
**第二种一路绿灯**——文件写出来了，内容是坏的，没有任何一层会报错。

实测两家都转义正确，所以**这一章不为它写代码**。但风险是真的，Ch04 真正开始写文件
时会兑现。

### 6.2 多余字段被静默丢弃

```python
>>> await tools["run_shell"]({"command": "echo hi", "timeout": 5})
'hi\n'
```

模型传了 `timeout: 5`，代码用了默认的 30 秒，**没有任何人告诉模型它的意图被扔了**。

### 6.3 描述里的数字和代码常量没有绑定

Ch02 交付的代码里：

```python
f"commands are killed after {30} seconds."   # tools.py
DEFAULT_TIMEOUT = 30.0                        # shell.py
```

两个 30 一致，纯粹因为是同一个下午写的。**改了任何一个，测试、类型检查、ruff
都不会说话。** 而 §5.6 证明了模型真的会把描述里的数字当真。

---

## §7 现在写代码

十条清单，复现三条。代码只为这三条写，外加三个确定性问题。

### 7.1 `tool_errors.py`：把三段式变成类型错误

F03-07 是最强的复现，先做它。

```python
def tool_error(problem: str, *, you_sent: str | None = None, do_this: str) -> str:
    """Build the three-part message: what went wrong, what was received, what
    to do next.

    `you_sent` is optional because some failures have no input worth quoting
    back (a missing argument, say). `do_this` is not optional.
    """
    parts = [f"Error: {problem}"]
    if you_sent is not None:
        shown = you_sent if len(you_sent) <= 200 else you_sent[:200] + "..."
        parts.append(f"You sent: {shown}")
    parts.append(do_this)
    return " ".join(parts)
```

**`do_this` 是必填的关键字参数。** 少写它是 `TypeError`，不是一条效果差一点的
错误信息。实测证明"没有下一步指令"的错误信息在弱模型上直接导致死循环——
这属于 PLAN §2 的抽象例外第三条：**不变量需要被强制**。

放哪？`tools.py` 和 `shell.py` 都要用，而 `tools.py` 已经 import 了 `shell.py`。
放进任何一个都会造出循环 import。

**这是 Ch01 那个问题的第二次出现，解法一样**：下沉到一个不依赖任何自己模块的
叶子。

### 7.2 `paths.py`：把 F03-02 从描述层搬到代码层

M3 证明了描述层解决不了。那代码层能确定性地解决什么？

- **仓库内的绝对路径**：无歧义，直接接受（OpenAI 的习惯，不用打扰模型）
- **仓库外的绝对路径**：拒绝（这也是 Ch05 安全边界的雏形）
- **只匹配到一个文件的裸文件名**：无歧义，**在错误信息里直接告诉它是哪个**
  （Ollama 的习惯）

最后一条正好就是 D2 里被实测证明有效的那个措辞。

```python
def resolve(raw: str, root: Path) -> tuple[Path | None, str | None]:
    """(path, None) if it resolves to a file inside `root`, else (None, error)."""
    if not isinstance(raw, str) or not raw.strip():
        return None, tool_error(
            'this tool needs a "path" argument, a non-empty string',
            do_this='Example: {"path": "src/minicodex/model.py"}',
        )

    root = root.resolve()
    candidate = Path(raw)

    if candidate.is_absolute():
        try:
            # An absolute path that points inside the repository is not
            # ambiguous -- accept it rather than making the model guess again.
            relative = candidate.resolve().relative_to(root)
        except ValueError:
            return None, tool_error(
                "that path is outside the repository, and this tool only reads "
                "files inside it",
                you_sent=raw,
                do_this=f"Send a path relative to {root.name}/, for example src/minicodex/model.py",
            )
        candidate = relative

    full = (root / candidate).resolve()
    if full.is_dir():
        return None, tool_error(
            "that path is a directory, not a file",
            you_sent=raw,
            do_this="Send the path of a file, or use run_shell with ls to list the directory.",
        )
    if not full.exists():
        near = _candidates(Path(raw).name, root)
        if len(near) == 1:
            do_this = f"Send this instead: {near[0]}"
        elif near:
            do_this = "Did you mean one of these? " + ", ".join(near[:_MAX_SUGGESTIONS])
        else:
            do_this = "Use run_shell with ls or find to see what exists, then try again."
        return None, tool_error("no such file in the repository", you_sent=raw, do_this=do_this)

    return full, None
```

搜索候选时跳过的目录：

```python
_SKIP = {".git", "__pycache__", ".venv", "venv", "node_modules", ".pytest_cache", ".ruff_cache"}
```

`.git` 里的同名文件永远不是模型要的。

拿两家实测发出的真实路径喂进去：

```
what gpt-4o-mini sent (absolute, wrong file):
  Error: no such file in the repository You sent: /tmp/step03_build/model.py
  Send this instead: src/minicodex/model.py

what gemma4:31b sent (relative, wrong file):
  Error: no such file in the repository You sent: model.py
  Send this instead: src/minicodex/model.py
```

**两种错法，收敛到同一条三段式信息**，而且这条信息的形状正是 D2 里被证明有效的
那个。

### 7.3 描述从常量生成

```python
def tool_schemas(timeout: float = DEFAULT_TIMEOUT) -> list[dict[str, Any]]:
    """...
    A function taking the timeout rather than a module-level constant with the
    number typed into the string: chapter 2 shipped `f"...killed after {30}
    seconds"` next to `DEFAULT_TIMEOUT = 30.0`, which agreed only because both
    were written on the same afternoon.
    """
    return [
        ...
        f"commands are killed after {timeout:.0f} seconds."
        ...
    ]
```

### 7.4 超时信息现在得给出路

L 证明了描述里写约束无效，那唯一有效的位置就是超时**之后**：

```python
if give_up == "timeout":
    # Saying "commands are killed after N seconds" in the tool description
    # was measured in chapter 3 and changed nothing: both providers sent a
    # two-minute command anyway, 6 runs out of 6. This message is the one
    # the model actually acts on, so it has to carry the instruction, not
    # just the fact.
    out += (
        f"\n... (killed: still running after {self.timeout:.0f}s. "
        "Run a smaller piece of the work -- a single test file or a "
        "single directory -- rather than the whole suite.)"
    )
```

### 7.5 决定**不**做的四件事

这一节和上面四节一样重要。

| 不做 | 因为实测 |
|---|---|
| `additionalProperties: false` + `strict` | 四个场景两家，**零次**发明字段。模型宁可绕路（`timeout 5s pytest`、`sed -n '40,60p'`）也不发明字段 |
| 给 `run_shell` 加"不要用于读文件" | 两家本来就 3/3 选对了 `read_file`。只有 J 场景那种名字含糊的工具才需要 |
| 把固定值集合改成 `enum` | 和散文描述**零差别**。而且 I 场景说明 enum 也防不住选错值 |
| 双重转义检测 | 两家都转义正确，**未观测到**。Ch04 真正写文件时再说 |

> PLAN 写作纪律第 7 条：**不为没观测到的行为写代码。**
>
> 这四条里每一条都是"业界常见建议"，写上去没人会说你错。但每一条都会增加代码、
> 增加 token、增加以后要维护的分支——**而实测说它们现在不解决任何问题。**

---

## §8 验证

### 8.1 把规则写成测试

第三次了（Ch01 有两次）。这次的规则是"每条错误信息都必须经过 `tool_error()`"：

```python
@pytest.mark.parametrize("module", ["tools.py", "shell.py", "paths.py"])
def test_F03_07_no_module_builds_an_error_string_by_hand(module: str) -> None:
    """The rule is only worth anything if every error goes through it.

    Checked with `ast`, not a regex over the source: the word "Error:" appears
    in docstrings and comments in these files, and a text search would flag
    those. This walks actual string literals in `return` statements.
    """
    tree = ast.parse((SRC / module).read_text(encoding="utf-8"))

    offenders = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Return) or node.value is None:
            continue
        for piece in ast.walk(node.value):
            if isinstance(piece, ast.Constant) and isinstance(piece.value, str):
                if piece.value.lstrip().startswith("Error:"):
                    offenders.append(f"line {piece.lineno}: {piece.value[:60]!r}")

    assert not offenders, f"{module} returns a hand-built error string: {offenders}"
```

用 `ast` 不用正则的理由和 Ch01 一样，而且这次专门反向验证过：往 docstring 里塞
一句 `returning "Error: something"`，测试**不**报警。

### 8.2 描述快照（F03-10 的廉价版）

M3 证明了改一个例子就能让两家从 3/3 对变成 3/3 错。**所以描述的任何改动都是行为
改动**，不该能随手改掉：

```python
EXPECTED_DESCRIPTIONS = {
    "read_file": "Read a UTF-8 text file from the repository and return its contents.",
    "read_file.path": (
        "Path to the file, relative to the repository root. Example: src/minicodex/model.py"
    ),
    ...
}
```

改描述就得同时改这里——**这个"麻烦"就是这个测试的全部意义**。

F03-10 完整的做法（改描述前后各跑一遍任务集，比较结果）需要 Ch14 的评估基建。

### 8.3 变异测试

| 撤销的修复 | 结果 |
|---|---|
| 绝对路径不再检查是否在仓库内 | 红，`/etc/hosts` 被放行 |
| 找不到文件时不给候选 | 红 ×2 |
| `do_this` 改成可选参数 | 红（`DID NOT RAISE TypeError`） |
| 手写错误字符串绕过 `tool_error` | 红，报出 `paths.py line 53` |
| 描述里的秒数改回硬编码 30 | 红——**但只有第二个测试抓到** |
| 偷偷改一句描述 | 红，diff 出具体差异 |

倒数第二行值得单独说。测试有两条：

1. 描述里的数字 == `DEFAULT_TIMEOUT`
2. `tool_schemas(timeout=90)` 生成的句子里得是 90

把 `f"{timeout:.0f}"` 改回硬编码的 `30`，**第一条测试仍然是绿的**——因为
`DEFAULT_TIMEOUT` 恰好就是 30。只有第二条抓到了。

> **只断言"当前值一致"，抓不到硬编码。** 必须断言"改变来源，结果跟着变"。

### 8.4 全量

```
98 passed
ruff check: All checks passed!
ruff format --check: 22 files already formatted
```

Ch02 有一条测试红了：

```python
assert "timed out after 1" in out       # 断言了具体措辞
```

这一章重写了所有错误信息，它就红了。**它测的是措辞，而措辞正是这一章要改的东西。**
改成：

```python
# Asserts that the kill was reported, not the exact sentence. Chapter 3
# rewrote every error message and this was the only test that broke --
# it was pinning wording that test_schemas.py now checks properly.
assert "killed" in out
```

行为测试断言行为，措辞由 `test_schemas.py` 的快照专门管。**一条测试红了不一定是
代码错了，也可能是这条测试断言了不该由它断言的东西。**

---

## §9 文件清点

| 文件 | 出现位置 | 状态 |
|---|---|---|
| `src/minicodex/tool_errors.py` | §7.1（完整 45 行） | ✅ |
| `src/minicodex/paths.py` | §7.2（`resolve` 完整，`_candidates` 见交付代码） | ✅ |
| `src/minicodex/tools.py` | §7.3 + §7.5（改动部分），完整版在交付代码 | ✅ |
| `src/minicodex/shell.py` | §7.4（超时信息改动），其余同 Ch02 | ✅ |
| `tests/test_schemas.py` | §8.1 / §8.2（关键片段），16 个测试完整版在交付代码 | ✅ |
| `probe_descriptions.py` / `probe_descriptions2.py` | §3 / §5（关键片段），非项目代码 | ✅ |

---

## §10 收工：commit 与 review

三个 commit：

```
16e9877 test: pin the wording, and the rule that produces it
e476664 fix: resolve the path the model actually sends, instead of describing it better
b593443 feat: make every tool error say what to do next
```

第二条的 message 里有一整段"**Deliberately not done, because it was measured and
did not reproduce**"，逐条列出四个不做的决定和实测理由。

> **一个"我没做 X"的决定，比"我做了 Y"更需要写进 commit message。** 因为半年后
> 有人会问"为什么这里没加 strict schema"，而 diff 里永远不会有答案。

### Code review

**1 · `paths.py` 的 `rglob` 在大仓库上会不会很慢？**

> 作者：会。`_MAX_SUGGESTIONS = 5` 提前 break，`_SKIP` 挡掉了 `.git` 和
> `node_modules`——但在一个几十万文件的仓库里，一次找不到文件的调用仍然要遍历。
> **接受这个风险，因为它只在出错路径上。** 真成为问题的时候，正确的解法是维护一份
> 文件索引，而那要等到有第二个功能也需要它。（三次法则：现在是第一次。）

**2 · `resolve()` 返回 `tuple[Path | None, str | None]`，两个 `None` 的组合有四种，
其中两种非法。**

> 作者：同意这是个弱类型。真正的解法是一个 `Ok | Err` 联合类型。**但现在只有一个
> 调用点**，收益是零，成本是每个调用点都要 `match`。第二个工具需要路径解析的时候
> （Ch04 的 `apply_patch` 一定需要）再改，那时候有两个样本，能看出真正的共性。

**3 · `EXPECTED_DESCRIPTIONS` 把描述抄了一遍，改描述要改两个地方。**

> 作者：**这正是它的目的。** 快照测试的价值就在于让改动变麻烦。如果它能自动跟着变，
> 它就什么都测不到了。

**4 · 那句 "Long-running or silent commands are killed after 30 seconds." 既然实测
证明对模型行为零影响，为什么不删掉？**

> 作者：好问题，我犹豫过。留下的理由是它对**人**有用——调试时看请求体，这句话
> 解释了为什么会有 `killed` 的输出。成本是每轮几个 token。
> **但这个理由没有被实测支持，是我的判断。** 如果哪天要压缩 prompt，这句话应该是
> 第一批被砍的——而且砍之前应该跑一次 Ch14 的任务集，而不是像现在这样凭感觉留着。

**5 · 探测脚本里 `note` 函数的分类逻辑和真实代码没有任何共享，会不会漂移？**

> 作者：会，而且已经漂了——§5.1 那个 `[valid]` 就是分类器比真实语义宽松导致的。
> 但这两者**不该**共享代码：探测脚本的分类是"我关心什么"，真实代码的校验是
> "什么是合法的"，两者恰好不同才有信息量。**代价是我必须看原始输出，不能只看标签。**

---

## §11 本章给 CI 加了什么

**加了一条，但它跑不了。**

这一章的核心结论全部来自真实模型的 A/B 采样。CI 里没有 Ollama，也不该有 OpenAI
key。`test_schemas.py` 里的 16 个测试全部是确定性的——它们保护的是**从实测里学到的
结论**（三段式、路径解析、描述快照），而不是重新验证结论本身。

> **实测发现规律，测试固化规律。** 这两件事用的是完全不同的手段，混在一起就会得到
> 一个又慢又不稳定的 CI。

Ch14 会补上真正缺的那块：一个能在 CI 里跑的任务集 + 录制回放，让"改了描述之后
行为有没有变差"成为一个可以自动回答的问题。

---

## §12 回头看：这一章撞到了什么

| 编号 | 故障 | 实测结果 | 挡住它的东西 |
|---|---|---|---|
| F03-01 | `path` / `filename` 在两个工具间混用 | **未复现**，两家都用各自 schema 的名字 | —— |
| F03-02 | 相对/绝对没说明，模型一半时候是对的 | 🟡 **复现**，且两家错法相反 | `paths.resolve()`，**代码层**解决 |
| F03-03 | 模型幻觉出一个字段 | **未复现**，模型改用现有字段绕路 | —— |
| F03-04 | 描述只说做什么，不说什么时候别用 | 🟡 **复现**（仅限名字含糊的工具） | "Do not use this for…" |
| F03-05 | 两个重叠工具，模型随机选 | 🟡 **复现**（仅限 J 场景那种含糊命名） | 同上 |
| F03-06 | enum 用散文描述 | **未复现**，且 enum 也防不住选错值 | —— |
| F03-07 | 裸 `ValueError:` 原样返回，模型原样重试 | 🔵 **强复现**，弱模型 3/3 卡死 | `tool_error()`，`do_this` 必填 |
| F03-08 | 必填参数埋在 schema 末尾 | **未复现**，6/6 都传了 | —— |
| F03-09 | 多行代码过 JSON 转义 | **未复现**（但双重转义确定性地危险） | 留给 Ch04 |
| F03-10 | 改一句描述，无关任务跟着退化 | 🟢 **确定性证实**（M3：改例子翻转结果） | 描述快照测试；完整方案在 Ch14 |
| **新** | 描述里写约束，对行为零影响 | 🟢 6/6 无差别 | 把指令搬进错误信息 |
| **新** | 描述里的默认值会被模型原样传回 | 🟠 从 K 场景的原始输出里看到的 | 描述从常量生成 |
| **新** | 多余字段被静默丢弃 | 🟢 确定性 | **未修**（模型不发明字段） |

标记：🔴 崩溃 · 🟡 静默 · 🟢 边界测试 · 🔵 长跑 · 🟠 看日志 · 🟣 review · ⚫ 用户报告 · ⚪ lint/类型

**十条清单，复现三条，新发现三条。**

---

## §13 codex 是怎么做的

**codex 的工具描述是 Markdown 文件，不是 Python 字符串。**
`codex-rs/core/src/tool_descriptions/` 下面一堆 `.md`，编译时嵌进二进制。理由和
这一章 §8.2 一样：**描述是需要被 review 的内容**，放在 `.md` 里，PR 的 diff 才读
得懂；塞在代码里的字符串拼接，review 的时候没人看得出改了什么。

**错误信息也是精心写的 prompt。** `codex-rs/core/src/exec.rs` 和 `apply_patch/`
里的错误分支，几乎每一条都带着"下一步该怎么做"。这不是巧合——是同一个实测结论在
不同项目里独立被发现。

**`apply_patch` 用的是自定义文本格式，不是 JSON 参数。** 这直接对应 §6.1 的
F03-09：多行代码穿过 JSON 转义有风险，codex 的选择是**不穿**——用 freeform 文本
格式，模型不需要转义任何东西。Ch04 会走到这一步。

**codex 也有描述的快照测试**，理由和 §8.2 一致。

---

## 如果你只记住三件事

1. **别人的故障清单是假设，不是事实。** 这一章列了十条，实测复现三条。照着清单
   写代码，会为七个不存在的问题增加复杂度。
2. **事前在描述里预防无效，事后在错误信息里纠正有效。** 而且描述每轮重发，错误信息
   只在出错时发一次——**便宜的那个还更管用**。
3. **只用强模型测，你会以为一切都好。** 同一份错误信息，`gpt-4o-mini` 每次都能自己
   纠正，`gemma4:31b` 每次都卡死。你的开发环境越强，你的 bug 就越晚被发现——
   Ch01 是"服务端越宽松"，这一章是"模型越聪明"。

---

## 动手练习

1. 把 `_SKIP` 里的 `.git` 删掉，在一个真实仓库里对着 `config.py` 跑一次
   `resolve()`。看看错误信息会变成什么——然后想一想，模型收到那条信息之后
   最可能做什么。
2. `tool_error()` 现在把三段拼成一行。改成三行（用 `\n` 分隔），用
   `probe_descriptions.py --only D` 各跑三次，对比 Ollama 的恢复率有没有变化。
   **这是一次真正的 A/B**——如果没有差别，就把它改回去，并且在 commit message 里
   写清楚你测过。
3. 给 `run_shell` 的描述加一句 "Prefer running a single test file over the whole
   suite."，然后重跑 §5.4 的 L 场景。如果模型仍然 3/3 发出整个测试套件，你就复现了
   这一章最有用的那条负面结论——**并且亲手证明了描述不是万能的**。

下一章：Ch04 · 文件编辑 `apply_patch`——写文件比读文件危险得多，而且这一章
§6.1 那个"双重转义"的风险，到那时候就不再是理论了。
---

# 附录 · 代码到底怎么敲出来的（新手向，逐段精讲）

跟第 1 章附录同一个规矩：正文讲"为什么"，这里补"怎么写"。默认你已经读过
前面几章的附录（`asyncio.to_thread`、`functools.partial`、`Path` 基础），
这里只讲这一章新出现的、容易让新手卡住的写法。代码摘自
`steps/step03_tool_descriptions/src/minicodex/`，逐段核对过。

先把范围说死：

1. 本附录只解释第 3 章在 `steps/step03_tool_descriptions/` 里新增或修改的
   代码。正文 §9 的清点表说得很清楚：`tool_errors.py` 的 `tool_error` 在
   §7.1 给了全文，`tool_schemas` 的片段在 §7.3/§8.3 给了——不再重复。这里
   补正文明说"见交付代码"的（§7.2 的 `_candidates`、§7.3 的 `read_file`/
   `default_tools` 完整版、§7.4 的 shell 改动）。
2. 完整函数代码块里不使用代表遗漏实现的省略号，也不把关键分支写成
   "此处略"。示例字符串或命令文本里出现的三个点，如果本来就是源码的
   一部分，会原样保留。
3. 代码块按当前源码核对。

这一章的核心是正文那句"**这不是描述问题**"（模块 docstring 的第一条
决定）：两个模型都抄描述里的例子而不是学规则，所以能确定性地解决的
（绝对路径、唯一文件名）全部搬到代码层。附录按"代码层做了什么"来组织。

## M1 · `paths.py`：把"模型猜路径"变成"代码查路径"

正文 §7.2 给了 `resolve`，`_candidates` 自述"见交付代码"：

```python
_SKIP = {".git", "__pycache__", ".venv", "venv", "node_modules", ".pytest_cache", ".ruff_cache"}
_MAX_SUGGESTIONS = 5


def _candidates(name: str, root: Path) -> list[str]:
    """Files under `root` whose final component is `name`."""
    found: list[str] = []
    for path in root.rglob(name):
        if any(part in _SKIP for part in path.relative_to(root).parts):
            continue
        if path.is_file():
            found.append(str(path.relative_to(root)).replace("\\", "/"))
            if len(found) > _MAX_SUGGESTIONS:
                break
    return sorted(found)
```

四个细节：

1. **`root.rglob(name)` 递归找所有"最后一段是 name"的文件**——模型发来
   `model.py`，代码去找仓库里所有叫这个名字的文件。`rglob` 按名字匹配
   最后一段，不是全路径。
2. **`_SKIP` 过滤不可能是意图的目录**——`.git`、`__pycache__`、`.venv`、
   `node_modules` 里的同名文件绝不可能是模型想要的。检查的是
   `path.relative_to(root).parts` 的每一段（`any(part in _SKIP ...)`）——
   路径里**任何一段**属于跳过名单就跳过。
3. **`str(path.relative_to(root)).replace("\\", "/")` 返回仓库相对路径**，
   且统一斜杠（Windows 上 `Path` 用 `\`，错误信息给模型看的应该是一致
   形式）。
4. **`if len(found) > _MAX_SUGGESTIONS: break` 提前停**——大仓库上
   `rglob` 可能很慢（正文 §10 review 第一条就讨论了），找满 5 个就够
   建议了，不扫全库。

`resolve` 的完整逻辑（正文 §7.2 给了全文，这里只讲它怎么用 `_candidates`）：

```python
    if not full.exists():
        near = _candidates(Path(raw).name, root)
        if len(near) == 1:
            do_this = f"Send this instead: {near[0]}"
        elif near:
            do_this = "Did you mean one of these? " + ", ".join(near[:_MAX_SUGGESTIONS])
        else:
            do_this = "Use run_shell with ls or find to see what exists, then try again."
        return None, tool_error("no such file in the repository", you_sent=raw, do_this=do_this)
```

三种"下一步"对应三种情况：**恰好一个候选**（直接告诉模型发哪个）、
**多个候选**（列出来让模型挑）、**没有候选**（教它用 ls/find 找）。
`_candidates(Path(raw).name, root)` 用原始字符串的**文件名部分**去搜——
`src/model.py` 找不到时，拿 `model.py` 搜全仓库。

## M2 · `shell.py` 的改动：`_handle_cd` 和"拒绝后台命令"

正文 §7.4 只说了"超时信息改动"，实际改动有两处（`_handle_cd` 的
`tool_error` 化 + `run` 里拒绝 `&`）：

```python
    def _handle_cd(self, command: str) -> str | None:
        stripped = command.strip()
        if stripped != "cd" and not stripped.startswith("cd "):
            return None
        target = stripped[2:].strip() or self.env.get("HOME", "/")
        new_dir = os.path.normpath(os.path.join(self.cwd, os.path.expanduser(target)))
        if not os.path.isdir(new_dir):
            return tool_error(
                "cd: no such directory",
                you_sent=target,
                do_this="Use ls to see what is here, then cd to a directory that exists.",
            )
        self.cwd = new_dir
        return ""
```

- **`_handle_cd` 返回 `None`（不是 cd 命令）或字符串（是 cd 命令的
  结果）**——`run` 开头 `cd_result = self._handle_cd(command); if cd_result
  is not None: return cd_result`。空串 `""` 表示"cd 成功了，没有输出"，
  `None` 表示"这不是 cd，继续正常执行"。
- **`stripped[2:].strip()`**：命令是 `"cd " + target`，`[2:]` 切掉
  `"cd"`（两字符），`.strip()` 去掉空格。`cd` 裸命令（无参数）回落
  `$HOME`。
- **`os.path.normpath(os.path.join(self.cwd, os.path.expanduser(target)))`**
  拼绝对路径：`os.path.join` 相对当前 cwd，`expanduser` 展开 `~`，
  `normpath` 规范化 `..`。这是"cd 只改 cwd 这个状态变量"的全部机制——
  docstring 明说这覆盖 `cd` 独立命令，不覆盖 `cd foo && pytest`。

`run` 里新增的"拒绝后台命令"分支：

```python
        if command.rstrip().endswith("&"):
            return tool_error(
                "run_shell does not support backgrounded commands (trailing '&')",
                you_sent=command,
                do_this=(
                    "Run it in the foreground, or split the work into steps short "
                    "enough to finish within the timeout."
                ),
            )
```

正文 §7 测到 `sleep 30 &` 在几毫秒内返回、后台进程继续跑——它在
`chunks` 之外、退出码不是 `proc.returncode`、`killpg` 也杀不到它。**拒绝
是诚实的**（docstring 明说"真正支持意味着后台任务注册表 + 轮询，那是一个
真功能，不是两行修复"）。

## M3 · `tools.py`：`read_file`、`default_tools` 的完整版

正文 §7.3 只给了改动部分。完整实现：

```python
def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


async def read_file(root: Path, args: dict[str, Any]) -> str:
    path, error = resolve(args.get("path"), root)
    if error is not None:
        return error
    assert path is not None
    return await asyncio.to_thread(_read, path)


def default_tools(root: Path | None = None) -> dict[str, Any]:
    root = (root or Path.cwd()).resolve()
    session = ShellSession()
    return {
        "read_file": functools.partial(read_file, root),
        "run_shell": functools.partial(_run_shell, session),
    }
```

三个要点：

1. **`_read` 和 `read_file` 分开**：`_read` 是同步阻塞 IO（
   `Path.read_text`），`read_file` 是 async 外壳，用
   `await asyncio.to_thread(_read, path)` 把阻塞调用丢线程池——第 1 章
   F00-09 的规则（正文 docstring 明说："with one tool at a time nobody
   notices; once chapter 8 runs tools concurrently the concurrency quietly
   turns into a queue"）。
2. **`resolve` 返回 `(path, error)` 二元组**：`error is not None` 就返回
   错误消息（`resolve` 已经把三段式拼好了），否则 `assert path is not
   None` 收窄类型后读文件。`assert` 是给类型检查器看的：`resolve` 保证
   error 为 None 时 path 一定非空。
3. **`default_tools` 用 `functools.partial` 绑定**：`read_file` 绑
   `root`（仓库根），`run_shell` 绑 `session`（ShellSession 持有 cwd/env）。
   `root = (root or Path.cwd()).resolve()`——没传就用当前目录，
   `.resolve()` 规范化。**两者都是"每会话"的**（root 决定路径合法性、
   session 持有 cwd），不属于进程，所以不进模块级常量。

## M4 · 新手常见报错/坑对照表

| 症状 | 原因 | 修法 |
|---|---|---|
| 模型一直发绝对路径 | 描述里给了绝对路径例子 | `resolve` 接受指向仓库内的绝对路径（`relative_to` 判定），不再让模型猜 |
| 错误建议把 `.git` 里的同名文件列出来 | `_candidates` 没过滤 | `any(part in _SKIP for part in path.relative_to(root).parts)` |
| 大仓库上 `rglob` 卡死 | 扫全库找候选 | `_MAX_SUGGESTIONS`（5）够了就 `break` |
| `cd` 后下一次 `pwd` 又回去了 | 把 `cd` 交给子 shell | `_handle_cd` 在 Python 里更新 `self.cwd`，每次 `run` 传 `cwd=` |
| `cd` 到不存在的目录返回原始 shell 错误 | 没拦 | `_handle_cd` 里 `os.path.isdir` 检查 + `tool_error` 三段式 |
| `sleep 30 &` 让进程泄漏 | 支持了后台命令 | 拒绝（`endswith("&")` 返回三段式错误），诚实说明缺口 |
| `read_file` 里 `Path.read_text` 直接调用 | 阻塞调用在 async 里 | `_read` 同步函数 + `await asyncio.to_thread(_read, path)` |
| 描述里的数字和常量漂移 | 数字硬编码进字符串 | `tool_schemas(timeout)` 参数化，`f"{timeout:.0f}"` 生成 |
| `default_tools` 的 root/session 变成模块级 | 不属于进程的状态被提升 | `functools.partial` 绑定每会话对象 |
