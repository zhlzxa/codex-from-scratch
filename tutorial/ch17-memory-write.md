# 第 17 章 · 记忆（二）：让它自己写，并且学会忘

> **代码**：`steps/step17_memory_write/`
> **分支**：`feat/memory-write`
> **产出**：`memory_write.py`（抽取、合并、脱敏、遗忘、单写者）、`memory_jobs.py`
> （认领/租约/退避，本书第一个数据库）、`--remember` 开关、
> `minicodex memory --jobs / --remember-now`
> **你需要**：本章 56 个测试全部离线。`probe_memory_write.py` 九节里三节离线，
> 六节要真调 API（openai，几美分）
>
> **阅读顺序在第 15 章之前，编号在其之后。** 理由在第 16 章 §1.3。

---

## §1 这一章要做出来的东西

### 1.1 先看见它动

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

跑四次，四次一模一样。**三个进程认领了同一批两条，六次认领里四次是重复的。**

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

第 16 章的 F16-09 有一条很硬的结论：**需要模型主动选择进入的阶段，模型就是不进**
（36 次运行，`memory_search` 被调 3 次）。那这个 `remember_this` 呢？

同一条约定，两种说法，各三次：

```
"…and nobody commits without it. Please remember that for future sessions."   3/3 写了 note
"…and nobody commits without it."                                             0/3 写了 note
```

结论很干净：

> **`remember_this` 是"请记住"这句话的通道，不是"注意到约定"的机制。**

而"顺口说的约定"由抽取管线负责——`docstrings` / `runner` / `layout` 三条会话
证明它确实抓得到。两条路互补，谁也不能替代谁。

这也解释了为什么这个工具留下来了，而第 16 章的 `memory_search` 被收进了
"只在装不下时才挂"：**这个工具有 3/3 的调用记录，那个有 3/36。**

---

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
  20260816T002539-39752        done     attempt 1
  20260816T002611-31188        done     attempt 1
  20260816T003149-15548        pending  attempt 0

  done: 2, pending: 1
  3 session file(s) in .minicodex\sessions are eligible
```

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

## §14 清点：这一章写了多少代码

| 文件 | 行数 | 完整代码在哪一节 |
|---|---|---|
| `src/minicodex/memory_write.py` | 1395 | §2.3、§5.5、§6.2、§8.2、§9.1、§10.2、§11.2 |
| `src/minicodex/memory_jobs.py` | 312 | §4.1、§4.3、§4.4、§12.3 |
| `tests/test_faults_ch17.py` | 1159 | 摘录散在各节；56 个测试全部离线 |
| `probe_memory_write.py` | 860 | §4.2、§5.8、§6.2、§9.2 |
| `probe_mutations_ch17.py` | 323 | §15.5 |
| `src/minicodex/model.py` | +55 | §7.1、§7.2 |
| `src/minicodex/__main__.py` | +262 / -5 | §3.2、§3.3、§12 |
| `src/minicodex/composition.py` | +10 | §8.2 |
| `src/minicodex/rollout.py` | ±8 | §11.1（一个字段的诞生与删除） |

**没有进正文的部分**，以及为什么：

- `Stage1` / `Merged` / `Prune` / `WriteReport` 四个 dataclass 的完整定义——
  字段都在各节里被逐个解释过了。
- `merge_request()` 的拼接顺序（手改 diff 放最后并且带标签）——
  一句 docstring 说完的事：*"the input with the highest authority and the one
  most easily read as more of the same"*。
- `_git()` 的 subprocess 封装——三行，没有判断。
- `JobStore.rows()` / `counts()` / `state_of()`——纯读，给 `--jobs` 用。

---

## §15 收工：commit、PR、review

### 15.1 commit 序列

七个：

```
1  feat(memory): a claim table for sessions waiting to become memory

   The first database in this program, and the argument is on three axes
   rather than on "it got complicated": every row is updated, rows are
   contended between processes, and losing the contention has to be atomic.
   Chapter 7's append-only JSONL is the opposite choice on all three.

   `isolation_level=None` plus an explicit BEGIN IMMEDIATE, because the
   obvious select-then-update version is not a claim: three processes, six
   jobs, 4 duplicates, 4 runs out of 4, no error anywhere. Python opens a
   transaction on the first DML statement, and a SELECT is not one.

2  feat(memory): stage 1, one session in, at most a few statements out

   Two model calls, no tools, no agent: there is nothing to iterate and a
   tool loop would hand a model somewhere to write, which is the one thing
   the next commit exists to prevent.

   The shipped prompt asks for a verdict -- `DURABLE: yes|no` -- before it
   shows a shape that could be filled, and `parse_stage1` enforces the
   verdict rather than trusting it. The careful version without that (kept
   as STAGE1_QUOTA) produced three bullets for every session including one
   that taught nothing, 3/3. "At most three bullets per section" is a quota.

   Chapter 16 met the same mechanism from the other side: under codex's real
   citation format, an unconditional ask produces a block 9/9 times and 0 of
   those 9 resolve to a real entry. A required shape is filled whether or not
   there is anything to put in it -- and it is filled with plausible material,
   which is why neither failure announces itself.

3  feat(memory): stage 2, and the only function that writes MEMORY.md

   Everything else produces a proposal. This validates before it opens
   anything (F04-11), refuses an empty result rather than writing it
   (F06-08), holds an O_EXCL lock while it writes (F07-08), and adds the
   `v1` line itself so that a model can never get it wrong.

   The merge lock is chapter 7's lock, copied. Second occurrence, not third:
   the rule of three says copy with a note.

4  feat(memory): redact on both sides of the model

   A key the session happened to read was never repeated by the extractor
   (0/3). A key the *user asked to have remembered* came back 3/3, twice in
   one sample. An inbound-only scrub cannot help with the second: the model
   was asked to write the key down and it did.

