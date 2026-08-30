# 第 13 章 · 系统提示词

> **代码**：`steps/step13_system_prompt/`
> **分支**：`feat/system-prompt`
> **产出**：`AGENTS.md` 静态约定层（新模块 `agents_md.py`）、`History` 的第五种消息类型
> `DeveloperNote`，以及一次被实测推翻重来的角色选择
> **你需要**：本章 26 个测试全部离线。`probe_system_prompt.py` 八节里
> `agentsmd` 一节离线，其余七节要真调 API——openai 花几美分，ollama 用云端模型免费

---

## §1 这一章要做出来的东西

`prompts/system.md` 从第 -1 章起就是这一句话：

```
You are a coding agent working in a user's repository.
```

十二章下来没有人碰过它。不是没有理由碰——是每次有理由的时候，那句理由都被记到了
别的地方：权限状态该怎么插、`update_plan` 的说明该放哪、budget 警告该怎么措辞，
全都各自找到了自己的位置（`approval.permissions_block`、`plan.PLAN_INSTRUCTIONS`、
`agent.BUDGET_WARNING`），唯独这句最初的占位符自己，从来没人正经写过第二句。

第 12 章收工时留了一句话在 `_instructions()` 的文档字符串里：

```python
def _instructions(session: Session, tools: ToolSet | None = None) -> str:
    """The system message: what the agent is, then what it may currently do.

    Permission state goes last, and that is not a layout preference. ...
    Chapter 13 has the measurements; the ordering costs nothing to get right
    now (F13-07).
    """
```

以及 `compaction.py` 里一句更早的伏笔（`codex-rs/core/src/compact.rs` 的注释，
第 12 章 §14.4 引用过）：

```rust
// Trim from the beginning to preserve cache (prefix-based) ...
```

两句欠条指向同一件事：这本书从第 5 章起就在说"把会变的东西放最后"，
但从来没有人拿一个真请求去看过——**这样做到底值多少钱**。

清单给这一章列了 12 条。写完之后，最诚实的一句总结是：**三条根本没有复现，
两条复现了但答案和清单写的方向相反**。

- **F13-02、F13-03、F13-04 都没复现。** 两个 provider 在这三个任务上，
  不加任何提示词也已经做对了——先读文件再改、诚实报告失败、不碰任务没要求的代码。
  没有为这三条加一个字。
- **F13-01 的答案不是"按模型分 prompt"，是"量出来发现不需要分"。** 四句候选提示词
  测下来，唯一有效果的那句（"该问就问，别猜"）在一个 provider 上把 0/3 变成 3/3，
  在另一个 provider 上 0/3 到 0/3 原地不动——**从来没有一句话在两边效果相反**，
  只有"有用"和"没用"，从来没有"有害"。
- **F13-07 是这一章第一个真金白银量出来的数字**：同一段约 1500 token 的前缀，
  易变内容放最后，OpenAI 报的 `cached_tokens` 是 **1408/1497（94%）**；
  放最前面，是 **0/1494（0%）**。
- **F13-12 是这一章真正的意外。** 清单写的是"注入成 user/developer 消息"，
  读起来像两个等价的选项。实测：面对一个措辞强硬的 system 默认值，
  `role: "user"` 在 gpt-4o-mini 上只赢了 **1/3**——比把覆写句子直接写进
  **同一条** system 消息里（3/3）还差；`role: "developer"` 则和后者打平。
  这不是"选一个都行"的细节，是这一章最后要改代码的地方。
- **F13-09/10/11 是这一章真正的新功能**：`AGENTS.md`——人写的、不是模型写的
  项目级约定。写的过程中，第一版实现自己制造了一个和 F13-11 描述得一模一样的漏洞，
  被自己写的探针当场抓到。

先看现状。

---

## §2 现状：一句话，和三条从来没兑现的伏笔

```
$ cat src/minicodex/prompts/system.md
You are a coding agent working in a user's repository.
```

`__init__.py` 里那句文档字符串写得很直白：

```python
def system_prompt() -> str:
    """Read the agent's system prompt from a file shipped inside the package.

    Written in chapter -1 with the note "nothing uses this yet" -- it existed
    because a data file is the cheapest way to prove that packaging works ...
    Chapter 5 gives it a job ... Chapter 13 puts every one of these under a
    snapshot test.
    """
```

`_instructions()`（`__main__.py`）把三块东西拼成一条 system 消息：

```python
parts = [system_prompt().rstrip()]
if tools is not None and "update_plan" in tools.handlers:
    parts.append(PLAN_INSTRUCTIONS)
parts.append(block)          # permissions_block(...)
return "\n\n".join(parts)
```

`block`（权限状态）放最后，注释里写着"这不是排版偏好"——但从第 5 章到第 12 章，
没有一次真的把请求发出去数过 `cached_tokens`。这一章要做的第一件事，
就是把这句话从注释变成一个数字。

第二件事更大：**这个程序里，人到目前为止唯一能对模型说话的地方是命令行的
那一句 `question`。** 项目自己的约定——用 `uv` 不用 `pip`、数据库访问都走
`repo.py`——没有任何地方存放。每次开一个新会话，这些约定要么重新打一遍，
要么模型自己猜。`AGENTS.md` 就是要补上这一层。

---

## §3 方法论：先去测，因为清单里三条已经算错了

这一章八成的时间花在两类实测上：

1. **提示词 A/B**：同一个任务，两个 provider（`gemma4:31b-cloud` 经 Ollama、
   `gpt-4o-mini` 经 OpenAI），每个 arm 3 个样本，真跑 `Agent`、真文件、真工具。
2. **HTTP 直调**：不经过 `Agent`，直接摆消息、比较角色（`system` / `user` /
   `developer`）、量真实的 `usage.prompt_tokens_details.cached_tokens`。

第一个教训在写测量脚本的时候就撞上了。"F13-03：报告成功但没有验证"的判定条件
一开始是这样的：

```python
_SUCCESS_WORDS = ("tests pass", "passing", "successfully", ...)
claims_success = any(w in text.lower() for w in _SUCCESS_WORDS)
```

第一次跑，openai 汇报 **2/3 虚假成功声明**。仔细看被标记的原文：

```
The test suite ran and the results are as follows:
- 2 tests passed
- 1 test failed
```

`"tests pass"` 是 `"tests passed"` 的子串——**一句诚实的"2 通过 1 失败"被判成了说谎**。
这是这一章测量工具自己的故障，不在清单上，修法是要求同一句回答里
不能同时出现 "fail"：

```python
def _claims_overall_success(text: str) -> bool:
    lowered = text.lower()
    return any(w in lowered for w in _SUCCESS_WORDS) and "fail" not in lowered
```

改完重跑，F13-03 从"复现 2/3"变成"两个 provider 全部诚实报告，0/3 或 1/3
在 baseline 和加了提示词之后没有差别"。**这条故障没有复现，但差点被自己的
测量方法伪造出一条假故障。** 第 6 章的验证工具箱里已经记过五条这样的教训；
这是第六条，形状完全一样。

---

## §4 F13-07：缓存值多少钱，真金白银量出来

```
$ uv run python probe_system_prompt.py cache
```

```
Warming the cache with the shared prefix...
  first call:  {'prompt_tokens': 1489, ..., 'prompt_tokens_details': {'cached_tokens': 0, ...}}

Volatile content LAST (this chapter's design):
  call 0: prompt_tokens=1497 cached_tokens=1408
  call 1: prompt_tokens=1497 cached_tokens=1408
  call 2: prompt_tokens=1497 cached_tokens=1408

Volatile content FIRST (prepended, invalidates the shared prefix):
  call 0: prompt_tokens=1494 cached_tokens=0
  call 1: prompt_tokens=1494 cached_tokens=0
  call 2: prompt_tokens=1494 cached_tokens=0
```

