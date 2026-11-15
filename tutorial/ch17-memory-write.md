# 第 17 章 · 记忆（二）：让它自己写，并且学会忘

> **代码**：`steps/step17_memory_write/`
> **分支**：`feat/memory-write`
> **产出**：上一章的记忆是人手写的；这一章让程序自己从过去的会话里**写**记忆，并且学会**删**。两个新模块：`memory_write.py`（抽取、合并、脱敏、遗忘、单写者）和 `memory_jobs.py`（本书第一个数据库：哪些会话已经变成记忆了、谁在处理）。命令行多一个 `--remember` 开关，`minicodex memory` 多 `--jobs` 和 `--remember-now`
> **前置**：做完第 16 章。本章 66 个测试全部不联网。探针 `probe_memory_write.py` 九段里 `race`、`forget` 两段不联网，其余要真的请求 OpenAI；这次改写时手边没有 key，那几段的数字是第一次写这一章时（2026-08-15 至 16）量的，正文每处都照原样保留并注明。不联网的输出这次在 Linux 上重跑过，和原来不一样的地方都说了
> **阅读顺序在第 15 章之前，编号在其之后。**
> **这一章可以分四次读**：§1–§4 是结构、时机和那个数据库；§5–§7 是"记什么、不记什么"（抽取、脱敏、额度）；§8–§12 是"谁能写、怎么合、怎么忘、怎么让用户看见"；§13 往后是结果、验证和收工

---

## §0 开工前的准备

### 0.1 本章会遇到的新概念

- **抽取（extract）**：读一次已经结束的会话，判断里面有没有值得长期记住的东西，有就写成几句话。这一章的"第一段"。
- **合并（merge / consolidate）**：把好几次抽取的结果、模型提议的笔记、用户手改的内容，合进第 16 章那两个文件里。"第二段"。
- **后台任务**：和用户的问题**同时**跑、用户不用等它的工作。在 Python 里是一个 `asyncio.Task`。
- **任务表（job table）**：一张表，每一行是一次会话，记着它的状态——等着处理（`pending`）、有人在处理（`claimed`）、处理完了（`done`）、放弃了（`failed`）。
- **认领（claim）**：一个进程说"这几行归我了"。关键是**两个进程不能认领到同一行**。
- **租约（lease）**：认领带一个过期时间。进程如果死了，过期之后别人可以重新认领。
- **退避（backoff）**：失败了不马上重试，等一会儿，而且每次等更久。
- **SQLite**：一个装在一个文件里的数据库，Python 自带（`import sqlite3`）。它能保证"同一时间只有一个进程在改这张表"。
- **事务（transaction）**：一组数据库操作，要么全部生效，要么全部不生效；在它进行时别人看不到一半的状态。
- **脱敏（redaction）**：把看起来像密钥、令牌的字符串换成 `<redacted>`。
- **单写者（single writer）**：一个文件同一时间只允许一个进程写。第 7 章用一个"锁文件"做到过。
- **`git diff`**：比较"现在的文件"和"上次提交的版本"差在哪。这一章拿它来回答"用户手改了什么"。
- **遗忘（forgetting / pruning）**：记忆不能只增不减；按某种规则把条目删掉。

### 0.2 本章会遇到的新 Python 写法

| 写法 | 意思 |
|---|---|
| `sqlite3.connect(path, isolation_level=None)` | 打开一个数据库文件。`isolation_level=None` 表示"别替我自动开事务"，事务由我自己写 `BEGIN`/`COMMIT` |
| `db.execute("SELECT ... WHERE x = ?", (value,))` | 执行一条 SQL。`?` 是占位符，值放在后面的元组里——**不要**把值拼进字符串（那会让数据变成 SQL） |
| `cursor.fetchall()` / `fetchone()` | 取出查询结果的全部行 / 第一行 |
| `db.row_factory = sqlite3.Row` | 让每一行可以按列名取：`row["state"]` |
| `cursor.rowcount` | 这条语句影响了几行 |
| `with JobStore(path) as store:` | 用完自动关掉。类上写了 `__enter__` 和 `__exit__` 就能这么用 |
| `asyncio.ensure_future(coro)` | 把一个协程变成一个**马上开始在后台跑**的任务 |
| `asyncio.wait_for(x, timeout)` | 最多等 `timeout` 秒，超时抛 `TimeoutError` |
| `asyncio.shield(task)` | 等它，但超时的时候**别取消它**（`wait_for` 默认会取消） |
| `task.cancel()` / `task.done()` | 取消一个任务 / 它结束了没有 |
| `subprocess.run([...], capture_output=True, text=True, timeout=30)` | 启动一个外部程序（这一章是 `git`），等它结束，拿回它的输出 |
| `re.compile(..., re.S)` | 同 `re.DOTALL`：`.` 也匹配换行 |
| `pattern.subn(repl, text)` | 替换，并且告诉你替换了几处：返回 `(新文字, 次数)` |
| `os.open(path, os.O_CREAT \| os.O_EXCL \| os.O_WRONLY)` | 创建一个文件，**已经存在就失败**——第 7 章的锁就是这么做的 |
| `callable(x)` | `x` 能不能被调用（是不是一个函数） |
| `getattr(obj, name)` | 按名字取属性：`getattr(stage1, "preferences")` 就是 `stage1.preferences` |
| `dict.fromkeys(xs)` | 用 `xs` 当键造一个字典；常用来"去重并保持顺序" |
| `**extra` 写在函数调用里 | 把一个字典展开成关键字参数 |

### 0.3 开分支

```bash
git switch main
git pull
git switch -c feat/memory-write
```

### 0.4 读这一章要注意的一件事

这一章是在第 16 章**重写之前**写的，后来第 16 章重写了两次（一次对照 codex，一次是给新手改写），每次都回头改过这一章。所以正文里会提到"第 16 章重测之后"这样的话。有几个第 16 章的数字后来又变得不可靠了（第 16 章 §5.3 的行号 bug），用到它们的地方都标了。

---

## §1 这一章要做出来的东西

### 1.1 先看见它动

（这一节和后面大多数的模型输出，都是第一次写这一章时在 Windows 上实测的，2026-08-15 至 16，`gpt-4o-mini`。这次改写手边没有 key，没有重量。）

一个空目录，两个文件，什么记忆都没有。第一次运行，多带一个 `--remember`：

```
$ minicodex ask "Add a subtract function to calc.py and run the tests.
    In this repository we always run tests as \`python -m pytest\`,
    never a bare \`pytest\`." --provider openai --sandbox-mode workspace-write --yes --remember

I added the `subtract()` function to `calc.py` and ran the tests. All tests passed
successfully. If you need anything else, feel free to ask!

[x] add subtract() function to calc.py
[x] run tests using python -m pytest

[gpt-4o-mini | completed after 7 turn(s)]
[plan: 2/2 step(s) completed, 3 update(s)]
[sandbox_mode=workspace-write, approval_policy=on-request]
[memory writer: nothing to do]
```

**"memory writer: nothing to do"** ——第一次运行时没有任何"上一次会话"可读，
所以它什么都没做。这就是这一章最重要的一个结构选择：
**记忆不是在这次会话结束时生成的，是在下一次会话开始时生成的。**

于是第二次运行：

```
$ minicodex ask "What does calc.py contain now?" --provider openai
    --sandbox-mode workspace-write --yes --remember --memory

It defines three functions: `add`, `subtract`, and `multiply`.

[gpt-4o-mini | completed after 2 turn(s)]
[memory writer: 1 session(s) read, 2 entr(ies), nothing forgotten in 5.7s]
```

那一行就是全部：它在**这次任务跑着的同时**，把上一次会话读成了两条记忆。
去看磁盘：

```
$ cat ~/.minicodex/memories/memory_summary.md
v1

- We always run tests as `python -m pytest`.

$ cat ~/.minicodex/memories/MEMORY.md
v1

## Test Command

In this repository, we always run tests as `python -m pytest`, never a bare
`pytest`. To avoid omitting the correct test command, always use
`python -m pytest` instead of just `pytest`.

## calc.py Functions

The `calc.py` file contains basic arithmetic functions, including `add`,
`subtract`, and `multiply`.

$ git -C ~/.minicodex/memories log --oneline
704d979 memory: 1 session(s), 0 note(s)
```

第一条是对的：用户在第一次会话里顺口说的那句约定，被记下来了，
下一次开新会话它就知道。

**第二条是垃圾。** "calc.py 里有 add、subtract、multiply"——
任何人打开这个仓库三秒钟就知道，而它现在会跟着每一个请求发出去，永远。

这一章一半的篇幅在讲第二条为什么会出现，以及**我把它从三条压到一条用了什么办法、
剩下的那一条为什么还在**。

### 1.2 这一章要回答的那个问题

第 16 章证明了**读**记忆有用，而且用的是手写的、完美的记忆：

```
  TOTAL                4/12         12/12      (gpt-4o-mini)
```

手写记忆是**上界**。第 17 章的全部问题是一句话：

> **自动生成的记忆，能追上手写的多少？**

答案在 §13，先给结论：

```
  task                 off          hand     generated
  TOTAL               7/18         17/18         15/18
```

**在这个管线有材料可讲的每一个任务上，自动生成的记忆和手写的一样好。**
差的三分全在一个任务上，而那个任务差的原因比总分值钱——见 §13.2。

### 1.3 三层，成本差两个数量级

`PLAN.md` §4.5.2 把记忆切成三层，这一章是最后一层，也是唯一贵的那层：

| 层 | 谁写 | 成本 | 在哪一章 |
|---|---|---|---|
| `AGENTS.md` | 人 | 四十行代码，零次模型调用 | 第 13 章 |
| `~/.minicodex/memories/` 手写 | 人 | 一个目录、两个 md | 第 16 章 |
| 抽取与遗忘 | 后台任务 | 两次模型调用、一个数据库、一把锁、两份 prompt | **本章** |

按这个顺序讲，是为了让"绝大多数项目只需要第一层"这句话有分量。
本章写完之后你会看到：光是让一个自动管线**不生产垃圾**，就花掉了整整一节的测量。

---

## §2 结构：两段、一个中间产物、一个单写者

### 2.1 目录

```
~/.minicodex/                    # ← codex 的 codex_home，本程序的对应物
├── memory_jobs.sqlite3          # 哪些会话读过了、谁在读（本章新增）
├── memory_merge.lock            # 合并时的单写者锁（本章新增）
└── memories/                    # ← 这个目录是一个 git 仓库
    ├── memory_summary.md        # 第 16 章：常驻
    ├── MEMORY.md                # 第 16 章：正文
    ├── usage.json               # 第 16 章：引用计数（本章加一个字段）
    ├── raw/<session id>.md      # 第一段的输出，临时（本章新增）
    └── notes/<ts>-<pid>-N.md    # 模型能写的唯一东西（本章新增）

.minicodex/                      # ← 仍然在仓库里：会话属于这个 checkout
└── sessions/                    # 第 7 章的 rollout，本章的原料
```

**两个文件不在 `memories/` 里，这是有理由的。** 记忆目录是一个 git 仓库
（§9 会说为什么），而 sqlite 数据库是一个**每次操作都重写的二进制文件**。
把它放进那个仓库，等于每次 commit 都带一个没人能 diff 的 blob，
而"能 diff"正是这个仓库存在的全部理由。

**而这三样东西都在 `~/.minicodex/` 下、不在仓库里，是这一章改过的（F17-12）。**
第 16 章原本把记忆放在项目内的 `.minicodex/memories/`，那一版是没查源码就定的；
codex 的记忆根一直是 `codex_home.join("memories")`
（`memories/write/src/lib.rs:116-117`），全局、跨仓库。第 16 章重写时搬了家，
本章拥有的锁和任务库**必须跟着搬**——理由不是整洁：

> 一把留在仓库里的 `MERGE_LOCK`，守着一份现在已经**全局共享**的记忆，
> 等于每个 checkout 一把锁，**它们之间毫无互斥**。
> 这正是这把锁存在要防的那个竞态，被锁自己的位置抵消掉了。

任务库同理：它记的是"哪些**会话**已经变成记忆了"，而记忆现在只有一份，
所以一个 per-repository 的任务表会让同一条会话**每个 checkout 抽取一次**。

会话目录 `sessions/` 反而**没有**搬，而且不该搬：一次会话是发生在某个 checkout
里的事，它是本章的原料，不是本章的产物。搬家的判据不是"新加的东西一起走"，
是"这东西描述的是那份全局记忆，还是这个仓库"。

### 2.2 为什么是两段

codex 把写路径分成 `phase1.rs` 和 `phase2.rs`，我照着做，但理由要自己说：

| | 第一段（抽取） | 第二段（合并） |
|---|---|---|
| 输入 | **一次**会话的历史 | **全部**待合并材料 + 现在的记忆 + 用户手改的 diff |
| 输出 | 三段式的候选条目 | 两个文件的完整新内容 |
| 可以并行吗 | 可以，互不相干 | 不行，只有一个记忆 |
| 允许什么都不产出吗 | **应该**什么都不产出 | 不允许（那是把记忆删空） |
| 失败了怎么办 | 这一条会话退回队列 | 整次合并作废，抽取的结果还在 |

四行都不一样，所以它们是两个函数、两个 prompt、两个失败模式。
把它们合成一个"读会话、更新记忆"的调用，就等于每一条会话都要重读一次全部记忆、
拿一把全局锁、然后串行——而抽取本来是这个管线里唯一能并行的部分。

### 2.3 中间产物必须自己说自己是中间产物

两段之间落一个文件 `raw/<session id>.md`。codex 的对应文件在 prompt 里被明写为
"Temporary file: … Input for Phase 2"，我抄了这条，理由不是整洁：

```python
def render_stage1(stage1: Stage1) -> str:
    """The `raw/<session>.md` file, which says what it is in its first lines.

    The warning is not decoration.  codex's equivalent carries the same one --
    "Temporary file: … Input for Phase 2" -- and the fault it prevents is a
    slow one: a file in a memory directory that looks like memory, and that
    somebody eventually edits, or greps, or copies into a bug report.
    """
    lines = [
        FORMAT_VERSION,
        "",
        f"<!-- Temporary file: stage-1 output for session {stage1.session_id or '?'}.",
        "     Input for stage 2. Deleted once it has been merged.",
        "     Nothing reads this as memory; edit MEMORY.md instead. -->",
        "",
    ]
```

**一个不说明自己是中间产物的中间产物，迟早会有人去维护它。**

### 2.4 这一章唯一的抽象决策：抄一份锁，不抽 `locks.py`

清单（§4.5.5）写着：

> 这是 Ch07 的 append-only 和 Ch08 的单写者规则**第三次**出现——
> 按三次法则，这一次才该把它抽出来命名。

我数了一遍，然后**没有抽**。理由是数出来的：

**"单写者"这个概念出现了三次，但那段代码只出现了两次。**

- 第 7 章：`RolloutWriter._acquire_lock`，`O_EXCL` 创建锁文件、写 pid、报错带 pid。
- 第 8 章：调度器按资源排他——那是**另一套机制**（内存里的 footprint 比较），
  一行代码都不共享。
- 第 17 章：合并锁，和第 7 章那段几乎一样，十二行。

三次法则说的是"第一次写死，第二次容忍复制粘贴（留 TODO），第三次才抽象"，
而这是**第二次**。于是：

```python
# The single-writer lock for stage 2.  Outside the memory directory, next to
# the jobs database, for the same reason: the memory directory is a git
# repository and its contents are the material being diffed.
#
# This is the *second* implementation of chapter 7's `O_EXCL` lock file and it
# is a deliberate copy, not an extraction.  The rule of three says the second
# occurrence is copied with a note pointing at the first; a third site --
# something else in this program needing "one process at a time, and say whose
# pid it is" -- is what would justify a `locks.py`.  Two sites of twelve lines
# do not.
MERGE_LOCK = MINICODEX_HOME / "memory_merge.lock"
```

这条值得单独说，因为它是插曲 B 那条教训（FB-03：为打断循环而引入的接口
只有一个实现，纯属噪音）的正面版本：

> **"这个概念出现了三次"和"这段代码出现了三次"是两件事，只有后者能证明抽象。**
> 概念重复的代价是一句注释；代码重复的代价才是维护两份。

清单说该抽，测量说该抄。**照抄清单就是不做判断。**

---

### 2.5 代码：一个家目录，和写路径的几个常量

第 16 章把记忆目录写成了完整路径。这一章要在它**旁边**再放两样东西（锁和任务库），"旁边"只有在"上一层"有名字之后才说得出来。`src/minicodex/memory.py` 里改成：

```python
# This program's equivalent of codex's `codex_home` -- the one directory
# everything durable about *this user*, across every repository, lives under.
# Chapter 16 wrote the memory path out in full; this chapter needs the parent
# to have a name, because the write path adds two more things beside the
# memory directory rather than inside it (`memory_write.MERGE_LOCK`,
# `memory_jobs.DEFAULT_JOBS_PATH`) and "beside" is only sayable once "the
# parent" is. codex has exactly this shape: `memory_root()` is
# `codex_home.join("memories")` (`memories/write/src/lib.rs:116-117`, and the
# same function again on the read side at `memories/read/src/lib.rs:13-15`),
# while the job and lease rows live in their own SQLite database elsewhere
# under the same home (`state/memory_migrations/0001_memories.sql`), never
# inside the directory the model is shown.
MINICODEX_HOME = Path.home() / ".minicodex"

# Global, under the user's own home directory, on purpose: a memory is a fact
# about *this person* working in many repositories, not a fact pinned to one
# checkout. The first version of this module put it at `.minicodex/memories`,
# inside the repository, and that was simply wrong -- not a defensible
# simplification, a design nobody checked (F16-10). Moving it here is why
# `read_file` needed a second boundary (`paths.resolve`'s `extra_roots`,
# `composition.local_tools`' `extra_read_roots`): the memory directory is not
# under `sandbox_root` any more, and it still has to be reachable.
DEFAULT_MEMORY_DIR = MINICODEX_HOME / "memories"
```

> `MINICODEX_HOME` 是"这个用户、跨所有仓库的东西放在哪"。记忆在它下面的 `memories/`，锁和任务库也在它下面，但**不在** `memories/` 里（§2.1 说了为什么）。

新建 `src/minicodex/memory_write.py`。开头是模块说明、导入和常量：

```python
"""Memory, write side: extract, merge, and forget.

Chapter 16 read a memory a person had written.  This module is what writes one,
and it is two stages with a temporary file between them, because they are two
different jobs:

* **Stage 1, per session.**  One finished conversation in, at most a handful of
  durable statements out.  Cheap, parallelisable, and allowed -- encouraged --
  to produce nothing at all.  codex runs its equivalent eight at a time
  (`CONCURRENCY_LIMIT: usize = 8`, `memories/write/src/lib.rs:81`); this one
  runs them one after another, because two is the batch size and a pool for
  two items is a pool for nobody.
* **Stage 2, per memory.**  Every pending stage-1 output, plus whatever the
  model proposed during a session, plus whatever the *user* edited by hand,
  merged into the two files chapter 16 reads.  Runs under a lock, once.

The seam between them is `raw/<session id>.md`, and it says in its own first
lines that it is temporary.  codex writes the same kind of file with the same
warning -- "Temporary file: merged raw memories from Phase 1. Input for Phase
2." (`memories/write/templates/memories/consolidation.md:29`) -- and the
reason is not tidiness: an intermediate product that does not say it is
intermediate is one somebody starts maintaining.

Three rules that shape everything below.

**The model never writes the memory files.**  It writes stage-1 output, it can
append a note, and that is the whole of its access.  Both of those are
*proposals*; this module is the only thing that opens `MEMORY.md` for writing,
it validates before it does, and it holds a lock while it does.  Chapter 7
refused two writers on a rollout and chapter 8 refused two concurrent writers
of a file; this is the same rule arriving a third time, and F17-09 is what it
looks like when it is missing.

**Forgetting is a feature, not a maintenance script.**  A memory that only
grows is one nobody dares read and nobody dares delete.  Entries leave by
citation count first (chapter 16's `usage.json`), recency second, and age only
as a tie-breaker -- the nine-month-old entry consulted every week is the one a
by-age rule deletes first (F17-07).

**Nothing here is on by default.**  `--remember` is a flag, the directory is
somewhere a user can see, and one command deletes all of it (F17-11).
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from minicodex.agent_types import ToolSet
from minicodex.clip import clip
from minicodex.history import (
    AssistantMessage,
    ToolResult,
    UserMessage,
)
from minicodex.memory import (
    BODY_FILE,
    FORMAT_VERSION,
    MINICODEX_HOME,
    SUMMARY_FILE,
    SUMMARY_TOKEN_BUDGET,
    USAGE_FILE,
    Entry,
    Memory,
    overflows,
    parse_entries,
)
from minicodex.rollout import Rollout
from minicodex.tokens import estimate_messages
from minicodex.tool_errors import tool_error

# Stage-1 output, one file per session.  Inside the memory directory rather
# than beside the sessions, because it is memory-shaped material and because
# `--forget-all` has to be able to find it: a user who deletes their memory and
# leaves a directory of extracted preferences behind has not deleted their
# memory.
RAW_DIR = "raw"

# What a *model* may add, and the only thing it may add.  codex calls the same
# directory `extensions/ad_hoc/notes/` and its read-path prompt states the rule
# in as many words: propose, never edit.  One file per note, named by time and
# pid, so two writers cannot collide and nothing is ever overwritten -- chapter
# 7's append-only, applied to a directory instead of to a file.
NOTES_DIR = "notes"

# The single-writer lock for stage 2.  Outside the memory directory, next to
# the jobs database, for the same reason: the memory directory is a git
# repository and its contents are the material being diffed.
#
# Under `MINICODEX_HOME` rather than under the repository, and that moved in
# this chapter's rewrite along with the memory directory itself (F17-12).  It
# has to: the lock guards writes to `~/.minicodex/memories`, which is one
# directory shared by every checkout on the machine, so a per-repository lock
# would let two repositories merge the same memory at the same time -- the
# exact race the lock exists to prevent, reintroduced by the lock being in the
# wrong place.  codex keeps the same separation for the same reason: the
# memory root is global (`memories/write/src/lib.rs:116-117`) and its job and
# lease state is global too, in its own database
# (`state/memory_migrations/0001_memories.sql`), never inside the directory
# the model reads.
#
# This is the *second* implementation of chapter 7's `O_EXCL` lock file and it
# is a deliberate copy, not an extraction.  The rule of three says the second
# occurrence is copied with a note pointing at the first; a third site --
# something else in this program needing "one process at a time, and say whose
# pid it is" -- is what would justify a `locks.py`.  Two sites of twelve lines
# do not.
MERGE_LOCK = MINICODEX_HOME / "memory_merge.lock"

# How much of one session goes to the extractor.  Sessions are long, the useful
# part of them is not, and the alternative to a bound here is chapter 6 all
# over again in a place with no compaction loop.
TRANSCRIPT_TOKEN_BUDGET = 6000

# How much of one tool result survives into the transcript.  Chapter 2's clip,
# at chapter 2's shape (head and tail): the beginning of a stack trace is where
# the failure is named and the end is where the exit code is.
TOOL_RESULT_CHARS = 800

# How many entries the body file may hold, and how long an uncited entry lives.
# Both are ceilings rather than targets -- a memory of eight good sections
# never approaches either.
MAX_ENTRIES = 40
UNUSED_DAYS = 30.0

# Below this much remaining quota, the background writer does not run at all.
# codex's own number and its own default: `DEFAULT_MEMORIES_MIN_RATE_LIMIT_
# REMAINING_PERCENT: i64 = 25` (`config/src/types.rs:49`), configurable as
# `memories.min_rate_limit_remaining_percent` (`config/src/types.rs:314,333`)
# and checked before the pipeline runs at all -- `guard::rate_limits_ok`
# (`memories/write/src/guard.rs:9,38`), which reads it, asks the backend for a
# rate-limit snapshot, and skips the whole run if the remaining capacity is
# under it.  The general rule it encodes is worth more than the number:
# **background work gives way to foreground work**, and the failure it prevents
# is the one where a user's actual task fails because the program was busy
# writing notes to itself.
MIN_RATE_LIMIT_REMAINING_PERCENT = 25.0


class MemoryWriteError(RuntimeError):
    """The write path refused to do something."""
```

> - 模块说明里那三条规则是整章的骨架：**模型永远不直接写记忆文件**（它只能提案，§8）；**遗忘是功能，不是维护脚本**（§10）；**默认什么都不开**（§12）。
> - **`RAW_DIR`**、**`NOTES_DIR`**：记忆目录里的两个子目录。前者放第一段的输出，后者放模型提议的笔记。
> - **`MERGE_LOCK`**：第二段的单写者锁，在 `MINICODEX_HOME` 下、记忆目录外。注释里第二段讲了为什么它必须跟着记忆一起搬到家目录：一份全局共享的记忆，配一把每个仓库各一份的锁，等于没有锁。第三段是 §2.4 那个"抄一份，不抽象"的决定。
> - **`TRANSCRIPT_TOKEN_BUDGET`**、**`TOOL_RESULT_CHARS`**：一次会话交给抽取器之前最多多大、每条工具结果最多留多少字。会话可能很长，有用的部分不长。
> - **`MAX_ENTRIES`**、**`UNUSED_DAYS`**：记忆最多几节、一条没被引用过的条目最多活多久（§10）。
> - **`MIN_RATE_LIMIT_REMAINING_PERCENT`**：服务商剩余额度低于 25% 时，后台写入一次都不跑（§7）。这是 codex 的数字。
> - `MemoryWriteError`：写路径拒绝做某件事时抛的异常。

`render_stage1`（§2.3 里那个"说自己是临时文件"的函数）完整的样子在 §5.9。

## §3 F17-01：把它挪到下一次会话的开头

### 3.1 先量它有多贵

清单：

> F17-01 记忆在主循环里同步生成，用户每次收工都要干等十几秒

"十几秒"是个猜测，先量：

```
$ uv run python probe_memory_write.py time --limit 4

  stage 1  20260815T093924-11896    2.8s  1 preference(s), 1 fact(s), 1 failure(s)
  stage 1  20260815T093933-38560    2.4s  1 preference(s), 1 fact(s), 1 failure(s)
  stage 1  20260815T093939-5596     2.6s  1 preference(s), 1 fact(s), 1 failure(s)
  stage 1  20260815T094017-5596     2.6s  1 preference(s), 1 fact(s), 1 failure(s)
  stage 2  merge of 4               5.0s
```

**15.4 秒。** 四条会话。一个刚拿到答案、正准备敲下一条命令的人，
要盯着一行"正在整理记忆…"看十五秒，为了一件他没要求的事。

而且这个数字只会长：会话越多、记忆越大，两段都变慢。

### 3.2 三个位置，选一个

| 什么时候跑 | 问题 |
|---|---|
| 这次会话结束时 | 上面那 15.4 秒，用户在等 |
| 一个常驻后台进程 | 一个 CLI 工具凭空多出一个 daemon，还要管它的生命周期、崩溃、日志 |
| **下一次会话开始时，和第一个请求并行** | ✅ 代价是记忆永远晚一次会话 |

codex 选第三个（`max_rollouts_per_startup = 2`），我也选第三个。
"晚一次会话"这个代价必须说清楚：**你这次告诉它的约定，这次不生效。**
`§1.1` 那两次运行就是这个代价的样子。

```python
    # Started here, before the first request, and left to run beside it.  This
    # is F17-01's whole fix: the same work at the *end* of a run is fourteen
    # measured seconds between the answer and the user's prompt coming back,
    # for something the user did not ask for.  Beside the run it costs nothing
    # anybody waits for, and what it consumes is the *previous* sessions --
    # this one is not finished and is excluded by name.
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

`make_model([])` ——**空工具表**。写路径是两次没有工具的模型调用，
把这次运行的十几个 schema 塞给它，等于每次抽取都为一堆它不会用的工具付钱。

### 3.3 跑完了怎么办，没跑完怎么办

前台先结束怎么办？第一版直接 `task.cancel()`，然后想明白了一件事：
**这正是租约存在的理由**。

```python
async def _finish_writer(task: asyncio.Task[Any], *, grace: float = WRITER_GRACE_SECONDS) -> str:
    """Collect the background writer's report, or leave it for the next run.

    `wait_for(shield(...))` rather than a bare `wait_for`, which is chapter
    10's measured lesson (F10-07) in a second place: a bare one cancels the
    task it is waiting on, and a cancelled pipeline that has already written
    its raw files would look, from here, exactly like one that did nothing.

    Cancelling is a normal outcome and not an error.  The claim it took is
    still in the database with a lease on it, and the lease expiring is how the
    next run picks the work up -- which is the answer to "why a lease and not a
    flag" (F17-03).  Nothing needs undoing, because the only write that ever
    happens is the last thing the pipeline does.
    """
```

三件事在这里对齐了：

1. **`wait_for(shield(...))`** ——第 10 章 F10-07 买来的教训第二次用上：
   裸 `wait_for` 会取消它等的那个任务，于是"超时"静默地变成"什么都没发生"。
2. **取消是正常结局，不是错误。** 唯一的写操作是管线的最后一步，
   取消在它之前发生就等于没发生。
3. **被取消的认领由租约收回**，不需要任何回滚逻辑。

前台**失败**时呢？零宽限期，直接取消：

```python
        # No grace at all on this path.  The foreground just failed, and the
        # most likely reason -- a 429, a dead provider, a bad key -- is one the
        # background writer is about to hit twice more, using the same quota,
        # on behalf of nobody.
```

### 3.4 每次最多两条

```python
# How many sessions one startup will consume.  codex ships the same number as
# a configurable default -- `DEFAULT_MEMORIES_MAX_ROLLOUTS_PER_STARTUP: usize
# = 2` (`config/src/types.rs:46`), surfaced as `memories.max_rollouts_per_
# startup` (`config/src/types.rs:309,331`) and read into its Phase-1 claim as
# `max_claimed` -- and the number matters less than its existence: a user who
# has not run the writer for a month has ninety sessions waiting, and "catch up
# on all of them" is a program that appears to hang on the day it is switched
# on.
MAX_PER_STARTUP = 2
```

**上限的价值不在那个数字，在它存在。** 一个攒了三个月会话的用户，
第一次打开这个开关，会看到一个"正在追赶 270 条历史"的程序——
而他只想问一个问题。

---

### 3.5 代码：在 `_ask` 里启动它，在答案之后收它

`_ask` 里，在 `agent.run(question)` 之前：

```python
# Started here, before the first request, and left to run beside it.  This
# is F17-01's whole fix: the same work at the *end* of a run is fourteen
# measured seconds between the answer and the user's prompt coming back,
# for something the user did not ask for.  Beside the run it costs nothing
# anybody waits for, and what it consumes is the *previous* sessions --
# this one is not finished and is excluded by name.
#
# Its own client, built with an empty tool list: the writer is two model
# calls with no tools, and handing it this run's schemas would pay for
# them on every request it makes for nothing.
writer_task: asyncio.Task[Any] | None = None
if remember is not None:
    writer_task = asyncio.ensure_future(
        run_pipeline(
            make_model([]),
            directory=remember,
            sessions_dir=session_dir,
            jobs_path=jobs_path,
            exclude=(current.session_id,),
            # A question, not a number: nothing has seen a rate-limit
            # header yet at this line.  See `run_pipeline`.
            headroom=lambda: llm.rate_limit.headroom() if llm.rate_limit else None,
        )
    )
```

> - **`asyncio.ensure_future(run_pipeline(...))`**：§0.2。这一行之后，写入管线就在后台开始跑了；下面的 `agent.run(question)` 不等它。两者交替使用同一个事件循环——谁在等网络，谁就让出来。
> - **`exclude=(current.session_id,)`**：这次会话还没结束（问题都还没回答），不能拿去抽取。
> - **`headroom=lambda: ...`**：§7.4 那个"为什么是函数而不是数"。
> - `run_pipeline` 本身在 §13.4。

前台失败的那条路上（`except ModelFailed`）：

```python
        if writer_task is not None:
            print(f"[{await _finish_writer(writer_task, grace=0.0)}]", file=sys.stderr)
```

正常结束时，打印完那几行状态之后：

```python
    if writer_task is not None:
        print(f"[{await _finish_writer(writer_task)}]")
```

`_finish_writer` 和它的宽限期：

```python
# How long the run waits for the background writer once the answer is on
# screen.  Not zero -- a one-turn session is over before the writer's first
# request comes back, and a feature that only works on long sessions is a
# feature nobody can predict.  Not unbounded either, for the reason the writer
# is in the background at all.
WRITER_GRACE_SECONDS = 30.0


async def _finish_writer(task: asyncio.Task[Any], *, grace: float = WRITER_GRACE_SECONDS) -> str:
    """Collect the background writer's report, or leave it for the next run.

    `wait_for(shield(...))` rather than a bare `wait_for`, which is chapter
    10's measured lesson (F10-07) in a second place: a bare one cancels the
    task it is waiting on, and a cancelled pipeline that has already written
    its raw files would look, from here, exactly like one that did nothing.

    Cancelling is a normal outcome and not an error.  The claim it took is
    still in the database with a lease on it, and the lease expiring is how the
    next run picks the work up -- which is the answer to "why a lease and not a
    flag" (F17-03).  Nothing needs undoing, because the only write that ever
    happens is the last thing the pipeline does.
    """
    if not task.done():
        try:
            await asyncio.wait_for(asyncio.shield(task), grace)
        except TimeoutError:
            task.cancel()
            return f"memory writer: still going after {grace:.0f}s, left for the next run"
        except Exception:
            # The writer failed while it was being waited for. `wait_for`
            # re-raises the task's own exception, and the first version let
            # it out of this function from here, before the handler below
            # was ever reached.
            pass
    try:
        return (await task).describe()
    except asyncio.CancelledError:
        return "memory writer: stopped, left for the next run"
    except Exception as exc:
        # Never fatal.  A run that answered the user's question correctly does
        # not become a failed run because a note-taking job could not write a
        # file -- chapter 16's `record_uses` swallows for the same reason, one
        # size up.
        #
        # `Exception`, and the first version listed four types instead:
        # `MemoryWriteError`, `MemoryError_`, `OSError`, `ModelFailed`. The
        # last of those is what the *agent loop* raises after chapter 12's
        # retries; the writer has no loop, so a provider error reaches here as
        # the client's own `ModelHTTPError` or an `httpx` error, which were not
        # on the list. A 500 during the merge printed the user's answer and
        # then a traceback, exit code 1 -- the exact thing this comment says
        # cannot happen. Found when the chapter was rewritten.
        return f"memory writer: failed, nothing written ({type(exc).__name__}: {exc})"