5  feat(memory): forget by citation first, age last

   The listed fix for "memory grows forever" is a window of unused days, and
   taken literally that deletes the nine-month-old entry consulted every
   week. Citations first, recency second, age as a tie-break -- and zero
   citations alone never drops anything, because chapter 16 measured the
   signal under-reporting exactly the entries that change behaviour.

6  feat(memory): make the memory directory a git repository

   Not version control for its own sake. An addition a person makes to
   MEMORY.md survives a merge either way, because the edited file is what
   the merge is shown. A *deletion* is not in the file -- that is what
   deleting means -- so the diff is the only evidence it happened: with the
   diff the deletion stuck 3/3, without it the merge put the section back
   3/3.

7  feat(cli): --remember, off by default, and the writer in the background

   Reading what you wrote and letting a model write about you are two
   consents, so two flags. The pipeline runs at the *start* of the next
   session beside the first request, not at the end of this one: the same
   work at the end is 15.4 measured seconds a user waits for something they
   did not ask for.

   `minicodex memory --jobs` answers "why does it not know that yet", and
   --forget-all now deletes raw/, notes/ and the job table -- leaving those
   would mark every source session `done`, so regenerating would produce
   nothing and the feature would look broken.
```

一条**没有**单独成 commit 的改动：`SessionMeta.ephemeral` 的诞生与删除。
它从来没有进过任何一个 commit——写出来、找不到调用者、删掉，
留下一段注释说明它去哪了。**没有调用者的字段不该有历史。**

### 15.2 PR 描述

```markdown
## What

The write path: sessions become memory, and memory forgets.

* `memory_write.py` — stage 1 (extract), stage 2 (merge), prune, redact, the
  single writer, and `remember_this` (the only write access a model has).
* `memory_jobs.py` — a sqlite claim table: claim, lease, backoff, give up.
* `--remember` (off by default), `minicodex memory --jobs / --remember-now`,
  a `--forget-all` that deletes the extracted material too.

## Why

Chapter 16 proved that reading a **hand-written** memory is worth 4/12 → 12/12.
This is the same question for a **generated** one, and the answer is that on
every task the pipeline had material for, it is indistinguishable from the
hand-written fixture.

## How

* The pipeline runs at the start of the *next* session, beside its first
  request. 15.4s of work nobody waits for; the price is that memory is always
  one session out of date, which is visible in the first two runs of §1.1.
* Stage 1 must answer `DURABLE: yes|no` before it is shown a fillable shape,
  and the answer is enforced in code.
* Redaction runs on the transcript and again on the model's answer.
* Forgetting starts from chapter 16's citation counts, not from age.
* The memory directory is a git repository, for one measured reason: a
  deletion a person makes is only visible in a diff.

## Testing

56 new tests, all offline. 38 mutations; two survived the first run and both
were defences with no caller in the tests. Fixing the second turned up a real
fault behind it (see the notes).

## Notes for the reviewer

* **Two listed faults did not reproduce as written.** F17-04's flow-of-work
  diary never appeared; what appeared was a *quota being filled*, which needs
  the opposite fix from the one chapter 16 bought. F17-10 cannot happen here
  at all — the writer is not an agent — and I deleted the field I had written
  for it. Its shape turned up twice elsewhere.
* **A real fault behind a mutation:** the writer trimmed the resident summary
  with its own 400-token ruler while the reader budgets 400 over the summary
  *and* the heading index. A memory this program wrote came back truncated by
  the program that reads it, silently, and the truncation marker is also what
  decides whether the search tools get mounted (F16-09). One ruler now: the
  writer asks `overflows()`.
