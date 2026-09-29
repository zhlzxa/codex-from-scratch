# 第 3 章 · 工具描述工程

> **代码**：`steps/step03_tool_descriptions/`
> **分支**：`feat/tool-descriptions`
> **产出**：模型第一次就把参数传对；传错的时候，第二次能自己改正
> **前置**：做完第 2 章。要亲自做本章的测量，需要 Ollama；有 OpenAI key 收获会翻倍——
> **本章最重要的几个结论，全部来自两家模型的差异**，只测一家会得出错误的结论。
> 没有这两样也能跟完：所有测量结果都记在正文里，代码部分不需要模型。

---

## §0 开工前的准备

### 0.1 本章会遇到的新概念

**模型看到的不是你的函数，是一段描述。** 第 0 章 §3 的 `TOOLS` 就是它：

```json
{"name": "read_file",
 "description": "Read a UTF-8 text file and return its contents.",
 "parameters": {"type": "object",
                "required": ["path"],
                "properties": {"path": {"type": "string",
                                        "description": "Path to the file."}}}}
```

`parameters` 这一部分用的是一种叫 **JSON Schema** 的格式，描述"参数应该长什么样"：

- `properties`：有哪些参数，每个参数的类型（`type`）和说明（`description`）。
- `required`：哪些参数必须给。
- `enum`：参数只能从列出的几个值里选。
- `additionalProperties: false`：不允许出现没列出的参数。

这些描述文字，每一个词都会参与模型的"思考"。**它们是写给模型的提示（prompt），不是写给人看的文档。**

**采样和 A/B 测试。** 同一个问题问模型几次，回答可能不一样（第 0 章 §7 见过）。所以想知道
"描述这样写好不好"，问一次是不够的。这一章的做法是 **A/B 测试**：同一个问题、同一个模型，
准备两种写法（A 和 B），每种问三次，比较结果。

### 0.2 本章会遇到的新 Python 写法

| 写法 | 意思 |
|---|---|
| `copy.deepcopy(x)` | 把 `x` 连同它里面所有的列表、字典完整复制一份，改副本不影响原件 |
| `path.rglob(name)` | 在 `path` 下面的所有子目录里，找名字等于 `name` 的文件 |
| `path.relative_to(root)` | `path` 相对于 `root` 的路径；`path` 不在 `root` 下面时抛 `ValueError` |
| `f"{x:.0f}"` | 把数字格式化成不带小数的样子，比如 `30.0` 显示成 `30` |
| 函数参数里的 `*` 之后的必填参数 | 例如 `def f(a, *, b)`：`b` 必须用 `b=...` 的形式传，而且不能省略 |

### 0.3 开分支

```bash
git switch main
git pull
git switch -c feat/tool-descriptions
```

---

## §1 这一章要做出来的东西

前两章做的是"工具能不能正确执行"。这一章做的是**"模型能不能正确调用它"**——这是完全不同的一件事，
而且没有哪一行代码能直接决定它。

问题在于：你没办法通过读代码判断"这句描述写得好不好"。**只能测。**

---

## §2 定需求，猜故障

需求只有一句：**让模型少传错参数；传错了，能自己改正。**

这一次的猜测表很长，来自经验和 codex 的源码——一份"工具描述的常见问题清单"：

| 编号 | 猜测 | 怎么判断它成立（A/B） |
|---|---|---|
| F03-01 | 参数名 `path` / `file` / `filename` 在不同工具之间混用 | 两个工具用不同的参数名，看模型会不会用错 |
| F03-02 | 没说清路径是相对还是绝对，模型乱给 | 含糊的描述 vs 说清楚的描述 |
| F03-03 | 模型凭空编出一个不存在的参数 | 需求里暗示了一个 schema 没有的参数 |
| F03-04 | 描述只说"做什么"，不说"什么时候别用" | 加不加"不要用于……" |
| F03-05 | 两个功能重叠的工具，模型随便选一个 | 同上 |
| F03-06 | 固定的几个取值只写在文字里，而不是用 `enum` | 文字描述 vs `enum` |
| F03-07 | 工具出错时只返回一句 `ValueError: ...`，模型原样重试 | 同一个失败，两种错误信息 |
| F03-08 | 必填参数埋在一堆可选参数后面，被漏掉 | 必填参数放最后 vs 放最前 |
| F03-09 | 多行代码作为参数，在 JSON 转义时出错 | 让模型写一段多行代码 |
| F03-10 | 改一句描述，让不相关的任务变差 | 只改一处，看别处有没有受影响 |

听起来很扎实。**然后实测了：十条里只有三条复现。**

这不是说清单没用，而是说**清单不能照抄**。照着清单写代码，会为七个在当前模型上根本不会发生的
问题写一堆防御逻辑，还会漏掉三个清单上根本没有的真问题。

> 这一章最重要的教训，比任何一条具体故障都重要：
> **别人的故障清单——包括这本书的——是猜测，不是事实。用之前，先在你自己的模型上测一遍。**

---

## §3 测量工具，以及它自己的 bug

要做 A/B 测试，得有一个"同一个问题、换一种描述、问几次、把结果打出来"的小工具。
下面是一个最小的版本，放在项目根目录，**看完就删，不提交**：

```python
# probe.py -- ask the same question under two tool descriptions, N times each,
# and print what the model actually sent.  A throwaway measuring tool, not
# project code.
import asyncio
import copy
import sys

from minicodex.model import ChatCompletionsModel, ToolCallDelta
from minicodex.tools import TOOL_SCHEMAS

BASE_URL = sys.argv[1] if len(sys.argv) > 1 else "http://localhost:11434/v1"
MODEL = sys.argv[2] if len(sys.argv) > 2 else "gemma4:31b-cloud"
SAMPLES = 3
QUESTION = "What does the model module define?"

VARIANTS = {
    "A1 vague": "Path to the file.",
    "A2 rule + example": (
        "Path to the file, relative to the repository root -- never absolute. "
        "Example: src/minicodex/model.py"
    ),
}


def schemas_with(path_description: str) -> list[dict]:
    schemas = copy.deepcopy(TOOL_SCHEMAS)
    for schema in schemas:
        fn = schema["function"]
        if fn["name"] == "read_file":
            fn["parameters"]["properties"]["path"]["description"] = path_description
    return schemas


async def first_call(llm: ChatCompletionsModel) -> str:
    try:
        async for event in llm.stream([{"role": "user", "content": QUESTION}]):
            if isinstance(event, ToolCallDelta):
                return f"{event.name}({event.arguments})"
    except Exception as exc:  # a failed request is not a model behaviour
        return f"!! REQUEST FAILED: {type(exc).__name__}: {exc}"
    return "(no tool call)"


async def main() -> None:
    for label, description in VARIANTS.items():
        llm = ChatCompletionsModel(base_url=BASE_URL, model=MODEL, tools=schemas_with(description))
        for i in range(SAMPLES):
            print(f"{label:18} #{i + 1}: {await first_call(llm)}")


asyncio.run(main())
```

> - `sys.argv` 是命令行参数的列表，`sys.argv[0]` 是脚本名，后面依次是用户给的参数。
>   这里第一个参数是服务地址，第二个是模型名，不给就用默认值。
> - `schemas_with(...)`：把当前的工具描述**复制一份**，只改掉 `read_file` 的 `path` 说明。
>   用 `deepcopy` 是因为 `TOOL_SCHEMAS` 是嵌套的字典和列表，浅复制的话改副本会连原件一起改掉。
> - `first_call(...)`：只看模型的**第一个**工具调用，把名字和参数原样打出来。
> - **`except Exception` 那一行最重要**，下面讲为什么。

