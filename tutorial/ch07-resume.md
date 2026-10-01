# 第 7 章 · 中断与恢复

> **代码**：`steps/step07_resume/`
> **分支**：`feat/rollout`
> **产出**：进程被杀掉之后，`--resume last` 能接着干；按 Ctrl-C 停下来时，停在一个还能继续的地方
> **前置**：做完第 6 章。测试都不联网。两个探针脚本里，`probe_rollout.py` 会真的启动并杀掉进程（不需要网络），
> `probe_resume.py` 需要 `OPENAI_API_KEY`；没有 key 照着读正文里的输出即可。
> **这一章的实测是在两个系统上做的**（Windows 和 Linux），因为有一条结论在两个系统上不一样。

---

## §0 开工前的准备

### 0.1 本章会遇到的新概念

- **落盘**：把内存里的东西写到磁盘上的文件里。进程没了，内存里的东西就没了，磁盘上的还在。
- **JSONL**：一种文件格式，每一行是一个独立的 JSON。和"整个文件是一个 JSON 数组"相对。
- **只追加（append-only）**：只往文件末尾加内容，从不修改已经写下的部分。
- **会话文件（rollout）**：这一章新增的文件，一行一条历史记录。名字沿用 codex 的叫法。
- **取消（cancel）**：`asyncio` 里让一个正在等待的任务停下来的办法。在等待的那个位置会抛出 `asyncio.CancelledError`。
  按 Ctrl-C 时，`asyncio.run()` 做的就是取消主任务。
- **锁文件**：一个专门用来表示"这个东西正在被我使用"的小文件。
- **安慰剂对照**：比较"加了一段话"和"没加"时，再加一组"加了一段无关的话"，用来分清起作用的是**内容**还是"多了一段话"这件事本身。

### 0.2 本章会遇到的新 Python 写法

| 写法 | 意思 |
|---|---|
| `path.open("a", encoding="utf-8", newline="")` | 以"追加"方式打开文件；`newline=""` 表示不要把 `\n` 自动转换成别的 |
| `fh.flush()` / `os.fsync(fh.fileno())` | 把 Python 攒着的内容交给操作系统 / 让操作系统真的写到磁盘上 |
| `os.open(path, os.O_CREAT \| os.O_EXCL \| os.O_WRONLY)` | 创建文件；**如果文件已经存在就报错**（`FileExistsError`） |
| `with RolloutWriter(...) as writer:` | 类里定义了 `__enter__` 和 `__exit__`，就能用在 `with` 里；离开 `with` 时自动执行 `__exit__` |
| `@classmethod` / `cls` | 写在类里、通过类本身调用的方法；`cls` 就是这个类。常用来写"另一种创建对象的方式" |
| `dataclasses.replace(obj, a=1)` | 复制一个 dataclass 对象，只改指定的字段 |
| `{**d, "k": v}` | 新建一个字典：包含 `d` 的全部内容，再加上（或覆盖）`"k"` |
| `except BaseException` 与 `except Exception` | `Exception` 是 `BaseException` 的子类；有少数异常只属于前者，比如 `KeyboardInterrupt`、`asyncio.CancelledError` |
| `asyncio.ensure_future(协程)` / `task.cancel()` | 把协程变成一个可以在后台跑的任务 / 取消它 |
| `subprocess.Popen([...])` / `proc.kill()` | 启动一个子进程 / 强行结束它 |

### 0.3 开分支

```bash
git switch main
git pull
git switch -c feat/rollout
```

---

## §1 这一章要做出来的东西

第 6 章结束时，Agent 能问、能记、能干活，上下文快满时会自己压缩。它唯一还做不到的是：**活过自己这个进程**。

一个你大概遇到过的场景：

```
$ minicodex ask "把 client.py 的 send() 加上重试，然后跑测试"
[读 client.py]
[改 client.py]
[跑 pytest —— 这一步要 40 秒]
```

第 40 秒时你手滑按了 Ctrl-C。或者笔记本合上了。或者程序自己崩了。**然后呢？**

到第 6 章为止，答案是：什么都没有了。历史在内存里，内存跟着进程走了。只能重新问一遍，让它重新读、重新想、重新改——
如果它上次真的改成功了，这一遍还会在改过的文件上再改一次。

这一章要做的事，一句话：**把会话一边发生一边写到磁盘上，并且保证写下来的东西从任何位置断掉，都还能读回来接着用。**

---

## §2 定需求，猜故障

需求：

- 会话的每一条记录，在发生时就写到磁盘；
- `minicodex ask "..." --resume last` 能从上一次停下的地方继续；
- 能列出保存过的会话（`minicodex sessions`），能从某个会话分出一条新的线（`minicodex fork`）；
- Ctrl-C 之后留下的是一段**还能继续**的对话。

动工前的猜测清单，十条：

| 编号 | 猜测 | 怎么判断 |
|---|---|---|
| F07-01 | 进程退出，历史全没了 | 杀掉进程，看磁盘上有什么 |
| F07-02 | 工具执行到一半时崩溃，磁盘上留下"有调用、没结果"的记录 | 在不同时刻杀进程，看文件停在哪 |
| F07-03 | 写文件写到一半被杀，最后一行 JSON 不完整 | 一边狂写一边杀 |
| F07-04 | 按了 Ctrl-C，不知道该停工具还是停整轮 | 取消一个正在跑工具的任务 |
| F07-05 | 恢复之后，模型不知道自己被打断过 | 真模型测 |
| F07-06 | 被打断时，正在跑的命令没有被清理 | 取消后看命令还在不在 |
| F07-07 | 恢复时环境变了（换了模型、换了目录），历史里的内容过期了 | 构造 |
| F07-08 | 两个进程写同一个会话文件，互相破坏 | 真的开两个进程写 |
| F07-09 | 文件格式升级后，旧文件读不了 | 构造旧格式的文件 |
| F07-10 | 从会话中间分出新线后，两条线共用了后面的写入 | 构造 |

先说两条最出乎意料的结果：

- **F07-02 不是"一种可能的情况"，而是几乎唯一的情况**：杀 20 次，有 19 到 20 次停在那个位置。
- **F07-03 一次都没复现出来**，两个系统、三种写法都试了。

---

## §3 最直白的版本：跑完再存

```python
result = await agent.run(question)
Path("session.json").write_text(json.dumps(result.history.to_wire()))
```

两行，而且确实能用——只要那个 `await` 会返回。

这一章的第一个探针 `probe_rollout.py` 回答四个靠推理答不了的问题。先把整个脚本放进来（放在项目根目录），再一段一段看结果：

```python
"""What actually survives when the process does not exit politely.

Four questions this chapter cannot answer by reasoning:

1. If the history is written when `run()` returns, what is on disk when it does
   not return?
2. Does killing a process mid-write really produce half a line, or does the OS
   write whole lines and the worry evaporate?
3. What does a second process appending to the same file do to the first one's
   records?
4. Where does a crashed session actually end -- and is that a place a
   conversation is allowed to end?

Run:  uv run python probe_rollout.py
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path

# --- 1: save-at-the-end -----------------------------------------------------

NAIVE = r"""
import json, sys, time
# The obvious first version: keep the history in memory, write it out at the end.
history = [{"role": "user", "content": "add a retry to the client"}]
for turn in range(50):
    history.append({"role": "assistant", "content": f"turn {turn}"})
    time.sleep(0.05)
open(sys.argv[1], "w", encoding="utf-8").write(json.dumps(history))
print("saved", flush=True)
"""

# --- 2 and 3: what a kill and a second writer do to a file ------------------

CHILD = r"""
import json, sys
path, mode, tag = sys.argv[1], sys.argv[2], sys.argv[3]
# 400KB per line is not a stress test: chapter 6's F06-09 was a 400KB stack
# trace in one tool output, and a rollout line carries the tool output.
size = 400_000 if mode == "big" else 200
n = 4000 if mode == "count" else 100000
with open(path, "a", encoding="utf-8") as fh:
    for i in range(n):
        fh.write(json.dumps({"w": tag, "n": i, "p": "x" * size}) + "\n")
        if mode != "buffered":
            fh.flush()
print("child finished", flush=True)
"""


def _kill_after(args: list[str], seconds: float) -> None:
    proc = subprocess.Popen([sys.executable, "-c", *args])
    try:
        proc.wait(timeout=seconds)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()


def probe_save_at_the_end() -> None:
    print("=== the history is written when the run finishes; the run is killed")
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "history.json"
        _kill_after([NAIVE, str(path)], 0.4)
        print(f"    file exists: {path.exists()}")
        print(f"    bytes on disk: {path.stat().st_size if path.exists() else 0}")


def probe_torn_line() -> None:
    for label, mode in [
        ("small lines (200 bytes), flushed", "flush"),
        ("one 400KB line per write, flushed", "big"),
        ("small lines, default buffering, no flush", "buffered"),
    ]:
        print(f"=== killed mid-write: {label}")
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "out.jsonl"
            _kill_after([CHILD, str(path), mode, "A"], 0.12)
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
            good = 0
            for line in lines:
                try:
                    json.loads(line)
                except json.JSONDecodeError:
                    break
                good += 1
            print(f"    lines: {len(lines)}   parsed before the first bad one: {good}")
            print(f"    torn tail: {len(lines) != good}")


def probe_two_writers() -> None:
    print("=== two processes appending to one file, 4000 records each")
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "shared.jsonl"
        procs = [
            subprocess.Popen([sys.executable, "-c", CHILD, str(path), "count", tag])
            for tag in ("A", "B")
        ]
        for proc in procs:
            proc.wait(timeout=120)
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        seen = {"A": 0, "B": 0}
        broken = 0
        for line in lines:
            try:
                seen[json.loads(line)["w"]] += 1
            except (json.JSONDecodeError, KeyError):
                broken += 1
        print("    records written: 8000")
        print(f"    lines on disk:   {len(lines)}   from A: {seen['A']}   from B: {seen['B']}")
        print(f"    unparseable:     {broken}")
        print(f"    records lost:    {8000 - seen['A'] - seen['B']}")


# --- 4: where a crashed session ends ---------------------------------------

AGENT_LIKE = r"""
import json, sys, time
# One rollout line per history item, as chapter 7 writes them.
path = sys.argv[1]
def w(rec):
    with open(path, "a", encoding="utf-8", newline="") as fh:
        fh.write(json.dumps(rec) + "\n"); fh.flush()
w({"type": "meta", "session_id": "probe", "version": 2})
w({"type": "user", "text": "add a retry to the client"})
for turn in range(50):
    w({"type": "assistant", "text": "", "tool_calls": [
        {"call_id": f"call_{turn}", "name": "run_shell",
         "arguments": {"command": "pytest"}, "raw_arguments": "{\"command\":\"pytest\"}"}]})
    time.sleep(0.08)   # the tool runs
    w({"type": "tool_result", "call_id": f"call_{turn}", "name": "run_shell", "content": "ok"})
"""


def probe_where_it_ends() -> None:
    print("=== an agent-shaped writer, killed at 20 random-ish moments")
    ends: dict[str, int] = {}
    with tempfile.TemporaryDirectory() as d:
        for i in range(20):
            path = Path(d) / f"s{i}.jsonl"
            _kill_after([AGENT_LIKE, str(path)], 0.30 + i * 0.011)
            last = None
            for line in path.read_text(encoding="utf-8").splitlines():
                try:
                    last = json.loads(line)["type"]
                except (json.JSONDecodeError, KeyError):
                    last = "damaged"
            ends[last or "empty"] = ends.get(last or "empty", 0) + 1
    for kind, count in sorted(ends.items(), key=lambda kv: -kv[1]):
        legal = "legal place to stop" if kind != "assistant" else "UNSENDABLE: call with no result"
        print(f"    ends on {kind:<12} {count:>2}/20   {legal}")


if __name__ == "__main__":
    probe_save_at_the_end()
    print()
    probe_torn_line()
    print()
    probe_two_writers()
    print()
    probe_where_it_ends()
```

> - 脚本里有三段写在字符串里的小程序（`NAIVE`、`CHILD`、`AGENT_LIKE`），它们会被当成**子进程**启动：
>   `subprocess.Popen([sys.executable, "-c", 程序文字, 参数...])`，`sys.executable` 是当前这个 Python 解释器的路径。
> - **`_kill_after(args, seconds)`**：启动子进程，等 `seconds` 秒；到时间还没结束就 `proc.kill()`。这就是"模拟崩溃"。
> - 四个 `probe_*` 函数对应四个问题：跑完才存会留下什么；写到一半被杀会不会留下半行；两个进程同时写一个文件会怎样；
>   一个"形状像 Agent"的写入者被杀时停在哪里。
> - `tempfile.TemporaryDirectory()`：一个用完自动删除的临时目录。

第一个问题。`NAIVE` 那个小程序把历史放在内存里，循环 50 次，最后才写文件。让它跑 0.4 秒，然后杀掉：

```
=== the history is written when the run finishes; the run is killed
    file exists: False
    bytes on disk: 0
```

0 字节，文件根本没被创建。**F07-01 成立。**

这个结果本身谁都猜得到。值得想的是它的另一面：一个只在"顺利结束"时才执行的保存，**恰好在唯一需要它的场合不执行**。
顺利跑完的会话，你根本不需要恢复。

> **这条规则在别处也成立**：所有"收尾时清理 / 上报 / 保存"的代码，都要问一句"如果收尾没有发生呢？"
> 如果答案是"那就什么都没有"，这段代码保护的就是不需要保护的那一半。

所以顺序要反过来：**先写下来，再往下做。**

---

## §4 一条一条写下去

每产生一条历史记录，就往文件末尾追加一行 JSON。

为什么是"一行一条"而不是一个 JSON 数组？数组要有右括号。一个 `[...]` 的文件在写完之前不是合法的 JSON——
**它合不合法取决于未来**，而我们做这件事的全部理由，就是未来可能不会到来。一行一条，"写到哪算哪"的结果永远是"前 N 条都在"。

**存什么？** 这是这一节唯一真正的决定。

第 1 章把历史定义成"事实"（`UserMessage`、`AssistantMessage`、`ToolResult`、`SystemNote`），发送时才用 `to_wire()` 变成某个服务商的格式。
写到磁盘的是哪一层？

- 存**发送格式**：最省事，一行 `json.dumps`。但它是**某一个服务商**的格式。
- 存**事实**：要多写两个互相对应的函数（对象 → 字典，字典 → 对象）。

选事实。理由不是"更干净"，而是**这个文件的寿命比程序的任何一次运行都长**——它是唯一一个会被下一个版本、甚至接了另一个服务商的版本读到的东西。

新建 `src/minicodex/rollout.py`。先是开头、会话的"环境信息"，和两个转换函数：

```python
"""The session on disk: what was said, in the order it was said.

Chapter 0's recorder already writes a transcript, and it is not this.  The
recorder writes *requests and responses* -- what crossed the model boundary,
for a human reading it afterwards.  This writes *history items* -- the facts
chapter 1 keeps in memory -- in a shape that can be loaded back into a
`History` and continued.  The two files answer different questions ("what did
the model receive" versus "where were we"), and merging them would mean the
recovery path depends on a debugging aid nobody promised to keep stable.

Three properties, each of them the answer to something measured:

* **Append-only.**  A file rewritten from scratch on every turn is a file that
  can be lost on any turn.  Appending means the worst case is a missing tail,
  and a missing tail is recoverable.

* **One JSON document per line.**  A reader that stops at the first line it
  cannot parse still has everything before it.  This is cheap insurance rather
  than a measured need: five attempts to tear a line by killing the writer
  mid-write produced zero torn lines (see `probe_rollout.py`), so the guard is
  written and honestly labelled unmeasured.

* **One writer.**  Two processes appending to one file is the configuration
  that *did* corrupt it in measurement (`probe_rollout.py`): two processes
  appending 4,000 records each.  On Windows about a third of the records
  were overwritten and gone; on Linux none were lost.  On both, the file
  ended up holding two different sessions' records interleaved.  Refused
  with a lock file rather than tolerated.

`History` is not asked to load itself.  It is rebuilt through its own
`add_*` methods, so a rollout that ends in the middle of a turn cannot become
an invalid conversation: the same invariant that refuses to build one in
memory refuses to load one from disk.
"""

from __future__ import annotations

import json
import os
import time
from collections.abc import Iterable
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from minicodex.agent_types import ToolCall
from minicodex.history import (
    AssistantMessage,
    History,
    HistoryError,
    HistoryItem,
    SystemNote,
    ToolResult,
    UserMessage,
)

DEFAULT_DIR = Path(".minicodex") / "sessions"

# Bumped once already, during this chapter, and the bump is the point: version 1
# stored a tool call's parsed `arguments` and not the `raw_arguments` string the
# model actually sent.  Re-rendering a resumed history therefore produced
# different bytes from the original -- valid JSON, same meaning, different
# string -- which chapter 1 keeps `raw_arguments` around precisely to avoid.
# The old files are still readable; see `_migrate`.
ROLLOUT_VERSION = 2


# One message for one condition.  It was two for an afternoon -- "first record
# is not a session header" from inside the loop and "no session header" from
# after it -- which is two things to grep for and two things to keep in step.
_NO_HEADER = "{path}: no session header; not a rollout file"


class RolloutError(RuntimeError):
    """The session file cannot be used as asked."""


@dataclass(frozen=True)
class SessionMeta:
    """The environment the session was recorded in.

    Stored because a resumed session is not the same run: the model can be
    different, the working directory can be different, the sandbox mode can be
    different, and the history says nothing about any of them.  What is done
    with the difference is `environment_note`'s problem, not this one's.
    """

    session_id: str
    version: int = ROLLOUT_VERSION
    created: float = 0.0
    cwd: str = ""
    provider: str = ""
    model: str = ""
    sandbox_mode: str = ""
    approval_policy: str = ""
    forked_from: str | None = None
    forked_at: int | None = None

    def to_json(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "version": self.version,
            "created": self.created,
            "cwd": self.cwd,
            "provider": self.provider,
            "model": self.model,
            "sandbox_mode": self.sandbox_mode,
            "approval_policy": self.approval_policy,
            "forked_from": self.forked_from,
            "forked_at": self.forked_at,
        }

    @classmethod
    def from_json(cls, payload: dict[str, Any]) -> SessionMeta:
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in payload.items() if k in known})

    def describe(self) -> str:
        where = self.cwd or "?"
        return f"{self.session_id} | {self.model or '?'} | {self.sandbox_mode or '?'} | {where}"


def new_session_id() -> str:
    """Sortable, unique enough, and readable in `ls`.

    Time first so that listing a directory is listing a history.  The pid is
    what makes two agents started in the same second land in different files --
    which matters because the alternative is the two-writer corruption above.
    """
    return f"{time.strftime('%Y%m%dT%H%M%S')}-{os.getpid()}"


def _dump_item(item: HistoryItem) -> dict[str, Any]:
    if isinstance(item, UserMessage):
        return {"type": "user", "text": item.text}
    if isinstance(item, SystemNote):
        return {"type": "system_note", "text": item.text}
    if isinstance(item, ToolResult):
        return {
            "type": "tool_result",
            "call_id": item.call_id,
            "name": item.name,
            "content": item.content,
        }
    if isinstance(item, AssistantMessage):
        return {
            "type": "assistant",
            "text": item.text,
            "tool_calls": [
                {
                    "call_id": c.call_id,
                    "name": c.name,
                    "arguments": c.arguments,
                    # The string the model sent, byte for byte.  Chapter 1 keeps
                    # it so a history can be re-sent without a lossy round trip;
                    # a rollout that drops it re-introduces exactly that loss one
                    # process boundary later.
                    "raw_arguments": c.raw_arguments,
                }
                for c in item.tool_calls
            ],
        }
    raise AssertionError(f"unserialisable history item: {item!r}")  # pragma: no cover


def _load_item(record: dict[str, Any]) -> HistoryItem:
    kind = record.get("type")
    if kind == "user":
        return UserMessage(record["text"])
    if kind == "system_note":
        return SystemNote(record["text"])
    if kind == "tool_result":
        return ToolResult(record["call_id"], record["name"], record["content"])
    if kind == "assistant":
        calls = tuple(
            ToolCall(
                c["call_id"],
                c["name"],
                c.get("arguments"),
                c["raw_arguments"],
            )
            for c in record.get("tool_calls", ())
        )
        return AssistantMessage(record.get("text", ""), calls)
    raise RolloutError(f"unknown record type {kind!r}")
```