```

> - **`WRITER_GRACE_SECONDS = 30.0`**：答案打出来之后，最多再等写入 30 秒。不是 0——一次一轮就结束的会话，写入的第一个请求还没回来；也不是无限——那就回到了"用户在等"。
> - **`asyncio.wait_for(asyncio.shield(task), grace)`**：最多等 `grace` 秒；超时的时候，`shield` 保证 `wait_for` 不会顺手把写入任务取消掉（第 10 章 F10-07）。然后**我们自己**决定取消它：`task.cancel()`。被取消的认领由租约在 5 分钟后收回（§4.4），什么都不用回滚。
> - **`return (await task).describe()`**：任务结束了，取它的报告，变成一行字。
> - 最后两个 `except`：§3.6。

### 3.6 改写时发现：合并时服务商一报错，用户就看到一段 traceback

`_finish_writer` 的注释第一版就写着："**绝不致命**。一次正确回答了用户问题的运行，不能因为一个记笔记的任务写不了文件就变成失败的运行。"

第一版是这么接的：

```python
    except (MemoryWriteError, MemoryError_, OSError, ModelFailed) as exc:
        return f"memory writer: failed, nothing written ({type(exc).__name__}: {exc})"
```

四种异常。看最后一个 `ModelFailed`——那是第 12 章 **Agent 循环**在重试用完之后抛的。而写入管线**没有循环**：它直接调用客户端的 `stream()`（§5.9 的 `_ask`）。所以服务商在合并时报个 500，到这里的是客户端自己的 `ModelHTTPError`；连接断了，是 `httpx` 的异常。**两个都不在名单上。**

后果：用户的答案已经打印出来了，然后屏幕上是一段 traceback，退出码 1。正是注释说"不能发生"的那件事。

改写时又发现第二条路：上面那个 `except` 只在 `return (await task)` 那一行生效。如果写入任务是在**宽限期里**、也就是 `await asyncio.wait_for(...)` 的时候失败的，`wait_for` 会把任务的异常原样抛出来——而那个 `try` 只接 `TimeoutError`。所以要两处都改：

> - `wait_for` 那里多一个 `except Exception: pass`——失败的事交给下面统一报告；
> - 下面那个 `except` 改成 `Exception`。注释里写了为什么列名单不行。

为什么不是 `BaseException`：`KeyboardInterrupt`（Ctrl-C）和 `CancelledError` 不是"写入失败"，是用户或程序在叫停，它们得穿过去（第 7 章 F07-04）。

`minicodex memory --remember-now`（§12）那条前台的路是同一个问题，同样改成 `except Exception`：那里用户就站在面前等着，一个服务商错误应该是一句话加一个退出码，不是 traceback。

```python
def test_a_provider_error_in_the_merge_is_reported_not_raised(tmp_path: Path) -> None:
    """`_finish_writer` caught four named exception types, and `ModelFailed`
    -- the agent loop's -- was one of them. The writer has no loop: a 500
    during the merge arrives as the client's own `ModelHTTPError`, which was
    not on the list, and the CLI printed the user's answer followed by a
    traceback and exit code 1."""
    from minicodex.__main__ import _finish_writer
    from minicodex.model import ModelHTTPError

    async def failing() -> Any:
        raise ModelHTTPError(status=500, url="u", headers={}, body="boom")

    async def scenario(already_finished: bool) -> str:
        task = asyncio.ensure_future(failing())
        if already_finished:
            await asyncio.sleep(0)
            await asyncio.sleep(0)
        return await _finish_writer(task, grace=1.0)

    # Both orders: failed before the answer was printed, and failed while the
    # CLI was waiting for it -- the second escaped by a different route.
    for already_finished in (True, False):
        line = asyncio.run(scenario(already_finished))
        assert line.startswith("memory writer: failed, nothing written (ModelHTTPError"), line
```

> 两种顺序都测：写入任务在答案打印之前就失败了，和在等它的时候失败。第二种走的是另一条路，第一版的修法只修了第一种——测试里这个循环就是被第二种逼出来的。

## §4 F17-03：本书第一个数据库，以及"先 select 再 update"不是认领

### 4.1 为什么现在才上数据库

十七章了，这个程序一直在用文件：JSONL 的 rollout、JSON 的规则、markdown 的记忆。
现在突然要 sqlite，这种决定最容易糊弄过去，所以先把理由写出来：

```python
"""Which sessions have been turned into memory, which are being worked on, and by whom.

This is the first database in the program, sixteen chapters in, and the reason
is worth stating because "use a database" is the kind of decision that arrives
with no argument attached.  Chapter 7 stores a conversation and chose an
append-only JSONL file: one writer, no updates, and the worst case is a missing
tail.  What is stored here is the opposite of that on every axis --

* every row is **updated** (pending -> claimed -> done),
* rows are **contended**: two `minicodex` processes starting at the same moment
  both want the same unprocessed sessions, and one of them must lose,
* the losing has to be **atomic**, or both of them do the work.
"""
```

**三个轴，第 7 章的选择在每一个轴上都是相反的。** 这才是"换存储"的理由，
而不是"数据结构复杂了"。

### 4.2 显而易见的写法，和它的测量结果

认领两条待办，显然是这么写：

```python
def naive_claim(self, *, limit=2, now=None, lease=300.0):
    rows = self.db.execute(
        "SELECT session_id, path, attempts FROM jobs WHERE state='pending' "
        "ORDER BY updated LIMIT ?", (limit,)).fetchall()
    out = []
    for row in rows:
        self.db.execute(
            "UPDATE jobs SET state='claimed', attempts=attempts+1, lease_until=?, updated=? "
            "WHERE session_id=?", (stamp + lease, stamp, row["session_id"]))
        out.append(Job(row["session_id"], Path(row["path"])))
    return tuple(out)
```

三个进程、六条待办，同时启动：

```
$ uv run python probe_memory_write.py race

  naive select-then-update   3 workers claimed 6 job(s), 2 distinct, 4 duplicate(s)
  BEGIN IMMEDIATE            3 workers claimed 6 job(s), 6 distinct, 0 duplicate(s)
```

跑四次，四次一模一样。**三个进程认领了同一批两条，六次认领里四次是重复的。**（这是第一次写这一章时在 Windows 上的结果。改写时在 Linux 上重跑，六次里三次有重复——§4.6。）

不是"偶尔"。Python 的 `sqlite3` 默认在第一条 DML 语句上开事务，
**`SELECT` 不算 DML**——所以那个 select 根本不在任何事务里，
三个进程读到同一批行，然后三个都 update 成功。没有异常，没有警告，
数据库也没有说谎：它们确实都执行成功了。

代价是三次模型调用做同一件事，以及三份一模一样的 `raw/` 文件。

### 4.3 修法：先拿写锁，再 select

```python
    def __init__(self, path: Path = DEFAULT_JOBS_PATH, *, timeout: float = 10.0) -> None:
        self.db = sqlite3.connect(self.path, timeout=timeout, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        # Both of these are per-connection and both are needed.  WAL lets a
        # reader (`minicodex memory --jobs`) look at the table while a writer
        # holds it; `busy_timeout` turns "database is locked" from an exception
        # into a wait, which is what a second process starting one millisecond
        # later actually wants.
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute(f"PRAGMA busy_timeout={int(timeout * 1000)}")
```

```python
        self.db.execute("BEGIN IMMEDIATE")
        try:
            rows = self.db.execute(
                "SELECT session_id, path, attempts FROM jobs "
                " WHERE (state = 'pending' AND not_before <= ?) "
                "    OR (state = 'claimed' AND lease_until <= ?) "
                " ORDER BY updated LIMIT ?",
                (stamp, stamp, limit),
            ).fetchall()
            ...
            self.db.execute("COMMIT")
        except BaseException:
            self.db.execute("ROLLBACK")
            raise
```

`isolation_level=None` 关掉 Python 那层"聪明"的隐式事务，
`BEGIN IMMEDIATE` 在 select **之前**拿写锁。0 重复，4/4。

> **一个默认值，让最自然的写法变成竞态。**
> 这类 bug 的特征是：它不在你写的那几行里，在你没写的那一行里。

### 4.4 租约、计数、退避

三个细节，每个都是一个"如果没有会怎样"：

**租约，不是标志位。**

```python
        Also takes back jobs whose lease has expired, which is the only reason
        a lease exists: the process that claimed them may have been killed, and
        a killed process does not release anything.  A job is *not* taken back
        because it is slow -- the lease is longer than the work.
```

在这个程序里"被杀"甚至是常态：§3.3 那个 `task.cancel()` 每次都在制造一个
没人释放的认领。

**尝试次数在认领时加一，不在失败时。**

```python
    def fail(self, session_id: str, detail: str, *, now: float | None = None) -> None:
        """Hand a job back with a delay, or give up on it.

        The attempt count was already incremented by `claim`, on purpose: a
        process that dies between claiming and failing must still have used up
        an attempt, or a rollout that crashes the extractor is claimed forever
        by whoever starts next.
        """
```

**三次之后放弃。** 一条抽取三次都失败的会话不会自己变好——
常见原因是它太大、或者模型拒绝它，两者都是文件的性质，不是时刻的性质。

---

### 4.5 代码：`memory_jobs.py`

新建 `src/minicodex/memory_jobs.py`。模块说明（§4.1 引过前半段）、导入和常量：

```python
"""Which sessions have been turned into memory, which are being worked on, and by whom.

This is the first database in the program, sixteen chapters in, and the reason
is worth stating because "use a database" is the kind of decision that arrives
with no argument attached.  Chapter 7 stores a conversation and chose an
append-only JSONL file: one writer, no updates, and the worst case is a missing
tail.  What is stored here is the opposite of that on every axis --

* every row is **updated** (pending -> claimed -> done),
* rows are **contended**: two `minicodex` processes starting at the same moment
  both want the same unprocessed sessions, and one of them must lose,
* the losing has to be **atomic**, or both of them do the work.

A JSON file would need a lock around every read-modify-write, and the lock is
the part that is hard to get right.  `sqlite3` is in the standard library, it
has that lock, and it has been tested by more people than this program will
ever have users.  codex keeps the same two facts in the same kind of place --
`state/memory_migrations/0001_memories.sql`, which creates exactly two tables:
`stage1_outputs` (one row per extracted rollout, keyed by `thread_id`) and
`jobs` (keyed by `(kind, job_key)`, carrying `status`, `lease_until`,
`retry_at`, `retry_remaining` and a pair of watermark columns).  The lease and
retry columns below are that table's, one name at a time.

The jobs database lives **outside** the memory directory.  That directory is a
git repository (F17-08) and a binary file rewritten on every operation is the
one thing that must not be in one: every commit would carry a blob no diff can
read, which is exactly the material the merge step needs a diff of.
"""

from __future__ import annotations

import os
import sqlite3
import time
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from minicodex.memory import MINICODEX_HOME
from minicodex.rollout import RolloutError, read_rollout

# Beside the memory directory, not inside it, and under the user's home rather
# than under the repository -- the same move the memory directory itself made
# in this chapter's rewrite (F17-12).  The reason it has to follow: this table
# records which *sessions* have been turned into memory, and the memory it
# feeds is now one global directory, so a per-repository jobs database would
# let the same session be extracted once per checkout.
DEFAULT_JOBS_PATH = MINICODEX_HOME / "memory_jobs.sqlite3"

# How long a claim is good for.  Not "until the process says it is done": a
# process that is killed says nothing, and a flag would leave the session
# unprocessable forever.  Five minutes is long enough for two model calls on a
# slow provider and short enough that a crashed run is retried the same
# afternoon.
LEASE_SECONDS = 300.0

# How many sessions one startup will consume.  codex ships the same number as
# a configurable default -- `DEFAULT_MEMORIES_MAX_ROLLOUTS_PER_STARTUP: usize
# = 2` (`config/src/types.rs:46`), surfaced as `memories.max_rollouts_per_
# startup` (`config/src/types.rs:309,331`) and read into its Phase-1 claim as
# `max_claimed` -- and the number matters less than its existence: a user who
# has not run the writer for a month has ninety sessions waiting, and "catch up
# on all of them" is a program that appears to hang on the day it is switched
# on.
MAX_PER_STARTUP = 2

# Retry, then stop.  A rollout that fails extraction three times is not going
# to start working: the usual cause is that it is enormous, or that the model
# refuses it, and both are properties of the file rather than of the moment.
MAX_ATTEMPTS = 3

# Doubling, from one minute.  The failure this backs off from is usually a
# provider one, and chapter 12 already measured what an immediate retry of an
# impossible request costs -- the quota, and then a 429 for the foreground.
BACKOFF_BASE_SECONDS = 60.0
```

> - **`DEFAULT_JOBS_PATH`** 在 `MINICODEX_HOME` 下，不在记忆目录里（§2.1）。
> - **`LEASE_SECONDS = 300.0`**：认领 5 分钟有效。注释说了为什么是租约而不是一个"处理中"的标记：被杀掉的进程什么都不会说，一个标记会让那条会话永远处理不了。
> - **`MAX_PER_STARTUP = 2`**：§3.4。
> - **`MAX_ATTEMPTS = 3`**、**`BACKOFF_BASE_SECONDS = 60.0`**：失败三次就放弃；每次重试前等 60 秒、120 秒……

表结构和一行任务：

```python
_SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    session_id  TEXT PRIMARY KEY,
    path        TEXT NOT NULL,
    state       TEXT NOT NULL,
    attempts    INTEGER NOT NULL DEFAULT 0,
    lease_until REAL NOT NULL DEFAULT 0,
    claimed_by  TEXT,
    not_before  REAL NOT NULL DEFAULT 0,
    detail      TEXT,
    updated     REAL NOT NULL DEFAULT 0
);
"""


@dataclass(frozen=True)
class Job:
    """One session waiting to be, or being, turned into memory."""

    session_id: str
    path: Path
    attempts: int = 0

    def describe(self) -> str:
        return f"{self.session_id} ({self.path.name}, attempt {self.attempts})"
```

> - `CREATE TABLE IF NOT EXISTS`：表不存在才建，所以每次打开都可以执行一遍。
> - `session_id TEXT PRIMARY KEY`：会话 id 是主键——同一个 id 只能有一行。§下面的 `enrol` 靠它防重复。
> - `state`：`pending` / `claimed` / `done` / `failed`。
> - `attempts`：认领过几次。`lease_until`：租约到什么时候。`claimed_by`：哪个进程（pid）。`not_before`：退避——这个时刻之前别来认领。`detail`：最后一次失败的原因。`updated`：最后一次变动的时间。

`JobStore` 的类说明——它就是 §4.2、§4.3 的结论：

```python
class JobStore:
    """The claim table.  Every method is safe to call from a second process.

    `isolation_level=None` is not a style choice and it is the whole of F17-03.
    Python's `sqlite3` defaults to opening a transaction on the first DML
    statement and *not* on a `SELECT`, so the obvious implementation of
    `claim()` -- select the pending rows, then update them -- runs its select
    outside any transaction at all.  Two processes select the same rows, both
    update them, and both do the work.  Measured: 4 duplicate claims out of 6
    jobs across 3 processes, with no error anywhere -- every run, on the
    Windows machine this was first written on; 3 runs in 6 on Linux, which is
    worse rather than better, because a race that shows up half the time is
    one that passes a check half the time.  Autocommit plus an
    explicit `BEGIN IMMEDIATE` takes the write lock *before* the select, which
    is the only ordering that makes the pair atomic.
    """
```

打开数据库（§4.3 引过）和"写"的那几个方法：

```python
def __init__(self, path: Path = DEFAULT_JOBS_PATH, *, timeout: float = 10.0) -> None:
    self.path = Path(path)
    self.path.parent.mkdir(parents=True, exist_ok=True)
    self.db = sqlite3.connect(self.path, timeout=timeout, isolation_level=None)
    self.db.row_factory = sqlite3.Row
    # Both of these are per-connection and both are needed.  WAL lets a
    # reader (`minicodex memory --jobs`) look at the table while a writer
    # holds it; `busy_timeout` turns "database is locked" from an exception
    # into a wait, which is what a second process starting one millisecond
    # later actually wants.
    self.db.execute("PRAGMA journal_mode=WAL")
    self.db.execute(f"PRAGMA busy_timeout={int(timeout * 1000)}")
    self.db.executescript(_SCHEMA)


def enrol(self, sessions: Iterable[tuple[str, Path]], *, now: float | None = None) -> int:
    """Record sessions this store has not seen.  Returns how many were new.

    `INSERT OR IGNORE` rather than a select-then-insert, for the reason in
    the class docstring one size down: the primary key is the session id,
    so the database decides who was first.
    """
    stamp = now if now is not None else time.time()
    added = 0
    for session_id, path in sessions:
        cursor = self.db.execute(
            "INSERT OR IGNORE INTO jobs(session_id, path, state, updated) "
            "VALUES (?, ?, 'pending', ?)",
            (session_id, str(path), stamp),
        )
        added += cursor.rowcount or 0
    return added


def claim(
    self,
    *,
    limit: int = MAX_PER_STARTUP,
    now: float | None = None,
    lease: float = LEASE_SECONDS,
) -> tuple[Job, ...]:
    """Take up to `limit` jobs, exclusively, for `lease` seconds.

    Also takes back jobs whose lease has expired, which is the only reason
    a lease exists: the process that claimed them may have been killed, and
    a killed process does not release anything.  A job is *not* taken back
    because it is slow -- the lease is longer than the work.
    """
    stamp = now if now is not None else time.time()
    deadline = stamp + lease
    self.db.execute("BEGIN IMMEDIATE")
    try:
        rows = self.db.execute(
            "SELECT session_id, path, attempts FROM jobs "
            " WHERE (state = 'pending' AND not_before <= ?) "
            "    OR (state = 'claimed' AND lease_until <= ?) "
            " ORDER BY updated LIMIT ?",
            (stamp, stamp, limit),
        ).fetchall()
        claimed = []
        for row in rows:
            self.db.execute(
                "UPDATE jobs SET state='claimed', attempts=attempts+1, lease_until=?, "
                "claimed_by=?, updated=? WHERE session_id=?",
                (deadline, str(os.getpid()), stamp, row["session_id"]),
            )
            claimed.append(
                Job(row["session_id"], Path(row["path"]), attempts=row["attempts"] + 1)
            )
        self.db.execute("COMMIT")
    except BaseException:
        self.db.execute("ROLLBACK")
        raise
    return tuple(claimed)


def finish(self, session_id: str, *, now: float | None = None) -> None:
    stamp = now if now is not None else time.time()
    self.db.execute(
        "UPDATE jobs SET state='done', lease_until=0, claimed_by=NULL, updated=? "
        "WHERE session_id=?",
        (stamp, session_id),
    )


def fail(self, session_id: str, detail: str, *, now: float | None = None) -> None:
    """Hand a job back with a delay, or give up on it.

    The attempt count was already incremented by `claim`, on purpose: a
    process that dies between claiming and failing must still have used up
    an attempt, or a rollout that crashes the extractor is claimed forever
    by whoever starts next.
    """
    stamp = now if now is not None else time.time()
    row = self.db.execute(
        "SELECT attempts FROM jobs WHERE session_id=?", (session_id,)
    ).fetchone()
    attempts = row["attempts"] if row else MAX_ATTEMPTS
    if attempts >= MAX_ATTEMPTS:
        self.db.execute(
            "UPDATE jobs SET state='failed', lease_until=0, claimed_by=NULL, "
            "detail=?, updated=? WHERE session_id=?",
            (detail[:500], stamp, session_id),
        )
        return
    self.db.execute(
        "UPDATE jobs SET state='pending', lease_until=0, claimed_by=NULL, "
        "not_before=?, detail=?, updated=? WHERE session_id=?",
        (stamp + BACKOFF_BASE_SECONDS * (2 ** (attempts - 1)), detail[:500], stamp, session_id),
    )


def forget_all(self) -> None:
    """Part of `--forget-all`: a user deleting their memory means all of it.

    Leaving the job table behind would mean the sessions that produced the
    deleted memory are marked `done`, so regenerating it would produce
    nothing and the user would conclude the feature is broken.
    """
    self.db.execute("DELETE FROM jobs")
```

> - **`enrol`**：把新发现的会话登记成 `pending`。**`INSERT OR IGNORE`**——主键已经存在就什么都不做。这比"先查有没有、没有再插"可靠：先查再插是两步，两个进程会在中间撞上；让数据库用主键决定谁先到，是一步。`cursor.rowcount` 是这条语句插进去几行（0 或 1），累加起来就是"这次新登记了几条"。
> - **`claim`**：§4.3 的全部。注意它还会**收回过期的认领**：`(state = 'claimed' AND lease_until <= ?)`——那是被杀掉的进程留下的。`ORDER BY updated LIMIT ?`：最久没动过的先来，最多 `limit` 条。认领时 `attempts=attempts+1`（§4.4 第二条）。
> - **`try: ... COMMIT / except BaseException: ROLLBACK; raise`**：中间出任何事（包括 Ctrl-C），都把事务撤销，不留半截的认领。这里用 `BaseException` 是对的——不管为什么停下，事务都得撤。
> - **`finish`**：标成 `done`，清掉租约。
> - **`fail`**：次数没用完就退回 `pending`，并设 `not_before = 现在 + 60 × 2^(次数-1)`；用完了标成 `failed`。`detail[:500]`：原因最多存 500 字，一个异常消息可能很长。
> - **`forget_all`**：§12.3。

"读"的那几个方法，和 `with` 语句要的两个方法：

```python
def rows(self) -> list[dict[str, Any]]:
    return [dict(r) for r in self.db.execute("SELECT * FROM jobs ORDER BY updated DESC")]


def counts(self) -> dict[str, int]:
    return {
        row["state"]: row["n"]
        for row in self.db.execute("SELECT state, COUNT(*) AS n FROM jobs GROUP BY state")
    }


def state_of(self, session_id: str) -> str | None:
    row = self.db.execute("SELECT state FROM jobs WHERE session_id=?", (session_id,)).fetchone()
    return row["state"] if row else None


def close(self) -> None:
    self.db.close()


def __enter__(self) -> JobStore:
    return self


def __exit__(self, *exc: object) -> None:
    self.close()
```

> - `dict(r)`：`sqlite3.Row` 可以直接变成字典。
> - `GROUP BY state` 加 `COUNT(*)`：每种状态各几行。
> - **`__enter__` 返回 `self`，`__exit__` 关掉连接**——于是 `with JobStore(path) as store:` 用完自动关。`*exc: object` 是"收下任意多个参数，我不用它们"（`with` 会传进来异常的类型、值和回溯）。

最后是"哪些会话算数"：

```python
def pending_sessions(
    directory: Path,
    *,
    exclude: Sequence[str] = (),
) -> list[tuple[str, Path]]:
    """Session files that are finished, are a user's, and are not this run's own.

    Two exclusions to start with, each of which is a fault somewhere else in
    the book:

    * **Still open.**  Chapter 7 puts an `O_EXCL` lock file next to a rollout
      that is being written.  Its presence is reused here as the answer to
      "has this session finished", because the alternative -- a footer record
      written at the end -- is a footer that a crashed run never writes, and
      the crashed runs are the ones with the interesting failures in them.

    * **This process's own session**, passed in by the caller.  It has not
      finished; it does not even have its question answered yet.

    ...and a third, which was not planned and was found by running the thing:
    **a sub-agent's session**.  Four questions asked through the CLI left seven
    files, because one of them spawned two children, and each child is a
    complete rollout with a `parent` field.  Extracted, they produce a third
    and fourth copy of what the parent's session already says -- measured, the
    two children of one task both yielded "the `pad_string(s, width, fillchar)`
    function is in util/text.py", as did the parent.  They also have no user in
    them: the whole "stable preference the user stated" half of stage 1 is
    inapplicable to a conversation whose only participant is another agent.

    Chapter 7 made exactly this exclusion, for exactly this reason, in
    `resolve("last")`: "a run that spawns two sub-agents leaves three files, and
    the two newest are the children".  The lesson is not about sub-agents.  It
    is that **a session directory stopped being a list of conversations a user
    had two chapters ago**, and every new reader of that directory has to be
    told again.
    """
    directory = Path(directory)
    if not directory.is_dir():
        return []
    out: list[tuple[str, Path]] = []
    for path in sorted(directory.glob("*.jsonl")):
        if path.with_suffix(path.suffix + ".lock").exists():
            continue
        try:
            rollout = read_rollout(path)
        except RolloutError:
            continue
        if rollout.meta.parent or rollout.meta.session_id in exclude:
            continue
        if not rollout.items:
            continue
        out.append((rollout.meta.session_id, path))
    return out
```

> - 三种不算：**还开着的**（旁边有第 7 章的 `.lock` 文件）、**这次运行自己的**（`exclude`）、**子 Agent 的**（`rollout.meta.parent` 有值）。说明里第三种的故事在 §11.2。
> - `path.with_suffix(path.suffix + ".lock")`：`a.jsonl` → `a.jsonl.lock`。
> - 读不了的会话文件（`RolloutError`）跳过，不让一个坏文件挡住别的。

```python
def test_F17_01_the_pipeline_consumes_earlier_sessions_and_never_this_one(tmp_path: Path) -> None:
    """The whole shape of the fix in one assertion.

    Generating memory at the end of a run means the user waits for it -- 15.4
    measured seconds for four sessions.  Generating it at the *start* of the
    next run costs nobody anything, and the price is that the material is
    always one session out of date.  The current session is excluded by name
    rather than by "it has no lock yet", because it does have a lock: it is
    open.
    """
    from minicodex.history import UserMessage

    sessions = tmp_path / "sessions"
    finished = _write_session(sessions, "old", ["remember: use uv"])
    current = SessionMeta(session_id="current")
    with RolloutWriter(sessions / "current.jsonl", current) as writer:
        writer.append(UserMessage("what am I doing"))
        # Open, locked, and therefore invisible even without `exclude`.
        assert [s for s, _ in pending_sessions(sessions)] == ["old"]
    assert sorted(s for s, _ in pending_sessions(sessions)) == ["current", "old"]
    assert [s for s, _ in pending_sessions(sessions, exclude=["current"])] == ["old"]
    assert finished.exists()


def test_F17_01_at_most_two_sessions_are_consumed_per_startup(tmp_path: Path) -> None:
    """A user who switches this on after a month has ninety sessions waiting."""
    store = JobStore(tmp_path / "jobs.db")
    store.enrol([(f"s{i}", Path(f"s{i}.jsonl")) for i in range(10)])
    assert len(store.claim()) == MAX_PER_STARTUP
    store.close()


def test_F17_01_a_session_that_is_still_open_is_not_eligible(tmp_path: Path) -> None:
    """Chapter 7's lock file, reused as "has this finished".

    A footer record would be the other way to answer it, and a crashed run
    never writes a footer -- so the sessions with the most interesting
    failures in them would be the ones never extracted.
    """
    sessions = tmp_path / "sessions"
    _write_session(sessions, "done", ["hello"])
    writer = RolloutWriter(sessions / "live.jsonl", SessionMeta(session_id="live"))
    try:
        assert [s for s, _ in pending_sessions(sessions)] == ["done"]
    finally:
        writer.release()


def test_F17_01_a_sub_agents_session_is_not_a_users_session(tmp_path: Path) -> None:
    """Not on the list, and found by running four questions through the CLI.

    They left *seven* session files, because one task spawned two children.
    Extracted, the children restate what the parent already said, and the half
    of stage 1 that is about what the user prefers has no user in it at all.
    Chapter 7's `resolve("last")` makes the same exclusion for the same reason.
    """
    sessions = tmp_path / "sessions"
    _write_session(sessions, "parent", ["do the thing"])
    _write_session(sessions, "child", ["sub-task"], parent="parent")
    assert [s for s, _ in pending_sessions(sessions)] == ["parent"]
```

> 这几个测试用的 `_write_session(...)` 是测试文件底部的辅助函数（§14.1）：用第 7 章真正的 `RolloutWriter` 写一个会话文件，而不是手写 JSON——第 14 章说过，一个重新实现了被测格式的测试，测的是那份抄本。

```python
def test_F17_03_a_claimed_job_is_not_claimed_twice(tmp_path: Path) -> None:
    store = JobStore(tmp_path / "jobs.db")
    store.enrol([("a", Path("a.jsonl")), ("b", Path("b.jsonl"))])
    first = store.claim(limit=1, now=100.0)
    second = store.claim(limit=1, now=100.0)
    assert [j.session_id for j in first] == ["a"]
    assert [j.session_id for j in second] == ["b"]
    assert store.claim(limit=5, now=100.0) == ()
    store.close()


def test_F17_03_an_expired_lease_is_taken_back(tmp_path: Path) -> None:
    """The only reason a lease exists rather than a flag.

    The process that claimed the job may have been killed -- or, in this
    program, simply cancelled when the foreground run finished first -- and a
    killed process releases nothing.
    """
    store = JobStore(tmp_path / "jobs.db")
    store.enrol([("a", Path("a.jsonl"))])
    store.claim(now=100.0, lease=60.0)
    assert store.claim(now=150.0, lease=60.0) == ()
    retaken = store.claim(now=200.0, lease=60.0)
    assert [j.session_id for j in retaken] == ["a"]
    assert retaken[0].attempts == 2
    store.close()


def test_F17_03_failure_backs_off_and_then_gives_up(tmp_path: Path) -> None:
    """Three attempts, doubling, and then the job stops coming back.

    A rollout that fails extraction three times is not going to start working:
    the causes are properties of the file, not of the moment.  Coming back
    forever is how a background job becomes a background cost.
    """
    store = JobStore(tmp_path / "jobs.db")
    store.enrol([("a", Path("a.jsonl"))])
    now = 100.0
    for attempt in range(1, MAX_ATTEMPTS):
        assert store.claim(now=now) != (), attempt
        store.fail("a", "boom", now=now)
        assert store.state_of("a") == "pending"
        # Not available until the backoff has passed...
        assert store.claim(now=now + 1) == ()
        now += BACKOFF_BASE_SECONDS * (2 ** (attempt - 1)) + 1
    assert store.claim(now=now) != ()
    store.fail("a", "boom", now=now)
    assert store.state_of("a") == "failed"
    assert store.claim(now=now + 10 * DAY) == ()
    store.close()


def test_F17_03_the_attempt_is_counted_when_it_is_claimed(tmp_path: Path) -> None:
    """Counted on claim rather than on failure, and that is the load-bearing bit.

    A process that dies between claiming and failing never calls `fail`.  If
    the count lived there, a rollout that crashes the extractor would be
    claimed forever by whoever starts next.
    """
    store = JobStore(tmp_path / "jobs.db")
    store.enrol([("a", Path("a.jsonl"))])
    job = store.claim(now=100.0)[0]
    assert job.attempts == 1
    assert store.rows()[0]["attempts"] == 1
    store.close()
```

> - 这些测试都传 `now=...`：时间由测试决定，"租约过期"不需要真等 5 分钟。第 12 章的重试测试用过同一招。

### 4.6 改写时重量：这个竞态在 Linux 上是时有时无的

§4.2 写的是"跑四次，四次一模一样"。这次改写在 Linux 上重跑了六次：

```
$ uv run python probe_memory_write.py race      # 六次里的三次
  naive select-then-update   3 workers claimed 6 job(s), 4 distinct, 2 duplicate(s)
  naive select-then-update   3 workers claimed 6 job(s), 6 distinct, 0 duplicate(s)
  naive select-then-update   3 workers claimed 6 job(s), 4 distinct, 2 duplicate(s)
```

**六次里三次有重复，每次两个；另外三次一个都没有。** `BEGIN IMMEDIATE` 那一行六次都是 0 重复。

所以"确定性的"这个说法只在当时那台 Windows 机器上成立。三个进程什么时候开始、操作系统怎么调度它们，决定了两个 `SELECT` 会不会落在同一个窗口里。这反而让这个 bug 更危险：**它在一台机器上每次都出现，在另一台上一半时间不出现**——一个只在"没出现"的那台上测过的人，会以为朴素写法没问题。

修法不变：先拿写锁，再查。也正因为它时有时无，测试里没有拿"三个进程赛跑"去测它（那会是一个第 14 章 §15 所说的不稳定的测试），而是直接检查结果：`test_F17_03_a_claimed_job_is_not_claimed_twice` 在同一个数据库上连续认领两次，第二次必须一条都拿不到。

### 4.7 改写时发现：被杀掉的会话，永远不会变成记忆，而且没人知道

`pending_sessions` 把"旁边有 `.lock` 文件"当成"这次会话还开着"。说明里写得很清楚，理由也对：用一个"结尾标记"来判断会话结束，那个标记在崩溃的运行里永远不会写，而崩溃的运行恰恰是最值得记的。

可是第 7 章的锁是在 `finally` 里删的。**一个被直接杀掉的进程不执行 `finally`**——关掉终端窗口、`kill -9`。那样会话旁边的 `.lock` 就永远留着，`pending_sessions` 永远跳过它。这次会话永远不会变成记忆，而 `minicodex memory --jobs` 也看不到它，因为它从来没被登记进表。

为什么不自动判断"那个进程还活着吗"？在 POSIX 上可以用 `os.kill(pid, 0)` 问；但在 Windows 上，`os.kill` 用这个参数会**直接结束那个进程**。第 7 章对同一个问题的回答是：把 pid 说出来，让人决定。这里照做——加一个函数，把这些会话列出来：

```python
def locked_sessions(directory: Path) -> list[tuple[Path, str]]:
    """Session files skipped because a lock file sits beside them, and whose pid.

    `pending_sessions` reads a lock as "still open", which is right while the
    process is alive and wrong forever after it is not: chapter 7's lock is
    removed in a `finally`, and a process that is killed outright -- the
    terminal window closed, `kill -9` -- runs no `finally`. Its session is then
    never turned into memory, and nothing anywhere said so. Found when the
    chapter was rewritten.

    Listed rather than taken: whether pid 4127 is still a `minicodex` cannot be
    answered portably (`os.kill(pid, 0)` on Windows *terminates* the process),
    and chapter 7's answer to the same question was to name the pid and let
    the person decide. `minicodex memory --jobs` prints these.
    """
    directory = Path(directory)
    if not directory.is_dir():
        return []
    out: list[tuple[Path, str]] = []
    for path in sorted(directory.glob("*.jsonl")):
        lock = path.with_suffix(path.suffix + ".lock")
        if not lock.exists():
            continue
        try:
            holder = lock.read_text(encoding="utf-8", errors="replace").strip()
        except OSError:  # pragma: no cover
            holder = ""
        out.append((path, holder or "?"))
    return out
```

> `lock.read_text(...)` 读出第 7 章写进锁文件的那个 pid。读不了就是 `"?"`。

`minicodex memory --jobs`（§12.2）在最后把它们打出来，**不管任务表存不存在**——一个刚开记忆功能的用户，第一天就可能遇到。

```python
def test_a_session_left_locked_by_a_dead_process_is_named(tmp_path: Path, capsys: Any) -> None:
    """A lock beside a rollout means "still open" -- and also "killed before
    its `finally` ran". Such a session was skipped forever, with nothing
    anywhere saying so. It is still skipped (whether pid 4127 is alive cannot
    be asked portably), but `minicodex memory --jobs` names it."""
    from minicodex.__main__ import main

    sessions = tmp_path / "sessions"
    path = _write_session(sessions, "killed", ["use uv"])
    path.with_suffix(".jsonl.lock").write_text("4127", encoding="utf-8")

    assert pending_sessions(sessions) == []
    assert locked_sessions(sessions) == [(path, "4127")]

    jobs = tmp_path / "jobs.db"
    assert main(["memory", "--jobs", "--jobs-db", str(jobs), "--session-dir", str(sessions)]) == 0
    out = capsys.readouterr().out
    assert "locked, skipped: killed.jsonl (pid 4127)" in out
    assert "delete killed.jsonl.lock" in out
```

> `capsys.readouterr().out` 取出 `main()` 打印的东西（第 14 章 §9.2）。断言它说出了文件名、pid，以及"删掉锁文件它就会被处理"。

## §5 F17-04：一个有空位的格式，模型就会把它填满

这一节是本章最长的，因为清单里那条药方**方向是对的，落地之后完全无效**。

### 5.1 清单怎么写的

> F17-04 记出来的是流水账："本次运行了测试、看了日志"
> → 高信号的明确定义（稳定偏好 / 高杠杆流程 / 失败护盾），并且**允许且鼓励什么都不写**
> ——codex 的 Phase 1 明写 "No-op is allowed and preferred"

于是初版 prompt 把这些全写上了：三类高信号的定义、五条"不要写什么"、
以及加粗的一句 **"Producing nothing is a normal and preferred outcome."**

这个 prompt 现在还在源码里，叫 `STAGE1_QUOTA`。

### 5.2 原料：四条真实会话

要测抽取，就得有真的会话。用 CLI 在四个工作区里各问一个问题，
每个问题里**顺口带一条约定**——因为真实的约定就是这么出现的：

| 会话 | 用户顺口说的 |
|---|---|
| `docstrings` | 每个函数都要有一行 docstring，reviewer 会打回 |
| `runner` | 用 `python -m pytest`，裸 `pytest` 会挑到别的解释器 |
| `layout` | 新的字符串 helper 放 `util/text.py`，`helpers.py` 是生成的 |
| `nothing` | **什么都没说**——只问 "which functions does calc.py define?" |

第四条是这一节的量尺：**一条什么都没教的会话，正确答案是零条记忆。**

### 5.3 测量：它在什么都没有的地方产出了三条

```
$ uv run python probe_memory_write.py signal --samples 3 --arms quota --only 094118

=== 20260815T094118-14584 (5 items) ===
    quota [       ] The user prefers to have functions clearly defined and documented in the code.
    quota [       ] The `calc.py` file defines two functions: `add(a, b)` and `multiply(a, b)`.
    quota [journal] To avoid confusion about function definitions, always read the necessary
                    files directly instead of relying on assumptions.
    quota -> bullets [3, 3, 3]  empty 0/3  unparseable 0/3
```

**三条，三次，一次都没有空过。** 而这条会话的全部内容是"calc.py 定义了哪些函数"。

三条都是真话。三条都毫无价值。三条都会被注入到这个仓库以后的每一个请求里。

### 5.4 病因不在那句加粗的话上

我先怀疑的是那句 "Producing nothing is a normal and preferred outcome." 不够强。
但真正的病因在它下面两段：

```
Each bullet must be one sentence, standing on its own, understandable by
someone who has not read this transcript. Write at most three bullets per
section, and fewer is better.

Answer with exactly this shape and nothing else:

## Preference signals
- ...
```

**"每节最多三条"是一个配额。一个带空位的格式，模型会把它填满。**

而这件事第 16 章刚刚从另一个方向量过一次（F16-07）。

**这里要先撤回一个数字。** 这一节原先写的是"条件式问法 0/9，无条件问法
18/18"，并据此说两章需要同一个机制往相反方向拧。前半句还站得住，后半句
不站得住了——第 16 章按 codex 真实的引用格式重测之后：

```
  codex 真实的条件式问法（已发布）      引用块 0/9   解析成功 0   编造 0
  无条件问法（只用于对照，没发布）      引用块 9/9   解析成功 0   编造 9
```

（改写第 16 章时又发现一件事：那时程序给每个小节算的**行号本身是错的**（第 16 章 §5.3），一条照真实行号写的引用也会被判成编造。所以"9 个都是编的"里，可能有几个其实是对的。这组数字没有重量过。）

无条件问法**确实**几乎每次都给出引用块。但**九条里没有一条解析得到真实条目**：
格式要的是真实行号（`MEMORY.md:12-14`），而这个程序从没让模型看见过任何行号，
于是它编了九个看起来很合理的。加一句"先用 `grep -n` 查一下"也没有改变结果。

所以两个测量合起来说的，比原先记的那句更狠：

> **一个必须填的格式会被填满，而且是被"看起来很像真的东西"填满。**
> 配额产出的是"calc.py 定义了两个函数"——真的，没用。
> 引用格式产出的是 `MEMORY.md:12-14`——格式完全正确，指向没人读过的行。
> 两边都不报错。

而两边的修法是同一个形状：**让模型在看见槽位之前先给一个判决**，
而不是指望它面对一个空位时选择不填。第 16 章还没有做到这一点（它把这条
记成了未解决的缺口）；这一章下面要做的 `DURABLE: yes|no` 就是它。

**同一个旋钮，两章往相反方向拧。** 如果只读第 16 章的结论就来写这一章，
你会做出一个更严格的格式，然后得到更多的垃圾。

### 5.5 修法：先判决，再给格式，而且判决由代码执行

```python
STAGE1_SYSTEM = f"""\
You are reading the transcript of one finished coding session. ...

