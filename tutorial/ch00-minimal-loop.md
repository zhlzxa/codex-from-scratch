# 第 0 章 · 让它先去查，再回答

> **代码**：`steps/step00_minimal_loop/`
> **分支**：`feat/agent-loop`
> **产出**：一个必须先读文件、才能回答问题的程序
> **前置**：做完第 -1 章，手上有一个能装、能测、已经提交过的 `minicodex` 项目。
> 本地有没有 Ollama（一个在自己电脑上跑大模型的工具）都可以，§0.3 讲两种情况怎么准备。

---

## §0 开工前的准备

这一章第一次让程序和大模型对话。在写代码之前，有四样东西必须先弄明白：
程序怎么和大模型"说话"，什么是"工具调用"，为什么回答是一小片一小片流过来的，
以及 Python 的 `async`/`await`。它们是这一章每一行代码的地基。

### 0.1 四个新概念

**一、程序怎么和大模型说话：HTTP 请求。**

大模型跑在一个服务程序里（本章用 Ollama）。你的程序通过 **HTTP** 给它发消息——
和浏览器打开网页用的是同一套协议。

- 服务程序在一个**地址**上等着，比如 `http://localhost:11434/v1/chat/completions`。
  `localhost` 指"这台电脑自己"，`11434` 是**端口号**（同一台电脑上区分不同服务的编号），
  后面的路径叫**端点（endpoint）**，表示你要用它的哪项功能。
- 你发一个 **POST 请求**，请求里带一段 JSON（就是 Python 的字典和列表写成文本的样子）。
- 它回一个**响应**，带一个**状态码**：`200` 表示成功，`400` 表示你发的东西有问题，
  `500` 表示它自己出了问题。

请求体里最重要的是 `messages`，一个**消息列表**。每条消息是一个字典，有 `role`（角色）
和 `content`（内容）：

```python
[
    {"role": "system", "content": "You are a coding agent."},        # 给模型的总体说明
    {"role": "user", "content": "What does __init__.py define?"},     # 用户说的话
    {"role": "assistant", "content": "It defines __version__ ..."},   # 模型之前说的话
]
```

**模型不记得任何事。** 每一次请求，你都要把**到目前为止的全部对话**重新发一遍，
它才知道前面聊过什么。这个列表通常叫**历史（history）**，本章会反复出现。

**二、工具调用：模型不能自己做事，但可以请你做。**

模型只会输出文字，它打不开你硬盘上的文件。**工具调用**是这样一个约定：

1. 你在请求里告诉模型："你可以用这些工具"，并描述每个工具叫什么、要什么参数。
   这份描述叫工具的 **schema**。
2. 模型回复时，可以不直接回答，而是说："请帮我调用 `read_file`，参数是
   `{"path": "src/minicodex/__init__.py"}`"。
3. **你的程序**真的去读这个文件，把读到的内容作为一条 `role` 为 `"tool"` 的消息，
   追加到历史里。
4. 再把整个历史发给模型。这次它看到了文件内容，就能回答了。

问模型 → 它要工具 → 你执行 → 把结果给它 → 再问模型……**这个循环就是 Agent（智能体）。**
这一章要写的就是它。

**三、流式响应：回答是一小片一小片流过来的。**

大模型生成一段话要好几秒。如果等它全部生成完再一次性返回，用户就得盯着空白屏幕干等。
所以请求里可以加 `"stream": true`，让服务端**生成一点就发一点**。

这种一点点发的格式叫 **SSE（Server-Sent Events）**。它是纯文本，一行一条：

```
data: {"choices": [{"delta": {"content": "I"}}]}
data: {"choices": [{"delta": {"content": " will read"}}]}
data: [DONE]
```

每行以 `data: ` 开头，后面是一段 JSON；每一段叫一个 **chunk（片）**，里面 `delta`
的意思是"这次新增的部分"。最后一行 `data: [DONE]` 表示"发完了"。§3 会看到真实的样子。

**四、`async`/`await`：等待的时候别干等。**

Agent 的绝大部分时间都在**等**：等模型回复（几秒）、等文件读完、以后还要等命令跑完。
普通的 Python 函数在等的时候什么都不干。`async`/`await` 让程序在等一件事的时候，
可以先去做别的。

先看最小的例子：

```python
import asyncio


async def greet(name: str) -> str:      # async def 定义的是"协程函数"
    await asyncio.sleep(1)              # await：在这里等，等的时候程序可以去干别的
    return f"hello {name}"


async def main() -> None:
    text = await greet("world")         # 调用协程函数，必须 await 才会真正执行
    print(text)


asyncio.run(main())                     # 从普通代码进入 async 世界的入口
```

记住四条规则就够本章用了：

1. **`async def` 定义的函数，调用它不会执行函数体**，只会得到一个"协程对象"。
   必须 `await` 它，函数体才会跑。**忘了写 `await`，函数就等于没调用**——而且不报错。
   这一条本章会真的撞上。
2. **`await` 只能写在 `async def` 里面。** 所以一旦某个底层函数是 async 的，调用它的
   函数也得是 async 的，一路传染到最外层。最外层用 `asyncio.run(...)` 启动。
3. **`async with` 和 `async for`** 是 `with` 和 `for` 的异步版本：打开连接、逐行读取
   这类会等待的操作，要用它们。
4. **只有在 `await` 的地方，程序才会去干别的。** 如果你在 `async def` 里调用一个普通的、
   会卡住的函数（比如 `time.sleep(1)` 或者读一个大文件），整个程序都会停在那里，
   谁也干不了。管理这一切的东西叫**事件循环（event loop）**，`asyncio.run` 就是在启动它。

### 0.2 本章会遇到的新 Python 写法

第一次出现时都会再解释，这里先有个印象：

| 写法 | 意思 |
|---|---|
| `@dataclass` 写在 `class` 上一行 | 自动生成 `__init__`、`__eq__` 等方法的"数据类"，适合只装数据的类 |
| `@dataclass(frozen=True)` | 冻结的数据类：创建之后字段不能再改 |
| `A \| B`（在类型标注里） | "A 或 B 类型之一" |
| `yield`（在函数里） | 让函数变成**生成器**：每 `yield` 一次就交出一个值，调用方可以边拿边用 |
| `class X(Protocol)` | 描述"一个对象应该有哪些方法"，不需要继承 |
| `isinstance(x, T)` | 判断 `x` 是不是 `T` 类型 |
| `f"{x!r}"` | 在 f-string 里用 `repr(x)` 显示 `x`，字符串会带上引号，便于看清内容 |
| `*[...]` 写在列表里 | 把另一个列表的元素**展开**放进来 |
| `try: ... except X as exc:` | 捕获 `X` 类型的异常，`exc` 是那个异常对象 |

### 0.3 准备一个能对话的模型

本章的代码需要一个"会说 OpenAI 格式的服务"。两种办法，选一种：

**办法一：装 Ollama（有一块像样的显卡，或者愿意用它的云端模型）。**

从 <https://ollama.com> 下载安装。装好后在终端里拉一个支持工具调用的模型：

```bash
ollama pull qwen3
```

Ollama 装好后会在后台运行，地址是 `http://localhost:11434`。

> 本章代码里的默认模型名是 `gemma4:31b-cloud`，带 `-cloud` 后缀的是 Ollama 的**云端模型**，
> 在 Ollama 的服务器上跑，需要先登录 Ollama 账号（`ollama signin`）。用本地模型的话，
> 运行时加上 `--model qwen3` 即可。

**办法二：不装 Ollama，用仓库里带的录音服务。**

仓库里的 `steps/step00_minimal_loop/` 带了一个**录音服务**：它不是真的模型，而是把
2026 年 8 月录下的一次真实对话原样回放。本章后半部分会亲手把它写出来，现在先借来用。
另开一个终端，运行：

```bash
cd steps/step00_minimal_loop
uv sync --all-extras
uv run minicodex serve-stub
```

看到 `stub listening on http://127.0.0.1:11435/v1  (Ctrl-C to stop)` 就说明它在运行了。
**让这个终端一直开着**，用完按 Ctrl-C 停掉。之后凡是要连模型的地方，把地址换成
`http://127.0.0.1:11435/v1`。

> 录音服务只会背几段固定台词，不管你问什么，它都按录音回答。本章的演示正好用的就是
> 录音里的那个问题，所以效果和真模型一样。

### 0.4 开一个分支

第 -1 章说过：从这一章开始，每个功能都在单独的分支上做。回到你自己的项目根目录：

```bash
git switch main                  # 确认在 main 上
git switch -c feat/agent-loop    # 新建分支 feat/agent-loop 并切过去
```

> `git switch -c` 和第 -1 章提到的 `git checkout -b` 效果一样，是较新的写法。

本章的所有提交都在这个分支上，最后在 §11 合并回 `main`。

---

## §1 这一章要做出来的东西

一条命令：

```bash
minicodex ask "What does src/minicodex/__init__.py define?"
```

关键在于：**这个问题，程序不查是答不出来的。** 它必须先打开那个文件。

这就是 Agent 和聊天机器人的区别：**它能要求外界替它做事，拿到结果，再继续。**
之后所有的内容——执行命令、修改文件、权限、上下文压缩——都是在给这个能力加码。

而这一章会撞上一个错得非常安静的地方：**它说它读了文件，其实没读。**

> **关于本章的代码和输出**
>
> 模型的输出都是真实录下来的：本地 Ollama 上的 `gemma4:31b`，录制于 2026-08-06。
> 仓库里的录音服务**逐字回放**这些响应（文字和 id 完全一致，只是 JSON 的空格排版略有
> 不同），所以没有显卡也能跑完全章，结果和书上一样。
>
> 标注"Windows 上实测"的输出是在 Windows 上对着录音服务跑的；其余终端输出来自 Linux。

---

## §2 定需求：把目标翻译成待办

沿用第 -1 章的方法：**把目标里的每个词拆开问"这需要什么"**。

| 目标里的词 | 追问 | 需要做的事 |
|---|---|---|
| 回答 | 谁来组织语言？ | 连上一个**模型**（HTTP 客户端） |
| 必须先读文件 | 程序怎么知道该读哪个？ | 模型得能**要求我们做事**——工具调用 |
| 先……再…… | 一次问答不够 | 一个**循环**：问 → 干活 → 把结果给它 → 再问 |
| `minicodex ask` | 怎么从终端触发？ | 给命令行加一个 `ask` 子命令 |

然后是第 -1 章的两个追问：

> **追问一：我怎么验证它真的成了？** 模型说"我读了"就算读了吗？
> → 不能信它说的，**得能观察到工具真的被调用**。
>
> **追问二：出了问题，我怎么查？** 模型是个黑盒，而对话历史只活在内存里，
> 程序一退出就没了。→ **得能看到每一轮到底发给了模型什么。**
> 这正是第 -1 章挪到这里的那条猜测 F-1-04。

### 一个必须现在做的决定：用 async

§0.1 说过，`async` 会一路传染到最外层。反过来说：如果现在先写同步的，以后想改成异步，
**要改的是从入口到最底层的每一个函数和它们所有的调用点**。

而"这个程序几乎全部时间在等"不是猜测，是已知事实：等模型（秒级）、等文件（毫秒级），
以后还要等命令跑完（可能是分钟级）。所以现在就用 async。

对照一个**不该现在决定**的：多个工具是一个个跑，还是同时跑？现在不知道，
而且以后改只动一个函数。

> **判断标准不是"以后会不会变"，而是"以后改要动多少个地方"。**
> async 要动每一个调用点；工具怎么调度，只动一个函数。

### 什么时候值得加一层抽象

这一章会反复做同一类决定：要不要多定义一个类型、多包一层函数？规则是：
**默认不加**。到目前为止，值得加的只有下面三种情况；以后遇到新的情况，会在那时补上。

1. **以后改的代价极高。** 比如上面的 async。
2. **外部数据进来的地方。** 服务端返回的 JSON 可能缺字段、多字段、改名字。在它进入程序
   的那一刻，把它翻译成自己定义的类型；翻译出错就在这一处出错，不会渗到别处。
   这种地方叫**信任边界**。
3. **今天就有不止一种实现。** 不是"以后可能要换"，而是现在就有两个东西必须能互换。

还有一个比抽象更轻的做法，本章也会用到几次，叫**"留缝"**：不定义新接口，只是把一个
以后很可能要单独用的东西**拿出来放好**，代价通常是一两行。

---

## §3 先看一眼真实的流

动手写正式代码之前，先看看服务端到底会发回什么。这一步不是在写程序，是在**观察**——
就像第 -1 章打开 wheel 看里面有什么一样。

### 3.1 加上第一个运行时依赖

要发 HTTP 请求，用一个第三方库 **httpx**，它同时支持普通调用和 async 调用。
在 `pyproject.toml` 里，把第 -1 章的 `dependencies = []` 改成：

```toml
dependencies = [
    # The first runtime dependency of the project.  Chapter -1 had none; talking
    # to a model over HTTP is what finally justified one.
    "httpx>=0.27",
]
```

然后同步环境：

```bash
uv sync --all-extras
```

> 这是项目的**第一个运行时依赖**，也就是用户装 `minicodex` 时会被一起装上的东西。
> 第 -1 章说过：每加一个依赖都是一次决策。这一个的理由很实在：要和模型说话，就得发 HTTP。

### 3.2 一个临时脚本

在项目根目录建一个 `explore.py`。它是**临时的观察工具，看完就删，不提交**：

```python
# explore.py -- a throwaway script to look at the raw stream
import asyncio

import httpx

URL = "http://localhost:11434/v1/chat/completions"
TOOLS = [{"type": "function", "function": {
    "name": "read_file", "description": "Read a UTF-8 text file",
    "parameters": {"type": "object", "required": ["path"],
                   "properties": {"path": {"type": "string"}}}}}]


async def main():
    payload = {
        "model": "gemma4:31b",
        "messages": [
            {"role": "system", "content":
             "You are a coding agent. Before every tool call, first say one "
             "short sentence about what you are doing."},
            {"role": "user", "content": "What does src/minicodex/__init__.py define?"},
        ],
        "tools": TOOLS,
        "stream": True,
    }
    async with httpx.AsyncClient() as client:
        async with client.stream("POST", URL, json=payload) as resp:
            async for line in resp.aiter_lines():
                if line:
                    print(line)


asyncio.run(main())
```

逐段看：

> - **`TOOLS`**：§0.1 说的工具 schema。它告诉模型：有一个叫 `read_file` 的工具，
>   参数是一个对象，必须有 `path` 字段，类型是字符串。
> - **`payload`**：请求体。`model` 是模型名，`messages` 是历史，`tools` 是可用工具，
>   `"stream": True` 表示要流式返回。system 消息要求模型"调工具之前先说一句在做什么"——
>   真实的编码 Agent 几乎都会这么要求，因为用户想知道它在干什么。
> - **`async with httpx.AsyncClient() as client`**：创建一个 HTTP 客户端，用完自动关闭连接。
> - **`client.stream("POST", URL, json=payload)`**：发 POST 请求，`json=` 会把字典转成 JSON。
>   用 `stream` 而不是普通的 `post`，是为了能边收边读。
> - **`async for line in resp.aiter_lines()`**：响应每到一行就处理一行。空行跳过。
>
> 用 Ollama 本地模型的，把 `"model"` 改成你拉的模型名；用录音服务的，把 `URL` 改成
> `http://127.0.0.1:11435/v1/chat/completions`。

> **为什么用 `/v1/chat/completions` 这个地址？** 这是 **OpenAI 兼容**的接口格式，
> 很多家服务都支持。本地跑 Ollama 不要钱，而同一份代码换个地址就能对着别家跑。
> Ollama 另有一套自己的 `/api/chat` 接口，形状不同；两条路都能通，选兼容格式是因为
> 以后接第二家供应商时，代码改动最小。

跑：

```
$ uv run python explore.py
data: {"id":"chatcmpl-864",...,"choices":[{"index":0,"delta":{"role":"assistant","content":"I"},"finish_reason":null}]}
data: {"id":"chatcmpl-864",...,"choices":[{"index":0,"delta":{"role":"assistant","content":" will read the contents of"},"finish_reason":null}]}
data: {"id":"chatcmpl-864",...,"choices":[{"index":0,"delta":{"role":"assistant","content":" the file"},"finish_reason":null}]}
data: {"id":"chatcmpl-864",...,"choices":[{"index":0,"delta":{"role":"assistant","content":" `"},"finish_reason":null}]}
data: {"id":"chatcmpl-864",...,"choices":[{"index":0,"delta":{"role":"assistant","content":"src/minicodex"},"finish_reason":null}]}
data: {"id":"chatcmpl-864",...,"choices":[{"index":0,"delta":{"role":"assistant","content":"/__init__.py`."},"finish_reason":null}]}
data: {"id":"chatcmpl-864",...,"choices":[{"index":0,"delta":{"role":"assistant","content":"","tool_calls":[{"id":"call_rgpykfbt","index":0,"type":"function","function":{"name":"read_file","arguments":"{\"path\":\"src/minicodex/__init__.py\"}"}}]},"finish_reason":null}]}
data: {"id":"chatcmpl-864",...,"choices":[{"index":0,"delta":{"role":"assistant","content":""},"finish_reason":"tool_calls"}]}
data: [DONE]
```

> 这是对着真实 Ollama 的输出，每行中间几个不重要的字段用 `...` 省略了。录音服务回放的
> 内容相同，只是 JSON 里冒号和逗号后面多了空格。

**它动了。** 而且这九行里有三件事，是坐着想想不出来、只能看出来的。

### 3.3 三个只能看出来的事实

**一、文字不是一个词一个词来的，是任意长度的碎片。**

```
"I"
" will read the contents of"
" the file"
" `"
"src/minicodex"
"/__init__.py`."
```

第一片一个字母，第二片五个词。模型的第二次回复里更夸张：路径
`src/minicodex/prompts/system.md` 被劈成了 `"minicodex/prom"` 和 `"pts/system.md"` 两片。

> **任何一片单独看都可能是半个词，所以在拼完之前不能对文字做任何判断。**

**二、工具调用是一次给完的，不是碎片。**

```json
"function": {"name": "read_file", "arguments": "{\"path\":\"src/minicodex/__init__.py\"}"}
```

`arguments` 是一整个 JSON **字符串**（注意它外面有引号，里面的引号被转义成了 `\"`），
而且是完整的。

这一条决定了我**不用写**什么代码：如果参数是碎片，就得攒起来再拼；既然不是，那套
拼接逻辑就是凭空想出来的代码。

> **这不是普遍规律，是这家服务的行为。** 别家可能真的分片发。等接第二家供应商时再去测，
> 测出来什么样就写什么样。**现在不为没见过的情况写代码。**

**三、有两个结束信号，不是一个。**

```
"finish_reason":"tool_calls"     ← 模型为什么停：它要调工具
data: [DONE]                     ← 这次 HTTP 响应发完了
```

这两个是分开的，而且 `finish_reason` 在一个**内容为空的单独 chunk** 里。

一个流如果没收到 `[DONE]` 就结束了，那它不是"说完了"，而是**被切断了**。

还有一个细节值得记下：`tool_calls` 是一个**列表**，每一项带一个 `index`（这里是 0）。
既然是列表、还编了号，模型**可能一次要不止一个工具**。录音里只有一个，但格式允许多个。

看完了。把 `explore.py` 删掉：

```bash
rm explore.py        # Windows PowerShell 里也可以用 rm
```

---

## §4 猜故障：动工前写下它可能坏在哪

§3 看到的东西，已经足够让我对"它会坏在哪"有一些具体的猜测了。和第 -1 章一样，
每条都要写清楚**怎么判断它成立**：