> - **开头的 docstring**：先说清它和第 0 章的 `Recorder` 不是一回事——录像记的是"发给模型什么、模型回了什么"，是给人事后看的；
>   这个文件记的是历史本身，是给程序读回去继续用的。然后是三条性质，后面几节逐条讲。
> - `DEFAULT_DIR`：会话文件默认放在 `.minicodex/sessions/`。**这个目录已经在 `.gitignore` 里了**——第 0 章忽略的是整个 `.minicodex/`，
>   所以这一章新造的目录一出生就被挡住了（它装的是整段对话，包括 Agent 读过的每个文件的内容）。
> - `ROLLOUT_VERSION = 2`：格式的版本号。为什么已经是 2，§13 讲。
> - **`SessionMeta`**：这次会话是在什么环境里录下来的——目录、服务商、模型、沙箱模式、审批策略，以及"是从哪个会话分出来的"。
>   为什么要记？因为**历史里一个字都没提这些，而历史的内容全都依赖它们**："读 `client.py`"这句话只在某个目录下才成立。§12 会用到。
>   - `to_json()`：变成字典。
>   - `from_json(payload)`：从字典变回对象。它是 `@classmethod`，所以写成 `SessionMeta.from_json(...)`。
>     `cls.__dataclass_fields__` 是这个 dataclass 所有字段的名字；**只取认识的键**，文件里多出来的键直接忽略——
>     这样新版本往里加字段，旧版本照样读得了。`cls(**字典)`：把字典展开成关键字参数来创建对象。
>   - `describe()`：给人看的一行。
> - **`new_session_id()`**：时间在前（所以按文件名排序就是按时间排序），加上进程号（同一秒启动的两个 Agent 不会撞名）。
> - **`_dump_item(item)`**：历史对象 → 字典。按类型分四种。助手消息里每个工具调用存了四样，其中 `raw_arguments` 是"模型原样发来的那个字符串"——§13 的主角。
> - **`_load_item(record)`**：反过来，字典 → 历史对象。不认识的 `type` 抛 `RolloutError`。

写文件的那个类：

```python
class RolloutWriter:
    """Appends history items to one file, and refuses to share it.

    The lock is a separate file created with `O_EXCL`, not an advisory lock on
    the rollout itself: it has to work on Windows, where the POSIX locking
    calls do not exist, and it has to be inspectable -- a stale lock names the
    pid that left it, so the user can decide rather than guess.
    """

    def __init__(self, path: Path, meta: SessionMeta, *, enabled: bool = True) -> None:
        self.path = Path(path)
        self.meta = meta
        self.enabled = enabled
        self._lock_fd: int | None = None
        self._items = 0
        if not self.enabled:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._acquire_lock()
        if not self.path.exists() or self.path.stat().st_size == 0:
            self._write({"type": "meta", **meta.to_json()})

    # -- lock ---------------------------------------------------------------

    @property
    def lock_path(self) -> Path:
        return self.path.with_suffix(self.path.suffix + ".lock")

    def _acquire_lock(self) -> None:
        try:
            fd = os.open(self.lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            holder = self.lock_path.read_text(encoding="utf-8", errors="replace").strip()
            raise RolloutError(
                f"{self.path} is already open by another minicodex (pid {holder or '?'}). "
                f"Resume it there, or delete {self.lock_path} if that process is gone."
            ) from None
        os.write(fd, str(os.getpid()).encode())
        self._lock_fd = fd

    def release(self) -> None:
        if self._lock_fd is not None:
            os.close(self._lock_fd)
            self._lock_fd = None
            try:
                self.lock_path.unlink()
            except OSError:  # pragma: no cover - someone else already cleaned up
                pass

    def __enter__(self) -> RolloutWriter:
        return self

    def __exit__(self, *exc: object) -> None:
        self.release()

    # -- appending ----------------------------------------------------------

    def _write(self, record: dict[str, Any]) -> None:
        if not self.enabled:
            return
        # `newline=""` because the default on Windows turns every "\n" into
        # "\r\n", and a torn "\r\n" pair is the one corruption two concurrent
        # writers actually produced in measurement.  One writer makes that
        # unreachable; writing "\n" makes it unreachable twice.
        with self.path.open("a", encoding="utf-8", newline="") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
            fh.flush()
            os.fsync(fh.fileno())

    def append(self, item: HistoryItem) -> None:
        self._write({"type_version": ROLLOUT_VERSION, **_dump_item(item)})
        self._items += 1

    def extend(self, items: Iterable[HistoryItem]) -> None:
        for item in items:
            self.append(item)

    def mark(self, kind: str, **payload: Any) -> None:
        """Record something that is not a history item -- a turn boundary, an abort.

        Kept in the same file rather than a second one: the only thing that makes
        "the run stopped here" useful is its position relative to the items, and
        two files have no shared order.
        """
        self._write({"type": "mark", "mark": kind, "ts": time.time(), **payload})

NULL_WRITER = RolloutWriter(Path(os.devnull), SessionMeta("null"), enabled=False)
```

> - **`__init__`**：建目录、拿锁（§8）、如果是新文件就先写一行 meta（文件的第一行永远是环境信息）。
>   `enabled=False` 时什么都不做——`NULL_WRITER` 就是这样一个"什么都不写"的写入者，给不需要落盘的场合（比如以前所有的测试）用。
> - **`_write(record)`**：打开、写一行、`flush()`、`fsync()`、关闭。`flush` 只是把 Python 自己攒的内容交给操作系统，
>   操作系统还可能再攒一会儿；我们要的是"进程没了，这一行还在"，所以还要 `fsync`。第 0 章的 `Recorder` 也是这么写的。
>   `ensure_ascii=False`：中文按原样写，不转成 `\uXXXX`。
> - **`append(item)`**：写一条历史记录。`type_version` 记下"写这一条时的格式版本"。
> - **`mark(kind, **payload)`**：写一条**不是历史**的记录——一个标记，比如"在这里被打断了"。和历史放在同一个文件里，
>   因为标记有用的地方全在于它相对历史的**位置**，分成两个文件就没有共同的顺序了。
> - `__enter__`/`__exit__`：让它能用在 `with` 里，离开时自动放锁。

文件长这样（真实文件的前四行，每行截到 150 个字符）：

```
{"type": "meta", "session_id": "20261001T102447-29040", "version": 2, "created": 1790821487.6437805, "cwd": "D:\\study-codex-with-claude\\codex-from-s
{"type_version": 2, "type": "system_note", "text": "You are a coding agent working in a user's repository.\n\n# What you are allowed to do right now\n
{"type_version": 2, "type": "user", "text": "Read src/minicodex/tool_errors.py and tell me the two prefixes it defines."}
{"type_version": 2, "type": "assistant", "text": "", "tool_calls": [{"call_id": "call_HGai0clKWWkJodZDxRGmUZ87", "name": "read_file", "arguments": {"p
```

---

## §5 谁来调用 `append`

第一个想到的做法是在 `agent.py` 里，历史每加一条就写一条：

```python
history.add_user(user_message)
self.rollout.append(UserMessage(user_message))
...
history.add_assistant(turn.text, turn.tool_calls)
self.rollout.append(AssistantMessage(turn.text, turn.tool_calls))
```

能跑。但同一件事说了两遍，而且有四个地方（`add_user`、`add_system_note`、`add_assistant`、`add_tool_result`）。

第 5 章加到清单里的那条规则，这里又用上了：**一条规则必须只在一个地方执行。**
"每一条进历史的东西都要写到磁盘"如果靠四个调用点各自记得，以后的第五个调用点就会忘。而**忘掉的那一条不会报错**——
它只会在某天有人恢复会话时，安安静静地不在那里。

所以往下放一层，放进 `History` 里。`history.py` 改两处：

```python
    def __init__(self, observer: Callable[[HistoryItem], None] | None = None) -> None:
        # Something to tell whenever an item is accepted.  Chapter 7 uses it to
        # write the session to disk, and the reason it is a hook here rather
        # than four calls in the agent is that four call sites are four places
        # to forget one -- the same argument that put the invariant in this
        # class instead of in the loop.
        self._observer = observer
        self._items: list[HistoryItem] = []
```

```python
    def _append(self, item: HistoryItem) -> None:
        self._items.append(item)
        if self._observer is not None:
            self._observer(item)
```

然后把四个 `add_*` 方法里的 `self._items.append(...)` 全部换成 `self._append(...)`；开头的 import 加上 `Callable`。

> - `observer`（观察者）：一个函数，`History` 每接受一条记录就调用它一次。默认 `None`，所以前面所有章的测试一行都不用改。
> - Agent 那边只需要一行：`History(observer=self.rollout.append)`——把"写文件"这个函数交给 `History`。

> **判断"重复能不能忍"的标准很具体：如果漏掉一个调用点，会发生什么？** 编译错误？测试变红？还是什么都不发生？
> 这里的答案是"什么都不发生，直到某天有人恢复会话"。这种情况下，重复就不能忍。

测试（新建 `tests/test_faults_ch07.py`，开头和帮手）：

```python
"""Chapter 7: what has to survive the process not surviving.

Test names carry the fault ids from FAULTS.md.  Nothing here touches a network
or a real model: a crash is simulated by dropping the process's memory on the
floor, which is exactly what a crash does.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
import time
from collections.abc import AsyncIterator, Sequence
from pathlib import Path
from typing import Any

import pytest

from minicodex.agent import Agent
from minicodex.agent_types import ToolCall
from minicodex.history import AssistantMessage, History, SystemNote, ToolResult, UserMessage
from minicodex.model import Completed, StreamEvent, TextDelta, ToolCallDelta
from minicodex.rollout import (
    ROLLOUT_VERSION,
    RolloutError,
    RolloutWriter,
    SessionMeta,
    environment_note,
    fork,
    interrupted_note,
    list_sessions,
    read_rollout,
    resolve,
)

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def meta(session_id: str = "s1", **kwargs: Any) -> SessionMeta:
    return SessionMeta(session_id=session_id, **kwargs)


def call(call_id: str, name: str = "run_shell", **arguments: Any) -> ToolCall:
    raw = json.dumps(arguments)
    return ToolCall(call_id, name, arguments, raw)


def three_turn_history() -> History:
    """A history in the shape a real run leaves behind: notes, calls, results."""
    history = History()
    history.add_system_note("You are a coding agent.")
    history.add_user("Add a retry to the client.")
    history.add_assistant("Reading it first.", [call("call_1", "read_file", path="client.py")])
    history.add_tool_result("call_1", "def send(): ...")
    history.add_assistant("Patching.", [call("call_2", "apply_patch", path="client.py")])
    history.add_tool_result("call_2", "applied")
    return history


class ScriptedModel:
    """Replays a fixed list of turns.  Same shape as interlude A's."""

    def __init__(self, turns: Sequence[Any]) -> None:
        self.turns = list(turns)
        self.sent: list[list[dict[str, Any]]] = []

    async def stream(self, messages: Sequence[dict[str, Any]]) -> AsyncIterator[StreamEvent]:
        self.sent.append([dict(m) for m in messages])
        turn = self.turns[len(self.sent) - 1]
        if isinstance(turn, str):
            yield TextDelta(turn)
        else:
            for delta in turn:
                yield delta
        yield Completed("stop")


def delta(call_id: str, index: int, name: str, **arguments: Any) -> ToolCallDelta:
    return ToolCallDelta(call_id=call_id, index=index, name=name, arguments=json.dumps(arguments))


```

> - `meta(...)`、`call(...)`：造 `SessionMeta` 和 `ToolCall` 的小帮手。`**kwargs`、`**arguments`：把多余的关键字参数收成一个字典。
> - `three_turn_history()`：一段"像真的"的历史——系统消息、用户消息、两轮"调用 / 结果"。
> - `ScriptedModel`：照着剧本一轮一轮回答的假模型，和插曲 A 的那个一样；`delta(...)` 造一个工具调用事件。

```python
async def test_F07_01_history_survives_the_process(tmp_path: Path) -> None:
    """Everything the agent said reaches disk while it is being said.

    Not "at the end of the run": the run ending normally is the case that did
    not need saving.
    """
    path = tmp_path / "s.jsonl"
    model = ScriptedModel([[delta("call_1", 0, "read_file", path="x.py")], "done"])

    async def read_file(args: dict[str, Any]) -> str:
        return "contents"

    with RolloutWriter(path, meta()) as writer:
        agent = Agent(model, {"read_file": read_file}, rollout=writer)
        result = await agent.run("what is in x.py")

    assert result.stop_reason == "completed"
    loaded = read_rollout(path)
    # user, assistant(call), tool result, assistant(text)
    assert [type(i).__name__ for i in loaded.items] == [
        "UserMessage",
        "AssistantMessage",
        "ToolResult",
        "AssistantMessage",
    ]
    restored, dropped = loaded.history()
    assert dropped == 0
    assert restored.to_wire() == result.history.to_wire()


def test_F07_01_written_during_the_run_not_at_the_end(tmp_path: Path) -> None:
    """The distinction the fault is about, asserted directly.

    A run that is killed halfway has no "end", so a writer that flushes at the
    end writes nothing at all.  Reading the file with the history still live is
    the cheapest way to state that this one does not.
    """
    path = tmp_path / "s.jsonl"
    with RolloutWriter(path, meta()) as writer:
        history = History(observer=writer.append)
        history.add_user("first")
        assert len(read_rollout(path).items) == 1
        history.add_assistant("second")
        assert len(read_rollout(path).items) == 2
```

> - 第一个：一次完整的运行之后，文件里依次是用户消息、带调用的助手消息、工具结果、最后的回答；读回来再变成发送格式，和内存里的一模一样。
>   （它用到 `Agent(..., rollout=writer)` 和 `read_rollout`，后面两节写出来。）
> - 第二个把这条故障的要点直接断言出来：**历史还"活着"的时候去读文件**，加一条，文件里就多一条。

---

## §6 它到底停在哪里

现在文件里一直有东西了。问题变成：**进程被杀时，文件停在什么位置？**

这不是想得出来的问题。探针里的 `AGENT_LIKE` 是一个"形状像 Agent"的写入者：写一行 meta、一行用户消息，然后循环——
写一条带调用的助手消息 → 等 80 毫秒（工具在跑）→ 写一条工具结果。在 20 个略有不同的时刻杀掉它：

```
Windows（2026-10-01）
=== an agent-shaped writer, killed at 20 random-ish moments
    ends on assistant    19/20   UNSENDABLE: call with no result
    ends on tool_result   1/20   legal place to stop

Linux（2026-10-01）
    ends on assistant    20/20   UNSENDABLE: call with no result
```

**20 次里 19 次、20 次。**（这一章最初的记录也是 20/20。）不是"有时候会停在坏的位置"，而是**几乎每一次**。

原因想一下就明白，但不测就想不到要去想：一轮的时间几乎全花在工具上。写两行 JSON 是微秒级的事，跑一次测试是秒级的。
所以随机的一个时刻，落在"调用已经写下、结果还没写下"之间的概率，接近 1。

> **恢复不是"处理一种边界情况"，它是主路径。磁盘上的会话文件，默认就是"坏"的。**

"坏"在这里有精确的含义，第 1 章已经给过了——发起了调用却没有结果的历史，`to_wire()` 拒绝渲染：

```
HistoryError: refusing to send: tool calls with no result: call_1 (slow)
```

**F07-02 成立，而且比猜的严重得多。**

---

## §7 恢复：还是走第 1 章那扇门

怎么把这样一个文件读回成能用的历史？

**第一版**（错的，但多半会先写它）：把最后一条扔掉。拿一个真实的形状试：一轮里模型同时发起两个调用，第一个的结果写下来了，第二个还没有。

```
on disk: ['UserMessage', 'AssistantMessage', 'ToolResult']
naive 'drop the last item' -> HistoryError: refusing to send: tool calls with no result: call_1 (run_shell), call_2 (read_file)
```

**扔掉一条，没回答的调用反而从一个变成了两个**——被扔掉的那条正是 `call_1` 的结果。

**第二版**（还是错的）："最后一条如果是带调用的助手消息，才扔。"对上面这个例子，最后一条是工具结果，一条都不扔——`call_2` 仍然没人回答。

这两版的共同毛病是**用形状去猜合不合法**。合不合法不是形状，是一条规则，而这条规则第 1 章已经实现好了，就在 `History` 里。
所以正确的写法不是"找到最后一个完整的轮次"——那是把同一条规则在第二个地方再实现一遍，两份实现迟早会不一致。
正确的写法是：**重放，并记住每一个"什么都不欠"的时刻。**

```python
@dataclass
class Rollout:
    meta: SessionMeta
    items: list[HistoryItem] = field(default_factory=list)
    marks: list[dict[str, Any]] = field(default_factory=list)
    #: Set when the file stopped making sense partway through.
    truncated_at: int | None = None
    path: Path | None = None

    def history(self) -> tuple[History, int]:
        """Rebuild, dropping any trailing turn that was never finished.

        Returns the history and the number of items dropped.  The rule is not
        "find the last complete turn" as a special case: the history refuses
        anything invalid anyway, so the loading rule is the simpler one --
        *replay, and remember the last point at which nothing was outstanding.*
        Where that point is falls out of chapter 1's invariant rather than being
        computed separately, which means it cannot disagree with it.
        """
        history = History()
        settled: list[HistoryItem] = []
        for item in self.items:
            try:
                _add(history, item)
            except HistoryError:
                # The file contains something no conversation can contain --
                # a result for a call that is not there, most likely because
                # the assistant message ahead of it was lost.  Stop; what is
                # already settled is still a conversation.
                break
            if not history.unanswered():
                settled = list(history.items)

        dropped = len(self.items) - len(settled)
        if dropped:
            history = History()
            for item in settled:
                _add(history, item)
        return history, dropped


def _add(history: History, item: HistoryItem) -> None:
    if isinstance(item, UserMessage):
        history.add_user(item.text)
    elif isinstance(item, SystemNote):
        history.add_system_note(item.text)
    elif isinstance(item, AssistantMessage):
        history.add_assistant(item.text, item.tool_calls)
    elif isinstance(item, ToolResult):
        history.add_tool_result(item.call_id, item.content)
    else:  # pragma: no cover
        raise AssertionError(item)
```

> - **`Rollout`**：读文件得到的东西——环境信息、历史记录、标记、"从第几行开始读不懂了"、文件路径。
> - **`history()`**：返回重建好的 `History`，和被丢掉的条数。
>   - 一条一条用 `_add` 放进一个新的 `History`。`_add` 按类型调用 `add_user`/`add_assistant`/`add_tool_result`/`add_system_note`——**走第 1 章那扇门**。
>   - 每放一条，问一句 `history.unanswered()`（有没有还没回答的调用）。没有，就把"到目前为止"记成一个安全点（`settled`）。
>   - 如果 `add_*` 抛了 `HistoryError`（文件里有第 1 章不接受的东西），就在那里停。
>   - 最后，安全点之后的全部丢掉；如果真的丢了东西，就只用安全点之前的重新建一遍。

于是"哪里可以停"这个问题根本不需要单独回答，它是 `unanswered()` 的副产品。上面那个例子：

```
replay rule -> kept ['UserMessage'] dropped 2
```

> **代价要说清楚**：`call_1` 那个工具真的跑过，它的结果被丢掉了。这是有意的——留着它，就得留着发起它的那条助手消息，
> 而那条消息里还有一个没人回答的 `call_2`。**"丢掉一次真实的工作"和"造出一段没有服务端肯接受的历史"之间，选前者。**
> §11 要做的，就是让模型知道这件事发生过。

读文件的函数：

```python
def read_rollout(path: Path) -> Rollout:
    """Load a session file, tolerating a damaged tail.

    Line by line, and the first line that does not parse ends the file.  Not
    "skip the bad line and carry on": a gap in the middle of a conversation is
    a conversation with a hole in it, and a hole is exactly the shape chapter
    1's invariant exists to reject.  Stopping keeps a prefix, which is a real
    conversation, over a filtered file, which is a plausible-looking fiction.
    """
    path = Path(path)
    if not path.exists():
        raise RolloutError(f"no session file at {path}")

    meta: SessionMeta | None = None
    items: list[HistoryItem] = []
    marks: list[dict[str, Any]] = []
    truncated_at: int | None = None
    # Index of the first item of a compaction baseline that is still being
    # written, or None when no compaction is in progress.
    baseline_from: int | None = None

    with path.open("r", encoding="utf-8", errors="replace", newline="") as fh:
        for lineno, line in enumerate(fh, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                truncated_at = lineno
                break
            if not isinstance(record, dict):
                truncated_at = lineno
                break
            if record.get("type") == "meta":
                meta = SessionMeta.from_json(record)
                continue
            if meta is None:
                raise RolloutError(_NO_HEADER.format(path=path))
            if record.get("type") == "mark":
                marks.append(record)
                kind = record.get("mark")
                if kind == "compacting":
                    # A compaction is about to write its replacement history.
                    # Remember where the replacement starts; nothing is
                    # discarded until the matching "compacted" says the
                    # replacement is all there.
                    baseline_from = len(items)
                elif kind == "compacted":
                    if baseline_from is None:
                        # A file written before there were two markers: a
                        # single one, placed *ahead* of the baseline.
                        items.clear()
                    else:
                        # Everything before the baseline was replaced in memory
                        # by the summary inside it.  Replaying it would undo the
                        # compaction on every resume -- the session would come
                        # back at the size that made it compact in the first
                        # place.
                        del items[:baseline_from]
                        baseline_from = None
                continue
            try:
                items.append(_load_item(_migrate(record, meta.version)))
            except (RolloutError, KeyError):
                truncated_at = lineno
                break

    if baseline_from is not None:
        # The file ends inside a compaction: "compacting" with no "compacted".
        # The replacement was never finished, so *it* is the part to discard --
        # what it was replacing is still here, whole.  With a single marker
        # written first, this same file loaded as one system note and nothing
        # else, with nothing reported as dropped.
        del items[baseline_from:]

    if meta is None:
        raise RolloutError(_NO_HEADER.format(path=path))
    return Rollout(meta=meta, items=items, marks=marks, truncated_at=truncated_at, path=path)
```

