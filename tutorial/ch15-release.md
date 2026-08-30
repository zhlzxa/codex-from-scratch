# 第 15 章 · 发布与维护：交出去之后，它就不再只属于你

> **代码**：`steps/step15_release/`
> **分支**：`feat/release`
> **产出**：`release.py`（磁盘契约表、废弃日历、bug 报告块）、`PACKAGE.md`、
> `CHANGELOG.md`、`CONTRIBUTING.md`、`SECURITY.md`、issue 模板、
> `release.yml`、CI 加一条 Python 矩阵、`tests/fixtures/compat/`（**十四个真实旧版本写的文件**）
> **你需要**：本章 54 个测试全部离线（其中三个会 `uv build` 一个 wheel）。
> `probe_release.py compat` 需要同目录下其他 step 的 `.venv`，跑一遍约四分钟，不花钱
>
> **阅读顺序上这是最后一章。** 第 16、17 章编号在后、阅读在前，理由见第 16 章 §1.3。

---

## §1 这一章要做出来的东西

### 1.1 先看见它动

前十七章，每一次"跑起来"都是 `uv run minicodex ...` ——在项目目录里，
用 `uv sync` 两秒钟前刚装好的那棵树。这一章第一件事是换个身份：

```
$ python -m venv /tmp/cv2
$ /tmp/cv2/bin/pip install minicodex-0.1.0-py3-none-any.whl
$ cd /tmp/demo && ls
src/

$ minicodex ask "What does src/minicodex/__init__.py define?"

- **`__version__`**: The current version of the package (`0.0.1`).
- **`system_prompt()`**: A function that reads and returns the agent's system
  prompt from a file located at `src/minicodex/prompts/system.md`.
- **`__all__`**: An export list containing `__version__` and `system_prompt`.

[gemma4:31b-cloud | completed after 2 turn(s)]
[transcript: .minicodex\recordings\session-1786890174.jsonl]
[session: .minicodex\sessions\20260816T222254-34636.jsonl]
```

一个空目录、一个装好的 wheel、一次真实运行。然后：

```
$ minicodex --version
minicodex 0.1.0
python    3.13.5 (win32, CPython)
install   C:\Users\qpdyl\AppData\Local\Temp\cv2\Lib\site-packages\minicodex
cwd       C:\Users\qpdyl\AppData\Local\Temp\demo
state     .minicodex: 1 session(s), 1 recording(s), memory none, approvals none
latest    C:\...\demo\.minicodex\recordings\session-1786890174.jsonl
          reproduce it with: minicodex replay C:\...\session-1786890174.jsonl
report    https://github.com/example/minicodex/issues

$ minicodex replay .minicodex/recordings/session-1786890174.jsonl
  2 turn(s), 2 attempt(s) (0 failed), 1 tool call(s)
  recorded against ollama/gemma4:31b-cloud
  turn 0: 2 message(s) match
  turn 1: 4 message(s) match

ok  completed after 2 turn(s), same as recorded
```

**这一屏就是这一章的全部论点。** 一个陌生人装上它、跑一次、`--version`
告诉他哪个文件能复现这次运行，而那个文件真的能——不联网、不要 key、
在**任何**版本上。第 14 章造出了这个能力，但没有人知道该附上哪个文件；
这一章把最后一米接上。

### 1.2 这一章要回答的那个问题

前面每一章问的都是"它做得对不对"。这一章问的是另一个：

> **交出去的那个文件，和我测的这棵树，是同一个东西吗？**

答案是不是。而且分歧不止一处：

| 我测的 | 用户拿到的 |
|---|---|
| `uv sync` 装好的可编辑安装 | 一个 wheel，里面缺什么没人看过 |
| 调函数，看返回值 | 看终端，看退出码 |
| 一个版本，自己写自己读 | 上个月那个版本写的文件，这个月这个版本读 |
| 3.13（我的笔记本） | `requires-python` 说的 `>=3.10` |

四行里有三行当场出了问题，**全是线上代码**。

### 1.3 为什么"发布"必须排在最后

不是因为它不重要，是因为**在有用户之前，这一章的每一条都成立不了**。
第 16、17 章的记忆机制也是同一个理由排在这里：
一个只被用过一次的工具不需要记忆，也不需要废弃期。

而这一章的独特之处在于，它是全书唯一一个**测量对象不是程序、是产物**的单元。
所以它用的工具也不一样：不是 pytest，是 `pip install`；
不是断言，是**读十四个旧版本留在磁盘上的文件**。

---

## §2 F15-01：十四个版本，一个版本号

### 2.1 先量

清单写的是：

> F15-01 破坏性变更发了 patch 版本，用户升级后崩了

这句话预设了一个前提：**有版本号可发**。先去看看这个项目的版本号长什么样。

这本书的交付形态在这里意外地帮了大忙：`steps/` 下每一个目录都是一个
**完整的、装好的、能跑的** minicodex。从第 5 章起每一个都有 `.venv`。
那不是十四个示例，那是**十四个历史版本，附带可运行的解释器**。

```
$ for d in step05_approval … step17_memory_write; do
    (cd $d && ./.venv/bin/python -m minicodex --version | head -1)
  done

minicodex 0.0.1
minicodex 0.0.1
minicodex 0.0.1
minicodex 0.0.1
… （十四行，全都一样）
```

**十四个版本，一个版本号。** 而这个范围里发生过什么，第 14 章的正文自己写着：
录制格式变了，旧录制回放不了。

也就是说：这十四个版本之间存在至少一次破坏性变更，
而任何人拿着 `minicodex 0.0.1` 这句话，**无法知道自己在哪一边**。

清单说的"破坏性变更发了 patch 版本"，实际形态比它更糟：
**根本没有版本可发。** 语义化版本的第一个前提不是"怎么编号"，是"编号会变"。

### 2.2 一个 Agent 的公开接口是什么

写到这里就不得不回答一个问题：**语义化版本管的是"公开接口",
那这个程序的公开接口是什么？**

标准答案是 Python API。而这个程序几乎没有人 `import` 它——它是一个 CLI。
于是第二个答案是命令行参数。这个对，但不完整。

真正会把用户搞崩的东西，是这五个：

```
.minicodex/
├── sessions/            第 7 章    --resume 读它
├── recordings/          第 -1 章   minicodex replay 读它
├── memories/            第 16 章   每一次请求都注入它
├── rules.json           第 5 章    每一条命令都查它
└── memory_jobs.sqlite3  第 17 章   下次启动读它
```

**这五个是用户升级之后还留在他磁盘上的东西。**
它们是这个程序做过的最长期的承诺，而在前十七章里，
没有任何一行代码、任何一个测试、任何一句文档提到过它们是承诺。

它们也是唯一**不受 0.x 豁免保护**的东西。语义化版本说 0.x 阶段公开 API
可以随便改——那是对**代码**说的。用户的 `.minicodex/` 目录不读版本号。

这份清单在 `release.py` 里写成了 `SURFACES`：

```python
SURFACES: tuple[Surface, ...] = (
    Surface("sessions", SESSIONS_DIR, "type_version (chapter 7)", "refuse", "per record"),
    Surface("recordings", RECORDINGS_DIR, "none", "refuse", "names the missing field"),
    Surface(
        "memory",
        DEFAULT_MEMORY_DIR,
        f"`v1` on line 1 of {SUMMARY_FILE} and {BODY_FILE}",
        "rewrite",
        "a wrong line-1 marker means the whole file is regenerated, not patched",
    ),
    Surface(
        "approvals",
        DEFAULT_RULES_PATH,
        "none",
        "refuse",
        "a bad file yields the empty set of rules, never a partial one",
    ),
    Surface("memory jobs", DEFAULT_JOBS_PATH, "sqlite user_version", "recreate", "it is a cache"),
)
```

**注意这不是一个抽象。** 没有 `SurfaceProtocol`，没有任何代码对它做分发，
没有一条执行路径是泛型的。它是**一张写在一个地方而不是五个地方的事实表**。
第 17 章那条尺子在这里第四次用上：

> **"这个概念出现了三次"和"这段代码出现了三次"是两件事。**
> 三次法则管的是代码；这是数据。

### 2.3 交叉版本矩阵：让旧版本自己写，让新版本去读

有了这份清单，问题就变得可以执行了：**十四个旧版本各写一份，今天的代码去读。**

麻烦在于让十四个旧版本都跑一次真实对话——那要 API key，要网络，要钱，
而且每次结果不同。第 0 章那个 `serve-stub` 正好解决这个：
它回放的是 2026-08-06 真实录下来的 Ollama 字节，
所有版本对着它跑，**收到的是一模一样的对话**。

```python
STUB_PORT = 11477
QUESTION = "What does src/minicodex/__init__.py define?"
```

每个版本都在**它自己的目录里**跑（stub 要求读 `src/minicodex/__init__.py`，
每个 step 都有这个文件），产物拷出来，交给当前代码：

```
$ uv run python probe_release.py compat --corpus

  stepA_refactor             session: -                  recording: REFUSED: tool call 'read_file' …
  step05_approval            session: -                  recording: REFUSED: tool call 'read_file' …
  step06_compaction          session: -                  recording: REFUSED: tool call 'read_file' …
  step07_resume              session: ok, 5 item(s)      recording: REFUSED: tool call 'read_file' …
  step08_concurrency         session: ok, 5 item(s)      recording: REFUSED: tool call 'read_file' …
  step09_mcp                 session: ok, 5 item(s)      recording: REFUSED: tool call 'read_file' …
  step10_subagents           session: ok, 5 item(s)      recording: REFUSED: tool call 'read_file' …
  stepB_boundaries           session: ok, 5 item(s)      recording: REFUSED: tool call 'read_file' …
  step11_plan                session: ok, 5 item(s)      recording: REFUSED: tool call 'read_file' …
  step12_retry               session: ok, 5 item(s)      recording: REFUSED: tool call 'read_file' …
  step13_system_prompt       session: ok, 5 item(s)      recording: REFUSED: tool call 'read_file' …
  step14_eval                session: ok, 5 item(s)      recording: ok, 2 turn(s)
  step16_memory_read         session: ok, 5 item(s)      recording: ok, 2 turn(s)
  step17_memory_write        session: ok, 5 item(s)      recording: ok, 2 turn(s)

  14 version(s) read by this one.
  corpus: 25 file(s) under tests/fixtures/compat/
```

两列，两个完全相反的结论：

**会话文件 11/11 全部可读。** 第 7 章那条"加字段不算破坏性变更"的论证是对的——
`SessionMeta` 后来加了 `forked_from`、又加了 `parent`，都没有 bump
`type_version`，而 `from_json` 丢弃不认识的键、缺失的键取默认值。
这条论证**写在第 7 章的注释里，十章都没有被执行过一次**。现在它是一个测试。

**录制文件 14 个里 11 个读不了。** 而且这 11 个不是"很久以前"的版本——
`stepB_boundaries`、`step13_system_prompt` 是紧挨着第 14 章的前两个版本。

### 2.4 改格式的那次提交，没有碰写格式的那个文件

这是本章最值得记住的一条，而且它是从上面那张表倒推出来的。

录制格式变过两次（第 12 章加了 `attempt`，第 14 章加了工具参数和失败证据）。
按常理，改格式应该改写格式的那个模块。去查：

```
$ for d in step00_minimal_loop … step15_release; do
    md5sum $d/src/minicodex/recorder.py | cut -c1-8
  done

60014dd3   ← step00_minimal_loop
60014dd3   ← step01_protocol
…
60014dd3   ← step14_eval
60014dd3   ← step17_memory_write
60014dd3   ← step15_release
```

**二十个版本，`recorder.py` 一个字节都没变。**

因为 `Recorder.record(kind, payload)` 只负责把一个 dict 写成一行 JSON；
**payload 是在 `agent.py` 的调用点拼出来的**。格式的定义在调用点，
而调用点有两个，分散在一个 800 行的循环里。

于是：

> **"哪些文件改了"这个问题，回答不了"格式变了没有"。**
> 只有拿旧文件去喂新代码才能回答。