| 编号 | 我担心的事 | 从哪儿想到的 | 怎么判断它成立 |
|---|---|---|---|
| F-1-04 | 出了问题，不知道模型当时收到了什么 | 第 -1 章留下的；§2 追问二 | 程序退出后，还能不能找到每一轮发给模型的完整历史 |
| F00-03 | 模型一次要多个工具，只处理了一个 | §3.3：`tool_calls` 是个带编号的列表 | 给它一个一次要三个工具的响应，看三个是不是都执行了 |
| F00-04 | 流中途断了，半截回复被当成完整的 | §3.3：有 `[DONE]` 才算真的结束 | 在 `[DONE]` 之前掐断连接，看半截的工具调用会不会被执行 |
| F00-05 | 工具出错（文件不存在、没权限），把整个程序带崩 | 读文件本来就可能失败 | 让工具抛一个异常，看程序还活着没有 |
| F00-06 | 模型调了一个不存在的工具，或者参数不是合法 JSON | 模型的输出本质上是它"写"出来的文字，可能写错 | 构造这样的调用，看程序怎么反应 |
| F00-01 | 循环停不下来；或者到了上限被硬砍，用户什么也没拿到 | 循环的结束完全取决于模型什么时候不再要工具 | 让模型一直要工具，看会发生什么 |

> 编号是 `FAULTS.md` 里的固定 ID：`F00-xx` 表示第一次在第 0 章登记。F-1-04 是第 -1 章
> 登记、留到这里验证的那一条。F00-02、F00-07 等几个编号为什么不在表里，§10 会揭晓。

**这里有一个和第 -1 章相同的做法：第一版先不处理这些情况。** 第 -1 章明知平铺结构可能
有问题，还是先写了平铺结构，然后去撞。这一章也一样：先写一个最直白的版本，让它跑起来，
再回来逐条去撞这张表。

还有一点现在就能看出来：要验证这几条，我得能**控制模型回什么**——让它一次要三个工具、
让流在半路断掉。真模型做不到按需出错。这意味着迟早需要一个"假模型"。但现在还不知道
具体要它做什么，所以先不造。

---

## §5 做出来

### 5.1 把文字取出来——然后撞上第一个意外

打印原始行只能看一眼，接下来要真正用它。最直接的写法是：去掉 `data: ` 前缀，
把剩下的当 JSON 解析，取出 `content`：

```python
async for line in resp.aiter_lines():
    if not line:
        continue
    chunk = json.loads(line.removeprefix("data: "))
    delta = chunk["choices"][0]["delta"]
    print(delta.get("content", ""), end="")
```

> - `line.removeprefix("data: ")`：如果字符串以 `"data: "` 开头，就去掉它。
> - `json.loads(...)`：把 JSON 文本变成 Python 的字典。
> - `chunk["choices"][0]["delta"]`：按 §3.2 输出的结构，一层层取到 `delta`。
> - `print(..., end="")`：打印后不换行，这样碎片会接在一起显示。

跑：

```
I will read the contents of the file `src/minicodex/__init__.py`.
Traceback (most recent call last):
  ...
  File "/usr/lib/python3.10/json/decoder.py", line 355, in raw_decode
    raise JSONDecodeError("Expecting value", s, err.value) from None
json.decoder.JSONDecodeError: Expecting value: line 1 column 2 (char 1)
```

**注意，它先把整句话正确地打了出来，然后才崩溃。**

这种"做对了一部分再崩"比一上来就崩难查得多：你的第一反应是"输出是对的呀，哪儿有问题"。
而且报错里完全没说是哪一行数据出的事。

原因是最后那行 `[DONE]`：**它不是 JSON。** 所以要先判断它，再解析：

```python
if not line.startswith("data: "):
    continue
payload = line[len("data: "):]

# The sentinel is not JSON.  Parsing before checking for it is the first
# thing that breaks, and it breaks *after* printing a perfectly good answer.
if payload == "[DONE]":
    ...
```

这是本章第一个没猜到的问题，§4 的表里没有它。它会自己报错（🔴），所以不需要费力去找。

还有一个小坑：工具调用那个 chunk 里，`content` 是 `""`（空字符串），而不是没有这个字段。
所以判断要写 `if delta.get("content"):`，而不是 `if "content" in delta:`。前者在内容为空
时是假，后者只要字段存在就是真，会往文字里塞一堆空串。

### 5.2 给流里的东西起名字

在把代码整理成模块之前，得先回答一个问题：**这个读取循环应该交出什么？**

现在它什么都不交出，只是 `print`。可接下来的循环需要知道"这一轮模型说了什么、要调什么
工具"。最省事的写法是把服务端的字典直接往外扔：

```python
yield chunk["choices"][0]["delta"]     # 直接把 delta 交出去
```

**不行。** 那个字典长这样：

```json
{"role": "assistant", "content": "", "tool_calls": [
    {"id": "call_x", "index": 0, "type": "function",
     "function": {"name": "read_file", "arguments": "{...}"}}]}
```

拿到它的代码，每次想知道"这片是文字还是工具调用"，都得写
`if d.get("content"): ... elif d.get("tool_calls"): ...`，还得记住 `content` 是空串
不是 None、`name` 埋在 `function` 里面两层。**这些细节会从这个文件漏到每一个用它的地方**，
而且没有任何东西提醒你漏了。

这正是 §2 说的第二种情况——**信任边界**：服务端的 JSON 到此为止，往外交出的是我们自己
定义的类型。

定几个类型？**照着 §3.3 那三个观察来，一个观察一个类型：**

| §3.3 的观察 | 对应的类型 |
|---|---|
| 文字是任意长度的碎片，必须先拼 | `TextDelta` |
| 工具调用是完整的，一次给完 | `ToolCallDelta` |
| 有两个结束信号，`[DONE]` 才是真的结束 | `Completed` |

```python
@dataclass(frozen=True)
class TextDelta:
    """A slice of prose.  Not a token, not a word -- whatever the server sent."""

    text: str


@dataclass(frozen=True)
class ToolCallDelta:
    """One complete tool call.

    Named `Delta` because that is the field it arrives in, not because it is a
    fragment: `arguments` is the entire JSON string.
    """

    call_id: str
    index: int
    name: str
    arguments: str


@dataclass(frozen=True)
class Completed:
    """The `[DONE]` sentinel, carrying the `finish_reason` seen before it."""

    reason: str | None


StreamEvent = TextDelta | ToolCallDelta | Completed
```

**`@dataclass` 是什么。** 写在类上面，Python 会根据下面列出的字段，自动生成
`__init__`（所以能写 `TextDelta("hi")`）、`__eq__`（两个字段相同的对象判为相等）等方法。
适合这种"只装数据、没有复杂行为"的类。

**为什么是三个类，而不是一个带可选字段的类。** 一个类的话会长成
`Event(text=None, call=None, done=False)`，用的人还是得靠判空来区分，而且能造出
`Event(text="hi", done=True)` 这种没有意义的组合。三个类让**不合理的状态根本写不出来**：
拿到一个 `TextDelta`，它就一定有 `text`，不可能同时是结束信号。

**为什么 `frozen=True`。** 冻结后，对象创建出来就不能再改字段。这些对象记录的是
"已经发生的事实"——服务端已经把这片数据发过来了，任何代码都不该有机会改它。
冻结的对象还能直接比较相等，后面的测试会用到。

**为什么 `TextDelta` 只有一个字段还要包一层。** 直接 `yield "I"` 也能跑。但那样用的人
只能靠 `isinstance(event, str)` 判断类型，而字符串太通用了，以后别的东西也可能是字符串。
**包一层的成本是三行，换来的是类型检查器能帮你检查有没有漏掉哪种情况。**

**为什么 `ToolCallDelta` 同时要 `call_id` 和 `index`。** 两个都来自服务端，用途不同：
`call_id`（`call_rgpykfbt`）是**给对方看的**，回传工具结果时要用它说明"这是哪个调用的
结果"；`index`（0、1、2）是**给自己看的**，多个调用时用来排顺序。

**为什么叫 `Delta`，而它其实不是碎片。** 因为服务端那个字段就叫 `delta`
（`choices[0].delta.tool_calls`）。**和协议用同一个词，胜过一个单独看更准确的名字**，
否则读代码的人要在两套词汇之间来回翻译。docstring 里写明了这个别扭之处。

**`StreamEvent = TextDelta | ToolCallDelta | Completed`** 是一个**类型别名**：
"`StreamEvent` 是这三种之一"。它用来标注"读取函数会交出这三种东西"。

### 5.3 收成模块：`model.py`

把 §3 的脚本、§5.1 的修补、§5.2 的类型收进一个模块，放在 `src/minicodex/model.py`。
这是它的全部内容：

```python
"""Talking to a model over HTTP.

Written against Ollama's OpenAI-compatible endpoint, because that is what a
local Ollama exposes at http://localhost:11434/v1 and what most hosted
providers speak too.

The shapes below are not guesses.  They were read off a real response:

    data: {"choices":[{"delta":{"content":"I"},"finish_reason":null}]}
    ...
    data: {"choices":[{"delta":{"content":"","tool_calls":[
             {"id":"call_yfo64477","index":0,"type":"function",
              "function":{"name":"get_temperature",
                          "arguments":"{\"city\":\"New York\"}"}}]},
           "finish_reason":null}]}
    data: {"choices":[{"delta":{"content":""},"finish_reason":"tool_calls"}]}
    data: [DONE]

Two things in there are easy to get wrong and cost nothing to get right once
you have seen them:

* **Prose and tool calls stream at different granularities.**  Prose arrives in
  arbitrary slices -- one recorded response cut the path
  `src/minicodex/prompts/system.md` across two chunks as `"minicodex/prom"` and
  `"pts/system.md"` -- so no slice may be interpreted before joining.  A tool
  call, by contrast, arrives whole in a single chunk, arguments already a
  complete JSON string.  One accumulator does not fit both.
* **There are two terminators.**  `finish_reason` says why the model stopped;
  the `[DONE]` sentinel says the HTTP stream is over.  A stream that ends
  without `[DONE]` did not finish -- it was cut.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass
from typing import Any

import httpx

DEFAULT_BASE_URL = "http://localhost:11434/v1"
DEFAULT_MODEL = "gemma4:31b-cloud"


@dataclass(frozen=True)
class TextDelta:
    """A slice of prose.  Not a token, not a word -- whatever the server sent."""

    text: str


@dataclass(frozen=True)
class ToolCallDelta:
    """One complete tool call.

    Named `Delta` because that is the field it arrives in, not because it is a
    fragment: `arguments` is the entire JSON string.
    """

    call_id: str
    index: int
    name: str
    arguments: str


@dataclass(frozen=True)
class Completed:
    """The `[DONE]` sentinel, carrying the `finish_reason` seen before it."""

    reason: str | None


StreamEvent = TextDelta | ToolCallDelta | Completed


class ModelHTTPError(RuntimeError):
    """The server answered, but not with a stream."""


class OllamaModel:
    def __init__(
        self,
        *,
        base_url: str = DEFAULT_BASE_URL,
        model: str = DEFAULT_MODEL,
        tools: list[dict[str, Any]] | None = None,
        extra_body: dict[str, Any] | None = None,
        timeout: float = 300.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.tools = tools or []
        # Used by tests to pick a stub recording.  A real server ignores keys it
        # does not know, so this costs nothing in production.
        self.extra_body = extra_body or {}
        self.timeout = timeout

    def request_body(self, history: Sequence[dict[str, Any]]) -> dict[str, Any]:
        """The exact JSON that will be posted.

        Separate from `stream()` so it can be printed, recorded, diffed and
        asserted on without making a network call.
        """
        body: dict[str, Any] = {
            "model": self.model,
            "messages": list(history),
            "stream": True,
        }
        if self.tools:
            body["tools"] = self.tools
        body.update(self.extra_body)
        return body

    async def stream(self, history: Sequence[dict[str, Any]]) -> AsyncIterator[StreamEvent]:
        url = f"{self.base_url}/chat/completions"
        finish_reason: str | None = None

        async with httpx.AsyncClient(timeout=self.timeout) as client:
            async with client.stream("POST", url, json=self.request_body(history)) as resp:
                if resp.status_code != 200:
                    detail = (await resp.aread()).decode("utf-8", "replace")[:500]
                    raise ModelHTTPError(f"HTTP {resp.status_code} from {url}: {detail}")

                async for line in resp.aiter_lines():
                    if not line.strip():
                        continue
                    if not line.startswith("data: "):
                        continue
                    payload = line[len("data: ") :]

                    # The sentinel is not JSON.  Parsing before checking for it
                    # is the first thing that breaks, and it breaks *after*
                    # printing a perfectly good answer.
                    if payload == "[DONE]":
                        yield Completed(finish_reason)
                        return

                    chunk = json.loads(payload)
                    choice = chunk["choices"][0]
                    if choice.get("finish_reason"):
                        finish_reason = choice["finish_reason"]

                    delta = choice.get("delta") or {}
                    # `content` is "" on tool-call chunks, so truthiness is the
                    # test, not membership.
                    if delta.get("content"):
                        yield TextDelta(delta["content"])
                    for raw in delta.get("tool_calls") or []:
                        fn = raw.get("function") or {}
                        yield ToolCallDelta(
                            call_id=raw.get("id") or f"call_{raw.get('index', 0)}",
                            index=raw.get("index", 0),
                            name=fn.get("name", ""),
                            arguments=fn.get("arguments", ""),
                        )
```

先看开头：**文件最上面的长 docstring 记录的是 §3 看到的真实数据**，而且说明了哪两件事
容易弄错。它是写给以后读这个文件的人的：这些形状不是猜的，是观察到的。

逐段说：

**`DEFAULT_BASE_URL` 和 `DEFAULT_MODEL`。** 默认连本机的 Ollama，默认模型是
`gemma4:31b-cloud`。§0.3 说过，不用这个模型的话，运行时用 `--model` 换掉。

**`ModelHTTPError`。** 服务端回的不是 200 时，把响应内容截取前 500 个字符放进错误信息里
抛出去。自定义一个异常类，是为了让调用方能专门捕获"模型服务出错"这一种情况。

**`OllamaModel.__init__` 里的 `*`。** 参数列表里单独一个 `*`，表示它后面的参数**必须用
名字传**：只能写 `OllamaModel(base_url="...")`，不能写 `OllamaModel("...")`。参数多了以后，
这样调用时一眼就知道每个值是什么。

**`extra_body`。** 允许在请求体里额外塞几个字段。代码里的注释说了用途：测试要用它来挑选
录音。真服务端会忽略它不认识的字段，所以对正常使用没有影响。这个用法 §7.3 会看到。

**`request_body()` 单独成方法。** 它不发请求，只返回将要发出去的那个字典。现在没人单独
用它，看着像多余的一层。留它的理由很具体：以后讲上下文压缩时，要对比两次请求体看改了
什么；写快照测试时，要拿到请求体但不发网络请求。如果它埋在 `stream()` 里，就只能想办法
拦截网络请求才能看到。**这就是 §2 说的"留缝"：没有定义新接口，只是把以后要单独用的东西
拿出来放好。**

**`stream()` 是一个异步生成器。** 注意它用的是 `yield`，不是 `return [...]`：

> - 函数里有 `yield`，它就不再是"算完返回一个结果"的普通函数，而是**生成器**：
>   每 `yield` 一次交出一个值，调用方用 `for` 一个个取。
> - 再加上 `async def`，就是**异步生成器**，调用方要用 `async for` 取。
> - 好处是**每收到一片就交出一片**，调用方可以边收边处理（比如像打字机一样逐字打印）。
>   返回列表的写法，要等整个响应收完才能交出。

现在的循环其实用不上"边收边处理"，它反正要等全部收完。留着 `yield` 是因为它不比返回
列表更复杂，却保住了一个以后很可能要用的能力——又是"留缝"。

**`stream()` 里的几个细节**：

> - `line.strip()` 为空就跳过：SSE 用空行分隔各条数据。
> - `choice.get("finish_reason")`：它在某个 chunk 里出现，先记在 `finish_reason` 变量里，
>   等收到 `[DONE]` 时，随 `Completed` 一起交出去。
> - `delta = choice.get("delta") or {}`：`.get` 在字段不存在时返回 `None`，`or {}` 把它
>   换成空字典，后面的 `.get` 就不会出错。**来自外部的数据，每一层都可能缺。**
> - `call_id=raw.get("id") or f"call_{raw.get('index', 0)}"`：观察到的响应每次都带 `id`，
>   但 Ollama 原生接口的文档示例里不带。文档和实际对不上，就给个兜底：用 index 编一个。
>   **这不是为将来做准备，而是对一个已知的不确定做防御。**

### 5.4 第一次提交

`model.py` 能从流里取出文字和工具调用了，这是一个能独立成立的小步。提交：

```bash
git status          # 应该看到 pyproject.toml、uv.lock 改了，src/minicodex/model.py 是新文件
git add .
git commit
```

```
feat: stream responses from an OpenAI-compatible endpoint

Written against a recorded response rather than the documentation, because
the documentation and the wire disagreed: the native /api/chat examples
show tool calls without ids, and the wire has them.

Prose and tool calls stream at different granularities. Prose arrives in
arbitrary slices -- one response split the path
src/minicodex/prompts/system.md across two chunks as "minicodex/prom" and
"pts/system.md" -- while a tool call arrives whole, arguments already a
complete JSON string.

There are two terminators. finish_reason says why the model stopped; the
[DONE] sentinel says the HTTP stream is over. [DONE] is not JSON, so parsing
before checking for it fails -- after printing a perfectly good answer.
```

> **"文档和实际返回对不上"这句话很值钱。** 半年后有人照着文档改代码时，这就是拦住他的东西。
>
> 如果 `git status` 里还看到 `explore.py`，说明 §3.3 忘了删。它不该进仓库。

### 5.5 把碎片拼成一整轮：`agent.py` 的第一部分

新建 `src/minicodex/agent.py`。`stream()` 交出的是一串**碎片**：六个 `TextDelta`、一个
`ToolCallDelta`、一个 `Completed`。而循环需要的是**一整轮**：这一轮模型说了什么、
要调哪些工具。中间缺一步"组装"。

先写文件开头和要用到的类型：

```python
"""The loop: ask, run what it asks for, feed the results back, ask again.

Everything lives in this one module on purpose.  The boundaries between stream
assembly, tool dispatch and the loop are guesses right now, and a boundary in
the wrong place is harder to remove than no boundary.  Interlude A pays this
off, once there are enough call sites for the boundaries to be observed rather
than invented.

Async is decided here rather than later: an agent spends nearly all its wall
clock waiting on IO, and converting a synchronous call chain to async in Python
means touching every caller on the path.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

from minicodex.model import Completed, StreamEvent, TextDelta, ToolCallDelta


@dataclass(frozen=True)
class ToolCall:
    call_id: str
    name: str
    arguments: dict[str, Any] | None  # None when the model emitted invalid JSON
    raw_arguments: str


@dataclass(frozen=True)
class ModelTurn:
    text: str
    tool_calls: tuple[ToolCall, ...]
    finish_reason: str | None = None


class Model(Protocol):
    """Anything that can stream a response given a history.

    A Protocol rather than a base class: structural typing, no inheritance, so
    the cost of the abstraction is close to zero.  It earns its place because
    swapping the model is a requirement today -- the tests need a deterministic
    one -- not a guess about tomorrow.
    """

    def stream(self, history: Sequence[dict[str, Any]]) -> AsyncIterator[StreamEvent]: ...


ToolFn = Callable[[dict[str, Any]], Awaitable[str]]


@dataclass
class RunResult:
    final_text: str
    stop_reason: str  # "completed" | "turn_limit"
    turns_used: int
    history: list[dict[str, Any]] = field(default_factory=list)


DEFAULT_MAX_TURNS = 12
```