> - 一行一行读。空行跳过；**第一行解析不了的，就到此为止**（记下行号，`break`）。
> - `type` 是 `meta` 的：环境信息。在读到 meta 之前就出现了别的记录——这不是会话文件，抛 `RolloutError`。
> - `type` 是 `mark` 的：标记。其中 `compacting`/`compacted` 这一对和压缩有关，§15 讲；先跳过那一段。
> - 其余的：先迁移到当前版本（§13），再变成历史对象。**不认识的类型、缺字段，同样到此为止。**

> **为什么读不懂的那一行之后要停，而不是跳过它继续读？** 跳过会得到一段"中间有个洞"的对话——比如一个结果，它的调用不见了。
> 那正是这一节在防的形状，只是从另一扇门进来。**保留一个前缀（是真实发生过的对话），好过筛出一个看起来完整的东西（是虚构的）。**
> 不认识的记录类型也一样：新版本写的一种记录，对旧版本来说就是"这里发生过一件我不知道的事"；跳过它，等于假装它没发生。

测试：

```python
def test_F07_02_unanswered_call_is_truncated_on_load(tmp_path: Path) -> None:
    """The file ends after a call and before its result: a shape no server accepts."""
    path = tmp_path / "s.jsonl"
    with RolloutWriter(path, meta()) as writer:
        history = History(observer=writer.append)
        history.add_user("go")
        history.add_assistant("running it", [call("call_1")])
        # ...and here the process dies.

    loaded = read_rollout(path)
    assert len(loaded.items) == 2

    restored, dropped = loaded.history()
    assert dropped == 1
    assert [type(i).__name__ for i in restored.items] == ["UserMessage"]
    # The real assertion: what comes back can be sent.
    assert restored.to_wire() == [{"role": "user", "content": "go"}]


def test_F07_02_only_the_incomplete_tail_is_dropped(tmp_path: Path) -> None:
    """Two finished turns and one unfinished one: the finished work is kept."""
    path = tmp_path / "s.jsonl"
    with RolloutWriter(path, meta()) as writer:
        history = History(observer=writer.append)
        for item in three_turn_history().items:
            _replay(history, item)
        history.add_assistant("Running the tests.", [call("call_3")])

    restored, dropped = read_rollout(path).history()
    assert dropped == 1
    assert len(restored) == 6
    assert restored.unanswered() == ()


def test_F07_02_a_partially_answered_turn_is_dropped_whole(tmp_path: Path) -> None:
    """Two calls in one turn, one answered: the turn is not half-kept.

    Keeping the answered half would leave an assistant message whose second
    call has no result -- the invalid shape this whole mechanism exists to
    avoid -- so the boundary is "nothing outstanding", not "drop the last
    item".
    """
    path = tmp_path / "s.jsonl"
    with RolloutWriter(path, meta()) as writer:
        history = History(observer=writer.append)
        history.add_user("go")
        history.add_assistant("two things", [call("call_1"), call("call_2")])
        history.add_tool_result("call_1", "ok")

    restored, dropped = read_rollout(path).history()
    assert dropped == 2
    assert [type(i).__name__ for i in restored.items] == ["UserMessage"]


def test_F07_02_a_session_with_no_complete_turn_recovers_to_nothing(tmp_path: Path) -> None:
    """Raised in review: `settled` can be empty, and the caller must be able to tell.

    An empty history plus `dropped == len(items)` is a legal result -- it means
    "there is no complete conversation in this file", which is true of a session
    that crashed inside its first turn.  The CLI prints the count for exactly
    this reason.
    """
    path = tmp_path / "s.jsonl"
    with RolloutWriter(path, meta()) as writer:
        writer.append(AssistantMessage("straight in", (call("call_1"),)))

    loaded = read_rollout(path)
    restored, dropped = loaded.history()
    assert dropped == len(loaded.items) == 1
    assert len(restored) == 0
    assert restored.to_wire() == []


def _replay(history: History, item: Any) -> None:
    if isinstance(item, UserMessage):
        history.add_user(item.text)
    elif isinstance(item, SystemNote):
        history.add_system_note(item.text)
    elif isinstance(item, AssistantMessage):
        history.add_assistant(item.text, item.tool_calls)
    elif isinstance(item, ToolResult):
        history.add_tool_result(item.call_id, item.content)
```

> - 文件停在一个调用之后：丢 1 条，剩下的能发送；
> - 前面两轮完整、最后一轮没完：只丢最后那一条；
> - **一轮两个调用、只回答了一个：整轮丢掉**（丢 2 条）。把 `history()` 换成前面那个"第二版"，实测只有这一个测试会红；
> - 文件里连一轮完整的都没有：恢复出一个空的历史，`dropped` 等于全部条数。这是合法的结果，意思是"这个文件里没有任何一段完整的对话"。
> - 末尾的 `_replay` 是测试自己用的小帮手，把一段现成的历史重放进另一个 `History`。

```bash
git add src/minicodex/history.py src/minicodex/rollout.py tests/test_faults_ch07.py probe_rollout.py
git commit -m "feat(rollout): write the session as it happens, and recover by replaying it"
```

---

## §8 F07-03：撕不开的那一行

F07-03 猜的是：写文件写到一半被杀，最后一行 JSON 不完整。这条看起来一定会复现——讲日志的文章都这么说。
探针的第二段：一个进程拼命往文件里写 JSON 行，0.12 秒后杀掉。三种写法：

```
Windows
=== killed mid-write: small lines (200 bytes), flushed
    lines: 15498   parsed before the first bad one: 15498
    torn tail: False
=== killed mid-write: one 400KB line per write, flushed
    lines: 91   parsed before the first bad one: 91
    torn tail: False
=== killed mid-write: small lines, default buffering, no flush
    lines: 43020   parsed before the first bad one: 43020
    torn tail: False

Linux
    lines: 33024   parsed before the first bad one: 33024      torn tail: False
    lines: 65      parsed before the first bad one: 65         torn tail: False
    lines: 63540   parsed before the first bad one: 63540      torn tail: False
```

**两个系统，三种写法，一次都没撕开。**

第三种是特意加的：不 `flush`，让 Python 自己攒够一个缓冲区再写——缓冲区的边界几乎必然落在某一行中间，"应该"会撕开。没有。
400KB 一行的那组也是特意加的（第 6 章说过的那种巨大的错误堆栈，写进会话文件就是这样的一行）。也没有。

为什么撕不开，这里不假装完全清楚。**清楚的是：没能复现它。** 那怎么办？三个选择：

1. **假装复现了**，写一句"经验表明会撕裂"，配一段防御代码。——不行。这本教程立足的就是"贴出来的每一行输出都是真跑的"。
2. **不写防御**，因为没测到。——也不行。这个防御只有三行（解析失败就停），而它防的事一旦发生，代价是整个会话读不出来。
3. **写防御，并且明确标注它没被测到。**

选 3，而且这句话写在模块的 docstring 里（"cheap insurance rather than a measured need"），不是写在某条提交信息里。

> **值不值得写一段防御代码，看两个数：写它的成本，和它防的那件事发生时的成本。**
> "我复现不出来"只能降低前者的优先级，决定不了后者。但"我复现不出来"**必须写下来**——否则下一个人会以为它是验证过的。

而"读到坏行就停"这个行为本身是要测的——它是我们自己的读取代码的行为，和"能不能撕开"无关：

```python
def test_F07_03_a_truncated_last_line_is_the_boundary(tmp_path: Path) -> None:
    path = tmp_path / "s.jsonl"
    with RolloutWriter(path, meta()) as writer:
        history = History(observer=writer.append)
        history.add_user("go")
        history.add_assistant("here you go")
    with path.open("a", encoding="utf-8", newline="") as fh:
        fh.write('{"type": "user", "te')

    loaded = read_rollout(path)
    assert loaded.truncated_at == 4  # meta, user, assistant, junk
    assert len(loaded.items) == 2
    assert loaded.history()[0].to_wire()[0] == {"role": "user", "content": "go"}


def test_F07_03_damage_in_the_middle_does_not_resurrect_the_tail(tmp_path: Path) -> None:
    """A bad line stops the read; later lines are not skipped past.

    Skipping would produce a history with a hole in it -- a result whose call
    is missing -- which is the F07-02 shape arriving by a different route.
    """
    path = tmp_path / "s.jsonl"
    with RolloutWriter(path, meta()) as writer:
        history = History(observer=writer.append)
        history.add_user("go")
        history.add_assistant("calling", [call("call_1")])
        history.add_tool_result("call_1", "output")

    lines = path.read_text(encoding="utf-8").splitlines()
    lines.insert(2, "}not json{")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="")

    loaded = read_rollout(path)
    assert loaded.truncated_at == 3
    assert len(loaded.items) == 1
    assert loaded.history()[1] == 0


def test_F07_03_an_unknown_record_type_is_also_a_boundary(tmp_path: Path) -> None:
    """Well-formed JSON this version does not understand is not skipped either."""
    path = tmp_path / "s.jsonl"
    with RolloutWriter(path, meta()) as writer:
        writer.append(UserMessage("go"))
    with path.open("a", encoding="utf-8", newline="") as fh:
        fh.write(json.dumps({"type": "reasoning_summary", "text": "hm"}) + "\n")
        fh.write(json.dumps({"type": "user", "text": "later"}) + "\n")

    loaded = read_rollout(path)
    assert loaded.truncated_at == 3
    assert len(loaded.items) == 1


def test_F07_03_a_file_without_a_header_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "not-a-session.jsonl"
    path.write_text(json.dumps({"type": "user", "text": "hi"}) + "\n", encoding="utf-8")
    with pytest.raises(RolloutError, match="no session header"):
        read_rollout(path)
```

> - 手工在文件末尾加半行：`truncated_at` 是 4（meta、用户、助手、坏行），前面两条还在；
> - 在文件**中间**插一行坏的：到那里为止，后面完好的行也不读了；
> - 一条格式正确、但类型不认识的记录：同样是边界；
> - 没有 meta 开头的文件：直接拒绝。

---

## §9 F07-08：两个进程写同一个文件

探针的第三段：两个进程各往同一个文件追加 4000 条记录，一共 8000 条。**这是全章唯一一条在两个系统上结果不同的。**

```
Windows
=== two processes appending to one file, 4000 records each
    records written: 8000
    lines on disk:   5389   from A: 2772   from B: 2566
    unparseable:     51
    records lost:    2662

Linux
    records written: 8000
    lines on disk:   8000   from A: 4000   from B: 4000
    unparseable:     0
    records lost:    0
```

**在 Windows 上，2662 条记录没了**——不是错位，不是乱序，是不存在（这个数每次跑都不一样，最初的记录是 2469）。
两个进程各自记着自己写到了文件的哪个位置，一个写过的地方被另一个盖掉了。
那几十行读不出来的，是另一回事：Python 在 Windows 上以文本方式写文件时，会把 `\n` 变成 `\r\n` 两个字节，**这两个字节之间可以被另一个进程插进来**。
`_write` 里的 `newline=""` 就是因此加的：只写 `\n` 一个字节，撕不开。

**在 Linux 上，一条都没丢。** Linux 的"追加"是由操作系统保证的：每次小的写入都原子地落在文件末尾。

那在 Linux 上是不是就没问题？不是。8000 行都在，但它们是**两个会话的记录一行隔一行地混在同一个文件里**。
读它的人没法把它们分开，而 §7 的重放规则会在第一条"对不上"的记录那里停下。

所以两个系统上结论一致，只是坏法不同：**一个会话文件只能有一个写入者。**

> 这一章原来的文字只有 Windows 的数字，并且把"丢了两千多条"当成了普遍事实；测试里也断言"磁盘上一定少于 8000 行"。
> 这个项目的 CI 配置跑在 Linux 上——**那个测试在 CI 里是会失败的**，只是这些快照从来没在 Linux 上跑过，所以没人知道。
> 改写时在 Linux 上跑了一遍才发现。

真正的修法是**不许有第二个写入者**——`RolloutWriter` 里那几个和锁有关的方法（§4 已经抄进去了）：

> - **`lock_path`**：会话文件名后面加 `.lock`。
> - **`_acquire_lock()`**：用 `O_CREAT | O_EXCL` 创建锁文件——**文件已经存在就失败**。失败时读出锁文件里的内容（持有者的进程号），
>   抛一个把话说全的 `RolloutError`。成功就把自己的进程号写进去。`from None`：不显示原来那个 `FileExistsError` 的堆栈，它对用户没用。
> - **`release()`**：关闭并删除锁文件。

真实的报错：

```
RolloutError: C:\...\s.jsonl is already open by another minicodex (pid 52932). Resume it there, or delete C:\...\s.jsonl.lock if that process is gone.
```

三个决定：

- **单独的 `.lock` 文件加 `O_EXCL`**，而不是操作系统提供的文件锁：后者在 Windows 和 Linux 上是两套完全不同的接口。
- **锁文件里写着进程号**：进程崩溃后会留下一个没人删的锁，要由人来判断"那个进程还在吗"，而人没法凭空判断。
  报错里给出进程号和锁文件的路径，**让用户能做决定，而不是只能发愣**。
- **不自动清理残留的锁**：判断"那个进程号还活着吗"并不可靠（进程号会被重复使用）。猜错一次，就回到两个写入者的世界。

### 9.1 而在今天的命令行里，这把锁碰不到

写完之后去找触发它的路径，发现一件不太舒服的事：**每次 `minicodex ask` 都会创建一个新文件**（`时间-进程号.jsonl`），`--resume` 也是写新文件。
所以在今天的命令行下，两个进程不可能选中同一个文件名。

那这把锁是不是摆设？想清楚之后的答案是：**不是，但理由不是一开始以为的那个。**

- 现在碰不到，是因为**起名的办法**恰好保证了不重名。起名的办法是最容易被改的东西——哪天有人觉得"恢复应该接着写同一个文件"（codex 就是这么做的），不重名立刻不成立。
  **"因为文件名不会撞，所以安全"是一个没有写下来的假设。**
- 库这一层碰得到：`RolloutWriter(path, meta)` 是公开的，`fork` 会用它，测试也直接用它。
- 成本是十几行。

**一把锁，是把一个"目前碰巧成立"的事实，变成一个"一直成立"的事实。** 这句实话也写进了 `README` 的"故意没做"里："这把锁目前从命令行碰不到"。

```python
def test_F07_08_a_second_writer_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "s.jsonl"
    with RolloutWriter(path, meta()):
        with pytest.raises(RolloutError, match="already open"):
            RolloutWriter(path, meta("s2"))


def test_F07_08_the_lock_names_the_process_holding_it(tmp_path: Path) -> None:
    """A stale lock is a decision for the user, and they cannot make it blind."""
    import os

    path = tmp_path / "s.jsonl"
    with RolloutWriter(path, meta()):
        with pytest.raises(RolloutError) as excinfo:
            RolloutWriter(path, meta("s2"))
    assert str(os.getpid()) in str(excinfo.value)
    assert ".lock" in str(excinfo.value)


def test_F07_08_the_lock_is_released_on_close(tmp_path: Path) -> None:
    path = tmp_path / "s.jsonl"
    with RolloutWriter(path, meta()) as first:
        assert first.lock_path.exists()
    assert not first.lock_path.exists()
    RolloutWriter(path, meta("s2")).release()


def test_F07_08_two_real_processes_leave_one_file_holding_two_sessions(tmp_path: Path) -> None:
    """The measurement the lock exists for, at a size that runs in CI.

    Not a test of minicodex: a test of the assumption underneath it.  What two
    writers do to one file depends on the platform, and it was measured on both
    (`probe_rollout.py`):

        Windows/NTFS   8000 written, about 5400 on disk -- each process keeps
                       its own offset and they overwrite each other's records
        Linux          8000 written, 8000 on disk -- O_APPEND makes each small
                       write atomic, so nothing is lost

    The first version of this test asserted `len(lines) < 8000`.  That is the
    Windows result, the machine it was written on; this project's CI runs on
    Linux, where the assertion fails.  A test of an assumption has to state the
    part of the assumption that is true everywhere, and that part is the one a
    session file cares about: the records of two unrelated writers end up
    interleaved in one file, and no reader can take them apart again.
    """
    path = tmp_path / "shared.jsonl"
    child = (
        "import json,sys,time\n"
        "tag=sys.argv[2]\n"
        "fh=open(sys.argv[1],'a',encoding='utf-8')\n"
        # Both children wait for the same wall-clock instant, so that they are
        # actually writing at the same time rather than one after the other.
        "time.sleep(max(0.0,float(sys.argv[3])-time.time()))\n"
        "for i in range(4000):\n"
        "    fh.write(json.dumps({'w':tag,'n':i,'p':'x'*200})+'\\n'); fh.flush()\n"
    )
    start = str(time.time() + 1.0)
    procs = [
        subprocess.Popen([sys.executable, "-c", child, str(path), tag, start]) for tag in ("A", "B")
    ]
    for proc in procs:
        proc.wait(timeout=60)

    order = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            order.append(json.loads(line)["w"])
        except (json.JSONDecodeError, KeyError):
            continue
    switches = sum(1 for i in range(1, len(order)) if order[i] != order[i - 1])
    assert set(order) == {"A", "B"}
    assert len(order) <= 8000
    # One switch would mean A wrote everything and then B did: two sessions end
    # to end, which a reader could still split.  More than that is a single
    # file whose lines alternate between two conversations.
    assert switches > 1, "the writers did not overlap; the lock would be ceremony"
```

> - 第二个写入者被拒绝；报错里有持有者的进程号和 `.lock`；离开 `with` 后锁文件消失，可以再打开。
> - **最后一个测试不是在测我们的代码，而是在测我们的代码所依赖的前提**："两个写入者会把一个文件弄坏"。
>   它真的启动两个进程（让它们等到同一个时刻再开始写，确保真的在同时写），然后断言**两个系统上都成立的那部分**：
>   文件里有两个写入者的记录，而且它们交替出现了不止一次。docstring 里记下了两个系统各自的数字，和第一版断言错在哪里。
>   `switches` 数的是"相邻两行来自不同写入者"的次数。
>
> **一个测"前提"的测试，要断言这个前提里在哪儿都成立的那一部分。** 把一台机器上的现象当成前提，测试就只在那台机器上是绿的。

```bash
git add src/minicodex/rollout.py tests/test_faults_ch07.py
git commit -m "feat(rollout): one writer per session file, and a reader that stops at the first bad line"
```

---

## §10 F07-04：Ctrl-C 打断的是谁

"崩溃"这条线到这里走完了。**"用户主动打断"是另一条线，而且更麻烦**：崩溃是管不了的，打断是管得了的——管得了，就得由我们决定它怎么发生。

在 Python 里，`asyncio.run()` 下面按 Ctrl-C，表现为**主任务被取消**：程序当时正停在哪个 `await` 上，那里就抛出 `asyncio.CancelledError`。
而按 §6 的测量，程序停着的地方**几乎总是在某个工具里面**。

于是第 0 章的一个承诺出了问题。第 0 章里执行工具的那个方法是这样保证"永远不抛异常"的：

```python
try:
    return await self.tools[call.name](call.arguments)
except Exception as exc:  # deliberately broad; see the docstring
    return f"Error: {call.name} raised {type(exc).__name__}: {exc}"
```

注释写着"故意写得很宽"。它还不够宽：

```
issubclass(asyncio.CancelledError, Exception): False
issubclass(asyncio.CancelledError, BaseException): True
```

**`CancelledError` 不是 `Exception`。** 所以 Ctrl-C 会直接穿过这个"永远不抛异常"的方法，留下一个发起了、却没有结果的调用——
**和崩溃留下的形状一模一样**，只是这次它在内存里，而且是我们自己造成的。

修法：在循环里调用工具的那个 `await` 上单独接一次，**先把已经发出去的调用全部回答掉**，然后才让这次运行结束。`agent.py` 的 `run()` 末尾那个循环改成：