这条对任何有磁盘格式的项目都成立，而且越是"格式模块写得很干净"的项目越危险——
干净的格式模块给人一种"改它我会注意到"的错觉，而真正的格式是调用点的形状。

### 2.5 第一次跑这个探针，它崩了

```
Traceback (most recent call last):
  File "probe_release.py", line 143, in _read_with_current
    recording = load(out / record["recording"])
  File "src\minicodex\replay.py", line 205, in load
    key = (payload["turn"], payload["attempt"])
                            ~~~~~~~^^^^^^^^^^^
KeyError: 'attempt'
```

第 14 章的 `load()` 有一段写得非常仔细的拒绝逻辑，正文里还专门讲过：

```python
if "arguments" not in call:
    raise ReplayError(
        f"{path}: tool call {call.get('name')!r} was recorded without its "
        "arguments, so this run cannot be replayed. Recordings made before "
        "chapter 14 stored only the id and the name."
    )
```

它对**手头那一种旧文件**给出了一句人话。而对再旧一点的文件，
它给出一个裸的 `KeyError`。

数一下：`attempt` 是第 12 章加的，所以 14 份录制里有 **9 份**是这个下场
（step05、06、07、08、09、10、11 和两个插曲）；
2 份（step12、step13）能走到那句人话；3 份能回放。

> **针对"手头那个旧文件"写的向后兼容，是只对那个文件的向后兼容。**

这条错误不是粗心。写第 14 章的时候，能拿到的旧录制**只有一种**，
因为那时候我只有一个"旧版本"可以拿。今天能发现，
纯粹是因为这本书的交付形态碰巧留下了十四个。
真实项目里对应的东西是**用户的文件**——而你拿不到，除非他们寄给你。
这就是 §4 那个 issue 模板存在的全部理由。

### 2.6 拒绝，还是迁移

修法不是"再加一个 `.get()`"，而是先把两种情况分开，因为它们**不该有同样的处置**：

```python
elif kind == "request":
    # `attempt` arrived with chapter 12's retry loop.  A recording made
    # before it has no such key, and the *meaning* of its absence is
    # not in doubt: there was one attempt per turn and this is it.
    # So it is migrated to 0 rather than refused -- which is the whole
    # distinction chapter 15 makes about old files.  Refuse when the
    # information is gone (a tool call recorded without its arguments,
    # below); migrate when it was merely never written down.
    key = (payload["turn"], payload.get("attempt", 0))
```

两条规则，一句话就能说清：

| 情况 | 例子 | 处置 | 为什么 |
|---|---|---|---|
| 信息**从来没被写下来**，但含义没有疑问 | 没有 `attempt` ⇒ 只有一次尝试 | **迁移** | 补出来的值和真值相同 |
| 信息**丢了** | 没有工具参数 | **拒绝**，并说清缺哪个字段 | 补一个 `{}` 会回放出"模型要求 patch 空内容"——一次**从未发生过的对话的绿色测试** |

第二行是关键。一个能"宽容处理"任何旧文件的读取器，
会把"读不了"变成"读出别的东西"，而后者永远更贵。
第 6 章 F06-08 那句话在这里第二次出现：**一个空摘要不是很短的摘要，是被静默销毁的记录。**

`SURFACES` 里那个 `policy` 字段就是这两个词。

### 2.7 把语料冻进版本库

探针要十四个装好的环境和四分钟。CI 里不能这么干。
所以 `--corpus` 把结果**拷进 `tests/fixtures/compat/`**，192KB，进版本库：

```
tests/fixtures/compat/
├── step05_approval/recording.jsonl
├── step07_resume/{recording.jsonl,session.jsonl}
├── …
└── step17_memory_write/{recording.jsonl,session.jsonl}
```

然后是两个参数化测试：

```python
@pytest.mark.parametrize("past", PAST_VERSIONS)
def test_F15_01_a_recording_from_any_past_version_replays_or_refuses_in_words(past: str) -> None:
    """Never a traceback.  Either it replays or it says which field is missing."""
```

**这是第 14 章那条论证换个尺度的重演**：一份从真实运行里抓下来的产物，
胜过任何人手写的 fixture。区别只在于第 14 章抓的是"一次真实对话"，
这里抓的是"一个真实旧版本的输出"——而后者是**只有那个旧版本知道的东西**。
今天手写一份"第 5 章格式的录制"，写出来的是我今天对第 5 章的记忆，不是第 5 章。

### 2.8 发 0.1.0，不发 1.0.0

到了这一步就该定版本号了。三个选项：

| | 说什么 | 代价 |
|---|---|---|
| 继续 `0.0.1` | 什么都不说 | 已经量出来了：十四个版本无法区分 |
| `1.0.0` | 从此每次破坏性变更都要 major | 这个程序的 CLI 还在动，一年发五个 major 等于没有语义 |
| **`0.1.0`** | 0.x：公开 API 随时可能变 | 诚实，但**只覆盖了代码** |

选 0.1.0，并且把它的漏洞明写出来。`__init__.py` 里：

```python
# 0.1.0 and not 1.0.0, deliberately.  Semantic versioning's 0.x escape hatch
# says the public API may change at any time -- which is honest about the
# Python API and says nothing at all about the files on disk, because a user's
# `.minicodex/` directory does not read the version number.  Chapter 15
# measures what that distinction costs.
__version__ = "0.1.0"
```

于是 `CHANGELOG.md` 里有两种破坏性变更，分开列：

```markdown
* **Breaking (API)** — code that imports `minicodex` stops working.
* **Breaking (files)** — a `.minicodex/` directory written by an older version
  stops being readable. This one is not covered by the 0.x escape hatch: a
  user's files do not read the version number.
```

**0.x 不是"还没发布"的意思，是"我保留改代码的权利"。
它从来没有保留改用户文件的权利。**

### 2.9 一个版本号，一处

顺手发现的：版本号写在两个地方。

```toml
# pyproject.toml
version = "0.0.1"
```
```python
# src/minicodex/__init__.py
__version__ = "0.0.1"
```

十七章，1656 个测试，没有一个比较过它们。修法是让 pyproject 去读模块：

```toml
dynamic = ["version"]

[tool.hatch.version]
path = "src/minicodex/__init__.py"
```

反过来（模块读 `importlib.metadata.version("minicodex")`）也是一个来源，
但被否决了：那样 `__version__` 在**没装过的树里不存在**，
而那正是一个人 `git clone` 之后、做任何别的事之前所在的树。

这是"两把尺子"这条教训在本书里第**四**次出现
（第 6 章的 token 估算、第 12 章的超时预算、第 17 章的摘要上限、这里），
而这一次它的形状最难看：**分歧出现在每一份 bug 报告的第一行**。

---

## §3 F15-02：盒子里到底有什么

### 3.1 少了什么（答案是没少，而这不是重点）

```
$ uv run python probe_release.py wheel

  built minicodex-0.1.0-py3-none-any.whl: 43 entries
  data files the code reads at runtime: 3, missing from the wheel: 0
```

清单说的"发布后才发现漏打包了数据文件"**没有复现**。三个 prompt 文件都在。

但第 -1 章那个测试是这么写的：

```python
assert "minicodex/prompts/system.md" in names, (...)
```

**一个字符串字面量。** 它钉住的是写它那天存在的那一个文件。
后来第 5 章加了 `permissions.md`、第 6 章加了 `compaction.md`，
两个都没有任何东西检查过是否进了 wheel。它们碰巧在。

修法是把字面量换成一个**从源码树算出来的列表**：

```python
def _data_files(repo_root: Path) -> list[str]:
    package = repo_root / "src" / "minicodex"
    return sorted(
        str(p.relative_to(package.parent)).replace("\\", "/")
        for p in package.rglob("*")
        if p.is_file() and p.suffix not in (".py", ".pyc") and "__pycache__" not in p.parts
    )
```

> **一个从源码树算出来的清单，不会落后于源码树；一个字面量会，而且是静默地落后。**

这条和第 3 章的 description 快照、插曲 B 的边界检查是同一件事的三个尺寸。

### 3.2 真正缺的三样，都只有从 wheel 里才看得见

```
  License-Expression   ABSENT
  Project-URL          ABSENT
```

（这是修之前的状态。）加上第三样——`readme = "README.md"`，
而那个 README 的第一行是：

```
# minicodex — chapter 17: memory, part two (writing it, and forgetting it)
```

三样东西，三个不同的受害者：

1. **没有 license。** 一个没有许可证的包，公司法务会直接让你卸载。
   这不是"不规范"，这是**不可用**。
2. **没有 `Project-URL`。** 一个只拿到 wheel 的用户，
   **在产物内部找不到任何联系方式**。§4 讲 bug 报告写得多好都没用——
   一封寄不出去的信不是措辞问题。
3. **首页写给了错的读者。** PyPI 上的项目页面就是这个长描述。
   "第 17 章：记忆（二）"对本书读者是对的第一句话，
   对一个刚 `pip install` 完、想知道自己拿到了什么的人是错的。

修法分别是 `license = "MIT"` + `LICENSE` 文件、`urls = {...}`、
以及新写一个 `PACKAGE.md`：安装、第一条命令、
**东西放在哪**（`.minicodex/` 那张表）、怎么报 bug、兼容性承诺。

第三条的测试不钉文件名，钉内容：

```python
    body = metadata.split("\n\n", 1)[1]
    assert "pip install minicodex" in body
    assert ISSUES_URL in body
    assert ".minicodex/" in body, "the front page must say where it puts things"
```

因为**把文件名改回去正是这个测试要拦的那个改动**。

### 3.3 `requires-python` 是一句承诺

`pyproject.toml` 从第 -1 章起就写着：

```toml
requires-python = ">=3.10"
```

CI 从第 -1 章起跑的是 `ubuntu-latest` 上 uv 挑的那个解释器。
也就是说，这句承诺**十七章从来没有被执行过**。

第一次执行它：

```
$ UV_PROJECT_ENVIRONMENT=.venv310 uv run --python 3.10 --all-extras pytest

FAILED tests/test_faults_ch10.py::test_F10_07_a_hanging_child_is_stopped_and_says_so
1 failed, 1655 passed, 9 skipped in 88.80s
```

### 3.4 `asyncio.TimeoutError` 和 `TimeoutError` 不是一个东西

```
$ python3.10 -c "import asyncio; print(asyncio.TimeoutError is TimeoutError)"
False
$ python3.13 -c "import asyncio; print(asyncio.TimeoutError is TimeoutError)"
True
```

Python 3.11 把 `asyncio.TimeoutError` 变成了内置 `TimeoutError` 的别名。
3.10 上它是一个**和内置类毫无继承关系**的独立类。

而 `subagent.py` 里写的是：

```python
    result = await asyncio.wait_for(asyncio.shield(task), ctx.timeout)
except TimeoutError:
    partial = await _stop(task)
```

**在 3.10 上这个 except 永远不会触发。** 后果不是一个红测试，是：
一个挂住的子 Agent 不会被停下来，`_stop(task)` 不会被调用，
异常从 `run_task` 里逃出去，而**子进程继续跑着**——
第 10 章 F10-07 的全部防御在这个平台上是关的，
顺带把第 2 章 F02-08（孤儿进程）也带回来了。

最难看的部分：`mcp.py` 从第 9 章起就写对了。

```python
except (TimeoutError, asyncio.TimeoutError) as exc:
```

**同一个知识，在同一个代码库里，正确了六章，然后没有传播。**
这是第 11 章的 `SYSTEMROOT`、第 12 章的 session 锁之后**第三次**。

修法选的是"哪个拼法两边都对"：`asyncio.TimeoutError` 在 3.10 上是
`wait_for` 抛的那个，在 3.11+ 上就是内置的那个。**一个拼法，处处正确。**
三处 `TimeoutError` 全改成它，`registry.py` 那三处一起。

然后把这条写成规则而不是写成那一处的修补：

```python
def test_F15_02_no_asyncio_timeout_is_caught_with_the_builtin(repo_root: Path) -> None:
    """The 3.10 fault, encoded as the rule rather than as the one instance.

    On 3.11+ `asyncio.TimeoutError` *is* the builtin, so both spellings work
    and the wrong one cannot be distinguished from the right one by running
    the tests on a modern interpreter.  Only one spelling works on both, so
    the rule is "use the one that always works" and this is what enforces it.
    """
```