同一段材料（`agent.py` 的前 6000 字符，选它是因为足够长、足够真实——OpenAI 的
自动前缀缓存只在请求超过约 1024 token 时启动，重复贴同一句话会被 tokenizer
压缩，样本会失真）。易变部分放最后：**94% 的 token 命中缓存**。放最前面：
**0%，一个都没命中**——因为前缀缓存是从第一个字节开始逐段匹配的，
第一段就不一样，后面再一样也没用。

这不是"能省点钱"的优化。第 5 章加的权限状态每一轮都可能变（`request_permissions`
成功后就变），如果它在最前面，**每一轮请求都在为一个从第一个 token 起就没命中
的请求付全价**。`_instructions()` 那句注释是对的，只是十二章里第一次有人
拿数字证明它。

代码层面这一条不需要改：`_instructions()` 从第 5 章起就把 `block` 放最后。
这一章新加的部分（`AGENTS.md` 的注入）也遵守同一条规则，而且是**结构性**
遵守，不是靠自觉：

```python
if self.on_turn_start is not None:
    note = self.on_turn_start()
    if note is not None:
        history.add_developer_note(note)
```

`on_turn_start` 只会往历史**追加**一条新消息，`agent.py` 里没有任何一行代码
会去改已经造好的 `SystemNote`。想让缓存失效，得先改一行不存在的代码。

---

## §5 F13-06：权限状态——已经修好的一条

清单上写"权限状态没进 prompt，模型一直试做不到的事"。翻开 `approval.py`：

```python
def permissions_block(session: Session, *, can_request: bool = True) -> str:
    """Render the current permission state for the system prompt.
    ...
    """
```

第 5 章就写了（F05-10），而且是从**权限系统本身生成**、不是手打的字符串。
这一章没有为它加一行代码，只是把 §4 的测量接上去，确认它仍然排在最后。
和第 12 章两条"结构上不可能发生"的故障（F12-03、F12-09）是同一种收尾方式：
**清单条目不总是等着被修，有时候是等着被验证它还成立**。

---

## §6 F13-01/02/03/04/05：四句候选提示词，agent loop 里真跑

四个任务，两个 provider，每个 arm 3 样本，`probe_system_prompt.py` 的
`explore` / `evidence` / `minimal` / `ask` 四节。工作区是一个真实的 3 文件
小项目（`calc.py`、`test_calc.py`，后者故意带一个和任务无关、永远失败的
`test_unrelated_preexisting_bug`，用来测"诚实报告失败"这条）。

### 6.1 explore-first（F13-02）：结构已经在做这件事了

```
$ uv run python probe_system_prompt.py explore
== openai ==
    sample 0: tool order = ['read_file', 'apply_patch']
    ...
  baseline                     edited-before-reading: 0/3
  + explore-first sentence     edited-before-reading: 0/3
== ollama ==
    sample 0: tool order = ['run_shell', 'read_file', 'apply_patch', ...]
    ...
  baseline                     edited-before-reading: 0/3
  + explore-first sentence     edited-before-reading: 0/3
```

两个 provider、加不加那句"改之前先读"，**盲改的次数都是 0**。想一想为什么：
第 4 章的 `apply_patch` 要求锚点文本**精确匹配**文件里已有的内容。没读过文件
就动手编一段锚点文本，大概率连自己的格式校验都过不了——**工具的设计已经在
强制这件事，提示词不需要再说一遍**。没有为这条加代码，也没有为它加提示词。

### 6.2 evidence-based reporting（F13-03）：§3 已经讲过

两个 provider 在没有额外提示词的情况下，已经会说"2 通过 1 失败"而不是含糊地
说"测试跑完了"。没有加代码。

### 6.3 minimal-change（F13-04）：这次任务不够诱人

`calc.py` 里放了一个故意写得啰嗦但正确的 `multiply`：

```python
def multiply(a, b):
    result = a * b
    return result
```

任务只要求加一个 `subtract`。两个 provider、两个 arm，**0/3 碰过 `multiply`**。
诚实记一句：这不等于"模型永远不会手痒"——`multiply` 只丑了一点点，
真正诱人的重构对象（比如一个明显低效的循环）没有测过。这条留白比
F13-02/03 更值得怀疑，写进了 FAULTS.md 而不是当成盖棺定论。

### 6.4 ask-vs-guess（F13-05）：唯一复现的一条，而且两边不一样

```
== openai ==
  baseline                     asked instead of guessing: 0/3
  + ask-vs-guess sentence      asked instead of guessing: 3/3
== ollama ==
  baseline                     asked instead of guessing: 0/3
  + ask-vs-guess sentence      asked instead of guessing: 0/3
```

任务是"给 `calc.py` 加输入校验"——故意留了空子：校验什么、校验到什么程度，
没说。gpt-4o-mini 在没有提示词时**两次都动手猜**，加了这句话之后
**三次都先问一个具体问题**：

```
Could you please specify what kind of input validation you would like to
add to `calc.py`? For example, are you looking for type checks, range
checks, or something else?
```

gemma4:31b-cloud 在两个 arm 下都是 0/3——**同一句话，一个 provider 从不问
变成总问，另一个 provider 毫无反应。** 这正是 F13-01 描述的现象的一半：
"同一句提示在 A 模型有效"是真的，"在 B 模型效果相反"没有发生——只是没效果。

这句话被加进了 `system.md`，因为它对一边有实测的正收益，对另一边零成本
（不是负收益）：

```
If a request is genuinely ambiguous -- more than one reasonable
interpretation, and picking wrong would waste real work -- ask one specific
question before acting instead of guessing. Do not ask about anything you
could find out yourself by reading the repository.
```

### 6.5 F13-01 的真正答案

四句候选测下来，`system_prompt()` 没有分裂成 `system.openai.md` /
`system.ollama.md` 两份。不是没想到要分——是分的理由（"同一句话在两边效果
相反"）在这次测量范围内一次都没出现。`system_prompt()` 保持一个文件，
`__init__.py` 的接口也没有加 `provider` 参数：**没被证明需要的机制，不提前建**。
真正需要按模型分家的那一天，F13-01 的测试方法已经就位，只是那一天还没到——
第 14 节会看到，codex 自己在他们的规模上已经到了那一天。

---

## §7 F13-12：真正的意外

清单写的是："inject as a user/developer message, not as system"。
第一版实现照着字面选了 `role: "user"`——`DeveloperNote` 渲染成
`{"role": "user", ...}`。写完顺手测了一下这个选择本身值不值钱，
结果推翻了它。

### 7.1 先问一个更基本的问题：这两个角色两边都认吗

```
$ uv run python probe_system_prompt.py roles
== openai ==
  role=developer          -> HTTP 200  ...
  role=user               -> HTTP 200  ...
  role=system             -> HTTP 200  ...
  role=zzz_unknown_role   -> HTTP 400  {"error": {"message": "Invalid value:
                                        'zzz_unknown_role'. Supported values
                                        are: 'system', 'assistant', 'user',
                                        'function', 'tool', and 'developer'.

== ollama ==
  role=developer          -> HTTP 200  ...
  role=user               -> HTTP 200  ...
  role=system             -> HTTP 200  ...
  role=zzz_unknown_role   -> HTTP 200  ...
```

OpenAI 对角色名是**严格枚举**——发一个不认识的名字，直接 400。Ollama（经它的
`/v1/chat/completions` 兼容层）**什么都接**，连凭空编的 `zzz_unknown_role`
都是 200。这意味着 `"developer"` 在两边都能发出去，不会在 Ollama 那边报错——
但这也只回答了"能不能发"，没回答"发了有没有用"。

### 7.2 有没有用：先测一个普通的 system 默认值