> 文件开头的 docstring 说明了两个决定：为什么所有东西都放在一个文件里（现在还看不出
> 该在哪里分开，分错了比不分更难改；等第一次重构时再拆），以及为什么现在就用 async。

四个类型，逐个说：

| 类型 | 装的是 | 谁产出它 | 谁用它 |
|---|---|---|---|
| `ToolCall` | **一个**工具调用（能直接用的形式） | 组装函数 | 循环，拿去执行 |
| `ModelTurn` | **一轮**完整回复 | 组装函数 | 循环，拿去决定下一步 |
| `RunResult` | **一整次**运行的结果 | `run()` | 调用方（命令行、测试） |
| `Model` | "能流式回复的东西"应该长什么样 | —— | 循环，用它来和模型说话 |

**`ToolCall` 和 `ToolCallDelta` 有什么区别？** 一个是服务端发来的原样，一个是能直接用的。
`ToolCallDelta.arguments` 是**字符串** `'{"path":"a.py"}'`，而工具函数要的是**字典**
`{"path": "a.py"}`。中间隔着一次 `json.loads`，而这次解析**可能失败**。所以 `ToolCall`
有两个参数字段：

```python
arguments: dict[str, Any] | None   # 解析成功的结果，失败时是 None
raw_arguments: str                 # 模型原样发来的字符串
```

为什么解析失败还要留着原文？因为失败时要告诉模型"你发的这个不是合法 JSON"，而要说清楚，
就得把**它实际发了什么**回显给它看。解析一失败，`arguments` 就是 `None`，原文就没处找了。

**`ModelTurn` 为什么要 `finish_reason`？** 现在没人用它。留着是因为它是 §3.3 看到的一个
**独立信号**（模型为什么停：说完了？要调工具？还是被截断了？），扔掉的话以后想用就得回头
改组装函数。**留一个已经拿到手的字段，成本是一行。**

**`RunResult` 为什么不直接是一个字符串？** 调用方需要知道的不只是答案：`stop_reason`
区分"正常答完"和"撞上轮次上限"，`turns_used` 看用了几轮，`history` 是完整的对话记录，
测试会检查它。`field(default_factory=list)` 的意思是"默认值是一个新的空列表"——数据类里
不能直接写 `= []`，否则所有对象会共用同一个列表。

**为什么 `RunResult` 没有 `frozen=True`，另外几个有？** `ToolCall`、`ModelTurn` 记录的是
已经发生的事实，不该被改；`RunResult` 是交给调用方的结果，冻结它没有好处。

**`tuple[ToolCall, ...]` 为什么是元组，不是列表？** `ModelTurn` 是冻结的，但如果里面装一个
列表，别人照样能 `turn.tool_calls.append(...)` 把内容改掉。**元组让"不可改"一路到底。**
`tuple[ToolCall, ...]` 的意思是"任意多个 `ToolCall` 组成的元组"。

**`Model` 是一个 `Protocol`。** 这是 §2 说的第三种情况：**今天就有不止一种实现。**
用户要连真的模型；而 §4 已经看出来，验证那些猜测需要一个能控制输出的假模型。
两者都必须能交给同一个循环使用。

> `Protocol` 和普通的基类不同：如果写成基类，`OllamaModel` 就得写
> `class OllamaModel(Model):` 显式继承；而 `Protocol` 只描述"有一个
> `stream(history)` 方法"，**任何有这个方法的对象都自动算数**，不需要继承、不需要
> import `Model`，甚至不需要知道它存在。方法体写 `...`（省略号），因为 Protocol
> 只声明形状，不提供实现。

**`ToolFn`** 是工具函数的类型：`Callable[[dict[str, Any]], Awaitable[str]]` 读作
"一个可调用的东西，接收一个字典，返回一个可以 await 的、最终得到字符串的东西"——
也就是一个 `async def tool(args: dict) -> str` 函数。

### 5.6 组装和循环：第一版

在 `agent.py` 末尾加上 `Agent` 类：

```python
class Agent:
    def __init__(
        self,
        model: Model,
        tools: dict[str, ToolFn] | None = None,
        *,
        max_turns: int = DEFAULT_MAX_TURNS,
    ) -> None:
        self.model = model
        self.tools = tools or {}
        self.max_turns = max_turns

    async def _collect(self, stream: AsyncIterator[StreamEvent]) -> ModelTurn:
        text_parts: list[str] = []
        by_index: dict[int, ToolCallDelta] = {}
        completed: Completed | None = None

        async for event in stream:
            if isinstance(event, TextDelta):
                text_parts.append(event.text)
            elif isinstance(event, ToolCallDelta):
                by_index[event.index] = event
            elif isinstance(event, Completed):
                completed = event

        calls = []
        for index in sorted(by_index):
            raw = by_index[index]
            try:
                parsed = json.loads(raw.arguments) if raw.arguments else {}
                arguments = parsed if isinstance(parsed, dict) else None
            except json.JSONDecodeError:
                arguments = None
            calls.append(ToolCall(raw.call_id, raw.name, arguments, raw.arguments))

        return ModelTurn("".join(text_parts), tuple(calls), completed.reason)

    async def run(self, user_message: str) -> RunResult:
        history: list[dict[str, Any]] = [{"role": "user", "content": user_message}]

        for turn_index in range(self.max_turns):
            turn = await self._collect(self.model.stream(history))

            history.append(
                {
                    "role": "assistant",
                    "content": turn.text,
                    "tool_calls": [
                        {
                            "id": c.call_id,
                            "type": "function",
                            "function": {"name": c.name, "arguments": c.raw_arguments},
                        }
                        for c in turn.tool_calls
                    ],
                }
            )

            if turn.text:  # the model said something, so it has answered
                return RunResult(turn.text, "completed", turn_index + 1, history)

            if turn.tool_calls:
                call = turn.tool_calls[0]
                output = await self.tools[call.name](call.arguments)
                history.append(
                    {"role": "tool", "tool_call_id": call.call_id, "content": output}
                )

        return RunResult("", "turn_limit", self.max_turns, history)
```

**为什么写成一个类，而不是一个函数？** 因为每一轮都要用到同一批东西：模型、工具表、
轮次上限。写成函数的话，这几个参数要一路传下去；写成类，在 `__init__` 里存一次就行。
**这不是为了"面向对象"，只是省掉几个反复出现的参数。**

`tools` 是一个字典：键是工具名，值是工具函数。模型说要调 `read_file`，就用
`self.tools["read_file"]` 找到对应的函数。

**`_collect`：把碎片拼成一整轮。** 因为 §3.3 那两条观察，它需要**两个"累加器"**：

> - **`text_parts` 是列表，最后 `"".join()`。** 不写成 `text += event.text`，因为 Python 的
>   字符串不能改，每次 `+=` 都要把已有内容复制一遍。十几片无所谓，几千片就慢了。
>   先收集、最后一次拼接，是标准写法。
> - **`by_index` 是字典，键是服务端给的 `index`。** 为什么不直接 `calls.append(event)`？
>   如果某家服务真的把一个调用分几片发（每片带同一个 index），字典至少是**同一个键被覆盖**，
>   而列表会变成**好几个残缺的调用**。
> - **`sorted(by_index)`**：按 index 排序，而不是按到达的先后。这一行是防御性的，§9 会看到
>   目前没有任何测试能证明它必要。
> - **参数在这里解析，而不是在每个工具里解析。** 解析失败是"模型发了坏 JSON"，属于协议层面
>   的问题。让每个工具各自 `json.loads`，等于把同一段错误处理复制到每个工具里。
> - **`isinstance(parsed, dict)` 这道检查。** `json.loads("[1,2]")` 不会报错，它成功返回一个
>   列表。但工具要的是字典。**合法的 JSON 不等于我要的形状**，所以解析成功后还要再判断一次。
> - **`completed`** 现在只是被赋值，最后取它的 `.reason`。

**`run`：循环本身。**

问模型 → 它说话了就结束 → 它要工具就执行，把结果放回历史 → 再问。**这就是 Agent。**

> - **`history` 从一条 user 消息开始，然后只增不减。** 每一轮往里追加模型说的话
>   （`assistant`）和工具的结果（`tool`）。**下一轮把整个 history 重新发一遍**——
>   §0.1 说过，模型自己不记得任何事，"上下文"就是这个列表。
> - **assistant 那条消息的形状，是照抄请求格式的**（`tool_calls` 里嵌着
>   `function.arguments` 字符串）。不是我设计成这样，而是因为下一轮要把整个 history 原样
>   发回去，它必须长成服务端认识的样子。
> - **`tool` 消息里的 `tool_call_id`**：告诉模型"这是哪个调用的结果"，对应 §5.2 说的
>   `call_id`。
> - **`turn_index + 1`**：`range` 从 0 开始，但"用了几轮"应该从 1 数起。
> - **`max_turns` 有默认值 12，也可以传别的值。** 默认值给正常使用；能调小，是为了测试时
>   不用真跑满 12 轮。

> **`history` 用的是 `list[dict]`，没有定义专门的类型。** 我知道这不理想。但现在只有一处
> 代码往里放东西，还看不出它该长什么样。**这时候定一套类型，定出来的一定是猜的。**
> 等接第二家供应商、有了具体理由时再改。

对照 §4 的猜测表，这一版**有意没处理**的地方已经能看出几处：只取了
`turn.tool_calls[0]`（F00-03）；没检查 `completed` 是不是 `None`（F00-04）；工具出错、
工具名不存在，都会直接抛异常（F00-05、F00-06）；到了上限就返回一个空答案（F00-01）。
§8 会逐条去撞。

还有一处有问题，但我此刻**完全没意识到**。§6 会撞上它。

### 5.7 第一个工具：`tools.py`

新建 `src/minicodex/tools.py`。我写读文件工具时，第一版是最自然的那种：

```python
async def read_file(args):
    return Path(args["path"]).read_text(encoding="utf-8")
```

保存，跑一下 ruff：

```
$ uv run ruff check .
ASYNC240 Async functions should not use pathlib.Path methods, use trio.Path or anyio.path
  --> src/minicodex/tools.py:24:16
```

`Path.read_text()` 是一个**会卡住**的普通函数。按 §0.1 的第四条规则，在 `async def` 里
调用它，卡住的**不是这一个任务，而是整个程序**。现在一次只跑一个工具，完全看不出来；
等以后多个工具同时跑，"同时"就会悄悄变成"排队"，而你会以为是模型变慢了。

> 这兑现了第 -1 章的一个决定：**在还没有任何异步代码时，就打开 ruff 的 `ASYNC` 规则组。**
> 它在这个问题被写出来的那一刻就拦住了它。这是本章编号 F00-09 的那条，属于 ⚪ 类：
> 静态检查工具发现的，最便宜的一种。

改法是把读文件这件事交给另一个线程去做，`await` 它的结果。这是 `tools.py` 的全部内容：

```python
"""The tools the agent may call, and the schema the model is shown."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any


def _read(path: Path) -> str:
    if not path.exists():
        return f"Error: {path} does not exist. Check the path and try again."
    if path.is_dir():
        return f"Error: {path} is a directory, not a file."
    return path.read_text(encoding="utf-8")


async def read_file(args: dict[str, Any]) -> str:
    """Read a UTF-8 text file relative to the current working directory.

    `Path.read_text()` blocks, and a blocking call inside `async def` stops the
    whole event loop rather than just this task.  With one tool at a time nobody
    notices; once chapter 8 runs tools concurrently the concurrency quietly
    turns into a queue.  Ruff's ASYNC240 rejects the direct call, which is why
    those rules are enabled.
    """
    path = args.get("path")
    if not isinstance(path, str):
        return 'Error: read_file needs a "path" argument, a string. Example: {"path": "a.py"}'
    return await asyncio.to_thread(_read, Path(path))


DEFAULT_TOOLS = {"read_file": read_file}

# What the model is shown.  Chapter 3 is about how much this wording matters.
TOOL_SCHEMAS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read a UTF-8 text file and return its contents.",
            "parameters": {
                "type": "object",
                "required": ["path"],
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Path to the file, relative to the working directory.",
                    }
                },
            },
        },
    }
]
```

逐段说：

> - **`asyncio.to_thread(_read, Path(path))`**：在另一个线程里执行 `_read(Path(path))`，
>   当前任务在这里 `await` 等结果。等的时候，事件循环可以去做别的事。
>   `_read` 是个普通函数，名字前的下划线表示"模块内部用"。
> - **`_read` 先检查文件在不在、是不是目录**，不对就返回一句错误说明，而不是抛异常。
>   错误说明里还写了"下一步该怎么做"（`Check the path and try again`）——为什么要这样写，
>   §8.3 会讲。
> - **工具自己也检查参数**（`isinstance(path, str)`），尽管 schema 里已经写了
>   `"required": ["path"]`。因为 schema 只是给模型的**说明**，不是服务端会强制执行的规则，
>   模型完全可能发来 `{"path": 123}`。**任何来自模型的东西，都要当作外部输入对待。**
> - **`DEFAULT_TOOLS`**：工具名到工具函数的字典，直接交给 `Agent`。
> - **`TOOL_SCHEMAS`**：发给模型看的工具说明，就是 §3.2 里 `TOOLS` 的正式版。
>   这段描述文字现在看着无关紧要，但它会显著影响模型出错的频率，以后会专门讲。

### 5.8 从命令行调用：`ask` 子命令

最后改 `src/minicodex/__main__.py`，加一个 `ask` 子命令：

```python
"""Command line entry point."""

from __future__ import annotations

import argparse
import asyncio
import platform
import sys

from minicodex import __version__
from minicodex.agent import Agent
from minicodex.model import DEFAULT_BASE_URL, DEFAULT_MODEL, OllamaModel
from minicodex.tools import DEFAULT_TOOLS, TOOL_SCHEMAS


async def _ask(question: str, *, base_url: str, model: str) -> int:
    llm = OllamaModel(base_url=base_url, model=model, tools=TOOL_SCHEMAS)
    agent = Agent(llm, DEFAULT_TOOLS)

    result = await agent.run(question)

    print(result.final_text or "(no answer)")
    print(f"\n[{model} | {result.stop_reason} after {result.turns_used} turn(s)]")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="minicodex")
    parser.add_argument(
        "--version",
        action="store_true",
        help="print the version and enough environment detail to file a bug report",
    )
    sub = parser.add_subparsers(dest="command")

    ask = sub.add_parser("ask", help="ask a question that requires reading a file")
    ask.add_argument("question")
    ask.add_argument("--base-url", default=DEFAULT_BASE_URL)
    ask.add_argument("--model", default=DEFAULT_MODEL)

    args = parser.parse_args(argv)

    if args.version:
        print(f"minicodex {__version__}")
        print(f"python    {platform.python_version()} ({sys.platform})")
        return 0

    if args.command == "ask":
        return asyncio.run(_ask(args.question, base_url=args.base_url, model=args.model))

    parser.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

和第 -1 章相比，新加的是：

> - **`_ask` 是 `async def`，`main` 是普通函数。** `main` 里用 `asyncio.run(_ask(...))`
>   进入 async 世界（§0.1 规则二）。命令行入口是普通函数、核心逻辑是异步的，这是标准接法。
> - **`add_subparsers(dest="command")`**：让命令行支持子命令，比如 `minicodex ask ...`。
>   用户敲的是哪个子命令，会存在 `args.command` 里。
> - **`ask.add_argument("question")`**：不带 `--` 的是**位置参数**，必填，也就是问题本身。
>   `--base-url`、`--model` 是可选参数，不写就用默认值。
> - **`result.final_text or "(no answer)"`**：答案为空时，打印一句占位文字，而不是什么都不打。

### 5.9 跑起来

对着 Ollama 或录音服务跑（用录音服务的，加上 `--base-url http://127.0.0.1:11435/v1`）：

```
$ uv run minicodex ask "What does src/minicodex/__init__.py define?" --base-url http://127.0.0.1:11435/v1
I will read the contents of the file `src/minicodex/__init__.py`.

[gemma4:31b-cloud | completed after 1 turn(s)]
```

（Windows 上实测，对着录音服务。）

程序正常结束，退出码 0，没有任何报错，还说了一句通顺的话。看起来能用了，提交：

```bash
git status          # 应该看到 __main__.py 改了，agent.py 和 tools.py 是新文件
git add .
git commit
```

```
feat: add the loop, one tool, and the ask command

Ask the model, run the tool it asks for, feed the result back, ask again.
One tool call per turn for now: several at once is on the list of things
to check, not something the one recorded conversation has shown.
```

> **注意：这个提交里有一个 bug，而我此刻还不知道。** 真实开发就是这样，提交的时候认为
> 它是对的。下一节会发现问题，并用一个新提交修掉它——而不是回头改这个提交。

---

## §6 意外：它说它读了，其实没读

§2 的追问一是"我怎么验证它真的成了"。现在回头仔细看 §5.9 的输出：

```
I will read the contents of the file `src/minicodex/__init__.py`.

[gemma4:31b-cloud | completed after 1 turn(s)]
```

"我这就去读 `src/minicodex/__init__.py` 的内容。"——这句话语法正确、语气笃定，
程序报告 `completed`，退出码是 0。**但它没有回答问题。** 它只是宣布了自己要去做什么。

它到底读没读？不能听它说，得看它做了什么。给 `read_file` 临时加一行打印，只看它有没有被调用：

```python
async def read_file(args: dict[str, Any]) -> str:
    path = args.get("path")
    print(f"[read_file called: {path}]")     # 临时加的，用完删掉
    ...
```

再跑一次：

```
$ uv run minicodex ask "What does src/minicodex/__init__.py define?" --base-url http://127.0.0.1:11435/v1
I will read the contents of the file `src/minicodex/__init__.py`.

[gemma4:31b-cloud | completed after 1 turn(s)]
```

（Windows 上实测。）

**没有 `[read_file called: ...]` 这一行。文件从来没被打开过。** 用户拿到的是一个
自信的"非答案"。

### 6.1 原因

问题出在 §5.6 这一行：

```python
if turn.text:  # the model said something, so it has answered
```

而 §3 的录音里，模型**在同一个回复里先说了一句话，再要求调用工具**。这不是巧合：
system 提示里就要求它这么做，而真实的编码 Agent 几乎都会这么要求，因为用户想看见它在干什么。

所以"模型说话了"根本不代表"模型答完了"。它只是在**边做边解说**。

> 顺带一提：`ask` 目前并没有发送那条 system 提示，录音服务回放的是 §3 那次带提示的对话。
> 对着真实模型跑 `ask` 时，它可能先解说、也可能直接调工具，取决于模型自己的习惯。
> 这也是为什么这个 bug 在真实环境里**时有时无**——§7 会专门处理"时有时无"。

### 6.2 修

```python
            if turn.text:
                final_text = turn.text

            # Text is not a stop signal.  Models narrate before acting, and
            # stopping on the narration leaves the work undone while looking
            # exactly like success.
            if not turn.tool_calls:
                return RunResult(final_text, "completed", turn_index + 1, history)
```

同时在 `run()` 开头加一行 `final_text = ""`，并把最后那行改成
`return RunResult(final_text, "turn_limit", self.max_turns, history)`。

**停止信号是"没有工具调用"，而不是"有文字"。** 意思完全反过来了。

> - **`final_text` 单独存一份**：每一轮都可能有文字，而最后一轮**可能没有文字**
>   （模型只调工具不说话）。存下"最近一次非空的文字"，才有东西交给用户。
> - **注释是这时候补上的。** 这一行字面上完全看不出为什么不能写 `if turn.text`。
>   **一行反直觉的代码如果不解释，下一个人会"顺手修好"它。**