**注意为什么这条必须是静态检查**：在 3.13 上跑测试，
两种拼法**行为完全相同**。没有任何动态测试能在现代解释器上区分它们。
这是本书第二次遇到"只有 lint 能看见"的故障（第一次是 F00-08 那个漏掉的 `await`）。

### 3.5 CI 矩阵：只测边界

```yaml
    strategy:
      fail-fast: false
      matrix:
        python-version: ["3.10", "3.13"]
```

两个，不是五个。理由写在 yaml 的注释里，和第 -1 章那条"阻塞 CI 最多六步"同源：

> 矩阵的代价是**每一个 PR 永远都要付**。一句版本承诺需要被验证的是它的
> **边界**。3.11 和 3.12 夹在两个都能跑通的解释器中间；
> 只在那里出现的故障是可能的，但不值得让每个 PR 的阻塞任务翻倍。

再跑一次，确认修好：

```
$ UV_PROJECT_ENVIRONMENT=.venv310 uv run --python 3.10 --all-extras pytest
1706 passed, 12 skipped in 83.33s
```

---

## §4 F15-03：一份能被处理的 bug 报告

### 4.1 陌生人的前五分钟

清单说：

> F15-03 用户报 bug 但没有版本号和复现步骤

修法看起来很显然：`--version` 多印点东西，加个 issue 模板。
但在写那些之前，先做一件十七章里没人做过的事：
**在一个空目录里，按一个陌生人的顺序，把这个程序跑一遍，
并且把退出码和 stdout/stderr 分开看。**

```
$ uv run python probe_release.py firstrun

  $ minicodex (no arguments)          [exit 0]
    out | usage: minicodex [-h] [--version]
    out |                  {ask,memory,sessions,fork,serve-stub,replay,rules,forget} ...
    …
  $ minicodex ask hello          [exit 1]
    out | [transport: waiting 1s, attempt 2 of 4]
    out | [transport: waiting 2s, attempt 3 of 4]
    out | [transport: waiting 4s, attempt 4 of 4]
    out | [transport: waiting 7s, attempt 5 of 4]
    err | the model call failed: ConnectError: All connection attempts failed
    err | gave up after 4 attempts and 19s of waiting
    err | the connection did not survive; check the network or the base URL.
```

两个 bug，都在线上，都不是这一章新写的代码。

**为什么 1656 个测试一个都没看见？** 因为每一个测试都是
`main(["ask", "...", "--yes"])` 然后看返回值。
没有一个测试**看终端**，也没有一个测试关心退出码之外的通道。

> **测试测的是函数的返回值，用户看的是一段随时间展开的输出。**
> 这两件事之间没有任何东西自动成立。

### 4.2 裸调用退出 0

`minicodex` 什么都不加，打印帮助到 **stdout**，退出 **0**。

对着终端的人看不出问题。对一个脚本来说，这是"成功了，并且输出了一份菜单"。
一个把子命令改名之后的用户脚本会**静默地什么都不做，然后报告成功**。

argparse 自己对"你没说要干什么"的答案是：帮助进 stderr，退出 2。照抄：

```python
    # No subcommand.  Help on stderr and exit 2, which is argparse's own answer
    # to "you did not say what you wanted" and the opposite of what this did
    # until 0.1.0: help on **stdout** and exit **0**, so a shell script whose
    # subcommand had been renamed saw a successful run that printed a menu.
    parser.print_help(sys.stderr)
    return 2
```

### 4.3 `attempt 5 of 4`

这条更值钱，因为它同时是一个**功能 bug**和一个**报告 bug**。

第 12 章的重试循环是 `for attempt in range(policy.attempts)`，
`attempts=4`，所以是 0、1、2、3。失败之后打印
`attempt {attempt + 2} of {attempts}`——"下一次是第几次"。
在最后一次（attempt=3）上，这句话说的是 `attempt 5 of 4`。

而它不只是打印错了：那一行下面就是 `await asyncio.sleep(wait)`。
**程序真的睡了 7 秒，然后放弃。** 生产配置里 `cap=30`，所以最坏情况是
白等 30 秒之后告诉你失败。

根因在第 12 章一个**当时判断正确**的决定里。`wait_for` 的 docstring 写着：

```
The *attempt count* is deliberately not checked here.  It was, for about an
hour, and mutation testing found the check dead: `Agent._respond` iterates
`range(policy.attempts)`, so deleting the guard from this function changed
no behaviour and broke no test.  Two enforcement points for one rule means
one of them is decoration, and the one to keep is the loop.
```

这段推理是对的：**执行**次数上限的地方只该有一个，就是循环。
但它漏了另一半：**循环拥有这个上限，所以循环也不该再去问**。

```python
            # `attempt + 1 == attempts` means this was the last one, and asking
            # how long to wait before an attempt that will not happen is how
            # the shipped program came to sleep seven seconds and then print
            # `attempt 5 of 4` before giving up.
            last = attempt + 1 >= self.retry_policy.attempts
            wait = None if last else wait_for(failure, attempt, self.retry_policy, elapsed=elapsed)
```

回归测试的写法本身有个反复。第一版是去 grep `agent.py` 有没有那两行——
**那是在测补丁，不是在测行为**。第二版驱动真正的循环，
数"用户看见了几行"和"睡了几次"：

```python
    slept: list[float] = []
    real_sleep = asyncio.sleep

    async def counting_sleep(seconds: float, *args, **kwargs):
        slept.append(seconds)
        return await real_sleep(0, *args, **kwargs)

    monkeypatch.setattr(asyncio, "sleep", counting_sleep)
    …
    waits = [line for line in said if "waiting" in line]
    assert len(waits) == attempts - 1, f"one wait per gap, not per attempt: {waits}"
    assert len(slept) == attempts - 1, f"slept before giving up: {slept}"
```

用**计数**而不是墙钟，理由写在注释里：墙钟会把三次连接尝试
（这台机器上每次 2.3 秒）也量进去，而那和这个故障无关。

修完之后：

```
  $ minicodex ask hello          [exit 1]
    out | [transport: waiting 1s, attempt 2 of 4]
    out | [transport: waiting 1s, attempt 3 of 4]
    out | [transport: waiting 4s, attempt 4 of 4]
    err | the model call failed: ConnectError: All connection attempts failed
    err | gave up after 4 attempts and 15s of waiting
```

### 4.4 `--version` 该印什么

现在才是清单说的那件事。而"印什么"这个问题**不需要重新想**——
第 7 章已经回答过一次了。

`SessionMeta` 记录 cwd / provider / model / sandbox_mode / approval_policy，
理由是（F07-07）：**历史里一个字都没提这些东西，而它的行为全靠它们。**
一份 bug 报告面对的是同一个问题，只是往上一层：机器而不是会话。

所以答案是同一份单子，加上只有新进程才知道的两件事：装的是哪个版本、装在哪。

```python
def environment_report(root: Path | None = None) -> str:
    """Everything a stranger has to paste for a report to be actionable.

    The list is not invented here.  Chapter 7's `SessionMeta` already had to
    decide what about the machine a transcript cannot reconstruct …
    """
```

### 4.5 最后一行才是全部

```
latest    C:\...\demo\.minicodex\recordings\session-1786890174.jsonl
          reproduce it with: minicodex replay C:\...\session-1786890174.jsonl
report    https://github.com/example/minicodex/issues
```

上面那些行只是缩小搜索范围。**`minicodex replay <那个文件>` 本身就是复现**——
第 14 章造的，不联网、不要 key、离线跑完整个循环。

而在这一章之前，没有任何东西告诉用户那个文件在哪。
它的文件名是一个时间戳，住在一个隐藏目录里。

> **一个能力和一个人能用上这个能力，中间还差一行字。**

这条的回归测试踩到了一个坑，是变异测试挖出来的：第一版写的是

```python
    assert "session-1786884876.jsonl" in report
```

把 `latest` 那一行整个删掉，**测试照绿**——因为文件名在下面那行
`reproduce it with:` 里还出现一次。而且只放一个录制文件，
测不出"最新的"和"随便哪个"的区别。改成两个文件、锚到行首：

```python
    latest = next(line for line in report.splitlines() if line.startswith("latest"))
    assert new.name in latest
    assert old.name not in report
```

---

## §5 F15-04：改名要有截止日期

### 5.1 两个该改的名字

清单说：

> F15-04 旧用法要废弃，直接删掉，用户全炸

那得先有该废弃的东西。这个程序有两个，都是真的该改：

1. **`minicodex forget N`**（第 5 章：撤销一条记住的审批）。
   第 17 章给了 `forget` 第二个含义——`memory --forget-all` 删除全部记忆——
   而后者响亮得多。**一个动词同时能擦掉两样不相干的东西，
   早晚有人指错地方。** 改成 `minicodex rules --forget N`。
2. **`--yes`**。它读起来像"对屏幕上这个问题回答是"，
   实际含义是"对这次运行将要问的**每一个**问题回答是"。
   改成 `--dangerously-approve-all`——codex 自己那个更长
   （`--dangerously-bypass-approvals-and-sandbox`），思路一样：
   **危险的东西要在打字的时候就让人不舒服。**

### 5.2 先量删除的代价

"直接删掉"这个选项到底有多贵？先数一遍——数的是**我自己的仓库**：

```
$ uv run python probe_release.py deprecated

  '--yes' -> '--dangerously-approve-all': 17 occurrence(s) in 13 file(s)
      2  steps\step15_release\tests\test_faults_ch14.py
      2  tutorial\ch14-eval.md
      2  tutorial\ch16-memory-read.md
      2  tutorial\ch17-memory-write.md
      1  steps\step15_release\src\minicodex\approval.py
      …

  'minicodex forget' -> 'minicodex rules --forget': 8 occurrence(s) in 5 file(s)
      4  tutorial\ch05-approval.md
      1  steps\step15_release\src\minicodex\rules.py
      …
```

**17 处，13 个文件，而用户数是零。**

这个数字是个**下界**，而且是最松的那种下界：这些是**知道改名要来了的那个人**
写下的用法。用户写的东西从这里完全看不见，而且不会更少。

### 5.3 在 argparse 之前翻译

实现有两种：让 argparse 同时认两种拼法，或者在它看到 argv 之前重写。

选后者，理由是第 1 章那条：

```python
def apply_deprecations(argv: list[str]) -> tuple[list[str], list[str]]:
    """Rewrite old spellings into current ones and say so, once each.

    Before `parse_args` rather than after, and that is not a detail: argparse
    can hold both spellings itself, and then every branch downstream has to
    remember that two attributes mean one thing.  Translating at the edge is
    chapter 1's rule (normalise at the boundary, and nothing above it knows
    there was ever a second shape) applied to the command line.
    """
```

它买到的东西在**删除**那一天兑现：`forget` 这个子命令在 `__main__.py` 里
**根本不存在**，只有一个 `rules --forget`。0.3.0 删掉这条废弃，
删的是 `DEPRECATIONS` 里的一个条目，**不会留下任何一段烂掉的分支**。

一个细节，也是一条测试：

```python
    A positional command is only rewritten in first position.  `minicodex ask
    "how do I forget a rule"` contains the word and is not the command.
```

`forget` 是命令，也是英语单词。在任何位置都重写，
会把一个提问变成另一条命令——**比它要缓和的那次改名坏得多**。

警告去 stderr：

```python
    for warning in warnings:
        # stderr, so that a script reading stdout is unaffected by a message
        # aimed at the person maintaining it.
        print(warning, file=sys.stderr)
```

废弃期的**全部意义**就是脚本继续能跑，所以警告不能出现在脚本读的通道里。

```
$ minicodex forget 1
warning: `forget` is deprecated since 0.1.0 and will be removed in 0.3.0;
         use `rules --forget` instead (chapter 17 gave `forget` a second
         meaning, and it is the louder one)
no rule numbered 1; run `minicodex rules` to see them
```

