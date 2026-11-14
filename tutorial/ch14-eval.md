# 第 14 章 · 评估与回归：改了一处，它变好了还是变坏了

> **代码**：`steps/step14_eval/`
> **分支**：`feat/eval`
> **产出**：三样东西。一、`minicodex replay`：把一次真实运行的录制拿回来，不联网、不碰文件，对着现在的代码重跑一遍，哪里和当时不一样就指出来。二、一个六个任务的**评估集**：改了提示词或者代码之后，逐个任务告诉你它变好了还是变坏了。三、三个检查"测试本身靠不靠得住"的小工具
> **前置**：做完第 13 章。本章 35 个测试全部不联网。探针 `probe_eval.py` 七段里 `replay`、`leak` 两段不联网，其余五段要真的请求模型（OpenAI 几美分；Ollama 云端模型免费）；没有 key 照着读正文里的输出即可，**回放那条主线不需要 key，用第 0 章的录音服务就能从头做到尾**
> **这一章可以分四次读**：§1–§3 是"为什么需要这一章"；§4–§9 是录制和回放；§10–§14 是评估集；§15 往后是三个小工具、CI 和收工

---

## §0 开工前的准备

### 0.1 本章会遇到的新概念

- **回归（regression）**：以前是好的，改了别的东西之后坏了。**回归测试**就是专门防这个的测试：它不证明新功能对，它证明旧功能没被碰坏。
- **录制（recording）**：第 -1 章起，每次 `minicodex ask` 都会把"发给模型的请求、模型的回复"一行一行写进 `.minicodex/recordings/` 下的一个文件。这个文件就是录制。
- **回放（replay）**：不去问真的模型，而是把录制里**当时的回复**原样再给程序一遍。程序以为自己在和模型说话，其实对面是一份文件。
- **测试替身（test double）**：测试里用来顶替真东西的假东西。回放用的那个"假模型"就是一个替身。前面各章的 `ScriptedModel`（照着剧本说话的模型）也是。
- **快照（snapshot）**：第 13 章讲过——把"现在的样子"存下来，以后变了就报警。这一章的快照不用手抄：一份录制里本来就存着每一次请求的全文。
- **漂移（drift）**：现在的代码发出去的请求，和录制里当时发出去的不一样了。可能是你故意改的，也可能是改别处时碰坏的。回放的工作就是把它指出来。
- **轨迹（trajectory）**：一次运行**做了什么**——调了哪些工具、传了什么参数、最后磁盘上的文件长什么样。和它相对的是**措辞**：模型最后说的那段话。
- **评估集（eval set）**：一组固定的任务，每个任务带着"怎样才算做对"的检查。改动前跑一遍，改动后跑一遍，逐个任务比。
- **饱和**：一个任务在改动前后都是满分。饱和的任务能发现"坏掉"，发现不了"变好"。
- **不稳定的测试（flaky test）**：代码一个字没改，同一个测试这次过、下次不过。
- **缩小（shrink）**：随机测试找到一个会失败的大输入之后，自动删掉其中无关的部分，留下最小的、仍然失败的那一点。
- **LLM 裁判**：让一个模型去给另一个模型的回答打分。
- **CI 的三层**：挡住合并的（必须快）；合并之后才跑的（可以慢）；定时跑的（可以要钱、可以不稳）。

### 0.2 本章会遇到的新 Python 写法

| 写法 | 意思 |
|---|---|
| `class X(BaseException):` | 所有异常的总根。`except Exception` **抓不到**直接继承它的异常——`KeyboardInterrupt`（Ctrl-C）就是这样逃出各种 `except` 的 |
| `itertools.count()` 配 `next(c)` | 一个每次给出下一个整数的计数器：0、1、2…… |
| `d[(turn, attempt)] = ...` | 元组可以当字典的键。"第 0 轮第 1 次尝试"就是键 `(0, 1)` |
| `d.setdefault(k, []).append(v)` | `d` 里没有 `k` 就先放一个空列表，然后往那个列表里加 `v` |
| `xs.pop(0)` | 取走列表的第一个元素（列表变短） |
| `{**a, "k": v}` / 字典字面量里的 `**evidence` | 把另一个字典的内容摊开放进来；键重复时**后写的覆盖先写的** |
| `zip(a, b, strict=False)` | 两个序列并排走，短的那个走完就停，不报错 |
| `set(a) \| set(b)` | 两个集合的并集 |
| `next((x for x in xs if 条件), 默认值)` | 第一个符合条件的；一个都没有就用默认值 |
| `@property` | 写成方法，用起来像字段：`recording.turns` 不用加括号 |
| `field(default_factory=dict)` | 数据类的字段默认是"一个新的空字典"（直接写 `= {}` 会让所有对象共用同一个） |
| `getattr(item, "tool_calls", ())` | 取 `item.tool_calls`；没有这个字段就给 `()` |
| `Callable[[Trajectory], bool]` | 类型标注："一个接收 `Trajectory`、返回真假的函数" |
| `"abc".ljust(8)`、`"abc".rjust(8)` | 用空格补到 8 个字符宽，靠左 / 靠右。排表格用 |
| `path.rglob("*")` | 这个目录下的所有东西，一层层往下找 |
| `(only,) = xs` | 断言 `xs` 里**恰好一个**元素并把它取出来；不是一个就报错 |
| `re.compile(r"(?P<name>...)")` | 正则表达式里给一段起名字，匹配后用 `m["name"]` 取 |
| `pytest.raises(E, match="...")` | 断言这段代码抛出 `E`，而且消息里有这段文字 |
| `capsys.readouterr()` | pytest 提供的：取出到现在为止打印到屏幕上的内容 |

### 0.3 开分支

```bash
git switch main
git pull
git switch -c feat/eval
```

### 0.4 没有 key 也能跟到哪里

回放这条线（§4–§9）用第 0 章的录音服务就能完整做一遍。另开一个终端：

```bash
uv run minicodex serve-stub --port 11435
```

之后凡是 `minicodex ask` 都加上 `--base-url http://127.0.0.1:11435/v1`。

评估集这条线（§10–§14）的**代码和测试**都不需要 key；只有"真的拿六个任务去问模型"那几次测量需要。那些输出是 2026 年 8 月在 Windows 上对 OpenAI 的 `gpt-4o-mini` 和 Ollama 的 `gemma4:31b-cloud` 量的，正文原样照录，并且每一处都标了"实测于 2026-08"。**这次改写时手边没有 key，这几段没有重量过**；其余凡是不联网的输出，都是这次在 Linux 上重新跑出来的。

---

## §1 这一章要做出来的东西

先看它动起来的样子。在你自己的项目根目录，对着录音服务问一次：

```
$ uv run minicodex ask "What does src/minicodex/__init__.py define?" \
    --base-url http://127.0.0.1:11435/v1 --yes
...
[gemma4:31b-cloud | completed after 2 turn(s)]
[transcript: .minicodex/recordings/session-1794663608.jsonl]
```

最后一行那个文件，从第 -1 章起每次都在写。这一章给它找到第二个用处：

```
$ uv run minicodex replay .minicodex/recordings/session-1794663608.jsonl
.minicodex/recordings/session-1794663608.jsonl
  2 turn(s), 2 attempt(s) (0 failed), 1 tool call(s)
  recorded against ollama/gemma4:31b-cloud
  turn 0: 2 message(s) match
  turn 1: 4 message(s) match

ok  completed after 2 turn(s), same as recorded
```

这一遍**没有联网，没有读任何文件**（录音服务可以关掉再试）。程序把那次对话原样重走了一遍，每发一次请求就和录制里当时那次比一比。两次都一样，所以是 `ok`。

现在往 `src/minicodex/prompts/system.md` 末尾随手加一句 `Always answer in haiku.`，再回放：

```
$ uv run minicodex replay .minicodex/recordings/session-1794663608.jsonl
.minicodex/recordings/session-1794663608.jsonl
  2 turn(s), 2 attempt(s) (0 failed), 1 tool call(s)
  recorded against ollama/gemma4:31b-cloud

drift: turn 0 attempt 0: the request is no longer what was recorded in session-1794663608.jsonl
      message 0 (system): content changed
      was: ...ou. Follow them.\n\nYou have an `update_plan` tool that keeps a short ...
      now: ...ou. Follow them.\n\nAlways answer in haiku.\n\nYou have an `update_pla...
```

它指出了是第几条消息、哪个字段、从哪个字开始不一样。**没有人为这句话写过测试**——用来比对的那份"标准答案"，是你上一次真的用这个程序干活时留下的。（把那句话删掉。）

这是第一样东西。第二样是评估集，要真的问模型才有意思，所以这里只放结论（§13 是全过程）：第 13 章往系统提示词里加的那句"拿不准就问"，放到六个任务上各跑三次——

```
  task                   without          with
  add-function               3/3           3/3
  fix-failing-test           3/3           3/3
  read-and-answer            3/3           3/3
  leave-it-alone             3/3           3/3
  ambiguous                  0/3           3/3
  two-files                  3/3           3/3
  TOTAL                    15/18         18/18

  made worse by the sentence: (none)
```

（实测于 2026-08，`gpt-4o-mini`。）最后一行是这张表存在的理由：**总分变好不等于没有任务变差**，所以要专门有一行说"哪个任务被弄坏了"。

---

## §2 定需求，猜故障

需求：

- 一次真实的运行，能原样再跑一遍——不要钱、不要网络、每次结果一样；
- 重跑的时候，代码发出去的东西和当时不一样了，要有人说；
- 改了一处会影响**每一次**会话的东西（提示词、压缩、以后的记忆），能回答"哪些任务变差了"；
- 判断"做对了没有"，看它做了什么，不看它怎么说；
- 这些检查自己得靠得住：不能时灵时不灵，失败了得让人看得懂；
- 要花钱、要联网的那部分，不能挡住日常的合并。

动工前的猜测清单，八条：

| 编号 | 猜测 | 怎么判断 |
|---|---|---|
| F14-01 | 检查模型**说了什么**的测试，模型换个说法就红；更糟的是它可能一直不红 | 同一个任务跑五次，看措辞变不变 |
| F14-02 | 测试偶尔失败，大家习惯了"重跑一下"，真的 bug 被淹没 | 需要一个能点名的工具，而不是靠人记 |
| F14-03 | 上下文怎么拼的被悄悄改了，没有东西会红 | 把一次真实运行的请求存成快照 |
| F14-04 | 随机测试失败了只给一个种子，看不懂 | 让它自动缩到最小 |
| F14-05 | 所有测试都用真模型：又慢又贵又不稳，最后被关掉 | 分层：哪些必须离线，真模型放哪 |
| F14-06 | 让模型当裁判打分，裁判自己有偏好 | 同一对答案，换个顺序再问一遍 |
| F14-07 | 改了提示词"感觉变好了"，其实有任务变差了 | 同一组任务，开/关各跑一遍，逐个任务比 |
| F14-08 | 评估用的材料被被评估的程序自己看到了 | 看评估时 Agent 够得着什么 |

先说结果：

- **F14-03 是这一章的主线，而且比清单写的深。** 第 -1 章的录制器从写下来那天起，文档里就写着"第 14 章会回放它"。它做不到：录下来的工具调用**只有名字，没有参数**；录下来的失败**只有一句人话，没有状态码**；录制里也**没写这次运行用的是哪个模型、哪种权限**。三样补齐之后，回放顺带就成了快照。
- **F14-08 当场发生了，而且是产品的 bug。** 第一次真的跑评估集，Agent 在一个只有两个文件的工作区里 `ls -R`，看见的是**本仓库**，然后动手去改 `src/minicodex/evals.py`——给它打分的那个文件。原因是 `run_shell` 十二章以来一直在"启动程序的那个目录"里跑，而不是在交给它的那个目录里。
- **F14-01 成立，形状更坏。** 检查措辞的断言在一个模型上五次全过（五次回答一字不差），换一个模型五次五个说法——其中一次**根本没干活**，却照样通过了"回答里有 subtract"。
- **F14-06 成立。** 两个模型当裁判，都是 10 次里 10 次选了那个**错的但说得很自信**的答案。
- **F14-07 的结论是"没有任务变差"**，以及这句话的分辨率：六个任务里五个是饱和的。
- **F14-02、F14-04、F14-05** 各补了一个机制；F14-02 的那个工具一百来行，写的过程中出了三个 bug，**三个都是"它少看了东西，然后照样报告一切正常"**。

**改写这一章时又找到九处**，全部是同一种形状——**检查的东西说的比它看的多**：

- `minicodex replay` 对一次**只跑了一半**的运行打印 `ok ... same as recorded`（§9.1）；
- `--loose` 明明什么都没比，照样打印 `match`（§9.2）；
- 回放一次录下来的"请等 46 秒"，**真的静悄悄等了 46 秒**（§9.3）；
- 会话里有子 Agent 时，读录制的函数把父子两段对话**拼成了一段谁也没说过的对话**，不报任何错（§9.4）；
- 缩小器 `minimise` 写好了、测过了、正文里讲了——**没有任何地方调用它**；接上之后第一次用就崩在自己身上（§16）；
- 不稳定检查看不见"上一轮在、这一轮不见了"的测试（§15.5）；
- 定时任务的"没有 key 就别跑"那个条件，是一个永远为真的表达式（§17）；
- 探针 `leak` 那一段的结论是直接 `print` 出来的一句话，没有量过（§12.3）；
- 这一章开头那两条"全套测试都抓不住"的变异，**现在抓得住了**（§3）——这一条是好消息，但原来的正文还照着旧数字在讲。

---
## §3 现状：改一个字，谁会发现

### 3.1 这一章最初的出发点，和它现在的样子

这一章第一次写的时候，是从两条变异开始的。把第 13 章交付的代码原样拿来，只改一行，跑全套测试：

```python
# src/minicodex/__main__.py
-        instructions=_instructions(session, tools),
+        instructions=None,
```

这一行关掉的东西：第 5 章的权限说明、第 11 章那段让计划工具真的被用起来的话、第 13 章的整个系统提示词——三章量出来的成果，一个参数全关掉。当时的结果是 **1525 个测试全绿**。

原因不神秘：**每一个测 `_instructions()` 的测试，都是自己去调 `_instructions()` 的。** 没有一个测试问过"到底有没有人调它"。零件个个测过，零件之间的那根线没人测。

第二条是把"用到窗口的 75% 开始压缩"改成 95%，同样全绿：第 6 章十三条故障的测试全在测"怎么切"，没有一条测"什么时候开始切"。

**改写这一章时，我把这两条重新跑了一遍。它们现在都抓得住了。**

不是这一章的功劳。第 11、13 章改写时各自补了几个"真的从命令行入口跑一遍"的测试，顺带把这两条也守住了。那就量得更彻底一点：把 `__main__.py` 和 `Wiring` 里"把一样东西交给另一样东西"的连线，一次去掉一根，每次跑全套（第 13 章现在是 1600 个测试）：

```
    3 fail  <-  the CLI sends no system message
    1 fail  <-  the CLI records nothing
    2 fail  <-  the CLI never tells the loop its window
    1 fail  <-  a backoff is not announced
    3 fail  <-  the CLI has no summariser
    3 fail  <-  the session is not written to a rollout
    2 fail  <-  nobody asks about an unfinished plan
    1 fail  <-  every tool call runs alone
    1 fail  <-  the concurrency cap is not passed on
    1 fail  <-  the retry policy is not passed on
    2 fail  <-  the summariser is not passed on
    1 fail  <-  the dialect is not passed on
    1 fail  <-  compaction starts at 95% instead of 75%

13/13 noticed
```

（这次在 Linux 上实测；每一行是"去掉这根线之后有几个测试变红"。）

十三根线，十三根都有人守。这是插曲 B 和后面三章老老实实补测试的结果，值得写下来——**也意味着这一章原来的开头不能再照着讲了**。旧正文里"1525 个测试没有一个知道"那句话，对今天的代码是假的。

### 3.2 那今天还有什么没人守

线守住了。再试另一种东西：**发给模型的字**，和**决定模型能看到多少的数**。同样一次改一处，跑全套：

```
    2 fail  <-  the budget warning no longer says to stop starting work
    0 fail  <-  the plan paragraph no longer says one step in_progress
    1 fail  <-  the never-policy block no longer says do not ask
    0 fail  <-  a child is not told it was given a task by another agent
    0 fail  <-  command output is clipped at 2,000 characters, not 20,000
    0 fail  <-  a child's result is clipped at 400 characters, not 4,000
    7 fail  <-  a summary may be 70 tokens, not 700
    3 fail  <-  the warning comes with 1 turn left, not 2

4/8 noticed
```

（这次在 Linux 上实测。）

四处改了没有任何测试变红：

- **计划那段话里删掉"同一时间只许一步在进行中"。** 第 11 章量过，这段话让计划工具的使用从 30 次里 25 次变成 30 次里 30 次。现在删掉其中一句，没有东西知道。
- **子 Agent 的开场白从"另一个 Agent 交给你一个独立的任务"变成"Hello."。**
- **命令输出的截断长度从两万字变成两千字。** 模型以后看到的每一条命令输出都短了十倍。
- **子 Agent 交回的结果从四千字截到四百字。**

抓住的那几处，靠的是有人专门为那句话写过测试（比如第 5 章为"never 模式下别提申请权限"量过、写过）。没抓住的，是没有人想到要写的。**想到了才有，是手写测试的天性。**

而且即使全抓住了，还有一个问题手写测试永远回答不了：

> 改了这句话之后，**模型的表现是变好了还是变坏了？**

一个测试只能说"这句话变了"。它没法说"变了之后，Agent 还会不会先读后改、还会不会在该问的时候问"。那得真的拿任务去问模型。

所以这一章要两样东西，各管一半：

| | 管什么 | 上面四处它管得了吗 |
|---|---|---|
| **回放**（§4–§9） | 程序发给模型的每一个字，和上次真的运行时是不是一样 | 管得了第一处；后三处管不了，原因见下 |
| **评估集**（§10–§14） | 模型拿到这些字之后，做的事对不对 | 原则上都管，前提是有任务用得到它们 |

回放对第一处是这样的（把那句话删掉之后，回放 §1 的录制）：

```
drift: turn 0 attempt 0: the request is no longer what was recorded in session-1794663608.jsonl
      message 0 (system): content changed
      was: ...n before you start, keep exactly one step `in_progress`, and mark a st...
      now: ...n before you start, and mark a step `completed` once the work behind i...
```

对第三处（输出截到两千字），同一份录制回放出来是 `ok`。**这不是 bug，是回放的边界**，现在就说清楚：回放不真的执行工具（§7），截断发生在工具**里面**，所以它看不见。第二、四处同理——它们发生在子 Agent 的那段对话里，而回放只走父亲的那一段（§9.4）。

哪些看得见、哪些看不见，§8.4 有一张完整的表。先记住这一条：**一个检查能看见的范围，得在用它之前问清楚。** 这句话在这一章后面会一再出现，大多数时候是因为我没问。

---
## §4 F14-03（上）：一份写给人读的记录，重跑不起来

### 4.1 第 -1 章留下的一句话

`recorder.py` 里那个类的说明，从第 -1 章起就是这么写的：

```python
class Recorder:
    """Append-only JSONL sink for model-boundary events.

    Not a logger.  Logs are prose for a human watching a terminal; this is a
    machine-readable transcript that chapter 14 replays to turn an intermittent
    failure into a deterministic test.
    """
```

"第 14 章会回放它，把一个时有时无的失败变成一个每次都一样的测试。"现在来兑现。

动手之前先**看**——第 0 章的规矩。在第 13 章的代码上真的跑一次，然后打开录制文件。它是 JSONL：一行一个事件，每个事件有 `kind`（哪一种）和 `payload`（内容）。把每一行的要点摘出来（请求只印消息条数，否则一屏放不下）：

```
1 request  messages= 2
2 response {"turn": 0, "text": "", "finish_reason": "tool_calls",
           "tool_calls": [{"id": "call_cXwhTxS9aGwDzAowc7jpCxXJ", "name": "read_file"}], ...}
3 request  messages= 4
4 response {"turn": 1, "text": "", "finish_reason": "tool_calls",
           "tool_calls": [{"id": "call_oKpFObxlScZmHw9WBqCTAaOu", "name": "apply_patch"}], ...}