再跑一次：

```
$ uv run minicodex ask "What does src/minicodex/__init__.py define?" --base-url http://127.0.0.1:11435/v1
[read_file called: src/minicodex/__init__.py]
`src/minicodex/__init__.py` defines the following:

- **`__version__`**: The current version of the package (`0.0.1`).
- **`system_prompt()`**: A function that reads and returns the agent's system prompt from a file located at `src/minicodex/prompts/system.md`.
- **`__all__`**: An export list containing `__version__` and `system_prompt`.

[gemma4:31b-cloud | completed after 2 turn(s)]
```

（Windows 上实测。）

这次文件真的被读了，用了两轮：第一轮模型要求读文件，第二轮看到内容后回答。
答案也对：这正是第 -1 章写的 `__init__.py`。

把临时加的那行 `print` 删掉，提交：

```bash
git status          # 应该只看到 src/minicodex/agent.py 改了
git add .
git commit
```

```
fix: stop only when the model asks for no more tools

Models narrate before acting. Stopping as soon as the reply has text
returned "I will read the contents of the file" as the answer, with exit
code 0 and the file never opened. The absence of tool calls is the stop
signal; the presence of text is not.
```

### 6.3 这类 bug 的样子

- **不报错。** 没有异常，退出码是 0，状态是 `completed`。
- **结果看起来合理。** 解说本来就是通顺的句子。
- **只有对比"它说的"和"它做的"才能发现。**

它不是自己跳出来的，是**去找**才找到的。找的方法就是那一行 `print`。

> **一条可以带走的规则：永远不要相信模型对自己行为的描述。**
> 要检查能观察到的效果：哪个工具被调用了、传了什么参数、文件是不是真的变了。

这是本章编号 F00-02 的那条，§4 的猜测表里没有它。

---

## §7 意外：同一个问题，偶尔不一样

修完 §6，我想顺着往下查：**`if turn.text` 是凭直觉写的，直觉在这里错了。那我还有哪些
地方是凭直觉写的？** 再加上 §4 那张表还有六条等着验证。

问题来了：**每查一次，就要真调一次模型。**

一次两三秒，云端模型还要花钱。更麻烦的是这个——同一个问题，对着真 Ollama 问五遍：

```
run 1: The sky is blue because shorter blue wavelengths of sunlight are scattered in all directions by the gases and particles in Earth's atmosphere more than other colors.
run 2: The sky appears blue because shorter blue wavelengths of sunlight are scattered in all directions by the gases and particles in Earth's atmosphere more than other colors.
run 3: The sky is blue because shorter blue wavelengths of sunlight are scattered in all directions by the gases and particles in Earth's atmosphere more than other colors.
run 4: The sky is blue because shorter blue wavelengths of sunlight are scattered in all directions by the gases and particles in Earth's atmosphere more than other colors.
run 5: The sky is blue because shorter blue wavelengths of sunlight are scattered in all directions by the gases in Earth's atmosphere more than other colors.
```

五次里有三次逐字相同。第二次把 "is" 换成了 "appears"，第五次少了 "and particles"。

**这比"每次都不一样"更糟。**

如果每次都变，你第一天就知道必须想办法固定它。而"大部分时候一样、偶尔不一样"意味着：
**你改完一个 bug，跑一遍通过了，会以为自己修好了，其实只是这次运气好。**
如果这种差异出现在工具参数上，就是一次没人察觉的行为变化。

这是本章编号 F00-07 的那条，也不在 §4 的表里。解决它分两步：先把发生过的事记下来，
再把记下来的东西原样播回去。

### 7.1 先把发生过的事记下来（验证 F-1-04）

在解决"复现"之前，先回答 §4 表里的第一条：**出了问题，我能知道模型当时收到了什么吗？**

按表里写的判断方法检查：程序退出后，能不能找到每一轮发给模型的完整历史？**找不到。**
终端里只有模型说的话，而 §6 刚证明了那东西不可信；`history` 只活在内存里，进程一退就没了。
**F-1-04 成立。**

最笨的版本只要几行：每发生一件事，就往文件末尾追加一行 JSON。

```python
class Recorder:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def record(self, kind, payload):
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({"kind": kind, "payload": payload}) + "\n")
```

> - `open("a")`：以**追加**模式打开，写入的内容接在文件末尾，不会覆盖原有内容。
> - `mkdir(parents=True, exist_ok=True)`：连同上层目录一起创建；目录已经存在也不报错。

够用了。它后来长出了四样东西，每样都有具体来由：

| 长出来的 | 因为 |
|---|---|
| 写完立刻 `flush()` + `os.fsync()` | 按 Ctrl-C 中断一次跑到一半的会话，再去看文件——最后几条不在里面。**这份记录最有价值的时候，恰恰是程序崩了之后**，丢的正好是最想看的那几条 |
| 逐行读，遇到坏行就停 | 在文件末尾补了半行，模拟进程被强行杀掉，结果整个文件都读不出来了——可前面那些完整的行明明是好的 |
| 按键名隐藏敏感信息 | 打算把一份记录贴出来当例子，贴之前扫了一眼，看见了一个像密钥的值 |
| `except OSError` 吞掉写入错误 | 磁盘满的时候它抛异常，把 Agent 一起带崩了 |

最后一条值得多说一句：**一个能把被调试的程序搞崩的调试工具，比没有还糟。**

长完之后是这样，`src/minicodex/recorder.py` 的全部内容：

```python
"""A written record of everything that crosses the model boundary.

The first question when an agent misbehaves is always "what did the model
actually receive", and it is unanswerable after the process exits: the history
lived in memory and the memory is gone.  So it gets written down as it happens.

Deliberately dumb.  Append-only JSONL, one event per line, and it never raises
into its caller -- a debugging aid that can crash the program it exists to
debug is worse than no debugging aid at all.
"""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import Any

DEFAULT_DIR = Path(".minicodex") / "recordings"

# Recordings get pasted into bug reports.  Values under these keys never reach
# disk.  Key-name based, so a credential hidden inside an innocent-looking
# value still gets through; that limit is documented rather than hidden.
_REDACTED_KEYS = frozenset({"api_key", "authorization", "token", "secret", "password"})

_REDACTED = "<redacted>"


def _redact(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            k: (_REDACTED if k.lower() in _REDACTED_KEYS else _redact(v)) for k, v in value.items()
        }
    if isinstance(value, list):
        return [_redact(v) for v in value]
    return value


class Recorder:
    """Append-only JSONL sink for model-boundary events.

    Not a logger.  Logs are prose for a human watching a terminal; this is a
    machine-readable transcript that chapter 14 replays to turn an intermittent
    failure into a deterministic test.
    """

    def __init__(self, path: Path | None = None, *, enabled: bool = True) -> None:
        self.enabled = enabled
        self._seq = 0
        self._lock = threading.Lock()
        if path is None:
            path = DEFAULT_DIR / f"session-{int(time.time())}.jsonl"
        self.path = Path(path)
        if self.enabled:
            self.path.parent.mkdir(parents=True, exist_ok=True)

    def record(self, kind: str, payload: dict[str, Any]) -> int | None:
        """Append one event.  Returns its sequence number, None if disabled."""
        if not self.enabled:
            return None
        with self._lock:
            self._seq += 1
            seq = self._seq
            event = {"seq": seq, "ts": time.time(), "kind": kind, "payload": _redact(payload)}
            try:
                with self.path.open("a", encoding="utf-8") as fh:
                    fh.write(json.dumps(event, ensure_ascii=False) + "\n")
                    fh.flush()
                    # The transcript is worth having *because* the process
                    # crashed.  Buffered writes lose the last few events, which
                    # are the ones worth reading.
                    os.fsync(fh.fileno())
            except OSError:
                return seq
        return seq

    def read_all(self) -> list[dict[str, Any]]:
        """Read the transcript back, tolerating a truncated final line.

        A process killed mid-write leaves half a JSON object behind.  One
        document per line means the surviving prefix is still readable; chapter
        7 turns that property into the whole recovery mechanism.
        """
        if not self.path.exists():
            return []
        events: list[dict[str, Any]] = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                break
        return events


NULL_RECORDER = Recorder(path=Path(os.devnull), enabled=False)
```

逐段说：

**为什么是 JSONL（每行一个 JSON），而不是一个大 JSON 数组。** 进程被杀掉时，可能正写到
一半。一个 JSON 数组写了一半，整个文件都无法解析；JSONL 写了一半，是**完整的前面若干行
加上最后一行残缺**，前面的都还能读。`read_all()` 里遇到解析失败的行就 `break`，正是利用
这一点。

**`_redact`：隐藏敏感信息。** 它递归地走遍字典和列表，只要键名（不区分大小写）是
`api_key`、`authorization`、`token`、`secret`、`password` 之一，值就换成 `<redacted>`。

> - `frozenset({...})`：不可修改的集合。用集合是因为"判断在不在里面"很快。
> - **它的局限写在了注释里，没有藏着**：只看**键名**。如果密钥藏在一个普通字段的值里，
>   它拦不住。要修的话，得给每家服务的密钥格式写匹配规则，还会误伤正常代码，
>   而这会破坏这个工具"老老实实记录"的本意。**承认、记录、不修，也是一种合理的结局**——
>   前提是写在以后的人会看到的地方。

**`seq` 和 `ts`。** 每条记录带一个递增的序号和一个时间戳（`time.time()` 返回从 1970 年
起的秒数）。第一次在这个文件里找"第 23 轮"时，只能一行行数，加上序号就省事了。

**`ensure_ascii=False`。** `json.dumps` 默认会把中文等非英文字符转成 `中` 这种形式，
关掉之后文件里保存的是原样的文字，打开就能读。

**`os.fsync(fh.fileno())`。** `flush()` 只是把数据从 Python 交给操作系统，操作系统可能
先放在内存里，过一会儿再写到磁盘。`fsync` 要求操作系统**现在就写进磁盘**。代价是慢一点，
换来的是崩溃后记录仍然完整。

**`threading.Lock`。** 严格说，现在用不上它。`with self._lock:` 保证同一时刻只有一个
**线程**在执行"序号加一、写文件"这一段。而本章所有代码都在同一个线程里跑，async 任务
只在 `await` 的地方切换，`record()` 里一个 `await` 都没有，不会被打断。只有以后真的
在多个线程里调用它（比如从 `asyncio.to_thread` 里），这把锁才有意义。
**它是防御性的，代价一行，但目前没有任何情况需要它**——和 §5.6 的 `sorted` 一样，
我把这一点如实写在这里。

**`NULL_RECORDER`。** 一个"什么都不记"的记录器：`enabled=False`，路径是 `os.devnull`
（操作系统提供的"黑洞"文件，写进去的东西直接丢掉）。有了它，`Agent` 里就不用到处写
`if recorder is not None:`，直接调用 `record()` 就行。这也是"留缝"：它现在就在解决一个
具体的可读性问题，而不是为将来做准备。

**`DEFAULT_DIR`**：默认记录在当前目录下的 `.minicodex/recordings/`，每次会话一个文件，
文件名里带时间戳。

然后把它接进 `Agent`。在 `agent.py` 的 import 里加一行：

```python
from minicodex.recorder import NULL_RECORDER, Recorder
```

`__init__` 加一个参数：

```python
    def __init__(
        self,
        model: Model,
        tools: dict[str, ToolFn] | None = None,
        *,
        max_turns: int = DEFAULT_MAX_TURNS,
        recorder: Recorder = NULL_RECORDER,
    ) -> None:
        self.model = model
        self.tools = tools or {}
        self.max_turns = max_turns
        self.recorder = recorder
```

`run()` 里，在问模型的前后各记一笔：

```python
            self.recorder.record("request", {"turn": turn_index, "history": history})
            turn = await self._collect(self.model.stream(history))
            self.recorder.record(
                "response",
                {
                    "turn": turn_index,
                    "text": turn.text,
                    "finish_reason": turn.finish_reason,
                    "tool_calls": [{"id": c.call_id, "name": c.name} for c in turn.tool_calls],
                },
            )
```

`__main__.py` 的 `_ask` 里创建一个记录器交给 `Agent`，并在最后告诉用户记录存在哪：

```python
async def _ask(question: str, *, base_url: str, model: str) -> int:
    recorder = Recorder()
    llm = OllamaModel(base_url=base_url, model=model, tools=TOOL_SCHEMAS)
    agent = Agent(llm, DEFAULT_TOOLS, recorder=recorder)

    result = await agent.run(question)

    print(result.final_text or "(no answer)")
    print(f"\n[{model} | {result.stop_reason} after {result.turns_used} turn(s)]")
    print(f"[transcript: {recorder.path}]")
    return 0
```

（别忘了在 `__main__.py` 顶部加上 `from minicodex.recorder import Recorder`。）

跑一次 `ask`，最后多了一行：

```
[gemma4:31b-cloud | completed after 2 turn(s)]
[transcript: .minicodex\recordings\session-1790357385.jsonl]
```

（Windows 上实测，所以路径用的是反斜杠。）

看看它记了什么。每行取出序号、类型和内容的开头：

```
$ uv run python -c "import json,sys; [print(e['seq'], e['kind'], json.dumps(e['payload'], ensure_ascii=False)[:110]) for e in map(json.loads, open(sys.argv[1], encoding='utf-8'))]" .minicodex/recordings/session-1790357385.jsonl
1 request {"turn": 0, "history": [{"role": "user", "content": "What does src/minicodex/__init__.py define?"}]}
2 response {"turn": 0, "text": "I will read the contents of the file `src/minicodex/__init__.py`.", "finish_reason": "too
3 request {"turn": 1, "history": [{"role": "user", "content": "What does src/minicodex/__init__.py define?"}, {"role": "
4 response {"turn": 1, "text": "`src/minicodex/__init__.py` defines the following:\n\n- **`__version__`**: The current ve
```

（文件名换成你自己的。）

第 3 行就是以后最常盯着看的东西：**第二轮请求里，模型收到的完整历史。**

### 7.2 又一个意外：`git status` 里冒出了什么

准备提交，按第 -1 章的习惯先看一眼：

```
$ git status --short
 M src/minicodex/__main__.py
 M src/minicodex/agent.py
?? .minicodex/
?? src/minicodex/recorder.py
```

（Windows 上实测。`M` 表示改过的文件，`??` 表示 git 还不认识的新文件。）

**`.minicodex/` 在里面。** 它是记录器刚刚写出来的目录，里面是完整的对话：问题、模型的回复，
还有 Agent 读过的**每一个文件的全部内容**。如果这是一个公司项目，那就是源代码。
第 -1 章的 `.gitignore` 只挡了"一看就知道是机密"的东西（`.env`、`*.key`），没想到程序
自己会生成这样一个目录。

**这正是第 -1 章那条习惯的用处：`.gitignore` 挡的是你想得到的东西，`git status` 抓的是
你没想到的。** 如果直接 `git add .`，这些记录就进了仓库。

在 `.gitignore` 末尾加上：

```gitignore
# Recorded sessions.  Every `minicodex ask` writes one, and it contains the
# whole conversation: the prompt, the model's replies, and the contents of
# every file the agent read.  The recorder redacts credentials (chapter -1,
# F-1-04) but it does not redact the source code, and none of it belongs in
# the repository.  Found in chapter 0, when `git status` showed the directory
# right after the recorder first wrote to it.
.minicodex/
```

并把它加进第 -1 章那个检查 `.gitignore` 的测试里。`tests/test_packaging.py` 里那个参数化
列表改成：

```python
@pytest.mark.parametrize(
    "pattern",
    [
        ".env",
        "__pycache__/",
        "*.key",
        ".venv/",
        # Added in chapter 0. The chapter -1 list covered the things that are
        # obviously secret; this one is a directory the agent creates by itself,
        # on every run, full of everything it read. `git status` showed it the
        # first time the recorder wrote there.
        ".minicodex/",
    ],
)
def test_F_1_03_gitignore_covers_the_usual_accidents(repo_root: Path, pattern: str) -> None:
    assert pattern in (repo_root / ".gitignore").read_text(encoding="utf-8")
```

再看一次：

```bash
git status --short      # .minicodex/ 不见了
```

现在有两件不相干的事等着提交：记录器本身，和 `.gitignore` 的修补。**分成两个提交**，
这次用 `git add 文件名` 只挑出需要的文件：

```bash
git add src/minicodex/recorder.py src/minicodex/agent.py src/minicodex/__main__.py
git commit
```

```
feat: record every model request and response to a transcript

The first question when an agent misbehaves is what the model actually
received, and it is unanswerable once the process exits. Append-only JSONL,
fsync'd per event, readable up to a truncated final line, credentials
redacted by key name, and it never raises into its caller.
```

```bash
git add .gitignore tests/test_packaging.py
git commit -m "chore: keep recorded sessions out of git"
```

### 7.3 把记下来的东西播回去

有了记录，"复现"就不再靠运气了：**把那次真实的回复原样播一遍就行。**

做法是写一个小小的 HTTP 服务：它假装自己是 Ollama，但不跑任何模型，只把录下来的回复
一片片发回去。§0.3 里借用的就是它。`src/minicodex/stub_ollama.py` 的全部内容：

```python
"""A stand-in for Ollama's /v1/chat/completions endpoint.

Every chunk below is a verbatim copy of what Ollama actually returned for
`gemma4:31b` on 2026-08-06, ids and all.  It is not a model and does not
pretend to be one -- it replays those exact bytes so that client code, CI and
readers without a GPU all exercise the same wire format.

Run it with `minicodex serve-stub`, or point at a real Ollama instead:

    minicodex ask "..." --base-url http://localhost:11434/v1 --model qwen3
"""

from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any

MODEL = "gemma4:31b"


def _text(chat_id: str, s: str) -> dict[str, Any]:
    return {
        "id": chat_id,
        "object": "chat.completion.chunk",
        "created": 1786021399,
        "model": MODEL,
        "system_fingerprint": "fp_ollama",
        "choices": [
            {"index": 0, "delta": {"role": "assistant", "content": s}, "finish_reason": None}
        ],
    }


def _call(chat_id: str, tid: str, index: int, name: str, arguments: str) -> dict[str, Any]:
    return {
        "id": chat_id,
        "object": "chat.completion.chunk",
        "created": 1786021400,
        "model": MODEL,
        "system_fingerprint": "fp_ollama",
        "choices": [
            {
                "index": 0,
                "delta": {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {
                            "id": tid,
                            "index": index,
                            "type": "function",
                            "function": {"name": name, "arguments": arguments},
                        }
                    ],
                },
                "finish_reason": None,
            }
        ],
    }


def _finish(chat_id: str, reason: str) -> dict[str, Any]:
    return {
        "id": chat_id,
        "object": "chat.completion.chunk",
        "created": 1786021400,
        "model": MODEL,
        "system_fingerprint": "fp_ollama",
        "choices": [
            {"index": 0, "delta": {"role": "assistant", "content": ""}, "finish_reason": reason}
        ],
    }


# Recorded 2026-08-06 against gemma4:31b, asked "What does
# src/minicodex/__init__.py define?" with a system prompt telling it to say one
# sentence before each tool call.  Note the chunk sizes: prose does not arrive
# one token at a time, it arrives in whatever slices the server felt like.
NARRATE_THEN_CALL = [
    *[
        _text("chatcmpl-864", w)
        for w in [
            "I",
            " will read the contents of",
            " the file",
            " `",
            "src/minicodex",
            "/__init__.py`.",
        ]
    ],
    _call(
        "chatcmpl-864",
        "call_rgpykfbt",
        0,
        "read_file",
        '{"path":"src/minicodex/__init__.py"}',
    ),
    _finish("chatcmpl-864", "tool_calls"),
]