```python
            for index, call in enumerate(turn.tool_calls):
                try:
                    output = await self._run_tool(call)
                except (asyncio.CancelledError, KeyboardInterrupt):
                    for pending in turn.tool_calls[index:]:
                        history.add_tool_result(
                            pending.call_id,
                            "Error: interrupted by the user before this finished. "
                            "It may have run partially, or not at all.",
                        )
                    self.rollout.mark("interrupted", turn=turn_index)
                    history.add_system_note(interrupted_note())
                    return RunResult(
                        final_text, "interrupted", turn_index + 1, history, tuple(compactions)
                    )
                history.add_tool_result(call.call_id, output)
```

四个细节，每一个都对应一个测试：

**一，从 `index` 开始回答，不是只回答当前这一个。** 一轮里可能有三个调用，被打断时后两个还没开始。它们同样是"发起了、没有结果"。

**二，回答的措辞是"可能执行了一部分，也可能根本没执行"，不是"已取消"。** 因为**我们不知道**：工具是等到一半被取消的，子进程可能已经写了半个文件。
把不确定如实交给模型，好过替它编一个确定的说法。

**三，回答完就结束这次运行，不再问模型。** 用户按 Ctrl-C 是要它**停**，不是要它跳过这个工具换个办法接着来。
一个回答完调用、又去问模型"接下来怎么办"的循环，是花钱无视用户。`stop_reason` 是 `"interrupted"`。

**四，不能把这个 `except` 包在整个循环外面。** 那样的话，在**等模型回答**时按 Ctrl-C，也会被"处理"成一次干净的中断——
而任务其实没有被取消，等着它的上一层会拿到一个正常的返回值。**这是对上层撒谎。**

> 同一个 `except CancelledError`，包在"调用工具"那一个 `await` 上是对的，包在整个循环上是错的——
> **而两种写法都能让"按 Ctrl-C 不再打印一大段报错"这个现象消失。** 现象消失了，不等于修对了地方。

`agent.py` 开头加 `import asyncio`。

```python
async def test_F07_04_cancelling_a_tool_answers_every_issued_call(tmp_path: Path) -> None:
    """The invariant survives the interrupt.

    `_run_tool` promises never to raise, and `except Exception` does not catch
    `CancelledError`.  Without the explicit clause, the loop unwinds leaving
    calls unanswered and the history unsendable.
    """
    path = tmp_path / "s.jsonl"
    model = ScriptedModel(
        [[delta("call_1", 0, "slow", x=1), delta("call_2", 1, "slow", x=2)], "done"]
    )
    started = asyncio.Event()

    async def slow(args: dict[str, Any]) -> str:
        started.set()
        await asyncio.sleep(60)
        return "never"

    with RolloutWriter(path, meta()) as writer:
        agent = Agent(model, {"slow": slow}, rollout=writer)
        task = asyncio.ensure_future(agent.run("go"))
        await started.wait()
        task.cancel()
        result = await task

    assert result.stop_reason == "interrupted"
    assert result.history.unanswered() == ()
    # Both calls answered, both saying so.
    outputs = [i for i in result.history.items if isinstance(i, ToolResult)]
    assert len(outputs) == 2
    assert all("interrupted by the user" in o.content for o in outputs)
    # And the next request would be legal.
    assert result.history.to_wire()


async def test_F07_04_the_run_ends_rather_than_continuing(tmp_path: Path) -> None:
    """Cancelling the tool does not mean "skip this tool and carry on".

    The user pressed Ctrl-C to stop something.  A loop that answers the call and
    then asks the model what to do next has spent money to ignore them.
    """
    model = ScriptedModel([[delta("call_1", 0, "slow")], "should never be reached"])
    started = asyncio.Event()

    async def slow(args: dict[str, Any]) -> str:
        started.set()
        await asyncio.sleep(60)
        return "never"

    agent = Agent(model, {"slow": slow})
    task = asyncio.ensure_future(agent.run("go"))
    await started.wait()
    task.cancel()
    result = await task

    assert result.stop_reason == "interrupted"
    assert len(model.sent) == 1


async def test_F07_04_an_interrupt_between_turns_is_not_swallowed() -> None:
    """Cancellation outside a tool call still cancels.

    The clause added for F07-04 catches `CancelledError` around one `await`.
    If it were written around the whole loop instead, a Ctrl-C while waiting on
    the *model* would be reported as a clean interrupted run -- and the task
    would not actually be cancelled, which is a lie to whoever awaits it.
    """

    class SlowModel:
        async def stream(self, messages: Sequence[dict[str, Any]]) -> AsyncIterator[StreamEvent]:
            await asyncio.sleep(60)
            yield Completed("stop")

    agent = Agent(SlowModel(), {})
    task = asyncio.ensure_future(agent.run("go"))
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
```

> - 测试怎么"按 Ctrl-C"：用 `asyncio.ensure_future` 让 `agent.run(...)` 在后台跑；工具一开始就 `started.set()` 发个信号，然后睡 60 秒；
>   测试等到信号，就 `task.cancel()`。`asyncio.Event` 是协程之间传"某件事发生了"的小工具。
> - 第一个：两个调用都得到了回答，内容都说"被用户打断"，历史能发送；
> - 第二个：模型只被请求了**一次**——打断之后没有再去问它；
> - 第三个：模型还在回答时取消，`await task` 应该抛出 `CancelledError`，而不是悄悄返回。

---

## §11 F07-06：被打断的命令还在跑

F07-06 猜的是：被打断时，正在跑的命令没有被清理。去看第 2 章的清理代码，它在超时或输出过长时杀掉整个进程组——
但那段代码在 `if give_up is not None:` 里面。取消不会让 `give_up` 有值，取消是从读输出的那个 `await` 里直接抛出来的。

**所以第 2 章的清理，在第 7 章新开的这扇门上不生效。** 命令继续跑，Agent 进程没了，再没有任何东西知道它的存在——
这就是第 2 章的"孤儿进程"，**从一扇它被修好时还不存在的门里回来了**。

`shell.py` 改两处。把"杀掉进程组"抽成一个函数（现在有两个地方要用了）：

```python
def _kill_group(proc: asyncio.subprocess.Process) -> None:
    """Kill the command and everything it started.

    `shell=True` means the command is the shell's child, so killing the shell
    alone leaves a grandchild running with no parent watching it -- measured in
    chapter 2 with a `sleep 3600` that outlived its own timeout.

    `killpg` is POSIX-only.  On Windows this does nothing, which is F02-10 and
    is still not fixed; saying so in one place beats an `AttributeError` raised
    from three.
    """
    if not hasattr(os, "killpg"):  # pragma: no cover - platform branch
        return
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):  # pragma: no cover
        pass
```

原来那一行 `os.killpg(proc.pid, signal.SIGKILL)` 换成 `_kill_group(proc)`。然后在 `run()` 的 `try` 上，`finally` 之前，加一个分支：

```python
        except asyncio.CancelledError:
            # A Ctrl-C while a command is running unwinds through here, and
            # without this clause the command keeps running: the agent process
            # goes away, the subprocess does not, and nothing holds a reference
            # to it any more.  That is F02-08 again, arriving through a door
            # that did not exist when F02-08 was fixed -- the timeout path was
            # the only way out of this function when that kill was written.
            _kill_group(proc)
            raise
```

> - `_kill_group`：没有 `os.killpg` 的系统（Windows）上什么都不做——F02-10 那个缺口仍然没修，但现在这句话只需要写在一个地方。
> - `raise`（后面不带东西）：把刚接住的异常原样再抛出去。**清理完，取消还得继续往上传。**

```python
@pytest.mark.skipif(
    not hasattr(__import__("os"), "killpg"),
    reason="F02-10: killpg is POSIX-only and the Windows equivalent is not built",
)
async def test_F07_06_cancelling_a_command_kills_it(tmp_path: Path) -> None:
    """A cancelled `run_shell` does not leave the command's children running.

    Chapter 2 killed the process group on timeout, which was the only way out
    of that function at the time.  Cancellation is a second way out.

    The marker is written by a *grandchild* -- a shell started by the shell --
    and the test waits longer than the command sleeps.  Both halves matter and
    both were wrong at first, found by deleting the kill and watching nothing
    go red, twice:

    * `sleep 5; touch marker`, checked after one second: the test looked four
      seconds before a surviving command would have written anything.
    * `sleep 1; touch marker`, checked after two: still green, because closing
      the subprocess transport kills the *shell*, and a dead shell never
      reaches its `touch`.  The orphan that survives is the shell's child, so
      that is who has to be holding the marker.
    """
    from minicodex.shell import ShellSession

    session = ShellSession(timeout=30)
    marker = tmp_path / "f07_06.marker"
    task = asyncio.ensure_future(session.run(f"sh -c 'sleep 1; touch {marker}'; true"))
    await asyncio.sleep(0.3)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await asyncio.sleep(2.0)
    assert not marker.exists(), "the command outlived the interrupt"
```

> 这个测试在 Windows 上跳过，并且跳过时说出理由（F02-10）；在 Linux 上真正执行。
> 它不去查进程表，**它查那条命令有没有产生副作用**：让命令过 1 秒去创建一个标记文件，取消之后等 2 秒，标记文件不该出现。

### 11.1 这个测试写了三遍才测到东西

docstring 里记着前两遍，都是用同一个办法发现的：**把 `shell.py` 里那句 `_kill_group(proc)` 删掉，测试仍然是绿的。**

**第一遍**：`sleep 5; touch 标记`，取消后等 1 秒检查。活下来的命令要到第 5 秒才会创建标记，测试在第 1 秒多就看完了。它永远是绿的。

**第二遍**：改成 `sleep 1; touch 标记`，等 2 秒。**还是绿的。** 在 Linux 上把四种组合都跑了一遍：

```
direct      kill_group=True  marker written: False
direct      kill_group=False marker written: False
grandchild  kill_group=True  marker written: False
grandchild  kill_group=False marker written: True
```

`direct` 那两行说明：就算不杀进程组，标记也不会出现。原因是 `run()` 的 `finally` 会关掉和子进程之间的通道，这会顺带结束**那个 shell**；
shell 死了，就走不到 `touch`。真正逃掉的是 shell 启动的**子进程**（那个 `sleep`）——它成了孤儿，但它不写标记。

**第三遍**：让标记由"shell 的子进程"来写——`sh -c 'sleep 1; touch 标记'; true`（末尾的 `; true` 是为了让外层的 shell 不能偷懒直接变成里面那个）。
这次删掉 `_kill_group`，标记出现，测试变红。

> **一个"断言某件坏事没有发生"的测试，要先确认：不修的时候，那件坏事真的会发生。** 否则它测的只是"坏事碰巧没发生"。
> 而确认的办法只有一个：把修复拿掉，看它红不红。这两遍都是在 Linux 上做变异测试时才发现的——
> 这个测试在 Windows 上被跳过，所以在只有 Windows 的时候，它从来没有真正执行过一次。

```bash
git add src/minicodex/agent.py src/minicodex/shell.py tests/test_faults_ch07.py
git commit -m "fix(agent,shell): answer every issued call and kill the command when a turn is cancelled"
```

---

## §12 F07-05：告诉模型它被打断过

机械的部分都做完了：文件在，读得回来，断在哪里都能接上，进程也清理了。**然后是这一章真正的问题：恢复出来的那段历史，自己不知道自己被截断过。**

具体一点。§7 的规则丢掉了"发起了 `apply_patch`、但没有结果"的那一轮。恢复出来的历史是这样的：

```
S  你是一个编码 Agent……
U  在 client.py 里给 send() 加重试，然后跑 pytest
A  我先看一下这个文件。            [调用 read_file(client.py)]
T  (文件内容)
U  接着干
```

看起来完全正常。**而 `client.py` 现在可能已经被改过了**——那个 `apply_patch` 真的执行了，只是结果没来得及写下来。

模型会怎么做？这不能靠想。新建 `probe_resume.py`：

```python
"""Does the model need to be told it was interrupted?

The file-level questions in `probe_rollout.py` are settled by looking at bytes.
This one is not: after a crash, the recovered history is *silent* about the turn
that was dropped.  It reads as a conversation in which the last thing that
happened is the last thing on the page -- and the thing that was dropped is the
`apply_patch` that may or may not have already run.

The measurement: what does the model do first when told to carry on?

    verify   read the file / run a command to find out what the state is
    assume   patch (or re-patch) straight away

A: the recovered history, as chapter 1 would rebuild it, with nothing added.
B: the same history plus the system note from `interrupted_note(dropped)`.

Needs OPENAI_API_KEY.  Costs a few cents.

    uv run python probe_resume.py [samples]
"""

from __future__ import annotations

import json
import os
import sys
import time

import httpx

from minicodex.agent_types import ToolCall
from minicodex.history import History
from minicodex.model import OPENAI_BASE_URL
from minicodex.rollout import environment_note, interrupted_note
from minicodex.tools import TOOL_SCHEMAS

MODEL = "gpt-4o-mini"

TASK = (
    "In client.py, add a retry around the send() call, then run pytest and tell "
    "me whether it passes."
)

FILE = """\
import httpx

def send(payload):
    return httpx.post("https://example.invalid/v1", json=payload)
"""


def recovered_history(note: str | None) -> History:
    """What is on disk after a crash during `apply_patch`, loaded back.

    The assistant message that issued the patch call, and the call itself, are
    gone -- they were dropped because the call had no result.  The patch may
    nonetheless have been written: the tool ran, the process died before the
    result was recorded.
    """
    history = History()
    history.add_system_note(
        "You are a coding agent working in a user's repository. "
        "You can read files, run shell commands, and edit files with apply_patch."
    )
    history.add_user(TASK)
    history.add_assistant(
        "Let me look at the file first.",
        [
            ToolCall(
                "call_read",
                "read_file",
                {"path": "client.py"},
                '{"path":"client.py"}',
            )
        ],
    )
    history.add_tool_result("call_read", FILE)
    if note is not None:
        history.add_system_note(note)
    history.add_user("carry on")
    return history


def classify(calls: tuple[ToolCall, ...], text: str) -> str:
    if not calls:
        return "no-call"
    first = calls[0]
    if first.name in {"read_file"}:
        return "verify"
    if first.name == "run_shell":
        command = str((first.arguments or {}).get("command", ""))
        head = command.strip().split()[0] if command.strip() else ""
        if head in {"cat", "head", "less", "grep", "ls", "git", "sed", "tail", "type"}:
            return "verify"
        return "assume(run)"
    if first.name == "apply_patch":
        return "assume(patch)"
    return f"other({first.name})"


def one(history: History) -> str:
    """One request, non-streaming.

    Deliberately not `ChatCompletionsModel`: this probe is measuring the model,
    not the client, and the assembled-from-fragments path is already pinned by
    chapter 1's tests.  Fewer moving parts between the question and the answer.
    """
    payload = {
        "model": MODEL,
        "messages": history.to_wire(),
        "tools": list(TOOL_SCHEMAS),
    }
    # This machine drops roughly one TLS handshake in three.  Chapter 12 is
    # about doing this properly; here it is three lines so the measurement can
    # happen at all.
    for attempt in range(12):
        try:
            response = httpx.post(
                f"{OPENAI_BASE_URL}/chat/completions",
                headers={"Authorization": f"Bearer {os.environ['OPENAI_API_KEY']}"},
                json=payload,
                timeout=60,
            )
            break
        except httpx.ConnectError:
            if attempt == 11:
                raise
            time.sleep(2)
    response.raise_for_status()
    message = response.json()["choices"][0]["message"]
    calls = tuple(
        ToolCall(
            c["id"],
            c["function"]["name"],
            json.loads(c["function"]["arguments"] or "{}"),
            c["function"]["arguments"],
        )
        for c in message.get("tool_calls") or ()
    )
    return classify(calls, message.get("content") or "")


def ab(samples: int) -> None:
    arms = {
        "A no note": None,
        "B interrupted note": interrupted_note(2),
        # A note that is true, neutral, and carries no warning.  If this moves
        # the number as much as B does, then what B bought was not the
        # information -- it was the fact that a system message appeared at all,
        # and the wording in `interrupted_note` is decoration.
        "C placebo note": "This session was resumed from a file on disk.",
        # B's claim, minus the count.  Splits "something happened" from "two
        # specific messages are missing".
        "D vague warning": ("The previous session ended without finishing its last turn."),
    }
    for label, note in arms.items():
        results = [one(recovered_history(note)) for _ in range(samples)]
        tally: dict[str, int] = {}
        for r in results:
            tally[r] = tally.get(r, 0) + 1
        verified = sum(v for k, v in tally.items() if k == "verify")
        print(f"=== {label}")
        print(f"    {json.dumps(tally)}")
        print(f"    verified first: {verified}/{samples}")


def environment(samples: int) -> None:
    """F07-07: the session is resumed somewhere else.

    The history is full of relative paths that meant something in the old
    working directory.  Same question: does saying so change the first move?
    """
    from minicodex.rollout import SessionMeta

    note = environment_note(
        SessionMeta("old", cwd="/repo/service-a", model=MODEL, sandbox_mode="workspace-write"),
        SessionMeta("new", cwd="/repo/service-b", model=MODEL, sandbox_mode="workspace-write"),
    )
    assert note is not None
    for label, extra in {"A no note": None, "B environment note": note}.items():
        results = [one(recovered_history(extra)) for _ in range(samples)]
        tally: dict[str, int] = {}
        for r in results:
            tally[r] = tally.get(r, 0) + 1
        print(f"=== env {label}")
        print(f"    {json.dumps(tally)}")


if __name__ == "__main__":
    count = int(sys.argv[1]) if len(sys.argv) > 1 else 5
    ab(count)
    environment(count)
```

> - **`recovered_history(note)`**：造出上面那段"恢复出来的历史"，可以选择在"接着干"之前加一条系统消息 `note`。
> - **`classify(calls, text)`**：给模型的**第一个动作**分类——先去看（读文件，或者 `cat`/`ls`/`git` 这类只读命令）算 `verify`；
>   直接调用 `apply_patch` 算 `assume(patch)`；直接跑别的命令算 `assume(run)`。
> - **`one(history)`**：发一次请求，返回分类。它故意不用我们自己的模型客户端：这个探针量的是模型，不是客户端，中间的环节越少越好。
>   连接失败会重试几次（探针原作者的机器网络不稳）。
> - **`ab(samples)`**：四组，每组发 `samples` 次。**`environment(samples)`**：§12.3 用。

### 12.1 四组

- **A**：什么都不加。
- **B**：加上我们最后采用的那句话（`interrupted_note(2)`）。
- **C**：加一句**真实、中性、不含任何警告**的话："This session was resumed from a file on disk."——**安慰剂**。
- **D**：只有一句话，不带数量："The previous session ended without finishing its last turn."

```
2026-10-01，每组 10 次
=== A no note
    {"assume(patch)": 10}
    verified first: 0/10
=== B interrupted note
    {"verify": 9, "assume(patch)": 1}
    verified first: 9/10
=== C placebo note
    {"assume(patch)": 9, "assume(run)": 1}
    verified first: 0/10
=== D vague warning
    {"verify": 9, "assume(patch)": 1}
    verified first: 9/10
```

**A：0/10。** 十次全部直接改文件，没有一次先看一眼。**F07-05 成立。**

**C：0/10。** 这一组是整张表的地基。一条无关紧要、但出现在系统消息位置上的话，会不会同样让模型变谨慎？不会。
**它证明了起作用的是"警告"的内容，而不是"多了一条系统消息"这件事。** 没有这一组，B 的 9/10 只能说明"加点什么有用"——
而"加点什么有用"是所有提示词玄学的开始。

> **通用做法**：任何"我加了一段话，指标变好了"的结论，都要有一个**内容无关、形式相同**的对照组。否则你量的是"多一段话"，不是"这段话"。

### 12.2 写得更全的版本，输给了一句话

这句话的第一版其实是四句（原始记录，2026-08）：

```
The previous session ended without finishing its last turn, and 2 message(s)
were discarded because they were incomplete. Whatever was happening at that
moment did not necessarily complete. Check the state of anything you believe
you changed before continuing.
```

两次测量合在一起：

| 组 | 内容 | 2026-08 | 2026-10 |
|---|---|---|---|
| A | 什么都不加 | 0/13 | 0/10 |
| C | "已从磁盘上的文件恢复"（安慰剂） | 0/5 | 0/10 |
| — | 四句话：数量 + 两句建议 | 10/19 | （没有重测） |
| D | 一句话，不带数量 | 10/11 | 9/10 |
| B | 一句话 + 数量（现在用的） | 11/11 | 9/10 |

四句话的版本只有 10/19，输给了一句话的版本。多出来的两句是**建议**——而"改之前先看看"这件事模型本来就会，不需要教。
它不可能知道的只有第一句：**有一轮不见了**。