First decide whether this session taught anything durable at all. Most did not.
Apply this test to each candidate line, and write it down only if it passes:

  Would a competent engineer, opening this repository for the first time, be
  unable to work this out for themselves in thirty seconds?

If they could work it out -- what a file defines, what a function does, that
tests should be written -- it is not memory. ...

Begin your answer with one line, exactly `DURABLE: yes` or `DURABLE: no`.

Answer `DURABLE: no` and leave every section empty unless there is something
that passes the test above. That is the normal outcome, and an empty answer
costs nothing: a wrong line costs every future session.

If and only if you answered yes, fill in the sections below. ...
There is no minimum; one bullet is a good answer.
"""
```

两处改动，都不是"语气更重"：

1. **判决在格式之前。** 模型必须先用一个词表态，然后才看到那个可以填的形状。
2. **"三十秒测试"是一个有答案的问题。** "这条够不够 durable" 是一种感觉。

第三处改动在代码里——**判决不是建议**：

```python
    `DURABLE: no` empties the answer, whatever else is in it.  The model is
    asked to make one judgement before it is shown a shape it could fill, and
    this line is what makes that judgement binding rather than advisory: it
    said no, so the bullets under the headings are the shape talking.
```

```python
    verdict = _VERDICT.search(text)
    if verdict is not None and verdict.group(1).lower() == "no":
        return Stage1(session_id=session_id)
```

**能在确定性代码里执行的判断，不要留给模型自觉。** 第 4.4 节那条总原则第 N 次出现。

### 5.6 结果

```
$ uv run python probe_memory_write.py signal --samples 3 --arms quota,shipped --only 094118

    quota -> bullets [3, 3, 3]  empty 0/3
  shipped !! DURABLE: no
  shipped (nothing)
  shipped -> bullets [0, 0, 0]  empty 3/3
```

而三条**有东西可教**的会话，一条都没漏：

```
=== docstrings ===
  shipped [ ] Every function in this repository carries a one-line docstring,
              including trivial ones; reviewers reject patches without them.
=== runner ===
  shipped [ ] Always use `python -m pytest` to run the test suite instead of a bare `pytest`.
=== layout ===
  shipped [ ] New string helpers should be added to `util/text.py`, not `helpers.py`,
              as `helpers.py` is generated from a schema.
```

### 5.7 它**没有**修好的那一半

上面那些"有东西可教"的会话，每次还是三条。第一条对，后两条经常是：

> The `calc.py` file contains functions for basic arithmetic operations,
> including `add`, `subtract`, and `multiply`.

**同一条垃圾，它自己 prompt 里那个三十秒测试就能判出来。**

所以老实说清楚：

> **判决门把决定的粒度从"每条"提到了"每次会话"，而只有"每次会话"这一级被测出有效。**
> 每条一级的过滤没有做，本章没有买下它。

第二段合并（§8）会再筛一次，实测能筛掉一部分（§1.1 的三条变两条）。
但它筛不干净，`## calc.py Functions` 就是活下来的那条。这是一笔**记在账上的欠债**。

### 5.8 顺带：我的测量工具又两次在测别的东西

**第一次：那个"流水账"分类器。**

我写了一个关键词表（`we `、`ran the`、`this session`…）来自动判断一条 bullet
是不是流水账。60 条里它命中 2 条。而真正的垃圾——
"The `calc.py` file defines two functions"——**语法上和真信号一模一样**。

```python
# It is kept, and it is kept *labelled*, because the first run of this section
# proved it measures the wrong thing: it fired on 2 bullets out of 60 while the
# actual junk -- "The `calc.py` file defines two functions: add and multiply" --
# is grammatically indistinguishable from the actual signal.  The fault this
# chapter is looking for is not a tense, it is a *scope*, and no keyword list
# knows the difference.
```

**第二次：`empty` 和 `unparseable` 是同一个数字。**

第一版报告里 naive 那条臂经常 `empty 2/2`。我差点写下"naive 什么都不产出"。
真相是：有些时候模型的回答里根本没有 `##` 标题，解析器返回空。
"它说没有"和"它说了但我读不懂"是**两个完全相反的结论**，而我用一个数字表示它们。

```python
                if not bullets and "##" not in answer:
                    unparsed += 1
                    print(f"  {label:>7} !! no headings in the answer: {answer[:160]!r}")
```

然后这一行**自己又错了一次**：`DURABLE: no` 的回答里也没有 `##`，
于是每一次**正确的空答案**都被记成了"无法解析"。第三版：

```python
                # "no headings" is only a parse failure when the model did
                # not say `DURABLE: no` -- the second version of this line
                # counted every correct no-op as unparseable, which is this
                # section's own measurement fault number two.
                if not bullets and "##" not in answer and "durable: no" not in answer.lower():
```

第 6 章五条、第 14 章三条、第 16 章三条，**第 17 章两条**。这已经不是巧合了，
而且形状永远一样：

> **验证工具的缺陷不会让它说错话，只会让它在报告一个关于别的东西的数字。**

---

### 5.9 代码：第一段的全部

一次抽取的结果用一个类型装：

```python
@dataclass(frozen=True)
class Stage1:
    """What one session was worth remembering.

    Three fields rather than a free-text blob, because the sections are what
    the prompt is enforcing and a parsed shape is what makes "it produced
    nothing" a fact this program can act on rather than a judgement it has to
    make about prose.

    The section names are chapter 6's structured-summary result (F06-04: six
    mandatory headings kept 23/25 planted facts against 15/25 for "summarise
    this") one subsystem later.  What changed is the *selection* rule: a
    compaction summary must keep everything still in play, and this must keep
    almost nothing.
    """

    session_id: str = ""
    preferences: tuple[str, ...] = ()
    knowledge: tuple[str, ...] = ()
    failures: tuple[str, ...] = ()
    secrets_removed: int = 0

    def __bool__(self) -> bool:
        return bool(self.preferences or self.knowledge or self.failures)

    def bullets(self) -> tuple[str, ...]:
        return self.preferences + self.knowledge + self.failures

    def describe(self) -> str:
        if not self:
            return f"{self.session_id}: nothing worth keeping"
        return (
            f"{self.session_id}: {len(self.preferences)} preference(s), "
            f"{len(self.knowledge)} fact(s), {len(self.failures)} failure(s)"
        )


_SECTIONS = (
    ("preferences", "Preference signals"),
    ("knowledge", "Reusable knowledge"),
    ("failures", "Failures and how to do differently"),
)
```

> - 三个字段对应三类"值得记"的东西（§5.1）：偏好、知识、失败的教训。说明里写了为什么要分成字段而不是一段自由文字：分开之后，"什么都没产出"是一个程序能判断的**事实**（`__bool__` 返回假），而不是要去读一段话来判断。
> - `bullets()` 把三类合成一个元组；`describe()` 给报告用。
> - `_SECTIONS`：字段名和小节标题的对照表。提示词、解析、渲染都从这一张表取，三处不会说岔。

三份提示词都留在源码里，**连同输掉的那两份**（第 16 章的理由：只留赢家、删掉对照组的"A 比 B 好"，是一个没人能重跑的结论）：

```python
# The three wordings this chapter measured, weakest first.  All three are here
# rather than only the winner, for chapter 16's reason: a claim that one
# wording is better than another, with the other one deleted, is a claim
# nobody can re-run.
#
# **STAGE1_NAIVE** -- one sentence, the version anybody writes first.
STAGE1_NAIVE = (
    "Summarise what was learned in this session that would be useful next time. "
    "Use the same three headings:\n\n"
    f"## {_SECTIONS[0][1]}\n## {_SECTIONS[1][1]}\n## {_SECTIONS[2][1]}\n"
)

_KINDS = """\
Write only high-signal material, which means exactly three kinds of thing:

1. A **stable preference** the user stated or clearly implied about how work is
   done here -- a command they insist on, a style rule, a thing they do not want
   touched. Something they would have to say again in the next session if it is
   not written down.
2. **Reusable knowledge** about this repository that was expensive to find and
   will be needed again -- where something lives, how a thing is built or run,
   what a non-obvious file is for.
3. A **failure shield**: something that went wrong, and the one sentence that
   would stop it happening again.

Do not write:

- What happened. "Ran the tests", "read three files", "added a function" are
  facts about one afternoon, not about this repository.
- Anything true only of the task in this session.
- Anything already obvious from opening the repository.
- Credentials, tokens, keys, or anything that looks like one, even partially.
- Praise, narration, or a description of what you are about to do.
"""

_SHAPE = f"""\
## {_SECTIONS[0][1]}
- ...

## {_SECTIONS[1][1]}
- ...

## {_SECTIONS[2][1]}
- ...
"""

# **STAGE1_QUOTA** -- the careful version, and the one this chapter shipped
# first.  It says everything correct: what to keep, what not to keep, and that
# "producing nothing is a normal and preferred outcome" in bold, which is the
# sentence codex's own stage-one prompt carries.
#
# It produced three bullets for **every session it was shown, including the one
# that taught nothing**: 0/2 no-ops on a session whose whole content is "which
# functions does calc.py define". The bullets it invented for it were
# "The user prefers concise summaries of function definitions" and "The calc.py
# file defines two functions". Both true. Both worthless. Both would have been
# injected into every future request in this repository.
#
# The cause is the last paragraph, and it is not the one that says nothing is
# preferred: **"write at most three bullets per section" is a quota, and a
# format with slots in it is a format a model fills.**
#
# Chapter 16 met the same mechanism from the other side, and what it found
# there is now a sharper version of this result than the one this comment used
# to cite.  Its first draft claimed that asking for a citation block
# unconditionally took compliance from 0/9 to 18/18 -- and that measurement did
# not survive its rewrite.  Asking unconditionally does still produce a block
# almost every time; what the stricter format showed is that **0 of 9 of those
# blocks contained a line that resolved to anything real.**  The model filled
# the slot with invented line numbers.
#
# So the two chapters agree, and they agree on something worse than "a required
# shape is filled": a required shape is filled *with plausible material*
# whether or not there is anything to put in it.  A quota produces a bullet
# about `calc.py` defining two functions; a citation format produces
# `MEMORY.md:12-14` for lines nobody read.  Neither failure announces itself,
# and in both cases the fix is the same shape -- make the model commit to a
# verdict before it is shown the slot (`DURABLE: yes/no` below), rather than
# hoping it declines to fill one.
STAGE1_QUOTA = f"""\
You are reading the transcript of one finished coding session. Your job is to
decide what, if anything, about it is worth remembering in three months, and to
write that down. You are not summarising the session.

{_KINDS}
**Producing nothing is a normal and preferred outcome.** Most sessions teach
nothing durable. If this one did not, return the three headings with no bullets
under them. An empty answer costs nothing; a wrong one is injected into every
future session in this repository.

Each bullet must be one sentence, standing on its own, understandable by
someone who has not read this transcript. Write at most three bullets per
section, and fewer is better.

Answer with exactly this shape and nothing else:

{_SHAPE}"""
```

> - **`STAGE1_NAIVE`**：一句话版本，谁都会先写这个。
> - **`_KINDS`**：三类该写、五类不该写。两份认真的提示词共用它。
> - **`_SHAPE`**：要模型照着填的那个形状。
> - **`STAGE1_QUOTA`**：§5.3 量出来"在什么都没有的地方产出三条"的那一份。它上面那段长注释就是 §5.4，还记着和第 16 章对照的结论。
> - 上面这段代码到 `STAGE1_QUOTA` 结束为止。发布的那一份 `STAGE1_SYSTEM`（§5.5）紧接着它，单独列在下面：

```python
# **STAGE1_SYSTEM** -- what ships.  Two changes from the version above, and
# both are structural rather than emphatic.
#
# The first is the verdict line. The model must decide, in one word, before it
# is shown the shape it could fill -- and the decision is *enforced* by
# `parse_stage1` rather than trusted, which is this book's standing rule about
# anything that can be settled in code.
#
# The second is the test. "Would a competent engineer opening this repository
# be unable to work this out in thirty seconds" is a question with an answer,
# where "is this durable?" is a question with a vibe.
STAGE1_SYSTEM = f"""\
You are reading the transcript of one finished coding session. Your job is to
decide what, if anything, about it is worth remembering in three months, and to
write that down. You are not summarising the session.

First decide whether this session taught anything durable at all. Most did not.
Apply this test to each candidate line, and write it down only if it passes:

  Would a competent engineer, opening this repository for the first time, be
  unable to work this out for themselves in thirty seconds?

If they could work it out -- what a file defines, what a function does, that
tests should be written -- it is not memory. It is a sentence that will be sent
with every future request in this repository, forever, and will teach nobody
anything.

{_KINDS}
Begin your answer with one line, exactly `DURABLE: yes` or `DURABLE: no`.

Answer `DURABLE: no` and leave every section empty unless there is something
that passes the test above. That is the normal outcome, and an empty answer
costs nothing: a wrong line costs every future session.

If and only if you answered yes, fill in the sections below. One sentence per
bullet, standing on its own, understandable by someone who has not read this
transcript. There is no minimum; one bullet is a good answer.

Answer with exactly this shape and nothing else:

DURABLE: yes
{_SHAPE}"""
```

> 这一段的 `f"""..."""` 里，`{_KINDS}` 和 `{_SHAPE}` 会被替换成上面那两段字。

解析模型的回答：

```python
_VERDICT = re.compile(r"^\W*durable\W*:\W*(yes|no)\b", re.IGNORECASE | re.MULTILINE)


_BULLET = re.compile(r"^\s*[-*]\s+(.*\S)\s*$")


_H2 = re.compile(r"^##\s+(.+?)\s*$")


def parse_stage1(text: str, *, session_id: str = "") -> Stage1:
    """Read the extractor's answer, keeping only what is under a known heading.

    Tolerant in one direction and strict in the other: unknown headings and
    prose between sections are dropped, and nothing outside a section is ever
    promoted into one.  A model that ignores the format produces an empty
    `Stage1`, which is the same outcome as a model that had nothing to say --
    and that is the right collapse, because both mean "there is nothing here to
    merge".

    `DURABLE: no` empties the answer, whatever else is in it.  The model is
    asked to make one judgement before it is shown a shape it could fill, and
    this line is what makes that judgement binding rather than advisory: it
    said no, so the bullets under the headings are the shape talking.  Measured
    disagreement -- said no and wrote bullets anyway -- is in the chapter.
    """
    verdict = _VERDICT.search(text)
    if verdict is not None and verdict.group(1).lower() == "no":
        return Stage1(session_id=session_id)
    wanted = {title.lower(): key for key, title in _SECTIONS}
    found: dict[str, list[str]] = {key: [] for key, _ in _SECTIONS}
    current: str | None = None
    for line in text.splitlines():
        heading = _H2.match(line)
        if heading:
            current = wanted.get(heading.group(1).strip().lower())
            continue
        if current is None:
            continue
        bullet = _BULLET.match(line)
        if bullet:
            body = bullet.group(1).strip()
            # A model told to leave a section empty sometimes writes "- none"
            # rather than nothing at all.  Treating that as an entry is how a
            # memory ends up with a section that says "none" in it forever.
            #
            # `...` is on this list because it happened: the shape at the
            # bottom of the prompt is `- ...`, and one sample in three copied
            # the placeholder through into its answer.  A memory entry whose
            # entire text is an ellipsis is the kind of thing that survives
            # for a year because nobody can tell what it was meant to say.
            if body.lower().strip(" .") in {"none", "n/a", "nothing", "(none)", ""}:
                continue
            found[current].append(body)
    return Stage1(
        session_id=session_id,
        preferences=tuple(found["preferences"]),
        knowledge=tuple(found["knowledge"]),
        failures=tuple(found["failures"]),
    )
```

> - **`_VERDICT`**：认 `DURABLE: yes|no` 那一行。`^\W*durable\W*:\W*(yes|no)\b`——行首允许有任意符号（模型爱写成 `**DURABLE: no**`），大小写不论（`re.IGNORECASE`），任何一行都可以（`re.MULTILINE`）。
> - **`DURABLE: no` 直接返回一个空的 `Stage1`**，不管下面写了什么。这就是"判决由代码执行"。
> - 然后一行一行读：遇到 `## 标题`，看它是不是三个认识的标题之一（不区分大小写），是就记下"现在在哪一节"，不是就把"当前节"设成 `None`——后面的内容全部丢掉。遇到 `- 条目`，放进当前节。
> - **`- none`、`- ...` 这类不算条目。** 注释里写了 `...` 为什么在名单上：提示词的形状里写的是 `- ...`，三次里有一次模型把这个占位符原样抄进了答案。一条正文是省略号的记忆，能在文件里活一年，因为没人看得出它本来想说什么。

`raw/` 文件、会话变成文字、一次抽取、一次不流式的提问：

```python
def render_stage1(stage1: Stage1) -> str:
    """The `raw/<session>.md` file, which says what it is in its first lines.

    The warning is not decoration.  codex's equivalent carries the same one --
    "Temporary file: … Input for Phase 2" -- and the fault it prevents is a
    slow one: a file in a memory directory that looks like memory, and that
    somebody eventually edits, or greps, or copies into a bug report.
    """
    lines = [
        FORMAT_VERSION,
        "",
        f"<!-- Temporary file: stage-1 output for session {stage1.session_id or '?'}.",
        "     Input for stage 2. Deleted once it has been merged.",
        "     Nothing reads this as memory; edit MEMORY.md instead. -->",
        "",
    ]
    for key, title in _SECTIONS:
        lines.append(f"## {title}")
        for item in getattr(stage1, key):
            lines.append(f"- {item}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def transcript_of(rollout: Rollout, *, budget: int = TRANSCRIPT_TOKEN_BUDGET) -> tuple[str, int]:
    """One session as text, redacted and clipped.  Returns text and secret count.

    System and developer notes are **left out**, and that is a decision rather
    than an omission.  They are this program's own words -- the permissions
    block, chapter 0's "you have 2 turns left", chapter 13's `AGENTS.md`
    injection, chapter 16's memory block itself -- and an extractor shown them
    dutifully reports the program's instructions back as things worth
    remembering.  The most direct form of that is the last one in the list: a
    memory that was injected into the session is a memory the session then
    re-learns, which is a loop with no fixed point and no error message.

    One thing is lost with them and it is worth saying out loud, because it
    looks like an oversight: chapter 13's `AGENTS.md` arrives as a developer
    note, and it is the most convention-shaped material in a session.  It is
    dropped **because it is already in the repository**, in a file a person
    maintains and a team can read.  Extracting it would store the same sentence
    in two places, and the two places drift -- which is the whole argument for
    the three-layer table in chapter 16 §2.1.
    """
    parts: list[str] = []
    for item in rollout.items:
        if isinstance(item, UserMessage):
            parts.append(f"user: {item.text}")
        elif isinstance(item, AssistantMessage):
            if item.text.strip():
                parts.append(f"assistant: {item.text.strip()}")
            for call in item.tool_calls:
                if call.name == NOTE_NAME:
                    continue
                parts.append(f"assistant calls {call.name} {call.raw_arguments}")
        elif isinstance(item, ToolResult):
            if item.name == NOTE_NAME:
                # The writer does not read its own output.  A `remember_this`
                # call is *already* an input to stage 2 by another route
                # (`notes/`), so leaving it in the transcript delivers the same
                # sentence twice -- measured: 3/3 extractions of a session that
                # proposed a note re-derived the note as a bullet.
                #
                # This is F17-10's shape, in the one place this program can
                # reach it: the memory system reading a transcript of the
                # memory system.  The listed version of that fault needs the
                # writer to be an agent with a session of its own, which this
                # one is not (see the chapter).
                continue
            parts.append(f"{item.name} returned: {clip(item.content, TOOL_RESULT_CHARS)}")
    text = "\n".join(parts)
    text, secrets = scrub(text)
    # Head and tail, chapter 2's shape: the user's actual request is at the top
    # of a session and the thing that finally worked is at the bottom, and a
    # tail-only clip loses the first of those every time.
    if estimate_messages([{"role": "user", "content": text}]) > budget:
        text = clip(text, budget * 4)
    return text, secrets


async def extract(
    model: Any,
    rollout: Rollout,
    *,
    system: str = STAGE1_SYSTEM,
) -> Stage1:
    """Stage 1 for one session.  One model call, no tools, no loop.

    Not an `Agent`.  There is nothing to iterate: the input is a transcript
    that already exists and the output is text.  A tool loop here would add
    every failure mode chapters 0 to 12 exist to handle, in exchange for
    nothing -- and it would also give a model somewhere to write, which is the
    one thing F17-09 says it must not have.
    """
    text, secrets = transcript_of(rollout)
    answer = await _ask(
        model,
        [
            {"role": "system", "content": system},
            {"role": "user", "content": f"<transcript>\n{text}\n</transcript>"},
        ],
    )
    # The second scrub.  The first one was on the way in; this one is on the
    # way to a file that gets injected into every future request.
    answer, more = scrub(answer)
    stage1 = parse_stage1(answer, session_id=rollout.meta.session_id)
    return Stage1(
        session_id=stage1.session_id,
        preferences=stage1.preferences,
        knowledge=stage1.knowledge,
        failures=stage1.failures,
        secrets_removed=secrets + more,
    )


async def _ask(model: Any, messages: list[dict[str, Any]]) -> str:
    """Collect one non-streaming answer out of a streaming client.

    The same shape `make_summariser` uses in chapter 6, including the deferred
    import, and it is duplicated rather than shared: that one builds a
    `History` and renders it, this one has two literal messages and wants no
    history at all.  Two six-line functions that look alike is not the same
    thing as two call sites of one function.

    The `[DONE]` check is chapter 0's rule, third application.  A stream that
    ended early is not a shorter answer -- here it is half a memory, and half a
    memory is written to disk and read back forever.
    """
    from minicodex.model import Completed, TextDelta

    out: list[str] = []
    finished = False
    async for event in model.stream(messages):
        if isinstance(event, TextDelta):
            out.append(event.text)
        elif isinstance(event, Completed):
            finished = True
    if not finished:
        raise MemoryWriteError("the model stream ended without a [DONE] sentinel")
    return "".join(out)
```

> - **`render_stage1`**：§2.3。开头三行是 HTML 注释（`<!-- ... -->`），在 markdown 里看不见，打开文件看得见。
> - **`transcript_of`**：把一次会话的历史变成纯文字给抽取器读。
>   - 只要三种条目：用户说的、模型说的（和它要调的工具）、工具返回的。**系统和 developer 消息不要**——说明里讲了，那些是程序自己的话（权限说明、"还剩两轮"、`AGENTS.md`、**第 16 章的记忆块本身**）；抽取器读到它们，会把程序的指示当成"值得记住的事"报回来——尤其是记忆块：注入进会话的记忆，被会话重新学一遍，一代一代加强，没有终点也没有报错。
>   - `remember_this` 的调用和返回也跳过（§11.2）。
>   - 每条工具结果用第 2 章的 `clip` 截到 800 字（头和尾）。
>   - 整段先脱敏（§6），太长就再截一次，头尾都留：用户的请求在开头，最后成功的那一步在结尾。
> - **`extract`**：一次模型调用，没有工具、没有循环。两条消息：系统提示词、和用 `<transcript>` 包起来的会话。回来的答案**再脱敏一次**，然后解析。说明里讲了为什么不是一个 Agent：没有需要循环的东西，而一个 Agent 意味着要给模型一个能写东西的地方——那正是 §8 说它不能有的。
> - **`_ask`**：从一个流式的客户端里收一个完整的答案。`from minicodex.model import ...` 写在函数里面（延迟导入），和第 6 章的 `make_summariser` 一样。**没收到 `[DONE]` 就报错**——第 0 章的规矩第三次：一个提前断掉的流不是一个短一点的答案，在这里它是半份记忆，而半份记忆会被写进文件、以后每次都被读出来。

```python
def test_F17_04_the_shipped_prompt_asks_for_a_verdict_before_it_shows_a_shape() -> None:
    """A snapshot, chapter 3's F03-10 shape, and it is pinning a measurement.

    The version without these two clauses -- `STAGE1_QUOTA`, still in the
    module -- produced three bullets for *every* session it was shown,
    including one whose entire content was "which functions does calc.py
    define": 0/3 no-ops where the answer should have been nothing at all.  The
    shipped wording answered `DURABLE: no` 3/3 on the same session.

    Both clauses are load-bearing and both look like padding:
    """
    assert "DURABLE: yes" in STAGE1_SYSTEM
    assert "DURABLE: no" in STAGE1_SYSTEM
    assert "thirty seconds" in STAGE1_SYSTEM
    assert "There is no minimum" in STAGE1_SYSTEM
    # ...and the thing that had to go: a quota is filled.
    assert "at most three bullets" in STAGE1_QUOTA
    assert "at most three bullets" not in STAGE1_SYSTEM


def test_F17_04_a_no_verdict_empties_the_answer() -> None:
    """The judgement is enforced, not trusted.

    A model that answers `DURABLE: no` and then fills the sections anyway is
    the shape talking, and the shape does not get a vote.
    """
    answer = (
        "DURABLE: no\n\n## Preference signals\n- The user likes tests.\n"
        "## Reusable knowledge\n- calc.py has two functions.\n"
    )
    assert not parse_stage1(answer)
    assert parse_stage1(answer.replace("no", "yes")).bullets()


def test_F17_04_nothing_to_keep_is_a_success_not_a_failure(tmp_path: Path) -> None:
    """An empty extraction finishes the job and writes no file.

    Writing an empty raw file instead would make every merge read a page of
    empty headings, and re-queueing the session would mean paying for the same
    negative answer forever.
    """
    sessions = tmp_path / "sessions"
    _write_session(sessions, "s1", ["what does calc.py define?"])
    jobs = tmp_path / "jobs.db"
    report = asyncio.run(
        run_pipeline(
            _Scripted(["DURABLE: no"]),
            directory=tmp_path / "mem",
            sessions_dir=sessions,
            jobs_path=jobs,
            lock_path=tmp_path / "merge.lock",
        )
    )
    assert report.sessions == ("s1",)
    assert report.empty == ("s1",)
    assert not report.merged
    assert pending_raw(tmp_path / "mem") == ()
    with JobStore(jobs) as store:
        assert store.state_of("s1") == "done"


def test_F17_04_a_placeholder_bullet_is_not_a_memory() -> None:
    """`- ...` is in the prompt's example, and one sample in three copied it.

    An entry whose whole text is an ellipsis survives for a year, because
    nobody can tell what it was meant to say.
    """
    answer = "DURABLE: yes\n## Preference signals\n- ...\n- none\n- Use uv.\n"
    assert parse_stage1(answer).preferences == ("Use uv.",)


def test_F17_04_material_outside_a_known_heading_is_dropped() -> None:
    answer = (
        "Here is what I found.\n- a stray bullet\n"
        "## Something else\n- not one of ours\n"
        "## Reusable knowledge\n- Releases are cut with `make ship`.\n"
    )
    out = parse_stage1(answer)
    assert out.knowledge == ("Releases are cut with `make ship`.",)
    assert out.preferences == ()


def test_F17_04_the_raw_file_says_it_is_temporary(tmp_path: Path) -> None:
    """codex writes the same warning into the same file, and it is not tidiness.

    An intermediate product that does not say it is intermediate is one
    somebody starts maintaining.
    """
    path = write_raw(tmp_path, Stage1(session_id="s1", preferences=("Use uv.",)))
    text = path.read_text(encoding="utf-8")
    assert text.splitlines()[0] == FORMAT_VERSION
    assert "Temporary file" in text
    assert "Input for stage 2" in text
    assert path.parent.name == RAW_DIR


def test_a_stream_that_ends_early_is_not_half_a_memory(tmp_path: Path) -> None:
    """Chapter 0's `[DONE]` rule, third application.

    Half an answer here is written to a file and read back forever.
    """
    sessions = tmp_path / "sessions"
    _write_session(sessions, "s1", ["hello"])
    with pytest.raises(MemoryWriteError, match=r"\[DONE\]"):
        asyncio.run(
            extract(_Scripted(["half an ans"], finish=False), read_rollout(sessions / "s1.jsonl"))
        )
```

> - `_Scripted([...])` 是测试文件底部的假模型（§14.1）：每次被问，就按顺序说出列表里的下一句，并记下收到的消息。

## §6 F17-05：脱敏必须在两侧，而只有第二侧挡得住"请记住这个 key"

### 6.1 第一次测量：没复现

在一条真实会话里放一个 `.env`：

```
STRIPE_API_KEY=sk-live-9f4c2ab7d3e1f0aa88b2
```

模型 `read_file` 读了它，回答里说了"这个项目用 Stripe"。这条会话进抽取：

```
$ uv run python probe_memory_write.py secrets --samples 3

=== 20260815T094123-27500 ===
  the secret appears 1 time(s) in the raw session file
  after scrubbing on the way in: 0 time(s), 1 removed

  -- arm: incidental (a key the session happened to read) --
  [0] the model's own output contains the key 0 time(s); ... final 0
  [1] the model's own output contains the key 0 time(s); ... final 0
  [2] the model's own output contains the key 0 time(s); ... final 0
```

**0/3。** 就算把未脱敏的原文直接喂给抽取器，它也不会把 key 抄进记忆。

到这里就收工，结论会是"F17-05 未复现，入口侧脱敏够了"。

### 6.2 第二条臂：用户**要求**记住它

真实世界里 key 进记忆最可能的方式不是"顺手读到"，是有人说"记住它":

```python
ASKED_TO_REMEMBER = f"""\
user: Remember this for next time: the staging deploy key is {SECRET} -- you
will need it every time you deploy to staging and I do not want to paste it
again.
assistant: Understood. I will use that key for staging deploys.
run_shell returned: deploy ok
"""
```

```
  -- arm: asked for (the user says: remember this) --
  [0] the model's own output contains the key 1 time(s); the outbound scrub removed 1; final 0
  [1] the model's own output contains the key 1 time(s); the outbound scrub removed 1; final 0
  [2] the model's own output contains the key 2 time(s); the outbound scrub removed 2; final 0
```

**3/3，而且有一次抄了两遍。**

入口侧脱敏对这条臂**完全无效**——它不需要从历史里看到 key，用户在会话里
明明白白地把 key 交给它并要求记住。挡住它的只有**出口侧那一遍**。

```python
def scrub(text: str) -> tuple[str, int]:
    """Remove anything credential-shaped.  Returns the text and how many hits.

    Applied **twice**, on purpose, and that is the whole of F17-05: once to the
    transcript before it is sent to the extractor, and once to the extractor's
    answer before it is written to a file that will be injected into every
    future request.  Doing only the first is the version that feels sufficient
    -- if the model never saw the key it cannot repeat it -- and it is wrong in
    two directions: ...
    """
```

> **只测容易的那条臂，你会发一个单侧脱敏，并且拿到一个绿色的测量结果。**

### 6.3 顺带：计数又在数错的东西

第一次跑，报告是 "after scrubbing on the way in: 0 time(s), **2 removed**"——
一个 secret，两次移除。因为 shape 正则先把值换成 `<redacted>`，
然后 `NAME=value` 那条正则又在 `STRIPE_API_KEY=<redacted>` 上命中一次。

```python
    def _assignment(match: re.Match[str]) -> str:
        nonlocal hits
        if match.group(2) == REDACTED:
            # Already handled by a shape above.  Without this the count is of
            # *substitutions* rather than of secrets, and the probe reported
            # "2 removed" for one key -- a number about the regex list, not
            # about the transcript.
            return match.group(0)
```

这次它出现在**发布的代码里**，不是探针里——那个数字要打印给用户看
（"3 secret(s) removed"），一个乱报的计数就是一条没人信的告警。

### 6.4 为什么按名字也要匹配一遍

第 -1 章的 recorder 是**按 key 名**脱敏的（`api_key`、`authorization`），
那对 JSON 请求体是对的，对这里没用：

```python
# Shapes that are secret regardless of what they are called.  Chapter -1's
# recorder redacts by *key name* (`api_key`, `authorization`), which is right
# for a JSON request body and useless here: a transcript is prose, and the
# thing that reaches this module is `OPENAI_API_KEY=sk-proj-…` sitting in the
# output of `env`, or a token a user pasted into a question.
```

所以这里是两套：**形状**（`sk-`、`ghp_`、`AKIA`、JWT、PEM）
和**名字**（`*KEY*=`、`*TOKEN*:`、`*PASSWORD*=`）。
两套都要，因为一个自定义 token 没有形状，而一个粘贴在句子里的 key 没有名字。

---

### 6.5 代码：`scrub`

```python
# Redaction: on the way in, and again on the way out
# ---------------------------------------------------------------------------

# Shapes that are secret regardless of what they are called.  Chapter -1's
# recorder redacts by *key name* (`api_key`, `authorization`), which is right
# for a JSON request body and useless here: a transcript is prose, and the
# thing that reaches this module is `OPENAI_API_KEY=sk-proj-…` sitting in the
# output of `env`, or a token a user pasted into a question.
_SECRET_SHAPES = (
    re.compile(r"\bsk-[A-Za-z0-9_\-]{16,}"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{16,}"),
    re.compile(r"\bAKIA[0-9A-Z]{12,}"),
    re.compile(r"\bxox[baprs]-[A-Za-z0-9\-]{10,}"),
    re.compile(r"\beyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.S),
)

# `NAME=value` where the name says it is a credential.  The value is taken up
# to whitespace or a quote, which is what an `env` dump and a `.env` file both
# look like.
_SECRET_ASSIGNMENT = re.compile(
    r"(?i)\b([A-Z0-9_]*(?:KEY|TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIAL)[A-Z0-9_]*)"
    r"\s*[=:]\s*[\"']?([^\s\"'\n]{6,})"
)

REDACTED = "<redacted>"


def scrub(text: str) -> tuple[str, int]:
    """Remove anything credential-shaped.  Returns the text and how many hits.

    Applied **twice**, on purpose, and that is the whole of F17-05: once to the
    transcript before it is sent to the extractor, and once to the extractor's
    answer before it is written to a file that will be injected into every
    future request.  Doing only the first is the version that feels sufficient
    -- if the model never saw the key it cannot repeat it -- and it is wrong in
    two directions: the merge step also reads hand-written notes and a
    user-edited memory file, and a model asked to summarise a session can
    reconstruct a partially-masked value from context.

    A count is returned rather than a boolean because it goes in the report:
    "3 secret(s) removed" is a line that makes somebody look, and a silent
    redaction is a redaction nobody audits.
    """
    hits = 0
    for pattern in _SECRET_SHAPES:
        text, n = pattern.subn(REDACTED, text)
        hits += n

    def _assignment(match: re.Match[str]) -> str:
        nonlocal hits
        if match.group(2) == REDACTED:
            # Already handled by a shape above.  Without this the count is of
            # *substitutions* rather than of secrets, and the probe reported
            # "2 removed" for one key -- a number about the regex list, not
            # about the transcript.
            return match.group(0)
        hits += 1
        return f"{match.group(1)}={REDACTED}"

    text = _SECRET_ASSIGNMENT.sub(_assignment, text)
    return text, hits
```

> - **`_SECRET_SHAPES`**：六种"长得就像密钥"的东西——OpenAI 的 `sk-...`、GitHub 的 `ghp_...` 一族、AWS 的 `AKIA...`、Slack 的 `xox?-...`、JWT（三段 base64，用 `.` 隔开，`eyJ` 开头）、PEM 私钥（`-----BEGIN ... PRIVATE KEY-----` 到 `END`，中间可以跨行，所以带 `re.S`）。`{16,}` 是"至少 16 个"——太短的串不当成密钥，免得误伤普通的词。
> - **`_SECRET_ASSIGNMENT`**：`名字 = 值`，名字里含 `KEY`、`TOKEN`、`SECRET`、`PASSWORD` 之类。`(?i)` 让整个正则不区分大小写。值取到空白或引号为止，至少 6 个字符。
> - **`scrub`**：先按形状替换，`pattern.subn(...)` 同时告诉你换了几处；再按名字替换。
> - **里面那个 `_assignment` 函数**：`re.sub` 的第二个参数可以是一个函数，每找到一处就调用一次，返回值就是替换成的东西。它用 `nonlocal hits` 改外层的计数（第 16 章 §0.2）。如果值已经是 `<redacted>`（被形状那一步换过了），原样返回、不计数——§6.3 那个"一个 key 报两次"的修法。

```python
def test_F17_05_a_key_is_removed_from_the_transcript_before_it_is_sent(tmp_path: Path) -> None:
    sessions = tmp_path / "sessions"
    _write_session(sessions, "s1", ["the key is sk-live-9f4c2ab7d3e1f0aa88b2, do not lose it"])
    rollout = read_rollout(sessions / "s1.jsonl")
    text, removed = transcript_of(rollout)
    assert "sk-live-9f4c2ab7d3e1f0aa88b2" not in text
    assert REDACTED in text
    assert removed == 1


def test_F17_05_the_models_own_answer_is_scrubbed_too(tmp_path: Path) -> None:
    """The half that is easy to leave out, and the half that was measured.

    Given a transcript in which the user *asks* for a key to be remembered,
    gpt-4o-mini reproduced it in its extraction output 3 times out of 3.  A
    scrub on the way in cannot help with that: the model was asked to write
    the key down and it did.  Only the outbound pass stops it reaching a file
    that is injected into every future request.
    """
    sessions = tmp_path / "sessions"
    _write_session(sessions, "s1", ["hello"])
    rollout = read_rollout(sessions / "s1.jsonl")
    leak = "DURABLE: yes\n## Reusable knowledge\n- The deploy key is sk-live-aaaabbbbccccdddd.\n"
    out = asyncio.run(extract(_Scripted([leak]), rollout))
    assert "sk-live-aaaabbbbccccdddd" not in "".join(out.bullets())
    assert REDACTED in "".join(out.bullets())
    assert out.secrets_removed == 1


def test_F17_05_an_assignment_is_caught_by_its_name_not_only_its_shape() -> None:
    """`env` output and a `.env` file are the two ways this arrives."""
    text, hits = scrub("DATABASE_PASSWORD=hunter2hunter2\nGITHUB_TOKEN: ghp_abcdefghijklmnopqrst")
    assert "hunter2hunter2" not in text
    assert "ghp_abcdefghijklmnopqrst" not in text
    assert hits == 2


def test_F17_05_the_count_is_of_secrets_not_of_regexes() -> None:
    """One key matched by two patterns was reported as two.

    A number about the pattern list rather than about the transcript -- the
    same shape as every measurement-tool fault in this book, in the shipped
    code this time.
    """
    _, hits = scrub("STRIPE_API_KEY=sk-live-9f4c2ab7d3e1f0aa88b2")
    assert hits == 1


def test_F17_05_the_merge_answer_is_scrubbed_before_it_is_parsed() -> None:
    merged = parse_merged(
        f"{SUMMARY_MARK}\n- Deploy with token ghp_zzzzyyyyxxxxwwwwvvvv.\n{BODY_MARK}\n## A\n\nb\n"
    )
    assert "ghp_zzzzyyyyxxxxwwwwvvvv" not in merged.summary
    assert merged.secrets_removed == 1
```

> 第二个测试是 §6.2 那条臂的离线版：一个**模型自己的回答**里出现了密钥（假模型直接说出来），写进 `raw/` 的文件里必须没有它。

名字匹配有一个代价，值得知道：`(?i)` 加上"名字里含 KEY"，会把 `keyfunc=lambda` 或者 `tokenizer: tiktoken` 这样的普通代码也换成 `<redacted>`，报告里的"N secret(s) removed"会偏多。这是往安全的方向错，没有改；章末练习 4 是它。

## §7 F17-02：后台工作给前台让路

### 7.1 provider 到底说了什么

先看真实响应头：

```
$ uv run python probe_memory_write.py quota

  x-ratelimit-limit-requests: 10000
  x-ratelimit-limit-tokens: 200000
  x-ratelimit-remaining-requests: 9987
  x-ratelimit-remaining-tokens: 199996
  x-ratelimit-reset-requests: 1m51.535s
  x-ratelimit-reset-tokens: 1ms

  parsed: RateLimit(remaining_requests=9987, limit_requests=10000,
                    remaining_tokens=199996, limit_tokens=200000)
  headroom: 99.87
```

第 12 章已经解析过这些头——**但只在 429 的时候**，那时候这些数字已经变成了
"你刚刚撞上的那堵墙的描述"。这一章要在事情变坏之前读它，所以改在
**成功响应**上读：

```python
                # Before the first byte of the body: the headers are already
                # here, and reading them costs nothing.  Assigned even when
                # they are absent -- `None` is the honest answer for a server
                # that does not report, and the caller is required to treat it
                # as unknown rather than as unlimited.
                self.rate_limit = RateLimit.from_headers(resp.headers)
```

### 7.2 取两个窗口里紧的那个

```python
    def headroom(self) -> float | None:
        """The tighter of the two windows, as a percentage.  `None` if unknown.

        The *minimum* of requests and tokens, not an average: they are two
        separate buckets and running out of either one stops the program.
        """
```

### 7.3 不知道 ≠ 没有

ollama 一个 rate-limit 头都不发。所以 `headroom()` 会返回 `None`，
而这个 `None` 怎么解释是一个真实的设计决定：

```python
    # matters.  `None` means "the provider does not say", which is not the
    # same as "there is plenty" -- and the choice made here is to proceed,
    # because the alternative switches the feature off entirely against every
    # server that reports nothing, ollama included.
```

**把"未知"当成"没有"，等于对所有不上报的 provider 关掉这个功能。**
选了"继续"，并且把这个选择写下来，而不是让它成为一个可以被推断出来的默认值。

### 7.4 一个参数为什么是 callable

```python
    # A callable is allowed, and the reason is a real ordering problem rather
    # than flexibility for its own sake: this task is created *before* the
    # foreground run's first request, so at creation time nobody has seen a
    # rate-limit header yet.  Passing the number would mean passing `None`
    # forever; passing the question means it is answered at the moment it
    # matters.
```

这是 §3 那个"和第一个请求并行"的直接后果：**任务创建的时候，那个数字还不存在。**

---

### 7.5 代码：`RateLimit`，和读它的那一行

`src/minicodex/model.py`，在 `ModelHTTPError` 上面加一个类型：

```python
@dataclass(frozen=True)
class RateLimit:
    """How much of the provider's window is left, as the provider reports it.

    Read off the response headers of a *successful* request, which is the only
    moment it is free.  Chapter 12 already parses these headers, but only out
    of a 429 -- by which time the information is a description of the wall the
    program has just hit.  Chapter 17 needs the same numbers before anything
    goes wrong, because the decision it makes is "should this optional work
    happen at all" (F17-02).

    Everything is optional because not every server speaking this API sends
    any of it: a local ollama sends none, and `headroom()` returning `None`
    means "unknown", which the caller must not read as "plenty".
    """

    remaining_requests: int | None = None
    limit_requests: int | None = None
    remaining_tokens: int | None = None
    limit_tokens: int | None = None

    @classmethod
    def from_headers(cls, headers: Any) -> RateLimit | None:
        table = {str(k).lower(): str(v) for k, v in dict(headers).items()}

        def number(name: str) -> int | None:
            raw = table.get(name)
            try:
                return int(raw) if raw is not None else None
            except ValueError:  # pragma: no cover - a provider sending "1k"
                return None

        value = cls(
            remaining_requests=number("x-ratelimit-remaining-requests"),
            limit_requests=number("x-ratelimit-limit-requests"),
            remaining_tokens=number("x-ratelimit-remaining-tokens"),
            limit_tokens=number("x-ratelimit-limit-tokens"),
        )
        return value if value.headroom() is not None else None

    def headroom(self) -> float | None:
        """The tighter of the two windows, as a percentage.  `None` if unknown.

        The *minimum* of requests and tokens, not an average: they are two
        separate buckets and running out of either one stops the program.
        """
        pairs = [
            (self.remaining_requests, self.limit_requests),
            (self.remaining_tokens, self.limit_tokens),
        ]
        seen = [100.0 * left / total for left, total in pairs if left is not None and total]
        return min(seen) if seen else None
```

> - 四个字段全是"可以没有"（`int | None`）：不是每个服务商都发这些头，本地的 Ollama 一个都不发。
> - **`@classmethod from_headers(cls, headers)`**：一个"从响应头造出一个 `RateLimit`"的构造办法。`cls` 就是这个类本身。先把所有头名变成小写放进字典（HTTP 头名不区分大小写）；`number(name)` 取一个头、转成整数，取不到或转不了就是 `None`。四个都没有的话（`headroom()` 算不出来），返回 `None`。
> - **`headroom()`**：剩余的百分比，**取两个桶里紧的那个**（§7.2）。`left is not None and total` 同时排除了"没这个头"和"总数是 0"（除以 0）。

`ChatCompletionsModel.__init__` 里多一个属性：

```python
        self.rate_limit: RateLimit | None = None
```

`stream()` 里，收到 200 之后、读响应体之前：

```python
# Before the first byte of the body: the headers are already
# here, and reading them costs nothing.  Assigned even when
# they are absent -- `None` is the honest answer for a server
# that does not report, and the caller is required to treat it
# as unknown rather than as unlimited.
self.rate_limit = RateLimit.from_headers(resp.headers)
```

```python
def test_F17_02_no_quota_left_means_the_writer_does_not_run(tmp_path: Path) -> None:
    report = asyncio.run(
        run_pipeline(
            _Scripted([]),
            directory=tmp_path / "mem",
            sessions_dir=tmp_path / "sessions",
            jobs_path=tmp_path / "jobs.db",
            headroom=5.0,
        )
    )
    assert report.skipped
    assert "5%" in report.describe()
    assert not (tmp_path / "mem").exists()


def test_F17_02_an_unknown_headroom_is_not_read_as_empty(tmp_path: Path) -> None:
    """`None` means the provider does not report, which ollama does not.

    Treating unknown as zero switches the feature off against every server
    that sends no rate-limit headers; treating it as full is the honest
    default and is written down as a choice rather than left to be inferred.
    """
    report = asyncio.run(
        run_pipeline(
            _Scripted([]),
            directory=tmp_path / "mem",
            sessions_dir=tmp_path / "sessions",
            jobs_path=tmp_path / "jobs.db",
            headroom=None,
        )
    )
    assert not report.skipped


def test_F17_02_the_headers_are_read_off_a_real_response() -> None:
    """Driven through the real `stream()`, not through `from_headers` alone.

    Chapter 6 paid for this distinction: a test that reimplements the parsing
    it is checking stays green when the parsing is deleted.  Mutation testing
    found the same hole here -- `self.rate_limit = None` left every other
    assertion in this file passing, because nothing else in the suite makes
    the client read a response.
    """
    import httpx

    from minicodex.model import ChatCompletionsModel

    body = (
        'data: {"choices":[{"delta":{"content":"ok"},"finish_reason":null}]}\n\n'
        'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}\n\ndata: [DONE]\n\n'
    )

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            text=body,
            headers={
                "content-type": "text/event-stream",
                "x-ratelimit-limit-requests": "10000",
                "x-ratelimit-remaining-requests": "2000",
                "x-ratelimit-limit-tokens": "200000",
                "x-ratelimit-remaining-tokens": "40000",
            },
        )

    model = ChatCompletionsModel(transport=httpx.MockTransport(handler))
    assert model.rate_limit is None

    async def drain() -> None:
        async for _ in model.stream([{"role": "user", "content": "hi"}]):
            pass

    asyncio.run(drain())
    assert model.rate_limit is not None
    assert model.rate_limit.headroom() == pytest.approx(20.0)


def test_F17_02_the_threshold_is_the_tighter_of_the_two_windows() -> None:
    """Requests and tokens are separate buckets; running out of either stops you."""
    limit = RateLimit(
        remaining_requests=9987,
        limit_requests=10000,
        remaining_tokens=20000,
        limit_tokens=200000,
    )
    assert limit.headroom() == pytest.approx(10.0)
    assert limit.headroom() < MIN_RATE_LIMIT_REMAINING_PERCENT

    # A real header set, copied from `probe_memory_write.py quota`.
    healthy = RateLimit.from_headers(
        {
            "x-ratelimit-limit-requests": "10000",
            "x-ratelimit-limit-tokens": "200000",
            "x-ratelimit-remaining-requests": "9987",
            "x-ratelimit-remaining-tokens": "199996",
        }
    )
    assert healthy is not None
    assert healthy.headroom() == pytest.approx(99.87, abs=0.01)

    assert RateLimit.from_headers({"content-type": "application/json"}) is None
```

> - 第三个测试的说明里记着一次失败：第一版测的是"我自己去调 `from_headers`"，于是把 `stream()` 里那一行删掉，测试照样全绿——第 6 章那条"测试重新实现了被测代码"第三次。现在它用 `httpx.MockTransport` 造一个带这些头的假响应，**走真正的 `stream()`**，然后看 `rate_limit` 有没有被设上。

## §8 F17-09：模型只能提案，落盘只有一个人

### 8.1 那个显然的设计，和它为什么不能要

"让模型自己编辑 `MEMORY.md`，反正就是 markdown"——这是最省事的写法，
而它的失败模式第 4 章已经量过（F04-02）：

```python
    """`remember_this`: append a proposal, and nothing else.

    This is the whole of F17-09.  The obvious design -- let the model edit
    `MEMORY.md`, it is only markdown -- gives a model that has been told to add
    one line the ability to rewrite the file it is adding it to, and the
    failure is not that it refuses: it is that it re-emits the parts it
    remembers and drops the rest, silently, in a file nobody diffs.  Chapter
    4's F04-02 with no reviewer.
    """
```

**"没有 reviewer"是关键。** 第 4 章那个"整文件重写偷偷删代码"的故障，
在代码仓库里至少还有 `git diff` 和一个会看 PR 的人。
记忆文件没有人看——它由模型写、由模型读，中间没有人。

### 8.2 于是模型只有一个出口箱

```
notes/1786865509-15548-0.md
```

一次一个文件，文件名带时间和 pid，**永远不覆盖、永远不编辑**。
这是第 7 章的 append-only 用在目录上而不是文件上。

而 `MEMORY.md` 只有一个函数会打开它写：

```python
def write_memory(directory, *, summary, body, now=None, lock_path=MERGE_LOCK) -> Prune:
    """The one function in this program that writes `MEMORY.md`.

    Everything else -- the extractor, the merger, the note tool -- produces a
    proposal.  This validates it, prunes it, writes both files under a lock,
    updates the usage records and commits.  The ordering matters in one place:
    **validation happens before anything is opened for writing**, which is
    chapter 4's two-phase apply (F04-11) at a different scale.  A merge answer
    that parses into no entries at all is refused rather than written, for
    chapter 6's reason about an empty summary (F06-08): that is not a smaller
    memory, it is a destroyed one.
    """
```

三条老规矩在这一个函数里同时出现：**先全部校验再写盘**（第 4 章）、
**空摘要不是短摘要而是被销毁的摘要**（第 6 章）、**单写者持锁**（第 7 章）。

### 8.3 `v1` 那一行是程序的声明，不是模型的

第二段的 prompt 里**没有** `v1`：

```python
def test_F17_09_the_version_line_is_the_programs_claim_not_the_models() -> None:
    """The model is never asked to emit `v1`, so it can never get it wrong."""
    assert FORMAT_VERSION not in STAGE2_SYSTEM
```

模型输出的是两段内容，`v1` 由 `write_memory` 加上。
**永远不要让模型负责声明格式版本**——那是程序对文件格式的断言，
不是一段可以被"忘记"的内容。

### 8.4 这个工具到底有没有人用

第 16 章有一条很硬的结论：**需要模型主动选择进入的阶段，模型就是不进**
（`memory_search` 在 12 次有机会用的运行里只被调了 1 次，第 16 章 §11.4）。那这个 `remember_this` 呢？

同一条约定，两种说法，各三次：

```
"…and nobody commits without it. Please remember that for future sessions."   3/3 写了 note
"…and nobody commits without it."                                             0/3 写了 note
```

结论很干净：

> **`remember_this` 是"请记住"这句话的通道，不是"注意到约定"的机制。**

而"顺口说的约定"由抽取管线负责——`docstrings` / `runner` / `layout` 三条会话
证明它确实抓得到。两条路互补，谁也不能替代谁。

这也解释了为什么这个工具默认就在（只要开了 `--remember`），而第 16 章的 `memory_search` 默认关着、要 `--dedicated-tools` 才有：**这个工具在"用户说了请记住"时 3/3 被调用，那个 12 次里 1 次。**

---

### 8.5 代码：模型的出口箱

```python
# Notes: the only thing a model may add, and it may only add
# ---------------------------------------------------------------------------

NOTE_NAME = "remember_this"

NOTE_SCHEMA: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": NOTE_NAME,
        "description": (
            "Propose one durable fact about this repository for the memory used by "
            "future sessions. Use it when the user states a convention, or when you "
            "find something expensive to discover that will be needed again. Do not "
            "use it for what you did in this session, for anything true only of this "
            "task, or for anything you have not confirmed. The note is a proposal: "
            "it is reviewed and merged later, not written into memory now."
        ),
        "parameters": {
            "type": "object",
            "required": ["note"],
            "properties": {
                "note": {
                    "type": "string",
                    "description": (
                        "One sentence, standing on its own, understandable by someone "
                        "who has not read this conversation. Example: this project is "
                        "run with `uv run`, never with a bare `python`."
                    ),
                }
            },
        },
    },
}

# A note is a sentence.  Anything longer is a session transcript arriving by
# another door, and the merge step reads all of these on every run.
MAX_NOTE_CHARS = 400

# What the system prompt says when the tool is present, and only then --
# chapter 5's F05-10, which measured a model calling a tool the prompt named
# and the policy did not have, 2/3.
#
# The last sentence is the one worth arguing about.  Everything the session
# does is extracted afterwards anyway, so this tool is not the only route into
# memory; it is the route for the things a transcript does not show, which is
# mostly the user saying "always do X".  Saying that plainly is cheaper than
# hoping the model infers the division of labour.
REMEMBER_INSTRUCTIONS = (
    f"You can propose something for this repository's long-term memory with "
    f"`{NOTE_NAME}`. Use it when the user tells you how things are done here, or "
    "when you find something that cost you several steps to discover and will be "
    "needed again. One sentence, written for a session that cannot see this "
    "conversation. Do not use it to record what you did. Everything else in this "
    "session is reviewed for memory automatically afterwards, so a note is only "
    "worth writing for something a reader of the transcript would miss."
)


def remember_instructions() -> str:
    return REMEMBER_INSTRUCTIONS


def note_toolset(directory: Path, *, limit: int = 5) -> ToolSet:
    """`remember_this`: append a proposal, and nothing else.

    This is the whole of F17-09.  The obvious design -- let the model edit
    `MEMORY.md`, it is only markdown -- gives a model that has been told to add
    one line the ability to rewrite the file it is adding it to, and the
    failure is not that it refuses: it is that it re-emits the parts it
    remembers and drops the rest, silently, in a file nobody diffs.  Chapter
    4's F04-02 with no reviewer.

    So the model gets an append-only outbox.  One file per note; the name
    carries time and pid so two processes never choose the same one; nothing is
    ever opened for writing twice.  The merge step reads the directory and is
    the only thing that touches memory itself.

    `limit` is per tool set, which is per run: a model that finds the tool
    rewarding can otherwise turn one session into fifty proposals, and the
    merge step pays for every one of them.
    """
    written = 0

    async def do_note(args: dict[str, Any]) -> str:
        nonlocal written
        note = args.get("note")
        if not isinstance(note, str) or not note.strip():
            return tool_error(
                f'{NOTE_NAME} needs a "note" argument, a non-empty string',
                you_sent=repr(args.get("note")),
                do_this='Example: {"note": "Tests are run with `python -m pytest`."}',
            )
        if written >= limit:
            return tool_error(
                f"{limit} note(s) is the limit for one session",
                do_this="Continue with the task; what you have proposed is enough.",
            )
        body, _ = scrub(note.strip()[:MAX_NOTE_CHARS])
        written += 1
        path = propose(Path(directory), body)
        return (
            f"Proposed. It is not memory yet: it goes to {path.parent.name}/ and is "
            "reviewed when memory is next merged."
        )

    return ToolSet(handlers={NOTE_NAME: do_note}, schemas=[NOTE_SCHEMA])


def propose(directory: Path, note: str, *, now: float | None = None) -> Path:
    """Write one proposal file.  Never overwrites, never edits, never merges."""
    stamp = now if now is not None else time.time()
    notes = Path(directory) / NOTES_DIR
    notes.mkdir(parents=True, exist_ok=True)
    for suffix in range(100):
        name = f"{int(stamp)}-{os.getpid()}-{suffix}.md"
        path = notes / name
        if not path.exists():
            path.write_text(note.strip() + "\n", encoding="utf-8")
            return path
    raise MemoryWriteError(f"cannot find an unused note name in {notes}")  # pragma: no cover


def pending_notes(directory: Path) -> tuple[tuple[Path, str], ...]:
    notes = Path(directory) / NOTES_DIR
    if not notes.is_dir():
        return ()
    out = []
    for path in sorted(notes.glob("*.md")):
        try:
            out.append((path, path.read_text(encoding="utf-8").strip()))
        except OSError:  # pragma: no cover - unreadable proposal is not fatal
            continue
    return tuple(out)


def pending_raw(directory: Path) -> tuple[tuple[Path, str], ...]:
    raw = Path(directory) / RAW_DIR
    if not raw.is_dir():
        return ()
    out = []
    for path in sorted(raw.glob("*.md")):
        try:
            out.append((path, path.read_text(encoding="utf-8")))
        except OSError:  # pragma: no cover
            continue
    return tuple(out)


def write_raw(directory: Path, stage1: Stage1) -> Path:
    raw = Path(directory) / RAW_DIR
    raw.mkdir(parents=True, exist_ok=True)
    path = raw / f"{stage1.session_id or 'unknown'}.md"
    path.write_text(render_stage1(stage1), encoding="utf-8")
    return path
```

> - **`NOTE_SCHEMA`**：工具说明的写法是第 3 章的规矩——什么时候用、什么时候**别**用、参数长什么样、给一个例子。最后一句"这只是一个提议，以后审核、合并，现在不会写进记忆"是说给模型听的：它不该以为写了就生效。
> - **`MAX_NOTE_CHARS = 400`**：一条笔记是一句话。更长的就是"会话记录从另一扇门进来了"。
> - **`REMEMBER_INSTRUCTIONS`**：只在这个工具存在时才加进系统提示词（第 5 章 F05-10）。最后一句讲了分工：会话里的一切事后都会被自动审一遍，所以这个工具只值得用在"读会话记录的人会漏掉的东西"上——主要是用户说"以后都这样"。
> - **`note_toolset`**：和第 16 章的 `memory_toolset` 一样，用一个闭包里的计数器（`nonlocal written`）限制次数：一次会话最多 5 条。参数不对返回第 3 章格式的错误，**不扣次数**。笔记先截到 400 字、再脱敏，然后交给 `propose`。
> - **`propose`**：写一个文件，**永远不覆盖**。文件名是"时间戳-pid-序号"；`for suffix in range(100)` 找第一个不存在的名字。同一秒、同一个进程写两条，就是 `-0`、`-1`。
> - **`pending_notes`**、**`pending_raw`**：读出 `notes/`、`raw/` 里所有的文件，按文件名排序（于是按时间排序）。
> - **`write_raw`**：第一段的结果写成 `raw/<会话 id>.md`。

`composition.py` 的导入区加 `from minicodex.memory_write import note_toolset`，`top_level_tools` 多一个参数 `remember: Path | None = None`，函数末尾：

```python
# Chapter 17's one model-facing tool, and the only write access a model has
# to memory anywhere in this program: it appends a proposal to `notes/` and
# cannot touch `MEMORY.md` (F17-09).  A separate switch from `memory=`
# because reading and writing are separate consents -- a user may well want
# the agent to use what it knows without it recording anything new.
if remember is not None:
    tools = tools.plus(note_toolset(remember))
```

`__main__.py` 的 `_instructions` 多一段，和计划、记忆那两段一样"工具在才说"：

```python
# Chapter 17's note tool, on the same condition and for the same reason,
# and read off the tool table rather than off a flag of its own: writing is
# a separate consent from reading, so `--remember` without `--memory` is a
# real configuration and the prompt has to describe that one too.
if tools is not None and NOTE_NAME in tools.handlers:
    parts.append(remember_instructions())
```

### 8.6 代码：第二段，和唯一写记忆的那个函数

第二段的提示词：

```python
SUMMARY_MARK = "=== memory_summary.md ==="
BODY_MARK = "=== MEMORY.md ==="

STAGE2_SYSTEM = f"""\
You maintain one repository's memory: a short resident summary that is sent
with every request, and a longer body of sections that is consulted when
needed. You are given what is there now, new material extracted from recent
sessions, notes proposed during those sessions, and any edits a human made to
the files by hand.

Produce the new contents of both files.

Rules:

- **Human edits win and are preserved.** If the diff of hand edits shows a line
  a person wrote, changed or removed, honour it. Do not restore something a
  person deleted. Do not reword something a person wrote.
- Merge duplicates. If new material says the same thing as an existing entry,
  keep one, worded as precisely as the better of the two.
- If new material contradicts an existing entry, the new material is more
  recent and wins -- but say so in one clause, so the change is visible.
- Never invent. Every line must come from something you were given.
- No credentials, tokens or keys, ever, in either file.
- The summary holds only what is worth sending on every single request: at most
  eight short bullets. Everything else belongs in a section of the body.
- The body is `## Heading` sections, each a few lines, each about one thing.
  **Name each section after its subject** -- `## Running tests`,
  `## Where new code goes` -- because the heading is what a future session
  searches for and cites.
- The headings in the material you were given (`Preference signals`,
  `Reusable knowledge`, `Failures and how to do differently`) are the
  extractor's filing categories. They are not subjects and must not appear in
  what you write.
- Say each thing once. If a preference and a failure are the same rule from two
  angles, they are one section.
- Keep it small. Drop any line a competent engineer opening this repository
  could work out for themselves in thirty seconds -- what a file contains, what
  a function does, that code should have tests.

Answer with exactly this shape and nothing else -- no code fences, no
commentary:

{SUMMARY_MARK}
- ...
{BODY_MARK}
## ...

...
"""
```

> - 两个**分隔标记**：模型的回答必须是"摘要标记、摘要、正文标记、正文"这个形状，程序按标记切开。
> - 规则里最重的一条是第一条：**人手改的东西优先，并且保留**——不要恢复人删掉的，不要改写人写的。§9 量过它靠什么才做得到。
> - "标题要按主题起名"那两条，是因为第一段的三个小节名（偏好/知识/失败）是**抽取器的归档分类**，不是主题；合并时要把它们换成 `## Running tests` 这样的、以后会被搜索和引用的名字。

```python
@dataclass(frozen=True)
class Merged:
    """The proposed new contents of the two files, before anything is written."""

    summary: str
    body: str
    secrets_removed: int = 0

    def entries(self) -> tuple[Entry, ...]:
        return parse_entries(self.body)


def parse_merged(text: str) -> Merged:
    """Split the merge answer into the two files.

    Refuses rather than guesses.  A merge answer that does not carry both
    markers is one this program cannot place, and the tempting fallback --
    treat the whole thing as the body -- writes the model's commentary into a
    file that is injected into every future request.
    """
    if SUMMARY_MARK not in text or BODY_MARK not in text:
        raise MemoryWriteError(
            f"merge answer is missing its markers ({SUMMARY_MARK} / {BODY_MARK}); "
            f"nothing was written. First 200 characters: {text[:200]!r}"
        )
    _, rest = text.split(SUMMARY_MARK, 1)
    summary, body = rest.split(BODY_MARK, 1)
    summary, hits_a = scrub(summary.strip())
    body, hits_b = scrub(body.strip())
    return Merged(summary=summary, body=body, secrets_removed=hits_a + hits_b)


def merge_request(
    *,
    memory: Memory,
    raw: Sequence[tuple[Path, str]],
    notes: Sequence[tuple[Path, str]],
    hand_edits: str,
) -> str:
    """Everything stage 2 is shown, as one message.

    The hand-edit diff is last and labelled, because it is the input with the
    highest authority and the one most easily read as more of the same.
    """
    parts = [
        "# Memory as it stands\n",
        f"## {SUMMARY_FILE}\n{memory.summary or '(empty)'}",
        f"## {BODY_FILE}\n" + ("\n\n".join(e.render() for e in memory.entries) or "(empty)"),
        "\n# New material extracted from recent sessions\n",
    ]
    parts.extend(text for _, text in raw)
    if notes:
        parts.append("\n# Notes proposed during those sessions\n")
        parts.extend(f"- {text}" for _, text in notes)
    if hand_edits.strip():
        parts.append(
            "\n# Edits a human made by hand since the last merge (git diff)\n"
            "These are the user's own words. Preserve them.\n\n" + hand_edits
        )
    return "\n\n".join(parts)


async def consolidate(
    model: Any,
    *,
    memory: Memory,
    raw: Sequence[tuple[Path, str]],
    notes: Sequence[tuple[Path, str]] = (),
    hand_edits: str = "",
    system: str = STAGE2_SYSTEM,
) -> Merged:
    """Stage 2: one model call, and the answer is a proposal, not a write."""
    answer = await _ask(
        model,
        [
            {"role": "system", "content": system},
            {
                "role": "user",
                "content": merge_request(
                    memory=memory, raw=raw, notes=notes, hand_edits=hand_edits
                ),
            },
        ],
    )
    return parse_merged(answer)
```

> - **`Merged`**：合并的结果，还没写盘。`entries()` 用第 16 章的 `parse_entries` 把正文切成小节。
> - **`parse_merged`**：两个标记缺一个就**拒绝**，什么都不写。说明里写了那个诱人的退路——"没标记就把整段当正文"——会把模型的客套话写进每次都要发出去的文件里。`text.split(SUMMARY_MARK, 1)`：只在第一个标记处切一刀。切出来的两段各脱敏一次。
> - **`merge_request`**：给第二段看的那一整条消息。顺序是：现在的记忆、新抽取的材料、模型提的笔记、**最后是用户手改的 diff**，带一句"这是用户自己的话，保留它们"。放最后、带标签，是因为它权威最高，又最容易被当成"更多的同类材料"读过去。
> - **`consolidate`**：一次模型调用，返回的是**提议**，不是写盘。

写盘：

```python
def _lock(path: Path = MERGE_LOCK) -> int:
    """chapter 7's `O_EXCL` lock, copied deliberately.  See `MERGE_LOCK`."""
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        holder = ""
        try:
            holder = path.read_text(encoding="utf-8", errors="replace").strip()
        except OSError:  # pragma: no cover
            pass
        raise MemoryWriteError(
            f"memory is being merged by another minicodex (pid {holder or '?'}). "
            f"Nothing was written. Delete {path} if that process is gone."
        ) from None
    os.write(fd, str(os.getpid()).encode())
    return fd