* Three of the faults in this chapter are in its own probes, again.
```

### 15.3 Code review

我扮演 reviewer，五条：

> **1（正确性）**：`prune()` 在 `write_memory()` 里跑，而 `write_memory()` 是
> 合并的最后一步。也就是说，**模型刚刚辛辛苦苦合并出来的条目，可能立刻被剪掉**，
> 而模型完全不知道这件事。下一次合并它又会把同样的东西写回来，然后又被剪掉。
> 这不是一个循环吗？

是，而且这条意见是对的。现在的缓解是 `MAX_ENTRIES = 40` 远大于任何真实记忆，
所以这个循环在实践中不会启动。但它确实存在，正确的修法是**把剪掉的条目告诉下一次合并**
（"these were dropped and why; do not re-propose them"）。
记为已知边界，写进 step README 的 "deliberately not done"。
**一个不会启动的循环仍然是一个循环，区别只是没人踩到。**

> **2（边界）**：`transcript_of()` 把 system / developer note 全扔了，
> 理由是"那是程序自己的话"。可 `AGENTS.md` 也是 developer note，
> 那是**人写的项目规约**——一个会话里最像"稳定偏好"的东西，被你扔了。

说得对，而且这是一个真正的取舍。理由是：`AGENTS.md` 的内容**已经在版本库里了**，
把它抽进记忆等于把同一句话存两份，而且两份会漂移。
第 16 章那张三层表说得很清楚：`AGENTS.md` 是第一层，便宜两个数量级，
记忆不该去复制它。这条补进 `transcript_of` 的 docstring——
**因为"我们扔了它"和"我们扔了它，因为它已经在别处"是两回事。**

> **3（可测试性）**：`race` 那个探针用 `subprocess` 起三个进程，
> 而三个进程能不能"同时"到达 `claim()` 是靠运气的。这个测量为什么是可信的？

因为它不依赖时序。三个进程都在启动后立刻 `claim()`，
朴素版**四次运行都是 4 个重复**——如果它靠运气，应该看到 0～4 之间的分布。
看到的是常数。原因也清楚：那个 select 完全不加锁，任何两个进程只要在
同一个毫秒量级内到达就会读到同一批行，而进程启动的抖动比这大得多。
第 8 章 F08-09 是同一个形状的反例（"只在慢机器上复现"实测是反的）：
**竞态窗口大到某个程度，它就不是竞态了，是必然。**

> **4（命名）**：`scrub()` 返回 `(text, count)`，调用点几乎都不用那个 count。
> 一个返回二元组的函数逼着每个调用点写 `text, _ = scrub(...)`。

有三个调用点用了它（`transcript_of`、`extract`、`parse_merged`），
它们的值最后汇进 `WriteReport.secrets_removed`，而那个数字要打印给用户。
一个静默的脱敏是一个没人审计的脱敏——这正是它不能是 `str -> str` 的理由。
`_` 出现在两个地方（测试和探针），我认为可以接受。

> **5（风格）**：`memory_write.py` 1395 行，比第 16 章那个 914 行的
> `memory.py` 还长。第 16 章 review 第 5 条说"该拆的时刻是第 17 章"，
> 现在到了，为什么还不拆？

因为第 16 章那条预测**说错了拆的位置**。当时的判断是"读和写共享文件格式，
所以该有一个 `memory_format.py`"。真写完之后去数：
`memory_write.py` 从 `memory.py` 导入的是 `BODY_FILE`、`SUMMARY_FILE`、
`FORMAT_VERSION`、`USAGE_FILE`、`SUMMARY_TOKEN_BUDGET`、`Entry`、`Memory`、
`parse_entries`、`overflows`——**九个名字，其中六个是常量**。

为了共享六个常量和一个解析函数新开一个模块，会让 `memory.py` 和
`memory_write.py` 都去 import 第三个文件，而那个文件里没有行为。
FB-03 的原话：**区分"必要的倒置"与"仪式性抽象"**。

真正该拆的缝在别的地方，而且它现在还看不清楚：如果以后 `prune` 长成一个
带策略的东西（按项目、按标签、按用户显式 pin），那时 `forgetting.py` 是一条真缝。
现在拆是猜。

**一个被写进上一章 review 的预测，到期时要去核对，而不是执行。**

### 15.4 Merge 与 CI

squash 进 `main`。

`ci.yml` **一行没改**——第 -1 章那个"阻塞 CI 最多六步"的上限第五次起作用。
`postmerge.yml` 加一步，而且是被那条元测试逼着加的：

```
FAILED tests/test_packaging.py::test_F_1_05_every_mutation_script_runs_somewhere
E       AssertionError: mutation scripts nothing runs: ['probe_mutations_ch17.py']
```

第 12 章加的那条元测试，第二次当场生效。**"散文里的承诺不是机制"这条教训，
被机制化之后就是这个样子：我没有机会忘。**

### 15.5 变异测试

```
$ uv run python probe_mutations_ch17.py
38 mutations, tests/test_faults_ch17.py tests/test_faults_ch16.py
              tests/test_faults_ch07.py tests/test_schemas.py

   10 test(s) fail  <-  the claim is a select followed by an update, outside any transaction
    1 test(s) fail  <-  an expired lease is never taken back, so a killed run loses the session
    3 test(s) fail  <-  the attempt is counted on failure instead of on claim
    1 test(s) fail  <-  a failing job comes back forever instead of giving up
    1 test(s) fail  <-  failure retries immediately, with no backoff
    2 test(s) fail  <-  every session is eligible, including the ones the program had with itself
    1 test(s) fail  <-  a session that is still being written is eligible
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
    1 test(s) fail  <-  forgetting is by age, which deletes the entry that is old and used
   10 test(s) fail  <-  an entry with no citations is dropped whatever its age
    7 test(s) fail  <-  an entry nothing has recorded yet is treated as ancient rather than new
    1 test(s) fail  <-  the cap is not a cap
    1 test(s) fail  <-  the writer trims the summary with its own ruler, not the reader's
    1 test(s) fail  <-  the file is rewritten in ranking order, so every merge reshuffles the diff
    1 test(s) fail  <-  usage rows for entries that no longer exist are kept
    1 test(s) fail  <-  the resident half is left at whatever length the merge produced
   12 test(s) fail  <-  the merge lock is not taken
    1 test(s) fail  <-  a merge that produced nothing replaces the memory with nothing
    2 test(s) fail  <-  a merge answer with no markers is treated as the body
    3 test(s) fail  <-  a note may be written straight into the body file
    1 test(s) fail  <-  one session may propose as many notes as it likes
    2 test(s) fail  <-  a note overwrites the one before it
    1 test(s) fail  <-  the extractor is shown the note the same session proposed
    9 test(s) fail  <-  the extractor is shown the program's own system notes, memory included
    1 test(s) fail  <-  the writer runs whatever is left of the quota
    5 test(s) fail  <-  an unknown headroom is read as no headroom
    3 test(s) fail  <-  the note tool is present whether or not the user asked to be remembered
    1 test(s) fail  <-  the rate-limit headers are never read

every mutation was caught.
```

第一次跑活下来两条，**都是"有防御、没有调用者去证明它"**：

1. **速率限制头从来没被读过。** 我的测试测的是 `RateLimit.from_headers()`，
   而这个函数是我自己调的。把 `model.py` 里那一行改成 `self.rate_limit = None`，
   所有测试照绿。这是第 6 章那条教训（"测试重新实现了被测代码，
   于是删掉守卫它照样绿"）**一字不差的重演**。修法也一样：用
   `httpx.MockTransport` 驱动真正的 `stream()`。

2. **写入侧的摘要上限。** 我测的是 `trim_summary()` 这个函数，
   不是"写出去的东西在不在预算内"。改成后者之后，
   **它立刻抓到了一个真的 bug**：

```
E       AssertionError: the read path had to truncate what we wrote
```

写入方按自己的尺子裁剪摘要；而读取方的同一个预算是算在
**摘要 + 小节索引 + 围栏**上的。于是这个程序自己写出来的记忆，
被这个程序自己的读路径截断了——一声不吭，而且那个截断标记
**同时还是"要不要挂检索工具"的开关**（第 16 章 F16-09）。

第 6 章 F06-12 那句话第二次出现：

> **问题不是尺子不准，是有两把尺子。**

修法是让写入方去问读取方：

```python
    lines = summary.splitlines()
    while lines and overflows(
        Memory(directory=Path("."), summary="\n".join(lines), entries=tuple(entries)),
        budget=budget,
    ):
        lines.pop()