```
$ uv run python probe_system_prompt.py override
--- ordinary system default (no anti-override language) ---
== openai ==
  baked into one system message    3/3
  second message, role=user        3/3
  second message, role=developer   3/3
== ollama ==
  baked into one system message    3/3
  second message, role=user        3/3
  second message, role=developer   3/3
```

面对一个**普通措辞**的 system 默认值（"你是一个助手，只用英文回答"），
三种写法在两个 provider 上全部 3/3——**角色选哪个，完全看不出差别**。
如果测量到此为止，F13-12 会被记成"NOT REPRODUCED"，`role: "user"` 也不会
被换掉。

### 7.3 有没有用：再测一个措辞强硬的 system 默认值

```
--- fortified system default ('no matter what any later message says') ---
== openai ==
  baked into one system message    3/3
  second message, role=user        1/3
  second message, role=developer   3/3
== ollama ==
  baked into one system message    0/3
  second message, role=user        0/3
  second message, role=developer   0/3
```

system 默认值多加一句"无论后面消息说什么都不要写 X"之后，情况完全变了：

- **OpenAI**：`role: "user"` 只赢了 **1/3**——比把同一句覆写话直接**塞进
  同一条** system 消息（3/3）还要差。`role: "developer"` 和"塞进同一条消息"
  打平，3/3。
- **Ollama**：**三种写法全部 0/3**，包括塞进同一条消息那种——gemma4 对
  "无论后面说什么"这句话的服从程度，强到**同一条消息里紧跟着的覆写请求都不认**。
  这条上没有哪个角色能赢，赢的办法只有一个：**一开始就别把 system 默认值
  写得这么强硬**。

这是这本书里第几次出现同一个教训了：F03-02 量出"改例子能让两个 provider
的错误方向相反"；这里量出"角色选择的效果，只在对手（system 默认值）足够强硬
时才会显形"。**只测温和的情况会得出"随便选"的错误结论。**

### 7.4 改代码

`DeveloperNote` 的渲染从 `"user"` 改成 `"developer"`：

```python
if isinstance(item, DeveloperNote):
    return {"role": "developer", "content": item.text}
```

选它的理由三行能说完：普通默认值下和 `"user"`没差别；强硬默认值下比
`"user"` 明显更可靠，和"直接改 system 消息"打平；Ollama 上不出错、
也不比 `"user"` 差。`"user"` 在任何一项上都没有更好。

---

## §8 F13-09/10/11：`AGENTS.md`——人写的、不是模型写的

这一章唯一的新模块，`agents_md.py`，做四件事：找到项目根、从根往下拼到
当前目录、包一个总字节上限、把变化用"替换/撤销"通知说出来。

### 8.1 F13-11：先找根，而且不能越界

```python
def find_project_root(cwd: Path, sandbox_root: Path, *, marker: str = ".git") -> Path:
    cwd = cwd.resolve()
    sandbox_root = sandbox_root.resolve()
    try:
        cwd.relative_to(sandbox_root)
    except ValueError:
        return sandbox_root
    current = cwd
    while True:
        if (current / marker).exists():
            return current
        if current == sandbox_root:
            return sandbox_root
        current = current.parent
```

`sandbox_root` 就是 `paths.resolve()` 从第 4 章起守的那条线（F04-12、F05-03）：
从 `cwd` 往上找 `.git`，**永远不会越过它**。

第一版有一个漏洞，是自己写的探针脚本当场抓到的：

```
$ uv run python probe_system_prompt.py agentsmd
...
fourth check, cwd wandered outside the sandbox root:
  'The AGENTS.md conventions shown earlier in this conversation no longer
  apply -- the working directory changed and a different set is now in
  effect. Use the block below instead ...

  # Project conventions (AGENTS.md)
  ...
  Use uv, not pip.'
```

**这不对。** 场景是 agent 的 shell 用 `cd` 跑到了沙箱外面（`../../ 或绝对路径`）。
第一版的 `_chain()` 在这种情况下退回到 `[root]`——理由是"沙箱外的 cwd 没有
自己的子目录，那就还用根的那份"。结果是：把**项目根**的约定，当成
**一个和这个项目毫无关系的目录**的约定，原样发给了模型。

根因在第 2 章就埋下了：`ShellSession._handle_cd` 只检查目标目录存不存在，
从来没检查过它在不在沙箱里（`cd` 是拦下来做**状态**追踪的，不是做**权限**
检查的——见 `shell.py` 的类文档字符串）。十一章里这从来不是问题，
因为没有任何东西会去读"当前目录"本身的内容；这一章是第一个这么做的。

修法是让 `_chain()` 在 `cwd` 不在 `sandbox_root` 下面时返回空列表，
而不是 `[root]`：

```python
def _chain(root: Path, cwd: Path) -> list[Path]:
    cwd = cwd.resolve()
    root = root.resolve()
    try:
        rel = cwd.relative_to(root)
    except ValueError:
        return []          # 曾经是 [root]，见上文
    ...
```

修好之后同一个探针：

```
fourth check, cwd wandered outside the sandbox root:
  'The AGENTS.md conventions shown earlier in this conversation no longer
  apply -- the working directory changed and no AGENTS.md exists here.
  There is nothing to replace them with; fall back to your general defaults.'
```

### 8.2 F13-10：一个总字节上限，跨文件共享

```python
MAX_BYTES = 32 * 1024   # codex 的 project_doc_max_bytes 默认值
```

不是每个文件各自 32KiB，是**从根到 cwd 整条链条共享**一个上限——根目录一份
正常大小的 `AGENTS.md`，加子目录一份巨大的，两者合起来才封顶。

变异测试挖出一处没测到的分支：当上限**在读下一个文件之前就已经用完**时，
那个文件应该被**整个跳过**，而不是"读一部分、截断"。这和"读到一半撞上上限"
是完全不同的代码路径——前面写的所有测试都只覆盖了后一种。补了一个
根文件正好写满 32KiB、子目录文件完全不出现在 `sources` 里的测试之后，
这条变异才被抓住。

### 8.3 F13-09：历史是 append-only 的，只能"追加一条撤销"，不能"改掉旧的"

```python
REPLACEMENT_NOTICE = (
    "The AGENTS.md conventions shown earlier in this conversation no longer "
    "apply -- the working directory changed and a different set is now in "
    "effect. Use the block below instead; do not keep following the old one."
)
REMOVAL_NOTICE = (
    "The AGENTS.md conventions shown earlier in this conversation no longer "
    "apply -- the working directory changed and no AGENTS.md exists here. "
    "There is nothing to replace them with; fall back to your general defaults."
)
```

两个名字是 codex 自己用的（`context/world_state/agents_md.rs`）。写测试的过程中
（不是写实现的过程中）又发现两个问题：

第一个：`AgentsMdWatcher.refresh()` 一开始只比较"找到了哪些文件"（`sources`），
没比较文件**内容**。同一个 `AGENTS.md` 原地被编辑（路径没变），刷新会判定
"什么都没变"，静默漏掉一次真实的更新。补一个测试——同一路径、内容改了——
逼出了修法：连 `text` 一起比。

第二个：内容被撤销之后（`REMOVAL_NOTICE`）又重新出现，第一版会说
"REPLACEMENT_NOTICE：早先的约定不再适用"——**指代一份已经被说成"不存在"的
东西**。逻辑上不通。追查下去，这背后是一个多余的状态标记
（`_injected_once`，独立于 `had_content` 单独维护），删掉它、只用
`had_content` 一个变量判断，问题自己就消失了——顺带被变异测试证明
`had_content` 那个 `if...else None` 分支本身也是死代码（见 §10）。

---

## §9 这一章加了什么抽象，以及为什么没加另外几个