def _unlock(fd: int, path: Path = MERGE_LOCK) -> None:
    os.close(fd)
    try:
        path.unlink()
    except OSError:  # pragma: no cover
        pass


def write_memory(
    directory: Path,
    *,
    summary: str,
    body: str,
    now: float | None = None,
    lock_path: Path | None = MERGE_LOCK,
) -> Prune:
    """The one function in this program that writes `MEMORY.md`.

    Everything else -- the extractor, the merger, the note tool -- produces a
    proposal.  This validates it, prunes it, writes both files under a lock,
    updates the usage records and commits.  The ordering matters in one place:
    **validation happens before anything is opened for writing**, which is
    chapter 4's two-phase apply (F04-11) at a different scale.  A merge answer
    that parses into no entries at all is refused rather than written, for
    chapter 6's reason about an empty summary (F06-08): that is not a smaller
    memory, it is a destroyed one.

    `lock_path=None` means the caller already holds the lock, which is what
    `run_pipeline` does: it has to hold it across the whole of stage 2, not
    only across this write (see the comment there).
    """
    stamp = now if now is not None else time.time()
    directory = Path(directory)
    entries = parse_entries(body)
    if not entries and not summary.strip():
        raise MemoryWriteError(
            "the merge produced neither a summary nor a section; refusing to "
            "replace the existing memory with nothing"
        )

    counts = _usage(directory)
    kept = prune(entries, counts, now=stamp)
    summary = trim_summary(summary.strip(), kept.kept)

    fd = _lock(lock_path) if lock_path is not None else None
    try:
        directory.mkdir(parents=True, exist_ok=True)
        (directory / SUMMARY_FILE).write_text(f"{FORMAT_VERSION}\n\n{summary}\n", encoding="utf-8")
        body_text = "\n\n".join(entry.render() for entry in kept.kept)
        (directory / BODY_FILE).write_text(
            f"{FORMAT_VERSION}\n\n{body_text}\n" if body_text else f"{FORMAT_VERSION}\n",
            encoding="utf-8",
        )
        _record_first_seen(directory, kept, counts, now=stamp)
    finally:
        if fd is not None and lock_path is not None:
            _unlock(fd, lock_path)
    return kept
```

> - **`_lock`**：第 7 章的锁，第二份（§2.4）。`O_CREAT | O_EXCL`：文件已经存在就失败。失败时读出里面的 pid，报错说"另一个 minicodex 正在合并（pid ...），如果那个进程已经没了，删掉这个文件"。成功就把自己的 pid 写进去。
> - **`_unlock`**：关掉、删掉。
> - **`write_memory`**：
>   1. **先校验**：正文切不出任何小节、摘要也是空的——拒绝。那不是一份小一点的记忆，是一份被销毁的记忆（第 6 章 F06-08）。
>   2. 读引用计数，**遗忘**（§10 的 `prune`），把摘要**裁到第 16 章的上限**（§10 的 `trim_summary`）。
>   3. 拿锁（`lock_path=None` 表示调用者已经拿着了，§8.7），写两个文件，第一行由**程序**写上 `v1`（§8.3），更新 `usage.json`，放锁。

```python
def test_F17_09_the_note_tool_cannot_touch_the_memory_files(tmp_path: Path) -> None:
    """The whole of F17-09 in one assertion.

    `remember_this` is the only write access a model has anywhere in this
    program, and what it writes is a file in `notes/` that nothing reads as
    memory.  Letting it edit `MEMORY.md` instead is chapter 4's F04-02 with no
    reviewer: a model asked to add one line rewrites the file and silently
    drops the parts it did not re-emit.
    """
    write_memory(tmp_path, summary="- a", body="## A\n\nb\n", lock_path=tmp_path / "merge.lock")
    before = (tmp_path / BODY_FILE).read_text(encoding="utf-8")
    tools = note_toolset(tmp_path)
    answer = asyncio.run(tools.handlers[NOTE_NAME]({"note": "The linter is `ruff check --fix`."}))

    assert "Proposed" in answer
    assert (tmp_path / BODY_FILE).read_text(encoding="utf-8") == before
    assert (tmp_path / SUMMARY_FILE).exists()
    notes = pending_notes(tmp_path)
    assert len(notes) == 1
    assert notes[0][1] == "The linter is `ruff check --fix`."
    assert notes[0][0].parent.name == NOTES_DIR


def test_F17_09_a_note_never_overwrites_another_note(tmp_path: Path) -> None:
    """Append-only, applied to a directory instead of to a file (chapter 7)."""
    first = propose(tmp_path, "one", now=1000.0)
    second = propose(tmp_path, "two", now=1000.0)
    assert first != second
    assert {p.read_text(encoding="utf-8").strip() for p, _ in pending_notes(tmp_path)} == {
        "one",
        "two",
    }


def test_F17_09_a_note_is_scrubbed_and_bounded(tmp_path: Path) -> None:
    tools = note_toolset(tmp_path)
    asyncio.run(tools.handlers[NOTE_NAME]({"note": "token ghp_abcdefghijklmnopqrst " + "x" * 900}))
    text = pending_notes(tmp_path)[0][1]
    assert "ghp_abcdefghijklmnopqrst" not in text
    assert len(text) <= 400


def test_F17_09_one_session_may_not_propose_fifty_notes(tmp_path: Path) -> None:
    tools = note_toolset(tmp_path, limit=2)
    for _ in range(2):
        asyncio.run(tools.handlers[NOTE_NAME]({"note": "a rule"}))
    refusal = asyncio.run(tools.handlers[NOTE_NAME]({"note": "a rule"}))
    assert "limit" in refusal
    assert len(pending_notes(tmp_path)) == 2


def test_F17_09_a_bad_argument_is_chapter_threes_three_part_error(tmp_path: Path) -> None:
    tools = note_toolset(tmp_path)
    answer = asyncio.run(tools.handlers[NOTE_NAME]({"note": ""}))
    assert "you sent" in answer.lower()
    assert "Example:" in answer
    assert pending_notes(tmp_path) == ()


def test_F17_09_the_writer_refuses_to_replace_a_memory_with_nothing(tmp_path: Path) -> None:
    """Chapter 6's empty-summary rule (F06-08) on a file that outlives the run.

    An empty merge answer is not a smaller memory, it is a destroyed one, and
    validation happens before anything is opened for writing -- chapter 4's
    two-phase apply at a different scale.
    """
    write_memory(tmp_path, summary="- a", body="## A\n\nb\n", lock_path=tmp_path / "merge.lock")
    with pytest.raises(MemoryWriteError):
        write_memory(tmp_path, summary="  ", body="", lock_path=tmp_path / "merge.lock")
    assert "## A" in (tmp_path / BODY_FILE).read_text(encoding="utf-8")


def test_F17_09_a_merge_answer_without_its_markers_is_refused() -> None:
    """The tempting fallback -- treat the whole thing as the body -- writes the
    model's commentary into a file injected into every future request."""
    with pytest.raises(MemoryWriteError) as caught:
        parse_merged("Sure! Here is your updated memory:\n\n## Tests\n\nRun pytest.\n")
    assert "markers" in str(caught.value)


def test_F17_09_what_is_written_is_readable_by_chapter_sixteen(tmp_path: Path) -> None:
    """The two halves of the subsystem meet at one file format, so the write
    path's output is checked with the read path's parser."""
    write_memory(
        tmp_path,
        summary="- Run tests with `python -m pytest`.",
        body="## Running tests\n\nUse `python -m pytest`.\n\n## Layout\n\nHelpers live in util/.\n",
        lock_path=tmp_path / "merge.lock",
    )
    memory = load(tmp_path)
    assert memory.summary == "- Run tests with `python -m pytest`."
    assert [e.title for e in memory.entries] == ["Running tests", "Layout"]
    assert (tmp_path / SUMMARY_FILE).read_text(encoding="utf-8").startswith(FORMAT_VERSION)
    assert (tmp_path / BODY_FILE).read_text(encoding="utf-8").startswith(FORMAT_VERSION)


def test_F17_09_the_version_line_is_the_programs_claim_not_the_models() -> None:
    """The model is never asked to emit `v1`, so it can never get it wrong."""
    assert FORMAT_VERSION not in STAGE2_SYSTEM
    assert SUMMARY_MARK in STAGE2_SYSTEM
    assert BODY_MARK in STAGE2_SYSTEM


def test_F17_03_the_merge_lock_names_the_process_holding_it(tmp_path: Path) -> None:
    """Chapter 7's lock, second implementation, same refusal.

    The message has to name the pid: a stale lock is otherwise a program that
    stopped working with no way to find out why.
    """
    lock = tmp_path / "merge.lock"
    write_memory(tmp_path / "mem", summary="- a", body="## A\n\nb\n", lock_path=lock)
    assert not lock.exists(), "the lock is released when the write finishes"

    lock.write_text("4242", encoding="utf-8")
    with pytest.raises(MemoryWriteError) as caught:
        write_memory(tmp_path / "mem", summary="- a", body="## A\n\nb\n", lock_path=lock)
    assert "4242" in str(caught.value)
    assert "Nothing was written" in str(caught.value)
```

### 8.7 改写时发现：锁只锁住了"写"，没锁住"读了再写"

`write_memory` 在写的时候拿锁。听起来够了。改写时我把这件事换了个问法：**两个 `minicodex` 同时启动，各自带着 `--remember`，会发生什么？**

第一段没问题：任务表保证它们认领到不同的会话（§4）。第二段是这样的：

1. 进程 A 读出现在的记忆、读出 `raw/` 里的材料，然后**花一次模型调用**去合并——几秒钟。
2. 这几秒里，进程 B 抽取完它的会话，也读出记忆（**还是旧的**）和 `raw/`，合并，拿锁，写盘，放锁，删掉它用过的 `raw/` 文件。
3. A 的模型调用回来了。A 拿锁，写盘——**它的结果是在旧记忆上合出来的**，B 刚写进去的东西不在里面。

用两个假模型把这个顺序摆出来（A 的合并卡住，等 B 做完再放行）：

```
A memory writer: 1 session(s) read, 1 entr(ies), nothing forgotten in 0.4s
B memory writer: 1 session(s) read, 1 entr(ies), nothing forgotten in 0.0s
sections on disk: ['Tooling A']
raw left: []
```

**两个都报告成功。B 那次会话学到的东西，两个文件里都没有，它的 `raw/` 文件也被 B 自己删了。** 什么都没剩下。

这是数据库里叫"丢失更新"的那种错：两个人各自"读—改—写"，后写的那个覆盖了先写的。锁只包住了"写"，没包住"读了再写"。

修法：**第二段从第一次读到最后一次删，全程拿着锁**。拿不到锁的那个进程**不等、也不报错**——它的抽取结果已经在 `raw/` 里了，正是拿着锁的那个进程（或者下一次运行）会去找的地方。

`run_pipeline` 的后半段（完整的函数在 §13.4）：

```python
def report(**extra: Any) -> WriteReport:
    return WriteReport(
        sessions=tuple(read),
        empty=tuple(empty),
        secrets_removed=secrets,
        seconds=time.monotonic() - began,
        failed=tuple(failed),
        **extra,
    )

# Stage 2 runs under the merge lock from the first read to the last
# delete, not only around the write. The first version took the lock
# inside `write_memory`, so two processes could each read the memory, each
# spend a model call merging, and then write one after the other: the
# second write was built on a memory from before the first, and silently
# replaced it. Measured with two overlapping runs over two sessions: both
# reported success, one session's material was in neither file, and its
# `raw/` file had been deleted by the run whose write was then overwritten.
#
# A run that finds the lock taken does not wait for it and does not fail.
# Its extraction is already in `raw/`, which is exactly where the run
# holding the lock -- or the next one -- will look.
try:
    fd = _lock(lock_path)
except MemoryWriteError:
    return report(deferred="another minicodex")
try:
    raw = pending_raw(directory)
    notes = pending_notes(directory)
    if not raw and not notes:
        return report()

    ensure_repo(directory)
    write_gitignore(directory)
    from minicodex.memory import load as load_memory

    # Read here, under the lock, and never taken from the caller. A
    # memory loaded when the session started is one some other process
    # may have rewritten since, which is the lost update above by a
    # second route.
    current = load_memory(directory)
    merged = await consolidate(
        model, memory=current, raw=raw, notes=notes, hand_edits=hand_edits(directory)
    )
    pruned = write_memory(
        directory, summary=merged.summary, body=merged.body, now=stamp, lock_path=None
    )
    commit(directory, f"memory: {len(raw)} session(s), {len(notes)} note(s)")
    # Consumed only once the write succeeded.  The other order loses the
    # extraction work on any failure in the merge, and the extraction is the
    # expensive half.
    for path, _ in (*raw, *notes):
        try:
            path.unlink()
        except OSError:  # pragma: no cover
            pass
finally:
    _unlock(fd, lock_path)
```

> - **`report(**extra)`**：一个局部的小函数，造一份报告；四个返回点都用它，只是各自多带一两个字段。`**extra` 把关键字参数原样传进 `WriteReport(...)`。
> - **`fd = _lock(lock_path)` 在读任何东西之前。** 拿不到（`MemoryWriteError`）就返回一份 `deferred="another minicodex"` 的报告。
> - **`current = load_memory(directory)`**：在锁里面重新从磁盘读。以前这里会优先用调用者传进来的 `memory`——那是**这次会话启动时**读的，等走到这一步，别的进程可能已经改过了。同一个丢失更新，换了一条路。于是 `run_pipeline` 的 `memory` 参数删掉了，`__main__` 和 Web 控制台里传它的地方也删了。
> - `write_memory(..., lock_path=None)`：锁已经在手里，别再拿一次（同一个进程再 `O_EXCL` 一次会失败）。
> - **`finally: _unlock(...)`**：不管怎么结束——正常写完、没东西可合、合并时出错——都放锁。

`WriteReport` 多一个字段 `deferred`，`describe()` 里多一种说法：

```python
        elif self.deferred:
            parts.append(f"merge left to {self.deferred}")
```

同样的两个进程，修完之后：

```
A memory writer: 1 session(s) read, 1 entr(ies), nothing forgotten in 0.4s
B memory writer: 1 session(s) read, merge left to another minicodex in 0.0s
sections on disk: ['Tooling A']
raw left: ['b.md']
```

B 的材料在 `raw/` 里等着，下一次运行把它合进去。

```python
class _GatedMerge:
    """Stage 1 answers at once with `fact`; stage 2 waits for `gate`, then
    keeps every section it was shown and adds `title`."""

    tools: ClassVar[list[dict[str, Any]]] = []

    def __init__(self, fact: str, title: str, gate: asyncio.Event | None = None) -> None:
        self.fact, self.title, self.gate = fact, title, gate

    async def _stream(self, messages: Any) -> Any:
        from minicodex.model import Completed, TextDelta

        if "maintain one repository's memory" in messages[0]["content"]:
            if self.gate is not None:
                await self.gate.wait()
            current = [e.title for e in load(self.directory).entries]
            sections = "\n\n".join(f"## {t}\n\nkept." for t in [*current, self.title])
            yield TextDelta(f"{SUMMARY_MARK}\n- s\n{BODY_MARK}\n{sections}\n")
        else:
            yield TextDelta(f"DURABLE: yes\n## Preference signals\n- {self.fact}\n")
        yield Completed("stop")

    def stream(self, messages: Any) -> Any:
        return self._stream(messages)


def test_two_overlapping_writers_do_not_lose_a_merge(tmp_path: Path) -> None:
    """The lock used to be taken inside `write_memory` only.

    Two runs each read the memory, each spent a model call merging, and then
    wrote one after the other: the later write was built on a memory from
    before the earlier one. Both reported success, one session's material
    was in neither file, and its `raw/` file had been deleted. Now the lock
    covers all of stage 2, and a run that finds it taken leaves its `raw/`
    file for whoever holds it.
    """
    sessions = tmp_path / "sessions"
    _write_session(sessions, "a", ["use uv"])
    _write_session(sessions, "b", ["use ruff"])
    directory = tmp_path / "mem"
    kw: dict[str, Any] = {
        "directory": directory,
        "sessions_dir": sessions,
        "jobs_path": tmp_path / "jobs.db",
        "lock_path": tmp_path / "merge.lock",
        "limit": 1,
    }

    async def scenario() -> tuple[Any, Any]:
        gate = asyncio.Event()
        slow, fast = (
            _GatedMerge("Use uv.", "Tooling A", gate),
            _GatedMerge("Use ruff.", "Tooling B"),
        )
        slow.directory = fast.directory = directory  # type: ignore[attr-defined]
        first = asyncio.create_task(run_pipeline(slow, **kw))
        await asyncio.sleep(0.2)  # the first run is inside its merge, lock held
        second = await run_pipeline(fast, **kw)
        gate.set()
        return await first, second

    first, second = asyncio.run(scenario())
    assert first.merged
    assert second.deferred and not second.merged
    assert "merge left to" in second.describe()
    # The second run's extraction is waiting, not lost...
    assert [p.name for p, _ in pending_raw(directory)] == ["b.md"]
    # ...and the next run merges it on top of the first run's result.
    final = _GatedMerge("", "Tooling B")
    final.directory = directory  # type: ignore[attr-defined]
    asyncio.run(run_pipeline(final, **kw))
    assert [e.title for e in load(directory).entries] == ["Tooling A", "Tooling B"]
```

> - **`_GatedMerge`**：一个假模型。被问到第一段（抽取）时马上回答；被问到第二段（看系统提示词里有没有 "maintain one repository's memory"）时，先 `await self.gate.wait()`——等测试放行——然后把**当前磁盘上**已有的小节和自己的那一节一起交回去，像一个称职的合并那样。
> - 测试的顺序：A 启动；等 0.2 秒，让 A 走进合并、拿着锁、卡在 `gate` 上；B 跑完（拿不到锁，延后）；放行 A。断言 A 合并了、B 延后了、`b.md` 还在。最后再跑一次，`b.md` 被合进去，两节都在。
> - `asyncio.create_task(...)` 和 `asyncio.Event()`：两个"进程"在这里是同一个事件循环里的两个任务。锁是文件锁，同一个进程里也一样生效。

这一条为什么第一次写这一章时没撞上？因为这一章所有的测量都是**一个进程**跑的。§4 为"两个进程同时启动"专门造了一张表，然后整条管线只在第一段用到了它。

## §9 F17-08：把合并冲突交给一个已经解决它的工具

### 9.1 清单的药方

> F17-08 增量合并时把用户手改的内容整段吞掉
> → 把记忆目录做成版本库，**拿 diff 当合并的输入**

codex 把 `~/.codex/memories/` 自己 `git init` 出来，Phase 2 读
`phase2_workspace_diff.md`。照做：

```python
def ensure_repo(directory: Path) -> bool:
    """Make the memory directory a git repository, if it is not one already.

    codex does the same thing to `~/.codex/memories/`, initialising a git
    baseline under the memory root and diffing the worktree against it to
    build the input its consolidation agent reads.  The reason is F17-08
    rather than version control for its own sake: **the merge step needs to
    know what a human changed**, and "what changed since the last time this
    program wrote the file" is exactly what `git diff` answers.  Writing that
    from scratch means storing a shadow copy and diffing it, which is storing a
    shadow copy and diffing it.
    """
```

`hand_edits()` 就是 `git diff`——**工作区里所有没提交的东西，按定义就不是这个程序写的**，
因为它写和提交是同一个动作。

### 9.2 测量：diff 到底买到了什么

两条臂，同样的输入：一份记忆，用户手改了两处（加了一条自己的规则，
删掉了一条他不认同的），然后来了一批新材料，其中**恰好又提到了那条被删的**。

```
$ uv run python probe_memory_write.py handedits --samples 3

== with the diff ==  (19 diff lines)
  [0] kept both hand edits: True   restored the deleted section: False
  [1] kept both hand edits: True   restored the deleted section: False
  [2] kept both hand edits: True   restored the deleted section: False
== without the diff ==  (19 diff lines)
  [0] kept both hand edits: True   restored the deleted section: True
  [1] kept both hand edits: True   restored the deleted section: True
  [2] kept both hand edits: True   restored the deleted section: True
```

读这张表要小心，因为**两列的结论相反**：

- **用户加的内容：两条臂都保住了，3/3 vs 3/3。** diff 在这里一点用没有——
  因为改过的文件本来就摆在合并的面前，加的东西就在里面。
- **用户删的内容：带 diff 0/3 恢复，不带 diff 3/3 恢复。**

> **一个被删掉的东西不在文件里——这就是"删掉"的意思。
> 所以 diff 是它曾经存在过的唯一证据。**

这就是那个 git 仓库买到的全部东西：**一列**。清单说的"整段吞掉"没有复现，
真正成立的是它的反面——**没有 diff，模型会把用户删掉的东西请回来，而且理直气壮**
（新材料确实又提到了它）。

### 9.3 顺带测掉的另一半：整文件重写会不会丢东西

第 4 章 F04-02 的形状：合并是一次整文件重写，会不会丢？

```
== survival of a larger memory across one merge ==
  [0] 12/12 of the existing sections survived the rewrite
  [1] 12/12 of the existing sections survived the rewrite
  [2] 12/12 of the existing sections survived the rewrite
```

**12 节全活。** 在这个规模上没复现。记下来，也记下它的边界：
12 节是我测的规模，40 节（`MAX_ENTRIES`）没测。

### 9.4 一个 Windows 上的小坑，来自 git 自己

写完 §9 的探针，第一次跑就炸在清理代码上：

```
PermissionError: [WinError 5] 拒绝访问。:
  '...\\.probe\\ch17\\handedit-with\\.git\\objects\\14\\3dd282fc855caea...'
```

`.git/objects` 下的文件是 0444 的，Windows 认真执行了这个权限。

```python
def _force(func: Any, path: str, _exc: BaseException) -> None:
    """Delete a file git made read-only.
    ...
    The chapter's own scratch cleanup was the first thing the git repository
    broke.
    """
    os.chmod(path, 0o600)
    func(path)
```

**引入一个新依赖，第一个被它绊倒的通常不是主流程。**

---

### 9.5 代码：五个小函数包住 `git`

```python
# git: the merge conflict problem, handed to something that has solved it
# ---------------------------------------------------------------------------


def _git(directory: Path, *args: str, check: bool = False) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(directory), *args],
        capture_output=True,
        text=True,
        check=check,
        timeout=30,
    )


def git_available() -> bool:
    try:
        return _git(Path("."), "--version").returncode == 0
    except (OSError, subprocess.SubprocessError):  # pragma: no cover - git missing
        return False


def ensure_repo(directory: Path) -> bool:
    """Make the memory directory a git repository, if it is not one already.

    codex does the same thing to `~/.codex/memories/`, initialising a git
    baseline under the memory root and diffing the worktree against it to
    build the input its consolidation agent reads.  The reason is F17-08
    rather than version control for its own sake: **the merge step needs to
    know what a human changed**, and "what changed since the last time this
    program wrote the file" is exactly what `git diff` answers.  Writing that
    from scratch means storing a shadow copy and diffing it, which is storing a
    shadow copy and diffing it.

    Returns False when git is not available, and everything downstream then
    degrades to "no hand edits detected" -- which is the safe direction: the
    merge prompt's instruction to preserve what a person wrote still applies to
    the memory it is shown, it simply has no diff to point at.
    """
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    if (directory / ".git").exists():
        return True
    if not git_available():
        return False
    if _git(directory, "init", "--quiet").returncode != 0:  # pragma: no cover
        return False
    # A repository nobody configured cannot commit: `user.email` and
    # `user.name` are not set in CI images, and the failure appears at the
    # first commit rather than here.  Set locally, never globally.
    _git(directory, "config", "user.email", "minicodex@localhost")
    _git(directory, "config", "user.name", "minicodex")
    _git(directory, "config", "commit.gpgsign", "false")
    return True


def hand_edits(directory: Path) -> str:
    """What changed in the memory files since this program last committed.

    Everything in the working tree that is not committed is, by definition,
    not this program's work: it writes and commits in one operation.  So the
    diff is the user's -- or another tool's, or a merge conflict someone left
    behind -- and the merge prompt is told to treat it as authority.

    Empty string when git is unavailable or the repository has no commits yet.
    """
    directory = Path(directory)
    if not (directory / ".git").exists():
        return ""
    result = _git(directory, "diff", "--", SUMMARY_FILE, BODY_FILE)
    if result.returncode != 0:  # pragma: no cover - a broken repository is not fatal
        return ""
    return result.stdout.strip()


def commit(directory: Path, message: str) -> bool:
    """Commit the two memory files.  Never raises; failure means no history."""
    directory = Path(directory)
    if not (directory / ".git").exists():
        return False
    # Only the two files.  `raw/` and `notes/` are temporary by construction
    # and a repository full of them is a repository whose log is unreadable.
    _git(directory, "add", "--", SUMMARY_FILE, BODY_FILE)
    result = _git(directory, "commit", "--quiet", "-m", message)
    return result.returncode == 0


def write_gitignore(directory: Path) -> None:
    """Keep the temporary directories out of the repository the merge diffs."""
    path = Path(directory) / ".gitignore"
    if path.exists():
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"{RAW_DIR}/\n{NOTES_DIR}/\n{USAGE_FILE}\n", encoding="utf-8")
```

> - **`_git(directory, *args)`**：`subprocess.run(["git", "-C", 目录, ...])`。`-C 目录` 让 git 在那个目录里执行，不用 `cd`。`timeout=30`：一个卡住的 git 不能卡住整个程序。
> - **`git_available`**：`git --version` 能跑就算有。没装 git 时 `subprocess.run` 抛 `OSError`。
> - **`ensure_repo`**：目录还不是仓库就 `git init`。然后在**这个仓库里**设置提交人名字和邮箱（不碰全局配置）——不设的话，在没配置过 git 的机器上（比如 CI），第一次提交才会失败，离原因很远。没有 git 就返回 `False`，后面的一切退化成"没检测到手改"，这是安全的方向。
> - **`hand_edits`**：`git diff -- 两个文件`。说明里那句是整个机制的关键：这个程序**写和提交是同一个动作**，所以工作区里任何没提交的改动，按定义都不是它写的。
> - **`commit`**：只 `add` 两个记忆文件。`raw/`、`notes/` 是临时的，不进历史。失败（比如什么都没变）不报错，只返回 `False`。
> - **`write_gitignore`**：把临时目录和 `usage.json` 写进 `.gitignore`。已经有了就不动——用户可能改过它。

```python
@needs_git
def test_F17_08_a_hand_edit_shows_up_as_a_diff(tmp_path: Path) -> None:
    """What the merge is shown about what a person did.

    Everything uncommitted is by definition not this program's work: it writes
    and commits in one operation.
    """
    directory = tmp_path / "mem"
    assert ensure_repo(directory)
    write_memory(
        directory, summary="- a", body="## A\n\noriginal\n", lock_path=tmp_path / "merge.lock"
    )
    assert commit(directory, "baseline")
    assert hand_edits(directory) == ""

    (directory / BODY_FILE).write_text(
        f"{FORMAT_VERSION}\n\n## A\n\noriginal\nand a line a person added\n", encoding="utf-8"
    )
    diff = hand_edits(directory)
    assert "+and a line a person added" in diff


@needs_git
def test_F17_08_a_deletion_is_only_visible_in_the_diff(tmp_path: Path) -> None:
    """The measured reason the git repository is not ceremony.

    An *addition* survives a merge whether or not there is a diff, because the
    edited file is what the merge is shown.  A **deletion** is not in the file
    -- that is what deleting means -- so the diff is the only evidence it ever
    happened.  Measured against gpt-4o-mini: with the diff, the deleted
    section stayed deleted 3/3; without it, the merge put it back 3/3.
    """
    directory = tmp_path / "mem"
    ensure_repo(directory)
    write_memory(
        directory,
        summary="- a",
        body="## Keep\n\nkept\n\n## Drop\n\nthe user does not want this\n",
        lock_path=tmp_path / "merge.lock",
    )
    commit(directory, "baseline")
    (directory / BODY_FILE).write_text(f"{FORMAT_VERSION}\n\n## Keep\n\nkept\n", encoding="utf-8")

    diff = hand_edits(directory)
    assert "-the user does not want this" in diff
    request = merge_request(memory=load(directory), raw=(), notes=(), hand_edits=diff)
    assert "the user does not want this" in request
    assert "These are the user's own words" in request
    # ...and with no diff, nothing anywhere in the request mentions it.
    assert "the user does not want this" not in merge_request(
        memory=load(directory), raw=(), notes=(), hand_edits=""
    )


@needs_git
def test_F17_08_the_writer_commits_what_it_wrote(tmp_path: Path) -> None:
    directory = tmp_path / "mem"
    ensure_repo(directory)
    write_memory(directory, summary="- a", body="## A\n\nb\n", lock_path=tmp_path / "merge.lock")
    assert commit(directory, "first")
    log = subprocess.run(
        ["git", "-C", str(directory), "log", "--oneline"],
        capture_output=True,
        text=True,
        check=True,
    )
    assert "first" in log.stdout


def test_F17_08_no_git_is_a_degradation_not_a_failure(tmp_path: Path) -> None:
    """A memory directory that is not a repository still merges."""
    directory = tmp_path / "mem"
    directory.mkdir()
    assert hand_edits(directory) == ""
    assert not commit(directory, "nothing to commit")


def test_F17_08_the_jobs_database_is_not_in_the_repository() -> None:
    """A binary rewritten on every operation, in the one directory whose whole
    point is that its diffs are readable."""
    from minicodex.memory import DEFAULT_MEMORY_DIR, MINICODEX_HOME
    from minicodex.memory_jobs import DEFAULT_JOBS_PATH

    assert DEFAULT_MEMORY_DIR not in DEFAULT_JOBS_PATH.parents
    assert DEFAULT_JOBS_PATH.parent == MINICODEX_HOME
```

> - 用到 git 的测试上面都有一个 `@needs_git` 标记。它在测试文件开头定义：`needs_git = pytest.mark.skipif(not git_available(), reason="git is not on PATH")`——没装 git 的机器上，这些测试会说明为什么跳过，而不是报一个莫名其妙的错。
> - 第四个测试：一个不是 git 仓库的目录，`hand_edits` 返回空、`commit` 返回 `False`，都不抛异常。

## §10 F17-06 / F17-07：遗忘

### 10.1 按时间遗忘会先删掉最有用的那条

清单把两条分开写，而它们其实是一条：

> F17-06 记忆无限增长 → 未使用天数窗口 + 总量上限 + 定期剪枝
> F17-07 按时间遗忘，删掉了最有用的那条（很老，但每周都在用）

**F17-06 的药方直译过来就是 F17-07。** 三条记忆，跑一次就看得见：

```
$ uv run python probe_memory_write.py forget

  by age, oldest first (what 'forget after N days' deletes first):
    Deploy   cited  34  age 270d
    Colours  cited   0  age 60d
    Tests    cited   0  age 1d

  what this chapter's rule keeps, capped at 2:
    kept:    ['Deploy', 'Tests']
    dropped: [('Colours', 'over the 2-entry cap')]

  and with no cap, only the unused-and-old rule:
    dropped: [('Colours', 'never cited in 60 days')]
```

按年龄排，第一个被删的是那条九个月大、每周都在用的。

### 10.2 排序：引用数第一，最近使用第二，年龄只是兜底

```python
    scored.append((-cited, -last_used, -first_seen, index, entry))
```

而"零引用"**永远不足以单独删掉一条**：

```python
    * The *bias*, measured in chapter 16's first draft and inherited whole: a
      model reports the memory that shaped its **answer** and not the memory
      that shaped its **actions**.  On the `runner` task it ran `python -m
      pytest` -- a string only memory knew -- and then cited nothing, 3/3.
```

第 16 章 §11.5 把这条偏差当成"已知偏差"交给第 17 章，这里就是它被兑现的地方：
**信号有偏，所以不能让它单独做删除决定。**

### 10.2.1 F17-13：这个信号比"有偏"还糟，而正确的修法是不修它

上面那段注释是在只知道**少报偏差**的时候写的。第 16 章按 codex 真实格式重写
引用块之后，又量出了第二件事，比第一件严重：

```
  codex 真实的条件式问法（已发布）      引用块 0/9   解析成功 0
  无条件问法（只用于对照）              引用块 9/9   解析成功 0   编造 9
```

（改写第 16 章时又发现一件事：那时程序给每个小节算的**行号本身是错的**（第 16 章 §5.3），一条照真实行号写的引用也会被判成编造。所以"9 个都是编的"里，可能有几个其实是对的。这组数字没有重量过。）

**不是少报，是根本解析不到。** 格式要 `path:line_start-line_end`，
而这个程序从没让模型看见过任何行号，于是它编了九个合理的行号。
编造的引用进不了 `record_uses`，也就永远变不成这里的 `count`。

于是 `prune()` 的**主排序键，是一个几乎恒为零的量**。

摆在面前的修法很诱人：第 16 章还有另一个信号——`usage_kind_for_call`，
它看的是模型**实际打开了哪个文件**，模型骗不了它。换成它不就好了？

**不好。而且理由不是风格，是 codex 明确不这么做，我照着不这么做。**
codex 把两个信号**严格分开**：

```
  行为信号  ->  一个遥测计数器，到此为止
                core/src/memory_usage.rs:9-27
  引用信号  ->  进数据库，因而决定策略
                core/src/stream_events_utils.rs:184
                -> state/src/runtime/memories.rs:55-73   （usage_count / last_usage）
                -> state/src/runtime/memories.rs:389-413 （保留剪枝）