# Recorded 2026-08-06: the answer once the file contents were handed back.
# `"minicodex/prom"` and `"pts/system.md"` are two separate chunks -- a path cut
# in half mid-word, which is why nothing may be interpreted before joining.
FINAL_ANSWER = [
    *[
        _text("chatcmpl-975", w)
        for w in [
            "`",
            "src/minicodex",
            "/__init__.py`",
            " defines the following",
            ":\n\n-",
            " **`__version__",
            "`**: The",
            " current",
            " version of the package (`",
            "0.0.1",
            "`).\n- **`",
            "system_prompt()`**:",
            " A function that reads and",
            " returns the agent's",
            " system prompt from a file",
            " located at `src/",
            "minicodex/prom",
            "pts/system.md",
            "`.\n- **`",
            "__all__`**:",
            " An export list containing `",
            "__version__` and",
            " `system_prompt`.",
        ]
    ],
    _finish("chatcmpl-975", "stop"),
]

# Recorded 2026-08-06 from a separate probe -- three things asked for at once,
# to a weather API rather than a file.  Kept because the file question never
# produced parallel calls and this one did: three chunks, three ids, index 0/1/2.
THREE_CALLS = [
    _call("chatcmpl-908", "call_cz5zx7jy", 0, "get_temperature", '{"city":"New York"}'),
    _call("chatcmpl-908", "call_3zvh467n", 1, "get_conditions", '{"city":"New York"}'),
    _call("chatcmpl-908", "call_zfz547ah", 2, "get_temperature", '{"city":"London"}'),
    _finish("chatcmpl-908", "tool_calls"),
]


class _Handler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        messages = body["messages"]

        # `stub_*` keys are not part of the OpenAI schema.  A real server would
        # ignore them; this one uses them so tests can select a recording.
        mode = body.get("stub_mode", "narrate")
        cut = body.get("stub_cut")

        if any(m.get("role") == "tool" for m in messages):
            chunks = FINAL_ANSWER
        elif mode == "three":
            chunks = THREE_CALLS
        else:
            chunks = NARRATE_THEN_CALL

        if cut is not None:
            chunks = chunks[:cut]

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        for chunk in chunks:
            self.wfile.write(b"data: " + json.dumps(chunk).encode() + b"\n\n")
            self.wfile.flush()
        if cut is None:  # a cut stream never gets its sentinel
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()

    def log_message(self, *args: Any) -> None:
        pass


def serve(port: int = 11435) -> None:
    server = HTTPServer(("127.0.0.1", port), _Handler)
    print(f"stub listening on http://127.0.0.1:{port}/v1  (Ctrl-C to stop)")
    server.serve_forever()
```

> "stub"（桩）是测试里的常用词，指一个**只会按剧本回应的替身**。

分几块看：

**三个生成函数 `_text` / `_call` / `_finish`。** 每个都返回一个完整的 chunk 字典，形状和
§3.2 看到的一模一样：`_text` 是一片文字（有 `content`、没有 `tool_calls`），`_call` 是一个
工具调用（`content` 为空、带 `tool_calls`），`_finish` 是带 `finish_reason` 的结束片。
`"created"` 是写死的录制时间，**回放不该每次生成不同的时间**，否则就不是回放了。

**三段录音。**

> - **`NARRATE_THEN_CALL`**：§3 那次真实回复的逐字拷贝，连 `call_rgpykfbt` 这个 id 都是原样的。
>   `*[_text(...) for w in [...]]` 用列表推导式生成六片文字，再用 `*` 展开放进外层列表，
>   后面接上工具调用和结束片。
> - **`FINAL_ANSWER`**：把文件内容交回给模型之后，它给出的最终回答。注意 `"minicodex/prom"`
>   和 `"pts/system.md"` 是两片——这就是 §3.3 说的"路径被劈成两半"。
> - **`THREE_CALLS`**：另一次真实探测的录音，那次问的是天气，模型一次要了三个工具调用
>   （index 0、1、2）。读文件的问题从没让模型一次要多个，所以专门录了这一段。§8.1 会用到。

**`_Handler`：处理每一个请求。**

> - 它继承 Python 标准库的 `BaseHTTPRequestHandler`。收到 POST 请求时，标准库会调用
>   `do_POST` 方法。
> - **`self.rfile.read(int(self.headers["Content-Length"]))`**：读请求体。HTTP 请求会在
>   `Content-Length` 头里说明请求体有多少字节，必须按这个长度读，不能"读到结尾为止"——
>   连接还开着，没有结尾。
> - **选哪段录音**：如果历史里已经有 `role` 为 `"tool"` 的消息，说明工具结果交回来了，
>   该给最终回答（`FINAL_ANSWER`）；否则是第一轮。
> - **`stub_mode` 和 `stub_cut`**：这两个字段不属于 OpenAI 的格式，是给测试用的开关。
>   `stub_mode="three"` 选三个调用的那段录音；`stub_cut=N` 只发前 N 片，**而且不发 `[DONE]`**，
>   用来模拟连接中途断掉。测试通过 `OllamaModel` 的 `extra_body` 把它们塞进请求体
>   （§5.3 提过的那个参数）。
> - **发送**：先 `send_response(200)` 和响应头，然后每片写成 `data: {json}\n\n`。
>   `wfile.write` 要的是**字节**而不是字符串，所以用 `.encode()` 转换，前后用 `b"..."`
>   写字节串。每写一片就 `flush()`，让它立刻发出去。
> - **`log_message` 改成什么都不做**：标准库默认会给每个请求打一行访问日志，测试输出里不需要。

**`serve`**：在 `127.0.0.1` 的指定端口上启动服务器，`serve_forever()` 会一直运行，直到按 Ctrl-C。

> **它回放的是"内容"，不是"字节"。** `json.dumps` 默认在冒号和逗号后面加空格，而 Ollama
> 发的是紧凑格式。对我们的解析代码来说两者等价，但严格讲这不是逐字节的拷贝。

最后在 `__main__.py` 里加一个 `serve-stub` 子命令来启动它。这是 `__main__.py` 的最终版本：

```python
"""Command line entry point."""

from __future__ import annotations

import argparse
import asyncio
import platform
import sys

from minicodex import __version__
from minicodex.agent import Agent
from minicodex.model import DEFAULT_BASE_URL, DEFAULT_MODEL, OllamaModel
from minicodex.recorder import Recorder
from minicodex.tools import DEFAULT_TOOLS, TOOL_SCHEMAS


async def _ask(question: str, *, base_url: str, model: str) -> int:
    recorder = Recorder()
    llm = OllamaModel(base_url=base_url, model=model, tools=TOOL_SCHEMAS)
    agent = Agent(llm, DEFAULT_TOOLS, recorder=recorder)

    result = await agent.run(question)

    print(result.final_text or "(no answer)")
    print(f"\n[{model} | {result.stop_reason} after {result.turns_used} turn(s)]")
    print(f"[transcript: {recorder.path}]")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="minicodex")
    parser.add_argument(
        "--version",
        action="store_true",
        help="print the version and enough environment detail to file a bug report",
    )
    sub = parser.add_subparsers(dest="command")

    ask = sub.add_parser("ask", help="ask a question that requires reading a file")
    ask.add_argument("question")
    ask.add_argument("--base-url", default=DEFAULT_BASE_URL)
    ask.add_argument("--model", default=DEFAULT_MODEL)

    stub = sub.add_parser("serve-stub", help="replay recorded Ollama responses on a local port")
    stub.add_argument("--port", type=int, default=11435)

    args = parser.parse_args(argv)

    if args.version:
        print(f"minicodex {__version__}")
        print(f"python    {platform.python_version()} ({sys.platform})")
        return 0

    if args.command == "ask":
        return asyncio.run(_ask(args.question, base_url=args.base_url, model=args.model))

    if args.command == "serve-stub":
        from minicodex.stub_ollama import serve

        serve(args.port)
        return 0

    parser.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

> 新加的是 `serve-stub` 子命令。注意 `from minicodex.stub_ollama import serve` 写在
> `if` 分支**里面**：只有真的运行 `serve-stub` 时才导入这个模块，平时运行 `ask` 用不上它。

### 7.4 让测试连上录音服务

录音服务是给测试用的。测试要能自动启动它、用完关掉。

**先让 pytest 能跑 async 测试。** pytest 本身不认识 `async def test_...`，需要一个插件
**pytest-asyncio**。在 `pyproject.toml` 的 `dev` 依赖里加一行：

```toml
    "pytest-asyncio>=0.24",
```

在 `[tool.pytest.ini_options]` 里加一行：

```toml
asyncio_mode = "auto"
```

> `asyncio_mode = "auto"` 的意思是：所有 `async def` 的测试函数都自动在事件循环里运行，
> 不用给每个测试单独加标记。

然后 `uv sync --all-extras` 装上插件。

**再把录音服务做成一个 fixture。** `tests/conftest.py` 的全部内容：

```python
from __future__ import annotations

import socket
import threading
from collections.abc import Iterator
from http.server import HTTPServer
from pathlib import Path

import pytest

from minicodex import stub_ollama

REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def repo_root() -> Path:
    return REPO_ROOT


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="session")
def stub_url() -> Iterator[str]:
    """A local server replaying the recorded Ollama responses.

    Real HTTP over a real socket: the client's SSE parsing, status handling and
    connection teardown are all exercised.  Only the model's judgement is fake.
    """
    port = _free_port()
    server = HTTPServer(("127.0.0.1", port), stub_ollama._Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{port}/v1"
    finally:
        server.shutdown()
        server.server_close()
```

`repo_root` 是第 -1 章就有的。新加的两样：

> - **`_free_port()`**：让操作系统随便分配一个空闲端口。`bind(("127.0.0.1", 0))` 里的 `0`
>   就是"随便给我一个"，`getsockname()[1]` 读出实际分到的端口号。这样测试不会和你手动开着的
>   `serve-stub`（端口 11435）撞车。
> - **`stub_url()`**：
>   - 用 `HTTPServer` 和 `stub_ollama._Handler` 启动录音服务；
>   - 放在一个**线程**里跑（`threading.Thread`），因为 `serve_forever()` 会一直运行，
>     放在主线程里测试就没法继续了。`daemon=True` 表示测试结束时这个线程不会拦着程序退出；
>   - `yield` 把地址交给测试。**fixture 里的 `yield`**：之前的代码在测试前执行，之后的代码
>     在测试后执行。这里用 `try/finally` 保证无论测试成功还是失败，都会关掉服务器；
>   - `scope="session"`：整个测试过程只启动一次，所有测试共用，省去反复启动的时间。

**为什么要走真的网络连接，而不是用假对象替换掉 httpx？** 因为我最想测的恰恰是 §5.1 那种坑：
`[DONE]` 不是 JSON、`content` 是空串、每行的 `data: ` 前缀。把 httpx 替换掉，这些就全被
绕过去了，剩下的测试只能证明"我的假数据和我的解析代码互相同意"。

> **假模型不是这一章事先规划的，而是调查问题时造出来的工具。** 先撞见问题（§6、§7），
> 发现查不下去，才去造它。§4 时就看出"迟早需要一个假模型"，但当时不知道它具体要做什么——
> 现在知道了：回放真实录音，外加两个开关。

> **代价也要说清楚：录音会过时。** Ollama 换个版本、模型换一版，返回的格式就可能变了，
> 而你的测试仍然全绿——因为它们在对着一份 2026 年 8 月的"化石"对话。以后讲测试策略时，
> 会讲怎么定期用真模型重新录制来弥补这一点。

### 7.5 第一批测试

新建 `tests/test_agent.py`。先是开头和几个辅助函数：

```python
"""What the loop must guarantee, pinned so it stays true.

Every test runs against the recorded Ollama responses in `stub_ollama`, served
over a real socket.  Test names carry the fault IDs from FAULTS.md.
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Any

import pytest

from minicodex.agent import Agent
from minicodex.model import Completed, OllamaModel, TextDelta, ToolCallDelta
from minicodex.recorder import Recorder
from minicodex.tools import TOOL_SCHEMAS

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def make_tools(log: list[str] | None = None) -> dict[str, Any]:
    """Tools that record what actually ran.

    The log is the point.  Asserting on the model's prose only proves the model
    said something.
    """
    seen = log if log is not None else []

    async def read_file(args: dict[str, Any]) -> str:
        seen.append(f"read_file:{args.get('path')}")
        return '__version__ = "0.0.1"'

    async def get_temperature(args: dict[str, Any]) -> str:
        seen.append(f"get_temperature:{args.get('city')}")
        return "22C"

    async def get_conditions(args: dict[str, Any]) -> str:
        seen.append(f"get_conditions:{args.get('city')}")
        return "Sunny"

    async def explode(args: dict[str, Any]) -> str:
        raise ValueError("disk on fire")

    return {
        "read_file": read_file,
        "get_temperature": get_temperature,
        "get_conditions": get_conditions,
        "explode": explode,
    }


def model(stub_url: str, **extra: Any) -> OllamaModel:
    return OllamaModel(base_url=stub_url, tools=TOOL_SCHEMAS, extra_body=extra)


async def _naive_run(llm: Any, tools: dict[str, Any], max_turns: int = 6) -> str:
    """The loop as first written, kept executable.

    Two bugs live here deliberately: it stops as soon as the model says
    anything, and it runs only the first tool call.  Keeping it runnable is what
    makes those bugs reproducible rather than anecdotal.  It demonstrates
    exactly two bugs and does not grow.
    """
    messages: list[dict[str, Any]] = [{"role": "user", "content": "go"}]
    for _ in range(max_turns):
        text_parts: list[str] = []
        calls: list[ToolCallDelta] = []
        async for event in llm.stream(messages):
            if isinstance(event, TextDelta):
                text_parts.append(event.text)
            elif isinstance(event, ToolCallDelta):
                calls.append(event)
        text = "".join(text_parts)
        messages.append({"role": "assistant", "content": text})

        if text:  # bug: narration mistaken for an answer
            return text
        if calls:
            call = calls[0]  # bug: the rest are dropped
            out = await tools[call.name](json.loads(call.arguments))
            messages.append({"role": "tool", "tool_call_id": call.call_id, "content": out})
    return ""
```

> - **`make_tools(log)`**：造一套**会记账的假工具**。每个工具被调用时，往 `log` 列表里记一笔
>   "谁被调了、参数是什么"，然后返回一个固定结果。`explode` 专门用来抛异常。
>   docstring 说出了重点：**`log` 才是关键。** 只检查模型说了什么，只能证明模型说了些什么——
>   这就是 §6 的教训写成了代码。
> - `log if log is not None else []`：没传 `log` 就用一个新的空列表。
> - **`model(stub_url, **extra)`**：造一个连向录音服务的 `OllamaModel`。`**extra` 收集所有
>   额外的关键字参数（比如 `stub_mode="three"`），通过 `extra_body` 塞进请求体。
> - **`_naive_run`**：**§5.6 第一版循环的精简复制品**，故意保留了两个 bug——一有文字就停、
>   只执行第一个调用。docstring 说明了为什么要留着它：让这两个 bug 可以随时运行、亲眼看到，
>   而不是只存在于口头描述里。并且约定"它只演示这两个 bug，不许再长"。

然后是测试。**每个测试的名字里带着它钉住的故障编号**，这样在 `FAULTS.md`、测试、正文之间
能互相找到。在文件末尾依次追加：

**F00-02：§6 那个 bug，以及修复。**

```python
async def test_F00_02_naive_loop_answers_without_doing_the_work(stub_url: str) -> None:
    """The model narrates, the loop treats that as the answer, the tool never
    runs, and the run looks like a success."""
    log: list[str] = []

    answer = await _naive_run(model(stub_url), make_tools(log))

    assert answer == "I will read the contents of the file `src/minicodex/__init__.py`."
    assert log == [], "the file was never opened"


async def test_F00_02_absence_of_tool_calls_is_the_stop_signal(stub_url: str) -> None:
    log: list[str] = []
    agent = Agent(model(stub_url), make_tools(log))

    result = await agent.run("What does src/minicodex/__init__.py define?")

    assert result.final_text.startswith("`src/minicodex/__init__.py` defines the following:")
    assert "system_prompt()" in result.final_text
    assert result.stop_reason == "completed"
    assert result.turns_used == 2
    assert log == ["read_file:src/minicodex/__init__.py"]
```

> 第一个测试证明 bug 存在：第一版循环返回了那句解说，而 `log` 是空的。
> 第二个测试证明修复有效：用真正的 `Agent`，最终答案对、用了两轮、`read_file` 被调用了一次。
> **两个测试都检查 `log`**——检查实际发生了什么，而不是模型说了什么。

**F00-07：回放让每次运行都一样。**

```python
async def test_F00_07_the_recorded_stream_gives_the_same_run_every_time(stub_url: str) -> None:
    """A real model mostly repeats itself and occasionally does not, which is
    worse than always differing: a fix looks confirmed when it was only lucky.
    Replaying a recording removes the luck."""
    first = await Agent(model(stub_url), make_tools()).run("go")
    second = await Agent(model(stub_url), make_tools()).run("go")

    assert first.history == second.history
```

**§3.3 的观察：** 把"文字是碎片、工具调用是完整的、两个结束信号"这几条观察钉成测试，
再加上 §5.1 那次崩溃：

```python
async def test_F00_04_the_done_sentinel_is_not_json(stub_url: str) -> None:
    """Parsing every line before checking for the sentinel is the first thing
    that breaks -- and it breaks after printing a perfectly good answer."""
    with pytest.raises(json.JSONDecodeError):
        json.loads("[DONE]".removeprefix("data: "))


async def test_prose_is_joined_and_tool_calls_are_not(stub_url: str) -> None:
    events = [e async for e in model(stub_url).stream([{"role": "user", "content": "go"}])]

    texts = [e for e in events if isinstance(e, TextDelta)]
    calls = [e for e in events if isinstance(e, ToolCallDelta)]
    done = [e for e in events if isinstance(e, Completed)]

    assert len(texts) == 6, "prose arrives in arbitrary slices"
    assert "".join(t.text for t in texts) == (
        "I will read the contents of the file `src/minicodex/__init__.py`."
    )
    assert len(calls) == 1, "a tool call arrives whole, in a single chunk"
    assert calls[0].arguments == '{"path":"src/minicodex/__init__.py"}', (
        "arguments are one complete JSON string, not fragments"
    )
    assert done == [Completed("tool_calls")]


async def test_tool_calls_are_ordered_by_index_not_arrival(stub_url: str) -> None:
    agent = Agent(model(stub_url, stub_mode="three"), make_tools())
    turn = await agent._collect(model(stub_url, stub_mode="three").stream([]))

    assert [c.name for c in turn.tool_calls] == [
        "get_temperature",
        "get_conditions",
        "get_temperature",
    ]
    assert turn.finish_reason == "tool_calls"
```

> - `pytest.raises(json.JSONDecodeError)`：断言下面的代码**会**抛出这个异常，不抛就算失败。
> - `[e async for e in ...]`：**异步列表推导式**，把异步生成器交出的所有值收集成列表。
> - `assert ..., "说明"`：失败时打印后面的说明，告诉看到红色的人错在哪。

**F-1-04：记录器的四个承诺。**