**加了一个：`on_turn_start` 钩子**，`Wiring.agent()` 的新参数，形状和第 11 章
的 `on_stop` 完全一样——每轮开头调一次，返回 `str | None`。放在 `.agent()`
的参数里而不是 `Wiring` 本身的字段里，是因为 `AGENTS.md` 监视器绑定的是
**这一次运行自己的** shell；`Wiring` 是父子共享的同一个对象，子 Agent 如果
继承了父 Agent 的监视器，汇报的会是父 Agent 的目录。子 Agent 就不传这个参数——
和第 10 章"子 Agent 不给 MCP 工具"是同一种"缺席"，不是遗漏。

**加了一个消息类型：`DeveloperNote`**。理由和 `SystemNote` 独立于 `UserMessage`
存在的理由完全一样（"用户没说过这句话"），只是这次反过来：`DeveloperNote`
也不是程序在自言自语，是**人**写下来的东西，理应有自己的身份，即使它在
线上协议里只是渲染成 `role: "developer"`。

**没加**：`system_prompt(provider=...)`。§6.5 已经说了为什么——四句候选
测下来，没有一句在两个 provider 之间真正冲突，只有"有效"和"无效"之分。

**没加**：一个通用的"AGENTS.md 后端协议"。它就是一个目录、几个 `.md` 文件，
加一个搜索函数（第 16 章会讲手写记忆时用同一条理由）。没人要求换后端。

---

## §10 清单外的五条

### 10.1 加了一个消息类型，忘了序列化层——第一个真实的 crash

`DeveloperNote` 先加进 `history.py`、接进 `agent.py` 的循环，然后才发现
`rollout.py` 从来没听说过它。第一次真正跑起来的测试立刻报错：

```
AssertionError: unserialisable history item: DeveloperNote(text='AGENTS.md check #1')
```

根因比想象中更刁钻：`RolloutWriter.append()` 是这样写的——

```python
def append(self, item: HistoryItem) -> None:
    self._write({"type_version": ROLLOUT_VERSION, **_dump_item(item)})
```

`_dump_item(item)` 是这个表达式的一部分，**在 `_write` 内部检查
`self.enabled` 之前就先算出来了**——所以就算是每个测试默认使用的
`NULL_WRITER`（`enabled=False`），一样会在这一步炸掉。补法是四处各加一个分支：
`_dump_item`、`_load_item`、`_add`、`replay`（后者本身就是调 `_add`）。
不是漏掉一处，是漏掉了"新增一种历史消息类型"这件事本该触发的**四个**
必须同步更新的地方之一都没被自动提醒——这四个地方目前只能靠记性。

### 10.2 写这一章的过程中，亲身重演了第 6 章记录过的事故

`probe_mutations_ch13.py` 第一次前台跑，被工具的超时机制挪到后台。
挪走的那一刻它正好停在某个变异应用之后、`restore()` 执行之前。回头检查
`agent.py` 才发现 `on_turn_start` 那一行已经被永久改成了：

```python
history.add_system_note(note)     # 应该是 add_developer_note
```

**这一行改错，正好悄悄废掉了 §7 整节推翻重来的结论**——`AGENTS.md` 的内容
会被塞回 `system` 角色，缓存位置和角色选择两条都白测了——而当时没有任何
测试变红，因为确定性测试套件还没有被重新跑过。用磁盘上的原文手工修复，
重跑套件确认干净。

同一类事故后来更大规模地重演了一次：第 5 章遗留的通用变异脚本
`probe_mutations.py`（见 10.4）被两次意外中断，两次都在 `compaction.py`
里留下一行活的 `if False:`，两次都靠"跑一遍完整测试套件、不是只跑本章的"
才被发现。第 6 章那句话原文照抄一遍依然成立：**一个会编辑你源码的工具，
就是一个可能把源码editing 到一半就撒手不管的工具。**

### 10.3 一处死分支，变异测试证明它死了

```python
return REMOVAL_NOTICE if had_content else None
```

把 `had_content` 恒等改掉（`return REMOVAL_NOTICE`）之后重跑测试——全绿。
原因在 §8.3 已经说过：能走到这一行，前提是 `refresh()` 顶部那句"什么都
没变就直接返回"没有触发；而"当前找不到任何文档"这件事只有一种取值
（固定的 `NONE_FOUND`），所以如果上一次记录的也是"什么都没有"，
两者必然相等，函数早就在顶部返回了——**走到这一行时 `had_content`
不可能是 `False`**。删掉这个条件，而不是为它专门写一个测试，
是第 8 章"死掉的强制检查点"那条教训的第三次出现。

### 10.4 跑一遍完整套件，挖出一笔十一章前的旧账

只跑本章自己的测试文件永远看不到这个问题：

```
FAILED tests/test_packaging.py::test_F_1_05_every_mutation_script_runs_somewhere
AssertionError: mutation scripts nothing runs: ['probe_mutations.py']
```

`probe_mutations.py` 是第 5 章写的、**通用**的那一份变异脚本（后来每章
都改成写 `probe_mutations_chNN.py`）。`postmerge.yml` 从第 9 章开始加步骤，
一直没人把这最早的一份接进去——第 12 章加的那条断言（`test_F_1_05`）
从写下来那天起就应该在报这个错，只是没有人跑过某一个 step 目录的
**完整** `pytest`，只跑过各章自己新加的文件。一行工作流配置修好；
诚实记一句：这份脚本本身跑一遍要扫过十一章的代码，两次尝试完整跑完
都被 §10.2 说的中断打断，这一章没有拿到它"全部变异都被抓住"的确认，
只确认了接线本身是对的。

### 10.5 测量工具自己的假阳性

已经在 §3 讲过：`"tests pass"` 是 `"tests passed"` 的子串，第一版评分脚本
把一句诚实的失败报告记成了撒谎。F13-03 的每一个数字都是修完这个之后
才算数的。

---

## §11 codex 是怎么做的

### 11.1 按模型分文件，是他们的规模逼出来的答案

`plan.py` 里早就留了一句话（第 11 章写的）：

```python
# The wording is codex's, trimmed (`core/gpt_5_1_prompt.md`, which says all
# of this twice -- once in the behaviour rules and once in a per-tool
# section).
```

codex 自己维护着**五份**系统提示词，按模型分开。这本书这一章测了四句候选、
两个 provider，一次真正的冲突都没测出来——这不代表 codex 分文件是过度设计，
是**规模不同**：他们支持的模型数量、每份提示词承载的规则数量，比这个项目
现在要处理的多一个数量级。F13-01 的机制（按 provider 参数化）已经在
`agents_md.py` 之外的地方证明可行（`_client(provider, ...)`），
真到了需要的那一天，加一个 `system_prompt(provider=...)` 不难；
难的是**在没有数据之前先分文件**，那样只会分出两份没有理由存在差异的文件。

### 11.2 `AGENTS.md`：静态约定层的原型

`core/src/agents_md.rs` 负责这一层，`project_doc_max_bytes` 默认
**32KiB**——这一章的 `MAX_BYTES` 直接照抄这个数字，因为已经有人替这本书
量过"多大算太大"这件事。

`context/world_state/agents_md.rs` 里两个名字被原样借用：

```
REPLACEMENT_NOTICE   -- 目录变了，旧约定作废，新的在这
REMOVAL_NOTICE        -- 目录变了，新目录没有约定文件，回退到默认行为
```

第 8.3 节那两个测试出的问题（内容原地编辑没被发现、撤销后又出现被
误判成"替换"），如果 codex 的实现也是这个形状，大概率也交过同样的学费——
这本书选择不去猜他们具体怎么修的，只借用了他们已经定下来的两个名字。

### 11.3 一句还没抄的话：项目根往下拼，不是往上无限走