```

### 15.6 全套

```
$ uv run pytest
1682 passed, 9 skipped in 104.57s (0:01:44)

$ uv run python scripts/check_layers.py
ok  no import cycles
ok  __init__ is a leaf
ok  forbidden edges
ok  one Agent construction site
ok  every module imports alone
```

---

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
| F17-03 | **强复现，而且是确定性的**：三个进程六条待办，朴素的 select-then-update **4/4 次运行都是 4 个重复**。病因不在我写的那几行里，在 Python `sqlite3` 那个"SELECT 不开事务"的默认值里 |
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

发现方式分布（20 条）：

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

1. **把判决门退回去，亲手撞一次那三条垃圾。**
   把 `memory_write.py` 里的 `STAGE1_SYSTEM = ...` 改成 `STAGE1_SYSTEM = STAGE1_QUOTA`，
   然后跑 `uv run python probe_memory_write.py signal --arms shipped --only <那条 nothing 会话>`。
   看着它在一条只问"calc.py 有哪些函数"的会话上产出三条记忆。
   然后把 `parse_stage1` 里那三行 `DURABLE` 判断注释掉，再跑一次：
   **模型说 no 之后还写了几条？**（这个数字我没量，留给你。）

2. **给"每条"一级加一个过滤，然后证明它有没有用。**
   §5.7 说判决门只解决了"每次会话"这一级。写一个第二遍调用：
   把每条 bullet 单独送回模型，问它"这条能不能通过三十秒测试，yes/no"。
   用 §5.2 那四条会话量：**它砍掉了几条真信号？** 多花了几次调用？
   如果它砍掉了 `runner` 那条，你就亲手撞到了"精度换召回"的代价。

3. **让遗忘循环起来。** §15.3 review 第 1 条说的那个循环：
   把 `MAX_ENTRIES` 改成 3，然后连续跑三次 `minicodex memory --remember-now`
   （每次之间造一条新会话）。观察 `git -C ~/.minicodex/memories log -p`：
   **同一条记忆是不是被写进去、剪掉、又写回来？**
   然后实现 review 里说的修法（把剪掉的条目和原因告诉下一次合并），再跑一次。

4. **（难）测一次"记忆的记忆"。** §11.3 说抽取器不看 system note，
   否则它会把注入的记忆重新学一遍。把那个 `isinstance` 判断改成
   `(UserMessage, SystemNote)`，然后做这件事：开着 `--memory --remember` 连跑五次，
   每次问一个无关问题。观察 `MEMORY.md` **每一代都长了什么**。
   这是本书唯一一个你能亲眼看到"信息在系统里自我放大"的地方。

5. **（难）把第二段换成一个子 Agent。** codex 的 Phase 2 是一个受限子 Agent。
   用第 10 章的 `run_task` 实现它：给它一个只能写 `notes/` 的工具集、
   `AskForApproval::Never`、深度上限 0。然后回答两个问题：
   **它多花了几次模型调用？** 以及——更重要的——**它的会话文件出现在
   `sessions/` 里之后，下一次启动会不会把它捞起来抽取？**
   如果会，你就复现了 F17-10 本来的样子，而 `ephemeral` 这个字段
   在你的代码里就有调用者了。
---

# 附录 · 代码到底怎么敲出来的（新手向，逐段精讲）

跟第 16 章附录同一个规矩：正文讲"为什么"，这里补"怎么写"。默认你已经读过
前面几章的附录（`dataclasses`、`asyncio`、`sqlite3` 基础、第 16 章的
`Entry`/`Memory`），这里只讲这一章新出现的、容易让新手卡住的写法。代码摘自
`steps/step17_memory_write/src/minicodex/`（`memory_write.py` 和
`memory_jobs.py`），逐段核对过。

先把范围说死：

1. 本附录只解释第 17 章在 `steps/step17_memory_write/` 里新增或修改的代码。
   第 16 章已经讲过的读侧（`memory.py`）不重复，但本章调用它时会写清参数
   形状和返回边界。
2. 完整函数代码块里不使用代表遗漏实现的省略号，也不把关键分支写成
   "此处略"。示例字符串或命令文本里出现的三个点，如果本来就是源码的
   一部分，会原样保留。
3. 代码块按当前源码核对。正文已经给了全文的（`render_stage1`、
   `ensure_repo` 的 docstring、`scrub` 的片段、`write_memory` 的 docstring），
   本附录不再整段重复，只补正文明确说"没进正文"的（§14 自述：四个 dataclass
   完整定义、`merge_request` 拼接顺序、`_git` 封装、`JobStore.rows()/
   counts()/state_of()`），以及正文只有片段的核心函数（`parse_stage1`、
   `transcript_of`、`extract`、`prune`、`run_pipeline`、`JobStore.claim`）。

先画一张图，把写侧分成五块：

~~~text
写侧（memory_write.py + memory_jobs.py）
    │
    ├─ 脱敏      scrub / _SECRET_SHAPES / _SECRET_ASSIGNMENT（正文 §6）
    ├─ 阶段一    提取：transcript_of → extract → parse_stage1 → write_raw（H1）
    ├─ 出口箱    note_toolset / propose / pending_notes（H2）
    ├─ 阶段二    合并：merge_request → consolidate → parse_merged（H3）
    │              └─ write_memory（校验+剪枝+写盘+锁）→ prune / trim_summary（H4）
    ├─ git       _git / git_available / ensure_repo / hand_edits / commit（H5）
    └─ 任务表    memory_jobs.py：JobStore（enrol/claim/finish/fail）→ run_pipeline（H6）
~~~

## H1 · 阶段一：从一次会话到几条 durable 语句

### H1.1 `transcript_of`：把 rollout 变成给抽取器的文本

正文只提了"system note 被排除"的设计，完整函数：

```python
def transcript_of(rollout: Rollout, *, budget: int = TRANSCRIPT_TOKEN_BUDGET) -> tuple[str, int]:
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
                continue
            parts.append(f"{item.name} returned: {clip(item.content, TOOL_RESULT_CHARS)}")
    text = "\n".join(parts)
    text, secrets = scrub(text)
    if estimate_messages([{"role": "user", "content": text}]) > budget:
        text = clip(text, budget * 4)
    return text, secrets