> 这和第 3 章的结论看起来矛盾，其实不是。第 3 章说错误消息要写三段（出了什么错、你发了什么、下一步做什么），因为那时模型**不知道该往哪里改**。
> 这里模型什么都会，它只是**不知道发生过一件事**。
>
> **缺信息，和缺行动的方向，要用不同的东西来补。** 往"缺信息"的地方塞行动建议，等于把新闻埋进一段它已经知道的话里。

最后用的就是 B：

```python
def interrupted_note(dropped: int | None = None) -> str:
    """What the model is told about the turn that never finished.

    Without it the transcript reads as if the last thing in it simply happened.
    Measured against gpt-4o-mini on a history whose `apply_patch` was the turn
    that vanished, asking what it does first when told to carry on
    (`probe_resume.py`, "verify" = reads the file before touching it):

                                                  2026-08   2026-10
        no note at all                              0/13      0/10
        "resumed from a file on disk" (placebo)      0/5      0/10
        this text, first version (4 sentences)      10/19       --
        one sentence, no count                      10/11      9/10
        one sentence + the count  <- shipped        11/11      9/10

    The placebo arm is what makes the rest mean anything: a system note
    appearing is not the mechanism, the warning is.  And the first version --
    longer, more careful, with two sentences of advice on the end -- did worse
    than the plain sentence, twice, on independent runs.  The advice is the
    part the model already knows; the news is the part it cannot know.

    Two wordings for two situations.  Live interruption: the tools were
    answered and the answers say they were cut off, so there is no count to
    give.  Recovery from disk: whole messages are missing.
    """
    if dropped is None:
        # Not measured -- the probe reconstructs the from-disk case.  Kept to
        # the same shape as the measured one rather than written afresh.
        return (
            "You were interrupted by the user during the previous turn. Any tool "
            "call answered with 'interrupted' may have run partially, or not at all."
        )
    return (
        "The previous session ended without finishing its last turn. "
        f"{dropped} message(s) were discarded because they were incomplete."
    )
```

> - 那张表写在 docstring 里，而不是写在提交信息里：**提交信息只有翻旧账时才有人读；docstring 是改这句话的人一定会看到的地方。**
> - 两种情况两句话。从磁盘恢复：有整条的消息不见了，给出数量。运行中被 Ctrl-C 打断（§10 用的 `interrupted_note()`，不带参数）：
>   工具调用都得到了回答，回答里说了"被打断"，没有数量可给。**后一句没有被测量过**，代码里的注释如实写了这一点——
>   一段测量过的文字旁边的、没测量过的文字，会白白沾上前者的可信度。

```python
async def test_F07_05_the_interrupted_run_leaves_a_note(tmp_path: Path) -> None:
    model = ScriptedModel([[delta("call_1", 0, "slow")], "done"])
    started = asyncio.Event()

    async def slow(args: dict[str, Any]) -> str:
        started.set()
        await asyncio.sleep(60)
        return "never"

    agent = Agent(model, {"slow": slow})
    task = asyncio.ensure_future(agent.run("go"))
    await started.wait()
    task.cancel()
    result = await task

    notes = [i.text for i in result.history.items if isinstance(i, SystemNote)]
    assert any("interrupted by the user" in n for n in notes)


def test_F07_05_recovery_states_how_much_was_lost() -> None:
    """The count is the difference between "ignore this" and "redo this"."""
    assert "2 message(s)" in interrupted_note(2)
    assert interrupted_note() != interrupted_note(0)


async def test_F07_05_a_resumed_run_carries_the_note_into_the_request(
    tmp_path: Path,
) -> None:
    """The note has to reach the model, not just the history object."""
    path = tmp_path / "s.jsonl"
    with RolloutWriter(path, meta()) as writer:
        history = History(observer=writer.append)
        history.add_user("go")
        history.add_assistant("running", [call("call_1")])

    restored, dropped = read_rollout(path).history()
    restored.add_system_note(interrupted_note(dropped))

    model = ScriptedModel(["ok"])
    agent = Agent(model, {}, resume_from=restored)
    await agent.run("carry on")

    sent = model.sent[0]
    assert any("were discarded" in m["content"] for m in sent if m["role"] == "system")
    assert sent[-1] == {"role": "user", "content": "carry on"}


def test_F07_05_the_wording_is_pinned() -> None:
    """The note was measured; an edit to it is a change to a measured thing.

    Chapter 3 learned this the expensive way (F03-10): one word changed in a
    description flipped both providers 3/3 to 0/3, and nothing noticed.  Here
    the wording moved a model from 0/13 to 11/11 (and, measured again two months
    later, from 0/10 to 9/10).  A snapshot does not stop
    anyone editing it -- it stops them editing it *by accident*, and it puts
    the number they have to beat in the failure message.
    """
    assert interrupted_note(2) == (
        "The previous session ended without finishing its last turn. "
        "2 message(s) were discarded because they were incomplete."
    ), "measured at 11/11 and again at 9/10 (probe_resume.py); re-measure before changing it"
```

> - 被打断的运行，历史里留下了一条说明；
> - 带数量的说明里真的有数量，而且和不带参数的那句不一样；
> - **这条说明真的到了发给模型的请求里**，而不只是存在于 `History` 对象里；
> - **措辞被钉住**。它拦不住任何人改这句话——它拦的是**顺手**改。失败信息里带着测量结果和探针的名字，想改的人会看到自己要胜过的是什么。

### 12.3 F07-07：环境变了

恢复时，模型可能换了，目录可能换了，沙箱模式可能换了。这就是 §4 那行 meta 存在的理由：比一比，把不一样的说出来。

```python
def environment_note(meta: SessionMeta, current: SessionMeta) -> str | None:
    """One sentence per thing that is no longer what the history assumes.

    Returns None when nothing moved.  The model is told rather than protected:
    the history is full of paths relative to a directory that may not be this
    one, and of files it read under a permission set it may no longer have.
    Neither of those is recoverable by this program, and both are obvious to the
    model the moment it is told.
    """
    fields = [
        ("working directory", meta.cwd, current.cwd),
        ("model", meta.model, current.model),
        ("sandbox mode", meta.sandbox_mode, current.sandbox_mode),
        ("approval policy", meta.approval_policy, current.approval_policy),
    ]
    changed = [(label, was, now) for label, was, now in fields if was and now and was != now]
    if not changed:
        return None
    lines = [f"- {label}: was {was!r}, now {now!r}" for label, was, now in changed]
    return (
        "This session was resumed and the environment is not the one it was "
        "recorded in:\n"
        + "\n".join(lines)
        + "\nRe-check anything above that your earlier steps depended on before "
        "relying on it."
    )
```

真实的输出：

```
This session was resumed and the environment is not the one it was recorded in:
- working directory: was '/repo/x', now '/repo/y'
- model: was 'gemma4:31b-cloud', now 'gpt-4o-mini'
- sandbox mode: was 'read-only', now 'workspace-write'
Re-check anything above that your earlier steps depended on before relying on it.
```

三个细节：

- **没变，就一个字都不说**（返回 `None`）。每次恢复都来一段的话，模型会学会跳过它，而且它之后每一轮都要占地方。
- **`if was and now`**：旧文件里没记沙箱模式，不等于沙箱模式变了。空值的意思是"不知道"，不是"不一样"。
- **只告诉，不阻止。** 历史里全是相对路径，换了目录后它们可能指向别的文件。这个程序没能力把它们改对——但模型一被告知就明白了。

探针的第二部分顺便量了它（2026-10-01，各 10 次）：

```
=== env A no note
    {"assume(patch)": 10}
=== env B environment note
    {"verify": 8, "assume(patch)": 2}
```

0/10 → 8/10。

> **一个如实的说明**：这次测量**没能分清**"环境说明传达了具体的信息"和"环境说明让模型整体变谨慎了"。
> 它的效果和 §12.1 的 D 组差不多，而 D 组说的完全是另一件事。§12.1 的安慰剂组只管 §12.1。
> **这一条记为：效果测了，原因没测。**

```python
def test_F07_07_a_changed_environment_is_reported() -> None:
    before = meta(cwd="/repo/a", model="gpt-4o-mini", sandbox_mode="read-only")
    after = meta("s2", cwd="/repo/b", model="gpt-4o-mini", sandbox_mode="workspace-write")
    note = environment_note(before, after)
    assert note is not None
    assert "working directory" in note and "/repo/b" in note
    assert "sandbox mode" in note
    assert "model" not in note  # unchanged fields are not mentioned


def test_F07_07_an_unchanged_environment_says_nothing() -> None:
    """Silence when nothing moved.

    A note on every resume is a note the model learns to skip, and it costs
    tokens on every turn afterwards.
    """
    before = meta(cwd="/repo", model="m", sandbox_mode="read-only")
    assert (
        environment_note(before, meta("s2", cwd="/repo", model="m", sandbox_mode="read-only"))
        is None
    )


def test_F07_07_unknown_fields_are_not_reported_as_changes() -> None:
    """An old file with no `sandbox_mode` recorded is not a sandbox_mode change."""
    before = meta(cwd="/repo")
    after = meta("s2", cwd="/repo", sandbox_mode="workspace-write")
    assert environment_note(before, after) is None
```

```bash
git add src/minicodex/rollout.py tests/test_faults_ch07.py probe_resume.py
git commit -m "feat(rollout): tell the model about the turn it cannot see, and about a changed environment"
```

---

## §13 F07-09：格式的版本，和一次真的迁移

"格式升级后旧文件读不了"的标准解法是"版本号 + 迁移函数"。版本号第一天就可以有。**迁移函数却写不出来——直到你真的需要迁移一次。**

这一章里就需要了一次。`_dump_item` 的第一版，工具调用只存了三样：

```python
{"call_id": c.call_id, "name": c.name, "arguments": c.arguments}
```

`arguments` 是解析之后的字典。第 1 章的 `ToolCall` 还有一个字段 `raw_arguments`：模型原样发来的那个字符串。
存字典、不存字符串，看起来完全等价——都是 JSON 嘛。不等价：

```
model sent:       {"path":"client.py"}
re-encoded:       {"path": "client.py"}
equal: False
```

冒号后面多了一个空格。

这有什么关系？第 1 章留着 `raw_arguments`，就是为了历史能**一个字节不差地**重新发出去。字节变了，会影响两件实事：
服务商按请求的**开头部分**做缓存，字节一变缓存就失效；而插曲 A 那种"整轮对话逐字节对比"的测试，恢复之后就对不上了。

**这个 bug 可怕的地方在于：第一版写完时，所有测试都是绿的。** 两边都能被 `json.loads` 解析，含义完全一样。它只在"有人比较字节"的那一天才出现。

于是：加上 `raw_arguments`，版本号从 1 变成 2，写迁移函数：

```python
def _migrate(record: dict[str, Any], version: int) -> dict[str, Any]:
    """Bring one record forward to `ROLLOUT_VERSION`.

    A migration per version step, applied in order, each one small enough to
    read.  The alternative -- refusing to open old files -- is the version of
    F07-09 that makes the user's problem worse: their session is not corrupt,
    it is merely older than the program.
    """
    if version < 2 and record.get("type") == "assistant":
        record = dict(record)
        record["tool_calls"] = [
            # Version 1 had no `raw_arguments`.  Re-encoding the parsed object is
            # the best available reconstruction and is *not* the original string:
            # key order and whitespace are this program's, not the model's.  It
            # is written down here rather than pretended away.
            {**c, "raw_arguments": c.get("raw_arguments") or json.dumps(c.get("arguments") or {})}
            for c in record.get("tool_calls", ())
        ]
    return record
```

> - 版本小于 2 的助手消息：给每个工具调用补一个 `raw_arguments`——有就用原来的，没有就把解析后的参数重新变成字符串。
>   `{**c, "raw_arguments": ...}`：复制 `c`，加上（或覆盖）这一个键。`record = dict(record)`：先复制一份再改，不动传进来的那个。
> - **注释是这段代码里最重要的部分：迁移出来的不是原来的字节，而是一个"尽力而为的重建"。** 旧文件里那份信息是真的丢了，回不来。
>   迁移能做的是让文件读得了，不是让它变回原样。

> **"拒绝打开旧文件"为什么是坏的修法**：用户的会话没有坏，它只是比程序旧。一个升级之后打不开自己昨天的会话的工具，教给用户的是"别升级"。

```python
def test_F07_09_a_version_1_file_still_loads(tmp_path: Path) -> None:
    """Version 1 had no `raw_arguments`.  Refusing to open it is the bad fix."""
    path = tmp_path / "old.jsonl"
    path.write_text(
        "\n".join(
            [
                json.dumps({"type": "meta", "session_id": "old", "version": 1}),
                json.dumps({"type": "user", "text": "go"}),
                json.dumps(
                    {
                        "type": "assistant",
                        "text": "reading",
                        "tool_calls": [
                            {
                                "call_id": "call_1",
                                "name": "read_file",
                                "arguments": {"path": "x.py"},
                            }
                        ],
                    }
                ),
                json.dumps(
                    {
                        "type": "tool_result",
                        "call_id": "call_1",
                        "name": "read_file",
                        "content": "ok",
                    }
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    loaded = read_rollout(path)
    assert loaded.truncated_at is None
    restored, dropped = loaded.history()
    assert dropped == 0
    wire = restored.to_wire()
    assert wire[1]["tool_calls"][0]["function"]["arguments"] == '{"path": "x.py"}'


def test_F07_09_a_version_2_file_keeps_the_bytes_the_model_sent(tmp_path: Path) -> None:
    """The reason the version was bumped, stated as a test.

    A model that sent `{"path":"x.py"}` gets that string back, not this
    program's idea of how to spell it.  Version 1 could not do this, and the
    difference is invisible until something downstream compares bytes.
    """
    path = tmp_path / "new.jsonl"
    raw = '{"path":"x.py"}'
    with RolloutWriter(path, meta()) as writer:
        history = History(observer=writer.append)
        history.add_user("go")
        history.add_assistant("reading", [ToolCall("call_1", "read_file", {"path": "x.py"}, raw)])
        history.add_tool_result("call_1", "ok")

    restored, _ = read_rollout(path).history()
    assert restored.to_wire()[1]["tool_calls"][0]["function"]["arguments"] == raw


def test_F07_09_new_files_carry_the_current_version(tmp_path: Path) -> None:
    path = tmp_path / "s.jsonl"
    RolloutWriter(path, meta()).release()
    header = json.loads(path.read_text(encoding="utf-8").splitlines()[0])
    assert header["version"] == ROLLOUT_VERSION
```

> 手写一个版本 1 的文件：读得出来，参数被重建成 `{"path": "x.py"}`（带空格——重建，不是原样）；版本 2 的文件：模型发的是 `{"path":"x.py"}`，
> 读回来还是它；新文件的开头写着当前的版本号。

---

## §14 F07-10：分出一条新线——复制，不是引用

场景：跑到第 12 轮发现方向错了，想回到第 6 轮换个思路，又不想丢掉第 12 轮的那条线。

诱人的实现是**记一个位置**：新会话指向同一个文件，再记一句"我从第 6 条开始"。省磁盘，听起来很聪明。

它错在：两条线接下来都要往文件末尾追加。**这就是 §9 的"两个写入者"，只不过这次是我们自己安排的。**

所以：复制。

```python
def fork(source: Path, *, upto: int | None = None, directory: Path | None = None) -> Path:
    """Start a new session from a prefix of an old one.

    Copies records into a new file.  Not "point the new session at the old file
    and remember an offset": two sessions sharing a file is the two-writer
    configuration again, and the second one's turns would appear in the first
    one's replay.  Copying a conversation costs kilobytes.
    """
    original = read_rollout(source)
    directory = Path(directory) if directory is not None else Path(source).parent
    kept = original.items if upto is None else original.items[:upto]

    meta = replace(
        original.meta,
        session_id=new_session_id(),
        version=ROLLOUT_VERSION,
        created=time.time(),
        forked_from=original.meta.session_id,
        forked_at=len(kept),
    )
    target = directory / f"{meta.session_id}.jsonl"
    with RolloutWriter(target, meta) as writer:
        writer.extend(kept)
    return target
```

> - 读出原来的会话，取前 `upto` 条（不给就取全部）；
> - `replace(original.meta, ...)`：复制一份环境信息，换上新的会话 id 和创建时间，并记下"从哪个会话的第几条分出来的"；
> - 写进一个新文件。

一段对话几十 KB。为了省这几十 KB 去共用一个会变的文件末尾，不值得。

`--upto N` 可能正好切在一轮中间。`fork` 不需要操心这件事——§7 的加载规则会把不完整的尾巴丢掉。
**一条规则，两个用处**：这是把规则放在 `History` 里、而不是放在"恢复的代码"里得到的回报——`fork` 完全不需要知道什么叫"一轮"。

```python
def test_F07_10_a_fork_copies_rather_than_references(tmp_path: Path) -> None:
    source = tmp_path / "a.jsonl"
    with RolloutWriter(source, meta("parent")) as writer:
        history = History(observer=writer.append)
        for item in three_turn_history().items:
            _replay(history, item)

    forked = fork(source, upto=2, directory=tmp_path)

    # The parent keeps going after the fork...
    with RolloutWriter(source, meta("parent")) as writer:
        writer.append(UserMessage("parent carries on"))

    child = read_rollout(forked)
    assert len(child.items) == 2
    assert all("parent carries on" != getattr(i, "text", None) for i in child.items)
    assert child.meta.forked_from == "parent"
    assert child.meta.forked_at == 2

    # ...and the child can be written to without touching the parent.
    with RolloutWriter(forked, child.meta) as writer:
        writer.append(UserMessage("child goes elsewhere"))
    parent_texts = [getattr(i, "text", "") for i in read_rollout(source).items]
    assert "child goes elsewhere" not in parent_texts


def test_F07_10_forking_a_whole_session_keeps_all_of_it(tmp_path: Path) -> None:
    source = tmp_path / "a.jsonl"
    with RolloutWriter(source, meta("parent")) as writer:
        writer.extend(three_turn_history().items)
    forked = read_rollout(fork(source, directory=tmp_path))
    assert len(forked.items) == 6
    assert forked.meta.session_id != "parent"


def test_F07_10_a_fork_that_cuts_mid_turn_is_still_loadable(tmp_path: Path) -> None:
    """`--upto 3` can land between a call and its result.

    The same rule that recovers a crashed session covers this: the loader drops
    the unfinished tail rather than the fork having to know about turns.
    """
    source = tmp_path / "a.jsonl"
    with RolloutWriter(source, meta("parent")) as writer:
        writer.extend(three_turn_history().items)

    forked = read_rollout(fork(source, upto=3, directory=tmp_path))
    assert len(forked.items) == 3
    restored, dropped = forked.history()
    assert dropped == 1
    assert restored.to_wire()  # sendable
```

> 分出来之后，原来的会话继续写，新会话里看不到；新会话继续写，原来的会话里也看不到；整个复制时一条不少；切在一轮中间的，读回来丢掉 1 条，仍然能发送。

---

## §15 和第 6 章接上：压缩之后怎么写

第 6 章的结论是"摘要是有损的，所以写到磁盘上的必须是原文"。现在要兑现它，而它和"只追加"有冲突：
压缩发生时，内存里的历史**被换掉了**——前面二十轮变成了一份摘要。一个只能追加的文件表达不了"换掉"。

三个选择：

1. **重写整个文件。** 放弃"只追加"，于是"最坏只是缺一段尾巴"这个性质没了——重写到一半被杀，文件就是半截的。
2. **不写摘要，恢复时重放原文。** 很干净——但恢复出来的会话立刻又是那个撑爆窗口的大小，**压缩等于没做过**，下一轮它会再压一次。
3. **写标记，把新的历史追加在后面。** 读的时候，只取标记划出来的那一段。

选 3。原文还在文件里，只是加载时不参与重放——**同一个文件，人读它是记录，程序读它是状态。**

### 15.1 意外：标记写早了

第一版是这样的——先写一个标记，再把新的历史一条条追加上去：

```python
self.rollout.mark("compacted", generation=result.generation, replaced=result.plan.drops)
return self._attach(result.history), result
```

读的时候，遇到这个标记就把前面读到的全部清空，从标记后面重新开始。

改写这一章时，把文件格式从头看了一遍，对每一个位置问同一个问题：**"如果文件正好在这里结束呢？"**
前面每个位置的答案都是"丢掉不完整的尾巴，剩下的是一段真实的对话"。只有一个位置不是：**标记已经写下、新的历史还没写完的时候。**

构造这样一个文件（八条历史，然后是标记，然后只有新历史的第一条），用当时的代码去读，真实的输出：

```
items in the file before the marker: 8
items the loader returns:            1
dropped as incomplete:               0
what would be sent: [{'role': 'system', 'content': 'You are a coding agent.'}]
```

**八条变一条，用户的问题没了，而且 `dropped` 是 0**——所以连 §12 那句"上次没有完成"的说明都不会加。
标记说的是"我前面的全都不要了"，而它后面的替代品只写了一行。