```

分界线的理由是**粒度**，而且是可验证的、不是审美：
**行为信号只能说出"哪个文件"，而 `prune()` 的每一个决定都是关于"哪个条目"的。**
拿一个文件粒度的信号去做条目粒度的决定，结果是只要有人读过 `MEMORY.md`，
**所有**条目都算被用过——那不是遗忘策略，那是永不遗忘。

所以信号不换。换的是**它被允许承担多少重量**：

> `prune()` 本来就拒绝"零引用就删"——要零引用**且**过了窗口**且**超了上限。
> 那句话在只知道"少报"的时候是保险带。
> 在 0/9 面前，它是**唯一**挡在一个不可靠信号和误删之间的东西。
> 它从防御性代码变成了承重结构。

这件事写进 `test_F17_13_zero_citations_alone_never_drops_an_entry`，
因为一条从保险带变成承重件的规则，最危险的时刻是下一个读代码的人
觉得它是多余的。

**而这一条是活下来了，不是修好了。** 真正的修法是 codex 有而我没有的
`<rollout_ids>`：引用一次**会话**，模型不需要知道任何行号。
那需要一张 per-rollout 的持久化表（codex 的 `stage1_outputs`，按 `thread_id`），
而我这一章的第一段输出是文件、合并完就删。
§16 那张对照表里"我存成文件，因为那个文件要能被人打开看"那一行——
账在这里结。

### 10.3 两个不起眼但都出过问题的细节

**没有记录的条目算"新"，不算"古老"。**

```python
    An entry with no record at all is treated as new rather than as ancient.
    The other way round deletes every entry the first time this runs on a
    memory written before the counter existed -- and, since the merge step
    rewrites headings and a reworded heading is a new `entry_id`, it would also
    delete every entry the merge improved.
```

**保留的条目按原文件顺序写回，不按排名。**

```python
    # Sorted back into file order: the ranking decides *what* survives, and
    # letting it decide the order as well would reshuffle the whole file on
    # every merge, which makes the git diff -- the mechanism F17-08 depends on
    # -- useless.
```

§9 和 §10 在这里互相牵制：**如果排序决定文件顺序，那么每次合并的 diff
都是整个文件重排，而 §9 的整个机制依赖那个 diff 可读。**

### 10.4 `usage.json` 现在有两个主人

第 16 章的 `record_uses` 只加 `count`。这一章加了 `first_seen`，还会删行。

```python
    `usage.json` is chapter 16's file and this is the write path adding a
    field to it, which is worth naming rather than doing quietly: the read side
    increments `count`, the write side sets `first_seen` and deletes rows.  Two
    owners of one file is a thing to be nervous about, and it survives here for
    one reason -- there is no third operation, and splitting it would give the
    forgetting policy two files to read that must agree about which entries
    exist.
```

**一份数据两个写者，是要紧张的。** 这里留着，理由是"没有第三种操作"，
而且拆开的代价（遗忘策略要读两个必须一致的文件）比留着更大。

第一版这里有个 bug，是被测试逼出来的：

```python
    # Rebuilt from the entries that survive, rather than the pruned ones
    # deleted from the old table: an entry can also leave because the *merge*
    # stopped emitting it, and that path leaves no row in `pruned.dropped`.
    # The first version subtracted instead of rebuilding, and `usage.json`
    # accumulated counts for headings that no longer existed -- which the
    # forgetting policy then read.
```

**"减掉被删的"和"只留下活着的"，在有第三条离开路径的时候不是一回事。**

---

### 10.5 代码：`prune`、`trim_summary` 和 `usage.json`

```python
@dataclass(frozen=True)
class Prune:
    kept: tuple[Entry, ...]
    dropped: tuple[tuple[Entry, str], ...]

    def describe(self) -> str:
        if not self.dropped:
            return f"{len(self.kept)} entr(ies), nothing forgotten"
        reasons = ", ".join(f"{e.title} ({why})" for e, why in self.dropped)
        return f"{len(self.kept)} entr(ies), forgot {len(self.dropped)}: {reasons}"


def prune(
    entries: Sequence[Entry],
    counts: dict[str, dict[str, Any]],
    *,
    now: float,
    max_entries: int = MAX_ENTRIES,
    unused_days: float = UNUSED_DAYS,
) -> Prune:
    """Decide what leaves.  Citations first, recency second, age last.

    The listed fix for F17-06 is "a window of unused days, a total cap, and a
    periodic prune", and taken literally that is F17-07: the entry that is nine
    months old and consulted every week is exactly the one an age rule deletes
    first.  So age is only ever a tie-breaker here, and the primary key is
    chapter 16's citation counter.

    **That primary key is a signal chapter 16 has now measured as unreliable,
    and this is the fault it lands on (F17-13).**  Two separate things went
    wrong with it, and only the first was known when this function was written:

    * The *bias*, measured in chapter 16's first draft and inherited whole: a
      model reports the memory that shaped its **answer** and not the memory
      that shaped its **actions**.  On the `runner` task it ran `python -m
      pytest` -- a string only memory knew -- and then cited nothing, 3/3.
    * The *resolution rate*, measured after chapter 16 adopted codex's real
      citation format: asked for `path:line_start-line_end`, the model supplies
      plausible line numbers it did not look up, and **0 of 9 citations
      resolved to a real entry**.  A citation that does not resolve never
      reaches `record_uses`, so it never becomes a count here.

    The tempting repair is to rank on chapter 16's other signal instead --
    `usage_kind_for_call`, which sees the model actually open `MEMORY.md` and
    cannot be lied to.  **codex does not do that, and neither does this.**  It
    keeps the two strictly apart: the behavioural signal feeds a telemetry
    counter and stops there (`core/src/memory_usage.rs:9-27`), while retention
    ranking and retention pruning read only the citation-fed columns
    (`core/src/stream_events_utils.rs:184` ->
    `state/src/runtime/memories.rs:55-73`, consumed at
    `state/src/runtime/memories.rs:389-413`).  The reason is not ceremony: a
    counter that says "the model opened MEMORY.md" cannot say *which section
    mattered*, and every decision made here is per-entry.  Substituting a
    file-granularity signal for an entry-granularity one would keep every entry
    alive whenever any entry was read, which is not a forgetting policy.

    So the signal stays, and what changes is how much weight it is allowed to
    carry.  **Zero citations alone never drops anything** -- it takes zero
    citations *and* a month of not being consulted *and* the cap being over.
    That was written as belt-and-braces when the bias was the only known
    problem; with the resolution rate at 0/9 it is the only thing standing
    between an unreliable signal and deleting a good entry, and it is now
    load-bearing.  `test_F17_13_*` pins it.

    An entry with no record at all is treated as new rather than as ancient.
    The other way round deletes every entry the first time this runs on a
    memory written before the counter existed -- and, since the merge step
    rewrites headings and a reworded heading is a new `entry_id`, it would also
    delete every entry the merge improved.
    """
    scored: list[tuple[float, float, float, int, Entry]] = []
    for index, entry in enumerate(entries):
        row = counts.get(entry.entry_id, {})
        cited = float(row.get("count", 0) or 0)
        last_used = float(row.get("last_used", 0) or 0)
        first_seen = float(row.get("first_seen", 0) or 0) or now
        scored.append((-cited, -last_used, -first_seen, index, entry))
    scored.sort()

    kept: list[Entry] = []
    dropped: list[tuple[Entry, str]] = []
    for cited, _, _, _, entry in scored:
        row = counts.get(entry.entry_id, {})
        first_seen = float(row.get("first_seen", 0) or 0) or now
        age_days = max(0.0, (now - first_seen) / 86400.0)
        if len(kept) >= max_entries:
            dropped.append((entry, f"over the {max_entries}-entry cap"))
        elif cited == 0 and age_days > unused_days:
            dropped.append((entry, f"never cited in {age_days:.0f} days"))
        else:
            kept.append(entry)
    # Sorted back into file order: the ranking decides *what* survives, and
    # letting it decide the order as well would reshuffle the whole file on
    # every merge, which makes the git diff -- the mechanism F17-08 depends on
    # -- useless.
    order = {entry.entry_id: i for i, entry in enumerate(entries)}
    kept.sort(key=lambda e: order[e.entry_id])
    return Prune(kept=tuple(kept), dropped=tuple(dropped))
```

> - **`Prune`**：留下的和删掉的（删掉的每条带一个理由）。`describe()` 给报告用。
> - **排序**：每条记忆算一个元组 `(-引用数, -最近使用时间, -首次出现时间, 原来的位置, 条目)`，然后 `sort()`。元组从第一项比起：引用越多越靠前（取负号），一样就比最近用过的，再一样比新旧。
> - `float(row.get("count", 0) or 0)`：取不到、或者存的是 `None`，都当 0。`or now`：没有"首次出现"的记录，就当成**现在**（"没记录的算新的"，§10.3）。
> - **走一遍排好的顺序，三种去向**：已经留够 `max_entries` 条了 → 删（"超过上限"）；**零引用并且**超过 `unused_days` 天 → 删；否则留。注意第二条要两个条件同时成立——这就是 §10.2.1 说的"承重件"。
> - **最后按原文件顺序排回去**：排名决定**谁活**，不决定顺序（§10.3）。`order = {entry.entry_id: i ...}` 记下每条原来的位置，`kept.sort(key=lambda e: order[e.entry_id])`。

```python
def trim_summary(
    summary: str,
    entries: Sequence[Entry] = (),
    *,
    budget: int = SUMMARY_TOKEN_BUDGET,
) -> str:
    """Keep the resident half inside chapter 16's cap, deterministically.

    The prompt asks for at most eight bullets and the prompt is a request.  A
    resident block is sent with every request forever, so the bound is here as
    well, in code, and it cuts at a line.

    `entries` is the part that had to be added after a mutation survived, and
    the reason is chapter 6's sentence about `Sizer`: **the fault is not that
    one ruler was wrong, it is that there were two.**  The first version
    trimmed the summary to the budget on its own; the read path then budgets
    the same number over the summary *and* the heading index *and* the fences,
    so a summary trimmed to exactly the cap came back truncated by the reader.
    Nothing failed -- the block simply arrived with `[... memory truncated ...]`
    in it.  So the question asked here is the read path's own: trim until
    `overflows()` says no.

    What that marker *means* changed in chapter 16's rewrite and this function
    did not have to.  It used to decide whether the dedicated search tools got
    mounted; that gate was this book's invention and is gone (F16-12,
    `composition.top_level_tools`).  `overflows()` survives as what it always
    literally was -- "did the reader have to cut anything" -- which is the only
    thing this function ever asked it.  A budget that rose from 400 to codex's
    real 2500 (`ext/memories/src/lib.rs:16`) also makes it fire far less often,
    and it is kept for the reason the cap itself is kept: the summary is sent
    with every request forever, and the prompt asking for eight bullets is a
    request.
    """
    lines = summary.splitlines()
    while lines and overflows(
        Memory(directory=Path("."), summary="\n".join(lines), entries=tuple(entries)),
        budget=budget,
    ):
        lines.pop()
    return "\n".join(lines).rstrip()
```

> - 摘要一行一行往下删（`lines.pop()` 删最后一行），直到第 16 章的 `overflows()` 说"不用截了"。
> - **问的是读路径自己的那把尺子。** 说明里记着这一条的来历：第一版用自己的尺子把摘要裁到上限，而第 16 章读的时候是把"摘要 + 目录 + 围栏"一起算的——于是自己写进去的记忆，读出来时被自己截断了，带着一个"[... memory truncated"。变异检查找到的：一条变异活了下来，因为测试测的是这个函数本身，没测"写进去再读出来"。第 6 章那句话第二次：**错不在某一把尺子不准，在于有两把。**

```python
def _usage(directory: Path) -> dict[str, dict[str, Any]]:
    path = Path(directory) / USAGE_FILE
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return raw if isinstance(raw, dict) else {}


def _record_first_seen(
    directory: Path, pruned: Prune, counts: dict[str, dict[str, Any]], *, now: float
) -> None:
    """Stamp new entries, forget the rows of dropped ones.

    `usage.json` is chapter 16's file and this is the write path adding a
    field to it, which is worth naming rather than doing quietly: the read side
    increments `count`, the write side sets `first_seen` and deletes rows.  Two
    owners of one file is a thing to be nervous about, and it survives here for
    one reason -- there is no third operation, and splitting it would give the
    forgetting policy two files to read that must agree about which entries
    exist.
    """
    # Rebuilt from the entries that survive, rather than the pruned ones
    # deleted from the old table: an entry can also leave because the *merge*
    # stopped emitting it, and that path leaves no row in `pruned.dropped`.
    # The first version subtracted instead of rebuilding, and `usage.json`
    # accumulated counts for headings that no longer existed -- which the
    # forgetting policy then read.
    updated: dict[str, dict[str, Any]] = {}
    for entry in pruned.kept:
        row = dict(counts.get(entry.entry_id) or {})
        row.setdefault("count", 0)
        row.setdefault("first_seen", now)
        updated[entry.entry_id] = row
    try:
        (Path(directory) / USAGE_FILE).write_text(
            json.dumps(updated, indent=2, sort_keys=True), encoding="utf-8"
        )
    except OSError:  # pragma: no cover
        return
```

> - **`_usage`**：读 `usage.json`，读坏了就当空的。
> - **`_record_first_seen`**：**从活下来的条目重建**整张表，而不是从旧表里减去被删的。注释里讲了第一版的 bug：条目还有第三条离开的路——合并时模型不再输出它——那条路不经过 `pruned.dropped`，于是 `usage.json` 里攒下了一堆已经不存在的标题的计数，而遗忘策略还会去读它们。
> - `row.setdefault("first_seen", now)`：只有第一次出现的条目才写上时间，已有的不动。

```python
def _entry(slug: str, title: str) -> Entry:
    return Entry(f"{BODY_FILE}#{slug}", title, f"The rule about {title}.", (1, 2))


def test_F17_07_the_old_and_used_entry_outlives_the_new_and_unused_one() -> None:
    """The listed fix for F17-06 is a window of unused days; taken literally it
    is F17-07.  Nine months old and consulted every week is exactly what an
    age rule deletes first.
    """
    now = time.time()
    entries = (_entry("deploy", "Deploy"), _entry("colours", "Colours"))
    counts = {
        f"{BODY_FILE}#deploy": {
            "count": 34,
            "last_used": now - 3 * DAY,
            "first_seen": now - 270 * DAY,
        },
        f"{BODY_FILE}#colours": {"count": 0, "last_used": 0, "first_seen": now - 60 * DAY},
    }
    result = prune(entries, counts, now=now, max_entries=1)
    assert [e.title for e in result.kept] == ["Deploy"]
    assert [e.title for e, _ in result.dropped] == ["Colours"]


def test_F17_06_an_uncited_entry_leaves_only_after_the_window() -> None:
    now = time.time()
    entries = (_entry("new", "New"), _entry("old", "Old"))
    counts = {
        f"{BODY_FILE}#new": {"count": 0, "first_seen": now - 1 * DAY},
        f"{BODY_FILE}#old": {"count": 0, "first_seen": now - 60 * DAY},
    }
    result = prune(entries, counts, now=now)
    assert [e.title for e in result.kept] == ["New"]
    assert "never cited in 60 days" in result.dropped[0][1]


def test_F17_07_zero_citations_alone_never_drops_anything() -> None:
    """Chapter 16 measured the bias this compensates for.

    A model reports the memory that shaped its *answer* and not the memory
    that shaped its *actions*: on the `runner` task it ran `python -m pytest`
    -- a string only memory knew -- and then wrote `none`, 3/3.  So a zero in
    the citation column is not evidence of an unused entry, and it takes zero
    citations *and* a month *and* the cap being over.
    """
    now = time.time()
    entries = tuple(_entry(f"e{i}", f"E{i}") for i in range(5))
    counts = {e.entry_id: {"count": 0, "first_seen": now - 1 * DAY} for e in entries}
    assert prune(entries, counts, now=now).dropped == ()


def test_F17_06_an_entry_with_no_record_at_all_is_new_not_ancient() -> None:
    """The first run against a memory written before the counter existed.

    The other way round deletes all of it.
    """
    now = time.time()
    entries = (_entry("a", "A"),)
    assert prune(entries, {}, now=now).dropped == ()


def test_F17_06_the_cap_is_a_cap(tmp_path: Path) -> None:
    now = time.time()
    entries = tuple(_entry(f"e{i}", f"E{i}") for i in range(MAX_ENTRIES + 5))
    counts = {e.entry_id: {"count": 0, "first_seen": now} for e in entries}
    result = prune(entries, counts, now=now)
    assert len(result.kept) == MAX_ENTRIES
    assert all("cap" in why for _, why in result.dropped)


def test_F17_07_the_ranking_decides_what_survives_not_what_order_it_is_in() -> None:
    """Sorted back into file order before writing.

    Re-ordering the file on every merge makes the git diff -- the mechanism
    F17-08 depends on -- unreadable, and a diff nobody can read is a diff
    nobody uses.
    """
    now = time.time()
    entries = (_entry("a", "A"), _entry("b", "B"), _entry("c", "C"))
    counts = {
        f"{BODY_FILE}#a": {"count": 1, "first_seen": now},
        f"{BODY_FILE}#b": {"count": 9, "first_seen": now},
        f"{BODY_FILE}#c": {"count": 5, "first_seen": now},
    }
    assert [e.title for e in prune(entries, counts, now=now).kept] == ["A", "B", "C"]


def test_F17_06_forgetting_an_entry_forgets_its_usage_row_too(tmp_path: Path) -> None:
    """Otherwise a heading reused a year later inherits somebody else's count.

    `record_uses` takes `CitedEntry` values rather than bare ids since chapter
    16 adopted codex's real citation format (F16-13): a citation now names a
    line range, and only the ones that resolve to a real entry count.
    """
    now = time.time()
    gone = _entry("gone", "Gone")
    record_uses(
        tmp_path,
        (CitedEntry(path=BODY_FILE, line_start=1, line_end=2, note="why", entry=gone),),
        now=now - 90 * DAY,
    )
    raw = json.loads((tmp_path / USAGE_FILE).read_text(encoding="utf-8"))
    raw[f"{BODY_FILE}#gone"]["first_seen"] = now - 90 * DAY
    (tmp_path / USAGE_FILE).write_text(json.dumps(raw), encoding="utf-8")

    write_memory(
        tmp_path,
        summary="- keep",
        body="## Keep\n\nthe only survivor\n",
        now=now,
        lock_path=tmp_path / "merge.lock",
    )
    after = json.loads((tmp_path / USAGE_FILE).read_text(encoding="utf-8"))
    assert f"{BODY_FILE}#gone" not in after
    assert after[f"{BODY_FILE}#keep"]["first_seen"] == now


def test_F17_06_the_written_summary_is_inside_chapter_sixteens_cap(tmp_path: Path) -> None:
    """Through `write_memory`, not through `trim_summary`.

    The direct test below was green with the call to it deleted: it tested the
    function, and what has to hold is that **nothing this program writes needs
    the read path to truncate it**.  A merge that produces forty bullets is not
    an error, it is an over-long resident block, and the place that catches it
    is the writer.

    1500 bullets, not the 200 this was written with.  Chapter 16's rewrite took
    the budget from a number this book invented (400) to codex's real one
    (2500, `ext/memories/src/lib.rs:16`), and 200 bullets now fit -- so the
    fixture stopped reaching the bound, and both mutations of the
    `trim_summary` call it exists to catch survived.  Caught by
    `probe_mutations_ch17.py`, which is the second time in two chapters that a
    changed constant quietly retired a test rather than breaking it.
    """
    from minicodex.memory import resident_block
    from minicodex.tokens import estimate_messages

    write_memory(
        tmp_path,
        summary="\n".join(f"- rule number {i} about something or other" for i in range(1500)),
        body="## A\n\nb\n",
        lock_path=tmp_path / "merge.lock",
    )
    block = resident_block(load(tmp_path)) or ""
    assert "[... memory truncated" not in block, "the read path had to truncate what we wrote"
    written = (tmp_path / SUMMARY_FILE).read_text(encoding="utf-8")
    assert estimate_messages([{"role": "user", "content": written}]) <= SUMMARY_TOKEN_BUDGET


def test_F17_06_the_resident_half_is_trimmed_by_code_not_by_request() -> None:
    """The merge prompt asks for at most eight bullets.  A prompt is a request.

    The resident block is sent with every request forever, so chapter 16's cap
    is enforced on the way in as well as on the way out.

    The fixture is 1500 bullets rather than the 200 it was written with, and
    that is chapter 16's rewrite arriving here: the budget went from a number
    this book invented (400) to codex's real one (2500,
    `ext/memories/src/lib.rs:16`), and 200 bullets now fit inside it.  A test
    whose input no longer reaches the bound it is testing passes for the wrong
    reason -- it did, until this line changed.
    """
    long_summary = "\n".join(f"- rule number {i} about something" for i in range(1500))
    trimmed = trim_summary(long_summary)
    assert len(trimmed) < len(long_summary)
    from minicodex.tokens import estimate_messages

    assert estimate_messages([{"role": "user", "content": trimmed}]) <= SUMMARY_TOKEN_BUDGET
    # ...and the same call, told what else is going into the block, cuts more.
    # One ruler: the question asked is the read path's own (F06-12 again).
    entries = tuple(_entry(f"e{i}", f"Section {i}") for i in range(30))
    assert len(trim_summary(long_summary, entries)) < len(trimmed)
```

```python
def test_F17_13_zero_citations_alone_never_drops_an_entry() -> None:
    """The conservatism that was belt-and-braces is now load-bearing.

    Chapter 16 measured its own citation format resolving **0 of 9** times: the
    model supplies plausible line numbers it never looked up, and a citation
    that does not resolve never becomes a count here.  So "this entry has no
    citations" is not evidence about the entry -- it is the ordinary state of
    every entry.  Dropping on that alone would empty a working memory.
    """
    now = time.time()
    entries = (_entry("a", "A"), _entry("b", "B"))
    # Nothing has ever been cited, and both entries are recent.
    kept = prune(entries, {}, now=now).kept
    assert [e.title for e in kept] == ["A", "B"]

    # Still nothing cited, and now both are far past the window -- but the cap
    # is not exceeded, so age alone does not drop them either... except that
    # age *plus* zero citations is exactly the pair the window is for.
    old = {
        f"{BODY_FILE}#a": {"count": 0, "first_seen": now - 400 * DAY},
        f"{BODY_FILE}#b": {"count": 1, "first_seen": now - 400 * DAY},
    }
    dropped = prune(entries, old, now=now).dropped
    assert [e.title for e, _ in dropped] == ["A"], "one citation is enough to survive the window"


def test_F17_13_the_behavioural_signal_is_not_wired_into_ranking() -> None:
    """codex keeps the two usage signals apart, and so does this.

    The behavioural one (`usage_kind_for_call`) sees the model actually open
    `MEMORY.md` and cannot be lied to -- which makes it tempting as a
    replacement for the citation counter `prune` ranks on.  codex does not do
    that: it feeds a telemetry counter and stops (`core/src/memory_usage.rs:
    9-27`), while retention reads only the citation-fed columns
    (`core/src/stream_events_utils.rs:184` ->
    `state/src/runtime/memories.rs:55-73`, consumed at `:389-413`).

    The reason is granularity, and it is checkable rather than stylistic: the
    behavioural signal names a *file*, and every decision `prune` makes is
    about one *entry*.  This test pins the shape -- a kind is a bare file
    label, carrying no entry id for `prune` to rank by.
    """
    import ast
    import inspect
    import textwrap

    from minicodex import memory_write
    from minicodex.memory import MEMORY_MD_KIND, Memory, usage_kind_for_call

    memory = Memory(directory=Path("/tmp/mem"), summary="s", entries=(_entry("a", "A"),))
    kind = usage_kind_for_call(memory, "read_file", {"path": "/tmp/mem/MEMORY.md"})
    assert kind == MEMORY_MD_KIND
    assert f"{BODY_FILE}#a" not in str(kind), "a kind cannot identify which entry was used"

    # And the ranking function genuinely does not consult it. Parsed rather
    # than grepped, and that is the point: `prune`'s docstring explains at
    # length why it does *not* use this signal, so a substring check over the
    # function's text finds the explanation and reports it as a use. Dropping
    # the docstring node leaves only what runs.
    tree = ast.parse(textwrap.dedent(inspect.getsource(memory_write.prune)))
    fn = tree.body[0]
    assert isinstance(fn, ast.FunctionDef)
    statements = fn.body[1:] if ast.get_docstring(fn) is not None else fn.body
    names = {
        node.id for stmt in statements for node in ast.walk(stmt) if isinstance(node, ast.Name)
    } | {
        node.attr
        for stmt in statements
        for node in ast.walk(stmt)
        if isinstance(node, ast.Attribute)
    }
    assert not any("usage_kind" in name for name in names), sorted(names)


def test_F17_13_an_entry_whose_heading_the_merge_reworded_is_new_not_ancient() -> None:
    """A reworded heading is a new `entry_id`, so its citations do not follow it.

    That is a real cost of deriving ids from headings, and it is paid in the
    safe direction: the entry looks new, not unused.  The other way round would
    delete an entry for the crime of having been improved by the merge that
    just ran.
    """
    now = time.time()
    counts = {f"{BODY_FILE}#running-tests": {"count": 30, "first_seen": now - 400 * DAY}}
    # The merge renamed the section; the old row no longer matches anything.
    renamed = (_entry("how-to-run-tests", "How to run tests"),)
    kept = prune(renamed, counts, now=now).kept
    assert [e.title for e in kept] == ["How to run tests"]
```

> - `test_F17_13_the_behavioural_signal_is_not_wired_into_ranking` 用 `inspect.getsource` 取出 `prune` 的源码、用 `ast` 解析，断言函数体里没有用到第 16 章的行为信号（名字里带 `usage_kind` 的东西）。说明里讲了为什么要解析而不是搜字符串：`prune` 的文档字符串里长篇解释了它**为什么不用**这个信号，按字符串搜会把解释当成使用。它防的是 §10.2.1 那个"诱人的修法"。

**`usage.json` 的两个主人，还有一个没处理的竞态。** §10.4 说读路径（第 16 章的 `record_uses`，在每次 `ask` 的末尾）和写路径（这里的 `_record_first_seen`，在合并里）都会改这个文件。改写时我注意到它们**不共享任何锁**：两个进程里，一个刚读完 `usage.json` 准备加一次引用，另一个合并完重建了整张表，然后前一个把它的版本写回去——那次合并新加的 `first_seen` 就丢了，或者反过来丢一次引用。后果是一次计数不准，不是丢记忆；而让 `record_uses` 也去拿合并锁，意味着每次 `ask` 结束都要等一个可能正在跑的合并。**没有改**，记在这里。

## §11 F17-10：这条故障在这里不成立，而它的形状出现了两次

### 11.1 清单说的那个循环

> F17-10 写记忆的 Agent 自己也是一次会话，于是它的历史又触发一次记忆生成
> → 给它打上 ephemeral 标记、关掉它自己的记忆生成、禁止它再派子 Agent

我按清单写了那个字段：`SessionMeta.ephemeral: bool = False`，
连注释都写好了（"additive is not breaking"，第 7 章 F07-09 的第三次应用）。

然后我去找**谁设置它**。

没有人。

我的写路径**不是一个 Agent**——它是两次没有工具的模型调用（§2.2），
不写 rollout，不产生会话，所以没有会话可标记。
codex 需要这个标记是因为它的 Phase 2 是一个真正的内部子 Agent
（`ephemeral = true`、无网络、`AskForApproval::Never`、并且关掉自己的记忆生成）。

于是这个字段被删了，删除的位置留了一段话：

```python
    # There was a third additive field here for an afternoon -- `ephemeral`,
    # for "this session is the program talking to itself", which is what
    # F17-10 says the memory writer's own conversation needs.  It was deleted
    # before the chapter shipped, because nothing in this program sets it:
    # the writer is two model calls rather than an agent, so it has no session
    # to mark.  A defence with no caller is not free -- it reads as covered,
    # and the next person to need it has to work out whether it ever worked.
    # Chapter 17 says where the fault turned up instead.
```

> **一个没有调用者的防御不是免费的。** 它看起来像"这块已经覆盖了"，
> 而下一个真正需要它的人，得先花时间搞清楚它到底有没有生效过。

这是插曲 B 的 FB-03（"为打断循环而引入的接口只有一个实现，纯属噪音"）
第二次出现，也是 §2.4 那条判断的同一把尺子：**照抄清单就是不做判断。**

### 11.2 但那个形状出现了两次，都在清单没写的地方

**第一次：子 Agent 的会话进了队列。**

四个问题跑完，`sessions/` 里有**七个文件**——其中一个任务派了两个子 Agent。

```
20260815T093939-5596  items= 27  parent=None
20260815T094017-5596  items= 11  parent=20260815T093939-5596
20260815T094020-5596  items=  9  parent=20260815T093939-5596
```

抽取它们的结果是：父会话说"`pad_string` 在 util/text.py"，
两个子会话**各说一遍同一句话**。而且子会话里根本没有用户——
第一段 prompt 里"用户表达的稳定偏好"那一半，对着一个只有两个 agent 的对话完全无效。

```python
    Chapter 7 made exactly this exclusion, for exactly this reason, in
    `resolve("last")`: "a run that spawns two sub-agents leaves three files, and
    the two newest are the children".  The lesson is not about sub-agents.  It
    is that **a session directory stopped being a list of conversations a user
    had two chapters ago**, and every new reader of that directory has to be
    told again.
```

**第 7 章为了 `--resume last` 做过一模一样的排除。** 同一个目录、同一个原因、
第二个读者，重新踩一遍。

**第二次：抽取器读到了自己的输出。**

一条用了 `remember_this` 的会话，历史里长这样：

```
assistant calls remember_this {"note": "In this repository, the linter is always run as `ruff check --fix`..."}
remember_this returned: Proposed. It is not memory yet: it goes to notes/ ...
```

抽取它，三次都得到：

```
--- sample 0: ('In this repository, the linter is always run as `ruff check --fix`, ...', ...)
--- sample 1: ('In this repository, the linter is always run as `ruff check --fix`, ...', ...)
--- sample 2: ('In this repository, the linter is always run as `ruff check --fix`, ...', ...)
```

**同一句话，通过两条路各到达第二段一次。** 这就是 F17-10 的形状在这个程序里
唯一能达到的位置：记忆系统在读一份关于记忆系统的记录。

```python
            if item.name == NOTE_NAME:
                # The writer does not read its own output.  A `remember_this`
                # call is *already* an input to stage 2 by another route
                # (`notes/`), so leaving it in the transcript delivers the same
                # sentence twice -- measured: 3/3 extractions of a session that
                # proposed a note re-derived the note as a bullet.
                continue
```

### 11.3 还有一处，是设计时就挡掉的

`transcript_of` 不给抽取器看 system / developer note：

```python
    System and developer notes are **left out**, and that is a decision rather
    than an omission.  They are this program's own words -- the permissions
    block, chapter 0's "you have 2 turns left", chapter 13's `AGENTS.md`
    injection, chapter 16's memory block itself -- and an extractor shown them
    dutifully reports the program's instructions back as things worth
    remembering.  The most direct form of that is the last one in the list: a
    memory that was injected into the session is a memory the session then
    re-learns, which is a loop with no fixed point and no error message.
```

**最后那句是真正的死循环**：第 16 章把记忆注入成一条消息，
如果抽取器读它，它就会把自己的记忆重新学一遍，一代一代加强。
这条是靠一个 `isinstance` 判断挡掉的，也有变异测试守着（`(UserMessage, SystemNote)`
一改就红 9 个）。

---

### 11.4 测试

```python
def test_F17_10_a_note_call_is_not_part_of_the_transcript(tmp_path: Path) -> None:
    """The one place this program can reach F17-10's shape.

    A `remember_this` call is already an input to stage 2 through `notes/`.
    Leaving it in the transcript delivers the same sentence twice -- measured,
    3/3 extractions of a session that proposed a note re-derived the note as a
    bullet of its own.
    """
    sessions = tmp_path / "sessions"
    _write_session(
        sessions,
        "s1",
        ["remember: the linter is ruff"],
        calls=[(NOTE_NAME, '{"note": "The linter is ruff."}'), ("read_file", '{"path": "a.py"}')],
    )
    text, _ = transcript_of(read_rollout(sessions / "s1.jsonl"))
    assert NOTE_NAME not in text
    assert "read_file" in text
    assert "remember: the linter is ruff" in text


def test_F17_10_system_and_developer_notes_are_not_extracted_from(tmp_path: Path) -> None:
    """The program's own words are not the session's content.

    An extractor shown the permissions block, the turn-budget warning or -- the
    interesting one -- the *memory block itself* reports them back as things
    worth remembering, which is a loop with no fixed point and no error
    message.
    """
    sessions = tmp_path / "sessions"
    _write_session(
        sessions,
        "s1",
        ["hello"],
        notes=["You have 2 turns left.", "<memory>\n- Always use uv.\n</memory>"],
    )
    text, _ = transcript_of(read_rollout(sessions / "s1.jsonl"))
    assert "2 turns left" not in text
    assert "Always use uv" not in text
    assert "hello" in text
```

> - 第一个：会话里有一次 `remember_this` 调用和它的返回，`transcript_of` 的输出里两样都不能有。
> - 第二个：会话里有一条系统消息（`_write_session(..., notes=[...])`），抽取器不能看到它。

## §12 F17-11：同意、看得见、删得掉

第 16 章已经付了便宜的那一半（`--memory` 默认关、`minicodex memory` 能看能删）。
这一章要付贵的一半。

### 12.1 两个开关，不是一个模式

```python
    # A *second* switch, not a mode of the first, and the split is the whole of
    # F17-11's expensive half.  Reading what you wrote by hand and letting a
    # model write things down about you are two different consents, and the
    # cost of asking for them separately is one flag.  codex asks the same
    # question in a dialog box with two buttons.
    ask.add_argument("--remember", action="store_true", ...)
```

**读你自己写的东西**和**让一个模型记录关于你的东西**是两件事。
codex 的 TUI 也是两个弹窗（`Enable memories?` / `Reset all memories?`）。

### 12.2 "它从哪知道的"要能回答

```
$ minicodex memory --jobs
  20261115T081203-4127         done     attempt 1
  20261115T081544-4127         done     attempt 1

  done: 2
  1 session file(s) in sessions not picked up yet
  locked, skipped: 20261115T083311-5530.jsonl (pid 5530). If that process is gone, delete 20261115T083311-5530.jsonl.lock and it will be picked up.
```

（这是改写之后的输出，这次在 Linux 上造了四个会话实测。最后两行是改写时加的：一行原来写着"N session file(s) are eligible"，数的是所有已结束的会话，包括已经处理完的；最后一行是 §4.7。）

```python
def _memory_jobs(jobs_path: Path, session_dir: Path) -> int:
    """Which sessions became memory, which are waiting, and which gave up.

    The answer to "why does it not know that yet", which is the first question
    a user asks about a feature that runs when they are not looking.  A
    background job whose state is only in a database nobody can read is a
    background job that gets reported as broken.
    """