```

四个细节：

1. **`NOTE_NAME` 的调用和结果都被排除。** 一个 `remember_this` 调用**本来**
   就通过 `notes/` 目录进入阶段二（H2），留在 transcript 里会把它再抽一遍
   ——实测 3/3 的会话把 note 重新推导成 bullet（注释里那句 "F17-10 的形状"）。
2. **`ToolResult` 截断到 `TOOL_RESULT_CHARS`（800）。** 一个工具结果可以很
   长，而抽取器只需要知道"命令成功/失败/输出大概什么样"。截断在转录这一层，
   不给抽取器一个巨大的输入。
3. **两次脱敏，第一次在这里。** 进入抽取器之前先 `scrub`（F17-05 的入口侧）。
4. **超预算时 `clip(text, budget * 4)`。** 头尾各留一半（第 2 章的形状）——
   用户的原始请求在会话顶部、最后成功的东西在底部，tail-only 剪法每次都
   丢掉第一个。

### H1.2 `extract`：一次模型调用，没有 Agent

```python
async def extract(
    model: Any,
    rollout: Rollout,
    *,
    system: str = STAGE1_SYSTEM,
) -> Stage1:
    text, secrets = transcript_of(rollout)
    answer = await _ask(
        model,
        [
            {"role": "system", "content": system},
            {"role": "user", "content": f"<transcript>\n{text}\n</transcript>"},
        ],
    )
    answer, more = scrub(answer)
    stage1 = parse_stage1(answer, session_id=rollout.meta.session_id)
    return Stage1(
        session_id=stage1.session_id,
        preferences=stage1.preferences,
        knowledge=stage1.knowledge,
        failures=stage1.failures,
        secrets_removed=secrets + more,
    )
```

- **不是 `Agent`，注释明说为什么**：输入是已经存在的 transcript，输出是
  文本，没有要迭代的东西。套一个工具循环等于把第 0～12 章所有失败模式
  请回来，换不来任何东西，还会给模型一个"能写"的地方（F17-09）。
- **第二次脱敏在这里**（出口侧）：抽取器的回答要落盘、要注入未来每个
  请求。正文 §6.2 测过只脱敏入口侧的版本：模型能从上下文里重建被部分
  遮盖的值。
- **`secrets_removed = secrets + more`**：入口侧的数量 + 出口侧的数量，
  一并记进报告。

### H1.3 `parse_stage1`：宽容解析，严格筛选

```python
def parse_stage1(text: str, *, session_id: str = "") -> Stage1:
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

三个新手容易漏的点：

1. **`DURABLE: no` 一票否决。** 模型被要求在**看到可填的形状之前**先做
   一个判断（`DURABLE: yes/no`），这一行让那个判断有约束力：说了 no，
   标题下的 bullets 只是"形状在说话"。`_VERDICT` 正则
   `^\W*durable\W*:\W*(yes|no)\b` 宽容 `**DURABLE: no**` 这种加粗写法，
   忽略大小写。
2. **`wanted` 把标题小写化做映射**，`_H2.match` 命中的标题经
   `.strip().lower()` 查表——模型写的 `## Preference signals` 和
   `## preference signals` 落到同一个 key。**不认识的标题 `current = None`，
   其下的内容全部丢弃**——宽容方向是"格式花了"，严格方向是"什么都不
   乱收"。
3. **`"- none"`、`"- ..."`、`"- n/a"` 这些空条目被过滤。** 模型被要求
   留空时会写 `- none` 而不是什么都不写；`...` 是 prompt 里 `_SHAPE` 的
   占位符被照抄。这两者都会让记忆永远出现一个说"none"的小节，所以
   `body.lower().strip(" .") in {...}` 全部跳过。

### H1.4 `_ask`：非流式收集，[DONE] 检查