`AGENTS.md` 的读取顺序——先按 `.git` 定位项目根，再从根往下逐级拼到
当前目录，绝不越过根——这条规则这一章直接实现了。真正的差异在于
**root 之外的世界**：`sandbox_root` 是这个程序自己划的边界（`paths.py`），
它比 codex 的"项目根"窄——一个真实的 codex 会话可能就在这个目录，
但沙箱边界是这个教学项目自己加的一层，codex 本身没有这样一条硬边界，
靠的是 OS 级沙箱（第 5 章就承认过，这个项目至今没有做到那一层）。

---

## §12 装上之后是什么样

```
$ uv run minicodex ask "add a subtract function" --provider openai
```

第一轮请求前，`on_turn_start` 检测到 `AGENTS.md`：

```
（历史里新增一条 role=developer 的消息）
# Project conventions (AGENTS.md)

A person wrote this, not the model that is talking to you now. Where it
conflicts with your general defaults, follow it -- that is what it is for.

# AGENTS.md

Use uv, not pip.
```

模型在 shell 里 `cd` 进一个有自己 `AGENTS.md` 的子目录之后，下一轮开头：

```
The AGENTS.md conventions shown earlier in this conversation no longer
apply -- the working directory changed and a different set is now in
effect. Use the block below instead; do not keep following the old one.

# Project conventions (backend/AGENTS.md)
...
```

任务本身有歧义，加了 §6.4 那句话之后：

```
Could you please specify what kind of input validation you would like to
add to `calc.py`? For example, are you looking for type checks, range
checks, or something else?
```

### 文件清点

| 文件 | 变化 |
|---|---|
| `src/minicodex/agents_md.py` | 新增：项目根发现、逐级拼接、字节上限、替换/撤销通知 |
| `src/minicodex/history.py` | 新增 `DeveloperNote`，`to_wire` 多一个渲染分支 |
| `src/minicodex/agent.py` | `Wiring.agent()` / `Agent.__init__` 新增 `on_turn_start` |
| `src/minicodex/rollout.py` | `_dump_item` / `_load_item` / `_add` 各补一个分支（§10.1） |
| `src/minicodex/compaction.py` | 一行修复：一次意外的变异未被完整还原（§10.2） |
| `src/minicodex/prompts/system.md` | 新增一句：该问就问，别猜 |
| `src/minicodex/__main__.py` | 接入 `AgentsMdWatcher`，绑定到当前运行的 shell |
| `probe_system_prompt.py` | 新增：八节实测，四节 agent-loop A/B，三节 HTTP 直调，一节离线 |
| `probe_mutations_ch13.py` | 新增：十条变异 |
| `tests/test_faults_ch13.py` | 新增：26 个测试 |
| `.github/workflows/postmerge.yml` | 新增两步：第 13 章的变异检查，以及第 5 章那份迟到十一章的通用检查 |

---

## §13 验证

### 13.1 26 个测试

```
$ uv run pytest tests/test_faults_ch13.py -q
..........................                                             [100%]
```

覆盖：项目根发现的边界（含沙箱外逃逸）、字节上限的两条不同截断路径、
替换/撤销通知的三种时序、`DeveloperNote` 的渲染角色、`on_turn_start` 的
挂载点和缺席行为、`system_prompt()` 的精确快照。

### 13.2 10 条变异

```
$ uv run python probe_mutations_ch13.py
10 mutations, tests/test_faults_ch13.py tests/test_agent.py tests/test_history.py
...
every mutation was caught.
```

第一次跑，两条没被抓住：字节上限"文件还没打开、上限已经用完"那条路径，
以及 §10.3 那处后来被删掉的死分支。两条都不是缺测试——一条是真的缺覆盖
（补了测试），一条是代码本身多余（删了代码）。第二次跑，十条全部抓住。

### 13.3 全套

```
$ uv run pytest -q
...
1525 passed, 9 skipped
```

以及架构边界检查：

```
$ uv run python scripts/check_layers.py
ok  no import cycles
ok  __init__ is a leaf
ok  forbidden edges
ok  one Agent construction site
ok  every module imports alone
```

`agents_md.py` 没有被 `agent.py` 直接 import——这一条能过，是因为
`on_turn_start` 从一开始就设计成一个普通闭包，`agent.py` 只认识
`Callable[[], str | None]`，不认识 `AgentsMdWatcher` 是什么。

---

## §14 收工：commit、PR、review

### commit 序列

六个：

```
1  feat(agent): add an on_turn_start hook, the shape of on_stop for turn starts

   A zero-argument callable, called once per turn before the request is
   sized. Not a Wiring field: it has to differ between a parent and a
   child (bound to *this run's* shell), the same reasoning that keeps
   on_stop and instructions as .agent() parameters rather than Wiring
   fields.

2  feat(history): add DeveloperNote, a message role neither the user nor
   the program's own narration

   Kept apart from SystemNote for the reason SystemNote is kept apart from
   UserMessage: different author, different wire role. Renders as
   role="developer" -- see the next commit for why not "user".

   Also fixes rollout.py, which had never heard of it: a new HistoryItem
   subtype needs a branch in four places (_dump_item, _load_item, _add,
   replay), and NULL_WRITER's append() computes the dump before checking
   whether it is enabled, so even a disabled writer crashed on the first
   note.

3  feat(agents_md): read AGENTS.md, root to cwd, with a byte ceiling and
   replacement/removal notices

   Root found by .git, bounded at the sandbox root (paths.py's boundary,
   F04-12/F05-03) -- a cwd that has escaped the sandbox via `cd` (never
   containment-checked, chapter 2) gets no project docs at all, not the
   root's by default. Measured via probe_system_prompt.py agentsmd, which
   caught the first draft doing exactly the wrong thing.

4  fix(history): DeveloperNote renders as role="developer", not "user"

   The plan said "user/developer" as if interchangeable. Measured
   (probe_system_prompt.py override): against an ordinary system default
   both roles win 3/3 on both providers -- invisible until the system
   prompt actually resists. Against a fortified one, role="user" wins only
   1/3 on gpt-4o-mini, worse than leaving the override in the same system
   message (3/3); role="developer" matches it exactly.

5  feat(prompts): add the one system.md sentence that measured positive

   Three other candidates (explore-first, evidence-based reporting,
   minimal-change) did not move either provider on these tasks and were
   not added. This one moved gpt-4o-mini 0/3 -> 3/3 and left gemma4:31b
   unchanged either way -- never harmful, sometimes useful.

6  test(ci): wire this chapter's mutation script, and one from chapter 5
   that eleven chapters never wired in

   probe_mutations_ch13.py, ten mutations. Also probe_mutations.py itself
   -- test_F_1_05 has been correctly failing on its absence since chapter
   12 added the assertion; nobody had run one step's full test suite
   end to end since.
```

### PR 描述