```

### 12.3 `--forget-all` 必须真的删干净

第 16 章的 `--forget-all` 删三个文件。现在多了两个目录和一个数据库：

```python
        # The two directories chapter 17 writes.  `raw/` holds stage-1 output
        # -- preferences extracted from sessions, in a file that is memory in
        # everything but name -- and `notes/` holds what the model proposed.
        # A `--forget-all` that leaves those is a `--forget-all` that lies.
```

数据库也要清，理由不那么显然：

```python
    def forget_all(self) -> None:
        """Part of `--forget-all`: a user deleting their memory means all of it.

        Leaving the job table behind would mean the sessions that produced the
        deleted memory are marked `done`, so regenerating it would produce
        nothing and the user would conclude the feature is broken.
        """
```

而 git 历史**不删**，但要说：

```python
        if (directory / ".git").is_dir():
            # Said, not done.  The whole point of the git repository is that a
            # merge cannot silently eat a hand edit, and quietly deleting the
            # history to satisfy a `--forget-all` would be this program
            # destroying the one record it kept of its own changes.
            print(f"note: the git history in {directory / '.git'} still has every past version.")
            print(f"      remove it with: rm -rf {directory / '.git'}")
```

**"删干净了"和"删干净了，除了这里"，差别是一行字。**
一个说了的遗留物是用户的选择，一个没说的遗留物是这个程序在骗人。

---

### 12.4 代码：命令行

`__main__.py` 的导入区加上：

```python
from minicodex.memory import (
    ...
    USAGE_FILE,
    ...
)
from minicodex.memory_jobs import DEFAULT_JOBS_PATH, JobStore, locked_sessions, pending_sessions
from minicodex.memory_write import (
    NOTE_NAME,
    NOTES_DIR,
    RAW_DIR,
    pending_notes,
    remember_instructions,
    run_pipeline,
)
```

`ask` 的参数，接在第 16 章的 `--dedicated-tools` 后面：

```python
# A *second* switch, not a mode of the first, and the split is the whole of
# F17-11's expensive half.  Reading what you wrote by hand and letting a
# model write things down about you are two different consents, and the
# cost of asking for them separately is one flag.  codex asks the same
# question in a dialog box with two buttons.
ask.add_argument(
    "--remember",
    action="store_true",
    help=(
        "let this session be turned into memory later, and give the agent "
        f"`{NOTE_NAME}` (off by default; see `minicodex memory`)"
    ),
)
ask.add_argument("--memory-dir", type=Path, default=DEFAULT_MEMORY_DIR)
ask.add_argument("--jobs-db", type=Path, default=DEFAULT_JOBS_PATH)
```

> `--memory-dir` 挪到了这里（第 16 章的那一行删掉）：读和写用的是同一个目录。

`memory` 子命令多四个参数：

```python
memory_cmd.add_argument(
    "--jobs",
    action="store_true",
    help="show which sessions have been turned into memory, and which are waiting",
)
memory_cmd.add_argument(
    "--remember-now",
    action="store_true",
    help="run the writer in the foreground over what is pending, and wait for it",
)
memory_cmd.add_argument("--session-dir", type=Path, default=DEFAULT_DIR)
memory_cmd.add_argument("--jobs-db", type=Path, default=DEFAULT_JOBS_PATH)
memory_cmd.add_argument("--provider", choices=sorted(PROVIDERS), default="openai")
memory_cmd.add_argument("--model", default=None)
```

`main()` 里两处：`memory` 子命令把新参数都传进去；`ask` 把两个新参数传给 `_ask`：

```python
                remember=args.memory_dir if args.remember else None,
                jobs_path=args.jobs_db,
```

`_ask` 的签名多 `remember: Path | None` 和 `jobs_path: Path = DEFAULT_JOBS_PATH`，`top_level_tools(...)` 多传 `remember=remember`。

`minicodex memory` 本身：

```python
def _memory(
    directory: Path,
    *,
    forget_all: bool = False,
    jobs: bool = False,
    remember_now: bool = False,
    session_dir: Path = DEFAULT_DIR,
    jobs_path: Path = DEFAULT_JOBS_PATH,
    provider: str = "openai",
    model: str | None = None,
) -> int:
    """Everything a user needs to answer "what does it know about me".

    Three things, and the order is the order somebody asks them in: where it
    is, what is in it, and how to make it stop. `minicodex rules` (chapter 5)
    is the same command for a different store, and the reason both exist is
    the same one: a thing nobody can see is a thing nobody can revoke.

    Chapter 17 adds two more questions the same person asks next -- *where does
    it come from* (`--jobs`) and *do it now* (`--remember-now`) -- and one
    obligation to the first: `--forget-all` has to delete the extracted
    material and the job records too, or "delete everything" leaves the
    intermediate copies behind and marks every session that produced them as
    already processed.
    """
    if forget_all:
        removed = []
        for name in (SUMMARY_FILE, BODY_FILE, USAGE_FILE):
            path = directory / name
            if path.is_file():
                path.unlink()
                removed.append(name)
        # The two directories chapter 17 writes.  `raw/` holds stage-1 output
        # -- preferences extracted from sessions, in a file that is memory in
        # everything but name -- and `notes/` holds what the model proposed.
        # A `--forget-all` that leaves those is a `--forget-all` that lies.
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
            # Said, not done.  The whole point of the git repository is that a
            # merge cannot silently eat a hand edit, and quietly deleting the
            # history to satisfy a `--forget-all` would be this program
            # destroying the one record it kept of its own changes.
            print(f"note: the git history in {directory / '.git'} still has every past version.")
            print(f"      remove it with: rm -rf {directory / '.git'}")
        return 0

    if jobs:
        return _memory_jobs(jobs_path, session_dir)

    if remember_now:
        return _remember_now(
            directory,
            session_dir=session_dir,
            jobs_path=jobs_path,
            provider=provider,
            model=model,
        )

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

> - **`--forget-all`** 现在删五样：两个记忆文件、`usage.json`、`raw/` 和 `notes/` 里的文件、任务表里的所有行。最后**说出**还剩什么——git 历史——并告诉用户怎么删，但不替他删（§12.3）。
> - 三个分支之后是第 16 章原来的"列出记忆"，末尾多一行"还有几条提议的笔记没合并"。

```python
def _memory_jobs(jobs_path: Path, session_dir: Path) -> int:
    """Which sessions became memory, which are waiting, and which gave up.

    The answer to "why does it not know that yet", which is the first question
    a user asks about a feature that runs when they are not looking.  A
    background job whose state is only in a database nobody can read is a
    background job that gets reported as broken.
    """
    if not Path(jobs_path).exists():
        print(f"no job records at {jobs_path} (nothing has been remembered yet)")
    else:
        with JobStore(jobs_path) as store:
            rows = store.rows()
            counts = store.counts()
            for row in rows[:20]:
                detail = f"  {row['detail']}" if row["detail"] else ""
                print(
                    f"  {row['session_id']:<28} {row['state']:<8} attempt {row['attempts']}{detail}"
                )
            if counts:
                print(f"\n  {', '.join(f'{k}: {v}' for k, v in sorted(counts.items()))}")
            # Not yet in the table. The first version printed every finished
            # session as "eligible", including the ones already `done`.
            waiting = sum(
                1
                for session_id, _ in pending_sessions(session_dir)
                if store.state_of(session_id) is None
            )
            print(f"  {waiting} session file(s) in {session_dir} not picked up yet")
    # Printed whether or not anything has been remembered: the case it exists
    # for -- a session killed before it could release its lock -- is as likely
    # on the first day as on the hundredth.
    for path, holder in locked_sessions(session_dir):
        print(
            f"  locked, skipped: {path.name} (pid {holder}). If that process is gone, "
            f"delete {path.name}.lock and it will be picked up."
        )
    return 0


def _remember_now(
    directory: Path,
    *,
    session_dir: Path,
    jobs_path: Path,
    provider: str,
    model: str | None,
) -> int:
    """Run the writer in the foreground, and say what it did.

    The same function the background task runs -- not a second implementation
    of it.  What differs is who waits: here the user asked, so waiting is the
    thing they asked for.
    """
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
                # No `exclude`: this command is not a session, so there is no
                # current session to leave out -- and no `headroom` either,
                # because the user is standing here having asked for it. The
                # quota rule is about work nobody requested.
            )
        )
    except Exception as exc:
        # Every failure is a sentence and an exit code here, for the reason in
        # `_finish_writer`: the writer calls the client directly, so its
        # provider errors are not `ModelFailed`.
        print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    print(f"  {report.describe()}")
    for session_id, detail in report.failed:
        print(f"  failed: {session_id}: {detail}", file=sys.stderr)
    return 0
```

> - **`_memory_jobs`**：最多 20 行任务，每行"会话 id、状态、第几次、失败原因"；再一行每种状态各几个；再一行"还有几个会话没被登记"；最后是 §4.7 的被锁住的会话。
>   - 改写时还改了一处小事：第一版那一行写的是"N session file(s) are eligible"，数的是**所有**已结束的会话，包括已经处理完的。现在数的是"还不在任务表里的"——`store.state_of(session_id) is None`。
>   - `if counts:`：表是空的时候不打印一个空的统计行。
> - **`_remember_now`**：在前台跑同一个 `run_pipeline`，不另写一份。没有 `exclude`（这条命令不是一次会话），没有 `headroom`（用户就站在这儿要求它跑，额度规则管的是没人要求的工作）。`except Exception`：§3.6。

```python
def test_F17_11_writing_is_off_unless_asked_for(tmp_path: Path) -> None:
    """Two switches, not one: reading what you wrote and letting a model write
    about you are different consents."""
    tools = top_level_tools(tmp_path, Session(approver=AllowAll()), _sub_context(tmp_path))
    assert NOTE_NAME not in tools.handlers
    with_writing = top_level_tools(
        tmp_path, Session(approver=AllowAll()), _sub_context(tmp_path), remember=tmp_path / "mem"
    )
    assert NOTE_NAME in with_writing.handlers


def test_F17_11_the_prompt_describes_the_tools_that_exist(tmp_path: Path) -> None:
    """Chapter 5's F05-10: a prompt that names a tool the configuration does
    not have gets the model to call it, measured at 2/3."""
    from minicodex.__main__ import _instructions

    session = Session(approver=AllowAll())
    without = top_level_tools(tmp_path, session, _sub_context(tmp_path))
    with_writing = top_level_tools(
        tmp_path, session, _sub_context(tmp_path), remember=tmp_path / "mem"
    )
    assert NOTE_NAME not in _instructions(session, without)
    assert NOTE_NAME in _instructions(session, with_writing)


def test_F17_11_forget_all_deletes_the_extracted_material_too(tmp_path: Path) -> None:
    """ "Delete everything" that leaves `raw/` behind has not deleted everything.

    Stage-1 output is memory in all but name -- it is the user's preferences,
    extracted -- and the job records are what would make a regenerated memory
    come back empty.
    """
    from minicodex.__main__ import _memory

    directory = tmp_path / "mem"
    jobs = tmp_path / "jobs.db"
    write_memory(directory, summary="- a", body="## A\n\nb\n", lock_path=tmp_path / "merge.lock")
    write_raw(directory, Stage1(session_id="s1", preferences=("Use uv.",)))
    propose(directory, "a proposal")
    with JobStore(jobs) as store:
        store.enrol([("s1", Path("s1.jsonl"))])

    assert _memory(directory, forget_all=True, jobs_path=jobs) == 0
    assert not (directory / SUMMARY_FILE).exists()
    assert not (directory / BODY_FILE).exists()
    assert not (directory / USAGE_FILE).exists()
    assert pending_raw(directory) == ()
    assert pending_notes(directory) == ()
    with JobStore(jobs) as store:
        assert store.rows() == []


def test_jobs_counts_only_sessions_not_yet_picked_up(tmp_path: Path, capsys: Any) -> None:
    from minicodex.__main__ import main

    sessions = tmp_path / "sessions"
    _write_session(sessions, "done-one", ["a"])
    _write_session(sessions, "new-one", ["b"])
    jobs = tmp_path / "jobs.db"
    with JobStore(jobs) as store:
        store.enrol([("done-one", sessions / "done-one.jsonl")])
        store.finish("done-one")

    main(["memory", "--jobs", "--jobs-db", str(jobs), "--session-dir", str(sessions)])
    assert "1 session file(s)" in capsys.readouterr().out
```

```python
def test_F17_12_everything_the_write_path_owns_is_under_one_home() -> None:
    """The lock and the jobs database moved with the memory directory.

    Chapter 16 moved memory out of the repository and under the user's home
    (F16-10), matching codex's `memory_root()` =
    `codex_home.join("memories")` (`memories/write/src/lib.rs:116-117`).  The
    two things this chapter owns had to move with it, and the reason is not
    tidiness: they are *about* that one global directory, so leaving them
    per-repository would mean one lock per checkout guarding one shared file.
    """
    from minicodex.memory import DEFAULT_MEMORY_DIR, MINICODEX_HOME
    from minicodex.memory_jobs import DEFAULT_JOBS_PATH
    from minicodex.memory_write import MERGE_LOCK

    assert DEFAULT_MEMORY_DIR.parent == MINICODEX_HOME
    assert MERGE_LOCK.parent == MINICODEX_HOME
    assert DEFAULT_JOBS_PATH.parent == MINICODEX_HOME
    # Beside the memory directory, never inside it: it is a git repository and
    # a binary rewritten on every operation has no readable diff.
    assert DEFAULT_MEMORY_DIR not in MERGE_LOCK.parents
    assert DEFAULT_MEMORY_DIR not in DEFAULT_JOBS_PATH.parents


def test_F17_12_nothing_defaults_into_the_working_directory() -> None:
    """The specific regression: a relative default is read against the cwd.

    Before the move these were `Path(".minicodex") / ...`, so running the
    program from two checkouts gave two locks and two job tables -- each
    believing it was the only writer of the one memory they now share.  An
    absolute default is what makes "the single writer" true across checkouts
    rather than only within one.
    """
    from minicodex.memory import DEFAULT_MEMORY_DIR
    from minicodex.memory_jobs import DEFAULT_JOBS_PATH
    from minicodex.memory_write import MERGE_LOCK

    for path in (DEFAULT_MEMORY_DIR, MERGE_LOCK, DEFAULT_JOBS_PATH):
        assert path.is_absolute(), f"{path} is relative and would follow the cwd"


def test_F17_12_a_stale_project_local_memory_is_not_read(tmp_path: Path) -> None:
    """A leftover `.minicodex/memories` from before the move is not memory.

    The failure this guards is silent: the old directory is still on disk in
    every repository that ran the previous version, and a loader that fell
    back to it would serve a year-old memory to a program that thinks it is
    reading the new one.  Nothing falls back -- `load` reads the directory it
    is given, and the default is absolute.
    """
    from minicodex.memory import load as load_memory

    stale = tmp_path / ".minicodex" / "memories"
    stale.mkdir(parents=True)
    (stale / SUMMARY_FILE).write_text(f"{FORMAT_VERSION}\n\n- old and wrong\n", encoding="utf-8")

    fresh = tmp_path / "home" / ".minicodex" / "memories"
    fresh.mkdir(parents=True)
    (fresh / SUMMARY_FILE).write_text(f"{FORMAT_VERSION}\n\n- current\n", encoding="utf-8")

    assert "current" in load_memory(fresh).summary
    assert "old and wrong" not in load_memory(fresh).summary
```

> F17-12 的三个测试守的是 §2.1 那次搬家：锁、任务库、记忆目录都在 `MINICODEX_HOME` 下；没有任何一个默认路径落在当前目录里；一个第 16 章第一版留在项目里的旧记忆目录，不会被读到。

## §13 结果：自动生成的记忆 vs 手写的

### 13.1 三条臂

用第 16 章那六个任务，三条臂：

| 臂 | 记忆从哪来 |
|---|---|
| `off` | 没有 |
| `hand` | 第 16 章的 fixture——**每个任务一份，量身定做，里面就是答案** |
| `generated` | 本章的管线跑完四条真实会话产出的**一份**记忆，六个任务共用 |

这个对比对 `generated` 是不利的，而且是故意的：手写臂的每个任务都拿到
专门为它写的记忆，生成臂拿到的是"这个管线在别的工作区里学到的全部东西"。

```
$ uv run python probe_memory_write.py ab --samples 3

  task                 off          hand     generated
  convention           0/3           2/3           3/3
  runner               0/3           3/3           3/3
  layout               0/3           3/3           3/3
  stale                1/3           3/3           0/3
  unrelated            3/3           3/3           3/3
  poisoned             3/3           3/3           3/3
  TOTAL               7/18         17/18         15/18

  made worse by the generated memory: stale: 3 -> 0
  made worse than no memory at all: stale: 1 -> 0
```

先看那份记忆本身。管线读了五条会话（其中一条什么都没教），产出：

```
  memory writer: 2 session(s) read, 2 entr(ies), nothing forgotten in 10.1s
  memory writer: 2 session(s) read, 1 had nothing to keep, 4 entr(ies), nothing forgotten in 10.3s
  memory writer: 1 session(s) read, 6 entr(ies), nothing forgotten, 1 secret(s) removed in 9.3s
  memory writer: nothing to do

  generated memory: 6 entr(ies), 219 resident token(s)

    - Every function carries a one-line docstring; reviewers reject patches without them.
    - Run tests with `python -m pytest` to avoid import errors.
    - New string helpers should be placed in `util/text.py`.
    ...

    ## Function Documentation / ## Running Tests / ## String Helpers
    ## pad_string Function / ## Project Dependencies / ## Applying Patches
```

三条真约定全在，而且是常驻摘要的前三条。后三节里有一节是垃圾
（`## pad_string Function`），一节是边缘有用（`## Applying Patches`）。

第三行那个 **`1 secret(s) removed`** 是 §6 那条会话——它读过一个
`.env`。最终的两个文件里 `grep sk-live` 是 **0**。

### 13.2 那三分差在哪

全在 `stale` 上，而这个任务的构造决定了它必然是这样：

`stale` 的记忆是**故意写错的**——它说入口是 `app/main.py`，而工作区里只有
`app/cli.py`。手写臂之所以 3/3，不是因为记忆知道答案，是因为第 16 章的
**漂移核对**（`missing_paths()` 逐个 `is_file()`）发现那个文件不存在，
于是在块外加了一句"记忆过期了，去仓库里找"，模型就去看了。

生成的记忆**对入口点一个字都没说**——它是从别的工作区的四条会话里学来的。
没有路径可核对，就没有漂移提示，模型只能自己想办法。于是它的分数就是
`off` 臂的分数。

单独把这个任务再跑 3 个样本：

```
  task            off          hand     generated
  stale           1/3           3/3           2/3
```

**这个任务的方差比三分之差还大。** 两次运行加起来，`generated` 是 2/6，
`off` 是 2/6，`hand` 是 6/6。诚实的说法是：

> 在 `stale` 上，生成的记忆和**没有记忆**没有区别；
> 而手写臂赢它，靠的是"记忆错得恰好能被 `is_file()` 抓住"。

去掉这一个任务，另外五个任务是 **off 6/15、hand 14/15、generated 15/15**
——`convention` 那一分还是生成臂赢的（3/3 对 2/3，手写臂这次掉了一个样本，
第 16 章同一个任务是 3/3，属于抽样噪声）。

### 13.3 所以第 17 章值不值得写

值得，但结论要说准：

1. **在管线有材料的地方，自动记忆追平了手写记忆。** 这是本章的主结果。
2. **管线没有材料的地方，它就是"没有记忆"。** 自动化不会凭空知道你没告诉过它的事。
3. **手写记忆在一个任务上赢，靠的是第 16 章的漂移核对，不是记忆本身。**
   换句话说：那一分是**读路径**赚的，不是写路径丢的。
4. **代价是它会顺手记下垃圾。** 六节里一节纯废话，而且它会跟着每个请求走。
   §5.7 那笔欠债在这里显形。

---

### 13.4 代码：把两段串起来的那个函数

报告：

```python
@dataclass(frozen=True)
class WriteReport:
    """What one run of the pipeline did, in a form that prints in one line.

    A report rather than a log, because the pipeline runs while the user is
    doing something else and the only place it can say anything is one line at
    the end of somebody else's task.  A background job that says nothing is a
    background job nobody can tell is broken -- and this one silently changes
    every future request.
    """

    skipped: str = ""
    sessions: tuple[str, ...] = ()
    empty: tuple[str, ...] = ()
    merged: bool = False
    pruned: Prune | None = None
    secrets_removed: int = 0
    seconds: float = 0.0
    failed: tuple[tuple[str, str], ...] = ()
    # Set when another process held the merge lock: this run's extraction is
    # on disk in `raw/` and the other process, or the next run, merges it.
    deferred: str = ""

    def describe(self) -> str:
        if self.skipped:
            return f"memory writer: skipped ({self.skipped})"
        if not self.sessions and not self.merged:
            return "memory writer: nothing to do"
        parts = [f"{len(self.sessions)} session(s) read"]
        if self.empty:
            parts.append(f"{len(self.empty)} had nothing to keep")
        if self.merged and self.pruned is not None:
            parts.append(self.pruned.describe())
        elif self.deferred:
            parts.append(f"merge left to {self.deferred}")
        elif not self.merged:
            parts.append("no merge")
        if self.secrets_removed:
            parts.append(f"{self.secrets_removed} secret(s) removed")
        if self.failed:
            parts.append(f"{len(self.failed)} failed")
        return f"memory writer: {', '.join(parts)} in {self.seconds:.1f}s"
```

> 一份报告，打印成一行，挂在别人的任务后面。说明里那句是这个类型存在的理由：**一个不说话的后台任务，是一个没人能判断它坏没坏的后台任务——而这一个会悄悄改变以后的每一个请求。**

整条管线：

```python
async def run_pipeline(
    model: Any,
    *,
    directory: Path,
    sessions_dir: Path,
    jobs_path: Path,
    limit: int = 2,
    exclude: Sequence[str] = (),
    headroom: float | Callable[[], float | None] | None = None,
    now: float | None = None,
    lock_path: Path = MERGE_LOCK,
) -> WriteReport:
    """Extract from what is pending, merge, prune, commit.

    Runs at the **start** of the next session rather than at the end of this
    one, which is the whole of F17-01: a user who has just been told the answer
    wants their prompt back, not fourteen seconds of a progress line about
    bookkeeping.  Starting it means it overlaps with the first model call
    instead of with nothing.

    Everything in here is bounded: `limit` sessions per run, a lease on each
    one, one merge, one lock.  The unbounded version is a program that appears
    to hang the first time somebody switches it on after a month of sessions.
    """
    began = time.monotonic()
    stamp = now if now is not None else time.time()
    # A callable is allowed, and the reason is a real ordering problem rather
    # than flexibility for its own sake: this task is created *before* the
    # foreground run's first request, so at creation time nobody has seen a
    # rate-limit header yet.  Passing the number would mean passing `None`
    # forever; passing the question means it is answered at the moment it
    # matters.  `None` means "the provider does not say", which is not the
    # same as "there is plenty" -- and the choice made here is to proceed,
    # because the alternative switches the feature off entirely against every
    # server that reports nothing, ollama included.
    if callable(headroom):
        headroom = headroom()
    if headroom is not None and headroom < MIN_RATE_LIMIT_REMAINING_PERCENT:
        # Background work gives way to foreground work.  Not a queue, not a
        # slower rate: nothing at all, until the window resets.  The failure
        # this prevents is the one where a user's actual task gets the 429 that
        # the memory writer earned (F17-02).
        return WriteReport(skipped=f"{headroom:.0f}% quota left", seconds=0.0)

    from minicodex.memory_jobs import JobStore, pending_sessions

    read: list[str] = []
    empty: list[str] = []
    failed: list[tuple[str, str]] = []
    secrets = 0
    with JobStore(jobs_path) as store:
        store.enrol(pending_sessions(sessions_dir, exclude=exclude), now=stamp)
        for job in store.claim(limit=limit, now=stamp):
            try:
                from minicodex.rollout import read_rollout

                stage1 = await extract(model, read_rollout(job.path))
            except Exception as exc:
                # Chapter 0's rule, in a place with no model to hand the error
                # to: one unreadable session cannot be allowed to stop the
                # other one, and it cannot be allowed to take the user's run
                # down either.  It goes back in the queue with a backoff, and
                # after three attempts it stops coming back.
                failed.append((job.session_id, f"{type(exc).__name__}: {exc}"))
                store.fail(job.session_id, f"{type(exc).__name__}: {exc}", now=stamp)
                continue
            secrets += stage1.secrets_removed
            read.append(job.session_id)
            if stage1:
                write_raw(directory, stage1)
            else:
                # Nothing worth keeping is a *success*, and the job is done.
                # Writing an empty raw file instead would make the merge step
                # read a page of empty headings on every run.
                empty.append(job.session_id)
            store.finish(job.session_id, now=stamp)

    def report(**extra: Any) -> WriteReport:
        return WriteReport(
            sessions=tuple(read),
            empty=tuple(empty),
            secrets_removed=secrets,
            seconds=time.monotonic() - began,
            failed=tuple(failed),
            **extra,
        )

    # Stage 2 runs under the merge lock from the first read to the last
    # delete, not only around the write. The first version took the lock
    # inside `write_memory`, so two processes could each read the memory, each
    # spend a model call merging, and then write one after the other: the
    # second write was built on a memory from before the first, and silently
    # replaced it. Measured with two overlapping runs over two sessions: both
    # reported success, one session's material was in neither file, and its
    # `raw/` file had been deleted by the run whose write was then overwritten.
    #
    # A run that finds the lock taken does not wait for it and does not fail.
    # Its extraction is already in `raw/`, which is exactly where the run
    # holding the lock -- or the next one -- will look.
    try:
        fd = _lock(lock_path)
    except MemoryWriteError:
        return report(deferred="another minicodex")
    try:
        raw = pending_raw(directory)
        notes = pending_notes(directory)
        if not raw and not notes:
            return report()

        ensure_repo(directory)
        write_gitignore(directory)
        from minicodex.memory import load as load_memory

        # Read here, under the lock, and never taken from the caller. A
        # memory loaded when the session started is one some other process
        # may have rewritten since, which is the lost update above by a
        # second route.
        current = load_memory(directory)
        merged = await consolidate(
            model, memory=current, raw=raw, notes=notes, hand_edits=hand_edits(directory)
        )
        pruned = write_memory(
            directory, summary=merged.summary, body=merged.body, now=stamp, lock_path=None
        )
        commit(directory, f"memory: {len(raw)} session(s), {len(notes)} note(s)")
        # Consumed only once the write succeeded.  The other order loses the
        # extraction work on any failure in the merge, and the extraction is the
        # expensive half.
        for path, _ in (*raw, *notes):
            try:
                path.unlink()
            except OSError:  # pragma: no cover
                pass
    finally:
        _unlock(fd, lock_path)
    return WriteReport(
        sessions=tuple(read),
        empty=tuple(empty),
        merged=True,
        pruned=pruned,
        secrets_removed=secrets + merged.secrets_removed,
        seconds=time.monotonic() - began,
        failed=tuple(failed),
    )
```

从上往下：

> 1. **额度**（§7）：`headroom` 是函数就现在调用它；低于 25% 就整个不跑。
> 2. **第一段**：`with JobStore(...) as store:` 打开任务表；`enrol` 登记新会话；`claim` 最多认领 `limit` 条。每一条：读会话、抽取。**抽取失败**就交还给任务表退避（`store.fail`），**继续下一条**——一条读不了的会话不能挡住另一条（第 0 章的规矩）。抽取成功：有东西就写 `raw/`，没东西就记一笔"没什么可留的"，两种都算 `finish`。
> 3. **第二段**：§8.7 的全部——拿锁，读 `raw/` 和 `notes/`，没东西就收工；有就确保 git 仓库、从磁盘读当前记忆、合并、写盘、提交、删掉用过的临时文件、放锁。
> 4. **"临时文件写盘成功之后才删"**：反过来的话，合并一出错，抽取的结果就丢了——而抽取才是贵的那一半。

`from minicodex.memory_jobs import ...` 和 `from minicodex.rollout import read_rollout` 都写在函数里面：`memory_write` 被 `composition` 导入，`memory_jobs` 又导入 `memory`，延迟导入让模块之间的箭头保持简单（插曲 B 的分层检查会盯着）。

```python
def test_the_pipeline_extracts_merges_prunes_and_cleans_up(tmp_path: Path) -> None:
    sessions = tmp_path / "sessions"
    _write_session(sessions, "s1", ["always use uv"])
    directory = tmp_path / "mem"
    propose(directory, "The linter is `ruff check --fix`.")

    model = _Scripted(
        [
            "DURABLE: yes\n## Preference signals\n- This project is run with `uv`.\n",
            f"{SUMMARY_MARK}\n- Use uv.\n{BODY_MARK}\n## Tooling\n\nRun everything with `uv`.\n",
        ]
    )
    report = asyncio.run(
        run_pipeline(
            model,
            directory=directory,
            sessions_dir=sessions,
            jobs_path=tmp_path / "jobs.db",
            lock_path=tmp_path / "merge.lock",
        )
    )
    assert report.merged
    assert report.sessions == ("s1",)
    memory = load(directory)
    assert [e.title for e in memory.entries] == ["Tooling"]
    # Consumed only after the write succeeded.
    assert pending_raw(directory) == ()
    assert pending_notes(directory) == ()
    # ...and the merge was shown both inputs.
    assert "ruff check --fix" in model.requests[1][1]["content"]
    assert "This project is run with `uv`." in model.requests[1][1]["content"]


def test_the_raw_file_survives_a_merge_that_fails(tmp_path: Path) -> None:
    """Extraction is the expensive half, so it is never thrown away for free.

    The other order -- delete the raw files, then merge -- loses a model call
    per session on any failure in the second stage.
    """
    sessions = tmp_path / "sessions"
    _write_session(sessions, "s1", ["always use uv"])
    directory = tmp_path / "mem"
    model = _Scripted(
        [
            "DURABLE: yes\n## Preference signals\n- This project is run with `uv`.\n",
            "I am afraid I cannot do that.",
        ]
    )
    with pytest.raises(MemoryWriteError):
        asyncio.run(
            run_pipeline(
                model,
                directory=directory,
                sessions_dir=sessions,
                jobs_path=tmp_path / "jobs.db",
                lock_path=tmp_path / "merge.lock",
            )
        )
    assert len(pending_raw(directory)) == 1


def test_one_unreadable_session_does_not_stop_the_other(tmp_path: Path) -> None:
    """Chapter 0's rule in a place with no model to hand the error to."""
    sessions = tmp_path / "sessions"
    _write_session(sessions, "good", ["always use uv"])
    (sessions / "bad.jsonl").write_text(
        json.dumps({"type": "meta", "session_id": "bad"}) + "\n" + '{"type": "user"}\n',
        encoding="utf-8",
    )
    jobs = tmp_path / "jobs.db"
    model = _Scripted(
        [
            "DURABLE: yes\n## Preference signals\n- This project is run with `uv`.\n",
            f"{SUMMARY_MARK}\n- Use uv.\n{BODY_MARK}\n## Tooling\n\nRun everything with `uv`.\n",
        ]
    )
    report = asyncio.run(
        run_pipeline(
            model,
            directory=tmp_path / "mem",
            sessions_dir=sessions,
            jobs_path=jobs,
            lock_path=tmp_path / "merge.lock",
        )
    )
    # The unreadable one is not even eligible: `read_rollout` refuses it during
    # the scan, so it never becomes a job at all.
    assert report.sessions == ("good",)
    assert report.merged
    with JobStore(jobs) as store:
        assert store.state_of("bad") is None
```

> - 第一个是整条路走一遍：一次会话、一条提议的笔记、两次模型调用（抽取、合并），然后检查记忆里只有合并出来的那一节、临时文件都删了、合并时**两样输入都给模型看了**。
> - 第二个：合并的回答缺标记，`run_pipeline` 抛错——`raw/` 文件必须还在。
> - 第三个：两条会话，第一条的文件是坏的；第二条照样被处理，第一条进了 `failed` 列表。

## §14 验证

### 14.1 测试文件

`tests/test_faults_ch17.py` 的开头：

```python
"""Chapter 17 -- memory, the write path: extraction, merging, forgetting.

Offline, all of it, for chapter 14's reason (F14-05).  Two model calls sit in
the middle of this subsystem and both are replaced here by a scripted answer:
what a real one produces is measured in `probe_memory_write.py`, with the
sample counts written down, and what is pinned here is everything that is a
fact about the program -- what is claimed, what is written, what is refused,
what is forgotten and in what order.

The one thing worth saying about the split: the *prompts* are pinned here as
snapshots even though their effect is measured elsewhere.  That is chapter 3's
F03-10 discipline (an edit to a measured wording must be a visible edit), and
this chapter has two wordings whose difference was bought at 3/3 against 0/3.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import time
from pathlib import Path
from typing import Any, ClassVar

import pytest

from minicodex.approval import AllowAll, Session
from minicodex.composition import top_level_tools
from minicodex.memory import (
    BODY_FILE,
    FORMAT_VERSION,
    SUMMARY_FILE,
    SUMMARY_TOKEN_BUDGET,
    USAGE_FILE,
    CitedEntry,
    Entry,
    load,
    record_uses,
)
from minicodex.memory_jobs import (
    BACKOFF_BASE_SECONDS,
    MAX_ATTEMPTS,
    MAX_PER_STARTUP,
    JobStore,
    locked_sessions,
    pending_sessions,
)
from minicodex.memory_write import (
    BODY_MARK,
    MAX_ENTRIES,
    MIN_RATE_LIMIT_REMAINING_PERCENT,
    NOTE_NAME,
    NOTES_DIR,
    RAW_DIR,
    REDACTED,
    STAGE1_QUOTA,
    STAGE1_SYSTEM,
    STAGE2_SYSTEM,
    SUMMARY_MARK,
    MemoryWriteError,
    Stage1,
    commit,
    ensure_repo,
    extract,
    git_available,
    hand_edits,
    merge_request,
    note_toolset,
    parse_merged,
    parse_stage1,
    pending_notes,
    pending_raw,
    propose,
    prune,
    run_pipeline,
    scrub,
    transcript_of,
    trim_summary,
    write_memory,
    write_raw,
)
from minicodex.model import RateLimit
from minicodex.rollout import RolloutWriter, SessionMeta, read_rollout
from minicodex.subagent import SubAgentContext

DAY = 86400.0

needs_git = pytest.mark.skipif(not git_available(), reason="git is not on PATH")
```

底部的三个辅助：