这个窗口很窄（写几条记录的时间），按 §6 的测量，进程被杀时多半停在工具里，而不是这里。但这一章的全部主张就是
"文件从任何位置断掉都能读回来"，而这是唯一一个做不到的位置——而且它的后果不是"丢了最后一轮"，是**丢了整个会话**。

毛病出在顺序：**一个"作废前面所有内容"的标记，写在了它的替代品之前。** 这正是 §3 那条规则——先写下来，再往下做——被违反的地方。

修法是两个标记，把新的历史夹在中间：

```python
    def _rebaseline(self, result: CompactionResult) -> History:
        """Write a compacted history to the rollout as the new baseline.

        The rollout is append-only, so a history that has been *replaced*
        cannot be expressed by editing what is already on disk.  It is
        expressed by two markers with the new baseline between them, and
        `read_rollout` keeps what sits between the last finished pair.  The old
        turns stay in the file, unread by the loader and available to anyone
        reading it as a record.

        Two markers, not one.  The first version wrote a single "compacted"
        marker *ahead* of the baseline, which made this the one place in the
        chapter where a prefix of the file was not a recoverable session: a
        kill after the marker and before the last baseline item left a file
        that said "forget everything before me" in front of half a
        replacement.  It loaded as one system note, the user's question gone,
        and nothing reported as dropped.
        """
        payload = {"generation": result.generation, "replaced": result.plan.drops}
        self.rollout.mark("compacting", **payload)
        attached = self._attach(result.history)
        self.rollout.mark("compacted", **payload)
        return attached

    def _attach(self, history: History) -> History:
        """Re-point a history at the rollout, writing it out as it goes.

        `compact()` builds a plain `History` -- it has no business knowing about
        files -- so the agent hands the new one the observer and replays it.
        """
        rebuilt = History(observer=self.rollout.append)
        for item in history.items:
            replay(rebuilt, item)
        return rebuilt
```

`_maybe_compact` 的最后一行相应地变成 `return self._rebaseline(result), result`。

> - **`_attach(history)`**：`compact()` 返回的是一个普通的 `History`（它不该知道文件的事），所以由 Agent 新建一个带着"写文件"观察者的 `History`，
>   把内容重放进去——重放的过程，就把这些记录写进了文件。`replay` 是 `rollout.py` 里一个一行的函数，就是 `_add`。
> - **`_rebaseline(result)`**：先写 `compacting`（"我要开始写替代品了"），写完替代品，再写 `compacted`（"替代品写完了"）。

读的那一边，是 §7 的 `read_rollout` 里当时跳过的那一段：

```python
                kind = record.get("mark")
                if kind == "compacting":
                    baseline_from = len(items)
                elif kind == "compacted":
                    if baseline_from is None:
                        items.clear()
                    else:
                        del items[:baseline_from]
                        baseline_from = None
```

以及循环结束之后：

```python
    if baseline_from is not None:
        del items[baseline_from:]
```

> - 读到 `compacting`：记下"替代品从第几条开始"（`baseline_from`），**什么都不丢**。
> - 读到 `compacted`：替代品齐了，这时才把它**前面**的丢掉（`del items[:n]`：删掉前 n 条）。
> - 读到文件末尾，`compacting` 还没等到它的 `compacted`：替代品没写完——**丢掉的是这半截替代品**（`del items[n:]`：删掉第 n 条以后的），
>   它要替换的那些原文还在，一条不少。会话恢复成压缩之前的样子，接下来会再压缩一次：多花一次模型调用。另一种后果是丢掉整个对话。
> - `baseline_from is None` 时遇到 `compacted`：那是用第一版代码写出来的旧文件（只有一个标记，在替代品前面），按它当初写下的方式读。
>   **这是 §13 的同一件事**：格式变了，旧文件不能读不了。

```python
def test_compaction_writes_a_new_baseline(tmp_path: Path) -> None:
    """A compacted session resumes at its compacted size.

    The file is append-only, so a replaced history is expressed by two markers
    with the new baseline between them.  Without them, resuming replays the
    turns compaction removed and the session comes back at the size that made
    it compact.
    """
    path = tmp_path / "s.jsonl"
    with RolloutWriter(path, meta()) as writer:
        history = History(observer=writer.append)
        history.add_user("go")
        history.add_assistant("a", [call("call_1")])
        history.add_tool_result("call_1", "x" * 100)
        writer.mark("compacting", generation=1, replaced=2)
        writer.append(UserMessage("go"))
        writer.append(SystemNote("[compacted transcript | generation 1]"))
        writer.mark("compacted", generation=1, replaced=2)

    loaded = read_rollout(path)
    assert [type(i).__name__ for i in loaded.items] == ["UserMessage", "SystemNote"]
    assert [m["mark"] for m in loaded.marks] == ["compacting", "compacted"]


def test_a_kill_inside_the_new_baseline_keeps_what_it_was_replacing(tmp_path: Path) -> None:
    """Found by reading the file format as "what if it ends *here*", not by a crash.

    The first version wrote one "compacted" marker and then the baseline.  A
    kill between the marker and the last baseline item left a file whose marker
    said "forget everything before me" in front of half a replacement: it
    loaded as one system note, the user's question was gone, and `dropped` was
    0 -- so not even the "incomplete messages were discarded" note was added.
    Every other position in the file recovered; this one lost the session.

    With two markers the unfinished replacement is the part that is thrown
    away, and the session comes back un-compacted.  It will compact again,
    which costs a model call.  The alternative cost the conversation.
    """
    path = tmp_path / "s.jsonl"
    with RolloutWriter(path, meta()) as writer:
        history = History(observer=writer.append)
        for item in three_turn_history().items:
            _replay(history, item)
        writer.mark("compacting", generation=1, replaced=4)
        writer.append(SystemNote("You are a coding agent."))
        # ...and here the process dies, before the rest of the baseline.

    loaded = read_rollout(path)
    restored, dropped = loaded.history()
    assert dropped == 0
    assert restored.to_wire() == three_turn_history().to_wire()


def test_a_file_with_the_old_single_marker_still_loads(tmp_path: Path) -> None:
    """F07-09 again, one layer up: the marker scheme changed, old files did not.

    A lone "compacted" with no "compacting" before it is a file written by the
    first version of this chapter, where the marker came *ahead* of the
    baseline.  It is read the way it was written.
    """
    path = tmp_path / "s.jsonl"
    with RolloutWriter(path, meta()) as writer:
        history = History(observer=writer.append)
        history.add_user("go")
        history.add_assistant("a", [call("call_1")])
        history.add_tool_result("call_1", "x" * 100)
        writer.mark("compacted", generation=1, replaced=2)
        writer.append(UserMessage("go"))
        writer.append(SystemNote("[compacted transcript | generation 1]"))

    assert [type(i).__name__ for i in read_rollout(path).items] == ["UserMessage", "SystemNote"]


async def test_an_agent_that_compacts_can_be_resumed_from_its_file(tmp_path: Path) -> None:
    """The whole seam, end to end: what is on disk is what was in memory.

    The two tests above write the markers by hand.  This one lets the agent
    write them, so swapping the order back -- marker first, baseline second --
    turns it red.
    """
    path = tmp_path / "s.jsonl"
    model = ScriptedModel(
        [[delta(f"call_{n}", 0, "big")] for n in range(3)] + ["done"],
    )

    async def big(args: dict[str, Any]) -> str:
        return "o" * 4000

    async def summarise(request: Any) -> str:
        return "## Done\nsome work\n"

    with RolloutWriter(path, meta()) as writer:
        agent = Agent(
            model,
            {"big": big},
            rollout=writer,
            context_window=1500,
            summariser=summarise,
            max_turns=6,
        )
        result = await agent.run("go")

    assert result.compactions, "the window was small enough that this must compact"
    loaded = read_rollout(path)
    kinds = [m["mark"] for m in loaded.marks]
    assert kinds == ["compacting", "compacted"] * len(result.compactions)
    restored, dropped = loaded.history()
    assert dropped == 0
    assert restored.to_wire() == result.history.to_wire()
```

> - 两个标记之间的两条，就是读回来的全部；
> - **替代品写到一半文件就结束了：读回来的是原来那段完整的历史**，`dropped` 是 0；
> - 只有一个旧式标记的文件，照旧能读；
> - **最后一个让 Agent 自己去压缩、自己去写**（窗口 1500，工具每次返回 4000 个字符），然后断言：标记成对出现，
>   而且从文件读回来的历史，变成发送格式之后和内存里的**完全一样**。前三个测试的标记是手写的；把 Agent 里两个标记的顺序改回去，红的是这一个。

> 这条不是崩溃测出来的，也不是模型测出来的，是**把格式逐个位置读一遍**读出来的。
> §6 的探针杀了 20 次进程，一次都没落在这个窗口里——**测量告诉你常见的情况长什么样，它不会替你检查少见的情况。**

```bash
git add src/minicodex/rollout.py src/minicodex/agent.py tests/test_faults_ch07.py
git commit -m "feat(rollout): versions and migration, fork by copy, and a compaction baseline that survives a kill"
```

---

## §16 接到 Agent 和命令行上

### 16.1 `rollout.py` 剩下的几个小函数，和整个文件

```python
def list_sessions(directory: Path = DEFAULT_DIR) -> list[Rollout]:
    """Every readable session, newest first.  Unreadable ones are skipped, not raised."""
    directory = Path(directory)
    if not directory.is_dir():
        return []
    out = []
    for path in sorted(directory.glob("*.jsonl"), reverse=True):
        try:
            out.append(read_rollout(path))
        except RolloutError:
            continue
    return out


def replay(history: History, item: HistoryItem) -> None:
    """Put one stored item back into a live history, through the front door."""
    _add(history, item)


def rollout_path(session_id: str, directory: Path = DEFAULT_DIR) -> Path:
    return Path(directory) / f"{session_id}.jsonl"


def resolve(reference: str, directory: Path = DEFAULT_DIR) -> Path:
    """Accept a path, a session id, or `last`."""
    if reference == "last":
        sessions = list_sessions(directory)
        if not sessions:
            raise RolloutError(f"no sessions in {directory}")
        assert sessions[0].path is not None
        return sessions[0].path
    candidate = Path(reference)
    if candidate.exists():
        return candidate
    candidate = rollout_path(reference, directory)
    if candidate.exists():
        return candidate
    raise RolloutError(f"no session {reference!r} (looked in {directory})")
```

> - **`list_sessions(directory)`**：目录下所有 `.jsonl` 文件，按文件名倒序（文件名以时间开头，所以就是从新到旧）。读不了的文件跳过，不报错——
>   一个坏文件不该让整个列表出不来。
> - **`replay(history, item)`**：给别的模块用的名字，就是 `_add`。
> - **`rollout_path(session_id, directory)`**：会话 id → 文件路径。
> - **`resolve(reference, directory)`**：`--resume` 后面可以写三种东西——`last`（最新的一个）、一个文件路径、或者一个会话 id。都找不到就抛 `RolloutError`。

整个 `rollout.py`：

```python
"""The session on disk: what was said, in the order it was said.

Chapter 0's recorder already writes a transcript, and it is not this.  The
recorder writes *requests and responses* -- what crossed the model boundary,
for a human reading it afterwards.  This writes *history items* -- the facts
chapter 1 keeps in memory -- in a shape that can be loaded back into a
`History` and continued.  The two files answer different questions ("what did
the model receive" versus "where were we"), and merging them would mean the
recovery path depends on a debugging aid nobody promised to keep stable.

Three properties, each of them the answer to something measured:

* **Append-only.**  A file rewritten from scratch on every turn is a file that
  can be lost on any turn.  Appending means the worst case is a missing tail,
  and a missing tail is recoverable.

* **One JSON document per line.**  A reader that stops at the first line it
  cannot parse still has everything before it.  This is cheap insurance rather
  than a measured need: five attempts to tear a line by killing the writer
  mid-write produced zero torn lines (see `probe_rollout.py`), so the guard is
  written and honestly labelled unmeasured.

* **One writer.**  Two processes appending to one file is the configuration
  that *did* corrupt it in measurement (`probe_rollout.py`): two processes
  appending 4,000 records each.  On Windows about a third of the records
  were overwritten and gone; on Linux none were lost.  On both, the file
  ended up holding two different sessions' records interleaved.  Refused
  with a lock file rather than tolerated.

`History` is not asked to load itself.  It is rebuilt through its own
`add_*` methods, so a rollout that ends in the middle of a turn cannot become
an invalid conversation: the same invariant that refuses to build one in
memory refuses to load one from disk.
"""

from __future__ import annotations

import json
import os
import time
from collections.abc import Iterable
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from minicodex.agent_types import ToolCall
from minicodex.history import (
    AssistantMessage,
    History,
    HistoryError,
    HistoryItem,
    SystemNote,
    ToolResult,
    UserMessage,
)

DEFAULT_DIR = Path(".minicodex") / "sessions"

# Bumped once already, during this chapter, and the bump is the point: version 1
# stored a tool call's parsed `arguments` and not the `raw_arguments` string the
# model actually sent.  Re-rendering a resumed history therefore produced
# different bytes from the original -- valid JSON, same meaning, different
# string -- which chapter 1 keeps `raw_arguments` around precisely to avoid.
# The old files are still readable; see `_migrate`.
ROLLOUT_VERSION = 2


# One message for one condition.  It was two for an afternoon -- "first record
# is not a session header" from inside the loop and "no session header" from
# after it -- which is two things to grep for and two things to keep in step.
_NO_HEADER = "{path}: no session header; not a rollout file"


class RolloutError(RuntimeError):
    """The session file cannot be used as asked."""


@dataclass(frozen=True)
class SessionMeta:
    """The environment the session was recorded in.

    Stored because a resumed session is not the same run: the model can be
    different, the working directory can be different, the sandbox mode can be
    different, and the history says nothing about any of them.  What is done
    with the difference is `environment_note`'s problem, not this one's.
    """

    session_id: str
    version: int = ROLLOUT_VERSION
    created: float = 0.0
    cwd: str = ""
    provider: str = ""
    model: str = ""
    sandbox_mode: str = ""
    approval_policy: str = ""
    forked_from: str | None = None
    forked_at: int | None = None

    def to_json(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "version": self.version,
            "created": self.created,
            "cwd": self.cwd,
            "provider": self.provider,
            "model": self.model,
            "sandbox_mode": self.sandbox_mode,
            "approval_policy": self.approval_policy,
            "forked_from": self.forked_from,
            "forked_at": self.forked_at,
        }

    @classmethod
    def from_json(cls, payload: dict[str, Any]) -> SessionMeta:
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in payload.items() if k in known})

    def describe(self) -> str:
        where = self.cwd or "?"
        return f"{self.session_id} | {self.model or '?'} | {self.sandbox_mode or '?'} | {where}"


def new_session_id() -> str:
    """Sortable, unique enough, and readable in `ls`.

    Time first so that listing a directory is listing a history.  The pid is
    what makes two agents started in the same second land in different files --
    which matters because the alternative is the two-writer corruption above.
    """
    return f"{time.strftime('%Y%m%dT%H%M%S')}-{os.getpid()}"


# -- serialising one history item ------------------------------------------


def _dump_item(item: HistoryItem) -> dict[str, Any]:
    if isinstance(item, UserMessage):
        return {"type": "user", "text": item.text}
    if isinstance(item, SystemNote):
        return {"type": "system_note", "text": item.text}
    if isinstance(item, ToolResult):
        return {
            "type": "tool_result",
            "call_id": item.call_id,
            "name": item.name,
            "content": item.content,
        }
    if isinstance(item, AssistantMessage):
        return {
            "type": "assistant",
            "text": item.text,
            "tool_calls": [
                {
                    "call_id": c.call_id,
                    "name": c.name,
                    "arguments": c.arguments,
                    # The string the model sent, byte for byte.  Chapter 1 keeps
                    # it so a history can be re-sent without a lossy round trip;
                    # a rollout that drops it re-introduces exactly that loss one
                    # process boundary later.
                    "raw_arguments": c.raw_arguments,
                }
                for c in item.tool_calls
            ],
        }
    raise AssertionError(f"unserialisable history item: {item!r}")  # pragma: no cover


def _load_item(record: dict[str, Any]) -> HistoryItem:
    kind = record.get("type")
    if kind == "user":
        return UserMessage(record["text"])
    if kind == "system_note":
        return SystemNote(record["text"])
    if kind == "tool_result":
        return ToolResult(record["call_id"], record["name"], record["content"])
    if kind == "assistant":
        calls = tuple(
            ToolCall(
                c["call_id"],
                c["name"],
                c.get("arguments"),
                c["raw_arguments"],
            )
            for c in record.get("tool_calls", ())
        )
        return AssistantMessage(record.get("text", ""), calls)
    raise RolloutError(f"unknown record type {kind!r}")


def _migrate(record: dict[str, Any], version: int) -> dict[str, Any]:
    """Bring one record forward to `ROLLOUT_VERSION`.

    A migration per version step, applied in order, each one small enough to
    read.  The alternative -- refusing to open old files -- is the version of
    F07-09 that makes the user's problem worse: their session is not corrupt,
    it is merely older than the program.
    """
    if version < 2 and record.get("type") == "assistant":
        record = dict(record)
        record["tool_calls"] = [
            # Version 1 had no `raw_arguments`.  Re-encoding the parsed object is
            # the best available reconstruction and is *not* the original string:
            # key order and whitespace are this program's, not the model's.  It
            # is written down here rather than pretended away.
            {**c, "raw_arguments": c.get("raw_arguments") or json.dumps(c.get("arguments") or {})}
            for c in record.get("tool_calls", ())
        ]
    return record


# -- writing ----------------------------------------------------------------


class RolloutWriter:
    """Appends history items to one file, and refuses to share it.

    The lock is a separate file created with `O_EXCL`, not an advisory lock on
    the rollout itself: it has to work on Windows, where the POSIX locking
    calls do not exist, and it has to be inspectable -- a stale lock names the
    pid that left it, so the user can decide rather than guess.
    """

    def __init__(self, path: Path, meta: SessionMeta, *, enabled: bool = True) -> None:
        self.path = Path(path)
        self.meta = meta
        self.enabled = enabled
        self._lock_fd: int | None = None
        self._items = 0
        if not self.enabled:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._acquire_lock()
        if not self.path.exists() or self.path.stat().st_size == 0:
            self._write({"type": "meta", **meta.to_json()})

    # -- lock ---------------------------------------------------------------

    @property
    def lock_path(self) -> Path:
        return self.path.with_suffix(self.path.suffix + ".lock")

    def _acquire_lock(self) -> None:
        try:
            fd = os.open(self.lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            holder = self.lock_path.read_text(encoding="utf-8", errors="replace").strip()
            raise RolloutError(
                f"{self.path} is already open by another minicodex (pid {holder or '?'}). "
                f"Resume it there, or delete {self.lock_path} if that process is gone."
            ) from None
        os.write(fd, str(os.getpid()).encode())
        self._lock_fd = fd

    def release(self) -> None:
        if self._lock_fd is not None:
            os.close(self._lock_fd)
            self._lock_fd = None
            try:
                self.lock_path.unlink()
            except OSError:  # pragma: no cover - someone else already cleaned up
                pass

    def __enter__(self) -> RolloutWriter:
        return self

    def __exit__(self, *exc: object) -> None:
        self.release()

    # -- appending ----------------------------------------------------------

    def _write(self, record: dict[str, Any]) -> None:
        if not self.enabled:
            return
        # `newline=""` because the default on Windows turns every "\n" into
        # "\r\n", and a torn "\r\n" pair is the one corruption two concurrent
        # writers actually produced in measurement.  One writer makes that
        # unreachable; writing "\n" makes it unreachable twice.
        with self.path.open("a", encoding="utf-8", newline="") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
            fh.flush()
            os.fsync(fh.fileno())

    def append(self, item: HistoryItem) -> None:
        self._write({"type_version": ROLLOUT_VERSION, **_dump_item(item)})
        self._items += 1

    def extend(self, items: Iterable[HistoryItem]) -> None:
        for item in items:
            self.append(item)

    def mark(self, kind: str, **payload: Any) -> None:
        """Record something that is not a history item -- a turn boundary, an abort.

        Kept in the same file rather than a second one: the only thing that makes
        "the run stopped here" useful is its position relative to the items, and
        two files have no shared order.
        """
        self._write({"type": "mark", "mark": kind, "ts": time.time(), **payload})


NULL_WRITER = RolloutWriter(Path(os.devnull), SessionMeta("null"), enabled=False)


# -- reading ----------------------------------------------------------------


@dataclass
class Rollout:
    meta: SessionMeta
    items: list[HistoryItem] = field(default_factory=list)
    marks: list[dict[str, Any]] = field(default_factory=list)
    #: Set when the file stopped making sense partway through.
    truncated_at: int | None = None
    path: Path | None = None

    def history(self) -> tuple[History, int]:
        """Rebuild, dropping any trailing turn that was never finished.

        Returns the history and the number of items dropped.  The rule is not
        "find the last complete turn" as a special case: the history refuses
        anything invalid anyway, so the loading rule is the simpler one --
        *replay, and remember the last point at which nothing was outstanding.*
        Where that point is falls out of chapter 1's invariant rather than being
        computed separately, which means it cannot disagree with it.
        """
        history = History()
        settled: list[HistoryItem] = []
        for item in self.items:
            try:
                _add(history, item)
            except HistoryError:
                # The file contains something no conversation can contain --
                # a result for a call that is not there, most likely because
                # the assistant message ahead of it was lost.  Stop; what is
                # already settled is still a conversation.
                break
            if not history.unanswered():
                settled = list(history.items)

        dropped = len(self.items) - len(settled)
        if dropped:
            history = History()
            for item in settled:
                _add(history, item)
        return history, dropped


def _add(history: History, item: HistoryItem) -> None:
    if isinstance(item, UserMessage):
        history.add_user(item.text)
    elif isinstance(item, SystemNote):
        history.add_system_note(item.text)
    elif isinstance(item, AssistantMessage):
        history.add_assistant(item.text, item.tool_calls)
    elif isinstance(item, ToolResult):
        history.add_tool_result(item.call_id, item.content)
    else:  # pragma: no cover
        raise AssertionError(item)


def read_rollout(path: Path) -> Rollout:
    """Load a session file, tolerating a damaged tail.

    Line by line, and the first line that does not parse ends the file.  Not
    "skip the bad line and carry on": a gap in the middle of a conversation is
    a conversation with a hole in it, and a hole is exactly the shape chapter
    1's invariant exists to reject.  Stopping keeps a prefix, which is a real
    conversation, over a filtered file, which is a plausible-looking fiction.
    """
    path = Path(path)
    if not path.exists():
        raise RolloutError(f"no session file at {path}")

    meta: SessionMeta | None = None
    items: list[HistoryItem] = []
    marks: list[dict[str, Any]] = []
    truncated_at: int | None = None
    # Index of the first item of a compaction baseline that is still being
    # written, or None when no compaction is in progress.
    baseline_from: int | None = None

    with path.open("r", encoding="utf-8", errors="replace", newline="") as fh:
        for lineno, line in enumerate(fh, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                truncated_at = lineno
                break
            if not isinstance(record, dict):
                truncated_at = lineno
                break
            if record.get("type") == "meta":
                meta = SessionMeta.from_json(record)
                continue
            if meta is None:
                raise RolloutError(_NO_HEADER.format(path=path))
            if record.get("type") == "mark":
                marks.append(record)
                kind = record.get("mark")
                if kind == "compacting":
                    # A compaction is about to write its replacement history.
                    # Remember where the replacement starts; nothing is
                    # discarded until the matching "compacted" says the
                    # replacement is all there.
                    baseline_from = len(items)
                elif kind == "compacted":
                    if baseline_from is None:
                        # A file written before there were two markers: a
                        # single one, placed *ahead* of the baseline.
                        items.clear()
                    else:
                        # Everything before the baseline was replaced in memory
                        # by the summary inside it.  Replaying it would undo the
                        # compaction on every resume -- the session would come
                        # back at the size that made it compact in the first
                        # place.
                        del items[:baseline_from]
                        baseline_from = None
                continue
            try:
                items.append(_load_item(_migrate(record, meta.version)))
            except (RolloutError, KeyError):
                truncated_at = lineno
                break

    if baseline_from is not None:
        # The file ends inside a compaction: "compacting" with no "compacted".
        # The replacement was never finished, so *it* is the part to discard --
        # what it was replacing is still here, whole.  With a single marker
        # written first, this same file loaded as one system note and nothing
        # else, with nothing reported as dropped.
        del items[baseline_from:]

    if meta is None:
        raise RolloutError(_NO_HEADER.format(path=path))
    return Rollout(meta=meta, items=items, marks=marks, truncated_at=truncated_at, path=path)


def list_sessions(directory: Path = DEFAULT_DIR) -> list[Rollout]:
    """Every readable session, newest first.  Unreadable ones are skipped, not raised."""
    directory = Path(directory)
    if not directory.is_dir():
        return []
    out = []
    for path in sorted(directory.glob("*.jsonl"), reverse=True):
        try:
            out.append(read_rollout(path))
        except RolloutError:
            continue
    return out


# -- forking ----------------------------------------------------------------


def fork(source: Path, *, upto: int | None = None, directory: Path | None = None) -> Path:
    """Start a new session from a prefix of an old one.

    Copies records into a new file.  Not "point the new session at the old file
    and remember an offset": two sessions sharing a file is the two-writer
    configuration again, and the second one's turns would appear in the first
    one's replay.  Copying a conversation costs kilobytes.
    """
    original = read_rollout(source)
    directory = Path(directory) if directory is not None else Path(source).parent
    kept = original.items if upto is None else original.items[:upto]

    meta = replace(
        original.meta,
        session_id=new_session_id(),
        version=ROLLOUT_VERSION,
        created=time.time(),
        forked_from=original.meta.session_id,
        forked_at=len(kept),
    )
    target = directory / f"{meta.session_id}.jsonl"
    with RolloutWriter(target, meta) as writer:
        writer.extend(kept)
    return target


# -- what changed since the session was written -----------------------------


def environment_note(meta: SessionMeta, current: SessionMeta) -> str | None:
    """One sentence per thing that is no longer what the history assumes.

    Returns None when nothing moved.  The model is told rather than protected:
    the history is full of paths relative to a directory that may not be this
    one, and of files it read under a permission set it may no longer have.
    Neither of those is recoverable by this program, and both are obvious to the
    model the moment it is told.
    """
    fields = [
        ("working directory", meta.cwd, current.cwd),
        ("model", meta.model, current.model),
        ("sandbox mode", meta.sandbox_mode, current.sandbox_mode),
        ("approval policy", meta.approval_policy, current.approval_policy),
    ]
    changed = [(label, was, now) for label, was, now in fields if was and now and was != now]
    if not changed:
        return None
    lines = [f"- {label}: was {was!r}, now {now!r}" for label, was, now in changed]
    return (
        "This session was resumed and the environment is not the one it was "
        "recorded in:\n"
        + "\n".join(lines)
        + "\nRe-check anything above that your earlier steps depended on before "
        "relying on it."
    )


def interrupted_note(dropped: int | None = None) -> str:
    """What the model is told about the turn that never finished.

    Without it the transcript reads as if the last thing in it simply happened.
    Measured against gpt-4o-mini on a history whose `apply_patch` was the turn
    that vanished, asking what it does first when told to carry on
    (`probe_resume.py`, "verify" = reads the file before touching it):

                                                  2026-08   2026-10
        no note at all                              0/13      0/10
        "resumed from a file on disk" (placebo)      0/5      0/10
        this text, first version (4 sentences)      10/19       --
        one sentence, no count                      10/11      9/10
        one sentence + the count  <- shipped        11/11      9/10

    The placebo arm is what makes the rest mean anything: a system note
    appearing is not the mechanism, the warning is.  And the first version --
    longer, more careful, with two sentences of advice on the end -- did worse
    than the plain sentence, twice, on independent runs.  The advice is the
    part the model already knows; the news is the part it cannot know.

    Two wordings for two situations.  Live interruption: the tools were
    answered and the answers say they were cut off, so there is no count to
    give.  Recovery from disk: whole messages are missing.
    """
    if dropped is None:
        # Not measured -- the probe reconstructs the from-disk case.  Kept to
        # the same shape as the measured one rather than written afresh.
        return (
            "You were interrupted by the user during the previous turn. Any tool "
            "call answered with 'interrupted' may have run partially, or not at all."
        )
    return (
        "The previous session ended without finishing its last turn. "
        f"{dropped} message(s) were discarded because they were incomplete."
    )


def replay(history: History, item: HistoryItem) -> None:
    """Put one stored item back into a live history, through the front door."""
    _add(history, item)


def rollout_path(session_id: str, directory: Path = DEFAULT_DIR) -> Path:
    return Path(directory) / f"{session_id}.jsonl"


def resolve(reference: str, directory: Path = DEFAULT_DIR) -> Path:
    """Accept a path, a session id, or `last`."""
    if reference == "last":
        sessions = list_sessions(directory)
        if not sessions:
            raise RolloutError(f"no sessions in {directory}")
        assert sessions[0].path is not None
        return sessions[0].path
    candidate = Path(reference)
    if candidate.exists():
        return candidate
    candidate = rollout_path(reference, directory)
    if candidate.exists():
        return candidate
    raise RolloutError(f"no session {reference!r} (looked in {directory})")
```