```python
async def test_F_1_04_transcript_captures_what_the_model_was_sent(
    stub_url: str, tmp_path: Path
) -> None:
    recorder = Recorder(tmp_path / "run.jsonl")
    agent = Agent(model(stub_url), make_tools(), recorder=recorder)

    await agent.run("go")

    events = recorder.read_all()
    assert [e["kind"] for e in events] == ["request", "response", "request", "response"]
    assert events[0]["payload"]["history"][0] == {"role": "user", "content": "go"}


def test_F_1_04_transcript_survives_a_truncated_final_line(tmp_path: Path) -> None:
    path = tmp_path / "run.jsonl"
    recorder = Recorder(path)
    recorder.record("request", {"n": 1})
    with path.open("a", encoding="utf-8") as fh:
        fh.write('{"seq": 2, "kind": "resp')  # process killed here

    assert len(recorder.read_all()) == 1


def test_F_1_04_transcript_redacts_credentials(tmp_path: Path) -> None:
    recorder = Recorder(tmp_path / "run.jsonl")
    recorder.record("request", {"headers": [{"Authorization": "Bearer leak-me"}]})

    assert "leak-me" not in (tmp_path / "run.jsonl").read_text(encoding="utf-8")


def test_F_1_04_transcript_never_raises_into_its_caller(tmp_path: Path) -> None:
    recorder = Recorder(tmp_path / "run.jsonl")
    recorder.path = tmp_path / "no-such-dir" / "run.jsonl"
    assert recorder.record("request", {"n": 1}) == 1  # no exception
```

> 四个测试对应 §7.1 表里长出来的四样东西：记下了发送的内容、末尾坏一行还能读、隐藏密钥、
> 写入失败不抛异常。最后一个把路径改到一个不存在的目录，让写入必然失败，
> 然后断言 `record()` 正常返回了序号。

**§5.3 的"留缝"：请求体能单独拿到。**

```python
def test_request_body_can_be_inspected_without_a_network_call() -> None:
    """Chapter 6 diffs this; chapter 13 snapshots it.  Both need it separable."""
    body = OllamaModel(model="m", tools=TOOL_SCHEMAS).request_body(
        [{"role": "user", "content": "hi"}]
    )
    assert body["stream"] is True
    assert body["messages"] == [{"role": "user", "content": "hi"}]
    assert body["tools"][0]["function"]["name"] == "read_file"
```

**F00-09：§5.7 那次被 ruff 拦下的阻塞调用。** ruff 在写代码时拦住了它，这个测试再证明
一次"它为什么值得拦"：

```python
async def test_F00_09_blocking_io_in_a_tool_serialises_the_loop() -> None:
    async def blocking() -> None:
        time.sleep(0.05)  # noqa: ASYNC251 -- reproducing the fault on purpose

    async def cooperative() -> None:
        await asyncio.to_thread(time.sleep, 0.05)

    start = time.perf_counter()
    await asyncio.gather(blocking(), blocking())
    blocking_elapsed = time.perf_counter() - start

    start = time.perf_counter()
    await asyncio.gather(cooperative(), cooperative())
    cooperative_elapsed = time.perf_counter() - start

    assert blocking_elapsed > 0.09, "expected the blocking version to serialise"
    assert cooperative_elapsed < 0.09, "expected the offloaded version to overlap"
```

> - 两个 `blocking()` 用 `time.sleep` 各睡 0.05 秒，`asyncio.gather` 让它们"同时"跑。
>   因为 `time.sleep` 会卡住整个事件循环，实际是一个接一个，总共超过 0.09 秒。
> - 两个 `cooperative()` 把睡眠交给另一个线程，它们真的能同时进行，总共不到 0.09 秒。
> - `# noqa: ASYNC251` 告诉 ruff："这里是故意的，别报"。
> - `time.perf_counter()` 是专门用来测量时间间隔的高精度计时器。

跑测试，应该全部通过。提交：

```bash
git status          # 应该看到 pyproject.toml、uv.lock、__main__.py、tests/conftest.py 改了，
                    # src/minicodex/stub_ollama.py 和 tests/test_agent.py 是新文件
git add .
git commit
```

```
test: replay recorded Ollama responses instead of inventing them

Hand-written fixtures drift away from the wire silently. Recorded ones are
wrong only in ways the recording was wrong, which is a much smaller surface.
The stub is served over a real socket, so SSE parsing and the [DONE] check
are exercised for real; only the model's judgement is fake.
```

### 7.6 一个很容易犯的错：忘了 `await`

写异步测试时，有一个很容易犯、而且犯了也看不出来的错。

§0.1 规则一说过：调用 `async def` 函数，不 `await` 就不会执行。设想一个检查"某件事不该
发生"的测试：

```python
    agent.run("go")               # 少了 await
    assert log == [], "nothing should have run"
```

**这个测试会通过。** `agent.run("go")` 只是造出一个协程对象，然后丢掉，函数体一行都没跑，
`log` 当然是空的。唯一的信号是 Python 在清理这个对象时发出的一句警告：
`RuntimeWarning: coroutine 'Agent.run' was never awaited`。而在一大片通过的测试输出里，
这句警告等于不存在。

先用一个测试证明这件事：

```python
def test_F00_08_a_forgotten_await_runs_nothing_and_says_nothing() -> None:
    async def work() -> int:
        raise AssertionError("this body must never run")

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        coro = work()  # the missing await
        del coro
        gc.collect()

    assert any(issubclass(w.category, RuntimeWarning) for w in caught)
    assert any("never awaited" in str(w.message) for w in caught)
```

> - `work()` 的函数体是 `raise AssertionError`，而这个测试是通过的——**因为函数体根本没跑。**
> - `warnings.catch_warnings(record=True)` 把期间发出的警告收集到 `caught` 列表里；
>   `simplefilter("always")` 让同样的警告每次都发出。
> - `del coro` 删掉协程对象，`gc.collect()` 让垃圾回收立刻运行，警告就在这时发出。
> - 在文件开头的 import 里补上 `import gc` 和 `import warnings`，按字母顺序放在 `asyncio`、`json`、`time` 之间（ruff 会检查 import 的顺序）。

然后是修法：**把这种警告变成错误。** 在 `pyproject.toml` 的 `[tool.pytest.ini_options]`
里加上：

```toml
# A forgotten `await` raises nothing and returns nothing: the coroutine is
# built, dropped, and its body never runs.  The only signal is a RuntimeWarning
# at garbage-collection time, invisible in a green run.  Promote it.
filterwarnings = ["error::RuntimeWarning"]
```

现在，任何测试里出现 `RuntimeWarning`，这个测试都会直接失败。再用一个测试钉住这条配置，
免得哪天被人删掉：

```python
def test_F00_08_pytest_promotes_that_warning_to_a_failure(repo_root: Path) -> None:
    try:
        import tomllib
    except ModuleNotFoundError:  # Python 3.10
        import tomli as tomllib

    with (repo_root / "pyproject.toml").open("rb") as fh:
        cfg = tomllib.load(fh)

    assert "error::RuntimeWarning" in cfg["tool"]["pytest"]["ini_options"]["filterwarnings"]
```

> 这是本章编号 F00-08 的那条，属于 ⚪ 类：靠工具（这里是 Python 的警告机制加 pytest 配置）
> 发现，而不是靠运行结果。

```bash
git add .
git commit -m "chore: fail the test suite on RuntimeWarning"
```

---

## §8 逐条验证 §4 的猜测

有了录音服务和测试，现在可以系统地回到 §4 那张表了。F-1-04 已经在 §7.1 验证过，
还剩五条。每一条都按同一个顺序来：**先让第一版撞上它，再修，再用测试钉住。**

### 8.1 F00-03：模型一次要多个工具

§4 写的判断方法：给它一个一次要三个工具的响应，看三个是不是都执行了。
录音服务的 `stub_mode="three"` 就是这样一个响应：

```
index 0  call_cz5zx7jy  get_temperature(New York)
index 1  call_3zvh467n  get_conditions(New York)
index 2  call_zfz547ah  get_temperature(London)
```

三个独立的 chunk，三个不同的 id。而第一版循环写的是 `turn.tool_calls[0]`。用 `_naive_run`
跑一下：

```python
async def test_F00_03_naive_loop_drops_all_but_the_first_call(stub_url: str) -> None:
    log: list[str] = []

    await _naive_run(model(stub_url, stub_mode="three"), make_tools(log))

    assert log == ["get_temperature:New York"], "two calls were dropped without a word"
```

测试通过——**意味着 bug 确实存在**：三个调用只执行了第一个，另外两个被悄悄丢掉了。
**F00-03 成立。**

丢掉两个调用已经够糟。但**真正的伤害在下一轮**：模型发出了三个调用，历史里却只有一个结果。
另外两个成了**没人回应的调用**。那下一次请求会怎样？

对着我们现在用的 Ollama，**什么都不会发生——它照样返回 200：**

```
$ 故意发一个"有 tool_calls、但没有对应结果"的历史给 Ollama
HTTP 200
data: {..."delta":{"content":" symbiotic"}...}
data: {..."delta":{"content":" with"}...}
data: {..."delta":{"content":" the"}...}
data: {..."delta":{"content":" user"}...}
data: {..."delta":{"content":"'"}...}
data: {..."delta":{"content":"s"}...}
data: {..."delta":{"content":" request"}...}
```

"symbiotic with the user's request"——**历史坏了，服务端不管，模型自己开始胡言乱语。**

而同一个历史发给 OpenAI：

```
HTTP 400
{
  "error": {
    "message": "An assistant message with 'tool_calls' must be followed by tool messages responding to each 'tool_call_id'. The following tool_call_ids did not have response messages: call_probe01",
    "type": "invalid_request_error",
    "param": "messages",
    "code": null
  }
}
```

**同一个 bug，两种结局。** 宽松的服务让你一路绿灯地开发，严格的那个在上线时给你一屏 400。
而且**症状和病因隔了一轮**：你在第 N+1 轮看到 400，bug 却在第 N 轮。这是调试 Agent 时
最费时间的一类问题。

> **一个坦白。** 这段 OpenAI 报错，最初我是**凭记忆写的**，只写了前半句，而且从没验证过——
> 这违反了本书自己的规矩：外部 API 的行为必须实测。后来补测的结果是：前半句一字不差，
> 但我**漏掉了最有用的后半句**：`The following tool_call_ids did not have response
> messages: call_probe01`，它直接告诉你是哪个 id 出了事。
>
> 前半句蒙对了是运气，不是本事。留着这条坦白，是因为"我记得 API 是这么说的"是每个人都会
> 犯的错，而它恰好是最不该犯的那一类。

修法很直接：**每个调用都执行，每个都产出一个结果。** 把 `run()` 里那段改成：

```python
            # Every call runs and every call is answered.  Skipping one leaves
            # it unanswered in history, and the *next* request is what fails.
            for call in turn.tool_calls:
                output = await self.tools[call.name](call.arguments)
                history.append(
                    {"role": "tool", "tool_call_id": call.call_id, "content": output}
                )
```

然后把规则本身钉住：

```python
async def test_F00_03_every_call_runs_in_the_order_the_model_gave(stub_url: str) -> None:
    log: list[str] = []
    agent = Agent(model(stub_url, stub_mode="three"), make_tools(log))

    await agent.run("temperature and conditions for New York, temperature for London")

    assert log == [
        "get_temperature:New York",
        "get_conditions:New York",
        "get_temperature:London",
    ]


async def test_F00_03_outputs_pair_one_to_one_with_calls(stub_url: str) -> None:
    """The invariant chapter 1 turns into a type, asserted by hand for now."""
    agent = Agent(model(stub_url, stub_mode="three"), make_tools())

    result = await agent.run("go")

    issued = [c["id"] for i in result.history if i["role"] == "assistant" for c in i["tool_calls"]]
    answered = [i["tool_call_id"] for i in result.history if i["role"] == "tool"]
    assert issued == answered
```

> 第二个测试检查的是一条**不变量**（任何时候都必须成立的规则）：历史里每个发出的调用 id，
> 都恰好有一个对应的结果，而且顺序一致。
>
> - `issued` 用了**两层的列表推导式**：先遍历历史里的每条消息 `i`，只取 `assistant` 的，
>   再遍历它的每个 `tool_calls` 取出 `id`。
> - `answered` 取出所有 `tool` 消息的 `tool_call_id`。
>
> docstring 说"下一章会把它变成一个类型"：现在这条规则靠一个手写的检查来维持，而手写检查
> 不能扩展——每多一处修改历史的代码，就多一个忘记维护它的机会。接第二家供应商时会处理。

```bash
git add .
git commit -m "fix: run every tool call the model asks for"
```

> **关于"怎么暴露的"。** 这条故障在 `FAULTS.md` 里标的是 🔴（崩溃），因为对着严格的服务，
> 它会以 400 的形式炸出来。但对着本章用的 Ollama，它是**静默的**——返回 200，然后模型胡言
> 乱语。同一个 bug 暴露成什么样子，取决于你对面是谁。

### 8.2 F00-04：流在中途断了

§4 写的判断方法：在 `[DONE]` 之前掐断连接，看半截的工具调用会不会被执行。
录音服务的 `stub_cut=7` 只发前七片——六片文字加一个工具调用——然后不发 `[DONE]` 就断开。

第一版 `_collect` 会怎样？它把六片文字和一个工具调用都收下了，`completed` 仍然是 `None`，
最后一行 `completed.reason` 抛出 `AttributeError: 'NoneType' object has no attribute 'reason'`。
程序崩了，但崩得莫名其妙，看不出是"流被切断了"。**F00-04 成立。**

而更危险的写法，是"好心"地把 `completed.reason` 改成
`completed.reason if completed else None`：这样不崩了，**半截的回复会被当成完整的，
那个工具调用会被执行**——而模型可能还没说完、还想调别的工具。如果这个工具是修改文件
而不是读文件呢？

修法是：**没收到 `[DONE]`，就拒绝交出任何东西。** 在 `agent.py` 顶部定义一个专门的异常：

```python
class IncompleteStreamError(RuntimeError):
    """The connection ended before the server sent its `[DONE]` sentinel."""
```

`_collect` 在拼装之前先检查。这是它的最终版本：

```python
    async def _collect(self, stream: AsyncIterator[StreamEvent]) -> ModelTurn:
        """Turn a stream of events into one finished turn, or refuse to.

        Two accumulators because the wire has two granularities: prose arrives
        one token at a time and gets joined, tool calls arrive whole and get
        placed by `index`.  Sorting by index rather than trusting arrival order
        costs one line and removes a question nobody wants to answer later.

        Nothing is returned until `Completed` arrives.  A stream that dies
        halfway therefore leaves nothing behind -- there is no half-built turn
        that could reach the history by accident.
        """
        text_parts: list[str] = []
        by_index: dict[int, ToolCallDelta] = {}
        completed: Completed | None = None

        async for event in stream:
            if isinstance(event, TextDelta):
                text_parts.append(event.text)
            elif isinstance(event, ToolCallDelta):
                by_index[event.index] = event
            elif isinstance(event, Completed):
                completed = event

        if completed is None:
            raise IncompleteStreamError(
                f"stream ended after {len(text_parts)} text chunk(s) and "
                f"{len(by_index)} tool call(s) without a [DONE] sentinel"
            )

        calls = []
        for index in sorted(by_index):
            raw = by_index[index]
            try:
                parsed = json.loads(raw.arguments) if raw.arguments else {}
                arguments = parsed if isinstance(parsed, dict) else None
            except json.JSONDecodeError:
                arguments = None
            calls.append(ToolCall(raw.call_id, raw.name, arguments, raw.arguments))

        return ModelTurn("".join(text_parts), tuple(calls), completed.reason)
```

关键不在那个 `raise`，而在于**所有碎片都攒在函数的局部变量里**。抛出异常时，局部变量随着
函数结束一起消失——**历史一个字节都没被碰过**。这不是"多加了一个检查"，而是**结构上的保证**：
半成品根本没有路径能到达历史。docstring 最后一段说的就是这件事。

钉住它，并在测试文件的 import 里补上 `IncompleteStreamError`：

```python
from minicodex.agent import Agent, IncompleteStreamError
```

```python
async def test_F00_04_stream_without_the_done_sentinel_is_refused(stub_url: str) -> None:
    """`stub_cut` truncates the recording and withholds `[DONE]`."""
    agent = Agent(model(stub_url, stub_cut=7), make_tools())

    with pytest.raises(IncompleteStreamError) as excinfo:
        await agent.run("go")

    assert "without a [DONE] sentinel" in str(excinfo.value)


async def test_F00_04_nothing_from_a_cut_stream_is_executed(stub_url: str) -> None:
    """The dangerous version does not crash.  It commits a partial turn and
    fails on the next request, somewhere else entirely."""
    log: list[str] = []
    agent = Agent(model(stub_url, stub_cut=7), make_tools(log))

    with pytest.raises(IncompleteStreamError):
        await agent.run("go")

    assert log == [], "a call from an unfinished stream must never run"
```

> - `with pytest.raises(IncompleteStreamError) as excinfo:`：断言会抛出这个异常，
>   并把异常信息存进 `excinfo`，之后可以检查 `str(excinfo.value)` 的内容。
> - 第二个测试的 docstring 点出了危险所在：危险的版本**不会崩**，它会提交半个回合，
>   然后在下一次请求时、在完全不相干的地方出错。

```bash
git add .
git commit -m "fix: refuse a stream that ends without [DONE]"
```

### 8.3 F00-05、F00-06：工具出错，或者根本没这个工具

§4 写的判断方法：让工具抛一个异常；构造一个工具名不存在、或参数不是合法 JSON 的调用。

第一版里，这三种情况都会直接崩：

- 工具名不存在：`self.tools[call.name]` 抛出 `KeyError`。
- 参数不是合法 JSON：`call.arguments` 是 `None`，工具拿到 `None` 去 `.get(...)`，
  抛出 `AttributeError`。
- 工具内部出错（磁盘满、没权限）：异常一路冒到 `run()` 外面。

**三种情况都让整个会话结束。** F00-05、F00-06 成立。

模型会调 `read_fileZ`、`readFile`、`read_files` 这种名字：训练数据里见过别的工具、
描述写得不清楚、对话太长记混了——原因很多。而站在模型的角度想，事情就很清楚：
**工具失败是一条信息，不是一场灾难。** 模型完全有能力读到"这个文件不存在"，然后换个路径
再试；它没有能力处理"你的进程已经退出了"。

一条规则解决全部三种情况。把循环里那句 `await self.tools[call.name](call.arguments)`
抽成一个方法：

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

        try:
            return await self.tools[call.name](call.arguments)
        except Exception as exc:  # deliberately broad; see the docstring
            return f"Error: {call.name} raised {type(exc).__name__}: {exc}"
```

循环里对应改成：

```python
            for call in turn.tool_calls:
                output = await self._run_tool(call)
```

三种错误信息长这样：

```
Error: no tool named 'read_fileZ'. Available tools: read_file. Call one of those instead.
Error: arguments for 'read_file' were not valid JSON. Received: '{not json'. Send a single JSON object.
Error: read_file raised PermissionError: [Errno 13] Permission denied: 'x'
```

前两条都分三段：**出了什么错 · 你能用什么 · 下一步做什么。** 第三条只有一段，因为异常是
什么类型事先不知道，编不出"下一步"。

> - **`raw_arguments` 在这里派上了用场**（§5.5 定义 `ToolCall` 时留的那个字段）。要告诉模型
>   "你发的不是合法 JSON"，就得把它发的东西回显出来，而解析一失败，`arguments` 就只剩 `None`。
>   `[:200]` 只取前 200 个字符，免得太长；`!r` 让它带着引号显示，看得清空格和特殊字符。
> - **`except Exception` 通常是坏习惯**，因为它会把所有错误都吞掉。这里是故意的，注释写明了
>   理由：工具里发生的任何事情，都应该变成交给模型的信息。
> - `", ".join(sorted(self.tools))`：把所有工具名按字母排序、用逗号连起来。`or "(none)"`
>   处理一个工具都没有的情况。

> **一条贯穿全书的原则：工具的错误信息就是给模型的提示（prompt）。** 只说"什么坏了"的
> 错误，模型往往原样重试；说了"该怎么办"的错误，模型往往一次就改对。

钉住它，在测试文件的 import 里补上 `ToolCall`：

```python
from minicodex.agent import Agent, IncompleteStreamError, ToolCall
```

```python
async def test_F00_05_tool_exception_comes_back_as_output(stub_url: str) -> None:
    agent = Agent(model(stub_url), make_tools())
    call = ToolCall("call_1", "explode", {}, "{}")

    output = await agent._run_tool(call)

    assert "ValueError" in output
    assert "disk on fire" in output