用法：`uv run python probe.py http://localhost:11434/v1 qwen3`。改 `VARIANTS` 和 `QUESTION`，
就能测别的场景。（这个脚本已经对着录音服务跑通过；录音服务不管描述怎么写都回答同样的内容，
所以只能用来检查脚本本身能不能跑。）

### 3.1 测量工具自己的三个 bug

作者最初写的探测脚本更大，能一次跑十几个场景。跑真模型之前，先用一个故意返回各种怪回复的
假服务器（绝对路径、坏 JSON、编出来的参数、干脆不调工具）把脚本本身测了一遍，逮出三个 bug，
每一个都会污染真实的数据：

**1. 请求失败被当成了"模型选择不调工具"。** 网络一抖，就被记成了一条"模型行为"。
修法是把"请求失败"单独标出来——上面脚本里 `!! REQUEST FAILED` 那一行就是这个修法。
**这个改动在第二轮测量时真的救了命**，§5.6 会讲。

**2. 判断"编出了参数"时，只拿预期的那个工具的参数表去比。** 模型调了另一个工具时，它的正常参数
被误报成"编出来的"。

**3. JSON 解析失败被算成了"编出了参数"。** 那是解析失败，不是编参数。

> **能先在假数据上测的脚本，就不要直接拿真数据去测。** 真数据贵——时间、API 的钱、模型的排队。
> 而且假数据能造出真实采样里几百次都碰不到一次的怪回复。

---

## §4 第一轮：七个场景，五个没复现

对 Ollama 的 `gemma4:31b-cloud` 和 OpenAI 的 `gpt-4o-mini` 各采样三次。

### 4.1 路径（F03-02）——复现了，而且两家错的方向相反

问题：`What does the model module define?`（模型得自己拼出 `src/minicodex/model.py` 这个路径）

```
A1  描述："Path to the file."
    Ollama:   path='model.py'                      ×3
    OpenAI:   path='/home/dev/minicodex/model.py'  ×3

A2  描述："...relative to the repository root -- never absolute.
          Example: src/minicodex/model.py"
    Ollama:   path='src/minicodex/model.py'        ×3
    OpenAI:   path='src/minicodex/model.py'        ×3
```

**两家在 A1 都错了，但方向相反。** Ollama 给的是相对路径（但少了两层目录），OpenAI 从 system 提示里
抓出仓库路径，拼成了绝对路径。

> 这是第 1 章那个现象的新形式。第 1 章是"宽松的服务掩盖了坏历史"，这次是
> **"一家的习惯会让你对模型形成错误的印象"**。只对着 Ollama 开发，你会以为"模型总是给相对路径"，
> 代码里不处理绝对路径——换到 OpenAI 就全崩。

### 4.2 编出参数（F03-03）——没复现，而且没复现的方式很有意思

问题："跑测试，但超过 5 秒就放弃"。schema 里**只有** `command` 一个参数。预期模型会传
`{"command": "pytest", "timeout": 5}`。实际：

```
Ollama:   command='timeout 5s pytest'                          ×3
OpenAI:   command='timeout 5s pytest'                          ×2
          command='pytest --maxfail=1 --disable-warnings --timeout=5'  ×1
```

**两家都没有编参数，而是把意图写进了已有的参数里**——用 shell 自带的 `timeout` 命令达到目的。
模型比预想的守规矩得多。

加上 `additionalProperties: false` 和 `strict: true` 的版本，结果**完全一样**，因为根本没有多余的
参数需要拒绝。

### 4.3 另外四条：全部没有差别

两家、两种写法：

- 取值用文字描述 vs 用 `enum`（F03-06）：都给出了 `"append"`，都合法
- 读文件时 `read_file` vs `run_shell`（F03-05）：都选了 `read_file`
- `write_file(filename)` vs `write_file(path)`（F03-01）：都用了各自 schema 里的那个名字
- 写一段多行 Python 代码（F03-09）：都产出了合法的 JSON，换行正确（11–12 行）

### 4.4 错误信息就是提示（F03-07）——强烈复现，两家结果相反

这是本章最重要的一次测量。构造一个已经失败的调用（模型传了绝对路径），然后给它**两种不同的错误信息**，
看它第二次怎么做：

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

看 Ollama 在 D1 里的重试：原来失败的是 `/home/dev/minicodex/src/minicodex/model.py`，
重试的是 `/home/dev/minicodex/model.py`。

**它删掉了两层目录。** 它不是不想改，它在努力改——只是 `ValueError: invalid path` 没告诉它
"绝对路径不行"，它猜错了方向，以为是"路径太深"。

> **如果只用强模型开发，你会得出"错误信息怎么写都行"的结论。** 换一个弱一点的模型，同一份代码会
> 陷进循环：每一轮都重试同一个错误，直到用完第 0 章的轮次预算。
> 轮次预算兜住了这种情况，**但"兜住"不等于"解决"**。

第 0 章说过"工具的错误信息就是给模型的提示"，这是那句话的第一次实测。

---

## §5 第二轮：把没复现的场景全部加难

五条没复现，有两种可能：一是这些问题在当前模型上确实不会发生；二是**我的场景太好猜了**。
比如"想在末尾加一行"——`append` 几乎是唯一的答案；`read_file` 和 `run_shell` 光看名字就知道干什么。
得排除第二种可能，才能下结论。

### 5.1 取值改成猜不出来的（场景 I）

`u` / `c` / `n` 三个 diff 格式的缩写，问题只说"像 git 那样显示"：

```
I1 文字描述:  Ollama: format='u'  ×3     OpenAI: format='n'  ×3
I2 真 enum:   Ollama: format='u'  ×3     OpenAI: format='n'  ×3
```

文字和 `enum` **仍然没有差别**。但这次有个脚本没抓到的东西：

**脚本把两家都标成了"合法"——因为 `u` 和 `n` 都在允许的取值里。可 git 用的是 unified 格式，
正确答案是 `u`。OpenAI 选的 `n` 是错的**，它被 "normal" 这个词字面上误导了。

> **`enum` 保证值合法，不保证值正确。** 而脚本只检查了合法性——
> **只看分类标签、不看原始数据，就会漏掉这种事。** 测量工具本身也有盲区，第 2 章 pytest 的 stdin
> 是一次，这是又一次。

### 5.2 两个说不清自己是干什么的工具（场景 J）——"不要用于"唯一的复现

`fetch_content(target)` 和 `get_text(item)`，描述都很含糊：

```
J1 两个描述都含糊:
   Ollama: fetch_content ×3      OpenAI: fetch_content ×3     ← 两家都选错

J2 各自加一句"不要用于 X":
   "Retrieve the content of a remote URL over HTTP.
    Do not use this for files on disk -- use get_text for those."
   Ollama: get_text ×3           OpenAI: get_text ×3          ← 两家都改对
```

**干净的复现（F03-04、F03-05）。** 但结论要加限定：第一轮 `read_file` 和 `run_shell` 那个场景
没复现，因为那两个名字已经说清了自己是什么。

> **只有名字本身不足以区分时，才需要写"不要用于……"。** 给每个工具都加这句话是浪费——
> 描述每一轮请求都要重新发一遍。

### 5.3 例子是被"抄"的，不是被"理解"的（场景 M）——推翻了第一轮的结论