> 前面没单独讲的只有 `_NO_HEADER`（"这不是会话文件"的那句报错，做成常量是因为有两处要用同一句话）和 `RolloutError`。

### 16.2 `agent.py`

`Agent.__init__` 多两个参数：`rollout: RolloutWriter = NULL_WRITER` 和 `resume_from: History | None = None`，保存成同名的属性。
开头的 import 加上 `from minicodex.rollout import NULL_WRITER, RolloutWriter, interrupted_note, replay`。

`run()` 的开头从"新建一个空历史"变成：

```python
        if self.resume_from is not None:
            history = self._attach(self.resume_from)
        else:
            history = History(observer=self.rollout.append)
            if self.instructions is not None:
                history.add_system_note(self.instructions)
        history.add_user(user_message)
```

> - 默认的 `NULL_WRITER` 什么都不写，所以以前所有的测试照旧。
> - **恢复时不再加系统消息**：它已经在恢复出来的历史里了（就是第 6 章说的受保护前缀）。再加一遍，请求的最前面就会有两份权限说明，
>   而且在换了 `--sandbox-mode` 之后，这两份还互相矛盾。
> - 恢复出来的历史通过 `_attach` 重放进一个带观察者的新 `History`——于是它被完整地写进了**新的**会话文件。

其余的改动前面都出现过了：§10 的那个循环，§15 的 `_rebaseline` 和 `_attach`。

### 16.3 命令行

`__main__.py` 这一章改动比较多，整个文件：

```python
"""Command line entry point."""

from __future__ import annotations

import argparse
import asyncio
import os
import platform
import sys
import time
from dataclasses import replace
from pathlib import Path

from minicodex import __version__, system_prompt
from minicodex.agent import Agent
from minicodex.approval import AllowAll, CliApprover, Session, permissions_block
from minicodex.compaction import make_summariser
from minicodex.model import OLLAMA_BASE_URL, OPENAI_BASE_URL, ChatCompletionsModel
from minicodex.policy import APPROVAL_POLICIES, SANDBOX_MODES
from minicodex.recorder import Recorder
from minicodex.rollout import (
    DEFAULT_DIR,
    RolloutError,
    RolloutWriter,
    SessionMeta,
    environment_note,
    fork,
    interrupted_note,
    list_sessions,
    new_session_id,
    read_rollout,
    resolve,
    rollout_path,
)
from minicodex.rules import DEFAULT_RULES_PATH, RuleStore
from minicodex.tools import TOOL_SCHEMAS, default_tools

PROVIDERS = {
    "ollama": (OLLAMA_BASE_URL, "gemma4:31b-cloud"),
    "openai": (OPENAI_BASE_URL, "gpt-4o-mini"),
}


def _instructions(session: Session) -> str:
    """The system message: what the agent is, then what it may currently do.

    Permission state goes last, and that is not a layout preference.  It is the
    only part of this string that depends on the session's state, and providers
    cache a prompt by its prefix: volatile content near the top invalidates the
    cache whenever it changes. Chapter 13 has the measurements; the ordering
    costs nothing to get right now (F13-07).

    Rendered once, when the run starts.  A later `request_permissions` reports
    the new state in its tool result, but this message keeps the old one -- a
    known gap, recorded in FAULTS.md under chapter 5.
    """
    can_request = any(tool["function"]["name"] == "request_permissions" for tool in TOOL_SCHEMAS)
    block = permissions_block(session, can_request=can_request)
    return f"{system_prompt().rstrip()}\n\n{block}"


async def _ask(
    question: str,
    *,
    provider: str,
    base_url: str | None,
    model: str | None,
    session: Session,
    context_window: int | None,
    resume: str | None,
    session_dir: Path,
) -> int:
    default_url, default_model = PROVIDERS[provider]
    recorder = Recorder()
    llm = ChatCompletionsModel(
        base_url=base_url or default_url,
        model=model or default_model,
        # Read from the environment, never from a flag: a key in argv shows up
        # in shell history and in `ps`.
        api_key=os.environ.get("OPENAI_API_KEY") if provider == "openai" else None,
        tools=TOOL_SCHEMAS,
    )
    current = SessionMeta(
        session_id=new_session_id(),
        created=time.time(),
        cwd=os.getcwd(),
        provider=provider,
        model=model or default_model,
        sandbox_mode=session.mode,
        approval_policy=session.policy,
    )

    resume_from = None
    if resume is not None:
        try:
            path = resolve(resume, session_dir)
        except RolloutError as exc:
            print(exc, file=sys.stderr)
            return 1
        loaded = read_rollout(path)
        resume_from, dropped = loaded.history()
        if loaded.truncated_at is not None:
            print(f"[session file damaged from line {loaded.truncated_at}; using what precedes it]")
        if dropped:
            resume_from.add_system_note(interrupted_note(dropped))
            print(f"[resumed: {dropped} incomplete message(s) discarded]")
        note = environment_note(loaded.meta, current)
        if note is not None:
            resume_from.add_system_note(note)
            print("[environment changed since this session was recorded]")
        current = replace(current, forked_from=loaded.meta.session_id)
        # A resumed session is written to a *new* file rather than appended to
        # the old one.  Appending would mean the recovered prefix and the
        # discarded tail share a file, so the next recovery would have to
        # rediscover which of the two it was looking at.
        print(f"[resumed {len(resume_from)} message(s) from {path}]")

    writer = RolloutWriter(rollout_path(current.session_id, session_dir), current)
    agent = Agent(
        llm,
        default_tools(session=session),
        recorder=recorder,
        instructions=_instructions(session),
        context_window=context_window,
        rollout=writer,
        resume_from=resume_from,
        # The summariser shares the client, and therefore the provider and the
        # key, but not the tools: `make_summariser` builds its own history.
        summariser=make_summariser(llm) if context_window else None,
    )

    result = await agent.run(question)

    print(result.final_text or "(no answer)")
    print(f"\n[{llm.model} | {result.stop_reason} after {result.turns_used} turn(s)]")
    print(f"[{session.describe()}]")
    for event in result.compactions:
        print(f"[{event.describe()}]")
    print(f"[tokens: {agent.calibration.describe()}]")
    print(f"[transcript: {recorder.path}]")
    print(f"[session: {writer.path}  (resume with: minicodex ask ... --resume last)]")
    writer.release()
    return 0


def _add_permission_flags(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--sandbox-mode",
        choices=SANDBOX_MODES,
        default="read-only",
        help="what the agent may do without anyone being asked (default: read-only)",
    )
    parser.add_argument(
        "--approval-policy",
        choices=APPROVAL_POLICIES,
        default="on-request",
        help="what happens to everything else (default: on-request)",
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="approve everything without asking. For scripts you have read.",
    )


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
    ask.add_argument("--provider", choices=sorted(PROVIDERS), default="ollama")
    ask.add_argument("--base-url", default=None)
    ask.add_argument("--model", default=None)
    ask.add_argument(
        "--context-window",
        type=int,
        default=None,
        help=(
            "size of the model's context window in tokens. Compaction is off "
            "without it: nothing in this program can discover the number, and "
            "a guessed one is a silently wrong budget"
        ),
    )
    ask.add_argument(
        "--resume",
        default=None,
        metavar="SESSION",
        help="continue a previous session: its id, its path, or `last`",
    )
    ask.add_argument("--session-dir", type=Path, default=DEFAULT_DIR)
    _add_permission_flags(ask)

    sessions = sub.add_parser("sessions", help="list saved sessions, newest first")
    sessions.add_argument("--session-dir", type=Path, default=DEFAULT_DIR)

    fork_cmd = sub.add_parser("fork", help="branch a new session from an old one")
    fork_cmd.add_argument("session", help="session id, path, or `last`")
    fork_cmd.add_argument(
        "--upto",
        type=int,
        default=None,
        help="keep only the first N messages (see `minicodex sessions` for the count)",
    )
    fork_cmd.add_argument("--session-dir", type=Path, default=DEFAULT_DIR)

    stub = sub.add_parser("serve-stub", help="replay recorded Ollama responses on a local port")
    stub.add_argument("--port", type=int, default=11435)

    sub.add_parser("rules", help="list the approvals this project has remembered")

    forget = sub.add_parser("forget", help="revoke a remembered approval by its number")
    forget.add_argument("index", type=int)

    args = parser.parse_args(argv)

    if args.version:
        print(f"minicodex {__version__}")
        print(f"python    {platform.python_version()} ({sys.platform})")
        return 0

    if args.command == "sessions":
        return _list_sessions(args.session_dir)

    if args.command == "fork":
        return _fork(args.session, args.upto, args.session_dir)

    if args.command == "rules":
        return _list_rules()

    if args.command == "forget":
        return _forget_rule(args.index)

    if args.command == "ask":
        session = Session(
            mode=args.sandbox_mode,
            policy=args.approval_policy,
            rules=RuleStore(Path(DEFAULT_RULES_PATH)),
            approver=AllowAll() if args.yes else CliApprover(),
        )
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
            )
        )

    if args.command == "serve-stub":
        from minicodex.stub import serve

        serve(args.port)
        return 0

    parser.print_help()
    return 0


def _list_sessions(directory: Path) -> int:
    rollouts = list_sessions(directory)
    if not rollouts:
        print(f"no sessions in {directory}")
        return 0
    for rollout in rollouts:
        _, dropped = rollout.history()
        flags = []
        if dropped:
            flags.append(f"{dropped} incomplete")
        if rollout.truncated_at is not None:
            flags.append(f"damaged at line {rollout.truncated_at}")
        if rollout.meta.forked_from:
            flags.append(f"from {rollout.meta.forked_from}")
        suffix = f"   [{', '.join(flags)}]" if flags else ""
        print(f"  {rollout.meta.describe()}  {len(rollout.items)} msg{suffix}")
    print("\nresume with: minicodex ask '<next instruction>' --resume last")
    return 0


def _fork(reference: str, upto: int | None, directory: Path) -> int:
    try:
        path = fork(resolve(reference, directory), upto=upto, directory=directory)
    except RolloutError as exc:
        print(exc, file=sys.stderr)
        return 1
    print(f"forked to {path}")
    return 0


def _list_rules() -> int:
    """A rule nobody can see is a rule nobody can revoke."""
    store = RuleStore(Path(DEFAULT_RULES_PATH))
    if not len(store):
        print("no remembered approvals")
        return 0
    for index, rule in enumerate(store.all()):
        print(f"  {index}  {rule.describe()}")
    print(f"\nrevoke with: minicodex forget N   ({DEFAULT_RULES_PATH})")
    return 0


def _forget_rule(index: int) -> int:
    store = RuleStore(Path(DEFAULT_RULES_PATH))
    try:
        rule = store.forget(index)
    except IndexError:
        print(f"no rule numbered {index}; run `minicodex rules` to see them", file=sys.stderr)
        return 1
    print(f"revoked: {rule.describe()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

> 相对第 6 章：
>
> - **`_ask`** 多了 `resume` 和 `session_dir` 两个参数。
>   - 先造出"当前环境"的 `SessionMeta`（新的会话 id、目录、服务商、模型、沙箱模式、审批策略）。
>   - 如果要恢复：找到文件 → 读出来 → 重建历史。然后**每一件要告诉模型的事，也对用户说一遍**：文件有损坏、丢弃了几条不完整的消息、环境变了。
>     这几行 `print` 不是日志——否则用户看到的是一段凭空少了几条消息的对话，却没有任何解释。
>   - `current = replace(current, forked_from=...)`：新会话记下它是从哪个会话接着来的。
>   - **恢复是写进一个新文件，而不是接着写旧文件。** 接着写的话，"恢复出来的前半段"和"被丢弃的尾巴"会在同一个文件里，下次恢复还得再分辨一次。
>   - 结束时打印会话文件的位置，放掉锁。
> - **`ask`** 多了 `--resume` 和 `--session-dir` 两个选项；新增 **`sessions`** 和 **`fork`** 两个子命令。
> - **`_list_sessions`**：每个会话一行。它对每个会话调用一次 `rollout.history()`，用**同一段代码**算出"有几条不完整"——
>   如果列表自己去判断"完不完整"，那就是同一条规则的又一份实现。
> - **`_fork`**：调用 `fork`，出错时把话打印出来，返回 1。

### 16.4 真跑一次

（Windows，gpt-4o-mini。路径里很长的前半截用 `...` 代替了。）

先正常跑一次：

```
$ uv run minicodex ask "Read src/minicodex/tool_errors.py and tell me the two prefixes it defines." --provider openai
1. `ERROR_PREFIX`: "Error:"
2. `PERMISSION_PREFIX`: "Permission denied:"