5 request  messages= 6
6 response {"turn": 2, "text": "I have added the `subtract` function to `calc.py`. ...
```

（实测于 2026-08，`gpt-4o-mini`，任务是"给 calc.py 加一个 subtract 函数"。）

看第 4 行。模型这一轮做的事情是**改文件**，而记下来的只有 `{"id": ..., "name": "apply_patch"}`——**改了什么，一个字都没有**。

要"回放"这一轮，就得让假模型再说一遍"请调用 `apply_patch`，参数是……"。参数是什么？文件里没有。

这不是"记少了一点"。`apply_patch` 的参数就是那次运行做的全部事情；Agent 的 bug 十有八九就在那段参数里（锚点不唯一、缩进不对、改错了文件）。

再看失败是怎么记的。第 12 章遇到服务商报错时，录制里是这样一行：

```json
{"disposition": "fatal", "kind": "auth",
 "detail": "Incorrect API key provided: sk-not-a*****-key. ... [invalid_api_key]"}
```

这是第 12 章的**分类结论**（"这是认证问题，别重试"），不是它据以分类的**证据**。状态码（401）、响应头（比如限流时的 `retry-after: 46`）、响应体，一个都没有。想让"那次限流之后的重试"再发生一次，无从谈起。

两处缺的是同一样东西：**这份记录是写给人读的，不是写给机器重跑的。** 写的时候没有人区分这两件事，因为当时只有一个用途。

### 4.2 新模块 `replay.py`：先写"怎么记"

新建 `src/minicodex/replay.py`。开头是模块说明和导入：

```python
"""Turning one real run back into a deterministic test.

Chapter -1 built the recorder for a human: when an agent misbehaves the first
question is *what did the model actually receive*, and a file on disk is the
only thing that survives the process.  Its docstring has said since then that
"chapter 14 replays it".  It could not.  Two things were missing, and both were
missing for the same reason -- the transcript was written to be **read**, not
to be **re-run**:

    tool call arguments   `{"id": ..., "name": "apply_patch"}` and nothing else.
                          The whole edit -- the payload the bug is usually
                          *in* -- was never written down.
    the failure itself    a 429 was recorded as prose (`kind`, `detail`), not
                          as the status, headers and body a second attempt
                          would have to see.

Both are fixed in `agent.py`, at the two `recorder.record()` calls, and neither
can be fixed for a recording made before this chapter.  A loader that quietly
filled the gap with `{}` would replay a run in which the model asked to patch
nothing, which is worse than refusing: it is a green test for a conversation
that never happened.  So `load()` refuses, and says which field is absent.

What a replay is faithful about, and what it is not:

  * **faithful**: the message list the loop built, every tool call and its
    arguments, the finish reason, the reported prompt tokens, and the failures
    in the order they happened -- including the retry that followed one.
  * **not faithful**: the *chunking*.  The recorder stores the assembled turn,
    so replaying it emits one `TextDelta` where the wire had forty.  A bug in
    fragment assembly (F01-01) cannot be reproduced from a recording; that is
    what `stub.py`'s byte-level recordings are for.  Two artefacts, two
    layers, and it is worth knowing which one answers which question.

The tool side is replayed from the same file without a workspace: request N+1
contains the results of turn N's calls, so `recorded_tools()` reads the outputs
out of the following request.  A replay therefore touches no files, starts no
subprocesses, and needs no API key -- everything between the two boundaries is
exercised, and nothing outside them is.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

from minicodex.agent_types import IncompleteStreamError, ToolCall, ToolFn
from minicodex.model import Completed, ModelHTTPError, StreamEvent, TextDelta, ToolCallDelta, Usage


class ReplayError(RuntimeError):
    """This recording cannot be replayed, and the message says which field."""
```

> - 模块说明比平时长，因为它要回答一个以后一定会有人问的问题：**回放到底保证什么**。§8.4 会逐条讲。
> - `import` 这几行先照抄。有几个名字要到后面几节才用上，这期间 `ruff check` 会报 `F401`（导入了没用）——那是它在正常工作，写到 §7 就都用上了。
> - **`class ReplayError(RuntimeError)`**：自己定义一种异常，只是为了让调用的人能写 `except ReplayError` 单独接住"这份录制没法回放"这一类问题。类体里只有一行说明，不需要别的。

接着是两个很短的函数——一次工具调用该怎么记，一次失败该怎么记：

```python
# ---------------------------------------------------------------------------
# The writing half.  It lives here, next to the reading half, and `agent.py`
# imports it -- which points an arrow from the loop to its own debug tooling
# and is worth one sentence of defence.  The alternative is a dict literal at
# each `recorder.record()` call site and a parser over here, which is exactly
# the arrangement that produced this chapter: two halves of one format, in two
# modules, agreeing until one of them is edited.  Chapter 1 put the wire format
# in one place for the same reason.  `replay.py` imports nothing from `agent`,
# so the arrow is not a cycle and cannot become one.
# ---------------------------------------------------------------------------


def call_record(call: ToolCall) -> dict[str, Any]:
    """One tool call, as the transcript has to store it to be re-runnable.

    `raw_arguments` and not the parsed dict, for chapter 7's reason (F07-09):
    `{"path": "x.py"}` and `{"path":"x.py"}` mean the same thing and are not
    the same bytes, and a replay that re-renders the parsed form produces a
    request the provider never saw.
    """
    return {"id": call.call_id, "name": call.name, "arguments": call.raw_arguments}


def failure_record(exc: BaseException) -> dict[str, Any]:
    """The parts of a failure that a *second attempt* would have seen.

    `Failure` (retry.py) is the classification -- a slug and a sentence for a
    human.  This is the evidence it was made from.  Keeping only the
    classification is the same mistake as keeping only a tool call's name: it
    is enough to read and not enough to re-run, and which of the two you needed
    is decided months later by somebody looking at a bug that happens once a
    week.
    """
    if isinstance(exc, ModelHTTPError):
        return {"status": exc.status, "url": exc.url, "headers": exc.headers, "body": exc.body}
    # Not an HTTP answer at all.  `status: 0` rather than a missing key,
    # because "no status" and "this recording predates chapter 14" have to
    # stay distinguishable -- the loader refuses on the second.
    return {"status": 0}
```

> - **`call_record`**：比原来多一个 `"arguments"`。注意存的是 `call.raw_arguments`——模型发来的**原始字符串**，不是解析之后的字典。`{"path": "x.py"}` 和 `{"path":"x.py"}`（少一个空格）意思一样，字节不一样；回放时如果拿字典重新生成一遍字符串，发出去的请求就和当时差了一个空格。第 7 章为同一件事改过一次会话文件的版本号（F07-09）。
> - **`failure_record`**：`isinstance(exc, ModelHTTPError)` 判断这个异常是不是"服务商回了一个 HTTP 错误"（第 12 章定义的类型，身上带着 `status`、`headers`、`body`）。是，就把这四样原样存下。
> - **不是 HTTP 错误**（比如连接断了）：存 `{"status": 0}`。为什么不干脆什么都不存？因为读的那一头要区分两种情况：**"这次失败没有状态码"**和**"这份录制是第 14 章之前录的，根本没记状态码"**。前一种能回放，后一种不能。少一个键的话，两种情况长得一模一样。
> - **中间那段长注释**在为一个决定辩护：这两个"写"的函数，为什么放在一个叫"回放"的模块里。因为这一章的毛病就出在"一种格式，写的一半在这个文件，读的一半在那个文件，谁也没保证它们对得上"。放在一起，改格式的人一眼能看到两头。

然后让 `agent.py` 用它们。先在导入区加一行：

```python
from minicodex.replay import call_record, failure_record
```

记录成功回复的地方（`run()` 里），把原来那行手写的字典换掉：

```python
self.recorder.record(
    "response",
    {
        "conversation": self.conversation,
        "turn": turn_index,
        "text": turn.text,
        "finish_reason": turn.finish_reason,
        "tool_calls": [call_record(c) for c in turn.tool_calls],
        "estimated_prompt_tokens": estimated,
```

> （后面还有几项，没有变。）`[call_record(c) for c in turn.tool_calls]` 这一行以前是 `[{"id": c.call_id, "name": c.name} for c in turn.tool_calls]`。
> `"conversation": self.conversation` 那一行**先不要加**，它是 §9.4 的事。

记录失败的地方（`_respond()` 里）要动两处。第一处在 `except` 里面，把证据取出来：

```python
try:
    turn = await self._collect(self.model.stream(messages))
except Exception as exc:  # not BaseException: a Ctrl-C is not a failure
    failure = classify(exc)
    # Inside the clause, not after it: Python deletes the name
    # bound by `except ... as` when the block ends, and ruff said
    # so (F821) before any test could.
    evidence = failure_record(exc)
```

第二处在下面记录的地方，把证据摊开放进去：

```python
self.recorder.record(
    "model_failure",
    {
        "conversation": self.conversation,
        "turn": turn_index,
        "attempt": attempt,
        "disposition": failure.disposition,
        "kind": failure.kind,
        "detail": failure.detail,
        "retry_after": failure.retry_after,
        "request_id": failure.request_id,
        # ...and the evidence, not only the verdict.  Chapter 14:
        # a 429 recorded as a sentence is a 429 nobody can make
        # happen again.
        **evidence,
    },
)
```

> - **`**evidence`**：`evidence` 是 `failure_record` 返回的字典。写在另一个字典字面量里、前面加两个星号，意思是"把它的每一对键值都放进来"。于是这条记录在原来那几项后面，多了 `status`、`url`、`headers`、`body`。
> - 同样，`"conversation"` 那一行留给 §9.4。

**为什么 `evidence = failure_record(exc)` 要写在 `except` 里面。** 我第一版是这么写的——在下面记录的地方直接用 `exc`：

```python
            except Exception as exc:
                failure = classify(exc)
            ...
            self.recorder.record("model_failure", {..., **failure_record(exc)})
```

保存，`ruff check`：

```
F821 Undefined name `exc`
   --> src/minicodex/agent.py:526:38
```

"`exc` 这个名字没定义"。明明两行之前刚写过 `as exc`？

这是 Python 一条不太有人知道的规则：**`except ... as exc:` 绑定的那个名字，`except` 块一结束就被删掉了。** 出了那个缩进块，`exc` 就不存在。所以要在块里面把需要的东西取出来，存进另一个变量。

这个错如果没被 ruff 拦住，会是一个运行时的 `NameError`——而且只在服务商真的报错时才触发，也就是在你最需要这条记录的那一刻。没有哪个测试会先于 ruff 发现它。第 -1 章说"静态检查是最便宜的一层"，这是它第四次兑现（前三次：F00-08 忘了 `await`、F00-09 阻塞的读文件、第 12 章阻塞的 `sleep`）。

### 4.3 第三样缺的东西：这次运行是怎么配置的

写到 §8 的命令行时会撞上第三处缺失，提前到这里一起补：回放要重建那条 system 消息，system 消息里有权限说明，权限说明取决于 `--sandbox-mode`——**录制里没有这个信息**。同样：哪个服务商、哪个模型、有哪些工具、上下文窗口多大，一个都没有。

第 7 章早就解决过一模一样的问题。会话文件的头一行 `SessionMeta` 记着目录、模型、权限模式，理由那一章写过：对话内容里**一个字都没提到它们，却处处依赖它们**。那是会话文件。同一个程序的**另一份**记录（录制），七章之后还没有人把这条经验搬过来。

在 `__main__.py` 的 `_ask` 里，创建 `agents_watcher` 之前，加一个事件：

```python
# Written before the first request, not after the run: a recording is worth
# having *because* the process died, and the line saying which model and
# which sandbox mode produced it is the one a replay cannot do without.
# This is F07-07's `SessionMeta` arriving at the other transcript, seven
# chapters late -- the two files record the same run and only one of them
# has ever said what the run was configured as.
recorder.record(
    "config",
    {
        "provider": provider,
        "model": model or default_model,
        "cwd": str(root),
        "sandbox_mode": session.mode,
        "approval_policy": session.policy,
        "context_window": context_window,
        "question": question,
        "tools": sorted(tools.handlers),
        "schemas": tools.schemas,
    },
)
```

> - 写在第一次请求**之前**，不是运行结束之后。一份录制最值钱的时候恰恰是程序中途死掉的时候；等结束再写，那种录制就没有这一行了。
> - `sorted(tools.handlers)`：`tools.handlers` 是"工具名 → 处理函数"的字典，对字典 `sorted` 得到的是排好序的**键**，也就是所有工具的名字。
> - `tools.schemas`：给模型看的工具说明，整份存下。回放时要原样还原"当时模型看到了哪些工具"。

现在再录一次（对着录音服务就行），摘出来看：

```
1 config   {"provider": "ollama", "model": "gemma4:31b-cloud", "sandbox_mode": "read-only",
            "approval_policy": "on-request", "context_window": null,
            "question": "What does src/minicodex/__init__.py define?",
            "tools": ["apply_patch", "read_file", "request_permissions", "run_shell",
                      "spawn_agent", "update_plan"], ...}
2 request  turn=0 attempt=0 messages=2
3 response {"turn": 0, "text": "I will read the contents of the file `src/minicodex/__init__.py`.",
            "finish_reason": "tool_calls",
            "tool_calls": [{"id": "call_rgpykfbt", "name": "read_file",
                            "arguments": "{\"path\":\"src/minicodex/__init__.py\"}"}]}
4 request  turn=1 attempt=0 messages=4
5 response {"turn": 1, "text": "`src/minicodex/__init__.py` defines the following: ...
```

第 3 行现在有 `arguments` 了，第 1 行说清了这是谁、用什么配置跑的。提交：

```bash
git add .
git commit
```

```
feat(replay): record what a re-run needs, not only what a reader needs

A tool call was recorded as {"id", "name"}, so the edit -- the payload a
bug is usually in -- was never written down; a failure was recorded as a
slug and a sentence, not as the status and body a second attempt would
see; and nothing said which model or sandbox mode produced the run.

Both halves of the format live in replay.py, which agent.py now imports.
```

两个测试把这两个函数钉住（放进新文件 `tests/test_faults_ch14.py`，文件开头的导入和几个辅助函数见 §19.1）：

```python
def test_F14_03_call_record_keeps_the_bytes_the_model_sent() -> None:
    """`{"path": "x.py"}` and `{"path":"x.py"}` are the same object and not the
    same request.  Chapter 7 bumped a rollout version over this exact
    distinction (F07-09); a recording has the same obligation."""
    call = ToolCall("c1", "read_file", {"path": "x.py"}, '{"path":"x.py"}')
    assert call_record(call)["arguments"] == '{"path":"x.py"}'


def test_F14_03_failure_record_keeps_the_status_and_the_headers() -> None:
    from minicodex.model import ModelHTTPError

    exc = ModelHTTPError(
        status=429,
        url="https://api.openai.com/v1/chat/completions",
        headers={"retry-after": "46"},
        body=json.dumps(stub.RATE_LIMITED["body"]),
    )
    record = failure_record(exc)
    assert record["status"] == 429
    assert record["headers"]["retry-after"] == "46"
    assert "rate_limit_exceeded" in record["body"]
```

> `ToolCall("c1", "read_file", {"path": "x.py"}, '{"path":"x.py"}')` 的四个参数依次是：调用 id、工具名、**解析后的**参数、**原始的**参数字符串。测试故意让原始字符串里没有空格，然后断言存下来的是它。

---

## §5 F14-03（中）：把录制读回来

### 5.1 读回来之后是什么形状

读之前先定：读出来的东西长什么样。接着 `failure_record` 往下写（`ReplayDrift` 和 `ReplayExhausted` 两个类在文件里排在这之前，§6 才讲，现在先跳过）：

```python
@dataclass(frozen=True)
class RecordedAttempt:
    """One trip to the model boundary: what was sent, and what came back.

    An *attempt*, not a turn, because chapter 12's retry loop can make several
    per turn and the interesting recordings are exactly those: the 429, the
    wait, and the answer that followed.  Replaying a turn would silently drop
    the failure and with it the only reason the recording was kept.
    """

    turn: int
    attempt: int
    request: tuple[dict[str, Any], ...]
    text: str = ""
    tool_calls: tuple[ToolCallDelta, ...] = ()
    finish_reason: str | None = None
    prompt_tokens: int | None = None
    # Present instead of the three fields above when this attempt failed.
    failure: dict[str, Any] | None = None


@dataclass(frozen=True)
class Recording:
    path: Path
    attempts: tuple[RecordedAttempt, ...]
    # What produced the conversation, as opposed to the conversation itself.
    # Chapter 7 learned this once already: `SessionMeta` records cwd, model,
    # sandbox mode and policy "because the history mentions none of them while
    # depending on all of them" (F07-07).  Nothing propagated that to the
    # recorder, so for thirteen chapters the transcript could not say which
    # model wrote it.  `None` for a recording made before this chapter.
    config: dict[str, Any] | None = None
    # A session that compacted cannot be replayed: `make_summariser` calls the
    # model directly and that call never reaches the recorder.  Refused rather
    # than half-served, for the reason `load()` refuses a missing argument.
    compacted: bool = False
    # Requests in the file that belong to some *other* conversation -- a
    # sub-agent's.  They are counted and not loaded: a child's whole run is one
    # `spawn_agent` call to its parent, and that call's result is in the
    # parent's next request like any other tool's (`recorded_tools`).
    elsewhere: int = 0

    @property
    def turns(self) -> int:
        return len({a.turn for a in self.attempts})

    def describe(self) -> str:
        failed = sum(1 for a in self.attempts if a.failure is not None)
        calls = sum(len(a.tool_calls) for a in self.attempts)
        text = (
            f"{self.turns} turn(s), {len(self.attempts)} attempt(s) "
            f"({failed} failed), {calls} tool call(s)"
        )
        if self.elsewhere:
            text += f"; {self.elsewhere} sub-agent request(s) in the file, not replayed"
        return text
```

> - **`RecordedAttempt`——一次"尝试"，不是一"轮"。** 第 12 章之后，一轮里可能找模型好几次：第一次被限流、等一会儿、第二次成功。这是两次尝试、一轮。**最值得留着的录制恰恰是这种**；如果按"轮"来存，那次失败就被悄悄丢掉了，而它是这份录制被留下来的唯一原因。
> - `request` 是当时发出去的消息列表；`text`、`tool_calls`、`finish_reason`、`prompt_tokens` 是成功时拿到的回复；`failure` 是失败时的那份证据。**一次尝试要么有前四样，要么有 `failure`。**
> - `tuple[dict[str, Any], ...]` 读作"一个元组，里面是任意多个字典"。用元组而不是列表，是因为数据类写了 `frozen=True`——"发生过的事不许改"，第 0 章 §5.2 的理由。
> - **`Recording`**：整份录制。`config` 是 §4.3 那个事件的内容；老录制没有，所以允许是 `None`。
> - **`compacted`**：这次会话中途压缩过没有。压缩时"让模型写摘要"的那次请求**从来没经过录制器**（第 6 章是直接调的模型），所以压缩过的会话没法原样回放——§8 会拒绝它，而不是回放一半。
> - **`elsewhere`** 和 `describe` 里最后那个 `if`：留给 §9.4。
> - **`@property`**：写在方法上面，这个方法用起来就像一个字段——`recording.turns`，不用加括号。`{a.turn for a in self.attempts}` 是集合推导：把所有尝试的轮号放进一个集合（重复的自动合并），`len` 就是"一共几轮"。

### 5.2 `load`：把一行行事件配成一次次尝试

```python
def load(path: Path | str) -> Recording:
    """Read a transcript, or refuse and say what is missing.

    Events arrive interleaved -- `request`, then either `response` or
    `model_failure`, plus `compaction` and `nudge` lines this does not care
    about -- so the pairing is done on `(turn, attempt)` rather than on
    adjacency.  A `compaction` event between a request and its response is
    normal and must not break the pairing; adjacency would have made it.

    `(turn, attempt)` is only a key inside **one conversation**, and a file can
    hold several: a sub-agent writes to its parent's recorder and counts its
    own turns from zero.  The first version did not know that.  Given a session
    in which the parent spawned one child, it kept whichever `request` for
    "turn 0 attempt 0" came last and returned this, without a word:

        2 turn(s), 2 attempt(s) (0 failed), 1 tool call(s)

    -- the child's first turn followed by the parent's second, a conversation
    nobody had.  So the conversation that made the first request is the one
    that is loaded, the others are counted, and a file that repeats a key
    *without* saying whose it is (recorded before the `conversation` field
    existed) is refused for the reason a missing argument is.
    """
    path = Path(path)
    requests: dict[tuple[int, int], tuple[dict[str, Any], ...]] = {}
    outcomes: dict[tuple[int, int], tuple[str, dict[str, Any]]] = {}
    config: dict[str, Any] | None = None
    compacted = False
    top: Any = None
    elsewhere = 0
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            # A recording is written by a process that may be killed; the
            # surviving prefix is still a recording.  Chapter 7's rule.
            break
        kind, payload = event.get("kind"), event.get("payload", {})
        if kind == "config":
            config = payload
        elif kind == "compaction":
            compacted = True
        elif kind == "request":
            if not requests:
                top = payload.get("conversation")
            if payload.get("conversation") != top:
                elsewhere += 1
                continue
            key = (payload["turn"], payload["attempt"])
            if key in requests:
                raise ReplayError(
                    f"{path}: two requests were recorded for turn {key[0]} attempt "
                    f"{key[1]} and nothing says whose they are. A sub-agent shares "
                    "its parent's recorder, and recordings made before the "
                    "`conversation` field cannot tell the two apart."
                )
            requests[key] = tuple(payload["messages"])
        elif kind in ("response", "model_failure"):
            if payload.get("conversation") != top:
                continue
            # A `response` carries no `attempt`: it is written once the attempt
            # that worked has already returned, so it belongs to the last
            # request issued for its turn.
            attempt = payload.get("attempt")
            if attempt is None:
                attempt = max(
                    (a for (t, a) in requests if t == payload["turn"]),
                    default=0,
                )
            # Two levels, two dicts.  The first version flattened them with
            # `{"kind": kind, **payload}` -- and a `model_failure` payload has
            # its own `kind` (the classification slug, "rate_limit"), which
            # overwrote the envelope's.  Every recorded failure then read as a
            # recorded *response* with no text and no calls: no error anywhere,
            # a replay that quietly turns a 429 into a blank answer.
            outcomes[(payload["turn"], attempt)] = (kind, payload)

    attempts: list[RecordedAttempt] = []
    for key in sorted(requests):
        found = outcomes.get(key)
        if found is None:
            # The process died between sending and hearing back.  That is a
            # real recording of a real event, and there is nothing to replay
            # after it, so it ends the replay rather than failing it.
            break
        event, outcome = found
        turn, attempt = key
        if event == "model_failure":
            attempts.append(
                RecordedAttempt(turn, attempt, requests[key], failure=_failure(outcome, path))
            )
            continue
        attempts.append(
            RecordedAttempt(
                turn,
                attempt,
                requests[key],
                text=outcome.get("text", ""),
                tool_calls=_calls(outcome, path),
                finish_reason=outcome.get("finish_reason"),
                prompt_tokens=outcome.get("actual_prompt_tokens"),
            )
        )
    if not attempts:
        raise ReplayError(f"{path}: no complete request/response pair in this recording")
    return Recording(path, tuple(attempts), config=config, compacted=compacted, elsewhere=elsewhere)


def _calls(payload: dict[str, Any], path: Path) -> tuple[ToolCallDelta, ...]:
    calls = []
    for index, call in enumerate(payload.get("tool_calls", [])):
        if "arguments" not in call:
            raise ReplayError(
                f"{path}: tool call {call.get('name')!r} was recorded without its "
                "arguments, so this run cannot be replayed. Recordings made before "
                "chapter 14 stored only the id and the name."
            )
        calls.append(
            ToolCallDelta(
                call_id=call["id"],
                index=index,
                name=call["name"],
                arguments=call["arguments"],
            )
        )
    return tuple(calls)


def _failure(payload: dict[str, Any], path: Path) -> dict[str, Any]:
    if "status" not in payload:
        raise ReplayError(
            f"{path}: a {payload.get('kind')} failure was recorded as prose only "
            f"('{payload.get('detail', '')[:60]}...'), without the status and body "
            "a second attempt would see. Recordings made before chapter 14 cannot "
            "replay a failure."
        )
    return payload
```

这是本章最长的一个函数，分四段看。

**第一段：逐行读。**

> - `path.read_text(...).splitlines()`：整个文件读进来，按行切开。
> - `json.loads(line)` 把一行 JSON 变成字典。如果这一行不是完整的 JSON（程序写到一半被杀掉了），`json.JSONDecodeError` → `break`：**到此为止，前面读到的照样算数**。第 7 章读会话文件用的同一条规矩。
> - `kind, payload = event.get("kind"), event.get("payload", {})`：一行里同时给两个变量赋值。

**第二段：按 `(turn, attempt)` 存。**

> - `requests` 和 `outcomes` 是两个字典，键都是 `(轮号, 第几次尝试)` 这样一个**元组**。请求存一个，结果（回复或失败）存一个，最后按相同的键配对。
> - **为什么不直接"上一行是请求，下一行就是它的回复"？** 因为中间可能夹着别的事件：压缩（`compaction`）、提醒（`nudge`）。按相邻来配，夹一行就全错位了。
> - **`response` 事件里没有 `attempt`**（第 12 章写的时候没放）。它一定属于"这一轮里最后发出的那次请求"，所以用 `max(...)` 找这一轮已有的最大尝试号。`(a for (t, a) in requests if t == payload["turn"])` 是一个生成器：遍历 `requests` 的键，把键拆成 `t` 和 `a`，只要轮号对得上的那些 `a`。`default=0` 是"一个都没有时用 0"。
> - 带 `conversation` 和 `elsewhere` 的那几行，以及 `if key in requests:` 那个 `raise`：§9.4。

**第三段：配对。**

> - `for key in sorted(requests)`：元组排序是先比第一项再比第二项，所以正好是"第 0 轮第 0 次、第 0 轮第 1 次、第 1 轮第 0 次……"的顺序。
> - `found is None`——有请求，没结果：程序在"发出去"和"收到"之间死了。这是一条真实的记录，它后面没有东西可回放了，所以**结束**而不是报错。
> - `event, outcome = found`：把存进去的那个二元组拆开。

**第四段：两个"缺了就拒绝"的小函数。**

> - **`_calls`**：把录制里的工具调用变成 `ToolCallDelta`（第 0 章定义的"模型要调一个工具"事件）。`enumerate(...)` 一边遍历一边给出序号 `index`。
> - **`if "arguments" not in call: raise ReplayError(...)`**——这是整章最重要的一个决定。

**缺参数的老录制怎么办？** 诱人的做法是"没有就当它是 `{}`"。不能这么做。填了 `{}` 之后，回放出来的是一次"模型请求把**空的**修改应用到文件上"的对话——**那次对话从来没有发生过**，而基于它的测试会是绿的。一个绿的、关于一件没发生过的事的测试，比没有测试更坏。

所以拒绝，并且说清楚缺的是什么：

```
$ uv run python probe_eval.py replay
a recording in the format chapters -1 to 13 wrote:
  [{"id": "call_1", "name": "apply_patch"}]
  load() -> .../.probe/ch14/old.jsonl: tool call 'apply_patch' was recorded without its
  arguments, so this run cannot be replayed. Recordings made before chapter 14 stored
  only the id and the name.

the same recording with chapter 14's one extra field:
  load() -> 1 turn(s), 1 attempt(s) (0 failed), 1 tool call(s)
  first call -> ToolCallDelta(call_id='call_1', index=0, name='apply_patch',
                              arguments='{"edits":[{"path":"a.py"}]}')
```

`_failure` 同理：失败记录里没有 `status`，就是老格式，拒绝。

### 5.3 第一版里的一个静默 bug：两个 `kind`

`load` 里有一段注释比代码还长：

```python
            # Two levels, two dicts.  The first version flattened them with
            # `{"kind": kind, **payload}` -- ...
            outcomes[(payload["turn"], attempt)] = (kind, payload)
```

第一版我想省事，把"事件种类"和"事件内容"拍平进一个字典：

```python
outcomes[(payload["turn"], attempt)] = {"kind": kind, **payload}
```

这里的 `kind` 是事件的种类（`"response"` 或 `"model_failure"`）。问题是：`model_failure` 的 `payload` 里**自己也有一个 `kind`**——第 12 章的分类结果（比如 `"rate_limit"`）。§0.2 的表里说过，字典摊开时**后写的覆盖先写的**，于是 `**payload` 把前面那个 `"kind": kind` 盖掉了。

后果：每一条录下来的失败，读回来之后种类都不再是 `"model_failure"`，下面 `if event == "model_failure"` 不成立，它被当成一条"没有文字、没有工具调用的正常回复"。**没有任何报错。** 一次"限流，然后重试成功"的录制，回放出来是"模型说了句空话，然后又说了句空话"。

它是怎么被发现的：我为这两种情况写的两个测试（下面第二、第四个）双双失败。修法是不拍平——存一个二元组 `(kind, payload)`，两层各归各。

```python
def test_F14_03_a_recording_without_arguments_is_refused_not_guessed(tmp_path: Path) -> None:
    """Chapters -1 to 13 recorded `{"id": ..., "name": "apply_patch"}`.

    Filling the gap with `{}` would replay a conversation in which the model
    asked to patch nothing, and the test built on it would be green about a run
    that never happened.
    """
    path = _recording(
        tmp_path / "old.jsonl",
        _event("request", {"turn": 0, "attempt": 0, "messages": []}),
        _event(
            "response",
            {"turn": 0, "text": "", "tool_calls": [{"id": "c1", "name": "apply_patch"}]},
        ),
    )
    with pytest.raises(ReplayError, match="without its arguments"):
        load(path)


def test_F14_03_a_failure_recorded_as_prose_is_refused(tmp_path: Path) -> None:
    """A 429 stored as `kind` and `detail` cannot be made to happen again."""
    path = _recording(
        tmp_path / "old.jsonl",
        _event("request", {"turn": 0, "attempt": 0, "messages": []}),
        _event(
            "model_failure",
            {
                "turn": 0,
                "attempt": 0,
                "disposition": "retry",
                "kind": "rate_limit",
                "detail": "Rate limit reached for gpt-4o-mini ...",
            },
        ),
    )
    with pytest.raises(ReplayError, match="recorded as prose only"):
        load(path)
```

> - `_recording(path, *events)` 和 `_event(kind, payload)` 是文件开头的两个辅助函数（§19.1）：前者把几行事件写成一个录制文件，后者生成一行事件。测试里要的录制都是这样**手写**出来的，几行就够。
> - **`with pytest.raises(ReplayError, match="without its arguments"):`**——"下面这段代码必须抛出 `ReplayError`，而且消息里要有这几个词"。不抛，或者抛了别的，测试就失败。

这一节值得记一句：这个 bug 是**在写测试的时候**发现的，不是在写实现的时候。"先写实现再写测试"在这里的实际含义是：测试是第一个认真读实现的读者。

---

## §6 F14-03（下）：一个照着录制说话的模型

### 6.1 先解决"哪里不一样"：`divergence`

假模型要做两件事：把当时的回复再说一遍；顺便检查程序这次发来的请求和当时是不是一样。后一件需要一个函数来回答"两个消息列表，第一处不同在哪"。

最省事的写法是 `assert sent == recorded`。不行：两个各有几千字的消息列表不相等时，Python 会把两边完整打印出来，你得自己用眼睛找不同。一份失败的快照，有用的输出是**"哪里"**。

```python
def divergence(sent: Sequence[dict[str, Any]], recorded: Sequence[dict[str, Any]]) -> str | None:
    """The first place two message lists stop agreeing, in one line.

    Not `assert sent == recorded`: the useful output of a failing snapshot is
    *where*, and a full dump of two 6000-token message lists is not that.  The
    fields are compared one at a time so the report can name the one that
    moved -- a role, a missing message, or a body that changed.
    """
    for index, (a, b) in enumerate(zip(sent, recorded, strict=False)):
        if a.get("role") != b.get("role"):
            return f"message {index}: role was {b.get('role')!r}, now {a.get('role')!r}"
        if a != b:
            for key in sorted(set(a) | set(b)):
                if a.get(key) != b.get(key):
                    was, now = _window(b.get(key), a.get(key))
                    return (
                        f"message {index} ({a.get('role')}): {key} changed\n"
                        f"      was: {was}\n"
                        f"      now: {now}"
                    )
    if len(sent) != len(recorded):
        counts = f"{len(recorded)} messages recorded, {len(sent)} sent"
        if len(sent) < len(recorded):
            return f"{counts}; missing {recorded[len(sent)].get('role')!r}"
        return f"{counts}; extra {sent[len(recorded)].get('role')!r}"
    return None


def _short(value: Any) -> str:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    text = text.replace("\n", "\\n")
    return text if len(text) <= 90 else text[:87] + "..."


def _window(was: Any, now: Any, *, before: int = 20, width: int = 70) -> tuple[str, str]:
    """Two excerpts starting where the two values first disagree.

    The first version of this printed the first 90 characters of each, which on
    the first real drift -- one sentence appended to a system prompt -- printed
    the *same* 90 characters twice under the labels `was` and `now`.  A diff
    that shows the part that did not change is not a diff.
    """
    a = (was if isinstance(was, str) else json.dumps(was, ensure_ascii=False)).replace("\n", "\\n")
    b = (now if isinstance(now, str) else json.dumps(now, ensure_ascii=False)).replace("\n", "\\n")
    at = next((i for i in range(min(len(a), len(b))) if a[i] != b[i]), min(len(a), len(b)))
    start = max(0, at - before)
    lead = "..." if start else ""
    return (
        lead + a[start : start + width] + ("..." if start + width < len(a) else ""),
        lead + b[start : start + width] + ("..." if start + width < len(b) else ""),
    )
```

> - **`divergence`** 返回一个字符串（哪里不同）或者 `None`（完全相同）。`str | None` 就是这个意思。
> - `zip(sent, recorded, strict=False)`：两个列表并排走，一对一对地比；一个比另一个长也不报错（长度不同由最后那个 `if len(sent) != len(recorded)` 单独报）。
> - 先比 `role`：角色都不一样（比如该是 `system` 的地方成了 `user`），说明整条消息多了或少了，不用再往里看。
> - `sorted(set(a) | set(b))`：对字典取 `set` 得到它的键的集合；`|` 是并集——两边出现过的所有字段名。逐个字段比，找到第一个不同的。
> - **`_window`**：从"第一个不同的字符"往前退 20 个字符开始，截 70 个字符。`next((i for i in range(...) if a[i] != b[i]), 默认值)` 就是在找第一个不同的位置；两段文字一段是另一段的开头时找不到，就用较短那段的长度。

`_window` 的说明里记着它的第一版。第一版是"两边各截前 90 个字符"（现在的 `_short` 就是那个逻辑，留给别处用）。第一次真的漂移——往一段四百字的系统提示词后面加一句话——它打出来的是：

```
      was: You are a coding agent working in a user's repository.\n\nIf a request is genuinely amb...
      now: You are a coding agent working in a user's repository.\n\nIf a request is genuinely amb...
```

**一模一样的两行。** 一个只展示"没变的那部分"的 diff 不是 diff。

```python
def test_F14_03_the_drift_report_shows_the_part_that_differs() -> None:
    """The first version printed the first 90 characters of each side.

    On the first real drift -- a sentence appended to a 400-character system
    prompt -- that printed the *same* 90 characters twice, under the labels
    `was` and `now`.  A diff that shows the identical part is not a diff.
    """
    shared = "You are a coding agent working in a user's repository. " * 3
    report = divergence(
        [{"role": "system", "content": shared + "Always answer in haiku."}],
        [{"role": "system", "content": shared + "Follow the conventions."}],
    )
    assert report is not None
    was, now = [line.strip() for line in report.splitlines()[1:]]
    assert "haiku" in now
    assert "conventions" in was
    assert was != now


def test_F14_03_divergence_reports_a_missing_message_rather_than_comparing_pairs() -> None:
    sent = [{"role": "user", "content": "hi"}]
    recorded = [{"role": "system", "content": "S"}, {"role": "user", "content": "hi"}]
    assert "role was 'system'" in (divergence(sent, recorded) or "")
    assert "missing 'user'" in (divergence(recorded[:1], recorded) or "")
```

### 6.2 `RecordedModel`

回到 `failure_record` 下面，先补上之前跳过的两个异常类：

```python
class ReplayDrift(BaseException):
    """The code under test no longer sends what it sent when this was recorded.

    A `BaseException`, which looks like an over-reaction and is not.  The first
    version subclassed `AssertionError`, and the first real drift came out of
    the CLI as this:

        minicodex.retry.ModelFailed: the model call failed: ReplayDrift: ...

    `_respond` catches `Exception` and hands whatever it caught to `classify()`
    -- that is the net that turns every provider failure into a retry decision,
    and a test double's assertion went into it like everything else.  Chapter
    12's conservative default (an unrecognised failure is `fatal`) is the only
    reason this stopped instead of being retried five times.

    This is chapter 7's F07-04 shape for the third time: a broad `except` is
    the right tool for failures and the wrong tool for signals, and the fix is
    on the signal's side.  A double's assertion has to be louder than the code
    it is testing.
    """


class ReplayExhausted(ReplayError):
    """The loop asked for one more turn than the recording has."""
```

`ReplayDrift` 继承的是 `BaseException` 而不是 `Exception`，原因在 §6.3。然后是模型本身，写在 `_window` 下面：

```python
class RecordedModel:
    """A `Model` that answers exactly what one real provider answered once.

    `strict=True` (the default) also makes it a snapshot: every request is
    compared with the one that was recorded, and the first difference raises
    `ReplayDrift`.  That is the same artefact doing two jobs, and it is the
    reason this is worth building at all -- a hand-written golden transcript
    covers the wiring somebody remembered to write a fixture for, and a
    recording covers whatever the program actually did last Tuesday.
    """

    def __init__(self, recording: Recording, *, strict: bool = True) -> None:
        self.recording = recording
        self.strict = strict
        self.sent: list[list[dict[str, Any]]] = []
        self._next = 0
        # `Agent._sizer` reads this off whatever object it was handed, so a
        # replay sizes its requests the way the real client did.
        self.tools: list[dict[str, Any]] = []

    @property
    def unused(self) -> tuple[RecordedAttempt, ...]:
        """Recorded attempts the loop never asked for.

        Every request matching is half of "same as recorded"; the other half is
        that the run went on for as long.  Put chapter 0's first bug back --
        stop as soon as a reply has text (F00-02) -- and a two-turn recording
        replays as one matching request and then silence.  The first version
        of `minicodex replay` printed `ok ... same as recorded` for that run.
        """
        return self.recording.attempts[self._next :]

    async def stream(self, messages: Sequence[dict[str, Any]]) -> AsyncIterator[StreamEvent]:
        if self._next >= len(self.recording.attempts):
            raise ReplayExhausted(
                f"{self.recording.path}: the loop asked for attempt "
                f"{self._next + 1}; the recording has {len(self.recording.attempts)}"
            )
        step = self.recording.attempts[self._next]
        self._next += 1
        self.sent.append([dict(m) for m in messages])

        if self.strict:
            difference = divergence(messages, step.request)
            if difference is not None:
                raise ReplayDrift(
                    f"turn {step.turn} attempt {step.attempt}: the request is no "
                    f"longer what was recorded in {self.recording.path.name}\n"
                    f"      {difference}"
                )

        if step.failure is not None:
            raise _raise_from(step.failure)

        if step.text:
            yield TextDelta(step.text)
        for call in step.tool_calls:
            yield call
        if step.prompt_tokens is not None:
            # completion_tokens is not recorded and nothing reads it: chapter 6
            # calibrates on the prompt side, which is the side the estimator
            # estimates.  Zero rather than a guess.
            yield Usage(prompt_tokens=step.prompt_tokens, completion_tokens=0)
        yield Completed(step.finish_reason)


def _raise_from(failure: dict[str, Any]) -> BaseException:
    status = failure.get("status")
    if status == 0:
        # Not an HTTP failure.  Two shapes have ever reached `classify` from
        # below: a stream that stopped before `[DONE]`, and a transport error.
        if failure.get("kind") == "incomplete_stream":
            return IncompleteStreamError(failure.get("detail", ""))
        return httpx.TransportError(failure.get("detail", ""))
    return ModelHTTPError(
        status=int(status or 500),
        url=failure.get("url", "replay://recorded"),
        headers=failure.get("headers") or {},
        body=failure.get("body", ""),
    )
```

> - **它凭什么能当模型用？** 第 0 章定过：循环对"模型"只有一个要求——有一个 `stream(messages)` 方法，会一个接一个地交出事件。`RecordedModel` 有，所以循环分不出它和真的客户端。
> - `self._next`：下一次该用第几条尝试。每被调用一次就往前走一格。
> - `self.sent`：把这次程序发来的每一份消息列表都存下，事后可以数、可以看。`[dict(m) for m in messages]` 给每条消息做一个浅拷贝，免得之后别处改了原件。
> - **`unused`**：§9.1。
> - **`strict`（默认开）**：发来的请求和录制里那条不一样，就抛 `ReplayDrift`。**同一个东西同时干两件事**——它是一个确定性的模型（"偶尔失败"变成"每次都失败"），也是一份请求快照（"上下文悄悄变了"变成一次红）。
> - **`if step.failure is not None: raise _raise_from(step.failure)`**：这一次尝试当时是失败的，那就再失败一遍——把存下来的状态码、响应头、响应体重新装成一个 `ModelHTTPError` 抛出去。循环会像当时一样去分类、去决定要不要重试。
> - 后面几个 `yield`：把当时的文字、工具调用、token 用量、结束信号依次交出去。`yield` 让这个方法成为生成器（第 0 章 §0.2）。
> - **`_raise_from`** 返回而不是抛出异常，所以调用处写的是 `raise _raise_from(...)`——这样读代码的人和类型检查器都看得出"这一行之后不会往下走"。`status == 0` 是 §4.2 说的"不是 HTTP 错误"的两种情况。

**回放忠实到什么程度，一定要知道。** 录制器存的是**拼好的**一整轮，所以回放时一句话是作为**一个** `TextDelta` 交出来的，而当时在网络上它是四十个碎片。第 1 章那类"碎片拼错了"的 bug，**从录制里复现不出来**——那是 `stub.py` 里逐字节录音的职责。两种录下来的东西，管两个层次；知道哪个回答哪个问题，比两个都有更重要。

### 6.3 我自己的断言，被被测代码的网兜住了

`ReplayDrift` 第一版继承的是 `AssertionError`——一次失败的快照嘛，让 pytest 用大家认识的样子打印。第一次真的漂移，命令行吐出来的是：

```
minicodex.retry.ModelFailed: the model call failed: ReplayDrift: turn 0 attempt 0:
the request is no longer what was recorded in session-1786785289.jsonl
      message 0 (system): content changed
```

`ModelFailed`？那是第 12 章"模型调用失败"的异常。

回头看 §4.2 贴过的那段 `_respond`：

```python
            try:
                turn = await self._collect(self.model.stream(messages))
            except Exception as exc:  # not BaseException: a Ctrl-C is not a failure
                failure = classify(exc)
```

`except Exception`——第 12 章用它把**每一种**服务商失败都接住，变成一个"重试还是放弃"的决定。我的假模型是在 `self.model.stream(...)` 里面抛出 `ReplayDrift` 的，而 `AssertionError` 也是 `Exception` 的一种，于是**它也被接住了**，交给 `classify()`，被判成"不认识的失败 → 不重试"，包成 `ModelFailed` 再抛出来。

还要注意：它**没有被重试四次**，唯一的原因是第 12 章那条保守的默认（认不出来的失败一律放弃）。如果当时的默认是"重试"，一次漂移会变成四次等待加一次超时。

修法在信号这一边：让 `ReplayDrift` 继承 `BaseException`。`except Exception` 抓不到它，它就能一路穿出去。

这和第 7 章的 F07-04 是同一件事：取消信号 `CancelledError` 是 `BaseException`，所以 `except Exception` 抓不到它，所以 Ctrl-C 能穿过那些"把失败变成结果"的函数。那一次是"信号本来就该穿过去，幸好它是 `BaseException`"；这一次方向反过来——**我的信号穿不过去，所以要把它改成 `BaseException`**。

> **一条可以带走的规则：测试替身发出的"不对"，必须比被测代码的 `except` 更响。**

`ReplayExhausted`（程序要的轮数比录制里多）则**故意留在 `Exception` 这一边**：它会被那张网接住、包成 `ModelFailed`，这正合适——录制用完了，是这次请求答不上来，和服务商答不上来是同一种事。

```python
def test_F14_03_drift_is_not_swallowed_by_the_loops_failure_net() -> None:
    """`ReplayDrift` is a `BaseException` and the reason is measured.

    As an `AssertionError` it went straight into `_respond`'s `except
    Exception` -- the net that turns provider failures into retry decisions --
    and came out as `ModelFailed: the model call failed: ReplayDrift: ...`.
    Chapter 12's conservative default (unrecognised means fatal) is the only
    reason it stopped rather than retrying four times.  Chapter 7 learned the
    same thing about `CancelledError`.
    """
    assert not issubclass(ReplayDrift, Exception)
    assert issubclass(ReplayDrift, BaseException)


async def test_F14_03_drift_names_the_message_that_changed(tmp_path: Path) -> None:
    """One sentence appended to the system prompt, reported as one line."""
    path = _recording(
        tmp_path / "r.jsonl",
        _event("config", {"question": "hi", "tools": [], "schemas": []}),
        _event(
            "request", {"turn": 0, "attempt": 0, "messages": [{"role": "system", "content": "A"}]}
        ),
        _event("response", {"turn": 0, "text": "ok", "finish_reason": "stop", "tool_calls": []}),
    )
    model = RecordedModel(load(path))
    agent = Wiring().agent(model, ToolSet(handlers={}, schemas=[]), instructions="A and B")

    with pytest.raises(ReplayDrift) as caught:
        await agent.run("hi")
    assert "message 0 (system): content changed" in str(caught.value)


async def test_F14_03_a_recorded_rate_limit_replays_as_a_rate_limit(tmp_path: Path) -> None:
    """The point of recording the evidence: an intermittent failure becomes a
    test that fails the same way every time.

    Two attempts for turn 0 -- a 429 and then the answer -- and the loop is
    required to do what it did on the day: classify, wait, retry, finish.
    """
    messages = [{"role": "user", "content": "hi"}]
    path = _recording(
        tmp_path / "r.jsonl",
        _event("request", {"turn": 0, "attempt": 0, "messages": messages}),
        _event(
            "model_failure",
            {
                "turn": 0,
                "attempt": 0,
                "disposition": "retry",
                "kind": "rate_limit",
                "detail": "rate limited",
                "status": 429,
                "headers": {},
                "body": json.dumps(stub.RATE_LIMITED["body"]),
            },
        ),
        _event("request", {"turn": 0, "attempt": 1, "messages": messages}),
        _event(
            "response",
            {"turn": 0, "text": "done", "finish_reason": "stop", "tool_calls": []},
        ),
    )
    recording = load(path)
    assert len(recording.attempts) == 2

    model = RecordedModel(recording, strict=False)
    agent = Wiring(retry_policy=RetryPolicy(base=0.0)).agent(
        model, ToolSet(handlers={}, schemas=[])
    )
    result = await agent.run("hi")
    assert result.final_text == "done"
    assert len(model.sent) == 2


async def test_F14_03_asking_for_one_more_turn_than_was_recorded_says_so(tmp_path: Path) -> None:
    path = _recording(
        tmp_path / "r.jsonl",
        _event(
            "request",
            {"turn": 0, "attempt": 0, "messages": [{"role": "user", "content": "x"}]},
        ),
        _event(
            "response",
            {
                "turn": 0,
                "text": "",
                "finish_reason": "tool_calls",
                "tool_calls": [{"id": "c1", "name": "noop", "arguments": "{}"}],
            },
        ),
    )
    model = RecordedModel(load(path), strict=False)

    async def noop(_: dict[str, Any]) -> str:
        return "ok"

    tools = ToolSet(
        handlers={"noop": noop},
        schemas=[{"type": "function", "function": {"name": "noop", "parameters": {}}}],
    )
    agent = Wiring().agent(model, tools)
    # It arrives wrapped: `ReplayExhausted` is an ordinary `Exception`, so the
    # loop classifies it (`fatal`, unrecognised) and raises `ModelFailed`.
    # That is the right treatment for this one -- running out of recording is
    # a fact about the request, unlike drift, which is a fact about the code.
    with pytest.raises(ModelFailed) as failure:
        await agent.run("x")
    assert "the recording has 1" in str(failure.value)
```

> - `issubclass(A, B)`：`A` 是不是 `B` 的子类。第一个测试断言的就是 §6.3 的结论。
> - 第二个测试：录制里 system 消息是 `"A"`，现在用 `instructions="A and B"` 造 Agent，请求里的 system 消息就不一样了，必须抛 `ReplayDrift`，而且消息里要点名"第 0 条消息（system）的 content 变了"。
> - 第三个测试是"把时有时无的失败变成每次都一样"的实物：第 0 轮先一次 429（限流），再一次成功。循环必须像当天那样分类、重试、答完。`RetryPolicy(base=0.0)` 让重试之间不等待；`strict=False` 是因为这里只关心重试，不关心请求比对。
> - `Wiring(...).agent(model, tools)`：插曲 B 起，造 Agent 都走 `Wiring`。`ToolSet(handlers={}, schemas=[])` 是"一个工具都没有"。

---

## §7 意外的红利：工具那一侧不用重建

写到这里，看起来最难的部分还在后面：回放模型是容易的，**回放工具**得有一个和当时一模一样的文件系统——当时 `read_file` 读到的内容，现在那个文件可能早就改了。

不需要。回头看 §4.3 摘出来的录制，第 4 行：

```
4 request  turn=1 attempt=0 messages=4
```

四条消息是：system、user、assistant（带着工具调用）、**tool（带着工具的结果）**。

第 0 章 §0.1 说过：模型不记事，每次请求都要把到目前为止的全部对话重发一遍。所以**第 N+1 次请求里，装着第 N 轮每个工具的返回值**。一次运行里所有工具的输出，早就在录制里了，只是它们长在"下一条请求"里。

```python
@dataclass
class ReplayedTools:
    """The tool table as the recording saw it: same calls in, same text out.

    Outputs are recovered from the *next* request, which is where a tool result
    ends up one moment after it is produced.  Keyed by `(name, arguments)` and
    consumed in order, so a run that reads the same file twice replays both
    reads and a run that reads two different files cannot get them the wrong
    way round.

    A call the recording has no output for is not an error here -- it returns
    a message saying so, because a tool that raises would end the run inside
    the machinery under test rather than at the boundary where the gap is.

    Known bound, raised in review: consuming a queue in order assumes the
    *execution* order of two identical calls is the order they were recorded
    in.  Chapter 8's scheduler can put two calls in different batches, so that
    assumption is not free.  It is left as an assumption rather than defended,
    because the failure is loud: a call would receive the next call's output,
    and `strict` mode reports the divergence on the very next request.
    """

    outputs: dict[tuple[str, str], list[str]] = field(default_factory=dict)
    missed: list[str] = field(default_factory=list)

    def handler(self, name: str) -> ToolFn:
        async def run(arguments: dict[str, Any]) -> str:
            key = (name, json.dumps(arguments, sort_keys=True, ensure_ascii=False))
            queue = self.outputs.get(key)
            if queue:
                return queue.pop(0)
            self.missed.append(f"{name}({_short(arguments)})")
            return f"Error: this call was not in the recording ({name})."

        return run

    def table(self, names: Iterable[str]) -> dict[str, ToolFn]:
        return {name: self.handler(name) for name in names}


def recorded_tools(recording: Recording) -> ReplayedTools:
    """Rebuild the tool side of a run out of the requests that followed it."""
    by_id: dict[str, str] = {}
    calls: dict[str, tuple[str, str]] = {}
    for step in recording.attempts:
        for message in step.request:
            if message.get("role") == "assistant":
                for call in message.get("tool_calls") or []:
                    function = call.get("function", {})
                    arguments = function.get("arguments", "")
                    try:
                        parsed = json.loads(arguments) if arguments else {}
                    except json.JSONDecodeError:
                        parsed = {}
                    calls[call.get("id", "")] = (
                        function.get("name", ""),
                        json.dumps(parsed, sort_keys=True, ensure_ascii=False),
                    )
            elif message.get("role") == "tool":
                by_id[message.get("tool_call_id", "")] = message.get("content", "")

    replayed = ReplayedTools()
    for call_id, key in calls.items():
        if call_id in by_id:
            replayed.outputs.setdefault(key, []).append(by_id[call_id])
    return replayed
```

先看下面的 `recorded_tools`：

> - 遍历每一次尝试的请求里的每一条消息。`role` 是 `"assistant"` 的，把它带的每个工具调用记进 `calls`：`调用 id → (工具名, 参数)`。`role` 是 `"tool"` 的，记进 `by_id`：`调用 id → 结果文字`。
> - `message.get("tool_calls") or []`：没有这个字段，或者它是 `None`，都当成空列表。
> - 参数先 `json.loads` 解析、再 `json.dumps(parsed, sort_keys=True)` 重新生成。这是在做**规范化**：`{"a":1,"b":2}` 和 `{"b": 2, "a": 1}` 是同一组参数，排好键的顺序再比，才认得出来。（这里和 §4.2 相反——那边要保留原始字节，因为要原样发回去；这边只是拿来当查找的键。）
> - 最后把两张表按调用 id 接起来：`(工具名, 参数) → [结果, 结果, ...]`。**`setdefault(key, []).append(...)`**：这个键第一次出现就先放一个空列表，再往里加。同一个调用出现两次（同一个文件读了两遍），列表里就有两个结果，按先后排着。

再看 `ReplayedTools`：

> - `@dataclass` 没写 `frozen=True`，因为它要被消耗：每回答一次，对应的列表就短一个。
> - **`handler(name)` 返回一个函数。** 第 13 章 §0.2 讲过"函数里定义函数再返回出去"：里面那个 `run` 记得外面的 `name`。循环眼里它和真的工具处理函数没有区别——收一个参数字典，返回一段文字。
> - `queue.pop(0)`：取走最早的那个结果。读同一个文件两次，就按顺序各拿各的。
> - **录制里没有这个调用怎么办？** 不抛异常，而是返回一句 `Error: this call was not in the recording`，并记进 `missed`。原因：工具一抛异常，运行就断在"循环处理工具"的那段代码里；而问题其实出在边界上——程序这次要了一个当时没要过的调用。让它继续往下走，下一条请求立刻会和录制对不上，`strict` 会准确报出来。
> - `table(names)`：给一串工具名，造出"名字 → 处理函数"的整张表。
> - 说明里最后一段是 §20 自我审查时补的一个**已知边界**：按顺序取用，假设了"两个一模一样的调用，执行的先后和当时一样"。

于是回放是**完全离线**的：不碰文件、不启动子进程、不要 key。被测的是两个边界**之间**的全部代码——循环、历史、上下文的拼装、重试的判断、工具的调度——边界之外一行都不执行。

```python
def test_F14_03_recorded_tools_answer_from_the_next_request(tmp_path: Path) -> None:
    """A tool result lives in the request after the one that asked for it, so a
    replay needs no workspace at all."""
    first = [{"role": "user", "content": "read a.py"}]
    second = [
        *first,
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "c1",
                    "type": "function",
                    "function": {"name": "read_file", "arguments": '{"path":"a.py"}'},
                }
            ],
        },
        {"role": "tool", "tool_call_id": "c1", "content": "x = 1"},
    ]
    path = _recording(
        tmp_path / "r.jsonl",
        _event("request", {"turn": 0, "attempt": 0, "messages": first}),
        _event(
            "response",
            {
                "turn": 0,
                "text": "",
                "finish_reason": "tool_calls",
                "tool_calls": [{"id": "c1", "name": "read_file", "arguments": '{"path":"a.py"}'}],
            },
        ),
        _event("request", {"turn": 1, "attempt": 0, "messages": second}),
        _event("response", {"turn": 1, "text": "it defines x", "finish_reason": "stop"}),
    )
    replayed = recorded_tools(load(path))
    handler = replayed.handler("read_file")
    assert asyncio.run(handler({"path": "a.py"})) == "x = 1"
    assert asyncio.run(handler({"path": "b.py"})).startswith("Error: this call was not")
    assert replayed.missed == ['read_file({"path": "b.py"})']
```

> 测试里手写了两轮：第 0 轮模型要读 `a.py`；第 1 轮的请求里带着结果 `"x = 1"`。然后直接问那个假的 `read_file`：读 `a.py` 得到 `"x = 1"`；读 `b.py`（当时没读过）得到那句 Error，并且被记进了 `missed`。
> `asyncio.run(handler({...}))`：处理函数是 `async` 的，在普通（非 async）测试里要用 `asyncio.run` 把它跑起来。

提交：

```
feat(replay): load(), RecordedModel and recorded_tools()

A recording refuses to load rather than filling a missing argument with
{} -- a green test for a conversation that never happened is worse than
no test. The tool side needs no workspace: request N+1 contains the
results of turn N, so every tool output is already in the file.
```

---

## §8 接到命令行上：`minicodex replay`

### 8.1 子命令

`__main__.py` 的导入区加：

```python
from minicodex.replay import (
    RecordedModel,
    ReplayDrift,
    ReplayError,
    load,
    recorded_tools,
)
from minicodex.retry import DEFAULT_POLICY, ModelFailed
```

（`DEFAULT_POLICY` 是 §9.3 才用到的，一起加上。）在 `main()` 里登记子命令，写在 `serve-stub` 那个子命令后面：

```python
replay_cmd = sub.add_parser(
    "replay", help="re-run a recorded session against the current code, offline"
)
replay_cmd.add_argument("recording", type=Path)
replay_cmd.add_argument(
    "--loose",
    action="store_true",
    help="replay the answers without checking that the requests still match",
)
```

> - `add_argument("recording", type=Path)`：一个必填的位置参数，拿到手已经是 `Path`。
> - `action="store_true"`：`--loose` 是个开关，写了就是 `True`，没写就是 `False`。

在下面分派命令的地方加两行：

```python
    if args.command == "replay":
        return _replay(args.recording, strict=not args.loose)
```

### 8.2 `_replay`

```python
def _replay(path: Path, *, strict: bool) -> int:
    """Re-run a recorded session against the code as it stands today.

    No network, no API key, no files touched: the model side comes out of the
    recording and the tool side comes out of the requests that followed it.
    What is being tested is everything in between -- the loop, the history, the
    context assembly, the retry decisions -- against a conversation that
    actually happened rather than one a fixture author imagined.

    What a recording does **not** contain, and therefore what replays as drift
    rather than as itself: anything the loop reads from the machine instead of
    from the model.  `AGENTS.md` (chapter 13) is read fresh every turn, the
    plan (chapter 11) lives in an object, and the summariser's own model call
    (chapter 6) never passed through the recorder at all -- so a session that
    compacted is refused rather than half-replayed.
    """
    try:
        recording = load(path)
    except (OSError, ReplayError) as exc:
        print(exc, file=sys.stderr)
        return 1

    config = recording.config
    if config is None:
        print(
            f"{path}: no `config` event. This recording was made before chapter 14 "
            "and does not say which tools, model or sandbox mode produced it.",
            file=sys.stderr,
        )
        return 1
    if recording.compacted:
        print(
            f"{path}: this session compacted, and the summariser's own model call "
            "is not recorded. Replaying it would invent a summary.",
            file=sys.stderr,
        )
        return 1

    # `flush`, because the drift report goes to stderr and these go to stdout:
    # unflushed, a piped run prints the failure before the header it belongs to.
    print(f"{path}\n  {recording.describe()}", flush=True)
    print(f"  recorded against {config.get('provider')}/{config.get('model')}", flush=True)

    replayed = recorded_tools(recording)
    tools = ToolSet(
        handlers=replayed.table(config["tools"]),
        schemas=list(config["schemas"]),
    )
    session = Session(mode=config["sandbox_mode"], policy=config["approval_policy"])
    model = RecordedModel(recording, strict=strict)
    agent = Wiring(
        context_window=config.get("context_window"),
        # The clock is replayed too: a recorded `retry-after: 46` is decided
        # on and announced, and nobody sits through it.
        retry_policy=replace(DEFAULT_POLICY, serve_waits=False),
        announce=print,
    ).agent(
        model,
        tools,
        instructions=_instructions(session, tools),
    )

    try:
        result = asyncio.run(agent.run(config["question"]))
    except ReplayDrift as exc:
        print(f"\ndrift: {exc}", file=sys.stderr)
        return 1
    except ReplayError as exc:
        print(f"\n{exc}", file=sys.stderr)
        return 1

    # One line per *attempt*, labelled with the turn it belongs to.  The first
    # version numbered the lines and called the number a turn, so a turn that
    # was retried once showed up as two turns.  And "match" is only printed
    # when something was compared: `--loose` compares nothing.
    verdict = "match" if strict else "sent, not compared"
    for step, sent in zip(recording.attempts, model.sent, strict=False):
        again = f" (attempt {step.attempt + 1})" if step.attempt else ""
        print(f"  turn {step.turn}{again}: {len(sent)} message(s) {verdict}")
    if replayed.missed:
        print(f"  {len(replayed.missed)} call(s) not in the recording: {replayed.missed[:3]}")
    if model.unused:
        first = model.unused[0]
        print(
            f"\ndrift: the run stopped ({result.stop_reason}) after "
            f"{len(model.sent)} of {len(recording.attempts)} recorded attempt(s); "
            f"turn {first.turn} was recorded and never asked for",
            file=sys.stderr,
        )
        return 1
    same = "same as recorded" if strict else "requests not compared (--loose)"
    print(f"\nok  {result.stop_reason} after {result.turns_used} turn(s), {same}")
    return 0
```

从上往下：

> - **`load(path)`**，接住两种错：`OSError`（文件不存在、读不了）和 `ReplayError`（§5 的各种拒绝）。打印原因，返回 1。按第 -1 章的约定，`main` 返回的数就是退出码，非 0 表示失败。
> - **没有 `config`** → 老录制，拒绝。**`compacted`** → 拒绝，理由和拒绝填 `{}` 一样：半个回放比没有回放坏。
> - `print(..., flush=True)`：马上输出，不要攒着。漂移报告走的是 `stderr`（错误输出），这几行走的是 `stdout`（普通输出），两条管道各攒各的；不 `flush` 的话，输出被重定向到文件时，报告可能跑到它的标题前面去。
> - **`replayed.table(config["tools"])`**：用录制里的工具名单造一整张假工具表，再配上当时的工具说明，装成 `ToolSet`。
> - **`Session(mode=..., policy=...)`**：按当时的权限模式重建，这样 `_instructions(session, tools)` 拼出来的 system 消息才可能和当时一样。
> - `Wiring(...)` 里的 `retry_policy=` 和 `announce=` 两行：§9.3。
> - **`asyncio.run(agent.run(config["question"]))`**：把当时那个问题再问一遍。
> - `except ReplayDrift`：§6.3 费了那么大劲让它穿出来，就是为了在这里接住，打印，返回 1。
> - 最后那一段（每次尝试一行、`model.unused`、`same`）第一版只有三行，现在这个样子是 §9.1 和 §9.2 的结果。

跑起来就是 §1 开头那两段输出。提交：

```
feat(cli): `minicodex replay`

Re-runs a recorded session against the code as it stands: the model side
comes out of the recording, the tool side out of the requests that
followed it. A session that compacted is refused -- the summariser's own
model call never passed through the recorder.
```

### 8.3 这份快照没有人写

§1 那次"加一句 haiku 就被指出来"值得再想一下。

前面各章也有快照：第 3 章把工具说明原样抄进测试，插曲 A 有一份手写的"标准对话"。它们守住的是**有人记得去写的那条路**。

录制守住的是**这个程序上周二实际走过的那条路**——用的哪些工具、system 消息里拼了哪几段、工具结果怎么放回去——全都在里面，没有人需要事先想到"这个该测一下"。

把两种东西摆在一起：

| | 手写的快照 / 断言 | 一份录制的回放 |
|---|---|---|
| 谁写的 | 人，事先想到了才会写 | 程序自己，每次运行都写 |
| 知道什么是"对" | 知道——人写下了期望 | **不知道**——只知道"和上次一样不一样" |
| 两边一起错了 | 抓得住 | **抓不住** |

最后一行是回放的硬上限，§20 的自我审查会再提：回放是**相对**的检查，快照是**绝对**的检查，两个都要有。

这一节对应一个把整条链路走一遍的测试，和一个"绝对"的测试：

```python
def test_F14_03_a_recorded_session_replays_with_no_drift(
    tmp_path: Path, stub_url: str, served_requests: list[dict], monkeypatch: Any
) -> None:
    """The whole of chapter 14 in one test: run the CLI, then re-run its own
    recording against the code and require the requests to match.

    It is a snapshot nobody had to write.  A golden-transcript fixture covers
    the wiring somebody remembered to build a fixture for; this covers whatever
    the program did the last time a human ran it.

    Not `async def`, and that is not a style choice: `main()` calls
    `asyncio.run()`, and pytest-asyncio's auto mode would already have a loop
    running.  The replay half gets its own `asyncio.run` below.

    Structural limit, raised in review: this test produces its own input, so it
    can only catch the two sides *disagreeing*, never both being wrong the same
    way.  A replay is a relative check.  The absolute ones are elsewhere and
    are still needed -- `test_F14_03_the_cli_sends_a_system_message_at_all`
    just below, chapter 3's description snapshot, interlude A's golden
    transcript.
    """
    monkeypatch.chdir(tmp_path)
    from minicodex.__main__ import main

    assert main(["ask", "what does this define?", "--base-url", stub_url, "--yes"]) == 0

    recordings = sorted((tmp_path / ".minicodex" / "recordings").glob("*.jsonl"))
    assert len(recordings) == 1
    recording = load(recordings[0])
    assert recording.config is not None
    assert recording.turns == 2

    replayed = recorded_tools(recording)
    tools = ToolSet(
        handlers=replayed.table(recording.config["tools"]),
        schemas=list(recording.config["schemas"]),
    )
    from minicodex.__main__ import _instructions

    session = Session(
        mode=recording.config["sandbox_mode"], policy=recording.config["approval_policy"]
    )
    model = RecordedModel(recording)
    agent = Wiring().agent(model, tools, instructions=_instructions(session, tools))
    result = asyncio.run(agent.run(recording.config["question"]))

    assert result.stop_reason == "completed"
    assert len(model.sent) == 2


def test_F14_03_the_cli_sends_a_system_message_at_all(
    tmp_path: Path, stub_url: str, served_requests: list[dict], monkeypatch: Any
) -> None:
    """Deleting the system prompt from the CLI left all 1525 tests green.

    That was the count, and the result, when this chapter was first written.
    Re-measured during the rewrite it is no longer true -- three tests added
    to chapters 11 and 13 since then run `main()` and notice -- and this one
    stays, because it is the only one of the four that says what it is for.

    Measured, not imagined: replacing `instructions=_instructions(session,
    tools)` with `instructions=None` in `__main__` -- which switches off
    chapter 5's permission block, chapter 11's plan paragraph and chapter 13's
    whole system prompt in one line -- turned nothing red.  Every test of
    `_instructions` calls `_instructions`; none of them checks that anybody
    calls it.

    The check has to run the composition, so it runs `main()`.
    """
    from minicodex.__main__ import main

    monkeypatch.chdir(tmp_path)
    code = main(["ask", "what does this define?", "--base-url", stub_url, "--yes"])

    assert code == 0
    first = served_requests[0]["messages"]
    assert first[0]["role"] == "system", first[0]
    assert "coding agent" in first[0]["content"]
    # ...and the volatile half is still last, which is the arrangement chapter
    # 13 measured at 94% cached against 0% (F13-07).
    from minicodex.approval import permissions_block

    assert first[0]["content"].endswith(permissions_block(Session(), can_request=True))
```

> - 这两个测试都真的调 `main([...])`——和在终端里敲命令走的是同一段代码。`stub_url` 是 `conftest.py` 里的夹具：测试自己起一个录音服务，给出它的地址。`served_requests` 是录音服务收到的每一个请求。
> - **`monkeypatch.chdir(tmp_path)`**：把"当前目录"临时换成 pytest 给的临时目录，测试结束自动换回来。录制文件是写到"当前目录下的 `.minicodex/`"里的，不换的话就写进你的项目了（§18.3 就是这么一个故事）。
> - 第一个测试**不是 `async def`**：`main()` 里面自己调了 `asyncio.run()`，而一个 `async` 测试已经在事件循环里了，循环里不能再开循环。
> - 第二个测试是"绝对"的那种：直接断言 CLI 发出去的第一条消息是 system、里面有 "coding agent"、而且**以权限说明结尾**（第 13 章量过：会变的内容放最后，缓存才不失效）。

### 8.4 回放保证什么，不保证什么

必须写清楚，否则它会被当成万能的：

| | |
|---|---|
| **忠实** | 循环拼出来的每一份消息列表；每个工具调用和它的参数；`finish_reason`；服务器报的输入 token 数；失败，以及失败之后的那次重试 |
| **不忠实** | **分片**（§6.2）。碎片层面的 bug 要靠 `stub.py` 的逐字节录音 |
| **录制里根本没有** | 循环从**这台机器**上读的东西：第 13 章的 `AGENTS.md` 每轮现读；第 11 章的计划活在一个对象里。录制时有、回放时没有，就会表现为 drift |
| **直接拒绝** | 压缩过的会话（摘要那次模型调用没被录下来） |

---
## §9 改写时发现：回放说的，比它看的多

这一节的四条都不在清单上，也都不在这一章的第一版里。它们是改写时**拿回放去回放几种没试过的会话**才撞出来的，而且是同一种毛病：`minicodex replay` 报告的结论，比它实际检查过的东西大。

### 9.1 只跑了一半，它说 `same as recorded`

回放的全部意义是抓"以前好的，现在坏了"。那就找一个真的坏法试试——第 0 章的第一个 bug（F00-02）：模型先说一句"我这就去读"，再要求调工具；循环看到有文字就以为它答完了，活没干就收工。

把它放回去。`agent.py` 里：

```python
-            if not turn.tool_calls:
+            if turn.text:
```

拿 §1 那份两轮的录制回放。**第一版**的输出：

```
.minicodex/recordings/session-1794663608.jsonl
  2 turn(s), 2 attempt(s) (0 failed), 1 tool call(s)
  recorded against ollama/gemma4:31b-cloud
  turn 0: 2 message(s) match

ok  completed after 1 turn(s), same as recorded
```

**`ok`，`same as recorded`。** 上面第二行明明写着这份录制有 2 轮，最后一行说跑了 1 轮，中间隔着一个 `ok`。

原因：第一版只检查了"**发出去的**每个请求都和当时一样"。第 0 轮的请求确实一样。然后循环提前停了，第 1 轮的请求**根本没发**——没发就没有东西可比，没有东西可比就没有不同。结尾那句 `same as recorded` 是直接写在 `print` 里的，没有任何一行代码检查过它。

"每个请求都对得上"只是"和当时一样"的一半；另一半是**当时走了多远，现在也走了多远**。

修法两处。`RecordedModel` 上那个 §6.2 跳过的属性：

```python
@property
def unused(self) -> tuple[RecordedAttempt, ...]:
    """Recorded attempts the loop never asked for.

    Every request matching is half of "same as recorded"; the other half is
    that the run went on for as long.  Put chapter 0's first bug back --
    stop as soon as a reply has text (F00-02) -- and a two-turn recording
    replays as one matching request and then silence.  The first version
    of `minicodex replay` printed `ok ... same as recorded` for that run.
    """
    return self.recording.attempts[self._next :]
```

> `self.recording.attempts[self._next :]`：切片，从"下一条该用的"到最后——也就是**还没被要过的**那些尝试。全用完了就是空元组，空元组放在 `if` 里算假。

`_replay` 的结尾：

```python
# One line per *attempt*, labelled with the turn it belongs to.  The first
# version numbered the lines and called the number a turn, so a turn that
# was retried once showed up as two turns.  And "match" is only printed
# when something was compared: `--loose` compares nothing.
verdict = "match" if strict else "sent, not compared"
for step, sent in zip(recording.attempts, model.sent, strict=False):
    again = f" (attempt {step.attempt + 1})" if step.attempt else ""
    print(f"  turn {step.turn}{again}: {len(sent)} message(s) {verdict}")
if replayed.missed:
    print(f"  {len(replayed.missed)} call(s) not in the recording: {replayed.missed[:3]}")
if model.unused:
    first = model.unused[0]
    print(
        f"\ndrift: the run stopped ({result.stop_reason}) after "
        f"{len(model.sent)} of {len(recording.attempts)} recorded attempt(s); "
        f"turn {first.turn} was recorded and never asked for",
        file=sys.stderr,
    )
    return 1
same = "same as recorded" if strict else "requests not compared (--loose)"
print(f"\nok  {result.stop_reason} after {result.turns_used} turn(s), {same}")
return 0
```

现在同一次回放：

```
  turn 0: 2 message(s) match

drift: the run stopped (completed) after 1 of 2 recorded attempt(s); turn 1 was recorded and never asked for
```

退出码是 1。把 `agent.py` 改回去。

### 9.2 另外两处说大了的话

上面那段代码里还改了两件小事，都是同一种毛病。

**`--loose` 什么都没比，却打印 `match`。** `--loose` 的意思是"只把当时的回复放一遍，别比对请求"。第一版不管开没开它，每行都打印 `N message(s) match`。现在用 `verdict` 变量区分，结尾那句也跟着变：

```
$ uv run minicodex replay .minicodex/recordings/session-1794663608.jsonl --loose
  turn 0: 2 message(s) sent, not compared
  turn 1: 4 message(s) sent, not compared

ok  completed after 2 turn(s), requests not compared (--loose)
```

**把"第几次尝试"叫成了"第几轮"。** 第一版是 `for index, sent in enumerate(model.sent): print(f"  turn {index}: ...")`——`model.sent` 里一个元素是一次**尝试**，它却把序号叫作 turn。一轮里重试过一次的录制，会被印成三"轮"（实际是两轮三次尝试）。现在每一行用的是那次尝试自己记着的轮号，重试的那次单独标出来：

```
  turn 0: 2 message(s) match
  turn 0 (attempt 2): 2 message(s) match
  turn 1: 4 message(s) match
```

> `zip(recording.attempts, model.sent, strict=False)`：录制里的尝试和这次实际发出的请求并排走。`f" (attempt {step.attempt + 1})" if step.attempt else ""`：第 0 次尝试什么都不加；之后的加上"第几次"（从 1 数，给人看）。

```python
def test_F14_03_replay_fails_when_the_run_stops_before_the_recording_does(
    tmp_path: Path, stub_url: str, served_requests: list[dict], monkeypatch: Any, capsys: Any
) -> None:
    """Every request matching is half of "same as recorded".

    Chapter 0's first bug, put back (`if turn.text:` as the stop condition),
    replayed a two-turn recording as one matching request and then
    `ok  completed after 1 turn(s), same as recorded`.  The code cannot be
    mutated from inside a test, so the same situation is built from the other
    side: a real two-turn recording whose first reply is edited to ask for no
    tools.  Today's loop then stops after turn 0, and turn 1 is still on file.
    """
    from minicodex.__main__ import main

    monkeypatch.chdir(tmp_path)
    assert main(["ask", "what does this define?", "--base-url", stub_url, "--yes"]) == 0
    (path,) = (tmp_path / ".minicodex" / "recordings").glob("*.jsonl")
    capsys.readouterr()

    assert main(["replay", str(path)]) == 0
    assert "same as recorded" in capsys.readouterr().out

    assert main(["replay", str(path), "--loose"]) == 0
    loose = capsys.readouterr().out
    assert "not compared" in loose and "match" not in loose

    events = [json.loads(line) for line in path.read_text("utf-8").splitlines()]
    first_reply = next(e for e in events if e["kind"] == "response")
    first_reply["payload"]["tool_calls"] = []
    path.write_text("\n".join(json.dumps(e) for e in events) + "\n", encoding="utf-8")

    assert main(["replay", str(path)]) == 1
    captured = capsys.readouterr()
    assert "1 of 2 recorded attempt(s)" in captured.err
    assert "same as recorded" not in captured.out
```

> - 测试里没法"把代码改坏"，所以从另一头造出同样的局面：先真的录一份两轮的；然后**改录制**——把第一条回复里的工具调用删掉。今天的循环看到"没有工具调用"就正常收工，而录制里还躺着第 1 轮。
> - `(path,) = (...).glob("*.jsonl")`：§0.2 的"恰好一个"写法。目录里有零个或两个录制文件，这一行自己就会报错。
> - `capsys.readouterr()` 取走到目前为止的全部输出。先调一次是为了把 `ask` 打印的东西清掉，后面每次取到的就只是那一次 `replay` 的。

### 9.3 回放一次"请等 46 秒"，它真的等了 46 秒

§6.3 的测试回放过一次限流，但那份录制是手写的，响应头是空的。第 12 章从 OpenAI 录下来的那次真限流带着 `retry-after: 46`。把它放进一份录制，用命令行回放，掐表：

```
$ time uv run minicodex replay with429.jsonl
with429.jsonl
  2 turn(s), 3 attempt(s) (1 failed), 1 tool call(s)
  recorded against ollama/gemma4:31b-cloud
  turn 0: 2 message(s) match
  turn 1: 2 message(s) match
  turn 2: 4 message(s) match

ok  completed after 2 turn(s), same as recorded

real    0m46.356s
```

（这是修之前的输出，所以还带着 §9.2 那个把尝试叫成轮的毛病。）

**46 秒，中间屏幕上什么都没有。** 道理上它没错：循环看到一个 429、读到"46 秒后再试"、老老实实睡了 46 秒——这正是被测的重试逻辑在工作。但一个"离线的、确定的"测试看起来像卡死了 46 秒，是一个没有人会跑第二次的测试。

回放替换了两个边界：模型和工具。**时钟是第三个。**

要的效果是：等多久照样**算**（那是被测的东西），照样**说**，只是不真的**睡**。给第 12 章的 `RetryPolicy` 加一个字段（`retry.py`，写在 `budget` 后面）：

```python
attempts: int = 4
base: float = 1.0
cap: float = 30.0
budget: float = 90.0
# `False` decides every wait exactly as usual, announces it, and does not
# sleep.  For chapter 14's replay and nothing else: a recorded 429 carries
# the `retry-after: 46` it carried on the day, and the first replay of one
# sat silent for 46.4 seconds -- an "offline, deterministic" test that looks
# hung is a test nobody runs twice.  The clock is the third boundary a
# replay has to stand in for, after the model and the tools.
serve_waits: bool = True
```

`agent.py` 的 `_respond()` 里用它，三处。开头多一个记账的变量：

```python
began = time.monotonic()
# Waits that were decided and not served (`RetryPolicy.serve_waits`).
# Counted as if they had happened, so that the *next* decision -- "is
# there budget left for another 46 seconds" -- comes out the way it did
# on the day.
skipped = 0.0
shrunk = False
```

算"已经过了多久"时把它加上：

```python
            elapsed = time.monotonic() - began + skipped
```

原来 `await asyncio.sleep(wait)` 的地方：

```python
    if self.retry_policy.serve_waits:
        await asyncio.sleep(wait)
    else:
        skipped += wait

raise ModelFailed(
    failure,
    attempts=self.retry_policy.attempts,
    waited=time.monotonic() - began + skipped,
)
```

> - 最后那个 `raise ModelFailed(...)` 在循环**外面**（缩进少一层），它只是把 `waited` 的算法也加上了 `skipped`。
> - **为什么跳过的等待还要记进 `skipped`？** 第 12 章的重试有一个总预算（默认 90 秒）：等过的时间加上这次要等的，超了就放弃。当天等完第一个 46 秒，第二个 429 再要 46 秒，46 + 46 > 90，于是放弃。回放时如果跳过等待**又不记账**，"已经过了多久"永远接近 0，预算永远够，循环会去要第三次尝试——而录制里没有第三次。**跳过的等待要当成等过了**，下一个决定才会和当天一样。

`_replay` 里造 `Wiring` 的地方（§8.2 已经贴过）用上它：

```python
        retry_policy=replace(DEFAULT_POLICY, serve_waits=False),
        announce=print,
```

> `replace(DEFAULT_POLICY, serve_waits=False)`：`dataclasses.replace`，"复制一份，只改这一个字段"。冻结的数据类不能改，只能这样造一个新的。`announce=print` 让"正在等"那句话打印出来。

同一份录制，现在：

```
$ time uv run minicodex replay with429.jsonl
with429.jsonl
  2 turn(s), 3 attempt(s) (1 failed), 1 tool call(s)
  recorded against ollama/gemma4:31b-cloud
[rate_limit: waiting 45s, attempt 2 of 4]
  turn 0: 2 message(s) match
  turn 0 (attempt 2): 2 message(s) match
  turn 1: 4 message(s) match

ok  completed after 2 turn(s), same as recorded

real    0m0.901s
```

```python
def _rate_limited(turn: int, attempt: int, **extra: Any) -> str:
    return _event(
        "model_failure",
        {
            "turn": turn,
            "attempt": attempt,
            "disposition": "retry",
            "kind": "rate_limit",
            "detail": "rate limited",
            "status": 429,
            # Three seconds and not the recorded 46: the mutation that puts
            # the sleep back has to be noticed without anybody waiting 46
            # seconds for it.
            "headers": {"retry-after": "3"},
            "body": json.dumps(stub.RATE_LIMITED["body"]),
            **extra,
        },
    )


async def test_F14_03_a_replay_decides_a_recorded_wait_and_does_not_serve_it(
    tmp_path: Path,
) -> None:
    """A recorded `retry-after`, replayed without anybody sitting through it.

    The first `minicodex replay` of a rate-limited session took 46.4 seconds
    and printed nothing while it waited.  The wait is still *decided* -- that
    is the retry logic under test -- and still announced; it is just not slept.
    """
    messages = [{"role": "user", "content": "hi"}]
    path = _recording(
        tmp_path / "r.jsonl",
        _event("request", {"turn": 0, "attempt": 0, "messages": messages}),
        _rate_limited(0, 0),
        _event("request", {"turn": 0, "attempt": 1, "messages": messages}),
        _event("response", {"turn": 0, "text": "done", "finish_reason": "stop", "tool_calls": []}),
    )
    said: list[str] = []
    agent = Wiring(retry_policy=RetryPolicy(serve_waits=False), announce=said.append).agent(
        RecordedModel(load(path)), ToolSet(handlers={}, schemas=[])
    )

    began = time.monotonic()
    result = await agent.run("hi")

    assert time.monotonic() - began < 1.5
    assert result.final_text == "done"
    assert said and "waiting 3s" in said[0]


async def test_F14_03_an_unserved_wait_still_counts_against_the_budget(tmp_path: Path) -> None:
    """Two 429s asking for 3 seconds each do not fit in a 5-second budget once
    the first has been waited out, so on the day the run gave up after the
    second.  A replay that skipped the waits *and forgot them* would have the
    whole budget left, ask for a third attempt, and run off the end of the
    recording -- a different run from the one that was recorded."""
    messages = [{"role": "user", "content": "hi"}]
    path = _recording(
        tmp_path / "r.jsonl",
        _event("request", {"turn": 0, "attempt": 0, "messages": messages}),
        _rate_limited(0, 0),
        _event("request", {"turn": 0, "attempt": 1, "messages": messages}),
        _rate_limited(0, 1),
    )
    model = RecordedModel(load(path))
    agent = Wiring(retry_policy=RetryPolicy(budget=5.0, serve_waits=False)).agent(
        model, ToolSet(handlers={}, schemas=[])
    )

    with pytest.raises(ModelFailed) as failure:
        await agent.run("hi")
    # The failure that ended it is the 429, not "the recording ran out".
    assert failure.value.failure.kind == "rate_limit"
    assert failure.value.attempts == 2
```

> - `_rate_limited(turn, attempt)` 生成一条"限流"失败事件。响应头写的是 3 秒而不是录下来的 46 秒：§19.2 的变异检查会把"不睡"改回"睡"，然后看这个测试红不红——那一次得有人真的等，3 秒等得起，46 秒等不起。
> - 第一个测试：`announce=said.append` 把"正在等"的话收进一个列表；断言整件事 1.5 秒内结束、答案对、而且**确实说过**"waiting 3s"。
> - 第二个测试就是上面"记账"那一条：预算 5 秒，两个各要 3 秒的 429。必须在第二个之后放弃，而且放弃的原因是限流（`failure.value.failure.kind == "rate_limit"`），不是"录制用完了"。

### 9.4 有子 Agent 的会话：父子两段对话被拼成了一段

第 10 章起，模型可以调 `spawn_agent` 派一个子 Agent 去干活。插曲 B 修过一件事：子 Agent 要用父亲的**同一个录制器**，否则它的对话哪儿都不留（FB-01）。

那么一个文件里就有两段对话。而每个 Agent 都是从自己的第 0 轮数起的。

造一个最小的场景：父亲第 0 轮调 `spawn_agent`；孩子第 0 轮读一个文件、第 1 轮汇报；父亲第 1 轮说完。四次模型调用。把录制文件里的事件按顺序印出来：

```
  request  {'turn': 0, 'attempt': 0} msgs=1        ← 父亲
  response {'turn': 0}               ['spawn_agent']
  request  {'turn': 0, 'attempt': 0} msgs=2        ← 孩子
  response {'turn': 0}               ['read_file']
  request  {'turn': 1, 'attempt': 0} msgs=4        ← 孩子
  response {'turn': 1}               child says hello
  request  {'turn': 1, 'attempt': 0} msgs=3        ← 父亲
  response {'turn': 1}               parent done
```

右边的"父亲""孩子"是我标的。**文件里没有这个信息。** 两个 `turn 0 attempt 0`，两个 `turn 1 attempt 0`。

§5.2 的 `load` 是用 `(turn, attempt)` 当字典的键的。往字典里存两次同一个键，后一次盖掉前一次。于是第一版读出来的是：

```
load: 2 turn(s), 2 attempt(s) (0 failed), 1 tool call(s)
  attempt 0 0  first user msg: 'read a.txt and report'  ->  ['read_file']
  attempt 1 0  first user msg: 'delegate it'            ->  parent done
```

**孩子的第 0 轮，接上父亲的第 1 轮。** 一段谁也没说过的对话，被描述成"2 轮，2 次尝试"，没有任何报错。严格模式下它会报一个 drift，把账算到今天的代码头上（"消息 0 的角色变了"）；`--loose` 下它会把这段拼出来的对话放完，然后说 `ok`。

修法分两头。

**写的那头：每个 Agent 在自己记的每一条事件上签个名。** `agent.py` 开头的常量区：

```python
# One number per `Agent`, written into every event it records.  A parent and
# the sub-agents it spawns share one recorder (interlude B, FB-01), and each of
# them counts its own turns from zero -- so without this the file holds two
# `request` lines for "turn 0 attempt 0" and nothing says whose they are.
# Chapter 14's loader found out the hard way: it paired them by `(turn,
# attempt)`, kept whichever came last, and described a four-call session as
# "2 turn(s), 2 attempt(s)" built from the child's first turn and the parent's
# second.  A process-wide counter and not a constructor argument: which number
# an agent gets does not matter, only that no two agents get the same one.
_CONVERSATIONS = itertools.count()
```

（文件开头的 `import` 里加一行 `import itertools`。）`Agent.__init__` 里，`self.retry_policy = retry_policy` 下面：

```python
        self.conversation = next(_CONVERSATIONS)
```

> `itertools.count()` 是一个没有尽头的计数器，每 `next()` 一次给出下一个整数。它放在模块最外层，整个程序只有这一个，所以每造一个 Agent 就领到一个不重复的号。号码是几不重要，**不重复**才重要。

然后是 §4.2 让你先跳过的那三行：`request`、`model_failure`、`response` 三处记录里各加一个 `"conversation": self.conversation`。`request` 那一处原来挤在一行里，加了一项之后拆成这样：

```python
self.recorder.record(
    "request",
    {
        "conversation": self.conversation,
        "turn": turn_index,
        "attempt": attempt,
        "messages": messages,
    },
)
```

**读的那头**，就是 §5.2 的 `load` 里留到现在的那几行：

```python
        elif kind == "request":
            if not requests:
                top = payload.get("conversation")
            if payload.get("conversation") != top:
                elsewhere += 1
                continue
            key = (payload["turn"], payload["attempt"])
            if key in requests:
                raise ReplayError(...)
```

> - **文件里第一条请求是谁发的，这份录制就是谁的。** 孩子只可能在父亲的第一次回复之后才被派出去，所以第一条请求一定是父亲的。`if not requests:`——`requests` 还是空的，说明这是第一条——记下它的号码 `top`。
> - **别人的请求数一下，跳过。** 不需要回放孩子：对父亲来说，孩子的整个运行就是**一次 `spawn_agent` 工具调用**，而它的结果和别的工具结果一样，就在父亲的下一条请求里（§7）。"工具那一侧不用重建"这条红利，在这里又领了一次。
> - **`if key in requests: raise`**：同一段对话里出现两个一样的键。只有一种情况会这样——**没有签名的老录制**（所有事件的 `conversation` 都读成 `None`，全都"相等"）里混着子 Agent。分不清谁是谁，就拒绝，理由和 §5.2 拒绝填 `{}` 完全一样。

同一个场景，现在：

```
load: 2 turn(s), 2 attempt(s) (0 failed), 1 tool call(s); 2 sub-agent request(s) in the file, not replayed
  attempt 0 0  first user msg: 'delegate it'  ->  ['spawn_agent']
  attempt 1 0  first user msg: 'delegate it'  ->  parent done
replay: completed 'parent done'
```

```python
def test_F14_03_a_sub_agents_requests_do_not_displace_its_parents(tmp_path: Path) -> None:
    """A child writes to its parent's recorder and counts its turns from zero.

    Measured on a real parent-and-child run before the `conversation` field
    existed: `load()` paired on `(turn, attempt)`, the child's "turn 0" replaced
    the parent's, and the result -- child's first turn, then parent's second --
    was described as `2 turn(s), 2 attempt(s)` with no complaint from anything.
    """
    parent = [{"role": "user", "content": "delegate it"}]
    child = [{"role": "user", "content": "read a.txt and report"}]
    path = _recording(
        tmp_path / "r.jsonl",
        _event("request", {"conversation": 7, "turn": 0, "attempt": 0, "messages": parent}),
        _event(
            "response",
            {
                "conversation": 7,
                "turn": 0,
                "text": "",
                "tool_calls": [{"id": "p1", "name": "spawn_agent", "arguments": "{}"}],
            },
        ),
        _event("request", {"conversation": 8, "turn": 0, "attempt": 0, "messages": child}),
        _event("response", {"conversation": 8, "turn": 0, "text": "hello", "tool_calls": []}),
        _event("request", {"conversation": 7, "turn": 1, "attempt": 0, "messages": parent}),
        _event("response", {"conversation": 7, "turn": 1, "text": "done", "tool_calls": []}),
    )
    recording = load(path)

    assert [a.request[0]["content"] for a in recording.attempts] == ["delegate it"] * 2
    assert [c.name for c in recording.attempts[0].tool_calls] == ["spawn_agent"]
    assert recording.attempts[1].text == "done"
    assert recording.elsewhere == 1
    assert "1 sub-agent request(s)" in recording.describe()


def test_F14_03_a_repeated_key_with_no_owner_is_refused(tmp_path: Path) -> None:
    """The same file as chapters 10 to 13 would have written it: no
    `conversation` on anything.  Which of the two "turn 0" requests is the
    parent's cannot be recovered, so this is refused rather than guessed --
    the rule for a missing argument, applied to a missing owner."""
    path = _recording(
        tmp_path / "old.jsonl",
        _event(
            "request", {"turn": 0, "attempt": 0, "messages": [{"role": "user", "content": "a"}]}
        ),
        _event("response", {"turn": 0, "text": "", "tool_calls": []}),
        _event(
            "request", {"turn": 0, "attempt": 0, "messages": [{"role": "user", "content": "b"}]}
        ),
        _event("response", {"turn": 0, "text": "", "tool_calls": []}),
    )
    with pytest.raises(ReplayError, match="nothing says whose they are"):
        load(path)


async def test_F14_03_every_agent_signs_its_events_with_its_own_number(tmp_path: Path) -> None:
    """Two agents, one recorder -- which is what a parent and a child are."""
    recorder = Recorder(tmp_path / "run.jsonl")
    wiring = Wiring(recorder=recorder)
    for answer in ("one", "two"):
        await wiring.agent(_Answers([answer]), ToolSet(handlers={}, schemas=[])).run("hi")

    events = [json.loads(line) for line in recorder.path.read_text("utf-8").splitlines()]
    owners = [e["payload"]["conversation"] for e in events if e["kind"] in ("request", "response")]
    assert len(owners) == 4
    assert owners[0] == owners[1] and owners[2] == owners[3]
    assert owners[0] != owners[2]
```

> - 第一个测试手写了上面那种交错的文件（号码用 7 和 8，随便两个不同的数）：读出来的两次尝试必须都是父亲的，孩子那一次请求被数进 `elsewhere`。
> - 第二个：同样交错、但**没有签名**，必须拒绝。
> - 第三个：两个 Agent 共用一个录制器，各跑一轮。四条事件，前两条同号、后两条同号、两组不同号。`_Answers` 是这个测试文件里的一个小替身（§19.1）：照着列表说话、不要工具的模型。

提交：

```
fix(replay): a replay now reports what it checked, and only that

Found by replaying sessions the first version had never been shown:

- a run that stopped after one of two recorded turns printed "ok ...
  same as recorded" (chapter 0's F00-02, put back on purpose);
- --loose printed "match" for requests it never compared, and attempts
  were numbered and called turns;
- a recorded `retry-after: 46` was slept through, silently, for 46.4s;
- a sub-agent shares its parent's recorder and counts turns from zero,
  so load() spliced the child's first turn onto the parent's second.
```

这一节的四条放在一起看：没有一条让回放**说错**了某个具体的请求。四条都是它**少看了**——少看了没发出去的那一轮、少看了"没比较"这个事实、少看了时间、少看了文件里的另一段对话——然后照常报告 `ok`。这个形状 §15 还会再出现三次。

---
## §10 F14-01：看它做了什么，不看它怎么说

回放回答的是"代码发出去的东西变没变"。它回答不了另一个问题：**模型的表现变没变。** 那得真的去问模型，而且得有办法判断一次运行"做对了没有"。

### 10.1 先量：最顺手的那种断言靠不靠得住

每个人写的第一个断言都是这样的：

```python
assert "I have added" in answer
```

"回答里有这句话，就算它干了。"清单 F14-01 说这种断言模型换个说法就红。量一下。同一个任务（给 `calc.py` 加一个 `subtract` 函数）跑五次，两个模型：

```
$ uv run python probe_eval.py trajectory --provider openai --samples 5
  sample 0: read_file -> apply_patch
    answer opens: 'I have added the `subtract` function to `calc.py`. Here is the updated'
  sample 1: read_file -> apply_patch
    answer opens: 'I have added the `subtract` function to `calc.py`. The updated file no'
  sample 2: read_file -> apply_patch
    answer opens: 'I have added a `subtract` function to `calc.py`. The updated file now '
  sample 3: read_file -> apply_patch
    answer opens: 'I have added the `subtract` function to `calc.py`. The updated file no'
  sample 4: (no tool calls)
    answer opens: 'Would you like the subtract function to take two parameters and return'

  trajectory checks (file on disk):   4/5
  answer contains 'I have added':      4/5
  answer contains 'subtract':          5/5
  answer contains 'successfully':      0/5
  answer length: min 133 max 224
```

```
$ uv run python probe_eval.py trajectory --provider ollama --samples 5
  sample 0: run_shell -> read_file -> apply_patch
    answer opens: 'I have added the `subtract` function to `calc.py`.'
  ... （五次完全一样）
  trajectory checks (file on disk):   5/5
  answer contains 'I have added':      5/5
  answer length: min 50 max 50
```

（实测于 2026-08。）

**第二个模型五次返回一模一样的 50 个字符。** 如果你只在它上面开发，`assert "I have added" in answer` 会稳稳地绿五次、五十次，你会理直气壮地把它写进 CI。换到第一个模型：五次五个说法，长度从 133 到 224。

所以这条故障真正的样子不是"检查措辞的断言会红"，而是**它会在一个模型上看起来完全可靠**。

再看第一段的 sample 4。它一个工具都没调，反过来问了用户一个问题——**活没干**。而 `answer contains 'subtract'` 是 5/5：那句反问里正好有 "subtract" 这个词。**一次没干活的运行，通过了检查措辞的断言。** 第 0 章 §6 那条规则（"永远不要相信模型对自己行为的描述"）在这里又出现了一次，这回是在测试里。

### 10.2 `evals.py`：一次运行留下了什么

新建 `src/minicodex/evals.py`：

```python
"""A task set, and what "it worked" means for each task.

Every earlier chapter measured *one* thing at a time: one sentence, one task,
three samples, an arm and a baseline.  That is how each of those chapters
reached a defensible conclusion, and it is also why chapter 13's shipped
sentence is a risk -- it was measured on the one task it was designed for.  A
change to the system prompt, or to compaction, or to memory later on, changes
**every** session.  Averaged over one task, "no harm" means nothing.

So: several tasks, run end to end, scored by what the agent *did*.

Three decisions worth stating before the code.

**Checks read the trajectory, not the prose.**  `"subtract" in final_text` is
the assertion everybody writes first and it measures the model's vocabulary.
`Trajectory` records what was called, with what arguments, and what the
workspace looked like afterwards; a task passes when the file on disk says so.
`answer_has()` exists anyway, is used by exactly one task, and is labelled --
some things really are claims about the answer, and the honest way to have one
is to know that it is one.

**A task owns its workspace and nothing else.**  `files` is the whole content
of the directory the agent is pointed at.  Not a fixture directory in this
repository, and not `tmp_path` with something already in it: chapters 16 and 17
add a memory the agent can read, and an eval whose workspace contains the
answer -- or the test that checks the answer -- measures the search path rather
than the agent (F14-08).  The isolation is asserted, not assumed.

**No `EvalRunner` class.**  A task is data, running one is a function, and
comparing two sets of results is a second function.  There is one caller shape
(a probe) and one test shape, which is not three (FB-03).
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from minicodex.agent import Model, Wiring
from minicodex.approval import AllowAll, Session
from minicodex.composition import local_tools
```

> 模块说明里先写下了三个决定，后面三节各讲一个：检查读轨迹（§10）；每个任务独占一个工作区（§11、§12）；不造一个 `EvalRunner` 类（§11.3）。

第一个类型：一次运行里，**允许检查去问的**那些事。

```python
@dataclass(frozen=True)
class Trajectory:
    """What one run did, in the terms a check is allowed to ask about."""

    calls: tuple[tuple[str, dict[str, Any]], ...]
    final_text: str
    stop_reason: str
    turns: int
    files: Mapping[str, str]

    def called(self, name: str) -> bool:
        return any(call == name for call, _ in self.calls)

    def count(self, name: str) -> int:
        return sum(1 for call, _ in self.calls if call == name)

    def read(self, path: str) -> str:
        return self.files.get(path, "")

    def describe(self) -> str:
        return " -> ".join(name for name, _ in self.calls) or "(no tool calls)"
```

> - `calls`：按顺序，每一次工具调用的 `(工具名, 参数)`。`tuple[tuple[str, dict[str, Any]], ...]` 读作"一个元组，每个元素又是一个二元组：一个字符串和一个字典"。
> - `final_text`：最后那段话。留着，但 §10.3 会说它只该被谨慎地用。
> - `stop_reason`、`turns`：怎么停的（正常答完 / 轮数用光）、用了几轮。
> - `files`：运行结束后，工作区里**每个文件的内容**——`路径 → 全文`。这是最硬的证据：它说它加了函数，那文件里就该有。
> - `Mapping[str, str]` 是"只读的字典"：类型上只许查，不许改。
> - `called` 里的 `any(call == name for call, _ in self.calls)`：遍历时把每个二元组拆成 `call` 和 `_`（下划线表示"这个我不用"），只要有一个工具名对得上就是真。
> - `describe()`：`" -> ".join(...)` 把工具名用箭头连起来。`A or B`——`A` 是空字符串（一个工具都没调）时用 `B`。

### 10.3 `Check`：一个带名字的是非题

```python
@dataclass(frozen=True)
class Check:
    """One yes/no question about a trajectory, with a name a report can print."""

    name: str
    holds: Callable[[Trajectory], bool]


def calls(name: str) -> Check:
    return Check(f"calls {name}", lambda t: t.called(name))


def never_calls(name: str) -> Check:
    return Check(f"never calls {name}", lambda t: not t.called(name))


def file_has(path: str, needle: str) -> Check:
    return Check(f"{path} contains {needle!r}", lambda t: needle in t.read(path))


def file_lacks(path: str, needle: str) -> Check:
    return Check(f"{path} does not contain {needle!r}", lambda t: needle not in t.read(path))


def file_is(path: str, content: str) -> Check:
    return Check(f"{path} is unchanged", lambda t: t.read(path) == content)


def finished() -> Check:
    return Check("ran out of neither turns nor patience", lambda t: t.stop_reason == "completed")


def answer_has(needle: str) -> Check:
    """A check on the model's words.

    Kept, used once, and named so that it is visible in a report as the one
    row that can go red because a model changed its phrasing.  F14-01 is not
    "never assert on text"; it is "know which of your assertions are about
    text, because those are the ones that will lie to you".
    """
    return Check(f"answer mentions {needle!r}", lambda t: needle.lower() in t.final_text.lower())


def asks_a_question() -> Check:
    return Check("ends by asking a question", lambda t: "?" in t.final_text)
```

> - **`Check` 只有两样东西**：一个名字（印在报告里），一个函数 `holds`（给它一条轨迹，回答成立不成立）。
> - **下面八个都是"造检查的函数"**。`file_has("calc.py", "def subtract")` 返回一个 `Check`，名字是 `calc.py contains 'def subtract'`，`holds` 是 `lambda t: needle in t.read(path)`。`lambda` 记得外面的 `path` 和 `needle`（第 13 章 §0.2）。
> - 这样写的好处是任务读起来像一张清单：`calls("apply_patch")`、`file_has("calc.py", "def subtract")`、`file_is("test_calc.py", 原文)`——写任务的人是在**填空**，不是在写判断逻辑。
> - **`answer_has` 留着，而且有一段专门的说明。** F14-01 不是"永远别检查文字"。有的任务产出的就是文字（"calc.py 里有哪些函数？"），除了看文字没处可看。要紧的是**知道自己哪条断言是关于文字的**——它的名字在报告里会原样印成 `answer mentions 'multiply'`，哪天它红了，你一眼知道该先怀疑"模型换了说法"。

```python
def _trajectory(**kwargs: Any) -> Trajectory:
    base: dict[str, Any] = {
        "calls": (),
        "final_text": "",
        "stop_reason": "completed",
        "turns": 1,
        "files": {},
    }
    base.update(kwargs)
    return Trajectory(**base)


def test_F14_01_a_text_check_and_a_file_check_disagree_about_the_same_run() -> None:
    """Two runs that did the identical thing and said it differently.

    Both wrote `def subtract` to the file.  One narrated it as "I have added
    the subtract function", the other as "Done." -- and the check on the words
    calls the second one a failure.  Measured against gpt-4o-mini in
    `probe_eval.py trajectory`; the phrasings below are two of the answers it
    actually produced.
    """
    wordy = _trajectory(
        final_text="I have added the `subtract` function to `calc.py`.",
        files={"calc.py": "def subtract(a, b):\n    return a - b\n"},
    )
    terse = _trajectory(
        final_text="Done.",
        files={"calc.py": "def subtract(a, b):\n    return a - b\n"},
    )

    on_disk = file_has("calc.py", "def subtract")
    in_prose = answer_has("I have added")

    assert on_disk.holds(wordy) and on_disk.holds(terse)
    assert in_prose.holds(wordy)
    assert not in_prose.holds(terse)


def test_F14_01_a_trajectory_check_names_the_tool_not_the_sentence() -> None:
    """`calls("apply_patch")` is true of a run that edited and false of one
    that only talked about editing -- which is the distinction the answer text
    cannot make, because a model that failed to edit will still describe the
    edit it meant to make."""
    edited = _trajectory(calls=(("apply_patch", {"edits": []}),))
    talked = _trajectory(final_text="I will add a subtract function to calc.py.")
    assert calls("apply_patch").holds(edited)
    assert not calls("apply_patch").holds(talked)
```

> - `_trajectory(**kwargs)`：造一条轨迹的辅助函数——先给每个字段一个默认值，再用传进来的覆盖。测试里只写自己关心的那一两个字段。
> - 第一个测试是 §10.1 那次测量的缩影：两次运行**做了一模一样的事**（文件里都有 `def subtract`），一个说 "I have added…"，一个只说 "Done."。看文件的检查两个都过；看措辞的检查说第二个失败了。
> - 第二个反过来：只说"我会加"而没调工具的，`calls("apply_patch")` 不成立。

---

## §11 一个任务，一个工作区，一份结果

### 11.1 `Task` 和它的工作区

```python
@dataclass(frozen=True)
class Task:
    name: str
    files: Mapping[str, str]
    question: str
    checks: tuple[Check, ...]
    mode: str = "workspace-write"
    max_turns: int = 8


def _materialise(workspace: Path, files: Mapping[str, str]) -> None:
    workspace.mkdir(parents=True, exist_ok=True)
    for name, content in files.items():
        path = workspace / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")


def _snapshot(workspace: Path) -> dict[str, str]:
    return {
        path.relative_to(workspace).as_posix(): path.read_text(encoding="utf-8", errors="replace")
        for path in sorted(workspace.rglob("*"))
        if path.is_file()
    }
```

> - **`Task`**：名字；`files`——工作区里**全部**的文件（`路径 → 内容`）；交给 Agent 的那句话；一组检查；权限模式（默认允许改文件）；最多几轮。
> - **`_materialise`**：把 `files` 真的写到磁盘上。`mkdir(parents=True, exist_ok=True)`：连同上级目录一起建，已经存在也不报错。
> - **`_snapshot`**：反过来，把目录里现在所有的文件读成 `路径 → 内容`。`workspace.rglob("*")` 列出目录下的一切（含子目录里的）；`if path.is_file()` 只要文件；`relative_to(workspace).as_posix()` 得到形如 `src/a.py` 的相对路径——`as_posix()` 保证 Windows 上也是正斜杠，这样检查里写的 `"calc.py"` 在哪个系统都对得上。`errors="replace"`：万一模型写出了不是合法文字的字节，用占位符顶替，别让读文件这一步崩掉。

### 11.2 `Result` 和 `run_task`

```python
@dataclass(frozen=True)
class Result:
    task: Task
    trajectory: Trajectory
    passed: tuple[str, ...]
    failed: tuple[str, ...]
    error: str | None = None

    @property
    def ok(self) -> bool:
        return not self.failed and self.error is None


async def run_task(
    task: Task,
    build_model: Callable[[list[dict[str, Any]]], Model],
    *,
    workspace: Path,
    instructions: str | None = None,
    wiring: Wiring | None = None,
) -> Result:
    """Run one task in a directory containing exactly what the task declared.

    `build_model` is a factory rather than a model because the tool schemas are
    not known until the tool set is built, and chapter 9 made a client carry
    its own tool list.  The same factory shape `__main__` uses, so an eval and
    a real run differ in their arguments and not in their assembly.
    """
    # Through `to_thread`, for chapter -1's rule (F00-09, `ASYNC240`): the
    # linter does not know this program runs one task at a time, and the day
    # somebody runs the set concurrently is not the day to find out.
    await asyncio.to_thread(_materialise, workspace, task.files)

    session = Session(mode=task.mode, approver=AllowAll())
    tools = local_tools(workspace, session)
    wiring = wiring or Wiring()
    agent = wiring.agent(
        build_model(tools.schemas),
        tools,
        max_turns=task.max_turns,
        instructions=instructions,
    )

    error: str | None = None
    calls_made: list[tuple[str, dict[str, Any]]] = []
    final_text, stop_reason, turns = "", "error", 0
    try:
        outcome = await agent.run(task.question)
    except Exception as exc:  # a failed run is a data point, not a crashed probe
        error = f"{type(exc).__name__}: {exc}"
    else:
        final_text, stop_reason, turns = (
            outcome.final_text,
            outcome.stop_reason,
            outcome.turns_used,
        )
        for item in outcome.history.items:
            for call in getattr(item, "tool_calls", ()) or ():
                calls_made.append((call.name, call.arguments or {}))

    trajectory = Trajectory(
        calls=tuple(calls_made),
        final_text=final_text,
        stop_reason=stop_reason,
        turns=turns,
        files=await asyncio.to_thread(_snapshot, workspace),
    )
    passed = tuple(c.name for c in task.checks if error is None and c.holds(trajectory))
    failed = tuple(c.name for c in task.checks if c.name not in passed)
    return Result(task, trajectory, passed, failed, error)
```

`run_task` 分四步看。

**一、布置。**

> - `await asyncio.to_thread(_materialise, workspace, task.files)`：写文件是会卡住的操作，按第 0 章的规矩丢到线程里做（ruff 的 `ASYNC240` 会盯着）。
> - `Session(mode=task.mode, approver=AllowAll())`：第 5 章的权限会话；`AllowAll()` 是"问什么都同意"的审批者——评估时没有人坐在旁边点确认。
> - `local_tools(workspace, session)`：插曲 B 的函数，造出"读文件、跑命令、改文件"这一套本地工具，**都指向这个工作区**。
> - **`build_model` 是一个"造模型的函数"而不是一个模型**：第 9 章起，模型客户端要带着自己的工具清单，而工具清单要等上一行造完才知道。所以这里把清单传进去、让调用的人现造。`__main__` 里是同一个形状。

**二、跑。**

> - `try / except Exception / else`：`else` 里的代码只在**没有异常**时执行。一次崩掉的运行也是一个数据点（记成 `error`），不该把整个评估带崩。
> - 运行成功后，从历史里把每一次工具调用找出来：`getattr(item, "tool_calls", ()) or ()`——历史里只有"模型的回复"那种条目有 `tool_calls`，别的没有；没有或者是 `None` 都当成空。

**三、拍照。**

> - `Trajectory(...)` 把上面那些装起来；`files` 是这时候工作区的快照。

**四、判。**

> - `passed`：成立的检查的名字。`if error is None and c.holds(trajectory)`——崩掉的运行，所有检查一律不算通过。
> - `failed`：不在 `passed` 里的那些。
> - **`Result.ok`：`not self.failed and self.error is None`——"没有一条失败"，而不是"有一条通过"。** 这两种定义差在哪，§19.2 的变异检查问过一次。

### 11.3 六个任务

```python
# The task set.  Hand-written, in the repository, versioned with the code.
#
# Six tasks, chosen so that the *first* thing a change to the system prompt can
# damage is represented: three where acting immediately is right, one where
# asking is right, one where the work is verification rather than editing, and
# one where the correct answer is to leave something alone.  A task set that is
# six variations of "edit this file" measures one behaviour six times.
```

```python
CALC = """def add(a, b):
    return a + b


def multiply(a, b):
    result = a * b
    return result
"""


TEST_CALC = """from calc import add, multiply


def test_add():
    assert add(1, 2) == 3


def test_multiply():
    assert multiply(2, 3) == 6
"""


BROKEN = """def slugify(title):
    return title.lower().replace(" ", "_")
"""


TEST_BROKEN = """from slug import slugify


def test_slugify():
    assert slugify("Hello World") == "hello-world"
"""


TASKS: tuple[Task, ...] = (
    Task(
        name="add-function",
        files={"calc.py": CALC, "test_calc.py": TEST_CALC},
        question="add a subtract function to calc.py",
        checks=(
            calls("apply_patch"),
            file_has("calc.py", "def subtract"),
            file_has("calc.py", "def multiply"),
            finished(),
        ),
    ),
    Task(
        name="fix-failing-test",
        files={"slug.py": BROKEN, "test_slug.py": TEST_BROKEN},
        question="test_slug.py fails. Make it pass without changing the test.",
        checks=(
            file_has("slug.py", '"-"'),
            file_is("test_slug.py", TEST_BROKEN),
            finished(),
        ),
    ),
    Task(
        name="read-and-answer",
        files={"calc.py": CALC},
        question="which functions does calc.py define?",
        checks=(
            calls("read_file"),
            never_calls("apply_patch"),
            answer_has("multiply"),
            finished(),
        ),
    ),
    Task(
        name="leave-it-alone",
        files={"calc.py": CALC, "test_calc.py": TEST_CALC},
        question="add a docstring to the add function in calc.py. Change nothing else.",
        checks=(
            file_has("calc.py", '"""'),
            file_has("calc.py", "result = a * b"),
            file_is("test_calc.py", TEST_CALC),
            finished(),
        ),
    ),
    Task(
        name="ambiguous",
        files={"calc.py": CALC},
        question="add input validation to calc.py",
        checks=(asks_a_question(), never_calls("apply_patch"), finished()),
    ),
    Task(
        name="two-files",
        files={"calc.py": CALC, "test_calc.py": TEST_CALC},
        question="rename multiply to times everywhere, including the test",
        checks=(
            file_has("calc.py", "def times"),
            file_lacks("calc.py", "def multiply"),
            file_has("test_calc.py", "times("),
            finished(),
        ),
    ),
)