第一轮 A2 加了例子后两家全对，当时的结论是"说明规则 + 给例子，有效"。**这个结论是错的。**
场景 M 把规则的说明固定住，只换例子：

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

M2 和 M3 的规则说明**一字不差**，唯一的区别是例子里的文件名。

**M2 的成功不是因为模型理解了"相对于仓库根目录"，是因为它照抄了例子。** 而真实使用中，
例子几乎不可能恰好就是用户要的文件。所以：

> **路径这件事，在描述里解决不了。**
> **能在确定的代码里解决的，就不要交给模型。** 每次想在描述里写"请一定要……"，
> 先问一句：这件事能不能用代码来保证？

这也证实了 F03-10：**改一个例子，两家都从 3/3 对变成 3/3 错。描述的任何改动都是行为的改动。**

### 5.4 描述里写的限制，对行为毫无影响（场景 L）——清单上没有的新发现

`run_shell` 的描述里写着 "Long-running or silent commands are killed after 30 seconds."。任务明说要跑两分钟：

```
L1 描述里有这句话:   两家: command='pytest tests/integration'  ×3
L2 描述里没这句话:   两家: command='pytest tests/integration'  ×3
```

**6/6 原样发出，等着被杀。** 有没有这句话，没有任何区别。

和 §4.4 放在一起看：

| | 效果 |
|---|---|
| **事前**在描述里写限制 | 零 |
| **事后**在错误信息里给指令 | 对弱模型是决定性的 |

而且描述**每一轮都要重发**，错误信息**只在真出错时发一次**。

> **与其在描述里预防，不如在错误信息里纠正。便宜的那个还更管用。**

### 5.5 两家"组合工具"的策略相反（场景 H）

问"只看第 40 到 60 行"，而 `read_file` 只有 `path` 参数，没法指定行号：

```
H1 只给 read_file:
   两家: read_file(path='src/minicodex/agent.py')   ×3   读整个文件

H2 另外再给一个 run_shell:
   Ollama: run_shell("sed -n '40,60p' src/minicodex/agent.py")  ×3
   OpenAI: read_file('src/minicodex/agent.py')                  ×3
```

> `sed -n '40,60p' 文件` 是一个只打印第 40 到 60 行的命令。

**更弱的 Ollama 反而更会组合工具**，OpenAI 保守地用最直接的那个，把整个文件读了进来。
这也解释了 F03-03 为什么不复现：**模型不编参数，它们要么凑合，要么换一个工具。**

### 5.6 场景 K：测量工具救了一次命

"必填参数埋在五个可选参数后面"。第一次跑，两家全部返回 HTTP 400：

```
K1: !! REQUEST FAILED -- not a model behaviour: HTTP 400:
    "Invalid type for 'tools': expected an array of objects, but got an object instead."
```

脚本里传的是 `tool(...)` 而不是 `[tool(...)]`，少了一对方括号。**这是我的 bug，不是模型的行为。**

注意它被打印成了 `REQUEST FAILED`——**这就是 §3.1 那个修法的作用**。如果没有它，这 6 次会被记成
"模型选择不调用工具"，而我很可能据此写下"参数一多，模型就不调工具了"这种完全错误的结论，
还为它写一堆代码。

修好之后：

```
K1 必填参数放最后:  Ollama 3/3 传了    OpenAI 3/3 传了
K2 必填参数放最前:  Ollama 3/3 传了    OpenAI 3/3 传了
```

F03-08 也没复现。但 OpenAI 那几次回复泄露了别的东西：

```
{"pattern": "asyncio.wait_for", "max_results": 50}
```

描述里写的是 `"Cap on results. Default 50."`，**模型把这个默认值原样传了回来**。值本身没错——
可一旦代码里把默认值改成 100、描述却忘了改，模型会一直传 50，悄悄覆盖掉新的默认值。
**这比描述和代码里的超时数字对不上更隐蔽，因为它永远不会报错。**（清单外的新发现。）

---

## §6 不需要模型的部分：三个确定的发现

有些事不用问模型，跑一下就知道。

### 6.1 JSON 双重转义是静默的（F03-09 的另一半）

模型写多行代码时，参数里的换行有三种写法：

```python
# 1. 正确转义
'{"content": "line1\\nline2"}'     → 解析成功，2 行

# 2. 双重转义
r'{"content": "line1\\\\nline2"}'  → 解析成功，但没有真正的换行，
                                      文件里是字面的反斜杠和 n

# 3. 没转义的真换行
'{"content": "line1<真换行>line2"}' → JSONDecodeError:
                                      Invalid control character
```

> JSON 字符串里，换行要写成 `\n`（反斜杠加 n）。"双重转义"是写成了 `\\n`，解析出来就变成了
> 字面的两个字符 `\` 和 `n`。

第三种会被第 0 章的组装函数挡下（`arguments` 变成 `None`，返回一条错误给模型）。
**第二种一路绿灯**——文件写出来了，内容是坏的，没有哪一层会报错。

实测两家都转义正确，所以**这一章不为它写代码**。但风险是真的，等下一章真正开始写文件时再处理。

### 6.2 多余的参数被悄悄丢掉

```python
>>> await tools["run_shell"]({"command": "echo hi", "timeout": 5})
'hi\n'
```

模型传了 `timeout: 5`，代码用的还是默认的 30 秒，**没有人告诉模型它的意图被扔掉了**。
但既然实测中没有模型这么做过（§4.2），这一条**不修**。

### 6.3 描述里的数字和代码里的常量没有绑在一起

第 2 章交付的代码里：

```python
f"commands are killed after {30} seconds."   # tools.py
DEFAULT_TIMEOUT = 30.0                        # shell.py
```

两个 30 一致，纯粹是因为同一个下午写的。**改了任何一个，测试、类型检查、ruff 都不会说话。**
而 §5.6 刚证明了模型真的会把描述里的数字当真。

---

## §7 现在写代码

十条清单，复现三条（F03-02、F03-04/05、F03-07），确定地证实一条（F03-10），外加三个清单外的发现。
**代码只为复现了的写。** F03-04/05 只在名字含糊的工具上出现，而我们的 `read_file` 和 `run_shell`
名字已经说清了自己是什么，所以不需要改。

测试会放在一个新文件 `tests/test_schemas.py` 里，每写一段代码就加对应的测试。先建好文件开头：

```python
"""What the model is told, and what happens when it misreads it.

Fault IDs match FAULTS.md. Chapter 3 is unusual: most of the faults it went
looking for did not reproduce on either provider, and the tests here reflect
that -- there is no test for a fault that was never observed. What is tested
is the three that did reproduce, plus the two drift problems that are
deterministic and do not need a model at all.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

from minicodex.paths import resolve
from minicodex.shell import DEFAULT_TIMEOUT
from minicodex.tool_errors import tool_error
from minicodex.tools import TOOL_SCHEMAS, tool_schemas

SRC = Path(__file__).resolve().parent.parent / "src" / "minicodex"


def schema(name: str) -> dict:
    return next(t["function"] for t in TOOL_SCHEMAS if t["function"]["name"] == name)
```

> docstring 说出了这个文件的原则：**没观测到的故障，就不写测试。**
> `schema(name)` 是个小辅助函数：从 `TOOL_SCHEMAS` 里找出某个工具的描述。

### 7.1 `tool_errors.py`：让"下一步怎么做"成为必填项

F03-07 是复现得最强的一条，先做它。新建 `src/minicodex/tool_errors.py`：

```python
"""How a tool tells the model that something went wrong.