async def test_F00_05_a_failing_call_does_not_cancel_its_siblings(stub_url: str) -> None:
    """Reuses the three-call recording but points one name at a broken tool."""
    log: list[str] = []
    tools = make_tools(log)
    tools["get_conditions"] = tools["explode"]
    agent = Agent(model(stub_url, stub_mode="three"), tools)

    result = await agent.run("go")

    assert log == ["get_temperature:New York", "get_temperature:London"]
    assert len([i for i in result.history if i["role"] == "tool"]) == 3


async def test_F00_06_unknown_tool_says_what_to_call_instead(stub_url: str) -> None:
    agent = Agent(model(stub_url), make_tools())
    call = ToolCall("call_1", "get_temperatureZ", {"city": "X"}, "{}")

    message = await agent._run_tool(call)

    assert "get_temperatureZ" in message
    assert "explode, get_conditions, get_temperature" in message
    assert "Call one of those instead" in message


async def test_F00_06_malformed_arguments_become_output_not_a_crash(stub_url: str) -> None:
    agent = Agent(model(stub_url), make_tools())
    call = ToolCall("call_1", "get_temperature", None, "{not json")

    message = await agent._run_tool(call)

    assert "not valid JSON" in message
    assert "{not json" in message
```

> - 前两个直接调用 `agent._run_tool(...)`，手工构造一个 `ToolCall`，不用经过模型。
> - `test_F00_05_a_failing_call_does_not_cancel_its_siblings`：把三个调用里的
>   `get_conditions` 换成会爆炸的工具，断言另外两个照常执行，而且**三个都有结果**——
>   失败的那个结果是一条错误信息。

```bash
git add .
git commit -m "fix: return tool failures to the model instead of crashing"
```

### 8.4 F00-01：循环停不下来

§4 写的判断方法：让模型一直要工具，看会发生什么。

模型可能陷入一种模式：搜索、读文件、再搜索、再读。每一轮单看都合理，合起来是死循环。
第一版有上限（`max_turns`），不会真的无限跑下去。**但光有上限不够**：跑到第 12 轮，
循环结束，用户拿到 `turn_limit` 和一个空答案。模型正做到一半，被砍断了。

**它不知道自己快没时间了。没人告诉它。** 模型不知道自己是一个循环里的一次迭代，
不知道有"第 13 轮不存在"这回事。你不说，它就按时间无限来规划。**F00-01 成立。**

修法是：**快到上限时，在历史里加一条 system 消息，告诉它还剩几轮。** 在 `agent.py` 的
常量里加上：

```python
BUDGET_WARNING_AT = 2
```

`run()` 的循环开头加上：

```python
            remaining = self.max_turns - turn_index

            # A budget the model cannot see is one it spends freely and is then
            # killed by, mid-thought, with nothing to show.
            if remaining <= BUDGET_WARNING_AT:
                history.append(
                    {
                        "role": "system",
                        "content": (
                            f"You have {remaining} tool-calling turn(s) left. "
                            "Wrap up and give your best answer now."
                        ),
                    }
                )
```

> `remaining` 是还剩几轮（包括这一轮）。剩下 2 轮及以下时，每一轮开始前都追加一条提醒。

> **这个模式以后会反复出现：系统里有一个状态，只有代码知道，模型不知道。**
> 权限、剩余的上下文空间、计划进行到了哪一步……都是这样。解法也总是一样：
> **把它写进模型能看到的上下文里。**

钉住它：

```python
async def test_F00_01_runaway_loop_is_bounded(stub_url: str) -> None:
    """The recording asks for a tool every time it has not seen a tool result,
    so a loop that never feeds results back would run forever."""

    class NeverSatisfied:
        """Strips tool results out, so the model always asks again."""

        def __init__(self, inner: OllamaModel) -> None:
            self.inner = inner
            self.calls = 0

        def stream(self, history: list[dict[str, Any]]):
            self.calls += 1
            return self.inner.stream([m for m in history if m.get("role") != "tool"])

    llm = NeverSatisfied(model(stub_url))
    agent = Agent(llm, make_tools(), max_turns=5)

    result = await agent.run("go")

    assert result.stop_reason == "turn_limit"
    assert result.turns_used == 5
    assert llm.calls == 5, "the bound must apply to model calls, not loop iterations"


async def test_F00_01_model_is_warned_before_the_budget_runs_out(stub_url: str) -> None:
    """Being killed mid-thought produces nothing.  Being told produces an answer."""
    seen: list[list[dict[str, Any]]] = []

    class Watching:
        def __init__(self, inner: OllamaModel) -> None:
            self.inner = inner

        def stream(self, history: list[dict[str, Any]]):
            seen.append([dict(m) for m in history])
            return self.inner.stream([m for m in history if m.get("role") != "tool"])

    agent = Agent(Watching(model(stub_url)), make_tools(), max_turns=4)
    await agent.run("go")

    notices = [m for m in seen[-1] if m["role"] == "system"]
    assert notices, "the model was never told it was running out of turns"
    assert "1 tool-calling turn(s) left" in notices[-1]["content"]