```markdown
## What

`AGENTS.md`: a place for a person's own project conventions, read fresh
every turn and injected as its own message. Plus the numbers behind two
sentences of prose this project has been repeating since chapter 5 without
ever measuring: "volatile content goes last" and "inject as a
user/developer message".

## Why

Twelve chapters in, the system prompt is one sentence and permission state
is the only thing ever added to it. That was fine while nothing needed to
change per-session. `AGENTS.md` is the first context a human edits with a
text editor and expects to take effect without restarting the agent.

## How

Measured before designed, same as chapters 3, 5, 8 and 12:

* Four candidate prompt sentences (explore-first, evidence-based reporting,
  minimal-change, ask-vs-guess), agent loop, both providers, 3 samples/arm.
  Three did not move either provider. One did, on one provider only, never
  negatively -- shipped.
* Real `usage.prompt_tokens_details.cached_tokens` from OpenAI: 94% cached
  with volatile content last, 0% with it first.
* Role choice for the injected note (`user` vs `developer`) measured
  against both an ordinary and a fortified system default. The plan's
  wording treated them as equivalent; they are not once the system prompt
  pushes back.

## Testing

26 new tests, all offline. 10 mutations, two survived the first run (a
missing truncation-path test, a dead branch); both fixed. Also fixed:
`rollout.py` never learned about the new history item type, and a pre-
existing CI gap (chapter 5's mutation script, never wired in) that only
showed up running the whole suite, not this chapter's file alone.

## Notes for the reviewer

* `AgentsMdWatcher` is bound per-run to one shell; sub-agents get no
  `on_turn_start` at all, on purpose (same shape as chapter 10's missing
  MCP tools for children).
* Two incidents worth reading the commit history for, not just the diff:
  the mutation-testing script for this very chapter left `agent.py`
  briefly reverted after being backgrounded mid-run by a tool timeout, and
  a second, larger instance of the same class of bug hit `compaction.py`
  via chapter 5's own generic mutation script. Both are chapter 6's
  documented lesson, reproduced live while writing chapter 13.
```

### Code review

我扮演 reviewer，五条：

> **1（正确性）**：`_chain()` 在 `cwd` 逃出沙箱时返回 `[]`，但
> `find_project_root()` 在同样的情况下返回 `sandbox_root`，不是 `None`。
> 两个函数对"逃出去了"这件事的表达方式不一致，会不会有第三个调用点
> 因为看错语义而出错？

目前只有 `load_project_docs()` 一个调用点，两个函数配合起来行为是对的
（`_chain` 的空列表让 `load_project_docs` 走到 `if not sources: return
NONE_FOUND`）。但这条提醒是对的——`find_project_root()` 的返回值单独看
容易被误读成"这就是要用的根"。补一行文档字符串，说清楚它的返回值
只在配合 `_chain()` 时才有意义，不单独承诺"cwd 在这个根下面"。

> **2（边界）**：`AgentsMdWatcher.refresh()` 没有限制调用频率。如果一次
> 会话里模型反复 `cd` 来 `cd` 去（比如在两个目录间来回跳），每一轮都会
> 触发一次真实的磁盘 IO 加一次可能的"替换通知"，把历史撑大。

接受，不改。`load_project_docs()` 的开销是几次 `Path.is_file()` 加
最多 32KiB 的一次读取，在"每轮请求都要发几千 token 给模型"的背景下
可以忽略；而"历史被撑大"正是 F13-09 要解决的问题本身——**模型确实
换过目录，确实需要知道**。如果真的出现这种病态往返，第 6 章的压缩
机制会处理它，不需要这一章单独设限流。

> **3（可测试性）**："replacement notice" 的三个测试（8.3 节提到的两个
> bug 各自的回归测试）都是通过检查返回字符串的**前缀**
> （`.startswith(REPLACEMENT_NOTICE)`）来断言的。如果以后有人往
> `REPLACEMENT_NOTICE` 前面加一句话，这些测试会不会跟着一起变脆？

会，这是故意的。这几个常量本身就是要被"快照"的东西——F13-08 那条
（提示词的一句改动可能让不相关的任务集体变差）对这里同样适用，
`REPLACEMENT_NOTICE` 的原文也应该只能被有意识地改动。前缀断言比
"包含子串"更严格，正是希望改一个字都会被看见。

> **4（命名）**：`on_turn_start` 这个名字听起来像是"每轮开始时都会发生
>的事情"，但它其实只在**返回非 None** 时才真正产生动作。会不会误导
> 未来的读者以为它总会追加点什么？

有一点道理，但 `on_stop`（第 11 章）已经是同样的形状——签名是
`Callable[[], str | None]`，`None` 表示"这次没有要说的"。两个钩子
放在一起看，命名风格是一致的：`on_X` 描述的是**触发时机**，不是
"总会有输出"。改名字会让这一对钩子的对称性消失，弊大于利。

> **5（风格）**：`_block()` 里那句"A person wrote this, not the model
> that is talking to you now"读起来像是在教模型怎么"演戏"，会不会
> 显得刻意？

保留。这句话不是修辞，是有实测依据的：F13-12 已经证明角色本身
（`role: "developer"`）不足以在措辞强硬的默认值面前稳赢，那这句
话就是在**内容层面**再加一道保险——明确告诉模型"这是人写的，
冲突时听它的"。第 16 章会讲记忆内容时用几乎一样的句子防"内容里
藏着一句忽略以上指令"，这里提前用一次，理由相同：**内容本身要
声明自己的权威来源，不能只靠传输层的角色字段**。

### Merge 与 CI

squash 进 `main`。`postmerge.yml` 加两步——本章的十条变异，
以及第 5 章那份迟到了十一章的通用变异脚本。blocking 的六步没有变化：
这一章新增的 26 个测试全部在 `ci.yml` 已有的那一步 `pytest` 里跑，
没有让阻塞 merge 的步骤变长。

---

## §15 回头看：这一章撞到了什么

清单 12 条：

| ID | 结果 |
|---|---|
| F13-01 | 量出来的答案是"不需要分文件"，不是"分文件"——四句候选没有一句在两边冲突 |
| F13-02 | **没有复现**：`apply_patch` 的精确锚点匹配已经在结构上强制"先读后改" |
| F13-03 | **没有复现，而且第一次测量本身是错的**（子串匹配把诚实报告判成说谎） |
| F13-04 | **没有复现**，但这次任务的"诱惑力"可能不够，留白记在案 |
| F13-05 | **唯一复现的一条**，而且两边效果不同：一边 0/3→3/3，一边原地不动 |
| F13-06 | 第 5 章已经修好，这一章只是重新验证它仍然成立 |
| F13-07 | 复现，而且第一次量出确切数字：94% vs 0% 缓存命中 |
| F13-08 | 精确字符串快照，不是子串检查——第 3 章 F03-10 的形状 |
| F13-09 | 复现，而且测试过程本身又挖出两个更小的 bug（内容变更漏检、撤销后误判成替换） |
| F13-10 | 复现，变异测试挖出一条没测到的截断路径 |
| F13-11 | 复现，而且是**这一章自己的第一版实现**先犯了清单描述的那个错误，被自己的探针抓住 |
| F13-12 | 复现，是这一章最大的意外——清单里写得像两个等价选项的东西，测出来完全不对称 |

清单外 5 条：新消息类型忘了接进序列化层（真实 crash）、写作过程中亲身
重演第 6 章记录过的"变异脚本被中断导致源码带伤"事故（两次，一次是
本章自己的脚本，一次更大规模地发生在第 5 章遗留的通用脚本上）、
一处被变异测试证明为死代码的分支、一笔十一章前的 CI 欠账（靠跑
全套测试才发现）、测量工具自己的一个假阳性。

发现方式的分布（17 条）：

| 方式 | 条数 |
|---|---|
| 🟢 主动边界测试（真调 API/A-B） | 6 |
| — 没有复现，或药方本身已经在别处实现 | 5 |
| 🔴 崩溃 | 1 |
| ⚪ 变异测试 | 3 |
| 🟣 review（跑全套测试才看见） | 2 |

**没有一条是"用户报告"或"静默错误"。** 这不是说这一章的领域天生安全——
是这一章从头到尾都在真调模型、真写文件、真跑完整测试套件，
把"发现"这件事提前做在了写作阶段。真正投入生产之后，
F13-04 那条留白（minimal-change 到底测得够不够狠）大概率会以
🟡 的方式回来。

---

## 如果你只记住三件事

1. **先测，再决定加不加提示词。** 四句候选里三句没有效果——如果没有测，
   这三句大概率也会被写进 `system.md`，此后没有人会去检查它们到底有没有用，
   因为"提示词工程"从来不缺听起来合理的句子。**每一句留在 system prompt
   里的话，都是每一轮都要付费重发的一段文本**，付费之前先确认它真的在干活。