```python
async def _ask(model: Any, messages: list[dict[str, Any]]) -> str:
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

- **`finished` 标志 + 结尾检查**：第 0 章 F00-04 的规则第三次应用——一个
  提前断掉的流不是"短一点的回答"，在这里它是"写进磁盘、被读回永久的半条
  记忆"。没有 `[DONE]` 就抛 `MemoryWriteError`，什么都不落盘。
- **延迟 import**（`from minicodex.model import ...` 在函数内）：第 6 章
  `make_summariser` 同一个做法，避免模块顶部循环 import。
- 和第 6 章的 `make_summariser` 长得像但不共享：那个要建 `History`、渲染
  消息，这个只要两条字面量消息、不要历史。两条六行函数看着像不是
  "一个函数的两个调用点"（注释里专门说了）。

## H2 · 出口箱：`remember_this` 工具

正文 §8 讲了设计（append-only outbox），`note_toolset` 的函数体没进正文：

```python
def note_toolset(directory: Path, *, limit: int = 5) -> ToolSet:
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
```

- **`written` 闭包计数器（`nonlocal`）**，每运行限 5 条——一个发现这个工具
  好用的模型可以把一次会话变成五十条提议，而合并阶段要为每一条付费。
- **写之前先 `scrub`**（第三次脱敏，出口箱这一侧也要过一遍）。
- **`propose` 写文件，永不覆盖**：

```python
def propose(directory: Path, note: str, *, now: float | None = None) -> Path:
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
```

文件名 `时间戳-pid-序号.md`：时间戳让同进程内的多次调用不同名，
pid 让两个进程永远不会选同一个名。`if not path.exists()` 是最后一道保险。

## H3 · 阶段二：合并请求的组装与解析

### H3.1 `merge_request`：什么按什么顺序给模型看

正文 §14 明说这个函数"一句 docstring 说完"，完整实现：

```python
def merge_request(
    *,
    memory: Memory,
    raw: Sequence[tuple[Path, str]],
    notes: Sequence[tuple[Path, str]],
    hand_edits: str,
) -> str:
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
```

顺序就是优先级（docstring 那句话："the input with the highest authority and
the one most easily read as more of the same"）：**现状 → 新抽取 → 模型提议 →
人手改的 diff**。人手改的 diff 放最后并带标签"These are the user's own
words. Preserve them."——它是权威最高、也最容易跟新材料混淆成"同类"的输入，
放最后 + 显式标签让它读起来就是"这段不一样"。

### H3.2 `parse_merged`：拒绝，而不是猜

```python
def parse_merged(text: str) -> Merged:
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
```

- **两个标记缺一个就抛错，什么都不写。** 兜底方案（"整个当 body"）会把
  模型的评论写进一个注入每个请求的文件——比失败严重得多。
- **`split(mark, 1)` 只切第一次**：`marker` 前面的内容丢弃（`_`），后面
  从 `BODY_MARK` 再切一次。如果模型把标记写进了正文，`split(..., 1)` 保证
  只认第一个。
- **第三次脱敏**在合并输出的两边都做一遍。

## H4 · 写盘：`write_memory`、`prune`、`trim_summary`

### H4.1 `write_memory`：唯一写 `MEMORY.md` 的函数（完整实现）

正文 §8.2 只给了 docstring。函数体：

```python
def write_memory(
    directory: Path,
    *,
    summary: str,
    body: str,
    now: float | None = None,
    lock_path: Path = MERGE_LOCK,
) -> Prune:
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

    fd = _lock(lock_path)
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
        _unlock(fd, lock_path)
    return kept
```

顺序就是全部设计，四步：

1. **校验在开写之前**（`parse_entries(body)` + 空检查）。"既没有摘要也没有
   小节"的合并结果被拒绝——那是被销毁的记忆，不是更小的记忆（F06-08）。
2. **剪枝在写之前**（`prune`，H4.2），剪完的 `kept.kept` 喂给
   `trim_summary`——摘要按**剪枝后**的条目算预算，因为剪掉的条目不该占
   常驻预算。
3. **锁在写之前**（`_lock`，第 7 章 `O_EXCL` 锁的第二次实现，正文 §2.4
   解释了为什么不抽 `locks.py`）。`try/finally` 保证任何异常都 `_unlock`。
4. **`v1` 版本行由程序加，不由模型加**（§8.3 的测试钉死了
   `FORMAT_VERSION not in STAGE2_SYSTEM`）。两行写入都带 `{FORMAT_VERSION}\n\n`。

### H4.2 `prune`：引用优先，新近次之，年龄最后

```python
def prune(
    entries: Sequence[Entry],
    counts: dict[str, dict[str, Any]],
    *,
    now: float,
    max_entries: int = MAX_ENTRIES,
    unused_days: float = UNUSED_DAYS,
) -> Prune:
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
    order = {entry.entry_id: i for i, entry in enumerate(entries)}
    kept.sort(key=lambda e: order[e.entry_id])
    return Prune(kept=tuple(kept), dropped=tuple(dropped))
```

F17-06/F17-07 的整个论证都在排序元组里：

- **`(-cited, -last_used, -first_seen, index, entry)`**：主键是引用次数
  （第 16 章的回执产生的信号），次键是最近使用时间，第三键是第一次见到
  的时间。三个都是负号，让 `sorted` 从"最好"到"最差"。
- **删除条件有两个，缺一不可**：超过 `MAX_ENTRIES`（40）**或**
  "零引用 + 超过 `UNUSED_DAYS`（30）天"。零引用本身**从不**删——回执有
  已知偏差（模型报"影响回答的"，不报"影响动作的"，第 16 章 §11.5），
  零引用不是"没用"的证据。
- **`first_seen` 缺失时按 `now` 算**：没有记录的条目当"新"不当"古"——
  反过来的话，第一次在旧记忆上跑这个函数会把所有条目全删了。
- **剪完按文件顺序重排**（`order` dict + `kept.sort(key=...)`）：排名决定
  *什么*活下来，不能让它顺便决定*顺序*——否则每次合并都重排整个文件，
  git diff（F17-08 依赖的机制）就废了。

### H4.3 `trim_summary`：把常驻摘要压回第 16 章的预算

```python
def trim_summary(
    summary: str,
    entries: Sequence[Entry] = (),
    *,
    budget: int = SUMMARY_TOKEN_BUDGET,
) -> str:
    lines = summary.splitlines()
    while lines and overflows(
        Memory(directory=Path("."), summary="\n".join(lines), entries=tuple(entries)),
        budget=budget,
    ):
        lines.pop()
    return "\n".join(lines).rstrip()
```

注释里讲了一个变异测试活下来的教训：第一版只把摘要剪到预算，而读侧
把预算算在"摘要 + 索引 + 围栏"上——剪到恰好的摘要回来还是带截断标记。
**这里问的是读侧自己的问题**（`overflows()` 说 yes 就继续 pop），两把尺子
变成一把。

### H4.4 `_lock` / `_unlock`：第 7 章锁的第二次实现

```python
def _lock(path: Path = MERGE_LOCK) -> int:
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
```

- **`os.O_CREAT | os.O_EXCL`**：创建成功 = 拿到锁；`FileExistsError` = 别人
  拿着。`os.open` 是原子的（第 7 章 `RolloutWriter._acquire_lock` 同一个
  机制）。
- **锁文件里写 pid**，冲突时报"哪个进程拿着"，`from None` 吞掉原始
  `FileExistsError` 的链（用户不需要看"File exists: ..."）。
- **`_unlock` 里 unlink 失败也吞**——锁可能被外部删了，不致命。

## H5 · git：`_git` 封装与四个函数

正文 §9 给了 `ensure_repo` 的 docstring，§14 说 `_git` 是"三行，没有判断"。
完整实现：

```python
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