```

这两个测试是 §5.5 那个 `Protocol` 的回报：

> - `NeverSatisfied` 和 `Watching` 是**测试里临时写的两个小类**，什么都没继承，只要有一个
>   `stream(history)` 方法，就能直接交给 `Agent`。这就是"结构化类型"：看形状，不看出身。
> - `NeverSatisfied` 把历史里的 `tool` 消息全部删掉再转给录音服务。录音服务看不到工具结果，
>   就永远回"我要读文件"——模拟一个永远不满足的模型。测试断言循环在第 5 轮停下，而且
>   **模型确实只被调用了 5 次**。
> - `Watching` 在每次请求时把历史复制一份存下来（`dict(m)` 复制每条消息，免得之后被改动），
>   测试最后检查最后一次请求里有没有"还剩 1 轮"的提醒。
> - 两个测试都用 `max_turns=5` 或 `4`：这就是 §5.6 让上限可以传入的原因，不用真跑满 12 轮。

```bash
git add .
git commit -m "feat: warn the model before its turn budget runs out"
```

### 8.5 收工：`agent.py` 和 README

`src/minicodex/agent.py` 的最终版本，全部内容：

```python
"""The loop: ask, run what it asks for, feed the results back, ask again.

Everything lives in this one module on purpose.  The boundaries between stream
assembly, tool dispatch and the loop are guesses right now, and a boundary in
the wrong place is harder to remove than no boundary.  Interlude A pays this
off, once there are enough call sites for the boundaries to be observed rather
than invented.

Async is decided here rather than later: an agent spends nearly all its wall
clock waiting on IO, and converting a synchronous call chain to async in Python
means touching every caller on the path.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

from minicodex.model import Completed, StreamEvent, TextDelta, ToolCallDelta
from minicodex.recorder import NULL_RECORDER, Recorder


class IncompleteStreamError(RuntimeError):
    """The connection ended before the server sent its `[DONE]` sentinel."""


@dataclass(frozen=True)
class ToolCall:
    call_id: str
    name: str
    arguments: dict[str, Any] | None  # None when the model emitted invalid JSON
    raw_arguments: str


@dataclass(frozen=True)
class ModelTurn:
    text: str
    tool_calls: tuple[ToolCall, ...]
    finish_reason: str | None = None


class Model(Protocol):
    """Anything that can stream a response given a history.

    A Protocol rather than a base class: structural typing, no inheritance, so
    the cost of the abstraction is close to zero.  It earns its place because
    swapping the model is a requirement today -- the tests need a deterministic
    one -- not a guess about tomorrow.
    """

    def stream(self, history: Sequence[dict[str, Any]]) -> AsyncIterator[StreamEvent]: ...


ToolFn = Callable[[dict[str, Any]], Awaitable[str]]


@dataclass
class RunResult:
    final_text: str
    stop_reason: str  # "completed" | "turn_limit"
    turns_used: int
    history: list[dict[str, Any]] = field(default_factory=list)


DEFAULT_MAX_TURNS = 12
BUDGET_WARNING_AT = 2


class Agent:
    def __init__(
        self,
        model: Model,
        tools: dict[str, ToolFn] | None = None,
        *,
        max_turns: int = DEFAULT_MAX_TURNS,
        recorder: Recorder = NULL_RECORDER,
    ) -> None:
        self.model = model
        self.tools = tools or {}
        self.max_turns = max_turns
        self.recorder = recorder

    # -- assembling one response --------------------------------------------

    async def _collect(self, stream: AsyncIterator[StreamEvent]) -> ModelTurn:
        """Turn a stream of events into one finished turn, or refuse to.

        Two accumulators because the wire has two granularities: prose arrives
        one token at a time and gets joined, tool calls arrive whole and get
        placed by `index`.  Sorting by index rather than trusting arrival order
        costs one line and removes a question nobody wants to answer later.

        Nothing is returned until `Completed` arrives.  A stream that dies
        halfway therefore leaves nothing behind -- there is no half-built turn
        that could reach the history by accident.
        """
        text_parts: list[str] = []
        by_index: dict[int, ToolCallDelta] = {}
        completed: Completed | None = None

        async for event in stream:
            if isinstance(event, TextDelta):
                text_parts.append(event.text)
            elif isinstance(event, ToolCallDelta):
                by_index[event.index] = event
            elif isinstance(event, Completed):
                completed = event

        if completed is None:
            raise IncompleteStreamError(
                f"stream ended after {len(text_parts)} text chunk(s) and "
                f"{len(by_index)} tool call(s) without a [DONE] sentinel"
            )

        calls = []
        for index in sorted(by_index):
            raw = by_index[index]
            try:
                parsed = json.loads(raw.arguments) if raw.arguments else {}
                arguments = parsed if isinstance(parsed, dict) else None
            except json.JSONDecodeError:
                arguments = None
            calls.append(ToolCall(raw.call_id, raw.name, arguments, raw.arguments))

        return ModelTurn("".join(text_parts), tuple(calls), completed.reason)

    # -- running one tool ----------------------------------------------------

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

        try:
            return await self.tools[call.name](call.arguments)
        except Exception as exc:  # deliberately broad; see the docstring
            return f"Error: {call.name} raised {type(exc).__name__}: {exc}"

    # -- the loop ------------------------------------------------------------

    async def run(self, user_message: str) -> RunResult:
        history: list[dict[str, Any]] = [{"role": "user", "content": user_message}]
        final_text = ""

        for turn_index in range(self.max_turns):
            remaining = self.max_turns - turn_index

            # A budget the model cannot see is one it spends freely and is then
            # killed by, mid-thought, with nothing to show.
            if remaining <= BUDGET_WARNING_AT:
                history.append(
                    {
                        "role": "system",
                        "content": (
                            f"You have {remaining} tool-calling turn(s) left. "
                            "Wrap up and give your best answer now."
                        ),
                    }
                )

            self.recorder.record("request", {"turn": turn_index, "history": history})
            turn = await self._collect(self.model.stream(history))
            self.recorder.record(
                "response",
                {
                    "turn": turn_index,
                    "text": turn.text,
                    "finish_reason": turn.finish_reason,
                    "tool_calls": [{"id": c.call_id, "name": c.name} for c in turn.tool_calls],
                },
            )

            history.append(
                {
                    "role": "assistant",
                    "content": turn.text,
                    "tool_calls": [
                        {
                            "id": c.call_id,
                            "type": "function",
                            "function": {"name": c.name, "arguments": c.raw_arguments},
                        }
                        for c in turn.tool_calls
                    ],
                }
            )
            if turn.text:
                final_text = turn.text

            # Text is not a stop signal.  Models narrate before acting, and
            # stopping on the narration leaves the work undone while looking
            # exactly like success.
            if not turn.tool_calls:
                return RunResult(final_text, "completed", turn_index + 1, history)

            # Every call runs and every call is answered.  Skipping one leaves
            # it unanswered in history, and the *next* request is what fails.
            for call in turn.tool_calls:
                output = await self._run_tool(call)
                history.append({"role": "tool", "tool_call_id": call.call_id, "content": output})

        return RunResult(final_text, "turn_limit", self.max_turns, history)
```

**注意 `run()` 里那几段注释。** 它们解释的都是**反直觉的一行**：为什么不是 `if turn.text`、
为什么不能只执行第一个、为什么上限要说出来。这些代码字面上看不出理由，而没有理由的反直觉
代码，下一个人一定会"顺手修好"它。

最后更新 `README.md`，让第一次打开这个项目的人知道它能干什么、每个文件是什么：

````markdown
# minicodex — step 0: the loop

Ask a question the agent can only answer by reading a file first.

```bash
uv sync --all-extras

# Against your own Ollama:
uv run minicodex ask "What does src/minicodex/__init__.py define?"

# Or, with no GPU and no Ollama, against the recorded responses:
uv run minicodex serve-stub &
uv run minicodex ask "What does src/minicodex/__init__.py define?" \
    --base-url http://127.0.0.1:11435/v1

uv run pytest
```

| Path | What it is |
|---|---|
| `src/minicodex/model.py` | the HTTP client: SSE parsing, two granularities, two terminators |
| `src/minicodex/agent.py` | stream assembly, tool dispatch and the loop, together on purpose |
| `src/minicodex/stub_ollama.py` | responses recorded from a real Ollama on 2026-08-06, replayed verbatim |
| `src/minicodex/recorder.py` | append-only transcript of everything crossing the model boundary |
| `tests/test_agent.py` | one test per guarantee; `_naive_run` keeps the first version's bugs reproducible |

The stub is not a model. It replays bytes that were actually received, so the
client, the SSE parsing and the loop are all exercised for real — only the
model's judgement is missing.

## Deliberately unfinished

- history is `list[dict]`; nothing enforces one output per call — chapter 1
- tools run one after another — chapter 8
- `IncompleteStreamError` ends the session instead of retrying — chapter 12
````

> 最后一段 "Deliberately unfinished" 列出了**明知没做完的事**，以及它们会在哪里处理。
> 这比假装一切完美更有用：读的人知道哪些限制是有意的。

```bash
git add README.md
git commit -m "docs: describe the loop in the README"
```

---

## §9 验证这些测试真的有用

第 -1 章的规则：**把每个修复改回去，看对应的测试红不红。**

| 改回去 | 抓住它的测试 |
|---|---|
| `if not turn.tool_calls:` → `if turn.text:` | `test_F00_02_absence_of_tool_calls_is_the_stop_signal` |
| `for call in turn.tool_calls:` → `turn.tool_calls[:1]` | `test_F00_03_outputs_pair_one_to_one_with_calls` |
| `if completed is None:` → `if False:` | `test_F00_04_nothing_from_a_cut_stream_is_executed` |
| `except Exception` → `except ZeroDivisionError` | `test_F00_05_a_failing_call_does_not_cancel_its_siblings` |
| 删掉错误信息里的可用工具列表 | `test_F00_06_unknown_tool_says_what_to_call_instead` |
| 关掉上限提醒 | `test_F00_01_model_is_warned_before_the_budget_runs_out` |
| **`sorted(by_index)` → `list(by_index)`** | **没有测试抓住** |

六红一绿。**那个绿的比六个红的更值得记住。**

`sorted(by_index)` 按 index 排序，而不是按到达的先后。改成按到达顺序，测试照样全绿——
因为录音里三个调用本来就是按 0、1、2 的顺序到的。这行排序是**防御性的，目前不承担任何
作用**。它防的是一个我没观察到的情况，代价一行。哪天真观察到乱序，那时才会有对应的测试。
**假装它被验证过，就是骗人。**

全部跑一遍：

```
$ uv run pytest
........................................                                 [100%]
40 passed in 8.29s
```

（Windows 上实测。）

---

## §10 回顾：猜对了几条

回到 §4 那张表，逐条对账：

| 编号 | 动工前的猜测 | 结果 | 如果不防，它会怎么暴露 | 挡住它的东西 |
|---|---|---|---|---|
| F-1-04 | 不知道模型收到了什么 | ✅ **撞上了**（§7.1） | 🟠 想查问题时发现没有记录 | 请求和响应全部写进记录文件 |
| F00-03 | 一次多个调用只处理了一个 | ✅ **撞上了**（§8.1） | 🔴 严格的服务下一轮返回 400（Ollama 下是静默的胡言乱语） | 每个调用都执行、都有结果 |
| F00-04 | 流中途断了，半截被当完整 | ✅ **撞上了**（§8.2） | 🔴 崩溃，或者更糟：执行了半截的调用 | 没收到 `[DONE]` 就拒绝，碎片只在局部变量里 |
| F00-05 | 工具出错带崩程序 | ✅ **撞上了**（§8.3） | 🔴 崩溃 | 异常变成交给模型的信息 |
| F00-06 | 不存在的工具、坏 JSON | ✅ **撞上了**（§8.3） | 🔴 `KeyError` | 错误信息里列出可用工具 |
| F00-01 | 停不下来，或被砍断交不出东西 | ✅ **撞上了**（§8.4） | 🔵 长时间运行后才出现 | 上限 + **模型看得见的**剩余轮数提醒 |
| — | *（没猜到）* `[DONE]` 不是 JSON | ⚠️ **意外**（§5.1） | 🔴 崩溃，但**先正确打印了整句话** | 先判断结束标记，再解析 |
| F00-02 | *（没猜到）* **一有文字就停，活没干** | ⚠️ **意外**（§6） | 🟡 看起来像成功 | 停止信号是"没有工具调用" |
| F00-07 | *（没猜到）* **偶尔跑出不同结果** | ⚠️ **意外**（§7） | 🟡 修复只是碰巧通过 | 录下真实回复，原样回放 |
| — | *（没猜到）* 记录目录没被 git 忽略 | ⚠️ **意外**（§7.2） | 🟠 看 `git status` 才发现 | `.gitignore` + 测试 |
| F00-08 | *（没猜到）* 忘了 `await` | ⚠️ **意外**（§7.6） | ⚪ 只有一句警告 | 把 `RuntimeWarning` 变成错误 |
| F00-09 | *（第 -1 章预先布置了检查）* 阻塞调用卡住事件循环 | 🛡 **写出来的当下就被拦住**（§5.7） | ⚪ ruff 报错 | ruff `ASYNC240` + `asyncio.to_thread` |

标记的含义（全书通用）：

🔴 崩溃 · 🟡 静默错误 · 🟢 主动边界测试 · 🔵 长时间运行才出现 · 🟠 看日志/记录发现 ·
🟣 代码审查发现 · ⚫ 用户报告 · ⚪ lint/类型检查

从这张表能看出三件事：

**第一，猜的六条全中了。** 这不是因为我特别会猜，而是因为 §3 先**看了**真实的数据：
`tool_calls` 是带编号的列表、有两个结束信号——这些观察直接变成了猜测。**先观察，再猜，
猜得会准得多。**

**第二，最重要的两条都没猜到，而且都是 🟡。** F00-02 是活没干但答得漂亮，F00-07 是偶尔
才出错所以你以为修好了。两条都不报错，都得主动去找。**猜中的六条大多会自己跳出来（🔴），
真正危险的恰恰是那些不会跳出来的。**

**第三，工具替我挡掉了两条。** F00-08 和 F00-09 都是 ⚪：一个靠警告配置，一个靠第 -1 章
提前打开的 lint 规则。它们是最便宜的一类——不用想、不用找，写出来就被拦下。

---

## §11 交给 GitHub：推送、PR、审查、合并

第 -1 章说过：从这一章开始，每个功能在分支上做，做完合并回 `main`。现在把这个分支交出去。

### 11.1 第一次推送到 GitHub（如果还没做过）

第 -1 章的 CI 配置一直没有真正跑过，因为仓库还只在你的电脑上。

1. 在 GitHub 网页上点 **New repository**，起个名字（比如 `minicodex`），**不要**勾选
   "Add a README"之类的选项——仓库要是空的。
2. 创建后，页面上会显示仓库地址，形如 `https://github.com/你的用户名/minicodex.git`。
3. 在项目根目录里：

```bash
git remote add origin https://github.com/你的用户名/minicodex.git   # 登记远程仓库，名字叫 origin
git push -u origin main                                             # 先把 main 推上去
```

> `-u` 让本地的 `main` 记住它对应远程的 `origin/main`，以后直接 `git push` 就行。
> 第一次推送时，git 可能会弹出浏览器窗口让你登录 GitHub。

### 11.2 推送分支，开 PR

```bash
git push -u origin feat/agent-loop
```

推送完成后，打开 GitHub 上的仓库页面，会看到一条提示 **"feat/agent-loop had recent pushes"**
和一个 **Compare & pull request** 按钮。点它，就进入创建 **PR（Pull Request，合并请求）**
的页面：请求把 `feat/agent-loop` 合并进 `main`。

> 装了 GitHub 官方命令行工具 `gh` 的话，也可以直接 `gh pr create`。

PR 的描述写什么？和 commit message 一样：**做了什么，为什么，还有什么没做或没验证。**
比如：

```
Adds the agent loop: stream from an OpenAI-compatible endpoint, assemble each
turn, run the tools the model asks for, feed the results back.

Faults checked by reverting each guard (see the chapter): six guards turn a
named test red. One does not: sorting tool calls by index rather than arrival
changes nothing against a recording that already arrives in order. It is
defensive, not currently load-bearing.

Deliberately one module (agent.py) for now: the boundaries between assembly,
dispatch and the loop are not visible yet. history is still list[dict].
```

PR 一开，第 -1 章写的 CI 就会自动运行：装依赖、ruff 检查、跑全部测试。**测试对着录音服务跑，
所以 CI 那台机器上不需要装 Ollama，也不需要显卡。** 这是录音回放的另一个好处。
等 PR 页面上出现绿色的对勾，就说明在一台干净的机器上也全部通过了。

### 11.3 合并前自己审查一遍

一个人的项目没有别人审查，但可以自己在 PR 页面的 **Files changed** 标签里，把所有改动
从头看一遍。换个视角看，常常能发现在编辑器里看不到的问题。

审查的顺序：**正确性 → 边界情况 → 可测试性 → 命名 → 风格。** 风格最容易看出来、最有
"干活了"的感觉、也最不重要；放在最后，是为了逼自己先看前四项。

下面是这次审查提出的五个问题，以及各自的结局。

**1 · 正确性** — `_collect` 用 `by_index[event.index] = event` 覆盖写入。如果某家服务真的
分片发送参数（每片带同一个 index），后一片会**覆盖**前一片，而不是拼接起来。数据会被悄悄丢掉。

> **回答**：真实的风险，但不在这里修。这家服务观察到的行为是一次发完，为一个没见过的行为
> 写拼接逻辑，就是在为想象写代码。接第二家供应商时会先测、再写。**已经记进下一步的待办，
> 不在代码里写 TODO——一个没有人负责的 TODO 只是一个愿望。**

**结局：延后，但有明确的去处。** "接第二家供应商时"可以检查，"以后"不能。

**2 · 正确性** — 剩余轮数的提醒在**问模型之前**加进历史。如果模型这一轮直接结束，
历史里就留着一条"抓紧时间"给一个已经结束的对话。

> **回答**：接受，它无害，但要给出理由。这条提醒在 `return` 之后就是没人读的数据，
> 循环结束后没有代码会再读历史。如果挪到问模型之后，它就会跑到它本该领先的那条回复
> **后面**，那更糟。保持原样。

**3 · 边界情况** — `sorted(by_index)` 没有测试覆盖。

> **回答**：对，§9 的反向验证里我自己发现了，也写进了 PR 描述。不删，因为代价一行；
> 也不假装它被验证过。

**结局：承认、记录、保留。** 这是审查中最健康的一种结果：提出一个真问题，得到一个诚实但
不完美的回答。

**4 · 可测试性** — `_naive_run` 在测试文件里复制了一份循环。`Agent` 改了形状，它就会
和真实代码越走越远，最后什么都证明不了。

> **回答**：代价已知、接受、有边界。它让两个 bug 可以随时运行、亲眼看到，这值得那点重复。
> 边界写在了它的 docstring 和 README 里：只演示这两个 bug，不许再长。哪天它和真实代码
> 走远了，**直接删掉，不要修。**

**5 · 命名** — `ToolCallDelta` 叫 Delta，但它不是碎片，是一个完整的调用。名字在撒谎。

> **回答**：承认问题，但不改名。`delta` 是服务端那个字段的名字
> （`choices[0].delta.tool_calls`）。**和协议用同一个词，胜过一个单独看更准确的名字。**
> docstring 里写明了："Named `Delta` because that is the field it arrives in, not because
> it is a fragment."

**结局：拒绝，附理由。** 审查意见被否决是正常的；让否决站得住的，是**理由写在了下一个人
会看到的地方**。

**这次审查没有提的一条。** 它**没有要求拆分模块**。`agent.py` 两百多行，混着组装、执行工具、
循环好几种职责，每一种直觉都在说"该拆了"。但 PR 描述里已经写明：放在一个文件里是有意的，
理由是边界还看不出来。在这个前提下，"现在就拆"等于要求作者去猜边界。

> **一条要求对方去猜的审查意见，比不提更糟。** 它会产生一个没人相信的抽象，
> 而那恰恰是最难拆掉的东西。等调用的地方多起来、边界被**观察**到而不是被**发明**出来时，
> 再拆。

### 11.4 合并

CI 通过、自己审查完，在 PR 页面点 **Merge pull request**，再点 **Confirm merge**。
合并后页面上会出现 **Delete branch** 按钮，可以删掉远程分支——内容已经进了 `main`。

回到终端，把本地也同步过来：

```bash
git switch main
git pull                          # 把合并后的 main 拉下来
git branch -d feat/agent-loop     # 删掉本地分支
```

> **不用 GitHub 的话**，也可以在本地合并：
> ```bash
> git switch main
> git merge --no-ff feat/agent-loop
> ```
> `--no-ff` 会保留一个"合并提交"，历史里能看出这些提交是一起做的一个功能。

### 11.5 回头看：这一章的提交

```
docs: describe the loop in the README
feat: warn the model before its turn budget runs out
fix: return tool failures to the model instead of crashing
fix: refuse a stream that ends without [DONE]
fix: run every tool call the model asks for
chore: fail the test suite on RuntimeWarning
test: replay recorded Ollama responses instead of inventing them
chore: keep recorded sessions out of git
feat: record every model request and response to a transcript
fix: stop only when the model asks for no more tools
feat: add the loop, one tool, and the ask command
feat: stream responses from an OpenAI-compatible endpoint
```

（`git log --oneline` 的输出，最新的在最上面，省略了 hash。）

**第二个提交里有一个 bug，第三个提交修掉了它。** 这是真实开发的样子：提交时以为是对的，
后来发现不对，就用一个新的提交修掉，而不是回头改历史——更何况以后这些提交可能已经推送出去了。

**中途撞到的报错不单独提交。** §5.1 那个 `JSONDecodeError`、§6 那行临时的 `print`，
都是中间状态，没有一个是"能独立通过测试的完整想法"。但**撞报错的过程要留下来**——
留在 commit message 的正文里，或者代码注释里。

> **git 历史记录的是状态，注释和 message 记录的是理由。** 把探索过程塞进 git 历史
> （`wip`、`try again`、`fix fix`），两边都做不好。

---

## §12 本章给 CI 加了什么

**什么都没加。**

新加的保护——`filterwarnings = ["error::RuntimeWarning"]`——写在 `pyproject.toml` 里，
CI 现有的"跑测试"那一步自动就用上了。

值得专门说一句，因为条件反射是每章都给 CI 加点什么。第 -1 章的规则仍然成立：
**一个检查凭什么进"能挡住合并"的 CI？凭它拦下过真实的问题。** 这一章没有遇到需要新检查
才能防住的问题。

**但这一章加了一个运行时依赖**：`httpx`。第 -1 章的 `dependencies = []` 到此为止。
理由很实在：要和模型说话，就得发 HTTP。

---

## §13 三条主线各自留下了什么

### 主线 A · 需求变代码

**先看见它动，再谈设计。** 这一章的第一段代码是"把每一行打出来"，不是架构。那九行原始
输出里有三个想不出来、只能看出来的事实（碎片长度不规则、工具调用是完整的、两个结束信号），
**它们直接决定了后面每一个函数的形状**，也直接变成了 §4 的猜测。

**不为没见过的情况写代码。** 参数不分片，就不写拼接逻辑。别家可能分片，等接到别家再说。

**这一章做的五个抽象决定：**

| 东西 | 决定 | 理由 |
|---|---|---|
| async | **现在就做** | 以后改要动每个调用点，而且"一直在等"是确定的 |
| 三个事件类型 | **现在就做** | 信任边界：服务端的 JSON 到此为止 |
| `Model` Protocol | **现在就做** | 今天就有两个实现：真模型和录音服务 |
| `request_body()` 单独成方法 | **现在就做（留缝）** | 以后要单独查看、对比请求体，而不发网络请求 |
| 历史的专门类型 | **现在不做**（`list[dict]`） | 只有一处写入，形状还是猜的 |
| 拆分 `agent.py` | **现在不做** | 边界还看不出来，分错了比不分更难改 |

### 主线 B · 工程化交付

| 动作 | 本章的规则 |
|---|---|
| 分支 | 功能在分支上做；推送、开 PR、CI 通过、自己审查，再合并 |
| 提交时机 | 每个能跑的小步当场提交；发现旧提交有 bug，用新提交修 |
| 提交范围 | 两件不相干的事分开提交，用 `git add 文件名` 挑选 |
| 中途的报错 | 不提交。**git 记状态，注释和 message 记理由** |
| message 正文 | 记录"文档和实际对不上"；记录"这条没验证到" |
| 审查的结局 | 改 / 拒绝（附理由）/ 承认（写下来）/ 延后（给去处），四种都合理 |
| 审查不该做的 | 不提一条要求对方去猜的意见 |
| CI | 没遇到对应的问题，就不加检查 |

### 主线 C · 故障

**先观察，再猜。** 看过真实数据之后猜的六条全中了；没看数据之前，根本不知道该猜什么。

**第一招：永远不要相信模型对自己行为的描述。** 检查能观察到的效果——哪个工具被调用了、
传了什么参数、文件是不是真的变了。

**第二招："偶尔才出错"比"每次都错"更危险**，因为它让你误以为修好了。解法不是多跑几次，
而是**把那次真实回复录下来，原样播回去**。

**第三招：撞到一个凭直觉写错的地方后，顺着查一遍所有凭直觉写的地方。** 问"如果现实不像
我想的那样呢"。

**第四招（从第 -1 章延续）：写完修复，把修复改回去，确认测试会红。**
**没红的那一条比红了的六条更值钱**——它指出了哪行代码是一厢情愿。

---

## 如果你只记住三件事

1. **先看见它动。** 打开真实的流看九行原始输出，比想一小时有用。这一章后面所有代码的形状，
   都是那九行决定的。

2. **模型说它做了，不等于它做了。** F00-02 里那句"我这就去读那个文件"说得非常漂亮，
   文件从来没被打开过。检查实际发生的事，不检查措辞。

3. **假模型是调查工具，不是准备工作。** 先撞见问题，发现每次跑结果都不太一样、查不下去了，
   才去录制和回放。

---

## 动手

**用 Ollama 的：**

```bash
cd steps/step00_minimal_loop
uv sync --all-extras
uv run minicodex ask "What does src/minicodex/__init__.py define?" --model qwen3
uv run pytest
```

**不用 Ollama 的：** 开两个终端。

```bash
# 终端一：启动录音服务，保持运行
cd steps/step00_minimal_loop
uv sync --all-extras
uv run minicodex serve-stub
```

```bash
# 终端二
cd steps/step00_minimal_loop
uv run minicodex ask "What does src/minicodex/__init__.py define?" --base-url http://127.0.0.1:11435/v1
uv run pytest
```

> 在 macOS/Linux 上也可以用一个终端：`uv run minicodex serve-stub &` 让它在后台运行，
> 用完后 `kill %1` 停掉。PowerShell 里的 `&` 是另一个意思，不能这样用。

`uv run pytest` 不需要录音服务在运行：测试会自己启动一个。

**建议自己做一遍的四件事：**

1. **跑一次 §3 的临时脚本**，把原始流打印出来看一遍。这是本章的起点，也是以后面对任何
   陌生 API 时该做的第一件事。
2. 打开 `.minicodex/recordings/` 里的文件，找到**第二轮的 `request`**，看清楚模型在回答
   之前收到了什么。这是以后最常做的动作。
3. 把 `agent.py` 里的 `if not turn.tool_calls:` 改成 `if turn.text:`，跑测试看哪个红。
   然后跑 `ask`——**注意它的输出看起来仍然完全正常。**
4. 有 Ollama 的话，把同一个问题问五遍，看回答变不变。这是 §7 那个"偶尔才不一样"的第一手体验。

---

## 选读 · codex 是怎么做的

> 这一节是和 codex（OpenAI 用 Rust 写的编码 Agent，本书参照的对象）的对照，基于写作时
> （2026 年）的代码仓库，文件名和结构以后可能会变。不读不影响后面的内容。

**流的结束事件。** codex 对着 OpenAI 的 Responses API，等的是 `response.completed` 事件，
`codex-rs/core/src/client.rs` 里有完整的流处理逻辑。少了结束事件，整个响应作废——
和我们的 `IncompleteStreamError` 是同一条规则。

**假模型 + 录制回放是它的主力测试手段。** `codex-rs/core/tests/suite/` 下有大量测试文件，
都跑在一个假的 responses 服务上。文件名本身就像一份故障清单：`abort_tasks.rs`、
`compact_resume_fork.rs`、`quota_exceeded.rs`、`pending_input.rs`、`mcp_refresh_cleanup.rs`。

**"模型看不见的状态"这个问题有多大。** `codex-rs/core/src/context/` 下有一整个目录的模块，
每一个都是一段会被写进模型上下文的片段，比如：

- `token_budget_context.rs` —— 上下文空间还剩多少
- `rollout_budget.rs` —— 剩余额度
- `turn_aborted.rs` —— "你上一轮被打断了"

外加 `prompts/templates/goals/budget_limit.md`，专门处理预算用完时怎么收尾。
**"让模型知道自己的处境"不是一行 if，而是一个子系统。** 我们这一章写了它的第一个成员。

**工具错误就是提示。** codex 有 `consequential_tool_message_templates.json` 和
`mcp_tool_approval_templates.rs`：**错误和提示信息是被当成模板来管理的**，
而不是散落在代码里的字符串。

**"一坨"要不要拆？** 看 `core/src/` 就知道答案不是"一开始就拆好"：

```
core/src/tools/handlers/multi_agents/       ← 第一版
core/src/tools/handlers/multi_agents_v2/    ← 第二版，和第一版并存
core/src/compact_remote.rs
core/src/compact_remote_v2.rs
core/src/compact_remote_v2_attempt.rs
```

**这是 OpenAI 的团队。他们也是先写一版、发现不行、再写第二版，而且第一版还留着。**

---

**下一章**：[先定协议，再写逻辑](ch01-protocol.md)——接第二家供应商，然后发现 `list[dict]`
和"参数是完整的 JSON 字符串"这两个假设同时崩掉。

---

# 附录 · 对照检查

## T1 · 每个文件是在哪一节写出来的

本章结束时，你的项目内容应该和 `steps/step00_minimal_loop/` 一致（测试函数的先后顺序可以不同）。
对不上时，按这张表找到对应的小节：

| 文件 | 在哪写的 |
|---|---|
| `pyproject.toml` | `httpx` 依赖：§3.1；`pytest-asyncio` 和 `asyncio_mode`：§7.4；`filterwarnings`：§7.6 |
| `src/minicodex/model.py` | §5.3（一次写完，之后没再改） |
| `src/minicodex/agent.py` | §5.5 开头和类型，§5.6 第一版，§6.2 停止条件，§7.1 记录器，§8.1–§8.4 各处修复；最终版见 §8.5 |
| `src/minicodex/tools.py` | §5.7 |
| `src/minicodex/__main__.py` | §5.8 第一版，§7.1 加记录器，§7.3 最终版 |
| `src/minicodex/recorder.py` | §7.1 |
| `src/minicodex/stub_ollama.py` | §7.3 |
| `.gitignore` | §7.2 |
| `tests/conftest.py` | §7.4 |
| `tests/test_agent.py` | §7.5 新建，§7.6、§8.1–§8.4 各追加一部分 |
| `tests/test_packaging.py` | §7.2 改了 `.gitignore` 检查列表 |
| `README.md` | §8.5 |

`__init__.py`、`.github/workflows/ci.yml`、`.vscode/` 这一章没有改动。

## T2 · 常见报错对照表

| 症状 | 原因 | 修法 |
|---|---|---|
| `httpx.ConnectError` | 模型服务没在运行，或地址写错了 | 确认 Ollama 在运行；用录音服务的，确认另一个终端里 `serve-stub` 开着，并且加了 `--base-url` |
| `ModelHTTPError: HTTP 404 ...` | 模型名不对，Ollama 里没有这个模型 | `ollama pull` 拉下来，或用 `--model` 指定一个已有的 |
| 用默认模型报错，提示需要登录 | `gemma4:31b-cloud` 是云端模型 | `ollama signin`，或用 `--model` 换成本地模型 |
| `ModuleNotFoundError: No module named 'httpx'` | 改了 `pyproject.toml` 没同步 | `uv sync --all-extras` |
| async 测试没有运行，或提示 async 函数不被支持 | 没装 pytest-asyncio 或没设 `asyncio_mode` | 按 §7.4 加上并 `uv sync --all-extras` |
| 测试失败，提示 `coroutine ... was never awaited` | 某处调用 async 函数时漏了 `await` | 找到那一行补上 `await`（这正是 §7.6 那条配置在起作用） |
| 解析时 `JSONDecodeError`，而且是在输出了完整答案之后 | 把 `[DONE]` 当 JSON 解析了 | 先判断 `payload == "[DONE]"`（§5.1） |
| ruff 报 `ASYNC240` | 在 `async def` 里直接调用了 `Path` 的读写方法 | 用 `asyncio.to_thread(...)`（§5.7） |
| 答案只有一句"我这就去读……" | 循环把"有文字"当成了结束 | 停止条件改成"没有工具调用"（§6.2） |
| `git status` 里出现 `.minicodex/` | 记录目录没被忽略 | 按 §7.2 加进 `.gitignore` |
| PowerShell 里 `serve-stub &` 不起作用 | `&` 在 PowerShell 里是另一个意思 | 另开一个终端运行 `serve-stub` |