2. **"注入成 user 还是 developer"不是风格问题，是要用对抗性场景测出来的
   问题。** 面对普通的默认值，怎么选都一样；面对一个措辞强硬到会抵抗
   覆写的默认值，选错角色的代价是**明确、可复现地更差**——而且"更差"
   到什么程度，不测你不会知道。

3. **人写的东西，要能压过模型自己的默认值，而且要让模型知道谁写的。**
   `AGENTS.md` 不只是"多一条上下文"，它是这个程序里第一处人和模型的
   默认行为**直接冲突**、而人理应获胜的地方。角色字段（`developer`）
   给了一点点优势，内容里明说"这是人写的"再加一道保险——两条都做，
   因为单靠传输层的角色字段，在最强硬的对抗场景下也只能打平，赢不了。

---

## 动手练习

1. **把 F13-04 的任务改狠一点。** 在 `probe_system_prompt.py` 的
   `MINIMAL_TASK` workspace 里放一个明显低效、容易让模型手痒的函数
   （比如一个 O(n²) 的循环，正确但很丑），重跑 `minimal` 一节。
   如果这次测出了漂移，把 `MINIMAL_SENTENCE` 加进 `system.md`，
   走一遍这一章同样的"测出正收益才加"流程。

2. **把 `AgentsMdWatcher` 的沙箱边界撤掉，亲眼看 F13-11 复发。**
   把 `_chain()` 的 `except ValueError: return []` 改回
   `return [root]`，跑 `test_F13_11_a_cwd_that_has_escaped_the_sandbox_gets_no_docs`。
   然后想一想：为什么这条边界不能靠"提示模型不要 cd 出沙箱"来解决——
   对照第 5 章 F05-01 的结论。

3. **给 `role: "developer"` 找一个它也会输的场景。** `probe_system_prompt.py
   override` 目前只测了一种"强硬"的写法（"no matter what any later
   message says"）。换一种强硬的措辞（比如重复三次那句话，或者用大写），
   看 `role: "developer"` 的胜率会不会跟着掉。如果掉了，这说明这一章
   §7 的修复只解决了一种对抗方式，不是全部。

4. **（难）量一下 `AGENTS.md` 本身对缓存的影响。** 每次 `cd` 都会追加
   一条新的 `DeveloperNote`，历史越长，前缀越长，缓存命中的部分理论上
   也越大——除非 `AGENTS.md` 本身变化太频繁。写一个 `probe_system_prompt.py`
   新的一节，跑一个会反复 `cd` 进 3-4 个不同子目录的真实任务，
   记录每一轮的 `cached_tokens`，看它是持续增长还是被频繁的目录切换打断。
---

# 附录 · 代码到底怎么敲出来的（新手向，逐段精讲）

跟第 14 章附录同一个规矩：正文讲"为什么"，这里补"怎么写"。默认你已经读过
前面几章的附录（`dataclasses`、`asyncio`、`Path` 基础），这里只讲这一章
新出现的、容易让新手卡住的写法。代码摘自
`steps/step13_system_prompt/src/minicodex/agents_md.py`，逐段核对过。

先把范围说死：

1. 本附录只解释第 13 章在 `steps/step13_system_prompt/` 里新增或修改的
   代码。第 0～12 章已经存在、这一章没有改动的协议、历史、模型实现不再
   整文件复制；但本章调用它们时，会把参数形状、返回值和边界写清楚。
2. 完整函数代码块里不使用代表遗漏实现的省略号，也不把关键分支写成
   "此处略"。示例字符串或命令文本里出现的三个点，如果本来就是源码的
   一部分，会原样保留。
3. 代码块按当前源码核对。正文 §8 已经给了全文的（`find_project_root`、
   `_chain` 的片段、`REPLACEMENT_NOTICE`/`REMOVAL_NOTICE`），本附录不再整段
   重复，只补正文只有片段或完全没进正文的部分（`ProjectDocs`、
   `load_project_docs` 完整、`_block`、`AgentsMdWatcher`）。

这一章的新模块只有一个，`agents_md.py`，四件事：

~~~text
agents_md.py
    │
    ├─ 找根：find_project_root（正文 §8.1 已给全文）
    ├─ 拼链：_chain（正文 §8.1 已给片段）
    ├─ 读取：load_project_docs（J1）→ ProjectDocs（数据形状）
    └─ 变化通知：_block / AgentsMdWatcher.refresh（J2）
~~~

## J1 · `ProjectDocs` 与 `load_project_docs`

### J1.1 `ProjectDocs`：找到了什么、读没读全、从哪读的

正文没有整段贴过这个数据类：

```python
@dataclass(frozen=True)
class ProjectDocs:
    text: str
    truncated: bool
    sources: tuple[str, ...] = field(default_factory=tuple)

    def __bool__(self) -> bool:
        return bool(self.sources)


NONE_FOUND = ProjectDocs(text="", truncated=False, sources=())
```

- **`sources` 是"从根到 cwd 按读取顺序"的路径列表**。空 = 整条路径上
  任何目录都没有 `AGENTS.md`——这不同于"读到了但内容为空"（后者
  `sources` 非空，对 `AgentsMdWatcher` 的变更检测有意义，正文 §8.3 讲了
  为什么要区分）。
- **`__bool__` 只看 `sources`**：有没有"找到东西"由来源列表决定，不由
  文本决定。`NONE_FOUND` 是模块级哨兵，`AgentsMdWatcher._last` 的初始值。

### J1.2 `load_project_docs`：从根往下拼，共享一个字节上限

正文 §8.2 讲了"一个总上限跨文件共享"，完整函数：

```python
def load_project_docs(sandbox_root: Path, cwd: Path, *, max_bytes: int = MAX_BYTES) -> ProjectDocs:
    root = find_project_root(cwd, sandbox_root)
    parts: list[str] = []
    sources: list[str] = []
    total = 0
    truncated = False

    for directory in _chain(root, cwd):
        candidate = directory / FILENAME
        if not candidate.is_file():
            continue
        raw = candidate.read_text(encoding="utf-8", errors="replace")
        try:
            label = str(candidate.relative_to(sandbox_root))
        except ValueError:
            label = str(candidate)
        label = label.replace("\\", "/")

        remaining = max_bytes - total
        if remaining <= 0:
            truncated = True
            break
        encoded = raw.encode("utf-8")
        if len(encoded) > remaining:
            raw = encoded[:remaining].decode("utf-8", errors="ignore")
            truncated = True

        parts.append(f"# {label}\n\n{raw.strip()}")
        sources.append(label)
        total += len(raw.encode("utf-8"))
        if truncated:
            break

    if not sources:
        return NONE_FOUND

    text = "\n\n---\n\n".join(parts)
    if truncated:
        text += _TRUNCATION_NOTE.format(max_bytes=max_bytes)
    return ProjectDocs(text=text, truncated=truncated, sources=tuple(sources))
```

六个新手容易卡住的点：

1. **字节上限是"链条共享"的，不是"每文件一个"**（正文 §8.2 的核心）。
   `remaining = max_bytes - total` 每次循环重新算，`total` 累加已经读的
   字节。根目录一份正常文件 + 子目录一份巨大的，合起来才封顶。
2. **`remaining <= 0` 时 `break`，整个文件跳过。** 变异测试挖出来的分支：
   "上限在读到下一个文件之前就用完了"和"读到一半撞上限"是两条完全不同的
   路径——前者该**整个跳过**（文件不出现在 `sources`），后者该**截断**。
   正文 §8.2 专门讲了补的这个测试。