### 5.4 可执行的日历

这是本章最重要的一个机制，一共六行。

问题是这样的：一条废弃警告写着"will be removed in 0.3.0"。
到了 0.3.0，谁会记得？答案是没有人。真实项目里的废弃警告**平均寿命是永远**，
于是接口只会变大，从来不会变小。

而这本书从第 11 章起反复撞到同一句话：**散文里的承诺不是机制。**

```python
def test_F15_04_no_deprecation_outlives_its_own_removal_version() -> None:
    for rule in DEPRECATIONS:
        assert version_parts(minicodex.__version__) < version_parts(rule.remove_in), (
            f"`{' '.join(rule.old)}` was to be removed in {rule.remove_in} and this is "
            f"{minicodex.__version__}: delete it, or move the date deliberately"
        )
```

**把 `__version__` 改成 0.3.0，这个测试立刻变红。**
那一刻只有两个选择：删掉别名，或者**有意识地**把日期往后挪。
不会有第三种，也就是"忘了"。

`version_parts` 而不是字符串比较，也是被逼出来的：

```python
def version_parts(version: str) -> tuple[int, ...]:
    """`"0.10.0"` -> `(0, 10, 0)`, so that 0.10 sorts above 0.9.

    Written because the obvious string comparison gets that pair backwards,
    and because the probe that sweeps this project's own past versions got the
    same class of thing wrong first: `stepA_refactor` sorts after
    `step13_system_prompt` and belongs seven chapters earlier.  Ordering is
    the one job a version number has.
    """
```

后半句是真事：`probe_release.py` 第一版用 `sorted(STEPS.iterdir())`
并且管它叫"book order"，结果把两个插曲排到了最后——
比它们该在的位置晚了七章和六章。输出看起来还是一张整齐的表，
**而排得最离谱的那两行正是位置本身携带信息的两行**。

> **版本序不是字符串序。这就是语义化版本是一份规范而不是一个命名习惯的全部原因。**

---

## §6 F15-05：别人来了之后

### 6.1 codex 的选择，以及它的算术

清单：

> F15-05 外部 PR 涌入，review 成本超过自己写 → codex 的选择：关闭外部代码贡献

照抄这个结论很容易，但"不接受 PR"写出来像是傲慢。
`CONTRIBUTING.md` 里写的是算术：

> 评审一个 Agent 的改动，意味着判断它对不对，而这个程序做的大部分事情，
> **"对不对"在 diff 里根本看不见**：
>
> * 改一句工具描述、系统提示词或错误信息，改的是**模型行为**。
>   唯一能知道它干了什么的办法是拿一个任务集对着 provider 跑两遍——
>   开和关——然后去读那些**变差**的样本。第 14 章整章都在讲这件事有多贵、
>   以及骗自己有多容易。
> * 改审批或沙箱附近的代码是安全改动，改错的后果落在别人的仓库上。
> * 改上下文、压缩或记忆附近的任何东西，会**静默地**改变用户之后
>   **每一次**请求，方向是任何单元测试都观察不到的。
>
> 一个无偿的维护者读一遍 diff 就 merge，那不叫评审，那叫替陌生人猜。
> 拒绝整个类别比把其中一些 merge 坏了要诚实：
> 前者花掉贡献者一个下午，后者花掉所有用户一次回归。

然后说清楚**什么比补丁更有用**：一份录制、一次测量、一条没人列过的故障。

### 6.2 issue 模板就是复现的输入

模板不是礼貌，是机制。它的头两个字段就是 §4 那两样东西：

```yaml
  - type: textarea
    id: version
    attributes:
      label: minicodex --version
      description: Paste the whole block. "0.0.1" alone is not a version — fourteen builds said that.

  - type: input
    id: recording
    attributes:
      label: Recording
      description: >
        The last line of the block above names it. Attach the file, or say why
        you cannot. `minicodex replay <file>` re-runs it offline with no API key.
```

第三个字段是本书方法论的直接产物：

```yaml
  - type: dropdown
    id: silent
    attributes:
      label: How did you notice?
      description: >
        Roughly half the faults in this program's history produced no error at
        all. Which kind this is changes where to look first.
      options:
        - It crashed or printed an error
        - No error — the result was wrong
        - It never finished
        - It did something it should have asked about first
```

**§4.2 那张"发现方式"表，反过来问用户。**

模板和程序之间靠一个测试钉住，因为文档会漂——本书已经记录过四次了：

```python
def test_F15_05_the_bug_template_asks_for_exactly_what_version_prints(...):
    labels = [...]
    assert "minicodex --version" in labels
    assert any("Recording" in label for label in labels)
```

还有一行是 `CONTRIBUTING.md` 里**会执行的那一半**：

```yaml
blank_issues_enabled: false
```

开着的时候，那份表格只是一个建议。而每一个字段之所以存在，
都是因为有人交过一份没有它的报告——**建议正是那些报告的状态**。

### 6.3 `SECURITY.md`：说清楚哪里不设防

安全策略里最有用的一段不是"怎么报"，是**"什么不算"**：

> 这个沙箱是**一个审批闸门，不是一个沙箱**。没有操作系统级别的隔离：
> 一条用户批准的命令可以做用户能做的任何事。这一条是写下来的，不是被防住的，
> 因此"一条被批准的命令干了坏事"不是这个程序的漏洞。

> 如果结论是"不修"，它就进 `SECURITY.md` 的已知限制，
> 因为一条没写下来的已知限制和一个秘密是同一样东西。

### 6.4 `release.yml`：顺序就是全部设计

```yaml
      - name: Release checks      # tag == __version__, changelog has an entry
      - name: Test                # the whole suite, on this commit
      - name: Build
      - name: Install the artefact somewhere empty and run it
      - name: Publish
```

**每一个能拒绝的步骤都在那个不能撤销的步骤之上。**
PyPI 上的文件是不可变的；yank 只是让解析器看不见它，
不会从任何已经装了它的人那里收回来，而且版本号已经烧掉了。

倒数第二步是这个 workflow 存在的理由：`ci.yml` 在一棵
`uv sync --all-extras` 的树里跑 1707 个测试，而这 1707 个测试
**在一个漏了数据文件、import 了 dev 依赖、或者没有 entry point 的 wheel 上会全绿。**

顺序本身写成断言，因为一句注释拦不住"为了让产物早点出去，
把 publish 挪到测试前面"：

```python
    publish = next(i for i, n in enumerate(names) if "Publish" in n)
    for label in ("Release checks", "Test", "Build", "Install the artefact"):
        index = next(i for i, n in enumerate(names) if n.startswith(label))
        assert index < publish, f"{label!r} runs after the upload"
    assert publish == len(steps) - 1
```

`scripts/check_release.py` 只做两件事，理由写在它的 docstring 里：

```
Everything else worth checking before publishing is already a test, and is
therefore already run by `ci.yml` on the commit being tagged. …
Repeating those here would be a second ruler, and this project has paid for
that mistake twice (F06-12, and the memory summary in chapter 17).

What is *not* a test, because it does not exist until somebody types it:
  1. the tag and the version agree.
  2. the changelog has an entry for it.
```

**一句诚实的话**：这个 workflow 从来没有跑过——没有任何地方发布过叫
`minicodex` 的包。它在文件末尾自己写着这句，和 `nightly.yml` 从第 14 章起
写的那句一样。**一个从来没跑过的 workflow 是一份草稿，说出来不花钱。**

---

## §7 变异测试：这一章自己的两个 bug

27 条变异。前 22 条是照着测试写的——**这是这个检查最弱的一种形式**，
写测试和写变异是同一个人同一个下午的事，当然都能抓到。
后 5 条是反过来选的：**问"这一章改了什么而没有任何东西断言"**。

第一次跑：

```
27 mutations, tests/test_faults_ch15.py tests/test_packaging.py tests/test_faults_ch14.py

    1 test(s) fail  <-  the version is a literal here as well as in the module
    2 test(s) fail  <-  version comparison is lexical, so 0.10 sorts below 0.9
   11 test(s) fail  <-  a recording older than the attempt field crashes instead of migrating
   …
every mutation was caught.
```

**全绿，而且是假的。**

注意那一列数字：一大堆 `1`。一条变异只让一个测试红，本身不奇怪。
但去查那一个是谁：

```
$ # 手动应用「_count 永远返回 0」这条变异，然后跑
FAILED tests/test_packaging.py::test_F_1_05_every_mutation_script_runs_somewhere
```

`test_F_1_05_every_mutation_script_runs_somewhere` 是第 12 章加的元测试——
"没人跑的变异脚本是一个文件，不是一个检查"。
它在抱怨 `probe_mutations_ch15.py` **还没有被接进 `postmerge.yml`**。

于是它在**27 次运行里每一次都是红的**，和被测的那条变异毫无关系。

把脚本接进 `postmerge.yml`，再跑：

```
9 mutation(s) nothing noticed:
  - the version is a literal here as well as in the module
  - the list of on-disk surfaces loses the one nobody thinks about
  - --version stops naming the recording that reproduces the run
  - a command word is rewritten wherever it appears, including inside a question
  - a release with no changelog entry publishes anyway
  - --version reports the *oldest* recording, which is never the one that broke
  - --version reports a session count of zero however many there are
  - a surface points at a path this program does not use
  - blank issues are enabled again, so the template is optional
```

**九条。恰好就是刚才显示 `1` 的那九行。**

九条里最值得说的三条：

1. **"版本号又变成两处字面量"没被抓到**，而我明明写了那个测试。
   原因：`installed_version("minicodex")` 读的是**可编辑安装的元数据**，
   那份元数据是 `uv sync` 那一刻生成的，改 `pyproject.toml` 不会重新生成它。
   分歧只存在于**改动之后构建出来的产物里**。修法是同时断言 wheel 的
   `Version:` 字段——**而这一章之所以有能力这么做，正是因为它在构建产物**。
2. **"位置守卫被删掉"没被抓到**，因为我的参数化用例是
   `["ask", "how do I forget a rule"]`——那个 token 是整个字符串，
   压根不等于 `"forget"`，守卫从来没被执行到。
   改成 `["ask", "forget"]`。**一个用例要真的走到它声称在测的那个分支。**
3. **`SURFACES` 删掉一条没被抓到**，因为断言是 `len(SURFACES) >= 4`——
   五条删成四条还是通过。**一张列表只能靠点名来检查。**

修完九条之后，第三次跑，剩一条活的：

```
    0 test(s) fail  <-  blank issues are enabled again, so the template is optional
```

而这一条是**假的活口**：变异串 `blank_issues_enabled: false` 在
`config.yml` 里出现了两次，第一次在注释里引用它自己，
`replace(..., 1)` 改的是注释。**变异根本没生效。**