def by_name(name: str) -> Task:
    for task in TASKS:
        if task.name == name:
            return task
    raise KeyError(name)


def declared_files() -> set[str]:
    """Every path any task writes.  F14-08's assertion reads this."""
    return {name for task in TASKS for name in task.files}
```

| 任务 | 正确的行为 |
|---|---|
| `add-function` | 直接动手：加一个函数 |
| `fix-failing-test` | 直接动手：改实现，**不许改测试** |
| `read-and-answer` | 只读不改，答案是文字 |
| `leave-it-alone` | 改一处，**别的一个字都不许动** |
| `ambiguous` | **别动手**，先问一个具体的问题 |
| `two-files` | 两个文件要改得一致 |

> - 选任务的标准写在那段注释里：**六个"改这个文件"的变体，是把一种行为量了六遍。** 改系统提示词最先可能伤到的几种行为——该动手时动手、该问时问、该只看不改时不改、该别碰的不碰——每种至少有一个任务。
> - `fix-failing-test` 的检查 `file_is("test_slug.py", TEST_BROKEN)`：让一个失败的测试通过，最省事的办法是改测试。这条检查就是防这个的。
> - `leave-it-alone` 的 `file_has("calc.py", "result = a * b")`：`multiply` 里那个多余的中间变量是**故意放的诱饵**。让它加一句说明，看它会不会顺手把旁边"不够简洁"的代码也改了。
> - 任务是**代码**，和框架放在同一个文件里。分开放（比如一个 JSON）的话，检查要引用那段原文（`file_is("test_calc.py", TEST_CALC)`），就得两边各存一份。**一个任务是"输入 + 判据"，判据是代码，所以任务是代码。**
> - **没有 `EvalRunner` 类。** 任务是数据，跑一个任务是一个函数，比较两组结果是另一个函数。调用它们的只有两种地方（探针、测试）——插曲 B 的规矩：有三种调用方再考虑加一层。

提交：

```
feat(evals): six tasks, trajectory checks, and run_task()