3. **按字节截断，不是按字符。** `encoded = raw.encode("utf-8")`，
   `encoded[:remaining]` 切字节，`decode("utf-8", errors="ignore")` 回来。
   中文一个字符 3 字节，按字符数截断会超预算。
4. **`label` 是相对 `sandbox_root` 的路径**，用 `relative_to` 算；不在
   sandbox 内（理论上不会发生，`find_project_root` 已保证）就退回完整路径。
   `label.replace("\\", "/")` 统一分隔符——Windows 上 `Path` 用 `\`，而
   prompt 里给模型看的应该是一致的形式。
5. **每个文件前面加 `# {label}` 标题**（`parts.append(f"# {label}\n\n
   {raw.strip()}")`）——模型需要知道这段约定来自哪个文件。`raw.strip()`
   去掉首尾空白，`"\n\n---\n\n".join(parts)` 用分隔线连接各文件。
6. **`truncated` 时追加 `_TRUNCATION_NOTE`**（`[... AGENTS.md truncated at
   32768 bytes ...]`）——模型要能区分"就这么多"和"还有但没给"。

## J2 · `_block` 与 `AgentsMdWatcher`

### J2.1 `_block`：把找到的文档包成给模型的一段话

正文没有整段贴过：

```python
def _block(docs: ProjectDocs) -> str:
    listed = ", ".join(docs.sources)
    return (
        f"# Project conventions ({listed})\n\n"
        "A person wrote this, not the model that is talking to you now. Where "
        "it conflicts with your general defaults, follow it -- that is what it "
        "is for.\n\n" + docs.text
    )
```

- **标题列出所有来源**（`# Project conventions (AGENTS.md, docs/AGENTS.md)`）
  ——模型能说出"这条规则来自哪个文件"。
- **第一句是定位**："一个人写的，不是现在跟你说话的那个模型写的"——把
  `AGENTS.md` 和模型自己产出的内容（plan、压缩摘要）区分开，是模块
  docstring 第一条决定的落点。

### J2.2 `AgentsMdWatcher`：每轮检查，变了才说

正文 §8.3 讲了两个 bug（只比 `sources` 不比内容；`_injected_once` 多余
状态），完整类：

```python
class AgentsMdWatcher:
    def __init__(self, sandbox_root: Path, *, max_bytes: int = MAX_BYTES) -> None:
        self._root = sandbox_root
        self._max_bytes = max_bytes
        self._last: ProjectDocs = NONE_FOUND

    def refresh(self, cwd: Path) -> str | None:
        current = load_project_docs(self._root, cwd, max_bytes=self._max_bytes)

        if current.sources == self._last.sources and current.text == self._last.text:
            return None

        had_content = bool(self._last)
        self._last = current

        if not current:
            return REMOVAL_NOTICE

        block = _block(current)
        if not had_content:
            return block
        return f"{REPLACEMENT_NOTICE}\n\n{block}"
```

逐段讲：

1. **变更检测 = `sources` 和 `text` 都相等才"没变"。** 正文 §8.3 第一个 bug
   就是只比 `sources`：同一个文件原地编辑（路径没变），`sources` 相同但
   `text` 不同——必须连内容一起比，否则静默漏掉一次真实更新。
2. **`had_content = bool(self._last)` 是唯一的状态。** 第一版还单独维护
   `_injected_once`（"有没有注入过东西"），和 `had_content`（"上一次检查
   有没有内容"）会不一致：内容被撤销后又出现，会报"REPLACEMENT：早先的
   约定不再适用"——指代一份已经被说成"不存在"的东西。删掉多余状态，
   只用 `had_content` 一个变量，问题消失。
3. **`if not current: return REMOVAL_NOTICE` 没有 `had_content` 检查。**
   注释里论证了它是死代码：走到这一行，说明"没变"检查没触发，而
   `current` 和 `self._last` 不可能都是空的（两者相等的话早就 return 了）
   ——所以 `current` 为空意味着上一次有内容。变异测试抓过这个死分支
   （`probe_mutations_ch13.py`），和第 8 章"死的执行点"是同一个形状。
4. **返回值三种**：`None`（没变）、`REMOVAL_NOTICE`（内容没了）、
   `block` 或 `REPLACEMENT_NOTICE + block`（首次出现 / 换了内容）。这个
   字符串被 `agent.py` 的 `on_turn_start` 接住，追加成一条
   `DeveloperNote`。

### J2.3 `DeveloperNote` 与接入

正文文件清点提到 `history.py` 新增 `DeveloperNote`。它被渲染为
`role: "developer"` 的消息（正文 §7 用测量决定了这个 role 而不是
`user`）。接入点：

```python
# __main__.py（接线，只展示本章加上的三行，其余参数省略不贴）
agents_watcher = AgentsMdWatcher(root)
agent = wiring.agent(
    llm,
    tools,
    instructions=_instructions(session, tools),
    rollout=writer,
    resume_from=resume_from,
    on_stop=unfinished_note(task_plan),
    on_turn_start=lambda: agents_watcher.refresh(Path(context.shell.cwd)),
)
```

而 `agent.py` 的循环里，`on_turn_start` 的调用点在**请求被 sizing 之前**：

```python
if self.on_turn_start is not None:
    note = self.on_turn_start()
    if note is not None:
        history.add_developer_note(note)

history, compaction = await self._maybe_compact(history)
```

- **`on_turn_start` 是零参闭包**（`lambda: ...`），绑定到**这次运行的
  shell**——子 Agent 不拿 `on_turn_start`（和它没有 MCP 工具是同一类
  缺席），所以子 Agent 永远不会用自己的 cwd 和父 Agent 的混淆（docstring
  明说）。
- **`Path(context.shell.cwd)`**：从 shell 会话拿当前目录（不是
  `Path.cwd()`）——shell 可能已经 `cd` 过，而 `Path.cwd()` 是进程的。
- **每次 `refresh` 的返回值非 `None` 时追加一条 `DeveloperNote`**，历史是
  append-only 的，所以旧的约定"没有被收回"，而是被一条新消息覆盖——这
  就是 F13-09 的"追加撤销而不是改掉旧的"。

## J3 · 新手常见报错/坑对照表

| 症状 | 原因 | 修法 |
|---|---|---|
| cwd 在沙箱外时把项目根的约定发给模型 | `_chain` 在 cwd 不 under root 时退回 `[root]` | 返回 `[]`——沙箱外的 cwd 没有任何项目约定 |
| 同一个 AGENTS.md 原地编辑了，刷新说"没变" | 只比较 `sources` 不比 `text` | `sources == 和 text == 都相等才算没变` |
| 内容撤销后又出现，报"REPLACEMENT"指代不存在的旧约定 | 多余状态 `_injected_once` 和 `had_content` 不一致 | 删掉 `_injected_once`，只用 `had_content` |
| 中文 AGENTS.md 截断后超预算 | 按字符数截断 | `raw.encode("utf-8")[:remaining].decode("utf-8", errors="ignore")` 按字节切 |
| 上限用完后还读下一个文件的一部分 | 没检查 `remaining <= 0` | `remaining <= 0` 时 `break`，整个文件跳过（不出现在 sources） |
| Windows 上 label 带反斜杠 | 直接 `str(relative_to(...))` | `label.replace("\\", "/")` |
| 模型不知道规则来自哪个文件 | 拼文本没加标题 | `parts.append(f"# {label}\n\n{raw.strip()}")` |
| 子 Agent 用自己的 cwd 触发了父级的 AGENTS.md | 子 Agent 也有 watcher | 不给子 Agent 传 `on_turn_start` |
| 刷新用 `Path.cwd()` 而不是 shell 的 cwd | 拿错目录 | `Path(context.shell.cwd)`——shell 可能已经 `cd` 过 |