def hand_edits(directory: Path) -> str:
    directory = Path(directory)
    if not (directory / ".git").exists():
        return ""
    result = _git(directory, "diff", "--", SUMMARY_FILE, BODY_FILE)
    if result.returncode != 0:  # pragma: no cover - a broken repository is not fatal
        return ""
    return result.stdout.strip()


def commit(directory: Path, message: str) -> bool:
    directory = Path(directory)
    if not (directory / ".git").exists():
        return False
    _git(directory, "add", "--", SUMMARY_FILE, BODY_FILE)
    result = _git(directory, "commit", "--quiet", "-m", message)
    return result.returncode == 0
```

- **`hand_edits` 就是 `git diff`**：这个程序写文件和提交是同一个动作，
  所以工作区里所有未提交的东西，按定义就不是它写的（正文 §9.1 的原话）。
- **`commit` 只 add 两个文件**——`raw/` 和 `notes/` 是临时目录，进了版本
  库会让 log 不可读（`write_gitignore` 把它们挡在外面）。
- **`ensure_repo` 在 `git init` 后设置本地 `user.email`/`user.name`**
  （CI 镜像里没有全局配置，不设的话第一次 commit 才报错）——注意
  `commit.gpgsign=false`，CI 里签名会失败。

## H6 · 任务表与管线：`memory_jobs.py` + `run_pipeline`

### H6.1 `JobStore`：claim 表的全部方法

正文 §4 讲了 `isolation_level=None`（F17-03 的核心），方法体大部分没进正文：

```python
class JobStore:
    def __init__(self, path: Path = DEFAULT_JOBS_PATH, *, timeout: float = 10.0) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path, timeout=timeout, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute(f"PRAGMA busy_timeout={int(timeout * 1000)}")
        self.db.executescript(_SCHEMA)
```

三个 PRAGMA 各管一件事：`WAL` 让 `--jobs` 的读者和写者共存；
`busy_timeout` 把"database is locked"从异常变成等待（第二个进程晚一毫秒
启动时真正想要的）；`row_factory = sqlite3.Row` 让行可以按列名取
（`row["session_id"]` 而不是 `row[0]`）。

**`enrol`：`INSERT OR IGNORE` 让数据库决定谁先到。**

```python
def enrol(self, sessions: Iterable[tuple[str, Path]], *, now: float | None = None) -> int:
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
```

**`claim`：`BEGIN IMMEDIATE` 是 F17-03 的全部修复。**

```python
def claim(
    self,
    *,
    limit: int = MAX_PER_STARTUP,
    now: float | None = None,
    lease: float = LEASE_SECONDS,
) -> tuple[Job, ...]:
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
```

- **`BEGIN IMMEDIATE` 在 SELECT 之前**：拿写锁，再查再改——`isolation_level
  = None`（自动提交）下裸 SELECT + UPDATE 是两个事务，两个进程会选到同一批
  pending 行，都做一遍（实测 3 进程 6 个任务 4 次重复 claim）。
- **SELECT 的条件是两个**：`pending 且到时间了`（`not_before <= now`，
  失败回退的延迟）**或** `claimed 但租约过期了`（`lease_until <= now`，
  进程被杀后回收）。**不因为"慢"回收**——租约比活长。
- **`attempts` 在 claim 时 +1**（`attempts=attempts+1`），而不是在 fail 时
  ——进程在 claim 和 fail 之间死掉也必须用掉一次尝试，否则一个让抽取器
  崩溃的 rollout 会被下一个进程永远 claim。
- **`except BaseException` 而不是 `except Exception`**：`KeyboardInterrupt`
  也要 ROLLBACK，不能让事务悬着。

**`fail`：带回退地还回去，或放弃。**

```python
def fail(self, session_id: str, detail: str, *, now: float | None = None) -> None:
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
```

- **`detail` 截断到 500 字符**（`detail[:500]`）——一条失败的完整 traceback
  不该塞满数据库行。
- **回退是 `BACKOFF_BASE_SECONDS * 2 ** (attempts - 1)`**（60s、120s、240s）：
  第 12 章测过"对不可能成功的请求立即重试"的代价，这里为 provider 类失败
  做指数回退。
- **`attempts >= MAX_ATTEMPTS`（3）就永久失败**——一个抽三次都不行的
  rollout 不太可能开始行：它通常要么巨大、要么被模型拒绝，都是文件本身的
  属性而不是时刻的属性（常量注释）。

**三个纯读方法**（正文 §14 明说没进正文）：

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
```

`rows` 里 `dict(r)` 把 `sqlite3.Row` 变成普通 dict（`--jobs` 打印用）；
`counts` 用 `GROUP BY state` 一次数完；`state_of` 是 `fetchone` +
`row["state"] if row else None` 的三行模式。

### H6.2 `pending_sessions`：哪些会话"算数"

正文 §4.3 讲了三个排除，函数体：