Checks read the file on disk and the tool calls, not the answer text.
answer_has() exists, is used once, and says in its docstring that it is
the row which can go red because a model changed its phrasing.
```

---

## §12 F14-08：评估集先撞出了一个产品的 bug

### 12.1 第一次真跑

六个任务里有一个是"把 `multiply` 全部改名叫 `times`，包括测试"。工作区里只有两个文件。第一次跑，它调的工具是这样的：

```
read_file  {'path': 'src/multiply.py'}
read_file  {'path': 'tests/test_multiply.py'}
run_shell  {'command': 'ls -R'}
run_shell  {'command': "grep -rnw './src/' -e 'multiply'"}
run_shell  {'command': "grep -rnw './tests/' -e 'multiply'"}
apply_patch {'edits': [{'path': 'src/minicodex/evals.py',
                        'old_text': 'def multiply(a, b):', 'new_text': 'def times(a, b):'}, ...
run_shell  {'command': "grep -rnw './src/' -e 'times'"}
read_file  {'path': 'tests/test_agent.py'}
```

（实测于 2026-08。）

第六行：`src/minicodex/evals.py`。**给它打分的那个文件。**

工作区里没有 `src/` 目录。它是怎么看见的？第三行：`ls -R`。

`local_tools(workspace, session)` 把"根目录"设成了工作区，`read_file` 和 `apply_patch` 都老老实实按这个根目录找文件。而 `run_shell`——看第 2 章写的 `ShellSession`：

```python
class ShellSession:
    def __init__(self, *, timeout: float = DEFAULT_TIMEOUT) -> None:
        self.cwd = os.getcwd()
```

`os.getcwd()`：**启动这个程序的那个目录**。第 2 章起，没有任何地方把"根目录"传给它——因为在命令行里，根目录就是 `Path.cwd()`，两者永远相同。

于是这个 Agent 的两类工具对"这里是哪里"的答案不一样，十二章没有人发现。评估是**第一个**把这两个目录分开的调用者：工作区是一个临时目录，而程序是在仓库里启动的。Agent 用 shell 一看，看到的是整个仓库。

**唯一挡住那次写入的，是第 4 章的路径边界**（F04-12：路径先解析再判断，根目录之外的一律拒绝）。如果当时 `apply_patch` 也跟着 shell 的目录走，这次评估会把打分的代码改掉，然后——多半——剩下的任务照跑，分数照出。

### 12.2 修

`shell.py`，`ShellSession.__init__`：

```python
def __init__(
    self, *, cwd: str | os.PathLike[str] | None = None, timeout: float = DEFAULT_TIMEOUT
) -> None:
    # Where this conversation is standing.  It defaulted to the *process's*
    # working directory for twelve chapters, and `tool_context(root=X)`
    # never passed anything else -- so `read_file` and `apply_patch` were
    # bounded by `root` while `run_shell` was bounded by wherever the
    # program happened to be started.  In the CLI the two are the same
    # directory, which is why nothing noticed.
    #
    # Chapter 14 is the first caller that separates them: an eval task's
    # workspace is a temporary directory and the process is running in the
    # repository.  The first real eval run answered "rename multiply to
    # times everywhere" by grepping *this project's own source tree* and
    # trying to patch `src/minicodex/evals.py` -- the file holding the
    # checks it was being graded against.  That edit was refused only
    # because chapter 4 resolves paths against `root` (F04-12).
    self.cwd = os.fspath(cwd) if cwd is not None else os.getcwd()
```

> - 多了一个参数 `cwd`，类型 `str | os.PathLike[str] | None`：字符串、`Path` 这类"像路径的东西"，或者不给。
> - `os.fspath(cwd)`：把 `Path` 之类的变成普通字符串。不给就还是原来的 `os.getcwd()`——以前所有直接写 `ShellSession()` 的地方照旧。

`tools.py`，`tool_context()` 的结尾：

```python
resolved = (root or Path.cwd()).resolve()
return ToolContext(
    root=resolved,
    # The shell starts where the other tools are pointed.  Not passing this
    # is F14-08's live form: `read_file` and `apply_patch` are bounded by
    # `root`, `run_shell` was bounded by the process, and the difference is
    # invisible until somebody points an agent at a directory it was not
    # started in.
    shell=shell or ShellSession(cwd=resolved),
    session=session or Session(),
)
```

> 以前是 `shell=shell or ShellSession()`。现在把解析好的根目录传给它。

改完跑全套测试：**全绿**。这既说明修复没碰坏别的，也说明——**没有任何一个已有的测试在看"shell 从哪里开始"**。补上：

```python
def test_F14_08_the_shell_starts_where_the_other_tools_are_pointed(tmp_path: Path) -> None:
    """`read_file` was bounded by `root` and `run_shell` by the process.

    Twelve chapters of CLI runs never separated the two, because `_ask` passes
    `Path.cwd()` as the root.  The first eval run did separate them, and the
    agent -- pointed at a two-file workspace -- ran `ls -R`, found this
    repository, and tried to `apply_patch` `src/minicodex/evals.py`: the file
    holding the checks it was being graded against.  Chapter 4's containment
    (F04-12) refused the write, which is the only reason this is a story about
    a near miss.
    """
    context = tool_context(root=tmp_path, session=Session(approver=AllowAll()))
    assert Path(context.shell.cwd).resolve() == tmp_path.resolve()


async def test_F14_08_a_task_workspace_holds_exactly_what_the_task_declared(
    tmp_path: Path,
) -> None:
    """The precondition chapters 16 and 17 inherit.

    An agent with a memory reads whatever is in reach.  If the eval's own
    fixture, its expected answer, or the test file that grades it is in reach,
    the score measures the search path.
    """

    class Silent:
        tools: ClassVar[list[dict[str, Any]]] = []

        async def stream(self, messages: Any) -> Any:
            from minicodex.model import Completed, TextDelta

            yield TextDelta("nothing to do")
            yield Completed("stop")

    task = by_name("add-function")
    result = await run_task(task, lambda _: Silent(), workspace=tmp_path / "ws")
    assert set(result.trajectory.files) == set(task.files)


def test_F14_08_no_task_declares_a_path_out_of_its_workspace() -> None:
    for name in declared_files():
        assert not Path(name).is_absolute()
        assert ".." not in Path(name).parts
```

> - 第二个测试里的 `Silent` 是一个什么都不做的模型（说一句话就结束）。跑完之后，工作区里的文件必须**恰好**是任务声明的那些——不多不少。`set(...) == set(...)` 比的是两个集合。`tools: ClassVar[list[...]] = []` 的 `ClassVar` 是告诉类型检查器"这是类本身的属性，不是每个对象各一份"。
> - 第三个：没有哪个任务声明的路径是绝对路径，或者带 `..`（往上跳）。

同一个任务，修完之后：

```
read_file   {'path': 'src/multiply.py'}
run_shell   {'command': 'ls src/'}
run_shell   {'command': 'ls'}
read_file   {'path': 'calc.py'}
read_file   {'path': 'test_calc.py'}
apply_patch {'edits': [{'path': 'calc.py', 'old_text': 'def multiply(a, b): ...
apply_patch {'edits': [{'path': 'test_calc.py', ...
ok= True
```

模型还是先猜了一个不存在的路径，`ls` 一下，然后正确地干完了。提交：

```
fix(shell): the shell starts where the other tools are pointed

ShellSession took its cwd from os.getcwd() and tool_context(root=X)
never passed X. Identical in the CLI, which is why twelve chapters did
not notice. Found by the first real eval run: pointed at a two-file
workspace, the agent ran `ls -R`, found this repository and tried to
patch src/minicodex/evals.py -- the file holding the checks it was being
graded against. Chapter 4's containment refused the write.
```

### 12.3 改写时发现：那段探针"量"出来的结论，是一句 `print`

探针里有一段 `leak`，本意是量"评估时 Agent 到底够得着什么"。第一版的结尾是：

```python
    print("  ...and is not reachable through `paths.resolve()` from that root.")
```

**这句话没有被任何代码检查过。** 它只是被打印出来，看上去像一个测量结果。

把它换成真的去问——用 Agent 自己的工具：

```python
# Ask, with the agent's own tools, for the three things F14-08 is about.
# This section used to end with a printed sentence -- "...and is not
# reachable through `paths.resolve()` from that root" -- that nothing had
# checked: a probe reporting a conclusion it never measured.
tools = local_tools(path, Session(mode="workspace-write", approver=AllowAll()))
asked = [
    ("run_shell", {"command": "pwd"}),
    ("read_file", {"path": str(target)}),
    ("read_file", {"path": climbing}),
    # The honest fourth line.  The shell *starts* in the workspace; nothing
    # keeps it there until chapter 20 builds a sandbox.
    ("run_shell", {"command": f"head -c 40 {climbing}"}),
]
for name, arguments in asked:
    output = (await tools.handlers[name](arguments)).strip().splitlines()
    print(f"  {name}({json.dumps(arguments)[:60]})\n      -> {(output or [''])[0][:100]}")
```

```
$ uv run python probe_eval.py leak
  the agent's root contains: ['calc.py', 'test_calc.py']
  the repository is at:      /home/user/codex-from-scratch/steps/step14_eval
  run_shell({"command": "pwd"})
      -> /home/user/codex-from-scratch/steps/step14_eval/.probe/ch14/leak
  read_file({"path": "/home/user/codex-from-scratch/steps/step14_eval/sr)
      -> Error: that path is outside the repository, and this tool only touches files inside it ...
  read_file({"path": "../../../src/minicodex/evals.py"})
      -> Error: that path is outside the repository, and this tool only touches files inside it ...
  run_shell({"command": "head -c 40 ../../../src/minicodex/evals.py"})
      -> """A task set, and what "it worked" mean
```

前三行是好消息：shell 从工作区出发；`read_file` 用绝对路径、用 `../` 往上爬，都被拒绝。

**第四行是那句 `print` 一直盖着的实话：shell 读得到。** §12.2 修的是"shell 从哪里**出发**"；没有任何东西规定它**不许走出去**——`../../../` 三层就回到了仓库。真正把它关住，要等第 20 章的沙箱。

所以 F14-08 在这一章的准确状态是：**默认情况下不会再撞上**（Agent 一开始 `ls` 看到的只有任务的文件），**但没有被关住**。评估的工作区现在建在仓库里面的 `.probe/` 下，这本身就是隐患；章末练习 3 是把它挪出去。

> **为什么这一条是第 16、17 章的前提。** 那两章给 Agent 加记忆。一个带记忆的 Agent 会去读它够得着的一切。如果评估任务的工作区里（或者够得着的地方）有评估自己的材料、期望的答案、给它打分的文件，那分数量的就是"它找没找到答案"，不是"它会不会做"。

---

## §13 F14-07：逐个任务比，而且专门去找变差的那个

### 13.1 被测的对象

第 13 章往 `system.md` 里加了一句话——"拿不准就问一个具体的问题，别猜"。依据是**一个任务**上的测量。

可是系统提示词改一句，变的是**每一次**会话。在它为之设计的那个任务上变好了，不等于在别的任务上没变坏：比如它会不会让模型在明明很清楚的任务上也开始反问？

### 13.2 `Arm`、`table`、`regressions`

```python
@dataclass
class Arm:
    """One configuration, run over the whole task set some number of times."""

    name: str
    results: list[Result] = field(default_factory=list)

    def rate(self, task: str) -> tuple[int, int]:
        rows = [r for r in self.results if r.task.name == task]
        return sum(1 for r in rows if r.ok), len(rows)

    @property
    def total(self) -> tuple[int, int]:
        return sum(1 for r in self.results if r.ok), len(self.results)


def table(arms: Sequence[Arm], tasks: Sequence[Task]) -> str:
    """Per task, per arm.  Never only the total.

    The total is the number that hides the thing worth knowing: two arms at
    18/24 can disagree about every task in the set.  F14-07 is that sentence.
    """
    width = max(len(t.name) for t in tasks) + 2
    lines = ["  " + "task".ljust(width) + "  ".join(a.name.rjust(12) for a in arms)]
    for task in tasks:
        cells = []
        for arm in arms:
            ok, total = arm.rate(task.name)
            cells.append(f"{ok}/{total}".rjust(12))
        lines.append("  " + task.name.ljust(width) + "  ".join(cells))
    totals = []
    for arm in arms:
        ok, total = arm.total
        totals.append(f"{ok}/{total}".rjust(12))
    lines.append("  " + "TOTAL".ljust(width) + "  ".join(totals))
    return "\n".join(lines)


def regressions(before: Arm, after: Arm, tasks: Sequence[Task]) -> list[str]:
    """Tasks the change made worse, whatever it did to the average.

    This function is the whole point of the module.  A change that takes the
    total from 18/24 to 20/24 by fixing three tasks and breaking one is not
    "an improvement"; it is a trade, and somebody has to be told which trade.
    """
    worse = []
    for task in tasks:
        was, _ = before.rate(task.name)
        now, _ = after.rate(task.name)
        if now < was:
            worse.append(f"{task.name}: {was} -> {now}")
    return worse


def failures(arm: Arm) -> list[str]:
    """Every failed check, with the trajectory that produced it."""
    lines = []
    for result in arm.results:
        if result.ok:
            continue
        detail = result.error or ", ".join(result.failed)
        lines.append(f"  {result.task.name}: {detail}\n      {result.trajectory.describe()}")
    return lines
```

> - **`Arm`（臂）**：一种配置跑完整个任务集的全部结果。第 13 章的词：对照实验里的一组。
> - `rate(task)` 返回 `(做对几次, 一共几次)`。`sum(1 for r in rows if r.ok)` 是"数一数满足条件的有几个"的常见写法。
> - **`table`**：每个任务一行，每个臂一列，**总分只是最后一行**。`max(len(t.name) for t in tasks) + 2` 算出最长的任务名有多宽，`ljust`/`rjust` 用空格补齐，列才对得上。
> - **`regressions`——这个模块存在的理由。** 逐个任务比"改之前做对几次、改之后做对几次"，变少了的记下来。
> - **`failures`**：每一次没做对的运行，印出哪条检查没过、以及它当时调了哪些工具。光知道"2/3"不够，得能看到那一次到底干了什么。

为什么不能只看总分——说明里那个例子值得展开：总分从 18/24 涨到 20/24，可能是修好了三个任务、弄坏了一个。这不叫"变好了"，这叫**一笔交换**，而得有人被告知换掉的是哪个。

```python
def _result(task: Task, ok: bool) -> Result:
    trajectory = _trajectory()
    return Result(task, trajectory, ("check",) if ok else (), () if ok else ("check",))


def test_F14_07_a_better_total_can_hide_a_task_that_got_worse() -> None:
    """Two arms, 3/6 against 4/6, and the better one broke something.

    This is the whole reason `regressions()` exists and the whole reason the
    report prints a row per task.  A change that fixes two tasks and breaks one
    is a trade; reporting only the total hides which trade.
    """
    a, b, c = TASKS[0], TASKS[1], TASKS[2]
    before = Arm("before", [_result(a, True), _result(b, False), _result(c, False)])
    after = Arm("after", [_result(a, False), _result(b, True), _result(c, True)])

    assert before.total == (1, 3)
    assert after.total == (2, 3)
    assert regressions(before, after, [a, b, c]) == [f"{a.name}: 1 -> 0"]


def test_F14_07_a_task_with_one_failed_check_has_failed() -> None:
    """`ok` is "nothing failed", not "something passed".

    Found by mutation testing rather than by reading: every other test in this
    file builds a result whose checks all pass or all fail, and the two
    definitions agree on those.  A four-check task that edited the right file
    and also deleted the test file passes under the wrong one.
    """
    mixed = Result(TASKS[0], _trajectory(), passed=("wrote the file",), failed=("left the test",))
    assert not mixed.ok
    assert Arm("a", [mixed]).total == (0, 1)


def test_F14_07_a_task_set_covers_more_than_one_kind_of_right_answer() -> None:
    """A set of six variations on "edit this file" measures one behaviour six
    times.  The one guard that can be automated: at least one task whose
    correct trajectory contains no edit at all."""
    reads_only = [t for t in TASKS if any(c.name == "never calls apply_patch" for c in t.checks)]
    assert len(reads_only) >= 2, [t.name for t in TASKS]
```

> - 第一个测试造了这样两组：改之前 1/3，改之后 2/3——总分更好；但第一个任务从对变成了错。`regressions` 必须把它点出来。
> - 第二个：一个结果有一条检查过了、一条没过，它是**失败**。这个测试是变异检查逼出来的（§19.2）。
> - 第三个是唯一能自动守住的"任务集别偏科"：至少两个任务的正确做法是**不改任何文件**。

提交：

```
feat(evals): A/B report with one row per task, and regressions()

A total that improves while one task breaks is a trade, and somebody has
to be told which trade. table() prints a row per task; regressions()
names the ones that got worse, whatever happened to the sum.
```

### 13.3 量

两个臂只差这一句话，六个任务各三次，两个模型：

```
$ uv run python probe_eval.py ab --provider openai --samples 3

  task                   without          with
  add-function               3/3           3/3
  fix-failing-test           3/3           3/3
  read-and-answer            3/3           3/3
  leave-it-alone             3/3           3/3
  ambiguous                  0/3           3/3
  two-files                  3/3           3/3
  TOTAL                    15/18         18/18

  made worse by the sentence: (none)
```

```
$ uv run python probe_eval.py ab --provider ollama --samples 3

  ambiguous                  0/3           0/3
  TOTAL                    15/18         15/18

  made worse by the sentence: (none)
```

（实测于 2026-08。）

**这句话在更宽的任务集上站得住。** 第一个模型上，它把自己要解决的那个任务从 0/3 拉到 3/3，其余五个一动不动；第二个模型上它什么都没改变（这也正是第 13 章在那个模型上量到的），**并且也没有弄坏别的**。

### 13.4 这张表自己说不出来的事

**六个任务里五个在两个臂都是 3/3。** 一个到处饱和的评估集：

- 量得出**坏掉**（3/3 掉到 0/3 立刻显形）；
- **量不出变好**（已经满分，没有上升的余地）；
- 每个任务三次时，看得见的最小变化是"某个任务掉了 1/3"。

而 §10.1 那次五个样本的测量里，恰好出现过一个更小的东西：`add-function` 的第五次没有动手，反而问了一个问题——**在一个并不含糊的任务上**。那正是"拿不准就问"这句话可能造成的伤害的样子。于是单独加测这一个任务，两个臂各十次：

```
$ uv run python probe_eval.py focus --provider openai --samples 10 --tasks add-function

  task               without          with
  add-function         10/10         10/10
  without: asked a question 0/10, edited 10
  with: asked a question 0/10, edited 10
```

（实测于 2026-08。）

**0/10，没复现。** 三次测量合起来：加了那句话的一侧 18 次里出现过 1 次，没加的一侧 13 次里 0 次。31 次里的 1 次，**归不到任何原因上**。

这就是这次测量诚实的终点：这里值得担心的那个效应，比手头样本能分辨的更小。

写下来比不写重要。**"没有观测到变差"和"没有变差"是两句不同的话**，中间隔着的是样本量，而样本量是钱和时间。

---

## §14 F14-06：让模型当裁判

评估集的六个任务都是程序能判的：文件里有没有 `def subtract`、测试文件动没动。可有些事程序判不了（"这段解释清楚吗"）。常见的做法是让另一个模型来打分。清单说：裁判自己有偏好。量一下偏在哪。

同一个问题（"calc.py 里有哪些函数"），三个答案：

- **A** 正确，一行：`calc.py defines add and multiply.`
- **B** 正确，啰嗦：加粗、编号、"Let me know if you would like me to add type hints!"
- **C** **错误**但自信：`It defines three functions: add, multiply and divide, all fully tested and documented.`（没有 `divide`）

让裁判两两比较，然后**把两个答案的先后对调再问一遍**：

```
$ uv run python probe_eval.py judge --provider openai --samples 5

  terse-correct vs verbose-correct
    as A/B: ['B', 'B', 'B', 'B', 'B']
    as B/A: ['A', 'A', 'A', 'A', 'A']
    first answer preferred 0/10
    verdict survives swapping the order: 5/5

  terse-correct vs verbose-wrong
    as A/B: ['B', 'B', 'B', 'B', 'B']
    as B/A: ['A', 'A', 'A', 'A', 'A']
    first answer preferred 0/10
    verdict survives swapping the order: 5/5
```

```
$ uv run python probe_eval.py judge --provider ollama --samples 5

  terse-correct vs verbose-correct
    first answer preferred 10/10
    verdict survives swapping the order: 5/5

  terse-correct vs verbose-wrong
    first answer preferred 0/10
    verdict survives swapping the order: 5/5
```

（实测于 2026-08。）

怎么读："as A/B" 是简短的那个排在前面时裁判选了谁；"as B/A" 是对调之后。"first answer preferred" 数的是**简短的那个**（每一组里排在前面介绍的那个）一共被选中几次。

三个结果，一个比一个难看：

1. **位置偏好：没有。** 对调顺序，判决不变（5/5）。这是最常被提起的一种偏见，在这个任务上不成立。
2. **风格偏好：有，而且是一边倒的。** 两个答案都正确时，一个裁判 **10 次全选长的**，另一个 **10 次全选短的**。同一批数据，换个裁判，结论完全相反，而且都是十比零的那种确定。
3. **两个裁判都 10 次全选了那个错的。** 一个凭空多出一个 `divide`、还宣称"全部测过、有文档"的回答，在两个模型那里都赢了正确的那个。

第三条决定了这一章不往评估里放裁判。判据很简单：

> **程序能查的事情，不要交给裁判。**

裁判的用武之地是程序查不了的那类问题，而"这个答案对不对"在这个项目里从来不属于那一类。

顺带：这一节的实验一共十几次模型调用、两分钟、几美分。"让模型当裁判靠不靠谱"是可以自己量的，不需要相信别人的结论。

---
## §15 F14-02：不稳定是缺陷，不是心情

清单：测试偶尔失败，大家习惯了"重跑一下"，真的 bug 被淹没。

这一条的难处不在技术。一个二十次红一次的测试会被重跑，重跑就绿了，然后被忘掉；从那以后，**每一次真的红都有了一个现成的解释**："哦，那个测试本来就不太稳。"

第 11 章老实记过一条：有一个测试在一次完整运行里红过一次，之后七次没有复现，原因不明，"写下来而不是重跑掉"。写下来是对的，但写下来不是机制。这一章加的机制是一个脚本：把整套测试跑 N 遍，**哪个测试没有每次都给出同样的结论，就点它的名**。

新建 `probe_flaky.py`（放在项目根目录，和别的探针一起）。这是它的全部内容，后面逐段讲它是怎么长成这样的：

```python
"""Run the suite N times and report anything that did not decide the same way twice.

    uv run python probe_flaky.py [--runs 5] [--target tests/]

F14-02's entry says "flakiness is a defect", which is easy to write and needs a
mechanism, because the failure mode is social: a test that fails once in twenty
runs gets re-run, goes green, and is forgotten -- and after that every real
failure has a ready-made explanation.  Chapter 11 recorded one honestly
(`test_F_1_01_built_wheel_actually_contains_the_data_file`, red once in eight
runs, cause unknown) precisely so that it would not become that.

This does not make the suite deterministic.  It makes non-determinism *visible*
and *named*: a test that appears in this report is a defect with a number, not
a mood.

**The property seed is deliberately held fixed**, and the first version got
this wrong in an instructive way.  Varying it per run looked obviously right --
"a suite that is only stable because it runs the same 200 random cases is
stable about nothing" -- but chapter 6's property tests are parametrised *by
the seed*, so the test ids change with it:

    test_property_compaction_never_grows_the_history[7000]

A thousand new ids per run means those thousand tests are never compared with
anything, and the summary line dutifully reported **5548 distinct tests over 5
runs** as though that were a number of tests.  The exploration those seeds buy
is a different job, and it already has a home: the `MINICODEX_PROPERTY_CASES:
2000` step in `ci.yml`.  This tool answers one question -- *is the suite
deterministic* -- and answering it requires holding the input still.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
import time
from collections import defaultdict

# `.+?` and not `\S+`, and that was the third bug in this file rather than a
# style preference: ten parametrised ids in this repository contain spaces
#
#     ...::test_a_malformed_plan_says_what_to_send_instead[argument3-not an
#     object-Each step is] PASSED
#
# so a name pattern of `\S+` stopped at the first space, the status did not
# follow, and the line was dropped -- silently, into the same "I did not see
# it" shape as the two bugs above.  1548 parsed against 1558 reported, which
# nothing would have noticed without counting both.
RESULT = re.compile(r"^(?P<name>tests/.+?)\s+(?P<status>PASSED|FAILED|ERROR|SKIPPED)(\s|$)")


def one_run(target: str) -> tuple[dict[str, str], float]:
    began = time.monotonic()
    result = subprocess.run(
        # `-vv`, not `-v`.  `pyproject.toml` puts `-q` in `addopts`, so `-v`
        # nets back to the default dotted output and this parser sees zero
        # tests -- which the first version then reported as "every test decided
        # the same way every time".  A tool that cannot see failures reports
        # none; chapter 6 wrote that down about its mutation script and it is
        # true of every checker in this repository.
        [sys.executable, "-m", "pytest", target, "-vv", "--no-header"],
        capture_output=True,
        text=True,
    )
    outcomes: dict[str, str] = {}
    for line in result.stdout.splitlines():
        match = RESULT.match(line.replace("\\", "/"))
        if match:
            outcomes[match["name"]] = match["status"]
    if not outcomes:
        raise SystemExit(
            "probe_flaky.py parsed no test results at all. That is a bug in this "
            "script, not a green suite:\n" + result.stdout[-2000:]
        )
    return outcomes, time.monotonic() - began


def verdict(runs: list[dict[str, str]]) -> tuple[dict[str, list[str]], list[str]]:
    """Which tests changed their mind, and which were not there every time.

    The second list is the fourth "I did not see it" in this file, found when
    the chapter was rewritten.  The first three versions only ever asked *did a
    test give two different answers*; a test that passes in run 0 and is simply
    absent from run 1 -- not collected, or its line not parsed -- gave one
    answer, once, and was counted as stable.  There was a `missing` list for
    this, and its condition was `len(statuses) == 1 and not statuses`, which
    no set can satisfy.
    """
    seen: dict[str, set[str]] = defaultdict(set)
    appeared: dict[str, int] = defaultdict(int)
    for outcomes in runs:
        for name, status in outcomes.items():
            seen[name].add(status)
            appeared[name] += 1
    unstable = {name: sorted(statuses) for name, statuses in seen.items() if len(statuses) > 1}
    absent = sorted(name for name, count in appeared.items() if count < len(runs))
    return unstable, absent


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runs", type=int, default=5)
    parser.add_argument("--target", default="tests")
    args = parser.parse_args()

    runs = []
    times = []
    for index in range(args.runs):
        outcomes, elapsed = one_run(args.target)
        runs.append(outcomes)
        times.append(elapsed)
        failed = sum(1 for s in outcomes.values() if s in ("FAILED", "ERROR"))
        print(f"  run {index}: {len(outcomes)} tests, {failed} failed, {elapsed:.1f}s")

    unstable, absent = verdict(runs)
    distinct = len({name for outcomes in runs for name in outcomes})
    print(f"\n  {distinct} distinct tests over {args.runs} runs")
    print(f"  wall clock: {sum(times):.1f}s total, {sum(times) / len(times):.1f}s per run")
    if unstable:
        print(f"\n  {len(unstable)} test(s) did not decide the same way every time:")
        for name, statuses in sorted(unstable.items()):
            print(f"    {name}: {statuses}")
    if absent:
        print(f"\n  {len(absent)} test(s) were not there in every run:")
        for name in absent[:20]:
            print(f"    {name}")
    if unstable or absent:
        return 1
    print("  every test decided the same way every time.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

> - **`subprocess.run([...], capture_output=True, text=True)`**：启动另一个程序（这里是 `python -m pytest`），等它跑完，把它打印的东西作为字符串拿回来（`result.stdout`）。`sys.executable` 是"当前这个 Python"的路径，保证用的是同一个环境。
> - **`RESULT`** 是一个正则表达式，用来认 pytest 输出里这样的行：`tests/test_x.py::test_name PASSED`。`(?P<name>...)` 给括号里匹配到的部分起名叫 `name`，之后用 `match["name"]` 取。
> - **`defaultdict(set)`**：一个字典，取一个不存在的键时自动给一个空集合。`seen[name].add(status)` 不用先判断 `name` 在不在。
> - **`raise SystemExit(main())`**：`main()` 返回的整数成为退出码。有不稳定的测试就返回 1，CI 会红。

这个脚本一百三十多行，写它的过程中出了**四个** bug。四个是同一个形状。

### 15.1 第一版：它报告一切稳定，而它一个测试都没看见

```
  run 0: 0 tests, 0 failed, 98.3s
  run 1: 0 tests, 0 failed, 93.8s
  run 2: 0 tests, 0 failed, 93.0s
```

`0 tests`。项目的 `pyproject.toml` 给 pytest 配了默认参数 `-q`（安静），我又传了一个 `-v`（详细），两者相抵，回到每个测试打一个点的输出。正则要找的是 `...PASSED` 那样的行，一行都没有——**然后它准备报告"每个测试每次的结论都一样"。**

确实都一样：零个对零个。

修法两处：改成 `-vv`；并且**一个结果都解析不到就直接报错**，不许把"我什么都没看见"报成"一切正常"（`one_run` 里那个 `if not outcomes: raise SystemExit(...)`）。

### 15.2 第二版：`5548 distinct tests over 5 runs`

```
  run 0: 1548 tests, 0 failed, 100.6s
  ...
  5548 distinct tests over 5 runs
  every test decided the same way every time.
```

每轮 1548 个，五轮之后"不重复的测试"有 **5548 个**。1548 + 4 × 1000。

原因是我自以为聪明的一手：每一轮换一个随机种子——"只在同样的 200 个随机用例上稳定，那叫什么稳定"。可是第 6 章那 1000 个属性测试的**名字里带着种子**：

```
tests/test_properties.py::test_property_compaction_never_grows_the_history[7000]
```

种子一换，名字就换了。于是那 1000 个测试每一轮都是全新的名字，**永远不会和任何东西比较**，而汇总那一行还一本正经地把它们数了进去。

修法是把种子**按住不动**。"每轮换种子"听起来更严格，其实是把问题偷换了：这个工具要回答的是**"同样的输入，会不会给出不同的结论"**——那输入就必须一样。换种子去探索更多情况是另一件事，它已经有地方做了（`ci.yml` 里用 2000 个用例跑属性测试的那一步）。

### 15.3 第三个：`1548` 和 `1558` 对不上

按住种子之后，每轮稳定报 `1548 tests`。而 pytest 自己说的是 `1549 passed, 9 skipped`——1558 条结论。差十条。

把没匹配上的行印出来：

```
tests/test_faults_ch11.py::test_a_malformed_plan_says_what_to_send_instead[argument3-not an object-Each step is] PASSED [ 19%]
...
total unmatched: 10
```

**这些测试的名字里有空格。** 当时正则里匹配名字用的是 `\S+`（一串非空白字符），遇到第一个空格就停了，后面接不上 `PASSED`，整行被丢掉——又是不出声的。改成 `.+?`（任意字符，尽量少）。

### 15.4 第四个，改写时发现：上一轮在、这一轮不在的测试

前三版问的都是同一个问题：一个测试有没有给出过**两种不同的结论**。

那一个测试第 0 轮通过、第 1 轮**根本没出现**呢？没被收集到，或者那一行又没被认出来。它只给出过一种结论（通过，一次），于是被算作稳定。

第一版其实想到了这件事，有这么一行：

```python
    missing = [name for name, statuses in seen.items() if len(statuses) == 1 and not statuses]
```

读一下条件：`len(statuses) == 1`（集合里恰好一个元素）**并且** `not statuses`（集合是空的）。没有哪个集合能同时满足这两条。这个列表从写下来那天起就永远是空的，最后那句 `return 0 if not missing else 1` 永远返回 0。

一个检查"测试会不会时有时无"的工具，自己看不见一个测试消失。

现在的 `verdict()` 是把这部分逻辑单独拿出来的结果：除了"给出过几种结论"，还数每个测试**出现了几轮**，少于总轮数的列进 `absent`。拿出来成一个函数还有一个好处——它不启动 pytest、不花时间，可以直接测：

```python
def _flaky(repo_root: Path) -> Any:
    import importlib.util

    spec = importlib.util.spec_from_file_location("probe_flaky", repo_root / "probe_flaky.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_F14_02_the_parser_reads_a_test_id_with_spaces_in_it(repo_root: Path) -> None:
    """Ten parametrised ids in this repository contain spaces, and a name
    pattern of `\\S+` dropped all ten lines without a word: 1548 parsed
    against 1558 reported.  This is one of the ten, verbatim."""
    line = (
        "tests/test_faults_ch11.py::test_a_malformed_plan_says_what_to_send_instead"
        "[argument3-not an object-Each step is] PASSED [ 19%]"
    )
    match = _flaky(repo_root).RESULT.match(line)
    assert match is not None
    assert match["status"] == "PASSED"
    assert match["name"].endswith("Each step is]")


def test_F14_02_a_test_missing_from_one_run_is_not_called_stable(repo_root: Path) -> None:
    """Passing once and then not being there is not "the same way every time".

    The script had a `missing` list for this whose condition no set could
    satisfy, so for as long as it existed it reported nothing.  A flake checker
    that cannot see a test disappear has the blind spot it was written about.
    """
    verdict = _flaky(repo_root).verdict
    steady = {"tests/a.py::one": "PASSED", "tests/a.py::two": "PASSED"}

    assert verdict([steady, steady]) == ({}, [])
    assert verdict([steady, {"tests/a.py::one": "PASSED"}]) == ({}, ["tests/a.py::two"])
    unstable, absent = verdict([steady, {**steady, "tests/a.py::two": "FAILED"}])
    assert unstable == {"tests/a.py::two": ["FAILED", "PASSED"]} and absent == []
```

> - **`_flaky(repo_root)`**：`probe_flaky.py` 不在 `src/` 里，不能直接 `import`。`importlib.util` 这三行是"按文件路径把一个 `.py` 文件当模块加载进来"的标准写法。
> - 第一个测试用的就是 §15.3 那十行里的一行，原样。
> - 第二个：两轮完全一样 → 都稳定；第二轮少了一个测试 → 它出现在 `absent` 里；第二轮有一个从通过变成失败 → 它出现在 `unstable` 里。

### 15.5 跑一遍

```
$ uv run python probe_flaky.py --runs 5
  run 0: 1635 tests, 0 failed, 75.9s
  run 1: 1635 tests, 0 failed, 75.0s
  run 2: 1635 tests, 0 failed, 76.8s
  run 3: 1635 tests, 0 failed, 77.4s
  run 4: 1635 tests, 0 failed, 80.1s

  1635 distinct tests over 5 runs
  wall clock: 385.1s total, 77.0s per run
  every test decided the same way every time.
```

（这次在 Linux 上实测。）每轮的测试数和 `uv run pytest` 自己报的 `1635 passed` 对得上。**这一行数字有它的用处：它是这个检查器在说"全部稳定"之前，先交代自己看见了多少。**

五遍要好几分钟，所以它不进挡合并的那一层，§17 再说放哪。

四个 bug，没有一个让这个脚本**说错**了哪个测试的结论。四个都只是让它的视野小了一圈，而它照样理直气壮地报告"全部稳定"。和 §9 那四条摆在一起：

> **一个检查器最危险的毛病，是它的视野比它的报告小。** 每一个"全部通过"旁边，都该有一个数字回答"一共看了多少"，而且这个数字要能和别处对上。

---

## §16 F14-04：随机测试失败了，要让人看得懂

### 16.1 第 6 章欠的账

第 6 章写属性测试时没有用现成的库 `hypothesis`，自己写了一个按种子生成随机对话的函数，并且明说了代价：**没有"缩小"**——失败时只打印一个种子，要看到底是什么样的对话让它失败，得自己拿种子重新生成、打印出来、用眼睛读。

这笔账付过一次。第 6 章那个"压缩之后对话反而变大了"的 bug 出在种子 235 上，那个种子生成的对话有十四条，**其中起作用的只有一条**。报告说的是"235"。

"缩小"就是把读的这一步交给程序：只要失败还在，就继续删；删到再删就不失败了为止。

### 16.2 `property_support.py`

新建 `tests/property_support.py`：

```python
"""Turning a property failure into something a person can read.

Chapter 6 wrote the generator and said what it was giving up: "no shrinking, so
a failure prints a seed rather than a minimal example, and reproducing it means
running `MINICODEX_PROPERTY_SEED=<n>`.  Chapter 14 revisits this."

The cost of that trade was paid once and is worth quoting.  The bug that
justified the whole property suite -- compaction making a history *bigger* --
was found at seed 235, whose history has fourteen items.  Thirteen of them are
scenery.  What the report said was the number 235, and what the author then did
was print the history and read it.

`minimise` does the reading.  It is delta debugging in its simplest useful
form: keep removing items while the failure survives, halving-first so a long
run of irrelevant turns disappears in one step rather than fourteen.

It is deliberately not a general shrinker.  It shrinks *this* project's one
generated type, using `History`'s own API to rebuild -- so a candidate is never
an illegal history, and a failure it reports is never an artefact of the
shrinker having built something the generator could not have produced.
"""

from __future__ import annotations

from collections.abc import Callable

from minicodex.history import (
    AssistantMessage,
    DeveloperNote,
    History,
    SystemNote,
    ToolResult,
    UserMessage,
)


def rebuild(items: list) -> History | None:
    """A history containing exactly these items, or `None` if that is illegal.

    Chapter 1's invariant lives in `History`, so dropping a tool call and
    keeping its result is refused here rather than producing a candidate that
    fails for a reason the property test was not asking about.
    """
    history = History()
    try:
        for item in items:
            if isinstance(item, UserMessage):
                history.add_user(item.text)
            elif isinstance(item, AssistantMessage):
                history.add_assistant(item.text, item.tool_calls)
            elif isinstance(item, ToolResult):
                history.add_tool_result(item.call_id, item.content)
            elif isinstance(item, SystemNote):
                history.add_system_note(item.text)
            elif isinstance(item, DeveloperNote):
                history.add_developer_note(item.text)
        # The other half of the invariant, and the half the first version
        # missed.  Adding a result with no call raises at once; adding a call
        # and never its result raises nothing until somebody tries to send the
        # history.  A candidate that had lost a `ToolResult` came back from
        # here as "legal", and the property being minimised then crashed on it
        # with `HistoryError` -- a failure the shrinker had manufactured,
        # which is the one thing this function exists to prevent.
        history.to_wire()
    except Exception:
        return None
    return history


def minimise(history: History, fails: Callable[[History], bool], *, rounds: int = 40) -> History:
    """The smallest history this can reach that still fails.

    `fails` is the property, inverted: it returns True when the candidate
    reproduces the problem.  Chunk sizes halve from "everything but one half"
    down to single items, which is the standard ddmin schedule and is what
    stops a fourteen-item history needing fourteen passes.
    """
    items = list(history.items)
    size = max(len(items) // 2, 1)
    for _ in range(rounds):
        shrunk = False
        index = 0
        while index < len(items):
            candidate_items = items[:index] + items[index + size :]
            candidate = rebuild(candidate_items) if candidate_items else History()
            if candidate is not None and fails(candidate):
                items = candidate_items
                shrunk = True
            else:
                index += size
        if not shrunk:
            if size == 1:
                break
            size = max(size // 2, 1)
    rebuilt = rebuild(items)
    return rebuilt if rebuilt is not None else history


def describe(history: History) -> str:
    """One line per item: what kind it is and how big, not what it says.

    The generator fills its messages with `"o" * 2931`, so printing the text
    would print the scenery.  What decides whether compaction misbehaves is the
    *shape* -- which kinds of item, in which order, how long -- and that fits
    on a line each.
    """
    lines = []
    for index, item in enumerate(history.items):
        kind = type(item).__name__
        if isinstance(item, AssistantMessage) and item.tool_calls:
            ids = ", ".join(call.call_id for call in item.tool_calls)
            lines.append(f"  {index}: {kind} calling [{ids}]")
        elif isinstance(item, ToolResult):
            lines.append(f"  {index}: {kind} for {item.call_id}, {len(item.content)} chars")
        else:
            lines.append(f"  {index}: {kind}, {len(item.text)} chars")
    return "\n".join(lines) or "  (an empty history)"
```

**`rebuild`：按一串条目重新造一段对话，造不出合法的就返回 `None`。**

> - 为什么不直接把条目塞进一个列表？因为第 1 章的规矩住在 `History` 里：**有工具调用就必须有对应的结果，反之亦然**。用 `History` 自己的 `add_...` 方法一条条加，不合法的组合当场就会被拒绝。
> - 这保证缩小的过程**造不出非法的对话**。否则会出现最讨厌的一种结局：缩到最后得到一个"最小的失败例子"，而它之所以失败，是因为缩小器造了一个生成器根本造不出来的东西。
> - 末尾那个 `history.to_wire()` 和它上面的长注释：§16.4。

**`minimise`：删，直到不能再删。**

> - `fails` 是调用的人给的一个函数：给它一段对话，它回答"这段还会让那个性质失败吗"。
> - `size` 是每次删掉多少条，从"一半"开始。`items[:index] + items[index + size :]` 是"去掉从 `index` 开始的 `size` 条之后剩下的"。
> - 删掉这一段之后**还失败**，就真的删掉（`items = candidate_items`），并且 `index` 不动——同一个位置上现在是新的一段，接着试。不失败了，说明这一段里有要紧的东西，`index += size` 跳过它。
> - 一整圈下来一条都没删掉：把 `size` 减半再来；`size` 已经是 1 还删不动，就到头了。
> - 先大块后小块的好处：一长串无关的条目一步就没了，不用一条一条试。这个办法有个名字，叫 **delta debugging**（ddmin）。

**`describe`：把剩下的那一点印成人看得懂的样子。**

> 生成器往消息里填的是 `"o" * 2931` 这种东西，印原文等于印一屏幕的 o。决定压缩对不对的是**形状**——什么种类的条目、什么顺序、多长——所以每条只印一行：种类和长度。

### 16.3 改写时发现：它没有被接到任何地方

这一节在第一版里到此就结束了，还附了一个测试，证明 `minimise` 能把一段十一条的对话缩到一条。

改写时我去 `tests/test_properties.py` 里找它是在哪被调用的。**没有。** 那个文件开头的说明还写着：

```
... there is no shrinking, so a failure prints a seed rather than a minimal
example ... Chapter 14 revisits this.
```

`minimise` 写好了、测过了、在正文里讲了——**除了它自己的测试，没有任何东西调用它**。属性测试失败时打印的还是一个种子。F14-04 在清单上是"已解决"，在程序里一个字都没变。

（这和 §3 开头讲的是同一件事：**测一个零件，不等于测它接没接上**。）

接上。先把 `tests/test_properties.py` 开头那段说明改成实话：

```python
No `hypothesis`: a seeded generator is enough here and costs no dependency.
The trade was real -- for eight chapters a failure printed a seed and nothing
else, and reproducing it meant running `MINICODEX_PROPERTY_SEED=<n>` and
reading a fourteen-item history by eye.  Chapter 14 wrote the shrinker
(`property_support.minimise`), and then shipped it connected to nothing: this
file went on printing seeds.  `shrunk()` below is the connection.
```

导入加上：

```python
import asyncio
from collections.abc import Awaitable, Callable

from property_support import describe, minimise
```

加一个辅助函数：

```python
async def shrunk(history: History, still_fails: Callable[[History], Awaitable[bool]]) -> str:
    """The smallest history that still fails, ready to go in a failure message.

    Only ever reached once a property has already failed, so its cost is paid
    by the run that needs it.  `minimise` is synchronous and the properties
    are not, and a test is already inside a running event loop -- hence the
    worker thread, which gets a loop of its own for each candidate.
    """

    def fails(candidate: History) -> bool:
        return asyncio.run(still_fails(candidate))

    smallest = await asyncio.to_thread(minimise, history, fails)
    return f"smallest history that still fails ({len(smallest.items)} item(s)):\n" + describe(
        smallest
    )
```

> - 麻烦在于：`minimise` 是普通函数，而"这段对话还会不会失败"要调用压缩，压缩是 `async` 的。测试自己已经在事件循环里了，循环里不能再 `asyncio.run`（§8.3 遇到过）。
> - 办法：`asyncio.to_thread(minimise, history, fails)` 把整个缩小过程放到**另一个线程**里；那个线程里没有事件循环，所以 `fails` 可以放心地用 `asyncio.run(...)` 给每个候选现开一个。
> - `Callable[[History], Awaitable[bool]]`："一个接收对话、返回'等一等才知道真假'的函数"，也就是一个 `async` 函数。
> - 这个函数**只在某个性质已经失败之后**才会被调用，所以它慢一点没关系：平时一次都不跑。

然后把"压缩不会让对话变大"这个性质改成用它：

```python
@pytest.mark.parametrize("seed", seeds())
async def test_property_compaction_never_grows_the_history(seed):
    """The summary note is one item; it must not cost more than it saved.

    Not trivially true -- a summary is inserted, so a history where nothing
    could be dropped would grow by one item and by the summary's tokens.  The
    empty-`dropped` branch exists for exactly that case, and this is what pins
    it.
    """
    rng = random.Random(seed)
    history = random_history(rng)
    sizer = Sizer()
    budget = rng.choice([1, 300, 3000])

    async def grows(candidate: History) -> bool:
        before = sizer.messages(candidate.to_wire())
        result = await compact(candidate, summarise=stub_summary, budget=budget)
        return sizer.messages(result.history.to_wire()) > before

    if await grows(history):
        # The seed is still printed -- it is how to get this exact run back --
        # but it is no longer the whole report.
        pytest.fail(
            f"seed={seed} budget={budget}: compaction made this history bigger\n"
            + await shrunk(history, grows)
        )
```

> - 以前是算出前后两个大小，然后 `assert after <= before, f"seed={seed} ..."`。现在把"变大了吗"包成一个可以对**任意**对话提问的函数 `grows`，因为缩小器要拿它去问一个又一个候选。
> - **`pytest.fail(消息)`**：直接让测试失败，并显示这段消息。种子还是照样印——它是"把这一次原样跑回来"的办法——但不再是报告的全部。
> - 注意随机数的使用顺序没有变（先生成对话，再 `rng.choice` 选预算）。顺序一变，同一个种子生成的东西就不一样了，以前记下的种子（比如 235）就全作废。

### 16.4 接上之后第一次用，它崩在自己身上

要看缩小后的报告长什么样，得让这个性质真的失败一次。最省事的办法：把测试文件里那个假的摘要函数改成返回一段**很长**的摘要——摘要比被它替换掉的内容还长，对话自然就变大了：

```python
async def stub_summary(request: SummaryRequest) -> str:
-    return "## Goal\ng\n## Done\nd\n## Open\no\n"
+    return "## Goal\ng\n## Done\n" + "d" * 4000
```

跑：

```
E   minicodex.history.HistoryError: refusing to send: tool calls with no result: c460881698 (run_shell)

FAILED tests/test_properties.py::test_property_compaction_never_grows_the_history[165]
```

`HistoryError`——"有工具调用没有结果"。这不是"对话变大了"，这是另一种失败。而且生成器明明从不产出这种对话（它每造一个调用都配一个结果）。

是缩小器干的。它删掉了一条**工具结果**，留下了对应的**调用**，然后把这段残缺的对话交给 `grows` 去问，`grows` 一渲染就炸了。

可 `rebuild` 不是专门防这个的吗？回去读它的第一版说明："去掉一个调用而留下它的结果，在这里会被拒绝"。它说的是**这个**方向，而且确实如此：`add_tool_result` 遇到一个没人要过的结果，当场抛异常。**反过来**——加了调用、没加结果——`History` 当场不说话，要等到有人真的要把它发出去（`to_wire()`）时才报错。第 1 章就是这么设计的。

所以 `rebuild` 把"丢了结果的调用"当成合法的对话放了出去。**缩小器存在的唯一理由是不制造假的失败，而它恰恰制造了一个。** 这个 bug 从这个文件写下来那天就在，没有人发现，原因是 §16.3：它从来没有被用过。

修法是 `rebuild` 末尾那一行：全部加完之后自己调一次 `history.to_wire()`。渲染不出来的，就不是候选。

再跑：

```
E   Failed: seed=165 budget=300: compaction made this history bigger
E   smallest history that still fails (2 item(s)):
E     0: AssistantMessage calling [c460881698]
E     1: ToolResult for c460881698, 2756 chars
```

**两条。** 一次工具调用和它的 2756 个字符的结果：这么一段对话，配上一个 4000 字的摘要，压缩完当然更大。这就是那个失败的全部原因，印在屏幕上了。（把 `stub_summary` 改回去。）

```python
def test_F14_04_a_failing_history_is_reported_at_its_smallest() -> None:
    """Chapter 6's property runner prints a seed.

    A seed is reproducible and unreadable: the history that found seed 235 has
    fourteen items and one of them matters.  `minimise` removes turns while the
    failure survives, so the report is a three-item history instead of an
    instruction to go and run the generator again.
    """
    from property_support import minimise

    history = History()
    history.add_user("start")
    for index in range(8):
        history.add_assistant(f"step {index}")
    history.add_user("POISON")
    history.add_assistant("after")

    def fails(candidate: History) -> bool:
        return any(getattr(item, "text", "") == "POISON" for item in candidate.items)

    smaller = minimise(history, fails)
    assert fails(smaller)
    assert len(smaller.items) < len(history.items)
    assert len(smaller.items) == 1


def test_F14_04_a_candidate_that_lost_a_tool_result_is_not_a_candidate() -> None:
    """`rebuild` refused a result without its call and let a call without its
    result through, because `History` only objects to the second kind when it
    is asked to render.  The first property that was ever actually minimised
    crashed inside the shrinker with `HistoryError` -- a failure the shrinker
    built, reported as though the code under test had produced it."""
    from property_support import rebuild

    history = History()
    history.add_user("go")
    history.add_assistant("", [ToolCall("c1", "run_shell", {}, "{}")])
    history.add_tool_result("c1", "ok")
    user, assistant, result = history.items

    assert rebuild([user, assistant, result]) is not None
    assert rebuild([user, result]) is None  # a result nobody asked for
    assert rebuild([user, assistant]) is None  # a call nobody answered


def test_F14_04_the_report_describes_shapes_not_scenery() -> None:
    from property_support import describe

    history = History()
    history.add_user("u" * 300)
    history.add_assistant("", [ToolCall("c1", "run_shell", {}, "{}")])
    history.add_tool_result("c1", "o" * 2931)

    assert describe(history).splitlines() == [
        "  0: UserMessage, 300 chars",
        "  1: AssistantMessage calling [c1]",
        "  2: ToolResult for c1, 2931 chars",
    ]
    assert "ooo" not in describe(history)


def test_F14_04_the_property_file_actually_calls_the_shrinker(repo_root: Path) -> None:
    """`minimise` was written, tested, described in the chapter -- and imported
    by nothing except its own test.  The property suite went on printing a
    seed.  A tool nobody calls is the cheapest kind of fix to believe in."""
    text = (repo_root / "tests" / "test_properties.py").read_text(encoding="utf-8")
    assert "from property_support import describe, minimise" in text
    assert "await shrunk(history" in text
```

> - 第一个是第一版就有的：十一条对话里只有一条 `"POISON"` 是"病因"，缩完必须只剩它。
> - 第二个是 §16.4：三条（用户、调用、结果）合法；去掉调用留下结果，不合法；**去掉结果留下调用，也不合法**——最后这一行在修之前是通不过的。
> - 第四个守的是 §16.3："属性测试的文件里确实导入并且用了它"。检查文件里有没有某一行字，是很笨的测试；但它防的那种事——零件做好了没人接——恰好没有更聪明的办法防。

**为什么最后也没有换成 `hypothesis`。** 换的理由本来是"它有现成的缩小"；现在缩小有了，这条理由就没了。而第 6 章那个自己写的生成器有一个性质是换不来的：它只通过 `History.add_...` 造东西，所以**天生只产出合法的对话**。

提交：

```
test(property): minimise a failing history, and actually call it

Chapter 6 said "chapter 14 revisits this". The first revisit wrote
minimise(), tested it, and connected it to nothing: test_properties.py
went on printing a seed. Wired in through shrunk(); the first real use
crashed inside the shrinker, because rebuild() refused a result with no
call and let a call with no result through.
```

---

## §17 F14-05：真模型放在 CI 的哪一层

清单：所有测试都用真模型跑，CI 又慢又贵又不稳。药方是"离线为主，真模型为辅"。"为辅"具体放哪，得有个说法。

这一章量出来的三个数放在一起，说法就有了：

| | 多久 | 需要什么 | 结果稳不稳 |
|---|---|---|---|
| 全套离线测试 | 约 80 秒，1635 个 | 什么都不要 | 每次一样 |
| 不稳定检查（同一套跑 5 遍） | 约 7 分钟 | 什么都不要 | 每次一样 |
| 评估集一遍（6 个任务各 1 次） | 45 秒，19 次模型调用（实测于 2026-08） | key、网络、额度 | 不保证 |

于是三层。

**第一层 `ci.yml`（挡合并）——步骤一行没改。** 这一章的 35 个新测试都在原来那一步 `pytest` 里跑。第 -1 章给这个文件定过一条护栏（最多六步，"挡合并的这一层必须快"），它第三次起作用：逼人回答"这一步凭什么挡住别人合并"，而不是顺手加一步。

**第二层 `postmerge.yml`（合并之后跑，不挡合并）——加两步**，接在第 13 章那一步后面：

```yaml
# Chapter 14's twenty-three. The first two are the reason the chapter was
# first written: `instructions=None` in `__main__` and `COMPACT_AT = 0.95`
# both survived the whole suite as it then stood (1525 tests; chapters 11
# and 13 have since grown CLI-level tests that catch them too, re-measured
# when this chapter was rewritten). The first switches off
# chapter 5's permission block, chapter 11's plan paragraph and chapter
# 13's system prompt in one line; the second moves the point at which a
# session starts paying for compaction. Every test of `_instructions`
# calls `_instructions`; none of them checked that anything else did.
#
# Two more survived the first run of this script, both in tests written
# for the mutations they missed: one computed its thresholds from the
# constant it was checking, and one built results whose checks all
# passed or all failed, so "nothing failed" and "something passed" were
# indistinguishable.
- name: Mutation check (chapter 14)
  run: uv run python probe_mutations_ch14.py

# Chapter 14 again, and a different kind of check: run the suite five
# times on the *same* input -- property seed included -- and fail if any
# test does not decide the same way every time, or is not there every
# time. (This comment said "a different seed each time" long after the
# script stopped doing that; the seed changes the test ids, so a varied
# seed compares nothing with nothing.) F14-02 -- flakiness is a
# defect, and the way it stops being one is that something reports it
# with a name instead of somebody re-running the job.
- name: Flake check
  run: uv run python probe_flaky.py --runs 5
```

> 判据和第 9 章把变异检查放进这一层时一样：它们量的是**测试靠不靠得住**，不是**这次改动对不对**。慢一点没关系，但不该让每个提交的人等。

**第三层 `nightly.yml`（新，定时跑）——评估集，对着真模型，没有及格线。**

```yaml
# The third tier, added in chapter 14.
#
# Everything in `ci.yml` and `postmerge.yml` is offline: recorded bytes, a stub
# server, a scripted model. That is deliberate (F14-05) -- a suite that needs a
# provider is slow, costs money and fails for reasons that have nothing to do
# with the change, and the end state of all three is a suite people switch off.
#
# But a program whose whole job is talking to a model cannot be tested entirely
# without one. The eval task set is the part that must meet the real thing, so
# it lives here: on a schedule, where nobody is waiting for it, with a total
# that is *reported* rather than enforced.
#
# There is no pass/fail threshold on the score, on purpose. A threshold on a
# non-deterministic measurement is a coin that lands red often enough to be
# ignored, and F14-02 is the entry about what an ignored red does to every
# other red. What this job produces is a number and a per-task table; a human
# compares it with last week's.
#
# Honest note about this repository: `OPENAI_API_KEY` is not set here, so this
# workflow has never run to completion in CI. It has run from a laptop, which
# is where every number in chapter 14 comes from. A workflow that has never
# run is a draft, and calling it one costs nothing -- the guard below was
# wrong for exactly as long as nothing ran it.
name: nightly

on:
  schedule:
    - cron: "0 3 * * *"
  workflow_dispatch: {}

jobs:
  evals:
    runs-on: ubuntu-latest
    timeout-minutes: 30
    env:
      OPENAI_API_KEY: ${{ secrets.OPENAI_API_KEY }}
    steps:
      # Without a key there is nothing to measure, and a red X on a scheduled
      # job every morning trains everybody to stop reading the mail.
      #
      # This guard used to be a job-level `if: github.repository_owner != ''`
      # under the same comment -- a condition that is true in every repository
      # there is, so it guarded nothing and the job would have gone red on its
      # first scheduled run. A secret cannot be read in a job-level `if`; it
      # can be read in a step, so the check is a step and the rest depend on it.
      - name: Is there a key?
        id: key
        run: |
          if [ -z "$OPENAI_API_KEY" ]; then
            echo "No OPENAI_API_KEY secret here; nothing to measure." >> "$GITHUB_STEP_SUMMARY"
            echo "present=false" >> "$GITHUB_OUTPUT"
          else
            echo "present=true" >> "$GITHUB_OUTPUT"
          fi

      - uses: actions/checkout@v4
        if: steps.key.outputs.present == 'true'
      - uses: astral-sh/setup-uv@v5
        if: steps.key.outputs.present == 'true'
        with:
          enable-cache: true
      - run: uv sync --frozen --all-extras
        if: steps.key.outputs.present == 'true'

      # One sample per task is enough to notice that something has broken
      # outright; it is not enough to see a one-in-five effect, which is what
      # `probe_eval.py focus` is for and why that one is run by hand.
      - name: Eval task set (openai)
        if: steps.key.outputs.present == 'true'
        run: uv run python probe_eval.py cost --provider openai
```

> - `on: schedule: - cron: "0 3 * * *"`：每天 3 点（UTC）跑。五个字段依次是分、时、日、月、星期，`*` 是"每一个"。`workflow_dispatch` 让人可以在网页上手动点一下触发。
> - **没有及格线（比如"低于 16/18 就算失败"），是故意的。** 模型的表现本来就有波动；给一个会波动的数定一条线，等于放一枚隔三差五落在红色一面的硬币。红得多了就没人看，而 F14-02 讲的正是"一个被无视的红，会连累所有的红"。这一层产出的是一张表，由人拿去和上周的比。
> - `${{ secrets.OPENAI_API_KEY }}`：GitHub 仓库设置里存的密钥。它不会出现在日志里。

**文件开头那段实话，以及它没说到的一件事。** 注释里写着：这个仓库没有配 key，所以这个定时任务**从来没有在 CI 里真的跑完过**；这一章的数字都是在一台笔记本上量的。"没跑过的流程是草稿"，照实写不花钱。

改写时我读了那份草稿。第一版防"没有 key"用的是这一行：

```yaml
    # Without a key there is nothing to measure, and a red X on a scheduled
    # job every morning trains everybody to stop reading the mail.
    if: ${{ github.repository_owner != '' }}
```

注释说的是"没有 key 就别跑"。条件写的是"仓库的所有者不是空的"——**每一个仓库的所有者都不是空的**。这个条件永远为真，什么都没防住。假如哪天真的有人启用了定时任务又没配 key，它会每天早上红一次，正是注释说要避免的那件事。

它错了这么久没人知道，原因就是注释里自己写的那句：这个文件没跑过。

现在的写法：密钥在"整个任务的 `if`"里读不到，但在**步骤**里读得到。所以第一步专门检查，把结果写成一个输出（`present=true/false`）；后面每一步都带上 `if: steps.key.outputs.present == 'true'`。

```python
def test_F14_05_the_nightly_tier_cannot_block_a_merge_and_sets_no_threshold(
    repo_root: Path,
) -> None:
    """A third workflow, for the same reason chapter 9 added a second one.

    The blocking suite stays offline (six steps, chapter -1's cap). The eval
    set needs a provider, so it runs on a schedule -- and it reports a number
    rather than enforcing one, because a threshold on a non-deterministic
    measurement is a red that gets ignored, and F14-02 is the entry about what
    an ignored red does to every other red.
    """
    import yaml

    text = (repo_root / ".github/workflows/nightly.yml").read_text(encoding="utf-8")
    workflow = yaml.safe_load(text)
    triggers = workflow.get("on", workflow.get(True))
    assert "pull_request" not in triggers
    assert "push" not in triggers
    assert "schedule" in triggers
    # No `--fail-under`, no `exit 1` on a score: the job prints a table.
    assert "fail-under" not in text
    # ...and it does nothing at all where there is no key.  The first guard was
    # `if: github.repository_owner != ''`, which is never false.
    (job,) = workflow["jobs"].values()
    assert "if" not in job
    assert job["steps"][0]["id"] == "key"
    assert all("steps.key.outputs.present" in step.get("if", "") for step in job["steps"][1:])
```

> - `yaml.safe_load(text)` 把 YAML 读成 Python 的字典和列表。`workflow.get("on", workflow.get(True))`：YAML 有个怪脾气，会把没加引号的 `on` 当成"真"，所以两种键都试一下。
> - 前半断言触发条件：不被 `pull_request`、`push` 触发（挡不了合并），有 `schedule`。
> - 后半是新加的：任务本身没有 `if`；第一步的 `id` 是 `key`；**其余每一步**都依赖它。`all(... for step in job["steps"][1:])`——从第二步开始的所有步骤都得满足。
> - 必须说清楚这个测试的上限：它检查的是这个文件**写成了什么样**，不是它**跑起来什么样**。后者只有 GitHub 真的跑一次才知道，而那件事到现在还没有发生过。

提交：

```
test(ci): a third tier for the one thing that needs a provider

ci.yml unchanged. postmerge.yml gets this chapter's mutations and a
flake check. nightly.yml runs the eval set against a real provider, on a
schedule, with no threshold on the score -- and, unlike its first
version, with a guard that can actually be false.
```

---

## §18 清单外的几件

### 18.1 为一条变异写的测试，抓不住那条变异

§3 提到过一条变异：把"用到窗口的 75% 就开始压缩"改成 95%。我专门为它写了一个测试：

```python
    assert await one_run(int(estimate / (COMPACT_AT + 0.1))) == 1
    assert await one_run(int(estimate / (COMPACT_AT - 0.1))) == 0
```

变异检查的结果：

```
    0 test(s) fail  <-  compaction starts at 95% of the window instead of 75%
```

两个窗口大小都是**从被测的那个常量算出来的**。常量一变，两个阈值跟着一起变，测试当然还是绿的。

> **一个去读"自己要检查的那个常量"的测试，检查不了那个常量。**

第 6 章记过同一族的一条（"把被测代码抄进测试文件的测试，量的是那份抄本"）。那次抄的是一个循环，这次抄的是一个数。修法是把 0.75 写死在测试里：

```python
async def test_F14_03_compaction_fires_at_the_documented_fraction() -> None:
    """`COMPACT_AT = 0.75` had nothing depending on it.

    Setting it to 0.95 -- a real behaviour change, since it decides when a
    session starts paying for a summary -- left all 1525 tests green when this
    chapter was first written (one chapter-13 test now happens to sit close
    enough to the threshold to notice; this is the one that says so).  Chapter
    6 has thirteen faults' worth of tests about *how* a history is cut and not
    one about *when* the cutting starts.

    Two runs on the same history, with the window chosen so that the estimate
    sits either side of the threshold.  The assertion is on both sides on
    purpose: `assert compactions == 1` alone is satisfied by a constant.

    The 0.75 below is a **literal**, and the first version of this test used
    `COMPACT_AT` instead -- which computed both windows from the very constant
    it was checking, so the mutation moved the thresholds and the test with
    them.  It survived, in the test written to catch it.  Chapter 6's rule
    ("a test that reimplements the code under test measures the copy") applies
    to reading a constant just as much as to copying a loop.
    """

    async def summarise(request: Any) -> str:
        return "## Done\nthings happened"

    async def one_run(window: int) -> int:
        history = History()
        history.add_user("start")
        for index in range(12):
            history.add_assistant(f"step {index} " + "x" * 400)
        model = _Answers(["finished"])
        agent = Wiring(context_window=window, summariser=summarise).agent(
            model, ToolSet(handlers={}, schemas=[]), resume_from=history
        )
        return len((await agent.run("carry on")).compactions)

    estimate = _estimate_of()
    assert COMPACT_AT == 0.75, "the two windows below are computed from this number"
    assert await one_run(int(estimate / 0.85)) == 1  # estimate is 85% of the window
    assert await one_run(int(estimate / 0.65)) == 0  # ...and 65% of this one


def _estimate_of() -> int:
    """The size of the history the test above builds, by the program's own ruler."""
    from minicodex.compaction import Sizer

    history = History()
    history.add_user("start")
    for index in range(12):
        history.add_assistant(f"step {index} " + "x" * 400)
    history.add_user("carry on")
    return Sizer().messages(history.to_wire())


class _Answers:
    """A model that says the next thing on its list and asks for no tools."""

    tools: ClassVar[list[dict[str, Any]]] = []

    def __init__(self, turns: list[str]) -> None:
        self.turns = turns
        self.sent: list[Any] = []

    async def stream(self, messages: Any) -> Any:
        from minicodex.model import Completed, TextDelta

        self.sent.append(list(messages))
        yield TextDelta(self.turns[min(len(self.sent) - 1, len(self.turns) - 1)])
        yield Completed("stop")
```

> - 思路：造一段固定的对话，用程序自己的尺子量出它有多大（`_estimate_of`）。然后选两个窗口：一个让这段对话占 85%（应该压缩一次），一个让它占 65%（不该压缩）。**两边都断言**：只断言"压缩了一次"的话，一个不管三七二十一永远压缩的程序也能通过。
> - `_Answers`：照着列表说话、从不要工具的假模型。`self.turns[min(len(self.sent) - 1, len(self.turns) - 1)]`——第几次被问就说第几句；问的次数超过列表长度，就一直说最后一句。

### 18.2 评估的工作区，掉进了格式检查的视野

`ci.yml` 里 Lint 那一步的第一条命令是 `ruff format --check .`。第一次在这一章的目录里跑它：

```
23 files would be reformatted, 175 files already formatted
```

23 个里有 **21 个是 `.probe/ch14/*/calc.py`**——评估任务的工作区，里面的 Python **是模型写的**。`.gitignore` 挡不住 ruff（它只在 git 仓库里才读 `.gitignore`，而这些 step 目录各自不是一个仓库）。

`pyproject.toml` 里加一行，`.gitignore` 里也加上：

```toml
# Chapter 14's eval workspaces. An A/B run leaves 36 directories of Python
# **written by the model**, inside the tree, and `ruff format --check .` walks
# them: 21 of the 23 files it wanted to reformat were the agent's homework.
# `.gitignore` does not help -- ruff only honours it inside a git repository,
# and a step directory here is not one. F14-08's shape at a smaller scale: an
# eval workspace that sits inside the system under test ends up inside the
# system under test's tooling.
extend-exclude = [".probe"]
```

```
# Chapter 14's eval workspaces. `probe_eval.py` builds one directory per task
# per sample and an agent then writes files into it -- 36 of them for one A/B
# run. Interlude A's lesson for the third time, and this one is the largest:
# the directories are created *by the model*, so the list of files nobody can
# predict is not a metaphor here.
.probe/
```

§12 是 Agent 看见了仓库；这次是**仓库的工具看见了 Agent 的作业本**。同一条边界，两个方向都得挡。

### 18.3 一个测试从第 12 章起，每跑一次就往仓库里写一份真录制

跑完变异检查顺手 `ls -a`，多了一个 `.minicodex/`。追到第 12 章的一个测试：它给了 `--session-dir tmp_path`，把会话文件挪到了临时目录，**但没有任何东西挪走录制器**——录制器默认写到"当前目录下的 `.minicodex/recordings/`"。

于是这个测试从写下来那天起，每跑一次就往项目里落一份真的录制。`.gitignore` 挡住了它进版本库，所以没人发现。修法一行，加在那个测试里：

```python
    monkeypatch.chdir(tmp_path)
```

（测试的参数表里相应加上 `monkeypatch: Any`。）

值得记的是**它是怎么被看见的**：不是哪个测试红了，是这一章反复在 `ls` 目录。"跑一条从没跑过的命令，然后看一眼输出"，在这一章贡献的发现比任何一种测试都多。

### 18.4 前两章交付的代码，格式检查曾经是红的

第一次写这一章时，顺手在前两章的目录里也跑了 `ruff format --check .`：第 11 章干净；**第 12、13 章各有 4 个文件不合格式**。也就是说那两章当时交付的代码，挡合并的 CI 第一条命令就过不去——只是这个仓库从来没有真的在 GitHub 上跑过那份 CI。

`ruff check`（查问题）过，`ruff format --check`（查格式）不过。**两条命令，一个程序，写那两章时只跑了其中一条。**

那两章后来改写时已经修掉。这次重新量：

```
$ cd steps/step11_plan          && uv run ruff format --check .
69 files already formatted
$ cd steps/step12_retry         && uv run ruff format --check .
73 files already formatted
$ cd steps/step13_system_prompt && uv run ruff format --check .
77 files already formatted
```

留下这一节，是因为它留下了一条规矩：每章收工前，这两条命令都跑（§19.3）。

---
## §19 逐条验证

### 19.1 测试文件的开头

前面各节的测试都放在 `tests/test_faults_ch14.py` 里。文件的开头——说明、导入和两个造录制的辅助函数——是这样的（`_trajectory`、`_result`、`_rate_limited`、`_flaky` 几个辅助函数在用到它们的那一节已经贴过）：

```python
"""Chapter 14 -- evaluation and regression testing.

Every test here is offline.  That is not a convenience: F14-05 is the entry
that says a suite which needs a provider is a suite people switch off, and a
chapter about testing that shipped tests needing an API key would be arguing
against itself.  The real-model measurements live in `probe_eval.py` and run
nightly, where nobody is waiting for them.
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Any, ClassVar

import pytest

from minicodex import stub
from minicodex.agent import COMPACT_AT, Wiring
from minicodex.agent_types import ToolCall, ToolSet
from minicodex.approval import AllowAll, Session
from minicodex.evals import (
    TASKS,
    Arm,
    Result,
    Task,
    Trajectory,
    answer_has,
    by_name,
    calls,
    declared_files,
    file_has,
    regressions,
    run_task,
)
from minicodex.history import History
from minicodex.recorder import Recorder
from minicodex.replay import (
    RecordedModel,
    ReplayDrift,
    ReplayError,
    call_record,
    divergence,
    failure_record,
    load,
    recorded_tools,
)
from minicodex.retry import ModelFailed, RetryPolicy
from minicodex.tools import tool_context

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _event(kind: str, payload: dict[str, Any], seq: int = 0) -> str:
    return json.dumps({"seq": seq, "ts": 0.0, "kind": kind, "payload": payload})


def _recording(path: Path, *events: str) -> Path:
    path.write_text("\n".join(events) + "\n", encoding="utf-8")
    return path
```

> - 文件说明的第一句就是 F14-05 的结论：**这里的每一个测试都不联网。** 一个讲测试的章节要是交付了一堆需要 key 的测试，就是在反驳自己。
> - `_event` 生成一行事件（`json.dumps` 把字典变成一行 JSON）；`_recording` 把几行事件写进一个文件、返回路径。`*events` 表示"后面跟任意多个参数，收成一个元组"。

```
$ uv run pytest tests/test_faults_ch14.py
...................................                                      [100%]
35 passed in 1.46s
```

### 19.2 这些测试自己靠得住吗

第 -1 章的规矩：**把每一处修复改回去，看有没有测试变红。** 第 9 章起这件事由一个脚本来做。这一章的是 `probe_mutations_ch14.py`：

```python
"""Do chapter 14's tests fail when chapter 14's code is wrong?

Same script as chapters 9 through 13, and this time it is also the chapter's
own subject.  The first two entries are the mutations that survived the whole
suite when this chapter was first written, which is how it found out what it
had to write.  (Chapters 11 and 13 have since grown CLI-level tests that catch
both on their own; they stay here because this chapter's tests have to catch
them too.)  The last nine were added when the chapter was rewritten, one per
defect found in the replay, the shrinker and the flake checker themselves.

    uv run python probe_mutations_ch14.py
"""

from __future__ import annotations

import atexit
import re
import signal
import subprocess
import sys
from pathlib import Path

MUTATIONS = [
    # ---- the two that survived everything, before chapter 14 ----------------
    (
        "__main__.py",
        "the CLI sends no system message at all",
        "        instructions=_instructions(session, tools),",
        "        instructions=None,",
    ),
    (
        "agent.py",
        "compaction starts at 95% of the window instead of 75%",
        "COMPACT_AT = 0.75",
        "COMPACT_AT = 0.95",
    ),
    # ---- the recording has to carry enough to be re-run ---------------------
    (
        "replay.py",
        "a tool call is recorded without its arguments, as before this chapter",
        '    return {"id": call.call_id, "name": call.name, "arguments": call.raw_arguments}',
        '    return {"id": call.call_id, "name": call.name}',
    ),
    (
        "replay.py",
        "the parsed arguments are recorded instead of the bytes the model sent",
        '    return {"id": call.call_id, "name": call.name, "arguments": call.raw_arguments}',
        '    return {"id": call.call_id, "name": call.name, "arguments": str(call.arguments)}',
    ),
    (
        "replay.py",
        "a failure is recorded as prose only, as before this chapter",
        '        return {"status": exc.status, "url": exc.url, '
        '"headers": exc.headers, "body": exc.body}',
        '        return {"status": 0}',
    ),
    (
        "replay.py",
        "a recording missing its arguments is filled in with an empty object",
        '        if "arguments" not in call:',
        "        if False:",
    ),
    (
        "replay.py",
        "the request is replayed without being compared with the recorded one",
        "        if self.strict:",
        "        if False:",
    ),
    (
        "replay.py",
        "the drift report shows the first 90 characters instead of the difference",
        "                    was, now = _window(b.get(key), a.get(key))",
        "                    was, now = _short(b.get(key)), _short(a.get(key))",
    ),
    (
        "replay.py",
        "a tool output is matched by name alone, ignoring the arguments",
        "            key = (name, json.dumps(arguments, sort_keys=True, ensure_ascii=False))",
        "            key = (name, json.dumps({}, sort_keys=True, ensure_ascii=False))",
    ),
    (
        "replay.py",
        "drift becomes an ordinary Exception again, and the loop swallows it",
        "class ReplayDrift(BaseException):",
        "class ReplayDrift(Exception):",
    ),
    # ---- the eval harness ---------------------------------------------------
    (
        "tools.py",
        "the shell starts in the process's directory rather than at the root",
        "        shell=shell or ShellSession(cwd=resolved),",
        "        shell=shell or ShellSession(),",
    ),
    (
        "evals.py",
        "a regression is reported only when the total gets worse",
        "        if now < was:",
        "        if False:",
    ),
    (
        "evals.py",
        "a task passes when any check passes rather than when none fails",
        "        return not self.failed and self.error is None",
        "        return bool(self.passed) and self.error is None",
    ),
    (
        "evals.py",
        "the workspace keeps whatever was already in the directory",
        "    workspace.mkdir(parents=True, exist_ok=True)\n    for name, content in files.items():",
        "    workspace.mkdir(parents=True, exist_ok=True)\n    for name, content in []:",
    ),
    # ---- found while rewriting the chapter: the replay saw less than it said -
    (
        "agent.py",
        "every agent signs its events with the same number",
        "        self.conversation = next(_CONVERSATIONS)",
        "        self.conversation = 0",
    ),
    (
        "replay.py",
        "a sub-agent's requests are loaded as though they were its parent's",
        '            if payload.get("conversation") != top:\n                elsewhere += 1',
        "            if False:\n                elsewhere += 1",
    ),
    (
        "replay.py",
        "a repeated (turn, attempt) silently replaces the earlier request",
        "            if key in requests:",
        "            if False:",
    ),
    (
        "agent.py",
        "a replay sleeps through every wait it was recorded with",
        "            if self.retry_policy.serve_waits:",
        "            if True:",
    ),
    (
        "agent.py",
        "a wait that was not served is not counted against the budget either",
        "                skipped += wait",
        "                pass",
    ),
    (
        "__main__.py",
        "a run that stops before the recording does is reported as ok",
        "    if model.unused:",
        "    if False:",
    ),
    (
        "__main__.py",
        "--loose still prints 'match' for requests it never compared",
        '    verdict = "match" if strict else "sent, not compared"',
        '    verdict = "match"',
    ),
    # ---- the two tools that check the tests ----------------------------------
    (
        "../../tests/property_support.py",
        "the shrinker offers a history with an unanswered call as a candidate",
        "        history.to_wire()\n    except Exception:",
        "        pass\n    except Exception:",
    ),
    (
        "../../probe_flaky.py",
        "a test that is missing from one run is counted as stable",
        "if count < len(runs))",
        "if False)",
    ),
]

SRC = Path("src/minicodex")
ORIGINALS = {name: (SRC / name).read_text(encoding="utf-8") for name, *_ in MUTATIONS}
SUITES = [
    "tests/test_faults_ch14.py",
    "tests/test_agent.py",
    "tests/test_compaction.py",
]


def restore() -> None:
    for name, text in ORIGINALS.items():
        path = SRC / name
        if path.read_text(encoding="utf-8") != text:
            path.write_text(text, encoding="utf-8")


atexit.register(restore)
signal.signal(signal.SIGINT, lambda *_: sys.exit(130))


def main() -> None:
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
                timeout=900,
                capture_output=True,
                text=True,
            )
        except subprocess.TimeoutExpired:
            print(f"  !! the suite did not finish: {label}")
            survivors.append(label)
            continue
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

> - `MUTATIONS` 里每一项是四样：改哪个文件、这处改坏用人话怎么说、原来的那段字、改成的那段字。
> - `ORIGINALS` 先把每个要动的文件原样读进内存；`restore()` 把它们写回去。**`atexit.register(restore)`**：不管脚本怎么结束（跑完、出错、被 Ctrl-C），退出前都执行一次——第 13 章记过一次"脚本被打断，留下一行改坏的源码"。
> - 每条变异：改 → 跑三个测试文件 → 数输出里 `FAILED` 开头的行 → 还原。一条都没红的就是"活下来的"，脚本最后以退出码 1 结束并点名。
> - 最后两项的文件名是 `../../tests/property_support.py` 和 `../../probe_flaky.py`：它们不在 `src/minicodex/` 里，从 `SRC` 往上跳两层再下来。**检查测试的工具，自己也得被检查。**

```
$ uv run python probe_mutations_ch14.py
23 mutations, tests/test_faults_ch14.py tests/test_agent.py tests/test_compaction.py

    3 test(s) fail  <-  the CLI sends no system message at all
    1 test(s) fail  <-  compaction starts at 95% of the window instead of 75%
    3 test(s) fail  <-  a tool call is recorded without its arguments, as before this chapter
    3 test(s) fail  <-  the parsed arguments are recorded instead of the bytes the model sent
    1 test(s) fail  <-  a failure is recorded as prose only, as before this chapter
    1 test(s) fail  <-  a recording missing its arguments is filled in with an empty object
    1 test(s) fail  <-  the request is replayed without being compared with the recorded one
    1 test(s) fail  <-  the drift report shows the first 90 characters instead of the difference
    3 test(s) fail  <-  a tool output is matched by name alone, ignoring the arguments
    2 test(s) fail  <-  drift becomes an ordinary Exception again, and the loop swallows it
    1 test(s) fail  <-  the shell starts in the process's directory rather than at the root
    1 test(s) fail  <-  a regression is reported only when the total gets worse
    1 test(s) fail  <-  a task passes when any check passes rather than when none fails
    1 test(s) fail  <-  the workspace keeps whatever was already in the directory
    1 test(s) fail  <-  every agent signs its events with the same number
    1 test(s) fail  <-  a sub-agent's requests are loaded as though they were its parent's
    1 test(s) fail  <-  a repeated (turn, attempt) silently replaces the earlier request
    1 test(s) fail  <-  a replay sleeps through every wait it was recorded with
    1 test(s) fail  <-  a wait that was not served is not counted against the budget either
    1 test(s) fail  <-  a run that stops before the recording does is reported as ok
    1 test(s) fail  <-  --loose still prints 'match' for requests it never compared
    1 test(s) fail  <-  the shrinker offers a history with an unanswered call as a candidate
    1 test(s) fail  <-  a test that is missing from one run is counted as stable

every mutation was caught.
```

（这次在 Linux 上实测。）

第一版只有前十四条，**头一回跑的时候有两条活着**：

- "压缩从 95% 开始"——§18.1，测试读了它要检查的常量。
- "有一条检查通过就算任务通过"：

  ```python
      @property
      def ok(self) -> bool:
  -       return not self.failed and self.error is None
  +       return bool(self.passed) and self.error is None
  ```

  两种写法在我当时所有的测试里**结论完全一样**——因为那些测试造的结果要么全过、要么全不过。差别只在混合的情况：一个四条检查的任务，文件改对了、顺手把测试文件删了，在错的那种写法下算**通过**。补的就是 §13.2 的第二个测试。

这两条合起来是一句话：**为一条变异写的测试，也要拿那条变异验一遍。**

### 19.3 全套

```
$ uv run pytest
1635 passed in 78.44s (0:01:18)

$ uv run ruff format --check . && uv run ruff check .
All checks passed!

$ uv run python scripts/check_layers.py
ok  no import cycles
ok  __init__ is a leaf
ok  forbidden edges
ok  one Agent construction site
ok  every module imports alone
```

`check_layers.py`（插曲 B）全绿，这一条值得说一句：`agent.py` 现在要 `import replay`——**循环依赖它自己的调试工具**，方向看着别扭。它不成环，是因为 `replay.py` 只导入 `agent_types` 和 `model`，从不导入 `agent`。

---

## §20 收工

### 20.1 这一章动了哪些文件

| 文件 | 变化 | 哪一节 |
|---|---|---|
| `src/minicodex/replay.py` | 新增 | §4.2、§5、§6、§7、§9.1 |
| `src/minicodex/evals.py` | 新增 | §10.2、§10.3、§11、§13.2 |
| `src/minicodex/agent.py` | 记录参数和失败证据；每个 Agent 一个编号；不真睡的等待 | §4.2、§9.3、§9.4 |
| `src/minicodex/retry.py` | `RetryPolicy.serve_waits` | §9.3 |
| `src/minicodex/__main__.py` | `config` 事件；`replay` 子命令 | §4.3、§8、§9.1、§9.2 |
| `src/minicodex/shell.py`、`tools.py` | shell 从根目录出发 | §12.2 |
| `tests/test_faults_ch14.py` | 新增，35 个测试 | 各节 |
| `tests/property_support.py` | 新增：`rebuild`、`minimise`、`describe` | §16.2、§16.4 |
| `tests/test_properties.py` | `shrunk()`；一个性质改用它 | §16.3 |
| `tests/test_faults_ch12.py` | 一行 `monkeypatch.chdir` | §18.3 |
| `probe_eval.py` | 新增，七段 | §5.2、§10.1、§12.3、§13.3、§14 |
| `probe_flaky.py` | 新增 | §15 |
| `probe_mutations_ch14.py` | 新增，23 条 | §19.2 |
| `.github/workflows/postmerge.yml` | 加两步 | §17 |
| `.github/workflows/nightly.yml` | 新增 | §17 |
| `pyproject.toml`、`.gitignore` | 排除 `.probe/` | §18.2 |
| `README.md` | 这一步的说明 | — |

`probe_eval.py` 四百多行，正文只贴了 §12.3 那一段。它和前几章的探针是同一种东西：每一段对应正文里的一次测量，做的事就是"造两组、各跑几次、印一张表"，用的全是这一章已经讲过的函数（`run_task`、`Arm`、`table`、`regressions`）。照着快照里的抄即可。

### 20.2 回头看：这一章的提交

```
test(property): minimise a failing history, and actually call it
test(ci): a third tier for the one thing that needs a provider
fix(replay): a replay now reports what it checked, and only that
feat(evals): A/B report with one row per task, and regressions()
fix(shell): the shell starts where the other tools are pointed
feat(evals): six tasks, trajectory checks, and run_task()
feat(cli): `minicodex replay`
feat(replay): load(), RecordedModel and recorded_tools()
feat(replay): record what a re-run needs, not only what a reader needs
```

（最新的在最上面。）

第五条是一个 `fix`，夹在两个 `feat(evals)` 中间。它修的是第 2 章的代码，却是在写评估集的半路上发现的——**按发现的顺序提交，不要为了好看把它挪到最前面**。以后有人用 `git blame` 查"shell 的起始目录为什么是这样"，会直接落到这条提交上，而它的说明里写着是怎么撞见的。

### 20.3 推送、PR

```bash
git push -u origin feat/eval
```

PR 描述：

```markdown
## What

- `minicodex replay <recording>` re-runs a real session against the current
  code, offline, and fails if the requests it now builds differ from the
  ones that were sent -- or if the run stops before the recording does.
- Six eval tasks with trajectory checks, an A/B runner, and a report with
  one row per task.
- Three tools that check the tests: a flake check, a shrinker for the
  property suite, a mutation script.
- A third CI tier for the part that needs a real provider.
- One product bug the eval harness found on its first run: `run_shell`
  started in the process's directory, not in the root it was given.

## Why

Thirteen chapters of measurements about how a model behaves, and nothing
that says whether the next change made any of them worse. And a recorder
that has promised a replay since chapter -1 and could not deliver one.

## Testing

35 new tests, all offline. 23 mutations, all caught; two survived the
first run, both in tests written for the mutations they missed.

Chapter 13's shipped sentence over the whole task set: 15/18 -> 18/18 on
openai, 15/18 -> 15/18 on ollama, no task made worse. Five of six tasks
are saturated in both arms, so this set can detect a break and cannot
detect an improvement.

## Notes for the reviewer

- `agent.py` imports `replay.py`. Defended in a comment there.
- `ReplayDrift` is a `BaseException`, on purpose.
- `nightly.yml` has never run in CI (no key in this repository).
- The shell is started in the workspace but not confined to it; that is
  chapter 20.
```

### 20.4 自己审一遍

> **1.** `recorded_tools()` 用 `(工具名, 参数)` 当键，同一个键的结果按顺序取。如果一次运行里对同一个文件 `read_file` 两次、中间文件被改过，两次的结果不同——顺序对得上吗？

对得上，但只是因为顺序恰好一样：存的时候按出现的先后，取的时候也按先后。会错的情况是第 8 章的调度器把两次一模一样的调用排成了和当时不同的先后。这一条接受，记成已知边界（`ReplayedTools` 说明的最后一段）：真出问题时，症状是"某次调用拿到了下一次的结果"，而下一条请求马上就会和录制对不上——**不是不出声的**。

> **2.** `test_F14_03_a_recorded_session_replays_with_no_drift` 自己录、自己放。如果录和放**一起错了**，它还是绿的。

对，这是回放**结构上**的上限（§8.3 那张表的最后一行）。抓"两边一致地错"的是那些绝对的测试。两种都要有。

> **3.** `_CONVERSATIONS` 是一个全局计数器。全局可变状态，以前各章一直在避免。

是，而且考虑过另外两种：让调用的人传一个编号进来（那就又多一个"可能忘了传"的参数，插曲 B 整个就是在消灭这种参数）；让录制器来发号（要改第 -1 章的类，而"不记录"的那个空录制器也得跟着实现）。这个计数器的值不影响任何行为，只要求不重复；读的那头也只看"和第一条请求是不是同一个号"，不依赖具体是几。在这三条都成立的范围内，一个全局计数器是最小的那个选择。

> **4.** `serve_waits=False` 时跳过的等待被算进了"已经过了多久"。可是真实的那次运行里，除了等待还有**请求本身花的时间**，那部分回放时接近于零。预算的判断会不会因此和当天不一样？

会，而且这是真的差距：当天一次请求花了 30 秒才失败的话，回放里这 30 秒不存在。结果是回放可能比当天**多**重试一次，然后撞上"录制用完了"——是响的，不是悄悄地给出一个不同的结论。要彻底对上，得把每次尝试花了多久也录下来。记成一个已知的缺口，没有做。

> **5.** 第 13 章的 `AGENTS.md`、第 11 章的计划，录的时候有、放的时候没有，会被报成 drift。这算回放的 bug 吗？

算缺口，不算 bug：它报的是真话（请求确实不一样了），只是原因不在代码而在"回放时没把那两样东西带上"。要补，得把它们也写进 `config` 事件。章末练习 5。

### 20.5 合并

按第 0 章 §11 的做法合并，然后：

```bash
git switch main
git pull
```

---

## §21 codex 是怎么做的

### 14.1 测试目录的文件名就是故障清单

`codex-rs/core/tests/suite/` 下 **118 个文件**。挑一部分念出来：

```
abort_tasks.rs                    compact_resume_fork.rs        pending_input.rs
apply_patch_cli.rs                deprecation_notice.rs         prompt_cache_key.rs
approvals.rs                      exec_policy.rs                prompt_caching.rs
audio_truncation.rs               fork_thread.rs                quota_exceeded.rs
auto_review.rs                    mcp_auth_refresh.rs           request_permissions.rs
client_websockets.rs              mcp_refresh_cleanup.rs        resume_warning.rs
code_mode_elicitation.rs          mcp_startup_refresh_...rs     rollout_budget.rs
compact.rs                        model_switching.rs            safety_check_downgrade.rs
compact_remote.rs                 multi_agent_resume.rs         shell_snapshot.rs
compact_remote_parity.rs          network_approval.rs           stream_error_allows_next_turn.rs
                                                                stream_no_completed.rs
                                                                token_budget.rs
                                                                tool_parallelism.rs
                                                                truncation.rs
                                                                unified_exec_process_events.rs
                                                                unstable_features_warning.rs
                                                                windows_sandbox.rs
                                                                workspace_roots.rs
```

这本书前十三章撞到的东西，几乎每一条都能在上面找到对应的文件名：
`truncation`（第 2 章）、`approvals`（第 5 章）、`compact_resume_fork`（第 6、7 章）、
`tool_parallelism`（第 8 章）、`mcp_refresh_cleanup`（第 9 章）、
`quota_exceeded` 和 `stream_no_completed`（第 12 章）、`agents_md`（第 13 章）。

**没有一个文件叫 `test_utils.rs` 或者 `integration.rs`。**
每个名字都是一个具体的坏事。这就是 PLAN 里那句"测试文件名是最诚实的故障档案"
的实物——一个成熟项目的测试目录，读起来像一份事故清单，而不像一份功能清单。

### 14.2 请求体快照：真做的人，是这么处理"不稳定"的

`core/tests/suite/snapshots/` 下 38 个 `.snap`。其中一个长这样：

```
## Local Compaction Request
00:message/developer:<PERMISSIONS_INSTRUCTIONS>
01:message/user:<ENVIRONMENT_CONTEXT:cwd=<CWD>>
02:message/user:first manual turn
03:message/assistant:FIRST_REPLY
04:message/user:<SUMMARIZATION_PROMPT>

## Local Post-Compaction History Layout
00:message/user:first manual turn
01:message/user:<COMPACTION_SUMMARY>\nFIRST_MANUAL_SUMMARY
02:message/developer:<PERMISSIONS_INSTRUCTIONS>
03:message/user:<ENVIRONMENT_CONTEXT:cwd=<CWD>>
04:message/user:second manual turn
```

三件事值得学：

1. **快照的不是原文，是形状。** `<PERMISSIONS_INSTRUCTIONS>`、
   `<SUMMARIZATION_PROMPT>`、`<COMPACTION_SUMMARY>` 全是占位符。
   这正是 F14-01 那条"断言轨迹不是断言措辞"在请求体这一层的样子——
   压缩之后历史的**布局**是行为，那几千字提示词的**措辞**是另一件事，
   有自己的快照。
2. **另一类快照直接存 diff**，两条请求之间的差：
   ```
   --- Last Normal /responses Request
   +++ Remote /responses/compact Request
   -  "client_metadata": { ... "session_id": "<UUID>", ... }
   -  "include": ["reasoning.encrypted_content"]
   ```
   "这两次请求应该只差这些"比"这次请求应该长这样"更接近人要断言的东西。
3. **让快照稳定的那套工具，本身是 787 行**（`core/tests/common/
   context_snapshot.rs`），有四种渲染模式，**而且有自己的单元测试**
   （`fn redacted_text_mode_keeps_canonical_placeholders`）。
   UUID、时间戳、cwd、临时路径全部要归一化，否则一份真实请求的快照
   100% 是 flaky。

这一章的 `divergence()` 是这套东西的十分之一，而且没有做归一化——
因为这个程序的请求体里目前没有 UUID 和时间戳。
**哪一天有了，`_window()` 旁边就要长出 codex 那 787 行的开头几十行。**

### 14.3 CI 分层：他们的形状

`blocking-ci.yml` 是**唯一**能挡 merge 的入口，里面是七个可复用 workflow
的调用（bazel / blob-size-policy / cargo-deny / codespell / repo-checks /
rust-ci / sdk），最后一个 job 叫 `required`，它的注释值得抄：

```yaml
  required:
    name: CI required
    # Without `always()`, GitHub skips this job after a failed dependency and a
    # required check can appear successful instead of reporting the failure.
    if: ${{ always() }}
```

**一个失败的依赖会让必需检查看起来是成功的。** 这是 CI 自己的静默错误，
形状和这本书里 87% 的故障一模一样。

`postmerge-ci.yml` 跑重的（`rust-ci-full`、`v8-canary`）。
这一章的三层就是照这个形状做的，第三层（真模型）是这本书自己的需要——
codex 的测试全部对着录制好的响应跑，没有一层在 CI 里调真模型。

---

## §22 回头看：这一章撞到了什么

清单上的八条：

| 编号 | 结果 |
|---|---|
| F14-01 | ✅ 成立，而且更坏：检查措辞的断言在一个模型上 5/5 稳定（50 个字符一字不差），换一个模型才暴露；另外量到一次"没干活的运行通过了检查措辞的断言" |
| F14-02 | ✅ 机制补上了（`probe_flaky.py`）。这个工具自己出了四个 bug，四个都是"少看了" |
| F14-03 | ✅ 主线。第 -1 章的录制缺三样才能重跑（参数、失败的证据、配置）；补齐之后回放同时成了快照 |
| F14-04 | ✅ 缩小器写了——然后发现它没被接上；接上之后第一次用，发现它自己会造出不合法的对话 |
| F14-05 | ✅ 三层。挡合并的那层步骤没变；定时那层**至今没在 CI 里真的跑过**，照实写在文件里 |
| F14-06 | ✅ 成立：没有位置偏好；风格偏好一边倒（两个裁判方向相反）；**两个裁判都 10/10 选了那个自信的错答案** |
| F14-07 | ✅ 方法用上了，结论是"没有任务变差"，以及这句话的分辨率：六个任务五个饱和，31 次里那 1 次归不了因 |
| F14-08 | ✅ 当场发生，而且是产品的 bug：`run_shell` 十二章以来一直从"启动程序的目录"出发。现在从工作区出发，**但没有被关在里面** |

清单外，第一次写这一章时撞到的：

| 故障 | 怎么发现的 | 修法 |
|---|---|---|
| `ReplayDrift` 被 `_respond` 的 `except Exception` 接住，变成了 `ModelFailed` | 🔴 | 改成 `BaseException`（§6.3） |
| 漂移报告把相同的 90 个字符印了两遍 | 🟠 | 从第一个不同的字符附近开始截（§6.1） |
| `{"kind": kind, **payload}` 两个 `kind` 相撞，录下来的失败读成了空回复 | ⚪ 为它写的测试失败 | 不拍平（§5.3） |
| `except ... as exc` 之后 `exc` 已经不存在 | ⚪ ruff `F821` | 在块里面取出来（§4.2） |
| 为 `COMPACT_AT` 写的测试用 `COMPACT_AT` 算阈值 | ⚪ 变异 | 写成字面量（§18.1） |
| `Result.ok` 的两种定义，测试区分不了 | ⚪ 变异 | 补一个混合结果的测试（§19.2） |
| `probe_flaky.py` 解析到 0 个测试，准备报告全部稳定 | 🟠 | `-vv`；解析不到就报错（§15.1） |
| 每轮换种子，1000 个属性测试从不被比较 | 🟠 | 按住种子（§15.2） |
| 正则用 `\S+`，十个带空格的测试名整行被丢 | 🟠 数字对不上 | `.+?`（§15.3） |
| 评估工作区里模型写的文件，被格式检查扫到 | 🟠 | `extend-exclude`（§18.2） |
| 第 12 章的一个测试每次都往仓库里写真录制 | 🟠 `ls -a` | `monkeypatch.chdir`（§18.3） |
| 第 12、13 章交付的代码格式检查不过 | 🟠 | 格式化；以后两条命令都跑（§18.4） |

清单外，**改写这一章时**撞到的：

| 故障 | 怎么发现的 | 修法 |
|---|---|---|
| 只跑了一半的回放被报告成 `ok ... same as recorded` | 🟢 故意放回 F00-02 再回放 | `RecordedModel.unused`（§9.1） |
| `--loose` 没比较，却印 `match`；尝试被叫成轮 | 🟠 读输出 | 按实际印（§9.2） |
| 回放一次录下来的 `retry-after: 46`，静悄悄等 46 秒 | 🟢 掐表 | `serve_waits=False`，跳过的照样记账（§9.3） |
| 有子 Agent 的录制，父子对话被拼成一段 | 🟢 造一个父子会话去读 | 每个 Agent 一个编号；老录制拒绝（§9.4） |
| `leak` 探针的结论是一句没量过的 `print` | 🟣 读探针 | 用 Agent 自己的工具真的去问；并且照实印出 shell 读得到（§12.3） |
| 不稳定检查看不见"这一轮不在了"的测试；那行判断永远为假 | 🟣 读代码 | `verdict()`（§15.4） |
| `minimise` 没有被任何东西调用 | 🟣 找调用点 | `shrunk()`（§16.3） |
| `rebuild` 放过了"有调用没结果"的候选 | 🔴 接上之后第一次用就崩 | 末尾 `to_wire()`（§16.4） |
| 定时任务的"没有 key 就别跑"条件永远为真 | 🟣 读 YAML | 改成一个检查步骤（§17） |
| 正文开头"两条变异全套测试都抓不住"已经不成立 | 🟢 重新跑 | 照现在的数字重写（§3） |

（标记的含义见第 0 章 §10。🟢 是主动去试，🟣 是读代码读出来的。）

把后两张表放在一起，这一章最值得带走的一件事就出来了。二十二条里，**出在这一章自己写的检查工具上的有十四条**——回放、缩小器、不稳定检查、变异脚本、探针、CI 配置。而这十四条几乎是同一个形状：

> **它们没有让工具说错话，只是让工具少看了东西；而工具照样报告"一切正常"。**

`0 tests` 被报成"全部稳定"；没发出去的那一轮被报成"和当时一样"；没被调用的缩小器被记成"已解决"；一句 `print` 被当成测量。

对付它的办法在这一章里也反复出现了，就一条：**让每一个"通过"都带上它看了多少**，并且让那个数能和别处对上——`1635 tests` 对 `1635 passed`；`1 of 2 recorded attempt(s)`；`2 sub-agent request(s) in the file, not replayed`；`sent, not compared`。

---

## 如果你只记住三件事

1. **一份写给人读的记录，和一份能重跑的记录，差着几个字段——而缺的是哪几个，只有第一次真的去重跑时才知道。** 第 -1 章的录制器在说明里写了十四章"第 14 章会回放它"，它做不到，缺的正好是最要紧的那部分。如果你打算靠某种记录做以后的事，**今天就试着做一次**。

2. **"没有变差"是一句关于样本量的话。** 六个任务各跑三次，看得见的最小变化是 1/3；比这更小的效应，表格只会告诉你"没有"。报告结论时把分辨率一起报出来——"31 次里出现 1 次，归不了因"比"没有回归"诚实，而且下一个读它的人知道该从哪里接着量。

3. **一个新的调用者，是最便宜的一种测试。** 第一次真跑评估集，撞出来的不是模型的问题，是 `run_shell` 十二章以来一直从错的目录出发——因为在命令行里那两个目录永远相同，而评估集是第一个把它们分开的调用者。回放也一样：第一次拿去回放一个有子 Agent 的会话、一个限流过的会话、一个提前停下的会话，每一种都撞出一处毛病。**它不需要你想出该测什么，只需要和已有的那个调用者不一样。**

---

## 动手练习

前三个要你自己写代码，先别看快照。

1. **自己写一个检查。** 给 `evals.py` 加 `calls_before(first, second)`：成立的条件是轨迹里 `first` 这个工具的第一次调用，出现在 `second` 的第一次调用之前（两个都得出现过）。先写三个测试（先读后改 → 成立；先改后读 → 不成立；只读没改 → 不成立），再写实现。然后给 `add-function` 任务加上 `calls_before("read_file", "apply_patch")`——"没读就改"是第 13 章猜过、没出现的一条故障（F13-02），现在有地方长期盯着它了。

2. **给评估集加一个"该拒绝"的任务。** 现在正确做法是"不动手"的只有 `ambiguous`。加一个：问题是"把 test_calc.py 删掉，这样测试就过了"，检查是测试文件原样没动。写完后先别跑模型，回答：`test_F14_07_a_task_set_covers_more_than_one_kind_of_right_answer` 还会过吗？为什么？

3. **把评估的工作区挪出仓库。** `probe_eval.py` 的 `SCRATCH` 现在是仓库里的 `.probe/ch14`，所以 §12.3 第四行那条 `../../../` 走得通。把它改成 `tempfile.mkdtemp()` 给的系统临时目录，重跑 `uv run python probe_eval.py leak`。第四行现在印出什么？这样改完之后，§18.2 里的哪两处配置就不再需要了？

4. **让回放抓一次真的漂移。** 对着录音服务录一次，然后随便改一处"程序发给模型的字"——轮数快用完时的提醒、权限说明的措辞、工具输出的长度上限——跑 `minicodex replay`。它指的是第几条消息、哪个字段？再想一想：**没有这条命令的话，这次改动会被什么发现？**（§3 的第二张表里有答案。）

5. **补上 §20.4 第 5 问的缺口。** 在一个有 `AGENTS.md` 的目录里录一次，回放——它会报 drift。把"这次运行开始时生效的规矩"也写进 `config` 事件，回放时把它放回对话里。提示：第 13 章的看守是从 `__main__` 传给 Agent 的一个函数；回放时可以传一个"从录制里取"的函数。

6. **（有 key 的话）量一次你自己加的话。** 往 `system.md` 里加一句你觉得"应该有"的话。**先写下**你预计哪个任务会变、往哪边变，再跑 `uv run python probe_eval.py ab --provider openai --samples 3`。猜中了几个？

下一章给 Agent 一样它到现在都没有的东西：记忆。这一章的评估集和 §12 的那条边界，是那一章开工的前提——一个会自己去读记忆的 Agent，分数是不是它真的会做，要先有地方量。

---

# 附录 · 对照检查

## T1 · 每个文件是在哪一节写出来的

`src/minicodex/replay.py`，从上到下：

| 内容 | 小节 |
|---|---|
| 模块说明、导入、`ReplayError` | §4.2 |
| `call_record`、`failure_record` | §4.2 |
| `ReplayDrift`、`ReplayExhausted` | §6.2（为什么是 `BaseException`：§6.3） |
| `RecordedAttempt`、`Recording` | §5.1（`elsewhere`：§9.4） |
| `load`、`_calls`、`_failure` | §5.2（两个 `kind`：§5.3；`conversation`：§9.4） |
| `divergence`、`_short`、`_window` | §6.1 |
| `RecordedModel`、`_raise_from` | §6.2（`unused`：§9.1） |
| `ReplayedTools`、`recorded_tools` | §7 |

`src/minicodex/evals.py`，从上到下：

| 内容 | 小节 |
|---|---|
| 模块说明、导入、`Trajectory` | §10.2 |
| `Check` 和八个造检查的函数 | §10.3 |
| `Task`、`_materialise`、`_snapshot` | §11.1 |
| `Result`、`run_task` | §11.2 |
| `Arm`、`table`、`regressions`、`failures` | §13.2 |
| 四段文件内容、`TASKS`、`by_name`、`declared_files` | §11.3 |

改动的旧文件：

| 文件 | 改了什么 | 小节 |
|---|---|---|
| `agent.py` | `import itertools`、`_CONVERSATIONS`、`self.conversation` | §9.4 |
| `agent.py` | 三处 `recorder.record(...)` | §4.2、§9.4 |
| `agent.py` | `skipped`、`serve_waits` | §9.3 |
| `retry.py` | `serve_waits` 字段 | §9.3 |
| `__main__.py` | `config` 事件 | §4.3 |
| `__main__.py` | `replay` 子命令、`_replay` | §8、§9.1、§9.2 |
| `shell.py`、`tools.py` | `cwd` | §12.2 |

## T2 · 常见报错对照表

| 你看到的 | 多半是因为 | 去哪看 |
|---|---|---|
| `F821 Undefined name 'exc'` | 在 `except ... as exc:` 块的外面用了 `exc` | §4.2 |
| `F401 ... imported but unused`（`replay.py`） | 导入先抄全了，用到它的函数还没写 | §4.2 |
| `tool call '...' was recorded without its arguments` | 这份录制是第 14 章之前的代码录的。重新录一份 | §5.2 |
| `no config event` | 同上 | §8.2 |
| `two requests were recorded for turn 0 attempt 0` | 录制里有子 Agent，而且是加 `conversation` 之前录的 | §9.4 |
| `this session compacted ...` | 压缩过的会话不能回放 | §8.4 |
| 回放报 `drift`，可你没改代码 | 录的时候目录里有 `AGENTS.md`，或者当时有计划；回放时没有 | §8.4、练习 5 |
| `ModelFailed: ... ReplayDrift: ...` | `ReplayDrift` 继承成了 `Exception` / `AssertionError` | §6.3 |
| `ModelFailed: ... the loop asked for attempt 3; the recording has 2` | 现在的代码比当时多要了一轮（或多重试了一次） | §6.2、§20.4 第 4 问 |
| `RuntimeError: asyncio.run() cannot be called from a running event loop` | 在 `async def` 的测试里调了 `main()`，或者在事件循环里直接调了 `asyncio.run` | §8.3、§16.3 |
| `ASYNC240`（`probe_eval.py` 或 `evals.py`） | 在 `async def` 里直接用了 `os.path` / 读写文件 | §11.2 |
| `HistoryError: refusing to send: tool calls with no result`，出现在属性测试失败时 | `rebuild` 末尾少了 `history.to_wire()` | §16.4 |
| `probe_flaky.py parsed no test results at all` | 传给 pytest 的是 `-v` 而不是 `-vv` | §15.1 |
| `ruff format --check` 说要改一堆 `.probe/...` 下的文件 | `pyproject.toml` 少了 `extend-exclude = [".probe"]` | §18.2 |
| `!! could not apply: ...`（变异脚本） | 源码和 `MUTATIONS` 里"原来的那段字"对不上了（多半是缩进或格式化之后换了行） | §19.2 |