Measured, not preferred. Chapter 3 sent the same failed call back to two
models with two different error messages:

    bare:       "ValueError: invalid path"
    three-part: "Error: path must be relative to the repository root, not
                 absolute. You sent: /home/dev/... Send this instead: src/..."

gemma4:31b, given the bare one, sent an absolute path again three times out
of three -- it was trying to fix it (it dropped two directory levels) but had
no way to know which direction was wrong. Given the three-part one, it
recovered three times out of three.

gpt-4o-mini recovered from both. That is the trap: test the wording against
the stronger model only and every wording looks fine.

So the parts are not a style guide. They are the difference between a loop
that recovers on the next turn and one that spends its whole turn budget
resending the same call. `do_this` is a required keyword argument for that
reason -- an error message that does not say what to do next is the one that
measurably does not work.

This module imports nothing of ours. `tools.py` and `shell.py` both need it,
and `tools.py` already imports `shell.py`; putting it in either would build
the circular import that chapter 1 broke with `agent_types.py`. Same shape,
same answer, second time -- move the shared thing down to a leaf.
"""

from __future__ import annotations


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

> - docstring 记下了 §4.4 的测量，以及为什么三段式不是"风格偏好"，而是"循环能不能恢复"的区别。
> - **`do_this` 是必填的关键字参数**（写在 `*` 后面、没有默认值）。少写它，调用时就是 `TypeError`，
>   而不是一条效果差一点的错误信息。实测已经证明，"不说下一步怎么做"的错误信息会让弱模型陷进循环，
>   所以这件事要由代码保证，不能靠自觉——这正是第 1 章说的第四种值得加一层的情况：
>   **一条必须一直成立的规则**。
> - `you_sent` 是可选的，因为有些失败没有值得回显的输入（比如参数根本没给）。回显时超过 200 个字符
>   就截断。
> - 三部分用空格连成一行。

**它放在哪？** `tools.py` 和 `shell.py` 都要用它，而 `tools.py` 已经 import 了 `shell.py`。
放进其中任何一个，都会造出循环 import。**这是第 1 章那个问题第二次出现，解法一样**：放进一个
谁都不依赖的叶子模块。docstring 最后一段说的就是这个。

`shell.py` 里所有的错误都改成用它。开头加上 import：

```python
from minicodex.tool_errors import tool_error
```

`cd` 到不存在的目录：

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

以 `&` 结尾的命令：

```python
        if command.rstrip().endswith("&"):
            # Measured directly: a trailing `&` returns in milliseconds while
            # the backgrounded process keeps running, outside every mechanism
            # this class has for tracking or killing it -- it is not in
            # `chunks`, its exit code is not `proc.returncode`, and nothing
            # here will ever call `killpg` on it.  Actually supporting this
            # means a registry of background jobs and a way to poll them,
            # which is a real feature, not a two-line fix; refusing it here
            # is honest about the gap rather than silently leaking processes.
            return tool_error(
                "run_shell does not support backgrounded commands (trailing '&')",
                you_sent=command,
                do_this=(
                    "Run it in the foreground, or split the work into steps short "
                    "enough to finish within the timeout."
                ),
            )
```

缺少 `command` 参数：

```python
async def run_shell(session: ShellSession, args: dict[str, Any]) -> str:
    """The tool-callable wrapper.  `session` is bound in `tools.py` so the
    signature the model sees (`args` only) stays a single JSON object."""
    command = args.get("command")
    if not isinstance(command, str):
        return tool_error(
            'run_shell needs a "command" argument, a string',
            you_sent=repr(args.get("command")),
            do_this='Example: {"command": "pytest -q"}',
        )
    return await session.run(command)
```

**超时和超上限之后的说明，也要给出路。** §5.4 证明了描述里写限制没用，唯一有效的位置是出事**之后**：

```python
        if give_up == "timeout":
            # Saying "commands are killed after N seconds" in the tool
            # description was measured in chapter 3 and changed nothing: both
            # providers sent a two-minute command anyway, 6 runs out of 6.
            # This message is the one the model actually acts on, so it has to
            # carry the instruction, not just the fact.
            out += (
                f"\n... (killed: still running after {self.timeout:.0f}s. "
                "Run a smaller piece of the work -- a single test file or a "
                "single directory -- rather than the whole suite.)"
            )
        elif give_up == "ceiling":
            out += (
                f"\n... (killed: produced more than {READ_CEILING_CHARS} characters. "
                "Narrow the output -- add a filter, a head/tail, or a more "
                "specific path -- and run it again.)"
            )
        elif proc.returncode != 0:
```

> 注释把 §5.4 的测量记在了代码旁边。以后有人想删掉这段"啰嗦"的说明，会先看到它存在的理由。

第 2 章有一个测试因此失败了：

```python
assert "timed out after 1" in out       # 检查的是具体措辞
```

这一章重写了所有错误信息，它就失败了。**它检查的是措辞，而措辞正是这一章要改的东西。** 改成：

```python
@posix_only
async def test_F02_01_a_hanging_command_is_killed_on_timeout() -> None:
    session = ShellSession(timeout=1)
    start = time.monotonic()
    out = await session.run("sleep 30")
    elapsed = time.monotonic() - start

    assert elapsed < 5, f"took {elapsed}s -- the timeout did not fire"
    # Asserts that the kill was reported, not the exact sentence. Chapter 3
    # rewrote every error message and this was the only test that broke --
    # it was pinning wording that test_schemas.py now checks properly.
    assert "killed" in out
```

行为测试检查行为（"它被杀掉了"），措辞交给下面的专门测试管。
**一个测试失败，不一定是代码错了，也可能是这个测试检查了不该由它检查的东西。**

在 `tests/test_schemas.py` 里加上 F03-07 的测试：

```python
def test_F03_07_an_error_always_says_what_to_do_next() -> None:
    """`do_this` is keyword-only and required: the measured difference between
    a model that recovers and one that resends the same call is whether the
    message contains an instruction."""
    with pytest.raises(TypeError):
        tool_error("something broke")  # type: ignore[call-arg]


def test_F03_07_the_three_parts_are_all_present() -> None:
    message = tool_error(
        "no such file in the repository",
        you_sent="model.py",
        do_this="Send this instead: src/minicodex/model.py",
    )
    assert message.startswith("Error: ")
    assert "You sent: model.py" in message
    assert "Send this instead: src/minicodex/model.py" in message


def test_F03_07_a_long_input_is_truncated_not_echoed_whole() -> None:
    message = tool_error("too long", you_sent="x" * 5000, do_this="Send less.")
    assert len(message) < 400


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

> - 第一个：不传 `do_this` 就是 `TypeError`。`pytest.raises(TypeError)` 断言会抛出这个异常。
> - 第二、三个：三部分都在；很长的输入被截断，而不是整段回显。
> - **第四个把规则本身写成测试**：`tools.py`、`shell.py`、`paths.py` 里，不允许有任何 `return` 语句
>   返回一个以 `Error:` 开头、手工拼出来的字符串——所有错误都必须经过 `tool_error()`。
>   它用 `ast` 遍历每个 `return` 语句里的字符串常量，而不是在源码里搜索文字，因为 `Error:`
>   这个词会出现在注释和 docstring 里，文字搜索会误报。（作者专门反向验证过：往 docstring 里
>   写一句 `returning "Error: something"`，测试**不**报警。）

```bash
git add .
git commit -m "feat: make every tool error say what to do next"
```

### 7.2 `paths.py`：把 F03-02 从描述搬到代码里

§5.3 证明了描述解决不了路径问题。那代码能确定地解决哪些情况？

- **指向仓库**内**的绝对路径**：没有歧义，直接接受（OpenAI 的习惯，不用打扰模型）
- **指向仓库**外**的绝对路径**：拒绝（这也是以后安全边界的雏形）
- **只匹配到一个文件的裸文件名**：没有歧义，**在错误信息里直接告诉它是哪个**（Ollama 的习惯）

最后一条，正好就是 §4.4 里被实测证明有效的 D2 那种措辞。新建 `src/minicodex/paths.py`：

```python
"""Turning what the model sent into a file we can open.