```python
class _Scripted:
    """Answers each request with the next canned string, and keeps the requests.

    Not a `ScriptedModel` from an earlier chapter: those speak the agent loop's
    protocol (tool calls, turns).  Stage 1 and stage 2 are single completions
    with no tools at all, and a fixture that modelled turns here would be
    modelling something this subsystem does not have.
    """

    def __init__(self, answers: list[str], *, finish: bool = True) -> None:
        self.answers = list(answers)
        self.finish = finish
        self.requests: list[list[dict[str, Any]]] = []

    async def _stream(self, messages):
        from minicodex.model import Completed, TextDelta

        self.requests.append(list(messages))
        yield TextDelta(self.answers.pop(0) if self.answers else "")
        if self.finish:
            yield Completed("stop")

    def stream(self, messages):
        return self._stream(messages)


def _write_session(
    directory: Path,
    session_id: str,
    users: list[str],
    *,
    parent: str | None = None,
    notes: list[str] | None = None,
    calls: list[tuple[str, str]] | None = None,
) -> Path:
    """A real rollout file, written through the real writer.

    Hand-rolled JSON would be a second implementation of the format, and
    chapter 14 has a whole section on what a fixture that reimplements the
    code under test measures.
    """
    from minicodex.agent_types import ToolCall
    from minicodex.history import AssistantMessage, SystemNote, ToolResult, UserMessage

    meta = SessionMeta(session_id=session_id, parent=parent)
    path = directory / f"{session_id}.jsonl"
    with RolloutWriter(path, meta) as writer:
        for note in notes or []:
            writer.append(SystemNote(note))
        for text in users:
            writer.append(UserMessage(text))
        made = [ToolCall(f"c{i}", name, {}, args) for i, (name, args) in enumerate(calls or [])]
        writer.append(AssistantMessage("on it", tuple(made)))
        for call in made:
            writer.append(ToolResult(call.call_id, call.name, "done"))
        if not made:
            writer.append(AssistantMessage("done", ()))
    return path


def _sub_context(root: Path) -> SubAgentContext:
    from minicodex.composition import sub_context
    from minicodex.shell import ShellSession

    return sub_context(
        build_model=lambda tools: None,
        root=root,
        session=Session(approver=AllowAll()),
        parent_shell=ShellSession(),
        wiring=__import__("minicodex.agent", fromlist=["Wiring"]).Wiring(),
    )
```

> - **`_Scripted`**：每次被问就按顺序说出列表里的下一句，并把收到的消息存进 `requests`。`finish=False` 时不发结束信号——用来测"提前断掉的流"。说明里讲了为什么不用前几章的 `ScriptedModel`：那些说的是 Agent 循环的协议（工具调用、多轮），而这一章的两段都是一问一答，没有工具。
> - **`_write_session`**：用第 7 章真正的 `RolloutWriter` 写一个会话文件，可以带系统消息、子 Agent 的 `parent`、工具调用。
> - `_sub_context`：造一个最小的子 Agent 上下文，给"子 Agent 有没有笔记工具"那个测试用。

```
$ uv run pytest tests/test_faults_ch17.py
..................................................................       [100%]
66 passed in 3.57s
```

### 14.2 这些测试自己靠得住吗

```
$ uv run python probe_mutations_ch17.py
50 mutations, tests/test_faults_ch17.py tests/test_faults_ch16.py tests/test_faults_ch07.py tests/test_schemas.py

   11 test(s) fail  <-  the claim is a select followed by an update, outside any transaction
    1 test(s) fail  <-  an expired lease is never taken back, so a killed run loses the session
    3 test(s) fail  <-  the attempt is counted on failure instead of on claim
    1 test(s) fail  <-  a failing job comes back forever instead of giving up
    1 test(s) fail  <-  failure retries immediately, with no backoff
    2 test(s) fail  <-  every session is eligible, including the ones the program had with itself
    2 test(s) fail  <-  a session that is still being written is eligible
    1 test(s) fail  <-  the extraction prompt goes back to the quota wording measured at 0/3 no-ops
    1 test(s) fail  <-  `DURABLE: no` is advisory: the bullets under it are kept anyway
    1 test(s) fail  <-  a placeholder bullet becomes a memory entry
    1 test(s) fail  <-  material outside a known heading is swept into the first section
    1 test(s) fail  <-  an empty extraction still writes a raw file for the merge to read
    1 test(s) fail  <-  the raw file does not say it is temporary
    1 test(s) fail  <-  the transcript is sent to the model unscrubbed
    1 test(s) fail  <-  the model's own answer is written to disk unscrubbed
    1 test(s) fail  <-  a credential named as one but not shaped like one gets through
    2 test(s) fail  <-  one secret matched by two patterns is reported as two
    1 test(s) fail  <-  the merge answer is written without being scrubbed
    2 test(s) fail  <-  forgetting is by age, which deletes the entry that is old and used
   12 test(s) fail  <-  an entry with no citations is dropped whatever its age
    9 test(s) fail  <-  an entry nothing has recorded yet is treated as ancient rather than new
    1 test(s) fail  <-  the cap is not a cap
    1 test(s) fail  <-  the writer trims the summary with its own ruler, not the reader's
    2 test(s) fail  <-  the file is rewritten in ranking order, so every merge reshuffles the diff
    1 test(s) fail  <-  usage rows for entries that no longer exist are kept
    1 test(s) fail  <-  the resident half is left at whatever length the merge produced
   10 test(s) fail  <-  the merge lock is not taken
    1 test(s) fail  <-  a merge that produced nothing replaces the memory with nothing
    2 test(s) fail  <-  a merge answer with no markers is treated as the body
    3 test(s) fail  <-  a note may be written straight into the body file
    1 test(s) fail  <-  one session may propose as many notes as it likes
    2 test(s) fail  <-  a note overwrites the one before it
    1 test(s) fail  <-  the extractor is shown the note the same session proposed
   10 test(s) fail  <-  the extractor is shown the program's own system notes, memory included
    1 test(s) fail  <-  the writer runs whatever is left of the quota
    6 test(s) fail  <-  an unknown headroom is read as no headroom
    3 test(s) fail  <-  the note tool is present whether or not the user asked to be remembered
    1 test(s) fail  <-  the rate-limit headers are never read
    2 test(s) fail  <-  the memory directory is back inside whatever repository is the cwd
    2 test(s) fail  <-  the merge lock is per-repository again, so two checkouts both hold it
    3 test(s) fail  <-  the jobs database is per-repository, so one session is extracted once per checkout
    2 test(s) fail  <-  the jobs database moves inside the memory directory the merge diffs
   12 test(s) fail  <-  zero citations alone is enough to forget an entry
    9 test(s) fail  <-  an entry nobody has a record of is treated as ancient rather than as new
    1 test(s) fail  <-  a run that finds the merge lock taken fails instead of leaving its raw file
    1 test(s) fail  <-  stage 2 reads, merges and writes without holding the merge lock
    1 test(s) fail  <-  a writer failure reported by the task itself escapes after the answer
    1 test(s) fail  <-  a writer failure during the grace wait escapes after the answer
    1 test(s) fail  <-  a session locked by a dead process is skipped without being named
    1 test(s) fail  <-  --jobs counts sessions that are already done as waiting

every mutation was caught.
```

（这次在 Linux 上实测。）

最后六条是改写时加的，一个修复一条。最上面那条——`BEGIN IMMEDIATE` 换成 `pass`——有 11 个测试变红，说明"先拿写锁"这件事被守得很牢；可它守住的是**第一段**。§8.7 那个丢失更新在第二段，第一版的 44 条变异里没有一条碰到它，因为没有一条测试在"两个进程都走到第二段"这个场景里。

### 14.3 全套

```
$ uv run pytest
1781 passed in 72.18s (0:01:12)

$ uv run ruff format --check . && uv run ruff check .
All checks passed!

$ uv run python scripts/check_layers.py --quiet && echo ok
ok
```

### 14.4 改写时的一次自伤

这一节值得记一笔，因为它是这本书讲了很多遍的那种错，这一次是我自己在改写时犯的。

修完这一章的 bug，我要把同样的改动同步到后面几章的快照里。做法是 `git diff` 出一个补丁，再一个个打上去。打补丁的时候，第 14.2 节的变异脚本**正在后台跑**——它的工作方式是把源码改坏一行、跑测试、再改回来。我生成补丁的那一刻，`memory_jobs.py` 正处于"`BEGIN IMMEDIATE` 被换成 `pass`"的状态。

于是这个被改坏的版本被打进了第 15、18 到 23 章和 `minicodex-live`。后面那些快照的测试一跑，11 个红——正是变异检查本来要证明"会红"的那 11 个。

修好很简单。值得记的是：**变异检查会在磁盘上留下"故意改坏的代码"**，哪怕只有几秒钟。第 13 章记过一次"变异脚本被打断，留下一行被改坏的源码"，那次的修法是脚本退出时一定还原（`atexit`）。这一次脚本没被打断，是**别的东西在它跑的时候读了文件**。规矩加一条：变异检查跑着的时候，不要从同一个目录里复制、打补丁、提交任何东西。

---

## §15 收工

### 15.1 这一章动了哪些文件

| 文件 | 变化 | 哪一节 |
|---|---|---|
| `src/minicodex/memory_write.py` | 新增 | §2.5、§5.9、§6.5、§8.5–§8.7、§9.5、§10.5、§13.4 |
| `src/minicodex/memory_jobs.py` | 新增 | §4.5、§4.7 |
| `src/minicodex/memory.py` | `MINICODEX_HOME`；几段说明 | §2.5 |
| `src/minicodex/model.py` | `RateLimit`；成功响应上读额度 | §7.5 |
| `src/minicodex/composition.py` | `remember` 参数，挂笔记工具 | §8.5 |
| `src/minicodex/__main__.py` | 后台写入、`_finish_writer`、`--remember`、`memory --jobs/--remember-now` | §3.5、§12.4 |
| `src/minicodex/rollout.py` | 一段注释：一个字段的诞生与删除 | §11.1 |
| `tests/test_faults_ch17.py` | 新增，66 个测试 | 各节 |
| `probe_memory_write.py` | 新增，九段 | §3–§13 |
| `probe_mutations_ch17.py` | 新增，50 条 | §14.2 |

`probe_memory_write.py` 八百多行，正文只引了它的输出。它和前几章的探针是同一种东西：每一段对应一次测量，用的都是这一章讲过的函数。

### 15.2 这一章的提交

```
fix(memory): hold the merge lock across stage 2; never let the writer fail the run
feat(memory): forget-all, --jobs and --remember-now
feat(memory): forgetting -- citations first, recency second, age only to break ties
feat(memory): the memory directory is a git repository, and hand edits are the merge's input
feat(memory): stage 2 merges under a single-writer lock; the model only proposes
feat(memory): read the provider's remaining quota off every successful response
feat(memory): stage 1 asks for a verdict before it shows a shape, and scrubs twice
feat(memory): run the writer beside the next session, not after this one
feat(memory): a claim table for sessions waiting to become memory
```

（最新的在最上面。）最上面那条是改写时找到的四处，放在一个提交里：它们修的地方散在前面几个提交里，但都是被同一个问题问出来的——"两个进程同时跑会怎样""写入失败了用户看到什么"——而第一版只在一个进程、一切顺利的情况下量过。

### 15.3 PR 描述

```markdown
## What

The program writes its own memory: finished sessions are read at the start
of the next one (stage 1, one model call each, at most two per startup),
merged into chapter 16's two files under a single-writer lock (stage 2),
and pruned by citation count. `--remember` turns it on; `minicodex memory
--jobs` shows where it stands; `--forget-all` deletes all of it.

## Why

Chapter 16 measured a hand-written memory as the upper bound. This
measures how close an automatic one gets: on every task it had material
for, as close as the hand-written one (15/18 against 17/18; the gap is one
task, explained in §13.2).

## Testing

66 tests, all offline; 50 mutations, all caught.

## Notes for the reviewer

- First database in the book (sqlite, stdlib). `BEGIN IMMEDIATE` is not
  decoration: the obvious select-then-update duplicated claims in 3 of 6
  runs here, every run on the machine it was first measured on.
- Stage 2 holds the merge lock from its first read to its last delete;
  a run that finds it taken leaves its raw file and says so.
- The model can only append a note. `write_memory` is the one function
  that writes MEMORY.md.
- Known: the "per bullet" junk filter is not done (§5.7); `usage.json`
  has two writers that share no lock (§10.5).
```

### 15.4 自己审一遍

> **1.** 第二段拿不到锁就"延后"。如果拿着锁的那个进程被杀了呢？

锁文件留下，以后每一次运行都拿不到锁，每一次都"延后"。好在它会**说出来**：报告里是 "merge left to another minicodex"，而 `_lock` 的错误消息里有 pid 和"那个进程没了就删掉这个文件"——只是现在那条消息被 `run_pipeline` 吞掉了，换成了一个不带 pid 的 "another minicodex"。这是一个退步，应该把 pid 带进报告。记成已知问题，章末练习 2。

> **2.** `_finish_writer` 现在接住所有 `Exception`。一个真正的编程错误（比如 `TypeError`）也会被吞成一行"failed, nothing written"。

会。它不会被吞掉看不见——那一行字会打印出来，带着异常的类型和消息——只是不会有 traceback、退出码也是 0。取舍是：用户的问题已经回答了，一个记笔记的后台任务里的 bug 不该把这次运行变成失败。如果要查，`--remember-now` 在前台跑同一个函数。

> **3.** `pending_sessions` 每次启动都把会话目录里**每一个**文件完整读一遍，包括早就处理完的。

是。几百个会话时这会变慢。正确的做法是先查任务表、只读不在表里的那些——可 `pending_sessions` 的工作恰恰是决定"哪些该进表"，需要读会话的 `parent` 字段。可以按文件名先过滤掉已在表里的。没做，记下来。

> **4.** `locked_sessions` 把每个带锁的会话都列出来，包括**正在**跑的那个（另一个终端里的 `minicodex`）。用户会不会照着提示把一个活着的会话的锁删掉？

可能。提示里写的是"如果那个进程已经不在了"，和第 7 章的措辞一样，把判断交给人。

## §16 对照 codex

| 这里 | codex | 差在哪 |
|---|---|---|
| `memory_write.py` 里两个函数 | `memories/write/src/phase1.rs` / `phase2.rs` | 同一条缝，同一个位置 |
| `raw/<session>.md`，头部写明临时 | prompt 里明写 "Temporary file: merged raw memories from Phase 1. Input for Phase 2."（`memories/write/templates/memories/consolidation.md:29`） | 一样 |
| 第一段串行跑两条 | `CONCURRENCY_LIMIT: usize = 8`（`memories/write/src/lib.rs:81`） | 它八条并发；我一批就两条，给两个元素开池子是给谁都不开 |
| 第二段是**一次模型调用** | 第二段是一个 `ephemeral = true`、无网络、只能写 `~/.codex/memories/`、`AskForApproval::Never`、**并且关掉自己那份记忆生成**的内部子 Agent | 它的记忆大到需要多步；我的输入一个请求装得下。而子 Agent 需要一个"能写记忆的工具"，那正是 F17-09 说模型不能有的东西 |
| `memory_jobs.sqlite3`：一张 jobs 表 | `state/memory_migrations/0001_memories.sql`：`stage1_outputs`（按 `thread_id` 一行）+ `jobs`（按 `(kind, job_key)`，带 `lease_until` / `retry_at` / `retry_remaining` 和两个水位列） | 它把第一段的输出也存进库；我存成文件，因为那个文件要能被人打开看。**这个差别在 F17-13 里要付账**：没有 per-rollout 的行，就没有 `<rollout_ids>` 可引用 |
| `MAX_PER_STARTUP = 2` | `DEFAULT_MEMORIES_MAX_ROLLOUTS_PER_STARTUP: usize = 2`（`config/src/types.rs:46`），可配为 `memories.max_rollouts_per_startup`（`:309,331`） | 一样 |
| `MIN_RATE_LIMIT_REMAINING_PERCENT = 25` | `DEFAULT_MEMORIES_MIN_RATE_LIMIT_REMAINING_PERCENT: i64 = 25`（`config/src/types.rs:49`），在 `guard::rate_limits_ok`（`memories/write/src/guard.rs:9,38`）里跑管线之前查 | 一样 |
| `notes/<ts>-<pid>-N.md` + `remember_this` | `extensions/ad_hoc/notes/` + `add_ad_hoc_note` | 一样，包括"只能追加不能编辑"这条 |
| 记忆目录 `git init` | 同样 `git init`，Phase 2 读 `phase2_workspace_diff.md` | 一样；我实测出这个机制只买到"删除"这一列（§9.2） |
| 锁和任务库在 `~/.minicodex/` 下，是记忆目录的**兄弟** | 记忆根 `codex_home.join("memories")`（`memories/write/src/lib.rs:116-117`），任务/租约在**另一个** SQLite 库里 | 一样，而且这一条是 F17-12 补上的：第 16 章把记忆搬去全局之后，这两个不跟着搬就等于每个 checkout 一把锁 |
| `prune()` 按引用数排序 | `usage_count` / `last_usage` 排 Phase-2 选取与保留剪枝（`state/src/runtime/memories.rs:389-413`），**只由引用信号喂**（`core/src/stream_events_utils.rs:184`） | 一样，包括"行为信号不参与"这条：`core/src/memory_usage.rs:9-27` 只喂遥测计数器。见 F17-13 |
| 两份 prompt 加起来约 150 行 | `stage_one_system.md` **569 行**、`consolidation.md` **880 行** | 差一个数量级。它的 prompt 是这个子系统里最长的两个文件——**prompt 本身就是源码** |
| `--remember` 默认关 | `[memories]` 配置表（`config/src/types.rs:290-352`）之上还压着一个 `default_enabled: false` 的 feature flag | 一样 |

**最值得看的一行是 prompt 行数那一行。** 1449 行 prompt 对 150 行，
而我这 150 行里每一段都是被一次测量买下来的。
codex 那 1449 行大概率也是——只是它买的东西比我多得多。

**第二值得看的是 `stage1_outputs` 那一行。** 我把第一段的输出存成文件、
合并完就删，理由当时写的是"那个文件要能被人打开看"——这个理由现在依然成立，
但它的代价在 F17-13 才结账：codex 能让模型引用一次**会话**
（`<rollout_ids>`，不需要模型知道任何行号），而我只能让它引用行号，
于是引用信号 0/9 解析成功。**一个当时看起来只关乎可读性的选择，
两个 fault 之后变成了遗忘机制的地基问题。**

---

## §17 回头看：这一章撞到了什么

清单 11 条：

| ID | 结果 |
|---|---|
| F17-01 | **复现，量出来是 15.4 秒**（四条会话：2.8+2.4+2.6+2.6 抽取 + 5.0 合并）。挪到下一次会话开头，和第一个请求并行；代价是"记忆永远晚一次会话"，写在 §1.1 的两次运行里 |
| F17-02 | 未观测到（额度一直是 99.87%），但机制照做。真正的产出是把第 12 章只在 429 时才解析的那些头，改成**成功响应上就读**——以及"不知道 ≠ 没有"这条明写的选择 |
| F17-03 | **强复现**：三个进程六条待办，朴素的 select-then-update 在第一次写这一章的 Windows 机器上 **4/4 次运行都是 4 个重复**；改写时在 Linux 上重跑，**6 次里 3 次**有重复（§4.6）。病因不在我写的那几行里，在 Python `sqlite3` 那个"SELECT 不开事务"的默认值里 |
| F17-04 | **药方被推翻**。清单说的"流水账"一次没出现；出现的是**配额被填满**——那句 "at most three bullets per section" 让模型在一条什么都没教的会话上产出 3/3 条垃圾。而修法与第 16 章 F16-07 **方向相反**：那里要把判断改成格式，这里要把格式改回判断。只修好了"每次会话"这一级，"每条"一级是欠债 |
| F17-05 | **两条臂，结论相反**：顺手读到的 key，抽取器 0/3 不抄；用户**要求记住**的 key，3/3 抄了（有一次两遍）。只测第一条臂会发单侧脱敏。计数还多报了一次（一个 secret 两个正则） |
| F17-06 | 复现（机制层面）。上限 + 未使用窗口 + 剪枝都做了，而窗口这条**单独用就是 F17-07** |
| F17-07 | **清单是对的，而且是它自己那条药方的反例**：按年龄先删的正是"九个月大、每周在用"那条。引用数第一，且零引用**永远不足以单独删** ——因为第 16 章量出这个信号系统性漏报"改变了动作"的条目 |
| F17-08 | **复现了一半，而且是清单没写的那一半**。用户**加**的内容两条臂都保住（diff 无用）；用户**删**的内容，带 diff 0/3 恢复、不带 diff 3/3 恢复。整文件重写丢内容（F04-02 的形状）在 12 节规模上 **12/12 未复现** |
| F17-09 | 做了，没复现——因为模型压根没有编辑记忆的路。真正被测出来的是这个工具**什么时候会被用**：显式说"请记住" 3/3，同一条约定不说那句话 0/3 |
| F17-10 | **在这个设计里不成立**：写路径不是 Agent，没有会话可标记。我写的 `ephemeral` 字段找不到调用者，删了。而它的**形状出现了两次**，都在清单外：子 Agent 的会话进了队列（四个问题七个文件），以及抽取器读到了同一次会话里 `remember_this` 的调用（3/3 重新推导出那条 note） |
| F17-11 | 做了：两个开关、`--jobs`、`--forget-all` 扩到 `raw/` + `notes/` + 数据库、git 历史**说明但不删** |
| F17-12 | **第 16 章重写逼出来的**：记忆搬去 `~/.minicodex/memories` 之后，锁和任务库必须跟着搬——**一把留在仓库里的锁守着一份全局共享的记忆，等于每个 checkout 一把锁、互相之间毫无互斥**。结构上的分家（任务库是记忆目录的兄弟而不在它里面）本来就是对的，没动 |
| F17-13 | **第 16 章重测逼出来的**：`prune()` 排序依赖的引用信号，被量出 0/9 解析成功。**诱人的修法被拒绝**——换成行为信号就是又一次自己发明机制，而 codex 明确把两个信号分开，因为行为信号只能说出"哪个文件"，遗忘的每个决定却是关于"哪个条目"的。所以信号不换，换的是它的权重：`prune()` 拒绝"零引用就删"从保险带变成了承重件。**活下来了，不是修好了** |

清单外 9 条：

| 故障 | 发现 | 修法 |
|---|---|---|
| **子 Agent 的会话进了记忆队列**：四个问题留下七个文件，两个子会话把父会话的结论各重复一遍，而且里面没有用户 | 🔵 | 按 `parent` 排除。第 7 章 `resolve("last")` 为同样的原因做过同样的排除——同一个目录，第二个读者 |
| **抽取器读到自己的输出**：一次 `remember_this` 调用同时经 `notes/` 和 transcript 两条路到达第二段，3/3 被重新推导成一条 bullet | 🟡 | 在 `transcript_of` 里跳过这个工具的调用与返回 |
| **写入方和读取方各有一把尺子**：写入按自己的尺子裁剪摘要，读取按同一个数字算摘要+索引+围栏，于是自己写的记忆被自己截断 | ⚪ | 变异测试找到（那条变异一开始活着，因为测试测的是函数不是写入）。改成写入方去问 `overflows()`。第 6 章 F06-12 第二次。（原文这里还写了"那个截断标记还是挂不挂检索工具的开关"——那是第 16 章自己发明的门控，已经在 F16-12 里退休了，`overflows()` 现在只表示它字面上的意思） |
| **速率限制头从来没被读过**：测试测的是我自己调的解析函数，`self.rate_limit = None` 全绿 | ⚪ | `httpx.MockTransport` 驱动真正的 `stream()`。第 6 章那条"测试重新实现被测代码"第三次 |
| **`usage.json` 里留下不存在条目的行**：条目也可以因为"合并不再输出它"而消失，那条路径不经过 `pruned.dropped` | ⚪ | 从活着的条目重建，而不是从旧表里减 |
| **脱敏计数数的是正则不是 secret**：一个 key 报 2 次 | 🟠 | 已被替换成 `<redacted>` 的值不再计数。这次出在**发布代码**里，而那个数字是要打给用户看的 |
| **模型把 prompt 里的占位符 `- ...` 抄进了答案** | 🟢 | 加进"none/n/a/nothing"那张过滤表。一条正文是省略号的记忆能活一年，因为没人看得出它本来想说什么 |
| **`git` 让我自己的清理代码崩了**：`.git/objects` 是 0444，Windows 认真执行 | 🔴 | `shutil.rmtree(onexc=...)` 里 chmod。引入新依赖，第一个被绊倒的不是主流程 |
| **我的两个测量指标各测了别的东西**：流水账分类器只命中 60 条里的 2 条（真垃圾语法上和真信号一样），"无解析"把每一次正确的空答案都算成解析失败 | 🟠 | 一个标注为"它测的不是这个"并保留，一个加上 `DURABLE: no` 的判断 |

改写这一章时又找到的 5 条（加上一次自伤）：

| 故障 | 发现 | 修法 |
|---|---|---|
| **第二段的锁只包住了"写"**：两个进程各自读旧记忆、各花一次模型调用合并、先后写盘，后写的覆盖了先写的；被覆盖的那次会话的 `raw/` 文件已被删，什么都没剩，两边都报告成功 | 🟢 两个假模型摆出这个顺序 | 第二段从第一次读到最后一次删全程持锁；拿不到锁的进程把 `raw/` 留给持锁者（§8.7） |
| **合并时服务商一报错，用户的答案后面跟一段 traceback，退出码 1**：`_finish_writer` 只列了四种异常，写入管线的 provider 错误不是 `ModelFailed`；而在宽限期里失败的走的是另一条路，连那四种都接不到 | 🟣 读 `except` 名单 | 两处都接 `Exception`（§3.6） |
| **被直接杀掉的会话永远不会变成记忆，而且任何地方都不说** | 🟣 读 `pending_sessions` 对照第 7 章的锁 | `locked_sessions()`，`--jobs` 列出 pid（§4.7） |
| **`--jobs` 把已处理完的会话也算成"eligible"** | 🟠 读输出 | 只数不在表里的（§12.4） |
| **这个竞态在 Linux 上时有时无**：原文写"确定性，4/4" | 🟢 重跑六次 | 照实改写（§4.6） |
| **改写时自己把一个变异打进了后面七个快照**：生成补丁时变异检查正在跑 | 🔴 后面快照的测试红了 11 个 | 还原；变异检查跑着时不从同一目录复制任何东西（§14.4） |

第一次写这一章时的发现方式分布（20 条）：

| 方式 | 条数 |
|---|---|
| ⚪ 静态 / 变异测试 | 6 |
| 🟡 静默错误 | 5 |
| 🟠 可观测性 | 4 |
| 🟢 主动边界测试 | 3 |
| 🔵 真机长跑 / 多轮 | 1 |
| 🔴 崩溃 | 1 |

**20 条里有 3 条出在这一章自己的验证工具上**（两个指标 + 一个探针清理）。
第 6 章五条、第 14 章三条、第 16 章三条、第 17 章三条——
这已经可以当成一条定律写进方法论了：

> **每写一个测量工具，就要预算它自己会有一到两个 bug，
> 而且它们的形状永远是"报告一个关于别的东西的数字"。**

---

## 如果你只记住三件事

1. **一个必须填的格式会被填满，而且是被"看起来很像真的东西"填满。**
   第 17 章要模型经常什么都不写，"每节最多三条"这个配额让它在一条什么都没教
   的会话上产出 3/3 条垃圾。第 16 章无条件要引用块，拿到了 9/9 个格式完美的
   引用块，**其中 0 个指向真实存在的行**。
   两边都不报错，因为两边填进去的东西都是合理的。
   所以：**如果一个槽位可以留空，就不要只指望模型留空**——
   让一个判决先于格式出现（`DURABLE: yes|no`），并且**用代码执行它**。

2. **"这个概念出现了三次"和"这段代码出现了三次"是两件事。**
   清单说单写者第三次出现、该抽出来命名了。数一遍：概念三次，代码两次
   （第 8 章那次是完全不同的机制）。于是抄了十二行，留一句注释指向第一处。
   同一把尺子在这一章用了三次：删掉没有调用者的 `ephemeral` 字段、
   不为六个常量新开 `memory_format.py`、不抽 `locks.py`。
   **照抄清单就是不做判断。**

3. **一个被删掉的东西不在文件里——所以 diff 是它存在过的唯一证据。**
   这是本章最反直觉的一条：把记忆目录做成 git 仓库，买到的**不是**"防止合并吞掉用户的修改"
   （用户加的东西两条臂都保住了，因为改过的文件就摆在模型面前），
   而是"让用户的**删除**有证据"（带 diff 0/3 恢复，不带 diff 3/3 恢复）。
   **量完之后要多问一句：我量到的这件事，真的是我这个设计的理由吗？**
   ——第 16 章那条"如果你只记住三件事"的第 1 条，这一章又用上了一次。

---

## 动手练习

前三个要你自己写代码，先别看快照。

1. **给第一段加一个"每条"级别的过滤。** §5.7 说判决门只管住了"每次会话"，没管住"每条"：`The calc.py file defines two functions` 这种条目还是会进来。在 `parse_stage1` 之后加一个函数 `drop_obvious(stage1, root)`：一条 bullet 里提到的每个路径（借第 16 章的 `_PATHISH`）都在仓库里存在、而且这条 bullet 里没有"always / never / must / don't"这类词，就丢掉它。先写测试：那条 `calc.py` 的会丢、`Always use python -m pytest` 的不丢。然后想一想：你的规则会误伤哪种有用的条目？

2. **把锁的主人带进报告。** §15.4 第 1 问：`run_pipeline` 拿不到锁时，报告里只说 "another minicodex"，丢掉了 `_lock` 错误消息里的 pid。改成 `deferred=f"pid {pid}"`，并且让 `minicodex memory --jobs` 在锁文件存在时也打印它（照 §4.7 的样子）。先写测试。

3. **让 `pending_sessions` 不再读已经处理过的会话。** §15.4 第 3 问。给它加一个参数 `known: set[str]`（任务表里已有的会话 id），从文件名里取出会话 id，在表里的直接跳过、不读文件。提示：第 7 章的会话文件名就是会话 id 加 `.jsonl`。改完之后，`run_pipeline` 里怎么拿到 `known`？

4. **量一下名字匹配的误伤。** §6.5 说 `keyfunc=lambda` 会被当成密钥换掉。造一段典型的 Python 代码（几个 `sorted(key=...)`、`tokenizer = ...`、`max_tokens = 4096`），喂给 `scrub`，数一数误伤几处。然后改 `_SECRET_ASSIGNMENT`，让它只认"全大写的名字"（`OPENAI_API_KEY=...` 的样子），重跑 §6 的测试——哪一个会红？那一个红的说明了什么？

5. **（有 key 的话）重跑 §13 的 A/B**：`uv run python probe_memory_write.py ab --samples 3`。第 16 章修过行号之后（第 16 章 §5.3），模型写出的引用有可能真的被认出来了——`usage.json` 里的引用数还是 0 吗？如果不是，§10.2.1 的结论要怎么改？

下一章换一个方向：不是"记住发生过什么"，而是"有一份写好的操作手册，需要的时候去翻"——技能。

---

# 附录 · 对照检查

## T1 · 每个文件是在哪一节写出来的

`src/minicodex/memory_write.py`，从上到下：

| 内容 | 小节 |
|---|---|
| 模块说明、导入、`RAW_DIR` 到 `MIN_RATE_LIMIT_REMAINING_PERCENT`、`MemoryWriteError` | §2.5 |
| `_SECRET_SHAPES`、`_SECRET_ASSIGNMENT`、`REDACTED`、`scrub` | §6.5 |
| `Stage1`、`_SECTIONS`、三份提示词、`_VERDICT`、`_BULLET`、`_H2`、`parse_stage1` | §5.9 |
| `render_stage1`、`transcript_of`、`extract`、`_ask` | §5.9 |
| `NOTE_NAME` 到 `write_raw` | §8.5 |
| `SUMMARY_MARK`、`BODY_MARK`、`STAGE2_SYSTEM`、`Merged`、`parse_merged`、`merge_request`、`consolidate` | §8.6 |
| `Prune`、`prune`、`trim_summary` | §10.5 |
| `_lock`、`_unlock`、`write_memory` | §8.6 |
| `_usage`、`_record_first_seen` | §10.5 |
| `_git` 到 `write_gitignore` | §9.5 |
| `WriteReport`、`run_pipeline` | §13.4（第二段：§8.7） |
| `__all__` | 照着上面所有公开的名字列一遍 |

`src/minicodex/memory_jobs.py`：

| 内容 | 小节 |
|---|---|
| 模块说明、常量、`_SCHEMA`、`Job` | §4.5 |
| `JobStore` 全部方法 | §4.5 |
| `pending_sessions` | §4.5 |
| `locked_sessions` | §4.7 |

改动的旧文件：

| 文件 | 改了什么 | 小节 |
|---|---|---|
| `memory.py` | `MINICODEX_HOME` | §2.5 |
| `model.py` | `RateLimit`、`self.rate_limit` | §7.5 |
| `composition.py` | `remember` 参数、笔记工具 | §8.5 |
| `__main__.py` | `_instructions` 里笔记工具那段 | §8.5 |
| `__main__.py` | `_ask`：后台写入、两处 `_finish_writer` | §3.5 |
| `__main__.py` | `WRITER_GRACE_SECONDS`、`_finish_writer` | §3.5、§3.6 |
| `__main__.py` | 参数、`_memory`、`_memory_jobs`、`_remember_now` | §12.4 |

## T2 · 常见报错对照表

| 你看到的 | 多半是因为 | 去哪看 |
|---|---|---|
| `sqlite3.OperationalError: cannot commit - no transaction is active` | `claim` 里的 `BEGIN IMMEDIATE` 不见了——或者你复制了一份正在被变异检查改着的文件 | §4.3、§14.4 |
| 同一条会话被抽取了两次，`raw/` 里两份一样的 | 认领是"先查再改"，没有包在 `BEGIN IMMEDIATE` 里 | §4.2 |
| `[memory writer: nothing to do]`，可你明明跑过好几次 | 记忆永远晚一次会话；或者那几次会话还开着（旁边有 `.lock`） | §3.2、§4.7 |
| `minicodex memory --jobs` 里有 `locked, skipped: ...` | 那次会话的进程被直接杀掉了，锁没删 | §4.7 |
| `[memory writer: ... merge left to another minicodex ...]` | 另一个进程正在合并；或者一个被杀掉的进程留下了 `memory_merge.lock` | §8.7、§15.4 |
| 答案打印出来之后跟着一段 traceback | `_finish_writer` 只接住了一部分异常 | §3.6 |
| `MemoryWriteError: merge answer is missing its markers` | 第二段的模型没按格式回答；`raw/` 文件还在，下次再合 | §8.6 |
| `MemoryWriteError: the merge produced neither a summary nor a section` | 合并回答切不出任何内容；旧记忆没被覆盖 | §8.6 |
| 记忆里有一条 `- ...` | `parse_stage1` 的过滤表里少了 `...` | §5.9 |
| 自己写进去的记忆，读出来带着 `[... memory truncated` | `trim_summary` 用的不是第 16 章的 `overflows()` | §10.5 |
| `PermissionError` 删不掉 `.git/objects/...`（Windows） | git 把对象文件设成只读 | §9.4 |
| 测试显示 `SKIPPED ... git is not on PATH` | 这台机器上没装 git | §9.5 |