[gpt-4o-mini | completed after 2 turn(s)]
[sandbox_mode=read-only, approval_policy=on-request]
[tokens: x0.83 from 2 observation(s)]
[transcript: .minicodex\recordings\session-1790821487.jsonl]
[session: ...\sessions\20261001T102447-29040.jsonl  (resume with: minicodex ask ... --resume last)]
```

会话文件有 6 行：meta、系统消息、用户消息、带调用的助手消息、工具结果、最后的回答。

要演示恢复，需要一个"停在工具执行中"的文件。这里用最直接的办法造一个：**取这个文件的前 4 行**（正好停在带调用的助手消息之后——
§6 测出来的、进程被杀时最常见的位置），另存成一个新的会话文件。然后：

```
$ uv run minicodex sessions
  20261001T102447-29040 | gpt-4o-mini | read-only | ...\steps\step07_resume  5 msg
  20261001T102000-999 | gpt-4o-mini | read-only | ...\steps\step07_resume  3 msg   [1 incomplete]

resume with: minicodex ask '<next instruction>' --resume last
```

```
$ uv run minicodex ask "carry on" --provider openai --resume 20261001T102000-999
[resumed: 1 incomplete message(s) discarded]
[resumed 3 message(s) from ...\sessions\20261001T102000-999.jsonl]
The file `src/minicodex/tool_errors.py` defines two prefixes:

1. `ERROR_PREFIX = "Error:"`
2. `PERMISSION_PREFIX = "Permission denied:"`

[gpt-4o-mini | completed after 2 turn(s)]
...
```

丢掉了那条没有结果的调用，告诉了用户，告诉了模型，然后模型重新读了文件、答完了题。再分一条线出来：

```
$ uv run minicodex fork last --upto 3
forked to ...\sessions\20261001T102511-10828.jsonl
$ uv run minicodex sessions
  20261001T102511-10828 | gpt-4o-mini | read-only | ...\steps\step07_resume  3 msg   [from 20261001T102507-58260]
  20261001T102507-58260 | gpt-4o-mini | read-only | ...\steps\step07_resume  7 msg   [from 20261001T102000-999]
  20261001T102447-29040 | gpt-4o-mini | read-only | ...\steps\step07_resume  5 msg
  20261001T102000-999 | gpt-4o-mini | read-only | ...\steps\step07_resume  3 msg   [1 incomplete]
```

每个会话都记着自己的来路（`[from ...]`）。

### 16.5 这一段的测试

```python
def test_sessions_are_listed_newest_first(tmp_path: Path) -> None:
    for name in ("20260101T000000-1", "20260102T000000-1"):
        RolloutWriter(tmp_path / f"{name}.jsonl", meta(name)).release()
    listed = [r.meta.session_id for r in list_sessions(tmp_path)]
    assert listed == ["20260102T000000-1", "20260101T000000-1"]


def test_an_unreadable_file_does_not_break_the_listing(tmp_path: Path) -> None:
    RolloutWriter(tmp_path / "good.jsonl", meta("good")).release()
    (tmp_path / "bad.jsonl").write_text("garbage\n", encoding="utf-8")
    assert [r.meta.session_id for r in list_sessions(tmp_path)] == ["good"]


def test_resolve_accepts_an_id_a_path_or_last(tmp_path: Path) -> None:
    path = tmp_path / "20260101T000000-1.jsonl"
    RolloutWriter(path, meta("20260101T000000-1")).release()
    assert resolve("20260101T000000-1", tmp_path) == path
    assert resolve(str(path), tmp_path) == path
    assert resolve("last", tmp_path) == path
    with pytest.raises(RolloutError):
        resolve("nope", tmp_path)


async def test_a_resumed_run_appends_to_a_new_file(tmp_path: Path) -> None:
    """Resuming does not reopen the old file.

    Appending to it would put the recovered prefix and the discarded tail in
    one file, and the next recovery would have to work out which of the two it
    was looking at.
    """
    first = tmp_path / "first.jsonl"
    with RolloutWriter(first, meta("first")) as writer:
        history = History(observer=writer.append)
        history.add_user("go")
        history.add_assistant("calling", [call("call_1")])

    restored, _ = read_rollout(first).history()
    second = tmp_path / "second.jsonl"
    with RolloutWriter(second, meta("second")) as writer:
        agent = Agent(ScriptedModel(["done"]), {}, rollout=writer, resume_from=restored)
        await agent.run("carry on")

    assert len(read_rollout(first).items) == 2  # untouched
    assert [type(i).__name__ for i in read_rollout(second).items] == [
        "UserMessage",  # the replayed one
        "UserMessage",  # the new instruction
        "AssistantMessage",
    ]


async def test_a_resumed_run_does_not_repeat_the_instructions() -> None:
    """The system message is already in the history that came off the disk.

    Adding this run's instructions again would put two permission statements
    at the front of the request -- and after `--sandbox-mode` changed, two that
    disagree.  Found by a mutation that added them back and turned nothing red.
    """
    restored = History()
    restored.add_system_note("instructions as recorded")
    restored.add_user("go")
    restored.add_assistant("done")

    model = ScriptedModel(["ok"])
    agent = Agent(model, {}, instructions="instructions for this run", resume_from=restored)
    await agent.run("carry on")

    system = [m["content"] for m in model.sent[0] if m["role"] == "system"]
    assert "instructions as recorded" in system
    assert "instructions for this run" not in system
```

> - 列表从新到旧；一个坏文件不影响列表；`resolve` 认三种写法，认不出就抛异常；
> - 恢复之后的运行写进新文件，旧文件一个字节都没动；
> - **恢复时不重复加系统消息。** 这个测试是变异测试逼出来的：把"恢复时也加一遍系统消息"写回去，当时没有任何测试变红。

```bash
git add src/minicodex/agent.py src/minicodex/__main__.py src/minicodex/rollout.py tests/test_faults_ch07.py
git commit -m "feat(cli): --resume, sessions and fork"
```

---

## §17 逐条验证

### 17.1 全量，两个系统

```
Windows
$ uv run pytest
1290 passed, 9 skipped in 13.85s

Linux（WSL，Python 3.12）
$ uv run pytest
1299 passed in 10.39s
```

```
$ uv run ruff check
All checks passed!
$ uv run ruff format --check
46 files already formatted
```

Windows 上跳过的 9 个：7 个是第 2 章的 F02-10，1 个是第 5 章的符号链接测试，1 个是这一章的 `killpg` 测试。**在 Linux 上它们全部真正执行，一个都不跳过。**

这一章不给 CI 加新的步骤：新测试都是普通的单元测试，`pytest` 已经在跑。两个探针一个要杀真进程、写几十 MB，一个要 API key——
它们是**测量工具，不是回归测试**，放进 CI 只会让 CI 变慢、变脆。**不加也是一个决定，也要说出理由。**

### 17.2 变异测试

十七个决定，每次撤销一个（在 Linux 上、在一份临时拷贝里做）：

```
baseline: 0 failed

mutation                                                           failed  first tests to notice
history: never tell the observer                                       12  test_F07_01_history_survives_the_process, test_F07_01_written_during_the_ru
recovery: drop a trailing unanswered call instead of replaying          1  test_F07_02_a_partially_answered_turn_is_dropped_whole
reader: skip a bad line and keep reading                                1  test_F07_03_damage_in_the_middle_does_not_resurrect_the_tail
reader: skip an unknown record type and keep reading                    1  test_F07_03_an_unknown_record_type_is_also_a_boundary
lock: let a second writer in                                            3  test_F07_08_a_second_writer_is_refused, test_F07_08_the_lock_is_released_on
writer: translate newlines again                                        0
writer: do not fsync                                                    0
cancel: answer only the call that was running                           1  test_F07_04_cancelling_a_tool_answers_every_issued_call
cancel: swallow an interrupt that arrives while waiting on the model      1  test_F07_04_an_interrupt_between_turns_is_not_swallowed
cancel: leave the command running                                       1  test_F07_06_cancelling_a_command_kills_it
note: reword the measured sentence                                      1  test_F07_05_the_wording_is_pinned
environment: report an unrecorded field as a change                     1  test_F07_07_unknown_fields_are_not_reported_as_changes
migration: do not rebuild raw_arguments for version 1                   1  test_F07_09_a_version_1_file_still_loads
fork: ignore --upto                                                     2  test_F07_10_a_fork_copies_rather_than_references, test_F07_10_a_fork_that_c
compaction: write the marker before the baseline again                  1  test_an_agent_that_compacts_can_be_resumed_from_its_file
compaction: keep a half-written baseline                                1  test_a_kill_inside_the_new_baseline_keeps_what_it_was_replacing
resume: add the instructions a second time                              1  test_a_resumed_run_does_not_repeat_the_instructions

15/17 caught
  survived: writer: translate newlines again
  survived: writer: do not fsync
tree green again: True
```

**两条没被抓住，而且抓不住是意料之中的：**

- **不 `fsync`**：它防的是"操作系统还没写盘就断电"。杀进程测不出来——进程死了，操作系统还活着，照样会把攒着的内容写下去。
- **恢复换行符的自动转换**：它防的是 Windows 上两个写入者之间的撕裂，而第二个写入者已经被锁挡在外面了。

这两条都是"便宜的保险"，和 §8 那个"读到坏行就停"是一类：**没被测量撑着，如实标出来。**

其余十五条里有十二条只靠一个测试守着。而这张表在得到现在这个样子之前，变异测试先后揪出了三个**测试本身**的毛病：
F07-06 的测试连着两版都是空的（§11.1）；"恢复时不重复系统消息"根本没有测试（§16.5）。

### 17.3 对照清单

| 编号 | 猜测 | 结果 |
|---|---|---|
| F07-01 | 进程退出，历史没了 | **成立**；"跑完再存"在被杀时留下 0 字节 |
| F07-02 | 文件停在"有调用、没结果" | **成立，而且几乎是唯一的情况**（19/20、20/20） |
| F07-03 | 最后一行被撕开 | **没有复现**（两个系统、三种写法）；防御照写，标明没测到 |
| F07-04 | Ctrl-C 停工具还是停整轮 | **成立**：`CancelledError` 穿过了 `except Exception` |
| F07-05 | 模型不知道自己被打断过 | **成立**（0/10 先检查）；一句话 9/10，安慰剂 0/10 |
| F07-06 | 命令没被清理 | **成立**（Linux 实测：孙进程活了下来） |
| F07-07 | 环境变了 | **动工前就挡住**；效果测了（8/10），原因没测 |
| F07-08 | 两个进程写同一个文件 | **成立，但两个系统上坏法不同**：Windows 丢记录，Linux 不丢但混在一起 |
| F07-09 | 旧文件读不了 | **在本章内就真的发生了一次**（`raw_arguments`） |
| F07-10 | 分出的线共用写入 | **动工前就挡住**：复制 |

---

## §18 收工

### 18.1 这一章的文件

| 文件 | 状态 | 在哪一节 |
|---|---|---|
| `src/minicodex/rollout.py` | 新增 | §4、§7、§9、§12–§15，全文在 §16.1 |
| `src/minicodex/history.py` | 加一个参数、一个方法 | §5 |
| `src/minicodex/agent.py` | 改动 | §10、§15.1、§16.2 |
| `src/minicodex/shell.py` | 改两处 | §11 |
| `src/minicodex/__main__.py` | 改动 | §16.3 |
| `tests/test_faults_ch07.py` | 新增 | 分散在各节 |
| `probe_rollout.py` | 新增 | §3 |
| `probe_resume.py` | 新增 | §12 |

### 18.2 提交、推送、PR

```
feat(rollout): write the session as it happens, and recover by replaying it
feat(rollout): one writer per session file, and a reader that stops at the first bad line
fix(agent,shell): answer every issued call and kill the command when a turn is cancelled
feat(rollout): tell the model about the turn it cannot see, and about a changed environment
feat(rollout): versions and migration, fork by copy, and a compaction baseline that survives a kill
feat(cli): --resume, sessions and fork
```

```bash
git push -u origin feat/rollout
```

PR 描述里要如实写的几条：

> - F07-03（撕裂的行）**没能复现**。防御代码照样在，模块的 docstring 里写明了它没被测到。
> - 那把锁目前从命令行**碰不到**（每次运行都有自己的文件名）。故意留着，理由见 `README`。
> - "两个进程写同一个文件"在 Windows 和 Linux 上的结果不一样，测试断言的是两边都成立的那一部分。
> - `fsync` 和 `newline=""` 没有测试守着，也没法用杀进程的办法测。

### 18.3 自己审一遍

**1 · `history()` 在遇到 `HistoryError` 后直接停，安全点可能一个都没有，返回一个空历史。调用的人会不会以为"恢复成功，只是没内容"？**

`dropped` 就是给调用的人判断用的，命令行会把它打印出来。空历史加上"全部被丢弃"是一个合法的结果，意思是"这个文件里没有任何一段完整的对话"——
在第一轮就崩溃的会话里，这是事实。有一个测试专门钉住了这个组合。

**2 · "两个进程"那个测试启动真进程、写 8000 条记录，而且测的不是我们的代码。**

约一秒。保留，理由在它的 docstring 里：它测的是**我们的代码依赖的前提**。哪天这个前提不成立了，它会变红——那也是有用的信息：说明锁可以拿掉了。

**3 · 锁从命令行根本碰不到，是不是多余？**

§9.1 讲过：它不是"多一层要理解的东西"，而是把一个目前碰巧成立的事实变成一直成立的事实。已在 `README` 里写明"目前碰不到"。

**4 · 每条记录里有 `type_version`，meta 里又有 `version`，两个名字。**

是两个东西：meta 里的是这个**文件**的格式版本；每条记录里的是**写这一条时**的版本。目前加载时只用到前者。

**5 · `_ask` 里 `writer.release()` 写在函数最后，中途出了异常就不会执行，会留下一个 `.lock` 文件。**

是的，这是一个真的问题。每次运行的文件名都不同，所以留下的锁挡不住任何人，只是白白留在目录里。这一章没有修它；后面讲出错与重试的那一章会回来处理。

---

## §19 codex 是怎么做的

- **会话文件同样是一行一条、只追加**，路径按日期分了层：`~/.codex/sessions/年/月/日/rollout-时间-编号.jsonl`。
  按日期分层这一章没做，理由只是规模——一个目录里有一万个文件时你才会想要它。
- **文件开头同样是一条环境信息**，带着目录、指令、来源、程序的版本号。
- **恢复的入口也叫 `resume`，也支持"最近的一个"**。它恢复时是**接着写同一个文件**——这正是 §9.1 说的、会让"文件名不重复"不再成立的那种设计。
- **`compact_resume_fork.rs`**——有一个测试文件的名字，就是把压缩、恢复、分叉连在一起。测试文件的名字是最诚实的故障记录：
  这三件事会互相干扰，所以才需要一个专门测它们组合的文件。§15 的那个窗口，就是"压缩"和"恢复"的组合。
- **中断**：codex 区分"打断当前这一轮"和更粗的"关闭"，并且有专门测中止的一组测试。§10 的"先回答完所有调用再结束"，在它那里对应着"中止的原因"和注入给模型的一段说明。

---

## §20 回头看：这一章撞到了什么

**预测到了，并且成立的：** F07-01、F07-02（比猜的普遍得多）、F07-04、F07-05、F07-06、F07-08（两个系统上坏法不同）、F07-09。

**预测到了，动工前就挡住的：** F07-07、F07-10。**预测了，没复现的：** F07-03。

**没预测到的：**

| 故障 | 怎么发现的 | 挡住它的东西 |
|---|---|---|
| 在 Agent 里写文件意味着四个调用点，第五个会被忘掉，而且不报错 | 🟣 设计时 | `History` 的观察者 |
| "扔掉最后一条"会让没回答的调用变多 | 🟢 边界测试 | 重放，记住"什么都不欠"的时刻 |
| Windows 上 `\r\n` 两个字节之间会被另一个写入者插进来 | 🟠 看坏行的字节 | `newline=""`；以及根本不许有第二个写入者 |
| 存了解析后的参数、没存原始字符串，恢复后字节变了 | 🟣 重读代码 | 版本 2 + 迁移 |
| 四句话的说明输给了一句话 | 🔵 真模型 | 只说模型不可能知道的那一件事 |
| **压缩的标记写在了替代品之前：窗口里被杀，整个会话只剩一条系统消息** | 🟣 改写本章时，逐个位置问"文件在这里结束会怎样" | 两个标记，替代品夹在中间 |
| **"丢记录"是 Windows 的现象，测试却把它断言成了普遍事实，在 Linux 上是红的** | 🔴 改写本章时，第一次在 Linux 上跑 | 断言两个系统上都成立的部分 |
| **F07-06 的测试是空的，而且修了一次还是空的** | ⚪ 变异测试（Linux） | 让孙进程写标记，等得比它睡得久 |
| "恢复时不重复系统消息"没有测试 | ⚪ 变异测试 | 补测试 |
| 两个没人用的名字（`TurnInterrupted`、`items_of`）一直留在代码里 | 🟣 改写本章时 | 删掉 |

标记：🔴 崩溃/测试红 · 🟡 静默 · 🟢 边界测试 · 🔵 真模型 · 🟠 看日志 · 🟣 审查 · ⚪ 工具

后四条有一个共同点：**它们都是换了一个角度才看见的。** 换一个操作系统，Windows 上的"普遍事实"变成了一台机器上的现象；
让一个一直被跳过的测试真正执行一次，才发现它什么都没测；不问"崩溃通常停在哪"，改问"如果停在这里呢"，才找到那个窗口。

> 第 5 章说：一个检查器最容易以为自己检查过了。第 6 章说：检查那个检查器的工具也一样。这一章再加一句：
> **你量到的，是你量的那台机器、那个时刻、那些样本。** 20 次里 20 次，说的是"通常"；它不替你回答"有没有例外"。

---

## 如果你只记住三件事

1. **崩溃停在哪里，是可以量的，而且答案不是"随机"。**
   一轮的时间几乎全在工具里，所以进程被杀时，文件几乎总是停在"发起了调用、还没有结果"的位置。**恢复不是边界情况，是主路径。**
   下次写"如果文件坏了……"之前，先去量一量它会坏成什么样——以为一定会发生的那条（撕裂的行）一次都没出现，清单上只有一句话的那条是常态。

2. **恢复的代码不要重新定义"什么是合法的"。**
   "找到最后一个完整的轮次"听起来是恢复该做的事，但那是第 1 章那条规则的第二份实现。正确的做法是**重放，让原来那扇门去拒绝**，
   然后记住最后一个"什么都不欠"的时刻。回报是：分叉时切在一轮中间，一行新代码都不用写。

3. **"先写下来，再往下做"对标记也成立。**
   一个说"前面的都不算了"的标记，必须等替代品写完才写。检查一种文件格式的办法，是对它的**每一个位置**问：如果文件正好在这里结束，读回来的是什么？

---

## 动手练习

1. 把 `Rollout.history()` 改成"最后一条如果是带调用的助手消息就扔掉"，跑测试，看哪一个红。再想一个它处理不了、而现在的测试也没覆盖到的形状，把它写成测试。

2. 把 `History.__init__` 的 `observer` 去掉，改回在 `agent.py` 里每个 `add_*` 后面各写一行。然后给 Agent 加一个新功能，往历史里多放一种记录。
   **看你会不会忘掉那一行。** 忘掉之后跑一次 `--resume`，看少了什么、有没有任何东西报错。

3. 在你自己的机器上跑 `uv run python probe_rollout.py`。和 §6、§8、§9 的数字比一比——**尤其是"两个进程"那一段**。
   如果你的系统上撕出了半行，你就有了这一章没有的数据。

4. 把 `_rebaseline` 里两个标记的顺序改回"先标记、后替代品"（并去掉第二个标记），跑测试，看哪一个红。
   然后照 §15.1 的办法手工造那个文件，亲眼看一次"八条变一条"。

5. 给 `interrupted_note` 写一个新的变体，比如把被丢掉的那个工具的名字也告诉模型（提示：`Rollout.items` 里还有那条被丢掉的助手消息）。
   用 `probe_resume.py` 的办法量它，**记得带上安慰剂组**。比 9/10 好，说明"是哪个工具"是有用的信息；一样，说明模型只需要知道"有事发生过"。两个结果都值得知道。

下一章讲工具并发：一轮里三个调用，能不能同时跑。这一章刚把"每个调用必须有结果"变成了一条**在被打断时也要成立**的规则；下一章会让它同时面对三个调用。