```python
def pending_sessions(
    directory: Path,
    *,
    exclude: Sequence[str] = (),
) -> list[tuple[str, Path]]:
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

- **`.lock` 文件 = 会话还没结束**（第 7 章 `O_EXCL` 锁的第三次使用：它的
  存在被复用来回答"这个会话结束了吗"——比"写一个 footer 记录"可靠，崩溃
  的进程不写 footer）。
- **`rollout.meta.parent` 排除子 Agent 的会话**（第三个排除，是跑出来的
  不是设计的）：子会话会产出父会话已经说过的内容的第三、四份拷贝，而且
  没有用户。
- **`not rollout.items` 跳过空会话**。

### H6.3 `run_pipeline`：整个写侧的组装

正文 §7 讲了配额让路，完整函数在这里（这是写侧最长的函数，拆五块）：

```python
async def run_pipeline(
    model: Any,
    *,
    directory: Path,
    sessions_dir: Path,
    jobs_path: Path,
    memory: Memory | None = None,
    limit: int = 2,
    exclude: Sequence[str] = (),
    headroom: float | Callable[[], float | None] | None = None,
    now: float | None = None,
    lock_path: Path = MERGE_LOCK,
) -> WriteReport:
    began = time.monotonic()
    stamp = now if now is not None else time.time()
    if callable(headroom):
        headroom = headroom()
    if headroom is not None and headroom < MIN_RATE_LIMIT_REMAINING_PERCENT:
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
                failed.append((job.session_id, f"{type(exc).__name__}: {exc}"))
                store.fail(job.session_id, f"{type(exc).__name__}: {exc}", now=stamp)
                continue
            secrets += stage1.secrets_removed
            read.append(job.session_id)
            if stage1:
                write_raw(directory, stage1)
            else:
                empty.append(job.session_id)
            store.finish(job.session_id, now=stamp)

    raw = pending_raw(directory)
    notes = pending_notes(directory)
    if not raw and not notes:
        return WriteReport(
            sessions=tuple(read),
            empty=tuple(empty),
            secrets_removed=secrets,
            seconds=time.monotonic() - began,
            failed=tuple(failed),
        )

    ensure_repo(directory)
    write_gitignore(directory)
    from minicodex.memory import load as load_memory

    current = memory if memory is not None else load_memory(directory)
    merged = await consolidate(
        model, memory=current, raw=raw, notes=notes, hand_edits=hand_edits(directory)
    )
    pruned = write_memory(
        directory, summary=merged.summary, body=merged.body, now=stamp, lock_path=lock_path
    )
    commit(directory, f"memory: {len(raw)} session(s), {len(notes)} note(s)")
    for path, _ in (*raw, *notes):
        try:
            path.unlink()
        except OSError:  # pragma: no cover
            pass
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

**第一块：配额让路（正文 §7 的核心）。** `headroom` 可以是数字也可以是
函数——创建这个 task 时（`__main__` 里）还没见过任何 rate-limit 头，所以
传"问句"而不是"答案"，在它真正重要的时候才问。`MIN_RATE_LIMIT_REMAINING_
PERCENT`（25%）以下**什么都不做**——后台工作给前台让路，防止用户的任务
拿到记忆写入赚来的 429（F17-02）。

**第二块：claim 循环。** `enrol` 先登记没见过的会话，`claim` 拿最多
`limit`（2）个。**单个会话的 `except Exception` 不中断整个管线**（第 0 章
规则在"没有模型可转交错误"的地方）：失败回队列带退避，三次后停止。

**第三块：空原始材料直接返回。** `raw` 和 `notes` 都空 = 没有可合并的
新材料，不用调合并模型。注意 `empty`（"没产出"的会话）不算失败——那是
成功（"nothing worth keeping" 是正常结局，不写空 raw 文件）。

**第四块：合并 + 写盘 + 提交。** `ensure_repo`/`write_gitignore` 保证
git 可用；`current` 用调用方传的 `memory` 或重新 `load`；`consolidate`
调合并模型；`write_memory` 校验+剪枝+写盘；`commit` 记录。**`raw`/`notes`
的消费在写成功之后**——反过来的顺序会在合并失败时丢掉昂贵的抽取结果。

**第五块：报告。** `WriteReport` 的一次性 `describe()` 就是第 15 章
`_finish_writer` 打印的那一行。

## H7 · 新手常见报错/坑对照表

| 症状 | 原因 | 修法 |
|---|---|---|
| 两个进程都处理了同一个会话 | `claim` 的 SELECT 和 UPDATE 不在一个事务里 | `isolation_level=None` + `BEGIN IMMEDIATE`，在 SELECT 之前拿写锁 |
| 进程被杀后会话永远没人处理 | 只有"done"标记，没有租约 | `claim` 设 `lease_until`，`claim` 的 SELECT 带 `lease_until <= now` 回收 |
| 让抽取器崩溃的会话被永远反复 claim | attempts 在 fail 时才 +1 | `attempts` 在 `claim` 时 +1（进程死在 claim 和 fail 之间也耗掉一次） |
| 失败后立即重试同一个 provider 错误 | 没回退 | `fail` 设 `not_before = now + 60 * 2 ** (attempts-1)` |
| `"DURABLE: no"` 了还写 bullets | 没解析判决行 | `parse_stage1` 先查 `_VERDICT`，`no` 直接返回空 `Stage1` |
| 记忆里出现 `- none` 或 `- ...` 条目 | 空占位被当内容 | `body.lower().strip(" .") in {"none", "n/a", "nothing", "(none)", ""}` 跳过 |
| 模型把 `remember_this` 的提议又抽成一条记忆 | 转录里包含 note 调用 | `transcript_of` 排除 `NOTE_NAME` 的调用和结果 |
| 合并输出把模型评论写进 MEMORY.md | `parse_merged` 没检查标记就拆 | 两个标记缺一即抛 `MemoryWriteError`，不写盘 |
| 手改的 diff 被新材料吞掉 | diff 放中间、没标签 | `merge_request` 把 `hand_edits` 放最后并标 "These are the user's own words" |
| 每次合并整个文件顺序都变 | 排名决定顺序 | `prune` 最后按原文件顺序 `kept.sort(key=order.get)` |
| 零引用的老条目被删 | 年龄规则直接删 | 零引用 + 超 30 天 + 超 40 条上限，三者（后两者）才删 |
| `git commit` 在 CI 里失败 | 没设 user.email/name | `ensure_repo` 里 `git config user.email/name`（本地，非全局） |
| `raw/`、`notes/` 进版本库 | 没写 .gitignore | `write_gitignore` 挡 `RAW_DIR/`、`NOTES_DIR/`、`USAGE_FILE` |
| 合并失败丢掉抽取结果 | raw 文件先删了 | 消费（unlink）放在 `write_memory`/`commit` 成功之后 |