```python
        # The leading newline anchors this to the setting rather than to the
        # comment above it that quotes the setting.  Without it, `replace(...,
        # 1)` edited the comment, the file's behaviour did not change, and this
        # script reported a survivor -- a mutation that was never applied,
        # presented as a gap in the tests.
        "\nblank_issues_enabled: false",
```

一个工具，两种反向的错误，同一次运行里：
**先是九条假的"抓到了"，然后是一条假的"没抓到"。**

第 17 章章末那句话，第五次兑现，而且这次两个方向都占了：

> **每写一个测量工具，就要预算它自己会有一到两个 bug，
> 而且它们的形状永远是"报告一个关于别的东西的数字"。**

最终：

```
$ uv run python probe_mutations_ch15.py
27 mutations, …
every mutation was caught.
```

---

## §8 收工

### 8.1 commit 序列

```
1  fix(replay): read a recording older than the attempt field

   Chapter 14's loader refuses a recording with no tool-call arguments and
   says which field is missing. It crashed with a bare KeyError on anything
   older than chapter 12, which is nine of this project's own fourteen past
   versions. The two cases are not the same: arguments are *gone* and must
   be refused, because filling them in replays a conversation that never
   happened; `attempt` was never written down and means "one attempt".

2  test(compat): read what fourteen past versions wrote

   probe_release.py compat runs every installed step directory against the
   chapter 0 stub and hands the artefacts to this code. 11/11 sessions load
   -- chapter 7's "an added field is not a breaking change" argument, run
   for the first time. 11 of 14 recordings do not, and recorder.py is
   byte-identical in all of them: the payloads are built at the call sites,
   so a diff cannot tell you whether a format changed.

   The corpus is committed (192KB). Regenerating it needs fourteen installed
   environments and four minutes; reading it needs neither.

3  fix(subagent): catch the timeout 3.10 actually raises

   requires-python has said >=3.10 since chapter -1 and nothing had ever run
   there. asyncio.TimeoutError only became the builtin in 3.11, so
   `except TimeoutError` never fired: a hanging child was not stopped, and
   its subprocess outlived the run. mcp.py had caught both since chapter 9.
   One spelling works on both interpreters; a static test now requires it,
   because on 3.11+ the wrong one is indistinguishable from the right one.

4  ci: run the floor of requires-python, not only the ceiling

   Two entries, not five. A version claim needs its boundaries tested; a
   break that only appears on 3.11 or 3.12 is possible and is not worth
   doubling every pull request forever.

5  fix(cli): stop announcing an attempt that will not happen

   The loop asked wait_for() for a backoff after its last attempt, slept for
   it, and printed `attempt 5 of 4` before giving up -- up to 30s of waiting
   for nothing, and a wrong number in every bug report that quotes it.
   Chapter 12 was right that the loop is the one enforcement point for the
   attempt count; this is the other half of that decision.

   Also: no subcommand now prints help on stderr and exits 2. It printed on
   stdout and exited 0, which tells a script that something worked.

6  feat(release): --version says enough to act on, and names the repro

   The facts are chapter 7's SessionMeta list one level up -- a report has
   the same problem about the machine that a resumed session has about the
   transcript. The last line is the one that matters: `minicodex replay
   <file>` *is* the reproduction, and until now nothing told anybody which
   file that is.

7  feat(release): one version literal, a licence, and a front page

   pyproject reads __version__ from the module instead of repeating it; the
   wheel declares MIT, a homepage and an issue tracker; the long description
   is written for someone who has just installed the package rather than for
   a reader of chapter 17. All three were only ever visible from the wheel,
   which is why seventeen chapters of tests did not see them.

8  feat(cli): rename two spellings, with an end date a test enforces

   `minicodex forget N` -> `rules --forget N` (chapter 17 gave the word a
   second and louder meaning) and `--yes` -> `--dangerously-approve-all`.
   Both rewritten before argparse sees them, so there is one implementation
   and deleting the alias in 0.3.0 deletes all of it.

   The date is not a note: test_F15_04_no_deprecation_outlives_its_own_
   removal_version goes red when __version__ reaches remove_in. A promise in
   prose is not a mechanism -- fourth time in this book.

9  docs: CHANGELOG, CONTRIBUTING, SECURITY, and an issue form

   Breaking (files) is its own heading because the 0.x escape hatch does not
   cover a user's .minicodex/ directory -- it does not read version numbers.
   Pull requests are not accepted, with the arithmetic rather than the rule.

10 ci: release.yml, and the mutation step this chapter forgot

    Everything that can refuse runs above the step that cannot be undone.
    probe_mutations_ch15.py joins postmerge.yml -- and finding out that it
    had not is in §7, because for 27 runs it was the mutation script's own
    result.
```

**commit 1 在最前面**，因为它是唯一一条纯 bug 修复，而且是探针撞出来的；
**commit 2 才引入探针本身**。这个顺序是反的时间顺序，故意的：
一个能独立回滚的修复不该被埋在一个新工具的 diff 里。

### 8.2 PR 描述

```markdown
## What

Ship it. Version 0.1.0, and the mechanisms that make a version number mean
something.

* `release.py` — `SURFACES` (the files that outlive a release), `DEPRECATIONS`
  with an enforced removal date, `environment_report()`.
* Packaging: one version literal, a licence, an issue tracker, a front page.
* `tests/fixtures/compat/` — artefacts written by fourteen real past versions.
* `CHANGELOG` / `CONTRIBUTING` / `SECURITY` / issue form / `release.yml`.
* Three fixes in shipped code (see below).

## Why

Every test in this repository imports `minicodex` from a tree that was synced
two seconds ago. A user installs a wheel, reads a terminal, and has files
written by last month's build. Three of those four differences were wrong.

## How

* Old artefacts are produced by *running the old versions*, not by writing
  fixtures from memory. Chapter 14's argument at a different scale.
* An old file is **migrated** when the information was merely never written
  down and **refused** when it is gone. Both policies are in `SURFACES`.
* The deprecation calendar is a test, not a note.
* `release.yml` puts every step that can refuse above the one that cannot be
  undone.

## Testing

54 new tests, 1707 total, green on **3.10 and 3.13**. 27 mutations; nine
survived the second run and are described in the chapter — the first run was
worthless because a meta-test unrelated to any mutation was failing in all 27.

## Notes for the reviewer

* **F15-02 did not reproduce as listed.** No data file is missing from the
  wheel. What is missing is a licence and a way to reach the author, and the
  chapter -1 test that would have caught a missing file names one filename as
  a literal — two more files have arrived since and nothing checks them.
* **Three fixes are in code from chapters 12, 10 and 5**, not in this
  chapter's. They were found by installing the artefact and by honouring
  `requires-python`, which is the whole point.
* The compat corpus is fourteen versions of *one* conversation. It answers
  "can the reader still read the writer" and nothing else.
```

### 8.3 Code review

我扮演 reviewer，五条：

> **1（正确性）**：`apply_deprecations` 在 argparse 之前重写 argv，
> 而 `--yes` 是一个前缀。`minicodex ask --yes-man` 会不会被改坏？

不会，但理由值得写下来：比较的是 `token != rule.old[0]`，**整个 token 相等**，
不是前缀匹配。第 5 章 F05-01 那条（前缀匹配被 `git status; rm -rf /` 绕过）
是同一个教训的另一面——**前缀匹配在两个方向上都是错的**。
不过这条意见暴露了一个真的缺口：`--yes=1` 这种写法 argparse 支持，
而我的重写不认。`store_true` 的标志不接受 `=`，所以现在不成立；
记进 step README 的 "deliberately not done"。

> **2（边界）**：`SURFACES` 里 `memory jobs` 的 `version_marker` 写的是
> `sqlite user_version`，但第 17 章的 `memory_jobs.py` **根本没有设置过
> `user_version`**。这是一句愿望。

说得对，而且这是本章最该被抓到的一条。三个选择：去把 `user_version` 加上、
把这一格改成 `none`、或者删掉这一行。选第二个——加一个没人读的版本字段
是"有防御没有调用者"，第 17 章刚为这个删过一个 `ephemeral`。
**一张说明书上的每一格都是一句断言。**

> **3（可测试性）**：`probe_release.py compat` 要十四个装好的环境。
> 别人 clone 这本书之后跑不了它。那这一章的核心测量对读者是不可复现的。

一半对。`uv sync` 每个 step 目录就能重建，README 里写了。
但真正的答案是**语料进了版本库**：读者不需要复现测量就能跑那些测试，
而这正是 §2.7 那个决定要买的东西。
**一次昂贵的测量 + 冻下来的产物 + 一个便宜的检查**，
这个组合在第 14 章（录制/回放）和这里是同一个形状。

> **4（命名）**：`environment_report()` 这个名字太软了。它印的是
> "一份 bug 报告该有的东西"，不是"环境"。

我保留这个名字，理由是它的**调用点**：`minicodex --version`。
一个叫 `bug_report()` 的函数被 `--version` 调用会更奇怪。
但 docstring 的第一句改了，从描述它印什么改成说清它为谁印：
"Everything a stranger has to paste for a report to be actionable."

> **5（风格）**：`release.py` 里有 `SURFACES`、`DEPRECATIONS`、
> `environment_report` 三样东西，它们之间没有任何调用关系。
> 这不是一个模块，这是一个抽屉。

这条我认一半。它们确实互不调用，但共享一个非常具体的定义：
**这个程序对"不是它作者的人"做出的承诺**——磁盘上的、命令行上的、报告里的。
按调用关系分模块是一种分法，按**变更原因**分是另一种，
而这三样东西的变更原因是同一件事：有人在用它。

真正会让我拆开它的信号是第三方出现：如果哪天 `SURFACES` 长出了
迁移函数、`DEPRECATIONS` 长出了多版本链，那时 `compat.py` 和
`deprecations.py` 是两条真缝。现在拆是猜——插曲 B 的 FB-03 第三次。

### 8.4 Merge 与 CI

squash 进 `main`，然后打 tag。

`ci.yml` **加了一个矩阵，没有加步骤**——第 -1 章那个"阻塞 CI 最多六步"的
上限第六次起作用，而这次它逼出来的是一个更好的答案：
矩阵是 job 层面的，不占步骤预算，而且"跑两个解释器"本来就不该是一个新步骤。

`postmerge.yml` 加一步（本章的变异脚本），`release.yml` 是新的第四层：

| 层 | 什么时候跑 | 挡不挡 merge | 从哪一章来 |
|---|---|---|---|
| `ci.yml` | 每个 PR，两个 Python | 挡 | 第 -1 章 |
| `postmerge.yml` | merge 到 main 之后 | 不挡 | 第 9 章 |
| `nightly.yml` | 每天，真 provider | 不挡 | 第 14 章 |
| `release.yml` | 打 tag | —— | **本章** |

第四层和前三层的区别不是"更严格"，是**不可撤销**。
前三层失败了重跑一次；这一层失败了，那个版本号就烧掉了。

### 8.5 全套

```
$ uv run pytest
1707 passed, 12 skipped in 124.35s

$ UV_PROJECT_ENVIRONMENT=.venv310 uv run --python 3.10 --all-extras pytest
1707 passed, 12 skipped in 102.63s

$ uv run ruff format --check . && uv run ruff check .
All checks passed!

$ uv run python scripts/check_layers.py
ok  no import cycles
ok  __init__ is a leaf
ok  forbidden edges
ok  one Agent construction site
ok  every module imports alone

$ uv run python probe_mutations_ch15.py
27 mutations …
every mutation was caught.

$ uv run python scripts/check_release.py v0.1.0
$ echo $?
0
```

**两个解释器上同样的 1707 和同样的 12 skipped** ——
这句话在这一章开始的时候是假的。

---

## §9 对照 codex

| 这里 | codex | 差在哪 |
|---|---|---|
| `CONTRIBUTING.md`：不接受 PR，收 issue 和分析 | 同样的选择，写在仓库说明里 | 一样。这是全书唯一一条我直接照抄结论的 |
| `SURFACES`：五个磁盘契约，各带一条 unreadable 策略 | 没有这么一张表；策略散在各自的模块里（`rollout` 的版本字段、`memory_summary.md` 第一行的 `v1`、state 库的 migration） | 我把它集中成一张表，因为**这五条散着的时候，没有任何人能回答"我这次改动破坏了什么"** |
| `memory_summary.md` 第一行必须是 `v1`，不是就整份重写 | 同 | 一样，而且这是"rewrite"这个策略的出处 |
| `state/…/0001_memories.sql` 编号迁移 | 同 | 我这边是 `recreate`（它是缓存），差别是数据的价值不同 |
| `legacy_apply_patch_exec_command_warning.rs`、`legacy_unified_exec_process_limit_warning.rs` | **专门为"用户还在用旧的错误用法"写的警告模块** | 我的 `DEPRECATIONS` 是两条表项；它的是**独立模块，一个故障一个**。规模差两个数量级，形状一样：每一个都是一次线上故障的墓碑 |
| `remove_in` + 一个会变红的测试 | 没有找到对应机制 | 这是我加的。codex 的 `legacy_*` 模块长期共存（`agent/control/legacy.rs`、`core-plugins/src/remote_legacy.rs`、`features/src/legacy.rs`），**这正是没有截止日期的后果** |
| `blocking-ci.yml` / `postmerge-ci.yml` | 同样的分层 | 一样，我多一层 nightly 和一层 release |
| `--version` 印录制路径 | codex 有 `codex debug` 系列 | 思路一样：让用户手里那份产物自己说出复现方式 |

**最值得看的一行是 `legacy_*` 那两行。** 全书第一章就把它们当作
"人类痕迹"的证据引用过：*每一个都是一次线上故障的墓碑*。
写到第 15 章才明白它们的另一半含义——**它们还在那里**。
一个没有截止日期的兼容层，寿命是无限的。

---

## §10 回头看：这一章撞到了什么

清单 5 条：

| ID | 结果 |
|---|---|
| F15-01 | **形态比清单写的更糟**。不是"破坏性变更发了 patch 版本"，是**十四个版本共用一个版本号 `0.0.1`**，而录制格式在这个范围内变过两次。交叉版本实测：会话 11/11 可读，录制 14 份里 11 份不可读。附带一条比结论更值钱的观察：**`recorder.py` 在二十个版本里一个字节都没变，而它写的格式变了**——格式定义在调用点 |
| F15-02 | **没按清单的样子复现**：wheel 里没有漏任何数据文件。真正缺的是**许可证、`Project-URL`、以及一份写给用户的首页**——三样都只有从产物里才看得见。而 `requires-python = ">=3.10"` 这句承诺**十七章没有被执行过一次**，第一次执行就红了一个测试：3.10 上 `asyncio.TimeoutError` 不是内置 `TimeoutError`，F10-07 的整个防御在那个平台上是关的 |
| F15-03 | **复现，而且现场又抓到两条**。清单说的"没有版本号和复现步骤"修法是 `--version` + issue 模板；而"在空目录里按陌生人的顺序跑一遍"这个动作本身抓到了：裸调用打印帮助到 stdout 并**退出 0**，以及重试循环打印 **`attempt 5 of 4`** 并且真的在放弃之前睡了 7 秒 |
| F15-04 | 做了，并且把清单里没有的那一半补上：**废弃需要一个会到期的截止日期**。删除代价量出来是 17 处/13 个文件（用户数为零时的下界）。实现放在 argparse 之前，于是 0.3.0 删除时**不会留下任何分支** |
| F15-05 | 照抄 codex 的结论（不接受外部代码贡献），但把**算术**写出来。机制部分是 issue 模板与 `--version` 互相钉住，以及 `blank_issues_enabled: false`——`CONTRIBUTING.md` 里会执行的那一半 |

清单外 8 条：

| 故障 | 发现 | 修法 |
|---|---|---|
| **`replay.load()` 对更旧的录制抛裸 `KeyError`**：第 14 章那句写得很好的拒绝，只覆盖了当时手头那一种旧文件；14 份里 9 份走不到它 | 🔴 | 分成两条规则：信息**没被写下来**就迁移（`attempt` ⇒ 0），信息**丢了**就拒绝并说清字段 |
| **`recorder.py` 二十个版本零改动，格式变了两次** | 🟠 | 无法修，只能改变检查方式：拿旧文件喂新代码。这条是本章最重要的方法论产出 |
| **`asyncio.TimeoutError` ≠ `TimeoutError`（3.10）**，两处；而 `mcp.py` 从第 9 章起就写对了 | ⚪ → 🔴 | 统一成两边都对的拼法，并且写成**静态**规则——现代解释器上两种拼法行为完全相同，动态测试区分不了 |
| **`minicodex` 裸调用打印帮助到 stdout 并退出 0** | 🟢 | stderr + exit 2。所有测试都调 `main([...])` 看返回值，没有一个看终端 |
| **`attempt 5 of 4`，外加一次白等最多 30 秒** | 🟠 | 第 12 章"只在循环里执行次数上限"的另一半：循环拥有它，所以循环也不该再问 |
| **变异脚本 27 次运行里每一次都被一个无关的元测试染红**，于是九个活口被报成"全部抓到" | ⚪ | 把脚本接进 `postmerge.yml`（那个元测试要的就是这个）。**九个假的"抓到"** |
| **一条变异改到了注释上**，于是被报成活口 | ⚪ | 变异串锚到行首。**同一次运行里，两个反方向的错误** |
| **`compat` 探针在十二个 step 目录里留下了一个空的 `.minicodex/`**：它小心地把自己创建的**文件**删干净了，没管**目录** | 🟠 | 记下"目录本来在不在"，不在就一起删。这是它自己 docstring 里那句"一个会编辑你的树的工具，就是一个可能把你的树留在被编辑状态的工具"的缩小版——**本书第三次** |

发现方式分布（13 条）：

| 方式 | 条数 |
|---|---|
| ⚪ 静态 / 变异测试 | 4 |
| 🟠 可观测性 | 4 |
| 🔴 崩溃 | 2 |
| 🟢 主动边界测试 | 2 |
| ⚫ 用户报告（推演） | 1 |

**这一章的分布和全书任何一章都不一样：没有一条 🟡 静默错误，也没有一条 🔵 长跑。**
原因很直白——这一章测的不是模型行为，是产物。
产物的故障要么在静态检查里，要么在你第一次把它装到别处的时候。

而 12 条里有 **2 条出在这一章自己的验证工具上**，
第 6、14、16、17 章之后第五次。这次的形状是新的：
**同一个工具，同一次运行，先报了九个假的"抓到"，又报了一个假的"没抓到"。**

---

## 如果你只记住三件事

1. **一个程序的公开接口，包含它写在别人磁盘上的每一个文件。**
   语义化版本的 0.x 豁免说的是"公开 API 随时可能变"——那是对代码说的。
   用户的 `.minicodex/` 目录不读版本号。这本书量出来的具体数字是：
   会话文件 11/11 兼容（因为第 7 章那条"加字段不算破坏"的论证是对的），
   录制文件 14 份里 11 份不兼容（因为第 14 章改的是**调用点**）。
   而**"哪些文件改了"永远回答不了"格式变了没有"**——
   `recorder.py` 二十个版本一个字节没动。
   唯一能回答的办法是：**留住旧版本写的文件，用新代码去读。**

2. **测试测的是函数的返回值；用户看的是终端、退出码和一个装好的 wheel。**
   这三样东西，1656 个测试一个都没碰过，于是这一章什么新功能都还没写，
   光是"装到空环境里按陌生人的顺序跑一遍"就抓到了三条线上故障——
   而其中两条是在**别的章节的代码**里。
   所以：**发布不是一个流程，是一次换视角的测量。**
   把产物装到一个什么都没有的地方，然后自己当一次用户。

3. **一句承诺，只有当某个东西会因为它变红时，才是一句承诺。**
   `requires-python = ">=3.10"` 写了十七章，从没跑过，而且是假的。
   一条 "will be removed in 0.3.0" 的废弃警告，如果没有测试在 0.3.0 变红，
   它的寿命就是永远——codex 那三个 `legacy_*` 模块还在那里。
   本章所有的机制都是同一个形状：把散文里的承诺换成一个会失败的东西。
   **而这条已经是本书第五次说了**（第 3 章的 description 快照、
   插曲 B 的 CI 边界检查、第 12 章的"没人跑的变异脚本"元测试、
   第 14 章的连线变异、这里）——
   区别只在于，这一次那个元测试反过来咬了我一口：
   它在 27 次变异运行里全程为红，把九个活口染成了绿色。

---

## 动手练习

1. **亲手撞一次 `KeyError: 'attempt'`。**
   把 `replay.py` 里的 `payload.get("attempt", 0)` 改回 `payload["attempt"]`，
   然后 `uv run pytest tests/test_faults_ch15.py -k recording`。
   数一下**几个版本变红**——然后想清楚：如果 `tests/fixtures/compat/`
   里只有第 13 章那一份（也就是我写第 14 章时能拿到的那一份），
   这个测试会不会变红？

2. **给一个 surface 真的加上版本标记。**
   §8.3 review 第 2 条把 `memory jobs` 的 `version_marker` 从
   `sqlite user_version` 改成了 `none`，因为那是一句愿望。
   现在去把它变成真的：在 `memory_jobs.py` 建库时 `PRAGMA user_version = 1`，
   读的时候检查它。然后回答一个问题：
   **这个字段的第一个读者出现之前，它值得存在吗？**
   （第 17 章为这个问题删掉过一个 `ephemeral` 字段。）

3. **把废弃日历推到期。**
   把 `__version__` 改成 `"0.3.0"`，跑 `uv run pytest tests/test_faults_ch15.py`。
   看那条失败信息，然后**照它说的做**：删掉 `DEPRECATIONS` 里的两条。
   数一下你一共改了几个文件——这就是"在边界翻译"买到的东西。
   然后把 `probe_release.py deprecated` 再跑一次，看看那 17 处里还剩几处。

4. **（难）给会话文件也造一次真正的破坏性变更。**
   `SessionMeta` 至今没有 bump 过 `type_version`，因为所有改动都是加字段。
   现在做一次**重新解释**：把 `cwd` 字段的含义从绝对路径改成"相对于家目录"。
   不 bump 版本，跑 `tests/test_faults_ch15.py -k session`——
   **它会不会变红？** 如果不会，说明那个测试只检查了"能读出来"，
   没检查"读出来的意思对不对"。补上，然后再决定要不要 bump。
   （这就是 F07-09 当年那次 bump 的完整推理过程。）

5. **（难）把这个补偿链条走完：从一份 issue 到一个测试。**
   拿 §1.1 那次运行留下的录制，假装它是用户寄来的。写一个脚本：
   读 `--version` 块 → 找到录制路径 → `minicodex replay` →
   如果 drift，把 drift 的那一轮裁剪成最小复现（第 4.3 节的"最小化复现"）。
   然后回答：**这个脚本能自动化到哪一步为止，从哪一步开始必须有人？**
   这是本书最后一个开放问题，也是"维护"这个词的全部内容。
---

# 附录 · 代码到底怎么敲出来的（新手向，逐段精讲）

跟第 12、5 章附录同一个规矩：正文讲"为什么"，这里补"怎么写"。默认你已经
读过前面几章的附录（`dataclasses`、`asyncio`、`Literal` 那些基础），这里
只讲这一章新出现的、容易让新手卡住的写法。代码摘自
`steps/step15_release/src/minicodex/`，逐段核对过。

先把范围说死：

1. 本附录只解释第 15 章在 `steps/step15_release/` 里新增或修改的代码。
   记忆三件套（`memory.py` / `memory_write.py` / `memory_jobs.py`）的真正
   主题是第 16、17 章，它们的完整精讲在第 16、17 章的附录里；这里只讲
   `release.py` 和 `__main__.py` 的发布/记忆接线。
2. 完整函数代码块里不使用代表遗漏实现的省略号，也不把关键分支写成
   "此处略"。示例字符串或命令文本里出现的三个点，如果本来就是源码的
   一部分，会原样保留。
3. 代码块按当前源码核对：实现来源是 `steps/step15_release/src/minicodex/`。

## F0 · 这一章新出现的写法，先过一遍

**1. `@dataclass(frozen=True)` + `@property`。** `Deprecation.message` 是
一个不存字段、现算现给的属性——`old`/`new`/`since`/`remove_in`/`why` 是
数据，`message` 是这些数据拼出来的一句话。写成 property 而不是方法，
调用方就能用 `rule.message`（像访问字段）而不是 `rule.message()`。

**2. 元组常量的不可变性。** `SURFACES` 和 `DEPRECATIONS` 都是
`tuple[...]`。元组不可改，这比 list 更适合"在模块里写死的事实表"——一个
在运行中途被 append 的表，说明有代码在改它不该改的东西。

**3. `Path.glob` + `sorted` + `key=lambda`。** `_newest_recording` 用
`stat().st_mtime` 排序找最新文件。`glob("*.jsonl")` 返回生成器，`sorted`
按 mtime 倒序（`reverse=True`），`found[0]` 是最新的。

**4. 环境变量的存在性检查。** `if os.environ.get("MINICODEX_BASE_URL"):`
不是 `in os.environ`——get 返回空串时 falsy，两种情况（没设、设为空）都
不打印。这是"只在非默认时提及"的标准写法。

**5. `argparse` 的 `add_subparsers(dest="command")`。** 一个主 parser +
多个子命令（ask/sessions/fork/replay/rules/memory/serve-stub）。
`dest="command"` 让 `args.command` 拿到子命令名，`if args.command == ...`
分发。新手最常见的错是忘了 `dest`，于是拿不到命令名。

## F1 · `release.py`：`Surface` 与 `SURFACES`

正文 §2.2 给了 `SURFACES` 表。`Surface` 这个类本身和字段注释值得逐段看：

```python
@dataclass(frozen=True)
class Surface:
    label: str
    path: Path
    version_marker: str
    policy: str
    note: str = ""
```

`policy` 字段是四个动词之一：`migrate`（信息还在，意思没有疑问）、
`refuse`（信息没了，用默认值填坑会产生"看起来跑过"的假象）、`rewrite`
（文件可再生，整体替换）、`recreate`（它只是个缓存）。docstring 里有一段
很值得抄进自己的代码注释：

> `policy` 是列表里的一个词，`note` 是散文，而不是一个字段同时装两者。
> 第一版只有散文，想检查策略的测试不得不从一句话里把动词解析出来——
> 在测试里发明了一个类型没有的分类法。**一个值需要测试去解释的字段，
> 是一个有两个含义的字段。**

`SURFACES` 的每条记录都是一句断言。第 5 条（memory jobs）的注释是全书
"表里的每个格子都是断言"最好的例子：第一稿写的是 `sqlite user_version`，
而 `memory_jobs.py` **从来没设置过** user_version——那是对"细心的人会
怎么做"的描述，不是对"这个程序实际做什么"的描述，而表里的格子分不出
这两者。

## F2 · `release.py`：`Deprecation` 与 `apply_deprecations`

正文 §5.3 只给了 docstring。完整实现在这里：

```python
@dataclass(frozen=True)
class Deprecation:
    old: tuple[str, ...]
    new: tuple[str, ...]
    since: str
    remove_in: str
    why: str

    @property
    def message(self) -> str:
        old = " ".join(self.old)
        new = " ".join(self.new)
        return (
            f"warning: `{old}` is deprecated since {self.since} and will be removed in "
            f"{self.remove_in}; use `{new}` instead ({self.why})"
        )


DEPRECATIONS: tuple[Deprecation, ...] = (
    Deprecation(
        old=("--yes",),
        new=("--dangerously-approve-all",),
        since="0.1.0",
        remove_in="0.3.0",
        why="it reads as an answer to one question and it is an answer to all of them",
    ),
    Deprecation(
        old=("forget",),
        new=("rules", "--forget"),
        since="0.1.0",
        remove_in="0.3.0",
        why="chapter 17 gave `forget` a second meaning, and it is the louder one",
    ),
)
```

**为什么 `old`/`new` 是元组而不是字符串？** `forget` → `rules --forget`
是**一个词变成两个词**。字符串版本要存空格和引号，元组版本每个词一个
元素，`" ".join` 拼回命令行。`apply_deprecations` 遍历 argv 时逐词比对，
`len(rule.old) != 1 or token != rule.old[0]` 要求旧拼法**只有一个词**
（`--yes`、`forget` 都是单词），多词的旧拼法直接跳过——设计上就不支持
"两个词拼成一个词"的废弃，因为那种改法该走别的路。

`apply_deprecations` 完整函数：

```python
def apply_deprecations(argv: list[str]) -> tuple[list[str], list[str]]:
    out: list[str] = []
    warnings: list[str] = []
    seen: set[tuple[str, ...]] = set()
    for index, token in enumerate(argv):
        replaced = False
        for rule in DEPRECATIONS:
            if len(rule.old) != 1 or token != rule.old[0]:
                continue
            positional = not rule.old[0].startswith("-")
            if positional and index != 0:
                continue
            out.extend(rule.new)
            replaced = True
            if rule.old not in seen:
                seen.add(rule.old)
                warnings.append(rule.message)
            break
        if not replaced:
            out.append(token)
    return out, warnings
```

三个细节：

1. **`positional and index != 0`：位置参数只在第一位重写。** `forget` 是
   命令也是英语单词——`minicodex ask "how do I forget a rule"` 里的
   `forget` 不能变成 `rules --forget`，否则一个提问被改成了另一条命令。
   `--yes` 这种带 `-` 的选项在任意位置都重写。
2. **`seen` 集合去重。** 同一个旧拼法出现多次只警告一次（`--yes --yes`
   不报两遍）。`rule.old` 是元组，可哈希，正好当集合元素。
3. **`out.extend(rule.new)` 而不是 `out.append`。** `forget` 要展开成两个
   词（`rules`, `--forget`），`extend` 把元组平铺进列表。

## F3 · `release.py`：`environment_report` 完整实现

正文 §4.4 只给了 docstring，§4.5 给了输出样例。函数体：

```python
def _count(directory: Path, pattern: str) -> int:
    return len(list(directory.glob(pattern))) if directory.is_dir() else 0


def _newest_recording(root: Path) -> Path | None:
    found = sorted(
        (root / RECORDINGS_DIR).glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True
    )
    return found[0] if found else None


def environment_report(root: Path | None = None) -> str:
    root = Path(root or Path.cwd())
    lines = [
        f"minicodex {__version__}",
        f"python    {platform.python_version()} "
        f"({sys.platform}, {platform.python_implementation()})",
        f"install   {Path(__file__).resolve().parent}",
        f"cwd       {root}",
    ]

    memory = root / DEFAULT_MEMORY_DIR
    state = [
        f"{_count(root / SESSIONS_DIR, '*.jsonl')} session(s)",
        f"{_count(root / RECORDINGS_DIR, '*.jsonl')} recording(s)",
        f"memory {'present' if (memory / SUMMARY_FILE).exists() else 'none'}",
        f"approvals {'present' if (root / DEFAULT_RULES_PATH).exists() else 'none'}",
    ]
    lines.append(f"state     .minicodex: {', '.join(state)}")

    agents = root / "AGENTS.md"
    if agents.is_file():
        lines.append(f"project   AGENTS.md present ({agents.stat().st_size} bytes)")

    if os.environ.get("MINICODEX_BASE_URL"):  # only when it is not the default
        lines.append(f"base url  {os.environ['MINICODEX_BASE_URL']}")

    newest = _newest_recording(root)
    if newest is not None:
        lines.append(f"latest    {newest}")
        lines.append(f"          reproduce it with: minicodex replay {newest}")
    else:
        lines.append("latest    no recording in this directory yet")
    lines.append(f"report    {ISSUES_URL}")
    return "\n".join(lines)
```

新手最容易写错的三处：

1. **`root = Path(root or Path.cwd())`。** `root or` 处理 `None`；`Path()`
   包一层保证类型。之后所有路径都从 `root` 拼，不直接碰 `Path.cwd()`。
2. **对齐的键。** `f"minicodex {__version__}"`、`f"python    ..."` 里的
   空格是**刻意对齐**的——`install`、`cwd`、`state`、`base url`、`latest`、
   `report` 都是第一列，值从第 9 列开始。这是给人肉眼扫的表格式输出，
   键长不齐时用空格补齐。
3. **`_count` 里的 `is_dir()` 守卫。** 目录不存在时 `glob` 返回空生成器，
   `len(list(...))` 是 0，但显式守卫让"目录不在"和"目录在但没文件"两种
   情况都不出错——而且不抛 `FileNotFoundError`（新用户第一次跑，`.minicodex`
   目录可能根本不存在）。

## F4 · `version_parts`：版本序不是字符串序

正文 §5.4 给了它。代码三行，值得讲的都在 docstring 里：

```python
def version_parts(version: str) -> tuple[int, ...]:
    return tuple(int(part) for part in version.split("-")[0].split("."))
```

- **`split("-")[0]`** 丢掉预发布后缀（`0.1.0-beta.1` 只取 `0.1.0`）。
- **`split(".")`** 按点切，`int()` 每个分量——`"0.10.0"` 变成
  `(0, 10, 0)`，`"0.9.0"` 变成 `(0, 9, 0)`，元组比较 `(0, 10, 0) >
  (0, 9, 0)` 正确。字符串比较 `"0.10.0" < "0.9.0"` 是错的（`'1' < '9'`）。
- 生成器表达式 + `tuple()`，不用列表推导——`tuple(int(p) for p in ...)`
  直接构造元组。

## F5 · `__main__.py`：argparse 与废弃翻译的接线

正文 §5.3 讲了 `apply_deprecations` 在 argparse 之前。接线代码：

```python
argv = sys.argv[1:] if argv is None else list(argv)
argv, warnings = apply_deprecations(argv)
for warning in warnings:
    print(warning, file=sys.stderr)
args = parser.parse_args(argv)
```

`argv = sys.argv[1:] if argv is None else list(argv)` 这行是给测试留的口子：
`main(argv)` 接受显式参数列表，测试直接传 `["ask", "..."]` 不用动
`sys.argv`。`list(argv)` 复制一份，防止调用方传入的列表被就地改写。

`--version` 的处理在 argparse 之外：

```python
if args.version:
    print(environment_report())
    return 0
```

`--version` 不打印"0.1.0"三个字，而是打印整份 `environment_report`——
因为 bug 报告需要的正是环境（正文 §4.4/§4.5）。`args.version` 是
`store_true` 的 flag，默认 `False`。

`main` 的分发是链式 `if` 而不是 `match`。下面是完整的分发序列（每个分支
都返回 `int` 退出码；`memory` 分支的参数较多，展开写完整）：

```python
if args.command == "sessions":
    return _list_sessions(args.session_dir)

if args.command == "fork":
    return _fork(args.session, args.upto, args.session_dir)

if args.command == "replay":
    return _replay(args.recording, strict=not args.loose)

if args.command == "rules":
    return _forget_rule(args.forget) if args.forget is not None else _list_rules()

if args.command == "memory":
    return _memory(
        args.memory_dir,
        forget_all=args.forget_all,
        jobs=args.jobs,
        remember_now=args.remember_now,
        session_dir=args.session_dir,
        jobs_path=args.jobs_db,
        provider=args.provider,
        model=args.model,
    )

if args.command == "ask":
    session = Session(
        mode=args.sandbox_mode,
        policy=args.approval_policy,
        rules=RuleStore(Path(DEFAULT_RULES_PATH)),
        approver=AllowAll() if args.approve_all else CliApprover(),
    )
    try:
        configs_ok = args.mcp is None or load_config(args.mcp)
    except (OSError, ValueError, McpError) as exc:
        print(f"--mcp: {exc}", file=sys.stderr)
        return 1
    del configs_ok
    memory = None
    if args.memory:
        try:
            memory = load_memory(args.memory_dir)
        except MemoryError_ as exc:
            print(f"--memory: {exc}", file=sys.stderr)
            return 1
        if not memory:
            print(f"[{memory.describe()}; run `minicodex memory` to see where it goes]")
            memory = None
    return asyncio.run(
        _ask(
            args.question,
            provider=args.provider,
            base_url=args.base_url,
            model=args.model,
            session=session,
            context_window=args.context_window,
            resume=args.resume,
            session_dir=args.session_dir,
            mcp_config=args.mcp,
            memory=memory,
            remember=args.memory_dir if args.remember else None,
            jobs_path=args.jobs_db,
        )
    )

if args.command == "serve-stub":
    from minicodex.stub import serve

    serve(args.port)
    return 0

parser.print_help(sys.stderr)
return 2
```

每个分支返回 `int` 退出码，`if __name__ == "__main__": raise SystemExit(main())`
把返回值变成进程退出码。最后一行 `print_help(sys.stderr) + return 2` 是
正文 §4.2 的修复：没子命令时帮助走 stderr、退出 2，而不是 stdout、退出 0
（那样脚本以为成功了）。

## F6 · `__main__.py`：记忆的接线

`ask` 分支里记忆相关的新代码（完整接线）：

```python
memory = None
if args.memory:
    try:
        memory = load_memory(args.memory_dir)
    except MemoryError_ as exc:
        print(f"--memory: {exc}", file=sys.stderr)
        return 1
    if not memory:
        print(f"[{memory.describe()}; run `minicodex memory` to see where it goes]")
        memory = None
return asyncio.run(
    _ask(
        args.question,
        provider=args.provider,
        base_url=args.base_url,
        model=args.model,
        session=session,
        context_window=args.context_window,
        resume=args.resume,
        session_dir=args.session_dir,
        mcp_config=args.mcp,
        memory=memory,
        remember=args.memory_dir if args.remember else None,
        jobs_path=args.jobs_db,
    )
)
```

三个点：

1. **`--memory` 是读的开关，`--remember` 是写的开关**（两个独立 flag）。
   正文 F17-11 讲过这是两种不同的同意：读自己手写的东西和让模型写关于
   你的东西不是一回事。
2. **空记忆不是错误。** `load_memory` 成功但没内容时，打印一句"去哪儿
   看怎么开始"然后 `memory = None`——不报错，也不带着空记忆跑。
3. **`remember=args.memory_dir if args.remember else None`。** 传的是记忆
   目录路径，不是布尔值——`_ask` 里 `remember is not None` 就启动后台
   写入 pipeline（下面 F7）。

## F7 · `_ask` 里的后台写入：`writer_task` 与 `_finish_writer`

`_ask` 中启动后台记忆写入的部分：

```python
writer_task: asyncio.Task[Any] | None = None
if remember is not None:
    writer_task = asyncio.ensure_future(
        run_pipeline(
            make_model([]),
            directory=remember,
            sessions_dir=session_dir,
            jobs_path=jobs_path,
            memory=memory,
            exclude=(current.session_id,),
            headroom=lambda: llm.rate_limit.headroom() if llm.rate_limit else None,
        )
    )
```

- **`asyncio.ensure_future(...)` 而不是 `await`。** 这是"在对话进行的同时
  后台跑"的全部技术：创建一个 task 但不等它。`writer_task` 拿住句柄，
  对话结束时 `_finish_writer` 收尾。
- **`make_model([])` 给它一个空工具列表的 client。** 写入是两个模型调用、
  不需要工具；传这一轮的 schemas 会让它每次请求都为用不上的工具付费。
- **`exclude=(current.session_id,)`。** 当前会话还没结束，不算"过去的
  会话"，按名字排除——这是 F17-01 的"同一份工作在 run 结束时做要等 14
  秒"的修复（后台做，用户不等待）。
- **`headroom=lambda: ...`。** 一个函数而不是一个数：此刻还没见过任何
  rate-limit 头，pipeline 内部会在每次请求后重新询问（正文 §17 讲过
  配额规则）。

对话结束后收尾：

```python
async def _finish_writer(task: asyncio.Task[Any], *, grace: float = WRITER_GRACE_SECONDS) -> str:
    if not task.done():
        try:
            await asyncio.wait_for(asyncio.shield(task), grace)
        except asyncio.TimeoutError:
            task.cancel()
            return f"memory writer: still going after {grace:.0f}s, left for the next run"
    try:
        return (await task).describe()
    except asyncio.CancelledError:
        return "memory writer: stopped, left for the next run"
    except (MemoryWriteError, MemoryError_, OSError, ModelFailed) as exc:
        return f"memory writer: failed, nothing written ({type(exc).__name__}: {exc})"
```

逐层拆：

1. **`wait_for(shield(task), grace)` 而不是裸 `wait_for`。** 第 10 章 F10-07
   的教训在这里第二次用：裸 `wait_for` 超时会把 task **取消**，而一个
   已经写完 raw 文件的 pipeline 被取消后，从外面看和"什么都没做"一样。
   `shield` 把取消挡在外面——超时只结束"等待"，task 继续跑完。
2. **`WRITER_GRACE_SECONDS = 30.0`。** 不是 0（一回合的会话在第一次请求
   回来前就结束了，grace 0 意味着那个功能只在长会话上有效），也不是无限
   （后台化的全部意义就是不让用户等）。
3. **`(await task).describe()`** 收集 pipeline 的报告（`RunReport`）。
   `task.done()` 时直接 await 拿结果；没 done 时先 `wait_for(shield(...))`
   等 grace。
4. **`except asyncio.TimeoutError` 里 `task.cancel()` 并返回"留给下次"**。
   取消是正常结局不是错误：它 claim 的租约还在数据库里，租约过期就是
   下次运行捡起它的机制（F17-03 的"为什么是租约不是旗标"）。
5. **四个异常的宽 except 全部吞掉并返回描述。** 回答用户问题正确的一次
   运行，不会因为记笔记的工作写不了文件而变成失败运行——第 16 章
   `record_uses` 也是同一个理由往上吞一级。**注意这里没有 `except
   Exception`**：`CancelledError` 单独处理（3.10 上它是 `BaseException`
   的子类，宽 except 接不住它），其余明确的失败类型列出来。

## F8 · `__main__.py`：`_memory`、`_memory_jobs`、`_remember_now`

`memory` 子命令的三个功能对应三个函数，正文只有输出样例，函数体在这里
（记忆本身的读写逻辑在第 16/17 章附录，这里只讲 CLI 接线）。

`_memory` 的 `--forget-all` 分支：

```python
if forget_all:
    removed = []
    for name in (SUMMARY_FILE, BODY_FILE, USAGE_FILE):
        path = directory / name
        if path.is_file():
            path.unlink()
            removed.append(name)
    for name in (RAW_DIR, NOTES_DIR):
        folder = directory / name
        if folder.is_dir():
            for path in folder.glob("*.md"):
                path.unlink()
            removed.append(f"{name}/")
    if Path(jobs_path).exists():
        with JobStore(jobs_path) as store:
            store.forget_all()
        removed.append(jobs_path.name)
    print(f"deleted {', '.join(removed) if removed else 'nothing'} from {directory}")
    if (directory / ".git").is_dir():
        print(f"note: the git history in {directory / '.git'} still has every past version.")
        print(f"      remove it with: rm -rf {directory / '.git'}")
    return 0
```

- **`removed` 列表记录删了什么**，最后拼进一句话——"删了 nothing"也是
  输出，不是静默。
- **`RAW_DIR`/`NOTES_DIR` 也要清。** 第 17 章写这两个目录，`--forget-all`
  留着它们等于撒谎（"删除一切"留下了中间产物）。
- **`JobStore` 用 `with` 上下文**，事务提交在退出时。
- **git 历史只提示不动手。** 注释说了为什么：这个程序不该静默删掉它自己
  改动历史的记录——`rm -rf` 留给用户决定。

`_memory` 的展示分支：

```python
print(f"memory directory: {directory.resolve()}")
try:
    memory = load_memory(directory)
except MemoryError_ as exc:
    print(exc, file=sys.stderr)
    return 1
print(f"  {memory.describe()}")
if not memory:
    print(f"\nTo start one, write {directory}/{SUMMARY_FILE}:")
    print("  v1\n  - Run tests as `python -m pytest`.")
    print(f"and, optionally, longer sections in {directory}/{BODY_FILE}:")
    print("  v1\n\n  ## Running tests\n\n  ...")
    print("\nMemory is off unless you pass --memory.")
    return 0
counts = usage(directory)
for entry in memory.entries:
    row = counts.get(entry.entry_id, {})
    used = row.get("count", 0)
    print(f"  {entry.entry_id:<40} cited {used} time(s)  {entry.headline()[:40]}")
pending = pending_notes(directory)
if pending:
    print(f"\n  {len(pending)} proposed note(s) not merged yet, in {directory / NOTES_DIR}")
print(f"\ndelete everything with: minicodex memory --forget-all --memory-dir {directory}")
return 0
```

- **空记忆时教用户怎么写第一个文件。** 输出两行带 `v1` 标记的示例——
  不是"没有记忆"五个字，是"怎么开始"。
- **`entry.entry_id:<40` 左对齐 40 列**，`cited N time(s)` 是对齐的引用
  计数。`counts.get(entry.entry_id, {})` 拿不到就默认空 dict，`row.get`
  再默认 0——两层的防御，因为"引用计数"和"条目"可能不在同一份数据里
  （计数是第 16 章 `record_uses` 写的，条目是手写/合并的）。
- **`pending_notes` 的提示**：第 17 章"写了 note 但没合并"的状态在这里
  可见——又是"看不到的东西没法撤销"。

`_memory_jobs` 与 `_remember_now` 是同一份逻辑的两个读者：

```python
def _memory_jobs(jobs_path: Path, session_dir: Path) -> int:
    if not Path(jobs_path).exists():
        print(f"no job records at {jobs_path} (nothing has been remembered yet)")
        return 0
    with JobStore(jobs_path) as store:
        rows = store.rows()
        counts = store.counts()
        if not rows:
            print(f"no job records at {jobs_path}")
            return 0
        for row in rows[:20]:
            detail = f"  {row['detail']}" if row["detail"] else ""
            print(f"  {row['session_id']:<28} {row['state']:<8} attempt {row['attempts']}{detail}")
        print(f"\n  {', '.join(f'{k}: {v}' for k, v in sorted(counts.items()))}")
        waiting = len(pending_sessions(session_dir))
        print(f"  {waiting} session file(s) in {session_dir} are eligible")
    return 0
```

`--remember-now` 就是前台跑 `run_pipeline`（同一个函数，不是第二份实现），
区别只在"谁在等"：

```python
def _remember_now(
    directory: Path,
    *,
    session_dir: Path,
    jobs_path: Path,
    provider: str,
    model: str | None,
) -> int:
    default_url, default_model = PROVIDERS[provider]
    client = ChatCompletionsModel(
        base_url=default_url,
        model=model or default_model,
        api_key=os.environ.get("OPENAI_API_KEY") if provider == "openai" else None,
        tools=[],
    )
    print(f"memory directory: {directory.resolve()}")
    try:
        report = asyncio.run(
            run_pipeline(
                client,
                directory=directory,
                sessions_dir=session_dir,
                jobs_path=jobs_path,
            )
        )
    except (MemoryWriteError, MemoryError_, ModelFailed, OSError) as exc:
        print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    print(f"  {report.describe()}")
    for session_id, detail in report.failed:
        print(f"  failed: {session_id}: {detail}", file=sys.stderr)
    return 0
```

注意它**没有** `exclude` 和 `headroom`——注释里明说：这个命令不是会话，
没有"当前会话"要排除；用户站在这里亲自要求，配额规则只管没人要求的工作。

## F9 · 新手常见报错/坑对照表

| 症状 | 原因 | 修法 |
|---|---|---|
| `--yes` 不工作了但没警告 | `apply_deprecations` 没在 `parse_args` 之前调用 | 接线顺序：先翻译 argv，再 `parser.parse_args(argv)` |
| `minicodex ask "how do I forget a rule"` 被改成 `rules --forget` | 位置参数在任何位置都重写 | `positional and index != 0` 才跳过（只在第一位重写） |
| 同一个旧拼法警告了两次 | 没去重 | `seen: set[tuple[str, ...]]`，`rule.old not in seen` 才警告 |
| `--version` 只打印版本号 | 没调 `environment_report` | `if args.version: print(environment_report()); return 0` |
| 没子命令时退出 0、帮助上 stdout | 老行为 | `parser.print_help(sys.stderr); return 2` |
| `"0.10.0" < "0.9.0"` 判成真 | 字符串比较版本号 | `version_parts()` 转 `(0, 10, 0)` 再比 |
| `_finish_writer` 把后台任务取消了 | 裸 `wait_for` | `wait_for(shield(task), grace)`，超时只结束等待 |
| `--forget-all` 删了记忆但留下 `raw/`、`notes/` | 只删了三个主文件 | 循环清 `RAW_DIR`/`NOTES_DIR` 下的 `*.md`，再清 job 库 |
| 后台写入把当前会话也算进去了 | 没排除 | `exclude=(current.session_id,)` |
| 记忆写入失败导致整个 run 失败 | 异常往上抛到 `main` | `_finish_writer` 里明确的 `except` 列表全部吞掉并返回描述 |
| `environment_report` 在 `.minicodex` 不存在时抛异常 | `glob` 前没守卫 | `_count` 里 `if directory.is_dir()` |