Chapter 3 measured what two models actually send when asked to read
`src/minicodex/model.py`, given only "Path to the file." as the description:

    gpt-4o-mini    /home/dev/minicodex/model.py     absolute, wrong file
    gemma4:31b     model.py                         relative, wrong file

Both wrong, in different directions. Stating the rule in the description
fixed both -- but only when the example in the description happened to be the
answer. Change the example to a different file, keep the sentence identical,
and both models go back to being wrong, three times out of three. They were
copying the example, not learning the rule.

Which means this is not a description problem. An absolute path inside the
repository is unambiguous and can simply be accepted; a name that matches
exactly one file is unambiguous and can be named in the error. That is
deterministic work, and the rule is: anything that can be settled in code
should not be handed to the model.
"""

from __future__ import annotations

from pathlib import Path

from minicodex.tool_errors import tool_error

# Directories whose contents are never what the model meant.
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


def resolve(raw: str, root: Path) -> tuple[Path | None, str | None]:
    """(path, None) if it resolves to a file inside `root`, else (None, error).

    The error is always a three-part message -- chapter 3 measured what
    happens with the other kind.
    """
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
                "that path is outside the repository, and this tool only reads files inside it",
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

逐段说：

> - **`_SKIP`**：找候选文件时跳过的目录。`.git`、`.venv` 里的同名文件，永远不是模型要的。
> - **`_candidates(name, root)`**：在仓库里找所有叫 `name` 的文件。路径的**任何一段**在跳过名单里
>   就跳过；返回仓库内的相对路径，并把 Windows 的 `\` 统一成 `/`（给模型看的形式要一致）；
>   找到超过 5 个就停，不在大仓库里一直找下去。
> - **`resolve(raw, root)`** 返回一对值：成功时是 `(文件路径, None)`，失败时是 `(None, 错误信息)`。
>   1. 不是非空字符串：报错，并给一个例子。
>   2. 是绝对路径：用 `relative_to(root)` 判断它在不在仓库里。在，就换成相对路径继续；
>      不在（`relative_to` 抛 `ValueError`），就报错。
>   3. 拼出完整路径。是目录：报错。
>   4. 不存在：用**文件名部分**去全仓库找候选。恰好一个，就说"发这个"；有几个，就列出来；
>      一个都没有，就教它用 `ls` 或 `find` 去找。
>   5. 都没问题：返回路径。
> - docstring 最后一段写的就是 §5.3 的结论：**这不是描述的问题。能在代码里确定解决的，
>   就不要交给模型。**

拿两家实测时发出的真实路径喂进去：

```
what gpt-4o-mini sent (absolute, wrong file):
  Error: no such file in the repository You sent: /tmp/step03_build/model.py
  Send this instead: src/minicodex/model.py

what gemma4:31b sent (relative, wrong file):
  Error: no such file in the repository You sent: model.py
  Send this instead: src/minicodex/model.py
```

**两种错法，收敛到同一条三段式信息**，而且正是 D2 里被证明有效的那种形状。

然后 `read_file` 改成用它。`read_file` 需要知道仓库根目录在哪，所以多了一个 `root` 参数，
由 `default_tools()` 用 `functools.partial` 绑定——和第 2 章绑定 `session` 是同一个做法。
下一节给出 `tools.py` 的全部内容。

在 `tests/test_schemas.py` 里加上 F03-02 的测试：

```python
def test_F03_02_an_absolute_path_inside_the_repo_is_accepted(tmp_path: Path) -> None:
    """What gpt-4o-mini sent 3/3 with a vague description. It is unambiguous,
    so it is resolved rather than bounced back."""
    (tmp_path / "src").mkdir()
    target = tmp_path / "src" / "model.py"
    target.write_text("x = 1")

    got, error = resolve(str(target), tmp_path)

    assert error is None
    assert got == target.resolve()


def test_F03_02_an_absolute_path_outside_the_repo_is_refused(tmp_path: Path) -> None:
    # Built from tmp_path so it is absolute on every platform; "/etc/hosts"
    # has no drive letter and is not absolute on Windows.
    outside = tmp_path.parent / "somewhere-else" / "hosts"
    got, error = resolve(str(outside), tmp_path)
    assert got is None
    assert error is not None
    assert "outside the repository" in error


def test_F03_02_a_bare_filename_names_the_file_it_meant(tmp_path: Path) -> None:
    """What gemma4:31b sent 3/3: `model.py`, relative but not the file. One
    match exists, so the error can simply say which one."""
    (tmp_path / "src" / "minicodex").mkdir(parents=True)
    (tmp_path / "src" / "minicodex" / "model.py").write_text("x = 1")

    got, error = resolve("model.py", tmp_path)

    assert got is None
    assert error is not None
    assert "src/minicodex/model.py" in error


def test_F03_02_several_matches_are_all_offered(tmp_path: Path) -> None:
    for sub in ("a", "b"):
        (tmp_path / sub).mkdir()
        (tmp_path / sub / "utils.py").write_text("x = 1")

    _, error = resolve("utils.py", tmp_path)

    assert error is not None
    assert "a/utils.py" in error and "b/utils.py" in error


def test_F03_02_ignored_directories_are_not_offered(tmp_path: Path) -> None:
    """A match inside .git or __pycache__ is never what the model meant."""
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "config.py").write_text("x = 1")

    _, error = resolve("config.py", tmp_path)

    assert error is not None
    assert ".git" not in error
```

> - 每个测试都在 `tmp_path`（pytest 给的临时目录）里造一个小"仓库"，然后调用 `resolve`。
> - **"仓库外的绝对路径"那个测试**，路径是用 `tmp_path.parent` 拼出来的。最初写的是 `"/etc/hosts"`，
>   在 Windows 上它没有盘符，不算绝对路径，测试就失败了。**测试里的路径也要在每个系统上成立。**
> - 最后一个：`.git` 和 `.venv` 里的同名文件不会出现在候选里。

```bash
git add .
git commit
```

```
fix: resolve the path the model actually sends, instead of describing it better

Measured: with "Path to the file." gpt-4o-mini sent an absolute path and
gemma4 an incomplete relative one, 3/3 each. Stating the rule fixed both
only while the example in the description was the answer; changing the
example flipped both back to 3/3 wrong. So paths.resolve() accepts an
absolute path inside the repository, refuses one outside it, and names
the file when a bare name matches exactly one.

Deliberately not done, because it was measured and did not reproduce:
additionalProperties/strict (no model invented a field in four scenarios),
"do not use for" on read_file/run_shell (both picked right 3/3), enum
instead of prose (no difference, and enum does not stop a wrong valid
value), double-escape detection (both providers escaped correctly).
```

> **一个"我没做 X"的决定，比"我做了 Y"更需要写进 commit message。** 半年后有人会问
> "为什么这里没加 strict schema"，而代码的改动里永远不会有答案。

### 7.3 描述从常量生成

§6.3 的问题：描述里的 `30` 和 `DEFAULT_TIMEOUT` 没有绑在一起。改成一个函数，数字从参数生成。
这是 `tools.py` 的全部内容：

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
from pathlib import Path
from typing import Any

from minicodex.paths import resolve
from minicodex.shell import DEFAULT_TIMEOUT, ShellSession
from minicodex.shell import run_shell as _run_shell
from minicodex.tool_errors import tool_error


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


def default_tools(root: Path | None = None) -> dict[str, Any]:
    """Build a fresh tool table bound to one repository and one shell session.

    Both tools are `functools.partial` now. `ShellSession` holds state (cwd,
    env) that belongs to one conversation; `root` is what makes it possible to
    decide whether an absolute path is inside the repository, and to name a
    near-miss file in the error. Neither belongs to the process, so neither
    lives at module level.
    """
    root = (root or Path.cwd()).resolve()
    session = ShellSession()
    return {
        "read_file": functools.partial(read_file, root),
        "run_shell": functools.partial(_run_shell, session),
    }


def tool_schemas(timeout: float = DEFAULT_TIMEOUT) -> list[dict[str, Any]]:
    """What the model is shown.

    A function taking the timeout rather than a module-level constant with the
    number typed into the string: chapter 2 shipped `f"...killed after {30}
    seconds"` next to `DEFAULT_TIMEOUT = 30.0`, which agreed only because both
    were written on the same afternoon. Nothing would have caught the day
    someone changed one of them. Now there is only one number, and a test
    asserts the sentence matches it.
    """
    return [
        {
            "type": "function",
            "function": {
                "name": "read_file",
                "description": (
                    "Read a UTF-8 text file from the repository and return its contents."
                ),
                "parameters": {
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
            },
        },
        {
            "type": "function",
            "function": {
                "name": "run_shell",
                "description": (
                    "Run a shell command and return its combined stdout and stderr. "
                    "The working directory persists across calls within one session "
                    "(cd changes it for subsequent calls). Backgrounded commands "
                    "(trailing '&') are not supported. Long-running or silent "
                    f"commands are killed after {timeout:.0f} seconds."
                ),
                "parameters": {
                    "type": "object",
                    "required": ["command"],
                    "properties": {
                        "command": {
                            "type": "string",
                            "description": "The shell command to run.",
                        }
                    },
                },
            },
        },
    ]


# Kept as a module-level name because `__main__` and the tests both want the
# default, and calling it once here is cheaper than threading it through.
TOOL_SCHEMAS: list[dict[str, Any]] = tool_schemas()

__all__ = ["TOOL_SCHEMAS", "default_tools", "read_file", "tool_error", "tool_schemas"]
```

> - 开头的 docstring 记下了 §4、§5 的几条测量，以及描述里每句话为什么留着或不加。
> - **`_read`** 现在用 `errors="replace"` 读文件，和第 2 章处理命令输出的做法一致。
> - **`read_file(root, args)`**：先 `resolve`，失败就把错误信息原样返回；成功就在线程里读文件。
>   `assert path is not None` 是给类型检查器看的：`resolve` 保证错误为空时路径一定有值。
> - **`default_tools(root=None)`**：没给 `root` 就用当前目录。`read_file` 绑 `root`，`run_shell` 绑
>   `session`。
> - **`tool_schemas(timeout=DEFAULT_TIMEOUT)`**：描述里的秒数由 `f"{timeout:.0f}"` 生成。
>   `read_file` 的 `path` 说明改成了"相对于仓库根目录"，并给了例子。
> - **`TOOL_SCHEMAS = tool_schemas()`**：保留一个模块级的名字，`__main__.py` 和测试都用它，不用改。
> - **`__all__`** 列出这个模块对外提供的东西。

为什么还留着 "killed after 30 seconds" 这句话？§5.4 证明它对模型毫无影响。留下的理由是它对**人**
有用——看请求记录时，能解释为什么会出现 `killed` 的输出，而代价是每轮多几个词。**但这个理由没有
经过实测，是作者的判断。** docstring 里也是这么写的。

测试：

```python
def test_the_timeout_in_the_description_matches_the_timeout_in_the_code() -> None:
    """Chapter 2 shipped `f"...killed after {30} seconds"` beside
    `DEFAULT_TIMEOUT = 30.0`. They agreed because both were typed the same
    afternoon, and nothing would have noticed the day one of them changed."""
    described = re.findall(r"\b(\d+)\s*seconds?\b", schema("run_shell")["description"])
    assert described == [f"{DEFAULT_TIMEOUT:.0f}"]


def test_changing_the_timeout_changes_the_sentence() -> None:
    """The real assertion: the number is generated, not typed."""
    other = tool_schemas(timeout=90.0)
    described = next(
        t["function"]["description"] for t in other if t["function"]["name"] == "run_shell"
    )
    assert "90 seconds" in described
    assert "30 seconds" not in described
```

> - 第一个：描述里的数字等于 `DEFAULT_TIMEOUT`。`re.findall(r"\b(\d+)\s*seconds?\b", ...)` 用正则表达式
>   找出所有"数字 + seconds"里的数字。
> - **第二个才是真正的检查**：传 `timeout=90`，句子里就得是 90。§8 会看到为什么只有第一个不够。

```bash
git add .
git commit -m "fix: generate the timeout in the description from the constant"
```

### 7.4 描述快照（F03-10 的简易版）

§5.3 证明了改一个例子就能让两家从全对变成全错。**所以描述的任何改动都是行为的改动**，不该能随手改掉。
在 `tests/test_schemas.py` 末尾加上：

```python
# ---------------------------------------------------------------------------
# F03-10  a description edit degrades unrelated tasks
# ---------------------------------------------------------------------------

# Every word the model is shown. Chapter 3 measured that changing a single
# example in a description flipped both providers from 3/3 correct to 3/3
# wrong, so a description edit is a behaviour change and should not be
# possible to make by accident. Updating this dict is the confirmation.
#
# This is the cheap version of F03-10. The real one -- run a task set before
# and after and compare -- needs the harness chapter 14 builds.
EXPECTED_DESCRIPTIONS = {
    "read_file": "Read a UTF-8 text file from the repository and return its contents.",
    "read_file.path": (
        "Path to the file, relative to the repository root. Example: src/minicodex/model.py"
    ),
    "run_shell": (
        "Run a shell command and return its combined stdout and stderr. The working "
        "directory persists across calls within one session (cd changes it for "
        "subsequent calls). Backgrounded commands (trailing '&') are not supported. "
        "Long-running or silent commands are killed after 30 seconds."
    ),
    "run_shell.command": "The shell command to run.",
}


def test_F03_10_descriptions_are_pinned() -> None:
    actual = {}
    for tool in TOOL_SCHEMAS:
        fn = tool["function"]
        actual[fn["name"]] = fn["description"]
        for param, spec in fn["parameters"]["properties"].items():
            actual[f"{fn['name']}.{param}"] = spec["description"]

    assert actual == EXPECTED_DESCRIPTIONS


# ---------------------------------------------------------------------------
# schema shape
# ---------------------------------------------------------------------------


def test_every_parameter_has_a_description() -> None:
    for tool in TOOL_SCHEMAS:
        fn = tool["function"]
        for param, spec in fn["parameters"]["properties"].items():
            assert spec.get("description"), f"{fn['name']}.{param} has no description"


def test_every_required_parameter_exists_in_properties() -> None:
    for tool in TOOL_SCHEMAS:
        params = tool["function"]["parameters"]
        for name in params.get("required", []):
            assert name in params["properties"], f"{tool['function']['name']}: {name}"
```

> - **`EXPECTED_DESCRIPTIONS`** 把模型能看到的每一句描述都抄了一遍。改描述，就必须同时改这里——
>   **这个"麻烦"就是这个测试的全部意义。** 它是一张"快照"：记下当前的样子，任何变化都会被发现。
> - `test_F03_10_descriptions_are_pinned` 把实际的描述收集成同样形状的字典，逐项比较。
> - 另外两个是基本的结构检查：每个参数都有说明；`required` 里列出的参数确实存在。

完整的 F03-10（改描述之前和之后各跑一遍任务集，比较结果）需要一套评估工具，以后会专门做。

```bash
git add .
git commit -m "test: pin every tool description"
```

### 7.5 决定**不**做的四件事

这一节和上面几节一样重要。

| 不做 | 因为实测 |
|---|---|
| `additionalProperties: false` + `strict` | 四个场景两家，**零次**编参数。模型宁可绕路（`timeout 5s pytest`、`sed -n '40,60p'`） |
| 给 `run_shell` 加"不要用于读文件" | 两家本来就 3/3 选对了 `read_file`。只有名字含糊的工具才需要 |
| 把固定取值改成 `enum` | 和文字描述**零差别**。而且场景 I 说明 `enum` 也防不住选错值 |
| 检测双重转义 | 两家都转义正确，**没观测到**。下一章真正写文件时再说 |

> **不为没观测到的行为写代码。** 这四条每一条都是"常见建议"，写上去没人会说你错。但每一条都会增加代码、
> 增加每轮发送的文字、增加以后要维护的分支——**而实测说它们现在不解决任何问题。**

---

## §8 验证

### 8.1 把修复改回去

| 改回去 | 结果 |
|---|---|
| 绝对路径不再检查是否在仓库内 | 失败，仓库外的路径被放行 |
| 找不到文件时不给候选 | 失败 ×2 |
| `do_this` 改成可选参数 | 失败（`DID NOT RAISE TypeError`） |
| 在 `paths.py` 里手写一个错误字符串 | 失败，报出具体的行号 |
| 描述里的秒数改回写死的 30 | 失败——**但只有第二个测试抓到了** |
| 偷偷改一句描述 | 失败，列出具体的差异 |

（这张表来自写作时的实测。）

倒数第二行值得单独说。把 `f"{timeout:.0f}"` 改回写死的 `30`，**第一个测试仍然通过**——因为
`DEFAULT_TIMEOUT` 恰好就是 30。只有第二个测试（传 90 进去）抓到了。

> **只检查"现在两个值一样"，抓不到写死的数字。必须检查"改变来源，结果跟着变"。**

### 8.2 全部跑一遍

```
$ uv run pytest
92 passed, 7 skipped in 9.60s
```

（Windows 上实测；7 个跳过的测试是第 2 章的 F02-10。）

---

## §9 回顾：十条猜测，三条成立

| 编号 | 猜测 | 实测结果 | 挡住它的东西 |
|---|---|---|---|
| F03-01 | 参数名在工具间混用 | ❌ **没复现**：两家都用各自 schema 里的名字 | —— |
| F03-02 | 相对/绝对没说清，模型乱给 | ✅ **复现**，而且两家错的方向相反 🟡 | `paths.resolve()`，**在代码里**解决 |
| F03-03 | 模型编出参数 | ❌ **没复现**：模型改用已有参数绕路 | —— |
| F03-04 | 只说做什么、不说什么时候别用 | ✅ **复现**（仅限名字含糊的工具）🟡 | "Do not use this for…" |
| F03-05 | 两个重叠的工具，模型乱选 | ✅ **复现**（同上）🟡 | 同上 |
| F03-06 | 取值写在文字里而不是 `enum` | ❌ **没复现**，而且 `enum` 也防不住选错值 | —— |
| F03-07 | 只返回 `ValueError`，模型原样重试 | ✅ **强烈复现**，弱模型 3/3 卡住 🔵 | `tool_error()`，`do_this` 必填 |
| F03-08 | 必填参数埋在最后 | ❌ **没复现**，6/6 都传了 | —— |
| F03-09 | 多行代码在 JSON 转义时出错 | ❌ **没复现**（但双重转义确定是静默的）| 留给下一章 |
| F03-10 | 改一句描述，别的任务跟着变差 | ✅ **确定地证实**（换个例子，结果翻转）🟢 | 描述快照测试 |
| *新* | 描述里写的限制，对行为毫无影响 | ⚠️ 6/6 无差别 🟢 | 把指令放进错误信息 |
| *新* | 描述里的默认值被模型原样传回 | ⚠️ 从原始输出里看到的 🟠 | 描述从常量生成 |
| *新* | 多余参数被悄悄丢掉 | ⚠️ 确定的 🟢 | **不修**（没有模型这么做过） |

标记：🔴 崩溃 · 🟡 静默 · 🟢 主动边界测试 · 🔵 长时间运行才出现 · 🟠 看日志发现 · 🟣 代码审查 ·
⚫ 用户报告 · ⚪ lint/类型检查

**十条清单，成立三条（加一条确定证实），另外发现三条。**

前几章的猜测大多成立，这一章大多不成立。区别在于：前几章的猜测来自**自己看到的数据**（第 0、1 章）
或**操作系统公认的边界**（第 2 章）；这一章的清单来自**别人的经验**，而别人的模型和你的不一样。

没成立的七条，`FAULTS.md` 里照样留着，标明"NOT REPRODUCED"和证据——**负结果也是结果**。
下一次换模型时，这些就是要重测的清单。

---

## §10 交给 GitHub

```bash
git push -u origin feat/tool-descriptions
```

开 PR、等 CI、自己审查、合并。这一章的提交：

```
test: pin every tool description
fix: generate the timeout in the description from the constant
fix: resolve the path the model actually sends, instead of describing it better
feat: make every tool error say what to do next
```

### 10.1 审查时提出的问题

**1 · `paths.py` 的 `rglob` 在大仓库里会不会很慢？**

> **回答**：会。找到 5 个就停、跳过 `.git` 和 `node_modules`，但在一个几十万文件的仓库里，
> 一次"找不到文件"的调用仍然要遍历很多目录。**接受这个风险，因为它只发生在出错的路径上。**
> 真成了问题，正确的解法是维护一份文件索引——那要等到有第二个功能也需要它时再做。
> （"三次法则"的一种变体：一件事第一次出现时直接写，重复出现了再抽象。）

**2 · `resolve()` 返回 `tuple[Path | None, str | None]`，两个值有四种组合，其中两种是不合法的。**

> **回答**：同意这是个不够严格的类型。更好的写法是一个"成功或失败"的专门类型。**但现在只有一个地方
> 调用它**，收益几乎为零。等第二个工具也需要解析路径时（下一章写文件的工具一定需要），
> 有了两个例子，再看真正的共同点在哪。

**3 · `EXPECTED_DESCRIPTIONS` 把描述抄了一遍，改描述要改两个地方。**

> **回答**：**这正是它的目的。** 快照测试的价值就在于让改动变麻烦。如果它能自动跟着变，
> 它就什么都测不到了。

**4 · 既然实测证明 "killed after 30 seconds" 对模型毫无影响，为什么不删掉？**

> **回答**：犹豫过。留下的理由是它对人有用（§7.3），代价是每轮几个词。**但这个理由没有经过实测，
> 是我的判断。** 哪天要精简描述，它应该是第一批被删的——而且删之前应该用任务集跑一遍，
> 而不是像现在这样凭感觉。

**5 · 探测脚本里判断结果的逻辑，和真实代码没有共享，会不会越走越远？**

> **回答**：会，而且已经发生了——§5.1 那个"合法但错误"的 `n`，就是脚本的判断比真实需求宽松导致的。
> 但两者**不该**共享代码：脚本判断的是"我关心什么"，真实代码检查的是"什么是合法的"，
> 两者不同才有信息量。**代价是：必须看原始输出，不能只看标签。**

---

## §11 本章给 CI 加了什么

**什么都没加，而且这一章最重要的那部分，CI 根本跑不了。**

这一章的核心结论全部来自真实模型的 A/B 采样。CI 里没有 Ollama，也不该放 OpenAI key。
`test_schemas.py` 里的测试全部是确定的——它们保护的是**从实测中得出的结论**（三段式、路径解析、
描述快照），而不是重新验证结论本身。

> **实测发现规律，测试固化规律。** 这两件事用的是完全不同的手段，混在一起，就会得到一个又慢又不稳定的 CI。

---

## §12 三条主线各自留下了什么

### 主线 A · 需求变代码

**描述是提示，不是文档。** 它的好坏只能测，不能读出来。

**能在代码里解决的，就不要推给模型。** 路径问题在描述里解决不了（模型只会抄例子），在代码里能确定地解决。

**不为没观测到的行为写代码。** 这一章的"不做"清单和"做"的清单一样长。

### 主线 B · 工程化交付

| 动作 | 本章的规则 |
|---|---|
| commit message | "我没做 X，因为测过没复现"比"我做了 Y"更需要写下来 |
| 快照测试 | 让改动变麻烦，就是它的意义 |
| 行为测试 | 检查行为，不检查措辞；措辞由专门的测试管 |
| 测量工具 | 先在假数据上测它自己；请求失败和模型行为要分开记 |
| 平台 | 测试里的路径要在每个系统上都成立 |

### 主线 C · 故障

**第一招：别人的故障清单是猜测，不是事实。** 十条，成立三条。

**第二招：事前在描述里预防无效，事后在错误信息里纠正有效。** 而且便宜的那个更管用。

**第三招：只用强模型测，你会以为一切都好。** 同一条错误信息，`gpt-4o-mini` 每次都能自己改正，
`gemma4` 每次都卡住。第 1 章是"服务越宽松，bug 越晚发现"；这一章是"模型越聪明，bug 越晚发现"。

**第四招：只检查"两个值现在一样"，抓不到写死的数字。** 要检查"改变来源，结果跟着变"。

---

## 如果你只记住三件事

1. **别人的故障清单是猜测，不是事实。** 这一章列了十条，实测成立三条。照着清单写代码，
   会为七个不存在的问题增加复杂度。
2. **事前在描述里预防无效，事后在错误信息里纠正有效。** 描述每轮都要发，错误信息只在出错时发一次——
   便宜的那个还更管用。
3. **模型会抄例子，而不是理解规则。** 能在代码里确定解决的，就不要写在描述里指望模型。

---

## 动手

```bash
cd steps/step03_tool_descriptions
uv sync --all-extras
uv run pytest
```

**建议自己做一遍的三件事**（需要 Ollama 或 OpenAI key）：

1. 把 §3 的 `probe.py` 放进项目根目录，对着你自己的模型跑 A1 和 A2 两种描述。结果和书上一样吗？
   **如果不一样，那正是这一章的核心结论：你的模型不是书里的模型。**
2. 把 `VARIANTS` 换成 §5.3 的 M2 和 M3（只换例子里的文件名），看你的模型是不是也在抄例子。
3. 给 `run_shell` 的描述加一句 "Prefer running a single test file over the whole suite."，然后测 §5.4 的场景
   （让模型跑一个要两分钟的测试套件）。如果它仍然 3/3 发出整个测试套件，你就亲手复现了这一章最有用的
   那条负面结论。

---

## 选读 · codex 是怎么做的

> 基于写作时（2026 年）的 codex 仓库，以后可能会变。不读不影响后面的内容。

**codex 的工具描述写在 Markdown 文件里，而不是代码里的字符串。** `codex-rs/core/src/tool_descriptions/`
下面是一堆 `.md` 文件，编译时嵌进程序。理由和 §7.4 一样：**描述是需要被审查的内容**，放在 `.md` 里，
PR 的改动才读得懂；拼在代码里的字符串，审查时没人看得出改了什么。

**错误信息也是精心写的提示。** `exec.rs` 和 `apply_patch/` 里的错误分支，几乎每一条都带着"下一步该怎么做"。

**`apply_patch` 用的是自定义的文本格式，不是 JSON 参数。** 这直接对应 §6.1：多行代码穿过 JSON 转义有风险，
codex 的选择是**干脆不穿**——用纯文本格式，模型不需要转义任何东西。下一章会走到这一步。

---

**下一章**：[文件编辑 `apply_patch`](ch04-apply-patch.md)——写文件比读文件危险得多，而 §6.1 那个
"双重转义"的风险，到时候就不再是理论了。

---

# 附录 · 对照检查

## T1 · 每个文件是在哪一节写的

本章结束时，你的项目内容应该和 `steps/step03_tool_descriptions/` 一致（测试函数的先后顺序可以不同）。

| 文件 | 在哪写的 |
|---|---|
| `src/minicodex/tool_errors.py` | §7.1 |
| `src/minicodex/shell.py` | §7.1（改用 `tool_error`，超时和超上限的说明） |
| `src/minicodex/paths.py` | §7.2 |
| `src/minicodex/tools.py` | §7.3（完整版） |
| `tests/test_schemas.py` | §7 开头建文件，§7.1–§7.4 各加一部分 |
| `tests/test_shell.py` | §7.1（一个测试改为检查 `killed`） |

`probe.py` 是临时的测量脚本，不属于项目。

## T2 · 常见报错对照表

| 症状 | 原因 | 修法 |
|---|---|---|
| `TypeError: tool_error() missing 1 required keyword-only argument: 'do_this'` | 没写"下一步做什么" | 补上 `do_this=`——这正是设计的本意 |
| `ImportError` 循环 import | 把 `tool_error` 放进了 `tools.py` 或 `shell.py` | 放进独立的 `tool_errors.py`（§7.1） |
| `test_F03_07_no_module_builds_an_error_string_by_hand` 失败 | 某个 `return` 手工拼了 `"Error: ..."` | 改用 `tool_error()` |
| `test_F03_10_descriptions_are_pinned` 失败 | 改了描述，没改快照 | 确认这个改动是有意的，再同步修改 `EXPECTED_DESCRIPTIONS` |
| `test_F02_01_...` 在检查 `"timed out after 1"` 时失败 | 超时的说明改了措辞 | 改成检查 `"killed"`（§7.1） |
| `probe.py` 每行都是 `REQUEST FAILED` | 模型服务没开，或地址、模型名不对 | 检查 Ollama 是否运行、`ollama list` 里有没有这个模型 |
| 模型一直发绝对路径 | 这是 OpenAI 的习惯 | `resolve` 会接受仓库内的绝对路径，不需要改描述 |
